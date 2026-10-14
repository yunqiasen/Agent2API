"""国际奖励的管理HTTP契约；只替代上游网络，不操作正式账号。"""
import datetime as dt
import json
import time
from urllib.parse import urlsplit

import pytest
import requests

from test_credentials_refresh import client, jwt


class RewardUpstream:
    def __init__(self):
        self.today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
        self.scores = {}
        self.calls = []
        self.reported = []
        self.confirm = True
        self.status_error = None
        self.report_error = None
        self.pages = {1: []}
        self.total = 0

    def request(self, session, method, url, **kw):
        path = urlsplit(url).path
        uid = kw.get('headers', {}).get('X-User-Id', '')
        self.calls.append((method, url, kw))
        code = 200
        if path == '/activity/growth/heatmap':
            if self.status_error:
                code, body = self.status_error
            else:
                # The last array entry is deliberately stale.
                body = {'code': 0, 'data': {'today': self.today, 'cells': [
                    {'date': self.today, 'score': self.scores.get(uid, 0)},
                    {'date': '2026-01-01', 'score': 5}]}}
        elif path == '/v2/report':
            self.reported.append(uid)
            if self.report_error:
                raise self.report_error
            if self.confirm:
                self.scores[uid] = 2
            body = {'code': 0}
        elif path.endswith('/get-user-resource'):
            page = kw['json']['PageNumber']
            body = {'code': 0, 'data': {'Response': {'Data': {
                'Accounts': self.pages.get(page, []), 'TotalCount': self.total}}}}
        else:
            raise AssertionError(f'unexpected upstream request: {method} {path}')
        r = requests.Response()
        r.status_code = code
        r._content = json.dumps(body).encode()
        return r


@pytest.fixture
def upstream(monkeypatch):
    u = RewardUpstream()
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda s, m, url, **kw: u.request(s, m, url, **kw))
    return u


def add_account(db, platform='workbuddy_intl', uid='one', enabled=True):
    aid = db.add_account(platform, jwt(int(time.time()) + 7200), uid=uid)
    db.update_account(aid, enabled=int(enabled))
    return aid


def state(client, aid):
    return next(a for a in client.get('/api/accounts').get_json() if a['id'] == aid)['daily_reward']


@pytest.mark.parametrize('platform', ['codebuddy', 'workbuddy_intl'])
def test_status_reads_actual_today_without_submitting_activity(client, isolated_db, upstream, platform):
    aid = add_account(isolated_db, platform)
    r = client.get(f'/api/accounts/{aid}/checkin-status')
    assert r.status_code == 200
    assert r.get_json()['checkin_kind'] == 'international_activity'
    current = state(client, aid)
    assert current['activity_date'] == upstream.today
    assert current['activity_status'] == 'not_completed'
    assert current['reward_status'] == 'unknown'
    assert upstream.reported == []
    assert {urlsplit(c[1]).netloc for c in upstream.calls} == {f'www.{"codebuddy" if platform == "codebuddy" else "workbuddy"}.ai'}


def test_daily_activity_confirmed_is_pending_not_a_credit_claim(client, isolated_db, upstream):
    aid = add_account(isolated_db)
    r = client.post(f'/api/accounts/{aid}/checkin', json={})
    assert r.status_code == 200
    result = r.get_json()
    assert result['ok'] is True
    assert result['daily_reward']['activity_status'] == 'confirmed'
    assert result['daily_reward']['reward_status'] == 'pending'
    assert result.get('claimed_credit') is None
    assert upstream.reported == ['one']
    result = client.post(f'/api/accounts/{aid}/checkin', json={}).get_json()
    assert result['skipped'] is True
    assert upstream.reported == ['one']
    paths = [urlsplit(c[1]).path for c in upstream.calls]
    assert paths[:3] == ['/activity/growth/heatmap', '/v2/report', '/activity/growth/heatmap']
    event = next(c[2]['json'][0] for c in upstream.calls if urlsplit(c[1]).path == '/v2/report')
    assert event['eventCode'] == 'chat_request_send'
    assert event['userId'] == 'one'


