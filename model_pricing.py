"""Channel/account price snapshots from official catalogs; never used for billing or routing."""
from __future__ import annotations

from decimal import Decimal
import json
import re
import threading
import time

import db

_SETTING = 'model_pricing_v1'
_lock = threading.RLock()


def _load():
    try:
        data = json.loads(db.get_setting(_SETTING, '{}'))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _multiplier(raw):
    if not isinstance(raw, str) or len(raw) > 100:
        return None
    match = re.fullmatch(r'[x×]\s*(\d+(?:\.\d+)?)(?:\s+credits)?', raw.strip(), re.I)
    if not match:
        return None
    value = Decimal(match[1])
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def record_success(platform, account_id, ids, metadata):
    prices = metadata.get('credits') or {}
    with _lock:
        data = _load()
        data.setdefault(platform, {})[str(account_id)] = {
            'source': metadata.get('source', ''), 'updated_at': int(time.time()), 'failed': False,
            'models': {mid: prices.get(mid) if isinstance(prices.get(mid), str) else None for mid in ids},
        }
        db.set_setting(_SETTING, json.dumps(data, ensure_ascii=False))


def record_failure(platform, account_id):
    # The error text belongs to catalog diagnostics, not the price snapshot.
    with _lock:
        data = _load()
        data.setdefault(platform, {}).setdefault(str(account_id), {})['failed'] = True
        db.set_setting(_SETTING, json.dumps(data, ensure_ascii=False))


def for_platform(platform, ids):
    records = _load().get(platform, {})
    active = [str(r['id']) for r in db.list_accounts(platform)
              if r['enabled'] and r['status'] not in ('banned', 'disabled')]
    if active:
        records = {aid: records.get(aid, {'failed': True}) for aid in active}
    stale = not active or any(rec.get('failed') for rec in records.values())
    out = {}
    for mid in ids:
        samples = [{'account_id': int(aid), 'raw': rec['models'][mid],
                    'multiplier': _multiplier(rec['models'][mid]),
                    'source': rec.get('source', ''), 'updated_at': rec.get('updated_at', 0),
                    'stale': bool(rec.get('failed')) or not active}
                   for aid, rec in records.items() if mid in rec.get('models', {})]
        values = {s['multiplier'] for s in samples}
        conflict = len(values) > 1
        value = next(iter(values)) if len(values) == 1 else None
        out[mid] = {
            'status': 'unpublished' if value is None else ('free' if Decimal(value) == 0 else 'multiplier'),
            'multiplier': value,
            'label': '未公布' if value is None else ('免费' if Decimal(value) == 0 else '×' + value),
            'source': samples[-1]['source'] if samples else '',
            'updated_at': max((s['updated_at'] for s in samples), default=0),
            'stale': stale, 'conflict': conflict,
            'note': '账号返回不同倍率' if conflict else ('无可用账号，保留上次数据' if not active else
                    ('目录同步失败或尚未同步，保留上次数据' if stale else '')),
            'observations': samples,
        }
    return out
