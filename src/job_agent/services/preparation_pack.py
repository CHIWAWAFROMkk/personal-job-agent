from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from job_agent.models.application_tracking import ApplicationStatus
from job_agent.models.interview_debrief import InterviewDebriefRecord
from job_agent.models.job import HardGateStatus, MatchResult, MatchStatus
from job_agent.models.job_record import JobDetail
from job_agent.models.preparation import (
    CapabilityGap,
    InterviewQuestion,
    LearningPlan,
    LearningTask,
    PreparationJobSnapshot,
    PreparationPack,
    PreparationPriority,
    RoleKnowledge,
    StarPreparationCard,
)
from job_agent.models.profile import EvidenceFact, Experience, Profile


class PreparationPackError(RuntimeError):
    pass


PREPARATION_TRIGGER_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {
        "resume_requested",
        "screening",
        "assessment",
        "written_test",
        "interview_1",
        "interview_2",
        "final_interview",
    }
)


@dataclass(frozen=True)
class PreparationPackFiles:
    directory: Path
    pack_json: Path
    markdown: Path
    jd_text: Path


def should_auto_generate_preparation(status: ApplicationStatus) -> bool:
    return status in PREPARATION_TRIGGER_STATUSES


def preparation_priority_for_status(
    status: ApplicationStatus,
    match_score: int,
) -> PreparationPriority:
    if status in {"offer", "rejected", "withdrawn", "no_response"}:
        return "closed"
    priority: PreparationPriority = {
        "saved": "routine",
        "ready_to_apply": "routine",
        "applied": "routine",
        "hr_read": "routine",
        "resume_requested": "high",
        "screening": "high",
        "assessment": "urgent",
        "written_test": "urgent",
        "interview_1": "critical",
        "interview_2": "critical",
        "final_interview": "critical",
    }[status]
    if priority == "routine" and match_score >= 85:
        return "elevated"
    return priority


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _clean_responsibility(value: str) -> str:
    value = re.sub(r"^[\s\d.、（）()\-—]+", "", value).strip()
    return value.rstrip("。；; ")


def _ready_fact_contexts(profile: Profile) -> list[tuple[Experience, EvidenceFact]]:
    return [
        (experience, fact)
        for experience in profile.experiences
        for fact in experience.facts
        if profile.is_application_ready(fact.status)
    ]


def _fact_terms(fact: EvidenceFact) -> list[str]:
    terms = [*fact.skills, *fact.tools]
    terms.extend(re.findall(r"[A-Za-z][A-Za-z0-9+.#/-]{1,}|[\u4e00-\u9fff]{2,6}", fact.statement))
    return _dedupe(terms)


def _related_fact_ids(
    text: str,
    fact_contexts: list[tuple[Experience, EvidenceFact]],
) -> list[str]:
    lowered = text.casefold()
    matches: list[str] = []
    domain_markers = (
        "数据",
        "分析",
        "活动",
        "项目",
        "沟通",
        "跨部门",
        "ai",
        "代码",
        "编程",
        "自动化",
        "sop",
        "调研",
        "流程",
    )
    for _, fact in fact_contexts:
        terms = _fact_terms(fact)
        fact_text = " ".join([fact.statement, *fact.skills, *fact.tools]).casefold()
        if any(
            term.casefold() in lowered or lowered in term.casefold()
            for term in terms
            if len(term.strip()) >= 2
        ) or any(marker in lowered and marker in fact_text for marker in domain_markers):
            matches.append(fact.id)
    return _dedupe(matches)


