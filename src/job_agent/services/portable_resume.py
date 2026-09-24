from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from job_agent.models.job import MatchResult, MatchStatus
from job_agent.models.job_record import JobDetail
from job_agent.models.profile import EvidenceFact, Experience, ExperienceKind, Profile
from job_agent.services.resume_render import format_resume_target, resume_section_title


class PortableResumeError(RuntimeError):
    pass


@dataclass(frozen=True)
class PortableResumeFiles:
    directory: Path
    content_json: Path
    docx: Path
    pdf: Path
    manifest: Path


_FACT_ID_PATTERN = re.compile(r"\[([a-zA-Z0-9][a-zA-Z0-9_.-]*)]")
_WORK_KINDS = {ExperienceKind.INTERNSHIP, ExperienceKind.EMPLOYMENT}
_PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png"}
_MAX_PHOTO_BYTES = 5 * 1024 * 1024
_PROFILE_PHOTO_NAMES = ("profile-photo.jpg", "profile-photo.jpeg", "profile-photo.png")
# 查找目录优先级：photo/ 为约定落点，assets/ 为历史/手动落点兼容
_PROFILE_PHOTO_DIRS = ("photo", "assets")


def resume_fact_review_sources(document: object) -> set[str]:
    """Return review provenance independently of the last rendering engine.

    Old cloud drafts only recorded ``generation.engine``. Keep that as a
    conservative compatibility signal while new versions persist provenance
    separately so a local edit cannot silently clear it.
    """
    if not isinstance(document, dict):
        return set()
    origin = document.get("fact_review_origin")
    sources: set[str] = set()
    if isinstance(origin, dict) and origin.get("required") is True:
        raw_sources = origin.get("sources")
        if isinstance(raw_sources, list):
            sources.update(source for source in raw_sources if isinstance(source, str) and source)
        if not sources:
            sources.add("inherited_cloud_content")
    generation = document.get("generation")
    if isinstance(generation, dict) and generation.get("engine") == "cloud_composition":
        sources.add("cloud_composition")
    return sources


def resume_fact_review_required(document: object) -> bool:
    return bool(resume_fact_review_sources(document))


def inherit_resume_fact_review(
    content: dict, *upstream: object, source: str = "",
    source_resume_text: str = "", sources: set[str] | None = None,
) -> None:
    """Carry any cloud-authored claims through later local processing."""
    inherited_sources = resume_fact_review_sources(content)
    for item in upstream:
        inherited_sources.update(resume_fact_review_sources(item))
    inherited_sources.update(sources or ())
    if source:
        inherited_sources.add(source)
    if not inherited_sources:
        return
    origin: dict[str, object] = {"required": True, "sources": sorted(inherited_sources)}
    for item in (content, *upstream):
        if isinstance(item, dict) and isinstance(item.get("fact_review_origin"), dict):
            text = item["fact_review_origin"].get("source_resume_text")
            if isinstance(text, str) and text.strip():
                origin["source_resume_text"] = text
                break
    if source_resume_text.strip():
        origin["source_resume_text"] = source_resume_text.strip()
    content["fact_review_origin"] = origin


def verified_resume_content_sha256(manifest_path: Path, manifest: dict) -> str:
    """Bind fact approval to the exact content JSON shown for the PDF."""
    name = manifest.get("content_source")
    expected = str(manifest.get("content_sha256") or "").upper()
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise PortableResumeError("简历内容文件缺失或路径无效，请重新生成草稿。")
    content_path = (manifest_path.parent / name).resolve()
    if (
        content_path.parent != manifest_path.parent.resolve()
        or not content_path.is_file()
        or not re.fullmatch(r"[0-9A-F]{64}", expected)
        or _sha256(content_path) != expected
    ):
        raise PortableResumeError("简历内容摘要缺失或已变化，请重新生成草稿。")
    return expected


def find_profile_photo(private_dir: Path) -> Path | None:
    """在约定位置查找证件照（photo/ 优先，assets/ 兜底）。"""
    base = Path(private_dir).expanduser()
    for subdir in _PROFILE_PHOTO_DIRS:
        for name in _PROFILE_PHOTO_NAMES:
            candidate = base / subdir / name
            if candidate.is_file():
                return candidate
    return None


def save_profile_photo(private_dir: Path, data: bytes) -> Path:
    """保存证件照到约定位置 private_dir/photo/，按魔数识别格式。"""
    if len(data) > _MAX_PHOTO_BYTES:
        raise PortableResumeError("简历照片超过 5MB，请先压缩。")
    if not data:
        raise PortableResumeError("照片内容为空。")
    if data[:3] == b"\xff\xd8\xff":
        extension = ".jpg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        extension = ".png"
    else:
        raise PortableResumeError("照片必须是 JPG 或 PNG 格式。")
    photo_dir = Path(private_dir).expanduser() / "photo"
    photo_dir.mkdir(parents=True, exist_ok=True)
    for name in _PROFILE_PHOTO_NAMES:
        candidate = photo_dir / name
        if candidate.suffix.casefold() != extension and candidate.exists():
            candidate.unlink()
    target = photo_dir / f"profile-photo{extension}"
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    # 已通过临时文件 replace 保证原子性
    temporary.replace(target)
    return target


