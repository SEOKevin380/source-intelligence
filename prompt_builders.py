"""
Layer-specific prompt builders for the Source Intelligence Tool.

Each function takes structured research data and returns a complete prompt string
ready to paste into Claude Projects. Pure functions — data in, string out.
"""

import json
import os
from config import INGREDIENT_DB_PATH


def _load_ingredient_kb():
    """Load the ingredient knowledge base (cached PubMed + safety data)."""
    if os.path.exists(INGREDIENT_DB_PATH):
        with open(INGREDIENT_DB_PATH) as f:
            return json.load(f)
    return {}


def _get_enriched_ingredient(ingredient_name, product_research, ingredient_kb):
    """Get the richest data for an ingredient by merging product research with KB.

    The KB may have more studies than the current product's research if the ingredient
    was previously researched for other products.
    """
    key = ingredient_name.lower().strip()
    kb_entry = ingredient_kb.get(key, {})
    product_entry = product_research.get(ingredient_name, {})

    # Start with KB data (may have accumulated studies from multiple products)
    merged = {
        "evidence_grade": kb_entry.get("evidence_grade", product_entry.get("evidence_grade", "Insufficient")),
        "clinical_dose_range": kb_entry.get("clinical_dose_range", product_entry.get("clinical_dose_range", "")),
        "product_dose": product_entry.get("product_dose", ""),
        "studies": [],
        "side_effects": kb_entry.get("side_effects", []),
        "drug_interactions": kb_entry.get("drug_interactions", []),
        "contraindications": kb_entry.get("contraindications", []),
    }

    # Merge studies from both sources, dedup by PMID
    seen_pmids = set()
    for source in [kb_entry.get("studies", []), product_entry.get("studies", [])]:
        for study in source:
            pmid = study.get("pmid", "")
            if pmid and pmid not in seen_pmids:
                seen_pmids.add(pmid)
                merged["studies"].append(study)

    return merged


def _format_studies_block(studies, max_studies=None):
    """Format a list of PubMed studies as a text block for prompt injection."""
    if not studies:
        return "No PubMed studies available for this ingredient.\n"

    subset = studies[:max_studies] if max_studies else studies
    lines = []
    for s in subset:
        tier = s.get("quality_tier", "standard").upper()
        lines.append(
            f"  [{tier}] PMID:{s.get('pmid', '')} — {s.get('title', '')} "
            f"({s.get('journal', '')}, {s.get('year', '')})"
        )
        if s.get("abstract"):
            abstract = s["abstract"][:600] + "..." if len(s.get("abstract", "")) > 600 else s.get("abstract", "")
            lines.append(f"    Abstract: {abstract}")
    return "\n".join(lines) + "\n"


def _format_safety_block(ingredient_name, safety_data):
    """Format safety data for a single ingredient."""
    sdata = safety_data.get(ingredient_name, {})
    interactions = sdata.get("drug_interactions", [])
    side_fx = sdata.get("side_effects", [])
    contras = sdata.get("contraindications", [])

    if not interactions and not side_fx and not contras:
        return ""

    lines = [f"\n{ingredient_name}:"]
    for di in interactions:
        lines.append(f"  [{di.get('severity', 'Unknown')}] {di.get('drug_class', '')}: {di.get('interaction', '')}")
    if side_fx:
        lines.append(f"  Side Effects: {', '.join(side_fx)}")
    if contras:
        lines.append(f"  Contraindications: {', '.join(contras)}")
    return "\n".join(lines) + "\n"


# =============================================================================
# L1: INGREDIENT PROFILE PROMPT
# =============================================================================

