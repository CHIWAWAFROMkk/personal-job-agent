from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import weakref
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_agent.models.application_tracking import (
    ApplicationEvent,
    ApplicationListItem,
    ApplicationRecord,
    ApplicationSummary,
    ApplicationStatus,
)
from job_agent.models.commute import CommuteRouteResult
from job_agent.models.interview_debrief import (
    InterviewDebriefInput,
    InterviewDebriefRecord,
    InterviewDebriefSaveResult,
)
from job_agent.models.job import MatchResult
from job_agent.services.tracking_parser import safe_public_url
from job_agent.models.job_record import (
    CandidateVerificationStatus,
    JobDatabaseStats,
    JobDetail,
    JobListItem,
    JobRecordInput,
    JobSourceItem,
    JobUpsertResult,
    SearchCandidateInput,
    SearchCandidateListItem,
    SearchCandidateUpsertResult,
)


from job_agent.constants import (
    SCHEMA_VERSION,
    COMPATIBLE_SCHEMA_VERSIONS,
    COMPANY_SUFFIXES,
    CITY_NAMES,
    TRACKING_QUERY_KEYS,
    CANDIDATE_VERIFICATION_STATUSES,
    APPLICATION_STATUSES,
    SUBMITTED_APPLICATION_STATUSES,
    APPLICATION_STAGE_RANK,
    TERMINAL_APPLICATION_STATUSES,
)



class JobDatabaseError(RuntimeError):
    pass


