"""Tests for database migrations, freshness tracking, and thread safety."""

import copy
import hashlib
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import ProductDatabase, CURRENT_SCHEMA_VERSION


def _legacy_v2_device_pack(human_accepted=True):
    from source_pack_contract import (
        CONTRACT_NAME,
        _canonical_payload,
        seal_source_pack,
    )

    current = seal_source_pack({
        "product": {
            "product_name": "Legacy Device",
            "official_url": "https://example.com/device",
            "product_type": "device",
        },
        "all_artifacts": {
            "a1": {
                "source_url": "https://example.com/device",
                "source_class": "official_vendor",
            },
        },
        "claims_by_type": {
            "feature": [
                {
                    "claim_id": f"feature-{number}",
                    "text": f"Legacy feature {number}",
                    "artifact_id": "a1",
                    "source_class": "official_vendor",
                    "review_status": (
                        "accepted" if human_accepted else "unreviewed"
                    ),
                    **({
                        "reviewed_by": "Migration Test Reviewer",
                    } if human_accepted else {}),
                    "metadata": {
                        "excerpt_is_literal": True,
                        "fact_key": "key_features",
                    },
                }
                for number in range(3)
            ],
        },
        "required_facts": {"missing": []},
    })
    legacy = copy.deepcopy(current)
    legacy.pop("source_pack_contract_migrations", None)
    legacy["source_pack_contract"] = {
        "name": CONTRACT_NAME,
        "version": 2,
        "generated_at": "2026-07-29T00:00:00+00:00",
        "readiness": "complete",
        "readiness_reasons": [],
        "source_of_truth": "source_intelligence",
        "generation_system": "MBK Master Content Generation System v3.8",
    }
    legacy["source_pack_contract"]["sha256"] = hashlib.sha256(
        _canonical_payload(legacy)
    ).hexdigest()
    return legacy


