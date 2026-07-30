import copy
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from claims import (
    Claim,
    ClaimsLedger,
    ClaimType,
    ReviewStatus,
    claim_evidence_attestation_payload,
    claim_publication_record,
    claim_snapshot_hash,
    review_attestation_payload,
    review_event_hash,
)
from source_pack_contract import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    REVIEW_HEAD_LEASE_SECONDS,
    SOURCE_PACK_ATTESTATION_KIND,
    _canonical_payload,
    _canonical_review_timestamp,
    _claim_has_mandatory_fact_shape,
    _normalized_assertion_key,
    _review_head_digest,
    _signature_payload,
    _validate_review_head_checkpoint,
    attest_artifact_capture,
    migrate_source_pack as _migrate_source_pack,
    recheck_review_head_checkpoint,
    requires_source_verification,
    seal_source_pack as _seal_source_pack,
    validate_source_pack as _validate_source_pack,
)
from trust_attestations import sign_attestation


_CAPTURED_AT = "2026-07-30T15:00:00+00:00"
_OFFERING_ID = "example-device-offering"


def seal_source_pack(pack, **kwargs):
    """Legacy fixture helper; production never enables this bypass."""
    kwargs.setdefault("allow_offline_review_fixtures", True)
    return _seal_source_pack(pack, **kwargs)


def validate_source_pack(pack, *args, **kwargs):
    kwargs.setdefault("allow_offline_review_fixtures", True)
    return _validate_source_pack(pack, *args, **kwargs)


def migrate_source_pack(pack, **kwargs):
    kwargs.setdefault("allow_offline_review_fixtures", True)
    return _migrate_source_pack(pack, **kwargs)


def _attest_human_claim(
    claim,
    *,
    reviewer="Kevin Mahoney",
    prior_status="unreviewed",
):
    claim.setdefault("offering_id", _OFFERING_ID)
    claim.setdefault("claim_type", "feature")
    claim.setdefault("excerpt", "")
    claim.setdefault("location", "")
    claim.setdefault("captured_at", _CAPTURED_AT)
    claim.setdefault("extraction_method", "manual")
    claim.setdefault("effective_market", "US")
    claim["review_status"] = "accepted"
    claim["reviewed_by"] = reviewer
    claim["reviewed_at"] = _CAPTURED_AT
    event = review_attestation_payload(
        claim_id=claim["claim_id"],
        offering_id=claim["offering_id"],
        prior_status=prior_status,
        new_status="accepted",
        reviewer=reviewer,
        reviewed_at=claim["reviewed_at"],
        claim_snapshot_sha256=claim_snapshot_hash(
            claim, offering_id=claim["offering_id"]
        ),
    )
    claim.setdefault("metadata", {})["review_attestation"] = {
        "event_id": 1,
        "event_hash": review_event_hash(event),
        "event": event,
        "signature": sign_attestation(
            "claim-review-transition", event
        ),
    }
    return claim


def _captured_test_artifact(
    source_url,
    source_class,
    source_relationship,
    *,
    offering_id=_OFFERING_ID,
    content=b"captured evidence",
    capture_route="",
    corroboration_eligible=False,
):
    content_hash = hashlib.sha256(content).hexdigest()
    artifact_id = hashlib.sha256(
        (
            source_url
            + content_hash
            + offering_id
            + source_class
            + capture_route
        ).encode("utf-8")
    ).hexdigest()
    artifact = {
        "artifact_id": artifact_id,
        "artifact_type": "html_snapshot",
        "source_url": source_url,
        "final_url": source_url,
        "source_class": source_class,
        "source_relationship": source_relationship,
        "captured_at": "2026-07-30T12:00:00+00:00",
        "status_code": 200,
        "content_hash": content_hash,
        "content_length": len(source_url),
        "tls_verified": True,
        "is_usable": True,
        "offering_id": offering_id,
        "job_id": "fixture-job",
        "acquisition_phase": "ACQUIRE",
        "capture_route": capture_route,
        "corroboration_eligible": corroboration_eligible,
    }
    artifact["capture_attestation"] = attest_artifact_capture(artifact)
    return artifact_id, artifact


def _attest_evidence_claim(claim, artifact, *, offering_id=_OFFERING_ID):
    """Create a contract fixture equivalent to the trusted extraction boundary."""
    claim.setdefault("claim_id", hashlib.sha256(
        (
            str(claim.get("text") or "")
            + str(claim.get("artifact_id") or "")
        ).encode()
    ).hexdigest()[:32])
    claim.setdefault("offering_id", offering_id)
    claim.setdefault("claim_type", "feature")
    if not claim.get("excerpt"):
        claim["excerpt"] = claim.get("text", "")
    claim.setdefault("location", "fixture")
    claim.setdefault("captured_at", _CAPTURED_AT)
    claim.setdefault("extraction_method", "llm_extraction")
    claim.setdefault("effective_market", "US")
    payload = claim_evidence_attestation_payload(
        claim,
        artifact,
        offering_id=offering_id,
    )
    serialized = __import__("json").dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    claim.setdefault("metadata", {})["evidence_attestation"] = {
        "payload_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "payload": payload,
        "signature": sign_attestation("claim-evidence-link", payload),
    }
    return claim


def _pack(missing=None):
    raw = {
        "offering_id": _OFFERING_ID,
        "product": {
            "product_name": "Example Product",
            "official_url": "https://example.com/product",
            "product_type": "device",
        },
        "all_artifacts": {
            "art-1": {
                "source_url": "https://example.com/product",
                "source_class": "official_vendor",
                "source_relationship": "first_party",
            }
        },
        "source_manifest": [{"type": "official", "status": "captured"}],
        "required_facts": {"missing": missing or []},
    }
    raw["claims_by_type"] = {
        "feature": [
            _attest_human_claim({
                "claim_id": f"feature-{number}",
                "text": f"Literal product fact {number}",
                "artifact_id": "art-1",
                "source_class": "official_vendor",
                "metadata": {
                    "excerpt_is_literal": True,
                    "fact_key": "key_features",
                },
            })
            for number in range(3)
        ]
    }
    return raw


def _single_seller_unreviewed_pack():
    raw = _pack()
    for claim in raw["claims_by_type"]["feature"]:
        claim["review_status"] = "unreviewed"
        claim.pop("reviewed_by", None)
        claim.pop("reviewed_at", None)
        claim["metadata"].pop("review_attestation", None)
    return raw


def _resign_current_pack(pack):
    """Re-sign a deliberately modified current-contract test fixture."""
    contract = pack["source_pack_contract"]
    contract["signature"] = sign_attestation(
        SOURCE_PACK_ATTESTATION_KIND,
        _signature_payload(pack),
    )
    contract["sha256"] = hashlib.sha256(
        _canonical_payload(pack)
    ).hexdigest()
    return pack


