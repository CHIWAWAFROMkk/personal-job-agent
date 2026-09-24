"""Empirical verification of cross-version database compatibility.
Tests whether the OLD packaged app (0.8.5-trial-20260920-221809) can safely read and operate
on an isolated database after the NEW packaged app (20260921-015435-225) has written to it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
QA_DIR = ROOT / "build" / "work" / "compat-qa"

OLD_EXE = Path(os.getenv("COMPAT_OLD_EXE", ""))
NEW_EXE = Path(os.getenv("COMPAT_NEW_EXE", ""))
REAL_DB = Path(os.getenv("COMPAT_REAL_DB", ""))


def start_headless(exe: Path, data_dir: Path, port: int = 0) -> tuple[subprocess.Popen, str]:
    url_file = data_dir / f"server_url_{exe.stem}_{int(time.time()*1000)}.txt"
    cmd = [
        str(exe),
        "--headless",
        "--data-dir", str(data_dir),
        "--url-file", str(url_file),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 20.0
    url = None
    while time.time() < deadline:
        if url_file.is_file() and url_file.stat().st_size > 0:
            url = url_file.read_text(encoding="utf-8").strip()
            if url.startswith("http"):
                break
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise RuntimeError(f"Process {exe.parent.name} exited early ({proc.returncode}):\n{stderr}")
        time.sleep(0.3)
    if not url:
        proc.kill()
        raise TimeoutError(f"Server {exe.parent.name} timed out starting.")
    return proc, url


def stop_proc(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_compat_test():
    QA_DIR.mkdir(parents=True, exist_ok=True)
    test_data_dir = QA_DIR / "isolated_data"
    if test_data_dir.exists():
        shutil.rmtree(test_data_dir, ignore_errors=True)

    priv_dir = test_data_dir / "data" / "private"
    priv_dir.mkdir(parents=True, exist_ok=True)
    (test_data_dir / "data" / "output").mkdir(parents=True, exist_ok=True)
    (test_data_dir / "data" / "inbox").mkdir(parents=True, exist_ok=True)
    (test_data_dir / "logs").mkdir(parents=True, exist_ok=True)

    # 1. Copy real database in read-only mode to isolated test directory
    print(f"Copying real db from {REAL_DB} to isolated test dir...")
    shutil.copy2(REAL_DB, priv_dir / "job_agent.sqlite3")

    # Copy profile and settings if available
    real_profile = REAL_DB.parent / "profile.json" if REAL_DB.is_file() else Path()
    real_settings = REAL_DB.parent / "app-settings.json" if REAL_DB.is_file() else Path()
    if real_profile.is_file():
        shutil.copy2(real_profile, priv_dir / "profile.json")
    if real_settings.is_file():
        shutil.copy2(real_settings, priv_dir / "app-settings.json")

    results = {
        "old_exe": str(OLD_EXE),
        "new_exe": str(NEW_EXE),
        "real_db_source": str(REAL_DB),
        "new_app_writes": {},
        "old_app_reads": {},
        "compatibility_verdict": "FAIL",
    }

    # 2. Start NEW app and write new data
    print("Starting NEW app to perform writes on isolated database...")
    new_proc, new_url = start_headless(NEW_EXE, test_data_dir)
    try:
        # Check dashboard
        dash_res = json.loads(urllib.request.urlopen(f"{new_url}api/dashboard", timeout=5).read().decode("utf-8"))
        token = dash_res.get("action_token", "")
        initial_job_count = len(dash_res.get("tracked_jobs", []))
        print(f"NEW app started successfully. Initial tracked jobs in db: {initial_job_count}")

        # Insert a new test job via API
        new_job_payload = json.dumps({
            "company": "跨版本兼容性校验企业",
            "title": "兼容性验证分析师",
            "jd_text": "负责多版本数据库结构一致性、字段向前/向后兼容性验证与回归分析。",
            "source": "兼容性回归测试",
            "source_url": "https://example.com/compat-test-job",
            "location": "上海",
        }).encode("utf-8")
        req_import = urllib.request.Request(
            f"{new_url}api/jobs/import-parsed",
            data=new_job_payload,
            headers={"Content-Type": "application/json", "X-Job-Agent-Token": token},
            method="POST",
        )
        import_res = json.loads(urllib.request.urlopen(req_import, timeout=5).read().decode("utf-8"))
        new_job_id = import_res.get("job", {}).get("job_id") or import_res.get("job_id")
        print(f"NEW app imported new test job #{new_job_id}")

        results["new_app_writes"] = {
            "initial_job_count": initial_job_count,
            "new_job_id": new_job_id,
            "import_status": "SUCCESS",
        }
    finally:
        print("Stopping NEW app...")
        stop_proc(new_proc)

    # 3. Start OLD app on the SAME data directory and verify it reads the data without error!
    print(f"Starting OLD app ({OLD_EXE.parent.name}) on the written database...")
    old_proc, old_url = start_headless(OLD_EXE, test_data_dir)
    try:
        old_dash = json.loads(urllib.request.urlopen(f"{old_url}api/dashboard", timeout=5).read().decode("utf-8"))
        old_jobs = old_dash.get("tracked_jobs", [])
        print(f"OLD app started successfully. Read {len(old_jobs)} jobs from database.")

        # Find the job written by the new app
        matching_job = next((j for j in old_jobs if j.get("job_id") == new_job_id), None)
        assert matching_job is not None, f"OLD app failed to find job #{new_job_id} written by NEW app!"
        assert matching_job.get("company") == "跨版本兼容性校验企业"
        print(f"OLD app verified reading job #{new_job_id}: {matching_job.get('title')}")

        # Fetch detail of the new job through OLD app
        old_detail = json.loads(urllib.request.urlopen(f"{old_url}api/jobs/{new_job_id}/detail", timeout=5).read().decode("utf-8"))
        assert old_detail.get("job", {}).get("company") == "跨版本兼容性校验企业"
        print(f"OLD app successfully queried detail of new job.")

        results["old_app_reads"] = {
            "dashboard_status": 200,
            "total_jobs_read": len(old_jobs),
            "new_job_retrieved": True,
            "new_job_title": matching_job.get("title"),
            "detail_query_success": True,
        }
        results["compatibility_verdict"] = "PASS"
    finally:
        print("Stopping OLD app...")
        stop_proc(old_proc)

    # Clean up isolated data
    shutil.rmtree(test_data_dir, ignore_errors=True)

    report_file = QA_DIR / "cross_version_compatibility_report.json"
    report_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n--- CROSS VERSION COMPATIBILITY REPORT ---")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return results


if __name__ == "__main__":
    run_compat_test()
