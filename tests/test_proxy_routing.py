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
    assert proxy.model_route("deep-model")[1] == ["workbuddy", "codebuddy"]


def test_route_priority_global_reorders_all_models(registry, monkeypatch):
    """全局顺序生效：codebuddy 提到最前。"""
    cfg = {"default": ["codebuddy", "trae", "workbuddy"], "overrides": {}}
    monkeypatch.setattr("db.get_setting", lambda k, d=None: json.dumps(cfg))
    assert proxy.model_route("deep-model")[1] == ["codebuddy", "workbuddy"]


def test_route_priority_single_model_override_beats_global(registry, monkeypatch):
    """单模型覆盖优先于全局默认，其它模型不受影响。"""
    cfg = {"default": ["codebuddy", "trae", "workbuddy"],
           "overrides": {"deep-model": ["workbuddy", "codebuddy"]}}
    monkeypatch.setattr("db.get_setting", lambda k, d=None: json.dumps(cfg))
    assert proxy.model_route("deep-model")[1] == ["workbuddy", "codebuddy"]
    assert proxy.model_route("single-model")[1] == ["trae"]


def test_route_priority_invalid_json_falls_back(registry, monkeypatch):
    """配置写坏时不炸、不改变顺序。"""
    monkeypatch.setattr("db.get_setting", lambda k, d=None: "{not-json")
    assert proxy.model_route("deep-model")[1] == ["workbuddy", "codebuddy"]




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


def test_checkin_device_id_accepts_sqlite_row(isolated_db):
    """入参兼容 sqlite3.Row（不转 dict 直接传时不该崩）。"""
    db = isolated_db
    aid = db.add_account("trae", "t1", uid="u-123", device_id="hex")
    row = db.get_account(aid)
    assert proxy._checkin_device_id(row) == "u-123"


# ---------------- 签到跨日状态 ----------------

def test_stale_checkin_flag_does_not_skip_new_day(isolated_db, monkeypatch):
    """昨日 checkin_today=1 到今天必须失效，全部签到仍应命中该账号。"""
    db = isolated_db
    aid = db.add_account("workbuddy", "w1", uid="u1")
    db.update_account(aid, checkin_today=1, checkin_last_date="2026-08-31",
                      checkin_updated_at=1788134400)
    called = []
    monkeypatch.setattr(proxy, "_today_str", lambda: "2026-09-01")
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda p: True)
    monkeypatch.setattr(proxy, "checkin_account",
                        lambda row: called.append(row["id"]) or {"claimed_credit": 100})
    out = proxy.checkin_all(platform="workbuddy")
    assert called == [aid]
    assert out["results"][0]["ok"] is True
    assert out["results"][0].get("skipped") is not True


def test_current_day_local_flag_still_verifies_upstream(isolated_db, monkeypatch):
    """今天本地已签也必须进入上游核验；由上游结果决定是否跳过。"""
    db = isolated_db
    aid = db.add_account("workbuddy", "w1", uid="u1")
    db.update_account(aid, checkin_today=1, checkin_last_date="2026-09-01",
                      checkin_updated_at=1788220800)
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda p: True)
    called = []
    monkeypatch.setattr(proxy, "checkin_account", lambda row: (
        called.append(row["id"]) or {"skipped": True,
                                      "reason": "上游确认今日已签到"}
    ))
    out = proxy.checkin_all(platform="workbuddy")
    assert called == [aid]
    assert out["results"] == [{"id": aid, "ok": True,
                               "claimed_credit": 0, "streak_days": 0,
                               "skipped": True, "reason": "上游确认今日已签到"}]


def test_checkin_all_verifies_upstream_even_when_local_says_checked(isolated_db, monkeypatch):
    """批量签到不得信任本地 checkin_today；每个账号必须进入上游状态核验链。"""
    db = isolated_db
    aid = db.add_account("trae", "t1", uid="u1")
    db.update_account(aid, checkin_today=1, checkin_last_date="2026-09-01")
    called = []
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda p: True)
    monkeypatch.setattr(proxy, "checkin_account", lambda row: (
        called.append(row["id"]) or {"claimed_credit": 200, "streak_days": 1}
    ))
    out = proxy.checkin_all(platform="trae")
    assert called == [aid]
    assert out["results"][0]["claimed_credit"] == 200


