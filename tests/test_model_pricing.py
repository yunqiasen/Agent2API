"""Official directory → persisted channel price → management HTTP, without inference."""
import time
from unittest.mock import Mock

import providers as P
from test_credentials_refresh import client


def account(db, platform='workbuddy_intl', token='catalog-token'):
    aid = db.add_account(platform, token, uid=token)
    db.update_account(aid, expires_at=int(time.time()) + 7200)
    return aid


def catalog(raw_prices):
    return {'code': 0, 'data': {
        'agents': [{'name': 'cli', 'models': list(raw_prices)}],
        'models': [{'id': mid, 'credits': raw} for mid, raw in raw_prices.items()],
    }}


def upstream(monkeypatch, payload):
    session = Mock()
    session.get.return_value = Mock(status_code=200, json=lambda: payload)
    monkeypatch.setattr(P, '_session', lambda: session)
    return session


def entries(client, platform='workbuddy_intl'):
    r = client.get('/api/platforms/' + platform + '/models')
    assert r.status_code == 200
    return {x['id']: x for x in r.get_json()['models']}


def test_directory_exposes_exact_multiplier_without_changing_public_models(client, isolated_db, monkeypatch):
    account(isolated_db)
    session = upstream(monkeypatch, catalog({'deepseek-v4.1-flash': 'x0.00',
                                           'deepseek-v4.1-flash-sg': 'x0.03',
                                           'fast-model': 'x0.34 credits',
                                           'missing': '', 'tiny': 'x0.00000001'}))
    r = client.get('/api/models/discover')
    assert r.status_code == 200
    models = entries(client)
    assert models['deepseek-v4.1-flash']['pricing']['label'] == '免费'
    assert models['deepseek-v4.1-flash-sg']['pricing']['label'] == '×0.03'
    assert models['fast-model']['pricing']['label'] == '×0.34'
    assert models['missing']['pricing']['label'] == '未公布'
    assert models['tiny']['pricing']['label'] == '×0.00000001'
    price = models['deepseek-v4.1-flash-sg']['pricing']
    assert price['source'] == 'https://www.workbuddy.ai/v3/config'
    assert price['updated_at'] > 0 and not price['stale']
    assert session.get.call_count == 1
    session.post.assert_not_called()
    public = client.get('/v1/models', headers={'Authorization': 'Bearer ' + isolated_db.DEFAULT_API_KEY}).get_json()
    assert all(set(m) == {'id', 'object', 'created', 'owned_by'} for m in public['data'])


def test_failed_refresh_marks_saved_prices_old_and_success_missing_price_clears_it(client, isolated_db, monkeypatch):
    aid = account(isolated_db)
    session = upstream(monkeypatch, catalog({'model-a': 'x0.03'}))
    client.post(f'/api/accounts/{aid}/models/sync')
    first = entries(client)['model-a']['pricing']
    session.get.side_effect = RuntimeError('upstream unavailable')
    r = client.post(f'/api/accounts/{aid}/models/sync')
    assert not r.get_json()['ok']
    old = entries(client)['model-a']['pricing']
    assert old['label'] == '×0.03' and old['stale']
    assert old['updated_at'] == first['updated_at']
    session.get.side_effect = None
    session.get.return_value = Mock(status_code=200, json=lambda: catalog({'model-a': None}))
    client.post(f'/api/accounts/{aid}/models/sync')
    new = entries(client)['model-a']['pricing']
    assert new['label'] == '未公布' and not new['stale']


def test_conflicting_account_prices_are_not_reported_as_free(client, isolated_db, monkeypatch):
    account(isolated_db, token='one')
    account(isolated_db, token='two')
    def get(url, **kwargs):
        raw = 'x0.00' if kwargs['headers']['Authorization'] == 'Bearer one' else 'x0.03'
        return Mock(status_code=200, json=lambda: catalog({'shared': raw}))
    monkeypatch.setattr(P, '_session', lambda: Mock(get=get))
    client.get('/api/models/discover')
    price = entries(client)['shared']['pricing']
    assert price['label'] == '未公布'
    assert '账号' in price['note'] and '不同' in price['note']
    assert {s['raw'] for s in price['observations']} == {'x0.00', 'x0.03'}


import pytest


@pytest.mark.parametrize('raw', [None, '', '0.03', '-1', 'x-1', 'xNaN', 'xInfinity',
                                 '300000 context', 'free until tomorrow', 0, False])
def test_missing_or_malformed_prices_are_never_free(client, isolated_db, monkeypatch, raw):
    account(isolated_db)
    upstream(monkeypatch, catalog({'model-a': raw}))
    client.get('/api/models/discover')
    assert entries(client)['model-a']['pricing']['label'] == '未公布'


def test_channel_isolation_and_no_active_account_keeps_old_marker(client, isolated_db, monkeypatch):
    ids = {p: account(isolated_db, p, token=p) for p in ('workbuddy', 'workbuddy_intl', 'codebuddy')}
    values = {'workbuddy': 'x0', 'workbuddy_intl': 'x0.03', 'codebuddy': 'x0.5'}
    def get(url, **kwargs):
        platform = kwargs['headers']['Authorization'].removeprefix('Bearer ')
        return Mock(status_code=200, json=lambda: catalog({'same': values[platform]}))
    monkeypatch.setattr(P, '_session', lambda: Mock(get=get))
    client.get('/api/models/discover')
    assert entries(client, 'workbuddy')['same']['pricing']['label'] == '免费'
    assert entries(client, 'workbuddy_intl')['same']['pricing']['label'] == '×0.03'
    assert entries(client, 'codebuddy')['same']['pricing']['label'] == '×0.5'
    isolated_db.update_account(ids['codebuddy'], enabled=0)
    r = client.get('/api/models/discover')
    assert r.get_json()['catalog_status']['codebuddy']['ok'] is None
    p = entries(client, 'codebuddy')['same']['pricing']
    assert p['label'] == '×0.5' and p['stale']


def test_partial_failure_preserves_other_account_price_and_reports_staleness(client, isolated_db, monkeypatch):
    account(isolated_db, token='one'); account(isolated_db, token='two')
    upstream(monkeypatch, catalog({'m': 'x0.03'}))
    client.get('/api/models/discover')
    def get(url, **kwargs):
        if kwargs['headers']['Authorization'] == 'Bearer two':
            raise RuntimeError('catalog offline')
        return Mock(status_code=200, json=lambda: catalog({'m': 'x0.03'}))
    monkeypatch.setattr(P, '_session', lambda: Mock(get=get))
    d = client.get('/api/models/discover').get_json()
    assert d['catalog_status']['workbuddy_intl']['ok'] is False
    p = entries(client)['m']['pricing']
    assert p['label'] == '×0.03' and p['stale']
    assert len(p['observations']) == 2


def test_trae_unverified_price_fields_stay_unpublished(client, isolated_db, monkeypatch):
    import trae
    account(isolated_db, 'trae')
    session = Mock()
    session.post.return_value = Mock(status_code=200, json=lambda: {'config_info_list': [
        {'config_name': 'trae-model', 'display_config': {'display_name': 'Trae Model'},
         'max_tokens': 8192}]})
    monkeypatch.setattr(trae, '_session', lambda: session)
    client.get('/api/models/discover')
    assert entries(client, 'trae')['trae-model']['pricing']['label'] == '未公布'
    assert session.post.call_count == 1
    assert session.post.call_args.args[0].endswith('/api/ide/v1/get_detail_param')
