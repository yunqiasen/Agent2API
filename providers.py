# -*- coding: utf-8 -*-
"""
hy3api - 平台适配层

支持三个平台的账号、令牌、额度与模型管理：
  - workbuddy  WorkBuddy      (www.codebuddy.cn, platform=workbuddy)
  - trae       TraeWork       (www.trae.cn, SOLO Cloud IDE)
  - codebuddy  CodeBuddy 国际 (www.codebuddy.ai, platform=ide)

流程与常量来自 cockpit-tools 源码（crates/cockpit-core/src/modules/*_oauth.rs），
对话接口来自本地客户端日志实测。
"""
from __future__ import annotations

import datetime as _dt
import json
import time
import uuid

import db
import requests
import trae

# --------------------------------------------------------------------------
# 平台定义
# --------------------------------------------------------------------------

PLATFORMS = {
    "workbuddy": {
        "key": "workbuddy",
        "label": "WorkBuddy",
        "api_base": "https://www.codebuddy.cn",
        "prefix": "/v2/plugin",
        "platform": "workbuddy",
        "model_base": "https://copilot.tencent.com",
        "chat_path": "/v2/chat/completions",
        "config_path": "/v3/config",
        "session_header": "X-WorkBuddy-Session-Id",
        "supports_checkin": True,
    },
    # 2026-08-29 实测：WorkBuddy 与 CodeBuddy CN 同账号、同额度、同模型，
    # 拆两个平台只会重复扣同一池子，因此移除 codebuddy_cn，仅保留 WorkBuddy。
    "trae": {
        "key": "trae",
        "label": "TraeWork",
        "api_base": "https://www.trae.cn",
        "prefix": "",
        "platform": "trae",
        "model_base": "https://trae-api-cn.mchost.guru",
        "chat_path": "/api/agent/v3/llm_utils_chat",
        "config_path": "/api/ide/v1/get_detail_param",
        "session_header": "X-Trae-Session-Id",
        # Trae 三类请求头与 WorkBuddy 完全不同，见 trae.py
        "provider": "trae",
        "supports_checkin": True,
    },
    "codebuddy": {
        "key": "codebuddy",
        "label": "CodeBuddy",
        "api_base": "https://www.codebuddy.ai",
        "prefix": "/v2/plugin",
        "platform": "ide",
        "model_base": "https://www.codebuddy.ai",
        "chat_path": "/v2/chat/completions",
        "config_path": "/v3/config",
        "session_header": "X-CodeBuddy-Session-Id",
        # 实测：国际站要求 messages[0].role 必须是 system，否则 400 Parse message failed
        "require_leading_system": True,
        "supports_checkin": False,
    },
}

DEFAULT_PRODUCT_CODE = "p_tcaca"
DEFAULT_STATUS = [0, 3]
OAUTH_TIMEOUT_SECONDS = 600
OAUTH_POLL_INTERVAL = 1.5

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ProviderError(Exception):
    pass


def _is_trae(platform_key: str) -> bool:
    """是否 Trae 渠道。"""
    return PLATFORMS.get(platform_key, {}).get("provider") == "trae"


def get_platform(key: str) -> dict:
    p = PLATFORMS.get(key)
    if not p:
        raise ProviderError("未知平台: %s" % key)
    return p


PLATFORM_NOTES = {
    "workbuddy": "国内网关 copilot.tencent.com；原 CodeBuddy CN 与其同额度同模型，已合并",
    "trae": "Trae SOLO Cloud IDE 网关，账号/额度独立",
    "codebuddy": "国际站，独立账号与独立网关",
}


def platform_list() -> list:
    return [
        {"key": v["key"], "label": v["label"], "name": v["label"],
         "api_base": v["api_base"], "platform": v["platform"],
         "model_base": v["model_base"], "note": PLATFORM_NOTES.get(v["key"], ""),
         "models": catalog_entries(v["key"])}
        for v in PLATFORMS.values()
    ]


