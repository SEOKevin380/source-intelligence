import copy
import hashlib
import json
import os
import stat

import pytest

from evidence import (
    Artifact,
    EvidenceIntegrityError,
    EvidenceLake,
    SourceClass,
    SourceRelationship,
)
from claims import (
    Claim,
    ClaimsLedger,
    ClaimType,
    claim_evidence_attestation_payload,
)
from source_pack_contract import (
    CONTRACT_NAME,
    _canonical_payload,
    attest_artifact_capture,
    migrate_source_pack,
    seal_source_pack,
    validate_source_pack,
    verify_artifact_attestation,
    verify_source_pack_signature,
)
from trust_attestations import (
    KEY_ID_FILENAME,
    PIN_ENVIRONMENT_VARIABLE,
    PRIVATE_KEY_FILENAME,
    public_key_fingerprint,
    sign_attestation,
    signing_identity,
    verify_attestation,
)


@pytest.fixture
def trust_key_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "source-intelligence-data"
    monkeypatch.setenv("SOURCE_INTELLIGENCE_DATA_DIR", str(data_dir))
    return data_dir


def _captured_artifact(
    source_url,
    source_class,
    source_relationship,
    content,
):
    content_hash = hashlib.sha256(content).hexdigest()
    artifact_id = hashlib.sha256(
        (
            source_url
            + content_hash
            + source_class
            + "trust-test-offering"
        ).encode()
    ).hexdigest()
    independent = source_class == "regulatory_database"
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
        "content_length": len(content),
        "tls_verified": True,
        "is_usable": True,
        "offering_id": "trust-test-offering",
        "job_id": "trust-test-job",
        "acquisition_phase": "ACQUIRE",
        "capture_route": (
            "regulatory_allowlisted" if independent else "official_page"
        ),
        "corroboration_eligible": independent,
        "error": "",
        "notes": "",
    }
    artifact["capture_attestation"] = attest_artifact_capture(artifact)
    return artifact


def _attest_claim_evidence(claim, artifact):
    claim.setdefault("offering_id", "trust-test-offering")
    claim.setdefault("claim_type", "feature")
    claim.setdefault("excerpt", claim["text"])
    claim.setdefault("location", "fixture")
    claim.setdefault("captured_at", "2026-07-30T12:00:00+00:00")
    claim.setdefault("extraction_method", "llm_extraction")
    claim.setdefault("effective_market", "US")
    payload = claim_evidence_attestation_payload(
        claim,
        artifact,
        offering_id="trust-test-offering",
    )
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    claim["metadata"]["evidence_attestation"] = {
        "payload_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "payload": payload,
        "signature": sign_attestation("claim-evidence-link", payload),
    }
    return claim


def _corroborated_pack(include_independent_attestation=True):
    shared_text = "Device includes a washable pre-filter"
    seller = _captured_artifact(
        "https://seller.example/device",
        "official_vendor",
        "first_party",
        (shared_text + "\nSeller lists a replaceable filter cartridge").encode(),
    )
    independent = _captured_artifact(
        "https://fda.gov/device",
        "regulatory_database",
        "third_party",
        shared_text.encode(),
    )
    claims = [
        {
            "claim_id": "seller-shared",
            "text": shared_text,
            "artifact_id": seller["artifact_id"],
            "source_class": "official_vendor",
            "review_status": "unreviewed",
            "metadata": {
                "excerpt_is_literal": True,
                "fact_key": "key_features",
            },
        },
        {
            "claim_id": "independent-shared",
            "text": shared_text,
            "artifact_id": independent["artifact_id"],
            "source_class": "regulatory_database",
            "review_status": "unreviewed",
            "metadata": {
                "excerpt_is_literal": True,
                "fact_key": "key_features",
            },
        },
        {
            "claim_id": "seller-second",
            "text": "Seller lists a replaceable filter cartridge",
            "artifact_id": seller["artifact_id"],
            "source_class": "official_vendor",
            "review_status": "unreviewed",
            "metadata": {
                "excerpt_is_literal": True,
                "fact_key": "key_features",
            },
        },
    ]
    for claim in claims:
        artifact = (
            independent
            if claim["artifact_id"] == independent["artifact_id"]
            else seller
        )
        _attest_claim_evidence(claim, artifact)
    if not include_independent_attestation:
        independent.pop("capture_attestation")
    return {
        "offering_id": "trust-test-offering",
        "product": {
            "product_name": "Trust Test Device",
            "official_url": "https://seller.example/device",
            "product_type": "device",
        },
        "all_artifacts": {
            seller["artifact_id"]: seller,
            independent["artifact_id"]: independent,
        },
        "source_manifest": [{"type": "official", "status": "captured"}],
        "claims_by_type": {"feature": claims},
        "required_facts": {"missing": []},
    }


