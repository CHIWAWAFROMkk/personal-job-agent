from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from job_agent.models.copilot import (
    CopilotAction,
    CopilotMessage,
    CopilotSnapshot,
    CopilotThread,
)
from job_agent.services.api_usage import load_api_usage, record_api_usage
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import load_profile, write_json_atomic
from job_agent.services.runtime_config import RuntimeConfig, effective_runtime_config


class CopilotChatError(RuntimeError):
    pass


_SUGGESTIONS = [
    "分析我的简历，给我经历写作模板",
    "今天优先推进哪几个岗位？",
    "给最高匹配岗位准备简历草稿",
    "按 JD 直接润色最高匹配岗位的简历",
    "哪些投递现在应该跟进？",
    "我下一场面试该准备什么？",
]


def _contextual_suggestions(context: dict[str, object]) -> list[str]:
    jobs = list(context.get("top_jobs", []))
    pipeline = dict(context.get("pipeline", {}))
    applied = int(pipeline.get("applied_or_later", 0))
    interviews = int(pipeline.get("interview_or_later", 0))
    suggestions: list[str] = []
    suggestions.append("分析我的简历，给我经历写作模板")
    if interviews:
        suggestions.insert(0, "我下一场面试该准备什么？")
    if applied:
        suggestions.append("哪些投递现在应该跟进？")
    top = jobs[0] if jobs else None
    if top is not None:
        suggestions.append(f"为什么推荐 #{top['job_id']} {top['company']}？")
        suggestions.append("给最高匹配岗位准备简历草稿")
        suggestions.append(f"按 JD 直接润色 #{top['job_id']} 的简历")
    else:
        suggestions.append("今天优先推进哪几个岗位？")
    for fallback in _SUGGESTIONS:
        if len(suggestions) >= 4:
            break
        if fallback not in suggestions:
            suggestions.append(fallback)
    return suggestions[:4]
_SYSTEM_PROMPT = """
你是“个人求职 Agent”内部的操作型伙伴。你可以自然地理解追问、上下文省略和用户对简历的修改要求，但事实判断只能依据 WORKSPACE_CONTEXT。

规则：
1. 先回答用户真正的问题，再给具体理由或下一步；中文自然、直接，不套固定话术，也不要机械重复岗位列表。
2. 不得虚构岗位、投递状态、经历、技能、面试反馈、公司事实或执行结果。
   resume_evidence 是已确认的脱敏经历库；pending_resume_facts 只能用于追问核实，绝不能当成已有能力或已完成成果。
3. 不得在用户执行确认按钮前声称已经投递、发送、填写、上传、修改或联系任何人。用户要求改简历时，说明可通过对话下方的确认按钮直接生成新版，不要求用户进入表单逐项修改。
4. 用户要求投递或代填时，明确说明 Agent 只打开岗位页面并准备岗位专用简历文件；登录、上传、作品集、表单与最终提交均由本人完成。
5. 需要写入或打开网页的动作只能建议用户点击界面中的确认按钮，不得绕过确认节点。
6. WORKSPACE_CONTEXT 不包含联系方式、API Key 或私有路径；不得索取或回显这些值。
7. 信息不足时明确说“当前本地记录不足”，并指出应补充的真实证据。
8. 用户要求简历分析或经历模板时，先结合完整已确认经历库和当前 JD 分析选材、职责与成果的缺口，再给具体写作帮助。
   分成“基于真实素材的改写”和“待填写的写作模板”。真实改写应标明来源经历及 fact_id，不补造指标、工具、角色或结果。
   示例可展示与岗位相关的写法，但必须明确标注“写作示例，非本人经历”，所有未知事实使用 [本人实际任务]、[实际工具]、[可核实结果] 等占位符。
   不把假设的公司、数字或经历表述为使用者已有事实，不自动保存示例到画像或正式简历。给出 2 至 3 个有针对性的核实问题。
9. 简历分析可以在未选择岗位时进行；有 JD 时围绕该岗位，没有时依据真实画像说明适用方向。
10. current_resume_draft 是当前岗位最新草稿，可能包含用户手工编辑；用它诊断表达和选材，事实仍须回到 resume_evidence 核对。没有草稿时基于经历库分析，不声称看过一份不存在的简历。
""".strip()


