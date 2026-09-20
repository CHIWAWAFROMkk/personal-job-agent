"""SMS sync service for Personal Job Agent.

Enables iPhone / Android devices to push SMS verification codes to the agent
via a local LAN webhook, and extracts 4-6 digit codes automatically.
"""

from __future__ import annotations

import json
import logging
import hmac
import secrets
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlencode

logger = logging.getLogger(__name__)

_DEFAULT_SMS_PORT = 8088


def extract_verification_code(text: str) -> str | None:
    """Extract 4 to 6 digit verification code from SMS text using smart heuristic patterns."""
    if not text:
        return None

    keyword_patterns = [
        # Keywords preceding 4-6 digits (e.g. 验证码：123456, code is 1234)
        r"(?:验证码|校验码|动态码|code|Code|CODE)[^\d]{0,10}?(\d{4,6})(?!\d)",
        # 4-6 digits preceding keywords (e.g. 123456为您的验证码)
        r"(\d{4,6})(?!\d)[^\d]{0,10}?(?:为您的验证码|是您本次|是本次验证码|为本次验证码|是您的验证码|打死不要|切勿告|千万不要)",
        # Standard 6-digit number isolation
        r"(?<!\d)(\d{6})(?!\d)",
        # Standard 4-digit number isolation
        r"(?<!\d)(\d{4})(?!\d)",
    ]

    for pattern in keyword_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1)

    return None


