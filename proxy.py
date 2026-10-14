# -*- coding: utf-8 -*-
"""Agent2API - 反代引擎

核心职责：
  1. 令牌保活：按 expires_at 提前刷新（refresh 走 X-Refresh-Token 头）
  2. 号池调度：least_used / round_robin / random，失败自动换号重试
  3. 对话反代：上游【只支持流式】，非流式请求由本层聚合后返回标准 OpenAI 响应
  4. 每日维护：普通签到与国际活动/奖励核验，共用手动、批量及定时入口
"""
from __future__ import annotations

import datetime as _dt
import copy
import json
import logging
import math
import re
import threading
import time
import uuid

import db
import credentials
import daily_rewards
import rate_limits
import providers as P
import requests
import trae

_refresh_locks: dict[object, threading.Lock] = {}
_checkin_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()

_CHECKIN_LOG = logging.getLogger("agent2api.checkin")


def _checkin_log(acc, stage: str, result: dict | None = None, error=None) -> None:
    """记录逐账号签到链路，不记录 access/refresh token。"""
    result = result if isinstance(result, dict) else {}
    raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
    code = raw.get("code", result.get("code", ""))
    message = (result.get("msg") or raw.get("message") or raw.get("msg") or
               raw.get("error_msg") or "")
    message = str(message).replace("\n", " ")[:180]
    fields = {
        "account_id": acc.get("id"),
        "platform": acc.get("platform"),
        "stage": stage,
        "checked": result.get("today_checked_in"),
        "credit": result.get("today_credit", result.get("claimed_credit", "")),
        "code": code,
        "message": message,
    }
    if error is not None:
        fields["error"] = str(error).replace("\n", " ")[:240]
        _CHECKIN_LOG.error("checkin_trace %s", fields)
    else:
        _CHECKIN_LOG.warning("checkin_trace %s", fields)

# 上游网络抖动时，在同一账号上的重试次数
NET_RETRY = 2


def _lock_for(account_id: object) -> threading.Lock:
    with _locks_guard:
        lk = _refresh_locks.get(account_id)
        if lk is None:
            lk = threading.Lock()
            _refresh_locks[account_id] = lk
        return lk


def _checkin_lock_for(account_id: int) -> threading.Lock:
    with _locks_guard:
        lk = _checkin_locks.get(account_id)
        if lk is None:
            lk = threading.Lock()
            _checkin_locks[account_id] = lk
        return lk


def _norm_ts(v) -> int:
    """兼容历史调用方；统一时间格式。"""
    return credentials.epoch(v)


