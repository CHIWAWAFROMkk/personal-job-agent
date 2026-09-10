from __future__ import annotations

import re
from dataclasses import dataclass

from job_agent.models.job import (
    HardGateAssessment,
    HardGateStatus,
    MatchEvidence,
    MatchResult,
    MatchStatus,
    Recommendation,
    Requirement,
    RequirementImportance,
    ScoreBreakdown,
    StructuredJob,
    recommendation_for_score,
)
from job_agent.models.profile import ClaimStatus, EvidenceFact, Profile


SKILL_CATALOG: dict[str, tuple[str, ...]] = {
    "Excel": ("excel", "vlookup", "xlookup", "pivot table", "数据透视表"),
    "SQL": ("sql", "mysql", "postgresql", "sqlite"),
    "Python": ("python", "pandas", "numpy"),
    "Power BI": ("power bi", "powerbi", "dax"),
    "Tableau": ("tableau",),
    "数据分析": (
        "数据分析",
        "data analysis",
        "数据洞察",
        "数据监控",
        "数据复盘",
        "数据思维",
    ),
    "数据可视化": ("数据可视化", "visualization", "可视化"),
    "A/B 测试": ("a/b test", "ab test", "a/b测试", "ab测试"),
    "AI/大模型": ("ai", "人工智能", "大模型", "llm", "generative ai", "生成式ai"),
    "Prompt Engineering": ("prompt engineering", "提示词", "prompt"),
    "自动化": ("自动化", "automation", "rpa"),
    "用户运营": ("用户运营", "community operation", "用户增长"),
    "内容运营": ("内容运营", "content operation", "内容策划"),
    "产品运营": ("产品运营", "product operation"),
    "新媒体运营": ("新媒体", "social media operation", "社媒运营"),
    "项目管理": ("项目管理", "project management"),
    "活动策划": ("活动策划", "活动运营", "活动配置"),
    "需求分析": (
        "需求分析",
        "requirements analysis",
        "产品需求",
        "需求跟进",
        "bug提报",
        "测试验收",
    ),
    "跨部门协作": ("跨部门", "沟通协调", "cross-functional"),
    "流程标准化": ("sop", "标准化", "流程沉淀"),
    "竞品分析": ("竞品分析", "competitive analysis"),
    "市场调研": ("市场调研", "market research"),
    "英语": ("英语", "english", "cet-4", "cet4", "cet-6", "cet6"),
}

REQUIREMENT_MARKERS = (
    "要求",
    "任职资格",
    "岗位要求",
    "你需要",
    "必须",
    "熟悉",
    "熟练",
    "掌握",
    "具备",
    "优先",
    "加分",
    "本科",
    "硕士",
    "博士",
    "每周",
    "实习",
    "required",
    "requirements",
    "preferred",
    "qualifications",
)

RESPONSIBILITY_MARKERS = (
    "职责",
    "工作内容",
    "你将",
    "负责",
    "responsibilities",
    "what you'll do",
)

HARD_GATE_MARKERS = (
    "必须",
    "硬性",
    "本科及以上",
    "硕士及以上",
    "博士",
    "每周",
    "连续实习",
    "实习至少",
    "不少于",
    "到岗",
    "届毕业",
    "届学生",
    "cet-",
    "证书",
    "required",
)

SECTION_HEADINGS = {
    "岗位职责": "responsibilities",
    "职位职责": "responsibilities",
    "工作职责": "responsibilities",
    "工作内容": "responsibilities",
    "任职要求": "requirements",
    "岗位要求": "requirements",
    "职位要求": "requirements",
    "任职资格": "requirements",
}

METADATA_LABELS = {
    "公司",
    "公司名称",
    "岗位",
    "职位",
    "职位名称",
    "地点",
    "工作地点",
    "城市",
    "来源",
    "岗位链接",
    "职位链接",
    "页面刷新时间",
    "抓取核验状态",
    "薪资",
    "截止日期",
}


def _contains(text: str, alias: str) -> bool:
    haystack = text.casefold()
    needle = alias.casefold().strip()
    if not needle:
        return False
    if re.fullmatch(r"[a-z0-9.+#/-]+", needle):
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])",
                haystack,
            )
        )
    return needle in haystack