def test_checkin_account_skips_only_after_upstream_confirms_today(isolated_db, monkeypatch):
    """单号签到先查上游；上游确认已签才跳过，且回写本地状态。"""
    db = isolated_db
    aid = db.add_account("trae", "t1", uid="u1", device_id="bad-dev")
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(db.get_account(aid)))
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda p: True)
    monkeypatch.setattr(proxy.P, "fetch_checkin_status", lambda *a, **k: {
        "supported": True, "today_checked_in": True, "today_credit": 200,
        "streak_days": 4, "last_checkin_date": "2026-09-01",
    })
    monkeypatch.setattr(proxy.P, "do_checkin",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("已签不应重复领取")))
    monkeypatch.setattr(proxy, "refresh_quota", lambda row: 0)
    res = proxy.checkin_account(db.get_account(aid))
    assert res["skipped"] is True
    assert res["reason"] == "上游确认今日已签到"
    row = db.get_account(aid)
    assert row["checkin_today"] == 1
    assert row["checkin_credit"] == 200


def test_checkin_account_claims_when_upstream_says_not_checked(isolated_db, monkeypatch):
    """即使本地标记已签，只要上游说未签，单号签到必须真正领取。"""
    db = isolated_db
    aid = db.add_account("trae", "t1", uid="u1")
    db.update_account(aid, checkin_today=1, checkin_last_date="2026-09-01")
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(db.get_account(aid)))
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda p: True)
    statuses = iter([
        {"today_checked_in": False, "today_credit": 200, "streak_days": 0},
        {"today_checked_in": True, "today_credit": 200, "streak_days": 1,
         "last_checkin_date": "2026-09-01"},
    ])
    monkeypatch.setattr(proxy.P, "fetch_checkin_status", lambda *a, **k: next(statuses))
    claimed = []
    monkeypatch.setattr(proxy.P, "do_checkin", lambda *a, **k: (
        claimed.append(True) or {"claimed_credit": 200, "streak_days": 1}
    ))
    monkeypatch.setattr(proxy, "refresh_quota", lambda row: 0)
    res = proxy.checkin_account(db.get_account(aid))
    assert claimed == [True]
    assert res["claimed_credit"] == 200
    assert db.get_account(aid)["checkin_today"] == 1


def test_model_route_migrates_retired_platforms_before_disabled_filter(isolated_db, monkeypatch):
    """移除 CodeBuddy CN 后，旧注册表引用迁入 WorkBuddy 且继续受全局开关约束。"""
    db = isolated_db
    db.set_model_registry({
        "legacy-model": {"type": "chat", "platforms": ["workbuddy", "codebuddy_cn", "codebuddy"]},
        "ghost-model": {"type": "chat", "platforms": ["codebuddy_cn"]},
    })
    db.set_setting("model_platform_disabled", json.dumps({
        "workbuddy": ["legacy-model"],
        "codebuddy": ["legacy-model"],
    }))

    assert proxy.model_route("legacy-model")[1] == []
    assert proxy.model_route("ghost-model")[1] == ["workbuddy"]
    assert "legacy-model" not in proxy.list_models()
    assert "ghost-model" in proxy.list_models()


def test_model_route_priority_resolves_display_name_alias_to_model_id(isolated_db):
    """历史配置里填了展示名时，路由仍要按对应模型 ID 应用优先级。"""
    db = isolated_db
    db.set_model_registry({
        "hy4-preview-x": {"type": "chat", "platforms": ["workbuddy", "trae", "codebuddy"]},
    })
    db.set_setting("model_route_priority", json.dumps({
        "default": ["workbuddy", "trae", "codebuddy"],
        "overrides": {"Hy4 Preview X": ["codebuddy", "trae", "workbuddy"]},
    }))

    assert proxy.model_route("hy4-preview-x")[1] == ["codebuddy", "trae", "workbuddy"]


