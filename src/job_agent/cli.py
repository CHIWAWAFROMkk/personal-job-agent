from __future__ import annotations

import argparse
import json
import importlib.util
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from job_agent.models.application_tracking import ApplicationStatus
from job_agent.models.interview_debrief import InterviewDebriefInput
from job_agent.services.interview_debrief import record_interview_debrief
from job_agent.models.job import MatchResult
from job_agent.models.job_record import JobRecordInput, SearchCandidateInput
from job_agent.models.profile import Availability, SourceDocument, empty_profile
from job_agent.services.application_pack import (
    ApplicationPackError,
    build_application_pack,
    resolve_resume_bundle,
    write_application_pack,
)
from job_agent.services.api_usage import record_api_usage
from job_agent.services.browser_assist import (
    BrowserAssistError,
    create_application_session,
    find_application_pack,
    load_application_pack,
    playwright_environment_status,
    run_browser_session,
    verify_application_page,
)
from job_agent.services.contact_import import (
    ContactImportError,
    import_contact_from_confirmed_resume,
)
from job_agent.services.dashboard import DashboardError, run_dashboard
from job_agent.services.job_repository import (
    APPLICATION_STATUSES,
    JobDatabaseError,
    JobRepository,
)
from job_agent.services.job_search_provider import (
    JobSearchProviderError,
    build_job_search_provider,
)
from job_agent.services.job_source_review import review_job_source
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.profile_store import (
    ProfileStoreError,
    load_profile,
    save_profile,
    write_json_atomic,
)
from job_agent.services.preparation_pack import (
    PreparationPackError,
    build_preparation_pack,
    preparation_priority_for_status,
    should_auto_generate_preparation,
    write_preparation_pack,
)
from job_agent.services.resume_reader import ResumeReadError, read_resume
from job_agent.services.resume_audit import audit_resume
from job_agent.services.status_sync import (
    StatusSyncError,
    collect_shixiseng_statuses,
    reconcile_status_collection,
)
from job_agent.services.tailored_resume import (
    TailoredResumeError,
    approve_resume_visual_review,
    build_tailored_resume_draft,
)
from job_agent.settings import get_settings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-agent",
        description="本地优先的个人求职 Agent",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查本地运行环境")

    profile = commands.add_parser("profile", help="管理真实经历 Profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("init", help="创建空白 Profile（不会覆盖）")
    profile_commands.add_parser("validate", help="校验 Profile 和事实引用")
    profile_commands.add_parser("summary", help="显示不含联系方式的 Profile 摘要")
    profile_commands.add_parser(
        "confirm-all",
        help="将全部待确认事实标记为用户已确认，并保留备份",
    )
    profile_commands.add_parser(
        "import-contact",
        help="从本人已确认的简历原文提取联系方式，不在终端显示具体值",
    )
    availability = profile_commands.add_parser(
        "set-availability",
        help="更新已确认的到岗范围，并保留备份",
    )
    availability.add_argument("--min-days", type=int, required=True)
    availability.add_argument("--max-days", type=int)
    availability.add_argument("--min-months", type=int, required=True)
    availability.add_argument("--max-months", type=int)
    availability.add_argument("--start", required=True)
    availability.add_argument("--notes", default="")

    resume = commands.add_parser("resume", help="读取简历文件")
    resume_commands = resume.add_subparsers(dest="resume_command", required=True)
    resume_import = resume_commands.add_parser("import", help="提取简历文字并登记来源")
    resume_import.add_argument("path", type=Path)
    resume_audit = resume_commands.add_parser(
        "audit",
        help="在本地检查简历结构、证据与表达，不调用外部服务",
    )
    resume_audit.add_argument("path", type=Path)
    resume_audit.add_argument("--output", type=Path, help="另存完整 JSON 报告")

    jobs = commands.add_parser("jobs", help="管理 SQLite 岗位库")
    jobs_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_commands.add_parser("init", help="初始化本地岗位数据库")
    jobs_import = jobs_commands.add_parser("import", help="导入 JD、去重并保存评分")
    jobs_import.add_argument("jd_path", type=Path)
    jobs_import.add_argument("--match-result", type=Path)
    jobs_import.add_argument("--company")
    jobs_import.add_argument("--title")
    jobs_import.add_argument("--location")
    jobs_import.add_argument("--source")
    jobs_import.add_argument("--url", dest="source_url")
    jobs_import.add_argument("--external-id")
    jobs_import.add_argument("--published-at")
    jobs_import.add_argument("--deadline-at")
    jobs_list = jobs_commands.add_parser("list", help="按匹配分列出岗位")
    jobs_list.add_argument("--limit", type=int, default=20)
    jobs_review = jobs_commands.add_parser(
        "review",
        help="只读检查岗位来源路径、时效和下一步核验动作",
    )
    jobs_review.add_argument("job_id", type=int, nargs="+", help="可一次检查多个岗位编号")
    jobs_commands.add_parser("stats", help="显示发现、去重和推荐统计")
    jobs_commands.add_parser("verify", help="检查 SQLite 完整性和外键引用")
    jobs_rescore = jobs_commands.add_parser(
        "rescore",
        help="根据当前已确认 Profile 重新计算已入库岗位的本地匹配分",
    )
    jobs_rescore.add_argument("job_id", type=int, nargs="+", help="可一次填写多个岗位编号")
    jobs_discover = jobs_commands.add_parser(
        "discover",
        help="依据 Profile 的目标岗位主动搜索候选链接",
    )
    jobs_discover.add_argument("--limit-per-query", type=int, default=5)
    jobs_discover.add_argument(
        "--max-queries",
        type=int,
        default=3,
        help="本次最多调用搜索 API 的次数，默认 3",
    )
    jobs_discover.add_argument(
        "--role",
        action="append",
        help="只搜索指定岗位方向；可重复使用",
    )
    jobs_candidates = jobs_commands.add_parser(
        "candidates",
        help="列出尚未进入正式岗位库的搜索候选",
    )
    jobs_candidates.add_argument(
        "--status",
        choices=(
            "pending",
            "live",
            "expired",
            "blocked",
            "irrelevant",
            "needs_manual_review",
        ),
    )
    jobs_candidates.add_argument("--limit", type=int, default=20)
    candidate_status = jobs_commands.add_parser(
        "candidate-status",
        help="记录候选岗位的在招核验结果",
    )
    candidate_status.add_argument("candidate_id", type=int)
    candidate_status.add_argument(
        "status",
        choices=(
            "pending",
            "live",
            "expired",
            "blocked",
            "irrelevant",
            "needs_manual_review",
        ),
    )
    candidate_status.add_argument("--detail", default="")

    match = commands.add_parser("match", help="对一个真实 JD 进行匹配评分")
    match.add_argument("jd_path", type=Path)
    match.add_argument("--engine", choices=("local", "ai", "openai"), default="local")
    match.add_argument("--provider", help="AI Provider；默认读取本地配置")
    match.add_argument("--company")
    match.add_argument("--title")
    match.add_argument("--location")
    match.add_argument("--source", default="manual")
    match.add_argument("--url", dest="source_url")
    match.add_argument("--output", type=Path)

    applications = commands.add_parser(
        "applications",
        help="为已入库岗位生成可追溯的投递材料包",
    )
    application_commands = applications.add_subparsers(
        dest="applications_command",
        required=True,
    )
    prepare = application_commands.add_parser(
        "prepare",
        help="读取岗位、评分和真实经历，一键准备投递材料",
    )
    prepare.add_argument("job_id", type=int, help="jobs list 显示的岗位编号")
    prepare.add_argument(
        "--output-dir",
        type=Path,
        help="可选的输出目录；默认按岗位和生成时间创建新版本",
    )
    prepare.add_argument(
        "--resume-content",
        type=Path,
        help="可选的定向简历内容 JSON；不填写时自动查找已有版本",
    )
    prepare.add_argument(
        "--skip-resume-generation",
        action="store_true",
        help="没有现成定向简历时，只生成文字材料，不自动创建 DOCX/PDF 草稿",
    )
    approve_resume = application_commands.add_parser(
        "approve-resume",
        help="本人看过 PDF 版面后，批准自动生成的定向简历",
    )
    approve_resume.add_argument("manifest_path", type=Path)
    generate_resume = application_commands.add_parser(
        "generate-resume",
        help="即使已有版本，也为岗位生成一份新的固定模板 STAR 简历草稿",
    )
    generate_resume.add_argument("job_id", type=int)
    generate_resume.add_argument("--output-dir", type=Path)
    assist = application_commands.add_parser(
        "assist",
        help="打开岗位页面并准备已批准的简历文件，网页内容由本人完成",
    )
    assist.add_argument("job_id", type=int)
    assist.add_argument("--pack", type=Path, help="可选的 application-pack.json")
    assist.add_argument("--url", help="可选的真实岗位页面；默认使用岗位库链接")
    assist.add_argument(
        "--mode",
        choices=("open_only",),
        default="open_only",
        help=argparse.SUPPRESS,
    )
    assist.add_argument(
        "--plan-only",
        action="store_true",
        help="只生成安全填写计划，不启动浏览器",
    )
    assist.add_argument(
        "--browser",
        choices=("chromium", "msedge", "chrome"),
        default="chromium",
    )
    assist.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    assist.add_argument("--close", action="store_true", help=argparse.SUPPRESS)
    assist.add_argument("--max-actions", type=int, default=12, help=argparse.SUPPRESS)
    verify_application = application_commands.add_parser(
        "verify",
        help="只读打开岗位页，核验是否已投递并写入投递数据库",
    )
    verify_application.add_argument("job_id", type=int)
    verify_application.add_argument("--pack", type=Path, help=argparse.SUPPRESS)
    verify_application.add_argument("--url", help=argparse.SUPPRESS)
    verify_application.add_argument(
        "--browser",
        choices=("chromium", "msedge", "chrome"),
        default="chromium",
        help=argparse.SUPPRESS,
    )
    verify_application.add_argument("--headed", action="store_true", help=argparse.SUPPRESS)
    application_list = application_commands.add_parser(
        "list",
        help="列出全部投递及当前进度",
    )
    application_list.add_argument(
        "--status",
        choices=tuple(sorted(APPLICATION_STATUSES)),
    )
    application_list.add_argument("--limit", type=int, default=100)
    application_status = application_commands.add_parser(
        "status",
        help="手动记录真实招聘进度",
    )
    application_status.add_argument("job_id", type=int)
    application_status.add_argument(
        "status",
        choices=tuple(sorted(APPLICATION_STATUSES)),
    )
    application_status.add_argument("--detail", default="")
    application_status.add_argument(
        "--force",
        action="store_true",
        help="真实状态确实回退或终态恢复时显式使用",
    )
    application_history = application_commands.add_parser(
        "history",
        help="查看一个岗位的完整状态时间线",
    )
    application_history.add_argument("job_id", type=int)
    debrief = application_commands.add_parser("debrief", help="保存本人提供的面试复盘 JSON")
    debrief.add_argument("job_id", type=int)
    debrief.add_argument("input_path", type=Path)
    debriefs = application_commands.add_parser("debriefs", help="查看岗位的历史面试复盘")
    debriefs.add_argument("job_id", type=int)
    application_prep = application_commands.add_parser(
        "prep",
        help="根据 JD、真实经历和当前招聘进度生成岗位学习与面试准备包",
    )
    application_prep.add_argument("job_id", type=int)
    application_prep.add_argument("--output-dir", type=Path)
    application_commands.add_parser(
        "summary",
        help="显示当前求职漏斗统计",
    )
    application_sync = application_commands.add_parser(
        "sync",
        help="只读批量检查招聘平台反馈，并安全同步唯一匹配的本地岗位",
    )
    application_sync.add_argument(
        "--platform",
        choices=("shixiseng",),
        default="shixiseng",
    )
    application_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="只核对匹配结果，不修改投递数据库",
    )
    application_sync.add_argument(
        "--browser",
        choices=("chromium", "msedge", "chrome"),
        default="chromium",
        help=argparse.SUPPRESS,
    )
    application_sync.add_argument(
        "--headed",
        action="store_true",
        help="显示专用浏览器窗口，用于本人重新登录",
    )
    application_sync.add_argument(
        "--wait-seconds",
        type=int,
        default=5,
        help="页面等待时间；需要本人重新登录时可设为 60",
    )
    browser_demo = application_commands.add_parser(
        "browser-demo",
        help="在本地模拟招聘表单验证安全填写和提交硬暂停",
    )
    browser_demo.add_argument("job_id", type=int)
    browser_demo.add_argument("--pack", type=Path)

    dashboard = commands.add_parser(
        "dashboard",
        help="启动本地求职 Dashboard",
    )
    dashboard.add_argument(
        "--port",
        type=int,
        default=8787,
        help="本地访问端口，默认 8787",
    )
    dashboard.add_argument(
        "--no-open",
        action="store_true",
        help="启动后不自动打开浏览器",
    )
    return parser


