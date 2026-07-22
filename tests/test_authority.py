"""Tests for authority.py — Source authority scoring."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from authority import score_authority, compare_authority
from evidence import SourceClass, SourceRelationship


class TestScoreAuthority:
    def test_regulatory_highest(self):
        """Regulatory databases should score highest."""
        reg = score_authority(SourceClass.REGULATORY_DATABASE)
        vendor = score_authority(SourceClass.OFFICIAL_VENDOR)
        user = score_authority(SourceClass.USER_GENERATED)
        assert reg > vendor > user

    def test_peer_reviewed_high(self):
        """Peer-reviewed sources score near the top."""
        peer = score_authority(SourceClass.PEER_REVIEWED)
        assert peer >= 0.60

    def test_anonymous_lowest(self):
        """Anonymous sources should score lowest."""
        anon = score_authority(SourceClass.ANONYMOUS)
        assert anon <= 0.15

    def test_tls_penalty(self):
        """Non-TLS sources should score lower."""
        tls_score = score_authority(SourceClass.OFFICIAL_VENDOR, tls_verified=True)
        no_tls_score = score_authority(SourceClass.OFFICIAL_VENDOR, tls_verified=False)
        assert tls_score > no_tls_score

    def test_first_party_vs_third_party(self):
        """First-party relationship should score >= third-party."""
        first = score_authority(
            SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY
        )
        third = score_authority(
            SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.THIRD_PARTY
        )
        assert first >= third

    def test_api_extraction_highest(self):
        """API extraction should be trusted most."""
        api = score_authority(
            SourceClass.REGULATORY_DATABASE,
            extraction_method="api"
        )
        ocr = score_authority(
            SourceClass.REGULATORY_DATABASE,
            extraction_method="machine_ocr"
        )
        assert api > ocr

    def test_ocr_low_confidence(self):
        """Machine OCR should produce lower confidence."""
        ocr = score_authority(
            SourceClass.OFFICIAL_VENDOR,
            extraction_method="machine_ocr"
        )
        llm = score_authority(
            SourceClass.OFFICIAL_VENDOR,
            extraction_method="llm_extraction"
        )
        assert llm > ocr

    def test_score_bounded(self):
        """Scores should always be between 0 and 1."""
        for sc in SourceClass:
            for sr in SourceRelationship:
                for tls in [True, False]:
                    score = score_authority(sc, sr, tls)
                    assert 0.0 <= score <= 1.0


class TestCompareAuthority:
    def test_clear_winner(self):
        assert compare_authority(0.95, 0.50) == "a"
        assert compare_authority(0.30, 0.90) == "b"

    def test_equal_scores(self):
        assert compare_authority(0.50, 0.52) == "equal"
        assert compare_authority(0.80, 0.80) == "equal"