def _resolve_photo(content: dict[str, object]) -> Path | None:
    person = content.get("person")
    raw = str(dict(person).get("photo_path") or "").strip() if person else ""
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.suffix.casefold() not in _PHOTO_SUFFIXES:
        raise PortableResumeError(
            f"简历照片仅支持 JPG/PNG，收到: {path.suffix or '无扩展名'}"
        )
    if not path.is_file():
        raise PortableResumeError(f"简历照片文件不存在: {path}")
    if path.stat().st_size > _MAX_PHOTO_BYTES:
        raise PortableResumeError("简历照片超过 5MB，请先压缩。")
    return path.resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _ready_fact_index(profile: Profile) -> dict[str, tuple[Experience, EvidenceFact]]:
    return {
        fact.id: (experience, fact)
        for experience in profile.experiences
        for fact in experience.facts
        if profile.is_application_ready(fact.status)
    }


def _is_imported_experience(experience: Experience) -> bool:
    return experience.role == "简历原文事实导入"


def _is_imported_metadata(experience: Experience, fact: EvidenceFact, profile: Profile) -> bool:
    """Omit only pure lists already represented by confirmed skills/preferences.

    A heading is not proof that its entire paragraph is metadata: qualifications,
    proficiency and real experience under a skills heading must remain verbatim.
    """
    if not _is_imported_experience(experience):
        return False
    known_skills = {name.strip().casefold() for name in [skill.name for skill in profile.skills
                    if profile.is_application_ready(skill.status)][:12]}
    known_roles = {role.strip().casefold() for role in profile.job_search.target_roles}
    known_locations = {location.strip().casefold() for location in profile.job_search.preferred_locations}
    parts = [part.strip() for part in re.split(r"[。；;\n]", fact.statement) if part.strip()]
    if not parts:
        return False
    for part in parts:
        match = re.fullmatch(r"([^:：]+)[:：]\s*(.+)", part)
        if not match:
            return False
        label, value = match.groups()
        if label.strip() in {"技能", "专业技能", "技术技能", "核心技能"}:
            known = known_skills
        elif label.strip() in {"求职意向", "求职目标", "目标岗位"}:
            known = known_roles
        elif label.strip() in {"期望地点", "期望城市"}:
            known = known_locations
        else:
            return False
        values = {item.strip().casefold() for item in re.split(r"[、,，|/]", value) if item.strip()}
        if not values or not values.issubset(known):
            return False
    return True


def _ranked_facts(
    profile: Profile,
    result: MatchResult,
) -> list[tuple[Experience, EvidenceFact]]:
    index = _ready_fact_index(profile)
    ranked_ids: list[str] = []
    for advantage in result.advantages:
        ranked_ids.extend(_FACT_ID_PATTERN.findall(advantage))
    ranked_ids.extend(
        fact_id
        for evidence in result.evidence
        if evidence.status in {MatchStatus.MATCHED, MatchStatus.PARTIAL}
        for fact_id in evidence.profile_fact_ids
    )
    for experience in profile.experiences:
        for fact in experience.facts:
            if profile.is_application_ready(fact.status):
                ranked_ids.append(fact.id)
    seen: set[str] = set()
    ranked: list[tuple[Experience, EvidenceFact]] = []
    for fact_id in ranked_ids:
        if fact_id in seen or fact_id not in index:
            continue
        seen.add(fact_id)
        ranked.append(index[fact_id])
    return ranked


def _selected_experiences(
    profile: Profile,
    result: MatchResult,
    jd_text: str = "",
) -> list[tuple[Experience, list[EvidenceFact]]]:
    grouped: dict[str, tuple[Experience, list[EvidenceFact]]] = {}
    ranked = _ranked_facts(profile, result)
    ranked = [item for item in ranked if not _is_imported_metadata(*item, profile)]
    # Certificates are rendered under skills, never as a job or a project.
    ranked = [item for item in ranked if item[0].kind != ExperienceKind.OTHER or _is_imported_experience(item[0])] or ranked
    jd_signal = jd_text.casefold()
    concepts = (
            "excel", "vlookup", "数据透视表", "sql", "python", "数据", "统计",
            "分析", "清洗", "可视化", "报表", "日报", "周报", "运营", "业务",
            "调研", "问卷", "报告", "研究", "招聘", "人才", "hr", "员工", "档案", "沟通", "活动",
            "内容", "文案", "公众号", "新媒体", "用户", "社群", "产品", "需求", "测试", "自动化", "ai",
    )

    def relevance(fact: EvidenceFact) -> int:
        fact_signal = " ".join([fact.statement, *fact.skills, *fact.tools]).casefold()
        return sum(1 for concept in concepts if concept in jd_signal and concept in fact_signal)

    for experience, fact in ranked:
        grouped.setdefault(experience.id, (experience, []))[1].append(fact)
    values = list(grouped.values())
    # Relevance determines order, not whether a whole verified experience exists.
    values.sort(
        key=lambda item: (
            item[0].kind not in _WORK_KINDS,
            -max(relevance(fact) for fact in item[1]),
            -sum(sorted((relevance(fact) for fact in item[1]), reverse=True)[:2]),
        )
    )
    selected = []
    budget = 10
    candidates = values[:5]
    for position, (experience, facts) in enumerate(candidates):
        reserved = len(candidates) - position - 1
        limit = min(4 if experience.kind in _WORK_KINDS or _is_imported_experience(experience) else 2, budget - reserved)
        ordered = []
        seen_text = set()
        for fact in sorted(facts, key=lambda fact: -relevance(fact)):
            key = re.sub(r"\s+", "", fact.statement)
            if key not in seen_text:
                ordered.append(fact)
                seen_text.add(key)
        picked = ordered[:limit]
        outcomes = [fact for fact in ordered if fact.metrics or any(term in fact.statement for term in ("获得", "成绩", "认可"))]
        # Preserve a real outcome beside the strongest action; unrelated awards do not win selection.
        if len(picked) > 1 and outcomes and not any(fact in outcomes for fact in picked):
            picked[-1] = outcomes[0]
        selected.append((experience, picked))
        budget -= len(picked)
        if budget <= 0:
            break
    if not selected or not any(facts for _, facts in selected):
        raise PortableResumeError("Profile 中没有可用于简历草稿的已确认事实。")
    return selected