def _dependency_status(module: str) -> str:
    return "已安装" if importlib.util.find_spec(module) else "未安装"


def _doctor() -> int:
    settings = get_settings()
    search_provider = settings.search_provider.strip().casefold()
    search_key_configured = (
        bool(settings.bocha_api_key)
        if search_provider == "bocha"
        else bool(settings.brave_search_api_key)
        if search_provider == "brave"
        else True
    )
    print("个人求职 Agent 环境检查")
    print(f"Python: {sys.version.split()[0]}")
    print(f"运行程序: {sys.executable}")
    print(f"项目目录: {settings.project_root}")
    print(f"Pydantic: {_dependency_status('pydantic')}")
    print(f"OpenAI SDK: {_dependency_status('openai')}")
    print(f"DOCX 读取: {_dependency_status('docx')}")
    print(f"PDF 读取: {_dependency_status('pypdf')}")
    print(f"PDF 生成: {_dependency_status('reportlab')}")
    print(f"固定模板处理: {_dependency_status('lxml')}")
    playwright_installed, browser_installed, browser_detail = playwright_environment_status()
    print(f"浏览器自动化: {'已安装' if playwright_installed else '未安装'}")
    print(f"Chromium 运行组件: {'已安装' if browser_installed else '未安装'}")
    if playwright_installed and not browser_installed:
        print(f"浏览器提示: {browser_detail}")
    print(f"Profile: {'已创建' if settings.profile_path.is_file() else '尚未创建'}")
    print(f"岗位数据库: {'已创建' if settings.job_db_path.is_file() else '尚未创建'}")
    print(
        "岗位搜索: "
        + (
            "未启用（可手动导入岗位）"
            if search_provider == "none"
            else
            f"{settings.search_provider}（已配置）"
            if search_key_configured
            else f"{settings.search_provider}（缺少 API Key）"
        )
    )
    ai_ready = settings.ai_provider == "local" or settings.has_ai_api_key
    print(
        f"AI 分析: {settings.ai_provider} / {settings.ai_model}"
        f"（{'可用' if ai_ready else '缺少 API Key'}）"
    )
    print(f"配置来源: {settings.config_source}")
    if sys.version_info < (3, 12):
        print("错误: 需要 Python 3.12 或更高版本。")
        return 1
    if playwright_installed and not browser_installed:
        print("提示: 运行浏览器辅助前，请执行 python -m playwright install chromium。")
    return 0