def _new_thread() -> CopilotThread:
    now = datetime.now(UTC)
    return CopilotThread(
        thread_id=secrets.token_urlsafe(18),
        created_at=now,
        updated_at=now,
    )


def load_copilot_thread(path: Path) -> CopilotThread:
    if not path.is_file():
        return _new_thread()
    try:
        return CopilotThread.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise CopilotChatError(f"本地对话记录无法读取: {exc}") from exc


def save_copilot_thread(thread: CopilotThread, path: Path) -> Path:
    trimmed = thread.model_copy(
        update={
            "updated_at": datetime.now(UTC),
            "messages": thread.messages[-60:],
        }
    )
    try:
        return write_json_atomic(trimmed.model_dump(mode="json"), path)
    except OSError as exc:
        raise CopilotChatError(f"本地对话记录无法保存: {exc}") from exc


def reset_copilot_thread(path: Path) -> CopilotThread:
    thread = _new_thread()
    save_copilot_thread(thread, path)
    return thread


def _safe_profile_context(profile_path: Path) -> dict[str, object] | None:
    if not profile_path.is_file():
        return None
    profile = load_profile(profile_path)
    availability = profile.job_search.availability
    commute = profile.job_search.commute
    return {
        "stage": profile.job_search.stage,
        "target_roles": profile.job_search.target_roles,
        "adjacent_roles": profile.job_search.adjacent_roles,
        "target_industries": profile.job_search.target_industries,
        "preferred_locations": profile.job_search.preferred_locations,
        "employment_types": profile.job_search.employment_types,
        "must_haves": profile.job_search.must_haves,
        "avoid": profile.job_search.avoid,
        "availability": {
            "earliest_start": availability.earliest_start,
            "days_per_week": availability.days_per_week,
            "duration_months": availability.duration_months,
        },
        "commute": {
            "max_one_way_minutes": commute.max_one_way_minutes,
            "transport_modes": commute.transport_modes,
            "remote_acceptable": commute.remote_acceptable,
        },
        "resume_count": sum(
            source.kind == "resume" for source in profile.source_documents
        ),
        "confirmed_fact_count": sum(
            profile.is_application_ready(fact.status)
            for experience in profile.experiences
            for fact in experience.facts
        ),
        "confirmed_skill_count": sum(
            profile.is_application_ready(skill.status) for skill in profile.skills
        ),
        "resume_evidence": {key: value for key, value in profile.application_context().items()
                            if key in {"education", "experiences", "skills", "stories"}},
        "pending_resume_facts": [
            {"id": fact.id, "statement": fact.statement, "status": "needs_confirmation"}
            for experience in profile.experiences for fact in experience.facts
            if not profile.is_application_ready(fact.status)
        ],
    }


