from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from job_agent.models.base import StrictModel


class ClaimStatus(StrEnum):
    DOCUMENTED = "documented"
    USER_CONFIRMED = "user_confirmed"
    NEEDS_CONFIRMATION = "needs_confirmation"


class ExperienceKind(StrEnum):
    INTERNSHIP = "internship"
    EMPLOYMENT = "employment"
    PROJECT = "project"
    CAMPUS = "campus"
    COMPETITION = "competition"
    VOLUNTEER = "volunteer"
    GAME = "game"
    OTHER = "other"


class SkillLevel(StrEnum):
    AWARENESS = "awareness"
    BASIC = "basic"
    WORKING = "working"
    ADVANCED = "advanced"
    EXPERT = "expert"


class ContactInfo(StrictModel):
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    website: str | None = None


class Person(StrictModel):
    display_name: str = ""
    legal_name: str | None = None
    current_city: str | None = None
    contact: ContactInfo = Field(default_factory=ContactInfo)


class SourceDocument(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    kind: str
    original_path: str
    sha256: str | None = None
    imported_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    notes: str = ""


class Metric(StrictModel):
    label: str
    value: str
    context: str = ""
    status: ClaimStatus = ClaimStatus.NEEDS_CONFIRMATION


class EvidenceFact(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    statement: str = Field(min_length=1)
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    status: ClaimStatus = ClaimStatus.NEEDS_CONFIRMATION
    source_ids: list[str] = Field(default_factory=list)
    interview_notes: str = ""


class Education(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    institution: str
    degree: str = ""
    major: str = ""
    start: str | None = None
    end: str | None = None
    location: str | None = None
    gpa: str | None = None
    coursework: list[str] = Field(default_factory=list)
    status: ClaimStatus = ClaimStatus.NEEDS_CONFIRMATION
    source_ids: list[str] = Field(default_factory=list)


class Experience(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    kind: ExperienceKind
    organization: str = ""
    role: str
    start: str | None = None
    end: str | None = None
    location: str | None = None
    summary: str = ""
    facts: list[EvidenceFact] = Field(default_factory=list)


class Skill(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    name: str
    category: str = "other"
    level: SkillLevel = SkillLevel.WORKING
    aliases: list[str] = Field(default_factory=list)
    evidence_fact_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    status: ClaimStatus = ClaimStatus.NEEDS_CONFIRMATION
    last_used: str | None = None


class InterviewStory(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    title: str
    situation: str
    task: str
    action: str
    result: str
    evidence_fact_ids: list[str] = Field(default_factory=list)
    status: ClaimStatus = ClaimStatus.NEEDS_CONFIRMATION


class Availability(StrictModel):
    earliest_start: str | None = None
    # days_per_week/duration_months represent the guaranteed minimum.
    days_per_week: int | None = Field(default=None, ge=1, le=7)
    max_days_per_week: int | None = Field(default=None, ge=1, le=7)
    duration_months: int | None = Field(default=None, ge=1, le=60)
    max_duration_months: int | None = Field(default=None, ge=1, le=60)
    notes: str = ""

    @model_validator(mode="after")
    def validate_ranges(self) -> Availability:
        if self.max_days_per_week is not None:
            if self.days_per_week is None:
                raise ValueError("填写每周最多到岗天数前，必须填写保证到岗天数。")
            if self.max_days_per_week < self.days_per_week:
                raise ValueError("每周最多到岗天数不能低于保证到岗天数。")
        if self.max_duration_months is not None:
            if self.duration_months is None:
                raise ValueError("填写最长实习月数前，必须填写保证实习月数。")
            if self.max_duration_months < self.duration_months:
                raise ValueError("最长实习月数不能低于保证实习月数。")
        return self


class CommutePreferences(StrictModel):
    """User-owned commuting limits used as an explainable job filter."""

    origin: str = ""
    max_one_way_minutes: int | None = Field(default=None, ge=5, le=240)
    transport_modes: list[str] = Field(default_factory=list)
    remote_acceptable: bool = False
    notes: str = ""


class JobSearchPreferences(StrictModel):
    stage: str = ""
    target_roles: list[str] = Field(default_factory=list)
    adjacent_roles: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    availability: Availability = Field(default_factory=Availability)
    commute: CommutePreferences = Field(default_factory=CommutePreferences)
    internship_daily_pay_floor: int | None = Field(default=None, ge=0, le=5000)
    exclude_outsourcing: bool = True
    must_haves: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    notes: str = ""


class Profile(StrictModel):
    schema_version: str = "1.0"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    person: Person = Field(default_factory=Person)
    job_search: JobSearchPreferences = Field(default_factory=JobSearchPreferences)
    education: list[Education] = Field(default_factory=list)
    experiences: list[Experience] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    stories: list[InterviewStory] = Field(default_factory=list)
    source_documents: list[SourceDocument] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ids_and_references(self) -> Profile:
        def ensure_unique(values: list[str], label: str) -> None:
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise ValueError(f"{label} ID 重复: {', '.join(duplicates)}")

        source_ids = [source.id for source in self.source_documents]
        education_ids = [item.id for item in self.education]
        experience_ids = [item.id for item in self.experiences]
        skill_ids = [item.id for item in self.skills]
        story_ids = [item.id for item in self.stories]
        facts = [fact for experience in self.experiences for fact in experience.facts]
        fact_ids = [fact.id for fact in facts]

        ensure_unique(source_ids, "来源文档")
        ensure_unique(education_ids, "教育经历")
        ensure_unique(experience_ids, "经历")
        ensure_unique(skill_ids, "技能")
        ensure_unique(story_ids, "面试故事")
        ensure_unique(fact_ids, "事实")

        known_sources = set(source_ids)
        known_facts = set(fact_ids)
        for label, references in self._source_references():
            unknown = sorted(set(references) - known_sources)
            if unknown:
                raise ValueError(f"{label} 引用了不存在的来源文档: {', '.join(unknown)}")
        for label, references in self._fact_references():
            unknown = sorted(set(references) - known_facts)
            if unknown:
                raise ValueError(f"{label} 引用了不存在的事实: {', '.join(unknown)}")
        return self

    def _source_references(self) -> list[tuple[str, list[str]]]:
        references: list[tuple[str, list[str]]] = []
        references.extend((f"教育经历 {item.id}", item.source_ids) for item in self.education)
        for experience in self.experiences:
            references.extend(
                (f"事实 {fact.id}", fact.source_ids) for fact in experience.facts
            )
        references.extend((f"技能 {item.id}", item.source_ids) for item in self.skills)
        return references

    def _fact_references(self) -> list[tuple[str, list[str]]]:
        references: list[tuple[str, list[str]]] = []
        references.extend(
            (f"技能 {item.id}", item.evidence_fact_ids) for item in self.skills
        )
        references.extend(
            (f"面试故事 {item.id}", item.evidence_fact_ids) for item in self.stories
        )
        return references

    @staticmethod
    def is_application_ready(status: ClaimStatus) -> bool:
        return status in {ClaimStatus.DOCUMENTED, ClaimStatus.USER_CONFIRMED}

    def application_context(self) -> dict[str, Any]:
        """Return only verified, non-contact evidence suitable for model prompts."""

        education = [
            item.model_dump(mode="json", exclude={"source_ids"})
            for item in self.education
            if self.is_application_ready(item.status)
        ]
        experiences: list[dict[str, Any]] = []
        for experience in self.experiences:
            facts = [
                fact.model_dump(mode="json", exclude={"source_ids", "interview_notes"})
                for fact in experience.facts
                if self.is_application_ready(fact.status)
            ]
            if facts:
                item = experience.model_dump(mode="json", exclude={"facts"})
                item["facts"] = facts
                experiences.append(item)
        skills = [
            item.model_dump(mode="json", exclude={"source_ids"})
            for item in self.skills
            if self.is_application_ready(item.status)
        ]
        stories = [
            item.model_dump(mode="json")
            for item in self.stories
            if self.is_application_ready(item.status)
        ]
        return {
            "profile_schema_version": self.schema_version,
            "job_search": self.job_search.model_dump(mode="json"),
            "education": education,
            "experiences": experiences,
            "skills": skills,
            "stories": stories,
        }

    def confirm_all_pending_claims(self) -> tuple[Profile, int]:
        """Return a copy with every pending claim explicitly user-confirmed."""

        payload = self.model_dump(mode="json")
        confirmed = 0

        def visit(value: Any) -> None:
            nonlocal confirmed
            if isinstance(value, dict):
                if value.get("status") == ClaimStatus.NEEDS_CONFIRMATION.value:
                    value["status"] = ClaimStatus.USER_CONFIRMED.value
                    confirmed += 1
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)
        return Profile.model_validate(payload), confirmed


def empty_profile() -> Profile:
    return Profile()
