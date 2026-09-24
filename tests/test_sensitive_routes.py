"""Actual local HTTP authentication tests using isolated synthetic data."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

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
            body = result.read().decode("utf-8")
            return result.status, json.loads(body) if result.headers.get_content_type() == "application/json" else body

    def test_sensitive_routes_reject_missing_and_wrong_tokens(self):
        for path, method in [("/api/profile", "GET"), ("/api/agent/token", "POST")]:
            for headers in [{}, {"X-Job-Agent-Token": "synthetic-invalid-token"}]:
                with self.subTest(path=path, headers_present=bool(headers)):
                    status, payload = self.request(path, method, headers)
                    self.assertEqual(status, 403)
                    self.assertNotIn("agent_token", payload)
                    self.assertNotIn("webhook_url", payload)
                    self.assertNotIn("facts", payload)

    def test_unauthorized_json_has_explicit_byte_length(self):
        request = urllib.request.Request(self.base + '/api/profile')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5)
        with caught.exception as response:
            body = response.read()
            self.assertEqual(response.status, 403)
            self.assertEqual(int(response.headers['Content-Length']), len(body))
            self.assertIn('error', json.loads(body))

    def test_sensitive_routes_reject_foreign_origins_even_with_action_token(self):
        for path, method in [("/api/profile", "GET"), ("/api/agent/token", "POST")]:
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

    def test_removed_sms_routes_are_unavailable(self):
        headers = {"X-Job-Agent-Token": self.token, "Origin": self.base}
        for path, method in [("/api/sms/setup", "POST"), ("/api/sms/status", "GET"),
                             ("/api/sms/manual", "POST"), ("/api/sms/clear", "POST"),
                             ("/api/sms/webhook", "POST")]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, method, headers)[0], 404 if method == "GET" else 405)
        self.assertFalse((self.root / "sms-webhook-token.json").exists())


if __name__ == "__main__":
    unittest.main()
