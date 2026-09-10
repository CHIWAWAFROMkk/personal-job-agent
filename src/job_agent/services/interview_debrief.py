from __future__ import annotations

from job_agent.models.interview_debrief import (
    InterviewDebriefInput,
    InterviewDebriefSaveResult,
)
from job_agent.models.profile import Profile
from job_agent.services.job_repository import JobRepository


class InterviewDebriefError(ValueError):
    pass


def record_interview_debrief(
    repository: JobRepository,
    profile: Profile,
    job_id: int,
    entry: InterviewDebriefInput,
) -> InterviewDebriefSaveResult:
    if repository.get_application(job_id) is None:
        raise InterviewDebriefError(
            f"岗位 #{job_id} 尚无投递记录；请先记录真实投递或招聘进度。"
        )

    approved_fact_ids = {
        fact.id
        for experience in profile.experiences
        for fact in experience.facts
        if profile.is_application_ready(fact.status)
    }
    unknown = sorted(set(entry.evidence_fact_ids) - approved_fact_ids)
    if unknown:
        raise InterviewDebriefError(
            "复盘引用了不存在或尚未确认的事实 ID: " + "、".join(unknown)
        )
    return repository.add_interview_debrief(job_id, entry)
