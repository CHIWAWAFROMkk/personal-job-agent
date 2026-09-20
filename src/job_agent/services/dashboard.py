from __future__ import annotations

import json
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from datetime import UTC, date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen


from job_agent.services.dashboard_routes.dashboard_api import (
    build_dashboard_snapshot,
    _profile_summary,
    _commute_fit,
    _latest_preparation_path,
    _job_source_url,
    _safe_slug,
    _job_workspace,
)
from job_agent.models.dashboard import (
    DashboardBreakdownRow,
    DashboardFeedbackRow,
    DashboardFunnelStage,
    DashboardJobRow,
    DashboardJobWorkspace,
    DashboardMetric,
    DashboardPreparationRow,
    DashboardProfileSummary,
    DashboardSnapshot,
    DashboardStrategySummary,
)
from job_agent.models.job import MatchResult
from job_agent.models.job_record import JobDetail
from job_agent.models.profile import Profile
from job_agent.services.application_pack import (
    ApplicationPackError,
    build_application_pack,
    resolve_resume_bundle,
    write_application_pack,
)
from job_agent.services.browser_assist import (
    BrowserAssistError,
    create_application_session,
    find_application_pack,
    run_browser_session,
)
from job_agent.services.copilot_chat import (
    CopilotChatError,
    copilot_snapshot,
    reset_copilot_thread,
    respond_to_copilot,
)
from job_agent.services.commute_routing import (
    AmapCommuteProvider,
    CommuteRoutingError,
)
from job_agent.services.api_usage import load_api_usage, record_api_usage
from job_agent.services.job_repository import JobDatabaseError, JobRepository
from job_agent.services.job_strategy import JobStrategyResult, evaluate_job_strategy
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.profile_store import write_text_atomic
from job_agent.services.portable_resume import (
    PortableResumeError,
    build_portable_resume_content,
    build_portable_resume_draft,
    find_latest_resume_manifest,
    find_profile_photo,
    resume_artifact_from_manifest,
    save_profile_photo,
)
from job_agent.services.preparation_pack import preparation_priority_for_status
from job_agent.services.profile_onboarding import (
    ProfileOnboardingError,
    ProfileOnboardingInput,
    onboard_profile,
)
from job_agent.services.profile_preferences import (
    ProfilePreferencesError,
    ProfilePreferencesUpdate,
    update_profile_preferences,
)
from job_agent.services.profile_store import ProfileStoreError, load_profile
from job_agent.services.project_workshop import (
    ProjectWorkshopError,
    project_preview_path,
    project_workshop_snapshot,
    run_project,
    verify_project,
)
from job_agent.services.resume_editor import (
    ResumeEditorError,
    load_latest_resume_content,
    rerender_edited_resume,
    validate_resume_content,
)
from job_agent.services.resume_polish import (
    CloudAIUnavailableError,
    ResumePolishError,
    polish_resume_content_locally,
    polish_resume_content_with_jd,
)
from job_agent.services.resume_compose import compose_resume_content_with_jd
from job_agent.services.resume_import import (
    ResumeImportError,
    import_resume_text_into_draft,
)
from job_agent.services.resume_reader import ResumeReadError
from job_agent.services.runtime_config import (
    RuntimeConfigError,
    RuntimeConfigUpdate,
    effective_runtime_config,
    public_runtime_config,
    update_runtime_config,
)
from job_agent.services.tailored_resume import TailoredResumeError, approve_resume_visual_review
from job_agent.constants import (
    POSITIVE_FEEDBACK_STATUSES as _POSITIVE_FEEDBACK_STATUSES,
    MEANINGFUL_FEEDBACK_STATUSES as _MEANINGFUL_FEEDBACK_STATUSES,
    DASHBOARD_VERSION as _DASHBOARD_VERSION,
    MAX_JSON_BODY as _MAX_JSON_BODY,
    MAX_UPLOAD_BODY as _MAX_UPLOAD_BODY,
)
import logging

