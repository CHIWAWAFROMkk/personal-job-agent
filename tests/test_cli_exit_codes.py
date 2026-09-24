from __future__ import annotations

import unittest
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx
from openai import AuthenticationError

from pydantic import ValidationError

from job_agent.cli import _build_parser, _jobs_command, main
from job_agent.constants import ExitCode
from job_agent.services.api_usage import ApiQuotaExceededError, load_api_usage
from job_agent.models.job import ScoreBreakdown, StructuredJob
from job_agent.services.openai_matcher import AIJobAnalysis
from job_agent.services.job_search_provider import JobSearchProviderError
from job_agent.services.profile_store import ProfileStoreError, save_profile
from job_agent.services.runtime_config import AIConnectorConfig, RuntimeConfig, SearchConnectorConfig
from tests.helpers import sample_profile


class CliExitCodesTests(unittest.TestCase):
    def test_cli_match_401_releases_slot_and_corrected_key_retries(self):
        analysis = AIJobAnalysis(
            job=StructuredJob(company="合成公司", title="SQL 实习生"),
            score_breakdown=ScoreBreakdown(
                role_direction=10, skills=10, experience=5,
                education=5, logistics=5, preferences=2,
            ),
        )
        for provider in ("openai", "deepseek"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                private = root / "private"
                profile_path = private / "profile.json"
                save_profile(sample_profile(), profile_path)
                jd_path = root / "jd.txt"
                jd_path.write_text("SQL 数据分析实习岗位，使用 SQL 清洗数据。", encoding="utf-8")
                endpoint = "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com"
                auth_error = AuthenticationError(
                    "synthetic invalid key",
                    response=httpx.Response(401, request=httpx.Request("POST", endpoint + "/v1/chat/completions")),
                    body=None,
                )
                settings = SimpleNamespace(
                    project_root=root, profile_path=profile_path, private_dir=private,
                    runtime_config_path=private / "app-settings.json",
                    ai_provider=provider, ai_model="synthetic-model", ai_api_key="synthetic-wrong",
                    ai_base_url=endpoint if provider == "deepseek" else None,
                )
                config = RuntimeConfig(ai=AIConnectorConfig(
                    provider=provider, model="synthetic-model", api_key="synthetic-wrong", monthly_quota=1,
                ))
                client = MagicMock()
                if provider == "openai":
                    sdk_call = client.responses.parse
                    success = SimpleNamespace(output_parsed=analysis, usage=None)
                else:
                    sdk_call = client.chat.completions.create
                    success = SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content=analysis.model_dump_json()))],
                        usage=None,
                    )
                sdk_call.side_effect = [auth_error, success]
                args = [
                    "match", str(jd_path), "--engine", "ai", "--provider", provider,
                    "--company", "合成公司", "--title", "SQL 实习生",
                    "--output", str(root / "match.json"),
                ]
                with patch("job_agent.cli.get_settings", return_value=settings), \
                     patch("job_agent.cli.effective_runtime_config", return_value=(config, None)), \
                     patch("job_agent.cli.configure_logging"), \
                     patch("openai.OpenAI", return_value=client), \
                     redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as first:
                        main(args)
                    self.assertEqual(first.exception.code, ExitCode.ERROR)
                    usage_path = private / "api-usage.json"
                    self.assertFalse(load_api_usage(usage_path).pending)
                    settings.ai_api_key = "synthetic-correct"
                    with self.assertRaises(SystemExit) as retry:
                        main(args)
                self.assertEqual(retry.exception.code, ExitCode.OK)
                self.assertEqual(sdk_call.call_count, 2)
                usage = load_api_usage(usage_path)
                self.assertEqual(usage.ai.successful_requests, 1)
                self.assertFalse(usage.pending)
                self.assertTrue((root / "match.json").is_file())

    def test_cli_search_stops_before_second_paid_call_at_quota(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            private = Path(temp_dir)
            profile_path = private / "profile.json"
            save_profile(sample_profile(), profile_path)
            settings = SimpleNamespace(
                profile_path=profile_path, job_db_path=private / "jobs.sqlite3",
                private_dir=private, runtime_config_path=private / "app-settings.json",
                search_provider="bocha", bocha_api_key="test-only-key",
                brave_search_api_key=None,
            )
            config = RuntimeConfig(
                ai=AIConnectorConfig(provider="local"),
                search=SearchConnectorConfig(provider="bocha", api_key="test-only-key", monthly_quota=1),
            )
            provider = Mock()
            provider.search.return_value = []
            args = _build_parser().parse_args(["jobs", "discover", "--max-queries", "2"])
            with patch("job_agent.cli.get_settings", return_value=settings), \
                 patch("job_agent.cli.effective_runtime_config", return_value=(config, "test")), \
                 patch("job_agent.cli.build_job_search_provider", return_value=provider), \
                 redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = _jobs_command(args)
            self.assertEqual(code, ExitCode.UNAVAILABLE)
            self.assertEqual(provider.search.call_count, 1)
            self.assertEqual(load_api_usage(private / "api-usage.json").search.successful_requests, 1)

    def test_quota_rejection_uses_unavailable_exit_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(project_root=Path(temp_dir))
            with patch("job_agent.cli.get_settings", return_value=settings), \
                 patch("job_agent.cli.configure_logging"), \
                 patch("job_agent.cli._jobs_command", side_effect=ApiQuotaExceededError("本月 AI 请求额度已用完。")):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as ctx:
                    main(["jobs", "rescore", "0"])
        self.assertEqual(ctx.exception.code, ExitCode.UNAVAILABLE)

    def test_startup_config_failure_is_reported(self):
        stderr = StringIO()
        with patch("job_agent.cli.get_settings", side_effect=OSError("bad config")):
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                main(["doctor"])
        self.assertEqual(ctx.exception.code, ExitCode.CONFIG)
        self.assertIn("bad config", stderr.getvalue())

    def test_logging_failure_is_visible_but_does_not_block_doctor(self):
        stderr = StringIO()
        settings = SimpleNamespace(project_root=Path(".").resolve())
        with patch("job_agent.cli.get_settings", return_value=settings):
            with patch("job_agent.cli.configure_logging", side_effect=OSError("log unavailable")):
                with patch("job_agent.cli._doctor", return_value=ExitCode.OK):
                    with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                        main(["doctor"])
        self.assertEqual(ctx.exception.code, ExitCode.OK)
        self.assertIn("log unavailable", stderr.getvalue())

    def test_missing_profile_exits_with_config_code(self):
        with patch("job_agent.cli._profile_command", side_effect=ProfileStoreError("Profile 不存在")):
            with self.assertRaises(SystemExit) as ctx:
                main(["profile", "summary"])
            self.assertEqual(ctx.exception.code, ExitCode.CONFIG)

    def test_validation_or_value_error_exits_with_data_err_code(self):
        with patch("job_agent.cli._jobs_command", side_effect=ValueError("岗位编号必须大于 0")):
            with self.assertRaises(SystemExit) as ctx:
                main(["jobs", "rescore", "0"])
            self.assertEqual(ctx.exception.code, ExitCode.DATA_ERR)

    def test_search_provider_error_exits_with_unavailable_code(self):
        with patch("job_agent.cli._jobs_command", side_effect=JobSearchProviderError("Network timeout")):
            with self.assertRaises(SystemExit) as ctx:
                main(["jobs", "discover"])
            self.assertEqual(ctx.exception.code, ExitCode.UNAVAILABLE)

    def test_runtime_error_exits_with_general_error_code(self):
        with patch("job_agent.cli._doctor", side_effect=RuntimeError("Unexpected crash")):
            with self.assertRaises(SystemExit) as ctx:
                main(["doctor"])
            self.assertEqual(ctx.exception.code, ExitCode.ERROR)


if __name__ == "__main__":
    unittest.main()
