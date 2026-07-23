"""Versioned publication contract between Source Intelligence and publishers."""

import copy
import hashlib
import json
from datetime import datetime, timezone


CONTRACT_NAME = "mbk.source-intelligence.publication-pack"
CONTRACT_VERSION = 1


def _canonical_payload(pack: dict) -> bytes:
    payload = copy.deepcopy(pack)
    contract = payload.get("source_pack_contract", {})
    contract.pop("sha256", None)
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


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
    publication_claims = {}
    excluded_claims = []
    for claim_type, items in (pack.get("claims_by_type") or {}).items():
        for claim in items or []:
            status = str(claim.get("review_status", "unreviewed")).lower()
            metadata = claim.get("metadata") or {}
            literal = metadata.get("excerpt_is_literal", True)
            has_artifact = bool(claim.get("artifact_id"))
            safe = (
                status == "accepted"
                or (status == "unreviewed" and literal and has_artifact)
            )
            if safe:
                publication_claims.setdefault(claim_type, []).append(claim)
            else:
                excluded_claims.append({
                    "claim_type": claim_type,
                    "text": claim.get("text", ""),
                    "review_status": status,
                    "reason": "not_accepted_or_literal_artifact_backed",
                })
    pack["publication_claims"] = publication_claims
    pack["excluded_publication_claims"] = excluded_claims
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
