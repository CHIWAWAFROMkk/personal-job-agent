from __future__ import annotations

import hashlib
import json
import logging
import re
import os
import shutil
import subprocess
import threading
import webbrowser
from datetime import UTC, date, datetime
from http import HTTPStatus
from http.client import HTTPException
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from job_agent.models.job_record import JobDetail, JobRecordInput
from job_agent.models.profile import Profile
from job_agent.services.application_pack import build_application_pack, write_application_pack, ApplicationPackError, resolve_resume_bundle
from job_agent.services.browser_assist import BrowserAssistError, create_application_session, find_application_pack
from job_agent.services.commute_routing import AmapCommuteProvider, CommuteRoutingError
from job_agent.services.api_usage import (
    ApiQuotaExceededError, ApiUsageUnavailableError, load_api_usage,
    reserve_api_usage, response_token_counts,
)
from job_agent.services.job_repository import JobDatabaseError, JobRepository, normalize_company
from job_agent.services.job_strategy import evaluate_job_strategy
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.match_refresh import ensure_current_match
from job_agent.models.job import MatchResult
from job_agent.services.portable_resume import (
    PortableResumeError, build_portable_resume_content, build_portable_resume_draft,
    find_latest_resume_manifest, find_profile_photo, read_resume_manifest,
    resume_artifact_from_manifest, inherit_resume_fact_review,
    resume_manifest_fact_approved, resume_manifest_fact_review_required,
    resume_manifest_fact_review_sources,
    verified_resume_content_sha256,
)
from job_agent.services.profile_store import ProfileStoreError, load_profile as _load_profile
from job_agent.services.project_workshop import ProjectWorkshopError, project_preview_path, project_workshop_snapshot, run_project, verify_project
from job_agent.services.resume_editor import ResumeEditorError, load_latest_resume_content, rerender_edited_resume, validate_resume_content
from job_agent.services.resume_polish import CloudAIUnavailableError, ResumePolishError, polish_resume_content_locally, polish_resume_content_with_jd
from job_agent.services.resume_compose import build_resume_fact_comparisons, compose_resume_content_with_jd
from job_agent.services.resume_import import ResumeImportError, import_resume_text_into_draft
from job_agent.services.runtime_config import RuntimeConfigError, effective_runtime_config
from job_agent.services.tailored_resume import TailoredResumeError, approve_resume_visual_review
from job_agent.constants import MAX_JSON_BODY as _MAX_JSON_BODY
from job_agent.constants import MAX_RESUME_CONTENT_BODY as _MAX_RESUME_CONTENT_BODY
from job_agent.services.browser_use_agent import (
    BrowserUseError,
    check_cdp_available,
    get_chrome_launch_instructions,
    is_browser_use_available,
    run_browser_use_assist,
    validate_local_cdp_url,
)
from job_agent.services.dashboard_routes import route
from job_agent.services.dashboard_routes.dashboard_api import (
    _profile_summary, _commute_fit, _latest_preparation_path, _safe_slug, _job_workspace, _job_source_url
)

logger = logging.getLogger(__name__)

_JOB_OPERATION_LOCKS_GUARD = threading.Lock()
_JOB_OPERATION_LOCKS: dict[int, threading.RLock] = {}


def _get_job_operation_lock(job_id: int) -> threading.RLock:
    """获取岗位级别的操作协调互斥锁（涵盖生成、重生成、编辑与审批）。

    确保同一岗位在版本检查、审批落盘和材料包同步期间，不会被后台并发的
    准备投递或重新生成操作打断，消除生成与审批之间的竞态窗口。
    """
    with _JOB_OPERATION_LOCKS_GUARD:
        if job_id not in _JOB_OPERATION_LOCKS:
            _JOB_OPERATION_LOCKS[job_id] = threading.RLock()
        return _JOB_OPERATION_LOCKS[job_id]


# 向后兼容别名，全部指向统一的岗位操作锁
_get_job_prepare_lock = _get_job_operation_lock
_get_job_review_lock = _get_job_operation_lock


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def load_profile(path: Path) -> Profile:
    """Keep private paths and validation payloads out of public API errors."""
    if not path.is_file():
        raise ProfileStoreError("请先打开“我的 → 个人资料与简历”，导入简历并确认真实经历，再进行岗位匹配或生成简历。")
    try:
        return _load_profile(path)
    except ProfileStoreError as exc:
        raise ProfileStoreError("个人档案暂时无法读取，请到“我的 → 个人资料与简历”检查档案或重新导入；原文件未被修改。") from exc


