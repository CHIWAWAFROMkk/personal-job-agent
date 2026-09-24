import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import sample_profile
from job_agent.models.job_record import JobRecordInput
from job_agent.services.job_repository import JobRepository
from job_agent.services.match_refresh import ensure_current_match, refresh_all_matches
from job_agent.services.dashboard_routes.dashboard_api import build_dashboard_snapshot
from job_agent.services.profile_store import save_profile


class MatchRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = JobRepository(self.root/'jobs.sqlite3')
        record = self.repo.upsert_job(JobRecordInput(company='测试公司',title='SQL 实习',jd_text='本科在读，SQL 数据分析，每周四天。',source='test'))
        self.job = self.repo.get_job(record.job_id)
        self.profile = sample_profile()

    def test_unchanged_inputs_skip_scoring(self):
        first = ensure_current_match(self.repo,self.profile,self.job)
        with patch('job_agent.services.match_refresh.match_job_locally') as scorer:
            again = ensure_current_match(self.repo,self.profile,self.job)
        scorer.assert_not_called()
        self.assertEqual(first.generated_at,again.generated_at)

    def test_jd_and_rule_version_invalidate_cache(self):
        first = ensure_current_match(self.repo,self.profile,self.job)
        changed = self.job.model_copy(update={'jd_text':'硕士及以上，连续实习六个月，要求 Python。'})
        second = ensure_current_match(self.repo,self.profile,changed)
        self.assertNotEqual(first.input_fingerprint,second.input_fingerprint)
        with patch('job_agent.services.match_refresh.MATCH_CACHE_VERSION','test-next-version'):
            third = ensure_current_match(self.repo,self.profile,changed)
        self.assertNotEqual(second.input_fingerprint,third.input_fingerprint)

    def test_dashboard_reloads_profile_and_preserves_state(self):
        profile_path=self.root/'profile.json'
        save_profile(self.profile,profile_path)
        build_dashboard_snapshot(self.repo,output_dir=self.root/'output',profile_path=profile_path)
        before=self.repo.get_latest_match_result(self.job.job_id)
        self.profile.education=[]
        self.profile.skills=[]
        self.profile.experiences=[]
        save_profile(self.profile,profile_path,overwrite=True)
        snapshot=build_dashboard_snapshot(self.repo,output_dir=self.root/'output',profile_path=profile_path)
        after=self.repo.get_latest_match_result(self.job.job_id)
        self.assertNotEqual(before.input_fingerprint,after.input_fingerprint)
        row=next(row for row in snapshot.tracked_jobs if row.job_id==self.job.job_id)
        self.assertEqual(row.match_score,after.overall_score)
        self.assertEqual(self.repo.get_job(self.job.job_id).status,self.job.status)

    def test_bulk_refresh_caches_every_job_across_batch_boundary(self):
        for index in range(104):
            self.repo.upsert_job(JobRecordInput(
                company=f'测试公司 {index}', title=f'SQL 实习 {index}',
                jd_text='本科在读，SQL 数据分析，每周四天。', source='test',
            ))
        refresh_all_matches(self.repo, self.profile)
        self.assertEqual(self.repo.stats().match_results, 105)
        self.assertEqual(len(self.repo.match_refresh_inputs()), 105)

        with patch('job_agent.services.match_refresh.match_job_locally') as scorer:
            refresh_all_matches(self.repo, self.profile)
        scorer.assert_not_called()
        self.assertEqual(self.repo.stats().match_results, 105)

        self.profile.skills = []
        refresh_all_matches(self.repo, self.profile)
        self.assertEqual(self.repo.stats().match_results, 210)
