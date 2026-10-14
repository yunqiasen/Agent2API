# -*- coding: utf-8 -*-
"""Agent2API - SQLite 数据层"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

import credentials

def _resolve_db_path() -> str:
    """数据库位置优先级：AGENT2API_DB > AGENT2API_DATA_DIR/agent2api.db > 代码目录。

    容器里挂 volume 到 /data 并设 AGENT2API_DATA_DIR=/data 即可持久化。
    """
    p = os.environ.get("AGENT2API_DB") or os.environ.get("HY3API_DB")
    if p:
        return p
    d = os.environ.get("AGENT2API_DATA_DIR") or os.environ.get("HY3API_DATA_DIR")
    if d:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "agent2api.db")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent2api.db")


DB_PATH = _resolve_db_path()

_local = threading.local()
_write_lock = threading.RLock()
_model_registry_lock = threading.RLock()

# 首次初始化用的默认凭据，可用环境变量覆盖（仅在数据库为空时生效）
DEFAULT_ADMIN_PASSWORD = os.environ.get("AGENT2API_ADMIN_PASSWORD") or os.environ.get("HY3API_ADMIN_PASSWORD") or "admin"
DEFAULT_API_KEY = os.environ.get("AGENT2API_DEFAULT_KEY") or "Agent2API"


def get_conn() -> sqlite3.Connection:
    """返回一个【全新】的 SQLite 连接。

    重要：每次都新建连接，不缓存。原因——

    在 WAL 模式下，长生命周期的缓存连接会持有「首次读时的快照」，导致跨线程
    （统计落库后台线程写入、请求线程读取）读到陈旧数据。改为每次新建连接后，
    每个读都看到最新已提交内容，彻底消除该竞态。

    调用方均以 `db.get_conn().execute(...)` 形式使用，表达式结束后连接引用计数
    归零、由 __del__ 立即关闭，不会泄漏文件描述符；开销仅为每次约亚毫秒级的
    连接创建，相对上游网络往返可忽略。
    """
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # autocommit：每条语句独立事务，读永远看到最新提交。
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def hash_pw(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    h = hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()
    return "%s$%s" % (salt, h)


def verify_pw(pw: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_pw(pw, salt), stored)


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    label           TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    uid             TEXT DEFAULT '',
    nickname        TEXT DEFAULT '',
    enterprise_id   TEXT DEFAULT '',
    enterprise_name TEXT DEFAULT '',
    access_token    TEXT NOT NULL,
    refresh_token   TEXT DEFAULT '',
    token_type      TEXT DEFAULT 'Bearer',
    expires_at      INTEGER DEFAULT 0,
    domain          TEXT DEFAULT '',
    state           TEXT DEFAULT '',
    machine_id      TEXT DEFAULT '',
    device_id       TEXT DEFAULT '',
    source          TEXT DEFAULT '',
    enabled         INTEGER DEFAULT 1,
    weight          INTEGER DEFAULT 1,
    status          TEXT DEFAULT 'active',
    status_reason   TEXT DEFAULT '',
    plan_type       TEXT DEFAULT '',
    quota_total     REAL DEFAULT 0,
    quota_used      REAL DEFAULT 0,
    quota_remain    REAL DEFAULT 0,
    quota_unit      TEXT DEFAULT 'credits',
    quota_raw       TEXT DEFAULT '',
    quota_updated_at INTEGER DEFAULT 0,
    checkin_today   INTEGER DEFAULT 0,
    checkin_streak  INTEGER DEFAULT 0,
    checkin_last_date TEXT DEFAULT '',
    checkin_credit  INTEGER DEFAULT 0,
    checkin_updated_at INTEGER DEFAULT 0,
    req_count       INTEGER DEFAULT 0,
    fail_count      INTEGER DEFAULT 0,
    last_used       INTEGER DEFAULT 0,
    last_error      TEXT DEFAULT '',
    created_at      INTEGER,
    updated_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_accounts_platform ON accounts(platform, enabled);

CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT DEFAULT '',
    api_key     TEXT NOT NULL UNIQUE,
    enabled     INTEGER DEFAULT 1,
    platform    TEXT DEFAULT '',
    model_scope TEXT NOT NULL DEFAULT 'all',
    allowed_models TEXT NOT NULL DEFAULT '[]',
    req_count   INTEGER DEFAULT 0,
    last_used   INTEGER DEFAULT 0,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER,
    api_key_id   INTEGER,
    account_id   INTEGER,
    platform     TEXT,
    model        TEXT,
    status       INTEGER,
    stream       INTEGER,
    duration_ms  INTEGER,
    prompt_chars INTEGER DEFAULT 0,
    error        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);

CREATE TABLE IF NOT EXISTS account_cooldowns (
    identity TEXT NOT NULL,
    service TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    retry_at INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY(identity, service, model)
);

CREATE TABLE IF NOT EXISTS reward_evidence (
    identity TEXT NOT NULL,
    record_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY(identity, record_id)
);

CREATE TABLE IF NOT EXISTS daily_reward_state (
    identity TEXT NOT NULL,
    activity_date TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(identity, activity_date)
);

CREATE TABLE IF NOT EXISTS pending_login (
    state       TEXT PRIMARY KEY,
    platform    TEXT,
    auth_url    TEXT,
    label       TEXT DEFAULT '',
    created_at  INTEGER,
    done        INTEGER DEFAULT 0
);
"""

