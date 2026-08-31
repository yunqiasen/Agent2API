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
