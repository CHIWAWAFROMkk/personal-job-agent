from __future__ import annotations

import unittest
from datetime import UTC, datetime

from job_agent.models.job_record import JobDetail, JobSourceItem
from job_agent.services.job_source_review import review_job_source


def _job(
    *,
    sources: list[JobSourceItem],
    published_at: str | None = None,
    deadline_at: str | None = None,
    last_seen_at: str = "2026-08-31T00:00:00Z",
) -> JobDetail:
    return JobDetail(
        job_id=7,
        company="示例公司",
        title="数据运营实习生",
        location="上海",
        jd_text="负责数据运营。",
        published_at=published_at,
        deadline_at=deadline_at,
        status="discovered",
        sources=sources,
        created_at="2026-08-20T00:00:00Z",
        updated_at=last_seen_at,
        first_seen_at="2026-08-20T00:00:00Z",
        last_seen_at=last_seen_at,
    )


class JobSourceReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

    def test_recent_official_ats_with_two_sources_has_strong_route_evidence(self) -> None:
        job = _job(
            published_at="2026-08-28T08:00:00Z",
            deadline_at="2026-09-15",
            sources=[
                JobSourceItem(
                    platform="Greenhouse",
                    source_url="https://boards.greenhouse.io/example/jobs/123",
                    first_seen_at="2026-08-28T00:00:00Z",
                    last_seen_at="2026-08-31T00:00:00Z",
                ),
                JobSourceItem(
                    platform="Lever",
                    source_url="https://jobs.lever.co/example/abc",
                    first_seen_at="2026-08-29T00:00:00Z",
                    last_seen_at="2026-08-31T00:00:00Z",
                ),
            ],
        )

        review = review_job_source(job, now=self.now)

        self.assertEqual(review.source_route, "official_ats")
        self.assertEqual(review.source_confidence, "high")
        self.assertEqual(review.freshness, "fresh")
        self.assertEqual(review.age_days, 3)
        self.assertTrue(any("2 个来源" in item for item in review.positive_signals))
        self.assertIn("仍需再次确认", review.recommended_action)

    def test_expired_manual_record_is_held_for_read_only_verification(self) -> None:
        job = _job(
            deadline_at="2026-08-29",
            sources=[
                JobSourceItem(
                    platform="manual",
                    first_seen_at="2026-08-20T00:00:00Z",
                    last_seen_at="2026-08-31T00:00:00Z",
                )
            ],
        )

        review = review_job_source(job, now=self.now)

        self.assertEqual(review.source_route, "manual")
        self.assertEqual(review.source_confidence, "low")
        self.assertEqual(review.freshness, "expired")
        self.assertIn("不要生成或上传", review.recommended_action)
        self.assertTrue(any("缺少可直接核验" in item for item in review.review_items))

    def test_old_board_record_without_original_dates_requires_recheck(self) -> None:
        job = _job(
            last_seen_at="2026-07-30T00:00:00Z",
            sources=[
                JobSourceItem(
                    platform="实习僧",
                    source_url="https://www.shixiseng.com/intern/example",
                    first_seen_at="2026-07-30T00:00:00Z",
                    last_seen_at="2026-07-30T00:00:00Z",
                )
            ],
        )

        review = review_job_source(job, now=self.now)

        self.assertEqual(review.source_route, "established_board")
        self.assertEqual(review.freshness, "needs_recheck")
        self.assertEqual(review.age_days, 32)
        self.assertTrue(any("未记录原始发布时间" in item for item in review.review_items))
        self.assertIn("先打开来源页面", review.recommended_action)

    def test_known_company_career_portal_uses_official_route(self) -> None:
        job = _job(
            sources=[
                JobSourceItem(
                    platform="字节跳动校园招聘官网",
                    source_url="https://jobs.bytedance.com/campus/position/123",
                    first_seen_at="2026-08-27T00:00:00Z",
                    last_seen_at="2026-08-27T00:00:00Z",
                )
            ],
            last_seen_at="2026-08-27T00:00:00Z",
        )

        review = review_job_source(job, now=self.now)

        self.assertEqual(review.source_route, "official_ats")
        self.assertEqual(review.freshness, "recently_seen")
        self.assertEqual(review.source_confidence, "high")


if __name__ == "__main__":
    unittest.main()
