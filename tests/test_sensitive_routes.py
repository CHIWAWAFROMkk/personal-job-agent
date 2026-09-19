"""Actual local HTTP authentication tests using isolated synthetic data."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import save_profile
from tests.helpers import sample_profile


class SensitiveRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = sample_profile()
        self.profile_path = self.root / "profile.json"
        save_profile(self.profile, self.profile_path)
        self.lan_patch = patch("job_agent.services.sms_sync.get_lan_ip", return_value="127.0.0.1")
        self.lan_patch.start()
        self.server = create_dashboard_server(JobRepository(self.root / "jobs.sqlite3"),
            output_dir=self.root / "output", port=0, profile_path=self.profile_path, private_dir=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = self.request("/api/dashboard")[1]["action_token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.lan_patch.stop()
        self.temp.cleanup()

    def request(self, path, method="GET", headers=None):
        req = urllib.request.Request(self.base + path, method=method,
            # These routes take no request body. Avoid an unread payload when
            # authentication deliberately rejects the request before parsing.
            data=b"" if method == "POST" else None, headers=headers or {})
        try:
            result = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as error:
            result = error
        with result:
            return result.status, json.loads(result.read().decode("utf-8"))

    def test_sensitive_routes_reject_missing_and_wrong_tokens(self):
        for path, method in [("/api/profile", "GET"), ("/api/agent/token", "POST"), ("/api/sms/setup", "POST")]:
            for headers in [{}, {"X-Job-Agent-Token": "synthetic-invalid-token"}]:
                with self.subTest(path=path, headers_present=bool(headers)):
                    status, payload = self.request(path, method, headers)
                    self.assertEqual(status, 403)
                    self.assertNotIn("agent_token", payload)
                    self.assertNotIn("webhook_url", payload)
                    self.assertNotIn("facts", payload)

    def test_sensitive_routes_reject_foreign_origins_even_with_action_token(self):
        for path, method in [("/api/profile", "GET"), ("/api/agent/token", "POST"), ("/api/sms/setup", "POST")]:
            for origin in ["https://evil.example", "chrome-extension://synthetic", "moz-extension://synthetic", "null"]:
                with self.subTest(path=path, origin=origin):
                    status, _ = self.request(path, method, {"X-Job-Agent-Token": self.token, "Origin": origin})
                    self.assertEqual(status, 403)

    def test_authorized_profile_only_returns_current_confirmed_facts(self):
        status, data = self.request("/api/profile", headers={"X-Job-Agent-Token": self.token, "Origin": self.base})
        self.assertEqual(status, 200)
        self.assertEqual(data["facts"]["name"], self.profile.person.display_name)
        self.assertEqual(data["facts"]["email"], self.profile.person.contact.email)
        self.assertNotIn("Tableau", data["facts"]["experience"])

    def test_authorized_pairing_returns_separate_synthetic_runtime_secrets(self):
        headers = {"X-Job-Agent-Token": self.token, "Origin": self.base}
        status, agent = self.request("/api/agent/token", "POST", headers)
        self.assertEqual(status, 200)
        self.assertTrue(agent["agent_token"])
        self.assertNotEqual(agent["agent_token"], self.token)
        status, setup = self.request("/api/sms/setup", "POST", headers)
        self.assertEqual(status, 200)
        self.assertIn("/api/sms/webhook?token=", setup["webhook_url"])
        self.assertNotIn(agent["agent_token"], setup["webhook_url"])
        self.assertIn("latest_code", setup)
        self.assertTrue((self.root / "sms-webhook-token.json").is_file())

    def test_public_sms_status_has_no_secrets_or_messages(self):
        status, payload = self.request("/api/sms/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        for key in ["latest_code", "token", "sms_webhook_token", "agent_token", "webhook_url", "raw_text"]:
            self.assertNotIn(key, payload)


if __name__ == "__main__":
    unittest.main()
