"""Trust-boundary tests for submitted source material."""

from unittest.mock import MagicMock, patch


def test_identify_does_not_blend_vsl_into_product_facts():
    from stage_handlers import handle_identify
    from workflow import Job

    job = Job.create(
        url="https://vendor.example/product",
        product_name="Example Supplement",
        vsl_url="https://vendor.example/vsl",
    )
    extracted = {
        "product_name": "Example Supplement",
        "product_type": "supplement",
        "supplement_facts": {"ingredients": []},
    }
    with patch("research_product.phase1_extract_product",
               return_value=extracted) as phase1, \
         patch("entities.Offering.save"):
        handle_identify(job)

    assert phase1.call_args.kwargs["vsl_url"] is None


def test_vsl_is_separate_marketing_source_and_does_not_fail_acquisition():
    from stage_handlers import handle_acquire
    from workflow import Job, PipelineStage

    job = Job.create(
        url="https://vendor.example/product",
        product_name="Example Supplement",
        vsl_url="https://vendor.example/vsl",
    )
    job.offering_id = "off-vsl-boundary"
    job.set_stage_result(PipelineStage.IDENTIFY, {"product_data": {}})

    fake_acquirer = MagicMock()
    fake_acquirer.fetch_official_page.return_value = ("product-artifact", "product")
    fake_acquirer.fetch_with_browser.return_value = (
        "vsl-artifact",
        "This dramatic presentation says the product changes everything.",
    )
    with patch("acquire.Acquirer", return_value=fake_acquirer), \
         patch("evidence.EvidenceLake"):
        result = handle_acquire(job)

    vsl = next(s for s in result["source_manifest"] if s["type"] == "vsl")
    assert vsl["status"] == "captured"
    assert vsl["capture_scope"] == "rendered_page_html"
    assert vsl["spoken_transcript_status"] == "not_confirmed"
    assert result["intake_complete"] is True


def test_primary_offer_url_is_automatically_treated_as_marketing_evidence():
    from stage_handlers import handle_acquire, _looks_like_marketing_offer
    from workflow import Job, PipelineStage

    url = "https://stockinvestor.com/offer/fs-americas-1-stock-vsl/?step=1"
    assert _looks_like_marketing_offer(url) is True
    assert _looks_like_marketing_offer("https://example.com/about") is False

    job = Job.create(url=url, product_name="Forecasts & Strategies")
    job.offering_id = "off-financial-vsl"
    job.set_stage_result(PipelineStage.IDENTIFY, {"product_data": {}})
    fake_acquirer = MagicMock()
    fake_acquirer.fetch_with_browser.return_value = (
        "offer-artifact", "Rendered stock research offer page text " * 10,
    )
    with patch("acquire.Acquirer", return_value=fake_acquirer), \
         patch("evidence.EvidenceLake"):
        result = handle_acquire(job)

    assert result["marketing_artifact_id"] == "offer-artifact"
    assert fake_acquirer.fetch_with_browser.called
    assert not fake_acquirer.fetch_official_page.called
    vsl = next(s for s in result["source_manifest"] if s["type"] == "vsl")
    assert vsl["auto_detected_from_primary_url"] is True


def test_channel_display_names_reach_compliance_as_engine_keys():
    from stage_handlers import _normalize_publishing_channel

    assert _normalize_publishing_channel("Accesswire") == "accesswire"
    assert _normalize_publishing_channel("Globe Newswire") == "globe"
    assert _normalize_publishing_channel("Barchart Advertorial") == "barchart"
    assert _normalize_publishing_channel("Domain Site") == "wordpress"


