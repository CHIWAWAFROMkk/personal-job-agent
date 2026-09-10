from __future__ import annotations

import re

from job_agent.models.resume_audit import (
    ResumeAuditFinding,
    ResumeAuditMetrics,
    ResumeAuditReport,
)
from job_agent.services.resume_reader import ResumeDocument


_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d(?:[-\s]?\d){8}(?!\d)")
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BULLET_RE = re.compile(r"^[\s\-–—•·▪◦*]+")
_HEADING_RE = re.compile(
    r"^(个人信息|联系方式|求职意向|教育背景|教育经历|实习经历|工作经历|项目经历|"
    r"校园经历|实践经历|技能|专业技能|技能证书|证书|获奖经历|自我评价)[:：]?$"
)
_SECTION_PATTERNS = {
    "education": re.compile(r"教育背景|教育经历|学历"),
    "experience": re.compile(r"实习经历|工作经历|项目经历|校园经历|实践经历"),
    "skills": re.compile(r"专业技能|技能证书|技能|证书"),
}
_PLACEHOLDER_PATTERNS = (
    re.compile(r"(?i)\b(?:TBD|TODO|N/?A)\b"),
    re.compile(r"请填写|待填写|此处填写|示例内容"),
    re.compile(r"(?i)test@example\.com"),
    re.compile(r"(?<!\d)138(?:[-\s]?0){8}(?!\d)"),
    re.compile(r"(?i)(?:姓名|公司|学校)[:：]?\s*[xX]{2,}"),
)
_GENERIC_PHRASES = (
    "认真负责",
    "吃苦耐劳",
    "抗压能力强",
    "沟通能力强",
    "团队合作精神",
    "学习能力强",
    "执行力强",
    "责任心强",
)
_AIISH_PHRASES = (
    "赋能",
    "形成闭环",
    "高质量完成",
    "方法论",
    "抓手",
    "具备较强的",
    "取得了良好的效果",
)
_WEAK_PHRASES = (
    "被安排",
    "被要求",
    "协助完成",
    "参与了",
    "负责了",
)
_ACTION_PATTERN = re.compile(
    r"负责|主导|设计|搭建|开发|优化|分析|运营|策划|推进|协调|交付|完成|提升|降低|"
    r"维护|撰写|制作|调研|管理|支持|协助|参与"
)
_QUANTITY_PATTERN = re.compile(
    r"(?<!\w)\d+(?:\.\d+)?\s*(?:%|％|万|千|百|亿|人|个|项|次|天|周|月|年|小时|"
    r"分钟|家|篇|元|万元|亿元|倍|名|场|套|款|条|份|页|字|GB|MB|K|M)(?![A-Za-z])",
    re.IGNORECASE,
)


