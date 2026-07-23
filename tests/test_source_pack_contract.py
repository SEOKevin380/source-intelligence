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
