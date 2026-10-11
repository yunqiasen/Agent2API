"""调用密钥范围：管理/公共 HTTP，旧库迁移及受控上游，不用正式账号。"""
import json
import sqlite3
import time

import pytest

from test_credentials_refresh import client


def seed_catalog(db):
    db.set_model_registry({
        'chat-a': {'type': 'chat', 'platforms': ['workbuddy', 'trae', 'codebuddy', 'workbuddy_intl']},
        'chat-b': {'type': 'chat', 'platforms': ['workbuddy', 'codebuddy']},
        'intl-only': {'type': 'chat', 'platforms': ['workbuddy_intl']},
        'image-a': {'type': 'image', 'platforms': ['workbuddy']},
        'audio-a': {'type': 'audio', 'platforms': ['workbuddy']},
    })


def headers(key):
    return {'Authorization': 'Bearer ' + key}


def visible(client, key):
    response = client.get('/v1/models', headers=headers(key))
    assert response.status_code == 200
    data = response.get_json()
    assert data['object'] == 'list'
    assert all(set(m) == {'id', 'object', 'created', 'owned_by'} for m in data['data'])
    return [m['id'] for m in data['data']]


def key_row(client, key):
    return next(row for row in client.get('/api/keys').get_json() if row['api_key'] == key)


def test_legacy_database_and_old_create_keep_unrestricted_permissions(client, isolated_db):
    db = isolated_db
    seed_catalog(db)
    # 独立旧库结构作为输入，验证只通过管理与公共接口观察升级结果。
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.executescript('''
            DROP TABLE api_keys;
            CREATE TABLE api_keys (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT DEFAULT '',
              api_key TEXT NOT NULL UNIQUE, enabled INTEGER DEFAULT 1,
              platform TEXT DEFAULT '', req_count INTEGER DEFAULT 0,
              last_used INTEGER DEFAULT 0, created_at INTEGER);
            INSERT INTO api_keys VALUES
              (7, '旧密钥', 'legacy-test-key', 1, 'workbuddy', 23, 12345, 10000),
              (8, '停用密钥', 'disabled-test-key', 0, 'trae', 5, 23456, 20000);
        ''')
    db.init_db()
    db.init_db()  # 重启/重复迁移不改现有记录
    old = key_row(client, 'legacy-test-key')
    assert old['model_scope'] == 'all'
    assert old['allowed_models'] == []
    assert {k: old[k] for k in ('id', 'platform', 'enabled', 'req_count', 'last_used', 'created_at')} == {
        'id': 7, 'platform': 'workbuddy', 'enabled': 1, 'req_count': 23,
        'last_used': 12345, 'created_at': 10000}
    assert visible(client, 'legacy-test-key') == ['audio-a', 'chat-a', 'chat-b', 'image-a']
    disabled = key_row(client, 'disabled-test-key')
    assert disabled['enabled'] == 0 and disabled['req_count'] == 5
    assert client.get('/v1/models', headers=headers('disabled-test-key')).status_code == 401
    created = client.post('/api/keys', json={'name': '旧客户端', 'api_key': 'old-client-test-key'})
    assert created.status_code == 200
    assert key_row(client, 'old-client-test-key')['model_scope'] == 'all'
    assert visible(client, 'old-client-test-key') == ['audio-a', 'chat-a', 'chat-b', 'image-a', 'intl-only']


def test_create_selected_normalizes_exact_ids_and_filters_public_list(client, isolated_db):
    seed_catalog(isolated_db)
    created = client.post('/api/keys', json={
        'name': '指定模型测试', 'api_key': 'selected-test-key', 'platform': 'workbuddy',
        'model_scope': 'selected', 'allowed_models': [' chat-b ', 'chat-a', 'chat-b', 'image-a', 'audio-a']})
    assert created.status_code == 200
    row = key_row(client, 'selected-test-key')
    assert row['model_scope'] == 'selected'
    assert row['allowed_models'] == ['chat-b', 'chat-a', 'image-a', 'audio-a']
    assert visible(client, 'selected-test-key') == ['audio-a', 'chat-a', 'chat-b', 'image-a']
    isolated_db.update_model_registry(lambda current: {**current, 'new-chat': {'type': 'chat', 'platforms': ['workbuddy']}})
    assert visible(client, 'selected-test-key') == ['audio-a', 'chat-a', 'chat-b', 'image-a']


