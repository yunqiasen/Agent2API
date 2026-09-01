"""测活 seam + Trae 登录轮询 seam。"""
from unittest.mock import MagicMock, patch

import pytest

import providers as P
import proxy


def _acc(platform="trae"):
    return {"id": 1, "platform": platform, "access_token": "tok",
            "uid": "u", "machine_id": "m", "device_id": "d"}


def test_poll_token_trae_never_hits_wrong_endpoint():
    """Trae 没有 token 轮询端点：直接返回 None 表示等待回调导入。"""
    with patch.object(P, "_session") as sess:
        assert P.poll_token("trae", "state-x") is None
    sess.assert_not_called()


def test_probe_account_success_reports_latency_and_reply(monkeypatch):
    """成功：绿灯数据（ok/ms/reply）齐全。"""
    fake = MagicMock()
    fake.status_code = 200
    fake.close = lambda: None
    agg = {"choices": [{"message": {"content": "pong"}}]}
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(row))
    monkeypatch.setattr(proxy, "open_upstream", lambda acc, body, timeout=None: fake)
    monkeypatch.setattr(proxy, "aggregate_trae_stream", lambda r, m: agg)
    res = proxy.probe_account(_acc(), "glm-5-turbo")
    assert res["ok"] is True
    assert res["model"] == "glm-5-turbo"
    assert res["reply"] == "pong"
    assert res["ms"] >= 0


def test_probe_account_http_error_shows_upstream_reason(monkeypatch):
    """上游 400：返回完整错误文本，前端爆红展示。"""
    fake = MagicMock()
    fake.status_code = 400
    fake.text = '{"code":11102,"msg":"model not found"}'
    fake.close = lambda: None
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(row))
    monkeypatch.setattr(proxy, "open_upstream", lambda acc, body, timeout=None: fake)
    res = proxy.probe_account(_acc(), "bad-model")
    assert res["ok"] is False
    assert "11102" in res["error"]
    assert "model not found" in res["error"]


def test_probe_account_connection_failure_is_survivable(monkeypatch):
    """连接炸了返回结构化失败，不抛异常拖死面板。"""
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(row))
    def boom(acc, body, timeout=None):
        raise OSError("connection reset")
    monkeypatch.setattr(proxy, "open_upstream", boom)
    res = proxy.probe_account(_acc(), "glm-5-turbo")
    assert res["ok"] is False
    assert "connection reset" in res["error"]


def test_probe_account_workbuddy_uses_standard_aggregator(monkeypatch):
    """非 Trae 平台走 aggregate_stream，不会误用 Trae 聚合器。"""
    fake = MagicMock()
    fake.status_code = 200
    fake.close = lambda: None
    agg = {"choices": [{"message": {"content": "hi"}}]}
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(row))
    monkeypatch.setattr(proxy, "open_upstream", lambda acc, body, timeout=None: fake)
    monkeypatch.setattr(proxy, "aggregate_stream", lambda r, m: agg)
    with patch.object(proxy, "aggregate_trae_stream") as trae_agg:
        res = proxy.probe_account(_acc("workbuddy"), "glm-5.3")
    trae_agg.assert_not_called()
    assert res["ok"] is True and res["reply"] == "hi"


def test_fetch_upstream_models_falls_back_after_console_retry_error(monkeypatch):
    """国际站 console/models 被重试器耗尽时，仍必须回落到 /v3/config。"""
    import requests
    import providers as P

    class Resp:
        status_code = 200

        def json(self):
            return {"code": 0, "data": {"agents": [{"name": "cli", "models": ["glm-5.3"]}]}}

    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/console/enterprises/personal/models"):
                raise requests.exceptions.RetryError("500 retries exhausted")
            return Resp()

    monkeypatch.setattr(P, "_session", lambda: Session())
    assert P.fetch_upstream_models("codebuddy", "tok") == ["glm-5.3"]


def test_poll_token_makes_one_request_per_poll(monkeypatch):
    """登录轮询一次只发一个请求，避免重复消耗上游轮询配额。"""
    import providers as P

    class Resp:
        def json(self):
            return {"code": 11217, "msg": "login ing"}

    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            return Resp()

    sess = Session()
    monkeypatch.setattr(P, "_session", lambda: sess)
    assert P.poll_token("workbuddy", "state-x") is None
    assert sess.calls == 1


