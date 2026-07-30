#!/usr/bin/env python3
"""Read-only monitor for the deduplicated editorial-truth packet rollout.

The monitor inspects the first 25 *reviewer provider returns* after the r43
schema cutover. The immutable LLM-call ledger is the source of truth, including
returns later marked invalid; applied events are deliberately not the cohort
authority because an invalid response never creates one. Manual imports and
zero-call derived reports therefore cannot enter the sample. The monitor never
constructs ``WorkbenchEngine`` and opens SQLite with ``mode=ro`` plus
``query_only`` so it cannot migrate production state.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import statistics
import sys


# Railway deployment b22a558e started its production container at the timestamp
# below. Operators may override it with ``--since`` or
# REVIEW_PACKET_SCHEMA_CUTOVER when monitoring a later rollout.
DEFAULT_SCHEMA_CUTOVER = "2026-07-30T13:52:55.356871+00:00"
DEFAULT_REPORT_LIMIT = 25
DEFAULT_MINIMUM_BASELINE = 3
EXCERPT_ID_PATTERN = re.compile(r"^e-", re.IGNORECASE)
EMPTY_CANDIDATE_SET_HASH = hashlib.sha256(b"[]").hexdigest()
VALIDATED_CANDIDATE_COUNT_SOURCE = "llm_call_intent"
REVIEW_PURPOSES = frozenset({
    "compliance",
    "final_signoff",
    "post_seo_signoff",
    "independent_rescue_signoff",
    "executive_rescue_signoff",
    "war_room_signoff",
})


def _default_db_path() -> Path:
    data_root = Path(
        os.environ.get(
            "SOURCE_INTELLIGENCE_DATA_DIR",
            "~/.source-intelligence/data",
        )
    ).expanduser()
    workbench_root = Path(
        os.environ.get(
            "NEWSWIRE_WORKBENCH_HOME",
            str(data_root / "newswire-workbench"),
        )
    ).expanduser()
    return workbench_root / "workbench.db"


def _timestamp(value: object) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _payload(value: object) -> dict:
    if isinstance(value, dict):
        return value
    parsed = json.loads(str(value or "{}"))
    if not isinstance(parsed, dict):
        raise ValueError("review payload is not a JSON object")
    return parsed


def load_report_rows(db_path: Path) -> list[dict]:
    """Load provider returns and their pre-network candidate attestations.

    The provider-call row remains the rollout-cohort authority. A matching
    immutable call-intent row records the expected candidate count and set hash
    before network I/O. Reviewer decisions and later applied reports are never
    allowed to attest their own expected scope.
    """
    resolved = Path(db_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Workbench database not found: {resolved}")
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        llm_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(llm_calls)")
        }
        expected_count_sql = (
            "reviewer_call.expected_candidate_count"
            if "expected_candidate_count" in llm_columns
            else "-1"
        )
        expected_hash_sql = (
            "reviewer_call.expected_candidate_set_hash"
            if "expected_candidate_set_hash" in llm_columns
            else "''"
        )
        rows = [
            dict(row)
            for row in connection.execute(
            f"""
            SELECT
                reviewer_call.id AS reviewer_call_id,
                reviewer_call.id AS event_id,
                reviewer_call.project_id,
                reviewer_call.raw_output AS payload,
                reviewer_call.input_article_hash AS article_hash,
                reviewer_call.created_at,
                reviewer_call.stage AS reviewer_call_purpose,
                reviewer_call.lifecycle AS reviewer_call_lifecycle,
                {expected_count_sql}
                    AS reviewer_expected_candidate_count,
                {expected_hash_sql}
                    AS reviewer_expected_candidate_set_hash,
                project.title,
                project.platform,
                project.vertical
            FROM llm_calls AS reviewer_call
            JOIN projects AS project
              ON project.id=reviewer_call.project_id
            WHERE reviewer_call.status='success'
              AND reviewer_call.raw_output<>''
              AND reviewer_call.stage IN (
                  'compliance',
                  'final_signoff',
                  'post_seo_signoff',
                  'independent_rescue_signoff',
                  'executive_rescue_signoff',
                  'war_room_signoff'
              )
            ORDER BY reviewer_call.id ASC
            """
            ).fetchall()
        ]
        for row in rows:
            try:
                expected_count = int(
                    row.get("reviewer_expected_candidate_count")
                )
            except (TypeError, ValueError):
                expected_count = -1
            expected_hash = str(
                row.get("reviewer_expected_candidate_set_hash") or ""
            ).strip()
            if expected_count >= 0 and expected_hash:
                row["reviewer_expected_candidate_count_source"] = (
                    VALIDATED_CANDIDATE_COUNT_SOURCE
                )
        return rows
    finally:
        connection.close()


def _mandatory_edit_count(report: dict) -> int:
    edits = report.get("mandatory_edits")
    return len(edits) if isinstance(edits, list) else 0


def _validated_expected_candidate_attestation(row: dict):
    """Return scope attested by the immutable pre-network call-intent row."""
    if (
        row.get("reviewer_expected_candidate_count_source")
        != VALIDATED_CANDIDATE_COUNT_SOURCE
    ):
        return None
    count = row.get("reviewer_expected_candidate_count")
    candidate_hash = str(
        row.get("reviewer_expected_candidate_set_hash") or ""
    ).strip()
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or not candidate_hash
    ):
        return None
    return count, candidate_hash


def _review_report_schema_error(report: dict, row: dict) -> str:
    """Return an integrity error for an unusable reviewer response."""
    verdict = report.get("verdict")
    edits = report.get("mandatory_edits")
    if verdict not in {"approved", "not_approved"}:
        return "reviewer report has an invalid verdict"
    if not isinstance(edits, list):
        return "reviewer report mandatory_edits is not a list"
    if not isinstance(report.get("mandatory_count"), int):
        return "reviewer report mandatory_count is not an integer"
    if report.get("mandatory_count") != len(edits):
        return "reviewer report mandatory_count does not match edits"
    if not isinstance(
        report.get("conditional_approval_after_exact_edits"), bool
    ):
        return "reviewer report conditional-approval flag is not boolean"
    source_accuracy = report.get("source_accuracy")
    if not isinstance(source_accuracy, dict):
        return "reviewer report source_accuracy is not an object"
    for list_field in (
        "recommended_edits", "approved_elements", "notes"
    ):
        if not isinstance(report.get(list_field), list):
            return f"reviewer report {list_field} is not a list"
    if verdict == "approved" and edits:
        return "approved reviewer report contains mandatory edits"
    if verdict == "not_approved" and not edits:
        return "not-approved reviewer report has no mandatory edits"
    review = report.get("editorial_truth_review")
    if not isinstance(review, dict):
        return "reviewer report has no editorial_truth_review object"
    if not str(review.get("candidate_set_hash") or "").strip():
        return "reviewer report has no candidate_set_hash"
    decisions = review.get("decisions")
    if not isinstance(decisions, list):
        return "reviewer report editorial-truth decisions is not a list"
    expected = _validated_expected_candidate_attestation(row)
    if expected is None:
        return (
            "reviewer call has no independent expected-candidate "
            "intent attestation"
        )
    expected_count, expected_hash = expected
    returned_hash = str(
        review.get("candidate_set_hash") or ""
    ).strip()
    if returned_hash != expected_hash:
        return (
            "reviewer report candidate_set_hash does not match the "
            "pre-network call intent"
        )
    if len(decisions) != expected_count:
        return (
            "reviewer report candidate coverage does not match the "
            "pre-network expected candidate count"
        )
    if expected_count == 0 and expected_hash.casefold() != (
        EMPTY_CANDIDATE_SET_HASH
    ):
        return "zero-candidate call intent has the wrong candidate_set_hash"
    seen_edit_ids = set()
    for edit in edits:
        if not isinstance(edit, dict):
            return "reviewer report contains a non-object mandatory edit"
        required = ("id", "category", "issue", "exact_text", "replacement")
        if any(not str(edit.get(field) or "").strip() for field in required):
            return "reviewer report contains a non-actionable mandatory edit"
        edit_id = str(edit["id"]).strip()
        if edit_id in seen_edit_ids:
            return "reviewer report contains duplicate mandatory edit ids"
        seen_edit_ids.add(edit_id)
    for decision in decisions:
        if not isinstance(decision, dict):
            return "reviewer report contains a non-object truth decision"
        if not str(decision.get("sentence_id") or "").strip():
            return "reviewer report truth decision has no sentence_id"
        if not isinstance(decision.get("source_ids"), list):
            return "reviewer report truth decision source_ids is not a list"
        if decision.get("verdict") not in {
            "source_supported", "non_material", "unsupported"
        }:
            return "reviewer report truth decision has an invalid verdict"
        if not str(decision.get("rationale") or "").strip():
            return "reviewer report truth decision has no rationale"
    return ""


def _group_key(row: dict) -> tuple[str, str, str]:
    # Platform rules, vertical, and review purpose all materially affect edit
    # counts. A compliance pass is not a valid baseline for a final signoff.
    return (
        str(row.get("platform") or "unknown"),
        str(row.get("vertical") or "unknown"),
        str(row.get("reviewer_call_purpose") or "unknown"),
    )


def _count_summary(counts: list[int]) -> dict:
    if not counts:
        return {
            "report_count": 0,
            "mandatory_edit_total": 0,
            "mandatory_edit_mean": None,
            "mandatory_edit_median": None,
            "mandatory_edit_min": None,
            "mandatory_edit_max": None,
        }
    return {
        "report_count": len(counts),
        "mandatory_edit_total": sum(counts),
        "mandatory_edit_mean": round(statistics.mean(counts), 3),
        "mandatory_edit_median": round(statistics.median(counts), 3),
        "mandatory_edit_min": min(counts),
        "mandatory_edit_max": max(counts),
    }


def _excerpt_id_leaks(row: dict, report: dict) -> list[dict]:
    review = report.get("editorial_truth_review")
    decisions = review.get("decisions") if isinstance(review, dict) else []
    if not isinstance(decisions, list):
        return []
    findings = []
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        source_ids = decision.get("source_ids")
        if not isinstance(source_ids, list):
            continue
        normalized = {}
        for source_id in source_ids:
            value = str(source_id or "").strip()
            if not EXCERPT_ID_PATTERN.match(value):
                continue
            canonical = "E-" + value[2:].strip()
            normalized.setdefault(canonical.casefold(), canonical)
        leaked = sorted(normalized.values(), key=str.casefold)
        if leaked:
            findings.append({
                "event_id": row.get("event_id"),
                "project_id": row.get("project_id"),
                "title": row.get("title") or "",
                "platform": row.get("platform") or "",
                "vertical": row.get("vertical") or "",
                "sentence_id": decision.get("sentence_id") or "",
                "leaked_source_ids": leaked,
            })
    return findings


def monitor_report_rows(
    rows: list[dict],
    *,
    since: str = DEFAULT_SCHEMA_CUTOVER,
    limit: int = DEFAULT_REPORT_LIMIT,
    baseline_per_group: int = DEFAULT_REPORT_LIMIT,
    minimum_baseline: int = DEFAULT_MINIMUM_BASELINE,
) -> dict:
    """Analyze the rollout cohort without modifying caller-owned rows."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if baseline_per_group < 0:
        raise ValueError("baseline_per_group cannot be negative")
    if minimum_baseline <= 0:
        raise ValueError("minimum_baseline must be positive")
    cutoff = _timestamp(since)
    dated_rows = []
    malformed = []
    ignored_non_reviewer = 0
    for original in rows:
        row = dict(original)
        purpose = str(row.get("reviewer_call_purpose") or "")
        if (
            not row.get("reviewer_call_id")
            or purpose not in REVIEW_PURPOSES
        ):
            ignored_non_reviewer += 1
            continue
        try:
            row["_created_at"] = _timestamp(row.get("created_at"))
        except (TypeError, ValueError) as exc:
            malformed.append({
                "event_id": row.get("event_id"),
                "project_id": row.get("project_id"),
                "scope": "unknown_timestamp",
                "error": str(exc),
            })
            continue
        dated_rows.append(row)

    def event_order(row):
        try:
            return int(row.get("event_id") or 0)
        except (TypeError, ValueError):
            return 0

    dated_rows.sort(
        key=lambda row: (
            row["_created_at"],
            event_order(row),
        )
    )
    # A derived event can retain the original approval purpose. Count one
    # schema exposure per immutable provider call, never one per event.
    unique_rows = []
    seen_call_ids = set()
    duplicate_report_events = 0
    for row in dated_rows:
        call_id = str(row.get("reviewer_call_id") or "")
        if call_id in seen_call_ids:
            duplicate_report_events += 1
            continue
        seen_call_ids.add(call_id)
        unique_rows.append(row)
    dated_rows = unique_rows
    cohort_rows = [
        row for row in dated_rows if row["_created_at"] >= cutoff
    ][:limit]
    post_cutover = []
    for row in cohort_rows:
        try:
            row["_report"] = _payload(row.get("payload"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            malformed.append({
                "event_id": row.get("event_id"),
                "project_id": row.get("project_id"),
                "scope": "post_cutover_cohort",
                "error": str(exc),
            })
            continue
        integrity_errors = []
        if str(row.get("reviewer_call_lifecycle") or "") == "invalid":
            integrity_errors.append(
                "provider return lifecycle is invalid"
            )
        schema_error = _review_report_schema_error(row["_report"], row)
        if schema_error:
            integrity_errors.append(schema_error)
        row["_integrity_error"] = "; ".join(integrity_errors)
        if integrity_errors:
            malformed.append({
                "event_id": row.get("event_id"),
                "project_id": row.get("project_id"),
                "scope": "post_cutover_cohort",
                "error": row["_integrity_error"],
            })
        post_cutover.append(row)

    baseline_by_group = defaultdict(list)
    for row in reversed([
        item for item in dated_rows if item["_created_at"] < cutoff
    ]):
        try:
            row["_report"] = _payload(row.get("payload"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            str(row.get("reviewer_call_lifecycle") or "") == "invalid"
            or _review_report_schema_error(row["_report"], row)
        ):
            continue
        key = _group_key(row)
        if len(baseline_by_group[key]) < baseline_per_group:
            baseline_by_group[key].append(row)

    post_by_group = defaultdict(list)
    leaks = []
    report_rows = []
    for row in post_cutover:
        report = row["_report"]
        leaks.extend(_excerpt_id_leaks(row, report))
        if row.get("_integrity_error"):
            continue
        count = _mandatory_edit_count(report)
        post_by_group[_group_key(row)].append((row, count))
        report_rows.append({
            "event_id": row.get("event_id"),
            "project_id": row.get("project_id"),
            "created_at": row.get("created_at"),
            "title": row.get("title") or "",
            "platform": row.get("platform") or "",
            "vertical": row.get("vertical") or "",
            "reviewer_call_id": row.get("reviewer_call_id"),
            "reviewer_call_purpose": row.get(
                "reviewer_call_purpose"
            ) or "",
            "verdict": report.get("verdict") or "",
            "mandatory_edit_count": count,
        })

    comparisons = []
    band_violations = []
    for platform, vertical, purpose in sorted(post_by_group):
        key = (platform, vertical, purpose)
        current_rows = post_by_group[key]
        current_counts = [item[1] for item in current_rows]
        current = _count_summary(current_counts)
        historical_counts = [
            _mandatory_edit_count(row["_report"])
            for row in baseline_by_group.get(key, [])
        ]
        historical = _count_summary(historical_counts)
        band_evaluable = (
            len(historical_counts) >= minimum_baseline
        )
        band_min = min(historical_counts) if band_evaluable else None
        band_max = max(historical_counts) if band_evaluable else None
        group_violations = []
        if band_evaluable:
            for row, count in current_rows:
                if band_min <= count <= band_max:
                    continue
                finding = {
                    "event_id": row.get("event_id"),
                    "project_id": row.get("project_id"),
                    "reviewer_call_id": row.get("reviewer_call_id"),
                    "platform": platform,
                    "vertical": vertical,
                    "reviewer_call_purpose": purpose,
                    "mandatory_edit_count": count,
                    "historical_band_min": band_min,
                    "historical_band_max": band_max,
                }
                group_violations.append(finding)
                band_violations.append(finding)
        current_mean = current["mandatory_edit_mean"]
        historical_mean = historical["mandatory_edit_mean"]
        comparisons.append({
            "platform": platform,
            "vertical": vertical,
            "reviewer_call_purpose": purpose,
            "post_cutover": current,
            "historical_comparable": historical,
            "historical_band": {
                "method": "observed_min_max",
                "minimum_baseline_required": minimum_baseline,
                "evaluable": band_evaluable,
                "minimum": band_min,
                "maximum": band_max,
            },
            "within_historical_band": (
                not group_violations if band_evaluable else None
            ),
            "band_violations": group_violations,
            "mandatory_edit_mean_delta": (
                round(current_mean - historical_mean, 3)
                if current_mean is not None and historical_mean is not None
                else None
            ),
        })

    leaked_ids = sorted({
        source_id
        for finding in leaks
        for source_id in finding["leaked_source_ids"]
    })
    cohort_complete = len(cohort_rows) == limit
    historical_band_evaluable = bool(comparisons) and all(
        item["historical_band"]["evaluable"]
        for item in comparisons
    )
    historical_band_within = (
        historical_band_evaluable and not band_violations
    )
    integrity_clean = not leaks and not malformed
    passed_so_far = integrity_clean and not band_violations
    passed = (
        cohort_complete
        and passed_so_far
        and historical_band_evaluable
        and historical_band_within
    )
    if not integrity_clean or band_violations:
        status = "failed"
    elif not cohort_complete:
        status = "collecting"
    elif not historical_band_evaluable:
        status = "insufficient_baseline"
    else:
        status = "passed"
    return {
        "monitor": "review_packet_schema_rollout",
        "monitor_version": 3,
        "read_only": True,
        "schema_cutover": cutoff.isoformat(),
        "target_report_count": limit,
        "reports_observed": len(cohort_rows),
        "reports_remaining": max(0, limit - len(cohort_rows)),
        "ignored_non_reviewer_report_events": ignored_non_reviewer,
        "duplicate_report_events_ignored": duplicate_report_events,
        "cohort_complete": cohort_complete,
        "passed_so_far": passed_so_far,
        "passed": passed,
        "status": status,
        "excerpt_id_leaks": {
            "report_decisions_with_leaks": len(leaks),
            "leaked_ids": leaked_ids,
            "findings": leaks,
        },
        "malformed_reports": malformed,
        "historical_band": {
            "method": "observed_min_max",
            "minimum_baseline_required": minimum_baseline,
            "evaluable_for_all_groups": historical_band_evaluable,
            "all_reports_within_band": historical_band_within,
            "violation_count": len(band_violations),
            "violations": band_violations,
        },
        "mandatory_edit_comparisons": comparisons,
        "reports": report_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only monitor for the first reviewer reports produced by "
            "the deduplicated editorial-truth packet schema."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_default_db_path(),
        help="Path to workbench.db (opened in SQLite mode=ro).",
    )
    parser.add_argument(
        "--since",
        default=os.environ.get(
            "REVIEW_PACKET_SCHEMA_CUTOVER",
            DEFAULT_SCHEMA_CUTOVER,
        ),
        help="ISO-8601 schema deployment boundary.",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_REPORT_LIMIT)
    parser.add_argument(
        "--baseline-per-group",
        type=int,
        default=DEFAULT_REPORT_LIMIT,
    )
    parser.add_argument(
        "--minimum-baseline",
        type=int,
        default=DEFAULT_MINIMUM_BASELINE,
        help=(
            "Minimum comparable pre-cutover reviewer calls required to "
            "evaluate a platform/vertical/review-purpose historical band."
        ),
    )
    parser.add_argument(
        "--stdin-json",
        action="store_true",
        help="Read pre-extracted event rows from stdin instead of SQLite.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Omit individual cohort report rows from JSON output.",
    )
    parser.add_argument(
        "--fail-on-leaks",
        action="store_true",
        help=(
            "Exit nonzero for excerpt-ID leaks, malformed reports, or a "
            "mandatory-edit historical-band violation."
        ),
    )
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help=(
            "Exit nonzero until the 25-report cohort is complete, all "
            "comparison baselines are sufficient, and every assertion passes."
        ),
    )
    args = parser.parse_args()
    if args.stdin_json:
        rows = json.load(sys.stdin)
        if not isinstance(rows, list):
            raise RuntimeError("--stdin-json requires a JSON list")
    else:
        rows = load_report_rows(args.db)
    result = monitor_report_rows(
        rows,
        since=args.since,
        limit=args.limit,
        baseline_per_group=args.baseline_per_group,
        minimum_baseline=args.minimum_baseline,
    )
    if args.summary_only:
        result.pop("reports", None)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    if args.fail_on_leaks and result["status"] == "failed":
        raise SystemExit(1)
    if args.require_pass and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
