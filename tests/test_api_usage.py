from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_agent.services.api_usage import public_api_usage, record_api_usage


class ApiUsageTests(unittest.TestCase):
    def test_local_monthly_remaining_is_explicitly_separate_from_provider_balance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "private" / "api-usage.json"
            record_api_usage(
                path,
                "search",
                "bocha",
                successful_requests=3,
            )

            public = public_api_usage(
                path,
                {"ai": None, "search": 20, "maps": None},
            )
            search = public["connectors"]["search"]  # type: ignore[index]

            self.assertEqual(search["successful_requests"], 3)
            self.assertEqual(search["local_remaining"], 17)
            self.assertEqual(search["balance_source"], "local_limit")
            self.assertIn("真实余额", public["notice"])


if __name__ == "__main__":
    unittest.main()
