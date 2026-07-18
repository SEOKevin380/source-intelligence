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
Globe: {'PASS' if compliance.get('globe_compliance', {}).get('passes', True) else 'FAIL'}

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
        price_val = p.get('price', '') or p.get('total', '')
        block += f"- {p.get('package', '')}: {price_val} ({p.get('per_unit', '')}/unit) — Shipping: {p.get('shipping', 'N/A')}\n"
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

    # Marketing claims — with inline blocking tags
    # Marketing claims — blocked claims silently omitted
    block += "\n--- MARKETING CLAIMS (VERBATIM — UNVERIFIED, DO NOT REPUBLISH AS FACT) ---\n"
    cvd9_claims = compliance.get("cvd9_blocked_claims", [])
    bl_claims = compliance.get("accesswire_blocklist_check", {}).get("blocked_claims", [])
    blocked_set = set()
    for item in cvd9_claims:
        blocked_set.add(item.get("claim", "").lower())
    for item in bl_claims:
        blocked_set.add(item.get("claim", "").lower())
    clean_count = 0
    for c in claims:
        if isinstance(c, dict):
            claim_text = c.get("claim", "")
            if claim_text.lower() not in blocked_set:
                block += f"- [{c.get('source', 'unknown')}] \"{claim_text}\"\n"
                clean_count += 1
    if not claims or clean_count == 0:
        block += "No marketing claims available. Build from ingredient research.\n"

    # CVD-9 blocked claims — disease-reversal claims DROPPED entirely
    if cvd9_claims:
        block += f"\n--- CVD-9 BLOCKED ({len(cvd9_claims)} disease-reversal claims DROPPED) ---\n"
        for item in cvd9_claims:
            block += f"DROPPED: \"{item.get('claim', '')}\"\n"
            block += f"Reason: {item.get('reason', '')}\n"
        block += "Do NOT reintroduce, soften, or attribute these claims.\n"

    # Hedging suggestions for claims that need softened language
    flagged = compliance.get("claim_audit", [])
    if flagged:
        block += f"\n--- HEDGING SUGGESTIONS ({len(flagged)} claims need softened language) ---\n"
        for item in flagged:
            block += f"Original: \"{item.get('claim', '')}\"\n"
            block += f"Suggested: \"{item.get('safe_alternative', '')}\"\n"

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

EDITORIAL PHILOSOPHY (NON-NEGOTIABLE — APPLIES TO ALL CONTENT):
- These are our clients. Never talk negatively about the brand or product.
  Balance means showing what works well AND where limitations exist — not
  being hostile or accusatory.
- Never tell the reader to buy. Never tell the reader not to buy. Present
  verified facts, ingredient research, dose comparisons, and safety data —
  then let the reader decide.
- If the label says one thing and the sales page markets another, lead with
  what the LABEL actually contains and what those ingredients do per research.
  This is buyer-protection information gain, not an accusation against the brand.
- Conversion-optimized through trust: factual, helpful content that connects
  the right reader to the right information is what drives conversions.
- Never fabricate information. Never plagiarize. Every factual assertion must
  trace back to the source materials or PubMed research provided.
- Third-party marketing funnels (VSLs, affiliates) are not the brand's
  editorial position. Do not attribute funnel marketing claims to the brand.
- Affiliate disclaimers MUST use passive voice. The site is not the
  affiliate. Use "This article may contain affiliate links" or
  "Compensation may be received through links in this article." Never
  "We earn" or "Our affiliate links."
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
# PRESS RELEASE: MBK v3.10 VA BRIEF SUBMISSION
# =============================================================================

