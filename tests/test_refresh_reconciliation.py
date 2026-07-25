from unittest.mock import patch


def test_phase1_can_extract_from_captured_artifact_without_refetching():
    from research_product import phase1_extract_product

    extracted = {
        "product_name": "Power Pro Genius",
        "product_type": "device",
        "shipping_policy": {"delivery_time": "10-12 business days"},
    }
    with patch("research_product._try_multiple_urls") as fetch, \
         patch("research_product.call_claude", return_value=__import__(
             "json"
         ).dumps(extracted)):
        result = phase1_extract_product(
            "https://powerprogenius.com/secure-bogo/",
            product_name="Power Pro Genius",
            source_html="<html><body>" + ("current offer " * 300) + "</body></html>",
        )

    fetch.assert_not_called()
    assert result["shipping_policy"]["delivery_time"] == (
        "10-12 business days"
    )


def test_official_refresh_replaces_stale_device_fields():
    from stage_handlers import _merge_product_data

    merged = _merge_product_data({
        "product_name": "Power Pro Genius",
        "official_url": "https://powerprogenius.com/secure-bogo/",
        "shipping_policy": {"delivery_time": "12-15 business days"},
        "warranty": "Old warranty language",
    }, {
        "shipping_policy": {"delivery_time": "10-12 business days"},
        "warranty": "Current warranty language",
    }, replace_existing=True)

    assert merged["shipping_policy"] == {
        "delivery_time": "10-12 business days"
    }
    assert merged["warranty"] == "Current warranty language"


def test_source_conflicts_quarantine_structured_product_values():
    from stage_handlers import _quarantine_conflicted_product_fields

    product = _quarantine_conflicted_product_fields({
        "product_name": "Power Pro Genius",
        "certifications": ["UL approved"],
        "shipping_policy": {"delivery_time": "12-15 business days"},
        "source_conflicts": [{
            "field": "certifications",
            "values": ["UL approved", "UL-recognized components"],
            "resolution": "unresolved — omit",
        }],
    }, {
        "conflicts": [{
            "fact_keys": ["shipping_policy"],
            "values": ["10-12 business days", "12-15 business days"],
            "source_artifact_ids": ["current", "stale"],
            "description": "Conflicting shipping policy",
        }],
    })

    assert "certifications" not in product
    assert "shipping_policy" not in product
    assert product["quarantined_fields"] == [
        "certifications",
        "shipping_policy",
    ]


def test_extractor_sanitizer_removes_contested_nested_value():
    from research_product import _sanitize_extracted_product_data

    result = _sanitize_extracted_product_data({
        "product_name": "Power Pro Genius",
        "specifications": {
            "coverage_per_unit": "800-1,200 sq ft",
            "results_timeline": "6-8 weeks",
        },
        "source_conflicts": [{
            "field": "specifications.results_timeline",
            "values": ["2-3 weeks", "6-8 weeks"],
            "resolution": "unresolved — omit",
        }],
    })

    assert result["specifications"] == {
        "coverage_per_unit": "800-1,200 sq ft"
    }
    assert result["source_conflicts"][0]["field"] == (
        "specifications.results_timeline"
    )
