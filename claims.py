"""
Source Intelligence — Atomic Fact/Claim Ledger
===============================================
Every extracted statement becomes a traceable record that can answer:
- What exactly supports this sentence?
- Which source supplied it?
- What exact passage?
- When was it retrieved?
- Was it contradicted elsewhere?
- Who verified it?

Claims are stored per-source, not blended. Conflicts between sources
are detected and surfaced for human resolution.
"""

import hashlib
import html
import json
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List


_claims_lock = threading.Lock()


def _pricing_axis(value: str) -> str:
    """Return the API billing axis encoded in a pricing claim."""
    folded = " ".join(str(value or "").casefold().split())
    if "input token" in folded:
        return "input"
    if "output token" in folded:
        return "output"
    if "cache read" in folded:
        return "cache_reads"
    if "cache write" in folded:
        if "5-minute" in folded or "5 minute" in folded:
            return "cache_writes_5_minute"
        if "1-hour" in folded or "1 hour" in folded:
            return "cache_writes_1_hour"
        return "cache_writes"
    return ""


class ClaimType(Enum):
    """Categories of atomic claims that can be extracted from sources."""
    INGREDIENT_AMOUNT = "ingredient_amount"
    INGREDIENT_FORM = "ingredient_form"
    HEALTH_BENEFIT = "health_benefit"
    PRICING = "pricing"
    REFUND_POLICY = "refund_policy"
    SHIPPING_POLICY = "shipping_policy"
    MANUFACTURER_CLAIM = "manufacturer_claim"
    SERVING_INFO = "serving_info"
    ALLERGEN = "allergen"
    CERTIFICATION = "certification"
    CLINICAL_RESULT = "clinical_result"
    SAFETY_WARNING = "safety_warning"
    DRUG_INTERACTION = "drug_interaction"
    FEATURE = "feature"
    SPECIFICATION = "specification"
    COMPANY_INFO = "company_info"
    TESTIMONIAL = "testimonial"
    REGULATORY_STATUS = "regulatory_status"


class ReviewStatus(Enum):
    """Review/disposition status of a claim."""
    UNREVIEWED = "unreviewed"
    ACCEPTED = "accepted"
    AUTO_SUBSTITUTED = "auto_substituted"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"
    NEEDS_VERIFICATION = "needs_verification"


_NON_HUMAN_REVIEWERS = frozenset({
    "",
    "human",
    "reviewer",
    "automation",
    "official-source-refresh",
    "source-intelligence-automation",
    "system",
})
_NON_HUMAN_REVIEWER_TERMS = frozenset({
    "agent",
    "ai",
    "admin",
    "anonymous",
    "anthropic",
    "automation",
    "automated",
    "bot",
    "claude",
    "gemini",
    "gpt",
    "guest",
    "machine",
    "model",
    "openai",
    "service",
    "system",
    "team",
    "unknown",
    "board",
    "compliance",
    "content",
    "desk",
    "editorial",
    "office",
    "operations",
    "ops",
    "press",
    "qa",
})
_GENERIC_REVIEWER_WORDS = frozenset({
    "being",
    "human",
    "operator",
    "person",
    "reviewer",
    "user",
})


def is_human_reviewer(reviewer: Optional[str]) -> bool:
    """Return whether a persisted reviewer label plausibly names a person.

    This is deliberately a fail-closed label check, not authentication. The
    UI records a self-asserted name, so callers must not describe this value as
    a cryptographically authenticated identity.
    """
    identity = str(reviewer or "").strip().casefold()
    if identity in _NON_HUMAN_REVIEWERS:
        return False
    if identity == "test user":
        return False
    if sum(character.isalpha() for character in identity) < 2:
        return False
    words = {
        token
        for token in "".join(
            character if character.isalpha() else " "
            for character in identity
        ).split()
        if token
    }
    if words & _NON_HUMAN_REVIEWER_TERMS:
        return False
    if words and words <= _GENERIC_REVIEWER_WORDS:
        return False
    # Until reviewer authentication lands, require a person-shaped full name
    # and fail closed on role/department labels such as "Editorial Desk".
    if len(words) < 2:
        return False
    if "source intelligence" in identity:
        return False
    return not (
        identity.startswith("source-intelligence-")
        or identity.endswith("-automation")
        or identity.endswith("_automation")
        or identity.endswith("-bot")
        or identity.endswith("_bot")
    )


def review_attestation_payload(
    *,
    claim_id: str,
    offering_id: str,
    prior_status: str,
    new_status: str,
    reviewer: str,
    reviewed_at: str,
    claim_snapshot_sha256: str,
) -> dict:
    """Return the immutable payload bound to a typed review transition."""
    return {
        "claim_id": str(claim_id or "").strip(),
        "offering_id": str(offering_id or "").strip(),
        "prior_status": str(prior_status or "").strip(),
        "new_status": str(new_status or "").strip(),
        "reviewer": str(reviewer or "").strip(),
        "reviewed_at": str(reviewed_at or "").strip(),
        "claim_snapshot_sha256": str(
            claim_snapshot_sha256 or ""
        ).strip().casefold(),
    }


def canonical_claim_snapshot(claim, *, offering_id: str = "") -> dict:
    """Return immutable assertion material bound by human review."""
    getter = (
        claim.get
        if isinstance(claim, dict)
        else lambda key, default=None: getattr(claim, key, default)
    )
    metadata = dict(getter("metadata", {}) or {})
    metadata.pop("review_attestation", None)
    claim_type = getter("claim_type", "")
    if isinstance(claim_type, ClaimType):
        claim_type = claim_type.value
    return {
        "claim_id": str(getter("claim_id", "") or "").strip(),
        "offering_id": str(
            offering_id or getter("offering_id", "") or ""
        ).strip(),
        "claim_text": " ".join(str(
            getter("claim_text", "")
            or getter("text", "")
            or ""
        ).split()),
        "claim_type": str(claim_type or "").strip(),
        "source_artifact_id": str(
            getter("source_artifact_id", "")
            or getter("artifact_id", "")
            or ""
        ).strip(),
        "exact_excerpt": str(
            getter("exact_excerpt", "")
            or getter("excerpt", "")
            or ""
        ),
        "page_location": str(
            getter("page_location", "")
            or getter("location", "")
            or ""
        ),
        "captured_at": str(getter("captured_at", "") or "").strip(),
        "source_class": str(getter("source_class", "") or "").strip(),
        "extraction_method": str(
            getter("extraction_method", "") or ""
        ).strip(),
        "effective_market": str(
            getter("effective_market", "US") or "US"
        ).strip(),
        "metadata": metadata,
    }


def claim_snapshot_hash(claim, *, offering_id: str = "") -> str:
    snapshot = canonical_claim_snapshot(claim, offering_id=offering_id)
    material = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def review_event_hash(payload: dict) -> str:
    """Hash the canonical review transition stored in the append-only ledger."""
    material = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(material.encode()).hexdigest()


