import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.job_repository import JobRepository


class DashboardHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = JobRepository(self.root / "jobs.sqlite3")
        self.server = create_dashboard_server(
            self.repository, output_dir=self.root / "output", port=0,
            private_dir=self.root / "private",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, *, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_js_asset_allowlist_rejects_traversal_and_keeps_modules_working(self):
        # A sensitive JS outside web/js was readable through the old join logic.
        outside = self.root / "private.js"
        outside.write_text("private fixture must never be served", encoding="utf-8")
        (self.root / "js").mkdir()
        self.server.RequestHandlerClass.web_dir = self.root
        assets = self.server.RequestHandlerClass.js_assets
        self.assertTrue(assets)
        module = next(iter(assets))
        status, body, headers = self.request(module + "?v=test")
        self.assertEqual(status, 200)
        self.assertEqual(body, assets[module])
        self.assertIn("javascript", headers["Content-Type"])
        for path in (
            "/js/../private.js", "/js/..\\private.js", "/js/%2e%2e/private.js",
            "/js/..%5cprivate.js", "/js//../private.js", "/js/unknown.js",
        ):
            with self.subTest(path=path):
                status, body, _ = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"private fixture", body)

    def test_untrusted_hosts_cannot_read_dashboard_or_settings(self):
        port = self.server.server_port
        for host in (f"attacker.invalid:{port}", "localhost:1", f"user@127.0.0.1:{port}"):
            for path in ("/api/dashboard", "/api/settings", "/"):
                with self.subTest(host=host, path=path):
                    status, body, _ = self.request(path, headers={"Host": host})
                    self.assertEqual(status, 403)
                    self.assertNotIn(b"action_token", body)
        self.assertEqual(self.request("/api/health", headers={"Host": f"localhost:{port}"})[0], 200)

    def test_auth_rejection_does_not_wait_for_claimed_body(self):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as connection:
            connection.sendall((
                f"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n"
            ).encode("ascii"))
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 403)
            response.read()

    def test_invalid_token_characters_return_forbidden(self):
        for path, method, header in (
            ("/api/settings", "POST", "X-Job-Agent-Token"),
            ("/api/agent/state", "GET", "X-Agent-Token"),
        ):
            self.assertEqual(self.request(path, method=method, body=b"{}", headers={header: "\xe9"})[0], 403)

    def test_json_request_framing_is_checked(self):
        token = self.server.RequestHandlerClass.action_token
        headers = {"X-Job-Agent-Token": token, "Content-Type": "application/json"}
        for body, extra in ((b"[]", {}), (b"{", {}), (b"{}", {"Transfer-Encoding": "chunked"})):
            with self.subTest(body=body, extra=extra):
                status, response, _ = self.request("/api/settings", method="POST", body=body, headers=headers | extra)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(response))
        # Valid requests still dispatch after the invalid ones.
        status, response, _ = self.request("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)["action_token"], token)

    def test_duplicate_length_and_truncated_body_are_rejected(self):
        token = self.server.RequestHandlerClass.action_token
        for body_headers, body in (
            ("Content-Length: 2\r\nContent-Length: 100", b"{}"),
            ("Content-Length: 100", b"{}"),
        ):
            with self.subTest(headers=body_headers):
                with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as connection:
                    connection.sendall((
                        f"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                        f"X-Job-Agent-Token: {token}\r\nContent-Type: application/json\r\n"
                        f"{body_headers}\r\n\r\n"
                    ).encode("ascii") + body)
                    connection.shutdown(socket.SHUT_WR)
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    self.assertEqual(response.status, 400)
                    self.assertIn("error", json.loads(response.read()))

    def test_unexpected_route_error_returns_json_without_internal_details(self):
        from job_agent.services.dashboard_routes import copilot_api
        with mock.patch.object(copilot_api, "copilot_snapshot", side_effect=RuntimeError("private fixture path")):
            with self.assertLogs("job_agent.services.dashboard", level="ERROR"):
                status, body, headers = self.request("/api/copilot")
        self.assertEqual(status, 500)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn("error", json.loads(body))
        self.assertNotIn(b"private fixture", body)
        self.assertEqual(self.request("/api/health")[0], 200)

    def test_missing_artifact_after_lookup_is_not_reported_as_success(self):
        from job_agent.services.dashboard_routes import jobs_api
        with mock.patch.object(jobs_api, "_latest_preparation_path", return_value=self.root / "missing.md"):
            with self.assertLogs("job_agent.services.dashboard", level="ERROR"):
                status, body, _ = self.request("/preparation/1")
        self.assertEqual(status, 500)
        self.assertIn("error", json.loads(body))

    def test_unsupported_write_methods_keep_json_405_and_host_protection(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for path in ("/api/dashboard", "/does-not-exist"):
                with self.subTest(method=method, path=path):
                    status, body, headers = self.request(path, method=method, body=b"{}")
                    self.assertEqual(status, 405)
                    self.assertIn("application/json", headers["Content-Type"])
                    self.assertIn("error", json.loads(body))
                    status, _, _ = self.request(path, method=method, headers={"Host": "attacker.invalid"})
                    self.assertEqual(status, 403)
        self.assertEqual(self.request("/does-not-exist")[0], 404)


if __name__ == "__main__":
    unittest.main()
