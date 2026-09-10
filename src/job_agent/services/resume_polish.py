"""JD 与用户指令驱动的简历措辞润色核心（本函数只返回建议，不落盘）。

安全约定：
- AI 只改写措辞与术语对齐，禁止新增事实；输出按 bullet_id 逐条映射，
  结构（板块/经历/要点数量、机构、时间、教育、联系方式）与原稿完全一致。
- 每条要点的数字集合必须原样保留（不得新增或丢失量化结果）。
- 润色结果必须经过用户确认才可写盘；无论由表单保存还是 Agent 对话生成新版，
  都必须重新通过 PDF 版面人工审阅，才可能进入投递材料包。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from job_agent.services.ai_errors import safe_ai_error_message
from job_agent.services.resume_editor import validate_resume_content
from job_agent.services.runtime_config import RuntimeConfig


class ResumePolishError(RuntimeError):
    pass


class CloudAIUnavailableError(ResumePolishError):
    """Cloud connector failed before a usable suggestion was returned."""


_MAX_JD_CHARS = 6000
_MAX_SKILL_COUNT = 12
_MAX_SUMMARY_CHARS = 500
_MAX_BULLET_CHARS = 300
_MAX_INSTRUCTION_CHARS = 2000

_SYSTEM_PROMPT = (
    "你是中文求职简历的措辞润色助手。你会收到简历的可编辑文本与目标岗位 JD。"
    "任务：只改写措辞，让个人摘要、核心能力与经历要点自然嵌入 JD 中的关键术语"
    "（能力词、工具名、业务领域词）。"
    "如果提供了用户修改要求，在不突破事实边界的前提下优先遵循；若要求新增原稿没有的事实，忽略该部分。"
    "写作要求（STAR 法则，简约直白突出重点）："
    "1) 每条经历要点按 STAR 结构组织——情境/任务最多半句带过，"
    "写清本人做了什么、针对什么对象，用具体动词而非空话；不为追求强动词把参与升级成主导。"
    "结果只用原文已有产出和数字；没有量化依据就保留定性事实，不能强行补指标；"
    "2) 每条要点一句话讲完，不超过原文的 1.3 倍，不堆砌形容词与空泛评价"
    "（禁止“认真负责”“赋能”“闭环”这类词）；"
    "3) 一段经历内要点按“行动—结果”递进排序，最重要的放最前。"
    "4) 每句应能解释具体动作、本人贡献和结果口径；无法从原稿回答时保留事实边界。"
    "不要用“持续优化”“从 0 到 1”等套话替代具体行为，也不要为了匹配 JD 冒充已掌握的工具。"
    "绝对禁止：新增任何事实、数字、荣誉或经历；改变任何机构、角色、时间；"
    "删除或改写任何量化结果（百分比、人数、次数、金额等数字必须原样保留）。"
    "使用中文书面语。自我评价 self_evaluation 用两三句话、最多300字，结合JD突出有经历证据支持的能力和工作方式。"
    "允许润色用户已有性格自评，但不能把JD要求当成已具备的能力，不能编造技能、性格、管理经历或提高熟练程度。"
    "自我评价不要重复个人摘要或引入新的量化数字；输入自我评价为空时保持为空。"
    "输出严格为 JSON 对象：{\"summary\": 字符串, \"skills\": [字符串], "
    "\"self_evaluation\": 字符串, \"bullets\": [{\"bullet_id\": 字符串, \"text\": 字符串}]}，"
    "bullets 必须包含输入的全部 bullet_id 且不新增，不要输出任何其他文字。"
)


@dataclass
class ResumePolishSuggestion:
    content: dict[str, object]
    changes: list[dict[str, object]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def _numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _collect_bullets(content: dict[str, object]) -> list[tuple[str, str]]:
    """把所有要点拍平为 (bullet_id, text)，bullet_id 用于回填与结构对齐。"""
    items: list[tuple[str, str]] = []
    sections = content.get("experience_sections") or []
    for section_index, section in enumerate(sections):
        for entry_index, entry in enumerate(section.get("entries") or []):  # type: ignore[union-attr]
            for bullet_index, bullet in enumerate(entry.get("bullets") or []):  # type: ignore[union-attr]
                items.append(
                    (f"s{section_index}e{entry_index}b{bullet_index}", str(bullet.get("text") or ""))  # type: ignore[union-attr]
                )
    return items


def _parse_model_json(raw: str) -> dict[str, object]:
    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResumePolishError("AI 返回的内容不是合法 JSON，已放弃本次润色建议。") from exc
    if not isinstance(parsed, dict):
        raise ResumePolishError("AI 返回的内容不是 JSON 对象，已放弃本次润色建议。")
    return parsed


def _local_star_rewrite(text: str) -> str:
    """Tighten a fact without adding claims, numbers, tools, or outcomes."""
    rewrites = (
        ("使用 ", "运用 "),
        ("参与公司安全合规工作，协助", "围绕公司安全合规工作，协助"),
        ("参与前期调研，", "开展前期调研，"),
    )
    for before, after in rewrites:
        if text.startswith(before):
            return after + text[len(before) :]
    return text


def polish_resume_content_locally(
    content: dict[str, object],
    jd_text: str,
    *,
    user_instruction: str = "",
) -> ResumePolishSuggestion:
    """Evidence-safe fallback when no working cloud model is available.

    It may reorder confirmed skills, tighten action wording and group bullets into
    the reference template's content/result sections. It deliberately refuses to
    invent a missing result merely to make a bullet look more impressive.
    """
    try:
        validated = validate_resume_content(content)
    except Exception as exc:  # ResumeEditorError
        raise ResumePolishError(f"简历内容无法润色: {exc}") from exc
    jd = (jd_text or "").strip()
    if not jd:
        raise ResumePolishError("该岗位没有 JD 文本，无法按 JD 润色。")
    instruction = (user_instruction or "").strip()
    if len(instruction) > _MAX_INSTRUCTION_CHARS:
        raise ResumePolishError(
            f"单次简历修改要求最多 {_MAX_INSTRUCTION_CHARS} 字。"
        )

    merged = json.loads(json.dumps(validated, ensure_ascii=False))
    changes: list[dict[str, object]] = []
    signal = f"{jd}\n{instruction}".casefold()
    original_skills = [str(item) for item in list(merged.get("skills") or [])]

    def skill_relevant(skill: str) -> bool:
        folded = skill.casefold()
        if folded in signal:
            return True
        if skill == "数据分析":
            return "数据" in signal and "分析" in signal
        return False

    indexed_skills = list(enumerate(original_skills))
    ranked_skills = [
        skill
        for _, skill in sorted(
            indexed_skills,
            key=lambda item: (not skill_relevant(item[1]), item[0]),
        )
    ]
    if ranked_skills != original_skills:
        changes.append(
            {"field": "skills", "before": original_skills, "after": ranked_skills}
        )
        merged["skills"] = ranked_skills

    role = str(dict(merged.get("target") or {}).get("role") or "目标岗位")
    relevant = [skill for skill in ranked_skills if skill_relevant(skill)][:5]
    old_summary = str(merged.get("summary") or "").strip()
    summary_items = [
        item.strip()
        for item in re.split(r"[；;。]", old_summary)
        if item.strip()
    ]
    tailored: list[str] = []
    if relevant:
        tailored.append(f"面向{role}岗位，具备 {'、'.join(relevant)}等相关能力")
    for item in summary_items:
        if item not in tailored and not item.startswith("具备 "):
            tailored.append(item)
        if len(tailored) >= 3:
            break
    if not tailored and old_summary:
        tailored.append(old_summary.rstrip("。"))
    new_summary = "；".join(tailored[:3]).rstrip("；") + ("。" if tailored else "")
    if new_summary and new_summary != old_summary:
        changes.append(
            {"field": "summary", "before": old_summary, "after": new_summary}
        )
        merged["summary"] = new_summary

    for section in list(merged.get("experience_sections") or []):
        section_title = str(section.get("title") or "")  # type: ignore[union-attr]
        is_work = "工作" in section_title or "实习" in section_title
        section["title"] = "工作经历" if is_work else "项目经历"  # type: ignore[index]
        content_label = "工作内容" if is_work else "项目内容"
        result_label = "工作业绩" if is_work else "项目业绩"
        for entry in list(section.get("entries") or []):  # type: ignore[union-attr]
            for bullet_index, bullet in enumerate(entry.get("bullets") or []):  # type: ignore[union-attr]
                old_text = str(bullet.get("text") or "")  # type: ignore[union-attr]
                new_text = _local_star_rewrite(old_text)
                has_result = bool(re.search(
                    r"\d|提升|降低|增长|节省|保障|提供支持|获得|准确|结果|成果|成绩|认可|接管|补全",
                    new_text,
                ))
                old_label = str(bullet.get("label") or "")  # type: ignore[union-attr]
                new_label = result_label if has_result else content_label
                if new_text != old_text:
                    changes.append(
                        {
                            "field": "bullet",
                            "bullet_id": f"local-{bullet_index}",
                            "before": old_text,
                            "after": new_text,
                        }
                    )
                    bullet["text"] = new_text  # type: ignore[index]
                if new_label != old_label:
                    bullet["label"] = new_label  # type: ignore[index]

    try:
        validate_resume_content(merged)
    except Exception as exc:  # ResumeEditorError
        raise ResumePolishError(f"本地润色结果未通过结构校验: {exc}") from exc
    return ResumePolishSuggestion(content=merged, changes=changes)


def _invoke_ai(
    config: RuntimeConfig,
    user_payload: str,
) -> tuple[str, int, int]:
    """按运行配置调用云端 AI；供测试 monkeypatch。"""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise CloudAIUnavailableError(
            "云端润色组件加载失败，请修复或更新桌面安装包后重试。"
        ) from exc

    try:
        if config.ai.provider == "openai":
            if not config.ai.api_key:
                raise CloudAIUnavailableError("OpenAI API Key 尚未配置。")
            response = OpenAI(api_key=config.ai.api_key).responses.create(
                model=config.ai.model,
                store=False,
                input=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_payload},
                ],
                max_output_tokens=1600,
            )
            content = (getattr(response, "output_text", "") or "").strip()
        elif config.ai.provider == "openai_compatible":
            if not config.ai.base_url:
                raise CloudAIUnavailableError("OpenAI 兼容 API 地址尚未配置。")
            response = OpenAI(
                api_key=config.ai.api_key or "local-api-no-key",
                base_url=config.ai.base_url,
            ).chat.completions.create(
                model=config.ai.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_payload},
                ],
                temperature=0.3,
            )
            content = (response.choices[0].message.content or "").strip()
        else:
            raise CloudAIUnavailableError("当前 AI Provider 不支持云端润色。")
    except CloudAIUnavailableError:
        raise
    except Exception as exc:
        raise CloudAIUnavailableError(
            safe_ai_error_message(
                exc,
                provider=config.ai.provider,
                action="简历润色",
            )
        ) from exc
    if not content:
        raise ResumePolishError("AI 未返回可用回答，已放弃本次润色建议。")

    usage = getattr(response, "usage", None)
    input_tokens = int(
        getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", 0) or 0
    )
    output_tokens = int(
        getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", 0) or 0
    )
    return content, input_tokens, output_tokens


def polish_resume_content_with_jd(
    content: dict[str, object],
    jd_text: str,
    *,
    config: RuntimeConfig,
    user_instruction: str = "",
) -> ResumePolishSuggestion:
    """按 JD 关键词润色措辞，返回建议内容（不写盘、不改变原对象）。"""
    if config.ai.provider == "local":
        raise ResumePolishError(
            "尚未配置云端 AI；请在“连接与 API”中配置后再使用 JD 润色。"
        )
    try:
        validated = validate_resume_content(content)
    except Exception as exc:  # ResumeEditorError
        raise ResumePolishError(f"简历内容无法润色: {exc}") from exc

    jd = (jd_text or "").strip()
    if not jd:
        raise ResumePolishError("该岗位没有 JD 文本，无法按 JD 润色。")
    instruction = (user_instruction or "").strip()
    if len(instruction) > _MAX_INSTRUCTION_CHARS:
        raise ResumePolishError(
            f"单次简历修改要求最多 {_MAX_INSTRUCTION_CHARS} 字。"
        )

    bullets = _collect_bullets(validated)
    if not bullets:
        raise ResumePolishError("简历没有任何经历要点，无法润色。")

    user_payload = json.dumps(
        {
            "resume": {
                "summary": str(validated.get("summary") or ""),
                "self_evaluation": str(validated.get("self_evaluation") or ""),
                "skills": list(validated.get("skills") or []),
                "bullets": [
                    {"bullet_id": bullet_id, "text": text} for bullet_id, text in bullets
                ],
            },
            "job_description": jd[:_MAX_JD_CHARS],
            "user_instruction": instruction,
        },
        ensure_ascii=False,
    )

    raw, input_tokens, output_tokens = _invoke_ai(config, user_payload)
    parsed = _parse_model_json(raw)

    polished_summary = parsed.get("summary")
    polished_skills = parsed.get("skills")
    polished_bullets = parsed.get("bullets")
    original_evaluation = str(validated.get("self_evaluation") or "")
    evaluation = parsed.get("self_evaluation", original_evaluation)
    if not isinstance(evaluation, str) or len(evaluation) > 300:
        raise ResumePolishError("AI 返回的自我评价格式不正确或超过300字。")
    if _numbers_in(evaluation) != _numbers_in(original_evaluation):
        raise ResumePolishError("自我评价的量化数字发生变化，已放弃本次润色建议。")
    if not original_evaluation.strip():
        evaluation = ""
    if not isinstance(polished_summary, str) or not polished_summary.strip():
        raise ResumePolishError("AI 返回缺少有效 summary，已放弃本次润色建议。")
    if not isinstance(polished_skills, list) or not polished_skills:
        raise ResumePolishError("AI 返回缺少有效 skills，已放弃本次润色建议。")
    if not isinstance(polished_bullets, list):
        raise ResumePolishError("AI 返回缺少 bullets，已放弃本次润色建议。")

    polished_map: dict[str, str] = {}
    for item in polished_bullets:
        if not isinstance(item, dict):
            raise ResumePolishError("AI 返回的 bullet 结构不合法，已放弃本次润色建议。")
        bullet_id = item.get("bullet_id")
        text = item.get("text")
        if not isinstance(bullet_id, str) or not isinstance(text, str):
            raise ResumePolishError("AI 返回的 bullet 字段不合法，已放弃本次润色建议。")
        if bullet_id in polished_map:
            raise ResumePolishError("AI 返回了重复的 bullet_id，已放弃本次润色建议。")
        polished_map[bullet_id] = text.strip()

    expected_ids = {bullet_id for bullet_id, _ in bullets}
    if set(polished_map) != expected_ids:
        raise ResumePolishError(
            "AI 返回的要点与原稿数量不一致，已放弃本次润色建议。"
        )

    # 数字完整性：每条要点的数字集合必须原样保留（不新增、不丢失）。
    for bullet_id, original_text in bullets:
        polished_text = polished_map[bullet_id]
        if _numbers_in(original_text) != _numbers_in(polished_text):
            raise ResumePolishError(
                f"要点 {bullet_id} 的量化数字发生变化，已放弃本次润色建议。"
            )
        if not polished_text:
            raise ResumePolishError(f"要点 {bullet_id} 被润色为空，已放弃本次润色建议。")
        if len(polished_text) > _MAX_BULLET_CHARS:
            raise ResumePolishError(
                f"要点 {bullet_id} 润色后超过 {_MAX_BULLET_CHARS} 字，请重试或手动改写。"
            )

    cleaned_skills: list[str] = []
    for skill in polished_skills:
        if not isinstance(skill, str):
            raise ResumePolishError("AI 返回的 skills 含非字符串项，已放弃本次润色建议。")
        text = skill.strip()
        if not text or len(text) > 30:
            raise ResumePolishError("AI 返回的技能项为空或超长，已放弃本次润色建议。")
        cleaned_skills.append(text)
    if len(cleaned_skills) > _MAX_SKILL_COUNT:
        cleaned_skills = cleaned_skills[:_MAX_SKILL_COUNT]
    if len(polished_summary) > _MAX_SUMMARY_CHARS:
        raise ResumePolishError("润色后的个人摘要超过 500 字，请重试或手动改写。")

    merged = json.loads(json.dumps(validated, ensure_ascii=False))  # deep copy
    changes: list[dict[str, object]] = []
    if evaluation != original_evaluation:
        changes.append({"field": "self_evaluation", "before": original_evaluation, "after": evaluation})
        merged["self_evaluation"] = evaluation

    if polished_summary != merged.get("summary"):
        changes.append(
            {"field": "summary", "before": merged.get("summary"), "after": polished_summary}
        )
        merged["summary"] = polished_summary
    if cleaned_skills != list(merged.get("skills") or []):
        changes.append(
            {"field": "skills", "before": merged.get("skills"), "after": cleaned_skills}
        )
        merged["skills"] = cleaned_skills

    sections = merged.get("experience_sections") or []
    for section_index, section in enumerate(sections):
        for entry_index, entry in enumerate(section.get("entries") or []):  # type: ignore[union-attr]
            for bullet_index, bullet in enumerate(entry.get("bullets") or []):  # type: ignore[union-attr]
                bullet_id = f"s{section_index}e{entry_index}b{bullet_index}"
                polished_text = polished_map[bullet_id]
                if polished_text != bullet.get("text"):  # type: ignore[union-attr]
                    changes.append(
                        {
                            "field": "bullet",
                            "bullet_id": bullet_id,
                            "before": bullet.get("text"),  # type: ignore[union-attr]
                            "after": polished_text,
                        }
                    )
                    bullet["text"] = polished_text  # type: ignore[index]

    # 合并结果必须仍然通过结构校验（长度、数量上限等）。
    try:
        validate_resume_content(merged)
    except Exception as exc:  # ResumeEditorError
        raise ResumePolishError(f"润色结果未通过结构校验: {exc}") from exc

    return ResumePolishSuggestion(
        content=merged,
        changes=changes,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
