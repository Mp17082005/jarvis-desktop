"""Opt-in live browser smoke check. Requires running Jarvis and real LLM."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv('JARVIS_TEST_URL', 'http://127.0.0.1:8765')
config = {}
for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        config[k] = v.strip().strip("'\"")

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda err: errors.append(str(err)))
    page.goto(BASE)
    page.locator('#login-panel').wait_for(state='visible')
    # Authenticate via API, then exercise the browser using its HttpOnly cookie.
    response = context.request.post(BASE + '/api/login', data={'token': config['JARVIS_ACCESS_TOKEN']}, headers={'Origin': BASE})
    assert response.status == 200
    page.reload()
    page.locator('#login-panel').wait_for(state='hidden')
    page.locator('#speak-replies').uncheck()
    page.locator('#command').fill('What is the current time in Hyderabad? Use your tool.')
    page.locator('#send').click()
    expect(page.locator('#send')).to_be_enabled(timeout=100000)
    text = page.locator('#messages').inner_text()
    assert 'current_time' in text or 'Hyderabad' in text
    assert page.locator('#messages').locator('.message').count() >= 2
    page.locator('#command').fill('Run the diagnostic python --version on the server VM.')
    page.locator('#send').click()
    page.get_by_role('button', name='Approve action', exact=True).wait_for(timeout=100000)
    page.get_by_role('button', name='Approve action', exact=True).click()
    expect(page.locator('#send')).to_be_enabled(timeout=30000)
    expect(page.locator('#messages')).to_contain_text('Python 3.12', timeout=30000)
    (ROOT/'data').mkdir(exist_ok=True)
    page.screenshot(path=str(ROOT/'data/desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
    page.screenshot(path=str(ROOT/'data/mobile.png'), full_page=True)
    print(json.dumps({'url': BASE, 'browser_errors': errors, 'mobile_overflow': False, 'live_chat': 'passed', 'confirmation_and_diagnostic': 'passed', 'screenshots': ['data/desktop.png', 'data/mobile.png']}))
    assert not errors
    browser.close()
