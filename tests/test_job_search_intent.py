from __future__ import annotations

import unittest
from datetime import date

from job_agent.models.profile import Education, JobSearchPreferences, Profile
from job_agent.services.job_search_intent import (
    build_discovery_query,
    resolve_employment_keywords,
    resolve_graduation_cohort,
)


class JobSearchIntentTests(unittest.TestCase):
    def test_cohort_derived_from_education_end(self):
        profile = Profile(
            education=[
                Education(id="edu-1", institution="大学A", end="2025-06"),
                Education(id="edu-2", institution="大学B", end="2026-07"),
            ]
        )
        self.assertEqual(resolve_graduation_cohort(profile), 2026)

    def test_cohort_fallback_to_current_date_calendar(self):
        profile = Profile()
        # Spring/Summer (before July) belongs to current calendar year
        cohort_spring = resolve_graduation_cohort(profile, current_date=date(2026, 4, 15))
        self.assertEqual(cohort_spring, 2026)

        # Fall/Winter (July+) belongs to next calendar year
        cohort_autumn = resolve_graduation_cohort(profile, current_date=date(2026, 9, 23))
        self.assertEqual(cohort_autumn, 2027)

    def test_employment_keywords_from_profile_and_override(self):
        profile = Profile(
            job_search=JobSearchPreferences(
                employment_types=["campus", "internship"]
            )
        )
        self.assertEqual(resolve_employment_keywords(profile), ["校招", "实习"])

        # Override overrides profile
        self.assertEqual(resolve_employment_keywords(profile, override_type="social"), ["社招"])

    def test_build_discovery_query_formats_correctly(self):
        query = build_discovery_query(
            location="上海",
            role="算法工程师",
            cohort=2026,
            employment_keyword="校招",
        )
        self.assertIn("上海 算法工程师 校招 2026届", query)
        self.assertIn("site:shixiseng.com OR site:nowcoder.com OR site:zhipin.com", query)


if __name__ == "__main__":
    unittest.main()
