from __future__ import annotations

"""Explicit desktop acceptance checks, loaded only for test modes."""

import json
import os
import time
from functools import wraps
from pathlib import Path
from urllib.request import urlopen

from job_agent import __version__
from job_agent.desktop import (
    APP_TITLE,
    DEFAULT_WINDOW_SIZE,
    MINIMUM_WINDOW_SIZE,
    _require_isolated_test_root,
    start_desktop_server,
    wait_until_ready,
)
from job_agent.services.profile_store import write_text_atomic


def _restore_qa_environment(function):
    @wraps(function)
    def run(*args, **kwargs):
        keys = ("JOB_AGENT_PROJECT_ROOT", "JOB_AGENT_PROFILE", "JOB_AGENT_DB", "JOB_AGENT_CONFIG",
                "JOB_AGENT_AI_PROVIDER", "JOB_AGENT_SEARCH_PROVIDER", "JOB_AGENT_MAP_PROVIDER")
        original = {key: os.environ.get(key) for key in keys}
        try:
            return function(*args, **kwargs)
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return run


def desktop_smoke_test(data_root: Path, report_path: Path | None = None) -> Path:
    # Exercise lazy SDK imports in the frozen build without making a network call.
    from openai import OpenAI

    with OpenAI(api_key="local-smoke-only", base_url="http://127.0.0.1:1/v1") as client:
        sdk_ready = callable(client.responses.create) and callable(client.chat.completions.create)
    runtime = start_desktop_server(data_root)
    errors: list[str] = []
    try:
        health = wait_until_ready(runtime.url)
        with urlopen(runtime.url + "api/settings", timeout=2) as response:
            connector_settings = json.loads(response.read().decode("utf-8"))
        db_created = (data_root / "data/private/job_agent.sqlite3").is_file()
        settings_ready = (
            "ai" in connector_settings
            and "search" in connector_settings
            and "maps" in connector_settings
        )
        if not sdk_ready:
            errors.append("OpenAI SDK lazy imports not ready")
        if not db_created:
            errors.append("Database file not created")
        if not settings_ready:
            errors.append("Settings API did not return required connectors")

        status = "ok" if not errors else "failed"
        report = {
            "status": status,
            "openai_sdk_ready": sdk_ready,
            "app_version": __version__,
            "service": health.get("service"),
            "service_version": health.get("version"),
            "data_root": str(data_root),
            "database_created": db_created,
            "settings_api_ready": settings_ready,
            "errors": errors,
        }
    finally:
        runtime.stop()
    target = report_path or data_root / "logs" / "desktop-smoke-test.json"
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        target,
    )
    if status != "ok":
        raise RuntimeError(f"Desktop smoke test failed: {errors}")
    return target


