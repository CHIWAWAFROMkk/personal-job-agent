"""Compose a whole resume from verified evidence; identities stay server-owned."""
from __future__ import annotations

import json
import re
from pathlib import Path

from job_agent.models.profile import Profile, ExperienceKind
from job_agent.services.resume_editor import validate_resume_content
from job_agent.services.resume_polish import (
    ResumePolishError, ResumePolishSuggestion, _invoke_ai, _parse_model_json,
)
from job_agent.services.runtime_config import RuntimeConfig


COMPOSE_PROMPT = (
    Path(__file__).resolve().parent.parent
    / "prompts" / "resume_compose_system.txt"
).read_text(encoding="utf-8").strip()


def _metric_tokens(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:[,.]\d+)*(?:\+|%|％|万|亿)?", re.sub(r"\s+", "", text)))


def _evidence_text(text: object, source: str, *, limit: int) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > limit:
        raise ResumePolishError("整份简历生成返回了空白或过长的内容。")
    if not _metric_tokens(text).issubset(_metric_tokens(source)):
        raise ResumePolishError("整份简历生成引入了来源不支持的数字或量化口径。")
    for term in ("精通", "专家", "主导", "独立负责", "独立完成"):
        if term in text and term not in source:
            raise ResumePolishError("整份简历生成提高了原始事实中的责任或熟练程度。")
    if "历史保存输出" in source and ("保存" not in text or "复算" not in text or "未" not in text):
        raise ResumePolishError("Notebook历史输出必须保留保存记录及未复算的限制。")
    return text.strip()


