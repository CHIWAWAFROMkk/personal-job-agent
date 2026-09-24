from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from job_agent.services.runtime_config import (
    RuntimeConfigUpdate,
    effective_runtime_config,
    public_runtime_config,
    update_runtime_config,
)


RELEVANT_ENV = {
    "DEEPSEEK_API_KEY": "",
    "OPENAI_API_KEY": "",
    "JOB_AGENT_AI_API_KEY": "",
    "JOB_AGENT_AI_PROVIDER": "",
    "JOB_AGENT_AI_MODEL": "",
    "JOB_AGENT_AI_BASE_URL": "",
    "JOB_AGENT_SEARCH_PROVIDER": "",
    "BOCHA_API_KEY": "",
    "BRAVE_SEARCH_API_KEY": "",
    "AMAP_WEB_API_KEY": "",
    "JOB_AGENT_MAP_API_KEY": "",
    "JOB_AGENT_MAP_PROVIDER": "",
}


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "private" / "app-settings.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_defaults_run_locally_without_any_api(self) -> None:
        with patch.dict("os.environ", RELEVANT_ENV, clear=False):
            config, source = effective_runtime_config(self.path)
            public = public_runtime_config(self.path)
        self.assertEqual(source, "defaults")
        self.assertEqual(config.ai.provider, "local")
        self.assertEqual(config.search.provider, "none")
        self.assertTrue(public["ai"]["ready"])
        self.assertTrue(public["search"]["ready"])
        self.assertTrue(public["maps"]["ready"])

    def test_saves_keys_but_never_returns_them_from_public_api(self) -> None:
        secret = "secret-value-that-must-not-leak"
        update_runtime_config(
            self.path,
            RuntimeConfigUpdate(
                ai_provider="openai_compatible",
                ai_model="deepseek-chat",
                ai_base_url="https://api.example.com/v1",
                ai_api_key=secret,
                search_provider="bocha",
                search_api_key="bocha-secret",
                map_provider="amap",
                map_api_key="amap-secret",
                search_monthly_quota=20,
            ),
        )
        raw = self.path.read_text(encoding="utf-8")
        public_json = json.dumps(public_runtime_config(self.path), ensure_ascii=False)
        self.assertIn(secret, raw)
        self.assertNotIn(secret, public_json)
        self.assertNotIn("bocha-secret", public_json)
        self.assertNotIn("amap-secret", public_json)
        self.assertTrue(public_runtime_config(self.path)["ai"]["api_key_configured"])
        self.assertEqual(public_runtime_config(self.path)["search"]["monthly_quota"], 20)

    def test_switching_provider_preserves_its_key_without_exposing_it(self) -> None:
        update_runtime_config(
            self.path,
            RuntimeConfigUpdate(
                ai_provider="openai",
                ai_model="gpt-test",
                ai_api_key="openai-secret",
                search_provider="bocha",
                search_api_key="bocha-secret",
            ),
        )
        same = update_runtime_config(
            self.path,
            RuntimeConfigUpdate(
                ai_provider="openai",
                ai_model="gpt-new",
                search_provider="bocha",
            ),
        )
        self.assertEqual(same.ai.api_key, "openai-secret")
        self.assertEqual(same.search.api_key, "bocha-secret")
        switched = update_runtime_config(
            self.path,
            RuntimeConfigUpdate(
                ai_provider="local",
                ai_model="local-explainable-v1",
                search_provider="brave",
            ),
        )
        self.assertEqual(switched.ai.api_key, "")
        self.assertEqual(switched.search.api_key, "")
        restored = update_runtime_config(
            self.path,
            RuntimeConfigUpdate(
                ai_provider="openai",
                ai_model="gpt-new",
                search_provider="bocha",
            ),
        )
        self.assertEqual(restored.ai.api_key, "openai-secret")
        self.assertEqual(restored.search.api_key, "bocha-secret")
        self.assertNotIn("openai-secret", json.dumps(public_runtime_config(self.path)))
        self.assertNotIn("bocha-secret", json.dumps(public_runtime_config(self.path)))

    def test_compatible_endpoint_keys_are_isolated_and_can_be_cleared(self) -> None:
        first = RuntimeConfigUpdate(
            ai_provider="openai_compatible",
            ai_model="model",
            ai_base_url="https://first.example/v1",
            ai_api_key="first-secret",
            search_provider="none",
        )
        update_runtime_config(self.path, first)
        second = RuntimeConfigUpdate(
            ai_provider="openai_compatible",
            ai_model="model",
            ai_base_url="https://second.example/v1",
            search_provider="none",
        )
        self.assertEqual(update_runtime_config(self.path, second).ai.api_key, "")
        self.assertEqual(
            update_runtime_config(self.path, first.model_copy(update={"ai_api_key": ""})).ai.api_key,
            "first-secret",
        )
        cleared = update_runtime_config(
            self.path,
            first.model_copy(update={"ai_api_key": "", "clear_ai_api_key": True}),
        )
        self.assertEqual(cleared.ai.api_key, "")
        self.assertEqual(
            update_runtime_config(self.path, second).ai.api_key,
            "",
        )
        self.assertEqual(
            update_runtime_config(self.path, first.model_copy(update={"ai_api_key": ""})).ai.api_key,
            "",
        )

    def test_remote_compatible_url_requires_https(self) -> None:
        with self.assertRaises(ValidationError):
            update_runtime_config(
                self.path,
                RuntimeConfigUpdate(
                    ai_provider="openai_compatible",
                    ai_model="model",
                    ai_base_url="http://api.example.com/v1",
                    search_provider="none",
                ),
            )

    def test_loopback_compatible_api_can_run_without_key(self) -> None:
        update_runtime_config(
            self.path,
            RuntimeConfigUpdate(
                ai_provider="openai_compatible",
                ai_model="qwen-local",
                ai_base_url="http://127.0.0.1:11434/v1",
                search_provider="none",
            ),
        )
        self.assertTrue(public_runtime_config(self.path)["ai"]["ready"])

    def test_codex_readiness_scan_is_cached_briefly(self) -> None:
        update_runtime_config(
            self.path,
            RuntimeConfigUpdate(
                ai_provider="codex", ai_model="codex-default", search_provider="none"
            ),
        )
        with patch("job_agent.services.runtime_config.time.monotonic", side_effect=[9_000_000, 9_000_001, 9_000_046]):
            with patch(
                "job_agent.services.codex_bridge.find_codex_executable",
                side_effect=[None, "codex.exe"],
            ) as probe:
                first = public_runtime_config(self.path)
                second = public_runtime_config(self.path)
                refreshed = public_runtime_config(self.path)
        self.assertFalse(first["ai"]["ready"])
        self.assertFalse(second["ai"]["ready"])
        self.assertTrue(refreshed["ai"]["ready"])
        self.assertEqual(probe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
