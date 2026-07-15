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
# SHARED: SOURCE DATA BLOCK
# =============================================================================

def _build_source_data_block(full_data):
    """Build the source intelligence data section used by all L6 prompts."""
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

    block = f"""
════════════════════════════════════════════════════════
SOURCE MATERIALS (Pre-Verified Research Data)
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
        block += f"PROPRIETARY BLEND — Total: {sf.get('proprietary_blend_total', 'Not disclosed')}\n"

    for ing in ingredients:
        line = f"- {ing.get('name', '')}"
        if ing.get("amount"):
            line += f" — {ing['amount']}"
        if ing.get("daily_value"):
            line += f" ({ing['daily_value']} DV)"
        if ing.get("form"):
            line += f" [Form: {ing['form']}]"
        block += line + "\n"
    if not ingredients:
        block += "No ingredients extracted — invoke Thin Web Presence Protocol\n"

    # Ingredient research — enriched with KB data
    block += "\n--- INGREDIENT RESEARCH (PubMed-Verified) ---\n"
    for ing_name, ing_data in ingredient_research.items():
        enriched = _get_enriched_ingredient(ing_name, ingredient_research, ingredient_kb)
        block += f"\n{ing_name} — Evidence: {enriched['evidence_grade']} — {len(enriched['studies'])} studies\n"
        if enriched.get("product_dose"):
            block += f"  Product Dose: {enriched['product_dose']}\n"
        if enriched.get("clinical_dose_range"):
            block += f"  Clinical Dose Range: {enriched['clinical_dose_range']}\n"
        for s in enriched["studies"][:8]:
            tier = s.get("quality_tier", "standard").upper()
            block += f"  [{tier}] PMID:{s.get('pmid', '')} — {s.get('title', '')} ({s.get('journal', '')}, {s.get('year', '')})\n"
    if not ingredient_research:
        block += "No PubMed research available\n"

    # Safety
    block += "\n--- DRUG INTERACTIONS & SAFETY ---\n"
    has_safety = False
    for ing_name, sdata in safety.items():
        sblock = _format_safety_block(ing_name, safety)
        if sblock:
            has_safety = True
            block += sblock
    if not has_safety:
        block += "No significant drug interactions identified\n"

    # Pricing
    block += "\n--- PRICING (Verified from live page) ---\n"
    for p in pricing:
        block += f"- {p.get('package', '')}: {p.get('price', '')} ({p.get('per_unit', '')}/unit) — Shipping: {p.get('shipping', 'N/A')}\n"
    if not pricing:
        block += "No pricing extracted — verify from live page\n"

    # Refund policy
    block += "\n--- REFUND POLICY ---\n"
    if rp.get("duration_days"):
        block += f"{rp['duration_days']}-day money-back guarantee\n"
        if rp.get("conditions"):
            block += f"Conditions: {rp['conditions']}\n"
        if rp.get("verbatim"):
            block += f"Verbatim: \"{rp['verbatim']}\"\n"
    else:
        block += "No refund policy extracted — verify from live page\n"

    # Shipping
    shipping = product.get("shipping_policy", product.get("shipping", {}))
    if shipping:
        block += "\n--- SHIPPING ---\n"
        for k, v in shipping.items():
            if v:
                block += f"{k.replace('_', ' ').title()}: {v}\n"

    # Company
    block += "\n--- COMPANY / CONTACT ---\n"
    company = product.get("company", {})
    if company:
        for k, v in company.items():
            if v:
                block += f"{k}: {v}\n"
    else:
        block += f"Name: {product.get('brand_name', name)}\n"
        block += f"Website: {product.get('official_url', '')}\n"

    # Marketing claims
    block += "\n--- MARKETING CLAIMS (VERBATIM — UNVERIFIED, DO NOT REPUBLISH AS FACT) ---\n"
    for c in claims:
        if isinstance(c, dict):
            block += f"- [{c.get('source', 'unknown')}] \"{c.get('claim', '')}\" (Verified: False)\n"
    if not claims:
        block += "No marketing claims captured\n"

    # Compliance flags
    flagged = compliance.get("claim_audit", [])
    if flagged:
        block += f"\n--- COMPLIANCE FLAGS ({len(flagged)} flagged claims) ---\n"
        for item in flagged:
            block += f"FLAGGED: \"{item.get('claim', '')}\"\n"
            for issue in item.get("issues", []):
                block += f"  Issue: {issue}\n"
            block += f"  Safe Alternative: \"{item.get('safe_alternative', '')}\"\n"

    # Required disclaimers
    req_disclaimers = compliance.get("required_disclaimers", [])
    if req_disclaimers:
        block += "\n--- REQUIRED DISCLAIMERS ---\n"
        for d in req_disclaimers:
            block += f"- {d}\n"

    # Testimonials
    testimonials = product.get("testimonials", [])
    if testimonials:
        block += "\n--- TESTIMONIALS (Reference Only — Do Not Republish as Verified) ---\n"
        for t in testimonials:
            if isinstance(t, dict) and t.get("text"):
                block += f"- {t.get('name', 'Anonymous')} ({t.get('location', '')}): \"{t['text'][:300]}...\"\n"

    # Publishing recommendations
    recs = full_data.get("publishing_recommendations", {})
    if recs:
        block += "\n--- PUBLISHING RECOMMENDATIONS ---\n"
        for site, info in recs.items():
            block += f"- {site}: Category IDs {info.get('category_ids', [])}\n"

    block += "\n════════════════════════════════════════════════════════\n"
    return block


def _build_serp_stacking_section(previous_releases, competitor_release):
    """Build anti-cannibalization and SERP stacking instructions.

    Only included when previous releases or competitor releases exist.
    """
    has_prev = previous_releases and previous_releases.strip().upper() != "FIRST RELEASE"
    has_comp = competitor_release and competitor_release.strip()

    if not has_prev and not has_comp:
        return ""

    section = """
