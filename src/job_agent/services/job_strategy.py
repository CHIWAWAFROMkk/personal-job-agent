from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import Field

from job_agent.models.base import StrictModel
from job_agent.models.job_record import JobDetail


OpportunityTrack = Literal["daily_internship", "autumn_recruitment", "other"]
RoleTier = Literal["primary", "secondary", "stretch", "other"]
CompensationFit = Literal["meets_floor", "below_floor", "unknown", "not_applicable"]
StrategyFit = Literal["recommended", "manual_review", "blocked"]


class JobStrategyResult(StrictModel):
    opportunity_track: OpportunityTrack
    role_tier: RoleTier
    compensation_min_daily: int | None = None
    compensation_max_daily: int | None = None
    compensation_fit: CompensationFit = "unknown"
    outsourcing_risk: bool = False
    workload_risk: bool = False
    deadline_days: int | None = None
    deadline_urgent: bool = False
    strategy_fit: StrategyFit
    priority_score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)


_CAMPUS_MARKERS = (
    "秋招",
    "校招",
    "校园招聘",
    "应届",
    "应届生",
    "应届毕业生",
    "graduate program",
    "graduate programme",
    "campus recruitment",
)
_INTERNSHIP_MARKERS = ("实习", "intern", "internship")

_OUTSOURCING_MARKERS = (
    "劳务派遣",
    "第三方派遣",
    "人力外包",
    "服务外包",
    "外包岗位",
    "外包员工",
    "派驻",
)
_WORKLOAD_MARKERS = (
    "996",
    "大小周",
    "高强度",
    "接受加班",
    "频繁加班",
    "随时响应",
    "强抗压",
)
_DAILY_PAY_PATTERN = re.compile(
    r"(?:日薪\s*[:：]?\s*)?(?:rmb|人民币|[¥￥])?\s*"
    r"(?P<low>\d{2,4})(?:\s*[-~—–至到]\s*(?P<high>\d{2,4}))?"
    r"\s*(?:元|rmb|人民币)?\s*(?:/|每)?\s*(?:天|日)",
    re.IGNORECASE,
)


def _compact(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value.casefold())


def infer_opportunity_track(title: str, jd_text: str) -> OpportunityTrack:
    combined = f"{title}\n{jd_text}".casefold()
    if any(marker in combined for marker in _CAMPUS_MARKERS):
        return "autumn_recruitment"
    if any(marker in combined for marker in _INTERNSHIP_MARKERS):
        return "daily_internship"
    return "other"


def infer_role_tier(
    title: str,
    jd_text: str,
    *,
    primary_roles: tuple[str, ...] | list[str] = (),
    adjacent_roles: tuple[str, ...] | list[str] = (),
) -> RoleTier:
    """Classify a role against the active user's own targets.

    The public application intentionally contains no developer-specific role list.
    """

    compact_title = _compact(title)
    compact_all = _compact(f"{title}\n{jd_text}")
    for searchable in (compact_title, compact_all):
        if any(_compact(role) in searchable for role in primary_roles if role.strip()):
            return "primary"
        if any(_compact(role) in searchable for role in adjacent_roles if role.strip()):
            return "secondary"
    return "other"


def parse_daily_compensation(text: str) -> tuple[int | None, int | None]:
    match = _DAILY_PAY_PATTERN.search(text)
    if match is None:
        return None, None
    low = int(match.group("low"))
    high = int(match.group("high") or low)
    if high < low:
        low, high = high, low
    return low, high


def detect_outsourcing(text: str) -> bool:
    normalized = text.casefold()
    normalized = re.sub(r"(?:非|不是|不属于)\s*(?:劳务|人力|服务)?\s*外包", "", normalized)
    return any(marker in normalized for marker in _OUTSOURCING_MARKERS)


def _deadline_days(value: str | None, today: date) -> int | None:
    if not value:
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", raw)
        if match is None:
            return None
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return (parsed - today).days


