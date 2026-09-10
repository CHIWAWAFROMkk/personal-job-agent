from __future__ import annotations

import json
import unittest
from unittest import mock

from job_agent.services.resume_import import (
    ResumeImportError,
    import_resume_text_into_draft,
)
from job_agent.services.runtime_config import AIConnectorConfig, RuntimeConfig


def _content() -> dict[str, object]:
    return {
        "template_id": "portable-evidence-resume-v1",
        "schema_version": "1.0",
        "resume_version_id": "resume-test-job-1-portable-v1",
        "person": {
            "name": "测试用户",
            "phone": "13800000000",
            "email": "test@example.com",
            "city": "上海",
            "photo_path": "C:/private/photo.png",
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


RESUME_TEXT = (
    "李测试，13811112222，litest@example.com，上海。\n"
    "个人简介：数据运营方向，擅长 SQL 与看板。\n"
    "技能：SQL、Python、数据看板、A/B 测试。\n"
    "实习经历：甲公司 运营实习生 2025.06-2025.09：负责投放优化，效率提升 25%；搭建 6 张看板。\n"
    "教育：某大学 信息管理 本科 2023-2027。"
)


def _ai_payload(**overrides: object) -> str:
    payload: dict[str, object] = {
        "person": {
            "name": "李测试",
            "phone": "13811112222",
            "email": "litest@example.com",
            "city": "上海",
            "linkedin": "",
            "website": "",
        },
        "summary": "数据运营方向实习生，擅长 SQL 与数据看板。",
        "skills": ["SQL", "Python", "数据看板", "A/B 测试"],
        "experience_sections": [
            {
                "title": "实习经历",
                "entries": [
                    {
                        "organization": "甲公司",
                        "role": "运营实习生",
                        "dates": "2025.06-2025.09",
                        "bullets": [
                            {"label": "结果", "text": "负责投放优化，效率提升 25%。"},
                            {"label": "动作", "text": "搭建 6 张看板。"},
                        ],
                    }
                ],
            }
        ],
        "education": [
            {
                "institution": "某大学",
                "degree": "本科",
                "major": "信息管理",
                "start": "2023",
                "end": "2027",
            }
        ],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class ResumeImportTests(unittest.TestCase):
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
        with mock.patch("job_agent.services.resume_import._invoke_ai") as invoke:
            with self.assertRaises(ResumeImportError) as raised:
                import_resume_text_into_draft(RESUME_TEXT, _content(), config=RuntimeConfig())
        invoke.assert_not_called()
        self.assertIn("云端 AI", str(raised.exception))

    def test_import_parses_text_and_preserves_local_fields(self) -> None:
        original = _content()
        with mock.patch(
            "job_agent.services.resume_import._invoke_ai",
            return_value=(_ai_payload(), 300, 500),
        ) as invoke:
            suggestion = import_resume_text_into_draft(
                RESUME_TEXT, original, config=self.config
            )
        invoke.assert_called_once()
        self.assertEqual(suggestion.input_tokens, 300)
        self.assertEqual(suggestion.output_tokens, 500)

        merged = suggestion.content
        # 岗位绑定、模板、证件照路径沿用本地原值，不接受 AI 输出
        self.assertEqual(merged["target"], original["target"])
        self.assertEqual(merged["template_id"], "portable-evidence-resume-v1")
        self.assertEqual(
            merged["person"]["photo_path"], original["person"]["photo_path"]
        )
        # AI 解析出的人物信息生效
        self.assertEqual(merged["person"]["name"], "李测试")
        self.assertEqual(merged["person"]["phone"], "13811112222")
        # 量化数字原样来自原文
        bullets = merged["experience_sections"][0]["entries"][0]["bullets"]
        self.assertIn("25%", bullets[0]["text"])
        self.assertIn("6 张", bullets[1]["text"])
        # 教育来自原文
        self.assertEqual(merged["education"][0]["institution"], "某大学")
        self.assertEqual(suggestion.warnings, [])

    def test_import_rejects_invented_numbers(self) -> None:
        invented = _ai_payload()
        parsed = json.loads(invented)
        parsed["experience_sections"][0]["entries"][0]["bullets"][0]["text"] = (
            "负责投放优化，效率提升 99%。"
        )
        with mock.patch(
            "job_agent.services.resume_import._invoke_ai",
            return_value=(json.dumps(parsed, ensure_ascii=False), 300, 500),
        ):
            with self.assertRaises(ResumeImportError) as raised:
                import_resume_text_into_draft(
                    RESUME_TEXT, _content(), config=self.config
                )
        self.assertIn("原文没有的数字", str(raised.exception))

    def test_import_rejects_invalid_model_json(self) -> None:
        with mock.patch(
            "job_agent.services.resume_import._invoke_ai",
            return_value=("这不是 JSON", 10, 10),
        ):
            with self.assertRaises(ResumeImportError) as raised:
                import_resume_text_into_draft(
                    RESUME_TEXT, _content(), config=self.config
                )
        self.assertIn("合法 JSON", str(raised.exception))

    def test_import_rejects_empty_text(self) -> None:
        with mock.patch("job_agent.services.resume_import._invoke_ai") as invoke:
            with self.assertRaises(ResumeImportError) as raised:
                import_resume_text_into_draft("   \n ", _content(), config=self.config)
        invoke.assert_not_called()
        self.assertIn("为空", str(raised.exception))

    def test_import_falls_back_to_current_sections_when_ai_returns_none(self) -> None:
        with mock.patch(
            "job_agent.services.resume_import._invoke_ai",
            return_value=(
                json.dumps(
                    {"person": {}, "summary": "", "skills": [], "experience_sections": [], "education": []},
                    ensure_ascii=False,
                ),
                10,
                10,
            ),
        ):
            suggestion = import_resume_text_into_draft(
                RESUME_TEXT, _content(), config=self.config
            )
        merged = suggestion.content
        # 空解析结果回退到当前草稿，保证结构合法可保存
        self.assertEqual(merged["summary"], "数据运营实习生，熟悉 Excel 与 SQL。")
        self.assertEqual(
            merged["experience_sections"], _content()["experience_sections"]
        )
        self.assertEqual(merged["person"]["name"], "测试用户")
        self.assertTrue(suggestion.warnings)

    def test_import_rejects_invalid_current_content(self) -> None:
        broken = {"template_id": "other-template"}
        with mock.patch("job_agent.services.resume_import._invoke_ai") as invoke:
            with self.assertRaises(ResumeImportError):
                import_resume_text_into_draft(RESUME_TEXT, broken, config=self.config)
        invoke.assert_not_called()

    def test_import_number_normalization_matches_decimal_variants(self) -> None:
        # 原文 25%，AI 输出 25.0% —— 浮点归一化后视为同一数字，不误杀
        payload = _ai_payload()
        parsed = json.loads(payload)
        parsed["experience_sections"][0]["entries"][0]["bullets"][0]["text"] = (
            "负责投放优化，效率提升 25.0%。"
        )
        with mock.patch(
            "job_agent.services.resume_import._invoke_ai",
            return_value=(json.dumps(parsed, ensure_ascii=False), 10, 10),
        ):
            suggestion = import_resume_text_into_draft(
                RESUME_TEXT, _content(), config=self.config
            )
        self.assertIn(
            "25.0%",
            suggestion.content["experience_sections"][0]["entries"][0]["bullets"][0]["text"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