def _redact_evidence(text: str, *, limit: int = 110) -> str:
    value = _EMAIL_RE.sub("[邮箱已隐藏]", text)
    value = _PHONE_RE.sub("[手机号已隐藏]", value)
    value = _ID_RE.sub("[证件号已隐藏]", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _matching_lines(lines: list[str], phrases: tuple[str, ...]) -> list[str]:
    return [line for line in lines if any(phrase in line for phrase in phrases)]


def _unique_evidence(lines: list[str], *, limit: int = 3) -> list[str]:
    output: list[str] = []
    for line in lines:
        redacted = _redact_evidence(line)
        if redacted and redacted not in output:
            output.append(redacted)
        if len(output) >= limit:
            break
    return output


def _experience_lines(lines: list[str]) -> list[str]:
    candidates: list[str] = []
    for raw_line in lines:
        line = _BULLET_RE.sub("", raw_line).strip()
        if len(line) < 10 or _HEADING_RE.fullmatch(line):
            continue
        if _EMAIL_RE.search(line) or _PHONE_RE.search(line):
            continue
        if _ACTION_PATTERN.search(line):
            candidates.append(line)
    return candidates


def audit_resume(document: ResumeDocument) -> ResumeAuditReport:
    text = document.text
    lines = [re.sub(r"\s+", " ", item).strip() for item in text.splitlines() if item.strip()]
    findings: list[ResumeAuditFinding] = []
    strengths: list[str] = []

    placeholder_lines = [
        line for line in lines if any(pattern.search(line) for pattern in _PLACEHOLDER_PATTERNS)
    ]
    if placeholder_lines:
        findings.append(
            ResumeAuditFinding(
                code="integrity.placeholder",
                category="integrity",
                severity="high",
                title="发现疑似占位内容",
                evidence=_unique_evidence(placeholder_lines),
                recommendation="逐项替换为本人可核验的信息；不适用的字段应删除，不要保留模板示例。",
                hr_question="这些字段是尚未填写，还是导出模板时误留下的？",
            )
        )

    has_email = bool(_EMAIL_RE.search(text))
    has_phone = bool(_PHONE_RE.search(text))
    if has_email and has_phone:
        strengths.append("联系方式要素完整（报告不会保存具体号码或邮箱）。")
    else:
        missing = "、".join(label for present, label in ((has_phone, "手机号"), (has_email, "邮箱")) if not present)
        findings.append(
            ResumeAuditFinding(
                code="contact.missing",
                category="contact",
                severity="medium",
                title=f"缺少可识别的{missing}",
                recommendation="确认投递版简历包含可联系到本人的信息，并在导出后再次检查。",
            )
        )

    present_sections = {
        key: bool(pattern.search(text)) for key, pattern in _SECTION_PATTERNS.items()
    }
    section_labels = {"education": "教育经历", "experience": "实习/项目经历", "skills": "技能"}
    missing_sections = [section_labels[key] for key, present in present_sections.items() if not present]
    if not missing_sections:
        strengths.append("教育、经历与技能三个核心模块均可识别。")
    else:
        findings.append(
            ResumeAuditFinding(
                code="structure.missing_sections",
                category="structure",
                severity="high" if len(missing_sections) >= 2 else "medium",
                title="核心模块不完整",
                evidence=["缺少：" + "、".join(missing_sections)],
                recommendation="用清晰、常见的栏目标题补齐真实内容，避免让招聘者猜测信息位置。",
            )
        )

    experience_lines = _experience_lines(lines)
    quantified_lines = [line for line in experience_lines if _QUANTITY_PATTERN.search(line)]
    ratio = len(quantified_lines) / len(experience_lines) if experience_lines else 0.0
    if len(experience_lines) >= 2 and ratio >= 0.3:
        strengths.append("部分经历已用可核验的范围、规模或结果说明贡献。")
    elif len(experience_lines) >= 2:
        findings.append(
            ResumeAuditFinding(
                code="evidence.low_quantification",
                category="evidence",
                severity="medium",
                title="经历描述缺少可核验的规模或结果",
                evidence=_unique_evidence(
                    [line for line in experience_lines if line not in quantified_lines]
                ),
                recommendation=(
                    "优先补充能核验的范围、频次、对象数量、周期或结果；如果没有准确数字，"
                    "就写清职责边界和具体产出，不要编造数字。"
                ),
                hr_question="你能核实这项工作的覆盖范围、周期、产出数量或前后变化吗？",
            )
        )

    generic_lines = _matching_lines(lines, _GENERIC_PHRASES)
    if generic_lines:
        findings.append(
            ResumeAuditFinding(
                code="language.generic_claims",
                category="language",
                severity="medium",
                title="存在缺少证据的泛化自评",
                evidence=_unique_evidence(generic_lines),
                recommendation="把形容词改成具体行为、场景和可核验产出；无法举证的自评可以删掉。",
                hr_question="哪一段真实经历最能证明这项能力？",
            )
        )

    weak_lines = _matching_lines(lines, _WEAK_PHRASES)
    if weak_lines:
        findings.append(
            ResumeAuditFinding(
                code="language.weak_voice",
                category="language",
                severity="low",
                title="部分表述没有说明你的实际贡献",
                evidence=_unique_evidence(weak_lines),
                recommendation="在不夸大的前提下，说明你具体做了什么、交付了什么，以及和团队工作的边界。",
            )
        )

    aiish_lines = _matching_lines(lines, _AIISH_PHRASES)
    if aiish_lines:
        findings.append(
            ResumeAuditFinding(
                code="language.aiish_phrasing",
                category="language",
                severity="low",
                title="部分表达偏空泛或模板化",
                evidence=_unique_evidence(aiish_lines),
                recommendation="换成日常、直接的业务语言，并补充真实动作与对象。",
            )
        )

    long_lines = [line for line in lines if len(line) > 100]
    if long_lines:
        findings.append(
            ResumeAuditFinding(
                code="readability.long_lines",
                category="readability",
                severity="low",
                title="个别条目过长，不利于快速扫读",
                evidence=_unique_evidence(long_lines),
                recommendation="每条只保留一个主要动作和一个结果，必要时拆成两条。",
            )
        )

    normalized_counts: dict[str, int] = {}
    for line in lines:
        normalized = re.sub(r"[\s，。；、,.\-–—]", "", line).casefold()
        if len(normalized) >= 12:
            normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1
    duplicate_lines = [line for line in lines if normalized_counts.get(re.sub(r"[\s，。；、,.\-–—]", "", line).casefold(), 0) > 1]
    if duplicate_lines:
        findings.append(
            ResumeAuditFinding(
                code="readability.duplicate_lines",
                category="readability",
                severity="low",
                title="发现重复表述",
                evidence=_unique_evidence(duplicate_lines),
                recommendation="合并重复内容，把有限版面留给不同的证据。",
            )
        )

    weights = {"high": 15, "medium": 8, "low": 4}
    score = max(0, 100 - sum(weights[item.severity] for item in findings))
    if score >= 90:
        summary = "结构和证据表达较完整，仍需本人核对事实与版面。"
    elif score >= 75:
        summary = "基础可用，建议先处理高、中优先级问题再投递。"
    else:
        summary = "存在明显缺口，建议完成真实性核对和结构修订后再投递。"

    return ResumeAuditReport(
        source_filename=document.path.name,
        source_sha256=document.sha256,
        score=score,
        summary=summary,
        metrics=ResumeAuditMetrics(
            character_count=len(text),
            non_empty_line_count=len(lines),
            experience_line_count=len(experience_lines),
            quantified_line_count=len(quantified_lines),
            quantified_line_ratio=round(ratio, 3),
        ),
        strengths=strengths,
        findings=findings,
        extraction_warnings=list(document.warnings),
    )
