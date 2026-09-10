from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, model_validator

from job_agent.models.base import StrictModel


class RequirementImportance(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    UNKNOWN = "unknown"


class Requirement(StrictModel):
    text: str
    category: str
    importance: RequirementImportance = RequirementImportance.UNKNOWN
    hard_gate: bool = False
    jd_evidence: str = ""


class StructuredJob(StrictModel):
    company: str = "未识别"
    title: str = "未识别"
    source: str = "manual"
    source_url: str | None = None
    location: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    industry_experience: list[str] = Field(default_factory=list)
    education_requirements: list[str] = Field(default_factory=list)
    major_requirements: list[str] = Field(default_factory=list)
    internship_requirements: list[str] = Field(default_factory=list)
    arrival_requirements: list[str] = Field(default_factory=list)
    raw_text: str = ""


class MatchStatus(StrEnum):
    MATCHED = "matched"
    PARTIAL = "partial"
    GAP = "gap"
    UNKNOWN = "unknown"


class HardGateStatus(StrEnum):
    PASSES = "passes"
    FAILS = "fails"
    UNKNOWN = "unknown"


class MatchEvidence(StrictModel):
    requirement: str
    status: MatchStatus
    profile_fact_ids: list[str] = Field(default_factory=list)
    explanation: str


class HardGateAssessment(StrictModel):
    requirement: str
    status: HardGateStatus
    profile_fact_ids: list[str] = Field(default_factory=list)
    explanation: str


class ScoreBreakdown(StrictModel):
    role_direction: int = Field(ge=0, le=20)
    skills: int = Field(ge=0, le=30)
    experience: int = Field(ge=0, le=25)
    education: int = Field(ge=0, le=10)
    logistics: int = Field(ge=0, le=10)
    preferences: int = Field(ge=0, le=5)

    @property
    def total(self) -> int:
        return (
            self.role_direction
            + self.skills
            + self.experience
            + self.education
            + self.logistics
            + self.preferences
        )


class Recommendation(StrEnum):
    STRONGLY_RECOMMEND = "strongly_recommend"
    RECOMMEND = "recommend"
    TRY = "try"
    LOW_PRIORITY = "low_priority"

    @property
    def zh(self) -> str:
        return {
            Recommendation.STRONGLY_RECOMMEND: "强烈推荐",
            Recommendation.RECOMMEND: "推荐",
            Recommendation.TRY: "可以尝试",
            Recommendation.LOW_PRIORITY: "低优先级",
        }[self]


def recommendation_for_score(score: int) -> Recommendation:
    if score >= 85:
        return Recommendation.STRONGLY_RECOMMEND
    if score >= 75:
        return Recommendation.RECOMMEND
    if score >= 60:
        return Recommendation.TRY
    return Recommendation.LOW_PRIORITY


class MatchResult(StrictModel):
    job: StructuredJob
    score_breakdown: ScoreBreakdown
    overall_score: int = Field(ge=0, le=100)
    recommendation: Recommendation
    why_fit: list[str] = Field(default_factory=list)
    why_not_fit: list[str] = Field(default_factory=list)
    advantages: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    hard_gates: list[HardGateAssessment] = Field(default_factory=list)
    evidence: list[MatchEvidence] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    scoring_version: str = "phase1-v1"
    engine: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def normalize_score_and_label(self) -> MatchResult:
        object.__setattr__(self, "overall_score", self.score_breakdown.total)
        object.__setattr__(
            self,
            "recommendation",
            recommendation_for_score(self.score_breakdown.total),
        )
        return self
