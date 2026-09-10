import copy
import json
import unittest
from unittest.mock import patch

from job_agent.models.profile import EvidenceFact, Experience
from job_agent.services.resume_compose import compose_resume_content_with_jd
from job_agent.services.resume_polish import ResumePolishError
from job_agent.services.runtime_config import RuntimeConfig, AIConnectorConfig
from tests.helpers import sample_profile
from tests.test_resume_polish import _content


class ResumeComposeTests(unittest.TestCase):
    def setUp(self):
        self.profile = sample_profile()
        self.profile.experiences.append(Experience(
            id="exp-research", kind="project", organization="示例大学", role="问卷调研",
            facts=[EvidenceFact(id="fact-research", statement="设计问卷并整理100份有效答卷，形成调研报告。", status="documented")]))
        self.content = _content()
        self.content["truthfulness"] = {"confirmed_fact_ids": ["fact-sql-analysis"]}
        self.config = RuntimeConfig(ai=AIConnectorConfig(provider="openai_compatible", model="test", base_url="http://localhost:9"))
        self.response = {
            "summary": "具有问卷分析和报告撰写实践。", "summary_fact_ids": ["fact-research"],
            "self_evaluation": "", "skill_ids": ["skill-sql"],
            "entries": [{"experience_id": "exp-research", "bullets": [{"label": "调研与交付", "text": "围绕调研任务设计问卷，整理100份有效答卷并形成报告。", "fact_ids": ["fact-research"]}]}],
            "strategy": "根据JD优先展示调研与报告能力。", "questions": [],
        }

    def compose(self, response=None):
        with patch("job_agent.services.resume_compose._invoke_ai", return_value=(json.dumps(response or self.response, ensure_ascii=False), 500, 400)) as invoke:
            result = compose_resume_content_with_jd(self.content, "问卷调研与研究报告", profile=self.profile, config=self.config)
        return result, invoke

    def test_whole_profile_selection_is_not_limited_to_original_bullets(self):
        original = copy.deepcopy(self.content)
        result, invoke = self.compose()
        entry = result.content["experience_sections"][0]["entries"][0]
        self.assertEqual(entry["experience_id"], "exp-research")
        self.assertEqual(entry["organization"], "示例大学")
        self.assertEqual(self.content, original)
        payload = json.loads(invoke.call_args.args[1])
        rendered = json.dumps(payload)
        self.assertNotIn("private@example.com", rendered)
        self.assertNotIn("123456", rendered)
        self.assertNotIn("fact-pending-tableau", rendered)
        self.assertNotIn("original_path", rendered)
        self.assertEqual(result.content["generation"]["engine"], "cloud_composition")

    def test_rejects_wrong_experience_unconfirmed_facts_numbers_and_skill_invention(self):
        for key, value in (("fact_ids", ["fact-sql-analysis"]), ("fact_ids", ["fact-pending-tableau"]),
                           ("text", "整理1000份有效答卷。"), ("text", "整理100%有效答卷。"),
                           ("text", "独立负责问卷分析。")):
            with self.subTest(key=key, value=value):
                response = copy.deepcopy(self.response)
                response["entries"][0]["bullets"][0][key] = value
                with self.assertRaises(ResumePolishError):
                    self.compose(response)
        response = copy.deepcopy(self.response)
        response["skill_ids"] = ["skill-tableau-pending"]
        with self.assertRaises(ResumePolishError):
            self.compose(response)

    def test_can_combine_multiple_facts_from_same_experience(self):
        self.profile.experiences[-1].facts.append(EvidenceFact(id="fact-award", statement="项目获得校级一等奖。", status="documented"))
        self.response["entries"][0]["bullets"][0].update(
            text="整理100份有效答卷并形成调研报告，项目获得校级一等奖。", fact_ids=["fact-research", "fact-award"])
        result, _ = self.compose()
        self.assertIn("fact-award", result.content["truthfulness"]["confirmed_fact_ids"])

    def test_rejects_duplicate_experiences_and_unsupported_summary_metrics(self):
        response = copy.deepcopy(self.response)
        response["entries"] *= 2
        with self.assertRaises(ResumePolishError):
            self.compose(response)
        response = copy.deepcopy(self.response)
        response["summary"] = "实现100%准确率。"
        with self.assertRaises(ResumePolishError):
            self.compose(response)