def _legacy_v2_pack(raw):
    current = seal_source_pack(raw)
    legacy = copy.deepcopy(current)
    legacy.pop("source_pack_contract_migrations", None)
    legacy["source_pack_contract"] = {
        "name": CONTRACT_NAME,
        "version": 2,
        "generated_at": "2026-07-29T00:00:00+00:00",
        "readiness": "complete",
        "readiness_reasons": [],
        "source_of_truth": "source_intelligence",
        "generation_system": "MBK Master Content Generation System v3.8",
    }
    legacy["source_pack_contract"]["sha256"] = __import__(
        "hashlib"
    ).sha256(_canonical_payload(legacy)).hexdigest()
    return legacy


def test_complete_pack_is_sealed_and_validates():
    pack = seal_source_pack(_pack())
    assert pack["source_pack_contract"]["readiness"] == "complete"
    assert not requires_source_verification(pack)
    assert len(pack["source_pack_contract"]["sha256"]) == 64
    validate_source_pack(pack)


@pytest.mark.parametrize("invalid_version", [True, 3.0, "3"])
def test_source_pack_contract_version_requires_exact_integer(invalid_version):
    pack = seal_source_pack(_pack())
    pack["source_pack_contract"]["version"] = invalid_version

    with pytest.raises(ValueError, match="Unsupported source-pack version"):
        validate_source_pack(pack)


@pytest.mark.parametrize("invalid_version", [True, 1.0, "1"])
def test_review_head_checkpoint_version_requires_exact_integer(
    invalid_version,
):
    pack = seal_source_pack(_pack())
    pack["source_pack_contract"][
        "review_head_checkpoint"
    ]["version"] = invalid_version
    _resign_current_pack(pack)

    with pytest.raises(ValueError, match="checkpoint version"):
        validate_source_pack(pack)


def test_single_seller_literal_claims_are_limited_until_verified():
    pack = seal_source_pack(_single_seller_unreviewed_pack())

    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert pack["source_pack_contract"]["readiness_reasons"] == [
        "unverified_mandatory_facts:key_features"
    ]
    assert pack["source_pack_contract"]["unverified_mandatory_facts"] == [
        "key_features"
    ]
    assert pack["source_pack_contract"]["mandatory_fact_assurance"][
        "key_features"
    ]["state"] == "unverified"
    assert requires_source_verification(pack)
    validate_source_pack(pack)


def test_bare_category_noun_cannot_satisfy_generic_mandatory_fact():
    assert _claim_has_mandatory_fact_shape(
        {
            "text": "Device",
            "metadata": {"fact_key": "key_features"},
        },
        "key_features",
    ) is False
    assert _claim_has_mandatory_fact_shape(
        {
            "text": "Device includes a washable pre-filter",
            "metadata": {"fact_key": "key_features"},
        },
        "key_features",
    ) is True


def test_unrelated_literal_excerpts_cannot_manufacture_corroboration():
    seller_id, seller = _captured_test_artifact(
        "https://seller.example/device",
        "official_vendor",
        "first_party",
        content=b"The device is blue",
    )
    authority_id, authority = _captured_test_artifact(
        "https://fda.gov/device-record",
        "regulatory_database",
        "third_party",
        content=b"FDA record 123",
        capture_route="regulatory_allowlisted",
        corroboration_eligible=True,
    )
    claims = []
    for claim_id, artifact_id, excerpt, artifact in (
        ("seller-false", seller_id, "The device is blue", seller),
        ("authority-false", authority_id, "FDA record 123", authority),
    ):
        claims.append(_attest_evidence_claim({
            "claim_id": claim_id,
            "text": "Device cures asthma",
            "artifact_id": artifact_id,
            "source_class": artifact["source_class"],
            "review_status": "unreviewed",
            "excerpt": excerpt,
            "metadata": {
                "excerpt_is_literal": True,
                "fact_key": "key_features",
            },
        }, artifact))
    claims.append(_attest_evidence_claim({
        "claim_id": "seller-third",
        "text": "Device includes a washable pre-filter",
        "artifact_id": seller_id,
        "source_class": "official_vendor",
        "review_status": "unreviewed",
        "excerpt": "The device is blue",
        "metadata": {
            "excerpt_is_literal": True,
            "fact_key": "key_features",
        },
    }, seller))
    raw = {
        "offering_id": _OFFERING_ID,
        "product": {
            "product_name": "False Corroboration Device",
            "official_url": "https://seller.example/device",
            "product_type": "device",
        },
        "all_artifacts": {
            seller_id: seller,
            authority_id: authority,
        },
        "source_manifest": [{"type": "official", "status": "captured"}],
        "claims_by_type": {"feature": claims},
        "required_facts": {"missing": []},
    }

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert pack["source_pack_contract"][
        "mandatory_fact_assurance"
    ]["key_features"]["state"] == "unverified"


def _live_review_head_fixture(tmp_path):
    from database import ProductDatabase

    db_path = str(tmp_path / "review-head-contract.db")
    ProductDatabase(db_path=db_path)
    ledger = ClaimsLedger(db_path=db_path)
    claim_ids = []
    for number in range(3):
        claim_id = ledger.add_claim(Claim(
            offering_id=_OFFERING_ID,
            claim_text=f"Device includes verified feature {number}",
            claim_type=ClaimType.FEATURE,
            source_artifact_id="art-1",
            exact_excerpt=f"Device includes verified feature {number}",
            source_class="official_vendor",
            metadata={
                "excerpt_is_literal": True,
                "fact_key": "key_features",
            },
        ))
        ledger.update_review(
            claim_id,
            ReviewStatus.ACCEPTED,
            reviewer="Kevin Mahoney",
        )
        claim_ids.append(claim_id)
    accepted_snapshots = [
        claim_publication_record(ledger.get_claim(claim_id))
        for claim_id in claim_ids
    ]
    raw = {
        "offering_id": _OFFERING_ID,
        "product": {
            "product_name": "Review Head Device",
            "official_url": "https://example.com/product",
            "product_type": "device",
        },
        "all_artifacts": {
            "art-1": {
                "source_url": "https://example.com/product",
                "source_class": "official_vendor",
                "source_relationship": "first_party",
            }
        },
        "source_manifest": [{"type": "official", "status": "captured"}],
        "claims_by_type": {"feature": accepted_snapshots},
        "claim_review_inventory": copy.deepcopy(accepted_snapshots),
        "required_facts": {"missing": []},
    }
    sealed = _seal_source_pack(raw, claims_ledger=ledger)
    return ledger, claim_ids, raw, sealed


