from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from job_agent.models.base import StrictModel


ReviewStatus = Literal[
    "confirmed",
    "needs_confirmation",
    "capability_gap",
    "manual_input",
]


class ApplicationJobSnapshot(StrictModel):
    job_id: int
    company: str
    title: str
    location: str | None = None
    source_url: str | None = None
    match_score: int
    recommendation: str


class ApplicationEvidence(StrictModel):
    fact_id: str
    experience_id: str
    organization: str
    role: str
    statement: str
    status: str
    used_for: list[str] = Field(default_factory=list)


class ApplicationMaterials(StrictModel):
    boss_greeting: str
    email_subject: str
    email_body: str
    self_introduction_30s: str
    why_company: str
    why_role: str
    personal_strengths: list[str] = Field(default_factory=list)
    interview_self_introduction_60s: str
    availability_answer: str


class ApplicationReviewItem(StrictModel):
    category: str
    item: str
    status: ReviewStatus
    detail: str
    blocks_submission: bool = False


class ResumeBundle(StrictModel):
    status: Literal["ready", "needs_review", "needs_generation"]
    content_path: str | None = None
    manifest_path: str | None = None
    docx_path: str | None = None
    pdf_path: str | None = None
    fact_ids: list[str] = Field(default_factory=list)
    qa_verified: bool = False
    note: str = ""


class ApplicationPack(StrictModel):
    schema_version: str = "1.0"
    generator: str = "local-truth-first-v1"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    job: ApplicationJobSnapshot
    materials: ApplicationMaterials
    evidence: list[ApplicationEvidence] = Field(default_factory=list)
    review_checklist: list[ApplicationReviewItem] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    resume: ResumeBundle
