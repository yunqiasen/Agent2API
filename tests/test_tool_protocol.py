"""通过完整 SSE/HTTP 边界验证工具分片、指定工具与多轮回传。"""
import copy
import json
import time

import pytest
import providers as P
import proxy
import responses as R
from test_credentials_refresh import client, jwt


class Stream:
    status_code = 200
    encoding = "utf-8"
    _hy3_platform = "workbuddy_intl"

    def __init__(self, frames):
        self.raw = "".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n"

    def iter_lines(self, decode_unicode=False, **kwargs):
        for line in self.raw.splitlines():
            yield line if decode_unicode else line.encode()

    def iter_content(self, **kwargs):
        yield self.raw.encode()

    def close(self):
        pass


def chunk(delta, finish=None):
    return {"id": "chat-tools", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def frames():
    return [
        chunk({"role": "assistant", "reasoning_content": "tool reasoning"}),
        chunk({"tool_calls": [{"index": 0, "id": "call_weather", "type": "function",
                               "function": {"name": "weather", "arguments": '{"city":'}}]}),
        chunk({"tool_calls": [{"index": 1, "id": "call_clock", "type": "function",
                               "function": {"name": "clock", "arguments": "{"}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Shanghai"}'}},
                               {"index": 1, "function": {"arguments": "}"}}]}),
        {**chunk({}, "tool_calls"), "usage": {"prompt_tokens": 12, "completion_tokens": 8,
                                                "total_tokens": 20, "credit": .02}},
    ]


def events(raw):
    return [json.loads(line[5:]) for line in raw.splitlines() if line.startswith("data: {")]


def test_nonstream_aggregates_interleaved_tool_arguments():
    out = proxy.aggregate_stream(Stream(frames()), "glm-5.3")
    msg = out["choices"][0]["message"]
    assert msg.get("tool_calls") == [
        {"id": "call_weather", "type": "function", "function": {"name": "weather", "arguments": '{"city":"Shanghai"}'}},
        {"id": "call_clock", "type": "function", "function": {"name": "clock", "arguments": '{}'}},
    ]
    assert msg["reasoning_content"] == "tool reasoning"
    assert msg["content"] is None
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"]["credit"] == .02


def test_tool_names_can_arrive_in_fragments():
    out = proxy.aggregate_stream(Stream([
        chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "wea", "arguments": ""}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"name": "ther", "arguments": "{}"}}]}, "tool_calls"),
    ]), "m")
    assert out["choices"][0]["message"].get("tool_calls", [{}])[0].get("function") == {"name": "weather", "arguments": "{}"}


def test_aggregate_keeps_choices_separate():
    data = {"choices": [{"index": i, "delta": {"content": str(i)}, "finish_reason": "stop"} for i in (0, 1)]}
    out = proxy.aggregate_stream(Stream([data]), "m")
    assert [c["message"]["content"] for c in out["choices"]] == ["0", "1"]


@pytest.mark.parametrize("converter", [proxy.aggregate_stream, lambda s, m: list(R.stream_chat_to_responses(s, m))])
def test_empty_stream_is_not_a_success(converter):
    with pytest.raises(proxy.ProxyError, match="空|empty"):
        converter(Stream([]), "m")


def test_responses_preserves_named_choice_and_function_strict():
    out = R.responses_to_chat({"model": "m", "input": "hi", "tool_choice": {"type": "function", "name": "weather"},
        "tools": [{"type": "function", "name": "weather", "strict": True, "parameters": {"type": "object"}}]})
    assert out["tool_choice"] == {"type": "function", "function": {"name": "weather"}}
    assert out["tools"][0]["function"]["strict"] is True


@pytest.mark.parametrize("platform", ["workbuddy", "codebuddy", "workbuddy_intl"])
def test_tencent_profile_converts_named_choice_without_losing_name(platform):
    body = {"messages": [{"role": "user", "content": "keep exact wording"}],
            "tools": [{"type": "function", "function": {"name": "weather"}},
                      {"type": "function", "function": {"name": "clock"}}],
            "tool_choice": {"type": "function", "function": {"name": "weather"}}}
    saved = copy.deepcopy(body)
    out = P.normalize_body(platform, body)
    assert out["tool_choice"] == "weather"
    assert body == saved