def test_latest_review_heads_revoke_stale_accepted_pack_snapshots(tmp_path):
    ledger, claim_ids, raw, sealed = _live_review_head_fixture(tmp_path)
    checkpoint = sealed["source_pack_contract"]["review_head_checkpoint"]

    assert sealed["source_pack_contract"]["readiness"] == "complete"
    assert checkpoint["mode"] == "claims_ledger"
    assert checkpoint["accepted_claim_ids"] == sorted(claim_ids)
    checked_at = datetime.fromisoformat(checkpoint["checked_at"])
    valid_until = datetime.fromisoformat(checkpoint["valid_until"])
    assert checked_at.tzinfo is not None
    assert checked_at.utcoffset() == timedelta(0)
    assert valid_until > checked_at
    assert (
        valid_until - checked_at
        == timedelta(seconds=REVIEW_HEAD_LEASE_SECONDS)
    )
    assert checkpoint["lease_seconds"] == REVIEW_HEAD_LEASE_SECONDS
    assert checkpoint["freshness_basis"] == (
        "live_claims_ledger_reconciliation"
    )
    _validate_source_pack(sealed)
    assert recheck_review_head_checkpoint(
        sealed,
        claims_ledger=ledger,
    )["currentness"] == "live_db_rechecked"

    resealed_current = _seal_source_pack(
        sealed,
        claims_ledger=ledger,
    )
    refreshed = resealed_current["source_pack_contract"][
        "review_head_checkpoint"
    ]
    assert refreshed["checked_at"] != checkpoint["checked_at"]
    assert refreshed["valid_until"] != checkpoint["valid_until"]
    assert (
        datetime.fromisoformat(refreshed["valid_until"])
        - datetime.fromisoformat(refreshed["checked_at"])
        == timedelta(seconds=REVIEW_HEAD_LEASE_SECONDS)
    )
    sealed = resealed_current

    for claim_id in claim_ids:
        ledger.update_review(
            claim_id,
            ReviewStatus.REJECTED,
            reviewer="Kevin Mahoney",
        )
    stale_check = recheck_review_head_checkpoint(
        sealed,
        claims_ledger=ledger,
    )
    assert stale_check["valid"] is False
    assert stale_check["stale_claim_ids"] == sorted(claim_ids)

    raw["claim_review_inventory"] = [
        claim_publication_record(ledger.get_claim(claim_id))
        for claim_id in claim_ids
    ]
    resealed = _seal_source_pack(raw, claims_ledger=ledger)
    assert resealed["source_pack_contract"]["readiness"] == "blocked"
    assert resealed["source_pack_contract"][
        "review_head_checkpoint"
    ]["accepted_claim_ids"] == []
    assert {
        claim.get("claim_id")
        for claim in resealed["excluded_publication_claims"]
    } >= set(claim_ids)


def test_not_required_checkpoint_is_explicitly_nonexpiring():
    pack = _seal_source_pack(_single_seller_unreviewed_pack())
    checkpoint = pack["source_pack_contract"]["review_head_checkpoint"]

    assert checkpoint["mode"] == "not_required"
    assert checkpoint["accepted_claim_ids"] == []
    assert checkpoint["valid_until"] is None
    assert checkpoint["lease_seconds"] is None
    assert checkpoint["freshness_basis"] == (
        "not_required_no_accepted_claims"
    )
    assert datetime.fromisoformat(
        checkpoint["checked_at"]
    ).utcoffset() == timedelta(0)
    _validate_source_pack(pack)
    result = recheck_review_head_checkpoint(pack)
    assert result["valid"] is True
    assert result["checkpoint_expired"] is False
    assert result["currentness"] == "not_required_no_accepted_claims"


@pytest.mark.parametrize(
    ("field", "value_factory", "message"),
    [
        (
            "checked_at",
            lambda checked: checked.replace(tzinfo=None).isoformat(),
            "checked_at is not canonical UTC",
        ),
        (
            "checked_at",
            lambda checked: checked.astimezone(
                timezone(timedelta(hours=-5))
            ).isoformat(),
            "checked_at is not canonical UTC",
        ),
        (
            "valid_until",
            lambda checked: _canonical_review_timestamp(checked),
            "freshness lease is invalid",
        ),
        (
            "valid_until",
            lambda checked: _canonical_review_timestamp(
                checked
                + timedelta(seconds=REVIEW_HEAD_LEASE_SECONDS + 1)
            ),
            "freshness lease is invalid",
        ),
    ],
)
def test_claims_ledger_checkpoint_lease_fails_closed(
    tmp_path, field, value_factory, message,
):
    _, _, _, sealed = _live_review_head_fixture(tmp_path)
    checkpoint = sealed["source_pack_contract"]["review_head_checkpoint"]
    checked = datetime.fromisoformat(checkpoint["checked_at"])
    checkpoint[field] = value_factory(checked)
    checkpoint["heads_sha256"] = _review_head_digest(checkpoint)

    with pytest.raises(ValueError, match=message):
        _validate_review_head_checkpoint(sealed)


def test_expired_checkpoint_is_valid_only_after_live_ledger_recheck(tmp_path):
    ledger, claim_ids, _, sealed = _live_review_head_fixture(tmp_path)
    checkpoint = sealed["source_pack_contract"]["review_head_checkpoint"]
    checked = datetime.now(timezone.utc) - timedelta(minutes=30)
    checkpoint["checked_at"] = _canonical_review_timestamp(checked)
    checkpoint["valid_until"] = _canonical_review_timestamp(
        checked + timedelta(seconds=REVIEW_HEAD_LEASE_SECONDS)
    )
    checkpoint["heads_sha256"] = _review_head_digest(checkpoint)
    _resign_current_pack(sealed)

    # Expiry does not corrupt a historical signed pack. Currentness is a
    # separate live check at the point of use.
    _validate_source_pack(sealed)
    live = recheck_review_head_checkpoint(
        sealed,
        claims_ledger=ledger,
    )
    assert live["valid"] is True
    assert live["mode"] == "claims_ledger"
    assert live["checkpoint_expired"] is True
    assert live["currentness"] == "live_db_rechecked"

    class UnavailableLedger:
        def get_latest_review_heads(self, offering_id, claim_ids):
            raise RuntimeError("simulated ledger outage")

    unavailable = recheck_review_head_checkpoint(
        sealed,
        claims_ledger=UnavailableLedger(),
    )
    assert unavailable["valid"] is False
    assert unavailable["checkpoint_expired"] is True
    assert unavailable["currentness"] == "live_db_unavailable"
    assert unavailable["stale_claim_ids"] == sorted(claim_ids)
    assert "freshness lease expired" in unavailable["reasons"][0]


def test_offline_acceptance_fixture_is_explicitly_nonproduction():
    pack = seal_source_pack(_pack())

    assert pack["source_pack_contract"][
        "review_head_checkpoint"
    ]["mode"] == "offline_fixture"
    with pytest.raises(ValueError, match="Offline review fixture"):
        _validate_source_pack(pack)
    assert recheck_review_head_checkpoint(pack)["valid"] is False
    assert recheck_review_head_checkpoint(
        pack,
        allow_offline_review_fixtures=True,
    )["valid"] is True


def test_untrusted_v3_metadata_cannot_remove_canonical_mandatory_facts():
    raw = _single_seller_unreviewed_pack()
    raw["source_pack_contract"] = {
        "name": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "mandatory_facts": [],
    }

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["mandatory_facts"] == [
        "key_features"
    ]
    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert pack["source_pack_contract"][
        "unverified_mandatory_facts"
    ] == ["key_features"]
    validate_source_pack(pack)


