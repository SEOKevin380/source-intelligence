from product_manager import get_prompt_completeness


class FakeDatabase:
    def __init__(self, data):
        self.data = data

    def get_product(self, product_key):
        return {"research_data": self.data}


def test_financial_completeness_never_uses_health_fields():
    data = {
        "product": {
            "product_name": "Forecasts & Strategies",
            "official_url": "https://example.com/offer",
            "product_type": "financial",
            "refund_policy": {"duration_days": 30},
        },
        "all_artifacts": [{"id": "page"}, {"id": "vsl"}],
        "publication_claims": {"feature": [{"text": "Monthly newsletter"}]},
        "compliance": {"risk_level": "high"},
    }
    result = get_prompt_completeness("financial", FakeDatabase(data))
    labels = " ".join(
        key + " " + section["detail"] for key, section in result["sections"].items()
    ).lower()
    assert "ingredient" not in labels
    assert "pubmed" not in labels
    assert "clinical" not in labels
    assert {"Identity", "Sources", "Claims", "Compliance"}.issubset(result["sections"])


def test_supplement_completeness_keeps_health_scorecard():
    data = {
        "product": {
            "product_name": "Example Supplement",
            "official_url": "https://example.com",
            "product_type": "supplement",
            "supplement_facts": {"ingredients": [{"name": "Zinc"}]},
        },
        "compliance": {"risk_level": "low"},
    }
    result = get_prompt_completeness("supplement", FakeDatabase(data))
    assert "C1" in result["sections"]
    assert "C4" in result["sections"]

