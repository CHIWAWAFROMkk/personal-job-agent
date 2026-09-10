import unittest

from job_agent.services.ai_provider import (
    AIProviderError,
    LocalJobMatchProvider,
    OpenAICompatibleJobMatchProvider,
    OpenAIJobMatchProvider,
    build_job_match_provider,
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


if __name__ == "__main__":
    unittest.main()
