import gzip
import json

from body_exemplar_corpus import (
    build_cluster_playbooks,
    extract_article_body,
    format_body_playbook,
    heading_role,
    profile_article_body,
)


def _article():
    paragraphs = "".join(
        f"<p>Seller materials provide useful product information number {i}. "
        "Readers can compare the stated terms and decide what to verify.</p>"
        for i in range(40)
    )
    return f"""
    <html><body>
      <nav>Navigation noise</nav>
      <div class="article-content"><article>
        <p><strong>Paid Advertorial:</strong> A commission may be earned.</p>
        <p><strong>What Is Example Device and Who Is It For?</strong></p>
        {paragraphs}
        <p><strong>Pricing and Package Information</strong></p>
        <p><a href="https://example.com">Review the current offer details</a></p>
        <p><strong>Material Limitations</strong></p>
        <p><strong>Frequently Asked Questions</strong></p>
      </article></div>
      <footer>Footer noise</footer>
    </body></html>
    """


def test_extract_and_profile_flattened_newswire_body():
    body = extract_article_body(_article(), "barchart")
    assert "Navigation noise" not in body
    assert "Footer noise" not in body
    profile = profile_article_body(
        body,
        url="https://www.barchart.com/story/example",
        platform="barchart",
        niche="energy_devices",
        title="Example Device Review",
        product_name="Example Device",
    )
    assert profile["word_count"] >= 250
    assert profile["disclosure_paragraph_index"] == 0
    assert profile["cta_count"] == 1
    assert profile["has_limitations"] is True
    assert profile["has_faq"] is True
    assert "[PRODUCT]" in profile["heading_sequence"][0]


def test_cluster_playbook_contains_roles_not_historical_facts(tmp_path):
    body = extract_article_body(_article(), "barchart")
    profile = profile_article_body(
        body,
        url="https://www.barchart.com/story/example",
        platform="barchart",
        niche="energy_devices",
        title="Example Device Review",
        product_name="Example Device",
    )
    playbook = build_cluster_playbooks([profile])[
        "barchart::energy_devices"
    ]
    assert "pricing" in playbook["common_section_roles"]
    assert "Example Device" not in str(playbook["common_section_roles"])
    assert "sealed current-product pack" in playbook["fact_boundary"]


def test_heading_roles_are_fact_free():
    assert heading_role("How StopWatt Technology Works") == "mechanism"
    assert heading_role("Things Buyers Should Verify") == "buyer_checks"
    assert heading_role("Current Pricing and Refund Terms") == "pricing"


def test_formatted_playbook_exposes_structure_and_fact_boundary(tmp_path):
    path = tmp_path / "profiles.json.gz"
    payload = {
        "profiles": [],
        "clusters": {
            "barchart::energy_devices": {
                "sample_size": 3,
                "median_word_count": 1800,
                "median_heading_count": 8,
                "median_cta_count": 3,
                "common_section_roles": [
                    "overview", "features", "pricing", "buyer_checks",
                ],
                "fact_boundary": (
                    "Aggregate approved structure only. The sealed "
                    "current-product pack remains the exclusive factual authority."
                ),
            }
        },
    }
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    rendered = format_body_playbook(
        "barchart", "energy_devices", str(path)
    )
    assert "Profile sample: 3" in rendered
    assert "buyer checks" in rendered
    assert "exclusive factual authority" in rendered
