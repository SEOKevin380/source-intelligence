"""
Source Intelligence — Immutable Evidence Lake
==============================================
Every acquired artifact (HTML page, PDF, image, label, API response, search result)
is stored immutably with full provenance metadata. This enables:

- Exact source tracing: "Which page supplied this claim?"
- Temporal tracking: "When was it retrieved? Has it changed?"
- Authority classification: "Was this official, independent, or user-generated?"
- Audit trail: "What extraction method was used?"

Artifacts under 100KB are stored inline in SQLite.
Larger artifacts are stored on disk in the artifacts directory.
"""

import hashlib
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List


_evidence_lock = threading.Lock()


class SourceClass(Enum):
    """Authority classification of a source."""
    OFFICIAL_VENDOR = "official_vendor"
    AUTHORIZED_RESELLER = "authorized_reseller"
    INDEPENDENT_LAB = "independent_lab"
    REGULATORY_DATABASE = "regulatory_database"
    PEER_REVIEWED = "peer_reviewed"
    NEWS_MEDIA = "news_media"
    USER_GENERATED = "user_generated"
    SEARCH_RESULT = "search_result"
    SOCIAL_PROFILE = "social_profile"
    ANONYMOUS = "anonymous"


class SourceRelationship(Enum):
    """Relationship of the source to the offering being researched."""
    FIRST_PARTY = "first_party"       # Official vendor content
    SECOND_PARTY = "second_party"     # Authorized partner/reseller
    THIRD_PARTY = "third_party"       # Independent source


class ArtifactType(Enum):
    """Type of acquired evidence artifact."""
    HTML_SNAPSHOT = "html_snapshot"
    PDF = "pdf"
    IMAGE = "image"
    LABEL_SCREENSHOT = "label_screenshot"
    VIDEO_TRANSCRIPT = "video_transcript"
    API_RESPONSE = "api_response"
    SEARCH_RESULTS = "search_results"
    STRUCTURED_DATA = "structured_data"  # JSON-LD, WooCommerce API, etc.


@dataclass
class Artifact:
    """An immutable piece of evidence acquired during research.

    Once stored, an artifact is never modified — only new artifacts are added.
    The artifact_id is the SHA-256 hash of the content, ensuring deduplication.
    """
    artifact_id: str = ""
    artifact_type: ArtifactType = ArtifactType.HTML_SNAPSHOT
    source_url: str = ""
    final_url: str = ""               # After redirects
    source_class: SourceClass = SourceClass.ANONYMOUS
    source_relationship: SourceRelationship = SourceRelationship.THIRD_PARTY
    captured_at: str = ""             # ISO 8601 UTC
    content_hash: str = ""            # SHA-256 hex
    content_length: int = 0
    tls_verified: bool = True
    status_code: int = 0
    elapsed_ms: float = 0.0
    error: str = ""
    content_path: str = ""            # Relative path for large artifacts (>100KB)
    content_inline: str = ""          # Inline storage for small artifacts (<100KB)
    offering_id: Optional[str] = None
    job_id: Optional[str] = None
    acquisition_phase: str = ""
    notes: str = ""

    @property
    def is_usable(self) -> bool:
        """Check if this artifact contains usable evidence.

        Returns False for failed fetches, empty content, or error-marked artifacts.
        """
        if self.error:
            return False
        if self.notes and self.notes.startswith("FAILED:"):
            return False
        if self.content_length == 0 and not self.content_inline:
            return False
        if self.status_code and not (200 <= self.status_code < 400):
            return False
        return True

    @classmethod
    def from_fetch_result(cls, fetch_result, source_url: str,
                          source_class: SourceClass,
                          source_relationship: SourceRelationship,
                          artifact_type: ArtifactType = ArtifactType.HTML_SNAPSHOT,
                          **kwargs) -> "Artifact":
        """Create an Artifact from a net.py FetchResult.

        This bridges the existing hardened fetch layer to the evidence lake.
        """
        content_hash = fetch_result.content_hash or hashlib.sha256(
            fetch_result.content or b""
        ).hexdigest()

        return cls(
            artifact_id=content_hash,
            artifact_type=artifact_type,
            source_url=source_url,
            final_url=fetch_result.final_url or source_url,
            source_class=source_class,
            source_relationship=source_relationship,
            captured_at=fetch_result.fetched_at or datetime.now(timezone.utc).isoformat(),
            content_hash=content_hash,
            content_length=fetch_result.content_length or len(fetch_result.content or b""),
            tls_verified=fetch_result.tls_verified,
            status_code=fetch_result.status_code,
            elapsed_ms=fetch_result.elapsed_ms,
            error=fetch_result.error or "",
            **kwargs,
        )


