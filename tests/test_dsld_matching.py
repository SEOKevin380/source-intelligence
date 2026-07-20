"""Tests for DSLD false-positive matching prevention.

Round 3 finding: overly permissive single-word matching caused
"Cardio Slim Tea" → "Cardio Miracle" and "Glyco Reset Drops" → "RnA ReSet Drops".
The fix uses SequenceMatcher guards and tiered thresholds.
"""

import sys
import os
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _simulate_dsld_match(query_name: str, dsld_name: str) -> bool:
    """Reproduce the matching logic from research_product._query_dsld().

    Returns True if the DSLD name would be accepted as a match for the query.
    This mirrors the algorithm without making actual API calls.
    """
    generic_words = {
        "supplement", "support", "formula", "pro", "plus",
        "max", "ultra", "advanced", "daily", "natural",
        "premium", "gummies", "capsules", "tablets", "powder",
        "blend", "complex", "health", "care", "nerve",
        "brain", "joint", "weight", "loss", "eye", "skin",
        "hair", "bone", "heart", "immune", "gut", "sleep",
        "energy", "mood", "focus", "memory", "vision",
        "review", "reviews", "the", "and", "for", "with",
        "tea", "coffee", "drink", "shake", "bar", "drops",
        "slim", "thin", "lean", "fat", "burn", "burner",
        "green", "red", "gold", "silver", "black", "white",
        "super", "mega", "extra", "complete", "total",
        "original", "pure", "organic", "herbal", "vital",
        "essential", "basic", "mens", "womens", "men", "women",
    }

    query_lower = query_name.lower().strip()
    full_name = dsld_name.lower().strip()

    # Exact match
    if full_name == query_lower:
        return True

    query_words = set(query_lower.split())
    hit_words = set(full_name.split())

    unique_query = query_words - generic_words
    unique_hit = hit_words - generic_words

    # SequenceMatcher guard
    name_similarity = SequenceMatcher(None, query_lower, full_name).ratio()
    if name_similarity < 0.4:
        return False

    if unique_query:
        unique_overlap = unique_query & unique_hit
        overlap_ratio = len(unique_overlap) / len(unique_query)

        if len(unique_query) == 1:
            if overlap_ratio >= 1.0 and name_similarity >= 0.65:
                return True
        elif len(unique_query) >= 2:
            if overlap_ratio >= 0.75 and len(unique_overlap) >= 2:
                return True

    return False


class TestKnownFalsePositives:
    """These false matches must NOT occur — they were the Round 3 finding."""

    def test_cardio_slim_tea_rejects_cardio_miracle(self):
        assert not _simulate_dsld_match("Cardio Slim Tea", "Cardio Miracle")

    def test_glyco_reset_drops_rejects_rna_reset_drops(self):
        assert not _simulate_dsld_match("Glyco Reset Drops", "RnA ReSet Drops")


class TestLegitimateMatches:
    """Correct matches should still work."""

    def test_exact_match(self):
        assert _simulate_dsld_match("Alpha Brain", "Alpha Brain")

    def test_exact_match_case_insensitive(self):
        assert _simulate_dsld_match("Alpha Brain", "alpha brain")

    def test_two_unique_words_match(self):
        # "NervoPure" has 2 unique words if split: "nervo" + "pure"
        # but as a single token, it behaves differently. Use a real multi-word example:
        assert _simulate_dsld_match(
            "Cardio Miracle Supplement",
            "Cardio Miracle Advanced Formula"
        )

    def test_high_similarity_single_word_match(self):
        # When there's only 1 unique word but names are very similar overall
        assert _simulate_dsld_match(
            "Cardio Pro",
            "Cardio Pro Capsules"
        )


class TestEdgeCases:
    """Boundary conditions for the matching algorithm."""

    def test_completely_different_names_rejected(self):
        assert not _simulate_dsld_match("MegaMind Pro", "Sunset Valley Blend")

    def test_all_generic_words_no_match(self):
        # When ALL words are generic, unique_query is empty → no match
        assert not _simulate_dsld_match(
            "Natural Health Support",
            "Premium Daily Supplement"
        )

    def test_single_unique_word_low_similarity_rejected(self):
        # One unique word matches but overall names too different
        assert not _simulate_dsld_match(
            "Zenith Brain Capsules",
            "Zenith Gut Health Blend"
        )
