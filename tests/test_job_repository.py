import tempfile
import unittest
from pathlib import Path

from job_agent.models.commute import CommuteRouteOption, CommuteRouteResult
from job_agent.models.job_record import JobRecordInput, SearchCandidateInput
from job_agent.services.job_repository import JobDatabaseError, JobRepository
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from tests.helpers import sample_profile


JD = """
公司：北京牛客科技有限公司
岗位：商业化产品运营实习生
地点：上海
岗位职责：负责产品运营和数据分析。
任职要求：
1. 本科及以上学历。
2. 每周至少 4 天，连续实习 3 个月。
""".strip()


def make_record(
    *,
    source: str = "牛客网",
    source_url: str = "https://example.com/jobs/123?utm_source=test",
    company: str = "北京牛客科技有限公司",
    title: str = "商业化产品运营实习生",
    location: str = "上海",
) -> JobRecordInput:
    return JobRecordInput(
        company=company,
        title=title,
        location=location,
        jd_text=JD,
        source=source,
        source_url=source_url,
    )


def make_candidate(
    *,
    provider: str = "bocha",
    query: str = "上海 AI 产品运营 实习",
    url: str = "https://example.com/job/ai-1?utm_source=bocha",
) -> SearchCandidateInput:
    return SearchCandidateInput(
        provider=provider,
        query=query,
        title="AI 产品运营实习生",
        url=url,
        snippet="每周 5 天，实习 3 个月。",
    )


class JobRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "jobs.sqlite3"
        self.repository = JobRepository(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_same_source_import_is_idempotent(self) -> None:
        first = self.repository.upsert_job(make_record())
        second = self.repository.upsert_job(
            make_record(source_url="https://example.com/jobs/123")
        )

        self.assertTrue(first.created)
        self.assertTrue(first.source_added)
        self.assertFalse(second.created)
        self.assertFalse(second.source_added)
        self.assertEqual(second.dedupe_reason, "same_source")
        stats = self.repository.stats()
        self.assertEqual(stats.jobs, 1)
        self.assertEqual(stats.sources, 1)

    def test_cross_platform_duplicate_keeps_both_sources(self) -> None:
        first = self.repository.upsert_job(
            make_record(
                company="北京牛客科技有限公司",
                title="商业化产品运营实习生（A12345）",
                location="上海市浦东新区",
            )
        )
        second = self.repository.upsert_job(
            make_record(
                source="实习僧",
                source_url="https://other.example/intern/456?pcm=search",
                company="北京牛客科技公司",
                title="商业化产品运营实习生（A99999）",
                location="上海",
            )
        )

        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(second.created)
        self.assertTrue(second.source_added)
        self.assertEqual(second.dedupe_reason, "same_company_title_location")
        stats = self.repository.stats()
        self.assertEqual(stats.jobs, 1)
        self.assertEqual(stats.sources, 2)
        self.assertEqual(stats.merged_source_records, 1)

    def test_different_city_is_not_merged(self) -> None:
        first = self.repository.upsert_job(make_record(location="上海"))
        second = self.repository.upsert_job(
            make_record(
                source="实习僧",
                source_url="https://other.example/jobs/123",
                location="北京",
            )
        )

        self.assertNotEqual(first.job_id, second.job_id)
        self.assertEqual(self.repository.stats().jobs, 2)

    def test_match_result_updates_job_and_is_idempotent(self) -> None:
        outcome = self.repository.upsert_job(make_record())
        match_result = match_job_locally(
            sample_profile(),
            structure_job_locally(
                JD,
                company="北京牛客科技有限公司",
                title="商业化产品运营实习生",
                location="上海",
                source="牛客网",
                source_url="https://example.com/jobs/123",
            ),
        )

        self.assertTrue(self.repository.add_match_result(outcome.job_id, match_result))
        self.assertFalse(self.repository.add_match_result(outcome.job_id, match_result))
        jobs = self.repository.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].match_score, match_result.overall_score)
        self.assertEqual(self.repository.stats().match_results, 1)

    def test_get_job_and_latest_match_result(self) -> None:
        outcome = self.repository.upsert_job(make_record())
        match_result = match_job_locally(
            sample_profile(),
            structure_job_locally(
                JD,
                company="北京牛客科技有限公司",
                title="商业化产品运营实习生",
                location="上海",
                source="牛客网",
                source_url="https://example.com/jobs/123",
            ),
        )
        self.repository.add_match_result(outcome.job_id, match_result)

        job = self.repository.get_job(outcome.job_id)
        latest = self.repository.get_latest_match_result(outcome.job_id)

        self.assertEqual(job.company, "北京牛客科技有限公司")
        self.assertEqual(job.jd_text, JD)
        self.assertEqual(len(job.sources), 1)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.overall_score, match_result.overall_score)

    def test_get_missing_job_has_clear_error(self) -> None:
        with self.assertRaisesRegex(JobDatabaseError, "岗位 #999 不存在"):
            self.repository.get_job(999)

    def test_user_confirmed_commute_is_persisted_and_listed(self) -> None:
        outcome = self.repository.upsert_job(make_record())

        updated = self.repository.update_job_commute(
            outcome.job_id,
            47,
            method="route_estimate",
            note="地铁加步行，工作日早高峰估算。",
        )

        self.assertEqual(updated.commute_minutes, 47)
        self.assertEqual(updated.commute_method, "route_estimate")
        self.assertEqual(updated.commute_note, "地铁加步行，工作日早高峰估算。")
        self.assertIsNotNone(updated.commute_updated_at)
        listed = self.repository.list_jobs()
        self.assertEqual(listed[0].commute_minutes, 47)
        with self.assertRaisesRegex(JobDatabaseError, "0-600"):
            self.repository.update_job_commute(outcome.job_id, 601)

    def test_calculated_route_is_persisted_with_explainable_fields(self) -> None:
        outcome = self.repository.upsert_job(make_record())
        route = CommuteRouteResult(
            mode="transit",
            origin_query="上海市徐汇区宜山路站",
            origin_resolved="上海市徐汇区宜山路地铁站",
            destination_query="上海市杨浦区创智天地",
            destination_resolved="上海市杨浦区创智天地广场",
            options=[
                CommuteRouteOption(
                    rank=1,
                    minutes=46,
                    distance_meters=16800,
                    walking_meters=620,
                    transfers=1,
                    summary="9号线 → 10号线",
                )
            ],
        )

        updated = self.repository.update_job_route(outcome.job_id, route)

        self.assertEqual(updated.commute_minutes, 46)
        self.assertEqual(updated.commute_provider, "amap")
        self.assertEqual(updated.commute_mode, "transit")
        self.assertEqual(updated.commute_distance_meters, 16800)
        self.assertEqual(updated.commute_destination, "上海市杨浦区创智天地")
        listed = self.repository.list_jobs()[0]
        self.assertEqual(listed.commute_route_summary, "9号线 → 10号线")

    def test_application_status_is_idempotent_and_updates_job(self) -> None:
        result = self.repository.upsert_job(
            make_record(
                source="实习僧",
                source_url="https://www.shixiseng.com/intern/soul-1",
            )
        )

        first_changed = self.repository.record_application_status(
            result.job_id,
            "applied",
            source="browser_verification",
            detail="页面显示已投递",
            resume_path="C:/private/soul.pdf",
            evidence_path="C:/private/applied.png",
            verification_method="visible_page_marker",
        )
        second_changed = self.repository.record_application_status(
            result.job_id,
            "applied",
            source="browser_verification",
            detail="再次核验仍为已投递",
            verification_method="visible_page_marker",
        )

        application = self.repository.get_application(result.job_id)
        events = self.repository.list_application_events(result.job_id)
        job = self.repository.get_job(result.job_id)
        self.assertTrue(first_changed)
        self.assertFalse(second_changed)
        self.assertIsNotNone(application)
        assert application is not None
        self.assertEqual(application.status, "applied")
        self.assertIsNotNone(application.applied_at)
        self.assertIsNotNone(application.last_verified_at)
        self.assertEqual(job.status, "applied")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, "applied")
        listed = self.repository.list_applications(status="applied")
        summary = self.repository.application_summary()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].job_id, result.job_id)
        self.assertEqual(summary.total, 1)
        self.assertEqual(summary.active, 1)
        self.assertEqual(summary.applied_or_later, 1)

    def test_application_progress_prevents_accidental_regression(self) -> None:
        result = self.repository.upsert_job(make_record())
        self.repository.record_application_status(
            result.job_id,
            "applied",
            source="browser_verification",
        )
        self.repository.record_application_status(
            result.job_id,
            "hr_read",
            source="manual",
            detail="本人在站内看到 HR 已读",
        )

        with self.assertRaisesRegex(JobDatabaseError, "回退"):
            self.repository.record_application_status(
                result.job_id,
                "applied",
                source="browser_verification",
            )

        self.repository.update_application_verification(
            result.job_id,
            verification_method="visible_page_marker",
            evidence_path="C:/private/recheck.png",
        )
        application = self.repository.get_application(result.job_id)
        events = self.repository.list_application_events(result.job_id)
        summary = self.repository.application_summary()
        assert application is not None
        self.assertEqual(application.status, "hr_read")
        self.assertEqual([event.status for event in events], ["applied", "hr_read"])
        self.assertEqual(summary.hr_read_or_later, 1)
        self.assertEqual(summary.meaningful_responses, 0)
        self.assertEqual(summary.priority_preparation, 0)

        self.assertTrue(
            self.repository.record_application_status(
                result.job_id,
                "applied",
                source="manual",
                detail="真实状态确认回退",
                allow_regression=True,
            )
        )
        self.assertEqual(
            self.repository.get_application(result.job_id).status,  # type: ignore[union-attr]
            "applied",
        )

    def test_integrity_check_passes(self) -> None:
        self.repository.upsert_job(make_record())
        self.assertIsNone(self.repository.verify())

    def test_profile_switch_backup_is_recoverable_and_clears_user_data(self) -> None:
        result = self.repository.upsert_job(make_record())
        self.repository.record_application_status(
            result.job_id,
            "applied",
            source="manual_dashboard",
        )
        backup = self.repository.backup_and_clear_for_new_profile(
            self.database_path.parent / "backups"
        )

        self.assertTrue(backup.is_file())
        self.assertEqual(self.repository.stats().jobs, 0)
        self.assertEqual(self.repository.application_summary().total, 0)
        restored = JobRepository(backup)
        self.assertEqual(restored.stats().jobs, 1)
        self.assertEqual(restored.application_summary().applied_or_later, 1)

    def test_search_candidate_dedupes_url_and_keeps_sightings(self) -> None:
        first = self.repository.upsert_search_candidate(make_candidate())
        second = self.repository.upsert_search_candidate(
            make_candidate(
                provider="browser",
                query="AI 运营 实习 上海",
                url="https://example.com/job/ai-1",
            )
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.candidate_id, second.candidate_id)
        self.assertTrue(second.sighting_added)
        candidates = self.repository.list_search_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_count, 2)
        self.assertEqual(candidates[0].url, "https://example.com/job/ai-1")

    def test_candidate_must_be_verified_before_promotion(self) -> None:
        outcome = self.repository.upsert_search_candidate(make_candidate())
        pending = self.repository.list_search_candidates(status="pending")
        self.assertEqual(len(pending), 1)

        self.repository.mark_candidate_verification(
            outcome.candidate_id,
            "live",
            detail="页面显示可投递且 JD 完整。",
        )

        self.assertEqual(self.repository.list_search_candidates(status="pending"), [])
        live = self.repository.list_search_candidates(status="live")
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].verification_detail, "页面显示可投递且 JD 完整。")
        stats = self.repository.stats()
        self.assertEqual(stats.candidates, 1)
        self.assertEqual(stats.live_candidates, 1)

    def test_irrelevant_candidate_is_counted_separately(self) -> None:
        outcome = self.repository.upsert_search_candidate(make_candidate())

        self.repository.mark_candidate_verification(
            outcome.candidate_id,
            "irrelevant",
            detail="不是在招岗位，或明显偏离目标方向。",
        )

        irrelevant = self.repository.list_search_candidates(status="irrelevant")
        self.assertEqual(len(irrelevant), 1)
        stats = self.repository.stats()
        self.assertEqual(stats.irrelevant_candidates, 1)
        self.assertEqual(stats.pending_candidates, 0)


if __name__ == "__main__":
    unittest.main()
