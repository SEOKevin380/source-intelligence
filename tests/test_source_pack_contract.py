import copy

import pytest

from source_pack_contract import seal_source_pack, validate_source_pack


def _pack(missing=None):
    return {
        "product": {
            "product_name": "Example Product",
            "official_url": "https://example.com/product",
            "product_type": "device",
        },
        "all_artifacts": {"art-1": {"source_url": "https://example.com/product"}},
        "source_manifest": [{"type": "official", "status": "captured"}],
        "required_facts": {"missing": missing or []},
    }


def test_complete_pack_is_sealed_and_validates():
    pack = seal_source_pack(_pack())
    assert pack["source_pack_contract"]["readiness"] == "complete"
    assert len(pack["source_pack_contract"]["sha256"]) == 64
    validate_source_pack(pack)


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