def _date_range(experience: Experience) -> str:
    start = (experience.start or "").replace("-", ".")
    end = (experience.end or "").replace("-", ".")
    return " - ".join(value for value in (start, end) if value)


def _bullet_label(experience: Experience, fact: EvidenceFact) -> str:
    """Map truthful facts into the reference template's content/result groups."""
    prefix = "工作" if experience.kind in _WORK_KINDS else "项目"
    outcome_terms = (
        "提升",
        "降低",
        "增长",
        "节省",
        "保障",
        "提供支持",
        "获得",
        "准确",
        "结果",
        "成果",
        "成绩",
    )
    has_outcome = bool(fact.metrics) or any(term in fact.statement for term in outcome_terms)
    return f"{prefix}{'业绩' if has_outcome else '内容'}"


def build_portable_resume_content(
    profile: Profile,
    job: JobDetail,
    result: MatchResult,
    *,
    generated_at: datetime | None = None,
    photo_path: Path | None = None,
) -> dict[str, object]:
    generated_at = generated_at or datetime.now(UTC)
    name = profile.person.display_name or profile.person.legal_name
    if not name:
        raise PortableResumeError("Profile 缺少姓名，无法生成简历草稿。")
    resolved_photo: Path | None = None
    if photo_path is not None:
        resolved_photo = Path(photo_path).expanduser().resolve()
    selected = _selected_experiences(profile, result, job.jd_text)
    skills = [
        skill.name
        for skill in profile.skills
        if profile.is_application_ready(skill.status)
    ][:12]
    education = [
        item.model_dump(mode="json", exclude={"source_ids", "status"})
        for item in profile.education
        if profile.is_application_ready(item.status)
    ][:2]
    # Local selection has no independent summary: copying selected bullets here
    # repeats the same claims. The target, skills and evidence have their own sections.
    summary = ""

    work_entries: list[dict[str, object]] = []
    internship_entries: list[dict[str, object]] = []
    project_entries: list[dict[str, object]] = []
    other_entries: list[dict[str, object]] = []
    imported_entries: list[dict[str, object]] = []
    for experience, facts in selected:
        entry = {
            "experience_id": experience.id,
            "experience_kind": experience.kind.value,
            "organization": experience.organization or "",
            "role": "" if _is_imported_experience(experience) else experience.role,
            "dates": _date_range(experience),
            "context": experience.summary.split("。")[0] if "派遣" in experience.summary else "",
            "bullets": [
                {
                    "label": "" if _is_imported_experience(experience) else _bullet_label(experience, fact),
                    "text": fact.statement,
                    "fact_ids": [fact.id],
                }
                for fact in facts
            ],
        }
        if _is_imported_experience(experience):
            imported_entries.append(entry)
        elif experience.kind == ExperienceKind.INTERNSHIP:
            internship_entries.append(entry)
        elif experience.kind == ExperienceKind.EMPLOYMENT:
            work_entries.append(entry)
        elif experience.kind == ExperienceKind.OTHER:
            other_entries.append(entry)
        else:
            project_entries.append(entry)
    sections = []
    if internship_entries:
        sections.append({"title": "实习经历", "entries": internship_entries})
    if work_entries:
        sections.append({"title": "工作经历", "entries": work_entries})
    if project_entries:
        sections.append({"title": "项目经历", "entries": project_entries})
    if other_entries:
        sections.append({"title": "其他经历", "entries": other_entries})
    if imported_entries:
        sections.append({"title": "相关经历", "entries": imported_entries})

    rendered_facts = [fact for _, facts in selected for fact in facts]
    shown_text = {re.sub(r"\s+", "", fact.statement) for fact in rendered_facts}
    highlights = []
    for experience in profile.experiences:
        if experience.kind != ExperienceKind.OTHER or _is_imported_experience(experience):
            continue
        for fact in experience.facts:
            key = re.sub(r"\s+", "", fact.statement)
            if profile.is_application_ready(fact.status) and key not in shown_text and len(highlights) < 3:
                highlights.append({"text": fact.statement, "fact_ids": [fact.id]})
                rendered_facts.append(fact)
                shown_text.add(key)
    rendered_ids = {fact.id for fact in rendered_facts}

    contact = profile.person.contact
    return {
        "schema_version": "1.0",
        "template_id": "reference-a4",
        "resume_version_id": (
            f"resume-{generated_at.strftime('%Y%m%dT%H%M%SZ')}-job-{job.job_id}-portable-v1"
        ),
        "person": {
            "name": name,
            "email": contact.email,
            "phone": contact.phone,
            "city": profile.person.current_city,
            "linkedin": contact.linkedin,
            "website": contact.website,
            "photo_path": str(resolved_photo) if resolved_photo else "",
        },
        "target": {
            "job_id": job.job_id,
            "company": job.company,
            "role": job.title,
            "location": job.location,
            "generated_at": generated_at.isoformat(),
        },
        "summary": summary,
        # Leave optional authored sections empty until distinct content is supplied.
        "self_evaluation": "",
        "skills": skills,
        "availability": profile.job_search.availability.model_dump(mode="json"),
        "highlights": highlights,
        "experience_sections": sections,
        "selection": {
            "protected_experience_ids": [experience.id for experience, _ in selected],
            "omitted_experience_ids": [e.id for e in profile.experiences
                if e.kind != ExperienceKind.OTHER and any(profile.is_application_ready(f.status) for f in e.facts)
                and e.id not in {selected_e.id for selected_e, _ in selected}],
            "policy": "按JD排序；最多五段、十条事实，先保留经历覆盖，再分配细节。",
            "omitted_fact_ids": [fact.id for experience in profile.experiences for fact in experience.facts
                if profile.is_application_ready(fact.status) and fact.id not in rendered_ids],
            "metadata_fact_ids": [fact.id for experience in profile.experiences for fact in experience.facts
                if profile.is_application_ready(fact.status) and _is_imported_metadata(experience, fact, profile)],
        },
        "education": education,
        "truthfulness": {
            "confirmed_fact_ids": [
                fact.id for fact in rendered_facts
            ],
            "excluded_gaps": result.gaps,
            "excluded_unknowns": result.unknowns,
            "rule": "只使用 documented 或 user_confirmed 的事实。",
        },
    }


