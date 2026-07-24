from article_provenance import build_article_claim_ledger, extract_sealed_pack


def test_extract_pack_and_map_attributed_claim():
    pack = {
        "source_pack_contract": {"sha256": "packhash"},
        "publication_claims": {
            "specification": [{
                "claim_id": "c1",
                "text": "The stated voltage range is 90V to 250V",
                "artifact_id": "a1",
                "source_class": "official_vendor",
                "publication_treatment": "seller_attribution_required",
            }]
        },
        "excluded_publication_claims": [],
    }
    source = (
        "context\n═══ SEALED CURRENT-PRODUCT SOURCE PACK — FACTS ONLY ═══\n"
        + __import__("json").dumps(pack)
    )
    assert extract_sealed_pack(source) == pack
    ledger = build_article_claim_ledger(
        pack,
        "<p>Seller materials state that the voltage range is 90V to 250V.</p>",
    )
    assert ledger["used_claim_count"] == 1
    assert ledger["mappings"][0]["claims"][0]["publication_treatment"] == (
        "seller_attribution_required"
    )
    assert ledger["passed"] is True


def test_unattributed_mapped_seller_claim_fails_provenance():
    pack = {
        "source_pack_contract": {"sha256": "packhash"},
        "publication_claims": {
            "feature": [{
                "claim_id": "c1",
                "text": "The device filters dirty electricity",
                "artifact_id": "a1",
                "source_class": "official_vendor",
                "publication_treatment": "seller_attribution_required",
            }]
        },
        "excluded_publication_claims": [],
    }
    ledger = build_article_claim_ledger(
        pack, "<p>The device filters dirty electricity.</p>"
    )
    assert ledger["passed"] is False
    assert ledger["attribution_violations"]


def test_short_exact_device_claim_maps_with_required_attribution():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "short-feature",
                "text": "Voltage stabilization",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>Seller materials describe voltage stabilization as a listed "
        "product feature.</p>",
    )
    assert ledger["used_claim_count"] == 1
    assert ledger["mapped_sentence_count"] == 1
    assert not ledger["attribution_violations"]


def test_seller_calls_phrase_satisfies_required_attribution():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "seller-phrase",
                "text": "Dirty EMF electricity filtering",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        '<p>The stated function relates to reducing what the seller calls '
        '"dirty EMF electricity."</p>',
    )
    assert ledger["used_claim_count"] == 1
    assert ledger["mapped_sentence_count"] == 1
    assert not ledger["attribution_violations"]
