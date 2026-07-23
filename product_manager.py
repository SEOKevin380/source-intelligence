"""
Source Intelligence — Product Manager
======================================
Strategic intelligence layer for the Product Intelligence CRM.
Coverage analysis, SERP strategy, publication compliance, and
cross-site coordination.
"""

import json
import os
import sys
from datetime import datetime

from database import ProductDatabase


# ──────────────────────────────────────────────────────────────────
# SITE INFRASTRUCTURE LOADING
# ──────────────────────────────────────────────────────────────────

def _load_wp_sites() -> dict:
    """Load wp-sites.json safely. Returns {site_key: config} or empty dict."""
    try:
        from config import WP_SITES_JSON_PATH
        if os.path.exists(WP_SITES_JSON_PATH):
            with open(WP_SITES_JSON_PATH) as f:
                raw = json.load(f)
            return raw.get("sites", raw)
    except Exception:
        pass
    return {}


def _load_slug_diversifier():
    """Load slug_diversifier module safely. Returns module or None."""
    try:
        from config import SLUG_DIVERSIFIER_DIR
        if SLUG_DIVERSIFIER_DIR not in sys.path:
            sys.path.insert(0, SLUG_DIVERSIFIER_DIR)
        import slug_diversifier
        return slug_diversifier
    except Exception:
        return None


# Category → relevant site archetypes mapping
# Determines which sites are "relevant" for a given product category
CATEGORY_SITE_RELEVANCE = {
    "weight_loss": ["clinical_medical", "physician_authority", "wellness_lifestyle",
                    "consumer_advocacy", "weight_specialist"],
    "brain_health": ["clinical_medical", "physician_authority", "neurological_research",
                     "wellness_lifestyle", "consumer_advocacy"],
    "blood_sugar": ["clinical_medical", "physician_authority", "wellness_lifestyle",
                    "consumer_advocacy"],
    "male_enhancement": ["clinical_medical", "physician_authority", "wellness_lifestyle",
                         "consumer_advocacy"],
    "heart_health": ["clinical_medical", "physician_authority", "cardiac_safety",
                     "wellness_lifestyle"],
    "joint_health": ["clinical_medical", "physician_authority", "pt_specialist",
                     "wellness_lifestyle", "consumer_advocacy"],
    "nerve_health": ["clinical_medical", "physician_authority", "neurological_research",
                     "wellness_lifestyle"],
    "pain_relief": ["clinical_medical", "physician_authority", "pt_specialist",
                    "podiatry_specialist", "wellness_lifestyle"],
    "skin_care": ["clinical_medical", "physician_authority", "aesthetic_wellness",
                  "wellness_lifestyle", "consumer_advocacy"],
    "cannabis": ["natural_botanical", "wellness_lifestyle", "consumer_advocacy"],
    "sleep": ["clinical_medical", "physician_authority", "wellness_lifestyle",
              "consumer_advocacy"],
    "gut_health": ["clinical_medical", "physician_authority", "wellness_lifestyle",
                   "natural_botanical", "consumer_advocacy"],
    "immune_health": ["clinical_medical", "physician_authority", "wellness_lifestyle",
                      "natural_botanical"],
    "dental": ["clinical_medical", "physician_authority", "wellness_lifestyle",
               "consumer_advocacy"],
    "vision": ["clinical_medical", "physician_authority", "wellness_lifestyle"],
    "anti_aging": ["clinical_medical", "physician_authority", "aesthetic_wellness",
                   "wellness_lifestyle"],
    "respiratory": ["clinical_medical", "physician_authority", "wellness_lifestyle"],
    "telehealth": ["clinical_medical", "physician_authority", "wellness_lifestyle",
                   "consumer_advocacy"],
}

# 10 primary sites we actively manage
PRIMARY_SITES = [
    "pvmedcenter", "tutelamedical", "hollyherman", "totalhealthrd",
    "hathawaymd", "utcardiothoracic", "mercyiowacity",
    "londonbridgeurology", "topshelfmushrooms", "vitaminsformen",
]

# Site key → archetype loaded from wp-sites.json (source of truth)
# Keys in wp-sites.json may differ slightly from PRIMARY_SITES keys
_WP_SITES_KEY_MAP = {
    "utcardiothoracic": "utcardiothoracicsurgery",
    "mercyiowacity": "mercyiowacityclinics",
}