def test_rehashed_noncanonical_v3_contract_is_rejected_by_signature():
    pack = seal_source_pack(_single_seller_unreviewed_pack())
    pack["source_pack_contract"]["mandatory_facts"] = []
    pack["source_pack_contract"]["mandatory_fact_assurance"] = {}
    pack["source_pack_contract"]["unverified_mandatory_facts"] = []
    pack["source_pack_contract"]["readiness"] = "complete"
    pack["source_pack_contract"]["readiness_reasons"] = []
    pack["source_pack_contract"]["sha256"] = __import__(
        "hashlib"
    ).sha256(_canonical_payload(pack)).hexdigest()

    with pytest.raises(ValueError, match="trust signature"):
        validate_source_pack(pack)


def test_two_source_class_corroboration_completes_without_human_review():
    raw = _single_seller_unreviewed_pack()
    seller_id, seller_artifact = _captured_test_artifact(
        "https://example.com/product",
        "official_vendor",
        "first_party",
        content=b"Literal product fact 1",
    )
    independent_id, independent_artifact = _captured_test_artifact(
        "https://fda.gov/device",
        "regulatory_database",
        "third_party",
        content=b"Literal product fact 1",
        capture_route="regulatory_allowlisted",
        corroboration_eligible=True,
    )
    raw["all_artifacts"] = {
        seller_id: seller_artifact,
        independent_id: independent_artifact,
    }
    for claim in raw["claims_by_type"]["feature"]:
        claim["artifact_id"] = seller_id
    raw["claims_by_type"]["feature"][1].update({
        "text": raw["claims_by_type"]["feature"][0]["text"],
        "artifact_id": independent_id,
        "source_class": "regulatory_database",
    })
    for claim in raw["claims_by_type"]["feature"]:
        artifact = raw["all_artifacts"][claim["artifact_id"]]
        _attest_evidence_claim(claim, artifact)

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "complete"
    assurance = pack["source_pack_contract"]["mandatory_fact_assurance"][
        "key_features"
    ]
    assert assurance["state"] == "corroborated"
    assert assurance["artifact_ids"] == sorted([
        seller_id,
        independent_id,
    ])
    assert assurance["source_classes"] == [
        "official_vendor", "regulatory_database"
    ]
    assert assurance["source_relationships"] == [
        "first_party", "third_party"
    ]
    assert not requires_source_verification(pack)


def test_third_party_label_on_official_host_cannot_self_corroborate():
    raw = _single_seller_unreviewed_pack()
    raw["all_artifacts"]["spoofed-independent"] = {
        "source_url": "https://cdn.example.com/device-evidence",
        "source_class": "news_media",
        "source_relationship": "third_party",
    }
    raw["claims_by_type"]["feature"][1].update({
        "text": raw["claims_by_type"]["feature"][0]["text"],
        "artifact_id": "spoofed-independent",
        "source_class": "news_media",
    })

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert pack["source_pack_contract"][
        "mandatory_fact_assurance"
    ]["key_features"]["state"] == "unverified"


def test_third_party_label_on_seller_redirect_host_cannot_corroborate():
    raw = _single_seller_unreviewed_pack()
    raw["all_artifacts"]["art-1"]["final_url"] = (
        "https://seller-checkout.example/device"
    )
    raw["all_artifacts"]["spoofed-independent"] = {
        "source_url": "https://seller-checkout.example/evidence",
        "final_url": "https://seller-checkout.example/evidence",
        "source_class": "news_media",
        "source_relationship": "third_party",
    }
    raw["claims_by_type"]["feature"][1].update({
        "text": raw["claims_by_type"]["feature"][0]["text"],
        "artifact_id": "spoofed-independent",
        "source_class": "news_media",
    })

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert pack["source_pack_contract"][
        "mandatory_fact_assurance"
    ]["key_features"]["state"] == "unverified"


def test_third_party_label_on_sibling_seller_host_cannot_corroborate():
    raw = _single_seller_unreviewed_pack()
    raw["product"]["official_url"] = "https://shop.brand.com/product"
    raw["all_artifacts"]["art-1"]["source_url"] = (
        "https://shop.brand.com/product"
    )
    raw["all_artifacts"]["spoofed-independent"] = {
        "source_url": "https://reviews.brand.com/product",
        "source_class": "news_media",
        "source_relationship": "third_party",
    }
    raw["claims_by_type"]["feature"][1].update({
        "text": raw["claims_by_type"]["feature"][0]["text"],
        "artifact_id": "spoofed-independent",
        "source_class": "news_media",
    })

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "limited"


def test_failed_third_party_artifact_cannot_corroborate():
    raw = _single_seller_unreviewed_pack()
    raw["all_artifacts"]["failed-independent"] = {
        "source_url": "https://news.invalid/device",
        "source_class": "news_media",
        "source_relationship": "third_party",
        "status_code": 500,
        "content_length": 0,
        "error": "fetch failed",
        "notes": "FAILED: upstream error",
        "is_usable": False,
    }
    raw["claims_by_type"]["feature"][1].update({
        "text": raw["claims_by_type"]["feature"][0]["text"],
        "artifact_id": "failed-independent",
        "source_class": "news_media",
    })

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert pack["source_pack_contract"][
        "mandatory_fact_assurance"
    ]["key_features"]["state"] == "unverified"


def test_string_false_artifact_usability_fails_closed():
    raw = _single_seller_unreviewed_pack()
    raw["all_artifacts"]["string-false"] = {
        "source_url": "https://news.invalid/device",
        "source_class": "news_media",
        "source_relationship": "third_party",
        "is_usable": "false",
    }
    raw["claims_by_type"]["feature"][1].update({
        "text": raw["claims_by_type"]["feature"][0]["text"],
        "artifact_id": "string-false",
        "source_class": "news_media",
    })

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "limited"


def test_partial_metadata_cannot_hide_contradictory_ingredient_amounts():
    first = {
        "text": "Zinc: 15 mg",
        "metadata": {
            "fact_key": "ingredients_with_amounts",
            "ingredient_name": "Zinc",
        },
    }
    second = {
        "text": "Zinc: 30 mg",
        "metadata": {
            "fact_key": "ingredients_with_amounts",
            "ingredient_name": "Zinc",
        },
    }

    assert _normalized_assertion_key(
        first, "ingredients_with_amounts"
    ) != _normalized_assertion_key(second, "ingredients_with_amounts")


def test_partial_pricing_metadata_does_not_collapse_distinct_prices():
    first = {
        "text": "Single package: $49",
        "metadata": {
            "fact_key": "pricing_tiers",
            "package": "Single package",
        },
    }
    second = {
        "text": "Single package: $79",
        "metadata": {
            "fact_key": "pricing_tiers",
            "package": "Single package",
        },
    }

    assert _normalized_assertion_key(
        first, "pricing_tiers"
    ) != _normalized_assertion_key(second, "pricing_tiers")


@pytest.mark.parametrize(
    ("fact_key", "label", "value"),
    [
        ("active_ingredients", "Active ingredients:", "Menthol"),
        ("whats_included", "What's included", "Workbook and videos"),
        (
            "service_description",
            "Service description",
            "Virtual nutrition coaching",
        ),
        (
            "regulatory_registrations",
            "Regulatory registrations",
            "SEC registration CRD 12345",
        ),
        (
            "lab_results",
            "Lab results",
            "Lab test passed potency analysis",
        ),
        ("allergens", "Allergens", "Contains milk"),
    ],
)
def test_label_only_generic_mandatory_fact_is_not_evidence(
    fact_key, label, value,
):
    claim = {
        "text": label,
        "metadata": {
            "fact_key": fact_key,
            "excerpt_is_literal": True,
        },
    }
    assert _claim_has_mandatory_fact_shape(claim, fact_key) is False
    claim["text"] = value
    assert _claim_has_mandatory_fact_shape(claim, fact_key) is True


