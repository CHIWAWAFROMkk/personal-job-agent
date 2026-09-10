from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from job_agent.models.job_record import JobRecordInput
from job_agent.models.status_sync import BrowserStatusCollection
from job_agent.services.job_repository import JobRepository
from job_agent.services.status_sync import (
    extract_shixiseng_observations,
    normalize_shixiseng_status,
    reconcile_status_collection,
)


def record(company: str, title: str, *, location: str = "上海") -> JobRecordInput:
    return JobRecordInput(
        company=company,
        title=title,
        location=location,
        jd_text=f"公司：{company}\n岗位：{title}\n地点：{location}",
        source="实习僧",
        source_url="https://www.shixiseng.com/intern/test-job",
    )


def payload() -> dict:
    return {
        "data": {
            "messages": [
                {
                    "title": "商业化策略运营实习生(A52005)",
                    "company_name": "Soul APP",
                    "stype": "normal_deliver",
                    "uuid": "dlv_soul",
                    "deliver_status": "checked",
                    "deliver_status_desc": "简历被查看",
                    "latest_time": 1787402667,
                    "content": "这段站内消息正文绝不能进入快照",
                    "user_uuid": "private-user-id",
                },
                {
                    "title": "其他岗位",
                    "company_name": "其他公司",
                    "stype": "normal_deliver",
                    "uuid": "dlv_other",
                    "deliver_status": "reject",
                    "deliver_status_desc": "不合适",
                    "latest_time": 1787402000,
                },
                {
                    "title": "系统邀请岗位",
                    "company_name": "邀请公司",
                    "stype": "system_invite_deliver",
                    "uuid": "siv_invite",
                    "deliver_status": "",
                    "deliver_status_desc": "",
                },
            ]
        }
    }


class StatusSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repository = JobRepository(self.root / "jobs.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_status_mapping_and_privacy_minimized_extraction(self) -> None:
        self.assertEqual(normalize_shixiseng_status("delivered", "投递成功"), "applied")
        self.assertEqual(normalize_shixiseng_status("checked", "简历被查看"), "hr_read")
        self.assertEqual(normalize_shixiseng_status("undeter", "初步筛选"), "screening")
        self.assertEqual(normalize_shixiseng_status("reject", "不合适"), "rejected")
        self.assertEqual(normalize_shixiseng_status("", "收到笔试"), "written_test")

        observations = extract_shixiseng_observations(
            payload(),
            source_endpoint="https://www.shixiseng.com/api/messages?token=secret",
            observed_at=datetime(2026, 8, 22, tzinfo=UTC),
        )
        serialized = "\n".join(item.model_dump_json() for item in observations)
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0].normalized_status, "hr_read")
        self.assertEqual(observations[0].source_endpoint, "https://www.shixiseng.com/api/messages")
        self.assertNotIn("站内消息正文", serialized)
        self.assertNotIn("private-user-id", serialized)
        self.assertNotIn("token=secret", serialized)

    def test_exact_match_updates_and_unmatched_details_are_not_persisted(self) -> None:
        job_id = self.repository.upsert_job(
            record("Soul APP", "商业化策略运营实习生（A52005）")
        ).job_id
        self.repository.record_application_status(
            job_id,
            "applied",
            source="browser_verification",
        )
        observations = extract_shixiseng_observations(payload())
        collection = BrowserStatusCollection(
            session_id="sync-test-update",
            platform="shixiseng",
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
            finished_at=datetime(2026, 8, 22, 1, tzinfo=UTC),
            status="completed",
            observations=observations,
        )

        report, report_path = reconcile_status_collection(
            self.repository,
            collection,
            evidence_root=self.root / "status-sync",
        )

        application = self.repository.get_application(job_id)
        events = self.repository.list_application_events(job_id)
        evidence = Path(report.evidence_path or "").read_text(encoding="utf-8")
        assert application is not None
        self.assertEqual(application.status, "hr_read")
        self.assertEqual([event.status for event in events], ["applied", "hr_read"])
        self.assertEqual(events[-1].source, "shixiseng_sync")
        self.assertEqual(report.updated, 1)
        self.assertEqual(report.matched, 1)
        self.assertEqual(report.unmatched, 1)
        self.assertTrue(report_path.is_file())
        self.assertNotIn("其他公司", evidence)
        self.assertNotIn("这段站内消息正文绝不能进入快照", evidence)
        self.assertIn("unmatched_observation_count", evidence)

    def test_stale_platform_status_does_not_regress_local_progress(self) -> None:
        job_id = self.repository.upsert_job(
            record("Soul APP", "商业化策略运营实习生（A52005）")
        ).job_id
        self.repository.record_application_status(job_id, "hr_read", source="manual")
        stale_payload = payload()
        stale_payload["data"]["messages"][0]["deliver_status"] = "delivered"
        stale_payload["data"]["messages"][0]["deliver_status_desc"] = "投递成功"
        collection = BrowserStatusCollection(
            session_id="sync-test-stale",
            platform="shixiseng",
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
            finished_at=datetime(2026, 8, 22, 1, tzinfo=UTC),
            status="completed",
            observations=extract_shixiseng_observations(stale_payload),
        )

        report, _ = reconcile_status_collection(
            self.repository,
            collection,
            evidence_root=self.root / "status-sync",
        )

        application = self.repository.get_application(job_id)
        assert application is not None
        self.assertEqual(application.status, "hr_read")
        self.assertEqual(report.stale, 1)
        self.assertEqual(len(self.repository.list_application_events(job_id)), 1)

    def test_dry_run_never_writes_status(self) -> None:
        job_id = self.repository.upsert_job(
            record("Soul APP", "商业化策略运营实习生（A52005）")
        ).job_id
        self.repository.record_application_status(job_id, "applied", source="manual")
        collection = BrowserStatusCollection(
            session_id="sync-test-dry",
            platform="shixiseng",
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
            finished_at=datetime(2026, 8, 22, 1, tzinfo=UTC),
            status="completed",
            observations=extract_shixiseng_observations(payload()),
        )
        report, _ = reconcile_status_collection(
            self.repository,
            collection,
            evidence_root=self.root / "status-sync",
            dry_run=True,
        )
        application = self.repository.get_application(job_id)
        assert application is not None
        self.assertEqual(application.status, "applied")
        self.assertEqual(report.decisions[0].outcome, "dry_run")


if __name__ == "__main__":
    unittest.main()
