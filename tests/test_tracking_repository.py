import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.models.job_record import JobRecordInput
from job_agent.services.job_repository import JobRepository
from job_agent.services.dashboard_routes.tracking_api import build_calendar


class TrackingRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = JobRepository(Path(self.temp.name) / 'jobs.sqlite3')
        self.job_id = self.repo.upsert_job(JobRecordInput(company='合成企业', title='测试岗位', jd_text='合成 JD', source='manual')).job_id
        self.payload = {'confirmed': True, 'job_id': self.job_id, 'status': 'oa_pending', 'request_id': 'req-1', 'event': {'kind': 'oa', 'at': '2026-09-25T12:00:00+08:00', 'url': 'https://exam.example/test'}}

    def test_transaction_idempotency_and_calendar(self):
        result = self.repo.apply_tracking_update(self.payload)
        self.assertTrue(result['changed'])
        self.assertTrue(self.repo.apply_tracking_update(self.payload)['duplicate'])
        self.assertEqual(len(self.repo.list_application_events(self.job_id)), 1)
        board = self.repo.tracking_board()
        self.assertEqual(board['jobs'][0]['status'], 'assessment')
        self.assertEqual(len(board['reminders']), 1)
        self.assertEqual(board['reminders'][0]['at'], '2026-09-25T04:00:00+00:00')
        self.assertIn(b'DTSTART:20260925T040000Z', build_calendar(board['reminders']))
        with self.assertRaises(ValueError):
            self.repo.apply_tracking_update({**self.payload, 'status': 'rejected', 'event': None})
        self.repo.complete_tracking_reminder(result['reminder_id'])
        self.assertEqual(self.repo.tracking_board()['reminders'], [])

    def test_rollback_after_status_write(self):
        original = self.repo._record_application_status
        def fail_after(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('synthetic write failure')
        with patch.object(self.repo, '_record_application_status', side_effect=fail_after):
            with self.assertRaises(RuntimeError):
                self.repo.apply_tracking_update(self.payload)
        self.assertIsNone(self.repo.get_application(self.job_id))
        self.assertEqual(self.repo.tracking_board()['jobs'][0]['status'], 'discovered')
        self.assertEqual(self.repo.tracking_board()['reminders'], [])
        self.assertFalse(self.repo.apply_tracking_update(self.payload)['duplicate'])

    def test_invalid_input_has_no_writes(self):
        for change in [{'confirmed': False}, {'job_id': True}, {'request_id': ''}, {'status': []}, {'event': {'kind': 'oa', 'at': '2026-09-25'}}, {'event': {'kind': 'oa', 'at': '2026-09-25T12:00:00'}}, {'event': {**self.payload['event'], 'url': 'javascript:alert(1)'}}, {'event': {**self.payload['event'], 'kind': 'interview'}}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.repo.apply_tracking_update({**self.payload, **change})
        self.assertIsNone(self.repo.get_application(self.job_id))

    def test_distinct_events_preserved_and_terminal_legacy_status(self):
        self.repo.apply_tracking_update(self.payload)
        self.repo.apply_tracking_update({**self.payload, 'request_id': 'req-2', 'event': {**self.payload['event'], 'at': '2026-09-26T12:00:00+08:00'}})
        self.assertEqual(len(self.repo.tracking_board()['reminders']), 2)
        self.repo.record_application_status(self.job_id, 'rejected', source='manual')
        # Applications are authoritative even if an old import changed jobs.status.
        with self.repo._connection() as connection:
            connection.execute("UPDATE jobs SET status='discovered' WHERE id=?", (self.job_id,))
        self.assertEqual(self.repo.tracking_board()['jobs'][0]['status'], 'rejected')
        self.assertEqual(self.repo.tracking_board()['reminders'], [])

    def test_ics_escaping_utf8_folding_and_stability(self):
        self.repo.apply_tracking_update(self.payload)
        rows = self.repo.tracking_board()['reminders']
        rows[0]['company'] = '测' * 100 + ',;\\\nBEGIN:VEVENT'
        value = build_calendar(rows)
        self.assertEqual(value, build_calendar(rows))
        self.assertTrue(all(len(line) <= 75 for line in value.split(b'\r\n')))
        self.assertEqual(value.count(b'\r\nBEGIN:VEVENT\r\n'), 1)
        self.assertIn(b'\\,\\;\\\\\\nBEGIN:VEVENT', value)