class ProxyError(Exception):
    def __init__(self, message, status=500, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


# --------------------------------------------------------------------------
# 令牌保活
# --------------------------------------------------------------------------

def ensure_token(account_row, force: bool = False) -> dict:
    """统一保活：补齐有效期、提前续期、持久化轮换、退避；force 失败明确抛错。"""
    original = dict(account_row)
    aid = original["id"]
    with _lock_for(db.credential_scope(original)):
        fresh = db.get_account(aid)
        if fresh is None:
            raise ProxyError("账号已删除", status=404)
        acc = dict(fresh)
        now = int(time.time())
        exp = credentials.expiry(acc["access_token"], acc.get("expires_at"))
        if exp != acc.get("expires_at"):
            db.update_account(aid, expires_at=exp)
            acc["expires_at"] = exp
        if not force and db.get_setting("auto_refresh", "1") != "1":
            return acc
        # 等锁期间其他请求已轮换，401 的旧快照不应再次轮换同一会话。
        if ((acc["access_token"] != original.get("access_token") or
             int(acc.get("last_refresh_at") or 0) > int(original.get("last_refresh_at") or 0))
                and acc.get("last_refresh_at") and (not exp or exp > now)):
            return acc
        ahead = max(0, int(db.get_setting("refresh_ahead", "900") or 900))
        rt = (acc.get("refresh_token") or "").strip()
        rt_exp = credentials.expiry(rt)
        deadlines = [v for v in (exp, rt_exp) if v]
        deadline = min(deadlines, default=0)
        need = force or (deadline and now >= deadline - ahead) or (
            not exp and now >= int(acc.get("last_refresh_at") or 0) + 3600)
        if not need:
            return acc
        if not rt:
            if force or (exp and exp <= now):
                raise ProxyError("缺少刷新凭证，请重新登录或导入完整凭证", status=401)
            return acc
        if not force and now < int(acc.get("refresh_retry_at") or 0):
            if exp and exp <= now:
                raise ProxyError(acc.get("refresh_error") or "续期重试等待中", status=502)
            return acc
        try:
            res = P.refresh_token(acc["platform"], acc["access_token"], rt,
                                  domain=acc.get("domain") or None)
            token = res.get("access_token")
            if not isinstance(token, str) or not token.strip():
                raise P.ProviderError("刷新响应缺少 access token")
            new_exp = credentials.expiry(token, res.get("expires_at"))
            if new_exp and new_exp <= now:
                raise P.ProviderError("刷新返回的凭证已过期")
        except Exception as e:
            error = "续期失败: " + credentials.redact(e, acc["access_token"], rt)
            failures = int(acc.get("refresh_failures") or 0) + 1
            fields = dict(last_refresh_attempt=now, refresh_error=error,
                          refresh_failures=failures,
                          refresh_retry_at=now + min(3600, 60 * 2 ** min(failures - 1, 6)))
            acc = db.save_refresh_result(acc, fields)
            logging.getLogger("agent2api.refresh").warning(
                "refresh_trace account_id=%s platform=%s stage=failed error=%s",
                aid, acc["platform"], error)
            if force or not exp or exp <= now:
                raise ProxyError(error, status=502) from None
            return acc  # 未过期 access 仍可使用，但独立续期错误不被额度刷新覆盖。
        fields = dict(access_token=token, refresh_token=res.get("refresh_token") or rt,
                      expires_at=new_exp, last_refresh_at=now, last_refresh_attempt=now,
                      refresh_error="", refresh_failures=0, refresh_retry_at=0)
        if res.get("domain"):
            fields["domain"] = res["domain"]
        acc = db.save_refresh_result(acc, fields)
        if not acc:
            raise ProxyError("账号已删除", status=404)
        logging.getLogger("agent2api.refresh").warning(
            "refresh_trace account_id=%s platform=%s stage=success expires_at=%s",
            aid, acc["platform"], new_exp)
        return acc


def refresh_quota(account_row) -> dict:
    """刷新账号额度并落库；失败时标记账号并保留上一份成功快照。"""
    acc = ensure_token(account_row)
    try:
        s = P.collect_quota(acc["platform"], acc["access_token"],
                            uid=acc.get("uid") or None,
                            enterprise_id=acc.get("enterprise_id") or None,
                            domain=acc.get("domain") or None,
                            # Trae 的 UG 额度接口与签到共用设备头；
                            # 凭证导入生成的孤立 device_id 会返回错误额度。
                            device_id=(_checkin_device_id(acc)
                                       if _is_trae_acc(acc)
                                       else acc.get("device_id") or None))
        if s.get("errors"):
            raise ProxyError("额度刷新失败：%s" % "; ".join(str(x) for x in s["errors"]),
                             status=502)
    except ProxyError as e:
        db.update_account(acc["id"], status="error", status_reason=str(e),
                          last_error=str(e)[:400])
        raise
    except Exception as e:
        msg = "额度刷新失败：%s" % e
        db.update_account(acc["id"], status="error", status_reason=msg,
                          last_error=msg[:400])
        raise ProxyError(msg, status=502)

    db.save_quota(acc["id"], s)
    db.update_account(acc["id"], status="active", status_reason="", last_error="")
    return s


# --------------------------------------------------------------------------
# 每日签到
# --------------------------------------------------------------------------

def _checkin_device_id(acc):
    """签到 X-Device-Id：用 uid（2026-08-31 全号池 6/6 实测 code 0）。

    实测对比：扫码导入的老号 device_id 可用；手填 token 导入的号 device_id
    是未注册的孤儿码 → 9074。uid 对两类账号均稳定成功，因此统一用 uid，
    规避 device_id 来源不一致的坑。
    兼容 dict 与 sqlite3.Row 两种入参（老代码只按 dict 写）。
    """
    def g(k):
        return acc[k] if hasattr(acc, "keys") and k in acc.keys() else acc.get(k)

    if P._is_trae(g("platform")):
        return str(g("uid") or "").strip() or g("device_id") or None
    return g("device_id") or None


def _today_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def checkin_marked_today(acc) -> bool:
    """判断本地签到标记是否属于今天，防止 checkin_today 跨日永久为 1。

    上游部分平台的 last_checkin_date 偶尔滞后，因此同时接受：
    - checkin_last_date == 今天；或
    - 最近一次状态刷新 checkin_updated_at 发生在今天。
    两者都不是今天时，旧的 checkin_today 必须视为 False。
    """
    def g(k, default=None):
        if hasattr(acc, "keys") and k in acc.keys():
            return acc[k]
        return acc.get(k, default)

    if not int(g("checkin_today", 0) or 0):
        return False
    today = _today_str()
    if str(g("checkin_last_date", "") or "") == today:
        return True
    ts = int(g("checkin_updated_at", 0) or 0)
    if ts:
        try:
            return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") == today
        except Exception:
            pass
    return False


def _persist_checkin_status(acc: dict, st: dict) -> None:
    """把一次上游签到状态响应落库；所有签到入口共用，避免各自写出矛盾状态。"""
    today = _today_str()
    checked = bool(st.get("today_checked_in"))
    db.update_account(
        acc["id"],
        checkin_today=1 if checked else 0,
        # -1 表示上游未提供连签字段；前端隐藏，不伪造“连续 0 天”。
        checkin_streak=(-1 if st.get("streak_days") is None
                        else int(st.get("streak_days") or 0)),
        checkin_last_date=(st.get("last_checkin_date")
                           or (today if checked else "")),
        checkin_credit=int(st.get("today_credit") or 0),
        checkin_updated_at=int(time.time()),
    )


def _upstream_checkin_status(acc: dict) -> dict:
    """查询该账号上游签到真值并落库。"""
    try:
        st = P.fetch_checkin_status(
            acc["platform"], acc["access_token"],
            uid=acc.get("uid") or None,
            enterprise_id=acc.get("enterprise_id") or None,
            domain=acc.get("domain") or None,
            device_id=_checkin_device_id(acc),
        )
    except Exception as e:
        _checkin_log(acc, "status", error=e)
        raise
    _checkin_log(acc, "status", result=st)
    _persist_checkin_status(acc, st)
    return st


def refresh_checkin_status(account_row) -> dict:
    """查询签到状态并落库。返回归一化状态字典。"""
    acc = ensure_token(account_row)
    if not P.supports_checkin(acc["platform"]):
        return {"supported": False, "today_checked_in": False,
                "streak_days": 0, "claimable": False}
    if acc["platform"] in db.INTERNATIONAL_CHANNELS:
        return daily_rewards.run(acc, submit=False)
    return _upstream_checkin_status(acc)


def checkin_account(account_row) -> dict:
    """核验上游状态后领取；单号、批量、自动签到统一走这条链。

    同一账号的签到请求串行化，避免手动点击与后台自动签到同时通过「未签到」
    检查后重复领取。锁内每次仍重新查询上游，状态真值不依赖本地缓存。
    """
    acc = ensure_token(account_row)
    if acc["platform"] in db.INTERNATIONAL_CHANNELS:
        return daily_rewards.run(acc, submit=True)
    with _checkin_lock_for(acc["id"]):
        return _checkin_account_locked(acc)


def _checkin_account_locked(acc: dict) -> dict:
    """在账号签到锁内执行一次完整的状态核验、领取与回读。"""
    if not P.supports_checkin(acc["platform"]):
        raise ProxyError("该平台不支持签到", status=400)

    try:
        before = _upstream_checkin_status(acc)
    except Exception as e:
        # 状态查询失败时停止本次流程，避免跳过核验后把结果误报成成功。
        raise ProxyError("签到状态查询失败：%s" % e, status=502)

    if before and before.get("today_checked_in"):
        try:
            refresh_quota(db.get_account(acc["id"]))
        except Exception:
            pass
        return {
            "ok": True,
            "skipped": True,
            "reason": "上游确认今日已签到",
            "claimed_credit": 0,
            "today_checked_in": True,
            "streak_days": int(before.get("streak_days") or 0),
            "today_credit": float(before.get("today_credit") or 0),
        }

    try:
        res = P.do_checkin(
            acc["platform"], acc["access_token"],
            uid=acc.get("uid") or None,
            enterprise_id=acc.get("enterprise_id") or None,
            domain=acc.get("domain") or None,
            device_id=_checkin_device_id(acc),
        )
    except Exception as e:
        _checkin_log(acc, "claim", error=e)
        raise
    _checkin_log(acc, "claim", result=res)

    try:
        refresh_quota(db.get_account(acc["id"]))
    except Exception:
        pass

    claim_checked = res.get("today_checked_in")
    try:
        after = _upstream_checkin_status(acc)
        # 部分 Trae claim 响应 claimed_credit=0，但状态接口明确显示今日 +200。
        if not float(res.get("claimed_credit") or 0):
            res["claimed_credit"] = float(after.get("today_credit") or 0)
        # 领取响应或状态回读任一明确确认即可；两边都明确 false 时必须报失败，
        # 不能仅凭 HTTP/code=0 把本地状态伪造成已签到。
        confirmed = bool(after.get("today_checked_in")) or claim_checked is True
        res["today_checked_in"] = confirmed
        res["streak_days"] = int(after.get("streak_days") or res.get("streak_days") or 0)
        if not confirmed:
            raise ProxyError("上游领取完成但未确认签到成功", status=502)
    except ProxyError:
        raise
    except Exception as e:
        _checkin_log(acc, "status_after_claim", error=e)
        # 领取请求已经发出，但回读失败时保留本地旧状态，交给用户重试，
        # 避免卡片出现“已签到”的假状态。
        raise ProxyError("签到请求已发送，但状态回读失败：%s" % e, status=502)
    return res


def checkin_all(silent: bool = False, platform: str | None = None) -> dict:
    """遍历所有支持签到的账号（调用开关不影响签到），逐个领取。platform 指定时只签该渠道。"""
    results, identities = [], {}
    want = str(platform or "").strip()
    for row in db.list_accounts():
        a = dict(row)
        if want and a["platform"] != want:
            continue
        if not P.supports_checkin(a["platform"]):
            continue
        try:
            res = checkin_account(db.get_account(a["id"]))
            if a['platform'] in db.INTERNATIONAL_CHANNELS:
                identity = db.maintenance_identity(a)
                item = {"id": a["id"], **res, 'deduplicated': identity in identities}
                if res.get('ok'):
                    previous = identities.get(identity)
                    identities[identity] = {**item, 'skipped': bool(item.get('skipped'))
                        and (previous is None or previous.get('skipped', False))}
            else:
                item = {"id": a["id"], "ok": res.get('ok', True),
                        "claimed_credit": res.get("claimed_credit", 0),
                        "streak_days": res.get("streak_days", 0)}
                if res.get("skipped"):
                    item.update(skipped=True, reason=res.get("reason") or "上游确认今日已签到")
            results.append(item)
        except Exception as e:
            results.append({"id": a["id"], "ok": False,
                            "error": credentials.redact(e, a.get('access_token'), a.get('refresh_token'))})
    summary = dict(new_checkin=0, skipped=0, pending_verification=0, pending_reward=0,
                   credited=0, not_applicable=0, failed=0, new_credit=0)
    for item in results:
        if not item.get('ok'):
            summary['failed'] += 1
        elif item.get('checkin_kind') == 'international_activity':
            # 奖励记录已经在持久层去重；第二张卡片晚查到的新奖励同样要计入。
            summary['new_credit'] += item['daily_reward'].get('new_credit', 0)
        else:
            summary['skipped' if item.get('skipped') else 'new_checkin'] += 1
    for item in identities.values():
        # 同一身份取本批最新核验阶段，而非固定采用第一张卡片的旧结果。
        state = item['daily_reward']
        if state['activity_status'] == 'unavailable':
            summary['not_applicable'] += 1
        elif state['reward_status'] == 'credited':
            summary['credited'] += 1
        elif state['activity_status'] == 'confirmed':
            summary['pending_reward'] += 1
        else:
            summary['pending_verification'] += 1
        if item.get('skipped'):
            summary['skipped'] += 1
    return {"ok": True, "results": results, "summary": summary}


def _checkin_time_reached():
    current = _dt.datetime.now()
    try:
        hh, mm = (int(x) for x in (db.get_setting('checkin_time', '00:00') or '00:00').split(':'))
        scheduled = current.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (ValueError, TypeError):
        scheduled = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return current >= scheduled


def run_auto_checkin_if_due():
    """由后台维护线程调用：若开启自动签到且今日尚未执行，则全部签到一次。"""
    if db.get_setting("auto_checkin", "1") != "1":
        return
    today = _dt.datetime.now().strftime("%Y-%m-%d")
    if db.get_setting("_last_checkin_run_date", "") == today:
        return

    if not _checkin_time_reached():
        return

    checkin_all(silent=True)
    db.set_setting("_last_checkin_run_date", today)


def retry_failed_checkins():
    """每小时补签一次今日仍未签到的账号（TraeWork 上游量控/风控窗口解除后
    可自动补领；其它平台失败也顺带重试）。由后台维护线程调用。"""
    if db.get_setting("auto_checkin", "1") != "1":
        return
    if not _checkin_time_reached():
        return
    now = time.time()
    if now - float(db.get_setting("_last_checkin_retry_ts", "0") or 0) < 3600:
        return
    db.set_setting("_last_checkin_retry_ts", str(int(now)))
    for row in db.list_accounts():
        a = dict(row)
        # 不能在这里信任本地标记：上一次状态查询可能失败，但仍留下了
        # 当天的缓存。统一交给 checkin_account() 再查上游，已签到的账号
        # 会由上游幂等跳过，未签到的账号才会领取。
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
                           exclude=exclude, min_quota=min_quota, model=model)


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


