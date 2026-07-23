"""Versioned publication contract between Source Intelligence and publishers."""

import copy
import hashlib
import json
from datetime import datetime, timezone


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
    "cancellation_policy": "refund_policy",
    "trial_period": "pricing",
}


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
    manifest = (pack or {}).get("intake_manifest", {}) or {}
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
    }


def _canonical_payload(pack: dict) -> bytes:
    payload = copy.deepcopy(pack)
    contract = payload.get("source_pack_contract", {})
    contract.pop("sha256", None)
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _first_artifact_id(pack: dict) -> str:
    artifacts = pack.get("all_artifacts") or {}
    if isinstance(artifacts, dict) and artifacts:
        return str(next(iter(artifacts)))
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict) and artifact.get("artifact_id"):
                return str(artifact["artifact_id"])
    return "structured-source-record"


def _structured_product_claims(pack: dict) -> dict:
    """Migrate captured structured fields into attributed publication claims.

    Older reports populated the CVD/source brief but did not always populate
    the parallel claim ledger. These are not promoted to independent facts:
    they remain explicitly seller/source-material attributed.
    """
    product = pack.get("product") or {}
    artifact_id = _first_artifact_id(pack)
    migrated = {}

    def add(claim_type: str, field: str, text: str):
        clean = str(text or "").strip()
        if not clean or clean.casefold() in {
            "not established", "unknown", "none", "n/a",
        }:
            return
        migrated.setdefault(claim_type, []).append({
            "text": clean,
            "artifact_id": artifact_id,
            "source_class": "official_vendor",
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
            for key, item in value.items():
                add(
                    claim_type,
                    field,
                    f"{str(key).replace('_', ' ')}: {item}",
                )
        else:
            add(claim_type, field, value)
    return migrated


def assess_readiness(full_data: dict) -> tuple:
    """Return (state, reasons). Limited packs remain publishable."""
    product = full_data.get("product", {}) or {}
    reasons = []
    if not str(product.get("product_name", "")).strip():
        reasons.append("missing_product_identity")
    if not str(product.get("official_url", "")).strip():
        reasons.append("missing_official_url")
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
    compliance = pack.get("compliance") or {}
    blocked_texts = set()
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
    if not any(source_claims.values()):
        source_claims = _structured_product_claims(pack)
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
            compliance_blocked = str(claim.get("text", "")).strip().casefold() in blocked_texts
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
                status == "unreviewed"
                and literal
                and has_artifact
                and not seller_attribution_required
                and not compliance_blocked
            )
            safe = not compliance_blocked and (
                status == "accepted"
                or (status == "unreviewed" and literal and has_artifact)
                or seller_attribution_required
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
