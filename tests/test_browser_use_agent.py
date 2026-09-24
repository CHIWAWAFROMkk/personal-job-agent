import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from job_agent.models.job_record import JobDetail, JobSourceItem
from job_agent.services.browser_use_agent import (
    BrowserUseError,
    _approved_job_navigation,
    _run_agent_async,
    build_browser_use_task_prompt,
    check_cdp_available,
    create_browser_use_llm,
    get_chrome_launch_instructions,
    get_safe_controller_class,
    is_browser_use_available,
    is_submission_element,
    is_forbidden_input_element,
    run_browser_use_assist,
    validate_local_cdp_url,
)
from job_agent.services.runtime_config import (
    AIConnectorConfig,
    RuntimeConfig,
    RuntimeConfigError,
    save_runtime_config,
)
from job_agent.services.profile_store import save_profile
from tests.helpers import sample_profile


class BrowserUseAgentTests(unittest.TestCase):
    def setUp(self):
        self.profile = sample_profile()
        self.job = JobDetail(
            job_id=201,
            company="携程（上海）",
            title="运营管培生",
            jd_text="负责平台商家运营与活动落地，跨部门协调推进。",
            location="上海",
            status="discovered",
            created_at="2026-09-01T00:00:00Z",
            updated_at="2026-09-01T00:00:00Z",
            first_seen_at="2026-09-01T00:00:00Z",
            last_seen_at="2026-09-01T00:00:00Z",
        )

    def test_is_browser_use_available(self):
        self.assertTrue(is_browser_use_available())

    def test_check_cdp_available_not_hanging(self):
        # Port 65530 is unlikely to be listening, should return False quickly
        res = check_cdp_available("http://localhost:65530")
        self.assertFalse(res)

    def test_get_chrome_launch_instructions(self):
        instructions = get_chrome_launch_instructions(9222)
        self.assertIn("executable_path", instructions)
        self.assertIn("chrome.exe", instructions["executable_path"].lower())
        self.assertEqual(instructions["port"], "9222")
        self.assertIn("--remote-debugging-port=9222", instructions["command_powershell"])
        self.assertIn("cdp_url", instructions)

    def test_build_browser_use_task_prompt_contains_facts_and_hard_stop(self):
        resume_dummy = Path("data/private/sample_resume.pdf")
        prompt = build_browser_use_task_prompt(self.job, self.profile, resume_dummy)

        # 检查候选人真实档案与岗位信息
        self.assertIn("运营管培生", prompt)
        self.assertIn("携程（上海）", prompt)
        self.assertIn(self.profile.person.display_name, prompt)
        self.assertIn(self.profile.person.contact.phone, prompt)
        self.assertIn(self.profile.person.contact.email, prompt)
        self.assertIn(self.profile.experiences[0].role, prompt)
        self.assertIn(self.profile.experiences[0].facts[0].statement, prompt)
        self.assertNotIn(self.profile.experiences[0].facts[1].statement, prompt)

        # 检查关键安全红线（Hard Stop）
        self.assertIn("铁律安全红线", prompt)
        self.assertIn("严禁最终提交", prompt)
        self.assertIn("完成即停", prompt)
        self.assertIn("遇到验证码", prompt)
        self.assertIn("汇报审计", prompt)

    def test_create_browser_use_llm_local_raises_runtime_error(self):
        config = RuntimeConfig(ai=AIConnectorConfig(provider="local"))
        with self.assertRaises(RuntimeConfigError) as ctx:
            create_browser_use_llm(config)
        self.assertIn("需要连接云端大模型", str(ctx.exception))

    def test_create_browser_use_llm_deepseek(self):
        config = RuntimeConfig(
            ai=AIConnectorConfig(
                provider="deepseek",
                api_key="sk-test-fake-key",
                model="deepseek-chat",
            )
        )
        llm = create_browser_use_llm(config)
        self.assertIsNotNone(llm)

    def test_run_browser_use_assist_dry_run(self):
        config = RuntimeConfig(ai=AIConnectorConfig(provider="local"))
        with patch("job_agent.services.browser_use_agent.check_cdp_available") as cdp_probe:
            res = run_browser_use_assist(
                job=self.job,
                profile=self.profile,
                resume_path=None,
                config=config,
                dry_run=True,
            )
            cdp_probe.assert_not_called()
        self.assertTrue(res.success)
        self.assertEqual(res.status, "simulated")
        self.assertIn("姓名", res.filled_fields)
        self.assertNotIn("定向简历附件", res.filled_fields)
        self.assertFalse(res.actions_taken)
        self.assertFalse(res.cdp_connected)
        self.assertIn("模拟代填预览", res.summary)
        self.assertIn("未核验或上传", res.summary)

    def test_dashboard_routes_browser_use_status_and_apply(self):
        from job_agent.services.dashboard_routes.jobs_api import (
            handle_browser_use_status,
            handle_job_browser_use,
        )

        handler = MagicMock()
        handler._authorized_action.return_value = True
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        handler.runtime_config_path = root / "runtime_config.json"
        handler.profile_path = root / "profile.json"
        handler.output_dir = root / "output"
        save_profile(self.profile, handler.profile_path)
        save_runtime_config(RuntimeConfig(ai=AIConnectorConfig(provider="local")), handler.runtime_config_path)
        handler.repository = MagicMock()
        handler.repository.get_job.return_value = self.job

        # Test status GET
        handle_browser_use_status(handler)
        handler._json.assert_called()
        status_data = handler._json.call_args[0][0]
        self.assertTrue(status_data.get("ok"))
        self.assertFalse(status_data.get("browser_use_available"))
        self.assertFalse(status_data.get("live_available"))

        # Test job POST with dry_run
        handler._json.reset_mock()
        handler.headers = {"Content-Type": "application/json"}
        handler._read_body.return_value = b'{"dry_run": true}'

        handle_job_browser_use(handler, "201")
        handler._json.assert_called_once()
        post_data = handler._json.call_args[0][0]
        self.assertTrue(post_data.get("ok"))
        self.assertEqual(post_data["result"]["status"], "simulated")

        # A direct API call cannot bypass the paused live-entry UI.
        handler._json.reset_mock()
        handler._read_body.return_value = b'{"dry_run": false}'
        with patch("job_agent.services.dashboard_routes.jobs_api.run_browser_use_assist") as runner:
            handle_job_browser_use(handler, "201")
            runner.assert_not_called()
        self.assertIn("实站 AI 代填已暂停", handler._json.call_args.args[0]["error"])

    def test_is_submission_element_detects_forbidden_patterns(self):
        class MockNode:
            def __init__(self, tag="button", text="", attrs=None, parent=None):
                self.tag_name = tag
                self.attributes = attrs or {}
                self._text = text
                self.parent = parent

            def get_all_children_text(self):
                return self._text

        forbidden_samples = [
            MockNode(text="立即投递"),
            MockNode(text="确认申请"),
            MockNode(text="提交简历"),
            MockNode(text="确认提交"),
            MockNode(text="立即申请"),
            MockNode(text="直接投递"),
            MockNode(text="提交"),
            MockNode(text="Submit Application"),
            MockNode(text="Apply Now"),
            MockNode(tag="input", attrs={"type": "submit", "value": "投递"}),
            MockNode(attrs={"aria-label": "立即投递"}),
            MockNode(attrs={"id": "btn-submit-job"}),
            MockNode(attrs={"class": "apply-btn"}),
        ]
        for node in forbidden_samples:
            is_sub, reason = is_submission_element(node)
            self.assertTrue(is_sub, f"Failed to detect forbidden submission element: {reason}")

        # Hierarchy check: clicking a child span inside a submit button
        submit_parent = MockNode(text="确认提交")
        child_span = MockNode(tag="span", text="", parent=submit_parent)
        is_sub, reason = is_submission_element(child_span)
        self.assertTrue(is_sub, "Failed to detect forbidden element through parent hierarchy")

    def test_is_submission_element_allows_safe_actions(self):
        class MockNode:
            def __init__(self, tag="button", text="", attrs=None, parent=None):
                self.tag_name = tag
                self.attributes = attrs or {}
                self._text = text
                self.parent = parent

            def get_all_children_text(self):
                return self._text

        safe_samples = [
            MockNode(text="下一步"),
            MockNode(text="保存草稿"),
            MockNode(text="上传简历附件"),
            MockNode(text="选择文件"),
            MockNode(text="预览简历"),
            MockNode(tag="input", attrs={"type": "file", "id": "resume_attachment"}),
            MockNode(tag="input", attrs={"type": "text", "name": "applicant_name"}),
        ]
        for node in safe_samples:
            is_sub, reason = is_submission_element(node)
            self.assertFalse(is_sub, f"Safe element incorrectly detected as submission: {reason}")

    def test_safe_job_controller_hard_stop_interception(self):
        controller_cls = get_safe_controller_class()
        controller = controller_cls()

        # Check dangerous actions excluded
        self.assertNotIn("evaluate", controller.registry.registry.actions)
        self.assertNotIn("write_file", controller.registry.registry.actions)
        self.assertNotIn("replace_file", controller.registry.registry.actions)

        # Mock browser session and node targeting submit button
        mock_node = MagicMock()
        mock_node.tag_name = "button"
        mock_node.attributes = {}
        mock_node.get_all_children_text.return_value = "立即投递"
        mock_node.parent = None

        mock_session = MagicMock()
        mock_session.get_element_by_index = AsyncMock(return_value=mock_node)

        from browser_use.tools.views import ClickElementActionIndexOnly

        ActionModel = controller.registry.create_action_model()
        action = ActionModel(click=ClickElementActionIndexOnly(index=7))

        res = asyncio.run(controller.act(action, mock_session))
        self.assertIsNotNone(res)
        self.assertIn("Hard Stop", res.error)
        self.assertIn("立即投递", res.error)
        self.assertEqual(len(controller.blocked_attempts), 1)

    def test_browser_navigation_is_limited_to_one_verified_job_url(self):
        from browser_use import ActionResult, Controller
        from browser_use.tools.views import NavigateAction, UploadFileAction

        approved_url = "https://careers.example.org/job/201"
        controller = get_safe_controller_class()(
            approved_first_url=approved_url,
            approved_resume_path="C:/approved.pdf",
        )
        ActionModel = controller.registry.create_action_model()
        approved = ActionModel(navigate=NavigateAction(url=approved_url, new_tab=True))
        wrong_url = ActionModel(navigate=NavigateAction(url="https://other.example.org/", new_tab=True))
        wrong_file = ActionModel(upload_file=UploadFileAction(index=1, path="C:/private.pdf"))
        with patch.object(Controller, "act", new_callable=AsyncMock) as delegated:
            delegated.return_value = ActionResult(extracted_content="opened")
            result = asyncio.run(controller.act(approved, MagicMock()))
            self.assertEqual(result.extracted_content, "opened")
            self.assertEqual(delegated.await_count, 1)
            self.assertIn("仅允许首次", asyncio.run(controller.act(approved, MagicMock())).error)
            self.assertIn("本轮已停止", asyncio.run(controller.act(wrong_url, MagicMock())).error)
        self.assertIn("本轮已停止", asyncio.run(controller.act(wrong_file, MagicMock())).error)
        fresh = get_safe_controller_class()(
            approved_first_url=approved_url,
            approved_resume_path="C:/approved.pdf",
        )
        self.assertIn("仅允许首次", asyncio.run(fresh.act(wrong_url, MagicMock())).error)
        fresh = get_safe_controller_class()(
            approved_first_url=approved_url,
            approved_resume_path="C:/approved.pdf",
        )
        self.assertIn("仅允许上传", asyncio.run(fresh.act(wrong_file, MagicMock())).error)

    def test_browser_input_blocks_captcha_and_sensitive_fields(self):
        from browser_use.tools.views import InputTextAction

        controller = get_safe_controller_class()()
        ActionModel = controller.registry.create_action_model()
        node = MagicMock()
        node.attributes = {"id": "sms_verification_code", "type": "text"}
        node.tag_name = "input"
        node.get_all_children_text.return_value = ""
        node.parent = None
        session = MagicMock()
        session.get_selector_map = AsyncMock(return_value={5: node})
        action = ActionModel(input=InputTextAction(index=5, text="123456"))
        result = asyncio.run(controller.act(action, session))
        self.assertIn("验证码", result.error)
        self.assertEqual(len(controller.blocked_attempts), 1)
        self.assertTrue(is_forbidden_input_element(node)[0])

    def test_live_browser_agent_uses_exact_navigation_and_one_pdf_allowlist(self):
        from urllib.parse import urlsplit

        url = "https://careers.example.org/job/201"
        self.job.sources = [JobSourceItem(
            platform="official", source_url=url,
            first_seen_at="2026-09-01T00:00:00Z",
            last_seen_at="2026-09-01T00:00:00Z",
        )]
        with tempfile.TemporaryDirectory() as folder:
            resume = Path(folder) / "approved.pdf"
            resume.write_bytes(b"%PDF-1.4\nsynthetic test\n")
            history = MagicMock()
            history.action_names.return_value = ["navigate", "done"]
            history.final_result.return_value = "Stopped for review"
            history.agent_steps = []
            with (
                patch("job_agent.services.browser_use_agent.resolve_public", return_value=(urlsplit(url), "careers.example.org", 443, object())),
                patch("job_agent.services.browser_use_agent.create_browser_use_llm", return_value=MagicMock()),
                patch("job_agent.services.browser_use_agent.check_cdp_available", return_value=True),
                patch("browser_use.Browser") as browser_class,
                patch("browser_use.Agent") as agent_class,
            ):
                agent_class.return_value.run = AsyncMock(return_value=history)
                result = asyncio.run(_run_agent_async(
                    self.job, self.profile, resume,
                    RuntimeConfig(ai=AIConnectorConfig(provider="openai", api_key="fake", model="gpt-4o")),
                ))
                self.assertTrue(result.success)
                self.assertEqual(browser_class.call_args.kwargs["allowed_domains"], ["careers.example.org"])
                self.assertFalse(browser_class.call_args.kwargs["captcha_solver"])
                options = agent_class.call_args.kwargs
                self.assertFalse(options["directly_open_url"])
                self.assertEqual(options["initial_actions"], [{"navigate": {"url": url, "new_tab": True}}])
                self.assertEqual(options["available_file_paths"], [str(resume.resolve())])
                self.assertEqual(options["controller"].approved_resume_path, str(resume.resolve()))

    def test_live_browser_rejects_missing_or_insecure_job_url(self):
        with self.assertRaises(BrowserUseError):
            _approved_job_navigation(self.job)

    def test_remote_browser_debugger_is_rejected(self):
        self.assertEqual(validate_local_cdp_url("http://127.0.0.1:9222"), "http://127.0.0.1:9222")
        for url in ("http://example.org:9222", "https://localhost:9222", "http://localhost:9222/x"):
            with self.subTest(url=url), self.assertRaises(BrowserUseError):
                validate_local_cdp_url(url)

    def test_browser_upload_requires_latest_approved_hash_checked_pdf(self):
        from job_agent.services.dashboard_routes.jobs_api import _verified_browser_use_resume

        with tempfile.TemporaryDirectory() as folder:
            output_dir = Path(folder)
            version_dir = output_dir / "applications" / "job-201"
            version_dir.mkdir(parents=True)
            pdf_path = version_dir / "approved.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nsynthetic test\n")
            digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest().upper()
            docx_path = version_dir / "approved.docx"
            docx_path.write_bytes(b"synthetic docx placeholder")
            docx_digest = hashlib.sha256(docx_path.read_bytes()).hexdigest().upper()
            content_path = version_dir / "resume-content-portable.json"
            content_path.write_text('{"summary":"synthetic"}', encoding="utf-8")
            content_digest = hashlib.sha256(content_path.read_bytes()).hexdigest().upper()
            manifest_path = version_dir / "resume-version-test.json"
            manifest = {
                "target": {"job_id": 201},
                "qa": {"pdf_visual_review": "passed", "truthfulness_check": "passed"},
                "generation": {"engine": "local_selection"},
                "content_source": content_path.name,
                "content_sha256": content_digest,
                "artifacts": {
                    "pdf": {"path": pdf_path.name, "sha256": digest},
                    "docx": {"path": docx_path.name, "sha256": docx_digest},
                },
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(_verified_browser_use_resume(output_dir, 201), pdf_path.resolve())

            manifest["generation"]["engine"] = "cloud_composition"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(BrowserUseError):
                _verified_browser_use_resume(output_dir, 201)
            manifest["fact_approval"] = {
                "approved_by": "user", "approved_at": "2026-09-23T00:00:00Z",
                "pdf_sha256": digest,
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(BrowserUseError):
                _verified_browser_use_resume(output_dir, 201)
            manifest["fact_approval"]["content_sha256"] = content_digest
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(_verified_browser_use_resume(output_dir, 201), pdf_path.resolve())

            pdf_path.write_bytes(b"%PDF-1.4\nchanged file\n")
            with self.assertRaises(BrowserUseError):
                _verified_browser_use_resume(output_dir, 201)
        self.job.sources = [JobSourceItem(
            platform="unverified", source_url="http://127.0.0.1:9999/apply",
            first_seen_at="2026-09-01T00:00:00Z",
            last_seen_at="2026-09-01T00:00:00Z",
        )]
        with self.assertRaises(BrowserUseError):
            _approved_job_navigation(self.job)


if __name__ == "__main__":
    unittest.main()
