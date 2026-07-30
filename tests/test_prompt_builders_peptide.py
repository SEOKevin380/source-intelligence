"""Regression coverage for the research-peptide publication prompt."""

from prompt_builders import (
    _build_cvd_source_block,
    build_l6_press_release_prompt,
)


def _peptide_data():
    return {
        "product": {
            "product_name": "Test Research Compound",
            "product_type": "research_peptide",
            "category": "Research Compound",
            "official_url": "https://example.com/research-compound",
            "peptide_sequence": "H-Ala-Gly-OH",
            "purity_percentage": "99.1%",
            "molecular_weight": "146.14 g/mol",
            "cas_number": "0000-00-0",
            "form": "lyophilized powder",
            "amount_per_vial": "5 mg",
            "storage_requirements": "Store at -20 C",
            "research_use_only_disclaimer": "Research use only.",
        },
        "compliance": {},
        "safety": {},
    }


def test_research_peptide_public_prompt_builds_with_one_c1_and_c19():
    prompt = build_l6_press_release_prompt(
        _peptide_data(),
        {
            "platform": "Barchart Advertorial",
            "affiliate_link": "TRAFFIC-FIRST",
            "previous_releases": "FIRST RELEASE",
        },
    )

    assert prompt.count("C1 — RESEARCH COMPOUND SPECIFICATIONS") == 1
    assert prompt.count("C19 — VIAL / STORAGE / RESEARCH-USE DETAILS") == 1
    assert "C19 — SERVING SIZE / SUPPLY DURATION" not in prompt
    assert "Peptide Sequence: H-Ala-Gly-OH" in prompt
    assert "Amount Per Vial: 5 mg" in prompt
    assert "HANDLING INCOMPLETE RESEARCH-COMPOUND DATA" in prompt
    assert (
        "Do not present research-use material as approved for human use."
        in prompt
    )


def test_research_peptide_missing_fields_are_not_established():
    data = _peptide_data()
    for key in (
        "peptide_sequence", "purity_percentage", "cas_number",
        "storage_requirements",
    ):
        data["product"].pop(key)

    block = _build_cvd_source_block(data)

    assert "Peptide Sequence: NOT ESTABLISHED" in block
    assert "Purity Percentage: NOT ESTABLISHED" in block
    assert "Cas Number: NOT ESTABLISHED" in block
    assert "Storage Requirements: NOT ESTABLISHED" in block


def test_research_peptide_conflicted_fields_remain_quarantined():
    data = _peptide_data()
    data["product"]["quarantined_fields"] = [
        "purity_percentage",
        "storage_requirements",
    ]

    block = _build_cvd_source_block(data)

    assert (
        "Purity Percentage: QUARANTINED — incompatible source values; "
        "omit from draft"
    ) in block
    assert (
        "Storage Requirements: QUARANTINED — incompatible source values; "
        "omit from draft"
    ) in block
    assert "Purity Percentage: 99.1%" not in block
    assert "Storage Requirements: Store at -20 C" not in block
