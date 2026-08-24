# -*- coding: utf-8 -*-
"""
hy3api - OpenAI Responses API 翻译层

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
        messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
        for cid in pending_order:
            messages.append({
                "role": "tool",
                "content": pending_outputs.get(cid, ""),
                "tool_call_id": cid,
            })
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
            messages.append({"role": "user", "content": str(item)})
            flush_calls()
            continue

        t = item.get("type", "message")

        if t == "message":
            flush_calls()
            role = item.get("role", "user")
            messages.append({"role": role, "content": _convert_content(item.get("content", ""))})
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
        elif t in ("reasoning", "item_reference"):
            # 推理项/引用项：无对应 chat 语义，跳过
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
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            out.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters") or {},
                },
            })
        else:
            # file_search / web_search / code_interpreter 等上游不支持，
            # 原样透传（失败由上游报错，本层不阻断）
            out.append(t)
    return out


def _convert_tool_choice(tc):
    """Responses tool_choice → chat tool_choice。

    上游 chat 接口只接受字符串形式的 tool_choice（"auto"/"none"/"required"），
    不支持 OpenAI 的 {"type":"function","function":{...}} 对象形式（会 11101）。
    因此把对象形式做有损降级：
      - {type:"function", name:X} → "required"（强制调用某个函数；
        当仅一个工具时等价于指定该函数；多工具时为尽力而为）
      - {type:"auto"} / {type:"none"} → 同名字串
    """
    if tc is None:
        return None
    if isinstance(tc, str):
        return tc                      # auto / none / required 两 API 一致
    if isinstance(tc, dict):
        t = tc.get("type")
        if t == "function":
            return "required"
        if t in ("auto", "none"):
            return t
        return "auto"
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
        msg_item["content"].append({
            "type": "reasoning",
            "summary": [],
            "text": reasoning,
        })
    if content_text or reasoning:
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

    return {
        "id": response_id or _new_id("resp_"),
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": model,
        "output": output,
        "usage": _map_usage(chat.get("usage")),
        "error": None,
        "incomplete_details": None,
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

def stream_chat_to_responses(resp, model: str, response_id: str | None = None,
                             instructions=None):
    """生成器：读取上游 chat 流式响应，逐块 yield Responses SSE 字节。"""
    response_id = response_id or _new_id("resp_")
    created_at = int(time.time())
    msg_id = _new_id("msg_")
    part_id = _new_id("cp_")

    # 状态
    item_added = False        # 助手文本消息项是否已 emit added
    part_added = False        # 文本 content part 是否已 emit added
    text_parts: list[str] = []
    text_done = False
    # 工具调用：index -> {id, name, args, added}
    tools: dict[int, dict] = {}
    next_index = 0

    def base_resp(status: str) -> dict:
        return {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": status,
            "model": model,
            "output": [],
            "usage": None,
        }

    def ensure_msg_item():
        nonlocal item_added, part_added, next_index
        if not item_added:
            yield _sse("response.output_item.added", {
                "output_index": 0,
                "item": {
                    "id": msg_id, "type": "message", "status": "in_progress",
                    "role": "assistant", "content": [], "created_at": created_at,
                },
            })
            item_added = True
            next_index = 1          # 文本消息占 index 0，工具调用从 1 起
        if not part_added:
            yield _sse("response.content_part.added", {
                "item_id": msg_id, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
            part_added = True

    yield _sse("response.created", base_resp("in_progress"))
    yield _sse("response.in_progress", {})

    usage = None
    finish_reason = None
    saw_content = False
    saw_tool = False

    for line in proxy._sse_lines(resp):
        if not line or not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            j = json.loads(chunk)
        except Exception:
            continue

        if j.get("usage"):
            usage = j["usage"]
        if not j.get("choices"):
            continue
        ch = j["choices"][0]
        delta = ch.get("delta") or {}
        finish_reason = ch.get("finish_reason") or finish_reason

        # ---- 文本内容 ----
        content = delta.get("content")
        if content:
            saw_content = True
            yield from ensure_msg_item()
            text_parts.append(content)
            yield _sse("response.output_text.delta", {
                "item_id": msg_id, "content_index": 0,
                "delta": content, "logprobs": None,
            })

        # 推理内容（如上游返回 reasoning_content），按文本增量处理
        reasoning = delta.get("reasoning_content")
        if reasoning:
            saw_content = True
            yield from ensure_msg_item()
            text_parts.append(reasoning)
            yield _sse("response.output_text.delta", {
                "item_id": msg_id, "content_index": 0,
                "delta": reasoning, "logprobs": None,
            })

        # ---- 工具调用 ----
        for tc in (delta.get("tool_calls") or []):
            saw_tool = True
            idx = tc.get("index", 0)
            t = tools.get(idx)
            if t is None:
                t = {
                    "id": tc.get("id") or _new_id("call_"),
                    "name": "",
                    "args": "",
                    "added": False,
                }
                tools[idx] = t
            if tc.get("id") and not t["id"].startswith("call_"):
                t["id"] = tc.get("id")
            fn = tc.get("function") or {}
            if fn.get("name"):
                t["name"] = fn["name"]
            arg_frag = fn.get("arguments", "")
            if arg_frag:
                if not t["added"]:
                    t["out_index"] = next_index
                    yield _sse("response.output_item.added", {
                        "output_index": next_index,
                        "item": {
                            "id": t["id"], "type": "function_call",
                            "status": "in_progress", "call_id": t["id"],
                            "name": t["name"], "arguments": "",
                            "created_at": created_at,
                        },
                    })
                    t["added"] = True
                    next_index += 1
                t["args"] += arg_frag
                yield _sse("response.function_call_arguments.delta", {
                    "item_id": t["id"], "delta": arg_frag,
                })

    # ---- 收尾：文本 ----
    if saw_content and not text_done:
        full = "".join(text_parts)
        if part_added:
            yield _sse("response.output_text.done", {
                "item_id": msg_id, "content_index": 0,
                "text": full, "logprobs": None,
            })
            yield _sse("response.content_part.done", {
                "item_id": msg_id, "content_index": 0,
                "part": {"type": "output_text", "text": full, "annotations": []},
            })
        if item_added:
            yield _sse("response.output_item.done", {
                "output_index": 0,
                "item": {
                    "id": msg_id, "type": "message", "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": full, "annotations": []}],
                    "created_at": created_at,
                },
            })

    # ---- 收尾：工具调用 ----
    final: dict = {}
    if saw_content:
        final[0] = {
            "id": msg_id, "type": "message", "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "".join(text_parts), "annotations": []}],
            "created_at": created_at,
        }
    for idx in sorted(tools.keys()):
        t = tools[idx]
        if not t["added"]:
            # 没有参数片段但也算一次调用
            t["out_index"] = next_index
            yield _sse("response.output_item.added", {
                "output_index": next_index,
                "item": {
                    "id": t["id"], "type": "function_call", "status": "in_progress",
                    "call_id": t["id"], "name": t["name"], "arguments": "",
                    "created_at": created_at,
                },
            })
            next_index += 1
        yield _sse("response.function_call_arguments.done", {
            "item_id": t["id"], "arguments": t["args"],
        })
        oi = t.get("out_index", idx)
        yield _sse("response.output_item.done", {
            "output_index": oi,
            "item": {
                "id": t["id"], "type": "function_call", "status": "completed",
                "call_id": t["id"], "name": t["name"], "arguments": t["args"],
                "created_at": created_at,
            },
        })
        final[oi] = {
            "id": t["id"], "type": "function_call", "status": "completed",
            "call_id": t["id"], "name": t["name"], "arguments": t["args"],
            "created_at": created_at,
        }

    out_items = [final[k] for k in sorted(final.keys())]

    status = "incomplete" if finish_reason in ("length", "content_filter") else "completed"
    completed = base_resp(status)
    completed["output"] = out_items
    completed["usage"] = _map_usage(usage)
    completed["instructions"] = instructions
    # 供 app.py 流式请求结束后记录上游真实 tokens / credit；不改变对外响应。
    try:
        resp._hy3_usage = usage
    except Exception:
        pass
    yield _sse("response.completed", completed)