@pytest.fixture
def tmp_db():
    """Create a temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = ProductDatabase(db_path=path)
    yield db
    db.close()
    os.unlink(path)


def _make_research(name="Test Product", brand="Test Brand", **extra):
    """Build minimal research_data dict for upsert."""
    data = {
        "product": {
            "product_name": name,
            "brand_name": brand,
        },
        "ingredient_research": {},
        "safety": {},
        "compliance": {},
        "reputation": {},
    }
    data.update(extra)
    return data


class TestSchemaVersion:
    def test_schema_version_is_8(self):
        assert CURRENT_SCHEMA_VERSION == 8

    def test_new_db_has_current_version(self, tmp_db):
        version = tmp_db._get_schema_version()
        assert version == CURRENT_SCHEMA_VERSION


def test_persist_completed_pack_replaces_stale_shared_report(tmp_path):
    from database import persist_completed_pack

    db_path = str(tmp_path / "crm.db")
    stale = {
        "product": {
            "product_name": "T-Max",
            "product_type": "supplement",
            "supplement_facts": {"ingredients": []},
        },
        "ingredient_research": {},
    }
    repaired = {
        "product": {
            "product_name": "T-Max",
            "product_type": "supplement",
            "supplement_facts": {
                "ingredients": [{"name": "Vitamin B12", "amount": "2500 mcg"}],
            },
        },
        "ingredient_research": {
            "Vitamin B12": {"studies": [{"pmid": "123"}, {"pmid": "456"}]},
        },
    }
    ProductDatabase(db_path=db_path).upsert_product("t-max", stale)
    key = persist_completed_pack(repaired, "t-max", db_path=db_path)
    saved = ProductDatabase(db_path=db_path).get_product(key)

    assert key == "t-max"
    assert saved["study_count"] == 2
    assert saved["research_data"]["ingredient_research"]["Vitamin B12"]["studies"]


def test_persist_completed_pack_rejects_tampered_current_v3(tmp_path):
    from database import persist_completed_pack
    from source_pack_contract import migrate_source_pack

    db_path = str(tmp_path / "tampered-current.db")
    ProductDatabase(db_path=db_path).close()
    current = migrate_source_pack(_legacy_v2_device_pack())
    current["product"]["product_name"] = "Tampered Current Device"

    with pytest.raises(ValueError, match="integrity"):
        persist_completed_pack(
            current,
            preferred_key="tampered-current-device",
            db_path=db_path,
        )

    db = ProductDatabase(db_path=db_path)
    assert db.get_product("tampered-current-device") is None
    assert db.conn.execute(
        "SELECT COUNT(*) FROM source_pack_migration_events"
    ).fetchone()[0] == 0


def test_persist_completed_pack_audits_later_v2_migration(tmp_path):
    from database import persist_completed_pack
    from source_pack_contract import validate_source_pack

    db_path = str(tmp_path / "later-v2.db")
    ProductDatabase(db_path=db_path).close()
    legacy = _legacy_v2_device_pack()
    prior_sha = legacy["source_pack_contract"]["sha256"]

    key = persist_completed_pack(
        legacy,
        preferred_key="later-v2-device",
        db_path=db_path,
    )
    # Replaying the same sealed legacy handoff is idempotent at the audit edge.
    assert persist_completed_pack(
        legacy,
        preferred_key="later-v2-device",
        db_path=db_path,
    ) == key

    db = ProductDatabase(db_path=db_path)
    saved = db.get_product(key)
    events = db.conn.execute(
        """SELECT * FROM source_pack_migration_events
        WHERE product_key=? ORDER BY id""",
        (key,),
    ).fetchall()

    contract = validate_source_pack(saved["research_data"])
    assert contract["version"] == 3
    assert len(events) == 1
    assert events[0]["status"] == "success"
    assert events[0]["prior_sha256"] == prior_sha
    assert events[0]["new_sha256"] == contract["sha256"]


def test_persist_completed_pack_rolls_back_when_audit_insert_fails(tmp_path):
    from database import persist_completed_pack

    db_path = str(tmp_path / "audit-failure.db")
    db = ProductDatabase(db_path=db_path)
    db.conn.execute(
        """CREATE TRIGGER fail_pack_migration_audit
        BEFORE INSERT ON source_pack_migration_events
        BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END"""
    )
    db.conn.commit()
    db.close()

    with pytest.raises(sqlite3.DatabaseError, match="forced audit failure"):
        persist_completed_pack(
            _legacy_v2_device_pack(),
            preferred_key="audit-failure-device",
            db_path=db_path,
        )

    reopened = ProductDatabase(db_path=db_path)
    assert reopened.get_product("audit-failure-device") is None
    assert reopened.conn.execute(
        "SELECT COUNT(*) FROM source_pack_migration_events"
    ).fetchone()[0] == 0


class TestMigrationIdempotency:
    def test_migration_can_run_twice(self, tmp_db):
        """Migrations should be safe to re-run."""
        tmp_db._set_schema_version(0)
        tmp_db._run_migrations()
        assert tmp_db._get_schema_version() == CURRENT_SCHEMA_VERSION

        # Run again — should not crash
        tmp_db._set_schema_version(0)
        tmp_db._run_migrations()
        assert tmp_db._get_schema_version() == CURRENT_SCHEMA_VERSION


def test_v7_reclassifies_legacy_automation_acceptance(tmp_path):
    db_path = str(tmp_path / "legacy-auto.db")
    db = ProductDatabase(db_path=db_path)
    db.conn.execute(
        """INSERT INTO claims (
            claim_id, offering_id, claim_text, claim_type, captured_at,
            source_class, review_status, reviewed_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "legacy-auto",
            "offering-1",
            "Policy-safe replacement",
            "manufacturer_claim",
            "2026-07-29T00:00:00+00:00",
            "official_vendor",
            "accepted",
            "source-intelligence-automation",
        ),
    )
    db.conn.execute("PRAGMA user_version = 6")
    db.conn.commit()
    db.close()

    migrated = ProductDatabase(db_path=db_path)
    row = migrated.conn.execute(
        "SELECT review_status, reviewed_by FROM claims WHERE claim_id=?",
        ("legacy-auto",),
    ).fetchone()

    assert row["review_status"] == "auto_substituted"
    assert row["reviewed_by"] == "source-intelligence-automation"
    assert migrated._get_schema_version() == 8


