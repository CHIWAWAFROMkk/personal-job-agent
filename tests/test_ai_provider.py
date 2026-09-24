import unittest
from unittest.mock import patch

from job_agent.models.profile import Profile
from job_agent.services.ai_provider import (
    AIProviderError,
    LocalJobMatchProvider,
    OpenAICompatibleJobMatchProvider,
    OpenAIJobMatchProvider,
    build_job_match_provider,
)
from job_agent.services.openai_matcher import (
    OpenAIMatcherError,
    match_job_with_openai,
    match_job_with_openai_compatible,
)


class AIProviderTests(unittest.TestCase):
    def test_builds_openai_adapter(self) -> None:
        provider = build_job_match_provider(
            "OpenAI", "test-model", api_key="test-secret"
        )
        self.assertIsInstance(provider, OpenAIJobMatchProvider)
        self.assertEqual(provider.model, "test-model")

    def test_builds_local_adapter_without_api_key(self) -> None:
        provider = build_job_match_provider("local", "local-explainable-v1")
        self.assertIsInstance(provider, LocalJobMatchProvider)

    def test_builds_openai_compatible_adapter(self) -> None:
        provider = build_job_match_provider(
            "openai_compatible",
            "deepseek-chat",
            api_key="test-secret",
            base_url="https://api.example.com/v1",
        )
        self.assertIsInstance(provider, OpenAICompatibleJobMatchProvider)
        self.assertEqual(provider.base_url, "https://api.example.com/v1")

    def test_compatible_adapter_requires_base_url(self) -> None:
        with self.assertRaisesRegex(AIProviderError, "服务地址"):
            build_job_match_provider("openai_compatible", "test-model")

    def test_unknown_provider_has_clear_error(self) -> None:
        with self.assertRaises(AIProviderError):
            build_job_match_provider("not-installed", "test-model")

    @patch("openai.OpenAI")
    def test_openai_match_has_bounded_timeout_without_paid_retry(self, factory) -> None:
        factory.return_value.responses.parse.side_effect = RuntimeError("offline")
        with self.assertRaises(OpenAIMatcherError):
            match_job_with_openai(Profile(), "合成岗位要求", model="test-model", api_key="synthetic-key")
        self.assertEqual(factory.call_args.kwargs["timeout"], 45)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)

    @patch("openai.OpenAI")
    def test_compatible_match_has_bounded_timeout_without_paid_retry(self, factory) -> None:
        factory.return_value.chat.completions.create.side_effect = RuntimeError("offline")
        with self.assertRaises(OpenAIMatcherError):
            match_job_with_openai_compatible(
                Profile(), "合成岗位要求", model="test-model",
                base_url="https://example.com/v1", api_key="synthetic-key",
            )
        self.assertEqual(factory.call_args.kwargs["timeout"], 45)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)


if __name__ == "__main__":
    unittest.main()