═══════════════════════════════════════════════
SERP STACKING & ANTI-CANNIBALIZATION STRATEGY
═══════════════════════════════════════════════
"""

    if has_prev:
        section += f"""
PREVIOUS RELEASES TO DIFFERENTIATE FROM:
{previous_releases}

CRITICAL — DO NOT CANNIBALIZE:
You MUST create content that targets a DIFFERENT search intent than the previous release(s) listed above.
Before writing, analyze what angles and keywords the previous release(s) likely target, then deliberately
choose a different content angle. The goal is SERP stacking — multiple releases that each rank for
different queries and collectively dominate the SERP landscape for this product.

CONTENT DIVERSIFICATION RULES:
- If previous release was a standard review → write an ingredients deep-dive, safety guide, or comparison
- If previous release was ingredients-focused → write a buyer's guide, side effects analysis, or "who should avoid" angle
- If previous release was a comparison → write a standalone investigative review or ingredients breakdown
- NEVER use the same title structure, H2 pattern, or intro angle as a previous release
- Each release must target at least 3 unique long-tail keywords not covered by previous releases

INTER-RELEASE LINKING STRATEGY:
- Reference previous release(s) with natural anchor text within your article
- Position this new release as complementary: "For our full review, see [previous]" or "We previously examined [X], and now we're investigating [Y]"
- Each release should make the others stronger — they work as a network, not standalone pieces
"""

    if has_comp:
        section += f"""
COMPETITOR RELEASES TO OUTRANK:
{competitor_release}

