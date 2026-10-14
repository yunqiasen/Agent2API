"""四渠道限流，走真实公共HTTP/选号/持久化边界，网络由可控上游替代。"""
import copy
import json
import time
from urllib.parse import urlsplit

import pytest
import requests

from test_credentials_refresh import client, jwt


class GenerationUpstream:
    def __init__(self, db):
        self.db, self.calls, self.behavior = db, [], {}

    def request(self, session, method, url, **kw):
        headers = kw.get('headers', {})
        auth = headers.get('Authorization', '')
        acc = next((dict(a) for a in self.db.list_accounts() if a['access_token'] in auth), None)
        assert acc is not None, 'unknown fixture credential'
        self.calls.append((acc['id'], url, copy.deepcopy(kw)))
        outcome = self.behavior.get(acc['id'])
        if callable(outcome):
            return outcome(acc, url, kw)
        r = requests.Response()
        if outcome:
            r.status_code, body, hdrs = outcome
            r._content = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            r.headers.update({'Content-Type': 'application/json', **hdrs})
        else:
            r.status_code = 200
            r.headers['Content-Type'] = 'text/event-stream'
            if 'images' in url:
                r.headers['Content-Type'] = 'application/json'
                r._content = json.dumps({'data': [{'url': 'https://example.test/image.png'}]}).encode()
            elif acc['platform'] == 'trae':
                r._content = b'event: output\ndata: {"response":"pong"}\n\nevent: done\ndata: {"finish_reason":"stop"}\n\n'
            else:
                r._content = b'data: {"choices":[{"index":0,"delta":{"content":"pong"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        r._content_consumed = True
        return r


@pytest.fixture
def upstream(isolated_db, monkeypatch):
    import app
    u = GenerationUpstream(isolated_db)
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda s, method, url, **kw: u.request(s, method, url, **kw))
    monkeypatch.setattr(app, '_enqueue_stats', lambda fn: fn())
    return u


def add_account(db, platform='workbuddy', uid=None):
    uid = uid or f'u{len(db.list_accounts())+1}'
    return db.add_account(platform, jwt(int(time.time()) + 7200, uid=uid, client=platform), uid=uid)


def call(client, db, model='model', path='/v1/chat/completions', stream=False, key=None, **extra):
    data = {'model': model, 'stream': stream, **extra}
    if 'responses' in path:
        data.setdefault('input', 'hi')
    elif 'images' in path:
        data.setdefault('prompt', 'hi')
    else:
        data.setdefault('messages', [{'role': 'user', 'content': 'hi'}])
    return client.post(path, headers={'Authorization': 'Bearer ' + (key or db.DEFAULT_API_KEY)}, json=data)


@pytest.mark.parametrize('platform', ['workbuddy', 'trae', 'codebuddy', 'workbuddy_intl'])
def test_http429_cools_account_across_requests_and_restart(client, isolated_db, upstream, platform):
    db = isolated_db
    first, second = [add_account(db, platform) for _ in range(2)]
    db.set_model_registry({'model': {'type': 'chat', 'platforms': [platform]}})
    upstream.behavior[first] = (429, {'error': {'message': 'busy'}}, {'Retry-After': '120'})
    r = call(client, db)
    assert r.status_code == 200
    assert 'pong' in r.get_data(as_text=True)
    assert [c[0] for c in upstream.calls] == [first, second]
    rows = client.get('/api/accounts').get_json()
    assert rows[0]['cooldowns'][0]['retry_at'] >= int(time.time()) + 115
    assert rows[0]['enabled'] == 1
    db.init_db()
    assert call(client, db).status_code == 200
    assert [c[0] for c in upstream.calls] == [first, second, second]


def test_more_than_old_budget_tries_whole_channel_before_next(client, isolated_db, upstream):
    db = isolated_db
    ids = [add_account(db) for _ in range(11)]
    other = add_account(db, 'codebuddy')
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy', 'codebuddy']}})
    for aid in ids[:-1]:
        upstream.behavior[aid] = (429, {'code': 6004, 'msg': 'busy'}, {})
    assert call(client, db).status_code == 200
    assert [c[0] for c in upstream.calls] == ids
    assert other not in [c[0] for c in upstream.calls]