def _set_docx_font(run: object, *, size: int = 9, bold: bool = False) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    run.font.name = "Microsoft YaHei"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    run.font.size = Pt(size)
    run.bold = bold


def _add_docx_paragraph(document: object, text: str, *, size: int = 9, bold: bool = False) -> object:
    from docx.shared import Pt

    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(2)
    run = paragraph.add_run(text)
    _set_docx_font(run, size=size, bold=bold)
    return paragraph


def _write_docx(content: dict[str, object], path: Path, photo: Path | None) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Mm

    document = Document()
    section = document.sections[0]
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.top_margin = Mm(11)
    section.bottom_margin = Mm(11)
    section.left_margin = Mm(14)
    section.right_margin = Mm(14)
    person = dict(content["person"])
    name = str(person.get("name") or "")
    contacts = [
        str(value)
        for value in (
            person.get("phone"),
            person.get("email"),
            person.get("city"),
            person.get("linkedin"),
            person.get("website"),
        )
        if value
    ]

    def _append_name_heading(paragraph: object) -> None:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_after = Mm(1.5)
        title_run = paragraph.add_run(name)
        _set_docx_font(title_run, size=18, bold=True)

    def _append_contact_line(paragraph: object) -> None:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_after = Mm(2)
        contact_run = paragraph.add_run("  |  ".join(contacts))
        _set_docx_font(contact_run, size=8)

    if photo is None:
        _append_name_heading(document.add_paragraph())
        _append_contact_line(document.add_paragraph())
    else:
        from docx.enum.table import WD_ALIGN_VERTICAL

        table = document.add_table(rows=1, cols=2)
        table.autofit = False
        left_cell = table.cell(0, 0)
        right_cell = table.cell(0, 1)
        left_cell.width = Mm(146)
        right_cell.width = Mm(36)
        _append_name_heading(left_cell.paragraphs[0])
        _append_contact_line(left_cell.add_paragraph())
        right_cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        photo_paragraph = right_cell.paragraphs[0]
        photo_paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        photo_paragraph.paragraph_format.space_after = Mm(0)
        photo_run = photo_paragraph.add_run()
        photo_run.add_picture(str(photo), width=Mm(26))

    target = dict(content["target"])
    # match_score 属内部排序数据，不写入对外投递的简历
    _add_docx_paragraph(
        document,
        format_resume_target(target),
        size=10,
        bold=True,
    )
    _add_docx_paragraph(document, str(content.get("summary") or ""), size=8)
    skills = list(content.get("skills", []))
    if skills:
        _add_docx_paragraph(document, "核心能力", size=10, bold=True)
        _add_docx_paragraph(document, " · ".join(str(item) for item in skills), size=8)

    for section_payload in list(content.get("experience_sections", [])):
        section_payload = dict(section_payload)
        _add_docx_paragraph(document, resume_section_title(section_payload), size=10, bold=True)
        for entry_payload in list(section_payload.get("entries", [])):
            entry_payload = dict(entry_payload)
            heading = " · ".join(
                value
                for value in (
                    str(entry_payload.get("organization") or ""),
                    str(entry_payload.get("role") or ""),
                    str(entry_payload.get("dates") or ""),
                )
                if value
            )
            _add_docx_paragraph(document, heading, size=9, bold=True)
            for bullet in list(entry_payload.get("bullets", [])):
                paragraph = document.add_paragraph(style="List Bullet")
                paragraph.paragraph_format.space_after = Mm(0.7)
                run = paragraph.add_run(str(dict(bullet).get("text") or ""))
                _set_docx_font(run, size=8)

    education = list(content.get("education", []))
    if education:
        _add_docx_paragraph(document, "教育经历", size=10, bold=True)
        for item in education:
            item = dict(item)
            label = " · ".join(
                value
                for value in (
                    str(item.get("institution") or ""),
                    str(item.get("major") or ""),
                    str(item.get("degree") or ""),
                    str(item.get("end") or ""),
                )
                if value
            )
            _add_docx_paragraph(document, label, size=8)
    evaluation = str(content.get("self_evaluation") or "").strip()
    if evaluation:
        _add_docx_paragraph(document, "自我评价", size=10, bold=True)
        _add_docx_paragraph(document, evaluation, size=8)
    document.save(path)


