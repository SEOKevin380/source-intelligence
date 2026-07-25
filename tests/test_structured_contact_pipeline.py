import json

import pytest
from bs4 import BeautifulSoup

from article_provenance import (
    build_article_claim_ledger,
    ensure_structured_contact_block,
)
from newswire_workbench.prompts import writer_evidence_view
from newswire_workbench.formatting import repair_publication_gates
from prompt_builders import build_l6_press_release_prompt
from source_pack_contract import (
    extract_labeled_source_inputs,
    form_values_from_pack,
    resolve_intake_contact_terms,
    seal_source_pack,
)


SCRATCH_NOTES = """Email: support@scratch-fortune.com
ClickBank Order Support:
https://www.clkbank.com/#!/?
US: 1-800-390-6035
INT: 1-208-345-4245
60-Day Refund Guarantee"""


def _legacy_pack():
    return {
        "product": {
            "product_name": "Scratch Off Fortune",
            "official_url": "https://scratch-fortune.com/",
            "product_type": "gaming",
            "category": "gaming",
        },
        "intake_manifest": {
            "product_url": "https://scratch-fortune.com/",
            "product_name": "Scratch Off Fortune",
            "publishing_channel": "Accesswire",
            "operator_notes": SCRATCH_NOTES,
        },
        "source_manifest": [{
            "type": "operator_intake",
            "status": "captured",
            "artifact_id": "operator-artifact",
        }],
        "all_artifacts": {
            "operator-artifact": {
                "source_class": "operator_submitted",
                "source_url": "intake://operator-context",
            }
        },
        "claims_by_type": {
            "feature": [{
                "text": "The offer is digital entertainment.",
                "artifact_id": "operator-artifact",
                "source_class": "official_vendor",
                "review_status": "accepted",
                "metadata": {"excerpt_is_literal": True},
            }]
        },
        "required_facts": {"missing": []},
    }


def test_legacy_notes_migrate_into_structured_manifest_without_research():
    sealed = seal_source_pack(_legacy_pack())
    contact = sealed["intake_manifest"]["contact_information"]

    assert contact == {
        "support_email": "support@scratch-fortune.com",
        "support_phone_us": "1-800-390-6035",
        "support_phone_international": "1-208-345-4245",
        "order_support_provider": "ClickBank",
        "order_support_url": "https://www.clkbank.com/#!/?",
    }
    assert sealed["intake_manifest"]["refund_terms"] == (
        "60-Day Refund Guarantee"
    )
    claim_text = {
        claim["text"]
        for claim in sealed["publication_claims"]["company_info"]
    }
    assert "Product support email: support@scratch-fortune.com" in claim_text
    assert "United States support phone: 1-800-390-6035" in claim_text
    assert "International support phone: 1-208-345-4245" in claim_text
    assert "Order support provider: ClickBank" in claim_text


def test_free_text_intake_sorts_product_support_and_order_provider():
    notes = """Product Support:
Email: contact@getCogniHoney.com
Phone: +1 (323) 237-8559

PagAmerican Order Support:
Email: support@pagamerican.app Phone: +1-888-407-0627

The product includes a 60-day money-back guarantee."""
    contact, refund = resolve_intake_contact_terms(notes)

    assert contact["support_email"] == "contact@getCogniHoney.com"
    assert contact["support_phone_us"] == "+1 (323) 237-8559"
    assert contact["order_support_provider"] == "PagAmerican"
    assert refund == "The product includes a 60-day money-back guarantee."


def test_explicit_contact_override_wins_over_automatic_note_sorting():
    contact, refund = resolve_intake_contact_terms(
        SCRATCH_NOTES,
        {
            "support_email": "current@example.com",
            "order_support_provider": "Current Processor",
        },
        "30-day refund guarantee",
    )

    assert contact["support_email"] == "current@example.com"
    assert contact["order_support_provider"] == "Current Processor"
    assert contact["support_phone_us"] == "1-800-390-6035"
    assert refund == "30-day refund guarantee"


def test_labeled_optional_source_urls_are_sorted_from_one_notes_box():
    inputs = extract_labeled_source_inputs(
        """VSL: https://example.com/watch
Label / references: https://example.com/references/
Previous releases:
https://news.example.com/one, https://news.example.com/two
Competitor release: https://competitor.example.com/review
Product Support: support@example.com"""
    )

    assert inputs == {
        "vsl_url": "https://example.com/watch",
        "label_source_url": "https://example.com/references/",
        "previous_releases": (
            "https://news.example.com/one, https://news.example.com/two"
        ),
        "competitor_releases": "https://competitor.example.com/review",
    }


def test_saved_pack_restores_dedicated_contact_controls():
    values = form_values_from_pack(_legacy_pack())

    assert values["rd_support_email"] == "support@scratch-fortune.com"
    assert values["rd_support_phone_us"] == "1-800-390-6035"
    assert values["rd_support_phone_international"] == "1-208-345-4245"
    assert values["rd_order_support_provider"] == "ClickBank"
    assert values["rd_order_support_url"] == "https://www.clkbank.com/#!/?"
    assert values["rd_refund_terms"] == "60-Day Refund Guarantee"


def test_writer_safe_view_keeps_structured_contacts_but_not_free_text_notes():
    sealed = seal_source_pack(_legacy_pack())
    safe = json.loads(writer_evidence_view(json.dumps(sealed)))

    assert safe["intake_manifest"]["contact_information"]["support_email"] == (
        "support@scratch-fortune.com"
    )
    assert safe["intake_manifest"]["refund_terms"] == (
        "60-Day Refund Guarantee"
    )
    assert "operator_notes" not in safe["intake_manifest"]


