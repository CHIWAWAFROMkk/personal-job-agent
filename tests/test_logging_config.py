from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from job_agent.services.logging_config import configure_logging


class LoggingConfigTests(unittest.TestCase):
    def test_configure_logging_creates_file_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir).resolve() / "custom_logs"
            log_path = configure_logging(log_dir=log_dir, log_file_name="test.log")
            self.assertTrue(log_path.is_file() or log_dir.is_dir())

            test_logger = logging.getLogger("test_logger")
            test_logger.info("Test message for logging verification")

            # Flush handlers
            for h in list(logging.getLogger().handlers):
                h.flush()

            content = log_path.read_text(encoding="utf-8")
            self.assertIn("Test message for logging verification", content)

            # Close and remove handlers so Windows can delete temp file
            for h in list(logging.getLogger().handlers):
                if getattr(h, "baseFilename", None) == str(log_path.resolve()):
                    h.close()
                    logging.getLogger().removeHandler(h)

    def test_configure_logging_avoids_duplicate_handlers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir).resolve() / "dup_logs"
            path1 = configure_logging(log_dir=log_dir, log_file_name="dup.log")
            initial_count = len(logging.getLogger().handlers)
            path2 = configure_logging(log_dir=log_dir, log_file_name="dup.log")
            self.assertEqual(path1, path2)
            self.assertEqual(len(logging.getLogger().handlers), initial_count)

            # Close and remove handlers so Windows can delete temp file
            for h in list(logging.getLogger().handlers):
                if getattr(h, "baseFilename", None) == str(path1.resolve()):
                    h.close()
                    logging.getLogger().removeHandler(h)


if __name__ == "__main__":
    unittest.main()