def normalize_model_registry(registry: dict | None) -> dict:
    """清理模型注册表中的失效渠道引用。

    旧版本曾保存过 ``codebuddy_cn``。该渠道已经合并进 WorkBuddy，若继续
    留在注册表里，它会在模型关闭过滤前被当成一个仍可用的平台，形成幽灵
    路由并让已全关模型继续出现在 ``/v1/models``。
    """
    out = {}
    active = set(P.PLATFORMS)
    aliases = {"codebuddy_cn": "workbuddy", "ide": "codebuddy"}
    for mid, rec in (registry or {}).items():
        if not isinstance(rec, dict):
            continue
        platforms = []
        for platform in rec.get("platforms") or []:
            platform = aliases.get(str(platform), str(platform))
            if platform in active and platform not in platforms:
                platforms.append(platform)
        if not platforms:
            continue
        clean = dict(rec)
        clean["platforms"] = platforms
        if clean.get("type") not in ("chat", "image", "audio"):
            clean["type"] = "chat"
        out[str(mid)] = clean
    return out


def _priority_token(value) -> str:
    """用于兼容历史把模型展示名写进优先级配置的情况。"""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _resolve_priority_model(raw_model, registry: dict) -> str:
    raw_model = str(raw_model or "").strip()
    if not raw_model:
        return ""
    if raw_model in registry:
        return raw_model
    needle = _priority_token(raw_model)
    matches = []
    for mid in registry:
        if needle in (_priority_token(mid), _priority_token(P.model_name(mid))):
            matches.append(mid)
    return matches[0] if len(matches) == 1 else raw_model


