from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from job_agent.models.application_tracking import ApplicationStatus
from job_agent.models.base import StrictModel
from job_agent.models.preparation import PreparationPriority


class DashboardMetric(StrictModel):
    metric_id: str
    label: str
    value: int | float
    unit: str = ""
    description: str
    tone: str = "neutral"


class DashboardFunnelStage(StrictModel):
    stage_id: str
    label: str
    value: int
    definition: str


class DashboardJobWorkspace(StrictModel):
    resume_status: Literal["missing", "needs_review", "ready"] = "missing"
    resume_pdf_url: str | None = None
    resume_docx_url: str | None = None
    resume_editable: bool = False
    application_pack_ready: bool = False
    safe_fill_ready: bool = False
    blockers: list[str] = Field(default_factory=list)


class DashboardJobRow(StrictModel):
    job_id: int
    company: str
    title: str
    location: str | None = None
    match_score: int | None = None
    recommendation: str = ""
    status: str
    has_applied: bool = False
    applied_at: str | None = None
    source_url: str | None = None
    opportunity_track: Literal["daily_internship", "autumn_recruitment", "other"] = "other"
    role_tier: Literal["primary", "secondary", "stretch", "other"] = "other"
    strategy_fit: Literal["recommended", "manual_review", "blocked"] = "manual_review"
    strategy_priority: int = Field(default=0, ge=0, le=100)
    strategy_reasons: list[str] = Field(default_factory=list)
    compensation_min_daily: int | None = None
    compensation_max_daily: int | None = None
    compensation_fit: Literal["meets_floor", "below_floor", "unknown", "not_applicable"] = "unknown"
    outsourcing_risk: bool = False
    deadline_days: int | None = None
    deadline_urgent: bool = False
    commute_minutes: int | None = None
    commute_method: str = ""
    commute_note: str = ""
    commute_origin: str = ""
    commute_destination: str = ""
    commute_mode: str = ""
    commute_distance_meters: int | None = None
    commute_route_summary: str = ""
    commute_provider: str = ""
    commute_fit: Literal["good", "near_limit", "over_limit", "unknown"] = "unknown"
    workspace: DashboardJobWorkspace = Field(default_factory=DashboardJobWorkspace)


class DashboardPreparationRow(StrictModel):
    job_id: int
    company: str
    title: str
    application_status: ApplicationStatus
    priority: PreparationPriority
    match_score: int | None = None
    preparation_path: str | None = None


class DashboardFeedbackRow(StrictModel):
    job_id: int
    company: str
    title: str
    previous_status: ApplicationStatus | None = None
    status: ApplicationStatus
    source: str
    detail: str = ""
    occurred_at: datetime


class DashboardBreakdownRow(StrictModel):
    status_id: str
    label: str
    value: int
    tone: str = "neutral"


class DashboardProfileSummary(StrictModel):
    display_name: str = ""
    stage: str = ""
    target_roles: list[str] = Field(default_factory=list)
    adjacent_roles: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    must_haves: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    availability: str = ""
    earliest_start: str | None = None
    days_per_week: int | None = None
    duration_months: int | None = None
    commute_origin: str = ""
    max_one_way_minutes: int | None = None
    transport_modes: list[str] = Field(default_factory=list)
    remote_acceptable: bool = False
    internship_daily_pay_floor: int | None = None
    exclude_outsourcing: bool = True
    resume_count: int = 0
    confirmed_fact_count: int = 0
    skill_count: int = 0
    updated_at: datetime | None = None


class DashboardStrategySummary(StrictModel):
    recommended_track: Literal["daily_internship", "autumn_recruitment"]
    daily_internship_share: int = Field(ge=0, le=100)
    autumn_recruitment_share: int = Field(ge=0, le=100)
    rule_label: str
    deadline_urgent_days: int = 14
    experiment_batch_size: int = 20
    internship_observation_days: int = 7
    autumn_observation_days: int = 14
    thirty_day_application_goal: int = 60
    applications_last_30_days: int = 0
    meaningful_progress_last_30_days: int = 0
    internship_secured: bool = False


class DashboardSnapshot(StrictModel):
    schema_version: str = "phase13-dual-track-workshop-v1"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    timezone: str = "Asia/Shanghai"
    source_name: str = "本地 SQLite"
    source_path: str
    source_freshness_at: datetime
    metrics: list[DashboardMetric] = Field(default_factory=list)
    funnel: list[DashboardFunnelStage] = Field(default_factory=list)
    jobs_to_apply: list[DashboardJobRow] = Field(default_factory=list)
    tracked_jobs: list[DashboardJobRow] = Field(default_factory=list)
    priority_preparation: list[DashboardPreparationRow] = Field(default_factory=list)
    recent_feedback: list[DashboardFeedbackRow] = Field(default_factory=list)
    candidate_breakdown: list[DashboardBreakdownRow] = Field(default_factory=list)
    active_profile: DashboardProfileSummary | None = None
    strategy: DashboardStrategySummary
    action_token: str = ""
    caveats: list[str] = Field(default_factory=list)
