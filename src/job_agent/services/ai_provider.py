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


@dataclass(frozen=True)
class CodexJobMatchProvider:
    model: str = "codex-default"
    provider_id: str = "codex"

    def match_job(self, profile: Profile, raw_jd: str, *, company: str | None = None,
                  title: str | None = None, location: str | None = None,
                  source: str = "manual", source_url: str | None = None) -> MatchResult:
        import json
        from job_agent.services.codex_bridge import CodexBridgeError, codex_completion
        from job_agent.services.openai_matcher import (
            AIJobAnalysis, OpenAIMatcherError, SYSTEM_PROMPT, _request_payload, _finalize_analysis, _json_from_model_text,
        )
        metadata = dict(company=company, title=title, location=location,
                        source=source, source_url=source_url)
        payload = _request_payload(profile, raw_jd, **metadata)
        allowed_ids = [fact.id for experience in profile.experiences for fact in experience.facts
                       if profile.is_application_ready(fact.status)]
        payload["allowed_profile_fact_ids"] = allowed_ids
        payload["output_json_schema"] = AIJobAnalysis.model_json_schema()
        for definition in payload["output_json_schema"].get("$defs", {}).values():
            field = definition.get("properties", {}).get("profile_fact_ids")
            if field:
                field["items"] = {"type": "string", "enum": allowed_ids}
        try:
            text, _, _ = codex_completion(SYSTEM_PROMPT + "\n仅返回符合 output_json_schema 的 JSON。"
                "所有 profile_fact_ids 及 advantages 开头的编号只能从 allowed_profile_fact_ids 选择。"
                "学历 ID、技能 ID 和来源 ID 都不是事实 ID；学历或技能有记录但无事实 ID 时，"
                "可按记录分析并将该项 profile_fact_ids 留空，不得引用这些其他类型的 ID。",
                                         json.dumps(payload, ensure_ascii=False), model=self.model)
            analysis = AIJobAnalysis.model_validate_json(_json_from_model_text(text))
            return _finalize_analysis(profile, analysis, raw_jd, engine=f"codex:{self.model}", **metadata)
        except (CodexBridgeError, ValueError, OpenAIMatcherError) as exc:
            raise AIProviderError("Codex 岗位分析未完成或输出不符合证据格式。") from exc


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
    if normalized == "codex":
        return CodexJobMatchProvider(model=model or "codex-default")
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
