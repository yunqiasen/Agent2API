# -*- coding: utf-8 -*-
"""TraeWork / Trae SOLO 渠道适配层。

结构对齐 hy3api 的平台约定；上游协议常量、请求头、payload、SSE 解析逻辑
移植自 ai-multi-platform-proxy（internal/provider/trae）。
"""
from __future__ import annotations

import json
import time
import uuid

import requests

# 来自 ai-multi-platform-proxy internal/provider/trae/constants.go
AGENT_HOST = "https://trae-api-cn.mchost.guru"
UG_HOST = "https://api.trae.cn"
OAUTH_HOST = "https://api.trae.com.cn"
CONSOLE_HOST = "https://www.trae.cn"
CLIENT_ID = "en1oxy7wnw8j9n"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IDE_VERSION = "0.1.43"
IDE_VERSION_CODE = "20260716"
DEVICE_BRAND = "83DG"
OS_VERSION = "Windows 11 Pro"
FUNCTION = "solo_work_lite"

EP_CHAT = "/api/agent/v3/llm_utils_chat"
EP_MODELS = "/api/ide/v1/get_detail_param"
EP_EXCHANGE = "/cloudide/api/v3/trae/oauth/ExchangeToken"
EP_USER_INFO = "/cloudide/api/v3/trae/oauth/GetUserInfo"
EP_CHECKIN_STATUS = "/trae/api/v2/ug/checkin_credits/status"
EP_CHECKIN_CLAIM = "/trae/api/v2/ug/checkin_credits/claim"
EP_ENT_USAGE = "/trae/api/v2/pay/ide_user_ent_usage"

DEFAULT_TIMEOUT = 30


def _ua() -> str:
    return "Trae/" + IDE_VERSION


# 官方客户端签到 / 积分接口实际使用浏览器 UA（逆向自多个开源签到实现与实测）
CHECKIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _ua(), "Accept": "application/json"})
    return s


def solo_headers(access_token, uid=None, machine_id=None, device_id=None,
                 stream=True, session_id=None) -> dict:
    """SOLO 对话 / 模型目录请求头。"""
    h = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": _ua(),
        "Authorization": "Cloud-IDE-JWT %s" % access_token,
        "X-Cloudide-Token": access_token,
        "X-Ide-Token": access_token,
        "X-App-Id": APP_ID,
        "X-App-Version": "default",
        "X-Ide-Version": IDE_VERSION,
        "X-Ide-Version-Code": IDE_VERSION_CODE,
        "X-App-Version-Code": IDE_VERSION_CODE,
        "X-Ide-Version-Type": "stable",
        "X-Device-Type": "windows",
        "X-OS-Version": OS_VERSION,
        "X-Device-Brand": DEVICE_BRAND,
        "Request-Traffic-Type": "prod",
    }
    if uid:
        h["X-Uid"] = uid
    if machine_id:
        h["X-Machine-Id"] = machine_id
    if device_id:
        h["X-Device-Id"] = device_id
    if session_id:
        h["X-Trae-Session-Id"] = session_id
    return h


def ug_headers(access_token, device_id=None) -> dict:
    """签到 / 积分请求头（对齐官方客户端）。

    风控事实（实测 + 逆向确认）：claim 接口的 x-device-id 必须是官方客户端
    设备注册服务下发的 16 位纯数字 Aha 设备号；用自造 hex/UUID 会被软拒，
    返回误导性 9074「当前参与用户太多」（缺失则 9004 订单参数错误）。
    """
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": CHECKIN_UA,
        "Authorization": "Cloud-IDE-JWT %s" % access_token,
        "X-User-Region": "CN",
        "X-Device-Type": "Windows",
    }
    if device_id:
        h["X-Device-Id"] = device_id
    return h


def oauth_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _ua(),
    }


# --------------------------------------------------------------------------
# OAuth / token
# --------------------------------------------------------------------------

def build_login_url(machine_id: str, device_id: str, trace_id: str | None = None) -> str:
    from urllib.parse import urlencode
    trace_id = trace_id or uuid.uuid4().hex[:8]
    q = {
        "login_version": "1",
        "auth_from": "solo",
        "login_channel": "native_ide",
        "plugin_version": "2.3.62834",
        "auth_type": "local",
        "client_id": CLIENT_ID,
        "redirect": "0",
        "login_trace_id": trace_id,
        "auth_callback_url": "http://127.0.0.1:18080/authorize",
        "machine_id": machine_id,
        "device_id": device_id,
        "x_device_id": device_id,
        "x_machine_id": machine_id,
        "x_device_brand": "PC",
        "x_device_type": "PC",
        "x_os_version": "1.0",
        "x_app_version": IDE_VERSION,
        "x_app_type": "stable",
    }
    return CONSOLE_HOST + "/authorization?" + urlencode(q)


