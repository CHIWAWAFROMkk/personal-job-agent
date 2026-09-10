from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from job_agent.models.job import MatchResult, MatchStatus
from job_agent.models.job_record import JobDetail
from job_agent.models.profile import EvidenceFact, Experience, ExperienceKind, Profile
from job_agent.services.application_pack import extract_job_themes
from job_agent.services.job_repository import normalize_company, normalize_title


class TailoredResumeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResumeTemplateSource:
    reference_docx: Path
    reference_sha256: str
    base_content_path: Path
    base_content: dict
    manifest_path: Path


@dataclass(frozen=True)
class ResumeDraftFiles:
    directory: Path
    content_json: Path
    docx: Path
    pdf: Path
    manifest: Path


_FACT_ID_PATTERN = re.compile(r"\[([a-zA-Z0-9][a-zA-Z0-9_.-]*)\]")
_METRIC_PATTERN = re.compile(r"\d[\d,.]*\s*(?:\+|%|分|人|条|个|小时|万|元)?")


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value, flags=re.UNICODE).strip("-")
    return safe[:60] or "resume"


def _content_matches_target(payload: dict, target: dict) -> bool:
    content_target = payload.get("target", {})
    return (
        normalize_company(str(content_target.get("company", "")))
        == normalize_company(str(target.get("company", "")))
        and normalize_title(str(content_target.get("role", "")))
        == normalize_title(str(target.get("title", "")))
    )


