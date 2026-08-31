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
    assert json.loads(out["model_route_priority"]) == prio


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