@pytest.mark.parametrize(
    "label",
    ["Features:", "Product Features", ".", "***"],
)
def test_device_feature_heading_or_punctuation_is_not_evidence(label):
    assert _claim_has_mandatory_fact_shape(
        {
            "text": label,
            "metadata": {
                "fact_key": "key_features",
                "excerpt_is_literal": True,
            },
        },
        "key_features",
    ) is False


def test_generic_metadata_cannot_collapse_conflicting_assertion_text():
    seller = {
        "text": "Battery lasts 8 hours",
        "metadata": {
            "fact_key": "key_features",
            "normalized_value": "battery runtime",
        },
    }
    independent = {
        "text": "Battery lasts 2 hours",
        "metadata": {
            "fact_key": "key_features",
            "normalized_value": "battery runtime",
        },
    }

    assert _normalized_assertion_key(
        seller, "key_features"
    ) != _normalized_assertion_key(independent, "key_features")


@pytest.mark.parametrize(
    ("ingredient_text", "serving_text", "expected_readiness"),
    [
        ("Zinc", "Serving size", "limited"),
        ("Zinc 15 mg", "Serving size: 1 capsule", "complete"),
    ],
)
def test_supplement_mandatory_assurance_requires_values_not_labels(
    ingredient_text, serving_text, expected_readiness,
):
    evidence_bytes = f"{ingredient_text}\n{serving_text}".encode()
    seller_id, seller_artifact = _captured_test_artifact(
        "https://example.com/shape-test",
        "official_vendor",
        "first_party",
        content=evidence_bytes,
    )
    independent_id, independent_artifact = _captured_test_artifact(
        "https://ods.od.nih.gov/shape-test",
        "regulatory_database",
        "third_party",
        content=evidence_bytes,
        capture_route="regulatory_allowlisted",
        corroboration_eligible=True,
    )
    raw = {
        "offering_id": _OFFERING_ID,
        "product": {
            "product_name": "Shape Test Supplement",
            "official_url": "https://example.com/shape-test",
            "product_type": "supplement",
        },
        "all_artifacts": {
            seller_id: seller_artifact,
            independent_id: independent_artifact,
        },
        "claims_by_type": {
            "ingredient_amount": [
                {
                    "text": ingredient_text,
                    "artifact_id": artifact_id,
                    "source_class": source_class,
                    "review_status": "unreviewed",
                    "metadata": {
                        "excerpt_is_literal": True,
                        "fact_key": "ingredients_with_amounts",
                    },
                }
                for artifact_id, source_class in (
                    (seller_id, "official_vendor"),
                    (independent_id, "regulatory_database"),
                )
            ],
            "serving_info": [
                {
                    "text": serving_text,
                    "artifact_id": artifact_id,
                    "source_class": source_class,
                    "review_status": "unreviewed",
                    "metadata": {
                        "excerpt_is_literal": True,
                        "fact_key": "serving_size",
                    },
                }
                for artifact_id, source_class in (
                    (seller_id, "official_vendor"),
                    (independent_id, "regulatory_database"),
                )
            ],
        },
        "required_facts": {"missing": []},
    }
    for claims in raw["claims_by_type"].values():
        for claim in claims:
            _attest_evidence_claim(
                claim,
                raw["all_artifacts"][claim["artifact_id"]],
            )

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == expected_readiness
    states = {
        fact: item["state"]
        for fact, item in pack["source_pack_contract"][
            "mandatory_fact_assurance"
        ].items()
    }
    if expected_readiness == "limited":
        assert states == {
            "ingredients_with_amounts": "missing",
            "serving_size": "missing",
        }
    else:
        assert states == {
            "ingredients_with_amounts": "corroborated",
            "serving_size": "corroborated",
        }
    validate_source_pack(pack)


@pytest.mark.parametrize("failure_mode", [
    "same_artifact",
    "seller_only",
    "conflicting_value",
    "missing_source_class",
    "missing_source_relationship",
    "first_party_mislabeled_news",
])
def test_false_corroboration_patterns_remain_limited(failure_mode):
    raw = _single_seller_unreviewed_pack()
    second_id = "art-1" if failure_mode == "same_artifact" else "second-art"
    second_class = (
        "authorized_reseller"
        if failure_mode == "seller_only"
        else ("news_media" if failure_mode != "missing_source_class" else "")
    )
    if second_id != "art-1":
        raw["all_artifacts"][second_id] = {
            "source_url": "https://second.example/device",
            "source_class": second_class,
            "source_relationship": (
                "second_party"
                if failure_mode == "seller_only"
                else "first_party"
                if failure_mode == "first_party_mislabeled_news"
                else ""
                if failure_mode == "missing_source_relationship"
                else "third_party"
            ),
        }
    raw["claims_by_type"]["feature"][1].update({
        "text": raw["claims_by_type"]["feature"][0]["text"],
        "artifact_id": second_id,
        "source_class": second_class,
    })
    if failure_mode == "conflicting_value":
        raw["claims_by_type"]["feature"][1]["text"] = (
            "Contradictory product fact"
        )

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert requires_source_verification(pack)


@pytest.mark.parametrize("reviewer", [None, "", "human", "system"])
def test_accepted_without_named_human_does_not_clear_readiness(reviewer):
    raw = _single_seller_unreviewed_pack()
    for claim in raw["claims_by_type"]["feature"]:
        claim["review_status"] = "accepted"
        claim["reviewed_by"] = reviewer

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "limited"


def test_auto_substitution_is_attributed_but_never_human_verified():
    raw = _single_seller_unreviewed_pack()
    claim = raw["claims_by_type"]["feature"][0]
    claim.update({
        "review_status": "auto_substituted",
        "reviewed_by": "source-intelligence-automation",
        "extraction_method": "automated_policy_substitution",
    })
    claim["metadata"].update({
        "excerpt_is_literal": False,
        "substitution_origin": "automated_policy",
    })

    pack = seal_source_pack(raw)
    published = pack["publication_claims"]["feature"][0]

    assert published["review_status"] == "auto_substituted"
    assert published["publication_treatment"] == (
        "source_attribution_required"
    )
    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert pack["source_pack_contract"]["review_state_counts"] == {
        "accepted": 0,
        "auto_substituted": 1,
        "unreviewed": 2,
        "needs_verification": 0,
        "conflicted": 0,
        "excluded": 0,
    }