def _get_primary_site_archetypes() -> dict:
    """Load archetypes from wp-sites.json for primary sites.

    Returns {site_key: archetype} using wp-sites.json values which match
    CATEGORY_SITE_RELEVANCE labels (clinical_medical, wellness_lifestyle, etc.).
    """
    wp_sites = _load_wp_sites()
    result = {}
    for site_key in PRIMARY_SITES:
        wp_key = _WP_SITES_KEY_MAP.get(site_key, site_key)
        cfg = wp_sites.get(wp_key, {})
        archetype = cfg.get("archetype", "")
        if archetype:
            result[site_key] = archetype
        else:
            # Fallback if wp-sites.json unavailable
            result[site_key] = "clinical_medical"
    return result

# Lazy-loaded on first use
_primary_site_archetypes_cache = None

def _get_cached_archetypes() -> dict:
    global _primary_site_archetypes_cache
    if _primary_site_archetypes_cache is None:
        _primary_site_archetypes_cache = _get_primary_site_archetypes()
    return _primary_site_archetypes_cache


# ──────────────────────────────────────────────────────────────────
# COVERAGE ANALYSIS
# ──────────────────────────────────────────────────────────────────

def get_coverage_report(product_key: str, db: ProductDatabase = None) -> dict:
    """
    Analyze which sites have/don't have this product and recommend next targets.

    Returns:
        {
            "published_sites": [{site_key, site_name, slug, angle, date}],
            "recommended_sites": [{site_key, site_name, archetype, reason}],
            "not_relevant_sites": [{site_key, reason}],
            "coverage_pct": float,
            "total_relevant": int,
        }
    """
    if db is None:
        db = ProductDatabase()

    product = db.get_product(product_key)
    if not product:
        return {
            "published_sites": [], "recommended_sites": [],
            "not_relevant_sites": [], "coverage_pct": 0, "total_relevant": 0,
        }

    category = product.get("category", "")
    product_type = product.get("product_type", "supplement")
    coverage = db.get_coverage_matrix(product_key)

    # Determine which archetypes are relevant for this category
    relevant_archetypes = CATEGORY_SITE_RELEVANCE.get(category, [])
    # Default: most archetypes except highly specialized ones
    if not relevant_archetypes:
        relevant_archetypes = [
            "clinical_medical", "physician_authority", "wellness_lifestyle",
            "consumer_advocacy",
        ]

    published = []
    recommended = []
    not_relevant = []

    archetypes = _get_cached_archetypes()
    for site_key in PRIMARY_SITES:
        archetype = archetypes.get(site_key, "unknown")

        if site_key in coverage:
            pub = coverage[site_key]
            published.append({
                "site_key": site_key,
                "site_name": site_key,
                "slug": pub.get("slug", ""),
                "angle": pub.get("angle", ""),
                "date": pub.get("date", ""),
                "content_type": pub.get("content_type", ""),
            })
        elif archetype in relevant_archetypes:
            recommended.append({
                "site_key": site_key,
                "site_name": site_key,
                "archetype": archetype,
                "reason": f"Category '{category}' fits {archetype} archetype",
            })
        else:
            not_relevant.append({
                "site_key": site_key,
                "reason": f"Archetype '{archetype}' not relevant for '{category}' products",
            })

    # Also check for publications on non-primary sites
    for site_key, pub in coverage.items():
        if site_key not in PRIMARY_SITES:
            published.append({
                "site_key": site_key,
                "site_name": pub.get("site_name", site_key),
                "slug": pub.get("slug", ""),
                "angle": pub.get("angle", ""),
                "date": pub.get("date", ""),
                "content_type": pub.get("content_type", ""),
            })

    total_relevant = len(published) + len(recommended)
    coverage_pct = (len(published) / total_relevant * 100) if total_relevant > 0 else 0

    return {
        "published_sites": published,
        "recommended_sites": recommended,
        "not_relevant_sites": not_relevant,
        "coverage_pct": round(coverage_pct, 1),
        "total_relevant": total_relevant,
    }


