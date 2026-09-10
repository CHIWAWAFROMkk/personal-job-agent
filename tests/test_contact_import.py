from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_agent.services.contact_import import (
    ContactImportError,
    extract_contact_candidates,
    import_contact_from_confirmed_resume,
)
from tests.helpers import sample_profile


class ContactImportTests(unittest.TestCase):
    def test_extracts_and_normalizes_contact_without_guessing(self) -> None:
        emails, phones = extract_contact_candidates(
            "联系方式：Candidate.Name@Example.com，+86 138-0013-8000"
        )
        self.assertEqual(emails, ["candidate.name@example.com"])
        self.assertEqual(phones, ["13800138000"])

    def test_import_requires_confirmed_resume_source(self) -> None:
        profile = sample_profile()
        profile.person.contact.email = None
        profile.person.contact.phone = None
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ContactImportError, "本人确认"):
                import_contact_from_confirmed_resume(profile, Path(directory))

    def test_imports_unique_values_from_confirmed_private_copy(self) -> None:
        profile = sample_profile()
        profile.person.contact.email = None
        profile.person.contact.phone = None
        source = profile.source_documents[0]
        source.sha256 = "a" * 64
        source.notes = "用户已确认该简历提取内容真实，并允许用于求职。"
        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory)
            (private_dir / f"resume-{source.sha256[:12]}.txt").write_text(
                "邮箱 candidate@example.com 手机 139 1234 5678",
                encoding="utf-8",
            )
            updated, imported = import_contact_from_confirmed_resume(
                profile,
                private_dir,
            )

        self.assertEqual(imported, ["邮箱", "手机号"])
        self.assertEqual(updated.person.contact.email, "candidate@example.com")
        self.assertEqual(updated.person.contact.phone, "13912345678")


if __name__ == "__main__":
    unittest.main()
