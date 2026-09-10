"""Job workspace UI regression. Isolated local fixtures; never opens a real ATS."""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from job_agent.models.job_record import JobRecordInput
from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import save_profile
from job_agent.services.runtime_config import RuntimeConfig, save_runtime_config
from tests.helpers import sample_profile
from tests.test_dashboard import scored_result

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "work" / "workspace-qa"
JD = """岗位职责
1. 分析用户行为与业务数据，识别关键转化环节，提出可验证的改进建议。
2. 参与 AI 产品的内容运营，与产品、算法和设计团队协作完成实验。
3. 维护业务指标口径，跟踪实验效果，沉淀可复用的分析方法。

任职要求
1. 本科及以上，每周至少到岗 4 天，连续实习 3 个月。
2. 熟悉 SQL 与基础数据分析，能够清楚说明自己的项目贡献。
3. 对 AI 产品有持续关注，有用户研究或运营实践经历优先。

这是一份仅用于界面验证的示例职位，不代表任何公司的真实招聘。"""


def fixture_server(port: int = 0):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix="fixture-", dir=OUTPUT))
    private = fixture / "private"
    save_profile(sample_profile(), private / "profile.json")
    save_runtime_config(RuntimeConfig(), private / "app-settings.json")
    repo = JobRepository(private / "jobs.sqlite3")
    companies = ["示例 · 星图科技", "示例 · 远山数据", "示例 · 云帆", "示例 · 知序", "示例 · 弧光", "示例 · 未见"]
    titles = ["AI 产品运营实习生", "数据分析实习生", "用户增长策略实习生", "内容运营实习生", "商业化产品实习生", "用户研究实习生"]
    for index, (company, title) in enumerate(zip(companies, titles)):
        job = repo.upsert_job(JobRecordInput(
            company=company, title=title, jd_text=JD, location="上海 · 杨浦",
            source="QA fixture", source_url=f"https://example.com/jobs/{index}",
        ))
        repo.add_match_result(job.job_id, scored_result(company=company, title=title, score=90 if index == 0 else 80))
        if index == 0:
            repo.record_application_status(job.job_id, "applied", source="manual_dashboard", detail="示例：官网完成申请")
    return create_dashboard_server(repo, output_dir=fixture / "output", private_dir=private, profile_path=private / "profile.json", port=port)


