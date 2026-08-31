"""TraeWork 签到 seam：X-Device-Id 用 uid + 请求头契约。"""
import json
from unittest.mock import MagicMock, patch

import pytest

import trae


AHA_DID = "7382917465019283"


def test_checkin_with_aha_device_id_claims_success():
    """有效 16 位 Aha 设备号走真实 claim 并返回领取结果。"""
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"code": 0, "message": "success", "credits": 200}
    with patch.object(trae, "_session") as sess:
        sess.return_value.post.return_value = fake
        res = trae.do_checkin("token", device_id=AHA_DID, attempts=1)
    assert res["ok"] is True
    assert res["claimed_credit"] == 200
    # 请求头必须带 Aha 设备号
    headers = sess.return_value.post.call_args.kwargs["headers"]
    assert headers["X-Device-Id"] == AHA_DID


def test_checkin_9074_reports_device_header_issue():
    """9074 的真实含义是 X-Device-Id 不合法，文案「参与用户太多」是误导。"""
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"code": 9074, "message": "当前参与用户太多，请稍后再试"}
    with patch.object(trae, "_session") as sess:
        sess.return_value.post.return_value = fake
        with pytest.raises(RuntimeError, match="9074"):
            trae.do_checkin("token", device_id=AHA_DID, attempts=1)


def test_ug_headers_match_official_client_contract():
    """签到请求头对齐官方客户端：浏览器 UA + Windows 设备类型 + Aha 设备号。"""
    h = trae.ug_headers("token", AHA_DID)
    assert h["User-Agent"].startswith("Mozilla/5.0")
    assert h["X-Device-Type"] == "Windows"
    assert h["X-Device-Id"] == AHA_DID
    assert h["Authorization"] == "Cloud-IDE-JWT token"


def test_checkin_rejects_invalid_attempt_count_without_request():
    """attempts<=0 时不该先打一次上游再报「重试后仍失败」。"""
    with patch.object(trae, "_session") as sess:
        with pytest.raises(RuntimeError, match="attempts"):
            trae.do_checkin("token", device_id=AHA_DID, attempts=0)
    sess.assert_not_called()
