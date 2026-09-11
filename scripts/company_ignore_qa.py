"""Isolated UI checks for job archive/restore and manual page opening; never submits."""
from pathlib import Path
import threading
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect
from workspace_qa import fixture_server


def main():
    output = Path(__file__).resolve().parents[1] / "work" / "company-ignore-qa"
    output.mkdir(parents=True, exist_ok=True)
    server = fixture_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch("job_agent.services.dashboard.webbrowser.open", return_value=True) as opened, sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for name, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                expect(page.locator(".job-choice")).to_have_count(6)
                page.locator('[data-select-job="2"]').click()
                expect(page.locator('[data-detail-action="open-page"]')).to_be_enabled()
                expect(page.locator('.safe-fill-task')).to_be_disabled()
                page.locator('[data-detail-action="open-page"]').click()
                expect(page.get_by_text("已请求系统浏览器打开招聘页。", exact=False)).to_be_visible()
                assert opened.call_args.args == ("https://example.com/jobs/1",)
                page.locator('[data-detail-action="archive-job"]').click()
                expect(page.locator('[data-detail-action="archive-job"]')).to_have_text("恢复岗位")
                expect(page.locator('.job-choice').last).to_have_attribute("data-select-job", "2")
                expect(page.locator('[data-select-job="2"] .ignored-company-mark')).to_have_count(1)
                page.screenshot(path=str(output / f"{name}-ignored.png"))
                page.reload()
                expect(page.locator('.job-choice').last).to_have_attribute("data-select-job", "2")
                page.locator('[data-job-filter="archived"]').click()
                expect(page.locator('.job-choice')).to_have_count(1)
                expect(page.locator('.job-choice')).to_have_attribute("data-select-job", "2")
                page.locator('[data-job-filter="pending"]').click()
                expect(page.locator('[data-select-job="2"]')).to_have_count(0)
                page.locator('[data-job-filter="all"]').click()
                page.locator('[data-select-job="2"]').click()
                page.locator('[data-detail-action="archive-job"]').click()
                expect(page.locator('[data-detail-action="archive-job"]')).to_have_text("岗位已失效")
                expect(page.locator('.job-choice').last).to_have_attribute("data-select-job", "1")
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                page.close()
            browser.close()
        print("PASS desktop/mobile: open without resume, single job sinks, red marker, reload persistence, expired filter, pending exclusion, restore; browser mocked, no submission")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
