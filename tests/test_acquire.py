"""Tests for acquire.py — Source-classified acquisition with validation."""

import hashlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acquire import Acquirer, AcquisitionError, _validate_fetch_result
from evidence import EvidenceLake, Artifact, SourceClass
from net import FetchResult


@pytest.fixture
def lake():
    """Create a temporary evidence lake."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    artifacts_dir = tempfile.mkdtemp()
    lk = EvidenceLake(db_path=path, artifacts_dir=artifacts_dir)
    yield lk
    if lk._conn:
        lk._conn.close()
    os.unlink(path)
    shutil.rmtree(artifacts_dir, ignore_errors=True)


@pytest.fixture
def acquirer(lake):
    """Create an Acquirer with test lake."""
    return Acquirer(lake, offering_id="test-offering", job_id="test-job")


def _good_fetch_result(url="https://example.com/product"):
    """Create a valid FetchResult with real content."""
    content = b"<html><body><h1>Product Page</h1><p>Real content here</p></body></html>"
    return FetchResult(
        content=content,
        text="Product Page\nReal content here",
        final_url=url,
        status_code=200,
        headers={"Content-Type": "text/html"},
        fetched_at="2026-07-22T12:00:00+00:00",
        content_hash=hashlib.sha256(content).hexdigest(),
        content_length=len(content),
        tls_verified=True,
        elapsed_ms=150.0,
        error="",
    )


def _empty_fetch_result(url="https://example.com/missing"):
    """Create a FetchResult with empty content."""
    return FetchResult(
        content=b"",
        text="",
        final_url=url,
        status_code=200,
        headers={},
        fetched_at="2026-07-22T12:00:00+00:00",
        content_hash=hashlib.sha256(b"").hexdigest(),
        content_length=0,
        tls_verified=True,
        elapsed_ms=100.0,
        error="",
    )


def _error_fetch_result(url="https://example.com/error"):
    """Create a FetchResult with an error."""
    return FetchResult(
        content=b"",
        text="",
        final_url=url,
        status_code=0,
        headers={},
        fetched_at="2026-07-22T12:00:00+00:00",
        content_hash=hashlib.sha256(b"").hexdigest(),
        content_length=0,
        tls_verified=False,
        elapsed_ms=0.0,
        error="Connection refused",
    )


def _http_error_fetch_result(url="https://example.com/404"):
    """Create a FetchResult with HTTP 404."""
    content = b"Not Found"
    return FetchResult(
        content=content,
        text="Not Found",
        final_url=url,
        status_code=404,
        headers={},
        fetched_at="2026-07-22T12:00:00+00:00",
        content_hash=hashlib.sha256(content).hexdigest(),
        content_length=len(content),
        tls_verified=True,
        elapsed_ms=50.0,
        error="",
    )


class TestValidateFetchResult:
    def test_valid_result_passes(self):
        _validate_fetch_result(_good_fetch_result(), "https://example.com")

    def test_error_result_raises(self):
        with pytest.raises(AcquisitionError, match="Connection refused"):
            _validate_fetch_result(_error_fetch_result(), "https://example.com")

    def test_empty_content_raises(self):
        with pytest.raises(AcquisitionError, match="empty content"):
            _validate_fetch_result(_empty_fetch_result(), "https://example.com")

    def test_http_404_raises(self):
        with pytest.raises(AcquisitionError, match="HTTP 404"):
            _validate_fetch_result(_http_error_fetch_result(), "https://example.com")

    def test_http_500_raises(self):
        result = _good_fetch_result()
        result.status_code = 500
        with pytest.raises(AcquisitionError, match="HTTP 500"):
            _validate_fetch_result(result, "https://example.com")

    def test_whitespace_only_text_raises(self):
        result = _good_fetch_result()
        result.text = "   \n\t  "
        with pytest.raises(AcquisitionError, match="no extractable text"):
            _validate_fetch_result(result, "https://example.com")


class TestAcquirerSuccess:
    @patch("net.safe_fetch")
    def test_official_page_stores_and_returns(self, mock_fetch, acquirer, lake):
        mock_fetch.return_value = _good_fetch_result()
        aid, text = acquirer.fetch_official_page("https://example.com/product")
        assert aid  # non-empty artifact ID
        assert "Real content" in text
        # Verify stored in lake
        artifact = lake.get(aid)
        assert artifact is not None
        assert artifact.source_class == SourceClass.OFFICIAL_VENDOR

    @patch("net.safe_fetch")
    def test_regulatory_stores_correctly(self, mock_fetch, acquirer, lake):
        mock_fetch.return_value = _good_fetch_result()
        aid, text = acquirer.fetch_regulatory(
            "https://dsld.od.nih.gov/api/test", source_name="DSLD"
        )
        artifact = lake.get(aid)
        assert artifact.source_class == SourceClass.REGULATORY_DATABASE

    @patch("net.safe_fetch")
    def test_third_party_stores_correctly(self, mock_fetch, acquirer, lake):
        mock_fetch.return_value = _good_fetch_result()
        aid, text = acquirer.fetch_third_party("https://reviews.com/test")
        artifact = lake.get(aid)
        assert artifact.source_class == SourceClass.USER_GENERATED


class TestAcquirerRejection:
    @patch("net.safe_fetch")
    def test_empty_content_raises_acquisition_error(self, mock_fetch, acquirer):
        mock_fetch.return_value = _empty_fetch_result()
        with pytest.raises(AcquisitionError, match="empty content"):
            acquirer.fetch_official_page("https://example.com/missing")

    @patch("net.safe_fetch")
    def test_error_fetch_raises_acquisition_error(self, mock_fetch, acquirer):
        mock_fetch.return_value = _error_fetch_result()
        with pytest.raises(AcquisitionError, match="Connection refused"):
            acquirer.fetch_official_page("https://example.com/error")

    @patch("net.safe_fetch")
    def test_http_404_raises_acquisition_error(self, mock_fetch, acquirer):
        mock_fetch.return_value = _http_error_fetch_result()
        with pytest.raises(AcquisitionError, match="HTTP 404"):
            acquirer.fetch_official_page("https://example.com/404")

    @patch("net.safe_fetch")
    def test_failed_artifact_still_stored_for_audit(self, mock_fetch, acquirer, lake):
        """Failed fetches store an audit record but raise AcquisitionError."""
        mock_fetch.return_value = _error_fetch_result()
        with pytest.raises(AcquisitionError) as exc_info:
            acquirer.fetch_official_page("https://example.com/error")

        # The failed artifact should be stored for audit trail
        stored = lake.list_for_offering("test-offering")
        assert len(stored) >= 1
        failed = stored[0]
        assert "FAILED" in failed.notes

    @patch("net.safe_fetch")
    def test_regulatory_rejects_empty(self, mock_fetch, acquirer):
        mock_fetch.return_value = _empty_fetch_result()
        with pytest.raises(AcquisitionError):
            acquirer.fetch_regulatory("https://dsld.od.nih.gov/api/test")

    @patch("net.safe_fetch")
    def test_peer_reviewed_rejects_error(self, mock_fetch, acquirer):
        mock_fetch.return_value = _error_fetch_result()
        with pytest.raises(AcquisitionError):
            acquirer.fetch_peer_reviewed("https://pubmed.ncbi.nlm.nih.gov/12345")

    @patch("net.safe_fetch")
    def test_third_party_rejects_empty(self, mock_fetch, acquirer):
        mock_fetch.return_value = _empty_fetch_result()
        with pytest.raises(AcquisitionError):
            acquirer.fetch_third_party("https://reviews.com/test")

    @patch("net.safe_fetch")
    def test_subpage_rejects_error(self, mock_fetch, acquirer):
        mock_fetch.return_value = _error_fetch_result()
        with pytest.raises(AcquisitionError):
            acquirer.fetch_official_subpage("https://example.com/shipping")


class TestArtifactIsUsable:
    def test_normal_artifact_is_usable(self):
        a = Artifact(
            artifact_id="abc123",
            content_length=500,
            status_code=200,
        )
        assert a.is_usable is True

    def test_error_artifact_not_usable(self):
        a = Artifact(
            artifact_id="abc123",
            error="Connection refused",
            content_length=0,
        )
        assert a.is_usable is False

    def test_failed_notes_not_usable(self):
        a = Artifact(
            artifact_id="abc123",
            notes="FAILED: empty content",
            content_length=0,
        )
        assert a.is_usable is False

    def test_empty_content_not_usable(self):
        a = Artifact(
            artifact_id="abc123",
            content_length=0,
            content_inline="",
        )
        assert a.is_usable is False

    def test_http_500_not_usable(self):
        a = Artifact(
            artifact_id="abc123",
            status_code=500,
            content_length=100,
        )
        assert a.is_usable is False

    def test_http_403_not_usable(self):
        a = Artifact(
            artifact_id="abc123",
            status_code=403,
            content_length=100,
        )
        assert a.is_usable is False

    def test_status_zero_with_content_is_usable(self):
        """Status 0 (unknown) with content is usable — browser fetches may not report status."""
        a = Artifact(
            artifact_id="abc123",
            status_code=0,
            content_length=500,
        )
        assert a.is_usable is True