def test_reward_evidence_is_paged_deduplicated_and_separate_from_activity_date(client, isolated_db, upstream):
    aid = add_account(isolated_db)
    yesterday = (dt.date.fromisoformat(upstream.today) - dt.timedelta(days=1)).isoformat()
    upstream.pages = {
        1: [{'AccountId': 'purchase', 'PackageCode': 'paid', 'CapacitySizePrecise': 600,
             'CreateTime': upstream.today + 'T01:00:00+08:00'}],
        2: [{'AccountId': 'reward-1', 'PackageCode': 'TCACA_code_007_nzdH5h4Nl0',
             'RewardType': 'daily_activity', 'ActivityDate': yesterday,
             'CapacitySizePrecise': 17.5, 'CapacityRemainPrecise': 0, 'Status': 3,
             'CreateTime': upstream.today + 'T02:20:00+08:00'}],
    }
    upstream.total = 2
    r = client.get(f'/api/accounts/{aid}/checkin-status').get_json()
    assert r['daily_reward']['activity_status'] == 'not_completed'
    rewards = r['daily_reward']['rewards']
    assert [(r['record_id'], r['amount'], r['activity_date']) for r in rewards] == [('reward-1', 17.5, yesterday)]
    assert r['daily_reward']['new_credit'] == 17.5
    assert r['daily_reward']['reward_status'] != 'credited'
    assert client.get(f'/api/accounts/{aid}/checkin-status').get_json()['daily_reward']['new_credit'] == 0
    queries = [c[2]['json'] for c in upstream.calls if urlsplit(c[1]).path.endswith('/get-user-resource')]
    assert [q['PageNumber'] for q in queries] == [1, 2, 1, 2]
    assert queries[0]['Status'] == []
    assert queries[0]['PackageEndTimeRangeBegin'] < upstream.today
    isolated_db.init_db()
    assert state(client, aid)['rewards'] == rewards


@pytest.mark.parametrize('failure', ['delayed', 'transport', 'readback'])
def test_unconfirmed_submission_survives_restart_without_blind_resend(client, isolated_db, upstream, monkeypatch, failure):
    aid = add_account(isolated_db)
    upstream.confirm = False
    if failure == 'transport':
        upstream.report_error = requests.Timeout('transmission uncertain')
    if failure == 'readback':
        original = upstream.request
        def request(s, m, url, **kw):
            if upstream.reported and url.endswith('/heatmap'):
                upstream.status_error = (503, {'code': 503, 'msg': 'later'})
            return original(s, m, url, **kw)
        monkeypatch.setattr(requests.sessions.Session, 'request', request)
    client.post(f'/api/accounts/{aid}/checkin', json={})
    assert state(client, aid)['activity_status'] == 'submitted'
    assert state(client, aid)['reward_status'] != 'credited'
    isolated_db.init_db()
    client.post(f'/api/accounts/{aid}/checkin', json={})
    assert upstream.reported == ['one']


def test_status_failure_preserves_old_evidence_and_redacts_token(client, isolated_db, upstream):
    aid = add_account(isolated_db)
    client.post(f'/api/accounts/{aid}/checkin', json={})
    before = state(client, aid)
    token = isolated_db.get_account(aid)['access_token']
    upstream.status_error = (503, {'code': 503, 'msg': token})
    result = client.post(f'/api/accounts/{aid}/checkin', json={}).get_json()
    assert result['ok'] is False
    after = state(client, aid)
    assert after['activity_status'] == 'confirmed'
    assert after['last_success_at'] == before['last_success_at']
    assert after['error_stage'] == 'status'
    assert token not in json.dumps(result)
    assert len(upstream.reported) == 1


@pytest.mark.parametrize('day', [None, '2026-01-01', 'invalid'])
def test_missing_or_stale_server_date_is_not_activity_proof(client, isolated_db, upstream, day):
    aid = add_account(isolated_db)
    upstream.today = day
    result = client.post(f'/api/accounts/{aid}/checkin', json={}).get_json()
    assert result['ok'] is False
    assert result['daily_reward']['activity_status'] == 'unknown'
    assert upstream.reported == []


def test_unknown_reward_amount_is_not_zero_or_fixed_thirty(client, isolated_db, upstream):
    aid = add_account(isolated_db)
    upstream.scores['one'] = 2
    upstream.total = 2
    upstream.pages[1] = [
        {'AccountId': 'unknown', 'RewardType': 'daily_activity', 'ActivityDate': upstream.today,
         'CreateTime': upstream.today + 'T01:00:00+08:00'},
        {'AccountId': 'generic', 'PackageCode': 'TCACA_code_007_nzdH5h4Nl0', 'CapacitySize': 50,
         'CreateTime': upstream.today + 'T02:00:00+08:00'},
    ]
    result = client.post(f'/api/accounts/{aid}/checkin', json={}).get_json()['daily_reward']
    assert result['amount'] is None
    assert result['reward_status'] == 'pending'
    assert next(r for r in result['rewards'] if r['record_id'] == 'unknown')['amount'] is None
    assert next(r for r in result['rewards'] if r['record_id'] == 'generic')['activity_date'] is None


