"""国际维护界面：运行真实管理应用，隔离账号和上游。"""
from unittest.mock import Mock

import pytest
from test_ui_account_parity import ui, account
from playwright.sync_api import expect


def test_refresh_error_exposes_retry_time_on_card(ui, monkeypatch):
    import requests
    page, url, db, _ = ui
    aid = account(db)
    db.update_account(aid, refresh_token='fixture-refresh')
    response = Mock(status_code=503)
    response.json.return_value = {'code': 503, 'msg': 'temporary refresh problem'}
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda *_a, **_kw: response)
    r = page.context.request.post(f'{url}/api/accounts/{aid}/refresh', data={})
    assert r.status == 500
    page.goto(url + '/accounts')
    expect(page.get_by_text('下次重试', exact=False)).to_be_visible()
    expect(page.get_by_text('失败：', exact=False)).to_contain_text('temporary refresh problem')

from test_international_rewards import upstream


def test_international_checkin_card_shows_pending_not_credit_success(ui, upstream):
    page, url, db, _ = ui
    aid = account(db)
    page.goto(url + '/accounts')
    page.get_by_role('button', name='签到', exact=True).click()
    expect(page.get_by_text('活动已确认，奖励待到账', exact=True).first).to_be_visible()
    expect(page.get_by_text('签到成功', exact=True)).to_have_count(0)
    expect(page.get_by_text('Asia/Shanghai', exact=False).first).to_be_visible()
    page.reload()
    expect(page.get_by_text('活动已确认，奖励待到账', exact=True).first).to_be_visible()
    page.get_by_role('button', name='全部签到', exact=False).click()
    expect(page.get_by_text('国际待到账 1', exact=False)).to_be_visible()
    assert upstream.reported == ['existing']


def test_international_pool_summary_does_not_claim_zero_daily_credit(ui, upstream):
    page, url, db, _ = ui
    account(db)
    page.goto(url + '/accounts')
    expect(page.get_by_text('国际每日奖励', exact=True)).to_be_visible()
    label = page.get_by_text('见账号卡片', exact=True)
    expect(label).to_be_visible()
    assert label.evaluate('el => { const range = document.createRange(); range.selectNodeContents(el); return new Set([...range.getClientRects()].map(r => Math.round(r.top))).size; }') == 1
    expect(page.get_by_text('今日可领积分', exact=True)).to_have_count(0)