def test_aggressive_vsl_language_is_not_product_compliance_corpus():
    from compliance import ComplianceState
    from stage_handlers import handle_comply
    from workflow import Job, PipelineStage

    extreme_phrase = "GUARANTEED MIRACLE CURE IN SECONDS"
    job = Job.create(
        url="https://vendor.example/product",
        product_name="Example Supplement",
        vsl_url="https://vendor.example/vsl",
        vsl_marketing_copy=extreme_phrase,
        channel="Accesswire",
    )
    job.set_stage_result(PipelineStage.IDENTIFY, {
        "offering_type": "supplement",
        "product_data": {
            "description": "A dietary supplement.",
            "claims": [],
        },
    })
    report = MagicMock(
        overall_state=ComplianceState.CLEARED,
        blocks=[], reviews=[], warnings=[], results=[],
    )
    report.summary.return_value = "cleared"
    engine = MagicMock()
    engine.evaluate.return_value = report

    with patch("compliance.ComplianceEngine", return_value=engine):
        handle_comply(job)

    corpus, _offering_type, channel, _jurisdiction = engine.evaluate.call_args.args
    assert extreme_phrase not in corpus
    assert channel == "accesswire"


def test_accesswire_r12_adapter_reports_rewrite_term_not_missing_field_fail():
    from compliance import ComplianceState
    from stage_handlers import handle_comply
    from workflow import Job, PipelineStage

    job = Job.create(
        url="https://vendor.example/product",
        product_name="T-Max African Aphrodisiac",
        channel="Accesswire",
    )
    job.set_stage_result(PipelineStage.IDENTIFY, {
        "offering_type": "supplement",
        "product_data": {
            "product_name": "T-Max African Aphrodisiac",
            "product_type": "supplement",
            "description": "A dietary supplement.",
            "claims": [],
        },
    })
    report = MagicMock(
        overall_state=ComplianceState.CLEARED,
        blocks=0, reviews=0, warnings=0, results=[],
    )
    report.summary.return_value = "cleared"
    engine = MagicMock()
    engine.evaluate.return_value = report

    with patch("compliance.ComplianceEngine", return_value=engine):
        result = handle_comply(job)

    check = result["compliance"]["accesswire_blocklist_check"]
    assert check["passes"] is False
    assert check["action"] == "rewrite"
    assert "aphrodisiac" in check["flagged_terms"]
    assert check["blocked_claims"][0]["safe_alternatives"]["aphrodisiac"]


def test_label_artifact_preserves_original_source_url(tmp_path):
    from acquire import Acquirer
    from evidence import EvidenceLake

    db_path = str(tmp_path / "provenance.db")
    from database import ProductDatabase
    db = ProductDatabase(db_path=db_path)
    lake = EvidenceLake(db_path=db_path)
    acquirer = Acquirer(lake, offering_id="off-label", job_id="job-label")
    source_url = "https://cdn.example.com/supplement-facts.png"
    artifact_id = acquirer.store_label_image(
        b"not-a-real-png-but-valid-evidence-bytes",
        source_description="downloaded intake label",
        source_url=source_url,
    )

    assert lake.get(artifact_id).source_url == source_url
    db.close()


