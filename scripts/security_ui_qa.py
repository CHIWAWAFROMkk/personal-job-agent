"""Synthetic browser checks for private previews and removed SMS UI."""
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
                sms_requests = []
                page.on('request', lambda request: sms_requests.append(request.url)
                        if '/api/sms' in request.url else None)
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
                expect(page.locator('#browserUseDialog')).to_be_visible()
                expect(page.locator('#browserUseSmsSection, [id^="sms"]')).to_have_count(0)
                expect(page.locator('#buFactGpa')).to_have_text('待完善')
                size = page.locator('#closeBrowserUseButton').bounding_box()
                assert size['width'] >= 44 and size['height'] >= 44, {'size':size,'network':network_failures}
                page.screenshot(path=str(output/f'{width}-preview.png'))
                page.locator('#closeBrowserUseButton').click()
                expect(page.locator('#browserUseDialog')).not_to_be_visible()
                page.locator('#geminiProfileAvatarBtn').click()
                page.locator('#copyAgentTokenButton').click()
                expect(page.locator('#copyAgentTokenStatus')).to_contain_text('已复制')
                assert page.evaluate('window.clipboardWasWritten')
                page.screenshot(path=str(output/f'{width}-settings.png'))
                assert not errors, errors
                assert not sms_requests, sms_requests
                report[str(width)] = {'page_errors':errors, 'close_size':size, 'copy_pass':True, 'no_sms_requests':True}
                page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__':
    main()