def test_v7_reassesses_v2_pack_with_immutable_transition_event(tmp_path):
    db_path = str(tmp_path / "legacy-pack.db")
    db = ProductDatabase(db_path=db_path)
    legacy = _legacy_v2_device_pack()
    prior_sha = legacy["source_pack_contract"]["sha256"]
    db.upsert_product("legacy-device", legacy)
    before = db.conn.execute(
        """SELECT research_version, research_updated_at
        FROM products WHERE product_key=?""",
        ("legacy-device",),
    ).fetchone()
    db.conn.execute("PRAGMA user_version = 6")
    db.conn.commit()
    db.close()

    migrated_db = ProductDatabase(db_path=db_path)
    saved = migrated_db.get_product("legacy-device")
    event = migrated_db.conn.execute(
        """SELECT * FROM source_pack_migration_events
        WHERE product_key=?""",
        ("legacy-device",),
    ).fetchone()

    contract = saved["research_data"]["source_pack_contract"]
    transition = saved["research_data"][
        "source_pack_contract_migrations"
    ][0]
    assert contract["version"] == 3
    assert transition["prior_sha256"] == prior_sha
    assert event["event_type"] == "source_pack_contract_migrated"
    assert event["status"] == "success"
    assert event["prior_sha256"] == prior_sha
    assert event["new_sha256"] == contract["sha256"]
    assert event["prior_readiness"] == "complete"
    # A legacy reviewer label has no signed review transition.  V3 therefore
    # preserves the old decision in audit history but reassesses it as limited.
    assert event["new_readiness"] == "limited"
    assert saved["research_version"] == before["research_version"] + 1
    assert saved["research_updated_at"] == before["research_updated_at"]
    with pytest.raises(sqlite3.DatabaseError):
        migrated_db.conn.execute(
            """UPDATE source_pack_migration_events
            SET status='tampered' WHERE id=?""",
            (event["id"],),
        )


def test_v7_downgrades_single_seller_v2_and_surfaces_review_queue(tmp_path):
    db_path = str(tmp_path / "legacy-unreviewed-pack.db")
    db = ProductDatabase(db_path=db_path)
    legacy = _legacy_v2_device_pack(human_accepted=False)
    db.upsert_product("legacy-unreviewed-device", legacy)
    db.conn.execute("PRAGMA user_version = 6")
    db.conn.commit()
    db.close()

    migrated_db = ProductDatabase(db_path=db_path)
    saved = migrated_db.get_product("legacy-unreviewed-device")
    event = migrated_db.conn.execute(
        """SELECT * FROM source_pack_migration_events
        WHERE product_key=?""",
        ("legacy-unreviewed-device",),
    ).fetchone()
    queue = migrated_db.list_source_verification_queue()

    contract = saved["research_data"]["source_pack_contract"]
    assert contract["version"] == 3
    assert contract["readiness"] == "limited"
    assert contract["unverified_mandatory_facts"] == ["key_features"]
    assert event["prior_readiness"] == "complete"
    assert event["new_readiness"] == "limited"
    assert [item["product_key"] for item in queue] == [
        "legacy-unreviewed-device"
    ]


def test_v7_leaves_tampered_v2_pack_untouched_and_records_failure(tmp_path):
    db_path = str(tmp_path / "tampered-pack.db")
    db = ProductDatabase(db_path=db_path)
    legacy = _legacy_v2_device_pack()
    legacy["product"]["product_name"] = "Tampered Device"
    db.upsert_product("tampered-device", legacy)
    db.conn.execute("PRAGMA user_version = 6")
    db.conn.commit()
    db.close()

    migrated_db = ProductDatabase(db_path=db_path)
    saved = migrated_db.get_product("tampered-device")
    event = migrated_db.conn.execute(
        """SELECT * FROM source_pack_migration_events
        WHERE product_key=?""",
        ("tampered-device",),
    ).fetchone()

    assert saved["research_data"]["source_pack_contract"]["version"] == 2
    assert event["event_type"] == "source_pack_contract_migration_failed"
    assert event["status"] == "failed"
    assert "integrity" in event["error"].casefold()
    queue = migrated_db.list_source_verification_queue()
    assert [item["product_key"] for item in queue] == ["tampered-device"]
    assert queue[0]["action"] == "repair_source_contract"
    assert queue[0]["may_start_paid_call"] is False