DEFAULT_SETTINGS = {
    # 留空表示从运行时探测注册表自动选择首个 chat 模型；不内置模型 ID。
    "default_model": "",
    "strategy": "least_used",
    "auto_refresh": "1",
    "refresh_ahead": "900",
    "rate_limit_default_seconds": "60",
    "model_base_override": "",
    "allow_model_passthrough": "1",
    "log_retention": "2000",
    # 国际站要求首条 system 消息，客户端没传时用它补齐
    "system_prompt": "",
    # 每日维护：普通签到及国际活动/奖励使用同一日程
    "auto_checkin": "1",
    "checkin_time": "00:00",
    # 积分阈值门控：账号剩余积分低于阈值时，禁止调用「会扣积分」的模型
    # （hy3 等计费模型）；free_models 列出的模型视为免费，永远放行。
    "credit_threshold": "1",
    "free_models": "",
    # 是否开放「公共号池扩充」入口：终端用户可扫码/网页登录把自己的账号贡献进号池
    "public_join_enabled": "1",
}


def init_db():
    conn = get_conn()
    with _write_lock:
        conn.executescript(SCHEMA)
        conn.commit()

    _migrate_accounts_columns()
    _migrate_logs_columns()
    _migrate_api_keys_columns()

    if get_setting("admin_password") is None:
        set_setting("admin_password", hash_pw(DEFAULT_ADMIN_PASSWORD))
    for k, v in DEFAULT_SETTINGS.items():
        if get_setting(k) is None:
            set_setting(k, v)

    row = conn.execute("SELECT COUNT(*) c FROM api_keys").fetchone()
    if row["c"] == 0:
        add_api_key("默认密钥", DEFAULT_API_KEY)


def _dedupe_account_uids(c: sqlite3.Connection) -> None:
    """合并同渠道同 uid 的重复账号，保留最早 id 与最新凭证快照。"""
    groups = c.execute(
        "SELECT platform,uid FROM accounts WHERE uid<>'' "
        "GROUP BY platform,uid HAVING COUNT(*)>1"
    ).fetchall()
    for group in groups:
        rows = c.execute(
            "SELECT * FROM accounts WHERE platform=? AND uid=? ORDER BY id ASC",
            (group["platform"], group["uid"]),
        ).fetchall()
        canonical = rows[0]
        latest = max(rows, key=lambda r: (int(r["updated_at"] or 0), int(r["id"])))
        fields = {}
        for key in latest.keys():
            if key in {"id", "platform", "uid", "created_at",
                       "req_count", "fail_count"}:
                continue
            fields[key] = latest[key]
        fields["req_count"] = sum(int(r["req_count"] or 0) for r in rows)
        fields["fail_count"] = sum(int(r["fail_count"] or 0) for r in rows)
        cols = ", ".join("%s=?" % key for key in fields)
        c.execute("UPDATE accounts SET %s WHERE id=?" % cols,
                  list(fields.values()) + [canonical["id"]])
        duplicate_ids = [r["id"] for r in rows[1:]]
        # 调用日志没有外键约束；删除重复账号前先改指向保留账号，
        # 否则迁移后历史统计会留下不存在的 account_id。
        c.execute("UPDATE logs SET account_id=? WHERE account_id IN (%s)" %
                  ",".join("?" * len(duplicate_ids)),
                  [canonical["id"], *duplicate_ids])
        c.execute("DELETE FROM accounts WHERE id IN (%s)" %
                  ",".join("?" * len(duplicate_ids)), duplicate_ids)


def migrate_legacy_platforms() -> None:
    """归一旧渠道别名，并合并迁移后重复的真实账号。"""
    aliases = {"codebuddy_cn": "workbuddy", "ide": "codebuddy"}
    with _write_lock:
        c = get_conn()
        # 旧库可能已建唯一索引；先移除，完成别名归一与去重后再恢复。
        c.execute("DROP INDEX IF EXISTS ux_accounts_platform_uid")
        for table in ("accounts", "api_keys", "pending_login", "logs"):
            for old, new in aliases.items():
                c.execute("UPDATE %s SET platform=? WHERE platform=?" % table,
                          (new, old))
        _dedupe_account_uids(c)
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_platform_uid "
            "ON accounts(platform,uid) WHERE uid<>''"
        )
        c.commit()


