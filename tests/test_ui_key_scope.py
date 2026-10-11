"""真实管理页面：模型页开关联动、Key新建编辑、失效选择与错误恢复。"""
import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_ui_account_parity import ui
from test_key_model_scope import seed_catalog


def choose_channel(page, form, label):
    form.get_by_role('combobox', name='绑定渠道').click()
    page.get_by_role('option', name=label, exact=True).click()


def test_key_form_obeys_model_page_switch_with_real_mouse_and_keyboard(ui):
    page, url, db, state = ui
    seed_catalog(db)
    page.goto(url + '/models')
    # 同ID在其他渠道仍开启；WorkBuddy绑定仍应灰显。
    with page.expect_response(lambda r: r.url.endswith('/api/settings') and r.request.method == 'POST'):
        page.get_by_role('switch', name='开关 Chat B', exact=True).first.click()
    expect(page.get_by_role('switch', name='开关 Chat B', exact=True).first).not_to_be_checked()
    page.goto(url + '/keys')
    form = page.get_by_role('form', name='新建调用密钥')
    form.get_by_role('radio', name='指定模型', exact=True).check()
    choose_channel(page, form, 'WorkBuddy')
    disabled = form.get_by_role('checkbox', name='选择 chat-b', exact=True)
    expect(disabled).to_be_disabled()
    expect(disabled).not_to_be_checked()
    form.get_by_text('Chat B', exact=True).click(force=True)  # 点击标签同样不改勾选
    expect(disabled).not_to_be_checked()
    active = form.get_by_role('checkbox', name='选择 chat-a', exact=True)
    expect(active).to_be_enabled()
    active.focus()
    active.press('Space')
    expect(active).to_be_checked()
    active.press('Tab')
    expect(form.get_by_role('checkbox', name='选择 image-a', exact=True)).to_be_focused()
    expect(disabled).not_to_be_checked()
    form.get_by_label('名称', exact=True).fill('指定模型浏览器测试')
    form.get_by_label('自定义 Key（留空自动生成）', exact=True).fill('ui-selected-test-key')
    form.get_by_role('button', name='创建', exact=True).click()
    expect(page.get_by_text('指定模型浏览器测试', exact=True)).to_be_visible()
    rows = page.context.request.get(url + '/api/keys').json()
    row = next(k for k in rows if k['api_key'] == 'ui-selected-test-key')
    assert row['allowed_models'] == ['chat-a'] and row['platform'] == 'workbuddy'
    public = page.context.request.get(url + '/v1/models', headers={'Authorization': 'Bearer ui-selected-test-key'}).json()
    assert [m['id'] for m in public['data']] == ['chat-a']
    assert state['probed'] == []


def add_key(ui, name='待编辑', models=None, platform='workbuddy'):
    page, url, db, state = ui
    payload = {'name': name, 'api_key': 'ui-edit-test-key', 'platform': platform,
               'model_scope': 'selected', 'allowed_models': models or ['chat-a']}
    response = page.context.request.post(url + '/api/keys', data=payload)
    assert response.status == 200
    return next(k for k in page.context.request.get(url + '/api/keys').json() if k['api_key'] == payload['api_key'])


def edit_form(page, name):
    page.get_by_role('group', name='密钥 ' + name, exact=True).get_by_role('button', name='编辑', exact=True).click()
    return page.get_by_role('form', name='编辑调用密钥')