def _write_pdf(content: dict[str, object], path: Path, photo: Path | None) -> int:
    from pypdf import PdfReader
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from job_agent.services.pdf_fonts import resume_pdf_font
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font_name = resume_pdf_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ResumeTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=18,
        leading=21,
        alignment=TA_CENTER,
        spaceAfter=3 * mm,
        textColor=colors.HexColor("#16130f"),
    )
    meta_style = ParagraphStyle(
        "ResumeMeta",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=7.5,
        leading=10,
        alignment=TA_CENTER,
        spaceAfter=2 * mm,
        textColor=colors.HexColor("#4f4a43"),
    )
    heading_style = ParagraphStyle(
        "ResumeHeading",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=10,
        leading=12,
        spaceBefore=2.5 * mm,
        spaceAfter=1 * mm,
        textColor=colors.HexColor("#941638"),
    )
    body_style = ParagraphStyle(
        "ResumeBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=8,
        leading=10.5,
        spaceAfter=1.1 * mm,
        textColor=colors.HexColor("#16130f"),
    )
    bullet_style = ParagraphStyle(
        "ResumeBullet",
        parent=body_style,
        leftIndent=3 * mm,
        firstLineIndent=-2.4 * mm,
        bulletIndent=0,
        spaceAfter=0.7 * mm,
    )
    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=13 * mm,
        rightMargin=13 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=str(dict(content["person"]).get("name") or "简历草稿"),
        author="个人求职 Agent",
    )
    story: list[object] = []
    person = dict(content["person"])
    contacts = [
        str(value)
        for value in (
            person.get("phone"),
            person.get("email"),
            person.get("city"),
            person.get("linkedin"),
            person.get("website"),
        )
        if value
    ]
    if photo is None:
        story.append(Paragraph(html.escape(str(person.get("name") or "")), title_style))
        if contacts:
            story.append(Paragraph(html.escape("  |  ".join(contacts)), meta_style))
    else:
        from reportlab.lib.utils import ImageReader
        from reportlab.platypus import Image as RLImage

        reader = ImageReader(str(photo))
        image_width, image_height = reader.getSize()
        photo_width = 24 * mm
        photo_height = photo_width * image_height / image_width
        photo_flow = RLImage(str(photo), width=photo_width, height=photo_height)
        left_cell: list[object] = [
            Paragraph(html.escape(str(person.get("name") or "")), title_style)
        ]
        if contacts:
            left_cell.append(
                Paragraph(html.escape("  |  ".join(contacts)), meta_style)
            )
        header = Table(
            [[left_cell, photo_flow]],
            colWidths=[184 * mm - photo_width - 4 * mm, photo_width + 4 * mm],
        )
        header.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        story.append(header)
    def add_section_heading(text: str) -> None:
        story.append(Paragraph(html.escape(text), heading_style))
        rule = HRFlowable(width="100%", thickness=0.75, color=colors.black, spaceAfter=3)
        rule.keepWithNext = True
        story.append(rule)

    target = dict(content["target"])
    # match_score 属内部排序数据，不写入对外投递的简历
    add_section_heading(format_resume_target(target))
    story.append(Paragraph(html.escape(str(content.get("summary") or "")), body_style))
    skills = list(content.get("skills", []))
    if skills:
        add_section_heading("核心能力")
        story.append(Paragraph(html.escape(" · ".join(str(item) for item in skills)), body_style))
    for section_payload in list(content.get("experience_sections", [])):
        section_payload = dict(section_payload)
        add_section_heading(resume_section_title(section_payload))
        for entry_payload in list(section_payload.get("entries", [])):
            entry_payload = dict(entry_payload)
            heading = " · ".join(
                value
                for value in (
                    str(entry_payload.get("organization") or ""),
                    str(entry_payload.get("role") or ""),
                    str(entry_payload.get("dates") or ""),
                )
                if value
            )
            story.append(Paragraph(f"<b>{html.escape(heading)}</b>", body_style))
            for bullet in list(entry_payload.get("bullets", [])):
                text = html.escape(str(dict(bullet).get("text") or ""))
                story.append(Paragraph(f"• {text}", bullet_style))
    education = list(content.get("education", []))
    if education:
        add_section_heading("教育经历")
        for item in education:
            item = dict(item)
            label = " · ".join(
                value
                for value in (
                    str(item.get("institution") or ""),
                    str(item.get("major") or ""),
                    str(item.get("degree") or ""),
                    str(item.get("end") or ""),
                )
                if value
            )
            story.append(Paragraph(html.escape(label), body_style))
    evaluation = str(content.get("self_evaluation") or "").strip()
    if evaluation:
        add_section_heading("自我评价")
        story.append(Paragraph(html.escape(evaluation), body_style))
    story.append(Spacer(1, 1 * mm))
    story.append(
        Paragraph(
            "草稿状态：仅供本人审阅，未获批准前不得用于自动上传或投递。",
            meta_style,
        )
    )
    document.build(story)
    return len(PdfReader(str(path)).pages)


