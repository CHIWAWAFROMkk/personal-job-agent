"""Check the three onboarding states using isolated synthetic data."""
import json
import sys
import threading
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from tests.test_profile_onboarding import RESUME_TEXT
from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.job_repository import JobRepository
from job_agent.services.runtime_config import RuntimeConfig, save_runtime_config


def main():
    out = ROOT / 'build/simplification-qa'
    out.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='fixture-', dir=out))
    private = root / 'data/private'
    repo = JobRepository(private / 'jobs.sqlite3')
    save_runtime_config(RuntimeConfig(), private / 'app-settings.json')
    server = create_dashboard_server(repo, private_dir=private,
        profile_path=private / 'profile.json', output_dir=root / 'output', port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            for stage in ('empty', 'profile', 'job'):
                if stage == 'profile':
                    setup = browser.new_page()
                    setup.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                    setup.locator('[data-home-action="profile"]').click()
                    setup.locator('#resumeFile').set_input_files({'name': 'synthetic.txt', 'mimeType': 'text/plain', 'buffer': RESUME_TEXT.encode('utf-8')})
                    setup.locator('#displayName').fill('测试用户')
                    setup.locator('#targetRoles').fill('数据运营')
                    setup.locator('#preferredLocations').fill('上海')
                    setup.locator('details.profile-optional').first.locator('summary').click()
                    setup.locator('#profileEmail').fill('synthetic@example.com')
                    setup.locator('#confirmTruth').check()
                    setup.locator('#profileSubmit').click()
                    expect(setup.locator('[data-home-action="import"]')).to_be_visible(timeout=20000)
                    setup.reload(wait_until='networkidle')
                    expect(setup.locator('[data-home-action="import"]')).to_be_visible()
                    setup.locator('#profileNav').click()
                    setup.locator('#openProfileButton').click()
                    expect(setup.locator('#profileEmail')).to_have_value('synthetic@example.com')
                    setup.locator('details.profile-optional').first.locator('summary').click()
                    setup.locator('#profileMode').select_option('replace')
                    expect(setup.locator('#profileEmail')).to_have_value('')
                    expect(setup.locator('#targetRoles')).to_have_value('')
                    setup.locator('#profileMode').select_option('update')
                    expect(setup.locator('#profileEmail')).to_have_value('synthetic@example.com')
                    expect(setup.locator('#targetRoles')).to_have_value('数据运营')
                    setup.locator('#closeProfileButton').click()
                    setup.close()
                if stage == 'job':
                    setup = browser.new_page()
                    setup.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                    setup.locator('[data-home-action="import"]').click()
                    for field, value in {'parsedCompany': '合成测试公司', 'parsedTitle': '数据运营实习生',
                        'parsedLocation': '上海', 'parsedSourceUrl': 'https://example.com/test-job',
                        'parsedJdText': '负责 SQL 数据分析与报表整理。仅用于软件测试。'}.items():
                        setup.locator('#' + field).fill(value)
                    setup.locator('#submitImportJob').click()
                    expect(setup.locator('#importJobDialog')).not_to_be_visible(timeout=20000)
                    expect(setup.locator('[data-detail-action="prepare"]')).to_be_visible()
                    setup.close()
                for width, theme in ((1440, 'aurora'), (390, 'aurora'), (1440, 'cosmic'), (390, 'cosmic')):
                    page = browser.new_page(viewport={'width': width, 'height': 900}, reduced_motion='reduce')
                    page.set_default_timeout(10000)
                    errors, sms = [], []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.on('request', lambda r: sms.append(r.url) if '/api/sms' in r.url else None)
                    page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    if stage == 'empty':
                        expect(page.locator('[data-home-action="profile"]')).to_be_visible()
                        page.locator('[data-home-action="profile"]').click()
                        expect(page.locator('#resumeFile')).to_be_visible()
                        expect(page.locator('#profileEmail')).not_to_be_visible()
                        page.locator('#closeProfileButton').click()
                        page.locator('#openDrawerButton').click()
                        page.locator('.drawer-tools summary').click()
                        page.locator('[data-drawer-nav="help"]').click()
                        expect(page.locator('.setup-guide')).to_be_focused()
                    elif stage == 'profile':
                        expect(page.locator('[data-home-action="import"]')).to_be_visible()
                        page.locator('[data-home-action="import"]').click()
                        expect(page.locator('#parsedCompany')).to_be_visible()
                        page.route('**/api/jobs/extract-jd', lambda route: route.fulfill(json={
                            'ok': False, 'warning': '网页无法读取。', 'preserve_input': True}))
                        page.locator('#rawJobInput').fill('https://example.com/job/1')
                        page.locator('#btnAiExtract').click()
                        expect(page.locator('#importJobStatus')).to_contain_text('网页无法读取')
                        expect(page.locator('#parsedJdText')).to_have_value('')
                        page.unroute('**/api/jobs/extract-jd')
                        page.route('**/api/jobs/extract-jd', lambda route: route.fulfill(json={
                            'ok': True, 'parsed': {'company': '未知公司', 'title': '数据运营实习生',
                            'jd_text': '负责整理业务数据并撰写分析报告。',
                            'source_url': 'https://example.com/job/1'}}))
                        page.locator('#btnAiExtract').click()
                        expect(page.locator('#parsedSourceUrl')).to_have_value('https://example.com/job/1')
                        expect(page.locator('#parsedCompany')).to_have_value('')
                        expect(page.locator('#importJobStatus')).to_contain_text('公司或岗位名称未识别')
                        page.keyboard.press('Escape')
                    else:
                        expect(page.locator('#startGuide')).to_be_visible()
                        expect(page.locator('.today-focus-label')).to_have_text('建议优先查看')
                        expect(page.locator('.today-focus-actions [data-home-job]')).to_have_text('查看并决定')
                        assert '算法精选' not in page.locator('#todayPanel').inner_text()
                        page.locator('#startGuide summary').click()
                        expect(page.locator('.start-steps')).to_be_visible()
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'horizontal overflow'
                    assert not errors and not sms, (errors, sms)
                    page.screenshot(path=str(out / f'{stage}-{width}-{theme}.png'))
                    results.append({'stage': stage, 'width': width, 'theme': theme, 'passed': True})
                    page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (out / 'report.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False))


if __name__ == '__main__':
    main()
