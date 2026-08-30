# -*- coding: utf-8 -*-
"""hy3api - 反代引擎

核心职责：
  1. 令牌保活：按 expires_at 提前刷新（refresh 走 X-Refresh-Token 头）
  2. 号池调度：least_used / round_robin / random，失败自动换号重试
  3. 对话反代：上游【只支持流式】，非流式请求由本层聚合后返回标准 OpenAI 响应
  4. 每日签到：CN 域平台手动/定时自动签到，领取积分并落库状态
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import time
import uuid

import db
import providers as P
import requests
import trae

_refresh_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()

# 上游网络抖动时，在同一账号上的重试次数
NET_RETRY = 2


def _lock_for(account_id: int) -> threading.Lock:
    with _locks_guard:
        lk = _refresh_locks.get(account_id)
        if lk is None:
            lk = threading.Lock()
            _refresh_locks[account_id] = lk
        return lk


def _norm_ts(v) -> int:
    """expires_at 可能是秒或毫秒，统一成秒。"""
    try:
        v = int(v or 0)
    except Exception:
        return 0
    if v > 10_000_000_000:      # 毫秒
        v //= 1000
    return v


class ProxyError(Exception):
    def __init__(self, message, status=500, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


# --------------------------------------------------------------------------
# 令牌保活
# --------------------------------------------------------------------------

def ensure_token(account_row, force: bool = False) -> dict:
    """确保账号 access_token 有效，必要时刷新并落库。返回最新账号 dict。"""
    acc = dict(account_row)
    aid = acc["id"]

    if db.get_setting("auto_refresh", "1") != "1" and not force:
        return acc

    ahead = int(db.get_setting("refresh_ahead", "900") or 900)
    exp = _norm_ts(acc.get("expires_at"))
    now = int(time.time())

    need = force or (exp > 0 and now >= exp - ahead)
    if not need:
        return acc

    rt = (acc.get("refresh_token") or "").strip()
    if not rt:
        return acc

    with _lock_for(aid):
        fresh = db.get_account(aid)
        if fresh is None:
            return acc
        acc = dict(fresh)
        exp = _norm_ts(acc.get("expires_at"))
        if not force and exp > 0 and now < exp - ahead:
            return acc  # 其它线程已刷新

        try:
            res = P.refresh_token(acc["platform"], acc["access_token"],
                                  acc["refresh_token"], domain=acc.get("domain") or None)
        except Exception as e:
            db.update_account(aid, status="error", status_reason="刷新失败: %s" % e,
                              last_error=str(e)[:400])
            return acc

        fields = {
            "access_token": res["access_token"],
            "status": "active",
            "status_reason": "",
            "last_error": "",
        }
        if res.get("refresh_token"):
            fields["refresh_token"] = res["refresh_token"]
        if res.get("expires_at"):
            fields["expires_at"] = int(res["expires_at"])
        if res.get("domain"):
            fields["domain"] = res["domain"]
        db.update_account(aid, **fields)
        acc.update(fields)
        return acc


def refresh_quota(account_row) -> dict:
    """刷新账号额度并落库。"""
    acc = ensure_token(account_row)
    s = P.collect_quota(acc["platform"], acc["access_token"],
                        uid=acc.get("uid") or None,
                        enterprise_id=acc.get("enterprise_id") or None,
                        domain=acc.get("domain") or None,
                        device_id=acc.get("device_id") or None)
    db.save_quota(acc["id"], s)
    return s


# --------------------------------------------------------------------------
# 每日签到
# --------------------------------------------------------------------------

def _checkin_device_id(acc: dict):
    """签到用设备号：TraeWork 优先用设置里的 16 位 Aha 设备号（全号池通用，
    风控只校验设备注册有效性，不绑定账号），未配置回退账号自带 device_id。"""
    if P._is_trae(acc.get("platform")):
        aha = (db.get_setting("trae_aha_device_id") or "").strip()
        if aha:
            return aha
    return acc.get("device_id") or None


def refresh_checkin_status(account_row) -> dict:
    """查询签到状态并落库。返回归一化状态字典。"""
    acc = ensure_token(account_row)
    if not P.supports_checkin(acc["platform"]):
        return {"supported": False, "today_checked_in": False,
                "streak_days": 0, "claimable": False}
    st = P.fetch_checkin_status(acc["platform"], acc["access_token"],
                                uid=acc.get("uid") or None,
                                enterprise_id=acc.get("enterprise_id") or None,
                                domain=acc.get("domain") or None,
                                device_id=_checkin_device_id(acc))
    today = _dt.datetime.now().strftime("%Y-%m-%d")
    db.update_account(acc["id"],
                      checkin_today=1 if st.get("today_checked_in") else 0,
                      checkin_streak=int(st.get("streak_days") or 0),
                      checkin_last_date=st.get("last_checkin_date")
                                        or (today if st.get("today_checked_in") else ""),
                      checkin_credit=int(st.get("today_credit") or 0),
                      checkin_updated_at=int(time.time()))
    return st


def checkin_account(account_row) -> dict:
    """手动领取今日签到积分。领取后刷新状态确保字段与上游一致。"""
    acc = ensure_token(account_row)
    if not P.supports_checkin(acc["platform"]):
        raise ProxyError("该平台不支持签到", status=400)
    res = P.do_checkin(acc["platform"], acc["access_token"],
                       uid=acc.get("uid") or None,
                       enterprise_id=acc.get("enterprise_id") or None,
                       domain=acc.get("domain") or None,
                       device_id=_checkin_device_id(acc))
    # 领取成功后刷新额度：Trae 签到直接加 credits，立即回读号池余额
    try:
        refresh_quota(db.get_account(acc["id"]))
    except Exception:
        pass
    # 领取成功后用状态接口回写 today/streak/last_date，保证与上游一致
    try:
        refresh_checkin_status(db.get_account(acc["id"]))
    except Exception:
        # 领取本身已成功，回写失败不影响结果，直接兜底标记今日已签到
        db.update_account(acc["id"],
                          checkin_today=1,
                          checkin_streak=int(res.get("streak_days") or 0),
                          checkin_last_date=_dt.datetime.now().strftime("%Y-%m-%d"),
                          checkin_updated_at=int(time.time()))
    return res


def checkin_all(silent: bool = False) -> dict:
    """遍历所有启用且支持签到的账号，逐个领取。返回逐账号结果。"""
    results = []
    for row in db.list_accounts():
        a = dict(row)
        if not a["enabled"]:
            continue
        if not P.supports_checkin(a["platform"]):
            continue
        if a.get("checkin_today"):
            results.append({"id": a["id"], "ok": True,
                            "skipped": True, "reason": "今日已签到"})
            continue
        try:
            res = checkin_account(db.get_account(a["id"]))
            results.append({"id": a["id"], "ok": True,
                            "claimed_credit": res.get("claimed_credit", 0),
                            "streak_days": res.get("streak_days", 0)})
        except Exception as e:
            results.append({"id": a["id"], "ok": False, "error": str(e)})
    return {"ok": True, "results": results}


def run_auto_checkin_if_due():
    """由后台维护线程调用：若开启自动签到且今日尚未执行，则全部签到一次。"""
    if db.get_setting("auto_checkin", "1") != "1":
        return
    today = _dt.datetime.now().strftime("%Y-%m-%d")
    if db.get_setting("_last_checkin_run_date", "") == today:
        return

    ct = (db.get_setting("checkin_time", "00:00") or "00:00").strip() or "00:00"
    try:
        hh, mm = (int(x) for x in ct.split(":"))
        run_at = _dt.datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        run_at = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # 未到设定时间则本次跳过，等下一轮（不更新 run_date，避免提前消费当日额度）
    if _dt.datetime.now() < run_at:
        return

    checkin_all(silent=True)
    db.set_setting("_last_checkin_run_date", today)


def retry_failed_checkins():
    """每小时补签一次今日仍未签到的账号（TraeWork 上游量控/风控窗口解除后
    可自动补领；其它平台失败也顺带重试）。由后台维护线程调用。"""
    if db.get_setting("auto_checkin", "1") != "1":
        return
    now = time.time()
    if now - float(db.get_setting("_last_checkin_retry_ts", "0") or 0) < 3600:
        return
    db.set_setting("_last_checkin_retry_ts", str(int(now)))
    for row in db.list_accounts():
        a = dict(row)
        if not a["enabled"] or a.get("checkin_today"):
            continue
        if not P.supports_checkin(a["platform"]):
            continue
        try:
            checkin_account(db.get_account(a["id"]))
        except Exception:
            pass


# --------------------------------------------------------------------------
# 号池调度
# --------------------------------------------------------------------------

def choose_account(platform: str | None = None, model: str | None = None,
                   exclude: list | None = None):
    """按策略选号。model 非空且属于「计费模型」且账号剩余积分低于阈值时，
    自动排除该账号（阈值门控），保证积分不足的账号不会被用来调用扣费模型。
    """
    strategy = db.get_setting("strategy", "least_used")
    min_quota = _quota_threshold_for(model)
    return db.pick_account(platform=platform or None, strategy=strategy,
                           exclude=exclude, min_quota=min_quota)


def _model_costs_credit(model: str | None) -> bool:
    """该模型是否会扣积分。free_models 列表中的视为免费（永远放行）。"""
    if not model:
        return True  # 未知模型按计费处理，保守拦截
    free = {x.strip() for x in (db.get_setting("free_models") or "").split(",") if x.strip()}
    return model not in free


def _quota_threshold_for(model: str | None) -> float:
    """该模型对应的积分门槛；免费模型返回 0（不门控）。"""
    if not _model_costs_credit(model):
        return 0.0
    try:
        return float(db.get_setting("credit_threshold", "1") or 0)
    except Exception:
        return 0.0


# 模型 → 平台 路由：完全由运行时注册表驱动（见 discovery.py + db 的
# discovered_models 设置），本文件不再内置任何静态目录。
#
# 设计原则（来自需求「程序不要内置任何模型，探测到什么模型就提供什么模型」）：
#   - 对外暴露与请求调度的模型目录 100% 来自真实账号探测，存于 DB 注册表；
#   - 客户端请求一个注册表外的模型时，model_route() 触发实时探测回退
#     （discovery.live_probe_model，跨三平台逐项探测），命中即写入注册表；
#   - 探测确认不存在的模型进入负面缓存（db.model_probe_failures，带 TTL），
#     避免对同一个不存在的模型反复发起昂贵真实请求；
#   - 未探测确认且实时探测也失败的模型不盲选账号，返回清晰的 503。

# 探测负面缓存有效期（秒）：过期后允许重新探测（例如上游新增了该模型）。
_PROBE_FAIL_TTL = 3600


def _live_probe_model(mid: str):
    """懒加载 discovery 模块，避免 proxy ↔ discovery 循环导入。"""
    import discovery
    return discovery.live_probe_model(mid)


def _probe_failed_recently(mid: str) -> bool:
    d = db.get_probe_failures()
    ts = d.get(mid)
    if not ts:
        return False
    if time.time() - float(ts) > _PROBE_FAIL_TTL:
        d.pop(mid, None)
        db.set_probe_failures(d)
        return False
    return True


def _mark_probe_failed(mid: str) -> None:
    d = db.get_probe_failures()
    d[mid] = time.time()
    db.set_probe_failures(d)


def _apply_route_priority(mid: str, platforms: list[str]) -> list[str]:
    """按「模型调用优先级」设置重排平台候选。

    设置 model_route_priority 为 JSON：
      {"default": ["workbuddy","trae","codebuddy"],
       "overrides": {"glm-5": ["trae","workbuddy","codebuddy"]}}
    单模型覆盖优先于全局默认；配置中未出现的平台排在最后并保持原相对顺序。
    """
    if not platforms:
        return platforms
    raw = (db.get_setting("model_route_priority") or "").strip()
    if not raw:
        return platforms
    try:
        cfg = json.loads(raw)
    except Exception:
        return platforms
    order = None
    if isinstance(cfg, dict):
        ov = cfg.get("overrides")
        if isinstance(ov, dict) and isinstance(ov.get(mid), list):
            order = ov[mid]
        elif isinstance(cfg.get("default"), list):
            order = cfg["default"]
    if not order:
        return platforms
    rank = {p: i for i, p in enumerate(order)}
    return sorted(platforms, key=lambda x: (rank.get(x, len(rank)),
                                            platforms.index(x)))


def model_route(model: str | None) -> tuple[str | None, list[str]]:
    """返回 (type, platforms)。

    - type ∈ {chat, image, audio}；platforms 为有序平台候选列表。
    - 注册表已收录：直接返回，不做网络调用。
    - 注册表未收录：触发实时探测回退；命中则写入注册表并返回；
      失败则记入负面缓存并返回 (None, [])，调用方据此拦截。
    """
    mid = str(model or "").strip()
    if not mid:
        return None, []
    reg = db.get_model_registry()
    rec = reg.get(mid)
    if isinstance(rec, dict) and rec.get("platforms"):
        return rec.get("type", "chat"), _apply_route_priority(mid, list(rec["platforms"]))
    # 未知模型：先看负面缓存，避免重复昂贵探测
    if _probe_failed_recently(mid):
        return None, []
    live = _live_probe_model(mid)
    if live and live.get("platforms"):
        return live.get("type", "chat"), _apply_route_priority(mid, list(live["platforms"]))
    _mark_probe_failed(mid)
    return None, []


def is_image_model(model: str | None) -> bool:
    """模型是否为图像生成类（走 /v2/images/generations，而非 chat 网关）。

    仅读注册表类型；未知模型默认 False（由 model_route 在请求时实时判定）。
    """
    return P.is_image_model(model)


def model_platform_candidates(model: str | None) -> list[str]:
    """返回模型的有序平台候选；未知且实时探测也失败的模型返回空列表。"""
    return model_route(model)[1]


def resolve_platform_candidates(model: str | None,
                                 requested_platform: str | None = None) -> list[str]:
    """合并 API Key 的显式绑定与模型路由。

    显式绑定优先级最高：绑定 Key 的请求永远只尝试绑定平台，不会因为模型
    在另一平台可用就越权切换。未绑定 Key 才使用模型路由（注册表 + 实时探测）。
    """
    bound = str(requested_platform or "").strip()
    if bound:
        return [bound]
    return model_platform_candidates(model)


def _is_model_route_error(status: int, text: str, model: str | None = None) -> bool:
    """只识别「模型未注册」类错误，避免把普通 400 当成可重试错误。"""
    if status != 400:
        return False
    s = str(text or "").lower()
    markers = ("11102", "service info not found", "model [", "model not found",
               "model does not exist", "11103", "backend [aiart]")
    return any(x in s for x in markers)


# 图像端点特有的错误判定
def _is_image_limit_error(status: int, text: str) -> bool:
    """图像任务队列上限（13212 RequestLimitExceeded.JobNumExceed）属可重试限流。"""
    if status != 400:
        return False
    s = str(text or "").lower()
    return "13212" in s or "jobnumexceed" in s or "requestlimitexceeded" in s


def _route_error_message(model: str, candidates: list[str], attempted: list[str]) -> str:
    if candidates:
        labels = ", ".join(P.PLATFORMS.get(x, {}).get("label", x) for x in candidates)
        tried = ", ".join(attempted) or "无"
        return ("模型 %s 在候选平台 [%s] 均不可用（已尝试: %s）。"
                "请确认上游实际模型 ID、账号套餐权限或重新运行「模型探测」。"
                % (model, labels, tried))
    return ("模型 %s 在号池各平台均未发现可用账号或未被探测确认。"
            "为避免把请求发送到错误账号，当前不会盲选平台；请先在号池添加该平台账号"
            "并运行一次「模型探测」（控制面板「模型」页 → 刷新模型目录）。" % model)


# --------------------------------------------------------------------------
# 上游请求
# --------------------------------------------------------------------------

def _upstream_headers(acc: dict, session_id: str | None = None) -> dict:
    return P.build_model_headers(
        acc["platform"], acc["access_token"],
        uid=acc.get("uid") or None,
        session_id=session_id,
        enterprise_id=acc.get("enterprise_id") or None,
        domain=acc.get("domain") or None,
        machine_id=acc.get("machine_id") or None,
        device_id=acc.get("device_id") or None,
    )


def _is_trae_acc(acc: dict) -> bool:
    """账号是否 Trae 渠道（统一 provider 判断，避免散落字符串比较）。"""
    return P._is_trae(acc.get("platform"))


def _upstream_url(acc: dict) -> str:
    if _is_trae_acc(acc):
        return P.chat_url(acc["platform"])
    override = (db.get_setting("model_base_override") or "").strip()
    return P.chat_url(acc["platform"], model_base=override or None)


def open_upstream(acc: dict, body: dict, timeout: object = 300):
    """向上游发起【流式】请求，返回 requests.Response(stream=True)。

    timeout 可为数字（总超时秒）或 (connect, read) 元组。模型探测场景传入
    短超时，避免上游卡住拖垮整个请求。
    """

    payload = P.normalize_body(acc["platform"], body,
                               system_prompt=db.get_setting("system_prompt") or None)
    headers = _upstream_headers(acc)
    headers["Accept"] = "text/event-stream"
    url = _upstream_url(acc)

    # 实测 copilot.tencent.com 偶发 SSLEOFError / 连接重置，属网络抖动而非账号问题，
    # 先在同一账号上做短重试，避免无谓地把好号排除掉。
    last = None
    for i in range(NET_RETRY + 1):
        try:
            r = requests.post(url, headers=headers, json=payload,
                              timeout=timeout, stream=True)
            r.encoding = "utf-8"
            r._hy3_platform = acc.get("platform") or ""
            return r
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = e
            if i < NET_RETRY:
                time.sleep(0.6 * (i + 1))
    raise last


def open_image_upstream(acc: dict, body: dict, timeout: object = 120):
    """向上游图像端点发起请求，返回 requests.Response（JSON，非流式）。

    timeout 可为数字（总超时秒）或 (connect, read) 元组。模型探测场景传入
    短超时，避免上游图像生成过慢拖垮整个探测流程。
    """

    payload = dict(body)
    headers = _upstream_headers(acc)
    headers["Accept"] = "application/json, text/event-stream, */*"
    url = P.image_url(acc["platform"])
    last = None
    for i in range(NET_RETRY + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            r.encoding = "utf-8"
            return r
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = e
            if i < NET_RETRY:
                time.sleep(0.6 * (i + 1))
    raise last


def _is_retryable(status: int) -> bool:
    return status in (401, 403, 429) or status >= 500


# --------------------------------------------------------------------------
# 对外：流式 / 非流式
# --------------------------------------------------------------------------

def _sse_lines(resp):
    """按行读取上游 SSE。

    上游 text/event-stream 不带 charset，requests 会退化成 ISO-8859-1 解码，
    导致中文变成乱码，这里强制按 UTF-8 解码。
    """
    for raw in resp.iter_lines(decode_unicode=False):
        if raw is None:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        yield raw


class SSEUsageTracker:
    """旁路解析原样透传的 SSE 字节，捕获最终帧的上游 usage。

    feed() 不改变客户端收到的字节；只维护跨 chunk 的行缓冲，解决
    iter_content() 任意切块导致 JSON 帧不完整的问题。最终 usage 通常位于
    finish_reason 帧，包含 prompt/completion/total_tokens 与真实 credit。
    """
    def __init__(self):
        self._buf = b""
        self.usage = None

    def feed(self, chunk) -> None:
        if not chunk:
            return
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        self._buf += chunk
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            self._parse_line(raw.rstrip(b"\r"))

    def finish(self) -> None:
        if self._buf:
            self._parse_line(self._buf.rstrip(b"\r"))
            self._buf = b""

    def _parse_line(self, raw: bytes) -> None:
        if not raw.startswith(b"data:"):
            return
        payload = raw[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            obj = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        usage = obj.get("usage") if isinstance(obj, dict) else None
        if isinstance(usage, dict):
            self.usage = usage


def extract_usage(obj) -> dict | None:
    """从 chat / image 上游 JSON 中提取 usage 字典。"""
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if isinstance(usage, dict):
        return usage
    data = obj.get("data")
    if isinstance(data, dict):
        usage = data.get("usage")
        if isinstance(usage, dict):
            return usage
    return None


def stream_chat(body: dict, platform: str | None = None, max_retry: int = 3):
    """按模型路由平台、按号池策略选号，并向上游发起流式请求。

    API Key 显式绑定平台时只尝试绑定平台；未绑定时按模型路由矩阵依次尝试。
    只有明确的「模型未注册」400 才会切换候选平台，普通参数 400 直接返回。
    """
    tried_accounts = []
    tried_platforms = []
    last_err = None
    last_failure_was_route = False
    any_account = False
    model = body.get("model") or default_model()

    # 解析模型类型与候选平台：注册表驱动，未知模型触发实时探测回退。
    # 图像生成模型必须走 /v1/images/generations，不能进入 chat 网关。
    mtype, _ = model_route(model)
    if mtype == "image":
        raise ProxyError(
            "模型 %s 是图像生成模型，请改用 /v1/images/generations 端点调用" % model,
            status=400)

    candidates = resolve_platform_candidates(model, platform)

    if not candidates:
        raise ProxyError(_route_error_message(model, candidates, []), status=503)

    per_platform_budget = max(1, int(max_retry or 0))

    for candidate in candidates:
        tried_platforms.append(candidate)

        for _ in range(per_platform_budget):
            row = choose_account(candidate, model=model, exclude=tried_accounts)
            if row is None:
                break
            any_account = True
            acc = ensure_token(row)
            tried_accounts.append(acc["id"])

            try:
                resp = open_upstream(acc, body)
            except Exception as e:
                last_err = "连接上游失败: %s" % e
                last_failure_was_route = False
                db.bump_account(acc["id"], False, last_err)
                continue

            if resp.status_code >= 400:
                status = resp.status_code
                text = ""
                try:
                    text = resp.text[:400]
                except Exception:
                    pass
                resp.close()
                last_err = "HTTP %s %s" % (status, text)
                last_failure_was_route = _is_model_route_error(status, text, model)
                db.bump_account(acc["id"], False, last_err)

                if status == 401:
                    # token 可能过期，强制刷新一次后让同平台的下一个账号继续尝试。
                    try:
                        ensure_token(db.get_account(acc["id"]), force=True)
                    except Exception:
                        pass

                if last_failure_was_route:
                    # 这是平台级能力错误，同平台换账号无意义，直接切换候选平台。
                    break
                if _is_retryable(status):
                    continue
                raise ProxyError(last_err, status=status)

            return acc, resp

    if last_failure_was_route:
        raise ProxyError(_route_error_message(model, candidates, tried_platforms), status=503)
    if last_err:
        raise ProxyError("所有候选账号均调用失败: %s" % last_err, status=502)

    # 候选平台存在启用账号但一个都选不出，通常是额度门槛拦截。
    enabled_count = sum(db.count_accounts(p, enabled=1) for p in candidates)
    if not any_account and _model_costs_credit(model) and enabled_count > 0:
        thr = db.get_setting("credit_threshold", "1")
        scope = ("API Key 绑定平台 %s" % platform) if platform else "模型候选平台"
        raise ProxyError("%s的账号积分不足（低于阈值 %s），无法调用模型 %s"
                         % (scope, thr, model), status=403)

    if platform:
        raise ProxyError("号池中没有 API Key 绑定平台 %s 的可用账号" % platform,
                         status=503)
    raise ProxyError("模型 %s 的候选平台号池中没有可用账号" % model, status=503)


def generate_image(body: dict, platform: str | None = None, max_retry: int = 3):
    """按模型路由平台、按号池策略选号，向图像端点发起请求。

    与 stream_chat 同构：先按模型路由矩阵选候选平台，平台内按号池策略选号；
    只有「模型未注册 / aiart 不支持」这类错误才切下一平台；图像任务队列上限
    （13212）按可重试限流处理，跳下一账号/平台。
    """
    tried_accounts = []
    tried_platforms = []
    last_err = None
    last_failure_was_route = False
    any_account = False
    model = body.get("model") or ""
    if not model:
        raise ProxyError("缺少 model 字段", status=400)
    # 解析模型类型：注册表驱动，未知模型触发实时探测回退。
    # 只有确实为图像类型的模型才允许走图像端点；文本/音频模型或探测失败的模型
    # 一律拦截（文本/音频用 chat 网关，探测失败则 503 不盲发）。
    mtype, candidates = model_route(model)
    if mtype is not None and mtype != "image":
        raise ProxyError(
            "模型 %s 不是图像生成模型（类型=%s），请使用 /v1/chat/completions 或确认模型 id"
            % (model, mtype), status=400)

    if not candidates:
        raise ProxyError(_route_error_message(model, candidates, tried_platforms), status=503)

    per_platform_budget = max(1, int(max_retry or 0))

    for candidate in candidates:
        tried_platforms.append(candidate)
        for _ in range(per_platform_budget):
            row = choose_account(candidate, model=model, exclude=tried_accounts)
            if row is None:
                break
            any_account = True
            acc = ensure_token(row)
            tried_accounts.append(acc["id"])

            try:
                resp = open_image_upstream(acc, body)
            except Exception as e:
                last_err = "连接上游失败: %s" % e
                last_failure_was_route = False
                db.bump_account(acc["id"], False, last_err)
                continue

            if resp.status_code >= 400:
                status = resp.status_code
                text = ""
                try:
                    text = resp.text[:400]
                except Exception:
                    pass
                resp.close()
                last_err = "HTTP %s %s" % (status, text)
                if _is_image_limit_error(status, text):
                    # 任务队列上限：当前账号/平台暂时打满，换下一账号继续
                    db.bump_account(acc["id"], False, last_err)
                    continue
                last_failure_was_route = _is_model_route_error(status, text, model)
                db.bump_account(acc["id"], False, last_err)
                if last_failure_was_route:
                    break
                if _is_retryable(status):
                    continue
                raise ProxyError(last_err, status=status)

            return acc, resp

    if last_failure_was_route:
        raise ProxyError(_route_error_message(model, candidates, tried_platforms), status=503)
    if last_err:
        raise ProxyError("所有候选账号均调用失败: %s" % last_err, status=502)

    enabled_count = sum(db.count_accounts(p, enabled=1) for p in candidates)
    if not any_account and enabled_count > 0:
        raise ProxyError("模型 %s 的候选平台号池中没有可用账号" % model, status=503)
    if platform:
        raise ProxyError("号池中没有 API Key 绑定平台 %s 的可用账号" % platform,
                         status=503)
    raise ProxyError("模型 %s 的候选平台号池中没有可用账号" % model, status=503)


def _trae_openai_sse_events(resp, model: str):
    """把 Trae SOLO SSE 逐事件转换成 OpenAI chat.completion.chunk SSE。"""
    resp_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    state = {"event": "", "data": []}
    finish = "stop"
    usage = None

    def chunk(delta=None, finish_reason=None, with_usage=False):
        obj = {
            "id": resp_id, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta or {},
                         "finish_reason": finish_reason}],
        }
        if with_usage:
            obj["usage"] = usage or {"prompt_tokens": 0, "completion_tokens": 0,
                                     "total_tokens": 0}
        return ("data: %s\n\n" % json.dumps(obj, ensure_ascii=False)).encode("utf-8")

    def dispatch(event, payload):
        nonlocal finish, usage
        if not payload:
            return None
        try:
            obj = json.loads(payload)
        except Exception:
            return None
        if event == "output":
            delta = {}
            if obj.get("response"):
                delta["content"] = obj["response"]
            if obj.get("reasoning_content"):
                delta["reasoning_content"] = obj["reasoning_content"]
            return chunk(delta) if delta else None
        if event == "token_usage":
            usage = obj
            return None
        if event == "done":
            finish = obj.get("finish_reason") or finish
            return None
        return None

    yield chunk({"role": "assistant"})
    for line in _sse_lines(resp):
        if line is None:
            continue
        if line.startswith("event:"):
            state["event"] = line[6:].strip()
        elif line.startswith("data:"):
            state["data"].append(line[5:])
        elif line == "":
            if state["event"] or state["data"]:
                out = dispatch(state["event"], "".join(state["data"]).strip())
                state["event"], state["data"] = "", []
                if out:
                    yield out
    if state["event"] or state["data"]:
        out = dispatch(state["event"], "".join(state["data"]).strip())
        if out:
            yield out
    yield chunk(delta={}, finish_reason=finish, with_usage=True)
    yield b"data: [DONE]\n\n"


def probe_account(account_row, model: str, timeout=(5, 30)) -> dict:
    """对指定账号用指定模型发一次真实测活请求（固定 ping / max_tokens=1）。

    锁定该账号直连上游（不走号池调度），用于判断单账号死活。
    返回 {ok, ms, model, platform, reply|error}；不写调用计数。
    """
    acc = ensure_token(account_row)
    body = {"model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1}
    t0 = time.time()
    resp = None
    try:
        resp = open_upstream(acc, body, timeout=timeout)
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "platform": acc["platform"], "model": model,
                "error": "连接上游失败: %s" % e}
    try:
        if resp.status_code >= 400:
            text = ""
            try:
                text = resp.text[:300]
            except Exception:
                pass
            return {"ok": False, "ms": int((time.time() - t0) * 1000),
                    "platform": acc["platform"], "model": model,
                    "error": "HTTP %s %s" % (resp.status_code, text)}
        out = (aggregate_trae_stream(resp, model) if _is_trae_acc(acc)
               else aggregate_stream(resp, model))
        msg = ((out.get("choices") or [{}])[0].get("message") or {})
        reply = str(msg.get("content") or "")[:120]
        return {"ok": True, "ms": int((time.time() - t0) * 1000),
                "platform": acc["platform"], "model": model, "reply": reply}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "platform": acc["platform"], "model": model,
                "error": "响应解析失败: %s" % e}
    finally:
        try:
            resp.close()
        except Exception:
            pass


def aggregate_stream(resp, model: str) -> dict:
    """把上游 SSE 流聚合成一个标准的 OpenAI 非流式响应体。"""
    content_parts = []
    reasoning_parts = []
    finish_reason = "stop"
    resp_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    usage = None
    role = "assistant"

    for line in _sse_lines(resp):
        if not line or not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            j = json.loads(chunk)
        except Exception:
            continue

        resp_id = j.get("id") or resp_id
        created = j.get("created") or created
        model = j.get("model") or model
        if j.get("usage"):
            usage = j["usage"]

        for ch in (j.get("choices") or []):
            delta = ch.get("delta") or ch.get("message") or {}
            if delta.get("role"):
                role = delta["role"]
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

    message = {"role": role, "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)

    out = {
        "id": resp_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        out["usage"] = usage
    else:
        # 上游未返回 usage 时只能标记为未知，不能按字符数伪造 token。
        # 保留 OpenAI 兼容字段，但日志侧会把它视为非真实积分来源。
        out["usage"] = {"prompt_tokens": 0, "completion_tokens": 0,
                        "total_tokens": 0}
    return out


def aggregate_trae_stream(resp, model: str) -> dict:
    """把 Trae SOLO SSE 聚合成标准 OpenAI 非流式响应。"""
    agg = trae.SSEAggregator()
    for line in _sse_lines(resp):
        if line is not None:
            agg.feed_line(line)
    agg.finish()
    return agg.result(model)


    """把上游 SSE 流聚合成一个标准的 OpenAI 非流式响应体。"""
    content_parts = []
    reasoning_parts = []
    finish_reason = "stop"
    resp_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    usage = None
    role = "assistant"

    for line in _sse_lines(resp):
        if not line or not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            j = json.loads(chunk)
        except Exception:
            continue

        resp_id = j.get("id") or resp_id
        created = j.get("created") or created
        model = j.get("model") or model
        if j.get("usage"):
            usage = j["usage"]

        for ch in (j.get("choices") or []):
            delta = ch.get("delta") or ch.get("message") or {}
            if delta.get("role"):
                role = delta["role"]
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

    message = {"role": role, "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)

    out = {
        "id": resp_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        out["usage"] = usage
    else:
        # 上游未返回 usage 时只能标记为未知，不能按字符数伪造 token。
        # 保留 OpenAI 兼容字段，但日志侧会把它视为非真实积分来源。
        out["usage"] = {"prompt_tokens": 0, "completion_tokens": 0,
                        "total_tokens": 0}
    return out


def default_model() -> str:
    """返回已探测注册表中的默认 chat 模型；没有可用项时返回空字符串。

    管理员配置只在模型仍存在于运行时注册表时生效。这样旧数据库里残留的
    默认模型不会绕过真实探测，也不需要在程序中硬编码任何模型 ID。
    """
    registry = db.get_model_registry()
    configured = str(db.get_setting("default_model", "") or "").strip()
    rec = registry.get(configured)
    if configured and isinstance(rec, dict) and rec.get("platforms") \
            and rec.get("type", "chat") == "chat":
        return configured
    for mid in sorted(registry):
        rec = registry.get(mid)
        if isinstance(rec, dict) and rec.get("platforms") \
                and rec.get("type", "chat") == "chat":
            return mid
    return ""


def list_models() -> list:
    """返回真实探测注册表中的全部模型，不合并任何配置或静态默认值。"""
    return P.catalog_models()
