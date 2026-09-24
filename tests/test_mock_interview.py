import json
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from job_agent.services.mock_interview import MockInterviewStore, InterviewConflict, teleprompter
from job_agent.services.runtime_config import RuntimeConfig
from tests.helpers import sample_profile


class MockInterviewTests(unittest.TestCase):
    def test_http_route_passes_shared_usage_file_to_store(self):
        from job_agent.services.dashboard_routes.mock_interview_api import _run

        response = Mock()
        usage_path = Path(self.temp.name) / 'separate-private' / 'api-usage.json'
        handler = SimpleNamespace(
            repository=SimpleNamespace(path=self.store.path),
            api_usage_path=usage_path,
            _authorized_action=lambda: True,
            _json=response,
        )
        _run(handler, lambda store: {'usage_path': str(store.usage_path)})
        response.assert_called_once_with({'usage_path': str(usage_path)})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MockInterviewStore(Path(self.temp.name) / 'jobs.sqlite3')
        self.job = SimpleNamespace(job_id=1, company='合成企业', title='数据岗位', jd_text='使用 SQL 完成数据分析')
        self.profile = sample_profile()
        self.config = RuntimeConfig()

    def tearDown(self):
        self.temp.cleanup()

    def start(self, **extra):
        return self.store.start({'job_id': 1, 'request_id': 'start', **extra}, self.job, self.profile, self.config)

    def reply(self, session, answer='当时负责分析任务，我使用 SQL 检查数据，最终交付结果。', **extra):
        return self.store.reply({'session_id': session['session_id'], 'expected_revision': session['revision'], 'request_id': 'reply-' + str(session['revision']), 'answer': answer, **extra}, self.config)

    def test_local_no_network_and_confirmed_only(self):
        with patch('job_agent.services.mock_interview.get_openai_client', side_effect=AssertionError('network')):
            s = self.start()
            self.assertEqual(len(s['questions']), 5)
            text = json.dumps(s, ensure_ascii=False)
            self.assertNotIn('Tableau', text)
            self.assertNotIn('private@example.com', text)
            self.assertEqual(s['evidence'][0]['id'], 'fact-sql-analysis')
            self.assertEqual(self.store.get(s['session_id']), s)

    def test_idempotency_and_conflict(self):
        s = self.start()
        self.assertEqual(self.start()['session_id'], s['session_id'])
        r = self.reply(s)
        self.assertEqual(self.reply(s), r)
        with self.assertRaises(InterviewConflict):
            self.reply(s, '不同回答')
        with self.assertRaises(InterviewConflict):
            self.reply(s, request_id='fresh-stale')
        self.assertEqual(self.store.get(s['session_id'])['turn_count'], 1)

    def test_five_rounds_score_and_no_answer_promotion(self):
        s = self.start()
        with self.assertRaises(ValueError):
            self.store.score({'session_id': s['session_id'], 'expected_revision': 0})
        for _ in range(5):
            s = self.reply(s, '我提升了业务指标9999%，这是尚未核验的回答。')
        self.assertEqual(s['status'], 'ready_to_score')
        with self.assertRaises(InterviewConflict):
            self.reply(s)
        result = self.store.score({'session_id': s['session_id'], 'expected_revision': 5})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([r['question'] for r in result['score']['rewrites']], [q['text'] for q in s['questions']])
        self.assertEqual(result['score']['rewrites'][0]['fact_ids'], [])
        self.assertEqual(result['score']['rewrites'][1]['fact_ids'], ['fact-sql-analysis'])
        self.assertIn('统计范围', result['score']['rewrites'][3]['script'])
        self.assertIn('未来计划', result['score']['rewrites'][4]['script'])
        self.assertTrue(result['score']['warnings'])
        self.assertNotIn('9999', json.dumps(result['score']['rewrites']))
        self.assertEqual(self.store.score({'session_id': s['session_id']}), result)
        self.assertEqual(len(self.store.list(1)), 1)

    def test_validation_and_rollback(self):
        s = self.start()
        for value in ('', 'x' * 6001, None, 123):
            with self.assertRaises(ValueError):
                self.reply(s, value)
        with patch('job_agent.services.mock_interview._feedback', side_effect=RuntimeError('fail')):
            with self.assertRaises(RuntimeError):
                self.reply(s)
        self.assertEqual(self.store.get(s['session_id'])['turn_count'], 0)
        self.assertEqual(self.reply(s)['turn_count'], 1)

    def test_cloud_consent_safe_fallback_and_config_change(self):
        with self.assertRaises(ValueError):
            self.start(engine='cloud')
        with patch('job_agent.services.mock_interview.get_openai_client', side_effect=RuntimeError('secret-key')):
            s = self.start(engine='cloud', cloud_consent=True)
        self.assertNotIn('secret-key', str(s))
        self.assertIn('本地规则', s['notice'])
        self.config.ai.provider = 'deepseek'
        with patch('job_agent.services.mock_interview.get_openai_client', side_effect=AssertionError('must not send')):
            s = self.reply(s)
        self.assertIn('配置已变更', s['notice'])

    def test_cloud_cannot_inject_prose(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"id":"invented employed at company"}'))]))))
        with patch('job_agent.services.mock_interview.get_openai_client', return_value=(client, 'test-model')):
            s = self.start(engine='cloud', cloud_consent=True)
        self.assertNotIn('invented', str(s))
        self.assertEqual(s['questions'][0]['id'], 'motivation')

    def test_cards_no_invented_metrics_empty_facts(self):
        self.profile.skills = []
        self.profile.experiences = []
        cards = teleprompter(self.job, self.profile)
        self.assertEqual(cards['metrics'], [])
        self.assertEqual(cards['facts'], [])
        self.assertIn('待补充', str(cards['star']))

    def test_concurrent_replies_commit_only_once(self):
        s = self.start()
        barrier = threading.Barrier(2)
        def feedback(*args):
            barrier.wait(timeout=5)
            return 'detail', '补充细节'
        def attempt(key):
            try:
                return self.reply(s, request_id=key)['turn_count']
            except InterviewConflict:
                return 'conflict'
        with patch('job_agent.services.mock_interview._feedback', side_effect=feedback):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, ['thread-a', 'thread-b']))
        self.assertCountEqual(results, [1, 'conflict'])
        self.assertEqual(self.store.get(s['session_id'])['turn_count'], 1)

    def test_cloud_minimizes_contacts_and_holds_no_writer_lock(self):
        captured = []
        def create(**kw):
            captured.append(str(kw['messages']))
            # A concurrent writer can finish while cloud selection is pending.
            import sqlite3
            db = sqlite3.connect(self.store.path, timeout=0.1)
            db.execute('CREATE TABLE IF NOT EXISTS synthetic_writer(value TEXT)')
            db.commit()
            db.close()
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"id":"motivation"}'))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        self.job.jd_text += ' private@example.com 123456 测试用户'
        with patch('job_agent.services.mock_interview.get_openai_client', return_value=(client, 'test-model')):
            s = self.start(engine='cloud', cloud_consent=True)
        self.assertEqual(s['notice'], '')
        self.assertNotIn('private@example.com', captured[0])
        self.assertNotIn('123456', captured[0])
        self.assertNotIn('测试用户', captured[0])

    def test_cloud_selection_obeys_monthly_quota_and_releases_failed_call(self):
        from job_agent.services.api_usage import load_api_usage

        self.config.ai.provider = 'openai'
        self.config.ai.monthly_quota = 1
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kw: SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"id":"motivation"}'),
            )]),
        )))
        with patch('job_agent.services.mock_interview.get_openai_client', side_effect=RuntimeError('offline')):
            failed = self.start(engine='cloud', cloud_consent=True)
        self.assertIn('本地规则', failed['notice'])
        self.assertEqual(load_api_usage(self.store.usage_path).ai.successful_requests, 0)

        with patch('job_agent.services.mock_interview.get_openai_client', return_value=(client, 'test-model')) as get_client:
            succeeded = self.start(engine='cloud', cloud_consent=True, request_id='cloud-success')
            self.assertEqual(succeeded['notice'], '')
            capped = self.start(engine='cloud', cloud_consent=True, request_id='cloud-capped')
            self.assertIn('月度调用上限', capped['notice'])
            self.assertEqual(get_client.call_count, 1)
        self.assertEqual(load_api_usage(self.store.usage_path).ai.successful_requests, 1)

    def test_invalid_cloud_selection_still_counts_successful_response(self):
        from job_agent.services.api_usage import load_api_usage

        self.config.ai.provider = 'openai'
        self.config.ai.monthly_quota = 1
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"id":"invalid"}'))],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2),
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)))
        with patch('job_agent.services.mock_interview.get_openai_client', return_value=(client, 'test-model')) as provider:
            first = self.start(engine='cloud', cloud_consent=True, request_id='invalid-selection-first')
            second = self.start(engine='cloud', cloud_consent=True, request_id='invalid-selection-second')
            self.assertIn('本地规则', first['notice'])
            self.assertIn('月度调用上限', second['notice'])
            self.assertEqual(provider.call_count, 1)
        counter = load_api_usage(self.store.usage_path).ai
        self.assertEqual((counter.successful_requests, counter.input_tokens, counter.output_tokens), (1, 4, 2))
