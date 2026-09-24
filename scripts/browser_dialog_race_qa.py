"""Synthetic delayed-response regression. No external site or AI is invoked."""
import json
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from workspace_qa import fixture_server


def main():
    server = fixture_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    output = Path('build/browser-dialog-race-qa')
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page(viewport={'width': 1266, 'height': 900})
            page.set_default_timeout(10000)
            errors, profile_wait, calls, status_calls = [], [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            mode = {'profile_hold': True}

            def profile(route):
                if mode['profile_hold']:
                    profile_wait.append(route)
                else:
                    route.fulfill(json={'facts': {'name': 'Synthetic current profile'}})

            def status(route):
                status_calls.append(route.request.url)
                route.fulfill(json={'live_available': False})

            def execute(route):
                calls.append(route.request.url)
                assert route.request.post_data_json['dry_run'] is True
                route.fulfill(json={'result': {'summary': 'Synthetic preview only'}})

            page.route('**/api/profile', profile)
            page.route('**/api/jobs/browser-use/status', status)
            page.route('**/api/jobs/*/browser-use', execute)
            page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
            page.locator('#workspaceNav').click()
            choices = page.locator('.job-choice')
            a, b = [choices.nth(i).get_attribute('data-select-job') for i in (0, 1)]

            def open_job(job):
                page.locator(f'[data-select-job="{job}"]').click()
                button = page.locator('[data-detail-action="browser-use-assist"]')
                if not button.is_visible():
                    page.locator('.job-more summary').click()
                button.click()

            open_job(a)
            expect(page.locator('#browserUseRunBtn')).to_be_disabled()
            page.locator('#closeBrowserUseButton').click()
            expect(page.locator('#browserUseDialog')).not_to_be_visible()
            mode['profile_hold'] = False
            open_job(b)
            expect(page.locator('#browserUseDryRunBtn')).to_be_enabled()
            assert len(profile_wait) == 1
            profile_wait.pop().fulfill(json={'facts': {'name': 'STALE PROFILE'}})
            page.locator('#browserUseDryRunBtn').click()
            expect(page.locator('#browserUseLogContent')).to_contain_text('Synthetic preview')
            expect(page.locator('#buFactName')).to_have_text('Synthetic current profile')
            assert calls[-1].endswith(f'/{b}/browser-use')
            page.locator('#closeBrowserUseButton').click()

            open_job(a)
            expect(page.locator('#buFactsStatus')).to_contain_text('来自当前档案')
            expect(page.locator('#browserUseRunBtn')).to_be_disabled()
            page.locator('#closeBrowserUseButton').click()
            open_job(b)
            expect(page.locator('#browserUseDryRunBtn')).to_be_enabled()
            page.locator('#browserUseDryRunBtn').click()
            expect(page.locator('#browserUseLogContent')).to_contain_text('Synthetic preview')
            expect(page.locator('#browserUseCdpStatus')).to_contain_text('实站 AI 代填已暂停')
            expect(page.locator('#browserUseRunBtn')).to_be_disabled()
            assert len(calls) == 2 and all(url.endswith(f'/{b}/browser-use') for url in calls)
            assert not status_calls, status_calls
            assert not errors, errors
            page.screenshot(path=str(output / 'current-dialog.png'))
            browser.close()
        report = {'slow_profile_close': True, 'late_profile_ignored': True, 'live_entry_disabled': True, 'only_current_job_mock_preview': True, 'errors': errors}
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
