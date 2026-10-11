# -*- coding: utf-8 -*-
"""
Agent2API - OpenAI Responses API 翻译层

上游（WorkBuddy / CodeBuddy）只暴露 /v1/chat/completions，且只支持流式。
本模块把 OpenAI Responses API（/v1/responses）翻译成 chat/completions：

  输入翻译：
    - input（str / 数组）→ messages
        * message 项（role + content）        → 普通 message
        * function_call 项（历史工具调用）      → assistant 带 tool_calls
        * function_call_output 项             → tool 角色 message
        * system 角色 message                  → system message
    - instructions            → system message（置于最前）
    - tools / tool_choice     → chat 的 tools / tool_choice
    - text.format             → response_format
    - max_output_tokens       → max_tokens
    - temperature / top_p ... → 透传
    - previous_response_id / include / store / truncation 等本层不持久化，忽略

  输出翻译：
    - 非流式：聚合上游 chat 流 → 标准 response 对象
    - 流式：  逐块把上游 chat SSE 转成 Responses 事件
        response.created / response.in_progress /
        response.output_item.added / response.content_part.added /
        response.output_text.delta / response.output_text.done /
        response.content_part.done / response.output_item.done /
        response.function_call_arguments.delta / .done /
        response.completed
"""
from __future__ import annotations

import json
import time
import uuid

import proxy


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def _new_id(prefix: str) -> str:
    return "%s%s" % (prefix, uuid.uuid4().hex[:24])


def _map_usage(u: dict | None) -> dict | None:
    """chat usage → Responses usage。"""
    if not u:
        return None
    details = u.get("completion_tokens_details") or {}
    return {
        "input_tokens": u.get("prompt_tokens"),
        "output_tokens": u.get("completion_tokens"),
        "output_tokens_details": {
            "reasoning_tokens": details.get("reasoning_tokens", 0),
        },
        "total_tokens": u.get("total_tokens"),
    }


def _sse(event: str, data: dict) -> bytes:
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8")


# --------------------------------------------------------------------------
# 输入：Responses input → chat messages
# --------------------------------------------------------------------------

