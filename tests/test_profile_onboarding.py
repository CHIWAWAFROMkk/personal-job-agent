from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_agent.services.profile_onboarding import (
    ProfileOnboardingError,
    ProfileOnboardingInput,
    onboard_profile,
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


if __name__ == "__main__":
    unittest.main()
