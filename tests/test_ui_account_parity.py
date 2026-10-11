"""真实浏览器 + 临时 SQLite + 可控上游；不使用正式账号。
先构建 frontend/dist；pip install playwright && playwright install chromium。
可用 AGENT2API_TEST_CHROMIUM 指定已有 Chromium。
"""
import json
import os
import re
from pathlib import Path
import threading
import time
from unittest.mock import Mock

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect


@pytest.fixture
def ui(isolated_db, monkeypatch, request):
    from werkzeug.serving import make_server
    import app
    import providers as P
    import proxy
    db = isolated_db
    db.set_setting("admin_password", db.hash_pw("test-ui-password"))
    app.app.secret_key = "test-ui-secret"
    app.app.config["TESTING"] = True
    state = {"models": ["new-model"], "error": "", "probed": []}
    def catalog(*args, **kwargs):
        if state["error"]:
            raise P.ProviderError(state["error"])
        return state["models"]
    monkeypatch.setattr(P, "fetch_upstream_models", catalog)
    monkeypatch.setattr(proxy, "refresh_quota", Mock(return_value={"remain": 100}))
    monkeypatch.setattr(P, "start_login", Mock(return_value={"state": "ui-state", "auth_url": "https://example.test/login"}))
    monkeypatch.setattr(P, "poll_token", Mock(return_value=None))
    class Resp:
        status_code = 200
        def iter_lines(self, **kwargs):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"pong"},"finish_reason":"stop"}]}'
            yield b'data: [DONE]'
        def close(self): pass
    def opened(account, body, **kwargs):
        state["probed"].append((account["id"], body["model"]))
        return Resp()
    monkeypatch.setattr(proxy, "open_upstream", opened)
    server = make_server("127.0.0.1", 0, app.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    errors = []
    with playwright.sync_playwright() as p:
        executable = os.environ.get("AGENT2API_TEST_CHROMIUM")
        browser = p.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(7000)
        page.on("pageerror", lambda e: errors.append(str(e)))
        context.request.post(url + "/api/login", data={"password": "test-ui-password"})
        try:
            yield page, url, db, state
            assert errors == []
        finally:
            if os.environ.get("AGENT2API_UI_ARTIFACTS"):
                directory = Path(os.environ["AGENT2API_UI_ARTIFACTS"])
                directory.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(directory / (request.node.name + ".png")), full_page=True, animations="disabled")
            browser.close()
            server.shutdown()
            thread.join(2)


def account(db):
    aid = db.add_account("workbuddy_intl", "fixture-token", uid="existing", nickname="Existing account")
    db.update_account(aid, expires_at=int(time.time()) + 7200)
    db.set_model_registry({"old-model": {"type": "chat", "platforms": ["workbuddy_intl"]}})
    return aid


