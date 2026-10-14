"""国际凭证导入经管理 HTTP 验证；外部账户/额度/目录由可控响应替代。"""
import json
import time
from unittest.mock import Mock

import pytest
import providers as P
import proxy
from test_credentials_refresh import client, jwt


@pytest.fixture
def imports(client, monkeypatch):
    import discovery
    monkeypatch.setattr(proxy, 'refresh_quota', Mock(return_value={'remain': 100}))
    monkeypatch.setattr(discovery, 'sync_account_catalog', Mock(return_value={'ok': True, 'count': 0}))
    return client


def test_nested_snake_case_import_keeps_client_and_refresh_material(imports):
    expiration = int(time.time()) + 7200
    response = imports.post('/api/accounts/import_token', json={
        'platform': 'workbuddy_intl',
        'json_text': json.dumps({
            'platform': 'workbuddy',
            'label': 'My independent session',
            'auth': {'access_token': jwt(expiration), 'refresh_token': 'refresh-material',
                     'expires_at': expiration, 'domain': 'https://WWW.WORKBUDDY.AI/login/'},
            'account': {'uid': 'nested-user', 'nickname': 'Nested User',
                        'enterprise_id': 'tenant-a', 'email': 'person@example.test'},
        }),
    })
    assert response.status_code == 200
    rows = imports.get('/api/accounts').get_json()
    assert len(rows) == 1
    row = rows[0]
    assert row['platform'] == 'workbuddy_intl'
    assert row['enterprise_id'] == 'tenant-a'
    assert row['domain'] == 'www.workbuddy.ai'
    assert row['label'] == 'My independent session'
    assert row['email'] == 'person@example.test'
    assert row['has_refresh'] is True
    assert row['expires_at_sec'] == expiration
    assert 'refresh-material' not in json.dumps(row)


@pytest.mark.parametrize('target, domain', [
    ('codebuddy', 'www.codebuddy.cn'),
    ('workbuddy_intl', 'https://copilot.tencent.com/login'),
    ('workbuddy', 'www.workbuddy.ai'),
])
def test_import_explicit_region_conflict_does_not_create_account(imports, target, domain):
    r = imports.post('/api/accounts/import_token', json={
        'platform': target, 'json_text': json.dumps({'uid': 'conflict', 'access_token': jwt(int(time.time())+7200), 'domain': domain})})
    assert r.status_code == 400
    assert '区域' in r.get_json()['error']
    assert imports.get('/api/accounts').get_json() == []


@pytest.mark.parametrize('target', ['codebuddy', 'workbuddy_intl'])
def test_flat_import_accepts_either_intl_domain_without_reassigning(imports, target):
    r = imports.post('/api/accounts/import_token', json={
        'platform': target, 'json_text': json.dumps({'uid': 'same-user', 'accessToken': jwt(int(time.time())+7200), 'domain': 'codebuddy.ai'})})
    assert r.status_code == 200
    rows = imports.get('/api/accounts').get_json()
    assert len(rows) == 1 and rows[0]['platform'] == target


def test_validation_happens_before_batch_mutation(imports):
    r = imports.post('/api/accounts/import_token', json={
        'platform': 'codebuddy', 'json_text': json.dumps([
            {'uid':'good', 'access_token':jwt(int(time.time())+7200)},
            {'uid':'bad', 'access_token':'token', 'domain':'www.codebuddy.cn'},
        ])})
    assert r.status_code == 400
    assert imports.get('/api/accounts').get_json() == []