def _try_load_declarative_template(template_id: str) -> dict[str, object] | None:
    """模板 ID 命中 templates/ 目录声明式模板时返回 spec，否则 None（走 legacy）。"""
    if not template_id or template_id == "portable-evidence-resume-v1":
        return None
    try:
        from job_agent.services.resume_render import TemplateRenderError, load_template

        return load_template(template_id)
    except Exception:
        return None


def _write_pdf_from_docx(docx_path: Path, pdf_path: Path) -> int | None:
    """用本机 Word（docx2pdf/COM）把 DOCX 转为 PDF；环境不支持时返回 None。

    返回值为 PDF 页数；转换成功由调用方继续做单页硬校验。
    """
    import os
    import subprocess
    from pypdf import PdfReader
    # Native Office can retain COM/file locks after a timeout. Keep normal
    # exports deterministic and local; use Office only by explicit opt-in.
    if os.environ.get("JOB_AGENT_USE_OFFICE_PDF", "").strip().lower() not in {"1", "true"}:
        return None
    if os.name != "nt" or not shutil.which("pwsh"):
        return None
    # Isolate COM conversion with a deadline; never close the user's other documents.
    script = r'''
$ErrorActionPreference = 'Stop'
$app = $null; $doc = $null
try {
  try { $app = New-Object -ComObject KWPS.Application }
  catch { $app = New-Object -ComObject Word.Application }
  $alerts = $app.DisplayAlerts; $security = $app.AutomationSecurity
  $app.DisplayAlerts = 0; $app.AutomationSecurity = 3
  $doc = $app.Documents.Open($env:JOB_AGENT_EXPORT_DOCX, $false, $true, $false)
  $doc.Repaginate()
  $doc.ExportAsFixedFormat($env:JOB_AGENT_EXPORT_PDF, 17)
} finally {
  if ($null -ne $doc) { $doc.Close(0); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($doc) }
  if ($null -ne $app) {
    $app.DisplayAlerts = $alerts; $app.AutomationSecurity = $security
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
  }
}
'''
    try:
        subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                       env={**os.environ, "JOB_AGENT_EXPORT_DOCX": str(docx_path.resolve()), "JOB_AGENT_EXPORT_PDF": str(pdf_path.resolve())},
                       # Bound optional Office conversion; retain time for local fallback.
                       check=True, timeout=10, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return len(PdfReader(str(pdf_path)).pages)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _render_resume_version(
    content: dict[str, object],
    target_payload: dict[str, object],
    output_dir: Path,
    *,
    generated_at: datetime,
    extra_manifest: dict[str, object] | None = None,
    extra_qa: dict[str, object] | None = None,
) -> PortableResumeFiles:
    """把已构建好的 content 渲染为 DOCX/PDF 并写入版本目录与质检清单。"""
    # Ranking scores belong to the job dashboard, never to an application artifact.
    content = json.loads(json.dumps(content, ensure_ascii=False))
    inherit_resume_fact_review(content)
    target = dict(content.get("target") or {})
    target.pop("match_score", None)
    content["target"] = target
    target_payload = dict(target_payload)
    target_payload.pop("match_score", None)
    photo = _resolve_photo(content)
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise PortableResumeError(f"简历输出目录已存在，拒绝覆盖: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        content_path = temporary / "resume-content-portable.json"
        docx_path = temporary / "岗位专属简历草稿.docx"
        pdf_path = temporary / "岗位专属简历草稿.pdf"
        manifest_path = temporary / "resume-version-portable.json"
        # 已通过目录级 replace 保证原子性
        content_path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        spec = _try_load_declarative_template(str(content.get("template_id") or ""))
        pdf_engine = "local_renderer"
        if spec is not None:
            from job_agent.services.resume_render import render_docx

            render_docx(content, spec, docx_path, photo)
            pdf_pages = _write_pdf_from_docx(docx_path, pdf_path)
            pdf_engine = "office_docx_conversion"
            if pdf_pages is None:
                pdf_engine = "local_renderer"
                if spec["id"] == "reference-a4":
                    from job_agent.services.resume_reference_layout import render_reference_pdf
                    pdf_pages = render_reference_pdf(content, spec, pdf_path, photo)
                else:
                    pdf_pages = _write_pdf(content, pdf_path, photo)
        else:
            _write_docx(content, docx_path, photo)
            pdf_pages = _write_pdf(content, pdf_path, photo)
        if pdf_pages != 1 and spec and spec["id"] == "reference-a4":
            # One bounded spacing-only retry. Keep every fact and a readable 10.5 pt body.
            content["layout_density"] = "compact"
            render_docx(content, spec, docx_path, photo)
            pdf_pages = _write_pdf_from_docx(docx_path, pdf_path)
            pdf_engine = "office_docx_conversion"
            if pdf_pages is None:
                from job_agent.services.resume_reference_layout import render_reference_pdf
                pdf_pages = render_reference_pdf(content, spec, pdf_path, photo)
                pdf_engine = "local_renderer"
            # 已通过目录级 replace 保证原子性
            content_path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if pdf_pages != 1:
            raise PortableResumeError(
                f"简历草稿生成了 {pdf_pages} 页；为避免未经检查的分页，已拒绝标记为可审阅。"
            )
        if spec and spec["id"] == "reference-a4":
            from pypdf import PdfReader
            from job_agent.services.resume_reference_layout import body_blocks
            normalize = lambda text: re.sub(r"\s+|\u200b", "", str(text))
            extracted = normalize("".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages))
            expected = [text for _, text, _ in body_blocks(content) if text]
            if any(normalize(text) not in extracted for text in expected):
                raise PortableResumeError("PDF文本完整性检查未通过，请检查字体或转换器后重新生成。")
        fact_ids = list(
            dict(content.get("truthfulness") or {}).get("confirmed_fact_ids", [])
        )
        star_bullets = sum(
            1
            for section in list(content.get("experience_sections", []))
            for entry in list(dict(section).get("entries", []))
            for bullet in list(dict(entry).get("bullets", []))
            if str(dict(bullet).get("label") or "").endswith("业绩")
        )
        manifest: dict[str, object] = {
            "version_id": content["resume_version_id"],
            "created_at": generated_at.isoformat(),
            "template_id": content["template_id"],
            "content_source": content_path.name,
            "content_sha256": _sha256(content_path),
            "target": target_payload,
            "artifacts": {
                "docx": {"path": docx_path.name, "sha256": _sha256(docx_path)},
                "pdf": {"path": pdf_path.name, "sha256": _sha256(pdf_path)},
            },
            "qa": {
                "confirmed_fact_ids": fact_ids,
                "star_bullets": star_bullets,
                "embedded_photos": 1 if photo is not None else 0,
                "photo_requirement": "embedded" if photo is not None else "not_required",
                "docx_page_size": ("A4" if spec and spec["page"].get("size") == "A4" else "140x204mm"),
                "pdf_engine": pdf_engine,
                "docx_pdf_layout_equivalence": "office_converted" if pdf_engine == "office_docx_conversion" else "requires_visual_review",
                "pdf_pages": pdf_pages,
                "layout_density": content.get("layout_density", "standard"),
                "docx_structural_review": "passed",
                "pdf_text_integrity": "passed" if spec and spec["id"] == "reference-a4" else "not_checked",
                # A cloud response can cite confirmed fact IDs while inventing
                # a new achievement. Numeric checks do not establish truth.
                "truthfulness_check": (
                    "pending_user_review"
                    if resume_fact_review_required(content)
                    else "passed"
                ),
                "pdf_visual_review": "pending_user_review",
            },
            "generation": content.get("generation", {"engine": "local_selection", "review_required": True}),
        }
        if resume_fact_review_required(content):
            manifest["fact_review_origin"] = content["fact_review_origin"]
        if extra_manifest:
            manifest.update(extra_manifest)
        if extra_qa:
            manifest["qa"].update(extra_qa)
        # 已通过目录级 replace 保证原子性
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return PortableResumeFiles(
        directory=output_dir,
        content_json=output_dir / content_path.name,
        docx=output_dir / docx_path.name,
        pdf=output_dir / pdf_path.name,
        manifest=output_dir / manifest_path.name,
    )


