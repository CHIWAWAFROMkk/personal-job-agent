from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from job_agent.models.application_tracking import ApplicationStatus
from job_agent.models.base import StrictModel


class PlatformApplicationObservation(StrictModel):
    platform: str
    external_application_id: str
    company: str
    title: str
    raw_status: str
    raw_status_description: str = ""
    normalized_status: ApplicationStatus | None = None
    latest_activity_at: datetime | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_endpoint: str = ""


class BrowserStatusCollection(StrictModel):
    session_id: str
    platform: str
    started_at: datetime
    finished_at: datetime
    status: Literal["completed", "login_required", "no_data", "blocked"]
    requested_urls: list[str] = Field(default_factory=list)
    final_urls: list[str] = Field(default_factory=list)
    response_endpoints: list[str] = Field(default_factory=list)
    observations: list[PlatformApplicationObservation] = Field(default_factory=list)
    screenshot_path: str | None = None
    blocker: str | None = None


class StatusSyncDecision(StrictModel):
    external_application_id: str
    company: str
    title: str
    job_id: int | None = None
    match_method: Literal["exact_company_title", "none", "ambiguous"]
    previous_status: ApplicationStatus | None = None
    observed_status: ApplicationStatus | None = None
    outcome: Literal[
        "updated",
        "unchanged",
        "stale",
        "dry_run",
        "manual_review",
    ]
    detail: str


class StatusEvidenceSnapshot(StrictModel):
    schema_version: str = "phase5-sync-v1"
    session_id: str
    platform: str
    collected_at: datetime
    collection_status: str
    matched_observations: list[PlatformApplicationObservation] = Field(
        default_factory=list
    )
    unmatched_observation_count: int = 0
    response_endpoints: list[str] = Field(default_factory=list)
    screenshot_path: str | None = None
    privacy_note: str = (
        "仅保存与本地岗位匹配所需的公司、岗位和状态字段；不保存站内消息正文。"
    )


class ApplicationStatusSyncReport(StrictModel):
    schema_version: str = "phase5-sync-v1"
    session_id: str
    platform: str
    started_at: datetime
    finished_at: datetime
    dry_run: bool
    collection_status: str
    observed: int = 0
    matched: int = 0
    updated: int = 0
    unchanged: int = 0
    stale: int = 0
    manual_review: int = 0
    unmatched: int = 0
    evidence_path: str | None = None
    screenshot_path: str | None = None
    decisions: list[StatusSyncDecision] = Field(default_factory=list)
    blocker: str | None = None