logger = logging.getLogger(__name__)


class DashboardError(RuntimeError):
    pass


_LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _split_values(value: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,，、;；\n]+", value or "")
        if item.strip()
    ]


def _optional_int(value: str, *, label: str, minimum: int, maximum: int) -> int | None:
    if not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ProfileOnboardingError(f"{label}必须填写整数。") from exc
    if parsed < minimum or parsed > maximum:
        raise ProfileOnboardingError(f"{label}必须在 {minimum}-{maximum} 之间。")
    return parsed


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], str, bytes]:
    try:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise ProfileOnboardingError("无法解析上传表单。") from exc
    if not message.is_multipart():
        raise ProfileOnboardingError("上传表单格式不正确。")
    fields: dict[str, str] = {}
    resume_filename = ""
    resume_bytes = b""
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if name == "resume" and filename:
            resume_filename = filename
            resume_bytes = payload
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            fields[name] = payload.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise ProfileOnboardingError(f"表单字段 {name} 不是有效文字。") from exc
    return fields, resume_filename, resume_bytes

def _dashboard_template_path() -> Path:
    return Path(__file__).resolve().parents[1] / "web" / "dashboard.html"


def _dashboard_stylesheet_path() -> Path:
    return Path(__file__).resolve().parents[1] / "web" / "dashboard.css"


def _dashboard_font_paths() -> dict[str, Path]:
    font_dir = Path(__file__).resolve().parents[1] / "web" / "fonts"
    names = (
        "SmileySans-Oblique.woff2",
        "NotoSerifSC-Variable.subset.woff2",
        "NotoSansSC-Variable.subset.woff2",
    )
    return {name: font_dir / name for name in names}


