from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import webbrowser
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit

from job_agent.models.job_record import JobDetail, JobRecordInput
from job_agent.models.profile import Profile
from job_agent.services.application_pack import build_application_pack, write_application_pack, ApplicationPackError, resolve_resume_bundle
from job_agent.services.browser_assist import BrowserAssistError, create_application_session, find_application_pack
from job_agent.services.commute_routing import AmapCommuteProvider, CommuteRoutingError
from job_agent.services.api_usage import load_api_usage, record_api_usage
from job_agent.services.job_repository import JobDatabaseError, JobRepository
from job_agent.services.job_strategy import evaluate_job_strategy
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.match_refresh import ensure_current_match
from job_agent.models.job import MatchResult
from job_agent.services.portable_resume import (
    PortableResumeError, build_portable_resume_content, build_portable_resume_draft,
    find_latest_resume_manifest, find_profile_photo, resume_artifact_from_manifest
)
from job_agent.services.profile_store import ProfileStoreError, load_profile
from job_agent.services.project_workshop import ProjectWorkshopError, project_preview_path, project_workshop_snapshot, run_project, verify_project
from job_agent.services.resume_editor import ResumeEditorError, load_latest_resume_content, rerender_edited_resume, validate_resume_content
from job_agent.services.resume_polish import CloudAIUnavailableError, ResumePolishError, polish_resume_content_locally, polish_resume_content_with_jd
from job_agent.services.resume_compose import compose_resume_content_with_jd
from job_agent.services.resume_import import ResumeImportError, import_resume_text_into_draft
from job_agent.services.runtime_config import RuntimeConfigError, effective_runtime_config
from job_agent.services.tailored_resume import TailoredResumeError, approve_resume_visual_review
from job_agent.constants import MAX_JSON_BODY as _MAX_JSON_BODY
from job_agent.services.browser_use_agent import (
    BrowserUseError,
    check_cdp_available,
    get_chrome_launch_instructions,
    is_browser_use_available,
    run_browser_use_assist,
)
from job_agent.services.sms_sync import (
    clear_latest_code,
    get_latest_code,
    get_sms_service_status,
    record_sms,
    start_sms_listener,
)

from job_agent.services.dashboard_routes import route
from job_agent.services.dashboard_routes.dashboard_api import (
    _profile_summary, _commute_fit, _latest_preparation_path, _safe_slug, _job_workspace, _job_source_url
)

logger = logging.getLogger(__name__)


