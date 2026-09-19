import json
import re
import unittest
from unittest.mock import Mock, patch

from job_agent.models.profile import EvidenceFact, Experience, ExperienceKind, ClaimStatus
from job_agent.services.resume_tailor_live import tailor_live, contains_phrase
from job_agent.services.dashboard_routes.jobs_api import handle_resume_tailor_live
from tests.helpers import sample_profile


class TailorLiveTests(unittest.TestCase):
    def setUp(self):
        self.profile = sample_profile()
        self.args = dict(jd_text="要求 SQL、Python 和数据分析，本科及以上，每周至少 3 天。",
                         paragraph="使用 SQL 清洗业务数据并输出周度分析。", fact_ids=["fact-sql-analysis"])

    def test_three_evidence_only_variants(self):
        result = tailor_live(self.profile, **self.args)
        self.assertEqual(len(result["suggestions"]), 3)
        for item in result["suggestions"]:
            self.assertEqual(item["source_text"], self.profile.experiences[0].facts[0].statement)
            self.assertEqual(item["text"].replace("；", ""), item["source_text"])
            self.assertEqual(item["fact_ids"], self.args["fact_ids"])
        self.assertEqual(result["engine"], "local")
        self.assertIn("Python", result["coverage"]["missing"])

    def test_safe_rewrite_keeps_assistance_and_explicit_output(self):
        fact = self.profile.experiences[0].facts[0]
        fact.statement = "我协助使用 SQL 整理 12 份数据并输出周报。"
        before = fact.statement
        result = tailor_live(self.profile, **self.args)
        revised = result["suggestions"][1]
        self.assertEqual(revised["text"], "协助使用 SQL 整理 12 份数据；并输出周报。")
        self.assertEqual(revised["source_text"], before)
        self.assertEqual(fact.statement, before)
        self.assertEqual(len(revised["changes"]), 2)
        self.assertIn("结果及验证依据待核对", revised["missing_evidence"])
        self.assertTrue(revised["questions"])
        self.assertNotIn("负责", revised["text"])
        self.assertEqual(re.findall(r"\d+", revised["text"]), ["12"])

    def test_qualifiers_are_not_dropped_or_separated(self):
        for statement in ["我未使用 SQL 并输出周报。", "我计划使用 SQL 并输出周报。",
                          "我仅协助整理数据并输出周报。", "我可能使用 SQL 并输出周报。",
                          "我使用 SQL 整理数据，但没有独立完成。", "I may output a report."]:
            with self.subTest(statement=statement):
                self.profile.experiences[0].facts[0].statement = statement
                result = tailor_live(self.profile, **self.args)
                for item in result["suggestions"]:
                    self.assertEqual(item["text"], statement)
                    self.assertIn("事实含有限定表述", item["missing_evidence"])

    def test_no_evidence_of_result_means_question_not_invented_star(self):
        self.profile.experiences[0].facts[0].statement = "参与整理业务资料。"
        result = tailor_live(self.profile, **{**self.args, "paragraph": "提升效率 80%"})
        for item in result["suggestions"]:
            self.assertEqual(item["text"], "参与整理业务资料。")
            self.assertIn("交付物待核对", item["missing_evidence"])
            self.assertIn("结果及验证依据待核对", item["missing_evidence"])
            self.assertNotIn("80", item["text"])

    def test_same_experience_facts_never_gain_causal_connection(self):
        self.profile.experiences[0].facts.append(EvidenceFact(id="extra-fact",
            statement="协助制作演示文档。", status=ClaimStatus.USER_CONFIRMED))
        result = tailor_live(self.profile, **{**self.args, "fact_ids": ["extra-fact", "fact-sql-analysis"]})
        revised = result["suggestions"][1]
        self.assertEqual(revised["fact_ids"], ["fact-sql-analysis", "extra-fact"])
        self.assertEqual(revised["text"], "使用 SQL 清洗业务数据；并输出周度分析。\n协助制作演示文档。")
        self.assertNotIn("因此", revised["text"])

    def test_unknown_pending_and_no_sources_fail_closed(self):
        for ids in [[], ["missing"], ["fact-pending-tableau"], ["fact-sql-analysis", "missing"]]:
            self.assertEqual(tailor_live(self.profile, **{**self.args, "fact_ids": ids})["suggestions"], [])

    def test_mixed_experiences_are_not_merged(self):
        self.profile.experiences.append(Experience(id="other", role="示例项目", kind=ExperienceKind.PROJECT,
            facts=[EvidenceFact(id="other-fact", statement="使用 Python 整理数据。", status=ClaimStatus.USER_CONFIRMED)]))
        result = tailor_live(self.profile, **{**self.args, "fact_ids": ["fact-sql-analysis", "other-fact"]})
        self.assertEqual(result["suggestions"], [])

    def test_user_text_does_not_become_verified(self):
        result = tailor_live(self.profile, **{**self.args, "paragraph": "收入增长 500%，精通 Python。"})
        self.assertNotIn("500", json.dumps(result["suggestions"]))
        self.assertTrue(any("尚未自动核验" in v for v in result["warnings"]))

    def test_english_word_boundaries(self):
        self.assertFalse(contains_phrase("email trained postgresql", "AI"))
        self.assertFalse(contains_phrase("nosql", "SQL"))
        self.assertTrue(contains_phrase("用SQL分析", "SQL"))
        result = tailor_live(self.profile, **{**self.args, "jd_text": "email training", "resume_text": "SQL"})
        self.assertIsNone(result["coverage"]["score"])

    def test_coverage_uses_whole_resume_when_provided(self):
        result = tailor_live(self.profile, **{**self.args, "resume_text": "SQL Python 数据分析"})
        self.assertEqual(result["coverage"]["score"], 100)

    def test_bounds_and_types(self):
        for key, value in [("jd_text", ""), ("jd_text", "x" * 30001), ("paragraph", 3),
                           ("fact_ids", "bad"), ("resume_text", "x" * 60001)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                tailor_live(self.profile, **{**self.args, key: value})

    def test_route_requires_authorization_before_reading(self):
        handler = Mock()
        handler._authorized_action.return_value = False
        handle_resume_tailor_live(handler)
        handler._reject_unauthorized_action.assert_called_once()
        handler._read_body.assert_not_called()

    def test_route_bounded_and_read_only(self):
        handler = Mock()
        handler._authorized_action.return_value = True
        handler.headers = {"Content-Type": "application/json"}
        handler._read_body.return_value = json.dumps({"job_id": 1, **self.args}).encode()
        with patch("job_agent.services.dashboard_routes.jobs_api.load_profile", return_value=self.profile):
            handle_resume_tailor_live(handler)
        handler._read_body.assert_called_once_with(256 * 1024)
        self.assertEqual(handler._json.call_args.args[0]["engine"], "local")
        handler.repository.get_job.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