def test_v7_contains_malformed_v2_row_and_migrates_other_rows(tmp_path):
    from source_pack_contract import _canonical_payload

    db_path = str(tmp_path / "mixed-v2-rows.db")
    db = ProductDatabase(db_path=db_path)
    good = _legacy_v2_device_pack()
    bad = copy.deepcopy(good)
    bad["product"] = []
    bad["source_pack_contract"]["sha256"] = hashlib.sha256(
        _canonical_payload(bad)
    ).hexdigest()
    bad_json = json.dumps(bad)

    db.upsert_product("good-v2-device", good)
    db.conn.execute(
        """INSERT INTO products (
            product_key, product_name, research_json, research_hash,
            first_researched, last_updated, research_version, quality_flags
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "malformed-v2-device",
            "Malformed V2 Device",
            bad_json,
            hashlib.sha256(bad_json.encode()).hexdigest(),
            "2026-07-30T00:00:00",
            "2026-07-30T00:00:00",
            1,
            "[]",
        ),
    )
    db.conn.execute("PRAGMA user_version = 6")
    db.conn.commit()
    db.close()

    migrated = ProductDatabase(db_path=db_path)
    good_saved = migrated.get_product("good-v2-device")
    bad_saved = migrated.get_product("malformed-v2-device")
    events = migrated.conn.execute(
        """SELECT product_key, status, error
        FROM source_pack_migration_events ORDER BY product_key"""
    ).fetchall()

    assert migrated._get_schema_version() == 8
    assert good_saved["research_data"]["source_pack_contract"]["version"] == 3
    assert bad_saved["research_data"]["source_pack_contract"]["version"] == 2
    assert [(row["product_key"], row["status"]) for row in events] == [
        ("good-v2-device", "success"),
        ("malformed-v2-device", "failed"),
    ]
    assert "attribute" in events[1]["error"].casefold()


def test_source_verification_queue_surfaces_only_unverified_v3_packs(tmp_path):
    from source_pack_contract import migrate_source_pack

    db = ProductDatabase(db_path=str(tmp_path / "verification-queue.db"))
    waiting = migrate_source_pack(
        _legacy_v2_device_pack(human_accepted=False)
    )
    ready = _make_research("Ordinary CRM Record")
    db.upsert_product("waiting-device", waiting)
    db.upsert_product("ready-device", ready)

    queue = db.list_source_verification_queue()

    assert [item["product_key"] for item in queue] == ["waiting-device"]
    assert queue[0]["unverified_mandatory_facts"] == ["key_features"]
    assert queue[0]["action"] == "repair_source_ledger"
    assert queue[0]["offering_id"]
    assert queue[0]["claim_count"] == 0
    assert queue[0]["may_start_paid_call"] is False


def test_source_verification_queue_reviews_only_product_scoped_claims(
    tmp_path,
):
    from claims import Claim, ClaimsLedger
    from source_pack_contract import _canonical_payload, migrate_source_pack

    db_path = str(tmp_path / "scoped-verification-queue.db")
    legacy = _legacy_v2_device_pack(human_accepted=False)
    legacy["offering_id"] = "offering-scoped-device"
    legacy["source_pack_contract"]["sha256"] = hashlib.sha256(
        _canonical_payload(legacy)
    ).hexdigest()
    waiting = migrate_source_pack(legacy)
    db = ProductDatabase(db_path=db_path)
    db.upsert_product("scoped-device", waiting)
    db.conn.execute(
        """INSERT INTO artifacts (
            artifact_id, artifact_type, source_url, final_url,
            source_class, source_relationship, captured_at, content_hash,
            content_length, tls_verified, status_code, offering_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "artifact-scoped-device",
            "web_page",
            "https://example.com/device",
            "https://example.com/device",
            "official_vendor",
            "first_party",
            "2026-07-30T00:00:00+00:00",
            "a" * 64,
            10,
            1,
            200,
            "offering-scoped-device",
        ),
    )
    db.conn.commit()
    ClaimsLedger(db_path=db_path).add_claim(Claim(
        offering_id="offering-scoped-device",
        claim_text="Scoped source claim",
        source_artifact_id="artifact-scoped-device",
        metadata={"fact_key": "key_features"},
    ))

    queue = db.list_source_verification_queue()

    assert len(queue) == 1
    assert queue[0]["action"] == "review_source_claims"
    assert queue[0]["offering_id"] == "offering-scoped-device"
    assert queue[0]["claim_count"] == 1


