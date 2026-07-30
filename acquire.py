"""
Source Intelligence — Source-Classified Acquisition Layer
=========================================================
Wraps the existing net.py hardened fetching and browser_fetch.py rendering
to store every acquisition in the evidence lake with proper source classification.

Every URL fetch goes through here instead of directly through fetch_url().
This ensures:
1. Every artifact is stored immutably with provenance
2. Source boundaries are preserved (official vs third-party)
3. Authority classification is assigned at acquisition time
4. Content is never blended before extraction
"""

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

from evidence import (
    EvidenceLake, Artifact, SourceClass, SourceRelationship, ArtifactType,
)


class AcquisitionError(Exception):
    """Raised when an artifact acquisition fails or produces unusable evidence.

    The failed/empty artifact is still stored in the evidence lake for audit
    trail purposes, but the caller knows not to treat it as valid evidence.
    """
    def __init__(self, message: str, artifact_id: str = ""):
        super().__init__(message)
        self.artifact_id = artifact_id  # ID of the stored failure record


_AUTHORITY_HOSTS = {
    "regulatory_allowlisted": (
        "api.fda.gov",
        "clinicaltrials.gov",
        "dsld.od.nih.gov",
        "fda.gov",
        "ftc.gov",
        "ods.od.nih.gov",
        "sec.gov",
    ),
    "peer_reviewed_allowlisted": (
        "eutils.ncbi.nlm.nih.gov",
        "ncbi.nlm.nih.gov",
        "pubmed.ncbi.nlm.nih.gov",
    ),
}

# Ephemeral, process-local capability key.  A corroboration-capable capture proof
# exists only long enough to cross the validated Acquirer -> EvidenceLake
# boundary; it is not persisted and is intentionally not a public API.
_AUTHORITY_CAPTURE_CAPABILITY_KEY = secrets.token_bytes(32)
_AUTHORITY_CAPTURE_CAPABILITY_VERSION = 1


def _authority_capture_material(artifact: Artifact, content: bytes) -> dict:
    """Return the exact acquisition result covered by the internal capability."""
    return {
        "version": _AUTHORITY_CAPTURE_CAPABILITY_VERSION,
        "content_sha256": hashlib.sha256(bytes(content or b"")).hexdigest(),
        "source_url": str(artifact.source_url or "").strip(),
        "final_url": str(artifact.final_url or "").strip(),
        "source_class": artifact.source_class.value,
        "source_relationship": artifact.source_relationship.value,
        "captured_at": str(artifact.captured_at or "").strip(),
        "status_code": int(artifact.status_code or 0),
        "tls_verified": bool(artifact.tls_verified),
        "offering_id": str(artifact.offering_id or "").strip(),
        "job_id": str(artifact.job_id or "").strip(),
        "acquisition_phase": str(artifact.acquisition_phase or "").strip(),
        "capture_route": str(artifact.capture_route or "").strip(),
        "corroboration_eligible": bool(artifact.corroboration_eligible),
    }


def _mint_authority_capture_capability(
    artifact: Artifact,
    content: bytes,
) -> dict:
    """Mint a proof only after the authority route and fetch have validated."""
    route = str(artifact.capture_route or "").strip()
    expected = {
        "regulatory_allowlisted": SourceClass.REGULATORY_DATABASE,
        "peer_reviewed_allowlisted": SourceClass.PEER_REVIEWED,
    }.get(route)
    if (
        expected is None
        or artifact.source_class != expected
        or artifact.source_relationship != SourceRelationship.THIRD_PARTY
        or artifact.corroboration_eligible is not True
        or artifact.tls_verified is not True
        or not 200 <= int(artifact.status_code or 0) < 400
        or not content
    ):
        raise AcquisitionError(
            "Authority capture capability requires a validated successful fetch"
        )
    _validate_authority_url(artifact.source_url, route)
    _validate_authority_url(artifact.final_url or artifact.source_url, route)
    material = _authority_capture_material(artifact, content)
    serialized = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return {
        "version": _AUTHORITY_CAPTURE_CAPABILITY_VERSION,
        "material_sha256": hashlib.sha256(serialized).hexdigest(),
        "mac": hmac.new(
            _AUTHORITY_CAPTURE_CAPABILITY_KEY,
            serialized,
            hashlib.sha256,
        ).hexdigest(),
    }


