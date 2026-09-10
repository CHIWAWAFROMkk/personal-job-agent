import json
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader

from job_agent.models.job_record import JobRecordInput
from job_agent.services.application_pack import resolve_resume_bundle
from job_agent.services.job_repository import JobRepository
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.portable_resume import (
    build_portable_resume_draft,
    find_latest_resume_manifest,
)
from job_agent.services.resume_editor import (
    ResumeEditorError,
    load_latest_resume_content,
    rerender_edited_resume,
    validate_resume_content,
)
from job_agent.services.tailored_resume import approve_resume_visual_review
from tests.helpers import sample_profile


JD = "负责 SQL、Tableau 数据分析、周报与运营协作；要求本科，每周到岗四天。"


class ResumeEditorTests(unittest.TestCase):
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
        self.draft_dir = self.applications_dir / "job-1-draft"
        self.files = build_portable_resume_draft(
            self.profile,
            self.job,
            self.result,
            self.draft_dir,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_returns_editable_content_and_manifest(self) -> None:
        content, manifest_path = load_latest_resume_content(
            self.applications_dir, self.job.job_id
        )
        self.assertEqual(content["template_id"], "mono-photo")
        self.assertEqual(manifest_path, self.files.manifest)

    def test_load_rejects_job_without_draft(self) -> None:
        with self.assertRaises(ResumeEditorError):
            load_latest_resume_content(self.applications_dir, 999)

    def test_rerender_edited_resume_creates_pending_review_version(self) -> None:
        content, manifest_path = load_latest_resume_content(
            self.applications_dir, self.job.job_id
        )
        content["summary"] = "用户改写的个人摘要：聚焦数据运营与协同推进。"
        content["skills"] = [*content["skills"][:3], "新技能"]
        first_section = content["experience_sections"][0]
        first_section["entries"][0]["bullets"][0]["text"] = "用户改写后的第一条要点。"

        edited = rerender_edited_resume(
            content,
            self.applications_dir,
            based_on=str(manifest_path),
        )

        manifest = json.loads(edited.manifest.read_text(encoding="utf-8"))
        self.assertTrue(manifest["edited_by_user"])
        self.assertEqual(manifest["based_on"], str(manifest_path))
        self.assertTrue(manifest["qa"]["user_edited"])
        self.assertEqual(manifest["qa"]["pdf_visual_review"], "pending_user_review")
        self.assertEqual(len(PdfReader(str(edited.pdf)).pages), 1)

        edited_content = json.loads(edited.content_json.read_text(encoding="utf-8"))
        self.assertIn("用户改写后的第一条要点", edited_content["experience_sections"][0]["entries"][0]["bullets"][0]["text"])

        bundle = resolve_resume_bundle(self.profile, self.job, self.applications_dir)
        self.assertEqual(bundle.status, "needs_review")
        self.assertEqual(
            find_latest_resume_manifest(self.applications_dir, self.job.job_id),
            edited.manifest,
        )

        approve_resume_visual_review(edited.manifest)
        ready = resolve_resume_bundle(self.profile, self.job, self.applications_dir)
        self.assertEqual(ready.status, "ready")
        self.assertTrue(ready.qa_verified)

    def test_rerender_with_declarative_template_dispatches_spec(self) -> None:
        content, manifest_path = load_latest_resume_content(
            self.applications_dir, self.job.job_id
        )
        content["template_id"] = "minimal-mono"

        edited = rerender_edited_resume(
            content,
            self.applications_dir,
            based_on=str(manifest_path),
        )

        manifest = json.loads(edited.manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["template_id"], "minimal-mono")
        self.assertEqual(manifest["qa"]["pdf_visual_review"], "pending_user_review")
        self.assertTrue(edited.docx.exists() and edited.docx.stat().st_size > 10_000)
        # 测试环境无 Word → PDF 回退 legacy 渲染，仍须单页
        self.assertEqual(len(PdfReader(str(edited.pdf)).pages), 1)
        edited_content = json.loads(edited.content_json.read_text(encoding="utf-8"))
        self.assertEqual(edited_content["template_id"], "minimal-mono")

    def test_validate_rejects_structural_problems(self) -> None:
        content, _ = load_latest_resume_content(self.applications_dir, self.job.job_id)

        with self.assertRaises(ResumeEditorError):
            validate_resume_content({**content, "person": {**content["person"], "name": ""}})
        with self.assertRaises(ResumeEditorError):
            validate_resume_content({**content, "template_id": "other-template"})
        with self.assertRaises(ResumeEditorError):
            validate_resume_content({**content, "experience_sections": []})
        with self.assertRaises(ResumeEditorError):
            validate_resume_content({**content, "target": {}})
        broken = json.loads(json.dumps(content))
        broken["experience_sections"][0]["entries"][0]["bullets"][0]["text"] = "   "
        with self.assertRaises(ResumeEditorError):
            validate_resume_content(broken)

    def test_rerender_rejects_summary_over_limit(self) -> None:
        content, _ = load_latest_resume_content(self.applications_dir, self.job.job_id)
        content["summary"] = "超长摘要。" * 120
        with self.assertRaises(ResumeEditorError):
            rerender_edited_resume(content, self.applications_dir)


if __name__ == "__main__":
    unittest.main()
