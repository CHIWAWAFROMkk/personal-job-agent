from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from job_agent.models.job_record import JobRecordInput
from job_agent.services.job_repository import JobRepository
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.preparation_pack import (
    PreparationPackError,
    build_preparation_pack,
    preparation_priority_for_status,
    render_preparation_pack_markdown,
    should_auto_generate_preparation,
    write_preparation_pack,
)
from tests.helpers import sample_profile


JD = """
公司：示例科技
岗位：商业化数据运营实习生
地点：上海
岗位职责：
1. 负责商业化活动配置、上线监控和数据复盘。
2. 跟进产品需求、Bug 提报和测试验收。
3. 研究 AI 工具在运营提效场景的应用并沉淀 SOP。
任职要求：
1. 熟练使用 SQL 进行数据分析；会 Tableau 优先。
2. 具备清晰沟通和项目推进能力。
3. 本科及以上学历，每周至少 4 天。
""".strip()


class PreparationPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.profile = sample_profile(days_per_week=4)
        repository = JobRepository(self.root / "jobs.sqlite3")
        outcome = repository.upsert_job(
            JobRecordInput(
                company="示例科技",
                title="商业化数据运营实习生",
                location="上海",
                jd_text=JD,
                source="测试来源",
                source_url="https://example.com/jobs/prep-1",
            )
        )
        self.job = repository.get_job(outcome.job_id)
        structured = structure_job_locally(
            JD,
            company=self.job.company,
            title=self.job.title,
            location=self.job.location,
        )
        self.result = match_job_locally(self.profile, structured)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pack_is_grounded_and_contains_all_learning_horizons(self) -> None:
        pack = build_preparation_pack(
            self.profile,
            self.job,
            self.result,
            trigger_status="hr_read",
            generated_at=datetime(2026, 8, 22, tzinfo=UTC),
        )

        serialized = pack.model_dump_json()
        mastery = {item.requirement: item.mastery for item in pack.capability_gaps}
        self.assertEqual(pack.job.priority, "elevated")
        self.assertEqual(
            {plan.horizon for plan in pack.learning_plans},
            {"1_hour", "1_day", "3_days", "7_days"},
        )
        for plan in pack.learning_plans:
            self.assertEqual(sum(task.minutes for task in plan.tasks), plan.total_minutes)
        self.assertEqual(mastery["SQL"], "mastered")
        self.assertEqual(mastery["Tableau"], "not_evidenced")
        self.assertNotIn("fact-pending-tableau", serialized)
        self.assertNotIn("private@example.com", serialized)
        self.assertNotIn("123456", serialized)
        self.assertTrue(pack.role_knowledge.likely_kpis)
        self.assertTrue(pack.interview_questions)
        self.assertTrue(pack.star_cards)

        markdown = render_preparation_pack_markdown(pack)
        self.assertIn("1 小时速成", markdown)
        self.assertIn("7 天系统学习", markdown)
        self.assertIn("基于 JD 的常见指标推断", markdown)
        self.assertIn("未找到已确认证据", markdown)

    def test_priority_and_positive_feedback_trigger_rules(self) -> None:
        self.assertEqual(preparation_priority_for_status("applied", 94), "elevated")
        self.assertEqual(preparation_priority_for_status("applied", 70), "routine")
        self.assertEqual(preparation_priority_for_status("hr_read", 70), "routine")
        self.assertEqual(preparation_priority_for_status("resume_requested", 70), "high")
        self.assertEqual(preparation_priority_for_status("screening", 70), "high")
        self.assertEqual(preparation_priority_for_status("assessment", 70), "urgent")
        self.assertEqual(preparation_priority_for_status("interview_1", 70), "critical")
        self.assertEqual(preparation_priority_for_status("rejected", 94), "closed")
        self.assertFalse(should_auto_generate_preparation("applied"))
        self.assertFalse(should_auto_generate_preparation("hr_read"))
        self.assertTrue(should_auto_generate_preparation("resume_requested"))
        self.assertTrue(should_auto_generate_preparation("final_interview"))

    def test_writer_is_versioned_and_refuses_overwrite(self) -> None:
        pack = build_preparation_pack(
            self.profile,
            self.job,
            self.result,
            trigger_status="screening",
        )
        output = self.root / "preparation" / "v1"
        files = write_preparation_pack(pack, self.job, output)
        self.assertTrue(files.pack_json.is_file())
        self.assertTrue(files.markdown.is_file())
        self.assertTrue(files.jd_text.is_file())
        with self.assertRaisesRegex(PreparationPackError, "拒绝覆盖"):
            write_preparation_pack(pack, self.job, output)


if __name__ == "__main__":
    unittest.main()
