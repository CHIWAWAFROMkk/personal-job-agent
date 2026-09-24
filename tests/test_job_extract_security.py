import json
import tempfile
import unittest
from http.client import HTTPException
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from job_agent.services.dashboard_routes.jobs_api import handle_extract_jd
from job_agent.services.api_usage import load_api_usage
from job_agent.services.runtime_config import AIConnectorConfig, RuntimeConfig


class JobExtractSecurityTests(unittest.TestCase):
    @patch('job_agent.services.dashboard_routes.jobs_api.effective_runtime_config')
    def test_local_inline_labels_remain_separate(self, configuration):
        configuration.return_value = (RuntimeConfig(), None)
        text = '仅用于验收的虚构岗位。公司：演示数据工作室；岗位：数据运营实习生；地点：上海。\n任职要求：熟练使用 SQL。'
        handler = self.handler(text)
        handle_extract_jd(handler)
        parsed = handler._json.call_args.args[0]['parsed']
        self.assertEqual((parsed['company'], parsed['title'], parsed['location']),
                         ('演示数据工作室', '数据运营实习生', '上海'))
        self.assertEqual(parsed['jd_text'], text)

    @patch('job_agent.services.dashboard_routes.jobs_api.effective_runtime_config')
    def test_local_unlabeled_paragraph_does_not_invent_company_or_title(self, configuration):
        configuration.return_value = (RuntimeConfig(), None)
        handler = self.handler('我们是一家科技公司，岗位负责产品运营，要求每周至少三天。')
        handle_extract_jd(handler)
        parsed = handler._json.call_args.args[0]['parsed']
        self.assertEqual((parsed['company'], parsed['title']), ('未知公司', '未命名岗位'))

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
        config = RuntimeConfig(ai=AIConnectorConfig(
            provider='openai', model='configured-model', api_key='synthetic-only-key', monthly_quota=1,
        ))
        configuration.return_value = (config, None)
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.return_value.choices = [SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            'company': '示例公司', 'title': '合成岗位', 'jd_text': '合成要求', 'source_url': 'https://example.com/wrong'
        })))]
        helper.return_value = (client, 'configured-model')
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = self.handler('https://example.com/job')
            handler.api_usage_path = Path(temp_dir) / 'api-usage.json'
            handle_extract_jd(handler)
            self.assertEqual(load_api_usage(handler.api_usage_path).ai.successful_requests, 1)
        helper.assert_called_once_with(config)
        self.assertEqual(client.chat.completions.create.call_args.kwargs['model'], 'configured-model')
        result = handler._json.call_args.args[0]
        self.assertTrue(result['ok'])
        self.assertEqual(result['parsed']['source_url'], 'https://example.com/job')

    @patch('job_agent.services.ai_provider.get_openai_client')
    @patch('job_agent.services.dashboard_routes.jobs_api.effective_runtime_config')
    def test_invalid_cloud_extraction_still_counts_successful_sdk_response(self, configuration, helper):
        config = RuntimeConfig(ai=AIConnectorConfig(
            provider='openai', model='configured-model', api_key='synthetic-only-key', monthly_quota=1,
        ))
        configuration.return_value = (config, None)
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"title":""}'))],
            usage=SimpleNamespace(prompt_tokens=9, completion_tokens=4),
        )
        helper.return_value = (client, 'configured-model')
        with tempfile.TemporaryDirectory() as temp_dir:
            usage_path = Path(temp_dir) / 'api-usage.json'
            for _ in range(2):
                handler = self.handler('合成公司招聘数据分析实习生，岗位职责包括清洗样本、编写查询和制作报表。' * 2)
                handler.api_usage_path = usage_path
                handle_extract_jd(handler)
                self.assertTrue(handler._json.call_args.args[0]['ok'])
            self.assertEqual(client.chat.completions.create.call_count, 1)
            counter = load_api_usage(usage_path).ai
            self.assertEqual((counter.successful_requests, counter.input_tokens, counter.output_tokens), (1, 9, 4))

    @patch('job_agent.services.safe_job_fetch.fetch_public_html')
    def test_unauthorized_requests_never_fetch(self, fetch):
        handler = self.handler('https://example.com/job')
        handler._authorized_action.return_value = False
        handle_extract_jd(handler)
        handler._reject_unauthorized_action.assert_called_once()
        fetch.assert_not_called()
