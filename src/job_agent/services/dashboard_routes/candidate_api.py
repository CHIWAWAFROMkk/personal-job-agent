"""Small, action-token protected queue for reviewing discovered links."""

from __future__ import annotations

import json
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

from job_agent.constants import MAX_JSON_BODY
from job_agent.models.job_record import CandidateVerificationStatus
from job_agent.services.dashboard_routes import route
from job_agent.services.job_repository import CANDIDATE_VERIFICATION_STATUSES, JobDatabaseError


@route("GET", r"/api/candidates")
def handle_get_candidates(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        query = parse_qs(urlsplit(handler.path).query)
        status_value = query.get("status", ["needs_manual_review"])[0]
        if status_value != "all" and status_value not in CANDIDATE_VERIFICATION_STATUSES:
            raise ValueError("不支持的候选核验状态。")
        status: CandidateVerificationStatus | None = (
            None if status_value == "all" else status_value
        )
        page = int(query.get("page", ["0"])[0])
        if page < 0 or page > 100000:
            raise ValueError("候选岗位页码无效。")
        page_size = 20
        total = handler.repository.count_search_candidates(status=status)
        candidates = handler.repository.list_search_candidates(
            status=status, limit=page_size, offset=page * page_size
        )
    except (ValueError, JobDatabaseError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({
        "items": [item.model_dump(mode="json") for item in candidates],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@route("POST", r"/api/candidates/(\d+)/verify")
def handle_post_candidate_verify(handler, candidate_id_str):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ValueError("候选核验必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            raise ValueError("请确认你已亲自核实这条候选链接。")
        status = payload.get("status")
        detail = str(payload.get("detail", "")).strip()
        if status not in CANDIDATE_VERIFICATION_STATUSES:
            raise ValueError("不支持的候选核验状态。")
        if len(detail) > 500:
            raise ValueError("核验说明最多 500 个字符。")
        handler.repository.mark_candidate_verification(
            int(candidate_id_str), status, detail=detail
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, JobDatabaseError) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True, "candidate_id": int(candidate_id_str), "status": status})
