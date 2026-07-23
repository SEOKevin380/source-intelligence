"""Deterministic gates and durable issue-pattern learning."""

import hashlib
import re


PROMPT_VERSION = "newswire-v1.1"

PUBLICATION_BLOCKER_IDS = frozenset({
    "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9",
    "D10", "D11", "D12", "D13", "D14", "D17", "D18",
})


def partition_findings(findings):
    """Return material blockers separately from repairable quality findings."""
    blockers, recommendations = [], []
    for item in findings or []:
        (
            blockers if item.get("id") in PUBLICATION_BLOCKER_IDS
            else recommendations
        ).append(item)
    return blockers, recommendations


def issue_fingerprint(category, issue):
    normalized = re.sub(r"[^a-z0-9]+", " ", f"{category} {issue}".casefold()).strip()
    families = (
        ("affiliate_disclosure_cta", ("affiliate", "partner link", "cta", "compensation")),
        ("advertorial_label", ("advertorial", "paid advertising", "paid promotional")),
        ("advertiser_attribution", ("attribut", "source record", "advertiser claim", "settled fact", "editorial fact")),
        ("financial_performance", ("return", "performance", "profit", "outcome", "ranking", "superiority")),
        ("guarantee_scope", ("guarantee", "guaranteed trial", "refund")),
        ("testimonial_scope", ("testimonial", "reader quote", "individual experience")),
        ("urgency_scarcity", ("urgency", "urgent", "scarcity", "timing pressure")),
        ("delivery_terms", ("delivery", "digital format", "print issue", "access method")),
    )
    for family, markers in families:
        if any(marker in normalized for marker in markers):
            return family
    # Remove volatile wording so recurring issue families group together.
    stop = {"the", "article", "draft", "copy", "text", "current", "still"}
    tokens = [t for t in normalized.split() if t not in stop]
    return hashlib.sha256(" ".join(tokens[:24]).encode()).hexdigest()[:20]