def _migrate_accounts_columns():
    """为已存在的 accounts 表补齐签到相关字段（SCHEMA 的 IF NOT EXISTS 不会改已有表）。

    仅在列缺失时 ALTER，保证老库升级无损。
    """
    needed = {
        "checkin_today": "INTEGER DEFAULT 0",
        "checkin_streak": "INTEGER DEFAULT 0",
        "checkin_last_date": "TEXT DEFAULT ''",
        "checkin_credit": "INTEGER DEFAULT 0",
        "checkin_updated_at": "INTEGER DEFAULT 0",
        "source": "TEXT DEFAULT ''",
        "machine_id": "TEXT DEFAULT ''",
        "device_id": "TEXT DEFAULT ''",
        "last_refresh_at": "INTEGER DEFAULT 0",
        "last_refresh_attempt": "INTEGER DEFAULT 0",
        "refresh_error": "TEXT DEFAULT ''",
        "refresh_failures": "INTEGER DEFAULT 0",
        "refresh_retry_at": "INTEGER DEFAULT 0",
    }
    with _write_lock:
        c = get_conn()
        existing = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        for col, typ in needed.items():
            if col not in existing:
                c.execute("ALTER TABLE accounts ADD COLUMN %s %s" % (col, typ))
        c.commit()


def _migrate_api_keys_columns():
    """旧Key增量升级为不限模型；已有模型权限及统计原样保留。"""
    with _write_lock:
        c = get_conn()
        existing = {r[1] for r in c.execute("PRAGMA table_info(api_keys)")}
        for col, typ in {
            "model_scope": "TEXT NOT NULL DEFAULT 'all'",
            "allowed_models": "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            if col not in existing:
                c.execute("ALTER TABLE api_keys ADD COLUMN %s %s" % (col, typ))
        c.commit()


def _migrate_logs_columns():
    """为已存在的 logs 表补齐 token 用量与积分扣费字段。

    新增列：
        prompt_tokens     - 提示 tokens（来自上游 usage）
        completion_tokens - 补全 tokens
        total_tokens      - 合计 tokens
        credit_cost       - 本次调用从账号扣除的积分（按 model_price 折算）
    """
    needed = {
        "prompt_tokens": "INTEGER DEFAULT 0",
        "completion_tokens": "INTEGER DEFAULT 0",
        "total_tokens": "INTEGER DEFAULT 0",
        "credit_cost": "REAL DEFAULT 0",
        # 1 = credit_cost 来自上游 usage.credit；0 = 历史人工估算/未知。
        # 统计面板只汇总真实上游积分，避免把旧估算值冒充实际扣费。
        "credit_actual": "INTEGER DEFAULT 0",
        "request_id": "TEXT DEFAULT ''",
        "attempts": "TEXT DEFAULT '[]'",
    }
    with _write_lock:
        c = get_conn()
        existing = {r[1] for r in c.execute("PRAGMA table_info(logs)")}
        for col, typ in needed.items():
            if col not in existing:
                c.execute("ALTER TABLE logs ADD COLUMN %s %s" % (col, typ))
        # 复合索引（按模型+日期聚合用）
        existing_idx = {
            (r[0] if len(r) > 0 else "")
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_logs_%'"
            )
        }
        for idx_name, cols in (
            ("idx_logs_model_ts", "model,ts"),
            ("idx_logs_platform_ts", "platform,ts"),
        ):
            if idx_name not in existing_idx:
                c.execute("CREATE INDEX IF NOT EXISTS %s ON logs(%s)" % (idx_name, cols))
        c.commit()


def get_secret_key() -> str:
    """Flask session 密钥。

    优先取环境变量；否则持久化在数据库里，保证容器重启后登录态不失效。
    """
    env = os.environ.get("AGENT2API_SECRET") or os.environ.get("HY3API_SECRET")
    if env:
        return env
    v = get_setting("_secret_key")
    if not v:
        v = secrets.token_hex(32)
        set_setting("_secret_key", v)
    return v


# ------------------------------ settings ------------------------------

def get_setting(key: str, default=None):
    row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value):
    with _write_lock:
        c = get_conn()
        c.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), int(time.time())))
        c.commit()


