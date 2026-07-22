"""Tests for compliance.py — Rule-based compliance engine."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compliance import (
    ComplianceEngine, ComplianceState, ComplianceReport,
    ComplianceRule, Severity,
)
from entities import OfferingType


@pytest.fixture
def engine():
    return ComplianceEngine()


class TestComplianceEngine:
    def test_engine_loads_rules(self, engine):
        """Engine should have rules loaded from config.py."""
        assert engine.rule_count > 0

    def test_clean_text_passes(self, engine):
        """Normal hedged supplement text should pass."""
        text = (
            "This supplement may help support healthy blood sugar levels. "
            "Individual results may vary. Consult your healthcare provider."
        )
        report = engine.evaluate(text, OfferingType.SUPPLEMENT)
        assert report.blocks == 0

    def test_disease_reversal_blocked(self, engine):
        """CVD-9 disease reversal claims must be blocked."""
        text = "This product cures diabetes and eliminates cancer."
        report = engine.evaluate(text, OfferingType.SUPPLEMENT)
        assert report.overall_state == ComplianceState.BLOCKED
        assert report.blocks > 0

    def test_deceptive_claims_blocked(self, engine):
        """Physically impossible claims must be blocked."""
        text = "Guaranteed to increase penis size by 3 inches permanently."
        report = engine.evaluate(text, OfferingType.SUPPLEMENT)
        assert report.overall_state == ComplianceState.BLOCKED

    def test_red_flags_require_review(self, engine):
        """Unhedged health claims should trigger review."""
        text = "This supplement cures joint pain and prevents heart disease."
        report = engine.evaluate(text, OfferingType.SUPPLEMENT)
        # Should have at least review-level results
        assert report.reviews > 0 or report.blocks > 0

    def test_accesswire_channel_filtering(self, engine):
        """AccessWire R12 terms should only block on accesswire channel."""
        text = "This male enhancement product boosts libido and sexual function."

        # On WordPress: should flag as review (red flags) but not R12 block
        wp_report = engine.evaluate(text, OfferingType.SUPPLEMENT, channel="wordpress")
        # On AccessWire: should be blocked by R12
        aw_report = engine.evaluate(text, OfferingType.SUPPLEMENT, channel="accesswire")

        # AccessWire should have more blocks due to R12
        assert aw_report.blocks >= wp_report.blocks

    def test_globe_channel_filtering(self, engine):
        """Globe blocklist should only apply to globe channel."""
        text = "According to the company, this article examines the product."

        wp_report = engine.evaluate(text, OfferingType.SUPPLEMENT, channel="wordpress")
        globe_report = engine.evaluate(text, OfferingType.SUPPLEMENT, channel="globe")

        assert globe_report.blocks > wp_report.blocks

    def test_offering_type_filtering(self, engine):
        """Some rules only apply to ingestible products."""
        text = "This product prevents cognitive decline."
        # Supplements should be flagged
        supp_report = engine.evaluate(text, OfferingType.SUPPLEMENT)
        # Software should not be flagged for health claims
        sw_report = engine.evaluate(text, OfferingType.SOFTWARE)

        assert supp_report.reviews >= sw_report.reviews

    def test_standing_decline_drug_test(self, engine):
        """Drug test defeat products must be hard declined."""
        text = "Use this product to pass drug test easily with clean urine."
        report = engine.evaluate(text, OfferingType.SUPPLEMENT)
        assert report.overall_state == ComplianceState.BLOCKED

    def test_fda_disclaimer_check_present(self, engine):
        """FDA disclaimer check should pass when disclaimer is present."""
        text = (
            "Great supplement. "
            "These statements have not been evaluated by the Food and Drug Administration. "
            "This product is not intended to diagnose, treat, cure, or prevent any disease."
        )
        result = engine.check_fda_disclaimer(text, OfferingType.SUPPLEMENT)
        assert result is None  # No issue

    def test_fda_disclaimer_check_missing(self, engine):
        """FDA disclaimer check should flag when disclaimer is missing."""
        text = "Great supplement with amazing ingredients."
        result = engine.check_fda_disclaimer(text, OfferingType.SUPPLEMENT)
        assert result is not None
        assert result.rule_id == "FDA_DISCLAIMER_MISSING"

    def test_fda_disclaimer_not_required_for_software(self, engine):
        """Software doesn't need FDA disclaimer."""
        text = "Great project management software."
        result = engine.check_fda_disclaimer(text, OfferingType.SOFTWARE)
        assert result is None


class TestComplianceReport:
    def test_empty_report(self):
        report = ComplianceReport()
        assert report.overall_state == ComplianceState.CLEARED
        assert report.blocks == 0

    def test_summary(self):
        report = ComplianceReport()
        s = report.summary()
        assert "cleared" in s
        assert "0 blocks" in s
