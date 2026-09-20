import json
import http.client
import unittest
import urllib.request
import urllib.error
import tempfile
import socket
from pathlib import Path
from email.message import Message
from unittest.mock import MagicMock

from job_agent.services.sms_sync import (
    clear_latest_code,
    extract_verification_code,
    get_lan_ip,
    get_latest_code,
    get_sms_service_status,
    get_sms_setup,
    load_sms_webhook_token,
    record_sms,
    start_sms_listener,
    stop_sms_listener,
    SmsWebhookHandler,
)


class SmsSyncTests(unittest.TestCase):
    def test_rejected_request_body_discard_is_bounded(self):
        for length in ['2', '8193', '-1', 'invalid']:
            handler = object.__new__(SmsWebhookHandler)
            handler.headers = Message()
            handler.headers['Content-Length'] = length
            handler.rfile = MagicMock()
            handler.rfile.read1.return_value = b'{}'
            handler.connection = MagicMock()
            handler.connection.gettimeout.return_value = 5
            handler._discard_small_rejected_body()
            if length == '2':
                handler.rfile.read1.assert_called_once_with(2)
                self.assertLessEqual(handler.connection.settimeout.call_args_list[0].args[0], 0.2)
                self.assertEqual(handler.connection.settimeout.call_args_list[-1].args, (5,))
            else:
                handler.rfile.read1.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private_dir = Path(self.temp.name)

    def tearDown(self):
        clear_latest_code()
        stop_sms_listener()
        self.temp.cleanup()

    def test_extract_verification_code_various_formats(self):
        cases = [
            ("【百度校招】您的验证码是 492015，5分钟内有效。", "492015"),
            ("【美团】动态验证码：849201。工作人员不会向您索取。", "849201"),
            ("您正在登录Boss直聘，验证码为8492，打死都不要告诉别人哦！", "8492"),
            ("【网易招聘】392015 是您本次投递确认的验证码，请勿泄露。", "392015"),
            ("您的手机尾号2388正在进行实名认证，验证码 729104 (5分钟内有效)", "729104"),
            ("Your verification code is 918234 for sign-in.", "918234"),
            ("没有数字的消息内容", None),
            ("", None),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                code = extract_verification_code(text)
                self.assertEqual(code, expected)

    def test_record_and_get_latest_code(self):
        res = record_sms("验证码 123456", sender="10690000")
        self.assertEqual(res["code"], "123456")

        latest = get_latest_code(max_age_seconds=60)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["code"], "123456")
        self.assertEqual(latest["sender"], "10690000")

        clear_latest_code()
        self.assertIsNone(get_latest_code())

    def test_get_lan_ip_returns_valid_format(self):
        ip = get_lan_ip()
        parts = ip.split(".")
        self.assertEqual(len(parts), 4)

    def test_sms_webhook_server_post_and_get(self):
        server = start_sms_listener(host="127.0.0.1", port=0, private_dir=self.private_dir)
        port = server.server_port
        status = get_sms_service_status(port=port)
        self.assertTrue(status["is_listening"])

        # Test POST
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/sms/webhook",
            data=json.dumps({"message": "【腾讯科技】面试验证码：582019"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-SMS-Token": load_sms_webhook_token(self.private_dir)},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertNotIn("data", data)

        # Test in-memory update
        latest = get_latest_code()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["code"], "582019")

    def test_dashboard_routes_sms(self):
        from job_agent.services.dashboard_routes.jobs_api import (
            handle_sms_clear,
            handle_sms_manual,
            handle_sms_status,
        )

        handler = MagicMock()
        handler._authorized_action.return_value = True

        # Test Status
        handle_sms_status(handler)
        handler._json.assert_called()
        status_data = handler._json.call_args[0][0]
        self.assertTrue(status_data["ok"])
        self.assertNotIn("webhook_url", status_data)
        self.assertNotIn("latest_code", status_data)

        # Test Manual Input
        handler._json.reset_mock()
        handler.headers = {"Content-Type": "application/json"}
        handler._read_body.return_value = b'{"code": "999888"}'
        from job_agent.services.fill_bridge import FillBridge
        handler.server.fill_bridge = FillBridge()

        handle_sms_manual(handler)
        handler._json.assert_called_once()
        manual_data = handler._json.call_args[0][0]
        self.assertTrue(manual_data["ok"])
        self.assertEqual(manual_data, {"ok": True, "duplicate": False, "queued": False})
        self.assertNotIn("data", manual_data)

        # Test Clear
        handler._json.reset_mock()
        handle_sms_clear(handler)
        handler._json.assert_called_once()
        clear_data = handler._json.call_args[0][0]
        self.assertTrue(clear_data["ok"])
        self.assertIsNone(get_latest_code())

    def test_webhook_rejects_cross_site_and_unauthenticated_requests(self):
        server = start_sms_listener(host="127.0.0.1", port=0, private_dir=self.private_dir)
        base = f"http://127.0.0.1:{server.server_port}"
        token = load_sms_webhook_token(self.private_dir)
        for path, method, headers, code in [
            ("/api/sms/latest", "GET", {}, 404),
            ("/api/sms/webhook", "POST", {}, 403),
            ("/api/sms/webhook", "POST", {"X-SMS-Token": "wrong"}, 403),
            ("/api/sms/webhook", "POST", {"X-SMS-Token": token, "Origin": "https://evil.example"}, 403),
            ("/api/sms/webhook", "POST", {"X-SMS-Token": token, "Origin": "null"}, 403),
            ("/api/sms/webhook", "OPTIONS", {}, 403),
            ("/api/sms/webhook-extra", "POST", {"X-SMS-Token": token}, 404),
        ]:
            with self.subTest(path=path, method=method, expected=code):
                req = urllib.request.Request(base + path, data=b"test" if method == "POST" else None,
                                             method=method, headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(caught.exception.code, code)
                self.assertIsNone(caught.exception.headers.get("Access-Control-Allow-Origin"))
        self.assertIsNone(get_latest_code())

    def test_query_token_persistence_and_safe_status(self):
        token = load_sms_webhook_token(self.private_dir)
        self.assertEqual(token, load_sms_webhook_token(self.private_dir))
        server = start_sms_listener(host="127.0.0.1", port=0, private_dir=self.private_dir)
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/sms/webhook?token={token}",
                                     data=b"code 654321", headers={"Content-Type": "text/plain"})
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
        status = get_sms_service_status()
        self.assertNotIn(token, json.dumps(status))
        self.assertNotIn("latest_code", status)
        self.assertIn(token, get_sms_setup(self.private_dir)["webhook_url"])

    def test_invalid_json_and_oversized_body_are_rejected(self):
        server = start_sms_listener(host="127.0.0.1", port=0, private_dir=self.private_dir)
        token = load_sms_webhook_token(self.private_dir)
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/sms/webhook",
            data=b"{", headers={"Content-Type": "application/json", "X-SMS-Token": token})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        # The size limit must reject headers immediately, before consuming any
        # payload. Sending an unread large body races Windows TCP reset handling.
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.putrequest("POST", "/api/sms/webhook")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("X-SMS-Token", token)
            connection.putheader("Content-Length", "65537")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            response.read()
        finally:
            connection.close()
        self.assertIsNone(get_latest_code())

    def test_busy_port_never_falls_back(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with self.assertRaisesRegex(RuntimeError, "未尝试其他端口"):
                start_sms_listener(host="127.0.0.1", port=occupied.getsockname()[1], private_dir=self.private_dir)
        self.assertFalse(get_sms_service_status()["is_listening"])

    def test_sms_logs_never_contain_sender_or_code(self):
        with self.assertLogs("job_agent.services.sms_sync", level="INFO") as logged:
            record_sms("Synthetic code 654321", sender="synthetic-sender")
        output = " ".join(logged.output)
        self.assertNotIn("654321", output)
        self.assertNotIn("synthetic-sender", output)

    def test_stop_removes_only_requested_listener(self):
        server = start_sms_listener(host="127.0.0.1", port=0, private_dir=self.private_dir)
        self.assertEqual(get_sms_service_status()["port"], server.server_port)
        stop_sms_listener()
        self.assertFalse(get_sms_service_status()["is_listening"])


if __name__ == "__main__":
    unittest.main()
