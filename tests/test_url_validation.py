"""Tests for SSRF protection and URL validation in net.py."""

import sys
import os
import pytest

# Add parent directory to path so we can import project modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from net import validate_url


class TestProtocolWhitelist:
    """Only http and https should be allowed."""

    def test_https_allowed(self):
        result = validate_url("https://example.com/page")
        assert result  # Returns normalized URL

    def test_http_allowed(self):
        result = validate_url("http://example.com/page")
        assert result

    def test_file_protocol_blocked(self):
        with pytest.raises(ValueError, match="unsupported protocol"):
            validate_url("file:///etc/passwd")

    def test_ftp_protocol_blocked(self):
        with pytest.raises(ValueError, match="unsupported protocol"):
            validate_url("ftp://example.com/file")

    def test_javascript_protocol_blocked(self):
        with pytest.raises(ValueError, match="unsupported protocol"):
            validate_url("javascript:alert(1)")

    def test_data_protocol_blocked(self):
        with pytest.raises(ValueError, match="unsupported protocol"):
            validate_url("data:text/html,<h1>test</h1>")


class TestPrivateIPBlocking:
    """Private, loopback, and reserved IPs must be blocked."""

    def test_loopback_ipv4_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url("http://127.0.0.1/admin")

    def test_loopback_localhost_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url("http://localhost/admin")

    def test_private_10_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url("http://10.0.0.1/internal")

    def test_private_172_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url("http://172.16.0.1/internal")

    def test_private_192_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url("http://192.168.1.1/router")

    def test_link_local_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url("http://169.254.1.1/link-local")


class TestCloudMetadataBlocking:
    """Cloud metadata endpoints must be blocked (AWS/GCP/Azure)."""

    def test_aws_metadata_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url("http://169.254.169.254/latest/meta-data/")

    def test_aws_metadata_with_path_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url("http://169.254.169.254/latest/api/token")


class TestEdgeCases:
    """Edge cases and malformed inputs."""

    def test_empty_string_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url("")

    def test_none_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url(None)

    def test_no_hostname_blocked(self):
        with pytest.raises(ValueError, match="Blocked"):
            validate_url("http://")

    def test_valid_public_url_passes(self):
        result = validate_url("https://www.google.com/search?q=test")
        assert "google.com" in result

    def test_valid_api_url_passes(self):
        result = validate_url("https://api.fda.gov/drug/event.json")
        assert "api.fda.gov" in result
