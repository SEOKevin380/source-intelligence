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
from datetime import datetime, timezone
from typing import Optional, Tuple

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
        from net import safe_fetch
        result = safe_fetch(url, max_bytes=200_000, allow_tls_fallback=False)
        artifact = Artifact.from_fetch_result(
            result, source_url=url,
            source_class=SourceClass.REGULATORY_DATABASE,
            source_relationship=SourceRelationship.THIRD_PARTY,
            artifact_type=ArtifactType.API_RESPONSE,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            notes=source_name,
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

    def fetch_peer_reviewed(self, url: str, source_name: str = "",
                            phase: str = "RESEARCH") -> Tuple[str, str]:
        """Fetch from a peer-reviewed source (PubMed abstract, journal page).

        Returns (artifact_id, text_content).
        Raises AcquisitionError if the fetch fails or returns empty content.
        """
        from net import safe_fetch
        result = safe_fetch(url, max_bytes=100_000, allow_tls_fallback=False)
        artifact = Artifact.from_fetch_result(
            result, source_url=url,
            source_class=SourceClass.PEER_REVIEWED,
            source_relationship=SourceRelationship.THIRD_PARTY,
            artifact_type=ArtifactType.API_RESPONSE,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            notes=source_name,
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
        )
        return self.lake.store(artifact, content)

    def store_label_image(self, image_data: bytes,
                          source_description: str = "",
                          source_url: str = "",
                          phase: str = "ACQUIRE") -> str:
        """Store a label image as an artifact.

        Returns artifact_id.
        """
        now = datetime.now(timezone.utc).isoformat()
        content_hash = hashlib.sha256(image_data).hexdigest()
        artifact = Artifact(
            artifact_id=content_hash,
            artifact_type=ArtifactType.LABEL_SCREENSHOT,
            source_url=source_url or "upload://label-image",
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
            captured_at=now,
            content_hash=content_hash,
            content_length=len(image_data),
            tls_verified=True,
            offering_id=self.offering_id,
            job_id=self.job_id,
            acquisition_phase=phase,
            notes=source_description,
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
                    final_url=url,
                    source_class=source_class,
                    source_relationship=relationship,
                    captured_at=now,
                    content_hash=content_hash,
                    content_length=len(content),
                    tls_verified=True,
                    offering_id=self.offering_id,
                    job_id=self.job_id,
                    acquisition_phase=phase,
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
                notes=f"FAILED: browser_render_failed: {e}",
            )
            self.lake.store(artifact)
            raise AcquisitionError(
                f"Browser render failed for {url}: {e}",
                artifact_id=error_id,
            )