def _normalize_platform_order(order, active=None) -> list[str]:
    active = list(active or P.PLATFORMS)
    out = []
    for platform in order or []:
        platform = str(platform)
        if platform in active and platform not in out:
            out.append(platform)
    out.extend(platform for platform in active if platform not in out)
    return out


def normalize_route_priority(raw) -> dict:
    """返回只含当前渠道、且始终为全排列的优先级配置。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "")
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    active = list(P.PLATFORMS)
    default = _normalize_platform_order(raw.get("default"), active)
    registry = normalize_model_registry(db.get_model_registry())
    overrides = {}
    raw_overrides = raw.get("overrides")
    if isinstance(raw_overrides, dict):
        for raw_model, order in raw_overrides.items():
            model = _resolve_priority_model(raw_model, registry)
            if model:
                overrides[model] = _normalize_platform_order(order, active)
    return {"default": default, "overrides": overrides}


def migrate_runtime_state() -> None:
    """启动时一次性修复旧注册表/优先级设置，后续读路径仍保持防御性过滤。"""
    raw_registry = db.get_model_registry()
    registry = normalize_model_registry(raw_registry)
    if registry != raw_registry:
        db.set_model_registry(registry)

    raw_priority = db.get_setting("model_route_priority", "") or ""
    priority = normalize_route_priority(raw_priority)
    canonical = json.dumps(priority, ensure_ascii=False, separators=(",", ":"))
    try:
        old_obj = json.loads(raw_priority) if raw_priority else None
    except Exception:
        old_obj = None
    if old_obj != priority:
        db.set_setting("model_route_priority", canonical)


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
    cfg = normalize_route_priority(db.get_setting("model_route_priority") or "")
    order = cfg.get("overrides", {}).get(mid) or cfg.get("default")
    if not order:
        return platforms
    rank = {p: i for i, p in enumerate(order)}
    return sorted(platforms, key=lambda x: (rank.get(x, len(rank)),
                                            platforms.index(x)))


def normalize_model_disabled_map(raw) -> dict:
    """归一化渠道×模型开关并迁移旧渠道别名。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "")
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    aliases = {"codebuddy_cn": "workbuddy", "ide": "codebuddy"}
    active = set(P.PLATFORMS)
    out = {}
    for raw_platform, ids in raw.items():
        platform = aliases.get(str(raw_platform), str(raw_platform))
        if platform not in active or not isinstance(ids, list):
            continue
        bucket = out.setdefault(platform, set())
        bucket.update(str(model).strip() for model in ids if str(model).strip())
    return {platform: sorted(ids) for platform, ids in sorted(out.items())}


def get_model_disabled_map() -> dict:
    """渠道×模型开关：{platform_key: set(model_id)}。

    设置 model_platform_disabled 为 JSON {"workbuddy": ["glm-5.3"], ...}；
    开关是全局的：真实调用 / Key 绑定 / 对外清单统一生效，测活与探测不受影响。
    """
    cfg = normalize_model_disabled_map(db.get_setting("model_platform_disabled") or "")
    return {platform: set(ids) for platform, ids in cfg.items()}


