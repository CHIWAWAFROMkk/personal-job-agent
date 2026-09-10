from __future__ import annotations

from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from job_agent.models.job_record import JobDetail, JobSourceReview


_OFFICIAL_ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "successfactors.com",
    "jobs.bytedance.com",
)
_ESTABLISHED_BOARD_HOSTS = (
    "shixiseng.com",
    "nowcoder.com",
    "zhipin.com",
    "liepin.com",
    "zhaopin.com",
    "51job.com",
)
_ESTABLISHED_BOARD_NAMES = (
    "实习僧",
    "牛客",
    "boss",
    "猎聘",
    "智联",
    "前程无忧",
)


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(cleaned[:10]), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _source_route(job: JobDetail) -> tuple[str, str, list[str], list[str]]:
    hosts: list[str] = []
    schemes: list[str] = []
    platforms = [source.platform.casefold() for source in job.sources]
    for source in job.sources:
        if not source.source_url:
            continue
        parsed = urlsplit(source.source_url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host:
            hosts.append(host)
            schemes.append(parsed.scheme.casefold())

    if any(_host_matches(host, _OFFICIAL_ATS_HOSTS) for host in hosts):
        return "official_ats", "公开 ATS / 企业招聘系统", hosts, schemes
    if any(_host_matches(host, _ESTABLISHED_BOARD_HOSTS) for host in hosts) or any(
        marker in platform
        for platform in platforms
        for marker in _ESTABLISHED_BOARD_NAMES
    ):
        return "established_board", "常见招聘平台", hosts, schemes
    if hosts:
        return "direct_site", "直达或尚未分类的网站", hosts, schemes
    if any(platform in {"manual", "manual_dashboard", "手工", "手动"} for platform in platforms):
        return "manual", "手工录入", hosts, schemes
    return "unknown", "来源未分类", hosts, schemes


def review_job_source(
    job: JobDetail,
    *,
    now: datetime | None = None,
) -> JobSourceReview:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    route, source_label, hosts, schemes = _source_route(job)
    positive_signals: list[str] = []
    review_items: list[str] = []

    if schemes and all(scheme == "https" for scheme in schemes):
        positive_signals.append("现有来源链接均使用 HTTPS。")
    elif schemes:
        review_items.append("存在非 HTTPS 来源，打开前先核对域名。")
    else:
        review_items.append("缺少可直接核验的岗位链接。")

    if route == "official_ats":
        positive_signals.append("至少一个来源位于公开 ATS / 企业招聘系统。")
    elif route == "established_board":
        positive_signals.append("至少一个来源位于常见招聘平台。")
    elif route in {"manual", "unknown"}:
        review_items.append("来源证据较弱，需核对公司、岗位名称和在招状态。")
    else:
        review_items.append("网站尚未归类为公开 ATS，需确认它确为企业或招聘方页面。")

    if len(job.sources) >= 2:
        positive_signals.append(f"本地记录保留了 {len(job.sources)} 个来源。")
    else:
        review_items.append("当前只有一个来源，交叉核验能力有限。")

    published_at = _parse_datetime(job.published_at)
    deadline_at = _parse_datetime(job.deadline_at)
    last_seen_at = _parse_datetime(job.last_seen_at)
    freshness = "unknown"
    freshness_label = "缺少可判断时效的日期"
    age_days: int | None = None

    if deadline_at and deadline_at.date() < now.date():
        freshness = "expired"
        freshness_label = "记录的截止日期已过"
        age_days = (now.date() - deadline_at.date()).days
        review_items.append(f"截止日期已过去 {age_days} 天。")
    elif published_at:
        delta_days = (now.date() - published_at.date()).days
        if delta_days < -2:
            freshness = "needs_recheck"
            freshness_label = "发布时间晚于当前时间"
            review_items.append("发布时间可能存在时区或录入错误。")
        else:
            age_days = max(0, delta_days)
            if age_days <= 14:
                freshness = "fresh"
                freshness_label = f"发布约 {age_days} 天"
                positive_signals.append("发布时间处于优先关注窗口。")
            elif age_days <= 45:
                freshness = "aging"
                freshness_label = f"发布约 {age_days} 天"
                review_items.append("岗位已发布一段时间，准备材料前应重新核验。")
            else:
                freshness = "stale"
                freshness_label = f"发布约 {age_days} 天"
                review_items.append("发布时间较久，可能已关闭或重复发布。")
    elif last_seen_at:
        age_days = max(0, (now.date() - last_seen_at.date()).days)
        if age_days <= 7:
            freshness = "recently_seen"
            freshness_label = f"本地最近记录于 {age_days} 天前"
            review_items.append("最近记录时间不等同于仍在招聘。")
        else:
            freshness = "needs_recheck"
            freshness_label = f"已 {age_days} 天未更新本地记录"
            review_items.append("本地记录较旧，需要重新打开页面核验。")

    if job.published_at is None:
        review_items.append("未记录原始发布时间。")
    if job.deadline_at is None:
        review_items.append("未记录申请截止时间。")
    if deadline_at and deadline_at.date() >= now.date():
        positive_signals.append("记录的申请截止时间尚未到期。")

    if freshness == "expired":
        recommended_action = "先只读核验岗位是否延期或重开；确认前不要生成或上传投递材料。"
    elif freshness in {"stale", "needs_recheck", "aging"}:
        recommended_action = "先打开来源页面核对公司、岗位、地点和在招状态，再决定是否评分或准备材料。"
    elif route in {"manual", "unknown", "direct_site"}:
        recommended_action = "先确认来源页面属于招聘方并且岗位仍在招，再进入匹配评估。"
    else:
        recommended_action = "可进入匹配评估；生成材料或填写表单前仍需再次确认岗位在招。"

    if route == "official_ats" and freshness in {"fresh", "recently_seen"}:
        confidence = "high"
    elif route in {"official_ats", "established_board", "direct_site"}:
        confidence = "medium"
    else:
        confidence = "low"

    return JobSourceReview(
        job_id=job.job_id,
        source_route=route,
        source_label=source_label,
        source_confidence=confidence,
        freshness=freshness,
        freshness_label=freshness_label,
        age_days=age_days,
        positive_signals=positive_signals,
        review_items=list(dict.fromkeys(review_items)),
        recommended_action=recommended_action,
    )
