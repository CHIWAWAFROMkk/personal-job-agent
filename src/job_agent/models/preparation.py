from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from job_agent.models.application_tracking import ApplicationStatus
from job_agent.models.base import StrictModel
from job_agent.models.interview_debrief import InterviewDebriefRecord


PreparationPriority = Literal[
    "routine",
    "elevated",
    "high",
    "urgent",
    "critical",
    "closed",
]

MasteryStatus = Literal[
    "mastered",
    "partial",
    "not_evidenced",
    "unknown",
]

LearningHorizon = Literal["1_hour", "1_day", "3_days", "7_days"]


class PreparationJobSnapshot(StrictModel):
    job_id: int
    company: str
    title: str
    location: str | None = None
    match_score: int
    trigger_status: ApplicationStatus
    priority: PreparationPriority


class RoleKnowledge(StrictModel):
    source_note: str
    actual_work: list[str] = Field(default_factory=list)
    daily_work: list[str] = Field(default_factory=list)
    likely_kpis: list[str] = Field(default_factory=list)
    workflow: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    industry_knowledge: list[str] = Field(default_factory=list)


class CapabilityGap(StrictModel):
    requirement: str
    category: str
    mastery: MasteryStatus
    evidence_fact_ids: list[str] = Field(default_factory=list)
    explanation: str
    learning_priority: int = Field(ge=1, le=5)


class LearningTask(StrictModel):
    sequence: int = Field(ge=1)
    title: str
    minutes: int = Field(ge=5)
    objective: str
    deliverable: str
    related_requirements: list[str] = Field(default_factory=list)


class LearningPlan(StrictModel):
    horizon: LearningHorizon
    total_minutes: int = Field(ge=5)
    outcome: str
    tasks: list[LearningTask] = Field(default_factory=list)


class InterviewQuestion(StrictModel):
    category: Literal["motivation", "role", "professional", "resume", "behavioral", "case"]
    question: str
    answer_framework: list[str] = Field(default_factory=list)
    evidence_fact_ids: list[str] = Field(default_factory=list)
    truthfulness_guard: str


class StarPreparationCard(StrictModel):
    title: str
    fact_id: str
    confirmed_evidence: str
    situation_prompt: str
    task_prompt: str
    action_prompt: str
    result_prompt: str


class PreparationPack(StrictModel):
    schema_version: str = "phase6-v1"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    job: PreparationJobSnapshot
    role_knowledge: RoleKnowledge
    capability_gaps: list[CapabilityGap] = Field(default_factory=list)
    learning_plans: list[LearningPlan] = Field(default_factory=list)
    interview_questions: list[InterviewQuestion] = Field(default_factory=list)
    star_cards: list[StarPreparationCard] = Field(default_factory=list)
    evidence_statements: dict[str, str] = Field(default_factory=dict)
    truthfulness_notes: list[str] = Field(default_factory=list)
    debriefs: list[InterviewDebriefRecord] = Field(default_factory=list)