def test_filtered_batch_reports_failures_and_shared_identity_once(client, isolated_db, upstream, monkeypatch):
    first = add_account(isolated_db, 'codebuddy', 'shared', enabled=False)
    second = add_account(isolated_db, 'workbuddy_intl', 'shared')
    third = add_account(isolated_db, 'workbuddy_intl', 'bad')
    last = add_account(isolated_db, 'workbuddy_intl', 'last')
    original = upstream.request
    def request(s, m, url, **kw):
        if kw.get('headers', {}).get('X-User-Id') == 'bad':
            raise requests.Timeout('account query failed')
        return original(s, m, url, **kw)
    monkeypatch.setattr(requests.sessions.Session, 'request', request)
    result = client.post('/api/accounts/checkin_all', json={}).get_json()
    assert [r['id'] for r in result['results']] == [first, second, third, last]
    assert [r['ok'] for r in result['results']] == [True, True, False, True]
    assert result['summary']['pending_reward'] == 2
    assert result['summary']['failed'] == 1
    assert upstream.reported == ['shared', 'last']
    result = client.post('/api/accounts/checkin_all', json={'platform': 'codebuddy'}).get_json()
    assert [r['id'] for r in result['results']] == [first]
    assert state(client, first)['activity_status'] == state(client, second)['activity_status'] == 'confirmed'


def test_ineligible_activity_is_not_submitted_or_marked_claimed(client, isolated_db, upstream, monkeypatch):
    aid = add_account(isolated_db)
    original = upstream.request
    def request(s, m, url, **kw):
        r = original(s, m, url, **kw)
        if url.endswith('/heatmap'):
            body = r.json()
            body['data'].update(eligible=False, reason='campaign ended')
            r._content = json.dumps(body).encode()
        return r
    monkeypatch.setattr(requests.sessions.Session, 'request', request)
    result = client.post(f'/api/accounts/{aid}/checkin', json={}).get_json()
    assert result['daily_reward']['activity_status'] == 'unavailable'
    assert result['daily_reward']['reason'] == 'campaign ended'
    assert upstream.reported == []


def test_automatic_retry_respects_schedule_and_disabled_accounts(client, isolated_db, upstream, monkeypatch):
    import proxy
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 14, 10, 0, tzinfo=tz)
    monkeypatch.setattr(proxy._dt, 'datetime', Clock)
    upstream.today = '2026-10-14'
    aid = add_account(isolated_db, enabled=False)
    isolated_db.set_setting('checkin_time', '23:00')
    proxy.retry_failed_checkins()
    assert upstream.reported == []
    isolated_db.set_setting('checkin_time', '00:00')
    proxy.run_auto_checkin_if_due()
    assert upstream.reported == ['one']
    add_account(isolated_db, uid='new-after-batch')
    proxy.retry_failed_checkins()
    assert upstream.reported == ['one', 'new-after-batch']
    assert state(client, aid)['activity_status'] == 'confirmed'
    isolated_db.set_setting('auto_checkin', '0')
    add_account(isolated_db, uid='auto-off')
    isolated_db.set_setting('_last_checkin_retry_ts', '0')
    proxy.retry_failed_checkins()
    assert 'auto-off' not in upstream.reported


def test_concurrent_clients_submit_once_and_keep_old_day_reward_separate(client, isolated_db, upstream, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import app
    import daily_rewards
    ids = [add_account(isolated_db, p, 'shared') for p in ('codebuddy', 'workbuddy_intl')]
    def execute(aid):
        with app.app.test_client() as c:
            with c.session_transaction() as session:
                session['admin'] = True
            return c.post(f'/api/accounts/{aid}/checkin', json={}).get_json()
    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(execute, ids))
    assert all(r['ok'] for r in result)
    assert upstream.reported == ['shared']
    old = upstream.today
    next_day = dt.datetime.combine(dt.date.fromisoformat(old) + dt.timedelta(days=1), dt.time(0, 1), tzinfo=daily_rewards.TZ)
    monkeypatch.setattr(daily_rewards, 'now', lambda: next_day)
    upstream.today = next_day.date().isoformat()
    upstream.scores = {}
    # No local "confirmed yesterday" marker can complete today's activity.
    result = client.get(f'/api/accounts/{ids[0]}/checkin-status').get_json()
    assert result['daily_reward']['activity_status'] == 'not_completed'
    assert upstream.reported == ['shared']


def test_reward_query_failure_preserves_confirmed_credit_evidence(client, isolated_db, upstream, monkeypatch):
    aid = add_account(isolated_db)
    upstream.scores['one'] = 1
    upstream.total = 1
    upstream.pages[1] = [{'AccountId': 'earned', 'RewardType': 'daily_activity',
        'ActivityDate': upstream.today, 'CapacitySizePrecise': 12,
        'CreateTime': upstream.today + 'T03:00:00+08:00'}]
    client.get(f'/api/accounts/{aid}/checkin-status')
    assert state(client, aid)['reward_status'] == 'credited'
    original = upstream.request
    def request(s, m, url, **kw):
        if url.endswith('/get-user-resource'):
            raise requests.Timeout('ledger temporarily unavailable')
        return original(s, m, url, **kw)
    monkeypatch.setattr(requests.sessions.Session, 'request', request)
    result = client.get(f'/api/accounts/{aid}/checkin-status').get_json()['daily_reward']
    assert result['reward_status'] == 'credited'
    assert result['amount'] == 12
    assert result['error_stage'] == 'rewards'
    assert result['new_credit'] == 0