def build_portable_resume_draft(
    profile: Profile,
    job: JobDetail,
    result: MatchResult,
    output_dir: Path,
    *,
    generated_at: datetime | None = None,
    photo_path: Path | None = None,
    content: dict[str, object] | None = None,
) -> PortableResumeFiles:
    generated_at = generated_at or datetime.now(UTC)
    if content is None:
        content = build_portable_resume_content(
            profile,
            job,
            result,
            generated_at=generated_at,
            photo_path=photo_path,
        )
    return _render_resume_version(
        content,
        {
            "job_id": job.job_id,
            "company": job.company,
            "title": job.title,
            "location": job.location,
        },
        output_dir,
        generated_at=generated_at,
    )


def find_latest_resume_manifest(
    applications_dir: Path,
    job_id: int,
    *,
    visual_status: str | None = None,
) -> Path | None:
    """Return the newest draft manifest, optionally limited to one QA state.

    Viewing and editing must follow chronology.  Application-pack resolution
    separately prefers an approved version, so an older approved PDF can never
    hide a newer draft that is waiting for the user's review.
    """

    candidates: list[tuple[float, Path]] = []
    if not applications_dir.is_dir():
        return None
    for path in applications_dir.rglob("resume-version*.json"):
        payload = read_resume_manifest(path)
        if payload is None:
            continue
        if payload["target"]["job_id"] != job_id:
            continue
        status = payload["qa"]["pdf_visual_review"]
        if visual_status is not None and status != visual_status:
            continue
        try:
            candidates.append((path.stat().st_mtime, path.resolve()))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], str(item[1])))[1]