@pytest.mark.parametrize("choice", ["none", {"type": "none"}])
def test_intl_none_disables_new_tools(choice):
    out = P.normalize_body("workbuddy_intl", {"tools": [{"type": "function", "name": "weather"}],
                           "tool_choice": choice, "messages": []})
    assert "tools" not in out
    assert "tool_choice" not in out


def test_intl_flat_tools_and_token_limit_alias():
    body = {"messages": [], "tools": [{"type": "function", "name": "weather", "strict": True,
                                     "parameters": {"type": "object"}}], "max_completion_tokens": 32}
    out = P.normalize_body("workbuddy_intl", body)
    assert out["tools"][0].get("function", {}).get("name") == "weather"
    assert out["tools"][0]["function"]["strict"] is True
    assert out.get("max_tokens") == 32 and "max_completion_tokens" not in out
    assert P.normalize_body("workbuddy_intl", {**body, "max_tokens": 16})["max_tokens"] == 16


def test_deepseek_history_backfills_missing_reasoning_without_mutating_input():
    body = {"model": "deepseek-v4-pro", "messages": [
        {"role": "assistant", "content": "first", "reasoning": "preserved trace"},
        {"role": "user", "content": "then?"}, {"role": "assistant", "content": "second"}]}
    saved = copy.deepcopy(body)
    out = P.normalize_body("workbuddy_intl", body)
    assistants = [m for m in out["messages"] if m["role"] == "assistant"]
    assert [m.get("reasoning_content") for m in assistants] == ["preserved trace", ""]
    assert body == saved


def test_trae_failover_does_not_mutate_body_for_next_channel():
    body = {"model": "m", "messages": [{"role": "user", "content": "hello"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function",
            "function": {"name": "weather", "arguments": "{}"}}]}]}
    saved = copy.deepcopy(body)
    P.normalize_body("trae", body)
    assert body == saved
    out = P.normalize_body("workbuddy_intl", body)
    assert out["messages"][-1]["tool_calls"][0]["function"]["name"] == "weather"


def test_responses_reasoning_is_a_separate_item_and_replays_into_tool_history():
    output = R.chat_to_response(proxy.aggregate_stream(Stream(frames()), "m"))["output"]
    assert output[0]["type"] == "reasoning"
    replay = R.responses_to_chat({"input": output + [
        {"type": "function_call_output", "call_id": "call_weather", "output": "20C"},
        {"type": "function_call_output", "call_id": "call_clock", "output": "12:00"},
    ]})
    assistant = next(m for m in replay["messages"] if m["role"] == "assistant")
    assert assistant.get("reasoning_content") == "tool reasoning"
    assert len(assistant["tool_calls"]) == 2
    assert [m["content"] for m in replay["messages"] if m["role"] == "tool"] == ["20C", "12:00"]


def test_responses_does_not_invent_missing_tool_outputs():
    out = R.responses_to_chat({"input": [{"type": "function_call", "call_id": "c", "name": "weather", "arguments": "{}"}]})
    assert [m["role"] for m in out["messages"]] == ["assistant"]


def test_stream_responses_tool_before_text_has_unique_indices_and_standard_envelope():
    raw = b"".join(R.stream_chat_to_responses(Stream([
        frames()[1], chunk({"content": "Checking"}), *frames()[2:],
    ]), "m")).decode()
    data = events(raw)
    assert data[0].get("type") == "response.created"
    assert data[0]["response"]["object"] == "response"
    added = [d for d in data if d.get("type") == "response.output_item.added"]
    assert len({d["output_index"] for d in added}) == len(added) == 3
    final = data[-1]["response"]["output"]
    assert [x["type"] for x in final] == ["function_call", "message", "function_call"]
    assert final[0]["call_id"] == "call_weather"
    assert final[0]["arguments"] == '{"city":"Shanghai"}'
    assert [d["sequence_number"] for d in data] == list(range(len(data)))