def _build_capability_gaps(
    profile: Profile,
    result: MatchResult,
) -> list[CapabilityGap]:
    facts = _ready_fact_contexts(profile)
    known_fact_ids = {fact.id for _, fact in facts}
    items: list[CapabilityGap] = []
    seen: set[str] = set()

    def add(item: CapabilityGap) -> None:
        key = re.sub(r"\s+", "", item.requirement).casefold()
        if key and key not in seen:
            seen.add(key)
            items.append(item)

    for evidence in result.evidence:
        mastery = {
            MatchStatus.MATCHED: "mastered",
            MatchStatus.PARTIAL: "partial",
            MatchStatus.GAP: "not_evidenced",
            MatchStatus.UNKNOWN: "unknown",
        }[evidence.status]
        fact_ids = [
            fact_id for fact_id in evidence.profile_fact_ids if fact_id in known_fact_ids
        ]
        category = (
            "required_skill"
            if evidence.requirement in result.job.required_skills
            else "preferred_skill"
        )
        add(
            CapabilityGap(
                requirement=evidence.requirement,
                category=category,
                mastery=mastery,
                evidence_fact_ids=fact_ids,
                explanation=evidence.explanation,
                learning_priority={
                    "not_evidenced": 1,
                    "unknown": 2,
                    "partial": 3,
                    "mastered": 5,
                }[mastery],
            )
        )

    for gate in result.hard_gates:
        mastery = {
            HardGateStatus.PASSES: "mastered",
            HardGateStatus.FAILS: "not_evidenced",
            HardGateStatus.UNKNOWN: "unknown",
        }[gate.status]
        fact_ids = [
            fact_id for fact_id in gate.profile_fact_ids if fact_id in known_fact_ids
        ]
        add(
            CapabilityGap(
                requirement=gate.requirement,
                category="hard_gate",
                mastery=mastery,
                evidence_fact_ids=fact_ids,
                explanation=gate.explanation,
                learning_priority={
                    "not_evidenced": 1,
                    "unknown": 2,
                    "mastered": 5,
                }[mastery],
            )
        )

    for requirement in result.job.requirements:
        fact_ids = _related_fact_ids(requirement.text, facts)
        lowered = requirement.text.casefold()
        availability = profile.job_search.availability
        approved_education = [
            item
            for item in profile.education
            if profile.is_application_ready(item.status)
        ]
        mastery = "partial" if fact_ids else "unknown"
        explanation = (
            "找到相关的已确认经历，但是否达到 JD 所要求的熟练程度仍需面试举证。"
            if fact_ids
            else "Profile 中没有足够信息可靠判断；这不等同于确认不会。"
        )
        learning_priority = 3 if fact_ids else 2

        matching_skill_evidence = [
            evidence
            for evidence in result.evidence
            if evidence.status == MatchStatus.MATCHED
            and any(
                token.casefold() in lowered
                for token in re.split(r"[/、\s]+", evidence.requirement)
                if len(token.strip()) >= 2
            )
        ]
        if matching_skill_evidence:
            mastery = "mastered"
            fact_ids = _dedupe(
                [
                    *fact_ids,
                    *[
                        fact_id
                        for evidence in matching_skill_evidence
                        for fact_id in evidence.profile_fact_ids
                        if fact_id in known_fact_ids
                    ],
                ]
            )
            explanation = "JD 相关能力已在 Profile 中找到明确的已确认技能或经历证据。"
            learning_priority = 5

        day_match = re.search(r"每周[^\d\n]{0,12}(\d)\s*[天日]", lowered)
        month_match = re.search(r"(?:至少|不少于|连续)?\s*(\d+)\s*个?月", lowered)
        if requirement.category == "availability" and day_match:
            required_days = int(day_match.group(1))
            if (
                availability.days_per_week is not None
                and availability.days_per_week >= required_days
            ):
                mastery = "mastered"
                explanation = (
                    f"已确认每周可稳定到岗 {availability.days_per_week} 天，达到要求。"
                )
            else:
                explanation = "当前已确认的每周到岗范围尚不足以证明满足该要求。"
            learning_priority = 5
        elif requirement.category == "availability" and month_match:
            required_months = int(month_match.group(1))
            if (
                availability.duration_months is not None
                and availability.duration_months >= required_months
            ):
                mastery = "mastered"
                explanation = (
                    f"已确认至少可连续实习 {availability.duration_months} 个月，达到要求。"
                )
            elif (
                availability.max_duration_months is not None
                and availability.max_duration_months >= required_months
            ):
                mastery = "partial"
                explanation = (
                    f"已确认至少 {availability.duration_months or 0} 个月、最多 "
                    f"{availability.max_duration_months} 个月，仍需确认是否能承诺该时长。"
                )
            else:
                mastery = "unknown"
                confirmed = availability.duration_months
                explanation = (
                    f"目前只确认至少可实习 {confirmed} 个月，尚未确认能否达到 "
                    f"{required_months} 个月。"
                    if confirmed is not None
                    else "Profile 中尚未确认可连续实习时长。"
                )
            learning_priority = 5
        elif any(marker in lowered for marker in ("尽快入职", "立即入职", "尽快到岗", "立即到岗")):
            earliest = availability.earliest_start
            try:
                ready_now = bool(earliest and date.fromisoformat(earliest) <= date.today())
            except ValueError:
                ready_now = bool(earliest and any(marker in earliest for marker in ("立即", "尽快")))
            if ready_now:
                mastery = "mastered"
                explanation = f"已确认可从 {earliest} 起开始，满足尽快到岗要求。"
            else:
                mastery = "unknown"
                explanation = "尚未确认可立即开始的准确日期。"
            learning_priority = 5
        elif requirement.category == "major" and "不限" in lowered:
            mastery = "mastered" if approved_education else "unknown"
            explanation = (
                "JD 明确专业不限，已确认教育经历不存在专业门槛冲突。"
                if approved_education
                else "JD 专业不限，但 Profile 中尚无已确认教育经历。"
            )
            learning_priority = 5
        elif requirement.category in {"education", "major", "location"}:
            learning_priority = 5
        fact_ids = _dedupe(fact_ids)[:5]
        add(
            CapabilityGap(
                requirement=requirement.text,
                category=requirement.category,
                mastery=mastery,
                evidence_fact_ids=fact_ids,
                explanation=explanation,
                learning_priority=learning_priority,
            )
        )

    return sorted(
        items,
        key=lambda item: (item.learning_priority, item.category, item.requirement),
    )


