"""模型刷新遵循当前客户端配置，不复活旧目录、也不触发模型生成。"""
from unittest.mock import Mock
import providers as P


def response(data, status=200):
    return Mock(status_code=status, json=lambda: {'code': 0, 'data': data})


def test_workbuddy_uses_current_client_gateway_and_profile(monkeypatch):
    session = Mock()
    session.get.return_value = response({'agents': [{'name': 'cli', 'models': ['current']}]})
    monkeypatch.setattr(P, '_session', lambda: session)
    assert P.fetch_upstream_models('workbuddy', 'token', strict=True) == ['current']
    first = session.get.call_args_list[0]
    assert first.args[0] == 'https://www.workbuddy.cn/v3/config'
    assert first.kwargs['headers']['User-Agent'] == 'WorkBuddy/5.7.6 WorkBuddy/5.7.6 CLI/2.156.0'


def test_current_catalog_expands_official_paid_lines_not_arbitrary_metadata(monkeypatch):
    data = {
        'agents': [{'name': 'cli', 'models': ['shared']}],
        'models': [{'id': 'metadata-only'}, {'id': 'image-only', 'tags': ['text-to-image']}],
        'productFeaturesConfig': {'ModelRateLimitCap': {'lines': [
            {'freeId': 'shared', 'paidId': 'shared-paid'},
            {'freeId': '', 'paidId': 'incomplete-line'},
        ]}},
    }
    session = Mock(); session.get.return_value = response(data)
    monkeypatch.setattr(P, '_session', lambda: session)
    assert P.fetch_upstream_models('workbuddy_intl', 'token', strict=True) == ['shared', 'shared-paid']


def test_current_catalog_does_not_revive_removed_legacy_models(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        ids = ['current'] if url.endswith('/v3/config') else ['removed']
        return response({'agents': [{'name': 'cli', 'models': ids}]})
    monkeypatch.setattr(P, '_session', lambda: Mock(get=get))
    assert P.fetch_upstream_models('workbuddy_intl', 'token', strict=True) == ['current']
    assert calls == ['https://www.workbuddy.ai/v3/config']


from test_credentials_refresh import client


def test_refresh_models_never_runs_generation_probe(client, monkeypatch, isolated_db):
    import discovery
    isolated_db.set_model_registry({'model-a': {'type': 'chat', 'platforms': ['workbuddy']}})
    monkeypatch.setattr(discovery, 'sync_upstream_catalogs', lambda **kwargs: {'workbuddy': []})
    probe = Mock(side_effect=AssertionError('refresh must not call paid generation'))
    monkeypatch.setattr(discovery, 'run_discovery', probe)
    result = client.get('/api/models/discover')
    assert result.status_code == 200
    assert result.get_json()['image_discovering'] is False
    probe.assert_not_called()


def test_strict_refresh_reports_current_source_failure_not_old_catalog(monkeypatch):
    import pytest
    def get(url, **kwargs):
        if url.endswith('/v3/config'):
            return response({}, 503)
        return response({'agents': [{'name': 'cli', 'models': ['outdated']}]})
    monkeypatch.setattr(P, '_session', lambda: Mock(get=get))
    with pytest.raises(P.ProviderError, match='503'):
        P.fetch_upstream_models('codebuddy', 'token', strict=True)


def test_partial_account_failure_keeps_other_accounts_models(isolated_db, monkeypatch):
    import discovery
    import proxy
    db = isolated_db
    db.add_account('workbuddy', 'ok', uid='a')
    db.add_account('workbuddy', 'bad', uid='b')
    db.set_model_registry({'other-account-model': {'type': 'chat', 'platforms': ['workbuddy']}})
    monkeypatch.setattr(proxy, 'ensure_token', lambda r: dict(r))
    def fetch(platform, token, **kw):
        if token == 'bad':
            raise P.ProviderError('HTTP 503')
        return ['new-model']
    monkeypatch.setattr(P, 'fetch_upstream_models', fetch)
    discovery.sync_upstream_catalogs()
    assert set(db.get_model_registry()) == {'other-account-model', 'new-model'}


def test_workbuddy_catalog_and_chat_share_current_gateway():
    assert P.config_url('workbuddy') == 'https://www.workbuddy.cn/v3/config'
    assert P.chat_url('workbuddy') == 'https://www.workbuddy.cn/v2/chat/completions'
    assert P.build_model_headers('workbuddy', 'token')['User-Agent'] == (
        'WorkBuddy/5.7.6 WorkBuddy/5.7.6 CLI/2.156.0')


def test_empty_catalog_bootstrap_only_reads_official_models(isolated_db, monkeypatch):
    import sys
    import app
    import discovery

    targets = []
    def thread(*, target, **kwargs):
        targets.append(target)
        return Mock()
    monkeypatch.setattr(app.threading, 'Thread', thread)
    monkeypatch.setattr(sys, 'argv', ['agent2api', '--debug'])
    monkeypatch.setattr(app.app, 'run', Mock())
    sync = Mock()
    probe = Mock()
    monkeypatch.setattr(discovery, 'sync_upstream_catalogs', sync)
    monkeypatch.setattr(discovery, 'run_discovery', probe)
    app.main()
    for target in targets:
        if target is not app._maintenance_loop:
            target()
    sync.assert_called_once_with()
    probe.assert_not_called()
