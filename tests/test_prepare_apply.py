import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from job_agent.models.job import MatchResult, Recommendation, ScoreBreakdown
from job_agent.models.job_record import JobRecordInput
from job_agent.services.application_pack import resolve_resume_bundle
from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.job_repository import JobRepository
from job_agent.services.local_matcher import structure_job_locally
from job_agent.services.profile_store import save_profile
from job_agent.services.resume_editor import load_latest_resume_content, rerender_edited_resume
from job_agent.services.runtime_config import RuntimeConfig, save_runtime_config
from tests.helpers import sample_profile

JD = """
岗位职责：负责业务数据分析、策略运营与跨部门项目协作。
任职要求：本科及以上，每周至少 4 天，连续实习 3 个月。
""".strip()


def scored_result(*, company: str, title: str, score: int = 85) -> MatchResult:
    breakdown = ScoreBreakdown(
        role_direction=20,
        skills=25,
        experience=20,
        education=10,
        logistics=6,
        preferences=4,
    )
    structured = structure_job_locally(
        JD,
        company=company,
        title=title,
        location="上海",
        source="test",
        source_url="https://example.com/job/123",
    )
    return MatchResult(
        job=structured,
        score_breakdown=breakdown,
        overall_score=score,
        recommendation=Recommendation.RECOMMEND,
        engine="test",
    )


class PrepareApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.output_dir = self.root / "output"
        self.private_dir = self.root / "private"
        self.private_dir.mkdir(parents=True, exist_ok=True)
        self.profile_path = self.private_dir / "profile.json"
        self.config_path = self.private_dir / "app-settings.json"

        # Initialize profile and runtime config
        profile = sample_profile()
        save_profile(profile, self.profile_path)
        save_runtime_config(RuntimeConfig(), self.config_path)

        # Initialize repository and test job
        self.repository = JobRepository(self.root / "jobs.sqlite3")
        job = self.repository.upsert_job(
            JobRecordInput(
                company="测试互娱",
                title="数据分析实习生",
                jd_text=JD,
                source="测试招聘",
                source_url="https://example.com/jobs/game-data-intern",
                location="上海",
            )
        )
        self.job_id = job.job_id
        self.repository.add_match_result(
            self.job_id,
            scored_result(company="测试互娱", title="数据分析实习生", score=85),
        )

        self.server = create_dashboard_server(
            self.repository,
            output_dir=self.output_dir,
            profile_path=self.profile_path,
            private_dir=self.private_dir,
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.root_url = f"http://127.0.0.1:{self.server.server_port}"

        # Fetch action token
        with urllib.request.urlopen(self.root_url + "/api/dashboard", timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            self.token = data["action_token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp_dir.cleanup()

    def _post(self, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.root_url + path,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Origin": self.root_url,
                "X-Job-Agent-Token": self.token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8")
            raise RuntimeError(f"HTTPError {exc.code}: {err_body}") from exc

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(
            self.root_url + path,
            headers={"X-Job-Agent-Token": self.token},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def _make_unverified_cloud_resume(self) -> tuple[Path, dict]:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        content, _ = load_latest_resume_content(self.output_dir / "applications", self.job_id)
        bullet = content["experience_sections"][0]["entries"][0]["bullets"][0]
        bullet["text"] = bullet["text"].rstrip("。") + "，并获得国家级一等奖。"
        content["generation"] = {"engine": "cloud_composition", "review_required": True}
        files = rerender_edited_resume(
            content, self.output_dir / "applications", edit_origin="agent_dialog",
        )
        manifest = json.loads(files.manifest.read_text(encoding="utf-8"))
        return files.manifest, manifest

    def test_cloud_achievement_requires_separate_fact_review_before_open(self) -> None:
        manifest_path, manifest = self._make_unverified_cloud_resume()
        self.assertEqual(manifest["qa"]["truthfulness_check"], "pending_user_review")
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        version = detail["resume_version"]
        self.assertEqual(Path(version["manifest_path"]), manifest_path.resolve())
        self.assertTrue(version["fact_review_required"])
        self.assertTrue(any("国家级一等奖" in row["draft"] for row in version["fact_comparisons"]))
        request = {"manifest_path": str(manifest_path), "expected_sha256": version["pdf_sha256"]}
        with self.assertRaisesRegex(RuntimeError, "逐项核对"):
            self._post(f"/api/jobs/{self.job_id}/review-and-open", request)
        self.assertFalse(list(manifest_path.parent.glob("resume-version-approved-*.json")))
        with mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page"), \
             mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True), \
             mock.patch("os.startfile", create=True):
            approved = self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                **request, "confirmed_fact_review": True,
                "fact_review_sha256": version["pdf_sha256"],
                "fact_review_content_sha256": version["content_sha256"],
            })
        self.assertTrue(approved["ok"])
        approved_path = next(manifest_path.parent.glob("resume-version-approved-*.json"))
        approved_manifest = json.loads(approved_path.read_text(encoding="utf-8"))
        self.assertEqual(approved_manifest["qa"]["truthfulness_check"], "passed")
        self.assertEqual(approved_manifest["qa"]["pdf_visual_review"], "passed")
        self.assertEqual(approved_manifest["fact_approval"]["pdf_sha256"], version["pdf_sha256"])

    def test_legacy_cloud_passed_flag_is_not_fact_approval(self) -> None:
        manifest_path, manifest = self._make_unverified_cloud_resume()
        manifest["qa"]["truthfulness_check"] = "passed"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "逐项核对"):
            self._post(f"/api/jobs/{self.job_id}/resume-draft/approve", {
                "confirmed_pdf_review": True,
            })
        self.assertFalse(list(manifest_path.parent.glob("resume-version-approved-*.json")))
        pdf_sha = manifest["artifacts"]["pdf"]["sha256"]
        result = self._post(f"/api/jobs/{self.job_id}/resume-draft/approve", {
            "confirmed_pdf_review": True,
            "confirmed_fact_review": True,
            "fact_review_sha256": pdf_sha,
            "fact_review_content_sha256": manifest["content_sha256"],
        })
        self.assertTrue(result["ok"])
        approved_path = next(manifest_path.parent.glob("resume-version-approved-*.json"))
        self.assertEqual(
            json.loads(approved_path.read_text(encoding="utf-8"))["fact_approval"]["approved_by"],
            "user",
        )

    def test_local_revision_cannot_clear_cloud_fact_review(self) -> None:
        original_path, original = self._make_unverified_cloud_resume()
        self.assertEqual(original["qa"]["truthfulness_check"], "pending_user_review")
        self.assertTrue(self._get(f"/api/jobs/{self.job_id}/detail")["resume_version"]["fact_review_required"])

        revised = self._post(f"/api/jobs/{self.job_id}/resume-content/revise", {
            "confirmed": True, "instruction": "调整措辞并保留现有经历",
        })
        self.assertTrue(revised["ok"])
        content, manifest_path = load_latest_resume_content(self.output_dir / "applications", self.job_id)
        self.assertNotEqual(manifest_path, original_path)
        self.assertEqual(content["generation"]["engine"], "local_fallback")
        self.assertTrue(content["fact_review_origin"]["required"])
        self.assertIn("cloud_composition", content["fact_review_origin"]["sources"])
        self.assertIn("国家级一等奖", json.dumps(content, ensure_ascii=False))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["qa"]["truthfulness_check"], "pending_user_review")
        version = self._get(f"/api/jobs/{self.job_id}/detail")["resume_version"]
        self.assertTrue(version["fact_review_required"])
        self.assertTrue(any("国家级一等奖" in row["draft"] for row in version["fact_comparisons"]))
        with self.assertRaisesRegex(RuntimeError, "逐项核对"):
            self._post(f"/api/jobs/{self.job_id}/resume-draft/approve", {
                "confirmed_pdf_review": True,
            })

        approved = self._post(f"/api/jobs/{self.job_id}/resume-draft/approve", {
            "confirmed_pdf_review": True,
            "confirmed_fact_review": True,
            "fact_review_sha256": version["pdf_sha256"],
            "fact_review_content_sha256": version["content_sha256"],
        })
        self.assertEqual(approved["workspace"]["resume_status"], "ready")
        approved_path = next(manifest_path.parent.glob("resume-version-approved-*.json"))
        approved_manifest = json.loads(approved_path.read_text(encoding="utf-8"))
        self.assertEqual(approved_manifest["fact_approval"]["pdf_sha256"], version["pdf_sha256"])
        self.assertEqual(approved_manifest["fact_approval"]["content_sha256"], manifest["content_sha256"])

        # A fresh manual edit can omit client-side metadata but must inherit
        # the server-owned review requirement and discard the old approval.
        edited_content = json.loads(json.dumps(content))
        edited_content.pop("fact_review_origin", None)
        edited_content["generation"] = {"engine": "local_fallback"}
        edited_content["summary"] += " 本人补充。"
        saved = self._post(f"/api/jobs/{self.job_id}/resume-content", {
            "confirmed": True, "content": edited_content,
        })
        self.assertEqual(saved["workspace"]["resume_status"], "needs_review")
        self.assertFalse(saved["workspace"]["application_pack_ready"])
        latest = self._get(f"/api/jobs/{self.job_id}/detail")["resume_version"]
        self.assertTrue(latest["fact_review_required"])
        self.assertNotEqual(latest["pdf_sha256"], version["pdf_sha256"])

    def test_preupgrade_local_revision_of_cloud_draft_fails_closed(self) -> None:
        cloud_path, cloud_manifest = self._make_unverified_cloud_resume()
        cloud_content_path = cloud_path.parent / cloud_manifest["content_source"]
        cloud_content = json.loads(cloud_content_path.read_text(encoding="utf-8"))
        cloud_manifest.pop("fact_review_origin", None)
        cloud_manifest.pop("content_sha256", None)
        cloud_content.pop("fact_review_origin", None)
        cloud_path.write_text(json.dumps(cloud_manifest, ensure_ascii=False), encoding="utf-8")
        cloud_content_path.write_text(json.dumps(cloud_content, ensure_ascii=False), encoding="utf-8")

        cloud_content["generation"] = {"engine": "local_fallback", "review_required": True}
        legacy = rerender_edited_resume(
            cloud_content, self.output_dir / "applications",
            based_on=str(cloud_path), edit_origin="agent_dialog",
        )
        legacy_content = json.loads(legacy.content_json.read_text(encoding="utf-8"))
        legacy_content.pop("fact_review_origin", None)
        legacy.content_json.write_text(json.dumps(legacy_content, ensure_ascii=False), encoding="utf-8")
        legacy_manifest = json.loads(legacy.manifest.read_text(encoding="utf-8"))
        legacy_manifest.pop("fact_review_origin", None)
        legacy_manifest.pop("content_sha256", None)
        legacy_manifest["qa"]["truthfulness_check"] = "passed"
        legacy_manifest["qa"]["pdf_visual_review"] = "passed"
        legacy.manifest.write_text(json.dumps(legacy_manifest, ensure_ascii=False), encoding="utf-8")

        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        version = detail["resume_version"]
        self.assertEqual(Path(version["manifest_path"]), legacy.manifest.resolve())
        self.assertTrue(version["fact_review_required"])
        self.assertFalse(version["fact_review_approved"])
        self.assertIn("摘要", version["fact_review_error"])
        dashboard = self._get("/api/dashboard")
        jobs = dashboard.get("jobs_to_apply", []) + dashboard.get("tracked_jobs", [])
        workspace = next(item for item in jobs if item["job_id"] == self.job_id)["workspace"]
        self.assertEqual(workspace["resume_status"], "needs_review")
        self.assertFalse(workspace["application_pack_ready"])
        bundle = resolve_resume_bundle(
            sample_profile(), self.repository.get_job(self.job_id),
            self.output_dir / "applications",
        )
        self.assertNotEqual(bundle.status, "ready")
        with self.assertRaisesRegex(RuntimeError, "摘要"):
            self._post(f"/api/jobs/{self.job_id}/resume-draft/approve", {
                "confirmed_pdf_review": True,
            })

        # Saving this historical version creates a new version with explicit
        # provenance and a content digest that can be reviewed normally.
        restored = self._post(f"/api/jobs/{self.job_id}/resume-content", {
            "confirmed": True, "content": legacy_content,
        })
        self.assertEqual(restored["workspace"]["resume_status"], "needs_review")
        current, current_path = load_latest_resume_content(self.output_dir / "applications", self.job_id)
        self.assertTrue(current["fact_review_origin"]["required"])
        self.assertIn("cloud_composition", current["fact_review_origin"]["sources"])
        self.assertTrue(json.loads(current_path.read_text(encoding="utf-8"))["content_sha256"])

    def test_hash_download_ignores_damaged_other_job_manifest(self) -> None:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        version = self._get(f"/api/jobs/{self.job_id}/detail")["resume_version"]
        damaged = self.output_dir / "applications" / "other-job" / "resume-version-broken.json"
        damaged.parent.mkdir(parents=True)
        damaged.write_text(json.dumps({"target": {"job_id": "damaged"}}), encoding="utf-8")
        with urllib.request.urlopen(
            self.root_url + f"/resume-draft/{self.job_id}/pdf?v={version['pdf_sha256']}",
            timeout=10,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(response.read().startswith(b"%PDF"))

    def test_prepare_apply_first_run_generates_all_and_sets_ready_to_apply(self) -> None:
        res = self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["job_id"], self.job_id)
        self.assertFalse(res["resume_reused"])
        self.assertTrue(res["application_pack_ready"])
        self.assertIsInstance(res.get("greetings"), list)
        self.assertGreater(len(res["greetings"]), 0)

        # Application status must be recorded as ready_to_apply, NOT applied
        app = self.repository.get_application(self.job_id)
        self.assertIsNotNone(app)
        self.assertEqual(app.status, "ready_to_apply")

        # Visual review must NOT be auto-approved in prepare phase: status is needs_review!
        workspace = res["workspace"]
        self.assertEqual(workspace["resume_status"], "needs_review")
        self.assertFalse(workspace["safe_fill_ready"])

    def test_prepare_apply_second_run_reuses_existing_resume(self) -> None:
        first_res = self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        self.assertFalse(first_res["resume_reused"])

        second_res = self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        self.assertTrue(second_res.get("ok"))
        self.assertTrue(second_res["resume_reused"])
        self.assertEqual(second_res["workspace"]["resume_status"], "needs_review")

        app = self.repository.get_application(self.job_id)
        self.assertEqual(app.status, "ready_to_apply")

    @mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page")
    @mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True)
    def test_review_and_open_opens_assets_without_changing_status(
        self, mock_reveal, mock_open_page
    ) -> None:
        # Prepare first
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})

        # Fetch detail to get exact resume_version signature
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        resume_version = detail.get("resume_version")
        self.assertIsNotNone(resume_version)
        self.assertTrue(len(resume_version.get("pdf_sha256", "")) > 0)

        # Review and open with expected sha256
        with mock.patch("os.startfile", create=True) as mock_startfile:
            res = self._post(
                f"/api/jobs/{self.job_id}/review-and-open",
                {
                    "expected_sha256": resume_version["pdf_sha256"],
                    "manifest_path": resume_version["manifest_path"],
                },
            )
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("pdf_opened"))
            self.assertTrue(res.get("page_opened"))
            self.assertTrue(res.get("revealed"))
            self.assertEqual(res.get("sha256"), resume_version["pdf_sha256"])
            self.assertIn("resume-draft", res.get("pdf_url"))
            self.assertTrue(len(res.get("top_greeting", "")) > 0)
            mock_startfile.assert_called_once()
            mock_open_page.assert_called_once()

        # Status must remain ready_to_apply, NEVER automatically marked as applied!
        app = self.repository.get_application(self.job_id)
        self.assertEqual(app.status, "ready_to_apply")

        # Now resume visual review is legitimately approved -> resume becomes ready!
        fresh_detail = self._get("/api/dashboard")
        all_jobs = fresh_detail.get("jobs_to_apply", []) + fresh_detail.get("tracked_jobs", [])
        job_in_list = next(j for j in all_jobs if j["job_id"] == self.job_id)
        self.assertEqual(job_in_list["workspace"]["resume_status"], "ready")
        self.assertTrue(job_in_list["workspace"]["safe_fill_ready"])

    def test_review_and_open_hash_mismatch_fails(self) -> None:
        # Prepare first
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        manifest_path = detail["resume_version"]["manifest_path"]

        # Calling review-and-open with wrong hash must reject with 409 Conflict
        with self.assertRaises(RuntimeError) as ctx:
            self._post(
                f"/api/jobs/{self.job_id}/review-and-open",
                {
                    "expected_sha256": "0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF",
                    "manifest_path": manifest_path,
                },
            )
        self.assertIn("HTTPError 409", str(ctx.exception))

    def test_prepare_apply_concurrent_deduplication(self) -> None:
        # Launch 4 concurrent prepare requests for the same job with simulated generation delay
        from concurrent.futures import ThreadPoolExecutor
        import time
        from job_agent.services.resume_polish import polish_resume_content_locally as real_polish

        call_counts = {"polish": 0}

        def delayed_polish(*args, **kwargs):
            call_counts["polish"] += 1
            time.sleep(0.05)
            return real_polish(*args, **kwargs)

        with mock.patch(
            "job_agent.services.dashboard_routes.jobs_api.polish_resume_content_locally",
            side_effect=delayed_polish,
        ):
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(self._post, f"/api/jobs/{self.job_id}/prepare-apply", {})
                    for _ in range(4)
                ]
                results = [f.result() for f in futures]

        for res in results:
            self.assertTrue(res["ok"])
        reused_flags = [res["resume_reused"] for res in results]
        self.assertEqual(reused_flags.count(False), 1)
        self.assertEqual(reused_flags.count(True), 3)
        # Proven: generation algorithm invoked strictly once across 4 concurrent requests
        self.assertEqual(call_counts["polish"], 1)

    def test_prepare_apply_reports_exact_failed_stage(self) -> None:
        # Simulate failure in resume_draft stage
        with mock.patch(
            "job_agent.services.dashboard_routes.jobs_api.build_portable_resume_draft",
            side_effect=ValueError("磁盘空间不足，无法写入草稿"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
            err_text = str(ctx.exception)
            self.assertIn("HTTPError 400", err_text)
            self.assertIn('"failed_stage": "resume_draft"', err_text)
            self.assertIn("专属简历草稿生成", err_text)

    def test_review_and_open_version_id_mismatch_fails(self) -> None:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]

        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": ver["pdf_sha256"],
                "expected_version_id": "non-existent-version-id-999",
                "manifest_path": ver["manifest_path"],
            })
        self.assertIn("HTTPError 409", str(ctx.exception))
        self.assertIn("简历版本编号不匹配", str(ctx.exception))

    @mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page", side_effect=ValueError("系统浏览器启动失败"))
    @mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True)
    def test_review_and_open_when_open_page_fails(self, mock_reveal, mock_open_page) -> None:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]
        with mock.patch("os.startfile", create=True):
            res = self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": ver["pdf_sha256"],
                "manifest_path": ver["manifest_path"],
            })
        self.assertTrue(res.get("ok"))
        self.assertFalse(res.get("page_opened"))
        self.assertTrue(res.get("revealed"))

    @mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page")
    @mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=False)
    def test_review_and_open_when_reveal_fails(self, mock_reveal, mock_open_page) -> None:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]
        with mock.patch("os.startfile", create=True):
            res = self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": ver["pdf_sha256"],
                "manifest_path": ver["manifest_path"],
            })
        self.assertTrue(res.get("ok"))
        self.assertTrue(res.get("page_opened"))
        self.assertFalse(res.get("revealed"))

    def test_switch_job_and_regenerate(self) -> None:
        # Create second job
        job2 = self.repository.upsert_job(
            JobRecordInput(
                company="测试科技二部",
                title="算法工程实习生",
                jd_text=JD,
                source="测试招聘",
                source_url="https://example.com/jobs/algo-intern",
                location="上海",
            )
        )
        self.repository.add_match_result(
            job2.job_id,
            scored_result(company="测试科技二部", title="算法工程实习生", score=88),
        )

        # Prepare job 1 and job 2
        res_job1 = self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        res_job2 = self._post(f"/api/jobs/{job2.job_id}/prepare-apply", {})
        self.assertTrue(res_job1["ok"])
        self.assertTrue(res_job2["ok"])

        detail1 = self._get(f"/api/jobs/{self.job_id}/detail")
        detail2 = self._get(f"/api/jobs/{job2.job_id}/detail")
        hash1 = detail1["resume_version"]["pdf_sha256"]
        hash2 = detail2["resume_version"]["pdf_sha256"]
        self.assertTrue(hash1)
        self.assertTrue(hash2)

        # Cross hash check: job 1 review with job 2 hash must fail with 409
        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": hash2,
                "manifest_path": detail1["resume_version"]["manifest_path"],
            })
        self.assertIn("HTTPError 409", str(ctx.exception))

    def test_record_applied_explicitly_marks_applied(self) -> None:
        # Prepare first
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})

        # Explicitly record status as applied
        res = self._post(
            f"/api/applications/{self.job_id}/status",
            {"status": "applied", "confirmed": True, "detail": "用户手动登记已投递"},
        )
        self.assertTrue(res.get("ok"))

        # Status is now applied
        app = self.repository.get_application(self.job_id)
        self.assertEqual(app.status, "applied")

    def test_prepare_apply_commute_unknown_not_auto_cleared(self) -> None:
        # Job has no commute_minutes set -> commute_fit must be unknown and include warning
        res = self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        self.assertEqual(res.get("commute_fit"), "unknown")
        self.assertIsNotNone(res.get("commute_warning"))
        self.assertIn("通勤仍需核实", res["commute_warning"])

    def test_review_and_open_fails_when_no_resume_draft(self) -> None:
        # Create a fresh job without preparing
        new_job = self.repository.upsert_job(
            JobRecordInput(
                company="另一公司",
                title="后端实习生",
                jd_text=JD,
                source="测试招聘",
                source_url="https://example.com/jobs/backend-intern",
                location="上海",
            )
        )
        self.repository.add_match_result(
            new_job.job_id,
            scored_result(company="另一公司", title="后端实习生", score=80),
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{new_job.job_id}/review-and-open", {
                "expected_sha256": "A" * 64,
                "manifest_path": str(self.output_dir / "applications" / "nonexistent.json"),
            })
        self.assertIn("HTTPError 400", str(ctx.exception))

    def test_review_and_open_empty_or_invalid_hash_rejected(self) -> None:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        manifest_path = detail["resume_version"]["manifest_path"]

        # 1. Empty payload rejected with 400
        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{self.job_id}/review-and-open", {})
        self.assertIn("HTTPError 400", str(ctx.exception))

        # 2. Empty string hash rejected with 400
        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": "",
                "manifest_path": manifest_path,
            })
        self.assertIn("HTTPError 400", str(ctx.exception))

        # 3. Short / malformed hash rejected with 400
        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": "12345",
                "manifest_path": manifest_path,
            })
        self.assertIn("HTTPError 400", str(ctx.exception))

        # 4. Non-hex hash rejected with 400
        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": "Z" * 64,
                "manifest_path": manifest_path,
            })
        self.assertIn("HTTPError 400", str(ctx.exception))

        # Manifest must NOT have been approved
        manifest_data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest_data["qa"]["pdf_visual_review"], "pending_user_review")

    def test_review_and_open_missing_or_invalid_manifest_rejected(self) -> None:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        valid_hash = detail["resume_version"]["pdf_sha256"]

        # 1. Missing manifest_path rejected with 400
        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": valid_hash,
            })
        self.assertIn("HTTPError 400", str(ctx.exception))

        # 2. Non-existent manifest_path rejected with 400
        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": valid_hash,
                "manifest_path": str(self.output_dir / "applications" / "not_found.json"),
            })
        self.assertIn("HTTPError 400", str(ctx.exception))

        # 3. Manifest outside applications dir rejected with 400
        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": valid_hash,
                "manifest_path": str(self.root / "jobs.sqlite3"),
            })
        self.assertIn("HTTPError 400", str(ctx.exception))

    def test_review_and_open_cross_job_manifest_rejected(self) -> None:
        # Create Job 2
        job2 = self.repository.upsert_job(
            JobRecordInput(
                company="另一公司",
                title="算法工程师",
                jd_text=JD,
                source="测试招聘",
                source_url="https://example.com/jobs/algo",
                location="上海",
            )
        )
        self.repository.add_match_result(
            job2.job_id,
            scored_result(company="另一公司", title="算法工程师", score=90),
        )
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        self._post(f"/api/jobs/{job2.job_id}/prepare-apply", {})

        detail1 = self._get(f"/api/jobs/{self.job_id}/detail")
        detail2 = self._get(f"/api/jobs/{job2.job_id}/detail")
        hash1 = detail1["resume_version"]["pdf_sha256"]
        manifest2 = detail2["resume_version"]["manifest_path"]

        # Attempt to confirm Job 1 using Job 1's valid hash but Job 2's manifest
        with self.assertRaises(RuntimeError) as ctx:
            self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": hash1,
                "manifest_path": manifest2,
            })
        self.assertIn("HTTPError 400", str(ctx.exception))

        # Both manifests must remain pending review
        m1 = json.loads(Path(detail1["resume_version"]["manifest_path"]).read_text(encoding="utf-8"))
        m2 = json.loads(Path(manifest2).read_text(encoding="utf-8"))
        self.assertEqual(m1["qa"]["pdf_visual_review"], "pending_user_review")
        self.assertEqual(m2["qa"]["pdf_visual_review"], "pending_user_review")

    @mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page")
    def test_review_and_open_approval_failure_stops_execution(self, mock_open_page) -> None:
        from job_agent.services.tailored_resume import TailoredResumeError
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]

        with mock.patch(
            "job_agent.services.dashboard_routes.jobs_api.approve_resume_visual_review",
            side_effect=TailoredResumeError("权限不足或磁盘锁定，无法写入批准记录"),
        ):
            with mock.patch("os.startfile", create=True) as mock_startfile:
                with self.assertRaises(RuntimeError) as ctx:
                    self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                        "expected_sha256": ver["pdf_sha256"],
                        "manifest_path": ver["manifest_path"],
                    })
                self.assertIn("HTTPError 400", str(ctx.exception))
                mock_startfile.assert_not_called()
                mock_open_page.assert_not_called()

    @mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page")
    def test_review_and_open_pack_sync_failure_stops_execution(self, mock_open_page) -> None:
        from job_agent.services.application_pack import ApplicationPackError
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]

        with mock.patch(
            "job_agent.services.dashboard_routes.jobs_api.write_application_pack",
            side_effect=ApplicationPackError("材料包文件无法同步写入"),
        ):
            with mock.patch("os.startfile", create=True) as mock_startfile:
                with self.assertRaises(RuntimeError) as ctx:
                    self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                        "expected_sha256": ver["pdf_sha256"],
                        "manifest_path": ver["manifest_path"],
                    })
                self.assertIn("HTTPError 400", str(ctx.exception))
                mock_startfile.assert_not_called()
                mock_open_page.assert_not_called()

    def test_reveal_resume_does_not_approve_resume(self) -> None:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        manifest_path = Path(detail["resume_version"]["manifest_path"])

        with mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True) as mock_reveal:
            res = self._post(f"/api/jobs/{self.job_id}/reveal-resume", {})
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("revealed"))
            mock_reveal.assert_called_once()

        # Crucial check: revealing MUST NOT approve the resume!
        mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(mdata["qa"]["pdf_visual_review"], "pending_user_review")
        approved_files = list(manifest_path.parent.glob("resume-version-approved-*.json"))
        self.assertEqual(len(approved_files), 0, "定位简历文件绝不能生成批准记录")

    def test_resume_draft_download_binds_version_hash(self) -> None:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        valid_hash = detail["resume_version"]["pdf_sha256"]
        fake_hash = "0" * 64

        # 1. Download with correct hash succeeds and matches hash
        req_ok = urllib.request.Request(
            f"{self.root_url}/resume-draft/{self.job_id}/pdf?v={valid_hash}",
            headers={"X-Job-Agent-Token": self.token},
            method="GET",
        )
        with urllib.request.urlopen(req_ok, timeout=5) as response:
            self.assertEqual(response.status, 200)
            body = response.read()
            import hashlib
            self.assertEqual(hashlib.sha256(body).hexdigest().upper(), valid_hash)

        # 2. Download with non-existent version hash must be rejected (404 Not Found), NOT return latest
        req_fake = urllib.request.Request(
            f"{self.root_url}/resume-draft/{self.job_id}/pdf?v={fake_hash}",
            headers={"X-Job-Agent-Token": self.token},
            method="GET",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_fake, timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_review_and_open_retry_after_pack_failure_does_not_duplicate_approval(self) -> None:
        from job_agent.services.application_pack import ApplicationPackError
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]
        manifest_dir = Path(ver["manifest_path"]).parent

        # Attempt 1: mock write_application_pack to fail
        with mock.patch(
            "job_agent.services.dashboard_routes.jobs_api.write_application_pack",
            side_effect=ApplicationPackError("第一次尝试：材料包同步异常"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                    "expected_sha256": ver["pdf_sha256"],
                    "manifest_path": ver["manifest_path"],
                })
            err_msg = str(ctx.exception)
            self.assertIn("HTTPError 400", err_msg)
            self.assertIn('"failed_stage": "application_pack_sync"', err_msg)
            self.assertIn('"visual_review_approved": true', err_msg)

        # Check approved records after attempt 1: exactly 1 approved manifest created
        approved_files_1 = list(manifest_dir.glob("resume-version-approved-*.json"))
        self.assertEqual(len(approved_files_1), 1, "初次审批应成功产生 1 份批准记录")

        # Attempt 2 (Retry): without error, write_application_pack succeeds
        with mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page"):
            with mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True):
                with mock.patch("os.startfile", create=True):
                    res2 = self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                        "expected_sha256": ver["pdf_sha256"],
                        "manifest_path": ver["manifest_path"],
                    })
        self.assertTrue(res2.get("ok"))
        self.assertTrue(res2.get("resumed_from_partial"), "重试时应复用已有批准记录并标识 resumed_from_partial")

        # Assert ONLY 1 approved manifest file exists on disk (NO duplicate generated upon retry!)
        approved_files_2 = list(manifest_dir.glob("resume-version-approved-*.json"))
        self.assertEqual(len(approved_files_2), 1, "重试时必须避免产生重复的批准记录")

        # And application status is now ready_to_apply with ready resume
        fresh_dash = self._get("/api/dashboard")
        all_jobs = fresh_dash.get("jobs_to_apply", []) + fresh_dash.get("tracked_jobs", [])
        j = next(item for item in all_jobs if item["job_id"] == self.job_id)
        self.assertEqual(j["workspace"]["resume_status"], "ready")

    def test_review_and_open_retry_after_oserror_pack_failure_does_not_duplicate_approval(self) -> None:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]
        manifest_dir = Path(ver["manifest_path"]).parent

        # Attempt 1: mock write_application_pack raising PermissionError/OSError (真实写盘受限)
        with mock.patch(
            "job_agent.services.dashboard_routes.jobs_api.write_application_pack",
            side_effect=PermissionError("权限不足或文件被占用"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                    "expected_sha256": ver["pdf_sha256"],
                    "manifest_path": ver["manifest_path"],
                })
            err_msg = str(ctx.exception)
            self.assertIn("HTTPError 400", err_msg)
            self.assertIn('"failed_stage": "application_pack_sync"', err_msg)
            self.assertIn('"visual_review_approved": true', err_msg)
            self.assertIn('"retryable": false', err_msg)
            self.assertIn("解除文件或目录占用", err_msg)

        # 校验初次失败后：审批已经安全落盘，严格产生 1 份批准清单
        approved_files_1 = list(manifest_dir.glob("resume-version-approved-*.json"))
        self.assertEqual(len(approved_files_1), 1, "材料包写入虽失败，但视觉审阅已落盘应且仅有 1 份")

        # Attempt 2 (Retry): 恢复写入条件后重试
        with mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page"):
            with mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True):
                with mock.patch("os.startfile", create=True):
                    res2 = self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                        "expected_sha256": ver["pdf_sha256"],
                        "manifest_path": ver["manifest_path"],
                    })
        self.assertTrue(res2.get("ok"))
        self.assertTrue(res2.get("resumed_from_partial"), "重试时应识别已批准并复用")

        # 校验重试后磁盘上依然只有 1 份批准记录
        approved_files_2 = list(manifest_dir.glob("resume-version-approved-*.json"))
        self.assertEqual(len(approved_files_2), 1, "恢复写入重试后，批准记录仍必须严格只有 1 份")

    def test_review_and_open_pre_approval_oserror_does_not_claim_approved(self) -> None:
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]
        manifest_dir = Path(ver["manifest_path"]).parent

        # 模拟审批前清单文件读取异常 (OSError)
        with mock.patch.object(Path, "read_text", side_effect=OSError("模拟磁盘扇区读取损坏")):
            with self.assertRaises(RuntimeError) as ctx:
                self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                    "expected_sha256": ver["pdf_sha256"],
                    "manifest_path": ver["manifest_path"],
                })
            err_msg = str(ctx.exception)
            self.assertIn("HTTPError 400", err_msg)
            # 审批前失败，绝不能误报为已批准或 application_pack_sync
            self.assertNotIn('"failed_stage": "application_pack_sync"', err_msg)
            self.assertNotIn('"visual_review_approved": true', err_msg)

        # 磁盘上绝不应产生批准清单
        approved_files = list(manifest_dir.glob("resume-version-approved-*.json"))
        self.assertEqual(len(approved_files), 0, "审批前失败绝不能产生批准记录")

    def test_requesting_old_version_preview_never_returns_new_version_pdf(self) -> None:
        import hashlib
        # 1. 准备第 1 版材料，获取其哈希与内容
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail1 = self._get(f"/api/jobs/{self.job_id}/detail")
        hash_v1 = detail1["resume_version"]["pdf_sha256"]

        req_v1 = urllib.request.Request(
            f"{self.root_url}/resume-draft/{self.job_id}/pdf?v={hash_v1}",
            headers={"X-Job-Agent-Token": self.token},
            method="GET",
        )
        with urllib.request.urlopen(req_v1, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            content_v1 = resp.read()
            self.assertEqual(hashlib.sha256(content_v1).hexdigest().upper(), hash_v1)

        # 2. 模拟修改经历并生成第 2 版材料草稿（产生不同的 PDF 哈希）
        from job_agent.services.profile_store import load_profile
        prof = load_profile(self.profile_path)
        prof.person.display_name = "测试候选人二代"
        save_profile(prof, self.profile_path, overwrite=True)

        res_v2 = self._post(f"/api/jobs/{self.job_id}/resume-draft", {"confirmed": True})
        self.assertTrue(res_v2.get("ok"))
        detail2 = self._get(f"/api/jobs/{self.job_id}/detail")
        hash_v2 = detail2["resume_version"]["pdf_sha256"]
        self.assertNotEqual(hash_v1, hash_v2, "重新生成后两个版本的哈希必须不同")

        # 3. 验证使用新版本哈希请求，能正常得到新版本内容
        req_v2 = urllib.request.Request(
            f"{self.root_url}/resume-draft/{self.job_id}/pdf?v={hash_v2}",
            headers={"X-Job-Agent-Token": self.token},
            method="GET",
        )
        with urllib.request.urlopen(req_v2, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            content_v2 = resp.read()
            self.assertEqual(hashlib.sha256(content_v2).hexdigest().upper(), hash_v2)

        # 4. 关键验证：使用旧版本哈希请求预览，绝对不能得到新版本 PDF！
        req_old = urllib.request.Request(
            f"{self.root_url}/resume-draft/{self.job_id}/pdf?v={hash_v1}",
            headers={"X-Job-Agent-Token": self.token},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req_old, timeout=5) as resp:
                # 若旧版本文件在独立目录保留，返回的内容哈希必须严格是旧版，绝不能是新版
                old_body = resp.read()
                self.assertEqual(hashlib.sha256(old_body).hexdigest().upper(), hash_v1)
                self.assertNotEqual(old_body, content_v2, "请求旧版本绝不能返回新版本 PDF 内容")
        except urllib.error.HTTPError as exc:
            # 若旧版本已被覆盖不可用，服务端必须以 404 明确拒绝，绝不能静默返回新版本！
            self.assertEqual(exc.code, 404)

        # 5. 请求完全不存在或过期的无效哈希，必须以 404 明确拒绝，绝不能 fallback 返回最新版本
        req_unknown = urllib.request.Request(
            f"{self.root_url}/resume-draft/{self.job_id}/pdf?v={'F' * 64}",
            headers={"X-Job-Agent-Token": self.token},
            method="GET",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_unknown, timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_approve_new_version_after_old_version_already_approved(self) -> None:
        """验证状态转移缺陷已修复：版本 A 已批准后，重新生成版本 B，版本 B 能顺利通过审批并完成材料包同步。"""
        from job_agent.services.profile_store import load_profile

        # 1. 准备并批准版本 A
        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail_a = self._get(f"/api/jobs/{self.job_id}/detail")
        ver_a = detail_a["resume_version"]
        manifest_dir = Path(ver_a["manifest_path"]).parent

        with mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page"):
            with mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True):
                with mock.patch("os.startfile", create=True):
                    res_a = self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                        "expected_sha256": ver_a["pdf_sha256"],
                        "manifest_path": ver_a["manifest_path"],
                    })
        self.assertTrue(res_a.get("ok"))
        approved_files_a = list(manifest_dir.glob("resume-version-approved-*.json"))
        self.assertEqual(len(approved_files_a), 1, "版本 A 审批后必须有 1 份批准记录")

        # 2. 变更经历并重新生成版本 B（产生新的 PDF 哈希与草稿清单）
        prof = load_profile(self.profile_path)
        prof.person.display_name = "全新版本候选人"
        save_profile(prof, self.profile_path, overwrite=True)

        res_v2 = self._post(f"/api/jobs/{self.job_id}/resume-draft", {"confirmed": True})
        self.assertTrue(res_v2.get("ok"))
        detail_b = self._get(f"/api/jobs/{self.job_id}/detail")
        ver_b = detail_b["resume_version"]
        self.assertNotEqual(ver_a["pdf_sha256"], ver_b["pdf_sha256"], "重新生成后必须产生不同的 PDF 哈希")

        # 3. 关键验证：版本 B 必须能顺利批准，绝不能因版本 A 曾经 ready 而报错 409
        with mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page"):
            with mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True):
                with mock.patch("os.startfile", create=True):
                    res_b = self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                        "expected_sha256": ver_b["pdf_sha256"],
                        "manifest_path": ver_b["manifest_path"],
                    })
        self.assertTrue(res_b.get("ok"), "版本 B 应成功批准并通过")
        self.assertEqual(res_b.get("sha256"), ver_b["pdf_sha256"])

        # 4. 验证版本 B 审批后详情绑定版本 B 的哈希，且仪表盘反映材料包已就绪
        detail_b_final = self._get(f"/api/jobs/{self.job_id}/detail")
        self.assertEqual(detail_b_final["resume_version"]["pdf_sha256"], ver_b["pdf_sha256"])

        fresh_dash = self._get("/api/dashboard")
        all_jobs = fresh_dash.get("jobs_to_apply", []) + fresh_dash.get("tracked_jobs", [])
        j = next(item for item in all_jobs if item["job_id"] == self.job_id)
        self.assertEqual(j["workspace"]["resume_status"], "ready")
        self.assertTrue(j["workspace"]["application_pack_ready"])

        from job_agent.services.browser_assist import find_application_pack
        _, pack = find_application_pack(self.output_dir / "application-packs", self.job_id)
        self.assertEqual(pack.resume.status, "ready")
        self.assertTrue(pack.resume.qa_verified)
        self.assertEqual(Path(pack.resume.pdf_path).resolve(), Path(ver_b["pdf_path"]).resolve())

    def test_concurrent_review_and_open_produces_single_approval(self) -> None:
        """验证并发安全与严格幂等：并发多次 review-and-open 请求只生成一份批准记录。"""
        import concurrent.futures

        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]
        manifest_dir = Path(ver["manifest_path"]).parent

        def _do_review() -> dict:
            return self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                "expected_sha256": ver["pdf_sha256"],
                "manifest_path": ver["manifest_path"],
            })

        with mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page"):
            with mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True):
                with mock.patch("os.startfile", create=True):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                        futures = [executor.submit(_do_review) for _ in range(5)]
                        results = [f.result() for f in futures]

        for res in results:
            self.assertTrue(res.get("ok"), f"并发请求返回值应为 ok: {res}")

        # 核心断言：磁盘上严格且仅有 1 份批准记录
        approved_files = list(manifest_dir.glob("resume-version-approved-*.json"))
        self.assertEqual(len(approved_files), 1, "并发确认必须保证绝对幂等，磁盘仅生成 1 份批准记录")

    def test_review_blocks_concurrent_prepare_coordination(self) -> None:
        """验证岗位操作协调锁：审批期间并发的 prepare-apply 必须排队等待，杜绝竞态。"""
        import time
        from job_agent.services.tailored_resume import approve_resume_visual_review

        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]

        review_entered = threading.Event()
        review_proceed = threading.Event()
        events = []

        orig_approve = approve_resume_visual_review

        def slow_approve(manifest_file):
            events.append("review_started")
            review_entered.set()
            if not review_proceed.wait(timeout=5):
                raise TimeoutError("review_proceed timeout")
            res = orig_approve(manifest_file)
            events.append("review_finished")
            return res

        review_res = {}
        prepare_res = {}

        def do_review():
            with mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page"), \
                 mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True), \
                 mock.patch("job_agent.services.dashboard_routes.jobs_api.approve_resume_visual_review", side_effect=slow_approve), \
                 mock.patch("os.startfile", create=True):
                review_res["data"] = self._post(f"/api/jobs/{self.job_id}/review-and-open", {
                    "expected_sha256": ver["pdf_sha256"],
                    "manifest_path": ver["manifest_path"],
                })

        def do_prepare():
            events.append("prepare_invoked")
            prepare_res["data"] = self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
            events.append("prepare_finished")

        t_review = threading.Thread(target=do_review)
        t_prepare = threading.Thread(target=do_prepare)

        t_review.start()
        self.assertTrue(review_entered.wait(timeout=5), "审阅应进入锁内并触发 slow_approve")

        t_prepare.start()
        time.sleep(0.1)

        self.assertIn("review_started", events)
        self.assertIn("prepare_invoked", events)
        self.assertNotIn("prepare_finished", events)

        review_proceed.set()

        t_review.join(timeout=5)
        t_prepare.join(timeout=5)

        self.assertTrue(review_res.get("data", {}).get("ok"))
        self.assertTrue(prepare_res.get("data", {}).get("ok"))
        self.assertEqual(
            events,
            ["review_started", "prepare_invoked", "review_finished", "prepare_finished"],
            "操作执行时序必须严格互斥排队，避免生成与审批竞态交错",
        )

    def test_prepare_blocks_concurrent_review_coordination(self) -> None:
        """验证岗位操作协调锁反向互斥：材料准备期间并发的 review-and-open 必须排队等待。"""
        import time
        from job_agent.services.dashboard_routes.jobs_api import _ensure_match

        self._post(f"/api/jobs/{self.job_id}/prepare-apply", {})
        detail = self._get(f"/api/jobs/{self.job_id}/detail")
        ver = detail["resume_version"]

        prepare_entered = threading.Event()
        prepare_proceed = threading.Event()
        events = []

        orig_ensure_match = _ensure_match

        def slow_ensure_match(repo, prof, j):
            if not prepare_entered.is_set():
                events.append("prepare_started")
                prepare_entered.set()
                if not prepare_proceed.wait(timeout=5):
                    raise TimeoutError("prepare_proceed timeout")
            res = orig_ensure_match(repo, prof, j)
            if "prepare_started" in events and "prepare_stage_done" not in events:
                events.append("prepare_stage_done")
            return res

        prepare_res = {}
        review_res = {}
        failures = []

        def do_prepare():
            with mock.patch("job_agent.services.dashboard_routes.jobs_api._ensure_match", side_effect=slow_ensure_match):
                prepare_res["data"] = self._post(f"/api/jobs/{self.job_id}/prepare-apply", {"force_regenerate": True})
                events.append("prepare_finished")

        def do_review():
            events.append("review_invoked")
            with mock.patch("job_agent.services.dashboard_routes.jobs_api._open_job_page"), \
                 mock.patch("job_agent.services.dashboard_routes.jobs_api._reveal_resume_in_explorer", return_value=True), \
                 mock.patch("os.startfile", create=True):
                request = urllib.request.Request(
                    f"{self.root_url}/api/jobs/{self.job_id}/review-and-open",
                    data=json.dumps({"expected_sha256": ver["pdf_sha256"],
                                     "manifest_path": ver["manifest_path"]}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": self.root_url,
                             "X-Job-Agent-Token": self.token}, method="POST",
                )
                try:
                    response = urllib.request.urlopen(request, timeout=10)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    review_res["status"] = response.status
                    review_res["data"] = json.loads(response.read().decode("utf-8"))
            events.append("review_finished")

        def capture_failure(action):
            try:
                action()
            except Exception as exc:
                failures.append(exc)

        t_prepare = threading.Thread(target=capture_failure, args=(do_prepare,))
        t_review = threading.Thread(target=capture_failure, args=(do_review,))

        t_prepare.start()
        try:
            self.assertTrue(prepare_entered.wait(timeout=5), "准备流程应进入锁内")
            t_review.start()
            time.sleep(0.1)
            self.assertIn("prepare_started", events)
            self.assertIn("review_invoked", events)
            self.assertNotIn("review_finished", events)
        finally:
            prepare_proceed.set()
            t_prepare.join(timeout=10)
            if t_review.ident is not None:
                t_review.join(timeout=10)

        self.assertFalse(t_prepare.is_alive(), "准备请求必须结束")
        self.assertFalse(t_review.is_alive(), "审阅请求必须结束")
        if failures:
            raise failures[0]
        # A queued review carries the old manifest. Regeneration must invalidate it.
        self.assertEqual(review_res.get("status"), 409)
        self.assertIn("已有更新的简历草稿", review_res["data"]["error"])
        self.assertIn("review_finished", events)
        self.assertEqual(list(Path(ver["manifest_path"]).parent.glob("resume-version-approved-*.json")), [])
        self.assertTrue(prepare_res.get("data", {}).get("ok"))
        self.assertEqual(
            events[:3],
            ["prepare_started", "review_invoked", "prepare_stage_done"],
            "审阅请求必须在准备操作释放锁之后方可继续执行",
        )