def test_legitimate_seal_is_signed_and_validates(trust_key_dir):
    pack = seal_source_pack(_corroborated_pack())

    contract = pack["source_pack_contract"]
    assert contract["readiness"] == "complete"
    assert contract["mandatory_fact_assurance"]["key_features"][
        "state"
    ] == "corroborated"
    assert contract["trust_identity"]["algorithm"] == "Ed25519"
    assert contract["trust_identity"]["key_id"] == public_key_fingerprint()
    assert contract["signature"]["key_id"] == public_key_fingerprint()
    assert verify_source_pack_signature(
        pack,
        contract["trust_identity"]["public_key"],
    )
    validate_source_pack(pack)

    key_path = trust_key_dir / PRIVATE_KEY_FILENAME
    assert key_path.is_file()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert (trust_key_dir / KEY_ID_FILENAME).read_text().strip() == (
        public_key_fingerprint()
    )


def test_missing_key_with_identity_marker_never_rotates_silently(
    trust_key_dir,
):
    signing_identity()
    (trust_key_dir / PRIVATE_KEY_FILENAME).unlink()

    with pytest.raises(RuntimeError, match="restore the key"):
        signing_identity()


def test_configured_trust_fingerprint_is_enforced(
    trust_key_dir,
    monkeypatch,
):
    signing_identity()
    monkeypatch.setenv(PIN_ENVIRONMENT_VARIABLE, "sha256:" + "0" * 64)

    with pytest.raises(RuntimeError, match=PIN_ENVIRONMENT_VARIABLE):
        signing_identity()


@pytest.mark.parametrize("invalid_version", [True, 1.0, "1"])
def test_attestation_version_requires_exact_integer(
    trust_key_dir, invalid_version,
):
    payload = {"claim_id": "strict-version"}
    attestation = sign_attestation("strict-version-test", payload)
    attestation["version"] = invalid_version

    assert verify_attestation(
        "strict-version-test", payload, attestation
    ) is False


def test_forged_pack_hash_without_valid_signature_is_rejected(trust_key_dir):
    pack = seal_source_pack(_corroborated_pack())
    forged = copy.deepcopy(pack)
    forged["product"]["product_name"] = "Forged Product Identity"
    forged["source_pack_contract"]["sha256"] = hashlib.sha256(
        _canonical_payload(forged)
    ).hexdigest()

    with pytest.raises(ValueError, match="trust signature"):
        validate_source_pack(forged)


def test_mutated_signed_pack_is_rejected_before_recomputed_metadata(
    trust_key_dir,
):
    pack = seal_source_pack(_corroborated_pack())
    pack["product"]["official_url"] = "https://attacker.invalid/device"

    with pytest.raises(ValueError, match="integrity"):
        validate_source_pack(pack)


def test_raw_independent_metadata_cannot_self_corroborate(trust_key_dir):
    # seal_source_pack legitimately signs the enclosing pack.  The unsigned
    # independent capture still cannot satisfy corroboration.
    pack = seal_source_pack(
        _corroborated_pack(include_independent_attestation=False)
    )

    assert pack["source_pack_contract"]["readiness"] == "limited"
    assert pack["source_pack_contract"]["mandatory_fact_assurance"][
        "key_features"
    ]["state"] == "unverified"
    validate_source_pack(pack)


def test_mutated_artifact_attestation_fails_inside_newly_signed_pack(
    trust_key_dir,
):
    raw = _corroborated_pack()
    independent = next(
        item
        for item in raw["all_artifacts"].values()
        if item["source_relationship"] == "third_party"
    )
    # An attacker with access to pack JSON may alter metadata and invoke the
    # public sealer, but cannot mint a capture attestation without the key.
    independent["source_class"] = "independent_lab"

    resealed = seal_source_pack(raw)

    assert resealed["source_pack_contract"]["readiness"] == "limited"
    validate_source_pack(resealed)


