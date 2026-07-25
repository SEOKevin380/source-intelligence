import json
from unittest.mock import patch

import pytest

from article_provenance import build_article_claim_ledger
from newswire_workbench.editorial_truth import audit_editorial_truth
from newswire_workbench.engine import WorkbenchEngine
from newswire_workbench.routing import route_for
from source_pack_contract import seal_source_pack


AFFILIATE = "https://partner.example/offer"


def _pack():
    return {
        "product": {
            "product_name": "Example Fortune",
            "official_url": "https://example.com/",
            "product_type": "gaming",
        },
        "intake_manifest": {
            "affiliate_link": AFFILIATE,
            "refund_terms": "60-Day Refund Guarantee",
        },
        "publication_claims": {
            "feature": [
                {
                    "claim_id": "feature-free-game",
                    "text": (
                        "A free scratch game lets users uncover six of nine "
                        "crystal balls to reveal Fortune Numbers."
                    ),
                },
                {
                    "claim_id": "feature-reading",
                    "text": (
                        "According to the seller, personalized readings and "
                        "reports are based on generated Fortune Numbers."
                    ),
                },
                {
                    "claim_id": "feature-delivery",
                    "text": (
                        "A secure digital link is provided after purchase."
                    ),
                },
            ],
            "pricing": [
                {
                    "claim_id": "pricing-options",
                    "text": (
                        "One-time or monthly recurring options are available."
                    ),
                },
            ],
            "limitation": [
                {
                    "claim_id": "limit-outcome",
                    "text": (
                        "Fortune Numbers are not a prediction of any lottery "
                        "result and no outcome is guaranteed."
                    ),
                },
            ],
        },
        "required_facts": {
            "missing": ["pricing", "jurisdiction limits"],
        },
    }


def _disclosure():
    return (
        "<p><strong>Paid Advertorial:</strong> Compensation may be received "
        "if a purchase is made through links in this advertorial.</p>"
    )


@pytest.mark.parametrize(
    ("sentence", "rule"),
    [
        (
            "The numbers are not predetermined or assigned arbitrarily.",
            "invented_randomness_or_assignment",
        ),
        (
            "The calendars presumably suggest auspicious dates.",
            "speculative_product_expansion",
        ),
        (
            "Buyers receive access to an account and content library.",
            "invented_access_or_delivery",
        ),
        (
            "Package tiers and promotional pricing may vary over time.",
            "invented_price_variability",
        ),
        (
            "The free trial period lets buyers evaluate the paid product.",
            "invented_trial_scope",
        ),
        (
            "The product is not affiliated with any state or national lottery.",
            "invented_affiliation",
        ),
        (
            "The refund policy means the financial risk is limited.",
            "invented_commercial_safety",
        ),
        (
            "There is no software to download and no hardware required.",
            "invented_access_or_delivery",
        ),
    ],
)
def test_high_confidence_false_claim_mutations_are_blocked(sentence, rule):
    audit = audit_editorial_truth(
        _pack(), _disclosure() + f"<p>{sentence}</p>", AFFILIATE
    )
    assert audit["passed"] is False
    assert rule in {
        item["rule"] for item in audit["grounding_violations"]
    }


def test_known_good_source_bound_copy_passes_hard_truth_and_cta_gates():
    article = (
        _disclosure()
        + '<p><a href="https://partner.example/offer"><strong>'
          "Review the Example Fortune offer</strong></a></p>"
        + "<h2><strong>How the Game Works</strong></h2>"
        + "<p>According to the seller, a free scratch game lets users uncover "
          "six of nine crystal balls to reveal Fortune Numbers.</p>"
        + "<p>Fortune Numbers are not a prediction of any lottery result and "
          "no outcome is guaranteed.</p>"
        + '<p><a href="https://partner.example/offer"><strong>'
          "See current ordering details</strong></a></p>"
        + "<p>According to the seller, one-time or monthly recurring options "
          "are available.</p>"
        + "<p>A secure digital link is provided after purchase.</p>"
    )
    audit = audit_editorial_truth(_pack(), article, AFFILIATE)
    assert audit["grounding_violations"] == []
    assert audit["cta_integrity_violations"] == []
    assert audit["passed"] is True


def test_identical_cta_text_cannot_hide_official_and_affiliate_roles():
    article = (
        _disclosure()
        + '<p><a href="https://example.com/"><strong>'
          "Review the current product details</strong></a></p>"
        + '<p><a href="https://partner.example/offer"><strong>'
          "Review the current product details</strong></a></p>"
    )
    audit = audit_editorial_truth(_pack(), article, AFFILIATE)
    issues = " ".join(
        item["issue"] for item in audit["cta_integrity_violations"]
    )
    assert "Identical CTA text" in issues
    assert "consecutively" in issues
    assert audit["passed"] is False


def test_affiliate_cta_density_is_bounded_independently_of_contact_links():
    body = [_disclosure()]
    for index in range(5):
        body.append(
            f'<p><a href="{AFFILIATE}"><strong>'
            f"Review offer detail {index}</strong></a></p>"
        )
        body.append(
            "<p>According to the seller, a secure digital link is provided "
            "after purchase.</p>"
        )
    audit = audit_editorial_truth(_pack(), "".join(body), AFFILIATE)
    assert any(
        item["category"] == "CTA density integrity"
        for item in audit["cta_integrity_violations"]
    )