def _filter_disabled_platforms(mid: str, platforms: list[str]) -> list[str]:
    """从候选平台中剔除「该模型已被管理员关闭」的渠道。"""
    if not platforms:
        return platforms
    disabled = get_model_disabled_map()
    if not disabled:
        return platforms
    return [p for p in platforms if mid not in disabled.get(p, ())]


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
    reg = normalize_model_registry(db.get_model_registry())
    rec = reg.get(mid)
    if isinstance(rec, dict) and rec.get("platforms"):
        plats = _apply_route_priority(mid, list(rec["platforms"]))
        return rec.get("type", "chat"), _filter_disabled_platforms(mid, plats)
    # 未知模型：先看负面缓存，避免重复昂贵探测
    if _probe_failed_recently(mid):
        return None, []
    live = _live_probe_model(mid)
    if live and live.get("platforms"):
        plats = _apply_route_priority(mid, list(live["platforms"]))
        return live.get("type", "chat"), _filter_disabled_platforms(mid, plats)
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
        if bound not in P.PLATFORMS:
            return []
        # 先经过模型注册表/实时探测，再应用 Key 绑定。否则绑定 Key 会把
        # 未确认的任意模型硬发到该平台，既绕过模型目录，也绕过平台能力边界。
        _model_type, candidates = model_route(model)
        if bound not in candidates:
            return []
        # 模型开关是全局的：绑定的渠道若被管理员关闭该模型，同样拦截。
        return _filter_disabled_platforms(str(model or "").strip(), [bound])
    return model_platform_candidates(model)


def _is_model_route_error(status: int, text: str, model: str | None = None) -> bool:
    """只识别「模型未注册」类错误，避免把普通 400 当成可重试错误。"""
    if status != 400:
        return False
    s = str(text or "").lower()
    markers = ("11102", "service info not found", "model [", "model not found",
               "model does not exist", "11103", "backend [aiart]")
    return any(x in s for x in markers)


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


def _sse_business_error(obj) -> str:
    """从 HTTP 200 的 SSE JSON 中提取业务错误，避免空 choices 被当成成功。"""
    if not isinstance(obj, dict):
        return ""
    err = obj.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("msg") or err.get("code")
        if msg:
            return str(msg)
    elif err:
        return str(err)
    code = obj.get("code")
    if code is None:
        return ""
    try:
        ok = int(code) in (0, 200)
    except (TypeError, ValueError):
        ok = False
    if ok:
        return ""
    msg = obj.get("message") or obj.get("msg") or obj.get("error_msg") or "业务错误"
    return "code=%s: %s" % (code, msg)


class _PrefetchedSSEResponse:
    """保留预读 SSE 行的响应包装器，后续流式/聚合读取不会丢首帧。"""

    def __init__(self, resp, prefetched, remaining):
        self._resp = resp
        self._prefetched = list(prefetched)
        self._remaining = remaining

    def __getattr__(self, name):
        return getattr(self._resp, name)

    def iter_lines(self, decode_unicode=False):
        for raw in self._prefetched:
            if decode_unicode and isinstance(raw, bytes):
                yield raw.decode("utf-8", errors="replace")
            else:
                yield raw
        for raw in self._remaining:
            if decode_unicode and isinstance(raw, bytes):
                yield raw.decode("utf-8", errors="replace")
            else:
                yield raw

    def iter_content(self, chunk_size=None):
        # SSE 行重组为标准 LF 分隔字节；空行会形成事件间的双换行。
        for raw in self.iter_lines(decode_unicode=False):
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            yield (raw or b"") + b"\n"

    def close(self):
        return self._resp.close()


def _generation_sse_lines(resp):
    """合并跨 data 行的 JSON，供预读和后续协议适配共用；单行事件保持原样。"""
    pending = []
    for raw in resp.iter_lines(decode_unicode=False):
        text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw or '')
        if text.startswith('data:'):
            pending.append(text[5:].lstrip())
            payload = '\n'.join(pending)
            try:
                obj = json.loads(payload) if payload != '[DONE]' else None
            except (ValueError, TypeError):
                if len(payload) > 1048576:
                    raise ValueError('上游 SSE 数据帧超长且不是有效 JSON')
                continue
            if len(pending) == 1:
                yield raw
            else:
                yield ('data: ' + json.dumps(obj, ensure_ascii=False)).encode('utf-8')
            pending = []
        else:
            if text == '' or text.startswith('event:'):
                for part in pending:
                    yield ('data: ' + part).encode('utf-8')
                pending = []
            yield raw
    for part in pending:
        yield ('data: ' + part).encode('utf-8')
    # 兼容最后一帧缺少空行的上游；Trae 事件同样需要在 EOF 派发。
    yield b''


def _prefetch_sse_evidence(resp, is_trae: bool = False, is_image: bool = False):
    """预读首个有意义 SSE 事件，返回 (可重放响应, 业务错误)。"""
    if not hasattr(resp, "iter_lines"):
        return resp, ""
    remaining = iter(_generation_sse_lines(resp))
    buffered = []
    event = ""
    event_data = []

    def decode(raw):
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)

    for raw in remaining:
        buffered.append(raw)
        line = decode(raw)
        if line.startswith("event:"):
            event = line[6:].strip()
        if is_trae:
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                event_data.append(line[5:].strip())
            elif line == "" and (event or event_data):
                payload = "".join(event_data).strip()
                try:
                    obj = json.loads(payload) if payload else {}
                except Exception:
                    obj = {}
                if event == 'token_usage' and isinstance(obj, dict):
                    resp._hy3_usage = obj
                if event == "error":
                    err = _sse_business_error(obj) or payload or "Trae SSE error"
                    wrapped = _PrefetchedSSEResponse(resp, buffered, remaining)
                    wrapped._hy3_error_payload = obj
                    return wrapped, err
                # 只有 output 才证明模型真正产生了响应；token_usage/done 单独出现
                # 仍是空流，继续读到结尾后按失败换号。
                if event == "output" and isinstance(obj, dict) and (obj.get("response") or obj.get("reasoning_content")):
                    return _PrefetchedSSEResponse(resp, buffered, remaining), ""
                event, event_data = "", []
        elif line.startswith("data:"):
            payload = line[5:].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                return (_PrefetchedSSEResponse(resp, buffered, remaining),
                        "HTTP 200 empty SSE")
            try:
                obj = json.loads(payload)
            except (ValueError, TypeError):
                if event != 'error':
                    continue
                obj = {'error': {'message': payload}}
            usage = extract_usage(obj)
            if usage is not None:
                resp._hy3_usage = usage
            err = _sse_business_error(obj) or (payload if event == 'error' else '')
            if err:
                wrapped = _PrefetchedSSEResponse(resp, buffered, remaining)
                wrapped._hy3_error_payload = obj
                return wrapped, err
            if isinstance(obj, dict) and (
                    (is_image and obj.get('data')) or
                    any(isinstance(ch, dict) and any(
                        value for key, value in (ch.get('delta') or ch.get('message') or {}).items()
                        if key != 'role') for ch in (obj.get('choices') or []))):
                return _PrefetchedSSEResponse(resp, buffered, remaining), ""
        if not line:
            event = ''
        if len(buffered) >= 100:
            break
    return _PrefetchedSSEResponse(resp, buffered, remaining), "HTTP 200 empty SSE"


