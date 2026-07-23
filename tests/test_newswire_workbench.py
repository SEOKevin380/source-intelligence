import json
import zipfile
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from newswire_workbench.engine import WorkbenchEngine, _source_affiliate_link
from newswire_workbench.prompts import detect_vertical
from newswire_workbench.prompts import generation_prompt
from newswire_workbench.learning import deterministic_findings, partition_findings
from newswire_workbench.audit import audit_article, audit_system_contract
from newswire_workbench.wordpress import WordPressDraftPublisher
from newswire_workbench.formatting import (
    ensure_article_html,
    ensure_affiliate_links,
    normalize_master_html,
    repair_publication_gates,
    repair_source_grounding,
)
from newswire_workbench.routing import risk_tier, route_for
from source_pack_contract import seal_source_pack


def _three_literal_claims():
    return {
        "feature": [
            {
                "text": f"Literal product fact {number}",
                "artifact_id": "a1",
                "source_class": "official_vendor",
                "review_status": "unreviewed",
                "metadata": {"excerpt_is_literal": True},
            }
            for number in range(3)
        ]
    }


def _independent_approval(engine, project_id):
    p = engine.get(project_id)
    return {
        "verdict": "approved",
        "mandatory_count": 0,
        "source_accuracy": {"verified": 1, "checked": 1},
        "mandatory_edits": [],
        "recommended_edits": [],
        "approved_elements": ["Independent final artifact review passed"],
        "notes": [],
        "reviewed_article_hash": p["article_hash"],
    }


def test_vertical_detection_is_category_aware():
    assert detect_vertical("investment stock newsletter") == "financial"
    assert detect_vertical("commemorative gold-plated coin") == "collectible"
    assert detect_vertical("supplement facts serving size") == "health"


def test_generation_prompt_preserves_client_advocacy_without_invention():
    prompt = generation_prompt(
        "verified product source", "Barchart Advertorial", "device", ""
    )
    assert "client's strongest compliant advocate" in prompt
    assert "must not replace the article with a prosecution brief" in prompt
    assert "missing evidence" in prompt


def test_fenced_plain_text_is_converted_to_submission_html():
    raw = """```html
This is a paid advertorial.

Key Takeaway Summary

Readers should verify current terms.

What the Service Offers

The service publishes general market research.
```"""
    converted = ensure_article_html(raw)
    assert "```" not in converted
    assert "<p>This is a paid advertorial.</p>" in converted
    assert "<h2><strong>Key Takeaway Summary</strong></h2>" in converted
    assert "<h2><strong>What the Service Offers</strong></h2>" in converted
    assert deterministic_findings(
        converted, "Barchart Advertorial", "general_consumer"
    )[0]["id"] != "D17"


def test_zero_cost_auth_failure_does_not_consume_call_ceiling(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Retryable", "AccessNewsWire", "Official URL: https://example.com\nProduct: Device"
    )
    route = route_for("draft", "general")
    engine._record_llm_call(pid, "draft", route, status="failed", error="401 invalid key")
    # A rejected credential never reached paid generation and must remain retryable.
    engine._assert_call_budget(pid, "draft", route)
    usage = engine.usage_summary(pid)
    assert usage["calls"] == 0
    assert usage["attempts"] == 1


def test_project_and_immutable_audit(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "Official URL: https://example.com\nProduct: Device")
    project = engine.get(pid)
    assert project["stage"] == "source_ready"
    assert len(project["source_hash"]) == 64
    events = engine.events(pid)
    assert events[0]["event_type"] == "project_created"
    with engine._connect() as conn:
        with pytest.raises(Exception):
            conn.execute("UPDATE events SET event_type='tampered' WHERE project_id=?", (pid,))


