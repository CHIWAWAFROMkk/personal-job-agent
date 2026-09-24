"""Exercise the job list with many synthetic records and no private data."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from job_agent.models.job_record import JobRecordInput, SearchCandidateInput
from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import save_profile
from job_agent.services.runtime_config import RuntimeConfig, save_runtime_config
from tests.helpers import sample_profile


def main() -> None:
    fixture = ROOT / "build" / "high-volume-ui-qa" / datetime.now().strftime("%Y%m%d-%H%M%S")
    private = fixture / "data" / "private"
    private.mkdir(parents=True)
    repo = JobRepository(private / "jobs.sqlite3")
    profile = sample_profile()
    profile.job_search.commute.max_one_way_minutes = 30
    save_profile(profile, private / "profile.json")
    save_runtime_config(RuntimeConfig(), private / "app-settings.json")
    for index in range(125):
        repo.upsert_job(JobRecordInput(
            company=f"示例公司{index:04d}",
            title=f"数据分析实习岗位{index:04d}",
            jd_text="负责 SQL、报表核对和业务分析。仅用于软件压力验收。",
            source="合成验收",
            source_url=f"https://example.com/jobs/{index}",
            location="上海",
        ))
    for index in range(45):
        candidate = repo.upsert_search_candidate(SearchCandidateInput(
            provider="synthetic", query="上海数据分析实习",
            title=f"候选链接{index:04d}",
            url=f"https://example.com/candidate/{index}",
            snippet="搜索线索，尚未录入正式岗位。",
        ))
        repo.mark_candidate_verification(candidate.candidate_id, "needs_manual_review")

    server = create_dashboard_server(
        repo,
        private_dir=private,
        profile_path=private / "profile.json",
        output_dir=fixture / "output",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {"fixture": str(fixture), "records": 125}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
            page.set_default_timeout(30000)
            started = time.perf_counter()
            page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="networkidle")
            page.locator("#workspaceNav").click()
            page.locator('.track-plan [data-job-track="all"]').click()
            expect(page.locator("#jobsCount")).to_have_text("125")
            expect(page.locator("#jobs [data-select-job]")).to_have_count(60)
            expect(page.locator("#jobs .job-list-pagination")).to_contain_text("1–60 / 125")
            report["first_page_seconds"] = round(time.perf_counter() - started, 2)

            page.locator('[data-jobs-page="1"]').click()
            expect(page.locator("#jobs [data-select-job]")).to_have_count(60)
            expect(page.locator("#jobs .job-list-pagination")).to_contain_text("61–120 / 125")
            page.locator('[data-jobs-page="1"]').click()
            expect(page.locator("#jobs [data-select-job]")).to_have_count(5)
            expect(page.locator("#jobs .job-list-pagination")).to_contain_text("121–125 / 125")

            page.locator('[data-job-filter="applied"]').click()
            expect(page.locator("#jobsCount")).to_have_text("0")
            expect(page.locator("#jobDetail")).to_contain_text("选择一个职位")
            page.locator('[data-job-filter="all"]').click()
            page.locator("#jobSearch").fill("示例公司0120")
            expect(page.locator("#jobsCount")).to_have_text("1")
            expect(page.locator("#jobs [data-select-job]")).to_have_count(1)
            expect(page.locator("#jobDetail h2")).to_contain_text("数据分析实习岗位0120")

            page.locator("#overviewNav").click()
            page.locator("#candidateQueue summary").click()
            expect(page.locator("#candidateQueueList [data-candidate-id]")).to_have_count(20)
            expect(page.locator("#candidateQueuePager")).to_contain_text("1–20 / 45")
            page.locator("#candidateQueuePager [data-candidate-page='1']").click()
            expect(page.locator("#candidateQueuePager")).to_contain_text("21–40 / 45")
            page.locator("#candidateQueuePager [data-candidate-page='1']").click()
            expect(page.locator("#candidateQueueList [data-candidate-id]")).to_have_count(5)
            page.locator("#candidateQueuePager [data-candidate-page='-1']").click()
            expect(page.locator("#candidateQueuePager")).to_contain_text("21–40 / 45")
            first_card = page.locator("#candidateQueueList [data-candidate-id]").first
            first_card.locator("[data-candidate-status]").select_option("live")
            first_card.locator("[data-candidate-save]").click()
            expect(page.locator("#candidateQueuePager")).to_contain_text("/ 44")
            candidate_title = page.locator("#candidateQueueList [data-candidate-id] h3").first.inner_text()
            page.locator("#candidateQueueList [data-candidate-import]").first.click()
            expect(page.locator("#parsedTitle")).to_have_value(candidate_title)
            expect(page.locator("#parsedCompany")).to_have_value("")
            page.locator("#closeImportJobButton").click()
            page.screenshot(path=str(fixture / "candidate-review-desktop.png"))

            page.set_viewport_size({"width": 390, "height": 844})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(fixture / "high-volume-mobile.png"))
            page.goto(f"http://127.0.0.1:{server.server_port}/#job/999999", wait_until="networkidle")
            page.reload(wait_until="networkidle")
            expect(page).to_have_url(f"http://127.0.0.1:{server.server_port}/#jobs")
            expect(page.locator("#jobWorkspace")).not_to_have_class(re.compile(r"\bdetail-visible\b"))
            expect(page.locator("#alert")).to_contain_text("岗位不存在或已移除")
            with repo._connection() as connection:
                connection.execute("UPDATE jobs SET commute_minutes = 599")
            page.goto(f"http://127.0.0.1:{server.server_port}/#today", wait_until="networkidle")
            page.reload(wait_until="networkidle")
            expect(page.locator("#todayContent")).to_contain_text("触发了已设置的筛选条件")
            expect(page.locator("#todayContent [data-home-action='jobs']")).to_be_visible()
            report["checks"] = ["pagination", "filter_detail_consistency", "search_all_records", "candidate_review_pagination", "candidate_to_manual_import", "mobile_overflow", "missing_job_link", "blocked_jobs_not_recommended"]
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    (fixture / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