@_restore_qa_environment
def desktop_acceptance_test(data_root: Path, report_path: Path | None = None) -> Path:
    _require_isolated_test_root(data_root, prefix="acceptance-")
    _isolate_qa_settings(data_root)
    _seed_window_fixture(data_root)
    from urllib.request import Request, urlopen
    import sqlite3
    from openai import OpenAI

    with OpenAI(api_key="local-smoke-only", base_url="http://127.0.0.1:1/v1") as client:
        sdk_ready = callable(client.responses.create) and callable(client.chat.completions.create)

    runtime = start_desktop_server(data_root)
    checks: dict[str, dict[str, object]] = {}
    health_data: dict[str, object] = {}
    all_jobs: list[dict[str, object]] = []
    applied_jobs: list[dict[str, object]] = []
    tracking_data: dict[str, object] = {}
    auto_backups: list[str] = []
    db_path = data_root / "data" / "private" / "job_agent.sqlite3"
    preview_pages = 0

    try:
        # 1. Health check
        try:
            health_data = wait_until_ready(runtime.url)
            checks["health"] = {
                "status": "ok",
                "service": health_data.get("service"),
                "version": health_data.get("version"),
            }
        except Exception as exc:
            checks["health"] = {"status": "failed", "error": str(exc)}

        # 2. Database check
        try:
            backups_dir = data_root / "data" / "private" / "backups"
            if backups_dir.exists():
                auto_backups = [p.name for p in backups_dir.glob("job_agent_pre_migration_*.sqlite3.bak")]
            if not db_path.is_file():
                checks["database"] = {"status": "failed", "error": f"Database file not found: {db_path}"}
            else:
                db_size = db_path.stat().st_size
                if db_size == 0:
                    checks["database"] = {"status": "failed", "error": "Database file is empty (0 bytes)"}
                else:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                    try:
                        quick = conn.execute("PRAGMA quick_check;").fetchone()
                        fk = conn.execute("PRAGMA foreign_key_check;").fetchall()
                    finally:
                        conn.close()
                    if quick and quick[0] == "ok" and len(fk) == 0:
                        checks["database"] = {
                            "status": "ok",
                            "size": db_size,
                            "integrity": "ok",
                            "foreign_keys": "ok",
                            "auto_backups": auto_backups,
                        }
                    else:
                        checks["database"] = {
                            "status": "failed",
                            "error": f"Database check failed: quick_check={quick}, foreign_key_violations={len(fk)}",
                        }
        except Exception as exc:
            checks["database"] = {"status": "failed", "error": str(exc)}

        # 3. Settings API
        try:
            with urlopen(runtime.url + "api/settings", timeout=3) as response:
                connector_settings = json.loads(response.read().decode("utf-8"))
            settings_ready = (
                "ai" in connector_settings
                and "search" in connector_settings
                and "maps" in connector_settings
            )
            if settings_ready:
                checks["settings_api"] = {"status": "ok"}
            else:
                checks["settings_api"] = {
                    "status": "failed",
                    "error": f"Missing required settings keys in: {list(connector_settings.keys())}",
                }
        except Exception as exc:
            checks["settings_api"] = {"status": "failed", "error": str(exc)}

        # 4. OpenAI SDK
        if sdk_ready:
            checks["openai_sdk"] = {"status": "ok"}
        else:
            checks["openai_sdk"] = {"status": "failed", "error": "OpenAI SDK create methods not callable"}

        # 5. Dashboard snapshot & action_token
        action_token = ""
        first_job_id: int | None = None
        try:
            with urlopen(runtime.url + "api/dashboard", timeout=5) as response:
                dashboard_data = json.loads(response.read().decode("utf-8"))
            action_token = dashboard_data.get("action_token", "")
            tracked_jobs = dashboard_data.get("tracked_jobs", [])
            jobs_to_apply = dashboard_data.get("jobs_to_apply", [])
            all_jobs = tracked_jobs or jobs_to_apply
            applied_jobs = [
                j for j in tracked_jobs
                if j.get("has_applied") or j.get("applied_at") or j.get("status") not in ("discovered", "saved")
            ]
            first_job_id = all_jobs[0]["job_id"] if all_jobs else None
            checks["dashboard"] = {
                "status": "ok",
                "jobs_count": len(all_jobs),
                "applications_count": len(applied_jobs),
                "has_action_token": bool(action_token),
            }
        except Exception as exc:
            checks["dashboard"] = {"status": "failed", "error": str(exc)}

        auth_headers = {"X-Job-Agent-Token": action_token} if action_token else {}

        # 6. Tracking board
        try:
            req = Request(runtime.url + "api/tracking/board", headers=auth_headers)
            with urlopen(req, timeout=5) as response:
                tracking_data = json.loads(response.read().decode("utf-8"))
            if isinstance(tracking_data.get("jobs"), list) and isinstance(tracking_data.get("reminders"), list):
                checks["tracking_board"] = {
                    "status": "ok",
                    "jobs_count": len(tracking_data["jobs"]),
                    "reminders_count": len(tracking_data["reminders"]),
                }
            else:
                checks["tracking_board"] = {
                    "status": "failed",
                    "error": f"Invalid tracking board payload structure: {list(tracking_data.keys())}",
                }
        except Exception as exc:
            checks["tracking_board"] = {"status": "failed", "error": str(exc)}

        # 7. Resume preview
        if first_job_id is None:
            checks["resume_preview"] = {
                "status": "skipped",
                "reason": "no_jobs_in_database",
            }
        else:
            first_job = all_jobs[0]
            preview_payload = {
                "job_id": first_job_id,
                "content": {
                    "template_id": "classic-centered",
                    "person": {"name": "求职者", "phone": "13800000000", "email": "candidate@example.com"},
                    "summary": "资深产品经理，具备丰富系统架构与端到端产品落地经验。",
                    "skills": ["Python", "SQLite", "系统架构"],
                    "experience_sections": [
                        {
                            "title": "工作经历",
                            "entries": [
                                {
                                    "organization": first_job.get("company") or "测试科技",
                                    "role": first_job.get("title") or "产品经理",
                                    "dates": "2023 - 至今",
                                    "bullets": [{"text": "负责核心业务系统架构演进，提升数据一致性与可用性。"}]
                                }
                            ]
                        }
                    ],
                    "education": [
                        {
                            "school": "测试大学",
                            "degree": "硕士",
                            "major": "软件工程",
                            "dates": "2020 - 2023"
                        }
                    ],
                    "target": {
                        "job_id": first_job_id,
                        "company": first_job.get("company") or "测试科技",
                        "role": first_job.get("title") or "产品经理"
                    }
                }
            }
            req = Request(
                runtime.url + "api/resume/preview",
                data=json.dumps(preview_payload).encode("utf-8"),
                headers={**auth_headers, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(req, timeout=10) as response:
                    pdf_bytes = response.read()
                    preview_pages = int(response.headers.get("X-Resume-Preview-Pages", "1"))
                    if response.status == 200 and pdf_bytes.startswith(b"%PDF-") and len(pdf_bytes) > 500:
                        checks["resume_preview"] = {
                            "status": "ok",
                            "pages": preview_pages,
                            "bytes": len(pdf_bytes),
                        }
                    else:
                        checks["resume_preview"] = {
                            "status": "failed",
                            "error": f"Invalid resume preview response: status={response.status}, bytes={len(pdf_bytes)}",
                        }
            except Exception as exc:
                checks["resume_preview"] = {"status": "failed", "error": str(exc)}

        # 8. Mock interview
        if first_job_id is None:
            checks["mock_interview"] = {
                "status": "skipped",
                "reason": "no_jobs_in_database",
            }
        else:
            tp_payload = {"job_id": first_job_id}
            req = Request(
                runtime.url + "api/interview/teleprompter",
                data=json.dumps(tp_payload).encode("utf-8"),
                headers={**auth_headers, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(req, timeout=5) as response:
                    tp_res = json.loads(response.read().decode("utf-8"))
                    if response.status == 200 and isinstance(tp_res, dict) and ("facts" in tp_res or "intro" in tp_res or "cards" in tp_res):
                        checks["mock_interview"] = {
                            "status": "ok",
                            "facts_count": len(tp_res.get("facts", [])),
                        }
                    else:
                        checks["mock_interview"] = {
                            "status": "failed",
                            "error": f"Unexpected teleprompter response: status={response.status}, body={tp_res}",
                        }
            except Exception as exc:
                # 明确失败，绝对不能吞没为 True！
                checks["mock_interview"] = {"status": "failed", "error": str(exc)}

    finally:
        runtime.stop()

    failed_checks = {k: v.get("error", "unknown error") for k, v in checks.items() if v.get("status") == "failed"}
    skipped_checks = {k: v.get("reason", "unknown reason") for k, v in checks.items() if v.get("status") == "skipped"}
    passed_checks = [k for k, v in checks.items() if v.get("status") == "ok"]

    overall_status = "failed" if (failed_checks or skipped_checks) else "ok"

    report = {
        "status": overall_status,
        "failed_checks": failed_checks,
        "skipped_checks": skipped_checks,
        "passed_checks": passed_checks,
        "checks": checks,
        "app_version": __version__,
        "service": health_data.get("service"),
        "service_version": health_data.get("version"),
        "data_root": str(data_root),
        "database_exists": checks.get("database", {}).get("status") == "ok",
        "database_size": db_path.stat().st_size if db_path.is_file() else 0,
        "auto_backups": auto_backups,
        "jobs_count": len(all_jobs),
        "sample_job": f"#{all_jobs[0]['job_id']} {all_jobs[0]['company']} - {all_jobs[0]['title']}" if all_jobs else None,
        "applications_count": len(applied_jobs),
        "sample_application": f"job_id={applied_jobs[0]['job_id']} {applied_jobs[0]['company']} - {applied_jobs[0]['title']} (stage: {applied_jobs[0]['status']})" if applied_jobs else None,
        "tracking_jobs_count": len(tracking_data.get("jobs", [])),
        "tracking_reminders_count": len(tracking_data.get("reminders", [])),
        "resume_preview_status": checks.get("resume_preview", {}).get("status"),
        "resume_preview_ready": checks.get("resume_preview", {}).get("status") == "ok",
        "resume_preview_pages": preview_pages,
        "mock_interview_status": checks.get("mock_interview", {}).get("status"),
        "mock_interview_ready": checks.get("mock_interview", {}).get("status") == "ok",
        "settings_api_ready": checks.get("settings_api", {}).get("status") == "ok",
        "openai_sdk_ready": checks.get("openai_sdk", {}).get("status") == "ok",
    }

    target = report_path or data_root / "logs" / "desktop-acceptance-report.json"
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        target,
    )
    if overall_status != "ok":
        raise RuntimeError(f"Desktop acceptance test failed on checks: {list(failed_checks.keys())} -> {failed_checks}")
    return target


def _isolate_qa_settings(data_root: Path) -> None:
    """A caller's development overrides must not redirect a synthetic test."""
    import os
    os.environ.update({
        "JOB_AGENT_PROJECT_ROOT": str(data_root),
        "JOB_AGENT_PROFILE": str(data_root / "data/private/profile.json"),
        "JOB_AGENT_DB": str(data_root / "data/private/job_agent.sqlite3"),
        "JOB_AGENT_CONFIG": str(data_root / "data/private/app-settings.json"),
        "JOB_AGENT_AI_PROVIDER": "local",
        "JOB_AGENT_SEARCH_PROVIDER": "none",
        "JOB_AGENT_MAP_PROVIDER": "none",
    })


def _seed_window_fixture(data_root: Path) -> int:
    """Synthetic facts only; called after the fresh temporary-root guard."""
    from job_agent.models.job import StructuredJob
    from job_agent.models.job_record import JobRecordInput
    from job_agent.services.job_repository import JobRepository
    from job_agent.services.local_matcher import match_job_locally
    from job_agent.services.runtime_config import RuntimeConfig, save_runtime_config

    profile_path = data_root / "data/private/profile.json"
    from job_agent.models.profile import (
        ClaimStatus, ContactInfo, Education, EvidenceFact, Experience,
        ExperienceKind, JobSearchPreferences, Person, Profile, Skill,
    )
    synthetic_profile = Profile(
        schema_version="1.0",
        person=Person(
            display_name="桌面验收样例",
            current_city="北京",
            contact=ContactInfo(email="liming.candidate@example.com", phone="13800138000"),
        ),
        job_search=JobSearchPreferences(target_roles=["产品经理", "产品运营"]),
        education=[
            Education(
                id="edu-1",
                institution="合成测试大学",
                degree="硕士",
                major="软件工程",
                start="2020",
                end="2023",
                status=ClaimStatus.USER_CONFIRMED,
            )
        ],
        experiences=[
            Experience(
                id="exp-1",
                kind=ExperienceKind.EMPLOYMENT,
                organization="合成测试公司",
                role="高级产品经理",
                start="2023",
                end="至今",
                summary="主导业务核心流转引擎重构，端到端交付率提升 40%。",
                facts=[
                    EvidenceFact(
                        id="fact-1",
                        statement="主导业务核心流转引擎重构，端到端交付率提升 40%。",
                        skills=["Python", "SQLite"],
                        status=ClaimStatus.USER_CONFIRMED,
                    )
                ],
            )
        ],
        skills=[
            Skill(id="sk-1", name="产品设计", status=ClaimStatus.USER_CONFIRMED),
            Skill(id="sk-2", name="Python", status=ClaimStatus.USER_CONFIRMED),
            Skill(id="sk-3", name="SQLite", status=ClaimStatus.USER_CONFIRMED),
        ],
    )
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(synthetic_profile.model_dump_json(indent=2), encoding="utf-8")

    save_runtime_config(RuntimeConfig(), data_root / "data/private/app-settings.json")
    repo = JobRepository(data_root / "data/private/job_agent.sqlite3")
    job = repo.upsert_job(JobRecordInput(
        company="合成验收招聘公司", title="产品经理", location="北京",
        jd_text="负责产品设计，使用 Python 和 SQLite 分析业务。", source="synthetic-window-qa",
        source_url="https://example.invalid/synthetic-window-qa",
    ))
    result = match_job_locally(synthetic_profile, StructuredJob(
        company="合成验收招聘公司", title="产品经理", location="北京", required_skills=["Python", "SQLite"],
        raw_text="负责产品设计，使用 Python 和 SQLite 分析业务。",
    ))
    repo.add_match_result(job.job_id, result)
    return job.job_id


def _window_result(checks: dict, required: set[str]) -> dict:
    """A missing/skipped required check must never certify a release."""
    failures = {name: checks.get(name, {}).get("error", "required check missing or not passed")
                for name in required if checks.get(name, {}).get("status") != "ok"}
    failures.update({name: value.get("error", "failed") for name, value in checks.items()
                     if value.get("status") == "failed"})
    return {"status": "failed" if failures else "ok", "checks": checks,
            "failed_checks": failures,
            "passed_checks": [name for name, value in checks.items() if value.get("status") == "ok"],
            "skipped_checks": {name: value.get("reason") for name, value in checks.items()
                               if value.get("status") == "skipped"}}


def _require_window_restart_root(data_root: Path) -> dict:
    import hashlib
    import tempfile
    root = data_root.resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if root == temp or not root.is_relative_to(temp) or not root.name.startswith("window-"):
        raise RuntimeError("窗口重启验收只允许已有合成 window-* 临时目录。")
    try:
        marker = json.loads((root / "window-fixture.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256((root / "data/private/profile.json").read_bytes()).hexdigest()
        if marker["profile_sha256"] != digest or marker["schema"] != "synthetic-window-qa-v1":
            raise ValueError("fixture changed")
        return marker
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("缺少有效的合成窗口验收记录，拒绝重启验收。") from exc


@_restore_qa_environment
def desktop_window_test(data_root: Path, report_path: Path | None = None, *, restart: bool = False) -> Path:
    import hashlib
    import io
    import pypdf
    import webview

    if restart:
        marker = _require_window_restart_root(data_root)
        job_id = int(marker["job_id"])
    else:
        _require_isolated_test_root(data_root, prefix="window-")
        job_id = _seed_window_fixture(data_root)
        marker = {"schema": "synthetic-window-qa-v1", "job_id": job_id,
                  "profile_sha256": hashlib.sha256((data_root / "data/private/profile.json").read_bytes()).hexdigest()}
    _isolate_qa_settings(data_root)
    runtime = start_desktop_server(data_root)
    checks: dict[str, dict[str, object]] = {}
    required = {"dom_ready", "navigation_overview", "settings_dialog", "navigation_workspace",
                "job_detail_selection", "review_ui", "pdf_inspection", "review_is_read_only"}
    if restart:
        required.add("persisted_pdf_version")
    else:
        required.add("prepare_via_ui")

    def verify(window):
        current = "dom_ready"
        def wait_js(expression, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    value = window.evaluate_js(expression)
                    if value:
                        return value
                except Exception:
                    pass
                time.sleep(0.15)
            raise RuntimeError(f"WebView2 condition timed out: {expression}")
        def visible(selector):
            return "(() => { const e = document.querySelector(" + json.dumps(selector) + "); return Boolean(e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'); })()"
        def click(selector):
            wait_js(visible(selector))
            window.evaluate_js("document.querySelector(" + json.dumps(selector) + ").click()")
        try:
            wait_js("document.readyState === 'complete' && Boolean(document.querySelector('#todayContent'))")
            checks[current] = {"status": "ok", "engine": "native WebView2"}
            current = "navigation_overview"
            click("#overviewNav")
            wait_js(visible("#overviewTitle"))
            checks[current] = {"status": "ok"}
            current = "settings_dialog"
            click("#profileNav")
            click(".profile-connections button")
            wait_js("document.querySelector('#settingsDialog').open")
            click("#closeSettingsButton")
            wait_js("!document.querySelector('#settingsDialog').open")
            checks[current] = {"status": "ok"}
            current = "navigation_workspace"
            click("#workspaceNav")
            wait_js(visible("#jobWorkspace"))
            click('[data-job-track="all"]')
            checks[current] = {"status": "ok"}
            current = "job_detail_selection"
            click(f'[data-select-job="{job_id}"]')
            wait_js("document.querySelector('#jobDetail').textContent.includes('合成验收招聘公司')")
            checks[current] = {"status": "ok", "job_id": job_id}
            if not restart:
                current = "prepare_via_ui"
                click('button[data-detail-action="prepare"]')
                wait_js("document.querySelector('#reviewDialog').open", timeout=35)
                checks[current] = {"status": "ok", "trigger": "clicked visible prepare button"}
            else:
                # Reopen the persisted draft through the visible review action.
                click('button[data-detail-action="review-and-open"]')
            current = "review_ui"
            wait_js("document.querySelector('#reviewDialog').open")
            pdf_url = wait_js("(() => {const s = document.querySelector('#reviewPdfFrame').getAttribute('src'); return s && s.includes('/resume-draft/') && s.includes('?v=') ? s : false;})()")
            wait_js("!document.querySelector('#reviewOpenPageBtn').disabled")
            checks[current] = {"status": "ok", "pdf_url": pdf_url, "approval_button_enabled": True}
            current = "pdf_inspection"
            with urlopen(runtime.url.rstrip('/') + pdf_url, timeout=10) as response:
                pdf_bytes = response.read()
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            text = ''.join(page.extract_text() or '' for page in reader.pages)
            if not reader.pages or '桌面验收样例' not in text or '合成测试公司' not in text:
                raise RuntimeError("PDF synthetic identity or confirmed experience missing")
            digest = hashlib.sha256(pdf_bytes).hexdigest()
            if digest not in pdf_url.lower():
                raise RuntimeError("UI preview URL hash does not match downloaded PDF")
            checks[current] = {"status": "ok", "pages": len(reader.pages), "sha256": digest,
                               "bytes": len(pdf_bytes), "method": "pypdf text and version inspection; not visual PDF rendering"}
            if restart:
                current = "persisted_pdf_version"
                if digest != marker["pdf_sha256"]:
                    raise RuntimeError("PDF changed across process restart")
                checks[current] = {"status": "ok", "sha256": digest}
            current = "review_is_read_only"
            from job_agent.services.job_repository import JobRepository
            app = JobRepository(data_root / "data/private/job_agent.sqlite3").get_application(job_id)
            if not app or app.status != "ready_to_apply":
                raise RuntimeError(f"Viewing draft unexpectedly altered application status: {app}")
            if list((data_root / "data/output").rglob("resume-version-approved-*.json")):
                raise RuntimeError("Viewing draft unexpectedly approved it")
            checks[current] = {"status": "ok", "application_status": app.status}
            click("#closeReviewButton")
            if not restart:
                marker["pdf_sha256"] = digest
                write_text_atomic(json.dumps(marker), data_root / "window-fixture.json")
        except Exception as exc:
            checks[current] = {"status": "failed", "error": str(exc)}
            try:
                checks[current]["visible_text"] = window.evaluate_js("document.body.innerText.slice(0, 4000)")
            except Exception:
                pass
        finally:
            window.destroy()

    try:
        window = webview.create_window(APP_TITLE + " — 合成验收", runtime.url,
                                       width=DEFAULT_WINDOW_SIZE[0], height=DEFAULT_WINDOW_SIZE[1],
                                       min_size=MINIMUM_WINDOW_SIZE, background_color="#e9e7e1")
        webview.start(verify, window, gui="edgechromium")
    except Exception as exc:
        checks["native_window"] = {"status": "failed", "error": str(exc)}
    finally:
        runtime.stop()
    report = _window_result(checks, required)
    report.update(app_version=__version__, data_root=str(data_root),
                  phase="restart" if restart else "first_run", coverage="native WebView2 DOM interactions",
                  unverified=["PDF visual layout by human eyes", "OS external browser/PDF viewer launch", "live recruiting sites"])
    target = (report_path or data_root / "logs/desktop-window-report.json").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(json.dumps(report, ensure_ascii=False, indent=2) + "\n", target)
    if report["status"] != "ok":
        raise RuntimeError(f"Desktop window test failed: {report['failed_checks']}")
    return target
