import unittest

from job_agent.models.job import HardGateStatus, MatchStatus
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from tests.helpers import sample_profile


JD = """
公司：示例科技
岗位：AI运营实习生
地点：上海

岗位职责：负责 AI 产品运营和用户数据分析。
岗位要求：
- 本科及以上学历；
- 每周至少 4 天，连续实习 3 个月；
- 必须熟练使用 SQL；
- Tableau 经验加分。
""".strip()


class LocalMatcherTests(unittest.TestCase):
    def test_only_confirmed_evidence_can_match(self) -> None:
        profile = sample_profile()
        job = structure_job_locally(JD)
        result = match_job_locally(profile, job)

        sql = next(item for item in result.evidence if item.requirement == "SQL")
        tableau = next(item for item in result.evidence if item.requirement == "Tableau")
        self.assertEqual(sql.status, MatchStatus.MATCHED)
        self.assertEqual(sql.profile_fact_ids, ["fact-sql-analysis"])
        self.assertEqual(tableau.status, MatchStatus.GAP)
        self.assertNotIn("fact-pending-tableau", tableau.profile_fact_ids)
        self.assertGreaterEqual(result.overall_score, 60)

    def test_known_hard_gates_pass(self) -> None:
        result = match_job_locally(sample_profile(), structure_job_locally(JD))
        statuses = {
            gate.requirement: gate.status
            for gate in result.hard_gates
            if "本科" in gate.requirement or "每周" in gate.requirement
        }
        self.assertTrue(statuses)
        self.assertTrue(all(status == HardGateStatus.PASSES for status in statuses.values()))

    def test_failed_hard_gate_caps_score(self) -> None:
        result = match_job_locally(
            sample_profile(days_per_week=3),
            structure_job_locally(JD),
        )
        self.assertTrue(
            any(gate.status == HardGateStatus.FAILS for gate in result.hard_gates)
        )
        self.assertLessEqual(result.overall_score, 59)

    def test_metadata_is_not_parsed_as_requirement(self) -> None:
        job = structure_job_locally(JD, source="实习僧")
        texts = [item.text for item in job.requirements]
        self.assertFalse(any(text.startswith("岗位：") for text in texts))
        self.assertFalse(any(text.startswith("来源：") for text in texts))

    def test_requirement_containing_responsibility_word_is_not_a_duty(self) -> None:
        jd = """
岗位：活动运营实习生
岗位职责：
1. 负责活动配置和数据复盘。
任职要求：
1. 有独立负责活动或项目的经历优先。
""".strip()
        job = structure_job_locally(jd)
        self.assertEqual(job.responsibilities, ["负责活动配置和数据复盘。"])
        self.assertTrue(
            any("独立负责活动" in requirement.text for requirement in job.requirements)
        )

    def test_bachelor_requirement_with_postgraduate_preference_passes(self) -> None:
        jd = """
公司：示例科技
岗位：策略运营实习生
任职要求：
1. 本科及以上学历在读，专业不限，大四及研究生优先。
""".strip()
        result = match_job_locally(sample_profile(), structure_job_locally(jd))
        education_gates = [gate for gate in result.hard_gates if "本科" in gate.requirement]
        self.assertEqual(len(education_gates), 1)
        self.assertEqual(education_gates[0].status, HardGateStatus.PASSES)

    def test_preferred_duration_does_not_become_hard_gate(self) -> None:
        jd = """
岗位：策略运营实习生
任职要求：
1. 每周能稳定到岗 4 天，能持续实习 6 个月以上者优先。
""".strip()
        result = match_job_locally(sample_profile(), structure_job_locally(jd))
        self.assertFalse(any("6 个月" in gate.requirement for gate in result.hard_gates))
        day_gate = next(gate for gate in result.hard_gates if "每周" in gate.requirement)
        self.assertEqual(day_gate.status, HardGateStatus.PASSES)

    def test_unknown_hard_gate_prevents_strong_recommendation(self) -> None:
        profile = sample_profile()
        profile.job_search.availability.days_per_week = None
        result = match_job_locally(profile, structure_job_locally(JD))
        self.assertLessEqual(result.overall_score, 84)

    def test_availability_range_is_conditional_not_failed(self) -> None:
        profile = sample_profile(days_per_week=4)
        profile.job_search.availability.max_days_per_week = 5
        jd = """
岗位：商业化运营实习生
任职要求：
1. 每周能稳定到岗 5 天。
""".strip()
        result = match_job_locally(profile, structure_job_locally(jd))
        gate = next(gate for gate in result.hard_gates if "每周" in gate.requirement)
        self.assertEqual(gate.status, HardGateStatus.UNKNOWN)
        self.assertIn("保证 4 天、最多 5 天", gate.explanation)

    def test_confirmed_fact_skills_are_matchable(self) -> None:
        jd = """
岗位：数据运营实习生
任职要求：
1. 必须熟练使用 Excel。
""".strip()
        result = match_job_locally(sample_profile(), structure_job_locally(jd))
        excel = next(item for item in result.evidence if item.requirement == "Excel")
        self.assertEqual(excel.status, MatchStatus.MATCHED)
        self.assertEqual(excel.profile_fact_ids, ["fact-sql-analysis"])


if __name__ == "__main__":
    unittest.main()
