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




# ---------------- 签到 X-Device-Id：直接用 uid ----------------

def test_checkin_device_id_uses_uid_for_trae(monkeypatch):
    """TraeWork 签到 X-Device-Id 用 uid（实测最稳，无设备码概念）。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "")
    assert proxy._checkin_device_id(
        {"platform": "trae", "uid": "3225317503410283",
         "device_id": "hex-old"}) == "3225317503410283"


def test_checkin_device_id_falls_back_when_uid_empty():
    """uid 为空时回退账号 device_id，签到头不至于缺失。"""
    assert proxy._checkin_device_id(
        {"platform": "trae", "uid": "", "device_id": "hex-old"}) == "hex-old"


def test_checkin_device_id_untouched_for_other_platforms():
    """非 Trae 平台不吃 uid 逻辑，保持账号自身设备号。"""
    assert proxy._checkin_device_id(
        {"platform": "workbuddy", "uid": "u123", "device_id": "wb-dev"}) == "wb-dev"