def _session() -> requests.Session:
    """带重试的会话。

    实测 www.codebuddy.cn / copilot.tencent.com 偶发 SSLEOFError 与连接重置，
    额度、刷新这类幂等调用挂上重试后成功率明显提升。
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=3, connect=3, read=2, backoff_factor=0.6,
                      status_forcelist=(500, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "POST"]))
        ad = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        s.mount("https://", ad)
        s.mount("http://", ad)
    except Exception:
        pass
    return s


def _ok(body: dict) -> bool:
    code = body.get("code")
    return code in (0, 200, None)


def _msg(body: dict) -> str:
    return str(body.get("message") or body.get("msg") or body.get("error_msg") or "unknown error")


# --------------------------------------------------------------------------
# 步骤 1：申请 state，得到授权 URL
# --------------------------------------------------------------------------

def start_login(platform_key: str, timeout: int = 20) -> dict:
    """POST {api_base}{prefix}/auth/state?platform={platform} -> state + authUrl"""
    p = get_platform(platform_key)
    if _is_trae(platform_key):
        machine_id = uuid.uuid4().hex + uuid.uuid4().hex[:16]
        device_id = uuid.uuid4().hex + uuid.uuid4().hex[:16]
        return {
            "platform": platform_key,
            "state": uuid.uuid4().hex,
            "auth_url": trae.build_login_url(machine_id, device_id),
            "login_id": uuid.uuid4().hex,
            "expires_in": OAUTH_TIMEOUT_SECONDS,
            "interval": OAUTH_POLL_INTERVAL,
            "created_at": int(time.time()),
            "trae_machine_id": machine_id,
            "trae_device_id": device_id,
            "auth_mode": "callback",
        }
    url = "%s%s/auth/state?platform=%s" % (p["api_base"], p["prefix"], p["platform"])
    try:
        r = _session().post(url, json={}, timeout=timeout)
        body = r.json()
    except Exception as e:
        raise ProviderError("请求 auth/state 失败: %s" % e)

    data = body.get("data")
    if not data:
        raise ProviderError("auth/state 响应缺少 data: %s" % json.dumps(body)[:200])

    state = data.get("state")
    if not state:
        raise ProviderError("auth/state 响应缺少 state")

    auth_url = data.get("authUrl") or data.get("auth_url") or data.get("url")
    if not auth_url:
        auth_url = "%s/login?platform=%s&state=%s" % (p["api_base"], p["platform"], state)

    return {
        "platform": platform_key,
        "state": state,
        "auth_url": auth_url,
        "login_id": uuid.uuid4().hex,
        "expires_in": OAUTH_TIMEOUT_SECONDS,
        "interval": OAUTH_POLL_INTERVAL,
        "created_at": int(time.time()),
    }


# --------------------------------------------------------------------------
# 步骤 2：轮询 token
# --------------------------------------------------------------------------

def poll_token(platform_key: str, state: str, timeout: int = 20) -> dict | None:
    """GET {api_base}{prefix}/auth/token?state=...

    返回 None 表示尚未授权（code=11217 login ing...），成功返回 token 字典。
    """
    # Trae 没有 /auth/token 轮询端点（网页授权只能回调导入），直接返回等待中，
    # 避免对错误端点轮询抛出 "Expecting value" 误导报错。
    if _is_trae(platform_key):
        return None
    p = get_platform(platform_key)
    url = "%s%s/auth/token?state=%s" % (p["api_base"], p["prefix"], state)
    try:
        r = _session().get(url, timeout=timeout)
        body = r.json()
    except Exception as e:
        raise ProviderError("轮询 token 失败: %s" % e)

    code = body.get("code", -1)
    if code not in (0, 200):
        # 11217 = 尚在登录中
        return None

    data = body.get("data") or {}
    access = data.get("accessToken") or data.get("access_token") or ""
    if not access:
        return None

    return {
        "access_token": access,
        "refresh_token": data.get("refreshToken") or data.get("refresh_token"),
        "expires_at": data.get("expiresAt") or data.get("expires_at"),
        "domain": data.get("domain"),
        "token_type": data.get("tokenType") or data.get("token_type") or "Bearer",
        "raw": data,
    }


# --------------------------------------------------------------------------
# 步骤 3：拉取账号信息
# --------------------------------------------------------------------------

def fetch_account_info(platform_key: str, access_token: str, state: str | None = None,
                       domain: str | None = None, timeout: int = 20) -> dict:
    if _is_trae(platform_key):
        return trae.get_user_info(access_token, timeout=timeout)
    """GET {prefix}/login/account?state=...  或退化到 {prefix}/accounts"""
    p = get_platform(platform_key)
    headers = {"Authorization": "Bearer %s" % access_token}
    if domain:
        headers["X-Domain"] = domain

    data = {}
    if state:
        url = "%s%s/login/account?state=%s" % (p["api_base"], p["prefix"], state)
        try:
            r = _session().get(url, headers=headers, timeout=timeout)
            data = (r.json() or {}).get("data") or {}
        except Exception:
            data = {}

    if not data:
        # 退化路径：GET {prefix}/accounts 取 lastLogin 的账号
        url = "%s%s/accounts" % (p["api_base"], p["prefix"])
        try:
            r = _session().get(url, headers=headers, timeout=timeout)
            body = r.json() or {}
            arr = ((body.get("data") or {}).get("accounts")) or []
            if arr:
                data = next((a for a in arr if a.get("lastLogin")), arr[0])
        except Exception:
            data = {}

    return {
        "uid": data.get("uid") or "",
        "nickname": data.get("nickname") or "",
        "email": data.get("email") or "",
        "enterprise_id": data.get("enterpriseId") or "",
        "enterprise_name": data.get("enterpriseName") or "",
        "raw": data,
    }


# --------------------------------------------------------------------------
# 令牌刷新（refresh 通过 X-Refresh-Token 头传递）
# --------------------------------------------------------------------------

def refresh_token(platform_key: str, access_token: str, refresh_tok: str,
                  domain: str | None = None, timeout: int = 20) -> dict:
    if _is_trae(platform_key):
        return trae.exchange_token(refresh_tok, timeout=timeout)
    p = get_platform(platform_key)
    url = "%s%s/auth/token/refresh" % (p["api_base"], p["prefix"])
    headers = {
        "Authorization": "Bearer %s" % access_token,
        "X-Refresh-Token": refresh_tok,
        "Content-Type": "application/json",
    }
    if domain:
        headers["X-Domain"] = domain

    try:
        r = _session().post(url, headers=headers, json={}, timeout=timeout)
        body = r.json()
    except Exception as e:
        raise ProviderError("刷新 token 失败: %s" % e)

    if not _ok(body):
        raise ProviderError("刷新 token 失败 (code=%s): %s" % (body.get("code"), _msg(body)))

    data = body.get("data")
    if not data:
        raise ProviderError("刷新响应缺少 data")

    return {
        "access_token": data.get("accessToken") or data.get("access_token") or access_token,
        "refresh_token": data.get("refreshToken") or data.get("refresh_token") or refresh_tok,
        "expires_at": data.get("expiresAt") or data.get("expires_at"),
        "domain": data.get("domain") or domain,
        "raw": data,
    }


# --------------------------------------------------------------------------
# 套餐 / 额度
# --------------------------------------------------------------------------

def _billing_headers(access_token, uid=None, enterprise_id=None, domain=None):
    h = {
        "Authorization": "Bearer %s" % access_token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if uid:
        h["X-User-Id"] = uid
    if enterprise_id:
        h["X-Enterprise-Id"] = enterprise_id
        h["X-Tenant-Id"] = enterprise_id
    if domain:
        h["X-Domain"] = domain
    return h


def _default_time_range():
    now = _dt.datetime.now()
    begin = now.strftime("%Y-%m-%d %H:%M:%S")
    end = (now + _dt.timedelta(days=3650)).strftime("%Y-%m-%d %H:%M:%S")
    return begin, end


def fetch_user_resource(platform_key, access_token, uid=None, enterprise_id=None,
                        domain=None, product_code=DEFAULT_PRODUCT_CODE,
                        status=None, page=1, page_size=100, timeout=25) -> dict:
    """POST /v2/billing/meter/get-user-resource —— 套餐额度明细"""
    p = get_platform(platform_key)
    url = "%s/v2/billing/meter/get-user-resource" % p["api_base"]
    begin, end = _default_time_range()
    payload = {
        "PageNumber": page,
        "PageSize": page_size,
        "ProductCode": product_code or DEFAULT_PRODUCT_CODE,
        "Status": status or DEFAULT_STATUS,
        "PackageEndTimeRangeBegin": begin,
        "PackageEndTimeRangeEnd": end,
    }
    try:
        r = _session().post(url, headers=_billing_headers(access_token, uid, enterprise_id, domain),
                            json=payload, timeout=timeout)
        return r.json()
    except Exception as e:
        raise ProviderError("查询套餐额度失败: %s" % e)


def fetch_dosage_notify(platform_key, access_token, uid=None, enterprise_id=None,
                        domain=None, timeout=20) -> dict:
    p = get_platform(platform_key)
    url = "%s/v2/billing/meter/get-dosage-notify" % p["api_base"]
    try:
        r = _session().post(url, headers=_billing_headers(access_token, uid, enterprise_id, domain),
                            json={}, timeout=timeout)
        return r.json()
    except Exception as e:
        raise ProviderError("查询用量提醒失败: %s" % e)


def fetch_payment_type(platform_key, access_token, uid=None, enterprise_id=None,
                       domain=None, timeout=20) -> dict:
    p = get_platform(platform_key)
    url = "%s/v2/billing/meter/get-payment-type" % p["api_base"]
    try:
        r = _session().post(url, headers=_billing_headers(access_token, uid, enterprise_id, domain),
                            json={}, timeout=timeout)
        return r.json()
    except Exception as e:
        raise ProviderError("查询套餐类型失败: %s" % e)


def _to_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _extract_accounts(user_resource: dict) -> list:
    """从 get-user-resource 的多层结构里定位 Accounts 列表。

    实测真实结构: data.Response.Data.Accounts[]
    兼容 data.Resources / data.List / data.accounts 等其它形态。
    """
    d = (user_resource or {}).get("data") or {}
    resp = d.get("Response") or d.get("response") or {}
    inner = resp.get("Data") or resp.get("data") or {}
    accounts = inner.get("Accounts") or inner.get("accounts")
    if isinstance(accounts, list):
        return accounts
    for k in ("Accounts", "accounts", "Resources", "resources", "List", "list"):
        v = d.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            it = v.get("Items") or v.get("items")
            if isinstance(it, list):
                return it
    return []


def summarize_quota(user_resource: dict, dosage: dict, payment: dict) -> dict:
    """把三个额度接口的原始响应压成面板需要的摘要。

    真实字段(腾讯 credits 计费): CapacitySize / CapacityUsed / CapacityRemain,
    周期字段 CycleCapacitySize / CycleCapacityRemain。
    """
    out = {"plan_type": "", "packages": [], "total": 0.0, "used": 0.0,
           "remain": 0.0, "unit": "credits", "notify": "", "raw_ok": True}

    d = (payment or {}).get("data") or {}
    out["plan_type"] = str(d.get("paymentType") or d.get("PaymentType")
                           or d.get("planType") or d.get("type") or "") or "-"

    dn = (dosage or {}).get("data") or {}
    out["notify"] = str(dn.get("dosageNotifyZh") or dn.get("NotifyZh")
                        or dn.get("notifyZh") or "") or ""

    total = used = remain = 0.0
    for it in _extract_accounts(user_resource):
        if not isinstance(it, dict):
            continue

        def pick(*names):
            for n in names:
                if n in it and it[n] is not None:
                    return it[n]
            return None

        size = pick("CapacitySizePrecise", "CapacitySize", "TotalAmount", "Total")
        cons = pick("CapacityUsedPrecise", "CapacityUsed", "UsedAmount", "Used")
        rem = pick("CapacityRemainPrecise", "CapacityRemain", "RemainAmount", "Remain")
        a = _to_float(size)
        u = _to_float(cons)
        rm = _to_float(rem, max(a - u, 0.0))
        if cons is None and rem is not None:
            u = max(a - rm, 0.0)
        unit = pick("CapacityUnit", "OriginUnit") or "credits"
        out["unit"] = str(unit)
        total += a
        used += u
        remain += rm
        out["packages"].append({
            "name": str(pick("PackageName", "ProductName", "productName", "Name") or "套餐"),
            "code": str(pick("PackageCode", "packageCode") or ""),
            "total": a, "used": u, "remain": rm, "unit": str(unit),
            "cycle_size": _to_float(pick("CycleCapacitySizePrecise", "CycleCapacitySize")),
            "cycle_remain": _to_float(pick("CycleCapacityRemainPrecise", "CycleCapacityRemain")),
            "end_time": str(pick("CycleEndTime", "PackageEndTime", "EndTime", "ExpiredTime") or ""),
            "status": str(pick("Status", "status") or ""),
        })

    out["total"] = round(total, 4)
    out["used"] = round(used, 4)
    out["remain"] = round(remain if remain else max(total - used, 0.0), 4)
    return out


def collect_quota(platform_key, access_token, uid=None, enterprise_id=None,
                   domain=None, device_id=None) -> dict:
    """一次性抓取额度并汇总；Trae 走独立积分接口。"""
    if _is_trae(platform_key):
        try:
            return trae.fetch_quota(access_token, device_id=device_id)
        except Exception as e:
            return {"plan_type": "", "total": 0, "used": 0, "remain": 0,
                    "unit": "credits", "packages": [], "errors": [str(e)], "raw": {}}
    errors = []
    ur = dn = pt = {}
    try:
        ur = fetch_user_resource(platform_key, access_token, uid, enterprise_id, domain)
    except Exception as e:
        errors.append("user-resource: %s" % e)
    try:
        dn = fetch_dosage_notify(platform_key, access_token, uid, enterprise_id, domain)
    except Exception as e:
        errors.append("dosage-notify: %s" % e)
    try:
        pt = fetch_payment_type(platform_key, access_token, uid, enterprise_id, domain)
    except Exception as e:
        errors.append("payment-type: %s" % e)

    s = summarize_quota(ur, dn, pt)
    s["errors"] = errors
    s["raw"] = {"user_resource": ur, "dosage": dn, "payment": pt}
    return s


# --------------------------------------------------------------------------
# 模型调用（OpenAI 兼容）
# --------------------------------------------------------------------------

def build_model_headers(platform_key, access_token, uid=None, session_id=None,
                        enterprise_id=None, domain=None, machine_id=None,
                        device_id=None) -> dict:
    """构造模型网关必需的请求头。

    实测结论：Authorization 是唯一强制项；会话 ID / X-User-Id 用于来源标识，
    带上可提升成功率与配额归属准确性。
    """
    p = get_platform(platform_key)
    if _is_trae(platform_key):
        return trae.solo_headers(access_token, uid=uid, machine_id=machine_id,
                                 device_id=device_id, stream=True,
                                 session_id=session_id)
    h = {
        "Authorization": "Bearer %s" % access_token,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": UA,
    }
    h[p["session_header"]] = session_id or str(uuid.uuid4())
    if uid:
        h["X-User-Id"] = uid
    if enterprise_id:
        h["X-Enterprise-Id"] = enterprise_id
        h["X-Tenant-Id"] = enterprise_id
    if domain:
        h["X-Domain"] = domain
    return h


DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


def normalize_body(platform_key: str, body: dict,
                   system_prompt: str | None = None) -> dict:
    """按平台差异规范化请求体。

    实测差异（2026-08-05，真实账号）：
      - CodeBuddy 国际站 www.codebuddy.ai：messages[0].role 必须为 system，
        否则一律 400 {"code":11101,"msg":"Parse message failed"}；
        内容可以为空字符串，顺序颠倒（user 在前）同样报错。
      - copilot.tencent.com（WorkBuddy / CodeBuddy CN）：无此限制，
        但带 system 同样正常，故补齐不会有副作用。
      - 三平台一致：stream 必须为 true，非流式一律 11101。
    """
    if _is_trae(platform_key):
        return trae.normalize_body(body, body.get("model") or "")

    p = get_platform(platform_key)
    out = dict(body)
    out["stream"] = True

    msgs = list(out.get("messages") or [])
    if p.get("require_leading_system"):
        if not msgs or (msgs[0].get("role") != "system"):
            msgs.insert(0, {"role": "system",
                            "content": system_prompt or DEFAULT_SYSTEM_PROMPT})
    out["messages"] = msgs
    return out


def chat_url(platform_key: str, model_base: str | None = None,
             chat_path: str | None = None) -> str:
    p = get_platform(platform_key)
    return "%s%s" % ((model_base or p["model_base"]).rstrip("/"),
                     chat_path or p["chat_path"])


def image_url(platform_key: str, image_base: str | None = None,
              image_path: str | None = None) -> str:
    """图像生成端点。

    实测结论（2026-08-06，真实账号）：
      - 图像模型（hunyuan-image-*）走独立的 /v2/images/generations 端点，
        同一账号的 chat 网关返回 11103 "Backend [aiart] is not supported"；
      - 该端点只在国内网关 copilot.tencent.com 可用，国际站 www.codebuddy.ai
        对 hunyuan-image-* 返回 11102（未注册）；
      - 成功响应为 JSON：data.data[].url / revised_prompt，usage.credit 计费。
    """
    p = get_platform(platform_key)
    # 图像后端统一落在 copilot.tencent.com，与 platform 的 model_base 无关
    base = (image_base or "https://copilot.tencent.com").rstrip("/")
    return "%s%s" % (base, image_path or "/v2/images/generations")


def config_url(platform_key: str, model_base: str | None = None) -> str:
    p = get_platform(platform_key)
    return "%s%s" % ((model_base or p["model_base"]).rstrip("/"), p["config_path"])


def _fetch_config_models(platform_key, access_token, timeout=20) -> list:
    """读取 /v3/config 的 agents[cli].models（国际站可用）。"""
    p = get_platform(platform_key)
    headers = {
        "Authorization": "Bearer %s" % access_token,
        "Accept": "application/json",
        "Origin": p["api_base"],
        "Referer": p["api_base"] + "/",
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
    }
    try:
        r = _session().get(config_url(platform_key), headers=headers, timeout=timeout)
        if r.status_code != 200:
            return []
        body = r.json() or {}
        if not _ok(body):
            return []
        agents = (body.get("data") or {}).get("agents") or []
        for agent in agents:
            if agent.get("name") == "cli":
                ids = agent.get("models") or []
                return [str(x).strip() for x in ids if str(x).strip()]
        return []
    except Exception:
        return []


def fetch_upstream_models(platform_key, access_token, timeout=20, uid=None,
                           machine_id=None, device_id=None) -> list:
    """读取上游官方模型目录（CLI agent 白名单）。

    实测（2026-08-29）：
      - GET {model_base}/console/enterprises/personal/models 可用；
      - data.agents[].name == "cli" 的 models 是 CLI 客户端可用模型白名单；
      - data.models[] 只是模型元数据全集，包含非 CLI 可用项，不能直接当目录；
      - www.codebuddy.ai 当前返回 500，保留探测回退即可。
    """
    p = get_platform(platform_key)
    if _is_trae(platform_key):
        return fetch_trae_models(platform_key, access_token, uid=uid,
                                 machine_id=machine_id, device_id=device_id)
    base = (p.get("model_base") or "").rstrip("/")
    headers = {
        "Authorization": "Bearer %s" % access_token,
        "Accept": "application/json",
        "Origin": p["api_base"],
        "Referer": p["api_base"] + "/",
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
    }
    try:
        r = _session().get(base + "/console/enterprises/personal/models",
                           headers=headers, timeout=timeout)
        if r.status_code != 200:
            # 国际站该路径当前 500；/v3/config 有同样的 agents[cli].models 目录
            return _fetch_config_models(platform_key, access_token, timeout)
        body = r.json() or {}
        if not _ok(body):
            return _fetch_config_models(platform_key, access_token, timeout)
        data = body.get("data") or {}
        agents = data.get("agents") or []
        for agent in agents:
            if agent.get("name") == "cli":
                ids = agent.get("models") or []
                return [str(x).strip() for x in ids if str(x).strip()]
        return []
    except Exception:
        return []


def merge_upstream_models(platform_key, access_token, uid=None,
                           machine_id=None, device_id=None) -> list:
    """把官方目录里的 CLI 模型合并进运行时注册表，返回新增模型 ID。

    只标记平台与 chat 类型；这些模型来自 CLI 官方白名单，因此不再逐个真实请求。
    """
    # 国际站 /console/.../models 常年 500；fetch_upstream_models 内部已回退
    # /v3/config 的 agents[cli].models。这里直接用它的返回值，空列表才放弃。
    ids = fetch_upstream_models(platform_key, access_token, uid=uid,
                                 machine_id=machine_id, device_id=device_id)
    if not ids:
        ids = _fetch_config_models(platform_key, access_token)
    if not ids:
        return []
    reg = db.get_model_registry()
    added = []
    for mid in ids:
        rec = reg.get(mid)
        if not isinstance(rec, dict):
            rec = {"type": model_type(mid), "platforms": []}
        rec["type"] = rec.get("type") or model_type(mid)
        if platform_key not in rec.get("platforms", []):
            rec.setdefault("platforms", []).append(platform_key)
        if mid not in reg:
            added.append(mid)
        reg[mid] = rec
    db.set_model_registry(reg)
    return added


TRAE_INTERNAL_MODELS = {
    "browser_use_subagent", "file_search_agent", "explore_sub_agent_v2", "summary",
    "sagitta", "aquila", "custom_model_placeholder", "custom_model_1M_text",
    "custom_model_1M", "custom_model_kimi", "custom_model_claude",
    "custom_model_gpt-5", "custom_model_no-fc", "custom_model_deepseek_chat",
    "custom_model_deepseek_reasoner", "custom_model_deepseek_v4",
}


# Trae 官方目录的展示名（来自 get_detail_param 的 display_name）
TRAE_MODEL_DISPLAY_NAMES = {
    "DeepSeek-V4-Flash-Official": "DeepSeek-V4-Flash 正式版",
    "DeepSeek-V4-Pro-Official": "DeepSeek-V4-Pro 正式版",
    "DeepSeek-V4-Flash": "DeepSeek-V4-Flash",
    "DeepSeek-V4-Pro": "DeepSeek-V4-Pro",
    "Doubao-Seed-2.1-Pro": "Doubao-Seed-2.1-Pro",
    "Doubao-Seed-2.1-Turbo": "Doubao-Seed-2.1-Turbo",
    "Doubao-Seed-2.0-Code": "Doubao-Seed-2.0-Code",
    "Doubao-Seed-Evolving": "Doubao-Seed-Evolving",
    "glm-5": "GLM-5",
    "glm-5-turbo": "GLM-5-Turbo",
    "kimi-k2.7-code": "Kimi-K2.7-Code",
    "qwen3.8-max": "Qwen3.8-Max",
    "qwen-3.7-plus": "Qwen-3.7-Plus",
    "seed-code-pro-0430": "Seed-Code-Pro-0430",
}


def fetch_trae_models(platform_key, access_token, uid=None, machine_id=None,
                      device_id=None) -> list:
    """Trae 官方模型目录（过滤内部/子代理/占位模型）。"""
    items = trae.fetch_model_catalog(access_token, uid=uid, machine_id=machine_id,
                                     device_id=device_id)
    # 上游 display_name 会把多个 config_name 映射成同一显示名（如 seed-code-pro-0430
    # 也显示 Seed-2.1-Pro），仅在缺省映射不存在时写入，避免覆盖人工准确命名。
    for x in items:
        name = str(x.get("name") or "").strip()
        if name and x["id"] not in TRAE_MODEL_DISPLAY_NAMES:
            TRAE_MODEL_DISPLAY_NAMES[x["id"]] = name
    return [x["id"] for x in items
            if x["id"] not in TRAE_INTERNAL_MODELS
            and not x["id"].startswith("custom_model_")]


def available_models(platform_key: str) -> list:
    """返回该平台「真实探测确认可用」的模型 id 列表。

    上游无模型枚举接口（/v3/config 仅 productFeatures，/v1/models 404），因此模型
    目录完全来自运行时探测并持久化在 DB 注册表（见 discovery.py）。本函数只读取
    注册表中「该平台可用」的模型，不再内置任何静态清单。
    """
    return [e["id"] for e in catalog_entries(platform_key)]


def model_type(mid: str | None) -> str:
    """返回模型类型 chat / image / audio；未知模型回退为 chat（由实时探测决定）。"""
    mid = str(mid or "").strip()
    rec = db.get_model_registry().get(mid)
    return (rec or {}).get("type", "chat") if isinstance(rec, dict) else "chat"


def is_image_model(mid: str | None) -> bool:
    """模型是否为图像生成类（走 /v2/images/generations，而非 chat 网关）。"""
    return model_type(mid) == "image"


def catalog_models() -> list:
    """返回注册表中全部模型 id（去重，按字母序）。"""
    return sorted(db.get_model_registry().keys())


def model_name(mid: str) -> str:
    """展示名：优先使用上游 display_name，其次按 id 自动生成可读名称。"""
    if not mid:
        return mid
    if mid in TRAE_MODEL_DISPLAY_NAMES:
        return TRAE_MODEL_DISPLAY_NAMES[mid]
    return " ".join(w.capitalize() for w in str(mid).replace("_", "-").split("-") if w)


def to_entries(ids) -> list:
    """把 id 列表（或 {id} 字典）转成 [{id, name, kind}]，按出现顺序去重。类型取自注册表。"""
    reg = db.get_model_registry()
    seen, out = set(), []
    for m in ids or []:
        mid = m["id"] if isinstance(m, dict) else m
        if mid and mid not in seen:
            seen.add(mid)
            typ = (reg.get(mid) or {}).get("type", "chat") if isinstance(reg.get(mid), dict) else "chat"
            out.append({"id": mid, "name": model_name(mid), "kind": typ})
    return out


def catalog_entries(platform_key: str) -> list:
    """返回注册表中「该平台可用」的模型条目 [{id, name, kind}]。"""
    reg = db.get_model_registry()
    out = []
    for mid, rec in reg.items():
        if not isinstance(rec, dict):
            continue
        if platform_key and platform_key not in rec.get("platforms", []):
            continue
        out.append({"id": mid, "name": model_name(mid),
                    "kind": rec.get("type", "chat")})
    return out


# --------------------------------------------------------------------------
# 每日签到（workbuddy / trae；codebuddy 国际站无签到）
# --------------------------------------------------------------------------
#
# 实测接口(2026-08-05, 真实账号, cockpit-tools 同款):
#   状态: POST {api_base}/v2/billing/meter/checkin-activity-status   body {}
#   领取: POST {api_base}/v2/billing/meter/daily-checkin             body {}
# api_base 必须是 www.codebuddy.cn（copilot.tencent.com 该接口 404）。
# 国际站(codebuddy)无签到能力。

def supports_checkin(platform_key: str) -> bool:
    return bool(get_platform(platform_key).get("supports_checkin"))


def _checkin_status_url(platform_key: str) -> str:
    return "%s/v2/billing/meter/checkin-activity-status" % get_platform(platform_key)["api_base"]


def _checkin_claim_url(platform_key: str) -> str:
    return "%s/v2/billing/meter/daily-checkin" % get_platform(platform_key)["api_base"]


def fetch_checkin_status(platform_key, access_token, uid=None, enterprise_id=None,
                         domain=None, timeout=20, device_id=None) -> dict:
    """查询当前签到活动状态与今日是否已签到。"""
    if _is_trae(platform_key):
        return trae.fetch_checkin_status(access_token, device_id=device_id,
                                         timeout=timeout)
    if not supports_checkin(platform_key):
        return {"supported": False, "today_checked_in": False,
                "streak_days": 0, "claimable": False, "raw": {}}
    url = _checkin_status_url(platform_key)
    try:
        r = _session().post(url, headers=_billing_headers(access_token, uid, enterprise_id, domain),
                            json={}, timeout=timeout)
        body = r.json()
    except Exception as e:
        raise ProviderError("查询签到状态失败: %s" % e)

    if not _ok(body):
        raise ProviderError("查询签到状态失败(code=%s): %s"
                            % (body.get("code"), _msg(body)))

    data = body.get("data") or {}
    dates = data.get("checkin_dates") or []
    if not isinstance(dates, list):
        dates = []
    today_checked_in = bool(data.get("today_checked_in"))
    today_credit = _to_float(data.get("today_credit"))
    return {
        "supported": True,
        "active": bool(data.get("active")),
        "today_checked_in": today_checked_in,
        "streak_days": _to_float(data.get("streak_days")),
        "daily_credit": _to_float(data.get("daily_credit")),
        "today_credit": today_credit,
        "week_checkin_days": _to_float(data.get("week_checkin_days")),
        "total_credits": _to_float(data.get("total_credits")),
        "checkin_dates": dates,
        "last_checkin_date": dates[-1] if dates else "",
        "theme_name": str(data.get("theme_name") or ""),
        "season": data.get("season"),
        # 今日可领：有额度且尚未签到
        "claimable": (today_credit > 0) and (not today_checked_in),
        "raw": data,
    }


def do_checkin(platform_key, access_token, uid=None, enterprise_id=None,
               domain=None, timeout=20, device_id=None) -> dict:
    """领取今日签到积分。返回领取结果（含本次获得积分）。"""
    if _is_trae(platform_key):
        return trae.do_checkin(access_token, device_id=device_id, timeout=timeout)
    if not supports_checkin(platform_key):
        raise ProviderError("该平台不支持签到")

    url = _checkin_claim_url(platform_key)
    try:
        r = _session().post(url, headers=_billing_headers(access_token, uid, enterprise_id, domain),
                            json={}, timeout=timeout)
        body = r.json()
    except Exception as e:
        raise ProviderError("签到失败: %s" % e)

    if not _ok(body):
        raise ProviderError("签到失败(code=%s): %s"
                            % (body.get("code"), _msg(body)))

    data = body.get("data") or {}
    # 领取响应字段名不统一，做多重兜底
    claimed = (_to_float(data.get("credit")) or _to_float(data.get("credits"))
               or _to_float(data.get("amount")) or _to_float(data.get("today_credit")))
    return {
        "ok": True,
        "claimed_credit": claimed,
        "today_checked_in": bool(data.get("today_checked_in")
                                 or data.get("checked_in") or True),
        "streak_days": _to_float(data.get("streak_days")),
        "msg": str(body.get("msg") or body.get("message") or "OK"),
        "raw": data,
    }