def _any_alias(text: str, aliases: tuple[str, ...] | list[str]) -> bool:
    return any(_contains(text, alias) for alias in aliases)


def _clean_line(line: str) -> str:
    return re.sub(r"^[\s\-–—•·*\d.、（）()]+", "", line).strip()


def _line_label(line: str) -> str:
    return re.split(r"[:：]", line, maxsplit=1)[0].strip()


def _section_heading(line: str) -> str | None:
    normalized = line.strip().rstrip(":：").strip()
    return SECTION_HEADINGS.get(normalized)


def _is_metadata_line(line: str) -> bool:
    return _line_label(line) in METADATA_LABELS and bool(re.search(r"[:：]", line))


def _split_requirement_clauses(line: str) -> list[str]:
    clauses = [item.strip(" 。") for item in re.split(r"[，,；;]", line)]
    return [item for item in clauses if len(item) > 2]


def _extract_labeled_value(lines: list[str], labels: tuple[str, ...]) -> str | None:
    for line in lines:
        for label in labels:
            match = re.match(rf"^\s*{re.escape(label)}\s*[:：]\s*(.+)$", line, re.I)
            if match:
                return match.group(1).strip()
    return None


def _requirement_category(line: str) -> str:
    lowered = line.casefold()
    categories = {
        "education": ("学历", "本科", "硕士", "博士", "degree"),
        "major": ("专业", "major"),
        "experience": ("经验", "经历", "experience"),
        "availability": ("到岗", "每周", "实习", "个月", "availability"),
        "location": ("地点", "城市", "location"),
        "tool": ("工具", "软件", "excel", "sql", "python", "tableau", "power bi"),
        "language": ("英语", "english", "cet"),
    }
    for category, markers in categories.items():
        if any(marker in lowered for marker in markers):
            return category
    return "other"


def structure_job_locally(
    raw_text: str,
    *,
    company: str | None = None,
    title: str | None = None,
    location: str | None = None,
    source: str = "manual",
    source_url: str | None = None,
) -> StructuredJob:
    raw_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    clean_lines = [_clean_line(line) for line in raw_lines]
    company = company or _extract_labeled_value(raw_lines, ("公司", "公司名称", "Company"))
    title = title or _extract_labeled_value(raw_lines, ("岗位", "职位", "职位名称", "Role", "Title"))
    location = location or _extract_labeled_value(raw_lines, ("地点", "工作地点", "城市", "Location"))

    responsibilities: list[str] = []
    requirements: list[Requirement] = []
    current_section: str | None = None
    for line in clean_lines:
        heading = _section_heading(line)
        if heading:
            current_section = heading
            continue
        if _is_metadata_line(line):
            continue
        lowered = line.casefold()
        is_requirement = current_section == "requirements" or (
            current_section is None
            and any(marker in lowered for marker in REQUIREMENT_MARKERS)
        )
        is_responsibility = current_section == "responsibilities" or (
            current_section is None
            and any(marker in lowered for marker in RESPONSIBILITY_MARKERS)
        )
        if is_responsibility and len(line) > 4:
            responsibilities.append(line)
        if is_requirement and len(line) > 2:
            for clause in _split_requirement_clauses(line):
                clause_lowered = clause.casefold()
                preferred = any(
                    marker in clause_lowered
                    for marker in ("优先", "加分", "preferred", "plus")
                )
                hard_gate = not preferred and any(
                    marker in clause_lowered for marker in HARD_GATE_MARKERS
                )
                requirements.append(
                    Requirement(
                        text=clause,
                        category=_requirement_category(clause),
                        importance=(
                            RequirementImportance.PREFERRED
                            if preferred
                            else RequirementImportance.REQUIRED
                        ),
                        hard_gate=hard_gate,
                        jd_evidence=line,
                    )
                )

    required_skills: list[str] = []
    preferred_skills: list[str] = []
    tools: list[str] = []
    skill_lines = [requirement.text for requirement in requirements]
    for display, aliases in SKILL_CATALOG.items():
        matching_lines = [line for line in skill_lines if _any_alias(line, aliases)]
        if not matching_lines:
            continue
        is_preferred_only = all(
            any(marker in line.casefold() for marker in ("优先", "加分", "preferred", "plus"))
            for line in matching_lines
        )
        if is_preferred_only:
            preferred_skills.append(display)
        else:
            required_skills.append(display)
        if display in {"Excel", "SQL", "Python", "Power BI", "Tableau"}:
            tools.append(display)

    return StructuredJob(
        company=company or "未识别",
        title=title or "未识别",
        source=source,
        source_url=source_url,
        location=location,
        responsibilities=list(dict.fromkeys(responsibilities)),
        requirements=requirements,
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        tools=tools,
        education_requirements=[r.text for r in requirements if r.category == "education"],
        major_requirements=[r.text for r in requirements if r.category == "major"],
        internship_requirements=[r.text for r in requirements if r.category == "experience"],
        arrival_requirements=[r.text for r in requirements if r.category == "availability"],
        raw_text=raw_text,
    )


