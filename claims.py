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
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List


_claims_lock = threading.Lock()


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
    """Human review status of a claim."""
    UNREVIEWED = "unreviewed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"
    NEEDS_VERIFICATION = "needs_verification"


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

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def add_claim(self, claim: Claim) -> str:
        """Store a claim. Generates claim_id from content hash if not set.

        Returns the claim_id.
        """
        if not claim.claim_id:
            hash_input = f"{claim.offering_id}:{claim.claim_text}:{claim.source_artifact_id}"
            claim.claim_id = hashlib.sha256(hash_input.encode()).hexdigest()[:32]
        if not claim.captured_at:
            claim.captured_at = datetime.now(timezone.utc).isoformat()

        with _claims_lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO claims (
                    claim_id, offering_id, claim_text, claim_type,
                    source_artifact_id, exact_excerpt, page_location,
                    captured_at, source_class, confidence, extraction_method,
                    effective_market, review_status, reviewed_by, reviewed_at,
                    conflicts_json, metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                claim.claim_id, claim.offering_id, claim.claim_text,
                claim.claim_type.value, claim.source_artifact_id,
                claim.exact_excerpt, claim.page_location,
                claim.captured_at, claim.source_class, claim.confidence,
                claim.extraction_method, claim.effective_market,
                claim.review_status.value, claim.reviewed_by, claim.reviewed_at,
                json.dumps(claim.conflicts), json.dumps(claim.metadata),
            ))
            self.conn.commit()
        return claim.claim_id

    def add_claims_batch(self, claims: List[Claim]) -> List[str]:
        """Store multiple claims efficiently. Returns list of claim_ids."""
        ids = []
        with _claims_lock:
            for claim in claims:
                if not claim.claim_id:
                    hash_input = f"{claim.offering_id}:{claim.claim_text}:{claim.source_artifact_id}"
                    claim.claim_id = hashlib.sha256(hash_input.encode()).hexdigest()[:32]
                if not claim.captured_at:
                    claim.captured_at = datetime.now(timezone.utc).isoformat()

                self.conn.execute("""
                    INSERT OR REPLACE INTO claims (
                        claim_id, offering_id, claim_text, claim_type,
                        source_artifact_id, exact_excerpt, page_location,
                        captured_at, source_class, confidence, extraction_method,
                        effective_market, review_status, reviewed_by, reviewed_at,
                        conflicts_json, metadata_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    claim.claim_id, claim.offering_id, claim.claim_text,
                    claim.claim_type.value, claim.source_artifact_id,
                    claim.exact_excerpt, claim.page_location,
                    claim.captured_at, claim.source_class, claim.confidence,
                    claim.extraction_method, claim.effective_market,
                    claim.review_status.value, claim.reviewed_by, claim.reviewed_at,
                    json.dumps(claim.conflicts), json.dumps(claim.metadata),
                ))
                ids.append(claim.claim_id)
            self.conn.commit()
        return ids

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

    def update_review(self, claim_id: str, status: ReviewStatus,
                      reviewer: str = "system") -> bool:
        """Update the review status of a claim. Returns True if updated."""
        now = datetime.now(timezone.utc).isoformat()
        with _claims_lock:
            cursor = self.conn.execute("""
                UPDATE claims SET review_status = ?, reviewed_by = ?, reviewed_at = ?
                WHERE claim_id = ?
            """, (status.value, reviewer, now, claim_id))
            self.conn.commit()
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
                        if len(prices) > 1:
                            conflicts.append((
                                pkg_claims[0].claim_id,
                                pkg_claims[1].claim_id,
                                f"Conflicting prices for {pkg}: {prices}"
                            ))

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
        """Update the review status of a specific claim.

        Returns True if the claim was found and updated.
        """
        now = datetime.now(timezone.utc).isoformat()
        with _claims_lock:
            cursor = self.conn.execute(
                "UPDATE claims SET review_status = ?, reviewed_by = ?, "
                "reviewed_at = ? WHERE claim_id = ?",
                (status.value, reviewer, now, claim_id)
            )
            self.conn.commit()
        return cursor.rowcount > 0

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
            if not is_literal or c.review_status == ReviewStatus.NEEDS_VERIFICATION:
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
        active = [c for c in all_claims
                  if c.review_status != ReviewStatus.REJECTED]

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
                is_accepted = (c.review_status == ReviewStatus.ACCEPTED)
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
            review_status=ReviewStatus(d.get("review_status", "unreviewed")),
            reviewed_by=d.get("reviewed_by"),
            reviewed_at=d.get("reviewed_at"),
            conflicts=json.loads(d.get("conflicts_json", "[]")),
            metadata=json.loads(d.get("metadata_json", "{}")),
        )