def test_wrong_local_key_rejects_pack_but_pinned_external_key_verifies(
    tmp_path,
    monkeypatch,
):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    monkeypatch.setenv("SOURCE_INTELLIGENCE_DATA_DIR", str(first_dir))
    pack = seal_source_pack(_corroborated_pack())
    trusted_public_key = pack["source_pack_contract"]["trust_identity"][
        "public_key"
    ]

    monkeypatch.setenv("SOURCE_INTELLIGENCE_DATA_DIR", str(second_dir))
    signing_identity()  # Create a different pinned local identity.

    with pytest.raises(ValueError, match="trust signature"):
        validate_source_pack(pack)
    assert verify_source_pack_signature(pack, trusted_public_key)


def test_legacy_v2_migration_produces_valid_signed_v3(trust_key_dir):
    current = seal_source_pack(_corroborated_pack())
    legacy = copy.deepcopy(current)
    legacy["source_pack_contract"] = {
        "name": CONTRACT_NAME,
        "version": 2,
        "generated_at": "2026-07-29T00:00:00+00:00",
        "readiness": "complete",
        "readiness_reasons": [],
        "source_of_truth": "source_intelligence",
        "generation_system": "MBK Master Content Generation System v3.8",
    }
    legacy["source_pack_contract"]["sha256"] = hashlib.sha256(
        _canonical_payload(legacy)
    ).hexdigest()

    migrated = migrate_source_pack(legacy)

    assert migrated["source_pack_contract"]["version"] == 3
    assert migrated["source_pack_contract"]["signature"]["algorithm"] == (
        "Ed25519"
    )
    validate_source_pack(migrated)