def _approved_facts(profile: Profile) -> list[EvidenceFact]:
    return [
        fact
        for experience in profile.experiences
        for fact in experience.facts
        if profile.is_application_ready(fact.status)
    ]


def _approved_skill_map(profile: Profile) -> dict[str, tuple[list[str], list[str]]]:
    result: dict[str, tuple[list[str], list[str]]] = {}

    def register(name: str, aliases: list[str], fact_ids: list[str]) -> None:
        if not name.strip():
            return
        existing_aliases, existing_fact_ids = result.get(name, ([], []))
        result[name] = (
            list(dict.fromkeys([*existing_aliases, *aliases])),
            list(dict.fromkeys([*existing_fact_ids, *fact_ids])),
        )

    for skill in profile.skills:
        if not profile.is_application_ready(skill.status):
            continue
        aliases = list(dict.fromkeys([skill.name, *skill.aliases]))
        register(skill.name, aliases, skill.evidence_fact_ids)
    for fact in _approved_facts(profile):
        for name in [*fact.skills, *fact.tools]:
            register(name, [name], [fact.id])
    return result


def _profile_has_catalog_skill(
    skill_name: str,
    approved_skills: dict[str, tuple[list[str], list[str]]],
) -> tuple[bool, list[str]]:
    catalog_aliases = SKILL_CATALOG.get(skill_name, (skill_name,))
    evidence_ids: list[str] = []
    for profile_name, (profile_aliases, fact_ids) in approved_skills.items():
        combined = [profile_name, *profile_aliases]
        if any(
            _contains(alias, catalog_alias) or _contains(catalog_alias, alias)
            for alias in combined
            for catalog_alias in catalog_aliases
        ):
            evidence_ids.extend(fact_ids)
    return bool(evidence_ids) or any(
        _any_alias(profile_name, list(catalog_aliases))
        or _any_alias(" ".join(aliases), list(catalog_aliases))
        for profile_name, (aliases, _) in approved_skills.items()
    ), list(dict.fromkeys(evidence_ids))


def _role_direction_score(profile: Profile, job: StructuredJob) -> tuple[int, list[str]]:
    roles = [*profile.job_search.target_roles, *profile.job_search.adjacent_roles]
    if not roles:
        return 0, []
    searchable = f"{job.title}\n{job.raw_text}"
    direct = [role for role in roles if _contains(searchable, role)]
    if direct:
        target_direct = [role for role in profile.job_search.target_roles if role in direct]
        return (20 if target_direct else 15), direct
    title_lowered = job.title.casefold()
    role_families = ("运营", "产品", "数据", "市场", "人力资源", "项目")
    family_hits = [
        family
        for family in role_families
        if family in title_lowered and any(family in role for role in roles)
    ]
    if family_hits:
        target_family = any(
            family in role
            for family in family_hits
            for role in profile.job_search.target_roles
        )
        return (14 if target_family else 10), [f"{family}（相邻方向）" for family in family_hits]
    role_tokens = {
        token
        for role in roles
        for token in re.findall(r"[a-zA-Z]{2,}|[\u4e00-\u9fff]{2,4}", role)
    }
    hits = [token for token in role_tokens if _contains(searchable, token)]
    if not role_tokens:
        return 0, []
    return min(12, round(12 * len(hits) / len(role_tokens))), hits


