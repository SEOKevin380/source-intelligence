"""Prompt registry for the newsroom generation and compliance workflow."""

import json


PLATFORMS = ("AccessNewsWire", "Barchart Advertorial")

VERTICAL_TERMS = {
    "health": ("supplement", "telehealth", "vitamin", "ingredient", "serving size"),
    "financial": ("financial", "investment", "stock", "newsletter", "trading"),
    "gaming": ("lottery", "lotto", "gaming", "sweepstakes", "contest"),
    "collectible": ("coin", "collectible", "commemorative", "plated", "memorabilia"),
    "device": ("device", "gadget", "electronics", "power saver", "appliance"),
}


def detect_vertical(source_text: str) -> str:
    lowered = source_text.casefold()
    scores = {
        vertical: sum(lowered.count(term) for term in terms)
        for vertical, terms in VERTICAL_TERMS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] else "general_consumer"


def generation_prompt(source_text: str, platform: str, vertical: str,
                      master_instructions: str) -> str:
    return f"""You are the first-draft writer in a multi-stage editorial system.

Create a complete, publishable {platform} advertorial draft from the supplied
source record. This is a {vertical} assignment. The draft will receive an
independent compliance review before submission.

Operating rules:
- Begin with the finished draft. Do not discuss your process.
- Do not refuse merely because the category is regulated, controversial, or
  evidence-limited. Find the strongest compliant, source-supported angle.
- Never invent facts, first-hand use, endorsements, urgency, scarcity,
  performance, safety, pricing, or guarantees.
- Treat supplied sales pages and VSLs as records of what the advertiser says,
  not automatic proof that a claim is true.
- Preserve commercial intent with accurate attribution, qualification,
  omission, or a supported alternative.
- If facts are missing, omit them or state the limitation naturally. Do not
  pause, ask questions, or request operator approval.
- Write in plain English, use scannable formatting, and maximize defensible
  SEO and conversion value.
- Output article HTML only. Do not include html/head/body wrappers.

Project instructions:
Apply only the portions relevant to this product vertical and selected
platform. Never transfer supplement-specific fields or rules to a financial,
gaming, collectible, device, or general-consumer assignment.
{master_instructions}

Verified source record:
{source_text}
"""


def compliance_prompt(source_text: str, article: str, platform: str,
                      vertical: str, previous_report: dict = None,
                      final: bool = False) -> str:
    prior = json.dumps(previous_report or {}, ensure_ascii=False)
    scope = "final regression review" if final else "comprehensive compliance review"
    return f"""Act as the independent compliance editor for a paid {platform}
advertorial. Perform a {scope} on this {vertical} article.

The objective is compliant publication, not refusal. Identify exact edits that
preserve the strongest supportable commercial and SEO value. Missing evidence
means omit or qualify the claim; it does not justify inventing facts.

Review all applicable categories:
1. Factual traceability and consistency against the source record.
2. Platform disclosures, affiliate wording, CTA accuracy, and advertorial label.
3. Vertical-specific legal/regulatory risks. Apply health rules only to health;
   financial rules only to financial; gaming rules only to gaming; collectible
   and device rules only where relevant.
4. No disease treatment, guaranteed outcome, guaranteed return, fabricated
   testimonial, fake urgency, unsupported ranking, or regulatory implication.
5. Ingredient research is not finished-product evidence. Advertiser statements
   must remain attributed unless independently substantiated.
6. Internal production language must not leak: CVD/C-number codes, Phase 0,
   Source Intelligence, OCR, MBK, Path A/B/C, R/B rule codes, gate checks.
7. Passive affiliate disclosure and neutral CTA wording when a link is not the
   official brand domain.
8. Plain language, scannability, defensible title, search-intent coverage,
   reader-fit, and conversion quality.
9. Never require a VA to make an editorial decision. Supply the exact safe fix.

Return JSON only matching this shape:
{{
  "verdict": "approved" or "not_approved",
  "mandatory_count": integer,
  "source_accuracy": {{"verified": integer, "checked": integer}},
  "mandatory_edits": [{{"id":"M1","category":"...","issue":"...","exact_text":"...","replacement":"..."}}],
  "recommended_edits": [{{"id":"R1","category":"...","issue":"...","replacement":"..."}}],
  "approved_elements": ["..."],
  "notes": ["..."]
}}

Previous review, if any:
{prior}

SOURCE RECORD:
{source_text}

ARTICLE:
{article}
"""


def revision_prompt(source_text: str, article: str, report: dict,
                    platform: str, vertical: str) -> str:
    return f"""Revise the {platform} {vertical} advertorial using the independent
compliance report below.

- Apply every mandatory edit while preserving commercial strength.
- Apply recommended edits that improve clarity, SEO, or conversion without
  adding unsupported facts.
- Do not refuse, debate the assignment, ask questions, or print process notes.
- Do not fabricate facts or first-hand experience.
- Return the complete revised article HTML only.

SOURCE RECORD:
{source_text}

CURRENT ARTICLE:
{article}

COMPLIANCE REPORT:
{json.dumps(report, ensure_ascii=False)}
"""


def seo_prompt(source_text: str, article: str, platform: str,
               vertical: str) -> str:
    return f"""Optimize this already compliant {platform} {vertical} advertorial
for maximum defensible SEO and conversion performance.

- Preserve every factual and compliance limitation.
- Strengthen the title, opening, H2 search intent, scannability, information
  gain, reader-fit language, and CTA spacing.
- Add drama through verified stakes, contrast, specificity, curiosity, and
  consequences—not exaggeration, guarantees, fake urgency, or fear.
- Never call a product perfect for the reader. Explain who it may fit and who
  it may not fit using source-supported facts.
- Do not introduce facts, claims, experiences, testimonials, prices, or terms
  absent from the source record.
- Return complete article HTML only and no process commentary.

SOURCE RECORD:
{source_text}

ARTICLE:
{article}
"""