def _convert_content(content):
    """Responses content（str 或 content parts 数组）→ chat content。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append({"type": "text", "text": part})
            elif isinstance(part, dict):
                pt = part.get("type")
                if pt == "input_text":
                    out.append({"type": "text", "text": part.get("text", "")})
                elif pt == "input_image":
                    url = part.get("image_url") or part.get("url") or ""
                    if isinstance(url, dict):
                        url = url.get("url", "")
                    out.append({"type": "image_url", "image_url": {"url": url}})
                elif pt == "input_file":
                    # chat 接口无法按 file_id 引用文件，退化为文本占位
                    out.append({"type": "text",
                                "text": "[file: %s]" % part.get("file_id", "")})
                elif pt in ("text", "image_url"):
                    out.append(part)   # 已经是 chat 格式
                else:
                    out.append({"type": "text", "text": part.get("text", "")})
            else:
                out.append({"type": "text", "text": str(part)})
        return out
    return str(content)


def _convert_input(input_field, instructions):
    """把 Responses input 转成 chat messages 列表。

    返回 (messages, error_or_None)。input 不可解析时 error 非空。
    """
    messages = []
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # 历史函数调用缓冲：call_id -> {name, args}
    pending_calls: dict[str, dict] = {}
    pending_order: list[str] = []
    pending_outputs: dict[str, str] = {}
    pending_reasoning: list[str] = []

    def flush_calls():
        if not pending_calls:
            return
        tool_calls = []
        for cid in pending_order:
            c = pending_calls[cid]
            tool_calls.append({
                "id": cid,
                "type": "function",
                "function": {"name": c["name"], "arguments": c["args"]},
            })
        assistant = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        if pending_reasoning:
            assistant["reasoning_content"] = "".join(pending_reasoning)
            pending_reasoning.clear()
        messages.append(assistant)
        for cid in pending_order:
            if cid in pending_outputs:
                messages.append({"role": "tool", "content": pending_outputs[cid],
                                 "tool_call_id": cid})
        pending_calls.clear()
        pending_order.clear()
        pending_outputs.clear()

    if isinstance(input_field, str):
        messages.append({"role": "user", "content": input_field})
        return messages, None
    if not isinstance(input_field, list):
        # 非预期类型：退化为 user 文本
        messages.append({"role": "user", "content": str(input_field)})
        return messages, None

    for item in input_field:
        if not isinstance(item, dict):
            flush_calls()
            messages.append({"role": "user", "content": str(item)})
            continue

        t = item.get("type", "message")

        if t == "message":
            flush_calls()
            role = item.get("role", "user")
            message = {"role": role, "content": _convert_content(item.get("content", ""))}
            if role == "assistant" and pending_reasoning:
                message["reasoning_content"] = "".join(pending_reasoning)
                pending_reasoning.clear()
            messages.append(message)
        elif t == "system":
            flush_calls()
            messages.append({"role": "system", "content": _convert_content(item.get("content", ""))})
        elif t == "function_call":
            # 历史助手工具调用
            cid = item.get("call_id") or _new_id("call_")
            if cid not in pending_calls:
                pending_order.append(cid)
            pending_calls[cid] = {
                "name": item.get("name", ""),
                "args": item.get("arguments", "") or "",
            }
        elif t == "function_call_output":
            cid = item.get("call_id") or ""
            pending_outputs[cid] = item.get("output", "")
            # 若没有对应的 function_call 项，单独作为 tool 消息兜底
            if cid not in pending_calls:
                messages.append({"role": "tool", "content": item.get("output", ""),
                                 "tool_call_id": cid})
        elif t == "reasoning":
            # 回传的独立 reasoning 项属于紧随其后的 assistant/function_call。
            parts = item.get("content") or item.get("summary") or []
            pending_reasoning.extend(p.get("text", "") for p in parts if isinstance(p, dict))
        elif t == "item_reference":
            flush_calls()
        else:
            # 未知类型：若带有 role+content 则当作 message
            if "role" in item and "content" in item:
                flush_calls()
                messages.append({"role": item["role"], "content": _convert_content(item["content"])})

    flush_calls()
    return messages, None


def _convert_tools(tools):
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and "function" not in tool:
            fn = {k: v for k, v in tool.items() if k != "type"}
            fn.setdefault("parameters", {})
            out.append({"type": "function", "function": fn})
        else:
            out.append(tool)
    return out


def _convert_tool_choice(tc):
    """先保持标准 Chat 语义，各渠道适配器再转换其上游要求的格式。"""
    if tc is None or isinstance(tc, str):
        return tc
    if isinstance(tc, dict):
        typ = tc.get("type")
        if typ == "function":
            name = tc.get("name") or (tc.get("function") or {}).get("name")
            if not name:
                raise ValueError("指定工具缺少 name")
            return {"type": "function", "function": {"name": name}}
        if typ in ("auto", "none", "required"):
            return typ
        return tc
    return None


# --------------------------------------------------------------------------
# 主入口：Responses body → chat/completions body
# --------------------------------------------------------------------------

def responses_to_chat(body: dict, default_model: str = "") -> dict:
    """把 Responses 请求体翻译为 chat/completions 请求体（stream 强制 True）。

    失败时抛 ValueError，msg 描述原因。
    """
    model = body.get("model") or default_model
    instructions = body.get("instructions")
    messages, err = _convert_input(body.get("input"), instructions)
    if err:
        raise ValueError(err)
    if not messages:
        raise ValueError("input 未包含任何有效消息")

    chat = {
        "model": model,
        "messages": messages,
        "stream": True,
    }

    tools = _convert_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
    tc = _convert_tool_choice(body.get("tool_choice"))
    if tc:
        chat["tool_choice"] = tc

    # text.format → response_format
    tfmt = (body.get("text") or {}).get("format")
    if tfmt:
        chat["response_format"] = tfmt

    # 其余透传参数（统一改名 max_output_tokens → max_tokens）
    passthru = ("temperature", "top_p", "max_output_tokens", "stop",
                "presence_penalty", "frequency_penalty", "seed", "logit_bias",
                "logprobs", "top_logprobs", "n", "user")
    for k in passthru:
        if k in body and body[k] is not None:
            ck = "max_tokens" if k == "max_output_tokens" else k
            chat[ck] = body[k]

    return chat


# --------------------------------------------------------------------------
# 输出：chat completion → Response 对象（非流式）
# --------------------------------------------------------------------------

def chat_to_response(chat: dict, response_id: str | None = None,
                     model: str | None = None, instructions=None) -> dict:
    """把一个聚合后的 chat.completion 重排为 Responses response 对象。"""
    model = model or chat.get("model")
    created = chat.get("created", int(time.time()))
    msg = (chat.get("choices") or [{}])[0].get("message", {})
    content_text = msg.get("content") or ""
    tool_calls = msg.get("tool_calls")
    reasoning = msg.get("reasoning_content")

    output = []
    msg_id = _new_id("msg_")
    msg_item = {
        "id": msg_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [],
        "created_at": created,
    }
    if content_text:
        msg_item["content"].append({
            "type": "output_text",
            "text": content_text,
            "annotations": [],
        })
    if reasoning:
        output.append({"id": _new_id("rs_"), "type": "reasoning", "status": "completed",
                       "summary": [], "content": [{"type": "reasoning_text", "text": reasoning}]})
    if content_text:
        output.append(msg_item)

    if tool_calls:
        for tc in tool_calls:
            fc_id = _new_id("fc_")
            fn = tc.get("function", {}) or {}
            output.append({
                "id": fc_id,
                "type": "function_call",
                "status": "completed",
                "call_id": tc.get("id", fc_id),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", ""),
                "created_at": created,
            })

    finish = (chat.get("choices") or [{}])[0].get("finish_reason")
    status, incomplete = _completion_status(finish)
    if status == "incomplete":
        for item in output:
            item["status"] = "incomplete"
    return {
        "id": response_id or _new_id("resp_"),
        "object": "response",
        "created_at": created,
        "status": status,
        "model": model,
        "output": output,
        "usage": _map_usage(chat.get("usage")),
        "error": None,
        "incomplete_details": incomplete,
        "instructions": instructions,
        "metadata": {},
        "temperature": None,
        "top_p": None,
        "max_output_tokens": None,
        "previous_response_id": None,
        "tools": [],
        "tool_choice": "auto",
        "truncation": "disabled",
    }


# --------------------------------------------------------------------------
# 输出：流式（上游 chat SSE → Responses SSE）
# --------------------------------------------------------------------------

def _chat_sse_lines(resp, model: str):
    """统一上游 SSE：Trae SOLO 先转成 OpenAI chat chunk，其它渠道原样读取。"""
    if getattr(resp, "_hy3_platform", "") == "trae":
        for chunk in proxy._trae_openai_sse_events(resp, model):
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            for line in str(chunk).splitlines():
                yield line
        return
    yield from proxy._sse_lines(resp)


def _completion_status(finish):
    if finish in ("length", "content_filter"):
        return "incomplete", {"reason": "max_output_tokens" if finish == "length" else finish}
    return "completed", None


def stream_chat_to_responses(resp, model: str, response_id: str | None = None,
                             instructions=None):
    """Chat SSE → Responses：独立文本/推理/工具项，共享递增 output_index。"""
    response_id = response_id or _new_id("resp_")
    created_at = int(time.time())
    output = []
    tools = {}
    text_index = reasoning_index = None
    usage = None
    finish = None
    saw_choice = False
    sequence = 0

    def emit(event, data):
        nonlocal sequence
        payload = {"type": event, "sequence_number": sequence, **data}
        sequence += 1
        return _sse(event, payload)

    def response(status="in_progress", incomplete=None):
        return {"id": response_id, "object": "response", "created_at": created_at,
                "status": status, "model": model, "output": output,
                "usage": _map_usage(usage), "instructions": instructions,
                "error": None, "incomplete_details": incomplete}

    def add_tool(t):
        if not t["added"]:
            t["item"]["call_id"] = t["item"]["call_id"] or _new_id("call_")
            t["added"] = True
            yield emit("response.output_item.added", {
                "output_index": t["index"], "item": {**t["item"], "arguments": ""}})
            if t["item"]["arguments"]:
                yield emit("response.function_call_arguments.delta", {
                    "output_index": t["index"], "item_id": t["item"]["id"],
                    "delta": t["item"]["arguments"]})

    yield emit("response.created", {"response": response()})
    yield emit("response.in_progress", {"response": response()})
    for line in _chat_sse_lines(resp, model):
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except (ValueError, TypeError):
            continue
        err = proxy._sse_business_error(chunk)
        if err:
            raise proxy.ProxyError("上游业务错误: %s" % err, status=502)
        if not isinstance(chunk, dict):
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        if not chunk.get("choices"):
            continue
        saw_choice = True
        choice = chunk["choices"][0]
        delta = choice.get("delta") or choice.get("message") or {}
        finish = choice.get("finish_reason") or finish

        reasoning = delta.get("reasoning_content")
        if reasoning:
            if reasoning_index is None:
                reasoning_index = len(output)
                item = {"id": _new_id("rs_"), "type": "reasoning", "status": "in_progress",
                        "summary": [], "content": [{"type": "reasoning_text", "text": ""}]}
                output.append(item)
                yield emit("response.output_item.added", {"output_index": reasoning_index, "item": item})
            item = output[reasoning_index]
            item["content"][0]["text"] += reasoning
            yield emit("response.reasoning_text.delta", {
                "output_index": reasoning_index, "item_id": item["id"], "content_index": 0, "delta": reasoning})

        content = delta.get("content")
        if content:
            if text_index is None:
                text_index = len(output)
                item = {"id": _new_id("msg_"), "type": "message", "status": "in_progress",
                        "role": "assistant", "content": []}
                output.append(item)
                yield emit("response.output_item.added", {"output_index": text_index, "item": item})
                part = {"type": "output_text", "text": "", "annotations": []}
                item["content"].append(part)
                yield emit("response.content_part.added", {
                    "output_index": text_index, "item_id": item["id"], "content_index": 0, "part": part})
            item = output[text_index]
            item["content"][0]["text"] += content
            yield emit("response.output_text.delta", {
                "output_index": text_index, "item_id": item["id"], "content_index": 0,
                "delta": content, "logprobs": []})

        for position, tc in enumerate(delta.get("tool_calls") or []):
            index = tc.get("index", position)
            if index not in tools:
                item = {"id": _new_id("fc_"), "type": "function_call", "status": "in_progress",
                        "call_id": "", "name": "", "arguments": ""}
                tools[index] = {"item": item, "index": len(output), "added": False}
                output.append(item)
            t = tools[index]
            item = t["item"]
            if tc.get("id"):
                item["call_id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                item["name"] += fn["name"]
            fragment = fn.get("arguments") or ""
            item["arguments"] += fragment
            if t["added"] and fragment:
                yield emit("response.function_call_arguments.delta", {
                    "output_index": t["index"], "item_id": item["id"], "delta": fragment})
            elif not t["added"] and fragment and item["call_id"] and item["name"]:
                yield from add_tool(t)

    if not saw_choice:
        raise proxy.ProxyError("上游返回空响应流 (empty SSE)", status=502)
    for t in tools.values():
        yield from add_tool(t)
    status, incomplete = _completion_status(finish)
    for index, item in enumerate(output):
        item["status"] = "incomplete" if status == "incomplete" else "completed"
        common = {"output_index": index, "item_id": item["id"]}
        if item["type"] == "message":
            part = item["content"][0]
            yield emit("response.output_text.done", {**common, "content_index": 0, "text": part["text"], "logprobs": []})
            yield emit("response.content_part.done", {**common, "content_index": 0, "part": part})
        elif item["type"] == "reasoning":
            yield emit("response.reasoning_text.done", {**common, "content_index": 0, "text": item["content"][0]["text"]})
        else:
            yield emit("response.function_call_arguments.done", {**common, "arguments": item["arguments"]})
        yield emit("response.output_item.done", {"output_index": index, "item": item})
    # app.py 用原始 usage 记录真实 credit，Responses usage 只做协议字段映射。
    resp._hy3_usage = usage
    yield emit("response." + status, {"response": response(status, incomplete)})