class SSEUsageTracker:
    """旁路解析原样透传的 SSE 字节，捕获最终帧的上游 usage。

    feed() 不改变客户端收到的字节；只维护跨 chunk 的行缓冲，解决
    iter_content() 任意切块导致 JSON 帧不完整的问题。最终 usage 通常位于
    finish_reason 帧，包含 prompt/completion/total_tokens 与真实 credit。
    """
    def __init__(self):
        self._buf = b""
        self.usage = None
        self.error = ""

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
        err = _sse_business_error(obj)
        if err:
            self.error = err
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


def usage_credit(usage):
    """只认上游明确的有限数值；None/空串/布尔值不代表实际零费用。"""
    raw = usage.get('credit') if isinstance(usage, dict) else None
    if isinstance(raw, bool):
        return None
    try:
        amount = float(raw)
        return amount if math.isfinite(amount) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _response_failure(acc, response, model, image=False):
    resp = response
    status = resp.status_code
    error = ''
    payload = None
    if status >= 400:
        error = "HTTP %s %s" % (status, getattr(resp, 'text', '')[:400])
    elif 'application/json' in (getattr(resp, 'headers', {}).get('Content-Type') or '') or (image and 'text/event-stream' not in (getattr(resp, 'headers', {}).get('Content-Type') or '')):
        try:
            payload = resp.json()
        except Exception:
            payload = None
        error = _sse_business_error(payload) if isinstance(payload, dict) else '响应不是 JSON 对象'
        if not image and not error:
            error = '上游未返回 SSE'
        resp._hy3_error_payload = payload
    else:
        resp, error = _prefetch_sse_evidence(resp, is_trae=_is_trae_acc(acc), is_image=image)
    if not error:
        return resp, None
    failure = rate_limits.classify(acc, resp, error,
                                   route_error=_is_model_route_error(status if status >= 400 else 400, error, model))
    if failure.kind == 'rate_limit':
        rate_limits.mark(acc, model, resp, error, failure)
    resp.close()
    return resp, failure


def _attempt_entry(acc):
    return {'account_id': acc['id'], 'platform': acc['platform'], 'kind': 'selected',
            'outcome': 'selected', 'status': 200, 'retry_at': 0, 'message': '', 'credit': None}


def _failure_fields(failure):
    return {'kind': failure.kind, 'outcome': 'failed', 'status': failure.status,
            'http_status': failure.http_status, 'code': failure.code,
            'retry_at': failure.retry_at, 'message': failure.message}


class _GenerationResponse:
    """交付首帧后的流只观察和报错，不回到调度器重放。"""
    def __init__(self, response, acc, model, trace):
        self._response, self._acc, self._model = response, acc, model
        self._agent2api_trace = trace

    def __getattr__(self, name):
        return getattr(self._response, name)

    def _observe(self, payload, event):
        credit = usage_credit(payload if event == 'token_usage' else extract_usage(payload))
        if credit is not None:
            self._agent2api_trace['attempts'][-1]['credit'] = credit
        if not rate_limits.error_details(payload) and event != 'error':
            return
        failure = rate_limits.classify(self._acc, self._response,
            _sse_business_error(payload) or str(payload), payload=payload)
        rate_limits.mark(self._acc, self._model, self._response, failure.message, failure)
        self._agent2api_trace['attempts'][-1].update(_failure_fields(failure))
        raise ProxyError(failure.message, status=failure.status,
                         payload={'trace': self._agent2api_trace, 'retry_at': failure.retry_at})

    def iter_lines(self, decode_unicode=False):
        event = ''
        try:
            for raw in self._response.iter_lines(decode_unicode=False):
                text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw or '')
                if text.startswith('event:'):
                    event = text[6:].strip()
                    if event == 'error':
                        continue  # 错误帧由本地协议统一脱敏后结束。
                elif text.startswith('data:'):
                    data = text[5:].strip()
                    try:
                        payload = json.loads(data)
                    except (ValueError, TypeError):
                        if event == 'error':
                            self._observe({'error': {'message': data or '上游 error 事件'}}, event)
                    else:
                        self._observe(payload, event)
                elif not text:
                    if event == 'error':
                        self._observe({'error': {'message': '上游 error 事件缺少详情'}}, event)
                    event = ''
                yield text if decode_unicode else raw
        except ProxyError:
            raise
        except Exception as e:
            failure = rate_limits.Failure('network', 502,
                credentials.redact(e, self._acc.get('access_token'), self._acc.get('refresh_token')))
            self._agent2api_trace['attempts'][-1].update(_failure_fields(failure))
            raise ProxyError(failure.message, status=502) from e


    def iter_content(self, chunk_size=None):
        for raw in self.iter_lines():
            yield (raw.encode('utf-8') if isinstance(raw, str) else raw or b'') + b'\n'

    def close(self):
        return self._response.close()


