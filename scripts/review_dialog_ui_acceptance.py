"""Browser-level acceptance testing for review-dialog interactions.
Validates the four key requirements against real Dashboard DOM using Playwright:
1. Reveal resume button only reveals file in Explorer, never approves resume or opens job page.
2. Fast switching between Job A and B with simulated network latency maintains current job isolation.
3. Regeneration during review is rejected (409 Conflict) and never quietly replaces or falsely approves old version.
4. Application pack write failure accurately reports partial completion; retry succeeds with strictly one approval record.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import expect, sync_playwright

from job_agent.models.job_record import JobRecordInput
from job_agent.services.application_pack import ApplicationPackError
from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import load_profile, save_profile
from job_agent.services.runtime_config import RuntimeConfig, save_runtime_config
from tests.helpers import sample_profile
from tests.test_prepare_apply import scored_result

QA_DIR = ROOT / "build" / "work" / "review-ui-qa"

JD = """岗位职责：
1. 负责数据分析、策略运营与业务指标跟踪。
2. 配合团队完成实验与复盘报告。
任职要求：
1. 本科及以上，每周至少 4 天，连续实习 3 个月。
2. 熟练使用 SQL，具有良好逻辑思维能力。
"""


def create_qa_server(base_dir: Path):
    private_dir = base_dir / "private"
    output_dir = base_dir / "output"
    private_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    profile_path = private_dir / "profile.json"
    config_path = private_dir / "app-settings.json"
    save_profile(sample_profile(), profile_path)
    save_runtime_config(RuntimeConfig(), config_path)

    repo = JobRepository(private_dir / "jobs.sqlite3")

    # Insert Job 1
    job1 = repo.upsert_job(
        JobRecordInput(
            company="星图互娱",
            title="数据分析实习生",
            jd_text=JD,
            source="测试招聘",
            source_url="https://example.com/jobs/star-data-intern",
            location="上海",
        )
    )
    repo.add_match_result(
        job1.job_id,
        scored_result(company="星图互娱", title="数据分析实习生", score=88),
    )

    # Insert Job 2
    job2 = repo.upsert_job(
        JobRecordInput(
            company="远山智能",
            title="算法策略实习生",
            jd_text=JD,
            source="测试招聘",
            source_url="https://example.com/jobs/yuanshan-algo-intern",
            location="杭州",
        )
    )
    repo.add_match_result(
        job2.job_id,
        scored_result(company="远山智能", title="算法策略实习生", score=85),
    )

    server = create_dashboard_server(
        repo,
        output_dir=output_dir,
        private_dir=private_dir,
        profile_path=profile_path,
        port=0,
    )
    return server, repo, job1.job_id, job2.job_id, output_dir, profile_path


def run_ui_acceptance():
    QA_DIR.mkdir(parents=True, exist_ok=True)
    temp_dir = tempfile.TemporaryDirectory(prefix="ui-qa-", dir=QA_DIR)
    base_dir = Path(temp_dir.name)
    server, repo, job1_id, job2_id, output_dir, profile_path = create_qa_server(base_dir)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    server_url = f"http://127.0.0.1:{server.server_port}"

    results = {}

    # Prevent popping up external explorer.exe or browser during testing
    with mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True):
        with mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page", return_value=True):
            with mock.patch("os.startfile", create=True):
                try:
                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True)
                        context = browser.new_context(viewport={"width": 1440, "height": 900})
                        page = context.new_page()

                        network_requests = []
                        page.on("request", lambda r: network_requests.append({"method": r.method, "url": r.url}))
                        page.on("pageerror", lambda e: print("PAGE ERROR:", e))
                        page.on("console", lambda m: print("CONSOLE:", m.text))

                        page.goto(server_url, wait_until="networkidle")

                        # ------------------------------------------------------------------
                        # 准备阶段：切换到职位工作台，为 Job 1 和 Job 2 准备投递材料
                        # ------------------------------------------------------------------
                        page.locator("#workspaceNav").click()
                        expect(page.locator("#jobWorkspace")).to_be_visible()

                        # 准备 Job 1
                        page.locator(f'[data-select-job="{job1_id}"]').click()
                        expect(page.locator("#jobDetail h2")).to_contain_text("数据分析实习生")
                        prepare_btn = page.locator('button[data-detail-action="prepare"]')
                        expect(prepare_btn).to_be_visible()
                        prepare_btn.click()
                        # 等待准备完成，审阅按钮就绪
                        review_btn = page.locator('button[data-detail-action="review-and-open"]')
                        expect(review_btn).to_be_visible(timeout=15000)
                        expect(page.locator("#reviewDialog")).to_be_visible(timeout=10000)
                        page.locator("#closeReviewButton").click()

                        # 准备 Job 2
                        page.locator(f'[data-select-job="{job2_id}"]').click()
                        expect(page.locator("#jobDetail h2")).to_contain_text("算法策略实习生")
                        prepare_btn_2 = page.locator('button[data-detail-action="prepare"]')
                        expect(prepare_btn_2).to_be_visible()
                        prepare_btn_2.click()
                        expect(review_btn).to_be_visible(timeout=15000)
                        expect(page.locator("#reviewDialog")).to_be_visible(timeout=10000)
                        page.locator("#closeReviewButton").click()

                        page.screenshot(path=str(QA_DIR / "01_both_jobs_prepared.png"))

                        # ------------------------------------------------------------------
                        # 验证 1：“打开简历所在文件夹”只定位文件，不批准简历、不打开招聘页
                        # ------------------------------------------------------------------
                        # 切回 Job 1 并打开审阅面板
                        page.locator(f'[data-select-job="{job1_id}"]').click()
                        expect(page.locator("#jobDetail h2")).to_contain_text("数据分析实习生")
                        page.locator('button[data-detail-action="review-and-open"]').click()

                        dialog = page.locator("#reviewDialog")
                        expect(dialog).to_be_visible()
                        expect(page.locator("#reviewDialogTitle")).to_contain_text("数据分析实习生")

                        # 初始断言：状态为 ready_to_apply，无批准记录文件
                        app1 = repo.get_application(job1_id)
                        assert app1 is not None and app1.status == "ready_to_apply"
                        job1_apps_dir = output_dir / "applications"
                        approved_files_before = list(job1_apps_dir.rglob("resume-version-approved-*.json"))
                        assert len(approved_files_before) == 0

                        # 点击【打开简历所在文件夹】
                        network_requests.clear()
                        reveal_btn = page.locator("#reviewRevealBtn")
                        expect(reveal_btn).to_be_enabled()
                        reveal_btn.click()

                        # 等待网络交互完成
                        page.wait_for_timeout(500)

                        # 审查网络调用：严格只调用 reveal-resume，绝不调用 review-and-open
                        reveal_calls = [r for r in network_requests if f"/api/jobs/{job1_id}/reveal-resume" in r["url"]]
                        review_and_open_calls = [r for r in network_requests if "review-and-open" in r["url"]]
                        assert len(reveal_calls) == 1, f"Expected 1 reveal-resume call, got: {reveal_calls}"
                        assert len(review_and_open_calls) == 0, "revealBtn 绝不能调用 review-and-open"

                        # 审查数据隔离：无批准记录文件生成，申请状态绝不变成已投递
                        approved_files_after = list(job1_apps_dir.rglob("resume-version-approved-*.json"))
                        assert len(approved_files_after) == 0, "定位文件夹绝不能生成批准记录文件"
                        app1_after = repo.get_application(job1_id)
                        assert app1_after.status == "ready_to_apply", "定位文件夹绝不能推进投递状态"

                        page.screenshot(path=str(QA_DIR / "02_reveal_only_verified.png"))
                        page.locator("#closeReviewButton").click()
                        expect(dialog).not_to_be_visible()

                        results["requirement_1_reveal_isolation"] = {
                            "status": "PASS",
                            "reveal_called": True,
                            "review_and_open_called": False,
                            "approved_records_created": len(approved_files_after),
                            "application_status": app1_after.status,
                        }

                        # ------------------------------------------------------------------
                        # 验证 2：快速切换两个岗位并模拟请求延迟后，预览及全部按钮仍属于当前岗位
                        # ------------------------------------------------------------------
                        held_route = []

                        def handle_job_detail(route):
                            url = route.request.url
                            if f"/api/jobs/{job1_id}/detail" in url and held_route is not None:
                                # 拦截并滞留 Job 1 的异步详情请求
                                held_route.append(route)
                            else:
                                route.continue_()

                        page.route("**/api/jobs/*/detail", handle_job_detail)

                        # 1. 触发 Job 1 审阅（请求被挂起）
                        page.locator(f'[data-select-job="{job1_id}"]').click()
                        page.locator('button[data-detail-action="review-and-open"]').click()

                        # 2. 立即关闭并快速切换至 Job 2 打开审阅
                        page.locator("#closeReviewButton").click()
                        page.locator(f'[data-select-job="{job2_id}"]').click()
                        page.locator('button[data-detail-action="review-and-open"]').click()

                        # 3. Job 2 正常返回并渲染
                        expect(page.locator("#reviewDialogTitle")).to_contain_text("算法策略实习生")
                        page.wait_for_timeout(500)
                        frame_src_job2 = page.locator("#reviewPdfFrame").get_attribute("src")
                        assert f"/resume-draft/{job2_id}/pdf" in frame_src_job2

                        # 4. 释放被拦截滞留的 Job 1 详情响应
                        print(f"DEBUG: len(held_route)={len(held_route)}, urls={[r.request.url for r in held_route]}")
                        assert len(held_route) >= 1, "Job 1 detail 应已被拦截暂存"
                        while held_route:
                            delayed_route = held_route.pop()
                            try:
                                delayed_route.continue_()
                            except Exception as e:
                                print("continue_ error:", e)
                        page.unroute("**/api/jobs/*/detail")

                        # 等待潜在的乱序 DOM 冲突平息
                        page.wait_for_timeout(500)

                        # 5. 断言：迟到的 Job 1 响应绝未篡改当前 Job 2 弹窗的标题与 PDF 预览！
                        expect(page.locator("#reviewDialogTitle")).to_contain_text("算法策略实习生")
                        frame_src_after = page.locator("#reviewPdfFrame").get_attribute("src")
                        assert f"/resume-draft/{job2_id}/pdf" in frame_src_after, f"预期 Job 2 iframe，实际为 {frame_src_after}"

                        # 6. 断言：弹窗内的操作按钮严格绑定为 Job 2
                        network_requests.clear()
                        page.locator("#reviewRevealBtn").click()
                        page.wait_for_timeout(300)
                        reveal_urls = [r["url"] for r in network_requests if "reveal-resume" in r["url"]]
                        assert any(f"/api/jobs/{job2_id}/reveal-resume" in u for u in reveal_urls)
                        assert not any(f"/api/jobs/{job1_id}/reveal-resume" in u for u in reveal_urls)

                        page.evaluate("window.confirm = () => true")
                        network_requests.clear()
                        page.locator("#reviewMarkAppliedBtn").click()
                        page.wait_for_timeout(300)
                        applied_urls = [r["url"] for r in network_requests if "/api/applications/" in r["url"]]
                        assert any(f"/api/applications/{job2_id}/status" in u for u in applied_urls)
                        assert not any(f"/api/applications/{job1_id}/status" in u for u in applied_urls)

                        page.screenshot(path=str(QA_DIR / "03_out_of_order_session_isolated.png"))
                        expect(dialog).not_to_be_visible()

                        results["requirement_2_session_isolation"] = {
                            "status": "PASS",
                            "dialog_title_retained": "算法策略实习生",
                            "pdf_src_retained": frame_src_after,
                            "reveal_targeted_job": job2_id,
                            "mark_applied_targeted_job": job2_id,
                        }

                        # ------------------------------------------------------------------
                        # 验证 3：审阅期间重新生成简历，旧版本不能被静默替换或误批准
                        # ------------------------------------------------------------------
                        # 为 Job 1 打开审阅面板
                        page.locator(f'[data-select-job="{job1_id}"]').click()
                        page.locator('button[data-detail-action="review-and-open"]').click()
                        expect(page.locator("#reviewDialogTitle")).to_contain_text("数据分析实习生")
                        page.wait_for_timeout(500)

                        # 获取 Job 1 当前 v1 版本的哈希
                        detail_v1 = page.evaluate(f"fetch('/api/jobs/{job1_id}/detail').then(r => r.json())")
                        v1_hash = detail_v1["resume_version"]["pdf_sha256"]

                        # 保持审阅弹窗打开不动，后台更新 Profile 并重新生成 Job 1 简历
                        prof = load_profile(profile_path)
                        prof.person.display_name = "候选人二代"
                        save_profile(prof, profile_path, overwrite=True)

                        regen_res = page.evaluate(f"""
                            (async () => {{
                                const dash = await fetch('/api/dashboard').then(r => r.json());
                                const token = dash.action_token;
                                const res = await fetch('/api/jobs/{job1_id}/resume-draft', {{
                                    method: 'POST',
                                    headers: {{'Content-Type': 'application/json', 'X-Job-Agent-Token': token}},
                                    body: JSON.stringify({{confirmed: true}})
                                }});
                                return await res.json();
                            }})()
                        """)
                        assert regen_res.get("ok") is True, f"Regeneration failed: {regen_res}"

                        detail_v2 = page.evaluate(f"fetch('/api/jobs/{job1_id}/detail').then(r => r.json())")
                        v2_hash = detail_v2["resume_version"]["pdf_sha256"]
                        assert v1_hash != v2_hash, f"后台重新生成后哈希应发生改变，当前为 {v1_hash} vs {v2_hash}"

                        # 关键验证 A：哈希强校验（伪造/不匹配的哈希返回 404，指定哈希下载返回对应真实版本，不静默替换）
                        fake_hash = "0" * 64
                        fake_pdf_status = page.evaluate(f"""
                            fetch('/resume-draft/{job1_id}/pdf?v={fake_hash}').then(r => r.status)
                        """)
                        assert fake_pdf_status == 404, f"未知或失配哈希必须返回 404，实际为 {fake_pdf_status}"

                        v2_pdf_status = page.evaluate(f"""
                            fetch('/resume-draft/{job1_id}/pdf?v={v2_hash}').then(r => r.status)
                        """)
                        assert v2_pdf_status == 200, f"当前新哈希 PDF 下载必须返回 200，实际为 {v2_pdf_status}"

                        # 关键验证 B：前端弹窗闭包内仍持有 v1，点击【确认并打开招聘页】
                        page.locator("#reviewOpenPageBtn").click()

                        # 断言：服务端拦截并返回 409，界面明确提示版本不匹配
                        expect(page.locator("#alert")).to_be_visible()
                        expect(page.locator("#alert")).to_contain_text("已有更新的简历草稿生成，请重新打开审阅面板")

                        # 断言：旧版本 v1 绝未被生成批准文件
                        job1_manifests = list(job1_apps_dir.rglob("resume-version-approved-*.json"))
                        assert len(job1_manifests) == 0, "旧版本哈希失配时绝不能被误批准"

                        page.screenshot(path=str(QA_DIR / "04_regeneration_rejected.png"))
                        results["requirement_3_regeneration_rejection"] = {
                            "status": "PASS",
                            "v1_hash": v1_hash,
                            "v2_hash": v2_hash,
                            "fake_version_download_status": fake_pdf_status,
                            "v2_download_status": v2_pdf_status,
                            "conflict_alert_displayed": True,
                            "approved_records_created": len(job1_manifests),
                        }

                        # ------------------------------------------------------------------
                        # 验证 4：材料包写入失败后，界面准确提示部分完成；重试成功且只存在一份批准记录
                        # ------------------------------------------------------------------
                        # 409 后前端自动刷新加载了新版 v2，按钮重新启用
                        expect(page.locator("#reviewOpenPageBtn")).to_be_enabled(timeout=5000)

                        # 第 1 次点击：模拟 write_application_pack 异常
                        with mock.patch(
                            "job_agent.services.dashboard_routes.jobs_api.write_application_pack",
                            side_effect=ApplicationPackError("界面验收模拟：材料包磁盘写入异常"),
                        ):
                            page.locator("#reviewOpenPageBtn").click()

                            # 界面准确提示部分完成（包含材料包失败原因及安全重试指引）
                            expect(page.locator("#alert")).to_be_visible()
                            expect(page.locator("#alert")).to_contain_text("材料包同步失败")
                            expect(page.locator("#alert")).to_contain_text("可重试恢复未完成步骤")

                            # 弹窗保持打开，确认按钮重新可用供用户重试
                            expect(page.locator("#reviewOpenPageBtn")).to_be_enabled()

                            # 检查磁盘状态：此时已有 1 份视觉审批记录落盘
                            approved_attempt_1 = list(job1_apps_dir.rglob("resume-version-approved-*.json"))
                            assert len(approved_attempt_1) == 1, f"初次审批已落盘，应有 1 份记录，实际有 {len(approved_attempt_1)}"

                        # 第 2 次点击：故障恢复后点击重试
                        page.locator("#reviewOpenPageBtn").click()

                        # 弹窗成功关闭，界面提示全部完成
                        expect(dialog).not_to_be_visible()
                        expect(page.locator("#alert")).to_contain_text("招聘页与 PDF 已打开")

                        # 关键断言：重试成功后，磁盘上严格只存在 1 份批准记录，无重复记录
                        approved_attempt_2 = list(job1_apps_dir.rglob("resume-version-approved-*.json"))
                        assert len(approved_attempt_2) == 1, f"重试后必须保持严格只有 1 份批准记录，实际有 {len(approved_attempt_2)}"

                        page.screenshot(path=str(QA_DIR / "05_retry_success_single_approval.png"))
                        results["requirement_4_retry_and_single_approval"] = {
                            "status": "PASS",
                            "partial_completion_displayed": True,
                            "approved_record_count_attempt_1": len(approved_attempt_1),
                            "approved_record_count_attempt_2": len(approved_attempt_2),
                            "retry_succeeded": True,
                        }

                        browser.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=3)
                    temp_dir.cleanup()

    report_path = QA_DIR / "ui_acceptance_report.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n--- UI ACCEPTANCE RESULT ---")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return results


if __name__ == "__main__":
    run_ui_acceptance()