def test_named_human_can_accept_a_prior_auto_substitution():
    raw = _single_seller_unreviewed_pack()
    claim = raw["claims_by_type"]["feature"][0]
    claim.update({
        "review_status": "accepted",
        "reviewed_by": "Kevin Mahoney",
        "reviewed_at": "2026-07-30T15:00:00+00:00",
        "extraction_method": "automated_policy_substitution",
    })
    claim["metadata"].update({
        "excerpt_is_literal": False,
        "substitution_origin": "automated_policy",
        "substitution_note": "automatic platform-safe substitution",
    })
    _attest_human_claim(
        claim,
        prior_status="auto_substituted",
    )

    pack = seal_source_pack(raw)

    assurance = pack["source_pack_contract"][
        "mandatory_fact_assurance"
    ]["key_features"]
    assert assurance["state"] == "human_accepted"
    assert pack["source_pack_contract"]["readiness"] == "complete"
    assert pack["source_pack_contract"]["review_state_counts"] == {
        "accepted": 1,
        "auto_substituted": 0,
        "unreviewed": 2,
        "needs_verification": 0,
        "conflicted": 0,
        "excluded": 0,
    }
    validate_source_pack(pack)


def test_missing_product_type_fails_closed():
    raw = _pack()
    raw["product"].pop("product_type")

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "blocked"
    assert "missing_product_type" in (
        pack["source_pack_contract"]["readiness_reasons"]
    )


def test_valid_v2_pack_migrates_with_preserved_prior_decision():
    legacy = _legacy_v2_pack(_single_seller_unreviewed_pack())

    migrated = migrate_source_pack(legacy)

    assert migrated["source_pack_contract"]["version"] == CONTRACT_VERSION
    assert migrated["source_pack_contract"]["readiness"] == "limited"
    event = migrated["source_pack_contract_migrations"][-1]
    assert event["prior_sha256"] == legacy["source_pack_contract"]["sha256"]
    assert event["prior_readiness"] == "complete"
    assert event["new_readiness"] == "limited"
    assert migrate_source_pack(migrated) == migrated


def test_tampered_v2_pack_refuses_migration():
    legacy = _legacy_v2_pack(_single_seller_unreviewed_pack())
    legacy["product"]["product_name"] = "Tampered"

    with pytest.raises(ValueError, match="integrity"):
        migrate_source_pack(legacy)


def test_seal_unions_claim_ledgers_and_preserves_audit_only_exclusions():
    raw = _pack()
    raw["publication_claims"] = {
        "feature": [{
            "claim_id": "publication-only",
            "text": "Publication-only verified feature",
            "artifact_id": "art-1",
            "source_class": "official_vendor",
            "review_status": "accepted",
            "reviewed_by": "Migration Test Reviewer",
            "metadata": {
                "excerpt_is_literal": True,
                "fact_key": "key_features",
            },
        }],
    }
    raw["excluded_publication_claims"] = [{
        "claim_id": "audit-only-rejection",
        "claim_type": "manufacturer_claim",
        "text": "Previously rejected sales claim",
        "artifact_id": "art-1",
        "review_status": "rejected",
        "reason": "rejected_source_claim",
    }]

    sealed = seal_source_pack(raw)
    resealed = seal_source_pack(sealed)

    assert {
        claim.get("claim_id")
        for claim in sealed["publication_claims"]["feature"]
    } >= {"publication-only"}
    assert sealed["publication_claim_summary"][
        "publication_claim_count"
    ] == 4
    assert {
        claim.get("claim_id")
        for claim in sealed["excluded_publication_claims"]
    } == {"audit-only-rejection"}
    assert resealed["publication_claim_summary"] == (
        sealed["publication_claim_summary"]
    )
    assert resealed["source_pack_contract"]["review_state_counts"] == (
        sealed["source_pack_contract"]["review_state_counts"]
    )
    validate_source_pack(resealed)


def test_limited_pack_is_publishable_by_default():
    pack = seal_source_pack(_pack(["pricing"]))
    assert pack["source_pack_contract"]["readiness"] == "limited"
    validate_source_pack(pack)
    with pytest.raises(ValueError, match="Evidence-limited"):
        validate_source_pack(pack, allow_limited=False)


def test_missing_source_material_blocks_pack():
    raw = _pack()
    raw["all_artifacts"] = {}
    raw["source_manifest"] = []
    pack = seal_source_pack(raw)
    assert pack["source_pack_contract"]["readiness"] == "blocked"
    with pytest.raises(ValueError, match="no_captured_source_material"):
        validate_source_pack(pack)


def test_zero_claim_pack_is_blocked_before_paid_generation():
    raw = _pack()
    raw["claims_by_type"] = {}
    pack = seal_source_pack(raw)
    assert pack["source_pack_contract"]["readiness"] == "blocked"
    assert "insufficient_publication_claims:0/3" in (
        pack["source_pack_contract"]["readiness_reasons"]
    )
    with pytest.raises(ValueError, match="insufficient_publication_claims"):
        validate_source_pack(pack)


def test_empty_human_accepted_rows_cannot_satisfy_readiness():
    raw = _pack()
    for claim in raw["claims_by_type"]["feature"]:
        claim["text"] = "   "
        claim.pop("artifact_id", None)

    pack = seal_source_pack(raw)

    assert pack["publication_claim_summary"][
        "publication_claim_count"
    ] == 0
    assert pack["source_pack_contract"]["readiness"] == "blocked"
    assert pack["source_pack_contract"]["mandatory_fact_assurance"][
        "key_features"
    ]["state"] == "missing"
    with pytest.raises(ValueError, match="insufficient_publication_claims"):
        validate_source_pack(pack)


def test_device_pack_recovers_literal_affiliate_seller_headings():
    raw = _pack()
    raw["claims_by_type"] = {
        "pricing": [
            {
                "text": "Single Unit: $49.99",
                "artifact_id": "art-1",
                "source_class": "official_vendor",
                "review_status": "needs_verification",
                "metadata": {
                    "excerpt_is_literal": False,
                    "structured_source_record": True,
                },
            },
        ],
    }
    raw["all_artifacts"]["affiliate-art"] = {
        "source_url": "https://partner.example/device",
        "source_class": "authorized_reseller",
    }
    raw["contextual_source_profiles"] = [{
        "source_type": "affiliate_page",
        "artifact_id": "affiliate-art",
        "headings": [
            "How It Works",
            "Stabilizes the Power",
            "Reduces Dirty Electricity",
            "Easy to Install, No Maintenance Required",
            "Plug In the Device",
            "Check the Active Indicator",
            "Designed for Continuous Operation",
            "Filter, Stabilize and Save",
            "GET UP TO 65% OFF NOW",
        ],
    }]

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["readiness"] == "limited"
    recovered = pack["publication_claims"]["manufacturer_claim"]
    assert [claim["text"] for claim in recovered] == [
        "Stabilizes the Power",
        "Reduces Dirty Electricity",
        "Easy to Install, No Maintenance Required",
        "Plug In the Device",
        "Check the Active Indicator",
        "Designed for Continuous Operation",
    ]
    assert all(
        claim["publication_treatment"] == "seller_attribution_required"
        for claim in recovered
    )