def read_resume_manifest(path: Path, *, allow_legacy_without_job_id: bool = False) -> dict | None:
    """Read one usable manifest; quarantine malformed siblings during scans.

    The same schema check is used by latest-version lookup and hash download.
    A broken version in another job must never interrupt the current job.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    target = payload.get("target")
    qa = payload.get("qa")
    artifacts = payload.get("artifacts")
    if not isinstance(target, dict) or not isinstance(qa, dict) or not isinstance(artifacts, dict):
        return None
    raw_job_id = target.get("job_id")
    if raw_job_id is None and allow_legacy_without_job_id:
        normalized_job_id = None
    elif isinstance(raw_job_id, bool) or not (
        isinstance(raw_job_id, int) and raw_job_id > 0
        or isinstance(raw_job_id, str) and len(raw_job_id) <= 12
        and raw_job_id.isascii() and raw_job_id.isdecimal() and int(raw_job_id) > 0
    ):
        return None
    else:
        normalized_job_id = int(raw_job_id)
    visual_status = qa.get("pdf_visual_review")
    truth_status = qa.get("truthfulness_check")
    if not isinstance(visual_status, str) or visual_status not in {"pending_user_review", "passed"}:
        return None
    if not isinstance(truth_status, str) or truth_status not in {"pending_user_review", "passed"}:
        return None
    if any(not isinstance(artifacts.get(kind), dict) for kind in ("pdf", "docx")):
        return None
    target = dict(target)
    if normalized_job_id is not None:
        target["job_id"] = normalized_job_id
    return {**payload, "target": target}


def resume_manifest_fact_review_sources(manifest_path: Path, manifest: dict) -> set[str]:
    """Trace pre-upgrade edited versions that lack a persisted provenance flag.

    Older local revisions recorded only the most recent engine, but their
    ``based_on`` pointer still leads to the cloud-authored parent. An invalid
    or missing parent cannot prove the draft is local, so fail closed.
    """
    sources = resume_fact_review_sources(manifest)
    current_path = manifest_path.resolve()
    applications_root = current_path.parent.parent
    current = manifest
    visited = {current_path}
    job_id = dict(manifest.get("target") or {}).get("job_id")
    for _ in range(64):
        raw_parent = current.get("based_on")
        if not isinstance(raw_parent, str) or not raw_parent.strip():
            return sources
        parent = Path(raw_parent.strip())
        if not parent.is_absolute():
            parent = current_path.parent / parent
        parent = parent.resolve()
        if parent in visited or not parent.is_relative_to(applications_root):
            return sources | {"unknown_prior_version"}
        visited.add(parent)
        parent_manifest = read_resume_manifest(parent, allow_legacy_without_job_id=True)
        if parent_manifest is None:
            return sources | {"unknown_prior_version"}
        parent_job_id = dict(parent_manifest.get("target") or {}).get("job_id")
        if job_id is not None and parent_job_id not in {None, job_id}:
            return sources | {"unknown_prior_version"}
        sources.update(resume_fact_review_sources(parent_manifest))
        current, current_path = parent_manifest, parent
    return sources | {"unknown_prior_version"}


def resume_manifest_fact_review_required(manifest_path: Path, manifest: dict) -> bool:
    return bool(resume_manifest_fact_review_sources(manifest_path, manifest))


def resume_manifest_fact_approved(manifest_path: Path, manifest: dict) -> bool:
    """Require user approval for the actual PDF and content of this lineage."""
    if not resume_manifest_fact_review_required(manifest_path, manifest):
        return True
    approval = manifest.get("fact_approval")
    if not isinstance(approval, dict):
        return False
    try:
        resume_artifact_from_manifest(manifest_path, "pdf")
        content_sha256 = verified_resume_content_sha256(manifest_path, manifest)
    except PortableResumeError:
        return False
    expected_pdf = str(manifest.get("artifacts", {}).get("pdf", {}).get("sha256", "")).upper()
    return bool(
        approval.get("approved_by") == "user"
        and approval.get("approved_at")
        and approval.get("pdf_sha256") == expected_pdf
        and approval.get("content_sha256") == content_sha256
        and dict(manifest.get("qa") or {}).get("truthfulness_check") == "passed"
    )


def resume_artifact_from_manifest(manifest_path: Path, kind: str) -> Path:
    if kind not in {"pdf", "docx"}:
        raise PortableResumeError("只支持读取 PDF 或 DOCX 简历草稿。")
    payload = read_resume_manifest(manifest_path)
    if payload is None:
        raise PortableResumeError("简历质检清单损坏或缺少必要字段，请重新生成该岗位草稿。")
    name = str(payload.get("artifacts", {}).get(kind, {}).get("path", "")).strip()
    expected = str(payload.get("artifacts", {}).get(kind, {}).get("sha256", "")).upper()
    artifact = (manifest_path.parent / name).resolve()
    if (
        not name
        or not artifact.is_file()
        or artifact.parent != manifest_path.parent.resolve()
        or not expected
        or _sha256(artifact) != expected
    ):
        raise PortableResumeError(f"{kind.upper()} 草稿缺失或哈希已变化。")
    return artifact
