"""Only synthetic loopback HTTP; no browser permissions or real user data."""
import json
import unittest
import urllib.error
import urllib.request
from email.message import Message
from unittest.mock import Mock

from tests import test_sensitive_routes as fixtures


class HttpOriginBoundaryTests(unittest.TestCase):
    setUp = fixtures.SensitiveRoutesTests.setUp
    tearDown = fixtures.SensitiveRoutesTests.tearDown
    request = fixtures.SensitiveRoutesTests.request

    def call(self, path, headers, payload=None):
        request = urllib.request.Request(self.base + path, headers={
            'Content-Type': 'application/json', **headers},
            data=json.dumps(payload).encode() if payload is not None else None)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read()

    def test_external_origins_cannot_read_bootstrap_or_private_resources(self):
        for origin in ['https://evil.example', 'null', 'chrome-extension://' + 'a' * 32,
                       'chrome-extension://invalid', 'moz-extension://synthetic']:
            for path in ['/', '/api/dashboard', '/api/profile', '/api/jobs/1/resume-content', '/resume-draft/1/pdf']:
                with self.subTest(origin=origin, path=path):
                    status, body = self.call(path, {'Origin': origin,
                        'X-Agent-Token': self.server.RequestHandlerClass.agent_token,
                        'X-Job-Agent-Token': self.token})
                    self.assertEqual(status, 403)
                    self.assertNotIn(self.token.encode(), body)

    def test_cross_site_without_origin_cannot_bootstrap(self):
        for path in ['/', '/api/dashboard', '/api/profile']:
            self.assertEqual(self.call(path, {'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_local_clients_and_same_origin_ui_remain_compatible(self):
        for headers in [{}, {'Origin': self.base, 'Sec-Fetch-Site': 'same-origin'},
                        {'Sec-Fetch-Site': 'none'}]:
            self.assertEqual(self.call('/api/dashboard', headers)[0], 200)

    def test_valid_agent_is_limited_to_dedicated_routes(self):
        headers = {'Origin': 'chrome-extension://' + 'a' * 32,
                   'Sec-Fetch-Site': 'cross-site',
                   'X-Agent-Token': self.server.RequestHandlerClass.agent_token,
                   'X-Profile-Context': self.server.RequestHandlerClass.profile_context}
        self.assertEqual(self.call('/api/agent/state', headers)[0], 200)
        self.assertEqual(self.call('/api/fill/poll', headers, {})[0], 400)
        self.assertEqual(self.call('/api/jobs/import-parsed', headers, {})[0], 400)
        for path in ['/api/settings', '/api/agent/token']:
            self.assertEqual(self.call(path, {**headers, 'X-Job-Agent-Token': self.token}, {})[0], 403)
        self.assertEqual(self.call('/api/agent/state', {**headers, 'X-Agent-Token': 'bad'})[0], 403)
        self.assertEqual(self.call('/api/agent/state', {**headers, 'Origin': 'https://evil.example'})[0], 403)

    def test_action_token_does_not_authorize_extension_origin(self):
        self.assertEqual(self.call('/api/settings', {
            'Origin': 'chrome-extension://' + 'a' * 32, 'X-Job-Agent-Token': self.token}, {})[0], 403)

    def test_rejected_body_drain_is_bounded_and_never_parses(self):
        handler_class = self.server.RequestHandlerClass
        for length in ['-1', '8193', 'invalid', '2']:
            handler = object.__new__(handler_class)
            handler.headers = Message()
            handler.headers['Content-Length'] = length
            handler.rfile = Mock()
            handler.rfile.read1.return_value = b'{}'
            handler.connection = Mock()
            handler.connection.gettimeout.return_value = 10
            handler._discard_small_rejected_body()
            if length == '2':
                handler.rfile.read1.assert_called_once_with(2)
                self.assertLessEqual(handler.connection.settimeout.call_args_list[0].args[0], 0.2)
                self.assertEqual(handler.connection.settimeout.call_args_list[-1].args, (10,))
            else:
                handler.rfile.read1.assert_not_called()

    def test_duplicate_origin_headers_are_rejected(self):
        handler = object.__new__(self.server.RequestHandlerClass)
        handler.headers = Message()
        handler.headers['Origin'] = self.base
        handler.headers['Origin'] = 'https://evil.example'
        self.assertFalse(handler._request_source_allowed('GET', '/api/dashboard'))

    def test_oversized_body_without_route_error_handler_is_a_client_error(self):
        status, body = self.call(
            '/api/jobs/1/reveal-resume',
            {'X-Job-Agent-Token': self.token},
            {'padding': 'x' * 17000},
        )
        self.assertEqual(status, 400)
        self.assertIn('请求内容过大', body.decode('utf-8'))