def test_source_pack_contains_complete_intake_and_uncited_artifacts(tmp_path):
    from acquire import Acquirer
    from database import ProductDatabase
    from evidence import EvidenceLake
    from stage_handlers import handle_source_pack
    from workflow import Job, PipelineStage

    db_path = str(tmp_path / "source-pack.db")
    db = ProductDatabase(db_path=db_path)
    lake = EvidenceLake(db_path=db_path)
    job = Job.create(
        url="https://vendor.example/product",
        product_name="Example Supplement",
        vsl_url="https://vendor.example/vsl",
        label_source_url="https://cdn.example/label.png",
        affiliate_link="https://publisher.example/offer",
        channel="Accesswire",
        operator_notes="Capture the advertising, but verify the facts.",
    )
    job.offering_id = "off-source-pack"
    artifact_id = Acquirer(
        lake, offering_id=job.offering_id, job_id=job.job_id
    ).store_structured_data(
        {"marketing_context": "captured but not substantiated"},
        source_url="https://vendor.example/vsl",
        source_name="vsl_context",
        phase="ACQUIRE_VSL",
    )
    job.set_stage_result(PipelineStage.IDENTIFY, {
        "product_data": {"product_name": "Example Supplement"},
        "offering_type": "",
    })
    job.set_stage_result(PipelineStage.ACQUIRE, {
        "artifacts": [{"artifact_id": artifact_id, "type": "vsl_page"}],
        "intake_complete": True,
        "source_manifest": [{
            "type": "vsl",
            "url": "https://vendor.example/vsl",
            "status": "captured",
            "artifact_id": artifact_id,
            "spoken_transcript_status": "not_confirmed",
        }],
    })

    with patch("config.DB_PATH", db_path):
        pack = handle_source_pack(job)

    data = pack["full_data"]
    assert data["intake_manifest"]["vsl_url"].endswith("/vsl")
    assert data["intake_manifest"]["publishing_channel"] == "Accesswire"
    assert data["intake_manifest_hash"]
    assert artifact_id in data["all_artifacts"]
    assert "complete spoken-word transcript has not been confirmed" in pack["doc_text"]
    assert "strongest compliant client-positive positioning" in pack["doc_text"]
    assert "SOURCE-OF-RECORD RULE" in pack["doc_text"]
    db.close()


def test_generation_prompt_enforces_client_positive_compliance_boundary():
    from prompt_builders import build_l6_press_release_prompt

    prompt = build_l6_press_release_prompt(
        {
            "product": {"product_name": "Example", "official_url": "https://example.com"},
            "compliance": {},
        },
        {"platform": "Accesswire", "affiliate_link": "TRAFFIC-FIRST"},
    )

    assert "CLIENT ADVOCACY STANDARD (GOVERNING RULE)" in prompt
    assert "compliance boundary is the target" in prompt
    assert "Assume the client and brand are acting in good faith" in prompt
    assert "SOURCE-OF-RECORD STANDARD (GOVERNING RULE)" in prompt
    assert "exclusive factual source for the draft" in prompt
    assert "EDITORIAL DELIVERY:" in prompt
    assert "Editorial review occurs after drafting" in prompt


def test_accesswire_r12_uses_neutral_approved_framing_and_never_pauses():
    from prompt_builders import build_l6_press_release_prompt

    prompt = build_l6_press_release_prompt(
        {
            "product": {
                "product_name": "T-Max African Aphrodisiac",
                "official_url": "https://example.com/t-max",
                "product_type": "supplement",
                "supplement_facts": {
                    "ingredients": [{"name": "Vitamin B12", "amount": "2500 mcg"}],
                },
            },
            "ingredient_research": {"Vitamin B12": {"studies": []}},
            "compliance": {
                "accesswire_blocklist_check": {
                    "passes": False,
                    "flagged_terms": ["aphrodisiac"],
                },
            },
        },
        {"platform": "Accesswire"},
    )

    assert "'aphrodisiac' → use 'men's vitality'" in prompt
    assert "desire-supporting" not in prompt
    assert "AUTHORIZED TO DRAFT NOW" in prompt
    assert "Deliver the complete draft now" in prompt
    assert "DELIVERABLE:" in prompt
    assert "Return the finished article only" in prompt
    assert "jailbreak" not in prompt.lower()
    assert "No publishable marketing claims were extracted" in prompt


def test_press_release_prompt_accepts_legacy_list_safety_shape():
    from prompt_builders import build_l6_press_release_prompt

    prompt = build_l6_press_release_prompt(
        {
            "product": {
                "product_name": "Legacy Product",
                "product_type": "supplement",
                "official_url": "https://example.com/product",
                "supplement_facts": {
                    "ingredients": [{"name": "Ingredient A", "amount": "10 mg"}],
                },
            },
            "ingredient_research": {"Ingredient A": {"studies": []}},
            "safety": {
                "Ingredient A": [
                    {"severity": "Moderate", "drug_class": "Example drugs",
                     "interaction": "May alter exposure"},
                    "Legacy safety note",
                ],
            },
            "compliance": {},
        },
        {"platform": "Accesswire"},
    )

    assert "May alter exposure" in prompt
    assert "Legacy safety note" in prompt


