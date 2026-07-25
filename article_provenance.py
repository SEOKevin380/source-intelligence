"""Deterministic article-to-source claim provenance reporting."""

from __future__ import annotations

import hashlib
import html
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from source_pack_contract import (
    normalize_contact_information,
    normalized_intake_manifest,
)


def extract_sealed_pack(source_text: str) -> dict:
    marker = "═══ SEALED CURRENT-PRODUCT SOURCE PACK — FACTS ONLY ═══"
    if marker not in source_text:
        return {}
    raw = source_text.split(marker, 1)[1].strip()
    try:
        value, _ = json.JSONDecoder().raw_decode(raw)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _tokens(value: str) -> set[str]:
    stop = {
        "and", "the", "that", "this", "with", "from", "for", "are", "was",
        "were", "has", "have", "its", "into", "may", "seller", "materials",
        "described", "reported", "stated", "presented", "listed",
        # Transport/domain furniture is not claim meaning. Treating these as
        # ordinary tokens allowed an email at one domain to map to that
        # domain's website claim.
        "http", "https", "www", "com", "org", "net", "html",
    }
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", value.casefold()):
        if len(token) <= 2 or token in stop:
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
    """Return exact numeric atoms; 50 mg must never substantiate 500 mg."""
    return set(re.findall(r"(?<![a-z])\d+(?:\.\d+)?%?", value.casefold()))


def _negated(value: str) -> bool:
    return bool(re.search(
        r"\b(?:no|not|never|cannot|can't|does not|doesn't|without)\b",
        value.casefold(),
    ))


def _attribution_signals(
    value: str, seller_subject: str = ""
) -> tuple[bool, bool]:
    """Return seller/source attribution signals in one semantic scope."""
    lowered = value.casefold()
    seller_attributed = bool(re.search(
        r"\b(?:seller|brand|offer|vendor|manufacturer|product page|"
        r"sales page|source materials?|materials?)\b.{0,50}"
        r"\b(?:states?|says?|describes?|lists?|reports?|claims?|"
        r"calls?|presents?|identif(?:y|ies)|connects?|uses?|includes?|"
        r"provides?|positions?|offers?|confirms?|explains?|notes?|"
        r"indicates?|specifies?|shows?)\b",
        lowered,
    ))
    # A seller/source noun phrase can govern a later reporting verb in a long
    # but single sentence ("Seller headings such as ... describe ...").
    seller_attributed = seller_attributed or bool(re.search(
        r"^\s*(?:the\s+)?(?:seller|brand|offer|vendor|manufacturer|"
        r"product page|sales page|source materials?|materials?)\b"
        r".*\b(?:states?|says?|describes?|lists?|reports?|claims?|"
        r"calls?|presents?|identif(?:y|ies)|connects?|uses?|includes?|"
        r"provides?|positions?|offers?|confirms?|explains?|notes?|"
        r"indicates?|specifies?|shows?)\b",
        lowered,
    ))
    if seller_subject:
        seller_attributed = seller_attributed or bool(re.match(
            rf"^\s*(?:{re.escape(seller_subject.casefold())})\b.{{0,100}}"
            r"\b(?:is|are)\s+(?:designed|intended|positioned|presented|"
            r"described|listed|offered|built|configured)\b|"
            rf"^\s*(?:{re.escape(seller_subject.casefold())})\b.{{0,80}}"
            r"\b(?:features?|includes?|uses?|offers?|provides?)\b",
            lowered,
        ))
    seller_attributed = seller_attributed or bool(re.search(
        r"\b(?:seller|vendor|manufacturer|brand|offer|product-page)"
        r"[- ](?:described|reported|stated|presented|listed|claimed)\b|"
        r"\b(?:seller|vendor|manufacturer|brand|offer)(?:'s|’s)\s+"
        r"(?:description|language|message|instructions?|stated|"
        r"reported|claimed|listed|presented)\b",
        lowered,
    ))
    seller_attributed = seller_attributed or bool(re.search(
        r"\baccording to (?:the )?(?:seller|vendor|manufacturer|"
        r"product page|sales page|offer)\b",
        lowered,
    ))
    seller_attributed = seller_attributed or bool(re.search(
        r"\b(?:is|are|was|were)\s+(?:described|listed|reported|stated|"
        r"presented|offered|confirmed|specified)\s+(?:by|in)\s+"
        r"(?:the\s+)?(?:seller|vendor|manufacturer|brand|offer|"
        r"product page|sales page|materials?)\b",
        lowered,
    ))
    seller_attributed = seller_attributed or bool(re.search(
        r"\b(?:the\s+)?(?:seller|vendor|manufacturer|brand|offer)\s+"
        r"(?:is|was)\s+(?:clear|explicit|specific|transparent)\b",
        lowered,
    ))
    source_attributed = seller_attributed or bool(re.search(
        r"\b(?:according to|the source|the record|the cited|"
        r"documentation|reported by|published by)\b",
        lowered,
    ))
    return seller_attributed, source_attributed


