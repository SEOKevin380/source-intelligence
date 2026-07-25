import pytest
from bs4 import BeautifulSoup

from article_provenance import build_article_claim_ledger
from newswire_workbench import WorkbenchEngine
from newswire_workbench.engine import _source_platform_link
from newswire_workbench.audit import audit_article
from newswire_workbench.learning import deterministic_findings
from newswire_workbench.platform_contracts import (
    AUTOMATED_PLATFORMS,
    GLOBE_DISCLOSURE_TEXT,
)
from newswire_workbench.prompts import generation_prompt, revision_prompt
from scripts.audit_production_editorial_truth import audit_rows


AFFILIATE = "https://partner.example/power"


def test_declared_automated_platforms_include_globe():
    assert AUTOMATED_PLATFORMS == (
        "AccessNewsWire",
        "Barchart Advertorial",
        "Globe Newswire",
    )


def test_newswire_com_rejects_before_project_creation(tmp_path):
    engine = WorkbenchEngine(tmp_path)
    with pytest.raises(ValueError, match="Newswire.com is not supported"):
        engine.create_project(
            "Example", "Newswire.com", "source facts", "device"
        )
    assert engine.list_projects() == []


def test_globe_mechanical_repair_enforces_format_c_structure():
    article = (
        "<p><strong>Paid Advertorial:</strong> Compensation may be received "
        "if a purchase is made through links in this advertorial.</p>"
        "<h2><strong>Product Overview</strong></h2>"
        "<p>Power Pro Genius is designed to support electrical filtering.</p>"
        f'<p><a href="{AFFILIATE}"><strong>Review the offer</strong></a></p>'
        "<h2><strong>Questions Readers May Have</strong></h2>"
        "<p>What does it include?</p>"
        f'<p><a href="{AFFILIATE}"><strong>Buy now</strong></a></p>'
    )
    repaired = audit_article(
        article, "Globe Newswire", "device", AFFILIATE
    )["article"]
    soup = BeautifulSoup(repaired, "html.parser")
    web_links = [
        node for node in soup.find_all("a", href=True)
        if str(node["href"]).startswith("http")
    ]
    assert len(web_links) == 1
    assert web_links[0]["href"] == AFFILIATE
    assert web_links[0].get_text(" ", strip=True) == "Product information"
    assert "Questions Readers May Have" not in repaired
    assert soup.find_all("p")[-1].get_text(" ", strip=True) == (
        GLOBE_DISCLOSURE_TEXT
    )
    ids = {
        item["id"] for item in deterministic_findings(
            repaired, "Globe Newswire", "device", AFFILIATE
        )
    }
    assert not {"D22", "D23"} & ids


def test_globe_observer_attribution_is_a_blocker():
    article = (
        "<h2><strong>Product Overview</strong></h2>"
        "<p>According to the company, the device filters electricity.</p>"
        "<h2><strong>Related Links</strong></h2>"
        f'<p><a href="{AFFILIATE}">Product information</a></p>'
        f"<p>{GLOBE_DISCLOSURE_TEXT}</p>"
    )
    ids = {
        item["id"] for item in deterministic_findings(
            article, "Globe Newswire", "device", AFFILIATE
        )
    }
    assert "D24" in ids


def test_globe_brand_subject_satisfies_seller_claim_provenance():
    pack = {
        "product": {
            "product_name": "Power Pro Genius",
            "publishing_platform": "Globe Newswire",
        },
        "publication_claims": {
            "feature": [{
                "claim_id": "feature-1",
                "text": "Whole-home electricity stabilization",
                "publication_treatment": "seller_attribution_required",
            }]
        },
    }
    article = (
        "<p>Power Pro Genius is designed for whole-home electricity "
        "stabilization.</p>"
    )
    ledger = build_article_claim_ledger(pack, article)
    assert ledger["used_claim_count"] == 1
    assert not ledger["attribution_violations"]


def test_globe_uses_official_destination_when_affiliate_is_absent():
    source = (
        '═══ SEALED CURRENT-PRODUCT SOURCE PACK — FACTS ONLY ═══\n'
        '{"product":{"official_url":"https://example.com/official"}}'
    )
    assert _source_platform_link(source, "Globe Newswire") == (
        "https://example.com/official"
    )
    assert _source_platform_link(source, "AccessNewsWire") == ""


def test_globe_writer_and_repair_prompts_do_not_inherit_web_cta_or_backlink():
    source = (
        '═══ SEALED CURRENT-PRODUCT SOURCE PACK — FACTS ONLY ═══\n'
        '{"product":{"product_name":"Power Pro Genius",'
        '"product_type":"device","official_url":"https://example.com"}}'
    )
    prompts = (
        generation_prompt(
            source, "Globe Newswire", "device", ""
        ),
        revision_prompt(
            source,
            "<p>Power Pro Genius is designed to filter current.</p>",
            {},
            "Globe Newswire",
            "device",
        ),
    )
    for prompt in prompts:
        assert "Do not name their publishers or link to them" in prompt
        assert "A previous-release backlink is mandatory context" not in prompt
        assert "build naturally toward a clear CTA" not in prompt
        assert "decision summary, and FAQs" not in prompt


def test_production_truth_audit_fails_closed_on_empty_dataset():
    result = audit_rows([])
    assert result["audited"] is False
    assert result["empty_input"] is True
    assert result["passed"] is False