def _profile_command(args: argparse.Namespace) -> int:
    command = args.profile_command
    settings = get_settings()
    if command == "init":
        path = save_profile(
            empty_profile(),
            settings.profile_path,
            overwrite=False,
            create_backup=False,
        )
        print(f"已创建空白 Profile: {path}")
        return 0
    profile = load_profile(settings.profile_path)
    if command == "import-contact":
        profile, imported = import_contact_from_confirmed_resume(
            profile,
            settings.private_dir,
        )
        if imported:
            save_profile(
                profile,
                settings.profile_path,
                overwrite=True,
                create_backup=True,
            )
            print("已从本人确认的简历导入：" + "、".join(imported) + "。")
            print("为保护隐私，终端不显示联系方式具体值；修改前 Profile 已备份。")
        else:
            print("联系方式已存在且与本人确认的简历一致，无需修改。")
        return 0
    if command == "set-availability":
        profile.job_search.availability = Availability(
            earliest_start=args.start,
            days_per_week=args.min_days,
            max_days_per_week=args.max_days,
            duration_months=args.min_months,
            max_duration_months=args.max_months,
            notes=args.notes,
        )
        save_profile(
            profile,
            settings.profile_path,
            overwrite=True,
            create_backup=True,
        )
        day_range = (
            f"{args.min_days}-{args.max_days} 天"
            if args.max_days and args.max_days != args.min_days
            else f"{args.min_days} 天"
        )
        month_range = (
            f"{args.min_months}-{args.max_months} 个月"
            if args.max_months and args.max_months != args.min_months
            else f"至少 {args.min_months} 个月"
        )
        print(
            f"已更新到岗信息：每周 {day_range}，{month_range}，"
            f"最早 {args.start} 开始；已保存修改前备份。"
        )
        return 0
    if command == "confirm-all":
        confirmed_profile, count = profile.confirm_all_pending_claims()
        for source in confirmed_profile.source_documents:
            if source.kind == "resume":
                source.notes = "用户已确认该简历提取内容真实，并允许用于求职。"
        save_profile(
            confirmed_profile,
            settings.profile_path,
            overwrite=True,
            create_backup=True,
        )
        print(f"已确认 {count} 个待确认条目，并保存修改前备份。")
        return 0
    if command == "validate":
        ready_facts = sum(
            1
            for experience in profile.experiences
            for fact in experience.facts
            if profile.is_application_ready(fact.status)
        )
        pending_facts = sum(len(item.facts) for item in profile.experiences) - ready_facts
        print("Profile 校验通过。")
        print(f"可用于投递的已确认事实: {ready_facts}")
        print(f"待确认事实: {pending_facts}")
        return 0
    if command == "summary":
        print(f"求职阶段: {profile.job_search.stage or '未填写'}")
        print("目标岗位: " + ("、".join(profile.job_search.target_roles) or "未填写"))
        print(f"教育经历: {len(profile.education)}")
        print(f"经历条目: {len(profile.experiences)}")
        print(f"技能条目: {len(profile.skills)}")
        print(f"面试故事: {len(profile.stories)}")
        return 0
    raise ValueError(f"未知 Profile 命令: {command}")


def _resume_import(path: Path) -> int:
    settings = get_settings()
    profile = load_profile(settings.profile_path)
    document = read_resume(path)
    source_id = f"resume-{document.sha256[:12]}"
    extracted_path = settings.private_dir / f"{source_id}.txt"
    if extracted_path.exists():
        existing = extracted_path.read_text(encoding="utf-8")
        if existing != document.text + "\n":
            raise ResumeReadError(f"目标文字文件已存在且内容不同: {extracted_path}")
    else:
        extracted_path.write_text(document.text + "\n", encoding="utf-8")

    if not any(source.id == source_id for source in profile.source_documents):
        profile.source_documents.append(
            SourceDocument(
                id=source_id,
                kind="resume",
                original_path=str(document.path),
                sha256=document.sha256,
                notes="已提取原文；尚未自动确认为 Profile 事实。",
            )
        )
        save_profile(profile, settings.profile_path, overwrite=True, create_backup=True)
    print(f"简历读取成功，共 {len(document.text)} 个字符。")
    print(f"已保存本地文字副本: {extracted_path}")
    print("尚未把任何内容直接认定为事实；下一步需要逐项提取并确认。")
    for warning in document.warnings:
        print(f"警告: {warning}")
    return 0


def _resume_audit(path: Path, output: Path | None = None) -> int:
    settings = get_settings()
    document = read_resume(path)
    report = audit_resume(document)
    output_path = (
        output.expanduser().resolve()
        if output
        else settings.output_dir
        / "resume-audits"
        / f"{_safe_filename(document.path.stem)}-{document.sha256[:12]}.json"
    )
    write_json_atomic(report.model_dump(mode="json"), output_path)

    print(f"简历体检分: {report.score}/100")
    print(report.summary)
    if report.strengths:
        print("已识别优点:")
        for item in report.strengths:
            print(f"  - {item}")
    if report.findings:
        print("建议处理:")
        severity_labels = {"high": "高", "medium": "中", "low": "低"}
        for item in report.findings:
            print(f"  - [{severity_labels[item.severity]}] {item.title}")
            print(f"    {item.recommendation}")
    else:
        print("未命中当前规则集中的明显问题。")
    for warning in report.extraction_warnings:
        print(f"提取警告: {warning}")
    print(f"完整报告: {output_path}")
    print(f"真实性边界: {report.safety_note}")
    return 0