def build_safe_workspace_context(
    repository: JobRepository,
    *,
    profile_path: Path,
    selected_job_id: int | None = None,
) -> dict[str, object]:
    repository.verify()
    jobs = repository.list_jobs(limit=1000)
    applications = repository.list_applications(limit=1000)
    application_by_job = {item.job_id: item for item in applications}
    ignored = repository.archived_jobs()
    ranked_jobs = sorted(
        [job for job in jobs if job.job_id not in ignored],
        key=lambda item: (-(item.match_score if item.match_score is not None else -1), -item.job_id),
    )
    top_jobs = []
    top_ranked = ranked_jobs[:12]
    if selected_job_id is not None:
        selected = next((job for job in jobs if job.job_id == selected_job_id), None)
        if selected is None:
            raise CopilotChatError("所选职位不存在，请刷新职位列表。")
        if selected not in top_ranked:
            top_ranked.append(selected)
    insights = repository.get_latest_match_insights(
        [job.job_id for job in top_ranked]
    )
    for job in top_ranked:
        application = application_by_job.get(job.job_id)
        entry: dict[str, object] = {
            "job_id": job.job_id,
            "company": job.company,
            "title": job.title,
            "location": job.location,
            "match_score": job.match_score,
            "recommendation": job.recommendation,
            "status": application.status if application else job.status,
            "commute_minutes": job.commute_minutes,
        }
        insight = insights.get(job.job_id)
        if insight:
            entry["match_insight"] = insight
        top_jobs.append(entry)
    selected_job = None
    if selected_job_id is not None:
        detail = repository.get_job(selected_job_id)
        selected_job = {
            "job_id": detail.job_id,
            "company": detail.company,
            "title": detail.title,
            "jd_text": detail.jd_text[:8000],
            "jd_truncated": len(detail.jd_text) > 8000,
        }
    summary = repository.application_summary()
    return {
        "selected_job_id": selected_job_id,
        "selected_job": selected_job,
        "profile": _safe_profile_context(profile_path),
        "top_jobs": top_jobs,
        "pipeline": {
            "total": summary.total,
            "active": summary.active,
            "applied_or_later": summary.applied_or_later,
            "hr_read_or_later": summary.hr_read_or_later,
            "meaningful_responses": summary.meaningful_responses,
            "screening_or_later": summary.screening_or_later,
            "interview_or_later": summary.interview_or_later,
            "offers": summary.offers,
            "rejected": summary.rejected,
            "no_response": summary.no_response,
            "current_status_counts": summary.current_status_counts,
        },
        "safety": {
            "final_submit": "always_user_only",
            "captcha": "manual_only",
            "salary_location_subjective": "manual_only",
            "browser_fill": "confirmed_objective_fields_only",
        },
    }


def _job_from_message(message: str, context: dict[str, object]) -> dict[str, object] | None:
    jobs = list(context.get("top_jobs", []))
    # Only treat a number as a job reference when it matches a known job id;
    # scores, dates, and quantities in the message must not be misread as ids.
    for match in re.finditer(r"(?:#|岗位\s*|职位\s*)(\d{1,9})(?!\d)", message):
        job_id = int(match.group(1))
        hit = next((job for job in jobs if int(job["job_id"]) == job_id), None)
        if hit is not None:
            return hit
        return None
    selected_id = context.get("selected_job_id")
    if selected_id is not None:
        return next((job for job in jobs if job["job_id"] == selected_id), None)
    return jobs[0] if jobs else None


def _action(
    kind: str,
    label: str,
    *,
    job_id: int | None = None,
    requires_confirmation: bool = False,
    description: str = "",
    instruction: str = "",
) -> CopilotAction:
    suffix = str(job_id) if job_id is not None else "workspace"
    return CopilotAction(
        action_id=f"{kind}-{suffix}",
        label=label,
        kind=kind,  # type: ignore[arg-type]
        job_id=job_id,
        requires_confirmation=requires_confirmation,
        description=description,
        instruction=instruction,
    )


