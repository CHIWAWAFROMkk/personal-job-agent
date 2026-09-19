"""Synthetic SSRF regression tests: no external network requests."""
import http.client
import queue
import socket
import unittest
import threading
import time
from unittest.mock import MagicMock, patch

from job_agent.services.safe_job_fetch import UnsafeJobURL, fetch_public_html, resolve_public


def address(ip, port=443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    target = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", target)


def response(status=200, chunks=None, headers=None):
    value = MagicMock()
    value.status = status
    headers = headers or {}
    value.getheader.side_effect = lambda key, default=None: headers.get(key, default)
    value.read1.side_effect = chunks if chunks is not None else [b"<p>synthetic JD</p>", b""]
    return value


class SafeJobFetchTests(unittest.TestCase):
    def test_watchdog_eof_is_not_successful_partial_content(self):
        def expired_eof(*_):
            time.sleep(0.12)
            return b''
        with patch('socket.getaddrinfo', return_value=[address('93.184.216.34', 80)]), patch('socket.socket'), patch('http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value.read1.side_effect = expired_eof
            connection.return_value.getresponse.return_value.status = 200
            connection.return_value.getresponse.return_value.getheader.return_value = 'identity'
            with self.assertRaises(TimeoutError):
                fetch_public_html('http://jobs.example/', timeout=0.1)

    def test_translation_prefixes_cannot_embed_private_ipv4(self):
        for ip in ('64:ff9b::7f00:1', '64:ff9b::a00:1', '2002:7f00:1::'):
            with self.subTest(ip=ip), patch('socket.getaddrinfo', return_value=[address(ip)]):
                with self.assertRaises(UnsafeJobURL):
                    fetch_public_html('http://jobs.example/')

    def test_absolute_deadline_interrupts_slow_response_headers(self):
        stopped = threading.Event()
        def slow_headers():
            self.assertTrue(stopped.wait(1), 'Absolute deadline did not close socket')
            raise OSError('synthetic cancelled read')
        with patch('socket.getaddrinfo', return_value=[address('93.184.216.34', 80)]), patch('socket.socket') as sockets, patch('http.client.HTTPConnection') as connection:
            sockets.return_value.shutdown.side_effect = lambda *_: stopped.set()
            connection.return_value.getresponse.side_effect = slow_headers
            with self.assertRaises(OSError):
                fetch_public_html('http://jobs.example/', timeout=0.1)
            self.assertTrue(stopped.is_set())
    def test_rejects_private_reserved_and_mixed_dns(self):
        for ips in [["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"],
                    ["0.0.0.0"], ["224.0.0.1"], ["192.168.1.1"],
                    ["93.184.216.34", "127.0.0.1"]]:
            with self.subTest(ips=ips), patch("socket.getaddrinfo", return_value=[address(ip) for ip in ips]):
                with self.assertRaises(UnsafeJobURL):
                    resolve_public("https://jobs.example")

    def test_rejects_unsafe_ipv6(self):
        for ip in ["::1", "::", "fe80::1", "fd00::1", "ff02::1", "::ffff:93.184.216.34"]:
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=[address(ip)]):
                with self.assertRaises(UnsafeJobURL):
                    resolve_public("https://jobs.example")

    def test_accepts_global_ipv6(self):
        resolved = address("2606:4700:4700::1111")
        with patch("socket.getaddrinfo", return_value=[resolved]):
            self.assertEqual(resolve_public("https://jobs.example")[3], resolved)

    def test_rejects_credentials_localhost_and_non_http(self):
        urls = ["https://name:password@example.com/", "https://name@example.com/",
                "https://localhost./", "http://localhost:1234/", "file:///private",
                "ftp://jobs.example/", "http://jobs.example/\nheader", "http://jobs.example\\private",
                "http://[fe80::1%25eth0]/", "https://jobs.example:bad/"]
        with patch("socket.getaddrinfo") as resolver:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(UnsafeJobURL):
                    resolve_public(url)
            resolver.assert_not_called()

    def test_dns_failure_is_actionable(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror()):
            with self.assertRaisesRegex(UnsafeJobURL, "无法解析"):
                resolve_public("https://jobs.example")

    def test_dns_wait_is_bounded(self):
        with patch("socket.getaddrinfo", return_value=[]), patch("queue.Queue") as pending:
            pending.return_value.get.side_effect = queue.Empty
            with self.assertRaisesRegex(UnsafeJobURL, "解析超时"):
                resolve_public("https://jobs.example", timeout=0.25)
            pending.return_value.get.assert_called_once_with(timeout=0.25)

    def test_connects_to_pinned_numeric_address_and_preserves_host(self):
        pinned = address("93.184.216.34")
        with patch("socket.getaddrinfo", return_value=[pinned]) as dns, \
             patch("socket.socket") as socket_factory, \
             patch("ssl.create_default_context") as tls, \
             patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = response()
            self.assertIn("synthetic JD", fetch_public_html("https://jobs.example/list?q=test#ignored"))
            self.assertEqual(dns.call_count, 1)
            socket_factory.return_value.connect.assert_called_once_with(pinned[4])
            tls.return_value.wrap_socket.assert_called_once_with(socket_factory.return_value, server_hostname="jobs.example")
            self.assertEqual(connection.call_args.args[:2], ("jobs.example", 443))
            self.assertEqual(connection.return_value.request.call_args.args, ("GET", "/list?q=test"))
            connection.return_value.close.assert_called_once()

    def test_redirect_to_private_destination_is_blocked_before_second_connect(self):
        with patch("socket.getaddrinfo", side_effect=[[address("93.184.216.34", 80)], [address("127.0.0.1", 80)]]), \
             patch("socket.socket") as socket_factory, \
             patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = response(302, headers={"Location": "http://internal.example/secret"})
            with self.assertRaises(UnsafeJobURL):
                fetch_public_html("http://jobs.example/")
            self.assertEqual(socket_factory.return_value.connect.call_count, 1)

    def test_same_host_redirect_revalidates_dns(self):
        with patch("socket.getaddrinfo", side_effect=[[address("93.184.216.34", 80)], [address("10.0.0.1", 80)]]) as dns, \
             patch("socket.socket"), patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = response(302, headers={"Location": "/new"})
            with self.assertRaises(UnsafeJobURL):
                fetch_public_html("http://jobs.example/")
            self.assertEqual(dns.call_count, 2)

    def test_redirect_hops_bounded(self):
        with patch("socket.getaddrinfo", return_value=[address("93.184.216.34", 80)]), \
             patch("socket.socket"), patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = response(302, headers={"Location": "/again"})
            with self.assertRaisesRegex(UnsafeJobURL, "重定向"):
                fetch_public_html("http://jobs.example/")
            self.assertEqual(connection.call_count, 4)

    def test_body_bound_and_encoding_and_http_status(self):
        for reply in [response(chunks=[b"12345"]), response(headers={"Content-Encoding": "gzip"}), response(403)]:
            with self.subTest(reply=reply), patch("socket.getaddrinfo", return_value=[address("93.184.216.34", 80)]), \
                 patch("socket.socket") as socket_factory, patch("http.client.HTTPConnection") as connection:
                connection.return_value.getresponse.return_value = reply
                with self.assertRaises(ValueError):
                    fetch_public_html("http://jobs.example", max_bytes=4)
                socket_factory.return_value.close.assert_called_once()

    def test_timeout_before_connect(self):
        with patch("socket.getaddrinfo") as dns:
            with self.assertRaises(TimeoutError):
                fetch_public_html("http://jobs.example", timeout=-1)
            dns.assert_not_called()

    def test_http_protocol_errors_propagate_and_socket_closes(self):
        with patch("socket.getaddrinfo", return_value=[address("93.184.216.34", 80)]), \
             patch("socket.socket") as socket_factory, patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.side_effect = http.client.BadStatusLine("invalid synthetic status")
            with self.assertRaises(http.client.HTTPException):
                fetch_public_html("http://jobs.example")
            socket_factory.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
