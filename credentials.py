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


def domain_host(value) -> str:
    """凭证域名只是路由提示，统一主机名格式，不用它证明账号身份。"""
    from urllib.parse import urlsplit
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        return (urlsplit(text if '://' in text else '//' + text).hostname or '').lower().rstrip('.')
    except ValueError:
        return ''


def import_item(item: dict) -> dict:
    """正规化明文凭证，保留所选渠道之外的账号/会话资料。"""
    auth = item.get('auth', item)
    account = item.get('account', item)
    if not isinstance(auth, dict) or not isinstance(account, dict):
        raise ValueError('auth 和 account 必须是对象')
    out = dict(item)
    for target, aliases, sources in (
        ('access_token', ('access_token', 'accessToken'), (auth,)),
        ('refresh_token', ('refresh_token', 'refreshToken'), (auth,)),
        ('expires_at', ('expires_at', 'expiresAt'), (auth,)),
        ('domain', ('domain',), (auth, item)),
        ('machine_id', ('machine_id', 'machineId'), (auth, item)),
        ('device_id', ('device_id', 'deviceId'), (auth, item)),
        ('uid', ('uid',), (account, item)),
        ('nickname', ('nickname',), (account, item)),
        ('email', ('email',), (account, item)),
        ('enterprise_id', ('enterprise_id', 'enterpriseId'), (account, item)),
        ('enterprise_name', ('enterprise_name', 'enterpriseName'), (account, item)),
    ):
        for source in sources:
            match = next((source[a] for a in aliases if a in source and source[a] is not None), None)
            if match is not None:
                out[target] = match
                break
    if out.get('domain'):
        out['domain'] = domain_host(out['domain'])
    return out


def check_region(platform: str, domain: str, access_token: str) -> None:
    """仅拦截显式矛盾的区域提示；JWT提示不作为鉴权依据。"""
    def region(value):
        host = domain_host(value)
        if any(host == x or host.endswith('.' + x) for x in ('codebuddy.ai', 'workbuddy.ai')):
            return 'intl'
        if host == 'copilot.tencent.com' or host == 'codebuddy.cn' or host.endswith('.codebuddy.cn'):
            return 'cn'
        return ''
    expected = 'intl' if platform in ('codebuddy', 'workbuddy_intl') else 'cn' if platform == 'workbuddy' else ''
    if expected and any(r and r != expected for r in (region(domain), region(claims(access_token).get('iss')))):
        raise ValueError('凭证区域与所选客户端不一致，请检查登录来源')