def _local_reply(
    message: str,
    context: dict[str, object],
) -> tuple[str, list[CopilotAction]]:
    normalized = re.sub(r"\s+", "", message.casefold())
    jobs = list(context.get("top_jobs", []))
    pipeline = dict(context.get("pipeline", {}))
    profile = context.get("profile")
    target_job = _job_from_message(message, context)

    if any(term in normalized for term in ("分析简历", "分析我的简历", "经历模板", "写作模板", "写作示例", "经历怎么写")):
        evidence = profile.get("resume_evidence", {}) if isinstance(profile, dict) else {}
        experiences = evidence.get("experiences", [])
        lines = ["先把每段经历拆成任务、个人行动和可核实的结果，再按岗位要求选材。"]
        for experience in experiences[:2]:
            facts = experience.get("facts", [])
            if facts:
                fact = facts[0]
                lines.append(f"真实素材 · {experience.get('organization', '')}：{fact.get('statement', '')} [{fact.get('id', '')}]")
        lines.extend([
            "待填写的写作模板（写作示例，非本人经历）：针对 [业务问题]，我负责 [本人实际任务]，使用 [实际工具/方法] 完成 [具体行动]，形成 [可核实产出]，结果为 [真实指标及统计口径；没有数据则描述交付与验收]。",
            "补充三个信息：你具体负责哪一步？交付物是什么？结果有什么记录可以验证？模板不会自动写入个人资料或正式简历。",
        ])
        return "\n\n".join(lines), [_action("open_profile", "补充真实经历")]

    resume_revision_intent = any(
        term in normalized
        for term in ("修改简历", "改简历", "润色简历", "优化简历", "简历改成", "简历里", "简历中")
    ) or (
        "简历" in normalized
        and any(term in normalized for term in ("修改", "改写", "改为", "改成", "缩短", "精简", "润色", "优化", "突出", "删掉", "去掉", "去ai味", "空话", "增加"))
    )
    if resume_revision_intent:
        if target_job is None:
            return (
                "当前还没有可绑定的岗位。先导入完整 JD 并生成岗位专属草稿，我才能按你的话直接修改简历。",
                [_action("review_queue", "查看岗位队列")],
            )
        job_id = int(target_job["job_id"])
        return (
            f"我会把这条要求作用到 #{job_id} {target_job['company']} · {target_job['title']} 的最新草稿，"
            "结合该岗位 JD 重新组织摘要、能力词和 STAR 经历要点，并直接生成新版 DOCX/PDF。"
            "不会新增未经确认的经历或数字；生成后仍由你打开 PDF 复核。",
            [
                _action(
                    "revise_resume",
                    "确认并直接生成新版",
                    job_id=job_id,
                    requires_confirmation=True,
                    description="按本条对话修改最新岗位简历；不打开表单，不对外发送。",
                    instruction=message.strip()[:2000],
                ),
                _action("open_job", "查看对应岗位", job_id=job_id),
            ],
        )

    if any(term in normalized for term in ("简历草稿", "定制简历", "生成简历", "准备简历")):
        if target_job is None:
            return (
                "当前岗位库里还没有可绑定的正式岗位。先导入完整 JD 并完成匹配，再生成岗位专属简历草稿。",
                [_action("review_queue", "查看岗位队列")],
            )
        job_id = int(target_job["job_id"])
        return (
            f"建议先为 #{job_id} {target_job['company']} · {target_job['title']} 生成岗位专属草稿。"
            "Agent 会依据完整 JD 重排真实能力与经历，按 STAR 组织内容并生成 DOCX/PDF；生成后仍需你打开 PDF 审阅。",
            [
                _action(
                    "create_resume_draft",
                    "生成简历草稿",
                    job_id=job_id,
                    requires_confirmation=True,
                    description="本地生成，不对外发送；完成后必须本人审阅 PDF。",
                ),
                _action("open_job", "查看对应岗位", job_id=job_id),
            ],
        )

    if any(term in normalized for term in ("代填", "填申请", "自动填")):
        if target_job is None:
            return (
                "当前没有可进入人工投递准备的岗位。该流程必须绑定正式岗位、岗位链接和已批准的定向简历。",
                [_action("review_queue", "查看岗位队列")],
            )
        job_id = int(target_job["job_id"])
        return (
            f"可以为 #{job_id} 打开招聘页面，并在资源管理器中选中哈希核验后的岗位专属 PDF。"
            "Agent 不会填写或上传网页字段；登录、作品集、筛选题和最终提交都由你本人完成。",
            [
                _action(
                    "start_safe_fill",
                    "打开岗位页与简历",
                    job_id=job_id,
                    requires_confirmation=True,
                    description="只打开页面和简历文件夹，不填写、上传或提交。",
                ),
                _action("open_job", "先查看岗位", job_id=job_id),
            ],
        )

    if any(term in normalized for term in ("面试", "准备", "练习", "押题")):
        interview_jobs = [
            job
            for job in jobs
            if job.get("status")
            in {"resume_requested", "screening", "assessment", "written_test", "interview_1", "interview_2", "final_interview"}
        ]
        selected = target_job if context.get("selected_job_id") is not None else (interview_jobs[0] if interview_jobs else target_job)
        if selected is None:
            return (
                "当前没有可绑定的岗位。面试训练必须基于真实 JD 和已确认经历，先导入一个目标岗位。",
                [_action("review_queue", "查看岗位队列")],
            )
        job_id = int(selected["job_id"])
        return (
            f"先准备 #{job_id} {selected['company']} · {selected['title']}。优先核对岗位要求、能力缺口和可引用的 STAR 事实，再开始模拟回答。",
            [
                _action("open_preparation", "打开岗位准备包", job_id=job_id),
                _action("open_job", "查看对应岗位", job_id=job_id),
            ],
        )

    if any(term in normalized for term in ("为什么", "理由", "差距", "缺口", "匹配度怎么", "适合我")):
        if target_job is None:
            return (
                "当前岗位库里还没有正式岗位。先导入完整 JD 并完成匹配，我才能解释每个岗位为什么值得或不值得投。",
                [_action("review_queue", "查看岗位队列")],
            )
        job_id = int(target_job["job_id"])
        insight = target_job.get("match_insight")
        if not isinstance(insight, dict):
            return (
                f"#{job_id} {target_job['company']} · {target_job['title']} 还没有匹配报告。"
                "先在岗位列表里完成一次评分，我就能逐条解释得分依据。",
                [_action("open_job", "查看对应岗位", job_id=job_id)],
            )
        lines = []
        why_fit = list(insight.get("why_fit", []))
        gaps = list(insight.get("gaps", []))
        hard_gates = list(insight.get("hard_gates", []))
        if why_fit:
            lines.append("匹配理由：" + "；".join(str(item) for item in why_fit[:2]))
        if gaps:
            lines.append("主要差距：" + "；".join(str(item) for item in gaps[:2]))
        failing = [
            gate for gate in hard_gates if isinstance(gate, dict) and gate.get("status") == "fails"
        ]
        if failing:
            lines.append(
                "硬门槛未过：" + "；".join(str(gate.get("requirement")) for gate in failing[:2])
            )
        if not lines:
            lines.append("该岗位已有匹配报告，但没有记录具体理由条目。")
        score = target_job.get("match_score") or "未评分"
        return (
            f"#{job_id} {target_job['company']} · {target_job['title']}（匹配 {score}）。"
            + "；".join(lines)
            + "。简历草稿只会引用已确认事实，不会为了补差距虚构经历。",
            [
                _action(
                    "create_resume_draft",
                    "生成简历草稿",
                    job_id=job_id,
                    requires_confirmation=True,
                    description="本地生成，不对外发送；完成后必须本人审阅 PDF。",
                ),
                _action("open_job", "查看对应岗位", job_id=job_id),
            ],
        )

    if any(term in normalized for term in ("进度", "漏斗", "跟进", "反馈", "已读")):
        applied = int(pipeline.get("applied_or_later", 0))
        hr_read = int(pipeline.get("hr_read_or_later", 0))
        meaningful = int(pipeline.get("meaningful_responses", 0))
        interviews = int(pipeline.get("interview_or_later", 0))
        return (
            f"当前记录：已投递 {applied} 个，HR 已读 {hr_read} 个，有效反馈 {meaningful} 个，进入面试 {interviews} 个。"
            "单纯已读只作为观察信号，不算有效回复。"
            "先处理长期停留在已投递且没有新证据的岗位；任何跟进都应基于真实页面或沟通记录补录。",
            [_action("review_queue", "查看投递与反馈")],
        )

    if any(term in normalized for term in ("画像", "方向", "目标", "我的资料", "简历情况")):
        if not isinstance(profile, dict):
            return (
                "当前还没有完整个人画像。上传简历后，提取内容只进入待确认区；你确认过的事实才会用于匹配和投递材料。",
                [_action("open_profile", "建立个人资料")],
            )
        roles = "、".join(profile.get("target_roles", [])) or "尚未填写"
        locations = "、".join(profile.get("preferred_locations", [])) or "尚未填写"
        return (
            f"当前方向：{roles}；地点：{locations}；已确认事实 {profile.get('confirmed_fact_count', 0)} 条，"
            f"已确认技能 {profile.get('confirmed_skill_count', 0)} 项。任何缺少证据的内容都不会自动写进简历。",
            [_action("open_profile", "更新简历与画像")],
        )

    if any(term in normalized for term in ("设置", "api", "模型", "额度")):
        return (
            "AI、搜索和地图连接都在本机设置中管理。密钥不会通过 Dashboard 接口回显；没有云端 AI 时，对话仍可读取本地岗位和进度。",
            [_action("open_settings", "打开连接设置")],
        )

    if jobs:
        top = jobs[:3]
        lines = "；".join(
            f"#{job['job_id']} {job['company']}·{job['title']}（{job.get('match_score') or '未评分'}）"
            for job in top
        )
        actions = [_action("review_queue", "进入待确认队列")]
        for job in top[:2]:
            actions.append(_action("open_job", f"查看 #{job['job_id']}", job_id=int(job["job_id"])))
        return (
            f"今天先围绕真实岗位推进。当前优先序列是：{lines}。你可以继续问我某个岗位为什么值得投、如何改简历、是否能代填或该准备什么。",
            actions,
        )
    return (
        "工作台里暂时没有正式岗位。先建立个人画像并导入一个完整 JD；之后我才能围绕真实岗位做匹配、简历和投递建议。",
        [_action("open_profile", "建立个人资料")],
    )


