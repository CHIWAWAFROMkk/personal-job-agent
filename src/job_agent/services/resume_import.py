"""原简历文本 → 便携证据版简历底稿（仅生成建议，不落盘）。

安全约定：
- AI 只做"结构化解析"：从用户粘贴的原简历文本中抽取姓名、联系方式、
  摘要、技能、经历要点与教育背景，填入便携模板的可编辑结构。
- 禁止编造：输出内容中的每一个数字（百分比、人数、金额、年份等）
  都必须能在原文中找到，否则整体拒绝本次导入建议。
- 导入结果只是建议：填入编辑器表单后，必须由本人逐项检查并确认保存
  （user_confirmed），再通过 PDF 版面人工审阅，才可能进入投递材料包。
- person.photo_path / target 等岗位绑定与本地文件字段不接受 AI 输出，
  一律沿用当前草稿的原值。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from job_agent.services.ai_errors import safe_ai_error_message
from job_agent.services.resume_editor import validate_resume_content
from job_agent.services.runtime_config import RuntimeConfig


class ResumeImportError(RuntimeError):
    pass


_MAX_RESUME_CHARS = 8000
_MAX_SUMMARY_CHARS = 500
_MAX_SKILL_COUNT = 12
_MAX_BULLET_CHARS = 300

PORTABLE_TEMPLATE_ID = "portable-evidence-resume-v1"

_SYSTEM_PROMPT = (
    "你是中文简历的结构化解析助手。你会收到用户原简历的纯文本与当前简历草稿的字段结构说明。"
    "任务：把原简历文本解析为与草稿同构的 JSON 对象，用于填充简历编辑器表单。"
    "字段规则：person 只含 name/phone/email/city/linkedin/website（原文没有就留空字符串，"
    "不得猜测）；summary 用 1-3 句话概括原文的个人定位（≤500 字，不得新增原文没有的信息）；"
    "skills 为技能词数组（每项 ≤30 字，最多 12 项，来自原文）；"
    "experience_sections 为经历板块数组（最多 3 板块，如“实习与工作经历”“项目与校园经历”），"
    "每个板块含 title 与 entries（每板块最多 4 段），每段经历含 organization/role/dates 与 "
    "bullets（每段最多 4 条），每条 bullet 含可选 label（≤16 字）与 text（≤300 字），"
    "text 按 STAR 法则整理：情境/任务半句带过、行动以强动词开头、结果落在原文的量化产出上，"
    "措辞尽量沿用原文，量化结果必须原样保留；"
    "education 为数组（最多 3 条），每条含 institution/degree/major/start/end（原文没有的字段留空）。"
    "绝对禁止：编造原文没有的事实、数字、荣誉或经历；合并或改写任何量化数字"
    "（百分比、人数、次数、金额等必须与原文一致）。"
    "输出严格为 JSON 对象：{\"person\": {...}, \"summary\": 字符串, \"skills\": [字符串], "
    "\"experience_sections\": [{\"title\": 字符串, \"entries\": [{\"organization\": 字符串, "
    "\"role\": 字符串, \"dates\": 字符串, \"bullets\": [{\"label\": 字符串, \"text\": 字符串}]}]}], "
    "\"education\": [{\"institution\": 字符串, \"degree\": 字符串, \"major\": 字符串, "
    "\"start\": 字符串, \"end\": 字符串}]}，不要输出任何其他文字。"
)


@dataclass
class ResumeImportSuggestion:
    content: dict[str, object]
    warnings: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def _numbers_in(text: str) -> set[float]:
    """抽取数字集合（按浮点值归一化，便于 2023.06 与 2023.6 匹配）。"""
    values: set[float] = set()
    for token in re.findall(r"\d+(?:\.\d+)?", text):
        try:
            values.add(float(token))
        except ValueError:  # pragma: no cover - 正则保证可解析
            continue
    return values


def _collect_text_values(node: object) -> str:
    """深度遍历 AI 输出，收集所有字符串值用于数字完整性校验。"""
    parts: list[str] = []
    if isinstance(node, str):
        parts.append(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in {"photo_path", "target", "resume_version_id", "template_id", "schema_version"}:
                continue  # 这些字段沿用本地原值，不参与数字校验
            parts.append(_collect_text_values(value))
    elif isinstance(node, list):
        for item in node:
            parts.append(_collect_text_values(item))
    return "\n".join(parts)


def _parse_model_json(raw: str) -> dict[str, object]:
    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResumeImportError("AI 返回的内容不是合法 JSON，已放弃本次导入建议。") from exc
    if not isinstance(parsed, dict):
        raise ResumeImportError("AI 返回的内容不是 JSON 对象，已放弃本次导入建议。")
    return parsed


def _invoke_ai(
    config: RuntimeConfig,
    user_payload: str,
) -> tuple[str, int, int]:
    """按运行配置调用云端 AI；供测试 monkeypatch。"""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise ResumeImportError("OpenAI SDK 未安装，无法进行云端解析。") from exc

    if config.ai.provider == "openai":
        if not config.ai.api_key:
            raise ResumeImportError("OpenAI API Key 尚未配置。")
        response = OpenAI(api_key=config.ai.api_key).responses.create(
            model=config.ai.model,
            store=False,
            input=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            max_output_tokens=2400,
        )
        content = (getattr(response, "output_text", "") or "").strip()
    elif config.ai.provider == "openai_compatible":
        if not config.ai.base_url:
            raise ResumeImportError("OpenAI 兼容 API 地址尚未配置。")
        response = OpenAI(
            api_key=config.ai.api_key or "local-api-no-key",
            base_url=config.ai.base_url,
        ).chat.completions.create(
            model=config.ai.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            temperature=0.1,
        )
        content = (response.choices[0].message.content or "").strip()
    else:
        raise ResumeImportError("当前 AI Provider 不支持云端解析。")
    if not content:
        raise ResumeImportError("AI 未返回可用回答，已放弃本次导入建议。")

    usage = getattr(response, "usage", None)
    input_tokens = int(
        getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", 0) or 0
    )
    output_tokens = int(
        getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", 0) or 0
    )
    return content, input_tokens, output_tokens


def import_resume_text_into_draft(
    resume_text: str,
    current_content: dict[str, object],
    *,
    config: RuntimeConfig,
) -> ResumeImportSuggestion:
    """把原简历文本解析为可编辑底稿建议（不写盘、不改变原对象）。"""
    if config.ai.provider == "local":
        raise ResumeImportError(
            "尚未配置云端 AI；请在“连接与 API”中配置后再使用原简历导入。"
        )
    try:
        current = validate_resume_content(current_content)
    except Exception as exc:  # ResumeEditorError
        raise ResumeImportError(f"当前简历草稿无法解析导入: {exc}") from exc

    text = (resume_text or "").strip()
    if not text:
        raise ResumeImportError("原简历文本为空，无法解析。")
    if len(text) > _MAX_RESUME_CHARS:
        raise ResumeImportError(
            f"原简历文本超过 {_MAX_RESUME_CHARS} 字，请精简后重试。"
        )

    user_payload = json.dumps(
        {"resume_text": text},
        ensure_ascii=False,
    )

    try:
        raw, input_tokens, output_tokens = _invoke_ai(config, user_payload)
    except ResumeImportError:
        raise
    except Exception as exc:
        raise ResumeImportError(
            safe_ai_error_message(
                exc,
                provider=config.ai.provider,
                action="原简历解析",
            )
        ) from exc
    parsed = _parse_model_json(raw)

    # 数字完整性：AI 输出中的每个数字都必须能在原文中找到（防编造）。
    source_numbers = _numbers_in(text)
    output_numbers = _numbers_in(_collect_text_values(parsed))
    invented = sorted(
        str(number) for number in output_numbers - source_numbers
    )
    if invented:
        raise ResumeImportError(
            f"AI 输出包含原文没有的数字（{', '.join(invented[:5])}），"
            "疑似编造量化结果，已放弃本次导入建议。"
        )

    # 沿用本地控制的字段：模板标识、版本、岗位绑定、证件照路径。
    merged: dict[str, object] = {
        "schema_version": "1.0",
        "template_id": PORTABLE_TEMPLATE_ID,
        "resume_version_id": str(current.get("resume_version_id") or ""),
        "person": dict(current.get("person") or {}),  # type: ignore[arg-type]
        "target": dict(current.get("target") or {}),  # type: ignore[arg-type]
        "summary": "",
        "skills": [],
        "experience_sections": [],
        "education": [],
        "truthfulness": {
            "confirmed_fact_ids": [],
            "imported_from_user_resume": True,
            "rule": (
                "内容来自用户原简历文本的 AI 结构化解析；需本人逐项确认后才视为 user_confirmed。"
            ),
        },
    }
    ai_person = parsed.get("person")
    if isinstance(ai_person, dict):
        person = dict(merged["person"])  # type: ignore[assignment]
        for field_name in ("name", "phone", "email", "city", "linkedin", "website"):
            value = ai_person.get(field_name)
            if isinstance(value, str) and value.strip():
                person[field_name] = value.strip()
        # 姓名：AI 未给出时沿用当前草稿（校验要求非空）。
        if not str(person.get("name") or "").strip():
            person["name"] = str((current.get("person") or {}).get("name") or "")  # type: ignore[union-attr]
        merged["person"] = person

    summary = parsed.get("summary")
    if isinstance(summary, str) and summary.strip():
        merged["summary"] = summary.strip()
    else:
        merged["summary"] = str(current.get("summary") or "")

    skills = parsed.get("skills")
    if isinstance(skills, list):
        cleaned: list[str] = []
        for skill in skills:
            if isinstance(skill, str):
                item = skill.strip()
                if item and len(item) <= 30:
                    cleaned.append(item)
        merged["skills"] = cleaned[:_MAX_SKILL_COUNT]
    else:
        merged["skills"] = list(current.get("skills") or [])  # type: ignore[arg-type]

    sections = parsed.get("experience_sections")
    if isinstance(sections, list) and sections:
        cleaned_sections: list[dict[str, object]] = []
        for section in sections[:3]:
            if not isinstance(section, dict):
                continue
            title = str(section.get("title") or "").strip()
            raw_entries = section.get("entries")
            if not title or not isinstance(raw_entries, list) or not raw_entries:
                continue
            cleaned_entries: list[dict[str, object]] = []
            for entry in raw_entries[:4]:
                if not isinstance(entry, dict):
                    continue
                organization = str(entry.get("organization") or "").strip()
                raw_bullets = entry.get("bullets")
                if not organization or not isinstance(raw_bullets, list) or not raw_bullets:
                    continue
                cleaned_bullets: list[dict[str, object]] = []
                for bullet in raw_bullets[:4]:
                    if not isinstance(bullet, dict):
                        continue
                    bullet_text = str(bullet.get("text") or "").strip()
                    if not bullet_text:
                        continue
                    label = bullet.get("label")
                    cleaned_bullets.append(
                        {
                            "label": str(label).strip() if isinstance(label, str) else "",
                            "text": bullet_text[:_MAX_BULLET_CHARS],
                        }
                    )
                if not cleaned_bullets:
                    continue
                cleaned_entries.append(
                    {
                        "organization": organization,
                        "role": str(entry.get("role") or "").strip(),
                        "dates": str(entry.get("dates") or "").strip(),
                        "bullets": cleaned_bullets,
                    }
                )
            if cleaned_entries:
                cleaned_sections.append({"title": title, "entries": cleaned_entries})
        if cleaned_sections:
            merged["experience_sections"] = cleaned_sections

    education = parsed.get("education")
    if isinstance(education, list):
        cleaned_education: list[dict[str, object]] = []
        for item in education[:3]:
            if not isinstance(item, dict):
                continue
            institution = str(item.get("institution") or "").strip()
            if not institution:
                continue
            cleaned_education.append(
                {
                    "institution": institution,
                    "degree": str(item.get("degree") or "").strip(),
                    "major": str(item.get("major") or "").strip(),
                    "start": str(item.get("start") or "").strip(),
                    "end": str(item.get("end") or "").strip(),
                }
            )
        merged["education"] = cleaned_education

    warnings: list[str] = []
    if not merged["skills"]:
        warnings.append("未能从原文解析出技能项，已沿用当前草稿的技能。")
    if not merged["experience_sections"]:
        warnings.append("未能从原文解析出经历板块，已保留当前草稿的板块结构。")
        merged["experience_sections"] = list(
            current.get("experience_sections") or []
        )
    if not merged["education"]:
        warnings.append("未能从原文解析出教育背景，已沿用当前草稿的教育信息。")
        merged["education"] = list(current.get("education") or [])

    try:
        validate_resume_content(merged)
    except Exception as exc:  # ResumeEditorError
        raise ResumeImportError(f"解析结果未通过结构校验: {exc}") from exc

    return ResumeImportSuggestion(
        content=merged,
        warnings=warnings,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
