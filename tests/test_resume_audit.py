from __future__ import annotations

import json
import unittest
from pathlib import Path

from job_agent.services.resume_audit import audit_resume
from job_agent.services.resume_reader import ResumeDocument


class ResumeAuditTests(unittest.TestCase):
    def test_audit_finds_actionable_issues_without_leaking_contact_values(self) -> None:
        document = ResumeDocument(
            path=Path("待完善简历.txt"),
            sha256="A" * 64,
            text="""
姓名：小王
邮箱：test@example.com
手机：13800000000
教育经历
示例大学 市场营销 本科
自我评价
本人认真负责，沟通能力强，具备较强的团队合作精神。
实习经历
- 被安排参与了社群运营，协助完成日常工作。
- 负责了内容运营并形成闭环，取得了良好的效果。
""".strip(),
        )

        report = audit_resume(document)
        codes = {item.code for item in report.findings}
        serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, default=str)

        self.assertIn("integrity.placeholder", codes)
        self.assertIn("structure.missing_sections", codes)
        self.assertIn("evidence.low_quantification", codes)
        self.assertIn("language.generic_claims", codes)
        self.assertIn("language.weak_voice", codes)
        self.assertIn("language.aiish_phrasing", codes)
        self.assertNotIn("test@example.com", serialized)
        self.assertNotIn("13800000000", serialized)
        self.assertIn("不要为了分数编造", report.safety_note)
        self.assertTrue(
            any("不要编造数字" in item.recommendation for item in report.findings)
        )

    def test_evidence_based_resume_scores_higher(self) -> None:
        strong = ResumeDocument(
            path=Path("已核验简历.md"),
            sha256="B" * 64,
            text="""
姓名：小王
邮箱：candidate@example.com
手机：13912345678
教育经历
示例大学 信息管理 本科
实习经历
- 运营 3 个校园社群，每周整理 2 次用户反馈，累计形成 12 份记录。
- 分析 500 条问卷数据，交付 1 份结论报告和 6 项可执行建议。
项目经历
- 设计报名流程并协调 5 名同学完成 4 场活动，覆盖 200 人。
专业技能
Excel、SQL、Python
""".strip(),
        )
        weak = ResumeDocument(
            path=Path("空泛简历.md"),
            sha256="C" * 64,
            text="""
姓名：小王
自我评价
认真负责，学习能力强，沟通能力强。
实习经历
- 参与了运营工作，协助完成各项任务。
""".strip(),
        )

        strong_report = audit_resume(strong)
        weak_report = audit_resume(weak)

        self.assertGreater(strong_report.score, weak_report.score)
        self.assertGreaterEqual(strong_report.metrics.quantified_line_ratio, 0.5)
        self.assertTrue(any("可核验" in item for item in strong_report.strengths))


if __name__ == "__main__":
    unittest.main()
