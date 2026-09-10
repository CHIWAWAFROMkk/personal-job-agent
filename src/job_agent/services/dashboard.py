from __future__ import annotations

import json
import re
import secrets
import shutil
import socket
import subprocess
import threading
import webbrowser
from datetime import UTC, date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from job_agent.models.dashboard import (
    DashboardBreakdownRow,
    DashboardFeedbackRow,
    DashboardFunnelStage,
    DashboardJobRow,
    DashboardJobWorkspace,
    DashboardMetric,
    DashboardPreparationRow,
    DashboardProfileSummary,
    DashboardSnapshot,
    DashboardStrategySummary,
)
from job_agent.models.job import MatchResult
from job_agent.models.job_record import JobDetail
from job_agent.models.profile import Profile
from job_agent.services.application_pack import (
    ApplicationPackError,
    build_application_pack,
    resolve_resume_bundle,
    write_application_pack,
)
from job_agent.services.browser_assist import (
    BrowserAssistError,
    create_application_session,
    find_application_pack,
    run_browser_session,
)
from job_agent.services.copilot_chat import (
    CopilotChatError,
    copilot_snapshot,
    reset_copilot_thread,
    respond_to_copilot,
)
from job_agent.services.commute_routing import (
    AmapCommuteProvider,
    CommuteRoutingError,
)
from job_agent.services.api_usage import load_api_usage, record_api_usage
from job_agent.services.job_repository import JobDatabaseError, JobRepository
from job_agent.services.job_strategy import JobStrategyResult, evaluate_job_strategy
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.portable_resume import (
    PortableResumeError,
    build_portable_resume_content,
    build_portable_resume_draft,
    find_latest_resume_manifest,
    find_profile_photo,
    resume_artifact_from_manifest,
    save_profile_photo,
)
from job_agent.services.preparation_pack import preparation_priority_for_status
from job_agent.services.profile_onboarding import (
    ProfileOnboardingError,
    ProfileOnboardingInput,
    onboard_profile,
)
from job_agent.services.profile_preferences import (
    ProfilePreferencesError,
    ProfilePreferencesUpdate,
    update_profile_preferences,
)
from job_agent.services.profile_store import ProfileStoreError, load_profile
from job_agent.services.project_workshop import (
    ProjectWorkshopError,
    project_preview_path,
    project_workshop_snapshot,
    run_project,
    verify_project,
)
from job_agent.services.resume_editor import (
    ResumeEditorError,
    load_latest_resume_content,
    rerender_edited_resume,
    validate_resume_content,
)
from job_agent.services.resume_polish import (
    CloudAIUnavailableError,
    ResumePolishError,
    polish_resume_content_locally,
    polish_resume_content_with_jd,
)
from job_agent.services.resume_compose import compose_resume_content_with_jd
from job_agent.services.resume_import import (
    ResumeImportError,
    import_resume_text_into_draft,
)
from job_agent.services.resume_reader import ResumeReadError
from job_agent.services.runtime_config import (
    RuntimeConfigError,
    RuntimeConfigUpdate,
    effective_runtime_config,
    public_runtime_config,
    update_runtime_config,
)
from job_agent.services.tailored_resume import TailoredResumeError, approve_resume_visual_review


class DashboardError(RuntimeError):
    pass


_LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
_POSITIVE_FEEDBACK_STATUSES = {
    "resume_requested",
    "screening",
    "assessment",
    "written_test",
    "interview_1",
    "interview_2",
    "final_interview",
    "offer",
}
_MEANINGFUL_FEEDBACK_STATUSES = _POSITIVE_FEEDBACK_STATUSES | {"rejected"}
_DASHBOARD_VERSION = "phase13-dual-track-workshop-v1"
_MAX_JSON_BODY = 16 * 1024
_MAX_UPLOAD_BODY = 12 * 1024 * 1024


def _profile_summary(profile_path: Path | None) -> DashboardProfileSummary | None:
    if profile_path is None or not profile_path.is_file():
        return None
    try:
        profile = load_profile(profile_path)
    except ProfileStoreError:
        return None
    availability = profile.job_search.availability
    commute = profile.job_search.commute
    availability_parts: list[str] = []
    if availability.days_per_week:
        availability_parts.append(f"每周 {availability.days_per_week} 天")
    if availability.duration_months:
        availability_parts.append(f"至少 {availability.duration_months} 个月")
    if availability.earliest_start:
        availability_parts.append(f"{availability.earliest_start} 起")
    confirmed_facts = sum(
        profile.is_application_ready(fact.status)
        for experience in profile.experiences
        for fact in experience.facts
    )
    return DashboardProfileSummary(
        display_name=profile.person.display_name,
        stage=profile.job_search.stage,
        target_roles=profile.job_search.target_roles,
        adjacent_roles=profile.job_search.adjacent_roles,
        target_industries=profile.job_search.target_industries,
        preferred_locations=profile.job_search.preferred_locations,
        employment_types=profile.job_search.employment_types,
        must_haves=profile.job_search.must_haves,
        avoid=profile.job_search.avoid,
        availability=" · ".join(availability_parts),
        earliest_start=availability.earliest_start,
        days_per_week=availability.days_per_week,
        duration_months=availability.duration_months,
        commute_origin=commute.origin,
        max_one_way_minutes=commute.max_one_way_minutes,
        transport_modes=commute.transport_modes,
        remote_acceptable=commute.remote_acceptable,
        internship_daily_pay_floor=profile.job_search.internship_daily_pay_floor,
        exclude_outsourcing=profile.job_search.exclude_outsourcing,
        resume_count=sum(
            source.kind == "resume" for source in profile.source_documents
        ),
        confirmed_fact_count=confirmed_facts,
        skill_count=sum(
            profile.is_application_ready(skill.status) for skill in profile.skills
        ),
        updated_at=profile.updated_at,
    )


def _commute_fit(
    commute_minutes: int | None,
    max_one_way_minutes: int | None,
) -> str:
    if commute_minutes is None or max_one_way_minutes is None:
        return "unknown"
    if commute_minutes > max_one_way_minutes:
        return "over_limit"
    if commute_minutes >= max_one_way_minutes * 0.8:
        return "near_limit"
    return "good"


def _split_values(value: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,，、;；\n]+", value or "")
        if item.strip()
    ]


def _optional_int(value: str, *, label: str, minimum: int, maximum: int) -> int | None:
    if not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ProfileOnboardingError(f"{label}必须填写整数。") from exc
    if parsed < minimum or parsed > maximum:
        raise ProfileOnboardingError(f"{label}必须在 {minimum}-{maximum} 之间。")
    return parsed


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], str, bytes]:
    try:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise ProfileOnboardingError("无法解析上传表单。") from exc
    if not message.is_multipart():
        raise ProfileOnboardingError("上传表单格式不正确。")
    fields: dict[str, str] = {}
    resume_filename = ""
    resume_bytes = b""
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if name == "resume" and filename:
            resume_filename = filename
            resume_bytes = payload
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            fields[name] = payload.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise ProfileOnboardingError(f"表单字段 {name} 不是有效文字。") from exc
    return fields, resume_filename, resume_bytes


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _local_date(value: str) -> date:
    return _parse_datetime(value).astimezone(_LOCAL_TIMEZONE).date()


