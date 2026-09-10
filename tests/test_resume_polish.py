from __future__ import annotations

import json
import re
import unittest
from unittest import mock

from job_agent.services.ai_errors import safe_ai_error_message
from job_agent.services.resume_polish import (
    ResumePolishError,
    polish_resume_content_locally,
    polish_resume_content_with_jd,
)
from job_agent.services.resume_editor import validate_resume_content
from job_agent.services.runtime_config import AIConnectorConfig, RuntimeConfig


def _content() -> dict[str, object]:
    return {
        "template_id": "portable-evidence-resume-v1",
        "person": {
            "name": "测试用户",
            "phone": "13800000000",
            "email": "test@example.com",
            "city": "上海",
            "photo_path": "",
        },
        "summary": "数据运营实习生，熟悉 Excel 与 SQL。",
        "skills": ["Excel", "SQL", "数据分析"],
        "experience_sections": [
            {
                "title": "实习经历",
                "entries": [
                    {
                        "organization": "某公司",
                        "role": "运营实习生",
                        "dates": "2026-01 ~ 2026-03",
                        "bullets": [
                            {"label": "结果", "text": "优化投放流程，效率提升 30%。"},
                            {"label": "动作", "text": "搭建 5 张数据看板支持周会决策。"},
                        ],
                    }
                ],
            }
        ],
        "education": [{"institution": "某大学", "major": "信息管理", "degree": "本科", "end": "2027-06"}],
        "target": {"job_id": 1, "company": "示例公司", "role": "数据运营", "location": "上海"},
    }


def _ai_payload(summary: str, skills: list[str], bullets: list[dict[str, str]]) -> str:
    return json.dumps({"summary": summary, "skills": skills, "bullets": bullets}, ensure_ascii=False)


JD = "负责短视频内容数据分析和 A/B 实验设计，精通 SQL 与数据看板，提升投放 ROI。"


