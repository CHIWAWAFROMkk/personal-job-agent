from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_agent.services.profile_preferences import (
    ProfilePreferencesUpdate,
    update_profile_preferences,
)
from job_agent.services.profile_store import load_profile, save_profile
from tests.helpers import sample_profile


class ProfilePreferencesTests(unittest.TestCase):
    def test_exact_preferences_and_commute_boundary_are_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "private" / "profile.json"
            save_profile(sample_profile(), path)

            update_profile_preferences(
                path,
                ProfilePreferencesUpdate(
                    target_roles=["AI 产品运营", "数据运营"],
                    adjacent_roles=["产品助理"],
                    target_industries=["人工智能"],
                    preferred_locations=["上海"],
                    employment_types=["实习"],
                    must_haves=["连续三个月"],
                    avoid=["纯销售"],
                    commute_origin="上海市徐汇区宜山路站",
                    max_one_way_minutes=60,
                    transport_modes=["地铁", "步行"],
                    remote_acceptable=True,
                    internship_daily_pay_floor=180,
                    exclude_outsourcing=True,
                ),
            )

            profile = load_profile(path)
            self.assertEqual(profile.job_search.target_roles, ["AI 产品运营", "数据运营"])
            self.assertEqual(profile.job_search.commute.origin, "上海市徐汇区宜山路站")
            self.assertEqual(profile.job_search.commute.max_one_way_minutes, 60)
            self.assertTrue(profile.job_search.commute.remote_acceptable)
            self.assertEqual(profile.job_search.internship_daily_pay_floor, 180)
            self.assertTrue(profile.job_search.exclude_outsourcing)
            self.assertTrue((path.parent / "backups").is_dir())


if __name__ == "__main__":
    unittest.main()