def _resume_command(args: argparse.Namespace) -> int:
    if args.resume_command == "import":
        return _resume_import(args.path)
    if args.resume_command == "audit":
        return _resume_audit(args.path, args.output)
    raise ValueError(f"未知简历命令: {args.resume_command}")


def _jobs_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    repository = JobRepository(settings.job_db_path)
    if args.jobs_command == "init":
        path = repository.initialize()
        print(f"岗位数据库已初始化: {path}")
        return 0
    if args.jobs_command == "import":
        jd_path = args.jd_path.expanduser().resolve()
        if not jd_path.is_file():
            raise FileNotFoundError(f"JD 文件不存在: {jd_path}")
        raw_jd = jd_path.read_text(encoding="utf-8-sig").strip()
        if not raw_jd:
            raise ValueError("JD 文件为空。")

        match_result: MatchResult | None = None
        if args.match_result:
            match_path = args.match_result.expanduser().resolve()
            if not match_path.is_file():
                raise FileNotFoundError(f"匹配结果不存在: {match_path}")
            match_result = MatchResult.model_validate_json(
                match_path.read_text(encoding="utf-8-sig")
            )

        structured = (
            match_result.job
            if match_result
            else structure_job_locally(
                raw_jd,
                company=args.company,
                title=args.title,
                location=args.location,
                source=args.source or "manual",
                source_url=args.source_url,
            )
        )
        record = JobRecordInput(
            company=args.company or structured.company,
            title=args.title or structured.title,
            location=args.location or structured.location,
            jd_text=raw_jd,
            source=args.source or structured.source,
            source_url=args.source_url or structured.source_url,
            external_id=args.external_id,
            published_at=args.published_at,
            deadline_at=args.deadline_at,
        )
        outcome = repository.upsert_job(record)
        match_added = (
            repository.add_match_result(outcome.job_id, match_result)
            if match_result
            else False
        )
        action = "新建岗位" if outcome.created else "合并到已有岗位"
        source_action = "新增来源" if outcome.source_added else "来源已存在"
        print(f"{action} #{outcome.job_id}；{source_action}。")
        print(f"去重原因: {outcome.dedupe_reason}")
        if match_result:
            stored = "已保存" if match_added else "已存在"
            print(f"匹配结果: {stored}（{match_result.overall_score}/100）")
        return 0
    if args.jobs_command == "list":
        if args.limit < 1 or args.limit > 500:
            raise ValueError("--limit 必须在 1 到 500 之间。")
        jobs = repository.list_jobs(limit=args.limit)
        if not jobs:
            print("岗位库为空。")
            return 0
        for job in jobs:
            score = "未评分" if job.match_score is None else f"{job.match_score}/100"
            print(
                f"#{job.job_id} | {score} | {job.company} | {job.title} | "
                f"{job.location or '地点未注明'} | 来源 {job.source_count}"
            )
        return 0
    if args.jobs_command == "review":
        confidence_labels = {"high": "较高", "medium": "中等", "low": "较低"}
        for job_id in args.job_id:
            if job_id < 1:
                raise ValueError("岗位编号必须大于 0。")
            job = repository.get_job(job_id)
            review = review_job_source(job)
            print(f"#{job.job_id} | {job.company} | {job.title}")
            print(
                f"  来源路径: {review.source_label} | "
                f"来源证据: {confidence_labels[review.source_confidence]} | "
                f"时效: {review.freshness_label}"
            )
            for signal in review.positive_signals:
                print(f"  + {signal}")
            for item in review.review_items:
                print(f"  ! {item}")
            print(f"  下一步: {review.recommended_action}")
            print(f"  说明: {review.note}")
        return 0
    if args.jobs_command == "stats":
        stats = repository.stats()
        print(f"去重后岗位: {stats.jobs}")
        print(f"岗位来源记录: {stats.sources}")
        print(f"已合并重复来源: {stats.merged_source_records}")
        print(f"匹配报告: {stats.match_results}")
        print(f"推荐岗位（>=75）: {stats.recommended}")
        print(f"强烈推荐（>=85）: {stats.strongly_recommended}")
        print(f"搜索候选: {stats.candidates}")
        print(f"待核验候选: {stats.pending_candidates}")
        print(f"确认在招候选: {stats.live_candidates}")
        print(f"已失效候选: {stats.expired_candidates}")
        print(f"访问受阻候选: {stats.blocked_candidates}")
        print(f"待人工复核候选: {stats.manual_review_candidates}")
        print(f"与求职方向无关候选: {stats.irrelevant_candidates}")
        return 0
    if args.jobs_command == "verify":
        repository.verify()
        print("岗位数据库完整性检查通过。")
        return 0
    if args.jobs_command == "rescore":
        profile = load_profile(settings.profile_path)
        for job_id in args.job_id:
            if job_id < 1:
                raise ValueError("岗位编号必须大于 0。")
            job = repository.get_job(job_id)
            source = job.sources[0] if job.sources else None
            structured = structure_job_locally(
                job.jd_text,
                company=job.company,
                title=job.title,
                location=job.location,
                source=source.platform if source else "database",
                source_url=source.source_url if source else None,
            )
            result = match_job_locally(profile, structured)
            added = repository.add_match_result(job.job_id, result)
            stored = "已保存" if added else "结果未变化"
            print(
                f"#{job.job_id} | {job.company} | {job.title} | "
                f"{result.overall_score}/100（{result.recommendation.zh}）| {stored}"
            )
        return 0
    if args.jobs_command == "discover":
        if args.limit_per_query < 1 or args.limit_per_query > 20:
            raise ValueError("--limit-per-query 必须在 1 到 20 之间。")
        if args.max_queries < 1 or args.max_queries > 50:
            raise ValueError("--max-queries 必须在 1 到 50 之间。")
        profile = load_profile(settings.profile_path)
        roles = args.role or profile.job_search.target_roles
        locations = profile.job_search.preferred_locations or [""]
        if not roles:
            raise ValueError("Profile 尚未填写目标岗位方向。")
        provider = build_job_search_provider(
            settings.search_provider,
            bocha_api_key=settings.bocha_api_key,
            brave_api_key=settings.brave_search_api_key,
        )
        seen_urls: set[str] = set()
        hit_number = 0
        created_number = 0
        successful_queries = 0
        failed_queries = 0
        query_pairs = [
            (location, role)
            for location in locations
            for role in roles
        ][: args.max_queries]
        for location, role in query_pairs:
            query = (
                f"{location} {role} 实习 校招 应届 "
                "(site:shixiseng.com OR site:nowcoder.com OR site:zhipin.com)"
            ).strip()
            try:
                hits = provider.search(query, count=args.limit_per_query)
            except JobSearchProviderError as exc:
                failed_queries += 1
                print(f"搜索方向失败（{location} / {role}）: {exc}", file=sys.stderr)
                continue
            successful_queries += 1
            for hit in hits:
                if hit.url in seen_urls:
                    continue
                seen_urls.add(hit.url)
                hit_number += 1
                outcome = repository.upsert_search_candidate(
                    SearchCandidateInput(
                        provider=hit.provider,
                        query=hit.query,
                        title=hit.title,
                        url=hit.url,
                        snippet=hit.snippet,
                    )
                )
                created_number += int(outcome.created)
                print(f"{hit_number}. [候选 #{outcome.candidate_id}] {hit.title}")
                print(f"   {outcome.canonical_url}")
        print(f"本次发现候选链接: {hit_number}")
        print(f"其中新增候选: {created_number}")
        print(f"本次搜索 API 尝试: {len(query_pairs)}")
        print(f"成功方向: {successful_queries}；失败方向: {failed_queries}")
        if successful_queries:
            record_api_usage(
                settings.private_dir / "api-usage.json",
                "search",
                settings.search_provider,
                successful_requests=successful_queries,
            )
        print("候选尚未进入正式岗位库；需先核验仍在招聘并读取完整 JD。")
        return 0
    if args.jobs_command == "candidates":
        if args.limit < 1 or args.limit > 500:
            raise ValueError("--limit 必须在 1 到 500 之间。")
        candidates = repository.list_search_candidates(
            status=args.status,
            limit=args.limit,
        )
        if not candidates:
            print("没有符合条件的搜索候选。")
            return 0
        for candidate in candidates:
            print(
                f"#{candidate.candidate_id} | {candidate.verification_status} | "
                f"来源 {candidate.source_count} | {candidate.title}"
            )
            print(f"   {candidate.url}")
            if candidate.verification_detail:
                print(f"   核验说明: {candidate.verification_detail}")
        return 0
    if args.jobs_command == "candidate-status":
        repository.mark_candidate_verification(
            args.candidate_id,
            args.status,
            detail=args.detail,
        )
        print(f"候选 #{args.candidate_id} 已标记为 {args.status}。")
        return 0
    raise ValueError(f"未知岗位库命令: {args.jobs_command}")