def _dispatch_generation(body, platform, image, max_retry):
    trace = {'request_id': uuid.uuid4().hex, 'attempts': []}
    def error(message, status, **extra):
        return ProxyError(message, status=status, payload={'trace': trace, **extra})
    model = body.get('model') or default_model(platform)
    candidates = resolve_platform_candidates(model, platform)
    if not candidates:
        raise error(_route_error_message(model, [], []), status=503)
    minimum = _quota_threshold_for(model)
    pool_size = sum(len(db.callable_accounts(p, min_quota=minimum)) for p in candidates)
    # 每个候选至多尝试一次，加上每号一次已刷新的401补救；不按渠道均摊预算。
    limit = max(1, int(max_retry)) if max_retry is not None else max(1, pool_size * 2)
    tried, refreshed, failures = [], set(), []
    for candidate in candidates:
        for row in db.callable_accounts(candidate, min_quota=minimum):
            cooldowns = db.active_cooldowns(row, model=model)
            if cooldowns:
                cooling = max(cooldowns, key=lambda x: x['retry_at'])
                trace['attempts'].append({**_attempt_entry(dict(row)), 'kind': 'rate_limit',
                    'outcome': 'cooldown', 'status': 429, 'retry_at': cooling['retry_at'],
                    'message': cooling['message']})
        retry_row = None
        while len(tried) < limit:
            if candidate not in resolve_platform_candidates(model, platform):
                break
            row = retry_row if retry_row is not None else choose_account(candidate, model=model, exclude=tried)
            retry_row = None
            if row is None:
                break
            tried.append(row['id'])
            acc = dict(row)
            entry = _attempt_entry(acc)
            trace['attempts'].append(entry)
            resp = None
            try:
                acc = ensure_token(row)
            except Exception as e:
                failure = rate_limits.Failure('credential', getattr(e, 'status', 401),
                    credentials.redact(e, acc.get('access_token'), acc.get('refresh_token')))
            else:
                current = db.get_account(acc['id'])
                if (current is None or not current['enabled'] or current['status'] in ('banned', 'disabled')
                        or (minimum > 0 and current['quota_updated_at'] and current['quota_remain'] < minimum)
                        or candidate not in resolve_platform_candidates(model, platform)):
                    entry.update(kind='not_eligible', outcome='skipped', status=503,
                                 message='调用开关、模型或额度条件已变更')
                    continue
                cooldowns = db.active_cooldowns(acc, model=model)
                if cooldowns:
                    entry.update(kind='rate_limit', outcome='cooldown', status=429,
                                 retry_at=max(c['retry_at'] for c in cooldowns), message='并发请求已记录限流')
                    continue
                try:
                    resp = (open_image_upstream if image else open_upstream)(acc, copy.deepcopy(body))
                    resp, failure = _response_failure(acc, resp, model, image=image)
                    if failure is None:
                        trace['final_account_id'] = acc['id']
                        return acc, _GenerationResponse(resp, acc, model, trace)
                except Exception as e:
                    if resp is not None:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    failure = rate_limits.Failure('network', 502,
                        credentials.redact(e, acc.get('access_token'), acc.get('refresh_token')))
            entry.update(_failure_fields(failure))
            entry['credit'] = usage_credit(extract_usage(failure.details) or getattr(resp, '_hy3_usage', None))
            failures.append(failure)
            db.bump_account(acc['id'], False, failure.message)
            if failure.status == 401 and acc['id'] not in refreshed:
                refreshed.add(acc['id'])
                try:
                    fresh = ensure_token(acc, force=True)
                    if fresh.get('access_token') != acc.get('access_token'):
                        retry_row = fresh
                        continue
                except Exception:
                    pass
            if not failure.retryable:
                raise error(failure.message, status=failure.status)
    candidates = resolve_platform_candidates(model, platform)
    if not candidates:
        raise error(_route_error_message(model, [], []), status=503)
    eligible = [a for p in candidates for a in db.callable_accounts(p, min_quota=minimum)]
    cooldowns_by_account = [db.active_cooldowns(a, model=model) for a in eligible]
    if eligible and all(cooldowns_by_account) and (not failures or all(f.kind == 'rate_limit' for f in failures)):
        # 一个账号的全部限制都解除后才可调用；再取账号之间的最早恢复时间。
        retry_at = min(max(c['retry_at'] for c in limits) for limits in cooldowns_by_account)
        raise error('所有合格账号均处于限流冷却', status=429, retry_at=retry_at)
    if any(a['id'] not in tried and not limits for a, limits in zip(eligible, cooldowns_by_account)):
        raise error('本次重试预算已用尽，仍有合格账号未尝试', status=503)
    if failures:
        kinds = {f.kind for f in failures}
        cooling_messages = [c['message'] for limits in cooldowns_by_account for c in limits]
        if cooling_messages:
            kinds.add('rate_limit')
        if kinds == {'route'}:
            raise error(_route_error_message(model, candidates, candidates), status=503)
        status = failures[-1].status if len(kinds) == 1 else 502
        messages = [f.message for f in failures] + cooling_messages
        message = '所有候选账号均调用失败或处于冷却: ' + '; '.join(dict.fromkeys(messages))
        raise error(message, status=status)
    if not eligible and not image and minimum > 0 and any(db.count_accounts(p, enabled=1) for p in candidates):
        raise error('模型 %s 的候选账号积分低于阈值 %s' % (model, minimum), status=403)
    raise error('模型 %s 的候选平台号池中没有可用账号' % model, status=503)


def stream_chat(body: dict, platform: str | None = None, max_retry: int | None = None):
    model = body.get('model') or default_model(platform)
    mtype, _ = model_route(model)
    if mtype == 'image':
        raise ProxyError('模型 %s 是图像生成模型，请使用 /v1/images/generations' % model, status=400)
    return _dispatch_generation(body, platform, False, max_retry)


