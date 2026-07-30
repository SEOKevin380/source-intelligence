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
import json
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List


_evidence_lock = threading.Lock()


class EvidenceIntegrityError(RuntimeError):
    """Raised when stored evidence bytes no longer match immutable metadata."""


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
    The artifact_id is a SHA-256 capture identity. ``content_hash`` separately
    identifies the exact bytes, so identical bytes captured from two origins
    never discard either origin's provenance.
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
    capture_route: str = ""
    corroboration_eligible: bool = False
    notes: str = ""
    capture_attestation: dict = field(default_factory=dict)

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
            artifact_id="",
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

    def to_attestation_record(self) -> dict:
        """Return the complete capture metadata exported into sealed packs."""
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": (
                self.artifact_type.value
                if hasattr(self.artifact_type, "value")
                else str(self.artifact_type or "")
            ),
            "source_url": self.source_url,
            "final_url": self.final_url,
            "source_class": (
                self.source_class.value
                if hasattr(self.source_class, "value")
                else str(self.source_class or "")
            ),
            "source_relationship": (
                self.source_relationship.value
                if hasattr(self.source_relationship, "value")
                else str(self.source_relationship or "")
            ),
            "captured_at": self.captured_at,
            "status_code": int(self.status_code or 0),
            "content_hash": self.content_hash,
            "content_length": int(self.content_length or 0),
            "tls_verified": bool(self.tls_verified),
            "is_usable": self.is_usable,
            "offering_id": str(self.offering_id or ""),
            "job_id": str(self.job_id or ""),
            "acquisition_phase": str(self.acquisition_phase or ""),
            "capture_route": str(self.capture_route or ""),
            "corroboration_eligible": bool(self.corroboration_eligible),
            "error": str(self.error or ""),
            "notes": str(self.notes or ""),
            "capture_attestation": dict(self.capture_attestation or {}),
        }


