from __future__ import annotations

import json
import os

from pydantic import Field

from job_agent.models.base import StrictModel
from job_agent.models.job import (
    HardGateAssessment,
    HardGateStatus,
    MatchEvidence,
    MatchResult,
    Recommendation,
    ScoreBreakdown,
    StructuredJob,
    recommendation_for_score,
)
from job_agent.models.profile import Profile
from job_agent.services.ai_errors import safe_ai_error_message


class OpenAIMatcherError(RuntimeError):
    pass


class AIJobAnalysis(StrictModel):
    job: StructuredJob
    score_breakdown: ScoreBreakdown
    why_fit: list[str] = Field(default_factory=list)
    why_not_fit: list[str] = Field(default_factory=list)
    advantages: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    hard_gates: list[HardGateAssessment] = Field(default_factory=list)
    evidence: list[MatchEvidence] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


SYSTEM_PROMPT = """
你是个人求职系统中的 JD 分析与匹配模块。请严格遵守以下规则：

1. 只把 PROFILE_APPLICATION_CONTEXT 中出现的事实视为候选人已拥有的经历或能力。
2. 只有 documented 或 user_confirmed 状态的内容会出现在上下文中；不得补写、猜测或暗示任何未提供的经历、成果、学历、证书或技能。
3. 所有匹配证据必须引用真实存在的 profile_fact_ids。没有证据时标记 gap 或 unknown。
4. 优势列表中凡涉及候选人经历，必须以 [fact_id] 开头并忠实转述该事实。
5. 硬门槛只能判断 passes、fails 或 unknown；信息不足时必须选 unknown。
6. 将 JD 拆解为职责、必须条件、加分条件、技能、工具、行业经验、学历、专业、实习、地点、到岗与潜在硬门槛，并保留简短 JD 原文证据。
7. 按固定满分计算：岗位方向 20、技能 30、经历 25、学历 10、地点与到岗 10、个人偏好 5。不得更改权重。
8. 若确认存在 fails 硬门槛，总分不得超过 59。不要因为信息未知而判定失败。
9. 输出应解释为什么适合、为什么不适合、优势、短板、硬门槛和未知项，不要给空泛评价。
10. job.raw_text 输出空字符串；调用方会在本地补回原 JD。
""".strip()


def _cap_breakdown(breakdown: ScoreBreakdown, cap: int = 59) -> ScoreBreakdown:
    values = breakdown.model_dump()
    excess = breakdown.total - cap
    for field in ("preferences", "logistics", "education", "experience", "skills", "role_direction"):
        if excess <= 0:
            break
        reduction = min(values[field], excess)
        values[field] -= reduction
        excess -= reduction
    return ScoreBreakdown.model_validate(values)


def _validate_evidence_ids(profile: Profile, analysis: AIJobAnalysis) -> None:
    valid_ids = {
        fact.id
        for experience in profile.experiences
        for fact in experience.facts
        if profile.is_application_ready(fact.status)
    }
    referenced_ids = {
        fact_id
        for item in [*analysis.evidence, *analysis.hard_gates]
        for fact_id in item.profile_fact_ids
    }
    unknown_ids = sorted(referenced_ids - valid_ids)
    if unknown_ids:
        raise OpenAIMatcherError(
            "模型返回了不存在或未确认的事实 ID，结果已拒绝: " + ", ".join(unknown_ids)
        )
    for advantage in analysis.advantages:
        match = __import__("re").match(r"^\[([^\]]+)]", advantage)
        if not match or match.group(1) not in valid_ids:
            raise OpenAIMatcherError(
                "模型优势描述缺少有效事实 ID，结果已拒绝: " + advantage
            )


def _request_payload(
    profile: Profile,
    raw_jd: str,
    *,
    company: str | None,
    title: str | None,
    location: str | None,
    source: str,
    source_url: str | None,
) -> dict[str, object]:
    return {
        "known_job_metadata": {
            "company": company,
            "title": title,
            "location": location,
            "source": source,
            "source_url": source_url,
        },
        "profile_application_context": profile.application_context(),
        "raw_jd": raw_jd,
    }


