"""国际活动与到账证据。维护入口共用，不发起模型生成。"""
from __future__ import annotations

import datetime as dt
import math
import threading
import uuid
import logging
from zoneinfo import ZoneInfo

import credentials
import db
import providers as P

TZ = ZoneInfo('Asia/Shanghai')
_locks = {}
_guard = threading.Lock()


def now():
    return dt.datetime.now(TZ)


def today():
    return now().date().isoformat()


def _lock(acc):
    with _guard:
        return _locks.setdefault((db.DB_PATH, db.maintenance_identity(acc)), threading.RLock())


def view(acc):
    return db.get_daily_reward_state(acc, today()) or {
        'activity_date': today(), 'activity_status': 'unknown',
        'reward_status': 'unknown', 'amount': None, 'rewards': db.list_reward_evidence(acc),
        'last_success_at': 0, 'error': '', 'error_stage': '', 'timezone': 'Asia/Shanghai',
    }


def _activity(acc):
    data = P.growth_request(acc, '/activity/growth/heatmap')
    if data.get('eligible') is False or data.get('active') is False:
        return {'activity_date': today(), 'activity_status': 'unavailable',
                'reason': credentials.redact(data.get('reason') or data.get('message') or '活动不适用',
                                             acc.get('access_token'), acc.get('refresh_token'))}
    day = data.get('today')
    if day != today():
        raise P.ProviderError('活动日期缺失或陈旧，待核验')
    cells = [c for c in data.get('cells', []) if isinstance(c, dict) and c.get('date') == day]
    if len(cells) != 1:
        raise P.ProviderError('当日活动记录缺失或重复，待核验')
    try:
        score = float(cells[0]['score'])
        if not math.isfinite(score) or score < 0:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise P.ProviderError('当日活动分数未公布，待核验')
    return {'activity_date': day, 'activity_status': 'confirmed' if score > 0 else 'not_completed',
            'score': score}


def _submit(acc):
    # 字段协议参考 codebuddy-intl-cpa dailybonus.go；不是模型生成接口。
    request_id = 'wb-' + str(uuid.uuid4())
    stamp = int(now().timestamp() * 1000)
    P.growth_request(acc, '/v2/report', method='POST', payload=[{
        'eventCode': 'chat_request_send', 'timestamp': stamp, 'mode': 'craft',
        'conversationId': request_id, 'requestId': request_id, 'inputLength': 12,
        'requestModelId': 'deepseek-v4-flash', 'requestModelName': 'DeepSeek V4 Flash',
        'mentionContexts': [], 'knowledgeId': [], 'knowledgeName': [], 'presentAt': stamp,
        'rootRequestId': request_id, 'parentConversationId': request_id,
        'agentName': 'default', 'agentType': 'conversation', 'userId': acc.get('uid', ''),
    }])


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError):
        return None


def _timestamp(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)).timestamp())
    except (ValueError, TypeError):
        return credentials.epoch(value) or None


def _reward_record(package):
    if not isinstance(package, dict):
        return None
    kind = str(package.get('RewardType') or package.get('reward_type') or '')
    daily = kind in ('daily_activity', 'daily_bonus')
    if not daily and package.get('PackageCode') != 'TCACA_code_007_nzdH5h4Nl0':
        return None
    record_id = next((package[k] for k in ('AccountId', 'RecordId', 'ResourceId', 'Id') if package.get(k)), None)
    if record_id is None:
        return None  # 产品代码不是发放记录ID；证据不足时不猜新奖励。
    raw = next((package[k] for k in ('CapacitySizePrecise', 'CapacitySize') if package.get(k) is not None), None)
    try:
        amount = float(raw)
        if not math.isfinite(amount) or amount < 0:
            amount = None
    except (ValueError, TypeError):
        amount = None
    return {'record_id': str(record_id), 'kind': 'daily_activity' if daily else 'platform_reward',
            'activity_date': _date(package.get('ActivityDate') or package.get('RewardDate')),
            'amount': amount, 'credited_at': _timestamp(package.get('CreateTime') or package.get('GrantTime')),
            'package_code': str(package.get('PackageCode') or '')}


def _rewards(acc, state):
    records = [r for p in P.fetch_growth_packages(acc) if (r := _reward_record(p))]
    state['new_credit'] = db.record_reward_evidence(acc, records)
    state['rewards'] = db.list_reward_evidence(acc)
    matching = [r for r in state['rewards'] if r.get('kind') == 'daily_activity'
                and r.get('activity_date') == state['activity_date']]
    complete = matching and all(r.get('amount') is not None and r.get('credited_at') for r in matching)
    state['amount'] = sum(r['amount'] for r in matching) if complete else None
    state['reward_status'] = ('credited' if complete else 'pending'
                              if state['activity_status'] == 'confirmed' else 'not_eligible'
                              if state['activity_status'] == 'unavailable' else 'unknown')
    state['reward_checked_at'] = int(now().timestamp())


def run(acc, submit=False):
    with _lock(acc):
        state = view(acc)
        state['new_credit'] = 0
        stage, skipped = 'status', False
        try:
            activity = _activity(acc)
            state.update(activity, last_success_at=int(now().timestamp()), error='', error_stage='')
            skipped = activity['activity_status'] == 'confirmed'
            if submit and activity['activity_status'] == 'not_completed' and not state.get('submitted_at'):
                stage = 'submit'
                state.update(activity_status='submitted', submitted_at=int(now().timestamp()))
                # 在发送前持久化，进程中断后仍先核验，避免重放不确定的提交。
                db.save_daily_reward_state(acc, state['activity_date'], state)
                _submit(acc)
                stage = 'readback'
                state.update(_activity(acc), last_success_at=int(now().timestamp()))
            if state['activity_status'] == 'not_completed' and state.get('submitted_at'):
                state['activity_status'] = 'submitted'
            if state['activity_status'] == 'confirmed':
                state['confirmed_at'] = int(now().timestamp())
                if state['reward_status'] != 'credited':
                    state['reward_status'] = 'pending'
            stage = 'rewards'
            _rewards(acc, state)
        except Exception as e:
            if stage == 'submit' and isinstance(e, P.ActivitySubmissionRejected):
                state.pop('submitted_at', None)
                state['activity_status'] = 'not_completed'
            state.update(error=credentials.redact(e, acc.get('access_token'), acc.get('refresh_token')),
                         error_stage=stage)
            if state.get('submitted_at') and state['activity_status'] != 'confirmed':
                state['activity_status'] = 'submitted'
        state['updated_at'] = int(now().timestamp())
        db.save_daily_reward_state(acc, state['activity_date'], state)
        logging.getLogger('agent2api.checkin').warning(
            'daily_reward account_id=%s platform=%s date=%s activity=%s reward=%s stage=%s error=%s',
            acc['id'], acc['platform'], state['activity_date'], state['activity_status'],
            state['reward_status'], state['error_stage'], state['error'])
        return {'ok': not bool(state['error']), 'checkin_kind': 'international_activity',
                'daily_reward': state, 'skipped': skipped, 'error': state['error'],
                'supported': True, 'today_checked_in': state['activity_status'] == 'confirmed',
                'claimed_credit': None}
