"""第四渠道复用原有号池/接口/生命周期，而非另建服务。"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import providers as P
import proxy
from test_credentials_refresh import jwt, result, client


KEY = 'workbuddy_intl'


def test_platform_and_oauth_profile(monkeypatch):
    assert KEY in P.PLATFORMS
    p = P.get_platform(KEY)
    assert p['api_base'] == 'https://www.workbuddy.ai'
    assert p['platform'] == 'workbuddy-ai'
    response = Mock(status_code=200)
    response.json.return_value = {'code': 0, 'data': {'state': 'test-state'}}
    session = Mock(); session.post.return_value = response
    monkeypatch.setattr(P, '_session', lambda: session)
    out = P.start_login(KEY)
    assert 'www.workbuddy.ai/login?platform=workbuddy-ai' in out['auth_url']
    assert 'platform=workbuddy-ai' in session.post.call_args.args[0]
    assert P.supports_checkin(KEY)
    assert p["checkin_kind"] == "international_activity"


def test_new_channel_refresh_nested_payload(monkeypatch, isolated_db):
    aid = isolated_db.add_account(KEY, jwt(int(time.time()) + 10), refresh_token='old-rt')
    exp = int(time.time()) + 7200
    response = Mock(status_code=200)
    response.json.return_value = {'code': 0, 'data': {'data': {'accessToken': jwt(exp), 'refreshToken': 'new-rt'}}}
    session = Mock(); session.post.return_value = response
    monkeypatch.setattr(P, '_session', lambda: session)
    out = proxy.ensure_token(isolated_db.get_account(aid))
    assert out['refresh_token'] == 'new-rt'
    assert isolated_db.get_account(aid)['expires_at'] == exp
    call = session.post.call_args
    assert call.args[0] == 'https://www.workbuddy.ai/v2/plugin/auth/token/refresh'
    assert call.kwargs['headers']['User-Agent'] == 'WorkBuddy/5.7.6 WorkBuddy AI/5.7.6 CLI/2.156.0'


def test_model_catalog_uses_workbuddy_profile(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {'code': 0, 'data': {'agents': [{'name': 'cli', 'models': ['model-a']}]}}
    session = Mock(); session.get.return_value = response
    monkeypatch.setattr(P, '_session', lambda: session)
    assert P.fetch_upstream_models(KEY, 'token') == ['model-a']
    assert len(session.get.call_args_list) == 1
    for call in session.get.call_args_list:
        assert call.args[0].startswith('https://www.workbuddy.ai/')
        assert 'CLI/2.156.0' in call.kwargs['headers']['User-Agent']


def test_four_way_priority_and_global_switches(client, isolated_db):
    db = isolated_db
    db.set_model_registry({'model': {'type': 'chat', 'platforms': ['workbuddy', 'trae', 'codebuddy', KEY]}})
    old = {'default': ['trae', 'codebuddy', 'workbuddy'], 'overrides': {'model': ['codebuddy', 'trae', 'workbuddy']}}
    response = client.post('/api/settings', json={'model_route_priority': json.dumps(old)})
    assert response.get_json()['ok']
    saved = json.loads(db.get_setting('model_route_priority'))
    assert saved['default'] == old['default'] + [KEY]
    assert saved['overrides']['model'] == old['overrides']['model'] + [KEY]
    disabled = {p: ['model'] for p in P.PLATFORMS}
    client.post('/api/settings', json={'model_platform_disabled': json.dumps(disabled)})
    assert proxy.list_models() == []
    assert proxy.resolve_platform_candidates('model', KEY) == []
    del disabled[KEY]
    client.post('/api/settings', json={'model_platform_disabled': json.dumps(disabled)})
    assert proxy.resolve_platform_candidates('model') == [KEY]


def test_same_international_session_rotates_once_across_cards(isolated_db, monkeypatch):
    db = isolated_db
    old_token = jwt(int(time.time()) - 1)
    ids = [db.add_account(p, old_token, refresh_token='same-session', uid='same-user') for p in ('codebuddy', KEY)]
    old_rows = [dict(db.get_account(i)) for i in ids]
    def exchange(*args, **kwargs):
        time.sleep(.03)
        return result()
    refresh = Mock(side_effect=exchange)
    monkeypatch.setattr(P, 'refresh_token', refresh)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda r: proxy.ensure_token(r, force=True), old_rows))
    assert refresh.call_count == 1
    assert all(r['refresh_token'] == 'rotated-refresh' for r in rows)
    assert all(db.get_account(i)['refresh_token'] == 'rotated-refresh' for i in ids)


def test_different_international_sessions_not_overwritten(isolated_db, monkeypatch):
    db = isolated_db
    a = db.add_account('codebuddy', jwt(int(time.time()) + 30), refresh_token='session-a', uid='same-user')
    b = db.add_account(KEY, jwt(int(time.time()) + 30), refresh_token='session-b', uid='same-user')
    monkeypatch.setattr(P, 'refresh_token', Mock(return_value=result()))
    proxy.ensure_token(db.get_account(a), force=True)
    assert db.get_account(b)['refresh_token'] == 'session-b'


def test_shared_balance_is_counted_once_but_calls_stay_per_channel(isolated_db):
    db = isolated_db
    for p, balance, ts in [('codebuddy', 100, 10), (KEY, 90, 20), ('workbuddy', 30, 30)]:
        aid = db.add_account(p, 'tok', uid='same-user')
        db.update_account(aid, quota_remain=balance, quota_updated_at=ts)
    out = db.overview_usage()
    assert out['pool']['credits'] == 120
    assert out['pool']['accounts'] == 3
    assert db.stats()['quota_remain'] == 120


def test_import_reference_json_and_key_binding(client, isolated_db, monkeypatch):
    monkeypatch.setattr(proxy, 'refresh_quota', Mock(return_value={}))
    exp = int(time.time()) + 7200
    payload = {'uid': 'intl-u', 'accessToken': jwt(exp), 'refreshToken': 'r', 'domain': 'www.workbuddy.ai'}
    response = client.post('/api/accounts/import_token', json={'platform': KEY, 'json_text': json.dumps(payload)})
    assert response.status_code == 200
    account = isolated_db.find_account_by_uid(KEY, 'intl-u')
    assert account['refresh_token'] == 'r'
    assert account['expires_at'] == exp
    response = client.post('/api/keys', json={'name': 'intl', 'platform': KEY})
    assert response.status_code == 200
    key = isolated_db.get_api_key(response.get_json()['api_key'])
    assert key['platform'] == KEY


def test_workbuddy_request_normalization_does_not_mutate_prompt():
    body = {'model': 'model-a', 'messages': [{'role': 'developer', 'content': 'Keep exact wording.'}, {'role': 'user', 'content': 'Hello'}], 'stream_options': {'include_usage': True}}
    out = P.normalize_body(KEY, body)
    assert out['messages'][0] == {'role': 'system', 'content': 'Keep exact wording.'}
    assert body['messages'][0]['role'] == 'developer'
    assert out['stream'] is True
    assert 'stream_options' not in out
    headers = P.build_model_headers(KEY, 'tok', uid='u')
    assert 'WorkBuddy AI/' in headers['User-Agent']


def test_international_image_does_not_use_domestic_host():
    assert P.image_url(KEY).startswith('https://www.workbuddy.ai/')


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/responses'])
@pytest.mark.parametrize('stream', [False, True])
def test_chat_and_responses_share_routing_and_usage(client, isolated_db, monkeypatch, path, stream):
    import app as mod
    db = isolated_db
    db.set_model_registry({'model-a': {'type': 'chat', 'platforms': [KEY]}})
    aid = db.add_account(KEY, jwt(int(time.time())+7200), refresh_token='rt', uid='u')
    db.add_api_key('intl', 'fixture-api-key', KEY)
    class Response:
        status_code = 200
        _hy3_platform = KEY
        encoding = 'utf-8'
        def __init__(self):
            frames = [
                {'id':'c', 'choices':[{'index':0,'delta':{'role':'assistant','content':'OK'},'finish_reason':None}]},
                {'id':'c', 'choices':[{'index':0,'delta':{},'finish_reason':'stop'}], 'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'credit':.01}},
            ]
            self.raw = ''.join('data: '+json.dumps(f)+'\n\n' for f in frames)+'data: [DONE]\n\n'
        def close(self): pass
        def iter_lines(self, decode_unicode=False, **kwargs):
            for line in self.raw.splitlines():
                yield line if decode_unicode else line.encode()
        def iter_content(self, **kwargs):
            yield self.raw.encode()
    opened=[]
    monkeypatch.setattr(proxy, 'open_upstream', lambda acc, body: (opened.append(acc['platform']) or Response()))
    body={'model':'model-a','stream':stream}
    body.update({'input':'Hi'} if path.endswith('responses') else {'messages':[{'role':'user','content':'Hi'}]})
    response=client.post(path, headers={'Authorization':'Bearer fixture-api-key'}, json=body)
    assert response.status_code == 200
    text=response.get_data(as_text=True)
    assert 'OK' in text
    if stream:
        assert ('response.completed' if path.endswith('responses') else '[DONE]') in text
    mod._flush_stats()
    assert opened == [KEY]
    assert db.get_account(aid)['req_count'] == 1
    logs=db.list_logs()
    assert len(logs) == 1 and logs[0]['platform'] == KEY
    assert logs[0]['credit_actual'] == 1
    assert logs[0]['credit_cost'] == .01


def test_tool_calls_and_multiturn_content_preserved():
    calls=[{'id':'call_1','type':'function','function':{'name':'weather','arguments':'{"city":"Shanghai"}'}}]
    messages=[{'role':'system','content':'Keep exact prompt.'},{'role':'assistant','content':None,'tool_calls':calls},{'role':'tool','tool_call_id':'call_1','content':'20C'},{'role':'user','content':'Summarize.'}]
    tools=[{'type':'function','function':{'name':'weather','parameters':{'type':'object'}}}]
    body={'model':'glm-5.3','messages':messages,'tools':tools,'tool_choice':'auto'}
    out=P.normalize_body(KEY,body)
    assert out['messages'] == messages
    assert out['tools'] == tools
    assert out['tool_choice'] == 'auto'


def test_public_join_registers_workbuddy_intl(client,isolated_db,monkeypatch):
    db=isolated_db
    db.add_pending('new-state',KEY,'https://www.workbuddy.ai/login','new-account')
    monkeypatch.setattr(P,'poll_token',Mock(return_value=result()))
    monkeypatch.setattr(P,'fetch_account_info',Mock(return_value={'uid':'join-u','nickname':'Joined'}))
    monkeypatch.setattr(proxy,'refresh_quota',Mock(return_value={'remain':100}))
    response=client.post('/api/join/poll',json={'state':'new-state'})
    assert response.status_code == 200 and response.get_json()['ok']
    acc=db.find_account_by_uid(KEY,'join-u')
    assert acc['refresh_token'] == 'rotated-refresh'
    assert acc['source'] == 'user_join'
