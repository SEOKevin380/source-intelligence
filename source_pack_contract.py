"""Versioned publication contract between Source Intelligence and publishers."""

import copy
import hashlib
import html
import json
import re
from datetime import datetime, timezone

from offering_taxonomy import (
    CANONICAL_PRODUCT_TYPES,
    normalize_product_type,
)

CONTRACT_NAME = "mbk.source-intelligence.publication-pack"
CONTRACT_VERSION = 2
MINIMUM_PUBLICATION_CLAIMS = 3

DEVICE_ATTRIBUTABLE_CLAIM_TYPES = frozenset({
    "feature",
    "specification",
    "pricing",
    "refund_policy",
    "shipping_policy",
    "company_info",
    "manufacturer_claim",
})
SELLER_SOURCE_CLASSES = frozenset({
    "official_vendor",
    "authorized_reseller",
})

STRUCTURED_PRODUCT_CLAIM_TYPES = {
    "key_features": "feature",
    "specifications": "specification",
    "power_source": "specification",
    "pricing": "pricing",
    "services_offered": "feature",
    "pricing_tiers": "pricing",
    "whats_included": "feature",
    "format": "specification",
    "access_method": "feature",
    "platform_support": "specification",
    "integrations": "feature",
    "support_options": "feature",
    "service_description": "feature",
    "service_area": "feature",
    "program_structure": "feature",
    "duration": "specification",
    "included_items": "feature",
    "billing_frequency": "pricing",
    "billing_terms": "pricing",
    "cancellation_policy": "refund_policy",
    "cancellation_terms": "refund_policy",
    "renewal_terms": "pricing",
    "trial_period": "pricing",
    "eligibility": "specification",
    "odds_or_randomness_disclosure": "feature",
    "warnings": "feature",
    "guarantees": "refund_policy",
    "refund_policy": "refund_policy",
    "company": "company_info",
}

CONTACT_INFORMATION_FIELDS = (
    "media_contact_name",
    "support_email",
    "support_phone_us",
    "support_phone_international",
    "order_support_provider",
    "order_support_url",
)


def normalize_contact_information(value=None) -> dict:
    """Return the stable public contact schema used across intake and output."""
    raw = value if isinstance(value, dict) else {}
    aliases = {
        "media_contact": "media_contact_name",
        "contact_name": "media_contact_name",
        "email": "support_email",
        "product_support_email": "support_email",
        "phone": "support_phone_us",
        "phone_us": "support_phone_us",
        "us_phone": "support_phone_us",
        "phone_intl": "support_phone_international",
        "international_phone": "support_phone_international",
        "order_support": "order_support_provider",
        "order_support_link": "order_support_url",
    }
    normalized = {}
    for key, value_item in raw.items():
        canonical = aliases.get(str(key).strip(), str(key).strip())
        if canonical not in CONTACT_INFORMATION_FIELDS:
            continue
        clean = " ".join(str(value_item or "").split()).strip()
        if clean and clean.casefold() not in {
            "unknown", "not established", "n/a", "none",
        }:
            normalized[canonical] = clean
    return {
        key: normalized[key]
        for key in CONTACT_INFORMATION_FIELDS
        if key in normalized
    }