def _sentence_records(
    article: str, seller_subject: str = ""
) -> list[dict]:
    """Split within semantic blocks and preserve forward attribution scope.

    Flattening the entire document joined an H2 to its following paragraph.
    That changed the apparent subject of pricing and feature sentences and
    produced both false mappings and hidden attribution failures.

    Natural copy often opens a paragraph with "According to the seller" and
    then gives two or three related sentences. That attribution governs only
    the remainder of the same paragraph; it never flows backward to a claim
    stated before the attribution or forward into another HTML block.

    A seller-attributed paragraph ending in a colon may also introduce the
    immediately following list. In that narrow case, the introduction governs
    each direct list item. It does not govern a later paragraph or another
    list, which keeps attribution bounded to the visible editorial structure.
    """
    soup = BeautifulSoup(html.unescape(article or ""), "html.parser")
    blocks = soup.find_all(["p", "li", "td", "th", "figcaption"])
    if not blocks:
        blocks = [soup]
    records = []
    for block in blocks:
        plain = re.sub(r"\s+", " ", block.get_text(" ", strip=True)).strip()
        seller_scope = False
        source_scope = False
        if block.name == "li" and block.parent is not None:
            list_node = block.parent
            previous = list_node.find_previous_sibling()
            if previous is not None and previous.name == "p":
                introduction = re.sub(
                    r"\s+",
                    " ",
                    previous.get_text(" ", strip=True),
                ).strip()
                if introduction.endswith(":"):
                    seller_scope, source_scope = _attribution_signals(
                        introduction, seller_subject
                    )
        for item in re.split(r"(?<=[.!?])\s+", plain):
            sentence = item.strip()
            if len(sentence) < 20:
                continue
            local_seller, local_source = _attribution_signals(
                sentence, seller_subject
            )
            seller_scope = seller_scope or local_seller
            source_scope = source_scope or local_source
            records.append({
                "text": sentence,
                "seller_attributed": seller_scope,
                "source_attributed": source_scope,
            })
    return records


def prune_unattributed_claim_blocks(pack: dict, article: str) -> tuple[str, dict]:
    """Conservatively remove unsafe semantic blocks from a paid repair.

    This is a zero-cost recovery primitive, not a way to manufacture
    attribution. It deletes an entire paragraph or list item when that block
    contains a mapped sealed claim that still lacks its required attribution.
    Callers must re-run the complete depth, format, coverage, and provenance
    gates before accepting the result.
    """
    ledger = build_article_claim_ledger(pack, article)
    violation_sentences = {
        str(item.get("article_sentence") or "").strip()
        for item in ledger.get("attribution_violations") or []
        if str(item.get("article_sentence") or "").strip()
    }
    if not violation_sentences:
        return article, {
            "changed": False,
            "removed_block_count": 0,
            "removed_sentences": [],
        }

    soup = BeautifulSoup(html.unescape(article or ""), "html.parser")
    removed_sentences = []
    removed_block_count = 0
    for block in list(soup.find_all(["p", "li", "figcaption"])):
        plain = re.sub(
            r"\s+", " ", block.get_text(" ", strip=True)
        ).strip()
        matching = [
            sentence for sentence in violation_sentences
            if sentence in plain
        ]
        if not matching:
            continue
        removed_block_count += 1
        removed_sentences.extend(matching)
        block.decompose()

    for list_node in list(soup.find_all(["ul", "ol"])):
        if not list_node.get_text(" ", strip=True):
            list_node.decompose()

    return str(soup), {
        "changed": bool(removed_sentences),
        "removed_block_count": removed_block_count,
        "removed_sentences": sorted(set(removed_sentences)),
    }


