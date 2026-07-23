"""Durable, hash-bound Claude/OpenAI editorial workflow engine."""

import hashlib
import json
import os
import re
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .prompts import (
    compliance_prompt,
    detect_vertical,
    generation_prompt,
    revision_prompt,
    seo_prompt,
)
from .learning import (
    PROMPT_VERSION, deterministic_findings, issue_fingerprint,
    learned_guidance,
)
from .formatting import ensure_affiliate_links, normalize_master_html
from .routing import estimated_cost, route_for


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


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class WorkbenchEngine:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get(
            "NEWSWIRE_WORKBENCH_HOME",
            Path.home() / ".source-intelligence" / "newswire-workbench",
        )).expanduser()
        self.projects_dir = self.root / "projects"
        self.exports_dir = self.root / "exports"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "workbench.db"
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

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
            columns = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
            if "run_token" not in columns:
                conn.execute("ALTER TABLE projects ADD COLUMN run_token TEXT DEFAULT ''")
            if "run_started_at" not in columns:
                conn.execute("ALTER TABLE projects ADD COLUMN run_started_at TEXT DEFAULT ''")
            if "release_title" not in columns:
                conn.execute("ALTER TABLE projects ADD COLUMN release_title TEXT DEFAULT ''")
                conn.execute("UPDATE projects SET release_title=title WHERE release_title='' OR release_title IS NULL")
        self._backfill_issue_memory()

    def create_project(self, title, platform, source_text, vertical="auto"):
        source_text = source_text.strip()
        if not title.strip() or not source_text:
            raise ValueError("Project name and source record are required")
        if vertical == "auto":
            vertical = detect_vertical(source_text)
        pid = uuid.uuid4().hex[:12]
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO projects
                (id,title,release_title,platform,vertical,stage,source_text,source_hash,
                 article_text,article_hash,last_report,revision_round,
                 run_token,run_started_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, title.strip(), title.strip(), platform, vertical, "source_ready",
                 source_text, _hash(source_text), "", "", "{}", 0,
                 "", "", now, now),
            )
        self._event(pid, "project_created", "source_ready", "", {
            "source_hash": _hash(source_text), "vertical": vertical,
        })
        self._write(pid, "00-source-record.txt", source_text)
        return pid

    def create_project_from_pack(self, pack, platform, vertical="auto"):
        """Create or reuse a workbench job from a sealed Source Intelligence pack."""
        from source_pack_contract import validate_source_pack
        validate_source_pack(pack, allow_limited=True)
        product = pack.get("product") or {}
        title = str(product.get("product_name") or "Untitled source project").strip()
        source_text = json.dumps(pack, sort_keys=True, ensure_ascii=False, default=str)
        source_hash = _hash(source_text)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM projects WHERE source_hash=? AND platform=? ORDER BY created_at DESC LIMIT 1",
                (source_hash, platform),
            ).fetchone()
        if existing:
            return existing["id"]
        pid = self.create_project(title, platform, source_text, vertical)
        self._event(pid, "sealed_source_pack_imported", "source_ready", "", {
            "contract": pack["source_pack_contract"],
        })
        return pid

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

    def events(self, project_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE project_id=? ORDER BY id", (project_id,)
            ).fetchall()
        return [dict(r) for r in rows]

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
        findings = deterministic_findings(p["article_text"], p["platform"], p["vertical"])
        if findings:
            raise RuntimeError(
                "The approved article predates current formatting gates and must be repaired first: " +
                ", ".join(item["id"] for item in findings)
            )
        publisher = WordPressDraftPublisher()
        publisher.test_connection()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT post_id FROM wordpress_drafts WHERE project_id=? AND site_url=?",
                (project_id, publisher.site_url),
            ).fetchone()
        result = publisher.save_draft(
            p.get("release_title") or p["title"], p["article_text"],
            existing_post_id=row["post_id"] if row else None,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO wordpress_drafts(project_id,site_url,post_id,article_hash,edit_url,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,site_url) DO UPDATE SET
                post_id=excluded.post_id,article_hash=excluded.article_hash,
                edit_url=excluded.edit_url,updated_at=excluded.updated_at""",
                (project_id, publisher.site_url, result["post_id"], p["article_hash"],
                 result["edit_url"], _now()),
            )
        self._event(project_id, "wordpress_draft_saved", "package_ready", p["article_hash"], {
            "site_url": publisher.site_url, "post_id": result["post_id"],
            "edit_url": result["edit_url"],
        })
        return result

    def next_action(self, project):
        return {
            "source_ready": "Generate Claude draft",
            "drafted": "Run ChatGPT compliance review",
            "compliance_reviewed": "Apply edits with Claude",
            "revised": "Run ChatGPT sign-off",
            "signed_off": "Run Claude SEO optimization",
            "seo_optimized": "Run post-SEO ChatGPT regression",
            "seo_repair_needed": "Repair SEO compliance regressions with Claude",
            "seo_repaired": "Recheck repaired SEO article with ChatGPT",
            "post_seo_signed_off": "Build submission package",
            "package_ready": "Complete",
            "admin_review": "Kevin review queue",
        }[project["stage"]]

    def run_to_completion(self, project_id, master_instructions, max_steps=20):
        """Run unattended until complete, a credential is missing, or admin review."""
        for _ in range(max_steps):
            project = self.get(project_id)
            if project["stage"] in {"package_ready", "admin_review"}:
                return project
            self.run_next(project_id, master_instructions)
        raise RuntimeError("Workflow exceeded its safety step limit")

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
        if stage == "source_ready":
            article = self._claude(generation_prompt(
                p["source_text"], p["platform"], p["vertical"], master_instructions
            ), p["id"], "draft", p["vertical"])
            self._set_article(p, article, "drafted", "01-claude-draft.html")
        elif stage == "drafted":
            report = self._openai_review(p, final=False)
            self._set_report(p, report, "compliance_reviewed", "02-openai-review.json")
        elif stage == "compliance_reviewed":
            memory = self._learned_guidance(p["platform"], p["vertical"])
            article = self._claude(revision_prompt(
                p["source_text"], p["article_text"], p["last_report"],
                p["platform"], p["vertical"], memory,
                release_title=p.get("release_title", p["title"]),
            ), p["id"], "repair", p["vertical"])
            self._set_article(p, article, "revised", "03-claude-revision.html", bump=True)
        elif stage == "revised":
            report = self._openai_review(p, final=True, purpose="final_signoff")
            if report.get("verdict") != "approved":
                if p["revision_round"] >= 2:
                    if self._adjudication_count(p["id"]) < 2:
                        self._set_report(p, report, "revised", "04-openai-signoff.json")
                        if not self._adjudicate_current(self.get(p["id"]), report):
                            self._set_stage(p["id"], "admin_review")
                    else:
                        self._set_report(p, report, "admin_review", "04-openai-signoff.json")
                else:
                    self._set_report(p, report, "compliance_reviewed", "04-openai-signoff.json")
            else:
                self._set_report(p, report, "signed_off", "04-openai-signoff.json")
        elif stage == "signed_off":
            article = self._claude(seo_prompt(
                p["source_text"], p["article_text"], p["platform"], p["vertical"],
                release_title=p.get("release_title", p["title"]),
            ), p["id"], "seo", p["vertical"])
            self._set_article(p, article, "seo_optimized", "05-claude-seo.html")
        elif stage == "seo_optimized":
            report = self._openai_review(p, final=True, purpose="post_seo_signoff")
            if report.get("verdict") == "approved":
                target = "post_seo_signed_off"
            else:
                target = "seo_repair_needed"
            self._set_report(p, report, target, "06-openai-post-seo.json")
        elif stage == "seo_repair_needed":
            memory = self._learned_guidance(p["platform"], p["vertical"])
            article = self._claude(revision_prompt(
                p["source_text"], p["article_text"], p["last_report"],
                p["platform"], p["vertical"], memory,
                release_title=p.get("release_title", p["title"]),
            ), p["id"], "repair", p["vertical"])
            self._set_article(p, article, "seo_repaired", "07-claude-seo-repair.html", bump=True)
        elif stage == "seo_repaired":
            report = self._openai_review(p, final=True, purpose="post_seo_signoff")
            if report.get("verdict") == "approved":
                self._set_report(p, report, "post_seo_signed_off", "08-openai-post-seo-signoff.json")
            elif p["revision_round"] < 2:
                self._set_report(p, report, "seo_repair_needed", "08-openai-post-seo-signoff.json")
            elif self._adjudication_count(p["id"]) < 4:
                self._set_report(p, report, "seo_repaired", "08-openai-post-seo-signoff.json")
                if not self._adjudicate_current(self.get(p["id"]), report, target_stage="seo_repaired"):
                    self._set_stage(p["id"], "admin_review")
            else:
                self._set_report(p, report, "admin_review", "08-openai-post-seo-signoff.json")
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
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        route = route_for(purpose, vertical)
        self._assert_call_budget(project_id, purpose, route)
        import anthropic
        client = anthropic.Anthropic(
            api_key=key,
            timeout=float(os.environ.get("NEWSWIRE_PROVIDER_TIMEOUT", "180")),
            max_retries=int(os.environ.get("NEWSWIRE_PROVIDER_RETRIES", "2")),
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
        self._record_llm_call(
            project_id, purpose, route,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
        if getattr(msg, "stop_reason", None) == "max_tokens":
            raise RuntimeError("Claude output was truncated at the token limit; no partial article was saved")
        return text

    def _openai_review(self, p, final, purpose=None):
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not configured; use manual report import")
        from openai import OpenAI
        client = OpenAI(
            api_key=key,
            timeout=float(os.environ.get("NEWSWIRE_PROVIDER_TIMEOUT", "180")),
            max_retries=int(os.environ.get("NEWSWIRE_PROVIDER_RETRIES", "2")),
        )
        prompt = compliance_prompt(
            p["source_text"], p["article_text"], p["platform"], p["vertical"],
            p["last_report"], final=final,
            release_title=p.get("release_title", p["title"]),
        )
        purpose = purpose or ("final_signoff" if final else "compliance")
        route = route_for(purpose, p["vertical"])
        self._assert_call_budget(p["id"], purpose, route)
        try:
            response = client.responses.create(
            model=route.model,
            input=[{"role": "system", "content": "You are an independent, publication-focused compliance editor. Return valid JSON only."},
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
            self._record_llm_call(p["id"], purpose, route, status="failed", error=str(exc))
            error_text = str(exc).lower()
            if "authentication" in error_text or "api key" in error_text or "401" in error_text:
                raise RuntimeError(
                    "OpenAI authentication failed. Replace OPENAI_API_KEY in Streamlit "
                    "Secrets with an active key from platform.openai.com/api-keys."
                ) from exc
            raise
        usage = getattr(response, "usage", None)
        self._record_llm_call(
            p["id"], purpose, route,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
        text = response.output_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
        report = json.loads(text)
        report["reviewed_article_hash"] = p["article_hash"]
        report["prompt_version"] = PROMPT_VERSION
        report = self._remove_house_rule_conflicts(report)
        deterministic = deterministic_findings(
            p["article_text"], p["platform"], p["vertical"]
        )
        if deterministic:
            existing = report.setdefault("mandatory_edits", [])
            existing_ids = {item.get("id") for item in existing}
            for item in deterministic:
                if item["id"] not in existing_ids:
                    existing.append(item)
            report["mandatory_count"] = len(existing)
            report["verdict"] = "not_approved"
        return report

    def _record_llm_call(self, project_id, stage, route, input_tokens=0,
                         output_tokens=0, status="success", error=""):
        cost = estimated_cost(route, input_tokens, output_tokens)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO llm_calls(project_id,stage,provider,model,input_tokens,
                output_tokens,estimated_cost,status,error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (project_id, stage, route.provider, route.model, input_tokens,
                 output_tokens, cost, status, error[:2000], _now()),
            )

    def _assert_call_budget(self, project_id, stage, route):
        with self._connect() as conn:
            stage_calls = conn.execute(
                """SELECT COUNT(*) FROM llm_calls
                WHERE project_id=? AND stage=?
                AND (status='success' OR estimated_cost>0)""",
                (project_id, stage),
            ).fetchone()[0]
            spent = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost),0) FROM llm_calls WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
        if stage_calls >= route.max_calls:
            raise RuntimeError(
                f"Automated {stage} call ceiling reached; routed to admin review instead of repeating paid work"
            )
        ceiling = float(os.environ.get("NEWSWIRE_PROJECT_BUDGET_USD", "1.50"))
        if spent >= ceiling:
            raise RuntimeError(
                f"Project AI budget ceiling (${ceiling:.2f}) reached; no additional paid call was made"
            )

    def usage_summary(self, project_id):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) input_tokens,
                COALESCE(SUM(output_tokens),0) output_tokens,
                COALESCE(SUM(estimated_cost),0) estimated_cost
                FROM llm_calls WHERE project_id=?""", (project_id,),
            ).fetchone()
        return dict(row)

    def _remove_house_rule_conflicts(self, report):
        """Prevent a reviewer from turning its own house-rule conflicts into blockers."""
        kept, rejected = [], []
        for item in report.get("mandatory_edits", []) or []:
            exact = str(item.get("exact_text", "") or "")
            replacement = str(item.get("replacement", "") or "")
            if str(item.get("id", "")).startswith("D"):
                rejected.append({"id": item.get("id"), "reason": "deterministic_gate_requires_mechanical_or_model_repair"})
                continue
            reason = ""
            if self._unsafe_reviewer_replacement(replacement):
                reason = "replacement_conflicts_with_house_disclosure_or_cta_rules"
            elif re.fullmatch(r"Priority code\s+[A-Z0-9-]+\s+may apply\.", exact, re.I):
                reason = "source_supplied_priority_code_is_not_internal_language"
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

    def _set_article(self, p, article, stage, filename, bump=False):
        article = article.strip()
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
        affiliate_match = re.search(r"(?im)^AFFILIATE LINK:\s*(\S+)", p["source_text"])
        if p["platform"] == "AccessNewsWire" and word_count >= 1200 and affiliate_match:
            article = ensure_affiliate_links(article, affiliate_match.group(1), target=5)
        digest = _hash(title + "\n" + article)
        round_no = p["revision_round"] + (1 if bump else 0)
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET release_title=?,article_text=?,article_hash=?,stage=?,last_report='{}',revision_round=?,updated_at=? WHERE id=?",
                (title, article, digest, stage, round_no, _now(), p["id"]),
            )
        self._write(p["id"], filename, article)
        self._event(p["id"], "article_created", stage, digest, {"filename": filename})

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
        manifest = {
            "project_id": p["id"], "title": p["title"],
            "release_title": p.get("release_title", p["title"]), "platform": p["platform"],
            "vertical": p["vertical"], "source_hash": p["source_hash"],
            "article_hash": p["article_hash"], "approved_at": _now(),
            "approval_report": p["last_report"],
        }
        self._write(p["id"], "submission-manifest.json", json.dumps(manifest, indent=2))
        self._write(p["id"], "FINAL-ARTICLE.html", p["article_text"])
        export_path = self.exports_dir / f"{p['id']}-submission-package.zip"
        project_dir = self.projects_dir / p["id"]
        with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("00-source-record.txt", "FINAL-ARTICLE.html", "submission-manifest.json"):
                path = project_dir / name
                if path.exists():
                    archive.write(path, arcname=name)
            for path in sorted(project_dir.glob("*-openai-*.json")):
                archive.write(path, arcname=f"audit/{path.name}")
        with self._connect() as conn:
            conn.execute("UPDATE projects SET stage='package_ready',updated_at=? WHERE id=?", (_now(), p["id"]))
        self._event(p["id"], "package_ready", "package_ready", p["article_hash"], manifest)

    def export_path(self, project_id):
        return self.exports_dir / f"{project_id}-submission-package.zip"

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

    def _adjudicate_current(self, p, report, target_stage="revised"):
        """Apply safe exact reviewer edits while rejecting stale/contradictory ones."""
        article = p["article_text"]
        original = article
        applied, skipped = [], []
        for item in report.get("mandatory_edits", []) or []:
            exact = str(item.get("exact_text", "") or "")
            replacement = str(item.get("replacement", "") or "")
            replacement = replacement.replace(
                "This advertorial may receive compensation if readers click the partner link and subscribe.",
                "Compensation may be received if a subscription is purchased through the partner link in this advertorial.",
            )
            if not exact or exact not in article:
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
        result_hash = _hash(article) if article != original else ""
        payload = {"applied": applied, "skipped": skipped, "prompt_version": PROMPT_VERSION}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO adjudications(project_id,source_article_hash,result_article_hash,applied_count,skipped_count,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (p["id"], source_hash, result_hash, len(applied), len(skipped), json.dumps(payload), _now()),
            )
        if article == original:
            return False
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET article_text=?,article_hash=?,stage=?,updated_at=? WHERE id=?",
                (article, result_hash, target_stage, _now(), p["id"]),
            )
        self._write(p["id"], "07-adjudicated-revision.html", article)
        self._event(p["id"], "adjudicated_revision", target_stage, result_hash, payload)
        return True

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
        return learned_guidance([dict(row) for row in rows])
