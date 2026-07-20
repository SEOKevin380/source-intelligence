"""
Hardened Network Fetching Layer
================================
Single module for ALL HTTP fetching in the Source Intelligence Tool.
Provides URL validation, streaming downloads, redirect validation,
and provenance metadata on every fetch.

Usage:
    from net import safe_fetch, safe_download, validate_url, FetchResult

    # Text fetch with provenance
    result = safe_fetch("https://example.com/page")
    print(result.text, result.fetched_at, result.content_hash)

    # Binary download
    result = safe_download("https://example.com/image.jpg", "/tmp/image.jpg")

    # Legacy compatibility (drop-in for old fetch_url)
    from net import fetch_text
    html = fetch_text("https://example.com/page")
"""

import hashlib
import ipaddress
import os
import socket
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ============================================================================
# Configuration
# ============================================================================

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_CHUNK_SIZE = 8192  # 8KB streaming chunks

# Cloud metadata endpoints to block
_METADATA_IPS = {
    "169.254.169.254",  # AWS/GCP/Azure metadata
    "fd00:ec2::254",    # AWS IPv6 metadata
}


# ============================================================================
# FetchResult dataclass — provenance metadata on every fetch
# ============================================================================

@dataclass
class FetchResult:
    """Result of a network fetch with full provenance metadata."""
    content: bytes = b""
    text: str = ""
    final_url: str = ""
    status_code: int = 0
    headers: dict = field(default_factory=dict)
    fetched_at: str = ""       # ISO 8601 UTC
    content_hash: str = ""     # SHA-256 hex digest
    content_length: int = 0
    tls_verified: bool = True
    elapsed_ms: float = 0.0
    error: str = ""


# ============================================================================
# URL Validation — blocks SSRF, private IPs, dangerous protocols
# ============================================================================

def validate_url(url: str) -> str:
    """Validate URL is safe for server-side fetching.

    Checks:
    - Protocol whitelist (HTTP/HTTPS only)
    - Resolves hostname, blocks private/reserved/loopback/link-local IPs
    - Blocks cloud metadata endpoints (169.254.169.254)

    Returns:
        Normalized URL string.

    Raises:
        ValueError: If URL is unsafe for server-side fetching.
    """
    if not url or not isinstance(url, str):
        raise ValueError("Blocked: empty or non-string URL")

    parsed = urllib.parse.urlparse(url.strip())

    # Protocol whitelist
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Blocked: unsupported protocol '{parsed.scheme}' "
            f"(only http/https allowed)"
        )

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Blocked: no hostname in URL")

    # Block cloud metadata by hostname
    if hostname in _METADATA_IPS:
        raise ValueError(f"Blocked: cloud metadata endpoint {hostname}")

    # Check if hostname is a raw IP address
    try:
        addr = ipaddress.ip_address(hostname)
        _check_ip_safety(addr, hostname)
    except ValueError as ve:
        if "Blocked:" in str(ve):
            raise
        # Not a raw IP — it's a domain name. Resolve and check all IPs.
        _resolve_and_check(hostname)

    return url.strip()


def _check_ip_safety(addr, display_name: str):
    """Raise ValueError if IP address is private/reserved/loopback/link-local."""
    if addr.is_private:
        raise ValueError(f"Blocked: private IP {display_name}")
    if addr.is_loopback:
        raise ValueError(f"Blocked: loopback IP {display_name}")
    if addr.is_reserved:
        raise ValueError(f"Blocked: reserved IP {display_name}")
    if addr.is_link_local:
        raise ValueError(f"Blocked: link-local IP {display_name}")
    # Check metadata IPs by string
    if str(addr) in _METADATA_IPS:
        raise ValueError(f"Blocked: cloud metadata endpoint {display_name}")


def _resolve_and_check(hostname: str):
    """Resolve hostname via DNS and check all resulting IPs for safety."""
    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError(f"Blocked: cannot resolve hostname '{hostname}'")

    for family, _, _, _, sockaddr in results:
        ip = ipaddress.ip_address(sockaddr[0])
        _check_ip_safety(ip, f"{hostname} (resolves to {sockaddr[0]})")


# ============================================================================
# safe_fetch — streaming text/HTML fetch with provenance
# ============================================================================

