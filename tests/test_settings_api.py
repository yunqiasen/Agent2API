"""设置 seam：新增设置项经 /api/settings 公共接口可读写。"""
import json

import pytest


@pytest.fixture
def client(isolated_db):
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    app_mod.app.secret_key = "test-secret"
    with app_mod.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["admin"] = True
        yield c, isolated_db


def test_settings_roundtrip_new_fields(client):
    c, db = client
    # 初始：无值
    # 写入
    prio = {"default": ["trae", "workbuddy", "codebuddy"], "overrides": {"glm-5": ["codebuddy"]}}
    r = c.post("/api/settings", json={"model_route_priority": json.dumps(prio)})
    assert r.get_json()["ok"] is True
    # 读回
    out = c.get("/api/settings").get_json()
    saved = json.loads(out["model_route_priority"])
    assert saved["default"] == prio["default"]
    assert saved["overrides"]["glm-5"] == ["codebuddy", "workbuddy", "trae"]


def test_settings_rejects_unknown_keys_implicitly(client):
    """白名单外字段静默丢弃，不写库（沿用既有契约）。"""
    c, db = client
    c.post("/api/settings", json={"admin_password": "hack", "whatever": "x"})
    assert db.get_setting("admin_password", "") != "hack"
    assert db.get_setting("whatever", "") == ""


def test_model_switch_setting_roundtrip(client):
    """渠道×模型开关设置可经公共设置接口读写。"""
    c, db = client
    cfg = {"workbuddy": ["glm-5.3"], "trae": []}
    r = c.post("/api/settings", json={"model_platform_disabled": json.dumps(cfg)})
    assert r.get_json()["ok"] is True
    out = c.get("/api/settings").get_json()
    assert json.loads(out["model_platform_disabled"]) == cfg


def test_models_discover_progress_includes_disabled_map(client):
    """模型目录响应携带开关状态，供卡片渲染禁用态。"""
    c, db = client
    db.set_model_registry({"glm-5.3": {"type": "chat", "platforms": ["workbuddy", "trae"]}})
    db.set_setting("model_platform_disabled", json.dumps({"workbuddy": ["glm-5.3"]}))
    out = c.get("/api/models/discover/progress").get_json()
    assert out["disabled"] == {"workbuddy": ["glm-5.3"]}


def test_discover_exposed_reflects_disabled_switch(client):
    """「对外暴露」清单与真实 /v1/models 一致：三渠道全关的模型不显示。"""
    c, db = client
    db.set_model_registry({
        "glm-5.3": {"type": "chat", "platforms": ["workbuddy", "trae"]},
        "deepseek-v4-flash": {"type": "chat", "platforms": ["workbuddy"]},
    })
    db.set_setting("model_platform_disabled", json.dumps({
        "workbuddy": ["glm-5.3", "deepseek-v4-flash"],
        "trae": ["glm-5.3"],
    }))
    out = c.get("/api/models/discover/progress").get_json()
    ids = [m["id"] for m in out["exposed"]]
    assert ids == []  # 两个模型都被全渠道关闭


def test_health_alias_is_available(client):
    """健康检查同时兼容 /health 与 /api/health，避免监控口径不一致。"""
    c, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_import_token_reports_quota_refresh_failure(client, monkeypatch):
    """凭证导入成功但额度刷新失败时，接口必须明确暴露失败而非静默显示正常。"""
    c, db = client
    import app as app_mod

    monkeypatch.setattr(app_mod.proxy.P, "collect_quota", lambda *a, **k: {
        "errors": ["上游额度暂不可用"], "total": 0, "used": 0, "remain": 0,
    })
    r = c.post("/api/accounts/import_token", json={
        "platform": "trae", "access_token": "tok", "uid": "u1",
    })
    assert r.status_code == 200
    out = r.get_json()
    assert out["ok"] is True
    assert out["added"] == 1
    assert out["refresh_errors"][0]["id"] == 1
    row = db.get_account(1)
    assert row["status"] == "error"
    assert "额度暂不可用" in row["status_reason"]


def test_import_token_preserves_existing_trae_device_metadata(client, monkeypatch):
    """只更新 token 时，凭证里缺失的 Trae 设备字段不能被清空。"""
    c, db = client
    import app as app_mod

    aid = db.add_account("trae", "old-token", uid="u1",
                         machine_id="machine-registered", device_id="device-registered",
                         nickname="旧名称")
    monkeypatch.setattr(app_mod.proxy, "refresh_quota", lambda _row: {})
    r = c.post("/api/accounts/import_token", json={
        "platform": "trae", "access_token": "new-token", "uid": "u1",
    })
    assert r.status_code == 200
    row = db.get_account(aid)
    assert row["access_token"] == "new-token"
    assert row["machine_id"] == "machine-registered"
    assert row["device_id"] == "device-registered"
    assert row["nickname"] == "旧名称"


