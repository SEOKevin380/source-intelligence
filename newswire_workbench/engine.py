"""Durable, hash-bound Claude/OpenAI editorial workflow engine."""

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from .prompts import (
    compliance_prompt,
    detect_vertical,
    generation_prompt,
    revision_prompt,
    seo_prompt,
)
from .learning import (
    PROMPT_VERSION, deterministic_findings, issue_fingerprint,
    learned_guidance, partition_findings,
)
from .formatting import (
    ensure_article_html,
    ensure_affiliate_links,
    normalize_master_html,
    repair_publication_gates,
    repair_source_grounding,
)
from .routing import estimated_cost, route_for
from .audit import MECHANICAL_GATES, audit_article
from .publication_profiles import publication_profile
from .execution_budget import (
    PURPOSE_CALL_LIMITS,
    REQUIRED_CALL_PATH,
    execution_budget,
)


WORKBENCH_SOURCE_CONTEXT_VERSION = (
    "serp-differentiation-depth-v32-artifact-integrity"
)
WORKBENCH_RUNTIME_REVISION = "artifact-integrity-20260723-r2"

STAGES = (
    "source_ready",
    "drafted",
    "compliance_reviewed",
    "revised",
    "signed_off",
    "seo_optimized",
    "seo_repair_needed",
    "seo_repaired",
    "post_seo_signed_off",
    "package_ready",
    "admin_review",
)
PAID_CALL_STAGES = frozenset({
    "source_ready",
    "drafted",
    "compliance_reviewed",
    "revised",
    "seo_optimized",
    "seo_repair_needed",
    "seo_repaired",
})


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fact_source_hash(source_text):
    """Stable current-product identity across rebuild UUIDs and workflow versions."""
    marker = "═══ SEALED CURRENT-PRODUCT SOURCE PACK — FACTS ONLY ═══"
    if marker in str(source_text or ""):
        try:
            pack = json.loads(str(source_text).split(marker, 1)[1].strip())
            sealed = str(
                (pack.get("source_pack_contract") or {}).get("sha256") or ""
            )
            if re.fullmatch(r"[a-f0-9]{64}", sealed, re.I):
                return sealed.lower()
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    stable = re.sub(
        r"(?m)^EXPLICIT REBUILD RUN:\s*\S+\s*$", "", str(source_text or "")
    )
    return _hash(stable.strip())


def _source_policy_hash(source_text):
    match = re.search(
        r"(?m)^Snapshot hash:\s*([a-f0-9]{64})\s*$",
        str(source_text or ""),
        re.I,
    )
    return match.group(1).lower() if match else ""


def _source_affiliate_link(source_text):
    """Read the affiliate destination from legacy text or a sealed JSON pack."""
    source_text = str(source_text or "")
    legacy = re.search(
        r"(?im)^AFFILIATE LINK:\s*(https?://\S+)", source_text
    )
    if legacy:
        return legacy.group(1).rstrip(".,;)")
    sealed = re.search(
        r'"affiliate_link"\s*:\s*"(https?://[^"]+)"',
        source_text,
        re.I,
    )
    if sealed:
        return sealed.group(1).replace("\\/", "/")
    return ""