def _likely_kpis(raw_text: str) -> list[str]:
    rules = (
        (
            ("活动", "配置", "上线"),
            "活动按期上线率、配置准确率和异常数量",
        ),
        (
            ("商业化", "充值", "付费"),
            "活动参与率、付费转化、充值金额及付费用户表现",
        ),
        (
            ("数据监控", "数据分析", "复盘"),
            "数据口径准确性、异常发现时效和复盘结论可执行性",
        ),
        (
            ("需求", "bug", "测试", "验收"),
            "需求按期交付率、Bug 闭环时效和验收通过率",
        ),
        (
            ("竞品", "调研", "行业趋势"),
            "调研覆盖度、洞察质量及建议被业务采纳的情况",
        ),
        (
            ("ai", "自动化", "sop", "提效"),
            "节省工时、错误率变化、SOP 复用次数和团队采用情况",
        ),
        (
            ("内容", "传播"),
            "内容按期交付率、触达、互动和转化表现",
        ),
    )
    lowered = raw_text.casefold()
    results = [
        description
        for markers, description in rules
        if any(marker in lowered for marker in markers)
    ]
    if not results:
        results = ["任务按期完成率、交付准确性和问题闭环时效"]
    return [
        f"{item}（基于 JD 的常见指标推断，实际口径需向招聘方确认）"
        for item in _dedupe(results)
    ]


def _workflow(raw_text: str) -> list[str]:
    lowered = raw_text.casefold()
    flows = ["先确认业务目标与成功指标，再拆解任务、执行跟进、监控结果并复盘"]
    if any(marker in lowered for marker in ("活动", "充值", "榜单")):
        flows.append("活动目标与规则 → 方案/配置 → 联调验收 → 上线监控 → 数据复盘 → 下一轮迭代")
    if any(marker in lowered for marker in ("需求", "bug", "测试验收")):
        flows.append("收集运营问题 → 明确需求与优先级 → 提报跟进 → 测试验收 → 上线后闭环")
    if any(marker in lowered for marker in ("竞品", "调研", "行业趋势")):
        flows.append("定义调研问题 → 选择样本 → 拆解玩法与指标 → 提炼洞察 → 形成可执行建议")
    if any(marker in lowered for marker in ("ai", "自动化", "sop", "提效")):
        flows.append("选择高频场景 → 记录人工基线 → 小范围试验 → 校验准确性 → 沉淀 SOP → 量化提效")
    return _dedupe(flows)


def _tools(result: MatchResult) -> list[str]:
    tools = [*result.job.tools]
    raw = result.job.raw_text.casefold()
    known_tools = {
        "excel": "Excel",
        "sql": "SQL",
        "python": "Python",
        "tableau": "Tableau",
        "power bi": "Power BI",
        "codex": "Codex",
    }
    for marker, display in known_tools.items():
        if marker in raw:
            tools.append(display)
    if re.search(r"\bai\b|人工智能|大模型", raw, re.I):
        tools.append("AI 工具（JD 明示，但具体产品需确认）")
    return _dedupe(tools) or ["JD 未明确指定工具，面试时需确认团队实际工具栈"]


