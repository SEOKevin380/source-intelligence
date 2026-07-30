import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from scripts.monitor_review_packet_schema import (
    DEFAULT_SCHEMA_CUTOVER,
    load_report_rows,
    monitor_report_rows,
)


def _row(
    event_id,
    created_at,
    *,
    platform="AccessNewsWire",
    vertical="device",
    mandatory_count=0,
    source_ids=None,
    reviewer_call_id=None,
    reviewer_call_purpose="compliance",
    provider_backed=True,
):
    return {
        "event_id": event_id,
        "reviewer_call_id": (
            reviewer_call_id
            if reviewer_call_id is not None
            else (event_id if provider_backed else None)
        ),
        "reviewer_call_purpose": (
            reviewer_call_purpose if provider_backed else ""
        ),
        "reviewer_call_lifecycle": (
            "applied" if provider_backed else ""
        ),
        "reviewer_expected_candidate_count": (
            1 if provider_backed else -1
        ),
        "reviewer_expected_candidate_set_hash": (
            f"candidate-set-{event_id}" if provider_backed else ""
        ),
        "reviewer_expected_candidate_count_source": (
            "llm_call_intent" if provider_backed else ""
        ),
        "project_id": f"project-{event_id}",
        "created_at": created_at,
        "title": f"Project {event_id}",
        "platform": platform,
        "vertical": vertical,
        "payload": {
            "verdict": (
                "not_approved" if mandatory_count else "approved"
            ),
            "mandatory_edits": [
                {
                    "id": f"M-{event_id}-{index}",
                    "category": "source_accuracy",
                    "issue": "Repair the unsupported sentence.",
                    "exact_text": f"Unsupported sentence {index}.",
                    "replacement": f"Supported sentence {index}.",
                }
                for index in range(mandatory_count)
            ],
            "mandatory_count": mandatory_count,
            "conditional_approval_after_exact_edits": bool(mandatory_count),
            "source_accuracy": {"verified": 1, "checked": 1},
            "recommended_edits": [],
            "approved_elements": [],
            "notes": [],
            "editorial_truth_review": {
                "candidate_set_hash": f"candidate-set-{event_id}",
                "decisions": [{
                    "sentence_id": f"S-{event_id}",
                    "source_ids": list(source_ids or []),
                    "verdict": "source_supported",
                    "rationale": "The sentence is grounded in the source.",
                }],
            },
        },
    }


def test_monitor_checks_only_first_25_and_compares_like_verticals():
    rows = [
        _row(1, "2026-07-30T12:00:00+00:00", mandatory_count=1),
        _row(2, "2026-07-30T12:10:00+00:00", mandatory_count=3),
        _row(
            3,
            "2026-07-30T12:20:00+00:00",
            platform="Barchart Advertorial",
            vertical="supplement",
            mandatory_count=4,
        ),
    ]
    for index in range(30):
        platform = (
            "AccessNewsWire"
            if index < 10
            else "Barchart Advertorial"
        )
        vertical = "device" if index < 10 else "supplement"
        source_ids = ["artifact-1"]
        if index == 2:
            source_ids += ["E-7", "E-7"]
        if index == 27:
            source_ids += ["E-not-in-first-25"]
        rows.append(_row(
            100 + index,
            f"2026-07-30T14:{index:02d}:00+00:00",
            platform=platform,
            vertical=vertical,
            mandatory_count=2 if index < 10 else 1,
            source_ids=source_ids,
        ))
    original = copy.deepcopy(rows)

    result = monitor_report_rows(rows, minimum_baseline=1)

    assert rows == original
    assert result["read_only"] is True
    assert result["reports_observed"] == 25
    assert result["reports_remaining"] == 0
    assert result["cohort_complete"] is True
    assert result["passed_so_far"] is False
    assert result["passed"] is False
    assert result["status"] == "failed"
    assert result["excerpt_id_leaks"]["leaked_ids"] == ["E-7"]
    assert len(result["excerpt_id_leaks"]["findings"]) == 1
    comparisons = {
        (item["platform"], item["vertical"]): item
        for item in result["mandatory_edit_comparisons"]
    }
    device = comparisons[("AccessNewsWire", "device")]
    assert device["post_cutover"]["report_count"] == 10
    assert device["post_cutover"]["mandatory_edit_mean"] == 2
    assert device["historical_comparable"]["mandatory_edit_mean"] == 2
    assert device["mandatory_edit_mean_delta"] == 0
    supplement = comparisons[
        ("Barchart Advertorial", "supplement")
    ]
    assert supplement["post_cutover"]["report_count"] == 15
    assert supplement["post_cutover"]["mandatory_edit_mean"] == 1
    assert supplement["historical_comparable"][
        "mandatory_edit_mean"
    ] == 4
    assert supplement["mandatory_edit_mean_delta"] == -3
    assert supplement["within_historical_band"] is False
    assert result["historical_band"]["violation_count"] == 15


