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

_NICHE_TERMS = {
    "energy_devices": {
        "power saver", "energy saver", "electricity", "voltage", "power factor",
    },
    "cooling_devices": {
        "air cooler", "portable ac", "cooling", "air conditioning",
    },
    "hearing_devices": {"hearing aid", "hearing", "earbuds"},
    "vision_devices": {"binocular", "night vision", "vision", "eyewear"},
    "home_gadgets": {
        "vacuum", "camera", "charger", "projector", "speaker", "wifi",
        "smart ring", "gadget",
    },
    "weight_management": {
        "weight loss", "fat burn", "metabolic", "appetite", "glp-1",
    },
    "blood_sugar": {"blood sugar", "glucose", "glycemic"},
    "brain_cognition": {
        "brain", "memory", "cognitive", "nootropic", "focus",
    },
    "mens_health": {
        "male enhancement", "erectile", "testosterone", "prostate", "men's health",
    },
    "joint_pain": {"joint", "arthritis", "knee", "back pain", "pain relief"},
    "nerve_health": {"nerve", "neuropathy", "sciatica"},
    "dental_health": {"dental", "teeth", "gum", "oral health"},
    "skin_anti_aging": {
        "skin", "wrinkle", "anti-aging", "beauty", "serum", "cream",
    },
    "gut_health": {"gut", "digest", "probiotic", "bloating"},
    "sleep_stress": {"sleep", "stress", "anxiety", "calm"},
    "general_supplements": {
        "supplement", "vitamin", "capsule", "gummies", "formula",
    },
    "telehealth": {
        "telehealth", "telemedicine", "semaglutide", "tirzepatide", "prescription",
    },
    "financial_newsletters": {
        "stock", "investing", "investment", "newsletter", "portfolio",
        "dividend", "trading", "retirement",
    },
    "collectibles_currency": {
        "coin", "commemorative", "collectible", "silver certificate",
        "dollar bill", "$1 bill", "$2 bill",
    },
    "gaming_gambling": {
        "casino", "betting", "slots", "sportsbook", "poker", "lottery",
    },
    "courses_info_products": {
        "course", "program", "guide", "system", "masterclass", "training",
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


def infer_niche(*values: str) -> str:
    """Return the narrow editorial niche used for precedent retrieval."""
    text = " ".join(value or "" for value in values).casefold()
    scores = {
        niche: sum(3 if " " in term else 1 for term in terms if term in text)
        for niche, terms in _NICHE_TERMS.items()
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
    niche = infer_niche(product_name, source_url, previous_releases)
    query_tokens = _tokens(" ".join((product_name, source_url, previous_releases)))
    query_intents = set(infer_intents(product_name + " " + previous_releases))

    ranked = []
    same_vertical = []
    same_niche = []
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
        if release.get("niche") == niche:
            same_niche.append((score + 5, release))
        elif vertical_score:
            same_vertical.append((score, release))
        elif overlap:
            ranked.append((score, release))

    # A same-platform but wrong-vertical article is not a meaningful structural
    # precedent. Use it only when no same-vertical precedent exists at all.
    selected_pool = same_niche or same_vertical or ranked
    selected_pool.sort(key=lambda item: (-item[0], item[1].get("title", "")))
    selected = [
        dict(release)
        for _, release in selected_pool[: max(1, min(limit, 8))]
    ]
    try:
        from body_exemplar_corpus import profiles_by_url
        body_profiles = profiles_by_url()
    except (ImportError, OSError, ValueError, json.JSONDecodeError):
        body_profiles = {}
    for release in selected:
        profile = body_profiles.get(release.get("live_url", ""))
        if profile:
            release["body_profile"] = profile
            release["headings"] = profile.get("heading_sequence", [])
    return selected


def format_exemplar_guidance(exemplars: list[dict]) -> str:
    """Format metadata as precedent guidance without importing historical facts."""
    if not exemplars:
        return ""

    title_patterns = Counter(item.get("title_pattern", "") for item in exemplars)
    intents = Counter(
        intent for item in exemplars for intent in item.get("intents", ["overview"])
    )
    lines = [
        "═══ APPROVED PUBLICATION PRECEDENTS — SEO METADATA ═══",
        f"Matched {len(exemplars)} previously published release(s) on this platform.",
        "These records prove approved title and search-intent precedent. They do",
        "not prove full-body structure unless a body profile is supplied separately.",
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
            f"  • {item['title']} "
            f"[{item['platform']}/{item.get('niche', item['vertical'])}]"
        )
        lines.append(f"    Published URL: {item['live_url']}")
    lines.extend((
        "",
        "Use these for differentiated SEO angles. Current sealed source records",
        "control facts; explicit body profiles control article-shape precedent.",
        "═══════════════════════════════════════════════",
        "",
    ))
    return "\n".join(lines)


def build_approval_playbook(exemplars: list[dict], platform: str,
                            niche: str) -> dict:
    """Summarize repeatable approval signals without importing product facts."""
    patterns = Counter(
        item.get("title_pattern", "") for item in exemplars
        if item.get("title_pattern")
    )
    intents = Counter(
        intent for item in exemplars
        for intent in item.get("intents", ["overview"])
    )
    dates = sorted(
        item.get("published_date", "") for item in exemplars
        if item.get("published_date")
    )
    body_profiles = [item for item in exemplars if item.get("body_profile")]
    return {
        "schema_version": 1,
        "platform": normalize_platform(platform),
        "niche": niche,
        "approved_sample_size": len(exemplars),
        "body_profile_sample_size": len(body_profiles),
        "title_patterns": [name for name, _ in patterns.most_common(5)],
        "accepted_intents": [name for name, _ in intents.most_common()],
        "oldest_approval": dates[0] if dates else "",
        "latest_approval": dates[-1] if dates else "",
        "fact_boundary": (
            "Structure, voice, and SEO approach only. Current sealed sources "
            "remain the exclusive authority for product facts."
        ),
        "source_urls": [item.get("live_url", "") for item in exemplars],
    }


def format_approval_playbook(playbook: dict) -> str:
    if not playbook or not playbook.get("approved_sample_size"):
        return ""
    return "\n".join((
        "═══ PUBLISHER × NICHE APPROVAL PLAYBOOK ═══",
        f"Publisher: {playbook.get('platform', '')}",
        f"Niche: {playbook.get('niche', '')}",
        f"Approved sample: {playbook.get('approved_sample_size', 0)}",
        "Full-body structural profiles: "
        f"{playbook.get('body_profile_sample_size', 0)}",
        f"Observed approval window: {playbook.get('oldest_approval') or 'unknown'} "
        f"to {playbook.get('latest_approval') or 'unknown'}",
        "Accepted search intents: "
        + ", ".join(playbook.get("accepted_intents") or ["overview"]),
        "Observed title structures:",
        *[
            f"  • {pattern}"
            for pattern in playbook.get("title_patterns") or ["No stable pattern"]
        ],
        (
            "Use title and intent precedent aggressively for SEO. Use body "
            "structure only when a full-body profile is explicitly present."
        ),
        playbook["fact_boundary"],
        "═══════════════════════════════════════════════",
        "",
    ))


def build_generation_blueprint(pack: dict, exemplars: list[dict]) -> str:
    """Convert banked precedents and captured context into one locked SEO plan."""
    product = pack.get("product") or {}
    product_name = str(product.get("product_name") or "Product").strip()
    product_type = str(product.get("product_type") or "").strip().casefold()
    niche = infer_niche(
        product_name,
        str(product.get("category") or ""),
        product_type,
    )
    channel = pack.get("intake_manifest", {}).get("publishing_channel", "")
    playbook = build_approval_playbook(exemplars, channel, niche)
    try:
        from body_exemplar_corpus import format_body_playbook
        body_playbook = format_body_playbook(
            normalize_platform(channel), niche
        )
    except (ImportError, OSError, ValueError, json.JSONDecodeError):
        body_playbook = ""
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
        "features": "seller-described features, simple setup, and current pricing",
        "how_it_works": "how the seller describes operation, setup, and timing",
        "pricing": "current package pricing, per-unit value, and product features",
        "trust": "the documented product offer, package identity, and current terms",
        "review": "a clear product overview for prospective buyers",
        "buyer_fit": "the product's simple format, stated features, and current offer",
    }
    headline_patterns = {
        "features": (
            f"{product_name} Review 2026: Plug-In Setup, Features, and Pricing"
        ),
        "how_it_works": (
            f"How {product_name} Is Designed to Work: Features, Setup, and Pricing"
        ),
        "pricing": (
            f"{product_name} Pricing 2026: Packages, Features, and Current Value"
        ),
        "trust": (
            f"{product_name} Buyer Guide 2026: Product Details, Pricing, and Ordering"
        ),
        "review": (
            f"{product_name} Review 2026: Features, Setup, Pricing, and Key Details"
        ),
        "buyer_fit": (
            f"{product_name} 2026: Simple Setup, Stated Features, and Current Offer"
        ),
    }
    h2_spines = {
        "features": (
            f"What {product_name} Is",
            "Why the Simple Plug-In Format Stands Out",
            "How the Seller Describes the Power-Management Features",
            "Setup, Active Operation, and the Stated Optimization Period",
            "Stated Specifications at a Glance",
            "Current Pricing and Bundle Value",
            "What the Documented Order Covers",
            "Important Offer Details",
            f"The Case for {product_name}",
        ),
        "how_it_works": (
            f"What {product_name} Is",
            "The Seller-Described Operating Approach",
            "How Setup and Active Operation Are Described",
            "The Stated Six-to-Eight-Week Period",
            "Features and Specifications at a Glance",
            "Current Pricing and Bundle Value",
            "What the Documented Order Covers",
            "Important Offer Details",
            f"The Case for {product_name}",
        ),
        "pricing": (
            f"What {product_name} Is",
            "The Features Behind the Offer",
            "Simple Setup and Everyday Operation",
            f"Current {product_name} Package Pricing",
            "How the Four-Unit Bundle Changes the Per-Unit Price",
            "What the Documented Order Covers",
            "Important Offer Details",
            f"The Case for {product_name}",
        ),
        "trust": (
            f"What {product_name} Is",
            "The Physical Product and Plug-In Setup",
            "How the Seller Describes Its Features",
            "Stated Specifications and Active Operation",
            "Current Pricing and Package Identity",
            "What the Documented Order Covers",
            "Important Offer Details",
            f"The Case for {product_name}",
        ),
        "review": (
            f"What {product_name} Is",
            "Why the Simple Plug-In Format Stands Out",
            "The Seller-Described Power-Management Features",
            "Setup, Operation, Timing, and Stated Specifications",
            "Current Pricing and Bundle Value",
            "What the Documented Order Covers",
            "Important Offer Details",
            f"The Case for {product_name}",
        ),
        "buyer_fit": (
            f"What {product_name} Is",
            "Why the Low-Maintenance Format May Appeal",
            "How the Product Is Positioned to Operate",
            "Setup, Timing, and Stated Specifications",
            "Current Pricing and Bundle Value",
            "What the Documented Order Covers",
            "Important Offer Details",
            f"The Case for {product_name}",
        ),
    }
    if product_type == "gaming" or niche == "gaming_gambling":
        promises = {
            "features": (
                "the interactive game, seller-described digital content, "
                "access, and current offer"
            ),
            "how_it_works": (
                "how the free game works and how the seller describes the "
                "resulting digital content"
            ),
            "pricing": (
                "what is known about free access, paid options, billing, and "
                "cancellation"
            ),
            "trust": (
                "the documented entertainment offer, delivery method, support, "
                "and material limits"
            ),
            "review": (
                "a clear entertainment-product overview for prospective users"
            ),
            "buyer_fit": (
                "the interactive format, seller-described content, and "
                "entertainment-only reader fit"
            ),
        }
        headline_patterns = {
            "features": (
                f"{product_name} Review 2026: Interactive Game, Digital "
                "Content, and Offer Details"
            ),
            "how_it_works": (
                f"How {product_name} Works: Free Game, Fortune Numbers, "
                "and Digital Content"
            ),
            "pricing": (
                f"{product_name} Pricing 2026: Free Access, Paid Options, "
                "and Billing Details"
            ),
            "trust": (
                f"{product_name} Buyer Guide 2026: Access, Delivery, "
                "Support, and Terms"
            ),
            "review": (
                f"{product_name} Review 2026: How the Interactive "
                "Entertainment Offer Works"
            ),
            "buyer_fit": (
                f"{product_name} 2026: Interactive Entertainment, "
                "Digital Readings, and Reader Fit"
            ),
        }
        shared_gaming_spine = (
            f"What {product_name} Is",
            "How the Free Interactive Game Works",
            "How Fortune Numbers Shape the Experience",
            "What the Seller Says Paid Access Includes",
            "How Digital Delivery and Support Are Described",
            "What Is Known About Billing and Cancellation",
            "Current Pricing Availability and Offer Details",
            "Entertainment-Only Limits and Reader Fit",
            f"Questions About {product_name}",
            f"The Bottom Line on {product_name}",
        )
        h2_spines = {
            intent: shared_gaming_spine
            for intent in (
                "features",
                "how_it_works",
                "pricing",
                "trust",
                "review",
                "buyer_fit",
            )
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
        f"Publisher niche: {niche}",
        f"Platform: {channel}",
        f"Approved niche sample: {playbook['approved_sample_size']}",
        "Accepted precedent intents: "
        + ", ".join(playbook.get("accepted_intents") or ["overview"]),
        f"Primary SEO intent: {selected_intent}",
        f"Title promise: {promises[selected_intent]}",
        f"Recommended headline: {headline_patterns[selected_intent]}",
        f"Use approved {channel or 'target-publisher'} formatting from the "
        "matching precedent corpus.",
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
        "Write polished American English with natural human cadence.",
        "Vary sentence and paragraph openings; remove AI-style filler, repeated "
        "transitions, canned summaries, and mechanical section introductions.",
        "Every section must add a new sourced fact, useful explanation, buyer "
        "question, or decision aid. Delete repetition instead of padding.",
        "Do not import facts from exemplars, previous coverage, or competitors.",
        body_playbook,
        "═══════════════════════════════════════════════",
        "",
    ))
    return "\n".join(lines)
