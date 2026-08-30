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