def generate_image(body: dict, platform: str | None = None, max_retry: int | None = None):
    model = body.get('model')
    if not model:
        raise ProxyError('缺少 model 字段', status=400)
    mtype, _ = model_route(model)
    if mtype is not None and mtype != 'image':
        raise ProxyError('模型 %s 不是图像生成模型（类型=%s）' % (model, mtype), status=400)
    return _dispatch_generation(body, platform, True, max_retry)


def _trae_openai_sse_events(resp, model: str):
    """把 Trae SOLO SSE 逐事件转换成 OpenAI chat.completion.chunk SSE。"""
    resp_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    state = {"event": "", "data": []}
    finish = "stop"
    usage = None
    error = ""

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
        nonlocal finish, usage, error
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
        if event == "error":
            error = _sse_business_error(obj) or str(obj)
            return ("data: %s\n\n" % json.dumps(
                {"error": {"message": "上游业务错误: %s" % error,
                           "type": "upstream_error"}},
                ensure_ascii=False)).encode("utf-8")
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
    if error:
        try:
            resp._hy3_error = error
        except Exception:
            pass
        return
    yield chunk(delta={}, finish_reason=finish, with_usage=True)
    yield b"data: [DONE]\n\n"


def probe_account(account_row, model: str, timeout=(5, 30)) -> dict:
    """对指定账号用指定模型发一次真实测活请求（固定 ping / max_tokens=16（低于部分模型最小值会误报 400））。

    锁定该账号直连上游（不走号池调度），用于判断单账号死活。
    返回 {ok, ms, model, platform, reply|error}；不写调用计数。
    """
    acc = ensure_token(account_row)
    # max_tokens 不能用 1：GPT-5.x 系上游最小值校验会 400（integer_below_min_value），
    # 误报成"模型不可用"。16 对所有渠道安全且依然省额度。
    body = {"model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 16}
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
    """聚合 SSE，每个 choice/tool index 独立累积，保留工具调用与真实 usage。"""
    choices = {}
    resp_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    usage = None
    for line in _sse_lines(resp):
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            j = json.loads(payload)
        except (ValueError, TypeError):
            continue
        err = _sse_business_error(j)
        if err:
            raise ProxyError("上游业务错误: %s" % err, status=502)
        if not isinstance(j, dict):
            continue
        resp_id = j.get("id") or resp_id
        created = j.get("created") or created
        model = j.get("model") or model
        if j.get("usage"):
            usage = j["usage"]
        for ch in j.get("choices") or []:
            idx = ch.get("index", 0)
            c = choices.setdefault(idx, {"role": "assistant", "text": [],
                                         "reasoning": [], "tools": {}, "finish": None})
            delta = ch.get("delta") or ch.get("message") or {}
            c["role"] = delta.get("role") or c["role"]
            if delta.get("content"):
                c["text"].append(delta["content"])
            if delta.get("reasoning_content"):
                c["reasoning"].append(delta["reasoning_content"])
            for position, tc in enumerate(delta.get("tool_calls") or []):
                tool = c["tools"].setdefault(tc.get("index", position), {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    tool["id"] = tc["id"]
                if tc.get("type"):
                    tool["type"] = tc["type"]
                fn = tc.get("function") or {}
                for key in ("name", "arguments"):
                    if fn.get(key):
                        tool["function"][key] += fn[key]
            c["finish"] = ch.get("finish_reason") or c["finish"]
    if not choices:
        raise ProxyError("上游返回空响应流 (empty SSE)", status=502)
    output = []
    for idx, c in sorted(choices.items()):
        message = {"role": c["role"], "content": "".join(c["text"])}
        if c["reasoning"]:
            message["reasoning_content"] = "".join(c["reasoning"])
        if c["tools"]:
            message["tool_calls"] = [t for _, t in sorted(c["tools"].items())]
            if not c["text"]:
                message["content"] = None
        output.append({"index": idx, "message": message,
                       "finish_reason": c["finish"] or ("tool_calls" if c["tools"] else "stop")})
    return {"id": resp_id, "object": "chat.completion", "created": created,
            "model": model, "choices": output,
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def aggregate_trae_stream(resp, model: str) -> dict:
    """把 Trae SOLO SSE 聚合成标准 OpenAI 非流式响应。"""
    agg = trae.SSEAggregator()
    for line in _sse_lines(resp):
        if line is not None:
            agg.feed_line(line)
    agg.finish()
    return agg.result(model)


def default_model(platform: str | None = None) -> str:
    """返回已探测注册表中的默认 chat 模型；可限定到指定渠道。"""
    registry = normalize_model_registry(db.get_model_registry())
    platform = str(platform or "").strip()

    def usable(mid, rec):
        if not isinstance(rec, dict) or rec.get("type", "chat") != "chat":
            return False
        plats = _filter_disabled_platforms(mid, rec.get("platforms") or [])
        return bool(plats) and (not platform or platform in plats)

    configured = str(db.get_setting("default_model", "") or "").strip()
    rec = registry.get(configured)
    if configured and usable(configured, rec):
        return configured
    for mid in sorted(registry):
        rec = registry.get(mid)
        if usable(mid, rec):
            return mid
    return ""


def list_models(platform: str | None = None) -> list:
    """返回真实探测注册表中的模型；全关或不属于指定渠道的模型隐藏。"""
    reg = normalize_model_registry(db.get_model_registry())
    disabled = get_model_disabled_map()
    platform = str(platform or "").strip()
    out = []
    for mid in sorted(reg.keys()):
        rec = reg.get(mid)
        plats = rec.get("platforms") if isinstance(rec, dict) else None
        if platform and platform not in (plats or []):
            continue
        if plats is not None and not [p for p in plats if mid not in disabled.get(p, ())]:
            continue
        if platform and mid in disabled.get(platform, ()):
            continue
        out.append(mid)
    return out