def _build_cvd_source_block(full_data, platform=""):
    """Build pre-verified source data organized by CVD-5 verification categories.

    This maps Source Intelligence research directly to the MBK production system's
    verification framework. The data replaces Phase 0.0/0.1 entirely — the production
    system must NOT re-fetch or independently verify any URLs. The production system's
    own SERP analysis, archetype selection, keyword mapping, and angle differentiation
    run fresh in real-time.

    Platform-aware: adjusts compliance pre-check section based on target platform.
    """
    from datetime import date

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
    today = date.today().strftime("%B %d, %Y")

    # Determine C1 source type
    c1_source = sf.get("_source", "live page extraction")
    if c1_source == "auto_label_ocr":
        c1_source = "label image OCR (auto-detected on product page)"
    elif c1_source == "label_upload":
        c1_source = "uploaded label image OCR"

    block = f"""
═══════════════════════════════════════════════
SOURCE INTELLIGENCE — PRE-VERIFIED DATA
═══════════════════════════════════════════════

Research Date: {today}
Source Tool: MBK Source Intelligence Tool
Data Sources: Live page fetch + PubMed API + Claude vision OCR
Official URL: {product.get('official_url', '')}

PHASE 0.0 STATUS: COMPLETED EXTERNALLY
The Source Intelligence Tool has already performed live page fetches on the
official URL, policy pages, and affiliate link — satisfying CVD-1 live-source
requirements. The verified data is organized below by CVD-5 categories.
Re-fetching these URLs is unnecessary and would duplicate completed work.
All SEO strategy, archetype selection, and angle differentiation should be
determined by your own real-time SERP analysis — this data feeds facts only.

Marketing claims below have been pre-screened. Claims that failed compliance
review (CVD-9 disease-reversal language, R12 banned terms, Globe A-K phrases)
have already been removed — only publishable claims are included. Claims
marked with hedging suggestions should use the softened version provided.

WORKFLOW:
1. Phase 0.0 (source-page fetch) is complete — use the CVD categories below
   as your verified-facts inventory for Phase 0.1.
2. Proceed through all remaining phases to finished draft output.
3. Do NOT pause for confirmation between phases — the operator is a VA who
   cannot answer mid-process questions.
4. If you notice data conflicts, gaps, or discrepancies: document them in
   the Material Limitations section and continue drafting with the best
   available data.
5. Output a complete, publish-ready draft in a single response.

HANDLING INCOMPLETE DATA:
Some CVD categories below may show "NO DATA" — this means the information
was not available at research time. This is normal for first-to-market
releases where source data is incomplete. Write the release using every
fact that IS available:
- No ingredients? Write from product positioning, claims, and category.
- No pricing? Omit pricing references, direct reader to official site.
- No guarantee details? Omit guarantee claims from draft.
- No contact info? Use brand name only.
- No clinical research? Write from claims and positioning, not studies.
Never fabricate missing data. Note gaps in Material Limitations and
deliver the finished draft.

"""

    # Platform-specific guidance appended to the header
    is_globe_platform = "globe" in (platform or "").lower()
    is_barchart_platform = "barchart" in (platform or "").lower()

    if is_globe_platform:
        block += """GLOBE NEWSWIRE PLATFORM NOTES:
- Format C is the DEFAULT. No CTAs, no FAQ, no affiliate disclosure in opening.
- Voice: Brand-as-subject (Rule 1). Every sentence has the brand as subject, never
  as object being observed. '[Brand] is X' — never 'According to the brand, X.'
- Claims: Mechanism-forward (Rule 2). 'is designed to support...' — never bare
  outcome verbs like 'boosts' or 'reduces.'
- Categories A-K phrase blocklist is in effect (see compliance pre-check below).
- Compensation disclosure: ONE instance at end of release, exact Format C text.
- Related Links: single outbound link at end, no CTA language.
- The R12 sexual/performance blocklist does NOT apply to Globe.
- Attribution like 'according to the company' or 'the brand states' is a confirmed
  Globe rejection trigger. State facts directly.

"""
    elif is_barchart_platform:
        block += """BARCHART PLATFORM NOTES:
- Inherits ACW/NW R-rules with B1-B4 overlay.
- B1: Zero schema (no Review, AggregateRating, FAQPage, etc.)
- B2: Zero platform furniture (no sidebar, header, or nav references)
- B3: 'Fake Testimonial Hype' title pattern is confirmed approved on Barchart.
- B4: R12 blocklist applies in full.

"""

    # Telehealth/prescription product context — changes the safety calculus
    product_type = product.get("product_type", "supplement")
    if product_type == "telehealth":
        block += """PRODUCT TYPE: TELEHEALTH / PRESCRIPTION
This is a prescription product dispensed under licensed physician supervision,
NOT an unregulated over-the-counter supplement. The prescribing physician makes
dosing and combination decisions for each patient individually. Content should
reflect this distinction — the article covers the telehealth program and its
offerings, not self-medication advice. Drug interaction data in C6 below is
provided for reader awareness, not as a contraindication for the product itself.

"""

    # ── C1: SUPPLEMENT FACTS ──
    if ingredients:
        block += f"C1 — SUPPLEMENT FACTS [CLEARED]\n"
        block += f"Source: {c1_source}\n"
        # If sourced from DSLD, show the matched product for transparency
        if c1_source == "dsld_verified":
            dsld_name = sf.get("_dsld_match_name", "")
            dsld_brand = sf.get("_dsld_match_brand", "")
            dsld_id = sf.get("_dsld_id", "")
            if dsld_name:
                block += f"DSLD Match: \"{dsld_name}\" by {dsld_brand} (Label ID: {dsld_id})\n"
                block += "Note: DSLD data is from the NIH Dietary Supplement Label Database. If the\n"
                block += "matched product name differs significantly from the product being reviewed,\n"
                block += "re-fetch ingredients from the live product page or label image instead.\n"
        if sf.get("serving_size"):
            block += f"Serving Size: {sf['serving_size']}\n"
        if sf.get("servings_per_container"):
            block += f"Servings Per Container: {sf['servings_per_container']}\n"
        if sf.get("proprietary_blend"):
            block += f"PROPRIETARY BLEND — Total: {sf.get('proprietary_blend_total', 'Not disclosed')}\n"
        block += "\n"
        for ing in ingredients:
            line = f"  {ing.get('name', '')}"
            if ing.get("amount"):
                line += f" — {ing['amount']}"
            if ing.get("daily_value"):
                line += f" ({ing['daily_value']} DV)"
            if ing.get("form"):
                line += f" [Form: {ing['form']}]"
            block += line + "\n"
    else:
        block += "C1 — SUPPLEMENT FACTS [NO DATA]\n"
        block += "No ingredients extracted from available sources. Do not fabricate.\n"
        block += "Write from product positioning and available claims instead.\n"

    # ── C2: PRICING ──
    block += "\n"
    if pricing:
        block += "C2 — PRICING [CLEARED]\n"
        # Determine pricing source
        pricing_source = "live page extraction"
        if any(p.get("_source") == "buygoods" for p in pricing):
            pricing_source = "BuyGoods checkout link data attributes"
        block += f"Source: {pricing_source}\n\n"
        for p in pricing:
            line = f"  {p.get('package', '')}: "
            price_val = p.get('price', '') or p.get('total', '')
            if price_val:
                line += f"${price_val}" if not str(price_val).startswith('$') else str(price_val)
            if p.get('per_unit'):
                per = p['per_unit']
                line += f" ({per}/unit)" if not str(per).startswith('$') else f" (${per}/unit)"
            if p.get('shipping'):
                line += f" — Shipping: {p['shipping']}"
            block += line + "\n"
    else:
        block += "C2 — PRICING [NO DATA]\n"
        block += "No pricing extracted. Omit pricing references from draft.\n"

    # ── C3: GUARANTEE / REFUND ──
    block += "\n"
    if rp.get("duration_days"):
        block += "C3 — GUARANTEE / REFUND TERMS [CLEARED]\n"
        block += f"Source: live page extraction\n"
        block += f"Duration: {rp['duration_days']}-day money-back guarantee\n"
        if rp.get("conditions"):
            block += f"Conditions: {rp['conditions']}\n"
        if rp.get("verbatim"):
            block += f"Verbatim: \"{rp['verbatim']}\"\n"
    else:
        block += "C3 — GUARANTEE / REFUND TERMS [NO DATA]\n"
        block += "Not extracted. Omit guarantee claims from draft.\n"

    # ── C4: CONTACT INFORMATION ──
    block += "\n"
    company = product.get("company", {})
    has_contact = bool(company and any(company.values()))
    if has_contact:
        block += "C4 — CONTACT INFORMATION [CLEARED]\n"
        block += f"Source: live page extraction\n"
        for k, v in company.items():
            if v:
                block += f"  {k}: {v}\n"
    else:
        block += "C4 — CONTACT INFORMATION [PARTIAL]\n"
        block += f"  Brand: {product.get('brand_name', name)}\n"
        block += f"  Website: {product.get('official_url', '')}\n"
        block += "Limited contact info available. Use what is provided.\n"

    # ── C5: LEGAL / CORPORATE ENTITY ──
    block += "\n"
    block += "C5 — LEGAL / CORPORATE ENTITY [PARTIAL]\n"
    block += f"  Operating entity: {product.get('brand_name', name)}\n"
    block += "Use brand name only. Do not fabricate corporate entity details.\n"

    # ── C7: CLINICAL CITATIONS / RESEARCH ──
    block += "\n"
    if ingredient_research:
        total_studies = sum(len(d.get("studies", [])) for d in ingredient_research.values())
        block += f"C7 — CLINICAL CITATIONS / RESEARCH [CLEARED — {total_studies} studies across {len(ingredient_research)} ingredients]\n"
        block += "Source: PubMed API queries + ingredient knowledge base\n"
        block += "Note: Ingredient-level research, not finished-product clinical trials.\n\n"
        for ing_name, ing_data in ingredient_research.items():
            enriched = _get_enriched_ingredient(ing_name, ingredient_research, ingredient_kb)
            block += f"  {ing_name}\n"
            block += f"    Evidence Grade: {enriched['evidence_grade']}\n"
            if enriched.get("product_dose"):
                block += f"    Product Dose: {enriched['product_dose']}\n"
            if enriched.get("clinical_dose_range"):
                block += f"    Clinical Dose Range: {enriched['clinical_dose_range']}\n"
            for s in enriched["studies"][:8]:
                tier = s.get("quality_tier", "standard").upper()
                block += f"    [{tier}] PMID:{s.get('pmid', '')} — {s.get('title', '')} ({s.get('journal', '')}, {s.get('year', '')})\n"
            block += "\n"
    else:
        block += "C7 — CLINICAL CITATIONS / RESEARCH [NO DATA]\n"
        block += "No PubMed research available. Write from product claims and positioning.\n"
        block += "Do not cite studies that were not provided.\n"

    # ── C6: DRUG INTERACTIONS ──
    block += "\n"
    has_safety = False
    safety_lines = []
    for ing_name, sdata in safety.items():
        sblock = _format_safety_block(ing_name, safety)
        if sblock:
            has_safety = True
            safety_lines.append(sblock)
    if has_safety:
        block += f"C6 — DRUG INTERACTIONS [CLEARED]\n"
        block += "Source: PubMed safety queries + interaction databases\n"
        for sl in safety_lines:
            block += sl
    else:
        block += "C6 — DRUG INTERACTIONS [MINIMAL]\n"
        block += "No significant drug interactions identified in automated research.\n"
        block += "Verify manually for high-risk ingredient combinations.\n"

    # ── C10: SHIPPING ──
    block += "\n"
    shipping = product.get("shipping_policy", product.get("shipping", {}))
    if shipping and any(shipping.values()):
        block += "C10 — SHIPPING / DELIVERY [CLEARED]\n"
        block += "Source: live page extraction\n"
        for k, v in shipping.items():
            if v:
                block += f"  {k.replace('_', ' ').title()}: {v}\n"
    else:
        block += "C10 — SHIPPING / DELIVERY [NO DATA]\n"
        block += "Not extracted. Omit shipping details or direct reader to official site.\n"

    # ── C15: PRODUCT CATEGORY / POSITIONING ──
    block += "\n"
    block += "C15 — PRODUCT CATEGORY / POSITIONING [CLEARED]\n"
    block += f"  Product Type: {product.get('product_type', 'supplement')}\n"
    block += f"  Category: {product.get('category', 'Unknown')}\n"
    # Show category override if it happened
    cat_override = product.get("_category_override")
    if cat_override:
        block += f"  Category Override: {cat_override['original']} → {cat_override['corrected']}\n"
        block += f"  Override Reason: {cat_override['reason']}\n"
        block += "  NOTE: The Source Intelligence Tool detected a mismatch between the\n"
        block += "  auto-detected category and the actual ingredient profile. The category\n"
        block += "  has been corrected based on ingredient analysis. Handle any remaining\n"
        block += "  label-vs-marketing discrepancy editorially per C15 protocol.\n"
    # Flag potential label-vs-copy discrepancies
    if sf.get("_source") == "auto_label_ocr" and claims:
        block += "  NOTE: Ingredients sourced from label OCR — verify against sales page claims for C15 Path C analysis.\n"

    # ── C19: SERVING SIZE / SUPPLY DURATION ──
    block += "\n"
    if sf.get("serving_size") or sf.get("servings_per_container"):
        block += "C19 — SERVING SIZE / SUPPLY DURATION [CLEARED]\n"
        if sf.get("serving_size"):
            block += f"  Serving Size: {sf['serving_size']}\n"
        if sf.get("servings_per_container"):
            block += f"  Servings Per Container: {sf['servings_per_container']}\n"
    else:
        block += "C19 — SERVING SIZE / SUPPLY DURATION [NO DATA]\n"
        block += "Not extracted. Omit serving size claims from draft.\n"

    # ── COMPLIANCE PRE-CHECK (platform-aware) ──
    block += "\n"
    block += "═══ COMPLIANCE PRE-CHECK ═══\n"
    platform_lower = platform.lower() if platform else ""
    is_globe = "globe" in platform_lower
    is_barchart = "barchart" in platform_lower
    is_acw = "accesswire" in platform_lower or "newswire" in platform_lower

    aw = compliance.get("accesswire_blocklist_check", {})
    bc = compliance.get("barchart_compliance", {})
    gc = compliance.get("globe_compliance", {})

    if is_globe:
        # Globe-specific compliance
        block += f"Globe v1.12 Phrase Blocklist: {'PASS' if gc.get('passes') else 'FAIL'}\n"
        if not gc.get("passes"):
            for cat, terms in gc.get("flagged_categories", {}).items():
                cat_label = cat.split("_", 1)[-1].replace("_", " ").title() if "_" in cat else cat
                block += f"  Category {cat.split('_')[0]}: {', '.join(terms)}\n"
            block += "  NOTE: These terms/phrases are confirmed Globe rejection triggers.\n"
            block += "  The production system must avoid them entirely — no rewording fixes them.\n"
        block += "Globe Format: Format C (default — no CTAs, no FAQ, no affiliate disclosure in opening)\n"
        block += "Globe Voice: Brand-as-subject (Rule 1) + Mechanism-forward (Rule 2)\n"
        block += "  All attribution must be direct: '[Brand] is X' — never 'according to the brand'\n"
        block += "  All claims use mechanism language: 'is designed to support...' — never bare outcome verbs\n"
    elif is_barchart:
        # Barchart inherits ACW R-rules + B1-B4 overlay
        block += f"R12 Blocklist (ACW/Barchart): {'PASS' if aw.get('passes') else 'FAIL — avoid these terms in output: ' + str(aw.get('flagged_terms', []))}\n"
        block += f"Barchart B1-B4 Overlay: {'PASS' if bc.get('passes') else 'REVIEW — ' + str(bc.get('notes', ''))}\n"
    else:
        # Accesswire / Newswire.com
        block += f"R12 Blocklist (ACW): {'PASS' if aw.get('passes') else 'FAIL — avoid these terms in output: ' + str(aw.get('flagged_terms', []))}\n"

    block += f"Risk Level: {compliance.get('risk_level', 'Unknown')}\n"

    # CVD-9 blocked claims — disease-reversal claims that were DROPPED entirely
    cvd9 = compliance.get("cvd9_blocked_claims", [])
    if cvd9:
        block += f"\nCVD-9 BLOCKED ({len(cvd9)} disease-reversal claims DROPPED — not hedgeable):\n"
        for item in cvd9:
            block += f"  DROPPED: \"{item.get('claim', '')}\"\n"
            block += f"  Reason: {item.get('reason', '')}\n"
        block += "These claims have been removed from the marketing claims section below.\n"
        block += "Do NOT reintroduce, soften, hedge, or attribute them — they are excluded.\n"

    # Flagged claims — hedging suggestions (not blocked, just need softened language)
    flagged = compliance.get("claim_audit", [])
    if flagged:
        block += f"\nHedging Suggestions ({len(flagged)} claims need softened language):\n"
        for item in flagged:
            block += f"  Original: \"{item.get('claim', '')}\"\n"
            block += f"  Suggested: \"{item.get('safe_alternative', '')}\"\n"

    # Required disclaimers (Globe has different disclaimer rules)
    req_disclaimers = compliance.get("required_disclaimers", [])
    if req_disclaimers:
        if is_globe:
            block += "\nDisclaimer Notes (Globe Format C):\n"
            block += "  - Opening: regulatory/medical disclaimer ONLY (no affiliate/compensation)\n"
            block += "  - Compensation: ONE instance at end of release, exact Format C text\n"
            block += "  - Related Links: single outbound link, no CTA language\n"
        else:
            block += "Required Disclaimers:\n"
            for d in req_disclaimers:
                block += f"  - {d}\n"

    # ── MARKETING CLAIMS — blocked claims silently omitted ──
    block += "\n"
    if is_globe:
        block += "═══ MARKETING CLAIMS (MECHANISM-FORWARD REWRITE REQUIRED FOR GLOBE) ═══\n"
    else:
        block += "═══ MARKETING CLAIMS (VERBATIM — SOURCE: LIVE URL FETCH) ═══\n"

    # Build sets of blocked claim texts to silently exclude
    cvd9 = compliance.get("cvd9_blocked_claims", [])
    # R12 blocklist claims — only apply to ACW/Barchart, NOT Globe
    # (per Globe v1.12: "the R12 sexual/performance blocklist belongs to the
    # Accesswire-side system and never applies to Globe")
    bl_blocked = aw.get("blocked_claims", []) if not is_globe else []
    # Globe-specific blocked claims when targeting Globe
    gl_blocked = gc.get("blocked_claims", []) if is_globe else []
    blocked_texts = set()
    for item in cvd9:
        blocked_texts.add(item.get("claim", "").lower())
    for item in bl_blocked:
        blocked_texts.add(item.get("claim", "").lower())
    for item in gl_blocked:
        blocked_texts.add(item.get("claim", "").lower())
    clean_count = 0
    for c in claims:
        if isinstance(c, dict):
            claim_text = c.get("claim", "")
            if claim_text.lower() not in blocked_texts:
                block += f"- [{c.get('source', 'unknown')}] \"{claim_text}\"\n"
                clean_count += 1
    if not claims or clean_count == 0:
        block += "No marketing claims available. Build from ingredient research.\n"

    # ── TESTIMONIALS (reference only) ──
    testimonials = product.get("testimonials", [])
    if testimonials:
        block += f"\n═══ TESTIMONIALS ({len(testimonials)} — C9 reference, not independently verified) ═══\n"
        for t in testimonials:
            if isinstance(t, dict) and t.get("text"):
                block += f"- {t.get('name', 'Anonymous')} ({t.get('location', '')}): \"{t['text'][:300]}\"\n"

    block += "\n═══════════════════════════════════════════════\n"
    return block