def all_settings() -> dict:
    rows = get_conn().execute("SELECT key,value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# --------------------------------------------------------------------------
# 动态模型注册表（运行时真实探测得到，不是内置目录）
# --------------------------------------------------------------------------

def get_model_registry() -> dict:
    """返回 {model_id: {"type": "chat"|"image"|"audio", "platforms": [...]} }。"""
    import json as _json
    raw = get_setting("discovered_models", "{}") or "{}"
    try:
        d = _json.loads(raw)
    except Exception:
        d = {}
    return d if isinstance(d, dict) else {}


def set_model_registry(registry: dict) -> None:
    import json as _json
    with _model_registry_lock:
        set_setting("discovered_models", _json.dumps(registry, ensure_ascii=False))


def update_model_registry(mutator) -> dict:
    """在单一进程锁内完成注册表读-改-写，防止并发发现结果互相覆盖。"""
    import copy
    with _model_registry_lock:
        current = copy.deepcopy(get_model_registry())
        updated = mutator(current)
        if updated is None:
            updated = current
        if not isinstance(updated, dict):
            raise TypeError("模型注册表更新器必须返回 dict 或 None")
        set_model_registry(updated)
        return copy.deepcopy(updated)


# --------------------------------------------------------------------------
# 探测负面缓存（运行时真实探测得到的「不存在」结论，带 TTL）
# --------------------------------------------------------------------------
# 客户端请求一个注册表外的模型时，proxy.model_route 会触发实时探测回退
# （见 discovery.live_probe_model）。若探测确认该模型在各平台均不可用，
# 把结论缓存一段时间，避免对同一个不存在的模型反复发起昂贵的真实请求。
# 缓存条目 {model_id: unix_ts}；TTL 由 proxy 侧常量控制，过期后自动重探。

def get_probe_failures() -> dict:
    """返回 {model_id: unix_ts}。"""
    import json as _json
    raw = get_setting("model_probe_failures", "{}") or "{}"
    try:
        d = _json.loads(raw)
    except Exception:
        d = {}
    return d if isinstance(d, dict) else {}


def set_probe_failures(failures: dict) -> None:
    import json as _json
    set_setting("model_probe_failures", _json.dumps(failures, ensure_ascii=False))


# ------------------------------ accounts ------------------------------

def add_account(platform, access_token, refresh_token="", expires_at=0, domain="",
                token_type="Bearer", email="", uid="", nickname="",
                enterprise_id="", enterprise_name="", state="", label="",
                source="", machine_id="", device_id="") -> int:
    """新增账号；同渠道同 uid 已存在时原位更新并返回原 id。"""
    expires_at = credentials.expiry(access_token, expires_at)
    now = int(time.time())
    with _write_lock:
        c = get_conn()
        existing = None
        if uid:
            existing = c.execute(
                "SELECT * FROM accounts WHERE platform=? AND uid=?",
                (platform, uid),
            ).fetchone()
        if existing is not None:
            fields = {
                "access_token": access_token,
                "refresh_token": refresh_token or existing["refresh_token"] or "",
                "expires_at": int(expires_at or 0),
                "domain": domain or existing["domain"] or "",
                "token_type": token_type or existing["token_type"] or "Bearer",
                "email": email or existing["email"] or "",
                "nickname": nickname or existing["nickname"] or "",
                "enterprise_id": enterprise_id or existing["enterprise_id"] or "",
                "enterprise_name": enterprise_name or existing["enterprise_name"] or "",
                "state": state or existing["state"] or "",
                "label": label or existing["label"] or "",
                "source": source or existing["source"] or "",
                "machine_id": machine_id or existing["machine_id"] or "",
                "device_id": device_id or existing["device_id"] or "",
                "status": "active",
                "status_reason": "",
                "updated_at": now,
            }
            cols = ", ".join("%s=?" % key for key in fields)
            c.execute("UPDATE accounts SET %s WHERE id=?" % cols,
                      list(fields.values()) + [existing["id"]])
            c.commit()
            return int(existing["id"])
        cur = c.execute(
            """INSERT INTO accounts(platform,label,email,uid,nickname,enterprise_id,
               enterprise_name,access_token,refresh_token,token_type,expires_at,domain,
               state,source,machine_id,device_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (platform, label, email, uid, nickname, enterprise_id, enterprise_name,
             access_token, refresh_token or "", token_type, int(expires_at or 0),
             domain or "", state, source or "", machine_id or "", device_id or "",
             now, now))
        c.commit()
        return cur.lastrowid


def find_account_by_uid(platform, uid):
    if not uid:
        return None
    return get_conn().execute(
        "SELECT * FROM accounts WHERE platform=? AND uid=?", (platform, uid)).fetchone()


def update_account(account_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = int(time.time())
    cols = ", ".join("%s=?" % k for k in fields)
    vals = list(fields.values()) + [account_id]
    with _write_lock:
        c = get_conn()
        c.execute("UPDATE accounts SET %s WHERE id=?" % cols, vals)
        c.commit()


INTERNATIONAL_CHANNELS = ("codebuddy", "workbuddy_intl")


def credential_scope(acc) -> tuple:
    """同国际身份串行续期；不同会话只串行，不相互覆盖凭证。"""
    if acc["platform"] in INTERNATIONAL_CHANNELS and acc.get("uid"):
        return ("intl", acc["uid"], acc.get("enterprise_id") or "")
    return ("account", acc["id"])


def save_refresh_result(acc, fields) -> dict:
    """原子保存同一份会话的轮换结果；并发重新导入的新凭证不被旧请求覆盖。"""
    fields = dict(fields, updated_at=int(time.time()))
    with _write_lock:
        with get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT * FROM accounts WHERE id=?", (acc["id"],)).fetchone()
            if current is None:
                return {}
            if (current["access_token"] != acc["access_token"] or
                    current["refresh_token"] != acc["refresh_token"]):
                return dict(current)
            where = "id=?"
            args = [acc["id"]]
            if credential_scope(acc)[0] == "intl" and acc.get("refresh_token"):
                where = "platform IN (?,?) AND uid=? AND COALESCE(enterprise_id,'')=? AND refresh_token=?"
                args = [*INTERNATIONAL_CHANNELS, acc["uid"], acc.get("enterprise_id") or "", acc["refresh_token"]]
            cols = ", ".join(k + "=?" for k in fields)
            c.execute("UPDATE accounts SET %s WHERE %s" % (cols, where), [*fields.values(), *args])
            return dict(c.execute("SELECT * FROM accounts WHERE id=?", (acc["id"],)).fetchone())


def quota_identity(acc) -> tuple:
    """额度账本身份与凭证会话身份不同；只对已知国际 UID/租户去重。"""
    return credential_scope(acc)


def pool_quota_remain() -> float:
    snapshots = {}
    for row in list_accounts():
        acc = dict(row)
        key = quota_identity(acc)
        previous = snapshots.get(key)
        if previous is None or (acc.get("quota_updated_at") or 0, acc["id"]) > (previous.get("quota_updated_at") or 0, previous["id"]):
            snapshots[key] = acc
    return sum(float(a.get("quota_remain") or 0) for a in snapshots.values())


def delete_account(account_id: int):
    with _write_lock:
        c = get_conn()
        c.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        c.commit()


def get_account(account_id: int):
    return get_conn().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()


def list_accounts(platform: str | None = None) -> list:
    sql = "SELECT * FROM accounts"
    args = []
    if platform:
        sql += " WHERE platform=?"
        args.append(platform)
    sql += " ORDER BY id ASC"
    return get_conn().execute(sql, args).fetchall()


def callable_accounts(platform: str | None = None, strategy: str = "least_used",
                 exclude: list | None = None, min_quota: float = 0):
    """按策略列出符合调用开关及额度门槛的账号。exclude 为已失败的 account_id 列表。

    min_quota > 0 时仅选「剩余积分 >= min_quota」的账号；若账号从未刷新过额度
    （quota_updated_at=0，无法判断），一律放行，避免误杀未初始化的账号。
    """
    sql = "SELECT * FROM accounts WHERE enabled=1 AND status NOT IN ('banned', 'disabled')"
    args = []
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    if exclude:
        sql += " AND id NOT IN (%s)" % ",".join("?" * len(exclude))
        args.extend(exclude)
    if min_quota:
        sql += " AND (quota_remain >= ? OR quota_updated_at = 0)"
        args.append(min_quota)

    if strategy == "round_robin":
        sql += " ORDER BY last_used ASC, id ASC"
    elif strategy == "random":
        sql += " ORDER BY RANDOM()"
    else:
        sql += " ORDER BY req_count ASC, last_used ASC, id ASC"

    return get_conn().execute(sql, args).fetchall()


def pick_account(platform: str | None = None, strategy: str = 'least_used',
                 exclude: list | None = None, min_quota: float = 0, model=None):
    return next((row for row in callable_accounts(platform, strategy, exclude, min_quota)
                 if not active_cooldowns(row, model=model)), None)


def count_accounts(platform: str | None = None, enabled: int | None = None) -> int:
    sql = "SELECT COUNT(*) c FROM accounts WHERE 1=1"
    args = []
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    if enabled is not None:
        sql += " AND enabled=?"
        args.append(int(enabled))
    return get_conn().execute(sql, args).fetchone()["c"]


def bump_account(account_id: int, ok: bool, error: str = ""):
    now = int(time.time())
    with _write_lock:
        c = get_conn()
        if ok:
            c.execute("UPDATE accounts SET req_count=req_count+1, last_used=?, "
                      "last_error='' WHERE id=?", (now, account_id))
        else:
            c.execute("UPDATE accounts SET req_count=req_count+1, fail_count=fail_count+1, "
                      "last_used=?, last_error=? WHERE id=?", (now, (error or "")[:500], account_id))
        c.commit()


def save_quota(account_id: int, summary: dict):
    update_account(
        account_id,
        plan_type=summary.get("plan_type", ""),
        quota_total=summary.get("total", 0),
        quota_used=summary.get("used", 0),
        quota_remain=summary.get("remain", 0),
        quota_unit=summary.get("unit", "credits"),
        quota_raw=json.dumps(summary.get("packages", []), ensure_ascii=False)[:20000],
        quota_updated_at=int(time.time()),
    )


# ------------------------------ api keys ------------------------------

class DuplicateApiKey(ValueError):
    """调用密钥已存在，创建请求应明确报冲突。"""


def add_api_key(name: str, key: str | None = None, platform: str = "", *,
                model_scope: str = "all", allowed_models: list[str] | None = None) -> str:
    key = key or ("sk-agent2api-" + secrets.token_urlsafe(24))
    with _write_lock:
        c = get_conn()
        try:
            c.execute("INSERT INTO api_keys(name,api_key,platform,model_scope,allowed_models,created_at) "
                      "VALUES(?,?,?,?,?,?)", (name, key, platform, model_scope,
                                             json.dumps(allowed_models or []), int(time.time())))
        except sqlite3.IntegrityError:
            if c.execute("SELECT id FROM api_keys WHERE api_key=?", (key,)).fetchone():
                raise DuplicateApiKey("该 API Key 已存在，请编辑原密钥或使用其他 Key") from None
            raise
        c.commit()
    return key


def list_api_keys() -> list:
    return get_conn().execute("SELECT * FROM api_keys ORDER BY id ASC").fetchall()


def get_api_key(key: str):
    return get_conn().execute(
        "SELECT * FROM api_keys WHERE api_key=? AND enabled=1", (key,)).fetchone()


def get_api_key_by_id(kid: int):
    return get_conn().execute(
        "SELECT * FROM api_keys WHERE id=?", (kid,)).fetchone()


def delete_api_key(kid: int):
    with _write_lock:
        c = get_conn()
        c.execute("DELETE FROM api_keys WHERE id=?", (kid,))
        c.commit()


def edit_api_key(kid: int, prepare) -> bool:
    """在同一事务内读取、校验、更新权限，失败整体回滚且保留Key身份。"""
    with _write_lock:
        with get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM api_keys WHERE id=?", (kid,)).fetchone()
            if row is None:
                return False
            fields = prepare(dict(row))
            if fields.keys() - {"platform", "model_scope", "allowed_models", "enabled"}:
                raise ValueError("不支持的密钥更新字段")
            if "allowed_models" in fields:
                fields["allowed_models"] = json.dumps(fields["allowed_models"], ensure_ascii=False)
            if fields:
                c.execute("UPDATE api_keys SET %s WHERE id=?" %
                          ",".join(k + "=?" for k in fields), [*fields.values(), kid])
    return True


def toggle_api_key(kid: int, enabled: int):
    with _write_lock:
        c = get_conn()
        c.execute("UPDATE api_keys SET enabled=? WHERE id=?", (int(enabled), kid))
        c.commit()


def bump_api_key(kid: int):
    with _write_lock:
        c = get_conn()
        c.execute("UPDATE api_keys SET req_count=req_count+1, last_used=? WHERE id=?",
                  (int(time.time()), kid))
        c.commit()


# ------------------------------ logs ------------------------------

def add_log(api_key_id, account_id, platform, model, status, stream,
            duration_ms, prompt_chars=0, error="",
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
            credit_cost=0.0, credit_actual=False, request_id="", attempts=None):
    with _write_lock:
        c = get_conn()
        c.execute("""INSERT INTO logs(ts,api_key_id,account_id,platform,model,status,
                     stream,duration_ms,prompt_chars,error,
                     prompt_tokens,completion_tokens,total_tokens,credit_cost,credit_actual,request_id,attempts)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (int(time.time()), api_key_id, account_id, platform, model, status,
                   1 if stream else 0, duration_ms, prompt_chars, (error or "")[:500],
                   int(prompt_tokens or 0), int(completion_tokens or 0),
                   int(total_tokens or 0), float(credit_cost or 0),
                   1 if credit_actual else 0, request_id, json.dumps(attempts or [], ensure_ascii=False)))
        c.commit()


def overview_usage(days: int = 7) -> dict:
    """聚合仪表盘数据：剩余额度 / 调用趋势 / 各模型 / 各平台 / 扣费明细。"""
    import time as _t
    c = get_conn()
    now = int(_t.time())
    day_ago = now - 86400
    week_ago = now - 86400 * days

    # 剩余额度（号池所有账号的剩余积分合计 = quota_remain 之和）
    pool_row = c.execute(
        "SELECT COALESCE(SUM(COALESCE(quota_remain,0)), 0.0) credits, COUNT(*) total, "
        "       SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) enabled "
        "FROM accounts"
    ).fetchone()
    pool_credits = pool_quota_remain()

    # 今日调用
    today = c.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(total_tokens), 0) tokens, "
        "       COALESCE(SUM(prompt_tokens), 0) pt, "
        "       COALESCE(SUM(completion_tokens), 0) ct, "
        "       COALESCE(SUM(CASE WHEN credit_actual=1 THEN credit_cost ELSE 0 END), 0.0) cost "
        "FROM logs WHERE ts>=? AND status<400",
        (day_ago,),
    ).fetchone()
    today_calls = int(today["c"] or 0)

    # 7 天每日调用量（含 0 填充）
    bucket_rows = c.execute(
        "SELECT (ts/?)*? AS bucket, "
        "       COUNT(*) c, COALESCE(SUM(total_tokens),0) tokens, "
        "       COALESCE(SUM(CASE WHEN credit_actual=1 THEN credit_cost ELSE 0 END),0.0) cost "
        "FROM logs WHERE ts>=? "
        "GROUP BY bucket ORDER BY bucket ASC",
        (86400, 86400, week_ago),
    ).fetchall()
    by_day = {}
    for r in bucket_rows:
        key = str(r["bucket"])
        by_day[key] = {
            "bucket": key,
            "calls": int(r["c"] or 0),
            "tokens": int(r["tokens"] or 0),
            "cost": float(r["cost"] or 0),
        }
    series = []
    for i in range(days):
        b = (now - (days - 1 - i) * 86400) // 86400 * 86400
        rec = by_day.get(str(b)) or {"bucket": str(b), "calls": 0, "tokens": 0, "cost": 0}
        rec["bucket"] = str(b)
        series.append(rec)

    # 按模型
    by_model_rows = c.execute(
        "SELECT model, COUNT(*) c, COALESCE(SUM(total_tokens),0) tokens, "
        "       COALESCE(SUM(CASE WHEN credit_actual=1 THEN credit_cost ELSE 0 END),0.0) cost "
        "FROM logs WHERE ts>=? AND status<400 AND model<>'' "
        "GROUP BY model ORDER BY tokens DESC LIMIT 12",
        (week_ago,),
    ).fetchall()
    by_model = [
        {"model": r["model"] or "-", "calls": int(r["c"] or 0),
         "tokens": int(r["tokens"] or 0), "cost": float(r["cost"] or 0)}
        for r in by_model_rows
    ]

    # 按平台
    by_platform_rows = c.execute(
        "SELECT platform, COUNT(*) c, COALESCE(SUM(total_tokens),0) tokens, "
        "       COALESCE(SUM(CASE WHEN credit_actual=1 THEN credit_cost ELSE 0 END),0.0) cost "
        "FROM logs WHERE ts>=? AND status<400 AND platform<>'' "
        "GROUP BY platform ORDER BY cost DESC",
        (week_ago,),
    ).fetchall()
    by_platform = [
        {"platform": r["platform"] or "-", "calls": int(r["c"] or 0),
         "tokens": int(r["tokens"] or 0), "cost": float(r["cost"] or 0)}
        for r in by_platform_rows
    ]

    # 扣费明细（最近 10 条 > 0）
    deduct_rows = c.execute(
        "SELECT id, ts, platform, model, account_id, status, "
        "       total_tokens, credit_cost "
        "FROM logs WHERE ts>=? AND credit_actual=1 AND credit_cost>0 "
        "ORDER BY id DESC LIMIT 10",
        (day_ago,),
    ).fetchall()
    deductions = [
        {
            "id": int(r["id"]),
            "ts": int(r["ts"] or 0),
            "platform": r["platform"] or "",
            "model": r["model"] or "",
            "account_id": int(r["account_id"] or 0),
            "status": int(r["status"] or 0),
            "tokens": int(r["total_tokens"] or 0),
            "cost": float(r["credit_cost"] or 0),
        }
        for r in deduct_rows
    ]

    return {
        "now": now,
        "pool": {
            "credits": pool_credits,
            "accounts": int(pool_row["total"] or 0),
            "enabled": int(pool_row["enabled"] or 0),
        },
        "today": {
            "calls": today_calls,
            "tokens": int(today["tokens"] or 0),
            "prompt_tokens": int(today["pt"] or 0),
            "completion_tokens": int(today["ct"] or 0),
            "cost": float(today["cost"] or 0),
        },
        "series": series,
        "by_model": by_model,
        "by_platform": by_platform,
        "deductions": deductions,
        "credit_source": "upstream_usage",
    }


