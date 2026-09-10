from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import multiprocessing
import os
import sys
import threading
import time
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from job_agent import __version__


APP_TITLE = "个人求职 Agent"
APP_DATA_DIRECTORY = "PersonalJobAgent"
DEFAULT_WINDOW_SIZE = (1280, 820)
MINIMUM_WINDOW_SIZE = (960, 640)


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def default_user_data_root(
    *,
    explicit: Path | None = None,
    frozen: bool | None = None,
) -> Path:
    """Return the private workspace used by this Windows user.

    Source runs keep using the checked-out project so existing development data
    remains available. Packaged runs use LocalAppData, which keeps updates and
    copies of the application separate from resumes, API keys, and databases.
    """

    if explicit is not None:
        return explicit.expanduser().resolve()
    configured = os.getenv("JOB_AGENT_PROJECT_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if frozen if frozen is not None else _is_frozen():
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        base = (
            Path(local_app_data).expanduser()
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
        return (base / APP_DATA_DIRECTORY).resolve()
    return Path(__file__).resolve().parents[2]


def prepare_user_workspace(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    for relative in (
        Path("data/private"),
        Path("data/private/browser-sessions"),
        Path("data/private/browser-profiles"),
        Path("data/inbox"),
        Path("data/output"),
        Path("logs"),
    ):
        (resolved / relative).mkdir(parents=True, exist_ok=True)
    return resolved


def _configure_logging(root: Path) -> Path:
    log_path = root / "logs" / "desktop.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if not any(
        isinstance(item, logging.handlers.RotatingFileHandler)
        and Path(item.baseFilename) == log_path
        for item in logger.handlers
    ):
        logger.addHandler(handler)
    return log_path


@dataclass
class DesktopServer:
    server: ThreadingHTTPServer
    thread: threading.Thread
    url: str

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_desktop_server(data_root: Path) -> DesktopServer:
    # This must happen before settings are resolved. It also lets test and
    # support builds select an isolated workspace without touching real data.
    os.environ["JOB_AGENT_PROJECT_ROOT"] = str(data_root)

    from job_agent.services.dashboard import create_dashboard_server
    from job_agent.services.job_repository import JobRepository
    from job_agent.settings import get_settings

    settings = get_settings()
    repository = JobRepository(settings.job_db_path)
    repository.initialize()
    server = create_dashboard_server(
        repository,
        output_dir=settings.output_dir,
        profile_path=settings.profile_path,
        private_dir=settings.private_dir,
        port=0,
    )
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="job-agent-local-service",
        daemon=True,
    )
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    runtime = DesktopServer(server=server, thread=thread, url=url)
    try:
        wait_until_ready(runtime.url)
    except Exception:
        runtime.stop()
        raise
    return runtime


def wait_until_ready(url: str, *, timeout: float = 8.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url + "api/health", timeout=0.8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "ok":
                return payload
        except (OSError, ValueError) as exc:
            last_error = exc
        time.sleep(0.08)
    detail = f"：{last_error}" if last_error else ""
    raise RuntimeError(f"本地服务没有按时启动{detail}")


def desktop_smoke_test(data_root: Path, report_path: Path | None = None) -> Path:
    # Exercise lazy SDK imports in the frozen build without making a network call.
    from openai import OpenAI

    with OpenAI(api_key="local-smoke-only", base_url="http://127.0.0.1:1/v1") as client:
        sdk_ready = callable(client.responses.create) and callable(client.chat.completions.create)
    runtime = start_desktop_server(data_root)
    try:
        health = wait_until_ready(runtime.url)
        with urlopen(runtime.url + "api/settings", timeout=2) as response:
            connector_settings = json.loads(response.read().decode("utf-8"))
        report = {
            "status": "ok",
            "openai_sdk_ready": sdk_ready,
            "app_version": __version__,
            "service": health.get("service"),
            "service_version": health.get("version"),
            "data_root": str(data_root),
            "database_created": (data_root / "data/private/job_agent.sqlite3").is_file(),
            "settings_api_ready": "ai" in connector_settings
            and "search" in connector_settings
            and "maps" in connector_settings,
        }
    finally:
        runtime.stop()
    target = report_path or data_root / "logs" / "desktop-smoke-test.json"
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _show_fatal_error(message: str, log_path: Path | None) -> None:
    suffix = f"\n\n诊断日志：{log_path}" if log_path else ""
    text = message + suffix
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x10)
            return
        except Exception:
            pass
    print(text, file=sys.stderr)


def launch_desktop(data_root: Path) -> None:
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("桌面窗口组件未安装，请重新安装桌面版程序。") from exc

    runtime = start_desktop_server(data_root)
    try:
        webview.create_window(
            APP_TITLE,
            runtime.url,
            width=DEFAULT_WINDOW_SIZE[0],
            height=DEFAULT_WINDOW_SIZE[1],
            min_size=MINIMUM_WINDOW_SIZE,
            background_color="#e9e7e1",
        )
        # pywebview selects Edge Chromium on current Windows versions. Keeping
        # backend selection automatic also provides its supported fallbacks.
        webview.start(debug=False)
    finally:
        runtime.stop()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="PersonalJobAgent")
    parser.add_argument(
        "--data-dir",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--smoke-report",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = _build_parser().parse_args(argv)
    data_root = prepare_user_workspace(
        default_user_data_root(explicit=args.data_dir)
    )
    log_path = _configure_logging(data_root)
    logging.info("Starting Personal Job Agent %s in %s", __version__, data_root)
    try:
        if args.smoke_test:
            desktop_smoke_test(data_root, args.smoke_report)
        else:
            launch_desktop(data_root)
    except Exception as exc:
        logging.exception("Desktop startup failed")
        _show_fatal_error(f"个人求职 Agent 启动失败：{exc}", log_path)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