def test_import_refreshes_existing_card_catalog_and_probe(ui):
    page, url, db, state = ui
    account(db)
    page.goto(url + "/accounts")
    expect(page.locator('select option[value="old-model"]')).to_have_count(1)
    page.get_by_role("button", name="导入 Token", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("combobox").click()
    page.get_by_role("option", name="WorkBuddy 国际", exact=True).click()
    dialog.locator("textarea").fill(json.dumps({"uid": "second-user", "access_token": "new-token", "expires_at": int(time.time()) + 7200}))
    dialog.get_by_role("button", name="导入", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.locator("select")).to_have_count(2)
    expect(page.locator('select option[value="new-model"]')).to_have_count(2)
    page.locator("select").first.select_option("new-model")
    page.get_by_role("button", name="测活", exact=True).first.click()
    expect(page.get_by_text(re.compile(r"^通过 ·.*pong"))).to_be_visible()
    assert state["probed"][0][1] == "new-model"


def test_card_model_retry_shows_error_and_preserves_selection(ui):
    page, url, db, state = ui
    account(db)
    page.goto(url + "/accounts")
    expect(page.locator('select option[value="old-model"]')).to_have_count(1)
    assert page.get_by_role("button", name="刷新模型", exact=True).count() == 1
    state["error"] = "HTTP 503 catalog service unavailable"
    page.get_by_role("button", name="刷新模型", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("503")
    expect(page.locator("select")).to_have_value("old-model")
    state["error"] = ""
    page.get_by_role("button", name="刷新模型", exact=True).click()
    expect(page.locator('select option[value="new-model"]')).to_have_count(1)
    expect(page.locator("select")).to_have_value("old-model")
    expect(page.get_by_role("alert")).to_have_count(0)


def test_login_terminal_error_has_explicit_retry(ui, monkeypatch):
    page, url, db, state = ui
    import providers as P
    monkeypatch.setattr(P, "poll_token", Mock(side_effect=P.LoginError("login state expired 11218")))
    page.goto(url + "/accounts")
    page.get_by_role("button", name="添加账号", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="获取授权链接", exact=True).click()
    expect(dialog.locator("pre")).to_contain_text("11218")
    expect(dialog.get_by_role("button", name="重新获取授权链接", exact=True)).to_be_visible()


def test_public_join_done_enters_completion_screen(ui):
    page, url, db, state = ui
    page.goto(url + "/join")
    # 页面实际按钮文字由既有 UI 提供，后续终态必须退出二维码等待页。
    page.get_by_role("button", name="获取登陆链接", exact=True).click()
    expect(page.get_by_role("heading", name="完成授权", exact=True)).to_be_visible()
    db.finish_pending("ui-state")
    expect(page.get_by_role("heading", name="已加入共享号池", exact=True)).to_be_visible()


def test_public_join_quota_failure_is_unknown_not_zero(ui, monkeypatch):
    page, url, db, state = ui
    import providers as P
    import proxy
    monkeypatch.setattr(P, "poll_token", Mock(return_value={"access_token": "fixture", "expires_at": int(time.time())+7200}))
    monkeypatch.setattr(P, "fetch_account_info", Mock(return_value={"uid": "joined"}))
    monkeypatch.setattr(proxy, "refresh_quota", Mock(side_effect=RuntimeError("HTTP 503 quota unavailable")))
    page.goto(url + "/join")
    page.get_by_role("button", name="获取登陆链接", exact=True).click()
    expect(page.get_by_role("heading", name="已加入共享号池", exact=True)).to_be_visible()
    expect(page.get_by_text("额度尚未同步", exact=True)).to_be_visible()


def test_model_page_refresh_reports_failure_without_generation(ui, monkeypatch):
    import discovery
    page, url, db, state = ui
    account(db)
    probe = Mock(side_effect=AssertionError('directory refresh must not generate'))
    monkeypatch.setattr(discovery, 'run_discovery', probe)
    state['error'] = 'HTTP 503 catalog unavailable'
    page.goto(url + '/models')
    page.get_by_role('button', name='刷新模型目录', exact=True).click()
    expect(page.get_by_text(re.compile('目录部分更新：.*503'))).to_be_visible()
    assert 'old-model' in db.get_model_registry()
    state['error'] = ''
    page.get_by_role('button', name='刷新模型目录', exact=True).click()
    expect(page.get_by_text(re.compile('官方目录已刷新.*未执行模型测试'))).to_be_visible()
    assert 'new-model' in db.get_model_registry()
    probe.assert_not_called()


def test_model_page_shows_channel_prices_and_source_without_public_price_fields(ui, monkeypatch):
    page, url, db, state = ui
    import providers as P
    aid = account(db)
    def catalog(*args, **kwargs):
        kwargs['metadata'].update(source='https://www.workbuddy.ai/v3/config', credits={
            'free-model': 'x0.00', 'paid-model': 'x0.03', 'unknown-model': ''})
        return ['free-model', 'paid-model', 'unknown-model']
    monkeypatch.setattr(P, 'fetch_upstream_models', catalog)
    page.context.request.get(url + '/api/models/discover')
    page.goto(url + '/models')
    expect(page.get_by_role('button', name='Free Model 倍率说明')).to_have_text('免费')
    expect(page.get_by_role('button', name='Paid Model 倍率说明')).to_have_text('×0.03')
    expect(page.get_by_role('button', name='Unknown Model 倍率说明')).to_have_text('未公布')
    page.get_by_role('button', name='Paid Model 倍率说明').click()
    expect(page.get_by_role('dialog')).to_contain_text('https://www.workbuddy.ai/v3/config')
    expect(page.get_by_role('dialog')).to_contain_text('x0.03')
    assert state['probed'] == []
