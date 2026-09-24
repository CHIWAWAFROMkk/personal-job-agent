from __future__ import annotations

import tempfile
import unittest
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Condition, Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import subprocess
import sys
import httpx
from openai import AuthenticationError
from job_agent.services.openai_matcher import OpenAIMatcherError

from job_agent.services.api_usage import (
    ApiQuotaExceededError, ApiUsageUnavailableError,
    load_api_usage, public_api_usage,
    record_api_usage, reserve_api_usage,
)
from tests.helpers import sample_profile


class ApiUsageTests(unittest.TestCase):
    @staticmethod
    def _wrapped_match_error(cause: Exception) -> OpenAIMatcherError:
        try:
            raise OpenAIMatcherError("岗位分析失败") from cause
        except OpenAIMatcherError as exc:
            return exc

    def test_wrapped_timeout_or_custom_endpoint_401_keeps_quota_uncertain(self) -> None:
        cases = (
            ("openai", self._wrapped_match_error(TimeoutError("provider timeout"))),
            ("openai", OpenAIMatcherError("401 mentioned in text only")),
            ("deepseek", self._wrapped_match_error(self._authentication_error("https://custom.example"))),
        )
        for provider, error in cases:
            with self.subTest(provider=provider, error=str(error)), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "api-usage.json"
                with self.assertRaises(OpenAIMatcherError):
                    with reserve_api_usage(path, "ai", provider, 1) as reservation:
                        reservation.call(lambda: (_ for _ in ()).throw(error))
                self.assertEqual(next(iter(load_api_usage(path).pending.values())).state, "uncertain")

    @staticmethod
    def _authentication_error(endpoint: str = "https://api.openai.com") -> AuthenticationError:
        request = httpx.Request("POST", endpoint + "/v1/chat/completions")
        return AuthenticationError(
            "synthetic invalid API key",
            response=httpx.Response(401, request=request),
            body={"error": {"type": "invalid_api_key"}},
        )

    def test_definitive_401_releases_quota_for_corrected_first_party_key(self) -> None:
        for provider in ("openai", "deepseek"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "api-usage.json"
                with self.assertRaises(AuthenticationError):
                    with reserve_api_usage(path, "ai", provider, 1) as reservation:
                        endpoint = "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com"
                        reservation.call(lambda: (_ for _ in ()).throw(self._authentication_error(endpoint)))
                usage = load_api_usage(path)
                self.assertEqual(usage.ai.successful_requests, 0)
                self.assertFalse(usage.pending)
                with reserve_api_usage(path, "ai", provider, 1) as retry:
                    retry.call(lambda: SimpleNamespace(usage=None))
                self.assertEqual(load_api_usage(path).ai.successful_requests, 1)

    def test_custom_compatible_provider_401_remains_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            with self.assertRaises(AuthenticationError):
                with reserve_api_usage(path, "ai", "openai_compatible", 1) as reservation:
                    reservation.call(lambda: (_ for _ in ()).throw(self._authentication_error()))
            usage = load_api_usage(path)
            self.assertEqual(usage.ai.successful_requests, 0)
            self.assertEqual(len(usage.pending), 1)
            self.assertEqual(next(iter(usage.pending.values())).state, "uncertain")
            with self.assertRaises(ApiQuotaExceededError):
                reserve_api_usage(path, "ai", "openai_compatible", 1)

    def test_custom_deepseek_endpoint_401_remains_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            with self.assertRaises(AuthenticationError):
                with reserve_api_usage(path, "ai", "deepseek", 1) as reservation:
                    reservation.call(lambda: (_ for _ in ()).throw(self._authentication_error("https://custom.example")))
            self.assertEqual(next(iter(load_api_usage(path).pending.values())).state, "uncertain")

    def test_resume_compose_polish_import_success_settles_once(self) -> None:
        from job_agent.services.dashboard_routes.jobs_api import (
            _compose_resume_with_quota, handle_resume_import, handle_resume_polish,
        )
        from job_agent.services.runtime_config import AIConnectorConfig, RuntimeConfig

        config = RuntimeConfig(ai=AIConnectorConfig(
            provider="openai", model="test-model", api_key="test-only-key", monthly_quota=1,
        ))
        response = SimpleNamespace(usage=SimpleNamespace(input_tokens=5, output_tokens=2))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            handler = MagicMock()
            handler._authorized_action.return_value = True
            handler.output_dir = root
            handler.runtime_config_path = root / "settings.json"
            handler.repository.get_job.return_value = SimpleNamespace(jd_text="合成岗位要求")
            handler.headers = {"Content-Type": "application/json"}

            def compose(*_args, request_call=None, **_kwargs):
                request_call(lambda: response)
                return SimpleNamespace(input_tokens=5, output_tokens=2)

            handler.api_usage_path = root / "compose.json"
            with patch("job_agent.services.dashboard_routes.jobs_api.compose_resume_content_with_jd", side_effect=compose):
                _compose_resume_with_quota(handler, {}, "合成 JD", profile=None, config=config, user_instruction="")
            self.assertEqual(load_api_usage(handler.api_usage_path).ai.successful_requests, 1)

            def polish(*_args, request_call=None, **_kwargs):
                request_call(lambda: response)
                return SimpleNamespace(content={"target": {"job_id": 1}}, changes=[], input_tokens=5, output_tokens=2)

            handler.api_usage_path = root / "polish.json"
            handler._read_body.return_value = b'{"confirmed":true}'
            with patch("job_agent.services.dashboard_routes.jobs_api.load_latest_resume_content", return_value=({}, None)), \
                 patch("job_agent.services.dashboard_routes.jobs_api.effective_runtime_config", return_value=(config, None)), \
                 patch("job_agent.services.dashboard_routes.jobs_api.polish_resume_content_with_jd", side_effect=polish):
                handle_resume_polish(handler, "1")
            self.assertTrue(handler._json.call_args.args[0]["ok"])
            self.assertEqual(load_api_usage(handler.api_usage_path).ai.successful_requests, 1)

            def import_draft(*_args, request_call=None, **_kwargs):
                request_call(lambda: response)
                return SimpleNamespace(content={"target": {"job_id": 1}}, warnings=[], input_tokens=5, output_tokens=2)

            handler.api_usage_path = root / "import.json"
            handler._read_body.return_value = b'{"resume_text":"synthetic resume"}'
            with patch("job_agent.services.dashboard_routes.jobs_api.load_latest_resume_content", return_value=({}, None)), \
                 patch("job_agent.services.dashboard_routes.jobs_api.effective_runtime_config", return_value=(config, None)), \
                 patch("job_agent.services.dashboard_routes.jobs_api.import_resume_text_into_draft", side_effect=import_draft):
                handle_resume_import(handler, "1")
            self.assertTrue(handler._json.call_args.args[0]["ok"])
            self.assertEqual(load_api_usage(handler.api_usage_path).ai.successful_requests, 1)

    def test_valid_version_one_record_is_preserved_and_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            path.write_text(json.dumps({
                "schema_version": "1", "month": date.today().strftime("%Y-%m"),
                "ai": {"provider": "synthetic", "successful_requests": 2, "input_tokens": 8, "output_tokens": 3},
                "search": {"successful_requests": 0}, "maps": {"successful_requests": 0},
            }), encoding="utf-8")
            with reserve_api_usage(path, "ai", "synthetic", 3) as reservation:
                reservation.commit(input_tokens=4, output_tokens=2)
            usage = load_api_usage(path)
            self.assertEqual(usage.schema_version, "2")
            self.assertEqual((usage.ai.successful_requests, usage.ai.input_tokens, usage.ai.output_tokens), (3, 12, 5))

    def test_invalid_month_and_missing_count_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            for month in ("2026-13", "corrupt", "0000-01"):
                path.write_text(json.dumps({
                    "schema_version": "1", "month": month,
                    "ai": {"successful_requests": 100},
                    "search": {"successful_requests": 0},
                    "maps": {"successful_requests": 0},
                }), encoding="utf-8")
                with self.subTest(month=month), self.assertRaises(ApiUsageUnavailableError):
                    reserve_api_usage(path, "ai", "test", 1)
            path.write_text(json.dumps({
                "schema_version": "1", "month": "2026-09",
                "ai": {}, "search": {"successful_requests": 0},
                "maps": {"successful_requests": 0},
            }), encoding="utf-8")
            with self.assertRaises(ApiUsageUnavailableError):
                reserve_api_usage(path, "ai", "test", 1)
            path.write_text(json.dumps({
                "schema_version": "99", "month": date.today().strftime("%Y-%m"),
                "ai": {"successful_requests": 100},
                "search": {"successful_requests": 0},
                "maps": {"successful_requests": 0},
            }), encoding="utf-8")
            with self.assertRaises(ApiUsageUnavailableError):
                reserve_api_usage(path, "ai", "test", 1)
            path.write_text(json.dumps({
                "schema_version": "2", "month": date.today().strftime("%Y-%m"),
                "ai": {"successful_requests": 100},
                "search": {"successful_requests": 0},
                "maps": {"successful_requests": 0},
            }), encoding="utf-8")
            with self.assertRaises(ApiUsageUnavailableError):
                reserve_api_usage(path, "ai", "test", 1)

    def test_malformed_cloud_responses_still_consume_quota(self) -> None:
        from job_agent.services.dashboard_routes.jobs_api import (
            _greetings_with_quota, _interview_prep_with_quota,
        )
        from job_agent.services.runtime_config import AIConnectorConfig, RuntimeConfig

        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"invalid":true}'))],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)))
        job = SimpleNamespace(job_id=1, company="合成企业", title="合成岗位", jd_text="合成岗位说明")
        config = RuntimeConfig(ai=AIConnectorConfig(
            provider="openai", model="test-model", api_key="test-only-key", monthly_quota=1,
        ))
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = SimpleNamespace(api_usage_path=Path(temp_dir) / "greeting.json")
            with patch("job_agent.services.outreach_copilot.get_openai_client", return_value=(client, "test-model")) as provider:
                self.assertEqual(_greetings_with_quota(handler, job, sample_profile(), config).engine, "local_template")
                self.assertEqual(_greetings_with_quota(handler, job, sample_profile(), config).engine, "local_template")
                self.assertEqual(provider.call_count, 1)
            greeting = load_api_usage(handler.api_usage_path).ai
            self.assertEqual((greeting.successful_requests, greeting.input_tokens, greeting.output_tokens), (1, 7, 3))

            handler.api_usage_path = Path(temp_dir) / "interview.json"
            with patch("job_agent.services.interview_copilot.get_openai_client", return_value=(client, "test-model")) as provider:
                self.assertEqual(_interview_prep_with_quota(handler, job, sample_profile(), config).engine, "local_template")
                self.assertEqual(_interview_prep_with_quota(handler, job, sample_profile(), config).engine, "local_template")
                self.assertEqual(provider.call_count, 1)
            interview = load_api_usage(handler.api_usage_path).ai
            self.assertEqual((interview.successful_requests, interview.input_tokens, interview.output_tokens), (1, 7, 3))

    def test_timeout_after_dispatch_is_visible_as_uncertain_and_retains_quota(self) -> None:
        from job_agent.services.dashboard_routes.jobs_api import _greetings_with_quota
        from job_agent.services.runtime_config import AIConnectorConfig, RuntimeConfig

        def timeout(**_kwargs):
            raise TimeoutError("synthetic provider timeout")
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=timeout)))
        config = RuntimeConfig(ai=AIConnectorConfig(
            provider="openai", model="test-model", api_key="test-only-key", monthly_quota=1,
        ))
        job = SimpleNamespace(job_id=1, company="合成企业", title="合成岗位", jd_text="合成岗位说明")
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = SimpleNamespace(api_usage_path=Path(temp_dir) / "api-usage.json")
            with patch("job_agent.services.outreach_copilot.get_openai_client", return_value=(client, "test-model")) as provider:
                self.assertEqual(_greetings_with_quota(handler, job, sample_profile(), config).engine, "local_template")
                self.assertEqual(_greetings_with_quota(handler, job, sample_profile(), config).engine, "local_template")
                self.assertEqual(provider.call_count, 1)
            self.assertEqual(load_api_usage(handler.api_usage_path).ai.successful_requests, 0)
            public = public_api_usage(handler.api_usage_path, {"ai": 1, "search": None, "maps": None})
            ai = public["connectors"]["ai"]
            self.assertEqual(ai["pending_requests"], 1)
            self.assertEqual(ai["uncertain_requests"], 1)
            self.assertEqual(ai["local_remaining"], 0)

    def test_greeting_401_fallback_does_not_block_corrected_request(self) -> None:
        from job_agent.services.dashboard_routes.jobs_api import _greetings_with_quota
        from job_agent.services.runtime_config import AIConnectorConfig, RuntimeConfig

        success = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "greetings": [
                    {"style": "tech_match", "title": "A", "content": "A"},
                    {"style": "problem_solving", "title": "B", "content": "B"},
                    {"style": "passion_growth", "title": "C", "content": "C"},
                ],
            })))],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
        )
        create = MagicMock(side_effect=[self._authentication_error(), success])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        config = RuntimeConfig(ai=AIConnectorConfig(
            provider="openai", model="test-model", api_key="test-only-key", monthly_quota=1,
        ))
        job = SimpleNamespace(job_id=1, company="合成企业", title="合成岗位", jd_text="合成岗位说明")
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = SimpleNamespace(api_usage_path=Path(temp_dir) / "api-usage.json")
            with patch("job_agent.services.outreach_copilot.get_openai_client", return_value=(client, "test-model")):
                first = _greetings_with_quota(handler, job, sample_profile(), config)
                self.assertEqual(first.engine, "local_template")
                self.assertFalse(load_api_usage(handler.api_usage_path).pending)
                second = _greetings_with_quota(handler, job, sample_profile(), config)
                self.assertEqual(second.engine, "openai:test-model")
            self.assertEqual(create.call_count, 2)
            self.assertEqual(load_api_usage(handler.api_usage_path).ai.successful_requests, 1)

    def test_successful_response_with_failed_settlement_keeps_durable_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            with reserve_api_usage(path, "ai", "synthetic", 1) as reservation:
                with patch("job_agent.services.api_usage._locked_usage", side_effect=TimeoutError("lock busy")):
                    with self.assertRaisesRegex(TimeoutError, "lock busy"):
                        reservation.call(lambda: SimpleNamespace(usage=None))
            usage = load_api_usage(path)
            self.assertEqual(usage.ai.successful_requests, 0)
            self.assertEqual(len(usage.pending), 1)
            with self.assertRaises(ApiQuotaExceededError):
                reserve_api_usage(path, "ai", "synthetic", 1)

    def test_failed_uncertain_update_does_not_release_sent_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            with reserve_api_usage(path, "ai", "synthetic", 1) as reservation:
                with patch("job_agent.services.api_usage._locked_usage", side_effect=TimeoutError("lock busy")):
                    with self.assertRaisesRegex(TimeoutError, "lock busy"):
                        reservation.call(lambda: (_ for _ in ()).throw(TimeoutError("provider timeout")))
            self.assertEqual(len(load_api_usage(path).pending), 1)
            with self.assertRaises(ApiQuotaExceededError):
                reserve_api_usage(path, "ai", "synthetic", 1)

    def test_invalid_settlement_metadata_cannot_release_possible_paid_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            with reserve_api_usage(path, "maps", "amap", 1) as reservation:
                with self.assertRaises(ValueError):
                    reservation.commit(successful_requests=2)
            self.assertEqual(len(load_api_usage(path).pending), 1)
            with self.assertRaises(ApiQuotaExceededError):
                reserve_api_usage(path, "maps", "amap", 1)

    def test_damaged_existing_usage_file_fails_closed_and_reports_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            path.write_text("{damaged", encoding="utf-8")
            with self.assertRaises(ApiUsageUnavailableError):
                reserve_api_usage(path, "ai", "test", 1)
            with self.assertRaises(ApiUsageUnavailableError):
                record_api_usage(path, "ai", "test")
            public = public_api_usage(path, {"ai": 1, "search": None, "maps": None})
            ai = public["connectors"]["ai"]  # type: ignore[index]
            self.assertEqual(ai["record_status"], "unavailable")
            self.assertIsNone(ai["successful_requests"])
            self.assertIsNone(ai["local_remaining"])
            self.assertEqual(ai["local_monthly_limit"], 1)
            self.assertEqual(ai["balance_source"], "local_limit")
            self.assertIn("云端请求已暂停", public["notice"])

    def test_extra_ai_routes_count_remote_result_and_fall_back_at_quota(self) -> None:
        from job_agent.services.dashboard_routes.jobs_api import (
            _greetings_with_quota, _interview_prep_with_quota,
        )
        from job_agent.services.runtime_config import AIConnectorConfig, RuntimeConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config = RuntimeConfig(ai=AIConnectorConfig(
                provider="openai", model="test-model", api_key="test-only-key",
                monthly_quota=1,
            ))
            handler = SimpleNamespace(api_usage_path=Path(temp_dir) / "usage.json")
            providers: list[str] = []

            def greeting(_job, _profile, selected_config, *, on_cloud_response=None, on_cloud_failure=None):
                providers.append(selected_config.ai.provider)
                engine = "openai:test-model" if selected_config.ai.provider == "openai" else "local_template"
                if engine != "local_template":
                    on_cloud_response(SimpleNamespace(usage=None))
                return SimpleNamespace(engine=engine, greetings=[])

            with patch("job_agent.services.outreach_copilot.generate_greetings", side_effect=greeting):
                self.assertEqual(_greetings_with_quota(handler, None, None, config).engine, "openai:test-model")
                self.assertEqual(_greetings_with_quota(handler, None, None, config).engine, "local_template")
            self.assertEqual(providers, ["openai", "local"])
            self.assertEqual(config.ai.provider, "openai")
            self.assertEqual(load_api_usage(handler.api_usage_path).ai.successful_requests, 1)

            handler.api_usage_path = Path(temp_dir) / "interview-usage.json"
            def interview_result(_job, _profile, selected_config, *, on_cloud_response=None, on_cloud_failure=None):
                if selected_config.ai.provider == "openai":
                    on_cloud_response(SimpleNamespace(usage=None))
                    return SimpleNamespace(engine="openai:test-model")
                return SimpleNamespace(engine="local_template")

            with patch(
                "job_agent.services.interview_copilot.generate_interview_prep",
                side_effect=interview_result,
            ) as interview:
                self.assertEqual(_interview_prep_with_quota(handler, None, None, config).engine, "openai:test-model")
                self.assertEqual(_interview_prep_with_quota(handler, None, None, config).engine, "local_template")
                self.assertEqual(interview.call_count, 2)
            self.assertEqual(load_api_usage(handler.api_usage_path).ai.successful_requests, 1)

    def test_concurrent_reservations_do_not_exceed_local_quota(self) -> None:
        workers = 12
        quota = 3
        start = Barrier(workers + 1)
        state_changed = Condition()
        release = Event()
        entered = 0
        rejected = 0
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "private" / "api-usage.json"

            def invoke() -> bool:
                nonlocal entered, rejected
                start.wait(timeout=10)
                try:
                    with reserve_api_usage(path, "ai", "test", quota) as reservation:
                        with state_changed:
                            entered += 1
                            state_changed.notify_all()
                        if not release.wait(timeout=10):
                            raise TimeoutError("test did not release calls")
                        reservation.commit(input_tokens=2)
                        return True
                except ApiQuotaExceededError:
                    with state_changed:
                        rejected += 1
                        state_changed.notify_all()
                    return False

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(invoke) for _ in range(workers)]
                start.wait(timeout=10)
                with state_changed:
                    self.assertTrue(
                        state_changed.wait_for(
                            lambda: entered + rejected == workers, timeout=10,
                        )
                    )
                    self.assertEqual(entered, quota)
                    self.assertEqual(rejected, workers - quota)
                public = public_api_usage(path, {"ai": quota, "search": None, "maps": None})
                ai = public["connectors"]["ai"]  # type: ignore[index]
                self.assertEqual(ai["in_flight_requests"], quota)
                self.assertEqual(ai["local_remaining"], 0)
                release.set()
                self.assertEqual(sum(future.result(timeout=10) for future in futures), quota)

            usage = load_api_usage(path)
            self.assertEqual(usage.ai.successful_requests, quota)
            self.assertEqual(usage.ai.input_tokens, quota * 2)
            with self.assertRaises(ApiQuotaExceededError):
                reserve_api_usage(path, "ai", "test", quota)

    def test_two_processes_share_quota_without_losing_a_record(self) -> None:
        script = """
import sys, time
from pathlib import Path
from job_agent.services.api_usage import ApiQuotaExceededError, reserve_api_usage
path, gate, quota = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
while not gate.exists():
    time.sleep(0.01)
try:
    with reserve_api_usage(path, 'ai', 'synthetic', quota) as reservation:
        time.sleep(0.2)
        reservation.commit()
    print('admitted')
except ApiQuotaExceededError:
    print('capped')
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            for quota in (1, 2):
                path = Path(temp_dir) / f"api-usage-{quota}.json"
                gate = Path(temp_dir) / f"go-{quota}"
                children = [subprocess.Popen(
                    [sys.executable, "-c", script, str(path), str(gate), str(quota)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ) for _ in range(2)]
                gate.write_text("go", encoding="utf-8")
                results = [child.communicate(timeout=15) for child in children]
                for child, (_stdout, stderr) in zip(children, results):
                    self.assertEqual(child.returncode, 0, stderr)
                expected = ["admitted"] * quota + ["capped"] * (2 - quota)
                self.assertCountEqual([stdout.strip() for stdout, _ in results], expected)
                self.assertEqual(load_api_usage(path).ai.successful_requests, quota)

    def test_crashed_process_keeps_pending_slot_until_review(self) -> None:
        script = """
