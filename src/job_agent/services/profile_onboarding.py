from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from job_agent.models.profile import (
    Availability,
    ClaimStatus,
    CommutePreferences,
    Education,
    EvidenceFact,
    Experience,
    ExperienceKind,
    Skill,
    SkillLevel,
    SourceDocument,
    empty_profile,
)
from job_agent.services.profile_store import load_profile, save_profile
from job_agent.services.resume_reader import read_resume


class ProfileOnboardingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProfileOnboardingInput:
    resume_filename: str
    resume_bytes: bytes
    mode: str = "update"
    display_name: str = ""
    email: str = ""
    phone: str = ""
    stage: str = ""
    target_roles: list[str] = field(default_factory=list)
    adjacent_roles: list[str] = field(default_factory=list)
    target_industries: list[str] = field(default_factory=list)
    preferred_locations: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    must_haves: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    earliest_start: str | None = None
    days_per_week: int | None = None
    duration_months: int | None = None
    commute_origin: str = ""
    max_commute_minutes: int | None = None
    transport_modes: list[str] = field(default_factory=list)
    remote_acceptable: bool = False
    school: str = ""
    degree: str = ""
    major: str = ""
    graduation: str | None = None
    notes: str = ""
    confirm_truth: bool = False
    confirm_replace: bool = False


@dataclass(frozen=True)
class ProfileOnboardingResult:
    profile_path: Path
    resume_path: Path | None
    source_id: str
    extracted_characters: int
    imported_facts: int
    imported_skills: int
    warnings: list[str]
    replaced_profile: bool


_SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}
_HEADINGS = {
    "个人简历",
    "简历",
    "教育经历",
    "教育背景",
    "实习经历",
    "工作经历",
    "项目经历",
    "校园经历",
    "社团经历",
    "技能",
    "专业技能",
    "个人技能",
    "证书",
    "荣誉奖项",
    "自我评价",
    "个人优势",
    "联系方式",
}

# Imported skills start conservatively at BASIC. A later user review may raise
# the level, but the importer never turns a keyword mention into "proficient".
_SKILL_CATALOG: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("excel", "Excel", "data_tool", ("excel", "数据透视表", "vlookup", "xlookup")),
    ("sql", "SQL", "data_tool", ("sql", "sql server", "mysql", "postgresql")),
    ("python", "Python", "programming", ("python",)),
    ("pandas", "Pandas", "programming", ("pandas",)),
    ("numpy", "NumPy", "programming", ("numpy",)),
    ("powerbi", "Power BI", "data_tool", ("power bi", "powerbi")),
    ("tableau", "Tableau", "data_tool", ("tableau",)),
    ("ppt", "PowerPoint", "office", ("powerpoint", "ppt")),
    ("word", "Word", "office", ("microsoft word", "word")),
    ("figma", "Figma", "product", ("figma",)),
    ("axure", "Axure", "product", ("axure",)),
    ("ps", "Photoshop", "design", ("photoshop",)),
    ("ai-tools", "AI工具", "ai", ("chatgpt", "deepseek", "gemini", "大模型", "aigc", "ai agent", "ai工具")),
    ("data-analysis", "数据分析", "analysis", ("数据分析", "数据清洗", "数据可视化", "统计分析")),
    ("new-media", "新媒体运营", "operations", ("新媒体运营", "公众号运营", "小红书运营", "抖音运营")),
    ("user-operations", "用户运营", "operations", ("用户运营", "社群运营", "用户增长", "用户留存")),
    ("product-operations", "产品运营", "operations", ("产品运营", "需求分析", "竞品分析", "产品迭代")),
    ("english", "英语", "language", ("cet-4", "cet4", "cet-6", "cet6", "英语六级", "英语四级", "雅思", "托福")),
)


def _safe_text(value: str, *, limit: int) -> str:
    cleaned = unicodedata.normalize("NFKC", value or "").strip()
    if len(cleaned) > limit:
        raise ProfileOnboardingError(f"输入内容过长，最多允许 {limit} 个字符。")
    return cleaned


def _safe_filename(filename: str) -> tuple[str, str]:
    name = Path(filename or "resume").name
    suffix = Path(name).suffix.casefold()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ProfileOnboardingError("简历仅支持 PDF、DOCX、TXT 或 MD。")
    stem = unicodedata.normalize("NFKC", Path(name).stem)
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", stem).strip("-._")
    return (stem[:48] or "resume"), suffix


def _contains_contact(line: str) -> bool:
    if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", line):
        return True
    digits = re.sub(r"\D", "", line)
    if len(digits) >= 11 and re.search(r"1[3-9]\d{9}", digits):
        return True
    return "http://" in line.casefold() or "https://" in line.casefold()


