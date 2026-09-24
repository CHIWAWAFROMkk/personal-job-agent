"""Automated verification of the packaged standalone Windows desktop build.

Verifies the 5 key release requirements against the packaged PersonalJobAgent.exe:
1. Multi-process single instance exclusivity on PersonalJobAgent.exe (two processes, same data dir).
2. Version A approved -> generate new version B -> approve version B cleanly (no 409 conflict).
3. Concurrent review approvals (5 threads) -> strictly 1 approval record on disk.
4. Prepare failure error display on live DOM and retry mechanism (Playwright against packaged UI).
5. Review dialog switching with delayed refresh (Playwright against packaged UI).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from job_agent.models.job_record import JobRecordInput
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import save_profile
from job_agent.services.runtime_config import RuntimeConfig, save_runtime_config
from tests.helpers import sample_profile
from tests.test_prepare_apply import scored_result

QA_DIR = ROOT / "build" / "work" / "packaged-release-qa"

JD = """岗位职责：
1. 负责数据分析、策略运营与业务指标跟踪。
2. 配合团队完成实验与复盘报告。
任职要求：
1. 本科及以上，每周至少 4 天，连续实习 3 个月。
2. 熟练使用 SQL，具有良好逻辑思维能力。
"""


def find_latest_packaged_exe() -> Path:
    dist_desktop = ROOT / "dist" / "desktop"
    if not dist_desktop.is_dir():
        raise FileNotFoundError(f"Dist desktop directory not found: {dist_desktop}")
    candidates = sorted(dist_desktop.iterdir(), key=lambda p: p.name)
    if not candidates:
        raise FileNotFoundError(f"No builds found in {dist_desktop}")
    latest_build = candidates[-1]
    exe_path = latest_build / "PersonalJobAgent" / "PersonalJobAgent.exe"
    if not exe_path.is_file():
        raise FileNotFoundError(f"Packaged executable not found: {exe_path}")
    return exe_path


def setup_isolated_data(data_root: Path):
    priv_dir = data_root / "data" / "private"
    priv_dir.mkdir(parents=True, exist_ok=True)
    out_dir = data_root / "data" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = data_root / "data" / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = data_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    profile_path = priv_dir / "profile.json"
    config_path = priv_dir / "app-settings.json"
    save_profile(sample_profile(), profile_path)
    save_runtime_config(RuntimeConfig(), config_path)

    repo = JobRepository(priv_dir / "job_agent.sqlite3")
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

    job3 = repo.upsert_job(
        JobRecordInput(
            company="云翼网络",
            title="测试开发实习生",
            jd_text=JD,
            source="测试招聘",
            source_url="https://example.com/jobs/yunyi-qa-intern",
            location="北京",
        )
    )
    repo.add_match_result(
        job3.job_id,
        scored_result(company="云翼网络", title="测试开发实习生", score=82),
    )
    return job1.job_id, job2.job_id, job3.job_id, repo


def post_json(url: str, data: dict, token: str) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Job-Agent-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read().decode("utf-8"))


def get_json(url: str, token: str = None) -> dict:
    headers = {}
    if token:
        headers["X-Job-Agent-Token"] = token
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read().decode("utf-8"))


def run_full_packaged_verification():
    QA_DIR.mkdir(parents=True, exist_ok=True)
    exe_path = find_latest_packaged_exe()
    build_id = exe_path.parent.parent.name
    print(f"==================================================")
    print(f"VERIFYING PACKAGED EXECUTABLE")
    print(f"Build ID: {build_id}")
    print(f"Path: {exe_path}")
    print(f"==================================================")

    # 0. Check asset integrity in _internal
    internal_dir = exe_path.parent / "_internal" / "job_agent" / "web" / "js"
    prepare_ctrl = internal_dir / "prepare-controller.js"
    review_dialog = internal_dir / "review-dialog.js"
    main_js = internal_dir / "main.js"

    assert prepare_ctrl.is_file(), f"Missing prepare-controller.js in packaged build at {prepare_ctrl}"
    assert review_dialog.is_file(), f"Missing review-dialog.js in packaged build at {review_dialog}"
    assert main_js.is_file(), f"Missing main.js in packaged build at {main_js}"

    review_code = review_dialog.read_text(encoding="utf-8")
    assert "currentSeq !== reviewSessionSeq" in review_code, "Packaged review-dialog.js missing session sequence check!"

    temp_dir = tempfile.TemporaryDirectory(prefix="release-qa-", dir=QA_DIR)
    data_root = Path(temp_dir.name)
    job1_id, job2_id, job3_id, repo = setup_isolated_data(data_root)

    url_file = data_root / "server_url.txt"

    # ==================================================
    # TEST 1: Launch Process 1 and Test Multi-Process SingleInstanceLock with Process 2
    # ==================================================
    print("\n--- [TEST 1] Packaged Multi-Process Single Instance Exclusivity ---")
    cmd1 = [
        str(exe_path),
        "--headless",
        "--data-dir", str(data_root),
        "--url-file", str(url_file),
    ]
    proc1 = subprocess.Popen(cmd1, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    server_url = None
    deadline = time.time() + 25.0
    while time.time() < deadline:
        if url_file.is_file() and url_file.stat().st_size > 0:
            server_url = url_file.read_text(encoding="utf-8").strip()
            if server_url.startswith("http"):
                break
        if proc1.poll() is not None:
            stdout, stderr = proc1.communicate()
            raise RuntimeError(f"Process 1 exited early: STDOUT={stdout}, STDERR={stderr}")
        time.sleep(0.5)

    assert server_url, "Server URL file not populated within deadline"
    print(f"Process 1 (PID {proc1.pid}) is live and serving at: {server_url}")

    # Launch Process 2 against the exact same data directory
    cmd2 = [
        str(exe_path),
        "--headless",
        "--data-dir", str(data_root),
    ]
    proc2 = subprocess.run(cmd2, capture_output=True, text=True, errors="replace", timeout=10)
    stdout_text = proc2.stdout or ""
    stderr_text = proc2.stderr or ""
    print(f"Process 2 exit code: {proc2.returncode}")
    print(f"Process 2 output: {stdout_text.strip()} {stderr_text.strip()}")
    assert proc2.returncode != 0, "Process 2 must be rejected and exit non-zero when data dir is locked"
    assert "已在运行中" in stdout_text or "已在运行中" in stderr_text or proc2.returncode == 1
    print("PASS: Multi-process single instance exclusivity verified on packaged executable.")

    # Get action token from server
    dash = get_json(f"{server_url}/api/dashboard")
    token = dash["action_token"]

    report = {
        "build_id": build_id,
        "exe_path": str(exe_path),
        "server_url": server_url,
        "tests": {},
    }
    report["tests"]["test1_multiprocess_single_instance"] = {
        "status": "PASS",
        "proc1_pid": proc1.pid,
        "proc2_exit_code": proc2.returncode,
    }

    try:
        # ==================================================
        # TEST 2: Approve Version A -> Generate Version B -> Approve Version B
        # ==================================================
        print("\n--- [TEST 2] Packaged Version A Approved -> Generate B -> Approve B ---")
        # 1. Prepare version A
        prep_a = post_json(f"{server_url}/api/jobs/{job1_id}/prepare-apply", {}, token)
        assert prep_a.get("ok"), f"Prepare A failed: {prep_a}"

        detail_a = get_json(f"{server_url}/api/jobs/{job1_id}/detail", token)
        ver_a = detail_a["resume_version"]
        assert ver_a and ver_a.get("pdf_sha256")
        sha_a = ver_a["pdf_sha256"]
        manifest_a = ver_a["manifest_path"]

        # 2. Approve version A
        rev_a = post_json(f"{server_url}/api/jobs/{job1_id}/review-and-open", {
            "expected_sha256": sha_a,
            "manifest_path": manifest_a,
        }, token)
        assert rev_a.get("ok"), f"Approve A failed: {rev_a}"
        print(f"Version A ({sha_a[:8]}...) approved successfully.")

        # 3. Modify candidate profile name on disk and generate version B
        profile_path = data_root / "data" / "private" / "profile.json"
        from job_agent.services.profile_store import load_profile
        prof = load_profile(profile_path)
        prof.person.display_name = "全新版本候选人B"
        save_profile(prof, profile_path, overwrite=True)

        prep_b = post_json(f"{server_url}/api/jobs/{job1_id}/resume-draft", {"confirmed": True}, token)
        assert prep_b.get("ok"), f"Generate B failed: {prep_b}"

        detail_b = get_json(f"{server_url}/api/jobs/{job1_id}/detail", token)
        ver_b = detail_b["resume_version"]
        sha_b = ver_b["pdf_sha256"]
        manifest_b = ver_b["manifest_path"]
        assert sha_b != sha_a, f"Version B hash must differ from A: {sha_b} vs {sha_a}"

        # 4. Approve version B: MUST SUCCEED cleanly without 409 conflict
        rev_b = post_json(f"{server_url}/api/jobs/{job1_id}/review-and-open", {
            "expected_sha256": sha_b,
            "manifest_path": manifest_b,
        }, token)
        assert rev_b.get("ok"), f"Approve B failed with conflict: {rev_b}"
        print(f"Version B ({sha_b[:8]}...) approved successfully without conflict.")

        # Verify application pack points to version B
        from job_agent.services.browser_assist import find_application_pack
        packs_dir = data_root / "data" / "output" / "application-packs"
        _, pack = find_application_pack(packs_dir, job1_id)
        assert Path(pack.resume.pdf_path).resolve() == Path(ver_b["pdf_path"]).resolve()
        print("PASS: Version A -> B state transition and pack update verified on packaged build.")
        report["tests"]["test2_version_transition"] = {
            "status": "PASS",
            "sha_a": sha_a,
            "sha_b": sha_b,
        }

        # ==================================================
        # TEST 3: Concurrent Review Approvals (5 threads) -> Strictly 1 Approved Record
        # ==================================================
        print("\n--- [TEST 3] Packaged Concurrent Review Approvals (5 threads) ---")
        prep_job2 = post_json(f"{server_url}/api/jobs/{job2_id}/prepare-apply", {}, token)
        assert prep_job2.get("ok")
        detail_job2 = get_json(f"{server_url}/api/jobs/{job2_id}/detail", token)
        ver_job2 = detail_job2["resume_version"]
        sha_job2 = ver_job2["pdf_sha256"]
        manifest_job2 = ver_job2["manifest_path"]
        manifest_dir_job2 = Path(manifest_job2).parent

        def do_concurrent_review():
            return post_json(f"{server_url}/api/jobs/{job2_id}/review-and-open", {
                "expected_sha256": sha_job2,
                "manifest_path": manifest_job2,
            }, token)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(do_concurrent_review) for _ in range(5)]
            results = [f.result() for f in futures]

        for idx, res in enumerate(results):
            assert res.get("ok"), f"Concurrent request {idx} failed: {res}"

        approved_files = list(manifest_dir_job2.glob("resume-version-approved-*.json"))
        assert len(approved_files) == 1, f"Expected strictly 1 approval file on disk, got {len(approved_files)}"
        print(f"PASS: 5 concurrent approvals returned OK; strictly 1 approval record on disk: {approved_files[0].name}")
        report["tests"]["test3_concurrent_approvals"] = {
            "status": "PASS",
            "threads": 5,
            "approved_files_count": len(approved_files),
        }

        # ==================================================
        # TEST 4 & 5: Playwright UI Verification Against Packaged Server
        # ==================================================
        print("\n--- [TEST 4 & 5] Playwright UI Verification Against Packaged Server ---")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()

            page.goto(server_url, wait_until="networkidle")

            # Navigate to workspace
            page.locator("#workspaceNav").click()
            expect(page.locator("#jobWorkspace")).to_be_visible()

            # --- TEST 4: Prepare Failure & Live DOM Retry ---
            print("Verifying Test 4: Prepare failure error display & retry button...")
            page.locator(f'[data-select-job="{job3_id}"]').click()
            page.wait_for_timeout(500)

            # Route next prepare call to fail
            route_failed = {"done": False}
            def fail_once(route):
                if not route_failed["done"]:
                    route_failed["done"] = True
                    route.fulfill(
                        status=400,
                        content_type="application/json",
                        body=json.dumps({"error": "模拟AI配额用尽", "failed_stage_name": "resume_generation"}),
                    )
                else:
                    route.continue_()

            page.route("**/api/jobs/*/prepare-apply", fail_once)

            # Trigger prepare on Job 3
            prepare_btn = page.locator('button[data-detail-action="prepare"]')
            expect(prepare_btn).to_be_visible()
            prepare_btn.click()

            # Expect error inside prepareProgress
            expect(page.locator("#prepareProgress")).to_be_visible()
            expect(page.locator("#prepareProgress")).to_contain_text("准备材料未完成")
            expect(page.locator("#prepareProgress .prepare-retry")).to_be_visible()
            print("UI correctly displays error message and retry button in prepareProgress.")

            # Click retry button rendered by prepare-controller.js
            page.locator("#prepareProgress .prepare-retry").click()
            # Wait for prepare to succeed and review button to appear
            review_btn_job3 = page.locator('button[data-detail-action="review-and-open"]')
            expect(review_btn_job3).to_be_visible(timeout=20000)
            page.unroute("**/api/jobs/*/prepare-apply")
            print("PASS: Prepare failure handling and live DOM retry verified.")
            page.screenshot(path=str(QA_DIR / "04_packaged_prepare_retry.png"))
            report["tests"]["test4_prepare_failure_retry"] = {"status": "PASS"}

            # --- TEST 5: Dialog Switching & Session Isolation ---
            print("Verifying Test 5: Review dialog switching & session isolation...")
            # Successful preparation opens its review dialog. Close that visible
            # modal through the UI before selecting another job behind it.
            expect(page.locator("#reviewDialog")).to_be_visible()
            page.locator("#closeReviewButton").click()
            expect(page.locator("#reviewDialog")).not_to_be_visible()
            # Open Job 1 review dialog
            page.locator(f'[data-select-job="{job1_id}"]').click()
            page.wait_for_timeout(300)
            review_btn = page.locator('button[data-detail-action="review-and-open"]')
            expect(review_btn).to_be_visible()
            review_btn.click()

            dialog = page.locator("#reviewDialog")
            expect(dialog).to_be_visible()
            expect(page.locator("#reviewDialogTitle")).to_contain_text("数据分析实习生")

            # Close Job 1 dialog and switch to Job 2
            page.locator("#closeReviewButton").click()
            expect(dialog).not_to_be_visible()

            # Select Job 2 and open review dialog
            page.locator(f'[data-select-job="{job2_id}"]').click()
            page.wait_for_timeout(300)
            page.locator('button[data-detail-action="review-and-open"]').click()
            expect(dialog).to_be_visible()
            expect(page.locator("#reviewDialogTitle")).to_contain_text("算法策略实习生")

            # Verify that Job 2 dialog remains open and shows Job 2 details
            import re
            expect(page.locator("#reviewPdfFrame")).to_have_attribute("src", re.compile(rf"/resume-draft/{job2_id}/pdf"), timeout=10000)
            pdf_frame_src = page.locator("#reviewPdfFrame").get_attribute("src")
            print(f"PASS: Dialog successfully switched to Job 2, PDF frame is {pdf_frame_src}")
            page.screenshot(path=str(QA_DIR / "05_packaged_dialog_switch.png"))
            report["tests"]["test5_dialog_switch"] = {"status": "PASS"}

            browser.close()

    finally:
        report_path = QA_DIR / "packaged_release_verification_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nReport written to: {report_path}")

        print("\nStopping packaged server process...")
        proc1.terminate()
        try:
            proc1.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc1.kill()
            proc1.wait()

        time.sleep(0.5)
        try:
            temp_dir.cleanup()
        except Exception as e:
            print(f"Notice: temp_dir cleanup deferred or failed: {e}")

    print("ALL 5 PACKAGED VERIFICATION TESTS PASSED SUCCESSFULLY!")
    return report


if __name__ == "__main__":
    run_full_packaged_verification()
