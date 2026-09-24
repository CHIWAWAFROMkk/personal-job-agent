"""Fresh local-only user journey, with real browser actions and isolated data."""
from __future__ import annotations

import json
import hashlib
import os
import threading
import uuid
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "work" / "product-ui"
RUN = OUTPUT / f"run-{uuid.uuid4().hex[:8]}"
RUN.mkdir(parents=True)
for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "JOB_AGENT_AI_API_KEY", "BOCHA_API_KEY", "BRAVE_SEARCH_API_KEY", "AMAP_WEB_API_KEY", "AMAP_API_KEY", "OPENAI_BASE_URL"):
    os.environ.pop(key, None)
os.environ.update(JOB_AGENT_PROJECT_ROOT=str(RUN), JOB_AGENT_DATA_DIR=str(RUN), JOB_AGENT_PROFILE_PATH=str(RUN / "private/profile.json"), JOB_AGENT_DATABASE_PATH=str(RUN / "private/jobs.sqlite3"), JOB_AGENT_CONFIG_PATH=str(RUN / "private/app-settings.json"), JOB_AGENT_AI_PROVIDER="none", TEMP=str(RUN), TMP=str(RUN))

from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader
from io import BytesIO
from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.job_repository import JobRepository
from job_agent.services.runtime_config import RuntimeConfig, save_runtime_config

JD = """仅用于验收的虚构岗位。公司：演示数据工作室；岗位：数据运营实习生；地点：上海。
职责：整理运营数据，使用 SQL 进行统计，协助用户调研并整理反馈，完成周报。
要求：本科在读，熟悉 Excel、SQL，能够清楚记录分析过程；每周三天，连续三个月。"""
RESUME = """验收用户（合成资料）
邮箱：qa@example.com
教育经历：示例大学，信息管理本科，2023 年至 2027 年。
项目经历：校园社团报名数据整理，使用 Excel 清理报名信息，使用 SQL 汇总活动人数，编写活动复盘。
技能：Excel、SQL、Python。目标岗位：数据运营实习生。期望地点：上海。
所有资料仅用于本机验收，不属于真实个人。"""