class ResumePolishTests(unittest.TestCase):
    def test_cloud_error_message_hides_provider_payload_and_explains_401(self) -> None:
        class UnauthorizedError(RuntimeError):
            status_code = 401

        secret = "test-secret-never-render"
        message = safe_ai_error_message(
            UnauthorizedError(f"Incorrect API key provided: {secret}"),
            provider="openai",
            action="简历润色",
        )
        self.assertIn("身份验证失败（401）", message)
        self.assertNotIn(secret, message)

    def test_self_evaluation_is_a_reviewable_jd_polish_change(self):
        original = _content()
        original["self_evaluation"] = "有数据运营实践，习惯结合数据开展复盘。"
        parsed = {"summary": original["summary"], "skills": original["skills"],
                  "self_evaluation": "结合数据运营实践分析问题，并通过复盘改进工作方式。",
                  "bullets": [{"bullet_id": f"s0e0b{i}", "text": b["text"]} for i, b in enumerate(original["experience_sections"][0]["entries"][0]["bullets"])]}
        with mock.patch("job_agent.services.resume_polish._invoke_ai", return_value=(json.dumps(parsed), 1, 1)) as invoke:
            suggestion = polish_resume_content_with_jd(original, JD, config=self.config)
        self.assertEqual(suggestion.content["self_evaluation"], parsed["self_evaluation"])
        self.assertEqual(suggestion.changes[0]["field"], "self_evaluation")
        self.assertEqual(json.loads(invoke.call_args.args[1])["resume"]["self_evaluation"], original["self_evaluation"])
        parsed["self_evaluation"] += "效率提升100%。"
        with mock.patch("job_agent.services.resume_polish._invoke_ai", return_value=(json.dumps(parsed), 1, 1)):
            with self.assertRaises(ResumePolishError):
                polish_resume_content_with_jd(original, JD, config=self.config)

    def setUp(self) -> None:
        self.config = RuntimeConfig(
            ai=AIConnectorConfig(
                provider="openai_compatible",
                model="test-model",
                base_url="http://127.0.0.1:9",
                api_key="test-key",
            )
        )

    def test_local_provider_rejected_before_any_call(self) -> None:
        with mock.patch("job_agent.services.resume_polish._invoke_ai") as invoke:
            with self.assertRaises(ResumePolishError) as raised:
                polish_resume_content_with_jd(_content(), JD, config=RuntimeConfig())
        invoke.assert_not_called()
        self.assertIn("云端 AI", str(raised.exception))

    def test_polish_rewrites_wording_and_preserves_structure_and_numbers(self) -> None:
        original = _content()
        ai_text = _ai_payload(
            "短视频内容数据分析方向实习生，熟练运用 SQL 与数据看板。",
            ["SQL", "数据看板", "A/B 实验", "数据分析"],
            [
                {"bullet_id": "s0e0b0", "text": "优化投放流程与 A/B 实验迭代，效率提升 30%。"},
                {"bullet_id": "s0e0b1", "text": "搭建 5 张 SQL 数据看板支持周会决策。"},
            ],
        )
        with mock.patch(
            "job_agent.services.resume_polish._invoke_ai",
            return_value=(ai_text, 100, 200),
        ) as invoke:
            suggestion = polish_resume_content_with_jd(original, JD, config=self.config)
        invoke.assert_called_once()

        self.assertEqual(suggestion.input_tokens, 100)
        self.assertEqual(suggestion.output_tokens, 200)
        # 结构与不可润色字段保持不变
        merged = suggestion.content
        self.assertEqual(merged["person"], original["person"])
        self.assertEqual(merged["education"], original["education"])
        self.assertEqual(merged["target"], original["target"])
        self.assertEqual(merged["experience_sections"][0]["entries"][0]["organization"], "某公司")
        self.assertEqual(merged["experience_sections"][0]["entries"][0]["dates"], "2026-01 ~ 2026-03")
        # 数字保留
        self.assertIn("30%", merged["experience_sections"][0]["entries"][0]["bullets"][0]["text"])
        self.assertIn("5 张", merged["experience_sections"][0]["entries"][0]["bullets"][1]["text"])
        # 原对象未被修改
        self.assertEqual(original["summary"], "数据运营实习生，熟悉 Excel 与 SQL。")
        # 变更记录覆盖摘要/技能/两条要点
        fields = {change["field"] for change in suggestion.changes}
        self.assertEqual(fields, {"summary", "skills", "bullet"})
        self.assertEqual(len(suggestion.changes), 4)
        # 合并结果通过结构校验
        validate_resume_content(merged)

    def test_polish_passes_user_instruction_to_the_model_payload(self) -> None:
        original = _content()
        ai_text = _ai_payload(
            original["summary"],  # type: ignore[arg-type]
            original["skills"],  # type: ignore[arg-type]
            [
                {"bullet_id": "s0e0b0", "text": "优化投放流程，效率提升 30%。"},
                {"bullet_id": "s0e0b1", "text": "搭建 5 张数据看板支持周会决策。"},
            ],
        )
        with mock.patch(
            "job_agent.services.resume_polish._invoke_ai",
            return_value=(ai_text, 10, 20),
        ) as invoke:
            polish_resume_content_with_jd(
                original,
                JD,
                config=self.config,
                user_instruction="突出 SQL 与实验设计，保持克制",
            )

        payload = json.loads(invoke.call_args.args[1])
        self.assertEqual(payload["user_instruction"], "突出 SQL 与实验设计，保持克制")

    def test_polish_rejects_dropped_numbers(self) -> None:
        ai_text = _ai_payload(
            "数据分析实习生。",
            ["SQL"],
            [
                {"bullet_id": "s0e0b0", "text": "优化投放流程，效率显著提升。"},
                {"bullet_id": "s0e0b1", "text": "搭建 5 张数据看板支持周会决策。"},
            ],
        )
        with mock.patch(
            "job_agent.services.resume_polish._invoke_ai",
            return_value=(ai_text, 10, 20),
        ):
            with self.assertRaises(ResumePolishError) as raised:
                polish_resume_content_with_jd(_content(), JD, config=self.config)
        self.assertIn("量化数字", str(raised.exception))

    def test_polish_rejects_invented_numbers(self) -> None:
        ai_text = _ai_payload(
            "数据分析实习生。",
            ["SQL"],
            [
                {"bullet_id": "s0e0b0", "text": "优化投放流程，效率提升 30%，覆盖 200 个项目。"},
                {"bullet_id": "s0e0b1", "text": "搭建 5 张数据看板支持周会决策。"},
            ],
        )
        with mock.patch(
            "job_agent.services.resume_polish._invoke_ai",
            return_value=(ai_text, 10, 20),
        ):
            with self.assertRaises(ResumePolishError):
                polish_resume_content_with_jd(_content(), JD, config=self.config)

    def test_polish_rejects_bullet_id_mismatch(self) -> None:
        ai_text = _ai_payload(
            "数据分析实习生。",
            ["SQL"],
            [{"bullet_id": "s0e0b0", "text": "优化投放流程，效率提升 30%。"}],
        )
        with mock.patch(
            "job_agent.services.resume_polish._invoke_ai",
            return_value=(ai_text, 10, 20),
        ):
            with self.assertRaises(ResumePolishError) as raised:
                polish_resume_content_with_jd(_content(), JD, config=self.config)
        self.assertIn("数量不一致", str(raised.exception))

    def test_polish_rejects_invalid_model_json(self) -> None:
        for raw in ("不是 JSON", "```json\n{\"summary\": 1}\n```", '{"skills": []}'):
            with mock.patch(
                "job_agent.services.resume_polish._invoke_ai",
                return_value=(raw, 10, 20),
            ):
                with self.assertRaises(ResumePolishError):
                    polish_resume_content_with_jd(_content(), JD, config=self.config)

    def test_polish_rejects_empty_jd(self) -> None:
        with self.assertRaises(ResumePolishError) as raised:
            polish_resume_content_with_jd(_content(), "   ", config=self.config)
        self.assertIn("JD", str(raised.exception))

    def test_local_fallback_tailors_without_inventing_facts_or_numbers(self) -> None:
        original = _content()
        suggestion = polish_resume_content_locally(
            original,
            JD,
            user_instruction="突出 SQL 与数据分析，使用 STAR 法则",
        )
        merged = suggestion.content
        self.assertEqual(merged["skills"][0], "SQL")
        self.assertIn("数据运营", merged["summary"])
        self.assertEqual(merged["experience_sections"][0]["title"], "工作经历")
        bullets = merged["experience_sections"][0]["entries"][0]["bullets"]
        self.assertEqual({bullet["label"] for bullet in bullets}, {"工作业绩"})
        self.assertEqual(
            [set(re.findall(r"\d+(?:\.\d+)?", bullet["text"])) for bullet in bullets],
            [{"30"}, {"5"}],
        )
        self.assertEqual(original["summary"], "数据运营实习生，熟悉 Excel 与 SQL。")


if __name__ == "__main__":
    unittest.main()
