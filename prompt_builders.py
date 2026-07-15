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

def _build_cvd_source_block(full_data):
    """Build pre-verified source data organized by CVD-5 verification categories.

    This maps Source Intelligence research directly to the MBK production system's
    verification framework so Phase 0.0/0.1 can use pre-verified data instead of
    re-fetching everything. The production system's own SERP analysis, archetype
    selection, keyword mapping, and angle differentiation run fresh in real-time.
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

This submission includes pre-verified research data organized by CVD-5
verification categories. Use as your verified-facts inventory for Phase 0.1.
Re-fetch policy pages (refund, shipping, ToS, contact) for currency confirmation.
All SEO strategy, archetype selection, and angle differentiation should be
determined by your own real-time SERP analysis — this data feeds facts only.

"""

    # ── C1: SUPPLEMENT FACTS ──
    if ingredients:
        block += f"C1 — SUPPLEMENT FACTS [CLEARED]\n"
        block += f"Source: {c1_source}\n"
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
        block += "C1 — SUPPLEMENT FACTS [NOT CLEARED]\n"
        block += "No ingredients extracted. Invoke CVD-6 ingredient gap protocol or request label.\n"

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
            if p.get('price'):
                line += f"${p['price']}" if not str(p['price']).startswith('$') else str(p['price'])
            if p.get('per_unit'):
                per = p['per_unit']
                line += f" ({per}/unit)" if not str(per).startswith('$') else f" (${per}/unit)"
            if p.get('shipping'):
                line += f" — Shipping: {p['shipping']}"
            block += line + "\n"
    else:
        block += "C2 — PRICING [NOT CLEARED]\n"
        block += "No pricing extracted. Verify from live checkout page.\n"

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
        block += "C3 — GUARANTEE / REFUND TERMS [NOT CLEARED]\n"
        block += "Not extracted. Re-fetch refund/guarantee policy page.\n"

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
        block += "Re-fetch contact page for phone, email, address.\n"

    # ── C5: LEGAL / CORPORATE ENTITY ──
    block += "\n"
    block += "C5 — LEGAL / CORPORATE ENTITY [PARTIAL]\n"
    block += f"  Operating entity: {product.get('brand_name', name)}\n"
    block += "Re-fetch ToS for complete entity names, copyright holder, retailer disclosure.\n"

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
        block += "C7 — CLINICAL CITATIONS / RESEARCH [NOT CLEARED]\n"
        block += "No PubMed research available.\n"

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
        block += "C10 — SHIPPING / DELIVERY [NOT CLEARED]\n"
        block += "Not extracted. Re-fetch shipping policy page.\n"

    # ── C15: PRODUCT CATEGORY / POSITIONING ──
    block += "\n"
    block += "C15 — PRODUCT CATEGORY / POSITIONING [CLEARED]\n"
    block += f"  Product Type: {product.get('product_type', 'supplement')}\n"
    block += f"  Category: {product.get('category', 'Unknown')}\n"
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
        block += "C19 — SERVING SIZE / SUPPLY DURATION [NOT CLEARED]\n"
        block += "Not extracted. Source from label or brand page before writing.\n"

    # ── COMPLIANCE PRE-CHECK ──
    block += "\n"
    block += "═══ COMPLIANCE PRE-CHECK ═══\n"
    aw = compliance.get("accesswire_blocklist_check", {})
    bc = compliance.get("barchart_compliance", {})
    block += f"R12 Blocklist (ACW/Barchart): {'PASS' if aw.get('passes') else 'FAIL — ' + str(aw.get('flagged_terms', []))}\n"
    block += f"Barchart B1-B4 Overlay: {'PASS' if bc.get('passes') else 'REVIEW — ' + str(bc.get('notes', ''))}\n"
    block += f"Risk Level: {compliance.get('risk_level', 'Unknown')}\n"

    # Flagged claims
    flagged = compliance.get("claim_audit", [])
    if flagged:
        block += f"Flagged Claims ({len(flagged)}):\n"
        for item in flagged:
            block += f"  FLAGGED: \"{item.get('claim', '')}\"\n"
            for issue in item.get("issues", []):
                block += f"    Issue: {issue}\n"
            block += f"    Safe Alternative: \"{item.get('safe_alternative', '')}\"\n"

    # Required disclaimers
    req_disclaimers = compliance.get("required_disclaimers", [])
    if req_disclaimers:
        block += "Required Disclaimers:\n"
        for d in req_disclaimers:
            block += f"  - {d}\n"

    # ── MARKETING CLAIMS (verbatim, for R18/L2 scaffolding) ──
    block += "\n"
    block += "═══ MARKETING CLAIMS (VERBATIM — CVD-1 SOURCE: LIVE URL FETCH) ═══\n"
    for c in claims:
        if isinstance(c, dict):
            block += f"- [{c.get('source', 'unknown')}] \"{c.get('claim', '')}\"\n"
    if not claims:
        block += "No marketing claims captured from product page.\n"

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

    prompt += f"\nSOURCE MATERIALS: Pre-verified research data included below"

    # ── STANDING PRODUCTION AUTHORIZATION (ALWAYS PRESENT) ──
    has_prev = previous and previous.strip().upper() != "FIRST RELEASE"

    prompt += """

═══════════════════════════════════════════════
STANDING PRODUCTION AUTHORIZATION
═══════════════════════════════════════════════

AUTHORIZATION FROM KEVIN — RUN THE FULL PIPELINE, NO STOPS.

This submission comes from our Source Intelligence research system with
pre-verified data. Execute Phase 0 through delivery in ONE pass. The QA
report is the accountability mechanism — not pre-approval gates.

DO NOT stop to ask the operator questions. Use the source materials below
plus your own live research to answer any question that arises during
production. These products are our clients. Find the compliant path
forward — always.
"""

    # Collision handling (conditional on previous releases)
    if has_prev:
        prompt += f"""
── CVD-8 COLLISION PROTOCOL ──
Previous release coverage:
{previous}

1. COLLISION HANDLING: Fetch and read the prior release(s), identify what
   angles, archetypes, and keyword clusters they own, then select the
   strongest available differentiated position. Do not stop for confirmation.

2. ARCHETYPE FLEX: If the initial archetype selection collides or is
   suboptimal after SERP analysis, pivot to whatever archetype produces the
   strongest differentiated coverage. A2 verification-first, B4 long-tail
   complaints, A6 lander-match, ingredients deep-dive, safety angle —
   whatever the live SERP data says will dominate. The archetype serves the
   SERP opportunity, not the other way around. Flex until you find the one
   that wins.

3. CROSS-LINK: Per R9, fetch and read prior release before writing anchor
   text. Link-is-endorsement still applies — no linking to content that
   asserts claims this article corrects.
"""

    if competitor:
        prompt += f"""
── COMPETITOR RELEASES ──
{competitor}
Feed these into your Phase 0.2 SERP gap analysis. Beat them with better
research, unique findings, and genuine information gain from the source
data below.
"""

    # ── MBK PRODUCTION PHILOSOPHY (HARD-CODED — APPLIES TO EVERY SUBMISSION) ──
    prompt += """
═══════════════════════════════════════════════
MBK PRODUCTION PHILOSOPHY — MANDATORY DECISION FRAMEWORK
═══════════════════════════════════════════════

This section is AUTHORITATIVE. Follow this decision framework BEFORE
applying any other rule. When this framework and another rule conflict,
this framework wins. There is exactly ONE situation that produces a HALT:
the product is on the R12 absolute blocklist. Everything else has a
compliant path. Find it.

── RULE 0: THE ONLY HALT ──
Check R12. Is the product on the absolute blocklist (weapons, controlled
substances, explicit content, illegal products)? If YES → HALT. If NO →
there is a compliant path. You MUST find it and execute. No other
condition produces a HALT. Not category mismatches. Not third-party
funnels. Not prior release errors. Not marketing overclaims. NONE of
these are HALTs. They are editorial inputs.

── RULE 1: THESE ARE OUR CLIENTS ──
We do not question whether their product is legitimate. We do not accuse
the brand of fraud, scams, or deception. We verify what we can, document
what we find, and build compliant content around the verified facts.
Every product gets a fair, professional release.

── RULE 2: NEVER STOP. NEVER ASK. ──
Our operators paste this submission and expect a finished release — not
questions they cannot answer. Use the source materials below plus your
own live research to resolve any question. If a verification category is
[NOT CLEARED], go fetch the data yourself. If there is ambiguity, use
editorial judgment to find the compliant path. Document your reasoning
in the QA report. The QA report is the accountability mechanism.

── RULE 3: C15 DECISION TREE (follow this exactly) ──
Step 1: Is the product on the R12 blocklist? → NO → continue.
Step 2: Do the label ingredients match the marketing category exactly?
  → YES → no C15 issue, proceed normally.
  → NO → this is Path C. Proceed with the following editorial approach:

PATH C EXECUTION (label-vs-marketing mismatch):
The mismatch between what is on the label and how the brand markets it
IS the editorial differentiator. This is not a problem — it is the
information gain that makes our content more valuable than the brand's
own marketing. Execute as follows:
  a) Lead with what the LABEL actually contains (verified ingredients)
  b) Frame content around what those ingredients actually do per PubMed
  c) Cite the brand's marketing positioning as context — do not adopt it
     as your own editorial claim
  d) Let the reader see both the verified formula and the marketing
     framing — this IS buyer-protection information gain
  e) If ingredients have ANY legitimate pathway to the marketed benefit
     (e.g., arginine supports blood flow, blood flow supports brain
     function), note the connection with appropriate hedging
  f) Document the C15 Path C analysis in the QA report

PATH A DOES NOT APPLY to supplement category mismatches. Path A exists
ONLY for R12 blocklist products. A nitric oxide formula marketed as brain
health is Path C. An amino acid stack marketed as cognitive support is
Path C. Any legal supplement marketed under any health category is Path C.
The editorial approach handles it — you do not need permission to proceed.

── RULE 4: THIRD-PARTY FUNNELS ARE OUT OF SCOPE ──
Do not research third-party ad funnels, VSLs, or affiliate marketing
pages for this product. Do not reference them. Do not let them influence
any production decision. If you encounter them during SERP analysis,
skip them — they are not the brand's content and they are not relevant
to our release.

Specifically:
  - Fake celebrity endorsements in ad funnels → out of scope, ignore
  - Deepfake videos → out of scope, ignore
  - Misleading VSL claims → out of scope, ignore
  - CVD-4 and CVD-9 apply to OUR content, not to third-party funnels
    we are not writing, referencing, or endorsing
  - Note "third-party ad funnels observed" under Material Limitations
    in QA report if relevant, then move on
  - Our content exists INDEPENDENTLY as the factual alternative

── RULE 5: PREVIOUS RELEASE ERRORS ARE OUR PROCESS ISSUE ──
If a prior release had wrong ingredients, incorrect claims, or factual
errors — that is a failure in our research process, not evidence of a
client scam. This submission includes corrected, re-verified data. Use
it. Do not reference prior errors in the article. Do not treat them as
red flags against the brand.

── RULE 6: CONVERSION-OPTIMIZED THROUGH TRUST ──
Conversions are how we stay in business. The path to conversions is
factual, trustworthy, genuinely helpful content.
  - Never tell the reader to buy
  - Never tell the reader not to buy
  - Never talk negatively about the brand or product
  - Present verified facts, ingredient research, dose comparisons, and
    safety data — then let the reader decide
  - Balance = showing what the product does well AND where it has
    limitations — not being negative, just being complete

── RULE 7: CONTENT INTEGRITY ──
We never fabricate information. We never plagiarize. We never republish
unverified marketing claims as our own editorial position. Every factual
assertion traces back to source materials, PubMed, or live verification.
Hedging language throughout: "may support," "research suggests,"
"evidence indicates."

── RULE 8: AUTONOMOUS EXECUTION ──
Run Phase 0 through delivery in ONE pass. Document every collision,
pivot, C15 analysis, and angle decision in the QA report. Do not pause
for approval at any gate. Do not ask the operator to make decisions the
system was built to make. Deliver the finished release.

═══════════════════════════════════════════════
"""

    # ── PRE-VERIFIED SOURCE DATA (CVD-organized) ──
    prompt += _build_cvd_source_block(full_data)

    return prompt