def test_full_pool_multiselect_search_and_edit_survive_reload(ui):
    page, url, db, state = ui
    seed_catalog(db)
    db.set_setting('model_platform_disabled', json.dumps({'workbuddy': ['chat-b']}))
    page.goto(url + '/keys')
    form = page.get_by_role('form', name='新建调用密钥')
    form.get_by_role('radio', name='指定模型', exact=True).check()
    expect(form.get_by_role('checkbox', name='选择 chat-a', exact=True)).to_have_count(1)
    form.get_by_role('checkbox', name='选择 chat-a', exact=True).check()
    form.get_by_role('checkbox', name='选择 chat-b', exact=True).check()  # CodeBuddy仍开启，全池可选
    form.get_by_label('名称', exact=True).fill('全池多选')
    form.get_by_label('自定义 Key（留空自动生成）', exact=True).fill('ui-pool-test-key')
    form.get_by_role('button', name='创建', exact=True).click()
    card = page.get_by_role('group', name='密钥 全池多选', exact=True)
    expect(card).to_contain_text('指定 2 个模型')
    before = next(k for k in page.context.request.get(url + '/api/keys').json() if k['api_key'] == 'ui-pool-test-key')
    form = edit_form(page, '全池多选')
    expect(form.get_by_role('checkbox', name='选择 chat-a', exact=True)).to_be_checked()
    choose_channel(page, form, 'WorkBuddy 国际')
    expect(form.get_by_text('chat-b · 停用：当前渠道未收录', exact=True)).to_be_visible()
    expect(form.get_by_role('button', name='保存权限', exact=True)).to_be_disabled()
    form.get_by_role('button', name='移除 chat-b', exact=True).click()
    form.get_by_label('搜索模型名称或 ID', exact=True).fill('intl')
    expect(form.get_by_role('checkbox')).to_have_count(1)
    form.get_by_role('checkbox', name='选择 intl-only', exact=True).check()
    form.get_by_role('button', name='保存权限', exact=True).click()
    expect(page.get_by_role('dialog')).not_to_be_visible()
    page.reload()
    form = edit_form(page, '全池多选')
    expect(form.get_by_role('combobox', name='绑定渠道')).to_have_text('WorkBuddy 国际')
    expect(form.get_by_role('checkbox', name='选择 chat-a', exact=True)).to_be_checked()
    expect(form.get_by_role('checkbox', name='选择 intl-only', exact=True)).to_be_checked()
    after = next(k for k in page.context.request.get(url + '/api/keys').json() if k['api_key'] == 'ui-pool-test-key')
    assert after['allowed_models'] == ['chat-a', 'intl-only']
    for field in ('id', 'api_key', 'req_count', 'last_used', 'created_at'):
        assert after[field] == before[field]
    assert state['probed'] == []


def test_stale_form_fails_then_historical_disabled_tag_can_restore_or_be_removed(ui):
    page, url, db, state = ui
    seed_catalog(db)
    original = add_key(ui)
    page.goto(url + '/keys')
    form = edit_form(page, '待编辑')
    selected = form.get_by_role('checkbox', name='选择 chat-a', exact=True)
    expect(selected).to_be_enabled()
    disabled = {'model_platform_disabled': json.dumps({'workbuddy': ['chat-a']})}
    page.context.request.post(url + '/api/settings', data=disabled)
    form.get_by_role('button', name='保存权限', exact=True).click()
    expect(form.get_by_text('以下模型在所选渠道未开启或未收录：chat-a', exact=True)).to_be_visible()
    expect(selected).to_be_disabled()
    expect(selected).to_be_checked()
    expect(form.get_by_text('chat-a · 停用：模型页已关闭', exact=True)).to_be_visible()
    now = next(k for k in page.context.request.get(url + '/api/keys').json() if k['id'] == original['id'])
    assert now == original
    # 全局重新开启直接恢复原ID；未新增授权。
    page.context.request.post(url + '/api/settings', data={'model_platform_disabled': '{}'})
    form.get_by_role('button', name='刷新可选模型', exact=True).click()
    expect(selected).to_be_enabled()
    expect(selected).to_be_checked()
    page.context.request.post(url + '/api/settings', data=disabled)
    form.get_by_role('button', name='刷新可选模型', exact=True).click()
    expect(form.get_by_text('chat-a · 停用：模型页已关闭', exact=True)).to_be_visible()
    form.get_by_role('button', name='移除 chat-a', exact=True).click()
    expect(selected).not_to_be_checked()
    expect(form.get_by_role('button', name='保存权限', exact=True)).to_be_disabled()
    form.get_by_role('checkbox', name='选择 chat-b', exact=True).check()
    form.get_by_role('button', name='保存权限', exact=True).click()
    expect(page.get_by_role('dialog')).not_to_be_visible()
    saved = next(k for k in page.context.request.get(url + '/api/keys').json() if k['id'] == original['id'])
    assert saved['allowed_models'] == ['chat-b']
    assert saved['api_key'] == original['api_key']
    assert state['probed'] == []