def _fact_lines(text: str, *, display_name: str) -> list[str]:
    facts: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = unicodedata.normalize("NFKC", raw_line).strip()
        line = re.sub(r"^[\s•·●▪◦*\-—–]+", "", line).strip()
        line = re.sub(r"\s+", " ", line)
        compact = line.replace(" ", "")
        if not line or len(line) < 6 or len(line) > 280:
            continue
        if compact in _HEADINGS or compact.rstrip(":：") in _HEADINGS:
            continue
        if display_name and compact == display_name.replace(" ", ""):
            continue
        if _contains_contact(line):
            continue
        if re.fullmatch(r"[\d\s./年月日—–-]+", line):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        facts.append(line)
        if len(facts) >= 80:
            break
    return facts


def _matched_skill_ids(text: str) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    lowered = unicodedata.normalize("NFKC", text).casefold()
    matches: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for slug, name, category, aliases in _SKILL_CATALOG:
        if any(alias.casefold() in lowered for alias in aliases):
            matches[slug] = (name, category, aliases)
    return matches


def _fact_skill_names(line: str) -> list[str]:
    matches = _matched_skill_ids(line)
    return [item[0] for item in matches.values()]


def _write_uploaded_resume(private_dir: Path, filename: str, payload: bytes) -> Path:
    stem, suffix = _safe_filename(filename)
    digest = hashlib.sha256(payload).hexdigest()
    directory = private_dir / "resumes"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest[:16]}-{stem}{suffix}"
    if destination.exists():
        if destination.read_bytes() != payload:
            raise ProfileOnboardingError("同名简历文件已存在且内容不同。")
        return destination.resolve()
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return destination.resolve()


def _portable_resume_path(_private_dir: Path, resume_path: Path) -> str:
    """Keep bundled resume references valid after moving the app data folder."""
    # `_write_uploaded_resume` always owns the destination beneath
    # `private/resumes`. Keep only that application-relative portion because
    # Windows Store/AppContainer path redirection can make two equivalent
    # absolute paths look unrelated to `Path.relative_to`.
    return (Path("resumes") / resume_path.name).as_posix()