def test_press_release_prompt_degrades_invalid_legacy_sections_without_crash():
    from prompt_builders import build_l6_press_release_prompt

    prompt = build_l6_press_release_prompt(
        {
            "product": {
                "product_name": "Legacy Product",
                "official_url": "https://example.com/product",
                "supplement_facts": None,
                "pricing": "unknown",
                "claims": None,
                "refund_policy": [],
            },
            "ingredient_research": [],
            "safety": [],
            "compliance": None,
        },
        {"platform": "Accesswire"},
    )

    assert "SOURCE INTELLIGENCE" in prompt
    assert "DELIVERABLE:" in prompt


def test_financial_press_release_uses_financial_vertical_only():
    from prompt_builders import build_l6_press_release_prompt

    name = "Forecasts & Strategies America's #1 Stock | Jim Woods"
    prompt = build_l6_press_release_prompt(
        {
            "product": {
                "product_name": name,
                "official_url": "https://jimwoodsinvesting.stockinvestor.com/offer/stock-vsl/",
                "product_type": "financial",
                "category": "financial",
                "service_type": "investment research publication",
                "topics_covered": ["equity research"],
                "track_record_claims": [],
                "regulatory_registrations": [],
                "pricing": [],
                "claims": [],
            },
            "ingredient_research": {},
            "safety": {"reason": "Not required for financial"},
            "compliance": {
                "risk_level": "low",
                "barchart_compliance": {
                    "passes": False,
                    "review_flag": True,
                    "notes": "Manual editorial review required",
                },
            },
            "keywords": {
                "primary": [f"{name} supplement"],
                "safety_queries": [f"{name} side effects"],
            },
        },
        {"platform": "Barchart Advertorial", "ymyl_category": "Yes"},
    )

    assert "C1 — FINANCIAL SERVICE / PUBLICATION FACTS" in prompt
    assert "C7 — FINANCIAL CLAIM SUBSTANTIATION" in prompt
    assert "C6 — FINANCIAL DISCLOSURES / REGULATORY STATUS" in prompt
    assert "C19 — SUBSCRIPTION / ACCESS TERMS" in prompt
    assert "Barchart B1-B4 Overlay: AUTOMATIC COMPLIANT REWRITE" in prompt
    assert "investment newsletter due diligence" in prompt
    forbidden = [
        "C1 — SUPPLEMENT FACTS", "CLINICAL CITATIONS / RESEARCH",
        "DRUG INTERACTIONS [PUBMED DATA]", "SERVING SIZE / SUPPLY DURATION",
        "financial supplements", "FDA approved?", "side effects?",
        "buy " + name + " on Amazon",
        "Manual editorial review required",
    ]
    for text in forbidden:
        assert text.lower() not in prompt.lower()


def test_sparse_financial_vsl_becomes_safe_descriptive_assignment():
    from prompt_builders import build_l6_press_release_prompt

    prompt = build_l6_press_release_prompt(
        {
            "product": {
                "product_name": "Forecasts & Strategies | Jim Woods",
                "official_url": "https://example.com/financial-vsl",
                "product_type": "financial",
                "category": "financial",
                "pricing": [],
                "claims": [],
            },
            "source_manifest": [
                {"type": "vsl_page", "status": "captured",
                 "url": "https://example.com/financial-vsl"},
            ],
            "claims_by_type": {
                "manufacturer_claim": [
                    {
                        "text": "Projected return claim",
                        "review_status": "needs_verification",
                        "metadata": {
                            "search_intent": "investment research newsletter",
                            "topic": "performance",
                        },
                    },
                ],
            },
            "compliance": {},
            "safety": {},
            "ingredient_research": {},
        },
        {"platform": "Barchart Advertorial", "ymyl_category": "Yes"},
    )

    assert "Investment research/newsletter promotional presentation" in prompt
    assert "Promotional Assertions Captured: 1" in prompt
    assert "not independently substantiated performance facts" in prompt
    assert "do not urge a securities transaction" in prompt
    assert "informational review of the publication" in prompt
    assert "jailbreak" not in prompt.lower()
    assert "Fake Testimonial Hype" not in prompt