def _is_application_status_regression(previous: str, current: str) -> bool:
    if previous == current or current in TERMINAL_APPLICATION_STATUSES:
        return False
    if previous in TERMINAL_APPLICATION_STATUSES:
        return True
    previous_rank = APPLICATION_STAGE_RANK.get(previous)
    current_rank = APPLICATION_STAGE_RANK.get(current)
    return (
        previous_rank is not None
        and current_rank is not None
        and current_rank < previous_rank
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _compact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def normalize_company(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(r"[（(].*?(?:中国|china).*?[）)]", "", normalized)
    compact = _compact_text(normalized)
    for suffix in COMPANY_SUFFIXES:
        suffix_compact = _compact_text(suffix)
        if compact.endswith(suffix_compact) and len(compact) > len(suffix_compact):
            compact = compact[: -len(suffix_compact)]
            break
    return compact


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(
        r"[（(\[]\s*[a-z]{0,3}[-_]?\d{4,}\s*[）)\]]",
        "",
        normalized,
        flags=re.I,
    )
    return _compact_text(normalized)


def normalize_location(value: str | None) -> str:
    if not value:
        return "未注明"
    for city in CITY_NAMES:
        if city in value:
            return city
    return _compact_text(value) or "未注明"


def build_dedupe_key(company: str, title: str, location: str | None) -> str:
    identity = "|".join(
        (
            "v1",
            normalize_company(company),
            normalize_title(title),
            normalize_location(location),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def canonicalize_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return value.strip()
    filtered_query = []
    for key, item_value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        filtered_query.append((key, item_value))
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path.rstrip("/") or "/",
            urlencode(sorted(filtered_query)),
            "",
        )
    )


def build_source_key(record: JobRecordInput, dedupe_key: str) -> str:
    canonical_url = canonicalize_url(record.source_url)
    identity = "|".join(
        (
            _compact_text(record.source),
            canonical_url or "",
            record.external_id or "",
            dedupe_key if not canonical_url and not record.external_id else "",
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build_candidate_key(candidate: SearchCandidateInput) -> tuple[str, str]:
    canonical_url = canonicalize_url(candidate.url) or candidate.url.strip()
    identity = f"v1|{canonical_url}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest(), canonical_url


def build_candidate_sighting_key(
    candidate: SearchCandidateInput,
    candidate_key: str,
) -> str:
    identity = "|".join(
        (
            "v1",
            candidate_key,
            _compact_text(candidate.provider),
            unicodedata.normalize("NFKC", candidate.query).casefold().strip(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class JobRepository:
    # A caller holds its lock strongly while initializing; unused test/data paths
    # can leave the cache without evicting a lock held by another thread.
    _init_locks: weakref.WeakValueDictionary[Path, threading.Lock] = weakref.WeakValueDictionary()
    _init_locks_mutex = threading.Lock()

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    @classmethod
    def _get_init_lock(cls, path: Path) -> threading.Lock:
        with cls._init_locks_mutex:
            resolved = path.expanduser().resolve()
            lock = cls._init_locks.get(resolved)
            if lock is None:
                lock = threading.Lock()
                cls._init_locks[resolved] = lock
            return lock

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> Path:
        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                with self._get_init_lock(self.path):
                    with self._connection() as connection:
                        has_metadata = connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
                        ).fetchone()
                        existing = connection.execute(
                            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                        ).fetchone() if has_metadata else None
                        if existing:
                            try:
                                ver_int = int(existing["value"])
                                if ver_int > int(SCHEMA_VERSION) or existing["value"] not in COMPATIBLE_SCHEMA_VERSIONS:
                                    raise JobDatabaseError(
                                        "数据库版本不兼容："
                                        f"当前 {existing['value']}，程序要求 {SCHEMA_VERSION}。"
                                    )
                            except ValueError:
                                raise JobDatabaseError(
                                    f"数据库版本标识无效: '{existing['value']}'。"
                                )
                        from job_agent.services.db_migration import MigrationError, run_database_migrations
                        try:
                            run_database_migrations(connection, auto_backup_path=self.path)
                        except MigrationError as exc:
                            raise JobDatabaseError(str(exc)) from exc
                return self.path
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempt < max_attempts - 1:
                    time.sleep(0.15 * (2 ** attempt))
                    continue
                raise
        return self.path

    def upsert_job(self, record: JobRecordInput) -> JobUpsertResult:
        self.initialize()
        now = _utc_now()
        dedupe_key = build_dedupe_key(record.company, record.title, record.location)
        source_key = build_source_key(record, dedupe_key)
        canonical_url = canonicalize_url(record.source_url)

        with self._connection() as connection:
            source_row = connection.execute(
                "SELECT job_id FROM job_sources WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            created = False
            source_added = False
            if source_row:
                job_id = int(source_row["job_id"])
                dedupe_reason = "same_source"
            else:
                job_row = connection.execute(
                    "SELECT id FROM jobs WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if job_row:
                    job_id = int(job_row["id"])
                    dedupe_reason = "same_company_title_location"
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO jobs(
                            dedupe_key, company, title, location, jd_text,
                            published_at, deadline_at, created_at, updated_at,
                            first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            dedupe_key,
                            record.company,
                            record.title,
                            record.location,
                            record.jd_text,
                            record.published_at,
                            record.deadline_at,
                            now,
                            now,
                            now,
                            now,
                        ),
                    )
                    job_id = int(cursor.lastrowid)
                    created = True
                    dedupe_reason = "new_job"

            existing_job = connection.execute(
                "SELECT jd_text, published_at, deadline_at FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            assert existing_job is not None
            preferred_jd = (
                record.jd_text
                if len(record.jd_text) > len(existing_job["jd_text"])
                else existing_job["jd_text"]
            )
            connection.execute(
                """
                UPDATE jobs
                SET jd_text = ?,
                    published_at = COALESCE(published_at, ?),
                    deadline_at = COALESCE(?, deadline_at),
                    updated_at = ?,
                    last_seen_at = ?
                WHERE id = ?
                """,
                (
                    preferred_jd,
                    record.published_at,
                    record.deadline_at,
                    now,
                    now,
                    job_id,
                ),
            )

            if source_row:
                connection.execute(
                    """
                    UPDATE job_sources
                    SET last_seen_at = ?,
                        jd_text = CASE
                            WHEN length(?) > length(jd_text) THEN ?
                            ELSE jd_text
                        END
                    WHERE source_key = ?
                    """,
                    (now, record.jd_text, record.jd_text, source_key),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO job_sources(
                        job_id, source_key, platform, source_url, external_id,
                        jd_text, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        source_key,
                        record.source,
                        canonical_url,
                        record.external_id,
                        record.jd_text,
                        now,
                        now,
                    ),
                )
                source_added = True

        return JobUpsertResult(
            job_id=job_id,
            created=created,
            source_added=source_added,
            dedupe_key=dedupe_key,
            dedupe_reason=dedupe_reason,
        )

    def upsert_search_candidate(
        self,
        candidate: SearchCandidateInput,
    ) -> SearchCandidateUpsertResult:
        self.initialize()
        now = _utc_now()
        candidate_key, canonical_url = build_candidate_key(candidate)
        sighting_key = build_candidate_sighting_key(candidate, candidate_key)

        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, title, snippet FROM search_candidates WHERE candidate_key = ?",
                (candidate_key,),
            ).fetchone()
            created = row is None
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO search_candidates(
                        candidate_key, canonical_url, title, snippet,
                        created_at, updated_at, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_key,
                        canonical_url,
                        candidate.title,
                        candidate.snippet,
                        now,
                        now,
                        now,
                        now,
                    ),
                )
                candidate_id = int(cursor.lastrowid)
            else:
                candidate_id = int(row["id"])
                preferred_title = (
                    candidate.title
                    if len(candidate.title) > len(row["title"])
                    else row["title"]
                )
                preferred_snippet = (
                    candidate.snippet
                    if len(candidate.snippet) > len(row["snippet"])
                    else row["snippet"]
                )
                connection.execute(
                    """
                    UPDATE search_candidates
                    SET title = ?, snippet = ?, updated_at = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (preferred_title, preferred_snippet, now, now, candidate_id),
                )

            sighting = connection.execute(
                "SELECT id FROM search_candidate_sightings WHERE sighting_key = ?",
                (sighting_key,),
            ).fetchone()
            sighting_added = sighting is None
            if sighting is None:
                connection.execute(
                    """
                    INSERT INTO search_candidate_sightings(
                        candidate_id, sighting_key, provider, query, title,
                        source_url, snippet, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        sighting_key,
                        candidate.provider,
                        candidate.query,
                        candidate.title,
                        candidate.url,
                        candidate.snippet,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE search_candidate_sightings
                    SET title = ?, snippet = ?, source_url = ?, last_seen_at = ?
                    WHERE sighting_key = ?
                    """,
                    (
                        candidate.title,
                        candidate.snippet,
                        candidate.url,
                        now,
                        sighting_key,
                    ),
                )

        return SearchCandidateUpsertResult(
            candidate_id=candidate_id,
            created=created,
            sighting_added=sighting_added,
            canonical_url=canonical_url,
        )

    def mark_candidate_verification(
        self,
        candidate_id: int,
        status: CandidateVerificationStatus,
        *,
        detail: str = "",
    ) -> None:
        self.initialize()
        if status not in CANDIDATE_VERIFICATION_STATUSES:
            raise JobDatabaseError(f"不支持的候选核验状态: {status}")
        now = _utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE search_candidates
                SET verification_status = ?, verification_detail = ?,
                    verified_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, detail.strip(), now, now, candidate_id),
            )
        if cursor.rowcount == 0:
            raise JobDatabaseError(f"候选岗位不存在: #{candidate_id}")

    def list_search_candidates(
        self,
        *,
        status: CandidateVerificationStatus | None = None,
        limit: int | None = 20,
        offset: int = 0,
    ) -> list[SearchCandidateListItem]:
        self.initialize()
        if status is not None and status not in CANDIDATE_VERIFICATION_STATUSES:
            raise JobDatabaseError(f"不支持的候选核验状态: {status}")
        if offset < 0 or (offset and limit is None):
            raise JobDatabaseError("候选岗位分页参数无效")
        where_clause = "WHERE search_candidates.verification_status = ?" if status else ""
        parameters: tuple[object, ...] = (status,) if status else ()
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ? OFFSET ?"
            parameters += (limit, offset)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    search_candidates.id,
                    search_candidates.title,
                    search_candidates.canonical_url,
                    search_candidates.snippet,
                    search_candidates.verification_status,
                    search_candidates.verification_detail,
                    search_candidates.first_seen_at,
                    search_candidates.last_seen_at,
                    search_candidates.verified_at,
                    COUNT(search_candidate_sightings.id) AS source_count
                FROM search_candidates
                LEFT JOIN search_candidate_sightings
                    ON search_candidate_sightings.candidate_id = search_candidates.id
                {where_clause}
                GROUP BY search_candidates.id
                ORDER BY search_candidates.last_seen_at DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        return [
            SearchCandidateListItem(
                candidate_id=int(row["id"]),
                title=row["title"],
                url=row["canonical_url"],
                snippet=row["snippet"],
                verification_status=row["verification_status"],
                verification_detail=row["verification_detail"],
                source_count=int(row["source_count"]),
                first_seen_at=row["first_seen_at"],
                last_seen_at=row["last_seen_at"],
                verified_at=row["verified_at"],
            )
            for row in rows
        ]

    def count_search_candidates(
        self, *, status: CandidateVerificationStatus | None = None
    ) -> int:
        self.initialize()
        if status is not None and status not in CANDIDATE_VERIFICATION_STATUSES:
            raise JobDatabaseError(f"不支持的候选核验状态: {status}")
        with self._connection() as connection:
            if status is None:
                row = connection.execute("SELECT COUNT(*) FROM search_candidates").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM search_candidates WHERE verification_status = ?",
                    (status,),
                ).fetchone()
        return int(row[0])

    def add_match_result(self, job_id: int, result: MatchResult) -> bool:
        return bool(self.add_match_results([(job_id, result)]))

    def add_match_results(self, results: list[tuple[int, MatchResult]]) -> int:
        """Persist a refresh in one transaction without changing match history rules."""
        if not results:
            return 0
        self.initialize()
        prepared = []
        for job_id, result in results:
            payload = result.model_dump(mode="json")
            result_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            result_hash = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
            prepared.append((job_id, result, result_json, result_hash))
        now = _utc_now()
        inserted = 0
        with self._connection() as connection:
            for job_id, result, result_json, result_hash in prepared:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO match_results(
                        job_id, result_hash, score, recommendation, engine,
                        scoring_version, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        result_hash,
                        result.overall_score,
                        result.recommendation.value,
                        result.engine,
                        result.scoring_version,
                        result_json,
                        now,
                    ),
                )
                inserted += cursor.rowcount
                connection.execute(
                    """
                    UPDATE jobs
                    SET match_score = ?, recommendation = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        result.overall_score,
                        result.recommendation.value,
                        now,
                        job_id,
                    ),
                )
        return inserted

    def archived_jobs(self) -> set[int]:
        self.initialize()
        with self._connection() as connection:
            return {row[0] for row in connection.execute("SELECT job_id FROM archived_jobs")}

    def set_job_archived(self, job_id: int, ignored: bool) -> None:
        if not isinstance(ignored, bool):
            raise ValueError("ignored 必须是布尔值。")
        self.get_job(job_id)
        with self._connection() as connection:
            if ignored:
                connection.execute("INSERT OR IGNORE INTO archived_jobs VALUES (?)", (job_id,))
            else:
                connection.execute("DELETE FROM archived_jobs WHERE job_id = ?", (job_id,))

    def list_job_ids(self) -> list[int]:
        self.initialize()
        with self._connection() as connection:
            return [int(row[0]) for row in connection.execute("SELECT id FROM jobs ORDER BY id")]

    def list_job_details(self) -> list[JobDetail]:
        """Read every dashboard job and its sources with two queries."""
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, company, title, location, jd_text, published_at,
                       deadline_at, status, match_score, recommendation,
                       commute_minutes, commute_method, commute_note,
                       commute_origin, commute_destination, commute_mode,
                       commute_distance_meters, commute_route_summary,
                       commute_provider, commute_updated_at, created_at,
                       updated_at, first_seen_at, last_seen_at
                FROM jobs
                ORDER BY match_score IS NULL, match_score DESC, last_seen_at DESC
                """
            ).fetchall()
            source_rows = connection.execute(
                """
                SELECT job_id, platform, source_url, external_id,
                       first_seen_at, last_seen_at
                FROM job_sources
                ORDER BY job_id, last_seen_at DESC, id DESC
                """
            ).fetchall()
        sources_by_job: dict[int, list[JobSourceItem]] = {}
        for source in source_rows:
            sources_by_job.setdefault(int(source["job_id"]), []).append(
                JobSourceItem(
                    platform=source["platform"],
                    source_url=source["source_url"],
                    external_id=source["external_id"],
                    first_seen_at=source["first_seen_at"],
                    last_seen_at=source["last_seen_at"],
                )
            )
        return [
            JobDetail(
                job_id=int(row["id"]),
                company=row["company"], title=row["title"],
                location=row["location"], jd_text=row["jd_text"],
                published_at=row["published_at"], deadline_at=row["deadline_at"],
                status=row["status"], match_score=row["match_score"],
                recommendation=row["recommendation"],
                commute_minutes=row["commute_minutes"],
                commute_method=row["commute_method"],
                commute_note=row["commute_note"],
                commute_origin=row["commute_origin"],
                commute_destination=row["commute_destination"],
                commute_mode=row["commute_mode"],
                commute_distance_meters=row["commute_distance_meters"],
                commute_route_summary=row["commute_route_summary"],
                commute_provider=row["commute_provider"],
                commute_updated_at=row["commute_updated_at"],
                sources=sources_by_job.get(int(row["id"]), []),
                created_at=row["created_at"], updated_at=row["updated_at"],
                first_seen_at=row["first_seen_at"], last_seen_at=row["last_seen_at"],
            )
            for row in rows
        ]

    def match_refresh_inputs(self) -> list[dict]:
        self.initialize()
        with self._connection() as connection:
            return [dict(row) for row in connection.execute("""
                SELECT j.id, j.company, j.title, j.location, j.jd_text,
                       s.platform, s.source_url, m.result_json
                FROM jobs j
                LEFT JOIN job_sources s ON s.id = (
                    SELECT id FROM job_sources WHERE job_id=j.id
                    ORDER BY last_seen_at DESC, id DESC LIMIT 1)
                LEFT JOIN match_results m ON m.id = (
                    SELECT id FROM match_results WHERE job_id=j.id
                    ORDER BY created_at DESC, id DESC LIMIT 1)
                ORDER BY j.id
            """)]

    def list_jobs(self, *, limit: int = 20) -> list[JobListItem]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    jobs.id,
                    jobs.company,
                    jobs.title,
                    jobs.location,
                    jobs.status,
                    jobs.match_score,
                    jobs.recommendation,
                    jobs.commute_minutes,
                    jobs.commute_method,
                    jobs.commute_note,
                    jobs.commute_origin,
                    jobs.commute_destination,
                    jobs.commute_mode,
                    jobs.commute_distance_meters,
                    jobs.commute_route_summary,
                    jobs.commute_provider,
                    jobs.commute_updated_at,
                    jobs.first_seen_at,
                    jobs.last_seen_at,
                    COUNT(job_sources.id) AS source_count
                FROM jobs
                LEFT JOIN job_sources ON job_sources.job_id = jobs.id
                GROUP BY jobs.id
                ORDER BY
                    jobs.match_score IS NULL,
                    jobs.match_score DESC,
                    jobs.last_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            JobListItem(
                job_id=int(row["id"]),
                company=row["company"],
                title=row["title"],
                location=row["location"],
                status=row["status"],
                match_score=row["match_score"],
                recommendation=row["recommendation"],
                commute_minutes=row["commute_minutes"],
                commute_method=row["commute_method"],
                commute_note=row["commute_note"],
                commute_origin=row["commute_origin"],
                commute_destination=row["commute_destination"],
                commute_mode=row["commute_mode"],
                commute_distance_meters=row["commute_distance_meters"],
                commute_route_summary=row["commute_route_summary"],
                commute_provider=row["commute_provider"],
                commute_updated_at=row["commute_updated_at"],
                source_count=int(row["source_count"]),
                first_seen_at=row["first_seen_at"],
                last_seen_at=row["last_seen_at"],
            )
            for row in rows
        ]

    def get_job(self, job_id: int) -> JobDetail:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    id, company, title, location, jd_text, published_at,
                    deadline_at, status, match_score, recommendation,
                    commute_minutes, commute_method, commute_note,
                    commute_origin, commute_destination, commute_mode,
                    commute_distance_meters, commute_route_summary,
                    commute_provider,
                    commute_updated_at,
                    created_at, updated_at, first_seen_at, last_seen_at
                FROM jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobDatabaseError(f"岗位 #{job_id} 不存在。")
            source_rows = connection.execute(
                """
                SELECT
                    platform, source_url, external_id, first_seen_at, last_seen_at
                FROM job_sources
                WHERE job_id = ?
                ORDER BY last_seen_at DESC, id DESC
                """,
                (job_id,),
            ).fetchall()
        return JobDetail(
            job_id=int(row["id"]),
            company=row["company"],
            title=row["title"],
            location=row["location"],
            jd_text=row["jd_text"],
            published_at=row["published_at"],
            deadline_at=row["deadline_at"],
            status=row["status"],
            match_score=row["match_score"],
            recommendation=row["recommendation"],
            commute_minutes=row["commute_minutes"],
            commute_method=row["commute_method"],
            commute_note=row["commute_note"],
            commute_origin=row["commute_origin"],
            commute_destination=row["commute_destination"],
            commute_mode=row["commute_mode"],
            commute_distance_meters=row["commute_distance_meters"],
            commute_route_summary=row["commute_route_summary"],
            commute_provider=row["commute_provider"],
            commute_updated_at=row["commute_updated_at"],
            sources=[
                JobSourceItem(
                    platform=source["platform"],
                    source_url=source["source_url"],
                    external_id=source["external_id"],
                    first_seen_at=source["first_seen_at"],
                    last_seen_at=source["last_seen_at"],
                )
                for source in source_rows
            ],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
        )

    def update_job_commute(
        self,
        job_id: int,
        minutes: int | None,
        *,
        method: str = "user_estimate",
        note: str = "",
        destination: str | None = None,
    ) -> JobDetail:
        """Record or clear a user-confirmed one-way commute estimate."""

        if isinstance(minutes, bool) or (
            minutes is not None and not isinstance(minutes, int)
        ):
            raise JobDatabaseError("单程通勤时间必须填写整数分钟。")
        if minutes is not None and not 0 <= minutes <= 600:
            raise JobDatabaseError("单程通勤时间必须在 0-600 分钟之间。")
        method = method.strip()
        note = note.strip()
        if len(method) > 80:
            raise JobDatabaseError("通勤估算方式最多 80 个字符。")
        if len(note) > 500:
            raise JobDatabaseError("通勤备注最多 500 个字符。")
        normalized_destination = None if destination is None else destination.strip()
        if normalized_destination is not None and len(normalized_destination) > 240:
            raise JobDatabaseError("办公地址最多 240 个字符。")

        self.initialize()
        now = _utc_now()
        with self._connection() as connection:
            if minutes is None:
                cursor = connection.execute(
                    """
                    UPDATE jobs
                    SET commute_minutes = NULL, commute_method = ?, commute_note = ?,
                        commute_origin = '',
                        commute_destination = COALESCE(?, commute_destination),
                        commute_mode = '', commute_distance_meters = NULL,
                        commute_route_summary = '', commute_provider = '',
                        commute_updated_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (method, note, normalized_destination, now, now, job_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE jobs
                    SET commute_minutes = ?, commute_method = ?, commute_note = ?,
                        commute_destination = COALESCE(?, commute_destination),
                        commute_mode = '', commute_distance_meters = NULL,
                        commute_route_summary = '', commute_provider = '',
                        commute_updated_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        minutes,
                        method,
                        note,
                        normalized_destination,
                        now,
                        now,
                        job_id,
                    ),
                )
            if cursor.rowcount == 0:
                raise JobDatabaseError(f"岗位 #{job_id} 不存在。")
        return self.get_job(job_id)

    def update_job_route(
        self,
        job_id: int,
        result: CommuteRouteResult,
    ) -> JobDetail:
        """Persist the best verified route while keeping alternatives in the response."""

        best = result.best
        mode_labels = {
            "transit": "公共交通",
            "driving": "驾车",
            "walking": "步行",
            "bicycling": "骑行",
        }
        method_label = mode_labels.get(result.mode, result.mode)
        note = f"{method_label} · {best.summary}".strip(" ·")
        now = _utc_now()
        self.initialize()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET commute_minutes = ?, commute_method = 'route_estimate',
                    commute_note = ?, commute_origin = ?, commute_destination = ?,
                    commute_mode = ?, commute_distance_meters = ?,
                    commute_route_summary = ?, commute_provider = ?,
                    commute_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    best.minutes,
                    note[:500],
                    result.origin_query,
                    result.destination_query,
                    result.mode,
                    best.distance_meters,
                    best.summary,
                    result.provider,
                    now,
                    now,
                    job_id,
                ),
            )
            if cursor.rowcount == 0:
                raise JobDatabaseError(f"岗位 #{job_id} 不存在。")
        return self.get_job(job_id)

    def get_latest_match_result(self, job_id: int) -> MatchResult | None:
        self.initialize()
        with self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if exists is None:
                raise JobDatabaseError(f"岗位 #{job_id} 不存在。")
            row = connection.execute(
                """
                SELECT result_json
                FROM match_results
                WHERE job_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return MatchResult.model_validate_json(row["result_json"])
        except ValueError as exc:
            raise JobDatabaseError(
                f"岗位 #{job_id} 的最近匹配结果无法读取: {exc}"
            ) from exc

    def get_latest_match_insights(
        self,
        job_ids: list[int],
    ) -> dict[int, dict[str, object]]:
        """Lightweight why/gap/gate excerpts for chat context.

        Skips full MatchResult validation so building a dashboard or copilot
        snapshot does not pay the cost of parsing complete match payloads.
        """
        insights: dict[int, dict[str, object]] = {}
        if not job_ids:
            return insights
        self.initialize()
        with self._connection() as connection:
            for job_id in job_ids:
                row = connection.execute(
                    """
                    SELECT result_json
                    FROM match_results
                    WHERE job_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                if row is None:
                    continue
                try:
                    payload = json.loads(row["result_json"])
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                gates = payload.get("hard_gates") or []
                insights[job_id] = {
                    "why_fit": [str(item) for item in (payload.get("why_fit") or [])][:3],
                    "gaps": [str(item) for item in (payload.get("gaps") or [])][:3],
                    "hard_gates": [
                        {
                            "requirement": str(gate.get("requirement", "")),
                            "status": str(gate.get("status", "")),
                        }
                        for gate in gates[:4]
                        if isinstance(gate, dict)
                    ],
                }
        return insights

    def record_application_status(
        self, job_id: int, status: ApplicationStatus, *, source: str,
        detail: str = '', resume_path: str | None = None,
        application_pack_path: str | None = None, source_url: str | None = None,
        verification_method: str = '', evidence_path: str | None = None,
        allow_regression: bool = False,
    ) -> bool:
        self.initialize()
        with self._connection() as connection:
            return self._record_application_status(
                connection, job_id, status, source=source, detail=detail,
                resume_path=resume_path, application_pack_path=application_pack_path,
                source_url=source_url, verification_method=verification_method,
                evidence_path=evidence_path, allow_regression=allow_regression,
            )

    def _record_application_status(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        status: ApplicationStatus,
        *,
        source: str,
        detail: str = "",
        resume_path: str | None = None,
        application_pack_path: str | None = None,
        source_url: str | None = None,
        verification_method: str = "",
        evidence_path: str | None = None,
        allow_regression: bool = False,
    ) -> bool:
        """Upsert current application state and append an event only on change."""

        if status not in APPLICATION_STATUSES:
            raise JobDatabaseError(f"不支持的投递状态: {status}")
        now = _utc_now()
        with nullcontext(connection) as connection:
            job_exists = connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job_exists is None:
                raise JobDatabaseError(f"岗位 #{job_id} 不存在。")

            existing = connection.execute(
                "SELECT id, status FROM applications WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            previous_status = existing["status"] if existing else None
            if (
                previous_status is not None
                and _is_application_status_regression(previous_status, status)
                and not allow_regression
            ):
                raise JobDatabaseError(
                    f"拒绝将岗位 #{job_id} 的投递状态从 {previous_status} "
                    f"回退为 {status}；确认真实回退时请显式强制。"
                )
            changed = previous_status != status
            applied_at = now if status in SUBMITTED_APPLICATION_STATUSES else None

            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO applications(
                        job_id, status, resume_path, application_pack_path,
                        source_url, applied_at, last_verified_at,
                        verification_method, evidence_path, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        status,
                        resume_path,
                        application_pack_path,
                        source_url,
                        applied_at,
                        now if verification_method else None,
                        verification_method,
                        evidence_path,
                        now,
                        now,
                    ),
                )
                application_id = int(cursor.lastrowid)
            else:
                application_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE applications
                    SET status = ?,
                        resume_path = COALESCE(?, resume_path),
                        application_pack_path = COALESCE(?, application_pack_path),
                        source_url = COALESCE(?, source_url),
                        applied_at = COALESCE(applied_at, ?),
                        last_verified_at = CASE
                            WHEN ? <> '' THEN ? ELSE last_verified_at END,
                        verification_method = CASE
                            WHEN ? <> '' THEN ? ELSE verification_method END,
                        evidence_path = COALESCE(?, evidence_path),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        resume_path,
                        application_pack_path,
                        source_url,
                        applied_at,
                        verification_method,
                        now,
                        verification_method,
                        verification_method,
                        evidence_path,
                        now,
                        application_id,
                    ),
                )

            if changed:
                connection.execute(
                    """
                    INSERT INTO application_events(
                        application_id, previous_status, status, source,
                        detail, evidence_path, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        application_id,
                        previous_status,
                        status,
                        source.strip() or "manual",
                        detail.strip(),
                        evidence_path,
                        now,
                    ),
                )
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, job_id),
            )
        return changed

    def apply_tracking_update(self, payload: dict) -> dict:
        """Commit confirmed status, audit event, reminder and retry key atomically."""
        if payload.get('confirmed') is not True:
            raise ValueError('请先确认识别结果。')
        job_id = payload.get('job_id')
        if type(job_id) is not int or job_id <= 0:
            raise ValueError('请选择有效岗位。')
        request_id = payload.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request_id):
            raise ValueError('缺少有效的重试标识。')
        aliases = {'oa_pending': 'assessment', 'interview_scheduled': 'interview_1', 'offered': 'offer'}
        raw_status = payload.get('status')
        if not isinstance(raw_status, str):
            raise ValueError('请选择流转状态。')
        status = aliases.get(raw_status, raw_status)
        if status not in APPLICATION_STATUSES:
            raise ValueError('不支持的流转状态。')
        event = payload.get('event')
        clean_event = None
        if event is not None:
            if not isinstance(event, dict) or event.get('kind') not in {'oa', 'interview'}:
                raise ValueError('日程类型无效。')
            if (event['kind'] == 'oa' and status not in {'assessment', 'written_test'}) or (event['kind'] == 'interview' and status not in {'interview_1', 'interview_2', 'final_interview'}):
                raise ValueError('日程类型与岗位状态不一致。')
            try:
                at = datetime.fromisoformat(event.get('at', ''))
            except (ValueError, TypeError):
                raise ValueError('请确认包含时区的完整日程时间。') from None
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError('请确认包含时区的完整日程时间。')
            url = event.get('url') or ''
            if not isinstance(url, str) or len(url) > 4000 or any(c.isspace() or ord(c) < 32 for c in url):
                raise ValueError('日程链接无效。')
            if url:
                if not safe_public_url(url):
                    raise ValueError('日程链接必须是有效的公共 HTTP(S) 地址。')
            clean_event = {'kind': event['kind'], 'at': at.astimezone(UTC).isoformat(timespec='seconds'), 'url': url}
        normalized = {'job_id': job_id, 'status': status, 'event': clean_event}
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        self.initialize()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            prior = connection.execute('SELECT payload_hash, response_json FROM tracking_requests WHERE request_id = ?', (request_id,)).fetchone()
            if prior:
                if prior['payload_hash'] != digest:
                    raise ValueError('同一个重试标识不能用于不同内容。')
                return {**json.loads(prior['response_json']), 'duplicate': True}
            changed = self._record_application_status(connection, job_id, status, source='confirmed_notification', verification_method='user_confirmed')
            if status in {'rejected', 'withdrawn', 'offer'}:
                connection.execute('UPDATE tracking_reminders SET completed = 1 WHERE job_id = ?', (job_id,))
            reminder_id = None
            if clean_event:
                # A new invitation need not replace an earlier round or deadline.
                # Keep distinct times until the user explicitly completes them.
                connection.execute('INSERT INTO tracking_reminders(job_id,kind,at,url,created_at) VALUES (?,?,?,?,?) ON CONFLICT(job_id,kind,at) DO UPDATE SET url=excluded.url, completed=0', (job_id, clean_event['kind'], clean_event['at'], clean_event['url'], _utc_now()))
                reminder_id = connection.execute('SELECT id FROM tracking_reminders WHERE job_id=? AND kind=? AND at=?', (job_id, clean_event['kind'], clean_event['at'])).fetchone()['id']
            result = {'ok': True, 'duplicate': False, 'changed': changed, 'reminder_id': reminder_id}
            connection.execute('INSERT INTO tracking_requests VALUES (?,?,?,?)', (request_id, digest, json.dumps(result), _utc_now()))
            return result

    def tracking_board(self) -> dict:
        self.initialize()
        with self._connection() as connection:
            jobs = [dict(row) for row in connection.execute('SELECT j.id AS job_id, j.company, j.title, COALESCE(a.status,j.status) AS status FROM jobs j LEFT JOIN applications a ON a.job_id=j.id WHERE j.id NOT IN (SELECT job_id FROM archived_jobs) ORDER BY j.updated_at DESC, j.id DESC')]
            reminders = [dict(row) for row in connection.execute("SELECT r.id,r.job_id,j.company,j.title,r.kind,r.at,r.url,r.completed,r.created_at FROM tracking_reminders r JOIN jobs j ON j.id=r.job_id LEFT JOIN applications a ON a.job_id=j.id WHERE r.completed=0 AND COALESCE(a.status,j.status) NOT IN ('rejected','withdrawn','offer') AND j.id NOT IN (SELECT job_id FROM archived_jobs) ORDER BY r.at,r.id")]
            for item in reminders:
                item['completed'] = bool(item['completed'])
            return {'jobs': jobs, 'reminders': reminders}

    def complete_tracking_reminder(self, reminder_id: int) -> None:
        self.initialize()
        with self._connection() as connection:
            result = connection.execute('UPDATE tracking_reminders SET completed=1 WHERE id=?', (reminder_id,))
            if not result.rowcount:
                raise ValueError('日程不存在。')

    def update_application_verification(
        self,
        job_id: int,
        *,
        verification_method: str,
        evidence_path: str | None = None,
        source_url: str | None = None,
    ) -> None:
        """Refresh verification evidence without changing the current status."""

        self.initialize()
        now = _utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE applications
                SET last_verified_at = ?, verification_method = ?,
                    evidence_path = COALESCE(?, evidence_path),
                    source_url = COALESCE(?, source_url), updated_at = ?
                WHERE job_id = ?
                """,
                (
                    now,
                    verification_method.strip(),
                    evidence_path,
                    source_url,
                    now,
                    job_id,
                ),
            )
        if cursor.rowcount == 0:
            raise JobDatabaseError(f"岗位 #{job_id} 尚无投递记录。")

    def get_application(self, job_id: int) -> ApplicationRecord | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, job_id, status, resume_path, application_pack_path,
                       source_url, applied_at, last_verified_at,
                       verification_method, evidence_path, created_at, updated_at
                FROM applications
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return ApplicationRecord(
            application_id=int(row["id"]),
            job_id=int(row["job_id"]),
            status=row["status"],
            resume_path=row["resume_path"],
            application_pack_path=row["application_pack_path"],
            source_url=row["source_url"],
            applied_at=row["applied_at"],
            last_verified_at=row["last_verified_at"],
            verification_method=row["verification_method"],
            evidence_path=row["evidence_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def add_interview_debrief(
        self, job_id: int, entry: InterviewDebriefInput,
    ) -> InterviewDebriefSaveResult:
        self.initialize()
        payload = entry.model_dump_json()
        digest = hashlib.sha256(f"{job_id}:{payload}".encode("utf-8")).hexdigest()
        with self._connection() as connection:
            application = connection.execute(
                "SELECT id FROM applications WHERE job_id = ?", (job_id,),
            ).fetchone()
            if application is None:
                raise JobDatabaseError(f"岗位 #{job_id} 尚无投递记录。")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO interview_debriefs "
                "(application_id, debrief_hash, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (application["id"], digest, payload, _utc_now()),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT id FROM interview_debriefs WHERE debrief_hash = ?", (digest,),
            ).fetchone()
        return InterviewDebriefSaveResult(debrief_id=row["id"], created=created)

    def list_interview_debriefs(self, job_id: int) -> list[InterviewDebriefRecord]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT d.*, a.job_id FROM interview_debriefs d "
                "JOIN applications a ON a.id = d.application_id "
                "WHERE a.job_id = ? ORDER BY d.id", (job_id,),
            ).fetchall()
        return [InterviewDebriefRecord(
            **json.loads(row["payload_json"]), debrief_id=row["id"],
            application_id=row["application_id"], job_id=row["job_id"],
            created_at=row["created_at"],
        ) for row in rows]

    def list_application_events(self, job_id: int) -> list[ApplicationEvent]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT application_events.id, application_events.application_id,
                       application_events.previous_status,
                       application_events.status, application_events.source,
                       application_events.detail, application_events.evidence_path,
                       application_events.occurred_at
                FROM application_events
                JOIN applications
                    ON applications.id = application_events.application_id
                WHERE applications.job_id = ?
                ORDER BY application_events.occurred_at, application_events.id
                """,
                (job_id,),
            ).fetchall()
        return [
            ApplicationEvent(
                event_id=int(row["id"]),
                application_id=int(row["application_id"]),
                previous_status=row["previous_status"],
                status=row["status"],
                source=row["source"],
                detail=row["detail"],
                evidence_path=row["evidence_path"],
                occurred_at=row["occurred_at"],
            )
            for row in rows
        ]

    def list_application_events_by_job(self) -> dict[int, list[ApplicationEvent]]:
        """Read the complete feedback timeline for dashboard summaries."""
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT applications.job_id,
                       application_events.id, application_events.application_id,
                       application_events.previous_status,
                       application_events.status, application_events.source,
                       application_events.detail, application_events.evidence_path,
                       application_events.occurred_at
                FROM application_events
                JOIN applications ON applications.id = application_events.application_id
                ORDER BY application_events.occurred_at, application_events.id
                """
            ).fetchall()
        events_by_job: dict[int, list[ApplicationEvent]] = {}
        for row in rows:
            events_by_job.setdefault(int(row["job_id"]), []).append(
                ApplicationEvent(
                    event_id=int(row["id"]),
                    application_id=int(row["application_id"]),
                    previous_status=row["previous_status"],
                    status=row["status"], source=row["source"],
                    detail=row["detail"], evidence_path=row["evidence_path"],
                    occurred_at=row["occurred_at"],
                )
            )
        return events_by_job

    def list_applications(
        self,
        *,
        status: ApplicationStatus | None = None,
        limit: int | None = 100,
    ) -> list[ApplicationListItem]:
        self.initialize()
        if status is not None and status not in APPLICATION_STATUSES:
            raise JobDatabaseError(f"不支持的投递状态: {status}")
        if limit is not None and (limit < 1 or limit > 1000):
            raise JobDatabaseError("投递列表数量必须在 1 到 1000 之间。")
        where_clause = "WHERE applications.status = ?" if status else ""
        parameters: tuple[object, ...] = (status,) if status else ()
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters += (limit,)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT applications.id, applications.job_id,
                       jobs.company, jobs.title, jobs.match_score,
                       applications.status, applications.applied_at,
                       applications.last_verified_at, applications.updated_at
                FROM applications
                JOIN jobs ON jobs.id = applications.job_id
                {where_clause}
                ORDER BY applications.updated_at DESC, applications.id DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        return [
            ApplicationListItem(
                application_id=int(row["id"]),
                job_id=int(row["job_id"]),
                company=row["company"],
                title=row["title"],
                status=row["status"],
                match_score=row["match_score"],
                applied_at=row["applied_at"],
                last_verified_at=row["last_verified_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def application_summary(self) -> ApplicationSummary:
        self.initialize()
        with self._connection() as connection:
            application_rows = connection.execute(
                """
                SELECT applications.id, applications.status, jobs.match_score
                FROM applications
                JOIN jobs ON jobs.id = applications.job_id
                """
            ).fetchall()
            event_rows = connection.execute(
                "SELECT application_id, status FROM application_events"
            ).fetchall()

        reached_by_application: dict[int, set[str]] = {
            int(row["id"]): {row["status"]} for row in application_rows
        }
        current_status_by_application = {
            int(row["id"]): row["status"] for row in application_rows
        }
        match_score_by_application = {
            int(row["id"]): row["match_score"] for row in application_rows
        }
        for row in event_rows:
            reached_by_application.setdefault(int(row["application_id"]), set()).add(
                row["status"]
            )

        current_status_counts: dict[str, int] = {}
        for status_value in current_status_by_application.values():
            current_status_counts[status_value] = (
                current_status_counts.get(status_value, 0) + 1
            )

        max_rank_by_application = {
            application_id: max(
                (APPLICATION_STAGE_RANK.get(status_value, -1) for status_value in statuses),
                default=-1,
            )
            for application_id, statuses in reached_by_application.items()
        }
        return ApplicationSummary(
            total=len(application_rows),
            active=sum(
                status_value not in TERMINAL_APPLICATION_STATUSES
                and APPLICATION_STAGE_RANK.get(status_value, -1) >= 2
                for status_value in current_status_by_application.values()
            ),
            applied_or_later=sum(rank >= 2 for rank in max_rank_by_application.values()),
            hr_read_or_later=sum(rank >= 3 for rank in max_rank_by_application.values()),
            resume_requested_or_later=sum(
                rank >= 4 for rank in max_rank_by_application.values()
            ),
            meaningful_responses=sum(
                bool(
                    statuses
                    & {
                        "resume_requested",
                        "screening",
                        "assessment",
                        "written_test",
                        "interview_1",
                        "interview_2",
                        "final_interview",
                        "offer",
                        "rejected",
                    }
                )
                for statuses in reached_by_application.values()
            ),
            screening_or_later=sum(rank >= 5 for rank in max_rank_by_application.values()),
            assessment_or_written_test=sum(
                bool(statuses & {"assessment", "written_test"})
                for statuses in reached_by_application.values()
            ),
            interview_or_later=sum(rank >= 7 for rank in max_rank_by_application.values()),
            priority_preparation=sum(
                (
                    status_value
                    in {
                        "resume_requested",
                        "screening",
                        "assessment",
                        "written_test",
                        "interview_1",
                        "interview_2",
                        "final_interview",
                    }
                    or (
                        status_value in {"saved", "ready_to_apply", "applied"}
                        and (match_score_by_application.get(application_id) or 0) >= 85
                    )
                )
                for application_id, status_value in current_status_by_application.items()
            ),
            offers=sum(
                "offer" in statuses for statuses in reached_by_application.values()
            ),
            rejected=current_status_counts.get("rejected", 0),
            no_response=current_status_counts.get("no_response", 0),
            withdrawn=current_status_counts.get("withdrawn", 0),
            current_status_counts=current_status_counts,
        )

    def stats(self) -> JobDatabaseStats:
        self.initialize()
        with self._connection() as connection:
            jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            sources = int(
                connection.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0]
            )
            match_results = int(
                connection.execute("SELECT COUNT(*) FROM match_results").fetchone()[0]
            )
            recommended = int(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE match_score >= 75"
                ).fetchone()[0]
            )
            strongly_recommended = int(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE match_score >= 85"
                ).fetchone()[0]
            )
            candidate_counts = {
                row["verification_status"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT verification_status, COUNT(*) AS count
                    FROM search_candidates
                    GROUP BY verification_status
                    """
                )
            }
            candidates = sum(candidate_counts.values())
        return JobDatabaseStats(
            jobs=jobs,
            sources=sources,
            merged_source_records=max(0, sources - jobs),
            match_results=match_results,
            recommended=recommended,
            strongly_recommended=strongly_recommended,
            candidates=candidates,
            pending_candidates=candidate_counts.get("pending", 0),
            live_candidates=candidate_counts.get("live", 0),
            expired_candidates=candidate_counts.get("expired", 0),
            blocked_candidates=candidate_counts.get("blocked", 0),
            irrelevant_candidates=candidate_counts.get("irrelevant", 0),
            manual_review_candidates=candidate_counts.get("needs_manual_review", 0),
        )

    def backup_and_clear_for_new_profile(
        self,
        backup_dir: Path,
        *,
        publish_profile: Callable[[], None] | None = None,
        restore_profile: Callable[[], None] | None = None,
        prepare_publish: Callable[[Path], None] | None = None,
        profile_switch_id: str | None = None,
    ) -> Path:
        """Back up and clear all user tables in one write transaction.

        A staged Profile can be published just before commit. If publication or
        commit fails, roll back the database and restore the former Profile.
        """

        if (publish_profile is None) != (restore_profile is None):
            raise ValueError("发布与恢复 Profile 必须成对提供。")

        self.initialize()
        backup_dir = backup_dir.expanduser().resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_dir / f"job-agent-profile-switch-{stamp}.sqlite3"
        if backup_path.exists():
            raise JobDatabaseError(f"岗位数据库备份已存在: {backup_path}")

        source = self._connect()
        publish_attempted = False
        backup_complete = False
        try:
            # The reserved writer lock keeps another process from changing the
            # database between the backup and the clear. A separate reader
            # backs up the last committed snapshot, including in WAL mode.
            source.execute("BEGIN IMMEDIATE")
            backup_reader = self._connect()
            try:
                destination = sqlite3.connect(backup_path)
                try:
                    backup_reader.backup(destination)
                    destination.commit()
                    backup_complete = True
                finally:
                    destination.close()
            finally:
                backup_reader.close()

            user_tables = {
                "archived_jobs", "jobs", "job_sources", "match_results",
                "applications", "application_events", "search_candidates",
                "search_candidate_sightings", "tracking_reminders",
                "tracking_requests", "interview_debriefs",
                "mock_interview_sessions", "mock_interview_requests",
            }
            actual_tables = {
                row[0] for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            } - {"schema_meta", "schema_migrations"}
            if actual_tables != user_tables:
                raise JobDatabaseError("数据库用户记录表与预期不符，已取消切换，请先检查数据库版本。")
            # Identifiers here come only from this fixed tuple, never from a
            # request or database content.
            for table in (
                "interview_debriefs", "application_events", "applications",
                "tracking_reminders", "archived_jobs", "match_results",
                "job_sources", "search_candidate_sightings", "search_candidates",
                "tracking_requests", "mock_interview_requests",
                "mock_interview_sessions", "jobs",
            ):
                source.execute(f"DELETE FROM {table}")
            if publish_profile is not None:
                if prepare_publish is not None:
                    prepare_publish(backup_path)
                if profile_switch_id is not None:
                    source.execute(
                        "INSERT OR REPLACE INTO schema_meta(key,value) VALUES ('profile_switch_id',?)",
                        (profile_switch_id,),
                    )
                publish_attempted = True
                publish_profile()
            source.commit()
        except Exception as exc:
            recovery_errors: list[str] = []
            try:
                source.rollback()
            except Exception as rollback_exc:
                recovery_errors.append(f"数据库回滚失败: {rollback_exc}")
            if publish_attempted and restore_profile is not None:
                try:
                    restore_profile()
                except Exception as restore_exc:
                    recovery_errors.append(f"Profile 恢复失败: {restore_exc}")
            if recovery_errors:
                raise JobDatabaseError(
                    "切换失败且自动恢复未完成，请停止使用并从备份恢复。"
                    f"数据库备份: {backup_path if backup_complete else '未完成'}；"
                    + "；".join(recovery_errors)
                ) from exc
            if isinstance(exc, JobDatabaseError):
                raise
            raise JobDatabaseError(
                "切换新用户失败，原有资料与岗位库已恢复；"
                f"数据库备份: {backup_path if backup_complete else '未完成'}；原因: {exc}"
            ) from exc
        finally:
            source.close()
        return backup_path

    def verify(self) -> None:
        self.initialize()
        with self._connection() as connection:
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            foreign_key_errors = list(connection.execute("PRAGMA foreign_key_check"))
        if integrity != ["ok"]:
            raise JobDatabaseError("数据库完整性检查失败: " + "；".join(integrity))
        if foreign_key_errors:
            raise JobDatabaseError(
                f"数据库存在 {len(foreign_key_errors)} 个外键引用错误。"
            )
