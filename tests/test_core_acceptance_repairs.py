import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from pypdf import PdfReader
from reportlab.pdfgen.canvas import Canvas
from job_agent.models.job_record import JobRecordInput
from job_agent.services.job_repository import JobRepository
from job_agent.services.dashboard_routes.jobs_api import _ensure_match
from job_agent.services.pdf_fonts import resume_pdf_font
from tests.helpers import sample_profile


class CoreAcceptanceRepairs(unittest.TestCase):
    def test_pdf_embeds_chinese_and_preserves_latin_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'font.pdf'
            c = Canvas(str(path))
            c.setFont(resume_pdf_font(), 12)
            c.drawString(30, 700, '中文 Python NumPy HR SQL')
            c.save()
            reader = PdfReader(path)
            self.assertIn('Python NumPy HR SQL', reader.pages[0].extract_text())
            fonts = [f.get_object() for f in reader.pages[0]['/Resources']['/Font'].values()]
            self.assertTrue(any('/FontFile2' in f['/FontDescriptor'] for f in fonts if '/FontDescriptor' in f))

    def test_match_refreshes_changed_profile_without_duplicate_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = JobRepository(Path(tmp) / 'jobs.sqlite3')
            item = repo.upsert_job(JobRecordInput(company='示例公司', title='数据实习', jd_text='要求 SQL 数据分析，本科在读，每周四天。', source='test'))
            job = repo.get_job(item.job_id)
            profile = sample_profile()
            first = _ensure_match(repo, profile, job)
            repo.add_match_result = Mock(wraps=repo.add_match_result)
            _ensure_match(repo, profile, job)
            repo.add_match_result.assert_not_called()
            changed = profile.model_copy(deep=True)
            changed.skills = []
            changed.experiences = []
            changed.education = []
            second = _ensure_match(repo, changed, job)
            repo.add_match_result.assert_called_once()
            self.assertNotEqual(first.model_dump(exclude={'generated_at'}), second.model_dump(exclude={'generated_at'}))
            self.assertEqual(repo.get_job(job.job_id).match_score, second.overall_score)
            self.assertEqual(repo.get_job(job.job_id).status, job.status)