def test_every_nonclinical_vertical_excludes_supplement_template_leakage():
    from prompt_builders import build_l6_press_release_prompt

    verticals = {
        "device": {"key_features": ["Feature A"], "specifications": {"size": "compact"}},
        "info_product": {"whats_included": ["Guide"], "format": "digital"},
        "software": {"key_features": ["Dashboard"], "platform_support": ["Web"]},
        "service": {"service_description": "Consulting", "credentials": ["Verified credential"]},
        "program": {"program_contents": ["Module 1"], "delivery_method": "online"},
        "subscription": {"whats_included": ["Monthly issue"], "access_method": "email"},
        "professional": {"service_description": "Professional advice", "credentials": ["License"]},
        "gaming": {"product_description": "Lottery number analysis tool", "how_it_works": "Analyzes past drawings"},
        "collectible": {"item_description": "Commemorative collector coin", "materials": "gold-colored alloy"},
        "unknown": {"description": "New category offering", "key_features": ["Feature"]},
    }
    forbidden = [
        "supplement facts", "clinical citations / research", "drug interactions",
        "serving size / supply duration", "pubmed api", "fda approved?",
        "side effects?", " on amazon?", "best financial supplements",
    ]
    for offering_type, fields in verticals.items():
        product = {
            "product_name": f"Example {offering_type}",
            "official_url": "https://example.com/offering",
            "product_type": offering_type,
            "category": "general",
            "pricing": [],
            "claims": [],
            **fields,
        }
        prompt = build_l6_press_release_prompt(
            {"product": product, "compliance": {}, "safety": {},
             "ingredient_research": {}},
            {"platform": "Accesswire", "ymyl_category": "No"},
        )
        lowered = prompt.lower()
        for phrase in forbidden:
            assert phrase not in lowered, (offering_type, phrase)
        assert "DELIVERABLE:" in prompt


def test_verified_label_ocr_satisfies_strict_mandatory_gate(tmp_path):
    from claims import Claim, ClaimsLedger, ClaimType
    from database import ProductDatabase

    db_path = str(tmp_path / "ocr-gate.db")
    db = ProductDatabase(db_path=db_path)
    ledger = ClaimsLedger(db_path=db_path)
    common = {
        "offering_id": "off-ocr-gate",
        "source_artifact_id": "immutable-label-artifact",
        "source_class": "official_vendor",
        "confidence": 0.8,
        "extraction_method": "machine_ocr",
    }
    ledger.add_claim(Claim(
        claim_text="Horny Goat Weed Extract: 20 mg",
        claim_type=ClaimType.INGREDIENT_AMOUNT,
        metadata={
            "fact_key": "ingredients_with_amounts",
            "excerpt_is_literal": False,
            "image_ocr": True,
            "artifact_transcription_verified": True,
        },
        **common,
    ))
    ledger.add_claim(Claim(
        claim_text="Serving size: 1 chewable tablet",
        claim_type=ClaimType.SERVING_INFO,
        metadata={
            "fact_key": "serving_size",
            "excerpt_is_literal": False,
            "image_ocr": True,
            "artifact_transcription_verified": True,
        },
        **common,
    ))

    result = ledger.check_required_facts(
        "off-ocr-gate",
        ["ingredients_with_amounts", "serving_size"],
        strict=True,
    )
    assert result["missing"] == []
    assert result["covered"] == ["ingredients_with_amounts", "serving_size"]
    db.close()