def test_bidirectional_claim_ledger_fails_when_required_claims_are_present_but_article_adds_false_fact():
    article = (
        _disclosure()
        + "<p>According to the seller, a free scratch game lets users uncover "
          "six of nine crystal balls to reveal Fortune Numbers.</p>"
        + "<p>According to the seller, personalized readings and reports are "
          "based on generated Fortune Numbers.</p>"
        + "<p>A secure digital link is provided after purchase.</p>"
        + "<p>According to the seller, one-time or monthly recurring options "
          "are available.</p>"
        + "<p>According to the seller, a 60-Day Refund Guarantee is available.</p>"
        + "<p>The product is not affiliated with any state or national lottery.</p>"
    )
    ledger = build_article_claim_ledger(_pack(), article)
    assert ledger["used_claim_count"] >= 3
    assert ledger["coverage_violations"] == []
    assert ledger["grounding_violations"]
    assert ledger["passed"] is False


def test_offline_preflight_surfaces_truth_and_cta_false_passes(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pack = seal_source_pack({
        "product": {
            "product_name": "Example Fortune",
            "official_url": "https://example.com/",
            "product_type": "gaming",
        },
        "intake_manifest": {
            "affiliate_link": AFFILIATE,
        },
        "all_artifacts": [{"artifact_id": "a1"}],
        "claims_by_type": {
            "feature": [
                {
                    "text": "Free scratch game with nine crystal balls",
                    "artifact_id": "a1",
                    "source_class": "official_vendor",
                    "review_status": "unreviewed",
                    "metadata": {"excerpt_is_literal": True},
                },
                {
                    "text": "Personalized readings use Fortune Numbers",
                    "artifact_id": "a1",
                    "source_class": "official_vendor",
                    "review_status": "unreviewed",
                    "metadata": {"excerpt_is_literal": True},
                },
                {
                    "text": "Secure digital link after purchase",
                    "artifact_id": "a1",
                    "source_class": "official_vendor",
                    "review_status": "unreviewed",
                    "metadata": {"excerpt_is_literal": True},
                },
            ],
        },
        "required_facts": {"missing": []},
    })
    pid = engine.create_project_from_pack(
        pack, "AccessNewsWire", force_new=True
    )
    article = (
        _disclosure()
        + '<p><a href="https://example.com/"><strong>'
          "Review the current product details</strong></a></p>"
        + f'<p><a href="{AFFILIATE}"><strong>'
          "Review the current product details</strong></a></p>"
        + "<p>According to the seller, the free scratch game has nine "
          "crystal balls.</p>"
        + "<p>According to the seller, personalized readings use Fortune "
          "Numbers.</p>"
        + "<p>According to the seller, a secure digital link is provided "
          "after purchase.</p>"
        + "<p>The product is not affiliated with any lottery.</p>"
    )
    engine._set_article(
        engine.get(pid), article, "revised", "candidate.html"
    )
    result = engine.offline_preflight(pid)
    ids = {item["id"] for item in result["blockers"]}
    assert any(item.startswith("E-TRUTH-") for item in ids)
    assert any(item.startswith("E-CTA-") for item in ids)
    assert result["ready_for_packaging"] is False


def test_wordpress_handoff_rechecks_current_truth_contract(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pack = seal_source_pack({
        "product": {
            "product_name": "Example Fortune",
            "official_url": "https://example.com/",
            "product_type": "gaming",
        },
        "all_artifacts": [{"artifact_id": "a1"}],
        "claims_by_type": {
            "feature": [
                {
                    "text": f"Literal product fact {index}",
                    "artifact_id": "a1",
                    "source_class": "official_vendor",
                    "review_status": "unreviewed",
                    "metadata": {"excerpt_is_literal": True},
                }
                for index in range(3)
            ],
        },
        "required_facts": {"missing": []},
    })
    pid = engine.create_project_from_pack(
        pack, "AccessNewsWire", force_new=True
    )
    article = (
        _disclosure()
        + "<p>Seller materials state Literal product fact 0.</p>"
        + "<p>Seller materials state Literal product fact 1.</p>"
        + "<p>Seller materials state Literal product fact 2.</p>"
        + "<p>The product includes a free trial period.</p>"
    )
    engine._set_article(
        engine.get(pid), article, "revised", "candidate.html"
    )
    p = engine.get(pid)
    report = {
        "verdict": "approved",
        "mandatory_count": 0,
        "mandatory_edits": [],
        "recommended_edits": [],
        "approved_elements": [],
        "notes": [],
        "reviewed_article_hash": p["article_hash"],
        "approval_purpose": "final_signoff",
    }
    engine._set_report(p, report, "package_ready", "false-approval.json")
    with patch(
        "newswire_workbench.engine.deterministic_findings",
        return_value=[],
    ), pytest.raises(RuntimeError, match="bidirectional editorial-truth"):
        engine.send_to_wordpress_draft(pid)


def test_reviewer_candidate_scope_hash_mismatch_cannot_approve(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    pid = engine.create_project(
        "Example Fortune",
        "AccessNewsWire",
        "gaming source",
        vertical="gaming",
    )
    engine.import_manual_article(
        pid,
        _disclosure()
        + "<p>The product provides an account for ongoing use.</p>",
    )
    engine._record_llm_call(
        pid,
        "compliance",
        route_for("compliance", "gaming"),
        100,
        100,
        raw_output=json.dumps({
            "verdict": "approved",
            "mandatory_count": 0,
            "conditional_approval_after_exact_edits": False,
            "source_accuracy": {"verified": 0, "checked": 1},
            "editorial_truth_review": {
                "candidate_set_hash": "wrong",
                "decisions": [],
            },
            "mandatory_edits": [],
            "recommended_edits": [],
            "approved_elements": [],
            "notes": [],
        }),
        lifecycle="provider_succeeded",
    )
    with patch(
        "newswire_workbench.engine.deterministic_findings",
        return_value=[],
    ):
        report = engine._openai_review(
            engine.get(pid), final=False, purpose="compliance"
        )
    assert report["verdict"] == "not_approved"
    assert any(
        item["id"] == "E-REVIEW-SCOPE"
        for item in report["mandatory_edits"]
    )