def test_import_token_rejects_json_item_without_access_token(client):
    """批量导入中出现缺 token 项时，直接返回可读错误，不写入半成品账号。"""
    c, db = client
    r = c.post("/api/accounts/import_token", json={
        "platform": "workbuddy", "json_text": '[{"uid":"u1"}]',
    })
    assert r.status_code == 400
    assert "access_token" in r.get_json()["error"]
    assert db.count_accounts() == 0


def test_v1_models_is_scoped_to_bound_api_key_platform(client):
    """绑定平台的 Key 读取模型目录时，只能看到该渠道仍可用的模型。"""
    c, db = client
    db.set_model_registry({
        "work-model": {"type": "chat", "platforms": ["workbuddy"]},
        "trae-model": {"type": "chat", "platforms": ["trae"]},
        "shared-model": {"type": "chat", "platforms": ["workbuddy", "trae"]},
    })
    key = db.add_api_key("Trae Key", "test-bound-key", "trae")
    r = c.get("/v1/models", headers={"Authorization": "Bearer " + key})
    assert r.status_code == 200
    assert [x["id"] for x in r.get_json()["data"]] == ["shared-model", "trae-model"]


def test_panel_test_chooses_model_available_on_selected_platform(client, monkeypatch):
    """面板按指定渠道测活时，默认模型必须从该渠道的注册表中选择。"""
    c, db = client
    db.set_model_registry({
        "aaa-trae": {"type": "chat", "platforms": ["trae"]},
        "zzz-work": {"type": "chat", "platforms": ["workbuddy"]},
    })
    captured = {}

    class Resp:
        def close(self):
            pass

    def fake_stream(body, platform=None):
        captured.update(body=body, platform=platform)
        return ({"id": 1, "platform": "workbuddy"}, Resp())

    monkeypatch.setattr("app.proxy.stream_chat", fake_stream)
    monkeypatch.setattr("app.proxy.aggregate_stream", lambda _resp, model: {
        "choices": [{"message": {"content": "ok"}}], "model": model,
    })
    r = c.post("/api/test", json={"platform": "workbuddy"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert captured["body"]["model"] == "zzz-work"


def test_login_poll_does_not_create_account_without_upstream_uid(client, monkeypatch):
    """SSO 拿到 token 但账号信息没有 uid 时，不得写入空身份账号。"""
    c, db = client
    import app as app_mod

    db.add_pending("state-no-uid", "workbuddy", "https://example.test/login", "test")
    monkeypatch.setattr(app_mod.P, "poll_token", lambda *a, **k: {
        "access_token": "tok", "refresh_token": "rt", "expires_at": 0,
    })
    monkeypatch.setattr(app_mod.P, "fetch_account_info", lambda *a, **k: {
        "uid": "", "email": "", "nickname": "",
        "enterprise_id": "", "enterprise_name": "",
    })
    monkeypatch.setattr(app_mod.proxy, "refresh_quota", lambda _row: {})

    r = c.post("/api/login/poll", json={"state": "state-no-uid"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is False
    assert db.count_accounts() == 0


def test_trae_callback_preserves_device_metadata_when_omitted(client, monkeypatch):
    """Trae 回调只更新 token 时，不得把已有设备元数据清空。"""
    c, db = client
    import app as app_mod
    from urllib.parse import quote

    aid = db.add_account("trae", "old-token", uid="u1",
                         machine_id="machine-old", device_id="device-old")
    monkeypatch.setattr(app_mod.trae, "get_user_info", lambda _token: {
        "uid": "u1", "nickname": "用户1", "enterprise_id": "",
    })
    monkeypatch.setattr(app_mod.proxy, "refresh_quota", lambda _row: {})
    callback = "http://127.0.0.1:18080/authorize?userJwt=" + quote(
        '{"Token":"new-token","TokenExpireAt":0}'
    )

    r = c.post("/api/login/trae_callback", json={"callback_url": callback})
    assert r.status_code == 200
    row = db.get_account(aid)
    assert row["access_token"] == "new-token"
    assert row["machine_id"] == "machine-old"
    assert row["device_id"] == "device-old"


def test_public_join_does_not_start_trae_polling_flow(client):
    """Trae 只有回调导入，公共页面没有回调输入时不能启动永不结束的轮询。"""
    c, _ = client
    r = c.post("/api/join/start", json={"platform": "trae"})
    assert r.status_code == 400
    assert "回调" in r.get_json()["error"]


def test_models_progress_migrates_legacy_registry_platform(client):
    """模型进度接口把旧 CodeBuddy CN 模型迁入 WorkBuddy，不丢模型。"""
    c, db = client
    db.set_model_registry({
        "legacy": {"type": "chat", "platforms": ["codebuddy_cn"]},
        "current": {"type": "chat", "platforms": ["workbuddy"]},
    })
    out = c.get("/api/models/discover/progress").get_json()
    assert out["total"] == 2
    workbuddy = next(p for p in out["platforms"] if p["platform"] == "workbuddy")
    assert {m["id"] for m in workbuddy["models"]} == {"legacy", "current"}
    assert all("codebuddy_cn" not in (p.get("platform") or "") for p in out["platforms"])


def test_account_mutations_validate_target_and_boolean(client):
    c, db = client
    r = c.patch("/api/accounts/999", json={"enabled": 0})
    assert r.status_code == 404

    aid = db.add_account("workbuddy", "tok", uid="u1")
    r = c.patch(f"/api/accounts/{aid}", json={"enabled": "false"})
    assert r.status_code == 200
    assert db.get_account(aid)["enabled"] == 0


def test_logs_rejects_invalid_limit(client):
    r = client[0].get("/api/logs?limit=abc")
    assert r.status_code == 400


def test_import_token_enriches_missing_uid_from_upstream(client, monkeypatch):
    """Token JSON 未带 uid 时，导入流程应从上游补齐身份再按 uid 入库。"""
    c, db = client
    import app as app_mod
    monkeypatch.setattr(app_mod.P, "fetch_account_info", lambda *a, **k: {
        "uid": "u-enriched", "email": "user@example.com", "nickname": "用户",
        "enterprise_id": "ent", "enterprise_name": "企业",
    })
    monkeypatch.setattr(app_mod.proxy, "refresh_quota", lambda _row: {})
    r = c.post("/api/accounts/import_token", json={
        "platform": "workbuddy", "access_token": "tok-no-uid",
    })
    assert r.status_code == 200
    row = db.get_account(1)
    assert row["uid"] == "u-enriched"
    assert row["email"] == "user@example.com"


def test_openai_endpoints_reject_non_object_json(client):
    """三类 OpenAI 入口收到 JSON 数组时应返回 400，而不是在 body.get 处 500。"""
    c, db = client
    db.add_api_key("test", "test-openai-key")
    headers = {"Authorization": "Bearer test-openai-key"}
    for path in ("/v1/chat/completions", "/v1/images/generations", "/v1/responses"):
        r = c.post(path, headers=headers, json=[])
        assert r.status_code == 400, (path, r.status_code, r.get_data(as_text=True))
        assert "JSON 对象" in r.get_json()["error"]["message"]


def test_api_key_patch_parses_string_false_and_rejects_missing_key(client):
    """Key 开关要正确解析字符串 false，且不存在的 Key 返回 404。"""
    c, db = client
    kid = db.add_api_key("test", "test-key")
    key_row = db.get_conn().execute("SELECT id FROM api_keys WHERE api_key=?", (kid,)).fetchone()
    key_id = key_row["id"]

    r = c.patch(f"/api/keys/{key_id}", json={"enabled": "false"})
    assert r.status_code == 200
    assert db.get_api_key("test-key") is None

    r = c.patch("/api/keys/999", json={"enabled": 0})
    assert r.status_code == 404


def test_api_key_add_rejects_unknown_platform(client):
    """API Key 绑定渠道必须使用当前三渠道之一，避免写入幽灵路由。"""
    c, db = client
    before = [dict(row) for row in db.list_api_keys()]
    r = c.post("/api/keys", json={"name": "bad", "platform": "codebuddy_cn"})
    assert r.status_code == 400
    assert [dict(row) for row in db.list_api_keys()] == before


def test_legacy_platform_aliases_are_migrated_before_account_api(client):
    """旧库里的 codebuddy_cn/ide 记录启动后必须归一到当前渠道键。"""
    c, db = client
    db.add_account("codebuddy_cn", "tok-cn", uid="cn-user")
    db.add_api_key("legacy", "legacy-key", "ide")

    # 模拟启动迁移；旧记录不应让 /api/accounts 因未知平台 500。
    import app as app_mod
    app_mod.migrate_legacy_platforms()

    r = c.get("/api/accounts")
    assert r.status_code == 200
    assert r.get_json()[0]["platform"] == "workbuddy"
    keys = [dict(row) for row in db.list_api_keys()]
    assert next(row for row in keys if row["api_key"] == "legacy-key")["platform"] == "codebuddy"


def test_login_poll_returns_completed_without_replaying_account_import(client):
    """已完成的登录 state 再次轮询时不能重复入库或重复刷新。"""
    c, db = client
    db.add_pending("done-state", "workbuddy", "https://example.test", "done")
    db.finish_pending("done-state")
    r = c.post("/api/login/poll", json={"state": "done-state"})
    assert r.status_code == 200
    assert r.get_json() == {
        "ok": False, "pending": False, "done": True, "msg": "登录流程已完成"
    }


def test_login_start_rejects_unknown_platform(client):
    """后台登录入口对未知渠道返回客户端错误，而不是 500。"""
    c, _ = client
    r = c.post("/api/login/start", json={"platform": "codebuddy_cn"})
    assert r.status_code == 400
    assert "未知平台" in r.get_json()["error"]


def test_model_switch_settings_migrate_legacy_platform_aliases(client):
    """模型开关配置里的旧渠道别名也要迁移，避免开关保存后失效。"""
    c, _ = client
    r = c.post("/api/settings", json={
        "model_platform_disabled": json.dumps({
            "codebuddy_cn": ["legacy-cn"],
            "ide": ["legacy-intl"],
        })
    })
    assert r.status_code == 200
    out = json.loads(c.get("/api/settings").get_json()["model_platform_disabled"])
    assert out == {"codebuddy": ["legacy-intl"], "workbuddy": ["legacy-cn"]}


def test_public_join_poll_returns_completed_without_replaying_account_import(client):
    """公共接入 state 完成后重复轮询也只能返回完成状态。"""
    c, db = client
    db.add_pending("done-join", "workbuddy", "https://example.test", "join")
    db.finish_pending("done-join")
    r = c.post("/api/join/poll", json={"state": "done-join"})
    assert r.status_code == 200
    assert r.get_json() == {
        "ok": False, "pending": False, "done": True, "msg": "接入流程已完成"
    }


def test_settings_reject_malformed_runtime_values(client):
    """运行参数必须在保存时校验，避免 refresh_ahead 等值把请求线程打成 500。"""
    c, db = client
    for payload in (
        {"refresh_ahead": "abc"},
        {"log_retention": "-1"},
        {"strategy": "not-a-strategy"},
        {"checkin_time": "25:99"},
    ):
        r = c.post("/api/settings", json=payload)
        assert r.status_code == 400, (payload, r.status_code, r.get_data(as_text=True))
    assert db.get_setting("refresh_ahead") == "900"
    assert db.get_setting("strategy") == "least_used"


def test_api_key_delete_rejects_missing_key(client):
    """删除不存在的 API Key 必须返回 404，避免面板显示假成功。"""
    c, _ = client
    r = c.delete("/api/keys/999")
    assert r.status_code == 404


def test_openai_endpoints_reject_non_string_model(client):
    """model 字段必须是字符串，避免列表/对象被转成奇怪的实时探测请求。"""
    c, db = client
    db.add_api_key("test", "test-model-key")
    headers = {"Authorization": "Bearer test-model-key"}
    cases = (
        ("/v1/chat/completions", {"model": {"id": "hy3"}, "messages": [{"role": "user", "content": "x"}]}),
        ("/v1/images/generations", {"model": ["hunyuan-image-v3.0"], "prompt": "x"}),
        ("/v1/responses", {"model": 123, "input": "x"}),
    )
    for path, payload in cases:
        r = c.post(path, headers=headers, json=payload)
        assert r.status_code == 400, (path, r.status_code, r.get_data(as_text=True))
        assert "model" in r.get_json()["error"]["message"]


@pytest.mark.parametrize("method,path", [
    ("post", "/api/login"),
    ("post", "/api/password"),
    ("post", "/api/accounts/import_token"),
    ("post", "/api/login/start"),
    ("post", "/api/keys"),
    ("post", "/api/test"),
])
def test_json_mutation_endpoints_reject_non_object_bodies(client, method, path):
    """管理与登录入口统一拒绝数组 JSON，避免 data.get 触发 500。"""
    c, _ = client
    r = getattr(c, method)(path, json=["bad"])
    assert r.status_code == 400, (path, r.status_code, r.get_data(as_text=True))
    assert "JSON 对象" in r.get_json()["error"]


def test_add_account_is_idempotent_for_platform_uid(client):
    """同渠道同 uid 重复导入必须更新原记录，不能扩大成重复号池。"""
    _, db = client
    first = db.add_account("workbuddy", "old-token", uid="same-user")
    second = db.add_account("workbuddy", "new-token", uid="same-user")
    assert second == first
    rows = db.list_accounts("workbuddy")
    assert len(rows) == 1
    assert rows[0]["access_token"] == "new-token"


def test_legacy_platform_migration_merges_duplicate_uid_accounts(client):
    """codebuddy_cn 合并进 WorkBuddy 时，同一真实账号只能保留一张号池卡片。"""
    _, db = client
    db.add_account("workbuddy", "old-token", uid="same-user", label="existing")
    db.add_account("codebuddy_cn", "new-token", uid="same-user", label="legacy")

    import app as app_mod
    app_mod.migrate_legacy_platforms()

    rows = [r for r in db.list_accounts("workbuddy") if r["uid"] == "same-user"]
    assert len(rows) == 1
    assert rows[0]["access_token"] == "new-token"


def test_management_endpoints_reject_invalid_string_fields(client):
    """管理接口的字符串字段收到数组/对象时应返回 400，不能在 strip/int 处 500。"""
    c, db = client
    aid = db.add_account("workbuddy", "tok", uid="u1")
    cases = [
        (f"/api/accounts/{aid}/probe", {"model": ["hy3"]}),
        ("/api/accounts/checkin_all", {"platform": ["workbuddy"]}),
        ("/api/accounts/import_token", {"platform": "workbuddy", "access_token": ["tok"]}),
        ("/api/accounts/import_token", {"platform": "workbuddy", "access_token": "tok", "expires_at": "later"}),
        ("/api/accounts/import_token", {"platform": "workbuddy", "json_text": "123"}),
        ("/api/join/start", {"platform": "workbuddy", "label": ["bad"]}),
        ("/api/login/trae_callback", {"callback_url": ["bad"]}),
        ("/api/keys", {"name": "bad", "api_key": ["bad"]}),
    ]
    for path, payload in cases:
        r = c.post(path, json=payload)
        assert r.status_code == 400, (path, r.status_code, r.get_data(as_text=True))


def test_legacy_account_merge_repoints_existing_logs(client):
    """旧渠道重复账号合并时，历史调用日志必须改指向保留账号。"""
    _, db = client
    keep = db.add_account("workbuddy", "old-token", uid="same-user")
    duplicate = db.add_account("codebuddy_cn", "new-token", uid="same-user")
    db.add_log(None, duplicate, "codebuddy_cn", "hy3", 200, False, 10)

    import app as app_mod
    app_mod.migrate_legacy_platforms()

    rows = db.list_logs(10)
    assert len(rows) == 1
    assert rows[0]["account_id"] == keep
    assert rows[0]["platform"] == "workbuddy"
    assert db.get_account(duplicate) is None


def test_login_page_does_not_advertise_insecure_default_password():
    """部署版登录页不能继续提示默认 admin，避免误导已有实例。"""
    from pathlib import Path
    source = Path("frontend/src/features/auth/login-page.tsx").read_text()
    assert 'placeholder="默认 admin"' not in source


def test_favicon_request_does_not_emit_browser_404(client):
    """浏览器默认请求 favicon 时应安静返回，避免控制台固定 404。"""
    c, _ = client
    r = c.get('/favicon.ico')
    assert r.status_code in (200, 204)


def test_models_page_loads_registry_without_auto_discovery():
    """进入模型页只读现有注册表，真实探测必须由用户点击刷新触发。"""
    from pathlib import Path
    source = Path("frontend/src/features/models/models-page.tsx").read_text()
    effect = source.split("useEffect(() => {", 1)[1].split("}, []);", 1)[0]
    assert "loadCurrent();" in effect
    assert "discover();" not in effect


def test_checkin_summary_distinguishes_new_claims_from_skips():
    """签到汇总要把“本次新签到”和“上游已签到跳过”分开显示，避免把 0 新签到误读为全失败。"""
    from pathlib import Path
    source = Path("frontend/src/features/accounts/accounts.tsx").read_text()
    assert "新签到 ${success}" in source
