# -*- coding: utf-8 -*-
"""
hy3api - 三平台 hy3 模型反代 + 号池控制面板

平台支持: WorkBuddy / CodeBuddy CN / CodeBuddy(国际)
对外接口: OpenAI 兼容 /v1/chat/completions, /v1/models
控制面板: /  (默认管理员密码 admin, 默认 API Key hy3api)

启动:
    python app.py                 # 默认 0.0.0.0:8787
    python app.py --port 9000
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import threading
import time
import uuid
from datetime import timedelta

import db
import providers as P
import proxy
import discovery
import trae
import responses as R
from flask import (Flask, Response, jsonify, request, send_from_directory,
                   session, stream_with_context)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)


# ==========================================================================
# 后台异步统计（计数 / 日志落库） —— 完全移出请求热路径
# ==========================================================================
# 设计：原本每次反代请求都要同步写 SQLite（bump_api_key / bump_account / add_log），
# 在并发场景下会阻塞响应、拉高首 token / 收尾延迟。这里改为把每次「统计写」交给一个
# 轻量守护线程异步执行，请求线程立即返回，响应速度不再受统计影响；同时统计/仪表盘
# 数据被完整保留（落库只有极短延迟）。统计写之间由 db._write_lock 串行化，安全无冲突。

def _run_stats_job(job):
    """后台统计写任务的执行体：吞掉异常，单条失败不影响主流程与统计准确性。"""
    try:
        job()
    except Exception:
        pass


def _enqueue_stats(job):
    """异步落库：起一个守护线程执行统计写，彻底不阻塞请求热路径。

    每次统计写（add_log / bump_*）单独起一个守护线程，复用 db 的连接与 WAL
    （写之间由 db._write_lock 串行化）。请求线程立即返回，响应速度不受统计影响。
    极端情况下（无法起线程）退化为同步执行，保证统计不丢。
    """
    try:
        threading.Thread(target=_run_stats_job, args=(job,), daemon=True).start()
    except Exception:
        try:
            job()
        except Exception:
            pass


def _flush_stats(timeout: float = 1.0):
    """测试断言前等待后台统计线程完成（生产环境无需调用）。

    统计线程本身是极轻量的 DB 写入，sleep 一个上限即保证落库完成，
    无需精确的队列 join 记账。
    """
    time.sleep(min(timeout, 1.5))
    return True


# ==========================================================================
# 工具：从上游聚合响应里读取真实 usage（tokens + credit），再走 add_log
# ==========================================================================

def _log(kr_id, acc_id, platform, model, status, stream, duration_ms,
         prompt_chars=0, error="", chat_obj=None, usage_obj=None):
    """记录上游真实 usage（tokens + credit），不再按固定单价人工换算。

    腾讯最终 SSE 帧会返回 usage.prompt_tokens / completion_tokens / total_tokens /
    credit。credit=0 也是有效的真实值（例如免费模型 hy3），必须保留，不能再用
    total_tokens × 15/1000 代替。usage_obj 用于流式旁路解析；非流式可传 chat_obj。

    注意：本函数只「计算」用量（纯 Python，极快），真正的 SQLite 写入通过
    _enqueue_stats 投递到后台线程，不阻塞当前请求。
    """
    pt = ct = tt = 0
    u = usage_obj
    if not isinstance(u, dict) and isinstance(chat_obj, dict):
        u = chat_obj.get("usage")
    if isinstance(u, dict):
        pt = int(u.get("prompt_tokens") or 0)
        ct = int(u.get("completion_tokens") or 0)
        tt = int(u.get("total_tokens") or (pt + ct))
    credit_actual = isinstance(u, dict) and "credit" in u
    try:
        cost = float(u.get("credit") or 0) if credit_actual else 0.0
    except Exception:
        cost = 0.0
        credit_actual = False
    _enqueue_stats(lambda: db.add_log(
        kr_id, acc_id, platform, model, status, stream,
        duration_ms, prompt_chars, error,
        pt, ct, tt, cost, credit_actual=credit_actual))

# 建库必须先于取 secret_key（密钥持久化在库里，保证重启后登录态不失效）
db.init_db()
app.secret_key = db.get_secret_key()
app.config["JSON_AS_ASCII"] = False
app.permanent_session_lifetime = timedelta(days=7)


def public_settings() -> dict:
    """对外可见的设置项：剔除密码、内部键和已废弃的人工目录/计费配置。"""
    deprecated = {"models_list", "credit_per_1k_tokens", "model_prices_json"}
    return {k: v for k, v in db.all_settings().items()
            if k != "admin_password" and not k.startswith("_") and k not in deprecated}


# ==========================================================================
# 鉴权
# ==========================================================================

def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return jsonify({"error": "未登录"}), 401
        return fn(*a, **kw)
    return wrapper


def check_api_key():
    """校验调用方 API Key（Authorization: Bearer xxx 或 x-api-key）。"""
    auth = request.headers.get("Authorization", "")
    key = ""
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    if not key:
        key = (request.headers.get("x-api-key") or "").strip()
    if not key:
        key = (request.args.get("key") or "").strip()
    if not key:
        return None
    return db.get_api_key(key)


def openai_error(msg, status=400, etype="invalid_request_error"):
    return jsonify({"error": {"message": msg, "type": etype, "code": status}}), status


# ==========================================================================
# OpenAI 兼容接口
# ==========================================================================

@app.route("/v1/models", methods=["GET"])
def api_models():
    kr = check_api_key()
    if kr is None:
        return openai_error("无效的 API Key", 401, "invalid_api_key")
    now = int(time.time())
    data = [{"id": m, "object": "model", "created": now, "owned_by": "hy3api"}
            for m in proxy.list_models()]
    return jsonify({"object": "list", "data": data})


@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/chat/completions", methods=["POST"])
def api_chat():
    kr = check_api_key()
    if kr is None:
        return openai_error("无效的 API Key", 401, "invalid_api_key")

    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return openai_error("请求体不是合法 JSON")

    if not body.get("messages"):
        return openai_error("缺少 messages 字段")

    want_stream = bool(body.get("stream"))
    model = body.get("model") or proxy.default_model()
    if db.get_setting("allow_model_passthrough", "1") != "1":
        model = proxy.default_model()
    if not model:
        return openai_error("暂无已探测可用的对话模型，请先刷新模型目录", 503,
                            "model_registry_empty")
    body["model"] = model

    platform = (kr["platform"] or "").strip() or None
    prompt_chars = sum(len(str(m.get("content") or "")) for m in body["messages"])
    t0 = time.time()

    try:
        acc, resp = proxy.stream_chat(body, platform=platform)
    except proxy.ProxyError as e:
        _log(kr["id"], None, platform or "", model, e.status, want_stream,
             int((time.time() - t0) * 1000), prompt_chars, str(e))
        return openai_error(str(e), e.status, "upstream_error")

    _enqueue_stats(lambda: db.bump_api_key(kr["id"]))

    is_trae = proxy._is_trae_acc(acc)

    if want_stream:
        def generate():
            ok = True
            err = ""
            usage_tracker = proxy.SSEUsageTracker()
            try:
                if is_trae:
                    # Trae SOLO SSE 不是 OpenAI chunk，需逐帧转换。
                    for chunk in proxy._trae_openai_sse_events(resp, model):
                        usage_tracker.feed(chunk)
                        yield chunk
                else:
                    # 原样透传上游字节，同时旁路解析最终 SSE 帧的真实 usage。
                    for chunk in resp.iter_content(chunk_size=None):
                        if chunk:
                            usage_tracker.feed(chunk)
                            yield chunk
            except Exception as e:      # 上游中断
                ok = False
                err = str(e)
                yield ("data: %s\n\n" % json.dumps(
                    {"error": {"message": "上游流中断: %s" % e}},
                    ensure_ascii=False)).encode("utf-8")
            finally:
                usage_tracker.finish()
                try:
                    resp.close()
                except Exception:
                    pass
                _enqueue_stats(lambda: db.bump_account(acc["id"], ok, err))
                _log(kr["id"], acc["id"], acc["platform"], model,
                     200 if ok else 502, True,
                     int((time.time() - t0) * 1000), prompt_chars, err,
                     usage_obj=usage_tracker.usage)

        # 注意：Connection 属 PEP3333 hop-by-hop 头，WSGI 层不可设置
        return Response(stream_with_context(generate()),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    # 非流式：上游只支持流式 → 本层聚合
    try:
        out = (proxy.aggregate_trae_stream(resp, model) if is_trae
               else proxy.aggregate_stream(resp, model))
    except Exception as e:
        db.bump_account(acc["id"], False, str(e))
        _log(kr["id"], acc["id"], acc["platform"], model, 502, False,
             int((time.time() - t0) * 1000), prompt_chars, str(e))
        return openai_error("聚合上游流失败: %s" % e, 502, "upstream_error")
    finally:
        try:
            resp.close()
        except Exception:
            pass

    _enqueue_stats(lambda: db.bump_account(acc["id"], True))
    _log(kr["id"], acc["id"], acc["platform"], model, 200, False,
         int((time.time() - t0) * 1000), prompt_chars, "", chat_obj=out)
    return jsonify(out)


# ==========================================================================
# 图像生成接口（/v1/images/generations）
#   图像模型（hunyuan-image-*）走独立的 /v2/images/generations 端点，
#   与 chat 网关不互通（chat 网关对图像模型返回 11103 aiart 不支持）。
#   上游返回单个 JSON：data.data[].url / revised_prompt，usage.credit 计费。
# ==========================================================================

def _normalize_image_response(upstream: dict) -> dict:
    """把上游 /v2/images/generations 的真实响应规整成 OpenAI 兼容格式。

    上游真实结构：{"code":0, "data":{"data":[{"url":..., "revised_prompt":...}], ...},
                   "usage":{"credit":...}} —— 图片数组嵌在 data.data 里，是「对象」
                   而非数组。直接透传给标准 OpenAI 客户端会触发
                   `(resp.data || ...).map is not a function`。

    OpenAI 标准格式要求顶层 data 为图片数组：{"created":..., "data":[{"url":...}]}。
    这里把嵌套的 data.data 提到顶层，保留每个条目的 url / b64_json / revised_prompt。
    """
    raw = upstream.get("data") if isinstance(upstream, dict) else None
    items = None
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("data")
    if not isinstance(items, list):
        items = []
    out = []
    for it in items:
        if isinstance(it, dict):
            item = {}
            for k in ("url", "b64_json", "revised_prompt"):
                if k in it:
                    item[k] = it[k]
            for k, v in it.items():  # 兜底保留其它字段，避免丢信息
                if k not in item:
                    item[k] = v
            out.append(item)
        elif isinstance(it, str):
            out.append({"url": it})
    return {"created": int(time.time()), "data": out}


@app.route("/v1/images/generations", methods=["POST"])
@app.route("/images/generations", methods=["POST"])
def api_images():
    kr = check_api_key()
    if kr is None:
        return openai_error("无效的 API Key", 401, "invalid_api_key")

    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return openai_error("请求体不是合法 JSON")

    model = body.get("model") or ""
    if not model:
        return openai_error("缺少 model 字段")
    # 类型判定与平台路由由 proxy.generate_image 统一处理（注册表驱动 + 实时探测回退），
    # 这里不再做静态 is_image_model 预检，避免拦截尚未写入注册表的图像模型。

    platform = (kr["platform"] or "").strip() or None
    t0 = time.time()

    try:
        acc, resp = proxy.generate_image(body, platform=platform)
    except proxy.ProxyError as e:
        _log(kr["id"], None, platform or "", model, e.status, False,
             int((time.time() - t0) * 1000), 0, str(e))
        return openai_error(str(e), e.status, "upstream_error")

    _enqueue_stats(lambda: db.bump_api_key(kr["id"]))

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/event-stream" in ctype:
        # 上游若走 SSE，则原样透传，同时旁路捕获最终 usage 并落库。
        def gen():
            ok = True
            err = ""
            usage_tracker = proxy.SSEUsageTracker()
            try:
                for chunk in resp.iter_content(chunk_size=None):
                    if chunk:
                        usage_tracker.feed(chunk)
                        yield chunk
            except Exception as e:
                ok = False
                err = str(e)
                raise
            finally:
                usage_tracker.finish()
                try:
                    resp.close()
                except Exception:
                    pass
                _enqueue_stats(lambda: db.bump_account(acc["id"], ok, err))
                _log(kr["id"], acc["id"], acc["platform"], model,
                     200 if ok else 502, True,
                     int((time.time() - t0) * 1000), 0, err,
                     usage_obj=usage_tracker.usage)
        return Response(stream_with_context(gen()),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    try:
        upstream = resp.json()
    except Exception:
        upstream = None
    finally:
        try:
            resp.close()
        except Exception:
            pass

    status = resp.status_code
    _enqueue_stats(lambda: db.bump_account(acc["id"], status < 400))
    if status >= 400 or not isinstance(upstream, dict) or upstream.get("code") not in (0, None):
        msg = ""
        if isinstance(upstream, dict):
            msg = str(upstream.get("msg") or upstream.get("error_msg")
                      or upstream.get("message") or "")
        _log(kr["id"], acc["id"], acc["platform"], model, status, False,
             int((time.time() - t0) * 1000), 0, msg or "upstream error")
        if not msg:
            msg = resp.text[:200] or ("HTTP %s" % status)
        return openai_error("上游图像生成失败: %s" % msg, status or 502, "upstream_error")

    _log(kr["id"], acc["id"], acc["platform"], model, 200, False,
         int((time.time() - t0) * 1000), 0, "",
         usage_obj=proxy.extract_usage(upstream))
    # 规整为 OpenAI 兼容格式（data 为图片数组），否则客户端 (.data||...).map 会报错
    return jsonify(_normalize_image_response(upstream))


# ==========================================================================
# OpenAI Responses API（/v1/responses）
#   上游只支持 /v1/chat/completions 且只支持流式；本路由在反代层把
#   Responses API 的 input/tools 翻译为 chat/completions，再把上游 SSE
#   流（或聚合后的非流式结果）重排成 Responses 格式的响应。
# ==========================================================================

@app.route("/v1/responses", methods=["POST"])
@app.route("/responses", methods=["POST"])
def api_responses():
    kr = check_api_key()
    if kr is None:
        return openai_error("无效的 API Key", 401, "invalid_api_key")

    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return openai_error("请求体不是合法 JSON")

    if not body.get("model"):
        return openai_error("缺少 model 字段")
    if "input" not in body:
        return openai_error("缺少 input 字段")

    # 模型解析 + pass-through 门控（与 chat 一致）
    model = body.get("model")
    if db.get_setting("allow_model_passthrough", "1") != "1":
        model = proxy.default_model()
    if not model:
        return openai_error("暂无已探测可用的对话模型，请先刷新模型目录", 503,
                            "model_registry_empty")
    body["model"] = model

    # Responses input → chat messages
    try:
        chat_body = R.responses_to_chat(body, default_model=proxy.default_model())
    except ValueError as e:
        return openai_error(str(e), 400, "invalid_request_error")

    want_stream = bool(body.get("stream"))
    platform = (kr["platform"] or "").strip() or None
    # 日志用 prompt 字符数：instructions + 所有 input 文本
    prompt_chars = len(str(body.get("instructions") or "")) + \
        sum(len(str(x)) for x in _flatten_input(body.get("input")))
    t0 = time.time()
    resp_id = "resp_" + uuid.uuid4().hex[:24]

    try:
        acc, resp = proxy.stream_chat(chat_body, platform=platform)
    except proxy.ProxyError as e:
        _log(kr["id"], None, platform or "", model, e.status, want_stream,
             int((time.time() - t0) * 1000), prompt_chars, str(e))
        return openai_error(str(e), e.status, "upstream_error")

    _enqueue_stats(lambda: db.bump_api_key(kr["id"]))

    if want_stream:
        def generate():
            ok = True
            err = ""
            try:
                for chunk in R.stream_chat_to_responses(resp, model, resp_id,
                                                         instructions=body.get("instructions")):
                    yield chunk
            except Exception as e:      # 上游中断
                ok = False
                err = str(e)
                yield _sse_err("上游流中断: %s" % e)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                _enqueue_stats(lambda: db.bump_account(acc["id"], ok, err))
                _log(kr["id"], acc["id"], acc["platform"], model,
                     200 if ok else 502, True,
                     int((time.time() - t0) * 1000), prompt_chars, err,
                     usage_obj=getattr(resp, "_hy3_usage", None))

        return Response(stream_with_context(generate()),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    # 非流式：聚合上游 chat 流 → 重排为 Responses 对象
    try:
        chat_obj = (proxy.aggregate_trae_stream(resp, model)
                    if proxy._is_trae_acc(acc)
                    else proxy.aggregate_stream(resp, model))
    except Exception as e:
        db.bump_account(acc["id"], False, str(e))
        _log(kr["id"], acc["id"], acc["platform"], model, 502, False,
             int((time.time() - t0) * 1000), prompt_chars, str(e))
        return openai_error("聚合上游流失败: %s" % e, 502, "upstream_error")
    finally:
        try:
            resp.close()
        except Exception:
            pass

    out = R.chat_to_response(chat_obj, response_id=resp_id, model=model,
                             instructions=body.get("instructions"))
    _enqueue_stats(lambda: db.bump_account(acc["id"], True))
    _log(kr["id"], acc["id"], acc["platform"], model, 200, False,
         int((time.time() - t0) * 1000), prompt_chars, "", chat_obj=chat_obj)
    return jsonify(out)


def _flatten_input(inp) -> list:
    """粗略统计 input 文本长度，支持 str / list[message(...)]。"""
    out = []
    if isinstance(inp, str):
        out.append(inp)
    elif isinstance(inp, list):
        for it in inp:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                c = it.get("content")
                if isinstance(c, str):
                    out.append(c)
                elif isinstance(c, list):
                    for p in c:
                        if isinstance(p, dict):
                            out.append(str(p.get("text") or p.get("input_text") or ""))
                        elif isinstance(p, str):
                            out.append(p)
    return out


def _sse_err(msg: str) -> bytes:
    import json as _json
    return ("event: error\ndata: %s\n\n"
            % _json.dumps({"error": {"message": msg, "type": "upstream_error"}},
                          ensure_ascii=False)).encode("utf-8")


# ==========================================================================
# 控制面板 - 认证
# ==========================================================================

@app.route("/api/login", methods=["POST"])
def panel_login():
    data = request.get_json(silent=True) or {}
    pw = str(data.get("password") or "")
    stored = db.get_setting("admin_password") or ""
    if db.verify_pw(pw, stored):
        session["admin"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "密码错误"}), 401


@app.route("/api/logout", methods=["POST"])
def panel_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def panel_me():
    return jsonify({"admin": bool(session.get("admin"))})


@app.route("/api/password", methods=["POST"])
@admin_required
def panel_password():
    data = request.get_json(silent=True) or {}
    new = str(data.get("password") or "")
    if len(new) < 4:
        return jsonify({"ok": False, "error": "密码至少 4 位"}), 400
    db.set_setting("admin_password", db.hash_pw(new))
    return jsonify({"ok": True})


# ==========================================================================
# 控制面板 - 概览 / 平台
# ==========================================================================

@app.route("/api/overview")
@admin_required
def panel_overview():
    s = db.stats()
    s["platforms"] = P.platform_list()
    s["settings"] = public_settings()
    return jsonify(s)


@app.route("/api/overview/usage")
@admin_required
def panel_overview_usage():
    """仪表盘数据：积分池 / token 用量 / 扣费明细 / 7天趋势 / 各模型平台分布。"""
    return jsonify(db.overview_usage())


@app.route("/api/platforms")
@admin_required
def panel_platforms():
    return jsonify(P.platform_list())


# ==========================================================================
# 控制面板 - 号池
# ==========================================================================

def _acc_public(row) -> dict:
    d = dict(row)
    at = d.get("access_token") or ""
    d["access_token"] = (at[:10] + "..." + at[-6:]) if len(at) > 20 else "***"
    rt = d.get("refresh_token") or ""
    d["has_refresh"] = bool(rt)
    d["refresh_token"] = (rt[:8] + "..." + rt[-4:]) if len(rt) > 16 else ("***" if rt else "")
    try:
        d["quota_packages"] = json.loads(d.get("quota_raw") or "[]")
    except Exception:
        d["quota_packages"] = []
    d.pop("quota_raw", None)
    d["expires_at_sec"] = proxy._norm_ts(d.get("expires_at"))
    d["supports_checkin"] = P.supports_checkin(d.get("platform"))
    d["checkin_today"] = int(d.get("checkin_today") or 0)
    d["checkin_streak"] = int(d.get("checkin_streak") or 0)
    d["checkin_last_date"] = d.get("checkin_last_date") or ""
    d["checkin_credit"] = int(d.get("checkin_credit") or 0)
    d["checkin_updated_at"] = int(d.get("checkin_updated_at") or 0)
    d["source"] = d.get("source") or ""
    return d


@app.route("/api/accounts")
@admin_required
def panel_accounts():
    plat = request.args.get("platform") or None
    return jsonify([_acc_public(r) for r in db.list_accounts(plat)])


@app.route("/api/accounts/<int:aid>", methods=["DELETE"])
@admin_required
def panel_account_delete(aid):
    db.delete_account(aid)
    return jsonify({"ok": True})


@app.route("/api/accounts/<int:aid>", methods=["PATCH"])
@admin_required
def panel_account_patch(aid):
    data = request.get_json(silent=True) or {}
    allowed = {}
    for k in ("enabled", "weight", "label", "status"):
        if k in data:
            allowed[k] = data[k]
    if allowed:
        db.update_account(aid, **allowed)
    return jsonify({"ok": True})


@app.route("/api/accounts/<int:aid>/refresh", methods=["POST"])
@admin_required
def panel_account_refresh(aid):
    row = db.get_account(aid)
    if row is None:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    try:
        acc = proxy.ensure_token(row, force=True)
        s = proxy.refresh_quota(db.get_account(aid))
        return jsonify({"ok": True, "quota": s,
                        "expires_at": proxy._norm_ts(acc.get("expires_at"))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/accounts/quota_all", methods=["POST"])
@admin_required
def panel_quota_all():
    results = []
    for row in db.list_accounts():
        try:
            s = proxy.refresh_quota(row)
            results.append({"id": row["id"], "ok": True,
                            "remain": s["remain"], "total": s["total"]})
        except Exception as e:
            results.append({"id": row["id"], "ok": False, "error": str(e)})
    return jsonify({"ok": True, "results": results})


@app.route("/api/accounts/<int:aid>/checkin-status", methods=["GET"])
@admin_required
def panel_account_checkin_status(aid):
    row = db.get_account(aid)
    if row is None:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    if not P.supports_checkin(row["platform"]):
        return jsonify({"ok": True, "supported": False,
                        "today_checked_in": False, "streak_days": 0,
                        "claimable": False, "reason": "该平台不支持签到"})
    try:
        st = proxy.refresh_checkin_status(db.get_account(aid))
        return jsonify({"ok": True, "supported": True, **st})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/accounts/<int:aid>/checkin", methods=["POST"])
@admin_required
def panel_account_checkin(aid):
    row = db.get_account(aid)
    if row is None:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    if not P.supports_checkin(row["platform"]):
        return jsonify({"ok": False, "error": "该平台不支持签到"}), 400
    try:
        res = proxy.checkin_account(db.get_account(aid))
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/accounts/<int:aid>/probe", methods=["POST"])
@admin_required
def panel_account_probe(aid):
    """单账号测活：锁定该账号用指定模型发一次真实请求。"""
    row = db.get_account(aid)
    if row is None:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    if not model:
        return jsonify({"ok": False, "error": "缺少 model"}), 400
    try:
        res = proxy.probe_account(dict(row), model)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify(res)


@app.route("/api/platforms/<key>/models")
@admin_required
def panel_platform_models(key):
    """轻量目录：注册表中该平台已探测确认的模型（卡片测活下拉用）。"""
    if key not in P.PLATFORMS:
        return jsonify({"ok": False, "error": "未知平台"}), 404
    return jsonify({"ok": True, "models": P.catalog_entries(key)})


@app.route("/api/accounts/checkin_all", methods=["POST"])
@admin_required
def panel_checkin_all():
    try:
        d = proxy.checkin_all(silent=False)
        return jsonify(d)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/accounts/import_token", methods=["POST"])
@admin_required
def panel_import_token():
    """手动导入 access_token(+refresh_token)，或粘贴 Cockpit Tools 导出的 JSON。"""
    data = request.get_json(silent=True) or {}
    platform = data.get("platform") or "workbuddy"
    if platform not in P.PLATFORMS:
        return jsonify({"ok": False, "error": "未知平台"}), 400

    raw = data.get("json_text")
    items = []
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception as e:
            return jsonify({"ok": False, "error": "JSON 解析失败: %s" % e}), 400
        if isinstance(parsed, dict):
            parsed = [parsed]
        for it in parsed:
            if not isinstance(it, dict):
                continue
            # 兼容 ai-multi-platform-proxy 的 auths/*.json：
            # {"platform":"trae","account":{...},"auth":{...}}
            if it.get("auth") and isinstance(it["auth"], dict):
                acc = it.get("account") or {}
                a = it["auth"]
                items.append({
                    "access_token": a.get("accessToken") or a.get("access_token"),
                    "refresh_token": a.get("refreshToken") or a.get("refresh_token"),
                    "uid": acc.get("uid") or "",
                    "nickname": acc.get("nickname") or "",
                    "enterprise_id": acc.get("enterpriseId") or "",
                    "domain": a.get("domain") or "",
                    "expires_at": a.get("expiresAt") or 0,
                    "machine_id": a.get("machineId") or "",
                    "device_id": a.get("deviceId") or "",
                })
            elif it.get("access_token") or it.get("accessToken"):
                items.append(it)
    else:
        at = (data.get("access_token") or "").strip()
        if not at:
            return jsonify({"ok": False, "error": "缺少 access_token"}), 400
        items.append({
            "access_token": at,
            "refresh_token": (data.get("refresh_token") or "").strip(),
            "email": data.get("email") or "",
            "uid": data.get("uid") or "",
            "domain": data.get("domain") or "",
            "expires_at": data.get("expires_at") or 0,
            "machine_id": data.get("machine_id") or "",
            "device_id": data.get("device_id") or "",
        })

    added, updated = 0, 0
    for it in items:
        at = it.get("access_token") or it.get("accessToken")
        rt = it.get("refresh_token") or it.get("refreshToken") or ""
        uid = it.get("uid") or ""
        exist = db.find_account_by_uid(platform, uid) if uid else None
        fields = dict(
            access_token=at, refresh_token=rt,
            expires_at=int(it.get("expires_at") or it.get("expiresAt") or 0),
            domain=it.get("domain") or "", email=it.get("email") or "",
            uid=uid, nickname=it.get("nickname") or "",
            enterprise_id=it.get("enterprise_id") or "",
            enterprise_name=it.get("enterprise_name") or "",
            machine_id=it.get("machine_id") or "",
            device_id=it.get("device_id") or "",
        )
        if exist:
            db.update_account(exist["id"], status="active", status_reason="", **fields)
            aid = exist["id"]
            updated += 1
        else:
            aid = db.add_account(platform, label=it.get("label") or "", **fields)
            added += 1
        # 导入即刷新一次令牌：验证 refresh_token 可用并补齐 expires_at
        if fields["refresh_token"] and not fields["expires_at"]:
            try:
                proxy.ensure_token(db.get_account(aid), force=True)
            except Exception:
                pass
        try:
            proxy.refresh_quota(db.get_account(aid))
        except Exception:
            pass

    return jsonify({"ok": True, "added": added, "updated": updated})


# ==========================================================================
# 公共号池扩充：终端用户扫码 / 网页登录，把自己的账号贡献进号池
#   无需管理员登录；受 public_join_enabled 开关控制。
#   账号入库时打 source='user_join' 标记，管理员可在号池页识别与管理。
# ==========================================================================

@app.route("/api/join/start", methods=["POST"])
def public_join_start():
    if db.get_setting("public_join_enabled", "1") != "1":
        return jsonify({"ok": False, "error": "公共接入已关闭"}), 403
    data = request.get_json(silent=True) or {}
    platform = data.get("platform") or "workbuddy"
    if platform not in P.PLATFORMS:
        return jsonify({"ok": False, "error": "未知平台"}), 400
    label = (data.get("label") or "用户贡献").strip()[:40]
    try:
        info = P.start_login(platform)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    db.add_pending(info["state"], platform, info["auth_url"], label)
    return jsonify({"ok": True, "state": info["state"],
                    "auth_url": info["auth_url"], "login_id": info.get("login_id")})


@app.route("/api/join/poll", methods=["POST"])
def public_join_poll():
    if db.get_setting("public_join_enabled", "1") != "1":
        return jsonify({"ok": False, "error": "公共接入已关闭"}), 403
    data = request.get_json(silent=True) or {}
    state = data.get("state") or ""
    pend = db.get_pending(state)
    if pend is None:
        return jsonify({"ok": False, "pending": True, "error": "state 不存在"}), 404

    try:
        tok = P.poll_token(pend["platform"], state)
    except Exception as e:
        return jsonify({"ok": False, "pending": True, "error": str(e)})

    if not tok:
        return jsonify({"ok": False, "pending": True, "msg": "等待网页端完成授权"})

    try:
        info = P.fetch_account_info(pend["platform"], tok["access_token"], state=state,
                                    domain=tok.get("domain"))
    except Exception:
        info = {"uid": "", "email": "", "nickname": "",
                "enterprise_id": "", "enterprise_name": ""}

    exist = db.find_account_by_uid(pend["platform"], info.get("uid"))
    fields = dict(
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token") or "",
        expires_at=int(tok.get("expires_at") or 0),
        domain=tok.get("domain") or "",
        email=info.get("email") or "", uid=info.get("uid") or "",
        nickname=info.get("nickname") or "",
        enterprise_id=info.get("enterprise_id") or "",
        enterprise_name=info.get("enterprise_name") or "",
        state=state,
    )
    if exist:
        db.update_account(exist["id"], status="active", status_reason="", **fields)
        aid = exist["id"]
    else:
        aid = db.add_account(pend["platform"], label=pend["label"] or "用户贡献",
                             source="user_join", **fields)

    db.finish_pending(state)
    quota = None
    try:
        quota = proxy.refresh_quota(db.get_account(aid))
    except Exception:
        quota = None

    return jsonify({"ok": True, "account_id": aid,
                    "email": info.get("email"), "nickname": info.get("nickname"),
                    "quota": quota})


# ==========================================================================
# 控制面板 - SSO 登录添加账号
# ==========================================================================

@app.route("/api/login/trae_callback", methods=["POST"])
@admin_required
def panel_login_trae_callback():
    """Trae 特有：粘贴登录回调 URL，解析 token 并入库。

    Trae 授权后跳到不可达的 127.0.0.1，需要用户复制完整回调链接；
    这里解析 refreshToken → ExchangeToken → GetUserInfo → 入库。
    """
    from urllib.parse import urlparse, parse_qs, unquote

    data = request.get_json(silent=True) or {}
    callback = (data.get("callback_url") or data.get("url") or "").strip()
    if not callback:
        return jsonify({"ok": False, "error": "缺少 callback_url"}), 400
    try:
        q = parse_qs(urlparse(callback).query)
    except Exception as e:
        return jsonify({"ok": False, "error": "回调 URL 解析失败: %s" % e}), 400

    def one(k):
        v = q.get(k) or []
        return v[0] if v else ""

    refresh = one("refreshToken")
    user_jwt = {}
    user_info = {}
    for k, target in (("userJwt", user_jwt), ("userInfo", user_info)):
        raw = one(k)
        if not raw:
            continue
        for text in (raw, unquote(raw)):
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    target.update(obj)
                    break
            except Exception:
                continue

    token = ""
    expires_at = 0
    if refresh:
        try:
            tok = trae.exchange_token(refresh)
            token = tok["access_token"]
            refresh = tok["refresh_token"]
            expires_at = int(tok["expires_at"] or 0)
        except Exception as e:
            return jsonify({"ok": False, "error": "ExchangeToken 失败: %s" % e}), 400
    else:
        jwt = user_jwt.get("Token") or user_jwt.get("token") or ""
        if not jwt:
            return jsonify({"ok": False, "error": "回调中没有 refreshToken / userJwt.Token"}), 400
        token = jwt
        expires_at = int(user_jwt.get("TokenExpireAt") or 0) or 0

    try:
        info = trae.get_user_info(token)
    except Exception as e:
        info = {"uid": "", "nickname": "", "enterprise_id": ""}

    uid = info.get("uid") or str(user_info.get("UserID") or "")
    nickname = info.get("nickname") or str(user_info.get("ScreenName") or "")
    ent = info.get("enterprise_id") or str(user_info.get("TenantID") or "")
    if not uid:
        return jsonify({"ok": False, "error": "无法获取 uid，token 可能已失效"}), 400

    # machine/device 若前端传回则保存；否则由导入字段/默认值兜底
    machine_id = data.get("machine_id") or ""
    device_id = data.get("device_id") or ""
    exist = db.find_account_by_uid("trae", uid)
    fields = dict(
        access_token=token, refresh_token=refresh,
        expires_at=expires_at, domain="trae.cn",
        email="", uid=uid, nickname=nickname,
        enterprise_id=ent, enterprise_name="",
        machine_id=machine_id, device_id=device_id,
    )
    if exist:
        db.update_account(exist["id"], status="active", status_reason="", **fields)
        aid = exist["id"]
    else:
        aid = db.add_account("trae", label=data.get("label") or "", **fields)
    try:
        proxy.refresh_quota(db.get_account(aid))
    except Exception:
        pass
    return jsonify({"ok": True, "account_id": aid, "uid": uid,
                    "nickname": nickname})


@app.route("/api/login/start", methods=["POST"])
@admin_required
def panel_login_start():
    data = request.get_json(silent=True) or {}
    platform = data.get("platform") or "workbuddy"
    try:
        info = P.start_login(platform)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    db.add_pending(info["state"], platform, info["auth_url"], data.get("label") or "")
    return jsonify({"ok": True, **info})


@app.route("/api/login/poll", methods=["POST"])
@admin_required
def panel_login_poll():
    data = request.get_json(silent=True) or {}
    state = data.get("state") or ""
    pend = db.get_pending(state)
    if pend is None:
        return jsonify({"ok": False, "error": "state 不存在"}), 404
    platform = pend["platform"]

    try:
        tok = P.poll_token(platform, state)
    except Exception as e:
        return jsonify({"ok": False, "pending": True, "error": str(e)})

    if not tok:
        return jsonify({"ok": False, "pending": True, "msg": "等待网页端完成授权"})

    try:
        info = P.fetch_account_info(platform, tok["access_token"], state=state,
                                    domain=tok.get("domain"))
    except Exception:
        info = {"uid": "", "email": "", "nickname": "",
                "enterprise_id": "", "enterprise_name": ""}

    exist = db.find_account_by_uid(platform, info.get("uid"))
    fields = dict(
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token") or "",
        expires_at=int(tok.get("expires_at") or 0),
        domain=tok.get("domain") or "",
        token_type=tok.get("token_type") or "Bearer",
        email=info.get("email") or "", uid=info.get("uid") or "",
        nickname=info.get("nickname") or "",
        enterprise_id=info.get("enterprise_id") or "",
        enterprise_name=info.get("enterprise_name") or "",
        state=state,
    )
    if exist:
        db.update_account(exist["id"], status="active", status_reason="", **fields)
        aid = exist["id"]
    else:
        aid = db.add_account(platform, label=pend["label"] or "", **fields)

    db.finish_pending(state)
    try:
        proxy.refresh_quota(db.get_account(aid))
    except Exception:
        pass

    return jsonify({"ok": True, "account_id": aid,
                    "email": info.get("email"), "nickname": info.get("nickname")})


# ==========================================================================
# 控制面板 - API Key
# ==========================================================================

@app.route("/api/keys")
@admin_required
def panel_keys():
    return jsonify([dict(r) for r in db.list_api_keys()])


@app.route("/api/keys", methods=["POST"])
@admin_required
def panel_key_add():
    data = request.get_json(silent=True) or {}
    key = db.add_api_key(data.get("name") or "未命名",
                         (data.get("api_key") or "").strip() or None,
                         data.get("platform") or "")
    return jsonify({"ok": True, "api_key": key})


@app.route("/api/keys/<int:kid>", methods=["DELETE"])
@admin_required
def panel_key_delete(kid):
    db.delete_api_key(kid)
    return jsonify({"ok": True})


@app.route("/api/keys/<int:kid>", methods=["PATCH"])
@admin_required
def panel_key_patch(kid):
    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        db.toggle_api_key(kid, int(bool(data["enabled"])))
    return jsonify({"ok": True})


# ==========================================================================
# 控制面板 - 设置 / 日志
# ==========================================================================

@app.route("/api/settings", methods=["GET", "POST"])
@admin_required
def panel_settings():
    if request.method == "GET":
        return jsonify(public_settings())
    data = request.get_json(silent=True) or {}
    allowed = ("default_model", "strategy", "auto_refresh", "refresh_ahead",
               "model_base_override", "allow_model_passthrough",
               "log_retention", "system_prompt", "auto_checkin", "checkin_time",
               "credit_threshold", "free_models", "public_join_enabled",
               "model_route_priority", "model_platform_disabled")
    for k in allowed:
        if k in data:
            db.set_setting(k, data[k])
    return jsonify({"ok": True})


@app.route("/api/models/discover", methods=["GET"])
@admin_required
def panel_models_discover():
    """跨三平台真实探测模型（上游无枚举接口，只能逐项请求），刷新运行时注册表。

    返回注册表驱动的目录：每个平台的模型条目 + 对外暴露清单。探测结果持久化在
    DB（discovered_models），下游 /v1/models 与请求调度全部读这张注册表。
    """
    try:
        # 先拉官方 CLI 目录，零成本收录新模型；再跑真实探测补齐官方目录未覆盖的
        # 平台（如国际站）与图像 / 音频模型。
        discovery.sync_upstream_catalogs()
        registry = discovery.run_discovery()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    platforms_out = []
    for p in P.PLATFORMS.values():
        key = p["key"]
        platforms_out.append({
            "platform": key,
            "label": p["label"],
            "models": P.catalog_entries(key),
        })
    exposed = P.to_entries(proxy.list_models())
    return jsonify({
        "ok": True,
        "platforms": platforms_out,
        "exposed": exposed,
        "total": len(registry),
        "discovered_at": int(time.time()),
        # 渠道×模型开关状态：{platform: [model_id]}，卡片据此渲染禁用态
        "disabled": {p: sorted(ids) for p, ids in proxy.get_model_disabled_map().items()},
        # 文本/音频已同步返回；图像探测在后台进行，前端轮询本接口的 progress 版本
        # 即可看到生图模型陆续出现，无需阻塞本次响应。
        "image_discovering": discovery.image_discovery_running(),
    })


@app.route("/api/models/discover/progress", methods=["GET"])
@admin_required
def panel_models_discover_progress():
    """轮询用：返回当前注册表 + 后台图像探测是否仍在进行。

    「刷新模型目录」同步只返回文本/音频模型；图像模型由后台线程探测，
    确认后实时写入注册表。前端定时轮询本接口即可把生图模型逐步展示出来。
    """
    registry = db.get_model_registry()
    platforms_out = []
    for p in P.PLATFORMS.values():
        key = p["key"]
        platforms_out.append({
            "platform": key,
            "label": p["label"],
            "models": P.catalog_entries(key),
        })
    exposed = P.to_entries(proxy.list_models())
    return jsonify({
        "ok": True,
        "platforms": platforms_out,
        "exposed": exposed,
        "total": len(registry),
        "discovered_at": int(time.time()),
        "disabled": {p: sorted(ids) for p, ids in proxy.get_model_disabled_map().items()},
        "image_discovering": discovery.image_discovery_running(),
    })


@app.route("/api/logs")
@admin_required
def panel_logs():
    limit = min(int(request.args.get("limit", 100)), 500)
    return jsonify([dict(r) for r in db.list_logs(limit)])


@app.route("/api/logs", methods=["DELETE"])
@admin_required
def panel_logs_clear():
    db.trim_logs(0)
    return jsonify({"ok": True})


@app.route("/api/test", methods=["POST"])
@admin_required
def panel_test():
    """面板内一键连通性测试（走完整反代链路，非流式聚合）。"""
    data = request.get_json(silent=True) or {}
    platform = data.get("platform") or None
    model = data.get("model") or db.get_setting("default_model", "hy3")
    prompt = data.get("prompt") or "用一句话介绍你自己"
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    t0 = time.time()
    try:
        acc, resp = proxy.stream_chat(body, platform=platform)
    except proxy.ProxyError as e:
        return jsonify({"ok": False, "error": str(e)}), 200
    try:
        out = (proxy.aggregate_trae_stream(resp, model)
               if proxy._is_trae_acc(acc)
               else proxy.aggregate_stream(resp, model))
    except Exception as e:
        return jsonify({"ok": False, "error": "聚合失败: %s" % e}), 200
    finally:
        try:
            resp.close()
        except Exception:
            pass
    _enqueue_stats(lambda: db.bump_account(acc["id"], True))
    return jsonify({
        "ok": True,
        "account_id": acc["id"],
        "platform": acc["platform"],
        "email": acc.get("email"),
        "ms": int((time.time() - t0) * 1000),
        "content": out["choices"][0]["message"]["content"][:500],
    })


# ==========================================================================
# 前端（React + Vite 构建产物，位于 frontend/dist）
# ==========================================================================

WEB_DIR = os.path.join(BASE_DIR, "frontend", "dist")


@app.route("/")
def index():
    # no-store：防止 Docker 重新部署后浏览器缓存旧面板（用户反馈的「缓存写入/读取」问题）
    r = send_from_directory(WEB_DIR, "index.html")
    r.headers["Cache-Control"] = "no-store, max-age=0"
    return r


@app.route("/assets/<path:fn>")
def web_assets(fn):
    # 构建产物带 hash 文件名，可长期缓存；新构建会生成新文件名，旧缓存自动失效
    r = send_from_directory(os.path.join(WEB_DIR, "assets"), fn)
    r.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return r


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "hy3api", "ts": int(time.time())})


@app.route("/<path:p>")
def spa_fallback(p):
    # 客户端路由（/join、/dashboard、/accounts …）：回退到 index.html
    # /api、/v1、/health 等由各自路由处理，这里不拦截
    if p.startswith("api/") or p.startswith("v1") or p.startswith("health") or p == "favicon.ico":
        return ("", 404)
    r = send_from_directory(WEB_DIR, "index.html")
    r.headers["Cache-Control"] = "no-store, max-age=0"
    return r


# ==========================================================================
# 后台维护线程
# ==========================================================================

def _maintenance_loop():
    while True:
        try:
            time.sleep(300)
            for row in db.list_accounts():
                if row["enabled"]:
                    try:
                        proxy.ensure_token(row)
                    except Exception:
                        pass
            try:
                proxy.run_auto_checkin_if_due()
            except Exception:
                pass
            try:
                proxy.retry_failed_checkins()
            except Exception:
                pass
            db.trim_logs(int(db.get_setting("log_retention", "2000") or 2000))
            db.cleanup_pending()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="hy3api - 三平台 hy3 反代与号池面板")
    ap.add_argument("--host", default=os.environ.get("HY3API_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("HY3API_PORT", "8787")))
    ap.add_argument("--threads", type=int,
                    default=int(os.environ.get("HY3API_THREADS", "16")))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    threading.Thread(target=_maintenance_loop, daemon=True).start()

    n_acc = len(db.list_accounts())
    print("=" * 62, flush=True)
    print("  hy3api 已启动", flush=True)
    print("  监听地址 : %s:%d" % (args.host, args.port), flush=True)
    print("  控制面板 : http://127.0.0.1:%d/" % args.port, flush=True)
    print("  API 地址 : http://127.0.0.1:%d/v1" % args.port, flush=True)
    print("  数据库   : %s" % db.DB_PATH, flush=True)
    print("  号池账号 : %d 个" % n_acc, flush=True)
    if n_acc == 0:
        print("  默认管理员密码 : %s      默认 API Key : %s"
              % (db.DEFAULT_ADMIN_PASSWORD, db.DEFAULT_API_KEY), flush=True)
    print("=" * 62, flush=True)

    # 启动阶段：若运行期模型注册表为空（首次部署），后台跑一次真实探测，
    # 避免 /v1/models 返回空目录。探测需要启用账号，无账号时跳过（返回空）。
    if not db.get_model_registry():
        def _bootstrap_discover():
            try:
                discovery.sync_upstream_catalogs()
                discovery.run_discovery()
                reg = db.get_model_registry()
                print("  自动模型探测完成，已收录 %d 个模型" % len(reg), flush=True)
            except Exception as e:
                print("  自动模型探测跳过（%s）" % e, flush=True)
        threading.Thread(target=_bootstrap_discover, daemon=True).start()

    if args.debug:
        app.run(host=args.host, port=args.port, debug=True, threaded=True)
    else:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=args.threads)


if __name__ == "__main__":
    main()