def _relevant_facts(facts: list[EvidenceFact], raw_jd: str) -> list[EvidenceFact]:
    relevant: list[EvidenceFact] = []
    for fact in facts:
        terms = [*fact.skills, *fact.tools]
        latin_terms = re.findall(r"[a-zA-Z][a-zA-Z0-9+.#/-]{2,}", fact.statement)
        terms.extend(latin_terms)
        if any(_contains(raw_jd, term) for term in terms if len(term.strip()) >= 2):
            relevant.append(fact)
    return relevant


def _degree_rank(value: str) -> int | None:
    lowered = value.casefold()
    explicit_minimum = re.search(
        r"(博士|phd|doctor|硕士|研究生|master|本科|学士|bachelor|大专|专科|associate)"
        r"\s*(?:学历)?\s*(?:及以上|以上)",
        lowered,
    )
    if explicit_minimum:
        minimum = explicit_minimum.group(1)
        if minimum in ("博士", "phd", "doctor"):
            return 4
        if minimum in ("硕士", "研究生", "master"):
            return 3
        if minimum in ("本科", "学士", "bachelor"):
            return 2
        return 1
    if any(item in lowered for item in ("博士", "phd", "doctor")):
        return 4
    if any(item in lowered for item in ("硕士", "研究生", "master")):
        return 3
    if any(item in lowered for item in ("本科", "学士", "bachelor")):
        return 2
    if any(item in lowered for item in ("大专", "专科", "associate")):
        return 1
    return None


def _evaluate_hard_gate(profile: Profile, requirement: str) -> HardGateAssessment:
    lowered = requirement.casefold()
    approved_education = [
        item for item in profile.education if profile.is_application_ready(item.status)
    ]

    required_rank = _degree_rank(requirement)
    if required_rank is not None:
        ranks = [_degree_rank(item.degree) for item in approved_education]
        known_ranks = [rank for rank in ranks if rank is not None]
        if not known_ranks:
            status = HardGateStatus.UNKNOWN
            explanation = "Profile 中没有已确认的学历层级。"
        elif max(known_ranks) >= required_rank:
            status = HardGateStatus.PASSES
            explanation = "已确认学历达到该要求。"
        else:
            status = HardGateStatus.FAILS
            explanation = "已确认学历低于该硬性要求。"
        return HardGateAssessment(
            requirement=requirement,
            status=status,
            explanation=explanation,
        )

    day_match = re.search(r"每周[^\d\n]{0,12}(\d)\s*[天日]", lowered)
    if day_match:
        required_days = int(day_match.group(1))
        availability = profile.job_search.availability
        available_days = availability.days_per_week
        maximum_days = availability.max_days_per_week
        if available_days is None:
            status = HardGateStatus.UNKNOWN
            explanation = "Profile 中尚未确认每周可到岗天数。"
        elif available_days >= required_days:
            status = HardGateStatus.PASSES
            explanation = f"每周保证可到岗 {available_days} 天，达到要求。"
        elif maximum_days is not None and maximum_days >= required_days:
            status = HardGateStatus.UNKNOWN
            explanation = (
                f"每周保证 {available_days} 天、最多 {maximum_days} 天；"
                f"该岗位要求稳定 {required_days} 天，需要进一步确认。"
            )
        elif maximum_days is None or maximum_days < required_days:
            status = HardGateStatus.FAILS
            upper = maximum_days if maximum_days is not None else available_days
            explanation = f"每周最多可到岗 {upper} 天，低于要求。"
        return HardGateAssessment(
            requirement=requirement,
            status=status,
            explanation=explanation,
        )

    month_match = re.search(r"(?:至少|不少于|连续)?\s*(\d+)\s*个?月", lowered)
    if month_match and "实习" in lowered:
        required_months = int(month_match.group(1))
        availability = profile.job_search.availability
        available_months = availability.duration_months
        maximum_months = availability.max_duration_months
        if available_months is None:
            status = HardGateStatus.UNKNOWN
            explanation = "Profile 中尚未确认可连续实习月数。"
        elif available_months >= required_months:
            status = HardGateStatus.PASSES
            explanation = f"保证可实习 {available_months} 个月，达到要求。"
        elif maximum_months is None:
            status = HardGateStatus.UNKNOWN
            explanation = (
                f"已确认至少可实习 {available_months} 个月，但尚未确认是否能达到"
                f"岗位要求的 {required_months} 个月。"
            )
        elif maximum_months >= required_months:
            status = HardGateStatus.UNKNOWN
            explanation = (
                f"可实习 {available_months}-{maximum_months} 个月；"
                f"是否能承诺 {required_months} 个月需要进一步确认。"
            )
        else:
            status = HardGateStatus.FAILS
            explanation = f"最多可实习 {maximum_months} 个月，低于要求。"
        return HardGateAssessment(
            requirement=requirement,
            status=status,
            explanation=explanation,
        )

    graduation_years = re.findall(r"(20\d{2})\s*届", requirement)
    if graduation_years:
        profile_years = {
            match.group(1)
            for item in approved_education
            if item.end
            for match in [re.search(r"(20\d{2})", item.end)]
            if match
        }
        if not profile_years:
            status = HardGateStatus.UNKNOWN
            explanation = "Profile 中尚未确认毕业年份。"
        elif profile_years.intersection(graduation_years):
            status = HardGateStatus.PASSES
            explanation = "已确认毕业年份符合要求。"
        else:
            status = HardGateStatus.FAILS
            explanation = "已确认毕业年份不在 JD 指定范围内。"
        return HardGateAssessment(
            requirement=requirement,
            status=status,
            explanation=explanation,
        )

    return HardGateAssessment(
        requirement=requirement,
        status=HardGateStatus.UNKNOWN,
        explanation="本地规则无法可靠判断，需要人工确认。",
    )


