import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from job_agent.models.job_record import JobRecordInput
from job_agent.services.dashboard_routes import jobs_api
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import ProfileStoreError, save_profile
from job_agent.services.resume_polish import ResumePolishError
from tests.helpers import sample_profile
from tests.test_profile_onboarding import RESUME_TEXT
from job_agent.services.profile_onboarding import ProfileOnboardingInput, onboard_profile


class OnboardingRouteReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.handler = Mock()
        self.handler.profile_path = self.root / 'private' / 'profile.json'
        self.handler.private_dir = self.root / 'private'
        self.handler.output_dir = self.root / 'output'
        self.handler.runtime_config_path = self.root / 'runtime.json'
        self.handler.api_usage_path = self.root / 'usage.json'
        self.handler.repository = JobRepository(self.root / 'jobs.sqlite3')
        self.handler.headers = {'Content-Type': 'application/json'}
        self.handler._authorized_action.return_value = True

    def test_missing_profile_is_actionable_and_does_not_leak_path(self):
        with self.assertRaises(ProfileStoreError) as caught:
            jobs_api.load_profile(self.handler.profile_path)
        self.assertIn('个人资料与简历', str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_import_failure_does_not_silently_create_job(self):
        self.handler._read_body.return_value = json.dumps({
            'company': '示例公司', 'title': '数据实习', 'jd_text': '使用 SQL 整理数据'
        }).encode()
        with patch.object(self.handler.repository, 'upsert_job', wraps=self.handler.repository.upsert_job) as write:
            jobs_api.handle_import_parsed_job(self.handler)
        write.assert_not_called()
        self.assertIn('个人资料与简历', self.handler._json.call_args.args[0]['error'])

    def test_corrupt_profile_does_not_expose_validation_contents(self):
        self.handler.profile_path.parent.mkdir()
        self.handler.profile_path.write_text('{"private-value": true}', encoding='utf-8')
        with self.assertRaises(ProfileStoreError) as caught:
            jobs_api.load_profile(self.handler.profile_path)
        self.assertNotIn('private-value', str(caught.exception))
        self.assertIn('原文件未被修改', str(caught.exception))

    def test_actual_draft_generation_without_photo_keeps_review_required(self):
        save_profile(sample_profile(), self.handler.profile_path)
        self._generate_actual_draft()

    def test_imported_resume_can_generate_without_inventing_organization(self):
        onboard_profile(ProfileOnboardingInput(
            resume_filename='synthetic.txt', resume_bytes=RESUME_TEXT.encode('utf-8'),
            display_name='测试用户', target_roles=['数据运营'], confirm_truth=True),
            profile_path=self.handler.profile_path, private_dir=self.handler.private_dir)
        original = self.handler.profile_path.read_bytes()
        content = self._generate_actual_draft()
        entry = content['experience_sections'][0]['entries'][0]
        self.assertEqual(content['experience_sections'][0]['title'], '相关经历')
        self.assertEqual(entry['organization'], '')
        self.assertEqual(entry['role'], '')
        self.assertTrue(entry['bullets'])
        self.assertTrue(all(bullet['fact_ids'] for bullet in entry['bullets']))
        self.assertEqual(original, self.handler.profile_path.read_bytes())

    def _generate_actual_draft(self):
        job = self.handler.repository.upsert_job(JobRecordInput(
            company='示例公司', title='数据实习', source='test',
            jd_text='使用 SQL 整理数据，本科，每周四天。'))
        self.handler._read_body.return_value = b'{"confirmed": true}'
        with patch.object(jobs_api, 'compose_resume_content_with_jd', side_effect=ResumePolishError('本地模式')):
            jobs_api.handle_resume_draft(self.handler, str(job.job_id))
        result = self.handler._json.call_args.args[0]
        self.assertTrue(result.get('ok'), result)
        manifests = list(self.handler.output_dir.rglob('resume-version-portable.json'))
        self.assertTrue(manifests)
        content_files = list(self.handler.output_dir.rglob('resume-content-portable.json'))
        self.assertTrue(content_files)
        content = json.loads(content_files[0].read_text(encoding='utf-8'))
        self.assertFalse(content['person']['photo_path'])
        self.assertTrue(result['generation']['review_required'])
        self.assertTrue(list(self.handler.output_dir.rglob('*.pdf')))
        return content
