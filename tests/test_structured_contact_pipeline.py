import json

from article_provenance import build_article_claim_ledger
from newswire_workbench.prompts import writer_evidence_view
from prompt_builders import build_l6_press_release_prompt
from source_pack_contract import (
    form_values_from_pack,
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