def test_catalog_load_failure_preserves_selection_and_blocks_save_until_retry(ui):
    page, url, db, state = ui
    seed_catalog(db)
    original = add_key(ui)
    page.route('**/api/models/discover/progress', lambda route: route.fulfill(
        status=503, content_type='application/json', body=json.dumps({'error': 'fixture catalog offline'})))
    page.goto(url + '/keys')
    form = edit_form(page, '待编辑')
    expect(form.get_by_role('alert')).to_contain_text('fixture catalog offline')
    expect(form.get_by_role('button', name='移除 chat-a', exact=True)).to_be_visible()
    expect(form.get_by_role('button', name='保存权限', exact=True)).to_be_disabled()
    choose_channel(page, form, 'TraeWork')
    expect(form.get_by_role('alert')).to_contain_text('fixture catalog offline')
    page.unroute('**/api/models/discover/progress')
    form.get_by_role('button', name='刷新可选模型', exact=True).click()
    expect(form.get_by_role('checkbox', name='选择 chat-a', exact=True)).to_be_enabled()
    expect(form.get_by_role('checkbox', name='选择 chat-a', exact=True)).to_be_checked()
    form.get_by_role('button', name='保存权限', exact=True).click()
    expect(page.get_by_role('dialog')).not_to_be_visible()
    saved = next(k for k in page.context.request.get(url + '/api/keys').json() if k['id'] == original['id'])
    assert saved['allowed_models'] == ['chat-a'] and saved['platform'] == 'trae'
    assert state['probed'] == []


def test_save_failure_keeps_form_values_and_original_key_permissions(ui):
    page, url, db, state = ui
    seed_catalog(db)
    original = add_key(ui)
    page.goto(url + '/keys')
    form = edit_form(page, '待编辑')
    form.get_by_role('checkbox', name='选择 chat-b', exact=True).check()
    form.get_by_role('checkbox', name='选择 chat-a', exact=True).uncheck()
    endpoint = '**/api/keys/' + str(original['id'])
    page.route(endpoint, lambda route: route.fulfill(status=503, content_type='application/json',
                                                     body=json.dumps({'error': 'fixture save offline'})))
    form.get_by_role('button', name='保存权限', exact=True).click()
    expect(form.get_by_role('alert')).to_contain_text('fixture save offline')
    expect(form.get_by_role('checkbox', name='选择 chat-b', exact=True)).to_be_checked()
    expect(form.get_by_role('checkbox', name='选择 chat-a', exact=True)).not_to_be_checked()
    saved = next(k for k in page.context.request.get(url + '/api/keys').json() if k['id'] == original['id'])
    assert saved == original
    page.unroute(endpoint)
    form.get_by_role('button', name='保存权限', exact=True).click()
    expect(page.get_by_role('dialog')).not_to_be_visible()
    saved = next(k for k in page.context.request.get(url + '/api/keys').json() if k['id'] == original['id'])
    assert saved['allowed_models'] == ['chat-b']
    assert state['probed'] == []


def test_mode_switch_clears_old_selection_and_key_toggle_refreshes_card(ui):
    page, url, db, state = ui
    seed_catalog(db)
    original = add_key(ui)
    page.goto(url + '/keys')
    form = edit_form(page, '待编辑')
    form.get_by_role('radio', name='不限模型', exact=True).check()
    form.get_by_role('radio', name='指定模型', exact=True).check()
    expect(form.get_by_role('checkbox', name='选择 chat-a', exact=True)).not_to_be_checked()
    expect(form.get_by_role('button', name='保存权限', exact=True)).to_be_disabled()
    form.get_by_role('radio', name='不限模型', exact=True).check()
    form.get_by_role('button', name='保存权限', exact=True).click()
    expect(page.get_by_role('dialog')).not_to_be_visible()
    card = page.get_by_role('group', name='密钥 待编辑', exact=True)
    expect(card).to_contain_text('不限模型')
    card.get_by_role('switch').click()
    expect(card.get_by_role('switch')).not_to_be_checked()
    expect(card).to_contain_text('已停用')
    saved = next(k for k in page.context.request.get(url + '/api/keys').json() if k['id'] == original['id'])
    assert saved['allowed_models'] == [] and saved['model_scope'] == 'all' and saved['enabled'] == 0
    assert state['probed'] == []