def extract_legacy_intake_terms(operator_notes: str) -> tuple[dict, str]:
    """Recover common support/refund fields from historical free-text intake.

    This migration exists so already-researched products do not require another
    source crawl merely because the old form stored structured facts in Notes.
    """
    notes = str(operator_notes or "").strip()
    if not notes:
        return {}, ""

    contact = {}
    product_support_block = re.search(
        r"(?ims)^\s*Product\s+Support\s*:?\s*(.*?)"
        r"(?=^\s*[A-Za-z0-9 .&'-]+\s+Order\s+Support\s*:|"
        r"^\s*(?:Refund|Guarantee|Return)\b|\Z)",
        notes,
    )
    product_support_text = (
        product_support_block.group(1)
        if product_support_block else notes
    )

    email_match = re.search(
        r"(?im)^\s*(?:product\s+support\s+)?email\s*:\s*"
        r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})\s*$",
        product_support_text,
    )
    if email_match:
        contact["support_email"] = email_match.group(1)

    media_match = re.search(
        r"(?im)^\s*(?:media\s+contact|contact\s+name)\s*:\s*(.+?)\s*$",
        notes,
    )
    if media_match:
        contact["media_contact_name"] = media_match.group(1)

    product_phone = re.search(
        r"(?im)^\s*(?:product\s+support\s+)?phone(?:\s+number)?\s*:\s*"
        r"(\+?[\d().\s-]{7,})\s*$",
        product_support_text,
    )
    if product_phone:
        contact["support_phone_us"] = product_phone.group(1).strip()

    us_phone = re.search(
        r"(?im)^\s*(?:U\.?S\.?|USA|United\s+States)\s*:\s*"
        r"(\+?[\d().\s-]{7,})\s*$",
        notes,
    )
    if us_phone and "support_phone_us" not in contact:
        contact["support_phone_us"] = us_phone.group(1).strip()

    international_phone = re.search(
        r"(?im)^\s*(?:INTL?|International)\s*:\s*"
        r"(\+?[\d().\s-]{7,})\s*$",
        notes,
    )
    if international_phone:
        contact["support_phone_international"] = (
            international_phone.group(1).strip()
        )

    order_label = re.search(
        r"(?im)^\s*([A-Za-z0-9 .&'-]+?)\s+Order\s+Support\s*:"
        r"\s*(https?://\S+)?\s*$",
        notes,
    )
    if order_label:
        provider = order_label.group(1).strip()
        if provider:
            contact["order_support_provider"] = provider
        if order_label.group(2):
            contact["order_support_url"] = order_label.group(2).strip()
        else:
            following = notes[order_label.end():]
            url_match = re.search(r"(?im)^\s*(https?://\S+)\s*$", following)
            if url_match:
                contact["order_support_url"] = url_match.group(1).strip()

    refund_match = re.search(
        r"(?im)^\s*(.*\b\d{1,3}\s*[- ]?\s*day\b.*"
        r"(?:refund|money[- ]back|guarantee).*)\s*$",
        notes,
    )
    if not refund_match:
        refund_match = re.search(
            r"(?im)^\s*(.*(?:refund|money[- ]back|guarantee).*\b"
            r"\d{1,3}\s*[- ]?\s*day\b.*)\s*$",
            notes,
        )
    refund_terms = " ".join(refund_match.group(1).split()) if refund_match else ""
    return normalize_contact_information(contact), refund_terms


def resolve_intake_contact_terms(
    operator_notes: str,
    explicit_contact=None,
    explicit_refund_terms: str = "",
) -> tuple[dict, str]:
    """Turn free-text intake into structured facts; explicit overrides win."""
    inferred_contact, inferred_refund = extract_legacy_intake_terms(
        operator_notes
    )
    merged = dict(inferred_contact)
    merged.update(normalize_contact_information(explicit_contact))
    refund_terms = " ".join(
        str(explicit_refund_terms or inferred_refund).split()
    ).strip()
    return normalize_contact_information(merged), refund_terms


