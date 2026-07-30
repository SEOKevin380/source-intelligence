"""Versioned publication contract between Source Intelligence and publishers."""

import base64
import copy
import hashlib
import html
import ipaddress
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from offering_taxonomy import (
    CANONICAL_PRODUCT_TYPES,
    normalize_product_type,
)
from trust_attestations import (
    sign_attestation,
    signing_identity,
    verify_attestation,
)

CONTRACT_NAME = "mbk.source-intelligence.publication-pack"
CONTRACT_VERSION = 3
LEGACY_CONTRACT_VERSIONS = frozenset({2})
READINESS_POLICY = "mandatory-assurance-v3"
MINIMUM_PUBLICATION_CLAIMS = 3
SOURCE_PACK_ATTESTATION_KIND = "source-pack-contract-v3"
ARTIFACT_CAPTURE_ATTESTATION_KIND = "artifact-capture"
REVIEW_HEAD_CHECKPOINT_VERSION = 1
REVIEW_HEAD_LEASE_SECONDS = 15 * 60
CORROBORATION_CAPTURE_ROUTES = frozenset({
    "regulatory_allowlisted",
    "peer_reviewed_allowlisted",
})

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
SELLER_CONTROLLED_SOURCE_CLASSES = frozenset({
    *SELLER_SOURCE_CLASSES,
    "client_submitted",
    "manual",
    "operator_submitted",
    "seller_submitted",
})

STRUCTURED_PRODUCT_CLAIM_TYPES = {
    "key_features": "feature",
    "specifications": "specification",
    "power_source": "specification",
    "warranty": "manufacturer_claim",
    "shipping_policy": "shipping_policy",
    "shipping": "shipping_policy",
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
    "media_contact_title",
    "support_email",
    "support_hours",
    "support_phone_us",
    "support_phone_international",
    "order_support_provider",
    "order_support_email",
    "order_support_url",
    "business_address",
    "return_address",
)


def is_publication_placeholder(value) -> bool:
    """Return True when a scalar is a template/masked value, not public data."""
    clean = " ".join(str(value or "").split()).strip()
    if not clean:
        return True
    folded = clean.casefold()
    semantic = re.sub(r"[\s.!?:;,/_-]+", " ", folded).strip()
    if semantic in {
        "unknown", "not established", "n/a", "none", "not provided",
        "unavailable", "tbd", "redacted", "not available",
        "no information available", "not listed", "not disclosed",
        "see website", "click here", "coming soon", "n a", "na",
        "tba", "tbc", "nil", "no data", "details pending",
        "information unavailable", "information pending",
        "pending", "see site", "see page", "visit website",
        "visit site", "check website", "check site", "learn more",
        "more information", "more info", "details unavailable",
    }:
        return True
    if re.search(r"\[[^\]]+\]", clean):
        return True
    return folded in {
        "email protected", "protected email", "example@example.com",
    }


def normalize_contact_information(value=None) -> dict:
    """Return the stable public contact schema used across intake and output."""
    raw = value if isinstance(value, dict) else {}
    aliases = {
        "media_contact": "media_contact_name",
        "contact_name": "media_contact_name",
        "contact_title": "media_contact_title",
        "media_title": "media_contact_title",
        "email": "support_email",
        "product_support_email": "support_email",
        "hours": "support_hours",
        "support_availability": "support_hours",
        "phone": "support_phone_us",
        "phone_us": "support_phone_us",
        "us_phone": "support_phone_us",
        "phone_intl": "support_phone_international",
        "international_phone": "support_phone_international",
        "order_support": "order_support_provider",
        "order_email": "order_support_email",
        "order_support_link": "order_support_url",
        "address": "business_address",
        "product_return_address": "return_address",
    }
    normalized = {}
    for key, value_item in raw.items():
        canonical = aliases.get(str(key).strip(), str(key).strip())
        if canonical not in CONTACT_INFORMATION_FIELDS:
            continue
        clean = " ".join(str(value_item or "").split()).strip()
        if not is_publication_placeholder(clean):
            normalized[canonical] = clean
    # A generic "Phone:" label is common in one-box operator intake. Treat a
    # visible non-NANP country code as authoritative instead of publishing an
    # Australian, UK, or other international number as "U.S. Support Phone."
    us_phone = normalized.get("support_phone_us", "")
    country_code = re.match(r"^\+\s*(\d{1,3})\b", us_phone)
    if country_code and country_code.group(1) != "1":
        normalized.pop("support_phone_us", None)
        normalized.setdefault("support_phone_international", us_phone)
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
    media_title = re.search(
        r"(?im)^\s*(?:media\s+contact\s+title|contact\s+title)\s*:\s*"
        r"(.+?)\s*$",
        notes,
    )
    if media_title:
        contact["media_contact_title"] = media_title.group(1)

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

    support_hours = re.search(
        r"(?im)^\s*((?:Available|Support\s+Hours)\b[^:\n]*"
        r"(?:[:\-]\s*)?.+?)\s*$",
        notes,
    )
    if support_hours:
        contact["support_hours"] = support_hours.group(1).strip()

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
        order_block = notes[order_label.start():]
        next_section = re.search(
            r"(?im)^\s*(?:Refund|Guarantee|Return|Product\s+Return|"
            r"Business\s+Address)\b",
            order_block[1:],
        )
        if next_section:
            order_block = order_block[:next_section.start() + 1]
        order_email = re.search(
            r"(?i)\bEmail\s*:\s*"
            r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
            order_block,
        )
        if order_email:
            contact["order_support_email"] = order_email.group(1)

    def address_block(*labels):
        joined = "|".join(re.escape(label) for label in labels)
        # When an address is supplied on the same line as its label, that line
        # is the complete value. Continuing into the next unlabeled line can
        # swallow ordinary production notes into the public contact block.
        inline = re.search(
            rf"(?im)^[ \t]*(?:{joined})[ \t]*:[ \t]*(\S[^\r\n]*)$",
            notes,
        )
        if inline:
            return " ".join(inline.group(1).split()).strip()
        match = re.search(
            rf"(?ims)^\s*(?:{joined})\s*:\s*(.*?)"
            r"(?=^\s*(?:Product\s+Support|Order\s+Support|Refund|"
            r"Guarantee|Support\s+Hours|Available|Product\s+Return|"
            r"Return\s+Address|Business\s+Address|Company\s+Address)\b|\Z)",
            notes,
        )
        if not match:
            return ""
        return " ".join(match.group(1).split()).strip()

    return_address = address_block("Product Return Address", "Return Address")
    if return_address:
        contact["return_address"] = return_address
    business_address = address_block("Business Address", "Company Address")
    if business_address:
        contact["business_address"] = business_address

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
    explicit = normalize_contact_information(explicit_contact)
    # Repair historical parser contamination without overruling a genuinely
    # different operator-entered address. If the stored explicit value is the
    # freshly inferred address plus trailing prose, the shorter line-bounded
    # value is the authoritative migration result.
    for field in ("business_address", "return_address"):
        inferred_value = inferred_contact.get(field, "")
        explicit_value = explicit.get(field, "")
        if (
            inferred_value
            and explicit_value
            and explicit_value.startswith(inferred_value)
            and explicit_value != inferred_value
        ):
            explicit.pop(field, None)
    merged = dict(inferred_contact)
    merged.update(explicit)
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


def _signature_payload(pack: dict) -> dict:
    """Return pack content covered by the Ed25519 trust signature."""
    payload = copy.deepcopy(pack)
    contract = payload.get("source_pack_contract", {})
    contract.pop("sha256", None)
    contract.pop("signature", None)
    return payload


