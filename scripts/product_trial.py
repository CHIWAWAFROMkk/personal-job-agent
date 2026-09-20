"""Readiness trial against the installed EXE using an isolated empty workspace."""
import json
import subprocess
import time
import sys
import argparse
import threading
from pathlib import Path
import psutil
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build' / ('product-trial-' + time.strftime('%Y%m%d-%H%M%S'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, help='Omit to test the source HTTP application')
    parser.add_argument('--seed', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True)
    if args.seed:
        from tests.helpers import sample_profile
        from job_agent.services.profile_store import save_profile
        save_profile(sample_profile(), OUT / 'empty-user/data/private/profile.json')
    proc = None
    server = None
    if args.exe:
        proc = subprocess.Popen([str(args.exe), '--data-dir', str(OUT / 'empty-user')])
    else:
        from job_agent.services.dashboard import create_dashboard_server
        from job_agent.services.job_repository import JobRepository
        private = OUT / 'empty-user/data/private'
        server = create_dashboard_server(JobRepository(private / 'jobs.sqlite3'),
            private_dir=private, profile_path=private / 'profile.json', output_dir=OUT / 'output', port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    report = []
    try:
        port = server.server_port if server else None
        deadline = time.monotonic() + 40
        while not port and time.monotonic() < deadline:
            ports = [c.laddr.port for c in psutil.Process(proc.pid).net_connections() if c.status == 'LISTEN' and c.laddr.ip == '127.0.0.1']
            if ports:
                port = ports[0]
                break
            time.sleep(.3)
        assert port, 'No installed server listening'
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel='msedge')
            page = browser.new_page(viewport={'width': 1440, 'height': 950})
            page.set_default_timeout(5000)
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{port}', wait_until='networkidle')
            def check(name, fn):
                try:
                    fn()
                    report.append({'check': name, 'result': 'PASS'})
                except Exception as e:
                    report.append({'check': name, 'result': 'FAIL', 'error': str(e)[:700]})
                page.screenshot(path=str(OUT / (name + '.png')))
                page.keyboard.press('Escape')
            check('empty-start', lambda: expect(page.locator('#todayPanel')).to_be_visible())
            def settings():
                page.locator('#geminiProfileAvatarBtn').click()
                page.locator('#aiProvider').select_option('local')
                page.locator('#settingsSubmit').click()
                expect(page.locator('#settingsDialog')).not_to_be_visible()
                page.reload(wait_until='networkidle')
                page.locator('#geminiProfileAvatarBtn').click()
                expect(page.locator('#aiProvider')).to_have_value('local')
            check('settings-save-reload', settings)
            def profile():
                page.locator('#profileNav').click()
                page.locator('#openProfileButton').click()
                expect(page.locator('#profileDialog')).to_be_visible()
            check('profile-entry', profile)
            def onboarding():
                page.locator('#openImportJobButton').click()
                expect(page.locator('#profileDialog')).to_be_visible()
                from tests.test_profile_onboarding import RESUME_TEXT
                page.locator('#resumeFile').set_input_files({'name': 'synthetic-resume.txt', 'mimeType': 'text/plain', 'buffer': RESUME_TEXT.encode('utf-8')})
                page.locator('#displayName').fill('测试用户')
                page.locator('#targetRoles').fill('数据运营')
                page.locator('#preferredLocations').fill('上海')
                page.locator('#confirmTruth').check()
                page.locator('#profileSubmit').click()
                expect(page.locator('#profileDialog')).not_to_be_visible(timeout=15000)
                page.reload(wait_until='networkidle')
                expect(page.locator('#todayTitle')).to_have_text('今天，先推进一个岗位')
            if not args.seed:
                check('new-user-onboarding', onboarding)
            def import_job():
                page.locator('#openImportJobButton').click()
                page.locator('#parsedCompany').fill('示例软件测试公司')
                page.locator('#parsedTitle').fill('数据运营实习生')
                page.locator('#parsedLocation').fill('上海')
                page.locator('#parsedSourceUrl').fill('https://example.com/jobs/qa')
                page.locator('#parsedJdText').fill('负责 SQL 数据分析和运营报表，要求本科，每周四天，连续三个月。仅用于软件测试的合成岗位。')
                page.locator('#submitImportJob').click()
                expect(page.locator('#importJobDialog')).not_to_be_visible()
                page.reload(wait_until='networkidle')
                page.locator('#workspaceNav').click()
                expect(page.locator('.job-choice')).to_have_count(1)
            check('manual-import-persist', import_job)
            def resume():
                page.locator('#workspaceNav').click()
                page.locator('.job-choice').first.click()
                page.wait_for_load_state('networkidle')
                page.once('dialog', lambda dialog: dialog.accept())
                page.locator('[data-detail-action="create-resume"]').click()
                page.wait_for_load_state('networkidle')
                expect(page.locator('[data-detail-action="open-resume"]').first).to_be_visible(timeout=30000)
            check('resume-draft-action', resume)
            def edit_preview():
                page.locator('.job-more summary').first.click()
                page.locator('[data-detail-action="edit-resume"]').click()
                expect(page.locator('#resumeEditorDialog')).to_be_visible()
                page.locator('[data-editor-field="bullet-text"]').first.focus()
                expect(page.locator('.tailor-suggestion')).to_have_count(3)
                with page.expect_response('**/api/resume/preview') as response:
                    page.locator('#tailorPreviewButton').click()
                assert response.value.body().startswith(b'%PDF-')
                page.locator('#resumeEditorCloseButton').click()
            check('actual-draft-editor-preview', edit_preview)
            def approve_synthetic():
                # Test-only approval of synthetic data; never opens an employer site.
                page.once('dialog', lambda dialog: dialog.accept())
                page.locator('[data-detail-action="approve-resume"]').first.click()
                expect(page.locator('[data-detail-action="manual-apply"]')).to_be_visible(timeout=20000)
                page.reload(wait_until='networkidle')
                expect(page.locator('[data-detail-action="manual-apply"]')).to_be_visible()
            check('synthetic-approval-pack-persist', approve_synthetic)
            def progress():
                page.locator('#overviewNav').click()
                expect(page.locator('#overviewTitle')).to_have_text('投递进度')
                expect(page.locator('#trackingOpen')).to_be_visible()
            check('progress-message-entry', progress)
            report.append({'page_errors': errors})
            browser.close()
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=10)
        if server:
            server.shutdown()
            server.server_close()
        (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
        print(OUT)
    if any(item.get('result') == 'FAIL' or item.get('page_errors') for item in report):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