def extract_labeled_source_inputs(operator_notes: str) -> dict:
    """Recover optional source URLs from ordinary labeled team notes."""
    notes = str(operator_notes or "")
    if not notes.strip():
        return {}
    result = {}
    previous = []
    competitors = []
    active_label = ""
    for raw_line in notes.splitlines():
        line = raw_line.strip()
        if not line:
            active_label = ""
            continue
        folded = line.casefold()
        if re.search(r"\b(?:vsl|video sales letter)\b", folded):
            active_label = "vsl_url"
        elif re.search(
            r"\b(?:label|supplement facts|product facts|references page)\b",
            folded,
        ):
            active_label = "label_source_url"
        elif re.search(r"\b(?:previous|prior)\s+releases?\b", folded):
            active_label = "previous_releases"
        elif re.search(r"\bcompetitor\s+releases?\b", folded):
            active_label = "competitor_releases"
        elif (
            not re.match(r"https?://", line, re.I)
            and re.match(r"^[A-Za-z][A-Za-z /&'-]{2,40}:\s*", line)
        ):
            active_label = ""
        urls = re.findall(r"https?://[^\s,]+", line)
        if not urls or not active_label:
            continue
        urls = [url.rstrip(").,;") for url in urls]
        if active_label == "previous_releases":
            previous.extend(urls)
        elif active_label == "competitor_releases":
            competitors.extend(urls)
        else:
            result.setdefault(active_label, urls[0])
    if previous:
        result["previous_releases"] = ", ".join(dict.fromkeys(previous))
    if competitors:
        result["competitor_releases"] = ", ".join(
            dict.fromkeys(competitors)
        )
    return result


def normalized_intake_manifest(pack: dict) -> dict:
    """Return a manifest with structured public contact/terms migration."""
    manifest = copy.deepcopy((pack or {}).get("intake_manifest") or {})
    structured, refund_terms = resolve_intake_contact_terms(
        manifest.get("operator_notes", ""),
        manifest.get("contact_information"),
        manifest.get("refund_terms", ""),
    )
    if structured:
        manifest["contact_information"] = structured
    else:
        manifest.pop("contact_information", None)
    if refund_terms:
        manifest["refund_terms"] = refund_terms
    else:
        manifest.pop("refund_terms", None)
    return manifest

_CONTEXTUAL_SELLER_HEADING_BLOCKLIST = (
    "order",
    "customer review",
    "what our customers",
    "money back",
    "guarantee",
    "limited time",
    "off now",
    "discount",
    "save up to",
    "cut your",
    "slash your",
    "save",
)


PLATFORM_LABELS = {
    "accessnewswire": "Accesswire",
    "accesswire": "Accesswire",
    "access newswire": "Accesswire",
    "barchart": "Barchart Advertorial",
    "barchart advertorial": "Barchart Advertorial",
    "newswire": "Newswire.com",
    "newswire.com": "Newswire.com",
    "globe": "Globe Newswire",
    "globe newswire": "Globe Newswire",
    "domain": "Domain Site",
    "domain site": "Domain Site",
}


def normalize_platform_label(value: str, default: str = "Accesswire") -> str:
    """Return the canonical UI label without silently changing platforms."""
    text = str(value or "").strip()
    return PLATFORM_LABELS.get(text.casefold(), text or default)


def form_values_from_pack(pack: dict) -> dict:
    """Restore every intake control from a saved publication pack.

    The intake manifest is the source of truth. Product fields are used only
    for legacy packs created before the manifest was introduced.
    """
    product = (pack or {}).get("product", {}) or {}
    manifest = normalized_intake_manifest(pack or {})
    contact = normalize_contact_information(
        manifest.get("contact_information")
    )
    return {
        "product_url": manifest.get("product_url") or product.get("official_url", ""),
        "product_name": manifest.get("product_name") or product.get("product_name", "Unknown"),
        "vsl_url": manifest.get("vsl_url", ""),
        "label_url": manifest.get("label_source_url", ""),
        "rd_affiliate": manifest.get("affiliate_link", ""),
        "rd_platform": normalize_platform_label(
            manifest.get("publishing_channel")
            or product.get("publishing_platform")
            or product.get("publishing_channel")
        ),
        "rd_previous": manifest.get("previous_releases") or "FIRST RELEASE",
        "rd_competitor": manifest.get("competitor_releases", ""),
        "rd_client_title": manifest.get("client_locked_title", ""),
        "rd_notes": manifest.get("operator_notes", ""),
        "rd_media_contact_name": contact.get("media_contact_name", ""),
        "rd_support_email": contact.get("support_email", ""),
        "rd_support_phone_us": contact.get("support_phone_us", ""),
        "rd_support_phone_international": contact.get(
            "support_phone_international", ""
        ),
        "rd_order_support_provider": contact.get(
            "order_support_provider", ""
        ),
        "rd_order_support_url": contact.get("order_support_url", ""),
        "rd_refund_terms": manifest.get("refund_terms", ""),
    }


