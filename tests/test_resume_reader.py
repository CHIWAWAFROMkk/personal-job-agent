import tempfile
import unittest
from pathlib import Path

from job_agent.services.resume_reader import ResumeReadError, read_resume


class ResumeReaderTests(unittest.TestCase):
    def test_reads_utf8_text_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "resume.txt"
            path.write_text("教育经历\n项目经历\n", encoding="utf-8")

            document = read_resume(path)

        self.assertIn("教育经历", document.text)
        self.assertEqual(len(document.sha256), 64)

    def test_rejects_unsupported_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "resume.rtf"
            path.write_text("resume", encoding="utf-8")
            with self.assertRaises(ResumeReadError):
                read_resume(path)


if __name__ == "__main__":
    unittest.main()

