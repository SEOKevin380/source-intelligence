from exemplar_corpus import (
    build_approval_playbook,
    build_generation_blueprint,
    format_exemplar_guidance,
    infer_intents,
    infer_niche,
    infer_vertical,
    normalize_platform,
    retrieve_exemplars,
)


def test_approval_playbook_is_scoped_and_fact_safe():
    examples = [{
        "title_pattern": "[PRODUCT] Review [YEAR]: Buyer Guide",
        "intents": ["review", "trust"],
        "published_date": "2026-07-01",
        "live_url": "https://www.barchart.com/story/example",
    }]
    playbook = build_approval_playbook(
        examples, "Barchart Advertorial", "energy_devices"
    )
    assert playbook["platform"] == "barchart"
    assert playbook["niche"] == "energy_devices"
    assert "sealed sources" in playbook["fact_boundary"]


def test_platform_normalization_prefers_live_host():
    assert normalize_platform(
        "BARCHART", "https://www.accessnewswire.com/newsroom/en/example"
    ) == "accesswire"
    assert normalize_platform("ACCESWRE") == "accesswire"
    assert normalize_platform("Barchart Advertorial") == "barchart"


def test_vertical_and_intent_inference_are_universal():
    assert infer_vertical("Jim Woods Stock Investing Newsletter") == "financial"
    assert infer_vertical("Portable Smart Air Cooler Review") == "consumer_electronics"
    assert "trust" in infer_intents("Example Reviews: Scam or Legit?")
    assert infer_niche("EcoWatt Power Saver electricity review") == "energy_devices"
    assert infer_niche("Portable Air Cooler Review") == "cooling_devices"
    assert infer_niche("Stock Investing Newsletter") == "financial_newsletters"


def test_accesswire_financial_precedents_are_available():
    matches = retrieve_exemplars(
        "Forecasts & Strategies America's #1 Stock | Jim Woods",
        "Accesswire",
        "financial",
        source_url="https://jimwoodsinvesting.stockinvestor.com/offer/example",
    )
    assert matches
    assert all(item["platform"] == "accesswire" for item in matches)
    assert all(item["vertical"] == "financial" for item in matches)

    block = format_exemplar_guidance(matches)
    assert "SEO METADATA" in block
    assert "Never transfer names, prices, claims" in block


def test_blueprint_uses_actual_publisher_and_intent_specific_spine():
    pack = {
        "product": {
            "product_name": "Example Device",
            "product_type": "device",
            "category": "gadgets",
        },
        "intake_manifest": {"publishing_channel": "AccessNewsWire"},
        "contextual_source_profiles": [{
            "source_type": "previous_release",
            "title": "Example Device Features and Pricing",
            "headings": ["How It Works", "Price"],
        }],
    }
    blueprint = build_generation_blueprint(pack, [])
    assert "AccessNewsWire formatting" in blueprint
    assert "Barchart advertorial formatting" not in blueprint
    assert "Required H2 spine:" in blueprint
