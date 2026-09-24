from __future__ import annotations

import concurrent.futures
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.constants import SCHEMA_VERSION
from job_agent.services.db_migration import (
    MIGRATIONS,
    MigrationError,
    MigrationStep,
    backup_database,
    run_database_migrations,
)
from job_agent.services.job_repository import JobDatabaseError, JobRepository
from tests.test_job_repository import make_record


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "jobs.sqlite3"

    def test_backup_creates_valid_integrity_checked_file(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO sample (name) VALUES ('test_item')")
        conn.commit()
        conn.close()

        backup_file = backup_database(self.db_path)
        self.assertIsNotNone(backup_file)
        self.assertTrue(backup_file.is_file())
        self.assertEqual(backup_file.parent, self.root / "backups")

        verify_conn = sqlite3.connect(backup_file)
        try:
            row = verify_conn.execute("PRAGMA integrity_check").fetchone()
            self.assertEqual(str(row[0]).lower(), "ok")
            data = verify_conn.execute("SELECT name FROM sample").fetchone()
            self.assertEqual(data[0], "test_item")
        finally:
            verify_conn.close()

    def test_backup_failure_aborts_migration_and_does_not_mutate_db(self):
        # Create an existing database with a table
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE initial_data (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO initial_data VALUES (1, 'keep_me')")
        conn.commit()
        conn.close()

        # Mock backup failure
        with patch("job_agent.services.db_migration.backup_database", side_effect=RuntimeError("Disk I/O failure simulated")):
            conn = sqlite3.connect(self.db_path)
            try:
                with self.assertRaises(MigrationError) as ctx:
                    run_database_migrations(conn, auto_backup_path=self.db_path)
                self.assertIn("数据库升级前备份失败", str(ctx.exception))
            finally:
                conn.close()

        # Verify database was NOT mutated: no new tables were created
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertEqual(tables, {"initial_data"})
            data = conn.execute("SELECT val FROM initial_data WHERE id=1").fetchone()
            self.assertEqual(data[0], "keep_me")
        finally:
            conn.close()

    def test_backup_rotation_only_touches_own_prefix(self):
        backup_dir = self.root / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Create manual/preflight backups that must NOT be touched
        manual_preflight = backup_dir / "job_agent_preflight_20260920.sqlite3.bak"
        manual_preflight.write_text("manual backup content", encoding="utf-8")
        custom_backup = backup_dir / "custom_user_snapshot.sqlite3.bak"
        custom_backup.write_text("user backup content", encoding="utf-8")

        # Create existing database
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE sample (id INT)")
        conn.execute("INSERT INTO sample VALUES (1)")
        conn.commit()
        conn.close()

        # Generate 6 backups with prefix "test_auto" and max_backups=3
        created_backups = []
        for _ in range(6):
            b = backup_database(self.db_path, max_backups=3, prefix="test_auto")
            self.assertIsNotNone(b)
            created_backups.append(b)

        # Manual backups must still exist intact
        self.assertTrue(manual_preflight.is_file())
        self.assertEqual(manual_preflight.read_text(encoding="utf-8"), "manual backup content")
        self.assertTrue(custom_backup.is_file())
        self.assertEqual(custom_backup.read_text(encoding="utf-8"), "user backup content")

        # Only 3 backups of prefix "test_auto" should remain
        test_auto_backups = list(backup_dir.glob("test_auto_*.sqlite3.bak"))
        self.assertEqual(len(test_auto_backups), 3)

    def test_backup_unique_naming_under_rapid_calls(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE sample (id INT)")
        conn.execute("INSERT INTO sample VALUES (1)")
        conn.commit()
        conn.close()

        backups = [
            backup_database(self.db_path, max_backups=20, prefix="rapid")
            for _ in range(10)
        ]
        unique_paths = set(backups)
        self.assertEqual(len(unique_paths), 10)
        for p in unique_paths:
            self.assertTrue(p.is_file())

    def test_migration_failure_rolls_back_atomically(self):
        conn = sqlite3.connect(self.db_path)

        def failing_step(c: sqlite3.Connection) -> None:
            c.execute("CREATE TABLE should_be_rolled_back (id INT)")
            raise RuntimeError("Injected step failure")

        test_migrations = (
            MigrationStep(1, "base", lambda c: c.execute("CREATE TABLE step1 (id INT)")),
            MigrationStep(2, "fail", failing_step),
        )

        with patch("job_agent.services.db_migration.MIGRATIONS", test_migrations):
            with self.assertRaises(RuntimeError):
                run_database_migrations(conn, target_version=2)

        # Verify step2 table was completely rolled back and step1 was also rolled back
        # because the transaction was atomic across the pending batch.
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        self.assertNotIn("should_be_rolled_back", tables)
        self.assertNotIn("step1", tables)
        conn.close()

    def test_concurrent_initialization(self):
        repo = JobRepository(self.db_path)

        def init_repo():
            r = JobRepository(self.db_path)
            r.initialize()
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(init_repo) for _ in range(8)]
            results = [f.result() for f in futures]

        self.assertEqual(len(results), 8)
        self.assertTrue(all(results))

        conn = sqlite3.connect(self.db_path)
        try:
            versions = [
                row[0]
                for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            ]
            self.assertEqual(versions, [1, 2, 3, 4, 5, 6])
        finally:
            conn.close()

    def test_legacy_v5_full_schema_simulation_and_reopen(self):
        # 1. Faithfully simulate full legacy v5 schema with all 13 tables
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '5');

            CREATE TABLE archived_jobs (job_id INTEGER PRIMARY KEY REFERENCES jobs(id));

            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                company TEXT NOT NULL,
                title TEXT NOT NULL,
                location TEXT,
                jd_text TEXT NOT NULL,
                published_at TEXT,
                deadline_at TEXT,
                status TEXT NOT NULL DEFAULT 'discovered',
                match_score INTEGER CHECK(match_score BETWEEN 0 AND 100),
                recommendation TEXT,
                commute_minutes INTEGER CHECK(commute_minutes BETWEEN 0 AND 600),
                commute_method TEXT NOT NULL DEFAULT '',
                commute_note TEXT NOT NULL DEFAULT '',
                commute_origin TEXT NOT NULL DEFAULT '',
                commute_destination TEXT NOT NULL DEFAULT '',
                commute_mode TEXT NOT NULL DEFAULT '',
                commute_distance_meters INTEGER CHECK(commute_distance_meters BETWEEN 0 AND 5000000),
                commute_route_summary TEXT NOT NULL DEFAULT '',
                commute_provider TEXT NOT NULL DEFAULT '',
                commute_updated_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE job_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                source_key TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL,
                source_url TEXT,
                external_id TEXT,
                jd_text TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE match_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                result_hash TEXT NOT NULL UNIQUE,
                score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
                recommendation TEXT NOT NULL,
                engine TEXT NOT NULL,
                scoring_version TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                resume_path TEXT,
                application_pack_path TEXT,
                source_url TEXT,
                applied_at TEXT,
                last_verified_at TEXT,
                verification_method TEXT NOT NULL DEFAULT '',
                evidence_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE application_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                previous_status TEXT,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                evidence_path TEXT,
                occurred_at TEXT NOT NULL
            );

            CREATE TABLE interview_debriefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                debrief_hash TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE tracking_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('oa', 'interview')),
                at TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                completed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, kind, at)
            );

            CREATE TABLE tracking_requests (
                request_id TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE search_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_key TEXT NOT NULL UNIQUE,
                canonical_url TEXT NOT NULL,
                title TEXT NOT NULL,
                snippet TEXT NOT NULL DEFAULT '',
                verification_status TEXT NOT NULL DEFAULT 'pending',
                verification_detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                verified_at TEXT
            );

            CREATE TABLE search_candidate_sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES search_candidates(id) ON DELETE CASCADE,
                sighting_key TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                query TEXT NOT NULL,
                title TEXT NOT NULL,
                source_url TEXT NOT NULL,
                snippet TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            INSERT INTO jobs (
                id, dedupe_key, company, title, location, jd_text, created_at, updated_at, first_seen_at, last_seen_at
            ) VALUES (
                1, 'legacy_job', '老字号科技', '高级算法专家', '北京', '全流程大模型优化', '2026-09-01', '2026-09-01', '2026-09-01', '2026-09-01'
            );
            INSERT INTO applications (id, job_id, status, created_at, updated_at) VALUES (1, 1, 'applied', '2026-09-01', '2026-09-01');
            INSERT INTO interview_debriefs (id, application_id, debrief_hash, payload_json, created_at)
            VALUES (1, 1, 'hash123', '{"stage": "interview_1", "question": "数据清洗步骤？", "answer": "先确认口径再处理缺失"}', '2026-09-01');
            """
        )
        conn.close()

        # 2. Reopen using JobRepository and trigger migration
        repo = JobRepository(self.db_path)
        repo.initialize()

        # 3. Verify original data preserved
        job = repo.get_job(1)
        self.assertEqual(job.company, "老字号科技")
        self.assertEqual(job.title, "高级算法专家")

        debriefs = repo.list_interview_debriefs(1)
        self.assertEqual(len(debriefs), 1)

        # 4. Verify v6 tables now available and writable
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("INSERT INTO mock_interview_sessions VALUES ('sess_1', 1, '{\"state\": \"ok\"}')")
            conn.commit()
            row = conn.execute("SELECT data FROM mock_interview_sessions WHERE id='sess_1'").fetchone()
            self.assertEqual(row[0], '{"state": "ok"}')

            meta_ver = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            self.assertEqual(meta_ver[0], "6")
        finally:
            conn.close()

    def test_backup_restore_round_trip(self):
        repo = JobRepository(self.db_path)
        repo.initialize()
        repo.upsert_job(
            make_record(
                company="备份测试公司",
                title="SRE工程师",
                location="上海",
            )
        )
        original_job = repo.get_job(1)
        self.assertEqual(original_job.company, "备份测试公司")

        # Take backup
        backup_file = backup_database(self.db_path, prefix="test_restore")
        self.assertIsNotNone(backup_file)

        # Corrupt original database
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())

        # Restore from backup
        shutil.copy2(backup_file, self.db_path)
        self.assertTrue(self.db_path.exists())

        # Reopen and verify full data fidelity
        restored_repo = JobRepository(self.db_path)
        restored_job = restored_repo.get_job(1)
        self.assertEqual(restored_job.company, "备份测试公司")
        self.assertEqual(restored_job.title, "SRE工程师")

    def test_missing_table_despite_ledger_record_triggers_recreation(self):
        # 1. Initialize full v6 schema
        repo = JobRepository(self.db_path)
        repo.initialize()
        repo.upsert_job(
            make_record(
                company="稳健科技",
                title="数据架构师",
                location="深圳",
            )
        )

        # 2. Corrupt state: drop table mock_interview_sessions while ledger records v6 as applied
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE mock_interview_sessions")
        conn.commit()
        # Verify ledger still says 6 is applied but table is missing
        tables_before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertNotIn("mock_interview_sessions", tables_before)
        versions_before = [r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()]
        self.assertIn(6, versions_before)
        conn.close()

        # 3. Reopen via JobRepository and initialize
        reopened_repo = JobRepository(self.db_path)
        reopened_repo.initialize()

        # 4. Verify mock_interview_sessions is recreated and operational
        conn = sqlite3.connect(self.db_path)
        try:
            tables_after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("mock_interview_sessions", tables_after)
            # Verify existing data preserved
            job = reopened_repo.get_job(1)
            self.assertEqual(job.company, "稳健科技")
            # Verify newly recreated table writable
            conn.execute("INSERT INTO mock_interview_sessions VALUES ('sess_recover', 1, '{\"status\": \"ok\"}')")
            conn.commit()
            val = conn.execute("SELECT data FROM mock_interview_sessions WHERE id='sess_recover'").fetchone()[0]
            self.assertEqual(val, '{"status": "ok"}')
        finally:
            conn.close()

    def test_two_independent_processes_concurrent_initialization(self):
        worker_code = f"""
