"""Approved-release exemplar retrieval.

The corpus is built from MBK's historical publishing workbook.  A row only
qualifies as an approved exemplar when it has a title and a live publisher URL.
Historical rows may teach structure, angle, and platform formatting; they must
never be treated as evidence for facts about the current product.
"""

from __future__ import annotations

import gzip
import json
import os
import re
from collections import Counter
from functools import lru_cache
from urllib.parse import urlparse


_CORPUS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "approved_release_index.json.gz"
)

_PLATFORM_HOSTS = {
    "accesswire": {"accessnewswire.com", "accesswire.com"},
    "barchart": {"barchart.com"},
    "globe": {"globenewswire.com"},
    "newswire": {"newswire.com"},
}

_VERTICAL_TERMS = {
    "financial": {
        "stock", "stocks", "investing", "investment", "investor", "newsletter",
        "portfolio", "dividend", "etf", "trading", "wealth", "retirement",
        "financial", "crypto", "bitcoin",
    },
    "telehealth": {
        "telehealth", "telemedicine", "semaglutide", "tirzepatide", "glp-1",
        "prescription", "doctor", "medical consultation",
    },
    "supplement": {
        "supplement", "ingredients", "capsule", "gummies", "gummy", "formula",
        "vitamin", "probiotic", "nootropic", "weight loss", "blood sugar",
        "testosterone", "joint", "memory", "detox",
    },
    "consumer_electronics": {
        "device", "gadget", "smartwatch", "camera", "charger", "headphones",
        "hearing aid", "air cooler", "vacuum", "portable", "wifi",
        "binocular", "night vision", "projector", "speaker", "smart ring",
    },
    "gambling": {
        "casino", "casinos", "betting", "slots", "sportsbook", "poker",
        "sweepstakes casino",
    },
    "collectible": {
        "coin", "commemorative", "collectible", "medallion", "memorabilia",
    },
    "info_product": {
        "course", "program", "guide", "system", "masterclass", "training",
        "lottery", "strategy",
    },
}

_INTENT_TERMS = {
    "review": {"review", "reviews", "reviewed", "analysis", "examined"},
    "trust": {"scam", "legit", "complaints", "honest", "warning", "red flags"},
    "features": {"features", "includes", "inside", "ingredients", "formula"},
    "pricing": {"price", "pricing", "cost", "discount", "coupon", "offer"},
    "safety": {"side effects", "safety", "risks", "warning", "dangers"},
    "how_it_works": {"how it works", "works", "science", "mechanism"},
    "results": {"results", "benefits", "performance", "returns"},
    "comparison": {" vs ", "alternatives", "comparison", "compared"},
}

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "does", "for",
    "from", "how", "in", "is", "it", "of", "on", "or", "the", "this", "to",
    "what", "with", "review", "reviews", "reviewed", "2023", "2024", "2025",
    "2026",
}


def normalize_platform(value: str = "", live_url: str = "") -> str:
    """Return a stable platform key, preferring the live publisher hostname."""
    host = urlparse(live_url or "").netloc.lower().removeprefix("www.")
    for platform, hosts in _PLATFORM_HOSTS.items():
        if host in hosts:
            return platform

    value = re.sub(r"[^a-z]", "", (value or "").lower())
    if "access" in value or value in {"acceswre", "acceswai"}:
        return "accesswire"
    if "barchart" in value:
        return "barchart"
    if "globe" in value:
        return "globe"
    if "newswire" in value:
        return "newswire"
    return value or "other"


def infer_vertical(*values: str) -> str:
    text = " ".join(v or "" for v in values).lower()
    scores = {
        vertical: sum(2 if " " in term else 1 for term in terms if term in text)
        for vertical, terms in _VERTICAL_TERMS.items()
    }
    best, score = max(scores.items(), key=lambda item: item[1])
    return best if score else "general_consumer"


def infer_intents(title: str) -> list[str]:
    text = f" {title.lower()} "
    intents = [
        intent for intent, terms in _INTENT_TERMS.items()
        if any(term in text for term in terms)
    ]
    return intents or ["overview"]


def _tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", (value or "").lower())
        if len(token) > 2 and token not in _STOP_WORDS
    }