def _industry_knowledge(raw_text: str) -> list[str]:
    lowered = raw_text.casefold()
    rules = (
        (("商业化", "付费", "充值"), "互联网商业化漏斗：曝光、参与、付费转化、留存与收入"),
        (("国际化", "海外"), "国际化运营中的区域差异、本地化、时区与合规意识"),
        (("社交", "soul"), "社交产品的用户关系、内容/互动生态与社区安全边界"),
        (("活动", "榜单"), "活动运营：目标、规则、激励、配置、风控、监控和复盘"),
        (("竞品", "行业趋势"), "竞品研究：对象选择、玩法拆解、指标假设和可迁移结论"),
        (("需求", "bug", "测试验收"), "产品需求协作：需求文档、优先级、测试用例、验收与问题闭环"),
        (("ai", "自动化", "sop"), "AI 提效：场景选择、提示设计、结果校验、隐私边界和 SOP 沉淀"),
        (("数据", "分析", "监控"), "运营数据基础：指标口径、维度拆解、异常定位和复盘表达"),
    )
    topics = [
        topic
        for markers, topic in rules
        if any(marker in lowered for marker in markers)
    ]
    return _dedupe(topics) or ["岗位所在业务的用户、产品、收入来源和核心运营指标"]


def _build_role_knowledge(result: MatchResult) -> RoleKnowledge:
    requirement_keys = {
        re.sub(r"\s+", "", requirement.text).casefold()
        for requirement in result.job.requirements
    }
    requirement_evidence_keys = {
        re.sub(r"\s+", "", _clean_responsibility(requirement.jd_evidence)).casefold()
        for requirement in result.job.requirements
        if requirement.jd_evidence
    }
    responsibilities = []
    for value in result.job.responsibilities:
        cleaned = _clean_responsibility(value)
        key = re.sub(r"\s+", "", cleaned).casefold()
        if key not in requirement_keys and key not in requirement_evidence_keys:
            responsibilities.append(cleaned)
    responsibilities = _dedupe(responsibilities)
    if not responsibilities:
        responsibilities = ["JD 未提供可可靠拆解的职责，需要在沟通中补充确认"]
    daily_work = [f"围绕“{item}”推进具体任务并记录结果" for item in responsibilities[:6]]
    return RoleKnowledge(
        source_note=(
            "岗位职责和明确工具来自 JD；KPI、流程及行业知识为基于 JD 的常见工作推断，"
            "不代表该公司已公开确认。"
        ),
        actual_work=responsibilities,
        daily_work=daily_work,
        likely_kpis=_likely_kpis(result.job.raw_text),
        workflow=_workflow(result.job.raw_text),
        tools=_tools(result),
        industry_knowledge=_industry_knowledge(result.job.raw_text),
    )


def _task(
    sequence: int,
    title: str,
    minutes: int,
    objective: str,
    deliverable: str,
    requirements: list[str],
) -> LearningTask:
    return LearningTask(
        sequence=sequence,
        title=title,
        minutes=minutes,
        objective=objective,
        deliverable=deliverable,
        related_requirements=requirements,
    )