def artifact_attestation_payload(
    artifact: dict,
    artifact_id: str = "",
) -> dict:
    """Return the immutable capture core covered by an artifact attestation."""
    record = artifact or {}
    resolved_id = str(
        artifact_id or record.get("artifact_id") or ""
    ).strip()
    if not resolved_id:
        raise ValueError("Artifact attestation requires an artifact_id")
    try:
        status_code = int(record.get("status_code"))
        content_length = int(record.get("content_length"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Artifact attestation requires integer status and content length"
        ) from exc
    if not isinstance(record.get("tls_verified"), bool):
        raise ValueError(
            "Artifact attestation requires a boolean tls_verified value"
        )
    if not isinstance(record.get("is_usable"), bool):
        raise ValueError(
            "Artifact attestation requires a boolean is_usable value"
        )
    return {
        "artifact_id": resolved_id,
        "artifact_type": str(
            record.get("artifact_type") or ""
        ).strip().casefold(),
        "source_url": str(record.get("source_url") or "").strip(),
        "final_url": str(record.get("final_url") or "").strip(),
        "source_class": str(
            record.get("source_class") or ""
        ).strip().casefold(),
        "source_relationship": str(
            record.get("source_relationship") or ""
        ).strip().casefold(),
        "captured_at": str(record.get("captured_at") or "").strip(),
        "status_code": status_code,
        "content_hash": str(
            record.get("content_hash") or ""
        ).strip().casefold(),
        "content_length": content_length,
        "tls_verified": record.get("tls_verified") is True,
        "is_usable": record.get("is_usable") is True,
        "offering_id": str(record.get("offering_id") or "").strip(),
        "job_id": str(record.get("job_id") or "").strip(),
        "acquisition_phase": str(
            record.get("acquisition_phase") or ""
        ).strip(),
        "capture_route": str(
            record.get("capture_route") or ""
        ).strip().casefold(),
        "corroboration_eligible": (
            record.get("corroboration_eligible") is True
        ),
    }


def attest_artifact_capture(
    artifact: dict,
    artifact_id: str = "",
) -> dict:
    """Create an attestation for a pipeline-owned captured artifact."""
    return sign_attestation(
        ARTIFACT_CAPTURE_ATTESTATION_KIND,
        artifact_attestation_payload(artifact, artifact_id),
    )


def verify_artifact_attestation(
    artifact: dict,
    artifact_id: str = "",
    trusted_public_key=None,
) -> bool:
    """Verify capture provenance under the pinned local trust identity."""
    try:
        payload = artifact_attestation_payload(artifact, artifact_id)
    except (TypeError, ValueError):
        return False
    return verify_attestation(
        ARTIFACT_CAPTURE_ATTESTATION_KIND,
        payload,
        (artifact or {}).get("capture_attestation"),
        trusted_public_key=trusted_public_key,
    )


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
    quarantined_fields = {
        str(field or "").strip().casefold()
        for field in product.get("quarantined_fields", []) or []
        if str(field or "").strip()
    }

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
                "fact_key": field,
            },
        })

    for field, claim_type in STRUCTURED_PRODUCT_CLAIM_TYPES.items():
        folded_field = field.casefold()
        if any(
            folded_field == quarantined
            or quarantined.startswith(folded_field + ".")
            for quarantined in quarantined_fields
        ):
            continue
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
                nested_field = f"{field}.{key}".casefold()
                if any(
                    nested_field == quarantined
                    or quarantined.startswith(nested_field + ".")
                    or nested_field.startswith(quarantined + ".")
                    for quarantined in quarantined_fields
                ):
                    continue
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
        "media_contact_title": "Media contact title",
        "support_email": "Product support email",
        "support_hours": "Product support hours",
        "support_phone_us": "United States support phone",
        "support_phone_international": "International support phone",
        "order_support_provider": "Order support provider",
        "order_support_email": "Order support email",
        "order_support_url": "Order support URL",
        "business_address": "Business address",
        "return_address": "Product return address",
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
            "review_status": "unreviewed",
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
            "review_status": "unreviewed",
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


def _claim_identity(claim: dict, fallback_type: str = "") -> tuple:
    """Return a stable identity shared by raw, publication, and audit ledgers."""
    claim_id = str((claim or {}).get("claim_id") or "").strip()
    if claim_id:
        return ("claim_id", claim_id)
    metadata = (claim or {}).get("metadata") or {}
    return (
        "claim",
        str(
            (claim or {}).get("claim_type") or fallback_type or ""
        ).strip().casefold(),
        " ".join(str(
            (claim or {}).get("text")
            or (claim or {}).get("claim_text")
            or ""
        ).split()).casefold(),
        str(
            (claim or {}).get("artifact_id")
            or (claim or {}).get("source_artifact_id")
            or ""
        ).strip(),
        str(metadata.get("fact_key") or "").strip().casefold(),
    )


def _merge_claim_ledgers(*ledgers) -> dict:
    """Union claim ledgers without allowing a secondary copy to elevate one."""
    merged = {}
    seen = set()
    for ledger in ledgers:
        if not isinstance(ledger, dict):
            continue
        # Preserve rows within each original ledger. Only suppress identities
        # already represented by a higher-precedence ledger.
        prior_seen = set(seen)
        for claim_type, items in ledger.items():
            if not isinstance(items, (list, tuple)):
                continue
            normalized_type = str(claim_type or "").strip()
            if not normalized_type:
                continue
            for claim in items:
                if not isinstance(claim, dict):
                    continue
                identity = _claim_identity(claim, normalized_type)
                if identity in prior_seen:
                    continue
                seen.add(identity)
                merged.setdefault(normalized_type, []).append(
                    copy.deepcopy(claim)
                )
    return merged


def _artifact_index(pack: dict) -> dict:
    """Return artifact records keyed by their immutable artifact IDs."""
    raw = pack.get("all_artifacts") or {}
    if isinstance(raw, dict):
        indexed = {}
        for artifact_id, artifact in raw.items():
            if not str(artifact_id):
                continue
            record = copy.deepcopy(artifact or {})
            record.setdefault("artifact_id", str(artifact_id))
            indexed[str(artifact_id)] = record
        return indexed
    return {
        str(item.get("artifact_id")): item
        for item in raw
        if isinstance(item, dict) and str(item.get("artifact_id") or "")
    }


def _seller_source_hosts(pack: dict) -> set:
    """Return saved seller-controlled hosts used for independence checks."""
    product = pack.get("product") or {}
    manifest = pack.get("intake_manifest") or {}
    hosts = set()
    for value in (
        product.get("official_url"),
        manifest.get("product_url"),
        manifest.get("label_source_url"),
        manifest.get("vsl_url"),
        manifest.get("affiliate_link"),
    ):
        host = _normalized_hostname(
            urlparse(str(value or "").strip()).hostname or ""
        )
        if host:
            hosts.add(host)
    for artifact in _artifact_index(pack).values():
        source_class = str(
            (artifact or {}).get("source_class") or ""
        ).strip().casefold()
        relationship = str(
            (artifact or {}).get("source_relationship") or ""
        ).strip().casefold()
        if (
            source_class not in SELLER_CONTROLLED_SOURCE_CLASSES
            and relationship not in {"first_party", "second_party"}
        ):
            continue
        for key in ("source_url", "final_url"):
            host = _normalized_hostname(
                urlparse(
                    str((artifact or {}).get(key) or "").strip()
                ).hostname or ""
            )
            if host:
                hosts.add(host)
    return hosts


def _normalized_hostname(value: str) -> str:
    """Return a lowercase ASCII hostname so IDNs compare to punycode."""
    clean = str(value or "").strip().strip(".").casefold()
    if clean.startswith("www."):
        clean = clean[4:]
    if not clean:
        return ""
    try:
        return clean.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _artifact_uses_seller_host(artifact: dict, seller_hosts: set) -> bool:
    """Reject third-party labels attached to a saved seller-controlled host."""
    common_two_label_suffixes = {
        "ac.uk", "co.in", "co.jp", "co.nz", "co.uk", "com.au",
        "com.br", "com.mx", "com.sg", "com.tr", "gov.uk", "net.au",
        "org.au", "org.uk",
    }

    def registrable_domain(host: str) -> str:
        clean = str(host or "").strip(".").casefold()
        if not clean:
            return ""
        try:
            ipaddress.ip_address(clean)
            return clean
        except ValueError:
            pass
        labels = clean.split(".")
        if len(labels) <= 2:
            return clean
        last_two = ".".join(labels[-2:])
        if last_two in common_two_label_suffixes and len(labels) >= 3:
            return ".".join(labels[-3:])
        return last_two

    for key in ("source_url", "final_url"):
        host = _normalized_hostname(
            urlparse(str((artifact or {}).get(key) or "").strip()).hostname
            or ""
        )
        if not host:
            continue
        if any(
            host == seller
            or host.endswith("." + seller)
            or seller.endswith("." + host)
            or (
                registrable_domain(host)
                and registrable_domain(host) == registrable_domain(seller)
            )
            for seller in seller_hosts
        ):
            return True
    return False


