import json
import tempfile
import unittest
from pathlib import Path

from job_agent.models.job import MatchResult, Recommendation, ScoreBreakdown
from job_agent.models.job_record import JobRecordInput
from job_agent.services.copilot_chat import (
    CopilotChatError,
    build_safe_workspace_context,
    load_copilot_thread,
    reset_copilot_thread,
    respond_to_copilot,
)
from job_agent.services.job_repository import JobRepository
from job_agent.services.local_matcher import structure_job_locally
from job_agent.services.profile_store import save_profile
from tests.helpers import sample_profile


JD = "负责 SQL 数据分析与运营策略；要求本科，每周到岗四天。"


class CopilotChatTests(unittest.TestCase):
    def test_experience_templates_are_advice_without_resume_write_actions(self):
        snapshot = respond_to_copilot(
            "分析我的简历，给我经历写作模板，并优化写法",
            thread_path=self.thread_path, repository=self.repository,
            profile_path=self.profile_path, runtime_config_path=self.runtime_path,
            usage_path=self.usage_path,
        )
        reply = snapshot.thread.messages[-1]
        self.assertIn("非本人经历", reply.content)
        self.assertIn("[本人实际任务]", reply.content)
        self.assertFalse(any(action.kind in {"revise_resume", "create_resume_draft"} for action in reply.actions))

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.private = self.root / "private"
        self.profile_path = self.private / "profile.json"
        self.thread_path = self.private / "copilot" / "current-thread.json"
        self.runtime_path = self.private / "app-settings.json"
        self.usage_path = self.private / "api-usage.json"
        save_profile(sample_profile(), self.profile_path)
        self.repository = JobRepository(self.private / "jobs.sqlite3")
        record = self.repository.upsert_job(
            JobRecordInput(
                company="示例数据",
                title="数据运营实习生",
                jd_text=JD,
                source="test",
                source_url="https://example.com/jobs/1",
                location="上海",
            )
        )
        structured = structure_job_locally(
            JD,
            company="示例数据",
            title="数据运营实习生",
            location="上海",
            source="test",
            source_url="https://example.com/jobs/1",
        )
        result = MatchResult(
            job=structured,
            score_breakdown=ScoreBreakdown(
                role_direction=18,
                skills=22,
                experience=18,
                education=9,
                logistics=9,
                preferences=4,
            ),
            overall_score=80,
            recommendation=Recommendation.RECOMMEND,
            engine="test",
        )
        self.repository.add_match_result(record.job_id, result)
        self.job_id = record.job_id

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_selected_job_outside_top_twelve_and_numbers_stay_in_scope(self) -> None:
        for index in range(14):
            self.repository.upsert_job(JobRecordInput(
                company=f"测试公司{index}", title="测试职位", jd_text=JD,
                source="test", source_url=f"https://example.com/extra/{index}",
            ))
        context = build_safe_workspace_context(
            self.repository, profile_path=self.profile_path, selected_job_id=2,
        )
        self.assertEqual(context["selected_job_id"], 2)
        self.assertEqual(context["selected_job"]["jd_text"], JD)
        self.assertFalse(context["selected_job"]["jd_truncated"])
        self.assertIn(2, [job["job_id"] for job in context["top_jobs"]])
        snapshot = respond_to_copilot(
            "简历缩短到 1 页，去掉空话", selected_job_id=2,
            thread_path=self.thread_path, repository=self.repository,
            profile_path=self.profile_path, runtime_config_path=self.runtime_path,
            usage_path=self.usage_path,
        )
        self.assertEqual(snapshot.thread.messages[-1].job_id, 2)
        self.assertEqual(snapshot.thread.messages[-2].job_id, 2)
        self.assertEqual(snapshot.thread.messages[-1].actions[0].job_id, 2)
        self.assertEqual(snapshot.thread.messages[-1].actions[0].kind, "revise_resume")
        self.repository.record_application_status(1, "interview_1", source="test")
        interview = respond_to_copilot(
            "这个职位面试该准备什么？", selected_job_id=2,
            thread_path=self.thread_path, repository=self.repository,
            profile_path=self.profile_path, runtime_config_path=self.runtime_path,
            usage_path=self.usage_path,
        )
        self.assertEqual(interview.thread.messages[-1].actions[0].job_id, 2)

    def test_invalid_or_conflicting_selection_does_not_save_or_create_actions(self) -> None:
        for selected, message in [(True, "改简历"), ("1", "改简历"), (0, "改简历"), (99999, "改简历"), (1, "修改 #99999 的简历")]:
            with self.subTest(selected=selected, message=message):
                with self.assertRaises(CopilotChatError):
                    respond_to_copilot(
                        message, selected_job_id=selected,
                        thread_path=self.thread_path, repository=self.repository,
                        profile_path=self.profile_path, runtime_config_path=self.runtime_path,
                        usage_path=self.usage_path,
                    )
        self.assertFalse(self.thread_path.exists())

    def test_safe_context_excludes_contact_secrets_and_paths(self) -> None:
        context = build_safe_workspace_context(
            self.repository,
            profile_path=self.profile_path,
        )
        rendered = json.dumps(context, ensure_ascii=False)

        self.assertNotIn("private@example.com", rendered)
        self.assertNotIn("123456", rendered)
        self.assertNotIn(str(self.root), rendered)
        evidence = context["profile"]["resume_evidence"]
        self.assertIn("fact-sql-analysis", json.dumps(evidence))
        self.assertNotIn("fact-pending-tableau", json.dumps(evidence))
        self.assertEqual(context["profile"]["pending_resume_facts"][0]["status"], "needs_confirmation")
        self.assertEqual(context["top_jobs"][0]["job_id"], self.job_id)

    def test_local_chat_persists_job_linked_safe_actions(self) -> None:
        snapshot = respond_to_copilot(
            "给最高匹配岗位准备简历草稿",
            thread_path=self.thread_path,
            repository=self.repository,
            profile_path=self.profile_path,
            runtime_config_path=self.runtime_path,
            usage_path=self.usage_path,
        )
        assistant = snapshot.thread.messages[-1]

        self.assertEqual(assistant.role, "assistant")
        self.assertIn("岗位专属草稿", assistant.content)
        self.assertEqual(assistant.actions[0].kind, "create_resume_draft")
        self.assertEqual(assistant.actions[0].job_id, self.job_id)
        self.assertTrue(assistant.actions[0].requires_confirmation)
        self.assertNotIn("submit", {action.kind for action in assistant.actions})
        persisted = load_copilot_thread(self.thread_path)
        self.assertEqual(len(persisted.messages), 2)

    def test_safe_fill_reply_preserves_no_submit_boundary(self) -> None:
        snapshot = respond_to_copilot(
            f"帮我代填 #{self.job_id}",
            thread_path=self.thread_path,
            repository=self.repository,
            profile_path=self.profile_path,
            runtime_config_path=self.runtime_path,
            usage_path=self.usage_path,
        )
        assistant = snapshot.thread.messages[-1]

        self.assertIn("最终提交", assistant.content)
        self.assertEqual(assistant.actions[0].kind, "start_safe_fill")
        self.assertTrue(assistant.actions[0].requires_confirmation)

    def test_local_chat_turns_resume_revision_request_into_confirmable_action(self) -> None:
        instruction = f"把 #{self.job_id} 的简历按 JD 润色，突出 SQL 和数据看板"
        snapshot = respond_to_copilot(
            instruction,
            thread_path=self.thread_path,
            repository=self.repository,
            profile_path=self.profile_path,
            runtime_config_path=self.runtime_path,
            usage_path=self.usage_path,
        )
        assistant = snapshot.thread.messages[-1]

        self.assertIn("直接生成新版", assistant.content)
        self.assertEqual(assistant.actions[0].kind, "revise_resume")
        self.assertEqual(assistant.actions[0].job_id, self.job_id)
        self.assertEqual(assistant.actions[0].instruction, instruction)
        self.assertTrue(assistant.actions[0].requires_confirmation)

    def test_reset_starts_a_new_thread_and_validation_rejects_empty_message(self) -> None:
        first = reset_copilot_thread(self.thread_path)
        second = reset_copilot_thread(self.thread_path)
        self.assertNotEqual(first.thread_id, second.thread_id)
        with self.assertRaises(CopilotChatError):
            respond_to_copilot(
                "   ",
                thread_path=self.thread_path,
                repository=self.repository,
                profile_path=self.profile_path,
                runtime_config_path=self.runtime_path,
                usage_path=self.usage_path,
            )

    def test_numbers_in_message_are_not_misread_as_job_ids(self) -> None:
        snapshot = respond_to_copilot(
            "75 分以上的岗位为什么值得投？",
            thread_path=self.thread_path,
            repository=self.repository,
            profile_path=self.profile_path,
            runtime_config_path=self.runtime_path,
            usage_path=self.usage_path,
        )
        assistant = snapshot.thread.messages[-1]

        self.assertIn(f"#{self.job_id}", assistant.content)
        self.assertNotIn("#75 ", assistant.content)

    def test_why_intent_uses_match_insight(self) -> None:
        context = build_safe_workspace_context(
            self.repository,
            profile_path=self.profile_path,
        )
        self.assertIn("match_insight", context["top_jobs"][0])

        snapshot = respond_to_copilot(
            f"为什么推荐 #{self.job_id}？",
            thread_path=self.thread_path,
            repository=self.repository,
            profile_path=self.profile_path,
            runtime_config_path=self.runtime_path,
            usage_path=self.usage_path,
        )
        assistant = snapshot.thread.messages[-1]

        self.assertIn(str(self.job_id), assistant.content)
        self.assertNotIn("还没有匹配报告", assistant.content)

    def test_suggestions_are_contextual_and_reference_top_job(self) -> None:
        snapshot = respond_to_copilot(
            "今天做什么？",
            thread_path=self.thread_path,
            repository=self.repository,
            profile_path=self.profile_path,
            runtime_config_path=self.runtime_path,
            usage_path=self.usage_path,
        )

        self.assertTrue(snapshot.suggestions)
        self.assertTrue(any(f"#{self.job_id}" in item for item in snapshot.suggestions))
        self.assertLessEqual(len(snapshot.suggestions), 4)


if __name__ == "__main__":
    unittest.main()