def test_contextual_seller_headings_do_not_rescue_non_device_pack():
    raw = _pack()
    raw["product"]["product_type"] = "supplement"
    raw["claims_by_type"] = {}
    raw["contextual_source_profiles"] = [{
        "source_type": "affiliate_page",
        "artifact_id": "affiliate-art",
        "headings": ["Supports Healthy Blood Sugar"],
    }]
    pack = seal_source_pack(raw)
    assert pack["source_pack_contract"]["readiness"] == "blocked"


def test_unverified_affiliate_remains_a_source_attributed_promotion():
    raw = _pack()
    raw["claims_by_type"] = {}
    raw["all_artifacts"]["affiliate-art"] = {
        "source_url": "https://partner.example/device",
        "source_class": "third_party_web_search",
    }
    raw["contextual_source_profiles"] = [{
        "source_type": "affiliate_page",
        "artifact_id": "affiliate-art",
        "headings": ["Stabilizes the Power"],
    }]
    pack = seal_source_pack(raw)
    recovered = pack["publication_claims"]["manufacturer_claim"]
    assert recovered[0]["source_class"] == "third_party_web_search"
    assert (
        recovered[0]["publication_treatment"]
        == "source_attribution_required"
    )


def test_resealing_legacy_publication_ledger_does_not_erase_claims():
    first = seal_source_pack(_pack())
    legacy = copy.deepcopy(first)
    legacy.pop("claims_by_type", None)
    resealed = seal_source_pack(legacy)
    assert resealed["publication_claim_summary"] == {
        "raw_claim_count": 3,
        "publication_claim_count": 3,
        "excluded_claim_count": 0,
    }
    assert len(resealed["publication_claims"]["feature"]) == 3


def test_structured_device_record_migrates_to_attributed_claim_ledger():
    raw = _pack()
    raw["claims_by_type"] = {}
    raw["product"].update({
        "key_features": ["Voltage stabilization", "Plug-and-play installation"],
        "specifications": {"voltage_range": "90V–250V"},
        "pricing": [
            {"package": "Single Unit", "price": "49.99", "per_unit": "49.99"},
        ],
    })
    pack = seal_source_pack(raw)
    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert pack["publication_claim_summary"]["publication_claim_count"] == 4
    claims = [
        claim
        for items in pack["publication_claims"].values()
        for claim in items
    ]
    assert all(
        claim["publication_treatment"] == "seller_attribution_required"
        for claim in claims
    )
    assert all(
        claim["metadata"]["structured_source_record"] is True
        for claim in claims
    )


def test_structured_buyer_protection_fields_enter_publication_ledger():
    raw = _pack()
    raw["claims_by_type"] = {}
    raw["product"].update({
        "eligibility": "Must be 18+",
        "odds_or_randomness_disclosure": (
            "Fortune Numbers are not a prediction of any lottery result"
        ),
        "warnings": ["For entertainment and reflection purposes only"],
        "guarantees": ["60-day refund guarantee"],
        "refund_policy": {
            "duration_days": 60,
            "conditions": "If not satisfied with purchase",
        },
    })

    pack = seal_source_pack(raw)
    claims = [
        claim
        for items in pack["publication_claims"].values()
        for claim in items
    ]
    claim_text = {claim["text"] for claim in claims}

    assert "Must be 18+" in claim_text
    assert (
        "Fortune Numbers are not a prediction of any lottery result"
        in claim_text
    )
    assert "For entertainment and reflection purposes only" in claim_text
    assert "60-day refund guarantee" in claim_text
    assert "duration days: 60" in claim_text
    assert "conditions: If not satisfied with purchase" in claim_text
    assert all(
        claim["publication_treatment"] == "seller_attribution_required"
        for claim in claims
    )


def test_structured_dict_order_does_not_change_sealed_contract_identity():
    first = _pack()
    first["claims_by_type"] = {}
    first["product"]["refund_policy"] = {
        "duration_days": 60,
        "conditions": "If not satisfied with purchase",
        "contact_method": "Contact support",
    }
    first["source_pack_contract"] = {
        "generated_at": "2026-07-25T00:00:00+00:00"
    }
    second = copy.deepcopy(first)
    second["product"]["refund_policy"] = {
        "contact_method": "Contact support",
        "conditions": "If not satisfied with purchase",
        "duration_days": 60,
    }

    sealed_first = seal_source_pack(first)
    sealed_second = seal_source_pack(second)

    assert sealed_first["publication_claims"] == (
        sealed_second["publication_claims"]
    )
    assert sealed_first["source_pack_contract"]["sha256"] == (
        sealed_second["source_pack_contract"]["sha256"]
    )


def test_structured_facts_reconcile_when_raw_claims_all_fail():
    raw = _pack()
    raw["claims_by_type"] = {
        "manufacturer_claim": [{
            "text": "Can save up to 47% on electricity",
            "artifact_id": "art-1",
            "source_class": "third_party_web_search",
            "review_status": "needs_verification",
            "metadata": {"excerpt_is_literal": False},
        }],
    }
    raw["product"].update({
        "key_features": [
            "Voltage stabilization",
            "Power factor correction",
            "Dirty electricity filtering",
        ],
        "specifications": {"voltage_range": "90V–250V"},
    })
    pack = seal_source_pack(raw)
    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert pack["publication_claim_summary"]["publication_claim_count"] == 4
    assert pack["publication_claim_summary"]["excluded_claim_count"] == 1
    assert pack["excluded_publication_claims"][0]["text"] == (
        "Can save up to 47% on electricity"
    )


def test_tampering_is_detected():
    pack = seal_source_pack(_pack())
    tampered = copy.deepcopy(pack)
    tampered["product"]["product_name"] = "Different Product"
    with pytest.raises(ValueError, match="integrity"):
        validate_source_pack(tampered)


def test_unverified_claims_are_excluded_from_publication_context():
    raw = _pack()
    raw["claims_by_type"] = {
        "manufacturer_claim": [
            {
                "text": "Literal brand statement",
                "artifact_id": "art-1",
                "review_status": "unreviewed",
                "metadata": {"excerpt_is_literal": True},
            },
            {
                "text": "Inferred outcome",
                "artifact_id": "art-1",
                "review_status": "needs_verification",
                "metadata": {"excerpt_is_literal": False},
            },
        ]
    }
    pack = seal_source_pack(raw)
    claims = pack["publication_claims"]["manufacturer_claim"]
    assert [c["text"] for c in claims] == ["Literal brand statement"]
    assert pack["excluded_publication_claims"][0]["text"] == "Inferred outcome"


def test_literal_device_seller_claim_requires_attribution_but_is_publishable():
    raw = _pack()
    raw["product"]["product_type"] = "device"
    raw["claims_by_type"] = {
        "specification": [
            {
                "text": "Seller states a 90V–250V operating range",
                "artifact_id": "official-page-artifact",
                "source_class": "official_vendor",
                "review_status": "needs_verification",
                "metadata": {"excerpt_is_literal": True},
            },
        ],
        "certification": [
            {
                "text": "Safety certified",
                "artifact_id": "official-page-artifact",
                "source_class": "official_vendor",
                "review_status": "needs_verification",
                "metadata": {"excerpt_is_literal": True},
            },
        ],
    }

    pack = seal_source_pack(raw)

    specification = pack["publication_claims"]["specification"][0]
    assert specification["publication_treatment"] == (
        "seller_attribution_required"
    )
    assert "certification" not in pack["publication_claims"]
    assert pack["excluded_publication_claims"][0]["text"] == "Safety certified"


