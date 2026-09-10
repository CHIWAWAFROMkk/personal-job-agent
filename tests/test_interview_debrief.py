import tempfile
import unittest
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from job_agent.models.interview_debrief import InterviewDebriefInput
from job_agent.services.interview_debrief import record_interview_debrief, InterviewDebriefError
from job_agent.services.job_repository import JobRepository, JobDatabaseError
from job_agent.services.profile_store import save_profile
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from job_agent.services.preparation_pack import build_preparation_pack, render_preparation_pack_markdown
from tests.helpers import sample_profile
from tests.test_job_repository import make_record


class InterviewDebriefTests(unittest.TestCase):
    def test_cli_round_trip_after_legacy_upgrade(self):
        work = Path(__file__).resolve().parents[1] / "work"
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work, prefix="debrief-e2e-") as directory:
            root = Path(directory)
            repo = JobRepository(root / "jobs.sqlite3")
            job_id = repo.upsert_job(make_record()).job_id
            repo.record_application_status(job_id, "applied", source="test")
            original = repo.get_job(job_id)
            # Simulate the previous schema only in this isolated fixture.
            with closing(sqlite3.connect(repo.path)) as connection, connection:
                connection.execute("DROP TABLE interview_debriefs")
                connection.execute("UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'")
            profile_path = root / "profile.json"
            save_profile(sample_profile(), profile_path)
            original_profile = profile_path.read_bytes()
            entry = {
                "stage": "interview_1", "question": "怎么检查数据？", "answer": "检查空值。",
                "better_answer": "先确认业务口径。", "evidence_fact_ids": ["fact-sql-analysis"],
                "strengths": ["步骤清晰"], "gaps": ["缺少边界情况"], "next_actions": ["练习去重"],
            }
            input_path = root / "复盘.json"
            input_path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8-sig")
            env = {**os.environ, "JOB_AGENT_PROJECT_ROOT": str(root),
                   "JOB_AGENT_DB": str(repo.path), "JOB_AGENT_PROFILE": str(profile_path),
                   "JOB_AGENT_CONFIG": str(root / "settings.json"), "PYTHONUTF8": "1"}

            def run(*args):
                result = subprocess.run(
                    [sys.executable, "-m", "job_agent", "applications", *args],
                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            run("debrief", str(job_id), str(input_path))
            run("debrief", str(job_id), str(input_path))
            records = json.loads(run("debriefs", str(job_id)))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["answer"], entry["answer"])
            output = root / "preparation"
            run("prep", str(job_id), "--output-dir", str(output))
            markdown = (output / "岗位学习与面试准备.md").read_text(encoding="utf-8")
            for value in [entry["question"], entry["answer"], entry["better_answer"],
                          "fact-sql-analysis", "步骤清晰", "缺少边界情况", "练习去重"]:
                self.assertIn(value, markdown)
            self.assertEqual(repo.get_job(job_id).jd_text, original.jd_text)
            self.assertEqual(repo.get_application(job_id).status, "applied")
            self.assertEqual(len(repo.list_application_events(job_id)), 1)
            self.assertEqual(profile_path.read_bytes(), original_profile)
            repo.verify()

    def test_future_schema_is_rejected_before_any_mutation(self):
        work = Path(__file__).resolve().parents[1] / "work"
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work, prefix="debrief-schema-") as directory:
            path = Path(directory) / "future.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '999')")
            before = path.read_bytes()
            with self.assertRaisesRegex(JobDatabaseError, "不兼容"):
                JobRepository(path).initialize()
            self.assertEqual(path.read_bytes(), before)

    def test_persistence_dedupe_isolation_and_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = JobRepository(Path(directory) / "jobs.sqlite3")
            job_id = repo.upsert_job(make_record()).job_id
            profile = sample_profile()
            entry = InterviewDebriefInput(
                stage="interview_1", question="如何清洗数据？", answer="用 SQL 检查空值。",
                better_answer="先确认口径，再检查空值。", evidence_fact_ids=["fact-sql-analysis"],
                next_actions=["补充异常值练习"],
            )
            with self.assertRaises(InterviewDebriefError):
                record_interview_debrief(repo, profile, job_id, entry)
            repo.record_application_status(job_id, "applied", source="test")
            self.assertTrue(record_interview_debrief(repo, profile, job_id, entry).created)
            self.assertFalse(record_interview_debrief(repo, profile, job_id, entry).created)
            self.assertEqual(repo.get_application(job_id).status, "applied")
            self.assertEqual(len(repo.list_application_events(job_id)), 1)
            with self.assertRaises(InterviewDebriefError):
                record_interview_debrief(repo, profile, job_id, entry.model_copy(
                    update={"evidence_fact_ids": ["fact-pending-tableau"]}))
            entries = JobRepository(repo.path).list_interview_debriefs(job_id)
            self.assertEqual(len(entries), 1)
            self.assertEqual(repo.list_interview_debriefs(job_id + 1), [])
            job = repo.get_job(job_id)
            result = match_job_locally(profile, structure_job_locally(job.jd_text))
            pack = build_preparation_pack(profile, job, result, trigger_status="applied", debriefs=entries)
            self.assertIn("补充异常值练习", render_preparation_pack_markdown(pack))
            repo.verify()

    def test_blank_question_is_rejected(self):
        with self.assertRaises(ValidationError):
            InterviewDebriefInput(stage="interview_1", question="  ", answer="回答")
