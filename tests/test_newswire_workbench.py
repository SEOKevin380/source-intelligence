import json

import pytest

from newswire_workbench.engine import WorkbenchEngine
from newswire_workbench.prompts import detect_vertical


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
