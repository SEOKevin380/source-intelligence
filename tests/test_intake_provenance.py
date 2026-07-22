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
    assert "not accepted product facts or substantiation" in pack["doc_text"]
    db.close()