def create_dashboard_server(
    repository: JobRepository,
    *,
    output_dir: Path,
    port: int,
    profile_path: Path | None = None,
    private_dir: Path | None = None,
) -> ThreadingHTTPServer:
    template_path = _dashboard_template_path()
    if not template_path.is_file():
        raise DashboardError(f"Dashboard 页面模板不存在: {template_path}")
    template = template_path.read_bytes()
    stylesheet_path = _dashboard_stylesheet_path()
    if not stylesheet_path.is_file():
        raise DashboardError(f"Dashboard 样式表不存在: {stylesheet_path}")
    stylesheet = stylesheet_path.read_bytes()
    # Only bundled, top-level modules are public assets. Never turn a request
    # path into a filesystem path (including Windows backslashes or symlinks).
    js_dir = template_path.parent / "js"
    js_assets = {
        f"/js/{asset.name}": asset.read_bytes()
        for asset in js_dir.glob("*.js")
        if asset.is_file() and not asset.is_symlink()
        and asset.resolve().parent == js_dir.resolve()
    }
    font_assets: dict[str, bytes] = {}
    for font_name, font_path in _dashboard_font_paths().items():
        if not font_path.is_file():
            raise DashboardError(f"Dashboard 字体不存在: {font_path}")
        font_assets[f"/assets/fonts/{font_name}"] = font_path.read_bytes()
    action_token = secrets.token_urlsafe(32)
    profile_path = profile_path or repository.path.parent / "profile.json"
    private_dir = private_dir or profile_path.parent

    agent_token_path = private_dir / "agent-token.json"

    def _load_or_create_agent_token() -> str:
        try:
            payload = json.loads(agent_token_path.read_text(encoding="utf-8"))
            token = payload.get("token") if isinstance(payload, dict) else None
            if isinstance(token, str) and token:
                return token
        except (OSError, json.JSONDecodeError):
            pass
        token = secrets.token_urlsafe(32)
        agent_token_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(
            json.dumps(
                {
                    "token": token,
                    "created_at": datetime.now(UTC).isoformat(),
                    "note": "外部 Agent（WorkBuddy/Codex 等）调用 /api/agent/* 时须携带 X-Agent-Token 头。",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            agent_token_path,
        )
        return token

    agent_token = _load_or_create_agent_token()
    runtime_config_path = private_dir / "app-settings.json"
    api_usage_path = private_dir / "api-usage.json"
    copilot_thread_path = private_dir / "copilot" / "current-thread.json"
    copilot_lock = threading.Lock()
    assist_runs: dict[str, dict[str, object]] = {}
    assist_lock = threading.Lock()

    def public_assist_plan(session: object) -> dict[str, object]:
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

    def launch_assist_worker(session_path: Path, session_id: str) -> None:
        with assist_lock:
            assist_runs[session_id] = {"session_id": session_id, "status": "launching"}
        try:
            report, _ = run_browser_session(
                session_path,
                browser_profile_dir=private_dir / "browser-profiles",
                headless=False,
                keep_open=True,
                browser_channel="msedge",
                max_actions=12,
                dashboard_wait_seconds=900,
            )
        except Exception as exc:
            logger.exception("Browser session encountered an unexpected error.")
            public = {
                "session_id": session_id,
                "status": "failed",
                "error": "浏览器辅助投递过程发生内部错误，请重试或手动投递。",
                "submit_attempted": False,
            }
        else:
            public = {
                "session_id": session_id,
                "status": report.status,
                "filled_fields": report.filled_fields,
                "uploaded_resume": report.uploaded_resume,
                "manual_fields_detected": report.manual_fields_detected,
                "submit_controls_detected": report.submit_controls_detected,
                "submit_attempted": report.submit_attempted,
                "submission_outcome": report.submission_outcome,
                "blockers": report.blockers,
            }
        with assist_lock:
            assist_runs[session_id] = public

    class Handler(BaseHTTPRequestHandler):
        server_version = "JobAgentDashboard/2.1"

        def log_message(self, format: str, *args: object) -> None:
            # Windowed executables have no stderr. Do not log request URLs,
            # which may contain private query values or authentication tokens.
            logging.getLogger(__name__).debug("Local HTTP request handled")

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10)


        def _headers(
            self,
            status: HTTPStatus,
            content_type: str,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self._response_started = True
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-src 'self' blob:; object-src 'none'; "
                "frame-ancestors 'none'; base-uri 'none'",
            )
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()

        def _json(
            self,
            payload: object,
            status: HTTPStatus = HTTPStatus.OK,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._headers(
                status,
                "application/json; charset=utf-8",
                extra_headers={**(extra_headers or {}), "Content-Length": str(len(body))},
            )
            self.wfile.write(body)

        def _read_body(self, maximum: int) -> bytes:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                self.close_connection = True
                raise ValueError("请求必须包含唯一的 Content-Length，且不能使用分块传输。")
            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length)
            except ValueError as exc:
                self.close_connection = True
                raise ValueError("请求缺少有效的 Content-Length。") from exc
            if length < 0 or length > maximum:
                self.close_connection = True
                raise ValueError("请求内容过大。")
            try:
                body = self.rfile.read(length)
            except TimeoutError as exc:
                self.close_connection = True
                raise ValueError("上传请求超时，请重试。") from exc
            if len(body) != length:
                self.close_connection = True
                raise ValueError("请求内容未完整传输。")
            return body

        def _trusted_host(self) -> bool:
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                return False
            return hosts[0].casefold() in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
                *({"127.0.0.1", "localhost"} if self.server.server_port == 80 else set()),
            }

        def _authorized_action(self) -> bool:
            if not self._trusted_host():
                return False
            host = self.headers.get("Host", "")
            try:
                hostname = urlsplit("http://" + host).hostname
            except ValueError:
                hostname = None
            if hostname not in {"127.0.0.1", "localhost"}:
                return False
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1 or (origins and origins[0] != "http://" + host):
                return False
            supplied = self.headers.get("X-Job-Agent-Token", "")
            return bool(supplied) and secrets.compare_digest(supplied.encode("utf-8"), action_token.encode("utf-8"))

        def _reject_unauthorized_action(self) -> None:
            self._discard_small_rejected_body()
            self.close_connection = True
            self._json(
                {"error": "本地操作授权已过期，请刷新页面后重试。"},
                HTTPStatus.FORBIDDEN,
                extra_headers={"Connection": "close"},
            )

        def _authorized_agent(self) -> bool:
            supplied = self.headers.get("X-Agent-Token", "")
            return self._trusted_host() and bool(supplied) and secrets.compare_digest(supplied.encode("utf-8"), agent_token.encode("utf-8"))
        def _reject_unauthorized_agent(self) -> None:
            self._discard_small_rejected_body()
            self.close_connection = True
            self._json(
                {"error": "Agent Token 无效；请读取 data/private/agent-token.json。"},
                HTTPStatus.FORBIDDEN,
                extra_headers={"Connection": "close"},
            )

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            self._dispatch_request("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            self._dispatch_request("POST")

        def _method_not_allowed(self) -> None:
            self._dispatch_request(self.command)

        do_PUT = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_DELETE = _method_not_allowed

        def _discard_small_rejected_body(self) -> None:
            # A Windows socket closed with unread POST bytes can reset before
            # the client receives the 403. Discard bounded bytes, never parse
            # or execute them. Large/malformed/slow uploads are simply closed.
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                return
            try:
                length = int(lengths[0])
            except ValueError:
                return
            if not 0 < length <= 8192:
                return
            previous_timeout = self.connection.gettimeout()
            try:
                deadline = time.monotonic() + 0.2
                while length > 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.connection.settimeout(remaining)
                    chunk = self.rfile.read1(length)
                    if not chunk:
                        break
                    length -= len(chunk)
            except (OSError, ValueError):
                pass
            finally:
                self.connection.settimeout(previous_timeout)

        def _request_source_allowed(self, method: str, path: str) -> bool:
            """Browser-origin guard, not authentication against local processes.

            Native CLI clients may omit these browser-controlled headers. A
            privileged extension/local attacker able to do so is out of scope.
            Agent credentials never grant access to the UI bootstrap or files.
            """
            origins = self.headers.get_all("Origin", [])
            sites = self.headers.get_all("Sec-Fetch-Site", [])
            if len(origins) > 1 or len(sites) > 1:
                return False
            origin = origins[0] if origins else None
            same_origin = origin is None or origin == "http://" + self.headers.get("Host", "")
            cross_site = bool(sites and sites[0].casefold() == "cross-site")
            if same_origin and not cross_site:
                return True
            extension_origin = bool(origin and re.fullmatch(r"chrome-extension://[a-p]{32}", origin))
            if origin is not None and not extension_origin:
                return False
            agent_endpoint = (
                method == "GET" and bool(re.fullmatch(
                    r"/api/(?:jobs/[0-9]+/fill-data|agent/(?:state|jobs(?:/[0-9]+)?|applications|candidates))", path
                ))
            ) or (
                method == "POST" and path in {
                    "/api/jobs/import-parsed", "/api/fill/session", "/api/fill/poll", "/api/fill/close",
                }
            )
            return agent_endpoint and self._authorized_agent()

        def _dispatch_request(self, method: str) -> None:
            if not self._trusted_host():
                self.close_connection = True
                self._json({"error": "仅允许通过本机地址访问。"}, HTTPStatus.FORBIDDEN)
                return
            try:
                path = urlsplit(self.path).path
            except ValueError:
                self._json({"error": "请求地址格式不正确。"}, HTTPStatus.BAD_REQUEST)
                return
            if not self._request_source_allowed(method, path):
                self._discard_small_rejected_body()
                self.close_connection = True
                self._json({"error": "请从本机工作台或已配对的伴侣扩展访问。"}, HTTPStatus.FORBIDDEN,
                           extra_headers={"Connection": "close"})
                return
            try:
                if dispatch(self, method, path):
                    return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
                return
            except Exception:
                logger.exception("Dashboard request failed (%s).", method)
                self.close_connection = True
                if not getattr(self, "_response_started", False):
                    self._json({"error": "处理请求时发生内部错误，请稍后重试。"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if method != "GET":
                self.close_connection = True
                message = "此地址不支持写入。" if method == "POST" else "不支持此请求方法。"
                self._json({"error": message}, HTTPStatus.METHOD_NOT_ALLOWED)
                return
            self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
            self.wfile.write("Not found".encode("utf-8"))

    # Register routes once before accepting requests. Missing modules fail at
    # startup instead of dropping the first browser connection.
    from job_agent.services.dashboard_routes import assets, jobs_api, copilot_api, agent_api, profile_api, tracking_api, mock_interview_api, dispatch
    Handler.repository = repository
    Handler.output_dir = output_dir
    Handler.profile_path = profile_path
    Handler.private_dir = private_dir
    Handler.action_token = action_token
    Handler.runtime_config_path = runtime_config_path
    Handler.api_usage_path = api_usage_path
    Handler.copilot_thread_path = copilot_thread_path
    Handler.copilot_lock = copilot_lock
    Handler.assist_runs = assist_runs
    Handler.assist_lock = assist_lock
    Handler.template = template
    Handler.stylesheet = stylesheet
    Handler.font_assets = font_assets
    Handler.js_assets = js_assets
    Handler.agent_token = agent_token
    Handler.web_dir = _dashboard_template_path().parent

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def run_dashboard(
    repository: JobRepository,
    *,
    output_dir: Path,
    profile_path: Path | None = None,
    private_dir: Path | None = None,
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    if port < 1 or port > 65535:
        raise DashboardError("Dashboard 端口必须在 1 到 65535 之间。")
    selected_port = port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            existing_url = f"http://127.0.0.1:{port}/"
            try:
                with urlopen(existing_url + "api/health", timeout=0.4) as response:
                    health = json.loads(response.read().decode("utf-8"))
            except (OSError, ValueError):
                health = {}
            if (
                health.get("service") == "personal-job-agent"
                and health.get("version") == _DASHBOARD_VERSION
            ):
                print(f"个人求职 Agent 已在运行: {existing_url}")
                if open_browser:
                    webbrowser.open(existing_url)
                return
            selected_port = 0
    try:
        server = create_dashboard_server(
            repository,
            output_dir=output_dir,
            port=selected_port,
            profile_path=profile_path,
            private_dir=private_dir,
        )
    except OSError as exc:
        if selected_port == 0:
            raise DashboardError(f"无法启动本地 Dashboard：{exc}") from exc
        try:
            server = create_dashboard_server(
                repository,
                output_dir=output_dir,
                port=0,
                profile_path=profile_path,
                private_dir=private_dir,
            )
        except OSError as fallback_exc:
            raise DashboardError(f"无法启动本地 Dashboard：{fallback_exc}") from fallback_exc
    url = f"http://127.0.0.1:{server.server_port}/"
    if server.server_port != port:
        print(f"默认端口 {port} 正在使用，已自动切换到可用端口。")
    print(f"个人求职 Agent 已启动: {url}")
    print("关闭此窗口或按 Ctrl+C 即可停止；只有本人点击确认后才会补录状态或更新资料。")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        from job_agent.services.sms_sync import get_lan_ip, start_sms_listener
        start_sms_listener(port=8088, private_dir=server.RequestHandlerClass.private_dir)
        print("短信同步监听已启动；配对链接请在本机设置中查看。")
    except Exception as sms_exc:
        logger.warning("短信监听服务启动异常: %s", sms_exc)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nDashboard 已停止。")
    finally:
        server.server_close()
        try:
            from job_agent.services.sms_sync import stop_sms_listener
            stop_sms_listener()
        except Exception:
            pass