def test_default_model_skips_models_disabled_on_all_channels(isolated_db):
    """默认模型不能选中已被三个渠道全部关闭的模型。"""
    db = isolated_db
    db.set_model_registry({
        "aaa-disabled": {"type": "chat", "platforms": ["workbuddy", "trae", "codebuddy"]},
        "zzz-open": {"type": "chat", "platforms": ["trae"]},
    })
    db.set_setting("default_model", "aaa-disabled")
    db.set_setting("model_platform_disabled", json.dumps({
        "workbuddy": ["aaa-disabled"],
        "trae": ["aaa-disabled"],
        "codebuddy": ["aaa-disabled"],
    }))

    assert proxy.default_model() == "zzz-open"


def test_retry_failed_checkins_rechecks_upstream_instead_of_local_skip(isolated_db, monkeypatch):
    """补签到不能用本地今日标记短路，必须让统一签到链重新核验上游。"""
    db = isolated_db
    aid = db.add_account("trae", "t1", uid="u1")
    db.update_account(aid, checkin_today=1, checkin_last_date="2026-09-01",
                      checkin_updated_at=1788220800)
    db.set_setting("auto_checkin", "1")
    db.set_setting("_last_checkin_retry_ts", "0")
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda p: True)
    called = []
    monkeypatch.setattr(proxy, "checkin_account", lambda row: called.append(row["id"]) or {"ok": True})

    proxy.retry_failed_checkins()
    assert called == [aid]


def test_refresh_quota_marks_account_error_without_erasing_previous_snapshot(isolated_db, monkeypatch):
    """额度刷新失败要标记错误，并保留上次成功额度用于追溯。"""
    db = isolated_db
    aid = db.add_account("workbuddy", "tok", uid="u1")
    db.update_account(aid, quota_total=100, quota_used=20, quota_remain=80,
                      quota_updated_at=1788220800)
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(row))
    monkeypatch.setattr(proxy.P, "collect_quota", lambda *a, **k: {
        "errors": ["user-resource: timeout"], "total": 0, "used": 0, "remain": 0,
    })

    with pytest.raises(proxy.ProxyError, match="额度刷新失败"):
        proxy.refresh_quota(db.get_account(aid))
    row = db.get_account(aid)
    assert row["status"] == "error"
    assert "额度刷新失败" in row["status_reason"]
    assert row["quota_remain"] == 80


def test_bound_key_cannot_route_model_outside_bound_platform(isolated_db):
    """显式绑定平台的 Key 不能把该平台没有的模型硬发到上游。"""
    db = isolated_db
    db.set_model_registry({
        "trae-only": {"type": "chat", "platforms": ["trae"]},
    })
    assert proxy.resolve_platform_candidates("trae-only", "workbuddy") == []


def test_bound_key_unknown_model_is_not_blindly_forwarded(isolated_db, monkeypatch):
    """注册表和实时探测都没有确认时，绑定 Key 也不能绕过模型门禁。"""
    db = isolated_db
    monkeypatch.setattr(proxy, "_live_probe_model", lambda _mid: None)
    monkeypatch.setattr(proxy, "_mark_probe_failed", lambda _mid: None)
    assert proxy.resolve_platform_candidates("unknown-model", "trae") == []


def test_refresh_quota_trae_uses_checkin_device_resolver(isolated_db, monkeypatch):
    """Trae UG 额度接口与签到一样使用可工作的账号设备标识，而非孤立凭证 device_id。"""
    db = isolated_db
    aid = db.add_account("trae", "t1", uid="u1", device_id="orphan-device")
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(row))
    captured = {}

    def collect(*args, **kwargs):
        captured.update(kwargs)
        return {"plan_type": "credits", "total": 100, "used": 1,
                "remain": 99, "unit": "credits", "packages": [], "errors": []}

    monkeypatch.setattr(proxy.P, "collect_quota", collect)
    proxy.refresh_quota(db.get_account(aid))
    assert captured["device_id"] == "u1"


