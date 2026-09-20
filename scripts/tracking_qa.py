"""Epic 4 real local HTTP + UI acceptance using isolated synthetic fixtures."""
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from workspace_qa import fixture_server


def main():
    server = fixture_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    output = Path('build/tracking-qa')
    output.mkdir(parents=True, exist_ok=True)
    report = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel='msedge')
            for index, (width, theme) in enumerate([(1440, 'aurora'), (1266, 'cosmic'), (390, 'cosmic')]):
                page = browser.new_page(viewport={'width': width, 'height': 950}, reduced_motion='reduce')
                page.set_default_timeout(10000)
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base, wait_until='networkidle')
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
                if width <= 900:
                    page.locator('#openDrawerButton').click()
                    page.locator('[data-drawer-nav="tracking"]').click()
                else:
                    page.locator('#overviewNav').click()
                expect(page.locator('.tracking-job')).to_have_count(6)
                page.locator('#trackingOpen').click()
                at = datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=24, minutes=index)
                message = f'【示例科技】邀请您参加在线测评。请于{at:%Y年%m月%d日 %H:%M}前完成测评。https://assessment.example.com/test'
                page.locator('#trackingMessage').fill(message)
                page.locator('#trackingParse').click()
                expect(page.locator('#trackingReview')).to_be_visible()
                expect(page.locator('#trackingCompany')).to_contain_text('示例科技')
                expect(page.locator('#trackingAt')).not_to_have_value('')
                expect(page.locator('#trackingJob')).to_have_value('')
                page.locator('#trackingJob').select_option(str(index+1))
                page.locator('#trackingConfirmed').check()
                page.locator('#trackingApply').scroll_into_view_if_needed()
                box = page.locator('#trackingApply').bounding_box()
                assert min(box['width'], box['height']) >= 44
                page.screenshot(path=str(output / f'{width}-{theme}-drawer.png'))
                page.locator('#trackingApply').click()
                expect(page.locator('#trackingDrawer')).not_to_be_visible()
                expect(page.locator('#trackingUrgent .tracking-event')).to_have_count(index+1)
                expect(page.locator(f'[data-tracking-job="{index+1}"]')).to_contain_text('测评')
                page.wait_for_load_state('networkidle')
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.evaluate('window.scrollTo(0,0)')
                page.screenshot(path=str(output / f'{width}-{theme}-board.png'), full_page=True)
                with page.expect_download() as download:
                    page.locator('#trackingExport').click()
                downloaded = output / f'{width}-calendar.ics'
                download.value.save_as(downloaded)
                assert 'BEGIN:VEVENT' in downloaded.read_text(encoding='utf-8')
                # Editing original notification invalidates a previous parsed preview.
                page.locator('#trackingOpen').click()
                page.locator('#trackingMessage').fill('【示例科技】面试邀请，时间另行通知。')
                page.locator('#trackingParse').click()
                expect(page.locator('#trackingReview')).to_be_visible()
                expect(page.locator('#trackingAt')).to_have_value('')
                page.locator('#trackingMessage').fill('通知已修改')
                expect(page.locator('#trackingReview')).not_to_be_visible()
                page.keyboard.press('Escape')
                expect(page.locator('#trackingMessage')).to_have_value('')
                if index == 2:
                    page.once('dialog', lambda d: d.accept())
                    page.locator('#trackingUrgent [data-complete]').first.click()
                    expect(page.locator('#trackingUrgent .tracking-event')).to_have_count(2)
                    page.reload(wait_until='networkidle')
                    expect(page.locator('#trackingUrgent .tracking-event')).to_have_count(2)
                page.wait_for_load_state('networkidle')
                assert not errors, errors
                report.append({'width':width,'theme':theme,'parse_apply_calendar':True,'errors':errors})
                page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
