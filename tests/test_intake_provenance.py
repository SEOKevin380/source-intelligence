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
    assert "NO-PAUSE DELIVERY RULE (GOVERNING RULE)" in prompt
    assert "Editorial review happens AFTER the complete draft" in prompt


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