def get_lan_ip() -> str:
    """Find the local IPv4 address of this machine on the active LAN."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


@dataclass
class StoredCode:
    code: str
    raw_text: str
    sender: str
    received_at: datetime
    expires_at: datetime


_lock = threading.Lock()
_latest_code: StoredCode | None = None
_sms_server: ThreadingHTTPServer | None = None
_sms_server_thread: threading.Thread | None = None


def record_sms(raw_text: str, sender: str = "") -> dict[str, Any]:
    """Record an incoming SMS, extract its verification code, and store with TTL."""
    code = extract_verification_code(raw_text)
    now = datetime.now(UTC)
    stored = StoredCode(
        code=code or "",
        raw_text=raw_text,
        sender=sender,
        received_at=now,
        expires_at=now + timedelta(seconds=300),
    )
    with _lock:
        global _latest_code
        _latest_code = stored

    logger.info("SMS received; content and code redacted")
    return {
        "code": code,
        "raw_text": raw_text,
        "sender": sender,
        "received_at": now.isoformat(),
        "expires_at": stored.expires_at.isoformat(),
    }


def get_latest_code(max_age_seconds: int = 300) -> dict[str, Any] | None:
    """Return the latest unexpired verification code if available."""
    with _lock:
        if _latest_code is None:
            return None
        now = datetime.now(UTC)
        if now > _latest_code.expires_at:
            return None
        age = (now - _latest_code.received_at).total_seconds()
        if age > max_age_seconds:
            return None
        return {
            "code": _latest_code.code,
            "raw_text": _latest_code.raw_text,
            "sender": _latest_code.sender,
            "received_at": _latest_code.received_at.isoformat(),
            "age_seconds": round(age, 1),
        }


def clear_latest_code() -> None:
    """Clear stored code after usage."""
    with _lock:
        global _latest_code
        _latest_code = None


class SmsWebhookHandler(BaseHTTPRequestHandler):
    """Minimal, secure HTTP handler dedicated to receiving SMS webhooks from LAN."""

    def log_message(self, format: str, *args: Any) -> None:
        # Request targets may contain the PSK. Never log them.
        pass

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self._discard_small_rejected_body()
        self._send_json({"error": "Forbidden"}, 403)

    def do_GET(self) -> None:
        self._discard_small_rejected_body()
        self._send_json({"error": "Not Found"}, status=404)

    def _discard_small_rejected_body(self) -> None:
        # Unread POST bytes can reset a Windows connection before its 403 is
        # delivered. Discard only bounded bytes; never parse untrusted input.
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

    def do_POST(self) -> None:
        if urlsplit(self.path).path == "/api/sms/webhook":
            tokens = parse_qs(urlsplit(self.path).query).get("token", [])
            supplied = self.headers.get("X-SMS-Token") or (tokens[0] if len(tokens) == 1 else "")
            expected = getattr(self.server, "sms_webhook_token", "")
            if (self.headers.get("Origin") is not None or
                    self.headers.get("Sec-Fetch-Site", "") in {"cross-site", "same-site"} or
                    not expected or not hmac.compare_digest(supplied.encode(), expected.encode())):
                self._discard_small_rejected_body()
                self._send_json({"error": "Forbidden"}, 403)
                return
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    raise ValueError
                length = int(lengths[0])
                if not 0 < length <= 64 * 1024:
                    self._send_json({"error": "Invalid body size"}, 413)
                    return
                raw_bytes = self.rfile.read(length)
                if len(raw_bytes) != length:
                    raise ValueError
                raw_body = raw_bytes.decode("utf-8")
            except (ValueError, UnicodeError, OSError):
                self._send_json({"error": "Invalid request body"}, 400)
                return

            raw_text = ""
            sender = ""
            content_type = self.headers.get("Content-Type", "").casefold()

            if "application/json" in content_type:
                try:
                    payload = json.loads(raw_body)
                    if isinstance(payload, dict):
                        msg_val = payload.get("message") or payload.get("text") or payload.get("content")
                        if isinstance(msg_val, dict):
                            raw_text = str(msg_val.get("content") or msg_val.get("text") or msg_val.get("body") or msg_val)
                            sender = str(msg_val.get("sender") or msg_val.get("phone") or payload.get("sender") or "")
                        else:
                            raw_text = str(msg_val or "")
                            sender = str(payload.get("sender") or "")
                except (ValueError, TypeError):
                    self._send_json({"error": "Invalid JSON"}, 400)
                    return
            elif "application/x-www-form-urlencoded" in content_type:
                params = parse_qs(raw_body)
                raw_text = params.get("message", params.get("text", params.get("content", [""])))[0]
                sender = params.get("sender", [""])[0]
            else:
                raw_text = raw_body

            if not raw_text.strip():
                self._send_json({"error": "短信内容不能为空"}, status=400)
                return

            record_sms(raw_text.strip(), sender=sender.strip())
            self._send_json({"ok": True})
        else:
            self._discard_small_rejected_body()
            self._send_json({"error": "Not Found"}, status=404)


_sms_servers: list[ThreadingHTTPServer] = []
_sms_threads: list[threading.Thread] = []


def load_sms_webhook_token(private_dir: Path) -> str:
    """Persist a dedicated PSK separately from the dashboard/extension token."""
    path = Path(private_dir) / "sms-webhook-token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            token = secrets.token_urlsafe(32)
            json.dump({"sms_webhook_token": token}, handle)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except FileExistsError:
        payload = json.loads(path.read_text(encoding="utf-8"))
        token = payload.get("sms_webhook_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or len(token) < 32 or not token.isascii():
        raise ValueError("短信通信密钥文件无效，请在本机重新配置。")
    return token


def start_sms_listener(host: str = "0.0.0.0", port: int = _DEFAULT_SMS_PORT,
                       *, private_dir: Path) -> ThreadingHTTPServer:
    """Start exactly one configured port; never claim another fallback port."""
    global _sms_servers, _sms_threads

    with _lock:
        token = load_sms_webhook_token(private_dir)
        if _sms_servers:
            if _sms_servers[0].sms_webhook_token != token:
                raise RuntimeError("短信监听服务已由其他数据目录启动。")
            if port != _sms_servers[0].server_port or host != _sms_servers[0].server_address[0]:
                raise RuntimeError("短信监听服务已在其他地址或端口运行，请先停止原监听。")
            return _sms_servers[0]

        ports_to_try = [port]

        main_server = None
        for p in ports_to_try:
            try:
                server = ThreadingHTTPServer((host, p), SmsWebhookHandler)
                server.sms_webhook_token = token
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                _sms_servers.append(server)
                _sms_threads.append(thread)
                if main_server is None:
                    main_server = server
                logger.info("SMS Webhook listener started at http://%s:%d/api/sms/webhook", host, p)
            except OSError as exc:
                logger.warning("Could not start SMS listener on port %d: %s", p, exc)

        if not main_server:
            raise RuntimeError(f"无法监听短信端口 {port}，端口可能已被占用；未尝试其他端口。")
        return main_server


def stop_sms_listener() -> None:
    """Stop all SMS webhook listeners."""
    global _sms_servers, _sms_threads

    with _lock:
        for server in _sms_servers:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        _sms_servers.clear()
        _sms_threads.clear()
        logger.info("All SMS Webhook listeners stopped")


def get_sms_service_status(port: int = _DEFAULT_SMS_PORT) -> dict[str, Any]:
    """Public-safe metadata only. Sensitive setup requires dashboard auth."""
    lan_ip = get_lan_ip()
    is_running = len(_sms_servers) > 0

    return {
        "ok": True,
        "is_listening": is_running,
        "lan_ip": lan_ip,
        "port": _sms_servers[0].server_port if _sms_servers else port,
    }


def get_sms_setup(private_dir: Path) -> dict[str, Any]:
    """For authenticated local dashboard POST only; never expose from LAN GET."""
    status = get_sms_service_status()
    token = load_sms_webhook_token(private_dir)
    if _sms_servers and _sms_servers[0].sms_webhook_token != token:
        raise RuntimeError("短信监听服务的数据目录不匹配。")
    status["webhook_url"] = (
        f"http://{status['lan_ip']}:{status['port']}/api/sms/webhook?"
        + urlencode({"token": token})
    )
    status["latest_code"] = get_latest_code()
    return status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    lan = get_lan_ip()
    print(f"正在启动 iPhone 短信验证码同步监听服务 (本机局域网 IP: {lan})...")
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", type=Path, required=True)
    args = parser.parse_args()
    start_sms_listener(port=8088, private_dir=args.private_dir)
    print(f"✅ Webhook 服务已就绪！")
    print(f"   - 端口 8088: http://{lan}:8088/api/sms/webhook")
    import time
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_sms_listener()
        print("服务已安全退出。")
