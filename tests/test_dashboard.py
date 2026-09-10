import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from unittest import mock

from job_agent.models.job import MatchResult, Recommendation, ScoreBreakdown
from job_agent.models.job_record import JobRecordInput, SearchCandidateInput
from job_agent.models.profile import CommutePreferences
from job_agent.services.dashboard import (
    build_dashboard_snapshot,
    create_dashboard_server,
)
from job_agent.services.job_repository import JobRepository
from job_agent.services.local_matcher import structure_job_locally
from job_agent.services.profile_store import load_profile, save_profile
from job_agent.services.resume_polish import CloudAIUnavailableError
from job_agent.services.runtime_config import AIConnectorConfig, RuntimeConfig, save_runtime_config
from tests.helpers import sample_profile


JD = """
岗位职责：负责业务数据分析、策略运营与跨部门项目协作。
任职要求：本科及以上，每周至少 4 天，连续实习 3 个月。
""".strip()

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def write_profile_photo(private_dir: Path) -> Path:
    path = private_dir / "photo" / "profile-photo.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_1PX)
    return path


def multipart_payload(
    fields: dict[str, str],
    *,
    filename: str,
    file_payload: bytes,
) -> tuple[str, bytes]:
    boundary = "----JobAgentDashboardTestBoundary"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="resume"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            b"Content-Type: text/plain\r\n\r\n",
            file_payload,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def scored_result(*, company: str, title: str, score: int) -> MatchResult:
    if score == 90:
        breakdown = ScoreBreakdown(
            role_direction=20,
            skills=25,
            experience=22,
            education=9,
            logistics=10,
            preferences=4,
        )
    elif score == 80:
        breakdown = ScoreBreakdown(
            role_direction=18,
            skills=22,
            experience=18,
            education=9,
            logistics=9,
            preferences=4,
        )
    else:
        raise ValueError("test score is not configured")
    structured = structure_job_locally(
        JD,
        company=company,
        title=title,
        location="上海",
        source="test",
        source_url="https://example.com/job",
    )
    return MatchResult(
        job=structured,
        score_breakdown=breakdown,
        overall_score=score,
        recommendation=Recommendation.RECOMMEND,
        engine="dashboard-test",
    )


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repository = JobRepository(self.root / "jobs.sqlite3")
        self.output_dir = self.root / "output"

        applied = self.repository.upsert_job(
            JobRecordInput(
                company="示例科技",
                title="策略运营实习生",
                jd_text=JD,
                source="实习僧",
                source_url="https://example.com/jobs/applied",
                location="上海",
            )
        )
        pending = self.repository.upsert_job(
            JobRecordInput(
                company="示例数据",
                title="数据运营实习生",
                jd_text=JD,
                source="牛客",
                source_url="https://example.com/jobs/pending",
                location="上海",
            )
        )
        self.applied_job_id = applied.job_id
        self.pending_job_id = pending.job_id
        self.repository.add_match_result(
            applied.job_id,
            scored_result(company="示例科技", title="策略运营实习生", score=90),
        )
        self.repository.add_match_result(
            pending.job_id,
            scored_result(company="示例数据", title="数据运营实习生", score=80),
        )
        self.repository.record_application_status(
            applied.job_id,
            "applied",
            source="browser_verification",
            detail="页面显示已投递",
        )
        candidate = self.repository.upsert_search_candidate(
            SearchCandidateInput(
                provider="bocha",
                query="上海 运营 实习",
                title="运营实习岗位",
                url="https://example.com/candidate/1",
                snippet="仍在招聘",
            )
        )
        self.repository.mark_candidate_verification(candidate.candidate_id, "live")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_snapshot_reconciles_metrics_and_funnel(self) -> None:
        snapshot = build_dashboard_snapshot(
            self.repository,
            output_dir=self.output_dir,
            today=date(2026, 9, 9),
        )
        metrics = {item.metric_id: item.value for item in snapshot.metrics}
        funnel = {item.stage_id: item.value for item in snapshot.funnel}

        self.assertEqual(metrics["candidates"], 1)
        self.assertEqual(metrics["jobs"], 2)
        self.assertEqual(metrics["recommended"], 2)
        self.assertEqual(metrics["strongly_recommended"], 1)
        self.assertEqual(metrics["pending_apply"], 1)
        self.assertEqual(metrics["commute_filtered"], 0)
        self.assertEqual(metrics["applied"], 1)
        self.assertEqual(metrics["meaningful_response_rate"], 0.0)
        self.assertEqual(metrics["priority_preparation"], 1)
        self.assertEqual(
            funnel,
            {
                "discovered": 1,
                "jobs": 2,
                "recommended": 2,
                "applied": 1,
                "hr_read": 0,
                "meaningful_response": 0,
                "screening": 0,
                "interview": 0,
                "offer": 0,
            },
        )
        self.assertEqual([item.job_id for item in snapshot.jobs_to_apply], [2])
        job_rows = {item.job_id: item for item in snapshot.tracked_jobs}
        self.assertTrue(job_rows[1].has_applied)
        self.assertIsNotNone(job_rows[1].applied_at)
        self.assertFalse(job_rows[2].has_applied)
        self.assertIsNone(job_rows[2].applied_at)
        self.assertEqual(snapshot.priority_preparation[0].job_id, 1)
        self.assertEqual(snapshot.priority_preparation[0].priority, "elevated")
        self.assertEqual(snapshot.recent_feedback[0].status, "applied")
        self.assertEqual(snapshot.strategy.daily_internship_share, 50)
        self.assertEqual(snapshot.strategy.autumn_recruitment_share, 50)
        self.assertIsNotNone(snapshot.source_freshness_at.tzinfo)

    def test_snapshot_excludes_only_confirmed_over_limit_commutes(self) -> None:
        profile_path = self.root / "private" / "profile.json"
        profile = sample_profile()
        profile.job_search.commute = CommutePreferences(
            origin="上海徐汇区宜山路站",
            max_one_way_minutes=60,
            transport_modes=["地铁"],
        )
        save_profile(profile, profile_path)
        self.repository.update_job_commute(
            self.pending_job_id,
            75,
            method="user_estimate",
            note="早高峰单程约 75 分钟",
        )

        snapshot = build_dashboard_snapshot(
            self.repository,
            output_dir=self.output_dir,
            profile_path=profile_path,
        )
        metrics = {item.metric_id: item.value for item in snapshot.metrics}
        pending_row = next(
            item for item in snapshot.tracked_jobs if item.job_id == self.pending_job_id
        )

        self.assertEqual(snapshot.jobs_to_apply, [])
        self.assertEqual(metrics["commute_filtered"], 1)
        self.assertEqual(pending_row.commute_fit, "over_limit")
        self.assertEqual(pending_row.commute_minutes, 75)
        self.assertEqual(snapshot.active_profile.max_one_way_minutes, 60)  # type: ignore[union-attr]

    def test_snapshot_returns_every_pending_job_without_hidden_cap(self) -> None:
        for index in range(12):
            record = self.repository.upsert_job(
                JobRecordInput(
                    company=f"扩展公司 {index}",
                    title=f"运营实习生 {index}",
                    jd_text=JD,
                    source="企业官网",
                    source_url=f"https://example.com/jobs/expanded-{index}",
                    location="上海",
                )
            )
            self.repository.add_match_result(
                record.job_id,
                scored_result(
                    company=f"扩展公司 {index}",
                    title=f"运营实习生 {index}",
                    score=80,
                ),
            )

        snapshot = build_dashboard_snapshot(
            self.repository,
            output_dir=self.output_dir,
        )
        metrics = {item.metric_id: item.value for item in snapshot.metrics}

        self.assertEqual(metrics["pending_apply"], 13)
        self.assertEqual(len(snapshot.jobs_to_apply), 13)

    def test_http_dashboard_is_local_and_protects_writes(self) -> None:
        server = create_dashboard_server(
            self.repository,
            output_dir=self.output_dir,
            port=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(root + "/", timeout=5) as response:
                html = response.read().decode("utf-8")
                self.assertIn("个人求职 Agent", html)
                self.assertIn('id="jobWorkspace"', html)
                self.assertNotIn('class="hero"', html)
                self.assertIn('id="jobSearch"', html)
                self.assertIn("连接与 API", html)
                self.assertIn("补录投递进度", html)
                self.assertIn("个人资料与简历", html)
                self.assertIn('id="queueButton"', html)
                self.assertIn("Agent 对话", html)
                self.assertIn('id="openCopilotButton"', html)
                self.assertIn("生成草稿", html)
                self.assertIn("人工投递准备", html)
                self.assertIn("原始职位", html)
                self.assertIn("投递记录", html)
                self.assertIn("实习机会", html)
                self.assertIn("校招机会", html)
                self.assertIn("项目工坊", html)
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["schema_version"], "phase13-dual-track-workshop-v1")
                self.assertEqual(payload["source_name"], "本地 SQLite")
                self.assertTrue(payload["action_token"])
                self.assertEqual(len(payload["tracked_jobs"]), 2)

            with urllib.request.urlopen(root + f"/api/jobs/{self.applied_job_id}/detail", timeout=5) as response:
                detail = json.loads(response.read().decode("utf-8"))
                self.assertEqual(detail["job"]["jd_text"], JD)
                self.assertEqual(detail["events"][-1]["status"], "applied")
                self.assertEqual(detail["project_workshop"]["status"], "idea")
                self.assertIn(detail["strategy"]["strategy_fit"], {"recommended", "manual_review", "blocked"})
                self.assertNotIn("evidence_path", detail["events"][-1])
                self.assertNotIn("commute_origin", detail["job"])
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(root + "/api/jobs/999999/detail", timeout=5)
            self.assertEqual(missing.exception.code, 404)

            with urllib.request.urlopen(root + "/api/health", timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))
                self.assertEqual(health["service"], "personal-job-agent")

            for font_name in (
                "SmileySans-Oblique.woff2",
                "NotoSerifSC-Variable.subset.woff2",
                "NotoSansSC-Variable.subset.woff2",
            ):
                with urllib.request.urlopen(
                    root + f"/assets/fonts/{font_name}", timeout=5
                ) as response:
                    self.assertEqual(response.headers.get_content_type(), "font/woff2")
                    self.assertGreater(len(response.read()), 1_000_000)

            with urllib.request.urlopen(root + "/api/settings", timeout=5) as response:
                settings = json.loads(response.read().decode("utf-8"))
                self.assertIn(settings["ai"]["provider"], {"local", "openai"})
                self.assertNotIn("api_key", settings["ai"])
                self.assertNotIn("api_key", settings["search"])
                self.assertNotIn("api_key", settings["maps"])
                self.assertIn("usage", settings)

            request = urllib.request.Request(root + "/api/dashboard", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 405)

            unauthorized_body = json.dumps(
                {"status": "applied", "confirmed": True}
            ).encode("utf-8")
            unauthorized = urllib.request.Request(
                root + f"/api/applications/{self.pending_job_id}/status",
                data=unauthorized_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(unauthorized, timeout=5)
            self.assertEqual(raised.exception.code, 403)

            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                token = json.loads(response.read().decode("utf-8"))["action_token"]

            project_request = urllib.request.Request(
                root + f"/api/jobs/{self.applied_job_id}/project-workshop/run",
                data=json.dumps({"confirmed": True}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": root,
                    "X-Job-Agent-Token": token,
                },
                method="POST",
            )
            with urllib.request.urlopen(project_request, timeout=5) as response:
                project_result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(project_result["ok"])
            self.assertEqual(project_result["project_workshop"]["status"], "ran")
            self.assertFalse(project_result["project_workshop"]["resume_eligible"])
            with urllib.request.urlopen(
                root + f"/project-workshop/{self.applied_job_id}/preview",
                timeout=5,
            ) as response:
                self.assertIn("合成数据", response.read().decode("utf-8"))

            settings_request = urllib.request.Request(
                root + "/api/settings",
                data=json.dumps(
                    {
                        "ai_provider": "openai_compatible",
                        "ai_model": "test-model",
                        "ai_base_url": "https://api.example.com/v1",
                        "ai_api_key": "test-secret",
                        "clear_ai_api_key": False,
                        "search_provider": "bocha",
                        "search_api_key": "search-secret",
                        "clear_search_api_key": False,
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": root,
                    "X-Job-Agent-Token": token,
                },
                method="POST",
            )
            with urllib.request.urlopen(settings_request, timeout=5) as response:
                saved_settings = json.loads(response.read().decode("utf-8"))
            self.assertTrue(saved_settings["ok"])
            rendered = json.dumps(saved_settings, ensure_ascii=False)
            self.assertNotIn("test-secret", rendered)
            self.assertNotIn("search-secret", rendered)
            self.assertTrue((self.root / "app-settings.json").is_file())

            authorized = urllib.request.Request(
                root + f"/api/applications/{self.pending_job_id}/status",
                data=json.dumps(
                    {
                        "status": "applied",
                        "confirmed": True,
                        "detail": "用户手动确认已投递",
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": root,
                    "X-Job-Agent-Token": token,
                },
                method="POST",
            )
            with urllib.request.urlopen(authorized, timeout=5) as response:
                recorded = json.loads(response.read().decode("utf-8"))
            self.assertTrue(recorded["ok"])
            self.assertTrue(recorded["changed"])
            self.assertEqual(
                self.repository.get_application(self.pending_job_id).status,
                "applied",
            )
            events = self.repository.list_application_events(self.pending_job_id)
            self.assertEqual(events[-1].source, "manual_dashboard")

            commute_request = urllib.request.Request(
                root + f"/api/jobs/{self.pending_job_id}/commute",
                data=json.dumps(
                    {
                        "minutes": 42,
                        "method": "route_estimate",
                        "note": "工作日导航估算",
                        "confirmed": True,
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": root,
                    "X-Job-Agent-Token": token,
                },
                method="POST",
            )
            with urllib.request.urlopen(commute_request, timeout=5) as response:
                commute_result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(commute_result["ok"])
            self.assertEqual(
                self.repository.get_job(self.pending_job_id).commute_minutes,
                42,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_profile_onboarding_accepts_local_resume_upload(self) -> None:
        profile_path = self.root / "private" / "profile.json"
        server = create_dashboard_server(
            self.repository,
            output_dir=self.output_dir,
            port=0,
            profile_path=profile_path,
            private_dir=self.root / "private",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                token = json.loads(response.read().decode("utf-8"))["action_token"]
            content_type, body = multipart_payload(
                {
                    "mode": "update",
                    "display_name": "测试用户",
                    "target_roles": "数据运营、产品运营",
                    "preferred_locations": "上海",
                    "employment_types": "实习",
                    "days_per_week": "5",
                    "duration_months": "3",
                    "commute_origin": "上海徐汇区宜山路站",
                    "max_commute_minutes": "60",
                    "transport_modes": "地铁、步行",
                    "remote_acceptable": "true",
                    "confirm_truth": "true",
                },
                filename="resume.txt",
                file_payload="使用 Excel 和 SQL 完成数据分析。".encode("utf-8"),
            )
            request = urllib.request.Request(
                root + "/api/profile/onboard",
                data=body,
                headers={
                    "Content-Type": content_type,
                    "Origin": root,
                    "X-Job-Agent-Token": token,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(result["ok"])
            self.assertGreaterEqual(result["imported_facts"], 1)
            profile = load_profile(profile_path)
            self.assertEqual(profile.person.display_name, "测试用户")
            self.assertEqual(profile.job_search.target_roles, ["数据运营", "产品运营"])
            self.assertEqual(profile.job_search.commute.max_one_way_minutes, 60)
            self.assertEqual(profile.job_search.commute.transport_modes, ["地铁", "步行"])
            self.assertEqual(len(profile.source_documents), 1)

            preference_type, preference_body = multipart_payload(
                {
                    "mode": "update",
                    "display_name": "测试用户",
                    "target_roles": "数据运营、产品运营",
                    "commute_origin": "上海静安寺站",
                    "max_commute_minutes": "45",
                    "transport_modes": "地铁",
                },
                filename="",
                file_payload=b"",
            )
            preference_request = urllib.request.Request(
                root + "/api/profile/onboard",
                data=preference_body,
                headers={
                    "Content-Type": preference_type,
                    "Origin": root,
                    "X-Job-Agent-Token": token,
                },
                method="POST",
            )
            with urllib.request.urlopen(preference_request, timeout=5) as response:
                preference_result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(preference_result["ok"])
            self.assertIsNone(preference_result["resume_filename"])
            profile = load_profile(profile_path)
            self.assertEqual(profile.job_search.commute.max_one_way_minutes, 45)
            self.assertEqual(len(profile.source_documents), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_job_workspace_copilot_resume_and_safe_fill_plan(self) -> None:
        private_dir = self.root / "private"
        profile_path = private_dir / "profile.json"
        save_profile(sample_profile(), profile_path)
        save_runtime_config(RuntimeConfig(), private_dir / "app-settings.json")
        write_profile_photo(private_dir)
        server = create_dashboard_server(
            self.repository,
            output_dir=self.output_dir,
            port=0,
            profile_path=profile_path,
            private_dir=private_dir,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=10) as response:
                dashboard = json.loads(response.read().decode("utf-8"))
            token = dashboard["action_token"]
            pending = next(
                item
                for item in dashboard["jobs_to_apply"]
                if item["job_id"] == self.pending_job_id
            )
            self.assertEqual(pending["workspace"]["resume_status"], "missing")
            self.assertFalse(pending["workspace"]["safe_fill_ready"])

            with urllib.request.urlopen(root + "/api/copilot", timeout=10) as response:
                copilot = json.loads(response.read().decode("utf-8"))
            rendered = json.dumps(copilot, ensure_ascii=False)
            self.assertIn("最终提交", copilot["safety_note"])
            self.assertNotIn("private@example.com", rendered)
            self.assertNotIn(str(private_dir), rendered)

            unauthorized = urllib.request.Request(
                root + "/api/copilot/message",
                data=json.dumps({"message": "今天投什么"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(unauthorized, timeout=10)
            self.assertEqual(raised.exception.code, 403)

            def post(path: str, payload: dict[str, object]) -> dict[str, object]:
                request = urllib.request.Request(
                    root + path,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": root,
                        "X-Job-Agent-Token": token,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    return json.loads(response.read().decode("utf-8"))

            chat = post(
                "/api/copilot/message",
                {"message": "给最高匹配的待投岗位准备简历草稿"},
            )
            messages = chat["copilot"]["thread"]["messages"]  # type: ignore[index]
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[-1]["actions"][0]["kind"], "create_resume_draft")

            draft = post(
                f"/api/jobs/{self.pending_job_id}/resume-draft",
                {"confirmed": True},
            )
            self.assertEqual(draft["workspace"]["resume_status"], "needs_review")  # type: ignore[index]
            with urllib.request.urlopen(
                root + f"/resume-draft/{self.pending_job_id}/pdf",
                timeout=20,
            ) as response:
                self.assertEqual(response.headers.get_content_type(), "application/pdf")
                self.assertTrue(response.read().startswith(b"%PDF"))

            approved = post(
                f"/api/jobs/{self.pending_job_id}/resume-draft/approve",
                {"confirmed_pdf_review": True},
            )
            workspace = approved["workspace"]
            self.assertEqual(workspace["resume_status"], "ready")  # type: ignore[index]
            self.assertTrue(workspace["application_pack_ready"])  # type: ignore[index]
            self.assertTrue(workspace["safe_fill_ready"])  # type: ignore[index]

            assist = post(
                f"/api/jobs/{self.pending_job_id}/assist",
                {"confirmed": True, "launch": False},
            )
            self.assertFalse(assist["launched"])
            self.assertEqual(assist["plan"]["final_submit"], "blocked")  # type: ignore[index]
            self.assertIn("最终提交申请", assist["plan"]["blocked_fields"])  # type: ignore[index]
            self.assertEqual(assist["plan"]["mode"], "manual_apply")  # type: ignore[index]
            self.assertFalse(assist["plan"]["automatic_form_fill"])  # type: ignore[index]
            staged_resume = Path(str(assist["plan"]["resume_path"]))  # type: ignore[index]
            self.assertTrue(staged_resume.is_file())
            self.assertEqual(staged_resume.parent.name, "ready-to-upload")
            self.assertIn("岗位专用简历", staged_resume.name)
            session_path = (
                private_dir
                / "browser-sessions"
                / str(assist["plan"]["session_id"])  # type: ignore[index]
                / "session.json"
            )
            session_payload = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(session_payload["mode"], "open_only")

            reset = post("/api/copilot/reset", {})
            self.assertEqual(reset["copilot"]["thread"]["messages"], [])  # type: ignore[index]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


    def test_http_resume_edit_loop_and_photo_upload(self) -> None:
        private_dir = self.root / "private"
        profile_path = private_dir / "profile.json"
        save_profile(sample_profile(), profile_path)
        save_runtime_config(RuntimeConfig(), private_dir / "app-settings.json")
        server = create_dashboard_server(
            self.repository,
            output_dir=self.output_dir,
            port=0,
            profile_path=profile_path,
            private_dir=private_dir,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        png_1px = PNG_1PX
        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=10) as response:
                token = json.loads(response.read().decode("utf-8"))["action_token"]

            def post_raw(path: str, data: bytes, content_type: str) -> object:
                request = urllib.request.Request(
                    root + path,
                    data=data,
                    headers={
                        "Content-Type": content_type,
                        "Origin": root,
                        "X-Job-Agent-Token": token,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))

            def post_json(path: str, payload: dict[str, object]) -> object:
                return post_raw(
                    path,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json",
                )

            # 无草稿时 GET 返回 404
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(
                    root + f"/api/jobs/{self.pending_job_id}/resume-content", timeout=10
                )
            self.assertEqual(raised.exception.code, 404)

            # 上传照片（未带 token 应 403）
            unauthorized_photo = urllib.request.Request(
                root + "/api/profile/photo",
                data=png_1px,
                headers={"Content-Type": "image/png"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(unauthorized_photo, timeout=10)
            self.assertEqual(raised.exception.code, 403)

            uploaded = post_raw("/api/profile/photo", png_1px, "image/png")
            self.assertTrue(uploaded["ok"])  # type: ignore[index]
            self.assertTrue(
                (private_dir / "photo" / "profile-photo.png").is_file()
            )

            # 非 PNG/JPG 内容应 400
            with self.assertRaises(urllib.error.HTTPError) as raised:
                post_raw("/api/profile/photo", b"not-an-image", "image/png")
            self.assertEqual(raised.exception.code, 400)

            # 生成草稿应嵌入照片
            draft = post_json(
                f"/api/jobs/{self.pending_job_id}/resume-draft",
                {"confirmed": True},
            )
            self.assertEqual(draft["workspace"]["resume_status"], "needs_review")  # type: ignore[index]

            with urllib.request.urlopen(
                root + f"/api/jobs/{self.pending_job_id}/resume-content", timeout=10
            ) as response:
                editable = json.loads(response.read().decode("utf-8"))
            self.assertTrue(editable["photo_available"])
            content = editable["content"]
            self.assertTrue(content["person"]["photo_path"])

            # 编辑并重渲染
            content["summary"] = "用户在 Dashboard 编辑后的摘要。"
            edited = post_json(
                f"/api/jobs/{self.pending_job_id}/resume-content",
                {"confirmed": True, "content": content},
            )
            self.assertTrue(edited["ok"])  # type: ignore[index]
            self.assertTrue(str(edited["version_id"]).endswith("edited-v1"))  # type: ignore[index]
            self.assertEqual(edited["workspace"]["resume_status"], "needs_review")  # type: ignore[index]

            with urllib.request.urlopen(
                root + f"/api/jobs/{self.pending_job_id}/resume-content", timeout=10
            ) as response:
                reloaded = json.loads(response.read().decode("utf-8"))
            self.assertEqual(reloaded["content"]["summary"], "用户在 Dashboard 编辑后的摘要。")

            # 结构非法的编辑应 400
            broken = json.loads(json.dumps(content))
            broken["person"]["name"] = ""
            with self.assertRaises(urllib.error.HTTPError) as raised:
                post_json(
                    f"/api/jobs/{self.pending_job_id}/resume-content",
                    {"confirmed": True, "content": broken},
                )
            self.assertEqual(raised.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


    def test_agent_api_requires_persistent_token_and_serves_readonly_data(self) -> None:
        private_dir = self.root / "private"
        server = create_dashboard_server(
            self.repository,
            output_dir=self.output_dir,
            port=0,
            private_dir=private_dir,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            token_path = private_dir / "agent-token.json"
            self.assertTrue(token_path.is_file())
            agent_token = json.loads(token_path.read_text(encoding="utf-8"))["token"]
            self.assertTrue(agent_token)

            # 无 token / 错 token → 403
            for headers in ({}, {"X-Agent-Token": "wrong-token"}):
                request = urllib.request.Request(
                    root + "/api/agent/state", headers=headers
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(raised.exception.code, 403)

            def agent_get(path: str) -> object:
                request = urllib.request.Request(
                    root + path, headers={"X-Agent-Token": agent_token}
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.loads(response.read().decode("utf-8"))

            state = agent_get("/api/agent/state")
            self.assertEqual(state["stats"]["jobs"], 2)  # type: ignore[index]
            self.assertEqual(state["funnel"]["applied_or_later"], 1)  # type: ignore[index]

            jobs = agent_get("/api/agent/jobs")
            self.assertEqual(jobs["count"], 2)  # type: ignore[index]

            detail = agent_get(f"/api/agent/jobs/{self.applied_job_id}")
            self.assertEqual(detail["job"]["job_id"], self.applied_job_id)  # type: ignore[index]
            self.assertEqual(detail["match_insight"]["why_fit"], [])  # type: ignore[index]  # 本地测试结果无洞察字段
            self.assertEqual(detail["application"]["status"], "applied")  # type: ignore[index]

            applications = agent_get("/api/agent/applications")
            self.assertEqual(applications["count"], 1)  # type: ignore[index]

            candidates = agent_get("/api/agent/candidates")
            self.assertEqual(candidates["count"], 1)  # type: ignore[index]

            # 未知岗位应 400 而非 500
            with self.assertRaises(urllib.error.HTTPError) as raised:
                agent_get("/api/agent/jobs/999")
            self.assertEqual(raised.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


    def test_http_resume_polish_suggestion_flow(self) -> None:
        private_dir = self.root / "private"
        profile_path = private_dir / "profile.json"
        save_profile(sample_profile(), profile_path)
        write_profile_photo(private_dir)
        save_runtime_config(
            RuntimeConfig(
                ai=AIConnectorConfig(
                    provider="openai_compatible",
                    model="test-model",
                    base_url="http://127.0.0.1:9",
                    api_key="test-key",
                )
            ),
            private_dir / "app-settings.json",
        )
        server = create_dashboard_server(
            self.repository,
            output_dir=self.output_dir,
            port=0,
            profile_path=profile_path,
            private_dir=private_dir,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"

        def fake_invoke(config, user_payload, **kwargs):
            payload = json.loads(user_payload)
            if "experiences" in payload:
                experience = payload["experiences"][0]
                fact = experience["facts"][0]
                return (json.dumps({"summary": "数据分析与周度报告实践（贴合 JD）", "summary_fact_ids": [fact["id"]],
                    "self_evaluation": "", "skill_ids": [payload["skills"][0]["id"]],
                    "entries": [{"experience_id": experience["id"], "bullets": [{"label": "数据分析", "text": fact["statement"], "fact_ids": [fact["id"]]}]}],
                    "strategy": "优先呈现数据分析证据", "questions": []}, ensure_ascii=False), 42, 24)
            resume = payload["resume"]
            skills = list(resume["skills"])[:11] + ["JD 对齐技能"]
            bullets = [
                {"bullet_id": item["bullet_id"], "text": f"（JD 对齐）{item['text']}"}
                for item in resume["bullets"]
            ]
            return (
                json.dumps(
                    {
                        "summary": f"{resume['summary']}（贴合 JD）",
                        "skills": skills,
                        "bullets": bullets,
                    },
                    ensure_ascii=False,
                ),
                42,
                24,
            )

        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=10) as response:
                token = json.loads(response.read().decode("utf-8"))["action_token"]

            def post_json(path: str, payload: dict[str, object]) -> object:
                request = urllib.request.Request(
                    root + path,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": root,
                        "X-Job-Agent-Token": token,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))

            # 无草稿岗位 → 404 之外的 400（ResumeEditorError）
            with mock.patch(
                "job_agent.services.resume_polish._invoke_ai", side_effect=fake_invoke
            ), mock.patch("job_agent.services.resume_compose._invoke_ai", side_effect=fake_invoke):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    post_json(
                        f"/api/jobs/{self.pending_job_id}/resume-content/polish",
                        {"confirmed": True},
                    )
                self.assertEqual(raised.exception.code, 400)

                # 生成草稿后润色
                draft = post_json(
                    f"/api/jobs/{self.pending_job_id}/resume-draft",
                    {"confirmed": True},
                )
                self.assertEqual(draft["workspace"]["resume_status"], "needs_review")  # type: ignore[index]

                with urllib.request.urlopen(root + f"/api/jobs/{self.pending_job_id}/resume-content", timeout=10) as response:
                    edited = json.loads(response.read().decode("utf-8"))["content"]
                edited["self_evaluation"] = "习惯通过数据复盘工作。"
                polished = post_json(
                    f"/api/jobs/{self.pending_job_id}/resume-content/polish",
                    {"confirmed": True, "content": edited},
                )
                self.assertEqual(polished["content"]["self_evaluation"], edited["self_evaluation"])
                wrong_job = json.loads(json.dumps(edited))
                wrong_job["target"]["job_id"] = self.pending_job_id + 999
                with self.assertRaises(urllib.error.HTTPError) as mismatch:
                    post_json(f"/api/jobs/{self.pending_job_id}/resume-content/polish", {"confirmed": True, "content": wrong_job})
                self.assertEqual(mismatch.exception.code, 400)
            self.assertTrue(polished["ok"])  # type: ignore[index]
            self.assertGreater(polished["change_count"], 0)  # type: ignore[index]
            self.assertIn("（贴合 JD）", polished["content"]["summary"])  # type: ignore[index]
            self.assertIn("JD 对齐技能", polished["content"]["skills"])  # type: ignore[index]
            first_bullet = polished["content"]["experience_sections"][0]["entries"][0]["bullets"][0]["text"]  # type: ignore[index]
            self.assertTrue(str(first_bullet).startswith("（JD 对齐）"))
            with mock.patch(
                "job_agent.services.resume_polish._invoke_ai",
                side_effect=CloudAIUnavailableError("OpenAI 身份验证失败（401）。"),
            ):
                fallback = post_json(
                    f"/api/jobs/{self.pending_job_id}/resume-content/polish",
                    {"confirmed": True, "content": edited},
                )
            self.assertEqual(fallback["engine"], "local_fallback")  # type: ignore[index]
            self.assertIn("401", fallback["warning"])  # type: ignore[index]
            # 建议不落盘：重新 GET 仍是原稿
            with urllib.request.urlopen(
                root + f"/api/jobs/{self.pending_job_id}/resume-content", timeout=10
            ) as response:
                reloaded = json.loads(response.read().decode("utf-8"))
            # 初次生成已按 JD 润色一次；“获取建议”不会把第二次建议写回文件。
            self.assertEqual(reloaded["content"]["summary"].count("（贴合 JD）"), 1)
            # 用量记账
            usage = json.loads(
                (private_dir / "api-usage.json").read_text(encoding="utf-8")
            )
            self.assertGreaterEqual(usage["ai"]["successful_requests"], 1)

            # Agent 对话动作会把同一份受事实约束的建议直接渲染成新版本，
            # 但仍保持 pending_user_review，不能直接进入投递材料。
            with mock.patch(
                "job_agent.services.resume_polish._invoke_ai", side_effect=fake_invoke
            ), mock.patch("job_agent.services.resume_compose._invoke_ai", side_effect=fake_invoke):
                revised = post_json(
                    f"/api/jobs/{self.pending_job_id}/resume-content/revise",
                    {
                        "confirmed": True,
                        "instruction": "按 JD 与 STAR 法则突出数据分析动作",
                    },
                )
            self.assertTrue(revised["ok"])  # type: ignore[index]
            self.assertEqual(revised["workspace"]["resume_status"], "needs_review")  # type: ignore[index]
            self.assertGreater(revised["change_count"], 0)  # type: ignore[index]
            with urllib.request.urlopen(
                root + f"/api/jobs/{self.pending_job_id}/resume-content", timeout=10
            ) as response:
                direct = json.loads(response.read().decode("utf-8"))
            self.assertIn("（贴合 JD）", direct["content"]["summary"])
            manifest = json.loads(Path(direct["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["edit_origin"], "agent_dialog")
            self.assertTrue(manifest["ai_assisted"])
            self.assertFalse(manifest["edited_by_user"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_resume_import_suggestion_flow(self) -> None:
        private_dir = self.root / "private"
        profile_path = private_dir / "profile.json"
        save_profile(sample_profile(), profile_path)
        write_profile_photo(private_dir)
        save_runtime_config(
            RuntimeConfig(
                ai=AIConnectorConfig(
                    provider="openai_compatible",
                    model="test-model",
                    base_url="http://127.0.0.1:9",
                    api_key="test-key",
                )
            ),
            private_dir / "app-settings.json",
        )
        server = create_dashboard_server(
            self.repository,
            output_dir=self.output_dir,
            port=0,
            profile_path=profile_path,
            private_dir=private_dir,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"

        resume_text = (
            "样本用户，擅长 SQL 与看板。实习：样本公司 运营实习生 2025.06-2025.09，"
            "效率提升 20%，搭建 3 张看板。教育：样本大学 信息管理 本科。"
        )

        def fake_invoke(config, user_payload):
            payload = json.loads(user_payload)
            text = payload["resume_text"]
            assert "20%" in text
            return (
                json.dumps(
                    {
                        "person": {"name": "样本用户", "city": "上海"},
                        "summary": "数据运营实习生，擅长 SQL 与数据看板。",
                        "skills": ["SQL", "数据看板"],
                        "experience_sections": [
                            {
                                "title": "实习经历",
                                "entries": [
                                    {
                                        "organization": "样本公司",
                                        "role": "运营实习生",
                                        "dates": "2025.06-2025.09",
                                        "bullets": [
                                            {"label": "结果", "text": "效率提升 20%。"},
                                            {"label": "动作", "text": "搭建 3 张看板。"},
                                        ],
                                    }
                                ],
                            }
                        ],
                        "education": [
                            {
                                "institution": "样本大学",
                                "degree": "本科",
                                "major": "信息管理",
                                "start": "",
                                "end": "",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                60,
                80,
            )

        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=10) as response:
                token = json.loads(response.read().decode("utf-8"))["action_token"]

            def post_json(path: str, payload: dict[str, object]) -> object:
                request = urllib.request.Request(
                    root + path,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": root,
                        "X-Job-Agent-Token": token,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))

            # 先生成草稿
            draft = post_json(
                f"/api/jobs/{self.pending_job_id}/resume-draft",
                {"confirmed": True},
            )
            self.assertEqual(draft["workspace"]["resume_status"], "needs_review")  # type: ignore[index]

            # 空文本 → 400
            with mock.patch(
                "job_agent.services.resume_import._invoke_ai", side_effect=fake_invoke
            ):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    post_json(
                        f"/api/jobs/{self.pending_job_id}/resume-content/import",
                        {"resume_text": "  "},
                    )
                self.assertEqual(raised.exception.code, 400)

                imported = post_json(
                    f"/api/jobs/{self.pending_job_id}/resume-content/import",
                    {"resume_text": resume_text},
                )
            self.assertTrue(imported["ok"])  # type: ignore[index]
            content = imported["content"]  # type: ignore[index]
            self.assertEqual(content["person"]["name"], "样本用户")
            self.assertIn("20%", content["experience_sections"][0]["entries"][0]["bullets"][0]["text"])
            self.assertEqual(content["education"][0]["institution"], "样本大学")
            # 岗位绑定沿用当前草稿
            self.assertEqual(content["target"]["job_id"], self.pending_job_id)
            # 建议不落盘：重新 GET 仍是原稿
            with urllib.request.urlopen(
                root + f"/api/jobs/{self.pending_job_id}/resume-content", timeout=10
            ) as response:
                reloaded = json.loads(response.read().decode("utf-8"))
            self.assertNotEqual(
                reloaded["content"]["person"]["name"], "样本用户"
            )
            # 用量记账
            usage = json.loads(
                (private_dir / "api-usage.json").read_text(encoding="utf-8")
            )
            self.assertGreaterEqual(usage["ai"]["successful_requests"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
