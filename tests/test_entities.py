"""Tests for entities.py — Universal entity model."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from entities import OfferingType, Organization, Brand, Offering


class TestOfferingType:
    def test_all_types_exist(self):
        expected = [
            "supplement", "topical", "device", "food", "cannabis",
            "telehealth", "info_product", "financial", "software",
            "service", "program", "subscription", "professional",
            "research_peptide", "unknown",
        ]
        for t in expected:
            assert OfferingType(t) is not None

    def test_unknown_type_exists(self):
        assert OfferingType.UNKNOWN.value == "unknown"


class TestOrganization:
    def test_creation(self):
        org = Organization(name="Acme Corp", url="https://acme.com")
        assert org.name == "Acme Corp"
        assert org.url == "https://acme.com"

    def test_defaults(self):
        org = Organization(name="Test")
        assert org.url is None
        assert org.identifiers == {}


class TestBrand:
    def test_creation(self):
        org = Organization(name="Parent Co")
        brand = Brand(name="BrandX", organization=org)
        assert brand.name == "BrandX"
        assert brand.organization.name == "Parent Co"


class TestOffering:
    def test_creation(self):
        offering = Offering(
            name="TestProduct",
            offering_type=OfferingType.SUPPLEMENT,
            url="https://test.com",
        )
        assert offering.name == "TestProduct"
        assert offering.offering_type == OfferingType.SUPPLEMENT

    def test_is_ingestible(self):
        supp = Offering(name="S", offering_type=OfferingType.SUPPLEMENT)
        device = Offering(name="D", offering_type=OfferingType.DEVICE)
        food = Offering(name="F", offering_type=OfferingType.FOOD)
        assert supp.is_ingestible() is True
        assert device.is_ingestible() is False
        assert food.is_ingestible() is True

    def test_requires_fda_disclaimer(self):
        supp = Offering(name="S", offering_type=OfferingType.SUPPLEMENT)
        sw = Offering(name="SW", offering_type=OfferingType.SOFTWARE)
        assert supp.requires_fda_disclaimer() is True
        assert sw.requires_fda_disclaimer() is False

    def test_requires_ingredient_research(self):
        supp = Offering(name="S", offering_type=OfferingType.SUPPLEMENT)
        info = Offering(name="I", offering_type=OfferingType.INFO_PRODUCT)
        assert supp.requires_ingredient_research() is True
        assert info.requires_ingredient_research() is False

    def test_to_dict(self):
        offering = Offering(
            name="TestProduct",
            offering_type=OfferingType.SUPPLEMENT,
            url="https://test.com",
        )
        d = offering.to_dict()
        assert d["name"] == "TestProduct"
        assert d["offering_type"] == "supplement"
        assert d["url"] == "https://test.com"

    def test_from_legacy_product_data(self):
        legacy = {
            "product_name": "GlycoReset",
            "brand_name": "NaturalCo",
            "official_url": "https://glycoreset.com",
            "product_type": "supplement",
            "category": "blood_sugar",
            "supplement_facts": {
                "ingredients": [
                    {"name": "Berberine", "amount": "500mg"},
                    {"name": "Chromium", "amount": "200mcg"},
                ],
                "serving_size": "2 capsules",
            },
            "pricing": {"1 bottle": "$49", "3 bottles": "$117"},
            "refund_policy": "60-day money-back guarantee",
        }
        offering = Offering.from_legacy_product_data(legacy)
        assert offering.name == "GlycoReset"
        assert offering.offering_type == OfferingType.SUPPLEMENT
        assert offering.url == "https://glycoreset.com"
        assert len(offering.composition.get("ingredients", [])) == 2
        assert offering.policies.get("refund") == "60-day money-back guarantee"

    def test_from_legacy_unknown_type(self):
        legacy = {
            "product_name": "Mystery",
            "product_type": "something_new",
        }
        offering = Offering.from_legacy_product_data(legacy)
        assert offering.offering_type == OfferingType.UNKNOWN

    def test_from_legacy_empty(self):
        offering = Offering.from_legacy_product_data({})
        # Default product_type is "supplement" in from_legacy when not specified
        assert offering.name == ""
