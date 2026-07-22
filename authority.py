"""
Source Intelligence — Source Authority Scoring
===============================================
Assigns confidence weights to claims based on source classification,
relationship to the offering, TLS verification status, and extraction method.

Higher authority sources (regulatory databases, peer-reviewed journals)
receive higher confidence than lower authority sources (user-generated,
anonymous). This scoring is used when reconciling conflicting claims
from different sources.
"""

from evidence import SourceClass, SourceRelationship


# Base confidence by source class (0.0 to 1.0)
SOURCE_CLASS_WEIGHTS = {
    SourceClass.REGULATORY_DATABASE: 0.95,
    SourceClass.PEER_REVIEWED: 0.90,
    SourceClass.INDEPENDENT_LAB: 0.85,
    SourceClass.NEWS_MEDIA: 0.60,
    SourceClass.OFFICIAL_VENDOR: 0.50,
    SourceClass.AUTHORIZED_RESELLER: 0.45,
    SourceClass.SOCIAL_PROFILE: 0.35,
    SourceClass.USER_GENERATED: 0.30,
    SourceClass.SEARCH_RESULT: 0.25,
    SourceClass.ANONYMOUS: 0.10,
}

# Multiplier by source relationship
RELATIONSHIP_MULTIPLIERS = {
    SourceRelationship.FIRST_PARTY: 1.0,
    SourceRelationship.SECOND_PARTY: 0.95,
    SourceRelationship.THIRD_PARTY: 0.90,
}

# Multiplier by extraction method
EXTRACTION_METHOD_MULTIPLIERS = {
    "api": 1.0,             # Structured API response (DSLD, PubMed)
    "manual": 0.95,         # Human-entered data
    "llm_extraction": 0.80, # LLM-based extraction from text
    "regex": 0.75,          # Pattern matching
    "machine_ocr": 0.60,    # OCR from label images — unverified
}

# TLS penalty: non-verified TLS reduces confidence
TLS_PENALTY = 0.15


def score_authority(source_class: SourceClass,
                    source_relationship: SourceRelationship = SourceRelationship.THIRD_PARTY,
                    tls_verified: bool = True,
                    extraction_method: str = "llm_extraction") -> float:
    """Calculate authority score for a source (0.0 to 1.0).

    Higher scores indicate more authoritative sources whose claims
    should be preferred when resolving conflicts.
    """
    base = SOURCE_CLASS_WEIGHTS.get(source_class, 0.10)
    relationship_mult = RELATIONSHIP_MULTIPLIERS.get(
        source_relationship, 0.90
    )
    extraction_mult = EXTRACTION_METHOD_MULTIPLIERS.get(
        extraction_method, 0.70
    )

    score = base * relationship_mult * extraction_mult

    if not tls_verified:
        score = max(0.0, score - TLS_PENALTY)

    return round(min(1.0, score), 3)


def compare_authority(score_a: float, score_b: float) -> str:
    """Compare two authority scores and return which should be preferred.

    Returns 'a', 'b', or 'equal'.
    """
    diff = abs(score_a - score_b)
    if diff < 0.05:
        return "equal"
    return "a" if score_a > score_b else "b"
