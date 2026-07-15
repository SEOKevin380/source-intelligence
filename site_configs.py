"""
Site configurations for layer-specific prompt generation.
Each site has editorial voice, disclaimers, and template preferences
used by prompt_builders.py to generate the right content brief.
"""

SITE_CONFIGS = {
    "topshelfmushrooms": {
        "name": "TopShelfMushrooms.com",
        "key": "topshelfmushrooms",
        "editorial_voice": (
            "Third-person as 'TopShelfMushrooms Research Desk'. "
            "Dose-math focused, formula-first, label-verification approach. "
            "Skeptical but fair. Every review verifies what's on the label vs. what the science supports. "
            "Anti-hype, transparent about limitations."
        ),
        "byline": "TopShelfMushrooms Research Desk",
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
        "name": "Vitamins-for-Men.com",
        "key": "vitaminsformen",
        "editorial_voice": (
            "Third-person as 'VFM Research Desk'. Skeptical, dose-math focused, investigative. "
            "Signature move: comparing clinical trial doses to what supplements actually deliver. "
            "Anti-hype, evidence-graded (Strong/Moderate/Preliminary/Traditional/Insufficient)."
        ),
        "byline": "VFM Research Desk",
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
        "name": "PVMedCenter.com",
        "key": "pvmedcenter",
        "editorial_voice": (
            "Professional, evidence-based health analysis. Expert analysis with balanced perspective. "
            "Hedging language throughout."
        ),
        "byline": "PVMedCenter Editorial Team",
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
        "name": "TutelaMedical.com",
        "key": "tutelamedical",
        "editorial_voice": (
            "Third-person as 'Tutela Medical'. Skeptical, evidence-forward, consumer-protective. "
            "Calls out marketing hype, proprietary blends, sub-therapeutic dosages. "
            "Anti-hype, transparent about limitations."
        ),
        "byline": "Tutela Medical Editorial Team",
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
        "name": "TotalHealthRD.com",
        "key": "totalhealthrd",
        "editorial_voice": (
            "Informative, encouraging. Inform and encourage the reader to consider purchasing. "
            "Ethical, transparent, no misleading claims. Uses hedging language throughout."
        ),
        "byline": "TotalHealthRD Editorial Team",
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
        "name": "HathawayMD.com",
        "key": "hathawaymd",
        "editorial_voice": "Evidence-based, medically informed, accessible.",
        "byline": "HathawayMD Editorial Team",
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
        "name": "HollyHerman.com",
        "key": "hollyherman",
        "editorial_voice": (
            "First-person as 'Holly'. Skeptical-investigative, anti-hype, 'your smart friend who did the "
            "research.' Calls out marketing BS, documents what she actually verified. "
            "Never declares winners — helps readers decide."
        ),
        "byline": "Holly Herman",
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
    "utcts": {
        "name": "UTCardiothoracicSurgery.com",
        "key": "utcts",
        "editorial_voice": (
            "Third-person as 'UTCTS Health Review Editorial Team'. Every review explicitly addresses "
            "cardiac patient safety, drug interactions with heart medications, and cardiovascular "
            "contraindications. Anti-hype, evidence-forward, cardiac-safety-first."
        ),
        "byline": "UTCTS Health Review Editorial Team",
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
    "micc": {
        "name": "MercyIowaCityClinics.org",
        "key": "micc",
        "editorial_voice": (
            "Third-person as 'MICC Review Team'. Evidence-forward, aggressive on calling out fake "
            "marketing. Separates marketing tactics from product evaluation. "
            "Direct, no-nonsense tone that respects the reader's intelligence."
        ),
        "byline": "MICC Review Team",
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
}


def get_site_config(site_key):
    """Get site config by key. Returns None if not found."""
    return SITE_CONFIGS.get(site_key)


def get_site_names():
    """Return list of (key, display_name) tuples for UI dropdowns."""
    return [(k, v["name"]) for k, v in SITE_CONFIGS.items()]


def get_l1_sites():
    """Return site configs that support L1 ingredient profiles."""
    return {k: v for k, v in SITE_CONFIGS.items() if v.get("l1_available")}