def _usage_tokens(response: object) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    input_tokens = getattr(usage, "input_tokens", None)
    if input_tokens is None:
        input_tokens = getattr(usage, "prompt_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", None)
    if output_tokens is None:
        output_tokens = getattr(usage, "completion_tokens", 0)
    return int(input_tokens or 0), int(output_tokens or 0)


def _cloud_reply(
    config: RuntimeConfig,
    thread: CopilotThread,
    message: str,
    context: dict[str, object],
) -> tuple[str, int, int]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise CopilotChatError("OpenAI SDK 未安装。") from exc
    history_items = thread.messages
    if history_items and history_items[-1].role == "user":
        history_items = history_items[:-1]
    history: list[dict[str, str]] = [
        {"role": item.role, "content": item.content}
        for item in history_items[-10:]
        if item.job_id == context.get("selected_job_id")
    ]
    user_payload = json.dumps(
        {"workspace_context": context, "user_message": message},
        ensure_ascii=False,
    )
    if config.ai.provider == "codex":
        from job_agent.services.codex_bridge import codex_completion
        return codex_completion(_SYSTEM_PROMPT, json.dumps({"history": history,
            "current": json.loads(user_payload)}, ensure_ascii=False), model=config.ai.model)
    if config.ai.provider == "openai":
        if not config.ai.api_key:
            raise CopilotChatError("OpenAI API Key 尚未配置。")
        response = OpenAI(api_key=config.ai.api_key).responses.create(
            model=config.ai.model,
            store=False,
            input=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": user_payload},
            ],
            max_output_tokens=900,
        )
        content = (getattr(response, "output_text", "") or "").strip()
    elif config.ai.provider == "openai_compatible":
        if not config.ai.base_url:
            raise CopilotChatError("OpenAI 兼容 API 地址尚未配置。")
        response = OpenAI(
            api_key=config.ai.api_key or "local-api-no-key",
            base_url=config.ai.base_url,
        ).chat.completions.create(
            model=config.ai.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": user_payload},
            ],
            temperature=0.2,
        )
        content = (response.choices[0].message.content or "").strip()
    else:
        raise CopilotChatError("当前使用本地对话模式。")
    if not content:
        raise CopilotChatError("AI 未返回可用回答。")
    input_tokens, output_tokens = _usage_tokens(response)
    return content, input_tokens, output_tokens


