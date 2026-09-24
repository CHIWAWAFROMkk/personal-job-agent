from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.desktop import (
    desktop_acceptance_test,
    desktop_window_test,
    main,
    prepare_user_workspace,
)


class DesktopAcceptanceTests(unittest.TestCase):
    def test_window_test_refuses_existing_profile_before_starting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="window-existing-") as temp_dir:
            data_root = Path(temp_dir)
            profile_path = data_root / "data" / "private" / "profile.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text('{"keep":"user-data"}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "已有个人资料"):
                desktop_window_test(data_root)
            self.assertEqual(profile_path.read_text(encoding="utf-8"), '{"keep":"user-data"}')

    def test_window_test_requires_explicit_isolated_directory(self) -> None:
        self.assertEqual(main(["--window-test"]), 1)

    def test_fresh_workspace_runs_all_required_checks_and_exits_0(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acceptance-empty-", ignore_cleanup_errors=True) as temp_dir:
            data_root = prepare_user_workspace(Path(temp_dir))
            report_file = data_root / "acceptance-report.json"

            # Should complete without error
            returned_path = desktop_acceptance_test(data_root, report_file)
            self.assertTrue(returned_path.is_file())

            report = json.loads(report_file.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["failed_checks"], {})
            self.assertEqual(report["skipped_checks"], {})
            self.assertEqual(len(report["passed_checks"]), 8)
            self.assertEqual(report["checks"]["resume_preview"]["status"], "ok")
            self.assertEqual(report["checks"]["mock_interview"]["status"], "ok")
            self.assertEqual(report["checks"]["database"]["status"], "ok")
            self.assertEqual(report["checks"]["settings_api"]["status"], "ok")

            # Main CLI invocation must return 0
            exit_code = main(["--acceptance-test", "--data-dir", str(data_root / "acceptance-cli"), "--acceptance-report", str(data_root / "cli-report.json")])
            self.assertEqual(exit_code, 0)
            cli_report = json.loads((data_root / "cli-report.json").read_text(encoding="utf-8"))
            self.assertEqual(cli_report["skipped_checks"], {})
            self.assertEqual(len(cli_report["passed_checks"]), 8)

    def test_mock_interview_failure_causes_failed_status_and_exit_1(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acceptance-mock-fail-", ignore_cleanup_errors=True) as temp_dir:
            data_root = prepare_user_workspace(Path(temp_dir))
            report_file = data_root / "acceptance-report.json"

            from urllib.request import Request, urlopen as original_urlopen

            def mock_urlopen(req, *args, **kwargs):
                url = req.full_url if isinstance(req, Request) else req
                if "api/interview/teleprompter" in url:
                    raise ConnectionResetError("Injected mock interview failure")
                return original_urlopen(req, *args, **kwargs)

            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with self.assertRaises(RuntimeError) as cm:
                    desktop_acceptance_test(data_root, report_file)

                self.assertIn("mock_interview", str(cm.exception))

            # Verify report was written with failed status
            self.assertTrue(report_file.is_file())
            report = json.loads(report_file.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertIn("mock_interview", report["failed_checks"])
            self.assertEqual(report["checks"]["mock_interview"]["status"], "failed")
            self.assertIn("Injected mock interview failure", report["checks"]["mock_interview"]["error"])
            self.assertFalse(report["mock_interview_ready"])

            # Verify main CLI returns 1 on failure
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                exit_code = main(["--acceptance-test", "--data-dir", str(data_root / "acceptance-cli"), "--acceptance-report", str(data_root / "cli-report.json")])
                self.assertEqual(exit_code, 1)
                cli_report = json.loads((data_root / "cli-report.json").read_text(encoding="utf-8"))
                self.assertEqual(cli_report["status"], "failed")
                self.assertEqual(cli_report["skipped_checks"], {})
                self.assertIn("Injected mock interview failure", cli_report["checks"]["mock_interview"]["error"])

    def test_resume_preview_failure_causes_failed_status_and_exit_1(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acceptance-preview-fail-", ignore_cleanup_errors=True) as temp_dir:
            data_root = prepare_user_workspace(Path(temp_dir))
            report_file = data_root / "acceptance-report.json"

            from urllib.request import Request, urlopen as original_urlopen

            def mock_urlopen(req, *args, **kwargs):
                url = req.full_url if isinstance(req, Request) else req
                if "api/resume/preview" in url:
                    raise TimeoutError("Injected resume preview timeout")
                return original_urlopen(req, *args, **kwargs)

            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with self.assertRaises(RuntimeError) as cm:
                    desktop_acceptance_test(data_root, report_file)
                self.assertIn("resume_preview", str(cm.exception))

            report = json.loads(report_file.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertIn("resume_preview", report["failed_checks"])
            self.assertEqual(report["checks"]["resume_preview"]["status"], "failed")
            self.assertFalse(report["resume_preview_ready"])

            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                exit_code = main(["--acceptance-test", "--data-dir", str(data_root / "acceptance-cli"), "--acceptance-report", str(data_root / "cli-report.json")])
                self.assertEqual(exit_code, 1)
                cli_report = json.loads((data_root / "cli-report.json").read_text(encoding="utf-8"))
                self.assertEqual(cli_report["status"], "failed")
                self.assertEqual(cli_report["skipped_checks"], {})
                self.assertIn("Injected resume preview timeout", cli_report["checks"]["resume_preview"]["error"])

    def test_database_corruption_causes_failed_status_and_exit_1(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acceptance-db-corrupt-", ignore_cleanup_errors=True) as temp_dir:
            data_root = prepare_user_workspace(Path(temp_dir))
            db_path = data_root / "data" / "private" / "job_agent.sqlite3"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # Write corrupted file (not a valid sqlite database)
            db_path.write_bytes(b"CORRUPTED_GARBAGE_BYTES_SQLITE_HEADER_INVALID")
            report_file = data_root / "acceptance-report.json"

            with self.assertRaises(Exception):
                desktop_acceptance_test(data_root, report_file)

            if report_file.is_file():
                report = json.loads(report_file.read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "failed")

            exit_code = main(["--acceptance-test", "--data-dir", str(data_root), "--acceptance-report", str(report_file)])
            self.assertEqual(exit_code, 1)
            self.assertEqual(db_path.read_bytes(), b"CORRUPTED_GARBAGE_BYTES_SQLITE_HEADER_INVALID")


if __name__ == "__main__":
    unittest.main()
