import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from job_agent.models.job_record import JobRecordInput
from job_agent.models.profile import ClaimStatus
from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.fill_bridge import FillBridge, minimal_fill_data, public_https_origin
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import save_profile
from tests.helpers import sample_profile


class FillBridgeTests(unittest.TestCase):
    def test_idempotency_isolation_expiry_and_consume_once(self):
        now = [0]
        store = FillBridge(clock=lambda: now[0])
        a = store.create(1, "https://jobs.example/a", "https://jobs.example/b")["session_id"]
        b = store.create(2, "https://jobs.example/c", "https://jobs.example/b")["session_id"]
        recorder = Mock()
        args = ("123456", "request-1", a, recorder)
        self.assertTrue(store.manual(*args)["queued"])
        self.assertTrue(store.manual(*args)["duplicate"])
        recorder.assert_not_called()
        self.assertIsNone(store.poll(b)["code"])
        self.assertEqual(store.poll(a)["code"], "123456")
        self.assertIsNone(store.poll(a)["code"])
        with self.assertRaises(ValueError):
            store.manual("654321", "request-1", a)
        store.manual("654321", "request-2", a)
        now[0] = 121
        self.assertIsNone(store.poll(a)["code"])
        self.assertEqual(store.list(1), [])
        with self.assertRaises(ValueError):
            store.manual("123456", "request-3", a)
        self.assertIsNone(FillBridge().poll(b)["code"])

    def test_close_legacy_and_limits(self):
        store = FillBridge()
        callback = Mock()
        store.manual("1234", legacy_record=callback)
        store.manual("1234", legacy_record=callback)
        callback.assert_called_once_with("1234", sender="manual")
        with self.assertRaises(ValueError):
            store.manual("1234", session_id="", legacy_record=callback)
        sid = store.create(1, "https://jobs.example/a", "https://jobs.example/a")["session_id"]
        store.close(sid)
        self.assertEqual(store.list(1), [])
        store.LIMIT = 1
        with self.assertRaises(ValueError):
            store.manual("5678", "new")
        for code in ["１２３４", "123", "1234567", "code 1234", " 1234", 1234, None]:
            with self.subTest(code=code), self.assertRaises(ValueError):
                FillBridge().manual(code)

    def test_public_origin_validation(self):
        for url in [None, "http://jobs.example", "https://127.0.0.1/a", "https://[::1]/", "https://192.168.0.1", "https://localhost", "https://x.local", "https://u:p@example.com", "https://jobs.example:8000", "https://jobs.example\\evil", "https://2130706433", "https://127.1"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_https_origin(url)
        with self.assertRaises(ValueError):
            FillBridge().create(1, "https://evil.example", "https://jobs.example")

    def test_minimal_profile(self):
        profile = sample_profile()
        data = minimal_fill_data(profile, 1)
        self.assertEqual(set(data["fields"]), {"name", "phone", "email", "school", "major", "degree", "experience"})
        self.assertNotIn("Tableau", json.dumps(data))
        self.assertIn("示例公司 / 数据运营实习生", data["fields"]["experience"])
        self.assertFalse(data["attachment"]["available"])
        profile.education[0].status = ClaimStatus.NEEDS_CONFIRMATION
        self.assertNotIn("school", minimal_fill_data(profile, 1)["fields"])
        profile.experiences[0].facts[0].statement = "x" * 12001
        self.assertNotIn("experience", minimal_fill_data(profile, 1)["fields"])
        self.assertTrue(minimal_fill_data(profile, 1)["notes"])


class FillBridgeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.profile = sample_profile()
        profile_path = root / "profile.json"
        save_profile(self.profile, profile_path)
        repository = JobRepository(root / "jobs.sqlite3")
        self.job_id = repository.upsert_job(JobRecordInput(company="示例企业", title="测试岗位", jd_text="测试 JD", source="official", source_url="https://jobs.example/role/1")).job_id
        self.server = create_dashboard_server(repository, output_dir=root / "output", port=0, profile_path=profile_path, private_dir=root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.action = {"X-Job-Agent-Token": self.server.RequestHandlerClass.action_token}
        self.agent = {"X-Agent-Token": self.server.RequestHandlerClass.agent_token}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, payload=None, headers=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_auth_and_cross_site(self):
        for path, payload in [(f"/api/jobs/{self.job_id}/fill-data", None), ("/api/fill/session", {}), ("/api/fill/poll", {}), ("/api/fill/close", {})]:
            for headers in [{}, {"X-Agent-Token": "bad"}, {**self.agent, "Origin": "https://evil.example"}, {**self.agent, "Origin": "chrome-extension://not-valid"}, {**self.agent, "Origin": "null"}]:
                with self.subTest(path=path, headers=list(headers)):
                    self.assertEqual(self.request(path, payload, headers)[0], 403)
        for path in ["/api/sms/manual", "/api/fill/sessions"]:
            self.assertEqual(self.request(path, {}, {})[0], 403)
            self.assertEqual(self.request(path, {}, {**self.action, "Origin": "https://evil.example"})[0], 403)

    def test_handoff_real_http(self):
        status, data = self.request(f"/api/jobs/{self.job_id}/fill-data", headers={**self.agent, "Origin": "chrome-extension://" + "a" * 32})
        self.assertEqual(status, 200)
        self.assertEqual(data["fields"]["name"], self.profile.person.display_name)
        self.assertEqual(data["job_url"], "https://jobs.example/role/1")
        self.assertNotIn("Tableau", json.dumps(data))
        payload = {"job_id": self.job_id, "url": "https://jobs.example/apply"}
        status, session = self.request("/api/fill/session", payload, self.agent)
        self.assertEqual(status, 200)
        sid = session["session_id"]
        self.assertEqual(self.request("/api/fill/sessions", {"job_id": self.job_id}, self.action)[1]["sessions"][0]["session_id"], sid)
        manual = {"code": "123456", "request_id": "synthetic-request", "session_id": sid}
        with patch("job_agent.services.dashboard_routes.jobs_api.record_sms") as record:
            self.assertEqual(self.request("/api/sms/manual", manual, self.action)[1], {"ok": True, "duplicate": False, "queued": True})
            self.assertTrue(self.request("/api/sms/manual", manual, self.action)[1]["duplicate"])
            record.assert_not_called()
        self.assertEqual(self.request("/api/fill/poll", {"session_id": sid}, self.agent)[1], {"code": "123456"})
        self.assertEqual(self.request("/api/fill/poll", {"session_id": sid}, self.agent)[1], {"code": None})
        self.assertEqual(self.request("/api/fill/close", {"session_id": sid}, self.agent)[0], 200)
        self.assertEqual(self.request("/api/fill/sessions", {"job_id": self.job_id}, self.action)[1], {"sessions": []})
        for bad in [{"job_id": True, "url": payload["url"]}, {**payload, "url": "https://evil.example"}, {**payload, "url": "http://jobs.example"}]:
            self.assertEqual(self.request("/api/fill/session", bad, self.agent)[0], 400)
        self.assertEqual(self.request("/api/sms/manual", {"code": "x" * 9000}, self.action)[0], 400)


if __name__ == "__main__":
    unittest.main()