def test_source_verification_queue_ignores_unrelated_or_rejected_claims(
    tmp_path,
):
    from claims import Claim, ClaimsLedger, ReviewStatus
    from source_pack_contract import _canonical_payload, migrate_source_pack

    db_path = str(tmp_path / "narrow-verification-queue.db")
    legacy = _legacy_v2_device_pack(human_accepted=False)
    legacy["offering_id"] = "offering-narrow-device"
    legacy["source_pack_contract"]["sha256"] = hashlib.sha256(
        _canonical_payload(legacy)
    ).hexdigest()
    waiting = migrate_source_pack(legacy)
    db = ProductDatabase(db_path=db_path)
    db.upsert_product("narrow-device", waiting)
    db.conn.execute(
        """INSERT INTO artifacts (
            artifact_id, artifact_type, source_url, final_url,
            source_class, source_relationship, captured_at, content_hash,
            content_length, tls_verified, status_code, offering_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "artifact-narrow-device",
            "web_page",
            "https://example.com/device",
            "https://example.com/device",
            "official_vendor",
            "first_party",
            "2026-07-30T00:00:00+00:00",
            "b" * 64,
            10,
            1,
            200,
            "offering-narrow-device",
        ),
    )
    db.conn.execute(
        """INSERT INTO artifacts (
            artifact_id, artifact_type, source_url, final_url,
            source_class, source_relationship, captured_at, content_hash,
            content_length, tls_verified, status_code, offering_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "artifact-other-device",
            "web_page",
            "https://example.com/other",
            "https://example.com/other",
            "official_vendor",
            "first_party",
            "2026-07-30T00:00:00+00:00",
            "e" * 64,
            10,
            1,
            200,
            "offering-other-device",
        ),
    )
    db.conn.commit()
    ledger = ClaimsLedger(db_path=db_path)
    ledger.add_claim(Claim(
        offering_id="offering-narrow-device",
        claim_text="A price claim is unrelated to key features",
        source_artifact_id="artifact-narrow-device",
        metadata={"fact_key": "pricing"},
    ))
    rejected_id = ledger.add_claim(Claim(
        offering_id="offering-narrow-device",
        claim_text="A rejected feature claim",
        source_artifact_id="artifact-narrow-device",
        metadata={"fact_key": "key_features"},
    ))
    ledger.add_claim(Claim(
        offering_id="offering-narrow-device",
        claim_text="A cross-product artifact must not become reviewable",
        source_artifact_id="artifact-other-device",
        metadata={"fact_key": "key_features"},
    ))
    ledger.update_review(
        rejected_id,
        ReviewStatus.REJECTED,
        reviewer="Alice Example",
    )

    queue = db.list_source_verification_queue()

    assert len(queue) == 1
    assert queue[0]["claim_count"] == 0
    assert queue[0]["action"] == "repair_source_ledger"


