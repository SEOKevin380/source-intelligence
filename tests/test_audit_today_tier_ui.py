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
    assert "Source verification queue" in production_app
    assert "_cached_source_verification_queue(" in production_app
    assert "Paid drafting is blocked." in production_app
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


def test_source_contract_repair_and_scope_actions_are_truthfully_routed():
    production_app = Path("app.py").read_text(encoding="utf-8")

    assert "def _raw_product_research_json(" in production_app
    assert (
        '_selected_verification["action"] == "repair_source_contract"'
        in production_app
    )
    assert "repair_reason" in production_app
    assert "Download Raw Source Record" in production_app
    assert (
        '_selected_verification["action"]\n'
        '                != "repair_source_contract"'
        in production_app
    )
    assert "review_reviewer_scope_failure" in production_app