def run_checks(url: str) -> None:
    report = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for name, width, height in [("desktop", 1440, 1000), ("mobile", 390, 844), ("landscape", 844, 390), ("compact", 1024, 768)]:
            page = browser.new_page(viewport={"width": width, "height": height})
            media = page.context.new_cdp_session(page)
            media.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-transparency", "value": "no-preference"}]})
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            response = page.goto(url, wait_until="networkidle", timeout=15000)
            expect(page.locator(".job-choice")).to_have_count(6)
            expect(page.locator("#systemState")).to_have_text("本地数据已连接")
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            assert page.locator(".hero").count() == 0
            material = page.locator(".topbar-inner").evaluate("el => { const style=getComputedStyle(el); return {blur:style.backdropFilter || style.webkitBackdropFilter || '', supports:CSS.supports('backdrop-filter','blur(1px)') || CSS.supports('-webkit-backdrop-filter','blur(1px)'), reduced:matchMedia('(prefers-reduced-transparency: reduce)').matches, background:style.backgroundColor, radius:parseFloat(style.borderRadius)} }")
            assert material["background"] != "rgba(0, 0, 0, 0)"
            if material["supports"] and not material["reduced"] and material["blur"]:
                assert material["blur"] != "none"
            assert material["radius"] >= 16
            media.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-transparency", "value": "reduce"}]})
            assert page.locator(".topbar-inner").evaluate("el => getComputedStyle(el).backdropFilter") == "none"
            media.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-transparency", "value": "no-preference"}]})
            assert page.locator("#jobDetail").evaluate("el => parseFloat(getComputedStyle(el).backgroundColor.split(',')[3] || 1)") >= .95
            if material["supports"] and not material["reduced"]:
                assert page.locator(".position-toolbar").evaluate("el => (getComputedStyle(el).backdropFilter || getComputedStyle(el).webkitBackdropFilter) !== 'none'")
            page.locator("#jobSearch").focus()
            expect(page.locator("#jobSearch")).to_be_focused()
            assert page.locator(".search-field").evaluate("el => getComputedStyle(el).outlineStyle") == "solid"
            assert page.locator('.job-choice[aria-current="false"]').first.evaluate("el => getComputedStyle(el).borderBottomColor") == "rgba(0, 0, 0, 0)"
            page.emulate_media(reduced_motion="reduce")
            assert page.locator(".job-choice").first.evaluate("el => getComputedStyle(el).transitionDuration") in ("0s", "1e-05s")
            page.emulate_media(reduced_motion="no-preference")
            page.locator("#jobSearch").blur()
            if width > 640:
                toolbar = page.locator(".position-toolbar").bounding_box()
                assert toolbar["y"] + toolbar["height"] <= height + 2, toolbar
            page.screenshot(path=str(OUTPUT / f"{name}.png"))
            page.locator('[data-job-filter="pending"]').click()
            expect(page.locator(".job-choice")).to_have_count(5)
            page.locator('[data-job-filter="applied"]').click()
            expect(page.locator(".job-choice")).to_have_count(1)
            page.locator('[data-job-filter="all"]').click()
            page.locator("#jobSearch").fill("数据分析")
            expect(page.locator(".job-choice")).to_have_count(1)
            page.locator(".job-choice").click()
            expect(page.locator(".position-head h2")).to_have_text("数据分析实习生")
            expect(page.locator(".resume-task")).to_be_visible()
            expect(page.locator(".safe-fill-task")).to_be_disabled()
            explanation = page.locator(".secondary-explanation")
            expect(explanation).not_to_have_attribute("open", "")
            explanation.locator("summary").click()
            expect(explanation.locator("p")).to_be_visible()
            explanation.locator("summary").click()
            expect(page.locator("#fillBlocker")).to_be_visible()
            expect(page.locator("#positionContent")).to_contain_text("仅用于界面验证")
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(OUTPUT / f"{name}-detail.png"))
            if name == "mobile":
                page.locator(".detail-scroll").evaluate("el => { el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 150); }")
                page.screenshot(path=str(OUTPUT / "mobile-glass-scroll.png"))
                page.locator(".detail-scroll").evaluate("el => { el.scrollTop = 0; }")
            page.locator('[data-detail-tab="progress"]').click()
            expect(page.locator("#positionContent")).to_contain_text("还没有投递记录")
            page.locator('[data-detail-tab="match"]').click()
            expect(page.locator("#positionContent")).to_contain_text("匹配证据")
            page.locator('[data-detail-tab="project"]').click()
            expect(page.locator("#positionContent")).to_contain_text("看懂")
            if page.locator('[data-detail-action="run-project"]').count():
                page.locator('[data-detail-action="run-project"]').click()
                expect(page.locator("#positionContent")).to_contain_text("已跑通")
            expect(page.locator("#projectQuestion")).to_be_visible()
            page.locator("#projectQuestion").fill("修改后：哪个能力缺口最值得优先用项目验证？")
            page.locator("#projectThreshold").fill("80")
            page.locator('[data-detail-action="rerun-project"]').click()
            expect(page.locator("#positionContent")).to_contain_text("本人修改关键设置")
            expect(page.locator('[href*="/project-workshop/"]')).to_be_visible()
            page.screenshot(path=str(OUTPUT / f"{name}-project-workshop.png"))
            page.locator('[data-detail-action="status"]').click()
            expect(page.locator("#statusJob")).to_have_value("2")
            expect(page.locator("#statusConfirmed")).not_to_be_checked()
            page.locator("#workspaceNav").click()
            page.locator("#openCopilotButton").click()
            expect(page.locator("#copilotScope")).to_contain_text("#2")
            expect(page.locator("#copilotSend")).to_be_enabled()
            page.locator("#copilotInput").fill("将简历改为 1 页")
            with page.expect_response("**/api/copilot/message") as received:
                page.locator("#copilotSend").click()
            payload = received.value.json()
            assert payload["copilot"]["thread"]["messages"][-1]["job_id"] == 2
            assert payload["copilot"]["thread"]["messages"][-1]["actions"][0]["job_id"] == 2
            expect(page.locator(".copilot-message.assistant").last).to_contain_text("#2")
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(OUTPUT / f"{name}-agent.png"))
            page.locator('[data-copilot-kind="open_job"]').last.click()
            expect(page.locator(".position-head h2")).to_be_visible()
            expect(page.locator(".position-head h2")).to_be_focused()
            page.locator("#openCopilotButton").click()
            page.locator("#closeCopilotButton").click()
            if width <= 640:
                page.locator('[data-detail-action="back"]').click()
            page.locator("#jobSearch").fill("不存在的岗位")
            expect(page.locator("#jobs")).to_contain_text("没有符合条件")
            page.locator("[data-clear-search]").click()
            expect(page.locator(".job-choice")).to_have_count(6)
            page.locator('[data-select-job="1"]').click()
            page.locator('[data-detail-tab="progress"]').click()
            expect(page.locator("#positionContent")).to_contain_text("官网完成申请")
            page.locator("#overviewNav").click()
            page.screenshot(path=str(OUTPUT / f"{name}-overview.png"))
            for open_id, dialog_id, close_id in [
                ("openProfileButton", "profileDialog", "closeProfileButton"),
                ("openPreferencesButton", "preferencesDialog", "closePreferencesButton"),
                ("openSettingsButton", "settingsDialog", "closeSettingsButton"),
            ]:
                page.locator("#" + open_id).click()
                expect(page.locator("#" + dialog_id)).to_be_visible()
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                page.screenshot(path=str(OUTPUT / f"{name}-{dialog_id}.png"))
                if width <= 640:
                    assert page.locator("#" + close_id).bounding_box()["height"] >= 44
                page.locator("#" + close_id).click()
            assert not errors, errors
            report[name] = {"http_status": response.status, "overflow": False, "page_errors": errors, "job_selection_search_filters_project_crm_chat_dialogs": "passed"}
            page.close()
        browser.close()
    (OUTPUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="Leave an isolated fixture preview in the calling reusable session.")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    server = fixture_server(args.port if args.serve else 0)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(url, flush=True)
    if args.serve:
        server.serve_forever()
    else:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            run_checks(url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
