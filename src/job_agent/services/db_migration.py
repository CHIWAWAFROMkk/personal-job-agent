from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
import weakref
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


_migration_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_migration_locks_mutex = threading.Lock()


def _get_migration_lock(db_key: str) -> threading.Lock:
    with _migration_locks_mutex:
        lock = _migration_locks.get(db_key)
        if lock is None:
            lock = threading.Lock()
            _migration_locks[db_key] = lock
        return lock



class MigrationError(RuntimeError):
    pass


def backup_database(
    db_path: Path,
    *,
    max_backups: int = 10,
    prefix: str = "job_agent_auto",
) -> Path | None:
    """Create an online atomic SQLite backup and verify its integrity.

    - Flushes WAL frames with a passive checkpoint before snapshot.
    - Generates unique filenames with microsecond timestamps and random tokens to prevent collisions.
    - Rotates only backups matching its own prefix, protecting manual or preflight backups.
    """
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return None
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    unique_token = uuid.uuid4().hex[:6]
    backup_path = backup_dir / f"{prefix}_{timestamp}_{unique_token}.sqlite3.bak"

    source = sqlite3.connect(db_path, timeout=10)
    try:
        try:
            source.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass

        dest = sqlite3.connect(backup_path, timeout=10)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    verify_conn = sqlite3.connect(backup_path, timeout=5)
    try:
        row = verify_conn.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise MigrationError(f"数据库备份完整性校验未通过: {row}")
    finally:
        verify_conn.close()

    # Rotate older backups keeping only the most recent ones for THIS prefix only
    if max_backups > 0:
        try:
            backups = sorted(
                [f for f in backup_dir.glob(f"{prefix}_*.sqlite3.bak") if f.is_file()],
                key=lambda f: f.stat().st_mtime,
            )
            if len(backups) > max_backups:
                for old in backups[:-max_backups]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    return backup_path


def _execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute semicolon-delimited SQL statements without triggering executescript's implicit COMMIT."""
    statements = [s.strip() for s in script.split(";") if s.strip()]
    for stmt in statements:
        conn.execute(stmt)


@dataclass(frozen=True)
class MigrationStep:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]


