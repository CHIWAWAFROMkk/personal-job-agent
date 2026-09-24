"""Synthetic loopback regressions for writes crossing a profile replacement."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.dashboard_routes import profile_api
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import load_profile, save_profile
from tests.helpers import sample_profile


def _multipart_switch() -> tuple[str, bytes]:
    boundary = "----ProfileContextBoundary"
    fields = {
        "mode": "replace",
        "display_name": "New synthetic user",
        "target_roles": "New role",
        "preferred_locations": "New city",
        "confirm_truth": "true",
        "confirm_replace": "true",
    }
    parts = []
    for key, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"resume\"; filename=\"new.txt\"\r\n"
        "Content-Type: text/plain\r\n\r\n".encode()
        + b"Synthetic resume facts.\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


class ProfileContextGateHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private_dir = self.root / "private"
        self.profile_path = self.private_dir / "profile.json"
        save_profile(sample_profile(), self.profile_path)
        self.repository = JobRepository(self.root / "jobs.sqlite3")
        self.server = create_dashboard_server(
            self.repository,
            output_dir=self.root / "output",
            profile_path=self.profile_path,
            private_dir=self.private_dir,
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def call(
        self, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers=headers or {},
            method="POST" if body is not None else "GET",
        )
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def switch(self, token: str) -> tuple[int, dict]:
        content_type, body = _multipart_switch()
        return self.call(
            "/api/profile/onboard", body=body,
            headers={"Content-Type": content_type, "Origin": self.base,
                     "X-Job-Agent-Token": token},
        )

    def preferences(self, token: str, role: str, city: str) -> tuple[int, dict]:
        body = json.dumps({"target_roles": [role], "preferred_locations": [city]}).encode()
        return self.call(
            "/api/profile/preferences", body=body,
            headers={"Content-Type": "application/json", "Origin": self.base,
                     "X-Job-Agent-Token": token},
        )

    def test_queued_old_page_write_and_later_old_tab_write_are_rejected(self) -> None:
        _, initial = self.call("/api/dashboard")
        old_token = initial["action_token"]
        entered = threading.Event()
        finish_switch = threading.Event()
        queued_started = threading.Event()
        results: dict[str, tuple[int, dict]] = {}
        original_switch = profile_api.switch_to_new_profile

        def paused_switch(*args, **kwargs):
            entered.set()
            if not finish_switch.wait(5):
                raise TimeoutError("profile switch test timed out")
            return original_switch(*args, **kwargs)

        def do_switch() -> None:
            results["switch"] = self.switch(old_token)

        def queued_write() -> None:
            queued_started.set()
            results["queued"] = self.preferences(old_token, "Old user role", "Old user city")

        try:
            with mock.patch.object(profile_api, "switch_to_new_profile", paused_switch):
                switch_thread = threading.Thread(target=do_switch, daemon=True)
                switch_thread.start()
                self.assertTrue(entered.wait(5))
                old_write_thread = threading.Thread(target=queued_write, daemon=True)
                old_write_thread.start()
                self.assertTrue(queued_started.wait(5))
                time.sleep(0.15)
                self.assertNotIn("queued", results)
                finish_switch.set()
                switch_thread.join(timeout=10)
                old_write_thread.join(timeout=10)
            self.assertFalse(switch_thread.is_alive())
            self.assertFalse(old_write_thread.is_alive())
            self.assertEqual(results["switch"][0], 200)
            new_token = results["switch"][1]["action_token"]
            self.assertTrue(new_token)
            self.assertNotEqual(new_token, old_token)
            self.assertEqual(results["queued"][0], 409)
            self.assertEqual(results["queued"][1]["code"], "profile_changed")
            self.assertNotIn(new_token, json.dumps(results["queued"][1]))
            self.assertEqual(self.preferences(old_token, "Old again", "Old city")[0], 409)

            # The switching page continues with its returned context. A fresh
            # dashboard read also obtains it, but old writes remain rejected.
            self.assertEqual(self.preferences(new_token, "New revised role", "New city")[0], 200)
            _, current = self.call("/api/dashboard")
            self.assertEqual(current["action_token"], new_token)
            profile = load_profile(self.profile_path)
            self.assertEqual(profile.person.display_name, "New synthetic user")
            self.assertEqual(profile.job_search.target_roles, ["New revised role"])
            self.assertEqual(profile.job_search.preferred_locations, ["New city"])
        finally:
            finish_switch.set()

    def test_other_ui_writes_and_agent_writes_are_bound_to_old_context(self) -> None:
        _, initial = self.call("/api/dashboard")
        old_token = initial["action_token"]
        agent_token = self.server.RequestHandlerClass.agent_token
        _, state = self.call("/api/agent/state", headers={"X-Agent-Token": agent_token})
        old_context = state["profile_context"]
        self.assertEqual(self.switch(old_token)[0], 200)
        _, fresh_state = self.call("/api/agent/state", headers={"X-Agent-Token": agent_token})
        self.assertNotEqual(old_context, fresh_state["profile_context"])

        payload = json.dumps({"company": "Old company", "title": "Old role",
                              "jd_text": "Old JD", "source": "synthetic"}).encode()
        self.assertEqual(self.call(
            "/api/jobs/import-parsed", body=payload,
            headers={"Content-Type": "application/json", "X-Job-Agent-Token": old_token},
        )[0], 409)
        self.assertEqual(self.call(
            "/api/jobs/import-parsed", body=payload,
            headers={"Content-Type": "application/json", "X-Agent-Token": agent_token,
                     "X-Profile-Context": old_context},
        )[0], 409)
        self.assertEqual(self.call(
            "/api/jobs/import-parsed", body=payload,
            headers={"Content-Type": "application/json", "X-Agent-Token": agent_token},
        )[0], 409)
        self.assertEqual(self.repository.stats().jobs, 0)

    def test_journal_finalization_failure_freezes_all_data_routes_until_restart(self) -> None:
        from job_agent.models.job_record import JobRecordInput
        from job_agent.services import profile_onboarding
        _, initial = self.call("/api/dashboard")
        old_token = initial["action_token"]
        with mock.patch.object(profile_onboarding,"finish_record",side_effect=OSError("journal finalization denied")):
            self.assertEqual(self.switch(old_token)[0],400)
        self.assertEqual(self.repository.stats().jobs,0)
        # A subsequent job in the new DB must not be writable by the old page.
        job_id = self.repository.upsert_job(JobRecordInput(company="New company",title="New job",jd_text="synthetic",source="synthetic")).job_id
        for path, body in (
            (f"/api/jobs/{job_id}/archive-job",json.dumps({"ignored":True}).encode()),
            ("/api/jobs/import-parsed",json.dumps({"company":"Old company","title":"Old role","jd_text":"Old JD","source":"synthetic"}).encode()),
            ("/api/dashboard",None),
        ):
            status, response = self.call(path,body=body,headers={"Content-Type":"application/json","X-Job-Agent-Token":old_token})
            self.assertEqual(status,503)
            self.assertEqual(response["code"],"profile_recovery_required")
        self.assertEqual(self.repository.stats().jobs,1)
        self.assertEqual(self.repository.archived_jobs(),set())


if __name__ == "__main__":
    unittest.main()