def verify_review_attestation(
    claim,
    *,
    pack_offering_id: str = "",
) -> bool:
    """Verify a sealed claim's signed append-only review-transition proof."""
    getter = (
        claim.get
        if isinstance(claim, dict)
        else lambda key, default=None: getattr(claim, key, default)
    )
    metadata = getter("metadata", {}) or {}
    proof = metadata.get("review_attestation")
    if not isinstance(proof, dict):
        return False
    event = proof.get("event")
    signature = proof.get("signature")
    if not isinstance(event, dict) or not isinstance(signature, dict):
        return False
    event_offering_id = str(event.get("offering_id") or "").strip()
    claim_offering_id = str(getter("offering_id", "") or "").strip()
    sealed_offering_id = str(pack_offering_id or claim_offering_id).strip()
    if (
        not sealed_offering_id
        or not claim_offering_id
        or claim_offering_id != sealed_offering_id
        or sealed_offering_id != event_offering_id
    ):
        return False
    snapshot_sha = claim_snapshot_hash(
        claim, offering_id=sealed_offering_id
    )
    review_status = getter("review_status", "")
    if isinstance(review_status, ReviewStatus):
        review_status = review_status.value
    expected = review_attestation_payload(
        claim_id=getter("claim_id", ""),
        offering_id=sealed_offering_id,
        prior_status=event.get("prior_status"),
        new_status=review_status,
        reviewer=getter("reviewed_by", ""),
        reviewed_at=getter("reviewed_at", ""),
        claim_snapshot_sha256=snapshot_sha,
    )
    if expected != event:
        return False
    if expected["new_status"] != ReviewStatus.ACCEPTED.value:
        return False
    event_hash = review_event_hash(event)
    if proof.get("event_hash") != event_hash:
        return False
    try:
        from trust_attestations import verify_attestation
        return verify_attestation("claim-review-transition", event, signature)
    except (ImportError, OSError, TypeError, ValueError):
        return False


def is_human_acceptance(review_status, reviewed_by: Optional[str]) -> bool:
    """Return the label-level human-acceptance shape.

    This helper does not verify the signed review transition. Authority gates
    must call :func:`has_attested_human_acceptance` instead.
    """
    status = (
        review_status.value
        if isinstance(review_status, ReviewStatus)
        else str(review_status or "").strip().casefold()
    )
    return status == ReviewStatus.ACCEPTED.value and is_human_reviewer(
        reviewed_by
    )


def has_attested_human_acceptance(
    claim,
    *,
    offering_id: str = "",
) -> bool:
    """Return whether a claim has a valid, offering-bound human acceptance."""
    getter = (
        claim.get
        if isinstance(claim, dict)
        else lambda key, default=None: getattr(claim, key, default)
    )
    return is_human_acceptance(
        getter("review_status", ""),
        getter("reviewed_by", ""),
    ) and verify_review_attestation(
        claim,
        pack_offering_id=str(offering_id or "").strip(),
    )


def _evidence_claim_snapshot(claim, *, offering_id: str = "") -> dict:
    """Return claim material covered by a capture-to-claim attestation."""
    snapshot = canonical_claim_snapshot(claim, offering_id=offering_id)
    metadata = dict(snapshot.get("metadata") or {})
    metadata.pop("evidence_attestation", None)
    snapshot["metadata"] = metadata
    return snapshot


