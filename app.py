# -*- coding: utf-8 -*-
"""
Agent2API - 三平台模型反代 + 号池控制面板

平台支持: WorkBuddy / TraeWork / CodeBuddy(国际)
对外接口: OpenAI 兼容 /v1/chat/completions, /v1/models
控制面板: /  (默认管理员密码 admin, 默认 API Key Agent2API)

启动:
    python app.py                 # 默认 0.0.0.0:8530
    python app.py --port 9000
"""
from __future__ import annotations

import argparse
import functools
import json
import math
import os
import queue
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
# 在并发场景下会阻塞响应、拉高首 token / 收尾延迟。这里改为一个有界统计队列，
# 由单个守护线程串行落库：请求线程立即返回，统计写不会创建无上限的线程风暴，
# 同时保持写入顺序，避免同一请求的计数和日志乱序。
_stats_queue: queue.Queue = queue.Queue(maxsize=10000)
_stats_worker_started = False
_stats_worker_guard = threading.Lock()

def _run_stats_job(job):
    """后台统计写任务的执行体：吞掉异常，单条失败不影响主流程与统计准确性。"""
    try:
        job()
    except Exception:
        pass


def _stats_worker():
    while True:
        job = _stats_queue.get()
        try:
            _run_stats_job(job)
        finally:
            _stats_queue.task_done()


def _ensure_stats_worker():
    global _stats_worker_started
    with _stats_worker_guard:
        if _stats_worker_started:
            return
        threading.Thread(target=_stats_worker, name="agent2api-stats", daemon=True).start()
        _stats_worker_started = True


def _enqueue_stats(job):
    """异步落库：投递到有界队列，由单 worker 串行执行。

    队列满时退化为当前线程执行，优先保证统计不丢；正常情况下请求线程
    只做一次非阻塞入队，不再为每条统计写创建守护线程。
    """
    try:
        _ensure_stats_worker()
        _stats_queue.put_nowait(job)
    except queue.Full:
        _run_stats_job(job)
    except Exception:
        _run_stats_job(job)