def _verify_authority_capture_capability(
    capability,
    artifact: Artifact,
    content: bytes,
) -> bool:
    """Verify the process-local acquisition capability at the storage boundary."""
    if not isinstance(capability, dict):
        return False
    if capability.get("version") != _AUTHORITY_CAPTURE_CAPABILITY_VERSION:
        return False
    material = _authority_capture_material(artifact, content)
    serialized = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    expected_hash = hashlib.sha256(serialized).hexdigest()
    expected_mac = hmac.new(
        _AUTHORITY_CAPTURE_CAPABILITY_KEY,
        serialized,
        hashlib.sha256,
    ).hexdigest()
    return (
        hmac.compare_digest(
            str(capability.get("material_sha256") or ""),
            expected_hash,
        )
        and hmac.compare_digest(
            str(capability.get("mac") or ""),
            expected_mac,
        )
    )


def _normalized_host(url: str) -> str:
    host = str(urlparse(str(url or "")).hostname or "").strip().casefold()
    if host.startswith("www."):
        host = host[4:]
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _validate_authority_url(url: str, route: str) -> None:
    """Reject classification-by-caller before any authority capture."""
    parsed = urlparse(str(url or "").strip())
    host = _normalized_host(url)
    allowed = _AUTHORITY_HOSTS.get(route, ())
    if (
        parsed.scheme != "https"
        or not host
        or not any(
            host == authority or host.endswith("." + authority)
            for authority in allowed
        )
    ):
        raise AcquisitionError(
            f"{route} requires an allowlisted HTTPS authority URL"
        )


def _validate_fetch_result(result, url: str) -> None:
    """Validate that a FetchResult contains usable content.

    Raises AcquisitionError if the fetch failed or returned empty content.
    A valid artifact must have:
    - A successful HTTP status (200-299) or at least non-zero status
    - Non-empty content
    - No fatal error
    """
    if result.error:
        raise AcquisitionError(
            f"Fetch failed for {url}: {result.error}"
        )
    if result.status_code and not (200 <= result.status_code < 400):
        raise AcquisitionError(
            f"Fetch returned HTTP {result.status_code} for {url}"
        )
    if not result.content or len(result.content) == 0:
        raise AcquisitionError(
            f"Fetch returned empty content for {url}"
        )
    if not result.text or len(result.text.strip()) == 0:
        raise AcquisitionError(
            f"Fetch returned no extractable text for {url}"
        )


def _image_format(image_data: bytes) -> str:
    """Identify the small set of image formats accepted for label OCR."""
    payload = bytes(image_data or b"")
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if (
        len(payload) >= 12
        and payload[:4] == b"RIFF"
        and payload[8:12] == b"WEBP"
    ):
        return "webp"
    return ""


