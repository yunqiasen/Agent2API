"""凭证生命周期回归：临时 DB，网络边界 mock，不使用生产 token。"""
import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import providers as P
import proxy


def jwt(exp, **claims):
    data = dict(exp=exp, sub='fixture-user', **claims)
    enc = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    return 'fixture.' + enc + '.signature'


def result():
    exp = int(time.time()) + 7200
    return {'access_token': jwt(exp), 'refresh_token': 'rotated-refresh', 'expires_at': exp}


@pytest.mark.parametrize('platform', ['workbuddy', 'trae', 'codebuddy'])
def test_missing_db_expiry_uses_jwt_and_persists_rotation(isolated_db, monkeypatch, platform):
    db = isolated_db
    aid = db.add_account(platform, jwt(int(time.time()) + 30), refresh_token='old-refresh')
    db.update_account(aid, expires_at=0)  # 模拟升级前的历史凭证
    refresh = Mock(return_value=result())
    monkeypatch.setattr(P, 'refresh_token', refresh)
    acc = proxy.ensure_token(db.get_account(aid))
    assert refresh.call_count == 1
    saved = dict(db.get_account(aid))
    assert saved['refresh_token'] == 'rotated-refresh'
    assert saved['expires_at'] > time.time() + 3600
    assert saved['last_refresh_at'] > 0
    assert not saved['refresh_error']
    assert acc['access_token'] == saved['access_token']


def test_unknown_expiry_with_refresh_gets_bounded_refresh(isolated_db, monkeypatch):
    aid = isolated_db.add_account('workbuddy', 'opaque', refresh_token='rt')
    refresh = Mock(return_value={'access_token': 'new-opaque', 'refresh_token': 'new-rt'})
    monkeypatch.setattr(P, 'refresh_token', refresh)
    proxy.ensure_token(isolated_db.get_account(aid))
    proxy.ensure_token(isolated_db.get_account(aid))
    assert refresh.call_count == 1


def test_future_jwt_backfilled_without_network(isolated_db, monkeypatch):
    exp = int(time.time()) + 7200
    aid = isolated_db.add_account('codebuddy', jwt(exp), refresh_token='rt')
    isolated_db.update_account(aid, expires_at=0)
    refresh = Mock()
    monkeypatch.setattr(P, 'refresh_token', refresh)
    proxy.ensure_token(isolated_db.get_account(aid))
    assert isolated_db.get_account(aid)['expires_at'] == exp
    refresh.assert_not_called()


def test_failed_force_refresh_raises_and_redacts_credentials(isolated_db, monkeypatch, caplog):
    at = jwt(int(time.time()) + 7200)
    aid = isolated_db.add_account('workbuddy', at, refresh_token='secret-refresh')
    monkeypatch.setattr(P, 'refresh_token', Mock(side_effect=P.ProviderError(at + ' secret-refresh rejected')))
    with pytest.raises(proxy.ProxyError) as exc:
        proxy.ensure_token(isolated_db.get_account(aid), force=True)
    saved = dict(isolated_db.get_account(aid))
    assert saved['refresh_error']
    assert not saved['last_refresh_at']
    assert 'secret-refresh' not in str(exc.value) + caplog.text + saved['refresh_error']
    assert at not in str(exc.value) + caplog.text + saved['refresh_error']
    assert saved['access_token'] == at


def test_failure_backoff_does_not_drop_still_valid_access(isolated_db, monkeypatch):
    aid = isolated_db.add_account('workbuddy', jwt(int(time.time()) + 120), refresh_token='rt')
    refresh = Mock(side_effect=P.ProviderError('network temporary error'))
    monkeypatch.setattr(P, 'refresh_token', refresh)
    for _ in range(2):
        assert proxy.ensure_token(isolated_db.get_account(aid))['id'] == aid
    assert refresh.call_count == 1
    assert isolated_db.get_account(aid)['refresh_retry_at'] > time.time()
    assert isolated_db.get_account(aid)['refresh_error']


def test_forced_missing_refresh_is_not_success(isolated_db):
    aid = isolated_db.add_account('workbuddy', jwt(int(time.time()) + 7200))
    with pytest.raises(proxy.ProxyError, match='刷新凭证'):
        proxy.ensure_token(isolated_db.get_account(aid), force=True)


def test_concurrent_refresh_runs_once(isolated_db, monkeypatch):
    aid = isolated_db.add_account('trae', jwt(int(time.time()) - 1), refresh_token='rt')
    old = dict(isolated_db.get_account(aid))
    def exchange(*args, **kwargs):
        time.sleep(.04)
        return result()
    refresh = Mock(side_effect=exchange)
    monkeypatch.setattr(P, 'refresh_token', refresh)
    with ThreadPoolExecutor(max_workers=5) as pool:
        rows = list(pool.map(lambda _: proxy.ensure_token(old, force=True), range(5)))
    assert refresh.call_count == 1
    assert all(r['refresh_token'] == 'rotated-refresh' for r in rows)


