from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import webbrowser
from datetime import UTC, date, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

from job_agent.models.dashboard import (
    DashboardBreakdownRow, DashboardFeedbackRow, DashboardFunnelStage,
    DashboardJobRow, DashboardJobWorkspace, DashboardMetric,
    DashboardPreparationRow, DashboardProfileSummary, DashboardSnapshot,
    DashboardStrategySummary,
)
from job_agent.models.job_record import JobDetail
from job_agent.models.profile import Profile
from job_agent.services.application_pack import ApplicationPackError, resolve_resume_bundle
from job_agent.services.browser_assist import BrowserAssistError, find_application_pack
from job_agent.services.job_repository import JobRepository
from job_agent.services.match_refresh import refresh_all_matches
from job_agent.services.job_strategy import JobStrategyResult, evaluate_job_strategy
from job_agent.services.portable_resume import find_latest_resume_manifest
from job_agent.services.preparation_pack import preparation_priority_for_status
from job_agent.services.profile_store import ProfileStoreError, load_profile
from job_agent.services.runtime_config import (
    RuntimeConfigError,
    RuntimeConfigUpdate,
    public_runtime_config,
    update_runtime_config,
)
from job_agent.constants import (
    POSITIVE_FEEDBACK_STATUSES as _POSITIVE_FEEDBACK_STATUSES,
    MEANINGFUL_FEEDBACK_STATUSES as _MEANINGFUL_FEEDBACK_STATUSES,
    DASHBOARD_VERSION as _DASHBOARD_VERSION,
    MAX_JSON_BODY as _MAX_JSON_BODY,
    MAX_UPLOAD_BODY as _MAX_UPLOAD_BODY,
)

from job_agent.services.dashboard_routes import route

logger = logging.getLogger(__name__)

_LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

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
    candidates.append(Path(str(repository.path) + "-wal"))
    mtimes = [path.stat().st_mtime for path in candidates if path.is_file()]
    return datetime.fromtimestamp(max(mtimes), tz=UTC) if mtimes else datetime.now(UTC)

def _job_source_url(job: JobDetail) -> str | None:
    return next((source.source_url for source in job.sources if source.source_url), None)

def _safe_slug(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", value).strip("-")
    return normalized[:48] or fallback

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

def build_dashboard_snapshot(
    repository: JobRepository,
    *,
    output_dir: Path,
    today: date | None = None,
    profile_path: Path | None = None,
    action_token: str = "",
) -> DashboardSnapshot:
    repository.verify()
    if profile_path is not None and profile_path.is_file():
        refresh_all_matches(repository, load_profile(profile_path))
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
    archived_jobs = repository.archived_jobs()
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
            job_archived=job.job_id in archived_jobs,
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
        if row.job_archived:
            continue
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
            item.job_archived,
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


@route("GET", r"/api/health")
def handle_health(handler, *args):
    handler._json(
        {
            "status": "ok",
            "service": "personal-job-agent",
            "version": _DASHBOARD_VERSION,
        }
    )

@route("GET", r"/api/settings")
def handle_settings_get(handler, *args):
    try:
        payload = public_runtime_config(
            handler.runtime_config_path,
            usage_path=handler.api_usage_path,
        )
    except RuntimeConfigError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return
    handler._json(payload)

@route("POST", r"/api/settings")
def handle_settings_post(handler, *args):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("API 设置必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("API 设置内容格式不正确。")
        update = RuntimeConfigUpdate.model_validate(payload)
        update_runtime_config(handler.runtime_config_path, update)
        public = public_runtime_config(
            handler.runtime_config_path,
            usage_path=handler.api_usage_path,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RuntimeConfigError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True, "settings": public})

@route("POST", r"/api/settings/test-connection")
def handle_settings_test_connection(handler, *args):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        body = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8")) if handler.headers.get("Content-Length") else {}
        provider = body.get("provider") or "local"
        model = body.get("model")
        base_url = body.get("base_url")
        api_key = body.get("api_key")

        from job_agent.services.runtime_config import effective_runtime_config
        current_cfg, _ = effective_runtime_config(handler.runtime_config_path)
        if not api_key:
            api_key = current_cfg.ai.api_key
        if not model:
            model = current_cfg.ai.model or ("deepseek-chat" if provider == "deepseek" else "gpt-4o")
        if not base_url:
            base_url = current_cfg.ai.base_url or ("https://api.deepseek.com" if provider == "deepseek" else None)

        if provider == "local":
            handler._json({"ok": True, "provider": "local", "message": "本地能力无需网络连接，已就绪！"})
            return

        if not api_key:
            handler._json({"ok": False, "provider": provider, "message": "尚未配置 API Key，请先输入密钥。"})
            return

        import time
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=10, max_retries=0)
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=2,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        handler._json({
            "ok": True,
            "provider": provider,
            "model": model,
            "latency_ms": latency_ms,
            "message": f"连接成功！{provider.upper()} 响应正常（耗时 {latency_ms}ms）。",
        })
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
        
        msg = str(exc)
        if status_code == 401:
            msg = "身份验证失败（401 Unauthorized），请检查 API Key 是否正确。"
        elif status_code == 402:
            msg = "账户余额不足（402 Insufficient Balance），API Key 有效，但需要前往服务商平台充值后方可调用。"
        elif status_code == 404:
            msg = f"未找到指定模型或接口（404 Not Found），请检查模型名 '{model}' 是否正确。"
        elif status_code == 429:
            msg = "请求过快或达到频次/额度限制（429 Too Many Requests），请稍后重试。"
        handler._json({
            "ok": False,
            "status_code": status_code,
            "message": msg,
        })

@route("GET", r"/api/dashboard")
def handle_dashboard(handler, *args):
    try:
        snapshot = build_dashboard_snapshot(
            handler.repository,
            output_dir=handler.output_dir,
            profile_path=handler.profile_path,
            action_token=handler.action_token,
        )
    except Exception:
        logger.exception("Failed to build dashboard snapshot.")
        handler._json(
            {"error": "获取看板数据时发生内部错误，请稍后重试。"},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
        return
    handler._json(snapshot.model_dump(mode="json"))