def _fact_comparisons_for_manifest(manifest_path: Path, manifest: dict, profile: Profile) -> list[dict[str, str]]:
    if not resume_manifest_fact_review_required(manifest_path, manifest):
        return []
    try:
        verified_resume_content_sha256(manifest_path, manifest)
    except PortableResumeError as exc:
        raise ResumePolishError(str(exc)) from exc
    content_name = manifest.get("content_source")
    if not isinstance(content_name, str) or not content_name or Path(content_name).name != content_name:
        raise ResumePolishError("云端简历缺少可核对的内容文件，请重新生成草稿。")
    try:
        content = json.loads((manifest_path.parent / content_name).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResumePolishError("云端简历内容无法读取，请重新生成草稿。") from exc
    if not isinstance(content, dict):
        raise ResumePolishError("云端简历内容格式无效，请重新生成草稿。")
    inherit_resume_fact_review(
        content, manifest,
        sources=resume_manifest_fact_review_sources(manifest_path, manifest),
    )
    return build_resume_fact_comparisons(content, profile)


def _manifest_fact_approved(manifest: dict, pdf_sha256: str, manifest_path: Path) -> bool:
    expected = str(manifest.get("artifacts", {}).get("pdf", {}).get("sha256", "")).upper()
    return expected == pdf_sha256 and resume_manifest_fact_approved(manifest_path, manifest)


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


def _compose_resume_with_quota(
    handler, content: dict, jd_text: str, *, profile: Profile, config,
    user_instruction: str,
):
    if config.ai.provider == "local":
        raise ResumePolishError("未配置云端模型，当前使用本地事实选材。")
    with reserve_api_usage(
        handler.api_usage_path, "ai", config.ai.provider, config.ai.monthly_quota,
    ) as usage:
        suggestion = compose_resume_content_with_jd(
            content, jd_text, profile=profile, config=config,
            user_instruction=user_instruction,
            request_call=usage.call,
        )
        if usage.active:
            usage.commit(
                input_tokens=suggestion.input_tokens,
                output_tokens=suggestion.output_tokens,
            )
    return suggestion


def _greetings_with_quota(handler, job: JobDetail, profile: Profile, config):
    from job_agent.services.outreach_copilot import generate_greetings

    if config.ai.provider not in {"openai", "deepseek", "openai_compatible"}:
        return generate_greetings(job, profile, config)
    try:
        with reserve_api_usage(
            handler.api_usage_path, "ai", config.ai.provider, config.ai.monthly_quota,
        ) as usage:
            result = generate_greetings(
                job, profile, config,
                on_cloud_response=lambda response: usage.commit(**response_token_counts(response)),
                on_cloud_failure=usage.handle_provider_failure,
            )
            return result
    except (ApiQuotaExceededError, ApiUsageUnavailableError, TimeoutError):
        local = config.model_copy(deep=True)
        local.ai.provider = "local"
        return generate_greetings(job, profile, local)


def _interview_prep_with_quota(handler, job: JobDetail, profile: Profile, config):
    from job_agent.services.interview_copilot import generate_interview_prep

    if config.ai.provider not in {"openai", "deepseek", "openai_compatible"}:
        return generate_interview_prep(job, profile, config)
    try:
        with reserve_api_usage(
            handler.api_usage_path, "ai", config.ai.provider, config.ai.monthly_quota,
        ) as usage:
            result = generate_interview_prep(
                job, profile, config,
                on_cloud_response=lambda response: usage.commit(**response_token_counts(response)),
                on_cloud_failure=usage.handle_provider_failure,
            )
            return result
    except (ApiQuotaExceededError, ApiUsageUnavailableError, TimeoutError):
        local = config.model_copy(deep=True)
        local.ai.provider = "local"
        return generate_interview_prep(job, profile, local)

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
            today=date.today(),
        )
        greetings_list = []
        try:
            resume_bundle = resolve_resume_bundle(load_profile(handler.profile_path), job, handler.output_dir / "applications")
            resume_ref = resume_bundle.pdf_path or resume_bundle.manifest_path
            if resume_ref:
                g_file = Path(resume_ref).parent / "greetings.json"
                if g_file.is_file():
                    cached_g = json.loads(g_file.read_text(encoding="utf-8"))
                    if isinstance(cached_g, list):
                        greetings_list = cached_g
        except Exception:
            pass

        resume_version = None
        manifest_path = find_latest_resume_manifest(handler.output_dir / "applications", job_id)
        if manifest_path is not None and manifest_path.is_file():
            try:
                mdata = read_resume_manifest(manifest_path)
                if mdata is None:
                    raise ValueError("简历清单格式无效")
                pdf_info = mdata.get("artifacts", {}).get("pdf", {})
                pdf_rel = str(pdf_info.get("path", "")).strip()
                pdf_abs = str((manifest_path.parent / pdf_rel).resolve()) if pdf_rel else None
                pdf_sha = str(pdf_info.get("sha256", "")).strip().upper()
                fact_review_required = resume_manifest_fact_review_required(manifest_path, mdata)
                fact_review_approved = (
                    _manifest_fact_approved(mdata, pdf_sha, manifest_path)
                    if fact_review_required else False
                )
                fact_comparisons = []
                fact_review_error = ""
                if fact_review_required and not fact_review_approved:
                    try:
                        fact_comparisons = _fact_comparisons_for_manifest(
                            manifest_path, mdata, load_profile(handler.profile_path),
                        )
                    except (ResumePolishError, ProfileStoreError) as exc:
                        fact_review_error = str(exc)
                resume_version = {
                    "version_id": mdata.get("version_id"),
                    "manifest_path": str(manifest_path.resolve()),
                    "pdf_sha256": pdf_sha,
                    "content_sha256": mdata.get("content_sha256", ""),
                    "pdf_path": pdf_abs,
                    "generated_at": mdata.get("generated_at", ""),
                    "fact_review_required": fact_review_required and not fact_review_approved,
                    "fact_review_approved": fact_review_approved,
                    "fact_comparisons": fact_comparisons,
                    "fact_review_error": fact_review_error,
                }
            except Exception:
                pass

        handler._json({
            "job": {
                "job_id": job.job_id,
                "company": job.company,
                "title": job.title,
                "jd_text": job.jd_text,
            },
            "match_insight": insight,
            "strategy": strategy.model_dump(mode="json"),
            "greetings": greetings_list,
            "resume_version": resume_version,
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
        handler.repository.get_job(job_id)
        content, manifest_path = load_latest_resume_content(
            handler.output_dir / "applications",
            job_id,
        )
    except JobDatabaseError:
        handler._json({"error": "当前用户没有该岗位。"}, HTTPStatus.NOT_FOUND)
        return
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
    try:
        handler.repository.get_job(job_id)
    except JobDatabaseError:
        handler._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
        handler.wfile.write("当前用户没有该岗位。".encode("utf-8"))
        return
    query = parse_qs(urlsplit(handler.path).query)
    requested_sha256 = (query.get("v") or query.get("sha256") or [""])[0].strip().upper()
    apps_dir = handler.output_dir / "applications"

    if requested_sha256:
        candidates = []
        if apps_dir.is_dir():
            for path in apps_dir.rglob("resume-version*.json"):
                payload = read_resume_manifest(path)
                if payload is None:
                    continue
                if payload["target"]["job_id"] != job_id:
                    continue
                art_sha = str(payload["artifacts"][kind].get("sha256", "")).strip().upper()
                if art_sha == requested_sha256:
                    try:
                        candidates.append((path.stat().st_mtime, path.resolve()))
                    except OSError:
                        continue
        if not candidates:
            handler._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
            handler.wfile.write(f"未找到哈希为 {requested_sha256} 的简历草稿。".encode("utf-8"))
            return
        manifest = max(candidates, key=lambda item: (item[0], str(item[1])))[1]
    else:
        manifest = find_latest_resume_manifest(
            apps_dir,
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
    actual_sha256 = hashlib.sha256(body).hexdigest().upper()
    if requested_sha256 and actual_sha256 != requested_sha256:
        handler._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
        handler.wfile.write(f"未找到哈希为 {requested_sha256} 的简历草稿（实际文件已被更新为 {actual_sha256}）。".encode("utf-8"))
        return
    handler._headers(
        HTTPStatus.OK,
        content_type,
        {"Content-Disposition": f'{disposition}; filename="job-{job_id}-resume.{kind}"'},
    )
    handler.wfile.write(body)

@route("GET", r"/project-workshop/(\d+)/preview")
def handle_project_preview(handler, job_id_str):
    try:
        handler.repository.get_job(int(job_id_str))
        preview = project_preview_path(
            handler.output_dir,
            int(job_id_str),
        )
    except (JobDatabaseError, ProjectWorkshopError) as exc:
        handler._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
        handler.wfile.write(str(exc).encode("utf-8"))
        return
    body = preview.read_bytes()
    handler._headers(HTTPStatus.OK, "text/html; charset=utf-8")
    handler.wfile.write(body)

@route("GET", r"/preparation/(\d+)")
def handle_preparation(handler, job_id_str):
    try:
        handler.repository.get_job(int(job_id_str))
    except JobDatabaseError:
        handler._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
        handler.wfile.write("当前用户没有该岗位。".encode("utf-8"))
        return
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
        payload = json.loads(handler._read_body(_MAX_RESUME_CONTENT_BODY).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            raise ValueError("请先确认本次润色。")
        job = handler.repository.get_job(job_id)
        content, _ = load_latest_resume_content(
            handler.output_dir / "applications",
            job_id,
        )
        if "content" in payload:
            proposed_content = validate_resume_content(payload["content"])
            inherit_resume_fact_review(proposed_content, content)
            content = proposed_content
            if int(content["target"]["job_id"]) != job_id:
                raise ValueError("简历与当前岗位不一致。")
        config, _ = effective_runtime_config(handler.runtime_config_path)
        warning = ""
        if config.ai.provider == "local":
            suggestion = polish_resume_content_locally(content, job.jd_text)
            polish_engine = "local"
        else:
            try:
                with reserve_api_usage(
                    handler.api_usage_path, "ai", config.ai.provider,
                    config.ai.monthly_quota,
                ) as usage:
                    suggestion = polish_resume_content_with_jd(
                        content, job.jd_text, config=config,
                        request_call=usage.call,
                    )
                    if usage.active:
                        usage.commit(
                            input_tokens=suggestion.input_tokens,
                            output_tokens=suggestion.output_tokens,
                        )
            except CloudAIUnavailableError as exc:
                raise ResumePolishError(f"{exc} 请修复连接后重试；原稿未修改。") from exc
            else:
                polish_engine = "cloud"
                # A wording suggestion can change the meaning even if every
                # digit survives. Keep the saved result behind the same
                # separate fact review as whole-resume cloud composition.
                suggestion.content["generation"] = {
                    "engine": "cloud_composition",
                    "method": "wording_polish",
                    "review_required": True,
                }
                inherit_resume_fact_review(
                    suggestion.content, content, source="cloud_wording_polish",
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
        with reserve_api_usage(
            handler.api_usage_path, "ai", config.ai.provider,
            config.ai.monthly_quota,
        ) as usage:
            suggestion = import_resume_text_into_draft(
                payload["resume_text"], content, config=config,
                request_call=usage.call,
            )
            if usage.active:
                usage.commit(
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
        person["photo_path"] = str(photo_path.resolve()) if photo_path else ""
        content["person"] = person
        try:
            suggestion = _compose_resume_with_quota(
                handler,
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
        payload = json.loads(handler._read_body(_MAX_RESUME_CONTENT_BODY).decode("utf-8"))
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
        with _get_job_operation_lock(job_id):
            job = handler.repository.get_job(job_id)
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
            photo_path = find_profile_photo(handler.private_dir)
            content = build_portable_resume_content(
                profile,
                job,
                result,
                generated_at=datetime.now(UTC),
                photo_path=photo_path,
            )
            config, _ = effective_runtime_config(handler.runtime_config_path)
            try:
                suggestion = _compose_resume_with_quota(
                    handler,
                    content,
                    job.jd_text,
                    profile=profile,
                    config=config,
                    user_instruction="按 JD 直接生成岗位专属简历，使用 STAR 法则突出相关真实经历。",
                )
            except (ResumePolishError, ApiQuotaExceededError, ApiUsageUnavailableError) as exc:
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
        with _get_job_operation_lock(job_id):
            job = handler.repository.get_job(job_id)
            result = _ensure_match(handler.repository, profile, job)
            latest_manifest = find_latest_resume_manifest(
                handler.output_dir / "applications", job_id,
            )
            if latest_manifest is None:
                raise ValueError("没有找到等待本人审阅的岗位专属简历草稿。")
            manifest = read_resume_manifest(latest_manifest)
            if manifest is None:
                raise ValueError("简历清单损坏，请重新生成草稿。")
            pdf_sha = str(manifest["artifacts"]["pdf"].get("sha256") or "").upper()
            qa = manifest["qa"]
            already_approved = bool(
                qa.get("pdf_visual_review") == "passed"
                and qa.get("truthfulness_check") == "passed"
                and _manifest_fact_approved(manifest, pdf_sha, latest_manifest)
            )
            if not already_approved:
                comparisons = _fact_comparisons_for_manifest(latest_manifest, manifest, profile)
                if comparisons and (
                    payload.get("confirmed_fact_review") is not True
                    or str(payload.get("fact_review_sha256") or "").upper()
                    != pdf_sha
                    or str(payload.get("fact_review_content_sha256") or "").upper()
                    != str(manifest.get("content_sha256") or "").upper()
                ):
                    raise ValueError("请先逐项核对云端改写与原事实，并单独确认当前 PDF 版本。")
                if comparisons:
                    approve_resume_visual_review(latest_manifest, fact_review_confirmed=True)
                else:
                    approve_resume_visual_review(latest_manifest)
            content_source = str(manifest.get("content_source") or "")
            if not content_source or Path(content_source).name != content_source:
                raise ValueError("简历内容文件缺失，请重新生成草稿。")
            resume = resolve_resume_bundle(
                profile, job, handler.output_dir / "applications",
                content_path=latest_manifest.parent / content_source,
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
        JobDatabaseError, ApplicationPackError, TailoredResumeError,
        ResumePolishError, OSError,
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

@route("POST", r"/api/jobs/(\d+)/prepare-apply")
def handle_prepare_apply(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    STAGE_NAMES = {
        "profile_check": "个人真实经历核查",
        "match_analysis": "岗位匹配与策略分析",
        "resume_draft": "专属简历草稿生成",
        "greetings": "打招呼语生成",
        "application_pack": "投递材料包组装",
    }
    current_stage = "profile_check"
    try:
        content_len = handler.headers.get("Content-Length")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8")) if content_len and int(content_len) > 0 else {}
        if not isinstance(payload, dict):
            payload = {}
        force_regenerate = bool(payload.get("force_regenerate", False))

        job_id = int(job_id_str)
        with _get_job_operation_lock(job_id):
            job = handler.repository.get_job(job_id)
            profile = load_profile(handler.profile_path)
            confirmed_facts = sum(
                profile.is_application_ready(fact.status)
                for experience in profile.experiences
                for fact in experience.facts
            )
            if not confirmed_facts:
                raise ValueError("请先在个人资料中录入并确认真实经历，再准备投递材料。")

            # 1. 匹配分析 (复用本地指纹缓存)
            current_stage = "match_analysis"
            result = _ensure_match(handler.repository, profile, job)

            # 2. 简历草稿 (已有结果复用，重复点击不重复调用 AI)
            current_stage = "resume_draft"
            resume = None
            resume_reused = False
            apps_dir = handler.output_dir / "applications"
            if not force_regenerate:
                latest_m = find_latest_resume_manifest(apps_dir, job_id)
                if latest_m is not None and latest_m.is_file():
                    try:
                        m_data = json.loads(latest_m.read_text(encoding="utf-8"))
                        csource = str(m_data.get("content_source") or "resume-content-portable.json").strip()
                        cpath = latest_m.parent / csource
                        if cpath.is_file():
                            existing_bundle = resolve_resume_bundle(profile, job, apps_dir, content_path=cpath)
                            if (
                                existing_bundle.status in {"ready", "needs_review"}
                                and existing_bundle.pdf_path
                                and Path(existing_bundle.pdf_path).is_file()
                            ):
                                resume = existing_bundle
                                resume_reused = True
                    except (ApplicationPackError, OSError, json.JSONDecodeError):
                        resume = None
                if resume is None:
                    try:
                        existing_bundle = resolve_resume_bundle(profile, job, apps_dir)
                        if (
                            existing_bundle.status in {"ready", "needs_review"}
                            and existing_bundle.pdf_path
                            and Path(existing_bundle.pdf_path).is_file()
                        ):
                            resume = existing_bundle
                            resume_reused = True
                    except (ApplicationPackError, OSError):
                        resume = None

            if resume is None:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                draft_dir = (
                    apps_dir
                    / (
                        f"portable-job-{job_id}-"
                        f"{_safe_slug(job.company, fallback='company')}-"
                        f"{_safe_slug(job.title, fallback='role')}-{stamp}"
                    )
                )
                photo_path = find_profile_photo(handler.private_dir)
                content = build_portable_resume_content(
                    profile,
                    job,
                    result,
                    generated_at=datetime.now(UTC),
                    photo_path=photo_path,
                )
                config, _ = effective_runtime_config(handler.runtime_config_path)
                try:
                    suggestion = _compose_resume_with_quota(
                        handler,
                        content,
                        job.jd_text,
                        profile=profile,
                        config=config,
                        user_instruction="按 JD 直接生成岗位专属简历，使用 STAR 法则突出相关真实经历。",
                    )
                except (ResumePolishError, ApiQuotaExceededError, ApiUsageUnavailableError) as exc:
                    suggestion = polish_resume_content_locally(
                        content,
                        job.jd_text,
                        user_instruction="按 JD 生成岗位专属简历并按 STAR 组织真实经历。",
                    )
                    warning_msg = f"云端 AI 暂时不可用（{exc}），系统已自动启用本地高精度算法生成专属简历。"
                    suggestion.content["generation"] = {
                        "engine": "local_fallback",
                        "warning": warning_msg,
                        "review_required": True,
                    }
                build_portable_resume_draft(
                    profile,
                    job,
                    result,
                    draft_dir,
                    photo_path=photo_path,
                    content=suggestion.content,
                )
                draft_content_path = draft_dir / "resume-content-portable.json"
                try:
                    resume = resolve_resume_bundle(profile, job, apps_dir, content_path=draft_content_path)
                except (ApplicationPackError, OSError):
                    resume = resolve_resume_bundle(profile, job, apps_dir)
                resume_reused = False

            # 3. 打招呼语 (已有结果复用，若无则生成并缓存)
            current_stage = "greetings"
            greetings_list = []
            greetings_cache_file = None
            resume_ref = resume.pdf_path or resume.manifest_path or resume.content_path if resume else None
            if resume_ref:
                greetings_cache_file = Path(resume_ref).parent / "greetings.json"
                if not force_regenerate and greetings_cache_file.is_file():
                    try:
                        cached_data = json.loads(greetings_cache_file.read_text(encoding="utf-8"))
                        if isinstance(cached_data, list) and cached_data:
                            greetings_list = cached_data
                    except (json.JSONDecodeError, OSError):
                        pass

            if not greetings_list:
                config, _ = effective_runtime_config(handler.runtime_config_path)
                try:
                    greetings_res = _greetings_with_quota(handler, job, profile, config)
                    greetings_list = [
                        {"style": g.style, "title": g.title, "content": g.content}
                        for g in greetings_res.greetings
                    ]
                except Exception:
                    greetings_list = [
                        {
                            "style": "tech_match",
                            "title": "技能匹配",
                            "content": f"您好！关注到贵司【{job.company}】的【{job.title}】岗位，我的技能与经历较为匹配，希望与您进一步交流。",
                        }
                    ]
                if greetings_cache_file:
                    try:
                        greetings_cache_file.write_text(
                            json.dumps(greetings_list, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    except OSError:
                        pass

            # 4. 材料整理 (组装投递材料包，保持 needs_review 等待用户审阅)
            current_stage = "application_pack"
            resume = resolve_resume_bundle(profile, job, apps_dir)
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
                    source="dashboard_prepare_apply",
                    detail="投递材料已一键准备就绪（专属简历与打招呼语就绪），待本人审阅后提交。",
                    resume_path=resume.pdf_path,
                    application_pack_path=str(pack_files.pack_json),
                    source_url=_job_source_url(job),
                    verification_method="user_prepare_apply",
                )

            workspace = _job_workspace(profile, job, output_dir=handler.output_dir)
            summary = _profile_summary(handler.profile_path)
            commute_status = _commute_fit(
                job.commute_minutes,
                summary.max_one_way_minutes if summary else None,
            )
            commute_warning = "办公地点或路线未确认，通勤仍需核实。" if commute_status == "unknown" else None

            # 获取版本签名
            manifest_path = find_latest_resume_manifest(apps_dir, job_id)
            version_id = None
            pdf_sha256 = None
            manifest_pdf = None
            if manifest_path is not None and manifest_path.is_file():
                try:
                    mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
                    version_id = mdata.get("version_id")
                    pdf_info = mdata.get("artifacts", {}).get("pdf", {})
                    pdf_sha256 = str(pdf_info.get("sha256", "")).strip().upper()
                    pdf_rel = str(pdf_info.get("path", "")).strip()
                    if pdf_rel:
                        cand_pdf = (manifest_path.parent / pdf_rel).resolve()
                        if cand_pdf.is_file():
                            manifest_pdf = cand_pdf
                except Exception:
                    pass
            if not pdf_sha256 and resume and resume.pdf_path and Path(resume.pdf_path).is_file():
                pdf_sha256 = _sha256(Path(resume.pdf_path))

            final_pdf_path = None
            if manifest_pdf:
                final_pdf_path = str(manifest_pdf)
            elif resume and resume.pdf_path and Path(resume.pdf_path).is_file():
                final_pdf_path = str(Path(resume.pdf_path).resolve())

            resume_version = {
                "version_id": version_id,
                "manifest_path": str(manifest_path.resolve()) if manifest_path else None,
                "pdf_sha256": pdf_sha256,
                "pdf_path": final_pdf_path,
            }
    except (
        UnicodeDecodeError, json.JSONDecodeError, ValueError, ProfileStoreError,
        JobDatabaseError, ApplicationPackError, TailoredResumeError, PortableResumeError,
        ResumePolishError, RuntimeConfigError, OSError,
    ) as exc:
        handler._json(
            {
                "error": str(exc),
                "failed_stage": current_stage,
                "failed_stage_name": STAGE_NAMES.get(current_stage, current_stage),
            },
            HTTPStatus.BAD_REQUEST,
        )
        return

    handler._json({
        "ok": True,
        "job_id": job_id,
        "match_score": result.overall_score,
        "resume_reused": resume_reused,
        "commute_fit": commute_status,
        "commute_warning": commute_warning,
        "greetings": greetings_list,
        "workspace": workspace.model_dump(mode="json"),
        "resume_version": resume_version,
        "application_pack_ready": True,
    })

@route("POST", r"/api/jobs/(\d+)/review-and-open")
def handle_review_and_open(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        content_len = handler.headers.get("Content-Length")
        payload = (
            json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
            if content_len and int(content_len) > 0
            else {}
        )
        if not isinstance(payload, dict):
            payload = {}

        # 1. 禁止空哈希绕过校验：必须提供有效 64 位 HEX SHA256
        expected_sha256 = str(payload.get("expected_sha256", "")).strip().upper()
        if not expected_sha256 or not re.fullmatch(r"^[0-9A-F]{64}$", expected_sha256):
            handler._json(
                {"error": "必须提供有效的 64 位 SHA256 简历版本哈希，请先在审阅面板核对最新简历。"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        # 2. 必须提供清单路径
        manifest_path_req = payload.get("manifest_path")
        if not manifest_path_req or not isinstance(manifest_path_req, str) or not manifest_path_req.strip():
            handler._json(
                {"error": "必须提供审阅核对的简历质检清单路径 (manifest_path)。"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        expected_version_id = str(payload.get("expected_version_id", "")).strip()

        job_id = int(job_id_str)
        with _get_job_operation_lock(job_id):
            job = handler.repository.get_job(job_id)
            profile = load_profile(handler.profile_path)
            apps_dir = (handler.output_dir / "applications").resolve()

            # 校验清单文件存在且位于 applications 目录内
            manifest_file = Path(manifest_path_req).resolve()
            if not manifest_file.is_file():
                handler._json(
                    {"error": f"简历质检清单文件不存在: {manifest_file.name}"},
                    HTTPStatus.BAD_REQUEST,
                )
                return

            try:
                if not manifest_file.is_relative_to(apps_dir):
                    handler._json(
                        {"error": "非法的简历清单路径，必须位于 applications 材料目录内。"},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
            except ValueError:
                handler._json(
                    {"error": "非法的简历清单路径。"},
                    HTTPStatus.BAD_REQUEST,
                )
                return

            # 解析清单文件并校验所属岗位
            mdata = read_resume_manifest(manifest_file)
            if mdata is None:
                handler._json(
                    {"error": "简历质检清单损坏或缺少必要字段，请重新生成草稿。"},
                    HTTPStatus.BAD_REQUEST,
                )
                return

            target = mdata.get("target", {})
            target_job_id = target["job_id"]
            if target_job_id and target_job_id != job_id:
                handler._json(
                    {"error": f"简历清单属于岗位 #{target_job_id}，与当前岗位 #{job_id} 不匹配。"},
                    HTTPStatus.BAD_REQUEST,
                )
                return

            target_company = str(target.get("company", ""))
            if target_company and normalize_company(target_company) != normalize_company(job.company):
                handler._json(
                    {"error": f"简历清单所指公司【{target_company}】与当前岗位【{job.company}】不一致。"},
                    HTTPStatus.BAD_REQUEST,
                )
                return

            # 校验清单所引用的 PDF 及记录哈希
            pdf_info = mdata.get("artifacts", {}).get("pdf", {})
            pdf_rel = str(pdf_info.get("path", "")).strip()
            manifest_recorded_sha256 = str(pdf_info.get("sha256", "")).strip().upper()
            if not pdf_rel:
                handler._json(
                    {"error": "简历清单未记录 PDF 文件信息。"},
                    HTTPStatus.BAD_REQUEST,
                )
                return

            manifest_pdf_path = (manifest_file.parent / pdf_rel).resolve()
            if not manifest_pdf_path.is_file():
                handler._json(
                    {"error": "简历清单指向的 PDF 文件在磁盘上不存在。"},
                    HTTPStatus.BAD_REQUEST,
                )
                return

            if manifest_recorded_sha256 != expected_sha256:
                handler._json(
                    {
                        "error": "简历清单记录的 PDF 哈希与待确认版本不一致，可能已被重新生成。",
                        "expected_sha256": expected_sha256,
                        "manifest_sha256": manifest_recorded_sha256,
                    },
                    HTTPStatus.CONFLICT,
                )
                return

            # 校验磁盘上该 PDF 的实际哈希
            pdf_disk_sha256 = _sha256(manifest_pdf_path)
            if pdf_disk_sha256 != expected_sha256:
                handler._json(
                    {
                        "error": "磁盘上的 PDF 文件内容已变更或损坏，哈希不匹配。",
                        "expected_sha256": expected_sha256,
                        "actual_sha256": pdf_disk_sha256,
                    },
                    HTTPStatus.CONFLICT,
                )
                return

            # 检查当前工作区是否已生成更新的草稿目录（防后台并发重新生成导致混用）
            latest_manifest = find_latest_resume_manifest(apps_dir, job_id)
            if (
                latest_manifest is not None
                and latest_manifest.parent != manifest_file.parent
                and latest_manifest.stat().st_mtime > manifest_file.stat().st_mtime
            ):
                handler._json(
                    {
                        "error": "已有更新的简历草稿生成，请重新打开审阅面板核对最新版本。",
                        "expected_sha256": expected_sha256,
                    },
                    HTTPStatus.CONFLICT,
                )
                return

            # 定位该 manifest 对应的简历内容文件
            content_source = str(mdata.get("content_source") or "resume-content-portable.json").strip()
            manifest_content_path = (manifest_file.parent / content_source).resolve()
            if not manifest_content_path.is_file():
                content_candidates = list(manifest_file.parent.glob("resume-content*.json"))
                if content_candidates:
                    manifest_content_path = content_candidates[0].resolve()

            # 校验当前工作区最新解析的简历是否一致
            try:
                current_resume = resolve_resume_bundle(profile, job, apps_dir, content_path=manifest_content_path)
            except (ApplicationPackError, OSError):
                current_resume = None

            if current_resume is None or not current_resume.pdf_path or not Path(current_resume.pdf_path).is_file():
                raise ValueError("未找到已生成的简历草稿，请先点击【准备投递材料】。")

            current_workspace_pdf_path = Path(current_resume.pdf_path).resolve()
            current_workspace_sha256 = _sha256(current_workspace_pdf_path)
            if current_workspace_sha256 != expected_sha256 or current_workspace_pdf_path != manifest_pdf_path:
                handler._json(
                    {
                        "error": "简历版本已更新或哈希不匹配（当前文件已被重新生成或修改），请重新打开审阅面板核对最新版本。",
                        "expected_sha256": expected_sha256,
                        "actual_sha256": current_workspace_sha256,
                    },
                    HTTPStatus.CONFLICT,
                )
                return

            actual_version_id = str(mdata.get("version_id", "")).strip() or None
            if expected_version_id and actual_version_id and actual_version_id != expected_version_id:
                handler._json(
                    {
                        "error": "简历版本编号不匹配（当前文件已被替换），请重新打开审阅面板核对最新版本。",
                        "expected_version_id": expected_version_id,
                        "actual_version_id": actual_version_id,
                    },
                    HTTPStatus.CONFLICT,
                )
                return

            # 3. 视觉审核确认：禁止吞咽异常，若失败立即报错并终止
            # 在互斥锁内检查是否已有本版本的批准文件（同版本并发确认幂等保护，仅产生 1 份批准记录）
            fact_review_required = resume_manifest_fact_review_required(manifest_file, mdata)
            already_approved = False
            for app_file in manifest_file.parent.glob("resume-version-approved-*.json"):
                app_data = read_resume_manifest(app_file)
                if app_data is None:
                    continue
                if app_data.get("qa", {}).get("pdf_visual_review") == "passed":
                    app_pdf_sha = str(app_data.get("artifacts", {}).get("pdf", {}).get("sha256", "")).upper()
                    if app_pdf_sha and app_pdf_sha == expected_sha256 and _manifest_fact_approved(app_data, expected_sha256, app_file):
                        already_approved = True
                        break

            qa = mdata.get("qa", {})
            qa_status = qa.get("pdf_visual_review")
            if qa_status == "passed" and _manifest_fact_approved(mdata, expected_sha256, manifest_file):
                already_approved = True
            if not already_approved:
                if qa_status not in {"pending_user_review", "passed"} or (qa_status == "passed" and not fact_review_required):
                    raise ValueError(f"该简历质检状态为「{qa_status}」，不满足人工确认条件。")
                if fact_review_required:
                    comparisons = _fact_comparisons_for_manifest(manifest_file, mdata, profile)
                    if (
                        not comparisons
                        or payload.get("confirmed_fact_review") is not True
                        or str(payload.get("fact_review_sha256") or "").upper() != expected_sha256
                        or str(payload.get("fact_review_content_sha256") or "").upper()
                        != str(mdata.get("content_sha256") or "").upper()
                    ):
                        raise ValueError("请先逐项核对云端改写与原事实，并单独确认当前 PDF 版本。")
                if fact_review_required:
                    approve_resume_visual_review(manifest_file, fact_review_confirmed=True)
                else:
                    approve_resume_visual_review(manifest_file)

            # 同步材料包：禁止吞咽异常，若失败立即报错并终止
            try:
                resume = resolve_resume_bundle(profile, job, apps_dir, content_path=manifest_content_path)
                result = _ensure_match(handler.repository, profile, job)
                pack = build_application_pack(profile, job, result, resume)
                write_application_pack(
                    pack,
                    job,
                    result,
                    _application_pack_output_dir(handler.output_dir, job),
                )
            except (ApplicationPackError, OSError) as exc:
                # 严格核验磁盘上是否确实存在该版本的已批准记录
                is_approved_on_disk = False
                for app_file in manifest_file.parent.glob("resume-version-approved-*.json"):
                    try:
                        app_data = json.loads(app_file.read_text(encoding="utf-8"))
                        if app_data.get("qa", {}).get("pdf_visual_review") == "passed":
                            app_pdf_sha = str(app_data.get("artifacts", {}).get("pdf", {}).get("sha256", "")).upper()
                            if app_pdf_sha and app_pdf_sha == expected_sha256 and _manifest_fact_approved(app_data, expected_sha256, app_file):
                                is_approved_on_disk = True
                                break
                    except (OSError, json.JSONDecodeError):
                        continue

                # 区分错误类型，不无条件承诺可立即重试
                if isinstance(exc, PermissionError):
                    retry_hint = "请解除文件或目录占用、检查写入权限后重试恢复未完成步骤。"
                    retryable = False
                elif isinstance(exc, ApplicationPackError):
                    retry_hint = "可重试恢复未完成步骤。"
                    retryable = True
                elif isinstance(exc, OSError):
                    retry_hint = "请检查磁盘空间及文件系统状态后重试恢复未完成步骤。"
                    retryable = False
                else:
                    retry_hint = "请排查异常原因后重试恢复未完成步骤。"
                    retryable = False

                if is_approved_on_disk:
                    err_msg = f"简历版面已通过人工审阅并落盘，但投递材料包同步失败: {exc}。{retry_hint}"
                else:
                    err_msg = f"投递材料包同步失败: {exc}。{retry_hint}"

                handler._json(
                    {
                        "error": err_msg,
                        "failed_stage": "application_pack_sync",
                        "visual_review_approved": is_approved_on_disk,
                        "retryable": retryable,
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return

            # 4. 全部确认成功后，才调起外部副作用
            pdf_opened = False
            pdf_error = None
            if hasattr(os, "startfile"):
                try:
                    os.startfile(str(manifest_pdf_path))
                    pdf_opened = True
                except OSError as exc:
                    pdf_error = str(exc)

            source_url = _job_source_url(job)
            page_opened = False
            page_error = None
            if source_url:
                try:
                    _open_job_page(source_url)
                    page_opened = True
                except Exception as exc:
                    page_opened = False
                    page_error = str(exc)
            else:
                page_error = "岗位缺少有效网页链接"

            revealed = _reveal_resume_in_explorer(manifest_pdf_path)
            reveal_error = None if revealed else "未能调起系统资源管理器或定位文件"

            top_greeting = ""
            greetings_file = manifest_pdf_path.parent / "greetings.json"
            if greetings_file.is_file():
                try:
                    cached = json.loads(greetings_file.read_text(encoding="utf-8"))
                    if isinstance(cached, list) and cached:
                        top_greeting = cached[0].get("content", "")
                except Exception:
                    pass

    except (ValueError, OSError, JobDatabaseError, ProfileStoreError, TailoredResumeError, ResumePolishError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    except Exception as exc:
        handler._json({"error": f"审阅确认执行失败: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return

    handler._json({
        "ok": True,
        "job_id": job_id,
        "pdf_opened": pdf_opened,
        "pdf_error": pdf_error,
        "pdf_url": f"/resume-draft/{job_id}/pdf?v={expected_sha256}",
        "sha256": expected_sha256,
        "version_id": actual_version_id,
        "resumed_from_partial": already_approved,
        "page_opened": page_opened,
        "page_error": page_error,
        "source_url": source_url,
        "revealed": revealed,
        "reveal_error": reveal_error,
        "top_greeting": top_greeting,
    })

@route("POST", r"/api/jobs/(\d+)/reveal-resume")
def handle_reveal_resume(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    job_id = int(job_id_str)
    apps_dir = (handler.output_dir / "applications").resolve()
    content_len = handler.headers.get("Content-Length")
    payload = (
        json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if content_len and int(content_len) > 0
        else {}
    )
    if not isinstance(payload, dict):
        payload = {}
    manifest_path_req = payload.get("manifest_path")
    manifest = None
    if manifest_path_req and isinstance(manifest_path_req, str):
        candidate_file = Path(manifest_path_req).resolve()
        if candidate_file.is_file() and candidate_file.is_relative_to(apps_dir):
            manifest = candidate_file
    if manifest is None:
        manifest = find_latest_resume_manifest(apps_dir, job_id)

    if manifest is None:
        handler._json({"error": "尚未生成该岗位的简历草稿。"}, HTTPStatus.NOT_FOUND)
        return
    try:
        pdf_path = resume_artifact_from_manifest(manifest, "pdf")
    except PortableResumeError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.CONFLICT)
        return

    revealed = _reveal_resume_in_explorer(pdf_path)
    handler._json({
        "ok": True,
        "job_id": job_id,
        "revealed": revealed,
        "reveal_error": None if revealed else "未能调起系统资源管理器或定位文件",
    })

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
            while len(handler.assist_runs) > 100:
                handler.assist_runs.pop(next(iter(handler.assist_runs)))
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
        # The route uses two geocoding calls and one directions call. Reserve
        # all three before starting, then count only calls that returned.
        with reserve_api_usage(
            handler.api_usage_path, "maps", "amap",
            config.maps.monthly_quota, units=3,
        ) as usage:
            try:
                result = provider.calculate(
                    origin_address=origin,
                    destination_address=destination,
                    mode=mode,  # type: ignore[arg-type]
                    origin_city=origin_city,
                    destination_city=job.location or origin_city,
                )
            finally:
                usage.commit(
                    successful_requests=provider.request_count,
                    uncertain_requests=max(0, provider.attempted_count - provider.request_count),
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
        if company in {"未知公司", "未命名岗位"} or title in {"未知公司", "未命名岗位"}:
            raise ValueError("请核对并填写真实公司名称和岗位名称，不能保存识别用的占位文字。")
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
        profile = load_profile(handler.profile_path)
        outcome = handler.repository.upsert_job(record)
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
                with reserve_api_usage(
                    handler.api_usage_path, "ai", config.ai.provider,
                    config.ai.monthly_quota,
                ) as usage:
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
                        try:
                            chat_res = client.chat.completions.create(
                                model=model,
                                messages=[
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": raw_text[:8000]},
                                ],
                                temperature=0.1,
                            )
                        except Exception as exc:
                            usage.handle_provider_failure(exc)
                            raise
                        usage.commit(**response_token_counts(chat_res))
                        content = (chat_res.choices[0].message.content or "").strip()
                        m = re.search(r"\{.*\}", content, re.DOTALL)
                        if m:
                            candidate = json.loads(m.group(0))
                            if isinstance(candidate, dict) and candidate.get("title") and candidate.get("company"):
                                extracted = candidate
            except Exception:
                logger.warning("AI 结构化提取失败，回退到本地规则")
                extracted = None
                
        if not extracted or not isinstance(extracted, dict) or not extracted.get("title") or not extracted.get("company"):
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            structured = structure_job_locally(raw_text)
            company_candidate = structured.company if structured.company != "未识别" else "未知公司"
            title_candidate = structured.title if structured.title != "未识别" else "未命名岗位"
            for line in lines[:5]:
                if company_candidate == "未知公司" and len(line) <= 40 and not re.search(r"[:：；;。]", line) and re.search(r"(?:公司|集团|科技|网络|企业)$", line):
                    company_candidate = line
                    break
            for line in lines[:5]:
                if title_candidate == "未命名岗位" and len(line) <= 40 and not re.search(r"[:：；;。]", line) and re.search(r"(?:实习生|工程师|开发|运营|助理|专家|经理|专员|岗)$", line):
                    title_candidate = line
                    break
            extracted = {
                "company": company_candidate,
                "title": title_candidate,
                "location": structured.location or "",
                "salary": "",
                "jd_text": raw_text,
                "skills": list(dict.fromkeys(structured.required_skills + structured.preferred_skills)),
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
        
        result = _greetings_with_quota(handler, job, profile, config)
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

@route("POST", r"/api/jobs/(\d+)/interview-prep")
def handle_job_interview_prep(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        profile = load_profile(handler.profile_path)
        config, _ = effective_runtime_config(handler.runtime_config_path)
        
        prep = _interview_prep_with_quota(handler, job, profile, config)
    except (ValueError, JobDatabaseError, ProfileStoreError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
        
    handler._json({"ok": True, "data": prep.to_dict()})


@route("GET", r"/api/jobs/browser-use/status")
def handle_browser_use_status(handler):
    handler._json({
        "ok": True,
        "browser_use_available": False,
        "live_available": False,
        "reason": "实站 AI 代填已暂停；第三方网页可能在输入或上传时自行提交。",
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
        if not dry_run:
            raise BrowserUseError(
                "实站 AI 代填已暂停：第三方网页可能在输入或上传时自行提交。"
                "请使用模拟预览并由本人在招聘网站完成投递。"
            )
        cdp_url = validate_local_cdp_url(
            str(payload.get("cdp_url", "http://localhost:9222")).strip() or "http://localhost:9222"
        )
        headless = bool(payload.get("headless", False))

        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        profile = load_profile(handler.profile_path)
        config, _ = effective_runtime_config(handler.runtime_config_path)
        result = run_browser_use_assist(
            job=job,
            profile=profile,
            resume_path=None,
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
        "usage_notice": "",
    })


def _verified_browser_use_resume(output_dir: Path, job_id: int) -> Path:
    """Allow only the latest user-approved, hash-checked PDF for this job."""
    applications_dir = (output_dir / "applications").resolve()
    manifest_path = find_latest_resume_manifest(applications_dir, job_id)
    if manifest_path is None or not manifest_path.resolve().is_relative_to(applications_dir):
        raise BrowserUseError("请先为当前岗位生成并审核定向简历 PDF。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Invalid manifest")
        target = manifest.get("target")
        qa = manifest.get("qa")
        if (
            not isinstance(target, dict)
            or type(target.get("job_id")) is not int
            or target["job_id"] != job_id
            or not isinstance(qa, dict)
            or qa.get("pdf_visual_review") != "passed"
            or qa.get("truthfulness_check") != "passed"
        ):
            raise ValueError("Resume still needs review")
        if resume_manifest_fact_review_required(manifest_path, manifest):
            expected_pdf_hash = str(manifest.get("artifacts", {}).get("pdf", {}).get("sha256", ""))
            if not _manifest_fact_approved(manifest, expected_pdf_hash, manifest_path):
                raise ValueError("Cloud-written claims need fact approval")
        pdf_path = resume_artifact_from_manifest(manifest_path, "pdf")
        if pdf_path.suffix.casefold() != ".pdf" or not pdf_path.is_relative_to(applications_dir):
            raise ValueError("Unsafe PDF path")
    except (OSError, ValueError, PortableResumeError, TypeError, AttributeError) as exc:
        raise BrowserUseError("当前岗位的最新简历尚未通过事实与版面审核，或文件已变化；请重新检查 PDF。") from exc
    return pdf_path


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