def build_l1_ingredient_prompt(ingredient_name, full_data, safety_data, site_config):
    """Build a prompt for generating an L1 Ingredient Profile page.

    Args:
        ingredient_name: Name of the ingredient to profile
        full_data: Complete source intelligence data dict
        safety_data: Safety data dict from source intelligence
        site_config: Site configuration dict from site_configs.py
    """
    ingredient_kb = _load_ingredient_kb()
    product_research = full_data.get("ingredient_research", {})
    enriched = _get_enriched_ingredient(ingredient_name, product_research, ingredient_kb)

    # Also check KB for safety data that might not be in product safety
    kb_key = ingredient_name.lower().strip()
    kb_entry = ingredient_kb.get(kb_key, {})
    merged_safety = {ingredient_name: {
        "side_effects": safety_data.get(ingredient_name, {}).get("side_effects", []) or kb_entry.get("side_effects", []),
        "drug_interactions": safety_data.get(ingredient_name, {}).get("drug_interactions", []) or kb_entry.get("drug_interactions", []),
        "contraindications": safety_data.get(ingredient_name, {}).get("contraindications", []) or kb_entry.get("contraindications", []),
    }}

    site_name = site_config.get("name", "")
    voice = site_config.get("editorial_voice", "")
    byline = site_config.get("byline", "")
    wc_range = site_config.get("l1_word_count_range", site_config.get("word_count_range", (1000, 1500)))
    l1_structure = site_config.get("l1_structure", [
        "What It Is", "What Research Shows", "Dosage & Forms",
        "Safety & Side Effects", "Bottom Line",
    ])
    evidence_grades = site_config.get("evidence_grades", ["Strong", "Moderate", "Preliminary", "Insufficient"])
    niche = site_config.get("niche_focus", "health supplements")

    prompt = f"""You are the {byline} writing team for {site_name}.

EDITORIAL VOICE: {voice}

Write a Layer 1 Atomic Ingredient Profile for: {ingredient_name}

This page will be the ONE definitive page on {site_name} that covers {ingredient_name} in depth.
Every other article that mentions this ingredient will LINK TO this page instead of re-explaining it.

OUTPUT FORMAT:
- Pure HTML (no html/head/body wrapper)
- Start with the editorial disclosure block, then H2 title
- Use H2 for major sections, H3 for subsections
- Include tables where appropriate (use inline styles for borders/padding)
- {wc_range[0]}-{wc_range[1]} words of substantive content
- End with FDA/medical disclaimer in <em> tags

EVIDENCE GRADING SYSTEM (use throughout):
"""
    for grade in evidence_grades:
        prompt += f"- {grade}\n"

    prompt += f"""
STRUCTURE (vary section naming but cover these topics):
"""
    for i, section in enumerate(l1_structure, 1):
        prompt += f"{i}. {section}\n"

    prompt += f"""
EDITORIAL RULES:
- Hedging language: "may support," "research suggests," "evidence indicates"
- Never make definitive health claims
- Compare supplement doses to clinical trial doses (the dose-math approach)
- Call out when evidence is limited, animal-only, or in vitro
- Include negative findings and limitations
- Reference PubMed-indexed research (citations provided below)
- No marketing language, no hype, no superlatives
- Frame within {niche} context

═══════════════════════════════════════════════
RESEARCH DATA FOR {ingredient_name.upper()}
═══════════════════════════════════════════════

Evidence Grade: {enriched['evidence_grade']}
Clinical Dose Range: {enriched['clinical_dose_range'] or 'Not established — verify from clinical literature'}
Studies Found: {len(enriched['studies'])}

--- PubMed Studies (cite these accurately — do NOT fabricate citations) ---
{_format_studies_block(enriched['studies'])}
--- SAFETY DATA ---
{_format_safety_block(ingredient_name, merged_safety)}
═══════════════════════════════════════════════
"""

    if site_config.get("disclaimer_top"):
        prompt += f"\nREQUIRED OPENING DISCLAIMER (include verbatim before content):\n{site_config['disclaimer_top']}\n"
    if site_config.get("disclaimer_bottom"):
        prompt += f"\nREQUIRED CLOSING DISCLAIMER (include verbatim after content):\n{site_config['disclaimer_bottom']}\n"

    return prompt