def test_v8_records_malformed_json_as_durable_operator_repair(tmp_path):
    db_path = str(tmp_path / "malformed-json-v7.db")
    db = ProductDatabase(db_path=db_path)
    db.conn.execute(
        """INSERT INTO products (
            product_key, product_name, research_json,
            first_researched, last_updated, research_version, quality_flags
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "broken-json",
            "Broken JSON",
            "{not valid json",
            "2026-07-30T00:00:00",
            "2026-07-30T00:00:00",
            1,
            "[]",
        ),
    )
    db.conn.execute("PRAGMA user_version = 7")
    db.conn.commit()
    db.close()

    migrated = ProductDatabase(db_path=db_path)
    events = migrated.conn.execute(
        """SELECT * FROM source_pack_migration_events
        WHERE product_key='broken-json' ORDER BY id"""
    ).fetchall()
    queue = migrated.list_source_verification_queue()

    assert migrated._get_schema_version() == 8
    assert len(events) == 1
    assert events[0]["status"] == "requires_repair"
    assert events[0]["event_type"] == (
        "source_pack_contract_repair_required"
    )
    assert "cannot be parsed" in events[0]["error"]
    assert [item["product_key"] for item in queue] == ["broken-json"]
    assert queue[0]["action"] == "repair_source_contract"
    assert queue[0]["migration_status"] == "requires_repair"
    # Re-running the additive migration is idempotent at the audit edge.
    migrated._set_schema_version(7)
    migrated._run_migrations()
    assert migrated.conn.execute(
        """SELECT COUNT(*) FROM source_pack_migration_events
        WHERE product_key='broken-json'"""
    ).fetchone()[0] == 1

    # The queue's named action is openable even though the original bytes are
    # malformed. The raw record remains available for a deliberate rebuild.
    repair_record = migrated.get_product("broken-json")
    assert repair_record["research_parse_error"]
    repair_shell = repair_record["research_data"]["source_pack_repair"]
    assert repair_shell["repair_required"] is True
    assert repair_shell["repair_owner"] == "source_pack_contract"
    assert repair_shell["raw_research_json"] == "{not valid json"
    assert repair_shell["raw_research_json_sha256"] == hashlib.sha256(
        b"{not valid json"
    ).hexdigest()


def test_v8_repairs_every_column_of_a_partial_artifacts_table(tmp_path):
    db_path = str(tmp_path / "partial-artifacts-v7.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY,
            source_url TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO artifacts(artifact_id, source_url) VALUES (?, ?)",
        ("legacy-artifact", "https://example.com/legacy"),
    )
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()

    migrated = ProductDatabase(db_path=db_path)
    columns = {
        row[1] for row in
        migrated.conn.execute("PRAGMA table_info(artifacts)").fetchall()
    }
    required = {
        "artifact_id", "artifact_type", "source_url", "final_url",
        "source_class", "source_relationship", "captured_at",
        "content_hash", "content_length", "tls_verified", "status_code",
        "elapsed_ms", "error", "content_path", "content_inline",
        "content_inline_blob", "offering_id", "job_id",
        "acquisition_phase", "notes", "capture_attestation_json",
        "capture_route", "corroboration_eligible",
    }

    assert migrated._get_schema_version() == 8
    assert required <= columns

    from evidence import EvidenceLake
    artifact = EvidenceLake(db_path=db_path).get("legacy-artifact")
    assert artifact is not None
    assert artifact.source_class.value == "anonymous"
    assert artifact.corroboration_eligible is False
    assert artifact.is_usable is False


def test_future_database_version_is_rejected_without_downgrade(tmp_path):
    db_path = str(tmp_path / "future-schema.db")
    db = ProductDatabase(db_path=db_path)
    db.conn.execute("PRAGMA user_version = 99")
    db.conn.commit()
    db.close()

    with pytest.raises(RuntimeError, match="newer than this runtime"):
        ProductDatabase(db_path=db_path)

    probe = sqlite3.connect(db_path)
    try:
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 99
    finally:
        probe.close()


def test_valid_reseal_supersedes_older_failure_without_rewriting_history(
    tmp_path,
):
    from source_pack_contract import migrate_source_pack

    db = ProductDatabase(db_path=str(tmp_path / "resolved-repair.db"))
    waiting = migrate_source_pack(
        _legacy_v2_device_pack(human_accepted=False)
    )
    product_id = db.upsert_product("resolved-device", waiting)
    db.conn.execute(
        """INSERT INTO source_pack_migration_events (
            product_id, product_key, event_type,
            from_version, to_version, prior_sha256, new_sha256,
            prior_readiness, new_readiness, status, error,
            payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            product_id,
            "resolved-device",
            "source_pack_contract_migration_failed",
            2,
            3,
            "old-sha",
            "",
            "complete",
            "",
            "failed",
            "Historical failure retained for audit",
            "{}",
            "2026-07-30T00:00:00+00:00",
        ),
    )
    db.conn.commit()

    queue = db.list_source_verification_queue()

    assert len(queue) == 1
    assert queue[0]["product_key"] == "resolved-device"
    assert queue[0]["action"] == "repair_source_ledger"
    assert "repair_reason" not in queue[0]
    assert db.conn.execute(
        """SELECT COUNT(*) FROM source_pack_migration_events
        WHERE product_id=? AND status='failed'""",
        (product_id,),
    ).fetchone()[0] == 1


