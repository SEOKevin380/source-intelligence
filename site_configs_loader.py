"""
Site Configuration Loader — Unified site config from wp-sites.json + editorial supplements.

wp-sites.json is the single source of truth for:
  - Site URL, credentials, archetype, voice, byline, categories

This module enriches those with editorial data that doesn't belong in wp-sites.json:
  - Disclaimers, word count ranges, L1 structures, evidence grades, niche focus
"""

import json
import os

# Editorial supplements — data that doesn't belong in wp-sites.json
# These are keyed by site_key (matching wp-sites.json keys or internal aliases)
_EDITORIAL_SUPPLEMENTS = {
    "topshelfmushrooms": {
        "disclaimer_top": "",
        "disclaimer_bottom": (
            '<p><em>This content is for informational purposes only and does not constitute medical advice. '
            "Consult a qualified healthcare provider before beginning any supplement. Dietary supplements have not "
            "been evaluated by the FDA and are not intended to diagnose, treat, cure, or prevent any disease.</em></p>"
        ),
        "slug_pattern": "descriptive, keyword-rich (e.g., product-review-2026-dose-math-and-verified-policies)",
        "word_count_range": (1000, 1500),
        "l1_word_count_range": (1500, 2200),
        "l1_available": True,
        "l1_structure": [
            "What It Is", "Key Bioactive Compounds", "How It Works",
            "What Research Shows", "Dosage & Standardization",
            "Quality Markers", "Synergies", "Safety Considerations", "Bottom Line",
        ],
        "evidence_grades": ["Strong", "Moderate", "Preliminary", "Traditional"],
        "niche_focus": "functional mushroom supplements",
    },
    "vitaminsformen": {
        "disclaimer_top": (
            '<p><em>This article is for informational purposes only and does not constitute medical advice. '
            "Consult a qualified healthcare provider before beginning any supplement. Dietary supplements have not "
            "been evaluated by the FDA and are not intended to diagnose, treat, cure, or prevent any disease.</em></p>"
        ),
        "disclaimer_bottom": "",
        "slug_pattern": "descriptive ingredient-focused (e.g., vitamin-d3-mens-health-complete-profile)",
        "word_count_range": (1000, 1500),
        "l1_word_count_range": (800, 1400),
        "l1_available": True,
        "l1_structure": [
            "Quick Answer", "What It Is", "What the Research Shows for Men",
            "Dose Math", "Forms & Bioavailability",
            "Who Should Consider It / Who Should Avoid It",
            "Safety & Side Effects", "Key Takeaway",
        ],
        "evidence_grades": ["Strong", "Moderate", "Preliminary", "Traditional", "Insufficient"],
        "niche_focus": "men's health supplements",
    },
    "pvmedcenter": {
        "disclaimer_top": "",
        "disclaimer_bottom": (
            '<p><em>These statements have not been evaluated by the FDA. This product is not intended to '
            "diagnose, treat, cure, or prevent any disease. Individual results may vary.</em></p>"
        ),
        "slug_pattern": "short (e.g., product-review)",
        "word_count_range": (1000, 1500),
        "l1_available": False,
    },
    "tutelamedical": {
        "disclaimer_top": "",
        "disclaimer_bottom": (
            '<p><em>These statements have not been evaluated by the FDA. This product is not intended to '
            "diagnose, treat, cure, or prevent any disease. Individual results may vary.</em></p>"
        ),
        "slug_pattern": "product-review-2026-descriptive-subtitle-examined",
        "word_count_range": (1000, 1500),
        "l1_available": False,
    },
    "totalhealthrd": {
        "disclaimer_top": "",
        "disclaimer_bottom": (
            '<p><em>These statements have not been evaluated by the FDA. This product is not intended to '
            "diagnose, treat, cure, or prevent any disease. Individual results may vary.</em></p>"
        ),
        "slug_pattern": "product-name-review (short, simple)",
        "word_count_range": (700, 1200),
        "l1_available": False,
    },
    "hathawaymd": {
        "disclaimer_top": "",
        "disclaimer_bottom": (
            '<p><em>These statements have not been evaluated by the FDA. This product is not intended to '
            "diagnose, treat, cure, or prevent any disease. Individual results may vary.</em></p>"
        ),
        "slug_pattern": "product-review",
        "word_count_range": (1000, 1500),
        "l1_available": False,
    },
    "hollyherman": {
        "disclaimer_top": (
            '<p><em>Affiliate Disclosure: This article may contain affiliate links. '
            "These statements have not been evaluated by the FDA. This product is not intended to "
            "diagnose, treat, cure, or prevent any disease.</em></p>"
        ),
        "disclaimer_bottom": (
            '<p><em>These statements have not been evaluated by the FDA. This product is not intended to '
            "diagnose, treat, cure, or prevent any disease. Individual results may vary.</em></p>"
        ),
        "slug_pattern": "product-review-what-i-verified-before-buying",
        "word_count_range": (1000, 1500),
        "l1_available": False,
    },
    "utcardiothoracicsurgery": {
        "disclaimer_top": (
            '<p><em>FTC Disclosure: This article may contain affiliate links. '
            "These statements have not been evaluated by the FDA. This product is not intended to "
            "diagnose, treat, cure, or prevent any disease.</em></p>"
        ),
        "disclaimer_bottom": "",
        "slug_pattern": "product-review-cardiac-safety-2026",
        "word_count_range": (1000, 1500),
        "l1_available": False,
    },
    "mercyiowacityclinics": {
        "disclaimer_top": (
            '<p><em>MercyIowaCityClinics.org is an independent editorial publication and is not affiliated '
            "with any hospital, clinic, or medical provider.</em></p>"
        ),
        "disclaimer_bottom": (
            '<p><em>These statements have not been evaluated by the FDA. This product is not intended to '
            "diagnose, treat, cure, or prevent any disease. Individual results may vary.</em></p>"
        ),
        "slug_pattern": "product-review-2026-micc-team-evaluates-the-formula",
        "word_count_range": (1000, 1500),
        "l1_available": False,
    },
    "londonbridgeurology": {
        "disclaimer_top": "",
        "disclaimer_bottom": (
            '<p><em>These statements have not been evaluated by the FDA. This product is not intended to '
            "diagnose, treat, cure, or prevent any disease. Individual results may vary.</em></p>"
        ),
        "slug_pattern": "product-review",
        "word_count_range": (1000, 1500),
        "l1_available": False,
    },
}

