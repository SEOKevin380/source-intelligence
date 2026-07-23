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
