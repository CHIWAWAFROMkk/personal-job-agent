from __future__ import annotations

import hashlib
import importlib.util
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from job_agent.models.application_tracking import ApplicationStatus
from job_agent.models.status_sync import (
    ApplicationStatusSyncReport,
    BrowserStatusCollection,
    PlatformApplicationObservation,
    StatusEvidenceSnapshot,
    StatusSyncDecision,
)
from job_agent.services.job_repository import (
    APPLICATION_STAGE_RANK,
    TERMINAL_APPLICATION_STATUSES,
    JobDatabaseError,
    JobRepository,
    normalize_company,
    normalize_title,
)
from job_agent.services.profile_store import write_json_atomic


class StatusSyncError(RuntimeError):
    pass


def _safe_endpoint(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def normalize_shixiseng_status(
    raw_status: str,
    description: str = "",
) -> ApplicationStatus | None:
    combined = f"{raw_status} {description}".casefold().strip()
    rules: tuple[tuple[tuple[str, ...], ApplicationStatus], ...] = (
        (("reject", "不合适", "拒绝", "未通过"), "rejected"),
        (("offer", "录用"), "offer"),
        (("final_interview", "终面"), "final_interview"),
        (("interview_2", "二面", "复试"), "interview_2"),
        (("interview", "一面", "面试邀请", "约面"), "interview_1"),
        (("written", "笔试"), "written_test"),
        (("assessment", "测评"), "assessment"),
        (("resume_requested", "索要简历", "请求简历", "请发简历"), "resume_requested"),
        (("undeter", "初步筛选", "筛选中"), "screening"),
        (("checked", "简历被查看", "已查看", "hr已读", "hr 已读"), "hr_read"),
        (("delivered", "投递成功", "已投递"), "applied"),
    )
    for markers, status in rules:
        if any(marker in combined for marker in markers):
            return status
    return None


def _parse_activity_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and not value.strip().isdigit():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def extract_shixiseng_observations(
    payload: Any,
    *,
    source_endpoint: str = "",
    observed_at: datetime | None = None,
) -> list[PlatformApplicationObservation]:
    """Extract only status metadata; message bodies and personal fields are discarded."""

    observed_at = observed_at or datetime.now(UTC)
    candidates: list[PlatformApplicationObservation] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            title = str(value.get("title", "")).strip()
            company = str(value.get("company_name", "")).strip()
            raw_status = str(value.get("deliver_status", "")).strip()
            description = str(value.get("deliver_status_desc", "")).strip()
            external_id = str(value.get("uuid", "")).strip()
            stype = str(value.get("stype", "")).strip()
            looks_like_application = (
                external_id.startswith("dlv_") or stype == "normal_deliver"
            )
            if title and company and raw_status and looks_like_application:
                stable_id = external_id or (
                    "derived_"
                    + hashlib.sha256(
                        f"{company}|{title}".encode("utf-8")
                    ).hexdigest()[:16]
                )
                candidates.append(
                    PlatformApplicationObservation(
                        platform="shixiseng",
                        external_application_id=stable_id,
                        company=company,
                        title=title,
                        raw_status=raw_status,
                        raw_status_description=description,
                        normalized_status=normalize_shixiseng_status(
                            raw_status,
                            description,
                        ),
                        latest_activity_at=_parse_activity_time(
                            value.get("latest_time")
                        ),
                        observed_at=observed_at,
                        source_endpoint=_safe_endpoint(source_endpoint)
                        if source_endpoint
                        else "",
                    )
                )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    deduped: dict[
        tuple[str, str, str, str], PlatformApplicationObservation
    ] = {}
    for item in candidates:
        key = (
            item.external_application_id,
            item.raw_status,
            item.raw_status_description,
            item.latest_activity_at.isoformat() if item.latest_activity_at else "",
        )
        deduped[key] = item
    return list(deduped.values())


def _valid_shixiseng_url(value: str) -> bool:
    parts = urlsplit(value)
    hostname = (parts.hostname or "").casefold().rstrip(".")
    return (
        parts.scheme == "https"
        and (hostname == "shixiseng.com" or hostname.endswith(".shixiseng.com"))
    )


def collect_shixiseng_statuses(
    target_urls: list[str],
    *,
    browser_profile_dir: Path,
    evidence_root: Path,
    browser_channel: str = "chromium",
    headless: bool = True,
    wait_milliseconds: int = 5000,
) -> BrowserStatusCollection:
    """Open existing job pages read-only and observe status API responses."""

    if importlib.util.find_spec("playwright") is None:
        raise StatusSyncError("Playwright 尚未安装，无法只读同步招聘进度。")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise StatusSyncError("Playwright 无法导入。") from exc

    unique_urls = list(dict.fromkeys(url for url in target_urls if _valid_shixiseng_url(url)))
    if not unique_urls:
        raise StatusSyncError("没有可用于实习僧只读同步的 HTTPS 岗位链接。")
    unique_urls = unique_urls[:10]
    started_at = datetime.now(UTC)
    session_id = (
        "shixiseng-"
        + started_at.strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    session_dir = evidence_root.expanduser().resolve() / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    screenshot_path = session_dir / "只读状态同步.png"
    browser_profile_dir = browser_profile_dir.expanduser().resolve()
    browser_profile_dir.mkdir(parents=True, exist_ok=True)
    observations: list[PlatformApplicationObservation] = []
    response_endpoints: list[str] = []
    final_urls: list[str] = []
    blocker: str | None = None
    collection_status = "no_data"
    context = None

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(browser_profile_dir),
                headless=headless,
                channel=browser_channel,
                locale="zh-CN",
            )
            context.set_default_timeout(5000)
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(response: Any) -> None:
                try:
                    hostname = (urlsplit(response.url).hostname or "").casefold()
                    content_type = response.headers.get("content-type", "").casefold()
                    if not hostname.endswith("shixiseng.com") or "json" not in content_type:
                        return
                    extracted = extract_shixiseng_observations(
                        response.json(),
                        source_endpoint=response.url,
                    )
                    if extracted:
                        observations.extend(extracted)
                        response_endpoints.append(_safe_endpoint(response.url))
                except Exception:
                    return

            page.on("response", on_response)
            for url in unique_urls:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(wait_milliseconds)
                final_urls.append(page.url)
            page.screenshot(path=str(screenshot_path), full_page=True)
            login_observation = page.evaluate(
                r"""
() => {
  const body = (document.body?.innerText || '').replace(/\s+/g, ' ');
  const visible = el => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 &&
      style.display !== 'none' && style.visibility !== 'hidden';
  };
  return {
    password: Array.from(document.querySelectorAll('input[type="password"]')).some(visible),
    loginPage: /请先登录|手机号登录|登录后/.test(body) && /登录/.test(document.title + ' ' + body),
  };
}
"""
            )
            if observations:
                collection_status = "completed"
            elif login_observation.get("password") or login_observation.get("loginPage"):
                collection_status = "login_required"
                blocker = "实习僧登录状态已失效，需要本人在专用浏览器中重新登录。"
            else:
                collection_status = "no_data"
                blocker = "页面没有返回可识别的投递状态数据，未修改本地数据库。"
    except Exception as exc:
        collection_status = "blocked"
        blocker = f"只读状态采集失败：{type(exc).__name__}"
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    deduped: dict[
        tuple[str, str, str, str], PlatformApplicationObservation
    ] = {}
    for item in observations:
        key = (
            item.external_application_id,
            item.raw_status,
            item.raw_status_description,
            item.latest_activity_at.isoformat() if item.latest_activity_at else "",
        )
        deduped[key] = item
    return BrowserStatusCollection(
        session_id=session_id,
        platform="shixiseng",
        started_at=started_at,
        finished_at=datetime.now(UTC),
        status=collection_status,
        requested_urls=unique_urls,
        final_urls=final_urls,
        response_endpoints=list(dict.fromkeys(response_endpoints)),
        observations=list(deduped.values()),
        screenshot_path=str(screenshot_path) if screenshot_path.is_file() else None,
        blocker=blocker,
    )


def _observation_time_key(item: PlatformApplicationObservation) -> datetime:
    return item.latest_activity_at or item.observed_at


def _is_stale_status(previous: ApplicationStatus, observed: ApplicationStatus) -> bool:
    if previous == observed:
        return False
    if previous in TERMINAL_APPLICATION_STATUSES:
        return True
    if observed in TERMINAL_APPLICATION_STATUSES:
        return False
    previous_rank = APPLICATION_STAGE_RANK.get(previous, -1)
    observed_rank = APPLICATION_STAGE_RANK.get(observed, -1)
    return observed_rank < previous_rank


def reconcile_status_collection(
    repository: JobRepository,
    collection: BrowserStatusCollection,
    *,
    evidence_root: Path,
    dry_run: bool = False,
) -> tuple[ApplicationStatusSyncReport, Path]:
    """Match exact company/title pairs and update only non-regressive statuses."""

    applications = repository.list_applications(limit=1000)
    by_key: dict[tuple[str, str], list[Any]] = {}
    for application in applications:
        key = (
            normalize_company(application.company),
            normalize_title(application.title),
        )
        by_key.setdefault(key, []).append(application)

    exact_by_job: dict[int, list[PlatformApplicationObservation]] = {}
    unmatched = 0
    ambiguous_observations: list[PlatformApplicationObservation] = []
    for observation in collection.observations:
        key = (
            normalize_company(observation.company),
            normalize_title(observation.title),
        )
        candidates = by_key.get(key, [])
        if len(candidates) == 1:
            exact_by_job.setdefault(candidates[0].job_id, []).append(observation)
        elif len(candidates) > 1:
            ambiguous_observations.append(observation)
        else:
            unmatched += 1

    selected: dict[int, PlatformApplicationObservation] = {}
    conflicting_jobs: set[int] = set()
    for job_id, observations in exact_by_job.items():
        ordered = sorted(observations, key=_observation_time_key, reverse=True)
        latest_time = _observation_time_key(ordered[0])
        latest = [item for item in ordered if _observation_time_key(item) == latest_time]
        latest_statuses = {item.normalized_status for item in latest}
        if len(latest_statuses) > 1:
            conflicting_jobs.add(job_id)
        else:
            selected[job_id] = latest[0]

    session_dir = evidence_root.expanduser().resolve() / collection.session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = session_dir / "status-evidence.json"
    snapshot = StatusEvidenceSnapshot(
        session_id=collection.session_id,
        platform=collection.platform,
        collected_at=collection.finished_at,
        collection_status=collection.status,
        matched_observations=list(selected.values()),
        unmatched_observation_count=unmatched + len(ambiguous_observations),
        response_endpoints=collection.response_endpoints,
        screenshot_path=collection.screenshot_path,
    )
    write_json_atomic(snapshot.model_dump(mode="json"), evidence_path)

    decisions: list[StatusSyncDecision] = []
    for observation in ambiguous_observations:
        decisions.append(
            StatusSyncDecision(
                external_application_id=observation.external_application_id,
                company=observation.company,
                title=observation.title,
                match_method="ambiguous",
                observed_status=observation.normalized_status,
                outcome="manual_review",
                detail="公司和岗位名称对应多个本地投递记录，未自动选择。",
            )
        )
    for job_id in conflicting_jobs:
        observation = sorted(
            exact_by_job[job_id], key=_observation_time_key, reverse=True
        )[0]
        current = repository.get_application(job_id)
        decisions.append(
            StatusSyncDecision(
                external_application_id=observation.external_application_id,
                company=observation.company,
                title=observation.title,
                job_id=job_id,
                match_method="exact_company_title",
                previous_status=current.status if current else None,
                observed_status=observation.normalized_status,
                outcome="manual_review",
                detail="平台同一时间返回了互相冲突的状态，未自动更新。",
            )
        )

    for job_id, observation in selected.items():
        current = repository.get_application(job_id)
        if current is None:
            continue
        observed_status = observation.normalized_status
        source_time = (
            observation.latest_activity_at.isoformat()
            if observation.latest_activity_at
            else "平台未提供"
        )
        detail = (
            f"实习僧只读同步：{observation.raw_status_description or observation.raw_status}；"
            f"平台最近活动时间 {source_time}。"
        )
        if observed_status is None:
            decisions.append(
                StatusSyncDecision(
                    external_application_id=observation.external_application_id,
                    company=observation.company,
                    title=observation.title,
                    job_id=job_id,
                    match_method="exact_company_title",
                    previous_status=current.status,
                    outcome="manual_review",
                    detail="平台状态无法安全映射到本地状态，未更新。",
                )
            )
            continue
        if _is_stale_status(current.status, observed_status):
            decisions.append(
                StatusSyncDecision(
                    external_application_id=observation.external_application_id,
                    company=observation.company,
                    title=observation.title,
                    job_id=job_id,
                    match_method="exact_company_title",
                    previous_status=current.status,
                    observed_status=observed_status,
                    outcome="stale",
                    detail="平台返回的阶段低于本地已确认进度，保留本地状态。",
                )
            )
            continue
        if dry_run:
            decisions.append(
                StatusSyncDecision(
                    external_application_id=observation.external_application_id,
                    company=observation.company,
                    title=observation.title,
                    job_id=job_id,
                    match_method="exact_company_title",
                    previous_status=current.status,
                    observed_status=observed_status,
                    outcome="dry_run",
                    detail="试运行仅核对匹配和状态，不修改数据库。",
                )
            )
            continue
        try:
            if current.status == observed_status:
                repository.update_application_verification(
                    job_id,
                    verification_method="shixiseng_read_only_sync",
                    evidence_path=str(evidence_path),
                )
                outcome = "unchanged"
            else:
                repository.record_application_status(
                    job_id,
                    observed_status,
                    source="shixiseng_sync",
                    detail=detail,
                    verification_method="shixiseng_read_only_sync",
                    evidence_path=str(evidence_path),
                )
                outcome = "updated"
        except JobDatabaseError as exc:
            decisions.append(
                StatusSyncDecision(
                    external_application_id=observation.external_application_id,
                    company=observation.company,
                    title=observation.title,
                    job_id=job_id,
                    match_method="exact_company_title",
                    previous_status=current.status,
                    observed_status=observed_status,
                    outcome="manual_review",
                    detail=f"数据库安全规则拒绝更新：{exc}",
                )
            )
            continue
        decisions.append(
            StatusSyncDecision(
                external_application_id=observation.external_application_id,
                company=observation.company,
                title=observation.title,
                job_id=job_id,
                match_method="exact_company_title",
                previous_status=current.status,
                observed_status=observed_status,
                outcome=outcome,
                detail=detail,
            )
        )

    report = ApplicationStatusSyncReport(
        session_id=collection.session_id,
        platform=collection.platform,
        started_at=collection.started_at,
        finished_at=datetime.now(UTC),
        dry_run=dry_run,
        collection_status=collection.status,
        observed=len(collection.observations),
        matched=len(selected),
        updated=sum(item.outcome == "updated" for item in decisions),
        unchanged=sum(item.outcome == "unchanged" for item in decisions),
        stale=sum(item.outcome == "stale" for item in decisions),
        manual_review=sum(item.outcome == "manual_review" for item in decisions),
        unmatched=unmatched,
        evidence_path=str(evidence_path),
        screenshot_path=collection.screenshot_path,
        decisions=decisions,
        blocker=collection.blocker,
    )
    report_path = session_dir / "sync-report.json"
    write_json_atomic(report.model_dump(mode="json"), report_path)
    return report, report_path