def test_probe_account_rejects_http_200_business_error_sse(monkeypatch):
    """上游 HTTP 200 但 SSE 首帧是业务错误时，测活必须亮红而不是绿灯。"""
    class Resp:
        status_code = 200
        headers = {}

        def iter_lines(self, decode_unicode=False):
            return iter([
                b'data: {"code":9074,"message":"business failed"}',
                b"",
                b"data: [DONE]",
            ])

        def close(self):
            pass

    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(row))
    monkeypatch.setattr(proxy, "open_upstream", lambda acc, body, timeout=None: Resp())
    res = proxy.probe_account(_acc("workbuddy"), "glm-5.3")
    assert res["ok"] is False
    assert "9074" in res["error"]


def test_aggregate_stream_rejects_http_200_business_error_sse():
    """非流式聚合不能把没有 choices 的错误帧包装成空成功响应。"""
    class Resp:
        def iter_lines(self, decode_unicode=False):
            return iter([
                b'data: {"code":11102,"msg":"model not found"}',
                b"",
                b"data: [DONE]",
            ])

    with pytest.raises(proxy.ProxyError, match="11102"):
        proxy.aggregate_stream(Resp(), "missing-model")


def test_chat_rejects_non_list_messages_without_500(monkeypatch, isolated_db):
    """OpenAI 请求体结构错误应在入口返回 400，而不是进入 prompt 统计触发 500。"""
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    app_mod.app.secret_key = "test-secret"
    key = isolated_db.add_api_key("test", "test-key")
    with app_mod.app.test_client() as c:
        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer test-key"},
                   json={"model": "hy3", "messages": {"role": "user", "content": "x"}})
    assert r.status_code == 400
    assert "messages" in r.get_json()["error"]["message"]


def test_sse_usage_tracker_records_business_error():
    tracker = proxy.SSEUsageTracker()
    tracker.feed(b'data: {"code":11102,"msg":"model not found"}\n')
    tracker.finish()
    assert "11102" in tracker.error


def test_responses_stream_rejects_http_200_business_error():
    import responses as response_adapter

    class Resp:
        def iter_lines(self, decode_unicode=False):
            return iter([
                b'data: {"code":11102,"msg":"model not found"}',
                b"",
                b"data: [DONE]",
            ])

    with pytest.raises(proxy.ProxyError, match="11102"):
        list(response_adapter.stream_chat_to_responses(Resp(), "missing-model"))


def test_poll_token_accepts_string_success_code(monkeypatch):
    """上游 code 即使是字符串 0，也应按成功解析 token。"""
    import providers as P

    class Resp:
        def json(self):
            return {"code": "0", "data": {"accessToken": "tok"}}

    class Session:
        def get(self, url, **kwargs):
            return Resp()

    monkeypatch.setattr(P, "_session", lambda: Session())
    out = P.poll_token("workbuddy", "state-x")
    assert out["access_token"] == "tok"


def test_stats_jobs_are_serialized_by_one_worker(monkeypatch):
    """统计写不能每条请求起一个并发线程，必须按队列串行落库。"""
    import threading
    import time
    import app as app_mod

    active = 0
    max_active = 0
    lock = threading.Lock()

    def job():
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1

    for _ in range(8):
        app_mod._enqueue_stats(job)
    assert app_mod._flush_stats(2.0)
    assert max_active == 1


def test_streaming_responses_translates_trae_solo_events():
    """Trae 的 event: output SOLO 流必须转成 Responses 文本增量，不能返回空 output。"""
    import responses as response_adapter

    class Resp:
        _hy3_platform = "trae"

        def iter_lines(self, decode_unicode=False):
            lines = [
                b"event: output",
                b'data: {"response":"hello from trae"}',
                b"",
                b"event: token_usage",
                b'data: {"prompt_tokens":2,"completion_tokens":3,"total_tokens":5,"credit":1}',
                b"",
                b"event: done",
                b'data: {"finish_reason":"stop"}',
                b"",
            ]
            yield from lines

    resp = Resp()
    chunks = list(response_adapter.stream_chat_to_responses(resp, "trae-model", "resp-test"))
    text = b"".join(chunks).decode("utf-8")
    assert "hello from trae" in text
    assert '"text": "hello from trae"' in text
    assert '"total_tokens": 5' in text
    assert resp._hy3_usage["total_tokens"] == 5


def test_trae_stream_does_not_emit_normal_done_after_late_error():
    """Trae 已输出部分内容后再报错时，不能继续伪造 stop + [DONE] 成功收尾。"""
    class Resp:
        def iter_lines(self, decode_unicode=False):
            yield from [
                b"event: output", b'data: {"response":"partial"}', b"",
                b"event: error", b'data: {"code":9001,"message":"late failure"}', b"",
            ]

    text = b"".join(proxy._trae_openai_sse_events(Resp(), "trae-model")).decode("utf-8")
    assert "late failure" in text
    assert '"finish_reason": "stop"' not in text
    assert "[DONE]" not in text
