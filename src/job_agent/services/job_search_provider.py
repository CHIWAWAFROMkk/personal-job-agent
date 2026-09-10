from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import Field

from job_agent.models.base import StrictModel


BRAVE_WEB_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BOCHA_WEB_SEARCH_ENDPOINT = "https://api.bochaai.com/v1/web-search"


class JobSearchProviderError(RuntimeError):
    pass


class SearchHit(StrictModel):
    provider: str
    query: str
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    snippet: str = ""


class JobSearchProvider(Protocol):
    provider_id: str

    def search(self, query: str, *, count: int = 10) -> list[SearchHit]: ...


JsonRequester = Callable[[str, dict[str, str], int], dict[str, Any]]
JsonPostRequester = Callable[
    [str, dict[str, str], dict[str, Any], int], dict[str, Any]
]


def _request_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise JobSearchProviderError(
            f"Brave Search 返回 HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except URLError as exc:
        raise JobSearchProviderError(f"无法连接 Brave Search: {exc.reason}") from exc
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise JobSearchProviderError("Brave Search 返回了无法解析的 JSON。") from exc
    if not isinstance(parsed, dict):
        raise JobSearchProviderError("Brave Search 返回格式不正确。")
    return parsed


def _post_bocha_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    request = Request(
        url,
        headers=headers,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        hints = {
            401: "API Key 无效或已失效",
            403: "账户余额不足或当前密钥无权调用",
            429: "请求过于频繁",
        }
        hint = hints.get(exc.code, detail or str(exc.reason))
        raise JobSearchProviderError(
            f"博查 Search 返回 HTTP {exc.code}: {hint}"
        ) from exc
    except URLError as exc:
        raise JobSearchProviderError(f"无法连接博查 Search: {exc.reason}") from exc
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise JobSearchProviderError("博查 Search 返回了无法解析的 JSON。") from exc
    if not isinstance(parsed, dict):
        raise JobSearchProviderError("博查 Search 返回格式不正确。")
    return parsed


class BraveSearchProvider:
    provider_id = "brave"

    def __init__(
        self,
        api_key: str,
        *,
        requester: JsonRequester = _request_json,
        timeout_seconds: int = 20,
    ) -> None:
        if not api_key.strip():
            raise JobSearchProviderError("尚未配置 BRAVE_SEARCH_API_KEY。")
        self.api_key = api_key.strip()
        self.requester = requester
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, *, count: int = 10) -> list[SearchHit]:
        normalized_query = query.strip()
        if not normalized_query:
            raise JobSearchProviderError("搜索关键词不能为空。")
        if len(normalized_query) > 400 or len(normalized_query.split()) > 50:
            raise JobSearchProviderError("搜索关键词超过 Brave Search 的长度限制。")
        if count < 1 or count > 20:
            raise JobSearchProviderError("单次搜索数量必须在 1 到 20 之间。")
        parameters = urlencode(
            {
                "q": normalized_query,
                "count": count,
                "country": "CN",
                "search_lang": "zh-hans",
                "ui_lang": "zh-CN",
                "safesearch": "moderate",
            }
        )
        payload = self.requester(
            f"{BRAVE_WEB_SEARCH_ENDPOINT}?{parameters}",
            {
                "Accept": "application/json",
                "X-Subscription-Token": self.api_key,
            },
            self.timeout_seconds,
        )
        web = payload.get("web", {})
        raw_results = web.get("results", []) if isinstance(web, dict) else []
        if not isinstance(raw_results, list):
            raise JobSearchProviderError("Brave Search 响应缺少 web.results 列表。")
        hits: list[SearchHit] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title or not url:
                continue
            hits.append(
                SearchHit(
                    provider=self.provider_id,
                    query=normalized_query,
                    title=title,
                    url=url,
                    snippet=str(item.get("description") or "").strip(),
                )
            )
        return hits


class BochaSearchProvider:
    provider_id = "bocha"

    def __init__(
        self,
        api_key: str,
        *,
        requester: JsonPostRequester = _post_bocha_json,
        timeout_seconds: int = 20,
        freshness: str = "oneMonth",
    ) -> None:
        if not api_key.strip():
            raise JobSearchProviderError("尚未配置 BOCHA_API_KEY。")
        self.api_key = api_key.strip()
        self.requester = requester
        self.timeout_seconds = timeout_seconds
        self.freshness = freshness

    def search(self, query: str, *, count: int = 10) -> list[SearchHit]:
        normalized_query = query.strip()
        if not normalized_query:
            raise JobSearchProviderError("搜索关键词不能为空。")
        if len(normalized_query) > 500:
            raise JobSearchProviderError("搜索关键词不能超过 500 个字符。")
        if count < 1 or count > 50:
            raise JobSearchProviderError("博查单次搜索数量必须在 1 到 50 之间。")

        payload = self.requester(
            BOCHA_WEB_SEARCH_ENDPOINT,
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            {
                "query": normalized_query,
                "freshness": self.freshness,
                "summary": False,
                "count": count,
            },
            self.timeout_seconds,
        )

        code = payload.get("code")
        if code not in (None, 0, 200, "200"):
            message = str(payload.get("msg") or payload.get("message") or "未知错误")
            log_id = str(payload.get("log_id") or "").strip()
            suffix = f"（log_id: {log_id}）" if log_id else ""
            raise JobSearchProviderError(f"博查 Search 调用失败: {message}{suffix}")

        response_data = payload.get("data", payload)
        if not isinstance(response_data, dict):
            raise JobSearchProviderError("博查 Search 响应缺少 data 对象。")
        web_pages = response_data.get("webPages", {})
        raw_results = web_pages.get("value", []) if isinstance(web_pages, dict) else []
        if not isinstance(raw_results, list):
            raise JobSearchProviderError("博查 Search 响应缺少 data.webPages.value 列表。")

        hits: list[SearchHit] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title or not url:
                continue
            snippet = str(item.get("snippet") or item.get("summary") or "").strip()
            hits.append(
                SearchHit(
                    provider=self.provider_id,
                    query=normalized_query,
                    title=title,
                    url=url,
                    snippet=snippet,
                )
            )
        return hits


def build_job_search_provider(
    provider_id: str,
    *,
    brave_api_key: str | None = None,
    bocha_api_key: str | None = None,
) -> JobSearchProvider:
    normalized = provider_id.strip().casefold()
    if normalized == "bocha":
        return BochaSearchProvider(bocha_api_key or "")
    if normalized == "brave":
        return BraveSearchProvider(brave_api_key or "")
    if normalized in {"", "none", "disabled"}:
        raise JobSearchProviderError(
            "尚未启用岗位搜索 API。请在本地 Dashboard 的“连接与 API”中选择博查或 Brave；也可以继续手动导入 JD。"
        )
    raise JobSearchProviderError(
        f"尚未安装或不支持搜索 Provider: {provider_id or '未配置'}。"
    )
