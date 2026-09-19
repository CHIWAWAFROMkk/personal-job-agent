"""Synthetic workbench browser regression; no user data or external services."""
import json
import re
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from workspace_qa import fixture_server


def main():
    server = fixture_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    output = Path('build/resume-tailor-qa')
    output.mkdir(parents=True, exist_ok=True)
    content = {'template_id': 'reference-a4', 'person': {'name': '合成测试用户'}, 'summary': 'SQL 数据分析',
               'skills': ['SQL'], 'education': [], 'experience_sections': [
                   {'title': '实习经历', 'entries': [{'organization': '示例公司', 'role': '数据运营',
                    'dates': '2025', 'bullets': [{'label': '分析', 'text': '使用 SQL 整理业务数据。',
                                                'fact_ids': ['fact-sql-analysis']}]}]}]}
    report = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel='msedge')
            for width, theme in [(1440, 'aurora'), (1266, 'cosmic'), (390, 'cosmic')]:
                page = browser.new_page(viewport={'width': width, 'height': 950}, reduced_motion='reduce')
                errors = []
                console_errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('console', lambda e: console_errors.append(e.text) if e.type == 'error' else None)
                page.set_default_timeout(10000)
                def draft(route):
                    job_id = int(re.search(r'/jobs/(\d+)/', route.request.url).group(1))
                    route.fulfill(json={'content': {**content, 'target': {'job_id': job_id}}})
                page.route('**/api/jobs/*/resume-content', draft)
                page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                # Public UI event handler, using a synthetic saved-draft response.
                page.locator('#workspaceNav').click()
                page.locator('.job-choice').first.click()
                page.evaluate("""() => { const b=document.createElement('button'); b.dataset.detailAction='edit-resume';
                  b.id='qaEdit'; b.textContent='编辑合成草稿'; document.querySelector('#jobDetail .next-action .task-controls').append(b); }""")
                page.locator('#qaEdit').click()
                expect(page.locator('#resumeEditorDialog')).to_be_visible()
                paragraph = page.locator('[data-editor-field="bullet-text"]').first
                paragraph.focus()
                expect(page.locator('.tailor-suggestion')).to_have_count(3)
                original = paragraph.input_value()
                page.locator('.tailor-suggestion button').first.click()
                expect(paragraph).to_have_value('使用 SQL 清洗业务数据并输出周度分析。')
                page.locator('#tailorUndo').click()
                expect(paragraph).to_have_value(original)
                paragraph.fill('SQL')
                expect(page.locator('#tailorCoverage')).not_to_have_text('更新中')
                expect(page.locator('.tailor-suggestion')).to_have_count(3)
                page.locator('.tailor-suggestion summary').first.click()
                expect(page.locator('.tailor-suggestion details').first).to_contain_text('已确认依据')
                overflow = page.locator('#resumeEditorDialog').evaluate('(e) => e.scrollWidth > e.clientWidth + 1')
                assert not overflow
                for selector in ['#tailorRetry', '#tailorUndo', '#resumeEditorSubmit']:
                    box = page.locator(selector).bounding_box()
                    assert box['height'] >= 44 and box['width'] >= 44, (selector, box)
                page.locator('#resumeEditorDialog .dialog-body').evaluate('(e) => e.scrollTop=0')
                page.screenshot(path=str(output / f'{width}-{theme}.png'))
                with page.expect_response('**/api/resume/preview') as preview_response:
                    page.locator('#tailorPreviewButton').click()
                expect(page.locator('#tailorPreviewStatus')).to_contain_text('尚未保存或批准', timeout=15000)
                expect(page.locator('#tailorPreviewFrame')).to_have_attribute('src', re.compile(r'^blob:'))
                assert preview_response.value.body().startswith(b'%PDF-')
                page.wait_for_timeout(1500)
                page.screenshot(path=str(output / f'{width}-{theme}-pdf.png'))
                page.locator('#tailorEditView').click()
                paragraph.fill('SQL 重新修改')
                expect(page.locator('#tailorPreviewFrame')).not_to_have_attribute('src', re.compile(r'^blob:'))
                expect(page.locator('#tailorPreviewStatus')).to_contain_text('重新预览')
                page.locator('#resumeEditorCloseButton').click()
                expect(page.locator('#resumeEditorDialog')).not_to_be_visible()
                assert not errors, errors
                report.append({'width': width, 'theme': theme, 'errors': errors, 'console_errors': console_errors, 'apply_undo': True, 'pdf_preview': True})
                page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
