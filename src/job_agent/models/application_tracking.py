from __future__ import annotations

from typing import Literal

from pydantic import Field

from job_agent.models.base import StrictModel


ApplicationStatus = Literal[
    "saved",
    "ready_to_apply",
    "applied",
    "hr_read",
    "resume_requested",
    "screening",
    "assessment",
    "written_test",
    "interview_1",
    "interview_2",
    "final_interview",
    "offer",
    "rejected",
    "withdrawn",
    "no_response",
]


class ApplicationRecord(StrictModel):
    application_id: int
    job_id: int
    status: ApplicationStatus
    resume_path: str | None = None
    application_pack_path: str | None = None
    source_url: str | None = None
    applied_at: str | None = None
    last_verified_at: str | None = None
    verification_method: str = ""
    evidence_path: str | None = None
    created_at: str
    updated_at: str


class ApplicationEvent(StrictModel):
    event_id: int
    application_id: int
    previous_status: ApplicationStatus | None = None
    status: ApplicationStatus
    source: str
    detail: str = ""
    evidence_path: str | None = None
    occurred_at: str


class ApplicationListItem(StrictModel):
    application_id: int
    job_id: int
    company: str
    title: str
    status: ApplicationStatus
    match_score: int | None = None
    applied_at: str | None = None
    last_verified_at: str | None = None
    updated_at: str


class ApplicationSummary(StrictModel):
    total: int = 0
    active: int = 0
    applied_or_later: int = 0
    hr_read_or_later: int = 0
    resume_requested_or_later: int = 0
    meaningful_responses: int = 0
    screening_or_later: int = 0
    assessment_or_written_test: int = 0
    interview_or_later: int = 0
    priority_preparation: int = 0
    offers: int = 0
    rejected: int = 0
    no_response: int = 0
    withdrawn: int = 0
    current_status_counts: dict[str, int] = Field(default_factory=dict)
