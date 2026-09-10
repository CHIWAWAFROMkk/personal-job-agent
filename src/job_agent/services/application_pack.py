from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from job_agent.models.application import (
    ApplicationEvidence,
    ApplicationJobSnapshot,
    ApplicationMaterials,
    ApplicationPack,
    ApplicationReviewItem,
    ResumeBundle,
)
from job_agent.models.job import HardGateStatus, MatchResult, MatchStatus
from job_agent.models.job_record import JobDetail
from job_agent.models.profile import ClaimStatus, EvidenceFact, Experience, Profile
from job_agent.services.job_repository import normalize_company, normalize_title


class ApplicationPackError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApplicationPackFiles:
    directory: Path
    pack_json: Path
    materials_markdown: Path
    jd_text: Path
    match_json: Path


@dataclass(frozen=True)
class _FactContext:
    experience: Experience
    fact: EvidenceFact


_FACT_ID_PATTERN = re.compile(r"\[([a-zA-Z0-9][a-zA-Z0-9_.-]*)\]")
_METRIC_PATTERN = re.compile(r"(?:\d[\d,.]*\s*(?:\+|%|分|人|条|个|小时|万|元)?)")
_THEME_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("战略", "重点项目"), "战略项目跟进"),
    (("项目", "进度", "闭环"), "项目推进与闭环"),
    (("流程", "堵点"), "流程梳理与优化"),
    (("数据", "分析", "复盘"), "数据分析与复盘"),
    (("AI", "GPT", "Claude", "智能"), "AI 工具提效"),
    (("产品", "需求"), "产品与需求协同"),
    (("用户", "增长"), "用户运营与增长"),
    (("活动", "策划"), "活动策划与执行"),
    (("内容", "文案"), "内容运营"),
    (("沟通", "协作", "跨部门"), "跨部门协作"),
    (("报告", "材料", "PPT"), "报告与材料制作"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _ready_fact_index(profile: Profile) -> dict[str, _FactContext]:
    index: dict[str, _FactContext] = {}
    for experience in profile.experiences:
        for fact in experience.facts:
            if profile.is_application_ready(fact.status):
                index[fact.id] = _FactContext(experience=experience, fact=fact)
    return index


def _job_source_url(job: JobDetail) -> str | None:
    return next((source.source_url for source in job.sources if source.source_url), None)


def extract_job_themes(result: MatchResult, *, limit: int = 4) -> list[str]:
    source_text = "\n".join(
        [
            *result.job.responsibilities,
            result.job.title,
            result.job.raw_text,
        ]
    )
    themes: list[str] = []
    for keywords, label in _THEME_RULES:
        if any(keyword.casefold() in source_text.casefold() for keyword in keywords):
            if label == "项目推进与闭环" and "战略项目跟进" in themes:
                continue
            themes.append(label)
        if len(themes) >= limit:
            break
    if not themes:
        role = re.sub(r"(?:实习生|专员|助理|经理)$", "", result.job.title).strip()
        themes.append(role or "岗位核心工作")
    return themes


def _fact_relevance_score(
    context: _FactContext,
    result: MatchResult,
    advantage_order: dict[str, int],
    evidence_order: dict[str, int],
    resume_order: dict[str, int],
) -> int:
    fact_id = context.fact.id
    score = 0
    if fact_id in advantage_order:
        score += 30 - min(advantage_order[fact_id], 20)
    if fact_id in evidence_order:
        score += 25 - min(evidence_order[fact_id], 15)
    if fact_id in resume_order:
        score += 12 - min(resume_order[fact_id], 8)
    jd_text = result.job.raw_text.casefold()
    for keyword in [*context.fact.skills, *context.fact.tools]:
        normalized = keyword.strip().casefold()
        if normalized and normalized in jd_text:
            score += 5
    if _METRIC_PATTERN.search(context.fact.statement):
        score += 4
    if context.experience.kind.value in {"internship", "employment"}:
        score += 2
    return score


def _select_evidence(
    profile: Profile,
    result: MatchResult,
    *,
    resume_fact_ids: list[str] | None = None,
    limit: int = 4,
) -> list[_FactContext]:
    fact_index = _ready_fact_index(profile)
    advantage_ids: list[str] = []
    for advantage in result.advantages:
        advantage_ids.extend(_FACT_ID_PATTERN.findall(advantage))
    evidence_ids = [
        fact_id
        for evidence in result.evidence
        if evidence.status in {MatchStatus.MATCHED, MatchStatus.PARTIAL}
        for fact_id in evidence.profile_fact_ids
    ]
    advantage_order = {fact_id: index for index, fact_id in enumerate(advantage_ids)}
    evidence_order = {fact_id: index for index, fact_id in enumerate(evidence_ids)}
    resume_order = {
        fact_id: index for index, fact_id in enumerate(resume_fact_ids or [])
    }
    ranked = sorted(
        fact_index.values(),
        key=lambda context: (
            -_fact_relevance_score(
                context,
                result,
                advantage_order,
                evidence_order,
                resume_order,
            ),
            context.fact.id,
        ),
    )
    relevant = [
        context
        for context in ranked
        if _fact_relevance_score(
            context,
            result,
            advantage_order,
            evidence_order,
            resume_order,
        )
        > 2
    ]
    candidates = relevant or ranked

    selected: list[_FactContext] = []
    used_experiences: set[str] = set()
    diversity_target = min(3, limit)
    for context in candidates:
        if context.experience.id in used_experiences:
            continue
        selected.append(context)
        used_experiences.add(context.experience.id)
        if len(selected) >= diversity_target:
            break
    for context in candidates:
        if context in selected:
            continue
        selected.append(context)
        if len(selected) >= limit:
            break

    metric_candidates = [
        context for context in candidates if _METRIC_PATTERN.search(context.fact.statement)
    ]
    if metric_candidates and not any(
        _METRIC_PATTERN.search(context.fact.statement) for context in selected
    ):
        if len(selected) >= limit:
            selected[-1] = metric_candidates[0]
        else:
            selected.append(metric_candidates[0])
    metric_position = next(
        (
            index
            for index, context in enumerate(selected)
            if _METRIC_PATTERN.search(context.fact.statement)
        ),
        None,
    )
    if metric_position not in {None, 0}:
        selected.insert(0, selected.pop(metric_position))
    return selected


def _education_identity(profile: Profile) -> tuple[str, str]:
    ready = [
        item for item in profile.education if profile.is_application_ready(item.status)
    ]
    if not ready:
        return "", "求职者"
    education = sorted(ready, key=lambda item: item.end or "", reverse=True)[0]
    graduation = f"{education.end[:4]}届" if education.end and len(education.end) >= 4 else ""
    major = f"{education.major}专业" if education.major else ""
    degree = f"{education.degree}生" if education.degree else "学生"
    identity = f"{education.institution}{major}{graduation}{degree}"
    return education.institution, identity


def _strip_terminal_punctuation(value: str) -> str:
    return value.strip().rstrip("。；;！!")


def _personal_statement(value: str) -> str:
    statement = _strip_terminal_punctuation(value)
    if statement.startswith(("曾", "目前")):
        return statement
    if statement.startswith(("负责", "参与", "使用", "协助", "独立", "完成", "对接", "策划")):
        return "曾" + statement
    return statement


def _join_chinese(items: list[str], *, fallback: str) -> str:
    usable = [item for item in items if item]
    if not usable:
        return fallback
    return "、".join(usable)


def _availability_answer(
    profile: Profile,
    result: MatchResult,
    generated_at: datetime,
) -> str:
    is_internship = "实习" in result.job.title or "实习" in "".join(
        result.job.arrival_requirements
    )
    availability = profile.job_search.availability
    if not is_internship:
        ready = [
            item
            for item in profile.education
            if profile.is_application_ready(item.status) and item.end
        ]
        if ready:
            end = sorted(ready, key=lambda item: item.end or "", reverse=True)[0].end
            assert end is not None
            return f"目前在读，预计 {end} 毕业；正式到岗时间需与岗位安排确认。"
        return "正式到岗时间需与岗位安排确认。"

    parts: list[str] = []
    if availability.earliest_start:
        try:
            start = date.fromisoformat(availability.earliest_start)
        except ValueError:
            parts.append(f"可于 {availability.earliest_start} 开始")
        else:
            if start <= generated_at.date():
                parts.append("可立即开始")
            else:
                parts.append(f"可于 {start.isoformat()} 开始")
    if availability.days_per_week:
        if (
            availability.max_days_per_week
            and availability.max_days_per_week != availability.days_per_week
        ):
            parts.append(
                f"每周可稳定到岗 {availability.days_per_week} 天、最高 {availability.max_days_per_week} 天"
            )
        else:
            parts.append(f"每周可稳定到岗 {availability.days_per_week} 天")
    if availability.duration_months:
        parts.append(f"可连续实习至少 {availability.duration_months} 个月")
    return "；".join(parts) + ("。" if parts else "到岗安排需要进一步确认。")


def _build_review_checklist(
    result: MatchResult,
    resume: ResumeBundle,
    availability_answer: str,
) -> list[ApplicationReviewItem]:
    items: list[ApplicationReviewItem] = []
    seen: set[tuple[str, str]] = set()

    for gate in result.hard_gates:
        if gate.status == HardGateStatus.PASSES:
            status = "confirmed"
            blocks = False
        elif gate.status == HardGateStatus.FAILS:
            status = "needs_confirmation"
            blocks = True
        else:
            status = "needs_confirmation"
            blocks = True
        item = ApplicationReviewItem(
            category="hard_gate",
            item=gate.requirement,
            status=status,
            detail=gate.explanation,
            blocks_submission=blocks,
        )
        items.append(item)
        seen.add((item.category, item.item))

    for unknown in result.unknowns:
        if any(unknown in item.item or item.item in unknown for item in items):
            continue
        key = ("unknown", unknown)
        if key in seen:
            continue
        items.append(
            ApplicationReviewItem(
                category="unknown",
                item=unknown,
                status="needs_confirmation",
                detail="匹配分析中尚无足够事实支持，提交前必须确认。",
                blocks_submission=True,
            )
        )
        seen.add(key)

    for gap in result.gaps:
        key = ("capability_gap", gap)
        if key in seen:
            continue
        items.append(
            ApplicationReviewItem(
                category="capability_gap",
                item=gap,
                status="capability_gap",
                detail="这是能力缺口，不会被写成已掌握经历。",
                blocks_submission=False,
            )
        )
        seen.add(key)

    items.append(
        ApplicationReviewItem(
            category="availability",
            item="到岗安排",
            status="confirmed" if "确认" not in availability_answer else "needs_confirmation",
            detail=availability_answer,
            blocks_submission="确认" in availability_answer,
        )
    )
    items.append(
        ApplicationReviewItem(
            category="resume",
            item="岗位定向简历",
            status="confirmed" if resume.status == "ready" else "needs_confirmation",
            detail=resume.note,
            blocks_submission=resume.status != "ready",
        )
    )
    for item, detail in (
        ("薪资要求", "若申请表要求填写，必须由本人确认，不自动猜测。"),
        ("地点与是否接受调剂", "属于主观选择，必须由本人确认。"),
        ("最终提交申请", "Agent 只准备材料；点击最终提交前必须由本人确认。"),
    ):
        items.append(
            ApplicationReviewItem(
                category="manual_input",
                item=item,
                status="manual_input",
                detail=detail,
                blocks_submission=True,
            )
        )
    return items


def _resume_fact_ids(payload: dict) -> list[str]:
    fact_ids: list[str] = []
    seen: set[str] = set()
    for section in payload.get("experience_sections", []):
        for entry in section.get("entries", []):
            for bullet in entry.get("bullets", []):
                for fact_id in bullet.get("fact_ids", []):
                    if fact_id not in seen:
                        fact_ids.append(fact_id)
                        seen.add(fact_id)
    return fact_ids


def _resume_matches_job(payload: dict, job: JobDetail) -> bool:
    target = payload.get("target", {})
    company = str(target.get("company", ""))
    role = str(target.get("role", ""))
    return (
        normalize_company(company) == normalize_company(job.company)
        and normalize_title(role) == normalize_title(job.title)
    )


def _load_resume_bundle(
    content_path: Path,
    profile: Profile,
    job: JobDetail,
) -> ResumeBundle:
    try:
        payload = json.loads(content_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplicationPackError(f"定向简历内容无法读取: {content_path}: {exc}") from exc
    if not _resume_matches_job(payload, job):
        raise ApplicationPackError(
            f"定向简历与岗位 #{job.job_id} 的公司或岗位名称不一致: {content_path}"
        )

    fact_ids = _resume_fact_ids(payload)
    if not fact_ids:
        raise ApplicationPackError(
            f"定向简历没有引用任何 Profile 事实，不能通过真实性检查: {content_path}"
        )
    ready_ids = set(_ready_fact_index(profile))
    invalid_ids = sorted(set(fact_ids) - ready_ids)
    if invalid_ids:
        raise ApplicationPackError(
            "定向简历引用了不存在或尚未确认的事实: " + "、".join(invalid_ids)
        )

    directory = content_path.parent
    manifest_candidates: list[tuple[int, float, Path, dict]] = []
    for manifest_path in directory.glob("resume-version*.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        target = manifest.get("target", {})
        target_matches = (
            normalize_company(str(target.get("company", "")))
            == normalize_company(job.company)
            and normalize_title(str(target.get("title", "")))
            == normalize_title(job.title)
        )
        if not target_matches:
            continue
        content_source = manifest.get("content_source")
        priority = 2 if content_source == content_path.name else 1 if not content_source else 0
        if priority:
            manifest_candidates.append(
                (priority, manifest_path.stat().st_mtime, manifest_path, manifest)
            )

    manifest_path: Path | None = None
    docx_path: Path | None = None
    pdf_path: Path | None = None
    base_qa_verified = False
    qa_verified = False
    problem = ""
    if manifest_candidates:
        _, _, manifest_path, manifest = max(
            manifest_candidates,
            key=lambda item: (item[0], item[1], str(item[2])),
        )
        artifacts = manifest.get("artifacts", {})
        docx_info = artifacts.get("docx", {})
        pdf_info = artifacts.get("pdf", {})
        docx_name = str(docx_info.get("path", "")).strip()
        pdf_name = str(pdf_info.get("path", "")).strip()
        docx_path = (directory / docx_name).resolve() if docx_name else None
        pdf_path = (directory / pdf_name).resolve() if pdf_name else None
        expected_docx = str(docx_info.get("sha256", "")).upper()
        expected_pdf = str(pdf_info.get("sha256", "")).upper()
        hashes_match = bool(
            docx_path
            and pdf_path
            and docx_path.is_file()
            and pdf_path.is_file()
            and expected_docx
            and expected_pdf
            and _sha256(docx_path) == expected_docx
            and _sha256(pdf_path) == expected_pdf
        )
        qa = manifest.get("qa", {})
        photo_ok = (
            qa.get("photo_requirement") == "not_required"
            or qa.get("photo_embedded") is True
            or qa.get("embedded_photos", 0) >= 1
        )
        base_qa_verified = bool(
            hashes_match
            and qa.get("truthfulness_check") == "passed"
            and qa.get("docx_structural_review") == "passed"
            and qa.get("pdf_pages") == 1
            and photo_ok
        )
        qa_verified = bool(
            base_qa_verified and qa.get("pdf_visual_review") == "passed"
        )
        if not hashes_match:
            problem = "质检清单中的文件哈希与当前 DOCX/PDF 不一致。"
        elif not base_qa_verified:
            problem = "质检清单未同时确认真实性、照片、DOCX 结构和单页 PDF。"
        elif qa.get("pdf_visual_review") not in {"passed", "pending_user_review"}:
            problem = "PDF 版面质检状态无效。"
    else:
        problem = "未找到与该定向简历对应的质检清单。"

    visual_status = (
        manifest.get("qa", {}).get("pdf_visual_review")
        if manifest_candidates
        else None
    )
    if qa_verified:
        status = "ready"
        note = "已通过事实引用、文件哈希、真实性、嵌入照片、DOCX 结构和单页 PDF 质检。"
    elif visual_status == "pending_user_review" and not problem:
        status = "needs_review"
        note = "机器质检已通过；请打开 PDF 检查版面，确认后才能标记为可投。"
    else:
        status = "needs_generation"
        note = f"已找到定向简历内容，但尚不能标记为可投：{problem}"
    return ResumeBundle(
        status=status,
        content_path=str(content_path.resolve()),
        manifest_path=str(manifest_path.resolve()) if manifest_path else None,
        docx_path=str(docx_path) if docx_path else None,
        pdf_path=str(pdf_path) if pdf_path else None,
        fact_ids=fact_ids,
        qa_verified=qa_verified,
        note=note,
    )


def resolve_resume_bundle(
    profile: Profile,
    job: JobDetail,
    applications_dir: Path,
    *,
    content_path: Path | None = None,
) -> ResumeBundle:
    if content_path is not None:
        path = content_path.expanduser().resolve()
        if not path.is_file():
            raise ApplicationPackError(f"定向简历内容不存在: {path}")
        return _load_resume_bundle(path, profile, job)

    candidates: list[Path] = []
    if applications_dir.is_dir():
        for path in applications_dir.rglob("resume-content*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if _resume_matches_job(payload, job):
                candidates.append(path)
    if not candidates:
        return ResumeBundle(
            status="needs_generation",
            note="尚未找到与该 JD 对应的定向简历；下一步应先生成并人工复核。",
        )
    resolved: list[tuple[int, float, Path, ResumeBundle]] = []
    errors: list[tuple[float, ApplicationPackError]] = []
    status_priority = {"ready": 2, "needs_review": 1, "needs_generation": 0}
    for candidate in candidates:
        try:
            bundle = _load_resume_bundle(candidate, profile, job)
        except ApplicationPackError as exc:
            errors.append((candidate.stat().st_mtime, exc))
            continue
        resolved.append(
            (
                status_priority[bundle.status],
                candidate.stat().st_mtime,
                candidate,
                bundle,
            )
        )
    if resolved:
        _, _, _, selected = max(
            resolved,
            key=lambda item: (item[0], item[1], str(item[2])),
        )
        return selected
    if errors:
        raise max(errors, key=lambda item: item[0])[1]
    return ResumeBundle(
        status="needs_generation",
        note="没有找到可通过真实性检查的定向简历版本。",
    )


def build_application_pack(
    profile: Profile,
    job: JobDetail,
    result: MatchResult,
    resume: ResumeBundle,
    *,
    generated_at: datetime | None = None,
) -> ApplicationPack:
    generated_at = generated_at or datetime.now(UTC)
    selected = _select_evidence(
        profile,
        result,
        resume_fact_ids=resume.fact_ids,
    )
    if not selected:
        raise ApplicationPackError("Profile 中没有可用于投递材料的已确认事实。")

    name = profile.person.display_name or profile.person.legal_name or "求职者"
    school, education_identity = _education_identity(profile)
    identity = f"{name}，{education_identity}" if education_identity else name
    themes = extract_job_themes(result)
    theme_text = _join_chinese(themes, fallback="岗位核心工作")
    statements = [_personal_statement(item.fact.statement) for item in selected]
    first = statements[0]
    second = statements[1] if len(statements) > 1 else statements[0]
    third = statements[2] if len(statements) > 2 else second
    availability_answer = _availability_answer(profile, result, generated_at)

    boss_greeting = (
        f"您好，我是{identity}，想应聘贵司{job.title}。"
        f"我{first}；同时{second}。"
        f"这些经历与岗位的{theme_text}较契合，期待进一步沟通。"
    )
    email_subject = f"应聘{job.title}｜{name}｜{school or '个人申请'}"
    email_body = (
        "您好：\n\n"
        f"我是{identity}，现申请{job.company}的{job.title}。\n\n"
        f"结合 JD，我理解该岗位的重点是{theme_text}。"
        f"我的相关经历包括：{first}；{second}；{third}。"
        "以上内容均来自已确认的真实经历，并已在定向简历中按岗位重点呈现。\n\n"
        f"{availability_answer}\n\n"
        "感谢您审阅我的申请，期待有机会进一步沟通。\n\n"
        f"{name}"
    )
    self_introduction = (
        f"您好，我是{identity}。我希望应聘{job.title}，"
        f"过往经历主要围绕{theme_text}展开。"
        f"我{first}；也{second}。"
        "我希望把这些已验证的执行和分析经验迁移到该岗位。"
    )
    why_company = (
        f"我关注{job.company}，最直接的原因是该岗位把{theme_text}放在同一条工作链路中，"
        "既要求扎实执行，也要求用数据和工具推动结果闭环。"
        f"这与我在{selected[0].experience.organization or '过往项目'}中形成的经验能够衔接，"
        "我可以较快进入实际任务，同时继续加深对业务场景的理解。"
    )
    why_role = (
        f"我选择{job.title}，是因为它不只是完成单点事务，而是需要围绕{theme_text}持续推进。"
        f"我已经有可迁移的真实经验：{first}；{second}。"
        "因此我既能从基础执行切入，也能用数据和复盘意识提高交付质量。"
    )
    personal_strengths = [
        item.fact.statement for item in selected
    ]
    interview_intro = (
        f"面试官您好，我是{identity}，应聘的是{job.title}。"
        f"我对这个岗位的理解，核心是{theme_text}。"
        f"与之最相关的经历有三类：第一，{first}；第二，{second}；第三，{third}。"
        "这些经历让我形成了先明确目标、再整理信息和数据、持续跟进问题、最后复盘交付的工作方式。"
        f"我希望在{job.company}把这套方法用于真实业务，并对结果负责。"
    )

    evidence = []
    for index, context in enumerate(selected):
        used_for = ["个人优势", "Why role", "面试自我介绍"]
        if index < 3:
            used_for.extend(["求职邮件", "30 秒自我介绍"])
        if index < 2:
            used_for.append("BOSS 招呼语")
        evidence.append(
            ApplicationEvidence(
                fact_id=context.fact.id,
                experience_id=context.experience.id,
                organization=context.experience.organization,
                role=context.experience.role,
                statement=context.fact.statement,
                status=context.fact.status.value,
                used_for=used_for,
            )
        )

    materials = ApplicationMaterials(
        boss_greeting=boss_greeting,
        email_subject=email_subject,
        email_body=email_body,
        self_introduction_30s=self_introduction,
        why_company=why_company,
        why_role=why_role,
        personal_strengths=personal_strengths,
        interview_self_introduction_60s=interview_intro,
        availability_answer=availability_answer,
    )
    checklist = _build_review_checklist(
        result,
        resume,
        availability_answer,
    )
    return ApplicationPack(
        generated_at=generated_at,
        job=ApplicationJobSnapshot(
            job_id=job.job_id,
            company=job.company,
            title=job.title,
            location=job.location,
            source_url=_job_source_url(job),
            match_score=result.overall_score,
            recommendation=result.recommendation.value,
        ),
        materials=materials,
        evidence=evidence,
        review_checklist=checklist,
        gaps=result.gaps,
        resume=resume,
    )


def _markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_application_pack_markdown(pack: ApplicationPack) -> str:
    review_lines = []
    for item in pack.review_checklist:
        marker = "✅" if item.status == "confirmed" else "⛔" if item.blocks_submission else "⚠️"
        review_lines.append(f"- {marker} **{item.item}**：{item.detail}")
    evidence_rows = [
        "| 事实 ID | 经历 | 已确认事实 | 用于 |",
        "|---|---|---|---|",
    ]
    for item in pack.evidence:
        experience = " / ".join(filter(None, (item.organization, item.role)))
        evidence_rows.append(
            "| "
            + " | ".join(
                (
                    _markdown_escape(item.fact_id),
                    _markdown_escape(experience),
                    _markdown_escape(item.statement),
                    _markdown_escape("、".join(item.used_for)),
                )
            )
            + " |"
        )
    strengths = "\n".join(
        f"- {item}" for item in pack.materials.personal_strengths
    )
    gaps = "\n".join(f"- {item}" for item in pack.gaps) or "- 暂无已识别缺口。"
    resume_lines = [f"- 状态：{pack.resume.status}", f"- 说明：{pack.resume.note}"]
    if pack.resume.manifest_path:
        resume_lines.append(f"- 质检清单：`{pack.resume.manifest_path}`")
    if pack.resume.docx_path:
        resume_lines.append(f"- DOCX：`{pack.resume.docx_path}`")
    if pack.resume.pdf_path:
        resume_lines.append(f"- PDF：`{pack.resume.pdf_path}`")

    recommendation = {
        "strongly_recommend": "强烈推荐",
        "recommend": "推荐",
        "try": "可以尝试",
        "low_priority": "低优先级",
    }.get(pack.job.recommendation, pack.job.recommendation)

    return f"""# {pack.job.company}｜{pack.job.title}｜投递材料包

> 岗位编号：#{pack.job.job_id}　匹配分：{pack.job.match_score}/100　推荐级别：{recommendation}

## 提交前确认

{chr(10).join(review_lines)}

## BOSS 招呼语

{pack.materials.boss_greeting}

## 求职邮件

**主题：{pack.materials.email_subject}**

{pack.materials.email_body}

## 30 秒自我介绍

{pack.materials.self_introduction_30s}

## Why this company

{pack.materials.why_company}

## Why this role

{pack.materials.why_role}

## 简短个人优势

{strengths}

## 60 秒面试自我介绍

{pack.materials.interview_self_introduction_60s}

## 到岗回答

{pack.materials.availability_answer}

## 能力缺口

{gaps}

## 定向简历

{chr(10).join(resume_lines)}

## 真实性追溯（不直接复制到申请表）

{chr(10).join(evidence_rows)}

---

本材料包只使用 Profile 中 `documented` 或 `user_confirmed` 的事实；能力缺口不会被伪装成已有经历。最终提交、薪资、地点、调剂和主观筛选题仍需本人确认。
"""


def write_application_pack(
    pack: ApplicationPack,
    job: JobDetail,
    result: MatchResult,
    output_dir: Path,
) -> ApplicationPackFiles:
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise ApplicationPackError(f"输出目录已存在，拒绝覆盖: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent)
    )
    try:
        pack_json = temporary / "application-pack.json"
        materials = temporary / "投递材料.md"
        jd_text = temporary / "JD.txt"
        match_json = temporary / "match-result.json"
        pack_json.write_text(pack.model_dump_json(indent=2) + "\n", encoding="utf-8")
        materials.write_text(render_application_pack_markdown(pack), encoding="utf-8")
        jd_text.write_text(job.jd_text.rstrip() + "\n", encoding="utf-8")
        match_json.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return ApplicationPackFiles(
        directory=output_dir,
        pack_json=output_dir / "application-pack.json",
        materials_markdown=output_dir / "投递材料.md",
        jd_text=output_dir / "JD.txt",
        match_json=output_dir / "match-result.json",
    )