def test_concurrent_checkin_requests_claim_once(isolated_db, monkeypatch):
    """手动签到与自动签到并发时，同一账号只能有一个领取请求。"""
    import threading
    import time

    db = isolated_db
    aid = db.add_account("trae", "t1", uid="u1")
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(db.get_account(aid)))
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda p: True)
    monkeypatch.setattr(proxy, "refresh_quota", lambda row: {})

    state = {"checked": False, "claims": 0}
    state_lock = threading.Lock()
    claim_entered = threading.Event()
    release_claim = threading.Event()

    def status(*args, **kwargs):
        with state_lock:
            checked = state["checked"]
        return {"today_checked_in": checked, "today_credit": 200,
                "streak_days": 0}

    def claim(*args, **kwargs):
        with state_lock:
            state["claims"] += 1
        claim_entered.set()
        release_claim.wait(1)
        with state_lock:
            state["checked"] = True
        return {"claimed_credit": 200, "streak_days": 1}

    monkeypatch.setattr(proxy.P, "fetch_checkin_status", status)
    monkeypatch.setattr(proxy.P, "do_checkin", claim)

    first = threading.Thread(target=proxy.checkin_account, args=(db.get_account(aid),))
    first.start()
    assert claim_entered.wait(1)
    second = threading.Thread(target=proxy.checkin_account, args=(db.get_account(aid),))
    second.start()
    time.sleep(0.05)
    release_claim.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert state["claims"] == 1


def test_stream_chat_tries_next_account_when_model_is_unavailable_on_first_account(isolated_db, monkeypatch):
    """同一渠道的账号套餐可能不同，首个账号没有模型时必须继续试同渠道其它账号。"""
    db = isolated_db
    first = db.add_account("workbuddy", "tok-1", uid="u1")
    second = db.add_account("workbuddy", "tok-2", uid="u2")
    db.set_model_registry({"shared-model": {"type": "chat", "platforms": ["workbuddy"]}})
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(row))

    class Resp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

        def close(self):
            pass

    responses = [Resp(400, '{"code":11102,"msg":"model not found"}'), Resp(200)]
    calls = []

    def open_upstream(acc, body, timeout=300):
        calls.append(acc["id"])
        return responses.pop(0)

    monkeypatch.setattr(proxy, "open_upstream", open_upstream)
    acc, resp = proxy.stream_chat(
        {"model": "shared-model", "messages": [{"role": "user", "content": "hi"}]},
        max_retry=3,
    )
    assert calls == [first, second]
    assert acc["id"] == second
    resp.close()


def test_disabled_status_account_is_not_selected(isolated_db):
    """status=disabled 的账号即使 enabled=1 也不能进入实际调度。"""
    db = isolated_db
    aid = db.add_account("workbuddy", "tok", uid="u1")
    db.update_account(aid, status="disabled")
    assert db.pick_account("workbuddy") is None


def test_stream_chat_retries_same_account_after_401_refresh(isolated_db, monkeypatch):
    """单账号 401 后强制刷新必须重试该账号，不能因排除表直接返回失败。"""
    db = isolated_db
    aid = db.add_account("workbuddy", "old-token", refresh_token="refresh", uid="u1")
    db.set_model_registry({"model": {"type": "chat", "platforms": ["workbuddy"]}})

    def ensure(row, force=False):
        if force:
            db.update_account(aid, access_token="fresh-token")
            return dict(db.get_account(aid))
        return dict(row)

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.text = "unauthorized" if status == 401 else ""

        def close(self):
            pass

    responses = [Resp(401), Resp(200)]
    calls = []

    monkeypatch.setattr(proxy, "ensure_token", ensure)
    monkeypatch.setattr(proxy, "open_upstream", lambda acc, body, timeout=300: (
        calls.append(acc["access_token"]) or responses.pop(0)
    ))

    acc, resp = proxy.stream_chat(
        {"model": "model", "messages": [{"role": "user", "content": "hi"}]},
        max_retry=2,
    )
    assert calls == ["old-token", "fresh-token"]
    assert acc["id"] == aid
    resp.close()