def _flush_stats(timeout: float = 1.0):
    """测试断言前等待统计队列清空；生产环境无需调用。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while _stats_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    return _stats_queue.unfinished_tasks == 0


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

def migrate_legacy_platforms() -> None:
    """启动时归一旧版本渠道别名，避免未知平台记录让面板/API 失效。"""
    db.migrate_legacy_platforms()


# 建库必须先于取 secret_key（密钥持久化在库里，保证重启后登录态不失效）
db.init_db()
migrate_legacy_platforms()
proxy.migrate_runtime_state()
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


@app.before_request
def reject_non_object_json():
    """所有 JSON 写入口只接受对象，统一拦截数组/标量，避免视图层 data.get 500。"""
    if request.method not in {"POST", "PUT", "PATCH"} or not request.is_json:
        return None
    try:
        data = request.get_json(silent=False)
    except Exception:
        # 非法 JSON 由各端点沿用原有错误文案处理；这里只处理合法但类型错误。
        return None
    if isinstance(data, dict):
        return None
    if request.path.startswith(("/v1/", "/chat/", "/images/", "/responses")):
        return openai_error("请求体必须是 JSON 对象")
    return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400


def _as_bool(value) -> bool:
    """兼容管理 API 的 bool、数字和字符串输入。"""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _validate_model_field(body):
    """拒绝非字符串模型名，避免进入路由/实时探测层后被隐式转型。"""
    if "model" in body and not isinstance(body["model"], str):
        return openai_error("model 必须是字符串")
    return None


def _string_field(data: dict, key: str, default: str = "", strip: bool = True) -> str:
    """读取管理 API 字符串字段；数组/对象直接视为客户端参数错误。"""
    value = data.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError("%s 必须是字符串" % key)
    return value.strip() if strip else value


# ==========================================================================
# OpenAI 兼容接口
# ==========================================================================

@app.route("/v1/models", methods=["GET"])
def api_models():
    kr = check_api_key()
    if kr is None:
        return openai_error("无效的 API Key", 401, "invalid_api_key")
    now = int(time.time())
    platform = (kr["platform"] or "").strip() or None
    data = [{"id": m, "object": "model", "created": now, "owned_by": "agent2api"}
            for m in proxy.list_models(platform=platform)]
    return jsonify({"object": "list", "data": data})


@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/chat/completions", methods=["POST"])
def api_chat():
    kr = check_api_key()
    if kr is None:
        return openai_error("无效的 API Key", 401, "invalid_api_key")

    try:
        body = request.get_json(force=True)
    except Exception:
        return openai_error("请求体不是合法 JSON")
    if not isinstance(body, dict):
        return openai_error("请求体必须是 JSON 对象")
    model_error = _validate_model_field(body)
    if model_error:
        return model_error

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return openai_error("messages 必须是非空数组")
    if any(not isinstance(m, dict) for m in messages):
        return openai_error("messages 数组中的每一项必须是对象")

    want_stream = _as_bool(body.get("stream"))
    platform = (kr["platform"] or "").strip() or None
    model = body.get("model") or proxy.default_model(platform)
    if db.get_setting("allow_model_passthrough", "1") != "1":
        model = proxy.default_model(platform)
    if not model:
        return openai_error("暂无已探测可用的对话模型，请先刷新模型目录", 503,
                            "model_registry_empty")
    body["model"] = model
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
                if usage_tracker.error:
                    ok = False
                    err = usage_tracker.error
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
        body = request.get_json(force=True)
    except Exception:
        return openai_error("请求体不是合法 JSON")
    if not isinstance(body, dict):
        return openai_error("请求体必须是 JSON 对象")
    model_error = _validate_model_field(body)
    if model_error:
        return model_error

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
                if usage_tracker.error:
                    ok = False
                    err = usage_tracker.error
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
        body = request.get_json(force=True)
    except Exception:
        return openai_error("请求体不是合法 JSON")
    if not isinstance(body, dict):
        return openai_error("请求体必须是 JSON 对象")
    model_error = _validate_model_field(body)
    if model_error:
        return model_error

    if not body.get("model"):
        return openai_error("缺少 model 字段")
    if "input" not in body:
        return openai_error("缺少 input 字段")

    # 模型解析 + pass-through 门控（与 chat 一致）
    platform = (kr["platform"] or "").strip() or None
    model = body.get("model")
    if db.get_setting("allow_model_passthrough", "1") != "1":
        model = proxy.default_model(platform)
    if not model:
        return openai_error("暂无已探测可用的对话模型，请先刷新模型目录", 503,
                            "model_registry_empty")
    body["model"] = model

    # Responses input → chat messages
    try:
        chat_body = R.responses_to_chat(body, default_model=proxy.default_model(platform))
    except ValueError as e:
        return openai_error(str(e), 400, "invalid_request_error")

    want_stream = _as_bool(body.get("stream"))
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
    # checkin_today 是持久化字段，跨天后必须按日期归零，避免页面假显示“已签到”。
    d["checkin_today"] = 1 if proxy.checkin_marked_today(d) else 0
    raw_streak = int(d.get("checkin_streak") if d.get("checkin_streak") is not None else -1)
    d["checkin_streak_known"] = raw_streak >= 0
    d["checkin_streak"] = max(0, raw_streak)
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
    if db.get_account(aid) is None:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    db.delete_account(aid)
    return jsonify({"ok": True})


@app.route("/api/accounts/<int:aid>", methods=["PATCH"])
@admin_required
def panel_account_patch(aid):
    if db.get_account(aid) is None:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    data = request.get_json(silent=True) or {}
    allowed = {}
    if "enabled" in data:
        allowed["enabled"] = 1 if _as_bool(data["enabled"]) else 0
    if "weight" in data:
        try:
            weight = int(data["weight"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "weight 必须是整数"}), 400
        if weight < 1:
            return jsonify({"ok": False, "error": "weight 必须大于 0"}), 400
        allowed["weight"] = weight
    if "label" in data:
        allowed["label"] = str(data["label"] or "")[:100]
    if "status" in data:
        status = str(data["status"] or "").strip()
        if status not in {"active", "error", "banned", "disabled"}:
            return jsonify({"ok": False, "error": "未知账号状态"}), 400
        allowed["status"] = status
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
        app.logger.warning("checkin account_id=%s platform=%s skipped=%s credit=%s",
                        aid, row["platform"], bool(res.get("skipped")),
                        res.get("claimed_credit", 0))
        return jsonify({"ok": True, **res})
    except Exception as e:
        app.logger.error("checkin account_id=%s platform=%s failed: %s",
                         aid, row["platform"], e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/accounts/<int:aid>/probe", methods=["POST"])
@admin_required
def panel_account_probe(aid):
    """单账号测活：锁定该账号用指定模型发一次真实请求。"""
    row = db.get_account(aid)
    if row is None:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    data = request.get_json(silent=True) or {}
    try:
        model = _string_field(data, "model")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
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
    data = request.get_json(silent=True) or {}
    try:
        platform = _string_field(data, "platform")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if platform and platform not in P.PLATFORMS:
        return jsonify({"ok": False, "error": "未知平台"}), 400
    try:
        d = proxy.checkin_all(silent=False, platform=platform or None)
        ok_count = sum(1 for x in d.get("results", []) if x.get("ok") and not x.get("skipped"))
        skip_count = sum(1 for x in d.get("results", []) if x.get("skipped"))
        fail_count = sum(1 for x in d.get("results", []) if not x.get("ok"))
        app.logger.warning("checkin_all platform=%s success=%s skipped=%s failed=%s results=%s",
                        platform or "all", ok_count, skip_count, fail_count,
                        d.get("results", []))
        return jsonify(d)
    except Exception as e:
        app.logger.error("checkin_all platform=%s failed: %s", platform or "all", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/accounts/import_token", methods=["POST"])
@admin_required
def panel_import_token():
    """手动导入 access_token(+refresh_token)，或粘贴 Cockpit Tools 导出的 JSON。"""
    data = request.get_json(silent=True) or {}
    try:
        platform = _string_field(data, "platform", "workbuddy") or "workbuddy"
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
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
        elif not isinstance(parsed, list):
            return jsonify({"ok": False, "error": "凭证 JSON 必须是对象或数组"}), 400
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
            else:
                # 保留无 token 项，让统一校验返回具体的项号与字段错误。
                items.append(it)
    else:
        try:
            at = _string_field(data, "access_token")
            refresh_token = _string_field(data, "refresh_token")
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        if not at:
            return jsonify({"ok": False, "error": "缺少 access_token"}), 400
        items.append({
            "access_token": at,
            "refresh_token": refresh_token,
            "email": data.get("email") or "",
            "uid": data.get("uid") or "",
            "domain": data.get("domain") or "",
            "expires_at": data.get("expires_at") or 0,
            "machine_id": data.get("machine_id") or "",
            "device_id": data.get("device_id") or "",
        })

    if not items:
        return jsonify({"ok": False, "error": "没有可导入的凭证"}), 400
    for idx, it in enumerate(items, 1):
        raw_access = it.get("access_token") or it.get("accessToken") or ""
        raw_refresh = it.get("refresh_token") or it.get("refreshToken") or ""
        if not isinstance(raw_access, str):
            return jsonify({"ok": False, "error": "第 %d 项 access_token 必须是字符串" % idx}), 400
        if not isinstance(raw_refresh, str):
            return jsonify({"ok": False, "error": "第 %d 项 refresh_token 必须是字符串" % idx}), 400
        if not raw_access.strip():
            return jsonify({"ok": False, "error": "第 %d 项缺少 access_token" % idx}), 400
        try:
            it["_expires_at"] = int(it.get("expires_at") or it.get("expiresAt") or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "第 %d 项 expires_at 必须是整数" % idx}), 400

    added, updated = 0, 0
    refresh_errors = []
    for it in items:
        at = str(it.get("access_token") or it.get("accessToken") or "").strip()
        rt = it.get("refresh_token") or it.get("refreshToken") or ""
        uid = str(it.get("uid") or "").strip()
        # 凭证文件有时只保存 token，不带账号元数据。先向上游补齐 uid，
        # 这样重复导入可以按真实账号更新，而不是不断产生无身份孤儿记录。
        upstream_info = {}
        if not uid:
            try:
                upstream_info = P.fetch_account_info(
                    platform, at, domain=it.get("domain") or None) or {}
            except Exception:
                upstream_info = {}
            uid = str(upstream_info.get("uid") or "").strip()
        exist = db.find_account_by_uid(platform, uid) if uid else None
        fields = dict(
            access_token=at, refresh_token=rt,
            expires_at=it.get("_expires_at", 0),
            domain=it.get("domain") or "", email=it.get("email") or upstream_info.get("email") or "",
            uid=uid, nickname=it.get("nickname") or upstream_info.get("nickname") or "",
            enterprise_id=(it.get("enterprise_id") or it.get("enterpriseId") or
                           upstream_info.get("enterprise_id") or ""),
            enterprise_name=(it.get("enterprise_name") or it.get("enterpriseName") or
                             upstream_info.get("enterprise_name") or ""),
            machine_id=it.get("machine_id") or it.get("machineId") or "",
            device_id=it.get("device_id") or it.get("deviceId") or "",
        )
        if exist:
            # Token-only 重新导入时，缺省字段代表“未提供”，不要清空已有
            # Trae 设备/账号元数据；真正的 token 字段仍按本次输入更新。
            update_fields = dict(fields)
            for key in ("email", "uid", "nickname", "enterprise_id", "enterprise_name",
                        "domain", "machine_id", "device_id"):
                if not update_fields.get(key):
                    update_fields.pop(key, None)
            db.update_account(exist["id"], status="active", status_reason="", **update_fields)
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
        except Exception as e:
            refresh_errors.append({"id": aid, "error": str(e)})

    return jsonify({"ok": True, "added": added, "updated": updated,
                    "refresh_errors": refresh_errors})


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
    try:
        platform = _string_field(data, "platform", "workbuddy") or "workbuddy"
        label = _string_field(data, "label", "用户贡献")[:40] or "用户贡献"
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if platform not in P.PLATFORMS:
        return jsonify({"ok": False, "error": "未知平台"}), 400
    if platform == "trae":
        return jsonify({"ok": False,
                        "error": "TraeWork 需要粘贴登录回调 URL，请在管理后台添加"}), 400
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
    if pend["done"]:
        return jsonify({"ok": False, "pending": False, "done": True,
                        "msg": "接入流程已完成"})

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

    if not info.get("uid"):
        return jsonify({"ok": False, "pending": True,
                        "error": "获取账号信息失败：上游未返回 uid，账号未入库"})

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
    quota_error = ""
    try:
        quota = proxy.refresh_quota(db.get_account(aid))
    except Exception as e:
        quota_error = str(e)

    return jsonify({"ok": True, "account_id": aid,
                    "email": info.get("email"), "nickname": info.get("nickname"),
                    "quota": quota, "quota_error": quota_error})


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
    raw_callback = data.get("callback_url") if "callback_url" in data else data.get("url", "")
    if raw_callback is None:
        raw_callback = ""
    if not isinstance(raw_callback, str):
        return jsonify({"ok": False, "error": "callback_url 必须是字符串"}), 400
    callback = raw_callback.strip()
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

    # machine/device 若前端传回则保存；回调未携带时保留已有账号元数据。
    machine_id = data.get("machine_id") or ""
    device_id = data.get("device_id") or ""
    exist = db.find_account_by_uid("trae", uid)
    if exist:
        machine_id = machine_id or exist["machine_id"] or ""
        device_id = device_id or exist["device_id"] or ""
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
    quota_error = ""
    try:
        proxy.refresh_quota(db.get_account(aid))
    except Exception as e:
        quota_error = str(e)
    return jsonify({"ok": True, "account_id": aid, "uid": uid,
                    "nickname": nickname, "quota_error": quota_error})


@app.route("/api/login/start", methods=["POST"])
@admin_required
def panel_login_start():
    data = request.get_json(silent=True) or {}
    platform = data.get("platform") or "workbuddy"
    if platform not in P.PLATFORMS:
        return jsonify({"ok": False, "error": "未知平台"}), 400
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
    if pend["done"]:
        return jsonify({"ok": False, "pending": False, "done": True,
                        "msg": "登录流程已完成"})
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

    if not info.get("uid"):
        return jsonify({"ok": False, "pending": True,
                        "error": "获取账号信息失败：上游未返回 uid，账号未入库"})

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
    quota_error = ""
    try:
        proxy.refresh_quota(db.get_account(aid))
    except Exception as e:
        quota_error = str(e)

    return jsonify({"ok": True, "account_id": aid,
                    "email": info.get("email"), "nickname": info.get("nickname"),
                    "quota_error": quota_error})


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
    try:
        platform = _string_field(data, "platform")
        name = _string_field(data, "name", "未命名", strip=False) or "未命名"
        requested_key = _string_field(data, "api_key") or None
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if platform and platform not in P.PLATFORMS:
        return jsonify({"ok": False, "error": "未知平台"}), 400
    key = db.add_api_key(name[:100], requested_key, platform)
    return jsonify({"ok": True, "api_key": key})


@app.route("/api/keys/<int:kid>", methods=["DELETE"])
@admin_required
def panel_key_delete(kid):
    if db.get_api_key_by_id(kid) is None:
        return jsonify({"ok": False, "error": "API Key 不存在"}), 404
    db.delete_api_key(kid)
    return jsonify({"ok": True})


@app.route("/api/keys/<int:kid>", methods=["PATCH"])
@admin_required
def panel_key_patch(kid):
    if db.get_api_key_by_id(kid) is None:
        return jsonify({"ok": False, "error": "API Key 不存在"}), 404
    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        db.toggle_api_key(kid, 1 if _as_bool(data["enabled"]) else 0)
    return jsonify({"ok": True})


# ==========================================================================
# 控制面板 - 设置 / 日志
# ==========================================================================

def _normalize_setting_value(key: str, value):
    """校验并归一化设置，避免坏值在实际请求线程才触发 500。"""
    binary = {"auto_refresh", "allow_model_passthrough", "auto_checkin",
              "public_join_enabled"}
    if key in binary:
        if isinstance(value, bool):
            return "1" if value else "0"
        text = str(value).strip().lower()
        if text not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
            raise ValueError("%s 必须是 0/1" % key)
        return "1" if text in {"1", "true", "yes", "on"} else "0"
    if key == "strategy":
        text = str(value or "").strip()
        if text not in {"least_used", "round_robin", "random"}:
            raise ValueError("strategy 必须是 least_used、round_robin 或 random")
        return text
    if key == "refresh_ahead":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError("refresh_ahead 必须是整数")
        if n < 0 or n > 7 * 24 * 3600:
            raise ValueError("refresh_ahead 范围必须是 0 到 604800 秒")
        return str(n)
    if key == "log_retention":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError("log_retention 必须是整数")
        if n < 0 or n > 1_000_000:
            raise ValueError("log_retention 范围必须是 0 到 1000000")
        return str(n)
    if key == "credit_threshold":
        try:
            n = float(value)
        except (TypeError, ValueError):
            raise ValueError("credit_threshold 必须是数字")
        if not math.isfinite(n) or n < 0:
            raise ValueError("credit_threshold 必须是非负数字")
        return str(value).strip()
    if key == "checkin_time":
        text = str(value or "").strip()
        try:
            hh, mm = (int(x) for x in text.split(":", 1))
        except (TypeError, ValueError):
            raise ValueError("checkin_time 必须是 HH:MM")
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("checkin_time 必须是 HH:MM")
        return "%02d:%02d" % (hh, mm)
    if key == "model_route_priority":
        return json.dumps(proxy.normalize_route_priority(value),
                          ensure_ascii=False, separators=(",", ":"))
    if key == "model_platform_disabled":
        if isinstance(value, str):
            try:
                parsed = json.loads(value or "{}")
            except Exception as e:
                raise ValueError("model_platform_disabled JSON 解析失败: %s" % e)
            if not isinstance(parsed, dict):
                raise ValueError("model_platform_disabled 必须是 JSON 对象")
            value = parsed
        elif not isinstance(value, dict):
            raise ValueError("model_platform_disabled 必须是 JSON 对象")
        return json.dumps(proxy.normalize_model_disabled_map(value),
                          ensure_ascii=False, separators=(",", ":"))
    return "" if value is None else str(value)


@app.route("/api/settings", methods=["GET", "POST"])
@admin_required
def panel_settings():
    if request.method == "GET":
        return jsonify(public_settings())
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    allowed = ("default_model", "strategy", "auto_refresh", "refresh_ahead",
               "model_base_override", "allow_model_passthrough",
               "log_retention", "system_prompt", "auto_checkin", "checkin_time",
               "credit_threshold", "free_models", "public_join_enabled",
               "model_route_priority", "model_platform_disabled")
    pending = {}
    for k in allowed:
        if k not in data:
            continue
        try:
            pending[k] = _normalize_setting_value(k, data[k])
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    for k, value in pending.items():
        db.set_setting(k, value)
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
    registry = proxy.normalize_model_registry(db.get_model_registry())
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
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit 必须是整数"}), 400
    limit = max(1, min(limit, 500))
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
    model = data.get("model") or proxy.default_model(platform)
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
@app.route("/api/health")
def health():
    return jsonify({"ok": True, "service": "agent2api", "ts": int(time.time())})


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


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
    ap = argparse.ArgumentParser(description="Agent2API - 三平台模型反代与号池面板")
    ap.add_argument("--host", default=os.environ.get("AGENT2API_HOST") or os.environ.get("HY3API_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("AGENT2API_PORT") or os.environ.get("HY3API_PORT", "8530")))
    ap.add_argument("--threads", type=int,
                    default=int(os.environ.get("AGENT2API_THREADS") or os.environ.get("HY3API_THREADS", "16")))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    threading.Thread(target=_maintenance_loop, daemon=True).start()

    n_acc = len(db.list_accounts())
    print("=" * 62, flush=True)
    print("  Agent2API 已启动", flush=True)
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
