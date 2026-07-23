from product_manager import get_prompt_completeness, get_relevant_primary_sites


def test_financial_routing_never_recommends_medical_primary_sites():
    sites = get_relevant_primary_sites("financial", "Financial Products")
    assert sites == {"totalhealthrd"}
    assert "pvmedcenter" not in sites
    assert "tutelamedical" not in sites


def test_general_consumer_routing_uses_consumer_sites():
    sites = get_relevant_primary_sites("consumer_electronics", "Tech & Gadgets")
    assert sites == {"totalhealthrd", "hollyherman"}


def test_health_routing_remains_archetype_driven():
    assert get_relevant_primary_sites("supplement", "heart_health") is None


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
    assert {
        "Who", "What", "Where", "When", "Why", "How", "How Much",
        "Trust / Scam Questions",
    }.issubset(result["sections"])
    assert result["questions_total"] == 8
    assert 0 < result["questions_answered"] <= 8


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
