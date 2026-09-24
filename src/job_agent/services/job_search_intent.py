from __future__ import annotations

from datetime import date
from typing import Sequence

from job_agent.models.profile import Profile


def resolve_graduation_cohort(profile: Profile, current_date: date | None = None) -> int:
    """Resolve graduation cohort year from profile education records or fallback to current date.

    If the user's profile specifies education end dates (e.g., '2026-06', '2027'),
    the latest valid year is used. Otherwise, calculates the current recruiting cohort
    based on the recruiting calendar in China (July+ corresponds to the next graduation year).
    """
    cohort: int | None = None
    for edu in profile.education:
        if edu.end:
            digits = "".join(ch for ch in edu.end if ch.isdigit())
            if len(digits) >= 4:
                try:
                    val = int(digits[:4])
                    if 2020 <= val <= 2040:
                        cohort = max(cohort or 0, val)
                except ValueError:
                    pass
    if cohort is not None:
        return cohort

    today = current_date or date.today()
    return today.year + (1 if today.month >= 7 else 0)


def resolve_employment_keywords(profile: Profile, override_type: str | None = None) -> list[str]:
    """Resolve job type keywords (e.g. 实习, 校招, 社招) from profile or override."""
    if override_type:
        type_clean = override_type.strip().casefold()
        if type_clean in ("internship", "intern", "实习"):
            return ["实习"]
        if type_clean in ("campus", "graduate", "校招", "应届"):
            return ["校招"]
        if type_clean in ("full_time", "social", "社招"):
            return ["社招"]
        return [override_type.strip()]

    types = profile.job_search.employment_types
    keywords: list[str] = []
    if types:
        for t in types:
            t_cf = t.strip().casefold()
            if t_cf in ("internship", "intern"):
                keywords.append("实习")
            elif t_cf in ("campus", "graduate"):
                keywords.append("校招")
            elif t_cf in ("full_time", "social"):
                keywords.append("社招")
            elif t.strip():
                keywords.append(t.strip())
    if not keywords:
        keywords = ["实习"]
    return list(dict.fromkeys(keywords))


def build_discovery_query(
    location: str,
    role: str,
    cohort: int,
    employment_keyword: str,
    sites: Sequence[str] = ("shixiseng.com", "nowcoder.com", "zhipin.com"),
) -> str:
    """Build a search engine query combining role, location, cohort, type and target platforms."""
    site_clause = " OR ".join(f"site:{site}" for site in sites)
    parts = [part for part in [location.strip(), role.strip(), employment_keyword.strip(), f"{cohort}届"] if part]
    base = " ".join(parts)
    return f"{base} ({site_clause})".strip()