class EvidenceLake:
    """Persistent storage for immutable research artifacts.

    Uses the same SQLite database as the main application. The artifacts table
    is created by database.py migration v3.
    """

    INLINE_THRESHOLD = 100_000  # 100KB — smaller artifacts stored in SQLite
    CORROBORATION_ROUTES = frozenset({
        "regulatory_allowlisted",
        "peer_reviewed_allowlisted",
        "independent_lab_verified",
    })

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
                notes TEXT DEFAULT '',
                capture_attestation_json TEXT NOT NULL DEFAULT '{}',
                content_inline_blob BLOB,
                capture_route TEXT NOT NULL DEFAULT '',
                corroboration_eligible INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_artifacts_offering
                ON artifacts(offering_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_source_class
                ON artifacts(source_class);
            CREATE INDEX IF NOT EXISTS idx_artifacts_job
                ON artifacts(job_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_captured
                ON artifacts(captured_at);
            CREATE TRIGGER IF NOT EXISTS trg_artifacts_no_update
                BEFORE UPDATE ON artifacts
                BEGIN SELECT RAISE(
                    ABORT, 'artifacts are immutable'
                ); END;
            CREATE TRIGGER IF NOT EXISTS trg_artifacts_no_delete
                BEFORE DELETE ON artifacts
                BEGIN SELECT RAISE(
                    ABORT, 'artifacts are immutable'
                ); END;
        """)
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(artifacts)")
        }
        if "capture_attestation_json" not in columns:
            self.conn.execute(
                """ALTER TABLE artifacts
                ADD COLUMN capture_attestation_json TEXT
                NOT NULL DEFAULT '{}'"""
            )
        for column, definition in (
            ("content_inline_blob", "BLOB"),
            ("capture_route", "TEXT NOT NULL DEFAULT ''"),
            (
                "corroboration_eligible",
                "INTEGER NOT NULL DEFAULT 0",
            ),
        ):
            if column not in columns:
                self.conn.execute(
                    f"ALTER TABLE artifacts ADD COLUMN {column} {definition}"
                )
        self.conn.commit()

    @staticmethod
    def _capture_identity(artifact: Artifact) -> str:
        material = {
            "content_hash": str(artifact.content_hash or "").strip().casefold(),
            "source_url": str(artifact.source_url or "").strip(),
            "final_url": str(artifact.final_url or "").strip(),
            "source_class": (
                artifact.source_class.value
                if hasattr(artifact.source_class, "value")
                else str(artifact.source_class or "")
            ),
            "source_relationship": (
                artifact.source_relationship.value
                if hasattr(artifact.source_relationship, "value")
                else str(artifact.source_relationship or "")
            ),
            "captured_at": str(artifact.captured_at or "").strip(),
            "offering_id": str(artifact.offering_id or "").strip(),
            "job_id": str(artifact.job_id or "").strip(),
            "acquisition_phase": str(
                artifact.acquisition_phase or ""
            ).strip(),
            "capture_route": str(artifact.capture_route or "").strip(),
        }
        return hashlib.sha256(
            json.dumps(
                material,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    def _store_blob(self, content_hash: str, content: bytes) -> str:
        """Atomically persist one content-addressed blob and verify reuse."""
        date_dir = content_hash[:2]
        rel_path = os.path.join(date_dir, f"{content_hash}.bin")
        full_path = os.path.join(self._artifacts_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        if os.path.exists(full_path):
            with open(full_path, "rb") as handle:
                existing = handle.read()
            if hashlib.sha256(existing).hexdigest() != content_hash:
                raise EvidenceIntegrityError(
                    "Existing evidence blob does not match its content hash"
                )
            return rel_path
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{content_hash}.",
            suffix=".tmp",
            dir=os.path.dirname(full_path),
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o444)
            os.replace(temporary_name, full_path)
            directory_fd = os.open(os.path.dirname(full_path), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        return rel_path

    def store(
        self,
        artifact: Artifact,
        content: bytes = b"",
        *,
        authority_capability=None,
    ) -> str:
        """Store an artifact immutably. Returns artifact_id.

        - Content under 100KB is stored inline in SQLite.
        - Larger content is stored on disk in the artifacts directory.
        - Capture identity and content identity are stored separately.
        - Corroboration-capable captures require a process-local capability
          minted by the validated Acquirer authority route.
        """
        if content:
            actual_hash = hashlib.sha256(content).hexdigest()
            if artifact.content_hash and artifact.content_hash != actual_hash:
                raise ValueError(
                    "Artifact content_hash does not match captured bytes"
                )
            artifact.content_hash = actual_hash
            artifact.content_length = len(content)
        elif not artifact.content_hash:
            artifact.content_hash = hashlib.sha256(content).hexdigest()

        if not artifact.captured_at:
            artifact.captured_at = datetime.now(timezone.utc).isoformat()
        if (
            artifact.corroboration_eligible
            and artifact.capture_route not in self.CORROBORATION_ROUTES
        ):
            raise ValueError(
                "Corroboration eligibility requires a validated capture route"
            )
        if artifact.corroboration_eligible:
            # Import lazily to avoid the evidence <-> acquisition module cycle.
            # A generic caller can persist contextual evidence, but cannot turn
            # caller-supplied authority labels into a corroboration attestation.
            from acquire import _verify_authority_capture_capability
            if not _verify_authority_capture_capability(
                authority_capability,
                artifact,
                content,
            ):
                raise ValueError(
                    "Corroboration eligibility requires an acquisition-owned "
                    "validated authority capability"
                )
        artifact.artifact_id = self._capture_identity(artifact)

        # Immutable idempotency — the capture ID includes the provenance core.
        existing = self.conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (artifact.artifact_id,)
        ).fetchone()
        if existing:
            existing_artifact = self._row_to_artifact(dict(existing))
            if (
                existing_artifact.to_attestation_record()
                | {"capture_attestation": {}}
            ) != (
                artifact.to_attestation_record()
                | {"capture_attestation": {}}
            ):
                raise EvidenceIntegrityError(
                    "Capture identity collision has conflicting provenance"
                )
            return artifact.artifact_id

        # Storage decision: inline vs disk
        inline_blob = None
        if content and len(content) < self.INLINE_THRESHOLD:
            inline_blob = sqlite3.Binary(content)
            artifact.content_inline = ""
        elif content:
            artifact.content_path = self._store_blob(
                artifact.content_hash,
                content,
            )

        if content:
            from source_pack_contract import attest_artifact_capture
            artifact.capture_attestation = attest_artifact_capture(
                artifact.to_attestation_record(),
                artifact.artifact_id,
            )

        with _evidence_lock:
            self.conn.execute("""
                INSERT OR IGNORE INTO artifacts (
                    artifact_id, artifact_type, source_url, final_url,
                    source_class, source_relationship, captured_at,
                    content_hash, content_length, tls_verified, status_code,
                    elapsed_ms, error, content_path, content_inline,
                    offering_id, job_id, acquisition_phase, notes,
                    capture_attestation_json, content_inline_blob,
                    capture_route, corroboration_eligible
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                json.dumps(
                    artifact.capture_attestation,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                inline_blob,
                artifact.capture_route,
                int(bool(artifact.corroboration_eligible)),
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

    def get_content_bytes(self, artifact_id: str) -> bytes:
        """Retrieve captured bytes and verify their full SHA-256 on every read."""
        artifact = self.get(artifact_id)
        if not artifact:
            return b""
        row = self.conn.execute(
            """SELECT content_inline_blob, content_inline
            FROM artifacts WHERE artifact_id=?""",
            (artifact_id,),
        ).fetchone()
        content = b""
        if row and row["content_inline_blob"] is not None:
            content = bytes(row["content_inline_blob"])
        elif row and row["content_inline"]:
            # Compatibility for pre-v3 evidence rows. Integrity verification
            # below fails closed if lossy legacy decoding changed the bytes.
            content = str(row["content_inline"]).encode("utf-8")
        elif artifact.content_path:
            full = os.path.join(self._artifacts_dir, artifact.content_path)
            if os.path.exists(full):
                with open(full, "rb") as handle:
                    content = handle.read()
        if not content and artifact.content_length:
            raise EvidenceIntegrityError(
                "Evidence bytes are missing from immutable storage"
            )
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != str(artifact.content_hash or "").casefold():
            raise EvidenceIntegrityError(
                "Evidence bytes failed SHA-256 integrity verification"
            )
        return content

    def get_content(self, artifact_id: str) -> str:
        """Retrieve verified artifact content as UTF-8 text."""
        return self.get_content_bytes(artifact_id).decode(
            "utf-8",
            errors="ignore",
        )

    def resolve_current_integrity(self, artifact_ids) -> dict:
        """Return current metadata only after verifying every retained blob.

        This is the pack-sealing boundary for persisted artifacts.  It returns
        no partial result: one missing row or one byte/hash mismatch raises
        ``EvidenceIntegrityError`` for the whole requested set.
        """
        requested = []
        seen = set()
        for value in artifact_ids or []:
            artifact_id = str(value or "").strip()
            if not artifact_id:
                raise EvidenceIntegrityError(
                    "Evidence integrity resolution received a blank artifact ID"
                )
            if artifact_id not in seen:
                requested.append(artifact_id)
                seen.add(artifact_id)

        resolved = {}
        for artifact_id in requested:
            artifact = self.get(artifact_id)
            if not artifact:
                raise EvidenceIntegrityError(
                    f"Evidence artifact is missing from immutable storage: "
                    f"{artifact_id}"
                )
            self.get_content_bytes(artifact_id)
            record = artifact.to_attestation_record()
            if str(record.get("artifact_id") or "") != artifact_id:
                raise EvidenceIntegrityError(
                    "Evidence metadata resolved to a different artifact ID"
                )
            resolved[artifact_id] = record
        return resolved

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
        """Convert a database row conservatively, quarantining legacy damage."""
        errors = []

        def safe_number(field, caster, default):
            try:
                return caster(d.get(field) if d.get(field) is not None else default)
            except (TypeError, ValueError):
                errors.append(f"invalid {field}")
                return default

        try:
            artifact_type = ArtifactType(d.get("artifact_type"))
        except (TypeError, ValueError):
            artifact_type = ArtifactType.HTML_SNAPSHOT
            errors.append("invalid artifact_type")
        try:
            source_class = SourceClass(d.get("source_class"))
        except (TypeError, ValueError):
            source_class = SourceClass.ANONYMOUS
            errors.append("invalid source_class")
        try:
            source_relationship = SourceRelationship(
                d.get("source_relationship", "third_party")
            )
        except (TypeError, ValueError):
            source_relationship = SourceRelationship.THIRD_PARTY
            errors.append("invalid source_relationship")
        try:
            capture_attestation = json.loads(
                d.get("capture_attestation_json", "{}") or "{}"
            )
            if not isinstance(capture_attestation, dict):
                raise ValueError("capture attestation is not an object")
        except (TypeError, ValueError, json.JSONDecodeError):
            capture_attestation = {}
            errors.append("invalid capture_attestation_json")
        content_length = safe_number("content_length", int, 0)
        status_code = safe_number("status_code", int, 0)
        elapsed_ms = safe_number("elapsed_ms", float, 0.0)
        tls_value = d.get("tls_verified", False)
        tls_verified = (
            tls_value is True
            or tls_value == 1
            or str(tls_value or "").strip().casefold() in {"1", "true", "yes"}
        )
        eligible_value = d.get("corroboration_eligible", 0)
        corroboration_eligible = (
            eligible_value is True
            or eligible_value == 1
            or str(eligible_value or "").strip().casefold()
            in {"1", "true", "yes"}
        )
        existing_error = str(d.get("error") or "")
        if errors:
            migration_error = "MIGRATION_REPAIR_REQUIRED: " + ", ".join(errors)
            existing_error = (
                existing_error + "; " + migration_error
                if existing_error else migration_error
            )
        return Artifact(
            artifact_id=d["artifact_id"],
            artifact_type=artifact_type,
            source_url=d.get("source_url", ""),
            final_url=d.get("final_url", ""),
            source_class=source_class,
            source_relationship=source_relationship,
            captured_at=d.get("captured_at", ""),
            content_hash=d.get("content_hash", ""),
            content_length=content_length,
            tls_verified=tls_verified,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            error=existing_error,
            content_path=d.get("content_path", ""),
            content_inline=d.get("content_inline", ""),
            offering_id=d.get("offering_id"),
            job_id=d.get("job_id"),
            acquisition_phase=d.get("acquisition_phase", ""),
            capture_route=d.get("capture_route", ""),
            corroboration_eligible=corroboration_eligible,
            notes=d.get("notes", ""),
            capture_attestation=capture_attestation,
        )
