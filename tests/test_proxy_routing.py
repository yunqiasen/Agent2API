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




# ---------------- 签到 X-Device-Id：统一用 uid ----------------

def test_checkin_device_id_uses_uid_even_with_credential_device_id():
    """统一用 uid：手填 token 导入的 device_id 是未注册孤儿码（实测 9074），
    uid 对扫码/手填两类账号均稳定成功。"""
    assert proxy._checkin_device_id(
        {"platform": "trae", "uid": "3225317503410283",
         "device_id": "f0c660bada0e401195540c826388d614e61d819f228c4db4"}
    ) == "3225317503410283"


def test_checkin_device_id_falls_back_to_device_id_when_uid_empty():
    """uid 为空时回退 device_id，签到头不缺失。"""
    assert proxy._checkin_device_id(
        {"platform": "trae", "uid": "",
         "device_id": "hex-old"}) == "hex-old"


def test_checkin_device_id_untouched_for_other_platforms():
    """非 Trae 平台不吃该逻辑，保持账号自身设备号。"""
    assert proxy._checkin_device_id(
        {"platform": "workbuddy", "uid": "u123", "device_id": "wb-dev"}) == "wb-dev"


def test_checkin_all_filters_by_platform(isolated_db, monkeypatch):
    """渠道签到：platform=trae 只签 trae 号，workbuddy 不动。"""
    db = isolated_db
    t1 = db.add_account("trae", "t1", uid="u1")
    w1 = db.add_account("workbuddy", "w1", uid="u2")
    called = []
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda p: True)
    monkeypatch.setattr(proxy.db, "get_account", lambda aid: aid)
    monkeypatch.setattr(proxy, "checkin_account",
                        lambda row: called.append(row) or {"ok": True})
    proxy.checkin_all(platform="trae")
    assert called == [t1]