def _latest_preparation_path(output_dir: Path, job_id: int) -> Path | None:
    root = output_dir / "preparation-packs"
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    try:
        for job_root in root.glob(f"job-{job_id}-*"):
            candidates.extend(job_root.glob("*/岗位学习与面试准备.md"))
    except OSError:
        return None
    files = [path for path in candidates if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def _source_freshness(repository: JobRepository) -> datetime:
    candidates = [repository.path]
    # The shared-memory file is touched by read-only SQLite connections, so it
    # would falsely make every page refresh look like a data update. The main
    # database and write-ahead log represent actual persisted changes.
    candidates.append(Path(str(repository.path) + "-wal"))
    mtimes = [path.stat().st_mtime for path in candidates if path.is_file()]
    return datetime.fromtimestamp(max(mtimes), tz=UTC) if mtimes else datetime.now(UTC)


def _job_source_url(job: JobDetail) -> str | None:
    return next((source.source_url for source in job.sources if source.source_url), None)


def _safe_slug(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", value).strip("-")
    return normalized[:48] or fallback


def _stage_resume_for_manual_upload(
    profile: Profile,
    job: JobDetail,
    resume_path: Path,
    *,
    output_dir: Path,
) -> Path:
    """Copy the approved PDF to one predictable, human-readable folder."""

    source = resume_path.expanduser().resolve()
    if not source.is_file():
        raise BrowserAssistError("岗位专属 PDF 不存在，无法准备人工上传。")
    target_dir = (output_dir / "ready-to-upload").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    name = _safe_slug(profile.person.display_name or "求职者", fallback="求职者")[:20]
    company = _safe_slug(job.company, fallback="公司")[:28]
    title = _safe_slug(job.title, fallback="岗位")[:36]
    target = target_dir / f"{name}-{company}-{title}-岗位专用简历.pdf"
    shutil.copy2(source, target)
    return target


def _reveal_resume_in_explorer(path: Path) -> bool:
    """Select the staged file in Explorer; failure never blocks opening the job page."""

    try:
        subprocess.Popen(
            ["explorer.exe", f"/select,{path}"],
            close_fds=True,
        )
    except OSError:
        return False
    return True


def _job_workspace(
    profile: Profile | None,
    job: JobDetail,
    *,
    output_dir: Path,
) -> DashboardJobWorkspace:
    blockers: list[str] = []
    if profile is None:
        return DashboardJobWorkspace(blockers=["先建立个人资料并确认真实经历。"])

    try:
        resume = resolve_resume_bundle(profile, job, output_dir / "applications")
    except (ApplicationPackError, OSError):
        resume = None
        blockers.append("岗位专属简历文件需要重新检查。")
    manifest = find_latest_resume_manifest(output_dir / "applications", job.job_id)
    latest_visual_status = ""
    if manifest is not None:
        try:
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            latest_visual_status = str(
                manifest_payload.get("qa", {}).get("pdf_visual_review", "")
            )
        except (OSError, json.JSONDecodeError, AttributeError):
            blockers.append("最新简历质检清单需要重新检查。")

    if resume is None or resume.status == "needs_generation":
        resume_status = "missing"
        blockers.append("先生成岗位专属简历草稿。")
    elif latest_visual_status == "pending_user_review" or resume.status == "needs_review":
        resume_status = "needs_review"
        blockers.append("打开 PDF 并由本人确认版面后才能准备投递文件。")
    else:
        resume_status = "ready"

    pdf_url = f"/resume-draft/{job.job_id}/pdf" if manifest else None
    docx_url = f"/resume-draft/{job.job_id}/docx" if manifest else None

    application_pack_ready = False
    try:
        _, pack = find_application_pack(output_dir / "application-packs", job.job_id)
    except BrowserAssistError:
        pack = None
    if pack is not None:
        application_pack_ready = pack.resume.status == "ready" and pack.resume.qa_verified
    if resume_status == "ready" and not application_pack_ready:
        blockers.append("投递材料包尚未就绪，请重新确认简历。")

    source_url = _job_source_url(job)
    safe_fill_ready = (
        resume_status == "ready" and application_pack_ready and bool(source_url)
    )
    if application_pack_ready and not source_url:
        blockers.append("岗位缺少可打开的招聘页面链接。")
    return DashboardJobWorkspace(
        resume_status=resume_status,
        resume_pdf_url=pdf_url,
        resume_docx_url=docx_url,
        resume_editable=manifest is not None,
        application_pack_ready=application_pack_ready,
        safe_fill_ready=safe_fill_ready,
        blockers=list(dict.fromkeys(blockers)),
    )


def _ensure_match(
    repository: JobRepository,
    profile: Profile,
    job: JobDetail,
) -> MatchResult:
    existing = repository.get_latest_match_result(job.job_id)
    if existing is not None:
        return existing
    structured = structure_job_locally(
        job.jd_text,
        company=job.company,
        title=job.title,
        location=job.location,
        source=job.sources[0].platform if job.sources else "dashboard",
        source_url=_job_source_url(job),
    )
    result = match_job_locally(profile, structured)
    repository.add_match_result(job.job_id, result)
    return result


def _application_pack_output_dir(output_dir: Path, job: JobDetail) -> Path:
    job_root = (
        output_dir
        / "application-packs"
        / f"job-{job.job_id}-{_safe_slug(job.company, fallback='company')}-{_safe_slug(job.title, fallback='role')}"
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return job_root / stamp


def build_dashboard_snapshot(
    repository: JobRepository,
    *,
    output_dir: Path,
    today: date | None = None,
    profile_path: Path | None = None,
    action_token: str = "",
) -> DashboardSnapshot:
    repository.verify()
    today = today or datetime.now(_LOCAL_TIMEZONE).date()
    stats = repository.stats()
    application_summary = repository.application_summary()
    jobs = repository.list_jobs(limit=1000)
    applications = repository.list_applications(limit=1000)
    candidates = repository.list_search_candidates(limit=1000)
    application_by_job = {item.job_id: item for item in applications}
    profile_summary = _profile_summary(profile_path)
    profile: Profile | None = None
    if profile_path is not None and profile_path.is_file():
        try:
            profile = load_profile(profile_path)
        except ProfileStoreError:
            profile = None
    max_commute_minutes = (
        profile_summary.max_one_way_minutes if profile_summary else None
    )
    daily_pay_floor = (
        profile_summary.internship_daily_pay_floor if profile_summary else None
    )
    exclude_outsourcing = (
        profile_summary.exclude_outsourcing if profile_summary else True
    )

    today_discovered = sum(_local_date(item.first_seen_at) == today for item in candidates)
    jobs_to_apply: list[DashboardJobRow] = []
    tracked_jobs: list[DashboardJobRow] = []
    strategy_by_job: dict[int, JobStrategyResult] = {}
    commute_filtered = 0
    outsourcing_filtered = 0
    pay_review_count = 0
    for job in jobs:
        application = application_by_job.get(job.job_id)
        detail = repository.get_job(job.job_id)
        source_url = _job_source_url(detail)
        commute_fit = _commute_fit(job.commute_minutes, max_commute_minutes)
        strategy = evaluate_job_strategy(
            detail,
            match_score=job.match_score,
            commute_fit=commute_fit,
            daily_pay_floor=daily_pay_floor,
            exclude_outsourcing=exclude_outsourcing,
            primary_roles=(profile_summary.target_roles if profile_summary else []),
            adjacent_roles=(profile_summary.adjacent_roles if profile_summary else []),
            today=today,
        )
        strategy_by_job[job.job_id] = strategy
        row = DashboardJobRow(
            job_id=job.job_id,
            company=job.company,
            title=job.title,
            location=job.location,
            match_score=job.match_score,
            recommendation=job.recommendation or "",
            status=application.status if application else job.status,
            has_applied=bool(application and application.applied_at),
            applied_at=application.applied_at if application else None,
            source_url=source_url,
            opportunity_track=strategy.opportunity_track,
            role_tier=strategy.role_tier,
            strategy_fit=strategy.strategy_fit,
            strategy_priority=strategy.priority_score,
            strategy_reasons=strategy.reasons,
            compensation_min_daily=strategy.compensation_min_daily,
            compensation_max_daily=strategy.compensation_max_daily,
            compensation_fit=strategy.compensation_fit,
            outsourcing_risk=strategy.outsourcing_risk,
            deadline_days=strategy.deadline_days,
            deadline_urgent=strategy.deadline_urgent,
            commute_minutes=job.commute_minutes,
            commute_method=job.commute_method,
            commute_note=job.commute_note,
            commute_origin=job.commute_origin,
            commute_destination=job.commute_destination,
            commute_mode=job.commute_mode,
            commute_distance_meters=job.commute_distance_meters,
            commute_route_summary=job.commute_route_summary,
            commute_provider=job.commute_provider,
            commute_fit=commute_fit,
            workspace=_job_workspace(profile, detail, output_dir=output_dir),
        )
        tracked_jobs.append(row)
        if (job.match_score or 0) < 60:
            continue
        if application is not None and application.status not in {
            "saved",
            "ready_to_apply",
        }:
            continue
        if row.commute_fit == "over_limit":
            commute_filtered += 1
            continue
        if strategy.outsourcing_risk and exclude_outsourcing:
            outsourcing_filtered += 1
            continue
        if strategy.compensation_fit in {"below_floor", "unknown"}:
            pay_review_count += 1
        if strategy.strategy_fit == "blocked":
            continue
        jobs_to_apply.append(row)
    commute_order = {"good": 0, "near_limit": 1, "unknown": 2, "over_limit": 3}
    jobs_to_apply.sort(
        key=lambda item: (
            -int(item.deadline_urgent),
            -item.strategy_priority,
            commute_order[item.commute_fit],
            -(item.match_score or -1),
        )
    )
    tracked_jobs.sort(
        key=lambda item: (
            item.status not in {"applied", "hr_read", "resume_requested", "screening", "assessment", "written_test", "interview_1", "interview_2", "final_interview", "offer"},
            -int(item.deadline_urgent),
            -item.strategy_priority,
            -(item.match_score or -1),
            -item.job_id,
        )
    )

    priority_preparation: list[DashboardPreparationRow] = []
    recent_feedback: list[DashboardFeedbackRow] = []
    today_new_feedback = 0
    cutoff_30_days = today - timedelta(days=29)
    meaningful_jobs_last_30_days: set[int] = set()
    for application in applications:
        priority = preparation_priority_for_status(
            application.status,
            application.match_score or 0,
        )
        if priority in {"elevated", "high", "urgent", "critical"}:
            latest_pack = _latest_preparation_path(output_dir, application.job_id)
            priority_preparation.append(
                DashboardPreparationRow(
                    job_id=application.job_id,
                    company=application.company,
                    title=application.title,
                    application_status=application.status,
                    priority=priority,
                    match_score=application.match_score,
                    preparation_path=str(latest_pack) if latest_pack else None,
                )
            )
        for event in repository.list_application_events(application.job_id):
            occurred_at = _parse_datetime(event.occurred_at)
            if (
                occurred_at.astimezone(_LOCAL_TIMEZONE).date() == today
                and event.status in _MEANINGFUL_FEEDBACK_STATUSES
            ):
                today_new_feedback += 1
            if (
                occurred_at.astimezone(_LOCAL_TIMEZONE).date() >= cutoff_30_days
                and event.status in _MEANINGFUL_FEEDBACK_STATUSES
            ):
                meaningful_jobs_last_30_days.add(application.job_id)
            recent_feedback.append(
                DashboardFeedbackRow(
                    job_id=application.job_id,
                    company=application.company,
                    title=application.title,
                    previous_status=event.previous_status,
                    status=event.status,
                    source=event.source,
                    detail=event.detail,
                    occurred_at=occurred_at,
                )
            )
    priority_order = {
        "critical": 0,
        "urgent": 1,
        "high": 2,
        "elevated": 3,
        "routine": 4,
        "closed": 5,
    }
    priority_preparation.sort(
        key=lambda item: (priority_order[item.priority], -(item.match_score or 0))
    )
    recent_feedback.sort(key=lambda item: item.occurred_at, reverse=True)
    recent_feedback = recent_feedback[:12]

    meaningful_response_rate = (
        round(100 * application_summary.meaningful_responses / application_summary.applied_or_later, 1)
        if application_summary.applied_or_later
        else 0.0
    )
    applications_last_30_days = sum(
        bool(application.applied_at)
        and _local_date(str(application.applied_at)) >= cutoff_30_days
        for application in applications
    )
    internship_secured = any(
        application.status == "offer"
        and strategy_by_job.get(application.job_id) is not None
        and strategy_by_job[application.job_id].opportunity_track == "daily_internship"
        for application in applications
    )
    urgent_autumn = any(
        row.opportunity_track == "autumn_recruitment"
        and row.deadline_urgent
        and row.status in {"discovered", "saved", "ready_to_apply", "matched", "recommended"}
        for row in tracked_jobs
    )
    if internship_secured:
        internship_share, autumn_share = 0, 100
        recommended_track = "autumn_recruitment"
        rule_label = "已获得实习 Offer，当前优先处理校招机会。"
    else:
        employment_types = {
            value.strip().lower()
            for value in (profile_summary.employment_types if profile_summary else [])
            if value.strip()
        }
        wants_internship = any(
            keyword in value
            for value in employment_types
            for keyword in ("实习", "intern", "internship")
        )
        wants_campus = any(
            keyword in value
            for value in employment_types
            for keyword in ("校招", "campus", "graduate", "new grad")
        )
        if wants_internship and not wants_campus:
            internship_share, autumn_share = 100, 0
            recommended_track = "daily_internship"
            rule_label = "根据个人求职类型，当前只展示实习机会。"
        elif wants_campus and not wants_internship:
            internship_share, autumn_share = 0, 100
            recommended_track = "autumn_recruitment"
            rule_label = "根据个人求职类型，当前只展示校招机会。"
        else:
            internship_share, autumn_share = 50, 50
            recommended_track = "autumn_recruitment" if urgent_autumn else "daily_internship"
            rule_label = "实习与校招各占一半；临近截止的校招机会优先。"
    strategy_summary = DashboardStrategySummary(
        recommended_track=recommended_track,
        daily_internship_share=internship_share,
        autumn_recruitment_share=autumn_share,
        rule_label=rule_label,
        applications_last_30_days=applications_last_30_days,
        meaningful_progress_last_30_days=len(meaningful_jobs_last_30_days),
        internship_secured=internship_secured,
    )
    metrics = [
        DashboardMetric(
            metric_id="today_discovered",
            label="今日发现",
            value=today_discovered,
            description="今天首次进入搜索候选库的链接数。",
            tone="accent",
        ),
        DashboardMetric(
            metric_id="candidates",
            label="候选总数",
            value=stats.candidates,
            description="搜索 API 已发现并完成链接去重的候选数。",
        ),
        DashboardMetric(
            metric_id="jobs",
            label="正式岗位",
            value=stats.jobs,
            description="已取得完整 JD 并进入正式岗位库的去重岗位数。",
        ),
        DashboardMetric(
            metric_id="recommended",
            label="推荐岗位",
            value=stats.recommended,
            description="当前匹配分不低于 75 分的岗位数。",
            tone="positive",
        ),
        DashboardMetric(
            metric_id="strongly_recommended",
            label="强烈推荐",
            value=stats.strongly_recommended,
            description="当前匹配分不低于 85 分的岗位数。",
            tone="positive",
        ),
        DashboardMetric(
            metric_id="pending_apply",
            label="待确认投递",
            value=len(jobs_to_apply),
            description="匹配分不低于 60、尚未投递，且未触发外包或通勤硬边界的岗位。",
            tone="warning",
        ),
        DashboardMetric(
            metric_id="commute_filtered",
            label="通勤超限",
            value=commute_filtered,
            description=(
                f"单程超过 {max_commute_minutes} 分钟、已从今日待投列表移出的岗位。"
                if max_commute_minutes is not None
                else "尚未设置单程通勤上限，因此暂未按通勤排除岗位。"
            ),
        ),
        DashboardMetric(
            metric_id="applied",
            label="已投递",
            value=application_summary.applied_or_later,
            description="投递时间线中曾到达已投递或更后阶段的岗位数。",
        ),
        DashboardMetric(
            metric_id="meaningful_response_rate",
            label="有效回复率",
            value=meaningful_response_rate,
            unit="%",
            description="索要简历、进入筛选/测评/面试、Offer 或明确拒绝 ÷ 已投递；单纯已读不算。",
        ),
        DashboardMetric(
            metric_id="new_feedback",
            label="今日新增反馈",
            value=today_new_feedback,
            description="今天新增的索要简历、推进、Offer 或明确拒绝事件；单纯已读不算。",
            tone="accent",
        ),
        DashboardMetric(
            metric_id="outsourcing_filtered",
            label="外包拦截",
            value=outsourcing_filtered,
            description="因 JD 出现外包、派遣或派驻信号而移出待投列表的岗位。",
        ),
        DashboardMetric(
            metric_id="pay_to_confirm",
            label="薪资待确认",
            value=pay_review_count,
            description=(
                f"日薪低于 {daily_pay_floor} 元或未披露、需要人工确认的实习岗位。"
                if daily_pay_floor is not None
                else "尚未设置日薪底线，实习薪资只做信息提示。"
            ),
        ),
        DashboardMetric(
            metric_id="priority_preparation",
            label="优先准备",
            value=len(priority_preparation),
            description="高匹配已投递岗位或出现积极反馈、需要优先准备的岗位。",
            tone="warning",
        ),
    ]
    funnel = [
        DashboardFunnelStage(
            stage_id="discovered",
            label="发现候选",
            value=stats.candidates,
            definition="已去重的搜索候选链接。",
        ),
        DashboardFunnelStage(
            stage_id="jobs",
            label="正式岗位",
            value=stats.jobs,
            definition="已取得完整 JD 的正式岗位。",
        ),
        DashboardFunnelStage(
            stage_id="recommended",
            label="推荐岗位",
            value=stats.recommended,
            definition="匹配分不低于 75。",
        ),
        DashboardFunnelStage(
            stage_id="applied",
            label="已投递",
            value=application_summary.applied_or_later,
            definition="曾到达已投递或更后阶段。",
        ),
        DashboardFunnelStage(
            stage_id="hr_read",
            label="HR 已读",
            value=application_summary.hr_read_or_later,
            definition="曾到达 HR 已读或更后阶段。",
        ),
        DashboardFunnelStage(
            stage_id="meaningful_response",
            label="有效反馈",
            value=application_summary.meaningful_responses,
            definition="索要简历、推进、Offer 或明确拒绝；单纯已读不计入。",
        ),
        DashboardFunnelStage(
            stage_id="screening",
            label="筛选",
            value=application_summary.screening_or_later,
            definition="曾到达筛选或更后阶段。",
        ),
        DashboardFunnelStage(
            stage_id="interview",
            label="面试",
            value=application_summary.interview_or_later,
            definition="曾进入任一面试阶段。",
        ),
        DashboardFunnelStage(
            stage_id="offer",
            label="Offer",
            value=application_summary.offers,
            definition="投递时间线中曾记录 Offer。",
        ),
    ]
    candidate_breakdown = [
        DashboardBreakdownRow(status_id="live", label="确认在招", value=stats.live_candidates, tone="positive"),
        DashboardBreakdownRow(status_id="manual", label="待人工复核", value=stats.manual_review_candidates, tone="warning"),
        DashboardBreakdownRow(status_id="blocked", label="访问受阻", value=stats.blocked_candidates, tone="warning"),
        DashboardBreakdownRow(status_id="expired", label="已失效", value=stats.expired_candidates),
        DashboardBreakdownRow(status_id="irrelevant", label="方向无关", value=stats.irrelevant_candidates),
        DashboardBreakdownRow(status_id="pending", label="待核验", value=stats.pending_candidates),
    ]
    caveats = [
        "招聘网站状态无法识别或更新不及时，可在本页手动补录；手动记录会进入同一条投递时间线。",
        "HR 已读只记录、不当作有效回复；每个方向累计一个实验批次后，再根据真实反馈调整策略。",
        "上传的简历、联系方式与求职偏好只保存在本机 data/private，不上传到 Dashboard 之外。",
        "通勤筛选使用本人确认或地图服务计算的单程分钟数；办公地点不详时标记为待估算，不会直接排除。通勤不会改写能力匹配分。",
        "岗位策略分与能力匹配分相互独立；学校背景只在 JD 明确设门槛时判断，不做隐含扣分。",
    ]
    return DashboardSnapshot(
        source_path=str(repository.path),
        source_freshness_at=_source_freshness(repository),
        metrics=metrics,
        funnel=funnel,
        jobs_to_apply=jobs_to_apply,
        tracked_jobs=tracked_jobs,
        priority_preparation=priority_preparation[:12],
        recent_feedback=recent_feedback,
        candidate_breakdown=candidate_breakdown,
        active_profile=profile_summary,
        strategy=strategy_summary,
        action_token=action_token,
        caveats=caveats,
    )


def _dashboard_template_path() -> Path:
    return Path(__file__).resolve().parents[1] / "web" / "dashboard.html"


def _dashboard_stylesheet_path() -> Path:
    return Path(__file__).resolve().parents[1] / "web" / "dashboard.css"


def _dashboard_font_paths() -> dict[str, Path]:
    font_dir = Path(__file__).resolve().parents[1] / "web" / "fonts"
    names = (
        "SmileySans-Oblique.woff2",
        "NotoSerifSC-Variable.subset.woff2",
        "NotoSansSC-Variable.subset.woff2",
    )
    return {name: font_dir / name for name in names}


def create_dashboard_server(
    repository: JobRepository,
    *,
    output_dir: Path,
    port: int,
    profile_path: Path | None = None,
    private_dir: Path | None = None,
) -> ThreadingHTTPServer:
    template_path = _dashboard_template_path()
    if not template_path.is_file():
        raise DashboardError(f"Dashboard 页面模板不存在: {template_path}")
    template = template_path.read_bytes()
    stylesheet_path = _dashboard_stylesheet_path()
    if not stylesheet_path.is_file():
        raise DashboardError(f"Dashboard 样式表不存在: {stylesheet_path}")
    stylesheet = stylesheet_path.read_bytes()
    font_assets: dict[str, bytes] = {}
    for font_name, font_path in _dashboard_font_paths().items():
        if not font_path.is_file():
            raise DashboardError(f"Dashboard 字体不存在: {font_path}")
        font_assets[f"/assets/fonts/{font_name}"] = font_path.read_bytes()
    action_token = secrets.token_urlsafe(32)
    profile_path = profile_path or repository.path.parent / "profile.json"
    private_dir = private_dir or profile_path.parent

    agent_token_path = private_dir / "agent-token.json"

    def _load_or_create_agent_token() -> str:
        try:
            payload = json.loads(agent_token_path.read_text(encoding="utf-8"))
            token = str(payload.get("token") or "")
            if token:
                return token
        except (OSError, json.JSONDecodeError):
            pass
        token = secrets.token_urlsafe(32)
        agent_token_path.parent.mkdir(parents=True, exist_ok=True)
        agent_token_path.write_text(
            json.dumps(
                {
                    "token": token,
                    "created_at": datetime.now(UTC).isoformat(),
                    "note": "外部 Agent（WorkBuddy/Codex 等）调用 /api/agent/* 时须携带 X-Agent-Token 头。",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return token

    agent_token = _load_or_create_agent_token()
    runtime_config_path = private_dir / "app-settings.json"
    api_usage_path = private_dir / "api-usage.json"
    copilot_thread_path = private_dir / "copilot" / "current-thread.json"
    copilot_lock = threading.Lock()
    assist_runs: dict[str, dict[str, object]] = {}
    assist_lock = threading.Lock()

    def public_assist_plan(session: object) -> dict[str, object]:
        fields = list(getattr(session, "fields", []))
        return {
            "session_id": getattr(session, "session_id"),
            "job_id": getattr(session, "job_id"),
            "company": getattr(session, "company"),
            "title": getattr(session, "title"),
            "platform": getattr(session, "platform"),
            "status": getattr(session, "status"),
            "ready_fields": [
                item.label
                for item in fields
                if item.category == "safe_objective" and item.status == "ready"
            ],
            "manual_fields": [
                item.label for item in fields if item.category == "manual"
            ],
            "blocked_fields": [
                item.label for item in fields if item.category == "blocked"
            ],
            "blockers": list(getattr(session, "blockers", [])),
            "hard_stops": [
                item.detail for item in list(getattr(session, "hard_stops", []))
            ],
            "final_submit": "blocked",
        }

    def launch_assist_worker(session_path: Path, session_id: str) -> None:
        with assist_lock:
            assist_runs[session_id] = {"session_id": session_id, "status": "launching"}
        try:
            report, _ = run_browser_session(
                session_path,
                browser_profile_dir=private_dir / "browser-profiles",
                headless=False,
                keep_open=True,
                browser_channel="msedge",
                max_actions=12,
                dashboard_wait_seconds=900,
            )
        except Exception as exc:
            public = {
                "session_id": session_id,
                "status": "failed",
                "error": type(exc).__name__,
                "submit_attempted": False,
            }
        else:
            public = {
                "session_id": session_id,
                "status": report.status,
                "filled_fields": report.filled_fields,
                "uploaded_resume": report.uploaded_resume,
                "manual_fields_detected": report.manual_fields_detected,
                "submit_controls_detected": report.submit_controls_detected,
                "submit_attempted": report.submit_attempted,
                "submission_outcome": report.submission_outcome,
                "blockers": report.blockers,
            }
        with assist_lock:
            assist_runs[session_id] = public

    class Handler(BaseHTTPRequestHandler):
        server_version = "JobAgentDashboard/2.1"

        def _headers(
            self,
            status: HTTPStatus,
            content_type: str,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()

        def _json(
            self,
            payload: object,
            status: HTTPStatus = HTTPStatus.OK,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._headers(
                status,
                "application/json; charset=utf-8",
                extra_headers=extra_headers,
            )
            self.wfile.write(body)

        def _read_body(self, maximum: int) -> bytes:
            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("请求缺少有效的 Content-Length。") from exc
            if length < 0 or length > maximum:
                raise ValueError("请求内容过大。")
            return self.rfile.read(length)

        def _authorized_action(self) -> bool:
            host = self.headers.get("Host", "")
            try:
                hostname = urlsplit("http://" + host).hostname
            except ValueError:
                hostname = None
            if hostname not in {"127.0.0.1", "localhost"}:
                return False
            origin = self.headers.get("Origin")
            if origin:
                try:
                    origin_parts = urlsplit(origin)
                    origin_host = origin_parts.hostname
                    origin_netloc = origin_parts.netloc
                except ValueError:
                    return False
                if origin_parts.scheme != "http" or origin_host not in {
                    "127.0.0.1",
                    "localhost",
                }:
                    return False
                if origin_netloc.casefold() != host.casefold():
                    return False
            supplied = self.headers.get("X-Job-Agent-Token", "")
            return bool(supplied) and secrets.compare_digest(supplied, action_token)

        def _reject_unauthorized_action(self) -> None:
            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length)
            except ValueError:
                length = 0
            content_type = self.headers.get("Content-Type", "").casefold()
            maximum = (
                _MAX_UPLOAD_BODY
                if content_type.startswith("multipart/form-data")
                else _MAX_JSON_BODY
            )
            if 0 < length <= maximum:
                self.rfile.read(length)
            self.close_connection = True
            self._json(
                {"error": "本地操作授权已过期，请刷新页面后重试。"},
                HTTPStatus.FORBIDDEN,
                extra_headers={"Connection": "close"},
            )

        def _authorized_agent(self) -> bool:
            supplied = self.headers.get("X-Agent-Token", "")
            return bool(supplied) and secrets.compare_digest(supplied, agent_token)

        def _reject_unauthorized_agent(self) -> None:
            self.close_connection = True
            self._json(
                {"error": "Agent Token 无效；请读取 data/private/agent-token.json。"},
                HTTPStatus.FORBIDDEN,
                extra_headers={"Connection": "close"},
            )

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            path = urlsplit(self.path).path
            if path == "/":
                self._headers(HTTPStatus.OK, "text/html; charset=utf-8")
                self.wfile.write(template)
                return
            if path == "/favicon.ico":
                self._headers(HTTPStatus.NO_CONTENT, "image/x-icon")
                return
            if path == "/assets/dashboard.css":
                self._headers(HTTPStatus.OK, "text/css; charset=utf-8")
                self.wfile.write(stylesheet)
                return
            font_asset = font_assets.get(path)
            if font_asset is not None:
                self._headers(HTTPStatus.OK, "font/woff2")
                self.wfile.write(font_asset)
                return
            if path == "/api/health":
                self._json(
                    {
                        "status": "ok",
                        "service": "personal-job-agent",
                        "version": _DASHBOARD_VERSION,
                    }
                )
                return
            if path == "/api/settings":
                try:
                    payload = public_runtime_config(
                        runtime_config_path,
                        usage_path=api_usage_path,
                    )
                except RuntimeConfigError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._json(payload)
                return
            if path == "/api/copilot":
                try:
                    with copilot_lock:
                        snapshot = copilot_snapshot(
                            copilot_thread_path,
                            runtime_config_path=runtime_config_path,
                            repository=repository,
                            profile_path=profile_path,
                        )
                except CopilotChatError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(snapshot.model_dump(mode="json"))
                return
            assist_match = re.fullmatch(r"/api/assist/([0-9A-Za-z_-]+)", path)
            if assist_match:
                session_id = assist_match.group(1)
                with assist_lock:
                    payload = assist_runs.get(session_id)
                if payload is None:
                    self._json({"error": "没有找到该代填会话。"}, HTTPStatus.NOT_FOUND)
                    return
                self._json(payload)
                return
            if path == "/api/dashboard":
                try:
                    snapshot = build_dashboard_snapshot(
                        repository,
                        output_dir=output_dir,
                        profile_path=profile_path,
                        action_token=action_token,
                    )
                except Exception as exc:
                    self._json(
                        {"error": f"Dashboard 数据读取失败：{type(exc).__name__}"},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._json(snapshot.model_dump(mode="json"))
                return
            agent_state_match = re.fullmatch(r"/api/agent/state", path)
            agent_jobs_match = re.fullmatch(r"/api/agent/jobs", path)
            agent_job_detail_match = re.fullmatch(r"/api/agent/jobs/(\d+)", path)
            agent_applications_match = re.fullmatch(r"/api/agent/applications", path)
            agent_candidates_match = re.fullmatch(r"/api/agent/candidates", path)
            if agent_state_match or agent_jobs_match or agent_job_detail_match or agent_applications_match or agent_candidates_match:
                if not self._authorized_agent():
                    self._reject_unauthorized_agent()
                    return
                try:
                    if agent_state_match:
                        payload: dict[str, object] = {
                            "stats": repository.stats().model_dump(mode="json"),
                            "funnel": repository.application_summary().model_dump(mode="json"),
                            "service": "personal-job-agent",
                            "version": _DASHBOARD_VERSION,
                        }
                    elif agent_jobs_match:
                        jobs = repository.list_jobs(limit=100)
                        payload = {
                            "jobs": [item.model_dump(mode="json") for item in jobs],
                            "count": len(jobs),
                        }
                    elif agent_job_detail_match:
                        job_id = int(agent_job_detail_match.group(1))
                        job = repository.get_job(job_id)
                        insights = repository.get_latest_match_insights([job_id])
                        application = repository.get_application(job_id)
                        payload = {
                            "job": job.model_dump(mode="json"),
                            "match_insight": insights.get(job_id),
                            "application": (
                                application.model_dump(mode="json")
                                if application is not None
                                else None
                            ),
                        }
                    elif agent_applications_match:
                        applications = repository.list_applications(limit=100)
                        payload = {
                            "applications": [
                                item.model_dump(mode="json") for item in applications
                            ],
                            "count": len(applications),
                        }
                    else:
                        candidates = repository.list_search_candidates(limit=100)
                        payload = {
                            "candidates": [
                                item.model_dump(mode="json") for item in candidates
                            ],
                            "count": len(candidates),
                        }
                except JobDatabaseError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(payload)
                return
            detail_match = re.fullmatch(r"/api/jobs/(\d+)/detail", path)
            if detail_match:
                try:
                    job_id = int(detail_match.group(1))
                    job = repository.get_job(job_id)
                    events = repository.list_application_events(job_id)
                    insight = repository.get_latest_match_insights([job_id]).get(job_id)
                    gaps = (
                        [str(item) for item in insight.get("gaps", [])]
                        if isinstance(insight, dict)
                        else []
                    )
                    summary = _profile_summary(profile_path)
                    strategy = evaluate_job_strategy(
                        job,
                        match_score=job.match_score,
                        commute_fit=_commute_fit(
                            job.commute_minutes,
                            summary.max_one_way_minutes if summary else None,
                        ),
                        daily_pay_floor=(
                            summary.internship_daily_pay_floor if summary else None
                        ),
                        exclude_outsourcing=(
                            summary.exclude_outsourcing if summary else True
                        ),
                        primary_roles=(summary.target_roles if summary else []),
                        adjacent_roles=(summary.adjacent_roles if summary else []),
                        today=datetime.now(_LOCAL_TIMEZONE).date(),
                    )
                    self._json({
                        "job": {
                            "job_id": job.job_id,
                            "company": job.company,
                            "title": job.title,
                            "jd_text": job.jd_text,
                        },
                        "match_insight": insight,
                        "strategy": strategy.model_dump(mode="json"),
                        "project_workshop": project_workshop_snapshot(
                            output_dir,
                            job,
                            gaps=gaps,
                        ),
                        "preparation_url": f"/preparation/{job_id}" if _latest_preparation_path(output_dir, job_id) else None,
                        "events": [
                            {
                                "status": event.status,
                                "detail": event.detail,
                                "occurred_at": event.occurred_at,
                            }
                            for event in events
                        ],
                    })
                except JobDatabaseError:
                    self._json({"error": "职位不存在或暂时无法读取。"}, HTTPStatus.NOT_FOUND)
                except ProjectWorkshopError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
                return
            resume_content_match = re.fullmatch(r"/api/jobs/(\d+)/resume-content", path)
            if resume_content_match:
                job_id = int(resume_content_match.group(1))
                try:
                    content, manifest_path = load_latest_resume_content(
                        output_dir / "applications",
                        job_id,
                    )
                except ResumeEditorError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                    return
                self._json(
                    {
                        "job_id": job_id,
                        "content": content,
                        "manifest_path": str(manifest_path),
                        "template": "portable-evidence-resume-v1",
                        "photo_available": find_profile_photo(private_dir) is not None,
                    }
                )
                return
            resume_match = re.fullmatch(r"/resume-draft/(\d+)/(pdf|docx)", path)
            if resume_match:
                job_id = int(resume_match.group(1))
                kind = resume_match.group(2)
                manifest = find_latest_resume_manifest(
                    output_dir / "applications",
                    job_id,
                )
                if manifest is None:
                    self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
                    self.wfile.write("尚未生成该岗位的简历草稿。".encode("utf-8"))
                    return
                try:
                    artifact = resume_artifact_from_manifest(manifest, kind)
                except PortableResumeError as exc:
                    self._headers(HTTPStatus.CONFLICT, "text/plain; charset=utf-8")
                    self.wfile.write(str(exc).encode("utf-8"))
                    return
                content_type = (
                    "application/pdf"
                    if kind == "pdf"
                    else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
                disposition = "inline" if kind == "pdf" else "attachment"
                self._headers(
                    HTTPStatus.OK,
                    content_type,
                    {"Content-Disposition": f'{disposition}; filename="job-{job_id}-resume.{kind}"'},
                )
                self.wfile.write(artifact.read_bytes())
                return
            project_preview_match = re.fullmatch(r"/project-workshop/(\d+)/preview", path)
            if project_preview_match:
                try:
                    preview = project_preview_path(
                        output_dir,
                        int(project_preview_match.group(1)),
                    )
                except ProjectWorkshopError as exc:
                    self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
                    self.wfile.write(str(exc).encode("utf-8"))
                    return
                self._headers(HTTPStatus.OK, "text/html; charset=utf-8")
                self.wfile.write(preview.read_bytes())
                return
            match = re.fullmatch(r"/preparation/(\d+)", path)
            if match:
                pack_path = _latest_preparation_path(output_dir, int(match.group(1)))
                if pack_path is None:
                    self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
                    self.wfile.write("尚未生成岗位学习包。".encode("utf-8"))
                    return
                self._headers(HTTPStatus.OK, "text/plain; charset=utf-8")
                self.wfile.write(pack_path.read_bytes())
                return
            self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
            self.wfile.write("Not found".encode("utf-8"))

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            path = urlsplit(self.path).path
            photo_upload_match = re.fullmatch(r"/api/profile/photo", path)
            if photo_upload_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    content_type = self.headers.get("Content-Type", "").casefold()
                    if not content_type.startswith("image/"):
                        raise ValueError("照片上传必须使用 image/* 请求体。")
                    data = self._read_body(_MAX_UPLOAD_BODY)
                    photo_path = save_profile_photo(private_dir, data)
                except (ValueError, PortableResumeError) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                except OSError as exc:
                    self._json({"error": f"照片保存失败: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._json(
                    {
                        "ok": True,
                        "photo_path": str(photo_path),
                        "note": "照片已保存；下次生成或编辑简历时会嵌入新版面。",
                    }
                )
                return
            project_run_match = re.fullmatch(
                r"/api/jobs/(\d+)/project-workshop/run",
                path,
            )
            if project_run_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("项目工坊操作必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict) or payload.get("confirmed") is not True:
                        raise ValueError("请先确认生成或重跑这个本地项目。")
                    job_id = int(project_run_match.group(1))
                    job = repository.get_job(job_id)
                    insight = repository.get_latest_match_insights([job_id]).get(job_id)
                    gaps = (
                        [str(item) for item in insight.get("gaps", [])]
                        if isinstance(insight, dict)
                        else []
                    )
                    project = run_project(
                        output_dir,
                        job,
                        config_update=payload.get("config"),
                        gaps=gaps,
                    )
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    JobDatabaseError,
                    ProjectWorkshopError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json({"ok": True, "project_workshop": project})
                return
            project_verify_match = re.fullmatch(
                r"/api/jobs/(\d+)/project-workshop/verify",
                path,
            )
            if project_verify_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("项目验证必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict) or payload.get("confirmed") is not True:
                        raise ValueError("请先确认由本人完成项目验证。")
                    job_id = int(project_verify_match.group(1))
                    job = repository.get_job(job_id)
                    insight = repository.get_latest_match_insights([job_id]).get(job_id)
                    gaps = (
                        [str(item) for item in insight.get("gaps", [])]
                        if isinstance(insight, dict)
                        else []
                    )
                    project = verify_project(
                        output_dir,
                        job,
                        explanation=str(payload.get("explanation") or ""),
                        demo_confirmed=payload.get("demo_confirmed") is True,
                        understanding_confirmed=(
                            payload.get("understanding_confirmed") is True
                        ),
                        gaps=gaps,
                    )
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    JobDatabaseError,
                    ProjectWorkshopError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json({"ok": True, "project_workshop": project})
                return
            resume_polish_match = re.fullmatch(r"/api/jobs/(\d+)/resume-content/polish", path)
            if resume_polish_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                job_id = int(resume_polish_match.group(1))
                try:
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict) or payload.get("confirmed") is not True:
                        raise ValueError("请先确认本次润色。")
                    job = repository.get_job(job_id)
                    content, _ = load_latest_resume_content(
                        output_dir / "applications",
                        job_id,
                    )
                    if "content" in payload:
                        content = validate_resume_content(payload["content"])
                        if int(content["target"]["job_id"]) != job_id:
                            raise ValueError("简历与当前岗位不一致。")
                    config, _ = effective_runtime_config(runtime_config_path)
                    warning = ""
                    if config.ai.provider == "local":
                        suggestion = polish_resume_content_locally(content, job.jd_text)
                        polish_engine = "local"
                    else:
                        try:
                            suggestion = polish_resume_content_with_jd(
                                content,
                                job.jd_text,
                                config=config,
                            )
                        except CloudAIUnavailableError as exc:
                            suggestion = polish_resume_content_locally(content, job.jd_text)
                            polish_engine = "local_fallback"
                            warning = str(exc)
                        else:
                            polish_engine = "cloud"
                            record_api_usage(
                                api_usage_path,
                                "ai",
                                config.ai.provider,
                                successful_requests=1,
                                input_tokens=suggestion.input_tokens,
                                output_tokens=suggestion.output_tokens,
                            )
                except (
                    ValueError,
                    ResumeEditorError,
                    ResumePolishError,
                    RuntimeConfigError,
                    JobDatabaseError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "content": suggestion.content,
                        "changes": suggestion.changes,
                        "change_count": len(suggestion.changes),
                        "generation": suggestion.content.get("generation", {}),
                        "engine": polish_engine,
                        "warning": warning,
                        "note": "润色仅为建议；请在编辑器中检查后保存，保存后仍需通过 PDF 版面人工审阅。",
                    }
                )
                return
            resume_import_match = re.fullmatch(
                r"/api/jobs/(\d+)/resume-content/import", path
            )
            if resume_import_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                job_id = int(resume_import_match.group(1))
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("原简历导入必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("resume_text"), str
                    ):
                        raise ValueError("请求需包含 resume_text 文本字段。")
                    job = repository.get_job(job_id)
                    content, _ = load_latest_resume_content(
                        output_dir / "applications",
                        job_id,
                    )
                    config, _ = effective_runtime_config(runtime_config_path)
                    if (
                        config.ai.monthly_quota is not None
                        and load_api_usage(api_usage_path).ai.successful_requests
                        >= config.ai.monthly_quota
                    ):
                        raise ValueError("本月 AI 请求额度已用完，无法执行原简历导入。")
                    suggestion = import_resume_text_into_draft(
                        payload["resume_text"],
                        content,
                        config=config,
                    )
                    record_api_usage(
                        api_usage_path,
                        "ai",
                        config.ai.provider,
                        successful_requests=1,
                        input_tokens=suggestion.input_tokens,
                        output_tokens=suggestion.output_tokens,
                    )
                except (
                    ValueError,
                    ResumeEditorError,
                    ResumeImportError,
                    RuntimeConfigError,
                    JobDatabaseError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "content": suggestion.content,
                        "warnings": suggestion.warnings,
                        "note": "导入仅为建议：AI 已把原简历解析为表单内容；请逐项检查（姓名、联系方式、量化数字）后再保存，保存后仍需通过 PDF 版面人工审阅。",
                    }
                )
                return
            resume_revise_match = re.fullmatch(
                r"/api/jobs/(\d+)/resume-content/revise", path
            )
            if resume_revise_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                job_id = int(resume_revise_match.group(1))
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("Agent 修改简历必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    instruction = (
                        str(payload.get("instruction", "")).strip()
                        if isinstance(payload, dict)
                        else ""
                    )
                    if not isinstance(payload, dict) or payload.get("confirmed") is not True:
                        raise ValueError("请先确认让 Agent 按这条要求直接修改简历。")
                    if not instruction:
                        raise ValueError("请告诉 Agent 需要怎样修改简历。")
                    if len(instruction) > 2000:
                        raise ValueError("单次修改要求不能超过 2000 个字符。")

                    photo_path = find_profile_photo(private_dir)
                    if photo_path is None:
                        raise ValueError(
                            "请先在“个人资料与简历”中上传证件照；当前默认模板要求右上角证件照。"
                        )
                    job = repository.get_job(job_id)
                    config, _ = effective_runtime_config(runtime_config_path)
                    try:
                        content, manifest_path = load_latest_resume_content(
                            output_dir / "applications",
                            job_id,
                        )
                    except ResumeEditorError:
                        profile = load_profile(profile_path)
                        result = _ensure_match(repository, profile, job)
                        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                        draft_dir = (
                            output_dir
                            / "applications"
                            / (
                                f"portable-job-{job_id}-"
                                f"{_safe_slug(job.company, fallback='company')}-"
                                f"{_safe_slug(job.title, fallback='role')}-{stamp}"
                            )
                        )
                        build_portable_resume_draft(
                            profile,
                            job,
                            result,
                            draft_dir,
                            photo_path=photo_path,
                        )
                        content, manifest_path = load_latest_resume_content(
                            output_dir / "applications",
                            job_id,
                        )
                    person = dict(content.get("person") or {})
                    person["photo_path"] = str(photo_path.resolve())
                    content["person"] = person
                    quota_available = not (
                        config.ai.monthly_quota is not None
                        and load_api_usage(api_usage_path).ai.successful_requests
                        >= config.ai.monthly_quota
                    )
                    try:
                        if not quota_available:
                            raise ResumePolishError("AI 请求额度已用完。")
                        suggestion = compose_resume_content_with_jd(
                            content,
                            job.jd_text,
                            profile=load_profile(profile_path),
                            config=config,
                            user_instruction=instruction,
                        )
                    except ResumePolishError as exc:
                        suggestion = polish_resume_content_locally(
                            content,
                            job.jd_text,
                            user_instruction=instruction,
                        )
                        suggestion.content["generation"] = {"engine": "local_fallback", "warning": str(exc), "review_required": True}
                    else:
                        record_api_usage(
                            api_usage_path,
                            "ai",
                            config.ai.provider,
                            successful_requests=1,
                            input_tokens=suggestion.input_tokens,
                            output_tokens=suggestion.output_tokens,
                        )
                    edited = rerender_edited_resume(
                        suggestion.content,
                        output_dir / "applications",
                        based_on=str(manifest_path),
                        edit_origin="agent_dialog",
                    )
                    profile = load_profile(profile_path)
                    workspace = _job_workspace(profile, job, output_dir=output_dir)
                    edited_manifest = json.loads(edited.manifest.read_text(encoding="utf-8"))
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    ResumeEditorError,
                    ResumePolishError,
                    RuntimeConfigError,
                    PortableResumeError,
                    JobDatabaseError,
                    ProfileStoreError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "version_id": edited_manifest.get("version_id"),
                        "change_count": len(suggestion.changes),
                        "workspace": workspace.model_dump(mode="json"),
                        "resume_url": f"/resume-draft/{job_id}/pdf",
                        "note": "Agent 已直接生成新版简历；请打开 PDF 核对事实与版面，确认前不会进入可投递状态。",
                    }
                )
                return
            resume_edit_match = re.fullmatch(r"/api/jobs/(\d+)/resume-content", path)
            if resume_edit_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                job_id = int(resume_edit_match.group(1))
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("简历编辑必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if (
                        not isinstance(payload, dict)
                        or payload.get("confirmed") is not True
                        or not isinstance(payload.get("content"), dict)
                    ):
                        raise ValueError(
                            "请先在编辑器中确认修改；请求需包含 confirmed 与 content。"
                        )
                    job = repository.get_job(job_id)
                    profile = load_profile(profile_path)
                    _, manifest_path = load_latest_resume_content(
                        output_dir / "applications",
                        job_id,
                    )
                    edited = rerender_edited_resume(
                        payload["content"],
                        output_dir / "applications",
                        based_on=str(manifest_path),
                    )
                    workspace = _job_workspace(profile, job, output_dir=output_dir)
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    ResumeEditorError,
                    PortableResumeError,
                    JobDatabaseError,
                    ProfileStoreError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "version_id": json.loads(edited.manifest.read_text(encoding="utf-8")).get("version_id"),
                        "workspace": workspace.model_dump(mode="json"),
                    }
                )
                return
            if path == "/api/copilot/message":
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("Agent 对话必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("Agent 对话内容格式不正确。")
                    with copilot_lock:
                        snapshot = respond_to_copilot(
                            str(payload.get("message", "")),
                            selected_job_id=payload.get("job_id"),
                            thread_path=copilot_thread_path,
                            repository=repository,
                            profile_path=profile_path,
                            runtime_config_path=runtime_config_path,
                            usage_path=api_usage_path,
                            applications_dir=output_dir / "applications",
                        )
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    CopilotChatError,
                    JobDatabaseError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json({"ok": True, "copilot": snapshot.model_dump(mode="json")})
                return

            if path == "/api/copilot/reset":
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    with copilot_lock:
                        reset_copilot_thread(copilot_thread_path)
                        snapshot = copilot_snapshot(
                            copilot_thread_path,
                            runtime_config_path=runtime_config_path,
                            repository=repository,
                            profile_path=profile_path,
                        )
                except CopilotChatError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json({"ok": True, "copilot": snapshot.model_dump(mode="json")})
                return

            draft_match = re.fullmatch(r"/api/jobs/(\d+)/resume-draft", path)
            if draft_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("简历草稿操作必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict) or payload.get("confirmed") is not True:
                        raise ValueError("请先确认生成该岗位的专属简历草稿。")
                    profile = load_profile(profile_path)
                    job_id = int(draft_match.group(1))
                    job = repository.get_job(job_id)
                    result = _ensure_match(repository, profile, job)
                    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                    draft_dir = (
                        output_dir
                        / "applications"
                        / (
                            f"portable-job-{job_id}-"
                            f"{_safe_slug(job.company, fallback='company')}-"
                            f"{_safe_slug(job.title, fallback='role')}-{stamp}"
                        )
                    )
                    photo_path = find_profile_photo(private_dir)
                    if photo_path is None:
                        raise ValueError(
                            "请先在“个人资料与简历”中上传证件照；当前默认模板要求右上角证件照。"
                        )
                    content = build_portable_resume_content(
                        profile,
                        job,
                        result,
                        generated_at=datetime.now(UTC),
                        photo_path=photo_path,
                    )
                    config, _ = effective_runtime_config(runtime_config_path)
                    quota_available = not (
                        config.ai.monthly_quota is not None
                        and load_api_usage(api_usage_path).ai.successful_requests
                        >= config.ai.monthly_quota
                    )
                    try:
                        if not quota_available:
                            raise ResumePolishError("AI 请求额度已用完。")
                        suggestion = compose_resume_content_with_jd(
                            content,
                            job.jd_text,
                            profile=profile,
                            config=config,
                            user_instruction="按 JD 直接生成岗位专属简历，使用 STAR 法则突出相关真实经历。",
                        )
                    except ResumePolishError as exc:
                        suggestion = polish_resume_content_locally(
                            content,
                            job.jd_text,
                            user_instruction="按 JD 生成岗位专属简历并按 STAR 组织真实经历。",
                        )
                        suggestion.content["generation"] = {"engine": "local_fallback", "warning": str(exc), "review_required": True}
                    else:
                        record_api_usage(
                            api_usage_path,
                            "ai",
                            config.ai.provider,
                            successful_requests=1,
                            input_tokens=suggestion.input_tokens,
                            output_tokens=suggestion.output_tokens,
                        )
                    build_portable_resume_draft(
                        profile,
                        job,
                        result,
                        draft_dir,
                        photo_path=photo_path,
                        content=suggestion.content,
                    )
                    workspace = _job_workspace(profile, job, output_dir=output_dir)
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    ProfileStoreError,
                    JobDatabaseError,
                    PortableResumeError,
                    ApplicationPackError,
                    ResumePolishError,
                    RuntimeConfigError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "workspace": workspace.model_dump(mode="json"),
                        "generation": suggestion.content.get("generation", {}),
                    }
                )
                return

            approve_match = re.fullmatch(r"/api/jobs/(\d+)/resume-draft/approve", path)
            if approve_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("简历批准操作必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if (
                        not isinstance(payload, dict)
                        or payload.get("confirmed_pdf_review") is not True
                    ):
                        raise ValueError("请先打开 PDF，并确认本人已检查版面与内容。")
                    profile = load_profile(profile_path)
                    job_id = int(approve_match.group(1))
                    job = repository.get_job(job_id)
                    result = _ensure_match(repository, profile, job)
                    resume = resolve_resume_bundle(
                        profile,
                        job,
                        output_dir / "applications",
                    )
                    if resume.status != "ready" or not resume.qa_verified:
                        pending_manifest = find_latest_resume_manifest(
                            output_dir / "applications",
                            job_id,
                            visual_status="pending_user_review",
                        )
                        if pending_manifest is None:
                            raise ValueError("没有找到等待本人审阅的岗位专属简历草稿。")
                        approve_resume_visual_review(pending_manifest)
                        resume = resolve_resume_bundle(
                            profile,
                            job,
                            output_dir / "applications",
                        )
                    if resume.status != "ready" or not resume.qa_verified:
                        raise ValueError("简历批准记录未通过完整质检，未生成投递材料包。")
                    pack = build_application_pack(profile, job, result, resume)
                    pack_files = write_application_pack(
                        pack,
                        job,
                        result,
                        _application_pack_output_dir(output_dir, job),
                    )
                    current = repository.get_application(job_id)
                    if current is None or current.status in {"saved", "ready_to_apply"}:
                        repository.record_application_status(
                            job_id,
                            "ready_to_apply",
                            source="dashboard_resume_approval",
                            detail="用户已审阅岗位专属 PDF；投递材料包已就绪，最终提交仍由本人完成。",
                            resume_path=resume.pdf_path,
                            application_pack_path=str(pack_files.pack_json),
                            source_url=_job_source_url(job),
                            verification_method="user_confirmed_pdf_review",
                        )
                    workspace = _job_workspace(profile, job, output_dir=output_dir)
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    ProfileStoreError,
                    JobDatabaseError,
                    ApplicationPackError,
                    TailoredResumeError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "workspace": workspace.model_dump(mode="json"),
                    }
                )
                return

            assist_match = re.fullmatch(r"/api/jobs/(\d+)/assist", path)
            if assist_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("打开投递页操作必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict) or payload.get("confirmed") is not True:
                        raise ValueError("请先确认人工投递边界。")
                    launch = payload.get("launch", False)
                    if not isinstance(launch, bool):
                        raise ValueError("launch 必须是布尔值。")
                    profile = load_profile(profile_path)
                    job_id = int(assist_match.group(1))
                    job = repository.get_job(job_id)
                    pack_path, pack = find_application_pack(
                        output_dir / "application-packs",
                        job_id,
                    )
                    if pack.resume.status != "ready" or not pack.resume.qa_verified:
                        raise ValueError("岗位专属简历尚未通过本人审阅，不能用于投递。")
                    session, session_path = create_application_session(
                        profile,
                        job,
                        pack,
                        pack_path,
                        sessions_dir=private_dir / "browser-sessions",
                        mode="open_only",
                    )
                    plan = public_assist_plan(session)
                    if not session.resume_path:
                        raise ValueError("岗位专属 PDF 尚未准备好。")
                    staged_resume = _stage_resume_for_manual_upload(
                        profile,
                        job,
                        Path(session.resume_path),
                        output_dir=output_dir,
                    )
                    plan.update(
                        {
                            "mode": "manual_apply",
                            "automatic_form_fill": False,
                            "resume_file_name": staged_resume.name,
                            "resume_path": str(staged_resume),
                        }
                    )
                    with assist_lock:
                        assist_runs[session.session_id] = {
                            "session_id": session.session_id,
                            "status": "planned",
                            "submit_attempted": False,
                        }
                    if launch:
                        plan["resume_folder_revealed"] = _reveal_resume_in_explorer(
                            staged_resume
                        )
                        threading.Thread(
                            target=launch_assist_worker,
                            args=(session_path, session.session_id),
                            daemon=True,
                            name=f"job-agent-assist-{job_id}",
                        ).start()
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    ProfileStoreError,
                    JobDatabaseError,
                    BrowserAssistError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json({"ok": True, "launched": launch, "plan": plan})
                return

            if path == "/api/settings":
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("API 设置必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("API 设置内容格式不正确。")
                    update = RuntimeConfigUpdate.model_validate(payload)
                    update_runtime_config(runtime_config_path, update)
                    public = public_runtime_config(
                        runtime_config_path,
                        usage_path=api_usage_path,
                    )
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    RuntimeConfigError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json({"ok": True, "settings": public})
                return

            if path == "/api/profile/preferences":
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("求职偏好必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("求职偏好内容格式不正确。")
                    update = ProfilePreferencesUpdate.model_validate(payload)
                    update_profile_preferences(profile_path, update)
                    summary = _profile_summary(profile_path)
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    ProfilePreferencesError,
                    ProfileStoreError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "profile": summary.model_dump(mode="json") if summary else None,
                    }
                )
                return

            status_match = re.fullmatch(r"/api/applications/(\d+)/status", path)
            if status_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("状态补录必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("状态补录内容格式不正确。")
                    if payload.get("confirmed") is not True:
                        raise ValueError("请先确认该状态由本人核实。")
                    status = str(payload.get("status", "")).strip()
                    detail = str(payload.get("detail", "")).strip()
                    if len(detail) > 500:
                        raise ValueError("状态备注最多 500 个字符。")
                    if not detail:
                        detail = (
                            "用户在本地 Dashboard 手动确认已完成投递。"
                            if status == "applied"
                            else "用户在本地 Dashboard 手动确认该招聘进度。"
                        )
                    job_id = int(status_match.group(1))
                    changed = repository.record_application_status(
                        job_id,
                        status,  # type: ignore[arg-type]
                        source="manual_dashboard",
                        detail=detail,
                        verification_method="user_confirmed_manual",
                    )
                    record = repository.get_application(job_id)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, JobDatabaseError) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "changed": changed,
                        "application": record.model_dump(mode="json") if record else None,
                    }
                )
                return

            route_match = re.fullmatch(r"/api/jobs/(\d+)/commute/route", path)
            if route_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("路线计算必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("路线计算内容格式不正确。")
                    if payload.get("confirmed") is not True:
                        raise ValueError("请先确认本次将地址发送给已配置的地图服务。")
                    destination = str(payload.get("destination", "")).strip()
                    mode = str(payload.get("mode", "transit")).strip().casefold()
                    if mode not in {"transit", "driving", "walking", "bicycling"}:
                        raise ValueError("通勤方式不受支持。")
                    profile = load_profile(profile_path)
                    origin = profile.job_search.commute.origin.strip()
                    if not origin:
                        raise ValueError("请先在“求职方向与通勤边界”中填写常用出发地址。")
                    config, _ = effective_runtime_config(runtime_config_path)
                    if config.maps.provider != "amap" or not config.maps.api_key:
                        raise ValueError("请先在“连接与 API”中配置高德地图 Web 服务 Key。")
                    job_id = int(route_match.group(1))
                    job = repository.get_job(job_id)
                    origin_city = (
                        profile.job_search.preferred_locations[0]
                        if profile.job_search.preferred_locations
                        else ""
                    )
                    provider = AmapCommuteProvider(config.maps.api_key)
                    result = provider.calculate(
                        origin_address=origin,
                        destination_address=destination,
                        mode=mode,  # type: ignore[arg-type]
                        origin_city=origin_city,
                        destination_city=job.location or origin_city,
                    )
                    record_api_usage(
                        api_usage_path,
                        "maps",
                        "amap",
                        successful_requests=provider.request_count,
                    )
                    record = repository.update_job_route(job_id, result)
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    ProfileStoreError,
                    RuntimeConfigError,
                    CommuteRoutingError,
                    JobDatabaseError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "route": result.model_dump(mode="json"),
                        "job": record.model_dump(mode="json"),
                    }
                )
                return

            commute_match = re.fullmatch(r"/api/jobs/(\d+)/commute", path)
            if commute_match:
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    if not self.headers.get("Content-Type", "").casefold().startswith(
                        "application/json"
                    ):
                        raise ValueError("通勤记录必须使用 JSON 请求。")
                    payload = json.loads(self._read_body(_MAX_JSON_BODY).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("通勤记录内容格式不正确。")
                    if payload.get("confirmed") is not True:
                        raise ValueError("请先确认通勤时间由本人核实或估算。")
                    raw_minutes = payload.get("minutes")
                    if raw_minutes is None or raw_minutes == "":
                        minutes = None
                    elif isinstance(raw_minutes, bool):
                        raise ValueError("单程通勤时间必须填写整数分钟。")
                    elif isinstance(raw_minutes, int):
                        minutes = raw_minutes
                    elif isinstance(raw_minutes, str) and re.fullmatch(
                        r"\d{1,3}", raw_minutes.strip()
                    ):
                        minutes = int(raw_minutes)
                    else:
                        raise ValueError("单程通勤时间必须填写整数分钟。")
                    method = str(payload.get("method", "user_estimate")).strip()
                    if method not in {
                        "user_estimate",
                        "route_estimate",
                        "remote",
                        "unknown",
                    }:
                        raise ValueError("通勤估算方式不受支持。")
                    note = str(payload.get("note", "")).strip()
                    destination = str(payload.get("destination", "")).strip() or None
                    job_id = int(commute_match.group(1))
                    record = repository.update_job_commute(
                        job_id,
                        minutes,
                        method=method,
                        note=note,
                        destination=destination,
                    )
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    JobDatabaseError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "job": record.model_dump(mode="json"),
                    }
                )
                return

            if path == "/api/profile/onboard":
                if not self._authorized_action():
                    self._reject_unauthorized_action()
                    return
                try:
                    content_type = self.headers.get("Content-Type", "")
                    if not content_type.casefold().startswith("multipart/form-data;"):
                        raise ProfileOnboardingError("资料导入必须使用文件上传表单。")
                    fields, filename, resume_bytes = _parse_multipart(
                        content_type,
                        self._read_body(_MAX_UPLOAD_BODY),
                    )
                    truthy = {"1", "true", "yes", "on"}
                    request = ProfileOnboardingInput(
                        resume_filename=filename,
                        resume_bytes=resume_bytes,
                        mode=fields.get("mode", "update").strip(),
                        display_name=fields.get("display_name", ""),
                        email=fields.get("email", ""),
                        phone=fields.get("phone", ""),
                        stage=fields.get("stage", ""),
                        target_roles=_split_values(fields.get("target_roles", "")),
                        adjacent_roles=_split_values(fields.get("adjacent_roles", "")),
                        target_industries=_split_values(fields.get("target_industries", "")),
                        preferred_locations=_split_values(fields.get("preferred_locations", "")),
                        employment_types=_split_values(fields.get("employment_types", "")),
                        must_haves=_split_values(fields.get("must_haves", "")),
                        avoid=_split_values(fields.get("avoid", "")),
                        earliest_start=fields.get("earliest_start", "").strip() or None,
                        days_per_week=_optional_int(
                            fields.get("days_per_week", ""),
                            label="每周到岗天数",
                            minimum=1,
                            maximum=7,
                        ),
                        duration_months=_optional_int(
                            fields.get("duration_months", ""),
                            label="连续实习月数",
                            minimum=1,
                            maximum=60,
                        ),
                        commute_origin=fields.get("commute_origin", ""),
                        max_commute_minutes=_optional_int(
                            fields.get("max_commute_minutes", ""),
                            label="单程通勤上限",
                            minimum=5,
                            maximum=240,
                        ),
                        transport_modes=_split_values(
                            fields.get("transport_modes", "")
                        ),
                        remote_acceptable=fields.get(
                            "remote_acceptable", ""
                        ).casefold()
                        in truthy,
                        school=fields.get("school", ""),
                        degree=fields.get("degree", ""),
                        major=fields.get("major", ""),
                        graduation=fields.get("graduation", "").strip() or None,
                        notes=fields.get("notes", ""),
                        confirm_truth=fields.get("confirm_truth", "").casefold() in truthy,
                        confirm_replace=fields.get("confirm_replace", "").casefold() in truthy,
                    )
                    result = onboard_profile(
                        request,
                        profile_path=profile_path,
                        private_dir=private_dir,
                    )
                    database_backup: Path | None = None
                    if result.replaced_profile:
                        database_backup = repository.backup_and_clear_for_new_profile(
                            private_dir / "backups"
                        )
                except (
                    ValueError,
                    ProfileOnboardingError,
                    ProfileStoreError,
                    ResumeReadError,
                    JobDatabaseError,
                    OSError,
                ) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(
                    {
                        "ok": True,
                        "source_id": result.source_id,
                        "resume_filename": (
                            result.resume_path.name if result.resume_path else None
                        ),
                        "extracted_characters": result.extracted_characters,
                        "imported_facts": result.imported_facts,
                        "imported_skills": result.imported_skills,
                        "warnings": result.warnings,
                        "replaced_profile": result.replaced_profile,
                        "database_backup": str(database_backup) if database_backup else None,
                    }
                )
                return

            self._json({"error": "此地址不支持写入。"}, HTTPStatus.METHOD_NOT_ALLOWED)

        def _method_not_allowed(self) -> None:
            self._json({"error": "不支持此请求方法。"}, HTTPStatus.METHOD_NOT_ALLOWED)

        do_PUT = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_DELETE = _method_not_allowed

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def run_dashboard(
    repository: JobRepository,
    *,
    output_dir: Path,
    profile_path: Path | None = None,
    private_dir: Path | None = None,
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    if port < 1 or port > 65535:
        raise DashboardError("Dashboard 端口必须在 1 到 65535 之间。")
    selected_port = port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            existing_url = f"http://127.0.0.1:{port}/"
            try:
                with urlopen(existing_url + "api/health", timeout=0.4) as response:
                    health = json.loads(response.read().decode("utf-8"))
            except (OSError, ValueError):
                health = {}
            if (
                health.get("service") == "personal-job-agent"
                and health.get("version") == _DASHBOARD_VERSION
            ):
                print(f"个人求职 Agent 已在运行: {existing_url}")
                if open_browser:
                    webbrowser.open(existing_url)
                return
            selected_port = 0
    try:
        server = create_dashboard_server(
            repository,
            output_dir=output_dir,
            port=selected_port,
            profile_path=profile_path,
            private_dir=private_dir,
        )
    except OSError as exc:
        if selected_port == 0:
            raise DashboardError(f"无法启动本地 Dashboard：{exc}") from exc
        try:
            server = create_dashboard_server(
                repository,
                output_dir=output_dir,
                port=0,
                profile_path=profile_path,
                private_dir=private_dir,
            )
        except OSError as fallback_exc:
            raise DashboardError(f"无法启动本地 Dashboard：{fallback_exc}") from fallback_exc
    url = f"http://127.0.0.1:{server.server_port}/"
    if server.server_port != port:
        print(f"默认端口 {port} 正在使用，已自动切换到可用端口。")
    print(f"个人求职 Agent 已启动: {url}")
    print("关闭此窗口或按 Ctrl+C 即可停止；只有本人点击确认后才会补录状态或更新资料。")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nDashboard 已停止。")
    finally:
        server.server_close()