# ──────────────────────────────────────────────────────────────────
# SERP STRATEGY
# ──────────────────────────────────────────────────────────────────

def get_serp_strategy(product_key: str, db: ProductDatabase = None) -> dict:
    """
    Analyze SERP stacking opportunities — which angles are used vs available.

    Returns:
        {
            "used_angles": [{"angle": str, "site_key": str, "slug": str}],
            "available_angles": [{"angle": str, "site_key": str}],
            "slug_previews": {site_key: suggested_slug},
            "strategy_notes": [str],
        }
    """
    if db is None:
        db = ProductDatabase()

    product = db.get_product(product_key)
    if not product:
        return {
            "used_angles": [], "available_angles": [],
            "slug_previews": {}, "strategy_notes": ["Product not found"],
        }

    coverage = get_coverage_report(product_key, db)
    used_angles = []
    available_angles = []
    slug_previews = {}
    notes = []

    # Collect used angles
    for pub in coverage["published_sites"]:
        angle = pub.get("angle", "")
        if not angle:
            # Try to infer angle from site archetype
            angle = _get_cached_archetypes().get(pub["site_key"], "unknown")
        used_angles.append({
            "angle": angle,
            "site_key": pub["site_key"],
            "slug": pub.get("slug", ""),
        })

    # Collect available angles and try to generate slug previews
    sd = _load_slug_diversifier()
    for rec in coverage["recommended_sites"]:
        angle = rec.get("archetype", "")
        available_angles.append({
            "angle": angle,
            "site_key": rec["site_key"],
        })

        # Try to generate a slug preview
        if sd:
            try:
                product_name = product.get("product_name", "")
                category = product.get("category", "")
                slug = sd.get_diversified_slug(
                    product_name, category, rec["site_key"]
                )
                slug_previews[rec["site_key"]] = slug
            except Exception:
                pass

    # Strategy notes
    used_angle_set = {a["angle"] for a in used_angles}
    if len(used_angles) == 0:
        notes.append("No publications yet — all angles available for SERP stacking")
    elif len(used_angles) >= 5:
        notes.append(
            f"Strong coverage ({len(used_angles)} angles used). "
            f"Consider funnel content (hub page, comparison, safety guide)."
        )
    if len(available_angles) > 0:
        notes.append(
            f"{len(available_angles)} sites still available for coverage. "
            f"Each targets a different keyword intent."
        )

    # Check for angle diversity issues
    if len(used_angle_set) < len(used_angles):
        notes.append(
            "WARNING: Some publications share the same angle — "
            "this reduces SERP stacking effectiveness."
        )

    return {
        "used_angles": used_angles,
        "available_angles": available_angles,
        "slug_previews": slug_previews,
        "strategy_notes": notes,
    }


# ──────────────────────────────────────────────────────────────────
# PREVIOUS RELEASES AUTO-POPULATION
# ──────────────────────────────────────────────────────────────────

def get_previous_releases(product_key: str, platform: str = None,
                          db: ProductDatabase = None) -> str:
    """
    Get formatted PREVIOUS RELEASES string from publication records.
    Returns empty string if no publications found.

    Format: "Site1: /slug1/ (YYYY-MM-DD) | Site2: /slug2/ (YYYY-MM-DD)"
    """
    if db is None:
        db = ProductDatabase()

    pubs = db.get_publications(product_key)
    if not pubs:
        return ""

    parts = []
    for p in pubs:
        site = p.get("site_name") or p.get("site_key", "unknown")
        slug = p.get("slug", "")
        date = p.get("published_date", "")
        if slug:
            parts.append(f"{site}: /{slug}/ ({date})")

    return " | ".join(parts) if parts else ""


# ──────────────────────────────────────────────────────────────────
# PROMPT COMPLETENESS SCORE
# ──────────────────────────────────────────────────────────────────

