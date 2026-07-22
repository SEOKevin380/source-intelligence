"""Tests for database.py v4 repair migration."""

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import ProductDatabase, CURRENT_SCHEMA_VERSION


@pytest.fixture
def tmp_db_path():
    """Return a temp path (no DB created yet)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


def _create_v3_db_with_missing_columns(path):
    """Simulate a broken v3 database: tables exist but with missing columns."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Create a complete products table matching the base schema
    # (ProductDatabase._ensure_tables creates indexes on these columns)
    conn.execute("""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_key TEXT UNIQUE NOT NULL,
        product_name TEXT NOT NULL,
        brand TEXT,
        product_type TEXT,
        category TEXT,
        product_url TEXT,
        risk_level TEXT,
        ingredient_count INTEGER DEFAULT 0,
        study_count INTEGER DEFAULT 0,
        research_json TEXT,
        first_researched TEXT,
        last_updated TEXT,
        research_version INTEGER DEFAULT 1,
        quality_score INTEGER DEFAULT 0,
        quality_flags TEXT,
        notes TEXT,
        verification_state TEXT DEFAULT 'unverified',
        caers_status TEXT DEFAULT '',
        research_updated_at TEXT,
        research_hash TEXT
    )""")
    # Also create publications and generation_log (expected by _ensure_tables)
    conn.execute("""CREATE TABLE IF NOT EXISTS publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id),
        site_key TEXT NOT NULL,
        site_name TEXT,
        post_url TEXT,
        slug TEXT,
        slug_angle TEXT,
        content_type TEXT,
        platform TEXT,
        published_date TEXT,
        wp_post_id INTEGER,
        UNIQUE(product_id, site_key, slug)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS generation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id),
        platform TEXT,
        content_type TEXT,
        target_site TEXT,
        generated_at TEXT,
        prompt_hash TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_key ON products(product_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_product ON publications(product_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_site ON publications(site_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_genlog_product ON generation_log(product_id)")

    # Create incomplete v3 tables — missing several columns each
    conn.execute("""CREATE TABLE IF NOT EXISTS offerings (
        offering_id TEXT PRIMARY KEY,
        offering_type TEXT NOT NULL DEFAULT 'unknown',
        name TEXT NOT NULL,
        url TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    # Missing: brand_name, organization_name, category, market,
    #          composition_json, policies_json, metadata_json, legacy_product_key

    conn.execute("""CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id TEXT PRIMARY KEY,
        artifact_type TEXT NOT NULL,
        source_url TEXT,
        source_class TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )""")
    # Missing: final_url, source_relationship, content_length, tls_verified,
    #          status_code, elapsed_ms, error, content_path, content_inline,
    #          offering_id, job_id, acquisition_phase, notes

    conn.execute("""CREATE TABLE IF NOT EXISTS claims (
        claim_id TEXT PRIMARY KEY,
        offering_id TEXT NOT NULL,
        claim_text TEXT NOT NULL,
        claim_type TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        source_class TEXT NOT NULL
    )""")
    # Missing: source_artifact_id, exact_excerpt, page_location, confidence,
    #          extraction_method, effective_market, review_status, reviewed_by,
    #          reviewed_at, conflicts_json, metadata_json

    conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'created',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    # Missing: offering_id, url, product_name, current_stage, quick_mode,
    #          completed_at, error, budget_seconds, elapsed_seconds,
    #          stage_data_json, stage_status_json, metadata_json

    conn.execute("""CREATE TABLE IF NOT EXISTS job_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        status TEXT NOT NULL
    )""")
    # Missing: started_at, completed_at, result_json, error

    # Stamp as v3 so _migrate_v3 won't re-run
    conn.execute(f"PRAGMA user_version = 3")
    conn.commit()
    conn.close()


def _get_columns(conn, table):
    """Return set of column names for a table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


class TestMigrationV4:
    def test_schema_version_is_6(self):
        assert CURRENT_SCHEMA_VERSION == 6

    def test_fresh_db_creates_all_tables(self, tmp_db_path):
        """A brand new database should have all tables with all columns."""
        db = ProductDatabase(db_path=tmp_db_path)
        ver = db.conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == CURRENT_SCHEMA_VERSION
        for table in ["offerings", "artifacts", "claims", "jobs",
                       "job_checkpoints", "recovery_audit_events"]:
            row = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            ).fetchone()
            assert row is not None, f"Table {table} missing"
        db.close()

    def test_repairs_missing_offerings_columns(self, tmp_db_path):
        """v4 should add missing columns to an incomplete offerings table."""
        _create_v3_db_with_missing_columns(tmp_db_path)

        db = ProductDatabase(db_path=tmp_db_path)
        cols = _get_columns(db.conn, "offerings")
        for expected in ["brand_name", "organization_name", "category",
                         "market", "composition_json", "policies_json",
                         "metadata_json", "legacy_product_key"]:
            assert expected in cols, f"offerings.{expected} missing after v4"
        db.close()

    def test_repairs_missing_artifacts_columns(self, tmp_db_path):
        _create_v3_db_with_missing_columns(tmp_db_path)

        db = ProductDatabase(db_path=tmp_db_path)
        cols = _get_columns(db.conn, "artifacts")
        for expected in ["final_url", "source_relationship", "content_length",
                         "tls_verified", "status_code", "elapsed_ms", "error",
                         "content_path", "content_inline", "offering_id",
                         "job_id", "acquisition_phase", "notes"]:
            assert expected in cols, f"artifacts.{expected} missing after v4"
        db.close()

    def test_repairs_missing_claims_columns(self, tmp_db_path):
        _create_v3_db_with_missing_columns(tmp_db_path)

        db = ProductDatabase(db_path=tmp_db_path)
        cols = _get_columns(db.conn, "claims")
        for expected in ["source_artifact_id", "exact_excerpt", "page_location",
                         "confidence", "extraction_method", "effective_market",
                         "review_status", "reviewed_by", "reviewed_at",
                         "conflicts_json", "metadata_json"]:
            assert expected in cols, f"claims.{expected} missing after v4"
        db.close()

    def test_repairs_missing_jobs_columns(self, tmp_db_path):
        _create_v3_db_with_missing_columns(tmp_db_path)

        db = ProductDatabase(db_path=tmp_db_path)
        cols = _get_columns(db.conn, "jobs")
        for expected in ["offering_id", "url", "product_name", "current_stage",
                         "quick_mode", "completed_at", "error", "budget_seconds",
                         "elapsed_seconds", "stage_data_json",
                         "stage_status_json", "metadata_json"]:
            assert expected in cols, f"jobs.{expected} missing after v4"
        db.close()

    def test_repairs_missing_checkpoint_columns(self, tmp_db_path):
        _create_v3_db_with_missing_columns(tmp_db_path)

        db = ProductDatabase(db_path=tmp_db_path)
        cols = _get_columns(db.conn, "job_checkpoints")
        for expected in ["started_at", "completed_at", "result_json", "error"]:
            assert expected in cols, f"job_checkpoints.{expected} missing after v4"
        db.close()

    def test_preserves_existing_data(self, tmp_db_path):
        """v4 repair must not delete rows from existing tables."""
        _create_v3_db_with_missing_columns(tmp_db_path)

        # Insert data into the incomplete tables
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("""INSERT INTO offerings (offering_id, offering_type, name,
            url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)""",
            ("off-1", "supplement", "TestProduct", "https://test.com",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.execute("""INSERT INTO jobs (job_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?)""",
            ("job-1", "completed", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.execute("""INSERT INTO claims (claim_id, offering_id, claim_text,
            claim_type, captured_at, source_class) VALUES (?, ?, ?, ?, ?, ?)""",
            ("clm-1", "off-1", "Contains 500mg Vitamin C", "ingredient_amount",
             "2026-01-01T00:00:00Z", "official_vendor"))
        conn.commit()
        conn.close()

        # Run v4 migration
        db = ProductDatabase(db_path=tmp_db_path)

        # Verify data survived
        off = db.conn.execute(
            "SELECT * FROM offerings WHERE offering_id = 'off-1'"
        ).fetchone()
        assert off is not None
        assert dict(off)["name"] == "TestProduct"

        job = db.conn.execute(
            "SELECT * FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        assert job is not None
        assert dict(job)["status"] == "completed"

        claim = db.conn.execute(
            "SELECT * FROM claims WHERE claim_id = 'clm-1'"
        ).fetchone()
        assert claim is not None
        assert dict(claim)["claim_text"] == "Contains 500mg Vitamin C"
        # New columns should have defaults
        d = dict(claim)
        assert d["confidence"] is not None  # Default 0.0 or NULL
        assert d["review_status"] is not None

        db.close()

    def test_indexes_created_after_repair(self, tmp_db_path):
        """v4 should ensure all indexes exist even on repaired DBs."""
        _create_v3_db_with_missing_columns(tmp_db_path)

        db = ProductDatabase(db_path=tmp_db_path)
        indexes = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        index_names = {r[0] for r in indexes}
        expected_indexes = [
            "idx_artifacts_offering", "idx_artifacts_source_class",
            "idx_artifacts_job", "idx_artifacts_captured",
            "idx_claims_offering", "idx_claims_source",
            "idx_claims_type", "idx_claims_review",
            "idx_jobs_offering", "idx_jobs_status",
            "idx_checkpoints_job",
        ]
        for idx in expected_indexes:
            assert idx in index_names, f"Index {idx} missing after v4"
        db.close()

    def test_idempotent(self, tmp_db_path):
        """Running v4 migration multiple times should not fail or duplicate."""
        db1 = ProductDatabase(db_path=tmp_db_path)
        # Insert test data
        db1.conn.execute("""INSERT INTO offerings (offering_id, offering_type, name,
            created_at, updated_at) VALUES (?, ?, ?, ?, ?)""",
            ("off-idem", "supplement", "Idempotent Test",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        db1.conn.commit()
        db1.close()

        # Re-open (triggers migration check again)
        db2 = ProductDatabase(db_path=tmp_db_path)
        row = db2.conn.execute(
            "SELECT * FROM offerings WHERE offering_id = 'off-idem'"
        ).fetchone()
        assert row is not None
        assert dict(row)["name"] == "Idempotent Test"

        ver = db2.conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == CURRENT_SCHEMA_VERSION
        db2.close()

    def test_v3_to_v4_upgrade_with_complete_schema(self, tmp_db_path):
        """A correct v3 DB should upgrade to v4 with no repairs needed."""
        # Create a proper v3 database first
        db1 = ProductDatabase(db_path=tmp_db_path)
        db1.close()

        # Downgrade version to 3 to simulate a pre-v4 database
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()

        # Re-open — should upgrade cleanly to latest
        db2 = ProductDatabase(db_path=tmp_db_path)
        ver = db2.conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == CURRENT_SCHEMA_VERSION

        # All tables and columns should still be intact
        for table in ["offerings", "artifacts", "claims", "jobs",
                       "job_checkpoints", "recovery_audit_events"]:
            row = db2.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            ).fetchone()
            assert row is not None
        db2.close()

    def test_products_bridge_column_added(self, tmp_db_path):
        """The offering_id bridge column on products should exist after v4."""
        _create_v3_db_with_missing_columns(tmp_db_path)

        db = ProductDatabase(db_path=tmp_db_path)
        cols = _get_columns(db.conn, "products")
        assert "offering_id" in cols
        db.close()

    def test_new_columns_have_usable_defaults(self, tmp_db_path):
        """Newly added columns should have workable default values."""
        _create_v3_db_with_missing_columns(tmp_db_path)

        # Pre-insert data before repair
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("""INSERT INTO jobs (job_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?)""",
            ("job-def", "running", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.commit()
        conn.close()

        db = ProductDatabase(db_path=tmp_db_path)
        row = db.conn.execute(
            "SELECT * FROM jobs WHERE job_id = 'job-def'"
        ).fetchone()
        d = dict(row)

        # workflow.py's JobStore._row_to_job expects these fields
        # and calls json.loads on them — they must be valid JSON or empty
        stage_data = d.get("stage_data_json")
        stage_status = d.get("stage_status_json")
        metadata = d.get("metadata_json")

        # Should be parseable (None or valid JSON string)
        if stage_data is not None:
            json.loads(stage_data)  # Should not raise
        if stage_status is not None:
            json.loads(stage_status)
        if metadata is not None:
            json.loads(metadata)

        db.close()