def _cloud_allowed(config: RuntimeConfig, usage_path: Path) -> bool:
    if config.ai.provider == "local":
        return False
    if config.ai.monthly_quota is None:
        return True
    usage = load_api_usage(usage_path)
    return usage.ai.successful_requests < config.ai.monthly_quota


def copilot_snapshot(
    thread_path: Path,
    *,
    runtime_config_path: Path,
    repository: JobRepository | None = None,
    profile_path: Path | None = None,
) -> CopilotSnapshot:
    thread = load_copilot_thread(thread_path)
    config, _ = effective_runtime_config(runtime_config_path)
    suggestions = list(_SUGGESTIONS)
    if repository is not None and profile_path is not None:
        try:
            context = build_safe_workspace_context(repository, profile_path=profile_path)
            suggestions = _contextual_suggestions(context)
        except Exception:
            # Snapshot must stay available even if context building fails.
            suggestions = list(_SUGGESTIONS)
    return CopilotSnapshot(
        thread=thread,
        provider=config.ai.provider,
        model=config.ai.model,
        suggestions=suggestions,
    )


def respond_to_copilot(
    message: str,
    *,
    thread_path: Path,
    repository: JobRepository,
    profile_path: Path,
    runtime_config_path: Path,
    usage_path: Path,
    selected_job_id: int | None = None,
    applications_dir: Path | None = None,
) -> CopilotSnapshot:
    cleaned = message.strip()
    if not cleaned:
        raise CopilotChatError("请输入要讨论的岗位、简历或求职任务。")
    if len(cleaned) > 4000:
        raise CopilotChatError("单条消息最多 4000 个字符。")

    thread = load_copilot_thread(thread_path)
    if selected_job_id is not None and (type(selected_job_id) is not int or selected_job_id <= 0):
        raise CopilotChatError("职位编号必须为正整数。")
    explicit = re.search(r"(?:#|岗位\s*|职位\s*)(\d{1,9})(?!\d)", cleaned)
    target_job_id = int(explicit.group(1)) if explicit else selected_job_id
    if explicit and selected_job_id is not None and target_job_id != selected_job_id:
        raise CopilotChatError("消息中的职位与当前选择不一致，请先切换到对应职位再发送。")
    context = build_safe_workspace_context(
        repository, profile_path=profile_path, selected_job_id=target_job_id
    )
    if applications_dir is not None and target_job_id is not None:
        from job_agent.services.resume_editor import ResumeEditorError, load_latest_resume_content
        try:
            draft, _ = load_latest_resume_content(applications_dir, target_job_id)
        except ResumeEditorError:
            pass
        else:
            context["current_resume_draft"] = {key: draft[key] for key in
                ("summary", "self_evaluation", "skills", "experience_sections", "education", "highlights")
                if key in draft}
    user_message = CopilotMessage(
        message_id=secrets.token_urlsafe(12),
        role="user",
        content=cleaned,
        provider="user",
        job_id=target_job_id,
    )
    thread.messages.append(user_message)
    local_content, actions = _local_reply(cleaned, context)
    config, _ = effective_runtime_config(runtime_config_path)
    provider = "local"
    content = local_content
    if _cloud_allowed(config, usage_path):
        try:
            content, input_tokens, output_tokens = _cloud_reply(
                config,
                thread,
                cleaned,
                context,
            )
        except Exception:
            # Connector, network, authentication, or SDK failures must never
            # make the local job workspace unusable. Do not expose provider
            # error text because it can contain secret-adjacent diagnostics.
            content = local_content + "\n\n云端模型本次不可用，以上回答来自本地工作台数据。"
            provider = "local_fallback"
        else:
            provider = f"{config.ai.provider}:{config.ai.model}"
            record_api_usage(
                usage_path,
                "ai",
                config.ai.provider,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
    elif config.ai.provider != "local":
        content = local_content + "\n\n本机 AI 月度调用上限已到，本次使用本地工作台数据回答。"
        provider = "local_quota_fallback"

    thread.messages.append(
        CopilotMessage(
            message_id=secrets.token_urlsafe(12),
            role="assistant",
            content=content,
            provider=provider,
            job_id=target_job_id,
            actions=actions,
        )
    )
    save_copilot_thread(thread, thread_path)
    saved = load_copilot_thread(thread_path)
    return CopilotSnapshot(
        thread=saved,
        provider=config.ai.provider,
        model=config.ai.model,
        suggestions=_contextual_suggestions(context),
    )
