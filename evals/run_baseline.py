"""Offline, synthetic regression benchmark; no provider calls or private inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

# Fixed test IDs are the benchmark contract; do not silently discover new tests.
CASES = {
    "matching": ("tests.test_local_matcher.LocalMatcherTests", [
        "test_only_confirmed_evidence_can_match",
        "test_failed_hard_gate_caps_score",
        "test_unknown_hard_gate_prevents_strong_recommendation",
        "test_availability_range_is_conditional_not_failed",
        "test_preferred_duration_does_not_become_hard_gate",
        "test_bachelor_requirement_with_postgraduate_preference_passes",
    ]),
    "truthfulness": ("tests.test_resume_compose.ResumeComposeTests", [
        "test_whole_profile_selection_is_not_limited_to_original_bullets",
        "test_rejects_wrong_experience_unconfirmed_facts_numbers_and_skill_invention",
        "test_can_combine_multiple_facts_from_same_experience",
        "test_rejects_duplicate_experiences_and_unsupported_summary_metrics",
    ]),
    "sources": ("tests.test_job_source_review.JobSourceReviewTests", [
        "test_recent_official_ats_with_two_sources_has_strong_route_evidence",
        "test_expired_manual_record_is_held_for_read_only_verification",
        "test_old_board_record_without_original_dates_requires_recheck",
        "test_known_company_career_portal_uses_official_route",
    ]),
    "privacy": ("tests.test_resume_audit.ResumeAuditTests", [
        "test_audit_finds_actionable_issues_without_leaking_contact_values",
    ]),
}


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), *args], text=True, encoding="utf-8", timeout=10
    ).strip()


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", type=Path, help="Previous JSON report")
    args = parser.parse_args()
    manifest = json.dumps(CASES, sort_keys=True).encode()
    fixture_paths = [ROOT / "tests" / name for name in (
        "helpers.py", "test_local_matcher.py", "test_resume_compose.py",
        "test_resume_polish.py", "test_job_source_review.py", "test_resume_audit.py",
    )]
    digest = hashlib.sha256(manifest + b"".join(p.read_bytes() for p in fixture_paths)).hexdigest()
    rows = []
    # Fail closed on accidental outbound requests, including fixture regressions.
    with patch("socket.socket.connect", side_effect=RuntimeError("Offline benchmark: network disabled")), patch(
        "socket.socket.connect_ex", side_effect=RuntimeError("Offline benchmark: network disabled")
    ):
        for category, (cls, names) in CASES.items():
            for name in names:
                case_id = f"{cls}.{name}"
                suite = unittest.defaultTestLoader.loadTestsFromName(case_id)
                result = unittest.TestResult()
                start = time.perf_counter()
                suite.run(result)
                passed = result.wasSuccessful() and result.testsRun == 1 and not result.skipped
                failures = result.errors + result.failures
                rows.append({"id": case_id, "category": category,
                    "status": "pass" if passed else "fail",
                    "latency_ms": round((time.perf_counter() - start) * 1000, 3),
                    "failure_reason": "\n".join(trace for _, trace in failures) or (
                        "Skipped or missing case" if not passed else None)})
    report = {
        "schema_version": 1, "suite": "synthetic-offline-v1", "fixture_sha256": digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": git("rev-parse", "HEAD"), "working_tree_dirty": bool(git("status", "--porcelain")),
        "source_sha256": hashlib.sha256(b"".join(
            p.relative_to(ROOT).as_posix().encode() + p.read_bytes()
            for p in sorted((ROOT / "src").rglob("*.py")))).hexdigest(),
        "python": sys.version.split()[0], "mode": "offline_mocked_provider",
        "provider_calls": 0, "cost_usd": 0, "tokens": None,
        "passed": sum(r["status"] == "pass" for r in rows), "total": len(rows), "cases": rows,
    }
    regression = False
    if args.compare:
        previous = json.loads(args.compare.read_text(encoding="utf-8"))
        compatible = (previous.get("suite") == report["suite"] and
                      previous.get("fixture_sha256") == digest and
                      previous.get("mode") == report["mode"] and
                      {r["id"] for r in previous["cases"]} == {r["id"] for r in rows})
        old = {r["id"]: r for r in previous["cases"]}
        report["comparison"] = {"compatible": compatible}
        if compatible:
            regressions = [r["id"] for r in rows if old[r["id"]]["status"] == "pass" and r["status"] != "pass"]
            report["comparison"].update(regressions=regressions,
                improvements=[r["id"] for r in rows if old[r["id"]]["status"] != "pass" and r["status"] == "pass"],
                latency_delta_ms={r["id"]: round(r["latency_ms"] - old[r["id"]]["latency_ms"], 3) for r in rows})
            regression = bool(regressions)
        else:
            report["comparison"]["reason"] = "Fixtures, case IDs, or mode changed; establish a new baseline."
            regression = True
    output = ROOT / "build" / "evals"
    output.mkdir(parents=True, exist_ok=True)
    path = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{report['passed']}/{report['total']} passed; report: {path}")
    for row in rows:
        if row["status"] != "pass":
            print(f"FAIL {row['id']}")
    return int(report["passed"] != report["total"] or regression)


if __name__ == "__main__":
    raise SystemExit(run())