def find_verified_resume_template(applications_dir: Path) -> ResumeTemplateSource:
    candidates: list[tuple[float, ResumeTemplateSource]] = []
    if not applications_dir.is_dir():
        raise TailoredResumeError("尚无可复用的定向简历模板目录。")

    for manifest_path in applications_dir.rglob("resume-version*.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        qa = manifest.get("qa", {})
        photo_ok = qa.get("photo_embedded") is True or qa.get("embedded_photos", 0) >= 1
        if not (
            qa.get("truthfulness_check") == "passed"
            and qa.get("docx_structural_review") == "passed"
            and qa.get("pdf_visual_review") == "passed"
            and qa.get("pdf_pages") == 1
            and photo_ok
        ):
            continue

        docx_info = manifest.get("artifacts", {}).get("docx", {})
        docx_name = str(docx_info.get("path", "")).strip()
        expected_hash = str(docx_info.get("sha256", "")).upper()
        if not docx_name or not expected_hash:
            continue
        reference_docx = (manifest_path.parent / docx_name).resolve()
        if not reference_docx.is_file() or _sha256(reference_docx) != expected_hash:
            continue

        content_source = str(manifest.get("content_source", "")).strip()
        content_paths = (
            [manifest_path.parent / content_source]
            if content_source
            else list(manifest_path.parent.glob("resume-content*.json"))
        )
        base_content_path: Path | None = None
        base_content: dict | None = None
        target = manifest.get("target", {})
        for content_path in content_paths:
            try:
                payload = json.loads(content_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("template_id") != "large-company-star-resume-v1":
                continue
            if _content_matches_target(payload, target):
                base_content_path = content_path.resolve()
                base_content = payload
                break
        if base_content_path is None or base_content is None:
            continue
        candidates.append(
            (
                manifest_path.stat().st_mtime,
                ResumeTemplateSource(
                    reference_docx=reference_docx,
                    reference_sha256=expected_hash,
                    base_content_path=base_content_path,
                    base_content=base_content,
                    manifest_path=manifest_path.resolve(),
                ),
            )
        )

    if not candidates:
        raise TailoredResumeError(
            "没有找到同时通过照片、真实性、结构、单页 PDF 和版面质检的模板。"
        )
    return max(candidates, key=lambda item: item[0])[1]


def _ready_facts(profile: Profile, experience: Experience) -> list[EvidenceFact]:
    return [
        fact
        for fact in experience.facts
        if profile.is_application_ready(fact.status)
    ]


def _match_fact_ids(result: MatchResult) -> tuple[dict[str, int], dict[str, int]]:
    advantages: list[str] = []
    for item in result.advantages:
        advantages.extend(_FACT_ID_PATTERN.findall(item))
    evidence = [
        fact_id
        for item in result.evidence
        if item.status in {MatchStatus.MATCHED, MatchStatus.PARTIAL}
        for fact_id in item.profile_fact_ids
    ]
    return (
        {fact_id: index for index, fact_id in enumerate(advantages)},
        {fact_id: index for index, fact_id in enumerate(evidence)},
    )


def _fact_score(
    fact: EvidenceFact,
    result: MatchResult,
    advantage_order: dict[str, int],
    evidence_order: dict[str, int],
) -> int:
    score = 0
    if fact.id in advantage_order:
        score += 40 - min(advantage_order[fact.id], 20)
    if fact.id in evidence_order:
        score += 32 - min(evidence_order[fact.id], 20)
    raw_jd = result.job.raw_text.casefold()
    for keyword in [*fact.skills, *fact.tools]:
        if keyword.strip() and keyword.strip().casefold() in raw_jd:
            score += 7
    if _METRIC_PATTERN.search(fact.statement):
        score += 5
    return score


def _experience_score(
    profile: Profile,
    experience: Experience,
    result: MatchResult,
    advantage_order: dict[str, int],
    evidence_order: dict[str, int],
) -> int:
    scores = sorted(
        (
            _fact_score(fact, result, advantage_order, evidence_order)
            for fact in _ready_facts(profile, experience)
        ),
        reverse=True,
    )
    kind_bonus = 10 if experience.kind in {ExperienceKind.INTERNSHIP, ExperienceKind.EMPLOYMENT} else 3
    return kind_bonus + sum(scores[:3])


def _select_experiences(
    profile: Profile,
    result: MatchResult,
) -> list[Experience]:
    advantage_order, evidence_order = _match_fact_ids(result)
    work_kinds = {ExperienceKind.INTERNSHIP, ExperienceKind.EMPLOYMENT}
    project_kinds = {
        ExperienceKind.PROJECT,
        ExperienceKind.CAMPUS,
        ExperienceKind.COMPETITION,
    }

    def ranked(kinds: set[ExperienceKind]) -> list[Experience]:
        usable = [
            experience
            for experience in profile.experiences
            if experience.kind in kinds and len(_ready_facts(profile, experience)) >= 2
        ]
        return sorted(
            usable,
            key=lambda experience: (
                -_experience_score(
                    profile,
                    experience,
                    result,
                    advantage_order,
                    evidence_order,
                ),
                experience.id,
            ),
        )

    work_ranked = ranked(work_kinds)
    first_work_candidates = [
        experience
        for experience in work_ranked
        if len(_ready_facts(profile, experience)) >= 3
    ]
    first_work = (
        max(
            first_work_candidates,
            key=lambda experience: (
                any(
                    _METRIC_PATTERN.search(fact.statement)
                    for fact in _ready_facts(profile, experience)
                ),
                _experience_score(
                    profile,
                    experience,
                    result,
                    advantage_order,
                    evidence_order,
                ),
            ),
        )
        if first_work_candidates
        else None
    )
    work = (
        [
            first_work,
            *[experience for experience in work_ranked if experience != first_work][:1],
        ]
        if first_work is not None
        else []
    )
    projects = ranked(project_kinds)[:2]
    if len(work) < 2 or len(projects) < 2:
        raise TailoredResumeError(
            "固定 STAR 模板需要 2 段实习和 2 段项目/校园经历，当前已确认事实不足。"
        )
    return [*work, *projects]


def _fact_label(fact: EvidenceFact, used: set[str]) -> str:
    text = f"{fact.statement} {' '.join(fact.skills)} {' '.join(fact.tools)}"
    rules = (
        (("主数据", "CDP"), "主数据治理"),
        (("AI", "人工智能"), "AI 提效"),
        (("合规",), "合规分析"),
        (("跨部门", "对接", "协同"), "跨部门协作"),
        (("流程",), "流程推进"),
        (("调研", "问卷"), "调研分析"),
        (("Python", "SQL", "Pandas", "NumPy", "数据分析"), "数据分析"),
        (("报告", "PPT", "展示"), "报告交付"),
        (("活动",), "活动策划"),
        (("公众号", "文案", "新媒体"), "内容运营"),
        (("一等奖", "成绩", "认可", "延长实习"), "成果表现"),
    )
    candidates = [label for keywords, label in rules if any(keyword in text for keyword in keywords)]
    candidates.extend(skill[:6] for skill in fact.skills if skill)
    candidates.append("任务执行")
    for label in candidates:
        if label not in used:
            used.add(label)
            return label
    fallback = f"工作成果{len(used) + 1}"
    used.add(fallback)
    return fallback


def _format_date(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("-", ".")


def _entry_from_experience(
    profile: Profile,
    experience: Experience,
    result: MatchResult,
    bullet_count: int,
) -> dict:
    advantage_order, evidence_order = _match_fact_ids(result)
    facts = sorted(
        _ready_facts(profile, experience),
        key=lambda fact: (
            -_fact_score(fact, result, advantage_order, evidence_order),
            fact.id,
        ),
    )
    if len(facts) < bullet_count:
        raise TailoredResumeError(
            f"经历 {experience.id} 只有 {len(facts)} 条已确认事实，无法填充 {bullet_count} 条 STAR 要点。"
        )
    selected = facts[:bullet_count]
    metric_position = next(
        (index for index, fact in enumerate(selected) if _METRIC_PATTERN.search(fact.statement)),
        None,
    )
    if metric_position not in {None, 0}:
        selected.insert(0, selected.pop(metric_position))
    used_labels: set[str] = set()
    organization, role = _entry_identity(experience, selected)
    return {
        "organization": organization,
        "role": role,
        "dates": " - ".join(
            filter(None, (_format_date(experience.start), _format_date(experience.end)))
        ),
        "bullets": [
            {
                "label": _fact_label(fact, used_labels),
                "text": fact.statement,
                "fact_ids": [fact.id],
            }
            for fact in selected
        ],
    }


def _short_organization(value: str) -> str:
    shortened = re.sub(r"[（(]上海[）)]", "", value)
    shortened = re.sub(
        r"(?:企业管理|信息技术|科技股份|科技|股份)?有限公司$",
        "",
        shortened,
    )
    return shortened.strip() or value


def _entry_identity(
    experience: Experience,
    selected_facts: list[EvidenceFact],
) -> tuple[str, str]:
    if experience.kind == ExperienceKind.PROJECT and experience.role.endswith("项目组长"):
        project_name = experience.role[: -len("项目组长")].strip()
        if project_name:
            return project_name, "项目组长"
    if experience.kind == ExperienceKind.COMPETITION and any(
        "生成式 AI" in fact.statement for fact in selected_facts
    ):
        return "生成式 AI 校园应用研究", experience.role
    return experience.organization or experience.role, experience.role


def _summary_role(experience: Experience) -> str:
    if "HR数字化" in experience.role.replace(" ", ""):
        return "HR 数字化"
    return experience.role.replace("实习", "").strip()


def _graduation_identity(profile: Profile) -> tuple[dict, str]:
    education = next(
        (
            item
            for item in sorted(profile.education, key=lambda item: item.end or "", reverse=True)
            if profile.is_application_ready(item.status)
        ),
        None,
    )
    if education is None:
        raise TailoredResumeError("缺少已确认的教育经历，无法生成简历。")
    graduation = education.end[:4] if education.end and len(education.end) >= 4 else ""
    identity = f"{graduation}届{education.major}{education.degree}生" if graduation else f"{education.major}{education.degree}生"
    return education.model_dump(mode="json"), identity


def _availability_label(profile: Profile, result: MatchResult, generated_at: datetime) -> str:
    if "实习" not in result.job.title:
        education, _ = _graduation_identity(profile)
        end = str(education.get("end") or "")
        return f"{end[:4]}届校招" if end else "应届生申请"
    start_value = profile.job_search.availability.earliest_start
    if start_value:
        try:
            if date.fromisoformat(start_value) <= generated_at.date():
                return "可立即到岗"
        except ValueError:
            pass
    return "到岗时间待确认"


def _verified_skill_names(profile: Profile) -> list[str]:
    return [
        skill.name
        for skill in profile.skills
        if profile.is_application_ready(skill.status)
    ]


def generate_tailored_resume_content(
    profile: Profile,
    job: JobDetail,
    result: MatchResult,
    template: ResumeTemplateSource,
    *,
    generated_at: datetime | None = None,
) -> dict:
    generated_at = generated_at or datetime.now(UTC)
    education, education_identity = _graduation_identity(profile)
    experiences = _select_experiences(profile, result)
    themes = extract_job_themes(result)
    skill_names = _verified_skill_names(profile)
    name = profile.person.display_name or profile.person.legal_name
    if not name:
        raise TailoredResumeError("Profile 缺少姓名。")
    base_person = dict(template.base_content.get("person", {}))
    if base_person.get("name") != name:
        raise TailoredResumeError("模板姓名与当前 Profile 不一致，拒绝复用。")
    for required in ("phone", "email", "photo_path"):
        if not base_person.get(required):
            raise TailoredResumeError(f"模板缺少 {required}，无法生成完整简历。")
    base_person["education_status"] = (
        f"{education.get('degree', '')}在读，{str(education.get('end') or '')[:4]}年毕业"
    )

    work_summary = "与".join(
        f"{_short_organization(experience.organization)}{_summary_role(experience)}"
        for experience in experiences[:2]
    )
    theme_text = "、".join(themes)
    tech = [name for name in ("Excel", "AI工具", "Python", "SQL") if name in skill_names]
    summary = (
        f"{education_identity}，具备{work_summary}双实习经历，围绕{theme_text}开展工作；"
        f"能够使用{'、'.join(tech) or '数据与办公工具'}完成信息整理、数据分析、协同推进与成果交付。"
    )

    availability = profile.job_search.availability
    availability_parts: list[str] = []
    if "实习" in result.job.title:
        availability_parts.append(_availability_label(profile, result, generated_at))
        if availability.days_per_week:
            days = (
                f"每周{availability.days_per_week}-{availability.max_days_per_week}天"
                if availability.max_days_per_week
                and availability.max_days_per_week != availability.days_per_week
                else f"每周{availability.days_per_week}天"
            )
            availability_parts.append(days)
        if availability.duration_months:
            availability_parts.append(f"至少{availability.duration_months}个月")
    language = "CET-6 550分" if any(fact.id == "fact-cet6-550" for exp in profile.experiences for fact in _ready_facts(profile, exp)) else "英语能力已记录"
    skills = [
        {"label": "岗位能力", "value": theme_text},
        {
            "label": "数据与办公",
            "value": "、".join(name for name in ("Excel", "数据分析", "Pandas", "NumPy", "SQL") if name in skill_names),
        },
        {
            "label": "AI 与技术",
            "value": "、".join(name for name in ("AI工具", "Python", "Pandas", "NumPy", "SQL") if name in skill_names),
        },
        {
            "label": "语言与到岗" if availability_parts else "语言能力",
            "value": "；".join([language, *availability_parts]),
        },
    ]
    if any(not row["value"] for row in skills):
        raise TailoredResumeError("已确认技能不足，无法填满固定模板的四行核心能力。")

    education_dates = " - ".join(
        filter(None, (_format_date(education.get("start")), _format_date(education.get("end"))))
    )
    coursework = "、".join(education.get("coursework", []))
    education_details = f"相关课程：{coursework}" if coursework else ""
    if language.startswith("CET"):
        education_details += ("  |  " if education_details else "") + language

    return {
        "schema_version": "1.0",
        "template_id": "large-company-star-resume-v1",
        "resume_version_id": (
            f"resume-v{generated_at.strftime('%Y%m%dT%H%M%SZ')}-job-{job.job_id}-local-star-v1"
        ),
        "person": base_person,
        "target": {
            "company": job.company,
            "role": job.title,
            "label": _availability_label(profile, result, generated_at),
            "keywords": themes,
            "job_id": job.job_id,
            "source_url": next(
                (source.source_url for source in job.sources if source.source_url),
                None,
            ),
            "generated_at": generated_at.isoformat(),
        },
        "summary": summary,
        "skills": skills,
        "experience_sections": [
            {
                "title": "实习经历",
                "entries": [
                    _entry_from_experience(profile, experiences[0], result, 3),
                    _entry_from_experience(profile, experiences[1], result, 2),
                ],
            },
            {
                "title": "项目与校园经历",
                "entries": [
                    _entry_from_experience(profile, experiences[2], result, 2),
                    _entry_from_experience(profile, experiences[3], result, 2),
                ],
            },
        ],
        "education": {
            "institution": education["institution"],
            "degree": " ".join(filter(None, (education.get("major"), education.get("degree")))),
            "dates": education_dates,
            "details": education_details,
        },
        "truthfulness": {
            "exclude_unverified": [
                *result.gaps,
                *result.unknowns,
                *[
                    gate.requirement
                    for gate in result.hard_gates
                    if gate.status.value != "passes"
                ],
                "无来源支持的量化提升",
            ]
        },
    }


def _run_script(script: Path, arguments: list[str], *, project_root: Path) -> str:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise TailoredResumeError(f"简历生成步骤失败 ({script.name}): {detail}")
    return completed.stdout.strip()


def build_tailored_resume_draft(
    profile: Profile,
    profile_path: Path,
    job: JobDetail,
    result: MatchResult,
    applications_dir: Path,
    project_root: Path,
    output_dir: Path,
    *,
    generated_at: datetime | None = None,
) -> ResumeDraftFiles:
    generated_at = generated_at or datetime.now(UTC)
    template = find_verified_resume_template(applications_dir)
    content = generate_tailored_resume_content(
        profile,
        job,
        result,
        template,
        generated_at=generated_at,
    )
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise TailoredResumeError(f"简历输出目录已存在，拒绝覆盖: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent)
    )
    try:
        content_path = temporary / "resume-content-auto-star.json"
        safe_role = _safe_filename(job.title)
        safe_name = _safe_filename(profile.person.display_name or "求职者")
        docx_path = temporary / f"{safe_name}-{safe_role}-自动STAR草稿.docx"
        pdf_path = temporary / f"{safe_name}-{safe_role}-自动STAR草稿.pdf"
        manifest_path = temporary / "resume-version-auto-star.json"
        content_path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        scripts_dir = project_root / "scripts"
        _run_script(
            scripts_dir / "build_star_resume_from_template.py",
            [
                str(template.reference_docx),
                str(content_path),
                str(docx_path),
                "--reference-sha256",
                template.reference_sha256,
            ],
            project_root=project_root,
        )
        _run_script(
            scripts_dir / "build_star_resume_pdf.py",
            [
                str(content_path),
                str(pdf_path),
                "--project-root",
                str(project_root),
            ],
            project_root=project_root,
        )
        verification_output = _run_script(
            scripts_dir / "verify_star_resume.py",
            [
                "--content",
                str(content_path),
                "--profile",
                str(profile_path),
                "--docx",
                str(docx_path),
                "--pdf",
                str(pdf_path),
                "--expected-bullets",
                "9",
                "--required",
                profile.person.display_name,
                "--required",
                job.title,
            ],
            project_root=project_root,
        )
        try:
            verification = json.loads(verification_output)
        except json.JSONDecodeError as exc:
            raise TailoredResumeError("简历验证输出无法解析。") from exc

        manifest = {
            "version_id": content["resume_version_id"],
            "created_at": generated_at.isoformat(),
            "template_id": content["template_id"],
            "content_source": content_path.name,
            "target": {
                "company": job.company,
                "title": job.title,
                "location": job.location,
            },
            "template_source": {
                "path": str(template.reference_docx),
                "sha256": template.reference_sha256,
                "preserved_unchanged": _sha256(template.reference_docx)
                == template.reference_sha256,
                "preserved_package_parts": 16,
            },
            "artifacts": {
                "docx": {"path": docx_path.name, "sha256": _sha256(docx_path)},
                "pdf": {"path": pdf_path.name, "sha256": _sha256(pdf_path)},
            },
            "qa": {
                "confirmed_fact_ids": verification["confirmed_fact_ids"],
                "star_bullets": verification["star_bullets"],
                "embedded_photos": verification["embedded_photos"],
                "docx_page_size": verification["docx_page_size"],
                "pdf_pages": verification["pdf_pages"],
                "required_text": verification["required_text"],
                "forbidden_text": verification["forbidden_text"],
                "docx_structural_review": "passed",
                "truthfulness_check": "passed",
                "pdf_visual_review": "pending_user_review",
                "docx_render_note": (
                    "LibreOffice is unavailable; DOCX structure and preserved package parts "
                    "were checked. Review the same-content PDF before approval."
                ),
            },
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output_dir)
    except Exception:
        import shutil

        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return ResumeDraftFiles(
        directory=output_dir,
        content_json=output_dir / content_path.name,
        docx=output_dir / docx_path.name,
        pdf=output_dir / pdf_path.name,
        manifest=output_dir / manifest_path.name,
    )


def approve_resume_visual_review(manifest_path: Path) -> Path:
    manifest_path = manifest_path.expanduser().resolve()
    if not manifest_path.is_file():
        raise TailoredResumeError(f"简历质检清单不存在: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TailoredResumeError(f"简历质检清单无法读取: {exc}") from exc
    qa = manifest.get("qa", {})
    if qa.get("pdf_visual_review") != "pending_user_review":
        raise TailoredResumeError("该简历不处于待人工版面确认状态。")
    artifacts = manifest.get("artifacts", {})
    for kind in ("docx", "pdf"):
        info = artifacts.get(kind, {})
        path = (manifest_path.parent / str(info.get("path", ""))).resolve()
        expected = str(info.get("sha256", "")).upper()
        if not path.is_file() or not expected or _sha256(path) != expected:
            raise TailoredResumeError(f"{kind.upper()} 文件缺失或哈希已变化，不能批准。")
    qa["pdf_visual_review"] = "passed"
    manifest["visual_approval"] = {
        "approved_at": datetime.now(UTC).isoformat(),
        "approved_by": "user",
        "statement": "用户已打开并确认 PDF 版面无误。",
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = manifest_path.with_name(f"resume-version-approved-{stamp}.json")
    if output.exists():
        raise TailoredResumeError(f"批准记录已存在，拒绝覆盖: {output}")
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return output
