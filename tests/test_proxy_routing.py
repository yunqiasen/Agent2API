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




# ---------------- 签到 X-Device-Id：凭证 device_id 优先，uid 兜底 ----------------

def test_checkin_device_id_prefers_credential_device_id():
    """扫码/回调导入的凭证自带 device_id（登录时注册），签到优先用它。"""
    assert proxy._checkin_device_id(
        {"platform": "trae", "uid": "3225317503410283",
         "device_id": "f0c660bada0e401195540c826388d614e61d819f228c4db4"}
    ) == "f0c660bada0e401195540c826388d614e61d819f228c4db4"


def test_checkin_device_id_falls_back_to_uid():
    """手填 token 导入无 device_id 时，回退 uid，签到头不缺失。"""
    assert proxy._checkin_device_id(
        {"platform": "trae", "uid": "3225317503410283",
         "device_id": ""}) == "3225317503410283"


def test_checkin_device_id_untouched_for_other_platforms():
    """非 Trae 平台不吃该逻辑，保持账号自身设备号。"""
    assert proxy._checkin_device_id(
        {"platform": "workbuddy", "uid": "u123", "device_id": "wb-dev"}) == "wb-dev"