def test_all_limited_returns_429_and_retry_time_without_waiting(client, isolated_db, upstream, monkeypatch):
    import proxy
    monkeypatch.setattr(proxy.time, 'sleep', lambda n: pytest.fail(f'unexpected wait {n}'))
    db = isolated_db
    ids = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    for aid, seconds in zip(ids, (60, 180)):
        upstream.behavior[aid] = (429, {'error': {'message': 'busy'}}, {'Retry-After': str(seconds)})
    r = call(client, db)
    assert r.status_code == 429
    assert 55 <= int(r.headers['Retry-After']) <= 60
    assert r.get_json()['error']['retry_at'] > time.time()
    assert call(client, db).status_code == 429
    assert [c[0] for c in upstream.calls] == ids
    assert [x['id'] for x in client.get('/v1/models', headers={'Authorization': 'Bearer ' + db.DEFAULT_API_KEY}).get_json()['data']] == ['model']


@pytest.mark.parametrize('platform,body,headers', [
    ('workbuddy', {'code':6004, 'msg':'busy'}, {}),
    ('codebuddy', 'data: {"error":{"code":6004,"message":"busy"}}\n\n', {'Content-Type':'text/event-stream'}),
    ('workbuddy_intl', {'error': {'code': 6004, 'message':'busy'}}, {}),
    ('trae', 'event: error\ndata: {"code":429,"message":"too many requests"}\n\n', {'Content-Type':'text/event-stream'}),
])
def test_structured_200_limit_is_cooled_before_failover(client, isolated_db, upstream, platform, body, headers):
    db = isolated_db
    ids = [add_account(db, platform) for _ in range(2)]
    db.set_model_registry({'model': {'type': 'chat', 'platforms': [platform]}})
    upstream.behavior[ids[0]] = (200, body, headers)
    assert call(client, db).status_code == 200
    cooldown = client.get('/api/accounts').get_json()[0]['cooldowns']
    assert cooldown[0]['kind'] == 'rate_limit'
    assert cooldown[0]['http_status'] == 200
    assert cooldown[0]['code'] == ('429' if platform == 'trae' else '6004')


@pytest.mark.parametrize('platform,body', [
    ('trae', {'code': 4008, 'message': 'Your requests have exceeded the quota.'}),
    ('trae', {'code': 6004, 'message': 'unclassified client error'}),
    ('workbuddy', {'error': {'code': 'insufficient_quota', 'message': 'credit balance empty'}}),
])
def test_non_rate_business_failures_are_not_cooldowns(client, isolated_db, upstream, platform, body):
    db = isolated_db
    aid = add_account(db, platform)
    db.set_model_registry({'model': {'type': 'chat', 'platforms': [platform]}})
    upstream.behavior[aid] = (200, body, {})
    r = call(client, db)
    assert r.status_code != 429
    assert client.get('/api/accounts').get_json()[0]['cooldowns'] == []


def test_normal_content_mentions_of_limits_do_not_trigger_cooldown(client, isolated_db, upstream):
    db = isolated_db
    aid = add_account(db)
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    upstream.behavior[aid] = (200, 'data: {"choices":[{"delta":{"content":"6004 rate limit 429"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n', {'Content-Type':'text/event-stream'})
    r = call(client, db)
    assert r.status_code == 200
    assert '6004 rate limit 429' in r.get_data(as_text=True)
    assert client.get('/api/accounts').get_json()[0]['cooldowns'] == []


@pytest.mark.parametrize('value', [0, -1, 86401, 1.5, True, None, '3.5', [], {}, 'abc'])
def test_invalid_cooldown_setting_rejects_entire_update(client, isolated_db, value):
    r = client.post('/api/settings', json={'strategy': 'random', 'rate_limit_default_seconds': value})
    assert r.status_code == 400
    settings = client.get('/api/settings').get_json()
    assert settings['rate_limit_default_seconds'] == '60'
    assert settings['strategy'] == 'least_used'