def test_device_attribution_needs_explicit_literal_seller_provenance():
    raw = _pack()
    raw["product"]["product_type"] = "device"
    raw["claims_by_type"] = {
        "feature": [
            {
                "text": "Inferred seller feature",
                "artifact_id": "official-page-artifact",
                "source_class": "official_vendor",
                "review_status": "needs_verification",
                "metadata": {},
            },
            {
                "text": "Competitor description",
                "artifact_id": "news-artifact",
                "source_class": "news_media",
                "review_status": "needs_verification",
                "metadata": {"excerpt_is_literal": True},
            },
        ],
    }

    pack = seal_source_pack(raw)

    assert "feature" not in pack["publication_claims"]
    assert {
        item["text"] for item in pack["excluded_publication_claims"]
    } == {"Inferred seller feature", "Competitor description"}


def test_accepted_seller_device_claim_still_requires_attribution():
    raw = _pack()
    raw["all_artifacts"]["art-1"]["source_class"] = "official_vendor"
    raw["claims_by_type"] = {
        "feature": [{
            "text": "The device filters dirty electricity",
            "artifact_id": "art-1",
            "review_status": "accepted",
            "metadata": {"excerpt_is_literal": True},
        }]
    }
    pack = seal_source_pack(raw)
    assert pack["publication_claims"]["feature"][0][
        "publication_treatment"
    ] == "seller_attribution_required"


def test_review_state_counts_include_quarantined_ledger_rows():
    raw = _pack()
    inventory = []
    for claim in raw["claims_by_type"]["feature"]:
        inventory.append({
            **copy.deepcopy(claim),
            "claim_type": "feature",
        })
    inventory.extend([
        {
            "claim_id": "auto-1",
            "claim_type": "feature",
            "text": "Policy-safe automated wording",
            "review_status": "auto_substituted",
            "reviewed_by": "source-intelligence-automation",
        },
        {
            "claim_id": "unreviewed-1",
            "claim_type": "feature",
            "text": "Unreviewed fact",
            "review_status": "unreviewed",
        },
        {
            "claim_id": "needs-1",
            "claim_type": "feature",
            "text": "Fact needing verification",
            "review_status": "needs_verification",
        },
        {
            "claim_id": "conflict-1",
            "claim_type": "pricing",
            "text": "$10 or $20",
            "review_status": "conflicted",
        },
        {
            "claim_id": "rejected-1",
            "claim_type": "manufacturer_claim",
            "text": "Rejected claim",
            "review_status": "rejected",
        },
    ])
    raw["claim_review_inventory"] = inventory

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["review_state_counts"] == {
        "accepted": 3,
        "auto_substituted": 1,
        "unreviewed": 1,
        "needs_verification": 1,
        "conflicted": 1,
        "excluded": 2,
    }
    excluded_ids = {
        item.get("claim_id")
        for item in pack["excluded_publication_claims"]
    }
    assert {"conflict-1", "rejected-1"} <= excluded_ids
    validate_source_pack(pack)


def test_review_state_counts_merge_partial_inventory_and_synthesized_claims():
    raw = _pack()
    raw["product"]["warranty"] = "One year"
    raw["claims_by_type"]["pricing"] = [{
        "text": "Conflicting price record",
        "artifact_id": "art-1",
        "source_class": "official_vendor",
        "review_status": "conflicted",
        "metadata": {
            "excerpt_is_literal": True,
            "fact_key": "pricing",
        },
    }]
    raw["claim_review_inventory"] = [
        {
            **copy.deepcopy(claim),
            "claim_type": "feature",
        }
        for claim in raw["claims_by_type"]["feature"]
    ]

    pack = seal_source_pack(raw)

    assert pack["source_pack_contract"]["review_state_counts"] == {
        "accepted": 3,
        "auto_substituted": 0,
        "unreviewed": 0,
        "needs_verification": 1,
        "conflicted": 1,
        "excluded": 1,
    }
    assert {
        item["review_status"]
        for item in pack["excluded_publication_claims"]
    } == {"conflicted"}
    validate_source_pack(pack)


def test_unreviewed_literal_news_claim_requires_source_attribution():
    raw = _pack()
    raw["all_artifacts"]["art-1"]["source_class"] = "news_media"
    raw["claims_by_type"] = {
        "company_info": [{
            "text": "The company launched in 2025",
            "artifact_id": "art-1",
            "review_status": "unreviewed",
            "metadata": {"excerpt_is_literal": True},
        }]
    }
    pack = seal_source_pack(raw)
    assert pack["publication_claims"]["company_info"][0][
        "publication_treatment"
    ] == "source_attribution_required"


def test_blocked_compliance_match_is_quarantined_from_publication_claims():
    raw = _pack()
    raw["claims_by_type"]["feature"][0]["text"] = (
        "This product guarantees a winning outcome"
    )
    raw["compliance"] = {
        "results": [{
            "rule_id": "DECEPTIVE_CLAIMS",
            "state": "blocked",
            "matched_text": "guarantees a winning outcome",
        }]
    }
    pack = seal_source_pack(raw)
    published = [
        claim["text"]
        for claims in pack["publication_claims"].values()
        for claim in claims
    ]
    assert "This product guarantees a winning outcome" not in published
    assert any(
        item["text"] == "This product guarantees a winning outcome"
        for item in pack["excluded_publication_claims"]
    )


def test_explicit_unknown_type_blocks_before_workbench_spend():
    raw = _pack()
    raw["product"]["product_type"] = "unknown"
    pack = seal_source_pack(raw)
    assert pack["source_pack_contract"]["readiness"] == "blocked"
    with pytest.raises(ValueError, match="unsupported_product_type"):
        validate_source_pack(pack)


def test_structured_claim_never_inherits_false_official_provenance():
    raw = _pack()
    raw["claims_by_type"] = {}
    raw["product"]["key_features"] = ["Operator supplied feature"]
    raw["all_artifacts"] = {
        "operator-note": {
            "source_url": "intake://operator",
            "source_class": "operator_submitted",
        }
    }
    pack = seal_source_pack(raw)
    assert not pack["publication_claims"]


def test_structured_company_dictionary_skips_blank_and_placeholder_cells():
    raw = _pack()
    raw["all_artifacts"]["art-1"]["source_class"] = "official_vendor"
    raw["product"]["company"] = {
        "name": "[OPERATOR LEGAL NAME]",
        "address": "[ADDRESS]",
        "phone": "",
        "website": "https://example.com/product",
    }
    pack = seal_source_pack(raw)
    company_claims = [
        item["text"]
        for item in pack["publication_claims"].get("company_info", [])
    ]
    assert "phone:" not in company_claims
    assert not any("[OPERATOR LEGAL NAME]" in item for item in company_claims)
    assert not any("[ADDRESS]" in item for item in company_claims)
    assert "website: https://example.com/product" in company_claims
