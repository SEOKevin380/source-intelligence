"""Canonical product taxonomy and downstream routing contracts.

The intake enum, research packs, workbench, policy layer, exemplar retrieval,
and model-risk routing must agree on product identity.  An explicit product
type is never flattened into a generic workbench route.  Legacy family names
remain readable, but new sealed packs must use a supported canonical type.
"""

from __future__ import annotations


CANONICAL_PRODUCT_TYPES = (
    "supplement",
    "topical",
    "device",
    "food",
    "cannabis",
    "telehealth",
    "info_product",
    "financial",
    "software",
    "service",
    "program",
    "subscription",
    "professional",
    "gaming",
    "collectible",
    "research_peptide",
)

PRODUCT_TYPE_ALIASES = {
    "dietary_supplement": "supplement",
    "nutraceutical": "supplement",
    "vitamin": "supplement",
    "skincare": "topical",
    "skin_care": "topical",
    "cosmetic": "topical",
    "cream": "topical",
    "serum": "topical",
    "medical_device": "device",
    "consumer_device": "device",
    "consumer_electronics": "device",
    "gadget": "device",
    "functional_food": "food",
    "beverage": "food",
    "cbd": "cannabis",
    "hemp": "cannabis",
    "thc": "cannabis",
    "telemedicine": "telehealth",
    "prescription_service": "telehealth",
    "course": "info_product",
    "ebook": "info_product",
    "training": "info_product",
    "investment_newsletter": "financial",
    "financial_service": "financial",
    "saas": "software",
    "app": "software",
    "membership": "subscription",
    "lottery_tool": "gaming",
    "lottery": "gaming",
    "memorabilia": "collectible",
    "coin": "collectible",
    "peptide": "research_peptide",
}

# The exact canonical type is the workbench route. Families are used only by
# shared risk/policy infrastructure and must never overwrite product identity.
VERTICAL_FAMILIES = {
    "supplement": "health",
    "topical": "health",
    "device": "device",
    "food": "health",
    "cannabis": "health",
    "telehealth": "health",
    "info_product": "general_consumer",
    "financial": "financial",
    "software": "general_consumer",
    "service": "general_consumer",
    "program": "general_consumer",
    "subscription": "general_consumer",
    "professional": "general_consumer",
    "gaming": "gaming",
    "collectible": "collectible",
    "research_peptide": "health",
}

EXEMPLAR_VERTICALS = {
    "supplement": "supplement",
    "topical": "supplement",
    "device": "consumer_electronics",
    "food": "supplement",
    "cannabis": "supplement",
    "telehealth": "telehealth",
    "info_product": "info_product",
    "financial": "financial",
    "software": "info_product",
    "service": "info_product",
    "program": "info_product",
    "subscription": "info_product",
    "professional": "info_product",
    "gaming": "info_product",
    "collectible": "collectible",
    "research_peptide": "supplement",
}

RISK_TIERS = {
    "general_consumer": 0,
    "info_product": 0,
    "software": 0,
    "service": 0,
    "program": 0,
    "subscription": 0,
    "collectible": 0,
    "device": 1,
    "professional": 1,
    "gaming": 1,
    "supplement": 2,
    "topical": 2,
    "food": 2,
    "cannabis": 2,
    "telehealth": 2,
    "research_peptide": 3,
    "financial": 3,
    "political": 3,
    # Legacy family route retained for stored projects.
    "health": 2,
}


class UnsupportedProductTypeError(ValueError):
    """Raised before model spend when an explicit type lacks a full contract."""


def normalize_product_type(value: str) -> str:
    """Normalize a declared type without guessing from unrelated product copy."""
    normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return PRODUCT_TYPE_ALIASES.get(normalized, normalized)


def workbench_route(value: str) -> str:
    """Return the exact product-specific route or fail before paid generation."""
    normalized = normalize_product_type(value)
    if normalized not in CANONICAL_PRODUCT_TYPES:
        raise UnsupportedProductTypeError(
            f"Unsupported explicit product type '{value or 'missing'}'. "
            "Classify it into a supported intelligence pack before generation."
        )
    return normalized


def vertical_family(vertical: str) -> str:
    """Return the shared policy/risk family without erasing the exact route."""
    normalized = normalize_product_type(vertical)
    return VERTICAL_FAMILIES.get(normalized, normalized or "general_consumer")


def policy_vertical_aliases(vertical: str) -> set[str]:
    """Return exact and family policy scopes for a product-specific route."""
    normalized = normalize_product_type(vertical)
    family = vertical_family(normalized)
    aliases = {normalized, family}
    if normalized == "supplement":
        aliases.add("health")
    return {alias for alias in aliases if alias}


def exemplar_vertical(vertical: str) -> str:
    """Translate the exact route only at the historical-corpus boundary."""
    normalized = normalize_product_type(vertical)
    return EXEMPLAR_VERTICALS.get(normalized, normalized or "general_consumer")


def risk_tier(vertical: str) -> int:
    """Return the independent-review tier for exact and legacy routes."""
    normalized = normalize_product_type(vertical)
    return RISK_TIERS.get(normalized, RISK_TIERS.get(vertical_family(normalized), 1))


def assert_taxonomy_complete(enum_values: set[str]) -> None:
    """Fail tests/startup when a new intake enum lacks downstream ownership."""
    expected = set(enum_values) - {"unknown"}
    configured = set(CANONICAL_PRODUCT_TYPES)
    missing = sorted(expected - configured)
    stale = sorted(configured - expected)
    if missing or stale:
        raise RuntimeError(
            "Product taxonomy is incomplete: "
            f"missing_routes={missing}; stale_routes={stale}"
        )