def evaluate_job_strategy(
    job: JobDetail,
    *,
    match_score: int | None = None,
    commute_fit: str = "unknown",
    daily_pay_floor: int | None = None,
    exclude_outsourcing: bool = True,
    primary_roles: tuple[str, ...] | list[str] = (),
    adjacent_roles: tuple[str, ...] | list[str] = (),
    today: date | None = None,
    deadline_urgent_days: int = 14,
) -> JobStrategyResult:
    """Apply user-owned search boundaries without changing the ability score."""

    today = today or date.today()
    text = f"{job.title}\n{job.jd_text}"
    track = infer_opportunity_track(job.title, job.jd_text)
    role_tier = infer_role_tier(
        job.title,
        job.jd_text,
        primary_roles=primary_roles,
        adjacent_roles=adjacent_roles,
    )
    pay_min, pay_max = parse_daily_compensation(text)
    outsourcing_risk = detect_outsourcing(text)
    workload_risk = any(marker in text.casefold() for marker in _WORKLOAD_MARKERS)
    remaining_days = _deadline_days(job.deadline_at, today)
    deadline_urgent = remaining_days is not None and 0 <= remaining_days <= deadline_urgent_days

    if track != "daily_internship":
        compensation_fit: CompensationFit = "not_applicable"
    elif pay_min is None or daily_pay_floor is None:
        compensation_fit = "unknown"
    elif pay_min < daily_pay_floor:
        compensation_fit = "below_floor"
    else:
        compensation_fit = "meets_floor"

    reasons: list[str] = []
    blocked = False
    manual_review = False
    score = match_score if match_score is not None else 50

    tier_adjustments = {"primary": 12, "secondary": 7, "stretch": 2, "other": -10}
    score += tier_adjustments[role_tier]
    if role_tier == "primary":
        reasons.append("属于当前主攻岗位方向。")
    elif role_tier == "secondary":
        reasons.append("属于可重点尝试的相邻方向。")
    elif role_tier == "stretch":
        reasons.append("属于冲刺方向，需要用项目或更强证据补足。")
    else:
        reasons.append("岗位方向未落入当前三层目标，需要人工判断。")
        manual_review = True

    if deadline_urgent:
        score += 18
        reasons.append(f"距明确截止日期仅 {remaining_days} 天，按规则优先处理。")
    elif remaining_days is not None and remaining_days < 0:
        score -= 60
        reasons.append("已超过明确截止日期。")
        blocked = True

    if commute_fit == "good":
        score += 5
        reasons.append("已记录通勤时间处于舒适区间。")
    elif commute_fit == "near_limit":
        score -= 6
        reasons.append("通勤接近本人设置的单程上限。")
    elif commute_fit == "over_limit":
        score -= 35
        reasons.append("通勤超过本人设置的单程上限。")
        blocked = True
    else:
        reasons.append("办公地点或路线未确认，通勤仍需核实。")

    if outsourcing_risk:
        score -= 45
        reasons.append("JD 出现外包、派遣或派驻信号。")
        if exclude_outsourcing:
            blocked = True
        else:
            manual_review = True

    if track == "daily_internship":
        if compensation_fit == "meets_floor":
            score += 4
            reasons.append(f"明确日薪不低于 {daily_pay_floor} 元底线。")
        elif compensation_fit == "below_floor":
            score -= 12
            reasons.append(
                f"明确日薪最低 {pay_min} 元，低于 {daily_pay_floor} 元底线；"
                "仅强公司且强相关时人工破例。"
            )
            manual_review = True
        else:
            reasons.append("日薪未明确披露，投递前需要确认。")
            manual_review = True

    if compensation_fit == "below_floor" and commute_fit in {"near_limit", "over_limit"} and workload_risk:
        score -= 18
        reasons.append("低薪、远通勤和高强度信号同时出现，不值得投入。")
        blocked = True
    elif workload_risk:
        score -= 6
        reasons.append("JD 出现高强度工作信号，建议面试前核实。")
        manual_review = True

    if match_score is not None and match_score < 60:
        manual_review = True
        reasons.append("能力匹配分低于 60，除非有明确战略价值，否则低优先级。")

    strategy_fit: StrategyFit
    if blocked:
        strategy_fit = "blocked"
    elif manual_review:
        strategy_fit = "manual_review"
    else:
        strategy_fit = "recommended"

    return JobStrategyResult(
        opportunity_track=track,
        role_tier=role_tier,
        compensation_min_daily=pay_min,
        compensation_max_daily=pay_max,
        compensation_fit=compensation_fit,
        outsourcing_risk=outsourcing_risk,
        workload_risk=workload_risk,
        deadline_days=remaining_days,
        deadline_urgent=deadline_urgent,
        strategy_fit=strategy_fit,
        priority_score=max(0, min(100, int(round(score)))),
        reasons=reasons,
    )