def _safe_filename(value: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value, flags=re.UNICODE).strip("-")
    return value[:60] or "job"


def _generate_preparation_for_job(
    job_id: int,
    *,
    trigger_status: ApplicationStatus,
    output_dir: Path | None = None,
):
    settings = get_settings()
    profile = load_profile(settings.profile_path)
    repository = JobRepository(settings.job_db_path)
    job = repository.get_job(job_id)
    result = repository.get_latest_match_result(job_id)
    if result is None:
        source = job.sources[0] if job.sources else None
        result = match_job_locally(
            profile,
            structure_job_locally(
                job.jd_text,
                company=job.company,
                title=job.title,
                location=job.location,
                source=source.platform if source else "database",
                source_url=source.source_url if source else None,
            ),
        )
        repository.add_match_result(job.job_id, result)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    resolved_output = output_dir or (
        settings.output_dir
        / "preparation-packs"
        / f"job-{job.job_id}-{_safe_filename(job.company)}-{_safe_filename(job.title)}"
        / stamp
    )
    pack = build_preparation_pack(
        profile,
        job,
        result,
        trigger_status=trigger_status,
        debriefs=repository.list_interview_debriefs(job_id),
    )
    files = write_preparation_pack(pack, job, resolved_output)
    return files, pack


def _shixiseng_status_target_urls(repository: JobRepository) -> list[str]:
    urls: list[str] = []
    for item in repository.list_applications(limit=1000):
        application = repository.get_application(item.job_id)
        if (
            application is not None
            and application.source_url
            and "shixiseng.com" in application.source_url.casefold()
        ):
            urls.append(application.source_url)
        job = repository.get_job(item.job_id)
        for source in job.sources:
            if source.source_url and (
                "实习僧" in source.platform
                or "shixiseng.com" in source.source_url.casefold()
            ):
                urls.append(source.source_url)
    return list(dict.fromkeys(urls))


