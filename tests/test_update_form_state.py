from source_pack_contract import form_values_from_pack, normalize_platform_label


def test_saved_accesswire_intake_is_restored_without_barchart_fallback():
    pack = {
        "product": {
            "product_name": "Forecasts & Strategies",
            "official_url": "https://example.com/official",
        },
        "intake_manifest": {
            "product_url": "https://example.com/official",
            "product_name": "Forecasts & Strategies",
            "publishing_channel": "Accesswire",
            "affiliate_link": "https://partner.example/offer",
            "vsl_url": "https://example.com/vsl",
            "label_source_url": "",
            "previous_releases": "FIRST RELEASE",
            "competitor_releases": "https://example.com/competitor",
            "client_locked_title": "Saved title",
            "operator_notes": "Saved notes",
        },
    }
    values = form_values_from_pack(pack)
    assert values["rd_platform"] == "Accesswire"
    assert values["rd_affiliate"] == "https://partner.example/offer"
    assert values["vsl_url"] == "https://example.com/vsl"
    assert values["rd_competitor"] == "https://example.com/competitor"
    assert values["rd_client_title"] == "Saved title"
    assert values["rd_notes"] == "Saved notes"


def test_platform_aliases_are_canonical_and_legacy_default_is_accesswire():
    assert normalize_platform_label("AccessNewsWire") == "Accesswire"
    assert normalize_platform_label("Barchart") == "Barchart Advertorial"
    assert normalize_platform_label("") == "Accesswire"


def test_legacy_pack_uses_product_identity_without_inventing_barchart():
    values = form_values_from_pack({
        "product": {
            "product_name": "Legacy Product",
            "official_url": "https://example.com",
        }
    })
    assert values["product_name"] == "Legacy Product"
    assert values["product_url"] == "https://example.com"
    assert values["rd_platform"] == "Accesswire"
