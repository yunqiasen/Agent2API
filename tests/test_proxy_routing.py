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


# ---------------- 渠道×模型开关（model_platform_disabled） ----------------

def _disabled_cfg(mapping):
    def fake_get(k, d=None):
        if k == "model_platform_disabled":
            return json.dumps(mapping)
        return ""
    return fake_get


def test_model_switch_disabled_platform_removed_from_candidates(registry, monkeypatch):
    """关闭 workbuddy 的 deep-model 后，候选剔除 workbuddy，其余保留。"""
    monkeypatch.setattr("db.get_setting",
                        _disabled_cfg({"workbuddy": ["deep-model"]}))
    assert proxy.model_route("deep-model")[1] == ["codebuddy_cn", "codebuddy"]


def test_model_switch_all_platforms_disabled_yields_no_candidates(registry, monkeypatch):
    """三渠道全关：候选为空，调度层据此 503。"""
    monkeypatch.setattr("db.get_setting", _disabled_cfg({
        "workbuddy": ["deep-model"],
        "codebuddy_cn": ["deep-model"],
        "codebuddy": ["deep-model"],
    }))
    assert proxy.model_route("deep-model")[1] == []


def test_model_switch_beats_explicit_key_binding(registry, monkeypatch):
    """开关全局生效：Key 绑定 workbuddy 且该模型在该渠道被关 → 无候选；
    未禁用的绑定渠道不受影响。"""
    monkeypatch.setattr("db.get_setting",
                        _disabled_cfg({"workbuddy": ["deep-model"]}))
    assert proxy.resolve_platform_candidates("deep-model", "workbuddy") == []
    assert proxy.resolve_platform_candidates("deep-model", "codebuddy") == ["codebuddy"]


def test_model_switch_only_affects_listed_model(registry, monkeypatch):
    """关闭只影响列出的模型：workbuddy 的其它模型照常。"""
    reg = dict(registry)
    reg["wb-only-model"] = {"type": "chat", "platforms": ["workbuddy"]}
    monkeypatch.setattr("db.get_model_registry", lambda: reg)
    monkeypatch.setattr("db.get_setting",
                        _disabled_cfg({"workbuddy": ["deep-model"]}))
    assert proxy.model_route("wb-only-model")[1] == ["workbuddy"]


def test_model_switch_invalid_json_keeps_candidates(registry, monkeypatch):
    """配置写坏时不炸、不拦截任何平台。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "{bad")
    assert proxy.model_route("deep-model")[1] == ["workbuddy", "codebuddy_cn", "codebuddy"]


def test_list_models_hides_fully_disabled_model(registry, monkeypatch):
    """三渠道全关的模型从 /v1/models 消失；部分关闭仍可见。"""
    monkeypatch.setattr("db.get_setting", _disabled_cfg({
        "workbuddy": ["deep-model"],
        "codebuddy_cn": ["deep-model"],
        "codebuddy": ["deep-model"],
    }))
    assert "deep-model" not in proxy.list_models()
    assert "single-model" in proxy.list_models()


# ---------------- TraeWork Aha 设备号池：账号分摊轮询 ----------------

def test_aha_pool_assigns_accounts_round_robin(monkeypatch):
    """设备号池按账号序分摊：i % n，同一天账号固定用同一台。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "1111111111111111\n2222222222222222")
    accs = [{"id": 3, "platform": "trae", "device_id": "hex"},
            {"id": 1, "platform": "trae", "device_id": "hex"},
            {"id": 2, "platform": "trae", "device_id": "hex"}]
    monkeypatch.setattr("db.list_accounts", lambda *a, **k: accs)
    got = {a["id"]: proxy._checkin_device_id(a) for a in accs}
    # 按账号 id 排序后 1→dev0, 2→dev1, 3→dev0
    assert got == {1: "1111111111111111", 2: "2222222222222222",
                   3: "1111111111111111"}


def test_aha_pool_wraps_when_fewer_devices(monkeypatch):
    """设备少于账号：取模环绕，不报错。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "1111111111111111")
    accs = [{"id": 1, "platform": "trae"}, {"id": 2, "platform": "trae"},
            {"id": 3, "platform": "trae"}]
    monkeypatch.setattr("db.list_accounts", lambda *a, **k: accs)
    assert [proxy._checkin_device_id(a) for a in accs] == ["1111111111111111"] * 3


def test_aha_pool_falls_back_without_setting(monkeypatch):
    """未配置回退账号自带 device_id。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "")
    assert proxy._checkin_device_id(
        {"platform": "trae", "device_id": "hex-old"}) == "hex-old"


def test_aha_pool_skips_invalid_lines(monkeypatch):
    """脏行（空行/非数字/UUID）被剔除，不参与分配。"""
    monkeypatch.setattr("db.get_setting",
                        lambda k, d=None: "uuid-x\n1111111111111111\n\n2222222222222222\n")
    accs = [{"id": 1, "platform": "trae"}, {"id": 2, "platform": "trae"},
            {"id": 3, "platform": "trae"}]
    monkeypatch.setattr("db.list_accounts", lambda *a, **k: accs)
    assert [proxy._checkin_device_id(a) for a in accs] == [
        "1111111111111111", "2222222222222222", "1111111111111111"]


def test_aha_pool_untouched_for_other_platforms(monkeypatch):
    """非 Trae 平台不吃设备池，保持账号自身设备号。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "1111111111111111")
    assert proxy._checkin_device_id(
        {"platform": "workbuddy", "device_id": "wb-dev"}) == "wb-dev"


def test_aha_pool_handles_sqlite_rows(isolated_db, monkeypatch):
    """真实 list_accounts 返回 sqlite3.Row（无 .get），分摊逻辑必须兼容。"""
    db = isolated_db
    a1 = db.add_account("trae", "t1", uid="u1")
    a2 = db.add_account("trae", "t2", uid="u2")
    monkeypatch.setattr("db.get_setting",
                        lambda k, d=None: "1111111111111111\n2222222222222222")
    got = {a: proxy._checkin_device_id({"id": aid, "platform": "trae"})
           for aid, a in [(a1, "a1"), (a2, "a2")]}
    assert got == {"a1": "1111111111111111", "a2": "2222222222222222"}