def main():
    report = {"run": str(RUN), "stages": [], "errors": []}
    private = RUN / "private"
    save_runtime_config(RuntimeConfig(), private / "app-settings.json")
    repo = JobRepository(private / "jobs.sqlite3")
    server = create_dashboard_server(repo, output_dir=RUN / "output", private_dir=private, profile_path=private / "profile.json", port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    resume = RUN / "synthetic-resume.txt"
    resume.write_text(RESUME, encoding="utf-8")
    empty_resume = RUN / "empty-resume.txt"
    empty_resume.write_text("", encoding="utf-8")
    page = None
    try:
        with patch("os.startfile"), patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True), patch("job_agent.services.dashboard_routes.jobs_api._open_job_page"), sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1366, "height": 768})
            page.set_default_timeout(15000)
            expect.set_options(timeout=15000)
            page.on("pageerror", lambda error: report["errors"].append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="networkidle")
            expect(page.get_by_role("heading", name="从你的简历开始")).to_be_visible()
            page.screenshot(path=str(RUN / "01-empty-desktop.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(RUN / "01-empty-mobile.png"))
            report["stages"].append("fresh_empty_state")
            page.locator('[data-home-action="profile"]').click()
            page.keyboard.press("Escape")
            expect(page.locator('[data-home-action="profile"]')).to_be_focused()
            page.keyboard.press("Enter")
            page.locator("#profileSubmit").click()
            assert page.locator("#profileForm").evaluate("el => !el.checkValidity()")
            page.locator("#displayName").fill("验收用户")
            page.locator("#resumeFile").set_input_files(str(resume))
            page.locator("#confirmTruth").check()
            page.locator("#targetRoles").fill("数据运营实习生")
            page.locator("#preferredLocations").fill("上海")
            page.locator("#resumeFile").set_input_files(str(empty_resume))
            page.locator("#profileSubmit").click()
            expect(page.locator("#profileFormStatus")).to_be_visible()
            expect(page.locator("#profileFormStatus")).to_be_focused()
            expect(page.locator("#targetRoles")).to_have_value("数据运营实习生")
            page.screenshot(path=str(RUN / "02-upload-error-mobile.png"))
            page.locator("#resumeFile").set_input_files(str(resume))
            page.screenshot(path=str(RUN / "02-onboarding-desktop.png"))
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(RUN / "02-onboarding-mobile.png"))
            page.set_viewport_size({"width": 1366, "height": 768})
            page.screenshot(path=str(RUN / "02-onboarding-desktop.png"))
            page.locator("#profileSubmit").click()
            expect(page.locator("#profileDialog")).not_to_be_visible(timeout=30000)
            saved_profile = json.loads((private / "profile.json").read_text(encoding="utf-8"))
            assert saved_profile["person"]["contact"]["email"] == "qa@example.com"
            report["stages"].append("profile_from_upload")
            page.locator('[data-home-action="import"]').click()
            page.locator("#rawJobInput").fill(JD)
            page.locator("#btnAiExtract").click()
            expect(page.locator("#btnAiExtract")).to_be_enabled(timeout=30000)
            page.screenshot(path=str(RUN / "03-jd-extraction-desktop.png"))
            report["extraction_status"] = page.locator("#importJobStatus").inner_text()
            page.locator("#parsedCompany").fill("演示数据工作室")
            page.locator("#parsedTitle").fill("数据运营实习生")
            page.locator("#parsedLocation").fill("上海")
            page.locator("#parsedJdText").fill(JD)
            page.locator("#submitImportJob").click()
            expect(page.locator("#importJobDialog")).not_to_be_visible()
            expect(page.locator(".job-choice")).to_have_count(1)
            report["stages"].append("job_import")
            page.locator(".job-choice").click()
            page.route("**/api/jobs/1/prepare-apply", lambda route: route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "验收模拟：本地文件暂被占用，请关闭后重试。"})), times=1)
            page.locator('[data-detail-action="prepare"]').click()
            expect(page.locator(".prepare-retry")).to_be_visible()
            expect(page.locator("#prepareProgress")).to_contain_text("文件暂被占用")
            page.screenshot(path=str(RUN / "04-retry-desktop.png"))
            with page.expect_response("**/api/jobs/1/prepare-apply", timeout=90000) as prepare_response:
                page.locator(".prepare-retry").click()
            assert prepare_response.value.status == 200, prepare_response.value.text()
            expect(page.locator("#reviewDialog")).to_be_visible(timeout=90000)
            report["stages"].append("prepared_not_applied")
            page.screenshot(path=str(RUN / "04-prepared-desktop.png"))
            expect(page.locator("#reviewOpenPageBtn")).to_be_enabled(timeout=30000)
            page.screenshot(path=str(RUN / "05-review-desktop.png"))
            report["stages"].append("review_loaded")
            detail = page.request.get(f"http://127.0.0.1:{server.server_port}/api/jobs/1/detail").json()
            assert all(event.get("status") != "applied" for event in detail.get("events", []))
            expect(page.locator(".job-choice.is-applied")).to_have_count(0)
            report["prepared_detail"] = detail
            pdf_link = page.locator("#reviewPdfLink").get_attribute("href")
            assert detail["resume_version"]["pdf_sha256"] in pdf_link
            pdf_response = page.request.get(f"http://127.0.0.1:{server.server_port}{pdf_link}")
            assert pdf_response.status == 200
            pdf_bytes = pdf_response.body()
            assert pdf_bytes.startswith(b"%PDF")
            report["pdf_http_sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
            assert report["pdf_http_sha256"] == detail["resume_version"]["pdf_sha256"].lower()
            pdf_text = "".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf_bytes)).pages)
            assert "qa@example.com" in pdf_text, "Imported contact must appear in the actual exported PDF"
            (RUN / "pdf-text.txt").write_text(pdf_text, encoding="utf-8")
            (RUN / "reviewed-synthetic-resume.pdf").write_bytes(pdf_bytes)
            page.set_viewport_size({"width": 390, "height": 844})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(RUN / "05-review-mobile.png"))
            page.locator("#reviewOpenPageBtn").scroll_into_view_if_needed()
            page.screenshot(path=str(RUN / "05-review-actions-mobile.png"))
            page.locator("#reviewOpenPageBtn").click()
            expect(page.locator("#reviewDialog")).not_to_be_visible()
            expect(page.locator(".job-choice.is-applied")).to_have_count(0)
            report["stages"].append("approval_does_not_record_application")
            page.locator(".job-more summary").click()
            page.locator('[data-detail-action="status"]').click()
            expect(page.locator("#statusConfirmed")).not_to_be_checked()
            page.locator("#statusValue").select_option("applied")
            page.locator("#statusDetail").fill("仅本机合成验收：模拟本人已在招聘网站完成提交，无真实投递。")
            page.locator("#statusSubmit").click()
            assert page.locator("#statusForm").evaluate("el => !el.checkValidity()")
            page.locator("#statusConfirmed").check()
            page.locator("#statusSubmit").click()
            expect(page.locator("#statusConfirmed")).not_to_be_checked()
            page.locator("#openDrawerButton").click()
            expect(page.locator("#geminiDrawerCloseBtn")).to_be_focused()
            page.keyboard.press("Shift+Tab")
            expect(page.locator("#drawerSettingsBtn")).to_be_focused()
            page.keyboard.press("Tab")
            expect(page.locator("#geminiDrawerCloseBtn")).to_be_focused()
            page.locator("#drawerUserProfileBtn").focus()
            page.keyboard.press("Enter")
            expect(page.locator("#geminiProfileSheet")).to_be_visible()
            page.locator("#geminiProfileDoneBtn").click()
            page.locator("#openDrawerButton").click()
            page.keyboard.press("Escape")
            expect(page.locator("#openDrawerButton")).to_be_focused()
            page.keyboard.press("Enter")
            page.locator('[data-drawer-nav="workspace"]').click()
            expect(page.locator(".job-choice.is-applied")).to_have_count(1)
            report["stages"].append("explicit_synthetic_application_record")
            page.screenshot(path=str(RUN / "06-applied-mobile.png"))
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            server = create_dashboard_server(JobRepository(private / "jobs.sqlite3"), output_dir=RUN / "output", private_dir=private, profile_path=private / "profile.json", port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="networkidle")
            page.locator("#openDrawerButton").click()
            page.locator('[data-drawer-nav="workspace"]').click()
            expect(page.locator(".job-choice.is-applied")).to_have_count(1)
            page.locator(".job-choice").click()
            page.locator('[data-detail-tab="progress"]').click()
            expect(page.locator("#positionContent")).to_contain_text("仅本机合成验收")
            page.screenshot(path=str(RUN / "07-persisted-mobile.png"))
            page.set_viewport_size({"width": 1366, "height": 768})
            page.screenshot(path=str(RUN / "07-persisted-desktop.png"))
            report["stages"].append("server_restart_persistence")
            report["stages"].append("keyboard_drawer_and_profile")
            report["limitations"] = ["OS PDF reader, Explorer and employer-page launch are mocked to prevent external effects.", "Headless Chromium iframe PDF paint is not accepted as native WebView PDF evidence; authenticated version-bound PDF bytes and SHA were verified separately."]
            assert report["errors"] == [], report["errors"]
            (RUN / "review-dom.html").write_text(page.content(), encoding="utf-8")
            browser.close()
    except Exception as exc:
        report["failure"] = str(exc)
        if page:
            try:
                page.screenshot(path=str(RUN / "failure.png"), full_page=True)
                (RUN / "failure-dom.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
        raise
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        (RUN / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