def test_initial_label_ocr_flows_through_extract_and_clears_gate(tmp_path):
    import hashlib

    from claims import ClaimsLedger
    from database import ProductDatabase
    from net import FetchResult
    from stage_handlers import handle_acquire, handle_extract
    from workflow import Job, PipelineStage

    db_path = str(tmp_path / "initial-label.db")
    db = ProductDatabase(db_path=db_path)
    label_path = tmp_path / "tmx.png"
    label_path.write_bytes(b"representative-label-image-bytes")
    page = b"<html><body>T-Max official product page</body></html>"
    fetch = FetchResult(
        content=page,
        text=page.decode(),
        final_url="https://primalforce.net/product/t-max/",
        status_code=200,
        headers={"Content-Type": "text/html"},
        content_hash=hashlib.sha256(page).hexdigest(),
        content_length=len(page),
        tls_verified=True,
        error="",
    )
    job = Job.create(
        url="https://primalforce.net/product/t-max/",
        product_name="T-Max",
        label_image=str(label_path),
        label_source_url="https://s15066.pcdn.co/wp-content/uploads/2016/07/tmx.png",
    )
    job.offering_id = "off-tmax-initial-label"
    job.set_stage_result(PipelineStage.IDENTIFY, {
        "offering_type": "supplement",
        "product_data": {
            "product_name": "T-Max",
            "product_type": "supplement",
            "supplement_facts": {"ingredients": []},
        },
    })
    ocr = {
        "serving_size": "1 Chewable Tablet",
        "servings_per_container": "30",
        "ingredients": [
            {"name": "Vitamin B12", "amount": "2,500 mcg"},
            {"name": "Guarana Extract", "amount": "568.18 mg"},
        ],
    }

    with patch("config.DB_PATH", db_path), \
         patch("net.safe_fetch", return_value=fetch), \
         patch("research_product.extract_label_image", return_value=ocr):
        acquire_result = handle_acquire(job)
        job.set_stage_result(PipelineStage.ACQUIRE, acquire_result)
        handle_extract(job)

    strict = ClaimsLedger(db_path=db_path).check_required_facts(
        job.offering_id,
        ["ingredients_with_amounts", "serving_size"],
        strict=True,
    )
    assert strict["missing"] == []
    assert strict["needs_review"] == []
    label_claims = ClaimsLedger(db_path=db_path).get_claims(job.offering_id)
    assert all(c.metadata.get("source_of_record") for c in label_claims)
    assert all(
        c.metadata.get("authoritative_scope") == "printed_label_contents"
        for c in label_claims
    )
    db.close()


def test_label_vision_retries_empty_response_and_transcribes(tmp_path):
    import json

    from research_product import extract_label_image

    image_path = tmp_path / "label.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"label-data")
    transcription = json.dumps({
        "serving_size": "1 Chewable Tablet",
        "servings_per_container": "30",
        "ingredients": [
            {"name": "Vitamin B12", "amount": "2,500 mcg"},
            {"name": "Guarana Extract", "amount": "568.18 mg"},
        ],
    })

    with patch("research_product.call_claude",
               side_effect=["", transcription]) as vision:
        result = extract_label_image(str(image_path))

    assert vision.call_count == 2
    assert result["serving_size"] == "1 Chewable Tablet"
    assert result["servings_per_container"] == "30"
    assert len(result["ingredients"]) == 2
    assert result["_extraction_attempt"] == 2


