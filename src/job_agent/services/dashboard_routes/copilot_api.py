from __future__ import annotations
import json
from http import HTTPStatus
from job_agent.constants import MAX_JSON_BODY as _MAX_JSON_BODY

from job_agent.services.dashboard_routes import route
from job_agent.services.copilot_chat import (
    CopilotChatError,
    copilot_snapshot,
    reset_copilot_thread,
    respond_to_copilot,
)
from job_agent.services.job_repository import JobDatabaseError

@route("GET", r"/api/copilot")
def handle_get_copilot(handler):
    try:
        with handler.copilot_lock:
            snapshot = copilot_snapshot(
                handler.copilot_thread_path,
                runtime_config_path=handler.runtime_config_path,
                repository=handler.repository,
                profile_path=handler.profile_path,
            )
    except CopilotChatError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(snapshot.model_dump(mode="json"))

@route("GET", r"/api/assist/([0-9A-Za-z_-]+)")
def handle_get_assist(handler, session_id):
    with handler.assist_lock:
        payload = handler.assist_runs.get(session_id)
    if payload is None:
        handler._json({"error": "没有找到该代填会话。"}, HTTPStatus.NOT_FOUND)
        return
    handler._json(payload)

@route("POST", r"/api/copilot/message")
def handle_post_copilot_message(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        if not handler.headers.get("Content-Type", "").casefold().startswith(
            "application/json"
        ):
            raise ValueError("Agent 对话必须使用 JSON 请求。")
        payload = json.loads(handler._read_body(_MAX_JSON_BODY).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Agent 对话内容格式不正确。")
        with handler.copilot_lock:
            snapshot = respond_to_copilot(
                str(payload.get("message", "")),
                selected_job_id=payload.get("job_id"),
                thread_path=handler.copilot_thread_path,
                repository=handler.repository,
                profile_path=handler.profile_path,
                runtime_config_path=handler.runtime_config_path,
                usage_path=handler.api_usage_path,
                applications_dir=handler.output_dir / "applications",
            )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        CopilotChatError,
        JobDatabaseError,
    ) as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True, "copilot": snapshot.model_dump(mode="json")})

@route("POST", r"/api/copilot/reset")
def handle_post_copilot_reset(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        with handler.copilot_lock:
            reset_copilot_thread(handler.copilot_thread_path)
            snapshot = copilot_snapshot(
                handler.copilot_thread_path,
                runtime_config_path=handler.runtime_config_path,
                repository=handler.repository,
                profile_path=handler.profile_path,
            )
    except CopilotChatError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({"ok": True, "copilot": snapshot.model_dump(mode="json")})