def onboard_profile(
    request: ProfileOnboardingInput,
    *,
    profile_path: Path,
    private_dir: Path,
) -> ProfileOnboardingResult:
    if request.mode not in {"update", "replace"}:
        raise ProfileOnboardingError("资料模式必须是更新当前资料或切换为新用户。")
    has_uploaded_resume = bool(request.resume_bytes)
    if request.resume_filename and not has_uploaded_resume:
        raise ProfileOnboardingError("选择的简历文件为空。")
    if has_uploaded_resume and not request.confirm_truth:
        raise ProfileOnboardingError("请先确认简历内容真实，并授权仅用于本地求职。")
    if request.mode == "replace" and not request.confirm_replace:
        raise ProfileOnboardingError("切换新用户前必须明确确认备份并切换当前资料。")
    if not has_uploaded_resume and (
        request.mode == "replace" or not profile_path.is_file()
    ):
        raise ProfileOnboardingError("首次建档或切换新用户时必须选择一份简历文件。")
    if request.mode == "replace" and not request.target_roles:
        raise ProfileOnboardingError("新用户至少需要填写一个目标岗位。")

    display_name = _safe_text(request.display_name, limit=80)
    email = _safe_text(request.email, limit=160)
    phone = _safe_text(request.phone, limit=40)
    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ProfileOnboardingError("邮箱格式不正确。")
    if phone and len(re.sub(r"\D", "", phone)) < 7:
        raise ProfileOnboardingError("手机号或联系电话格式不正确。")

    if request.mode == "replace" or not profile_path.is_file():
        profile = empty_profile()
    else:
        profile = load_profile(profile_path)

    resume_path: Path | None = None
    document = None
    source_id = ""
    already_imported = True
    if has_uploaded_resume:
        resume_path = _write_uploaded_resume(
            private_dir,
            request.resume_filename,
            request.resume_bytes,
        )
        document = read_resume(resume_path)
        source_id = f"resume-{document.sha256[:12]}"
        extracted_path = private_dir / f"{source_id}.txt"
        extracted_path.parent.mkdir(parents=True, exist_ok=True)
        expected_text = document.text + "\n"
        if (
            extracted_path.exists()
            and extracted_path.read_text(encoding="utf-8") != expected_text
        ):
            raise ProfileOnboardingError("该简历的本地文字副本存在内容冲突。")
        if not extracted_path.exists():
            temporary = extracted_path.with_suffix(".txt.tmp")
            temporary.write_text(expected_text, encoding="utf-8")
            temporary.replace(extracted_path)
        already_imported = any(
            source.id == source_id for source in profile.source_documents
        )

    if display_name:
        profile.person.display_name = display_name
    if email:
        profile.person.contact.email = email
    if phone:
        profile.person.contact.phone = phone

    preferences = profile.job_search
    if request.stage:
        preferences.stage = _safe_text(request.stage, limit=160)
    for field_name, values in (
        ("target_roles", request.target_roles),
        ("adjacent_roles", request.adjacent_roles),
        ("target_industries", request.target_industries),
        ("preferred_locations", request.preferred_locations),
        ("employment_types", request.employment_types),
        ("must_haves", request.must_haves),
        ("avoid", request.avoid),
    ):
        cleaned = list(dict.fromkeys(_safe_text(item, limit=80) for item in values if item.strip()))
        if cleaned or request.mode == "replace":
            setattr(preferences, field_name, cleaned)
    if any(
        value is not None
        for value in (request.earliest_start, request.days_per_week, request.duration_months)
    ):
        preferences.availability = Availability(
            earliest_start=request.earliest_start,
            days_per_week=request.days_per_week,
            max_days_per_week=request.days_per_week,
            duration_months=request.duration_months,
            max_duration_months=None,
            notes="通过本地资料中心由用户本人填写。",
        )
    if (
        request.commute_origin
        or request.max_commute_minutes is not None
        or request.transport_modes
        or request.remote_acceptable
        or request.mode == "replace"
    ):
        preferences.commute = CommutePreferences(
            origin=_safe_text(request.commute_origin, limit=160),
            max_one_way_minutes=request.max_commute_minutes,
            transport_modes=list(
                dict.fromkeys(
                    _safe_text(item, limit=40)
                    for item in request.transport_modes
                    if item.strip()
                )
            ),
            remote_acceptable=request.remote_acceptable,
            notes="通勤时间由用户本人填写或确认；不保存精确家庭住址。",
        )
    if request.notes:
        preferences.notes = _safe_text(request.notes, limit=600)

    if document is not None and resume_path is not None:
        stored_resume_path = _portable_resume_path(private_dir, resume_path)
        existing_source = next(
            (source for source in profile.source_documents if source.id == source_id),
            None,
        )
        if existing_source is None:
            profile.source_documents.append(
                SourceDocument(
                    id=source_id,
                    kind="resume",
                    original_path=stored_resume_path,
                    sha256=document.sha256,
                    notes="用户已确认该简历内容真实，并授权仅在本地用于求职。",
                )
            )
        else:
            # A profile imported before the desktop edition may still point to
            # an external download folder. Re-uploading the same resume should
            # make the profile self-contained without duplicating its facts.
            existing_source.original_path = stored_resume_path
            existing_source.sha256 = document.sha256

    if document is not None and (request.school or request.major or request.degree):
        education_id = f"edu-{document.sha256[:12]}"
        education = Education(
            id=education_id,
            institution=_safe_text(request.school, limit=160) or "未填写学校",
            degree=_safe_text(request.degree, limit=80),
            major=_safe_text(request.major, limit=120),
            end=request.graduation,
            status=ClaimStatus.USER_CONFIRMED,
            source_ids=[source_id],
        )
        existing_index = next(
            (index for index, item in enumerate(profile.education) if item.id == education_id),
            None,
        )
        if existing_index is None:
            profile.education.append(education)
        else:
            profile.education[existing_index] = education

    imported_facts = 0
    imported_skills = 0
    if document is not None and not already_imported:
        lines = _fact_lines(document.text, display_name=display_name)
        facts = [
            EvidenceFact(
                id=f"fact-resume-{document.sha256[:12]}-{index:03d}",
                statement=line,
                skills=_fact_skill_names(line),
                status=ClaimStatus.USER_CONFIRMED,
                source_ids=[source_id],
                interview_notes="由用户确认过的简历原文导入；后续可进一步拆成 STAR 事实。",
            )
            for index, line in enumerate(lines, start=1)
        ]
        if facts:
            profile.experiences.append(
                Experience(
                    id=f"exp-resume-{document.sha256[:12]}",
                    kind=ExperienceKind.OTHER,
                    role="简历原文事实导入",
                    summary="从用户本人确认的简历原文提取，未补写或虚构内容。",
                    facts=facts,
                )
            )
        imported_facts = len(facts)

        facts_by_skill: dict[str, list[str]] = {}
        for fact in facts:
            for skill_name in fact.skills:
                facts_by_skill.setdefault(skill_name, []).append(fact.id)
        existing_skill_names = {item.name.casefold() for item in profile.skills}
        for slug, (name, category, aliases) in _matched_skill_ids(document.text).items():
            if name.casefold() in existing_skill_names:
                continue
            profile.skills.append(
                Skill(
                    id=f"skill-{slug}-{document.sha256[:8]}",
                    name=name,
                    category=category,
                    level=SkillLevel.BASIC,
                    aliases=list(aliases),
                    evidence_fact_ids=facts_by_skill.get(name, []),
                    source_ids=[source_id],
                    status=ClaimStatus.USER_CONFIRMED,
                )
            )
            existing_skill_names.add(name.casefold())
            imported_skills += 1

    save_profile(
        profile,
        profile_path,
        overwrite=profile_path.is_file(),
        create_backup=profile_path.is_file(),
    )
    return ProfileOnboardingResult(
        profile_path=profile_path.resolve(),
        resume_path=resume_path,
        source_id=source_id,
        extracted_characters=len(document.text) if document is not None else 0,
        imported_facts=imported_facts,
        imported_skills=imported_skills,
        warnings=document.warnings if document is not None else [],
        replaced_profile=request.mode == "replace",
    )