def test_monitor_reports_collecting_state_and_malformed_payloads():
    rows = [
        _row(10, "2026-07-30T14:00:00+00:00"),
        {
            "event_id": 11,
            "reviewer_call_id": 11,
            "reviewer_call_purpose": "compliance",
            "project_id": "bad-report",
            "created_at": "2026-07-30T14:01:00+00:00",
            "platform": "AccessNewsWire",
            "vertical": "device",
            "payload": "{not-json",
        },
    ]

    result = monitor_report_rows(rows)

    assert result["reports_observed"] == 2
    assert result["reports_remaining"] == 23
    assert result["cohort_complete"] is False
    assert result["passed_so_far"] is False
    assert result["passed"] is False
    assert result["status"] == "failed"
    assert result["malformed_reports"][0]["project_id"] == "bad-report"


def test_invalid_clean_provider_return_fails_integrity_not_band():
    row = _row(12, "2026-07-30T14:00:00+00:00")
    row["reviewer_call_lifecycle"] = "invalid"

    result = monitor_report_rows([row])

    assert result["reports_observed"] == 1
    assert result["status"] == "failed"
    assert "lifecycle is invalid" in (
        result["malformed_reports"][0]["error"]
    )
    assert result["mandatory_edit_comparisons"] == []


def test_contradictory_reviewer_schema_fails_integrity():
    row = _row(
        13,
        "2026-07-30T14:00:00+00:00",
        mandatory_count=0,
    )
    row["payload"]["verdict"] = "not_approved"

    result = monitor_report_rows([row])

    assert result["status"] == "failed"
    assert "no mandatory edits" in (
        result["malformed_reports"][0]["error"]
    )


def test_clean_partial_cohort_is_collecting_not_prematurely_passed():
    result = monitor_report_rows([
        _row(10, "2026-07-30T14:00:00+00:00"),
    ])

    assert result["cohort_complete"] is False
    assert result["passed_so_far"] is True
    assert result["passed"] is False
    assert result["status"] == "collecting"


def test_manual_and_derived_reports_do_not_enter_reviewer_cohort():
    rows = [
        _row(1, "2026-07-30T14:00:00+00:00"),
        _row(
            2,
            "2026-07-30T14:01:00+00:00",
            provider_backed=False,
        ),
        _row(
            3,
            "2026-07-30T14:02:00+00:00",
            reviewer_call_id=1,
        ),
    ]

    result = monitor_report_rows(rows)

    assert result["reports_observed"] == 1
    assert result["ignored_non_reviewer_report_events"] == 1
    assert result["duplicate_report_events_ignored"] == 1
    assert result["reports"][0]["reviewer_call_id"] == 1


def test_independent_rescue_signoff_is_in_reviewer_cohort():
    result = monitor_report_rows([
        _row(
            9,
            "2026-07-30T14:00:00+00:00",
            reviewer_call_purpose="independent_rescue_signoff",
            source_ids=["E-9"],
        ),
    ])

    assert result["reports_observed"] == 1
    assert result["excerpt_id_leaks"]["leaked_ids"] == ["E-9"]