def _migration_1_base_tables(conn: sqlite3.Connection) -> None:
    _execute_sql_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archived_jobs (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id)
        );

        CREATE TABLE IF NOT EXISTS jobs (
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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS job_sources (
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

        CREATE TABLE IF NOT EXISTS match_results (
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

        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE
                REFERENCES jobs(id) ON DELETE CASCADE,
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

        CREATE TABLE IF NOT EXISTS application_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL
                REFERENCES applications(id) ON DELETE CASCADE,
            previous_status TEXT,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            evidence_path TEXT,
            occurred_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_score
            ON jobs(match_score DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_status
            ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_sources_job
            ON job_sources(job_id);
        CREATE INDEX IF NOT EXISTS idx_matches_job_created
            ON match_results(job_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_applications_status_updated
            ON applications(status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_application_events_application
            ON application_events(application_id, occurred_at DESC);
        """,
    )


def _migration_2_commute_columns(conn: sqlite3.Connection) -> None:
    job_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    commute_columns = {
        "commute_minutes": "INTEGER CHECK(commute_minutes BETWEEN 0 AND 600)",
        "commute_method": "TEXT NOT NULL DEFAULT ''",
        "commute_note": "TEXT NOT NULL DEFAULT ''",
        "commute_origin": "TEXT NOT NULL DEFAULT ''",
        "commute_destination": "TEXT NOT NULL DEFAULT ''",
        "commute_mode": "TEXT NOT NULL DEFAULT ''",
        "commute_distance_meters": (
            "INTEGER CHECK(commute_distance_meters BETWEEN 0 AND 5000000)"
        ),
        "commute_route_summary": "TEXT NOT NULL DEFAULT ''",
        "commute_provider": "TEXT NOT NULL DEFAULT ''",
        "commute_updated_at": "TEXT",
    }
    for column_name, declaration in commute_columns.items():
        if column_name not in job_columns:
            # SQL identifiers cannot be bound; both fragments come only from the
            # hard-coded declarations above, never from database or user input.
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column_name} {declaration}")


def _migration_3_search_candidates(conn: sqlite3.Connection) -> None:
    _execute_sql_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS search_candidates (
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

        CREATE TABLE IF NOT EXISTS search_candidate_sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL
                REFERENCES search_candidates(id) ON DELETE CASCADE,
            sighting_key TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL,
            query TEXT NOT NULL,
            title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            snippet TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_candidates_status_seen
            ON search_candidates(verification_status, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_candidate_sightings_candidate
            ON search_candidate_sightings(candidate_id);
        """,
    )


def _migration_4_tracking_schedule(conn: sqlite3.Connection) -> None:
    _execute_sql_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS tracking_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('oa', 'interview')),
            at TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            completed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(job_id, kind, at)
        );
        CREATE TABLE IF NOT EXISTS tracking_requests (
            request_id TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """,
    )


def _migration_5_interview_debriefs(conn: sqlite3.Connection) -> None:
    _execute_sql_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS interview_debriefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL
                REFERENCES applications(id) ON DELETE CASCADE,
            debrief_hash TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_interview_debriefs_application
            ON interview_debriefs(application_id, created_at DESC);
        """,
    )


def _migration_6_mock_interview(conn: sqlite3.Connection) -> None:
    _execute_sql_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS mock_interview_sessions (
            id TEXT PRIMARY KEY,
            job_id INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mock_interview_requests (
            key TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            session_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mock_interview_job
            ON mock_interview_sessions(job_id);
        """,
    )


MIGRATIONS: tuple[MigrationStep, ...] = (
    MigrationStep(1, "base_tables", _migration_1_base_tables),
    MigrationStep(2, "commute_columns", _migration_2_commute_columns),
    MigrationStep(3, "search_candidates", _migration_3_search_candidates),
    MigrationStep(4, "tracking_schedule", _migration_4_tracking_schedule),
    MigrationStep(5, "interview_debriefs", _migration_5_interview_debriefs),
    MigrationStep(6, "mock_interview", _migration_6_mock_interview),
)


STEP_REQUIRED_TABLES: dict[int, set[str]] = {
    1: {
        "archived_jobs",
        "jobs",
        "job_sources",
        "match_results",
        "applications",
        "application_events",
        "schema_meta",
    },
    3: {"search_candidates", "search_candidate_sightings"},
    4: {"tracking_reminders", "tracking_requests"},
    5: {"interview_debriefs"},
    6: {"mock_interview_sessions", "mock_interview_requests"},
}


def _step_2_commute_columns_exist(conn: sqlite3.Connection) -> bool:
    job_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    required = {
        "commute_minutes", "commute_method", "commute_note", "commute_origin",
        "commute_destination", "commute_mode", "commute_distance_meters",
        "commute_route_summary", "commute_provider", "commute_updated_at"
    }
    return required.issubset(job_cols)


def run_database_migrations(
    conn: sqlite3.Connection,
    *,
    target_version: int | None = None,
    auto_backup_path: Path | None = None,
) -> int:
    """Run pending migrations sequentially inside an atomic transaction.

    - Explicitly rejects databases with higher/unsupported versions; never overwrites with a lower version.
    - Read-only inspection is performed prior to any write; no writes occur before pre-migration backup completes.
    - Does NOT switch journal_mode before backup.
    - If backup fails, aborts immediately leaving database 100% untouched.
    - All writes (including migration ledger creation) are strictly wrapped in a SAVEPOINT transaction for complete rollback.
    """
    db_key = ""
    if auto_backup_path is not None:
        db_key = str(auto_backup_path.resolve())
    else:
        try:
            row = conn.execute("PRAGMA database_list").fetchone()
            if row and len(row) >= 3 and row[2]:
                db_key = str(Path(row[2]).resolve())
        except Exception:
            db_key = ""

    lock = _get_migration_lock(db_key) if db_key else nullcontext()
    with lock:
        conn.execute("PRAGMA foreign_keys = ON")

        max_supported_version = max(step.version for step in MIGRATIONS)
        max_version = target_version or max_supported_version

        # 1. Inspect existing migrations without writing to database yet
        has_migrations_table = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
        )
        if has_migrations_table:
            applied_versions = {
                row[0]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            if any(v > max_supported_version for v in applied_versions):
                max_ledger_ver = max(applied_versions)
                raise MigrationError(
                    f"数据库迁移台账中存在更高版本 ({max_ledger_ver} > 支持上限 {max_supported_version})，拒绝降级以防止损坏新版数据。"
                )
        else:
            applied_versions = set()

        # 2. Inspect schema_meta
        has_meta = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
            ).fetchone()
        )
        legacy_ver: int | None = None
        if has_meta:
            meta_row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if meta_row:
                try:
                    legacy_ver = int(meta_row[0])
                except ValueError:
                    raise MigrationError(
                        f"数据库版本标识无效: '{meta_row[0]}'，拒绝迁移以防止损坏数据。"
                    )
                if legacy_ver > max_supported_version:
                    raise MigrationError(
                        f"数据库版本 ({legacy_ver}) 高于当前程序支持的最大版本 ({max_supported_version})，拒绝降级以防止损坏新版数据。"
                    )
                if not applied_versions:
                    for step in MIGRATIONS:
                        if step.version <= legacy_ver:
                            applied_versions.add(step.version)

        # 3. Detect "台账存在但对应表/字段缺失"
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for v in list(applied_versions):
            if v in STEP_REQUIRED_TABLES:
                missing = STEP_REQUIRED_TABLES[v] - existing_tables
                if missing:
                    logger.warning("Step %d recorded as applied but required tables missing %s; re-queuing.", v, missing)
                    applied_versions.discard(v)
            if v == 2 and "jobs" in existing_tables:
                if not _step_2_commute_columns_exist(conn):
                    logger.warning("Step 2 recorded as applied but commute columns missing; re-queuing.")
                    applied_versions.discard(2)

        # Check if database has any existing content
        db_has_content = bool(existing_tables) or (
            auto_backup_path is not None
            and auto_backup_path.is_file()
            and auto_backup_path.stat().st_size > 0
        )

        pending = [
            step
            for step in MIGRATIONS
            if step.version not in applied_versions and step.version <= max_version
        ]

        # Check if schema_meta needs update
        meta_needs_update = (
            not has_meta
            or legacy_ver is None
            or (legacy_ver < max_version and not pending)
        )

        # If nothing pending and meta already current, absolutely zero writes needed
        if not pending and not meta_needs_update:
            return len(applied_versions)

        # 4. If database has content and writes are needed, mandatory pre-migration backup must succeed BEFORE ANY WRITES
        if db_has_content and auto_backup_path is not None and auto_backup_path.is_file() and auto_backup_path.stat().st_size > 0:
            try:
                backup_database(auto_backup_path, prefix="job_agent_pre_migration")
            except Exception as exc:
                raise MigrationError(
                    f"数据库升级前备份失败，已终止迁移以防止数据损坏: {exc}"
                ) from exc

        # Only after backup succeeds, ensure WAL mode before writing
        try:
            current_mode = conn.execute("PRAGMA journal_mode").fetchone()
            if current_mode and str(current_mode[0]).lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass

        # 5. ALL WRITES INSIDE ATOMIC SAVEPOINT TRANSACTION
        now_iso = datetime.now(UTC).isoformat()
        conn.execute("SAVEPOINT migration_batch")
        try:
            # Ensure schema_migrations table exists
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )

            # Record already applied versions in schema_migrations
            for v in applied_versions:
                name = next((s.name for s in MIGRATIONS if s.version == v), f"v{v}")
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (v, name, now_iso),
                )

            # If rollbacks occurred in test scenarios (legacy_ver < max_version), clean excess ledger entries inside transaction
            if legacy_ver is not None and legacy_ver < max_version:
                conn.execute(
                    "DELETE FROM schema_migrations WHERE version > ?",
                    (max_version,),
                )

            # Apply pending migrations
            for step in pending:
                step_already_complete = False
                in_ledger = bool(
                    conn.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?", (step.version,)
                    ).fetchone()
                )
                if in_ledger:
                    current_tables = {
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    }
                    if step.version in STEP_REQUIRED_TABLES:
                        if STEP_REQUIRED_TABLES[step.version].issubset(current_tables):
                            step_already_complete = True
                    elif step.version == 2:
                        if "jobs" in current_tables and _step_2_commute_columns_exist(conn):
                            step_already_complete = True
                    else:
                        step_already_complete = True

                if step_already_complete:
                    continue

                logger.info("Applying database migration v%d: %s", step.version, step.name)
                step.up(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (step.version, step.name, now_iso),
                )
                applied_versions.add(step.version)

            # Synchronize schema_meta within transaction (never lower an existing higher version)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            curr_meta = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if not curr_meta or int(curr_meta[0]) < max_version:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(max_version),),
                )

            conn.execute("RELEASE SAVEPOINT migration_batch")
        except Exception:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT migration_batch")
                conn.execute("RELEASE SAVEPOINT migration_batch")
            except Exception:
                pass
            raise

        return len(applied_versions)
