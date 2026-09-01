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


def test_trae_quota_uses_usage_summary_and_preserves_packages():
    """Trae 额度必须按上游 usage_summary 计算，不能硬写 total=remain/used=0。"""
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {
        "usage_summary": {
            "total_amount": 5100,
            "consumed_amount": 125.5,
            "consumption_ratio": 0.0246,
        },
        "user_entitlement_pack_list": [
            {
                "display_desc": "签到奖励",
                "group_name": "每日签到",
                "expire_time": 1790904604,
                "status": 1,
                "entitlement_base_info": {
                    "quota": {"credits_limit": 200},
                    "entitlement_id": "checkin_x",
                },
            },
            {
                "display_desc": "每月登录赠送",
                "group_name": "每月登录积分",
                "expire_time": 1790783999,
                "status": 1,
                "entitlement_base_info": {
                    "quota": {"credits_limit": 500},
                    "entitlement_id": "monthly_x",
                },
            },
        ],
    }
    with patch.object(trae, "_session") as sess:
        sess.return_value.post.return_value = fake
        q = trae.fetch_quota("token", device_id="uid")
    assert q["total"] == 5100
    assert q["used"] == 125.5
    assert q["remain"] == 4974.5
    assert len(q["packages"]) == 2
    assert q["packages"][0]["name"] == "签到奖励"
    assert q["packages"][0]["credits"] == 200


def test_trae_checkin_streak_is_unknown_when_upstream_omits_field():
    """Trae 状态响应没有 streak_days 时必须返回 None，不能伪造连续 0 天。"""
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {
        "code": 0, "message": "success", "checked_in": True,
        "credits": 200, "enable": True,
    }
    with patch.object(trae, "_session") as sess:
        sess.return_value.post.return_value = fake
        st = trae.fetch_checkin_status("token", device_id="uid")
    assert st["streak_days"] is None