COMPETITIVE STRATEGY:
- Study the competitor release angle and deliberately write something MORE useful
- Include information the competitor missed: dose-math comparisons, specific PubMed citations, safety data
- Target their exact keywords PLUS related long-tail queries they missed
- Provide genuine Information Gain — original analysis, unique comparisons, specific findings not in the competitor piece
- Your content should make the competitor release look shallow by comparison
- Do NOT copy or closely paraphrase competitor content — beat them with better research and analysis
"""

    return section


# =============================================================================
# L6: PRODUCT REVIEW PROMPT (DOMAIN SITE)
# =============================================================================

def build_l6_review_prompt(full_data, site_config, intake_fields):
    """Build a COMPLETE, self-contained production prompt for an L6 Product Review.

    This generates a full prompt ready to paste into ANY Claude chat for article
    generation — includes intake fields, editorial instructions, content generation
    rules, anti-cannibalization strategy, and all source materials inline.

    Args:
        full_data: Complete source intelligence data dict
        site_config: Site configuration dict (or None for generic)
        intake_fields: Dict with platform, affiliate_link, previous_releases, etc.
    """
    product = full_data.get("product", {})
    name = product.get("product_name", "Unknown")
    compliance = full_data.get("compliance", {})

    # Determine site-specific values
    if site_config:
        voice = site_config.get("editorial_voice", "")
        byline = site_config.get("byline", "Editorial Team")
        site_name = site_config.get("name", "")
        wc_range = site_config.get("word_count_range", (1000, 1500))
        slug_pattern = site_config.get("slug_pattern", "product-review")
    else:
        voice = "Professional, evidence-based health analysis."
        byline = "Editorial Team"
        site_name = "Domain Site"
        wc_range = (1000, 1500)
        slug_pattern = "product-review"

    # Build intake header
    prompt = f"""═══════════════════════════════════════════════
CONTENT GENERATION BRIEF — {name}
═══════════════════════════════════════════════

PRODUCT NAME: {name}
OFFICIAL WEBSITE URL: {product.get('official_url', '')}
PUBLISHING PLATFORM: {intake_fields.get('platform', 'Domain Site')}
AFFILIATE LINK: {intake_fields.get('affiliate_link', 'TRAFFIC-FIRST')}
RELEASE TYPE: {intake_fields.get('release_type', 'Single Product')}
YMYL CATEGORY: {intake_fields.get('ymyl_category', 'Yes')}
PREVIOUS RELEASES: {intake_fields.get('previous_releases', 'FIRST RELEASE')}
SOURCE MATERIALS: Included inline below"""

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

    # SERP stacking & anti-cannibalization (conditional)
    prompt += _build_serp_stacking_section(
        intake_fields.get("previous_releases", ""),
        intake_fields.get("competitor_release", ""),
    )

    # Editorial voice & content generation instructions
    prompt += f"""

═══════════════════════════════════════════════
CONTENT GENERATION INSTRUCTIONS
═══════════════════════════════════════════════

You are the {byline} for {site_name}. Write a comprehensive product review article.

EDITORIAL VOICE: {voice}

OUTPUT FORMAT:
- Pure HTML output (no html/head/body wrapper), start with H2 as the article title
- {wc_range[0]}-{wc_range[1]} words of substantive content
- Slug pattern: {slug_pattern}
- Include a suggested slug at the top as an HTML comment: <!-- slug: your-slug-here -->
"""

    # Disclaimers
    if site_config and site_config.get("disclaimer_top"):
        prompt += f"""
REQUIRED OPENING DISCLAIMER (include VERBATIM at the very top, before any content):
{site_config['disclaimer_top']}
"""
    if site_config and site_config.get("disclaimer_bottom"):
        prompt += f"""
REQUIRED CLOSING DISCLAIMER (include VERBATIM at the very end, after all content):
{site_config['disclaimer_bottom']}
"""

    prompt += f"""
ARTICLE STRUCTURE (vary section names and order — do NOT use this exact order every time):
- Product overview — what it is, who it's for, what it claims
- Ingredients deep-dive — list each ingredient, its amount, clinical dose comparison, and evidence grade
- How it works / mechanism of action
- Benefits assessment (use hedging: "may support," "research suggests")
- Real considerations — who should NOT use this, limitations, gaps in the formula
- Pricing breakdown — include all package options with per-unit cost
- Refund/guarantee policy details
- Pros and cons (separate bulleted lists)
- Bottom line — balanced editorial verdict
- FAQs (4-5 Q&A pairs using H3 for questions)

