import base64
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import httpx
from openai import AuthenticationError

from job_agent.constants import MAX_JSON_BODY, MAX_RESUME_CONTENT_BODY
from job_agent.models.job import MatchResult, Recommendation, ScoreBreakdown
from job_agent.models.job_record import JobRecordInput, SearchCandidateInput
from job_agent.models.profile import CommutePreferences
from job_agent.services.dashboard import (
    build_dashboard_snapshot,
    create_dashboard_server,
)
from job_agent.services.api_usage import load_api_usage
from job_agent.services.job_repository import JobRepository
from job_agent.services.local_matcher import structure_job_locally
from job_agent.services.profile_store import load_profile, save_profile
from job_agent.services.portable_resume import find_latest_resume_manifest
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
    def test_corrupt_profile_keeps_job_history_visible_without_replacing_profile(self) -> None:
        profile_path = self.root / "private" / "profile.json"
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text("{invalid", encoding="utf-8")
        snapshot = build_dashboard_snapshot(
            self.repository, output_dir=self.output_dir, profile_path=profile_path,
        )
        self.assertTrue(snapshot.profile_error)
        self.assertIsNone(snapshot.active_profile)
        self.assertEqual(len(snapshot.tracked_jobs), 2)
        self.assertEqual(profile_path.read_text(encoding="utf-8"), "{invalid")

    def test_connection_probe_respects_quota_and_never_sends_saved_key_to_new_endpoint(self) -> None:
        private_dir = self.root / "private"
        save_runtime_config(RuntimeConfig(ai=AIConnectorConfig(
            provider="openai", model="review-model", api_key="saved-test-key", monthly_quota=1,
        )), private_dir / "app-settings.json")
        server = create_dashboard_server(
            self.repository, output_dir=self.output_dir, port=0, private_dir=private_dir,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                token = json.load(response)["action_token"]

            def probe(payload: dict) -> dict:
                request = urllib.request.Request(
                    root + "/api/settings/test-connection",
                    data=json.dumps(payload).encode("utf-8"), method="POST",
                    headers={"Content-Type": "application/json", "Origin": root, "X-Job-Agent-Token": token},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.load(response)

            with mock.patch("openai.OpenAI") as client_type:
                client_type.return_value.__enter__.return_value.chat.completions.create.return_value = SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
                )
                untrusted = probe({"provider": "openai_compatible", "model": "review-model",
                                   "base_url": "https://untrusted.example/v1"})
                self.assertFalse(untrusted["ok"])
                self.assertIn("密钥", untrusted["message"])
                client_type.assert_not_called()

                first = probe({"provider": "openai", "model": "review-model",
                               "base_url": "https://untrusted.example/v1"})
                self.assertTrue(first["ok"])
                self.assertIsNone(client_type.call_args.kwargs["base_url"])
                self.assertEqual(client_type.call_args.kwargs["api_key"], "saved-test-key")
                second = probe({"provider": "openai", "model": "review-model"})
                self.assertFalse(second["ok"])
                self.assertIn("额度", second["message"])
                self.assertEqual(client_type.call_count, 1)
            usage = load_api_usage(private_dir / "api-usage.json")
            self.assertEqual((usage.ai.successful_requests, usage.ai.input_tokens, usage.ai.output_tokens), (1, 1, 2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_connection_probe_can_retry_after_definitive_401_with_corrected_key(self) -> None:
        private_dir = self.root / "private"
        save_runtime_config(RuntimeConfig(ai=AIConnectorConfig(
            provider="openai", model="review-model", api_key="saved-test-key", monthly_quota=1,
        )), private_dir / "app-settings.json")
        server = create_dashboard_server(
            self.repository, output_dir=self.output_dir, port=0, private_dir=private_dir,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                token = json.load(response)["action_token"]

            def probe(key: str) -> dict:
                request = urllib.request.Request(
                    root + "/api/settings/test-connection",
                    data=json.dumps({"provider": "openai", "model": "review-model", "api_key": key}).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json", "Origin": root, "X-Job-Agent-Token": token},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.load(response)

            auth_error = AuthenticationError(
                "synthetic invalid API key",
                response=httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
                body={"error": {"type": "invalid_api_key"}},
            )
            with mock.patch("openai.OpenAI") as client_type:
                create = client_type.return_value.__enter__.return_value.chat.completions.create
                create.side_effect = [
                    auth_error,
                    SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2)),
                ]
                first = probe("wrong-test-key")
                self.assertFalse(first["ok"])
                self.assertEqual(first["status_code"], 401)
                self.assertEqual(len(load_api_usage(private_dir / "api-usage.json").pending), 0)
                second = probe("corrected-test-key")
                self.assertTrue(second["ok"])
                self.assertEqual(create.call_count, 2)
            usage = load_api_usage(private_dir / "api-usage.json")
            self.assertEqual(usage.ai.successful_requests, 1)
            self.assertFalse(usage.pending)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_live_browser_use_is_paused_for_every_quota_setting(self) -> None:
        profile_path = self.root / "private" / "profile.json"
        config_path = profile_path.parent / "app-settings.json"
        save_profile(sample_profile(), profile_path)
        save_runtime_config(RuntimeConfig(ai=AIConnectorConfig(
            provider="openai", model="test-model", api_key="test-only-key",
            monthly_quota=1,
        )), config_path)
        server = create_dashboard_server(
            self.repository, output_dir=self.output_dir, port=0,
            profile_path=profile_path,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                token = json.load(response)["action_token"]

            def post():
                request = urllib.request.Request(
                    root + f"/api/jobs/{self.pending_job_id}/browser-use",
                    data=json.dumps({"dry_run": False}).encode("utf-8"), method="POST",
                    headers={
                        "Content-Type": "application/json", "Origin": root,
                        "X-Job-Agent-Token": token,
                    },
                )
                return urllib.request.urlopen(request, timeout=5)

            with mock.patch(
                "job_agent.services.dashboard_routes.jobs_api.run_browser_use_assist"
            ) as runner:
                with self.assertRaises(urllib.error.HTTPError) as quota_error:
                    post()
                self.assertEqual(quota_error.exception.code, 400)
                self.assertIn("实站 AI 代填已暂停", quota_error.exception.read().decode("utf-8"))
                runner.assert_not_called()

                save_runtime_config(RuntimeConfig(ai=AIConnectorConfig(
                    provider="openai", model="test-model", api_key="test-only-key",
                )), config_path)
                with self.assertRaises(urllib.error.HTTPError) as paused_error:
                    post()
                self.assertEqual(paused_error.exception.code, 400)
                self.assertIn("实站 AI 代填已暂停", paused_error.exception.read().decode("utf-8"))
                runner.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_interview_prep_requires_authenticated_post(self) -> None:
        profile_path = self.root / "private" / "profile.json"
        save_profile(sample_profile(), profile_path)
        save_runtime_config(
            RuntimeConfig(ai=AIConnectorConfig(provider="local")),
            profile_path.parent / "app-settings.json",
        )
        server = create_dashboard_server(
            self.repository, output_dir=self.output_dir, port=0,
            profile_path=profile_path,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        path = f"/api/jobs/{self.pending_job_id}/interview-prep"
        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                token = json.load(response)["action_token"]
            with self.assertRaises(urllib.error.HTTPError) as get_error:
                urllib.request.urlopen(root + path, timeout=5)
            self.assertEqual(get_error.exception.code, 404)
            request = urllib.request.Request(root + path, data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as no_token:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(no_token.exception.code, 403)
            request = urllib.request.Request(
                root + path, data=b"{}", method="POST",
                headers={"Origin": root, "X-Job-Agent-Token": token},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["engine"], "local_template")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

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

    def test_snapshot_keeps_over_2000_jobs_and_over_1000_applications(self) -> None:
        """Large local histories must remain reachable rather than silently truncate."""
        self.repository.initialize()
        stamp = "2026-09-09T00:00:00+00:00"
        with self.repository._connection() as connection:
            connection.executemany(
                """
                INSERT INTO jobs(id, dedupe_key, company, title, location, jd_text,
                                 status, match_score, recommendation, created_at,
                                 updated_at, first_seen_at, last_seen_at)
                VALUES (?, ?, '批量示例公司', ?, '上海', ?, 'discovered', 80,
                        'recommend', ?, ?, ?, ?)
                """,
                [
                    (10000 + index, f"bulk-{index}", f"运营实习生 {index}", JD,
                     stamp, stamp, stamp, stamp)
                    for index in range(2050)
                ],
            )
            connection.executemany(
                """
                INSERT INTO applications(id, job_id, status, applied_at,
                                         created_at, updated_at)
                VALUES (?, ?, 'applied', ?, ?, ?)
                """,
                [(10000 + index, 10000 + index, stamp, stamp, stamp)
                 for index in range(1101)],
            )
            connection.execute(
                """
                INSERT INTO application_events(application_id, previous_status,
                                               status, source, detail, occurred_at)
                VALUES (11100, 'applied', 'resume_requested', 'manual', '', ?)
                """,
                (stamp,),
            )
            connection.executemany(
                """
                INSERT INTO search_candidates(candidate_key, canonical_url, title,
                                              created_at, updated_at, first_seen_at,
                                              last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [(f"bulk-candidate-{index}", f"https://example.com/c/{index}",
                  f"候选 {index}", stamp, stamp, stamp, stamp)
                 for index in range(1101)],
            )
            connection.execute(
                """
                INSERT INTO job_sources(job_id, source_key, platform, source_url,
                                        jd_text, first_seen_at, last_seen_at)
                VALUES (12049, 'last-bulk-source', '企业官网',
                        'https://example.com/jobs/last', ?, ?, ?)
                """,
                (JD, stamp, stamp),
            )

        snapshot = build_dashboard_snapshot(
            self.repository, output_dir=self.output_dir, today=date(2026, 9, 9),
        )
        metrics = {item.metric_id: item.value for item in snapshot.metrics}
        rows = {item.job_id: item for item in snapshot.tracked_jobs}
        self.assertEqual(len(rows), 2052)
        self.assertEqual(metrics["jobs"], 2052)
        self.assertEqual(metrics["candidates"], 1102)
        self.assertEqual(metrics["applied"], 1102)
        self.assertEqual(rows[11100].status, "applied")
        self.assertEqual(rows[12049].source_url, "https://example.com/jobs/last")
        self.assertIn(12049, {item.job_id for item in snapshot.jobs_to_apply})
        self.assertIn(11100, {item.job_id for item in snapshot.recent_feedback})

    def test_dashboard_refresh_reuses_snapshot_until_database_changes(self) -> None:
        server = create_dashboard_server(self.repository, output_dir=self.output_dir, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/dashboard"
        try:
            with mock.patch(
                "job_agent.services.dashboard_routes.dashboard_api.build_dashboard_snapshot",
                wraps=build_dashboard_snapshot,
            ) as builder:
                with urllib.request.urlopen(url, timeout=5) as response:
                    first = json.load(response)
                with urllib.request.urlopen(url, timeout=5) as response:
                    second = json.load(response)
                self.assertEqual(builder.call_count, 1)
                self.assertEqual(first, second)

                self.repository.set_job_archived(self.pending_job_id, True)
                with urllib.request.urlopen(url, timeout=5) as response:
                    updated = json.load(response)
                self.assertEqual(builder.call_count, 2)
                row = next(item for item in updated["tracked_jobs"] if item["job_id"] == self.pending_job_id)
                self.assertTrue(row["job_archived"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_candidate_queue_pages_and_requires_confirmed_local_action(self) -> None:
        for index in range(24):
            candidate = self.repository.upsert_search_candidate(SearchCandidateInput(
                provider="synthetic", query="运营实习", title=f"示例候选 {index:02d}",
                url=f"https://example.com/candidate/batch-{index}",
            ))
            self.repository.mark_candidate_verification(candidate.candidate_id, "needs_manual_review")
        server = create_dashboard_server(self.repository, output_dir=self.output_dir, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                token = json.load(response)["action_token"]
            with self.assertRaises(urllib.error.HTTPError) as missing_token:
                urllib.request.urlopen(root + "/api/candidates", timeout=5)
            self.assertEqual(missing_token.exception.code, 403)
            def get_page(page: int) -> dict:
                request = urllib.request.Request(
                    root + f"/api/candidates?status=needs_manual_review&page={page}",
                    headers={"X-Job-Agent-Token": token},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.load(response)
            first, second = get_page(0), get_page(1)
            self.assertEqual((first["total"], len(first["items"]), len(second["items"])), (24, 20, 4))
            candidate_id = first["items"][0]["candidate_id"]
            def verify(confirmed: bool):
                request = urllib.request.Request(
                    root + f"/api/candidates/{candidate_id}/verify",
                    data=json.dumps({"status": "live", "confirmed": confirmed}).encode("utf-8"),
                    headers={"X-Job-Agent-Token": token, "Content-Type": "application/json"},
                    method="POST",
                )
                return urllib.request.urlopen(request, timeout=5)
            with self.assertRaises(urllib.error.HTTPError) as unconfirmed:
                verify(False)
            self.assertEqual(unconfirmed.exception.code, 400)
            with verify(True) as response:
                self.assertTrue(json.load(response)["ok"])
            self.assertEqual(get_page(0)["total"], 23)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

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
                self.assertIn('<script type="module" src="/js/main.js">', html)
                self.assertIn('id="todayPanel"', html)
                self.assertIn("今天，先推进一个岗位", html)
                self.assertIn("实习机会", html)
                self.assertIn("校招机会", html)
                self.assertIn("能力与限制", html)
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

            with urllib.request.urlopen(root + "/js/main.js", timeout=5) as response:
                self.assertIn("javascript", response.headers["Content-Type"])
                script = response.read().decode("utf-8")
                self.assertIn("生成草稿", script)
                self.assertIn("打开招聘页", script)
                self.assertIn("原始职位", script)
                self.assertIn("投递记录", script)
                self.assertIn("接下来再处理", script)
                self.assertIn("项目练习", script)

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
                self.assertIn(settings["ai"]["provider"], {"local", "openai", "deepseek"})
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

    def test_profile_switch_hides_intermediate_state_from_dashboard_reads(self) -> None:
        private_dir = self.root / "private"
        profile_path = private_dir / "profile.json"
        save_profile(sample_profile(), profile_path)
        self.repository.set_job_archived(self.pending_job_id, True)
        server = create_dashboard_server(
            self.repository, output_dir=self.output_dir, port=0,
            profile_path=profile_path, private_dir=private_dir,
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        published = threading.Event()
        resume_switch = threading.Event()
        read_done = threading.Event()
        results: dict[str, object] = {}
        original_switch = self.repository.backup_and_clear_for_new_profile

        def pause_after_publish(backup_dir: Path, **kwargs: object) -> Path:
            publish = kwargs["publish_profile"]

            def paused_publish() -> None:
                publish()
                published.set()
                if not resume_switch.wait(5):
                    raise TimeoutError("test switch wait expired")

            kwargs["publish_profile"] = paused_publish
            return original_switch(backup_dir, **kwargs)

        def send_switch(token: str) -> None:
            try:
                content_type, body = multipart_payload(
                    {"mode": "replace", "display_name": "新用户",
                     "target_roles": "数据运营", "confirm_truth": "true",
                     "confirm_replace": "true"},
                    filename="new.txt", file_payload="使用 SQL 完成数据分析。".encode("utf-8"),
                )
                request = urllib.request.Request(
                    root + "/api/profile/onboard", data=body,
                    headers={"Content-Type": content_type, "Origin": root,
                             "X-Job-Agent-Token": token}, method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    results["switch"] = json.load(response)
            except Exception as exc:
                results["switch_error"] = exc

        def read_dashboard() -> None:
            try:
                with urllib.request.urlopen(root + "/api/dashboard", timeout=10) as response:
                    results["dashboard"] = json.load(response)
            except Exception as exc:
                results["read_error"] = exc
            finally:
                read_done.set()

        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                token = json.load(response)["action_token"]
            with mock.patch.object(
                self.repository, "backup_and_clear_for_new_profile", pause_after_publish,
            ):
                switch_thread = threading.Thread(target=send_switch, args=(token,), daemon=True)
                switch_thread.start()
                self.assertTrue(published.wait(5))
                read_thread = threading.Thread(target=read_dashboard, daemon=True)
                read_thread.start()
                self.assertFalse(read_done.wait(0.2))
                resume_switch.set()
                switch_thread.join(timeout=10)
                read_thread.join(timeout=10)
            self.assertNotIn("switch_error", results)
            self.assertNotIn("read_error", results)
            self.assertTrue(results["switch"]["ok"])
            self.assertEqual(results["dashboard"]["active_profile"]["display_name"], "新用户")
            self.assertEqual(results["dashboard"]["tracked_jobs"], [])
        finally:
            resume_switch.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_switch_archives_old_resume_pack_chat_and_photo_before_job_id_reuse(self) -> None:
        private_dir = self.root / "private"
        profile_path = private_dir / "profile.json"
        save_profile(sample_profile(), profile_path)
        write_profile_photo(private_dir)
        (private_dir / "copilot").mkdir()
        (private_dir / "copilot" / "current-thread.json").write_text("old private chat", encoding="utf-8")
        apps = self.output_dir / "applications" / "old-job"
        apps.mkdir(parents=True)
        old_pdf = b"%PDF-1.4 old synthetic user"
        (apps / "old.pdf").write_bytes(old_pdf)
        (apps / "resume-version-old.json").write_text(json.dumps({
            "target": {"job_id": self.applied_job_id},
            "qa": {"pdf_visual_review": "passed", "truthfulness_check": "passed"},
            "artifacts": {
                "pdf": {"path": "old.pdf", "sha256": hashlib.sha256(old_pdf).hexdigest().upper()},
                "docx": {"path": "old.docx", "sha256": "DUMMY"},
            },
        }), encoding="utf-8")
        old_pack = self.output_dir / "preparation-packs" / f"job-{self.applied_job_id}-old" / "run"
        old_pack.mkdir(parents=True)
        (old_pack / "岗位学习与面试准备.md").write_text("old private preparation", encoding="utf-8")
        server = create_dashboard_server(
            self.repository, output_dir=self.output_dir, port=0,
            profile_path=profile_path, private_dir=private_dir,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"

        def assert_404(path: str) -> None:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(root + path, timeout=10)
            self.assertEqual(raised.exception.code, 404)

        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=10) as response:
                token = json.load(response)["action_token"]
            with urllib.request.urlopen(root + f"/resume-draft/{self.applied_job_id}/pdf", timeout=10) as response:
                self.assertEqual(response.read(), old_pdf)
            with urllib.request.urlopen(root + f"/preparation/{self.applied_job_id}", timeout=10) as response:
                self.assertIn(b"old private preparation", response.read())
            content_type, body = multipart_payload(
                {"mode": "replace", "display_name": "新用户", "email": "new@example.com",
                 "target_roles": "数据运营", "confirm_truth": "true", "confirm_replace": "true"},
                filename="new.txt", file_payload="使用 SQL 完成数据分析。".encode("utf-8"),
            )
            request = urllib.request.Request(
                root + "/api/profile/onboard", data=body,
                headers={"Content-Type": content_type, "Origin": root,
                         "X-Job-Agent-Token": token}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                switched = json.load(response)
            self.assertTrue(switched["ok"])
            self.assertNotEqual(switched["action_token"], token)
            self.assertEqual((Path(switched["output_backup"]) / "applications" / "old-job" / "old.pdf").read_bytes(), old_pdf)
            self.assertTrue((Path(switched["private_backup"]) / "photo" / "profile-photo.png").is_file())
            self.assertTrue((Path(switched["private_backup"]) / "copilot" / "current-thread.json").is_file())
            self.assertFalse((private_dir / "photo").exists())
            self.assertFalse((private_dir / "copilot").exists())
            assert_404(f"/resume-draft/{self.applied_job_id}/pdf")
            assert_404(f"/preparation/{self.applied_job_id}")

            with self.repository._connection() as connection:
                connection.execute("DELETE FROM sqlite_sequence WHERE name='jobs'")
            reused = self.repository.upsert_job(JobRecordInput(
                company="新用户企业", title="新用户岗位", jd_text=JD,
                source="synthetic", location="上海",
            ))
            self.assertEqual(reused.job_id, self.applied_job_id)
            assert_404(f"/resume-draft/{reused.job_id}/pdf")
            assert_404(f"/preparation/{reused.job_id}")
            draft_request = urllib.request.Request(
                root + f"/api/jobs/{reused.job_id}/resume-draft",
                data=b'{"confirmed":true}',
                headers={"Content-Type": "application/json", "Origin": root,
                         "X-Job-Agent-Token": switched["action_token"]}, method="POST",
            )
            with urllib.request.urlopen(draft_request, timeout=20) as response:
                self.assertTrue(json.load(response)["ok"])
            manifest = find_latest_resume_manifest(self.output_dir / "applications", reused.job_id)
            self.assertIsNotNone(manifest)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["qa"]["embedded_photos"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_import_reading_old_profile_finishes_before_switch_clears_jobs(self) -> None:
        private_dir = self.root / "private"
        profile_path = private_dir / "profile.json"
        save_profile(sample_profile(), profile_path)
        server = create_dashboard_server(
            self.repository, output_dir=self.output_dir, port=0,
            profile_path=profile_path, private_dir=private_dir,
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        profile_loaded = threading.Event()
        finish_import = threading.Event()
        switch_done = threading.Event()
        results: dict[str, object] = {}

        def post(path: str, body: bytes, content_type: str, token: str, key: str) -> None:
            try:
                request = urllib.request.Request(
                    root + path, data=body,
                    headers={"Content-Type": content_type, "Origin": root,
                             "X-Job-Agent-Token": token}, method="POST",
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    results[key] = json.load(response)
            except Exception as exc:
                results[key + "_error"] = exc
            finally:
                if key == "switch":
                    switch_done.set()

        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                token = json.load(response)["action_token"]
            from job_agent.services.dashboard_routes import jobs_api
            original_load = jobs_api.load_profile

            def pause_after_old_profile(path: Path):
                profile = original_load(path)
                profile_loaded.set()
                if not finish_import.wait(5):
                    raise TimeoutError("test import wait expired")
                return profile

            with mock.patch.object(jobs_api, "load_profile", pause_after_old_profile):
                import_thread = threading.Thread(
                    target=post,
                    args=("/api/jobs/import-parsed", json.dumps({
                        "company": "旧用户并发企业", "title": "旧用户岗位",
                        "jd_text": JD, "source": "synthetic",
                    }).encode("utf-8"), "application/json", token, "import"),
                    daemon=True,
                )
                import_thread.start()
                self.assertTrue(profile_loaded.wait(5))
                content_type, body = multipart_payload(
                    {"mode": "replace", "display_name": "新用户", "target_roles": "数据运营",
                     "confirm_truth": "true", "confirm_replace": "true"},
                    filename="new.txt", file_payload="使用 SQL 完成数据分析。".encode("utf-8"),
                )
                switch_thread = threading.Thread(
                    target=post,
                    args=("/api/profile/onboard", body, content_type, token, "switch"),
                    daemon=True,
                )
                switch_thread.start()
                self.assertFalse(switch_done.wait(0.2))
                finish_import.set()
                import_thread.join(timeout=10)
                switch_thread.join(timeout=10)
            self.assertNotIn("import_error", results)
            self.assertNotIn("switch_error", results)
            self.assertTrue(results["import"]["ok"])
            self.assertTrue(results["switch"]["ok"])
            self.assertEqual(load_profile(profile_path).person.display_name, "新用户")
            self.assertEqual(self.repository.stats().jobs, 0)
        finally:
            finish_import.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_active_fill_session_blocks_profile_switch_without_changes(self) -> None:
        from job_agent.services.fill_bridge import FillBridge

        private_dir = self.root / "private"
        profile_path = private_dir / "profile.json"
        save_profile(sample_profile(), profile_path)
        old_bytes = profile_path.read_bytes()
        server = create_dashboard_server(
            self.repository, output_dir=self.output_dir, port=0,
            profile_path=profile_path, private_dir=private_dir,
        )
        bridge = FillBridge()
        session = bridge.create(
            self.applied_job_id, "https://example.com/jobs/applied",
            "https://example.com/jobs/applied",
        )
        server.fill_bridge = bridge
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                token = json.load(response)["action_token"]
            content_type, body = multipart_payload(
                {"mode": "replace", "display_name": "新用户", "target_roles": "数据运营",
                 "confirm_truth": "true", "confirm_replace": "true"},
                filename="new.txt", file_payload="使用 SQL 完成数据分析。".encode("utf-8"),
            )
            request = urllib.request.Request(
                root + "/api/profile/onboard", data=body,
                headers={"Content-Type": content_type, "Origin": root,
                         "X-Job-Agent-Token": token}, method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(raised.exception.code, 409)
            self.assertEqual(profile_path.read_bytes(), old_bytes)
            self.assertEqual(self.repository.stats().jobs, 2)
            bridge.close(session["session_id"])
            server.RequestHandlerClass.assist_runs["synthetic"] = {"status": "launching"}
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(raised.exception.code, 409)
            server.RequestHandlerClass.assist_runs.clear()
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertTrue(json.load(response)["ok"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_failed_switch_upload_is_quarantined_outside_served_routes(self) -> None:
        private_dir = self.root / "private"
        profile_path = private_dir / "profile.json"
        save_profile(sample_profile(), profile_path)
        server = create_dashboard_server(
            self.repository, output_dir=self.output_dir, port=0,
            profile_path=profile_path, private_dir=private_dir,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(root + "/api/dashboard", timeout=5) as response:
                old_dashboard = json.load(response)
            content_type, body = multipart_payload(
                {"mode": "replace", "display_name": "新用户", "target_roles": "数据运营",
                 "confirm_truth": "true", "confirm_replace": "true"},
                filename="bad.txt", file_payload="姓名\n电话\n简历".encode("utf-8"),
            )
            request = urllib.request.Request(
                root + "/api/profile/onboard", data=body,
                headers={"Content-Type": content_type, "Origin": root,
                         "X-Job-Agent-Token": old_dashboard["action_token"]}, method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(raised.exception.code, 400)
            self.assertIn("failed-profile-switch-", json.load(raised.exception)["error"])
            failed = next((private_dir / "backups").glob("failed-profile-switch-*"))
            filename = next((failed / "resumes").iterdir()).name
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(
                    root + f"/data/private/backups/{failed.name}/resumes/{filename}",
                    timeout=10,
                )
            self.assertEqual(missing.exception.code, 404)
            with urllib.request.urlopen(root + "/api/dashboard", timeout=10) as response:
                current = json.load(response)
            self.assertEqual(current["active_profile"], old_dashboard["active_profile"])
            self.assertEqual(len(current["tracked_jobs"]), 2)
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

            # 编辑器提交整份内容；未采纳事实与证据说明也在 JSON 中，但不进入版面。
            large_content = json.loads(json.dumps(content, ensure_ascii=False))
            excluded_unknowns = [
                f"未核实的示例信息 {index}：" + "测试" * 30
                for index in range(100)
            ]
            large_content["truthfulness"]["excluded_unknowns"] = excluded_unknowns
            large_payload = {"confirmed": True, "content": large_content}
            large_body = json.dumps(large_payload, ensure_ascii=False).encode("utf-8")
            self.assertGreater(len(large_body), MAX_JSON_BODY)
            self.assertLess(len(large_body), MAX_RESUME_CONTENT_BODY)
            polished = post_json(
                f"/api/jobs/{self.pending_job_id}/resume-content/polish",
                large_payload,
            )
            self.assertEqual(polished["job_id"], self.pending_job_id)  # type: ignore[index]
            try:
                large_edited = post_json(
                    f"/api/jobs/{self.pending_job_id}/resume-content",
                    large_payload,
                )
            except urllib.error.HTTPError as exc:
                self.fail(f"超过普通 JSON 上限的合法简历保存失败: {exc.read().decode('utf-8')}")
            self.assertTrue(large_edited["ok"])  # type: ignore[index]
            with urllib.request.urlopen(
                root + f"/api/jobs/{self.pending_job_id}/resume-content", timeout=10
            ) as response:
                large_reloaded = json.loads(response.read().decode("utf-8"))
            self.assertEqual(
                large_reloaded["content"]["truthfulness"]["excluded_unknowns"],
                excluded_unknowns,
            )

            too_large = json.dumps(
                {**large_payload, "padding": "x" * MAX_RESUME_CONTENT_BODY},
                ensure_ascii=False,
            ).encode("utf-8")
            self.assertGreater(len(too_large), MAX_RESUME_CONTENT_BODY)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                post_raw(
                    f"/api/jobs/{self.pending_job_id}/resume-content",
                    too_large,
                    "application/json",
                )
            self.assertEqual(raised.exception.code, 400)
            self.assertIn("请求内容过大", raised.exception.read().decode("utf-8"))

            # 其他普通 JSON 操作仍保持原上限。
            with self.assertRaises(urllib.error.HTTPError) as raised:
                post_json(
                    f"/api/jobs/{self.pending_job_id}/resume-draft",
                    {"confirmed": True, "padding": "x" * MAX_JSON_BODY},
                )
            self.assertEqual(raised.exception.code, 400)
            self.assertIn("请求内容过大", raised.exception.read().decode("utf-8"))

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
            self.assertEqual(polished["content"]["generation"]["engine"], "cloud_composition")  # type: ignore[index]
            self.assertTrue(polished["content"]["generation"]["review_required"])  # type: ignore[index]
            self.assertIn("cloud_wording_polish", polished["content"]["fact_review_origin"]["sources"])  # type: ignore[index]
            self.assertGreater(polished["change_count"], 0)  # type: ignore[index]
            self.assertIn("（贴合 JD）", polished["content"]["summary"])  # type: ignore[index]
            self.assertIn("JD 对齐技能", polished["content"]["skills"])  # type: ignore[index]
            first_bullet = polished["content"]["experience_sections"][0]["entries"][0]["bullets"][0]["text"]  # type: ignore[index]
            self.assertTrue(str(first_bullet).startswith("（JD 对齐）"))
            with mock.patch(
                "job_agent.services.resume_polish._invoke_ai",
                side_effect=CloudAIUnavailableError("OpenAI 身份验证失败（401）。"),
            ):
                with self.assertRaises(urllib.error.HTTPError) as unavailable:
                    post_json(
                        f"/api/jobs/{self.pending_job_id}/resume-content/polish",
                        {"confirmed": True, "content": edited},
                    )
                self.assertEqual(unavailable.exception.code, 400)
                self.assertIn("401", unavailable.exception.read().decode("utf-8"))
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

        def fake_invoke(config, user_payload, *, request_call=None):
            if request_call is not None:
                request_call(lambda: SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=60, output_tokens=80),
                ))
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

            # Explicit local fixture generation must not rely on a failed cloud API.
            configured = json.loads((private_dir / "app-settings.json").read_text(encoding="utf-8"))
            save_runtime_config(RuntimeConfig(), private_dir / "app-settings.json")
            draft = post_json(
                f"/api/jobs/{self.pending_job_id}/resume-draft",
                {"confirmed": True},
            )
            self.assertEqual(draft["workspace"]["resume_status"], "needs_review")  # type: ignore[index]

            save_runtime_config(RuntimeConfig.model_validate(configured), private_dir / "app-settings.json")

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
