"""Tests for intelligence_packs.py — Vertical intelligence definitions."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from entities import OfferingType
from intelligence_packs import (
    get_pack, get_required_facts, get_evidence_requirements,
    get_content_opportunities, INTELLIGENCE_PACKS,
)


class TestIntelligencePacks:
    def test_all_known_types_have_packs(self):
        """Every non-UNKNOWN type should have an intelligence pack."""
        for ot in OfferingType:
            if ot == OfferingType.UNKNOWN:
                continue
            pack = INTELLIGENCE_PACKS.get(ot)
            assert pack is not None, f"Missing pack for {ot.value}"

    def test_unknown_type_fails_closed(self):
        """UNKNOWN type must raise ValueError — fail closed."""
        with pytest.raises(ValueError, match="No intelligence pack"):
            get_pack(OfferingType.UNKNOWN)

    def test_supplement_pack_has_required_fields(self):
        pack = get_pack(OfferingType.SUPPLEMENT)
        assert "required_facts" in pack
        assert "authoritative_sources" in pack
        assert "compliance_rules" in pack
        assert "evidence_requirements" in pack
        assert "content_opportunities" in pack

    def test_supplement_requires_ingredients(self):
        facts = get_required_facts(OfferingType.SUPPLEMENT)
        assert "ingredients_with_amounts" in facts

    def test_supplement_requires_pubmed(self):
        reqs = get_evidence_requirements(OfferingType.SUPPLEMENT)
        assert reqs.get("pubmed_research") == "required"

    def test_device_has_different_requirements(self):
        """Device pack should differ from supplement pack."""
        supp_facts = get_required_facts(OfferingType.SUPPLEMENT)
        device_facts = get_required_facts(OfferingType.DEVICE)
        assert supp_facts != device_facts
        assert "key_features" in device_facts

    def test_telehealth_has_prescriber_verification(self):
        pack = get_pack(OfferingType.TELEHEALTH)
        assert "prescriber_verification" in pack["compliance_rules"]

    def test_cannabis_has_cannabinoid_profile(self):
        facts = get_required_facts(OfferingType.CANNABIS)
        assert "cannabinoid_profile" in facts

    def test_content_opportunities(self):
        opps = get_content_opportunities(OfferingType.SUPPLEMENT)
        assert len(opps) > 0
        assert "L6_product_review" in opps

    def test_all_packs_have_vendor_source(self):
        """Every pack should include vendor_page as an authoritative source."""
        for ot, pack in INTELLIGENCE_PACKS.items():
            sources = pack["authoritative_sources"]
            source_types = [s["type"] for s in sources]
            assert "vendor_page" in source_types, (
                f"{ot.value} pack missing vendor_page source"
            )