@pytest.fixture
def client(isolated_db):
    import app as mod
    mod.app.config['TESTING'] = True
    mod.app.secret_key = 'fixture-secret'
    with mod.app.test_client() as c:
        with c.session_transaction() as s:
            s['admin'] = True
        yield c


def test_refresh_endpoint_does_not_mask_refresh_failure(client, isolated_db, monkeypatch):
    aid = isolated_db.add_account('workbuddy', jwt(int(time.time()) + 7200), refresh_token='rt')
    monkeypatch.setattr(P, 'refresh_token', Mock(side_effect=P.ProviderError('refresh rejected')))
    quota = Mock(return_value={'total': 100, 'remain': 100})
    monkeypatch.setattr(proxy, 'refresh_quota', quota)
    response = client.post('/api/accounts/%s/refresh' % aid, json={})
    assert response.status_code >= 400
    assert response.get_json()['ok'] is False
    quota.assert_not_called()


def test_token_only_reimport_preserves_refresh_and_recalculates_expiry(client, isolated_db, monkeypatch):
    exp = int(time.time()) + 7200
    aid = isolated_db.add_account('workbuddy', jwt(exp), uid='u', refresh_token='keep-me')
    monkeypatch.setattr(proxy, 'refresh_quota', Mock(return_value={}))
    monkeypatch.setattr(P, 'refresh_token', Mock(return_value=result()))
    response = client.post('/api/accounts/import_token', json={'platform': 'workbuddy', 'uid': 'u', 'access_token': jwt(exp+100)})
    assert response.get_json()['ok'] is True
    row = isolated_db.get_account(aid)
    assert row['refresh_token'] == 'keep-me'
    assert row['expires_at'] == exp + 100


def test_auto_refresh_disabled_does_not_exchange(isolated_db, monkeypatch):
    aid = isolated_db.add_account('trae', jwt(int(time.time()) - 1), refresh_token='rt')
    isolated_db.set_setting('auto_refresh', '0')
    refresh = Mock()
    monkeypatch.setattr(P, 'refresh_token', refresh)
    proxy.ensure_token(isolated_db.get_account(aid))
    refresh.assert_not_called()


@pytest.mark.parametrize('platform', ['workbuddy', 'codebuddy'])
def test_provider_accepts_nested_rotation_and_jwt_exp(monkeypatch, platform):
    exp = int(time.time()) + 7200
    response = Mock(status_code=200)
    response.json.return_value = {'code': 0, 'data': {'data': {'accessToken': jwt(exp), 'refreshToken': 'new'}}}
    session = Mock()
    session.post.return_value = response
    monkeypatch.setattr(P, '_session', lambda: session)
    out = P.refresh_token(platform, 'old', 'rt')
    assert out['access_token'] == jwt(exp)
    assert out['expires_at'] == exp
    assert out['refresh_token'] == 'new'
    headers = session.post.call_args.kwargs['headers']
    assert headers['X-Auth-Refresh-Source'] == 'plugin'
    assert 'Authorization' not in headers