def test_stream_chat_default_budget_uses_entire_channel_pool(isolated_db, monkeypatch):
    """默认调用必须遍历渠道内全部可用账号，不能固定只试前三个。"""
    db = isolated_db
    account_ids = [db.add_account("workbuddy", f"tok-{i}", uid=f"u{i}") for i in range(4)]
    db.set_model_registry({"pool-model": {"type": "chat", "platforms": ["workbuddy"]}})
    monkeypatch.setattr(proxy, "ensure_token", lambda row, force=False: dict(row))

    class Resp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

        def close(self):
            pass

    responses = [
        Resp(400, '{"code":11102,"msg":"model not found"}'),
        Resp(400, '{"code":11102,"msg":"model not found"}'),
        Resp(400, '{"code":11102,"msg":"model not found"}'),
        Resp(200),
    ]
    calls = []
    monkeypatch.setattr(proxy, "open_upstream", lambda acc, body, timeout=300: (
        calls.append(acc["id"]) or responses.pop(0)
    ))

    acc, resp = proxy.stream_chat({
        "model": "pool-model", "messages": [{"role": "user", "content": "hi"}],
    })
    assert calls == account_ids
    assert acc["id"] == account_ids[-1]
    resp.close()


def test_stream_chat_retries_when_http_200_sse_contains_business_error(isolated_db, monkeypatch):
    """首个账号用 HTTP 200 包装业务错误时，路由层必须换号，不能把错误流当成功返回。"""
    db = isolated_db
    first = db.add_account("workbuddy", "tok-1", uid="u1")
    second = db.add_account("workbuddy", "tok-2", uid="u2")
    db.set_model_registry({"shared-model": {"type": "chat", "platforms": ["workbuddy"]}})
    monkeypatch.setattr(proxy, "ensure_token", lambda row, force=False: dict(row))

    class Resp:
        status_code = 200
        headers = {"Content-Type": "text/event-stream"}
        text = ""

        def __init__(self, lines):
            self.lines = lines
            self.closed = False

        def iter_lines(self, decode_unicode=False):
            yield from self.lines

        def close(self):
            self.closed = True

    bad = Resp([b'data: {"code":11102,"msg":"model not found"}', b""])
    good = Resp([b'data: {"choices":[{"delta":{"content":"ok"}}]}', b"", b"data: [DONE]", b""])
    responses = [bad, good]
    calls = []
    monkeypatch.setattr(proxy, "open_upstream", lambda acc, body, timeout=300: (
        calls.append(acc["id"]) or responses.pop(0)
    ))

    acc, resp = proxy.stream_chat({
        "model": "shared-model", "messages": [{"role": "user", "content": "hi"}],
    })
    assert calls == [first, second]
    assert bad.closed is True
    assert acc["id"] == second
    assert "ok" in proxy.aggregate_stream(resp, "shared-model")["choices"][0]["message"]["content"]


def test_generate_image_respects_bound_key_platform_and_model_switch(isolated_db, monkeypatch):
    """图像调用也必须服从 Key 渠道绑定与全局模型开关，不能偷跑其它号池。"""
    db = isolated_db
    db.add_account("workbuddy", "tok-w", uid="w1")
    db.set_model_registry({
        "image-model": {"type": "image", "platforms": ["workbuddy", "codebuddy"]},
    })
    db.set_setting("model_platform_disabled", json.dumps({"codebuddy": ["image-model"]}))
    monkeypatch.setattr(proxy, "_live_probe_model", lambda _mid: None)
    called = []
    monkeypatch.setattr(proxy, "open_image_upstream", lambda *a, **k: called.append(True))

    with pytest.raises(proxy.ProxyError, match="未发现|绑定平台|候选平台"):
        proxy.generate_image({"model": "image-model", "prompt": "x"}, platform="codebuddy")
    assert called == []


