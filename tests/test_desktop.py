from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.desktop import (
    default_user_data_root,
    desktop_smoke_test,
    prepare_user_workspace,
)


class DesktopTests(unittest.TestCase):
    def test_packaged_app_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-agent-local-") as directory:
            with patch.dict(
                os.environ,
                {"LOCALAPPDATA": directory, "JOB_AGENT_PROJECT_ROOT": ""},
                clear=False,
            ):
                self.assertEqual(
                    default_user_data_root(frozen=True),
                    (Path(directory) / "PersonalJobAgent").resolve(),
                )

    def test_explicit_data_directory_has_priority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-agent-explicit-") as directory:
            expected = Path(directory).resolve()
            self.assertEqual(
                default_user_data_root(explicit=expected, frozen=True), expected
            )

    def test_workspace_and_smoke_test_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-agent-smoke-") as directory:
            root = prepare_user_workspace(Path(directory))
            report_path = desktop_smoke_test(root)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["database_created"])
            self.assertTrue(payload["settings_api_ready"])
            self.assertTrue(payload["openai_sdk_ready"])
            self.assertTrue((root / "data/private").is_dir())
            self.assertTrue((root / "data/output").is_dir())


if __name__ == "__main__":
    unittest.main()