def repair_bidirectional_claim_blocks(
    pack: dict, article: str, affiliate_href: str = ""
) -> tuple[str, dict]:
    """Delete deterministic false claims, prune attribution, and fix CTAs.

    This bounded zero-model primitive only removes complete semantic blocks
    containing high-confidence unsupported claims, uses the established
    attribution prune, and performs meaning-preserving CTA cleanup. Callers
    must re-run every depth, coverage, formatting, and semantic gate before
    accepting the result.
    """
    from newswire_workbench.editorial_truth import (
        audit_editorial_truth,
        repair_cta_integrity,
    )

    initial = audit_editorial_truth(pack, article, affiliate_href)
    unsupported_sentences = {
        re.sub(
            r"\s+", " ", str(item.get("exact_text") or "")
        ).strip()
        for item in initial.get("grounding_violations") or []
        if str(item.get("exact_text") or "").strip()
    }
    soup = BeautifulSoup(html.unescape(article or ""), "html.parser")
    removed_truth_sentences = []
    for block in list(soup.find_all(["p", "li", "figcaption"])):
        plain = re.sub(
            r"\s+", " ", block.get_text(" ", strip=True)
        ).strip()
        matching = [
            sentence
            for sentence in unsupported_sentences
            if sentence in plain
        ]
        if not matching:
            continue
        removed_truth_sentences.extend(matching)
        block.decompose()
    for list_node in list(soup.find_all(["ul", "ol"])):
        if not list_node.get_text(" ", strip=True):
            list_node.decompose()

    attribution_pruned, attribution_report = (
        prune_unattributed_claim_blocks(pack, str(soup))
    )
    cta_repaired, cta_report = repair_cta_integrity(
        pack, attribution_pruned, affiliate_href
    )
    return cta_repaired, {
        "changed": bool(
            removed_truth_sentences
            or attribution_report["changed"]
            or cta_report["changed"]
        ),
        "removed_truth_block_count": len(removed_truth_sentences),
        "removed_truth_sentences": sorted(set(removed_truth_sentences)),
        "attribution": attribution_report,
        "cta": cta_report,
    }


def _sentences(article: str) -> list[str]:
    """Compatibility wrapper returning only sentence text."""
    return [record["text"] for record in _sentence_records(article)]


def _contact_heading_kind(value: str) -> tuple[bool, bool]:
    """Return (belongs_to_contact_block, anchors_contact_block)."""
    folded = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    anchor = bool(
        "contact" in folded
        or re.search(r"\b(?:product|customer|order|media)\s+support\b", folded)
        or folded in {"support", "support information"}
    )
    family = bool(
        anchor
        or re.search(r"\b(?:billing|refund|guarantee)\s+(?:support|terms?)\b", folded)
        or folded in {
            "billing",
            "refund",
            "refund terms",
            "guarantee",
            "guarantee terms",
        }
    )
    return family, anchor


def _contact_section_nodes(soup: BeautifulSoup) -> list:
    """Return the final contiguous contact/support heading family.

    Valid newswire copy commonly splits the final block into ``Product
    Support``, ``Order Support and Billing``, and ``Refund Terms`` H3s. The
    earlier gate required the literal word ``Contact`` and therefore reported
    every exact value as missing even when all of them were visibly present.
    """
    headings = list(soup.find_all(["h1", "h2", "h3"]))
    runs = []
    current = []
    for heading in headings:
        family, _ = _contact_heading_kind(
            heading.get_text(" ", strip=True)
        )
        if family:
            current.append(heading)
        else:
            if current:
                runs.append(current)
                current = []
    if current:
        runs.append(current)
    eligible = [
        run for run in runs
        if any(
            _contact_heading_kind(
                heading.get_text(" ", strip=True)
            )[1]
            for heading in run
        )
    ]
    if not eligible:
        return []
    run = eligible[-1]
    start = run[0]
    allowed_headings = {id(heading) for heading in run}
    nodes = [start]
    for sibling in start.next_siblings:
        name = getattr(sibling, "name", None)
        if name in {"h1", "h2", "h3"} and id(sibling) not in allowed_headings:
            break
        nodes.append(sibling)
    return nodes