def test_excerpt_id_leaks_are_trimmed_case_insensitive_and_deduplicated():
    result = monitor_report_rows([
        _row(
            10,
            "2026-07-30T14:00:00+00:00",
            source_ids=[
                "artifact-1",
                "  e-7  ",
                "E-7",
                "\te-MixedCase\n",
            ],
        ),
    ])

    assert result["excerpt_id_leaks"]["leaked_ids"] == [
        "E-7",
        "E-MixedCase",
    ]
    assert result["excerpt_id_leaks"][
        "report_decisions_with_leaks"
    ] == 1


def test_empty_decisions_require_independent_zero_candidate_intent():
    row = _row(11, "2026-07-30T14:00:00+00:00")
    review = row["payload"]["editorial_truth_review"]
    review["candidate_set_hash"] = sha256(b"[]").hexdigest()
    review["decisions"] = []
    row.pop("reviewer_expected_candidate_count")
    row.pop("reviewer_expected_candidate_set_hash")
    row.pop("reviewer_expected_candidate_count_source")

    unproven = monitor_report_rows([row])

    assert unproven["status"] == "failed"
    assert "independent expected-candidate" in (
        unproven["malformed_reports"][0]["error"]
    )

    row["reviewer_expected_candidate_count"] = 0
    row["reviewer_expected_candidate_set_hash"] = sha256(b"[]").hexdigest()
    row["reviewer_expected_candidate_count_source"] = (
        "llm_call_intent"
    )
    proven = monitor_report_rows([row])

    assert proven["passed_so_far"] is True
    assert proven["status"] == "collecting"


def test_zero_candidate_event_cannot_attest_the_wrong_candidate_hash():
    row = _row(12, "2026-07-30T14:00:00+00:00")
    review = row["payload"]["editorial_truth_review"]
    review["candidate_set_hash"] = "not-the-empty-set-hash"
    review["decisions"] = []
    row["reviewer_expected_candidate_count"] = 0
    row["reviewer_expected_candidate_set_hash"] = sha256(b"[]").hexdigest()
    row["reviewer_expected_candidate_count_source"] = (
        "llm_call_intent"
    )

    result = monitor_report_rows([row])

    assert result["status"] == "failed"
    assert "does not match the pre-network call intent" in (
        result["malformed_reports"][0]["error"]
    )


def test_historical_cohorts_do_not_mix_reviewer_call_purposes():
    rows = [
        _row(
            1,
            "2026-07-30T12:00:00+00:00",
            reviewer_call_purpose="compliance",
            mandatory_count=1,
        ),
        _row(
            2,
            "2026-07-30T12:01:00+00:00",
            reviewer_call_purpose="final_signoff",
            mandatory_count=7,
        ),
        _row(
            101,
            "2026-07-30T14:00:00+00:00",
            reviewer_call_purpose="compliance",
            mandatory_count=1,
        ),
        _row(
            102,
            "2026-07-30T14:01:00+00:00",
            reviewer_call_purpose="final_signoff",
            mandatory_count=7,
        ),
    ]

    result = monitor_report_rows(rows, minimum_baseline=1)
    comparisons = {
        (
            item["platform"],
            item["vertical"],
            item["reviewer_call_purpose"],
        ): item
        for item in result["mandatory_edit_comparisons"]
    }

    compliance = comparisons[
        ("AccessNewsWire", "device", "compliance")
    ]
    final = comparisons[
        ("AccessNewsWire", "device", "final_signoff")
    ]
    assert compliance["historical_comparable"][
        "mandatory_edit_mean"
    ] == 1
    assert final["historical_comparable"]["mandatory_edit_mean"] == 7
    assert result["historical_band"]["violation_count"] == 0


