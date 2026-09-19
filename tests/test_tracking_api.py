import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from job_agent.models.job_record import JobRecordInput
from job_agent.services.dashboard import create_dashboard_server
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import save_profile
from tests.helpers import sample_profile


class TrackingAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        save_profile(sample_profile(), root / 'profile.json')
        self.repo = JobRepository(root / 'jobs.sqlite3')
        self.job_id = self.repo.upsert_job(JobRecordInput(company='合成企业', title='测试岗位', jd_text='测试 JD', source='manual')).job_id
        self.server = create_dashboard_server(self.repo, output_dir=root / 'output', port=0, profile_path=root / 'profile.json', private_dir=root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.auth = {'X-Job-Agent-Token': self.server.RequestHandlerClass.action_token}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, payload=None, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None, headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = self.opener.open(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            return response.status, data if 'text/calendar' in response.headers.get('Content-Type', '') else json.loads(data)

    def test_auth_and_confirm(self):
        routes = [('/api/tracking/parse-message', {}), ('/api/tracking/apply-update', {}), ('/api/tracking/board', None), ('/api/tracking/calendar.ics', None), ('/api/tracking/reminders/1/complete', {})]
        for path, payload in routes:
            self.assertEqual(self.request(path, payload)[0], 403)
            self.assertEqual(self.request(path, payload, {**self.auth, 'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/tracking/apply-update', {}, self.auth)[0], 400)
        self.assertEqual(self.request('/api/tracking/parse-message', {'text': ''}, self.auth)[0], 400)

    def test_round_trip(self):
        status, parsed = self.request('/api/tracking/parse-message', {'text': '【示例公司】邀请参加在线测评，请于2026年9月25日18:00前完成。https://exam.example/test'}, self.auth)
        self.assertEqual(status, 200)
        self.assertIn('warnings', parsed)
        self.assertEqual(self.repo.tracking_board()['reminders'], [])
        payload = {'confirmed': True, 'job_id': self.job_id, 'status': 'oa_pending', 'request_id': 'test-1', 'event': {'kind': 'oa', 'at': '2026-09-25T18:00:00+08:00', 'url': 'https://exam.example/test'}}
        status, result = self.request('/api/tracking/apply-update', payload, self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(self.request('/api/tracking/board', headers=self.auth)[1]['jobs'][0]['status'], 'assessment')
        self.assertIn(b'BEGIN:VEVENT', self.request('/api/tracking/calendar.ics', headers=self.auth)[1])
        self.assertEqual(self.request(f"/api/tracking/reminders/{result['reminder_id']}/complete", {'confirmed': True}, self.auth)[0], 200)
        self.assertNotIn(b'BEGIN:VEVENT', self.request('/api/tracking/calendar.ics', headers=self.auth)[1])
