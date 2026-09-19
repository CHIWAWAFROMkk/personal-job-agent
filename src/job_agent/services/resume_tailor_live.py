"""Evidence-preserving local paragraph suggestions; coverage is not an ATS score."""
from __future__ import annotations

import re

from job_agent.models.profile import Profile
from job_agent.services.local_matcher import SKILL_CATALOG, match_job_locally, structure_job_locally


# Never split a qualifier away from the claim it limits. These are deliberately
# broad: a missed cosmetic improvement is preferable to a strengthened claim.
_QUALIFIED = re.compile(r"不|未|无|没|仅|只|尚|拟|计划|预计|可能|尝试|希望|待|如果|若|估计|大约|约|或|并非|"
                        r"\b(?:not|no|never|may|might|would|could|planned|approximately|only)\b", re.I)


def _tidy_statement(statement: str) -> tuple[str, list[str]]:
    """Only drop first-person scaffolding / expose an explicit output clause."""
    if _QUALIFIED.search(statement):
        return statement, ["包含否定、范围限制或不确定表述，保留原文以免改变事实含义。"]
    text = statement
    changes = []
    text, count = re.subn(r"^我(?=使用|参与|协助|负责|整理|分析|完成|维护|设计|开发)", "", text, count=1)
    if count:
        changes.append("省略句首第一人称；保留责任范围、行动及全部原有事实。")
    # Keep the conjunction and all words: this is punctuation, not a claim of
    # impact or causal attribution. Do not split an existing sentence again.
    text, count = re.subn(r"(?<=[\w\u4e00-\u9fff])并(?=输出|提交|交付|形成|产出)", "；并", text)
    if count:
        changes.append("用分号突出原文已写明的产出；未添加成果或因果关系。")
    return text, changes


def _evidence_questions(facts: list) -> tuple[list[str], list[str]]:
    """Editing prompts, never evidence and never inserted into resume text."""
    statements = "\n".join(f.statement for f in facts)
    gaps, questions = [], []
    if _QUALIFIED.search(statements):
        gaps.append("事实含有限定表述")
        questions.append("哪些内容已实际完成，哪些仍未完成或仅为计划？保留原有限定，不要把计划写成结果。")
    checks = [
        (r"为了解决|针对|背景|目标|需求|为.*提供", "背景与任务待核对", "这项工作要解决什么具体问题？你承担的任务是什么？"),
        (r"参与|协助|负责|独立|主导|配合|个人|本人", "个人职责边界待核对", "哪些步骤由你完成，哪些由同事完成？请明确参与、协助或负责的边界。"),
        (r"输出|提交|交付|形成|产出|报告|看板|文档|清单|周报", "交付物待核对", "最终留下了什么可以展示或复核的交付物？没有交付物时不要补写。"),
        (r"提升|降低|减少|增长|缩短|节省|采纳|采用|上线|通过验收", "结果及验证依据待核对", "成果是否被使用或验证？有记录才写数据；没有量化依据也可以说明真实产出。"),
    ]
    for pattern, gap, question in checks:
        if not re.search(pattern, statements):
            gaps.append(gap)
            questions.append(question)
    return gaps, questions


def contains_phrase(text: str, phrase: str) -> bool:
    pattern = re.escape(phrase.casefold())
    if phrase and phrase[0].isascii() and phrase[0].isalnum():
        pattern = r"(?<![a-z0-9_])" + pattern
    if phrase and phrase[-1].isascii() and phrase[-1].isalnum():
        pattern += r"(?![a-z0-9_])"
    return bool(re.search(pattern, text.casefold()))


