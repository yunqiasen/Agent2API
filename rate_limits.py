"""生成请求的结构化失败分类和冷却；维护接口不经过本模块。"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import datetime as dt
import email.utils
import re
import math
import time

import credentials
import db


@dataclass
class Failure:
    kind: str
    status: int
    message: str
    http_status: int = 0
    code: str = ''
    retry_at: int = 0
    details: dict = field(default_factory=dict)

    @property
    def retryable(self):
        return self.kind != 'parameter'


def error_details(payload):
    """只认错误结构，不扫描模型回答文本里的数字或限流字样。"""
    if not isinstance(payload, dict):
        return {}
    error = payload.get('error')
    if isinstance(error, dict) and error:
        return {**payload, **error}
    if error:
        return {**payload, 'message': str(error)}
    code = payload.get('code')
    if code is not None and str(code) not in ('0', '200') and not payload.get('choices'):
        return payload
    return {}


def classify(acc, response, message, *, route_error=False, payload=None):
    status = int(getattr(response, 'status_code', 502))
    if payload is None:
        payload = getattr(response, '_hy3_error_payload', None)
    if payload is None and (status >= 400 or 'application/json' in (getattr(response, 'headers', {}).get('Content-Type') or '')):
        try:
            payload = response.json()
        except Exception:
            try:
                payload = json.loads(getattr(response, 'text', '')[:65536])
            except (ValueError, TypeError):
                payload = None
    details = error_details(payload)
    code = str(details.get('code') or details.get('type') or '')
    error_type = str(details.get('type') or '').lower()
    trae = acc['platform'] == 'trae'
    quota = status == 402 or code in ('insufficient_quota', 'insufficient_credit') or error_type == 'insufficient_quota'
    if trae and code in ('1005', '4008'):
        quota = True
    limited = status == 429 or code in ('429', 'rate_limit_exceeded', 'too_many_requests') or error_type == 'rate_limit_exceeded'
    if not trae and code.lower() in ('6004', '13212', 'requestlimitexceeded.jobnumexceed'):
        limited = True
    if quota:
        kind, public_status = 'quota', 402
    elif limited:
        kind, public_status = 'rate_limit', 429
    elif status == 401 or (trae and code == '1001'):
        kind, public_status = 'credential', 401
    elif route_error or (not trae and code in ('11102', '11103')):
        kind, public_status = 'route', 503
    elif status == 403:
        kind, public_status = 'permission', 403
    elif 400 <= status < 500 or (trae and code == '4001') or (not trae and code == '11101'):
        kind, public_status = 'parameter', status if status >= 400 else 400
    else:
        kind, public_status = 'upstream', status if status >= 400 else 502
    clean = credentials.redact(message, acc.get('access_token'), acc.get('refresh_token'))
    return Failure(kind, public_status, clean, status, credentials.redact(code, acc.get('access_token'), acc.get('refresh_token'))[:80], details=details)


def _absolute_time(value, default_tz=None):
    epoch = credentials.epoch(value)
    if epoch:
        return epoch
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            if default_tz is None:
                return 0
            parsed = parsed.replace(tzinfo=default_tz)
        return int(parsed.timestamp())
    except (ValueError, TypeError, OverflowError):
        return 0


def retry_at(response, failure, platform):
    now = int(time.time())
    headers = {k.lower(): v for k, v in getattr(response, 'headers', {}).items()}
    after = headers.get('retry-after', '')
    try:
        seconds = float(after)
        if math.isfinite(seconds) and seconds > 0:
            return now + math.ceil(seconds)
    except (ValueError, TypeError, OverflowError):
        pass
    try:
        stamp = email.utils.parsedate_to_datetime(str(after)).timestamp()
        if stamp > now:
            return math.ceil(stamp)
    except (ValueError, TypeError, OverflowError):
        pass
    info = failure.details
    detail_data = info.get('data') if isinstance(info.get('data'), dict) else {}
    default_tz = dt.timezone(dt.timedelta(hours=8)) if platform != 'trae' else None
    for raw in [headers.get('x-ratelimit-reset'), headers.get('x-rate-limit-reset'),
                *(source.get(k) for source in (info, detail_data) for k in ('reset_at', 'resetAt', 'retry_at'))]:
        stamp = _absolute_time(raw, default_tz)
        if stamp > now:
            return stamp
    message = str(info.get('message') or info.get('msg') or failure.message)
    match = re.search(
        r'reset\s+at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})'
        r'(?:\s*(UTC(?:[+-]\d{1,2}(?::\d{2})?)?|Z|[+-]\d{2}:\d{2}))?', message, re.I)
    if match:
        zone = (match[2] or '').upper()
        tz, valid_zone = default_tz, True
        if zone in ('UTC', 'Z'):
            tz = dt.timezone.utc
        elif zone:
            offset = zone.removeprefix('UTC')
            hours, _, minutes = offset[1:].partition(':')
            hours, minutes = int(hours), int(minutes or 0)
            valid_zone = hours < 24 and minutes < 60
            if valid_zone:
                tz = dt.timezone(dt.timedelta(minutes=(hours * 60 + minutes) * (-1 if offset[0] == '-' else 1)))
        stamp = _absolute_time(match[1], tz) if valid_zone else 0
        if stamp > now:
            return stamp
    return now + int(db.get_setting('rate_limit_default_seconds', '60'))


def mark(acc, model, response, message, failure=None):
    failure = failure or classify(acc, response, message)
    if failure.kind != 'rate_limit':
        return
    failure.retry_at = retry_at(response, failure, acc['platform'])
    info = failure.details
    scope = info.get('scope')
    scope_kind = scope.get('type') if isinstance(scope, dict) else scope
    scope_service = scope.get('service') if isinstance(scope, dict) else info.get('service')
    shared = scope_kind == 'shared_service' or (scope_kind == 'service' and scope_service in ('international', 'codebuddy_global'))
    service = 'intl' if shared and acc['platform'] in db.INTERNATIONAL_CHANNELS else acc['platform']
    scoped_model = model if scope_kind == 'model' else ''
    db.save_cooldown(acc, service, scoped_model, {
        'retry_at': failure.retry_at, 'kind': 'rate_limit', 'message': failure.message,
        'http_status': failure.http_status, 'code': failure.code, 'updated_at': time.time(),
    })
