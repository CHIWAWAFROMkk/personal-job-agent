import unittest

from job_agent.services.job_search_provider import (
    BOCHA_WEB_SEARCH_ENDPOINT,
    BochaSearchProvider,
    BraveSearchProvider,
    JobSearchProviderError,
    build_job_search_provider,
)


class JobSearchProviderTests(unittest.TestCase):
    def test_bocha_provider_posts_and_parses_nested_results(self) -> None:
        captured: dict[str, object] = {}

        def requester(
            url: str,
            headers: dict[str, str],
            body: dict[str, object],
            timeout: int,
        ) -> dict:
            captured.update(url=url, headers=headers, body=body, timeout=timeout)
            return {
                "code": 200,
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": "AI 产品运营实习生",
                                "url": "https://example.com/job/bocha-1",
                                "snippet": "上海，每周 4 天。",
                            },
                            {"name": "缺少链接"},
                        ]
                    }
                },
            }

        provider = BochaSearchProvider("test-key", requester=requester)
        hits = provider.search("上海 AI 产品运营 实习", count=5)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "AI 产品运营实习生")
        self.assertEqual(hits[0].provider, "bocha")
        self.assertEqual(captured["url"], BOCHA_WEB_SEARCH_ENDPOINT)
        self.assertEqual(
            captured["headers"],
            {
                "Accept": "application/json",
                "Authorization": "Bearer test-key",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(
            captured["body"],
            {
                "query": "上海 AI 产品运营 实习",
                "freshness": "oneMonth",
                "summary": False,
                "count": 5,
            },
        )

    def test_bocha_provider_accepts_direct_response_shape(self) -> None:
        provider = BochaSearchProvider(
            "test-key",
            requester=lambda *_: {
                "webPages": {
                    "value": [
                        {
                            "name": "官网招聘",
                            "url": "https://careers.example.com/job/1",
                            "summary": "岗位摘要",
                        }
                    ]
                }
            },
        )

        hits = provider.search("官网 实习")

        self.assertEqual(hits[0].snippet, "岗位摘要")

    def test_bocha_missing_key_has_clear_error(self) -> None:
        with self.assertRaisesRegex(JobSearchProviderError, "BOCHA_API_KEY"):
            build_job_search_provider("bocha", bocha_api_key=None)

    def test_bocha_search_count_is_bounded(self) -> None:
        provider = BochaSearchProvider("test-key", requester=lambda *_: {})
        with self.assertRaisesRegex(JobSearchProviderError, "1 到 50"):
            provider.search("岗位", count=51)

    def test_brave_provider_parses_web_results(self) -> None:
        captured: dict[str, object] = {}

        def requester(url: str, headers: dict[str, str], timeout: int) -> dict:
            captured.update(url=url, headers=headers, timeout=timeout)
            return {
                "web": {
                    "results": [
                        {
                            "title": "产品运营实习生",
                            "url": "https://example.com/job/1",
                            "description": "上海，2027届可投。",
                        },
                        {"title": "缺少链接"},
                    ]
                }
            }

        provider = BraveSearchProvider("test-key", requester=requester)
        hits = provider.search("上海 产品运营 实习", count=5)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "产品运营实习生")
        self.assertEqual(hits[0].provider, "brave")
        self.assertIn("country=CN", str(captured["url"]))
        self.assertEqual(
            captured["headers"],
            {"Accept": "application/json", "X-Subscription-Token": "test-key"},
        )

    def test_missing_key_has_clear_error(self) -> None:
        with self.assertRaisesRegex(JobSearchProviderError, "BRAVE_SEARCH_API_KEY"):
            build_job_search_provider("brave", brave_api_key=None)

    def test_search_count_is_bounded(self) -> None:
        provider = BraveSearchProvider("test-key", requester=lambda *_: {})
        with self.assertRaisesRegex(JobSearchProviderError, "1 到 20"):
            provider.search("岗位", count=21)


if __name__ == "__main__":
    unittest.main()