def safe_fetch(
    url: str,
    *,
    max_bytes: int = 2_000_000,
    timeout: int = 20,
    verify_tls: bool = True,
    allow_tls_fallback: bool = False,
    user_agent: Optional[str] = None,
) -> FetchResult:
    """Fetch URL content with streaming reads and full provenance.

    Args:
        url: URL to fetch (validated before any network request).
        max_bytes: Maximum bytes to read (streams in 8KB chunks).
        timeout: Connection timeout in seconds.
        verify_tls: Whether to verify TLS certificates.
        allow_tls_fallback: If True and TLS verification fails, retry
            without verification. Only for vendor page scraping.
        user_agent: Custom User-Agent header.

    Returns:
        FetchResult with content, provenance metadata, and any error.
    """
    if not url:
        return FetchResult(error="Empty URL")

    # Validate URL before any network request
    try:
        url = validate_url(url)
    except ValueError as e:
        return FetchResult(error=str(e))

    ua = user_agent or _DEFAULT_USER_AGENT
    start = time.monotonic()
    fetched_at = datetime.now(timezone.utc).isoformat()

    try:
        content, status_code, headers, final_url, tls_ok = _do_fetch(
            url, max_bytes=max_bytes, timeout=timeout,
            verify_tls=verify_tls, user_agent=ua,
        )
    except ssl.SSLError:
        if allow_tls_fallback and verify_tls:
            # Retry without TLS verification for vendor pages
            try:
                content, status_code, headers, final_url, tls_ok = _do_fetch(
                    url, max_bytes=max_bytes, timeout=timeout,
                    verify_tls=False, user_agent=ua,
                )
            except Exception as e:
                elapsed = (time.monotonic() - start) * 1000
                return FetchResult(
                    fetched_at=fetched_at, elapsed_ms=elapsed,
                    error=f"TLS fallback also failed: {e}",
                )
        else:
            elapsed = (time.monotonic() - start) * 1000
            return FetchResult(
                fetched_at=fetched_at, elapsed_ms=elapsed,
                error=f"TLS verification failed for {url}",
            )
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return FetchResult(
            fetched_at=fetched_at, elapsed_ms=elapsed,
            error=f"Fetch failed: {e}",
        )

    # Validate redirect destination
    if final_url and final_url != url:
        try:
            validate_url(final_url)
        except ValueError as e:
            elapsed = (time.monotonic() - start) * 1000
            return FetchResult(
                fetched_at=fetched_at, elapsed_ms=elapsed,
                error=f"Redirect blocked: {e}",
            )

    elapsed = (time.monotonic() - start) * 1000
    text = content.decode("utf-8", errors="ignore")
    content_hash = hashlib.sha256(content).hexdigest()

    return FetchResult(
        content=content,
        text=text,
        final_url=final_url or url,
        status_code=status_code,
        headers=dict(headers) if headers else {},
        fetched_at=fetched_at,
        content_hash=content_hash,
        content_length=len(content),
        tls_verified=tls_ok,
        elapsed_ms=round(elapsed, 1),
    )


def _do_fetch(url, *, max_bytes, timeout, verify_tls, user_agent):
    """Internal: perform the actual HTTP fetch with streaming reads."""
    if verify_tls:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        # Stream in chunks instead of reading entire response
        chunks = []
        bytes_read = 0
        while bytes_read < max_bytes:
            chunk = resp.read(min(_CHUNK_SIZE, max_bytes - bytes_read))
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)

        content = b"".join(chunks)
        status_code = resp.status
        headers = resp.headers
        final_url = resp.url

    return content, status_code, headers, final_url, verify_tls


# ============================================================================
# safe_download — binary file download with provenance
# ============================================================================

def safe_download(
    url: str,
    dest_path: str,
    *,
    max_bytes: int = 10_000_000,
    timeout: int = 30,
    user_agent: Optional[str] = None,
) -> FetchResult:
    """Download a binary file (image, PDF) with URL validation and TLS.

    Always verifies TLS (no fallback for downloads).

    Args:
        url: URL to download.
        dest_path: Local file path to save to.
        max_bytes: Maximum bytes to download (default 10MB).
        timeout: Connection timeout in seconds.
        user_agent: Custom User-Agent header.

    Returns:
        FetchResult with content bytes and provenance metadata.
    """
    if not url:
        return FetchResult(error="Empty URL")

    try:
        url = validate_url(url)
    except ValueError as e:
        return FetchResult(error=str(e))

    ua = user_agent or _DEFAULT_USER_AGENT
    start = time.monotonic()
    fetched_at = datetime.now(timezone.utc).isoformat()

    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": ua})

        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            chunks = []
            bytes_read = 0
            while bytes_read < max_bytes:
                chunk = resp.read(min(_CHUNK_SIZE, max_bytes - bytes_read))
                if not chunk:
                    break
                chunks.append(chunk)
                bytes_read += len(chunk)

            content = b"".join(chunks)
            final_url = resp.url
            status_code = resp.status
            headers = resp.headers

        # Validate redirect destination
        if final_url and final_url != url:
            validate_url(final_url)

        # Write to disk
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(content)

        elapsed = (time.monotonic() - start) * 1000
        content_hash = hashlib.sha256(content).hexdigest()

        return FetchResult(
            content=content,
            text="",
            final_url=final_url or url,
            status_code=status_code,
            headers=dict(headers) if headers else {},
            fetched_at=fetched_at,
            content_hash=content_hash,
            content_length=len(content),
            tls_verified=True,
            elapsed_ms=round(elapsed, 1),
        )

    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return FetchResult(
            fetched_at=fetched_at, elapsed_ms=elapsed,
            error=f"Download failed: {e}",
        )


# ============================================================================
# fetch_text — backward-compatible drop-in for old fetch_url()
# ============================================================================

def fetch_text(url: str, max_bytes: int = 60000) -> str:
    """Backward-compatible text fetch. Returns text or empty string.

    This is a drop-in replacement for the old fetch_url() function.
    Uses safe_fetch() internally with TLS fallback enabled for vendor pages.
    """
    result = safe_fetch(
        url, max_bytes=max_bytes,
        allow_tls_fallback=True,
    )
    return result.text
