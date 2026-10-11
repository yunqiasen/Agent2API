"""登录 pending / 终态 / 过期及并发重放行为。"""
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import providers as P
import proxy
from test_credentials_refresh import client, result


def session_with(body, status=200):
    response = Mock(status_code=status)
    response.json.return_value = body
    session = Mock()
    session.get.return_value = response
    return session


@pytest.mark.parametrize("platform", ["workbuddy", "codebuddy", "workbuddy_intl"])
def test_only_login_in_progress_code_is_pending(platform, monkeypatch):
    session = session_with({"code": 11218, "msg": "login state expired"})
    monkeypatch.setattr(P, "_session", lambda: session)
    with pytest.raises(P.ProviderError, match="11218.*expired"):
        P.poll_token(platform, "st")


def test_malformed_success_is_terminal_not_endless_pending(monkeypatch):
    monkeypatch.setattr(P, "_session", lambda: session_with({"code": 0, "data": {}}))
    with pytest.raises(P.ProviderError, match="access"):
        P.poll_token("workbuddy_intl", "st")


def test_nested_oauth_token_is_supported(monkeypatch):
    monkeypatch.setattr(P, "_session", lambda: session_with({"code": "0", "data": {"data": {"accessToken": "access", "refreshToken": "refresh"}}}))
    out = P.poll_token("workbuddy_intl", "st")
    assert out and out["access_token"] == "access" and out["refresh_token"] == "refresh"


def test_account_info_uses_selected_client_profile(monkeypatch):
    session = session_with({"code": 0, "data": {"uid": "u"}})
    monkeypatch.setattr(P, "_session", lambda: session)
    P.fetch_account_info("workbuddy_intl", "token", state="st")
    assert session.get.call_args.kwargs["headers"].get("User-Agent") == P.PLATFORMS["workbuddy_intl"]["user_agent"]


@pytest.mark.parametrize("path", ["/api/login/poll", "/api/join/poll"])
def test_terminal_error_stops_polling(client, isolated_db, monkeypatch, path):
    isolated_db.add_pending("expired-upstream", "workbuddy_intl", "https://example.test")
    monkeypatch.setattr(P, "_session", lambda: session_with({"code": 11218, "msg": "login state expired"}))
    data = client.post(path, json={"state": "expired-upstream"}).get_json()
    assert data["ok"] is False
    assert data["pending"] is False and data["error"]
    assert not isolated_db.list_accounts()


@pytest.mark.parametrize("path", ["/api/login/poll", "/api/join/poll"])
def test_local_state_expiry_skips_upstream(client, isolated_db, monkeypatch, path):
    db = isolated_db
    db.add_pending("stale", "workbuddy_intl", "https://example.test")
    db.get_conn().execute("UPDATE pending_login SET created_at=? WHERE state='stale'", (int(time.time())-301,))
    db.get_conn().commit()
    poll = Mock(return_value=None)
    monkeypatch.setattr(P, "poll_token", poll)
    data = client.post(path, json={"state": "stale"}).get_json()
    assert data.get("expired") is True and data.get("pending") is False
    poll.assert_not_called()


@pytest.mark.parametrize("path", ["/api/login/poll", "/api/join/poll"])
def test_transient_network_error_is_retryable(client, isolated_db, monkeypatch, path):
    isolated_db.add_pending("retry", "workbuddy_intl", "https://example.test")
    monkeypatch.setattr(P, "poll_token", Mock(side_effect=P.ProviderError("HTTP 503 temporary")))
    data = client.post(path, json={"state": "retry"}).get_json()
    assert data["pending"] is True and "503" in data["error"]


@pytest.mark.parametrize("path", ["/api/login/poll", "/api/join/poll"])
def test_bad_state_type_is_rejected_as_400(client, path):
    assert client.post(path, json={"state": ["unexpected"]}).status_code == 400


@pytest.mark.parametrize("path", ["/api/login/poll", "/api/join/poll"])
def test_concurrent_polls_exchange_once(client, isolated_db, monkeypatch, path):
    import app
    isolated_db.add_pending("same-state", "workbuddy_intl", "https://example.test")
    def delayed(*a, **kw):
        time.sleep(.05)
        return result()
    poll = Mock(side_effect=delayed)
    monkeypatch.setattr(P, "poll_token", poll)
    monkeypatch.setattr(P, "fetch_account_info", Mock(return_value={"uid": "u"}))
    monkeypatch.setattr(P, "fetch_upstream_models", Mock(return_value=["m"]))
    monkeypatch.setattr(proxy, "refresh_quota", Mock(return_value={}))
    def request(_):
        with app.app.test_client() as c:
            with c.session_transaction() as s:
                s["admin"] = True
            return c.post(path, json={"state": "same-state"}).get_json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(request, range(2)))
    assert poll.call_count == 1
    assert sum(bool(r["ok"]) for r in responses) == 1
    assert sum(bool(r.get("done")) for r in responses) == 1
    assert len(isolated_db.list_accounts()) == 1


@pytest.mark.parametrize("path", ["/api/login/poll", "/api/join/poll", "/api/login/trae_callback"])
def test_new_authorization_clears_old_refresh_failure(client, isolated_db, monkeypatch, path):
    import trae
    platform = "trae" if path.endswith("trae_callback") else "workbuddy_intl"
    db = isolated_db
    aid = db.add_account(platform, "old-token", uid="u")
    db.update_account(aid, refresh_error="old refresh revoked", refresh_retry_at=int(time.time())+3000, refresh_failures=5)
    db.add_pending("reauth", platform, "https://example.test")
    monkeypatch.setattr(P, "poll_token", Mock(return_value=result()))
    monkeypatch.setattr(P, "fetch_account_info", Mock(return_value={"uid": "u"}))
    monkeypatch.setattr(P, "fetch_upstream_models", Mock(return_value=["m"]))
    monkeypatch.setattr(trae, "exchange_token", Mock(return_value=result()))
    monkeypatch.setattr(trae, "get_user_info", Mock(return_value={"uid": "u"}))
    monkeypatch.setattr(proxy, "refresh_quota", Mock(return_value={}))
    payload = {"callback_url": "http://127.0.0.1/authorize?refreshToken=rt"} if platform=="trae" else {"state": "reauth"}
    assert client.post(path, json=payload).get_json()["ok"]
    row = db.get_account(aid)
    assert row["refresh_error"] == ""
    assert row["refresh_failures"] == 0 and row["refresh_retry_at"] == 0