def test_v8_adds_attestation_columns_to_partial_v7_without_data_loss(
    tmp_path,
):
    db_path = str(tmp_path / "partial-v7.db")
    ProductDatabase(db_path=db_path).close()
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        DROP TABLE artifacts;
        DROP TRIGGER IF EXISTS trg_claim_review_no_update;
        DROP TRIGGER IF EXISTS trg_claim_review_no_delete;
        DROP TABLE claim_review_events;
        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY,
            artifact_type TEXT NOT NULL,
            source_url TEXT,
            final_url TEXT,
            source_class TEXT NOT NULL,
            source_relationship TEXT NOT NULL DEFAULT 'third_party',
            captured_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            content_length INTEGER DEFAULT 0,
            tls_verified INTEGER DEFAULT 1,
            status_code INTEGER DEFAULT 0,
            elapsed_ms REAL DEFAULT 0.0,
            error TEXT DEFAULT '',
            content_path TEXT DEFAULT '',
            content_inline TEXT DEFAULT '',
            offering_id TEXT,
            job_id TEXT,
            acquisition_phase TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        );
        CREATE TABLE claim_review_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL,
            offering_id TEXT NOT NULL,
            prior_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            event_hash TEXT NOT NULL UNIQUE
        );
        INSERT INTO artifacts (
            artifact_id, artifact_type, source_class, source_relationship,
            captured_at, content_hash
        ) VALUES (
            'legacy-artifact', 'web_page', 'official_vendor', 'first_party',
            '2026-07-30T00:00:00+00:00',
            'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
        );
        INSERT INTO claim_review_events (
            claim_id, offering_id, prior_status, new_status, reviewer,
            reviewed_at, event_hash
        ) VALUES (
            'legacy-claim', 'legacy-offering', 'unreviewed', 'accepted',
            'Alice Example', '2026-07-30T00:00:00+00:00',
            'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
        );
        PRAGMA user_version = 7;
    """)
    conn.commit()
    conn.close()

    migrated = ProductDatabase(db_path=db_path)
    artifact_columns = {
        row[1] for row in migrated.conn.execute(
            "PRAGMA table_info(artifacts)"
        ).fetchall()
    }
    review_columns = {
        row[1] for row in migrated.conn.execute(
            "PRAGMA table_info(claim_review_events)"
        ).fetchall()
    }

    assert {
        "content_inline_blob",
        "capture_attestation_json",
        "capture_route",
        "corroboration_eligible",
    } <= artifact_columns
    assert {
        "claim_snapshot_json",
        "payload_json",
        "signature_json",
        "key_id",
    } <= review_columns
    assert migrated.conn.execute(
        "SELECT COUNT(*) FROM artifacts WHERE artifact_id='legacy-artifact'"
    ).fetchone()[0] == 1
    assert migrated.conn.execute(
        """SELECT COUNT(*) FROM claim_review_events
        WHERE claim_id='legacy-claim'"""
    ).fetchone()[0] == 1


class TestFreshnessTracking:
    def test_research_hash_set_on_upsert(self, tmp_db):
        """upsert_product should compute and store research_hash."""
        research = _make_research()
        research_json = json.dumps(research)
        expected_hash = hashlib.sha256(research_json.encode()).hexdigest()

        tmp_db.upsert_product("test-product", research)

        row = tmp_db.conn.execute(
            "SELECT research_hash, research_updated_at FROM products WHERE product_key = ?",
            ("test-product",)
        ).fetchone()

        assert row is not None
        assert row["research_hash"] == expected_hash
        assert row["research_updated_at"] is not None

    def test_unchanged_data_preserves_research_updated_at(self, tmp_db):
        """If research_json hasn't changed, research_updated_at should not change."""
        research = _make_research("Stable Product")

        tmp_db.upsert_product("stable-product", research)
        row1 = tmp_db.conn.execute(
            "SELECT research_updated_at FROM products WHERE product_key = ?",
            ("stable-product",)
        ).fetchone()
        ts1 = row1["research_updated_at"]

        # Second upsert with SAME data
        tmp_db.upsert_product("stable-product", research)
        row2 = tmp_db.conn.execute(
            "SELECT research_updated_at FROM products WHERE product_key = ?",
            ("stable-product",)
        ).fetchone()
        ts2 = row2["research_updated_at"]

        assert ts1 == ts2, "research_updated_at should not change when data is identical"

    def test_changed_data_updates_hash(self, tmp_db):
        """If research_json changes, research_hash should change."""
        research1 = _make_research("Evolving Product")
        research2 = _make_research("Evolving Product")
        research2["product"]["category"] = "brain"  # Add new field

        tmp_db.upsert_product("evolving-product", research1)
        row1 = tmp_db.conn.execute(
            "SELECT research_hash FROM products WHERE product_key = ?",
            ("evolving-product",)
        ).fetchone()

        tmp_db.upsert_product("evolving-product", research2)
        row2 = tmp_db.conn.execute(
            "SELECT research_hash FROM products WHERE product_key = ?",
            ("evolving-product",)
        ).fetchone()

        assert row1["research_hash"] != row2["research_hash"]