def _canonical_payload(pack: dict) -> bytes:
    payload = copy.deepcopy(pack)
    contract = payload.get("source_pack_contract", {})
    contract.pop("sha256", None)
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _structured_source_artifact(pack: dict) -> tuple[str, str]:
    """Resolve provenance for structured facts without laundering context."""
    artifacts = pack.get("all_artifacts") or {}
    if isinstance(artifacts, dict):
        candidates = [
            (str(artifact_id), artifact or {})
            for artifact_id, artifact in artifacts.items()
        ]
    else:
        candidates = [
            (str(artifact.get("artifact_id", "")), artifact)
            for artifact in artifacts
            if isinstance(artifact, dict)
        ]
    for preferred in ("official_vendor", "authorized_reseller"):
        for artifact_id, artifact in candidates:
            source_class = str(
                artifact.get("source_class", "")
            ).strip().casefold()
            if artifact_id and source_class == preferred:
                return artifact_id, source_class
    official_url = str(
        (pack.get("product") or {}).get("official_url", "")
    ).strip()
    official_manifest_ids = {
        str(item.get("artifact_id", ""))
        for item in pack.get("source_manifest", []) or []
        if isinstance(item, dict)
        and str(item.get("type", "")).casefold()
        in {"official", "official_page", "vendor_page"}
    }
    for artifact_id, artifact in candidates:
        source_url = str(artifact.get("source_url", "")).strip()
        if artifact_id and (
            artifact_id in official_manifest_ids
            or (official_url and source_url == official_url)
        ):
            return artifact_id, "official_vendor"
    # Older packs may omit source_class. Preserve the missing provenance rather
    # than falsely promoting the first artifact to an official seller record.
    for artifact_id, artifact in candidates:
        if artifact_id:
            return artifact_id, str(
                artifact.get("source_class", "")
            ).strip().casefold()
    return "", ""


def _structured_product_claims(pack: dict) -> dict:
    """Migrate captured structured fields into attributed publication claims.

    Older reports populated the CVD/source brief but did not always populate
    the parallel claim ledger. These are not promoted to independent facts:
    they remain explicitly seller/source-material attributed.
    """
    product = pack.get("product") or {}
    artifact_id, source_class = _structured_source_artifact(pack)
    migrated = {}

    def add(claim_type: str, field: str, text: str):
        clean = str(text or "").strip()
        if not clean or clean.casefold() in {
            "not established", "unknown", "none", "n/a",
        } or re.search(r"\[[^\]]+\]", clean):
            return
        migrated.setdefault(claim_type, []).append({
            "text": clean,
            "artifact_id": artifact_id,
            "source_class": source_class,
            "review_status": "needs_verification",
            "publication_treatment": "seller_attribution_required",
            "metadata": {
                "excerpt_is_literal": False,
                "structured_source_record": True,
                "source_pack_field": field,
            },
        })

    for field, claim_type in STRUCTURED_PRODUCT_CLAIM_TYPES.items():
        value = product.get(field)
        if not value:
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    label = item.get("package") or item.get("name") or field
                    price = item.get("price") or item.get("total")
                    per_unit = item.get("per_unit")
                    parts = []
                    if price:
                        parts.append(f"${str(price).lstrip('$')}")
                    if per_unit:
                        parts.append(f"${str(per_unit).lstrip('$')} per unit")
                    if parts:
                        add(claim_type, field, f"{label}: " + "; ".join(parts))
                    elif item:
                        add(claim_type, field, f"{label}: {item}")
                else:
                    add(claim_type, field, item)
        elif isinstance(value, dict):
            # Structured fields can arrive with different insertion orders
            # after JSON/database round trips. Claim order must not change the
            # sealed contract identity for otherwise identical facts.
            for key in sorted(value, key=lambda item: str(item).casefold()):
                item = value[key]
                clean_item = str(item or "").strip()
                if (
                    not clean_item
                    or clean_item.casefold() in {
                        "not established", "unknown", "none", "n/a",
                    }
                    or re.search(r"\[[^\]]+\]", clean_item)
                ):
                    continue
                add(
                    claim_type,
                    field,
                    f"{str(key).replace('_', ' ')}: {clean_item}",
                )
        else:
            add(claim_type, field, value)
    return migrated


