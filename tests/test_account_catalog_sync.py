"""账号接入到模型目录的完整闭环；上游边界使用测试替身。"""
import json
from unittest.mock import Mock

import pytest
import providers as P
import proxy
from test_credentials_refresh import client, result


@pytest.fixture
def catalogs(monkeypatch):
    fetch = Mock(return_value=["shared", "new-model"])
    monkeypatch.setattr(P, "fetch_upstream_models", fetch)
    monkeypatch.setattr(proxy, "ensure_token", lambda row, **kw: dict(row))
    monkeypatch.setattr(proxy, "refresh_quota", lambda row: {"remain": 100})
    return fetch


@pytest.mark.parametrize("platform", list(P.PLATFORMS))
def test_import_syncs_account_catalog(client, isolated_db, catalogs, platform):
    response = client.post("/api/accounts/import_token", json={
        "platform": platform, "uid": "new-user", "access_token": "token",
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data["added"] == 1
    models = client.get(f"/api/platforms/{platform}/models").get_json()["models"]
    assert {m["id"] for m in models} == {"shared", "new-model"}
    assert data["model_sync_results"][0]["ok"] is True
    assert catalogs.call_count == 1


@pytest.mark.parametrize("path", ["/api/login/poll", "/api/join/poll"])
@pytest.mark.parametrize("platform", ["workbuddy", "codebuddy", "workbuddy_intl"])
def test_web_authorization_syncs_catalog(client, isolated_db, catalogs, monkeypatch, path, platform):
    isolated_db.add_pending("new-state", platform, "https://example.test/login")
    monkeypatch.setattr(P, "poll_token", Mock(return_value=result()))
    monkeypatch.setattr(P, "fetch_account_info", Mock(return_value={"uid": "web-user"}))
    data = client.post(path, json={"state": "new-state"}).get_json()
    assert data["ok"] is True
    assert {m["id"] for m in P.catalog_entries(platform)} == {"shared", "new-model"}
    assert data["model_sync"]["ok"] is True
    assert catalogs.call_count == 1


def test_trae_callback_syncs_catalog(client, catalogs, monkeypatch):
    import trae
    monkeypatch.setattr(trae, "exchange_token", Mock(return_value=result()))
    monkeypatch.setattr(trae, "get_user_info", Mock(return_value={"uid": "trae-user"}))
    data = client.post("/api/login/trae_callback", json={
        "callback_url": "http://127.0.0.1:18080/authorize?refreshToken=test-refresh",
    }).get_json()
    assert data["ok"] is True
    assert {m["id"] for m in P.catalog_entries("trae")} == {"shared", "new-model"}
    assert data["model_sync"]["ok"] is True


def test_sync_is_incremental_and_keeps_user_settings(client, isolated_db, catalogs):
    db = isolated_db
    db.set_model_registry({
        "other-account-model": {"type": "chat", "platforms": ["workbuddy_intl"]},
        "verified-compatible": {"type": "chat", "platforms": ["workbuddy_intl"]},
        "shared": {"type": "chat", "platforms": ["codebuddy"]},
    })
    disabled = json.dumps({"workbuddy_intl": ["shared"]})
    priority = json.dumps({"default": list(P.PLATFORMS)})
    db.set_setting("model_platform_disabled", disabled)
    db.set_setting("model_route_priority", priority)
    response = client.post("/api/accounts/import_token", json={
        "platform": "workbuddy_intl", "uid": "second-user", "access_token": "t",
    })
    assert response.status_code == 200
    assert set(db.get_model_registry()) == {
        "shared", "new-model", "other-account-model", "verified-compatible",
    }
    assert db.get_model_registry()["shared"]["platforms"] == ["codebuddy", "workbuddy_intl"]
    assert db.get_setting("model_platform_disabled") == disabled
    assert db.get_setting("model_route_priority") == priority


def test_catalog_failure_does_not_undo_import_and_can_retry(client, isolated_db, catalogs):
    catalogs.side_effect = P.ProviderError("HTTP 503 catalog unavailable")
    data = client.post("/api/accounts/import_token", json={
        "platform": "workbuddy_intl", "uid": "retry-user", "access_token": "t",
    }).get_json()
    assert data["ok"] is True and data["added"] == 1
    assert data["model_sync_results"][0]["ok"] is False
    assert "503" in data["model_sync_results"][0]["error"]
    aid = isolated_db.find_account_by_uid("workbuddy_intl", "retry-user")["id"]
    isolated_db.update_account(aid, enabled=0)
    catalogs.side_effect = None
    response = client.post(f"/api/accounts/{aid}/models/sync")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert {m["id"] for m in response.get_json()["models"]} == {"shared", "new-model"}
    assert not isolated_db.get_account(aid)["enabled"]


def test_empty_catalog_is_not_reported_as_success(client, isolated_db, catalogs):
    catalogs.return_value = []
    aid = isolated_db.add_account("workbuddy_intl", "tok")
    isolated_db.set_model_registry({"old-model": {"type": "chat", "platforms": ["workbuddy_intl"]}})
    response = client.post(f"/api/accounts/{aid}/models/sync")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is False and data["error"]
    assert [m["id"] for m in data["models"]] == ["old-model"]


def test_catalog_retry_requires_existing_account_and_admin(client):
    assert client.post("/api/accounts/999/models/sync").status_code == 404
    import app
    with app.app.test_client() as anonymous:
        assert anonymous.post("/api/accounts/999/models/sync").status_code == 401


def test_catalog_failures_include_endpoint_reason_and_hide_token(client, isolated_db, monkeypatch):
    aid = isolated_db.add_account("workbuddy_intl", "sensitive-access")
    response = Mock(status_code=503)
    response.json.return_value = {"code": 503, "msg": "catalog service unavailable sensitive-access"}
    session = Mock()
    session.get.return_value = response
    monkeypatch.setattr(P, "_session", lambda: session)
    data = client.post(f"/api/accounts/{aid}/models/sync").get_json()
    assert data["ok"] is False
    assert "503" in data["error"]
    assert "sensitive-access" not in data["error"]
