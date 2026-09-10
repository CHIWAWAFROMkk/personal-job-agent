import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from job_agent.services.portable_resume import _write_pdf_from_docx


@unittest.skipUnless(os.name == 'nt', 'Windows Office converter')
class OfficePdfExportTests(unittest.TestCase):
    def test_office_timeout_leaves_time_for_local_rendering(self):
        with patch('shutil.which', return_value='pwsh'), patch(
            'subprocess.run', side_effect=subprocess.TimeoutExpired('pwsh', 10)
        ) as run:
            result = _write_pdf_from_docx(Path('input.docx'), Path('output.pdf'))
        self.assertIsNone(result)
        self.assertLessEqual(run.call_args.kwargs['timeout'], 10)
