from article_provenance import (
    build_article_claim_ledger,
    ensure_structured_contact_block,
    extract_sealed_pack,
    prune_unattributed_claim_blocks,
)


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


def test_long_seller_subject_governs_later_reporting_verb():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "long-seller-subject",
                "text": "Voltage stabilization and surge reduction",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>Seller headings such as “Stabilizes the Power” and “Reduces "
        "Surges,” which appear beside other promotional descriptions on the "
        "offer page, describe voltage stabilization and surge reduction.</p>",
    )
    assert ledger["used_claim_count"] == 1
    assert not ledger["attribution_violations"]


def test_heading_is_not_joined_to_unattributed_pricing_paragraph():
    pack = {
        "publication_claims": {
            "pricing": [{
                "claim_id": "price",
                "text": "Single Unit $49.99",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<h2>Seller Pricing</h2><p>Single Unit: $49.99.</p>",
    )
    assert ledger["used_claim_count"] == 1
    assert ledger["attribution_violations"]


def test_according_to_seller_is_valid_seller_attribution():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "seller-attribution",
                "text": "Voltage stabilization",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>According to the seller, voltage stabilization is a listed "
        "product feature.</p>",
    )
    assert ledger["used_claim_count"] == 1
    assert not ledger["attribution_violations"]


def test_paragraph_opening_attribution_governs_related_following_sentence():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "digital-content",
                "text": "Digital readings and reports delivered after purchase",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>According to the seller, paid access adds digital content. "
        "Digital readings and reports are delivered after purchase.</p>",
    )
    assert ledger["used_claim_count"] == 1
    assert not ledger["attribution_violations"]


def test_later_attribution_does_not_flow_backward_to_earlier_claim():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "digital-content",
                "text": "Digital readings and reports delivered after purchase",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>Digital readings and reports are delivered after purchase. "
        "The seller describes these as personalized materials.</p>",
    )
    assert ledger["used_claim_count"] == 1
    assert ledger["attribution_violations"]


def test_attribution_scope_does_not_cross_html_blocks():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "digital-content",
                "text": "Digital readings and reports delivered after purchase",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>According to the seller, paid access adds digital content.</p>"
        "<p>Digital readings and reports are delivered after purchase.</p>",
    )
    assert ledger["used_claim_count"] == 1
    assert ledger["attribution_violations"]


def test_seller_attributed_colon_introduction_governs_direct_list_items():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "audio-content",
                "text": "Audio content",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>According to the seller, paid access includes:</p>"
        "<ul><li>Audio content — recordings included with paid access</li></ul>",
    )
    assert ledger["used_claim_count"] == 1
    assert not ledger["attribution_violations"]


def test_list_attribution_does_not_flow_from_unattributed_introduction():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "audio-content",
                "text": "Audio content",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>Paid access includes:</p>"
        "<ul><li>Audio content — recordings included with paid access</li></ul>",
    )
    assert ledger["used_claim_count"] == 1
    assert ledger["attribution_violations"]


def test_prune_unattributed_claim_blocks_removes_whole_unsafe_paragraph():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "digital-content",
                "text": "Digital readings delivered after purchase",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    article = (
        "<p>According to the seller, digital readings are delivered after "
        "purchase.</p>"
        "<p>Digital readings are delivered after purchase and guarantee "
        "accurate predictions.</p>"
        "<p>Independent review is still required.</p>"
    )
    pruned, report = prune_unattributed_claim_blocks(pack, article)
    assert report["changed"] is True
    assert report["removed_block_count"] == 1
    assert "guarantee accurate predictions" not in pruned
    assert "According to the seller" in pruned
    assert "Independent review is still required." in pruned
    assert build_article_claim_ledger(pack, pruned)["passed"] is True


def test_seller_offers_and_confirms_are_valid_reporting_verbs():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "free-reading",
                "text": "Free initial reading no card required",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    for article in (
        "<p>The seller offers a free initial reading with no card required.</p>",
        "<p>The seller confirms a free initial reading needs no card.</p>",
    ):
        ledger = build_article_claim_ledger(pack, article)
        assert ledger["used_claim_count"] == 1
        assert not ledger["attribution_violations"]


def test_product_question_is_not_treated_as_an_asserted_claim():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "fortune-numbers",
                "text": "Fortune Numbers",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack, "<p>Will the Fortune Numbers predict my future?</p>"
    )
    assert ledger["used_claim_count"] == 0
    assert not ledger["attribution_violations"]


def test_duplicate_claim_matches_create_one_attribution_edit_per_sentence():
    pack = {
        "publication_claims": {
            "feature": [
                {
                    "claim_id": "free-game-1",
                    "text": "Free scratch game",
                    "publication_treatment": "seller_attribution_required",
                },
                {
                    "claim_id": "free-game-2",
                    "text": "Free initial scratch game",
                    "publication_treatment": "seller_attribution_required",
                },
            ]
        }
    }
    ledger = build_article_claim_ledger(
        pack, "<p>The free initial scratch game is available.</p>"
    )
    assert ledger["used_claim_count"] == 2
    assert len(ledger["attribution_violations"]) == 1