def get_prompt_completeness(product_key: str, db: ProductDatabase = None) -> dict:
    """
    Score how complete the research data is for prompt generation.
    Each C-section maps to specific data requirements.

    Returns:
        {
            "score": int (0-100),
            "sections": {
                "C1": {"status": "complete"|"partial"|"missing", "detail": str},
                "C2": {...},
                ...
            },
            "ready_for_editorial_review": bool,
            "missing_critical": [str],
        }
    """
    if db is None:
        db = ProductDatabase()

    product_rec = db.get_product(product_key)
    if not product_rec or not product_rec.get("research_data"):
        return {
            "score": 0,
            "sections": {},
            "ready_for_editorial_review": False,
            "missing_critical": ["No research data found"],
        }

    data = product_rec["research_data"]
    product = data.get("product", {})
    sf = product.get("supplement_facts", {})
    ingredients = sf.get("ingredients", [])
    ing_research = data.get("ingredient_research", {})
    safety = data.get("safety", {})
    compliance = data.get("compliance", {})
    reputation = data.get("reputation", {})
    keywords = data.get("keywords", {})
    competitive = data.get("competitive", {})

    # Non-health products must never inherit supplement/PubMed scorecards.
    # Their completeness is based on identity, captured sources, offer facts,
    # terms, claims, company/contact data, and category-appropriate compliance.
    product_type = str(product.get("product_type", "")).strip().lower()
    health_types = {"supplement", "topical", "cannabis", "food", "telehealth"}
    if product_type not in health_types:
        sections = {}
        missing_critical = []
        total = 109
        earned = 0

        identity_ok = bool(product.get("product_name") and product.get("official_url"))
        sections["Identity"] = {
            "status": "complete" if identity_ok else "missing",
            "detail": "Product and official URL captured" if identity_ok else "Product identity or official URL missing",
        }
        earned += 15 if identity_ok else 0
        if not identity_ok:
            missing_critical.append("Product identity or official URL missing")

        artifacts = data.get("all_artifacts") or []
        manifest = data.get("source_manifest") or []
        source_count = len(artifacts) or sum(
            1 for item in manifest if isinstance(item, dict)
            and str(item.get("status", "")).lower() in {"captured", "success", "fetched", "available", "reused"}
        )
        sections["Sources"] = {
            "status": "complete" if source_count >= 2 else "partial" if source_count else "missing",
            "detail": f"{source_count} source records captured" if source_count else "No source records captured",
        }
        earned += 20 if source_count >= 2 else 10 if source_count else 0
        if not source_count:
            missing_critical.append("No captured source material")

        claims = product.get("claims", []) or data.get("publication_claims", {})
        claim_count = len(claims) if isinstance(claims, list) else sum(
            len(items or []) for items in claims.values()
        ) if isinstance(claims, dict) else 0
        sections["Claims"] = {
            "status": "complete" if claim_count >= 3 else "partial" if claim_count else "missing",
            "detail": f"{claim_count} source-backed claims" if claim_count else "No publishable claims extracted",
        }
        earned += 15 if claim_count >= 3 else 7 if claim_count else 0

        pricing = product.get("pricing", [])
        sections["Pricing"] = {
            "status": "complete" if pricing else "missing",
            "detail": "Pricing captured" if pricing else "Pricing unavailable — omit from copy",
        }
        earned += 10 if pricing else 0

        refund = product.get("refund_policy")
        sections["Terms"] = {
            "status": "complete" if refund else "partial",
            "detail": "Refund/offer terms captured" if refund else "Refund terms unavailable — omit from copy",
        }
        earned += 8 if refund else 3

        company = product.get("company", {}) or {}
        contact_ok = bool(company.get("name") or product.get("brand_name") or product.get("contact_info"))
        sections["Company & Contact"] = {
            "status": "complete" if contact_ok else "partial",
            "detail": "Company/contact information captured" if contact_ok else "Limited company/contact information",
        }
        earned += 8 if contact_ok else 3

        sections["Compliance"] = {
            "status": "complete" if compliance else "missing",
            "detail": f"Category-specific risk: {compliance.get('risk_level', 'unknown')}" if compliance else "No category-specific compliance result",
        }
        earned += 15 if compliance else 0
        if not compliance:
            missing_critical.append("No category-specific compliance result")

        strategy_points = (5 if keywords else 0) + (4 if competitive else 0)
        sections["Search Strategy"] = {
            "status": "complete" if strategy_points >= 7 else "partial" if strategy_points else "missing",
            "detail": "Keyword and competitive inputs available" if strategy_points >= 7 else "Search inputs are limited",
        }
        earned += strategy_points

        access = product.get("delivery") or product.get("access_terms") or product.get("shipping_policy")
        sections["Delivery / Access"] = {
            "status": "complete" if access else "partial",
            "detail": "Delivery or access details captured" if access else "Delivery/access details unavailable — omit from copy",
        }
        earned += 9 if access else 4

        score = min(100, round(earned / total * 100))
        return {
            "score": score,
            "sections": sections,
            "ready_for_editorial_review": not missing_critical and score >= 55,
            "missing_critical": missing_critical,
        }

    sections = {}
    missing_critical = []
    total = 0
    earned = 0

    # C1 — Supplement Facts / Product Composition
    total += 10
    if ingredients:
        sections["C1"] = {"status": "complete", "detail": f"{len(ingredients)} ingredients"}
        earned += 10
    else:
        pt = product.get("product_type", "supplement")
        if pt in ("cannabis", "device", "info_product"):
            sections["C1"] = {"status": "partial", "detail": f"{pt} — ingredients may not apply"}
            earned += 5
        else:
            sections["C1"] = {"status": "missing", "detail": "No ingredients extracted"}
            missing_critical.append("C1: No ingredient data")

    # C2 — Pricing
    total += 8
    pricing = product.get("pricing", [])
    if pricing and len(pricing) >= 2:
        sections["C2"] = {"status": "complete", "detail": f"{len(pricing)} pricing tiers"}
        earned += 8
    elif pricing:
        sections["C2"] = {"status": "partial", "detail": "Single price only — check for bundles"}
        earned += 4
    else:
        sections["C2"] = {"status": "missing", "detail": "No pricing data"}

    # C3 — Refund Policy
    total += 5
    if product.get("refund_policy"):
        sections["C3"] = {"status": "complete", "detail": "Refund policy captured"}
        earned += 5
    else:
        sections["C3"] = {"status": "missing", "detail": "No refund policy"}

    # C4 — PubMed Research
    total += 12
    study_count = sum(len(r.get("studies", [])) for r in ing_research.values())
    if study_count >= 5:
        sections["C4"] = {"status": "complete", "detail": f"{study_count} studies"}
        earned += 12
    elif study_count > 0:
        sections["C4"] = {"status": "partial", "detail": f"{study_count} studies — limited"}
        earned += 6
    else:
        sections["C4"] = {"status": "missing", "detail": "No PubMed studies"}

    # C5 — Clinical Evidence Summary
    total += 8
    if study_count >= 3 and ingredients:
        sections["C5"] = {"status": "complete", "detail": "Sufficient data for evidence summary"}
        earned += 8
    elif study_count > 0:
        sections["C5"] = {"status": "partial", "detail": "Limited evidence base"}
        earned += 4
    else:
        sections["C5"] = {"status": "missing", "detail": "No clinical evidence data"}

    # C6 — Drug Interactions
    total += 10
    # Check that safety dict has actual data, not just empty sub-dicts
    has_real_safety = False
    if safety and isinstance(safety, dict):
        for ing_key, ing_safety in safety.items():
            if isinstance(ing_safety, dict) and any(
                v for k, v in ing_safety.items()
                if v and k not in ("ingredient_name",) and v != "Check required"
            ):
                has_real_safety = True
                break
    if has_real_safety:
        sections["C6"] = {"status": "complete", "detail": "Safety data available"}
        earned += 10
    elif product.get("product_type") == "cannabis" or product.get("category") == "cannabis":
        sections["C6"] = {"status": "partial", "detail": "Cannabis — generic safety profile applied, product-specific data not collected"}
        earned += 5
    elif ingredients:
        sections["C6"] = {"status": "partial", "detail": "Ingredients found but no safety data"}
        earned += 3
    else:
        sections["C6"] = {"status": "missing", "detail": "No safety/interaction data"}

    # C7 — Claims Analysis
    total += 8
    claims = product.get("claims", [])
    if len(claims) >= 3:
        sections["C7"] = {"status": "complete", "detail": f"{len(claims)} claims"}
        earned += 8
    elif claims:
        sections["C7"] = {"status": "partial", "detail": f"{len(claims)} claims — limited"}
        earned += 4
    else:
        sections["C7"] = {"status": "missing", "detail": "No claims extracted"}

    # C8-C9 — Company/Brand Info
    total += 5
    company = product.get("company", {})
    if company.get("name") or product.get("brand_name"):
        sections["C8-C9"] = {"status": "complete", "detail": "Company info available"}
        earned += 5
    else:
        sections["C8-C9"] = {"status": "missing", "detail": "No company info"}

    # C10 — Reputation
    total += 7
    # Check that reputation dict has actual findings, not just placeholders
    has_real_reputation = False
    if reputation and isinstance(reputation, dict):
        skip_keys = {"search_queries_to_run", "Check required"}
        for k, v in reputation.items():
            if k in skip_keys:
                continue
            if isinstance(v, str) and v in ("Check required", "", "Not checked"):
                continue
            if isinstance(v, (list, dict)) and not v:
                continue
            has_real_reputation = True
            break
    if has_real_reputation:
        sections["C10"] = {"status": "complete", "detail": "Reputation data available"}
        earned += 7
    else:
        sections["C10"] = {"status": "missing", "detail": "No reputation data"}

    # C11-C12 — Keywords & Competitive
    total += 7
    if keywords:
        sections["C11"] = {"status": "complete", "detail": "Keywords generated"}
        earned += 4
    else:
        sections["C11"] = {"status": "missing", "detail": "No keywords"}
    # Check competitive has actual data (not just empty competitors list)
    has_real_competitive = (
        competitive and isinstance(competitive, dict) and
        competitive.get("competitors") and len(competitive["competitors"]) > 0
    )
    if has_real_competitive:
        sections["C12"] = {"status": "complete", "detail": "Competitive intel available"}
        earned += 3
    else:
        sections["C12"] = {"status": "missing", "detail": "No competitive data"}

    # C15 — Compliance
    total += 10
    if compliance:
        sections["C15"] = {"status": "complete", "detail": f"Risk: {compliance.get('risk_level', 'Unknown')}"}
        earned += 10
    else:
        sections["C15"] = {"status": "missing", "detail": "No compliance analysis"}
        missing_critical.append("C15: No compliance data")

    # C16-C19 — Shipping, Warranty, Testimonials, FAQs
    # Use local scoring — NOT the global earned/total accumulators
    c16_earned = 0
    if product.get("shipping_policy"):
        c16_earned += 3
    if product.get("warranty"):
        c16_earned += 2
    if product.get("testimonials"):
        c16_earned += 3
    if product.get("brand_faqs"):
        c16_earned += 2
    total += 10
    earned += c16_earned
    sections["C16-C19"] = {
        "status": "complete" if c16_earned >= 7 else (
            "partial" if c16_earned > 0 else "missing"
        ),
        "detail": f"Supporting data: {c16_earned}/10 points",
    }

    score = round(earned / total * 100) if total > 0 else 0
    ready = score >= 60 and len(missing_critical) == 0

    return {
        "score": score,
        "sections": sections,
        "ready_for_editorial_review": ready,
        "missing_critical": missing_critical,
    }


# ──────────────────────────────────────────────────────────────────
# NETWORK-WIDE CHECKS
# ──────────────────────────────────────────────────────────────────

def get_stale_products(max_days: int = 30, db: ProductDatabase = None) -> list:
    """Find products with research older than max_days."""
    if db is None:
        db = ProductDatabase()

    products = db.list_products()
    stale = []
    for p in products:
        freshness = db.check_data_freshness(p["product_key"], max_days)
        if not freshness["is_fresh"] and freshness["days_old"] >= 0:
            stale.append({
                "product_key": p["product_key"],
                "product_name": p["product_name"],
                "days_old": freshness["days_old"],
                "last_updated": p.get("last_updated", ""),
                "publication_count": p.get("publication_count", 0),
            })

    return sorted(stale, key=lambda x: x["days_old"], reverse=True)


def get_low_quality_products(threshold: int = 40, db: ProductDatabase = None) -> list:
    """Find products with quality score below threshold."""
    if db is None:
        db = ProductDatabase()

    products = db.list_products()
    return [
        p for p in products
        if p.get("quality_score", 0) < threshold
        and p.get("quality_score", 0) > 0  # Exclude stubs
    ]
