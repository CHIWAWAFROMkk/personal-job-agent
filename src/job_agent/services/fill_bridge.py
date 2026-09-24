"""Minimal profile handoff and short-lived browser fill sessions."""
from __future__ import annotations

import ipaddress
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from job_agent.models.profile import ClaimStatus, Profile


def public_https_origin(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url) or "\\" in url:
        raise ValueError("请使用岗位的公开 HTTPS 页面。")
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.username or parts.password or not host or parts.port not in (None, 443):
        raise ValueError("请使用岗位的公开 HTTPS 页面。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or "." not in host or host.endswith((".localhost", ".local", ".internal", ".lan")) or host.split(".")[-1].isdigit():
            raise ValueError("不支持本地或非公开招聘页面。")
    else:
        if not address.is_global:
            raise ValueError("不支持本地或非公开招聘页面。")
    return "https://" + (f"[{host}]" if ":" in host else host)


def minimal_fill_data(profile: Profile, job_id: int) -> dict:
    confirmed = {ClaimStatus.DOCUMENTED, ClaimStatus.USER_CONFIRMED}
    person = profile.person
    fields = {"name": person.legal_name or person.display_name,
              "phone": person.contact.phone, "email": person.contact.email}
    education = next((edu for edu in profile.education if edu.status in confirmed), None)
    if education:
        fields.update(school=education.institution, major=education.major, degree=education.degree)
    blocks = []
    for experience in profile.experiences:
        facts = [fact.statement for fact in experience.facts if fact.status in confirmed]
        if facts:
            heading = " / ".join(value.strip() for value in (experience.organization, experience.role) if value.strip())
            blocks.append("\n".join([heading, *facts]))
    notes = []
    if blocks:
        experience_text = "\n\n".join(blocks)
        if len(experience_text) <= 12000:
            fields["experience"] = experience_text
        else:
            notes.append("经历超过代填长度限制，请按每段机构与岗位手动填写。")
    fields = {key: value.strip() for key, value in fields.items() if isinstance(value, str) and value.strip()}
    return {"job_id": job_id, "fields": fields, "notes": notes,
            "attachment": {"available": False, "note": "请本人上传已审阅简历"}}


class FillBridge:
    SESSION_TTL = 120
    LIMIT = 256

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}

    def _prune(self):
        now = self.clock()
        self.sessions = {key: value for key, value in self.sessions.items() if value["deadline"] > now}

    def create(self, job_id, url, source_url):
        origin = public_https_origin(url)
        if urlsplit(origin).hostname != urlsplit(public_https_origin(source_url)).hostname:
            raise ValueError("当前页面与岗位来源域名不一致，请手动填写。")
        with self.lock:
            self._prune()
            if len(self.sessions) >= self.LIMIT:
                raise ValueError("代填会话过多，请关闭旧会话后重试。")
            session_id = secrets.token_urlsafe(32)
            self.sessions[session_id] = {"job_id": job_id, "origin": origin, "deadline": self.clock() + self.SESSION_TTL, "code": None}
            return {"session_id": session_id, "job_id": job_id, "origin": origin, "expires_in": self.SESSION_TTL}

    def list(self, job_id):
        with self.lock:
            self._prune()
            return [{"session_id": key, "job_id": value["job_id"], "origin": value["origin"]} for key, value in self.sessions.items() if value["job_id"] == job_id]

    def poll(self, session_id):
        with self.lock:
            self._prune()
            session = self.sessions.get(session_id)
            if not session:
                return {"code": None}
            code, session["code"] = session["code"], None
            return {"code": code}

    def close(self, session_id):
        with self.lock:
            self.sessions.pop(session_id, None)


_init_lock = threading.Lock()


def server_bridge(handler):
    with _init_lock:
        if not hasattr(handler.server, "fill_bridge"):
            handler.server.fill_bridge = FillBridge()
        return handler.server.fill_bridge