def list_logs(limit: int = 100) -> list:
    return get_conn().execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def trim_logs(keep: int = 2000):
    with _write_lock:
        c = get_conn()
        c.execute("DELETE FROM logs WHERE id NOT IN "
                  "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)", (keep,))
        c.commit()


def stats() -> dict:
    c = get_conn()
    day_ago = int(time.time()) - 86400
    row = c.execute(
        "SELECT COUNT(*) total, SUM(CASE WHEN status<400 THEN 1 ELSE 0 END) ok "
        "FROM logs WHERE ts>=?", (day_ago,)).fetchone()
    acc = c.execute(
        "SELECT COUNT(*) total, SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) active, "
        "SUM(quota_remain) remain FROM accounts").fetchone()
    keys = c.execute("SELECT COUNT(*) total FROM api_keys WHERE enabled=1").fetchone()
    return {
        "req_24h": row["total"] or 0,
        "ok_24h": row["ok"] or 0,
        "accounts": acc["total"] or 0,
        "accounts_active": acc["active"] or 0,
        "quota_remain": round(pool_quota_remain(), 2),
        "keys": keys["total"] or 0,
    }


# ------------------------------ pending login ------------------------------

def add_pending(state, platform, auth_url, label=""):
    with _write_lock:
        c = get_conn()
        c.execute("INSERT OR REPLACE INTO pending_login(state,platform,auth_url,label,"
                  "created_at,done) VALUES(?,?,?,?,?,0)",
                  (state, platform, auth_url, label, int(time.time())))
        c.commit()


