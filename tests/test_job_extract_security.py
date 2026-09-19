import json
import unittest
from http.client import HTTPException
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from job_agent.services.dashboard_routes.jobs_api import handle_extract_jd
from job_agent.services.runtime_config import RuntimeConfig


class JobExtractSecurityTests(unittest.TestCase):
    def handler(self, raw_text):
        handler = MagicMock()
        handler._authorized_action.return_value = True
        handler.headers = {'Content-Type': 'application/json'}
        handler._read_body.return_value = json.dumps({'raw_text': raw_text}).encode()
        handler.runtime_config_path = Path('synthetic-runtime.json')
        return handler

    @patch('job_agent.services.ai_provider.get_openai_client')
    @patch('job_agent.services.safe_job_fetch.fetch_public_html')
    def test_short_or_empty_html_preserves_input_without_ai(self, fetch, helper):
        for html in ['', '<p>' + 'x' * 59 + '</p>', '<script>' + 'x' * 100 + '</script><div>loading</div>']:
            with self.subTest(length=len(html)):
                fetch.return_value = html
                handler = self.handler('https://example.com/job')
                handle_extract_jd(handler)
                result = handler._json.call_args.args[0]
                self.assertFalse(result['ok'])
                self.assertTrue(result['preserve_input'])
                self.assertIn('复制岗位文字', result['warning'])
        helper.assert_not_called()

    @patch('job_agent.services.ai_provider.get_openai_client')
    @patch('job_agent.services.safe_job_fetch.fetch_public_html')
    def test_blocked_or_failed_fetch_does_not_echo_sensitive_details(self, fetch, helper):
        for error in [ValueError('blocked private-host synthetic-secret'), HTTPException('transport synthetic-secret'), OSError('network synthetic-secret')]:
            with self.subTest(error=type(error).__name__):
                fetch.side_effect = error
                handler = self.handler('http://127.0.0.1/private?token=synthetic-secret')
                handle_extract_jd(handler)
                result = handler._json.call_args.args[0]
                self.assertFalse(result['ok'])
                self.assertTrue(result['preserve_input'])
                self.assertNotIn('synthetic-secret', json.dumps(result))
                self.assertNotIn('127.0.0.1', json.dumps(result))
        helper.assert_not_called()

    @patch('job_agent.services.dashboard_routes.jobs_api.effective_runtime_config')
    @patch('job_agent.services.safe_job_fetch.fetch_public_html')
    def test_valid_fetched_text_retains_source_in_local_fallback(self, fetch, configuration):
        text = '示例公司招聘数据实习岗位，负责整理合成数据与报表。' * 4
        fetch.return_value = '<main>' + text + '</main>'
        configuration.return_value = (RuntimeConfig(), None)
        handler = self.handler('https://example.com/job')
        handle_extract_jd(handler)
        result = handler._json.call_args.args[0]
        self.assertTrue(result['ok'])
        self.assertEqual(result['parsed']['source_url'], 'https://example.com/job')
        self.assertEqual(result['parsed']['jd_text'], text)

    @patch('job_agent.services.ai_provider.get_openai_client')
    @patch('job_agent.services.dashboard_routes.jobs_api.effective_runtime_config')
    @patch('job_agent.services.safe_job_fetch.fetch_public_html')
    def test_valid_text_uses_shared_client_and_preserves_actual_source(self, fetch, configuration, helper):
        fetch.return_value = '<main>' + '岗位职责与要求' * 15 + '</main>'
        config = SimpleNamespace(ai=SimpleNamespace(provider='openai'))
        configuration.return_value = (config, None)
        client = MagicMock()
        client.chat.completions.create.return_value.choices = [SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            'company': '示例公司', 'title': '合成岗位', 'jd_text': '合成要求', 'source_url': 'https://example.com/wrong'
        })))]
        helper.return_value = (client, 'configured-model')
        handler = self.handler('https://example.com/job')
        handle_extract_jd(handler)
        helper.assert_called_once_with(config)
        self.assertEqual(client.chat.completions.create.call_args.kwargs['model'], 'configured-model')
        result = handler._json.call_args.args[0]
        self.assertTrue(result['ok'])
        self.assertEqual(result['parsed']['source_url'], 'https://example.com/job')

    @patch('job_agent.services.safe_job_fetch.fetch_public_html')
    def test_unauthorized_requests_never_fetch(self, fetch):
        handler = self.handler('https://example.com/job')
        handler._authorized_action.return_value = False
        handle_extract_jd(handler)
        handler._reject_unauthorized_action.assert_called_once()
        fetch.assert_not_called()