def _build_learning_plans(
    knowledge: RoleKnowledge,
    gaps: list[CapabilityGap],
) -> list[LearningPlan]:
    priority_gaps = [
        item.requirement
        for item in gaps
        if item.mastery in {"not_evidenced", "unknown"}
        and item.category in {"required_skill", "preferred_skill", "tool"}
    ][:3]
    experience_topics = [
        item.requirement
        for item in gaps
        if item.mastery in {"not_evidenced", "unknown", "partial"}
        and item.category == "experience"
    ][:1]
    topics = _dedupe(
        [*priority_gaps, *knowledge.industry_knowledge[:3], *experience_topics]
    )
    primary = topics[0] if topics else "岗位核心工作"
    secondary = topics[1] if len(topics) > 1 else primary
    role_work = knowledge.actual_work[0]

    one_hour = LearningPlan(
        horizon="1_hour",
        total_minutes=60,
        outcome="能用自己的话说明岗位、核心指标、最大缺口，并完成一轮口头回答。",
        tasks=[
            _task(1, "画岗位工作链路", 10, f"理解“{role_work}”在业务中的位置。", "一张 5 步工作链路", []),
            _task(2, "补第一能力缺口", 15, f"掌握“{primary}”的基础概念和常见方法。", "5 个关键词及各自解释", [primary]),
            _task(3, "记住核心指标", 15, "理解指标含义、影响因素和异常拆解顺序。", "3 个指标的口径与分析思路", []),
            _task(4, "完成面试速练", 20, "练习岗位理解、动机和一段真实 STAR 素材。", "录音回答 3 题并复听一次", [primary]),
        ],
    )
    one_day = LearningPlan(
        horizon="1_day",
        total_minutes=360,
        outcome="能完成一份岗位分析、一个小案例和一轮结构化模拟面试。",
        tasks=[
            _task(1, "岗位与业务拆解", 45, "梳理用户、场景、目标、流程和协作方。", "一页岗位地图", []),
            _task(2, "核心指标训练", 60, "学会从结果指标向过程指标逐层拆解。", "指标树与异常排查清单", []),
            _task(3, "重点能力专项", 75, f"集中补齐“{primary}”。", "概念笔记和一个练习", [primary]),
            _task(4, "第二能力专项", 60, f"建立“{secondary}”的基本工作框架。", "标准步骤和检查清单", [secondary]),
            _task(5, "岗位案例演练", 60, "按目标—分析—方案—指标—风险完成案例。", "一份 5 分钟案例答案", [primary, secondary]),
            _task(6, "STAR 与模拟面试", 60, "把真实经历整理成可追问的回答。", "2 张 STAR 卡和一轮模拟面试", []),
        ],
    )
    three_days = LearningPlan(
        horizon="3_days",
        total_minutes=1080,
        outcome="形成可展示的岗位小作品，并能应对专业、行为和案例追问。",
        tasks=[
            _task(1, "业务与竞品基础", 120, "建立行业、用户和商业模式框架。", "业务地图与竞品对比表", []),
            _task(2, "数据与指标专项", 180, "练习指标口径、拆解、监控和复盘。", "一份指标分析练习", []),
            _task(3, "首要缺口训练", 180, f"从概念到实操补齐“{primary}”。", "一个可复述的实操案例", [primary]),
            _task(4, "次要缺口训练", 180, f"从概念到实操补齐“{secondary}”。", "一份流程或 SOP", [secondary]),
            _task(5, "岗位小作品", 180, "针对 JD 设计一份可讨论的业务方案。", "5—8 页方案或结构化文档", [primary, secondary]),
            _task(6, "两轮模拟面试", 240, "覆盖简历深挖、专业问题和压力追问。", "问题清单、录音和修订版答案", []),
        ],
    )
    seven_days = LearningPlan(
        horizon="7_days",
        total_minutes=2520,
        outcome="系统掌握岗位基础，完成作品集级案例，并通过多轮模拟持续修正表达。",
        tasks=[
            _task(1, "行业与公司研究", 300, "理解行业结构、用户、产品和收入逻辑。", "研究简报与待确认问题", []),
            _task(2, "岗位方法论", 420, "系统学习岗位工作流和常见决策框架。", "个人岗位手册", []),
            _task(3, "数据能力实战", 420, "完成从问题定义到分析结论的闭环。", "数据练习与复盘报告", []),
            _task(4, "重点缺口实战", 420, f"针对“{primary}”完成可验证练习。", "实操作品与 SOP", [primary]),
            _task(5, "完整岗位案例", 420, "综合业务、数据、执行、风险和复盘。", "作品集级岗位方案", [primary, secondary]),
            _task(6, "面试迭代", 540, "进行多轮模拟并针对薄弱问题复训。", "答案库、3 轮录音和最终复盘", []),
        ],
    )
    return [one_hour, one_day, three_days, seven_days]


def _relevant_fact_contexts(
    profile: Profile,
    result: MatchResult,
) -> list[tuple[Experience, EvidenceFact]]:
    contexts = _ready_fact_contexts(profile)
    preferred_ids = {
        fact_id for evidence in result.evidence for fact_id in evidence.profile_fact_ids
    }

    def score(context: tuple[Experience, EvidenceFact]) -> tuple[int, str]:
        _, fact = context
        value = 10 if fact.id in preferred_ids else 0
        value += sum(
            1
            for term in _fact_terms(fact)
            if len(term) >= 2 and term.casefold() in result.job.raw_text.casefold()
        )
        return value, fact.id

    return sorted(contexts, key=score, reverse=True)


