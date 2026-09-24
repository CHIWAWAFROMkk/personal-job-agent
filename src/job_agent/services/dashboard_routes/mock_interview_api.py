"""Authenticated local mock interview routes."""
import json
import sqlite3

from job_agent.services.dashboard_routes import route
from job_agent.services.mock_interview import InterviewConflict, MockInterviewStore, teleprompter
from job_agent.services.profile_store import load_profile, ProfileStoreError
from job_agent.services.runtime_config import effective_runtime_config
from job_agent.services.job_repository import JobDatabaseError


def _run(handler, callback):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return
    try:
        result = callback(MockInterviewStore(
            handler.repository.path, usage_path=handler.api_usage_path,
        ))
    except InterviewConflict as exc:
        handler._json({'error': str(exc)}, 409)
        return
    except LookupError:
        handler._json({'error': '对练记录不存在。'}, 404)
        return
    except (ValueError, TypeError, JobDatabaseError):
        handler._json({'error': '请求无效。请核对岗位、回答长度、会话版本及云端授权。'}, 400)
        return
    except sqlite3.Error:
        handler._json({'error': '本地记录暂时不可写，请稍后重试；未完成的操作不会保存。'}, 503)
        return
    except (ProfileStoreError, OSError):
        handler._json({'error': '请先在个人资料中保存可用档案，再开始对练。'}, 400)
        return
    handler._json(result)


def _body(handler):
    obj = json.loads(handler._read_body(40000))
    if not isinstance(obj, dict):
        raise ValueError('需要对象。')
    return obj


def _job(handler, value):
    if type(value) is not int or value <= 0:
        raise ValueError('岗位编号无效。')
    return handler.repository.get_job(value)


@route('POST', r'/api/interview/session/start')
def start(handler):
    def run(store):
        p = _body(handler)
        return store.start(p, _job(handler, p.get('job_id')), load_profile(handler.profile_path), effective_runtime_config(handler.runtime_config_path)[0])
    _run(handler, run)


@route('POST', r'/api/interview/session/reply')
def reply(handler):
    _run(handler, lambda store: store.reply(_body(handler), effective_runtime_config(handler.runtime_config_path)[0], load_profile(handler.profile_path)))


@route('POST', r'/api/interview/session/score')
def score(handler):
    _run(handler, lambda store: store.score(_body(handler)))


@route('GET', r'/api/interview/session/([0-9a-f-]{36})')
def get(handler, session_id):
    _run(handler, lambda store: store.get(session_id))


@route('GET', r'/api/interview/jobs/(\d+)/sessions')
def history(handler, job_id):
    _run(handler, lambda store: {'sessions': store.list(_job(handler, int(job_id)).job_id)})


@route('POST', r'/api/interview/teleprompter')
def cards(handler):
    def run(store):
        p = _body(handler)
        return teleprompter(_job(handler, p.get('job_id')), load_profile(handler.profile_path))
    _run(handler, run)