# =============================================================================
# L3: SAFETY & INTERACTIONS GUIDE PROMPT
# =============================================================================

def build_l3_safety_prompt(full_data, safety_data, site_config):
    """Build a prompt for generating an L3 Safety & Interactions Guide.

    Aggregates all safety data across the product's ingredients.
    """
    product = full_data.get("product", {})
    name = product.get("product_name", "Unknown")
    category = product.get("category", "")
    ingredients = product.get("supplement_facts", {}).get("ingredients", [])
    ingredient_research = full_data.get("ingredient_research", {})
    ingredient_kb = _load_ingredient_kb()

    site_name = site_config.get("name", "")
    voice = site_config.get("editorial_voice", "")
    byline = site_config.get("byline", "")
    wc_range = site_config.get("word_count_range", (1000, 1500))

    prompt = f"""You are the {byline} writing team for {site_name}.

EDITORIAL VOICE: {voice}

Write a Layer 3 Safety & Interactions Guide for the ingredients found in: {name}
Category: {category}

This page covers drug interactions, contraindications, side effects, and who should avoid these ingredients.
This is an EDUCATIONAL safety reference — not a product review.

OUTPUT FORMAT:
- Pure HTML (no html/head/body wrapper)
- H2 for major sections, H3 for per-ingredient subsections
- {wc_range[0]}-{wc_range[1]} words
- Include a summary table of all interactions at the top
- End with FDA/medical disclaimer in <em> tags

STRUCTURE:
1. Overview — What this guide covers and why safety matters
2. Quick Reference Table — All ingredients with interaction severity ratings
3. Per-Ingredient Safety Profiles (one H3 per ingredient):
   - Known drug interactions (with severity: High/Moderate/Low)
   - Side effects at typical supplement doses
   - Contraindications (who should NOT take this)
   - Special populations (pregnancy, elderly, children)
4. Cross-Ingredient Interactions — Do any of these ingredients interact with EACH OTHER?
5. Who Should Avoid This Product Category — Summary of populations at risk
6. When to Consult a Doctor — Clear guidance on medical consultation

EDITORIAL RULES:
- This is a SAFETY page — err on the side of caution
- Use specific medication names and drug classes
- Cite PubMed research where available
- Include severity ratings for all interactions
- Never downplay risks — be transparent and thorough
- Hedging language for uncertain interactions: "may interact," "potential interaction"

═══════════════════════════════════════════════
SAFETY DATA — ALL INGREDIENTS
═══════════════════════════════════════════════
"""

    for ing in ingredients:
        ing_name = ing.get("name", "")
        if not ing_name:
            continue

        prompt += f"\n### {ing_name}\n"
        prompt += f"Amount in product: {ing.get('amount', 'Not disclosed')}\n"
        prompt += f"Form: {ing.get('form', 'Not specified')}\n"

        # Get enriched data
        enriched = _get_enriched_ingredient(ing_name, ingredient_research, ingredient_kb)
        prompt += f"Evidence Grade: {enriched['evidence_grade']}\n"

        # Safety from product research
        safety_block = _format_safety_block(ing_name, safety_data)
        if safety_block:
            prompt += safety_block
        else:
            # Check KB for safety
            kb_key = ing_name.lower().strip()
            kb_entry = ingredient_kb.get(kb_key, {})
            if kb_entry.get("drug_interactions") or kb_entry.get("side_effects") or kb_entry.get("contraindications"):
                kb_safety = {ing_name: {
                    "drug_interactions": kb_entry.get("drug_interactions", []),
                    "side_effects": kb_entry.get("side_effects", []),
                    "contraindications": kb_entry.get("contraindications", []),
                }}
                prompt += _format_safety_block(ing_name, kb_safety)
            else:
                prompt += "  No specific safety concerns identified in research — verify from clinical sources\n"

        # Include top studies for context
        if enriched["studies"]:
            prompt += f"  Supporting studies ({len(enriched['studies'])} total):\n"
            for s in enriched["studies"][:3]:
                prompt += f"    PMID:{s.get('pmid', '')} — {s.get('title', '')} ({s.get('year', '')})\n"

    prompt += "\n═══════════════════════════════════════════════\n"

    if site_config.get("disclaimer_top"):
        prompt += f"\nREQUIRED OPENING DISCLAIMER:\n{site_config['disclaimer_top']}\n"
    if site_config.get("disclaimer_bottom"):
        prompt += f"\nREQUIRED CLOSING DISCLAIMER:\n{site_config['disclaimer_bottom']}\n"

    return prompt


