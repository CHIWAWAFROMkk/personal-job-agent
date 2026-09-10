from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from job_agent.models.application import ResumeBundle
from job_agent.services.application_pack import (
    ApplicationPackError,
    build_application_pack,
    resolve_resume_bundle,
    write_application_pack,
)
from job_agent.services.job_repository import JobRepository
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.portable_resume import find_latest_resume_manifest
from tests.helpers import sample_profile
from tests.test_job_repository import JD, make_record


class ApplicationPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repository = JobRepository(self.root / "jobs.sqlite3")
        self.profile = sample_profile()
        outcome = self.repository.upsert_job(make_record())
        self.job = self.repository.get_job(outcome.job_id)
        self.match = match_job_locally(
            self.profile,
            structure_job_locally(
                JD,
                company=self.job.company,
                title=self.job.title,
                location=self.job.location,
                source="牛客网",
                source_url="https://example.com/jobs/123",
            ),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pack_uses_only_confirmed_facts_and_excludes_contact(self) -> None:
        pack = build_application_pack(
            self.profile,
            self.job,
            self.match,
            ResumeBundle(
                status="needs_generation",
                note="尚未生成定向简历。",
            ),
            generated_at=datetime(2026, 8, 18, tzinfo=UTC),
        )

        evidence_ids = {item.fact_id for item in pack.evidence}
        serialized = pack.model_dump_json()
        self.assertNotIn("fact-pending-tableau", evidence_ids)
        self.assertNotIn("private@example.com", serialized)
        self.assertNotIn("123456", serialized)
        self.assertIn(self.job.title, pack.materials.boss_greeting)
        self.assertTrue(
            any(
                item.item == "岗位定向简历" and item.blocks_submission
                for item in pack.review_checklist
            )
        )

    def test_resume_bundle_is_discovered_and_fact_checked(self) -> None:
        resume_dir = self.root / "applications" / "job-1"
        resume_dir.mkdir(parents=True)
        content = {
            "target": {
                "company": self.job.company,
                "role": self.job.title,
            },
            "experience_sections": [
                {
                    "entries": [
                        {
                            "bullets": [
                                {"fact_ids": ["fact-sql-analysis"]},
                            ]
                        }
                    ]
                }
            ],
        }
        content_path = resume_dir / "resume-content-star.json"
        content_path.write_text(
            json.dumps(content, ensure_ascii=False),
            encoding="utf-8",
        )
        docx_bytes = b"docx-placeholder"
        pdf_bytes = b"pdf-placeholder"
        (resume_dir / "定向简历.docx").write_bytes(docx_bytes)
        (resume_dir / "定向简历.pdf").write_bytes(pdf_bytes)
        (resume_dir / "resume-version.json").write_text(
            json.dumps(
                {
                    "target": {
                        "company": self.job.company,
                        "title": self.job.title,
                    },
                    "artifacts": {
                        "docx": {
                            "path": "定向简历.docx",
                            "sha256": hashlib.sha256(docx_bytes).hexdigest().upper(),
                        },
                        "pdf": {
                            "path": "定向简历.pdf",
                            "sha256": hashlib.sha256(pdf_bytes).hexdigest().upper(),
                        },
                    },
                    "qa": {
                        "truthfulness_check": "passed",
                        "docx_structural_review": "passed",
                        "pdf_visual_review": "passed",
                        "pdf_pages": 1,
                        "embedded_photos": 1,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        bundle = resolve_resume_bundle(
            self.profile,
            self.job,
            self.root / "applications",
        )

        self.assertEqual(bundle.status, "ready")
        self.assertTrue(bundle.qa_verified)
        self.assertEqual(bundle.fact_ids, ["fact-sql-analysis"])
        self.assertTrue(bundle.docx_path)
        self.assertTrue(bundle.pdf_path)

        (resume_dir / "定向简历.docx").write_bytes(b"tampered")
        tampered = resolve_resume_bundle(
            self.profile,
            self.job,
            self.root / "applications",
        )
        self.assertEqual(tampered.status, "needs_generation")
        self.assertFalse(tampered.qa_verified)
        self.assertIn("哈希", tampered.note)

    def test_resume_bundle_rejects_unconfirmed_fact(self) -> None:
        resume_dir = self.root / "applications" / "job-1"
        resume_dir.mkdir(parents=True)
        content_path = resume_dir / "resume-content-star.json"
        content_path.write_text(
            json.dumps(
                {
                    "target": {
                        "company": self.job.company,
                        "role": self.job.title,
                    },
                    "experience_sections": [
                        {
                            "entries": [
                                {
                                    "bullets": [
                                        {"fact_ids": ["fact-pending-tableau"]}
                                    ]
                                }
                            ]
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ApplicationPackError, "尚未确认"):
            resolve_resume_bundle(
                self.profile,
                self.job,
                self.root / "applications",
                content_path=content_path,
            )

    def test_ready_resume_is_preferred_over_newer_unapproved_draft(self) -> None:
        def write_version(directory: Path, visual_status: str) -> Path:
            directory.mkdir(parents=True)
            content_path = directory / "resume-content-star.json"
            content_path.write_text(
                json.dumps(
                    {
                        "target": {"company": self.job.company, "role": self.job.title},
                        "experience_sections": [
                            {"entries": [{"bullets": [{"fact_ids": ["fact-sql-analysis"]}]}]}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            docx_bytes = f"docx-{directory.name}".encode()
            pdf_bytes = f"pdf-{directory.name}".encode()
            (directory / "定向简历.docx").write_bytes(docx_bytes)
            (directory / "定向简历.pdf").write_bytes(pdf_bytes)
            (directory / "resume-version.json").write_text(
                json.dumps(
                    {
                        "target": {
                            "job_id": self.job.job_id,
                            "company": self.job.company,
                            "title": self.job.title,
                        },
                        "content_source": content_path.name,
                        "artifacts": {
                            "docx": {
                                "path": "定向简历.docx",
                                "sha256": hashlib.sha256(docx_bytes).hexdigest().upper(),
                            },
                            "pdf": {
                                "path": "定向简历.pdf",
                                "sha256": hashlib.sha256(pdf_bytes).hexdigest().upper(),
                            },
                        },
                        "qa": {
                            "truthfulness_check": "passed",
                            "docx_structural_review": "passed",
                            "pdf_visual_review": visual_status,
                            "pdf_pages": 1,
                            "embedded_photos": 1,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return content_path

        ready = write_version(self.root / "applications" / "older-ready", "passed")
        newer = write_version(
            self.root / "applications" / "newer-unapproved",
            "pending_user_review",
        )

        selected = resolve_resume_bundle(
            self.profile,
            self.job,
            self.root / "applications",
        )

        self.assertEqual(selected.status, "ready")
        self.assertEqual(selected.content_path, str(ready.resolve()))
        latest_manifest = find_latest_resume_manifest(
            self.root / "applications", self.job.job_id
        )
        self.assertIsNotNone(latest_manifest)
        self.assertEqual(latest_manifest.parent, newer.parent.resolve())

    def test_pack_files_are_versioned_and_not_overwritten(self) -> None:
        pack = build_application_pack(
            self.profile,
            self.job,
            self.match,
            ResumeBundle(status="needs_generation", note="待生成。"),
            generated_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        output = self.root / "output" / "v1"

        files = write_application_pack(pack, self.job, self.match, output)

        self.assertTrue(files.pack_json.is_file())
        self.assertTrue(files.materials_markdown.is_file())
        self.assertTrue(files.jd_text.is_file())
        self.assertTrue(files.match_json.is_file())
        with self.assertRaisesRegex(ApplicationPackError, "拒绝覆盖"):
            write_application_pack(pack, self.job, self.match, output)


if __name__ == "__main__":
    unittest.main()