class Acquirer:
    """Acquires web artifacts with source classification and stores in evidence lake.

    Each fetch method classifies the source and stores the artifact immutably.
    Returns (artifact_id, text_content) so callers can reference the stored artifact.
    """

    def __init__(self, lake: EvidenceLake, offering_id: str, job_id: str = ""):
        self.lake = lake
        self.offering_id = offering_id
        self.job_id = job_id

    def fetch_official_page(self, url: str,
                            phase: str = "ACQUIRE") -> Tuple[str, str]:
        """Fetch the official vendor page and store as first-party artifact.

        Returns (artifact_id, text_content).
        Raises AcquisitionError if the fetch fails or returns empty content.
        """
        from net import safe_fetch
        result = safe_fetch(url, max_bytes=120_000, allow_tls_fallback=False)

        artifact = Artifact.from_fetch_result(
            result, source_url=url,
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            capture_route="official_page",
        )

        try:
            _validate_fetch_result(result, url)
        except AcquisitionError as e:
            # Store failed artifact for audit trail, then raise
            artifact.notes = f"FAILED: {e}"
            self.lake.store(artifact, result.content or b"")
            e.artifact_id = artifact.artifact_id
            raise

        aid = self.lake.store(artifact, result.content)
        return aid, result.text

    def fetch_official_subpage(self, url: str, page_name: str = "",
                               phase: str = "ACQUIRE") -> Tuple[str, str]:
        """Fetch a subpage from the official vendor site.

        Returns (artifact_id, text_content).
        Raises AcquisitionError if the fetch fails or returns empty content.
        """
        from net import safe_fetch
        result = safe_fetch(url, max_bytes=60_000, allow_tls_fallback=False)
        artifact = Artifact.from_fetch_result(
            result, source_url=url,
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            notes=f"Subpage: {page_name}" if page_name else "",
            capture_route="official_subpage",
        )

        try:
            _validate_fetch_result(result, url)
        except AcquisitionError as e:
            artifact.notes = f"FAILED: {e}"
            self.lake.store(artifact, result.content or b"")
            e.artifact_id = artifact.artifact_id
            raise

        aid = self.lake.store(artifact, result.content)
        return aid, result.text

    def fetch_regulatory(self, url: str, source_name: str = "",
                         phase: str = "ACQUIRE") -> Tuple[str, str]:
        """Fetch from a regulatory/scientific source (DSLD, PubMed, FDA CAERS).

        Returns (artifact_id, text_content).
        TLS fallback is disabled — regulatory sources must have valid certificates.
        Raises AcquisitionError if the fetch fails or returns empty content.
        """
        _validate_authority_url(url, "regulatory_allowlisted")
        from net import safe_fetch
        result = safe_fetch(url, max_bytes=200_000, allow_tls_fallback=False)
        _validate_authority_url(
            result.final_url or url,
            "regulatory_allowlisted",
        )
        artifact = Artifact.from_fetch_result(
            result, source_url=url,
            source_class=SourceClass.REGULATORY_DATABASE,
            source_relationship=SourceRelationship.THIRD_PARTY,
            artifact_type=ArtifactType.API_RESPONSE,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            notes=source_name,
            capture_route="regulatory_allowlisted",
            corroboration_eligible=True,
        )

        try:
            _validate_fetch_result(result, url)
        except AcquisitionError as e:
            artifact.notes = f"FAILED: {e}"
            artifact.corroboration_eligible = False
            self.lake.store(artifact, result.content or b"")
            e.artifact_id = artifact.artifact_id
            raise

        authority_capability = _mint_authority_capture_capability(
            artifact,
            result.content,
        )
        aid = self.lake.store(
            artifact,
            result.content,
            authority_capability=authority_capability,
        )
        return aid, result.text

    def fetch_peer_reviewed(self, url: str, source_name: str = "",
                            phase: str = "RESEARCH") -> Tuple[str, str]:
        """Fetch from a peer-reviewed source (PubMed abstract, journal page).

        Returns (artifact_id, text_content).
        Raises AcquisitionError if the fetch fails or returns empty content.
        """
        _validate_authority_url(url, "peer_reviewed_allowlisted")
        from net import safe_fetch
        result = safe_fetch(url, max_bytes=100_000, allow_tls_fallback=False)
        _validate_authority_url(
            result.final_url or url,
            "peer_reviewed_allowlisted",
        )
        artifact = Artifact.from_fetch_result(
            result, source_url=url,
            source_class=SourceClass.PEER_REVIEWED,
            source_relationship=SourceRelationship.THIRD_PARTY,
            artifact_type=ArtifactType.API_RESPONSE,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            notes=source_name,
            capture_route="peer_reviewed_allowlisted",
            corroboration_eligible=True,
        )

        try:
            _validate_fetch_result(result, url)
        except AcquisitionError as e:
            artifact.notes = f"FAILED: {e}"
            artifact.corroboration_eligible = False
            self.lake.store(artifact, result.content or b"")
            e.artifact_id = artifact.artifact_id
            raise

        authority_capability = _mint_authority_capture_capability(
            artifact,
            result.content,
        )
        aid = self.lake.store(
            artifact,
            result.content,
            authority_capability=authority_capability,
        )
        return aid, result.text

    def fetch_third_party(self, url: str, phase: str = "ACQUIRE",
                          notes: str = "") -> Tuple[str, str]:
        """Fetch a third-party review or external page.

        Returns (artifact_id, text_content).
        Third-party content is clearly separated from official vendor data.
        Raises AcquisitionError if the fetch fails or returns empty content.
        """
        from net import safe_fetch
        result = safe_fetch(url, max_bytes=60_000, allow_tls_fallback=False)
        artifact = Artifact.from_fetch_result(
            result, source_url=url,
            source_class=SourceClass.USER_GENERATED,
            source_relationship=SourceRelationship.THIRD_PARTY,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            notes=notes,
            capture_route="contextual_third_party",
        )

        try:
            _validate_fetch_result(result, url)
        except AcquisitionError as e:
            artifact.notes = f"FAILED: {e}"
            self.lake.store(artifact, result.content or b"")
            e.artifact_id = artifact.artifact_id
            raise

        aid = self.lake.store(artifact, result.content)
        return aid, result.text

    def fetch_authorized_reseller(self, url: str,
                                  phase: str = "ACQUIRE") -> Tuple[str, str]:
        """Capture an operator-supplied commercial destination as seller copy.

        This proves only what the page says. Claims extracted from it remain
        seller-attributed and do not become independent verification.
        """
        from net import safe_fetch
        result = safe_fetch(url, max_bytes=60_000, allow_tls_fallback=False)
        artifact = Artifact.from_fetch_result(
            result, source_url=url,
            source_class=SourceClass.AUTHORIZED_RESELLER,
            source_relationship=SourceRelationship.SECOND_PARTY,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            notes="Operator-supplied affiliate/commercial destination",
            capture_route="authorized_reseller",
        )
        try:
            _validate_fetch_result(result, url)
        except AcquisitionError as e:
            artifact.notes = f"FAILED: {e}"
            self.lake.store(artifact, result.content or b"")
            e.artifact_id = artifact.artifact_id
            raise
        aid = self.lake.store(artifact, result.content)
        return aid, result.text

    def store_search_results(self, query: str, results_text: str,
                             phase: str = "ACQUIRE") -> str:
        """Store search result data as a search_results artifact.

        Returns artifact_id.
        """
        content = results_text.encode("utf-8")
        now = datetime.now(timezone.utc).isoformat()
        artifact = Artifact(
            artifact_id=hashlib.sha256(content).hexdigest(),
            artifact_type=ArtifactType.SEARCH_RESULTS,
            source_url=f"search://{query}",
            source_class=SourceClass.SEARCH_RESULT,
            source_relationship=SourceRelationship.THIRD_PARTY,
            captured_at=now,
            content_hash=hashlib.sha256(content).hexdigest(),
            content_length=len(content),
            tls_verified=True,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            capture_route="search_results",
        )
        return self.lake.store(artifact, content)

    def store_label_image(self, image_data: bytes,
                          source_description: str = "",
                          source_url: str = "",
                          phase: str = "ACQUIRE",
                          fetch_result=None) -> str:
        """Store a label image as an artifact.

        Returns artifact_id.
        """
        image_format = _image_format(image_data)
        if not image_format:
            raise AcquisitionError(
                "Label artifact is not a supported PNG, JPEG, GIF, or WebP image"
            )
        if fetch_result is not None:
            if (
                getattr(fetch_result, "error", "")
                or not 200 <= int(
                    getattr(fetch_result, "status_code", 0) or 0
                ) < 400
                or getattr(fetch_result, "tls_verified", False) is not True
            ):
                raise AcquisitionError(
                    "Remote label fetch must be successful and TLS verified"
                )
            final_url = str(
                getattr(fetch_result, "final_url", "") or source_url or ""
            ).strip()
            if urlparse(final_url).scheme != "https":
                raise AcquisitionError(
                    "Remote label fetch must resolve to an HTTPS URL"
                )
        now = datetime.now(timezone.utc).isoformat()
        content_hash = hashlib.sha256(image_data).hexdigest()
        artifact = Artifact(
            artifact_id=content_hash,
            artifact_type=ArtifactType.LABEL_SCREENSHOT,
            source_url=source_url or "upload://label-image",
            final_url=(
                str(getattr(fetch_result, "final_url", "") or "")
                or source_url
                or "upload://label-image"
            ),
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
            captured_at=now,
            content_hash=content_hash,
            content_length=len(image_data),
            tls_verified=(
                bool(getattr(fetch_result, "tls_verified", False))
                if fetch_result is not None
                else not str(source_url or "").startswith(("http://", "https://"))
            ),
            status_code=int(
                getattr(fetch_result, "status_code", 0) or 0
            ),
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            capture_route=(
                "remote_official_label"
                if fetch_result is not None
                else "operator_label_upload"
            ),
            notes=(
                f"{source_description}; image_format={image_format}"
                if source_description else f"image_format={image_format}"
            ),
        )
        return self.lake.store(artifact, image_data)

    def store_structured_data(self, data_dict: dict, source_url: str,
                               source_name: str = "",
                               phase: str = "EXTRACT") -> str:
        """Store structured data (JSON-LD, WooCommerce API response, etc.).

        Returns artifact_id.
        """
        import json
        content = json.dumps(data_dict, indent=2).encode("utf-8")
        now = datetime.now(timezone.utc).isoformat()
        artifact = Artifact(
            artifact_id=hashlib.sha256(content).hexdigest(),
            artifact_type=ArtifactType.STRUCTURED_DATA,
            source_url=source_url,
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
            captured_at=now,
            content_hash=hashlib.sha256(content).hexdigest(),
            content_length=len(content),
            tls_verified=True,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            capture_route="operator_structured_data",
            notes=source_name,
        )
        return self.lake.store(artifact, content)

    def fetch_with_browser(self, url: str,
                           source_class: SourceClass = SourceClass.OFFICIAL_VENDOR,
                           phase: str = "ACQUIRE") -> Tuple[str, str]:
        """Fetch a page using Playwright browser rendering.

        Falls back gracefully if Playwright is not available.
        Returns (artifact_id, text_content).
        """
        from browser_fetch import PLAYWRIGHT_AVAILABLE, BrowserSession
        if not PLAYWRIGHT_AVAILABLE:
            # Fall back to regular fetch
            if source_class == SourceClass.OFFICIAL_VENDOR:
                return self.fetch_official_page(url, phase)
            return self.fetch_third_party(url, phase)

        now = datetime.now(timezone.utc).isoformat()
        try:
            with BrowserSession() as session:
                html = session.fetch(url)
                content = html.encode("utf-8") if html else b""

                if not content or not html or not html.strip():
                    # Browser returned empty — store for audit, raise
                    content_hash = hashlib.sha256(
                        f"empty:{url}:{now}".encode()
                    ).hexdigest()
                    artifact = Artifact(
                        artifact_id=content_hash,
                        artifact_type=ArtifactType.HTML_SNAPSHOT,
                        source_url=url,
                        source_class=source_class,
                        source_relationship=SourceRelationship.FIRST_PARTY,
                        captured_at=now,
                        content_hash=content_hash,
                        offering_id=self.offering_id,
                        job_id=self.job_id,
                        acquisition_phase=phase,
                        capture_route="browser_failed",
                        notes="FAILED: browser returned empty content",
                    )
                    self.lake.store(artifact, b"")
                    raise AcquisitionError(
                        f"Browser returned empty content for {url}",
                        artifact_id=content_hash,
                    )

                content_hash = hashlib.sha256(content).hexdigest()
                relationship = (SourceRelationship.FIRST_PARTY
                                if source_class == SourceClass.OFFICIAL_VENDOR
                                else SourceRelationship.THIRD_PARTY)

                artifact = Artifact(
                    artifact_id=content_hash,
                    artifact_type=ArtifactType.HTML_SNAPSHOT,
                    source_url=url,
                    final_url=(
                        getattr(session, "last_final_url", "") or url
                    ),
                    source_class=source_class,
                    source_relationship=relationship,
                    captured_at=now,
                    content_hash=content_hash,
                    content_length=len(content),
                    tls_verified=True,
                    status_code=int(
                        getattr(session, "last_status_code", 0) or 0
                    ),
                    offering_id=self.offering_id,
                    job_id=self.job_id,
                    acquisition_phase=phase,
                    capture_route=(
                        "browser_official"
                        if relationship == SourceRelationship.FIRST_PARTY
                        else "browser_context"
                    ),
                    notes="browser_rendered",
                )
                aid = self.lake.store(artifact, content)
                return aid, html
        except AcquisitionError:
            raise  # Re-raise our own validation errors
        except Exception as e:
            # Store a failed artifact for audit trail, then raise
            error_id = hashlib.sha256(f"error:{url}:{now}".encode()).hexdigest()
            artifact = Artifact(
                artifact_id=error_id,
                artifact_type=ArtifactType.HTML_SNAPSHOT,
                source_url=url,
                source_class=source_class,
                source_relationship=SourceRelationship.FIRST_PARTY,
                captured_at=now,
                content_hash="",
                error=str(e),
                offering_id=self.offering_id,
                job_id=self.job_id,
                acquisition_phase=phase,
                capture_route="browser_failed",
                notes=f"FAILED: browser_render_failed: {e}",
            )
            self.lake.store(artifact)
            raise AcquisitionError(
                f"Browser render failed for {url}: {e}",
                artifact_id=error_id,
            )