def _build_interview_questions(
    profile: Profile,
    result: MatchResult,
    gaps: list[CapabilityGap],
) -> list[InterviewQuestion]:
    questions: list[InterviewQuestion] = [
        InterviewQuestion(
            category="motivation",
            question=f"为什么选择 {result.job.company} 和 {result.job.title}？",
            answer_framework=["先说对 JD 核心工作的理解", "再连接一到两条真实经历", "最后说明希望补强的能力与贡献方式"],
            truthfulness_guard="不要编造公司内部战略、文化或未公开业务数据。",
        ),
        InterviewQuestion(
            category="role",
            question="你如何理解这个岗位？你认为最重要的 KPI 是什么？",
            answer_framework=["按目标—用户/对象—流程—协作方描述岗位", "给出 1 个结果指标和 2 个过程指标", "明确说明指标是基于 JD 的推断并反问实际口径"],
            truthfulness_guard="不要把推断的 KPI 说成公司已经确认的考核标准。",
        ),
    ]
    for responsibility in result.job.responsibilities[:3]:
        clean = _clean_responsibility(responsibility)
        questions.append(
            InterviewQuestion(
                category="professional",
                question=f"如果让你负责“{clean}”，你会如何开始？",
                answer_framework=["先确认目标、范围与约束", "拆解执行步骤、协作方和时间节点", "设计监控指标、风险预案和复盘方式"],
                truthfulness_guard="可以给方法和假设，但不要声称自己做过未发生的同类项目。",
            )
        )

    relevant_facts = _relevant_fact_contexts(profile, result)[:4]
    for experience, fact in relevant_facts:
        questions.append(
            InterviewQuestion(
                category="resume",
                question=f"请具体讲讲你在{experience.organization or '相关经历'}中提到的：{fact.statement}",
                answer_framework=["Situation：补充真实背景和约束", "Task：说明自己的职责与目标", "Action：按步骤讲本人实际动作", "Result：只使用可验证结果，并补充复盘"],
                evidence_fact_ids=[fact.id],
                truthfulness_guard="只围绕该事实和本人能解释的细节展开；没有数字就不要临时创造。",
            )
        )

    for gap in [
        item
        for item in gaps
        if item.mastery != "mastered"
        and item.category in {"required_skill", "preferred_skill", "tool"}
    ][:2]:
        questions.append(
            InterviewQuestion(
                category="professional",
                question=f"你对“{gap.requirement}”掌握到什么程度？",
                answer_framework=["先如实说明当前掌握边界", "再讲已具备的相邻能力或学习证据", "给出上手计划和一个可验证的练习目标"],
                evidence_fact_ids=gap.evidence_fact_ids,
                truthfulness_guard="不会的内容直接承认，不要用相邻经历冒充熟练掌握。",
            )
        )

    questions.extend(
        [
            InterviewQuestion(
                category="behavioral",
                question="讲一个你主动承担责任并把事情推进闭环的例子。",
                answer_framework=["选择一条已确认的真实经历", "讲清阻力、本人动作、协作方式和结果", "说明复盘后改变了什么"],
                evidence_fact_ids=[relevant_facts[0][1].id] if relevant_facts else [],
                truthfulness_guard="团队结果与个人贡献要分开表述。",
            ),
            InterviewQuestion(
                category="behavioral",
                question="遇到多任务、紧期限或跨部门分歧时，你怎么处理？",
                answer_framework=["说明优先级判断标准", "列出沟通与风险升级方式", "用真实经历验证，而非只说态度"],
                evidence_fact_ids=[relevant_facts[1][1].id] if len(relevant_facts) > 1 else [],
                truthfulness_guard="不要虚构冲突或夸大个人决策权限。",
            ),
        ]
    )

    raw = result.job.raw_text.casefold()
    if any(marker in raw for marker in ("活动", "充值", "榜单", "商业化")):
        questions.append(
            InterviewQuestion(
                category="case",
                question="请设计一个商业化活动，并说明目标用户、规则、指标、风险和复盘方式。",
                answer_framework=["明确业务目标与用户分层", "设计机制、资源、节奏和触达", "给出结果/过程/护栏指标", "考虑作弊、体验、合规和成本", "说明复盘与迭代"],
                truthfulness_guard="这是案例假设，要明确区分方案设计与真实项目经历。",
            )
        )
    if any(marker in raw for marker in ("数据", "监控", "分析", "复盘")):
        questions.append(
            InterviewQuestion(
                category="case",
                question="某核心指标突然下降，你会如何定位原因？",
                answer_framework=["先确认口径、数据质量和时间范围", "按用户、渠道、地区、版本、环节拆解", "结合事件与定性信息提出假设", "设计验证并给出行动优先级"],
                truthfulness_guard="没有数据时给分析框架，不要伪造结论。",
            )
        )
    return questions


