"""Durable, hash-bound Claude/OpenAI editorial workflow engine."""

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .prompts import (
    compliance_prompt,
    detect_vertical,
    generation_prompt,
    revision_prompt,
    seo_prompt,
)


STAGES = (
    "source_ready",
    "drafted",
    "compliance_reviewed",
    "revised",
    "signed_off",
    "seo_optimized",
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
        self.projects_dir.mkdir(parents=True, exist_ok=True)
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
                    platform TEXT NOT NULL,
                    vertical TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    article_text TEXT DEFAULT '',
                    article_hash TEXT DEFAULT '',
                    last_report TEXT DEFAULT '{}',
                    revision_round INTEGER DEFAULT 0,
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
                CREATE TRIGGER IF NOT EXISTS trg_events_no_update
                BEFORE UPDATE ON events BEGIN
                  SELECT RAISE(ABORT, 'workbench events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
                BEFORE DELETE ON events BEGIN
                  SELECT RAISE(ABORT, 'workbench events are immutable');
                END;
            """)

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
                "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, title.strip(), platform, vertical, "source_ready",
                 source_text, _hash(source_text), "", "", "{}", 0, now, now),
            )
        self._event(pid, "project_created", "source_ready", "", {
            "source_hash": _hash(source_text), "vertical": vertical,
        })
        self._write(pid, "00-source-record.txt", source_text)
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
        return {
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "openai": bool(os.environ.get("OPENAI_API_KEY")),
        }

    def next_action(self, project):
        return {
            "source_ready": "Generate Claude draft",
            "drafted": "Run ChatGPT compliance review",
            "compliance_reviewed": "Apply edits with Claude",
            "revised": "Run ChatGPT sign-off",
            "signed_off": "Run Claude SEO optimization",
            "seo_optimized": "Run post-SEO ChatGPT regression",
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
        p = self.get(project_id)
        stage = p["stage"]
        if stage == "source_ready":
            article = self._claude(generation_prompt(
                p["source_text"], p["platform"], p["vertical"], master_instructions
            ))
            self._set_article(p, article, "drafted", "01-claude-draft.html")
        elif stage == "drafted":
            report = self._openai_review(p, final=False)
            self._set_report(p, report, "compliance_reviewed", "02-openai-review.json")
        elif stage == "compliance_reviewed":
            article = self._claude(revision_prompt(
                p["source_text"], p["article_text"], p["last_report"],
                p["platform"], p["vertical"]
            ))
            self._set_article(p, article, "revised", "03-claude-revision.html", bump=True)
        elif stage == "revised":
            report = self._openai_review(p, final=True)
            if report.get("verdict") != "approved":
                if p["revision_round"] >= 3:
                    self._set_report(p, report, "admin_review", "04-openai-signoff.json")
                else:
                    self._set_report(p, report, "compliance_reviewed", "04-openai-signoff.json")
            else:
                self._set_report(p, report, "signed_off", "04-openai-signoff.json")
        elif stage == "signed_off":
            article = self._claude(seo_prompt(
                p["source_text"], p["article_text"], p["platform"], p["vertical"]
            ))
            self._set_article(p, article, "seo_optimized", "05-claude-seo.html")
        elif stage == "seo_optimized":
            report = self._openai_review(p, final=True)
            if report.get("verdict") == "approved":
                target = "post_seo_signed_off"
            elif p["revision_round"] >= 3:
                target = "admin_review"
            else:
                target = "compliance_reviewed"
            self._set_report(p, report, target, "06-openai-post-seo.json")
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
            mandatory = len(re.findall(r"(?im)^\s*\d+\.\s+", report_text))
            approved = "APPROVED" in report_text.upper() and "NOT APPROVED" not in report_text.upper()
            report = {"verdict": "approved" if approved else "not_approved",
                      "mandatory_count": 0 if approved else mandatory,
                      "mandatory_edits": [], "recommended_edits": [],
                      "approved_elements": [], "notes": [report_text]}
        target = "signed_off" if p["stage"] == "revised" and report.get("verdict") == "approved" else "compliance_reviewed"
        if p["stage"] == "seo_optimized" and report.get("verdict") == "approved":
            target = "post_seo_signed_off"
        self._set_report(p, report, target, "manual-openai-report.json")

    def _claude(self, prompt):
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=os.environ.get("ANTHROPIC_GENERATION_MODEL", "claude-sonnet-4-5-20250929"),
            max_tokens=12000,
            system="You are a client-positive, evidence-bound newsroom writer. Deliver compliant copy without process commentary.",
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text").strip()

    def _openai_review(self, p, final):
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not configured; use manual report import")
        from openai import OpenAI
        client = OpenAI(api_key=key)
        prompt = compliance_prompt(
            p["source_text"], p["article_text"], p["platform"], p["vertical"],
            p["last_report"], final=final,
        )
        response = client.responses.create(
            model=os.environ.get("OPENAI_COMPLIANCE_MODEL", "gpt-5.4-mini"),
            input=[{"role": "system", "content": "You are an independent, publication-focused compliance editor. Return valid JSON only."},
                   {"role": "user", "content": prompt}],
        )
        text = response.output_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
        report = json.loads(text)
        report["reviewed_article_hash"] = p["article_hash"]
        return report

    def _set_article(self, p, article, stage, filename, bump=False):
        article = article.strip()
        if not article:
            raise ValueError("Model returned an empty article")
        digest = _hash(article)
        round_no = p["revision_round"] + (1 if bump else 0)
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET article_text=?,article_hash=?,stage=?,last_report='{}',revision_round=?,updated_at=? WHERE id=?",
                (article, digest, stage, round_no, _now(), p["id"]),
            )
        self._write(p["id"], filename, article)
        self._event(p["id"], "article_created", stage, digest, {"filename": filename})

    def _set_report(self, p, report, stage, filename):
        reviewed_hash = report.get("reviewed_article_hash", p["article_hash"])
        if reviewed_hash != p["article_hash"]:
            raise ValueError("Compliance report does not match the current article version")
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET last_report=?,stage=?,updated_at=? WHERE id=?",
                (json.dumps(report), stage, _now(), p["id"]),
            )
        self._write(p["id"], filename, json.dumps(report, indent=2, ensure_ascii=False))
        self._event(p["id"], "compliance_report", stage, p["article_hash"], report)

    def _build_package(self, p):
        manifest = {
            "project_id": p["id"], "title": p["title"], "platform": p["platform"],
            "vertical": p["vertical"], "source_hash": p["source_hash"],
            "article_hash": p["article_hash"], "approved_at": _now(),
            "approval_report": p["last_report"],
        }
        self._write(p["id"], "submission-manifest.json", json.dumps(manifest, indent=2))
        self._write(p["id"], "FINAL-ARTICLE.html", p["article_text"])
        with self._connect() as conn:
            conn.execute("UPDATE projects SET stage='package_ready',updated_at=? WHERE id=?", (_now(), p["id"]))
        self._event(p["id"], "package_ready", "package_ready", p["article_hash"], manifest)

    def _write(self, project_id, filename, content):
        directory = self.projects_dir / project_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(content, encoding="utf-8")

    def _event(self, project_id, event_type, stage, article_hash, payload):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events(project_id,event_type,stage,article_hash,payload,created_at) VALUES(?,?,?,?,?,?)",
                (project_id, event_type, stage, article_hash,
                 json.dumps(payload, ensure_ascii=False), _now()),
            )