def test_evidence_lake_attests_verified_bytes_at_capture_boundary(
    trust_key_dir,
    tmp_path,
):
    lake = EvidenceLake(
        db_path=str(tmp_path / "evidence.db"),
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    artifact = Artifact(
        source_url="https://news.example/captured",
        final_url="https://news.example/captured",
        source_class=SourceClass.NEWS_MEDIA,
        source_relationship=SourceRelationship.THIRD_PARTY,
        captured_at="2026-07-30T12:00:00+00:00",
        status_code=200,
        tls_verified=True,
    )
    content = b"captured independent source bytes"

    artifact_id = lake.store(artifact, content)
    stored = lake.get(artifact_id)
    exported = stored.to_attestation_record()

    assert artifact_id != hashlib.sha256(content).hexdigest()
    assert stored.content_hash == hashlib.sha256(content).hexdigest()
    assert verify_artifact_attestation(exported)
    exported["source_class"] = "independent_lab"
    assert not verify_artifact_attestation(exported)


def test_evidence_lake_rejects_claimed_hash_that_does_not_match_bytes(
    trust_key_dir,
    tmp_path,
):
    lake = EvidenceLake(db_path=str(tmp_path / "evidence.db"))
    artifact = Artifact(
        artifact_id="f" * 64,
        content_hash="f" * 64,
        source_url="https://news.example/captured",
        final_url="https://news.example/captured",
        source_class=SourceClass.NEWS_MEDIA,
        source_relationship=SourceRelationship.THIRD_PARTY,
        captured_at="2026-07-30T12:00:00+00:00",
        status_code=200,
    )

    with pytest.raises(ValueError, match="content_hash"):
        lake.store(artifact, b"different bytes")


def test_claim_evidence_attestation_requires_excerpt_in_verified_bytes(
    trust_key_dir,
    tmp_path,
):
    from database import ProductDatabase

    db_path = str(tmp_path / "claim-evidence.db")
    ProductDatabase(db_path=db_path)
    lake = EvidenceLake(db_path=db_path)
    artifact = Artifact(
        source_url="https://seller.example/device",
        final_url="https://seller.example/device",
        source_class=SourceClass.OFFICIAL_VENDOR,
        source_relationship=SourceRelationship.FIRST_PARTY,
        captured_at="2026-07-30T12:00:00+00:00",
        status_code=200,
        tls_verified=True,
        offering_id="claim-proof-offering",
        job_id="claim-proof-job",
        acquisition_phase="ACQUIRE",
        capture_route="official_page",
    )
    artifact_id = lake.store(
        artifact,
        b"Device includes a washable pre-filter.",
    )
    ledger = ClaimsLedger(db_path=db_path)

    with pytest.raises(ValueError, match="not present"):
        ledger.add_claim(
            Claim(
                offering_id="claim-proof-offering",
                claim_text="Device cures asthma",
                claim_type=ClaimType.FEATURE,
                source_artifact_id=artifact_id,
                exact_excerpt="Device cures asthma",
                metadata={
                    "excerpt_is_literal": True,
                    "fact_key": "key_features",
                },
            ),
            attest_literal_evidence=True,
        )

    with pytest.raises(ValueError, match="not contained"):
        ledger.add_claim(
            Claim(
                offering_id="claim-proof-offering",
                claim_text="Device cures asthma",
                claim_type=ClaimType.FEATURE,
                source_artifact_id=artifact_id,
                exact_excerpt="Device includes a washable pre-filter.",
                metadata={
                    "excerpt_is_literal": True,
                    "fact_key": "key_features",
                },
            ),
            attest_literal_evidence=True,
        )

    claim_id = ledger.add_claim(
        Claim(
            offering_id="claim-proof-offering",
            claim_text="Device includes a washable pre-filter",
            claim_type=ClaimType.FEATURE,
            source_artifact_id=artifact_id,
            exact_excerpt="Device includes a washable pre-filter.",
            metadata={
                "excerpt_is_literal": True,
                "fact_key": "key_features",
            },
        ),
        attest_literal_evidence=True,
    )
    stored = ledger.get_claim(claim_id)
    assert stored.metadata["evidence_attestation"]["signature"]["key_id"] == (
        public_key_fingerprint()
    )


def test_identical_bytes_preserve_distinct_capture_provenance(
    trust_key_dir,
    tmp_path,
):
    lake = EvidenceLake(db_path=str(tmp_path / "captures.db"))
    content = b"identical evidence bytes"
    first = Artifact(
        source_url="https://seller-a.example/source",
        final_url="https://seller-a.example/source",
        source_class=SourceClass.OFFICIAL_VENDOR,
        source_relationship=SourceRelationship.FIRST_PARTY,
        captured_at="2026-07-30T12:00:00+00:00",
        status_code=200,
        tls_verified=True,
        offering_id="offering-a",
        job_id="job-a",
        capture_route="official_page",
    )
    second = Artifact(
        source_url="https://seller-b.example/source",
        final_url="https://seller-b.example/source",
        source_class=SourceClass.OFFICIAL_VENDOR,
        source_relationship=SourceRelationship.FIRST_PARTY,
        captured_at="2026-07-30T12:01:00+00:00",
        status_code=200,
        tls_verified=True,
        offering_id="offering-b",
        job_id="job-b",
        capture_route="official_page",
    )

    first_id = lake.store(first, content)
    second_id = lake.store(second, content)

    assert first_id != second_id
    assert lake.get(first_id).offering_id == "offering-a"
    assert lake.get(second_id).offering_id == "offering-b"
    assert lake.get(first_id).content_hash == lake.get(second_id).content_hash


def test_large_evidence_blob_is_hash_checked_on_every_read(
    trust_key_dir,
    tmp_path,
):
    artifacts_dir = tmp_path / "artifacts"
    lake = EvidenceLake(
        db_path=str(tmp_path / "large-evidence.db"),
        artifacts_dir=str(artifacts_dir),
    )
    lake.INLINE_THRESHOLD = 1
    artifact = Artifact(
        source_url="https://seller.example/large",
        final_url="https://seller.example/large",
        source_class=SourceClass.OFFICIAL_VENDOR,
        source_relationship=SourceRelationship.FIRST_PARTY,
        captured_at="2026-07-30T12:00:00+00:00",
        status_code=200,
        tls_verified=True,
        offering_id="large-offering",
        job_id="large-job",
        capture_route="official_page",
    )
    artifact_id = lake.store(artifact, b"immutable evidence")
    stored = lake.get(artifact_id)
    blob_path = artifacts_dir / stored.content_path
    os.chmod(blob_path, 0o600)
    blob_path.write_bytes(b"forged evidence")

    with pytest.raises(EvidenceIntegrityError, match="SHA-256"):
        lake.get_content(artifact_id)
