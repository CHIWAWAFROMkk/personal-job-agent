from __future__ import annotations
import json
from http import HTTPStatus

from job_agent.services.dashboard_routes import route
from job_agent.services.profile_preferences import (
    ProfilePreferencesError,
    ProfilePreferencesUpdate,
    update_profile_preferences,
)
from job_agent.services.profile_onboarding import (
    ProfileOnboardingError,
    ProfileOnboardingInput,
    onboard_profile,
)
from job_agent.services.profile_store import ProfileStoreError
from job_agent.services.dashboard import _split_values, _optional_int
from job_agent.services.resume_reader import ResumeReadError
from job_agent.services.job_repository import JobDatabaseError
from job_agent.services.portable_resume import (
    save_profile_photo,
    PortableResumeError,
)
from job_agent.services.dashboard import _parse_multipart
from job_agent.services.dashboard_routes.dashboard_api import _profile_summary
from job_agent.constants import MAX_UPLOAD_BODY as _MAX_UPLOAD_BODY, MAX_JSON_BODY as _MAX_JSON_BODY

@route("POST", r"/api/profile/photo")
def handle_post_profile_photo(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        content_type = handler.headers.get("Content-Type", "").casefold()
        if not content_type.startswith("image/"):
            raise ValueError("照片上传必须使用 image/* 请求体。")
        data = handler._read_body(_MAX_UPLOAD_BODY)
        photo_path = save_profile_photo(handler.private_dir, data)
    except (ValueError, PortableResumeError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    except OSError as exc:
        handler._json({"error": f"照片保存失败: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return
    handler._json(
        {
            "ok": True,
            "photo_path": str(photo_path),
            "note": "照片已保存；下次生成或编辑简历时会嵌入新版面。",
        }
    )

@route("POST", r"/api/profile/preferences")
def handle_post_profile_preferences(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith(
            "application/json"
        ):
            raise ValueError("求职偏好必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("求职偏好内容格式不正确。")
        update = ProfilePreferencesUpdate.model_validate(payload)
        update_profile_preferences(handler.profile_path, update)
        summary = _profile_summary(handler.profile_path)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        ProfilePreferencesError,
        ProfileStoreError,
        OSError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(
        {
            "ok": True,
            "profile": summary.model_dump(mode="json") if summary else None,
        }
    )

@route("POST", r"/api/applications/(\d+)/status")
def handle_post_application_status(handler, job_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith(
            "application/json"
        ):
            raise ValueError("状态补录必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("状态补录内容格式不正确。")
        if payload.get("confirmed") is not True:
            raise ValueError("请先确认该状态由本人核实。")
        status = str(payload.get("status", "")).strip()
        detail = str(payload.get("detail", "")).strip()
        if len(detail) > 500:
            raise ValueError("状态备注最多 500 个字符。")
        if not detail:
            detail = (
                "用户在本地 Dashboard 手动确认已完成投递。"
                if status == "applied"
                else "用户在本地 Dashboard 手动确认该招聘进度。"
            )
        job_id = int(job_id_str)
        changed = handler.repository.record_application_status(
            job_id,
            status,  # type: ignore[arg-type]
            source="manual_dashboard",
            detail=detail,
            verification_method="user_confirmed_manual",
        )
        record = handler.repository.get_application(job_id)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, JobDatabaseError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(
        {
            "ok": True,
            "changed": changed,
            "application": record.model_dump(mode="json") if record else None,
        }
    )

@route("POST", r"/api/profile/onboard")
def handle_post_profile_onboard(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        content_type = handler.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("multipart/form-data;"):
            raise ProfileOnboardingError("资料导入必须使用文件上传表单。")
        fields, filename, resume_bytes = _parse_multipart(
            content_type,
            handler._read_body(_MAX_UPLOAD_BODY),
        )
        truthy = {"1", "true", "yes", "on"}
        request = ProfileOnboardingInput(
            resume_filename=filename,
            resume_bytes=resume_bytes,
            mode=fields.get("mode", "update").strip(),
            display_name=fields.get("display_name", ""),
            email=fields.get("email", ""),
            phone=fields.get("phone", ""),
            stage=fields.get("stage", ""),
            target_roles=_split_values(fields.get("target_roles", "")),
            adjacent_roles=_split_values(fields.get("adjacent_roles", "")),
            target_industries=_split_values(fields.get("target_industries", "")),
            preferred_locations=_split_values(fields.get("preferred_locations", "")),
            employment_types=_split_values(fields.get("employment_types", "")),
            must_haves=_split_values(fields.get("must_haves", "")),
            avoid=_split_values(fields.get("avoid", "")),
            earliest_start=fields.get("earliest_start", "").strip() or None,
            days_per_week=_optional_int(
                fields.get("days_per_week", ""),
                label="每周到岗天数",
                minimum=1,
                maximum=7,
            ),
            duration_months=_optional_int(
                fields.get("duration_months", ""),
                label="连续实习月数",
                minimum=1,
                maximum=60,
            ),
            commute_origin=fields.get("commute_origin", ""),
            max_commute_minutes=_optional_int(
                fields.get("max_commute_minutes", ""),
                label="单程通勤上限",
                minimum=5,
                maximum=240,
            ),
            transport_modes=_split_values(
                fields.get("transport_modes", "")
            ),
            remote_acceptable=fields.get(
                "remote_acceptable", ""
            ).casefold()
            in truthy,
            school=fields.get("school", ""),
            degree=fields.get("degree", ""),
            major=fields.get("major", ""),
            graduation=fields.get("graduation", "").strip() or None,
            notes=fields.get("notes", ""),
            confirm_truth=fields.get("confirm_truth", "").casefold() in truthy,
            confirm_replace=fields.get("confirm_replace", "").casefold() in truthy,
        )
        result = onboard_profile(
            request,
            profile_path=handler.profile_path,
            private_dir=handler.private_dir,
        )
        database_backup = None
        if result.replaced_profile:
            database_backup = handler.repository.backup_and_clear_for_new_profile(
                handler.private_dir / "backups"
            )
    except (
        ValueError,
        ProfileOnboardingError,
        ProfileStoreError,
        ResumeReadError,
        JobDatabaseError,
        OSError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(
        {
            "ok": True,
            "source_id": result.source_id,
            "resume_filename": (
                result.resume_path.name if result.resume_path else None
            ),
            "extracted_characters": result.extracted_characters,
            "imported_facts": result.imported_facts,
            "imported_skills": result.imported_skills,
            "warnings": result.warnings,
            "replaced_profile": result.replaced_profile,
            "database_backup": str(database_backup) if database_backup else None,
        }
    )