def _education_score(profile: Profile, hard_gates: list[HardGateAssessment]) -> int:
    education_gates = [gate for gate in hard_gates if _degree_rank(gate.requirement)]
    if any(gate.status == HardGateStatus.FAILS for gate in education_gates):
        return 0
    if any(gate.status == HardGateStatus.PASSES for gate in education_gates):
        return 10
    if any(gate.status == HardGateStatus.UNKNOWN for gate in education_gates):
        return 4
    has_education = any(
        profile.is_application_ready(item.status) for item in profile.education
    )
    return 10 if has_education else 0


def _logistics_score(
    profile: Profile,
    job: StructuredJob,
    hard_gates: list[HardGateAssessment],
) -> int:
    preferences = profile.job_search.preferred_locations
    if not job.location or not preferences:
        location_score = 2
    elif any(_contains(job.location, place) or _contains(place, job.location) for place in preferences):
        location_score = 5
    else:
        location_score = 0

    availability_gates = [
        gate
        for gate in hard_gates
        if re.search(r"每周|实习.*月|个月.*实习|到岗", gate.requirement, re.I)
    ]
    if any(gate.status == HardGateStatus.FAILS for gate in availability_gates):
        availability_score = 0
    elif availability_gates and all(
        gate.status == HardGateStatus.PASSES for gate in availability_gates
    ):
        availability_score = 5
    elif availability_gates:
        availability_score = 2
    else:
        availability_score = 5
    return location_score + availability_score


def _preference_score(profile: Profile, raw_jd: str) -> int:
    avoids = [item for item in profile.job_search.avoid if _contains(raw_jd, item)]
    if avoids:
        return 0
    must_haves = profile.job_search.must_haves
    if not must_haves:
        return 5
    hits = sum(1 for item in must_haves if _contains(raw_jd, item))
    return round(5 * hits / len(must_haves))


def _cap_breakdown(breakdown: ScoreBreakdown, cap: int) -> ScoreBreakdown:
    values = breakdown.model_dump()
    excess = breakdown.total - cap
    if excess <= 0:
        return breakdown
    for field in ("preferences", "logistics", "education", "experience", "skills", "role_direction"):
        reduction = min(values[field], excess)
        values[field] -= reduction
        excess -= reduction
        if excess == 0:
            break
    return ScoreBreakdown.model_validate(values)


