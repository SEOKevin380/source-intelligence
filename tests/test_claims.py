"""Tests for claims.py — Atomic fact/claim ledger."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus


@pytest.fixture
def ledger():
    """Create a temporary claims ledger for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Create the claims table manually (normally done by database.py migration v3)
    import sqlite3
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS claims (
            claim_id TEXT PRIMARY KEY,
            offering_id TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            claim_type TEXT NOT NULL,
            source_artifact_id TEXT,
            exact_excerpt TEXT DEFAULT '',
            page_location TEXT DEFAULT '',
            captured_at TEXT NOT NULL,
            source_class TEXT DEFAULT '',
            confidence REAL DEFAULT 0.0,
            extraction_method TEXT DEFAULT 'manual',
            effective_market TEXT DEFAULT 'US',
            review_status TEXT DEFAULT 'unreviewed',
            reviewed_by TEXT,
            reviewed_at TEXT,
            conflicts_json TEXT DEFAULT '[]',
            metadata_json TEXT DEFAULT '{}'
        );
    """)
    conn.commit()
    conn.close()

    lg = ClaimsLedger(db_path=path)
    yield lg
    if lg._conn:
        lg._conn.close()
    os.unlink(path)


class TestClaim:
    def test_creation(self):
        c = Claim(
            offering_id="test-offering",
            claim_text="Contains 500mg Berberine",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
        )
        assert c.claim_text == "Contains 500mg Berberine"
        assert c.review_status == ReviewStatus.UNREVIEWED

    def test_claim_types(self):
        assert ClaimType.INGREDIENT_AMOUNT.value == "ingredient_amount"
        assert ClaimType.SAFETY_WARNING.value == "safety_warning"
        assert ClaimType.DRUG_INTERACTION.value == "drug_interaction"


class TestClaimsLedger:
    def test_add_and_get_claim(self, ledger):
        claim = Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg per serving",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_class="official_vendor",
            confidence=0.9,
        )
        cid = ledger.add_claim(claim)
        assert cid  # Non-empty

        retrieved = ledger.get_claim(cid)
        assert retrieved is not None
        assert retrieved.claim_text == "Berberine 500mg per serving"
        assert retrieved.claim_type == ClaimType.INGREDIENT_AMOUNT

    def test_add_claims_batch(self, ledger):
        claims = [
            Claim(
                offering_id="test-1",
                claim_text=f"Ingredient {i}: {i*100}mg",
                claim_type=ClaimType.INGREDIENT_AMOUNT,
            )
            for i in range(5)
        ]
        ids = ledger.add_claims_batch(claims)
        assert len(ids) == 5

    def test_get_claims_filtered(self, ledger):
        # Add mixed claim types
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
        ))
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="60-day refund",
            claim_type=ClaimType.REFUND_POLICY,
        ))
        ledger.add_claim(Claim(
            offering_id="test-2",
            claim_text="Other product claim",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
        ))

        # Filter by offering
        results = ledger.get_claims("test-1")
        assert len(results) == 2

        # Filter by type
        results = ledger.get_claims("test-1", claim_type=ClaimType.INGREDIENT_AMOUNT)
        assert len(results) == 1

    def test_update_review(self, ledger):
        claim = Claim(
            offering_id="test-1",
            claim_text="Test claim",
            claim_type=ClaimType.MANUFACTURER_CLAIM,
        )
        cid = ledger.add_claim(claim)

        success = ledger.update_review(cid, ReviewStatus.ACCEPTED, reviewer="human")
        assert success is True

        updated = ledger.get_claim(cid)
        assert updated.review_status == ReviewStatus.ACCEPTED
        assert updated.reviewed_by == "human"

    def test_detect_conflicts_ingredient_amounts(self, ledger):
        """Two different amounts for the same ingredient = conflict."""
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_class="official_vendor",
            metadata={"ingredient_name": "Berberine", "amount": "500mg"},
        ))
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 1000mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_class="third_party",
            metadata={"ingredient_name": "Berberine", "amount": "1000mg"},
        ))

        conflicts = ledger.detect_conflicts("test-1")
        assert len(conflicts) >= 1
        assert "Berberine" in conflicts[0][2].lower() or "berberine" in conflicts[0][2]

    def test_no_conflict_same_amounts(self, ledger):
        """Same amount from different sources = no conflict."""
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            metadata={"ingredient_name": "Berberine", "amount": "500mg"},
        ))
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg confirmed",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            metadata={"ingredient_name": "Berberine", "amount": "500mg"},
        ))

        conflicts = ledger.detect_conflicts("test-1")
        assert len(conflicts) == 0

    def test_count(self, ledger):
        assert ledger.count() == 0
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Test",
            claim_type=ClaimType.MANUFACTURER_CLAIM,
        ))
        assert ledger.count() == 1
        assert ledger.count(offering_id="test-1") == 1
        assert ledger.count(offering_id="nonexistent") == 0

    def test_get_nonexistent(self, ledger):
        assert ledger.get_claim("nonexistent") is None

    def test_update_review_status(self, ledger):
        claim = Claim(
            offering_id="test-1",
            claim_text="Claim for status update",
            claim_type=ClaimType.MANUFACTURER_CLAIM,
        )
        cid = ledger.add_claim(claim)

        success = ledger.update_review_status(cid, ReviewStatus.REJECTED, reviewer="qa")
        assert success is True
        updated = ledger.get_claim(cid)
        assert updated.review_status == ReviewStatus.REJECTED

    def test_update_review_status_nonexistent(self, ledger):
        success = ledger.update_review_status("nonexistent", ReviewStatus.ACCEPTED)
        assert success is False


class TestBuildEvidenceEdges:
    def test_corroboration_same_ingredient_same_amount_different_sources(self, ledger):
        """Same ingredient + same amount from different artifacts = corroboration."""
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-A",
            source_class="official_vendor",
            metadata={"ingredient_name": "Berberine", "amount": "500mg"},
        ))
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg confirmed by lab",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-B",
            source_class="independent_lab",
            metadata={"ingredient_name": "Berberine", "amount": "500mg"},
        ))

        edges = ledger.build_evidence_edges("test-1")
        assert len(edges["corroborations"]) == 1
        assert len(edges["conflicts"]) == 0
        assert "Corroborated" in edges["corroborations"][0][2]
        assert "berberine" in edges["corroborations"][0][2].lower()

    def test_conflict_same_ingredient_different_amounts_different_sources(self, ledger):
        """Same ingredient + different amounts from different artifacts = conflict."""
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Vitamin D 1000IU",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-A",
            source_class="official_vendor",
            metadata={"ingredient_name": "Vitamin D", "amount": "1000IU"},
        ))
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Vitamin D 2000IU",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-B",
            source_class="third_party",
            metadata={"ingredient_name": "Vitamin D", "amount": "2000IU"},
        ))

        edges = ledger.build_evidence_edges("test-1")
        assert len(edges["conflicts"]) == 1
        assert len(edges["corroborations"]) == 0
        assert "1000IU" in edges["conflicts"][0][2]
        assert "2000IU" in edges["conflicts"][0][2]

    def test_isolated_claims_have_no_edges(self, ledger):
        """Claims with no matching ingredient/package from other sources are isolated."""
        cid = ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Zinc 15mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-A",
            metadata={"ingredient_name": "Zinc", "amount": "15mg"},
        ))

        edges = ledger.build_evidence_edges("test-1")
        assert len(edges["conflicts"]) == 0
        assert len(edges["corroborations"]) == 0
        assert cid in edges["isolated"]

    def test_same_source_not_corroboration(self, ledger):
        """Two claims from the SAME artifact are not corroboration."""
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg (label)",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-A",
            metadata={"ingredient_name": "Berberine", "amount": "500mg"},
        ))
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg (description)",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-A",
            metadata={"ingredient_name": "Berberine", "amount": "500mg"},
        ))

        edges = ledger.build_evidence_edges("test-1")
        assert len(edges["corroborations"]) == 0
        assert len(edges["conflicts"]) == 0

    def test_pricing_corroboration(self, ledger):
        """Same price for same package from different sources = corroboration."""
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="1 Bottle: $49.95",
            claim_type=ClaimType.PRICING,
            source_artifact_id="artifact-A",
            metadata={"package": "1 Bottle", "price": "$49.95"},
        ))
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="1 Bottle costs $49.95",
            claim_type=ClaimType.PRICING,
            source_artifact_id="artifact-B",
            metadata={"package": "1 Bottle", "price": "$49.95"},
        ))

        edges = ledger.build_evidence_edges("test-1")
        assert len(edges["corroborations"]) == 1
        assert "1 bottle" in edges["corroborations"][0][2].lower()

    def test_pricing_conflict(self, ledger):
        """Different prices for same package from different sources = conflict."""
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="1 Bottle: $49.95",
            claim_type=ClaimType.PRICING,
            source_artifact_id="artifact-A",
            metadata={"package": "1 Bottle", "price": "$49.95"},
        ))
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="1 Bottle: $59.95",
            claim_type=ClaimType.PRICING,
            source_artifact_id="artifact-B",
            metadata={"package": "1 Bottle", "price": "$59.95"},
        ))

        edges = ledger.build_evidence_edges("test-1")
        assert len(edges["conflicts"]) == 1
        assert "$49.95" in edges["conflicts"][0][2]
        assert "$59.95" in edges["conflicts"][0][2]

    def test_mixed_corroboration_conflict_and_isolated(self, ledger):
        """A realistic mix: one corroborated, one conflicted, one isolated."""
        # Corroborated: Berberine 500mg from 2 sources
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-A",
            metadata={"ingredient_name": "Berberine", "amount": "500mg"},
        ))
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Berberine 500mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-B",
            metadata={"ingredient_name": "Berberine", "amount": "500mg"},
        ))
        # Conflicted: Chromium with different amounts
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Chromium 200mcg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-A",
            metadata={"ingredient_name": "Chromium", "amount": "200mcg"},
        ))
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Chromium 400mcg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-B",
            metadata={"ingredient_name": "Chromium", "amount": "400mcg"},
        ))
        # Isolated: Zinc only from one source
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Zinc 15mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-A",
            metadata={"ingredient_name": "Zinc", "amount": "15mg"},
        ))
        # Isolated: manufacturer claim (not ingredient/pricing type)
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Supports blood sugar levels",
            claim_type=ClaimType.MANUFACTURER_CLAIM,
        ))

        edges = ledger.build_evidence_edges("test-1")
        assert len(edges["corroborations"]) == 1
        assert len(edges["conflicts"]) == 1
        assert len(edges["isolated"]) >= 2  # Zinc + manufacturer claim

    def test_empty_offering_returns_empty_edges(self, ledger):
        """No claims = empty edge results."""
        edges = ledger.build_evidence_edges("nonexistent")
        assert edges["conflicts"] == []
        assert edges["corroborations"] == []
        assert edges["isolated"] == []

    def test_three_sources_corroborating(self, ledger):
        """Three sources all agreeing creates multiple corroboration edges."""
        for src in ["artifact-A", "artifact-B", "artifact-C"]:
            ledger.add_claim(Claim(
                offering_id="test-1",
                claim_text=f"Berberine 500mg from {src}",
                claim_type=ClaimType.INGREDIENT_AMOUNT,
                source_artifact_id=src,
                metadata={"ingredient_name": "Berberine", "amount": "500mg"},
            ))

        edges = ledger.build_evidence_edges("test-1")
        # 3 source groups → C(3,2) = 3 corroboration pairs
        assert len(edges["corroborations"]) == 3
        assert len(edges["conflicts"]) == 0
        assert len(edges["isolated"]) == 0


class TestHighRiskClaimTypes:
    """Tests for HIGH_RISK_CLAIM_TYPES and get_unverified_high_risk()."""

    def test_high_risk_types_defined(self, ledger):
        """HIGH_RISK_CLAIM_TYPES must include health-sensitive claim types."""
        from claims import ClaimsLedger
        hr = ClaimsLedger.HIGH_RISK_CLAIM_TYPES
        assert ClaimType.HEALTH_BENEFIT in hr
        assert ClaimType.CLINICAL_RESULT in hr
        assert ClaimType.DRUG_INTERACTION in hr
        assert ClaimType.SAFETY_WARNING in hr
        # Non-high-risk types should NOT be in the set
        assert ClaimType.PRICING not in hr
        assert ClaimType.INGREDIENT_AMOUNT not in hr

    def test_get_unverified_high_risk_returns_unverified(self, ledger):
        """Claims without literal evidence should be returned."""
        # Add a health benefit claim WITHOUT literal evidence
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Cures all disease",
            claim_type=ClaimType.HEALTH_BENEFIT,
            review_status=ReviewStatus.NEEDS_VERIFICATION,
            metadata={"excerpt_is_literal": False},
        ))
        # Add a health benefit claim WITH literal evidence
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="May support immune function",
            claim_type=ClaimType.HEALTH_BENEFIT,
            review_status=ReviewStatus.UNREVIEWED,
            metadata={"excerpt_is_literal": True},
        ))
        # Add a non-high-risk claim without evidence (should be ignored)
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Price: $49.99",
            claim_type=ClaimType.PRICING,
            review_status=ReviewStatus.NEEDS_VERIFICATION,
            metadata={"excerpt_is_literal": False},
        ))

        unverified = ledger.get_unverified_high_risk("test-1")
        assert len(unverified) == 1
        assert unverified[0].claim_text == "Cures all disease"

    def test_get_unverified_high_risk_empty_when_all_verified(self, ledger):
        """No results when all high-risk claims have literal evidence."""
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="May help with joint pain",
            claim_type=ClaimType.HEALTH_BENEFIT,
            metadata={"excerpt_is_literal": True},
        ))
        unverified = ledger.get_unverified_high_risk("test-1")
        assert len(unverified) == 0

    def test_get_unverified_catches_needs_verification_status(self, ledger):
        """Claims with NEEDS_VERIFICATION review status should be returned
        even if excerpt_is_literal metadata is missing."""
        ledger.add_claim(Claim(
            offering_id="test-1",
            claim_text="Interacts with statins",
            claim_type=ClaimType.DRUG_INTERACTION,
            review_status=ReviewStatus.NEEDS_VERIFICATION,
            metadata={},  # No excerpt_is_literal key
        ))
        unverified = ledger.get_unverified_high_risk("test-1")
        assert len(unverified) == 1


class TestRequiredFactsCoverage:
    """Tests for check_required_facts — verifying intelligence-pack fact coverage."""

    def test_all_facts_covered(self, ledger):
        """When all required facts have matching claims, missing is empty."""
        oid = "req-test-1"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Vitamin C 500mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
        ))
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="1 capsule per serving",
            claim_type=ClaimType.SERVING_INFO,
        ))
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Contains milk",
            claim_type=ClaimType.ALLERGEN,
        ))
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Made by NutraLab Inc",
            claim_type=ClaimType.COMPANY_INFO,
        ))

        result = ledger.check_required_facts(oid, [
            "ingredients_with_amounts", "serving_size",
            "allergens", "manufacturer",
        ])
        assert result["missing"] == []
        assert len(result["covered"]) == 4
        assert result["coverage_ratio"] == 1.0

    def test_missing_facts_detected(self, ledger):
        """Facts without matching claims appear in missing list."""
        oid = "req-test-2"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Zinc 15mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
        ))

        result = ledger.check_required_facts(oid, [
            "ingredients_with_amounts", "serving_size",
            "allergens", "manufacturer",
        ])
        assert "ingredients_with_amounts" in result["covered"]
        assert "serving_size" in result["missing"]
        assert "allergens" in result["missing"]
        assert "manufacturer" in result["missing"]
        assert result["coverage_ratio"] == 0.25

    def test_rejected_claims_do_not_count(self, ledger):
        """Rejected claims must not satisfy required facts."""
        oid = "req-test-3"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Serving: 2 capsules",
            claim_type=ClaimType.SERVING_INFO,
            review_status=ReviewStatus.REJECTED,
        ))

        result = ledger.check_required_facts(oid, ["serving_size"])
        assert "serving_size" in result["missing"]
        assert result["coverage_ratio"] == 0.0

    def test_empty_required_facts_returns_full_coverage(self, ledger):
        """Empty required facts list means 100% coverage."""
        result = ledger.check_required_facts("any-id", [])
        assert result["coverage_ratio"] == 1.0
        assert result["missing"] == []

    def test_unknown_fact_name_always_missing(self, ledger):
        """Fact names not in REQUIRED_FACT_CLAIM_MAP are always missing."""
        oid = "req-test-4"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Something",
            claim_type=ClaimType.FEATURE,
        ))
        result = ledger.check_required_facts(oid, ["nonexistent_fact"])
        assert "nonexistent_fact" in result["missing"]

    # --- fact_key precision tests ---

    def test_serving_size_does_not_satisfy_servings_per_container(self, ledger):
        """A SERVING_INFO with fact_key='serving_size' must NOT cover
        'servings_per_container'. This was the core false-positive bug."""
        oid = "fk-precision-1"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Serving size: 2 capsules",
            claim_type=ClaimType.SERVING_INFO,
            metadata={"fact_key": "serving_size"},
        ))
        result = ledger.check_required_facts(oid, [
            "serving_size", "servings_per_container",
        ])
        assert "serving_size" in result["covered"]
        assert "servings_per_container" in result["missing"]
        assert result["coverage_ratio"] == 0.5

    def test_manufacturer_does_not_satisfy_country(self, ledger):
        """A COMPANY_INFO with fact_key='manufacturer' must NOT cover
        'country_of_manufacture'."""
        oid = "fk-precision-2"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="NutraLab Inc",
            claim_type=ClaimType.COMPANY_INFO,
            metadata={"fact_key": "manufacturer"},
        ))
        result = ledger.check_required_facts(oid, [
            "manufacturer", "country_of_manufacture",
        ])
        assert "manufacturer" in result["covered"]
        assert "country_of_manufacture" in result["missing"]

    def test_both_fact_keys_present_covers_both(self, ledger):
        """When both serving_size and servings_per_container fact_keys exist,
        both required facts are covered."""
        oid = "fk-precision-3"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Serving size: 1 scoop",
            claim_type=ClaimType.SERVING_INFO,
            metadata={"fact_key": "serving_size"},
        ))
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="30 servings per container",
            claim_type=ClaimType.SERVING_INFO,
            metadata={"fact_key": "servings_per_container"},
        ))
        result = ledger.check_required_facts(oid, [
            "serving_size", "servings_per_container",
        ])
        assert result["missing"] == []
        assert result["coverage_ratio"] == 1.0

    def test_legacy_claim_without_fact_key_uses_broad_match(self, ledger):
        """Claims without a fact_key (legacy) fall back to broad ClaimType
        matching via REQUIRED_FACT_CLAIM_MAP — backward compatibility.
        These are marked as provisional coverage."""
        oid = "fk-legacy-1"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Serving: 2 caps",
            claim_type=ClaimType.SERVING_INFO,
            # No fact_key in metadata — legacy claim
        ))
        result = ledger.check_required_facts(oid, [
            "serving_size", "servings_per_container",
        ])
        # Legacy broad match: SERVING_INFO covers both (backward compat)
        assert "serving_size" in result["covered"]
        assert "servings_per_container" in result["covered"]
        # But both are provisional — inferred, not precisely tagged
        assert "serving_size" in result["provisional"]
        assert "servings_per_container" in result["provisional"]

    def test_tagged_claim_does_not_trigger_broad_match(self, ledger):
        """A SERVING_INFO claim WITH fact_key='serving_size' must not satisfy
        'servings_per_container' via broad type matching — the fact_key
        takes it out of the legacy pool."""
        oid = "fk-tagged-1"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Serving: 1 tablet",
            claim_type=ClaimType.SERVING_INFO,
            metadata={"fact_key": "serving_size"},
        ))
        result = ledger.check_required_facts(oid, ["servings_per_container"])
        # The only SERVING_INFO claim has a fact_key, so it's not legacy
        # → broad match fails → servings_per_container is missing
        assert "servings_per_container" in result["missing"]

    def test_exact_fact_key_match_is_not_provisional(self, ledger):
        """Claims with exact fact_key match are definitive coverage —
        they must NOT appear in the provisional list."""
        oid = "fk-definitive-1"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Serving size: 2 capsules",
            claim_type=ClaimType.SERVING_INFO,
            metadata={"fact_key": "serving_size"},
        ))
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Zinc: 30mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            metadata={"fact_key": "ingredients_with_amounts",
                      "ingredient_name": "Zinc"},
        ))
        result = ledger.check_required_facts(oid, [
            "serving_size", "ingredients_with_amounts",
        ])
        assert result["covered"] == ["serving_size", "ingredients_with_amounts"]
        assert result["provisional"] == []
        assert result["missing"] == []

    def test_mixed_exact_and_legacy_coverage(self, ledger):
        """When some facts have exact fact_key and others use legacy broad
        match, only the legacy ones should be provisional."""
        oid = "fk-mixed-1"
        # Exact match for serving_size
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Serving size: 1 scoop",
            claim_type=ClaimType.SERVING_INFO,
            metadata={"fact_key": "serving_size"},
        ))
        # Legacy claim (no fact_key) — manufacturer info
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Made by AcmeCo",
            claim_type=ClaimType.COMPANY_INFO,
            # No fact_key → legacy
        ))
        result = ledger.check_required_facts(oid, [
            "serving_size", "manufacturer",
        ])
        assert "serving_size" in result["covered"]
        assert "manufacturer" in result["covered"]
        # Only manufacturer is provisional (legacy broad match)
        assert "manufacturer" in result["provisional"]
        assert "serving_size" not in result["provisional"]

    def test_rejected_fact_key_claim_does_not_count(self, ledger):
        """A claim with the right fact_key but REJECTED status must not
        satisfy the required fact."""
        oid = "fk-rejected-1"
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Serving size: 3 pills",
            claim_type=ClaimType.SERVING_INFO,
            review_status=ReviewStatus.REJECTED,
            metadata={"fact_key": "serving_size"},
        ))
        result = ledger.check_required_facts(oid, ["serving_size"])
        assert "serving_size" in result["missing"]

    def test_strict_mode_blocks_manual_only_claims(self, ledger):
        """In strict mode, manual entries (NEEDS_VERIFICATION + no artifact)
        do NOT satisfy mandatory fact coverage."""
        oid = "strict-manual-1"
        # Manual entry — has fact_key but no artifact, NEEDS_VERIFICATION
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Contains 500mg Vitamin C",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id=None,
            review_status=ReviewStatus.NEEDS_VERIFICATION,
            extraction_method="manual_entry",
            metadata={"fact_key": "ingredients_with_amounts",
                       "manual_entry": True},
        ))
        # Non-strict: counts as covered (but manual_only)
        result = ledger.check_required_facts(
            oid, ["ingredients_with_amounts"], strict=False
        )
        assert "ingredients_with_amounts" in result["covered"]
        assert "ingredients_with_amounts" in result["manual_only"]

        # Strict: NOT covered — remains missing
        result_strict = ledger.check_required_facts(
            oid, ["ingredients_with_amounts"], strict=True
        )
        assert "ingredients_with_amounts" in result_strict["missing"]
        assert "ingredients_with_amounts" in result_strict["manual_only"]

    def test_strict_mode_blocks_provisional_legacy_claims(self, ledger):
        """In strict mode, provisional (legacy broad-match) claims
        do NOT satisfy mandatory fact coverage."""
        oid = "strict-prov-1"
        # Legacy claim — matches INGREDIENT_AMOUNT type but no fact_key
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Zinc 30mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="art-xyz",
            # No fact_key → legacy broad match
        ))
        # Non-strict: counts as covered (provisional)
        result = ledger.check_required_facts(
            oid, ["ingredients_with_amounts"], strict=False
        )
        assert "ingredients_with_amounts" in result["covered"]
        assert "ingredients_with_amounts" in result["provisional"]

        # Strict: NOT covered — remains missing as provisional
        result_strict = ledger.check_required_facts(
            oid, ["ingredients_with_amounts"], strict=True
        )
        assert "ingredients_with_amounts" in result_strict["missing"]
        assert "ingredients_with_amounts" in result_strict["provisional"]

    def test_strict_mode_accepts_evidence_backed_claims(self, ledger):
        """In strict mode, evidence-backed claims with exact fact_key
        DO satisfy mandatory fact coverage."""
        oid = "strict-ok-1"
        # Evidence-backed claim with fact_key and artifact
        ledger.add_claim(Claim(
            offering_id=oid, claim_text="Vitamin D 2000 IU",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="art-evidence-1",
            review_status=ReviewStatus.ACCEPTED,
            extraction_method="llm_extraction",
            metadata={"fact_key": "ingredients_with_amounts"},
        ))
        # Both strict and non-strict: covered
        for strict in (True, False):
            result = ledger.check_required_facts(
                oid, ["ingredients_with_amounts"], strict=strict
            )
            assert "ingredients_with_amounts" in result["covered"]
            assert "ingredients_with_amounts" not in result["missing"]
            assert "ingredients_with_amounts" not in result.get("manual_only", [])
            assert "ingredients_with_amounts" not in result.get("provisional", [])