def exchange_token(refresh_token: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """ExchangeToken：refreshToken 换 accessToken（refreshToken 轮换）。"""
    url = OAUTH_HOST + EP_EXCHANGE
    body = {"ClientID": CLIENT_ID, "RefreshToken": refresh_token,
            "ClientSecret": "-", "UserID": ""}
    r = _session().post(url, headers=oauth_headers(), json=body, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError("ExchangeToken HTTP %s: %s" % (r.status_code, r.text[:200]))
    data = (r.json() or {}).get("Result") or {}
    if not data.get("Token"):
        raise RuntimeError("ExchangeToken 未返回 Token")
    exp = data.get("TokenExpireAt") or 0
    if exp > 1_000_000_000_000:
        exp //= 1000
    if not exp and data.get("TokenExpireDuration"):
        exp = int(time.time()) + int(data["TokenExpireDuration"])
    return {
        "access_token": data["Token"],
        "refresh_token": data.get("RefreshToken") or refresh_token,
        "expires_at": exp,
    }


def get_user_info(access_token: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """GetUserInfo：补齐 uid / nickname / enterprise_id。"""
    url = OAUTH_HOST + EP_USER_INFO
    body = {"ReqSource": "IDE", "IDEVersion": IDE_VERSION}
    h = oauth_headers()
    h["x-cloudide-token"] = access_token
    r = _session().post(url, headers=h, json=body, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError("GetUserInfo HTTP %s: %s" % (r.status_code, r.text[:200]))
    d = r.json()
    res = d.get("Result") or {}
    return {
        "uid": d.get("UserID") or res.get("UserID") or "",
        "nickname": d.get("ScreenName") or res.get("ScreenName") or "",
        "enterprise_id": d.get("EnterpriseID") or res.get("EnterpriseID") or "",
        "raw": d,
    }


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------

def fetch_model_catalog(access_token, uid=None, machine_id=None, device_id=None,
                        timeout: int = DEFAULT_TIMEOUT) -> list:
    """返回 [{id,name}]；调用方负责过滤内部模型。"""
    body = {
        "function": FUNCTION,
        "config_names": None,
        "need_prompt": False,
        "current_config_info": None,
        "poly_prompt": True,
        "mode_type": None,
        "agent_type": None,
    }
    h = solo_headers(access_token, uid=uid, machine_id=machine_id,
                     device_id=device_id, stream=False)
    r = _session().post(AGENT_HOST + EP_MODELS, headers=h, json=body, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError("模型目录 HTTP %s: %s" % (r.status_code, r.text[:200]))
    data = r.json() or {}
    out = []
    for cfg in data.get("config_info_list") or []:
        mid = str(cfg.get("config_name") or "").strip()
        if not mid:
            continue
        out.append({
            "id": mid,
            "name": str(((cfg.get("display_config") or {}).get("display_name")) or mid),
        })
    if not out:
        raise RuntimeError("模型目录为空")
    return out


# --------------------------------------------------------------------------
# 请求体 / SSE
# --------------------------------------------------------------------------

def normalize_body(body: dict, model: str) -> dict:
    """OpenAI chat body → Trae SOLO body。"""
    out = dict(body)
    out["stream"] = True
    out["function"] = FUNCTION
    model = str(model or "").strip() or "DeepSeek-V4-Flash-Official"
    out["config_name"] = model
    out["model"] = model

    msgs = out.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "assistant" and isinstance(m.get("tool_calls"), list):
                kept = []
                for tc in m["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        tc = dict(tc)
                        tc["function_call"] = fn
                        tc.pop("function", None)
                    fc = tc.get("function_call")
                    if isinstance(fc, dict) and str(fc.get("name") or "").strip():
                        kept.append(tc)
                if kept:
                    m["tool_calls"] = kept
                else:
                    m.pop("tool_calls", None)

            c = m.get("content")
            if isinstance(c, str):
                m["content"] = [{"type": "text", "text": c}]
            elif c is None and role == "assistant":
                m["content"] = []

        # Trae 要求首条 system；缺失则补默认，否则可能被解析失败
        if not msgs or msgs[0].get("role") != "system":
            msgs.insert(0, {"role": "system", "content": [{"type": "text", "text": "You are a helpful assistant."}]})
        out["messages"] = msgs
    return out


def open_chat(access_token, body: dict, uid=None, machine_id=None,
              device_id=None, timeout=(10, 300)):
    """发起 SOLO 流式对话，返回 requests.Response(stream=True)。"""
    h = solo_headers(access_token, uid=uid, machine_id=machine_id,
                     device_id=device_id, stream=True)
    return requests.post(AGENT_HOST + EP_CHAT, headers=h, json=body,
                         timeout=timeout, stream=True)


class SSEAggregator:
    """旁路解析 Trae SSE，聚合 content/reasoning/usage。"""

    def __init__(self):
        self.content = []
        self.reasoning = []
        self.finish_reason = "stop"
        self.usage = None
        self.error = None
        self._event = ""
        self._data = []

    def feed_line(self, line: str):
        if line.startswith("event:"):
            self._event = line[6:].strip()
        elif line.startswith("data:"):
            self._data.append(line[5:])
        elif line == "":
            if not self._event and not self._data:
                return
            payload = "".join(self._data).strip()
            self._dispatch(self._event, payload)
            self._event = ""
            self._data = []

    def _dispatch(self, event: str, payload: str):
        if not payload:
            return
        try:
            obj = json.loads(payload)
        except Exception:
            return
        if event == "output":
            self.content.append(obj.get("response") or "")
            self.reasoning.append(obj.get("reasoning_content") or "")
        elif event == "token_usage":
            self.usage = obj
        elif event == "done":
            if obj.get("finish_reason"):
                self.finish_reason = obj["finish_reason"]
        elif event == "error":
            self.error = obj

    def finish(self):
        if self._data:
            self._dispatch(self._event, "".join(self._data).strip())
            self._event = ""
            self._data = []

    def result(self, model: str) -> dict:
        msg = {"role": "assistant", "content": "".join(self.content)}
        reasoning = "".join(self.reasoning)
        if reasoning:
            msg["reasoning_content"] = reasoning
        usage = self.usage or {}
        out = {
            "id": "chatcmpl-" + uuid.uuid4().hex[:24],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": self.finish_reason}],
        }
        if usage:
            out["usage"] = usage
        else:
            out["usage"] = {"prompt_tokens": 0, "completion_tokens": 0,
                            "total_tokens": 0}
        return out


# --------------------------------------------------------------------------
# 积分 / 签到
# --------------------------------------------------------------------------

def fetch_credit(access_token, device_id=None, timeout: int = DEFAULT_TIMEOUT) -> float:
    """ide_user_ent_usage 聚合 credits_limit。"""
    r = _session().post(UG_HOST + EP_ENT_USAGE, headers=ug_headers(access_token, device_id),
                        json={}, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError("积分 HTTP %s: %s" % (r.status_code, r.text[:200]))
    d = r.json() or {}
    packs = d.get("user_entitlement_pack_list") or []
    return float(sum(float(((p.get("entitlement_base_info") or {}).get("quota") or {}).get("credits_limit") or 0)
                     for p in packs))


def fetch_checkin_status(access_token, device_id=None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    r = _session().post(UG_HOST + EP_CHECKIN_STATUS,
                        headers=ug_headers(access_token, device_id), json={}, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError("签到状态 HTTP %s: %s" % (r.status_code, r.text[:200]))
    d = r.json() or {}
    return {
        "supported": True,
        "today_checked_in": bool(d.get("checked_in")),
        "streak_days": int(d.get("streak_days") or 0),
        "daily_credit": float(d.get("daily_credit") or 0),
        "today_credit": float(d.get("credits") or 0),
        "claimable": (not bool(d.get("checked_in"))) and bool(d.get("enable")),
        "raw": d,
    }


def _looks_like_aha_did(device_id) -> bool:
    """16 位左右纯数字 = 官方客户端下发的 Aha 设备号。"""
    s = str(device_id or "")
    return s.isdigit() and 8 <= len(s) <= 20


def do_checkin(access_token, device_id=None, timeout: int = DEFAULT_TIMEOUT,
               attempts: int = 3) -> dict:
    """领取签到积分。

    9074「当前参与用户太多」是误导文案：绝大多数情况是设备指纹风控——
    x-device-id 不是官方客户端注册的 16 位 Aha 数字设备号。设备号无效时
    重试永远失败；设备号有效时 9074 才可能是瞬时量控，值得递增退避重试。
    """
    if not _looks_like_aha_did(device_id):
        raise RuntimeError(
            "签到失败：x-device-id 不是官方客户端注册的 Aha 数字设备号"
            "（当前为 %s）。请在「设置 → TraeWork Aha 设备号」填入官方客户端 "
            "storage.json 中 iCubeAuthInfo://icube-dc: 后面的 16 位数字。"
            % (("空" if not device_id else "hex/UUID")))

    last = {}
    delays = (10, 15, 22, 30)
    for i in range(max(1, attempts)):
        r = _session().post(UG_HOST + EP_CHECKIN_CLAIM,
                            headers=ug_headers(access_token, device_id), json={},
                            timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError("签到 HTTP %s: %s" % (r.status_code, r.text[:200]))
        d = r.json() or {}
        last = d
        code = int(d.get("code") or 0)
        if code == 0 or d.get("checked_in"):
            return {
                "ok": True,
                "claimed_credit": float(d.get("credits") or 0),
                "today_checked_in": True,
                "streak_days": int(d.get("streak_days") or 0),
                "msg": str(d.get("message") or "OK"),
                "raw": d,
            }
        # 9074：设备号有效前提下才可能是瞬时量控，递增退避重试
        if code == 9074 and i < max(1, attempts) - 1:
            time.sleep(delays[min(i, len(delays) - 1)])
            continue
        if code == 9074:
            raise RuntimeError(
                "签到失败(code=9074)：设备指纹校验未通过或上游瞬时量控。"
                "若确认 Aha 设备号来自官方客户端，稍后重试或重启一次客户端刷新设备注册")
        raise RuntimeError("签到失败(code=%s): %s" % (code, d.get("message") or "未知错误"))

    raise RuntimeError("签到失败(code=%s): %s"
                       % (last.get("code"), last.get("message") or "重试后仍失败"))
