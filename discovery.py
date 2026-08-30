# -*- coding: utf-8 -*-
"""运行时动态模型发现 —— 程序不内置任何模型目录。

设计原则（来自需求「程序不要内置任何模型，探测到什么模型就提供什么模型」）：
  - 对外暴露的模型目录 100% 来自真实账号的最小请求探测结果，存在 DB 注册表；
  - 本文件的 PROBE_SEED 只是「要尝试探测的候选 ID 列表」，绝不是对外目录，
    探测不到的候选会被自动丢弃，探测到的才会进入注册表；
  - 类型（chat / image / audio）由「哪个端点真正接受该模型」判定，而非写死；
  - 客户端请求一个注册表外的模型时，会触发实时探测回退（见 proxy.live_resolve）。

上游无模型枚举接口（/v3/config 仅 productFeatures，/v1/models 404），因此发现
只能通过「对候选 ID 做真实请求」实现。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import db
import proxy
import providers as P

# 文本 / 音频探测：只关心状态码，立即关连接，短超时
_PROBE_TIMEOUT = (8, 20)
# 图像探测：真实生成一张图，较慢，给更长 read 超时（仅在后台相位使用）
_IMAGE_PROBE_TIMEOUT = (10, 45)
# 单平台并发探测的线程数
_PROBE_WORKERS = 10
# 文本 / 音频相位的整体超时（保证 discover 同步部分快速返回，绝不无限加载）
_TEXT_AUDIO_OVERALL_TIMEOUT = 40
# 后台图像探测整体预算（后台线程，不占用用户等待时间）
_IMAGE_OVERALL_TIMEOUT = 180

# 探测种子：仅用于尝试探测的候选 ID。文本 / 图像 / 音频都涵盖，尽量广。
# 任何探测不到的 ID 不会进入对外目录；未来上游新增模型只要被探测命中即自动收录。
PROBE_SEED = [
    # ---- 文本 / 对话 ----
    "hy3", "hy3-x", "auto",
    "hy4-preview", "hy4-preview-x",
    "glm-5.3", "glm-5.3-flash",
    "glm-5.2", "glm-5.1", "glm-5.0", "glm-5v-turbo", "glm-4.6",
    "minimax-m3", "minimax-m2",
    "kimi-k3", "kimi-k3-1", "kimi-k2.7", "kimi-k2.6", "kimi-k2.5", "kimi-k2.7-code",
    "deepseek-v4-flash", "deepseek-v3.2", "deepseek-v4", "deepseek-v4-pro",
    "qwen3-max", "qwen3-coder",
    "hunyuan-turbo", "hunyuan-2.0",
    # ---- 图像生成 ----
    "hunyuan-image-v3.0-art", "hunyuan-image-v3.0", "hunyuan-image-v3.0-turbo",
    "hunyuan-image-plus", "hunyuan-image-plus-v2",
    "hunyuan-image-standard", "hunyuan-image-standard-v2",
    "hunyuan-image-pro", "hunyuan-image-lite", "hunyuan-image", "hunyuan-image-art",
    # ---- 音频（语音 / TTS / ASR） ----
    "hunyuan-audio", "hunyuan-tts", "hunyuan-asr",
    "glm-tts", "kimi-audio", "voice-clone", "speech-01",
]

# 端点类型探测顺序：先按 id 启发式选首选类型，路由错误再依次尝试其它类型
_TYPE_ORDER = ("chat", "image", "audio")


def model_type_from_id(mid: str) -> str:
    """仅用于「决定先探测哪个端点」的启发式，不是对外类型真相。"""
    low = (mid or "").lower()
    if "image" in low:
        return "image"
    if any(t in low for t in ("audio", "tts", "speech", "voice", "asr", "sound")):
        return "audio"
    return "chat"


def _account_for(platform: str):
    rows = [dict(r) for r in db.list_accounts(platform) if r["enabled"]]
    if not rows:
        return None
    return proxy.ensure_token(rows[0])


def _route_err(status: int, text: str) -> bool:
    """模型未注册类错误（11102 / 11103 aiart），意味着该端点不提供此模型。"""
    if status != 400:
        return False
    s = (text or "").lower()
    return any(x in s for x in ("11102", "11103", "service info not found",
                                "model [", "model not found", "model does not exist",
                                "backend [aiart]"))


def _probe_endpoint(acc: dict, platform: str, mid: str, typ: str):
    """对单个端点做最小请求，返回 (ok, info)。

    - chat / audio：走 chat 网关，status<400 即可用；**只读状态码并立即关闭连接**，
      不下载完整生成流（之前读 resp.text 会把整段生成拉完，极慢且浪费额度）。
    - image：走 /v2/images/generations，status<400 或 13212（任务队列满，模型仍存在）即可用。
    使用短超时 _PROBE_TIMEOUT，上游卡顿时快速失败而非无限等待。
    """
    try:
        if typ == "image":
            # 注：图像端点真实生成一张图，较慢；仅后台相位调用，使用更长 read 超时。
            resp = proxy.open_image_upstream(
                acc, {"model": mid, "prompt": "a small red circle, simple", "n": 1},
                timeout=_IMAGE_PROBE_TIMEOUT)
            status = resp.status_code
            text = resp.text[:300]
            resp.close()
            # 图像端点「模型存在」判定（宽松，避免把可用生图模型误判为不存在）：
            #   - status<400：真实生成成功，最确凿；
            #   - 13212：任务队列满（限流），模型仍存在；
            #   - 11115 等参数校验错误：请求已抵达模型处理层（路由通过）才被校验
            #     拒绝，同样证明模型存在，且返回极快、不真正生成（比真生成更省）。
            # 仅 11102 / 11103(aiart) / model not found 等「未注册」才判不存在。
            if status < 400 or "13212" in text or (status == 400 and "11115" in text):
                return True, "HTTP %s %s" % (status, text[:80])
            return False, "HTTP %s %s" % (status, text[:80])
        else:
            resp = proxy.open_upstream(
                acc, {"model": mid, "messages": [{"role": "user", "content": "hi"}]},
                timeout=_PROBE_TIMEOUT)
            status = resp.status_code
            if status < 400:
                # 模型存在：立即关闭连接，放弃下载后续生成内容（不浪费额度/时间）
                resp.close()
                return True, "HTTP %s" % status
            # 仅 4xx/5xx 才读取错误体（通常很小）以判定是否为「模型未注册」类错误
            text = resp.text[:300]
            resp.close()
            return False, "HTTP %s %s" % (status, text[:80])
    except Exception as e:
        return False, "ERR %s" % e


def probe_model_on_platform(platform: str, mid: str, acc: dict | None = None):
    """探测模型在单平台是否可用，返回 (ok, type_or_None, info)。

    acc 可选：批量探测时由调用方预取一个启用账号传入，避免每个候选都重新
    刷新令牌（刷新本身也走网络）。传 None 时回退到自动取号。
    """
    if acc is None:
        acc = _account_for(platform)
    if not acc:
        return False, None, "no enabled account"
    preferred = model_type_from_id(mid)
    order = [preferred] + [t for t in _TYPE_ORDER if t != preferred]
    for typ in order:
        ok, info = _probe_endpoint(acc, platform, mid, typ)
        if ok:
            return True, typ, info
        if _route_err(_status_of(info), info):
            continue  # 该端点不提供此模型，尝试下一个端点类型
        # 其它错误（429/500 等）：本平台暂不可用，停止尝试端点
        return False, None, info
    return False, None, "no endpoint accepts this model"


def _status_of(info: str) -> int:
    if info.startswith("HTTP "):
        try:
            return int(info.split(" ", 2)[1])
        except Exception:
            return 0
    return 0


# --------------------------------------------------------------------------
# 后台图像探测状态（图像真实生成较慢，放后台不阻塞「刷新模型目录」响应）
# --------------------------------------------------------------------------
_image_discovery_running = False
_image_discovery_lock = threading.Lock()
_registry_write_lock = threading.Lock()


def image_discovery_running() -> bool:
    """供 /api/models/discover/progress 查询：后台图像探测是否仍在进行。"""
    return _image_discovery_running


def _image_platforms() -> list:
    """图像端点只在国内网关 copilot.tencent.com 可用（国际站返回 11102）。

    仅对 model_base 为 copilot.tencent.com 的平台做图像探测，避免对国际站
    做注定失败且拖慢整体预算的请求。
    """
    return [k for k, p in P.PLATFORMS.items()
            if (p.get("model_base") or "").rstrip("/") == "https://copilot.tencent.com"]


def _image_model_missing(status: int, text: str) -> bool:
    """图像端点「模型未注册」类错误（11102 / 11103 aiart / model not found）。"""
    if status != 400:
        return False
    s = (text or "").lower()
    return any(x in s for x in ("11102", "11103", "service info not found",
                                "model [", "model not found", "model does not exist",
                                "backend [aiart]"))


def _merge_image_model(mid: str, platform: str) -> None:
    """把确认可用的图像模型合并写入注册表（带锁，避免与同步探测并发写竞争）。"""
    with _registry_write_lock:
        reg = db.get_model_registry()
        rec = reg.setdefault(mid, {"type": "image", "platforms": []})
        rec["type"] = "image"
        if platform not in rec["platforms"]:
            rec["platforms"].append(platform)
        db.set_model_registry(reg)


def _start_image_discovery():
    """启动后台图像探测（去重：已有实例运行时不再重复启动）。"""
    global _image_discovery_running
    with _image_discovery_lock:
        if _image_discovery_running:
            return
        _image_discovery_running = True
    threading.Thread(target=_run_image_discovery, daemon=True).start()


def _run_image_discovery():
    """后台并行探测图像种子模型，确认后实时合并进注册表。

    图像端点真实生成一张图，较慢，因此：
      - 单独后台执行，不阻塞 discover 同步响应；
      - 每探测给更长 read 超时 _IMAGE_PROBE_TIMEOUT；
      - 整体预算 _IMAGE_OVERALL_TIMEOUT（后台，不占用用户等待时间）；
      - 判定逻辑：status<400 / 13212 限流 / 11115 参数校验 均视为「模型存在」
        （请求已抵达模型处理层、路由通过），仅 11102/11103 等「未注册」才排除。
    完成后清 _image_discovery_running 标志，供前端轮询判断是否仍在探测。
    """
    global _image_discovery_running
    try:
        platform_acc = {}
        for p in _image_platforms():
            try:
                platform_acc[p] = _account_for(p)
            except Exception:
                platform_acc[p] = None

        # 跳过已收录的图像模型，避免每次刷新都重新真实生成图（既慢又耗积分）；
        # 仅探测「尚未确认」的生图种子（新增模型或首次部署时一次性补齐）。
        prior = db.get_model_registry()
        known_image = {mid for mid, rec in prior.items()
                       if isinstance(rec, dict) and rec.get("type") == "image"
                       and rec.get("platforms")}
        seed_mids = [mid for mid in PROBE_SEED
                     if model_type_from_id(mid) == "image" and mid not in known_image]
        tasks = [(p, mid) for p in _image_platforms() for mid in seed_mids]

        def _probe_one_image(platform, mid):
            acc = platform_acc.get(platform)
            if not acc:
                return False, None, "no enabled account"
            try:
                ok, info = _probe_endpoint(acc, platform, mid, "image")
                return ok, ("image" if ok else None), info
            except Exception as e:
                return False, None, "ERR %s" % e

        ex = ThreadPoolExecutor(max_workers=max(2, _PROBE_WORKERS // 2))
        try:
            fut_to = {ex.submit(_probe_one_image, p, mid): (p, mid) for p, mid in tasks}
            try:
                for fut in as_completed(fut_to, timeout=_IMAGE_OVERALL_TIMEOUT):
                    p, mid = fut_to[fut]
                    try:
                        ok, typ, _info = fut.result()
                    except Exception:
                        ok, typ = False, None
                    if ok and typ == "image":
                        _merge_image_model(mid, p)
            except TimeoutError:
                pass
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    finally:
        with _image_discovery_lock:
            _image_discovery_running = False


def sync_upstream_catalogs() -> dict:
    """优先用上游官方 CLI 模型目录刷新注册表；失败的平台回落到真实探测。

    返回 {platform: [新增模型 ID]}。官方目录来自 CLI 客户端白名单，可直接使用；
    上游未开放该接口（如国际站 500）时保持原探测机制，不额外发真实模型请求。
    """
    result = {}
    for platform in P.PLATFORMS:
        acc = None
        try:
            acc = _account_for(platform)
        except Exception:
            acc = None
        if not acc or not (acc.get("access_token") or "").strip():
            result[platform] = []
            continue
        try:
            result[platform] = P.merge_upstream_models(
                platform, acc["access_token"], uid=acc.get("uid") or None,
                machine_id=acc.get("machine_id") or None,
                device_id=acc.get("device_id") or None)
        except Exception:
            result[platform] = []
    return result


def run_discovery() -> dict:
    """跨三平台并行探测 PROBE_SEED，构建并持久化模型注册表。

    拆分两个相位以保证「刷新模型目录」既快又完整：
      相位 1（同步、快速返回）：探测【文本 / 音频】端点。这类请求只读状态码、
        立即关闭连接，不下载生成内容，秒级完成；受 _TEXT_AUDIO_OVERALL_TIMEOUT
        约束，保证控制面板同步部分有限时间内返回。
      相位 2（后台 daemon 线程）：探测【图像】端点。图像真实生成较慢，放后台
        执行，不阻塞 discover 响应；完成后实时合并进注册表，前端轮询即可看到。

    返回当前注册表（相位 1 结果 + 历史已收录模型）。已存在的注册表会被刷新。
    """
    # 每个平台预取一个启用账号，供该平台所有候选复用（避免逐个刷新令牌）。
    platform_acc: dict = {}
    for platform in P.PLATFORMS:
        try:
            platform_acc[platform] = _account_for(platform)
        except Exception:
            platform_acc[platform] = None

    # 相位 1：仅文本 / 音频候选（图像走后台，避免拖慢同步响应）
    # Trae 使用官方 get_detail_param 目录（sync_upstream_catalogs 已收录），
    # 不参与逐项真实探测，避免为每个模型消耗真实额度。
    probe_platforms = [p for p in P.PLATFORMS
                       if P.PLATFORMS[p].get("provider") != "trae"]
    ta_tasks = [(p, mid) for p in probe_platforms for mid in PROBE_SEED
                if model_type_from_id(mid) != "image"]
    registry: dict = {}

    def _probe_one(platform, mid):
        try:
            return probe_model_on_platform(platform, mid, acc=platform_acc.get(platform))
        except Exception as e:
            return False, None, "ERR %s" % e

    ex = ThreadPoolExecutor(max_workers=_PROBE_WORKERS)
    try:
        fut_to_task = {ex.submit(_probe_one, p, mid): (p, mid) for p, mid in ta_tasks}
        try:
            for fut in as_completed(fut_to_task, timeout=_TEXT_AUDIO_OVERALL_TIMEOUT):
                p, mid = fut_to_task[fut]
                try:
                    ok, typ, _info = fut.result()
                except Exception:
                    ok, typ = False, None
                if ok and typ:
                    rec = registry.setdefault(mid, {"type": typ, "platforms": []})
                    rec["type"] = typ
                    if p not in rec["platforms"]:
                        rec["platforms"].append(p)
        except TimeoutError:
            # 总超时：提交已探测到的部分结果，未完成的探测线程随进程退出被丢弃。
            pass
    finally:
        # 关键：不要 wait 未完成任务（否则会卡到剩余探测的超时），直接取消待执行
        # 任务并释放执行器。正在运行的探测受 _PROBE_TIMEOUT 约束，会在秒级内自行结束。
        ex.shutdown(wait=False, cancel_futures=True)

    # 合并历史记录：Trae 官方目录不参与逐项探测，其平台标记必须保留；
    # 本次未复核的模型也保留（探测超时/失败不等于下线）。
    prior = db.get_model_registry()
    for mid, rec in prior.items():
        if not isinstance(rec, dict) or not rec.get("platforms"):
            continue
        if mid not in registry:
            registry[mid] = rec
            continue
        cur = registry[mid]
        for p in rec["platforms"]:
            if p not in cur.get("platforms", []):
                cur.setdefault("platforms", []).append(p)

    db.set_model_registry(registry)

    # 相位 2：后台图像探测（不阻塞响应）
    _start_image_discovery()

    return registry


def live_probe_model(mid: str):
    """实时探测任意模型（客户端请求注册表外模型时调用）。

    跨三平台尝试；命中则写入注册表并返回记录，否则返回 None。
    """
    mid = (mid or "").strip()
    if not mid:
        return None
    rec = None
    for platform in P.PLATFORMS:
        if P.PLATFORMS[platform].get("provider") == "trae":
            # Trae 模型以官方目录为准；请求未收录模型时直接走候选平台探测。
            continue
        try:
            ok, typ, info = probe_model_on_platform(platform, mid)
        except Exception:
            ok, typ = False, None
        if ok and typ:
            rec = rec or {"type": typ, "platforms": []}
            rec["type"] = typ
            if platform not in rec["platforms"]:
                rec["platforms"].append(platform)
    if rec:
        registry = db.get_model_registry()
        existing = registry.get(mid, {"type": rec["type"], "platforms": []})
        existing["type"] = rec["type"]
        for p in rec["platforms"]:
            if p not in existing["platforms"]:
                existing["platforms"].append(p)
        registry[mid] = existing
        db.set_model_registry(registry)
        return existing
    return None
