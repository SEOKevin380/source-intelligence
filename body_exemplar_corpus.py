"""Deterministic full-body intelligence for approved publisher exemplars.

Historical bodies teach structure and formatting only. They never become
product-fact sources for a new article.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import re
import statistics
from collections import Counter

from bs4 import BeautifulSoup


BODY_CORPUS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "approved_release_body_profiles.json.gz",
)

_BOILERPLATE_SELECTORS = (
    "script", "style", "nav", "header", "footer", "aside", "iframe",
    "form", "svg", "noscript", ".sidebar", ".related-articles",
    ".social-share", ".newsletter-signup", ".cookie-banner",
    ".advertisement", ".breadcrumb", ".share-bar", ".comments",
    ".related-news", ".trending", ".recommended", ".subscribe-form",
    ".ad-slot",
)

_PLATFORM_SELECTORS = {
    "barchart": (
        ".article-content article", ".article-content", ".bc-article article",
    ),
    "accesswire": (
        ".release-body", ".article-body", ".release-content",
        "article.release", "article",
    ),
    "newswire": (
        ".article-body", ".news-release-body", ".release-content",
        "article",
    ),
    "globe": (
        '[data-test="article-body"]', ".main-body-container",
        ".main-article-body", "article.main-article", "article",
    ),
}


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def extract_article_body(raw_html: str, platform: str) -> str:
    """Extract the narrowest substantial article container."""
    soup = BeautifulSoup(raw_html or "", "html.parser")
    for selector in _BOILERPLATE_SELECTORS:
        for node in soup.select(selector):
            node.decompose()

    candidates = []
    selectors = _PLATFORM_SELECTORS.get(platform, ()) + ("article", "main")
    for priority, selector in enumerate(selectors):
        for node in soup.select(selector):
            text = _clean_text(node.get_text(" ", strip=True))
            words = len(re.findall(r"\b[\w’'-]+\b", text))
            if words >= 250:
                candidates.append((priority, words, node))
        if candidates:
            break
    if not candidates:
        paragraphs = [
            node for node in soup.find_all("p")
            if len(_clean_text(node.get_text(" ", strip=True))) >= 40
        ]
        if len(paragraphs) >= 5:
            wrapper = BeautifulSoup("<article></article>", "html.parser").article
            for node in paragraphs:
                wrapper.append(node)
            candidates.append((99, len(wrapper.get_text(" ").split()), wrapper))
    if not candidates:
        return ""

    # Prefer the narrowest qualifying node in the highest-priority selector.
    priority = min(item[0] for item in candidates)
    same_priority = [item for item in candidates if item[0] == priority]
    node = min(same_priority, key=lambda item: item[1])[2]
    for heading in node.find_all(["h2", "h3", "strong"]):
        text = _clean_text(heading.get_text(" ", strip=True))
        if re.match(
            r"^(?:About\s+|Media Contact|Press Contact|Investor Relations|"
            r"Forward[- ]Looking|Safe Harbor|Contact Information)",
            text,
            re.I,
        ):
            for later in list(heading.find_all_next()):
                if later is not heading and node in later.parents:
                    later.decompose()
            heading.decompose()
            break
    return str(node).strip()


def _heading_pattern(value: str, product_name: str = "") -> str:
    value = _clean_text(value)
    if product_name:
        value = re.sub(re.escape(product_name), "[PRODUCT]", value, flags=re.I)
    value = re.sub(r"\b(?:19|20)\d{2}\b", "[YEAR]", value)
    value = re.sub(r"\$\d+(?:[.,]\d+)*", "[PRICE]", value)
    value = re.sub(r"\b\d+(?:\.\d+)?%?\b", "[NUMBER]", value)
    return value[:180]


def _structural_headings(soup: BeautifulSoup) -> list[str]:
    headings = [
        _clean_text(node.get_text(" ", strip=True))
        for node in soup.find_all(["h2", "h3"])
    ]
    if headings:
        return [item for item in headings if item]
    # Newswire syndication frequently flattens headings into standalone STRONG
    # paragraphs. Recover only heading-like blocks, not labels or CTA anchors.
    recovered = []
    for node in soup.find_all(["strong", "b"]):
        text = _clean_text(node.get_text(" ", strip=True))
        parent = node.parent
        parent_text = _clean_text(parent.get_text(" ", strip=True)) if parent else ""
        if (
            8 <= len(text) <= 160
            and parent
            and parent.name in {"p", "div"}
            and len(parent_text) <= len(text) + 12
            and not text.endswith(":")
            and not re.search(
                r"\b(?:click|visit|view|see|start|check|review|confirm)\b",
                text,
                re.I,
            )
            and (
                text.endswith("?")
                or re.match(
                    r"^(?:what|how|why|who|is|are|does|can|pricing|"
                    r"fast facts|quick answers|frequently asked|"
                    r"buyer verification|the bottom line|material limitations|"
                    r"contact information|things to verify|side effects|"
                    r"disclosure)",
                    text,
                    re.I,
                )
            )
        ):
            recovered.append(text)
    return recovered


def heading_role(value: str) -> str:
    """Reduce historical wording to a fact-free reader-question role."""
    text = value.casefold()
    rules = (
        ("disclosure", r"\b(?:disclosure|compliance information)\b"),
        ("contact", r"\b(?:contact|support email|mailing address)\b"),
        ("faq", r"\b(?:faq|frequently asked|quick answers?)\b"),
        ("conclusion", r"\b(?:bottom line|final (?:take|assessment)|verdict)\b"),
        ("limitations", r"\b(?:limitations?|drawbacks?|side effects?|risks?)\b"),
        ("buyer_checks", r"\b(?:verify|verification|questions? to ask|checklist)\b"),
        ("buyer_fit", r"\b(?:right for you|who is it for|best for|not for)\b"),
        ("pricing", r"\b(?:price|pricing|cost|refund|guarantee|package)\b"),
        ("trust", r"\b(?:scam|legit|trust|complaints?|better business bureau)\b"),
        ("feedback", r"\b(?:buyers?|customers?|reviews?|trustpilot)\b"),
        ("comparison", r"\b(?:compare|comparison|alternatives?|\bvs\.?\b)\b"),
        ("evidence", r"\b(?:research|evidence|experts?|independent|really work)\b"),
        ("mechanism", r"\b(?:how .* works?|technology|mechanism|actually do)\b"),
        ("features", r"\b(?:features?|included|inside|offers?|fast facts)\b"),
        ("overview", r"\b(?:what is|overview|introduction)\b"),
    )
    return next((role for role, pattern in rules if re.search(pattern, text)), "other")


def profile_article_body(
    body_html: str, *, url: str, platform: str, niche: str,
    title: str = "", product_name: str = "", published_date: str = "",
) -> dict:
    """Create a fact-free structural profile from an approved article body."""
    soup = BeautifulSoup(body_html or "", "html.parser")
    plain = _clean_text(soup.get_text(" ", strip=True))
    words = re.findall(r"\b[\w’'-]+\b", plain)
    if len(words) < 250:
        raise ValueError("Extracted article body is too short")
    headings = _structural_headings(soup)
    paragraphs = [
        _clean_text(node.get_text(" ", strip=True))
        for node in soup.find_all("p")
        if _clean_text(node.get_text(" ", strip=True))
    ]
    links = soup.find_all("a", href=True)
    paragraph_words = [
        len(re.findall(r"\b[\w’'-]+\b", paragraph))
        for paragraph in paragraphs
    ]
    lower = plain.casefold()
    disclosure_position = next(
        (
            index for index, paragraph in enumerate(paragraphs)
            if re.search(r"\b(?:paid advertorial|affiliate|commission|compensation)\b",
                         paragraph, re.I)
        ),
        -1,
    )
    cta_positions = []
    for link in links:
        anchor = _clean_text(link.get_text(" ", strip=True))
        if re.search(
            r"\b(?:buy|order|visit|view|review|learn|see|check|offer|details)\b",
            anchor,
            re.I,
        ):
            before = _clean_text(str(soup)[: str(soup).find(str(link))])
            cta_positions.append(
                round(len(before.split()) / max(len(words), 1), 3)
            )
    section_lengths = []
    heading_nodes = soup.find_all(["h2", "h3"])
    for heading in heading_nodes:
        count = 0
        for node in heading.find_all_next():
            if node.name in {"h2", "h3"}:
                break
            if node.name in {"p", "li"}:
                count += len(re.findall(
                    r"\b[\w’'-]+\b", _clean_text(node.get_text(" ", strip=True))
                ))
        section_lengths.append(count)
    return {
        "schema_version": 1,
        "url": url,
        "platform": platform,
        "niche": niche,
        "title": title,
        "published_date": published_date,
        "content_hash": hashlib.sha256(body_html.encode()).hexdigest(),
        "word_count": len(words),
        "heading_count": len(headings),
        "heading_sequence": [
            _heading_pattern(item, product_name) for item in headings[:24]
        ],
        "heading_role_sequence": [heading_role(item) for item in headings[:24]],
        "section_word_counts": section_lengths[:24],
        "paragraph_count": len(paragraphs),
        "median_paragraph_words": (
            round(statistics.median(paragraph_words), 1)
            if paragraph_words else 0
        ),
        "list_count": len(soup.find_all(["ul", "ol"])),
        "table_count": len(soup.find_all("table")),
        "link_count": len(links),
        "cta_count": len(cta_positions),
        "cta_relative_positions": cta_positions,
        "disclosure_paragraph_index": disclosure_position,
        "has_contact_block": bool(re.search(
            r"\b(?:contact information|media contact|email:|phone:)\b",
            lower,
        )),
        "has_faq": any(
            "faq" in item.casefold()
            or "frequently asked" in item.casefold()
            or "quick answers" in item.casefold()
            for item in headings
        ),
        "has_limitations": any(re.search(
            r"\b(?:limitations?|drawbacks?|considerations?|what to verify|"
            r"not for|questions to ask)\b", item, re.I
        ) for item in headings),
        "source_boundary": (
            "Approved historical structure only; never use this profile as "
            "evidence for current-product facts."
        ),
    }


def load_body_corpus(path: str = BODY_CORPUS_PATH) -> dict:
    if not os.path.exists(path):
        return {"schema_version": 1, "profiles": [], "clusters": {}}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def profiles_by_url(path: str = BODY_CORPUS_PATH) -> dict:
    return {
        item["url"]: item
        for item in load_body_corpus(path).get("profiles", [])
    }


def build_cluster_playbooks(profiles: list[dict]) -> dict:
    grouped = {}
    for profile in profiles:
        key = f"{profile['platform']}::{profile['niche']}"
        grouped.setdefault(key, []).append(profile)
    result = {}
    for key, items in grouped.items():
        role_sequences = [
            tuple(
                item.get("heading_role_sequence")
                or [heading_role(value) for value in item.get("heading_sequence", [])]
            )
            for item in items
        ]
        heading_sequences = Counter(
            role_sequences
        )
        common_roles = Counter(
            role for sequence in role_sequences for role in sequence
            if role != "other"
        )
        result[key] = {
            "sample_size": len(items),
            "median_word_count": round(statistics.median(
                item["word_count"] for item in items
            )),
            "median_heading_count": round(statistics.median(
                item["heading_count"] for item in items
            )),
            "median_paragraph_words": round(statistics.median(
                item["median_paragraph_words"] for item in items
            ), 1),
            "median_cta_count": round(statistics.median(
                item["cta_count"] for item in items
            )),
            "disclosure_above_fold_rate": round(sum(
                0 <= item["disclosure_paragraph_index"] <= 2 for item in items
            ) / len(items), 3),
            "limitations_rate": round(sum(
                item["has_limitations"] for item in items
            ) / len(items), 3),
            "faq_rate": round(sum(item["has_faq"] for item in items) / len(items), 3),
            "common_section_roles": [
                role for role, _ in common_roles.most_common()
            ],
            "common_role_spines": [
                list(spine) for spine, _ in heading_sequences.most_common(5)
                if spine
            ],
            "representative_urls": [
                item["url"] for item in sorted(
                    items,
                    key=lambda value: (
                        value.get("published_date", ""),
                        value.get("word_count", 0),
                    ),
                    reverse=True,
                )[:8]
            ],
            "fact_boundary": (
                "Aggregate approved structure only. The sealed current-product "
                "pack remains the exclusive factual authority."
            ),
        }
    return result


def format_body_playbook(platform: str, niche: str,
                         path: str = BODY_CORPUS_PATH) -> str:
    corpus = load_body_corpus(path)
    profiles = [
        item for item in corpus.get("profiles", [])
        if item.get("platform") == platform
    ]
    if not profiles:
        legacy = (corpus.get("clusters") or {}).get(f"{platform}::{niche}")
        if not legacy:
            return ""
        lines = [
            "═══ APPROVED FULL-BODY PUBLISHER × NICHE PROFILE ═══",
            f"Profile sample: {legacy['sample_size']}",
            "Profile confidence: legacy aggregate only; selected-body "
            "structure is not eligible for controlling generation.",
            f"Observed median length: {legacy['median_word_count']} words "
            "(calibration only; never pad beyond current-source value)",
            f"Median structural section count: {legacy['median_heading_count']}",
            f"Median CTA count: {legacy['median_cta_count']}",
            "Common approved section roles:",
        ]
        lines.extend(
            f"  • {role.replace('_', ' ')}"
            for role in legacy.get("common_section_roles", [])[:12]
        )
        lines.extend((legacy["fact_boundary"], ""))
        return "\n".join(lines)
    qualified = [
        item for item in profiles
        if item.get("niche") == niche
        and 800 <= int(item.get("word_count") or 0) <= 5000
        and 4 <= int(item.get("heading_count") or 0) <= 20
    ]
    confidence = "exact publisher × niche"
    if not qualified and niche.endswith("devices"):
        qualified = [
            item for item in profiles
            if str(item.get("niche") or "").endswith(("devices", "gadgets"))
            and 800 <= int(item.get("word_count") or 0) <= 5000
            and 4 <= int(item.get("heading_count") or 0) <= 20
        ]
        confidence = "adjacent publisher device niches"
    if not qualified:
        return ""
    qualified = sorted(
        qualified,
        key=lambda item: (
            abs(int(item.get("word_count") or 0) - 1800),
            item.get("url") or "",
        ),
    )[:3]
    normalized_profiles = [
        {**item, "niche": "__qualified__"} for item in qualified
    ]
    playbook = build_cluster_playbooks(normalized_profiles).get(
        f"{platform}::__qualified__"
    )
    if not playbook:
        return ""
    lines = [
        "═══ APPROVED FULL-BODY PUBLISHER × NICHE PROFILE ═══",
        f"Profile confidence: {confidence}",
        f"Qualified profile sample: {len(qualified)}",
        f"Observed median length: {playbook['median_word_count']} words "
        "(calibration only; never pad beyond current-source value)",
        f"Median structural section count: {playbook['median_heading_count']}",
        f"Median CTA count: {playbook['median_cta_count']}",
        "Common approved section roles:",
    ]
    lines.extend(
        f"  • {role.replace('_', ' ')}"
        for role in playbook.get("common_section_roles", [])[:12]
    )
    lines.append("Selected fact-free structural role spines:")
    for item in qualified:
        roles = [
            role for role in item.get("heading_role_sequence", [])
            if role and role != "other"
        ][:16]
        identity = hashlib.sha256(
            str(item.get("url") or "").encode()
        ).hexdigest()[:12]
        lines.append(
            f"  • profile {identity}: "
            + (" → ".join(roles) if roles else "no reliable role spine")
        )
    lines.extend((
        "Use this profile for structure, pacing, disclosure, and CTA rhythm only. "
        "The current assignment's bounded depth contract controls length.",
        playbook["fact_boundary"],
        "═══════════════════════════════════════════════",
        "",
    ))
    return "\n".join(lines)
