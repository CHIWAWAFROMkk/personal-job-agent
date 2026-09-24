from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.models.job_record import JobRecordInput
from job_agent.services.job_repository import JobDatabaseError, JobRepository
from job_agent.services.portable_resume import find_profile_photo
from job_agent.services.profile_onboarding import (
    ProfileOnboardingError,
    ProfileOnboardingInput,
    onboard_profile,
    switch_to_new_profile,
)
from job_agent.services.profile_store import load_profile, save_profile


RESUME_TEXT = """
测试用户
test@example.com 13800138000
教育经历
示例大学 行政管理 本科 2027届
实习经历
使用 Excel 整理业务数据并制作可视化周报。
使用 Python、Pandas 和 SQL 完成课程数据分析项目。
负责公众号内容运营与活动文案撰写。
通过大学英语六级考试。
""".strip()


class ProfileOnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.profile_path = self.root / "private" / "profile.json"
        self.private_dir = self.root / "private"
        self.output_dir = self.root / "output"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def request(self, **overrides: object) -> ProfileOnboardingInput:
        payload: dict[str, object] = {
            "resume_filename": "测试简历.txt",
            "resume_bytes": RESUME_TEXT.encode("utf-8"),
            "display_name": "测试用户",
            "stage": "2027届本科实习求职",
            "target_roles": ["数据运营", "产品运营"],
            "target_industries": ["互联网"],
            "preferred_locations": ["上海"],
            "employment_types": ["实习"],
            "earliest_start": "2026-08-26",
            "days_per_week": 5,
            "duration_months": 3,
            "commute_origin": "上海徐汇区宜山路站",
            "max_commute_minutes": 60,
            "transport_modes": ["地铁", "步行"],
            "remote_acceptable": True,
            "school": "示例大学",
            "degree": "本科",
            "major": "行政管理",
            "graduation": "2027-06",
            "confirm_truth": True,
        }
        payload.update(overrides)
        return ProfileOnboardingInput(**payload)

    def test_fullwidth_name_is_not_imported_as_an_experience_fact(self) -> None:
        onboard_profile(self.request(display_name="验收用户（合成资料）",
            resume_bytes=("验收用户（合成资料）\n" + RESUME_TEXT).encode("utf-8")),
            profile_path=self.profile_path, private_dir=self.private_dir)
        profile = load_profile(self.profile_path)
        self.assertFalse(any("验收用户" in fact.statement for experience in profile.experiences
                             for fact in experience.facts))

    def test_first_upload_infers_unique_header_contacts_without_optional_form(self) -> None:
        result = onboard_profile(self.request(resume_bytes=("个人简历\n验收用户\n联系方式\nqa@example.com +86 138 0013 8000\n教育经历\n使用 SQL 完成课程数据分析。" ).encode("utf-8")),profile_path=self.profile_path,private_dir=self.private_dir)
        contact = load_profile(self.profile_path).person.contact
        self.assertEqual(contact.email,"qa@example.com")
        self.assertEqual(contact.phone,"13800138000")
        self.assertTrue(any("已从简历抬头识别邮箱、电话" in warning for warning in result.warnings))

    def test_first_upload_sync_private_fields_does_not_mean_clear_missing_contacts(self) -> None:
        onboard_profile(self.request(sync_private_fields=True,resume_bytes=("验收用户\nqa@example.com 13800138000\n教育经历\n使用 SQL 完成课程数据分析。").encode("utf-8")),profile_path=self.profile_path,private_dir=self.private_dir)
        contact = load_profile(self.profile_path).person.contact
        self.assertEqual(contact.email,"qa@example.com")
        self.assertEqual(contact.phone,"13800138000")

    def test_explicit_and_existing_contacts_take_priority_over_resume_inference(self) -> None:
        onboard_profile(self.request(email="chosen@example.com",phone="13900139000"),profile_path=self.profile_path,private_dir=self.private_dir)
        onboard_profile(self.request(),profile_path=self.profile_path,private_dir=self.private_dir)
        contact = load_profile(self.profile_path).person.contact
        self.assertEqual(contact.email,"chosen@example.com")
        self.assertEqual(contact.phone,"13900139000")
        onboard_profile(self.request(sync_private_fields=True),profile_path=self.profile_path,private_dir=self.private_dir)
        contact = load_profile(self.profile_path).person.contact
        self.assertEqual(contact.email,"")
        self.assertEqual(contact.phone,"")

    def test_multiple_people_contacts_remain_empty_instead_of_choosing_one(self) -> None:
        text = "验收用户\nqa@example.com 13800138000\n教育经历\n使用 SQL 完成课程数据分析。\n推荐人: mentor@example.com 13900139000"
        result = onboard_profile(self.request(resume_bytes=text.encode("utf-8")),profile_path=self.profile_path,private_dir=self.private_dir)
        contact = load_profile(self.profile_path).person.contact
        self.assertFalse(contact.email)
        self.assertFalse(contact.phone)
        self.assertTrue(any("有多个候选时不会自动选择" in warning for warning in result.warnings))

    def test_reference_contacts_and_body_only_contacts_are_not_inferred(self) -> None:
        from job_agent.services.profile_onboarding import _resume_contacts
        for text in (
            "验收用户\n推荐人\nmentor@example.com 13800138000\n教育经历\n课程项目",
            "验收用户\n项目经历\n招聘系统测试 qa@example.com 13800138000",
            "验收用户\n项目经历：联络系统测试 qa@example.com 13800138000",
        ):
            self.assertEqual(_resume_contacts(text),("",""))

    def test_repeated_same_header_contacts_are_not_ambiguous(self) -> None:
        from job_agent.services.profile_onboarding import _resume_contacts
        self.assertEqual(_resume_contacts("验收用户\n邮箱：qa@example.com；电话：13800138000\nqa@example.com +86 138 0013 8000\n教育经历\n课程项目"),("qa@example.com","13800138000"))

    def test_name_with_parenthetical_annotation_is_header_but_full_fact_survives(self) -> None:
        from job_agent.services.profile_onboarding import _fact_lines
        facts = _fact_lines("验收用户（合成资料）\n验收用户参与校园运营项目，负责数据分析。",display_name="验收用户")
        self.assertEqual(facts,["验收用户参与校园运营项目,负责数据分析。"])

    def test_unusable_resume_does_not_create_a_profile(self) -> None:
        with self.assertRaisesRegex(ProfileOnboardingError, "未能识别出可用经历"):
            onboard_profile(
                self.request(resume_bytes="姓名\n电话\n简历".encode("utf-8")),
                profile_path=self.profile_path,
                private_dir=self.private_dir,
            )
        self.assertFalse(self.profile_path.exists())

    def test_edit_form_can_clear_prefilled_fields_without_erasing_unloaded_contacts(self) -> None:
        onboard_profile(
            self.request(email="old@example.com", phone="13800138000", notes="旧偏好"),
            profile_path=self.profile_path,
            private_dir=self.private_dir,
        )
        onboard_profile(
            self.request(
                resume_filename="", resume_bytes=b"", confirm_truth=False,
                display_name="", stage="", target_roles=[], target_industries=[],
                preferred_locations=[], employment_types=[], earliest_start=None,
                days_per_week=None, duration_months=None, commute_origin="",
                max_commute_minutes=None, transport_modes=[], remote_acceptable=False,
                email="", phone="", notes="", sync_visible_fields=True,
            ),
            profile_path=self.profile_path,
            private_dir=self.private_dir,
        )
        profile = load_profile(self.profile_path)
        self.assertEqual(profile.person.display_name, "")
        self.assertEqual(profile.job_search.target_roles, [])
        self.assertEqual(profile.job_search.commute.origin, "")
        self.assertIsNone(profile.job_search.availability.days_per_week)
        self.assertEqual(profile.person.contact.email, "old@example.com")
        self.assertEqual(profile.job_search.notes, "旧偏好")
        onboard_profile(
            self.request(
                resume_filename="", resume_bytes=b"", confirm_truth=False,
                email="", phone="", notes="", sync_private_fields=True,
            ),
            profile_path=self.profile_path,
            private_dir=self.private_dir,
        )
        profile = load_profile(self.profile_path)
        self.assertEqual(profile.person.contact.email, "")
        self.assertEqual(profile.person.contact.phone, "")
        self.assertEqual(profile.job_search.notes, "")

    def test_imports_confirmed_resume_facts_and_conservative_skills(self) -> None:
        result = onboard_profile(
            self.request(),
            profile_path=self.profile_path,
            private_dir=self.private_dir,
        )
        profile = load_profile(self.profile_path)

        self.assertGreaterEqual(result.imported_facts, 4)
        self.assertGreaterEqual(result.imported_skills, 5)
        self.assertEqual(profile.person.display_name, "测试用户")
        self.assertEqual(profile.job_search.target_roles, ["数据运营", "产品运营"])
        self.assertEqual(profile.job_search.availability.days_per_week, 5)
        self.assertEqual(profile.job_search.commute.origin, "上海徐汇区宜山路站")
        self.assertEqual(profile.job_search.commute.max_one_way_minutes, 60)
        self.assertEqual(profile.job_search.commute.transport_modes, ["地铁", "步行"])
        self.assertTrue(profile.job_search.commute.remote_acceptable)
        self.assertEqual(len(profile.source_documents), 1)
        statements = [
            fact.statement
            for experience in profile.experiences
            for fact in experience.facts
        ]
        self.assertFalse(any("test@example.com" in line for line in statements))
        skill_by_name = {skill.name: skill for skill in profile.skills}
        self.assertEqual(skill_by_name["SQL"].level, "basic")
        self.assertEqual(skill_by_name["Excel"].status, "user_confirmed")

    def test_reimport_updates_preferences_without_duplicate_resume_facts(self) -> None:
        onboard_profile(
            self.request(),
            profile_path=self.profile_path,
            private_dir=self.private_dir,
        )
        profile = load_profile(self.profile_path)
        profile.source_documents[0].original_path = "D:/旧下载目录/测试简历.txt"
        save_profile(profile, self.profile_path, overwrite=True, create_backup=False)
        second = onboard_profile(
            self.request(target_roles=["AI产品运营"]),
            profile_path=self.profile_path,
            private_dir=self.private_dir,
        )
        profile = load_profile(self.profile_path)
        self.assertEqual(second.imported_facts, 0)
        self.assertEqual(second.imported_skills, 0)
        self.assertEqual(profile.job_search.target_roles, ["AI产品运营"])
        self.assertEqual(len(profile.source_documents), 1)
        self.assertEqual(len(profile.experiences), 1)
        expected_path = second.resume_path.relative_to(self.private_dir.resolve()).as_posix()
        self.assertEqual(profile.source_documents[0].original_path, expected_path)

    def test_existing_profile_can_update_commute_without_reuploading_resume(self) -> None:
        onboard_profile(
            self.request(),
            profile_path=self.profile_path,
            private_dir=self.private_dir,
        )

        result = onboard_profile(
            self.request(
                resume_filename="",
                resume_bytes=b"",
                confirm_truth=False,
                commute_origin="上海静安寺站",
                max_commute_minutes=45,
                transport_modes=["地铁"],
            ),
            profile_path=self.profile_path,
            private_dir=self.private_dir,
        )
        profile = load_profile(self.profile_path)

        self.assertIsNone(result.resume_path)
        self.assertEqual(result.extracted_characters, 0)
        self.assertEqual(profile.job_search.commute.origin, "上海静安寺站")
        self.assertEqual(profile.job_search.commute.max_one_way_minutes, 45)
        self.assertEqual(len(profile.source_documents), 1)
        self.assertEqual(len(profile.experiences), 1)

    def test_replace_requires_explicit_confirmation(self) -> None:
        with self.assertRaisesRegex(ProfileOnboardingError, "明确确认"):
            onboard_profile(
                self.request(mode="replace", confirm_replace=False),
                profile_path=self.profile_path,
                private_dir=self.private_dir,
            )

    def test_switch_backs_up_old_profile_and_clears_all_user_tables(self) -> None:
        onboard_profile(self.request(), profile_path=self.profile_path, private_dir=self.private_dir)
        old_profile_bytes = self.profile_path.read_bytes()
        repository = JobRepository(self.root / "jobs.sqlite3")
        job_id = repository.upsert_job(JobRecordInput(
            company="旧用户企业", title="旧用户岗位", jd_text="SQL 分析",
            source="synthetic",
        )).job_id
        repository.set_job_archived(job_id, True)
        (self.private_dir / "photo").mkdir()
        (self.private_dir / "photo" / "profile-photo.png").write_bytes(b"old-photo")
        (self.private_dir / "assets").mkdir()
        (self.private_dir / "assets" / "profile-photo.jpg").write_bytes(b"old-fallback-photo")
        (self.private_dir / "copilot").mkdir()
        (self.private_dir / "copilot" / "current-thread.json").write_text("old private chat", encoding="utf-8")
        (self.private_dir / "browser-profiles").mkdir()
        (self.private_dir / "browser-profiles" / "old-cookie.txt").write_text("old", encoding="utf-8")
        (self.private_dir / "status-sync").mkdir()
        (self.private_dir / "status-sync" / "old-status.txt").write_text("old", encoding="utf-8")
        self.output_dir.mkdir()
        (self.output_dir / "applications").mkdir()
        (self.output_dir / "applications" / "old-user.txt").write_text("old", encoding="utf-8")
        with repository._connection() as connection:
            connection.execute(
                "INSERT INTO mock_interview_sessions(id,job_id,data) VALUES (?,?,?)",
                ("old-session", job_id, '{"answer":"old private answer"}'),
            )
            connection.execute(
                "INSERT INTO mock_interview_requests(key,fingerprint,session_id) VALUES (?,?,?)",
                ("old-key", "old-fingerprint", "old-session"),
            )
            connection.execute(
                "INSERT INTO tracking_requests(request_id,payload_hash,response_json,created_at) VALUES (?,?,?,?)",
                ("old-request", "old-hash", '{"private":"old"}', "2026-09-23T00:00:00Z"),
            )

        result, database_backup, profile_backup, private_backup, output_backup = switch_to_new_profile(
            self.request(mode="replace", confirm_replace=True, display_name="新用户"),
            profile_path=self.profile_path,
            private_dir=self.private_dir,
            output_dir=self.output_dir,
            repository=repository,
        )
        self.assertTrue(result.replaced_profile)
        self.assertEqual(load_profile(self.profile_path).person.display_name, "新用户")
        self.assertIsNotNone(profile_backup)
        self.assertEqual(profile_backup.read_bytes(), old_profile_bytes)
        self.assertTrue(private_backup.is_dir())
        self.assertTrue(output_backup.is_dir())
        self.assertTrue((output_backup / "applications" / "old-user.txt").is_file())
        self.assertFalse((self.output_dir / "applications" / "old-user.txt").exists())
        for name in ("photo", "assets", "copilot", "browser-profiles", "status-sync"):
            self.assertTrue((private_backup / name).is_dir())
            self.assertFalse((self.private_dir / name).exists())
        self.assertIsNone(find_profile_photo(self.private_dir))
        self.assertEqual(result.resume_path.read_bytes(), RESUME_TEXT.encode("utf-8"))
        self.assertEqual(JobRepository(database_backup).stats().jobs, 1)
        with repository._connection() as connection:
            for table in (
                "jobs", "archived_jobs", "tracking_requests",
                "mock_interview_sessions", "mock_interview_requests",
            ):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_switch_failure_rolls_back_old_profile_and_archived_job(self) -> None:
        onboard_profile(self.request(), profile_path=self.profile_path, private_dir=self.private_dir)
        old_profile_bytes = self.profile_path.read_bytes()
        repository = JobRepository(self.root / "jobs.sqlite3")
        job_id = repository.upsert_job(JobRecordInput(
            company="旧用户企业", title="旧用户岗位", jd_text="SQL 分析",
            source="synthetic",
        )).job_id
        repository.set_job_archived(job_id, True)
        with repository._connection() as connection:
            connection.execute("CREATE TRIGGER block_switch BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT, 'injected failure'); END")

        with self.assertRaisesRegex(JobDatabaseError, "injected failure"):
            switch_to_new_profile(
                self.request(mode="replace", confirm_replace=True, display_name="新用户"),
                profile_path=self.profile_path, private_dir=self.private_dir,
                output_dir=self.output_dir,
                repository=repository,
            )
        self.assertEqual(self.profile_path.read_bytes(), old_profile_bytes)
        self.assertEqual(repository.stats().jobs, 1)
        self.assertEqual(repository.archived_jobs(), {job_id})

    def test_invalid_new_resume_is_quarantined_without_exposing_new_files(self) -> None:
        onboard_profile(self.request(), profile_path=self.profile_path, private_dir=self.private_dir)
        old_bytes = self.profile_path.read_bytes()
        repository = JobRepository(self.root / "jobs.sqlite3")
        repository.upsert_job(JobRecordInput(
            company="旧用户企业", title="旧用户岗位", jd_text="SQL 分析", source="synthetic",
        ))
        invalid_resume = "姓名\n电话\n简历".encode("utf-8")
        with self.assertRaisesRegex(ProfileOnboardingError, "未能识别出可用经历.*归档"):
            switch_to_new_profile(
                self.request(mode="replace", confirm_replace=True, display_name="新用户",
                             resume_bytes=invalid_resume),
                profile_path=self.profile_path, private_dir=self.private_dir,
                output_dir=self.output_dir, repository=repository,
            )
        self.assertEqual(self.profile_path.read_bytes(), old_bytes)
        self.assertEqual(repository.stats().jobs, 1)
        self.assertFalse(self.output_dir.exists())
        failed = list((self.private_dir / "backups").glob("failed-profile-switch-*"))
        self.assertEqual(len(failed), 1)
        self.assertEqual(next((failed[0] / "resumes").iterdir()).read_bytes(), invalid_resume)
        self.assertEqual(list((self.private_dir / "backups").glob(".profile-switch-staging-*")), [])
        self.assertNotIn(invalid_resume, self.profile_path.read_bytes())

    def test_switch_restores_old_profile_if_publish_fails_after_replace(self) -> None:
        onboard_profile(self.request(), profile_path=self.profile_path, private_dir=self.private_dir)
        old_profile_bytes = self.profile_path.read_bytes()
        repository = JobRepository(self.root / "jobs.sqlite3")
        repository.upsert_job(JobRecordInput(
            company="旧用户企业", title="旧用户岗位", jd_text="SQL 分析",
            source="synthetic",
        ))
        (self.private_dir / "photo").mkdir()
        (self.private_dir / "photo" / "profile-photo.png").write_bytes(b"old-photo")
        self.output_dir.mkdir()
        (self.output_dir / "old-artifact.txt").write_text("old", encoding="utf-8")
        original_replace = Path.replace

        def fail_after_publish(source: Path, target: Path) -> Path:
            outcome = original_replace(source, target)
            if target == self.profile_path and source.parent.name.startswith(".profile-switch-staging-"):
                raise OSError("injected publish failure")
            return outcome

        with patch.object(Path, "replace", fail_after_publish):
            with self.assertRaisesRegex(JobDatabaseError, "injected publish failure"):
                switch_to_new_profile(
                    self.request(mode="replace", confirm_replace=True, display_name="新用户"),
                    profile_path=self.profile_path, private_dir=self.private_dir,
                    output_dir=self.output_dir,
                    repository=repository,
                )
        self.assertEqual(self.profile_path.read_bytes(), old_profile_bytes)
        self.assertEqual(repository.stats().jobs, 1)
        self.assertEqual((self.private_dir / "photo" / "profile-photo.png").read_bytes(), b"old-photo")
        self.assertEqual((self.output_dir / "old-artifact.txt").read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
