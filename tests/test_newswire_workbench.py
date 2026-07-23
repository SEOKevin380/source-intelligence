import json
import zipfile

import pytest

from newswire_workbench.engine import WorkbenchEngine
from newswire_workbench.prompts import detect_vertical
from newswire_workbench.learning import deterministic_findings
from newswire_workbench.wordpress import WordPressDraftPublisher
from newswire_workbench.formatting import (
    ensure_affiliate_links,
    normalize_master_html,
    repair_publication_gates,
)
from newswire_workbench.routing import risk_tier, route_for
from source_pack_contract import seal_source_pack


def test_vertical_detection_is_category_aware():
    assert detect_vertical("investment stock newsletter") == "financial"
    assert detect_vertical("commemorative gold-plated coin") == "collectible"
    assert detect_vertical("supplement facts serving size") == "health"


def test_zero_cost_auth_failure_does_not_consume_call_ceiling(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Retryable", "AccessNewsWire", "Official URL: https://example.com\nProduct: Device"
    )
    route = route_for("draft", "general")
    engine._record_llm_call(pid, "draft", route, status="failed", error="401 invalid key")
    # A rejected credential never reached paid generation and must remain retryable.
    engine._assert_call_budget(pid, "draft", route)


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
        "claims_by_type": {},
        "required_facts": {"missing": []},
    })
    first = engine.create_project_from_pack(pack, "AccessNewsWire")
    second = engine.create_project_from_pack(pack, "AccessNewsWire")
    assert first == second
    project = engine.get(first)
    assert project["stage"] == "source_ready"
    assert "AUTOMATION CONTEXT VERSION: approved-exemplars-v1" in project["source_text"]
    assert "SEALED CURRENT-PRODUCT SOURCE PACK" in project["source_text"]
    assert any(e["event_type"] == "sealed_source_pack_imported" for e in engine.events(first))


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
    assert "See current subscription pricing" in normalized


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
        + ("<p>Useful sourced newsletter information for readers.</p>" * 320),
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
        + ("<p>Useful sourced newsletter information for readers.</p>" * 320),
        "revised", "test-source-conflict.html",
    )
    p = engine.get(pid)
    engine._set_report(p, report, "admin_review", "legacy-conflict.json")

    assert engine._recover_mechanical_admin_review(pid) is True
    recovered = engine.get(pid)
    assert recovered["stage"] != "admin_review"
    assert "$77" not in recovered["article_text"]
    assert "current price shown at checkout" in recovered["article_text"]


def test_adjudicated_article_advances_without_third_paid_signoff(tmp_path):
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
    from unittest.mock import patch
    with patch("newswire_workbench.engine.deterministic_findings", return_value=[]):
        assert engine._complete_adjudicated_signoff(
            pid, "signed_off", "adjudicated-signoff.json"
        ) is True
    updated = engine.get(pid)
    assert updated["stage"] == "signed_off"
    assert updated["last_report"]["verdict"] == "approved"


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
    from unittest.mock import patch
    with patch(
        "newswire_workbench.engine.deterministic_findings",
        return_value=persistent_style_finding,
    ), patch(
        "newswire_workbench.engine.repair_publication_gates",
        side_effect=lambda article, *_args: article,
    ):
        assert engine._complete_adjudicated_signoff(
            pid, "signed_off", "style-warning-signoff.json"
        ) is True
    updated = engine.get(pid)
    assert updated["stage"] == "signed_off"
    assert "Non-blocking house-format recommendations" in " ".join(
        updated["last_report"]["notes"]
    )


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
        + ("<p>Useful sourced newsletter discussion for readers.</p>" * 320)
    )
    engine.import_manual_article(pid, article)
    assert engine._complete_adjudicated_signoff(
        pid, "signed_off", "adjudicated-signoff.json"
    ) is True
    updated = engine.get(pid)
    assert updated["stage"] == "signed_off"
    assert deterministic_findings(
        updated["article_text"], "AccessNewsWire", "financial"
    ) == []


def test_publication_gate_repair_hides_raw_url_and_adds_required_structure():
    article = (
        "<h1>Title</h1><h2>Details</h2>"
        "<p>Source Intelligence says this is a guaranteed trial.</p>"
        '<p><a href="https://partner.example/offer">'
        "https://partner.example/offer</a></p>"
        + ("<p>Useful sourced discussion for readers.</p>" * 320)
    )
    repaired = repair_publication_gates(
        article,
        "AccessNewsWire",
        "financial",
        "https://partner.example/offer",
    )
    assert deterministic_findings(
        repaired, "AccessNewsWire", "financial"
    ) == []
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
        + ("<p>Useful sourced newsletter discussion for readers.</p>" * 320),
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
    assert engine._recover_mechanical_admin_review(pid) is True
    recovered = engine.get(pid)
    assert recovered["stage"] == "signed_off"
    assert deterministic_findings(
        recovered["article_text"], recovered["platform"], recovered["vertical"]
    ) == []


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