def _artifact_is_usable_for_corroboration(artifact: dict) -> bool:
    """Require a complete, content-bound successful capture record."""
    record = artifact or {}
    if record.get("is_usable") is not True:
        return False
    if str(record.get("error") or "").strip():
        return False
    if str(record.get("notes") or "").strip().casefold().startswith(
        "failed:"
    ):
        return False
    source_url = str(record.get("source_url") or "").strip()
    final_url = str(record.get("final_url") or source_url).strip()
    if not source_url or not final_url:
        return False
    captured_at = str(record.get("captured_at") or "").strip()
    if not captured_at:
        return False
    try:
        parsed_capture = datetime.fromisoformat(
            captured_at.replace("Z", "+00:00")
        )
        if parsed_capture.tzinfo is None:
            return False
    except (TypeError, ValueError):
        return False
    content_hash = str(record.get("content_hash") or "").strip().casefold()
    artifact_id = str(record.get("artifact_id") or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", artifact_id):
        return False
    try:
        if int(record.get("content_length")) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    status = record.get("status_code")
    if source_url.startswith(("http://", "https://")):
        if (
            not source_url.startswith("https://")
            or not final_url.startswith("https://")
            or record.get("tls_verified") is not True
        ):
            return False
        try:
            if not 200 <= int(status) < 400:
                return False
        except (TypeError, ValueError):
            return False
    try:
        if not verify_artifact_attestation(
            record,
            str(record.get("artifact_id") or ""),
        ):
            return False
    except (OSError, TypeError, ValueError):
        return False
    return True


def _artifact_is_validated_independent(artifact: dict) -> bool:
    """Require an allowlisted acquisition route for independent assurance."""
    record = artifact or {}
    if record.get("corroboration_eligible") is not True:
        return False
    route = str(record.get("capture_route") or "").strip().casefold()
    if route not in CORROBORATION_CAPTURE_ROUTES:
        return False
    source_class = str(
        record.get("source_class") or ""
    ).strip().casefold()
    expected_class = {
        "regulatory_allowlisted": "regulatory_database",
        "peer_reviewed_allowlisted": "peer_reviewed",
    }.get(route)
    if source_class != expected_class:
        return False
    final_url = str(
        record.get("final_url")
        or record.get("source_url")
        or ""
    ).strip()
    parsed = urlparse(final_url)
    host = _normalized_hostname(parsed.hostname or "")
    if parsed.scheme != "https" or not host:
        return False
    route_hosts = {
        "regulatory_allowlisted": (
            "api.fda.gov",
            "clinicaltrials.gov",
            "dsld.od.nih.gov",
            "fda.gov",
            "ftc.gov",
            "ods.od.nih.gov",
            "sec.gov",
        ),
        "peer_reviewed_allowlisted": (
            "eutils.ncbi.nlm.nih.gov",
            "ncbi.nlm.nih.gov",
            "pubmed.ncbi.nlm.nih.gov",
        ),
    }
    return any(
        host == authority or host.endswith("." + authority)
        for authority in route_hosts[route]
    )


def _effective_review_status(claim: dict) -> str:
    """Normalize legacy automation self-attestation conservatively."""
    from claims import is_human_reviewer

    status = str(
        (claim or {}).get("review_status") or "unreviewed"
    ).strip().casefold()
    reviewer = str((claim or {}).get("reviewed_by") or "").strip().casefold()
    metadata = (claim or {}).get("metadata") or {}
    legacy_auto_signature = bool(
        str((claim or {}).get("extraction_method") or "").strip().casefold()
        in {"reviewer_substitution", "automated_policy_substitution"}
        and str(metadata.get("substitution_note") or "").strip().casefold()
        == "automatic platform-safe substitution"
    )
    if status == "accepted" and (
        reviewer == "source-intelligence-automation"
        or (
            legacy_auto_signature
            and not is_human_reviewer((claim or {}).get("reviewed_by"))
        )
    ):
        return "auto_substituted"
    return status


def _public_review_head(head: dict) -> dict:
    """Return the signed, serialization-safe portion of a ledger head."""
    return {
        "claim_id": str((head or {}).get("claim_id") or ""),
        "offering_id": str((head or {}).get("offering_id") or ""),
        "current_status": str((head or {}).get("current_status") or ""),
        "current_claim_sha256": str(
            (head or {}).get("current_claim_sha256") or ""
        ),
        "latest_event_id": (head or {}).get("latest_event_id"),
        "latest_event_hash": str(
            (head or {}).get("latest_event_hash") or ""
        ),
        "latest_event_status": str(
            (head or {}).get("latest_event_status") or ""
        ),
        "event_valid": bool((head or {}).get("event_valid")),
        "current_matches_event": bool(
            (head or {}).get("current_matches_event")
        ),
        "head_valid": bool((head or {}).get("head_valid")),
        "authoritative_human_acceptance": bool(
            (head or {}).get("authoritative_human_acceptance")
        ),
    }


def _review_head_digest(checkpoint: dict) -> str:
    material = copy.deepcopy(checkpoint or {})
    material.pop("heads_sha256", None)
    return hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _canonical_review_timestamp(value: datetime) -> str:
    """Return the one accepted UTC representation for checkpoint times."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Review-head checkpoint time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_review_timestamp(value, field_name: str) -> datetime:
    """Parse a canonical, timezone-aware UTC checkpoint timestamp."""
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"Source pack review-head {field_name} is not a timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Source pack review-head {field_name} is invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or value != _canonical_review_timestamp(parsed)
    ):
        raise ValueError(
            f"Source pack review-head {field_name} is not canonical UTC"
        )
    return parsed


def _accepted_claim_ids(claims_by_type: dict) -> list:
    return sorted({
        str(claim.get("claim_id") or "")
        for items in (claims_by_type or {}).values()
        for claim in (items or [])
        if (
            isinstance(claim, dict)
            and str(claim.get("claim_id") or "")
            and _effective_review_status(claim) == "accepted"
        )
    })


def _offline_review_heads(
    pack: dict,
    claims_by_type: dict,
    review_inventory: list,
) -> dict:
    """Build explicitly non-current fixture heads from embedded proofs."""
    from claims import claim_snapshot_hash

    offering_id = str(pack.get("offering_id") or "").strip()
    inventory_by_id = {
        str(claim.get("claim_id") or ""): claim
        for claim in (review_inventory or [])
        if isinstance(claim, dict) and str(claim.get("claim_id") or "")
    }
    candidates = {}
    for items in (claims_by_type or {}).values():
        for claim in items or []:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id") or "")
            if claim_id:
                candidates[claim_id] = claim
    candidates.update(inventory_by_id)
    heads = {}
    for claim_id, claim in candidates.items():
        if _effective_review_status(claim) != "accepted":
            continue
        proof = ((claim.get("metadata") or {}).get(
            "review_attestation"
        ) or {})
        event = proof.get("event") or {}
        authoritative = _is_human_accepted_claim(claim, pack)
        heads[claim_id] = {
            "claim_id": claim_id,
            "offering_id": offering_id,
            "current_status": "accepted",
            "current_claim_sha256": claim_snapshot_hash(
                claim,
                offering_id=offering_id,
            ),
            "latest_event_id": proof.get("event_id"),
            "latest_event_hash": str(proof.get("event_hash") or ""),
            "latest_event_status": str(event.get("new_status") or ""),
            "event_valid": authoritative,
            "current_matches_event": authoritative,
            "head_valid": authoritative,
            "authoritative_human_acceptance": authoritative,
            "current_claim": copy.deepcopy(claim),
        }
    return heads


def _resolve_latest_review_heads(
    pack: dict,
    claims_by_type: dict,
    review_inventory: list,
    *,
    claims_ledger=None,
    allow_offline_review_fixtures: bool = False,
) -> tuple:
    """Resolve the latest DB dispositions needed by embedded acceptances."""
    offering_id = str(pack.get("offering_id") or "").strip()
    claim_ids = sorted({
        str(claim.get("claim_id") or "")
        for items in (claims_by_type or {}).values()
        for claim in (items or [])
        if isinstance(claim, dict) and str(claim.get("claim_id") or "")
    } | {
        str(claim.get("claim_id") or "")
        for claim in (review_inventory or [])
        if isinstance(claim, dict) and str(claim.get("claim_id") or "")
    })
    accepted_present = bool(
        _accepted_claim_ids(claims_by_type)
        or any(
            _effective_review_status(claim) == "accepted"
            for claim in (review_inventory or [])
            if isinstance(claim, dict)
        )
    )
    if not accepted_present:
        return "not_required", {}, ""
    if allow_offline_review_fixtures:
        return (
            "offline_fixture",
            _offline_review_heads(pack, claims_by_type, review_inventory),
            "",
        )
    try:
        if claims_ledger is None:
            from claims import ClaimsLedger
            claims_ledger = ClaimsLedger()
        heads = claims_ledger.get_latest_review_heads(
            offering_id,
            claim_ids=claim_ids,
        )
        return "claims_ledger", heads, ""
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        return "claims_ledger_unavailable", {}, str(exc)


def _reconcile_review_heads(
    claims_by_type: dict,
    review_inventory: list,
    heads: dict,
    *,
    mode: str,
) -> tuple:
    """Apply DB-current dispositions without allowing inventory elevation."""
    inventory_by_id = {
        str(claim.get("claim_id") or ""): claim
        for claim in (review_inventory or [])
        if isinstance(claim, dict) and str(claim.get("claim_id") or "")
    }

    def reconciled(claim):
        item = copy.deepcopy(claim or {})
        claim_id = str(item.get("claim_id") or "")
        inventory = inventory_by_id.get(claim_id)
        inventory_status = (
            _effective_review_status(inventory)
            if inventory else ""
        )
        # Caller-supplied inventory may lower authority but can never create it.
        if inventory_status in {"rejected", "conflicted"}:
            item["review_status"] = inventory_status
            item["reviewed_by"] = inventory.get("reviewed_by")
            item["reviewed_at"] = inventory.get("reviewed_at")
            item.setdefault("metadata", {}).pop(
                "review_attestation", None
            )
            return item
        head = heads.get(claim_id)
        if head:
            current = copy.deepcopy(head.get("current_claim") or {})
            current_status = str(head.get("current_status") or "")
            if current:
                item = current
            item["review_status"] = current_status or "needs_verification"
            if not head.get("authoritative_human_acceptance"):
                item.setdefault("metadata", {}).pop(
                    "review_attestation", None
                )
                if item["review_status"] == "accepted":
                    item["review_status"] = "needs_verification"
            return item
        if _effective_review_status(item) == "accepted" and mode != (
            "offline_fixture"
        ):
            item["review_status"] = "needs_verification"
            item.setdefault("metadata", {}).pop(
                "review_attestation", None
            )
        return item

    reconciled_claims = {
        claim_type: [
            reconciled(claim)
            for claim in (items or [])
            if isinstance(claim, dict)
        ]
        for claim_type, items in (claims_by_type or {}).items()
    }
    reconciled_inventory = []
    represented = set()
    for claim in review_inventory or []:
        if not isinstance(claim, dict):
            continue
        item = reconciled(claim)
        claim_id = str(item.get("claim_id") or "")
        if claim_id:
            represented.add(claim_id)
        reconciled_inventory.append(item)
    for claim_id, head in sorted((heads or {}).items()):
        if claim_id in represented:
            continue
        current = copy.deepcopy(head.get("current_claim") or {})
        if current:
            reconciled_inventory.append(current)
    return reconciled_claims, reconciled_inventory


def _build_review_head_checkpoint(
    pack: dict,
    publication_claims: dict,
    heads: dict,
    *,
    mode: str,
    resolution_error: str = "",
) -> dict:
    existing_contract = (pack.get("source_pack_contract") or {})
    existing_checkpoint = (
        existing_contract.get("review_head_checkpoint") or {}
    )
    prior_checked_at = str(
        existing_checkpoint.get("checked_at") or ""
    )
    if mode == "claims_ledger":
        checked = datetime.now(timezone.utc)
        checked_at = _canonical_review_timestamp(checked)
        # A fast reconciliation must still mint a new checkpoint value. The
        # prior value is used only to avoid equality, never as the lease clock.
        if checked_at == prior_checked_at:
            checked += timedelta(microseconds=1)
            checked_at = _canonical_review_timestamp(checked)
        valid_until = _canonical_review_timestamp(
            checked + timedelta(seconds=REVIEW_HEAD_LEASE_SECONDS)
        )
        lease_seconds = REVIEW_HEAD_LEASE_SECONDS
        freshness_basis = "live_claims_ledger_reconciliation"
    else:
        source_time = (
            existing_checkpoint.get("checked_at")
            or existing_contract.get("generated_at")
        )
        try:
            checked_at = _canonical_review_timestamp(
                _parse_review_timestamp(source_time, "checked_at")
            )
        except ValueError:
            checked_at = _canonical_review_timestamp(
                datetime.now(timezone.utc)
            )
        valid_until = None
        lease_seconds = None
        freshness_basis = {
            "not_required": "not_required_no_accepted_claims",
            "offline_fixture": "offline_fixture_noncurrent",
            "claims_ledger_unavailable": (
                "claims_ledger_unavailable_noncurrent"
            ),
        }.get(mode, "noncurrent")
    checkpoint = {
        "version": REVIEW_HEAD_CHECKPOINT_VERSION,
        "offering_id": str(pack.get("offering_id") or ""),
        "checked_at": checked_at,
        "valid_until": valid_until,
        "lease_seconds": lease_seconds,
        "freshness_basis": freshness_basis,
        "mode": mode,
        "accepted_claim_ids": _accepted_claim_ids(publication_claims),
        "heads": [
            _public_review_head(head)
            for _, head in sorted((heads or {}).items())
        ],
    }
    if resolution_error:
        checkpoint["resolution_error"] = resolution_error
    checkpoint["heads_sha256"] = _review_head_digest(checkpoint)
    return checkpoint


def _is_human_accepted_claim(claim: dict, pack: dict) -> bool:
    from claims import has_attested_human_acceptance

    normalized = copy.deepcopy(claim or {})
    normalized["review_status"] = _effective_review_status(claim)
    return has_attested_human_acceptance(
        normalized,
        offering_id=str((pack or {}).get("offering_id") or ""),
    )


def _normalized_assertion_key(claim: dict, fact_key: str) -> str:
    """Return a stable fact value so contradictory claims never corroborate."""
    metadata = claim.get("metadata") or {}
    required_tuples = {
        "ingredients_with_amounts": ("ingredient_name", "amount"),
        "serving_size": ("serving_size",),
        "servings_per_container": ("servings_per_container",),
        "pricing": ("package", "price"),
        "pricing_tiers": ("package", "price"),
        "allergens": ("allergen",),
    }
    required_fields = required_tuples.get(fact_key)
    values = []
    if required_fields:
        values = [
            " ".join(str(metadata.get(field) or "").split())
            .strip().casefold()
            for field in required_fields
        ]
    text = html.unescape(" ".join(str(claim.get("text") or "").split()))
    normalized_text = (
        text.casefold()
        .replace("≤", "<=")
        .replace("≥", ">=")
    )
    normalized_text = re.sub(
        r"[^\w.$%+\-<>=?!]+", " ", normalized_text
    ).strip()
    if values and all(values):
        if fact_key == "ingredients_with_amounts":
            form = " ".join(
                str(metadata.get("form") or "").split()
            ).strip().casefold()
            if form:
                values.append(form)
        values.append(normalized_text)
        return json.dumps(values, separators=(",", ":"), ensure_ascii=False)
    return normalized_text


def _claim_has_mandatory_fact_shape(claim: dict, fact_key: str) -> bool:
    """Reject labels/placeholders that do not contain the mandatory value."""
    metadata = claim.get("metadata") or {}
    text = " ".join(str(claim.get("text") or "").split()).strip()
    if is_publication_placeholder(text):
        return False
    normalized_text = re.sub(r"[^a-z0-9]+", "", text.casefold())
    normalized_label = re.sub(
        r"[^a-z0-9]+",
        "",
        str(fact_key or "").replace("_", " ").casefold(),
    )
    if not normalized_text:
        return False
    # A section heading names the field but supplies no fact. Apostrophes,
    # punctuation, and a trailing colon do not turn it into evidence.
    if normalized_text == normalized_label:
        return False
    label_only_variants = {
        "key_features": {
            "featuresoverview", "featurelist", "mainfeatures",
            "producthighlights", "specifications", "overview",
            "productoverview", "whatitdoes",
        },
        "services_offered": {
            "ourservices", "servicesoverview", "servicelist",
        },
        "whats_included": {
            "whatsinside", "packagecontents", "includeditems",
        },
        "active_ingredients": {
            "activeingredient", "activeingredientslist",
        },
        "ingredients": {"ingredientslist"},
        "allergens": {"allergeninformation"},
        "lab_results": {"certificateofanalysis", "coa"},
        "program_structure": {"howitworks", "programoverview"},
        "item_description": {"aboutthisitem"},
    }
    if normalized_text in label_only_variants.get(fact_key, set()):
        return False
    text_words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    fact_words = set(re.findall(
        r"[a-z0-9]+",
        str(fact_key or "").replace("_", " ").casefold(),
    ))
    generic_label_words = {
        "active", "content", "contents", "description", "details",
        "feature", "features", "included", "includes", "info",
        "information", "key", "product", "service", "whats",
    }
    if (
        text_words
        and len(text_words) <= 4
        and text_words <= (fact_words | generic_label_words)
    ):
        return False

    amount_pattern = re.compile(
        r"\b\d+(?:\.\d+)?\s*(?:mcg|µg|ug|mg|g|kg|ml|l|iu|%|ppm)\b",
        re.I,
    )
    if fact_key == "ingredients_with_amounts":
        ingredient = str(
            metadata.get("ingredient_name")
            or metadata.get("ingredient")
            or ""
        ).strip()
        amount = str(
            metadata.get("amount") or metadata.get("dosage") or ""
        ).strip()
        return bool(
            (ingredient and amount_pattern.search(amount))
            or amount_pattern.search(text)
        )
    if fact_key == "serving_size":
        value = str(metadata.get("serving_size") or text).strip()
        return bool(re.search(
            r"\b(?:\d+(?:[./]\d+)?|one|two|three|four|five)\s*"
            r"(?:capsules?|tablets?|gummies?|softgels?|scoops?|"
            r"packets?|drops?|teaspoons?|tablespoons?|ml|g)\b",
            value,
            re.I,
        ))
    if fact_key in {
        "purity_percentage", "thc_content", "cannabinoid_profile",
    }:
        return bool(re.search(r"\b\d+(?:\.\d+)?\s*(?:%|mg|mcg|µg)\b", text))
    if fact_key == "pricing_tiers":
        return bool(re.search(r"(?:[$€£]\s*\d|\b\d+(?:\.\d+)?\s*USD\b)", text, re.I))
    if fact_key == "peptide_sequence":
        return bool(
            len(re.findall(r"[A-Za-z]{1,4}", text)) >= 3
            and ("-" in text or len(text.split()) >= 3)
        )
    if fact_key == "research_use_only_disclaimer":
        return bool(re.search(r"\bresearch use only\b", text, re.I))
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    generic_tokens = {
        "a", "an", "and", "about", "content", "contents", "description",
        "details", "device", "feature", "features", "for", "included",
        "includes", "information", "item", "items", "key", "of", "our",
        "overview", "product", "program", "service", "services", "the",
        "this", "what", "whats", "with",
    }
    value_tokens = [
        token for token in tokens
        if token not in generic_tokens
    ]
    if fact_key in {"active_ingredients", "ingredients", "allergens"}:
        # A named ingredient/allergen can legitimately be a single word, but a
        # field label or product category cannot.
        return bool(value_tokens)
    if fact_key in {"prescriber_credentials", "credentials"}:
        return bool(re.search(
            r"\b(?:md|do|np|pa|rn|rd|rdn|pharmd|phd|licensed|certified|"
            r"board[- ]certified|license(?:d| number)?|registration)\b",
            text,
            re.I,
        ))
    if fact_key == "regulatory_registrations":
        return bool(re.search(
            r"\b(?:sec|finra|fca|asic|registered|registration|license|"
            r"licensed|crd|firm id|registration number)\b",
            text,
            re.I,
        ))
    if fact_key == "lab_results":
        return bool(
            len(value_tokens) >= 2
            and re.search(
                r"\b(?:coa|certificate|tested|test|pass(?:ed)?|result|"
                r"analysis|contaminant|potency|pesticide|microbial)\b",
                text,
                re.I,
            )
        )
    descriptive_facts = {
        "key_features",
        "services_offered",
        "whats_included",
        "service_type",
        "service_description",
        "program_structure",
        "included_items",
        "product_description",
        "how_it_works",
        "item_description",
    }
    if fact_key in descriptive_facts:
        # These fields do not have a universal numeric schema. Require at least
        # two value-bearing tokens (or a concrete number plus one value token)
        # so headings and category nouns such as "Device" cannot corroborate.
        return bool(
            len(value_tokens) >= 2
            or (
                value_tokens
                and any(token.isdigit() for token in tokens)
            )
        )
    # A newly introduced mandatory fact has no machine-checkable shape until a
    # typed validator is added. Human acceptance remains available.
    return False


def _mandatory_facts_for_pack(pack: dict) -> list:
    """Derive the canonical mandatory facts for the current contract version.

    Callers may reseal a pack after claim review, so an incoming v3 contract
    can legitimately have a stale hash. Its policy fields are therefore never
    trusted as input. Changing this mapping requires a contract-version bump.
    """
    product_type = normalize_product_type(
        (pack.get("product") or {}).get("product_type", "")
    )
    if not product_type or product_type not in CANONICAL_PRODUCT_TYPES:
        return []
    from entities import OfferingType
    from intelligence_packs import get_mandatory_facts

    try:
        offering_type = OfferingType(product_type)
    except ValueError:
        return []
    return list(get_mandatory_facts(offering_type))


def _mandatory_fact_assurance(
    pack: dict,
    mandatory_facts: list,
) -> dict:
    """Compute the human-or-independent assurance state for each fact."""
    from claims import verify_claim_evidence_attestation

    artifacts = _artifact_index(pack)
    pack_offering_id = str(pack.get("offering_id") or "").strip()
    seller_hosts = _seller_source_hosts(pack)
    claims = [
        claim
        for items in (pack.get("publication_claims") or {}).values()
        for claim in (items or [])
        if isinstance(claim, dict)
    ]
    required = pack.get("required_facts") or {}
    declared_missing = {
        str(item) for item in (required.get("missing") or []) if str(item)
    }
    assurance = {}
    for fact_key in mandatory_facts:
        candidates = [
            claim for claim in claims
            if str((claim.get("metadata") or {}).get("fact_key") or "")
            == fact_key
            and bool(str(claim.get("text") or "").strip())
            and _claim_has_mandatory_fact_shape(claim, fact_key)
            and _effective_review_status(claim)
            not in {"rejected", "conflicted"}
        ]
        human = [
            claim
            for claim in candidates
            if _is_human_accepted_claim(claim, pack)
        ]
        state = "unverified"
        selected = human
        if human:
            state = "human_accepted"
        else:
            by_assertion = {}
            for claim in candidates:
                if _effective_review_status(claim) == "auto_substituted":
                    continue
                metadata = claim.get("metadata") or {}
                if not (
                    metadata.get("excerpt_is_literal") is True
                    or metadata.get("artifact_transcription_verified") is True
                ):
                    continue
                artifact_id = str(claim.get("artifact_id") or "")
                artifact = artifacts.get(artifact_id)
                if (
                    not artifact
                    or not _artifact_is_usable_for_corroboration(artifact)
                    or not verify_claim_evidence_attestation(
                        claim,
                        artifact,
                        pack_offering_id=pack_offering_id,
                    )
                ):
                    continue
                # The immutable artifact controls source class. A copied claim
                # label cannot promote anonymous evidence to independence.
                source_class = str(
                    artifact.get("source_class") or ""
                ).strip().casefold()
                source_relationship = str(
                    artifact.get("source_relationship") or ""
                ).strip().casefold()
                if not source_class or not source_relationship:
                    continue
                assertion_key = _normalized_assertion_key(claim, fact_key)
                if not assertion_key:
                    continue
                by_assertion.setdefault(assertion_key, []).append(
                    (
                        claim,
                        artifact_id,
                        source_class,
                        source_relationship,
                    )
                )
            corroborated = []
            for grouped in by_assertion.values():
                artifact_ids = {item[1] for item in grouped}
                source_classes = {item[2] for item in grouped}
                has_independent = any(
                    item[3] == "third_party"
                    and item[2] not in SELLER_CONTROLLED_SOURCE_CLASSES
                    and _artifact_is_validated_independent(
                        artifacts.get(item[1]) or {}
                    )
                    and not _artifact_uses_seller_host(
                        artifacts.get(item[1]) or {},
                        seller_hosts,
                    )
                    for item in grouped
                )
                if (
                    len(artifact_ids) >= 2
                    and len(source_classes) >= 2
                    and has_independent
                ):
                    corroborated = [item[0] for item in grouped]
                    break
            if corroborated:
                state = "corroborated"
                selected = corroborated
            elif not candidates or fact_key in declared_missing:
                state = "missing"
                selected = candidates
            else:
                selected = candidates
        assurance[fact_key] = {
            "state": state,
            "claim_ids": sorted({
                str(claim.get("claim_id") or "")
                for claim in selected
                if str(claim.get("claim_id") or "")
            }),
            "artifact_ids": sorted({
                str(claim.get("artifact_id") or "")
                for claim in selected
                if str(claim.get("artifact_id") or "")
            }),
            "source_classes": sorted({
                str(
                    (artifacts.get(str(claim.get("artifact_id") or "")) or {})
                    .get("source_class") or ""
                ).strip().casefold()
                for claim in selected
                if str(
                    (artifacts.get(str(claim.get("artifact_id") or "")) or {})
                    .get("source_class") or ""
                ).strip()
            }),
            "source_relationships": sorted({
                str(
                    (artifacts.get(str(claim.get("artifact_id") or "")) or {})
                    .get("source_relationship") or ""
                ).strip().casefold()
                for claim in selected
                if str(
                    (artifacts.get(str(claim.get("artifact_id") or "")) or {})
                    .get("source_relationship") or ""
                ).strip()
            }),
        }
    return assurance


def _review_state_counts(
    source_claims: dict,
    excluded_claims: list,
    review_inventory=None,
    pack=None,
) -> dict:
    """Return recomputable counts across persisted and synthesized claims.

    A DB-backed review inventory is authoritative for rows it contains, but it
    is not necessarily exhaustive: sealing can synthesize structured/intake
    claims after the inventory snapshot.  Merge both views by claim identity,
    then include quarantined rows absent from either view.
    """
    counts = {
        "accepted": 0,
        "auto_substituted": 0,
        "unreviewed": 0,
        "needs_verification": 0,
        "conflicted": 0,
        "excluded": len(excluded_claims or []),
    }

    def fingerprint(claim, fallback_type=""):
        metadata = (claim or {}).get("metadata") or {}
        return (
            str(
                (claim or {}).get("claim_type") or fallback_type or ""
            ).strip().casefold(),
            " ".join(str(
                (claim or {}).get("text")
                or (claim or {}).get("claim_text")
                or ""
            ).split()).casefold(),
            str(
                (claim or {}).get("artifact_id")
                or (claim or {}).get("source_artifact_id")
                or ""
            ).strip(),
            str(metadata.get("fact_key") or "").strip().casefold(),
        )

    inventory = (
        review_inventory
        if isinstance(review_inventory, list)
        else []
    )
    inventory_ids = {
        str(claim.get("claim_id") or "")
        for claim in inventory
        if isinstance(claim, dict) and str(claim.get("claim_id") or "")
    }
    inventory_fingerprints = {
        fingerprint(claim)
        for claim in inventory
        if isinstance(claim, dict)
    }
    merged = [
        claim for claim in inventory if isinstance(claim, dict)
    ]
    represented_ids = set(inventory_ids)
    represented_fingerprints = set(inventory_fingerprints)

    for claim_type, items in (source_claims or {}).items():
        for claim in items or []:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id") or "")
            claim_fingerprint = fingerprint(claim, claim_type)
            if (
                (claim_id and claim_id in represented_ids)
                or claim_fingerprint in represented_fingerprints
            ):
                continue
            merged.append(claim)
            if claim_id:
                represented_ids.add(claim_id)
            represented_fingerprints.add(claim_fingerprint)

    for claim in excluded_claims or []:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_id") or "")
        claim_fingerprint = fingerprint(claim)
        if (
            (claim_id and claim_id in represented_ids)
            or claim_fingerprint in represented_fingerprints
        ):
            continue
        merged.append(claim)
        if claim_id:
            represented_ids.add(claim_id)
        represented_fingerprints.add(claim_fingerprint)

    for claim in merged:
        status = _effective_review_status(claim)
        if (
            status == "accepted"
            and not _is_human_accepted_claim(claim, pack or {})
        ):
            status = "needs_verification"
        if status in counts and status != "excluded":
            counts[status] += 1
    return counts


def requires_source_verification(pack: dict) -> bool:
    """Return whether unattended drafting must wait for claim-level review."""
    contract = (pack or {}).get("source_pack_contract") or {}
    return bool(contract.get("unverified_mandatory_facts") or [])


def assess_readiness(
    full_data: dict,
    *,
    mandatory_facts=None,
) -> tuple:
    """Return (state, reasons). Limited packs remain publishable."""
    product = full_data.get("product", {}) or {}
    reasons = []
    if not str(full_data.get("offering_id") or "").strip():
        reasons.append("missing_offering_identity")
    if not str(product.get("product_name", "")).strip():
        reasons.append("missing_product_identity")
    if not str(product.get("official_url", "")).strip():
        reasons.append("missing_official_url")
    declared_product_type = product.get("product_type", "")
    product_type = normalize_product_type(declared_product_type)
    if not product_type:
        reasons.append("missing_product_type")
    elif product_type not in CANONICAL_PRODUCT_TYPES:
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
    limited_reasons = []
    if missing:
        limited_reasons.append(
            "missing_required_facts:" + ",".join(missing)
        )
    mandatory = (
        list(mandatory_facts)
        if mandatory_facts is not None
        else _mandatory_facts_for_pack(full_data)
    )
    assurance = _mandatory_fact_assurance(full_data, mandatory)
    unverified = [
        fact_key for fact_key in mandatory
        if assurance.get(fact_key, {}).get("state")
        not in {"human_accepted", "corroborated"}
    ]
    if unverified:
        limited_reasons.append(
            "unverified_mandatory_facts:" + ",".join(unverified)
        )
    if limited_reasons:
        return "limited", limited_reasons
    return "complete", []


def _validate_contract_integrity(pack: dict, allowed_versions: set) -> dict:
    contract = (pack or {}).get("source_pack_contract") or {}
    if contract.get("name") != CONTRACT_NAME:
        raise ValueError("Not a Source Intelligence publication pack")
    version = contract.get("version")
    if type(version) is not int or version not in allowed_versions:
        raise ValueError(
            f"Unsupported source-pack version: {version}"
        )
    expected = hashlib.sha256(_canonical_payload(pack)).hexdigest()
    if contract.get("sha256") != expected:
        raise ValueError("Source pack integrity check failed")
    if contract.get("version") == CONTRACT_VERSION:
        identity = contract.get("trust_identity") or {}
        signature = contract.get("signature") or {}
        try:
            public_key = base64.b64decode(
                str(identity.get("public_key") or "").encode("ascii"),
                validate=True,
            )
        except (TypeError, ValueError):
            public_key = b""
        expected_key_id = (
            "sha256:" + hashlib.sha256(public_key).hexdigest()
            if len(public_key) == 32
            else ""
        )
        if (
            identity.get("algorithm") != "Ed25519"
            or identity.get("public_key_encoding")
            != "base64-raw-ed25519"
            or identity.get("key_id") != expected_key_id
            or signature.get("key_id") != expected_key_id
            or not verify_attestation(
                SOURCE_PACK_ATTESTATION_KIND,
                _signature_payload(pack),
                signature,
            )
        ):
            raise ValueError("Source pack trust signature check failed")
    return contract


def verify_source_pack_signature(pack: dict, trusted_public_key) -> bool:
    """Verify a v3 pack using an independently obtained public key.

    This is the external-audit path.  Callers must pin ``trusted_public_key``
    from outside the pack; passing the pack's own public-key field without
    comparing its fingerprint to an independent record proves integrity only,
    not publisher identity.
    """
    try:
        contract = (pack or {}).get("source_pack_contract") or {}
        if (
            contract.get("name") != CONTRACT_NAME
            or type(contract.get("version")) is not int
            or contract.get("version") != CONTRACT_VERSION
            or contract.get("sha256")
            != hashlib.sha256(_canonical_payload(pack)).hexdigest()
        ):
            return False
        identity = contract.get("trust_identity") or {}
        signature = contract.get("signature") or {}
        try:
            embedded_public_key = base64.b64decode(
                str(identity.get("public_key") or "").encode("ascii"),
                validate=True,
            )
        except (TypeError, ValueError):
            return False
        if (
            len(embedded_public_key) != 32
            or identity.get("public_key_encoding")
            != "base64-raw-ed25519"
            or identity.get("key_id")
            != "sha256:" + hashlib.sha256(
                embedded_public_key
            ).hexdigest()
        ):
            return False
        return (
            identity.get("key_id") == signature.get("key_id")
            and verify_attestation(
                SOURCE_PACK_ATTESTATION_KIND,
                _signature_payload(pack),
                signature,
                trusted_public_key=trusted_public_key,
            )
        )
    except (AttributeError, TypeError, ValueError):
        return False


def seal_source_pack(
    full_data: dict,
    *,
    claims_ledger=None,
    allow_offline_review_fixtures: bool = False,
) -> dict:
    """Return a sealed pack reconciled to the latest durable review heads.

    ``allow_offline_review_fixtures`` exists only for isolated tests that mint
    signed claim fixtures without a ClaimsLedger. Packs created that way are
    marked and are rejected by production validation and currentness checks.
    """
    input_contract = (full_data or {}).get("source_pack_contract") or {}
    legacy_transition = None
    if (
        input_contract.get("name") == CONTRACT_NAME
        and input_contract.get("version") in LEGACY_CONTRACT_VERSIONS
    ):
        _validate_contract_integrity(
            full_data, set(LEGACY_CONTRACT_VERSIONS)
        )
        legacy_transition = {
            "event_type": "source_pack_contract_migrated",
            "migration": "mandatory-assurance-v3-reassessment",
            "from_version": input_contract.get("version"),
            "to_version": CONTRACT_VERSION,
            "prior_sha256": input_contract.get("sha256", ""),
            "prior_readiness": input_contract.get("readiness", ""),
            "prior_readiness_reasons": list(
                input_contract.get("readiness_reasons") or []
            ),
        }
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
    if not str(pack.get("offering_id") or "").strip():
        try:
            from entities import Offering
            pack["offering_id"] = Offering.from_legacy_product_data(
                product
            ).offering_id
        except (AttributeError, TypeError, ValueError):
            pack["offering_id"] = ""
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
    prior_excluded_claims = [
        copy.deepcopy(item)
        for item in (pack.get("excluded_publication_claims") or [])
        if isinstance(item, dict)
    ]
    excluded_claims = []
    review_inventory = [
        item for item in (pack.get("claim_review_inventory") or [])
        if isinstance(item, dict)
    ]
    source_claims = _merge_claim_ledgers(
        pack.get("claims_by_type"),
        pack.get("publication_claims"),
    )
    review_head_mode, review_heads, review_head_error = (
        _resolve_latest_review_heads(
            pack,
            source_claims,
            review_inventory,
            claims_ledger=claims_ledger,
            allow_offline_review_fixtures=(
                allow_offline_review_fixtures
            ),
        )
    )
    source_claims, review_inventory = _reconcile_review_heads(
        source_claims,
        review_inventory,
        review_heads,
        mode=review_head_mode,
    )
    pack["claim_review_inventory"] = review_inventory
    active_claim_keys = {
        _claim_identity(claim, claim_type)
        for claim_type, items in source_claims.items()
        for claim in items
    }
    excluded_keys = set()

    def add_excluded_claim(item):
        key = _claim_identity(item)
        if key in excluded_keys:
            return
        excluded_keys.add(key)
        excluded_claims.append(item)

    # The publication ledger intentionally omits rejected/conflicted source
    # rows. Preserve their disposition in the sealed audit inventory so the
    # six review-state counters remain externally recomputable.
    for claim in review_inventory:
        status = _effective_review_status(claim)
        if status not in {"rejected", "conflicted"}:
            continue
        add_excluded_claim({
            "claim_id": claim.get("claim_id", ""),
            "claim_type": claim.get("claim_type", ""),
            "text": claim.get("text", ""),
            "artifact_id": claim.get("artifact_id", ""),
            "metadata": copy.deepcopy(claim.get("metadata") or {}),
            "review_status": status,
            "reason": (
                "conflicted_source_claim"
                if status == "conflicted"
                else "rejected_source_claim"
            ),
        })
    # Preserve audit-only exclusions across a reseal after the authoritative
    # review inventory has had first claim on each identity. Active copies are
    # re-evaluated below, so stale exclusions cannot override current policy.
    for claim in prior_excluded_claims:
        if _claim_identity(claim) not in active_claim_keys:
            add_excluded_claim(claim)
    product_type = str(
        (pack.get("product") or {}).get("product_type", "")
    ).strip().casefold()
    artifacts = _artifact_index(pack)
    # Persisted reports can retain vetted claims only in the publication
    # ledger. Union both ledgers so a non-empty raw ledger cannot discard
    # publication-only facts during migration or resealing.
    source_claims = _merge_contextual_seller_claims(source_claims, pack)
    source_claims = _merge_structured_claims(source_claims, pack)
    source_claims = _merge_intake_claims(source_claims, pack)
    for claim_type, items in source_claims.items():
        for claim in items or []:
            status = _effective_review_status(claim)
            if (
                status == "accepted"
                and not _is_human_accepted_claim(claim, pack)
            ):
                status = "needs_verification"
            metadata = claim.get("metadata") or {}
            literal = metadata.get("excerpt_is_literal", True)
            has_artifact = bool(claim.get("artifact_id"))
            artifact = artifacts.get(str(claim.get("artifact_id") or ""), {})
            source_class = str(
                artifact.get("source_class")
                or claim.get("source_class")
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
                and status in {
                    "accepted",
                    "auto_substituted",
                    "unreviewed",
                    "needs_verification",
                }
                and not compliance_blocked
            )
            source_attribution_required = bool(
                (
                    (
                        status in {"unreviewed", "needs_verification"}
                        and literal
                    )
                    or status == "auto_substituted"
                )
                and has_artifact
                and not seller_attribution_required
                and (
                    status in {"unreviewed", "auto_substituted"}
                    or metadata.get("contextual_seller_heading") is True
                )
                and not compliance_blocked
            )
            safe = not compliance_blocked and (
                bool(normalized_claim_text)
                and (
                status == "accepted"
                or (status == "unreviewed" and literal and has_artifact)
                or seller_attribution_required
                or source_attribution_required
                )
            )
            if safe:
                publication_claim = copy.deepcopy(claim)
                publication_claim["review_status"] = status
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
                add_excluded_claim({
                    "claim_id": claim.get("claim_id", ""),
                    "claim_type": claim_type,
                    "text": claim.get("text", ""),
                    "artifact_id": claim.get("artifact_id", ""),
                    "metadata": copy.deepcopy(metadata),
                    "review_status": status,
                    "reason": (
                        "blocked_by_compliance"
                        if compliance_blocked
                        else (
                            "auto_substitution_missing_source_provenance"
                            if status == "auto_substituted"
                            else "not_accepted_or_literal_artifact_backed"
                        )
                    ),
                })
    pack["publication_claims"] = publication_claims
    pack["excluded_publication_claims"] = excluded_claims
    pack["publication_claim_summary"] = {
        "raw_claim_count": (
            len(review_inventory)
            if review_inventory
            else sum(len(items or []) for items in source_claims.values())
        ),
        "publication_claim_count": sum(
            len(items or []) for items in publication_claims.values()
        ),
        "excluded_claim_count": len(excluded_claims),
    }
    existing = pack.get("source_pack_contract", {}) or {}
    mandatory_facts = _mandatory_facts_for_pack(pack)
    assurance = _mandatory_fact_assurance(pack, mandatory_facts)
    state, reasons = assess_readiness(
        pack, mandatory_facts=mandatory_facts
    )
    unverified_mandatory = [
        fact_key for fact_key in mandatory_facts
        if assurance.get(fact_key, {}).get("state")
        not in {"human_accepted", "corroborated"}
    ]
    review_counts = _review_state_counts(
        source_claims,
        excluded_claims,
        review_inventory,
        pack,
    )
    review_head_checkpoint = _build_review_head_checkpoint(
        pack,
        publication_claims,
        review_heads,
        mode=review_head_mode,
        resolution_error=review_head_error,
    )
    if legacy_transition:
        legacy_transition.update({
            "new_readiness": state,
            "new_readiness_reasons": reasons,
        })
        migrations = list(pack.get("source_pack_contract_migrations") or [])
        if not any(
            item.get("prior_sha256") == legacy_transition["prior_sha256"]
            and item.get("to_version") == CONTRACT_VERSION
            for item in migrations
            if isinstance(item, dict)
        ):
            migrations.append(legacy_transition)
        pack["source_pack_contract_migrations"] = migrations
    pack["source_pack_contract"] = {
        "name": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "generated_at": existing.get("generated_at")
        or datetime.now(timezone.utc).isoformat(),
        "readiness": state,
        "readiness_reasons": reasons,
        "readiness_policy": READINESS_POLICY,
        "mandatory_facts": mandatory_facts,
        "mandatory_fact_assurance": assurance,
        "unverified_mandatory_facts": unverified_mandatory,
        "review_state_counts": review_counts,
        "review_head_checkpoint": review_head_checkpoint,
        "source_of_truth": "source_intelligence",
        "generation_system": "MBK Master Content Generation System v3.8",
        "trust_identity": signing_identity(),
    }
    pack["source_pack_contract"]["signature"] = sign_attestation(
        SOURCE_PACK_ATTESTATION_KIND,
        _signature_payload(pack),
    )
    pack["source_pack_contract"]["sha256"] = hashlib.sha256(
        _canonical_payload(pack)
    ).hexdigest()
    return pack


def _validate_review_head_checkpoint(
    pack: dict,
    *,
    allow_offline_review_fixtures: bool = False,
) -> dict:
    contract = (pack or {}).get("source_pack_contract") or {}
    checkpoint = contract.get("review_head_checkpoint")
    if not isinstance(checkpoint, dict):
        raise ValueError("Source pack has no review-head checkpoint")
    if (
        type(checkpoint.get("version")) is not int
        or checkpoint.get("version") != REVIEW_HEAD_CHECKPOINT_VERSION
    ):
        raise ValueError("Source pack review-head checkpoint version is invalid")
    if checkpoint.get("offering_id") != str(pack.get("offering_id") or ""):
        raise ValueError("Source pack review-head offering identity is stale")
    if checkpoint.get("heads_sha256") != _review_head_digest(checkpoint):
        raise ValueError("Source pack review-head checkpoint digest is invalid")
    mode = str(checkpoint.get("mode") or "")
    if mode not in {
        "claims_ledger",
        "claims_ledger_unavailable",
        "not_required",
        "offline_fixture",
    }:
        raise ValueError("Source pack review-head checkpoint mode is invalid")
    if mode == "offline_fixture" and not allow_offline_review_fixtures:
        raise ValueError(
            "Offline review fixture checkpoints are not valid in production"
        )
    checked_at = _parse_review_timestamp(
        checkpoint.get("checked_at"), "checked_at"
    )
    expected_freshness_basis = {
        "claims_ledger": "live_claims_ledger_reconciliation",
        "claims_ledger_unavailable": (
            "claims_ledger_unavailable_noncurrent"
        ),
        "not_required": "not_required_no_accepted_claims",
        "offline_fixture": "offline_fixture_noncurrent",
    }[mode]
    if checkpoint.get("freshness_basis") != expected_freshness_basis:
        raise ValueError(
            "Source pack review-head freshness basis is invalid"
        )
    if mode == "claims_ledger":
        valid_until = _parse_review_timestamp(
            checkpoint.get("valid_until"), "valid_until"
        )
        duration = valid_until - checked_at
        if (
            duration <= timedelta(0)
            or duration > timedelta(seconds=REVIEW_HEAD_LEASE_SECONDS)
        ):
            raise ValueError(
                "Source pack review-head freshness lease is invalid"
            )
        if (
            checkpoint.get("lease_seconds")
            != REVIEW_HEAD_LEASE_SECONDS
        ):
            raise ValueError(
                "Source pack review-head lease duration is invalid"
            )
    elif (
        checkpoint.get("valid_until") is not None
        or checkpoint.get("lease_seconds") is not None
    ):
        raise ValueError(
            "Non-current review-head checkpoints must be non-expiring"
        )
    heads = checkpoint.get("heads")
    if not isinstance(heads, list) or not all(
        isinstance(head, dict) for head in heads
    ):
        raise ValueError("Source pack review-head entries are invalid")
    head_ids = [str(head.get("claim_id") or "") for head in heads]
    if (
        any(not claim_id for claim_id in head_ids)
        or len(head_ids) != len(set(head_ids))
        or head_ids != sorted(head_ids)
    ):
        raise ValueError("Source pack review-head claim IDs are invalid")
    accepted_ids = _accepted_claim_ids(
        pack.get("publication_claims") or {}
    )
    if accepted_ids != list(checkpoint.get("accepted_claim_ids") or []):
        raise ValueError("Source pack accepted-claim checkpoint is stale")
    by_id = {
        str(head.get("claim_id") or ""): head
        for head in heads
    }
    if accepted_ids and mode not in {"claims_ledger", "offline_fixture"}:
        raise ValueError(
            "Accepted claims have no authoritative review-head checkpoint"
        )
    for claim_id in accepted_ids:
        head = by_id.get(claim_id) or {}
        if (
            head.get("current_status") != "accepted"
            or not head.get("head_valid")
            or not head.get("authoritative_human_acceptance")
        ):
            raise ValueError(
                f"Accepted claim {claim_id} has no current review head"
            )
    return checkpoint


def recheck_review_head_checkpoint(
    pack: dict,
    *,
    claims_ledger=None,
    allow_offline_review_fixtures: bool = False,
) -> dict:
    """Recheck a sealed acceptance checkpoint against current ledger heads."""
    try:
        _validate_contract_integrity(pack, {CONTRACT_VERSION})
        checkpoint = _validate_review_head_checkpoint(
            pack,
            allow_offline_review_fixtures=(
                allow_offline_review_fixtures
            ),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        return {
            "valid": False,
            "mode": "",
            "currentness": "invalid_checkpoint",
            "checkpoint_expired": None,
            "stale_claim_ids": [],
            "reasons": [str(exc)],
        }
    accepted_ids = list(checkpoint.get("accepted_claim_ids") or [])
    mode = str(checkpoint.get("mode") or "")
    rechecked_at = _canonical_review_timestamp(
        datetime.now(timezone.utc)
    )
    checkpoint_expired = False
    if mode == "claims_ledger":
        checkpoint_expired = (
            datetime.fromisoformat(rechecked_at)
            >= _parse_review_timestamp(
                checkpoint.get("valid_until"), "valid_until"
            )
        )
    common = {
        "mode": mode,
        "checkpoint_expired": checkpoint_expired,
        "checked_at": checkpoint.get("checked_at"),
        "valid_until": checkpoint.get("valid_until"),
        "rechecked_at": rechecked_at,
    }
    if not accepted_ids:
        return {
            "valid": True,
            **common,
            "currentness": "not_required_no_accepted_claims",
            "stale_claim_ids": [],
            "reasons": [],
        }
    if mode == "offline_fixture":
        return {
            "valid": bool(allow_offline_review_fixtures),
            **common,
            "currentness": "offline_fixture_noncurrent",
            "stale_claim_ids": (
                [] if allow_offline_review_fixtures else accepted_ids
            ),
            "reasons": (
                []
                if allow_offline_review_fixtures
                else ["offline fixture has no DB-current review heads"]
            ),
        }
    try:
        if claims_ledger is None:
            from claims import ClaimsLedger
            claims_ledger = ClaimsLedger()
        current = claims_ledger.get_latest_review_heads(
            str(pack.get("offering_id") or ""),
            claim_ids=accepted_ids,
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        if checkpoint_expired:
            reason = (
                "review-head freshness lease expired and live ledger "
                f"recheck failed: {exc}"
            )
        else:
            reason = f"review ledger unavailable: {exc}"
        return {
            "valid": False,
            **common,
            "currentness": "live_db_unavailable",
            "stale_claim_ids": accepted_ids,
            "reasons": [reason],
        }
    expected = {
        str(head.get("claim_id") or ""): head
        for head in checkpoint.get("heads") or []
    }
    stale = []
    for claim_id in accepted_ids:
        prior = expected.get(claim_id) or {}
        latest = _public_review_head(current.get(claim_id) or {})
        comparison_fields = {
            "current_status",
            "current_claim_sha256",
            "latest_event_id",
            "latest_event_hash",
            "latest_event_status",
            "event_valid",
            "current_matches_event",
            "head_valid",
            "authoritative_human_acceptance",
        }
        if (
            not latest.get("authoritative_human_acceptance")
            or any(latest.get(field) != prior.get(field)
                   for field in comparison_fields)
        ):
            stale.append(claim_id)
    return {
        "valid": not stale,
        **common,
        "currentness": (
            "live_db_rechecked" if not stale else "live_db_stale"
        ),
        "stale_claim_ids": stale,
        "reasons": (
            []
            if not stale
            else ["one or more accepted claims have a newer disposition"]
        ),
    }


def migrate_source_pack(
    pack: dict,
    *,
    claims_ledger=None,
    allow_offline_review_fixtures: bool = False,
) -> dict:
    """Verify and migrate a legacy sealed pack into the current contract."""
    contract = (pack or {}).get("source_pack_contract") or {}
    if contract.get("version") == CONTRACT_VERSION:
        validate_source_pack(
            pack,
            allow_limited=True,
            allow_offline_review_fixtures=(
                allow_offline_review_fixtures
            ),
        )
        return copy.deepcopy(pack)
    _validate_contract_integrity(pack, set(LEGACY_CONTRACT_VERSIONS))
    migrated = seal_source_pack(
        pack,
        claims_ledger=claims_ledger,
        allow_offline_review_fixtures=allow_offline_review_fixtures,
    )
    validate_source_pack(
        migrated,
        allow_limited=True,
        allow_offline_review_fixtures=allow_offline_review_fixtures,
    )
    return migrated


def validate_source_pack(
    pack: dict,
    allow_limited: bool = True,
    *,
    allow_offline_review_fixtures: bool = False,
) -> dict:
    """Validate contract identity, version, hash, and publication readiness."""
    if not isinstance(pack, dict):
        raise ValueError("Source pack must be a JSON object")
    contract = _validate_contract_integrity(pack, {CONTRACT_VERSION})
    _validate_review_head_checkpoint(
        pack,
        allow_offline_review_fixtures=allow_offline_review_fixtures,
    )
    mandatory_facts = list(contract.get("mandatory_facts") or [])
    canonical_mandatory_facts = _mandatory_facts_for_pack(pack)
    if mandatory_facts != canonical_mandatory_facts:
        raise ValueError(
            "Source pack mandatory-fact policy is stale or noncanonical"
        )
    assurance = _mandatory_fact_assurance(pack, mandatory_facts)
    state, reasons = assess_readiness(
        pack, mandatory_facts=mandatory_facts
    )
    if state != contract.get("readiness"):
        raise ValueError("Source pack readiness metadata is stale")
    if reasons != list(contract.get("readiness_reasons") or []):
        raise ValueError("Source pack readiness reasons are stale")
    if contract.get("readiness_policy") != READINESS_POLICY:
        raise ValueError("Source pack readiness policy is stale")
    if assurance != (contract.get("mandatory_fact_assurance") or {}):
        raise ValueError("Source pack mandatory-fact assurance is stale")
    source_claims = _merge_claim_ledgers(
        pack.get("claims_by_type"),
        pack.get("publication_claims"),
    )
    source_claims = _merge_contextual_seller_claims(source_claims, pack)
    source_claims = _merge_structured_claims(source_claims, pack)
    source_claims = _merge_intake_claims(source_claims, pack)
    review_counts = _review_state_counts(
        source_claims,
        pack.get("excluded_publication_claims") or [],
        pack.get("claim_review_inventory"),
        pack,
    )
    if review_counts != (contract.get("review_state_counts") or {}):
        raise ValueError("Source pack review-state counts are stale")
    expected_unverified = [
        fact_key for fact_key in mandatory_facts
        if assurance.get(fact_key, {}).get("state")
        not in {"human_accepted", "corroborated"}
    ]
    if expected_unverified != list(
        contract.get("unverified_mandatory_facts") or []
    ):
        raise ValueError("Source pack verification metadata is stale")
    if state == "blocked":
        raise ValueError("Source pack is blocked: " + "; ".join(reasons))
    if state == "limited" and not allow_limited:
        raise ValueError("Evidence-limited source pack is not allowed")
    return contract
