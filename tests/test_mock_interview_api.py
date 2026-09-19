from tests import test_tracking_api


class MockInterviewAPITests(test_tracking_api.TrackingAPITests):
    # Shared isolated HTTP fixture; do not inherit tracking assertions as new tests.
    test_auth_and_confirm = None
    test_round_trip = None

    def test_interview_auth_and_roundtrip(self):
        routes = [('/api/interview/session/start', {}), ('/api/interview/session/reply', {}),
                  ('/api/interview/session/score', {}), ('/api/interview/teleprompter', {}),
                  (f'/api/interview/jobs/{self.job_id}/sessions', None)]
        for path, payload in routes:
            self.assertEqual(self.request(path, payload)[0], 403)
            self.assertEqual(self.request(path, payload, {**self.auth, 'Origin': 'https://evil.example'})[0], 403)
        p = {'job_id': self.job_id, 'request_id': 'start', 'engine': 'local'}
        code, s = self.request('/api/interview/session/start', p, self.auth)
        self.assertEqual(code, 200)
        sid = s['session_id']
        self.assertEqual(self.request('/api/interview/session/' + sid)[0], 403)
        self.assertEqual(self.request('/api/interview/session/' + sid, headers=self.auth)[1], s)
        reply = {'session_id': sid, 'answer': '我使用 SQL 完成分析任务。', 'expected_revision': 0, 'request_id': 'reply'}
        self.assertEqual(self.request('/api/interview/session/reply', reply, self.auth)[0], 200)
        self.assertEqual(self.request('/api/interview/session/reply', {**reply, 'request_id': 'other'}, self.auth)[0], 409)
        self.assertEqual(self.request('/api/interview/session/score', {'session_id': sid, 'expected_revision': 1}, self.auth)[1]['status'], 'completed')
        self.assertEqual(len(self.request(f'/api/interview/jobs/{self.job_id}/sessions', headers=self.auth)[1]['sessions']), 1)
        self.assertEqual(self.request('/api/interview/teleprompter', {'job_id': self.job_id}, self.auth)[0], 200)

    def test_invalid(self):
        self.assertEqual(self.request('/api/interview/session/start', {'job_id': True}, self.auth)[0], 400)
        self.assertEqual(self.request('/api/interview/session/start', [], self.auth)[0], 400)
        self.assertEqual(self.request('/api/interview/session/00000000-0000-0000-0000-000000000000', headers=self.auth)[0], 404)