# Internal aliases used in old site_configs.py → wp-sites.json keys
_KEY_ALIASES = {
    "utcts": "utcardiothoracicsurgery",
    "micc": "mercyiowacityclinics",
    "utcardiothoracic": "utcardiothoracicsurgery",
    "mercyiowacity": "mercyiowacityclinics",
}

_wp_sites_cache = None


def _load_wp_sites() -> dict:
    """Load wp-sites.json. Returns {site_key: config}."""
    global _wp_sites_cache
    if _wp_sites_cache is not None:
        return _wp_sites_cache
    try:
        from config import WP_SITES_JSON_PATH
        path = WP_SITES_JSON_PATH
    except ImportError:
        path = os.path.expanduser(
            "~/Desktop/Code Projects/mbk-recovery/config/wp-sites.json"
        )
    if os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)
        _wp_sites_cache = raw.get("sites", raw)
    else:
        _wp_sites_cache = {}
    return _wp_sites_cache


def get_site_config(site_key: str) -> dict:
    """Get merged site config: wp-sites.json base + editorial supplements.

    Accepts internal aliases (utcts, micc, utcardiothoracic, mercyiowacity).
    Returns None if site not found anywhere.
    """
    canonical = _KEY_ALIASES.get(site_key, site_key)
    wp_sites = _load_wp_sites()
    wp_cfg = wp_sites.get(canonical, {})

    # Build merged config
    cfg = {}

    # wp-sites.json fields
    if wp_cfg:
        cfg["name"] = wp_cfg.get("name", canonical)
        cfg["key"] = canonical
        cfg["site_key"] = canonical
        cfg["url"] = wp_cfg.get("url", "")
        cfg["editorial_voice"] = wp_cfg.get("voice", "")
        cfg["byline"] = wp_cfg.get("byline", "")
        cfg["archetype"] = wp_cfg.get("archetype", "")
        cfg["compliance_level"] = wp_cfg.get("compliance_level", "")
        cfg["categories"] = wp_cfg.get("categories", [])

    # Editorial supplements (override/add)
    supplements = _EDITORIAL_SUPPLEMENTS.get(canonical, {})
    cfg.update(supplements)

    # Defaults for sites not in editorial supplements
    if "disclaimer_top" not in cfg:
        cfg["disclaimer_top"] = ""
    if "disclaimer_bottom" not in cfg:
        cfg["disclaimer_bottom"] = (
            '<p><em>These statements have not been evaluated by the FDA. This product is not intended to '
            "diagnose, treat, cure, or prevent any disease. Individual results may vary.</em></p>"
        )
    if "word_count_range" not in cfg:
        cfg["word_count_range"] = (1000, 1500)
    if "l1_available" not in cfg:
        cfg["l1_available"] = False

    # If we have nothing at all, return None
    if not wp_cfg and not supplements:
        return None

    return cfg


def get_site_names() -> list:
    """Return list of (key, display_name) tuples for UI dropdowns.

    Includes all sites from editorial supplements (primary sites).
    """
    result = []
    wp_sites = _load_wp_sites()
    seen = set()
    # Primary sites from editorial supplements first
    for key in _EDITORIAL_SUPPLEMENTS:
        cfg = get_site_config(key)
        if cfg:
            result.append((key, cfg.get("name", key)))
            seen.add(key)
    # Then any remaining wp-sites.json sites
    for key in wp_sites:
        if key not in seen:
            result.append((key, wp_sites[key].get("name", key)))
    return result


def get_l1_sites() -> dict:
    """Return site configs that support L1 ingredient profiles."""
    result = {}
    for key, supplements in _EDITORIAL_SUPPLEMENTS.items():
        if supplements.get("l1_available"):
            cfg = get_site_config(key)
            if cfg:
                result[key] = cfg
    return result
