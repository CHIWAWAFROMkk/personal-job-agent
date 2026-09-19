import unittest
from pathlib import Path
from unittest.mock import patch
from job_agent.services.portable_resume import _write_pdf_from_docx


class OfficeExportOptInTests(unittest.TestCase):
    def test_default_export_never_starts_office(self):
        with patch.dict('os.environ', {'JOB_AGENT_USE_OFFICE_PDF': ''}), patch('subprocess.run') as run:
            self.assertIsNone(_write_pdf_from_docx(Path('synthetic.docx'), Path('synthetic.pdf')))
            run.assert_not_called()
