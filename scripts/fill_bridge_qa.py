"""Isolated ordinary fill session and profile preview; synthetic data only."""
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
                sms_requests = []
                page.on('request', lambda request: sms_requests.append(request.url)
                        if '/api/sms' in request.url else None)
                page.route('**/api/jobs/browser-use/status', lambda r: r.fulfill(json={'cdp_connected': False, 'chrome_info': {}}))
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base, wait_until='networkidle')
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
                if width <= 900:
                    page.locator('#openDrawerButton').click()
                    page.locator('[data-drawer-nav="workspace"]').click()
                else:
                    page.locator('#workspaceNav').click()
                choice = page.locator('.job-choice').first
                job_id = int(choice.get_attribute('data-select-job'))
                choice.click()
                response = page.request.post(base + '/api/fill/session', headers={'X-Agent-Token': server.RequestHandlerClass.agent_token},
                    data={'job_id': job_id, 'url': 'https://example.com/application'})
                assert response.ok, response.text()
                session_id = response.json()['session_id']
                assert response.json()['job_id'] == job_id
                assert response.json()['origin'] == 'https://example.com'
                headers = {'X-Agent-Token': server.RequestHandlerClass.agent_token}
                fill_data = page.request.get(base + f'/api/jobs/{job_id}/fill-data', headers=headers)
                assert fill_data.ok, fill_data.text()
                assert fill_data.json()['fields']['name']
                button = page.locator('[data-detail-action="browser-use-assist"]')
                if not button.is_visible():
                    page.locator('.job-more summary').click()
                button.click()
                expect(page.locator('#browserUseDialog')).to_be_visible()
                expect(page.locator('#buFactsStatus')).to_contain_text('来自当前档案')
                expect(page.locator('#buFactName')).to_have_text(fill_data.json()['fields']['name'])
                expect(page.locator('#browserUseSmsSection, [id^="sms"]')).to_have_count(0)
                box = page.locator('#closeBrowserUseButton').bounding_box()
                assert min(box['width'], box['height']) >= 44
                page.screenshot(path=str(output / f'{width}-{theme}.png'))
                page.locator('#closeBrowserUseButton').click()
                expect(page.locator('#browserUseDialog')).not_to_be_visible()
                assert not errors, errors
                assert not sms_requests, sms_requests
                closed = page.request.post(base + '/api/fill/close', headers=headers, data={'session_id': session_id})
                assert closed.ok and closed.json()['ok'], closed.text()
                assert session_id not in server.fill_bridge.sessions
                report.append({'width': width, 'theme': theme, 'profile_preview': True, 'session_closed': True, 'no_sms_requests': True, 'errors': errors})
                page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