def compose_resume_content_with_jd(
    content: dict, jd_text: str, *, profile: Profile, config: RuntimeConfig,
    user_instruction: str = "",
) -> ResumePolishSuggestion:
    if config.ai.provider == "local":
        raise ResumePolishError("未配置云端模型，当前使用本地事实选材。")
    if not jd_text.strip() or len(user_instruction) > 2000:
        raise ResumePolishError("需要有效JD，修改要求不能超过2000字。")
    validate_resume_content(content)
    evidence = profile.application_context()
    # Only explicit resume evidence is sent. Contact, commute, paths and source notes stay local.
    payload = {
        "job_description": jd_text[:12000], "user_instruction": user_instruction,
        "experiences": evidence["experiences"], "skills": evidence["skills"],
        "education": evidence["education"],
        "protected_experience_ids": content.get("selection", {}).get("protected_experience_ids", []),
        "current_draft": {key: content.get(key) for key in (
            "summary", "self_evaluation", "skills", "experience_sections",
        )},
    }
    raw, input_tokens, output_tokens = _invoke_ai(
        config, json.dumps(payload, ensure_ascii=False),
        system_prompt=COMPOSE_PROMPT, max_output_tokens=6500,
    )
    parsed = _parse_model_json(raw)
    experiences = {e.id: e for e in profile.experiences}
    facts = {f.id: f for e in profile.experiences for f in e.facts if profile.is_application_ready(f.status)}
    entries = parsed.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 5:
        raise ResumePolishError("整份简历需要1至5段有证据的经历。")
    sections: dict[str, list] = {}
    used_facts: list[str] = []
    seen: set[str] = set()
    total_bullets = 0
    for item in entries:
        if not isinstance(item, dict):
            raise ResumePolishError("整份简历经历结构无效。")
        eid = item.get("experience_id")
        if not isinstance(eid, str) or eid not in experiences or eid in seen:
            raise ResumePolishError("整份简历引用了不存在或重复的经历。")
        seen.add(eid)
        experience = experiences[eid]
        eligible = {f.id for f in experience.facts if f.id in facts}
        rows = item.get("bullets")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
            raise ResumePolishError("每段经历需要1至4条要点。")
        bullets = []
        for row in rows:
            if not isinstance(row, dict):
                raise ResumePolishError("经历要点结构无效。")
            ids = row.get("fact_ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(fid, str) and fid in eligible for fid in ids):
                raise ResumePolishError("要点引用了未确认事实或其他经历的事实。")
            source = "\n".join(facts[fid].statement for fid in ids)
            text = _evidence_text(row.get("text"), source, limit=220)
            label = row.get("label", "")
            if not isinstance(label, str) or not 1 <= len(label) <= 16:
                raise ResumePolishError("要点标签长度必须为1至16字。")
            bullets.append({"label": label, "text": text, "fact_ids": list(dict.fromkeys(ids))})
            used_facts.extend(ids)
        total_bullets += len(bullets)
        title = {ExperienceKind.INTERNSHIP: "实习经历", ExperienceKind.EMPLOYMENT: "工作经历",
                 ExperienceKind.CAMPUS: "校园经历", ExperienceKind.VOLUNTEER: "志愿经历",
                 ExperienceKind.OTHER: "其他经历"}.get(experience.kind, "项目经历")
        sections.setdefault(title, []).append({
            "experience_id": eid, "experience_kind": experience.kind.value,
            "organization": experience.organization or "机构 / 项目名称待完善",
            "role": experience.role, "dates": " - ".join(v.replace("-", ".") for v in (experience.start, experience.end) if v),
            "context": experience.summary.split("。")[0] if "派遣" in experience.summary else "",
            "bullets": bullets,
        })
    if total_bullets > 12:
        raise ResumePolishError("整份简历超过12条要点，请压缩选材后再生成。")
    protected = set(content.get("selection", {}).get("protected_experience_ids", []))
    restored = []
    for section in content.get("experience_sections", []):
        for entry in section.get("entries", []):
            eid = entry.get("experience_id")
            if eid in protected and eid not in seen:
                ids = [fid for bullet in entry.get("bullets", []) for fid in bullet.get("fact_ids", [])]
                if not ids or any(fid not in facts for fid in ids):
                    raise ResumePolishError("保留经历的事实已失效，请重新生成基础草稿。")
                sections.setdefault(section["title"], []).append(json.loads(json.dumps(entry)))
                used_facts.extend(ids)
                total_bullets += len(entry["bullets"])
                seen.add(eid)
                restored.append(eid)
    if total_bullets > 12 or len(seen) > 5:
        raise ResumePolishError("AI删减了受保护经历且恢复后超出版面预算，已保留本地草稿。")
    summary_ids = parsed.get("summary_fact_ids")
    if not isinstance(summary_ids, list) or not summary_ids or not all(isinstance(fid, str) and fid in facts for fid in summary_ids):
        raise ResumePolishError("个人摘要缺少已确认事实依据。")
    summary_source = "\n".join(facts[fid].statement for fid in summary_ids)
    summary = _evidence_text(parsed.get("summary"), summary_source, limit=220)
    evaluation = parsed.get("self_evaluation", "")
    if evaluation:
        evaluation = _evidence_text(evaluation, summary_source, limit=100)
    elif not isinstance(evaluation, str):
        raise ResumePolishError("自我评价格式无效。")
    skill_index = {s.id: s for s in profile.skills if profile.is_application_ready(s.status)}
    skill_ids = parsed.get("skill_ids")
    if not isinstance(skill_ids, list) or len(skill_ids) > 12 or not all(isinstance(sid, str) and sid in skill_index for sid in skill_ids):
        raise ResumePolishError("技能选择包含未确认技能。")
    merged = json.loads(json.dumps(content, ensure_ascii=False))
    merged.update(summary=summary, self_evaluation=evaluation,
                  skills=[skill_index[sid].name + ("（基础）" if skill_index[sid].level.value in {"basic", "awareness"} else "") for sid in dict.fromkeys(skill_ids)],
                  experience_sections=[{"title": title, "entries": rows} for title, rows in sections.items()])
    highlight_ids = [fid for item in merged.get("highlights", []) for fid in item.get("fact_ids", []) if fid in facts]
    merged["truthfulness"]["confirmed_fact_ids"] = list(dict.fromkeys(used_facts + summary_ids + highlight_ids))
    questions = parsed.get("questions", [])
    strategy = parsed.get("strategy", "")
    if not isinstance(strategy, str) or len(strategy) > 600 or not isinstance(questions, list) or len(questions) > 8 or not all(isinstance(q, str) and len(q) <= 200 for q in questions):
        raise ResumePolishError("选材说明或待确认问题格式无效。")
    merged["generation"] = {"engine": "cloud_composition", "strategy": strategy, "questions": questions,
                            "restored_experience_ids": restored,
                            "review_required": True}
    validate_resume_content(merged)
    return ResumePolishSuggestion(merged, [{"field": "whole_resume", "before": content["experience_sections"], "after": merged["experience_sections"]}], input_tokens, output_tokens)