def test_financial_identity_guard_corrects_supplement_misclassification():
    from stage_handlers import _apply_offering_type_guard
    from workflow import Job

    job = Job.create(
        url="https://jimwoodsinvesting.stockinvestor.com/offer/stock-vsl/",
        product_name="Forecasts & Strategies America's #1 Stock",
        vsl_url="https://jimwoodsinvesting.stockinvestor.com/offer/stock-vsl/",
    )
    result = _apply_offering_type_guard({
        "product_name": job.product_name,
        "product_type": "supplement",
        "category": "financial",
        "supplement_facts": {"ingredients": []},
    }, job)

    assert result["product_type"] == "financial"
    assert "stockinvestor_domain" in result["_type_classification"]["signals"]


def test_financial_words_do_not_override_a_physical_supplement():
    from stage_handlers import _apply_offering_type_guard
    from workflow import Job

    job = Job.create(
        url="https://example.com/market-support-capsules",
        product_name="Stock Market Stress Support Capsules",
    )
    result = _apply_offering_type_guard({
        "product_name": job.product_name,
        "product_type": "supplement",
        "supplement_facts": {
            "serving_size": "2 capsules",
            "ingredients": [{"name": "Magnesium", "amount": "100 mg"}],
        },
    }, job)

    assert result["product_type"] == "supplement"


def test_lottery_tool_identity_guard_routes_to_gaming():
    from stage_handlers import _apply_offering_type_guard
    from workflow import Job

    job = Job.create(
        url="https://getlottochamp.com/text.php",
        product_name="LottoChamp Lottery Number Analysis Tool",
    )
    result = _apply_offering_type_guard({
        "product_name": job.product_name,
        "product_type": "info_product",
        "supplement_facts": {"ingredients": []},
    }, job)

    assert result["product_type"] == "gaming"
    assert result["category"] == "Lottery Tools"


def test_commemorative_coin_identity_guard_routes_to_collectible():
    from stage_handlers import _apply_offering_type_guard
    from workflow import Job

    job = Job.create(
        url="https://www.themagastore.net/products/donald-trump-survivor-gold-coin",
        product_name="Donald Trump Survivor Gold Coin",
    )
    result = _apply_offering_type_guard({
        "product_name": job.product_name,
        "product_type": "unknown",
        "supplement_facts": {"ingredients": []},
    }, job)

    assert result["product_type"] == "collectible"
    assert result["category"] == "Collectibles & Memorabilia"


def test_power_saver_identity_guard_routes_to_device_when_page_is_thin():
    from stage_handlers import _apply_offering_type_guard
    from workflow import Job

    job = Job.create(
        url="https://buyecowatt.com/flow3/",
        product_name="EcoWatt Power Saver",
    )
    result = _apply_offering_type_guard({
        "product_name": job.product_name,
        "product_type": "unknown",
        "supplement_facts": {"ingredients": []},
    }, job)

    assert result["product_type"] == "device"
    assert result["category"] == "Consumer Electronics"


def test_new_commercial_vertical_prompts_generate_without_wrong_category_language():
    from prompt_builders import build_l6_press_release_prompt

    cases = {
        "gaming": {
            "product_description": "Lottery number analysis software",
            "how_it_works": "Uses historical drawing data",
        },
        "collectible": {
            "item_description": "Commemorative collector coin",
            "materials": "gold-colored alloy",
        },
    }
    for product_type, fields in cases.items():
        prompt = build_l6_press_release_prompt({
            "product": {
                "product_name": "Example Offering",
                "official_url": "https://example.com/offer",
                "product_type": product_type,
                "category": product_type,
                "pricing": [],
                "claims": [],
                **fields,
            },
            "compliance": {}, "safety": {}, "ingredient_research": {},
        }, {"platform": "Accesswire", "ymyl_category": "No"})

        lowered = prompt.lower()
        assert ("deliver a complete, publish-ready draft" in lowered
                or "deliver the complete draft now" in lowered)
        assert "supplement facts" not in lowered
        assert "pubmed api" not in lowered
        if product_type == "gaming":
            assert "do not promise wins" in lowered
        else:
            assert "describe gold, silver, rarity" in lowered
            assert "only when the supplied record establishes the exact fact" in lowered