def deterministic_findings(article, platform, vertical):
    """Return non-negotiable mechanical issues before model judgment."""
    findings = []
    lowered = article.casefold()
    if re.search(r"^\s*```|```\s*$", article, re.I) or not re.search(
        r"<(?:p|h[1-6]|ul|ol|li|div|blockquote)\b", article, re.I
    ):
        findings.append({
            "id": "D17", "category": "Submission HTML gate",
            "issue": "The deliverable is fenced Markdown or unstructured plain text.",
            "exact_text": "```html" if "```" in article else "",
            "replacement": "Remove code fences and convert the complete draft to article-body HTML.",
        })
    if "advertorial" not in lowered[:1200]:
        findings.append({
            "id": "D1", "category": "Deterministic disclosure gate",
            "issue": "Paid advertorial label is missing near the top.",
            "exact_text": "", "replacement": "<p><strong>Paid Advertorial</strong></p>",
        })
    internal = re.search(
        r"\b(?:source intelligence|label ocr|phase 0(?:\.1)?|mbk|path [abc]|cvd-?\d+|c(?:1|2|15|19)\b|r\d+\b|b[1-4]\b)",
        article, re.I,
    )
    if internal:
        findings.append({
            "id": "D2", "category": "Internal language gate",
            "issue": "Internal production language appears in reader-facing copy.",
            "exact_text": internal.group(0), "replacement": "Remove this internal term.",
        })
    bad_cta = re.search(r"\b(?:official|verified) (?:order|purchase|checkout) page\b", article, re.I)
    if bad_cta:
        findings.append({
            "id": "D3", "category": "Affiliate CTA gate",
            "issue": "Affiliate CTA implies an official destination.",
            "exact_text": bad_cta.group(0), "replacement": "Review the current offer details",
        })
    first_person_disclosure = re.search(
        r"\b(?:we|our|us) (?:may |might |can )?(?:earn|receive|be compensated)", article, re.I
    )
    if first_person_disclosure:
        findings.append({
            "id": "D4", "category": "Affiliate disclosure gate",
            "issue": "Affiliate disclosure uses first-person language.",
            "exact_text": first_person_disclosure.group(0),
            "replacement": "Compensation may be received if a subscription is purchased through links in this advertorial.",
        })
    if "affiliate" in lowered and not re.search(r"compensation may be received|a commission may be earned", lowered):
        findings.append({
            "id": "D5", "category": "Affiliate disclosure gate",
            "issue": "Passive affiliate compensation disclosure is missing.",
            "exact_text": "", "replacement": "Compensation may be received if a subscription is purchased through links in this advertorial.",
        })
    naked_affiliate_anchor = re.search(
        r"<a\b[^>]*>\s*(?:https?://|www\.)[^<]+</a>", article, re.I
    )
    if naked_affiliate_anchor:
        findings.append({
            "id": "D8", "category": "Affiliate CTA presentation gate",
            "issue": "A raw affiliate URL is visible as reader-facing anchor text.",
            "exact_text": naked_affiliate_anchor.group(0),
            "replacement": "Use the same href with concise, product-specific CTA text.",
        })
    routing_explanation = re.search(
        r"[^<.]{0,80}(?:third[- ]party partner|rather than the official|not the official)[^<.]{0,120}[.]?",
        article, re.I,
    )
    if routing_explanation:
        findings.append({
            "id": "D9", "category": "Affiliate disclosure presentation gate",
            "issue": "Reader-facing copy exposes intermediary-link routing mechanics.",
            "exact_text": routing_explanation.group(0).strip(),
            "replacement": "Use a concise passive affiliate disclosure and neutral CTA without discussing link routing.",
        })
    links = list(re.finditer(r"<a\b[^>]*href=[\"'][^\"']+[\"'][^>]*>", article, re.I))
    word_count = len(re.findall(r"\b[\w’'-]+\b", re.sub(r"<[^>]+>", " ", article)))
    depth_floor = 0
    depth_label = ""
    if platform == "AccessNewsWire" and vertical == "financial":
        depth_floor, depth_label = 2200, "financial AccessNewsWire"
    elif platform == "Barchart Advertorial" and vertical == "device":
        depth_floor, depth_label = 2000, "device Barchart"
    if depth_floor and word_count < depth_floor:
        findings.append({
            "id": "D18", "category": "Editorial depth gate",
            "issue": (
                f"The {depth_label} draft is only {word_count} words; it does "
                "not yet provide the expected product-specific reader coverage."
            ),
            "exact_text": "",
            "replacement": (
                f"Expand beyond {depth_floor:,} useful source-grounded words. "
                "Answer who, what, why, how, cost, access or setup, fit, "
                "not-fit, limitations, trust, and the specific product thesis. "
                "Use evidence and explicit limitations, never generic filler."
            ),
        })
    if links and links[0].start() > max(1200, len(article) // 4):
        findings.append({
            "id": "D10", "category": "CTA distribution gate",
            "issue": "The first CTA appears too late for a conversion-focused advertorial.",
            "exact_text": "", "replacement": "Add a clean descriptive CTA near the opening without changing factual claims.",
        })
    if word_count >= 1200 and len(links) < 3:
        findings.append({
            "id": "D11", "category": "CTA distribution gate",
            "issue": "Long-form copy does not distribute enough clean CTAs through the article.",
            "exact_text": "", "replacement": "Use at least three naturally spaced descriptive CTA links in long-form copy.",
        })
    if re.search(r"<h1\b", article, re.I):
        findings.append({
            "id": "D12", "category": "MBK HTML format gate",
            "issue": "The article body contains an H1; WordPress stores the release title separately.",
            "exact_text": "", "replacement": "Move the headline to the WordPress title field and use only H2/H3 in article HTML.",
        })
    unbold_heading = re.search(r"<h[23]\b[^>]*>(?!\s*<strong\b)", article, re.I)
    if unbold_heading:
        findings.append({
            "id": "D13", "category": "MBK HTML format gate",
            "issue": "Every H2/H3 must explicitly wrap its heading text in STRONG.",
            "exact_text": unbold_heading.group(0),
            "replacement": "Use <h2><strong>Heading text</strong></h2> or the H3 equivalent.",
        })
    unbold_cta = re.search(r"<a\b[^>]*>(?!\s*<strong\b)", article, re.I)
    if unbold_cta:
        findings.append({
            "id": "D14", "category": "MBK HTML format gate",
            "issue": "Every CTA link must explicitly bold its anchor text with STRONG.",
            "exact_text": unbold_cta.group(0),
            "replacement": "Preserve the href and wrap the descriptive anchor text in STRONG.",
        })
    bold_key_phrases = len(re.findall(
        r"<strong\b[^>]*class=[\"'][^\"']*\bkey-takeaway\b[^\"']*[\"']",
        article, re.I,
    ))
    bold_target = 10 if word_count < 1600 else 11 if word_count < 2200 else 12
    if bold_key_phrases != bold_target:
        findings.append({
            "id": "D15", "category": "MBK multi-speed reading gate",
            "issue": f"Article has {bold_key_phrases} conversion scan-path phrases; its length calls for {bold_target} to avoid over-formatting.",
            "exact_text": "", "replacement": f"Distribute exactly {bold_target} genuinely useful STRONG.key-takeaway phrases through the article.",
        })
    hrefs = re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"']", article, re.I)
    dominant_link_count = max((hrefs.count(href) for href in set(hrefs)), default=0)
    if platform == "AccessNewsWire" and word_count >= 1200 and not 5 <= dominant_link_count <= 6:
        findings.append({
            "id": "D16", "category": "MBK strategic link gate",
            "issue": f"AccessNewsWire long-form article has {dominant_link_count} affiliate-destination links; MBK requires 5–6.",
            "exact_text": "", "replacement": "Use 5–6 natural, evenly distributed affiliate links with bold descriptive anchors and no raw URLs.",
        })
    if vertical == "financial":
        guaranteed_trial = re.search(r"\bguaranteed trial\b", article, re.I)
        if guaranteed_trial:
            findings.append({
                "id": "D6", "category": "Financial guarantee gate",
                "issue": "Guaranteed-trial wording can imply an investment outcome guarantee.",
                "exact_text": guaranteed_trial.group(0),
                "replacement": "subscription offer with a stated 30-day refund period",
            })
        if not re.search(r"(?:loss of principal|investments? (?:involve|carry|includes?) risk)", lowered):
            findings.append({
                "id": "D7", "category": "Financial risk gate",
                "issue": "Clear investment-loss risk language is missing.",
                "exact_text": "", "replacement": "Investing involves risk, including the possible loss of principal.",
            })
    return findings


def learned_guidance(rows):
    if not rows:
        return "No recurring issue patterns have reached the promotion threshold yet."
    lines = ["Recurring issues from prior independent reviews (prevent these proactively):"]
    for row in rows:
        lines.append(f"- Seen {row['occurrences']} times: {row['category']} — {row['issue']}")
    return "\n".join(lines)
