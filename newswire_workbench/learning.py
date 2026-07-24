"""Deterministic gates and durable issue-pattern learning."""

import hashlib
import re

from .publication_profiles import publication_profile
from .gate_registry import PUBLICATION_BLOCKER_IDS


PROMPT_VERSION = "newswire-v2.1-pre-review-source-contract"

HARD_BLOCKER_RATIONALE = {
    "D1": ("legal_disclosure", "Paid native advertising must be identifiable."),
    "D2": ("deliverable_integrity", "Internal production language cannot publish."),
    "D3": ("factual_accuracy", "A destination cannot be falsely described as official."),
    "D5": ("legal_disclosure", "Material affiliate compensation must be disclosed."),
    "D6": ("financial_claim", "Financial copy cannot imply a guaranteed outcome."),
    "D7": ("financial_disclosure", "Material investment-loss risk must be stated."),
    "D17": ("deliverable_integrity", "Reader-facing output must be rendered HTML."),
    "D18": ("editorial_depth", "Platform-specific reader coverage must be complete."),
    "D19": ("client_advocacy", "The strongest accurate client case must remain primary."),
    "D20": ("source_accuracy", "Material assertions must be grounded in the sealed record."),
}


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
    markdown_residue = re.search(
        r"(?m)\*\*[^*\n]{2,120}\*\*|"
        r"^\s{0,3}#{1,6}\s+\S|^\s*```|```\s*$",
        article,
    )
    escaped_html = re.search(
        r"&(?:amp;)?lt;/?(?:strong|h[1-6]|a|p|ul|ol|li|blockquote)"
        r"\b.{0,500}?&(?:amp;)?gt;",
        article,
        re.I | re.S,
    )
    if markdown_residue or escaped_html or not re.search(
        r"<(?:p|h[1-6]|ul|ol|li|div|blockquote)\b", article, re.I
    ):
        findings.append({
            "id": "D17", "category": "Submission HTML gate",
            "issue": (
                "The deliverable contains Markdown, visible escaped HTML, or "
                "is not publication-ready article HTML."
            ),
            "exact_text": (
                markdown_residue.group(0).strip()
                if markdown_residue else
                escaped_html.group(0) if escaped_html else ""
            ),
            "replacement": (
                "Remove code fences and convert escaped or Markdown markup "
                "into clean rendered article-body HTML."
            ),
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
    profile = publication_profile(platform, vertical)
    depth_floor = profile["hard_floor"]
    depth_label = profile["label"]
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
    caveat_phrases = re.findall(
        r"\b(?:not independently verified|cannot be independently verified|"
        r"no independent (?:proof|testing|verification)|unverified (?:claim|"
        r"claims|outcome|outcomes)|lack(?:s|ing) independent verification)\b",
        re.sub(r"<[^>]+>", " ", article),
        re.I,
    )
    normalized_caveats = [
        re.sub(r"\s+", " ", item.casefold()).strip()
        for item in caveat_phrases
    ]
    repeated_caveat = max(
        (normalized_caveats.count(item) for item in set(normalized_caveats)),
        default=0,
    )
    adversarial_headings = len(re.findall(
        r"<h[23]\b[^>]*>.*?\b(?:claims? versus|claims? vs\.?|"
        r"why .* (?:fails?|doesn.t work)|marketing fiction|the prosecution|"
        r"critical issue|missing or unverified|what (?:is|remains) missing)\b",
        article,
        re.I | re.S,
    ))
    plain_lower = re.sub(r"<[^>]+>", " ", article).casefold()
    negative_case_markers = re.findall(
        r"\b(?:unverified|not verified|not documented|not available|"
        r"does not|cannot|no benefit|inappropriate|conflicts? with|"
        r"lacks?|missing|purchase risk|speculative savings|"
        r"solely on seller marketing)\b",
        plain_lower,
    )
    alternatives_headings = len(re.findall(
        r"<h[23]\b[^>]*>.*?\b(?:alternatives?|instead|other options?)\b",
        article,
        re.I | re.S,
    ))
    alternative_branding = len(re.findall(
        r"\b(?:energy audit|smart thermostat|programmable thermostat|"
        r"LED lighting|insulation|air sealing|smart power strip|"
        r"whole-home surge protection)\b",
        plain_lower,
    ))
    client_case_markers = len(re.findall(
        r"\b(?:best fit|may appeal|may suit|designed for|marketed to|"
        r"key feature|product feature|plug-and-play|current offer|"
        r"ordering information|voltage stabili[sz]ation|power factor|"
        r"dirty electricity|electrical noise|surge reduction|"
        r"green (?:indicator )?light|zero maintenance|24/7 operation|"
        r"setup|installation|single unit|bundle|price|support|"
        r"buyer fit|potential fit|product details|stated mechanism|"
        r"sourced feature|offer details)\b",
        plain_lower,
    ))
    advocacy_imbalance = (
        len(negative_case_markers) >= 16
        and len(negative_case_markers) > max(client_case_markers * 2, 12)
    )
    alternatives_dominate = (
        alternatives_headings
        and alternative_branding >= 10
        and alternative_branding > client_case_markers
    )
    if (
        repeated_caveat > 2
        or adversarial_headings >= 2
        or advocacy_imbalance
        or alternatives_dominate
    ):
        findings.append({
            "id": "D19",
            "category": "Client advocacy drift gate",
            "issue": (
                "The article repeats evidentiary caveats or uses an adversarial "
                "frame that overwhelms the strongest supportable client case "
                f"(repeated caveat: {repeated_caveat}; adversarial headings: "
                f"{adversarial_headings}; negative/value markers: "
                f"{len(negative_case_markers)}/{client_case_markers}; "
                f"alternative markers: {alternative_branding})."
            ),
            "exact_text": "",
            "replacement": (
                "Keep every material limitation, but state each one once. "
                "Use a product-first structure: sourced value and features, "
                "how it works, price/setup, best fit, one consolidated "
                "limitations section, then a clear compliant next action. "
                "Keep alternatives to one short neutral paragraph. Do not use "
                "prosecutorial headings or invent benefits."
            ),
        })
    categorical_external_claims = re.findall(
        r"\b(?:utilities? (?:only|solely|do not|does not)|"
        r"utilities? (?:measure|bill|charge)|"
        r"all residential|no residential|cannot reduce|"
        r"residential (?:electricity )?billing|reactive power|"
        r"kilowatt-hours? regardless|typical (?:home|household)|"
        r"power factor (?:cannot|does not|doesn't)|"
        r"typically ranges? from \$|accounts for roughly \d+%|"
        r"breaks down approximately as)\b",
        plain_lower,
    )
    if len(categorical_external_claims) >= 2:
        findings.append({
            "id": "D20",
            "category": "Source-grounded analysis gate",
            "issue": (
                "The draft relies on multiple categorical technical, market, "
                "pricing, or industry assertions beyond the sealed record."
            ),
            "exact_text": categorical_external_claims[0],
            "replacement": (
                "Remove unsupported external assertions. Build depth from sealed "
                "product facts, attributed positioning, reader questions, "
                "disclosed gaps, and practical verification steps."
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