def test_sealed_source_pack_handoff_is_validated_and_idempotent(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pack = seal_source_pack({
        "product": {"product_name": "Test Device", "official_url": "https://example.com"},
        "all_artifacts": [{"artifact_id": "a1"}],
        "claims_by_type": _three_literal_claims(),
        "required_facts": {"missing": []},
    })
    first = engine.create_project_from_pack(pack, "AccessNewsWire")
    second = engine.create_project_from_pack(pack, "AccessNewsWire")
    assert first == second
    project = engine.get(first)
    assert project["stage"] == "source_ready"
    assert (
        "AUTOMATION CONTEXT VERSION: "
        "serp-differentiation-depth-v17-durable-run-transaction"
        in project["source_text"]
    )
    assert "SEALED CURRENT-PRODUCT SOURCE PACK" in project["source_text"]
    assert "LOCKED GENERATION BLUEPRINT" in project["source_text"]
    assert "SEO strategy is complete" in project["source_text"]
    assert "Recommended headline:" in project["source_text"]
    assert "polished American English" in project["source_text"]
    assert any(
        e["event_type"] == "sealed_source_pack_imported"
        for e in engine.events(first)
    )


def test_reviewer_style_preference_cannot_block_approval(tmp_path):
    engine = WorkbenchEngine(tmp_path / "workbench")
    report = {
        "verdict": "not_approved",
        "mandatory_edits": [{
            "id": "M1",
            "category": "Headline style",
            "issue": "The title could have more natural cadence.",
            "exact_text": "Current title",
            "replacement": "Optional stronger title",
        }],
        "recommended_edits": [],
        "notes": [],
    }
    adjudicated = engine._remove_house_rule_conflicts(report)
    assert adjudicated["verdict"] == "approved"
    assert not adjudicated["mandatory_edits"]
    assert adjudicated["recommended_edits"][0]["issue"].startswith("The title")


def test_explicit_rebuild_creates_new_project_and_preserves_source(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pack = seal_source_pack({
        "product": {
            "product_name": "Test Device",
            "official_url": "https://example.com",
        },
        "all_artifacts": [{"artifact_id": "a1"}],
        "claims_by_type": _three_literal_claims(),
        "required_facts": {"missing": []},
    })
    first = engine.create_project_from_pack(pack, "AccessNewsWire")
    rebuilt = engine.create_project_from_pack(
        pack, "AccessNewsWire", force_new=True
    )
    assert rebuilt != first
    assert "EXPLICIT REBUILD RUN:" in engine.get(rebuilt)["source_text"]
    assert engine.latest_project_from_pack(
        pack,
        "AccessNewsWire",
        "serp-differentiation-depth-v17-durable-run-transaction",
    ) == rebuilt


def test_invalid_legacy_package_is_automatically_rebuilt(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pack = seal_source_pack({
        "product": {
            "product_name": "Test Device",
            "official_url": "https://example.com",
        },
        "all_artifacts": [{"artifact_id": "a1"}],
        "claims_by_type": _three_literal_claims(),
        "required_facts": {"missing": []},
    })
    legacy = engine.create_project_from_pack(pack, "Barchart Advertorial")
    engine._set_article(
        engine.get(legacy),
        (
                "<p><strong>Paid Advertorial:</strong> Compensation may be "
                "received.</p><p>Utilities only bill kilowatt-hours regardless. "
                "Utilities measure reactive power in every home.</p>"
        ),
        "package_ready",
        "legacy.html",
    )
    current = engine.get(legacy)
    approval = {
        "verdict": "approved",
        "mandatory_count": 0,
        "mandatory_edits": [],
        "recommended_edits": [],
        "approved_elements": [],
        "notes": [],
        "reviewed_article_hash": current["article_hash"],
    }
    engine._set_report(
        current, approval, "package_ready", "legacy-approval.json"
    )
    rebuilt = engine.create_project_from_pack(pack, "Barchart Advertorial")
    assert rebuilt != legacy
    assert engine.get(rebuilt)["stage"] == "source_ready"
    assert "EXPLICIT REBUILD RUN:" in engine.get(rebuilt)["source_text"]


def test_wordpress_draft_inheritance_rejects_cross_product_state(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    old = engine.create_project(
        "Forecasts & Strategies", "AccessNewsWire", "financial source"
    )
    new = engine.create_project(
        "EcoWatt Power Saver", "Barchart Advertorial", "device source"
    )
    with pytest.raises(ValueError, match="same product and platform"):
        engine.inherit_wordpress_draft(new, old)


def test_wordpress_draft_inheritance_requires_confirmed_post_id(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    old = engine.create_project(
        "EcoWatt Power Saver", "Barchart Advertorial", "old device source"
    )
    new = engine.create_project(
        "EcoWatt Power Saver", "Barchart Advertorial", "new device source"
    )
    with engine._connect() as conn:
        conn.execute(
            """INSERT INTO wordpress_drafts
            (project_id,site_url,post_id,article_hash,edit_url,updated_at)
            VALUES(?,?,?,?,?,?)""",
            (
                old,
                "https://publisher.example",
                912,
                "old-hash",
                "https://publisher.example/wp-admin/post.php?post=912&action=edit",
                "2026-07-23T00:00:00+00:00",
            ),
        )

    with pytest.raises(ValueError, match="explicitly confirmed post ID"):
        engine.inherit_wordpress_draft(new, old)
    assert engine.wordpress_draft(new) is None

    engine.inherit_wordpress_draft(new, old, confirmed_post_id=912)
    inherited = engine.wordpress_draft(new)
    assert inherited["post_id"] == 912
    assert inherited["article_hash"] == ""


def test_article_diagnostics_proves_html_contract(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Test", "AccessNewsWire",
        "AUTOMATION CONTEXT VERSION: test-v1\nfinancial source",
        "financial",
    )
    engine.import_manual_article(
        pid,
        "```html\nPaid Advertorial\n\nWhat It Is\n\nUseful details.\n```",
    )
    diagnostics = engine.article_diagnostics(pid)
    assert diagnostics["workflow_version"] == "test-v1"
    assert diagnostics["has_code_fence"] is False
    assert diagnostics["has_article_html"] is True


def test_barchart_device_depth_is_visible_without_arbitrary_word_count_block():
    article = (
        "<p>Paid Advertorial: A commission may be earned.</p>"
        "<h2><strong>What It Is</strong></h2>"
        + "<p>Useful source-grounded product detail.</p>" * 120
    )
    findings = deterministic_findings(
        article, "Barchart Advertorial", "device"
    )
    ids = {item["id"] for item in findings}
    assert "D18" in ids
    blockers, recommendations = partition_findings(findings)
    assert "D18" not in {item["id"] for item in blockers}
    assert "D18" in {item["id"] for item in recommendations}


def test_repetitive_caveat_stacking_is_client_advocacy_warning():
    article = (
        "<p>Paid Advertorial: Compensation may be received.</p>"
        "<h2><strong>Product Details</strong></h2>"
        + "<p>The claim is not independently verified.</p>" * 5
        + "<p><a href='https://example.com'><strong>Review details</strong></a></p>"
    )
    findings = deterministic_findings(
        article, "Barchart Advertorial", "device"
    )
    blockers, recommendations = partition_findings(findings)
    assert "D19" not in {item["id"] for item in blockers}
    assert "D19" in {item["id"] for item in recommendations}


def test_markdown_bold_residue_is_a_publication_blocker():
    article = (
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>"
        "<p>**What Buyers Should Know** before ordering.</p>"
    )
    ids = {
        item["id"] for item in deterministic_findings(
            article, "Barchart Advertorial", "device"
        )
    }
    assert "D17" in ids


def test_publication_repair_canonicalizes_markdown_embedded_in_html():
    article = (
        "```html\n"
        "<p>**Paid Advertorial:** Compensation may be received.</p>"
        "<p>## What Buyers Should Know</p>"
        "<p>[Review details](https://partner.example/offer)</p>"
        + "<p>Useful source-grounded product detail for the reader.</p>" * 120
        + "\n```"
    )
    repaired = repair_publication_gates(
        article,
        "Barchart Advertorial",
        "device",
        "https://partner.example/offer",
    )
    assert "**" not in repaired
    assert "## " not in repaired
    assert "```" not in repaired
    assert "[Review details](" not in repaired
    assert "<h2><strong>What Buyers Should Know</strong></h2>" in repaired
    assert 'href="https://partner.example/offer"' in repaired
    assert "D17" not in {
        item["id"] for item in deterministic_findings(
            repaired, "Barchart Advertorial", "device"
        )
    }


def test_alternative_dominant_device_copy_triggers_advocacy_gate():
    article = (
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>"
        "<h2><strong>Verified Alternatives</strong></h2>"
        "<p>Energy audit, smart thermostat, programmable thermostat, LED "
        "lighting, insulation, air sealing, smart power strip, whole-home "
        "surge protection, energy audit, smart thermostat, LED lighting, "
        "insulation, and air sealing are alternatives.</p>"
    )
    ids = {
        item["id"] for item in deterministic_findings(
            article, "Barchart Advertorial", "device"
        )
    }
    assert "D19" in ids


def test_distinct_limitations_do_not_false_positive_advocacy_gate():
    article = (
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>"
        "<h2><strong>Product Features and Setup</strong></h2>"
        "<p>The plug-and-play device is marketed for voltage stabilization, "
        "power factor correction, dirty electricity filtering, surge reduction, "
        "24/7 operation, and zero maintenance. A green indicator light shows "
        "operation. The current offer lists a single unit and bundle price.</p>"
        "<h2><strong>Best Fit and Material Limitations</strong></h2>"
        "<p>Independent testing is not available. Warranty terms are missing. "
        "Certification is not documented. Shipping timing is not verified. "
        "Those are separate buyer questions to confirm before ordering.</p>"
    )
    ids = {
        item["id"] for item in deterministic_findings(
            article, "Barchart Advertorial", "device"
        )
    }
    assert "D19" not in ids


def test_publication_repair_neutralizes_prosecutorial_device_headings():
    article = (
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>"
        "<h2>The Critical Issue: Billing</h2><p>Useful detail.</p>"
        "<h2>What Information Is Missing or Unverified</h2><p>Limits.</p>"
        "<h2>Verified Alternatives With Clear Documentation</h2><p>Context.</p>"
    )
    repaired = repair_publication_gates(
        article, "Barchart Advertorial", "device"
    )
    assert "The Critical Issue" not in repaired
    assert "Missing or Unverified" not in repaired
    assert "Verified Alternatives" not in repaired
    assert "What Buyers Should Understand" in repaired
    assert "Material Limitations and Questions to Verify" in repaired
    assert "How This Product Fits a Broader Buying Decision" in repaired


def test_offline_system_audit_owns_every_blocker_and_route():
    report = audit_system_contract("device")
    assert report["passed"] is True
    assert report["blocker_count"] == 8
    assert not report["missing_blocker_rationales"]
    assert not report["stale_blocker_rationales"]
    assert report["missing_gate_owners"] == []
    assert report["route_errors"] == []
    assert report["end_to_end_budget_valid"] is True
    assert report["execution_budget"]["required_call_path"] == [
        "draft", "compliance", "compliance_repair", "final_signoff"
    ]


def test_unsafe_environment_call_ceiling_cannot_starve_review(monkeypatch):
    monkeypatch.setenv("NEWSWIRE_MAX_RUN_CALLS", "1")
    report = audit_system_contract("device")
    assert report["passed"] is True
    assert report["execution_budget"]["calls"] == 4
    assert report["execution_budget"]["configured_overrides"]["calls"] == "1"
    assert report["execution_budget"]["hard_limits"] == {
        "paid_calls": 4,
        "seconds_per_provider_call": 90.0,
    }


def test_prior_cost_cannot_starve_required_exact_hash_review(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    draft = route_for("draft", "device")
    with engine._connect() as conn:
        conn.execute(
            """INSERT INTO llm_calls(
                project_id,stage,provider,model,input_tokens,output_tokens,
                estimated_cost,status,error,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                pid, "draft", draft.provider, draft.model,
                100, 100, 99.0, "success", "", "2026-07-23T00:00:00+00:00",
            ),
        )
    final_route = route_for("final_signoff", "device")
    engine._assert_call_budget(pid, "final_signoff", final_route)


def test_global_call_ceiling_cannot_be_bypassed_by_nested_rescue(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    draft = route_for("draft", "device")
    for index in range(4):
        engine._record_llm_call(
            pid, f"nested-{index}", draft, 100, 100
        )
    with pytest.raises(RuntimeError, match="Complete-run paid-call ceiling"):
        engine._assert_call_budget(
            pid, "war_room_signoff",
            route_for("war_room_signoff", "device"),
        )


def test_zero_cost_packaging_finishes_after_fourth_paid_call(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    engine._set_stage(pid, "signed_off")
    draft = route_for("draft", "device")
    for index in range(4):
        engine._record_llm_call(
            pid, f"completed-{index}", draft, 100, 100
        )

    def package(project_id, _instructions):
        engine._set_stage(project_id, "package_ready")

    with patch.object(engine, "run_next", side_effect=package) as run_next:
        result = engine.run_to_completion(pid, "")
    assert result["stage"] == "package_ready"
    run_next.assert_called_once()


def test_house_optimization_gates_never_become_publication_blockers():
    house_quality_ids = {
        "D4", "D8", "D9", "D10", "D11", "D12", "D13", "D14",
        "D15", "D16", "D18", "D19",
    }
    findings = [
        {
            "id": gate_id,
            "category": "House quality preference",
            "issue": "Improve presentation.",
            "exact_text": "",
            "replacement": "Improve presentation.",
        }
        for gate_id in sorted(house_quality_ids)
    ]
    blockers, recommendations = partition_findings(findings)
    assert blockers == []
    assert {item["id"] for item in recommendations} == house_quality_ids


def test_preflight_capacity_respects_single_project_call_ceiling(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    route = route_for("draft", "device")
    for _ in range(4):
        engine._record_llm_call(pid, "draft", route, 10, 10)
    preflight = engine.offline_preflight(pid)
    assert preflight["semantic_review"]["remaining_calls"] == 0
    assert (
        preflight["semantic_review"]["reviewer_capacity"]["project"]["remaining"]
        == 0
    )


def test_ecowatt_shaped_preflight_exposes_all_semantic_failures_together():
    hostile = (
        "```html\n<h1>EcoWatt Review</h1>"
        "<p>Buyer Warning</p>"
        "<h2>The Critical Issue: Why It Does Not Work</h2>"
        + "<p>Utilities solely bill one measure. The claim is not independently "
        "verified. This purchase risk lacks proof and cannot provide a benefit. "
        "It is inappropriate and based solely on seller marketing.</p>" * 18
        + "<h2>What Information Is Missing or Unverified</h2>"
        "<h2>Verified Alternatives</h2>"
        "<p>Energy audit, smart thermostat, programmable thermostat, LED "
        "lighting, insulation, air sealing, smart power strip, whole-home "
        "surge protection, energy audit, smart thermostat, LED lighting.</p>\n```"
    )
    report = audit_article(
        hostile,
        "Barchart Advertorial",
        "device",
        "https://partner.example/ecowatt",
    )
    initial_ids = {item["id"] for item in report["initial_findings"]}
    final_ids = {item["id"] for item in report["final_findings"]}
    assert {"D17", "D1", "D18", "D19", "D20"}.issubset(initial_ids)
    assert not ({item["id"] for item in report["mechanical_remaining"]})
    assert {"D18", "D19", "D20"}.issubset(final_ids)
    assert {"D18", "D19"}.isdisjoint({
        item["id"] for item in report["semantic_remaining"]
    })
    assert report["passed"] is False


def test_offline_preflight_does_not_claim_ready_without_exact_semantic_approval(
    tmp_path,
):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "EcoWatt", "Barchart Advertorial",
        "AUTOMATION CONTEXT VERSION: test\n"
        "AFFILIATE LINK: https://partner.example/ecowatt",
        "device",
    )
    article = (
        "<p><strong>Paid Advertorial</strong></p>"
        + "<h2><strong>Product Details</strong></h2>"
        + "<p>EcoWatt product details, setup, price, best fit, and material "
        "limitations are explained in plain language for buyers.</p>" * 220
    )
    engine.import_manual_article(pid, article)
    p = engine.get(pid)
    engine._set_report(
        p,
        {
            "verdict": "not_approved",
            "mandatory_edits": [{
                "id": "S1",
                "category": "Semantic editorial review",
                "issue": "The comparison angle still overlaps the prior release.",
            }],
            "reviewed_article_hash": p["article_hash"],
        },
        "admin_review",
        "semantic-rejection.json",
    )
    preflight = engine.offline_preflight(pid)
    assert preflight["blockers"] == []
    assert preflight["semantic_review"]["passed"] is False
    assert preflight["semantic_review"]["unresolved_edits"][0]["id"] == "S1"
    assert preflight["ready_for_packaging"] is False


def test_unsourced_categorical_background_triggers_source_grounding_gate():
    article = (
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>"
        "<p>Utilities solely measure one factor. Equipment typically ranges "
        "from $300 to $700.</p>"
    )
    ids = {
        item["id"] for item in deterministic_findings(
            article, "Barchart Advertorial", "device"
        )
    }
    assert "D20" in ids


def test_barchart_affiliate_links_are_added_and_bolded():
    html = "".join(
        [
            "<p>Paid Advertorial: Compensation may be received if a purchase "
            "is made through links in this advertorial.</p>",
            *(
                f"<h2><strong>Section {i}</strong></h2>"
                f"<p>This is useful product-specific detail number {i}.</p>"
                for i in range(1, 8)
            ),
        ]
    )
    repaired = repair_publication_gates(
        html, "Barchart Advertorial", "device",
        "https://example.com/product",
    )
    assert repaired.count('href="https://example.com/product"') == 4
    assert '<a href="https://example.com/product"><strong>' in repaired


def test_affiliate_link_is_read_from_sealed_pack_json():
    source = (
        'AUTOMATION CONTEXT VERSION: test\n'
        '{"intake_manifest":{"affiliate_link":'
        '"https://seriouslifemagazine.com/ecowatt-power-saver"}}'
    )
    assert _source_affiliate_link(source) == (
        "https://seriouslifemagazine.com/ecowatt-power-saver"
    )


def test_escaped_html_is_blocked_then_rendered_as_clean_markup():
    article = (
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>"
        "&lt;h2&gt;&lt;strong&gt;Product Details&lt;/strong&gt;&lt;/h2&gt;"
        "&lt;p&gt;Seller materials state the setup details.&lt;/p&gt;"
    )
    assert "D17" in {
        item["id"] for item in deterministic_findings(
            article, "Barchart Advertorial", "device"
        )
    }
    repaired = repair_publication_gates(
        article, "Barchart Advertorial", "device"
    )
    assert "&lt;h2" not in repaired
    assert "<h2><strong>Product Details</strong></h2>" in repaired
    assert "D17" not in {
        item["id"] for item in deterministic_findings(
            repaired, "Barchart Advertorial", "device"
        )
    }


def test_disclosure_variants_collapse_to_one_opening_disclosure():
    article = (
        "<p><strong>Paid Advertorial</strong></p>"
        "<p>A commission may be earned through links in this article.</p>"
        "<p>Compensation may be received if a purchase is made.</p>"
        "<h2><strong>Product Details</strong></h2><p>Useful detail.</p>"
    )
    repaired = repair_publication_gates(
        article,
        "Barchart Advertorial",
        "device",
        "https://partner.example/device",
    )
    plain = BeautifulSoup(repaired, "html.parser").get_text(" ", strip=True)
    assert plain.casefold().count("paid advertorial") == 1
    assert plain.casefold().count("compensation may be received") == 1


def test_source_aware_repair_removes_complete_ecowatt_objection_family():
    pack = {
        "excluded_publication_claims": [
            {
                "text": "A patent application has been filed for the device.",
            },
        ],
    }
    source = (
        "AUTOMATION CONTEXT VERSION: test\n"
        "═══ SEALED CURRENT-PRODUCT SOURCE PACK — FACTS ONLY ═══\n"
        + json.dumps(pack)
    )
    article = (
        "<p>Buyers should verify whether a patent application has been filed.</p>"
        "<p>Industrial customers use power factor differently from residential "
        "utility customers.</p>"
        "<p>Dirty electricity comes from household appliances and wiring.</p>"
        "<p>This costs less than professional audits and appliance upgrades.</p>"
        "<p>Seller materials state that EcoWatt is a plug-in device.</p>"
    )

    repaired = repair_source_grounding(article, source, "device")

    assert "patent application" not in repaired
    assert "Industrial customers" not in repaired
    assert "Dirty electricity comes" not in repaired
    assert "costs less than professional" not in repaired
    assert "Seller materials state" in repaired


def test_approved_first_review_packages_without_post_approval_seo_mutation(
    tmp_path,
):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "EcoWatt Power Saver",
        "Barchart Advertorial",
        "AUTOMATION CONTEXT VERSION: test\n"
        "AFFILIATE LINK: https://partner.example/ecowatt",
        "device",
    )
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial:</strong> Compensation may be received "
        "if a purchase is made through links in this advertorial.</p>"
        "<h2><strong>Product Details</strong></h2>"
        + "<p>Seller materials describe the current product offer.</p>" * 250,
    )
    approval = _independent_approval(engine, pid)
    with patch.object(engine, "_openai_review", return_value=approval):
        engine.run_next(pid, "")
    assert engine.get(pid)["stage"] == "signed_off"
    approved_hash = engine.get(pid)["article_hash"]

    with patch.object(
        engine, "_claude", side_effect=AssertionError("SEO must not mutate")
    ):
        engine.run_next(pid, "")

    packaged = engine.get(pid)
    assert packaged["stage"] == "package_ready"
    assert packaged["article_hash"] == approved_hash


def test_double_escaped_html_and_anchor_attributes_are_repaired():
    article = (
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>"
        "&amp;lt;h2&amp;gt;&amp;lt;strong&amp;gt;Offer Details"
        "&amp;lt;/strong&amp;gt;&amp;lt;/h2&amp;gt;"
        "&amp;lt;a href=&amp;quot;https://partner.example/ecowatt&amp;quot;"
        "&amp;gt;&amp;lt;strong&amp;gt;Review details"
        "&amp;lt;/strong&amp;gt;&amp;lt;/a&amp;gt;"
    )
    repaired = repair_publication_gates(
        article, "Barchart Advertorial", "device"
    )
    assert "&amp;lt;" not in repaired
    assert "<h2><strong>Offer Details</strong></h2>" in repaired
    assert 'href="https://partner.example/ecowatt"' in repaired


def test_barchart_set_article_uses_sealed_pack_affiliate_link(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    source = (
        '{"intake_manifest":{"affiliate_link":'
        '"https://partner.example/device"}}'
    )
    pid = engine.create_project(
        "Device", "Barchart Advertorial", source, "device"
    )
    article = (
        "<p>Paid Advertorial: Compensation may be received.</p>"
        + "".join(
            f"<h2>Section {i}</h2><p>Useful verified product detail {i}.</p>"
            for i in range(1, 9)
        )
        + ("<p>Additional sourced detail for the buyer.</p>" * 260)
    )
    engine.import_manual_article(pid, article)
    saved = engine.get(pid)["article_text"]
    assert saved.count('href="https://partner.example/device"') == 4


def test_routing_uses_stronger_final_review_only_for_higher_risk():
    assert risk_tier("general_consumer") == 0
    assert risk_tier("financial") == 3
    assert route_for("final_signoff", "general_consumer").model == "gpt-5.4-mini"
    assert route_for("final_signoff", "financial").model == "gpt-5.4"
    assert route_for("compliance_repair", "financial").max_calls == 2
    assert route_for("seo_repair", "financial").max_calls == 2


def test_llm_call_budget_blocks_repeat_stage_calls(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "device source", "general_consumer")
    route = route_for("draft", "general_consumer")
    engine._record_llm_call(pid, "draft", route, 100, 100)
    with pytest.raises(RuntimeError, match="call ceiling"):
        engine._assert_call_budget(pid, "draft", route)
    summary = engine.usage_summary(pid)
    assert summary["calls"] == 1
    assert summary["estimated_cost"] > 0


def test_post_seo_signoff_has_independent_call_budget(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "financial source", "financial")
    pre_seo = route_for("final_signoff", "financial")
    for _ in range(pre_seo.max_calls):
        engine._record_llm_call(pid, "final_signoff", pre_seo, 100, 100)
    with pytest.raises(RuntimeError, match="call ceiling"):
        engine._assert_call_budget(pid, "final_signoff", pre_seo)

    post_seo = route_for("post_seo_signoff", "financial")
    engine._assert_call_budget(pid, "post_seo_signoff", post_seo)


def test_seo_repair_has_independent_call_budget(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Test", "AccessNewsWire", "financial source", "financial"
    )
    compliance_repair = route_for("compliance_repair", "financial")
    for _ in range(compliance_repair.max_calls):
        engine._record_llm_call(
            pid, "compliance_repair", compliance_repair, 100, 100
        )
    with pytest.raises(RuntimeError, match="call ceiling"):
        engine._assert_call_budget(
            pid, "compliance_repair", compliance_repair
        )

    seo_repair = route_for("seo_repair", "financial")
    engine._assert_call_budget(pid, "seo_repair", seo_repair)


def test_mandatory_quality_rescue_has_independent_stronger_budget():
    rescue = route_for("quality_rescue", "device")
    assert rescue.max_calls == 2
    assert rescue.max_tokens >= 12000


def test_compliance_repair_ceiling_escalates_to_quality_rescue(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>",
    )
    p = engine.get(pid)
    report = {
        "verdict": "not_approved",
        "mandatory_count": 1,
        "mandatory_edits": [{
            "id": "M1", "category": "Quality",
            "issue": "Repair the article.", "exact_text": "",
            "replacement": "Rebuild it.",
        }],
        "recommended_edits": [], "approved_elements": [], "notes": [],
        "reviewed_article_hash": p["article_hash"],
    }
    engine._set_report(
        p, report, "compliance_reviewed", "repair-needed.json"
    )
    normal = route_for("compliance_repair", "device")
    for _ in range(normal.max_calls):
        engine._record_llm_call(
            pid, "compliance_repair", normal, 10, 10
        )
    with patch.object(
        engine, "_claude",
        return_value=(
            "<h1>Device Review</h1>"
            "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>"
        ),
    ) as writer:
        engine._run_next_unlocked(pid, "")
    assert writer.call_args.args[2] == "quality_rescue"
    assert engine.get(pid)["stage"] == "revised"


def test_manual_path_and_hash_bound_report(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "Barchart Advertorial", "financial newsletter source")
    engine.import_manual_article(pid, "<h2>Draft</h2><p>Text.</p>")
    p = engine.get(pid)
    assert p["stage"] == "drafted"
    report = {"verdict": "not_approved", "mandatory_count": 1,
              "mandatory_edits": [{"id": "M1", "category": "Test",
                                    "issue": "Fix text", "exact_text": "Text",
                                    "replacement": "Revised text"}],
              "recommended_edits": [],
              "approved_elements": [], "reviewed_article_hash": p["article_hash"]}
    engine.import_manual_report(pid, json.dumps(report))
    assert engine.get(pid)["stage"] == "compliance_reviewed"


def test_manual_report_cannot_self_approve_without_current_hash(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "device source")
    engine.import_manual_article(pid, "<p>Draft</p>")
    with pytest.raises(ValueError, match="current article hash"):
        engine.import_manual_report(pid, "VERDICT: APPROVED")
    with pytest.raises(ValueError, match="current article hash"):
        engine.import_manual_report(pid, json.dumps({
            "verdict": "approved", "mandatory_edits": [],
            "reviewed_article_hash": "wrong",
        }))


def test_contradictory_compliance_report_is_rejected(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    with pytest.raises(ValueError, match="contradicts"):
        engine._validate_report({
            "verdict": "approved",
            "mandatory_edits": [{"id": "M1"}],
        })


def test_duplicate_paid_run_is_blocked_and_stale_lock_recovers(tmp_path, monkeypatch):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "device source")
    with engine._connect() as conn:
        conn.execute(
            "UPDATE projects SET run_token='busy',run_started_at=? WHERE id=?",
            ("2099-01-01T00:00:00+00:00", pid),
        )
    with pytest.raises(RuntimeError, match="already running"):
        engine.run_next(pid, "")
    monkeypatch.setenv("NEWSWIRE_STALE_RUN_SECONDS", "0")
    with engine._connect() as conn:
        conn.execute(
            "UPDATE projects SET run_started_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (pid,),
        )
    monkeypatch.setattr(engine, "_run_next_unlocked", lambda project_id, instructions: engine.get(project_id))
    engine.run_next(pid, "")
    assert any(e["event_type"] == "stale_run_recovered" for e in engine.events(pid))


def test_overwritten_artifact_is_archived(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "device source")
    engine._write(pid, "report.json", "first")
    engine._write(pid, "report.json", "second")
    history = list((engine.projects_dir / pid / "history").glob("report-*.json"))
    assert len(history) == 1
    assert history[0].read_text() == "first"


def test_package_builds_downloadable_zip(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "device source")
    engine.import_manual_article(pid, "<p>Final</p>")
    p = engine.get(pid)
    p["last_report"] = {"verdict": "approved", "mandatory_count": 0,
                        "mandatory_edits": [], "reviewed_article_hash": p["article_hash"]}
    engine._build_package(p)
    assert engine.export_path(pid).exists()
    with zipfile.ZipFile(engine.export_path(pid)) as archive:
        assert {"00-source-record.txt", "FINAL-ARTICLE.html", "submission-manifest.json"}.issubset(archive.namelist())


def test_next_action_includes_admin_queue(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "device source")
    with engine._connect() as conn:
        conn.execute("UPDATE projects SET stage='admin_review' WHERE id=?", (pid,))
    assert engine.next_action(engine.get(pid)) == "Kevin review queue"


def test_post_seo_has_separate_repair_path(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "financial source", "financial")
    with engine._connect() as conn:
        conn.execute("UPDATE projects SET stage='seo_repair_needed' WHERE id=?", (pid,))
    assert "SEO compliance" in engine.next_action(engine.get(pid))


def test_deterministic_financial_gates_override_model_ambiguity():
    findings = deterministic_findings(
        "<h1>Offer</h1><p>Take our guaranteed trial.</p>",
        "AccessNewsWire", "financial",
    )
    ids = {item["id"] for item in findings}
    assert {"D1", "D6", "D7"}.issubset(ids)


def test_deterministic_gate_hides_raw_affiliate_url_and_routing_explanation():
    article = """<p><strong>Paid Advertorial</strong> — Links may lead to a third-party partner page rather than the official domain.</p>
    <p>Investments carry risk, including loss of principal.</p>
    <a href=\"https://partner.example/offer\">https://partner.example/offer</a>"""
    ids = {item["id"] for item in deterministic_findings(
        article, "AccessNewsWire", "financial"
    )}
    assert {"D8", "D9"}.issubset(ids)


def test_long_advertorial_requires_early_distributed_ctas():
    article = ("<p><strong>Paid Advertorial</strong></p>" +
               "<p>Investments carry risk, including loss of principal.</p>" +
               ("<p>Useful sourced discussion for readers.</p>" * 400) +
               '<a href="https://partner.example/offer">Review offer details</a>')
    ids = {item["id"] for item in deterministic_findings(
        article, "AccessNewsWire", "financial"
    )}
    assert {"D10", "D11"}.issubset(ids)


def test_master_format_gate_requires_bold_headings_ctas_and_link_count():
    article = ("<h1>Title</h1><h2>Section</h2>" +
               '<p><a href="https://example.com/offer">Review offer</a></p>' +
               ("<p>Useful sourced discussion for readers.</p>" * 400))
    ids = {item["id"] for item in deterministic_findings(
        article, "AccessNewsWire", "financial"
    )}
    assert {"D12", "D13", "D14", "D15", "D16"}.issubset(ids)


def test_wordpress_connector_is_draft_only(monkeypatch):
    seen = []
    class Response:
        status_code = 200
        def json(self):
            return {"id": 42, "name": "Editor", "status": "draft", "link": "https://example.com/?p=42"}
    def fake_request(method, url, **kwargs):
        seen.append((method, url, kwargs))
        return Response()
    monkeypatch.setattr("newswire_workbench.wordpress.requests.request", fake_request)
    publisher = WordPressDraftPublisher("https://example.com", "user", "secret")
    result = publisher.save_draft("Title", "<p>Body</p>")
    assert result["status"] == "draft"
    assert seen[0][2]["json"]["status"] == "draft"


def test_master_formatter_caps_conversion_bolding_and_bolds_ctas():
    body = '<h2>Section</h2><a href="https://example.com">Offer</a>'
    body += "".join(f"<p><strong>Useful takeaway {i}</strong></p>" for i in range(30))
    normalized = normalize_master_html(body, 2400)
    assert '<h2><strong>Section</strong></h2>' in normalized
    assert '<a href="https://example.com"><strong>Offer</strong></a>' in normalized
    assert normalized.count('class="key-takeaway"') == 12


def test_affiliate_link_formatter_adds_early_varied_ctas_to_five():
    href = "https://partner.example/offer"
    body = "<p>Disclosure</p><p>Lead</p>" + ("<h2>Section</h2><p>Text</p>" * 8)
    body += f'<p><a href="{href}"><strong>Existing CTA</strong></a></p>'
    normalized = ensure_affiliate_links(body, href, target=5)
    assert normalized.count(f'href="{href}"') == 5
    assert "See current pricing and available package options" in normalized


def test_recurring_review_issues_become_memory(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "financial source", "financial")
    engine.import_manual_article(pid, "<p><strong>Paid Advertorial</strong></p><p>Investments carry risk, including loss of principal.</p>")
    for n in range(2):
        p = engine.get(pid)
        report = {"verdict": "not_approved", "mandatory_count": 1,
                  "mandatory_edits": [{"id": f"M{n}", "category": "Affiliate wording",
                  "issue": "Partner URL needs neutral disclosure", "exact_text": "", "replacement": ""}],
                  "recommended_edits": [], "approved_elements": [],
                  "reviewed_article_hash": p["article_hash"]}
        engine._set_report(p, report, "compliance_reviewed", f"r{n}.json")
    with engine._connect() as conn:
        conn.execute("UPDATE projects SET stage='package_ready' WHERE id=?", (pid,))
    assert "Seen 2 times" in engine._learned_guidance("AccessNewsWire", "financial")


def test_adjudicator_applies_exact_fixes_and_rejects_bad_platform_attribution(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "AccessNewsWire", "financial source", "financial")
    article = "<p><strong>Paid Advertorial</strong></p><p>Old title</p><p>Investments carry risk, including loss of principal.</p>"
    engine.import_manual_article(pid, article)
    p = engine.get(pid)
    report = {"mandatory_edits": [
        {"id": "M1", "exact_text": "<p>Old title</p>", "replacement": "<p>New title</p>"},
        {"id": "M2", "exact_text": "<p><strong>Paid Advertorial</strong></p>",
         "replacement": "<p>AccessNewsWire may receive compensation.</p>"},
    ]}
    assert engine._adjudicate_current(p, report) is True
    updated = engine.get(pid)["article_text"]
    assert "New title" in updated
    assert "AccessNewsWire may receive" not in updated


def test_adjudicator_can_repair_separately_stored_release_title(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "America's #1 Stock", "AccessNewsWire",
        "financial newsletter source", "financial",
    )
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial</strong></p>"
        "<p>Investing involves risk, including loss of principal.</p>",
    )
    p = engine.get(pid)
    old_hash = p["article_hash"]
    report = {"mandatory_edits": [{
        "id": "TITLE1",
        "category": "Title accuracy",
        "issue": "Avoid an unsupported superlative in the editorial title.",
        "exact_text": "America's #1 Stock",
        "replacement": "Jim Woods Forecasts & Strategies Review",
    }]}
    assert engine._adjudicate_current(p, report) is True
    updated = engine.get(pid)
    assert updated["release_title"] == "Jim Woods Forecasts & Strategies Review"
    assert updated["article_hash"] != old_hash
    assert updated["article_hash"] == __import__("hashlib").sha256(
        (
            updated["release_title"] + "\n" + updated["article_text"]
        ).encode("utf-8")
    ).hexdigest()


def test_legacy_reviewer_admin_state_recovers_without_global_counters(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "America's #1 Stock", "AccessNewsWire",
        "AFFILIATE LINK: https://partner.example/offer",
        "financial",
    )
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial</strong></p>"
        "<p>Investing involves risk, including loss of principal.</p>"
            + ("<p>Useful sourced newsletter information for readers.</p>" * 450),
    )
    p = engine.get(pid)
    report = {
        "verdict": "not_approved",
        "mandatory_count": 1,
        "mandatory_edits": [{
            "id": "TITLE1", "category": "Title accuracy",
            "issue": "Editorial title needs qualification.",
            "exact_text": "America's #1 Stock",
            "replacement": "Jim Woods Forecasts & Strategies Review",
        }],
        "recommended_edits": [], "approved_elements": [],
        "notes": [], "reviewed_article_hash": p["article_hash"],
    }
    engine._set_report(p, report, "admin_review", "legacy-admin.json")
    with patch.object(
        engine, "_openai_review",
        side_effect=lambda *_a, **_k: _independent_approval(engine, pid),
    ):
        assert engine._recover_mechanical_admin_review(pid) is True
    recovered = engine.get(pid)
    assert recovered["stage"] == "signed_off"
    assert recovered["release_title"] == "Jim Woods Forecasts & Strategies Review"


def test_reviewer_conflict_prose_is_not_source_conflict_evidence():
    report = {
        "mandatory_edits": [{
            "category": "Conflicting sources",
            "issue": "Sources conflict and require client clarification.",
            "exact_text": "Claim",
            "replacement": "Qualified claim",
        }]
    }
    assert WorkbenchEngine._report_has_true_source_conflict(report) is False
    report["source_conflict_evidence"] = [{
        "records": ["official_page", "checkout"],
        "incompatible_facts": ["$77", "$99"],
    }]
    assert WorkbenchEngine._report_has_true_source_conflict(report) is True


def test_structured_source_conflict_is_autoresolved_not_sent_to_admin(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Test", "AccessNewsWire",
        "AFFILIATE LINK: https://partner.example/offer\n"
        "official page and checkout records",
        "financial",
    )
    p = engine.get(pid)
    report = {
        "verdict": "not_approved",
        "mandatory_count": 1,
        "mandatory_edits": [{
            "id": "price-conflict",
            "category": "Factual verification",
            "issue": "The official page and checkout show different prices.",
            "exact_text": "$77",
            "replacement": "the current price shown at checkout",
        }],
        "source_conflict_evidence": [{
            "records": ["official_page", "checkout"],
            "incompatible_facts": ["$77", "$99"],
        }],
    }
    engine._set_article(
        p,
        "<p><strong>Paid Advertorial</strong></p>"
        "<p>Investing involves risk, including loss of principal.</p>"
        "<h2>Review</h2><p>The offer costs $77.</p>"
            + ("<p>Useful sourced newsletter information for readers.</p>" * 450),
        "revised", "test-source-conflict.html",
    )
    p = engine.get(pid)
    engine._set_report(p, report, "admin_review", "legacy-conflict.json")

    with patch.object(
        engine, "_openai_review",
        side_effect=lambda *_a, **_k: _independent_approval(engine, pid),
    ):
        assert engine._recover_mechanical_admin_review(pid) is True
    recovered = engine.get(pid)
    assert recovered["stage"] != "admin_review"
    assert "$77" not in recovered["article_text"]
    assert "current price shown at checkout" in recovered["article_text"]


def test_adjudicated_article_requires_independent_rescue_signoff(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "Barchart Advertorial", "financial source", "financial")
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial</strong></p>"
        "<p>Investments carry risk, including loss of principal.</p>"
        "<p>Old wording</p>",
    )
    p = engine.get(pid)
    report = {"mandatory_edits": [
        {"id": "M1", "exact_text": "<p>Old wording</p>",
         "replacement": "<p>Updated factual wording</p>"},
    ]}
    assert engine._adjudicate_current(p, report) is True
    with patch(
        "newswire_workbench.engine.deterministic_findings", return_value=[]
    ), patch.object(
        engine, "_openai_review",
        side_effect=lambda *_a, **_k: _independent_approval(engine, pid),
    ) as reviewer:
        assert engine._complete_adjudicated_signoff(
            pid, "signed_off", "adjudicated-signoff.json"
        ) is True
    reviewer.assert_called_once()
    updated = engine.get(pid)
    assert updated["stage"] == "signed_off"
    assert updated["last_report"]["verdict"] == "approved"


def test_exhausted_independent_signoff_escalates_to_executive_review(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>",
    )
    route = route_for("independent_rescue_signoff", "device")
    for _ in range(route.max_calls):
        engine._record_llm_call(
            pid, "independent_rescue_signoff", route, 100, 100
        )

    purposes = []

    def approve(_p, final=None, purpose=None):
        purposes.append(purpose)
        return _independent_approval(engine, pid)

    with patch(
        "newswire_workbench.engine.deterministic_findings", return_value=[]
    ), patch.object(engine, "_openai_review", side_effect=approve):
        assert engine._complete_adjudicated_signoff(
            pid, "signed_off", "executive-signoff.json"
        ) is True
    assert purposes == ["executive_rescue_signoff"]
    assert engine.get(pid)["stage"] == "signed_off"


def test_executive_signoff_has_protected_budget_after_normal_rescue(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    normal = route_for("quality_rescue", "device")
    with engine._connect() as conn:
        conn.execute(
            """INSERT INTO llm_calls(
                project_id,stage,provider,model,input_tokens,output_tokens,
                estimated_cost,status,error,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                pid, "quality_rescue", normal.provider, normal.model,
                100, 100, 6.25, "success", "", "2026-07-23T00:00:00+00:00",
            ),
        )
    executive = route_for("executive_rescue_signoff", "device")
    engine._assert_call_budget(
        pid, "executive_rescue_signoff", executive
    )


def test_all_semantic_review_tiers_exhaust_cleanly_without_exception(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>",
    )
    for purpose in (
        "independent_rescue_signoff",
        "executive_rescue_signoff",
        "war_room_signoff",
    ):
        route = route_for(purpose, "device")
        for _ in range(route.max_calls):
            engine._record_llm_call(pid, purpose, route, 10, 10)
    with patch(
        "newswire_workbench.engine.deterministic_findings", return_value=[]
    ):
        assert engine._complete_adjudicated_signoff(
            pid, "signed_off", "exhausted.json"
        ) is False
    assert engine.get(pid)["stage"] == "admin_review"
    assert engine.events(pid)[-1]["event_type"] == (
        "global_review_budget_exhausted"
    )


def test_quality_rescue_exhaustion_escalates_to_war_room_rebuild(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>",
    )
    normal = route_for("quality_rescue", "device")
    for _ in range(normal.max_calls):
        engine._record_llm_call(pid, "quality_rescue", normal, 10, 10)
    blocker = [{
        "id": "D20", "category": "Source grounding",
        "issue": "Unsupported technical assertions.",
        "exact_text": "unsupported claim", "replacement": "Rebuild completely.",
    }]
    rebuilt = (
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>"
        + "<p>Complete product-specific detail.</p>" * 250
    )
    with patch(
        "newswire_workbench.engine.deterministic_findings",
        side_effect=[blocker, blocker, [], []],
    ), patch.object(engine, "_claude", return_value=rebuilt) as writer, patch.object(
        engine, "_openai_review",
        side_effect=lambda *_a, **_k: _independent_approval(engine, pid),
    ):
        assert engine._complete_adjudicated_signoff(
            pid, "signed_off", "war-room.json"
        ) is True
    assert writer.call_args.args[2] == "war_room_rebuild"


def test_step_limit_becomes_typed_recovery_state_not_runtime_error(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    with patch.object(engine, "run_next", return_value=None):
        result = engine.run_to_completion(pid, "", max_steps=1)
    assert result["stage"] == "admin_review"
    assert engine.events(pid)[-1]["event_type"] == "workflow_step_limit_reached"


def test_same_source_failure_memory_is_available_to_next_attempt(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    first = engine.create_project(
        "Device", "Barchart Advertorial", "same source record", "device"
    )
    engine.import_manual_article(
        first,
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>",
    )
    project = engine.get(first)
    report = {
        "verdict": "not_approved",
        "mandatory_count": 1,
        "source_accuracy": {"verified": 1, "checked": 2},
        "mandatory_edits": [{
            "id": "M1", "category": "Source fidelity",
            "issue": "Remove unsupported utility billing claims.",
            "exact_text": "unsupported", "replacement": "Remove it.",
        }],
        "recommended_edits": [], "approved_elements": [], "notes": [],
        "reviewed_article_hash": project["article_hash"],
    }
    engine._set_report(project, report, "compliance_reviewed", "memory.json")
    second = engine.create_project(
        "Device", "Barchart Advertorial", "same source record", "device"
    )
    next_project = engine.get(second)
    guidance = engine._source_failure_guidance(
        next_project["fact_source_hash"], "Barchart Advertorial", "device"
    )
    assert "unsupported utility billing claims" in guidance


def test_forced_rebuild_keeps_stable_fact_source_identity(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pack = seal_source_pack({
        "product": {
            "product_name": "Test Device",
            "official_url": "https://example.com",
        },
        "all_artifacts": [{"artifact_id": "a1"}],
        "claims_by_type": _three_literal_claims(),
        "required_facts": {"missing": []},
    })
    first = engine.create_project_from_pack(
        pack, "Barchart Advertorial", force_new=True
    )
    second = engine.create_project_from_pack(
        pack, "Barchart Advertorial", force_new=True
    )
    first_project = engine.get(first)
    second_project = engine.get(second)
    assert first_project["source_hash"] != second_project["source_hash"]
    assert (
        first_project["fact_source_hash"]
        == second_project["fact_source_hash"]
        == pack["source_pack_contract"]["sha256"]
    )


def test_independent_rescue_rejection_cannot_be_synthetically_approved(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Device", "Barchart Advertorial", "device source", "device"
    )
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial:</strong> Compensation may be received.</p>",
    )
    rejected = {
        "verdict": "not_approved",
        "mandatory_count": 1,
        "source_accuracy": {"verified": 0, "checked": 1},
        "mandatory_edits": [{
            "id": "M1",
            "category": "Source fidelity",
            "issue": "An external assertion is unsupported.",
            "exact_text": "unsupported",
            "replacement": "Remove it.",
        }],
        "recommended_edits": [],
        "approved_elements": [],
        "notes": [],
        "reviewed_article_hash": engine.get(pid)["article_hash"],
    }
    with patch(
        "newswire_workbench.engine.deterministic_findings", return_value=[]
    ), patch.object(engine, "_openai_review", return_value=rejected):
        assert engine._complete_adjudicated_signoff(
            pid, "signed_off", "independent-signoff.json"
        ) is False
    updated = engine.get(pid)
    assert updated["stage"] == "compliance_reviewed"
    assert updated["last_report"]["verdict"] == "not_approved"


def test_nonblocking_house_format_target_cannot_strand_va(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Test", "AccessNewsWire", "financial source", "financial"
    )
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial</strong></p>"
        "<p>Investments carry risk, including loss of principal.</p>",
    )
    persistent_style_finding = [{
        "id": "D15",
        "category": "MBK multi-speed reading gate",
        "issue": "Scan-path phrase target was not exact.",
        "exact_text": "",
        "replacement": "Improve emphasis distribution.",
    }]
    with patch(
        "newswire_workbench.engine.deterministic_findings",
        return_value=persistent_style_finding,
    ), patch(
        "newswire_workbench.engine.repair_publication_gates",
        side_effect=lambda article, *_args: article,
    ), patch.object(
        engine, "_openai_review",
        side_effect=lambda *_a, **_k: _independent_approval(engine, pid),
    ):
        assert engine._complete_adjudicated_signoff(
            pid, "signed_off", "style-warning-signoff.json"
        ) is True
    updated = engine.get(pid)
    assert updated["stage"] == "signed_off"
    assert updated["last_report"]["verdict"] == "approved"


def test_nonblocking_house_format_target_cannot_block_wordpress_handoff(
    tmp_path,
):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Test", "AccessNewsWire", "financial source", "financial"
    )
    engine.import_manual_article(
        pid,
        "<p><strong>Paid Advertorial</strong></p>"
        "<p>Investments carry risk, including loss of principal.</p>",
    )
    p = engine.get(pid)
    approval = {
        "verdict": "approved",
        "mandatory_count": 0,
        "mandatory_edits": [],
        "recommended_edits": [],
        "approved_elements": [],
        "notes": [],
        "reviewed_article_hash": p["article_hash"],
    }
    engine._set_report(p, approval, "package_ready", "approved.json")
    style_finding = [{
        "id": "D16",
        "category": "MBK strategic link gate",
        "issue": "Affiliate-link count differs from house target.",
        "exact_text": "",
        "replacement": "Improve link distribution.",
    }]
    from unittest.mock import MagicMock, patch
    publisher = MagicMock()
    publisher.configured = True
    publisher.site_url = "https://example.com"
    publisher.save_draft.return_value = {
        "post_id": 123,
        "edit_url": "https://example.com/wp-admin/post.php?post=123&action=edit",
        "site_url": "https://example.com",
    }
    publisher.get_draft.return_value = {
        "post_id": 123,
        "status": "draft",
        "post_type": "post",
        "title_raw": "Test",
        "content_raw": p["article_text"],
    }
    with patch(
        "newswire_workbench.engine.deterministic_findings",
        return_value=style_finding,
    ), patch(
        "newswire_workbench.wordpress.WordPressDraftPublisher",
        return_value=publisher,
    ):
        result = engine.send_to_wordpress_draft(pid)
    assert result["post_id"] == 123


def test_deterministic_publication_defects_self_repair_without_admin(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    source = (
        "AFFILIATE LINK: https://partner.example/offer\n"
        "Official URL: https://publisher.example/newsletter"
    )
    pid = engine.create_project(
        "Newsletter", "AccessNewsWire", source, "financial"
    )
    article = (
        "<h1>Newsletter Review</h1><h2>What Readers Receive</h2>"
        "<p>Source Intelligence summary for this guaranteed trial.</p>"
            + ("<p>Useful sourced newsletter discussion for readers.</p>" * 450)
    )
    engine.import_manual_article(pid, article)
    with patch.object(
        engine, "_openai_review",
        side_effect=lambda *_a, **_k: _independent_approval(engine, pid),
    ):
        assert engine._complete_adjudicated_signoff(
            pid, "signed_off", "adjudicated-signoff.json"
        ) is True
    updated = engine.get(pid)
    assert updated["stage"] == "signed_off"
    findings = deterministic_findings(
        updated["article_text"], "AccessNewsWire", "financial"
    )
    assert partition_findings(findings)[0] == []


def test_publication_gate_repair_hides_raw_url_and_adds_required_structure():
    article = (
        "<h1>Title</h1><h2>Details</h2>"
        "<p>Source Intelligence says this is a guaranteed trial.</p>"
        '<p><a href="https://partner.example/offer">'
        "https://partner.example/offer</a></p>"
        + ("<p>Useful sourced discussion for readers.</p>" * 450)
    )
    repaired = repair_publication_gates(
        article,
        "AccessNewsWire",
        "financial",
        "https://partner.example/offer",
    )
    findings = deterministic_findings(
        repaired, "AccessNewsWire", "financial"
    )
    assert partition_findings(findings)[0] == []
    assert "https://partner.example/offer</a>" not in repaired
    assert repaired.count('href="https://partner.example/offer"') == 5


def test_legacy_mechanical_admin_project_recovers_and_resumes(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Newsletter",
        "AccessNewsWire",
        "AFFILIATE LINK: https://partner.example/offer",
        "financial",
    )
    engine.import_manual_article(
        pid,
        "<h2>Newsletter Details</h2>"
            + ("<p>Useful sourced newsletter discussion for readers.</p>" * 450),
    )
    p = engine.get(pid)
    findings = deterministic_findings(
        p["article_text"], p["platform"], p["vertical"]
    )
    engine._set_stage(pid, "admin_review")
    engine._event(
        pid, "adjudication_unresolved", "admin_review",
        p["article_hash"], {"findings": findings},
    )
    with patch.object(
        engine, "_openai_review",
        side_effect=lambda *_a, **_k: _independent_approval(engine, pid),
    ):
        assert engine._recover_mechanical_admin_review(pid) is True
    recovered = engine.get(pid)
    assert recovered["stage"] == "signed_off"
    findings = deterministic_findings(
        recovered["article_text"], recovered["platform"], recovered["vertical"]
    )
    assert partition_findings(findings)[0] == []


def test_reviewer_house_rule_conflicts_cannot_block(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    report = {"verdict": "not_approved", "mandatory_count": 2,
              "mandatory_edits": [
                  {"id": "M1", "exact_text": "Disclosure", "replacement": "We may earn a commission."},
                  {"id": "M2", "exact_text": "Priority code ABC123 may apply.", "replacement": ""},
              ], "notes": []}
    cleaned = engine._remove_house_rule_conflicts(report)
    assert cleaned["verdict"] == "approved"
    assert cleaned["mandatory_count"] == 0


def test_reviewer_cannot_require_affiliate_routing_disclosure(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    report = {
        "verdict": "not_approved",
        "mandatory_count": 1,
        "mandatory_edits": [{
            "id": "M1",
            "exact_text": "Opening disclosure",
            "issue": (
                "Opening disclosure must state that the linked page is not the "
                "official brand site because it uses a third-party affiliate domain."
            ),
            "replacement": "This is not the official brand site.",
        }],
        "notes": [],
    }
    cleaned = engine._remove_house_rule_conflicts(report)
    assert cleaned["verdict"] == "approved"
    assert cleaned["mandatory_edits"] == []


def test_learning_memory_drops_house_conflicts_and_narrows_prior_link_rule():
    rows = [
        {
            "category": "Disclosure",
            "issue": (
                "The opening must state this is not the official site because "
                "the CTA uses a third-party affiliate domain."
            ),
            "occurrences": 3,
        },
        {
            "category": "Prior release",
            "issue": "The prior-release rule forbids the contextual link.",
            "occurrences": 2,
        },
        {
            "category": "Source grounding",
            "issue": "Utility-grid assertions are absent from the sealed record.",
            "occurrences": 4,
        },
    ]
    cleaned = WorkbenchEngine._sanitize_guidance_rows(rows)
    assert len(cleaned) == 2
    assert "preserve one quiet contextual backlink" in cleaned[0]["issue"]
    assert "Utility-grid assertions" in cleaned[1]["issue"]