class TestCompletenessScoreLabel:
    def test_high_score_says_complete_not_verified(self, tmp_db):
        """Score >= 80 should produce 'COMPLETE' label, not 'VERIFIED'."""
        data = _make_research()
        data["product"].update({
            "product_type": "supplement",
            "category": "brain",
            "supplement_facts": {
                "ingredients": [
                    {"name": "A", "amount": "10mg"},
                    {"name": "B", "amount": "20mg"},
                    {"name": "C", "amount": "30mg"},
                ]
            },
            "claims": [{"claim": "c1"}, {"claim": "c2"}, {"claim": "c3"}],
            "pricing": [{"amount": "$49"}, {"amount": "$39"}],
        })
        data["ingredient_research"] = {
            "A": {"studies": [
                {"title": f"S{i}", "relevance_tags": ["human_study"]}
                for i in range(6)
            ]}
        }
        data["safety"] = {"A": {"side_effects": "Safe"}}
        data["compliance"] = {"risk_level": "Low"}
        data["reputation"] = {"bbb_rating": "A"}

        score, flags = tmp_db.compute_completeness_score(data)
        assert score >= 80
        for f in flags:
            assert "VERIFIED" not in f, f"Found 'VERIFIED' in flag: {f}"

    def test_method_name_is_completeness(self, tmp_db):
        """Method should be named compute_completeness_score."""
        assert hasattr(tmp_db, "compute_completeness_score")
        assert not hasattr(tmp_db, "compute_quality_score")

    def test_stub_label(self, tmp_db):
        """Stub data should get low score."""
        data = {"product": {}}
        score, flags = tmp_db.compute_completeness_score(data)
        assert score < 20
