"""Tests for database migrations, freshness tracking, and thread safety."""

import hashlib
import json
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
    def test_schema_version_is_6(self):
        assert CURRENT_SCHEMA_VERSION == 6

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