def _merge_structured_claims(source_claims: dict, pack: dict) -> dict:
    """Add missing structured facts even when an unusable raw ledger exists."""
    merged = copy.deepcopy(source_claims or {})
    existing = {
        str(claim.get("text", "")).strip().casefold()
        for items in merged.values()
        for claim in (items or [])
        if isinstance(claim, dict)
    }
    for claim_type, items in _structured_product_claims(pack).items():
        for claim in items:
            key = str(claim.get("text", "")).strip().casefold()
            if key and key not in existing:
                merged.setdefault(claim_type, []).append(claim)
                existing.add(key)
    return merged


def _intake_publication_claims(pack: dict) -> dict:
    """Expose structured operator-submitted support facts to publication."""
    manifest = normalized_intake_manifest(pack)
    contact = normalize_contact_information(
        manifest.get("contact_information")
    )
    refund_terms = str(manifest.get("refund_terms") or "").strip()
    artifact_id = ""
    for item in pack.get("source_manifest") or []:
        if (
            isinstance(item, dict)
            and str(item.get("type", "")).casefold() == "operator_intake"
        ):
            artifact_id = str(item.get("artifact_id") or "").strip()
            if artifact_id:
                break

    labels = {
        "media_contact_name": "Media contact name",
        "support_email": "Product support email",
        "support_phone_us": "United States support phone",
        "support_phone_international": "International support phone",
        "order_support_provider": "Order support provider",
        "order_support_url": "Order support URL",
    }
    claims = []
    for key in CONTACT_INFORMATION_FIELDS:
        value = contact.get(key)
        if not value:
            continue
        claims.append({
            "text": f"{labels[key]}: {value}",
            "artifact_id": artifact_id,
            "source_class": "operator_submitted",
            "review_status": "accepted",
            "publication_treatment": "direct_fact_allowed",
            "metadata": {
                "excerpt_is_literal": True,
                "structured_source_record": True,
                "source_pack_field": (
                    f"intake_manifest.contact_information.{key}"
                ),
            },
        })
    migrated = {"company_info": claims} if claims else {}
    if refund_terms:
        migrated.setdefault("refund_policy", []).append({
            "text": refund_terms,
            "artifact_id": artifact_id,
            "source_class": "operator_submitted",
            "review_status": "accepted",
            "publication_treatment": "seller_attribution_required",
            "metadata": {
                "excerpt_is_literal": True,
                "structured_source_record": True,
                "source_pack_field": "intake_manifest.refund_terms",
            },
        })
    return migrated


def _merge_intake_claims(source_claims: dict, pack: dict) -> dict:
    """Merge structured intake facts without duplicating extracted records."""
    merged = copy.deepcopy(source_claims or {})
    existing = {
        str(claim.get("text", "")).strip().casefold()
        for items in merged.values()
        for claim in (items or [])
        if isinstance(claim, dict)
    }
    for claim_type, items in _intake_publication_claims(pack).items():
        for claim in items:
            key = str(claim.get("text", "")).strip().casefold()
            if key and key not in existing:
                merged.setdefault(claim_type, []).append(claim)
                existing.add(key)
    return merged


