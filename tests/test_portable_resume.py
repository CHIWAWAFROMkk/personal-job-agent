import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from pypdf import PdfReader

from job_agent.models.job_record import JobRecordInput
from job_agent.services.application_pack import (
    build_application_pack,
    resolve_resume_bundle,
)
from job_agent.services.job_repository import JobRepository
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.portable_resume import (
    PortableResumeError,
    build_portable_resume_draft,
    find_latest_resume_manifest,
    find_profile_photo,
    resume_artifact_from_manifest,
)
from job_agent.services.tailored_resume import approve_resume_visual_review
from tests.helpers import sample_profile


JD = "负责 SQL、Tableau 数据分析、周报与运营协作；要求本科，每周到岗四天。"

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class PortableResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.profile = sample_profile()
        repository = JobRepository(self.root / "jobs.sqlite3")
        record = repository.upsert_job(
            JobRecordInput(
                company="示例数据",
                title="数据运营实习生",
                jd_text=JD,
                source="test",
                source_url="https://example.com/job/1",
                location="上海",
            )
        )
        self.job = repository.get_job(record.job_id)
        structured = structure_job_locally(
            JD,
            company=self.job.company,
            title=self.job.title,
            location=self.job.location,
            source="test",
            source_url="https://example.com/job/1",
        )
        self.result = match_job_locally(self.profile, structured)
        self.applications_dir = self.root / "applications"
        self.output = self.applications_dir / "job-1-draft"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_builds_single_page_evidence_locked_docx_and_pdf(self) -> None:
        files = build_portable_resume_draft(
            self.profile,
            self.job,
            self.result,
            self.output,
        )
        content = json.loads(files.content_json.read_text(encoding="utf-8"))
        manifest = json.loads(files.manifest.read_text(encoding="utf-8"))
        fact_ids = content["truthfulness"]["confirmed_fact_ids"]
        source_statements = {
            fact.id: fact.statement
            for experience in self.profile.experiences
            for fact in experience.facts
            if self.profile.is_application_ready(fact.status)
        }
        rendered_bullets = [
            bullet
            for section in content["experience_sections"]
            for entry in section["entries"]
            for bullet in entry["bullets"]
        ]

        self.assertTrue(files.docx.is_file())
        self.assertTrue(files.pdf.is_file())
        self.assertEqual(len(PdfReader(str(files.pdf)).pages), 1)
        self.assertEqual(
            [bullet["text"] for bullet in rendered_bullets],
            [source_statements[fact_id] for fact_id in fact_ids],
        )
        self.assertNotIn("独立搭建 Tableau", files.content_json.read_text(encoding="utf-8"))
        self.assertNotIn("Tableau", content["summary"])
        # 摘要必须是可投递的求职语言，不得包含内部流程提示
        self.assertNotIn("需由本人", content["summary"])
        self.assertNotIn("本地事实", content["summary"])
        self.assertIn("在读", content["summary"])
        self.assertIn("积累实践经验", content["summary"])
        # 空话套话不得进入摘要
        self.assertNotIn("期望把上述能力", content["summary"])
        # 匹配分属内部排序数据，不得出现在投递简历正文
        from docx import Document

        document = Document(str(files.docx))
        docx_text = "\n".join(p.text for p in document.paragraphs) + "\n" + "\n".join(
            p.text for table in document.tables for row in table.rows for cell in row.cells for p in cell.paragraphs
        )
        self.assertNotIn("匹配", docx_text)
        self.assertNotIn("/100", docx_text)
        self.assertNotIn("match_score", json.dumps(content, ensure_ascii=False))
        self.assertNotIn("match_score", json.dumps(manifest, ensure_ascii=False))
        section_titles = [section["title"] for section in content["experience_sections"]]
        self.assertTrue(section_titles)
        self.assertTrue(set(section_titles) <= {"实习经历", "工作经历", "项目经历"})
        labels = {bullet["label"] for bullet in rendered_bullets}
        self.assertTrue(labels <= {"工作内容", "工作业绩", "项目内容", "项目业绩"})
        self.assertEqual(
            manifest["qa"]["star_bullets"],
            sum(1 for bullet in rendered_bullets if bullet["label"].endswith("业绩")),
        )
        # 求职意向行呈现角色与期望城市（目标公司不出现在简历上）
        self.assertIn(f"求职方向：{self.job.title}", docx_text)
        self.assertNotIn("期望城市", docx_text)

    def test_finds_photo_in_assets_fallback_dir(self) -> None:
        # 历史/手动落点：photo/ 不存在时应能找到 assets/ 里的照片
        assets_dir = self.root / "assets"
        assets_dir.mkdir()
        photo = assets_dir / "profile-photo.png"
        photo.write_bytes(_PNG_1PX)
        self.assertEqual(find_profile_photo(self.root), photo)
        # photo/ 存在时优先于 assets/
        photo_dir = self.root / "photo"
        photo_dir.mkdir()
        preferred = photo_dir / "profile-photo.jpg"
        preferred.write_bytes(_PNG_1PX)
        self.assertEqual(find_profile_photo(self.root), preferred)

    def test_embeds_profile_photo_and_reports_in_manifest(self) -> None:
        photo_dir = self.root / "photo"
        photo_dir.mkdir()
        photo = photo_dir / "profile-photo.png"
        photo.write_bytes(_PNG_1PX)

        self.assertEqual(find_profile_photo(self.root), photo)
        files = build_portable_resume_draft(
            self.profile,
            self.job,
            self.result,
            self.output,
            photo_path=photo,
        )
        content = json.loads(files.content_json.read_text(encoding="utf-8"))
        manifest = json.loads(files.manifest.read_text(encoding="utf-8"))

        self.assertEqual(content["person"]["photo_path"], str(photo.resolve()))
        self.assertEqual(manifest["qa"]["embedded_photos"], 1)
        self.assertEqual(manifest["qa"]["photo_requirement"], "embedded")
        self.assertEqual(len(PdfReader(str(files.pdf)).pages), 1)
        with zipfile.ZipFile(files.docx) as archive:
            media = [name for name in archive.namelist() if name.startswith("word/media/")]
        self.assertEqual(len(media), 1)

    def test_rejects_photo_that_is_missing_or_wrong_type(self) -> None:
        with self.assertRaises(PortableResumeError):
            build_portable_resume_draft(
                self.profile,
                self.job,
                self.result,
                self.output,
                photo_path=self.root / "missing.png",
            )
        bad = self.root / "photo.txt"
        bad.write_text("not a photo", encoding="utf-8")
        with self.assertRaises(PortableResumeError):
            build_portable_resume_draft(
                self.profile,
                self.job,
                self.result,
                self.output,
                photo_path=bad,
            )

    def test_requires_visual_approval_before_application_pack_is_ready(self) -> None:
        files = build_portable_resume_draft(
            self.profile,
            self.job,
            self.result,
            self.output,
        )
        draft = resolve_resume_bundle(
            self.profile,
            self.job,
            self.applications_dir,
        )
        self.assertEqual(draft.status, "needs_review")

        approved = approve_resume_visual_review(files.manifest)
        ready = resolve_resume_bundle(
            self.profile,
            self.job,
            self.applications_dir,
        )
        pack = build_application_pack(self.profile, self.job, self.result, ready)

        self.assertTrue(approved.is_file())
        self.assertEqual(ready.status, "ready")
        self.assertTrue(ready.qa_verified)
        self.assertEqual(pack.resume.status, "ready")
        self.assertEqual(
            find_latest_resume_manifest(self.applications_dir, self.job.job_id),
            approved,
        )
        self.assertEqual(
            resume_artifact_from_manifest(approved, "pdf"),
            files.pdf,
        )


if __name__ == "__main__":
    unittest.main()
