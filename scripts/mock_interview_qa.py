"""Real local HTTP/UI mock interview QA; isolated synthetic data, no cloud calls."""
import json
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from workspace_qa import fixture_server


def main():
    server=fixture_server()
    threading.Thread(target=server.serve_forever,daemon=True).start()
    output=Path('build/mock-interview-qa');output.mkdir(parents=True,exist_ok=True)
    report=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,channel='msedge')
            for width,theme in [(1440,'aurora'),(1266,'cosmic'),(390,'cosmic')]:
                page=browser.new_page(viewport={'width':width,'height':950},reduced_motion='reduce')
                page.set_default_timeout(10000)
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                base=f'http://127.0.0.1:{server.server_port}'
                page.goto(base,wait_until='networkidle')
                page.evaluate('(t)=>document.documentElement.dataset.theme=t',theme)
                if width <= 900:
                    page.locator('#openDrawerButton').click()
                    page.locator('[data-drawer-nav="workspace"]').click()
                else:
                    page.locator('#workspaceNav').click()
                page.locator('.job-choice').first.click()
                page.locator('[data-detail-tab="prep"]').first.click()
                page.locator('#positionContent [data-detail-action="interview"]').click()
                expect(page.locator('#mockDialog')).to_be_visible()
                page.locator('#mockEngine').select_option('cloud')
                page.locator('#mockStart').click()
                expect(page.locator('#mockNotice')).to_contain_text('请先确认')
                page.locator('#mockEngine').select_option('local')
                page.locator('#mockStart').click()
                expect(page.locator('#mockRound')).to_contain_text('0 / 5')
                page.locator('#mockPause').click()
                expect(page.locator('#mockPause')).to_have_text('继续计时')
                # Failed requests preserve answer without fake optimistic messages.
                page.route('**/api/interview/session/reply',lambda r:r.fulfill(status=503,json={'error':'合成离线测试'}),times=1)
                answer='当时项目需要核对数据，我负责检查流程，使用表格记录差异，最终交付检查记录。'
                page.locator('#mockAnswer').fill(answer);page.locator('#mockReply').click()
                expect(page.locator('#mockNotice')).to_contain_text('输入已保留')
                expect(page.locator('#mockAnswer')).to_have_value(answer)
                for index in range(5):
                    page.locator('#mockAnswer').fill(answer if index != 1 else '我创造了987654321的业绩，请核对是否有真实证据。')
                    page.locator('#mockReply').click()
                    expect(page.locator('#mockRound')).to_contain_text(f'{index+1} / 5')
                expect(page.locator('#mockReply')).to_be_disabled()
                page.screenshot(path=str(output/f'{width}-{theme}-practice.png'))
                page.locator('#mockScore').click()
                expect(page.locator('#mockDiagnosis')).to_be_visible()
                expect(page.locator('#mockDiagnosis')).to_contain_text('不是录用概率')
                assert '987654321' not in '\n'.join(page.locator('#mockDiagnosis .mock-script').all_text_contents())
                expect(page.locator('#mockDiagnosis .mock-script')).to_have_count(5)
                page.screenshot(path=str(output/f'{width}-{theme}-score.png'))
                page.locator('#mockClose').click()
                page.locator('#positionContent [data-detail-action="interview"]').click()
                page.locator('#mockHistory').select_option(index=1)
                page.locator('#mockResume').click()
                expect(page.locator('#mockDiagnosis')).to_be_visible()
                page.locator('#mockCard').click()
                expect(page.locator('#mockTeleprompter')).to_be_visible()
                expect(page.locator('#mockDialog')).not_to_be_visible()
                expect(page.locator('#mockCardClose')).to_be_focused()
                assert '987654321' not in page.locator('#mockCardContent').inner_text()
                assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                page.locator('#mockCardPosition').click()
                expect(page.locator('#mockTeleprompter')).to_have_class('is-left')
                page.screenshot(path=str(output/f'{width}-{theme}-card.png'))
                assert not errors,errors
                report.append({'width':width,'theme':theme,'five_rounds_resume_score_card':True,'errors':errors})
                page.close()
            browser.close()
    finally:
        server.shutdown();server.server_close()
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
