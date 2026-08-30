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
