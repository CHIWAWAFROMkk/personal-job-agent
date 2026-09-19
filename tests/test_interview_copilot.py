import unittest
from pathlib import Path
from job_agent.models.job_record import JobDetail
from tests.helpers import sample_profile
from job_agent.services.runtime_config import RuntimeConfig, AIConnectorConfig
from job_agent.services.interview_copilot import generate_interview_prep


class InterviewCopilotTests(unittest.TestCase):
    def test_generate_interview_prep_for_ops_role(self):
        profile = sample_profile()
        config = RuntimeConfig(ai=AIConnectorConfig(provider="local"))
        job = JobDetail(
            job_id=101,
            company="百度（上海）",
            title="产品运营实习生",
            jd_text="负责产品运营与数据分析，熟练使用Excel与AI工具，具备跨团队协同能力。",
            location="上海",
            status="discovered",
            created_at="2026-09-01T00:00:00Z",
            updated_at="2026-09-01T00:00:00Z",
            first_seen_at="2026-09-01T00:00:00Z",
            last_seen_at="2026-09-01T00:00:00Z",
        )
        
        result = generate_interview_prep(job, profile, config)
        self.assertEqual(result.job_id, 101)
        self.assertEqual(result.company, "百度（上海）")
        self.assertEqual(len(result.questions), 5)
        
        categories = [q.category for q in result.questions]
        self.assertIn("胜任动机", categories)
        self.assertIn("经历深挖", categories)
        self.assertIn("能力缺口", categories)
        self.assertIn("协作与执行", categories)
        self.assertIn("反问", categories)

        # 检查是否结合了真实经历
        all_text = " ".join(q.star_answer for q in result.questions)
        self.assertIn(profile.experiences[0].facts[0].statement, all_text)
        self.assertNotIn(profile.experiences[0].facts[1].statement, all_text)

    def test_generate_interview_prep_for_hr_role(self):
        profile = sample_profile()
        config = RuntimeConfig(ai=AIConnectorConfig(provider="local"))
        job = JobDetail(
            job_id=102,
            company="强生（中国）",
            title="HR实习生",
            jd_text="负责HR系统主数据维护，员工入职档案整理与跨部门跟进。",
            location="上海",
            status="discovered",
            created_at="2026-09-01T00:00:00Z",
            updated_at="2026-09-01T00:00:00Z",
            first_seen_at="2026-09-01T00:00:00Z",
            last_seen_at="2026-09-01T00:00:00Z",
        )
        
        result = generate_interview_prep(job, profile, config)
        self.assertEqual(result.role, "HR实习生")
        self.assertEqual(len(result.questions), 5)
        
        all_text = " ".join(q.star_answer for q in result.questions)
        self.assertIn(profile.experiences[0].facts[0].statement, all_text)

    def test_settings_test_connection_local(self):
        from unittest.mock import MagicMock
        from job_agent.services.dashboard_routes.dashboard_api import handle_settings_test_connection

        handler = MagicMock()
        handler._authorized_action.return_value = True
        handler.headers = {"Content-Length": "20"}
        handler._read_body.return_value = b'{"provider": "local"}'
        handler.runtime_config_path = Path("data/private/runtime_config.json")

        handle_settings_test_connection(handler)
        handler._json.assert_called_once()
        args = handler._json.call_args[0][0]
        self.assertTrue(args.get("ok"))
        self.assertIn("本地能力无需网络连接", args.get("message"))


if __name__ == "__main__":
    unittest.main()
