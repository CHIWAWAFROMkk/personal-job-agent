from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from job_agent.models.base import StrictModel


InterviewStage = Literal[
    "screening",
    "assessment",
    "written_test",
    "interview_1",
    "interview_2",
    "final_interview",
]


class InterviewDebriefInput(StrictModel):
    stage: InterviewStage
    question: str = Field(min_length=1, max_length=1000)
    answer: str = Field(min_length=1, max_length=6000)
    better_answer: str = Field(default="", max_length=6000)
    evidence_fact_ids: list[str] = Field(default_factory=list, max_length=12)
    strengths: list[str] = Field(default_factory=list, max_length=12)
    gaps: list[str] = Field(default_factory=list, max_length=12)
    next_actions: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("question", "answer", "better_answer")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("question", "answer")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value:
            raise ValueError("问题和原回答不能只包含空白。")
        return value

    @field_validator(
        "evidence_fact_ids",
        "strengths",
        "gaps",
        "next_actions",
    )
    @classmethod
    def clean_lists(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            value = item.strip()
            if not value:
                continue
            if len(value) > 500:
                raise ValueError("单条复盘要点最多 500 个字符。")
            if value not in cleaned:
                cleaned.append(value)
        return cleaned


class InterviewDebriefRecord(InterviewDebriefInput):
    debrief_id: int
    application_id: int
    job_id: int
    created_at: str


class InterviewDebriefSaveResult(StrictModel):
    debrief_id: int
    created: bool
