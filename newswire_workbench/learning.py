"""Deterministic gates and durable issue-pattern learning."""

import hashlib
import re


PROMPT_VERSION = "newswire-v1.1"


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