def test_loader_uses_pre_network_intent_for_zero_candidates(
    tmp_path,
):
    db_path = Path(tmp_path) / "zero-candidate.db"
    row = _row(77, "2026-07-30T14:00:00+00:00")
    review = row["payload"]["editorial_truth_review"]
    review["candidate_set_hash"] = sha256(b"[]").hexdigest()
    review["decisions"] = []
    with sqlite3.connect(str(db_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT,
                platform TEXT,
                vertical TEXT
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                project_id TEXT,
                event_type TEXT,
                article_hash TEXT,
                payload TEXT,
                created_at TEXT
            );
            CREATE TABLE llm_calls (
                id INTEGER PRIMARY KEY,
                project_id TEXT,
                stage TEXT,
                status TEXT,
                lifecycle TEXT,
                raw_output TEXT,
                input_article_hash TEXT,
                created_at TEXT,
                expected_candidate_count INTEGER,
                expected_candidate_set_hash TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO projects VALUES(?,?,?,?)",
            ("project-77", "Zero Candidate", "AccessNewsWire", "device"),
        )
        connection.execute(
            "INSERT INTO llm_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                77,
                "project-77",
                "compliance",
                "success",
                "applied",
                json.dumps(row["payload"]),
                "article-zero",
                row["created_at"],
                0,
                sha256(b"[]").hexdigest(),
            ),
        )

    loaded = load_report_rows(db_path)

    assert loaded[0]["reviewer_expected_candidate_count"] == 0
    assert loaded[0]["reviewer_expected_candidate_set_hash"] == (
        sha256(b"[]").hexdigest()
    )
    assert loaded[0]["reviewer_expected_candidate_count_source"] == (
        "llm_call_intent"
    )
    result = monitor_report_rows(loaded)
    assert result["passed_so_far"] is True
    assert result["status"] == "collecting"


def test_applied_report_cannot_hide_candidate_omitted_by_reviewer(
    tmp_path,
):
    db_path = Path(tmp_path) / "omitted-candidate.db"
    row = _row(78, "2026-07-30T14:00:00+00:00")
    review = row["payload"]["editorial_truth_review"]
    review["candidate_set_hash"] = sha256(b"[]").hexdigest()
    review["decisions"] = []
    applied_payload = copy.deepcopy(row["payload"])
    applied_payload["approval_purpose"] = "compliance"
    with sqlite3.connect(str(db_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT,
                platform TEXT,
                vertical TEXT
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                project_id TEXT,
                event_type TEXT,
                article_hash TEXT,
                payload TEXT,
                created_at TEXT
            );
            CREATE TABLE llm_calls (
                id INTEGER PRIMARY KEY,
                project_id TEXT,
                stage TEXT,
                status TEXT,
                lifecycle TEXT,
                raw_output TEXT,
                input_article_hash TEXT,
                created_at TEXT,
                expected_candidate_count INTEGER,
                expected_candidate_set_hash TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO projects VALUES(?,?,?,?)",
            ("project-78", "Omitted Candidate", "AccessNewsWire", "device"),
        )
        connection.execute(
            "INSERT INTO llm_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                78,
                "project-78",
                "compliance",
                "success",
                "applied",
                json.dumps(row["payload"]),
                "article-one",
                row["created_at"],
                1,
                "candidate-set-78",
            ),
        )
        # Even a later applied event that mirrors the incomplete response must
        # not become the authority for expected scope.
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (
                178,
                "project-78",
                "compliance_report",
                "article-one",
                json.dumps(applied_payload),
                "2026-07-30T14:00:01+00:00",
            ),
        )

    result = monitor_report_rows(load_report_rows(db_path))

    assert result["status"] == "failed"
    assert result["passed_so_far"] is False
    assert (
        "does not match the pre-network call intent"
        in result["malformed_reports"][0]["error"]
    )


def test_complete_cohort_fails_when_edit_counts_leave_historical_band():
    rows = [
        _row(
            index,
            f"2026-07-30T12:{index:02d}:00+00:00",
            mandatory_count=0,
        )
        for index in range(1, 4)
    ]
    rows.extend(
        _row(
            100 + index,
            f"2026-07-30T14:{index:02d}:00+00:00",
            mandatory_count=100,
        )
        for index in range(25)
    )

    result = monitor_report_rows(rows)

    assert result["cohort_complete"] is True
    assert result["historical_band"]["evaluable_for_all_groups"] is True
    assert result["historical_band"]["violation_count"] == 25
    assert result["passed"] is False
    assert result["status"] == "failed"


