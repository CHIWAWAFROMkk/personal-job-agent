"""Read-only verification of the deployed PersonalJobAgent against production data.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEPLOYED_EXE = Path(os.getenv("DEPLOYED_EXE", ""))
PROD_DATA = Path(os.getenv("PROD_DATA", os.path.expandvars(r"%LOCALAPPDATA%\PersonalJobAgent") if os.name == "nt" else ""))
URL_FILE = PROD_DATA / "logs" / "verify_server_url.txt" if PROD_DATA else Path("verify_server_url.txt")


def verify_production_deployment():
    assert DEPLOYED_EXE.is_file(), f"Deployed exe not found: {DEPLOYED_EXE}"
    assert PROD_DATA.is_dir(), f"Production data dir not found: {PROD_DATA}"

    if URL_FILE.exists():
        URL_FILE.unlink()

    cmd = [
        str(DEPLOYED_EXE),
        "--headless",
        "--data-dir", str(PROD_DATA),
        "--url-file", str(URL_FILE),
    ]

    print(f"Starting deployed exe in headless mode: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    server_url = None
    deadline = time.time() + 25.0
    while time.time() < deadline:
        if URL_FILE.is_file() and URL_FILE.stat().st_size > 0:
            server_url = URL_FILE.read_text(encoding="utf-8").strip()
            if server_url.startswith("http"):
                break
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise RuntimeError(f"Process exited early ({proc.returncode}):\n{stderr}")
        time.sleep(0.3)

    if not server_url:
        proc.kill()
        raise TimeoutError("Server failed to write url file within deadline.")

    print(f"Deployed app is live at: {server_url}")

    results = {
        "exe_path": str(DEPLOYED_EXE),
        "data_dir": str(PROD_DATA),
        "server_url": server_url,
    }

    try:
        # 1. Fetch dashboard
        with urllib.request.urlopen(f"{server_url}api/dashboard", timeout=5) as response:
            assert response.status == 200
            dash = json.loads(response.read().decode("utf-8"))

        tracked_jobs = dash.get("tracked_jobs", [])
        jobs_to_apply = dash.get("jobs_to_apply", [])
        total_jobs = len(tracked_jobs)
        token = dash.get("action_token", "")

        print(f"Tracked jobs count: {total_jobs}")
        print(f"Jobs to apply count: {len(jobs_to_apply)}")

        # 2. Fetch settings
        req_settings = urllib.request.Request(
            f"{server_url}api/settings",
            headers={"X-Job-Agent-Token": token}
        )
        with urllib.request.urlopen(req_settings, timeout=5) as response:
            assert response.status == 200
            settings = json.loads(response.read().decode("utf-8"))

        results["job_count"] = total_jobs
        results["jobs_to_apply_count"] = len(jobs_to_apply)
        results["profile_summary"] = {
            "has_profile": bool(dash.get("profile_summary")),
            "headline": (dash.get("profile_summary") or {}).get("headline", ""),
            "readiness": (dash.get("profile_summary") or {}).get("readiness", ""),
        }
        results["settings_keys"] = list(settings.get("settings", {}).keys())
        results["dashboard_health"] = "OK"

        print("Verification completed successfully:")
        print(json.dumps(results, ensure_ascii=False, indent=2))

    finally:
        print("Stopping verification process...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if URL_FILE.exists():
            URL_FILE.unlink()

    return results


if __name__ == "__main__":
    verify_production_deployment()
