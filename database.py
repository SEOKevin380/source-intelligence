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
CURRENT_SCHEMA_VERSION = 1


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
        self._set_schema_version(CURRENT_SCHEMA_VERSION)

    def _migrate_v1(self):
        """V1 migration: Round 2 baseline — adds verification and CAERS columns."""
        # These are safe to run even if columns already exist (IF NOT EXISTS
        # isn't supported for ALTER TABLE, so we catch the error)
        for col_sql in [
            "ALTER TABLE products ADD COLUMN verification_state TEXT DEFAULT 'unverified'",
            "ALTER TABLE products ADD COLUMN caers_status TEXT DEFAULT ''",
        ]:
            try:
                self.conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # Column already exists
        self.conn.commit()

    def _execute_write(self, sql: str, params: tuple = ()):
        """Thread-safe write operation."""
        with _db_lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    # ──────────────────────────────────────────────────────────────────
    # QUALITY CHECKS — Data integrity gates
    # ──────────────────────────────────────────────────────────────────

    def compute_quality_score(self, research_data: dict) -> tuple:
        """
        Compute a quality score (0-100) and flag list for research data.
        This is the checks-and-balances system — ensures data meets
        minimum thresholds before being considered "verified."

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

        # PubMed research (max 15)
        total_studies = sum(len(r.get("studies", [])) for r in ingredient_research.values())
        if total_studies > 0:
            score += 5
            if total_studies >= 5:
                score += 5
            if total_studies >= 10:
                score += 5
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

        # Classify quality level
        if score >= 80:
            flags.insert(0, "QUALITY: VERIFIED — Ready for production")
        elif score >= 60:
            flags.insert(0, "QUALITY: GOOD — Minor gaps, review before production")
        elif score >= 40:
            flags.insert(0, "QUALITY: PARTIAL — Significant gaps, manual data needed")
        else:
            flags.insert(0, "QUALITY: THIN — Major data gaps, re-research recommended")

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
        """
        product = research_data.get("product", {})
        now = datetime.utcnow().isoformat()
        new_name = product.get("product_name", product_key)

        # Compute quality score
        quality_score, quality_flags = self.compute_quality_score(research_data)

        # Count ingredients and studies
        sf = product.get("supplement_facts", {})
        ing_count = len(sf.get("ingredients", []))
        study_count = sum(
            len(r.get("studies", []))
            for r in research_data.get("ingredient_research", {}).values()
        )

        # Check for existing record
        existing = self.conn.execute(
            "SELECT id, research_version, product_name FROM products WHERE product_key = ?",
            (product_key,)
        ).fetchone()

        # Collision detection: same key, different product name
        if existing and existing["product_name"] and new_name:
            existing_name_key = _slugify(existing["product_name"])
            new_name_key = _slugify(new_name)
            if existing_name_key != new_name_key:
                # Collision! Different products mapped to same key.
                # Append numeric suffix to create a distinct key.
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
                        existing = None  # Force INSERT path
                        break
                    suffix += 1

        research_json = json.dumps(research_data)

        if existing:
            version = (existing["research_version"] or 0) + 1
            self.conn.execute("""
                UPDATE products SET
                    product_name = ?, brand = ?, product_type = ?, category = ?,
                    product_url = ?, risk_level = ?, ingredient_count = ?,
                    study_count = ?, research_json = ?, last_updated = ?,
                    research_version = ?, quality_score = ?, quality_flags = ?
                WHERE id = ?
            """, (
                product.get("product_name", product_key),
                product.get("brand_name", ""),
                product.get("product_type", "supplement"),
                product.get("category", ""),
                product.get("official_url", ""),
                research_data.get("compliance", {}).get("risk_level", "Unknown"),
                ing_count, study_count, research_json, now, version,
                quality_score, json.dumps(quality_flags),
                existing["id"],
            ))
            self.conn.commit()
            return existing["id"]
        else:
            cursor = self.conn.execute("""
                INSERT INTO products (
                    product_key, product_name, brand, product_type, category,
                    product_url, risk_level, ingredient_count, study_count,
                    research_json, first_researched, last_updated,
                    research_version, quality_score, quality_flags
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (
                product_key,
                product.get("product_name", product_key),
                product.get("brand_name", ""),
                product.get("product_type", "supplement"),
                product.get("category", ""),
                product.get("official_url", ""),
                research_data.get("compliance", {}).get("risk_level", "Unknown"),
                ing_count, study_count, research_json, now, now,
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
            self.conn.execute("DELETE FROM generation_log WHERE product_id = ?", (pid,))
            self.conn.execute("DELETE FROM publications WHERE product_id = ?", (pid,))
            self.conn.execute("DELETE FROM products WHERE id = ?", (pid,))
            self.conn.commit()

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

            cursor = self.conn.execute("""
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
            self.conn.commit()
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
        Check if research data is stale.
        Returns: {"is_fresh": bool, "days_old": int, "message": str}
        """
        product = self.get_product(product_key)
        if not product:
            return {"is_fresh": False, "days_old": -1, "message": "Product not found"}

        last_updated = product.get("last_updated", "")
        if not last_updated:
            return {"is_fresh": False, "days_old": -1, "message": "No update timestamp"}

        try:
            updated_dt = datetime.fromisoformat(last_updated)
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

        cursor = self.conn.execute("""
            INSERT INTO generation_log (
                product_id, platform, content_type, target_site,
                generated_at, prompt_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (row["id"], platform, content_type, target_site, now, prompt_hash))
        self.conn.commit()
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
                self.conn.execute("""
                    INSERT OR IGNORE INTO products (
                        product_key, product_name, brand, product_type, category,
                        first_researched, last_updated, quality_score, quality_flags
                    ) VALUES (?, ?, ?, 'unknown', '', ?, ?, 0, '["QUALITY: STUB — No research data, imported from master list"]')
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
                    self.conn.execute("""
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

        self.conn.commit()
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