# =============================================================================
# L6: PRODUCT REVIEW PROMPT (DOMAIN SITE)
# =============================================================================

def build_l6_review_prompt(full_data, site_config, intake_fields):
    """Build a prompt for generating an L6 Product Review for a domain site.

    This is the refactored version of the current Export Prompt tab logic,
    enriched with accumulated KB data.

    Args:
        full_data: Complete source intelligence data dict
        site_config: Site configuration dict (or None for generic)
        intake_fields: Dict with platform, affiliate_link, previous_releases, etc.
    """
    product = full_data.get("product", {})
    name = product.get("product_name", "Unknown")
    compliance = full_data.get("compliance", {})
    safety = full_data.get("safety", {})
    ingredient_research = full_data.get("ingredient_research", {})
    pricing = product.get("pricing", [])
    claims = product.get("claims", [])
    rp = product.get("refund_policy", {})
    sf = product.get("supplement_facts", {})
    ingredients = sf.get("ingredients", [])
    ingredient_kb = _load_ingredient_kb()

    # v3.10 intake fields
    prompt = f"""PRODUCT NAME: {name}
OFFICIAL WEBSITE URL: {product.get('official_url', '')}
PUBLISHING PLATFORM: {intake_fields.get('platform', 'Domain Site')}
AFFILIATE LINK: {intake_fields.get('affiliate_link', 'TRAFFIC-FIRST')}
RELEASE TYPE: {intake_fields.get('release_type', 'Single Product')}
YMYL CATEGORY: {intake_fields.get('ymyl_category', 'Yes')}
PREVIOUS RELEASES: {intake_fields.get('previous_releases', 'FIRST RELEASE')}
SOURCE MATERIALS: Source intelligence data provided inline below"""

    # Optional intake fields
    for field, key in [
        ("COMPETITOR RELEASE", "competitor_release"),
        ("EDITOR-LOCKED TITLE", "editor_title"),
        ("SUBTITLE", "subtitle"),
        ("RELEASE SUMMARY (140 chars)", "release_summary"),
        ("RELEASE TAGS", "release_tags"),
    ]:
        val = intake_fields.get(key, "")
        if val:
            prompt += f"\n{field}: {val}"

    # Site-specific editorial instructions
    if site_config:
        prompt += f"""

═══════════════════════════════════════════════
EDITORIAL INSTRUCTIONS ({site_config.get('name', '')})
═══════════════════════════════════════════════
Voice: {site_config.get('editorial_voice', '')}
Byline: {site_config.get('byline', '')}
Word Count: {site_config.get('word_count_range', (1000, 1500))[0]}-{site_config.get('word_count_range', (1000, 1500))[1]} words
"""
        if site_config.get("disclaimer_top"):
            prompt += f"Top Disclaimer (include verbatim): {site_config['disclaimer_top']}\n"
        if site_config.get("disclaimer_bottom"):
            prompt += f"Bottom Disclaimer (include verbatim): {site_config['disclaimer_bottom']}\n"

    # Source intelligence data
    prompt += f"""

════════════════════════════════════════════════════════
SOURCE INTELLIGENCE DATA (Pre-Verified — Use for Phase 0)
════════════════════════════════════════════════════════

Product Name: {name}
Brand: {product.get('brand_name', '')}
Product Type: {product.get('product_type', 'supplement')}
Category: {product.get('category', '')}
Official URL: {product.get('official_url', '')}
Risk Level: {compliance.get('risk_level', 'Unknown')}
AccessWire: {'PASS' if compliance.get('accesswire_blocklist_check', {}).get('passes') else 'FAIL'}
Barchart: {'PASS' if compliance.get('barchart_compliance', {}).get('passes') else 'FAIL'}

--- SUPPLEMENT FACTS ---
"""
    if sf.get("proprietary_blend"):
        prompt += f"PROPRIETARY BLEND — Total: {sf.get('proprietary_blend_total', 'Not disclosed')}\n"

    for ing in ingredients:
        line = f"- {ing.get('name', '')}"
        if ing.get("amount"):
            line += f" — {ing['amount']}"
        if ing.get("daily_value"):
            line += f" ({ing['daily_value']} DV)"
        if ing.get("form"):
            line += f" [Form: {ing['form']}]"
        prompt += line + "\n"
    if not ingredients:
        prompt += "No ingredients extracted — invoke Thin Web Presence Protocol\n"

    # Ingredient research — enriched with KB data
    prompt += "\n--- INGREDIENT RESEARCH (PubMed-Verified, Enriched from Ingredient KB) ---\n"
    for ing_name, ing_data in ingredient_research.items():
        enriched = _get_enriched_ingredient(ing_name, ingredient_research, ingredient_kb)
        prompt += f"\n{ing_name} — Evidence: {enriched['evidence_grade']} — {len(enriched['studies'])} studies\n"
        if enriched.get("product_dose"):
            prompt += f"  Product Dose: {enriched['product_dose']}\n"
        if enriched.get("clinical_dose_range"):
            prompt += f"  Clinical Dose Range: {enriched['clinical_dose_range']}\n"
        for s in enriched["studies"][:8]:
            tier = s.get("quality_tier", "standard").upper()
            prompt += f"  [{tier}] PMID:{s.get('pmid', '')} — {s.get('title', '')} ({s.get('journal', '')}, {s.get('year', '')})\n"
    if not ingredient_research:
        prompt += "No PubMed research available\n"

    # Safety
    prompt += "\n--- DRUG INTERACTIONS & SAFETY ---\n"
    has_safety = False
    for ing_name, sdata in safety.items():
        block = _format_safety_block(ing_name, safety)
        if block:
            has_safety = True
            prompt += block
    if not has_safety:
        prompt += "No significant drug interactions identified\n"

    # Pricing
    prompt += "\n--- PRICING (Verified from live page) ---\n"
    for p in pricing:
        prompt += f"- {p.get('package', '')}: {p.get('price', '')} ({p.get('per_unit', '')}/unit) — Shipping: {p.get('shipping', 'N/A')}\n"
    if not pricing:
        prompt += "No pricing extracted — verify from live page\n"

    # Refund policy
    prompt += "\n--- REFUND POLICY ---\n"
    if rp.get("duration_days"):
        prompt += f"{rp['duration_days']}-day money-back guarantee\n"
        if rp.get("conditions"):
            prompt += f"Conditions: {rp['conditions']}\n"
        if rp.get("verbatim"):
            prompt += f"Verbatim: \"{rp['verbatim']}\"\n"
    else:
        prompt += "No refund policy extracted — verify from live page\n"

    # Shipping
    shipping = product.get("shipping_policy", product.get("shipping", {}))
    if shipping:
        prompt += "\n--- SHIPPING ---\n"
        for k, v in shipping.items():
            if v:
                prompt += f"{k.replace('_', ' ').title()}: {v}\n"

    # Company
    prompt += "\n--- COMPANY / CONTACT ---\n"
    company = product.get("company", {})
    if company:
        for k, v in company.items():
            if v:
                prompt += f"{k}: {v}\n"
    else:
        prompt += f"Name: {product.get('brand_name', name)}\n"
        prompt += f"Website: {product.get('official_url', '')}\n"

    # Marketing claims
    prompt += "\n--- MARKETING CLAIMS (VERBATIM — UNVERIFIED, DO NOT REPUBLISH AS FACT) ---\n"
    for c in claims:
        if isinstance(c, dict):
            prompt += f"- [{c.get('source', 'unknown')}] \"{c.get('claim', '')}\" (Verified: False)\n"
    if not claims:
        prompt += "No marketing claims captured\n"

    # Compliance flags
    flagged = compliance.get("claim_audit", [])
    if flagged:
        prompt += f"\n--- COMPLIANCE FLAGS ({len(flagged)} flagged claims) ---\n"
        for item in flagged:
            prompt += f"FLAGGED: \"{item.get('claim', '')}\"\n"
            for issue in item.get("issues", []):
                prompt += f"  Issue: {issue}\n"
            prompt += f"  Safe Alternative: \"{item.get('safe_alternative', '')}\"\n"

    # Required disclaimers
    req_disclaimers = compliance.get("required_disclaimers", [])
    if req_disclaimers:
        prompt += "\n--- REQUIRED DISCLAIMERS ---\n"
        for d in req_disclaimers:
            prompt += f"- {d}\n"

    # Testimonials
    testimonials = product.get("testimonials", [])
    if testimonials:
        prompt += "\n--- TESTIMONIALS (Reference Only — Do Not Republish as Verified) ---\n"
        for t in testimonials:
            if isinstance(t, dict) and t.get("text"):
                prompt += f"- {t.get('name', 'Anonymous')} ({t.get('location', '')}): \"{t['text'][:300]}...\"\n"

    # Publishing recommendations
    recs = full_data.get("publishing_recommendations", {})
    if recs:
        prompt += "\n--- PUBLISHING RECOMMENDATIONS ---\n"
        for site, info in recs.items():
            prompt += f"- {site}: Category IDs {info.get('category_ids', [])}\n"

    prompt += "\n════════════════════════════════════════════════════════\n"

    return prompt


