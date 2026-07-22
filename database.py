"""
Source Intelligence — Product Database
=======================================
SQLite persistence layer for the Product Intelligence CRM.
Stores products, publications, generation logs, and quality checks.
"""

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional

# Thread safety for write operations (SQLite handles concurrent reads)
_db_lock = threading.Lock()

# Schema version — increment when adding migrations
CURRENT_SCHEMA_VERSION = 6


def _slugify(name: str) -> str:
    """Convert a product name to a URL-safe key."""
    import re
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


class ProductDatabase:
    """SQLite-backed product intelligence database."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            from config import DB_PATH
            db_path = DB_PATH
        self.db_path = db_path
        self._conn = None
        self._ensure_tables()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _ensure_tables(self):
        """Create tables if they don't exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
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
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS publications (
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
            );

            CREATE TABLE IF NOT EXISTS generation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id),
                platform TEXT,
                content_type TEXT,
                target_site TEXT,
                generated_at TEXT,
                prompt_hash TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_products_key ON products(product_key);
            CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
            CREATE INDEX IF NOT EXISTS idx_publications_product ON publications(product_id);
            CREATE INDEX IF NOT EXISTS idx_publications_site ON publications(site_key);
            CREATE INDEX IF NOT EXISTS idx_genlog_product ON generation_log(product_id);
        """)
        # Add cross-product slug uniqueness (site_key + slug must be unique
        # across ALL products, not just within one product).
        # Check for existing violations before adding constraint.
        try:
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_site_slug "
                "ON publications(site_key, slug)"
            )
        except sqlite3.IntegrityError:
            # Existing violations — log them but don't block startup
            import logging
            dupes = self.conn.execute("""
                SELECT site_key, slug, COUNT(*) as cnt
                FROM publications GROUP BY site_key, slug HAVING cnt > 1
            """).fetchall()
            for d in dupes:
                logging.warning(
                    f"Slug collision in publications: site={d['site_key']}, "
                    f"slug={d['slug']}, count={d['cnt']}"
                )
        self.conn.commit()
        self._run_migrations()

    def _get_schema_version(self) -> int:
        """Get current schema version from PRAGMA user_version."""
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _set_schema_version(self, version: int):
        """Set schema version via PRAGMA user_version."""
        self.conn.execute(f"PRAGMA user_version = {version}")
        self.conn.commit()

    def _run_migrations(self):
        """Run any pending schema migrations."""
        current = self._get_schema_version()
        if current < 1:
            self._migrate_v1()
        if current < 2:
            self._migrate_v2()
        if current < 3:
            self._migrate_v3()
        if current < 4:
            self._migrate_v4()
        if current < 5:
            self._migrate_v5()
        if current < 6:
            self._migrate_v6()
        self._set_schema_version(CURRENT_SCHEMA_VERSION)

    def _migrate_v1(self):
        """V1 migration: Round 2 baseline — adds verification and CAERS columns."""
        for col_sql in [
            "ALTER TABLE products ADD COLUMN verification_state TEXT DEFAULT 'unverified'",
            "ALTER TABLE products ADD COLUMN caers_status TEXT DEFAULT ''",
        ]:
            try:
                self.conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # Column already exists
        self.conn.commit()

    def _migrate_v2(self):
        """V2 migration: Round 3 — freshness tracking, change detection, DSLD quarantine."""
        # Add columns for hash-based freshness tracking
        for col_sql in [
            "ALTER TABLE products ADD COLUMN research_updated_at TEXT",
            "ALTER TABLE products ADD COLUMN research_hash TEXT",
        ]:
            try:
                self.conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Backfill research_updated_at from last_updated for researched products
        self.conn.execute("""
            UPDATE products
            SET research_updated_at = last_updated
            WHERE research_json IS NOT NULL AND research_updated_at IS NULL
        """)

        # Backfill research_hash for existing records
        rows = self.conn.execute(
            "SELECT id, research_json FROM products "
            "WHERE research_json IS NOT NULL AND research_hash IS NULL"
        ).fetchall()
        for row in rows:
            h = hashlib.sha256(row["research_json"].encode()).hexdigest()
            self.conn.execute(
                "UPDATE products SET research_hash = ? WHERE id = ?",
                (h, row["id"])
            )

        # Quarantine known DSLD false matches
        self._quarantine_dsld_false_matches()

        self.conn.commit()

    def _quarantine_dsld_false_matches(self):
        """Clear DSLD data from products with known false DSLD matches.

        Round 3 finding: "Cardio Slim Tea" matched "Cardio Miracle" and
        "Glyco Reset Drops" matched "RnA ReSet Drops" in DSLD due to
        overly permissive single-word matching.
        """
        quarantine_names = ["Cardio Slim Tea", "Glyco Reset Drops"]
        for name in quarantine_names:
            row = self.conn.execute(
                "SELECT id, research_json FROM products WHERE product_name = ?",
                (name,)
            ).fetchone()
            if not row or not row["research_json"]:
                continue

            try:
                data = json.loads(row["research_json"])
            except (json.JSONDecodeError, TypeError):
                continue

            # Check if DSLD data is present
            product = data.get("product", {})
            sf = product.get("supplement_facts", {})
            has_dsld = sf.get("_dsld_id") or sf.get("source") == "dsld_label_record"
            if not has_dsld:
                continue

            # Clear DSLD-specific fields
            for key in ("_dsld_id", "_dsld_match_name", "_dsld_match_brand",
                        "_verification_state"):
                sf.pop(key, None)
            if sf.get("source") == "dsld_label_record":
                sf["source"] = "quarantined_dsld"
                sf["ingredients"] = []  # DSLD was the ingredient source — clear it

            data["product"]["supplement_facts"] = sf
            # Remove cross-reference if present
            data.pop("dsld_cross_reference", None)

            self.conn.execute(
                "UPDATE products SET research_json = ?, verification_state = ? WHERE id = ?",
                (json.dumps(data), "quarantined_dsld", row["id"])
            )

    def _migrate_v3(self):
        """V3 migration: Universal entity model + evidence lake + claims ledger + workflow."""
        migration_sqls = [
            # Offerings table — universal entity record
            """CREATE TABLE IF NOT EXISTS offerings (
                offering_id TEXT PRIMARY KEY,
                offering_type TEXT NOT NULL DEFAULT 'unknown',
                name TEXT NOT NULL,
                brand_name TEXT DEFAULT '',
                organization_name TEXT DEFAULT '',
                url TEXT DEFAULT '',
                category TEXT DEFAULT '',
                market TEXT DEFAULT 'US',
                composition_json TEXT DEFAULT '{}',
                policies_json TEXT DEFAULT '{}',
                metadata_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                legacy_product_key TEXT,
                UNIQUE(legacy_product_key)
            )""",

            # Artifacts table (evidence lake) — immutable source snapshots
            """CREATE TABLE IF NOT EXISTS artifacts (
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
            )""",

            # Claims ledger — atomic fact/claim records with source tracing
            """CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                offering_id TEXT NOT NULL,
                claim_text TEXT NOT NULL,
                claim_type TEXT NOT NULL,
                source_artifact_id TEXT,
                exact_excerpt TEXT DEFAULT '',
                page_location TEXT DEFAULT '',
                captured_at TEXT NOT NULL,
                source_class TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                extraction_method TEXT DEFAULT 'manual',
                effective_market TEXT DEFAULT 'US',
                review_status TEXT DEFAULT 'unreviewed',
                reviewed_by TEXT,
                reviewed_at TEXT,
                conflicts_json TEXT DEFAULT '[]',
                metadata_json TEXT DEFAULT '{}'
            )""",

            # Workflow jobs table — resumable pipeline state
            """CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                offering_id TEXT DEFAULT '',
                url TEXT DEFAULT '',
                product_name TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'created',
                current_stage TEXT DEFAULT '',
                quick_mode INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT DEFAULT '',
                error TEXT DEFAULT '',
                budget_seconds INTEGER DEFAULT 600,
                elapsed_seconds REAL DEFAULT 0.0,
                stage_data_json TEXT DEFAULT '{}',
                stage_status_json TEXT DEFAULT '{}',
                metadata_json TEXT DEFAULT '{}'
            )""",

            # Job stage checkpoints — audit trail for every pipeline stage
            """CREATE TABLE IF NOT EXISTS job_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                result_json TEXT DEFAULT '{}',
                error TEXT DEFAULT ''
            )""",

            # Indexes
            "CREATE INDEX IF NOT EXISTS idx_artifacts_offering ON artifacts(offering_id)",
            "CREATE INDEX IF NOT EXISTS idx_artifacts_source_class ON artifacts(source_class)",
            "CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id)",
            "CREATE INDEX IF NOT EXISTS idx_artifacts_captured ON artifacts(captured_at)",
            "CREATE INDEX IF NOT EXISTS idx_claims_offering ON claims(offering_id)",
            "CREATE INDEX IF NOT EXISTS idx_claims_source ON claims(source_artifact_id)",
            "CREATE INDEX IF NOT EXISTS idx_claims_type ON claims(claim_type)",
            "CREATE INDEX IF NOT EXISTS idx_claims_review ON claims(review_status)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_offering ON jobs(offering_id)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
            "CREATE INDEX IF NOT EXISTS idx_checkpoints_job ON job_checkpoints(job_id)",
        ]

        for sql in migration_sqls:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # Table/index already exists

        # Bridge: link legacy products table to new offerings table
        try:
            self.conn.execute(
                "ALTER TABLE products ADD COLUMN offering_id TEXT DEFAULT NULL"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists

        self.conn.commit()

    def _migrate_v4(self):
        """V4 repair migration: Ensure v3 tables match expected schema.

        Databases already stamped user_version=3 may have an incompatible
        earlier v3 schema (different column names, missing columns, wrong
        types). This migration inspects every v3 table and repairs safely:
        - Missing tables are created
        - Missing columns are added via ALTER TABLE
        - Indexes are added if absent
        No data is deleted or tables dropped — additive repairs only.
        """
        # Expected columns for each v3 table.
        # format: {table: {column_name: "TYPE DEFAULT ..."}}
        expected = {
            "offerings": {
                "offering_id": "TEXT PRIMARY KEY",
                "offering_type": "TEXT NOT NULL DEFAULT 'unknown'",
                "name": "TEXT NOT NULL",
                "brand_name": "TEXT DEFAULT ''",
                "organization_name": "TEXT DEFAULT ''",
                "url": "TEXT DEFAULT ''",
                "category": "TEXT DEFAULT ''",
                "market": "TEXT DEFAULT 'US'",
                "composition_json": "TEXT DEFAULT '{}'",
                "policies_json": "TEXT DEFAULT '{}'",
                "metadata_json": "TEXT DEFAULT '{}'",
                "created_at": "TEXT NOT NULL",
                "updated_at": "TEXT NOT NULL",
                "legacy_product_key": "TEXT",
            },
            "artifacts": {
                "artifact_id": "TEXT PRIMARY KEY",
                "artifact_type": "TEXT NOT NULL",
                "source_url": "TEXT",
                "final_url": "TEXT",
                "source_class": "TEXT NOT NULL",
                "source_relationship": "TEXT NOT NULL DEFAULT 'third_party'",
                "captured_at": "TEXT NOT NULL",
                "content_hash": "TEXT NOT NULL",
                "content_length": "INTEGER DEFAULT 0",
                "tls_verified": "INTEGER DEFAULT 1",
                "status_code": "INTEGER DEFAULT 0",
                "elapsed_ms": "REAL DEFAULT 0.0",
                "error": "TEXT DEFAULT ''",
                "content_path": "TEXT DEFAULT ''",
                "content_inline": "TEXT DEFAULT ''",
                "offering_id": "TEXT",
                "job_id": "TEXT",
                "acquisition_phase": "TEXT DEFAULT ''",
                "notes": "TEXT DEFAULT ''",
            },
            "claims": {
                "claim_id": "TEXT PRIMARY KEY",
                "offering_id": "TEXT NOT NULL",
                "claim_text": "TEXT NOT NULL",
                "claim_type": "TEXT NOT NULL",
                "source_artifact_id": "TEXT",
                "exact_excerpt": "TEXT DEFAULT ''",
                "page_location": "TEXT DEFAULT ''",
                "captured_at": "TEXT NOT NULL",
                "source_class": "TEXT NOT NULL",
                "confidence": "REAL DEFAULT 0.0",
                "extraction_method": "TEXT DEFAULT 'manual'",
                "effective_market": "TEXT DEFAULT 'US'",
                "review_status": "TEXT DEFAULT 'unreviewed'",
                "reviewed_by": "TEXT",
                "reviewed_at": "TEXT",
                "conflicts_json": "TEXT DEFAULT '[]'",
                "metadata_json": "TEXT DEFAULT '{}'",
            },
            "jobs": {
                "job_id": "TEXT PRIMARY KEY",
                "offering_id": "TEXT DEFAULT ''",
                "url": "TEXT DEFAULT ''",
                "product_name": "TEXT DEFAULT ''",
                "status": "TEXT NOT NULL DEFAULT 'created'",
                "current_stage": "TEXT DEFAULT ''",
                "quick_mode": "INTEGER DEFAULT 0",
                "created_at": "TEXT NOT NULL",
                "updated_at": "TEXT NOT NULL",
                "completed_at": "TEXT DEFAULT ''",
                "error": "TEXT DEFAULT ''",
                "budget_seconds": "INTEGER DEFAULT 600",
                "elapsed_seconds": "REAL DEFAULT 0.0",
                "stage_data_json": "TEXT DEFAULT '{}'",
                "stage_status_json": "TEXT DEFAULT '{}'",
                "metadata_json": "TEXT DEFAULT '{}'",
            },
            "job_checkpoints": {
                "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
                "job_id": "TEXT NOT NULL",
                "stage": "TEXT NOT NULL",
                "status": "TEXT NOT NULL",
                "started_at": "TEXT",
                "completed_at": "TEXT",
                "result_json": "TEXT DEFAULT '{}'",
                "error": "TEXT DEFAULT ''",
            },
        }

        # Expected indexes
        expected_indexes = [
            ("idx_artifacts_offering", "artifacts", "offering_id"),
            ("idx_artifacts_source_class", "artifacts", "source_class"),
            ("idx_artifacts_job", "artifacts", "job_id"),
            ("idx_artifacts_captured", "artifacts", "captured_at"),
            ("idx_claims_offering", "claims", "offering_id"),
            ("idx_claims_source", "claims", "source_artifact_id"),
            ("idx_claims_type", "claims", "claim_type"),
            ("idx_claims_review", "claims", "review_status"),
            ("idx_jobs_offering", "jobs", "offering_id"),
            ("idx_jobs_status", "jobs", "status"),
            ("idx_checkpoints_job", "job_checkpoints", "job_id"),
        ]

        repairs = []

        for table, columns in expected.items():
            # Check if table exists
            exists = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            ).fetchone()

            if not exists:
                # Table missing entirely — create it from v3 definition
                repairs.append(f"table:{table}:created")
                self._migrate_v3()  # Re-run v3 to create all missing tables
                break  # v3 creates everything, no need to continue

            # Table exists — check for missing columns
            existing_cols = {
                row[1] for row in
                self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }

            for col_name, col_def in columns.items():
                if col_name not in existing_cols:
                    # Extract just the type and default for ALTER TABLE
                    # PRIMARY KEY and NOT NULL can't be added via ALTER
                    add_def = col_def.replace("PRIMARY KEY", "")
                    add_def = add_def.replace("AUTOINCREMENT", "")
                    add_def = add_def.replace("NOT NULL", "")
                    add_def = add_def.strip()
                    if not add_def:
                        add_def = "TEXT"
                    try:
                        self.conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col_name} {add_def}"
                        )
                        repairs.append(f"column:{table}.{col_name}:added")
                    except sqlite3.OperationalError:
                        pass  # Column already exists (race condition)

        # Ensure all indexes exist
        for idx_name, idx_table, idx_col in expected_indexes:
            try:
                self.conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_table}({idx_col})"
                )
            except sqlite3.OperationalError:
                pass

        # Bridge column on products table
        try:
            self.conn.execute(
                "ALTER TABLE products ADD COLUMN offering_id TEXT DEFAULT NULL"
            )
        except sqlite3.OperationalError:
            pass

        self.conn.commit()

        # Log repairs if any were made
        if repairs:
            import logging
            logging.getLogger(__name__).info(
                "v4 repair migration applied %d fixes: %s",
                len(repairs), ", ".join(repairs)
            )

    def _migrate_v5(self):
        """V5 migration: Recovery audit events table.

        Immutable log of all recovery and manual-entry operations.
        """
        sqls = [
            """CREATE TABLE IF NOT EXISTS recovery_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                offering_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                url TEXT DEFAULT '',
                target_facts TEXT DEFAULT '[]',
                result TEXT DEFAULT '',
                facts_found TEXT DEFAULT '[]',
                facts_missing TEXT DEFAULT '[]',
                artifact_id TEXT DEFAULT '',
                claims_added INTEGER DEFAULT 0,
                error TEXT DEFAULT '',
                reviewer TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_audit_offering ON recovery_audit_events(offering_id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_job ON recovery_audit_events(job_id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_type ON recovery_audit_events(event_type)",
            # Immutability triggers — audit rows must never be modified or deleted
            """CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
               BEFORE UPDATE ON recovery_audit_events
               BEGIN SELECT RAISE(ABORT, 'recovery_audit_events is immutable: updates are prohibited'); END""",
            """CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
               BEFORE DELETE ON recovery_audit_events
               BEGIN SELECT RAISE(ABORT, 'recovery_audit_events is immutable: deletes are prohibited'); END""",
        ]
        for sql in sqls:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # Already exists
        self.conn.commit()

    def _migrate_v6(self):
        """V6 migration: Immutability triggers on recovery_audit_events.

        V5 databases created before triggers were added will not have them.
        This migration ensures all databases receive the triggers.
        """
        sqls = [
            """CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
               BEFORE UPDATE ON recovery_audit_events
               BEGIN SELECT RAISE(ABORT, 'recovery_audit_events is immutable: updates are prohibited'); END""",
            """CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
               BEFORE DELETE ON recovery_audit_events
               BEGIN SELECT RAISE(ABORT, 'recovery_audit_events is immutable: deletes are prohibited'); END""",
        ]
        for sql in sqls:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # Already exists
        self.conn.commit()

    def _execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Thread-safe single write operation."""
        with _db_lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            return cursor

    def _execute_write_batch(self, operations: list):
        """Thread-safe batch write. operations = [(sql, params), ...]"""
        with _db_lock:
            for sql, params in operations:
                self.conn.execute(sql, params)
            self.conn.commit()

    # ──────────────────────────────────────────────────────────────────
    # QUALITY CHECKS — Data integrity gates
    # ──────────────────────────────────────────────────────────────────

    def compute_completeness_score(self, research_data: dict) -> tuple:
        """
        Compute a completeness score (0-100) and flag list for research data.
        Measures data presence and coverage, NOT factual verification.
        A high score means data fields are populated, not that facts are confirmed.

        Returns: (score: int, flags: list[str])
        """
        score = 0
        flags = []
        product = research_data.get("product", {})
        sf = product.get("supplement_facts", {})
        ingredients = sf.get("ingredients", [])
        claims = product.get("claims", [])
        pricing = product.get("pricing", [])
        ingredient_research = research_data.get("ingredient_research", {})
        safety = research_data.get("safety", {})
        compliance = research_data.get("compliance", {})
        reputation = research_data.get("reputation", {})

        # Product identification (max 15)
        if product.get("product_name"):
            score += 5
        else:
            flags.append("MISSING: product_name")
        if product.get("brand_name"):
            score += 5
        else:
            flags.append("MISSING: brand_name")
        if product.get("product_type"):
            score += 3
        if product.get("category"):
            score += 2

        # Ingredients/composition (max 20)
        if ingredients:
            score += 10
            if len(ingredients) >= 3:
                score += 5
            if any(i.get("amount") or i.get("dosage") for i in ingredients):
                score += 5
        else:
            product_type = product.get("product_type", "supplement")
            if product_type in ("supplement", "food", "topical"):
                flags.append("WARNING: No ingredients extracted — supplement/topical should have ingredients")
            elif product_type == "cannabis":
                flags.append("INFO: Cannabis product — COA/cannabinoid profile may not be extractable")

        # Claims (max 10)
        if claims:
            score += 5
            if len(claims) >= 3:
                score += 5
        else:
            flags.append("WARNING: No claims extracted")

        # Pricing (max 10)
        if pricing:
            score += 5
            if len(pricing) >= 2:
                score += 3  # Multiple pricing tiers captured
            if any(p.get("original_price") or p.get("savings") for p in pricing):
                score += 2  # Bundle/savings data
        else:
            flags.append("WARNING: No pricing data extracted")

        # PubMed research (max 15) — weight human studies more than animal/in-vitro
        all_studies = []
        for r in ingredient_research.values():
            all_studies.extend(r.get("studies", []))
        human_studies = [
            s for s in all_studies
            if "human_study" in s.get("relevance_tags", [])
        ]
        total_studies = len(all_studies)
        human_count = len(human_studies)

        if human_count >= 5:
            score += 15  # Strong human evidence
        elif human_count >= 2:
            score += 10  # Some human evidence
        elif total_studies >= 5:
            score += 8   # Volume but no confirmed human studies
        elif total_studies > 0:
            score += 5   # Some evidence
        else:
            if ingredients:
                flags.append("WARNING: No PubMed studies found despite having ingredients")

        # Safety data (max 10) — require actual safety content, not empty dicts
        has_real_safety = False
        if safety and isinstance(safety, dict):
            for ing_key, ing_safety in safety.items():
                if isinstance(ing_safety, dict) and any(
                    v for k, v in ing_safety.items()
                    if v and k not in ("ingredient_name",) and v != "Check required"
                ):
                    has_real_safety = True
                    break
        if has_real_safety:
            score += 5
            has_interactions = any(
                v.get("drug_interactions") for v in safety.values()
                if isinstance(v, dict)
            )
            if has_interactions:
                score += 5
        else:
            if ingredients:
                flags.append("INFO: No safety/interaction data")

        # Compliance (max 10)
        if compliance:
            score += 5
            if compliance.get("risk_level"):
                score += 3
            if compliance.get("accesswire_blocklist_check", {}).get("passes") is not None:
                score += 2

        # Reputation (max 5) — require actual findings, not placeholders
        has_real_reputation = False
        if reputation and isinstance(reputation, dict):
            for k, v in reputation.items():
                if k in ("search_queries_to_run", "Check required"):
                    continue
                if isinstance(v, str) and v in ("Check required", "", "Not checked"):
                    continue
                if isinstance(v, (list, dict)) and not v:
                    continue
                has_real_reputation = True
                break
        if has_real_reputation:
            score += 3
            if reputation.get("bbb_rating") or reputation.get("trustpilot_score"):
                score += 2

        # Refund/shipping policies (max 5)
        if product.get("refund_policy"):
            score += 3
        else:
            flags.append("INFO: No refund policy extracted")
        if product.get("shipping_policy"):
            score += 2

        # Cap at 100
        score = min(score, 100)

        # Classify completeness level (NOT verification — score measures data presence)
        if score >= 80:
            flags.insert(0, "COMPLETENESS: FULL — Data sufficient for production")
        elif score >= 60:
            flags.insert(0, "COMPLETENESS: GOOD — Minor gaps, review before production")
        elif score >= 40:
            flags.insert(0, "COMPLETENESS: PARTIAL — Significant gaps, manual data needed")
        else:
            flags.insert(0, "COMPLETENESS: THIN — Major data gaps, re-research recommended")

        return score, flags

    # ──────────────────────────────────────────────────────────────────
    # PRODUCT CRUD
    # ──────────────────────────────────────────────────────────────────

    def upsert_product(self, product_key: str, research_data: dict) -> int:
        """
        Insert or update a product with research data.
        Returns the product id.

        Detects key collisions: if product_key already exists with a DIFFERENT
        product name, appends a numeric suffix to avoid silent data merging.

        Hash-based freshness: research_updated_at only changes when
        research_json content actually changes (not on every touch).
        """
        product = research_data.get("product", {})
        now = datetime.utcnow().isoformat()
        new_name = product.get("product_name", product_key)

        # Compute completeness score
        quality_score, quality_flags = self.compute_completeness_score(research_data)

        # Count ingredients and studies
        sf = product.get("supplement_facts", {})
        ing_count = len(sf.get("ingredients", []))
        study_count = sum(
            len(r.get("studies", []))
            for r in research_data.get("ingredient_research", {}).values()
        )

        research_json = json.dumps(research_data)
        new_hash = hashlib.sha256(research_json.encode()).hexdigest()

        # Atomic read-check-write under lock
        with _db_lock:
            # Check for existing record
            existing = self.conn.execute(
                "SELECT id, research_version, product_name, research_hash "
                "FROM products WHERE product_key = ?",
                (product_key,)
            ).fetchone()

            # Collision detection: same key, different product name
            if existing and existing["product_name"] and new_name:
                existing_name_key = _slugify(existing["product_name"])
                new_name_key = _slugify(new_name)
                if existing_name_key != new_name_key:
                    import logging
                    logging.warning(
                        f"Product key collision: '{product_key}' exists as "
                        f"'{existing['product_name']}', new product '{new_name}'. "
                        f"Creating distinct key."
                    )
                    suffix = 2
                    while True:
                        candidate = f"{product_key}-{suffix}"
                        check = self.conn.execute(
                            "SELECT id FROM products WHERE product_key = ?",
                            (candidate,)
                        ).fetchone()
                        if not check:
                            product_key = candidate
                            existing = None
                            break
                        suffix += 1

            if existing:
                version = (existing["research_version"] or 0) + 1
                # Only update research_updated_at if content actually changed
                old_hash = existing["research_hash"] if existing else None
                research_changed = (old_hash is None or old_hash != new_hash)

                if research_changed:
                    self.conn.execute("""
                        UPDATE products SET
                            product_name = ?, brand = ?, product_type = ?, category = ?,
                            product_url = ?, risk_level = ?, ingredient_count = ?,
                            study_count = ?, research_json = ?, last_updated = ?,
                            research_updated_at = ?, research_hash = ?,
                            research_version = ?, quality_score = ?, quality_flags = ?
                        WHERE id = ?
                    """, (
                        product.get("product_name", product_key),
                        product.get("brand_name", ""),
                        product.get("product_type", "supplement"),
                        product.get("category", ""),
                        product.get("official_url", ""),
                        research_data.get("compliance", {}).get("risk_level", "Unknown"),
                        ing_count, study_count, research_json, now, now, new_hash,
                        version, quality_score, json.dumps(quality_flags),
                        existing["id"],
                    ))
                else:
                    # Data unchanged — update administrative timestamp only
                    self.conn.execute("""
                        UPDATE products SET
                            last_updated = ?, quality_score = ?, quality_flags = ?
                        WHERE id = ?
                    """, (now, quality_score, json.dumps(quality_flags), existing["id"]))

                self.conn.commit()
                return existing["id"]
            else:
                cursor = self.conn.execute("""
                    INSERT INTO products (
                        product_key, product_name, brand, product_type, category,
                        product_url, risk_level, ingredient_count, study_count,
                        research_json, first_researched, last_updated,
                        research_updated_at, research_hash,
                        research_version, quality_score, quality_flags
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """, (
                    product_key,
                    product.get("product_name", product_key),
                    product.get("brand_name", ""),
                    product.get("product_type", "supplement"),
                    product.get("category", ""),
                    product.get("official_url", ""),
                    research_data.get("compliance", {}).get("risk_level", "Unknown"),
                    ing_count, study_count, research_json, now, now, now, new_hash,
                    quality_score, json.dumps(quality_flags),
                ))
                self.conn.commit()
                return cursor.lastrowid

    def get_product(self, product_key: str) -> Optional[dict]:
        """Get full product record with parsed research JSON."""
        row = self.conn.execute(
            "SELECT * FROM products WHERE product_key = ?", (product_key,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("research_json"):
            d["research_data"] = json.loads(d["research_json"])
        if d.get("quality_flags"):
            d["quality_flags_list"] = json.loads(d["quality_flags"])
        return d

    def list_products(self, search: str = None, category: str = None,
                      product_type: str = None) -> list:
        """List products with optional filters. Returns lightweight summaries."""
        query = """
            SELECT p.id, p.product_key, p.product_name, p.brand,
                   p.product_type, p.category, p.risk_level,
                   p.ingredient_count, p.study_count,
                   p.last_updated, p.research_version, p.quality_score,
                   COUNT(pub.id) as publication_count
            FROM products p
            LEFT JOIN publications pub ON pub.product_id = p.id
        """
        conditions = []
        params = []

        if search:
            conditions.append(
                "(p.product_name LIKE ? OR p.brand LIKE ? OR p.product_key LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like])
        if category:
            conditions.append("p.category = ?")
            params.append(category)
        if product_type:
            conditions.append("p.product_type = ?")
            params.append(product_type)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " GROUP BY p.id ORDER BY p.last_updated DESC"

        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_all_product_summaries(self) -> list:
        """Lightweight list for sidebar display."""
        rows = self.conn.execute("""
            SELECT p.id, p.product_key, p.product_name, p.brand,
                   p.product_type, p.category, p.last_updated,
                   p.quality_score,
                   COUNT(pub.id) as publication_count
            FROM products p
            LEFT JOIN publications pub ON pub.product_id = p.id
            GROUP BY p.id
            ORDER BY p.last_updated DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def delete_product(self, product_key: str):
        """Delete a product and all related records."""
        row = self.conn.execute(
            "SELECT id FROM products WHERE product_key = ?", (product_key,)
        ).fetchone()
        if row:
            pid = row["id"]
            self._execute_write_batch([
                ("DELETE FROM generation_log WHERE product_id = ?", (pid,)),
                ("DELETE FROM publications WHERE product_id = ?", (pid,)),
                ("DELETE FROM products WHERE id = ?", (pid,)),
            ])

    # ──────────────────────────────────────────────────────────────────
    # PUBLICATIONS
    # ──────────────────────────────────────────────────────────────────

    def add_publication(self, product_key: str, site_key: str, slug: str,
                        site_name: str = "", post_url: str = "",
                        slug_angle: str = "", content_type: str = "L6_review",
                        platform: str = "", published_date: str = "",
                        wp_post_id: int = None) -> Optional[int]:
        """Record a publication. Returns publication id or None if product not found."""
        row = self.conn.execute(
            "SELECT id FROM products WHERE product_key = ?", (product_key,)
        ).fetchone()
        if not row:
            return None

        if not published_date:
            published_date = datetime.utcnow().strftime("%Y-%m-%d")

        try:
            # Check for cross-product slug collision (same slug on same site
            # but different product)
            existing_pub = self.conn.execute(
                "SELECT product_id FROM publications WHERE site_key = ? AND slug = ?",
                (site_key, slug)
            ).fetchone()
            if existing_pub and existing_pub["product_id"] != row["id"]:
                import logging
                logging.warning(
                    f"Cross-product slug collision: site={site_key}, slug={slug}, "
                    f"existing_product_id={existing_pub['product_id']}, "
                    f"new_product_id={row['id']}. Rejecting insert."
                )
                return None

            cursor = self._execute_write("""
                INSERT INTO publications (
                    product_id, site_key, site_name, post_url, slug,
                    slug_angle, content_type, platform, published_date, wp_post_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, site_key, slug) DO UPDATE SET
                    site_name = excluded.site_name,
                    post_url = excluded.post_url,
                    slug_angle = excluded.slug_angle,
                    content_type = excluded.content_type,
                    platform = excluded.platform,
                    published_date = excluded.published_date,
                    wp_post_id = excluded.wp_post_id
            """, (
                row["id"], site_key, site_name, post_url, slug,
                slug_angle, content_type, platform, published_date, wp_post_id,
            ))
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    def get_publications(self, product_key: str) -> list:
        """Get all publications for a product."""
        row = self.conn.execute(
            "SELECT id FROM products WHERE product_key = ?", (product_key,)
        ).fetchone()
        if not row:
            return []
        rows = self.conn.execute(
            "SELECT * FROM publications WHERE product_id = ? ORDER BY published_date DESC",
            (row["id"],)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_coverage_matrix(self, product_key: str) -> dict:
        """
        Get which sites have this product and which don't.
        Returns: {site_key: {"status": "published"|"not_published", "slug": ..., "date": ...}}
        """
        pubs = self.get_publications(product_key)
        return {
            p["site_key"]: {
                "status": "published",
                "slug": p["slug"],
                "angle": p["slug_angle"],
                "date": p["published_date"],
                "url": p["post_url"],
                "content_type": p["content_type"],
            }
            for p in pubs
        }

    # ──────────────────────────────────────────────────────────────────
    # PUBLISHING COMPLIANCE CHECKS
    # ──────────────────────────────────────────────────────────────────

    def check_publishing_compliance(self, product_key: str, target_site: str,
                                     target_slug: str) -> list:
        """
        Run compliance checks before recording a publication.
        Returns list of warnings/errors.
        """
        warnings = []
        pubs = self.get_publications(product_key)

        # Check 1: Duplicate slug on same site
        for p in pubs:
            if p["site_key"] == target_site and p["slug"] == target_slug:
                warnings.append(
                    f"ERROR: Slug '{target_slug}' already exists on {target_site}"
                )

        # Check 2: Same slug used on another site (network fingerprint risk)
        for p in pubs:
            if p["site_key"] != target_site and p["slug"] == target_slug:
                warnings.append(
                    f"WARNING: Identical slug '{target_slug}' already used on "
                    f"{p['site_key']} — this creates a network fingerprint. "
                    f"Use slug_diversifier for unique per-site slugs."
                )

        # Check 3: Too many same-day publications (anti-pattern)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        same_day = [p for p in pubs if p["published_date"] == today]
        if len(same_day) >= 3:
            sites = [p["site_key"] for p in same_day]
            warnings.append(
                f"WARNING: Product already published to {len(same_day)} sites today "
                f"({', '.join(sites)}). Stagger across 2-3 days minimum to avoid "
                f"detectable cross-site patterns."
            )

        # Check 4: Quality score too low
        product = self.get_product(product_key)
        if product and product.get("quality_score", 0) < 40:
            warnings.append(
                f"WARNING: Quality score is {product['quality_score']}/100 — "
                f"research data is thin. Consider re-researching before publishing."
            )

        return warnings

    def check_data_freshness(self, product_key: str, max_days: int = 30) -> dict:
        """
        Check if research data is stale based on when content actually changed.
        Uses research_updated_at (content-change timestamp), NOT last_updated
        (administrative timestamp that refreshes on every touch).

        Returns: {"is_fresh": bool, "days_old": int, "message": str}
        """
        product = self.get_product(product_key)
        if not product:
            return {"is_fresh": False, "days_old": -1, "message": "Product not found"}

        # Prefer research_updated_at (only changes when data changes)
        # Fall back to last_updated for pre-v2 records
        timestamp = product.get("research_updated_at") or product.get("last_updated", "")
        if not timestamp:
            return {"is_fresh": False, "days_old": -1, "message": "No research timestamp"}

        # Stubs (no research_json) are never fresh
        if not product.get("research_json"):
            return {"is_fresh": False, "days_old": -1, "message": "No research data (stub)"}

        try:
            updated_dt = datetime.fromisoformat(timestamp)
            age = (datetime.utcnow() - updated_dt).days
            if age <= max_days:
                return {
                    "is_fresh": True,
                    "days_old": age,
                    "message": f"Research is {age} days old — current",
                }
            else:
                return {
                    "is_fresh": False,
                    "days_old": age,
                    "message": f"Research is {age} days old — consider refreshing "
                               f"(threshold: {max_days} days)",
                }
        except (ValueError, TypeError):
            return {"is_fresh": False, "days_old": -1, "message": "Invalid timestamp"}

    # ──────────────────────────────────────────────────────────────────
    # GENERATION LOG
    # ──────────────────────────────────────────────────────────────────

    def log_generation(self, product_key: str, platform: str,
                       content_type: str = "", target_site: str = "",
                       prompt_text: str = "") -> Optional[int]:
        """Log a prompt generation for audit trail."""
        row = self.conn.execute(
            "SELECT id FROM products WHERE product_key = ?", (product_key,)
        ).fetchone()
        if not row:
            return None

        prompt_hash = hashlib.md5(prompt_text.encode()).hexdigest() if prompt_text else ""
        now = datetime.utcnow().isoformat()

        cursor = self._execute_write("""
            INSERT INTO generation_log (
                product_id, platform, content_type, target_site,
                generated_at, prompt_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (row["id"], platform, content_type, target_site, now, prompt_hash))
        return cursor.lastrowid

    def get_generation_history(self, product_key: str) -> list:
        """Get generation log for a product."""
        row = self.conn.execute(
            "SELECT id FROM products WHERE product_key = ?", (product_key,)
        ).fetchone()
        if not row:
            return []
        rows = self.conn.execute(
            "SELECT * FROM generation_log WHERE product_id = ? ORDER BY generated_at DESC",
            (row["id"],)
        ).fetchall()
        return [dict(r) for r in rows]

    # ──────────────────────────────────────────────────────────────────
    # DATA IMPORT
    # ──────────────────────────────────────────────────────────────────

    def import_from_json(self, json_path: str) -> str:
        """
        Import a single output/*_source.json file.
        Returns product_key.
        """
        with open(json_path) as f:
            data = json.load(f)

        # Derive product_key from filename
        fname = os.path.basename(json_path)
        product_key = fname.replace("_source.json", "")

        self.upsert_product(product_key, data)
        return product_key

    def import_all_json_files(self, output_dir: str) -> list:
        """
        Import all *_source.json files from the output directory.
        Returns list of imported product_keys.
        """
        imported = []
        if not os.path.isdir(output_dir):
            return imported

        for fname in os.listdir(output_dir):
            if fname.endswith("_source.json") and not fname.startswith("_"):
                path = os.path.join(output_dir, fname)
                try:
                    key = self.import_from_json(path)
                    imported.append(key)
                except Exception as e:
                    print(f"  Error importing {fname}: {e}")

        return imported

    def import_from_master_list(self, master_json_path: str) -> dict:
        """
        Import cross-site publication data from master_product_list.json.
        Creates product stubs (minimal data) + publication records.
        Returns: {"products_imported": int, "publications_imported": int}
        """
        with open(master_json_path) as f:
            master = json.load(f)

        products_imported = 0
        pubs_imported = 0

        for product_name, data in master.items():
            product_key = _slugify(product_name)

            # Check if we already have full research data for this product
            existing = self.get_product(product_key)

            if not existing:
                # Create a stub product record (no research data, just name)
                now = datetime.utcnow().isoformat()
                self._execute_write("""
                    INSERT OR IGNORE INTO products (
                        product_key, product_name, brand, product_type, category,
                        first_researched, last_updated, quality_score, quality_flags
                    ) VALUES (?, ?, ?, 'unknown', '', ?, ?, 0, '["COMPLETENESS: STUB — No research data, imported from master list"]')
                """, (product_key, product_name, "", now, now))
                products_imported += 1

            # Get the product id
            row = self.conn.execute(
                "SELECT id FROM products WHERE product_key = ?", (product_key,)
            ).fetchone()
            if not row:
                continue
            product_id = row["id"]

            # Import publication records
            articles = data.get("articles", [])
            for article in articles:
                site_name = article.get("site", "")
                site_key = _slugify(site_name) if site_name else ""
                slug = article.get("slug", "")
                if not site_key or not slug:
                    continue

                try:
                    self._execute_write("""
                        INSERT OR IGNORE INTO publications (
                            product_id, site_key, site_name, slug,
                            content_type, published_date, wp_post_id
                        ) VALUES (?, ?, ?, ?, 'L6_review', ?, ?)
                    """, (
                        product_id, site_key, site_name, slug,
                        article.get("date", ""),
                        article.get("post_id"),
                    ))
                    pubs_imported += 1
                except sqlite3.IntegrityError:
                    pass
        return {"products_imported": products_imported, "publications_imported": pubs_imported}

    # ──────────────────────────────────────────────────────────────────
    # STATS
    # ──────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Get overall database statistics."""
        products = self.conn.execute("SELECT COUNT(*) as c FROM products").fetchone()["c"]
        researched = self.conn.execute(
            "SELECT COUNT(*) as c FROM products WHERE research_json IS NOT NULL"
        ).fetchone()["c"]
        pubs = self.conn.execute("SELECT COUNT(*) as c FROM publications").fetchone()["c"]
        gens = self.conn.execute("SELECT COUNT(*) as c FROM generation_log").fetchone()["c"]
        avg_quality = self.conn.execute(
            "SELECT AVG(quality_score) as avg FROM products WHERE research_json IS NOT NULL"
        ).fetchone()["avg"] or 0

        return {
            "total_products": products,
            "researched_products": researched,
            "stub_products": products - researched,
            "total_publications": pubs,
            "total_generations": gens,
            "avg_quality_score": round(avg_quality, 1),
        }

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
