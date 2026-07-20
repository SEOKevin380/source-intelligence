"""Tests for completeness scoring in database.py and product_manager.py.

Covers: COMPLETE (not VERIFIED) labels, C16-C19 local scoring,
cannabis partial scoring, human vs animal study weighting.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from database import ProductDatabase


@pytest.fixture
def tmp_db():
    """Create a temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = ProductDatabase(db_path=path)
    yield db
    db.close()
    os.unlink(path)


def _make_research(**overrides):
    """Build minimal research_data dict for scoring tests."""
    base = {
        "product": {
            "product_name": "Test Product",
            "brand_name": "Test Brand",
            "product_type": "supplement",
            "category": "brain",
            "supplement_facts": {
                "ingredients": [
                    {"name": "Vitamin D", "amount": "1000 IU"},
                    {"name": "Zinc", "amount": "15mg"},
                    {"name": "Magnesium", "amount": "200mg"},
                ]
            },
            "claims": [
                {"claim": "Supports brain health", "source": "sales_page"},
                {"claim": "Improves focus", "source": "sales_page"},
                {"claim": "Natural formula", "source": "sales_page"},
            ],
            "pricing": [
                {"amount": "$49.99", "unit": "bottle"},
                {"amount": "$39.99", "unit": "3 bottles"},
            ],
        },
        "ingredient_research": {},
        "safety": {},
        "compliance": {},
        "reputation": {},
    }
    for key, val in overrides.items():
        base[key] = val
    return base


class TestCompletenessScoreLabels:
    """Quality labels must say 'COMPLETE' not 'VERIFIED'."""

    def test_high_score_says_complete_not_verified(self, tmp_db):
        data = _make_research(
            ingredient_research={
                "Vitamin D": {
                    "studies": [
                        {"title": f"Study {i}", "relevance_tags": ["human_study"]}
                        for i in range(6)
                    ]
                }
            },
            safety={"Vitamin D": {"side_effects": "Generally safe"}},
            compliance={"risk_level": "Low"},
            reputation={"bbb_rating": "A+"},
        )
        score, flags = tmp_db.compute_completeness_score(data)
        assert score >= 80
        for f in flags:
            assert "VERIFIED" not in f, f"Found 'VERIFIED' in flag: {f}"

    def test_method_name_is_completeness(self):
        assert hasattr(ProductDatabase, "compute_completeness_score")
        assert not hasattr(ProductDatabase, "compute_quality_score")


class TestHumanVsAnimalStudyScoring:
    """Human studies should score higher than animal-only evidence."""

    def test_human_studies_get_full_marks(self, tmp_db):
        data_human = _make_research(
            ingredient_research={
                "Zinc": {
                    "studies": [
                        {"title": f"Human trial {i}", "relevance_tags": ["human_study"]}
                        for i in range(5)
                    ]
                }
            }
        )
        score_human, _ = tmp_db.compute_completeness_score(data_human)

        data_animal = _make_research(
            ingredient_research={
                "Zinc": {
                    "studies": [
                        {"title": f"Animal study {i}", "relevance_tags": ["animal_study"]}
                        for i in range(5)
                    ]
                }
            }
        )
        score_animal, _ = tmp_db.compute_completeness_score(data_animal)

        assert score_human > score_animal, (
            f"Human studies ({score_human}) should score higher than animal-only ({score_animal})"
        )

    def test_animal_only_scores_less_than_human(self, tmp_db):
        """5+ animal studies with 0 human should score less than 5+ human."""
        data_animal = _make_research(
            ingredient_research={
                "Zinc": {
                    "studies": [
                        {"title": f"Animal study {i}", "relevance_tags": ["animal_study"]}
                        for i in range(10)
                    ]
                }
            }
        )
        score_animal, _ = tmp_db.compute_completeness_score(data_animal)

        data_human = _make_research(
            ingredient_research={
                "Zinc": {
                    "studies": [
                        {"title": f"Study {i}", "relevance_tags": ["human_study"]}
                        for i in range(5)
                    ]
                }
            }
        )
        score_human, _ = tmp_db.compute_completeness_score(data_human)
        assert score_animal < score_human


class TestC16C19LocalScoring:
    """C16-C19 must use local scoring, not global accumulators."""

    def _insert_and_score(self, tmp_db, product_overrides):
        """Insert a product into the DB and score it via get_prompt_completeness."""
        from product_manager import get_prompt_completeness

        product = {
            "product_name": "Test Product",
            "brand_name": "Test Brand",
            "product_type": "supplement",
            "category": "brain",
            "supplement_facts": {"ingredients": [{"name": "Zinc"}]},
            "claims": [{"claim": "Helps focus"}],
        }
        product.update(product_overrides)
        research = {"product": product, "ingredient_research": {}, "safety": {}}
        tmp_db.upsert_product("test-c16", research)
        return get_prompt_completeness("test-c16", db=tmp_db)

    def test_c16_c19_partial_with_some_data(self, tmp_db):
        """Product with only shipping_policy should be 'partial', not 'complete'."""
        result = self._insert_and_score(tmp_db, {
            "shipping_policy": "Free shipping over $50",
        })
        c16_status = result["sections"]["C16-C19"]["status"]
        assert c16_status == "partial", (
            f"C16-C19 should be 'partial' with only shipping (3/10 pts), got '{c16_status}'"
        )

    def test_c16_c19_complete_with_all_data(self, tmp_db):
        result = self._insert_and_score(tmp_db, {
            "shipping_policy": "Free shipping",
            "warranty": "30-day guarantee",
            "testimonials": [{"text": "Great product"}],
            "brand_faqs": [{"q": "How to use?", "a": "Take daily"}],
        })
        c16_status = result["sections"]["C16-C19"]["status"]
        assert c16_status == "complete", (
            f"C16-C19 should be 'complete' with all data (10/10 pts), got '{c16_status}'"
        )


class TestCannabisPartialScoring:
    """Cannabis products should get partial (5pts), not full (10pts) for C6 safety."""

    def test_cannabis_c6_is_partial(self, tmp_db):
        from product_manager import get_prompt_completeness

        product = {
            "product_name": "CBD Calm Gummies",
            "brand_name": "Test Brand",
            "product_type": "cannabis",
            "category": "cannabis",
            "supplement_facts": {"ingredients": [{"name": "CBD", "amount": "25mg"}]},
            "claims": [{"claim": "Promotes calm"}],
        }
        research = {"product": product, "ingredient_research": {}, "safety": {}}
        tmp_db.upsert_product("cbd-calm-gummies", research)
        result = get_prompt_completeness("cbd-calm-gummies", db=tmp_db)
        c6_status = result["sections"]["C6"]["status"]
        assert c6_status == "partial", (
            f"Cannabis C6 should be 'partial', got '{c6_status}'"
        )
        assert "generic safety" in result["sections"]["C6"]["detail"].lower()