def test_manual_export_prompt_labels_structured_contact_and_refund_terms():
    sealed = seal_source_pack(_legacy_pack())
    prompt = build_l6_press_release_prompt(
        sealed,
        {
            "platform": "Accesswire",
            "affiliate_link": "https://publisher.example/scratch",
            "previous_releases": "FIRST RELEASE",
            "release_type": "Single Product",
            "ymyl_category": "No",
        },
    )

    assert "STRUCTURED CONTACT INFORMATION:" in prompt
    assert "Product Support Email: support@scratch-fortune.com" in prompt
    assert "U.S. Support Phone: 1-800-390-6035" in prompt
    assert "International Support Phone: 1-208-345-4245" in prompt
    assert "Order Support URL: https://www.clkbank.com/#!/?" in prompt
    assert (
        "STRUCTURED REFUND / GUARANTEE TERMS: 60-Day Refund Guarantee"
        in prompt
    )
    assert "Final-output requirement: reproduce every structured contact" in prompt


def test_contact_coverage_gate_blocks_incomplete_contact_block():
    sealed = seal_source_pack(_legacy_pack())
    article = """
    <p>Paid Advertorial: Compensation may be received if a purchase is made
    through links in this advertorial.</p>
    <h2><strong>Contact Information</strong></h2>
    <p>Scratch Off Fortune<br>
    Official product website</p>
    """
    ledger = build_article_claim_ledger(sealed, article)
    ids = {item["id"] for item in ledger["coverage_violations"]}

    assert "P-COVERAGE-CONTACT-SUPPORT_EMAIL" in ids
    assert "P-COVERAGE-CONTACT-SUPPORT_PHONE_US" in ids
    assert "P-COVERAGE-CONTACT-SUPPORT_PHONE_INTERNATIONAL" in ids
    assert "P-COVERAGE-CONTACT-ORDER_SUPPORT_URL" in ids
    assert "P-COVERAGE-CONTACT-OFFICIAL_URL" in ids
    assert "P-COVERAGE-REFUND-TERMS" in ids


def test_contact_coverage_gate_accepts_complete_clickable_contact_block():
    sealed = seal_source_pack(_legacy_pack())
    article = """
    <p>Paid Advertorial: Compensation may be received if a purchase is made
    through links in this advertorial.</p>
    <p>According to the seller, this is a digital entertainment offer.</p>
    <p>According to the seller, purchases carry a 60-day refund guarantee.</p>
    <h2><strong>Contact Information</strong></h2>
    <ul>
      <li><strong>Scratch Off Fortune</strong></li>
      <li>Product Support:
        <a href="mailto:support@scratch-fortune.com">support@scratch-fortune.com</a>
      </li>
      <li>Order Support: ClickBank</li>
      <li><a href="https://www.clkbank.com/">ClickBank Order Support</a></li>
      <li>U.S.: <a href="tel:+18003906035">1-800-390-6035</a></li>
      <li>International:
        <a href="tel:+12083454245">1-208-345-4245</a>
      </li>
      <li>Official Product Website:
        <a href="https://scratch-fortune.com/">scratch-fortune.com</a>
      </li>
    </ul>
    """
    ledger = build_article_claim_ledger(sealed, article)
    contact_violations = [
        item for item in ledger["coverage_violations"]
        if item["id"].startswith("P-COVERAGE-CONTACT")
        or item["id"] == "P-COVERAGE-REFUND-TERMS"
    ]

    assert contact_violations == []


@pytest.mark.parametrize(
    "product_type",
    ["topical", "food", "cannabis", "telehealth", "research_peptide"],
)
def test_structured_contact_contract_is_vertical_agnostic(product_type):
    raw = _legacy_pack()
    raw["product"]["product_type"] = product_type
    raw["product"]["category"] = product_type
    sealed = seal_source_pack(raw)
    rendered, report = ensure_structured_contact_block(
        sealed,
        "<h2>Offer Overview</h2><p>According to the seller, this offer is "
        "described in the supplied record.</p>",
    )
    ledger = build_article_claim_ledger(sealed, rendered)
    contact_violations = [
        item for item in ledger["coverage_violations"]
        if item["id"].startswith("P-COVERAGE-CONTACT")
        or item["id"] == "P-COVERAGE-REFUND-TERMS"
    ]

    assert report["field_count"] == 5
    assert contact_violations == []


def test_publication_repair_hides_only_raw_affiliate_url_anchors():
    article = """
    <p><a href="https://affiliate.example/offer">
      https://affiliate.example/offer
    </a></p>
    <h2>Contact Information</h2>
    <ul>
      <li>Order Support:
        <a href="https://www.clkbank.com/#!/?">
          https://www.clkbank.com/#!/?
        </a>
      </li>
      <li>Official Product Website:
        <a href="https://scratch-fortune.com/">
          https://scratch-fortune.com/
        </a>
      </li>
    </ul>
    """
    repaired = repair_publication_gates(
        article,
        "AccessNewsWire",
        "gaming",
        "https://affiliate.example/offer",
    )

    link_texts = {
        anchor.get_text(" ", strip=True)
        for anchor in BeautifulSoup(repaired, "html.parser").find_all("a")
    }
    assert "https://www.clkbank.com/#!/?" in link_texts
    assert "https://scratch-fortune.com/" in link_texts
    assert "https://affiliate.example/offer" not in link_texts
    assert "Review the current offer details" in repaired