def test_empty_refresh_response_rejected(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {'code': 0, 'data': {'unexpected': 'ok'}}
    session = Mock(); session.post.return_value = response
    monkeypatch.setattr(P, '_session', lambda: session)
    with pytest.raises(P.ProviderError):
        P.refresh_token('codebuddy', 'old', 'rt')


def test_failed_refresh_remains_visible_after_quota_success(isolated_db, monkeypatch):
    aid = isolated_db.add_account('workbuddy', jwt(int(time.time()) + 120), refresh_token='rt')
    monkeypatch.setattr(P, 'refresh_token', Mock(side_effect=P.ProviderError('temporary failure')))
    proxy.ensure_token(isolated_db.get_account(aid))
    monkeypatch.setattr(P, 'collect_quota', Mock(return_value={'remain': 100, 'total': 100, 'errors': []}))
    proxy.refresh_quota(isolated_db.get_account(aid))
    assert 'temporary failure' in isolated_db.get_account(aid)['refresh_error']


def test_token_only_different_session_rejected(client, isolated_db, monkeypatch):
    exp = int(time.time()) + 7200
    aid = isolated_db.add_account('workbuddy', jwt(exp, sid='session-a'), uid='u', refresh_token='session-a-refresh')
    monkeypatch.setattr(proxy, 'refresh_quota', Mock(return_value={}))
    response = client.post('/api/accounts/import_token', json={'platform': 'workbuddy', 'uid': 'u', 'access_token': jwt(exp, sid='session-b')})
    assert response.status_code == 400
    assert isolated_db.get_account(aid)['refresh_token'] == 'session-a-refresh'


def test_provider_rejects_invalid_rotated_refresh_type(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {'code': 0, 'data': {'accessToken': jwt(int(time.time())+7200), 'refreshToken': {'invalid': True}}}
    session = Mock(); session.post.return_value = response
    monkeypatch.setattr(P, '_session', lambda: session)
    with pytest.raises(P.ProviderError):
        P.refresh_token('workbuddy', 'old', 'rt')


def test_expired_refresh_failure_does_not_abort_remaining_pool(isolated_db, monkeypatch):
    db = isolated_db
    db.set_model_registry({'m': {'type': 'chat', 'platforms': ['workbuddy']}})
    first = db.add_account('workbuddy', jwt(int(time.time())-10), refresh_token='bad')
    second = db.add_account('workbuddy', jwt(int(time.time())+7200), refresh_token='good')
    monkeypatch.setattr(P, 'refresh_token', Mock(side_effect=P.ProviderError('expired refresh')))
    class Response:
        status_code = 200
        def close(self): pass
        def iter_lines(self, **kwargs):
            yield b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}'
            yield b'data: [DONE]'
    opened = []
    monkeypatch.setattr(proxy, 'open_upstream', lambda acc, body: (opened.append(acc['id']) or Response()))
    acc, response = proxy.stream_chat({'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]}, max_retry=2)
    assert acc['id'] == second
    assert opened == [second]
    response.close()


def test_concurrent_force_identical_token_response_still_singleflight(isolated_db, monkeypatch):
    exp=int(time.time())+7200
    aid=isolated_db.add_account('workbuddy', jwt(exp), refresh_token='rt')
    old=dict(isolated_db.get_account(aid))
    def exchange(*a, **k):
        time.sleep(.03)
        return {'access_token': old['access_token'], 'refresh_token':'rt', 'expires_at':exp}
    refresh=Mock(side_effect=exchange)
    monkeypatch.setattr(P,'refresh_token',refresh)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _:proxy.ensure_token(old,force=True), range(3)))
    assert refresh.call_count == 1


def test_refresh_post_is_not_automatically_replayed(monkeypatch):
    import requests
    session=P._session()
    response=Mock(status_code=200)
    response.json.return_value={'code':0,'data':{'accessToken':jwt(int(time.time())+7200)}}
    monkeypatch.setattr(session,'post',Mock(return_value=response))
    monkeypatch.setattr(P,'_session',lambda:session)
    P.refresh_token('workbuddy','old','rt')
    adapter=session.get_adapter('https://www.codebuddy.cn/v2/plugin/auth/token/refresh')
    assert adapter.max_retries.total == 0
    assert adapter.max_retries.read is False or adapter.max_retries.read == 0


def test_unknown_access_expiry_is_checked_even_when_refresh_expiry_known(isolated_db, monkeypatch):
    aid=isolated_db.add_account('workbuddy','opaque-access',refresh_token=jwt(int(time.time())+86400))
    refresh=Mock(return_value=result())
    monkeypatch.setattr(P,'refresh_token',refresh)
    proxy.ensure_token(isolated_db.get_account(aid))
    assert refresh.call_count == 1


@pytest.mark.parametrize('value', [None, '', 'bad', float('nan'), float('inf'), -1, {}, []])
def test_invalid_expiry_does_not_break_maintenance(value):
    import credentials
    assert credentials.epoch(value) == 0


def test_refresh_expiry_also_triggers_renewal(isolated_db, monkeypatch):
    aid=isolated_db.add_account('codebuddy',jwt(int(time.time())+7200),refresh_token=jwt(int(time.time())+60))
    refresh=Mock(return_value=result())
    monkeypatch.setattr(P,'refresh_token',refresh)
    proxy.ensure_token(isolated_db.get_account(aid))
    assert refresh.call_count == 1


def test_refresh_cas_keeps_concurrent_new_import(isolated_db, monkeypatch):
    db=isolated_db
    aid=db.add_account('trae',jwt(int(time.time())+20),refresh_token='old-session')
    imported=jwt(int(time.time())+14400,sid='new-session')
    def exchange(*a, **kw):
        db.update_account(aid,access_token=imported,refresh_token='new-session',expires_at=int(time.time())+14400)
        return result()
    monkeypatch.setattr(P,'refresh_token',exchange)
    acc=proxy.ensure_token(db.get_account(aid),force=True)
    assert acc['access_token'] == imported
    assert db.get_account(aid)['refresh_token'] == 'new-session'