def test_reward_partial_record_can_be_confirmed_once_without_losing_old_attribution(client, isolated_db, upstream):
    aid = add_account(isolated_db)
    package = {'AccountId': 'delayed-fields', 'PackageCode': 'TCACA_code_007_nzdH5h4Nl0',
               'RewardType': 'daily_activity', 'ActivityDate': upstream.today, 'CapacitySize': 9}
    upstream.total = 1
    upstream.pages[1] = [package]
    assert client.get(f'/api/accounts/{aid}/checkin-status').get_json()['daily_reward']['new_credit'] == 0
    package['CreateTime'] = upstream.today + 'T03:00:00+08:00'
    second = client.get(f'/api/accounts/{aid}/checkin-status').get_json()['daily_reward']
    assert second['new_credit'] == 9
    del package['RewardType']
    third = client.get(f'/api/accounts/{aid}/checkin-status').get_json()['daily_reward']
    assert third['reward_status'] == 'credited'
    assert third['new_credit'] == 0


def test_maintenance_contract_publishes_kinds_and_compatible_completion_fields(client, isolated_db, upstream):
    aid = add_account(isolated_db)
    platforms = {p['key']: p for p in client.get('/api/platforms').get_json()}
    assert platforms['workbuddy'].get('checkin_kind') == 'standard'
    assert platforms['trae'].get('checkin_kind') == 'standard'
    for key in ('codebuddy', 'workbuddy_intl'):
        assert platforms[key].get('checkin_kind') == 'international_activity'
        assert platforms[key].get('supports_checkin') is True
    assert state(client, aid)['activity_status'] == 'unknown'
    queried = client.get(f'/api/accounts/{aid}/checkin-status').get_json()
    assert queried['daily_reward']['activity_status'] == 'not_completed'
    assert queried['daily_reward']['reward_status'] == 'unknown'
    assert queried['today_checked_in'] is False
    assert 'claimed_credit' in queried and queried['claimed_credit'] is None
    completed = client.post(f'/api/accounts/{aid}/checkin', json={}).get_json()
    assert completed['today_checked_in'] is True
    assert completed['claimed_credit'] is None
    assert completed['daily_reward']['reward_status'] == 'pending'


def test_explicitly_rejected_activity_can_retry_after_querying_state(client, isolated_db, upstream, monkeypatch):
    aid = add_account(isolated_db)
    rejected = []
    original = upstream.request
    def request(s, method, url, **kw):
        if url.endswith('/v2/report') and not rejected:
            rejected.append(True)
            response = requests.Response()
            response.status_code = 429
            response._content = b'{"code":6004,"msg":"activity request rate limited"}'
            return response
        return original(s, method, url, **kw)
    monkeypatch.setattr(requests.sessions.Session, 'request', request)
    first = client.post(f'/api/accounts/{aid}/checkin', json={}).get_json()
    assert first['ok'] is False
    assert first['daily_reward']['activity_status'] == 'not_completed'
    assert first['daily_reward']['error_stage'] == 'submit'
    second = client.post(f'/api/accounts/{aid}/checkin', json={}).get_json()
    assert second['daily_reward']['activity_status'] == 'confirmed'
    assert upstream.reported == ['one']


def test_batch_counts_reward_that_arrives_on_second_shared_card(client, isolated_db, upstream, monkeypatch):
    add_account(isolated_db, 'codebuddy', 'shared')
    add_account(isolated_db, 'workbuddy_intl', 'shared')
    queries = []
    original = upstream.request
    def request(s, method, url, **kw):
        if url.endswith('/get-user-resource'):
            queries.append(url)
            if len(queries) == 2:
                upstream.total = 1
                upstream.pages[1] = [{'AccountId': 'arriving-reward', 'RewardType': 'daily_activity',
                    'ActivityDate': upstream.today, 'CapacitySize': 7,
                    'CreateTime': upstream.today + 'T03:00:00+08:00'}]
        return original(s, method, url, **kw)
    monkeypatch.setattr(requests.sessions.Session, 'request', request)
    result = client.post('/api/accounts/checkin_all', json={}).get_json()
    assert len(result['results']) == 2
    assert result['summary']['new_credit'] == 7
    assert result['summary']['credited'] == 1
    assert result['summary']['pending_reward'] == 0
    assert client.post('/api/accounts/checkin_all', json={}).get_json()['summary']['new_credit'] == 0