import sys
from pathlib import Path
from job_agent.services.job_repository import JobRepository

db = Path(r"{self.db_path}")
repo = JobRepository(db)
repo.initialize()
print("SUCCESS")
"""
        p1 = subprocess.Popen([sys.executable, "-c", worker_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p2 = subprocess.Popen([sys.executable, "-c", worker_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out1, err1 = p1.communicate(timeout=30)
        out2, err2 = p2.communicate(timeout=30)

        self.assertEqual(p1.returncode, 0, f"Process 1 failed: {err1}")
        self.assertEqual(p2.returncode, 0, f"Process 2 failed: {err2}")
        self.assertIn("SUCCESS", out1)
        self.assertIn("SUCCESS", out2)

        # Verify database is properly initialized to v6
        conn = sqlite3.connect(self.db_path)
        try:
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
            self.assertEqual(versions, [1, 2, 3, 4, 5, 6])
            meta_ver = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(meta_ver, "6")
        finally:
            conn.close()

    def test_backup_failure_aborts_and_leaves_logical_state_unchanged(self):
        # 1. Create existing database with specific legacy data
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '5');
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, company TEXT, title TEXT);
            INSERT INTO jobs VALUES (1, '原封不动科技', '主任架构师');
            CREATE TABLE applications (id INTEGER PRIMARY KEY, job_id INTEGER, status TEXT);
            INSERT INTO applications VALUES (10, 1, 'interview_scheduled');
            """
        )
        conn.close()

        # Capture complete logical state before
        conn = sqlite3.connect(self.db_path)
        tables_before = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        schema_before = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        jobs_before = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        apps_before = conn.execute("SELECT * FROM applications ORDER BY id").fetchall()
        meta_before = conn.execute("SELECT * FROM schema_meta ORDER BY key").fetchall()
        conn.close()

        # 2. Mock backup failure
        repo = JobRepository(self.db_path)
        with patch("job_agent.services.db_migration.backup_database", side_effect=PermissionError("Backup disk full")):
            with self.assertRaises(JobDatabaseError) as ctx:
                repo.initialize()
            self.assertIn("备份失败", str(ctx.exception))

        # 3. Verify logical state is 100% identical
        conn = sqlite3.connect(self.db_path)
        try:
            tables_after = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            schema_after = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
            jobs_after = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            apps_after = conn.execute("SELECT * FROM applications ORDER BY id").fetchall()
            meta_after = conn.execute("SELECT * FROM schema_meta ORDER BY key").fetchall()

            self.assertEqual(tables_after, tables_before)
            self.assertEqual(schema_after, schema_before)
            self.assertEqual(jobs_after, jobs_before)
            self.assertEqual(apps_after, apps_before)
            self.assertEqual(meta_after, meta_before)
            self.assertNotIn("mock_interview_sessions", tables_after)
            self.assertNotIn("schema_migrations", tables_after)
        finally:
            conn.close()

    def test_reject_unsupported_higher_version_database_without_downgrade(self):
        # 1. Test higher version in schema_meta
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '99');
            CREATE TABLE future_table (id INTEGER PRIMARY KEY, future_col TEXT);
            INSERT INTO future_table VALUES (1, 'future_data');
            """
        )
        conn.close()

        repo = JobRepository(self.db_path)
        with self.assertRaises(JobDatabaseError) as ctx:
            repo.initialize()
        self.assertIn("不兼容", str(ctx.exception))

        # Verify schema_version was NOT downgraded to 6 or overwritten
        conn = sqlite3.connect(self.db_path)
        try:
            meta_val = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(meta_val, "99")
            fut_val = conn.execute("SELECT future_col FROM future_table WHERE id=1").fetchone()[0]
            self.assertEqual(fut_val, "future_data")
        finally:
            conn.close()

        # 2. Test higher version in schema_migrations ledger directly
        db2 = self.root / "jobs_ledger_high.sqlite3"
        conn2 = sqlite3.connect(db2)
        conn2.executescript(
            """
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
            INSERT INTO schema_migrations VALUES (7, 'future_v7', '2026-10-01');
            """
        )
        conn2.close()

        conn_test = sqlite3.connect(db2)
        try:
            with self.assertRaises(MigrationError) as ctx:
                run_database_migrations(conn_test, auto_backup_path=db2)
            self.assertIn("存在更高版本", str(ctx.exception))
            # Verify version 7 is preserved and not deleted or lowered
            ledger_versions = [r[0] for r in conn_test.execute("SELECT version FROM schema_migrations").fetchall()]
            self.assertEqual(ledger_versions, [7])
        finally:
            conn_test.close()

    def test_missing_commute_columns_repaired_despite_ledger_record(self):
        # 1. Create a database where Step 1 base tables exist, but commute columns lack, while schema_migrations records v2
        conn = sqlite3.connect(self.db_path)
        from job_agent.services.db_migration import _migration_1_base_tables
        _migration_1_base_tables(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
            """
        )
        conn.execute("INSERT OR REPLACE INTO schema_migrations VALUES (1, 'base_tables', '2026-09-01')")
        conn.execute("INSERT OR REPLACE INTO schema_migrations VALUES (2, 'commute_columns', '2026-09-01')")
        conn.execute("INSERT OR REPLACE INTO schema_meta VALUES ('schema_version', '2')")
        conn.execute(
            "INSERT INTO jobs (dedupe_key, company, title, jd_text, created_at, updated_at, first_seen_at, last_seen_at) "
            "VALUES ('k1', '通勤科技', '后端工程师', '高并发', '2026-09-01', '2026-09-01', '2026-09-01', '2026-09-01')"
        )
        conn.commit()

        # Verify commute columns do not exist yet
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        self.assertNotIn("commute_minutes", cols_before)
        self.assertNotIn("commute_method", cols_before)
        conn.close()

        # 2. Reopen and initialize via JobRepository
        repo = JobRepository(self.db_path)
        repo.initialize()

        # 3. Verify actual structure, not just version
        conn = sqlite3.connect(self.db_path)
        try:
            cols_after = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            expected_commute_cols = {
                "commute_minutes", "commute_method", "commute_note", "commute_origin",
                "commute_destination", "commute_mode", "commute_distance_meters",
                "commute_route_summary", "commute_provider", "commute_updated_at"
            }
            self.assertTrue(expected_commute_cols.issubset(cols_after), f"Missing cols: {expected_commute_cols - cols_after}")

            # Verify actual DML write and read on the newly added columns
            conn.execute(
                "UPDATE jobs SET commute_minutes = 35, commute_method = 'subway_line_2' WHERE id = 1"
            )
            conn.commit()
            row = conn.execute("SELECT commute_minutes, commute_method FROM jobs WHERE id = 1").fetchone()
            self.assertEqual(row[0], 35)
            self.assertEqual(row[1], "subway_line_2")
        finally:
            conn.close()

    def test_missing_mock_interview_requests_repaired_despite_ledger_record(self):
        # 1. Fully initialize database
        repo = JobRepository(self.db_path)
        repo.initialize()

        # 2. Corrupt state: drop ONLY mock_interview_requests while mock_interview_sessions remains and ledger has v6
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE mock_interview_requests")
        conn.commit()
        tables_before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertNotIn("mock_interview_requests", tables_before)
        self.assertIn("mock_interview_sessions", tables_before)
        conn.close()

        # 3. Reopen and initialize
        reopened_repo = JobRepository(self.db_path)
        reopened_repo.initialize()

        # 4. Verify actual structure
        conn = sqlite3.connect(self.db_path)
        try:
            tables_after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("mock_interview_requests", tables_after)
            self.assertIn("mock_interview_sessions", tables_after)

            # Test actual DML operations on mock_interview_requests
            conn.execute("INSERT INTO mock_interview_requests VALUES ('req_1', 'fp_test', 'sess_abc')")
            conn.commit()
            res = conn.execute("SELECT fingerprint, session_id FROM mock_interview_requests WHERE key = 'req_1'").fetchone()
            self.assertEqual(res[0], "fp_test")
            self.assertEqual(res[1], "sess_abc")
        finally:
            conn.close()

    def test_no_journal_mode_switch_before_backup_on_failure(self):
        # 1. Create a database explicitly in DELETE journal mode
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("CREATE TABLE pre_data (id INT)")
        conn.execute("INSERT INTO pre_data VALUES (100)")
        conn.commit()
        mode_before = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode_before.lower(), "delete")
        conn.close()

        # 2. Trigger migration with mock backup failure
        repo = JobRepository(self.db_path)
        with patch("job_agent.services.db_migration.backup_database", side_effect=PermissionError("Simulated backup fail")):
            with self.assertRaises(JobDatabaseError):
                repo.initialize()

        # 3. Check journal mode: must NOT have been switched to WAL
        conn = sqlite3.connect(self.db_path)
        try:
            mode_after = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode_after.lower(), "delete", "Journal mode was prematurely switched before backup succeeded!")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

