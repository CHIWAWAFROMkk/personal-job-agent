import io
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from pypdf import PdfReader

from job_agent.models.job_record import JobRecordInput
from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.dashboard_routes.jobs_api import handle_resume_preview
from job_agent.services.job_repository import JobRepository
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.portable_resume import build_portable_resume_content
from job_agent.services.profile_store import save_profile
from job_agent.services.resume_editor import ResumeEditorError
from job_agent.services.resume_preview import render_resume_preview
from tests.helpers import sample_profile


class ResumePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = sample_profile()
        self.repository = JobRepository(self.root / "jobs.sqlite3")
        row = self.repository.upsert_job(JobRecordInput(company="示例公司", title="数据实习生",
            jd_text="要求 SQL 数据分析", source="test", source_url="https://example.com/job"))
        self.job = self.repository.get_job(row.job_id)
        match = match_job_locally(self.profile, structure_job_locally(self.job.jd_text))
        self.content = build_portable_resume_content(self.profile, self.job, match)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_pdf_and_no_existing_data_mutation(self):
        private = self.root / "private"
        private.mkdir()
        marker = private / "old-draft.json"
        marker.write_text('{"approved":true}', encoding="utf-8")
        before = json.dumps(self.content)
        db_before = self.repository.get_job(self.job.job_id)
        with patch("job_agent.services.portable_resume._write_pdf_from_docx", side_effect=AssertionError("Office forbidden")):
            pdf, pages = render_resume_preview(self.content, self.job, private)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(len(PdfReader(io.BytesIO(pdf)).pages), pages)
        self.assertIn(self.profile.person.display_name, PdfReader(io.BytesIO(pdf)).pages[0].extract_text())
        self.assertEqual(before, json.dumps(self.content))
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"approved":true}')
        self.assertEqual(db_before, self.repository.get_job(self.job.job_id))
        self.assertEqual(list((private / ".resume-preview").iterdir()), [])
        self.assertFalse((private / "applications").exists())

    def test_target_mismatch_and_invalid_content_rejected(self):
        for value in [None, {}, {**self.content, "target": {"job_id": 999}},
                      {**self.content, "target": {"job_id": True}}]:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ResumeEditorError):
                render_resume_preview(value, self.job, self.root)

    def test_untrusted_photo_path_is_never_used(self):
        self.content["person"]["photo_path"] = r"\\untrusted.example\share\photo.jpg"
        with patch("job_agent.services.resume_preview.render_reference_pdf", return_value=1) as renderer:
            def render(data, spec, pdf, photo):
                self.assertIsNone(photo)
                self.assertEqual(data["person"]["photo_path"], "")
                pdf.write_bytes(b"%PDF-test")
                return 1
            renderer.side_effect = render
            render_resume_preview(self.content, self.job, self.root)

    def test_photo_outside_private_dir_rejected(self):
        self.content["person"]["photo_path"] = "include-photo"
        with patch("job_agent.services.resume_preview.find_profile_photo", return_value=self.root.parent / "outside.png"):
            with self.assertRaises(ResumeEditorError):
                render_resume_preview(self.content, self.job, self.root)

    def test_unchecked_photo_omits_existing_private_photo(self):
        self.content["person"]["photo_path"] = ""
        with patch("job_agent.services.resume_preview.find_profile_photo", return_value=self.root / "photo.png") as find_photo:
            with patch("job_agent.services.resume_preview.render_reference_pdf") as renderer:
                def render(data, spec, pdf, photo):
                    self.assertIsNone(photo)
                    pdf.write_bytes(b"%PDF-test")
                    return 1
                renderer.side_effect = render
                render_resume_preview(self.content, self.job, self.root)
        find_photo.assert_not_called()

    def test_route_rejects_unauthorized_before_read(self):
        handler = Mock()
        handler._authorized_action.return_value = False
        handle_resume_preview(handler)
        handler._reject_unauthorized_action.assert_called_once()
        handler._read_body.assert_not_called()

    def test_route_bounds_errors_and_masks_render_exception(self):
        handler = Mock()
        handler._authorized_action.return_value = True
        handler.headers = {"Content-Type": "application/json"}
        handler._read_body.return_value = json.dumps({"job_id": self.job.job_id, "content": self.content}).encode()
        with patch("job_agent.services.resume_preview.render_resume_preview", side_effect=RuntimeError("private-path-secret")):
            handle_resume_preview(handler)
        handler._read_body.assert_called_once_with(256 * 1024)
        self.assertNotIn("private-path-secret", str(handler._json.call_args))
        self.assertEqual(handler._json.call_args.args[1], 500)

    def test_http_pdf_no_store_and_foreign_origin_denied(self):
        profile_path = self.root / "profile.json"
        save_profile(self.profile, profile_path)
        server = create_dashboard_server(self.repository, output_dir=self.root / "output", port=0,
            profile_path=profile_path, private_dir=self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with opener.open(base + "/api/dashboard", timeout=10) as response:
                token = json.loads(response.read())["action_token"]
            body = json.dumps({"job_id": self.job.job_id, "content": self.content}).encode()
            for origin, expected in [(base, 200), ("https://evil.example", 403)]:
                request = urllib.request.Request(base + "/api/resume/preview", data=body,
                    headers={"Content-Type": "application/json", "X-Job-Agent-Token": token, "Origin": origin})
                try:
                    response = opener.open(request, timeout=15)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    self.assertEqual(response.status, expected)
                    if expected == 200:
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
                        self.assertEqual(response.headers["Content-Type"], "application/pdf")
                        self.assertEqual(response.headers["X-Resume-Preview-Renderer"], "local_renderer")
                        self.assertTrue(response.read().startswith(b"%PDF-"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