def _contextual_seller_claims(pack: dict) -> dict:
    """Recover literal device claims from a supplied seller/affiliate page.

    A rendered seller page can be captured successfully while the general
    product extractor returns only pricing (commonly when a page is CSS- or
    script-heavy). The contextual profile retains literal headings from that
    captured artifact. For devices, conservative product-function headings are
    usable as seller statements, never as independently established facts.
    """
    product_type = str(
        (pack.get("product") or {}).get("product_type", "")
    ).strip().casefold()
    if product_type != "device":
        return {}

    recovered = []
    seen = set()
    artifacts = pack.get("all_artifacts") or {}
    if isinstance(artifacts, list):
        artifact_index = {
            str(item.get("artifact_id", "")): item
            for item in artifacts if isinstance(item, dict)
        }
    else:
        artifact_index = artifacts if isinstance(artifacts, dict) else {}
    for profile in pack.get("contextual_source_profiles") or []:
        if not isinstance(profile, dict):
            continue
        if str(profile.get("source_type", "")).casefold() != "affiliate_page":
            continue
        artifact_id = str(profile.get("artifact_id", "")).strip()
        if not artifact_id:
            continue
        artifact = artifact_index.get(artifact_id) or {}
        source_class = str(
            artifact.get("source_class") or profile.get("source_class") or ""
        ).strip().casefold()
        if source_class not in {
            "official_vendor", "authorized_reseller", "user_generated",
            "third_party_web_search",
        }:
            continue
        treatment = (
            "seller_attribution_required"
            if source_class in {"official_vendor", "authorized_reseller"}
            else "source_attribution_required"
        )
        for heading in profile.get("headings") or []:
            text = html.unescape(
                " ".join(str(heading or "").split()).strip()
            )
            folded = text.casefold()
            if (
                len(text) < 12
                or len(text) > 180
                or folded in seen
                or any(term in folded for term in _CONTEXTUAL_SELLER_HEADING_BLOCKLIST)
            ):
                continue
            # Avoid treating navigation/section furniture as a product claim.
            if folded in {
                "benefits of ecowatt",
                "how it works",
                "how many ecowatts do i need?",
                "order summary",
            }:
                continue
            seen.add(folded)
            recovered.append({
                "text": text,
                "artifact_id": artifact_id,
                "source_class": source_class,
                "review_status": "needs_verification",
                "publication_treatment": treatment,
                "metadata": {
                    "excerpt_is_literal": True,
                    "contextual_seller_heading": True,
                    "source_pack_field": "contextual_source_profiles.headings",
                },
            })
            # A long-form device advertorial needs more than three isolated
            # headings. Keep a bounded set of literal, non-promissory seller
            # statements so the writer has enough distinct source material
            # without importing sales-page hype.
            if len(recovered) >= 6:
                break
    return {"manufacturer_claim": recovered} if recovered else {}


def _merge_contextual_seller_claims(source_claims: dict, pack: dict) -> dict:
    """Use literal seller headings only when the primary ledger is too thin."""
    merged = copy.deepcopy(source_claims or {})
    count = sum(len(items or []) for items in merged.values())
    product_type = str(
        (pack.get("product") or {}).get("product_type", "")
    ).strip().casefold()
    contextual_floor = 6 if product_type == "device" else MINIMUM_PUBLICATION_CLAIMS
    if count >= contextual_floor:
        return merged
    existing = {
        str(claim.get("text", "")).strip().casefold()
        for items in merged.values()
        for claim in (items or [])
        if isinstance(claim, dict)
    }
    for claim_type, items in _contextual_seller_claims(pack).items():
        for claim in items:
            key = str(claim.get("text", "")).strip().casefold()
            if key and key not in existing:
                merged.setdefault(claim_type, []).append(claim)
                existing.add(key)
    return merged


