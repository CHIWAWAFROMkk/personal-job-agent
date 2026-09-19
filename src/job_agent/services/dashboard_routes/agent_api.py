from __future__ import annotations
from http import HTTPStatus

from job_agent.services.dashboard_routes import route
from job_agent.services.job_repository import JobDatabaseError
from job_agent.constants import DASHBOARD_VERSION as _DASHBOARD_VERSION


def _local_sensitive_action(handler):
    origin = handler.headers.get('Origin')
    expected = 'http://' + handler.headers.get('Host', '')
    if not handler._authorized_action() or (origin is not None and origin != expected):
        handler._reject_unauthorized_action()
        return False
    return True


@route('POST', r'/api/agent/token')
def handle_copy_agent_token(handler):
    if _local_sensitive_action(handler):
        handler._json({'agent_token': handler.agent_token})


@route('POST', r'/api/sms/setup')
def handle_sms_setup(handler):
    if not _local_sensitive_action(handler):
        return
    from job_agent.services.sms_sync import get_sms_setup
    try:
        handler._json(get_sms_setup(handler.private_dir))
    except (ValueError, OSError, RuntimeError):
        handler._json({'error': '短信配对信息读取失败，请检查本机监听设置。'}, HTTPStatus.BAD_REQUEST)


@route('GET', r'/api/profile')
def handle_profile_preview(handler):
    if not _local_sensitive_action(handler):
        return
    from job_agent.services.profile_store import load_profile, ProfileStoreError
    try:
        profile = load_profile(handler.profile_path)
    except (ProfileStoreError, OSError):
        handler._json({'error': '请先在个人资料中建立并核对档案。'}, HTTPStatus.NOT_FOUND)
        return
    ready = {'documented', 'user_confirmed'}
    education = next((e for e in profile.education if e.status in ready), None)
    facts = [f.statement for e in profile.experiences for f in e.facts if f.status in ready]
    handler._json({'facts': {
        'name': profile.person.legal_name or profile.person.display_name,
        'phone': profile.person.contact.phone,
        'email': profile.person.contact.email,
        'school': education.institution if education else None,
        'degree': ' / '.join(filter(None, [education.degree, education.major])) if education else None,
        'gpa': education.gpa if education else None,
        'experience': '；'.join(facts[:8]),
    }})

@route("GET", r"/api/agent/state")
def handle_get_agent_state(handler):
    if not handler._authorized_agent():
        handler._reject_unauthorized_agent()
        return
    try:
        payload = {
            "stats": handler.repository.stats().model_dump(mode="json"),
            "funnel": handler.repository.application_summary().model_dump(mode="json"),
            "service": "personal-job-agent",
            "version": _DASHBOARD_VERSION,
        }
    except JobDatabaseError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(payload)

@route("GET", r"/api/agent/jobs")
def handle_get_agent_jobs(handler):
    if not handler._authorized_agent():
        handler._reject_unauthorized_agent()
        return
    try:
        jobs = handler.repository.list_jobs(limit=100)
        payload = {
            "jobs": [item.model_dump(mode="json") for item in jobs],
            "count": len(jobs),
        }
    except JobDatabaseError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(payload)

@route("GET", r"/api/agent/jobs/(\d+)")
def handle_get_agent_job_detail(handler, job_id_str):
    if not handler._authorized_agent():
        handler._reject_unauthorized_agent()
        return
    try:
        job_id = int(job_id_str)
        job = handler.repository.get_job(job_id)
        insights = handler.repository.get_latest_match_insights([job_id])
        application = handler.repository.get_application(job_id)
        payload = {
            "job": job.model_dump(mode="json"),
            "match_insight": insights.get(job_id),
            "application": (
                application.model_dump(mode="json")
                if application is not None
                else None
            ),
        }
    except JobDatabaseError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(payload)

@route("GET", r"/api/agent/applications")
def handle_get_agent_applications(handler):
    if not handler._authorized_agent():
        handler._reject_unauthorized_agent()
        return
    try:
        applications = handler.repository.list_applications(limit=100)
        payload = {
            "applications": [
                item.model_dump(mode="json") for item in applications
            ],
            "count": len(applications),
        }
    except JobDatabaseError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(payload)

@route("GET", r"/api/agent/candidates")
def handle_get_agent_candidates(handler):
    if not handler._authorized_agent():
        handler._reject_unauthorized_agent()
        return
    try:
        candidates = handler.repository.list_search_candidates(limit=100)
        payload = {
            "candidates": [
                item.model_dump(mode="json") for item in candidates
            ],
            "count": len(candidates),
        }
    except JobDatabaseError as exc:
        handler._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(payload)