def test_checkin_account_rejects_unconfirmed_claim(isolated_db, monkeypatch):
    """领取接口与回读状态都明确未签到时，不能写成本地成功。"""
    db = isolated_db
    aid = db.add_account("workbuddy", "tok", uid="u1")
    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(db.get_account(aid)))
    monkeypatch.setattr(proxy.P, "supports_checkin", lambda _platform: True)
    statuses = iter([
        {"today_checked_in": False, "today_credit": 0, "streak_days": 0},
        {"today_checked_in": False, "today_credit": 0, "streak_days": 0},
    ])
    monkeypatch.setattr(proxy.P, "fetch_checkin_status", lambda *a, **k: next(statuses))
    monkeypatch.setattr(proxy.P, "do_checkin", lambda *a, **k: {
        "ok": True, "today_checked_in": False, "claimed_credit": 0,
        "streak_days": 0, "msg": "not confirmed",
    })
    monkeypatch.setattr(proxy, "refresh_quota", lambda _row: {})

    with pytest.raises(proxy.ProxyError, match="未确认签到成功"):
        proxy.checkin_account(db.get_account(aid))
    row = db.get_account(aid)
    assert row["checkin_today"] == 0


def test_stream_chat_retries_when_trae_returns_done_only_stream(isolated_db, monkeypatch):
    """Trae 只返回 done、没有 output 时属于空流，必须换下一个账号。"""
    db = isolated_db
    first = db.add_account("trae", "tok-1", uid="u1")
    second = db.add_account("trae", "tok-2", uid="u2")
    db.set_model_registry({"trae-model": {"type": "chat", "platforms": ["trae"]}})
    monkeypatch.setattr(proxy, "ensure_token", lambda row, force=False: dict(row))

    class Resp:
        status_code = 200
        text = ""

        def __init__(self, lines):
            self.lines = lines
            self.closed = False

        def iter_lines(self, decode_unicode=False):
            yield from self.lines

        def close(self):
            self.closed = True

    empty = Resp([b"event: done", b'data: {"finish_reason":"stop"}', b""])
    good = Resp([
        b"event: output", b'data: {"response":"ok"}', b"",
        b"event: done", b'data: {"finish_reason":"stop"}', b"",
    ])
    upstream = [empty, good]
    calls = []
    monkeypatch.setattr(proxy, "open_upstream", lambda acc, body, timeout=300: (
        calls.append(acc["id"]) or upstream.pop(0)
    ))

    acc, resp = proxy.stream_chat({
        "model": "trae-model", "messages": [{"role": "user", "content": "hi"}],
    })
    assert calls == [first, second]
    assert empty.closed is True
    assert acc["id"] == second
    assert proxy.aggregate_trae_stream(resp, "trae-model")["choices"][0]["message"]["content"] == "ok"


def test_generate_image_retries_http_200_business_error(isolated_db, monkeypatch):
    """图像上游用 HTTP 200 包装业务错误时，必须换号而不是返回假成功。"""
    db = isolated_db
    first = db.add_account("workbuddy", "tok-1", uid="u1")
    second = db.add_account("workbuddy", "tok-2", uid="u2")
    db.set_model_registry({"image-model": {"type": "image", "platforms": ["workbuddy"]}})
    monkeypatch.setattr(proxy, "ensure_token", lambda row, force=False: dict(row))

    class Resp:
        status_code = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, body):
            self.body = body
            self.text = json.dumps(body)
            self.closed = False

        def json(self):
            return self.body

        def close(self):
            self.closed = True

    bad = Resp({"code": 11102, "msg": "model not found"})
    good = Resp({"code": 0, "data": {"data": [{"url": "https://example.test/image.png"}]}})
    upstream = [bad, good]
    calls = []
    monkeypatch.setattr(proxy, "open_image_upstream", lambda acc, body, timeout=120: (
        calls.append(acc["id"]) or upstream.pop(0)
    ))

    acc, resp = proxy.generate_image({"model": "image-model", "prompt": "x"})
    assert calls == [first, second]
    assert bad.closed is True
    assert acc["id"] == second
    assert resp.json()["code"] == 0
