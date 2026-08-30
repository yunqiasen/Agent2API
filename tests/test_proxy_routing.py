"""proxy 公共行为：模型路由优先级 + 签到设备号选择 + 补签。"""
import json
from unittest.mock import patch

import pytest

import proxy


@pytest.fixture
def registry(monkeypatch):
    """固定注册表：deep-model 三平台同名，供优先级重排断言。"""
    reg = {
        "deep-model": {"type": "chat", "platforms": ["workbuddy", "codebuddy_cn", "codebuddy"]},
        "single-model": {"type": "chat", "platforms": ["trae"]},
    }
    monkeypatch.setattr("db.get_model_registry", lambda: reg)
    monkeypatch.setattr(proxy, "_live_probe_model", lambda mid: None)
    monkeypatch.setattr(proxy, "_mark_probe_failed", lambda mid: None)
    return reg


def test_route_priority_absent_keeps_registry_order(registry, monkeypatch):
    """未配置时保持注册表原顺序，不改变行为。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "")
    assert proxy.model_route("deep-model")[1] == ["workbuddy", "codebuddy_cn", "codebuddy"]


def test_route_priority_global_reorders_all_models(registry, monkeypatch):
    """全局顺序生效：codebuddy 提到最前。"""
    cfg = {"default": ["codebuddy", "trae", "workbuddy"], "overrides": {}}
    monkeypatch.setattr("db.get_setting", lambda k, d=None: json.dumps(cfg))
    assert proxy.model_route("deep-model")[1] == ["codebuddy", "workbuddy", "codebuddy_cn"]


def test_route_priority_single_model_override_beats_global(registry, monkeypatch):
    """单模型覆盖优先于全局默认，其它模型不受影响。"""
    cfg = {"default": ["codebuddy", "trae", "workbuddy"],
           "overrides": {"deep-model": ["workbuddy", "codebuddy"]}}
    monkeypatch.setattr("db.get_setting", lambda k, d=None: json.dumps(cfg))
    assert proxy.model_route("deep-model")[1] == ["workbuddy", "codebuddy", "codebuddy_cn"]
    assert proxy.model_route("single-model")[1] == ["trae"]


def test_route_priority_invalid_json_falls_back(registry, monkeypatch):
    """配置写坏时不炸、不改变顺序。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "{not-json")
    assert proxy.model_route("deep-model")[1] == ["workbuddy", "codebuddy_cn", "codebuddy"]


def test_checkin_device_id_prefers_global_aha_setting(monkeypatch):
    """TraeWork 签到用全局 Aha 设备号（设备级注册，全号池通用）。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "7382917465019283")
    assert proxy._checkin_device_id({"platform": "trae", "device_id": "hex-old"}) == "7382917465019283"


def test_checkin_device_id_falls_back_without_setting(monkeypatch):
    """未配置时回退账号自带 device_id。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "")
    assert proxy._checkin_device_id({"platform": "trae", "device_id": "hex-old"}) == "hex-old"


def test_checkin_device_id_untouched_for_other_platforms(monkeypatch):
    """非 Trae 平台不吃全局设置，保持账号自身设备号。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "7382917465019283")
    assert proxy._checkin_device_id({"platform": "workbuddy", "device_id": "wb-dev"}) == "wb-dev"


def test_retry_failed_checkins_only_targets_due_accounts(isolated_db, monkeypatch):
    """补签只碰：启用 + 支持签到 + 今日未签 的账号。"""
    db = isolated_db
    a1 = db.add_account("trae", "t1", uid="u1", device_id="1111111111111111")
    a2 = db.add_account("trae", "t2", uid="u2")
    a3 = db.add_account("workbuddy", "w1", uid="u3")
    a4 = db.add_account("trae", "t4", uid="u4")
    db.update_account(a4, enabled=0)

    # a2 今天已签，不该进补签队列
    db.update_account(a2, checkin_today=1)

    called = []
    monkeypatch.setattr(proxy.P, "supports_checkin",
                        lambda p: p in ("trae", "workbuddy"))
    monkeypatch.setattr(proxy, "checkin_account",
                        lambda row: called.append(row["id"]) or {"ok": True})
    monkeypatch.setattr(db, "get_account", lambda aid: db.get_conn().execute(
        "SELECT * FROM accounts WHERE id=?", (aid,)).fetchone())
    monkeypatch.setattr(db, "set_setting", lambda k, v: None)

    # 标记已签的 a2 与不支持签到的 codebuddy 不该被重试
    proxy.retry_failed_checkins()
    assert set(called) == {a1, a3}