CONTENT QUALITY RULES (NON-NEGOTIABLE):
1. HEDGING LANGUAGE THROUGHOUT: "may help," "could support," "believed to," "research suggests"
   — NEVER make definitive health claims
2. DOSE-MATH: For every ingredient, compare the product's dose to clinical trial doses from the research data.
   Call out when a product under-doses or uses proprietary blends that hide individual amounts
3. EVIDENCE GRADING: Reference the evidence grade for each ingredient (Strong/Moderate/Preliminary/Insufficient)
   and be transparent about the quality of supporting research
4. BALANCE IS MANDATORY: Include genuine limitations, "who this is NOT for," and negative observations.
   A one-sided positive review fails Google's Product Review System
5. NO MARKETING LANGUAGE: Do not republish marketing claims as fact. Analyze them skeptically.
   Note which claims are supported by research and which are marketing hype
6. CITE REAL RESEARCH: Reference PubMed studies by PMID from the source data below.
   Do NOT fabricate or hallucinate citations — only cite studies provided in the source materials
7. YMYL COMPLIANCE: This is health content subject to Google's highest scrutiny.
   Named byline, evidence-based analysis, prominent disclaimers, balanced perspective
8. UNIQUE CONTENT: This article must provide Information Gain — original analysis,
   specific findings, dose-math comparisons, and insights not available in other reviews
9. NO FILLER: Get to substance within the first 2-3 sentences. No generic introductions.
   Every paragraph should contain specific, verifiable information
10. AFFILIATE LINK: Use the provided affiliate link for any purchase/CTA links.
    If "TRAFFIC-FIRST" — do not include purchase links, focus on informational value
"""

    # Append source data block
    prompt += _build_source_data_block(full_data)

    prompt += f"""
═══════════════════════════════════════════════
FINAL INSTRUCTIONS
═══════════════════════════════════════════════

Write the complete article now in pure HTML. Follow ALL rules above.
Ensure the content is original, balanced, evidence-graded, and would pass
review by a Google Quality Rater evaluating E-E-A-T for YMYL health content.
"""

    return prompt


# =============================================================================
# L6: PRESS RELEASE PROMPT
# =============================================================================

def build_l6_press_release_prompt(full_data, intake_fields):
    """Build a COMPLETE, self-contained production prompt for a press release.

    Formatted for press release platforms (Accesswire, Barchart, Globe Newswire)
    with platform-specific compliance rules and advertorial structure.
    """
    product = full_data.get("product", {})
    name = product.get("product_name", "Unknown")
    compliance = full_data.get("compliance", {})
    platform = intake_fields.get("platform", "")

    # Build intake header
    prompt = f"""═══════════════════════════════════════════════
PRESS RELEASE CONTENT BRIEF — {name}
═══════════════════════════════════════════════

PRODUCT NAME: {name}
OFFICIAL WEBSITE URL: {product.get('official_url', '')}
PUBLISHING PLATFORM: {platform}
AFFILIATE LINK: {intake_fields.get('affiliate_link', 'TRAFFIC-FIRST')}
RELEASE TYPE: {intake_fields.get('release_type', 'Single Product')}
YMYL CATEGORY: {intake_fields.get('ymyl_category', 'Yes')}
PREVIOUS RELEASES: {intake_fields.get('previous_releases', 'FIRST RELEASE')}
SOURCE MATERIALS: Included inline below"""

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

    # SERP stacking & anti-cannibalization (conditional)
    prompt += _build_serp_stacking_section(
        intake_fields.get("previous_releases", ""),
        intake_fields.get("competitor_release", ""),
    )

    # Platform-specific instructions
    prompt += f"""

═══════════════════════════════════════════════
CONTENT GENERATION INSTRUCTIONS — PRESS RELEASE
═══════════════════════════════════════════════

