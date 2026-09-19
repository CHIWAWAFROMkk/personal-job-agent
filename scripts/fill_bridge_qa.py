"""Isolated dashboard OTP handoff; synthetic values only, no real website."""
import json
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from workspace_qa import fixture_server


def main():
    server = fixture_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    output = Path('build/fill-bridge-qa')
    output.mkdir(parents=True, exist_ok=True)
    report = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel='msedge')
            for width, theme in [(1440, 'aurora'), (1266, 'cosmic'), (390, 'cosmic')]:
                page = browser.new_page(viewport={'width': width, 'height': 950}, reduced_motion='reduce')
                page.set_default_timeout(10000)
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{readText:async()=> '验证码：004321，切勿泄露'}})")
                page.route('**/api/jobs/browser-use/status', lambda r: r.fulfill(json={'cdp_connected': False, 'chrome_info': {}}))
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base, wait_until='networkidle')
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
                page.locator('#workspaceNav').click()
                choice = page.locator('.job-choice').first
                job_id = int(choice.get_attribute('data-select-job'))
                choice.click()
                response = page.request.post(base + '/api/fill/session', headers={'X-Agent-Token': server.RequestHandlerClass.agent_token},
                    data={'job_id': job_id, 'url': 'https://example.com/application'})
                assert response.ok, response.text()
                session_id = response.json()['session_id']
                page.locator('[data-detail-action="browser-use-assist"]').click()
                page.locator('#smsRefreshTargets').click()
                expect(page.locator(f'#smsFillTarget option[value="{session_id}"]')).to_have_count(1)
                page.locator('#smsFillTarget').select_option(session_id)
                page.locator('#smsPasteCode').click()
                expect(page.locator('#smsManualInput')).to_have_value('004321')
                page.locator('#smsManualSubmit').click()
                expect(page.locator('#smsManualStatus')).to_contain_text('已交给所选页面')
                poll = lambda: page.request.post(base + '/api/fill/poll', headers={'X-Agent-Token': server.RequestHandlerClass.agent_token}, data={'session_id': session_id}).json()
                assert poll()['code'] == '004321'
                assert poll()['code'] is None
                page.locator('.sms-keypad-disclosure summary').click()
                for digit in '1234': page.locator(f'[data-code-key="{digit}"]').click()
                expect(page.locator('#smsManualInput')).to_have_value('1234')
                page.locator('#smsManualSubmit').scroll_into_view_if_needed()
                box = page.locator('#smsManualSubmit').bounding_box()
                assert min(box['width'], box['height']) >= 44
                page.screenshot(path=str(output / f'{width}-{theme}.png'))
                page.locator('#closeBrowserUseButton').click()
                expect(page.locator('#smsManualInput')).to_have_value('')
                assert not errors, errors
                page.request.post(base + '/api/fill/close', headers={'X-Agent-Token': server.RequestHandlerClass.agent_token}, data={'session_id': session_id})
                report.append({'width': width, 'theme': theme, 'clipboard_keypad': True, 'one_time_handoff': True, 'errors': errors})
                page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