import os, sys
from pathlib import Path
from job_agent.services.api_usage import reserve_api_usage
reserve_api_usage(Path(sys.argv[1]), 'ai', 'synthetic', 1)
os._exit(0)
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            child = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            usage = load_api_usage(path)
            self.assertEqual(len(usage.pending), 1)
            with self.assertRaises(ApiQuotaExceededError):
                reserve_api_usage(path, "ai", "synthetic", 1)
            public = public_api_usage(path, {"ai": 1, "search": None, "maps": None})
            self.assertEqual(public["connectors"]["ai"]["local_remaining"], 0)
            self.assertEqual(public["connectors"]["ai"]["in_flight_requests"], 0)
            self.assertEqual(public["connectors"]["ai"]["other_or_unreconciled_requests"], 1)
            self.assertIn("核对服务商控制台", public["notice"])

    def test_month_rollover_keeps_unsettled_request_reserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            with patch("job_agent.services.api_usage._month_now", return_value="2026-09"):
                reservation = reserve_api_usage(path, "ai", "synthetic", 1)
            with patch("job_agent.services.api_usage._month_now", return_value="2026-10"):
                with self.assertRaises(ApiQuotaExceededError):
                    reserve_api_usage(path, "ai", "synthetic", 1)
                reservation.commit()
                self.assertEqual(load_api_usage(path).month, "2026-10")
                self.assertEqual(load_api_usage(path).ai.successful_requests, 1)

    def test_partial_bundle_counts_returned_calls_and_retains_uncertain_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api-usage.json"
            with reserve_api_usage(path, "maps", "amap", 3, units=3) as reservation:
                reservation.commit(successful_requests=1, uncertain_requests=1)
            usage = load_api_usage(path)
            self.assertEqual(usage.maps.successful_requests, 1)
            self.assertEqual(sum(item.units for item in usage.pending.values()), 1)
            self.assertEqual(next(iter(usage.pending.values())).state, "uncertain")
            with reserve_api_usage(path, "maps", "amap", 3, units=1) as next_slot:
                next_slot.commit()
            with self.assertRaises(ApiQuotaExceededError):
                reserve_api_usage(path, "maps", "amap", 3, units=1)

    def test_failed_call_releases_slot_and_partial_bundle_counts_only_successes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "private" / "api-usage.json"
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                with reserve_api_usage(path, "ai", "test", 1):
                    raise RuntimeError("provider failed")
            self.assertEqual(load_api_usage(path).ai.successful_requests, 0)
            with reserve_api_usage(path, "ai", "test", 1) as reservation:
                reservation.commit()
            self.assertEqual(load_api_usage(path).ai.successful_requests, 1)

            with reserve_api_usage(path, "maps", "amap", 3, units=3) as reservation:
                reservation.commit(successful_requests=1)
            self.assertEqual(load_api_usage(path).maps.successful_requests, 1)
            with reserve_api_usage(path, "maps", "amap", 3, units=2) as reservation:
                reservation.commit(successful_requests=2)
            self.assertEqual(load_api_usage(path).maps.successful_requests, 3)
            with self.assertRaises(ApiQuotaExceededError):
                reserve_api_usage(path, "maps", "amap", 3, units=1)

    def test_concurrent_records_preserve_every_success_and_token(self) -> None:
        workers = 16
        writes_per_worker = 8
        start = Barrier(workers)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "private" / "api-usage.json"

            def record_many(worker: int) -> None:
                connector = ("ai", "search", "maps")[worker % 3]
                start.wait(timeout=10)
                for _ in range(writes_per_worker):
                    record_api_usage(
                        path,
                        connector,
                        "test-provider",
                        input_tokens=2,
                        output_tokens=3,
                    )

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(record_many, worker) for worker in range(workers)]
                for future in futures:
                    future.result(timeout=30)

            usage = load_api_usage(path)
            for index, connector in enumerate(("ai", "search", "maps")):
                expected = len(range(index, workers, 3)) * writes_per_worker
                counter = getattr(usage, connector)
                self.assertEqual(counter.successful_requests, expected)
                self.assertEqual(counter.input_tokens, expected * 2)
                self.assertEqual(counter.output_tokens, expected * 3)

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
