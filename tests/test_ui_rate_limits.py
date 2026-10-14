"""真实浏览器验收冷却卡片、设置与逐次调用日志。"""
import proxy
from playwright.sync_api import expect
from test_ui_account_parity import ui
from test_rate_limit_failover import upstream, add_account

REAL_OPEN_UPSTREAM = proxy.open_upstream


def test_cooldown_settings_card_and_attempt_log(ui, upstream, monkeypatch):
    page, url, db, _ = ui
    monkeypatch.setattr(proxy, 'open_upstream', REAL_OPEN_UPSTREAM)
    first, second = [add_account(db, 'workbuddy_intl') for _ in range(2)]
    db.set_model_registry({'model': {'type':'chat', 'platforms':['workbuddy_intl']}})
    upstream.behavior[first] = (429, {'code':6004,'msg':'fixture frequency limit'}, {'Retry-After':'180'})
    r = page.context.request.post(url + '/v1/chat/completions', headers={'Authorization':'Bearer '+db.DEFAULT_API_KEY}, data={
        'model':'model', 'messages':[{'role':'user','content':'hi'}]})
    assert r.status == 200
    page.goto(url + '/accounts')
    expect(page.get_by_text('限流冷却', exact=True)).to_be_visible()
    expect(page.get_by_label('模型限流').get_by_text('WorkBuddy 国际 · 该服务全部模型', exact=True)).to_be_visible()
    expect(page.get_by_label('模型限流').get_by_text('fixture frequency limit', exact=False)).to_be_visible()
    page.goto(url + '/logs')
    page.get_by_text('2 次尝试', exact=False).click()
    expect(page.get_by_text('费用未知', exact=False).first).to_be_visible()
    expect(page.get_by_text('rate_limit', exact=False)).to_be_visible()
    page.goto(url + '/settings')
    control = page.get_by_role('spinbutton', name='限流默认冷却（秒）')
    expect(control).to_have_value('60')
    control.fill('125')
    page.get_by_role('button', name='保存设置', exact=True).click()
    expect(page.get_by_text('设置已保存', exact=True)).to_be_visible()
    page.reload()
    expect(control).to_have_value('125')
    assert [c[0] for c in upstream.calls] == [first, second]