Write a press release / advertorial for {name} formatted for: {platform}

OUTPUT FORMAT:
- Pure HTML (no html/head/body wrapper)
- 1000-1500 words
- Press release structure: headline, subheadline, dateline, body, boilerplate
- Include a suggested slug as an HTML comment: <!-- slug: your-slug-here -->
"""

    # Platform-specific compliance
    if "barchart" in platform.lower():
        bc = compliance.get("barchart_compliance", {})
        prompt += f"""
BARCHART ADVERTORIAL RULES:
- Compliance status: {'PASS' if bc.get('passes') else 'FAIL — review flagged items'}
- {bc.get('notes', '')}
- Brand-as-subject voice (third person)
- No direct health cure/treat/prevent claims
- Hedging throughout: "may support," "designed to help," "research suggests"
- Include clear disclaimer at bottom
- Advertorial disclosure at top
"""
    elif "accesswire" in platform.lower() or "newswire" in platform.lower():
        aw = compliance.get("accesswire_blocklist_check", {})
        prompt += f"""
ACCESSWIRE/NEWSWIRE RULES:
- Blocklist check: {'PASS' if aw.get('passes') else 'FAIL — flagged terms: ' + str(aw.get('flagged_terms', []))}
- Do NOT use any blocklisted terms — they will cause rejection
- Professional press release tone
- Dateline format: CITY, STATE, Month Day, Year
- Include company boilerplate paragraph at end
- Forward-looking statement disclaimer required
"""
    elif "globe" in platform.lower():
        prompt += """
GLOBE NEWSWIRE RULES:
- Format C default (professional press release)
- Brand-as-subject voice throughout
- No direct health claims
- Include standard press release sections: headline, subheadline, dateline, body, about section, contact info
- Forward-looking statement disclaimer required
"""

    prompt += """
PRESS RELEASE STRUCTURE:
1. HEADLINE — Attention-grabbing but factual, no clickbait
2. SUBHEADLINE — Expands on the headline with key angle
3. DATELINE — City, State, Date
4. OPENING PARAGRAPH — Newsworthy hook: what's notable about this product
5. PRODUCT OVERVIEW — What it is, key ingredients, what it's designed to do
6. INGREDIENT ANALYSIS — Highlight 3-5 key ingredients with evidence grades and dose-math
7. MARKET CONTEXT — How it fits in the category, what differentiates it
8. AVAILABILITY & PRICING — Where to buy, pricing tiers, guarantee
9. ABOUT [BRAND] — Company boilerplate
10. DISCLAIMERS — FDA, forward-looking statements, affiliate disclosure as applicable

CONTENT QUALITY RULES:
1. HEDGING LANGUAGE: "may help," "designed to support," "research suggests" — never definitive health claims
2. EVIDENCE-BASED: Reference specific ingredients and research quality (Strong/Moderate/Preliminary evidence)
3. BALANCED: Include product limitations or gaps — one-sided puff pieces get rejected and rank poorly
4. NO BLOCKLISTED TERMS: Review the compliance data for any flagged terms and avoid them entirely
5. CITE RESEARCH: Reference PubMed studies by PMID from the source data — do NOT fabricate citations
6. YMYL COMPLIANCE: Health content under highest scrutiny — be conservative with claims
7. UNIQUE ANGLE: Each press release needs a distinct hook — not a generic "new product launches" piece
8. AFFILIATE LINK: Use the provided affiliate link for CTA. If "TRAFFIC-FIRST" — focus on brand awareness
"""

    # Append source data block
    prompt += _build_source_data_block(full_data)

    prompt += f"""
═══════════════════════════════════════════════
FINAL INSTRUCTIONS
═══════════════════════════════════════════════

Write the complete press release now in pure HTML. Follow ALL platform rules above.
Ensure the content is original, balanced, evidence-graded, and compliant with
{platform} submission requirements.
"""

    return prompt
