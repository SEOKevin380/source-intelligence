"""Tests for database.py migration v3 — New tables and bridge column."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import ProductDatabase, CURRENT_SCHEMA_VERSION


@pytest.fixture
def tmp_db():
    """Create a temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = ProductDatabase(db_path=path)
    yield db, path
    db.close()
    os.unlink(path)


class TestMigrationV3:
    def test_schema_version_is_6(self):
        assert CURRENT_SCHEMA_VERSION == 6

    def test_offerings_table_exists(self, tmp_db):
        db, path = tmp_db
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='offerings'"
        ).fetchone()
        assert tables is not None

    def test_artifacts_table_exists(self, tmp_db):
        db, path = tmp_db
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='artifacts'"
        ).fetchone()
        assert tables is not None

    def test_claims_table_exists(self, tmp_db):
        db, path = tmp_db
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='claims'"
        ).fetchone()
        assert tables is not None

    def test_jobs_table_exists(self, tmp_db):
        db, path = tmp_db
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        assert tables is not None

    def test_job_checkpoints_table_exists(self, tmp_db):
        db, path = tmp_db
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='job_checkpoints'"
        ).fetchone()
        assert tables is not None

    def test_products_table_still_works(self, tmp_db):
        """Existing products table should still function after migration."""
        db, path = tmp_db
        # Insert a product using existing system
        db.upsert_product("test-product", {
            "product": {"product_name": "Test", "brand_name": "TestBrand"},
        })
        product = db.get_product("test-product")
        assert product is not None
        assert product["product_name"] == "Test"

    def test_offerings_table_schema(self, tmp_db):
        """Offerings table should have expected columns."""
        db, path = tmp_db
        # Insert a test offering
        db.conn.execute("""
            INSERT INTO offerings (
                offering_id, offering_type, name, url,
                composition_json, policies_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("test-123", "supplement", "TestProduct", "https://test.com",
              '{"ingredients": []}', '{}',
              "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        db.conn.commit()

        row = db.conn.execute(
            "SELECT * FROM offerings WHERE offering_id = 'test-123'"
        ).fetchone()
        assert row is not None
        assert dict(row)["offering_type"] == "supplement"

    def test_idempotent_migration(self, tmp_db):
        """Running migration twice should not fail."""
        db, path = tmp_db
        # Migration already ran in __init__. Create a new instance
        # to trigger migration check again.
        db2 = ProductDatabase(db_path=path)
        # Should not raise
        tables = db2.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()
        assert tables[0] >= 5  # At least products + 4 new tables
        db2.close()

    def test_indexes_created(self, tmp_db):
        """Migration should create indexes on new tables."""
        db, path = tmp_db
        indexes = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        index_names = [r[0] for r in indexes]
        # Claims and jobs tables should have indexes
        assert any("claims" in n for n in index_names)
        assert any("jobs" in n for n in index_names)


class TestMigrationV5:
    """V5 migration: recovery_audit_events table."""

    def test_v4_db_upgrades_to_v5_with_audit_table(self):
        """A database at v4 should receive the audit table on upgrade."""
        import sqlite3
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            # Create a full v5 database first, then simulate v4 by
            # dropping the audit table and downgrading the version.
            db1 = ProductDatabase(db_path=path)
            db1.conn.execute("DROP TABLE IF EXISTS recovery_audit_events")
            db1.conn.execute("DROP INDEX IF EXISTS idx_audit_offering")
            db1.conn.execute("DROP INDEX IF EXISTS idx_audit_job")
            db1.conn.execute("DROP INDEX IF EXISTS idx_audit_type")
            db1.conn.execute("PRAGMA user_version = 4")
            db1.conn.commit()

            # Confirm no audit table
            row = db1.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='recovery_audit_events'"
            ).fetchone()
            assert row is None, "Audit table should not exist at v4"
            db1.close()

            # Re-open — v5 migration should create the audit table
            db2 = ProductDatabase(db_path=path)
            ver = db2.conn.execute("PRAGMA user_version").fetchone()[0]
            assert ver == 6

            # Audit table should now exist
            row = db2.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='recovery_audit_events'"
            ).fetchone()
            assert row is not None, "Audit table must be created by v5"

            # Verify audit indexes
            indexes = db2.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_audit%'"
            ).fetchall()
            assert len(indexes) >= 3, \
                f"Expected ≥3 audit indexes, got {len(indexes)}"

            # Verify immutability triggers
            triggers = db2.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_audit%'"
            ).fetchall()
            trigger_names = [r[0] for r in triggers]
            assert "trg_audit_no_update" in trigger_names, \
                "UPDATE trigger missing on recovery_audit_events"
            assert "trg_audit_no_delete" in trigger_names, \
                "DELETE trigger missing on recovery_audit_events"
            db2.close()
        finally:
            os.unlink(path)

    def test_v5_db_upgrades_to_v6_with_triggers(self):
        """A database already at v5 (lacking triggers) should receive
        immutability triggers via the v6 migration."""
        import sqlite3
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            # Create a full current database, then strip triggers and
            # downgrade to v5 to simulate a pre-trigger v5 database.
            db1 = ProductDatabase(db_path=path)
            db1.conn.execute("DROP TRIGGER IF EXISTS trg_audit_no_update")
            db1.conn.execute("DROP TRIGGER IF EXISTS trg_audit_no_delete")
            db1.conn.execute("PRAGMA user_version = 5")
            db1.conn.commit()

            # Confirm no triggers
            triggers = db1.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_audit%'"
            ).fetchall()
            assert len(triggers) == 0, "Triggers should not exist at v5"
            db1.close()

            # Re-open — v6 migration should add the triggers
            db2 = ProductDatabase(db_path=path)
            ver = db2.conn.execute("PRAGMA user_version").fetchone()[0]
            assert ver == 6

            triggers = db2.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_audit%'"
            ).fetchall()
            trigger_names = [r[0] for r in triggers]
            assert "trg_audit_no_update" in trigger_names
            assert "trg_audit_no_delete" in trigger_names

            # Verify the triggers actually work
            db2.conn.execute(
                "INSERT INTO recovery_audit_events "
                "(event_type, offering_id, job_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("test", "off-v6", "job-v6", "2026-01-01T00:00:00Z"),
            )
            db2.conn.commit()

            with pytest.raises(Exception):
                db2.conn.execute(
                    "UPDATE recovery_audit_events SET error='tampered' "
                    "WHERE offering_id='off-v6'"
                )

            with pytest.raises(Exception):
                db2.conn.execute(
                    "DELETE FROM recovery_audit_events "
                    "WHERE offering_id='off-v6'"
                )

            db2.close()
        finally:
            os.unlink(path)