def assess_readiness(full_data: dict) -> tuple:
    """Return (state, reasons). Limited packs remain publishable."""
    product = full_data.get("product", {}) or {}
    reasons = []
    if not str(product.get("product_name", "")).strip():
        reasons.append("missing_product_identity")
    if not str(product.get("official_url", "")).strip():
        reasons.append("missing_official_url")
    declared_product_type = product.get("product_type", "")
    product_type = normalize_product_type(declared_product_type)
    if declared_product_type and product_type not in CANONICAL_PRODUCT_TYPES:
        reasons.append(
            "unsupported_product_type:"
            + (product_type or "missing")
        )
    captured_manifest = any(
        str(item.get("status", "")).lower()
        in {"captured", "success", "fetched", "available", "reused"}
        for item in (full_data.get("source_manifest") or [])
        if isinstance(item, dict)
    )
    if not (full_data.get("all_artifacts") or captured_manifest):
        reasons.append("no_captured_source_material")
    publication_claim_count = sum(
        len(items or [])
        for items in (full_data.get("publication_claims") or {}).values()
    )
    if publication_claim_count < MINIMUM_PUBLICATION_CLAIMS:
        reasons.append(
            "insufficient_publication_claims:"
            f"{publication_claim_count}/{MINIMUM_PUBLICATION_CLAIMS}"
        )
    if reasons:
        return "blocked", reasons

    required = full_data.get("required_facts") or {}
    missing = list(required.get("missing") or [])
    if missing:
        return "limited", ["missing_required_facts:" + ",".join(missing)]
    return "complete", []