def test_mapped_claim_cannot_smuggle_an_extra_number():
    pack = {
        "publication_claims": {
            "pricing": [{
                "claim_id": "price",
                "text": "Single Unit $49.99",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>The seller lists one unit at $49.99 and promises 50% savings.</p>",
    )
    assert ledger["used_claim_count"] == 0


def test_seller_is_clear_is_valid_local_attribution():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "limits",
                "text": "Fortune Numbers are not a prediction",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>The seller is clear: Fortune Numbers are not a prediction.</p>",
    )
    assert ledger["used_claim_count"] == 1
    assert ledger["attribution_violations"] == []


def test_generic_support_copy_does_not_map_to_clickbank_claim():
    pack = {
        "publication_claims": {
            "feature": [{
                "claim_id": "clickbank-support",
                "text": "ClickBank customer support for billing/refund issues",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>This separation of support is common in digital product sales: "
        "the vendor handles content issues while a payment processor handles "
        "billing matters.</p>",
    )
    assert ledger["used_claim_count"] == 0
    assert ledger["attribution_violations"] == []


def test_email_at_product_domain_does_not_map_to_website_claim():
    pack = {
        "publication_claims": {
            "company_info": [{
                "claim_id": "website",
                "text": "website: https://scratch-fortune.com/",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>Email: support@scratch-fortune.com</p>",
    )
    assert ledger["used_claim_count"] == 0
    assert ledger["attribution_violations"] == []


def test_empty_dictionary_label_is_not_a_publication_claim():
    pack = {
        "publication_claims": {
            "company_info": [{
                "claim_id": "empty-phone",
                "text": "phone:",
                "publication_treatment": "seller_attribution_required",
            }]
        }
    }
    ledger = build_article_claim_ledger(
        pack,
        "<p>The direct support phone numbers are appropriate.</p>",
    )
    assert ledger["publication_claim_count"] == 0
    assert ledger["attribution_violations"] == []


def test_split_support_headings_form_one_valid_contact_block():
    pack = {
        "product": {
            "product_name": "Scratch Off Fortune",
            "official_url": "https://scratch-fortune.com/",
        },
        "intake_manifest": {
            "contact_information": {
                "support_email": "support@scratch-fortune.com",
                "support_phone_us": "1-800-390-6035",
                "support_phone_international": "1-208-345-4245",
                "order_support_provider": "ClickBank",
                "order_support_url": "https://www.clkbank.com/#!/?",
            },
            "refund_terms": "60-Day Refund Guarantee",
        },
        "publication_claims": {},
    }
    article = """
    <h2>The Bottom Line</h2><p>Reader conclusion.</p>
    <h3>Product Support</h3>
    <ul>
      <li>Email: support@scratch-fortune.com</li>
      <li>United States Phone: 1-800-390-6035</li>
      <li>International Phone: 1-208-345-4245</li>
      <li>Official Website:
        <a href="https://scratch-fortune.com/">Current offer</a>
      </li>
    </ul>
    <h3>Order Support and Billing</h3>
    <ul>
      <li>Provider: ClickBank</li>
      <li><a href="https://www.clkbank.com/#!/?">ClickBank Support</a></li>
    </ul>
    <h3>Refund Terms</h3>
    <p>According to the seller, purchases carry a 60-Day Refund Guarantee.</p>
    """
    ledger = build_article_claim_ledger(pack, article)
    assert ledger["coverage_violations"] == []


def test_structured_contact_renderer_is_exact_and_idempotent():
    pack = {
        "product": {
            "product_name": "Scratch Off Fortune",
            "official_url": "https://scratch-fortune.com/",
        },
        "intake_manifest": {
            "contact_information": {
                "support_email": "support@scratch-fortune.com",
                "support_phone_us": "1-800-390-6035",
                "support_phone_international": "1-208-345-4245",
                "order_support_provider": "ClickBank",
                "order_support_url": "https://www.clkbank.com/#!/?",
            },
            "refund_terms": "60-Day Refund Guarantee",
        },
        "publication_claims": {},
    }
    initial = (
        "<h2>Product Support</h2><p>Email omitted.</p>"
        "<h3>Order Support and Billing</h3><p>Details omitted.</p>"
    )
    rendered, report = ensure_structured_contact_block(pack, initial)
    rerendered, second_report = ensure_structured_contact_block(pack, rendered)

    assert report["changed"] is True
    assert report["replaced_existing_block"] is True
    assert second_report["replaced_existing_block"] is True
    assert rerendered == rendered
    for exact in (
        "support@scratch-fortune.com",
        "1-800-390-6035",
        "1-208-345-4245",
        "ClickBank",
        "https://www.clkbank.com/#!/?",
        "https://scratch-fortune.com/",
        "60-Day Refund Guarantee",
    ):
        assert exact in rendered
    assert "Email omitted" not in rendered
    assert build_article_claim_ledger(pack, rendered)[
        "coverage_violations"
    ] == []
