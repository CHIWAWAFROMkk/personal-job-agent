from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.settings import _detect_project_root


class SettingsTests(unittest.TestCase):
    def test_explicit_project_root_handles_non_ascii_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="求职-agent-") as directory:
            expected = Path(directory).resolve()
            with patch.dict(
                "os.environ",
                {"JOB_AGENT_PROJECT_ROOT": str(expected)},
                clear=False,
            ):
                self.assertEqual(_detect_project_root(), expected)


if __name__ == "__main__":
    unittest.main()
