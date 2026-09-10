from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from job_agent.models.job_record import JobDetail, JobSourceItem
from job_agent.models.profile import ClaimStatus, EvidenceFact, Experience, ExperienceKind
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.tailored_resume import (
    ResumeTemplateSource,
    approve_resume_visual_review,
    generate_tailored_resume_content,
)
from tests.helpers import sample_profile


JD = """
公司：示例科技
岗位：AI 数据运营实习生
地点：上海
岗位职责：负责数据分析、流程推进、AI 工具提效和跨部门协作。
任职要求：本科在读，熟练 Excel 和 SQL，每周至少 4 天，连续实习 3 个月。
""".strip()


def _fact(fact_id: str, statement: str, *skills: str) -> EvidenceFact:
    return EvidenceFact(
        id=fact_id,
        statement=statement,
        skills=list(skills),
        status=ClaimStatus.DOCUMENTED,
        source_ids=["resume-test"],
    )


class TailoredResumeTests(unittest.TestCase):
    def _profile(self):
        profile = sample_profile()
        profile.experiences[0].facts[1].status = ClaimStatus.DOCUMENTED
        profile.experiences.extend(
            [
                Experience(
                    id="exp-internship-two",
                    kind=ExperienceKind.INTERNSHIP,
                    organization="第二示例公司",
                    role="运营实习生",
                    start="2026-01",
                    end="2026-04",
                    facts=[
                        _fact("fact-two-analysis", "使用 Excel 输出业务分析报表。", "Excel"),
                        _fact("fact-two-process", "跟进业务流程并协调跨部门信息。", "流程推进"),
                        _fact("fact-two-ai", "使用 AI 工具完成信息整理。", "AI工具"),
                    ],
                ),
                Experience(
                    id="project-data",
                    kind=ExperienceKind.PROJECT,
                    organization="示例大学",
                    role="数据项目组长",
                    start="2025-01",
                    end="2025-05",
                    facts=[
                        _fact("fact-project-sql", "使用 SQL 完成数据存储与查询。", "SQL"),
                        _fact("fact-project-result", "完成项目展示并获得 95 分。", "成果展示"),
                    ],
                ),
                Experience(
                    id="project-ai",
                    kind=ExperienceKind.COMPETITION,
                    organization="示例大学",
                    role="AI 调研项目组长",
                    start="2024-03",
                    end="2024-07",
                    facts=[
                        _fact("fact-ai-research", "完成 AI 应用场景调研与问卷分析。", "AI工具"),
                        _fact("fact-ai-report", "完成研究报告与 PPT 成果展示。", "报告撰写"),
                    ],
                ),
            ]
        )
        return profile

    def test_content_has_fixed_template_shape_and_only_ready_facts(self) -> None:
        profile = self._profile()
        structured = structure_job_locally(
            JD,
            company="示例科技",
            title="AI 数据运营实习生",
            location="上海",
        )
        result = match_job_locally(profile, structured)
        now = "2026-08-18T00:00:00+00:00"
        job = JobDetail(
            job_id=7,
            company="示例科技",
            title="AI 数据运营实习生",
            location="上海",
            jd_text=JD,
            status="discovered",
            sources=[
                JobSourceItem(
                    platform="manual",
                    source_url="https://example.com/job/7",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            ],
            created_at=now,
            updated_at=now,
            first_seen_at=now,
            last_seen_at=now,
        )
        template = ResumeTemplateSource(
            reference_docx=Path("reference.docx"),
            reference_sha256="0" * 64,
            base_content_path=Path("base.json"),
            base_content={
                "person": {
                    "name": profile.person.display_name,
                    "city": "上海",
                    "phone": "10000000000",
                    "email": "test@example.com",
                    "education_status": "本科在读",
                    "photo_path": "data/private/assets/profile-photo.png",
                    "photo_alt": "个人照片",
                }
            },
            manifest_path=Path("manifest.json"),
        )

        content = generate_tailored_resume_content(
            profile,
            job,
            result,
            template,
            generated_at=datetime(2026, 8, 18, tzinfo=UTC),
        )

        self.assertEqual(len(content["skills"]), 4)
        self.assertEqual(len(content["experience_sections"]), 2)
        entries = [
            entry
            for section in content["experience_sections"]
            for entry in section["entries"]
        ]
        self.assertEqual(len(entries), 4)
        self.assertEqual([len(entry["bullets"]) for entry in entries], [3, 2, 2, 2])
        used_ids = {
            fact_id
            for entry in entries
            for bullet in entry["bullets"]
            for fact_id in bullet["fact_ids"]
        }
        ready_ids = {
            fact.id
            for experience in profile.experiences
            for fact in experience.facts
            if profile.is_application_ready(fact.status)
        }
        self.assertTrue(used_ids)
        self.assertLessEqual(used_ids, ready_ids)
        self.assertEqual(content["target"]["role"], job.title)

    def test_visual_approval_creates_new_manifest_and_checks_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx = root / "resume.docx"
            pdf = root / "resume.pdf"
            docx.write_bytes(b"docx")
            pdf.write_bytes(b"pdf")
            manifest_path = root / "resume-version-auto-star.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "artifacts": {
                            "docx": {
                                "path": docx.name,
                                "sha256": hashlib.sha256(b"docx").hexdigest().upper(),
                            },
                            "pdf": {
                                "path": pdf.name,
                                "sha256": hashlib.sha256(b"pdf").hexdigest().upper(),
                            },
                        },
                        "qa": {"pdf_visual_review": "pending_user_review"},
                    }
                ),
                encoding="utf-8",
            )

            approved = approve_resume_visual_review(manifest_path)

            payload = json.loads(approved.read_text(encoding="utf-8"))
            self.assertNotEqual(approved, manifest_path)
            self.assertEqual(payload["qa"]["pdf_visual_review"], "passed")
            self.assertEqual(payload["visual_approval"]["approved_by"], "user")


if __name__ == "__main__":
    unittest.main()