def get_pending(state):
    return get_conn().execute(
        "SELECT * FROM pending_login WHERE state=?", (state,)).fetchone()


def finish_pending(state):
    with _write_lock:
        c = get_conn()
        c.execute("UPDATE pending_login SET done=1 WHERE state=?", (state,))
        c.commit()


def cleanup_pending(older_than: int = 3600):
    with _write_lock:
        c = get_conn()
        c.execute("DELETE FROM pending_login WHERE created_at < ?",
                  (int(time.time()) - older_than,))
        c.commit()


def row_to_dict(row) -> dict:
    return dict(row) if row is not None else {}


# 每日活动使用真实计费身份；两张客户端卡片不产生两份账本。
def maintenance_identity(acc) -> str:
    return json.dumps(quota_identity(dict(acc)), ensure_ascii=True, separators=(",", ":"))


def get_daily_reward_state(acc, day: str) -> dict:
    row = get_conn().execute(
        "SELECT data FROM daily_reward_state WHERE identity=? AND activity_date=?",
        (maintenance_identity(acc), day)).fetchone()
    return json.loads(row["data"]) if row else {}


def save_daily_reward_state(acc, day: str, data: dict) -> None:
    with _write_lock:
        get_conn().execute(
            "INSERT INTO daily_reward_state VALUES(?,?,?,?) "
            "ON CONFLICT(identity,activity_date) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
            (maintenance_identity(acc), day, json.dumps(data, ensure_ascii=False), int(time.time())))