def _finalize_analysis(
    profile: Profile,
    analysis: AIJobAnalysis,
    raw_jd: str,
    *,
    company: str | None,
    title: str | None,
    location: str | None,
    source: str,
    source_url: str | None,
    engine: str,
) -> MatchResult:
    _validate_evidence_ids(profile, analysis)
    analysis.job.raw_text = raw_jd
    analysis.job.company = company or analysis.job.company
    analysis.job.title = title or analysis.job.title
    analysis.job.location = location or analysis.job.location
    analysis.job.source = source
    analysis.job.source_url = source_url
    breakdown = analysis.score_breakdown
    if any(gate.status == HardGateStatus.FAILS for gate in analysis.hard_gates):
        breakdown = _cap_breakdown(breakdown)
    score = breakdown.total
    return MatchResult(
        job=analysis.job,
        score_breakdown=breakdown,
        overall_score=score,
        recommendation=recommendation_for_score(score),
        why_fit=analysis.why_fit,
        why_not_fit=analysis.why_not_fit,
        advantages=analysis.advantages,
        gaps=analysis.gaps,
        hard_gates=analysis.hard_gates,
        evidence=analysis.evidence,
        unknowns=analysis.unknowns,
        engine=engine,
    )


def _json_from_model_text(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise OpenAIMatcherError("兼容 API 未返回 JSON 对象。")
    return cleaned[start : end + 1]


def match_job_with_openai(
    profile: Profile,
    raw_jd: str,
    *,
    model: str,
    company: str | None = None,
    title: str | None = None,
    location: str | None = None,
    source: str = "manual",
    source_url: str | None = None,
    api_key: str | None = None,
) -> MatchResult:
    resolved_api_key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
    if not resolved_api_key:
        raise OpenAIMatcherError(
            "OpenAI API Key 尚未配置。请在本地设置页填写，不要发到聊天中。"
        )
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise OpenAIMatcherError("OpenAI SDK 未安装，请先运行安装脚本。") from exc

    request_payload = _request_payload(
        profile,
        raw_jd,
        company=company,
        title=title,
        location=location,
        source=source,
        source_url=source_url,
    )
    try:
        response = OpenAI(api_key=resolved_api_key).responses.parse(
            model=model,
            store=False,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(request_payload, ensure_ascii=False),
                },
            ],
            text_format=AIJobAnalysis,
        )
    except Exception as exc:  # SDK raises several transport/API subclasses.
        raise OpenAIMatcherError(
            safe_ai_error_message(exc, provider="openai", action="岗位分析")
        ) from exc
    analysis = response.output_parsed
    if analysis is None:
        raise OpenAIMatcherError("OpenAI 未返回可解析的结构化结果。")
    return _finalize_analysis(
        profile,
        analysis,
        raw_jd,
        company=company,
        title=title,
        location=location,
        source=source,
        source_url=source_url,
        engine=f"openai:{model}",
    )


def match_job_with_openai_compatible(
    profile: Profile,
    raw_jd: str,
    *,
    model: str,
    base_url: str,
    api_key: str | None = None,
    company: str | None = None,
    title: str | None = None,
    location: str | None = None,
    source: str = "manual",
    source_url: str | None = None,
) -> MatchResult:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise OpenAIMatcherError("OpenAI SDK 未安装，请先运行安装脚本。") from exc
    resolved_api_key = (api_key or "").strip() or "local-api-no-key"
    request_payload = _request_payload(
        profile,
        raw_jd,
        company=company,
        title=title,
        location=location,
        source=source,
        source_url=source_url,
    )
    compatibility_prompt = (
        SYSTEM_PROMPT
        + "\n\n只输出一个 JSON 对象，不要使用 Markdown。输出必须严格符合以下 JSON Schema：\n"
        + json.dumps(AIJobAnalysis.model_json_schema(), ensure_ascii=False)
    )
    try:
        response = OpenAI(
            api_key=resolved_api_key,
            base_url=base_url,
        ).chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": compatibility_prompt},
                {
                    "role": "user",
                    "content": json.dumps(request_payload, ensure_ascii=False),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content or ""
        analysis = AIJobAnalysis.model_validate_json(_json_from_model_text(content))
    except OpenAIMatcherError:
        raise
    except Exception as exc:
        raise OpenAIMatcherError(
            safe_ai_error_message(
                exc,
                provider="openai_compatible",
                action="岗位分析",
            )
        ) from exc
    return _finalize_analysis(
        profile,
        analysis,
        raw_jd,
        company=company,
        title=title,
        location=location,
        source=source,
        source_url=source_url,
        engine=f"openai-compatible:{model}",
    )
