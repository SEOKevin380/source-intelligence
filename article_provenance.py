"""Deterministic article-to-source claim provenance reporting."""

from __future__ import annotations

import hashlib
import html
import json
import re


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
    plain = html.unescape(re.sub(r"<[^>]+>", " ", article))
    plain = re.sub(r"\s+", " ", plain).strip()
    return [
        item.strip() for item in re.split(r"(?<=[.!?])\s+", plain)
        if len(item.strip()) >= 20
    ]


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
            denominator = max(min(len(sentence_tokens), len(claim["tokens"])), 1)
            if overlap >= 3 and overlap / denominator >= 0.45:
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
                r"presents?|identifies?)\b|"
                r"\baccording to\b",
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
    return {
        "schema_version": 1,
        "source_pack_hash": (pack.get("source_pack_contract") or {}).get(
            "sha256", ""
        ),
        "article_hash": hashlib.sha256(article.encode()).hexdigest(),
        "publication_claim_count": len(claims),
        "mapped_sentence_count": len(mappings),
        "used_claim_count": len(used_ids),
        "mappings": mappings,
        "attribution_violations": attribution_violations,
        "passed": not attribution_violations,
        "excluded_claims": pack.get("excluded_publication_claims") or [],
        "scope_note": (
            "This deterministic ledger identifies textual claim support. "
            "Independent review remains responsible for implied claims and context."
        ),
    }