@route("POST", r"/api/resume/preview")
def handle_resume_preview(handler):
    """A transient PDF, not a saved/approved application artifact."""
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    from job_agent.services.resume_preview import render_resume_preview
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("请使用 JSON 请求。")
        payload = json.loads(handler._read_body(256 * 1024).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求必须为 JSON 对象。")
        job_id = payload.get("job_id")
        if type(job_id) is not int or job_id < 1:
            raise ValueError("请指定有效岗位 ID。")
        job = handler.repository.get_job(job_id)
        pdf, pages = render_resume_preview(payload.get("content"), job, handler.private_dir)
    except (ValueError, UnicodeDecodeError, JobDatabaseError, ResumeEditorError):
        handler._json({"error": "无法预览：请检查岗位归属和简历内容是否完整。"}, HTTPStatus.BAD_REQUEST)
        return
    except Exception:
        # Renderer errors can contain private paths or text; do not expose them.
        logger.warning("Local resume preview rendering failed")
        handler._json({"error": "本地 PDF 预览暂不可用，请检查照片或调整内容长度后重试。"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return
    handler._headers(HTTPStatus.OK, "application/pdf", {
        "Content-Length": str(len(pdf)), "Content-Disposition": 'inline; filename="resume-preview.pdf"',
        "X-Resume-Preview-Pages": str(pages), "X-Resume-Preview-Renderer": "local_renderer",
    })
    handler.wfile.write(pdf)


@route("POST", r"/api/resume/tailor-live")
def handle_resume_tailor_live(handler):
    """Read-only suggestions; never persist edited claims or call a cloud model."""
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    from job_agent.services.resume_tailor_live import tailor_live
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("请使用 JSON 请求。")
        payload = json.loads(handler._read_body(256 * 1024).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求必须为 JSON 对象。")
        job_id = payload.get("job_id")
        if type(job_id) is not int or job_id < 1:
            raise ValueError("请指定有效岗位 ID。")
        handler.repository.get_job(job_id)
        result = tailor_live(load_profile(handler.profile_path),
            jd_text=payload.get("jd_text"), paragraph=payload.get("paragraph", ""),
            fact_ids=payload.get("fact_ids", []), resume_text=payload.get("resume_text"))
    except (ValueError, UnicodeDecodeError, JobDatabaseError, ProfileStoreError, OSError):
        handler._json({"error": "无法生成建议：请检查岗位、档案和输入长度。"}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(result)

_LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

def _open_job_page(url: str | None) -> None:
    parsed = urlsplit(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("这个岗位没有有效的招聘链接，请先补充原职位链接。")
    if not webbrowser.open(url, new=2):
        raise ValueError("系统浏览器未能启动，请检查 Windows 默认浏览器，或复制原职位链接手动打开。")

def _stage_resume_for_manual_upload(
    profile: Profile,
    job: JobDetail,
    resume_path: Path,
    *,
    output_dir: Path,
) -> Path:
    source = resume_path.expanduser().resolve()
    if not source.is_file():
        raise BrowserAssistError("岗位专属 PDF 不存在，无法准备人工上传。")
    target_dir = (output_dir / "ready-to-upload").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    name = _safe_slug(profile.person.display_name or "求职者", fallback="求职者")[:20]
    company = _safe_slug(job.company, fallback="公司")[:28]
    title = _safe_slug(job.title, fallback="岗位")[:36]
    target = target_dir / f"{name}-{company}-{title}-岗位专用简历.pdf"
    shutil.copy2(source, target)
    return target

def _reveal_resume_in_explorer(path: Path) -> bool:
    try:
        subprocess.Popen(
            ["explorer.exe", f"/select,{path}"],
            close_fds=True,
        )
    except OSError:
        return False
    return True

def _ensure_match(
    repository: JobRepository,
    profile: Profile,
    job: JobDetail,
) -> MatchResult:
    return ensure_current_match(repository, profile, job)

def _application_pack_output_dir(output_dir: Path, job: JobDetail) -> Path:
    job_root = (
        output_dir
        / "application-packs"
        / f"job-{job.job_id}-{_safe_slug(job.company, fallback='company')}-{_safe_slug(job.title, fallback='role')}"
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return job_root / stamp

def _public_assist_plan(session: object) -> dict[str, object]:
    fields = list(getattr(session, "fields", []))
    return {
        "session_id": getattr(session, "session_id"),
        "job_id": getattr(session, "job_id"),
        "company": getattr(session, "company"),
        "title": getattr(session, "title"),
        "platform": getattr(session, "platform"),
        "status": getattr(session, "status"),
        "ready_fields": [
            item.label
            for item in fields
            if item.category == "safe_objective" and item.status == "ready"
        ],
        "manual_fields": [
            item.label for item in fields if item.category == "manual"
        ],
        "blocked_fields": [
            item.label for item in fields if item.category == "blocked"
        ],
        "blockers": list(getattr(session, "blockers", [])),
        "hard_stops": [
            item.detail for item in list(getattr(session, "hard_stops", []))
        ],
        "final_submit": "blocked",
    }

@route("GET", r"/api/jobs/(\d+)/detail")
def handle_job_detail(handler, job_id_str):
    try:
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        if handler.profile_path is not None and handler.profile_path.is_file():
            ensure_current_match(handler.repository, load_profile(handler.profile_path), job)
            job = handler.repository.get_job(job_id)
        events = handler.repository.list_application_events(job_id)
        insight = handler.repository.get_latest_match_insights([job_id]).get(job_id)
        gaps = (
            [str(item) for item in insight.get("gaps", [])]
            if isinstance(insight, dict)
            else []
        )
        summary = _profile_summary(handler.profile_path)
        strategy = evaluate_job_strategy(
            job,
            match_score=job.match_score,
            commute_fit=_commute_fit(
                job.commute_minutes,
                summary.max_one_way_minutes if summary else None,
            ),
            daily_pay_floor=(
                summary.internship_daily_pay_floor if summary else None
            ),
            exclude_outsourcing=(
                summary.exclude_outsourcing if summary else True
            ),
            primary_roles=(summary.target_roles if summary else []),
            adjacent_roles=(summary.adjacent_roles if summary else []),
            today=datetime.now(_LOCAL_TIMEZONE).date(),
        )
        handler._json({
            "job": {
                "job_id": job.job_id,
                "company": job.company,
                "title": job.title,
                "jd_text": job.jd_text,
            },
            "match_insight": insight,
            "strategy": strategy.model_dump(mode="json"),
            "project_workshop": project_workshop_snapshot(
                handler.output_dir,
                job,
                gaps=gaps,
            ),
            "preparation_url": f"/preparation/{job_id}" if _latest_preparation_path(handler.output_dir, job_id) else None,
            "events": [
                {
                    "status": event.status,
                    "detail": event.detail,
                    "occurred_at": event.occurred_at,
                }
                for event in events
            ],
        })
    except JobDatabaseError:
        handler._json({"error": "职位不存在或暂时无法读取。"}, HTTPStatus.NOT_FOUND)
    except ProjectWorkshopError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.CONFLICT)
    except ProfileStoreError:
        handler._json({"error": "个人档案暂时无法读取，请检查资料后重试。"}, HTTPStatus.CONFLICT)

@route("GET", r"/api/jobs/(\d+)/resume-content")
def handle_resume_content_get(handler, job_id_str):
    job_id = int(job_id_str)
    try:
        content, manifest_path = load_latest_resume_content(
            handler.output_dir / "applications",
            job_id,
        )
    except ResumeEditorError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        return
    handler._json(
        {
            "job_id": job_id,
            "content": content,
            "manifest_path": str(manifest_path),
            "template": "portable-evidence-resume-v1",
            "photo_available": find_profile_photo(handler.private_dir) is not None,
        }
    )

@route("GET", r"/resume-draft/(\d+)/(pdf|docx)")
def handle_resume_draft_download(handler, job_id_str, kind):
    job_id = int(job_id_str)
    manifest = find_latest_resume_manifest(
        handler.output_dir / "applications",
        job_id,
    )
    if manifest is None:
        handler._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
        handler.wfile.write("尚未生成该岗位的简历草稿。".encode("utf-8"))
        return
    try:
        artifact = resume_artifact_from_manifest(manifest, kind)
    except PortableResumeError as exc:
        handler._headers(HTTPStatus.CONFLICT, "text/plain; charset=utf-8")
        handler.wfile.write(str(exc).encode("utf-8"))
        return
    content_type = (
        "application/pdf"
        if kind == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    disposition = "inline" if kind == "pdf" else "attachment"
    body = artifact.read_bytes()
    handler._headers(
        HTTPStatus.OK,
        content_type,
        {"Content-Disposition": f'{disposition}; filename="job-{job_id}-resume.{kind}"'},
    )
    handler.wfile.write(body)

@route("GET", r"/project-workshop/(\d+)/preview")
def handle_project_preview(handler, job_id_str):
    try:
        preview = project_preview_path(
            handler.output_dir,
            int(job_id_str),
        )
    except ProjectWorkshopError as exc:
        handler._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
        handler.wfile.write(str(exc).encode("utf-8"))
        return
    body = preview.read_bytes()
    handler._headers(HTTPStatus.OK, "text/html; charset=utf-8")
    handler.wfile.write(body)

@route("GET", r"/preparation/(\d+)")
def handle_preparation(handler, job_id_str):
    pack_path = _latest_preparation_path(handler.output_dir, int(job_id_str))
    if pack_path is None:
        handler._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
        handler.wfile.write("尚未生成岗位学习包。".encode("utf-8"))
        return
    body = pack_path.read_bytes()
    handler._headers(HTTPStatus.OK, "text/plain; charset=utf-8")
    handler.wfile.write(body)

@route("POST", r"/api/jobs/(\d+)/project-workshop/run")
def handle_project_run(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith(
            "application/json"
        ):
            raise ValueError("项目工坊操作必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            raise ValueError("请先确认生成或重跑这个本地项目。")
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        insight = handler.repository.get_latest_match_insights([job_id]).get(job_id)
        gaps = (
            [str(item) for item in insight.get("gaps", [])]
            if isinstance(insight, dict)
            else []
        )
        project = run_project(
            handler.output_dir,
            job,
            config_update=payload.get("config"),
            gaps=gaps,
        )
    except (
        UnicodeDecodeError, json.JSONDecodeError, ValueError,
        JobDatabaseError, ProjectWorkshopError, OSError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True, "project_workshop": project})

@route("POST", r"/api/jobs/(\d+)/project-workshop/verify")
def handle_project_verify(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith(
            "application/json"
        ):
            raise ValueError("项目验证必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            raise ValueError("请先确认由本人完成项目验证。")
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        insight = handler.repository.get_latest_match_insights([job_id]).get(job_id)
        gaps = (
            [str(item) for item in insight.get("gaps", [])]
            if isinstance(insight, dict)
            else []
        )
        project = verify_project(
            handler.output_dir,
            job,
            explanation=str(payload.get("explanation") or ""),
            demo_confirmed=payload.get("demo_confirmed") is True,
            understanding_confirmed=payload.get("understanding_confirmed") is True,
            gaps=gaps,
        )
    except (
        UnicodeDecodeError, json.JSONDecodeError, ValueError,
        JobDatabaseError, ProjectWorkshopError, OSError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True, "project_workshop": project})

@route("POST", r"/api/jobs/(\d+)/resume-content/polish")
def handle_resume_polish(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    job_id = int(job_id_str)
    try:
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            raise ValueError("请先确认本次润色。")
        job = handler.repository.get_job(job_id)
        content, _ = load_latest_resume_content(
            handler.output_dir / "applications",
            job_id,
        )
        if "content" in payload:
            content = validate_resume_content(payload["content"])
            if int(content["target"]["job_id"]) != job_id:
                raise ValueError("简历与当前岗位不一致。")
        config, _ = effective_runtime_config(handler.runtime_config_path)
        warning = ""
        if config.ai.provider == "local":
            suggestion = polish_resume_content_locally(content, job.jd_text)
            polish_engine = "local"
        else:
            try:
                suggestion = polish_resume_content_with_jd(
                    content,
                    job.jd_text,
                    config=config,
                )
            except CloudAIUnavailableError as exc:
                raise ResumePolishError(f"{exc} 请修复连接后重试；原稿未修改。") from exc
            else:
                polish_engine = "cloud"
                record_api_usage(
                    handler.api_usage_path,
                    "ai",
                    config.ai.provider,
                    successful_requests=1,
                    input_tokens=suggestion.input_tokens,
                    output_tokens=suggestion.output_tokens,
                )
    except (ValueError, ResumeEditorError, ResumePolishError, RuntimeConfigError, JobDatabaseError, OSError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(
        {
            "ok": True,
            "job_id": job_id,
            "content": suggestion.content,
            "changes": suggestion.changes,
            "change_count": len(suggestion.changes),
            "generation": suggestion.content.get("generation", {}),
            "engine": polish_engine,
            "warning": warning,
            "note": "润色仅为建议；请在编辑器中检查后保存，保存后仍需通过 PDF 版面人工审阅。",
        }
    )

@route("POST", r"/api/jobs/(\d+)/resume-content/import")
def handle_resume_import(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    job_id = int(job_id_str)
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("原简历导入必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("resume_text"), str):
            raise ValueError("请求需包含 resume_text 文本字段。")
        job = handler.repository.get_job(job_id)
        content, _ = load_latest_resume_content(handler.output_dir / "applications", job_id)
        config, _ = effective_runtime_config(handler.runtime_config_path)
        if (
            config.ai.monthly_quota is not None
            and load_api_usage(handler.api_usage_path).ai.successful_requests >= config.ai.monthly_quota
        ):
            raise ValueError("本月 AI 请求额度已用完，无法执行原简历导入。")
        suggestion = import_resume_text_into_draft(payload["resume_text"], content, config=config)
        record_api_usage(
            handler.api_usage_path,
            "ai",
            config.ai.provider,
            successful_requests=1,
            input_tokens=suggestion.input_tokens,
            output_tokens=suggestion.output_tokens,
        )
    except (ValueError, ResumeEditorError, ResumeImportError, RuntimeConfigError, JobDatabaseError, OSError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(
        {
            "ok": True,
            "job_id": job_id,
            "content": suggestion.content,
            "warnings": suggestion.warnings,
            "note": "导入仅为建议：AI 已把原简历解析为表单内容；请逐项检查（姓名、联系方式、量化数字）后再保存，保存后仍需通过 PDF 版面人工审阅。",
        }
    )

@route("POST", r"/api/jobs/(\d+)/resume-content/revise")
def handle_resume_revise(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    job_id = int(job_id_str)
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("Agent 修改简历必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        instruction = str(payload.get("instruction", "")).strip() if isinstance(payload, dict) else ""
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            raise ValueError("请先确认让 Agent 按这条要求直接修改简历。")
        if not instruction:
            raise ValueError("请告诉 Agent 需要怎样修改简历。")
        if len(instruction) > 2000:
            raise ValueError("单次修改要求不能超过 2000 个字符。")

        photo_path = find_profile_photo(handler.private_dir)
        if photo_path is None:
            raise ValueError("请先在“个人资料与简历”中上传证件照；当前默认模板要求右上角证件照。")
        job = handler.repository.get_job(job_id)
        config, _ = effective_runtime_config(handler.runtime_config_path)
        try:
            content, manifest_path = load_latest_resume_content(
                handler.output_dir / "applications",
                job_id,
            )
        except ResumeEditorError:
            profile = load_profile(handler.profile_path)
            result = _ensure_match(handler.repository, profile, job)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            draft_dir = (
                handler.output_dir
                / "applications"
                / (
                    f"portable-job-{job_id}-"
                    f"{_safe_slug(job.company, fallback='company')}-"
                    f"{_safe_slug(job.title, fallback='role')}-{stamp}"
                )
            )
            build_portable_resume_draft(profile, job, result, draft_dir, photo_path=photo_path)
            content, manifest_path = load_latest_resume_content(
                handler.output_dir / "applications",
                job_id,
            )
        person = dict(content.get("person") or {})
        person["photo_path"] = str(photo_path.resolve())
        content["person"] = person
        quota_available = not (
            config.ai.monthly_quota is not None
            and load_api_usage(handler.api_usage_path).ai.successful_requests >= config.ai.monthly_quota
        )
        try:
            if not quota_available:
                raise ResumePolishError("AI 请求额度已用完。")
            suggestion = compose_resume_content_with_jd(
                content,
                job.jd_text,
                profile=load_profile(handler.profile_path),
                config=config,
                user_instruction=instruction,
            )
        except ResumePolishError as exc:
            if config.ai.provider != "local":
                raise ResumePolishError(f"{exc} 本次未生成替代简版，请修复连接后重试；原有简历保留。") from exc
            suggestion = polish_resume_content_locally(
                content,
                job.jd_text,
                user_instruction=instruction,
            )
            suggestion.content["generation"] = {"engine": "local_fallback", "warning": str(exc), "review_required": True}
        else:
            record_api_usage(
                handler.api_usage_path,
                "ai",
                config.ai.provider,
                successful_requests=1,
                input_tokens=suggestion.input_tokens,
                output_tokens=suggestion.output_tokens,
            )
        edited = rerender_edited_resume(
            suggestion.content,
            handler.output_dir / "applications",
            based_on=str(manifest_path),
            edit_origin="agent_dialog",
        )
        profile = load_profile(handler.profile_path)
        workspace = _job_workspace(profile, job, output_dir=handler.output_dir)
        edited_manifest = json.loads(edited.manifest.read_text(encoding="utf-8"))
    except (
        UnicodeDecodeError, json.JSONDecodeError, ValueError, ResumeEditorError, ResumePolishError,
        RuntimeConfigError, PortableResumeError, JobDatabaseError, ProfileStoreError, OSError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(
        {
            "ok": True,
            "job_id": job_id,
            "version_id": edited_manifest.get("version_id"),
            "change_count": len(suggestion.changes),
            "workspace": workspace.model_dump(mode="json"),
            "resume_url": f"/resume-draft/{job_id}/pdf",
            "note": "Agent 已直接生成新版简历；请打开 PDF 核对事实与版面，确认前不会进入可投递状态。",
        }
    )

@route("POST", r"/api/jobs/(\d+)/resume-content")
def handle_resume_content_post(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    job_id = int(job_id_str)
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("简历编辑必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("confirmed") is not True
            or not isinstance(payload.get("content"), dict)
        ):
            raise ValueError("请先在编辑器中确认修改；请求需包含 confirmed 与 content。")
        job = handler.repository.get_job(job_id)
        profile = load_profile(handler.profile_path)
        _, manifest_path = load_latest_resume_content(
            handler.output_dir / "applications",
            job_id,
        )
        edited = rerender_edited_resume(
            payload["content"],
            handler.output_dir / "applications",
            based_on=str(manifest_path),
        )
        workspace = _job_workspace(profile, job, output_dir=handler.output_dir)
    except (
        UnicodeDecodeError, json.JSONDecodeError, ValueError, ResumeEditorError,
        PortableResumeError, JobDatabaseError, ProfileStoreError, OSError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(
        {
            "ok": True,
            "job_id": job_id,
            "version_id": json.loads(edited.manifest.read_text(encoding="utf-8")).get("version_id"),
            "workspace": workspace.model_dump(mode="json"),
        }
    )

@route("POST", r"/api/jobs/(\d+)/resume-draft")
def handle_resume_draft(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("简历草稿操作必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            raise ValueError("请先确认生成该岗位的专属简历草稿。")
        profile = load_profile(handler.profile_path)
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        result = _ensure_match(handler.repository, profile, job)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        job = handler.repository.get_job(job_id)
        draft_dir = (
            handler.output_dir
            / "applications"
            / (
                f"portable-job-{job_id}-"
                f"{_safe_slug(job.company, fallback='company')}-"
                f"{_safe_slug(job.title, fallback='role')}-{stamp}"
            )
        )
        photo_path = find_profile_photo(handler.private_dir)
        if photo_path is None:
            raise ValueError("请先在“个人资料与简历”中上传证件照；当前默认模板要求右上角证件照。")
        content = build_portable_resume_content(
            profile,
            job,
            result,
            generated_at=datetime.now(UTC),
            photo_path=photo_path,
        )
        config, _ = effective_runtime_config(handler.runtime_config_path)
        quota_available = not (
            config.ai.monthly_quota is not None
            and load_api_usage(handler.api_usage_path).ai.successful_requests >= config.ai.monthly_quota
        )
        try:
            if not quota_available:
                raise ResumePolishError("AI 请求额度已用完。")
            suggestion = compose_resume_content_with_jd(
                content,
                job.jd_text,
                profile=profile,
                config=config,
                user_instruction="按 JD 直接生成岗位专属简历，使用 STAR 法则突出相关真实经历。",
            )
        except ResumePolishError as exc:
            # 云端 AI 不可用或余额不足时，自动自适应降级为本地高精度确定性 STAR 引擎，保证简历 100% 成功生成
            suggestion = polish_resume_content_locally(
                content,
                job.jd_text,
                user_instruction="按 JD 生成岗位专属简历并按 STAR 组织真实经历。",
            )
            warning_msg = f"云端 AI 暂时不可用（{exc}），系统已自动启用本地高精度确定性算法生成专属简历。"
            suggestion.content["generation"] = {
                "engine": "local_fallback",
                "warning": warning_msg,
                "review_required": True,
            }
        else:
            record_api_usage(
                handler.api_usage_path,
                "ai",
                config.ai.provider,
                successful_requests=1,
                input_tokens=suggestion.input_tokens,
                output_tokens=suggestion.output_tokens,
            )
        build_portable_resume_draft(
            profile,
            job,
            result,
            draft_dir,
            photo_path=photo_path,
            content=suggestion.content,
        )
        workspace = _job_workspace(profile, job, output_dir=handler.output_dir)
    except (
        UnicodeDecodeError, json.JSONDecodeError, ValueError, ProfileStoreError,
        JobDatabaseError, PortableResumeError, ApplicationPackError,
        ResumePolishError, RuntimeConfigError, OSError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(
        {
            "ok": True,
            "job_id": job_id,
            "workspace": workspace.model_dump(mode="json"),
            "generation": suggestion.content.get("generation", {}),
        }
    )

@route("POST", r"/api/jobs/(\d+)/resume-draft/approve")
def handle_resume_draft_approve(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("简历批准操作必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("confirmed_pdf_review") is not True:
            raise ValueError("请先打开 PDF，并确认本人已检查版面与内容。")
        profile = load_profile(handler.profile_path)
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        result = _ensure_match(handler.repository, profile, job)
        resume = resolve_resume_bundle(
            profile,
            job,
            handler.output_dir / "applications",
        )
        if resume.status != "ready" or not resume.qa_verified:
            pending_manifest = find_latest_resume_manifest(
                handler.output_dir / "applications",
                job_id,
                visual_status="pending_user_review",
            )
            if pending_manifest is None:
                raise ValueError("没有找到等待本人审阅的岗位专属简历草稿。")
            approve_resume_visual_review(pending_manifest)
            resume = resolve_resume_bundle(
                profile,
                job,
                handler.output_dir / "applications",
            )
        if resume.status != "ready" or not resume.qa_verified:
            raise ValueError("简历批准记录未通过完整质检，未生成投递材料包。")
        pack = build_application_pack(profile, job, result, resume)
        pack_files = write_application_pack(
            pack,
            job,
            result,
            _application_pack_output_dir(handler.output_dir, job),
        )
        current = handler.repository.get_application(job_id)
        if current is None or current.status in {"saved", "ready_to_apply"}:
            handler.repository.record_application_status(
                job_id,
                "ready_to_apply",
                source="dashboard_resume_approval",
                detail="用户已审阅岗位专属 PDF；投递材料包已就绪，最终提交仍由本人完成。",
                resume_path=resume.pdf_path,
                application_pack_path=str(pack_files.pack_json),
                source_url=_job_source_url(job),
                verification_method="user_confirmed_pdf_review",
            )
        workspace = _job_workspace(profile, job, output_dir=handler.output_dir)
    except (
        UnicodeDecodeError, json.JSONDecodeError, ValueError, ProfileStoreError,
        JobDatabaseError, ApplicationPackError, TailoredResumeError, OSError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(
        {
            "ok": True,
            "job_id": job_id,
            "workspace": workspace.model_dump(mode="json"),
        }
    )

@route("POST", r"/api/jobs/(\d+)/archive-job")
def handle_archive_job(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求格式不正确。")
        handler.repository.set_job_archived(int(job_id_str), payload.get("ignored"))
    except (ValueError, OSError, JobDatabaseError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True})

@route("POST", r"/api/jobs/(\d+)/open-page")
def handle_open_page(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求格式不正确。")
        job = handler.repository.get_job(int(job_id_str))
        _open_job_page(_job_source_url(job))
    except (ValueError, OSError, JobDatabaseError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True})

@route("POST", r"/api/jobs/(\d+)/assist")
def handle_assist(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("打开投递页操作必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            raise ValueError("请先确认人工投递边界。")
        launch = payload.get("launch", False)
        if not isinstance(launch, bool):
            raise ValueError("launch 必须是布尔值。")
        profile = load_profile(handler.profile_path)
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        pack_path, pack = find_application_pack(
            handler.output_dir / "application-packs",
            job_id,
        )
        if pack.resume.status != "ready" or not pack.resume.qa_verified:
            raise ValueError("岗位专属简历尚未通过本人审阅，不能用于投递。")
        session, session_path = create_application_session(
            profile,
            job,
            pack,
            pack_path,
            sessions_dir=handler.private_dir / "browser-sessions",
            mode="open_only",
        )
        plan = _public_assist_plan(session)
        if not session.resume_path:
            raise ValueError("岗位专属 PDF 尚未准备好。")
        staged_resume = _stage_resume_for_manual_upload(
            profile,
            job,
            Path(session.resume_path),
            output_dir=handler.output_dir,
        )
        plan.update(
            {
                "mode": "manual_apply",
                "automatic_form_fill": False,
                "resume_file_name": staged_resume.name,
                "resume_path": str(staged_resume),
            }
        )
        with handler.assist_lock:
            handler.assist_runs[session.session_id] = {
                "session_id": session.session_id,
                "status": "planned",
                "submit_attempted": False,
            }
        if launch:
            plan["resume_folder_revealed"] = _reveal_resume_in_explorer(staged_resume)
            _open_job_page(_job_source_url(job))
            with handler.assist_lock:
                handler.assist_runs[session.session_id]["status"] = "opened"
    except (
        UnicodeDecodeError, json.JSONDecodeError, ValueError, ProfileStoreError,
        JobDatabaseError, BrowserAssistError, OSError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True, "launched": launch, "plan": plan})

@route("POST", r"/api/jobs/(\d+)/commute/route")
def handle_commute_route(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("路线计算必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("路线计算内容格式不正确。")
        if payload.get("confirmed") is not True:
            raise ValueError("请先确认本次将地址发送给已配置的地图服务。")
        destination = str(payload.get("destination", "")).strip()
        mode = str(payload.get("mode", "transit")).strip().casefold()
        if mode not in {"transit", "driving", "walking", "bicycling"}:
            raise ValueError("通勤方式不受支持。")
        profile = load_profile(handler.profile_path)
        origin = profile.job_search.commute.origin.strip()
        if not origin:
            raise ValueError("请先在“求职方向与通勤边界”中填写常用出发地址。")
        config, _ = effective_runtime_config(handler.runtime_config_path)
        if config.maps.provider != "amap" or not config.maps.api_key:
            raise ValueError("请先在“连接与 API”中配置高德地图 Web 服务 Key。")
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        origin_city = (
            profile.job_search.preferred_locations[0]
            if profile.job_search.preferred_locations
            else ""
        )
        provider = AmapCommuteProvider(config.maps.api_key)
        result = provider.calculate(
            origin_address=origin,
            destination_address=destination,
            mode=mode,  # type: ignore[arg-type]
            origin_city=origin_city,
            destination_city=job.location or origin_city,
        )
        record_api_usage(
            handler.api_usage_path,
            "maps",
            "amap",
            successful_requests=provider.request_count,
        )
        record = handler.repository.update_job_route(job_id, result)
    except (
        UnicodeDecodeError, json.JSONDecodeError, ValueError, ProfileStoreError,
        RuntimeConfigError, CommuteRoutingError, JobDatabaseError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(
        {
            "ok": True,
            "route": result.model_dump(mode="json"),
            "job": record.model_dump(mode="json"),
        }
    )

@route("POST", r"/api/jobs/(\d+)/commute")
def handle_commute(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("通勤记录必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("通勤记录内容格式不正确。")
        if payload.get("confirmed") is not True:
            raise ValueError("请先确认通勤时间由本人核实或估算。")
        raw_minutes = payload.get("minutes")
        if raw_minutes is None or raw_minutes == "":
            minutes = None
        elif isinstance(raw_minutes, bool):
            raise ValueError("单程通勤时间必须填写整数分钟。")
        elif isinstance(raw_minutes, int):
            minutes = raw_minutes
        elif isinstance(raw_minutes, str) and re.fullmatch(r"\d{1,3}", raw_minutes.strip()):
            minutes = int(raw_minutes)
        else:
            raise ValueError("单程通勤时间必须填写整数分钟。")
        method = str(payload.get("method", "user_estimate")).strip()
        if method not in {"user_estimate", "route_estimate", "remote", "unknown"}:
            raise ValueError("通勤估算方式不受支持。")
        note = str(payload.get("note", "")).strip()
        destination = str(payload.get("destination", "")).strip() or None
        job_id = int(job_id_str)
        record = handler.repository.update_job_commute(
            job_id,
            minutes,
            method=method,
            note=note,
            destination=destination,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, JobDatabaseError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True, "job": record.model_dump(mode="json")})

@route("POST", r"/api/jobs/import-parsed")
def handle_import_parsed_job(handler):
    is_authorized = handler._authorized_action() or handler._authorized_agent()
    if not is_authorized:
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("导入岗位必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(128 * 1024).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求内容格式不正确。")
        company = str(payload.get("company", "")).strip()
        title = str(payload.get("title", "")).strip()
        jd_text = str(payload.get("jd_text", "")).strip()
        if not company or not title or not jd_text:
            raise ValueError("公司名称、岗位名称和 JD 详情不能为空。")
        source = str(payload.get("source", "extension")).strip() or "extension"
        source_url = str(payload.get("source_url", "")).strip() or None
        location = str(payload.get("location", "")).strip() or None
        
        record = JobRecordInput(
            company=company,
            title=title,
            jd_text=jd_text,
            source=source,
            source_url=source_url,
            location=location,
        )
        outcome = handler.repository.upsert_job(record)
        
        profile = load_profile(handler.profile_path)
        structured = structure_job_locally(
            jd_text,
            company=company,
            title=title,
            location=location,
            source=source,
            source_url=source_url,
        )
        match_res = match_job_locally(profile, structured)
        handler.repository.add_match_result(outcome.job_id, match_res)
        
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, JobDatabaseError, ProfileStoreError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
        
    handler._json({
        "ok": True,
        "job_id": outcome.job_id,
        "created": outcome.created,
        "source_added": outcome.source_added,
        "dedupe_reason": outcome.dedupe_reason,
        "match_score": match_res.overall_score,
    })

@route("POST", r"/api/jobs/extract-jd")
def handle_extract_jd(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("提取请求必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(128 * 1024).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求内容格式不正确。")
        raw_text = str(payload.get("raw_text", "")).strip()
        if not raw_text:
            raise ValueError("待提取的招聘文本不能为空。")
            
        target_url = ""
        if raw_text.startswith(("http://", "https://")):
            target_url = raw_text.split()[0]
            try:
                from job_agent.services.safe_job_fetch import fetch_public_html
                html = fetch_public_html(target_url)
                cleaned_html = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
                clean_text = re.sub(r"<[^>]+>", " ", cleaned_html)
                clean_text = re.sub(r"&[a-zA-Z]+;", " ", clean_text)
                clean_text = re.sub(r"\s+", " ", clean_text).strip()
                if len(clean_text) < 60:
                    handler._json({"ok": False, "warning": "网页受反爬保护或为单页应用，建议直接复制岗位文字", "preserve_input": True})
                    return
                raw_text = clean_text
            except (ValueError, OSError, TimeoutError, HTTPException):
                handler._json({"ok": False, "warning": "链接不可安全读取或网页访问失败，建议直接复制岗位文字", "preserve_input": True})
                return

        config, _ = effective_runtime_config(handler.runtime_config_path)
        extracted = None
        
        if config.ai.provider in {"deepseek", "openai", "openai_compatible"}:
            try:
                from job_agent.services.ai_provider import get_openai_client
                client, model = get_openai_client(config)
                with client:
                    system_prompt = (
                        "你是求职 Agent 的专业岗位解析器。请从用户输入的杂乱招聘信息、微信复制文本或网页片段中，"
                        "提取并结构化为 JSON 格式。必须严格返回有效 JSON，包含以下键：\n"
                        "company: 公司名（必填，若无全称提取品牌名或简称）\n"
                        "title: 岗位名称（必填，如 Python开发实习生）\n"
                        "location: 工作城市/地点（若无填空字符串）\n"
                        "salary: 薪资或补贴（如 200-300/天，若无填空字符串）\n"
                        "jd_text: 清理后的岗位职责与要求完整文本（保留全部有效技术与业务要求）\n"
                        "skills: 核心技能标签列表（字符串数组，如 [\"Python\", \"MySQL\"]）"
                    )
                    chat_res = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": raw_text[:8000]},
                        ],
                        temperature=0.1,
                    )
                    content = (chat_res.choices[0].message.content or "").strip()
                    m = re.search(r"\{.*\}", content, re.DOTALL)
                    if m:
                        extracted = json.loads(m.group(0))
            except Exception as e:
                logger.warning("AI 结构化提取失败，回退到本地规则")
                extracted = None
                
        if not extracted or not isinstance(extracted, dict) or not extracted.get("title") or not extracted.get("company"):
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            company_candidate = "未知公司"
            title_candidate = "未命名岗位"
            for line in lines[:5]:
                if any(suffix in line for suffix in ("公司", "集团", "科技", "网络", "企业")):
                    company_candidate = line[:40]
                    break
            for line in lines[:5]:
                if any(k in line for k in ("实习", "工程师", "开发", "产品", "运营", "助理", "专家", "经理", "岗")):
                    title_candidate = line[:40]
                    break
            extracted = {
                "company": company_candidate,
                "title": title_candidate,
                "location": "",
                "salary": "",
                "jd_text": raw_text,
                "skills": [],
            }
        if target_url and isinstance(extracted, dict):
            extracted["source_url"] = target_url
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
        
    handler._json({"ok": True, "parsed": extracted})

@route("POST", r"/api/jobs/(\d+)/outreach")
def handle_job_outreach(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        profile = load_profile(handler.profile_path)
        config, _ = effective_runtime_config(handler.runtime_config_path)
        
        from job_agent.services.outreach_copilot import generate_greetings
        result = generate_greetings(job, profile, config)
    except (ValueError, JobDatabaseError, ProfileStoreError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
        
    handler._json({
        "ok": True,
        "engine": result.engine,
        "greetings": [
            {"style": g.style, "title": g.title, "content": g.content}
            for g in result.greetings
        ]
    })

@route("GET", r"/api/jobs/(\d+)/interview-prep")
def handle_job_interview_prep(handler, job_id_str):
    try:
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        profile = load_profile(handler.profile_path)
        config, _ = effective_runtime_config(handler.runtime_config_path)
        
        from job_agent.services.interview_copilot import generate_interview_prep
        prep = generate_interview_prep(job, profile, config)
    except (ValueError, JobDatabaseError, ProfileStoreError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
        
    handler._json({"ok": True, "data": prep.to_dict()})


@route("GET", r"/api/jobs/browser-use/status")
def handle_browser_use_status(handler):
    try:
        config, _ = effective_runtime_config(handler.runtime_config_path)
        avail = is_browser_use_available()
        cdp_ok = check_cdp_available()
        chrome_info = get_chrome_launch_instructions()
        ai_ready = bool(config.ai.api_key) if config.ai.provider != "local" else False
    except Exception as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return

    handler._json({
        "ok": True,
        "browser_use_available": avail,
        "cdp_connected": cdp_ok,
        "chrome_info": chrome_info,
        "ai_provider": config.ai.provider,
        "ai_model": config.ai.model,
        "ai_ready": ai_ready,
    })


@route("POST", r"/api/jobs/(\d+)/browser-use")
def handle_job_browser_use(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        payload = {}
        if handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            body = handler._read_body(_MAX_JSON_BODY)
            if body:
                payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求内容格式不正确。")

        dry_run = bool(payload.get("dry_run", False))
        cdp_url = str(payload.get("cdp_url", "http://localhost:9222")).strip() or "http://localhost:9222"
        headless = bool(payload.get("headless", False))

        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        profile = load_profile(handler.profile_path)
        config, _ = effective_runtime_config(handler.runtime_config_path)

        resume_path: Path | None = None
        try:
            _, pack = find_application_pack(handler.output_dir / "application-packs", job_id)
            if pack.resume and pack.resume.pdf_path and Path(pack.resume.pdf_path).is_file():
                resume_path = Path(pack.resume.pdf_path)
        except Exception:
            try:
                bundle = resolve_resume_bundle(profile, job, handler.output_dir / "applications")
                if bundle and bundle.pdf_path and Path(bundle.pdf_path).is_file():
                    resume_path = Path(bundle.pdf_path)
            except Exception:
                resume_path = None

        result = run_browser_use_assist(
            job=job,
            profile=profile,
            resume_path=resume_path,
            config=config,
            cdp_url=cdp_url,
            dry_run=dry_run,
            headless=headless,
        )
    except (ValueError, JobDatabaseError, ProfileStoreError, BrowserUseError, RuntimeConfigError, OSError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return

    handler._json({
        "ok": True,
        "result": result.model_dump(mode="json"),
    })


@route("GET", r"/api/sms/status")
def handle_sms_status(handler):
    try:
        status = get_sms_service_status()
    except Exception as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(status)


@route("POST", r"/api/sms/manual")
def handle_sms_manual(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        payload = _fill_payload(handler)
        from job_agent.services.fill_bridge import server_bridge
        res = server_bridge(handler).manual(payload.get("code", payload.get("message")),
            payload.get("request_id"), payload.get("session_id"), legacy_record=record_sms)
    except (ValueError, OSError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(res)


def _fill_payload(handler):
    if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
        raise ValueError("请使用 JSON 请求。")
    payload = json.loads(handler._read_body(8192).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("请求必须为 JSON 对象。")
    return payload


def _fill_agent_authorized(handler):
    origins = handler.headers.get_all("Origin", [])
    if len(origins) > 1 or (origins and not re.fullmatch(r"chrome-extension://[a-p]{32}", origins[0])):
        handler._reject_unauthorized_agent()
        return False
    if not handler._authorized_agent():
        handler._reject_unauthorized_agent()
        return False
    return True


def _fill_job_id(payload):
    value = payload.get("job_id")
    if type(value) is not int or value < 1:
        raise ValueError("请指定有效岗位 ID。")
    return value


def _fill_session_id(payload):
    value = payload.get("session_id")
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32,128}", value):
        raise ValueError("代填会话无效。")
    return value


@route("POST", r"/api/fill/session")
def handle_fill_session(handler):
    if not _fill_agent_authorized(handler):
        return
    from job_agent.services.fill_bridge import server_bridge
    try:
        payload = _fill_payload(handler)
        job_id = _fill_job_id(payload)
        job = handler.repository.get_job(job_id)
        result = server_bridge(handler).create(job_id, payload.get("url"), _job_source_url(job))
    except (ValueError, JobDatabaseError):
        handler._json({"error": "无法连接代填：请确认岗位存在且当前 HTTPS 页面与岗位来源域名一致。"}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(result)


@route("POST", r"/api/fill/sessions")
def handle_fill_sessions(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    from job_agent.services.fill_bridge import server_bridge
    try:
        job_id = _fill_job_id(_fill_payload(handler))
        handler.repository.get_job(job_id)
        result = server_bridge(handler).list(job_id)
    except (ValueError, JobDatabaseError):
        handler._json({"error": "无法读取岗位代填会话。"}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"sessions": result})


@route("POST", r"/api/fill/poll")
def handle_fill_poll(handler):
    if not _fill_agent_authorized(handler):
        return
    from job_agent.services.fill_bridge import server_bridge
    try:
        result = server_bridge(handler).poll(_fill_session_id(_fill_payload(handler)))
    except ValueError:
        handler._json({"error": "代填会话无效。"}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(result)


@route("POST", r"/api/fill/close")
def handle_fill_close(handler):
    if not _fill_agent_authorized(handler):
        return
    from job_agent.services.fill_bridge import server_bridge
    try:
        server_bridge(handler).close(_fill_session_id(_fill_payload(handler)))
    except ValueError:
        handler._json({"error": "代填会话无效。"}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True})


@route("GET", r"/api/jobs/(\d+)/fill-data")
def handle_fill_data(handler, job_id_str):
    if not _fill_agent_authorized(handler):
        return
    from job_agent.services.fill_bridge import minimal_fill_data
    try:
        job = handler.repository.get_job(int(job_id_str))
        result = minimal_fill_data(load_profile(handler.profile_path), job.job_id)
        result["job_url"] = _job_source_url(job)
    except (ValueError, JobDatabaseError, ProfileStoreError):
        handler._json({"error": "无法读取岗位或个人档案。"}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(result)


@route("POST", r"/api/sms/clear")
def handle_sms_clear(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    clear_latest_code()
    handler._json({"ok": True})