# =============================================================================
# L6: PRESS RELEASE PROMPT
# =============================================================================

def build_l6_press_release_prompt(full_data, intake_fields):
    """Build a prompt for generating an L6 Product Review as a press release.

    Same data as domain site review but formatted for press release platforms
    (Accesswire, Barchart, Globe Newswire) with platform-specific notes.
    """
    # Reuse the L6 review builder with no site config (press releases don't use site voice)
    prompt = build_l6_review_prompt(full_data, site_config=None, intake_fields=intake_fields)

    # Add platform-specific compliance notes
    platform = intake_fields.get("platform", "")
    compliance = full_data.get("compliance", {})

    notes = "\n--- PLATFORM COMPLIANCE NOTES ---\n"
    if "accesswire" in platform.lower() or "newswire" in platform.lower():
        aw = compliance.get("accesswire_blocklist_check", {})
        if not aw.get("passes"):
            notes += f"WARNING: AccessWire blocklist FAIL — flagged terms: {aw.get('flagged_terms', [])}\n"
            notes += "These terms must be removed or reworded for AccessWire/Newswire submission.\n"
        else:
            notes += "AccessWire blocklist: PASS — no flagged terms detected.\n"
    elif "barchart" in platform.lower():
        bc = compliance.get("barchart_compliance", {})
        if not bc.get("passes"):
            notes += f"WARNING: Barchart compliance FAIL — {bc.get('notes', '')}\n"
        else:
            notes += f"Barchart compliance: PASS — {bc.get('notes', '')}\n"
    elif "globe" in platform.lower():
        notes += "Globe Newswire: Format C default. Brand-as-subject voice. No direct health claims.\n"

    prompt += notes
    return prompt