@pytest.mark.parametrize('kind,delay', [('default', 43), ('seconds', 19), ('http-date', 95), ('reset-message', 3600), ('reset-field', 35)])
def test_cooldown_time_uses_upstream_then_persisted_default(client, isolated_db, upstream, monkeypatch, kind, delay):
    import datetime as dt
    import email.utils
    import rate_limits
    now = int(time.time())
    monkeypatch.setattr(rate_limits.time, 'time', lambda: now)
    db = isolated_db
    assert client.post('/api/settings', json={'rate_limit_default_seconds': 43}).status_code == 200
    db.init_db()
    aid = add_account(db)
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    body, headers = {'code':6004, 'msg':'busy'}, {}
    if kind == 'seconds': headers['Retry-After'] = '19'
    if kind == 'http-date': headers['Retry-After'] = email.utils.formatdate(now + 95, usegmt=True)
    if kind == 'reset-message':
        stamp = dt.datetime.fromtimestamp(now + 3600, dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        body['msg'] = 'reset at ' + stamp + ' UTC+8'
    if kind == 'reset-field': body['reset_at'] = now + 35
    if kind == 'default': headers['Retry-After'] = 'invalid'
    upstream.behavior[aid] = (429, body, headers)
    r = call(client, db)
    assert r.status_code == 429
    assert r.get_json()['error']['retry_at'] == now + delay
    assert client.get('/api/settings').get_json()['rate_limit_default_seconds'] == '43'


def test_model_scoped_limit_does_not_disable_other_models_and_expires(client, isolated_db, upstream, monkeypatch):
    import rate_limits
    base = int(time.time())
    clock = [base]
    monkeypatch.setattr(rate_limits.time, 'time', lambda: clock[0])
    db = isolated_db
    ids = [add_account(db) for _ in range(2)]
    db.set_model_registry({m: {'type':'chat', 'platforms':['workbuddy']} for m in ('model', 'other')})
    upstream.behavior[ids[0]] = (429, {'code':6004, 'msg':'busy', 'scope':'model'}, {'Retry-After':'120'})
    assert call(client, db).status_code == 200
    upstream.behavior.clear()
    assert call(client, db, model='other').status_code == 200
    assert [c[0] for c in upstream.calls] == [ids[0], ids[1], ids[0]]
    clock[0] = base + 121
    client.patch(f"/api/accounts/{ids[1]}", json={"enabled": False})
    assert call(client, db).status_code == 200
    assert client.get('/api/accounts').get_json()[0]['cooldowns'] == []
    assert [c[0] for c in upstream.calls][-1] == ids[0]


def test_confirmed_shared_service_limit_skips_other_international_card(client, isolated_db, upstream):
    db = isolated_db
    first = add_account(db, 'codebuddy', 'shared')
    duplicate = add_account(db, 'workbuddy_intl', 'shared')
    last = add_account(db, 'workbuddy_intl', 'other')
    db.set_model_registry({'model': {'type':'chat', 'platforms':['codebuddy', 'workbuddy_intl']}})
    upstream.behavior[first] = (429, {'code':6004, 'msg':'busy', 'scope':'shared_service'}, {})
    assert call(client, db).status_code == 200
    assert [c[0] for c in upstream.calls] == [first, last]
    assert client.get('/api/accounts').get_json()[1]['cooldowns']
    assert duplicate not in [c[0] for c in upstream.calls]


@pytest.mark.parametrize('platform', ['workbuddy', 'trae'])
@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/responses'])
def test_stream_error_after_output_never_replays_or_emits_fake_completion(client, isolated_db, upstream, platform, path):
    db = isolated_db
    first, second = [add_account(db, platform) for _ in range(2)]
    db.set_model_registry({'model': {'type':'chat', 'platforms':[platform]}})
    if platform == 'trae':
        body = 'event: output\ndata: {"response":"partial"}\n\nevent: error\ndata: {"code":429,"message":"busy"}\n\nevent: done\ndata: {}\n\n'
    else:
        body = 'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\ndata: {"error":{"code":6004,"message":"busy"}}\n\ndata: [DONE]\n\n'
    upstream.behavior[first] = (200, body, {'Content-Type':'text/event-stream'})
    r = call(client, db, path=path, stream=True)
    text = r.get_data(as_text=True)
    assert 'partial' in text and 'error' in text
    assert 'response.completed' not in text
    assert 'data: [DONE]' not in text
    assert [c[0] for c in upstream.calls] == [first]
    assert client.get('/api/accounts').get_json()[0]['cooldowns'][0]['kind'] == 'rate_limit'


def test_log_contains_attempt_chain_and_unknown_not_zero_credit(client, isolated_db, upstream):
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type':'chat', 'platforms':['workbuddy']}})
    token = db.get_account(first)['access_token']
    upstream.behavior[first] = (429, {'error': {'message': 'busy ' + token}}, {})
    assert call(client, db).status_code == 200
    log = client.get('/api/logs').get_json()[0]
    assert log['request_id']
    assert [a['account_id'] for a in log['attempts']] == [first, second]
    assert log['attempts'][0]['kind'] == 'rate_limit'
    assert log['attempts'][0]['retry_at'] > time.time()
    assert log['account_id'] == second
    assert log['credit_actual'] == 0
    assert token not in json.dumps(log)


@pytest.mark.parametrize('path,stream', [('/v1/chat/completions',False), ('/chat/completions',True),
    ('/v1/responses',False), ('/responses',True), ('/v1/images/generations',False), ('/images/generations',False)])
def test_protocols_and_aliases_share_limit_failover_and_key_scope(client, isolated_db, upstream, path, stream):
    db = isolated_db
    first, second = [add_account(db, 'workbuddy_intl') for _ in range(2)]
    forbidden = add_account(db, 'workbuddy')
    db.set_model_registry({'model': {'type':'image' if 'images' in path else 'chat', 'platforms':['workbuddy_intl', 'workbuddy']}})
    db.add_api_key('bound', 'bound-key', platform='workbuddy_intl')
    upstream.behavior[first] = (429, {'code':6004,'msg':'busy'}, {})
    r = call(client, db, path=path, stream=stream, key='bound-key')
    assert r.status_code == 200
    r.get_data()
    assert [c[0] for c in upstream.calls] == [first, second]
    assert forbidden not in [c[0] for c in upstream.calls]


def test_usage_only_stream_is_not_a_success_before_trying_next_account(client, isolated_db, upstream):
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type':'chat', 'platforms':['workbuddy']}})
    upstream.behavior[first] = (200, 'data: {"usage":{"credit":0}}\n\ndata: [DONE]\n\n', {'Content-Type':'text/event-stream'})
    r = call(client, db)
    assert r.status_code == 200
    assert [c[0] for c in upstream.calls] == [first, second]
    assert client.get('/api/logs').get_json()[0]['attempts'][0]['credit'] == 0


def test_image_sse_prefetch_recognizes_error_then_real_image(client, isolated_db, upstream):
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type':'image', 'platforms':['workbuddy']}})
    upstream.behavior[first] = (200, 'data: {"code":6004,"msg":"busy"}\n\n', {'Content-Type':'text/event-stream'})
    upstream.behavior[second] = (200, 'data: {"data":[{"url":"https://example.test/image.png"}]}\n\ndata: [DONE]\n\n', {'Content-Type':'text/event-stream'})
    r = call(client, db, path='/v1/images/generations')
    assert r.status_code == 200
    assert 'image.png' in r.get_data(as_text=True)
    assert [c[0] for c in upstream.calls] == [first, second]


def test_all_key_and_model_switches_hold_during_failover(client, isolated_db, upstream):
    db = isolated_db
    first = add_account(db)
    disabled = add_account(db)
    backup = add_account(db, 'trae')
    wrong_channel = add_account(db, 'codebuddy')
    db.update_account(disabled, enabled=0)
    db.set_model_registry({m: {'type':'chat', 'platforms':['workbuddy','trae','codebuddy']} for m in ('model','denied')})
    db.set_setting('model_platform_disabled', json.dumps({'codebuddy':['model']}))
    db.add_api_key('selected', 'selected-key', model_scope='selected', allowed_models=['model'])
    upstream.behavior[first] = (429, {'code':6004,'msg':'busy'}, {})
    assert call(client, db, key='selected-key').status_code == 200
    assert [c[0] for c in upstream.calls] == [first, backup]
    assert call(client, db, key='selected-key', model='denied').status_code == 403
    assert disabled not in [c[0] for c in upstream.calls] and wrong_channel not in [c[0] for c in upstream.calls]


def test_cross_channel_retry_keeps_original_tools_and_history(client, isolated_db, upstream):
    db = isolated_db
    first = add_account(db, 'trae')
    second = add_account(db, 'workbuddy_intl')
    db.set_model_registry({'model': {'type':'chat', 'platforms':['trae','workbuddy_intl']}})
    upstream.behavior[first] = (429, {'code':429,'message':'busy'}, {})
    tools = [{'type':'function', 'function':{'name':'weather', 'description':'query', 'parameters':{'type':'object'}}}]
    messages = [{'role':'user','content':'weather?'}, {'role':'assistant','content':None,'tool_calls':[
        {'id':'previous', 'type':'function','function':{'name':'weather','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'previous','content':'sunny'}, {'role':'user','content':'again'}]
    r = call(client, db, messages=messages, tools=tools, tool_choice='auto')
    assert r.status_code == 200
    forwarded = upstream.calls[-1][2]['json']
    assert forwarded['model'] == 'model'
    assert forwarded['tools'] == tools
    assert next(m for m in forwarded['messages'] if m['role'] == 'tool')['content'] == 'sunny'
    assert forwarded['messages'][-1]['content'] == 'again'
    assert [c[0] for c in upstream.calls] == [first, second]


def test_mixed_failures_are_not_all_429_and_single_probe_never_switches(client, isolated_db, upstream):
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type':'chat', 'platforms':['workbuddy']}})
    upstream.behavior[first] = (429, {'code':6004,'msg':'busy'}, {})
    upstream.behavior[second] = (503, {'error':{'message':'upstream offline'}}, {})
    r = call(client, db)
    assert r.status_code == 502
    assert 'offline' in r.get_json()['error']['message']
    again = call(client, db)
    assert again.status_code == 502
    assert 'busy' in again.get_json()['error']['message'] and 'offline' in again.get_json()['error']['message']
    before = len(upstream.calls)
    r = client.post(f'/api/accounts/{first}/probe', json={'model':'model'})
    assert r.get_json()['ok'] is False
    assert [c[0] for c in upstream.calls[before:]] == [first]


def test_control_plane_429_does_not_cool_model_generation(client, isolated_db, upstream):
    db = isolated_db
    aid = add_account(db, 'workbuddy_intl')
    upstream.behavior[aid] = (429, {'code':6004,'msg':'billing limit'}, {})
    result = client.get(f'/api/accounts/{aid}/checkin-status').get_json()
    assert result['ok'] is False
    assert client.get('/api/accounts').get_json()[0]['cooldowns'] == []


def test_account_disabled_during_401_refresh_is_not_retried(client, isolated_db, upstream, monkeypatch):
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.update_account(first, refresh_token='fixture-refresh')
    db.set_model_registry({'model': {'type':'chat', 'platforms':['workbuddy']}})
    upstream.behavior[first] = (401, {'error':{'message':'expired'}}, {})
    original = upstream.request
    def request(session, method, url, **kw):
        if url.endswith('/auth/token/refresh'):
            db.update_account(first, enabled=0)
            upstream.behavior.pop(first)
            r = requests.Response(); r.status_code = 200
            r._content = json.dumps({'code':0,'data':{'accessToken':jwt(int(time.time())+7200, uid='renewed'), 'refreshToken':'rotated'}}).encode()
            return r
        return original(session, method, url, **kw)
    monkeypatch.setattr(requests.sessions.Session, 'request', request)
    assert call(client, db).status_code == 200
    assert [c[0] for c in upstream.calls] == [first, second]
    assert client.get('/api/accounts').get_json()[0]['enabled'] == 0


def test_late_success_does_not_erase_concurrent_rate_limit(client, isolated_db, upstream):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import app
    db = isolated_db
    aid = add_account(db)
    db.set_model_registry({'model': {'type':'chat', 'platforms':['workbuddy']}})
    entered, release = threading.Event(), threading.Event()
    lock, calls = threading.Lock(), [0]
    def response(acc, url, kw):
        with lock:
            calls[0] += 1
            number = calls[0]
        r = requests.Response()
        if number == 1:
            entered.set()
            assert release.wait(5)
            r.status_code = 200
            r.headers['Content-Type'] = 'text/event-stream'
            r._content = b'data: {"choices":[{"delta":{"content":"late success"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        else:
            r.status_code = 429
            r.headers['Retry-After'] = '180'
            r._content = b'{"code":6004,"msg":"new limit"}'
        r._content_consumed = True
        return r
    upstream.behavior[aid] = response
    def older_request():
        with app.app.test_client() as other:
            return call(other, db).status_code
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(older_request)
        assert entered.wait(5)
        try:
            assert call(client, db).status_code == 429
        finally:
            release.set()
        assert future.result(5) == 200
    assert client.get('/api/accounts').get_json()[0]['cooldowns'][0]['retry_at'] > time.time() + 175
    assert call(client, db).status_code == 429
    assert calls[0] == 2


def test_retry_time_waits_for_all_limits_on_a_candidate(client, isolated_db, upstream, monkeypatch):
    import rate_limits
    now = int(time.time())
    monkeypatch.setattr(rate_limits.time, 'time', lambda: now)
    db = isolated_db
    aid = add_account(db)
    db.set_model_registry({m: {'type': 'chat', 'platforms': ['workbuddy']} for m in ('model', 'other')})
    upstream.behavior[aid] = (429, {'code': 6004, 'msg': 'model limit', 'scope': 'model'}, {'Retry-After': '120'})
    assert call(client, db).status_code == 429
    upstream.behavior[aid] = (429, {'code': 6004, 'msg': 'service limit'}, {'Retry-After': '60'})
    assert call(client, db, model='other').status_code == 429
    result = call(client, db)
    assert result.status_code == 429
    assert result.get_json()['error']['retry_at'] == now + 120
    assert result.headers['Retry-After'] == '120'
    assert len(upstream.calls) == 2


def test_explicit_attempt_budget_does_not_claim_unused_accounts_failed(client, isolated_db, upstream):
    import proxy
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    upstream.behavior[first] = (429, {'code': 6004, 'msg': 'first busy'}, {})
    with pytest.raises(proxy.ProxyError) as error:
        proxy.stream_chat({'model': 'model', 'messages': [{'role': 'user', 'content': 'hi'}]}, max_retry=1)
    assert error.value.status == 503
    assert '预算' in str(error.value) and '未尝试' in str(error.value)
    assert [c[0] for c in upstream.calls] == [first]
    assert second not in [c[0] for c in upstream.calls]


@pytest.mark.parametrize('credit', [None, '', 'NaN', 'Infinity', True])
def test_missing_or_invalid_usage_credit_remains_unknown(client, isolated_db, upstream, credit):
    db = isolated_db
    aid = add_account(db)
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    payload = {'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}], 'usage': {'credit': credit}}
    upstream.behavior[aid] = (200, 'data: ' + json.dumps(payload) + '\n\ndata: [DONE]\n\n', {'Content-Type': 'text/event-stream'})
    assert call(client, db).status_code == 200
    log = client.get('/api/logs').get_json()[0]
    assert log['credit_actual'] == 0
    assert log['attempts'][-1]['credit'] is None


def test_role_only_empty_stream_tries_next_account(client, isolated_db, upstream):
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    upstream.behavior[first] = (200, 'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: [DONE]\n\n', {'Content-Type': 'text/event-stream'})
    result = call(client, db)
    assert result.status_code == 200
    assert result.get_json()['choices'][0]['message']['content'] == 'pong'
    assert [c[0] for c in upstream.calls] == [first, second]


def test_multiline_sse_error_is_classified_before_failover(client, isolated_db, upstream):
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    upstream.behavior[first] = (200, 'data: {"error": {\ndata: "code": 6004, "message": "multiline busy"\ndata: }}\n\n', {'Content-Type': 'text/event-stream'})
    assert call(client, db).status_code == 200
    assert [c[0] for c in upstream.calls] == [first, second]
    limits = client.get('/api/accounts').get_json()[0]['cooldowns']
    assert len(limits) == 1 and limits[0]['code'] == '6004'


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/responses'])
def test_plain_error_event_ends_stream_without_exposing_credential(client, isolated_db, upstream, path):
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    token = db.get_account(first)['access_token']
    body = ('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            'event: error\ndata: rejected ' + token + '\n\ndata: [DONE]\n\n')
    upstream.behavior[first] = (200, body, {'Content-Type': 'text/event-stream'})
    result = call(client, db, path=path, stream=True).get_data(as_text=True)
    assert 'partial' in result and 'rejected' in result
    assert token not in result
    assert 'response.completed' not in result and 'data: [DONE]' not in result
    assert [c[0] for c in upstream.calls] == [first]
    assert client.get('/api/logs').get_json()[0]['status'] >= 400


def test_response_is_closed_when_prefetch_connection_breaks(client, isolated_db, upstream):
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    closed = []
    class BrokenResponse(requests.Response):
        def iter_lines(self, **kw):
            raise requests.ConnectionError('prefetch disconnected')
        def close(self):
            closed.append(first)
    def broken(acc, url, kw):
        r = BrokenResponse()
        r.status_code = 200
        r.headers['Content-Type'] = 'text/event-stream'
        return r
    upstream.behavior[first] = broken
    assert call(client, db).status_code == 200
    assert closed == [first]
    assert [c[0] for c in upstream.calls] == [first, second]


def test_failed_attempt_keeps_explicit_upstream_credit(client, isolated_db, upstream):
    db = isolated_db
    first, second = [add_account(db) for _ in range(2)]
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    upstream.behavior[first] = (503, {'error': {'message': 'generation interrupted'}, 'usage': {'credit': 0.75}}, {})
    assert call(client, db).status_code == 200
    log = client.get('/api/logs').get_json()[0]
    assert log['attempts'][0]['credit'] == 0.75
    assert log['attempts'][1]['credit'] is None
    assert log['account_id'] == second and log['credit_actual'] == 0


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/responses'])
@pytest.mark.parametrize('stream', [False, True])
def test_interrupted_stream_keeps_known_usage_on_final_account(client, isolated_db, upstream, path, stream):
    db = isolated_db
    aid = add_account(db)
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    body = ('data: {"choices":[{"delta":{"content":"partial"}}],"usage":{"credit":0.25}}\n\n'
            'data: {"error":{"code":6004,"message":"interrupted"}}\n\n')
    upstream.behavior[aid] = (200, body, {'Content-Type': 'text/event-stream'})
    call(client, db, path=path, stream=stream).get_data()
    log = client.get('/api/logs').get_json()[0]
    assert log['attempts'][0]['kind'] == 'rate_limit'
    assert log['attempts'][0]['credit'] == 0.25
    assert log['credit_actual'] == 1 and log['credit_cost'] == 0.25


@pytest.mark.parametrize('zone', ['UTC', 'Z', '+00:00', 'UTC-05:30'])
def test_reset_message_respects_explicit_timezone(client, isolated_db, upstream, monkeypatch, zone):
    import datetime as dt
    import rate_limits
    now = int(time.time())
    monkeypatch.setattr(rate_limits.time, 'time', lambda: now)
    db = isolated_db
    aid = add_account(db)
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    tz = dt.timezone(dt.timedelta(hours=-5, minutes=-30)) if zone == 'UTC-05:30' else dt.timezone.utc
    stamp = dt.datetime.fromtimestamp(now + 3600, tz).strftime('%Y-%m-%d %H:%M:%S')
    upstream.behavior[aid] = (429, {'code': 6004, 'msg': f'reset at {stamp} {zone}'}, {})
    result = call(client, db)
    assert result.get_json()['error']['retry_at'] == now + 3600


def test_error_code_echo_is_redacted_in_cooldowns_and_logs(client, isolated_db, upstream):
    db = isolated_db
    aid = add_account(db)
    refresh = 'opaque-fixture-refresh-secret'
    db.update_account(aid, refresh_token=refresh)
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    upstream.behavior[aid] = (429, {'error': {'code': refresh, 'message': 'busy'}}, {})
    assert call(client, db).status_code == 429
    assert refresh not in json.dumps(client.get('/api/accounts').get_json())
    assert refresh not in json.dumps(client.get('/api/logs').get_json())


@pytest.mark.parametrize('code,message,status', [
    ('RequestLimitExceeded.JobNumExceed', 'queue full', 429),
    ('invalid_parameter', 'invalid prompt mentioning 13212', 400),
])
def test_image_queue_limit_uses_error_code_not_arbitrary_digits(client, isolated_db, upstream, code, message, status):
    db = isolated_db
    aid = add_account(db)
    db.set_model_registry({'model': {'type': 'image', 'platforms': ['workbuddy']}})
    upstream.behavior[aid] = (400, {'error': {'code': code, 'message': message}}, {})
    result = call(client, db, path='/v1/images/generations')
    assert result.status_code == status
    attempt = client.get('/api/logs').get_json()[0]['attempts'][0]
    assert attempt['status'] == status and attempt['http_status'] == 400
    limits = client.get('/api/accounts').get_json()[0]['cooldowns']
    assert bool(limits) == (status == 429)


def test_plain_error_before_first_frame_preserves_real_reason(client, isolated_db, upstream):
    db = isolated_db
    aid = add_account(db)
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy']}})
    upstream.behavior[aid] = (200, 'event: error\ndata: fixture rejected before output\n\n', {'Content-Type': 'text/event-stream'})
    result = call(client, db)
    assert result.status_code == 502
    assert 'fixture rejected before output' in result.get_json()['error']['message']