@lru_cache(maxsize=1)
def load_approved_release_index() -> list[dict]:
    if not os.path.exists(_CORPUS_PATH):
        return []
    with gzip.open(_CORPUS_PATH, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload.get("releases", [])


def retrieve_exemplars(
    product_name: str,
    platform: str,
    vertical: str = "",
    source_url: str = "",
    previous_releases: str = "",
    limit: int = 5,
) -> list[dict]:
    """Retrieve structurally useful, approved precedents at no API cost."""
    platform = normalize_platform(platform)
    vertical = vertical or infer_vertical(product_name, source_url)
    query_tokens = _tokens(" ".join((product_name, source_url, previous_releases)))
    query_intents = set(infer_intents(product_name + " " + previous_releases))

    ranked = []
    same_vertical = []
    for release in load_approved_release_index():
        if release.get("platform") != platform:
            continue

        title_tokens = set(release.get("tokens", ()))
        overlap = len(query_tokens & title_tokens)
        union = len(query_tokens | title_tokens) or 1
        token_score = overlap / union
        vertical_score = 1.0 if release.get("vertical") == vertical else 0.0
        intent_score = len(query_intents & set(release.get("intents", ()))) / max(
            len(query_intents), 1
        )
        recency = float(release.get("recency_score", 0))
        score = 8 * token_score + 3 * vertical_score + 2 * intent_score + recency
        if vertical_score:
            same_vertical.append((score, release))
        elif overlap:
            ranked.append((score, release))

    # A same-platform but wrong-vertical article is not a meaningful structural
    # precedent. Use it only when no same-vertical precedent exists at all.
    selected_pool = same_vertical or ranked
    selected_pool.sort(key=lambda item: (-item[0], item[1].get("title", "")))
    return [
        release for _, release in selected_pool[: max(1, min(limit, 8))]
    ]


def format_exemplar_guidance(exemplars: list[dict]) -> str:
    """Format metadata as precedent guidance without importing historical facts."""
    if not exemplars:
        return ""

    title_patterns = Counter(item.get("title_pattern", "") for item in exemplars)
    intents = Counter(
        intent for item in exemplars for intent in item.get("intents", ["overview"])
    )
    lines = [
        "═══ APPROVED PUBLICATION PRECEDENTS — STRUCTURE ONLY ═══",
        f"Matched {len(exemplars)} previously published release(s) on this platform.",
        "These precedents prove that the FORMAT and editorial approach have been",
        "published before. They are not factual sources for the current product.",
        "Never transfer names, prices, claims, results, or other product facts.",
        "",
        "PROVEN SEARCH-INTENT EMPHASIS:",
        "  " + ", ".join(name for name, _ in intents.most_common()),
        "PROVEN TITLE STRUCTURES:",
    ]
    for pattern, _ in title_patterns.most_common(4):
        if pattern:
            lines.append(f"  • {pattern}")
    lines.extend(("", "CLOSEST PUBLISHED REFERENCES:"))
    for item in exemplars:
        lines.append(
            f"  • {item['title']} [{item['platform']}/{item['vertical']}]"
        )
        lines.append(f"    Published URL: {item['live_url']}")
    lines.extend((
        "",
        "Use these only to choose a familiar article shape, reader-question order,",
        "and differentiated SEO angle. Current sealed source records control facts.",
        "═══════════════════════════════════════════════",
        "",
    ))
    return "\n".join(lines)


def build_generation_blueprint(pack: dict, exemplars: list[dict]) -> str:
    """Convert banked precedents and captured context into one locked SEO plan."""
    product = pack.get("product") or {}
    product_name = str(product.get("product_name") or "Product").strip()
    profiles = pack.get("contextual_source_profiles") or []
    prior_profiles = [
        item for item in profiles
        if item.get("source_type") == "previous_release"
    ]
    competitor_profiles = [
        item for item in profiles
        if item.get("source_type") == "competitor_release"
    ]
    context_text = " ".join(
        str(value or "")
        for item in prior_profiles + competitor_profiles
        for value in (
            item.get("title"),
            " ".join(item.get("headings") or []),
            item.get("opening_excerpt"),
        )
    )
    used_intents = set(infer_intents(context_text))
    intent_order = (
        "features",
        "how_it_works",
        "pricing",
        "trust",
        "review",
    )
    selected_intent = next(
        (intent for intent in intent_order if intent not in used_intents),
        "buyer_fit",
    )
    promises = {
        "features": "seller-described features, evidence limits, and buyer fit",
        "how_it_works": "how the seller describes operation and what remains unverified",
        "pricing": "current package pricing, offer gaps, and purchase fit",
        "trust": "source verification, seller transparency, and buyer checks",
        "review": "a source-grounded evaluation for prospective buyers",
        "buyer_fit": "who may find the offer relevant and what to verify first",
    }
    h2_spines = {
        "features": (
            f"What {product_name} Is Designed to Offer",
            "Seller-Described Features in Plain English",
            "What the Source Record Does and Does Not Establish",
            "Current Pricing and Package Information",
            "Who May Find the Offer Worth Evaluating",
            "Material Limitations and Questions to Verify",
            "How to Review the Current Offer",
        ),
        "how_it_works": (
            f"How the Seller Describes {product_name}",
            "The Claimed Operating Approach",
            "What Evidence Is Available",
            "Setup and Use Claims From Seller Materials",
            "Current Pricing and Buyer Fit",
            "Material Limitations and Questions to Verify",
            "The Source-Grounded Takeaway",
        ),
    }
    spine = h2_spines.get(selected_intent, h2_spines["features"])
    avoid = [
        item.get("title") for item in prior_profiles + competitor_profiles
        if item.get("title")
    ]
    previous_urls = [
        item.get("url") for item in prior_profiles if item.get("url")
    ]
    lines = [
        "═══ LOCKED GENERATION BLUEPRINT — DO NOT REDESIGN ═══",
        f"Product: {product_name}",
        f"Platform: {pack.get('intake_manifest', {}).get('publishing_channel', '')}",
        f"Primary SEO intent: {selected_intent}",
        f"Title promise: {promises[selected_intent]}",
        "Use approved Barchart advertorial formatting from the precedent corpus.",
        "SEO strategy is complete. Do not invent a different angle.",
        "Required H2 spine:",
    ]
    lines.extend(f"  {index}. {heading}" for index, heading in enumerate(spine, 1))
    if avoid:
        lines.append("Do not repeat these supplied title promises:")
        lines.extend(f"  • {title}" for title in avoid)
    if previous_urls:
        lines.append(
            "Include exactly one quiet contextual backlink to the supplied "
            f"coverage URL: {previous_urls[0]}"
        )
    lines.extend((
        "Fill this blueprint only with current sealed product facts.",
        "Do not import facts from exemplars, previous coverage, or competitors.",
        "═══════════════════════════════════════════════",
        "",
    ))
    return "\n".join(lines)
