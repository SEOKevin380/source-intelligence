"""Bidirectional article-to-source truth and CTA integrity audit.

The claim-provenance ledger answers whether the article used enough permitted
source claims. This module answers the inverse question: whether material
reader-facing assertions in the article stay inside the sealed source record.
It also audits every conversion link by destination role so an article cannot
hide duplicated or misleading official/affiliate calls to action behind clean
HTML.

The audit is deterministic, offline, and deliberately conservative. Clear
unsupported expansions are blockers. Lower-confidence unmatched sentences are
preserved as review candidates for the independent semantic reviewer.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup


_TOKEN_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "because", "been", "being",
    "but", "by", "can", "could", "did", "do", "does", "for", "from", "had",
    "has", "have", "he", "her", "here", "hers", "him", "his", "how", "if",
    "in", "into", "is", "it", "its", "may", "might", "more", "most", "of",
    "on", "or", "our", "ours", "she", "should", "so", "some", "such", "than",
    "that", "the", "their", "theirs", "them", "then", "there", "these", "they",
    "this", "those", "through", "to", "too", "us", "use", "used", "using",
    "was", "we", "were", "what", "when", "where", "which", "while", "who",
    "will", "with", "would", "you", "your", "yours",
    # Attribution and ordinary editorial furniture are not factual payload.
    "according", "available", "current", "described", "documentation",
    "information", "listed", "material", "materials", "offer", "product",
    "record", "reported", "says", "seller", "source", "stated",
}

_GENERIC_ADVICE = re.compile(
    r"^\s*(?:before|if|when|someone|readers?|buyers?|you)\b.*\b"
    r"(?:consider|decide|review|check|ask|compare|keep|save|contact|clarify|"
    r"determine|choose|evaluate|remember|expect)\b",
    re.I,
)

_RECORD_ABSENCE = re.compile(
    r"\b(?:not|isn['’]t|aren['’]t|wasn['’]t|weren['’]t)\s+"
    r"(?:detailed|documented|established|identified|listed|provided|specified|"
    r"stated|available|disclosed|described)\b|"
    r"\b(?:does not|do not|did not)\s+(?:detail|document|establish|identify|"
    r"list|provide|specify|state|disclose|describe)\b",
    re.I,
)

_HIGH_CONFIDENCE_RULES = (
    (
        "invented_affiliation",
        re.compile(
            r"\b(?:not|isn['’]t|is not|unaffiliated)\s+(?:directly\s+)?"
            r"affiliated\b|\bno\s+(?:formal\s+)?(?:connection|affiliation)\s+"
            r"(?:to|with)\b|\bstate or national lottery\b",
            re.I,
        ),
        "Affiliation or non-affiliation requires an explicit source record.",
    ),
    (
        "invented_randomness_or_assignment",
        re.compile(
            r"\b(?:not\s+)?predetermined\b|\bassigned\s+arbitrarily\b|"
            r"\brandomly\s+(?:assigned|generated|selected)\b|"
            r"\balgorithm(?:ic|ically)?\b",
            re.I,
        ),
        "The source must explicitly describe how values are assigned or generated.",
    ),
    (
        "invented_trial_scope",
        re.compile(
            r"\bfree trial\b|"
            r"\b(?:free|paid|formal|product|subscription|offer|money-back|"
            r"purchase)\s+(?:\w+\s+){0,2}trial period\b|"
            r"\btrial period\s+(?:for|of)\s+(?:the\s+)?"
            r"(?:paid|product|subscription|offer)\b",
            re.I,
        ),
        "A free introduction is not a paid-product trial unless the source says so.",
    ),
    (
        "invented_price_variability",
        re.compile(
            r"\bprices?\s+may\s+(?:change|vary)\b|\bpackage\s+tiers?\b|"
            r"\bpromotional\s+(?:price|prices|pricing|offer|offers|discounts?)\b",
            re.I,
        ),
        "Unknown pricing cannot be expanded into tiers, variability, or promotions.",
    ),
    (
        "invented_access_or_delivery",
        re.compile(
            r"\bcontent\s+library\b|\baccess\s+to\s+(?:an?|your)\s+account\b|"
            r"\baccount\s+and\s+content\b|\blifetime\s+access\b|"
            r"\bno\s+(?:software|app)\s+to\s+download\b|"
            r"\bno\s+hardware\s+(?:is\s+)?(?:needed|required|to set up)\b",
            re.I,
        ),
        "Access, account, library, download, and hardware terms require explicit support.",
    ),
    (
        "invented_commercial_safety",
        re.compile(
            r"\bfinancial\s+risk\s+(?:is\s+)?limited\b|\brisk[- ]free\b",
            re.I,
        ),
        "Refund terms do not establish low financial risk or an editorial value judgment.",
    ),
    (
        "speculative_product_expansion",
        re.compile(
            r"\bpresumably\b|\bapparently\b|\bperhaps\b|"
            r"\bcan\s+sometimes\b|\btypically\s+(?:includes?|means?|offers?)\b",
            re.I,
        ),
        "Speculation cannot add product function, availability, or commercial terms.",
    ),
    (
        "invented_prize_or_gambling_status",
        re.compile(
            r"\bno\s+cash\s+prizes?\b|\bnot\s+(?:an?|any)\s+"
            r"(?:actual\s+)?(?:lottery|gambling)\s+(?:product|operation)\b",
            re.I,
        ),
        "Prize, gambling, and lottery-operation status require explicit support.",
    ),
)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _tokens(value: str) -> set[str]:
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", _normalize(value).casefold()):
        if len(token) <= 2 or token in _TOKEN_STOP:
            continue
        if len(token) > 5 and token.endswith("ing"):
            token = token[:-3]
        elif len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith("ed"):
            token = token[:-2]
        elif len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        tokens.add(token)
    return tokens


def _numbers(value: str) -> set[str]:
    return set(re.findall(r"(?<![a-z])\d+(?:\.\d+)?%?", value.casefold()))


def _host(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    return (parsed.hostname or "").casefold().removeprefix("www.")


def _walk_strings(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = _normalize(value)
        if text:
            yield path, text


def _source_fragments(pack: dict) -> list[dict]:
    """Return only publication-safe source values and recorded gaps."""
    rows = []
    seen = set()

    def add(path, text, claim_id="", artifact_id=""):
        normalized = _normalize(text)
        key = normalized.casefold()
        if len(normalized) < 3 or key in seen:
            return
        seen.add(key)
        rows.append({
            "source_id": str(claim_id or hashlib.sha256(
                f"{path}:{normalized}".encode()
            ).hexdigest()[:16]),
            "artifact_id": str(artifact_id or ""),
            "path": path,
            "text": normalized,
            "tokens": _tokens(normalized),
            "numbers": _numbers(normalized),
        })

    for claim_type, items in (pack.get("publication_claims") or {}).items():
        for index, item in enumerate(items or []):
            add(
                f"publication_claims.{claim_type}[{index}]",
                item.get("text") or "",
                item.get("claim_id") or "",
                item.get("artifact_id") or "",
            )
    # Historical source packs may predate publication_claims. The compact
    # claims_by_type ledger is still publication-safe factual evidence.
    if not rows:
        for claim_type, items in (pack.get("claims_by_type") or {}).items():
            for index, item in enumerate(items or []):
                add(
                    f"claims_by_type.{claim_type}[{index}]",
                    item.get("text") or item.get("excerpt") or "",
                    item.get("claim_id") or "",
                    item.get("artifact_id") or "",
                )
    manifest = pack.get("intake_manifest") or {}
    for path, text in _walk_strings(
        {
            "contact_information": manifest.get("contact_information") or {},
            "refund_terms": manifest.get("refund_terms") or "",
        },
        "intake_manifest",
    ):
        add(path, text)
    for path, text in _walk_strings(
        (pack.get("required_facts") or {}).get("missing") or [],
        "required_facts.missing",
    ):
        add(path, f"not provided: {text}")
    return rows


def _sentence_rows(article: str) -> list[dict]:
    soup = BeautifulSoup(html.unescape(article or ""), "html.parser")
    rows = []
    current_heading = ""
    for node in soup.find_all(["h1", "h2", "h3", "p", "li", "figcaption"]):
        if node.name in {"h1", "h2", "h3"}:
            current_heading = _normalize(node.get_text(" ", strip=True))
            continue
        plain = _normalize(node.get_text(" ", strip=True))
        if not plain:
            continue
        anchors = node.find_all("a", href=True)
        anchor_text = _normalize(" ".join(
            anchor.get_text(" ", strip=True) for anchor in anchors
        ))
        if anchors and plain == anchor_text:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", plain):
            sentence = sentence.strip()
            if len(sentence) < 20:
                continue
            rows.append({
                "sentence_id": "S-" + hashlib.sha256(
                    sentence.encode()
                ).hexdigest()[:12],
                "text": sentence,
                "heading": current_heading,
                "tokens": _tokens(sentence),
                "numbers": _numbers(sentence),
            })
    return rows


def _is_exempt_sentence(row: dict) -> bool:
    sentence = row["text"]
    folded = sentence.casefold()
    heading = row["heading"].casefold()
    if "contact" in heading or re.search(
        r"\b(?:product|customer|order|media)\s+support\b", heading
    ):
        return True
    if (
        "paid advertorial" in folded
        or "compensation may be received" in folded
        or "commission may be earned" in folded
    ):
        return True
    if sentence.endswith("?"):
        return True
    if _GENERIC_ADVICE.search(sentence) and not re.search(
        r"\b(?:the product|the seller|the offer|it is|it has|it includes|"
        r"charges? (?:stop|continue)|price|guarantee|trial|affiliat)\b",
        folded,
    ):
        return True
    return False


def _best_source(row: dict, fragments: list[dict]) -> dict:
    best = {
        "source_id": "",
        "artifact_id": "",
        "path": "",
        "text": "",
        "sentence_coverage": 0.0,
        "source_coverage": 0.0,
        "shared": 0,
        "numbers_supported": not row["numbers"],
    }
    sentence_tokens = row["tokens"]
    for fragment in fragments:
        source_tokens = fragment["tokens"]
        shared = len(sentence_tokens & source_tokens)
        sentence_coverage = shared / max(len(sentence_tokens), 1)
        source_coverage = shared / max(len(source_tokens), 1)
        numbers_supported = row["numbers"].issubset(fragment["numbers"])
        score = (
            sentence_coverage * 0.7
            + min(source_coverage, 1.0) * 0.3
            + (0.05 if numbers_supported else -0.25)
        )
        best_score = (
            best["sentence_coverage"] * 0.7
            + min(best["source_coverage"], 1.0) * 0.3
            + (0.05 if best["numbers_supported"] else -0.25)
        )
        if score <= best_score:
            continue
        best = {
            "source_id": fragment["source_id"],
            "artifact_id": fragment["artifact_id"],
            "path": fragment["path"],
            "text": fragment["text"],
            "sentence_coverage": round(sentence_coverage, 3),
            "source_coverage": round(source_coverage, 3),
            "shared": shared,
            "numbers_supported": numbers_supported,
        }
    return best


def _rule_supported(rule_match, fragments: list[dict]) -> bool:
    phrase_tokens = _tokens(rule_match.group(0))
    if not phrase_tokens:
        return False
    return any(
        phrase_tokens.issubset(fragment["tokens"])
        for fragment in fragments
    )


def _material_sentence(row: dict, source_vocabulary: set[str]) -> bool:
    folded = row["text"].casefold()
    material_predicate = bool(re.search(
        r"\b(?:is|are|has|have|includes?|offers?|provides?|delivers?|"
        r"requires?|allows?|receives?|continues?|stops?|renews?|costs?|"
        r"charges?|ships?|works?|uses?|means?|becomes?|creates?|"
        r"available|guarantee|refund|subscription|trial|price|pricing|"
        r"account|access|affiliate|lottery|gambling)\b",
        folded,
    ))
    source_anchor_count = len(row["tokens"] & source_vocabulary)
    return material_predicate or source_anchor_count >= 2


def _grounding_audit(pack: dict, article: str) -> dict:
    fragments = _source_fragments(pack)
    source_vocabulary = set().union(
        *(fragment["tokens"] for fragment in fragments)
    ) if fragments else set()
    rows = _sentence_rows(article)
    violations = []
    candidates = []
    grounded = 0
    reviewed = 0
    for row in rows:
        if _is_exempt_sentence(row):
            continue
        rule_matches = [
            (rule_id, match, reason)
            for rule_id, pattern, reason in _HIGH_CONFIDENCE_RULES
            for match in [pattern.search(row["text"])]
            if match
        ]
        if (
            not _material_sentence(row, source_vocabulary)
            and not rule_matches
        ):
            continue
        reviewed += 1
        best = _best_source(row, fragments)
        grounded_sentence = bool(
            best["numbers_supported"]
            and (
                best["sentence_coverage"] >= 0.64
                or (
                    best["shared"] >= 4
                    and best["sentence_coverage"] >= 0.5
                    and best["source_coverage"] >= 0.35
                )
            )
        )
        if _RECORD_ABSENCE.search(row["text"]) and not re.search(
            r"\b(?:though|however|but)\b.*\b(?:may|can|could|typically|"
            r"sometimes|usually)\b",
            row["text"],
            re.I,
        ):
            grounded_sentence = True

        matched_rule = None
        for rule_id, match, reason in rule_matches:
            if _rule_supported(match, fragments):
                continue
            matched_rule = (rule_id, match, reason)
            break
        if matched_rule:
            rule_id, match, reason = matched_rule
            violations.append({
                "id": f"E-TRUTH-{len(violations) + 1}",
                "sentence_id": row["sentence_id"],
                "category": "Article-to-source grounding",
                "rule": rule_id,
                "issue": reason,
                "exact_text": row["text"],
                "replacement": (
                    "Delete this unsupported expansion or replace it with a "
                    "complete sentence directly supported by the cited source."
                ),
                "unsupported_excerpt": match.group(0),
                "best_source_id": best["source_id"],
                "best_source_artifact_id": best["artifact_id"],
                "best_source_excerpt": best["text"],
                "sentence_coverage": best["sentence_coverage"],
            })
            continue
        if grounded_sentence:
            grounded += 1
            continue
        candidates.append({
            "sentence_id": row["sentence_id"],
            "exact_text": row["text"],
            "heading": row["heading"],
            "best_source_id": best["source_id"],
            "best_source_artifact_id": best["artifact_id"],
            "best_source_excerpt": best["text"],
            "sentence_coverage": best["sentence_coverage"],
            "source_coverage": best["source_coverage"],
            "numbers_supported": best["numbers_supported"],
        })
    candidate_payload = [
        {
            "sentence_id": item["sentence_id"],
            "exact_text": item["exact_text"],
            "best_source_id": item["best_source_id"],
            "best_source_artifact_id": item[
                "best_source_artifact_id"
            ],
        }
        for item in candidates
    ]
    candidate_set_hash = hashlib.sha256(
        json.dumps(
            candidate_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return {
        "source_fragment_count": len(fragments),
        "sentence_count": len(rows),
        "material_sentence_count": reviewed,
        "deterministically_grounded_count": grounded,
        "grounding_violations": violations,
        "review_candidates": candidates,
        "review_candidate_set_hash": candidate_set_hash,
    }


def _is_contact_anchor(anchor) -> bool:
    href = str(anchor.get("href") or "").strip().casefold()
    if href.startswith(("mailto:", "tel:")):
        return True
    heading = anchor.find_previous(["h1", "h2", "h3"])
    heading_text = (
        _normalize(heading.get_text(" ", strip=True)).casefold()
        if heading is not None else ""
    )
    return bool(
        "contact" in heading_text
        or re.search(
            r"\b(?:product|customer|order|media)\s+support\b",
            heading_text,
        )
    )


def _cta_audit(pack: dict, article: str, affiliate_href: str = "") -> dict:
    soup = BeautifulSoup(html.unescape(article or ""), "html.parser")
    official_url = str(
        (pack.get("product") or {}).get("official_url") or ""
    ).strip()
    official_host = _host(official_url)
    affiliate_href = str(
        affiliate_href
        or (pack.get("intake_manifest") or {}).get("affiliate_link")
        or (pack.get("release_details") or {}).get("affiliate_link")
        or ""
    ).strip()
    anchors = []
    for index, anchor in enumerate(soup.find_all("a", href=True), 1):
        href = str(anchor.get("href") or "").strip()
        text = _normalize(anchor.get_text(" ", strip=True))
        role = "other"
        if href.casefold().startswith("mailto:"):
            role = "email"
        elif href.casefold().startswith("tel:"):
            role = "phone"
        elif affiliate_href and href == affiliate_href:
            role = "affiliate"
        elif official_host and _host(href) == official_host:
            role = "official"
        elif _is_contact_anchor(anchor):
            role = "support"
        is_contact = _is_contact_anchor(anchor)
        anchors.append({
            "index": index,
            "text": text,
            "normalized_text": re.sub(
                r"[^a-z0-9]+", " ", text.casefold()
            ).strip(),
            "href": href,
            "role": role,
            "contact": is_contact,
            "node": anchor,
        })

    conversion = [
        item for item in anchors
        if not item["contact"] and item["role"] in {"official", "affiliate"}
    ]
    affiliate = [item for item in conversion if item["role"] == "affiliate"]
    plain = _normalize(soup.get_text(" ", strip=True))
    word_count = len(re.findall(r"\b[\w’'-]+\b", plain))
    # The generation contract allows three to four naturally spaced partner
    # CTAs in a full-length release. Scale shorter copy down without turning
    # an otherwise compliant 1,200-1,600 word article into a needless jam.
    max_affiliate = (
        4
        if word_count >= 1200
        else max(2, min(3, math.ceil(max(word_count, 1) / 400)))
    )
    max_conversion = max_affiliate + 1
    findings = []

    by_label = {}
    for item in conversion:
        by_label.setdefault(item["normalized_text"], []).append(item)
    for label, items in by_label.items():
        destinations = {item["href"] for item in items}
        roles = {item["role"] for item in items}
        if label and len(destinations) > 1:
            findings.append({
                "id": f"E-CTA-{len(findings) + 1}",
                "category": "CTA destination integrity",
                "issue": (
                    "Identical CTA text points to different destinations "
                    f"and roles: {', '.join(sorted(roles))}."
                ),
                "exact_text": items[0]["text"],
                "replacement": (
                    "Keep one destination here or use distinct, accurate labels "
                    "that make the official and partner destinations clear."
                ),
                "roles": sorted(roles),
                "destinations": sorted(destinations),
            })

    cta_only_blocks = []
    for block in soup.find_all(["p", "li", "div"]):
        block_anchors = [
            item for item in conversion if item["node"] in block.descendants
        ]
        if not block_anchors:
            continue
        block_text = _normalize(block.get_text(" ", strip=True))
        anchor_text = _normalize(" ".join(
            item["text"] for item in block_anchors
        ))
        if block_text == anchor_text:
            cta_only_blocks.append((block, block_anchors[0]))
    for (left_block, left), (right_block, right) in zip(
        cta_only_blocks, cta_only_blocks[1:]
    ):
        sibling = left_block.find_next_sibling()
        while sibling is not None and not _normalize(
            getattr(sibling, "get_text", lambda *_a, **_k: "")(
                " ", strip=True
            )
        ):
            sibling = sibling.find_next_sibling()
        if sibling is right_block:
            findings.append({
                "id": f"E-CTA-{len(findings) + 1}",
                "category": "CTA spacing integrity",
                "issue": "Two standalone conversion CTAs appear consecutively.",
                "exact_text": f"{left['text']} | {right['text']}",
                "replacement": (
                    "Remove one CTA or place meaningful source-grounded reader "
                    "content between distinct conversion opportunities."
                ),
                "roles": [left["role"], right["role"]],
                "destinations": [left["href"], right["href"]],
            })
    if len(affiliate) > max_affiliate:
        findings.append({
            "id": f"E-CTA-{len(findings) + 1}",
            "category": "CTA density integrity",
            "issue": (
                f"The article contains {len(affiliate)} affiliate CTAs; "
                f"the deterministic maximum for {word_count} words is "
                f"{max_affiliate}."
            ),
            "exact_text": "",
            "replacement": (
                "Keep the strongest naturally spaced affiliate CTAs and remove "
                "the repetitive extras."
            ),
            "actual": len(affiliate),
            "maximum": max_affiliate,
        })
    if len(conversion) > max_conversion:
        findings.append({
            "id": f"E-CTA-{len(findings) + 1}",
            "category": "CTA density integrity",
            "issue": (
                f"The article contains {len(conversion)} body conversion links; "
                f"the maximum for {word_count} words is {max_conversion}."
            ),
            "exact_text": "",
            "replacement": (
                "Reduce body conversion links while retaining the official "
                "website in the final contact block."
            ),
            "actual": len(conversion),
            "maximum": max_conversion,
        })
    return {
        "word_count": word_count,
        "anchor_count": len(anchors),
        "conversion_cta_count": len(conversion),
        "affiliate_cta_count": len(affiliate),
        "maximum_affiliate_ctas": max_affiliate,
        "maximum_conversion_ctas": max_conversion,
        "anchors": [
            {key: value for key, value in item.items() if key != "node"}
            for item in anchors
        ],
        "cta_integrity_violations": findings,
    }


def _remove_conversion_anchor(anchor) -> str:
    """Remove one conversion link without deleting surrounding article copy."""
    text = _normalize(anchor.get_text(" ", strip=True))
    parent = anchor.parent
    if (
        parent is not None
        and parent.name in {"p", "li", "div"}
        and _normalize(parent.get_text(" ", strip=True)) == text
        and len(parent.find_all("a", href=True)) == 1
    ):
        parent.decompose()
        return text
    anchor.unwrap()
    return text


def repair_cta_integrity(
    pack: dict, article: str, affiliate_href: str = ""
) -> tuple[str, dict]:
    """Apply bounded, meaning-preserving CTA repairs at zero model cost.

    The routine may relabel ambiguous official/partner links and remove excess
    standalone conversion links. It never changes a destination or inserts a
    new commercial assertion.
    """
    soup = BeautifulSoup(html.unescape(article or ""), "html.parser")
    official_url = str(
        (pack.get("product") or {}).get("official_url") or ""
    ).strip()
    official_host = _host(official_url)
    affiliate_href = str(
        affiliate_href
        or (pack.get("intake_manifest") or {}).get("affiliate_link")
        or (pack.get("release_details") or {}).get("affiliate_link")
        or ""
    ).strip()
    changed_labels = []
    removed = []

    def conversion_rows():
        rows = []
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            if _is_contact_anchor(anchor):
                continue
            role = ""
            if affiliate_href and href == affiliate_href:
                role = "affiliate"
            elif official_host and _host(href) == official_host:
                role = "official"
            if role:
                rows.append((anchor, role))
        return rows

    by_label = {}
    for anchor, role in conversion_rows():
        label = re.sub(
            r"[^a-z0-9]+",
            " ",
            _normalize(anchor.get_text(" ", strip=True)).casefold(),
        ).strip()
        by_label.setdefault(label, []).append((anchor, role))
    for label, rows in by_label.items():
        if not label or len({role for _anchor, role in rows}) < 2:
            continue
        for anchor, role in rows:
            old = _normalize(anchor.get_text(" ", strip=True))
            new = (
                "Visit the official product website"
                if role == "official"
                else "Review the current partner offer"
            )
            strong = anchor.find("strong")
            target = strong if strong is not None else anchor
            target.clear()
            target.string = new
            changed_labels.append({
                "old": old,
                "new": new,
                "role": role,
            })

    # Consecutive standalone links add no reader value. Keep the first and
    # remove the later block before applying numeric density limits.
    previous_block = None
    for anchor, _role in list(conversion_rows()):
        parent = anchor.parent
        while parent is not None and parent.name not in {"p", "li", "div"}:
            parent = parent.parent
        if parent is None:
            previous_block = None
            continue
        text = _normalize(parent.get_text(" ", strip=True))
        anchor_text = _normalize(anchor.get_text(" ", strip=True))
        if text != anchor_text:
            previous_block = None
            continue
        if (
            previous_block is not None
            and previous_block.find_next_sibling() is parent
        ):
            removed.append(_remove_conversion_anchor(anchor))
            continue
        previous_block = parent

    initial = _cta_audit(pack, str(soup), affiliate_href)
    maximum_affiliate = initial["maximum_affiliate_ctas"]
    maximum_conversion = initial["maximum_conversion_ctas"]

    affiliates = [
        anchor for anchor, role in conversion_rows() if role == "affiliate"
    ]
    while len(affiliates) > maximum_affiliate:
        anchor = affiliates.pop()
        removed.append(_remove_conversion_anchor(anchor))

    conversions = conversion_rows()
    while len(conversions) > maximum_conversion:
        # Preserve the first official link and the earliest naturally placed
        # partner CTAs. Remove later official duplicates first, then the final
        # remaining conversion link.
        official = [
            anchor for anchor, role in conversions if role == "official"
        ]
        target = official[-1] if len(official) > 1 else conversions[-1][0]
        removed.append(_remove_conversion_anchor(target))
        conversions = conversion_rows()

    repaired = str(soup)
    final = _cta_audit(pack, repaired, affiliate_href)
    return repaired, {
        "changed": bool(changed_labels or removed),
        "changed_labels": changed_labels,
        "removed_cta_text": removed,
        "remaining_violations": final["cta_integrity_violations"],
        "affiliate_cta_count": final["affiliate_cta_count"],
        "conversion_cta_count": final["conversion_cta_count"],
    }


def audit_editorial_truth(
    pack: dict, article: str, affiliate_href: str = ""
) -> dict:
    """Audit material sentence grounding and conversion-link integrity."""
    grounding = _grounding_audit(pack or {}, article or "")
    cta = _cta_audit(pack or {}, article or "", affiliate_href)
    violations = (
        grounding["grounding_violations"]
        + cta["cta_integrity_violations"]
    )
    return {
        "schema_version": 1,
        "article_hash": hashlib.sha256(
            str(article or "").encode()
        ).hexdigest(),
        **grounding,
        **cta,
        "passed": not violations,
        "scope_note": (
            "High-confidence unsupported expansions and CTA-role defects are "
            "deterministic blockers. Lower-confidence unmatched material "
            "sentences remain explicit independent-review candidates."
        ),
    }