class WorkbenchEngine:
    def __init__(self, root=None):
        if root is None:
            from config import NEWSWIRE_WORKBENCH_PATH
            root = NEWSWIRE_WORKBENCH_PATH
        self.root = Path(root).expanduser()
        self.projects_dir = self.root / "projects"
        self.exports_dir = self.root / "exports"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "workbench.db"
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            # Two Streamlit sessions can open the same new database at once.
            # The connection enabling WAL briefly holds an exclusive schema
            # lock; the other connection is still valid and will observe WAL
            # after the first initializer commits.
            if "locked" not in str(exc).lower():
                conn.close()
                raise
        return conn

    @staticmethod
    def _uses_locked_call_path(project):
        # Safety policy is immutable across deployments. A version bump may
        # select a newer project, but it must never reclassify an older sealed
        # project as legacy and reopen rescue/war-room paid routes.
        return (
            "═══ SEALED CURRENT-PRODUCT SOURCE PACK — FACTS ONLY ═══"
            in project["source_text"]
        )

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    release_title TEXT DEFAULT '',
                    platform TEXT NOT NULL,
                    vertical TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    fact_source_hash TEXT DEFAULT '',
                    article_text TEXT DEFAULT '',
                    article_hash TEXT DEFAULT '',
                    last_report TEXT DEFAULT '{}',
                    revision_round INTEGER DEFAULT 0,
                    run_token TEXT DEFAULT '',
                    run_started_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    article_hash TEXT DEFAULT '',
                    payload TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS issue_observations (
                    event_id INTEGER NOT NULL,
                    project_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    vertical TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    category TEXT NOT NULL,
                    issue TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS adjudications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    source_article_hash TEXT NOT NULL,
                    result_article_hash TEXT DEFAULT '',
                    applied_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    payload TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wordpress_drafts (
                    project_id TEXT NOT NULL,
                    site_url TEXT NOT NULL,
                    post_id INTEGER NOT NULL,
                    article_hash TEXT NOT NULL,
                    remote_content_hash TEXT DEFAULT '',
                    post_type TEXT DEFAULT '',
                    remote_status TEXT DEFAULT '',
                    edit_url TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, site_url)
                );
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    estimated_cost REAL DEFAULT 0,
                    status TEXT NOT NULL,
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS trg_events_no_update
                BEFORE UPDATE ON events BEGIN
                  SELECT RAISE(ABORT, 'workbench events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
                BEFORE DELETE ON events BEGIN
                  SELECT RAISE(ABORT, 'workbench events are immutable');
                END;
            """)
            def add_column_if_missing(table, name, declaration):
                """Apply an additive migration safely across concurrent app sessions."""
                columns = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                if name in columns:
                    return False
                try:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )
                    return True
                except sqlite3.OperationalError as exc:
                    # Another Streamlit session may have completed the same
                    # additive migration after our PRAGMA snapshot.
                    if "duplicate column name" not in str(exc).lower():
                        raise
                    return False

            columns = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
            if "run_token" not in columns:
                add_column_if_missing(
                    "projects", "run_token", "TEXT DEFAULT ''"
                )
            if "run_started_at" not in columns:
                add_column_if_missing(
                    "projects", "run_started_at", "TEXT DEFAULT ''"
                )
            if "release_title" not in columns:
                add_column_if_missing(
                    "projects", "release_title", "TEXT DEFAULT ''"
                )
                conn.execute("UPDATE projects SET release_title=title WHERE release_title='' OR release_title IS NULL")
            if "fact_source_hash" not in columns:
                add_column_if_missing(
                    "projects", "fact_source_hash", "TEXT DEFAULT ''"
                )
            rows = conn.execute(
                "SELECT id,source_text FROM projects "
                "WHERE fact_source_hash='' OR fact_source_hash IS NULL"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE projects SET fact_source_hash=? WHERE id=?",
                    (_fact_source_hash(row["source_text"]), row["id"]),
                )
            wordpress_columns = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(wordpress_drafts)"
                )
            }
            for name in ("remote_content_hash", "post_type", "remote_status"):
                if name not in wordpress_columns:
                    add_column_if_missing(
                        "wordpress_drafts", name, "TEXT DEFAULT ''"
                    )
            llm_columns = {
                r[1] for r in conn.execute("PRAGMA table_info(llm_calls)")
            }
            for name, declaration in (
                ("lifecycle", "TEXT DEFAULT 'applied'"),
                ("raw_output", "TEXT DEFAULT ''"),
                ("output_hash", "TEXT DEFAULT ''"),
            ):
                if name not in llm_columns:
                    add_column_if_missing("llm_calls", name, declaration)
        self._backfill_issue_memory()

    def create_project(self, title, platform, source_text, vertical="auto"):
        source_text = source_text.strip()
        if not title.strip() or not source_text:
            raise ValueError("Project name and source record are required")
        if vertical == "auto":
            vertical = detect_vertical(source_text)
        if "═══ GOVERNED POLICY SNAPSHOT ═══" not in source_text:
            from policy_intelligence import format_policy_context
            source_text += "\n\n" + format_policy_context(vertical)
        pid = uuid.uuid4().hex[:12]
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO projects
                (id,title,release_title,platform,vertical,stage,source_text,source_hash,
                 fact_source_hash,article_text,article_hash,last_report,revision_round,
                 run_token,run_started_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, title.strip(), title.strip(), platform, vertical, "source_ready",
                 source_text, _hash(source_text), _fact_source_hash(source_text),
                 "", "", "{}", 0,
                 "", "", now, now),
            )
        self._event(pid, "project_created", "source_ready", "", {
            "source_hash": _hash(source_text), "vertical": vertical,
        })
        self._write(pid, "00-source-record.txt", source_text)
        return pid

    def create_project_from_pack(
        self, pack, platform, vertical="auto", force_new=False
    ):
        """Create or reuse a workbench job from a sealed Source Intelligence pack."""
        from exemplar_corpus import (
            build_approval_playbook,
            build_generation_blueprint,
            format_approval_playbook,
            format_exemplar_guidance,
            infer_niche,
            retrieve_exemplars,
        )
        from source_pack_contract import validate_source_pack
        from policy_intelligence import format_policy_context
        validate_source_pack(pack, allow_limited=True)
        product = pack.get("product") or {}
        manifest = pack.get("intake_manifest") or {}
        title = str(product.get("product_name") or "Untitled source project").strip()
        pack_text = json.dumps(pack, sort_keys=True, ensure_ascii=False, default=str)
        resolved_vertical = (
            detect_vertical(pack_text) if vertical == "auto" else vertical
        )
        exemplars = retrieve_exemplars(
            product_name=title,
            platform=platform,
            vertical=resolved_vertical,
            source_url=str(product.get("official_url") or ""),
            previous_releases=str(
                manifest.get("previous_releases") or "FIRST RELEASE"
            ),
        )
        exemplar_guidance = format_exemplar_guidance(exemplars)
        approval_playbook = build_approval_playbook(
            exemplars,
            platform,
            infer_niche(
                title,
                str(product.get("category") or ""),
                str(product.get("product_type") or ""),
            ),
        )
        generation_blueprint = build_generation_blueprint(pack, exemplars)
        source_text = "\n\n".join(part for part in (
            f"AUTOMATION CONTEXT VERSION: {WORKBENCH_SOURCE_CONTEXT_VERSION}",
            (
                f"EXPLICIT REBUILD RUN: {uuid.uuid4().hex}"
                if force_new else ""
            ),
            exemplar_guidance,
            format_approval_playbook(approval_playbook),
            format_policy_context(resolved_vertical),
            generation_blueprint,
            "═══ SEALED CURRENT-PRODUCT SOURCE PACK — FACTS ONLY ═══",
            pack_text,
        ) if part)
        source_hash = _hash(source_text)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM projects WHERE source_hash=? AND platform=? ORDER BY created_at DESC LIMIT 1",
                (source_hash, platform),
            ).fetchone()
        if existing:
            existing_project = self.get(existing["id"])
            blockers = []
            if existing_project["stage"] == "package_ready":
                findings = deterministic_findings(
                    existing_project.get("article_text") or "",
                    existing_project["platform"],
                    existing_project["vertical"],
                )
                blockers, _ = partition_findings(findings)
            approval_hash_mismatch = (
                existing_project["stage"] == "package_ready"
                and existing_project.get("last_report", {}).get(
                    "reviewed_article_hash"
                ) != existing_project.get("article_hash")
            )
            if (
                not force_new
                and (
                    existing_project["stage"] == "admin_review"
                    or blockers
                    or approval_hash_mismatch
                )
            ):
                return self.create_project_from_pack(
                    pack, platform, vertical=resolved_vertical, force_new=True
                )
            return existing["id"]
        # Claim the one active project for this exact pack/platform/workflow
        # inside a write transaction. Multiple Streamlit tabs must converge on
        # the same run instead of creating competing "authoritative" projects.
        fact_source_hash = str(
            (pack.get("source_pack_contract") or {}).get("sha256") or ""
        ).strip()
        pid = ""
        created = False
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                """SELECT id FROM projects
                WHERE fact_source_hash=? AND platform=?
                AND source_text LIKE ?
                AND stage IN (
                    'source_ready','drafted','compliance_reviewed','revised',
                    'signed_off','seo_optimized','seo_repair_needed',
                    'seo_repaired','post_seo_signed_off'
                )
                ORDER BY created_at DESC, updated_at DESC, rowid DESC
                LIMIT 1""",
                (
                    fact_source_hash,
                    platform,
                    "%AUTOMATION CONTEXT VERSION: "
                    + WORKBENCH_SOURCE_CONTEXT_VERSION + "%",
                ),
            ).fetchone()
            if active:
                pid = active["id"]
            else:
                pid = uuid.uuid4().hex[:12]
                conn.execute(
                    """INSERT INTO projects
                    (id,title,release_title,platform,vertical,stage,source_text,
                     source_hash,fact_source_hash,article_text,article_hash,
                     last_report,revision_round,run_token,run_started_at,
                     created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        pid, title, title, platform, resolved_vertical,
                        "source_ready", source_text, source_hash,
                        fact_source_hash, "", "", "{}", 0, "", "", now, now,
                    ),
                )
                created = True
        if not created:
            return pid
        self._event(pid, "project_created", "source_ready", "", {
            "source_hash": source_hash, "vertical": resolved_vertical,
        })
        self._write(pid, "00-source-record.txt", source_text)
        self._event(pid, "sealed_source_pack_imported", "source_ready", "", {
            "contract": pack["source_pack_contract"],
            "automation_context_version": WORKBENCH_SOURCE_CONTEXT_VERSION,
            "approved_exemplar_count": len(exemplars),
        })
        return pid

    def article_diagnostics(self, project_id):
        """Return user-visible proof that the packaged deliverable is usable."""
        p = self.get(project_id)
        article = p.get("article_text") or ""
        plain = re.sub(r"<[^>]+>", " ", article)
        findings = deterministic_findings(
            article, p["platform"], p["vertical"]
        )
        blockers, recommendations = partition_findings(findings)
        version_match = re.search(
            r"(?m)^AUTOMATION CONTEXT VERSION:\s*(\S+)", p["source_text"]
        )
        return {
            "workflow_version": (
                version_match.group(1) if version_match else "legacy"
            ),
            "word_count": len(re.findall(r"\b[\w’'-]+\b", plain)),
            "has_code_fence": bool(re.search(r"```", article)),
            "has_article_html": bool(re.search(
                r"<(?:p|h[1-6]|ul|ol|li|div|blockquote)\b", article, re.I
            )),
            "blocker_ids": [item["id"] for item in blockers],
            "recommendation_ids": [item["id"] for item in recommendations],
        }

    def offline_preflight(self, project_id):
        """Audit the exact stored artifact without making a paid model call."""
        p = self.get(project_id)
        result = audit_article(
            p.get("article_text") or "",
            p["platform"],
            p["vertical"],
            _source_affiliate_link(p["source_text"]),
        )
        last_report = p.get("last_report") or {}
        exact_semantic_approval = bool(
            last_report.get("verdict") == "approved"
            and last_report.get("reviewed_article_hash") == p["article_hash"]
            and p["stage"] in {
                "signed_off", "post_seo_signed_off", "package_ready"
            }
        )
        purpose = "final_signoff"
        route = route_for(purpose, p["vertical"])
        route_limit = self._purpose_call_limit(p, purpose, route)
        used = self._billable_call_count(project_id, purpose)
        total_used = int(self.usage_summary(project_id)["calls"])
        budget = execution_budget()
        project_call_maximum = budget["calls"]
        project_remaining = max(project_call_maximum - total_used, 0)
        route_remaining = max(route_limit - used, 0)
        reviewer_capacity = {
            purpose: {
                "used": used,
                "maximum": route_limit,
                "remaining": min(route_remaining, project_remaining),
            },
            "project": {
                "used": total_used,
                "maximum": project_call_maximum,
                "remaining": project_remaining,
            },
        }
        unresolved = last_report.get("mandatory_edits") or []
        result["semantic_review"] = {
            "passed": exact_semantic_approval,
            "stage": p["stage"],
            "last_verdict": last_report.get("verdict", "not_run"),
            "reviewed_article_hash": last_report.get(
                "reviewed_article_hash", ""
            ),
            "current_article_hash": p["article_hash"],
            "exact_hash_match": (
                last_report.get("reviewed_article_hash") == p["article_hash"]
            ),
            "unresolved_edits": unresolved,
            "reviewer_capacity": reviewer_capacity,
            "remaining_calls": min(route_remaining, project_remaining),
        }
        result["execution_budget"] = budget
        result["ready_for_packaging"] = bool(
            result["passed"] and exact_semantic_approval
        )
        from article_provenance import (
            build_article_claim_ledger,
            extract_sealed_pack,
        )
        result["claim_provenance"] = build_article_claim_ledger(
            extract_sealed_pack(p["source_text"]),
            p.get("article_text") or "",
        )
        result["ready_for_packaging"] = bool(
            result["ready_for_packaging"]
            and result["claim_provenance"]["passed"]
        )
        from policy_intelligence import policy_status
        result["policy_intelligence"] = policy_status(p["vertical"])
        source_policy_hash = _source_policy_hash(p["source_text"])
        current_policy_hash = result["policy_intelligence"]["snapshot_hash"]
        result["policy_intelligence"]["source_snapshot_hash"] = (
            source_policy_hash
        )
        result["policy_intelligence"]["exact_snapshot_match"] = bool(
            source_policy_hash
            and source_policy_hash == current_policy_hash
        )
        result["pre_run_authorized"] = bool(
            result["policy_intelligence"]["current"]
            and result["policy_intelligence"]["exact_snapshot_match"]
            and not result["blockers"]
        )
        wordpress = self.wordpress_draft(project_id)
        result["wordpress_delivery"] = {
            "present": bool(wordpress),
            "site_url": (wordpress or {}).get("site_url", ""),
            "post_id": (wordpress or {}).get("post_id"),
            "edit_url": (wordpress or {}).get("edit_url", ""),
            "article_hash": (wordpress or {}).get("article_hash", ""),
            "exact_hash_match": bool(
                wordpress
                and wordpress.get("article_hash") == p["article_hash"]
                and wordpress.get("remote_content_hash")
                == _hash(p["article_text"])
                and wordpress.get("post_type") == "post"
                and wordpress.get("remote_status") == "draft"
            ),
        }
        result["publication_ready"] = bool(
            result["ready_for_packaging"]
            and p["stage"] == "package_ready"
            and result["wordpress_delivery"]["exact_hash_match"]
        )
        return result

    def inherit_wordpress_draft(
        self, new_project_id, old_project_id, confirmed_post_id=None
    ):
        """Inherit only a WordPress draft whose post ID was explicitly confirmed."""
        new_project = self.get(new_project_id)
        old_project = self.get(old_project_id)
        if (
            new_project["title"].casefold().strip()
            != old_project["title"].casefold().strip()
            or new_project["platform"] != old_project["platform"]
        ):
            raise ValueError(
                "A WordPress draft can only be inherited by a rebuild of the "
                "same product and platform"
            )
        if confirmed_post_id is None:
            raise ValueError(
                "WordPress draft inheritance requires an explicitly confirmed "
                "post ID"
            )
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wordpress_drafts WHERE project_id=?",
                (old_project_id,),
            ).fetchall()
            for row in rows:
                if int(row["post_id"]) != int(confirmed_post_id):
                    continue
                conn.execute(
                    """INSERT INTO wordpress_drafts
                    (project_id,site_url,post_id,article_hash,edit_url,updated_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,site_url) DO UPDATE SET
                    post_id=excluded.post_id,article_hash=excluded.article_hash,
                    edit_url=excluded.edit_url,updated_at=excluded.updated_at""",
                    (
                        new_project_id, row["site_url"], row["post_id"], "",
                        row["edit_url"], _now(),
                    ),
                )

    def wordpress_draft(self, project_id):
        """Return the WordPress draft bound to this exact project, if any."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT site_url,post_id,article_hash,remote_content_hash,
                post_type,remote_status,edit_url,updated_at
                FROM wordpress_drafts WHERE project_id=?
                ORDER BY updated_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_projects(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC"
            ).fetchall()]

    def get(self, project_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError(project_id)
        data = dict(row)
        data["last_report"] = json.loads(data["last_report"] or "{}")
        return data

    def latest_project_from_pack(self, pack, platform, workflow_version=""):
        """Return the newest durable project for this exact product source."""
        fact_source_hash = str(
            (pack.get("source_pack_contract") or {}).get("sha256") or ""
        ).strip()
        if not fact_source_hash:
            return None
        query = (
            "SELECT id FROM projects WHERE fact_source_hash=? AND platform=?"
        )
        params = [fact_source_hash, platform]
        if workflow_version:
            query += " AND source_text LIKE ?"
            params.append(
                f"%AUTOMATION CONTEXT VERSION: {workflow_version}%"
            )
        query += " ORDER BY created_at DESC, updated_at DESC, rowid DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return row["id"] if row else None

    def is_authoritative_run_target(
        self, project_id, pack, platform, workflow_version,
    ):
        """Return whether this ID is the latest durable exact-pack project."""
        if not project_id:
            return False
        return project_id == self.latest_project_from_pack(
            pack, platform, workflow_version
        )

    def _is_authoritative_project_context(self, project):
        """Recheck durable authority during a run, not only at button click."""
        if not self._uses_locked_call_path(project):
            return True
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id FROM projects
                WHERE fact_source_hash=? AND platform=?
                AND source_text LIKE ?
                ORDER BY created_at DESC, updated_at DESC, rowid DESC
                LIMIT 1""",
                (
                    project["fact_source_hash"],
                    project["platform"],
                    "%AUTOMATION CONTEXT VERSION: "
                    + WORKBENCH_SOURCE_CONTEXT_VERSION + "%",
                ),
            ).fetchone()
        return bool(row and row["id"] == project["id"])

    def events(self, project_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE project_id=? ORDER BY id", (project_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def can_recover_locked_pre_signoff(self, project_id):
        """Return whether an admin stop has a zero-cost owned recovery."""
        p = self.get(project_id)
        if p["stage"] != "admin_review" or not self._uses_locked_call_path(p):
            return False
        # The canonical artifact may already be clean even when the event
        # history records an earlier D18 stop. Recovery decisions must follow
        # current state, not a stale blocker payload.
        current_preflight = audit_article(
            p["article_text"],
            p["platform"],
            p["vertical"],
            _source_affiliate_link(p["source_text"]),
        )
        from article_provenance import (
            build_article_claim_ledger,
            extract_sealed_pack,
        )
        current_provenance = build_article_claim_ledger(
            extract_sealed_pack(p["source_text"]),
            p["article_text"],
        )
        final_route = route_for("final_signoff", p["vertical"])
        if (
            not current_preflight["blockers"]
            and not current_provenance.get("coverage_violations")
            and not current_provenance.get("attribution_violations")
            and self._billable_call_count(project_id, "final_signoff")
            < self._purpose_call_limit(p, "final_signoff", final_route)
        ):
            return True
        relevant = [
            event for event in self.events(project_id)
            if event["event_type"] == "pre_signoff_blocked"
        ]
        rejected = [
            event for event in self.events(project_id)
            if event["event_type"] == "candidate_rejected"
        ]
        if rejected:
            payload = rejected[-1].get("payload") or "{}"
            if isinstance(payload, str):
                payload = json.loads(payload)
            rejected_ids = {
                item.get("id") for item in payload.get("blockers") or []
            }
            if (
                rejected_ids == {"D18"}
                and self._latest_successful_call_output(
                    project_id, "compliance_repair"
                )
                and self._billable_call_count(project_id, "final_signoff")
                < self._purpose_call_limit(p, "final_signoff", final_route)
            ):
                return True
        if not relevant:
            return False
        payload = relevant[-1].get("payload") or "{}"
        if isinstance(payload, str):
            payload = json.loads(payload)
        blockers = payload.get("blockers") or []
        blocker_ids = {item.get("id") for item in blockers}
        depth_recoverable = (
            blocker_ids == {"D18"}
            and self.usage_summary(project_id)["calls"] == 3
            and self._billable_call_count(project_id, "final_signoff") == 0
            and (self.projects_dir / project_id / "01-claude-draft.html").exists()
            and (
                self.projects_dir / project_id / "03-claude-revision.html"
            ).exists()
        )
        if depth_recoverable:
            return True
        if not blockers or not all(
            item.get("id") in MECHANICAL_GATES for item in blockers
        ):
            return False
        preflight = audit_article(
            p["article_text"],
            p["platform"],
            p["vertical"],
            _source_affiliate_link(p["source_text"]),
        )
        return not any(
            item.get("id") not in MECHANICAL_GATES
            for item in preflight["blockers"]
        )

    def capabilities(self):
        from .wordpress import WordPressDraftPublisher
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        return {
            "anthropic": bool(anthropic_key),
            "openai": bool(openai_key),
            "anthropic_format_valid": not anthropic_key or anthropic_key.startswith("sk-ant-"),
            "openai_format_valid": not openai_key or openai_key.startswith("sk-"),
            "wordpress": WordPressDraftPublisher().configured,
        }

    def send_to_wordpress_draft(self, project_id):
        from .wordpress import WordPressDraftPublisher
        p = self.get(project_id)
        if p["stage"] != "package_ready" or p["last_report"].get("verdict") != "approved":
            raise RuntimeError("Only a currently approved submission package can be sent to WordPress")
        repaired = repair_publication_gates(
            p["article_text"],
            p["platform"],
            p["vertical"],
            _source_affiliate_link(p["source_text"]),
        )
        if repaired != p["article_text"]:
            raise RuntimeError(
                "WordPress handoff detected a post-approval content mutation. "
                "The normalized artifact must receive independent signoff."
            )
        if p["last_report"].get("reviewed_article_hash") != p["article_hash"]:
            raise RuntimeError(
                "WordPress handoff requires approval bound to the exact final "
                "article hash."
            )
        if p["last_report"].get("approval_purpose") not in {
            "compliance", "final_signoff"
        }:
            raise RuntimeError(
                "WordPress handoff requires a purpose-bound independent "
                "editorial approval."
            )
        findings = deterministic_findings(p["article_text"], p["platform"], p["vertical"])
        blockers, _ = partition_findings(findings)
        if blockers:
            raise RuntimeError(
                "The approved article has a current publication blocker and must "
                "be repaired first: " +
                ", ".join(item["id"] for item in blockers)
            )
        publisher = WordPressDraftPublisher()
        manifest_path = self.projects_dir / project_id / "submission-manifest.json"
        manifest = (
            json.loads(manifest_path.read_text())
            if manifest_path.exists() else {}
        )
        bound_site = str(manifest.get("wordpress_site_url") or "").rstrip("/")
        if bound_site and publisher.site_url.rstrip("/") != bound_site:
            raise RuntimeError(
                "WordPress destination differs from the site bound into the "
                "approved submission package."
            )
        publisher.test_connection()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT post_id FROM wordpress_drafts WHERE project_id=? AND site_url=?",
                (project_id, publisher.site_url),
            ).fetchone()
        delivery_slug = (
            f"si-{project_id}-{p['article_hash'][:12]}".casefold()
        )
        existing_post_id = row["post_id"] if row else None
        if not existing_post_id:
            # Reconcile a remote POST that may have succeeded before a local
            # timeout/read-back failure. The deterministic identity prevents a
            # retry from creating a second draft.
            existing_post_id = publisher.find_draft_by_slug(delivery_slug)
        result = publisher.save_draft(
            p.get("release_title") or p["title"], p["article_text"],
            existing_post_id=existing_post_id,
            slug=delivery_slug,
        )
        remote = publisher.get_draft(result["post_id"])
        expected_title = p.get("release_title") or p["title"]
        remote_content_hash = _hash(remote.get("content_raw") or "")
        identity_errors = []
        if int(remote.get("post_id") or 0) != int(result["post_id"]):
            identity_errors.append("post_id")
        if remote.get("status") != "draft":
            identity_errors.append("status")
        if remote.get("post_type") != "post":
            identity_errors.append("post_type")
        if (remote.get("title_raw") or "").strip() != expected_title.strip():
            identity_errors.append("title")
        if remote_content_hash != _hash(p["article_text"]):
            identity_errors.append("content_hash")
        if identity_errors:
            raise RuntimeError(
                "WordPress remote read-back did not match the approved "
                "artifact: " + ", ".join(identity_errors)
            )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO wordpress_drafts(
                project_id,site_url,post_id,article_hash,remote_content_hash,
                post_type,remote_status,edit_url,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id,site_url) DO UPDATE SET
                post_id=excluded.post_id,article_hash=excluded.article_hash,
                remote_content_hash=excluded.remote_content_hash,
                post_type=excluded.post_type,
                remote_status=excluded.remote_status,
                edit_url=excluded.edit_url,updated_at=excluded.updated_at""",
                (
                    project_id, publisher.site_url, result["post_id"],
                    p["article_hash"], remote_content_hash,
                    remote["post_type"], remote["status"],
                    result["edit_url"], _now(),
                ),
            )
        self._event(project_id, "wordpress_draft_saved", "package_ready", p["article_hash"], {
            "site_url": publisher.site_url, "post_id": result["post_id"],
            "edit_url": result["edit_url"],
            "remote_content_hash": remote_content_hash,
            "remote_status": remote["status"],
            "post_type": remote["post_type"],
        })
        return result

    def next_action(self, project):
        return {
            "source_ready": "Generate Claude draft",
            "drafted": "Run ChatGPT compliance review",
            "compliance_reviewed": "Apply edits with Claude",
            "revised": "Run ChatGPT sign-off",
            "signed_off": "Build submission package",
            "seo_optimized": "Run post-SEO ChatGPT regression",
            "seo_repair_needed": "Repair SEO compliance regressions with Claude",
            "seo_repaired": "Recheck repaired SEO article with ChatGPT",
            "post_seo_signed_off": "Build submission package",
            "package_ready": "Complete",
            "admin_review": "Kevin review queue",
        }[project["stage"]]

    def run_to_completion(
        self, project_id, master_instructions, max_steps=20,
        progress_callback=None,
    ):
        """Run one project under a single lease until a typed terminal state."""
        self._release_stale_run(project_id)
        token = uuid.uuid4().hex
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE projects SET run_token=?,run_started_at=?
                WHERE id=? AND COALESCE(run_token,'')=''""",
                (token, _now(), project_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "This project is already running in another browser session. "
                "The active run owns every remaining stage; wait for it to finish."
            )
        try:
            return self._run_to_completion_unlocked(
                project_id,
                master_instructions,
                max_steps=max_steps,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            p = self.get(project_id)
            self._event(
                project_id, "workflow_error", p["stage"], p["article_hash"],
                {"error_type": type(exc).__name__, "message": str(exc)[:2000]},
            )
            raise
        finally:
            with self._connect() as conn:
                conn.execute(
                    """UPDATE projects SET run_token='',run_started_at=''
                    WHERE id=? AND run_token=?""",
                    (project_id, token),
                )

    def _run_to_completion_unlocked(
        self, project_id, master_instructions, max_steps=20,
        progress_callback=None,
    ):
        """Internal continuous runner; caller must hold the project lease."""
        budget = execution_budget()
        call_limit = budget["calls"]
        no_progress = 0
        for _ in range(max_steps):
            project = self.get(project_id)
            if not self._is_authoritative_project_context(project):
                self._event(
                    project_id,
                    "run_superseded_by_newer_project",
                    project["stage"],
                    project["article_hash"],
                    {
                        "paid_calls": self.usage_summary(project_id)["calls"],
                        "operator_decision_required": False,
                    },
                )
                return project
            contract_blockers = self._prepaid_contract_blockers(project)
            if (
                contract_blockers
                and project["stage"] in PAID_CALL_STAGES
            ):
                self._set_stage(project_id, "admin_review")
                self._event(
                    project_id,
                    "prepaid_contract_blocked",
                    "admin_review",
                    project["article_hash"],
                    {
                        "blockers": contract_blockers,
                        "operator_decision_required": False,
                    },
                )
                return self.get(project_id)
            usage = self.usage_summary(project_id)
            run_calls = usage["calls"]
            limit_reason = ""
            if (
                project["stage"] in PAID_CALL_STAGES
                and run_calls >= call_limit
            ):
                limit_reason = "paid_calls"
            if limit_reason:
                self._event(
                    project_id, "workflow_run_limit", project["stage"],
                    project["article_hash"], {
                        "reason": limit_reason,
                        "run_calls": run_calls,
                        "run_estimated_cost": round(
                            usage["estimated_cost"], 6
                        ),
                        "limits": budget["hard_limits"],
                        "operator_decision_required": False,
                    },
                )
                self._set_stage(project_id, "admin_review")
                return self.get(project_id)
            if project["stage"] == "admin_review":
                if self._uses_locked_call_path(project):
                    if self._recover_locked_pre_signoff(project_id):
                        continue
                    return project
                if self._recover_mechanical_admin_review(project_id):
                    continue
                return self.get(project_id)
            if project["stage"] == "package_ready":
                self._ensure_package_export(project)
                return self.get(project_id)
            before = (
                project["stage"],
                project["article_hash"],
                project["revision_round"],
                self.usage_summary(project_id)["calls"],
            )
            if progress_callback:
                progress_callback(self.next_action(project))
            self._run_next_unlocked(project_id, master_instructions)
            updated = self.get(project_id)
            after = (
                updated["stage"],
                updated["article_hash"],
                updated["revision_round"],
                self.usage_summary(project_id)["calls"],
            )
            if progress_callback:
                progress_callback(
                    "Completed: " + self.next_action(project)
                )
            no_progress = no_progress + 1 if after == before else 0
            if no_progress >= 2:
                self._event(
                    project_id, "workflow_stall_detected", updated["stage"],
                    updated["article_hash"], {"state": after},
                )
                self._set_stage(project_id, "admin_review")
        project = self.get(project_id)
        self._event(
            project_id, "workflow_step_limit_reached", project["stage"],
            project["article_hash"], {
                "max_steps": max_steps,
                "operator_decision_required": False,
            },
        )
        self._set_stage(project_id, "admin_review")
        return self.get(project_id)

    def _prepaid_contract_blockers(self, project):
        """Validate immutable source and policy prerequisites before paid work."""
        if not self._uses_locked_call_path(project):
            return []
        blockers = []
        from article_provenance import extract_sealed_pack
        pack = extract_sealed_pack(project["source_text"])
        claim_count = sum(
            len(items or [])
            for items in (pack.get("publication_claims") or {}).values()
        )
        if claim_count < 3:
            blockers.append({
                "id": "SOURCE-CLAIMS",
                "issue": (
                    f"The sealed pack contains {claim_count} permitted claims; "
                    "at least 3 are required before paid drafting."
                ),
            })
        from policy_intelligence import policy_status
        policy = policy_status(project["vertical"])
        source_policy_hash = _source_policy_hash(project["source_text"])
        if (
            not policy["current"]
            or not source_policy_hash
            or source_policy_hash != policy["snapshot_hash"]
        ):
            blockers.append({
                "id": "POLICY-SNAPSHOT",
                "issue": (
                    "The project is not bound to the current authoritative "
                    "policy snapshot."
                ),
            })
        return blockers

    def _recover_locked_pre_signoff(self, project_id):
        """Apply owned deterministic repairs without opening a paid rescue path."""
        p = self.get(project_id)
        if not self.can_recover_locked_pre_signoff(project_id):
            return False
        current_preflight = audit_article(
            p["article_text"],
            p["platform"],
            p["vertical"],
            _source_affiliate_link(p["source_text"]),
        )
        from article_provenance import (
            build_article_claim_ledger,
            extract_sealed_pack,
        )
        current_provenance = build_article_claim_ledger(
            extract_sealed_pack(p["source_text"]),
            p["article_text"],
        )
        if (
            not current_preflight["blockers"]
            and not current_provenance.get("coverage_violations")
            and not current_provenance.get("attribution_violations")
        ):
            self._set_stage(project_id, "revised")
            self._event(
                project_id,
                "clean_canonical_artifact_recovered",
                "revised",
                p["article_hash"],
                {
                    "paid_calls_added": 0,
                    "next_action": "reserved_final_signoff",
                    "operator_decision_required": False,
                },
            )
            return True
        rejected = [
            event for event in self.events(project_id)
            if event["event_type"] == "candidate_rejected"
        ]
        if rejected:
            payload = rejected[-1].get("payload") or "{}"
            if isinstance(payload, str):
                payload = json.loads(payload)
            rejected_ids = {
                item.get("id") for item in payload.get("blockers") or []
            }
            if rejected_ids == {"D18"}:
                raw_repair = self._latest_successful_call_output(
                    project_id, "compliance_repair"
                )
                if raw_repair and self._set_article(
                    p,
                    raw_repair,
                    "revised",
                    "03-claude-revision.html",
                    bump=True,
                    require_publishable=False,
                ):
                    self._event(
                        project_id,
                        "depth_candidate_reopened",
                        "revised",
                        self.get(project_id)["article_hash"],
                        {
                            "paid_calls_added": 0,
                            "reason": "D18_only",
                            "operator_decision_required": False,
                        },
                    )
                    return self._recover_depth_from_paid_artifacts(project_id)
        relevant = [
            event for event in self.events(project_id)
            if event["event_type"] == "pre_signoff_blocked"
        ]
        payload = relevant[-1].get("payload") or "{}"
        if isinstance(payload, str):
            payload = json.loads(payload)
        blockers = payload.get("blockers") or []
        if {item.get("id") for item in blockers} == {"D18"}:
            return self._recover_depth_from_paid_artifacts(project_id)
        if not blockers or any(
            item.get("id") not in MECHANICAL_GATES for item in blockers
        ):
            return False
        preflight = audit_article(
            p["article_text"],
            p["platform"],
            p["vertical"],
            _source_affiliate_link(p["source_text"]),
        )
        if preflight["mechanical_remaining"]:
            return False
        self._persist_preflight_article(p, preflight, "revised")
        self._event(
            project_id,
            "locked_pre_signoff_mechanical_recovered",
            "revised",
            self.get(project_id)["article_hash"],
            {
                "repaired_gates": [item.get("id") for item in blockers],
                "paid_calls_added": 0,
                "operator_decision_required": False,
            },
        )
        return True

    def _recover_depth_from_paid_artifacts(self, project_id):
        """Reconcile source-mapped blocks from immutable paid writer outputs."""
        p = self.get(project_id)
        project_dir = self.projects_dir / project_id
        draft_path = project_dir / "01-claude-draft.html"
        repair_path = project_dir / "03-claude-revision.html"
        if not draft_path.exists() or not repair_path.exists():
            return False
        from article_provenance import (
            build_article_claim_ledger,
            extract_sealed_pack,
        )
        sealed_pack = extract_sealed_pack(p["source_text"])
        missing_fact_tokens = {
            token
            for item in (
                (sealed_pack.get("required_facts") or {}).get("missing") or []
            )
            for token in re.findall(r"[a-z0-9]+", str(item).casefold())
            if len(token) > 3
        }

        report_path = project_dir / "02-openai-review.json"
        try:
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            report = {}
        prohibited = [
            re.sub(r"\s+", " ", str(item.get("exact_text") or "")).strip().casefold()
            for item in report.get("mandatory_edits", [])
            if str(item.get("exact_text") or "").strip()
        ]

        base = BeautifulSoup(p["article_text"], "html.parser")
        existing_blocks = [
            re.sub(r"\s+", " ", node.get_text(" ", strip=True)).casefold()
            for node in base.find_all(["p", "li"])
        ]
        existing_tokens = [
            set(re.findall(r"[a-z0-9]+", value))
            for value in existing_blocks if value
        ]
        additions = []
        assembled_words = self._article_word_count(p["article_text"])
        target_words = publication_profile(
            p["platform"], p["vertical"]
        )["recovery_target"]
        repair_raw = self._latest_successful_call_output(
            project_id, "compliance_repair"
        )
        sources = (
            repair_raw or repair_path.read_text(),
            self._latest_successful_call_output(project_id, "draft")
            or draft_path.read_text(),
        )
        for source_html in sources:
            source = BeautifulSoup(source_html, "html.parser")
            for node in source.find_all(["p", "ul", "ol"]):
                if assembled_words >= target_words:
                    break
                text = re.sub(
                    r"\s+", " ", node.get_text(" ", strip=True)
                ).strip()
                lowered = text.casefold()
                if len(text.split()) < 12:
                    continue
                if any(exact and exact in lowered for exact in prohibited):
                    continue
                if re.search(
                    r"\bpaid advertorial\b|\bcommission may be earned\b",
                    lowered,
                ):
                    continue
                tokens = set(re.findall(r"[a-z0-9]+", lowered))
                if not tokens:
                    continue
                if any(
                    len(tokens & known) / max(len(tokens | known), 1) >= 0.82
                    for known in existing_tokens
                ):
                    continue
                # Paid artifacts are editorial candidates, not factual
                # authority. Only reuse blocks that textually map to the sealed
                # publication ledger, satisfy required attribution, and do not
                # independently trigger client-advocacy/source-grounding gates.
                block_ledger = build_article_claim_ledger(
                    sealed_pack, str(node)
                )
                maps_permitted_claim = (
                    block_ledger["used_claim_count"] >= 1
                    and not block_ledger["attribution_violations"]
                )
                explains_recorded_gap = bool(
                    missing_fact_tokens & tokens
                    and re.search(
                        r"\b(?:not established|not documented|not available|"
                        r"unavailable|missing|unverified|not verified|verify|"
                        r"confirm|ask the seller|check the current)\b",
                        lowered,
                    )
                )
                if not maps_permitted_claim and not explains_recorded_gap:
                    continue
                block_findings = deterministic_findings(
                    str(node), p["platform"], p["vertical"]
                )
                if any(
                    item.get("id") in {"D19", "D20"}
                    for item in block_findings
                ):
                    continue
                additions.append(str(node))
                existing_tokens.append(tokens)
                assembled_words += len(re.findall(r"\b[\w’'-]+\b", text))
            if assembled_words >= target_words:
                break

        if not additions:
            return False
        heading = base.new_tag("h2")
        strong = base.new_tag("strong")
        strong.string = "Additional Source-Grounded Buyer Details"
        heading.append(strong)
        base.append(heading)
        for fragment in additions:
            parsed = BeautifulSoup(fragment, "html.parser")
            for node in list(parsed.contents):
                base.append(node)

        merged = repair_source_grounding(
            str(base), p["source_text"], p["vertical"]
        )
        self._set_article(
            p, merged, "revised", "03c-depth-reconciled.html", bump=False
        )
        recovered = self.get(project_id)
        preflight = audit_article(
            recovered["article_text"],
            recovered["platform"],
            recovered["vertical"],
            _source_affiliate_link(recovered["source_text"]),
        )
        self._persist_preflight_article(recovered, preflight, "revised")
        recovered = self.get(project_id)
        remaining = audit_article(
            recovered["article_text"],
            recovered["platform"],
            recovered["vertical"],
            _source_affiliate_link(recovered["source_text"]),
        )["blockers"]
        if remaining:
            self._set_stage(project_id, "admin_review")
            self._event(
                project_id, "depth_reconciliation_incomplete", "admin_review",
                recovered["article_hash"], {
                    "remaining_blockers": remaining,
                    "paid_calls_added": 0,
                    "operator_decision_required": False,
                },
            )
            return False
        self._event(
            project_id, "depth_reconciled_from_paid_artifacts", "revised",
            recovered["article_hash"], {
                "source_artifacts": [
                    draft_path.name, repair_path.name
                ],
                "added_blocks": len(additions),
                "final_words": self._article_word_count(
                    recovered["article_text"]
                ),
                "paid_calls_added": 0,
                "next_action": "reserved_final_signoff",
                "operator_decision_required": False,
            },
        )
        return True

    def _latest_successful_call_output(self, project_id, purpose):
        """Return the immutable paid response, regardless of presentation files."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT raw_output FROM llm_calls "
                "WHERE project_id=? AND stage=? AND status='success' "
                "AND raw_output<>'' ORDER BY id DESC LIMIT 1",
                (project_id, purpose),
            ).fetchone()
        return str(row["raw_output"] or "") if row else ""

    def _recover_mechanical_admin_review(self, project_id):
        """Resume legacy admin jobs, including source conflicts the engine can resolve."""
        p = self.get(project_id)
        if self._uses_locked_call_path(p):
            self._event(
                project_id,
                "locked_path_admin_recovery_forbidden",
                "admin_review",
                p["article_hash"],
                {"operator_decision_required": False},
            )
            return False
        report = p.get("last_report") or {}
        if self._report_has_true_source_conflict(report):
            self._record_source_conflict_resolution(p, report)

        target = (
            "post_seo_signed_off"
            if self._billable_call_count(project_id, "post_seo_signoff") > 0
            else "signed_off"
        )
        if report.get("mandatory_edits"):
            self._adjudicate_current(
                p, report,
                target_stage=(
                    "seo_repaired" if target == "post_seo_signed_off"
                    else "revised"
                ),
            )
        recovered = self._complete_adjudicated_signoff(
            project_id, target, "admin-mechanical-recovery.json"
        )
        if recovered:
            p = self.get(project_id)
            self._event(
                project_id, "mechanical_admin_recovered", p["stage"],
                p["article_hash"], {
                    "reason": "legacy admin state contained no typed source conflict"
                },
            )
        return recovered

    def _record_source_conflict_resolution(self, p, report):
        """Record the autonomous policy used to resolve contradictory source facts."""
        conflicts = report.get("source_conflict_evidence") or []
        self._event(
            p["id"], "source_conflict_autoresolved", p["stage"],
            p["article_hash"], {
                "conflicts": conflicts,
                "policy": (
                    "Prefer the controlling source of record and the most current "
                    "first-party record. If neither controls, attribute each version "
                    "or omit the disputed fact and document the limitation."
                ),
            },
        )

    def run_next(self, project_id, master_instructions):
        self._release_stale_run(project_id)
        token = uuid.uuid4().hex
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE projects SET run_token=?,run_started_at=?
                WHERE id=? AND COALESCE(run_token,'')=''""",
                (token, _now(), project_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("This project is already running in another browser session. Wait for it to finish instead of clicking again.")
        try:
            return self._run_next_unlocked(project_id, master_instructions)
        except Exception as exc:
            p = self.get(project_id)
            self._event(project_id, "workflow_error", p["stage"], p["article_hash"], {
                "error_type": type(exc).__name__, "message": str(exc)[:2000],
            })
            raise
        finally:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE projects SET run_token='',run_started_at='' WHERE id=? AND run_token=?",
                    (project_id, token),
                )

    def _release_stale_run(self, project_id):
        """Recover a project lock left behind by a killed app or sleeping Mac."""
        recovered_age = None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_token,run_started_at FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row or not row["run_token"] or not row["run_started_at"]:
                return
            try:
                started = datetime.fromisoformat(row["run_started_at"])
                age = (datetime.now(timezone.utc) - started).total_seconds()
            except (TypeError, ValueError):
                age = float("inf")
            stale_after = int(os.environ.get("NEWSWIRE_STALE_RUN_SECONDS", "900"))
            if age > stale_after:
                conn.execute(
                    "UPDATE projects SET run_token='',run_started_at='' WHERE id=?",
                    (project_id,),
                )
                recovered_age = int(age) if age != float("inf") else None
        if recovered_age is not None or age == float("inf"):
            project = self.get(project_id)
            self._event(project_id, "stale_run_recovered", project["stage"],
                        project["article_hash"], {"age_seconds": recovered_age})

    def _run_next_unlocked(self, project_id, master_instructions):
        p = self.get(project_id)
        stage = p["stage"]
        locked_stages = {
            "source_ready", "drafted", "compliance_reviewed", "revised",
            "signed_off", "package_ready", "admin_review",
        }
        if self._uses_locked_call_path(p) and stage not in locked_stages:
            self._set_stage(project_id, "admin_review")
            self._event(
                project_id,
                "locked_path_legacy_stage_forbidden",
                "admin_review",
                p["article_hash"],
                {
                    "blocked_stage": stage,
                    "operator_decision_required": False,
                },
            )
            return self.get(project_id)
        if stage == "source_ready":
            memory = "\n".join(filter(None, (
                self._learned_guidance(p["platform"], p["vertical"]),
                self._source_failure_guidance(
                    p["fact_source_hash"], p["platform"], p["vertical"]
                ),
            )))
            article = self._claude(generation_prompt(
                p["source_text"], p["platform"], p["vertical"],
                master_instructions, learned_guidance=memory,
            ), p["id"], "draft", p["vertical"])
            self._set_article(
                p, article, "drafted", "01-claude-draft.html",
                call_purpose="draft",
            )
        elif stage == "drafted":
            report = self._openai_review(p, final=False)
            draft_findings = deterministic_findings(
                p["article_text"], p["platform"], p["vertical"]
            )
            draft_blockers, _ = partition_findings(draft_findings)
            if report.get("verdict") == "approved" and not draft_blockers:
                self._set_report(
                    p,
                    report,
                    (
                        "revised"
                        if self._uses_locked_call_path(p)
                        else "signed_off"
                    ),
                    "02-openai-review.json",
                )
            elif report.get("verdict") == "approved":
                # Deterministic publication requirements outrank a semantic
                # reviewer's approval. Continue directly into the one reserved
                # repair call instead of surfacing an operator stop.
                self._set_report(
                    p,
                    report,
                    "compliance_reviewed",
                    "02-openai-review.json",
                )
                self._event(
                    p["id"],
                    "approved_draft_requires_owned_repair",
                    "compliance_reviewed",
                    p["article_hash"],
                    {
                        "blockers": draft_blockers,
                        "next_action": "reserved_compliance_repair",
                        "operator_decision_required": False,
                    },
                )
            else:
                self._set_report(
                    p, report, "drafted", "02-openai-review.json"
                )
                reviewed = self.get(project_id)
                repair_route = route_for(
                    "compliance_repair", reviewed["vertical"]
                )
                repair_already_used = (
                    self._billable_call_count(
                        project_id, "compliance_repair"
                    ) >= self._purpose_call_limit(
                        reviewed, "compliance_repair", repair_route
                    )
                )
                adjudication_target = (
                    "revised"
                    if (
                        self._uses_locked_call_path(reviewed)
                        and repair_already_used
                    )
                    else "compliance_reviewed"
                    if self._uses_locked_call_path(reviewed)
                    else "revised"
                )
                if not self._adjudicate_current(
                    reviewed, report, target_stage=adjudication_target
                ):
                    self._set_stage(project_id, "compliance_reviewed")
        elif stage == "compliance_reviewed":
            memory = "\n".join(filter(None, (
                self._learned_guidance(p["platform"], p["vertical"]),
                self._source_failure_guidance(
                    p["fact_source_hash"], p["platform"], p["vertical"]
                ),
            )))
            repair_purpose = "compliance_repair"
            primary_route = route_for(repair_purpose, p["vertical"])
            if (
                self._billable_call_count(p["id"], repair_purpose)
                >= self._purpose_call_limit(
                    p, repair_purpose, primary_route
                )
            ):
                if self._uses_locked_call_path(p):
                    self._set_stage(project_id, "admin_review")
                    self._event(
                        p["id"],
                        "reserved_repair_call_exhausted",
                        "admin_review",
                        p["article_hash"],
                        {
                            "purpose": repair_purpose,
                            "operator_decision_required": False,
                        },
                    )
                    return self.get(project_id)
                repair_purpose = "quality_rescue"
            article = self._claude(revision_prompt(
                p["source_text"], p["article_text"], p["last_report"],
                p["platform"], p["vertical"], memory,
                release_title=p.get("release_title", p["title"]),
            ), p["id"], repair_purpose, p["vertical"])
            candidate_words = self._article_word_count(article)
            current_words = self._article_word_count(p["article_text"])
            profile = publication_profile(p["platform"], p["vertical"])
            platform_floor = profile["hard_floor"] or 900
            minimum_preserved = max(
                platform_floor,
                int(current_words * 0.80),
            )
            enforce_floor = bool(profile["hard_floor"]) or current_words >= 900
            if enforce_floor and candidate_words < minimum_preserved:
                self._event(
                    p["id"],
                    "destructive_revision_rejected",
                    "compliance_reviewed",
                    p["article_hash"],
                    {
                        "current_words": current_words,
                        "candidate_words": candidate_words,
                        "minimum_preserved": minimum_preserved,
                        "action": "candidate_sent_to_acceptance_boundary",
                    },
                )
            accepted = self._set_article(
                p, article, "revised",
                (
                    "03-claude-revision.html"
                    if repair_purpose == "compliance_repair"
                    else "03-quality-rescue-revision.html"
                ),
                bump=True,
                call_purpose=repair_purpose,
                require_publishable=self._uses_locked_call_path(p),
            )
            if not accepted:
                return self.get(project_id)
        elif stage == "revised":
            preflight = audit_article(
                p["article_text"],
                p["platform"],
                p["vertical"],
                _source_affiliate_link(p["source_text"]),
            )
            self._persist_preflight_article(p, preflight, "revised")
            p = self.get(project_id)
            from article_provenance import (
                build_article_claim_ledger,
                extract_sealed_pack,
            )
            claim_ledger = build_article_claim_ledger(
                extract_sealed_pack(p["source_text"]),
                p["article_text"],
            )
            provenance_blockers = []
            for item in claim_ledger.get("coverage_violations") or []:
                provenance_blockers.append({
                    "id": item["id"],
                    "category": "Claim provenance coverage",
                    "issue": item["issue"],
                    "exact_text": "",
                    "replacement": (
                        "Use additional distinct permitted claims from the "
                        "sealed ledger with their required attribution."
                    ),
                })
            for index, item in enumerate(
                claim_ledger.get("attribution_violations") or [], 1
            ):
                provenance_blockers.append({
                    "id": f"P-ATTR-{index}",
                    "category": "Claim provenance attribution",
                    "issue": (
                        "A mapped sealed claim is missing its required "
                        "seller/source attribution."
                    ),
                    "exact_text": item.get("article_sentence", ""),
                    "replacement": "Add the required attribution.",
                })
            if provenance_blockers:
                preflight["blockers"] = (
                    list(preflight["blockers"]) + provenance_blockers
                )
            if preflight["blockers"]:
                self._set_stage(project_id, "admin_review")
                self._event(
                    p["id"],
                    "pre_signoff_blocked",
                    "admin_review",
                    p["article_hash"],
                    {
                        "blockers": preflight["blockers"],
                        "final_signoff_call_preserved": True,
                        "operator_decision_required": False,
                    },
                )
                # A depth-only stop is recoverable from the two paid writer
                # artifacts already produced in this same transaction. Perform
                # that zero-cost reconciliation immediately so the continuous
                # runner can proceed to the reserved exact-hash sign-off
                # without requiring an operator to resume or rebuild.
                if (
                    {item.get("id") for item in preflight["blockers"]}
                    == {"D18"}
                    and self._recover_locked_pre_signoff(project_id)
                ):
                    self._event(
                        p["id"],
                        "depth_reconciled_inline",
                        self.get(project_id)["stage"],
                        self.get(project_id)["article_hash"],
                        {
                            "paid_calls_added": 0,
                            "next_action": "reserved_final_signoff",
                            "operator_decision_required": False,
                        },
                    )
                return self.get(project_id)
            # Recover projects created before adjudicated sign-off advanced the
            # state. If the paid review ceiling is already exhausted, validate
            # the mechanically corrected article instead of calling the model
            # a third time.
            final_route = route_for("final_signoff", p["vertical"])
            if (
                self._billable_call_count(p["id"], "final_signoff")
                >= self._purpose_call_limit(
                    p, "final_signoff", final_route
                )
                and self._adjudication_count(p["id"]) > 0
            ):
                self._complete_adjudicated_signoff(
                    p["id"], "signed_off", "04-adjudicated-signoff.json"
                )
                return self.get(project_id)
            report = self._openai_review(p, final=True, purpose="final_signoff")
            if report.get("verdict") != "approved":
                if self._uses_locked_call_path(p):
                    self._set_report(
                        p, report, "admin_review",
                        "04-openai-signoff.json",
                    )
                    self._event(
                        p["id"],
                        "exact_hash_final_rejected",
                        "admin_review",
                        p["article_hash"],
                        {
                            "mandatory_count": len(
                                report.get("mandatory_edits") or []
                            ),
                            "operator_decision_required": False,
                        },
                    )
                    return self.get(project_id)
                repair_route = route_for("compliance_repair", p["vertical"])
                if (
                    self._billable_call_count(p["id"], "compliance_repair")
                    < repair_route.max_calls
                ):
                    self._set_report(
                        p, report, "compliance_reviewed",
                        "04-openai-signoff.json",
                    )
                else:
                    if self._report_has_true_source_conflict(report):
                        self._record_source_conflict_resolution(p, report)
                    self._set_report(
                        p, report, "revised", "04-openai-signoff.json"
                    )
                    self._adjudicate_current(self.get(p["id"]), report)
                    self._complete_adjudicated_signoff(
                        p["id"], "signed_off",
                        "04-adjudicated-signoff.json",
                    )
            else:
                self._set_report(p, report, "signed_off", "04-openai-signoff.json")
        elif stage == "signed_off":
            # SERP differentiation is decided during exemplar-grounded
            # generation. Never mutate an independently approved article just
            # to run a separate SEO pass.
            self._build_package(p)
        elif stage == "seo_optimized":
            report = self._openai_review(p, final=True, purpose="post_seo_signoff")
            if report.get("verdict") == "approved":
                target = "post_seo_signed_off"
            else:
                target = "seo_repair_needed"
            self._set_report(p, report, target, "06-openai-post-seo.json")
        elif stage == "seo_repair_needed":
            memory = self._learned_guidance(p["platform"], p["vertical"])
            repair_purpose = "seo_repair"
            primary_route = route_for(repair_purpose, p["vertical"])
            if (
                self._billable_call_count(p["id"], repair_purpose)
                >= primary_route.max_calls
            ):
                repair_purpose = "quality_rescue"
            article = self._claude(revision_prompt(
                p["source_text"], p["article_text"], p["last_report"],
                p["platform"], p["vertical"], memory,
                release_title=p.get("release_title", p["title"]),
            ), p["id"], repair_purpose, p["vertical"])
            self._set_article(
                p, article, "seo_repaired",
                (
                    "07-claude-seo-repair.html"
                    if repair_purpose == "seo_repair"
                    else "07-quality-rescue-seo-repair.html"
                ),
                bump=True,
                call_purpose=repair_purpose,
            )
        elif stage == "seo_repaired":
            post_route = route_for("post_seo_signoff", p["vertical"])
            if (
                self._billable_call_count(p["id"], "post_seo_signoff")
                >= post_route.max_calls
                and self._adjudication_count(p["id"]) > 0
            ):
                self._complete_adjudicated_signoff(
                    p["id"], "post_seo_signed_off",
                    "08-adjudicated-post-seo-signoff.json",
                )
                return self.get(project_id)
            report = self._openai_review(p, final=True, purpose="post_seo_signoff")
            if report.get("verdict") == "approved":
                self._set_report(p, report, "post_seo_signed_off", "08-openai-post-seo-signoff.json")
            elif (
                self._billable_call_count(p["id"], "seo_repair")
                < route_for("seo_repair", p["vertical"]).max_calls
            ):
                self._set_report(
                    p, report, "seo_repair_needed",
                    "08-openai-post-seo-signoff.json",
                )
            else:
                if self._report_has_true_source_conflict(report):
                    self._record_source_conflict_resolution(p, report)
                self._set_report(
                    p, report, "seo_repaired",
                    "08-openai-post-seo-signoff.json",
                )
                self._adjudicate_current(
                    self.get(p["id"]), report, target_stage="seo_repaired"
                )
                self._complete_adjudicated_signoff(
                    p["id"], "post_seo_signed_off",
                    "08-adjudicated-post-seo-signoff.json",
                )
        elif stage == "post_seo_signed_off":
            self._build_package(p)
        return self.get(project_id)

    def import_manual_article(self, project_id, article):
        p = self.get(project_id)
        next_stage = "drafted" if p["stage"] == "source_ready" else "revised"
        self._set_article(p, article, next_stage, "manual-article.html")

    def import_manual_report(self, project_id, report_text):
        p = self.get(project_id)
        try:
            report = json.loads(report_text)
        except json.JSONDecodeError:
            mandatory = max(1, len(re.findall(r"(?im)^\s*\d+\.\s+", report_text)))
            report = {"verdict": "not_approved",
                      "mandatory_count": mandatory,
                      "mandatory_edits": [], "recommended_edits": [],
                      "approved_elements": [],
                      "notes": ["Unstructured manual reports can never approve an article. Paste the JSON report with its article hash.", report_text]}
        if not isinstance(report, dict):
            raise ValueError("Manual compliance report must be a JSON object")
        if report.get("reviewed_article_hash") != p["article_hash"]:
            raise ValueError(
                "Manual report is missing the exact current article hash or reviews a different version. "
                f"Current article hash: {p['article_hash']}"
            )
        target = "signed_off" if p["stage"] == "revised" and report.get("verdict") == "approved" else "compliance_reviewed"
        if p["stage"] == "seo_optimized" and report.get("verdict") == "approved":
            target = "post_seo_signed_off"
        self._set_report(p, report, target, "manual-openai-report.json")

    def _claude(self, prompt, project_id, purpose, vertical):
        replay = self._latest_pending_call(project_id, purpose)
        if replay:
            self._event(
                project_id,
                "paid_response_replayed",
                self.get(project_id)["stage"],
                self.get(project_id)["article_hash"],
                {
                    "purpose": purpose,
                    "call_id": replay["id"],
                    "paid_calls_added": 0,
                },
            )
            return replay["raw_output"]
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        route = route_for(purpose, vertical)
        self._assert_prompt_budget(project_id, purpose, prompt)
        self._assert_call_budget(project_id, purpose, route)
        import anthropic
        client = anthropic.Anthropic(
            api_key=key,
            timeout=float(os.environ.get("NEWSWIRE_PROVIDER_TIMEOUT", "90")),
            max_retries=int(os.environ.get("NEWSWIRE_PROVIDER_RETRIES", "0")),
        )
        try:
            msg = client.messages.create(
                model=route.model,
                max_tokens=route.max_tokens,
                system="You are a client-positive, evidence-bound newsroom writer. Deliver compliant copy without process commentary.",
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            self._record_llm_call(project_id, purpose, route, status="failed", error=str(exc))
            error_text = str(exc).lower()
            if "authentication" in error_text or "api key is invalid" in error_text or "401" in error_text:
                raise RuntimeError(
                    "Anthropic authentication failed. Replace ANTHROPIC_API_KEY in "
                    "Streamlit Secrets with an active Claude API key from console.anthropic.com."
                ) from exc
            raise
        text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text").strip()
        usage = getattr(msg, "usage", None)
        call_id = self._record_llm_call(
            project_id, purpose, route,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            raw_output=text,
            lifecycle="provider_succeeded",
        )
        if getattr(msg, "stop_reason", None) == "max_tokens":
            self._mark_llm_call_lifecycle(call_id, "invalid")
            raise RuntimeError("Claude output was truncated at the token limit; no partial article was saved")
        return text

    def _openai_review(self, p, final, purpose=None):
        prompt = compliance_prompt(
            p["source_text"], p["article_text"], p["platform"], p["vertical"],
            p["last_report"], final=final,
            release_title=p.get("release_title", p["title"]),
        )
        purpose = purpose or ("final_signoff" if final else "compliance")
        route = route_for(purpose, p["vertical"])
        pending = self._latest_pending_call(p["id"], purpose)
        if pending:
            text = pending["raw_output"]
            call_id = pending["id"]
            self._event(
                p["id"],
                "paid_response_replayed",
                p["stage"],
                p["article_hash"],
                {
                    "purpose": purpose,
                    "call_id": call_id,
                    "paid_calls_added": 0,
                },
            )
        else:
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError(
                    "OPENAI_API_KEY is not configured; use manual report import"
                )
            from openai import OpenAI
            client = OpenAI(
                api_key=key,
                timeout=float(
                    os.environ.get("NEWSWIRE_PROVIDER_TIMEOUT", "90")
                ),
                max_retries=int(
                    os.environ.get("NEWSWIRE_PROVIDER_RETRIES", "0")
                ),
            )
            self._assert_call_budget(p["id"], purpose, route)
            self._assert_prompt_budget(p["id"], purpose, prompt)
            try:
                reviewer_system = (
                    "You are the executive editorial adjudicator. Distinguish "
                    "actual publication blockers from preferences. Approve the "
                    "exact article when all material source, platform, legal, "
                    "and reader-value requirements pass; never invent objections "
                    "or demand unsupported facts. Return valid JSON only."
                    if purpose in {
                        "executive_rescue_signoff", "war_room_signoff"
                    }
                    else "You are an independent, publication-focused compliance "
                         "editor. Return valid JSON only."
                )
                response = client.responses.create(
                    model=route.model,
                    input=[{"role": "system", "content": reviewer_system},
                           {"role": "user", "content": prompt}],
                    text={"format": {
                    "type": "json_schema",
                    "name": "newswire_compliance_report",
                    "strict": True,
                    "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "verdict": {"type": "string", "enum": ["approved", "not_approved"]},
                        "mandatory_count": {"type": "integer"},
                        "source_accuracy": {
                            "type": "object", "additionalProperties": False,
                            "properties": {"verified": {"type": "integer"}, "checked": {"type": "integer"}},
                            "required": ["verified", "checked"],
                        },
                        "mandatory_edits": {"type": "array", "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "id": {"type": "string"}, "category": {"type": "string"},
                                "issue": {"type": "string"}, "exact_text": {"type": "string"},
                                "replacement": {"type": "string"},
                            },
                            "required": ["id", "category", "issue", "exact_text", "replacement"],
                        }},
                        "recommended_edits": {"type": "array", "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "id": {"type": "string"}, "category": {"type": "string"},
                                "issue": {"type": "string"}, "replacement": {"type": "string"},
                            },
                            "required": ["id", "category", "issue", "replacement"],
                        }},
                        "approved_elements": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["verdict", "mandatory_count", "source_accuracy", "mandatory_edits",
                                 "recommended_edits", "approved_elements", "notes"],
                    },
                    }},
                )
            except Exception as exc:
                self._record_llm_call(
                    p["id"], purpose, route, status="failed", error=str(exc)
                )
                error_text = str(exc).lower()
                if (
                    "authentication" in error_text
                    or "api key" in error_text
                    or "401" in error_text
                ):
                    raise RuntimeError(
                        "OpenAI authentication failed. Replace OPENAI_API_KEY "
                        "in Streamlit Secrets with an active key from "
                        "platform.openai.com/api-keys."
                    ) from exc
                raise
            usage = getattr(response, "usage", None)
            text = response.output_text.strip()
            call_id = self._record_llm_call(
                p["id"], purpose, route,
                input_tokens=int(
                    getattr(usage, "input_tokens", 0) or 0
                ),
                output_tokens=int(
                    getattr(usage, "output_tokens", 0) or 0
                ),
                raw_output=text,
                lifecycle="provider_succeeded",
            )
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
        try:
            report = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            self._mark_llm_call_lifecycle(call_id, "invalid")
            self._event(
                p["id"],
                "reviewer_output_invalid",
                p["stage"],
                p["article_hash"],
                {
                    "purpose": purpose,
                    "error": str(exc),
                    "output_excerpt": text[:500],
                    "paid_call_consumed": True,
                    "operator_decision_required": False,
                },
            )
            raise RuntimeError(
                f"{purpose} returned an invalid structured report; its reserved "
                "call was consumed and cannot be borrowed by another stage"
            ) from exc
        report["reviewed_article_hash"] = p["article_hash"]
        report["approval_purpose"] = purpose
        report["prompt_version"] = PROMPT_VERSION
        self._event(
            p["id"],
            "reviewer_report_received",
            p["stage"],
            p["article_hash"],
            {
                "purpose": purpose,
                "verdict": report.get("verdict"),
                "mandatory_count": report.get("mandatory_count"),
                "reviewed_article_hash": p["article_hash"],
            },
        )
        report = self._remove_house_rule_conflicts(report, p["article_text"])
        deterministic = deterministic_findings(
            p["article_text"], p["platform"], p["vertical"]
        )
        if deterministic:
            existing = report.setdefault("mandatory_edits", [])
            existing_ids = {item.get("id") for item in existing}
            for item in deterministic:
                blockers, _ = partition_findings([item])
                if blockers and item["id"] not in existing_ids:
                    existing.append(item)
                elif not blockers:
                    report.setdefault("recommended_edits", []).append(item)
            report["mandatory_count"] = len(existing)
            report["verdict"] = "not_approved" if existing else "approved"
        from article_provenance import (
            build_article_claim_ledger,
            extract_sealed_pack,
        )
        provenance = build_article_claim_ledger(
            extract_sealed_pack(p["source_text"]), p["article_text"]
        )
        if provenance.get("coverage_violations"):
            existing = report.setdefault("mandatory_edits", [])
            existing_ids = {item.get("id") for item in existing}
            for index, violation in enumerate(
                provenance["coverage_violations"], 1
            ):
                finding_id = f"P-COVERAGE-{index}"
                if finding_id in existing_ids:
                    continue
                existing.append({
                    "id": finding_id,
                    "category": "Claim provenance",
                    "issue": violation["issue"],
                    "exact_text": "",
                    "replacement": (
                        "Use distinct, relevant permitted claims from the sealed "
                        "record with the required seller/source attribution."
                    ),
                })
            report["mandatory_count"] = len(existing)
            report["verdict"] = "not_approved"
        if provenance["attribution_violations"]:
            existing = report.setdefault("mandatory_edits", [])
            existing_ids = {item.get("id") for item in existing}
            for index, violation in enumerate(
                provenance["attribution_violations"], 1
            ):
                finding_id = f"P-ATTR-{index}"
                if finding_id in existing_ids:
                    continue
                sentence = violation["article_sentence"]
                treatment = violation["required_treatment"]
                prefix = (
                    "Seller materials state that "
                    if treatment == "seller_attribution_required"
                    else "According to the recorded source, "
                )
                replacement = prefix + sentence[:1].lower() + sentence[1:]
                existing.append({
                    "id": finding_id,
                    "category": "Claim provenance",
                    "issue": (
                        "A mapped publication claim is missing its required "
                        f"{treatment.replace('_', ' ')}."
                    ),
                    "exact_text": sentence,
                    "replacement": replacement,
                })
            report["mandatory_count"] = len(existing)
            report["verdict"] = "not_approved"
        return report

    def _record_llm_call(
        self, project_id, stage, route, input_tokens=0,
        output_tokens=0, status="success", error="", raw_output="",
        lifecycle="applied",
    ):
        cost = estimated_cost(route, input_tokens, output_tokens)
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO llm_calls(project_id,stage,provider,model,input_tokens,
                output_tokens,estimated_cost,status,error,created_at,lifecycle,
                raw_output,output_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, stage, route.provider, route.model, input_tokens,
                 output_tokens, cost, status, error[:2000], _now(), lifecycle,
                 raw_output, _hash(raw_output) if raw_output else ""),
            )
            return cursor.lastrowid

    def _mark_llm_call_lifecycle(self, call_id, lifecycle):
        with self._connect() as conn:
            conn.execute(
                "UPDATE llm_calls SET lifecycle=? WHERE id=?",
                (lifecycle, call_id),
            )

    def _mark_llm_call_applied(self, call_id):
        self._mark_llm_call_lifecycle(call_id, "applied")

    def _latest_pending_call(self, project_id, purpose):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,raw_output,output_hash FROM llm_calls "
                "WHERE project_id=? AND stage=? AND status='success' "
                "AND lifecycle='provider_succeeded' AND raw_output<>'' "
                "ORDER BY id DESC LIMIT 1",
                (project_id, purpose),
            ).fetchone()
        return dict(row) if row else None

    def _mark_latest_pending_call_applied(self, project_id, purpose):
        pending = self._latest_pending_call(project_id, purpose)
        if pending:
            self._mark_llm_call_applied(pending["id"])

    def _assert_call_budget(self, project_id, stage, route):
        total_calls = int(self.usage_summary(project_id)["calls"])
        global_limit = execution_budget()["calls"]
        if total_calls >= global_limit:
            raise RuntimeError(
                "Complete-run paid-call ceiling reached; no additional paid "
                "call was made"
            )
        project = self.get(project_id)
        is_current_bounded_run = self._uses_locked_call_path(project)
        if is_current_bounded_run:
            if stage not in REQUIRED_CALL_PATH:
                raise RuntimeError(
                    f"Paid purpose {stage} is outside the locked four-stage "
                    "publication route"
                )
            counts = {
                purpose: self._billable_call_count(project_id, purpose)
                for purpose in REQUIRED_CALL_PATH
            }
            purpose_limit = PURPOSE_CALL_LIMITS[stage]
            if counts[stage] >= purpose_limit:
                raise RuntimeError(
                    f"Reserved {stage} call was already consumed; no other "
                    "purpose may borrow its budget"
                )
            if stage == "draft" and any(counts.values()):
                raise RuntimeError(
                    "Draft must be the first paid call in the publication "
                    "transaction"
                )
            if stage == "compliance" and (
                counts["draft"] != 1
                or counts["compliance_repair"]
                or counts["final_signoff"]
            ):
                raise RuntimeError(
                    "Compliance review requires exactly one completed draft "
                    "and must precede repair and final sign-off"
                )
            if stage == "compliance_repair" and (
                counts["draft"] != 1
                or counts["compliance"] != 1
                or counts["final_signoff"]
            ):
                raise RuntimeError(
                    "Compliance repair requires the completed draft and "
                    "compliance review and must precede final sign-off"
                )
            if stage == "final_signoff" and (
                counts["draft"] != 1 or counts["compliance"] != 1
            ):
                raise RuntimeError(
                    "Final sign-off requires the completed draft and "
                    "compliance review"
                )
        stage_calls = self._billable_call_count(project_id, stage)
        if stage_calls >= route.max_calls:
            raise RuntimeError(
                f"Automated {stage} call ceiling reached; routed to admin "
                "review instead of repeating paid work"
            )

    def _purpose_call_limit(self, project, purpose, route):
        """Return the authoritative per-purpose limit for this project."""
        if self._uses_locked_call_path(project):
            return int(PURPOSE_CALL_LIMITS.get(purpose, 0))
        return int(route.max_calls)

    def _assert_prompt_budget(self, project_id, purpose, prompt):
        """Log and bound stage context before any paid provider request."""
        estimated_tokens = max(1, len(str(prompt or "")) // 4)
        ceiling = int(
            os.environ.get("NEWSWIRE_PROMPT_TOKEN_CEILING", "28000")
        )
        project = self.get(project_id)
        self._event(
            project_id,
            "paid_context_manifest",
            project["stage"],
            project["article_hash"],
            {
                "purpose": purpose,
                "characters": len(str(prompt or "")),
                "estimated_tokens": estimated_tokens,
                "ceiling": ceiling,
            },
        )
        if estimated_tokens > ceiling:
            raise RuntimeError(
                f"{purpose} context is approximately {estimated_tokens:,} "
                f"tokens, above the safe {ceiling:,}-token pre-call ceiling; "
                "no paid request was made"
            )

    def _billable_call_count(self, project_id, stage):
        with self._connect() as conn:
            return conn.execute(
                """SELECT COUNT(*) FROM llm_calls
                WHERE project_id=? AND stage=?
                AND (status='success' OR estimated_cost>0)""",
                (project_id, stage),
            ).fetchone()[0]

    def usage_summary(self, project_id):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT
                COALESCE(SUM(CASE WHEN status='success' OR estimated_cost>0
                    THEN 1 ELSE 0 END),0) calls,
                COUNT(*) attempts,
                COALESCE(SUM(input_tokens),0) input_tokens,
                COALESCE(SUM(output_tokens),0) output_tokens,
                COALESCE(SUM(estimated_cost),0) estimated_cost
                FROM llm_calls WHERE project_id=?""", (project_id,),
            ).fetchone()
        return dict(row)

    def usage_details(self, project_id):
        """Return the complete provider-stage ledger for operator diagnosis."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id,stage,provider,model,input_tokens,output_tokens,
                estimated_cost,status,error,created_at
                FROM llm_calls WHERE project_id=? ORDER BY id""",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _article_word_count(article):
        plain = re.sub(r"<[^>]+>", " ", str(article or ""))
        return len(re.findall(r"\b[\w’'-]+\b", plain))

    def _remove_house_rule_conflicts(self, report, article=""):
        """Prevent a reviewer from turning its own house-rule conflicts into blockers."""
        kept, rejected = [], []
        for item in report.get("mandatory_edits", []) or []:
            exact = str(item.get("exact_text", "") or "")
            replacement = str(item.get("replacement", "") or "")
            issue = str(item.get("issue", "") or "")
            category = str(item.get("category", "") or "")
            if str(item.get("id", "")).startswith("D"):
                rejected.append({"id": item.get("id"), "reason": "deterministic_gate_requires_mechanical_or_model_repair"})
                continue
            reason = ""
            if self._unsafe_reviewer_replacement(replacement):
                reason = "replacement_conflicts_with_house_disclosure_or_cta_rules"
            elif re.fullmatch(r"Priority code\s+[A-Z0-9-]+\s+may apply\.", exact, re.I):
                reason = "source_supplied_priority_code_is_not_internal_language"
            elif re.search(
                r"(?:must|required|should).{0,100}(?:not the official|"
                r"third[- ]party affiliate|affiliate domain)",
                issue,
                re.I,
            ):
                reason = "house_rule_forbids_reader_facing_affiliate_routing_explanation"
            elif (
                re.search(
                    r"\b(?:affiliate|partner|non-public)\s+(?:url|domain)|"
                    r"\baffiliate\s+(?:destination|routing)|"
                    r"\braw affiliate url\b",
                    issue,
                    re.I,
                )
                and not re.search(
                    r"<a\b[^>]*>\s*(?:https?://|www\.)[^<]+</a>",
                    article,
                    re.I,
                )
            ):
                reason = (
                    "house_rule_forbids_affiliate_destination_disclosure_when_"
                    "reader_facing_anchor_is_already_clean"
                )
            elif (
                re.search(r"prior[- ]release", issue, re.I)
                and re.search(r"(?:forbids?|remove).{0,80}(?:link|backlink)", issue, re.I)
            ):
                # The contextual backlink is required. Narrow the objection to
                # the genuinely valid publisher-name/repeated-framing portion.
                item = dict(item)
                item["issue"] = (
                    "Remove any prior publisher name or repeated prior-coverage "
                    "framing while preserving one quiet contextual backlink."
                )
                item["replacement"] = (
                    "Keep one descriptive contextual link without naming its "
                    "publisher or calling it a previous release."
                )
            elif (
                re.search(
                    r"\b(?:grammar|style|cadence|tone|wording|title|headline|"
                    r"seo|scannability|readability|flow|optional)\b",
                    category + " " + issue,
                    re.I,
                )
                and not re.search(
                    r"\b(?:unsupported|false|misleading|fabricat|source|legal|"
                    r"regulat|disclos|affiliate|platform|required|prohibited)\b",
                    category + " " + issue,
                    re.I,
                )
            ):
                report.setdefault("recommended_edits", []).append({
                    "id": str(item.get("id") or "R-SCOPE"),
                    "category": category or "Editorial recommendation",
                    "issue": issue,
                    "replacement": replacement,
                })
                reason = (
                    "non_material_editorial_preference_demoted_to_recommendation"
                )
            if reason:
                rejected.append({"id": item.get("id"), "reason": reason})
            else:
                kept.append(item)
        report["mandatory_edits"] = kept
        report["mandatory_count"] = len(kept)
        if rejected:
            report.setdefault("notes", []).append(
                "House-rule conflicts rejected by deterministic adjudication: " +
                json.dumps(rejected, ensure_ascii=False)
            )
        if not kept:
            report["verdict"] = "approved"
        return report

    def _set_article(
        self, p, article, stage, filename, bump=False, call_purpose=None,
        require_publishable=False,
    ):
        """Finalize a provider candidate before it can replace canonical state.

        Provider success and canonical acceptance are intentionally separate.
        A paid response remains an immutable artifact, but a malformed or
        source-unsafe repair can never overwrite the reviewed article/report.
        """
        from .human_copy import (
            human_copy_diagnostics,
            normalize_american_english,
        )
        article, american_english_changes = normalize_american_english(article)
        article = ensure_article_html(article)
        article = repair_source_grounding(
            article, p["source_text"], p["vertical"]
        )
        if not article:
            raise ValueError("Model returned an empty article")
        title = p.get("release_title") or p["title"]
        title_match = re.search(r"<h1\b[^>]*>(.*?)</h1>", article, re.I | re.S)
        if title_match:
            title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() or title
            article = (article[:title_match.start()] + article[title_match.end():]).strip()
        plain = re.sub(r"<[^>]+>", " ", article)
        word_count = len(re.findall(r"\b[\w’'-]+\b", plain))
        article = normalize_master_html(article, word_count)
        affiliate_href = _source_affiliate_link(p["source_text"])
        if word_count >= 1200 and affiliate_href:
            target = 5 if p["platform"] == "AccessNewsWire" else 4
            article = ensure_affiliate_links(
                article, affiliate_href, target=target
            )
        # Canonicalize all mechanically repairable requirements before any
        # paid compliance review. Reviewers should spend judgment on meaning,
        # not Markdown, heading wrappers, CTA distribution, or disclosures.
        candidate_preflight = audit_article(
            article, p["platform"], p["vertical"], affiliate_href
        )
        article = candidate_preflight["article"]
        from article_provenance import (
            build_article_claim_ledger,
            extract_sealed_pack,
        )
        provenance = build_article_claim_ledger(
            extract_sealed_pack(p["source_text"]), article
        )
        provenance_blockers = list(
            provenance.get("coverage_violations") or []
        ) + list(provenance.get("attribution_violations") or [])
        blockers = list(candidate_preflight["mechanical_remaining"])
        if require_publishable:
            blockers = [
                item for item in candidate_preflight["blockers"]
                if item.get("id") != "D18"
            ]
        if require_publishable:
            blockers.extend(provenance_blockers)
        if blockers:
            rejected_name = filename.rsplit(".", 1)[0] + "-rejected.html"
            self._write(p["id"], rejected_name, article)
            if call_purpose:
                pending = self._latest_pending_call(p["id"], call_purpose)
                if pending:
                    self._mark_llm_call_lifecycle(
                        pending["id"], "candidate_rejected"
                    )
            self._set_stage(p["id"], "admin_review")
            self._event(
                p["id"],
                "candidate_rejected",
                "admin_review",
                p["article_hash"],
                {
                    "purpose": call_purpose or "manual",
                    "candidate_hash": _hash(title + "\n" + article),
                    "canonical_hash_preserved": p["article_hash"],
                    "review_report_preserved": bool(p.get("last_report")),
                    "blockers": blockers,
                    "artifact": rejected_name,
                    "operator_decision_required": False,
                },
            )
            return False
        digest = _hash(title + "\n" + article)
        round_no = p["revision_round"] + (1 if bump else 0)
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE projects SET release_title=?,article_text=?,"
                "article_hash=?,stage=?,last_report='{}',revision_round=?,"
                "updated_at=? WHERE id=? AND article_hash=?",
                (
                    title, article, digest, stage, round_no, _now(), p["id"],
                    p["article_hash"],
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    "Candidate was not applied because the canonical article "
                    "changed after generation"
                )
        self._write(p["id"], filename, article)
        diagnostics = human_copy_diagnostics(article)
        diagnostics["american_english_changes"] = american_english_changes
        self._write(
            p["id"],
            "human-copy-diagnostics.json",
            json.dumps(diagnostics, indent=2, ensure_ascii=False),
        )
        self._event(p["id"], "article_created", stage, digest, {"filename": filename})
        if call_purpose:
            self._mark_latest_pending_call_applied(
                p["id"], call_purpose
            )
        return True

    def _persist_preflight_article(self, p, preflight, stage):
        """Persist a fixed-point mechanical repair without consuming a model call."""
        repaired = preflight["article"]
        if repaired == p["article_text"]:
            return False
        digest = _hash(
            (p.get("release_title") or p["title"]) + "\n" + repaired
        )
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET article_text=?,article_hash=?,stage=?,"
                "last_report='{}',updated_at=? WHERE id=?",
                (repaired, digest, stage, _now(), p["id"]),
            )
        self._write(p["id"], "03b-pre-signoff-mechanical-repair.html", repaired)
        self._event(
            p["id"],
            "pre_signoff_mechanical_repair",
            stage,
            digest,
            {
                "repair_passes": preflight["repair_passes"],
                "initial_findings": preflight["initial_findings"],
                "final_findings": preflight["final_findings"],
                "paid_calls_added": 0,
            },
        )
        return True

    def _set_report(self, p, report, stage, filename):
        self._validate_report(report)
        reviewed_hash = report.get("reviewed_article_hash", p["article_hash"])
        if reviewed_hash != p["article_hash"]:
            raise ValueError("Compliance report does not match the current article version")
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET last_report=?,stage=?,updated_at=? WHERE id=?",
                (json.dumps(report), stage, _now(), p["id"]),
            )
        self._write(p["id"], filename, json.dumps(report, indent=2, ensure_ascii=False))
        event_id = self._event(p["id"], "compliance_report", stage, p["article_hash"], report)
        self._record_issue_observations(event_id, p, report)
        purpose = report.get("approval_purpose")
        if purpose:
            self._mark_latest_pending_call_applied(p["id"], purpose)

    @staticmethod
    def _validate_report(report):
        if not isinstance(report, dict):
            raise ValueError("Compliance response must be a JSON object")
        if report.get("verdict") not in {"approved", "not_approved"}:
            raise ValueError("Compliance response has an invalid verdict")
        edits = report.get("mandatory_edits")
        if not isinstance(edits, list):
            raise ValueError("Compliance response mandatory_edits must be a list")
        if report["verdict"] == "approved" and edits:
            raise ValueError("Compliance response contradicts itself: approved with mandatory edits")
        if report["verdict"] == "not_approved" and not edits:
            raise ValueError("Compliance response is not approved but supplies no actionable mandatory edits")
        report["mandatory_count"] = len(edits)

    def _build_package(self, p):
        current = self.get(p["id"])
        if (
            current["stage"] != "signed_off"
            or current["article_hash"] != p["article_hash"]
        ):
            raise RuntimeError(
                "Package creation lost its exact signed-off artifact snapshot."
            )
        p = current
        final_preflight = audit_article(
            p["article_text"],
            p["platform"],
            p["vertical"],
            _source_affiliate_link(p["source_text"]),
        )
        if final_preflight["blockers"]:
            raise RuntimeError(
                "Package creation is blocked because the exact approved "
                "artifact no longer passes the complete publication preflight: "
                + ", ".join(
                    item.get("id", "unknown")
                    for item in final_preflight["blockers"]
                )
            )
        report = p.get("last_report") or {}
        if (
            report.get("verdict") != "approved"
            or report.get("reviewed_article_hash") != p["article_hash"]
            or report.get("approval_purpose") not in {
                "compliance", "final_signoff"
            }
        ):
            raise RuntimeError(
                "Package creation requires independent approval of the exact "
                "final article hash."
            )
        from article_provenance import (
            build_article_claim_ledger,
            extract_sealed_pack,
        )
        claim_ledger = build_article_claim_ledger(
            extract_sealed_pack(p["source_text"]), p["article_text"]
        )
        if not claim_ledger["passed"]:
            raise RuntimeError(
                "Package creation is blocked because sealed-claim coverage or "
                "required seller/source attribution is incomplete."
            )
        from policy_intelligence import policy_status
        policy = policy_status(p["vertical"])
        source_policy_hash = _source_policy_hash(p["source_text"])
        if (
            not policy["current"]
            or not source_policy_hash
            or source_policy_hash != policy["snapshot_hash"]
        ):
            raise RuntimeError(
                "Package creation is blocked because an applicable authoritative "
                "policy source is missing, changed, awaiting review, or differs "
                "from the snapshot bound to this project."
            )
        manifest = {
            "project_id": p["id"], "title": p["title"],
            "release_title": p.get("release_title", p["title"]), "platform": p["platform"],
            "vertical": p["vertical"], "source_hash": p["source_hash"],
            "article_hash": p["article_hash"], "approved_at": _now(),
            "approval_report": p["last_report"],
            "claim_provenance_file": "claim-provenance.json",
            "policy_snapshot_hash": policy["snapshot_hash"],
            "policy_status": policy,
            "wordpress_site_url": str(
                os.environ.get("NEWSWIRE_WORDPRESS_URL") or ""
            ).rstrip("/"),
        }
        self._write(p["id"], "submission-manifest.json", json.dumps(manifest, indent=2))
        self._write(p["id"], "FINAL-ARTICLE.html", p["article_text"])
        self._write(
            p["id"], "claim-provenance.json",
            json.dumps(claim_ledger, indent=2, ensure_ascii=False),
        )
        export_path = self.exports_dir / f"{p['id']}-submission-package.zip"
        pending_export = self.exports_dir / (
            f".{p['id']}-{p['article_hash'][:12]}.pending.zip"
        )
        project_dir = self.projects_dir / p["id"]
        with zipfile.ZipFile(
            pending_export, "w", zipfile.ZIP_DEFLATED
        ) as archive:
            for name in (
                "00-source-record.txt", "FINAL-ARTICLE.html",
                "claim-provenance.json", "submission-manifest.json",
            ):
                path = project_dir / name
                if path.exists():
                    archive.write(path, arcname=name)
            for path in sorted(project_dir.glob("*-openai-*.json")):
                archive.write(path, arcname=f"audit/{path.name}")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE projects SET stage='package_ready',updated_at=? "
                "WHERE id=? AND stage='signed_off' AND article_hash=?",
                (_now(), p["id"], p["article_hash"]),
            )
        if cursor.rowcount != 1:
            pending_export.unlink(missing_ok=True)
            raise RuntimeError(
                "Package compare-and-swap rejected a concurrent artifact mutation."
            )
        os.replace(pending_export, export_path)
        self._event(p["id"], "package_ready", "package_ready", p["article_hash"], manifest)

    def export_path(self, project_id):
        return self.exports_dir / f"{project_id}-submission-package.zip"

    def _ensure_package_export(self, project):
        """Finish or rebuild an exact-hash package after an interrupted handoff."""
        export_path = self.export_path(project["id"])
        if export_path.exists():
            return export_path
        pending_export = self.exports_dir / (
            f".{project['id']}-{project['article_hash'][:12]}.pending.zip"
        )
        if pending_export.exists():
            os.replace(pending_export, export_path)
            self._event(
                project["id"],
                "package_handoff_recovered",
                "package_ready",
                project["article_hash"],
                {"recovery": "pending_archive_promoted"},
            )
            return export_path
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE projects SET stage='signed_off',updated_at=? "
                "WHERE id=? AND stage='package_ready' AND article_hash=?",
                (_now(), project["id"], project["article_hash"]),
            )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "Package recovery lost the exact approved artifact snapshot."
            )
        self._event(
            project["id"],
            "package_handoff_recovered",
            "signed_off",
            project["article_hash"],
            {"recovery": "package_rebuilt_from_exact_approved_artifact"},
        )
        self._build_package(self.get(project["id"]))
        return export_path

    def _adjudication_count(self, project_id):
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM adjudications WHERE project_id=?", (project_id,)
            ).fetchone()[0]

    def _set_stage(self, project_id, stage):
        with self._connect() as conn:
            conn.execute("UPDATE projects SET stage=?,updated_at=? WHERE id=?", (stage, _now(), project_id))

    @staticmethod
    def _unsafe_reviewer_replacement(text):
        lowered = text.casefold()
        return any(pattern in lowered for pattern in (
            "accessnewswire may receive compensation",
            "barchart may receive compensation",
            "this advertorial may earn compensation",
            "this advertorial may receive compensation",
            "we may earn", "we receive compensation", "our affiliate",
            "promotional offer", "paid placement",
        ))

    @staticmethod
    def _report_has_true_source_conflict(report):
        """Require structured evidence; reviewer prose alone cannot escalate."""
        conflicts = report.get("source_conflict_evidence") or []
        if not isinstance(conflicts, list):
            return False
        for conflict in conflicts:
            if not isinstance(conflict, dict):
                continue
            records = conflict.get("records") or []
            facts = conflict.get("incompatible_facts") or []
            if (
                isinstance(records, list) and len(set(map(str, records))) >= 2
                and isinstance(facts, list) and len(set(map(str, facts))) >= 2
            ):
                return True
        return False

    def _adjudicate_current(self, p, report, target_stage="revised"):
        """Apply safe exact reviewer edits while rejecting stale/contradictory ones."""
        article = p["article_text"]
        original = article
        release_title = p.get("release_title") or p["title"]
        original_title = release_title
        applied, skipped = [], []
        for item in report.get("mandatory_edits", []) or []:
            exact = str(item.get("exact_text", "") or "")
            replacement = str(item.get("replacement", "") or "")
            replacement = replacement.replace(
                "This advertorial may receive compensation if readers click the partner link and subscribe.",
                "Compensation may be received if a subscription is purchased through the partner link in this advertorial.",
            )
            title_match = bool(
                exact and (
                    exact.strip() == release_title.strip()
                    or re.sub(r"<[^>]+>", "", exact).strip() == release_title.strip()
                )
            )
            if not exact or (exact not in article and not title_match):
                skipped.append({"id": item.get("id"), "reason": "exact_text_not_current"})
                continue
            if re.fullmatch(r"Priority code\s+[A-Z0-9-]+\s+may apply\.", exact, re.I):
                skipped.append({"id": item.get("id"), "reason": "source_supplied_priority_code_is_not_internal_language"})
                continue
            if replacement.strip().casefold() in {"[remove]", "remove", "delete"}:
                replacement = ""
            if self._unsafe_reviewer_replacement(replacement):
                skipped.append({"id": item.get("id"), "reason": "replacement_conflicts_with_house_rules"})
                continue
            if title_match:
                cleaned_title = re.sub(r"<[^>]+>", "", replacement).strip()
                if not cleaned_title:
                    skipped.append({
                        "id": item.get("id"),
                        "reason": "title_cannot_be_empty",
                    })
                    continue
                release_title = cleaned_title
            else:
                article = article.replace(exact, replacement, 1)
            applied.append(item.get("id"))

        # Remove source-advertiser urgency wording even when a reviewer points
        # at the wrong exact sentence. This is a safe, meaning-preserving edit.
        for phrase, replacement in (
            (' "almost immediately"', ' promptly'),
            (' "must move fast"', ' should review the timing described'),
            (' "window is beginning to close"', ' timing is discussed in the offer materials'),
        ):
            if phrase in article:
                article = article.replace(phrase, replacement)
                applied.append("house_urgency_rewrite")

        source_hash = p["article_hash"]
        changed = article != original or release_title != original_title
        result_hash = _hash(release_title + "\n" + article) if changed else ""
        payload = {"applied": applied, "skipped": skipped, "prompt_version": PROMPT_VERSION}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO adjudications(project_id,source_article_hash,result_article_hash,applied_count,skipped_count,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (p["id"], source_hash, result_hash, len(applied), len(skipped), json.dumps(payload), _now()),
            )
        if not changed:
            return False
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET release_title=?,article_text=?,article_hash=?,"
                "stage=?,last_report='{}',updated_at=? WHERE id=?",
                (
                    release_title, article, result_hash, target_stage,
                    _now(), p["id"],
                ),
            )
        self._write(p["id"], "07-adjudicated-revision.html", article)
        self._event(p["id"], "adjudicated_revision", target_stage, result_hash, payload)
        return True

    def _complete_adjudicated_signoff(self, project_id, target_stage, filename):
        """Approve a mechanically corrected article after deterministic gates pass."""
        p = self.get(project_id)
        affiliate_href = _source_affiliate_link(p["source_text"])
        preflight = audit_article(
            p["article_text"], p["platform"], p["vertical"], affiliate_href
        )
        repaired = preflight["article"]
        if repaired != p["article_text"]:
            digest = _hash((p.get("release_title") or p["title"]) + "\n" + repaired)
            with self._connect() as conn:
                conn.execute(
                    "UPDATE projects SET article_text=?,article_hash=?,"
                    "last_report='{}',updated_at=? "
                    "WHERE id=?",
                    (repaired, digest, _now(), project_id),
                )
            self._write(project_id, "09-deterministic-gate-repair.html", repaired)
            self._event(
                project_id, "offline_preflight_repair", p["stage"], digest, {
                    "repair_passes": preflight["repair_passes"],
                    "initial_findings": preflight["initial_findings"],
                    "final_findings": preflight["final_findings"],
                    "system_contract": preflight["system_contract"],
                },
            )
        p = self.get(project_id)
        findings = deterministic_findings(
            p["article_text"], p["platform"], p["vertical"]
        )
        blockers, quality_warnings = partition_findings(findings)
        if blockers:
            if self._uses_locked_call_path(p):
                self._set_stage(project_id, "admin_review")
                self._event(
                    project_id,
                    "locked_path_preflight_blocked",
                    "admin_review",
                    p["article_hash"],
                    {
                        "blockers": blockers,
                        "legacy_rescue_forbidden": True,
                        "operator_decision_required": False,
                    },
                )
                return False
            if self.usage_summary(project_id)["calls"] >= execution_budget()["calls"]:
                self._set_stage(project_id, "admin_review")
                self._event(
                    project_id,
                    "global_repair_budget_exhausted",
                    "admin_review",
                    p["article_hash"],
                    {
                        "blockers": blockers,
                        "operator_decision_required": False,
                    },
                )
                return False
            rescue_route = route_for("quality_rescue", p["vertical"])
            rescue_count = self._billable_call_count(
                project_id, "quality_rescue"
            )
            if rescue_count < rescue_route.max_calls:
                rescue_report = {
                    "verdict": "not_approved",
                    "mandatory_count": len(blockers),
                    "mandatory_edits": blockers,
                    "recommended_edits": quality_warnings,
                    "approved_elements": [],
                    "notes": [
                        "Autonomous quality rescue: preserve verified facts, "
                        "commercial strength, prior-release differentiation, "
                        "and complete every mandatory publication gate."
                    ],
                    "reviewed_article_hash": p["article_hash"],
                }
                rescue_article = self._claude(
                    revision_prompt(
                        p["source_text"],
                        p["article_text"],
                        rescue_report,
                        p["platform"],
                        p["vertical"],
                        self._learned_guidance(
                            p["platform"], p["vertical"]
                        ),
                        release_title=p.get("release_title", p["title"]),
                    ),
                    p["id"],
                    "quality_rescue",
                    p["vertical"],
                )
                self._set_article(
                    p,
                    rescue_article,
                    p["stage"],
                    f"10-quality-rescue-{rescue_count + 1}.html",
                    bump=True,
                    call_purpose="quality_rescue",
                )
                rescued = self.get(project_id)
                self._event(
                    project_id,
                    "autonomous_quality_rescue",
                    rescued["stage"],
                    rescued["article_hash"],
                    {
                        "attempt": rescue_count + 1,
                        "blockers": blockers,
                    },
                )
                return self._complete_adjudicated_signoff(
                    project_id, target_stage, filename
                )

            war_route = route_for("war_room_rebuild", p["vertical"])
            war_count = self._billable_call_count(
                project_id, "war_room_rebuild"
            )
            if war_count < war_route.max_calls:
                war_report = {
                    "verdict": "not_approved",
                    "mandatory_count": len(blockers),
                    "mandatory_edits": blockers,
                    "recommended_edits": quality_warnings,
                    "approved_elements": [],
                    "notes": [
                        "War-room rebuild: reconstruct the complete article "
                        "from the sealed source record, preserve the client case, "
                        "and satisfy every remaining publication blocker."
                    ],
                    "reviewed_article_hash": p["article_hash"],
                }
                rebuilt = self._claude(
                    revision_prompt(
                        p["source_text"],
                        p["article_text"],
                        war_report,
                        p["platform"],
                        p["vertical"],
                        self._learned_guidance(
                            p["platform"], p["vertical"]
                        ),
                        release_title=p.get("release_title", p["title"]),
                    ),
                    p["id"],
                    "war_room_rebuild",
                    p["vertical"],
                )
                self._set_article(
                    p, rebuilt, p["stage"],
                    f"11-war-room-rebuild-{war_count + 1}.html",
                    bump=True,
                    call_purpose="war_room_rebuild",
                )
                return self._complete_adjudicated_signoff(
                    project_id, target_stage, filename
                )

            # Every autonomous repair tier was exhausted. Preserve the exact
            # blockers as a typed technical state instead of throwing a generic
            # provider/call-ceiling exception.
            self._set_stage(project_id, "admin_review")
            self._event(
                project_id, "autonomous_repair_exhausted", "admin_review",
                p["article_hash"], {
                    "blockers": blockers,
                    "quality_warnings": quality_warnings,
                    "operator_decision_required": False,
                },
            )
            return False
        # Passing regex/mechanical gates is necessary, never sufficient.
        # A semantic rescue or adjudicated rewrite must be approved by the
        # independent reviewer on the exact final article hash.
        if self._uses_locked_call_path(p):
            self._set_stage(project_id, "admin_review")
            self._event(
                project_id,
                "locked_path_semantic_review_required",
                "admin_review",
                p["article_hash"],
                {
                    "legacy_rescue_forbidden": True,
                    "operator_decision_required": False,
                },
            )
            return False
        if self.usage_summary(project_id)["calls"] >= execution_budget()["calls"]:
            self._set_stage(project_id, "admin_review")
            self._event(
                project_id,
                "global_review_budget_exhausted",
                "admin_review",
                p["article_hash"],
                {
                    "last_report": p.get("last_report") or {},
                    "operator_decision_required": False,
                },
            )
            return False
        review_purpose = ""
        for candidate in (
            "independent_rescue_signoff",
            "executive_rescue_signoff",
            "war_room_signoff",
        ):
            route = route_for(candidate, p["vertical"])
            if self._billable_call_count(
                project_id, candidate
            ) < route.max_calls:
                review_purpose = candidate
                break
        if not review_purpose:
            self._set_stage(project_id, "admin_review")
            self._event(
                project_id, "semantic_review_exhausted", "admin_review",
                p["article_hash"], {
                    "last_report": p.get("last_report") or {},
                    "operator_decision_required": False,
                },
            )
            return False
        report = self._openai_review(
            p, final=True, purpose=review_purpose
        )
        if report.get("verdict") == "approved":
            self._set_report(p, report, target_stage, filename)
            return True

        # Apply exact safe replacements from the independent reviewer before
        # paying another writer to regenerate the whole article. If those
        # bounded edits changed the artifact, review that exact new hash now.
        self._set_report(p, report, p["stage"], filename)
        p = self.get(project_id)
        if self._adjudicate_current(p, report, target_stage=p["stage"]):
            self._event(
                project_id,
                "independent_reviewer_edits_applied",
                p["stage"],
                self.get(project_id)["article_hash"],
                {
                    "review_purpose": review_purpose,
                    "mandatory_count": len(
                        report.get("mandatory_edits", [])
                    ),
                },
            )
            return self._complete_adjudicated_signoff(
                project_id, target_stage, filename
            )

        repair_stage = (
            "seo_repair_needed"
            if target_stage == "post_seo_signed_off"
            else "compliance_reviewed"
        )
        self._set_report(p, report, repair_stage, filename)
        self._event(
            project_id,
            "independent_rescue_rejected",
            repair_stage,
            p["article_hash"],
            {
                "target_stage": target_stage,
                "mandatory_count": len(report.get("mandatory_edits", [])),
            },
        )
        return False

    def _write(self, project_id, filename, content):
        directory = self.projects_dir / project_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        if path.exists():
            old = path.read_text(encoding="utf-8")
            if old != content:
                history = directory / "history"
                history.mkdir(exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                archived = history / f"{path.stem}-{stamp}-{_hash(old)[:10]}{path.suffix}"
                archived.write_text(old, encoding="utf-8")
        path.write_text(content, encoding="utf-8")

    def _event(self, project_id, event_type, stage, article_hash, payload):
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO events(project_id,event_type,stage,article_hash,payload,created_at) VALUES(?,?,?,?,?,?)",
                (project_id, event_type, stage, article_hash,
                 json.dumps(payload, ensure_ascii=False), _now()),
            )
            return cursor.lastrowid

    def _record_issue_observations(self, event_id, project, report):
        with self._connect() as conn:
            for item in report.get("mandatory_edits", []) or []:
                category = str(item.get("category", "Uncategorized"))
                issue = str(item.get("issue", "Unknown issue"))
                conn.execute(
                    "INSERT OR IGNORE INTO issue_observations VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, project["id"], project["platform"], project["vertical"],
                     issue_fingerprint(category, issue), category, issue, _now()),
                )

    def _backfill_issue_memory(self):
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT e.id,e.project_id,e.payload,e.created_at,p.platform,p.vertical
                FROM events e JOIN projects p ON p.id=e.project_id
                WHERE e.event_type='compliance_report'
            """).fetchall()
            for row in rows:
                try:
                    report = json.loads(row["payload"] or "{}")
                except json.JSONDecodeError:
                    continue
                for item in report.get("mandatory_edits", []) or []:
                    category = str(item.get("category", "Uncategorized"))
                    issue = str(item.get("issue", "Unknown issue"))
                    conn.execute(
                        "INSERT OR IGNORE INTO issue_observations VALUES(?,?,?,?,?,?,?,?)",
                        (row["id"], row["project_id"], row["platform"], row["vertical"],
                         issue_fingerprint(category, issue), category, issue, row["created_at"]),
                    )

    def _learned_guidance(self, platform, vertical):
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT category,issue,COUNT(*) AS occurrences
                FROM issue_observations
                WHERE platform=? AND vertical=?
                  AND project_id IN (SELECT id FROM projects WHERE stage='package_ready')
                GROUP BY fingerprint
                HAVING COUNT(*) >= 2
                ORDER BY occurrences DESC, category
                LIMIT 20
            """, (platform, vertical)).fetchall()
        return learned_guidance(self._sanitize_guidance_rows(rows))

    @staticmethod
    def _sanitize_guidance_rows(rows):
        """Prevent stale reviewer conflicts from becoming generation policy."""
        sanitized = []
        for raw in rows:
            row = dict(raw)
            issue = str(row.get("issue", "") or "")
            if re.search(
                r"(?:must|required|should).{0,100}(?:not the official|"
                r"third[- ]party affiliate|affiliate domain)",
                issue,
                re.I,
            ):
                continue
            if (
                re.search(r"prior[- ]release", issue, re.I)
                and re.search(r"(?:forbids?|remove).{0,80}(?:link|backlink)", issue, re.I)
            ):
                row["issue"] = (
                    "Do not name a prior publisher or repeat prior-coverage "
                    "framing; preserve one quiet contextual backlink."
                )
            sanitized.append(row)
        return sanitized

    def _source_failure_guidance(self, fact_source_hash, platform, vertical):
        """Use recent same-source failures immediately, without global promotion."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT io.category,io.issue,COUNT(*) AS occurrences
                FROM issue_observations io
                JOIN projects p ON p.id=io.project_id
                WHERE p.fact_source_hash=? AND p.platform=? AND p.vertical=?
                GROUP BY io.fingerprint
                ORDER BY occurrences DESC, MAX(io.created_at) DESC
                LIMIT 12
            """, (fact_source_hash, platform, vertical)).fetchall()
        rows = self._sanitize_guidance_rows(rows)
        if not rows:
            return ""
        lines = [
            "Same-source issues observed in prior attempts "
            "(prevent them before review):"
        ]
        for row in rows:
            lines.append(
                f"- Seen {row['occurrences']} time(s): "
                f"{row['category']} — {row['issue']}"
            )
        return "\n".join(lines)
