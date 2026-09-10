from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from job_agent.models.job import MatchResult
from job_agent.models.profile import Profile
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.openai_matcher import (
    match_job_with_openai,
    match_job_with_openai_compatible,
)


class AIProviderError(RuntimeError):
    pass


class JobMatchProvider(Protocol):
    provider_id: str
    model: str

    def match_job(
        self,
        profile: Profile,
        raw_jd: str,
        *,
        company: str | None = None,
        title: str | None = None,
        location: str | None = None,
        source: str = "manual",
        source_url: str | None = None,
    ) -> MatchResult: ...


@dataclass(frozen=True)
class OpenAIJobMatchProvider:
    model: str
    api_key: str | None = field(default=None, repr=False)
    provider_id: str = "openai"

    def match_job(
        self,
        profile: Profile,
        raw_jd: str,
        *,
        company: str | None = None,
        title: str | None = None,
        location: str | None = None,
        source: str = "manual",
        source_url: str | None = None,
    ) -> MatchResult:
        return match_job_with_openai(
            profile,
            raw_jd,
            model=self.model,
            company=company,
            title=title,
            location=location,
            source=source,
            source_url=source_url,
            api_key=self.api_key,
        )


@dataclass(frozen=True)
class OpenAICompatibleJobMatchProvider:
    model: str
    base_url: str
    api_key: str | None = field(default=None, repr=False)
    provider_id: str = "openai_compatible"

    def match_job(
        self,
        profile: Profile,
        raw_jd: str,
        *,
        company: str | None = None,
        title: str | None = None,
        location: str | None = None,
        source: str = "manual",
        source_url: str | None = None,
    ) -> MatchResult:
        return match_job_with_openai_compatible(
            profile,
            raw_jd,
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            company=company,
            title=title,
            location=location,
            source=source,
            source_url=source_url,
        )


@dataclass(frozen=True)
class LocalJobMatchProvider:
    model: str = "local-explainable-v1"
    provider_id: str = "local"

    def match_job(
        self,
        profile: Profile,
        raw_jd: str,
        *,
        company: str | None = None,
        title: str | None = None,
        location: str | None = None,
        source: str = "manual",
        source_url: str | None = None,
    ) -> MatchResult:
        job = structure_job_locally(
            raw_jd,
            company=company,
            title=title,
            location=location,
            source=source,
            source_url=source_url,
        )
        return match_job_locally(profile, job)


def build_job_match_provider(
    provider_id: str,
    model: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> JobMatchProvider:
    normalized = provider_id.casefold().strip()
    if normalized == "local":
        return LocalJobMatchProvider(model=model or "local-explainable-v1")
    if normalized == "openai":
        return OpenAIJobMatchProvider(model=model, api_key=api_key)
    if normalized in {"openai_compatible", "openai-compatible", "compatible"}:
        if not base_url:
            raise AIProviderError("OpenAI 兼容 Provider 必须配置服务地址。")
        return OpenAICompatibleJobMatchProvider(
            model=model,
            base_url=base_url,
            api_key=api_key,
        )
    raise AIProviderError(
        f"尚未安装 AI Provider 适配器: {provider_id}。"
        "核心接口已经独立，可在不修改 Profile、评分结果或 CLI 的情况下添加。"
    )