@pytest.mark.parametrize('fields', [
    {'model_scope': 'selected'},
    {'model_scope': 'selected', 'allowed_models': []},
    {'model_scope': 'selected', 'allowed_models': 'chat-a'},
    {'model_scope': 'selected', 'allowed_models': None},
    {'model_scope': 'selected', 'allowed_models': {}},
    {'model_scope': 'selected', 'allowed_models': [1]},
    {'model_scope': 'selected', 'allowed_models': ['']},
    {'model_scope': 'selected', 'allowed_models': ['chat-a', '  ']},
    {'model_scope': None}, {'model_scope': True}, {'model_scope': 'unknown'},
    {'model_scope': 'all', 'allowed_models': ['chat-a']},
    {'model_scope': 'all', 'allowed_models': None},
    {'allowed_models': ['chat-a']},
    {'model_scope': 'selected', 'allowed_models': ['Chat-A']},
    {'model_scope': 'selected', 'allowed_models': ['unknown-model']},
    {'model_scope': 'selected', 'allowed_models': ['chat-b']},
    {'model_scope': 'selected', 'allowed_models': ['intl-only']},
])
def test_invalid_new_scope_is_rejected_without_creating_key(client, isolated_db, fields):
    seed_catalog(isolated_db)
    isolated_db.set_setting('model_platform_disabled', json.dumps({'workbuddy': ['chat-b']}))
    before = client.get('/api/keys').get_json()
    response = client.post('/api/keys', json={'name': '非法配置', 'platform': 'workbuddy', **fields})
    assert response.status_code == 400
    assert response.get_json()['error']
    assert client.get('/api/keys').get_json() == before


def test_duplicate_custom_key_reports_conflict_and_preserves_original(client, isolated_db):
    seed_catalog(isolated_db)
    payload = {'name': '原权限', 'api_key': 'duplicate-test-key', 'platform': 'workbuddy',
               'model_scope': 'selected', 'allowed_models': ['chat-a']}
    assert client.post('/api/keys', json=payload).status_code == 200
    before = key_row(client, payload['api_key'])
    response = client.post('/api/keys', json={**payload, 'name': '覆盖尝试', 'allowed_models': ['chat-b']})
    assert response.status_code == 409
    assert key_row(client, payload['api_key']) == before


def create_selected(client, models=None, platform='', key='scope-test-key'):
    response = client.post('/api/keys', json={'name': '范围测试', 'api_key': key, 'platform': platform,
        'model_scope': 'selected', 'allowed_models': models if models is not None else ['chat-a']})
    assert response.status_code == 200, response.get_json()
    return key_row(client, key)


def test_edit_scope_is_atomic_and_keeps_key_identity_statistics(client, isolated_db):
    seed_catalog(isolated_db)
    row = create_selected(client, platform='workbuddy')
    isolated_db.bump_api_key(row['id'])
    before = key_row(client, row['api_key'])
    response = client.patch(f"/api/keys/{row['id']}", json={
        'platform': 'workbuddy_intl', 'model_scope': 'selected', 'allowed_models': ['intl-only']})
    assert response.status_code == 200
    after = key_row(client, row['api_key'])
    assert after['platform'] == 'workbuddy_intl'
    assert after['allowed_models'] == ['intl-only']
    assert visible(client, row['api_key']) == ['intl-only']
    for name in ('id', 'api_key', 'name', 'enabled', 'req_count', 'last_used', 'created_at'):
        assert after[name] == before[name]
    invalid = client.patch(f"/api/keys/{row['id']}", json={
        'platform': 'trae', 'allowed_models': ['intl-only'], 'enabled': 0})
    assert invalid.status_code == 400
    assert 'intl-only' in invalid.get_json()['error']
    assert key_row(client, row['api_key']) == after
    assert client.patch('/api/keys/99999', json={'enabled': 0}).status_code == 404


def test_edit_omitted_fields_preserve_history_and_switch_to_all_clears_list(client, isolated_db):
    seed_catalog(isolated_db)
    row = create_selected(client, ['chat-b'], platform='workbuddy')
    endpoint = f"/api/keys/{row['id']}"
    # 表单打开后全局关闭，原选择保留；仅启停密钥仍然可操作。
    isolated_db.set_setting('model_platform_disabled', json.dumps({'workbuddy': ['chat-b']}))
    denied = client.patch(endpoint, json={'allowed_models': ['chat-b']})
    assert denied.status_code == 400 and 'chat-b' in denied.get_json()['error']
    assert client.patch(endpoint, json={'enabled': 0}).status_code == 200
    disabled = key_row(client, row['api_key'])
    assert disabled['allowed_models'] == ['chat-b'] and disabled['model_scope'] == 'selected'
    assert client.patch(endpoint, json={'enabled': 1}).status_code == 200
    assert visible(client, row['api_key']) == []
    assert client.patch(endpoint, json={'model_scope': 'all'}).status_code == 200
    unrestricted = key_row(client, row['api_key'])
    assert unrestricted['model_scope'] == 'all' and unrestricted['allowed_models'] == []
    assert client.patch(endpoint, json={'model_scope': 'selected'}).status_code == 400
    assert key_row(client, row['api_key']) == unrestricted
    assert client.patch(endpoint, json={'model_scope': 'selected', 'allowed_models': ['chat-a']}).status_code == 200
    assert visible(client, row['api_key']) == ['chat-a']


