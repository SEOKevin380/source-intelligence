"""Static UI anchors for the external-audit today-tier repairs."""

from pathlib import Path


def test_source_pack_wording_is_honest_and_queue_is_in_both_uis():
    production_app = Path("app.py").read_text(encoding="utf-8")
    local_workbench = Path("newswire_workbench_app.py").read_text(
        encoding="utf-8"
    )
    combined = (production_app + local_workbench).casefold()

    assert "verified source pack" not in combined
    assert "sealing does not independently verify" in production_app
    assert "Kevin review queue" in production_app
    assert "Kevin review queue" in local_workbench
    assert "admin_review_queue(\n        resolve_actions=False" in (
        production_app
    )
    assert "engine.admin_review_queue(resolve_actions=False)" in (
        local_workbench
    )
    assert "_cached_admin_review_action(" in production_app
    assert "_cached_admin_review_action(" in local_workbench
    assert "index=None" in production_app
    assert "index=None" in local_workbench
    assert "if _selected_admin_id:" in production_app
    assert "if _queue_id:" in local_workbench
