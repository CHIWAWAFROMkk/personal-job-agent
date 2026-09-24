from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.desktop_qa import _isolate_qa_settings, _require_window_restart_root, _restore_qa_environment, _window_result


class DesktopAcceptanceGateTests(unittest.TestCase):
    def test_qa_environment_restored_even_after_failure(self):
        with patch.dict(os.environ, {'JOB_AGENT_PROFILE': 'original/profile.json'}):
            original = dict(os.environ)

            @_restore_qa_environment
            def failing_check():
                _isolate_qa_settings(Path('synthetic-qa'))
                raise RuntimeError('failed check')

            with self.assertRaisesRegex(RuntimeError, 'failed check'):
                failing_check()
            self.assertEqual(dict(os.environ), original)

    def test_synthetic_test_ignores_inherited_private_path_overrides(self):
        with tempfile.TemporaryDirectory(prefix='window-isolation-') as directory, patch.dict(os.environ):
            root = Path(directory)
            os.environ['JOB_AGENT_DB'] = 'outside/database.sqlite3'
            os.environ['JOB_AGENT_PROFILE'] = 'outside/profile.json'
            _isolate_qa_settings(root)
            self.assertEqual(Path(os.environ['JOB_AGENT_DB']), root / 'data/private/job_agent.sqlite3')
            self.assertEqual(Path(os.environ['JOB_AGENT_PROFILE']), root / 'data/private/profile.json')

    def test_fresh_window_test_rejects_existing_database_without_profile(self):
        from job_agent.desktop import _require_isolated_test_root
        with tempfile.TemporaryDirectory(prefix='window-existing-') as directory:
            root = Path(directory)
            private = root / 'data/private'
            private.mkdir(parents=True)
            (private / 'job_agent.sqlite3').write_bytes(b'existing database')
            with self.assertRaises(RuntimeError):
                _require_isolated_test_root(root, prefix='window-')

    def test_missing_or_skipped_required_flow_cannot_pass(self):
        for checks in ({}, {'resume': {'status': 'skipped', 'reason': 'empty_database'}}):
            result = _window_result(checks, {'resume'})
            self.assertEqual(result['status'], 'failed')
            self.assertIn('resume', result['failed_checks'])

    def test_extra_failed_check_cannot_be_hidden_by_required_success(self):
        self.assertEqual(_window_result({'ui': {'status': 'ok'}, 'native': {'status': 'failed'}}, {'ui'})['status'], 'failed')

    def test_restart_rejects_unmarked_or_changed_profile(self):
        with tempfile.TemporaryDirectory(prefix='window-qa-') as directory:
            root = Path(directory)
            profile = root / 'data/private/profile.json'
            profile.parent.mkdir(parents=True)
            profile.write_bytes(b'synthetic profile')
            with self.assertRaises(RuntimeError):
                _require_window_restart_root(root)
            marker = {'schema': 'synthetic-window-qa-v1', 'job_id': 1,
                      'profile_sha256': hashlib.sha256(profile.read_bytes()).hexdigest()}
            (root / 'window-fixture.json').write_text(json.dumps(marker), encoding='utf-8')
            self.assertEqual(_require_window_restart_root(root)['job_id'], 1)
            profile.write_bytes(b'changed profile')
            with self.assertRaises(RuntimeError):
                _require_window_restart_root(root)

    def test_desktop_recovers_before_initializing_dashboard(self):
        from job_agent.desktop import start_desktop_server
        events = []
        with tempfile.TemporaryDirectory(prefix='desktop-recovery-') as directory, \
                patch.dict(os.environ, {'JOB_AGENT_PROJECT_ROOT': directory, 'JOB_AGENT_PROFILE': 'data/private/profile.json',
                                        'JOB_AGENT_DB': 'data/private/job_agent.sqlite3', 'JOB_AGENT_CONFIG': 'data/private/app-settings.json'}), \
                patch('job_agent.services.profile_recovery.recover_interrupted_profile_switch', side_effect=lambda **kwargs: events.append('recover')), \
                patch('job_agent.services.dashboard.create_dashboard_server', side_effect=RuntimeError('stop before network')):
            with self.assertRaisesRegex(RuntimeError, 'stop before network'):
                start_desktop_server(Path(directory))
            self.assertEqual(events, ['recover'])


if __name__ == '__main__':
    unittest.main()