def _evidence_claim_snapshot_hash(claim, *, offering_id: str = "") -> str:
    material = json.dumps(
        _evidence_claim_snapshot(claim, offering_id=offering_id),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def claim_evidence_attestation_payload(
    claim,
    artifact,
    *,
    offering_id: str = "",
) -> dict:
    """Return the immutable claim↔capture link signed after byte verification."""
    claim_get = (
        claim.get
        if isinstance(claim, dict)
        else lambda key, default=None: getattr(claim, key, default)
    )
    artifact_get = (
        artifact.get
        if isinstance(artifact, dict)
        else lambda key, default=None: getattr(artifact, key, default)
    )
    claim_offering_id = str(
        offering_id or claim_get("offering_id", "") or ""
    ).strip()
    artifact_offering_id = str(
        artifact_get("offering_id", "") or ""
    ).strip()
    exact_excerpt = str(
        claim_get("exact_excerpt", "")
        or claim_get("excerpt", "")
        or ""
    )
    metadata = claim_get("metadata", {}) or {}
    capture_attestation = (
        artifact_get("capture_attestation", {}) or {}
    )
    capture_proof_hash = hashlib.sha256(
        json.dumps(
            capture_attestation,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return {
        "claim_id": str(claim_get("claim_id", "") or "").strip(),
        "offering_id": claim_offering_id,
        "artifact_offering_id": artifact_offering_id,
        "claim_snapshot_sha256": _evidence_claim_snapshot_hash(
            claim,
            offering_id=claim_offering_id,
        ),
        "artifact_id": str(
            claim_get("source_artifact_id", "")
            or claim_get("artifact_id", "")
            or ""
        ).strip(),
        "artifact_content_sha256": str(
            artifact_get("content_hash", "") or ""
        ).strip().casefold(),
        "capture_attestation_sha256": capture_proof_hash,
        "exact_excerpt_sha256": hashlib.sha256(
            exact_excerpt.encode()
        ).hexdigest(),
        "fact_key": str(metadata.get("fact_key") or "").strip(),
        "extraction_method": str(
            claim_get("extraction_method", "") or ""
        ).strip(),
    }


def _normalized_literal_text(value: str) -> str:
    """Normalize presentation-only differences for literal containment."""
    text = html.unescape(str(value or ""))
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    if text.startswith("..."):
        text = text[3:]
    if text.endswith("..."):
        text = text[:-3]
    return " ".join(text.split())


def _literal_tokens(value: str) -> tuple:
    """Tokenize literal text while ignoring presentation-only punctuation.

    Extractors often render a label/value pair with a colon even when the
    retained page uses whitespace or a table cell boundary.  Token comparison
    admits that formatting difference while preserving numbers, currencies,
    percentages, and comparison operators as distinct semantic tokens.
    """
    text = _normalized_literal_text(value)
    text = (
        text.replace("\N{MINUS SIGN}", "-")
        .replace("\N{EN DASH}", "-")
        .replace("\N{EM DASH}", "-")
        .replace("\N{LESS-THAN OR EQUAL TO}", "<=")
        .replace("\N{GREATER-THAN OR EQUAL TO}", ">=")
    )
    return tuple(re.findall(
        r"<=|>=|!=|==|[<>=%$€£±]|"
        r"\d+(?:[.,]\d+)*|"
        r"[^\W\d_]+(?:['’][^\W\d_]+)?",
        text,
        flags=re.UNICODE,
    ))


def literal_claim_matches_excerpt(claim_text: str, exact_excerpt: str) -> bool:
    """Return whether the asserted literal text occurs in its exact excerpt.

    A source excerpt being present in captured bytes is not enough: the claim
    itself must also be literal text within that excerpt. Paraphrases and
    inferences must use a non-literal review path.
    """
    claim_tokens = _literal_tokens(claim_text)
    excerpt_tokens = _literal_tokens(exact_excerpt)
    if not claim_tokens or len(claim_tokens) > len(excerpt_tokens):
        return False
    width = len(claim_tokens)
    return any(
        excerpt_tokens[index:index + width] == claim_tokens
        for index in range(len(excerpt_tokens) - width + 1)
    )


def verify_claim_evidence_attestation(
    claim,
    artifact,
    *,
    pack_offering_id: str = "",
) -> bool:
    """Verify a literal claim was linked to this signed capture at extraction."""
    claim_get = (
        claim.get
        if isinstance(claim, dict)
        else lambda key, default=None: getattr(claim, key, default)
    )
    artifact_get = (
        artifact.get
        if isinstance(artifact, dict)
        else lambda key, default=None: getattr(artifact, key, default)
    )
    metadata = claim_get("metadata", {}) or {}
    proof = metadata.get("evidence_attestation")
    if not isinstance(proof, dict):
        return False
    payload = proof.get("payload")
    signature = proof.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, dict):
        return False
    claim_offering_id = str(claim_get("offering_id", "") or "").strip()
    artifact_offering_id = str(
        artifact_get("offering_id", "") or ""
    ).strip()
    pack_offering_id = str(pack_offering_id or "").strip()
    if (
        not pack_offering_id
        or claim_offering_id != pack_offering_id
        or artifact_offering_id != pack_offering_id
    ):
        return False
    claim_text = str(
        claim_get("claim_text", "")
        or claim_get("text", "")
        or ""
    )
    exact_excerpt = str(
        claim_get("exact_excerpt", "")
        or claim_get("excerpt", "")
        or ""
    )
    if not literal_claim_matches_excerpt(claim_text, exact_excerpt):
        return False
    expected = claim_evidence_attestation_payload(
        claim,
        artifact,
        offering_id=pack_offering_id,
    )
    if payload != expected:
        return False
    if proof.get("payload_sha256") != hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest():
        return False
    try:
        from trust_attestations import verify_attestation
        return verify_attestation(
            "claim-evidence-link",
            payload,
            signature,
        )
    except (ImportError, OSError, TypeError, ValueError):
        return False


@dataclass
class Claim:
    """An atomic, traceable statement extracted from evidence.

    Every claim links back to its source artifact and preserves the
    exact excerpt that supports it.
    """
    claim_id: str = ""
    offering_id: str = ""
    claim_text: str = ""
    claim_type: ClaimType = ClaimType.MANUFACTURER_CLAIM
    source_artifact_id: Optional[str] = None
    exact_excerpt: str = ""
    page_location: str = ""           # CSS selector, heading, section name
    captured_at: str = ""
    source_class: str = ""            # Mirrors the artifact's source_class
    confidence: float = 0.0
    extraction_method: str = "manual"  # llm_extraction, regex, api, manual, machine_ocr
    effective_market: str = "US"
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    conflicts: List[str] = field(default_factory=list)  # Claim IDs that conflict
    metadata: dict = field(default_factory=dict)         # Type-specific data


def claim_publication_record(claim: Claim) -> dict:
    """Serialize a durable claim row into the source-pack claim shape."""
    claim_type = (
        claim.claim_type.value
        if isinstance(claim.claim_type, ClaimType)
        else str(claim.claim_type or "")
    )
    review_status = (
        claim.review_status.value
        if isinstance(claim.review_status, ReviewStatus)
        else str(claim.review_status or "")
    )
    return {
        "claim_id": claim.claim_id,
        "offering_id": claim.offering_id,
        "claim_type": claim_type,
        "text": claim.claim_text,
        "artifact_id": claim.source_artifact_id,
        "excerpt": claim.exact_excerpt,
        "location": claim.page_location,
        "captured_at": claim.captured_at,
        "source_class": claim.source_class,
        "confidence": claim.confidence,
        "extraction_method": claim.extraction_method,
        "effective_market": claim.effective_market,
        "review_status": review_status,
        "reviewed_by": claim.reviewed_by,
        "reviewed_at": claim.reviewed_at,
        "conflicts": list(claim.conflicts or []),
        "metadata": dict(claim.metadata or {}),
    }


class ClaimsLedger:
    """Manages atomic claims backed by the claims table in SQLite.

    Uses the same database as the main application.
    The claims table is created by database.py migration v3.
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            from config import DB_PATH
            db_path = DB_PATH
        self.db_path = db_path
        self._conn = None

    @staticmethod
    def _same_immutable_claim(row, claim: Claim) -> bool:
        """Return whether an existing row is the same idempotent assertion.

        Claim IDs are content-derived by the extraction pipeline. Re-running a
        completed extraction may therefore encounter the same row again. That
        retry is safe to ignore, but a caller-supplied ID that points at
        different assertion material must never overwrite the durable record.
        """
        if not row:
            return False
        row_type = str(row["claim_type"] or "")
        claim_type = (
            claim.claim_type.value
            if isinstance(claim.claim_type, ClaimType)
            else str(claim.claim_type or "")
        )
        return (
            str(row["offering_id"] or "") == str(claim.offering_id or "")
            and str(row["claim_text"] or "") == str(claim.claim_text or "")
            and row_type == claim_type
            and str(row["source_artifact_id"] or "")
            == str(claim.source_artifact_id or "")
        )

    @staticmethod
    def _normalized_literal(value: str) -> str:
        """Normalize only presentation whitespace for byte-derived excerpts."""
        return _normalized_literal_text(value)

    def _attest_literal_claim_from_capture(self, claim: Claim) -> None:
        """Bind a literal claim to hash-verified bytes from this evidence lake.

        Raw/manual claim insertion remains supported, but only this path mints
        the proof required for independent corroboration.
        """
        if (claim.metadata or {}).get("excerpt_is_literal") is not True:
            return
        if not str(claim.source_artifact_id or "").strip():
            raise ValueError(
                "Literal evidence claims require a source artifact"
            )
        from evidence import EvidenceLake
        from source_pack_contract import verify_artifact_attestation
        from trust_attestations import sign_attestation

        lake = EvidenceLake(db_path=self.db_path)
        artifact = lake.get(str(claim.source_artifact_id))
        if not artifact:
            raise ValueError(
                "Literal claim source artifact is not present in the evidence lake"
            )
        if str(artifact.offering_id or "").strip() != str(
            claim.offering_id or ""
        ).strip():
            raise ValueError(
                "Literal claim and source artifact belong to different offerings"
            )
        artifact_record = artifact.to_attestation_record()
        if not verify_artifact_attestation(
            artifact_record,
            artifact.artifact_id,
        ):
            raise ValueError(
                "Literal claim source artifact has no valid capture attestation"
            )
        captured_text = lake.get_content(str(claim.source_artifact_id))
        excerpt = self._normalized_literal(claim.exact_excerpt)
        if not literal_claim_matches_excerpt(
            claim.claim_text,
            claim.exact_excerpt,
        ):
            raise ValueError(
                "Claim marked literal is not contained in its exact excerpt"
            )
        if (
            not excerpt
            or excerpt not in self._normalized_literal(captured_text)
        ):
            raise ValueError(
                "Claim marked literal is not present in the hash-verified "
                "captured artifact"
            )
        # The immutable capture—not caller-supplied claim metadata—controls
        # source authority.
        claim.source_class = (
            artifact.source_class.value
            if hasattr(artifact.source_class, "value")
            else str(artifact.source_class or "")
        )
        payload = claim_evidence_attestation_payload(
            claim,
            artifact_record,
            offering_id=claim.offering_id,
        )
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        claim.metadata["evidence_attestation"] = {
            "payload_sha256": hashlib.sha256(
                serialized.encode()
            ).hexdigest(),
            "payload": payload,
            "signature": sign_attestation(
                "claim-evidence-link",
                payload,
            ),
        }

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS claim_review_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id TEXT NOT NULL,
                    offering_id TEXT NOT NULL,
                    prior_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    claim_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    signature_json TEXT NOT NULL DEFAULT '{}',
                    key_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TRIGGER IF NOT EXISTS trg_claim_review_no_update
                    BEFORE UPDATE ON claim_review_events
                    BEGIN SELECT RAISE(
                        ABORT,
                        'claim_review_events is immutable'
                    ); END;
                CREATE TRIGGER IF NOT EXISTS trg_claim_review_no_delete
                    BEFORE DELETE ON claim_review_events
                    BEGIN SELECT RAISE(
                        ABORT,
                        'claim_review_events is immutable'
                    ); END;
            """)
            event_columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(claim_review_events)"
                )
            }
            for column, definition in (
                ("claim_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("signature_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("key_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in event_columns:
                    self._conn.execute(
                        f"ALTER TABLE claim_review_events "
                        f"ADD COLUMN {column} {definition}"
                    )
            self._conn.commit()
        return self._conn

    def add_claim(
        self,
        claim: Claim,
        *,
        attest_literal_evidence: bool = False,
    ) -> str:
        """Store a claim. Generates claim_id from content hash if not set.

        Returns the claim_id.
        """
        if not str(claim.claim_text or "").strip():
            raise ValueError("Claim text must be nonempty")
        if not str(claim.offering_id or "").strip():
            raise ValueError("Claim offering_id must be nonempty")
        if claim.review_status == ReviewStatus.ACCEPTED:
            raise ValueError(
                "Accepted claims must enter through update_review so the "
                "append-only review transition is attested"
            )
        if not claim.claim_id:
            hash_input = f"{claim.offering_id}:{claim.claim_text}:{claim.source_artifact_id}"
            claim.claim_id = hashlib.sha256(hash_input.encode()).hexdigest()[:32]
        if not claim.captured_at:
            claim.captured_at = datetime.now(timezone.utc).isoformat()
        if attest_literal_evidence:
            self._attest_literal_claim_from_capture(claim)

        with _claims_lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                existing = self.conn.execute(
                    "SELECT * FROM claims WHERE claim_id=?",
                    (claim.claim_id,),
                ).fetchone()
                if existing:
                    if not self._same_immutable_claim(existing, claim):
                        raise ValueError(
                            "Claim ID already belongs to different immutable "
                            "assertion material"
                        )
                else:
                    self.conn.execute("""
                        INSERT INTO claims (
                            claim_id, offering_id, claim_text, claim_type,
                            source_artifact_id, exact_excerpt, page_location,
                            captured_at, source_class, confidence,
                            extraction_method, effective_market, review_status,
                            reviewed_by, reviewed_at, conflicts_json,
                            metadata_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        claim.claim_id, claim.offering_id, claim.claim_text,
                        claim.claim_type.value, claim.source_artifact_id,
                        claim.exact_excerpt, claim.page_location,
                        claim.captured_at, claim.source_class, claim.confidence,
                        claim.extraction_method, claim.effective_market,
                        claim.review_status.value, claim.reviewed_by,
                        claim.reviewed_at, json.dumps(claim.conflicts),
                        json.dumps(claim.metadata),
                    ))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return claim.claim_id

    def add_claims_batch(
        self,
        claims: List[Claim],
        *,
        attest_literal_evidence: bool = False,
    ) -> List[str]:
        """Store multiple claims efficiently. Returns list of claim_ids."""
        for claim in claims:
            if not str(claim.claim_text or "").strip():
                raise ValueError("Claim text must be nonempty")
            if not str(claim.offering_id or "").strip():
                raise ValueError("Claim offering_id must be nonempty")
            if claim.review_status == ReviewStatus.ACCEPTED:
                raise ValueError(
                    "Accepted claims must enter through update_review so the "
                    "append-only review transition is attested"
                )
        for claim in claims:
            if not claim.claim_id:
                hash_input = (
                    f"{claim.offering_id}:{claim.claim_text}:"
                    f"{claim.source_artifact_id}"
                )
                claim.claim_id = hashlib.sha256(
                    hash_input.encode()
                ).hexdigest()[:32]
            if not claim.captured_at:
                claim.captured_at = datetime.now(timezone.utc).isoformat()
            if attest_literal_evidence:
                self._attest_literal_claim_from_capture(claim)

        ids = []
        with _claims_lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                for claim in claims:
                    existing = self.conn.execute(
                        "SELECT * FROM claims WHERE claim_id=?",
                        (claim.claim_id,),
                    ).fetchone()
                    if existing:
                        if not self._same_immutable_claim(existing, claim):
                            raise ValueError(
                                "Claim ID already belongs to different "
                                "immutable assertion material"
                            )
                    else:
                        self.conn.execute("""
                            INSERT INTO claims (
                                claim_id, offering_id, claim_text, claim_type,
                                source_artifact_id, exact_excerpt,
                                page_location, captured_at, source_class,
                                confidence, extraction_method,
                                effective_market, review_status, reviewed_by,
                                reviewed_at, conflicts_json, metadata_json
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (
                            claim.claim_id, claim.offering_id,
                            claim.claim_text, claim.claim_type.value,
                            claim.source_artifact_id, claim.exact_excerpt,
                            claim.page_location, claim.captured_at,
                            claim.source_class, claim.confidence,
                            claim.extraction_method, claim.effective_market,
                            claim.review_status.value, claim.reviewed_by,
                            claim.reviewed_at, json.dumps(claim.conflicts),
                            json.dumps(claim.metadata),
                        ))
                    ids.append(claim.claim_id)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return ids

    def update_evidence_metadata(
        self,
        claim_id: str,
        *,
        extraction_method: str,
        metadata_updates: dict,
    ) -> bool:
        """Update extraction evidence without mutating review disposition."""
        allowed = {
            "artifact_transcription_verified",
            "image_ocr",
        }
        updates = {
            str(key): value
            for key, value in (metadata_updates or {}).items()
            if str(key) in allowed
        }
        if not updates:
            return False
        with _claims_lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    """SELECT review_status, metadata_json
                    FROM claims WHERE claim_id=?""",
                    (claim_id,),
                ).fetchone()
                if not row:
                    self.conn.rollback()
                    return False
                if row["review_status"] == ReviewStatus.ACCEPTED.value:
                    raise ValueError(
                        "Accepted claim evidence is immutable; reopen the "
                        "claim to needs_verification before changing it"
                    )
                metadata = json.loads(row["metadata_json"] or "{}")
                metadata.pop("evidence_attestation", None)
                metadata.update(updates)
                cursor = self.conn.execute(
                    """UPDATE claims
                    SET extraction_method=?, metadata_json=?
                    WHERE claim_id=?""",
                    (
                        str(extraction_method or "").strip(),
                        json.dumps(metadata),
                        claim_id,
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return cursor.rowcount > 0

    def get_claim(self, claim_id: str) -> Optional[Claim]:
        """Retrieve a single claim by ID."""
        row = self.conn.execute(
            "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_claim(dict(row))

    def get_claims(self, offering_id: str,
                   claim_type: Optional[ClaimType] = None,
                   source_class: Optional[str] = None,
                   review_status: Optional[ReviewStatus] = None) -> List[Claim]:
        """Retrieve claims with optional filters."""
        if not str(offering_id or "").strip():
            raise ValueError("offering_id must be nonempty")
        query = "SELECT * FROM claims WHERE offering_id = ?"
        params: list = [offering_id]
        if claim_type:
            query += " AND claim_type = ?"
            params.append(claim_type.value)
        if source_class:
            query += " AND source_class = ?"
            params.append(source_class)
        if review_status:
            query += " AND review_status = ?"
            params.append(review_status.value)
        query += " ORDER BY captured_at ASC"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_claim(dict(r)) for r in rows]

    def get_latest_review_heads(
        self,
        offering_id: str,
        claim_ids=None,
    ) -> dict:
        """Return DB-current claims joined to their latest signed review event.

        This is the authority query used at seal time and immediately before a
        paid Workbench action. An embedded acceptance proof establishes that an
        acceptance occurred; this query establishes whether it is still the
        latest durable disposition.
        """
        offering_id = str(offering_id or "").strip()
        if not offering_id:
            raise ValueError("offering_id must be nonempty")
        requested = (
            {
                str(claim_id).strip()
                for claim_id in claim_ids
                if str(claim_id or "").strip()
            }
            if claim_ids is not None
            else None
        )
        if requested == set():
            return {}
        rows = self.conn.execute(
            """
            SELECT
                c.*,
                e.id AS review_event_id,
                e.claim_id AS review_event_claim_id,
                e.offering_id AS review_event_offering_id,
                e.prior_status AS review_prior_status,
                e.new_status AS review_new_status,
                e.reviewer AS review_event_reviewer,
                e.reviewed_at AS review_event_at,
                e.event_hash AS review_event_hash,
                e.claim_snapshot_json AS review_claim_snapshot_json,
                e.payload_json AS review_payload_json,
                e.signature_json AS review_signature_json,
                e.key_id AS review_key_id
            FROM claims AS c
            LEFT JOIN claim_review_events AS e
              ON e.id = (
                  SELECT MAX(latest.id)
                  FROM claim_review_events AS latest
                  WHERE latest.claim_id = c.claim_id
                    AND latest.offering_id = c.offering_id
              )
            WHERE c.offering_id = ?
            ORDER BY c.claim_id
            """,
            (offering_id,),
        ).fetchall()
        heads = {}
        for row in rows:
            record = dict(row)
            claim_id = str(record.get("claim_id") or "").strip()
            if requested is not None and claim_id not in requested:
                continue
            current_claim = self._row_to_claim(record)
            current_record = claim_publication_record(current_claim)
            current_status = current_claim.review_status.value
            current_snapshot_sha = claim_snapshot_hash(
                current_claim,
                offering_id=offering_id,
            )
            event_id = record.get("review_event_id")
            event = {}
            signature = {}
            snapshot = {}
            event_valid = False
            current_matches_event = False
            if event_id is not None:
                try:
                    event = json.loads(
                        record.get("review_payload_json") or "{}"
                    )
                    signature = json.loads(
                        record.get("review_signature_json") or "{}"
                    )
                    snapshot = json.loads(
                        record.get("review_claim_snapshot_json") or "{}"
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    event = {}
                    signature = {}
                    snapshot = {}
                if (
                    isinstance(event, dict)
                    and isinstance(signature, dict)
                    and isinstance(snapshot, dict)
                ):
                    snapshot_sha = claim_snapshot_hash(
                        snapshot,
                        offering_id=offering_id,
                    )
                    expected_event = review_attestation_payload(
                        claim_id=claim_id,
                        offering_id=offering_id,
                        prior_status=record.get("review_prior_status"),
                        new_status=record.get("review_new_status"),
                        reviewer=record.get("review_event_reviewer"),
                        reviewed_at=record.get("review_event_at"),
                        claim_snapshot_sha256=snapshot_sha,
                    )
                    event_hash = review_event_hash(event)
                    try:
                        from trust_attestations import verify_attestation
                        signature_valid = verify_attestation(
                            "claim-review-transition",
                            event,
                            signature,
                        )
                    except (ImportError, OSError, TypeError, ValueError):
                        signature_valid = False
                    event_valid = bool(
                        event == expected_event
                        and str(record.get("review_event_claim_id") or "")
                        == claim_id
                        and str(record.get("review_event_offering_id") or "")
                        == offering_id
                        and str(record.get("review_event_hash") or "")
                        == event_hash
                        and str(record.get("review_key_id") or "")
                        == str(signature.get("key_id") or "")
                        and signature_valid
                    )
                    current_matches_event = bool(
                        event_valid
                        and event.get("new_status") == current_status
                        and event.get("claim_snapshot_sha256")
                        == current_snapshot_sha
                    )
            proof = (current_claim.metadata or {}).get(
                "review_attestation"
            ) or {}
            proof_matches_latest = bool(
                event_id is not None
                and isinstance(proof, dict)
                and proof.get("event_id") == int(event_id)
                and proof.get("event_hash")
                == str(record.get("review_event_hash") or "")
                and proof.get("event") == event
                and proof.get("signature") == signature
            )
            authoritative_acceptance = bool(
                current_status == ReviewStatus.ACCEPTED.value
                and current_matches_event
                and proof_matches_latest
                and has_attested_human_acceptance(
                    current_record,
                    offering_id=offering_id,
                )
            )
            if event_id is None:
                head_valid = (
                    current_status != ReviewStatus.ACCEPTED.value
                )
            else:
                head_valid = current_matches_event
            heads[claim_id] = {
                "claim_id": claim_id,
                "offering_id": offering_id,
                "current_status": current_status,
                "current_claim_sha256": current_snapshot_sha,
                "latest_event_id": (
                    int(event_id) if event_id is not None else None
                ),
                "latest_event_hash": str(
                    record.get("review_event_hash") or ""
                ),
                "latest_event_status": str(
                    record.get("review_new_status") or ""
                ),
                "event_valid": event_valid,
                "current_matches_event": current_matches_event,
                "head_valid": head_valid,
                "authoritative_human_acceptance":
                    authoritative_acceptance,
                "current_claim": current_record,
            }
        return heads

    def update_review(self, claim_id: str, status: ReviewStatus,
                      reviewer: str = "system") -> bool:
        """Update the review status of a claim. Returns True if updated."""
        with _claims_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            current = self.conn.execute(
                "SELECT * FROM claims WHERE claim_id=?",
                (claim_id,),
            ).fetchone()
            if not current:
                self.conn.rollback()
                return False
            if (
                status == ReviewStatus.ACCEPTED
                and not is_human_reviewer(reviewer)
            ):
                self.conn.rollback()
                raise ValueError(
                    "Accepted review status requires a named human reviewer"
                )
            now = datetime.now(timezone.utc).isoformat()
            current_claim = self._row_to_claim(dict(current))
            claim_snapshot = canonical_claim_snapshot(
                current_claim,
                offering_id=current["offering_id"],
            )
            event = review_attestation_payload(
                claim_id=claim_id,
                offering_id=current["offering_id"],
                prior_status=current["review_status"],
                new_status=status.value,
                reviewer=reviewer,
                reviewed_at=now,
                claim_snapshot_sha256=claim_snapshot_hash(
                    current_claim,
                    offering_id=current["offering_id"],
                ),
            )
            event_hash = review_event_hash(event)
            try:
                from trust_attestations import sign_attestation
                event_signature = sign_attestation(
                    "claim-review-transition",
                    event,
                )
                event_cursor = self.conn.execute("""
                    INSERT INTO claim_review_events (
                        claim_id, offering_id, prior_status, new_status,
                        reviewer, reviewed_at, event_hash,
                        claim_snapshot_json, payload_json, signature_json,
                        key_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    claim_id,
                    current["offering_id"],
                    current["review_status"],
                    status.value,
                    str(reviewer or "").strip(),
                    now,
                    event_hash,
                    json.dumps(
                        claim_snapshot,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        event,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        event_signature,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    str(event_signature.get("key_id") or ""),
                ))
                metadata = json.loads(current["metadata_json"] or "{}")
                if status == ReviewStatus.ACCEPTED:
                    metadata["review_attestation"] = {
                        "event_id": int(event_cursor.lastrowid),
                        "event_hash": event_hash,
                        "event": event,
                        "signature": event_signature,
                    }
                else:
                    metadata.pop("review_attestation", None)
                cursor = self.conn.execute("""
                    UPDATE claims
                    SET review_status = ?, reviewed_by = ?, reviewed_at = ?,
                        metadata_json = ?
                    WHERE claim_id = ?
                """, (
                    status.value,
                    reviewer,
                    now,
                    json.dumps(metadata),
                    claim_id,
                ))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return cursor.rowcount > 0

    def detect_conflicts(self, offering_id: str) -> List[tuple]:
        """Find claims that conflict with each other.

        Returns list of (claim_a_id, claim_b_id, conflict_description) tuples.
        Currently detects:
        - Different amounts for the same ingredient from different sources
        - Different refund periods from different sources
        - Different pricing from different sources
        """
        claims = self.get_claims(offering_id)
        conflicts = []

        # Group by claim_type
        by_type: dict = {}
        for c in claims:
            by_type.setdefault(c.claim_type, []).append(c)

        # Ingredient amount conflicts
        if ClaimType.INGREDIENT_AMOUNT in by_type:
            by_ingredient: dict = {}
            for c in by_type[ClaimType.INGREDIENT_AMOUNT]:
                ing_name = c.metadata.get("ingredient_name", "").lower().strip()
                if ing_name:
                    by_ingredient.setdefault(ing_name, []).append(c)
            for ing, ing_claims in by_ingredient.items():
                if len(ing_claims) > 1:
                    amounts = set(c.metadata.get("amount", "") for c in ing_claims)
                    if len(amounts) > 1:
                        conflicts.append((
                            ing_claims[0].claim_id,
                            ing_claims[1].claim_id,
                            f"Conflicting amounts for {ing}: {amounts}"
                        ))

        # Refund policy conflicts
        if ClaimType.REFUND_POLICY in by_type:
            refund_claims = by_type[ClaimType.REFUND_POLICY]
            if len(refund_claims) > 1:
                durations = set(c.metadata.get("duration_days", "") for c in refund_claims)
                if len(durations) > 1:
                    conflicts.append((
                        refund_claims[0].claim_id,
                        refund_claims[1].claim_id,
                        f"Conflicting refund periods: {durations}"
                    ))

        # Pricing conflicts
        if ClaimType.PRICING in by_type:
            price_claims = by_type[ClaimType.PRICING]
            if len(price_claims) > 1:
                # Group by package name
                by_pkg: dict = {}
                for c in price_claims:
                    pkg = c.metadata.get("package", "").lower().strip()
                    if pkg:
                        by_pkg.setdefault(pkg, []).append(c)
                for pkg, pkg_claims in by_pkg.items():
                    if len(pkg_claims) > 1:
                        prices = set(c.metadata.get("price", "") for c in pkg_claims)
                        axes = [
                            _pricing_axis(
                                c.metadata.get("price", "")
                                or c.claim_text
                            )
                            for c in pkg_claims
                        ]
                        if (
                            len(axes) >= 2
                            and all(axes)
                            and len(set(axes)) == len(axes)
                        ):
                            # One API plan commonly has separate input,
                            # output, and cache rates. Older runs marked those
                            # complementary axes as conflicts. Heal that stale
                            # state while preserving any unrelated conflicts.
                            group_ids = {c.claim_id for c in pkg_claims}
                            with _claims_lock:
                                for claim in pkg_claims:
                                    row = self.conn.execute(
                                        "SELECT conflicts_json, review_status "
                                        "FROM claims WHERE claim_id = ?",
                                        (claim.claim_id,),
                                    ).fetchone()
                                    if not row:
                                        continue
                                    existing = json.loads(
                                        row["conflicts_json"] or "[]"
                                    )
                                    remaining = [
                                        claim_id for claim_id in existing
                                        if claim_id not in group_ids
                                    ]
                                    status = row["review_status"]
                                    if (
                                        not remaining
                                        and status
                                        == ReviewStatus.CONFLICTED.value
                                    ):
                                        status = (
                                            ReviewStatus.UNREVIEWED.value
                                        )
                                    self.conn.execute(
                                        "UPDATE claims SET conflicts_json = ?, "
                                        "review_status = ? WHERE claim_id = ?",
                                        (
                                            json.dumps(remaining),
                                            status,
                                            claim.claim_id,
                                        ),
                                    )
                                self.conn.commit()
                            continue
                        if len(prices) > 1:
                            conflicts.append((
                                pkg_claims[0].claim_id,
                                pkg_claims[1].claim_id,
                                f"Conflicting prices for {pkg}: {prices}"
                            ))

        # Singular structured facts from different artifacts must agree. This
        # catches stale-update conflicts outside ingredients/pricing/refunds
        # (for example warranty, device specifications, or power source).
        singular_fact_keys = {
            "serving_size", "servings_per_container", "specifications",
            "warranty", "power_source", "fda_clearance_status",
            "billing_frequency", "trial_period", "duration",
            "delivery_time", "shipping_policy", "certifications",
            "independent_testing",
        }
        by_fact_key = {}
        for claim in claims:
            if claim.review_status == ReviewStatus.REJECTED:
                continue
            fact_key = str(
                (claim.metadata or {}).get("fact_key") or ""
            ).strip()
            if fact_key in singular_fact_keys:
                by_fact_key.setdefault(fact_key, []).append(claim)
        existing_pairs = {
            frozenset((left, right))
            for left, right, _ in conflicts
        }
        for fact_key, fact_claims in by_fact_key.items():
            for index, first in enumerate(fact_claims):
                for second in fact_claims[index + 1:]:
                    if (
                        not first.source_artifact_id
                        or not second.source_artifact_id
                        or first.source_artifact_id == second.source_artifact_id
                    ):
                        continue
                    if (
                        " ".join(first.claim_text.casefold().split())
                        == " ".join(second.claim_text.casefold().split())
                    ):
                        continue
                    pair = frozenset((first.claim_id, second.claim_id))
                    if pair in existing_pairs:
                        continue
                    conflicts.append((
                        first.claim_id,
                        second.claim_id,
                        f"Conflicting values for {fact_key}: "
                        f"{first.claim_text!r} vs {second.claim_text!r}",
                    ))
                    existing_pairs.add(pair)

        # Mark conflicted claims
        for a_id, b_id, _ in conflicts:
            with _claims_lock:
                for cid, other_id in [(a_id, b_id), (b_id, a_id)]:
                    row = self.conn.execute(
                        "SELECT conflicts_json FROM claims WHERE claim_id = ?",
                        (cid,)
                    ).fetchone()
                    if row:
                        existing = json.loads(row["conflicts_json"] or "[]")
                        if other_id not in existing:
                            existing.append(other_id)
                            self.conn.execute(
                                "UPDATE claims SET conflicts_json = ?, review_status = ? WHERE claim_id = ?",
                                (json.dumps(existing), ReviewStatus.CONFLICTED.value, cid)
                            )
                self.conn.commit()

        return conflicts

    def build_evidence_edges(self, offering_id: str) -> dict:
        """Build evidence edges between claims from different sources.

        Returns a dict with:
        - conflicts: list of (claim_a_id, claim_b_id, description) — same fact, different values
        - corroborations: list of (claim_a_id, claim_b_id, description) — same fact, same values, different sources
        - isolated: list of claim_ids with no corroborating or conflicting evidence

        This is an extension of detect_conflicts() that also identifies
        corroborating evidence (same fact confirmed by multiple sources).
        """
        claims = self.get_claims(offering_id)
        conflicts = []
        corroborations = []
        linked_ids = set()

        # Group by claim_type
        by_type: dict = {}
        for c in claims:
            by_type.setdefault(c.claim_type, []).append(c)

        # Ingredient amounts: group by ingredient name, compare values + sources
        if ClaimType.INGREDIENT_AMOUNT in by_type:
            by_ingredient: dict = {}
            for c in by_type[ClaimType.INGREDIENT_AMOUNT]:
                ing_name = c.metadata.get("ingredient_name", "").lower().strip()
                if ing_name:
                    by_ingredient.setdefault(ing_name, []).append(c)
            for ing, ing_claims in by_ingredient.items():
                if len(ing_claims) > 1:
                    # Check if they're from different sources
                    source_groups: dict = {}
                    for c in ing_claims:
                        key = c.source_artifact_id or "no_source"
                        source_groups.setdefault(key, []).append(c)

                    if len(source_groups) > 1:
                        amounts = set(c.metadata.get("amount", "") for c in ing_claims)
                        pairs = list(source_groups.values())
                        for i in range(len(pairs)):
                            for j in range(i + 1, len(pairs)):
                                a, b = pairs[i][0], pairs[j][0]
                                a_amt = a.metadata.get("amount", "")
                                b_amt = b.metadata.get("amount", "")
                                if a_amt == b_amt:
                                    corroborations.append((
                                        a.claim_id, b.claim_id,
                                        f"Corroborated: {ing} = {a_amt} "
                                        f"(sources: {a.source_class}, {b.source_class})"
                                    ))
                                else:
                                    conflicts.append((
                                        a.claim_id, b.claim_id,
                                        f"Conflicting amounts for {ing}: "
                                        f"{a_amt} vs {b_amt}"
                                    ))
                                linked_ids.update([a.claim_id, b.claim_id])

        # Pricing: group by package, compare across sources
        if ClaimType.PRICING in by_type:
            by_pkg: dict = {}
            for c in by_type[ClaimType.PRICING]:
                pkg = c.metadata.get("package", "").lower().strip()
                if pkg:
                    by_pkg.setdefault(pkg, []).append(c)
            for pkg, pkg_claims in by_pkg.items():
                if len(pkg_claims) > 1:
                    source_groups: dict = {}
                    for c in pkg_claims:
                        key = c.source_artifact_id or "no_source"
                        source_groups.setdefault(key, []).append(c)
                    if len(source_groups) > 1:
                        pairs = list(source_groups.values())
                        for i in range(len(pairs)):
                            for j in range(i + 1, len(pairs)):
                                a, b = pairs[i][0], pairs[j][0]
                                a_price = a.metadata.get("price", "")
                                b_price = b.metadata.get("price", "")
                                if a_price == b_price:
                                    corroborations.append((
                                        a.claim_id, b.claim_id,
                                        f"Corroborated: {pkg} price = {a_price}"
                                    ))
                                else:
                                    conflicts.append((
                                        a.claim_id, b.claim_id,
                                        f"Conflicting prices for {pkg}: "
                                        f"{a_price} vs {b_price}"
                                    ))
                                linked_ids.update([a.claim_id, b.claim_id])

        # Identify isolated claims (no corroborating or conflicting evidence)
        all_ids = {c.claim_id for c in claims}
        isolated = list(all_ids - linked_ids)

        return {
            "conflicts": conflicts,
            "corroborations": corroborations,
            "isolated": isolated,
        }

    def update_review_status(self, claim_id: str,
                             status: ReviewStatus,
                             reviewer: str = "") -> bool:
        """Compatibility alias for the audited review transition path."""
        return self.update_review(claim_id, status, reviewer=reviewer)

    # Claim types where literal evidence from the artifact text is required.
    # Claims of these types that lack an exact excerpt are auto-flagged as
    # NEEDS_VERIFICATION so they can't silently enter the source pack.
    HIGH_RISK_CLAIM_TYPES = frozenset({
        ClaimType.HEALTH_BENEFIT,
        ClaimType.CLINICAL_RESULT,
        ClaimType.DRUG_INTERACTION,
        ClaimType.SAFETY_WARNING,
    })

    def get_unverified_high_risk(self, offering_id: str) -> List[Claim]:
        """Return high-risk claims that lack literal evidence.

        These are claims whose metadata shows excerpt_is_literal=False
        or whose review_status is NEEDS_VERIFICATION.
        """
        all_claims = self.get_claims(offering_id)
        results = []
        for c in all_claims:
            if c.claim_type not in self.HIGH_RISK_CLAIM_TYPES:
                continue
            is_literal = c.metadata.get("excerpt_is_literal", False)
            if (
                not is_literal
                or c.review_status in {
                    ReviewStatus.NEEDS_VERIFICATION,
                    ReviewStatus.AUTO_SUBSTITUTED,
                }
            ):
                results.append(c)
        return results

    # Mapping from intelligence-pack required_fact names to claim types
    # that satisfy them.  A required fact is "covered" when at least one
    # non-rejected claim of any matching type exists for the offering.
    REQUIRED_FACT_CLAIM_MAP: dict = {
        # Supplement facts
        "ingredients_with_amounts": {ClaimType.INGREDIENT_AMOUNT},
        "serving_size": {ClaimType.SERVING_INFO},
        "servings_per_container": {ClaimType.SERVING_INFO},
        "proprietary_blend_flag": {ClaimType.INGREDIENT_AMOUNT, ClaimType.MANUFACTURER_CLAIM},
        "other_ingredients": {ClaimType.INGREDIENT_FORM, ClaimType.INGREDIENT_AMOUNT},
        "allergens": {ClaimType.ALLERGEN},
        "manufacturer": {ClaimType.COMPANY_INFO},
        "country_of_manufacture": {ClaimType.COMPANY_INFO},
        # Topical
        "active_ingredients": {ClaimType.INGREDIENT_AMOUNT},
        "inactive_ingredients": {ClaimType.INGREDIENT_FORM},
        "application_method": {ClaimType.FEATURE},
        "warnings": {ClaimType.SAFETY_WARNING},
        "net_weight": {ClaimType.SPECIFICATION},
        # Device
        "key_features": {ClaimType.FEATURE},
        "specifications": {ClaimType.SPECIFICATION},
        "warranty": {ClaimType.MANUFACTURER_CLAIM},
        "fda_clearance_status": {ClaimType.REGULATORY_STATUS},
        "certifications": {ClaimType.CERTIFICATION},
        "power_source": {ClaimType.SPECIFICATION},
        # Telehealth
        "services_offered": {ClaimType.FEATURE},
        "pricing_tiers": {ClaimType.PRICING},
        "prescriber_credentials": {ClaimType.CERTIFICATION},
        "states_available": {ClaimType.FEATURE},
        "medications_offered": {ClaimType.FEATURE},
        "consultation_process": {ClaimType.FEATURE},
        # Info product
        "whats_included": {ClaimType.FEATURE},
        "format": {ClaimType.FEATURE, ClaimType.SPECIFICATION},
        "author_credentials": {ClaimType.CERTIFICATION},
        "access_method": {ClaimType.FEATURE},
        "pricing": {ClaimType.PRICING},
        # Financial
        "service_type": {ClaimType.FEATURE},
        "topics_covered": {ClaimType.FEATURE},
        "track_record_claims": {ClaimType.CLINICAL_RESULT, ClaimType.MANUFACTURER_CLAIM},
        "regulatory_registrations": {ClaimType.REGULATORY_STATUS},
        # Software
        "platform_support": {ClaimType.SPECIFICATION},
        "integrations": {ClaimType.FEATURE},
        "data_security": {ClaimType.FEATURE},
        "support_options": {ClaimType.FEATURE},
        # Service
        "service_description": {ClaimType.FEATURE},
        "service_area": {ClaimType.FEATURE},
        "credentials": {ClaimType.CERTIFICATION},
        "guarantees": {ClaimType.MANUFACTURER_CLAIM},
        # Food
        "nutrition_facts": {ClaimType.SERVING_INFO},
        "ingredients": {ClaimType.INGREDIENT_AMOUNT, ClaimType.INGREDIENT_FORM},
        # Cannabis
        "cannabinoid_profile": {ClaimType.INGREDIENT_AMOUNT},
        "terpene_profile": {ClaimType.INGREDIENT_AMOUNT},
        "thc_content": {ClaimType.INGREDIENT_AMOUNT},
        "cbd_content": {ClaimType.INGREDIENT_AMOUNT},
        "lab_results": {ClaimType.CERTIFICATION},
        "strain_type": {ClaimType.FEATURE},
        "consumption_method": {ClaimType.FEATURE},
        "state_availability": {ClaimType.FEATURE},
        # Research peptide
        "peptide_sequence": {ClaimType.SPECIFICATION},
        "purity_percentage": {ClaimType.SPECIFICATION},
        "molecular_weight": {ClaimType.SPECIFICATION},
        "cas_number": {ClaimType.SPECIFICATION},
        "form": {ClaimType.SPECIFICATION, ClaimType.FEATURE},
        "amount_per_vial": {ClaimType.SPECIFICATION},
        "storage_requirements": {ClaimType.SPECIFICATION},
        "research_use_only_disclaimer": {ClaimType.SAFETY_WARNING},
        # Program
        "program_structure": {ClaimType.FEATURE},
        "duration": {ClaimType.SPECIFICATION},
        "credentials_earned": {ClaimType.CERTIFICATION},
        "instructor_credentials": {ClaimType.CERTIFICATION},
        # Subscription
        "included_items": {ClaimType.FEATURE},
        "billing_frequency": {ClaimType.PRICING},
        "cancellation_policy": {ClaimType.REFUND_POLICY},
        "trial_period": {ClaimType.PRICING},
        # Professional
        "experience": {ClaimType.MANUFACTURER_CLAIM},
        "pricing_structure": {ClaimType.PRICING},
    }

    def check_required_facts(self, offering_id: str,
                             required_facts: List[str],
                             strict: bool = False) -> dict:
        """Check which required facts have supporting claims.

        Returns a dict with:
        - covered: list of fact names that have at least one matching claim
        - missing: list of fact names with no matching claims
        - provisional: list of fact names covered only by legacy broad-match
          (untagged claims matched by ClaimType, not explicit fact_key)
        - manual_only: list of fact names covered only by manual/unverified claims
        - coverage_ratio: float 0-1

        Only non-rejected claims count as coverage.

        When strict=True (used for mandatory fact enforcement):
        - Manual entries (NEEDS_VERIFICATION + no artifact) do NOT satisfy coverage
        - Legacy broad-match (provisional) does NOT satisfy coverage
        - Non-literal inferred claims (excerpt_is_literal=False) that haven't
          been explicitly ACCEPTED by a human do NOT satisfy coverage
        This prevents unverified or imprecise evidence from clearing mandatory gates.

        Matching priority:
        1. Exact fact_key match — claim.metadata["fact_key"] == fact_name
        2. Legacy fallback — broad ClaimType match via REQUIRED_FACT_CLAIM_MAP,
           but ONLY for claims that have no fact_key set (backward compat).
           These are marked provisional.
        """
        all_claims = self.get_claims(offering_id)
        active = [
            c for c in all_claims
            if c.review_status not in {
                ReviewStatus.REJECTED,
                ReviewStatus.CONFLICTED,
            }
        ]

        # Partition active claims into evidence-backed and manual/unverified
        evidence_backed = []
        manual_claims = []
        for c in active:
            is_manual = (
                c.extraction_method == "manual_entry"
                or c.metadata.get("manual_entry")
            )
            is_unverified_no_artifact = (
                c.review_status == ReviewStatus.NEEDS_VERIFICATION
                and not c.source_artifact_id
            )
            if is_manual or is_unverified_no_artifact:
                manual_claims.append(c)
            else:
                evidence_backed.append(c)

        # Index: fact_keys present in evidence-backed claims
        evidence_fact_keys: set = set()
        # Verified fact_keys: literal text, verified artifact transcription,
        # or explicit human acceptance. OCR of an immutable label image is a
        # transcription of the artifact—not an unsupported inference.
        verified_fact_keys: set = set()
        for c in evidence_backed:
            fk = c.metadata.get("fact_key")
            if fk:
                evidence_fact_keys.add(fk)
                is_literal = c.metadata.get("excerpt_is_literal", False)
                is_verified_transcription = bool(
                    c.source_artifact_id
                    and c.metadata.get("artifact_transcription_verified")
                    and c.extraction_method == "machine_ocr"
                )
                is_accepted = has_attested_human_acceptance(
                    c,
                    offering_id=offering_id,
                )
                if is_literal or is_verified_transcription or is_accepted:
                    verified_fact_keys.add(fk)

        # Index: fact_keys present in manual-only claims
        manual_fact_keys: set = set()
        for c in manual_claims:
            fk = c.metadata.get("fact_key")
            if fk:
                manual_fact_keys.add(fk)

        # Index: all fact_keys (for non-strict mode)
        all_fact_keys = evidence_fact_keys | manual_fact_keys

        covered = []
        missing = []
        provisional = []
        manual_only = []
        needs_review = []  # Inferred (non-literal) claims needing acceptance
        for fact_name in required_facts:
            if strict:
                # Strict: requires literal evidence OR explicit acceptance
                if fact_name in verified_fact_keys:
                    covered.append(fact_name)
                elif fact_name in evidence_fact_keys:
                    # Artifact-backed but non-literal and not accepted
                    missing.append(fact_name)
                    needs_review.append(fact_name)
                elif fact_name in manual_fact_keys:
                    # Manual entry exists but doesn't satisfy strict check
                    missing.append(fact_name)
                    manual_only.append(fact_name)
                else:
                    # Check legacy broad match — but only from evidence-backed
                    matching_types = self.REQUIRED_FACT_CLAIM_MAP.get(
                        fact_name, set()
                    )
                    has_legacy = any(
                        c.claim_type in matching_types
                        and not c.metadata.get("fact_key")
                        for c in evidence_backed
                    )
                    if has_legacy:
                        # Provisional doesn't satisfy strict either
                        missing.append(fact_name)
                        provisional.append(fact_name)
                    else:
                        missing.append(fact_name)
            else:
                # Non-strict: all fact_keys count, legacy fallback allowed
                if fact_name in all_fact_keys:
                    covered.append(fact_name)
                    if fact_name not in evidence_fact_keys:
                        manual_only.append(fact_name)
                    continue
                # Legacy broad match from any active claim
                matching_types = self.REQUIRED_FACT_CLAIM_MAP.get(
                    fact_name, set()
                )
                has_legacy = any(
                    c.claim_type in matching_types
                    and not c.metadata.get("fact_key")
                    for c in active
                )
                if has_legacy:
                    covered.append(fact_name)
                    provisional.append(fact_name)
                else:
                    missing.append(fact_name)

        total = len(required_facts)
        return {
            "covered": covered,
            "missing": missing,
            "provisional": provisional,
            "manual_only": manual_only,
            "needs_review": needs_review,
            "coverage_ratio": len(covered) / total if total else 1.0,
        }

    def count(self, offering_id: Optional[str] = None,
              review_status: Optional[ReviewStatus] = None) -> int:
        """Count claims with optional filters."""
        query = "SELECT COUNT(*) FROM claims WHERE 1=1"
        params: list = []
        if offering_id:
            query += " AND offering_id = ?"
            params.append(offering_id)
        if review_status:
            query += " AND review_status = ?"
            params.append(review_status.value)
        row = self.conn.execute(query, params).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _row_to_claim(d: dict) -> Claim:
        """Convert a database row dict to a Claim instance."""
        review_status = str(
            d.get("review_status", ReviewStatus.UNREVIEWED.value)
        )
        # Defense in depth for databases that have not yet run migration v7.
        if (
            review_status == ReviewStatus.ACCEPTED.value
            and str(d.get("reviewed_by") or "").strip().casefold()
            == "source-intelligence-automation"
        ):
            review_status = ReviewStatus.AUTO_SUBSTITUTED.value
        return Claim(
            claim_id=d["claim_id"],
            offering_id=d["offering_id"],
            claim_text=d["claim_text"],
            claim_type=ClaimType(d["claim_type"]),
            source_artifact_id=d.get("source_artifact_id"),
            exact_excerpt=d.get("exact_excerpt", ""),
            page_location=d.get("page_location", ""),
            captured_at=d.get("captured_at", ""),
            source_class=d.get("source_class", ""),
            confidence=d.get("confidence", 0.0),
            extraction_method=d.get("extraction_method", ""),
            effective_market=d.get("effective_market", "US"),
            review_status=ReviewStatus(review_status),
            reviewed_by=d.get("reviewed_by"),
            reviewed_at=d.get("reviewed_at"),
            conflicts=json.loads(d.get("conflicts_json", "[]")),
            metadata=json.loads(d.get("metadata_json", "{}")),
        )