def _build_star_cards(
    profile: Profile,
    result: MatchResult,
) -> list[StarPreparationCard]:
    cards: list[StarPreparationCard] = []
    for experience, fact in _relevant_fact_contexts(profile, result)[:4]:
        label = " / ".join(filter(None, (experience.organization, experience.role)))
        cards.append(
            StarPreparationCard(
                title=label or fact.id,
                fact_id=fact.id,
                confirmed_evidence=fact.statement,
                situation_prompt="当时的真实背景、对象、时间和限制是什么？",
                task_prompt="你的具体职责和成功标准是什么？",
                action_prompt="你本人按什么顺序做了哪些动作，为什么这样做？",
                result_prompt="有哪些已确认的结果、反馈或可验证变化？没有数字时用客观结果描述。",
            )
        )
    return cards


def build_preparation_pack(
    profile: Profile,
    job: JobDetail,
    result: MatchResult,
    *,
    trigger_status: ApplicationStatus,
    generated_at: datetime | None = None,
    debriefs: list[InterviewDebriefRecord] | None = None,
) -> PreparationPack:
    generated_at = generated_at or datetime.now(UTC)
    if result.job.company == "未识别":
        result.job.company = job.company
    if result.job.title == "未识别":
        result.job.title = job.title
    gaps = _build_capability_gaps(profile, result)
    knowledge = _build_role_knowledge(result)
    star_cards = _build_star_cards(profile, result)
    evidence_statements = {
        card.fact_id: card.confirmed_evidence for card in star_cards
    }
    return PreparationPack(
        debriefs=[entry for entry in (debriefs or []) if entry.job_id == job.job_id],
        generated_at=generated_at,
        job=PreparationJobSnapshot(
            job_id=job.job_id,
            company=job.company,
            title=job.title,
            location=job.location,
            match_score=result.overall_score,
            trigger_status=trigger_status,
            priority=preparation_priority_for_status(
                trigger_status,
                result.overall_score,
            ),
        ),
        role_knowledge=knowledge,
        capability_gaps=gaps,
        learning_plans=_build_learning_plans(knowledge, gaps),
        interview_questions=_build_interview_questions(profile, result, gaps),
        star_cards=star_cards,
        evidence_statements=evidence_statements,
        truthfulness_notes=[
            "只把 Profile 中 documented 或 user_confirmed 的事实作为个人经历证据。",
            "未找到证据不等于确认不会，学习包会明确标记为待确认或未找到已确认证据。",
            "公司 KPI、流程和行业知识若非 JD 明示，均标注为推断，面试前需要核实。",
            "STAR 卡只提供追问框架，不会补写未经确认的数字、结果或个人贡献。",
        ],
    )


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _bullets(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values) or "- 暂无。"


