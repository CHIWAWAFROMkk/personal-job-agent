from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from job_agent.models.application import (
    ApplicationJobSnapshot,
    ApplicationMaterials,
    ApplicationPack,
    ResumeBundle,
)
from job_agent.models.browser_assist import FormControlDescriptor
from job_agent.models.job_record import JobDetail, JobSourceItem
from job_agent.services.browser_assist import (
    BrowserAssistError,
    build_shixiseng_availability_answers,
    classify_form_control,
    create_application_session,
    detect_page_hard_stop,
    load_application_session,
    playwright_environment_status,
    run_browser_session,
    verify_application_page,
)
from tests.helpers import sample_profile


def _materials() -> ApplicationMaterials:
    return ApplicationMaterials(
        boss_greeting="您好",
        email_subject="申请",
        email_body="您好",
        self_introduction_30s="自我介绍",
        why_company="公司原因",
        why_role="岗位原因",
        personal_strengths=["真实优势"],
        interview_self_introduction_60s="面试介绍",
        availability_answer="每周四天",
    )


class BrowserAssistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.resume = self.root / "定向简历.pdf"
        self.resume.write_bytes(b"safe-resume-test")
        digest = hashlib.sha256(self.resume.read_bytes()).hexdigest().upper()
        self.manifest = self.root / "resume-version.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "artifacts": {
                        "pdf": {"path": self.resume.name, "sha256": digest}
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.pack_path = self.root / "application-pack.json"
        self.pack = ApplicationPack(
            job=ApplicationJobSnapshot(
                job_id=2,
                company="示例公司",
                title="产品运营实习生",
                source_url="https://jobs.example.com/apply/2",
                match_score=82,
                recommendation="recommend",
            ),
            materials=_materials(),
            resume=ResumeBundle(
                status="ready",
                manifest_path=str(self.manifest),
                pdf_path=str(self.resume),
                qa_verified=True,
                note="已质检",
            ),
        )
        self.pack_path.write_text(self.pack.model_dump_json(indent=2), encoding="utf-8")
        self.job = JobDetail(
            job_id=2,
            company="示例公司",
            title="产品运营实习生",
            jd_text="负责产品运营。",
            status="discovered",
            sources=[
                JobSourceItem(
                    platform="牛客",
                    source_url="https://jobs.example.com/apply/2",
                    first_seen_at="2026-08-18T00:00:00Z",
                    last_seen_at="2026-08-18T00:00:00Z",
                )
            ],
            created_at="2026-08-18T00:00:00Z",
            updated_at="2026-08-18T00:00:00Z",
            first_seen_at="2026-08-18T00:00:00Z",
            last_seen_at="2026-08-18T00:00:00Z",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_control_policy_separates_safe_manual_and_submit(self) -> None:
        email = classify_form_control(
            FormControlDescriptor(index=0, tag="input", input_type="email")
        )
        salary = classify_form_control(
            FormControlDescriptor(index=1, tag="input", label="期望薪资")
        )
        submit = classify_form_control(
            FormControlDescriptor(index=2, tag="button", input_type="submit", label="提交申请")
        )
        unknown = classify_form_control(
            FormControlDescriptor(index=3, tag="input", label="内部推荐码")
        )
        captcha = classify_form_control(
            FormControlDescriptor(index=4, tag="input", label="验证码")
        )
        custom_apply = classify_form_control(
            FormControlDescriptor(
                index=5,
                tag="div",
                class_name="resume_apply com_res",
                label="投个简历",
                action_like=True,
            )
        )

        self.assertEqual((email.category, email.field_key), ("safe_objective", "email"))
        self.assertEqual(salary.category, "manual")
        self.assertEqual(submit.category, "blocked")
        self.assertEqual(unknown.category, "ignored")
        self.assertEqual(captcha.category, "manual")
        self.assertEqual(custom_apply.category, "blocked")
        self.assertIn("投递", custom_apply.reason)

        self.assertIn(
            "登录密码框",
            detect_page_hard_stop(
                [FormControlDescriptor(index=0, tag="input", input_type="password")],
                "登录",
            )
            or "",
        )
        self.assertIn(
            "测评",
            detect_page_hard_stop([], "候选人在线测评中心") or "",
        )
        self.assertIsNone(
            detect_page_hard_stop(
                [
                    FormControlDescriptor(
                        index=0,
                        tag="input",
                        input_type="password",
                        visible=False,
                    )
                ],
                "已登录岗位页",
            )
        )

    def test_shixiseng_answers_are_derived_from_confirmed_profile(self) -> None:
        profile = sample_profile(days_per_week=5)
        profile.job_search.availability.earliest_start = "2026-08-18"
        profile.job_search.availability.duration_months = 3

        answers = build_shixiseng_availability_answers(
            profile,
            today=date(2026, 8, 22),
        )

        self.assertEqual(
            answers,
            {"arrival": "1周内", "duration": "3-6个月", "days": "5天"},
        )

    def test_session_is_private_auditable_and_hash_guarded(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "application_form.html"
        session, session_path = create_application_session(
            sample_profile(),
            self.job,
            self.pack,
            self.pack_path,
            sessions_dir=self.root / "sessions",
            target_url=fixture.resolve().as_uri(),
            local_demo=True,
        )

        self.assertTrue(session_path.is_file())
        self.assertEqual(session.allowed_domains, ["local-fixture"])
        self.assertTrue(any(item.key == "resume" and item.status == "ready" for item in session.fields))
        self.assertTrue(any(stop.rule == "no_submit" for stop in session.hard_stops))
        self.assertEqual(load_application_session(session_path).session_id, session.session_id)

        self.resume.write_bytes(b"changed-after-planning")
        with self.assertRaisesRegex(BrowserAssistError, "简历文件已变化"):
            load_application_session(session_path)

    def test_local_browser_demo_never_submits(self) -> None:
        package_ok, browser_ok, _ = playwright_environment_status()
        if not package_ok or not browser_ok:
            self.skipTest("Playwright Chromium is not installed in this test runtime")
        fixture = Path(__file__).parent / "fixtures" / "application_form.html"
        session, session_path = create_application_session(
            sample_profile(),
            self.job,
            self.pack,
            self.pack_path,
            sessions_dir=self.root / "sessions",
            target_url=fixture.resolve().as_uri(),
            local_demo=True,
        )

        report, report_path = run_browser_session(
            session_path,
            browser_profile_dir=self.root / "browser-profile",
            headless=True,
            keep_open=False,
        )

        self.assertTrue(report_path.is_file())
        self.assertEqual(report.status, "completed")
        self.assertFalse(report.submit_attempted)
        self.assertEqual(report.observed_demo_submit_count, 0)
        self.assertEqual(report.observed_demo_entry_click_count, 0)
        self.assertGreaterEqual(report.submit_controls_detected, 2)
        self.assertGreaterEqual(report.manual_fields_detected, 4)
        self.assertIn("resume", report.filled_fields)
        self.assertTrue(report.uploaded_resume)
        self.assertTrue(Path(report.screenshot_path or "").is_file())
        report_text = report_path.read_text(encoding="utf-8")
        self.assertNotIn("private@example.com", report_text)
        self.assertNotIn("123456", report_text)

    def test_framework_and_accessibility_labels_are_detected_safely(self) -> None:
        package_ok, browser_ok, _ = playwright_environment_status()
        if not package_ok or not browser_ok:
            self.skipTest("Playwright Chromium is not installed in this test runtime")
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "application_form_framework_labels.html"
        )
        _, session_path = create_application_session(
            sample_profile(),
            self.job,
            self.pack,
            self.pack_path,
            sessions_dir=self.root / "framework-label-sessions",
            target_url=fixture.resolve().as_uri(),
            local_demo=True,
        )

        report, _ = run_browser_session(
            session_path,
            browser_profile_dir=self.root / "framework-label-browser-profile",
            headless=True,
            keep_open=False,
        )

        self.assertEqual(report.status, "completed")
        self.assertFalse(report.submit_attempted)
        self.assertEqual(report.observed_demo_submit_count, 0)
        self.assertEqual(
            {"full_name", "email", "phone", "school", "major", "graduation_date"},
            set(report.filled_fields),
        )
        self.assertGreaterEqual(report.manual_fields_detected, 1)
        self.assertGreaterEqual(report.submit_controls_detected, 1)

    def test_confirmed_entry_clicks_once_but_never_final_submit(self) -> None:
        package_ok, browser_ok, _ = playwright_environment_status()
        if not package_ok or not browser_ok:
            self.skipTest("Playwright Chromium is not installed in this test runtime")
        fixture = Path(__file__).parent / "fixtures" / "application_form.html"
        _, session_path = create_application_session(
            sample_profile(),
            self.job,
            self.pack,
            self.pack_path,
            sessions_dir=self.root / "confirmed-sessions",
            target_url=fixture.resolve().as_uri(),
            local_demo=True,
        )

        report, _ = run_browser_session(
            session_path,
            browser_profile_dir=self.root / "confirmed-browser-profile",
            headless=True,
            keep_open=False,
            confirmed_entry_text="投个简历",
            confirmation_token="SUBMIT_JOB_2",
            confirmed_availability_answers={
                "arrival": "1周内",
                "duration": "3-6个月",
                "days": "5天",
            },
        )

        self.assertTrue(report.submit_attempted)
        self.assertEqual(report.submission_outcome, "form_opened")
        self.assertEqual(report.observed_demo_entry_click_count, 1)
        self.assertEqual(report.observed_demo_submit_count, 0)
        self.assertTrue(report.uploaded_resume)
        self.assertTrue(report.submission_prepared)
        self.assertTrue(report.final_confirmation_detected)
        self.assertIn("resume", report.filled_fields)
        self.assertIn("resume_source", report.filled_fields)
        self.assertIn("availability_arrival", report.filled_fields)
        preparation_path = session_path.parent / "submission-preparation.json"
        preparation = json.loads(preparation_path.read_text(encoding="utf-8"))
        self.assertEqual(preparation["status"], "ready_for_user_confirmation")
        self.assertEqual(preparation["verified_answers"]["days"], "5天")
        self.assertFalse(preparation["final_submit_clicked"])

        with self.assertRaisesRegex(BrowserAssistError, "确认令牌"):
            run_browser_session(
                session_path,
                browser_profile_dir=self.root / "rejected-browser-profile",
                headless=True,
                keep_open=False,
                confirmed_entry_text="投个简历",
                confirmation_token="SUBMIT_JOB_999",
            )

    def test_read_only_verification_detects_applied_marker(self) -> None:
        package_ok, browser_ok, _ = playwright_environment_status()
        if not package_ok or not browser_ok:
            self.skipTest("Playwright Chromium is not installed in this test runtime")
        fixture = Path(__file__).parent / "fixtures" / "application_status_applied.html"
        _, session_path = create_application_session(
            sample_profile(),
            self.job,
            self.pack,
            self.pack_path,
            sessions_dir=self.root / "verification-sessions",
            target_url=fixture.resolve().as_uri(),
            mode="open_only",
            local_demo=True,
        )

        verification, verification_path = verify_application_page(
            session_path,
            browser_profile_dir=self.root / "verification-browser-profile",
            headless=True,
        )

        self.assertEqual(verification.observed_status, "applied")
        self.assertIn("已投递", verification.explicit_markers)
        self.assertTrue(verification_path.is_file())
        self.assertTrue(Path(verification.screenshot_path or "").is_file())


if __name__ == "__main__":
    unittest.main()