def build_l6_press_release_prompt(full_data, intake_fields):
    """Build an MBK v3.10 production submission for press release platforms.

    Generates a complete intake submission with pre-verified source intelligence
    that maps directly to the MBK production system's CVD-5 verification
    categories. Paste directly into the platform project (Barchart, ACW,
    Newswire, Globe) — the system runs the full pipeline autonomously:
    real-time SERP analysis, archetype selection, angle differentiation,
    drafting, gate check, and delivery in ONE pass.

    The submission includes:
    1. MBK v3.10 intake header (exact field-for-field match)
    2. Pre-verified source data organized by CVD-5 verification categories
    3. Standing authorization for autonomous CVD-8 collision handling + archetype flex
    No production rules or article-writing instructions — the project has those.
    SEO strategy is determined in real-time by the production system, never pre-baked.
    """
    product = full_data.get("product", {})
    name = product.get("product_name", "Unknown")
    platform = intake_fields.get("platform", "")
    previous = intake_fields.get("previous_releases", "FIRST RELEASE")
    competitor = intake_fields.get("competitor_release", "")

    # ── MBK v3.10 INTAKE HEADER ──
    prompt = f"""PRODUCT NAME: {name}
OFFICIAL WEBSITE URL: {product.get('official_url', '')}
PUBLISHING PLATFORM: {platform}
AFFILIATE LINK: {intake_fields.get('affiliate_link', 'TRAFFIC-FIRST')}
RELEASE TYPE: {intake_fields.get('release_type', 'Single Product')}
YMYL CATEGORY: {intake_fields.get('ymyl_category', 'Yes')}
PREVIOUS RELEASES: {previous}"""

    # Optional intake fields
    if competitor:
        prompt += f"\nCOMPETITOR RELEASE: {competitor}"
    for field, key in [
        ("EDITOR-LOCKED TITLE", "editor_title"),
        ("SUBTITLE", "subtitle"),
        ("RELEASE SUMMARY (140 chars)", "release_summary"),
        ("RELEASE TAGS", "release_tags"),
    ]:
        val = intake_fields.get(key, "")
        if val:
            prompt += f"\n{field}: {val}"

    # Operator notes (verified contact info, special instructions, etc.)
    notes = intake_fields.get("notes", "").strip()
    if notes:
        prompt += f"\nOPERATOR NOTES: {notes}"

    prompt += f"\nSOURCE MATERIALS: Pre-verified research data included below"

    has_prev = previous and previous.strip().upper() != "FIRST RELEASE"

    # Collision context (when previous releases exist)
    if has_prev:
        prompt += f"""
PREVIOUS RELEASE COVERAGE: {previous}
"""

    if competitor:
        prompt += f"""
COMPETITOR RELEASE(S): {competitor}
"""

    # ── PRE-VERIFIED SOURCE DATA (CVD-organized) ──
    prompt += _build_cvd_source_block(full_data, platform=platform)

    return prompt
