from __future__ import annotations

from typing import Literal

from pydantic import Field

from job_agent.models.base import StrictModel


class JobRecordInput(StrictModel):
    company: str = Field(min_length=1)
    title: str = Field(min_length=1)
    jd_text: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_url: str | None = None
    external_id: str | None = None
    location: str | None = None
    published_at: str | None = None
    deadline_at: str | None = None


class JobUpsertResult(StrictModel):
    job_id: int
    created: bool
    source_added: bool
    dedupe_key: str
    dedupe_reason: str


class JobListItem(StrictModel):
    job_id: int
    company: str
    title: str
    location: str | None = None
    status: str
    match_score: int | None = None
    recommendation: str | None = None
    commute_minutes: int | None = None
    commute_method: str = ""
    commute_note: str = ""
    commute_origin: str = ""
    commute_destination: str = ""
    commute_mode: str = ""
    commute_distance_meters: int | None = None
    commute_route_summary: str = ""
    commute_provider: str = ""
    commute_updated_at: str | None = None
    source_count: int
    first_seen_at: str
    last_seen_at: str


class JobSourceItem(StrictModel):
    platform: str
    source_url: str | None = None
    external_id: str | None = None
    first_seen_at: str
    last_seen_at: str


class JobDetail(StrictModel):
    job_id: int
    company: str
    title: str
    location: str | None = None
    jd_text: str
    published_at: str | None = None
    deadline_at: str | None = None
    status: str
    match_score: int | None = None
    recommendation: str | None = None
    commute_minutes: int | None = None
    commute_method: str = ""
    commute_note: str = ""
    commute_origin: str = ""
    commute_destination: str = ""
    commute_mode: str = ""
    commute_distance_meters: int | None = None
    commute_route_summary: str = ""
    commute_provider: str = ""
    commute_updated_at: str | None = None
    sources: list[JobSourceItem] = Field(default_factory=list)
    created_at: str
    updated_at: str
    first_seen_at: str
    last_seen_at: str


JobSourceRoute = Literal[
    "official_ats",
    "established_board",
    "direct_site",
    "manual",
    "unknown",
]
JobFreshnessState = Literal[
    "expired",
    "fresh",
    "aging",
    "stale",
    "recently_seen",
    "needs_recheck",
    "unknown",
]
JobSourceConfidence = Literal["high", "medium", "low"]


class JobSourceReview(StrictModel):
    job_id: int
    source_route: JobSourceRoute
    source_label: str
    source_confidence: JobSourceConfidence
    freshness: JobFreshnessState
    freshness_label: str
    age_days: int | None = Field(default=None, ge=0)
    positive_signals: list[str] = Field(default_factory=list)
    review_items: list[str] = Field(default_factory=list)
    recommended_action: str
    note: str = "来源分级与时效只决定复核优先级，不代表岗位真实性或仍在招聘。"


class JobDatabaseStats(StrictModel):
    jobs: int
    sources: int
    merged_source_records: int
    match_results: int
    recommended: int
    strongly_recommended: int
    candidates: int
    pending_candidates: int
    live_candidates: int
    expired_candidates: int
    blocked_candidates: int
    irrelevant_candidates: int
    manual_review_candidates: int


CandidateVerificationStatus = Literal[
    "pending",
    "live",
    "expired",
    "blocked",
    "irrelevant",
    "needs_manual_review",
]


class SearchCandidateInput(StrictModel):
    provider: str = Field(min_length=1)
    query: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    snippet: str = ""


class SearchCandidateUpsertResult(StrictModel):
    candidate_id: int
    created: bool
    sighting_added: bool
    canonical_url: str


class SearchCandidateListItem(StrictModel):
    candidate_id: int
    title: str
    url: str
    snippet: str
    verification_status: CandidateVerificationStatus
    verification_detail: str
    source_count: int
    first_seen_at: str
    last_seen_at: str
    verified_at: str | None = None