def render_preparation_pack_markdown(pack: PreparationPack) -> str:
    status_labels = {
        "saved": "已收藏",
        "ready_to_apply": "待投递",
        "applied": "已投递",
        "hr_read": "HR 已读",
        "resume_requested": "索要简历",
        "screening": "筛选中",
        "assessment": "测评",
        "written_test": "笔试",
        "interview_1": "一面",
        "interview_2": "二面",
        "final_interview": "终面",
        "offer": "Offer",
        "rejected": "拒绝",
        "withdrawn": "撤回",
        "no_response": "长期无反馈",
    }
    priority_labels = {
        "routine": "常规",
        "elevated": "提高",
        "high": "高",
        "urgent": "紧急",
        "critical": "最高",
        "closed": "已结束",
    }
    mastery_labels = {
        "mastered": "已掌握/已满足",
        "partial": "部分掌握",
        "not_evidenced": "未找到已确认证据",
        "unknown": "待确认",
    }
    horizon_labels = {
        "1_hour": "1 小时速成",
        "1_day": "1 天准备",
        "3_days": "3 天准备",
        "7_days": "7 天系统学习",
    }

    gap_rows = [
        "| 优先级 | JD 要求 | 当前判断 | 证据 | 说明 |",
        "|---:|---|---|---|---|",
    ]
    for gap in pack.capability_gaps:
        evidence = "、".join(gap.evidence_fact_ids) or "—"
        gap_rows.append(
            f"| {gap.learning_priority} | {_escape(gap.requirement)} | "
            f"{mastery_labels[gap.mastery]} | {_escape(evidence)} | "
            f"{_escape(gap.explanation)} |"
        )

    plan_sections: list[str] = []
    for plan in pack.learning_plans:
        tasks = [
            f"{task.sequence}. **{task.title}（{task.minutes} 分钟）**："
            f"{task.objective} 产出：{task.deliverable}"
            for task in plan.tasks
        ]
        plan_sections.append(
            f"### {horizon_labels[plan.horizon]}\n\n"
            f"目标：{plan.outcome}\n\n" + "\n".join(tasks)
        )

    question_sections: list[str] = []
    category_labels = {
        "motivation": "动机",
        "role": "岗位理解",
        "professional": "专业",
        "resume": "简历追问",
        "behavioral": "行为",
        "case": "案例",
    }
    for index, question in enumerate(pack.interview_questions, start=1):
        framework = "；".join(question.answer_framework)
        evidence = "、".join(question.evidence_fact_ids)
        evidence_line = f"\n   - 可用事实：{evidence}" if evidence else ""
        question_sections.append(
            f"{index}. **[{category_labels[question.category]}] {question.question}**\n"
            f"   - 回答框架：{framework}{evidence_line}\n"
            f"   - 真实性边界：{question.truthfulness_guard}"
        )

    star_sections = [
        f"### {card.title}\n\n"
        f"- 事实 ID：`{card.fact_id}`\n"
        f"- 已确认事实：{card.confirmed_evidence}\n"
        f"- S：{card.situation_prompt}\n"
        f"- T：{card.task_prompt}\n"
        f"- A：{card.action_prompt}\n"
        f"- R：{card.result_prompt}"
        for card in pack.star_cards
    ]

    debrief_sections = [
        f"### 复盘 #{entry.debrief_id} · {status_labels[entry.stage]}\n\n"
        f"记录时间：{entry.created_at}\n\n"
        f"问题：{entry.question}\n\n"
        f"原回答：{entry.answer}\n\n"
        f"改进草稿：{entry.better_answer or '未填写'}\n\n"
        f"引用事实 ID：{'、'.join(entry.evidence_fact_ids) or '未引用'}\n\n"
        f"做得好的地方：\n\n{_bullets(entry.strengths)}\n\n"
        f"待改进：\n\n{_bullets(entry.gaps)}\n\n"
        f"后续行动：\n\n{_bullets(entry.next_actions)}"
        for entry in pack.debriefs
    ]

    return f"""# {pack.job.company}｜{pack.job.title}｜岗位学习与面试准备包

> 岗位编号：#{pack.job.job_id}　匹配分：{pack.job.match_score}/100　当前进度：{status_labels[pack.job.trigger_status]}　准备优先级：{priority_labels[pack.job.priority]}

## 使用说明

{pack.role_knowledge.source_note}

## 岗位实际做什么

{_bullets(pack.role_knowledge.actual_work)}

## 可能的日常工作

{_bullets(pack.role_knowledge.daily_work)}

## 核心 KPI

{_bullets(pack.role_knowledge.likely_kpis)}

## 常见工作流程

{_bullets(pack.role_knowledge.workflow)}

## 工具要求

{_bullets(pack.role_knowledge.tools)}

## 需要补充的行业知识

{_bullets(pack.role_knowledge.industry_knowledge)}

## JD 要求与我的能力

> 数字 1 表示最优先补齐；“未找到已确认证据”只表示当前 Profile 不能证明，不武断等同于完全不会。

{chr(10).join(gap_rows)}

## 学习计划

{chr(10).join(plan_sections)}

## 面试问题与回答思路

{chr(10).join(question_sections)}

## STAR 素材卡

{chr(10).join(star_sections) or '暂无可用的已确认事实；需要先补充真实经历。'}

## 真实性规则

{_bullets(pack.truthfulness_notes)}

## 历史面试复盘（本人记录，改进回答仍需审阅）

{chr(10).join(debrief_sections) or '暂无历史复盘。'}
"""


def write_preparation_pack(
    pack: PreparationPack,
    job: JobDetail,
    output_dir: Path,
) -> PreparationPackFiles:
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise PreparationPackError(f"输出目录已存在，拒绝覆盖: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent)
    )
    try:
        pack_json = temporary / "preparation-pack.json"
        markdown = temporary / "岗位学习与面试准备.md"
        jd_text = temporary / "JD.txt"
        pack_json.write_text(pack.model_dump_json(indent=2) + "\n", encoding="utf-8")
        markdown.write_text(render_preparation_pack_markdown(pack), encoding="utf-8")
        jd_text.write_text(job.jd_text.rstrip() + "\n", encoding="utf-8")
        temporary.replace(output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return PreparationPackFiles(
        directory=output_dir,
        pack_json=output_dir / "preparation-pack.json",
        markdown=output_dir / "岗位学习与面试准备.md",
        jd_text=output_dir / "JD.txt",
    )
