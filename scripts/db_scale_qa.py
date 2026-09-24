"""Measure dashboard data access against synthetic jobs in an isolated database."""

from __future__ import annotations

import argparse
import json
import cProfile
import pstats
import sqlite3
import sys
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from job_agent.services.dashboard_routes.dashboard_api import build_dashboard_snapshot
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_store import save_profile
from tests.helpers import sample_profile


def measure(label: str, action, measurements: dict[str, dict[str, float]]):
    tracemalloc.start()
    started = time.perf_counter()
    result = action()
    seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    measurements[label] = {"seconds": round(seconds, 3),
                           "peak_mib": round(peak / (1024 * 1024), 2)}
    print(json.dumps({"step": label, **measurements[label]}, ensure_ascii=False), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=int, nargs="?", default=3000)
    parser.add_argument("--profile", action="store_true", help="also measure a synthetic confirmed Profile")
    parser.add_argument("--cpu", action="store_true", help="print a warm snapshot CPU profile (requires --profile)")
    args = parser.parse_args()
    count = args.records
    if not 2000 <= count <= 20000:
        parser.error("record count must be between 2000 and 20000")
    if args.cpu and not args.profile:
        parser.error("--cpu requires --profile")
    fixture = ROOT / "build" / "db-scale-qa" / datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    fixture.mkdir(parents=True)
    repository = JobRepository(fixture / "jobs.sqlite3")
    repository.initialize()
    measurements: dict[str, dict[str, float]] = {}
    stamp = datetime.now(UTC).isoformat()
    jd = "负责数据分析、SQL 报表和跨团队协作。" * 45
    with repository._connection() as connection:
        connection.executemany(
            """INSERT INTO jobs(id,dedupe_key,company,title,location,jd_text,status,
                                match_score,recommendation,created_at,updated_at,first_seen_at,last_seen_at)
               VALUES (?,?,?,?,?,?,'discovered',80,'recommend',?,?,?,?)""",
            ((i + 1, f"synthetic-{i}", f"合成公司{i}", f"数据分析岗位{i}", "上海", jd,
              stamp, stamp, stamp, stamp) for i in range(count)),
        )
        connection.executemany(
            """INSERT INTO job_sources(job_id,source_key,platform,source_url,jd_text,first_seen_at,last_seen_at)
               VALUES (?,?,?,?,?,?,?)""",
            ((i + 1, f"synthetic-source-{i}", "合成测试", f"https://example.com/jobs/{i}",
              jd, stamp, stamp) for i in range(count)),
        )
        connection.executemany(
            """INSERT INTO applications(job_id,status,created_at,updated_at)
               VALUES (?,'saved',?,?)""",
            ((i + 1, stamp, stamp) for i in range(count // 3)),
        )
        connection.executemany(
            """INSERT INTO search_candidates(candidate_key,canonical_url,title,
                                              created_at,updated_at,first_seen_at,last_seen_at)
               VALUES (?,?,?,?,?,?,?)""",
            ((f"synthetic-candidate-{i}", f"https://example.com/candidates/{i}",
              f"合成候选{i}", stamp, stamp, stamp, stamp) for i in range(count // 2)),
        )
    with sqlite3.connect(repository.path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    print(json.dumps({"records": count, "fixture": str(fixture),
                      "journal_mode": journal_mode}, ensure_ascii=False), flush=True)
    jobs = measure("list_jobs_60", lambda: repository.list_jobs(limit=60), measurements)
    details = measure("list_job_details", repository.list_job_details, measurements)
    snapshot = measure("dashboard_snapshot_no_profile", lambda: build_dashboard_snapshot(
        repository, output_dir=fixture / "output"), measurements)
    print(json.dumps({"job_rows": len(jobs), "detail_rows": len(details),
                      "snapshot_rows": len(snapshot.tracked_jobs)}, ensure_ascii=False), flush=True)
    report = {"records": count, "candidates": count // 2,
              "applications": count // 3, "jd_characters": len(jd),
              "journal_mode": journal_mode, "measurements": measurements,
              "job_rows": len(jobs), "detail_rows": len(details),
              "snapshot_rows": len(snapshot.tracked_jobs),
              "database_mib": round(repository.path.stat().st_size / (1024 * 1024), 2)}
    if args.profile:
        profile_path = fixture / "profile.json"
        save_profile(sample_profile(), profile_path)
        first = measure("dashboard_snapshot_with_profile_initial", lambda: build_dashboard_snapshot(
            repository, output_dir=fixture / "output", profile_path=profile_path), measurements)
        warm = measure("dashboard_snapshot_with_profile_warm", lambda: build_dashboard_snapshot(
            repository, output_dir=fixture / "output", profile_path=profile_path), measurements)
        print(json.dumps({"initial_rows": len(first.tracked_jobs),
                          "warm_rows": len(warm.tracked_jobs)}, ensure_ascii=False), flush=True)
        report["initial_rows"] = len(first.tracked_jobs)
        report["warm_rows"] = len(warm.tracked_jobs)
        if args.cpu:
            profiler = cProfile.Profile()
            profiler.runcall(build_dashboard_snapshot, repository,
                             output_dir=fixture / "output", profile_path=profile_path)
            pstats.Stats(profiler).sort_stats("cumtime").print_stats(25)
    report_path = fixture / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
