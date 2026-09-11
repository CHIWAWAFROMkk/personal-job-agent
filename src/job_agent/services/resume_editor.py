"""简历内容编辑闭环：加载草稿 → 用户编辑校验 → 重渲染为新版本。

安全约定：
- 用户编辑后的内容视为 user_confirmed 级事实（本人书写/修改），
  仍需通过 PDF 版面人工审阅（pdf_visual_review）才能进入投递材料包。
- 校验只做结构检查，不改写用户输入的文字。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from job_agent.services.portable_resume import (
    PortableResumeFiles,
    _render_resume_version,
    find_latest_resume_manifest,
)

PORTABLE_TEMPLATE_ID = "portable-evidence-resume-v1"


def supported_template_ids() -> list[str]:
    """可编辑的模板 ID：legacy 便携证据版 + templates/ 目录下的声明式模板。"""
    from job_agent.services.resume_render import list_templates

    return [PORTABLE_TEMPLATE_ID, *list_templates()]

_MAX_NAME_CHARS = 30
_MAX_CONTACT_CHARS = 60
_MAX_PATH_CHARS = 260
_MAX_SUMMARY_CHARS = 500
_MAX_SKILL_CHARS = 30
_MAX_SKILL_COUNT = 12
_MAX_SECTIONS = 5
_MAX_ENTRIES_PER_SECTION = 4
_MAX_BULLETS_PER_ENTRY = 4
_MAX_LABEL_CHARS = 16
_MAX_BULLET_CHARS = 300
_MAX_FIELD_CHARS = 80


class ResumeEditorError(RuntimeError):
    pass


def _safe_slug(value: str, fallback: str = "resume") -> str:
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value, flags=re.UNICODE).strip("-")
    return safe[:60] or fallback


def _require_str(value: object, field: str, *, max_chars: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ResumeEditorError(f"{field} 必须是字符串。")
    text = value.strip()
    if not text and not allow_empty:
        raise ResumeEditorError(f"{field} 不能为空。")
    if len(text) > max_chars:
        raise ResumeEditorError(f"{field} 超过 {max_chars} 字。")
    return text


def load_latest_resume_content(
    applications_dir: Path,
    job_id: int,
) -> tuple[dict[str, object], Path]:
    """读取岗位最新草稿的 content JSON 与其 manifest 路径。"""
    manifest_path = find_latest_resume_manifest(applications_dir, job_id)
    if manifest_path is None:
        raise ResumeEditorError(f"岗位 {job_id} 还没有简历草稿，请先生成后再编辑。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResumeEditorError(f"简历质检清单无法读取: {exc}") from exc
    content_source = str(manifest.get("content_source") or "").strip()
    if not content_source:
        raise ResumeEditorError("质检清单缺少 content_source，无法定位简历内容。")
    content_path = (manifest_path.parent / content_source).resolve()
    if content_path.parent != manifest_path.parent.resolve():
        raise ResumeEditorError("简历内容路径异常，拒绝读取。")
    try:
        content = json.loads(content_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResumeEditorError(f"简历内容无法读取: {exc}") from exc
    if not isinstance(content, dict):
        raise ResumeEditorError("简历内容不是合法的 JSON 对象。")
    if content.get("template_id") not in supported_template_ids():
        raise ResumeEditorError(
            f"当前仅支持编辑便携证据版简历或声明式模板（{', '.join(supported_template_ids())}）。"
        )
    from job_agent.services.resume_render import suggest_self_evaluation
    content.setdefault("self_evaluation", suggest_self_evaluation(content))
    return content, manifest_path


def validate_resume_content(content: object) -> dict[str, object]:
    """结构校验；返回规范化前的原对象，失败抛 ResumeEditorError。"""
    if not isinstance(content, dict):
        raise ResumeEditorError("简历内容必须是 JSON 对象。")
    if content.get("template_id") not in supported_template_ids():
        raise ResumeEditorError(
            f"template_id 不受支持: {content.get('template_id')!r}；"
            f"当前仅支持 {', '.join(supported_template_ids())}。"
        )
    person = content.get("person")
    if not isinstance(person, dict):
        raise ResumeEditorError("person 必须是对象。")
    _require_str(person.get("name"), "person.name", max_chars=_MAX_NAME_CHARS)
    for field in ("phone", "email", "city", "linkedin", "website"):
        if person.get(field) is None:
            continue
        _require_str(
            person.get(field), f"person.{field}", max_chars=_MAX_CONTACT_CHARS, allow_empty=True
        )
    if person.get("photo_path") is not None:
        _require_str(
            person.get("photo_path"),
            "person.photo_path",
            max_chars=_MAX_PATH_CHARS,
            allow_empty=True,
        )

    _require_str(content.get("summary"), "summary", max_chars=_MAX_SUMMARY_CHARS)
    if "self_evaluation" in content:
        _require_str(content["self_evaluation"], "self_evaluation", max_chars=300, allow_empty=True)

    skills = content.get("skills")
    if not isinstance(skills, list):
        raise ResumeEditorError("skills 必须是字符串数组。")
    if len(skills) > _MAX_SKILL_COUNT:
        raise ResumeEditorError(f"skills 最多 {_MAX_SKILL_COUNT} 项。")
    for index, skill in enumerate(skills):
        _require_str(skill, f"skills[{index}]", max_chars=_MAX_SKILL_CHARS)

    sections = content.get("experience_sections")
    if not isinstance(sections, list):
        raise ResumeEditorError("experience_sections 必须是数组。")
    if not sections:
        raise ResumeEditorError("experience_sections 不能为空，至少保留一段经历。")
    if len(sections) > _MAX_SECTIONS:
        raise ResumeEditorError(f"experience_sections 最多 {_MAX_SECTIONS} 段。")
    for section_index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ResumeEditorError(f"experience_sections[{section_index}] 必须是对象。")
        _require_str(
            section.get("title"),
            f"experience_sections[{section_index}].title",
            max_chars=_MAX_FIELD_CHARS,
        )
        entries = section.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ResumeEditorError(
                f"experience_sections[{section_index}].entries 不能为空。"
            )
        if len(entries) > _MAX_ENTRIES_PER_SECTION:
            raise ResumeEditorError(
                f"experience_sections[{section_index}].entries 最多 "
                f"{_MAX_ENTRIES_PER_SECTION} 条。"
            )
        for entry_index, entry in enumerate(entries):
            entry_name = f"experience_sections[{section_index}].entries[{entry_index}]"
            if not isinstance(entry, dict):
                raise ResumeEditorError(f"{entry_name} 必须是对象。")
            project_entry = entry.get("experience_kind") == "project" or (
                not entry.get("experience_kind") and "项目" in section["title"]
            )
            organization = entry.get("organization")
            if project_entry and (organization is None or organization == ""):
                _require_str(entry.get("role"), f"“{section['title']}”第{entry_index + 1}段的项目名称", max_chars=_MAX_FIELD_CHARS)
            else:
                _require_str(
                    organization, f"“{section['title']}”第{entry_index + 1}段的机构 / 项目名称", max_chars=_MAX_FIELD_CHARS
                )
            for field in ("role", "dates"):
                if entry.get(field) is None:
                    continue
                _require_str(
                    entry.get(field), f"{entry_name}.{field}",
                    max_chars=_MAX_FIELD_CHARS, allow_empty=True,
                )
            if "context" in entry:
                _require_str(entry["context"], f"{entry_name}.context", max_chars=300, allow_empty=True)
            bullets = entry.get("bullets")
            if not isinstance(bullets, list) or not bullets:
                raise ResumeEditorError(f"{entry_name}.bullets 至少保留一条要点。")
            if len(bullets) > _MAX_BULLETS_PER_ENTRY:
                raise ResumeEditorError(
                    f"{entry_name}.bullets 最多 {_MAX_BULLETS_PER_ENTRY} 条。"
                )
            for bullet_index, bullet in enumerate(bullets):
                bullet_name = f"{entry_name}.bullets[{bullet_index}]"
                if not isinstance(bullet, dict):
                    raise ResumeEditorError(f"{bullet_name} 必须是对象。")
                if bullet.get("label") is not None:
                    _require_str(
                        bullet.get("label"), f"{bullet_name}.label",
                        max_chars=_MAX_LABEL_CHARS, allow_empty=True,
                    )
                _require_str(
                    bullet.get("text"), f"{bullet_name}.text", max_chars=_MAX_BULLET_CHARS
                )

    education = content.get("education")
    if not isinstance(education, list):
        raise ResumeEditorError("education 必须是数组。")
    if len(education) > 3:
        raise ResumeEditorError("education 最多 3 条。")
    for index, item in enumerate(education):
        if not isinstance(item, dict):
            raise ResumeEditorError(f"education[{index}] 必须是对象。")

    target = content.get("target")
    if not isinstance(target, dict) or not target.get("job_id"):
        raise ResumeEditorError("target.job_id 缺失，无法归属岗位。")
    return content


def rerender_edited_resume(
    content: object,
    applications_dir: Path,
    *,
    based_on: str | None = None,
    generated_at: datetime | None = None,
    edit_origin: str = "dashboard_form",
) -> PortableResumeFiles:
    """校验用户编辑后的 content 并渲染为新的待审阅版本。"""
    content = validate_resume_content(content)
    generated_at = generated_at or datetime.now(UTC)
    target = dict(content["target"])  # type: ignore[arg-type]
    job_id = int(target.get("job_id") or 0)
    stamp = generated_at.strftime("%Y%m%dT%H%M%S%fZ")
    content["resume_version_id"] = f"resume-{stamp}-job-{job_id}-portable-edited-v1"
    output_dir = (
        Path(applications_dir).expanduser()
        / (
            f"portable-job-{job_id}-"
            f"{_safe_slug(str(target.get('company') or 'company'))}-"
            f"{_safe_slug(str(target.get('role') or 'role'))}-edited-{stamp}"
        )
    )
    target_payload = {
        "job_id": job_id,
        "company": target.get("company"),
        "title": target.get("role") or target.get("title"),
        "location": target.get("location"),
    }
    user_edited = edit_origin == "dashboard_form"
    if user_edited:
        edit_note = "用户在 Dashboard 表单中编辑内容后重新渲染；内容视为 user_confirmed。"
    else:
        edit_note = (
            "用户在 Agent 对话中确认了修改指令；AI 生成了新版措辞，"
            "内容仍处于 pending_user_review，必须本人复核 PDF 后才能批准。"
        )
    return _render_resume_version(
        content,  # type: ignore[arg-type]
        target_payload,
        output_dir,
        generated_at=generated_at,
        extra_manifest={
            "edited_by_user": user_edited,
            "edit_origin": edit_origin,
            "ai_assisted": not user_edited,
            "based_on": based_on or "",
            "edit_note": edit_note,
        },
        extra_qa={"user_edited": user_edited, "ai_assisted": not user_edited},
    )
