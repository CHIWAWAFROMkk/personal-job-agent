from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from job_agent.models.base import StrictModel


ResumeFindingSeverity = Literal["high", "medium", "low"]
ResumeFindingCategory = Literal[
    "integrity",
    "contact",
    "structure",
    "evidence",
    "language",
    "readability",
]


class ResumeAuditFinding(StrictModel):
    code: str
    category: ResumeFindingCategory
    severity: ResumeFindingSeverity
    title: str
    evidence: list[str] = Field(default_factory=list)
    recommendation: str
    hr_question: str | None = None


class ResumeAuditMetrics(StrictModel):
    character_count: int = Field(ge=0)
    non_empty_line_count: int = Field(ge=0)
    experience_line_count: int = Field(ge=0)
    quantified_line_count: int = Field(ge=0)
    quantified_line_ratio: float = Field(ge=0, le=1)


class ResumeAuditReport(StrictModel):
    schema_version: str = "1.0"
    ruleset: str = "local-truthful-v1"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_filename: str
    source_sha256: str
    score: int = Field(ge=0, le=100)
    summary: str
    metrics: ResumeAuditMetrics
    strengths: list[str] = Field(default_factory=list)
    findings: list[ResumeAuditFinding] = Field(default_factory=list)
    extraction_warnings: list[str] = Field(default_factory=list)
    safety_note: str = (
        "体检只分析表达与结构，不推断经历真伪；任何数字、成果和职责都必须可核验，不要为了分数编造。"
    )
