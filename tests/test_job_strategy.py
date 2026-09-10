from __future__ import annotations

import unittest
from datetime import date

from job_agent.models.job_record import JobDetail
from job_agent.services.job_strategy import (
    detect_outsourcing,
    evaluate_job_strategy,
    infer_opportunity_track,
    parse_daily_compensation,
)


def make_job(*, title: str, jd: str, deadline: str | None = None) -> JobDetail:
    return JobDetail(
        job_id=1,
        company="示例公司",
        title=title,
        location="上海",
        jd_text=jd,
        deadline_at=deadline,
        status="discovered",
        match_score=78,
        sources=[],
        created_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:00:00+00:00",
        first_seen_at="2026-09-01T00:00:00+00:00",
        last_seen_at="2026-09-01T00:00:00+00:00",
    )


class JobStrategyTests(unittest.TestCase):
    def test_campus_marker_wins_even_when_title_contains_intern(self) -> None:
        job = make_job(
            title="2027届数据运营实习生",
            jd="秋招项目，截止日期临近，负责业务数据运营。",
            deadline="2026-09-19",
        )

        result = evaluate_job_strategy(
            job,
            match_score=78,
            commute_fit="good",
            daily_pay_floor=180,
            primary_roles=["数据运营"],
            today=date(2026, 9, 9),
        )

        self.assertEqual(result.opportunity_track, "autumn_recruitment")
        self.assertEqual(result.role_tier, "primary")
        self.assertTrue(result.deadline_urgent)
        self.assertEqual(result.deadline_days, 10)
        self.assertEqual(result.compensation_fit, "not_applicable")
        self.assertEqual(result.strategy_fit, "recommended")

    def test_daily_pay_commute_and_outsourcing_are_explainable_boundaries(self) -> None:
        job = make_job(
            title="产品运营实习生",
            jd="日薪 150-170 元/天，第三方派遣，工作节奏高强度。",
        )

        result = evaluate_job_strategy(
            job,
            match_score=82,
            commute_fit="over_limit",
            daily_pay_floor=180,
            exclude_outsourcing=True,
            today=date(2026, 9, 9),
        )

        self.assertEqual(result.opportunity_track, "daily_internship")
        self.assertEqual(result.compensation_min_daily, 150)
        self.assertEqual(result.compensation_max_daily, 170)
        self.assertEqual(result.compensation_fit, "below_floor")
        self.assertTrue(result.outsourcing_risk)
        self.assertEqual(result.strategy_fit, "blocked")
        self.assertTrue(any("低薪、远通勤" in reason for reason in result.reasons))

    def test_undisclosed_pay_stays_for_manual_confirmation(self) -> None:
        job = make_job(title="业务运营实习生", jd="负责周报、数据复盘和跨部门协作。")
        result = evaluate_job_strategy(
            job,
            match_score=76,
            daily_pay_floor=180,
            today=date(2026, 9, 9),
        )

        self.assertEqual(result.compensation_fit, "unknown")
        self.assertEqual(result.strategy_fit, "manual_review")
        self.assertTrue(any("日薪未明确披露" in reason for reason in result.reasons))

    def test_school_background_phrase_does_not_change_strategy_score(self) -> None:
        base = make_job(title="数据运营实习生", jd="负责数据运营，200元/天。")
        mentioned = make_job(
            title="数据运营实习生",
            jd="负责数据运营，200元/天。不限院校背景。",
        )
        first = evaluate_job_strategy(base, match_score=75, daily_pay_floor=180, today=date(2026, 9, 9))
        second = evaluate_job_strategy(mentioned, match_score=75, daily_pay_floor=180, today=date(2026, 9, 9))
        self.assertEqual(first.priority_score, second.priority_score)

    def test_parsers_do_not_convert_monthly_salary_or_false_positive_non_outsource(self) -> None:
        self.assertEqual(parse_daily_compensation("月薪 5000-7000 元"), (None, None))
        self.assertEqual(parse_daily_compensation("薪资 180—220 元/天"), (180, 220))
        self.assertFalse(detect_outsourcing("公司直招，非外包岗位"))
        self.assertEqual(infer_opportunity_track("产品运营", "校园招聘 应届生"), "autumn_recruitment")


if __name__ == "__main__":
    unittest.main()
