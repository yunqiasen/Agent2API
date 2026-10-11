"""调用密钥的模型范围：目录读取、持久化校验与公共入口共用。"""
from __future__ import annotations

import json

import providers as P
import proxy


def normalize_models(models) -> list[str]:
    if not isinstance(models, list) or any(
            not isinstance(mid, str) or not mid.strip() for mid in models):
        raise ValueError("allowed_models 必须是非空模型 ID 组成的数组")
    return list(dict.fromkeys(mid.strip() for mid in models))


def _normalize_scope(scope, models) -> tuple[str, list[str]]:
    if scope not in ("all", "selected"):
        raise ValueError("model_scope 仅支持 all 或 selected")
    models = normalize_models(models)
    if scope == "all":
        if models:
            raise ValueError("不限模型时 allowed_models 应为空数组")
        return scope, []
    if not models:
        raise ValueError("指定模型至少选择 1 个当前开启的模型")
    return scope, models


def validate(scope, models, platform: str) -> tuple[str, list[str]]:
    scope, models = _normalize_scope(scope, models)
    if scope == "all":
        return scope, models
    available = set(proxy.list_models(platform=platform or None))
    invalid = [mid for mid in models if mid not in available]
    if invalid:
        raise ValueError("以下模型在所选渠道未开启或未收录：" + "、".join(invalid))
    return scope, models


def serialize(row) -> dict:
    item = dict(row)
    models = []
    error = ""
    try:
        _, models = _normalize_scope(
            item.get("model_scope"), json.loads(item.get("allowed_models")))
    except (ValueError, TypeError) as e:
        error = "模型权限配置异常：%s；请编辑此 Key 重新设置模型范围" % e
    item["allowed_models"] = models
    item["model_scope_error"] = error
    return item


def visible_models(row) -> list[str]:
    config = serialize(row)
    if config["model_scope_error"]:
        return []
    models = proxy.list_models(platform=row["platform"] or None)
    if config["model_scope"] == "selected":
        allowed = set(config["allowed_models"])
        return [mid for mid in models if mid in allowed]
    return models


def prepare_edit(data: dict, current: dict) -> dict:
    """省略字段保持原值；显式切到all才清掉旧名单。"""
    if not {"platform", "model_scope", "allowed_models"}.intersection(data):
        return {}
    platform = data.get("platform", current["platform"])
    if not isinstance(platform, str):
        raise ValueError("platform 必须是字符串")
    platform = platform.strip()
    if platform and platform not in P.PLATFORMS:
        raise ValueError("未知平台")
    scope = data.get("model_scope", current["model_scope"])
    if "allowed_models" in data:
        models = data["allowed_models"]
    elif data.get("model_scope") == "all":
        models = []
    else:
        saved = serialize(current)
        if saved["model_scope_error"]:
            raise ValueError(saved["model_scope_error"])
        models = saved["allowed_models"]
    scope, models = validate(scope, models, platform)
    return {"platform": platform, "model_scope": scope, "allowed_models": models}


def denial(row, model: str | None) -> str:
    """仅查本地目录；指定范围之外不进入实时模型探测。"""
    config = serialize(row)
    if config["model_scope_error"]:
        return config["model_scope_error"]
    if config["model_scope"] == "selected" and model:
        if model not in config["allowed_models"] or model not in visible_models(row):
            return "API Key 未允许模型 %s，或该模型在绑定渠道已关闭/未收录" % model
    return ""
