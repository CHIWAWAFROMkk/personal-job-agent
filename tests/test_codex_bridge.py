import json
import subprocess
import unittest
from unittest.mock import patch

from job_agent.services.codex_bridge import CodexBridgeError, codex_completion
from job_agent.services.runtime_config import AIConnectorConfig
from job_agent.services.ai_provider import AIProviderError, build_job_match_provider
from job_agent.services.openai_matcher import AIJobAnalysis
from tests.helpers import sample_profile


class CodexBridgeTests(unittest.TestCase):
    def test_matcher_rejects_education_skill_and_pending_ids_as_fact_evidence(self):
        profile = sample_profile()
        baseline = build_job_match_provider("local", "").match_job(profile, "需要 SQL 数据分析", company="测试")
        payload = baseline.model_dump(mode="json", include=set(AIJobAnalysis.model_fields))
        for fact_id in ("fact-sql-analysis", "edu-undergrad", "skill-sql", "fact-pending-tableau"):
            with self.subTest(fact_id=fact_id):
                payload["hard_gates"] = [{"requirement": "SQL", "status": "passes",
                    "profile_fact_ids": [fact_id], "explanation": "模型返回的证据"}]
                with patch("job_agent.services.codex_bridge.codex_completion", return_value=(json.dumps(payload), 1, 1)):
                    provider = build_job_match_provider("codex", "codex-default")
                    if fact_id == "fact-sql-analysis":
                        self.assertEqual(provider.match_job(profile, "需要 SQL 数据分析", company="测试").engine, "codex:codex-default")
                    else:
                        with self.assertRaises(AIProviderError):
                            provider.match_job(profile, "需要 SQL 数据分析", company="测试")

    def test_completion_uses_login_and_windows_proxy_without_shell_or_secrets(self):
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "写作建议"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
        ]
        completed = subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, events)), "")
        with patch("job_agent.services.codex_bridge.find_codex_executable", return_value="codex.exe"), \
             patch("job_agent.services.codex_bridge.urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "never-forward-this", "CODEX_THREAD_ID": "parent"}), \
             patch("job_agent.services.codex_bridge.subprocess.run", return_value=completed) as run:
            self.assertEqual(codex_completion("system", "private resume", model="codex-default"), ("写作建议", 10, 3))
        args = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertNotIn("private resume", args)
        self.assertNotIn("--model", args)
        self.assertIn("--ephemeral", args)
        self.assertIn("read-only", args)
        self.assertEqual(options["input"], "private resume")
        self.assertFalse(options.get("shell", False))
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertNotIn("CODEX_THREAD_ID", options["env"])
        self.assertTrue(options["env"]["HTTPS_PROXY"])

    def test_incomplete_output_and_errors_are_not_returned_as_success(self):
        for result in [subprocess.CompletedProcess([], 1, "", "private error secret"),
                       subprocess.CompletedProcess([], 0, '{"type":"thread.started"}', "")]:
            with self.subTest(result=result.returncode), \
                 patch("job_agent.services.codex_bridge.find_codex_executable", return_value="codex.exe"), \
                 patch("job_agent.services.codex_bridge.subprocess.run", return_value=result):
                with self.assertRaises(CodexBridgeError) as error:
                    codex_completion("system", "user", model="codex-default")
                self.assertNotIn("secret", str(error.exception))

    def test_codex_config_never_stores_an_api_key(self):
        config = AIConnectorConfig(provider="codex", model="codex-default", api_key="old-key",
                                   base_url="https://example.com/v1")
        self.assertEqual(config.api_key, "")
        self.assertIsNone(config.base_url)
        self.assertEqual(build_job_match_provider("codex", "codex-default").provider_id, "codex")
