"""签到真值与失败可观测性回归测试。"""
from unittest.mock import MagicMock

import pytest

import providers as P
import proxy


def test_workbuddy_checkin_status_uses_latest_checkin_date(monkeypatch):
    """上游按最新日期在前返回时，last_checkin_date 仍应是最新日期。"""
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {
        "code": 0,
        "data": {
            "today_checked_in": True,
            "today_credit": 100,
            "streak_days": 3,
            "checkin_dates": ["2026-09-03", "2026-09-02", "2026-09-01"],
        },
    }
    monkeypatch.setattr(P, "_session", lambda: MagicMock(post=lambda *a, **k: fake))

    state = P.fetch_checkin_status("workbuddy", "token")

    assert state["last_checkin_date"] == "2026-09-03"


def test_checkin_status_failure_is_reported_without_claim(isolated_db, monkeypatch):
    """状态查询失败时不能静默跳过状态核验并直接领取。"""
    db = isolated_db
    aid = db.add_account("workbuddy", "token", uid="u1")
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(db.get_account(aid)))
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda _platform: True)
    monkeypatch.setattr(proxy.P, "fetch_checkin_status",
                        lambda *a, **k: (_ for _ in ()).throw(
                            P.ProviderError("查询签到状态失败 HTTP 502")))
    claimed = []
    monkeypatch.setattr(proxy.P, "do_checkin",
                        lambda *a, **k: claimed.append(True))

    with pytest.raises(proxy.ProxyError, match="状态查询失败"):
        proxy.checkin_account(db.get_account(aid))

    assert claimed == []
    assert db.get_account(aid)["checkin_today"] == 0


def test_checkin_readback_failure_does_not_mark_local_success(isolated_db, monkeypatch):
    """领取后状态回读失败时，不能把本地卡片伪造成已签到。"""
    db = isolated_db
    aid = db.add_account("workbuddy", "token", uid="u1")
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(db.get_account(aid)))
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda _platform: True)
    statuses = iter([
        {"today_checked_in": False, "today_credit": 100, "streak_days": 0},
        P.ProviderError("查询签到状态失败 HTTP 502"),
    ])

    def fetch_status(*args, **kwargs):
        value = next(statuses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(proxy.P, "fetch_checkin_status", fetch_status)
    monkeypatch.setattr(proxy.P, "do_checkin", lambda *a, **k: {
        "ok": True,
        "claimed_credit": 100,
        "today_checked_in": True,
    })
    monkeypatch.setattr(proxy, "refresh_quota", lambda _row: {})

    with pytest.raises(proxy.ProxyError, match="状态回读失败"):
        proxy.checkin_account(db.get_account(aid))

    assert db.get_account(aid)["checkin_today"] == 0
