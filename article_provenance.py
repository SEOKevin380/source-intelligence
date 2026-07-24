"""Deterministic article-to-source claim provenance reporting."""

from __future__ import annotations

import hashlib
import html
import json
import re

from bs4 import BeautifulSoup


def extract_sealed_pack(source_text: str) -> dict:
    marker = "═══ SEALED CURRENT-PRODUCT SOURCE PACK — FACTS ONLY ═══"
    if marker not in source_text:
        return {}
    raw = source_text.split(marker, 1)[1].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _tokens(value: str) -> set[str]:
    stop = {
        "and", "the", "that", "this", "with", "from", "for", "are", "was",
        "were", "has", "have", "its", "into", "may", "seller", "materials",
    }
    return {
        token for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) > 2 and token not in stop
    }


def _sentences(article: str) -> list[str]:
    """Split within semantic blocks so headings cannot contaminate claims.

    Flattening the entire document joined an H2 to its following paragraph.
    That changed the apparent subject of pricing and feature sentences and
    produced both false mappings and hidden attribution failures.
    """
    soup = BeautifulSoup(html.unescape(article or ""), "html.parser")
    blocks = soup.find_all(["p", "li", "td", "th", "figcaption"])
    if not blocks:
        blocks = [soup]
    sentences = []
    for block in blocks:
        plain = re.sub(r"\s+", " ", block.get_text(" ", strip=True)).strip()
        sentences.extend(
            item.strip()
            for item in re.split(r"(?<=[.!?])\s+", plain)
            if len(item.strip()) >= 20
        )
    return sentences


def build_article_claim_ledger(pack: dict, article: str) -> dict:
    claims = []
    for claim_type, items in (pack.get("publication_claims") or {}).items():
        for item in items or []:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            claim_id = str(item.get("claim_id") or hashlib.sha256(
                f"{claim_type}:{text}".encode()
            ).hexdigest()[:16])
            claims.append({
                "claim_id": claim_id,
                "claim_type": claim_type,
                "text": text,
                "artifact_id": item.get("artifact_id", ""),
                "source_class": item.get("source_class", ""),
                "publication_treatment": item.get(
                    "publication_treatment", "direct_fact_allowed"
                ),
                "tokens": _tokens(text),
            })

    mappings = []
    attribution_violations = []
    for sentence in _sentences(article):
        sentence_tokens = _tokens(sentence)
        matches = []
        for claim in claims:
            overlap = len(sentence_tokens & claim["tokens"])
            claim_token_count = len(claim["tokens"])
            required_overlap = min(3, claim_token_count)
            denominator = max(
                min(len(sentence_tokens), claim_token_count), 1
            )
            if (
                claim_token_count
                and overlap >= required_overlap
                and overlap / denominator >= 0.45
            ):
                matches.append({
                    key: value for key, value in claim.items() if key != "tokens"
                })
        if matches:
            mappings.append({"article_sentence": sentence, "claims": matches})
            sentence_lower = sentence.casefold()
            seller_attributed = bool(re.search(
                r"\b(?:seller|offer|vendor|manufacturer|product page|"
                r"sales page|source materials?|materials?)\b.{0,50}"
                r"\b(?:states?|says?|describes?|lists?|reports?|claims?|"
                r"calls?|presents?|identifies?)\b|"
                r"\baccording to\b",
                sentence_lower,
            ))
            # A seller/source noun phrase can govern a later reporting verb in
            # a long but single semantic block ("Seller headings such as ...
            # describe ..."). Do not impose an arbitrary 50-character window.
            seller_attributed = seller_attributed or bool(re.search(
                r"^\s*(?:the\s+)?(?:seller|offer|vendor|manufacturer|"
                r"product page|sales page|source materials?|materials?)\b"
                r".*\b(?:states?|says?|describes?|lists?|reports?|claims?|"
                r"calls?|presents?|identifies?)\b",
                sentence_lower,
            ))
            source_attributed = seller_attributed or bool(re.search(
                r"\b(?:according to|the source|the record|the cited|"
                r"documentation|reported by|published by)\b",
                sentence_lower,
            ))
            for claim in matches:
                treatment = claim.get("publication_treatment")
                if (
                    treatment == "seller_attribution_required"
                    and not seller_attributed
                ) or (
                    treatment == "source_attribution_required"
                    and not source_attributed
                ):
                    attribution_violations.append({
                        "article_sentence": sentence,
                        "claim_id": claim["claim_id"],
                        "required_treatment": treatment,
                    })

    used_ids = {
        claim["claim_id"] for mapping in mappings for claim in mapping["claims"]
    }
    required_used = min(3, len(claims))
    required_mapped_sentences = min(3, len(claims))
    coverage_violations = []
    if len(used_ids) < required_used:
        coverage_violations.append({
            "id": "P-COVERAGE-CLAIMS",
            "issue": (
                f"The article uses {len(used_ids)} of {len(claims)} permitted "
                f"publication claims; at least {required_used} distinct claims "
                "are required for a source-grounded product article."
            ),
            "required": required_used,
            "actual": len(used_ids),
        })
    if len(mappings) < required_mapped_sentences:
        coverage_violations.append({
            "id": "P-COVERAGE-SENTENCES",
            "issue": (
                f"Only {len(mappings)} article sentences map to the sealed "
                f"publication ledger; at least {required_mapped_sentences} "
                "source-grounded sentences are required."
            ),
            "required": required_mapped_sentences,
            "actual": len(mappings),
        })
    return {
        "schema_version": 1,
        "source_pack_hash": (pack.get("source_pack_contract") or {}).get(
            "sha256", ""
        ),
        "article_hash": hashlib.sha256(article.encode()).hexdigest(),
        "publication_claim_count": len(claims),
        "mapped_sentence_count": len(mappings),
        "used_claim_count": len(used_ids),
        "required_used_claim_count": required_used,
        "required_mapped_sentence_count": required_mapped_sentences,
        "mappings": mappings,
        "attribution_violations": attribution_violations,
        "coverage_violations": coverage_violations,
        "passed": not attribution_violations and not coverage_violations,
        "excluded_claims": pack.get("excluded_publication_claims") or [],
        "scope_note": (
            "This deterministic ledger identifies textual claim support. "
            "Independent review remains responsible for implied claims and context."
        ),
    }
