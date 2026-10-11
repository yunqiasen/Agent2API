"""凭证的时间提示与错误脱敏；JWT 解码不用于身份或权限验证。"""
from __future__ import annotations

import base64
import json
import math
import re
import time


def epoch(value) -> int:
    try:
        n = float(value or 0)
        if not math.isfinite(n) or n <= 0:
            return 0
        return int(n / 1000 if n > 10_000_000_000 else n)
    except (TypeError, ValueError, OverflowError):
        return 0


def claims(token) -> dict:
    try:
        segment = str(token).split('.')[1]
        if len(segment) > 65536:
            return {}
        data = json.loads(base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError, IndexError, UnicodeError):
        return {}


def expiry(token, explicit=0, duration=0) -> int:
    values = [epoch(explicit), epoch(claims(token).get('exp'))]
    if not any(values):
        try:
            seconds = float(duration or 0)
            if math.isfinite(seconds) and seconds > 0:
                values.append(int(time.time() + seconds))
        except (TypeError, ValueError, OverflowError):
            pass
    return min((v for v in values if v > 0), default=0)


def redact(message, *tokens) -> str:
    text = str(message).replace('\n', ' ')
    for token in sorted((str(t) for t in tokens if t), key=len, reverse=True):
        text = text.replace(token, '[REDACTED]')
    text = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED]', text)
    return text[:400]