def tailor_live(profile: Profile, *, jd_text: str, paragraph: str,
                fact_ids: list[str], resume_text: str | None = None) -> dict:
    if not isinstance(jd_text, str) or not jd_text.strip() or len(jd_text) > 30000:
        raise ValueError("JD 必须为 1–30000 字的文本。")
    if not isinstance(paragraph, str) or len(paragraph) > 10000:
        raise ValueError("段落最多 10000 字。")
    if resume_text is not None and (not isinstance(resume_text, str) or len(resume_text) > 60000):
        raise ValueError("简历文本最多 60000 字。")
    if not isinstance(fact_ids, list) or len(fact_ids) > 30 or any(not isinstance(v, str) or len(v) > 120 for v in fact_ids):
        raise ValueError("事实 ID 必须为最多 30 项的文本列表。")
    keywords = [name for name, aliases in SKILL_CATALOG.items()
                if any(contains_phrase(jd_text, word) for word in (name, *aliases))]
    text = paragraph if resume_text is None else resume_text
    matched = [name for name in keywords if any(contains_phrase(text, word)
               for word in (name, *SKILL_CATALOG[name]))]
    coverage = {"score": round(100 * len(matched) / len(keywords)) if keywords else None,
                "matched": matched, "missing": [k for k in keywords if k not in matched], "keywords": keywords}
    warnings = ["覆盖度仅衡量当前文本与本地核心词表的重合，不是 ATS 通过率，也不代表事实已核验。"]
    by_id = {fact.id: (experience.id, fact) for experience in profile.experiences for fact in experience.facts}
    selected = [by_id[id_] for id_ in dict.fromkeys(fact_ids) if id_ in by_id
                and profile.is_application_ready(by_id[id_][1].status)]
    invalid = [id_ for id_ in fact_ids if id_ not in by_id or not profile.is_application_ready(by_id[id_][1].status)]
    suggestions = []
    if invalid:
        warnings.append("包含未知或未确认的事实，请重新选择依据；本次未生成建议。")
    elif len({exp_id for exp_id, _ in selected}) > 1:
        warnings.append("一个段落不能混用不同经历的事实，请按经历分别编辑。")
    elif not selected:
        warnings.append("没有已确认的事实依据，请先选择本段落对应的经历事实。")
    else:
        facts = [fact for _, fact in selected]
        ranked = sorted(facts, key=lambda fact: -sum(contains_phrase(fact.statement, word)
                        for name in keywords for word in (name, *SKILL_CATALOG[name])))
        variants = [("faithful", "保留依据", facts, "保留选定事实原文，不添加任何成果或技能。"),
                    ("jd_first", "清晰表达", ranked, "按 JD 相关度排列事实，仅整理句首与行动／产出的标点，不扩写事实。"),
                    ("concise", "聚焦重点", ranked[:1], "选取最相关的一条完整事实；其余事实不在此建议中展示。")]
        for id_, title, items, reason in variants:
            source_text = "\n".join(f.statement for f in items)
            changes = []
            revised = []
            for fact in items:
                text, fact_changes = _tidy_statement(fact.statement) if id_ == "jd_first" else (fact.statement, [])
                revised.append(text)
                changes.extend(fact_changes)
            if id_ == "jd_first" and [f.id for f in facts] != [f.id for f in items]:
                changes.append("仅调整完整事实的顺序，不把不同事实拼接成新的因果关系。")
            if id_ == "concise" and len(items) < len(facts):
                changes.append("此建议只保留一条完整事实；没有从经历库删除其余事实。")
            missing_evidence, questions = _evidence_questions(items)
            suggestions.append({"id": id_, "title": title, "text": "\n".join(revised),
                                "fact_ids": [f.id for f in items], "reason": reason,
                                "source_text": source_text, "changes": list(dict.fromkeys(changes)),
                                "missing_evidence": missing_evidence, "questions": questions})
        warnings.append("STAR 补充问题仅用于提醒人工核对；关键词线索不能证明事实完整，回答后仍需确认入库。")
        if len({s["text"] for s in suggestions}) < 3:
            warnings.append("可用事实较少，部分建议相同；不会为凑齐三种措辞添加未经核验的内容。")
        if paragraph.strip() and paragraph.strip() not in {s["text"] for s in suggestions}:
            warnings.append("手工输入尚未自动核验；建议基于所选事实重新组织，不会将输入直接认定为真实经历。")
    structured = structure_job_locally(jd_text)
    gates = match_job_locally(profile, structured).hard_gates
    return {"suggestions": suggestions, "coverage": coverage,
            "hard_gates": [{"requirement": g.requirement, "status": g.status.value, "explanation": g.explanation} for g in gates],
            "warnings": warnings, "engine": "local"}