def record_reward_evidence(acc, records: list) -> float:
    added = 0.0
    identity = maintenance_identity(acc)
    with _write_lock, get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        for record in records:
            old = c.execute("SELECT data FROM reward_evidence WHERE identity=? AND record_id=?",
                            (identity, record["record_id"])).fetchone()
            previous = json.loads(old["data"]) if old else {}
            # 缺字段的新查询不抹去已有的到账证据。
            current = {**record, **previous, **{k: v for k, v in record.items() if v is not None}}
            if previous.get('kind') == 'daily_activity' and record.get('kind') == 'platform_reward':
                current['kind'] = 'daily_activity'
            previously_confirmed = previous.get('amount') is not None and previous.get('credited_at')
            if not previously_confirmed and current.get("amount") is not None and current.get("credited_at"):
                added += current["amount"]
            c.execute("INSERT INTO reward_evidence VALUES(?,?,?) "
                      "ON CONFLICT(identity,record_id) DO UPDATE SET data=excluded.data",
                      (identity, record["record_id"], json.dumps(current, ensure_ascii=False)))
    return added


def list_reward_evidence(acc) -> list:
    return [json.loads(r["data"]) for r in get_conn().execute(
        "SELECT data FROM reward_evidence WHERE identity=? ORDER BY record_id",
        (maintenance_identity(acc),))]


def save_cooldown(acc, service: str, model: str, data: dict) -> None:
    with _write_lock:
        get_conn().execute(
            "INSERT INTO account_cooldowns VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(identity,service,model) DO UPDATE SET "
            "retry_at=MAX(account_cooldowns.retry_at,excluded.retry_at), "
            "updated_at=excluded.updated_at,data=excluded.data "
            "WHERE excluded.updated_at >= account_cooldowns.updated_at",
            (maintenance_identity(acc), service, model, data['retry_at'], data['updated_at'], json.dumps(data)))


def active_cooldowns(acc, model=None) -> list:
    rows = get_conn().execute(
        "SELECT * FROM account_cooldowns WHERE identity=? AND service IN (?,?) AND retry_at>?",
        (maintenance_identity(acc), acc['platform'], 'intl' if acc['platform'] in INTERNATIONAL_CHANNELS else acc['platform'], time.time()))
    return [{**json.loads(r['data']), 'retry_at': r['retry_at'], 'service': r['service'], 'model': r['model']}
            for r in rows if model is None or not r['model'] or r['model'] == model]