class EvidenceLake:
    """Persistent storage for immutable research artifacts.

    Uses the same SQLite database as the main application. The artifacts table
    is created by database.py migration v3.
    """

    INLINE_THRESHOLD = 100_000  # 100KB — smaller artifacts stored in SQLite

    def __init__(self, db_path: str = None, artifacts_dir: str = None):
        if db_path is None:
            from config import DB_PATH
            db_path = DB_PATH
        self.db_path = db_path
        self._artifacts_dir = artifacts_dir or os.path.join(
            os.path.dirname(db_path), "artifacts"
        )
        os.makedirs(self._artifacts_dir, exist_ok=True)
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
        """Create artifacts table if it doesn't exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS artifacts (
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

            CREATE INDEX IF NOT EXISTS idx_artifacts_offering
                ON artifacts(offering_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_source_class
                ON artifacts(source_class);
            CREATE INDEX IF NOT EXISTS idx_artifacts_job
                ON artifacts(job_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_captured
                ON artifacts(captured_at);
        """)
        self.conn.commit()

    def store(self, artifact: Artifact, content: bytes = b"") -> str:
        """Store an artifact immutably. Returns artifact_id.

        - Content under 100KB is stored inline in SQLite.
        - Larger content is stored on disk in the artifacts directory.
        - Duplicate artifact_ids (same content hash) are silently skipped.
        """
        if not artifact.artifact_id:
            artifact.artifact_id = hashlib.sha256(content).hexdigest()
            artifact.content_hash = artifact.artifact_id

        if not artifact.captured_at:
            artifact.captured_at = datetime.now(timezone.utc).isoformat()

        # Immutable — skip if already stored
        existing = self.conn.execute(
            "SELECT artifact_id FROM artifacts WHERE artifact_id = ?",
            (artifact.artifact_id,)
        ).fetchone()
        if existing:
            return artifact.artifact_id

        # Storage decision: inline vs disk
        if content and len(content) < self.INLINE_THRESHOLD:
            artifact.content_inline = content.decode("utf-8", errors="replace")
        elif content:
            date_dir = artifact.captured_at[:10].replace("-", "")
            rel_path = os.path.join(date_dir, f"{artifact.artifact_id[:16]}.bin")
            full_path = os.path.join(self._artifacts_dir, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(content)
            artifact.content_path = rel_path

        artifact.content_length = len(content) if content else artifact.content_length

        with _evidence_lock:
            self.conn.execute("""
                INSERT OR IGNORE INTO artifacts (
                    artifact_id, artifact_type, source_url, final_url,
                    source_class, source_relationship, captured_at,
                    content_hash, content_length, tls_verified, status_code,
                    elapsed_ms, error, content_path, content_inline,
                    offering_id, job_id, acquisition_phase, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                artifact.artifact_id, artifact.artifact_type.value,
                artifact.source_url, artifact.final_url,
                artifact.source_class.value, artifact.source_relationship.value,
                artifact.captured_at, artifact.content_hash,
                artifact.content_length, int(artifact.tls_verified),
                artifact.status_code, artifact.elapsed_ms, artifact.error,
                artifact.content_path, artifact.content_inline,
                artifact.offering_id, artifact.job_id,
                artifact.acquisition_phase, artifact.notes,
            ))
            self.conn.commit()

        return artifact.artifact_id

    def get(self, artifact_id: str) -> Optional[Artifact]:
        """Retrieve artifact metadata by ID."""
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_artifact(dict(row))

    def get_content(self, artifact_id: str) -> str:
        """Retrieve artifact content as text."""
        artifact = self.get(artifact_id)
        if not artifact:
            return ""
        if artifact.content_inline:
            return artifact.content_inline
        if artifact.content_path:
            full = os.path.join(self._artifacts_dir, artifact.content_path)
            if os.path.exists(full):
                with open(full, "rb") as f:
                    return f.read().decode("utf-8", errors="replace")
        return ""

    def list_for_offering(self, offering_id: str,
                          source_class: Optional[SourceClass] = None) -> List[Artifact]:
        """List all artifacts for an offering, optionally filtered by source class."""
        query = "SELECT * FROM artifacts WHERE offering_id = ?"
        params: list = [offering_id]
        if source_class:
            query += " AND source_class = ?"
            params.append(source_class.value)
        query += " ORDER BY captured_at ASC"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_artifact(dict(r)) for r in rows]

    def list_for_job(self, job_id: str) -> List[Artifact]:
        """List all artifacts acquired during a specific job."""
        rows = self.conn.execute(
            "SELECT * FROM artifacts WHERE job_id = ? ORDER BY captured_at ASC",
            (job_id,)
        ).fetchall()
        return [self._row_to_artifact(dict(r)) for r in rows]

    def count(self, offering_id: Optional[str] = None) -> int:
        """Count artifacts, optionally filtered by offering."""
        if offering_id:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE offering_id = ?",
                (offering_id,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()
        return row[0] if row else 0

    @staticmethod
    def _row_to_artifact(d: dict) -> Artifact:
        """Convert a database row dict to an Artifact instance."""
        return Artifact(
            artifact_id=d["artifact_id"],
            artifact_type=ArtifactType(d["artifact_type"]),
            source_url=d.get("source_url", ""),
            final_url=d.get("final_url", ""),
            source_class=SourceClass(d["source_class"]),
            source_relationship=SourceRelationship(d.get("source_relationship", "third_party")),
            captured_at=d.get("captured_at", ""),
            content_hash=d.get("content_hash", ""),
            content_length=d.get("content_length", 0),
            tls_verified=bool(d.get("tls_verified", True)),
            status_code=d.get("status_code", 0),
            elapsed_ms=d.get("elapsed_ms", 0.0),
            error=d.get("error", ""),
            content_path=d.get("content_path", ""),
            content_inline=d.get("content_inline", ""),
            offering_id=d.get("offering_id"),
            job_id=d.get("job_id"),
            acquisition_phase=d.get("acquisition_phase", ""),
            notes=d.get("notes", ""),
        )