def match_job_locally(profile: Profile, job: StructuredJob) -> MatchResult:
    approved_skills = _approved_skill_map(profile)
    facts = _approved_facts(profile)
    relevant_facts = _relevant_facts(facts, job.raw_text)
    role_score, role_hits = _role_direction_score(profile, job)

    evidence: list[MatchEvidence] = []
    matched_required: list[str] = []
    missing_required: list[str] = []
    matched_preferred: list[str] = []
    for skill_name in job.required_skills:
        matched, fact_ids = _profile_has_catalog_skill(skill_name, approved_skills)
        if matched:
            matched_required.append(skill_name)
            status = MatchStatus.MATCHED
            explanation = "Profile 中有已确认技能或经历证据。"
        else:
            missing_required.append(skill_name)
            status = MatchStatus.GAP
            explanation = "Profile 中未找到已确认证据。"
        evidence.append(
            MatchEvidence(
                requirement=skill_name,
                status=status,
                profile_fact_ids=fact_ids,
                explanation=explanation,
            )
        )
    for skill_name in job.preferred_skills:
        matched, fact_ids = _profile_has_catalog_skill(skill_name, approved_skills)
        if matched:
            matched_preferred.append(skill_name)
        evidence.append(
            MatchEvidence(
                requirement=skill_name,
                status=MatchStatus.MATCHED if matched else MatchStatus.GAP,
                profile_fact_ids=fact_ids,
                explanation=(
                    "Profile 中有已确认技能或经历证据。"
                    if matched
                    else "Profile 中未找到已确认证据。"
                ),
            )
        )

    if job.required_skills:
        required_ratio = len(matched_required) / len(job.required_skills)
        preferred_ratio = (
            len(matched_preferred) / len(job.preferred_skills)
            if job.preferred_skills
            else required_ratio
        )
        skills_score = round(30 * (0.85 * required_ratio + 0.15 * preferred_ratio))
    elif job.preferred_skills:
        skills_score = round(30 * len(matched_preferred) / len(job.preferred_skills))
    else:
        skills_score = 10 if approved_skills else 0

    if not facts:
        experience_score = 0
    elif relevant_facts:
        experience_score = min(25, 10 + 5 * min(len(relevant_facts), 3))
    else:
        experience_score = 4

    hard_gates = [
        _evaluate_hard_gate(profile, requirement.text)
        for requirement in job.requirements
        if requirement.hard_gate
    ]
    breakdown = ScoreBreakdown(
        role_direction=role_score,
        skills=skills_score,
        experience=experience_score,
        education=_education_score(profile, hard_gates),
        logistics=_logistics_score(profile, job, hard_gates),
        preferences=_preference_score(profile, job.raw_text),
    )
    if any(gate.status == HardGateStatus.FAILS for gate in hard_gates):
        breakdown = _cap_breakdown(breakdown, 59)
    elif any(gate.status == HardGateStatus.UNKNOWN for gate in hard_gates):
        breakdown = _cap_breakdown(breakdown, 84)

    score = breakdown.total
    why_fit: list[str] = []
    if role_hits:
        why_fit.append("岗位方向命中: " + "、".join(role_hits))
    if matched_required:
        why_fit.append("已确认技能命中: " + "、".join(matched_required))
    if matched_preferred:
        why_fit.append("已确认加分技能命中: " + "、".join(matched_preferred))
    if relevant_facts:
        why_fit.append(f"找到 {len(relevant_facts)} 条与 JD 相关的已确认经历事实。")

    why_not_fit: list[str] = []
    if missing_required:
        why_not_fit.append("未找到已确认技能证据: " + "、".join(missing_required))
    failing_gates = [gate.requirement for gate in hard_gates if gate.status == HardGateStatus.FAILS]
    if failing_gates:
        why_not_fit.append("存在未满足的硬性门槛: " + "；".join(failing_gates))

    advantages = [f"[{fact.id}] {fact.statement}" for fact in relevant_facts[:5]]
    gaps = [f"缺少 {skill} 的已确认证据或能力" for skill in missing_required]
    unknowns = [
        gate.requirement for gate in hard_gates if gate.status == HardGateStatus.UNKNOWN
    ]
    if not job.required_skills and not job.preferred_skills:
        unknowns.append("本地规则未识别出明确技能关键词，建议使用 OpenAI 引擎复核。")

    return MatchResult(
        job=job,
        score_breakdown=breakdown,
        overall_score=score,
        recommendation=recommendation_for_score(score),
        why_fit=why_fit,
        why_not_fit=why_not_fit,
        advantages=advantages,
        gaps=gaps,
        hard_gates=hard_gates,
        evidence=evidence,
        unknowns=unknowns,
        engine="local-baseline",
    )
