from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import multiprocessing
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from job_agent import __version__
from job_agent.services.profile_store import write_text_atomic


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
    configured = (
        os.getenv("JOB_AGENT_DATA_DIR", "").strip()
        or os.getenv("JOB_AGENT_PROJECT_ROOT", "").strip()
    )
    if configured:
        return Path(configured).expanduser().resolve()
    effective_frozen = _is_frozen() if frozen is None else frozen
    if effective_frozen:
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


def start_desktop_server(data_root: Path, port: int = 0) -> DesktopServer:
    # This must happen before settings are resolved. It also lets test and
    # support builds select an isolated workspace without touching real data.
    os.environ["JOB_AGENT_PROJECT_ROOT"] = str(data_root)

    from job_agent.services.dashboard import create_dashboard_server
    from job_agent.services.job_repository import JobRepository
    from job_agent.settings import get_settings

    settings = get_settings()
    repository = JobRepository(settings.job_db_path)
    from job_agent.services.profile_recovery import recover_interrupted_profile_switch
    recovery = recover_interrupted_profile_switch(
        profile_path=settings.profile_path, private_dir=settings.private_dir,
        output_dir=settings.output_dir, repository=repository,
    )
    if recovery:
        logging.info("Recovered interrupted profile switch: %s", recovery)
    repository.initialize()
    server = create_dashboard_server(
        repository,
        output_dir=settings.output_dir,
        profile_path=settings.profile_path,
        private_dir=settings.private_dir,
        port=port,
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


def _require_isolated_test_root(data_root: Path, *, prefix: str) -> None:
    resolved = data_root.expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if (
        resolved == temp_root
        or not resolved.is_relative_to(temp_root)
        or not resolved.name.startswith(prefix)
    ):
        raise RuntimeError(f"桌面测试只能在独立的 {prefix}* 临时目录运行。")
    if any((resolved / "data/private" / name).exists() for name in
           ("profile.json", "job_agent.sqlite3", "app-settings.json")):
        raise RuntimeError("验收测试目录中已有个人资料，已停止以避免写入示例身份。")


def desktop_smoke_test(data_root: Path, report_path: Path | None = None) -> Path:
    from job_agent.desktop_qa import desktop_smoke_test as run_smoke_test

    return run_smoke_test(data_root, report_path)


def desktop_acceptance_test(data_root: Path, report_path: Path | None = None) -> Path:
    _require_isolated_test_root(data_root, prefix="acceptance-")
    from job_agent.desktop_qa import desktop_acceptance_test as run_acceptance_test

    return run_acceptance_test(data_root, report_path)


def desktop_window_test(data_root: Path, report_path: Path | None = None, *, restart: bool = False) -> Path:
    if not restart:
        _require_isolated_test_root(data_root, prefix="window-")
    from job_agent.desktop_qa import desktop_window_test as run_window_test

    return run_window_test(data_root, report_path, restart=restart)


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
        "--port",
        type=int,
        default=0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--url-file",
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
    parser.add_argument(
        "--acceptance-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--acceptance-report",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--window-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--window-report",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--window-restart-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    return parser


class SingleInstanceLock:
    """按数据目录加排他锁，防止多实例并发访问造成数据破坏。"""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path.expanduser().resolve()
        self.pid_path = self.lock_path.with_name(self.lock_path.name + ".pid")
        self._file = None
        self._holding = False

    def acquire(self) -> tuple[bool, int | None]:
        """尝试获取锁。返回 (是否成功, 若失败持有者 PID)。"""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._file = open(self.lock_path, "a+b")
            self._file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            self._holding = True
            try:
                self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            except Exception:
                pass
            return True, os.getpid()
        except (OSError, IOError):
            holder_pid = None
            try:
                if self.pid_path.is_file():
                    content = self.pid_path.read_text(encoding="utf-8").strip()
                    if content and content.isdigit():
                        holder_pid = int(content)
            except Exception:
                pass
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
            return False, holder_pid

    def release(self) -> None:
        if self._holding and self._file:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
            self._holding = False
            try:
                if self.pid_path.is_file():
                    self.pid_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                if self.lock_path.is_file():
                    self.lock_path.unlink(missing_ok=True)
            except Exception:
                pass

    def __enter__(self) -> SingleInstanceLock:
        ok, _ = self.acquire()
        if not ok:
            raise RuntimeError(f"无法获取数据目录单实例锁: {self.lock_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = _build_parser().parse_args(argv)
    candidate_root = default_user_data_root(explicit=args.data_dir)
    if args.acceptance_test or args.window_test or args.window_restart_test:
        if args.data_dir is None:
            print("Error: 桌面验收测试必须指定独立的 --data-dir。", file=sys.stderr)
            return 1
        try:
            if args.window_restart_test:
                from job_agent.desktop_qa import _require_window_restart_root
                _require_window_restart_root(candidate_root)
            else:
                _require_isolated_test_root(
                    candidate_root,
                    prefix="acceptance-" if args.acceptance_test else "window-",
                )
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    data_root = prepare_user_workspace(candidate_root)
    log_path = _configure_logging(data_root)
    logging.info("Starting Personal Job Agent %s in %s", __version__, data_root)
    is_automated = args.acceptance_test or args.smoke_test or args.window_test or args.window_restart_test or args.headless

    instance_lock = SingleInstanceLock(data_root / ".instance_lock")
    acquired, holder_pid = instance_lock.acquire()
    if not acquired:
        pid_hint = f"（进程 PID: {holder_pid}）" if holder_pid else ""
        msg = f"个人求职 Agent 已经在运行中{pid_hint}。同一数据目录只允许运行一个实例，请直接使用已有窗口。"
        logging.warning(msg)
        if not is_automated:
            _show_fatal_error(msg, log_path)
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    try:
        if args.acceptance_test:
            desktop_acceptance_test(data_root, args.acceptance_report)
        elif args.smoke_test:
            desktop_smoke_test(data_root, args.smoke_report)
        elif args.window_test or args.window_restart_test:
            desktop_window_test(data_root, args.window_report, restart=args.window_restart_test)
        elif args.headless:
            runtime = start_desktop_server(data_root, port=args.port)
            if args.url_file:
                args.url_file.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(runtime.url + "\n", args.url_file)
            logging.info("Headless server running at %s", runtime.url)
            try:
                while True:
                    time.sleep(0.5)
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                runtime.stop()
        else:
            launch_desktop(data_root)
    except Exception as exc:
        logging.exception("Desktop startup failed")
        if not is_automated:
            _show_fatal_error(f"个人求职 Agent 启动失败：{exc}", log_path)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        instance_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