def test_stream_reasoning_never_becomes_visible_answer():
    data = events(b"".join(R.stream_chat_to_responses(Stream([
        chunk({"reasoning_content": "private trace"}), chunk({"content": "answer"}, "stop"),
    ]), "m")).decode())
    text_deltas = [x["delta"] for x in data if x.get("type") == "response.output_text.delta"]
    assert text_deltas == ["answer"]
    assert [x["type"] for x in data[-1]["response"]["output"]] == ["reasoning", "message"]


@pytest.mark.parametrize("stream", [False, True])
def test_responses_length_is_incomplete(stream):
    source = Stream([chunk({"content": "partial"}, "length")])
    if stream:
        data = events(b"".join(R.stream_chat_to_responses(source, "m")).decode())
        assert data[-1].get("type") == "response.incomplete"
        out = data[-1]["response"]
    else:
        out = R.chat_to_response(proxy.aggregate_stream(source, "m"))
    assert out["status"] == "incomplete"
    assert out["incomplete_details"]["reason"] == "max_output_tokens"
    assert all(item["status"] == "incomplete" for item in out["output"])


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_full_http_tool_roundtrip_and_usage(client, isolated_db, monkeypatch, path, stream):
    import app
    db = isolated_db
    db.set_model_registry({"glm-5.3": {"type": "chat", "platforms": ["workbuddy_intl"]}})
    db.add_api_key("tool-test", "tool-key", "workbuddy_intl")
    aid = db.add_account("workbuddy_intl", jwt(int(time.time()) + 7200), uid="u")
    opened = []
    def open_upstream(acc, body):
        normalized = P.normalize_body(acc["platform"], body)
        opened.append(normalized)
        return Stream(frames() if len(opened) == 1 else [
            {**chunk({"content": "20C at 12:00"}, "stop"), "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12, "credit": .01}}])
    monkeypatch.setattr(proxy, "open_upstream", open_upstream)
    is_responses = path.endswith("responses")
    functions = [{"type": "function", "name": name, "parameters": {"type": "object"}} for name in ("weather", "clock")]
    body = {"model": "glm-5.3", "stream": stream, "tool_choice": {"type": "function", "name": "weather"},
            "tools": functions if is_responses else [{"type": "function", "function": {k: v for k, v in f.items() if k != "type"}} for f in functions]}
    body.update({"input": "weather?"} if is_responses else {"messages": [{"role": "user", "content": "weather?"}]})
    response = client.post(path, json=body, headers={"Authorization": "Bearer tool-key"})
    assert response.status_code == 200
    raw = response.get_data(as_text=True)
    assert opened[0]["tool_choice"] == "weather"
    if is_responses:
        out = events(raw)[-1]["response"] if stream else response.get_json()
        calls = [x for x in out["output"] if x["type"] == "function_call"]
        assert len(calls) == 2 and calls[0]["arguments"] == '{"city":"Shanghai"}'
        body["input"] = [{"role": "user", "content": "weather?"}] + out["output"] + [
            {"type": "function_call_output", "call_id": "call_weather", "output": "20C"},
            {"type": "function_call_output", "call_id": "call_clock", "output": "12:00"}]
    else:
        out = proxy.aggregate_stream(Stream([json.loads(l[5:]) for l in raw.splitlines() if l.startswith("data: {")]), "m") if stream else response.get_json()
        msg = out["choices"][0]["message"]
        assert len(msg.get("tool_calls", [])) == 2
        body["messages"] += [msg, {"role": "tool", "tool_call_id": "call_weather", "content": "20C"},
                             {"role": "tool", "tool_call_id": "call_clock", "content": "12:00"}]
    body["tool_choice"] = "auto"
    response = client.post(path, json=body, headers={"Authorization": "Bearer tool-key"})
    assert response.status_code == 200 and "20C at 12:00" in response.get_data(as_text=True)
    assert [m["content"] for m in opened[1]["messages"] if m["role"] == "tool"] == ["20C", "12:00"]
    app._flush_stats()
    assert db.get_account(aid)["req_count"] == 2
    assert sum(l["credit_cost"] for l in db.list_logs()) == pytest.approx(.03)