def _applications_command(args: argparse.Namespace) -> int:
    if args.applications_command in {"debrief", "debriefs"}:
        if args.job_id < 1:
            raise ValueError("岗位编号必须大于 0。")
        settings = get_settings()
        repository = JobRepository(settings.job_db_path)
        repository.get_job(args.job_id)
        if args.applications_command == "debrief":
            entry = InterviewDebriefInput.model_validate_json(
                args.input_path.read_text(encoding="utf-8-sig")
            )
            saved = record_interview_debrief(
                repository, load_profile(settings.profile_path), args.job_id, entry,
            )
            print(f"复盘 #{saved.debrief_id}：{'已保存' if saved.created else '已存在，无重复写入'}。")
        else:
            entries = repository.list_interview_debriefs(args.job_id)
            print(json.dumps([entry.model_dump(mode="json") for entry in entries], ensure_ascii=False, indent=2))
        return 0
    status_labels = {
        "saved": "已收藏",
        "ready_to_apply": "待投递",
        "applied": "已投递",
        "hr_read": "HR已读",
        "resume_requested": "索要简历",
        "screening": "筛选中",
        "assessment": "测评",
        "written_test": "笔试",
        "interview_1": "一面",
        "interview_2": "二面",
        "final_interview": "终面",
        "offer": "Offer",
        "rejected": "拒绝",
        "withdrawn": "已撤回",
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
    if args.applications_command == "sync":
        if args.wait_seconds < 1 or args.wait_seconds > 60:
            raise ValueError("只读同步等待时间必须在 1 到 60 秒之间。")
        settings = get_settings()
        repository = JobRepository(settings.job_db_path)
        target_urls = _shixiseng_status_target_urls(repository)
        if not target_urls:
            raise StatusSyncError("本地投递记录中没有实习僧岗位链接。")
        collection = collect_shixiseng_statuses(
            target_urls,
            browser_profile_dir=(
                settings.browser_profiles_dir / "www-shixiseng-com"
            ),
            evidence_root=settings.private_dir / "status-sync",
            browser_channel=args.browser,
            headless=not args.headed,
            wait_milliseconds=args.wait_seconds * 1000,
        )
        report, report_path = reconcile_status_collection(
            repository,
            collection,
            evidence_root=settings.private_dir / "status-sync",
            dry_run=args.dry_run,
        )
        mode_label = "试运行" if args.dry_run else "正式同步"
        outcome_labels = {
            "updated": "已更新",
            "unchanged": "无变化",
            "stale": "旧状态已忽略",
            "dry_run": "试运行",
            "manual_review": "需人工复核",
        }
        print(f"实习僧只读反馈同步（{mode_label}）")
        print(
            f"平台记录: {report.observed} | 唯一匹配: {report.matched} | "
            f"更新: {report.updated} | 未变化: {report.unchanged}"
        )
        print(
            f"旧状态: {report.stale} | 人工复核: {report.manual_review} | "
            f"未入本地岗位库: {report.unmatched}"
        )
        for decision in report.decisions:
            if decision.job_id is None:
                continue
            observed = (
                status_labels[decision.observed_status]
                if decision.observed_status is not None
                else "未识别"
            )
            print(
                f"#{decision.job_id} | 平台 {observed} | "
                f"{outcome_labels[decision.outcome]} | "
                f"{decision.company} | {decision.title}"
            )
        print(f"同步报告: {report_path}")
        if report.evidence_path:
            print(f"最小化状态证据: {report.evidence_path}")
        if report.blocker:
            print(f"说明: {report.blocker}")

        if not args.dry_run:
            for decision in report.decisions:
                if (
                    decision.outcome == "updated"
                    and decision.job_id is not None
                    and decision.observed_status is not None
                    and should_auto_generate_preparation(decision.observed_status)
                ):
                    try:
                        files, pack = _generate_preparation_for_job(
                            decision.job_id,
                            trigger_status=decision.observed_status,
                        )
                    except (
                        FileNotFoundError,
                        OSError,
                        ProfileStoreError,
                        PreparationPackError,
                        JobDatabaseError,
                        ValidationError,
                        ValueError,
                    ) as exc:
                        print(
                            f"岗位 #{decision.job_id} 状态已同步，但自动学习包生成失败: {exc}",
                            file=sys.stderr,
                        )
                        continue
                    print(
                        f"岗位 #{decision.job_id} 准备优先级已调整为"
                        f"{priority_labels[pack.job.priority]}：{files.markdown}"
                    )
        return 0 if collection.status == "completed" else 2
    if args.applications_command == "list":
        settings = get_settings()
        repository = JobRepository(settings.job_db_path)
        applications = repository.list_applications(
            status=args.status,
            limit=args.limit,
        )
        if not applications:
            print("尚无符合条件的投递记录。")
            return 0
        for application in applications:
            score = (
                f"匹配 {application.match_score}"
                if application.match_score is not None
                else "匹配未评分"
            )
            print(
                f"#{application.job_id} | {status_labels[application.status]} | "
                "准备："
                f"{priority_labels[preparation_priority_for_status(application.status, application.match_score or 0)]} | "
                f"{score} | {application.company} | {application.title}"
            )
            print(
                f"   投递: {application.applied_at or '—'} | "
                f"最近核验: {application.last_verified_at or '—'}"
            )
        return 0
    if args.applications_command == "status":
        if args.job_id < 1:
            raise ValueError("岗位编号必须大于 0。")
        settings = get_settings()
        repository = JobRepository(settings.job_db_path)
        changed = repository.record_application_status(
            args.job_id,
            args.status,
            source="manual",
            detail=args.detail,
            allow_regression=args.force,
        )
        print(
            f"岗位 #{args.job_id} 已记录为 {status_labels[args.status]}；"
            f"新增状态事件: {'是' if changed else '否（状态未变化）'}。"
        )
        if changed and should_auto_generate_preparation(args.status):
            try:
                files, pack = _generate_preparation_for_job(
                    args.job_id,
                    trigger_status=args.status,
                )
            except (
                FileNotFoundError,
                OSError,
                ProfileStoreError,
                PreparationPackError,
                JobDatabaseError,
                ValidationError,
                ValueError,
            ) as exc:
                print(f"状态已保存，但自动生成学习包失败: {exc}", file=sys.stderr)
                print(
                    f"修复后可重试: job-agent applications prep {args.job_id}",
                    file=sys.stderr,
                )
                return 2
            print(
                "检测到积极反馈，准备优先级已自动调整为"
                f"{priority_labels[pack.job.priority]}。"
            )
            print(f"岗位学习包已生成: {files.markdown}")
        return 0
    if args.applications_command == "history":
        if args.job_id < 1:
            raise ValueError("岗位编号必须大于 0。")
        settings = get_settings()
        repository = JobRepository(settings.job_db_path)
        application = repository.get_application(args.job_id)
        if application is None:
            print(f"岗位 #{args.job_id} 尚无投递记录。")
            return 0
        print(
            f"岗位 #{args.job_id} 当前状态: "
            f"{status_labels[application.status]}"
        )
        events = repository.list_application_events(args.job_id)
        for event in events:
            previous = (
                status_labels[event.previous_status]
                if event.previous_status is not None
                else "无"
            )
            print(
                f"{event.occurred_at} | {previous} → "
                f"{status_labels[event.status]} | {event.source}"
            )
            if event.detail:
                print(f"   {event.detail}")
        return 0
    if args.applications_command == "summary":
        settings = get_settings()
        summary = JobRepository(settings.job_db_path).application_summary()
        print("求职漏斗")
        print(f"已建立投递记录: {summary.total}")
        print(f"已投递及以后: {summary.applied_or_later}")
        print(f"HR已读及以后: {summary.hr_read_or_later}")
        print(f"有效反馈: {summary.meaningful_responses}")
        print(f"筛选及以后: {summary.screening_or_later}")
        print(f"测评/笔试: {summary.assessment_or_written_test}")
        print(f"进入面试: {summary.interview_or_later}")
        print(f"需要优先准备: {summary.priority_preparation}")
        print(f"Offer: {summary.offers}")
        print(
            f"拒绝: {summary.rejected} | 长期无反馈: {summary.no_response} | "
            f"撤回: {summary.withdrawn}"
        )
        return 0
    if args.applications_command == "prep":
        if args.job_id < 1:
            raise ValueError("岗位编号必须大于 0。")
        settings = get_settings()
        application = JobRepository(settings.job_db_path).get_application(args.job_id)
        trigger_status: ApplicationStatus = (
            application.status if application is not None else "saved"
        )
        files, pack = _generate_preparation_for_job(
            args.job_id,
            trigger_status=trigger_status,
            output_dir=args.output_dir,
        )
        print(
            f"岗位 #{args.job_id} 准备优先级: "
            f"{priority_labels[pack.job.priority]}。"
        )
        print(f"学习与面试准备包: {files.markdown}")
        print(f"可追溯 JSON: {files.pack_json}")
        return 0
    if args.applications_command == "approve-resume":
        approved = approve_resume_visual_review(args.manifest_path)
        print(f"简历版面已由本人确认，批准记录已保存: {approved}")
        return 0
    if args.applications_command == "generate-resume":
        if args.job_id < 1:
            raise ValueError("岗位编号必须大于 0。")
        settings = get_settings()
        profile = load_profile(settings.profile_path)
        repository = JobRepository(settings.job_db_path)
        job = repository.get_job(args.job_id)
        result = repository.get_latest_match_result(args.job_id)
        if result is None:
            source = job.sources[0] if job.sources else None
            result = match_job_locally(
                profile,
                structure_job_locally(
                    job.jd_text,
                    company=job.company,
                    title=job.title,
                    location=job.location,
                    source=source.platform if source else "database",
                    source_url=source.source_url if source else None,
                ),
            )
            repository.add_match_result(job.job_id, result)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_dir = args.output_dir or (
            settings.output_dir
            / "applications"
            / (
                f"auto-job-{job.job_id}-{_safe_filename(job.company)}-"
                f"{_safe_filename(job.title)}-{stamp}"
            )
        )
        files = build_tailored_resume_draft(
            profile,
            settings.profile_path,
            job,
            result,
            settings.output_dir / "applications",
            settings.project_root,
            output_dir,
        )
        print(f"定向 STAR 简历草稿已生成: {files.directory}")
        print(f"DOCX: {files.docx}")
        print(f"PDF: {files.pdf}")
        print("机器质检已完成；请先打开 PDF 检查版面，暂未标记为可投。")
        print(
            "确认无误后运行: "
            f"job-agent applications approve-resume \"{files.manifest}\""
        )
        return 0
    if args.applications_command == "verify":
        if args.job_id < 1:
            raise ValueError("岗位编号必须大于 0。")
        settings = get_settings()
        profile = load_profile(settings.profile_path)
        repository = JobRepository(settings.job_db_path)
        job = repository.get_job(args.job_id)
        if args.pack:
            pack_path = args.pack.expanduser().resolve()
            pack = load_application_pack(pack_path)
        else:
            pack_path, pack = find_application_pack(
                settings.output_dir / "application-packs",
                args.job_id,
            )
        session, session_path = create_application_session(
            profile,
            job,
            pack,
            pack_path,
            sessions_dir=settings.browser_sessions_dir,
            target_url=args.url,
            mode="open_only",
        )
        profile_dir = settings.browser_profiles_dir / _safe_filename(
            session.allowed_domains[0]
        )
        verification, verification_path = verify_application_page(
            session_path,
            browser_profile_dir=profile_dir,
            browser_channel=args.browser,
            headless=not args.headed,
        )
        print(f"投递状态核验: {verification_path}")
        if verification.observed_status == "applied":
            existing_application = repository.get_application(args.job_id)
            if (
                existing_application is not None
                and existing_application.status
                not in {"saved", "ready_to_apply", "applied"}
            ):
                repository.update_application_verification(
                    args.job_id,
                    verification_method="visible_page_marker",
                    evidence_path=verification.screenshot_path,
                    source_url=session.target_url,
                )
                changed = False
            else:
                changed = repository.record_application_status(
                    args.job_id,
                    "applied",
                    source="browser_verification",
                    detail="岗位页显示明确的“已投递”标记。",
                    resume_path=session.resume_path,
                    application_pack_path=str(pack_path),
                    source_url=session.target_url,
                    verification_method="visible_page_marker",
                    evidence_path=verification.screenshot_path,
                )
            print("核验结果: 已投递；本地岗位状态已更新。")
            print(f"新增状态事件: {'是' if changed else '否（状态未变化）'}")
            return 0
        labels = {
            "not_applied": "页面仍显示可投递入口",
            "login_required": "需要本人登录后重试",
            "blocked": "核验被安全规则阻止",
            "unknown": "页面没有明确状态标记",
        }
        print(f"核验结果: {labels[verification.observed_status]}")
        if verification.blocker:
            print(f"说明: {verification.blocker}")
        return 2

    if args.applications_command in {"assist", "browser-demo"}:
        if args.job_id < 1:
            raise ValueError("岗位编号必须大于 0。")
        settings = get_settings()
        profile = load_profile(settings.profile_path)
        repository = JobRepository(settings.job_db_path)
        job = repository.get_job(args.job_id)
        if args.pack:
            pack_path = args.pack.expanduser().resolve()
            pack = load_application_pack(pack_path)
        else:
            pack_path, pack = find_application_pack(
                settings.output_dir / "application-packs",
                args.job_id,
            )

        local_demo = args.applications_command == "browser-demo"
        target_url = (
            (settings.project_root / "tests" / "fixtures" / "application_form.html")
            .resolve()
            .as_uri()
            if local_demo
            else args.url
        )
        mode = "safe_fill" if local_demo else "open_only"
        session, session_path = create_application_session(
            profile,
            job,
            pack,
            pack_path,
            sessions_dir=settings.browser_sessions_dir,
            target_url=target_url,
            mode=mode,
            local_demo=local_demo,
        )
        ready_fields = sum(item.status == "ready" for item in session.fields)
        print(f"投递准备会话已创建: {session_path}")
        if not local_demo:
            print(f"岗位专属 PDF: {session.resume_path or '尚未准备'}")
            print("当前只打开岗位页面；登录、填写、作品集、上传与提交全部由本人完成。")
        else:
            print(f"可安全填写/上传: {ready_fields} 项")
            print(f"仍需本人处理或确认: {len(session.blockers)} 项")
            print("最终提交、验证码、测评、薪资、地点、调剂和主观题均保持硬暂停。")

        if not local_demo and args.plan_only:
            print("当前为计划模式，未打开网页，也未发送任何个人信息。")
            return 0

        channel = "chromium" if local_demo else args.browser
        headless = True if local_demo else args.headless
        keep_open = False if local_demo else not (args.close or args.headless)
        profile_dir = (
            settings.browser_profiles_dir
            / _safe_filename(session.allowed_domains[0])
        )
        report, report_path = run_browser_session(
            session_path,
            browser_profile_dir=profile_dir,
            headless=headless,
            keep_open=keep_open,
            browser_channel=channel,
            max_actions=12 if local_demo else args.max_actions,
            confirmed_entry_text=None,
            confirmation_token=None,
            confirmed_availability_answers=None,
        )
        print(f"浏览器审计报告: {report_path}")
        print(f"已安全填写: {len(report.filled_fields)} 项")
        print(f"已上传定向简历: {'是' if report.uploaded_resume else '否'}")
        print(f"检测到需本人处理字段: {report.manual_fields_detected} 项")
        print(
            f"检测到提交控件: {report.submit_controls_detected} 个；"
            f"Agent 点击: {1 if report.submit_attempted else 0} 次"
        )
        if local_demo:
            if report.observed_demo_submit_count != 0 or report.submit_attempted:
                raise BrowserAssistError("本地安全演示发现提交动作，测试失败。")
            print("本地安全演示通过：页面提交计数为 0。")
        return 0
    if args.applications_command != "prepare":
        raise ValueError(f"未知投递材料命令: {args.applications_command}")
    if args.job_id < 1:
        raise ValueError("岗位编号必须大于 0。")

    settings = get_settings()
    profile = load_profile(settings.profile_path)
    repository = JobRepository(settings.job_db_path)
    job = repository.get_job(args.job_id)
    result = repository.get_latest_match_result(args.job_id)
    generated_match = False
    if result is None:
        source = job.sources[0] if job.sources else None
        structured = structure_job_locally(
            job.jd_text,
            company=job.company,
            title=job.title,
            location=job.location,
            source=source.platform if source else "database",
            source_url=source.source_url if source else None,
        )
        result = match_job_locally(profile, structured)
        repository.add_match_result(job.job_id, result)
        generated_match = True

    resume = resolve_resume_bundle(
        profile,
        job,
        settings.output_dir / "applications",
        content_path=args.resume_content,
    )
    generated_resume = None
    if resume.status == "needs_generation" and not args.skip_resume_generation:
        resume_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        resume_output = (
            settings.output_dir
            / "applications"
            / (
                f"auto-job-{job.job_id}-{_safe_filename(job.company)}-"
                f"{_safe_filename(job.title)}-{resume_stamp}"
            )
        )
        try:
            generated_resume = build_tailored_resume_draft(
                profile,
                settings.profile_path,
                job,
                result,
                settings.output_dir / "applications",
                settings.project_root,
                resume_output,
            )
        except TailoredResumeError as exc:
            print(f"警告: 自动定向简历草稿未生成: {exc}", file=sys.stderr)
        else:
            resume = resolve_resume_bundle(
                profile,
                job,
                settings.output_dir / "applications",
                content_path=generated_resume.content_json,
            )
    pack = build_application_pack(profile, job, result, resume)
    if args.output_dir:
        output_dir = args.output_dir
    else:
        stamp = pack.generated_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_dir = (
            settings.output_dir
            / "application-packs"
            / (
                f"job-{job.job_id}-{_safe_filename(job.company)}-"
                f"{_safe_filename(job.title)}"
            )
            / stamp
        )
    files = write_application_pack(pack, job, result, output_dir)

    print(f"投递材料包已生成: {files.directory}")
    print(f"匹配分: {pack.job.match_score}/100")
    resume_label = {
        "ready": "已关联并通过质检",
        "needs_review": "草稿已生成，等待本人预览 PDF",
        "needs_generation": "尚待生成",
    }[pack.resume.status]
    print(f"定向简历: {resume_label}")
    print(
        "提交前待本人确认: "
        + str(sum(item.blocks_submission for item in pack.review_checklist))
        + " 项"
    )
    if generated_match:
        print("该岗位原本没有评分，已自动完成本地评分并存入岗位库。")
    if generated_resume:
        print(f"新简历 DOCX: {generated_resume.docx}")
        print(f"新简历 PDF: {generated_resume.pdf}")
        print(
            "看完 PDF 确认版面无误后，运行: "
            f"job-agent applications approve-resume \"{generated_resume.manifest}\""
        )
    print(f"先查看: {files.materials_markdown}")
    return 0


def _print_match(result_path: Path, result: object) -> None:
    from job_agent.models.job import MatchResult

    assert isinstance(result, MatchResult)
    print(f"匹配分: {result.overall_score}/100（{result.recommendation.zh}）")
    print(f"评分引擎: {result.engine}")
    if result.why_fit:
        print("为什么适合:")
        for item in result.why_fit:
            print(f"  - {item}")
    if result.why_not_fit:
        print("为什么不适合:")
        for item in result.why_not_fit:
            print(f"  - {item}")
    if result.hard_gates:
        print("硬性门槛:")
        for gate in result.hard_gates:
            print(f"  - [{gate.status.value}] {gate.requirement}: {gate.explanation}")
    if result.unknowns:
        print("需要确认:")
        for item in result.unknowns:
            print(f"  - {item}")
    print(f"完整结果: {result_path}")


def _match(args: argparse.Namespace) -> int:
    settings = get_settings()
    profile = load_profile(settings.profile_path)
    if not profile.application_context()["experiences"] and not profile.application_context()["skills"]:
        raise ProfileStoreError("Profile 中还没有可用于匹配的已确认经历或技能。")
    jd_path = args.jd_path.expanduser().resolve()
    if not jd_path.is_file():
        raise FileNotFoundError(f"JD 文件不存在: {jd_path}")
    raw_jd = jd_path.read_text(encoding="utf-8-sig").strip()
    if not raw_jd:
        raise ValueError("JD 文件为空。")

    if args.engine == "local":
        job = structure_job_locally(
            raw_jd,
            company=args.company,
            title=args.title,
            location=args.location,
            source=args.source,
            source_url=args.source_url,
        )
        result = match_job_locally(profile, job)
    else:
        from job_agent.services.ai_provider import build_job_match_provider

        provider_id = args.provider or settings.ai_provider
        if args.engine == "openai":
            provider_id = "openai"
        provider = build_job_match_provider(
            provider_id,
            settings.ai_model,
            api_key=settings.ai_api_key,
            base_url=settings.ai_base_url,
        )
        result = provider.match_job(
            profile,
            raw_jd,
            company=args.company,
            title=args.title,
            location=args.location,
            source=args.source,
            source_url=args.source_url,
        )
        record_api_usage(
            settings.private_dir / "api-usage.json",
            "ai",
            provider_id,
        )

    if args.output:
        output_path = args.output.expanduser().resolve()
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_path = settings.output_dir / (
            f"match-{_safe_filename(result.job.company)}-"
            f"{_safe_filename(result.job.title)}-{stamp}.json"
        )
    write_json_atomic(result.model_dump(mode="json"), output_path)
    _print_match(output_path, result)
    return 0


def _configure_output_encoding() -> None:
    """Use UTF-8 for captured Windows output without changing an interactive console."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        if (
            stream is not None
            and not stream.isatty()
            and hasattr(stream, "reconfigure")
        ):
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    _configure_output_encoding()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            code = _doctor()
        elif args.command == "profile":
            code = _profile_command(args)
        elif args.command == "resume":
            code = _resume_command(args)
        elif args.command == "jobs":
            code = _jobs_command(args)
        elif args.command == "match":
            code = _match(args)
        elif args.command == "applications":
            code = _applications_command(args)
        elif args.command == "dashboard":
            settings = get_settings()
            run_dashboard(
                JobRepository(settings.job_db_path),
                output_dir=settings.output_dir,
                profile_path=settings.profile_path,
                private_dir=settings.private_dir,
                port=args.port,
                open_browser=not args.no_open,
            )
            code = 0
        else:
            parser.error("未知命令")
            return
    except (
        FileNotFoundError,
        OSError,
        ProfileStoreError,
        ApplicationPackError,
        PreparationPackError,
        BrowserAssistError,
        ContactImportError,
        TailoredResumeError,
        JobDatabaseError,
        JobSearchProviderError,
        DashboardError,
        ResumeReadError,
        StatusSyncError,
        ValidationError,
        ValueError,
    ) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        code = 1
    except RuntimeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)