def seal_source_pack(full_data: dict) -> dict:
    """Return an immutable-style copy with contract metadata and content hash."""
    pack = copy.deepcopy(full_data)
    pack["intake_manifest"] = normalized_intake_manifest(pack)
    pack["intake_manifest_hash"] = hashlib.sha256(
        json.dumps(
            pack["intake_manifest"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    product = pack.setdefault("product", {})
    normalized_product_type = normalize_product_type(
        product.get("product_type", "")
    )
    if normalized_product_type:
        product["product_type"] = normalized_product_type
    compliance = pack.get("compliance") or {}
    blocked_texts = set()
    blocked_fragments = set()
    for result in compliance.get("results", []) or []:
        if not isinstance(result, dict):
            continue
        if str(result.get("state", "")).casefold() != "blocked":
            continue
        matched = str(result.get("matched_text", "")).strip().casefold()
        if matched:
            blocked_fragments.add(matched)
    for key in (
        "cvd9_blocked_claims", "deceptive_blocked_claims",
    ):
        for item in compliance.get(key, []) or []:
            if isinstance(item, dict):
                blocked_texts.add(str(item.get("claim", "")).strip().casefold())
    for check_key in (
        "accesswire_blocklist_check", "barchart_compliance",
        "globe_compliance",
    ):
        check = compliance.get(check_key) or {}
        for item in check.get("blocked_claims", []) or []:
            if isinstance(item, dict):
                blocked_texts.add(str(item.get("claim", "")).strip().casefold())

    publication_claims = {}
    excluded_claims = []
    product_type = str(
        (pack.get("product") or {}).get("product_type", "")
    ).strip().casefold()
    artifacts = pack.get("all_artifacts") or {}
    # Persisted reports created by older contract versions may retain the
    # already-vetted publication ledger without the original grouping. Reuse
    # that ledger instead of silently resealing it into an empty brain.
    source_claims = (
        pack.get("claims_by_type")
        or pack.get("publication_claims")
        or {}
    )
    source_claims = _merge_contextual_seller_claims(source_claims, pack)
    source_claims = _merge_structured_claims(source_claims, pack)
    source_claims = _merge_intake_claims(source_claims, pack)
    for claim_type, items in source_claims.items():
        for claim in items or []:
            status = str(claim.get("review_status", "unreviewed")).lower()
            metadata = claim.get("metadata") or {}
            literal = metadata.get("excerpt_is_literal", True)
            has_artifact = bool(claim.get("artifact_id"))
            artifact = (
                artifacts.get(claim.get("artifact_id"), {})
                if isinstance(artifacts, dict)
                else {}
            )
            source_class = str(
                claim.get("source_class")
                or artifact.get("source_class")
                or ""
            ).strip().casefold()
            normalized_claim_text = str(
                claim.get("text", "")
            ).strip().casefold()
            compliance_blocked = (
                normalized_claim_text in blocked_texts
                or any(
                    fragment in normalized_claim_text
                    for fragment in blocked_fragments
                )
            )
            seller_attribution_required = bool(
                claim_type in DEVICE_ATTRIBUTABLE_CLAIM_TYPES
                and (
                    (
                        product_type == "device"
                        and metadata.get("excerpt_is_literal") is True
                    )
                    or metadata.get("structured_source_record") is True
                )
                and has_artifact
                and source_class in SELLER_SOURCE_CLASSES
                and status in {"accepted", "unreviewed", "needs_verification"}
                and not compliance_blocked
            )
            source_attribution_required = bool(
                status in {"unreviewed", "needs_verification"}
                and literal
                and has_artifact
                and not seller_attribution_required
                and (
                    status == "unreviewed"
                    or metadata.get("contextual_seller_heading") is True
                )
                and not compliance_blocked
            )
            safe = not compliance_blocked and (
                status == "accepted"
                or (status == "unreviewed" and literal and has_artifact)
                or seller_attribution_required
                or source_attribution_required
            )
            if safe:
                publication_claim = copy.deepcopy(claim)
                if seller_attribution_required:
                    publication_claim["publication_treatment"] = (
                        "seller_attribution_required"
                    )
                elif source_attribution_required:
                    publication_claim["publication_treatment"] = (
                        "source_attribution_required"
                    )
                else:
                    publication_claim.setdefault(
                        "publication_treatment", "direct_fact_allowed"
                    )
                publication_claims.setdefault(claim_type, []).append(
                    publication_claim
                )
            else:
                excluded_claims.append({
                    "claim_type": claim_type,
                    "text": claim.get("text", ""),
                    "review_status": status,
                    "reason": (
                        "blocked_by_compliance"
                        if compliance_blocked
                        else "not_accepted_or_literal_artifact_backed"
                    ),
                })
    pack["publication_claims"] = publication_claims
    pack["excluded_publication_claims"] = excluded_claims
    pack["publication_claim_summary"] = {
        "raw_claim_count": sum(len(items or []) for items in source_claims.values()),
        "publication_claim_count": sum(
            len(items or []) for items in publication_claims.values()
        ),
        "excluded_claim_count": len(excluded_claims),
    }
    state, reasons = assess_readiness(pack)
    existing = pack.get("source_pack_contract", {}) or {}
    pack["source_pack_contract"] = {
        "name": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "generated_at": existing.get("generated_at")
        or datetime.now(timezone.utc).isoformat(),
        "readiness": state,
        "readiness_reasons": reasons,
        "source_of_truth": "source_intelligence",
        "generation_system": "MBK Master Content Generation System v3.8",
    }
    pack["source_pack_contract"]["sha256"] = hashlib.sha256(
        _canonical_payload(pack)
    ).hexdigest()
    return pack


def validate_source_pack(pack: dict, allow_limited: bool = True) -> dict:
    """Validate contract identity, version, hash, and publication readiness."""
    if not isinstance(pack, dict):
        raise ValueError("Source pack must be a JSON object")
    contract = pack.get("source_pack_contract") or {}
    if contract.get("name") != CONTRACT_NAME:
        raise ValueError("Not a Source Intelligence publication pack")
    if contract.get("version") != CONTRACT_VERSION:
        raise ValueError(
            f"Unsupported source-pack version: {contract.get('version')}"
        )
    expected = hashlib.sha256(_canonical_payload(pack)).hexdigest()
    if contract.get("sha256") != expected:
        raise ValueError("Source pack integrity check failed")
    state, reasons = assess_readiness(pack)
    if state != contract.get("readiness"):
        raise ValueError("Source pack readiness metadata is stale")
    if state == "blocked":
        raise ValueError("Source pack is blocked: " + "; ".join(reasons))
    if state == "limited" and not allow_limited:
        raise ValueError("Evidence-limited source pack is not allowed")
    return contract