def test_complete_clean_cohort_requires_sufficient_comparable_baseline():
    baseline = [
        _row(
            index,
            f"2026-07-30T12:{index:02d}:00+00:00",
            mandatory_count=index % 3,
        )
        for index in range(1, 4)
    ]
    cohort = [
        _row(
            100 + index,
            f"2026-07-30T14:{index:02d}:00+00:00",
            mandatory_count=index % 3,
        )
        for index in range(25)
    ]

    result = monitor_report_rows(baseline + cohort)

    assert result["cohort_complete"] is True
    assert result["historical_band"]["evaluable_for_all_groups"] is True
    assert result["historical_band"]["all_reports_within_band"] is True
    assert result["passed"] is True
    assert result["status"] == "passed"


def test_sqlite_report_loader_is_read_only(tmp_path):
    db_path = Path(tmp_path) / "workbench.db"
    with sqlite3.connect(str(db_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT,
                platform TEXT,
                vertical TEXT
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                project_id TEXT,
                event_type TEXT,
                article_hash TEXT,
                payload TEXT,
                created_at TEXT
            );
            CREATE TABLE llm_calls (
                id INTEGER PRIMARY KEY,
                project_id TEXT,
                stage TEXT,
                status TEXT,
                lifecycle TEXT,
                raw_output TEXT,
                input_article_hash TEXT,
                created_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO projects VALUES(?,?,?,?)",
            ("p1", "Read Only", "AccessNewsWire", "device"),
        )
        connection.execute(
            "INSERT INTO llm_calls VALUES(?,?,?,?,?,?,?,?)",
            (
                7,
                "p1",
                "compliance",
                "success",
                "applied",
                '{"verdict":"approved"}',
                "article-1",
                DEFAULT_SCHEMA_CUTOVER,
            ),
        )
        connection.execute(
            "INSERT INTO llm_calls VALUES(?,?,?,?,?,?,?,?)",
            (
                8,
                "p1",
                "final_signoff",
                "success",
                "invalid",
                json.dumps({
                    "verdict": "not_approved",
                    "mandatory_edits": [],
                    "editorial_truth_review": {
                        "decisions": [{
                            "sentence_id": "S-invalid",
                            "source_ids": ["E-invalid"],
                        }],
                    },
                }),
                "article-1",
                "2026-07-30T14:00:01+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (
                1,
                "p1",
                "compliance_report",
                "article-1",
                json.dumps({
                    "verdict": "approved",
                    "mandatory_edits": [],
                    "approval_purpose": "compliance",
                }),
                DEFAULT_SCHEMA_CUTOVER,
            ),
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (
                2,
                "p1",
                "compliance_report",
                "article-1",
                json.dumps({
                    "verdict": "approved",
                    "mandatory_edits": [],
                }),
                DEFAULT_SCHEMA_CUTOVER,
            ),
        )
    before = sha256(db_path.read_bytes()).hexdigest()
    original_mode = db_path.stat().st_mode
    os.chmod(db_path, 0o444)
    try:
        rows = load_report_rows(db_path)
    finally:
        os.chmod(db_path, original_mode)
    after = sha256(db_path.read_bytes()).hexdigest()

    assert len(rows) == 2
    assert rows[0]["project_id"] == "p1"
    assert rows[0]["reviewer_call_id"] == 7
    assert rows[1]["reviewer_call_lifecycle"] == "invalid"
    result = monitor_report_rows(rows)
    assert result["reports_observed"] == 2
    assert result["ignored_non_reviewer_report_events"] == 0
    assert result["excerpt_id_leaks"]["leaked_ids"] == ["E-invalid"]
    assert result["status"] == "failed"
    assert before == after
