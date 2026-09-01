"""discovery seam：模型注册表不得丢失官方目录收录的平台标记。"""
import pytest


def test_run_discovery_preserves_trae_catalog_entries(isolated_db, monkeypatch):
    """官方目录已收录 trae 后，run_discovery 重建注册表时必须保留 trae 标记。"""
    db = isolated_db
    db.set_model_registry({
        "glm-5.2": {"type": "chat", "platforms": ["workbuddy", "trae"]},
        "kimi-k3": {"type": "chat", "platforms": ["trae"]},
    })
    import discovery

    # 只让 workbuddy 探测命中 glm-5.2；Trae 不参与逐项探测
    def fake_probe(platform, mid, acc=None):
        return (platform == "workbuddy" and mid == "glm-5.2"), "chat", {}
    monkeypatch.setattr(discovery, "probe_model_on_platform", fake_probe)
    monkeypatch.setattr(discovery, "_start_image_discovery", lambda: None)
    monkeypatch.setattr(discovery, "_account_for", lambda p: {"access_token": "t"})

    out = discovery.run_discovery()
    assert out["glm-5.2"]["platforms"] == ["workbuddy", "trae"]
    assert out["kimi-k3"]["platforms"] == ["trae"]


def test_codebuddy_catalog_includes_config_fallback_models(isolated_db, monkeypatch):
    """国际站 console/models 500 时，/v3/config 的 CLI 目录必须合并进注册表。

    回归：glm-5.3/glm-5.2/kimi-k3/kimi-k2.6/minimax-m3 曾因回退断链而漏收。
    """
    import providers as P
    catalog = ["default-model", "glm-5.3", "kimi-k3", "minimax-m3"]
    monkeypatch.setattr(P, "fetch_upstream_models", lambda *a, **k: [])
    monkeypatch.setattr(P, "_fetch_config_models", lambda *a, **k: catalog)
    added = P.merge_upstream_models("codebuddy", "tok")
    assert added == catalog
    reg = isolated_db.get_model_registry()
    for mid in catalog:
        assert "codebuddy" in reg[mid]["platforms"]


def test_run_discovery_prunes_retired_platform_references(isolated_db, monkeypatch):
    """刷新模型目录时不能把历史 codebuddy_cn 平台引用重新写回数据库。"""
    db = isolated_db
    db.set_model_registry({
        "same-model": {"type": "chat", "platforms": ["workbuddy", "codebuddy_cn"]},
    })
    import discovery

    monkeypatch.setattr(discovery, "probe_model_on_platform",
                        lambda platform, mid, acc=None: (False, None, "not found"))
    monkeypatch.setattr(discovery, "_start_image_discovery", lambda: None)
    monkeypatch.setattr(discovery, "_account_for", lambda p: {"access_token": "t"})

    discovery.run_discovery()
    assert db.get_model_registry() == {
        "same-model": {"type": "chat", "platforms": ["workbuddy"]},
    }


def test_merge_upstream_models_removes_missing_chat_models_but_keeps_image_models(isolated_db, monkeypatch):
    """官方聊天目录刷新后，已下线聊天模型移除；独立图像目录不受影响。"""
    import providers as P

    db = isolated_db
    db.set_model_registry({
        "old-chat": {"type": "chat", "platforms": ["workbuddy"]},
        "old-image": {"type": "image", "platforms": ["workbuddy"]},
    })
    monkeypatch.setattr(P, "fetch_upstream_models", lambda *a, **k: ["new-chat"])

    P.merge_upstream_models("workbuddy", "tok")
    reg = db.get_model_registry()
    assert "old-chat" not in reg
    assert reg["old-image"]["platforms"] == ["workbuddy"]
    assert reg["new-chat"]["platforms"] == ["workbuddy"]


def test_sync_upstream_catalogs_unions_models_across_accounts(isolated_db, monkeypatch):
    """目录刷新应合并同一渠道不同账号的模型，不能被第一个账号的目录代表。"""
    db = isolated_db
    db.add_account("workbuddy", "tok-a", uid="a")
    db.add_account("workbuddy", "tok-b", uid="b")

    import discovery
    import proxy
    import providers as P

    monkeypatch.setattr(proxy, "ensure_token", lambda row: dict(row))
    monkeypatch.setattr(discovery, "_account_for", lambda platform: None)
    monkeypatch.setattr(P, "fetch_upstream_models", lambda platform, token, **kwargs: (
        ["model-a"] if token == "tok-a" else ["model-b"]
    ))

    out = discovery.sync_upstream_catalogs()
    assert out["workbuddy"] == ["model-a", "model-b"]
    reg = db.get_model_registry()
    assert reg["model-a"]["platforms"] == ["workbuddy"]
    assert reg["model-b"]["platforms"] == ["workbuddy"]


def test_probe_rejects_http_200_sse_business_error(isolated_db, monkeypatch):
    """模型探测不能只看 HTTP 200，首帧业务错误应判为不可用。"""
    import discovery

    class Resp:
        status_code = 200

        def iter_lines(self, decode_unicode=False):
            return iter([
                b'data: {"code":11102,"msg":"model not found"}',
                b"",
                b"data: [DONE]",
            ])

        @property
        def text(self):
            return '{"code":11102,"msg":"model not found"}'

        def close(self):
            pass

    monkeypatch.setattr(discovery.proxy, "open_upstream", lambda *a, **k: Resp())
    ok, info = discovery._probe_endpoint(
        {"id": 1, "platform": "workbuddy", "access_token": "tok"},
        "workbuddy", "missing-model", "chat"
    )
    assert ok is False
    assert "11102" in info


def test_probe_rejects_empty_or_done_only_http_200_streams(isolated_db):
    """HTTP 200 但没有 choices/usage 的空流不能登记成可用模型。"""
    import discovery

    class Resp:
        def __init__(self, lines):
            self._lines = lines

        def iter_lines(self, decode_unicode=False):
            yield from self._lines

    for lines in ([], [b"data: [DONE]", b""]):
        ok, info = discovery._probe_stream_evidence(Resp(lines), 200)
        assert ok is False
        assert "empty" in info.lower()


def test_concurrent_catalog_and_image_registry_updates_do_not_lose_models(isolated_db):
    """官方目录与后台生图探测并发写注册表时，两个模型都必须保留。"""
    import threading
    import discovery
    import providers as P

    db = isolated_db
    for i in range(20):
        db.set_model_registry({})
        t1 = threading.Thread(target=P.merge_upstream_model_ids,
                              args=("workbuddy", ["chat-model"]))
        t2 = threading.Thread(target=discovery._merge_image_model,
                              args=("image-model", "workbuddy"))
        t1.start(); t2.start(); t1.join(3); t2.join(3)
        assert not t1.is_alive() and not t2.is_alive()
        assert set(db.get_model_registry()) == {"chat-model", "image-model"}, i
