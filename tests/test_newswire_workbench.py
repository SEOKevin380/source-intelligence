import json

import pytest

from newswire_workbench.engine import WorkbenchEngine
from newswire_workbench.prompts import detect_vertical
from newswire_workbench.learning import deterministic_findings


def test_vertical_detection_is_category_aware():
    assert detect_vertical("investment stock newsletter") == "financial"
    assert detect_vertical("commemorative gold-plated coin") == "collectible"
    assert detect_vertical("supplement facts serving size") == "health"


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


def test_manual_path_and_hash_bound_report(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project("Test", "Barchart Advertorial", "financial newsletter source")
    engine.import_manual_article(pid, "<h2>Draft</h2><p>Text.</p>")
    p = engine.get(pid)
    assert p["stage"] == "drafted"
    report = {"verdict": "not_approved", "mandatory_count": 1,
              "mandatory_edits": [], "recommended_edits": [],
              "approved_elements": [], "reviewed_article_hash": p["article_hash"]}
    engine.import_manual_report(pid, json.dumps(report))
    assert engine.get(pid)["stage"] == "compliance_reviewed"


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