def _contact_section(article: str) -> tuple[str, list[str]]:
    """Return visible final contact/support-block text and link destinations."""
    soup = BeautifulSoup(html.unescape(article or ""), "html.parser")
    nodes = _contact_section_nodes(soup)
    if not nodes:
        return "", []
    text = " ".join(
        node.get_text(" ", strip=True)
        if hasattr(node, "get_text")
        else str(node)
        for node in nodes
    )
    fragment = BeautifulSoup(
        "".join(str(node) for node in nodes), "html.parser"
    )
    links = [
        str(anchor.get("href") or "").strip()
        for anchor in fragment.find_all("a")
        if str(anchor.get("href") or "").strip()
    ]
    return re.sub(r"\s+", " ", text).strip(), links


def _safe_href(value: str, schemes=("http", "https")) -> str:
    clean = str(value or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme.casefold() not in schemes:
        return ""
    return clean


def _tel_href(value: str) -> str:
    clean = str(value or "").strip()
    digits = re.sub(r"\D", "", clean)
    if not digits:
        return ""
    return ("+" if clean.startswith("+") else "") + digits


def ensure_structured_contact_block(
    pack: dict, article: str
) -> tuple[str, dict]:
    """Render submitted contact facts mechanically instead of asking a model.

    The source pack owns these exact values. A model may shape the editorial
    body, but it must not be the component responsible for copying phone
    numbers, email addresses, support destinations, or the official URL.
    """
    manifest = normalized_intake_manifest(pack)
    contact = normalize_contact_information(
        manifest.get("contact_information")
    )
    if not contact:
        return article, {
            "changed": False,
            "field_count": 0,
            "replaced_existing_block": False,
        }

    soup = BeautifulSoup(html.unescape(article or ""), "html.parser")
    prior_nodes = _contact_section_nodes(soup)
    replaced_existing = bool(prior_nodes)
    for node in prior_nodes:
        if getattr(node, "parent", None) is not None:
            node.extract()

    product = pack.get("product") or {}
    product_name = str(product.get("product_name") or "").strip()
    official_url = str(product.get("official_url") or "").strip()
    refund_terms = str(manifest.get("refund_terms") or "").strip()
    parts = ["<h2><strong>Contact Information</strong></h2>", "<ul>"]
    if product_name:
        parts.append(
            "<li><strong>Product / Brand:</strong> "
            + html.escape(product_name)
            + "</li>"
        )
    media_name = str(contact.get("media_contact_name") or "").strip()
    if media_name:
        parts.append(
            "<li><strong>Media Contact:</strong> "
            + html.escape(media_name)
            + "</li>"
        )
    media_title = str(contact.get("media_contact_title") or "").strip()
    if media_title:
        parts.append(
            "<li><strong>Media Contact Title:</strong> "
            + html.escape(media_title)
            + "</li>"
        )
    support_email = str(contact.get("support_email") or "").strip()
    if support_email:
        escaped_email = html.escape(support_email)
        parts.append(
            '<li><strong>Product Support Email:</strong> <a href="mailto:'
            + html.escape(support_email, quote=True)
            + '">'
            + escaped_email
            + "</a></li>"
        )
    support_hours = str(contact.get("support_hours") or "").strip()
    if support_hours:
        parts.append(
            "<li><strong>Product Support Hours:</strong> "
            + html.escape(support_hours)
            + "</li>"
        )
    for field, label in (
        ("support_phone_us", "U.S. Support Phone"),
        ("support_phone_international", "International Support Phone"),
    ):
        value = str(contact.get(field) or "").strip()
        if not value:
            continue
        tel = _tel_href(value)
        rendered = html.escape(value)
        if tel:
            rendered = (
                '<a href="tel:'
                + html.escape(tel, quote=True)
                + '">'
                + rendered
                + "</a>"
            )
        parts.append(
            f"<li><strong>{label}:</strong> {rendered}</li>"
        )
    provider = str(contact.get("order_support_provider") or "").strip()
    if provider:
        parts.append(
            "<li><strong>Order Support Provider:</strong> "
            + html.escape(provider)
            + "</li>"
        )
    order_email = str(contact.get("order_support_email") or "").strip()
    if order_email:
        escaped_order_email = html.escape(order_email)
        parts.append(
            '<li><strong>Order Support Email:</strong> <a href="mailto:'
            + html.escape(order_email, quote=True)
            + '">'
            + escaped_order_email
            + "</a></li>"
        )
    order_url = str(contact.get("order_support_url") or "").strip()
    safe_order_url = _safe_href(order_url)
    if order_url:
        rendered = html.escape(order_url)
        if safe_order_url:
            rendered = (
                '<a href="'
                + html.escape(safe_order_url, quote=True)
                + '">'
                + rendered
                + "</a>"
            )
        parts.append(
            "<li><strong>Order Support URL:</strong> "
            + rendered
            + "</li>"
        )
    for field, label in (
        ("business_address", "Business Address"),
        ("return_address", "Product Return Address"),
    ):
        value = str(contact.get(field) or "").strip()
        if value:
            parts.append(
                f"<li><strong>{label}:</strong> "
                + html.escape(value)
                + "</li>"
            )
    safe_official_url = _safe_href(official_url)
    if official_url:
        rendered = html.escape(official_url)
        if safe_official_url:
            rendered = (
                '<a href="'
                + html.escape(safe_official_url, quote=True)
                + '">'
                + rendered
                + "</a>"
            )
        parts.append(
            "<li><strong>Official Product Website:</strong> "
            + rendered
            + "</li>"
        )
    parts.append("</ul>")
    if refund_terms:
        parts.append(
            "<p><strong>Refund / Guarantee Terms:</strong> According to the "
            "seller, "
            + html.escape(refund_terms)
            + ".</p>"
        )
    fragment = BeautifulSoup("".join(parts), "html.parser")
    for node in list(fragment.contents):
        soup.append(node)
    rendered_article = str(soup)
    return rendered_article, {
        "changed": rendered_article != article,
        "field_count": len(contact),
        "replaced_existing_block": replaced_existing,
    }


def _phone_digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _url_host(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    return (parsed.hostname or "").casefold().removeprefix("www.")


def _contact_coverage_violations(pack: dict, article: str) -> list[dict]:
    """Require every submitted public support fact in the final contact block."""
    manifest = normalized_intake_manifest(pack)
    contact = normalize_contact_information(
        manifest.get("contact_information")
    )
    refund_terms = str(manifest.get("refund_terms") or "").strip()
    violations = []
    section_text, section_links = _contact_section(article)
    section_folded = section_text.casefold()
    link_folded = [link.casefold() for link in section_links]

    if contact and not section_text:
        violations.append({
            "id": "P-COVERAGE-CONTACT-SECTION",
            "issue": (
                "Structured support details were supplied, but the article "
                "has no final Contact Information section."
            ),
            "required": 1,
            "actual": 0,
        })
        return violations

    text_fields = {
        "media_contact_name": "media contact name",
        "media_contact_title": "media contact title",
        "support_hours": "product support hours",
        "order_support_provider": "order support provider",
        "business_address": "business address",
        "return_address": "product return address",
    }
    for field, label in text_fields.items():
        value = str(contact.get(field) or "").strip()
        if value and value.casefold() not in section_folded:
            violations.append({
                "id": f"P-COVERAGE-CONTACT-{field.upper()}",
                "issue": (
                    f"The supplied {label} is missing from the final "
                    "Contact Information section."
                ),
                "required": value,
                "actual": "",
            })

    email = str(contact.get("support_email") or "").strip().casefold()
    if email and not (
        email in section_folded
        or f"mailto:{email}" in link_folded
    ):
        violations.append({
            "id": "P-COVERAGE-CONTACT-SUPPORT_EMAIL",
            "issue": (
                "The supplied product support email is missing from the final "
                "Contact Information section."
            ),
            "required": email,
            "actual": "",
        })

    order_email = str(
        contact.get("order_support_email") or ""
    ).strip().casefold()
    if order_email and not (
        order_email in section_folded
        or f"mailto:{order_email}" in link_folded
    ):
        violations.append({
            "id": "P-COVERAGE-CONTACT-ORDER_SUPPORT_EMAIL",
            "issue": (
                "The supplied order support email is missing from the final "
                "Contact Information section."
            ),
            "required": order_email,
            "actual": "",
        })

    for field, label in (
        ("support_phone_us", "U.S. support phone"),
        ("support_phone_international", "international support phone"),
    ):
        value = str(contact.get(field) or "").strip()
        digits = _phone_digits(value)
        section_digits = _phone_digits(section_text)
        link_digits = {_phone_digits(link) for link in section_links}
        if digits and digits not in section_digits and digits not in link_digits:
            violations.append({
                "id": f"P-COVERAGE-CONTACT-{field.upper()}",
                "issue": (
                    f"The supplied {label} is missing from the final "
                    "Contact Information section."
                ),
                "required": value,
                "actual": "",
            })

    order_url = str(contact.get("order_support_url") or "").strip()
    order_host = _url_host(order_url)
    section_hosts = {_url_host(link) for link in section_links}
    if order_host and (
        order_host not in section_hosts
        and order_host not in section_folded
    ):
        violations.append({
            "id": "P-COVERAGE-CONTACT-ORDER_SUPPORT_URL",
            "issue": (
                "The supplied order-support destination is missing from the "
                "final Contact Information section."
            ),
            "required": order_url,
            "actual": "",
        })

    official_url = str(
        (pack.get("product") or {}).get("official_url") or ""
    ).strip()
    official_host = _url_host(official_url)
    if contact and official_host and (
        official_host not in section_hosts
        and official_host not in section_folded
    ):
        violations.append({
            "id": "P-COVERAGE-CONTACT-OFFICIAL_URL",
            "issue": (
                "The official product website is missing from the final "
                "Contact Information section."
            ),
            "required": official_url,
            "actual": "",
        })

    if refund_terms:
        article_folded = BeautifulSoup(
            html.unescape(article or ""), "html.parser"
        ).get_text(" ", strip=True).casefold()
        required_numbers = _numbers(refund_terms)
        has_refund_word = bool(re.search(
            r"\b(?:refund|money[- ]back|guarantee)\b", article_folded
        ))
        if (
            not required_numbers.issubset(_numbers(article_folded))
            or not has_refund_word
        ):
            violations.append({
                "id": "P-COVERAGE-REFUND-TERMS",
                "issue": (
                    "The structured refund/guarantee terms are missing from "
                    "the article."
                ),
                "required": refund_terms,
                "actual": "",
            })
    return violations


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
            # Empty extracted dictionary cells such as ``phone:`` are labels,
            # not publication claims. Historical sealed packs can contain
            # them, so audit defensively as well as fixing future sealing.
            if re.fullmatch(r"[\w /&().-]+:\s*", text):
                continue
            treatment = item.get(
                "publication_treatment", "direct_fact_allowed"
            )
            # Exact public contact destinations are directly reproducible
            # facts. Requiring "according to the seller" before an email,
            # phone number, or URL produces unnatural contact blocks and adds
            # no provenance value; exact-value coverage is enforced below.
            if claim_type == "company_info" and (
                re.search(r"https?://|[\w.+-]+@[\w.-]+\.[a-z]{2,}", text, re.I)
                or len(_phone_digits(text)) >= 7
            ):
                treatment = "direct_fact_allowed"
            claims.append({
                "claim_id": claim_id,
                "claim_type": claim_type,
                "text": text,
                "artifact_id": item.get("artifact_id", ""),
                "source_class": item.get("source_class", ""),
                "publication_treatment": treatment,
                "metadata": item.get("metadata") or {},
                "tokens": _tokens(text),
            })

    mappings = []
    attribution_violations = []
    attribution_violation_keys = set()
    attribution_violation_index = {}
    product = pack.get("product") or {}
    platform = str(
        product.get("publishing_platform")
        or product.get("publishing_channel")
        or (pack.get("release_details") or {}).get("platform")
        or ""
    )
    seller_subject = (
        str(product.get("product_name") or "").strip()
        if "globe" in platform.casefold()
        else ""
    )
    for sentence_record in _sentence_records(article, seller_subject):
        sentence = sentence_record["text"]
        sentence_tokens = _tokens(sentence)
        matches = []
        for claim in claims:
            overlap = len(sentence_tokens & claim["tokens"])
            claim_token_count = len(claim["tokens"])
            required_overlap = min(3, claim_token_count)
            denominator = max(
                min(len(sentence_tokens), claim_token_count), 1
            )
            claim_numbers = _numbers(claim["text"])
            sentence_numbers = _numbers(sentence)
            hypothetical = sentence.rstrip().endswith("?") or bool(re.search(
                r"\b(?:ask whether|check whether|verify whether|"
                r"if the seller|whether the product|could it|does it)\b",
                sentence.casefold(),
            ))
            polarity_conflict = (
                _negated(sentence) != _negated(claim["text"])
                and overlap >= required_overlap
            )
            if (
                claim_token_count
                and overlap >= required_overlap
                # Precision matters more than fuzzy recall here. The previous
                # denominator compared against the shorter sentence and mapped
                # generic "support/billing/issues" copy to a ClickBank claim,
                # and a generic digital-product sentence to a much richer
                # Fortune Numbers claim.
                and (
                    claim_token_count <= 3
                    or overlap / claim_token_count >= 0.65
                )
                and overlap / denominator >= 0.45
                # A mapped clause may not smuggle in additional quantities.
                # Every numeric atom must be supported by the same claim.
                and claim_numbers == sentence_numbers
                and not polarity_conflict
                and not hypothetical
            ):
                matches.append({
                    key: value for key, value in claim.items() if key != "tokens"
                })
        if matches:
            mappings.append({"article_sentence": sentence, "claims": matches})
            seller_attributed = sentence_record["seller_attributed"]
            source_attributed = sentence_record["source_attributed"]
            for claim in matches:
                treatment = claim.get("publication_treatment")
                if (
                    treatment == "seller_attribution_required"
                    and not seller_attributed
                ) or (
                    treatment == "source_attribution_required"
                    and not source_attributed
                ):
                    violation_key = (sentence, treatment)
                    if violation_key in attribution_violation_keys:
                        attribution_violations[
                            attribution_violation_index[violation_key]
                        ]["claims"].append({
                            "claim_id": claim["claim_id"],
                            "claim_text": claim["text"],
                            "claim_type": claim["claim_type"],
                            "source_class": claim["source_class"],
                            "artifact_id": claim["artifact_id"],
                        })
                        continue
                    attribution_violation_keys.add(violation_key)
                    attribution_violation_index[violation_key] = len(
                        attribution_violations
                    )
                    attribution_violations.append({
                        "article_sentence": sentence,
                        "claim_id": claim["claim_id"],
                        "claim_text": claim["text"],
                        "required_treatment": treatment,
                        "claims": [{
                            "claim_id": claim["claim_id"],
                            "claim_text": claim["text"],
                            "claim_type": claim["claim_type"],
                            "source_class": claim["source_class"],
                            "artifact_id": claim["artifact_id"],
                        }],
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
    pricing_claim_ids = {
        claim["claim_id"] for claim in claims
        if claim.get("claim_type") == "pricing"
        and (
            re.search(
                r"(?:[$£€¥]\s*\d)|"
                r"(?:\b\d[\d,.]*\s*(?:USD|CAD|AUD|GBP|EUR)\b)",
                claim.get("text") or "",
                re.I,
            )
            or (
                str(
                    (claim.get("metadata") or {}).get(
                        "source_pack_field"
                    ) or ""
                ).casefold() == "pricing"
                and not re.search(
                    r"\b(?:not specified|not provided|unavailable|unknown)\b",
                    claim.get("text") or "",
                    re.I,
                )
            )
        )
    }
    if pricing_claim_ids and not (pricing_claim_ids & used_ids):
        coverage_violations.append({
            "id": "P-COVERAGE-PRICING",
            "issue": (
                "The sealed record contains publishable monetary pricing, "
                "but the article does not state any seller-attributed price."
            ),
            "required": 1,
            "actual": 0,
        })
    coverage_violations.extend(
        _contact_coverage_violations(pack, article)
    )
    from newswire_workbench.editorial_truth import audit_editorial_truth
    editorial_truth = audit_editorial_truth(
        pack,
        article,
        str(
            (pack.get("intake_manifest") or {}).get("affiliate_link")
            or (pack.get("release_details") or {}).get("affiliate_link")
            or ""
        ),
    )
    return {
        "schema_version": 3,
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
        "grounding_violations": editorial_truth[
            "grounding_violations"
        ],
        "cta_integrity_violations": editorial_truth[
            "cta_integrity_violations"
        ],
        "editorial_truth": editorial_truth,
        "passed": not (
            attribution_violations
            or coverage_violations
            or editorial_truth["grounding_violations"]
            or editorial_truth["cta_integrity_violations"]
        ),
        "excluded_claims": pack.get("excluded_publication_claims") or [],
        "scope_note": (
            "This bidirectional ledger checks required source-to-article "
            "coverage, article-to-source high-confidence grounding, and CTA "
            "destination integrity. Explicit review candidates remain the "
            "independent semantic reviewer's responsibility."
        ),
    }
