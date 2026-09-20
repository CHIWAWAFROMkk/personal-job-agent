"""Synthetic browser checks for private previews and polling teardown."""
import json
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from workspace_qa import fixture_server


def main():
    output = Path('build/security-ui')
    output.mkdir(parents=True, exist_ok=True)
    server = fixture_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge')
            for width in (1266, 390):
                page = browser.new_page(viewport={'width':width, 'height':850}, reduced_motion='reduce')
                page.set_default_timeout(10000)
                errors = []
                network_failures = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('requestfailed', lambda r: network_failures.append({'path':r.url.split('?')[0], 'failure':r.failure}))
                page.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.clipboardWasWritten=Boolean(value);}}});")
                calls = []
                def sms(route):
                    calls.append(1)
                    route.fulfill(json={'is_listening':True,'webhook_url':'http://127.0.0.1:8088/api/sms/webhook?token=synthetic-not-a-secret', 'latest_code':{'code':'123456','age_seconds':0}})
                page.route('**/api/sms/setup', sms)
                page.route('**/api/jobs/browser-use/status', lambda route: route.fulfill(json={'cdp_connected':False,'chrome_info':{}}))
                page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                if width <= 900:
                    page.locator('#openDrawerButton').click()
                    page.locator('[data-drawer-nav="workspace"]').click()
                else:
                    page.locator('#workspaceNav').click()
                page.locator('.job-choice').first.click()
                button = page.locator('[data-detail-action="browser-use-assist"]')
                if not button.is_visible():
                    page.locator('.job-more summary').click()
                button.click()
                expect(page.locator('#buFactsStatus')).to_contain_text('来自当前档案')
                expect(page.locator('#buFactName')).not_to_have_text('待完善')
                expect(page.locator('#smsCurrentBadge')).to_have_text('已收到')
                expect(page.locator('#buFactGpa')).to_have_text('待完善')
                size = page.locator('#closeBrowserUseButton').bounding_box()
                assert size['width'] >= 44 and size['height'] >= 44, {'size':size,'network':network_failures}
                page.screenshot(path=str(output/f'{width}-preview.png'))
                initial_calls = len(calls)
                page.wait_for_timeout(2400)
                assert len(calls) == initial_calls, 'Polling did not slow after receiving a code'
                page.locator('#closeBrowserUseButton').click()
                page.wait_for_timeout(10200)
                assert len(calls) == initial_calls
                assert page.locator('#smsWebhookUrlDisplay').inner_text() == '打开弹窗后读取安全链接'
                page.locator('#geminiProfileAvatarBtn').click()
                page.locator('#copyAgentTokenButton').click()
                expect(page.locator('#copyAgentTokenStatus')).to_contain_text('已复制')
                assert page.evaluate('window.clipboardWasWritten')
                page.screenshot(path=str(output/f'{width}-settings.png'))
                assert not errors, errors
                report[str(width)] = {'page_errors':errors, 'close_size':size, 'copy_pass':True, 'polling_pass':True}
                page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__':
    main()