@pytest.fixture
def upstream(isolated_db, monkeypatch):
    """网络边界替身：正常生成/实时探测都经过同一HTTP计数，不mock权限或路由。"""
    import requests
    aid = isolated_db.add_account('workbuddy', 'fixture-access', uid='fixture-user')
    isolated_db.update_account(aid, expires_at=int(time.time()) + 7200, quota_remain=1000)
    calls = []

    def request(session, method, url, **kwargs):
        calls.append({'method': method, 'url': url, 'body': kwargs.get('json'), 'headers': kwargs.get('headers')})
        response = requests.Response()
        response.status_code = 200
        response.encoding = 'utf-8'
        response._content_consumed = True
        if '/images/generations' in url:
            response.headers['Content-Type'] = 'application/json'
            response._content = json.dumps({'code': 0, 'data': {'data': [{'url': 'https://example.test/fixture.png'}]}}).encode()
        else:
            response.headers['Content-Type'] = 'text/event-stream'
            event = {'model': (kwargs.get('json') or {}).get('model'),
                     'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'fixture-pong'}, 'finish_reason': 'stop'}]}
            response._content = ('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode()
        return response

    monkeypatch.setattr(requests.sessions.Session, 'request', request)
    yield calls
    import app
    app._flush_stats(3)


ROUTES = ['/v1/chat/completions', '/chat/completions', '/v1/responses', '/responses',
          '/v1/images/generations', '/images/generations']


def request_model(client, path, key, model, stream=False, auth='bearer'):
    payload = {'model': model, 'stream': stream}
    if path.endswith('/responses'):
        payload['input'] = 'ping'
    elif path.endswith('/generations'):
        payload['prompt'] = 'a blue square'
    else:
        payload['messages'] = [{'role': 'user', 'content': 'ping'}]
    kwargs = {'json': payload}
    if auth == 'query':
        kwargs['query_string'] = {'key': key}
    else:
        kwargs['headers'] = headers(key) if auth == 'bearer' else {'x-api-key': key}
    return client.post(path, **kwargs)


@pytest.mark.parametrize('path', ROUTES)
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('auth', ['bearer', 'x-api-key', 'query'])
@pytest.mark.parametrize('unknown', [False, True])
def test_every_generation_entry_blocks_outside_key_without_any_upstream_call(
        client, isolated_db, upstream, path, stream, auth, unknown):
    seed_catalog(isolated_db)
    row = create_selected(client)
    model = 'unlisted-never-probe' if unknown else ('image-a' if path.endswith('/generations') else 'chat-b')
    response = request_model(client, path, row['api_key'], model, stream, auth)
    assert response.status_code == 403
    assert response.get_json()['error']['type'] == 'model_not_allowed'
    assert model in response.get_json()['error']['message']
    assert upstream == []


@pytest.mark.parametrize('scope, raw', [
    ('unknown', '["chat-a"]'), ('selected', 'not-json'), ('selected', '[]'),
    ('selected', 'null'), ('selected', '"chat-a"'), ('selected', '[true]'),
    ('selected', '["chat-a", " "]'), ('all', 'not-json'), ('all', '["chat-a"]'),
])
def test_corrupt_permission_stays_closed_and_is_reported_in_management(
        client, isolated_db, upstream, scope, raw):
    seed_catalog(isolated_db)
    row = create_selected(client)
    # 人为损坏临时库作为输入；接口应报清问题而不是升级为不限。
    with sqlite3.connect(isolated_db.DB_PATH) as conn:
        conn.execute('UPDATE api_keys SET model_scope=?, allowed_models=? WHERE id=?', (scope, raw, row['id']))
    response = client.get('/api/keys')
    assert response.status_code == 200
    saved = key_row(client, row['api_key'])
    assert saved['model_scope_error']
    assert visible(client, row['api_key']) == []
    denied = request_model(client, '/v1/chat/completions', row['api_key'], 'chat-a')
    assert denied.status_code == 403
    assert '配置异常' in denied.get_json()['error']['message']
    assert upstream == []
    assert client.patch(f"/api/keys/{row['id']}", json={'enabled': 0}).status_code == 200
    assert key_row(client, row['api_key'])['model_scope_error']
    assert client.patch(f"/api/keys/{row['id']}", json={'platform': 'workbuddy'}).status_code == 400
    repair = client.patch(f"/api/keys/{row['id']}", json={
        'model_scope': 'selected', 'allowed_models': ['chat-b'], 'enabled': 1})
    assert repair.status_code == 200
    assert not key_row(client, row['api_key'])['model_scope_error']
    assert visible(client, row['api_key']) == ['chat-b']


@pytest.mark.parametrize('path', ROUTES)
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('auth', ['bearer', 'x-api-key', 'query'])
def test_allowed_models_keep_normal_generation_and_protocols(client, isolated_db, upstream, path, stream, auth):
    seed_catalog(isolated_db)
    model = 'image-a' if path.endswith('/generations') else 'chat-a'
    row = create_selected(client, [model], platform='workbuddy')
    response = request_model(client, path, row['api_key'], model, stream, auth)
    assert response.status_code == 200
    if path.endswith('/generations'):
        assert response.get_json()['data'][0]['url'] == 'https://example.test/fixture.png'
    elif stream:
        assert 'fixture-pong' in response.get_data(as_text=True)
        if path.endswith('/responses'):
            assert 'response.completed' in response.get_data(as_text=True)
    elif path.endswith('/responses'):
        assert response.get_json()['output'][0]['content'][0]['text'] == 'fixture-pong'
    else:
        assert response.get_json()['choices'][0]['message']['content'] == 'fixture-pong'
    assert len(upstream) == 1 and upstream[0]['body']['model'] == model


@pytest.mark.parametrize('platform', ['workbuddy', 'trae', 'codebuddy', 'workbuddy_intl'])
def test_bound_key_does_not_borrow_another_channels_switch(client, isolated_db, upstream, platform):
    seed_catalog(isolated_db)
    row = create_selected(client, ['chat-a'], platform=platform)
    assert visible(client, row['api_key']) == ['chat-a']
    isolated_db.set_setting('model_platform_disabled', json.dumps({platform: ['chat-a']}))
    assert visible(client, row['api_key']) == []
    assert request_model(client, '/v1/chat/completions', row['api_key'], 'chat-a').status_code == 403
    assert upstream == []
    assert key_row(client, row['api_key'])['allowed_models'] == ['chat-a']
    isolated_db.set_setting('model_platform_disabled', '{}')
    assert visible(client, row['api_key']) == ['chat-a']


def test_full_pool_deduplicates_and_follows_priority_only_on_enabled_channels(client, isolated_db, upstream):
    seed_catalog(isolated_db)
    for platform in ('codebuddy', 'workbuddy_intl'):
        aid = isolated_db.add_account(platform, 'fixture-' + platform, uid=platform)
        isolated_db.update_account(aid, expires_at=int(time.time()) + 7200, quota_remain=100)
    row = create_selected(client, ['chat-a'])
    isolated_db.set_setting('model_route_priority', json.dumps({
        'default': ['workbuddy', 'codebuddy', 'workbuddy_intl'],
        'overrides': {'chat-a': ['workbuddy_intl', 'codebuddy', 'workbuddy']}}))
    isolated_db.set_setting('model_platform_disabled', json.dumps({'workbuddy_intl': ['chat-a']}))
    assert visible(client, row['api_key']) == ['chat-a']
    assert request_model(client, '/v1/chat/completions', row['api_key'], 'chat-a').status_code == 200
    assert len(upstream) == 1 and upstream[0]['url'] == 'https://www.codebuddy.ai/v2/chat/completions'
    upstream.clear()
    isolated_db.set_setting('model_platform_disabled', json.dumps({
        p: ['chat-a'] for p in ('workbuddy', 'trae', 'codebuddy', 'workbuddy_intl')}))
    assert visible(client, row['api_key']) == []
    assert request_model(client, '/v1/chat/completions', row['api_key'], 'chat-a').status_code == 403
    assert upstream == []
    # 当前目录移除的旧ID保持收紧，外部调用不会用探测重新加入。
    isolated_db.set_setting('model_platform_disabled', '{}')
    isolated_db.set_model_registry({'chat-b': {'type': 'chat', 'platforms': ['workbuddy']}})
    assert request_model(client, '/v1/chat/completions', row['api_key'], 'chat-a').status_code == 403
    assert upstream == []


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/chat/completions', '/v1/responses', '/responses'])
def test_explicit_and_replaced_model_both_require_permission(client, isolated_db, upstream, path):
    seed_catalog(isolated_db)
    row = create_selected(client, ['chat-b'])
    isolated_db.set_setting('allow_model_passthrough', '0')
    isolated_db.set_setting('default_model', 'chat-a')
    denied = request_model(client, path, row['api_key'], 'chat-b')
    assert denied.status_code == 403 and 'chat-a' in denied.get_json()['error']['message']
    isolated_db.set_setting('default_model', 'chat-b')
    denied = request_model(client, path, row['api_key'], 'chat-a')
    assert denied.status_code == 403 and 'chat-a' in denied.get_json()['error']['message']
    assert upstream == []
    assert request_model(client, path, row['api_key'], 'chat-b').status_code == 200
    assert upstream[0]['body']['model'] == 'chat-b'


def test_default_model_and_required_parameters_keep_existing_contract(client, isolated_db, upstream):
    seed_catalog(isolated_db)
    row = create_selected(client, ['chat-b'])
    body = {'messages': [{'role': 'user', 'content': 'ping'}]}
    denied = client.post('/v1/chat/completions', headers=headers(row['api_key']), json=body)
    assert denied.status_code == 403 and 'chat-a' in denied.get_json()['error']['message']
    for path, payload, message in [('/v1/responses', {'input': 'ping'}, '缺少 model'),
                                   ('/v1/responses', {'model': 'chat-a'}, '缺少 input'),
                                   ('/v1/chat/completions', {'model': 'chat-a'}, 'messages'),
                                   ('/v1/images/generations', {'prompt': 'square'}, '缺少 model')]:
        response = client.post(path, headers=headers(row['api_key']), json=payload)
        assert response.status_code == 400 and message in response.get_json()['error']['message']
    assert upstream == []
    isolated_db.set_setting('default_model', 'chat-b')
    assert client.post('/v1/chat/completions', headers=headers(row['api_key']), json=body).status_code == 200
    assert upstream[0]['body']['model'] == 'chat-b'


def test_permissions_changes_disable_and_delete_apply_to_next_request(client, isolated_db, upstream):
    seed_catalog(isolated_db)
    row = create_selected(client, ['chat-a'])
    endpoint = f"/api/keys/{row['id']}"
    assert client.patch(endpoint, json={'allowed_models': ['chat-b']}).status_code == 200
    assert request_model(client, '/v1/chat/completions', row['api_key'], 'chat-a').status_code == 403
    assert client.patch(endpoint, json={'enabled': False}).status_code == 200
    assert request_model(client, '/v1/chat/completions', row['api_key'], 'chat-b').status_code == 401
    assert client.delete(endpoint).status_code == 200
    assert request_model(client, '/v1/chat/completions', row['api_key'], 'chat-b').status_code == 401
    assert upstream == []


def test_unrestricted_unknown_model_keeps_existing_live_discovery_behavior(client, isolated_db, upstream):
    seed_catalog(isolated_db)
    response = request_model(client, '/v1/chat/completions', isolated_db.DEFAULT_API_KEY, 'unknown-allowed-probe')
    assert response.status_code == 200
    assert 'fixture-pong' in response.get_data(as_text=True)
    assert len(upstream) >= 2  # 原未知模型探测 + 外部请求本身，指定Key则两者都不发生。


def test_registered_audio_uses_same_key_scope_without_new_protocol(client, isolated_db, upstream):
    seed_catalog(isolated_db)
    row = create_selected(client, ['audio-a'], platform='workbuddy')
    assert visible(client, row['api_key']) == ['audio-a']
    assert request_model(client, '/v1/chat/completions', row['api_key'], 'chat-a').status_code == 403
    assert upstream == []
    assert request_model(client, '/v1/chat/completions', row['api_key'], 'audio-a').status_code == 200
    assert upstream[0]['body']['model'] == 'audio-a'


@pytest.mark.parametrize('changes', [
    {'platform': None}, {'platform': 'missing-channel'},
    {'model_scope': None}, {'model_scope': 'unknown'},
    {'allowed_models': None}, {'allowed_models': []},
    {'model_scope': 'all', 'allowed_models': ['chat-a']},
])
def test_invalid_patch_is_not_partial_even_with_enabled_change(client, isolated_db, changes):
    seed_catalog(isolated_db)
    row = create_selected(client, platform='workbuddy')
    response = client.patch(f"/api/keys/{row['id']}", json={'enabled': 0, **changes})
    assert response.status_code == 400
    assert key_row(client, row['api_key']) == row
