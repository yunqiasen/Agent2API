"""账号调用开关不影响签到及凭证维护。"""
import pytest
import proxy


@pytest.mark.parametrize('runner', ['checkin_all', 'retry_failed_checkins'])
def test_disabled_account_still_checks_in_but_is_not_routed(isolated_db, monkeypatch, runner):
    db = isolated_db
    aid = db.add_account('workbuddy', 'token', uid='disabled-user')
    db.update_account(aid, enabled=0)
    seen = []
    def checkin(row):
        seen.append(row['id'])
        return {'claimed_credit': 100}
    monkeypatch.setattr(proxy, 'checkin_account', checkin)
    getattr(proxy, runner)()
    assert seen == [aid]
    assert proxy.choose_account(platform='workbuddy') is None


def test_disabled_account_token_is_maintained(isolated_db, monkeypatch):
    import app
    db = isolated_db
    aid = db.add_account('workbuddy', 'token', uid='disabled-user')
    db.update_account(aid, enabled=0)
    seen = []
    sleeps = []
    def sleep(_):
        if sleeps:
            raise KeyboardInterrupt()
        sleeps.append(True)
    monkeypatch.setattr(app.time, 'sleep', sleep)
    monkeypatch.setattr(proxy, 'ensure_token', lambda row: seen.append(row['id']))
    monkeypatch.setattr(proxy, 'run_auto_checkin_if_due', lambda: None)
    monkeypatch.setattr(proxy, 'retry_failed_checkins', lambda: None)
    with pytest.raises(KeyboardInterrupt):
        app._maintenance_loop()
    assert seen == [aid]
