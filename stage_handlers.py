"""
Source Intelligence — Legacy Bridge Stage Handlers
====================================================
Handler functions that wrap the existing research_product.py phases (1-8)
into the new pipeline interface. This keeps the existing system working
while new modules grow alongside.

Each handler receives a Job and returns a result dict.
The job's stage_data stores results from previous stages, enabling
data flow between handlers.
"""

import hashlib
import json
import os
import re
import tempfile
import time
from typing import Optional
from urllib.parse import urlparse

from workflow import Job, PipelineStage, ReviewBlockError


def _parse_intake_urls(raw_value: str) -> list:
    """Normalize comma/newline-separated intake URL fields."""
    if not raw_value or raw_value.strip().upper() == "FIRST RELEASE":
        return []
    return [
        value.strip()
        for value in re.split(r"[,\n\r]+", raw_value)
        if value.strip().startswith(("http://", "https://"))
    ]


def _normalize_publishing_channel(value: str) -> str:
    """Map intake display labels to compliance-engine channel keys."""
    normalized = (value or "").strip().lower()
    aliases = {
        "barchart advertorial": "barchart",
        "accesswire": "accesswire",
        "newswire.com": "newswire",
        "globe newswire": "globe",
        "domain site": "wordpress",
        "": "wordpress",
    }
    return aliases.get(normalized, normalized.replace(" ", "_"))


def _same_site(first_url: str, second_url: str) -> bool:
    """Conservative same-site check for first-party classification."""
    first = (urlparse(first_url).hostname or "").lower().removeprefix("www.")
    second = (urlparse(second_url).hostname or "").lower().removeprefix("www.")
    if not first or not second:
        return False
    return first == second or first.endswith("." + second) or second.endswith("." + first)


def _log_recovery_audit(event_type: str, offering_id: str, job_id: str,
                        db_path: str = "", **kwargs) -> None:
    """Log an immutable recovery audit event.

    event_type: "recovery_attempt", "recovery_success", "recovery_failure",
                "recovery_auth_failure", "manual_entry", "claim_review"
    """
    from datetime import datetime, timezone
    try:
        import sqlite3
        db_file = db_path or __import__("config").DB_PATH
        conn = sqlite3.connect(db_file)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO recovery_audit_events
            (event_type, offering_id, job_id, url, target_facts, result,
             facts_found, facts_missing, artifact_id, claims_added,
             error, reviewer, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event_type,
            offering_id,
            job_id,
            kwargs.get("url", ""),
            json.dumps(kwargs.get("target_facts", [])),
            kwargs.get("result", ""),
            json.dumps(kwargs.get("facts_found", [])),
            json.dumps(kwargs.get("facts_missing", [])),
            kwargs.get("artifact_id", ""),
            kwargs.get("claims_added", 0),
            kwargs.get("error", ""),
            kwargs.get("reviewer", ""),
            now,
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass  # Audit logging must never block the operation


def handle_identify(job: Job) -> dict:
    """Stage: IDENTIFY — Classify entity type, extract product data.

    Wraps phase1_extract_product() from research_product.py.
    Creates an Offering entity from the extracted data.
    """
    from research_product import phase1_extract_product

    browser_session = _get_browser_session()
    try:
        product_data = phase1_extract_product(
            job.url,
            # VSL marketing assertions are captured separately below. They
            # must not be blended into factual product extraction or block
            # content merely because the advertising language is aggressive.
            vsl_url=None,
            product_name=job.product_name or None,
            browser_session=browser_session,
        )
    finally:
        _cleanup_browser(browser_session)

    if not product_data:
        raise ValueError("Could not extract product data from URL")

    # Bridge to new entity model
    from entities import Offering, OfferingType
    offering = Offering.from_legacy_product_data(product_data)
    job.offering_id = offering.offering_id or job.offering_id

    # Persist the offering to the universal offerings table
    try:
        offering.save()
    except Exception:
        pass  # Non-fatal — table may not exist in test environments

    # Fail early for unclassified entities — don't waste research budget
    if offering.offering_type == OfferingType.UNKNOWN:
        from workflow import ReviewBlockError
        raise ReviewBlockError(
            f"Cannot classify entity type for '{product_data.get('product_name', 'unknown')}'. "
            "Human classification required before research can proceed."
        )

    return {
        "product_data": product_data,
        "offering_type": offering.offering_type.value,
        "product_name": product_data.get("product_name", ""),
        "product_type": product_data.get("product_type", ""),
        "ingredient_count": len(
            product_data.get("supplement_facts", {}).get("ingredients", [])
        ),
    }


def handle_acquire(job: Job) -> dict:
    """Stage: ACQUIRE — Fetch and store official pages in evidence lake.

    Requires at least one successful artifact before the pipeline can proceed.
    Raises ValueError if no artifacts could be stored.
    """
    identify_result = job.get_stage_result(PipelineStage.IDENTIFY)
    product_data = identify_result.get("product_data", {})

    stored_artifacts = []
    source_manifest = []
    errors = []

    try:
        from evidence import EvidenceLake, SourceClass
        from acquire import Acquirer

        lake = EvidenceLake()
        acq = Acquirer(lake, offering_id=job.offering_id, job_id=job.job_id)

        # Store official page if we have a URL
        if job.url:
            try:
                aid, _ = acq.fetch_official_page(job.url, phase="ACQUIRE")
                stored_artifacts.append({"artifact_id": aid, "type": "official_page"})
                source_manifest.append({"type": "product_page", "url": job.url,
                                        "status": "captured", "artifact_id": aid})
            except Exception as e:
                errors.append(f"Official page fetch failed: {e}")
                source_manifest.append({"type": "product_page", "url": job.url,
                                        "status": "failed", "error": str(e)})

        # Store the VSL page independently. Its claims are marketing
        # assertions used for intent/substantiation research, not product facts.
        vsl_url = job.metadata.get("vsl_url", "")
        if vsl_url:
            try:
                vsl_source_class = (
                    SourceClass.OFFICIAL_VENDOR
                    if _same_site(job.url, vsl_url)
                    else SourceClass.ANONYMOUS
                )
                aid, vsl_page_text = acq.fetch_with_browser(
                    vsl_url, source_class=vsl_source_class,
                    phase="ACQUIRE_VSL",
                )
                stored_artifacts.append({"artifact_id": aid, "type": "vsl_page"})
                source_manifest.append({
                    "type": "vsl",
                    "url": vsl_url,
                    "status": "captured",
                    "artifact_id": aid,
                    "capture_scope": "rendered_page_html",
                    "spoken_transcript_status": "not_confirmed",
                    "captured_characters": len(vsl_page_text or ""),
                    "source_class": vsl_source_class.value,
                })
            except Exception as e:
                errors.append(f"VSL capture failed: {e}")
                source_manifest.append({"type": "vsl", "url": vsl_url,
                                        "status": "failed", "error": str(e)})

        # Affiliate, previous, and competitor pages are contextual sources.
        # Capture them separately and never let them satisfy mandatory facts.
        contextual_sources = []
        affiliate_url = job.metadata.get("affiliate_link", "")
        if affiliate_url:
            contextual_sources.append(("affiliate_page", affiliate_url))
        contextual_sources.extend(
            ("previous_release", u)
            for u in _parse_intake_urls(job.metadata.get("previous_releases", ""))
        )
        contextual_sources.extend(
            ("competitor_release", u)
            for u in _parse_intake_urls(job.metadata.get("competitor_releases", ""))
        )
        for source_type, source_url in contextual_sources:
            try:
                aid, _ = acq.fetch_third_party(
                    source_url, phase="ACQUIRE_CONTEXT",
                    notes=f"Intake source: {source_type}",
                )
                stored_artifacts.append({"artifact_id": aid, "type": source_type})
                source_manifest.append({"type": source_type, "url": source_url,
                                        "status": "captured", "artifact_id": aid})
            except Exception as e:
                errors.append(f"{source_type} capture failed: {e}")
                source_manifest.append({"type": source_type, "url": source_url,
                                        "status": "failed", "error": str(e)})

        # Preserve operator-provided context immutably, but do not treat it as
        # verified factual evidence.
        operator_payload = {
            "notes": job.metadata.get("operator_notes", ""),
            "client_locked_title": job.metadata.get("client_locked_title", ""),
            "publishing_channel": job.metadata.get("channel", ""),
        }
        if any(operator_payload.values()):
            aid = acq.store_structured_data(
                operator_payload,
                source_url="intake://operator-context",
                source_name="operator_intake",
                phase="ACQUIRE_INTAKE",
            )
            stored_artifacts.append({"artifact_id": aid, "type": "operator_intake"})
            source_manifest.append({"type": "operator_intake",
                                    "status": "captured", "artifact_id": aid})

        # Store label image if provided
        label_url = job.metadata.get("label_image")
        if label_url:
            try:
                if os.path.isfile(label_url):
                    with open(label_url, "rb") as label_file:
                        label_bytes = label_file.read()
                else:
                    from net import safe_fetch
                    result = safe_fetch(label_url, max_bytes=5_000_000)
                    label_bytes = result.content
                if label_bytes:
                    aid = acq.store_label_image(
                        label_bytes,
                        source_description=label_url,
                        source_url=job.metadata.get("label_source_url", ""),
                    )
                    stored_artifacts.append({"artifact_id": aid, "type": "label_image"})
                    source_manifest.append({
                        "type": "label_image",
                        "url": job.metadata.get("label_source_url", label_url),
                        "status": "captured",
                        "artifact_id": aid,
                    })
            except Exception as e:
                errors.append(f"Label image fetch failed: {e}")
                source_manifest.append({
                    "type": "label_image",
                    "url": job.metadata.get("label_source_url", label_url),
                    "status": "failed",
                    "error": str(e),
                })

    except ImportError:
        # Evidence lake not yet wired — allow pipeline to continue
        # with data already captured during IDENTIFY
        return {
            "artifacts_stored": 0,
            "artifacts": [],
            "evidence_lake_available": False,
        }

    # Postcondition: at least one successful artifact required
    if not stored_artifacts and job.url:
        raise ValueError(
            f"Acquisition failed — no artifacts stored. "
            f"Errors: {'; '.join(errors) if errors else 'unknown'}. "
            f"Cannot proceed without evidence."
        )

    return {
        "artifacts_stored": len(stored_artifacts),
        "artifacts": stored_artifacts,
        "evidence_lake_available": True,
        "errors": errors,
        "source_manifest": source_manifest,
        "intake_complete": not any(
            s.get("status") == "failed" for s in source_manifest
        ),
    }


def _find_literal_excerpt(text: str, search_terms: list,
                          context_chars: int = 80,
                          require_all: bool = False) -> tuple:
    """Find a literal excerpt in artifact text matching search terms.

    Args:
        text: The artifact text to search in.
        search_terms: List of terms to look for.
        context_chars: How many chars of surrounding context to include.
        require_all: If True, ALL non-empty terms must appear in the text
            for the match to be considered literal. Use for composite facts
            like ingredient+amount, package+price, credential+holder.
            If False (default), any single term match is sufficient.

    Returns (exact_excerpt, page_location) where:
    - exact_excerpt is the literal text from the artifact (with context)
    - page_location is a character offset reference like "chars 145-225"

    If no match is found, returns ("", "") so the caller can fall back.
    """
    if not text:
        return "", ""

    text_lower = text.lower()
    non_empty_terms = [t for t in search_terms if t and str(t).strip()]

    # Max distance between components for composite literal match.
    # Terms appearing far apart (e.g. "Zinc" in header and "500 mg"
    # for a different ingredient in the footer) are not the same fact.
    _MAX_PROXIMITY = 200

    if require_all and len(non_empty_terms) > 1:
        # ALL non-empty terms must appear. Find the smallest window
        # across all occurrences of each term, not just the first.

        # Collect all occurrence positions for each term
        all_positions = []  # list of lists: one per term
        for term in non_empty_terms:
            t_lower = str(term).lower()
            t_len = len(t_lower)
            occs = []
            start_search = 0
            while True:
                pos = text_lower.find(t_lower, start_search)
                if pos < 0:
                    break
                occs.append((pos, t_len))
                start_search = pos + 1
            if not occs:
                return "", ""  # Missing component → not literal
            all_positions.append(occs)

        # Find the smallest window that contains one occurrence of each term
        # Use a sweep approach: try each occurrence of term[0] as anchor
        best_span = None
        best_start = 0
        best_end = 0
        for anchor_pos, anchor_len in all_positions[0]:
            window_start = anchor_pos
            window_end = anchor_pos + anchor_len
            valid = True
            for other_occs in all_positions[1:]:
                # Find the occurrence closest to the anchor
                closest = min(
                    other_occs,
                    key=lambda o: abs(o[0] - anchor_pos),
                )
                c_start, c_len = closest
                window_start = min(window_start, c_start)
                window_end = max(window_end, c_start + c_len)
            span = window_end - window_start
            if span <= _MAX_PROXIMITY:
                if best_span is None or span < best_span:
                    best_span = span
                    best_start = window_start
                    best_end = window_end

        if best_span is None:
            return "", ""  # No window within proximity

        start = max(0, best_start - context_chars // 2)
        end = min(len(text), best_end + context_chars // 2)
        excerpt = text[start:end].strip()
        if start > 0:
            excerpt = "..." + excerpt
        if end < len(text):
            excerpt = excerpt + "..."
        return excerpt, f"chars {best_start}-{best_end}"

    # Default: any single term match is sufficient
    for term in non_empty_terms:
        pos = text_lower.find(str(term).lower())
        if pos >= 0:
            start = max(0, pos - context_chars // 2)
            end = min(len(text), pos + len(str(term)) + context_chars // 2)
            excerpt = text[start:end].strip()
            if start > 0:
                excerpt = "..." + excerpt
            if end < len(text):
                excerpt = excerpt + "..."
            return excerpt, f"chars {pos}-{pos + len(str(term))}"
    return "", ""


# Deterministic fact_key → ClaimType mapping for the generic extractor.
# Each fact_key gets exactly ONE canonical type — no set iteration ambiguity.
_FACT_KEY_PRIMARY_TYPE = None  # Lazy-initialized below


def _get_fact_key_primary_type():
    """Build and cache the deterministic fact_key → ClaimType mapping."""
    global _FACT_KEY_PRIMARY_TYPE
    if _FACT_KEY_PRIMARY_TYPE is not None:
        return _FACT_KEY_PRIMARY_TYPE
    from claims import ClaimType
    _FACT_KEY_PRIMARY_TYPE = {
        # Supplement
        "ingredients_with_amounts": ClaimType.INGREDIENT_AMOUNT,
        "serving_size": ClaimType.SERVING_INFO,
        "servings_per_container": ClaimType.SERVING_INFO,
        "proprietary_blend_flag": ClaimType.MANUFACTURER_CLAIM,
        "other_ingredients": ClaimType.INGREDIENT_FORM,
        "allergens": ClaimType.ALLERGEN,
        "manufacturer": ClaimType.COMPANY_INFO,
        "country_of_manufacture": ClaimType.COMPANY_INFO,
        # Topical
        "active_ingredients": ClaimType.INGREDIENT_AMOUNT,
        "inactive_ingredients": ClaimType.INGREDIENT_FORM,
        "application_method": ClaimType.FEATURE,
        "warnings": ClaimType.SAFETY_WARNING,
        "net_weight": ClaimType.SPECIFICATION,
        # Device
        "key_features": ClaimType.FEATURE,
        "specifications": ClaimType.SPECIFICATION,
        "warranty": ClaimType.MANUFACTURER_CLAIM,
        "fda_clearance_status": ClaimType.REGULATORY_STATUS,
        "certifications": ClaimType.CERTIFICATION,
        "power_source": ClaimType.SPECIFICATION,
        # Telehealth
        "services_offered": ClaimType.FEATURE,
        "pricing_tiers": ClaimType.PRICING,
        "prescriber_credentials": ClaimType.CERTIFICATION,
        "states_available": ClaimType.FEATURE,
        "medications_offered": ClaimType.FEATURE,
        "consultation_process": ClaimType.FEATURE,
        # Info product
        "whats_included": ClaimType.FEATURE,
        "format": ClaimType.SPECIFICATION,
        "author_credentials": ClaimType.CERTIFICATION,
        "access_method": ClaimType.FEATURE,
        "pricing": ClaimType.PRICING,
        # Financial
        "service_type": ClaimType.FEATURE,
        "topics_covered": ClaimType.FEATURE,
        "track_record_claims": ClaimType.MANUFACTURER_CLAIM,
        "regulatory_registrations": ClaimType.REGULATORY_STATUS,
        # Software
        "platform_support": ClaimType.SPECIFICATION,
        "integrations": ClaimType.FEATURE,
        "data_security": ClaimType.FEATURE,
        "support_options": ClaimType.FEATURE,
        # Service
        "service_description": ClaimType.FEATURE,
        "service_area": ClaimType.FEATURE,
        "credentials": ClaimType.CERTIFICATION,
        "guarantees": ClaimType.MANUFACTURER_CLAIM,
        # Food
        "nutrition_facts": ClaimType.SERVING_INFO,
        "ingredients": ClaimType.INGREDIENT_AMOUNT,
        # Cannabis
        "cannabinoid_profile": ClaimType.INGREDIENT_AMOUNT,
        "terpene_profile": ClaimType.INGREDIENT_AMOUNT,
        "thc_content": ClaimType.INGREDIENT_AMOUNT,
        "cbd_content": ClaimType.INGREDIENT_AMOUNT,
        "lab_results": ClaimType.CERTIFICATION,
        "strain_type": ClaimType.FEATURE,
        "consumption_method": ClaimType.FEATURE,
        "state_availability": ClaimType.FEATURE,
        # Research peptide
        "peptide_sequence": ClaimType.SPECIFICATION,
        "purity_percentage": ClaimType.SPECIFICATION,
        "molecular_weight": ClaimType.SPECIFICATION,
        "cas_number": ClaimType.SPECIFICATION,
        "form": ClaimType.SPECIFICATION,
        "amount_per_vial": ClaimType.SPECIFICATION,
        "storage_requirements": ClaimType.SPECIFICATION,
        "research_use_only_disclaimer": ClaimType.SAFETY_WARNING,
        # Program
        "program_structure": ClaimType.FEATURE,
        "duration": ClaimType.SPECIFICATION,
        "credentials_earned": ClaimType.CERTIFICATION,
        "instructor_credentials": ClaimType.CERTIFICATION,
        # Subscription
        "included_items": ClaimType.FEATURE,
        "billing_frequency": ClaimType.PRICING,
        "cancellation_policy": ClaimType.REFUND_POLICY,
        "trial_period": ClaimType.PRICING,
        # Professional
        "experience": ClaimType.MANUFACTURER_CLAIM,
        "pricing_structure": ClaimType.PRICING,
    }
    return _FACT_KEY_PRIMARY_TYPE


def _extract_targeted_fact(fact_key: str, product_data: dict) -> list:
    """Extract a specific fact_key from production-shaped product data.

    Uses the same data-shape logic as handle_extract() — reads from the correct
    fields (e.g. supplement_facts.ingredients for ingredients_with_amounts).

    Returns a list of (claim_text, search_terms) tuples. Each search_terms list
    is used for literal excerpt matching. Returns [] if the fact is not found.
    """
    supp = product_data.get("supplement_facts", {})

    if fact_key == "ingredients_with_amounts":
        ingredients = supp.get("ingredients", [])
        if isinstance(ingredients, list):
            results = []
            for ing in ingredients:
                if isinstance(ing, dict):
                    name = ing.get("name", "")
                    amount = ing.get("amount", "")
                    if name:
                        text = f"{name}: {amount}" if amount else name
                        terms = [name, amount] if amount else [name]
                        results.append((text, terms))
                elif isinstance(ing, str) and ing.strip():
                    results.append((ing, [ing]))
            return results
        # Fallback: top-level or supplement_facts flat value
        val = product_data.get(fact_key, "") or supp.get(fact_key, "")
        if val and isinstance(val, str):
            return [(val, [val])]
        return []

    if fact_key == "serving_size":
        serving = supp.get("serving_size", "")
        if not serving:
            serving = product_data.get("serving_size", "")
        if serving:
            return [(f"Serving size: {serving}",
                     [str(serving), "serving size"])]
        return []

    if fact_key == "servings_per_container":
        ct = supp.get("servings_per_container", "") or \
            supp.get("servings", "") or \
            product_data.get("servings_per_container", "")
        if ct:
            return [(f"Servings per container: {ct}",
                     [str(ct), "servings per container", "servings"])]
        return []

    if fact_key == "pricing":
        pricing = product_data.get("pricing", {})
        if isinstance(pricing, dict) and pricing:
            results = []
            for pkg, price in pricing.items():
                text = f"{pkg}: {price}"
                results.append((text, [str(price), pkg]))
            return results
        return []

    if fact_key == "manufacturer":
        mfr = product_data.get("manufacturer", "") or \
            supp.get("manufacturer", "")
        if mfr:
            return [(f"Manufacturer: {mfr}", [mfr])]
        return []

    if fact_key == "allergens":
        allergens = product_data.get("allergens", []) or \
            supp.get("allergens", [])
        if isinstance(allergens, list):
            return [(a, [a]) for a in allergens if isinstance(a, str) and a]
        if isinstance(allergens, str) and allergens:
            return [(allergens, [allergens])]
        return []

    # Generic fallback: try top-level then supplement_facts
    val = product_data.get(fact_key, "")
    if not val:
        val = supp.get(fact_key, "")
    if not val:
        return []
    if isinstance(val, str):
        return [(val, [val])]
    if isinstance(val, list):
        results = []
        for item in val:
            s = str(item) if not isinstance(item, str) else item
            if s.strip():
                results.append((s, [s]))
        return results
    if isinstance(val, (int, float)):
        s = str(val)
        return [(f"{fact_key}: {s}", [s])]
    if isinstance(val, bool):
        s = "yes" if val else "no"
        return [(f"{fact_key}: {s}", [s])]
    return [(str(val), [str(val)])]


def _normalize_fact_value(fact_key: str, value) -> list:
    """Normalize any fact value into a list of strings for claim creation.

    Handles: str, int, float, list[str], list[dict], dict, bool.
    """
    if isinstance(value, str):
        return [f"{fact_key}: {value}"] if value.strip() else []
    if isinstance(value, bool):
        return [f"{fact_key}: {'yes' if value else 'no'}"]
    if isinstance(value, (int, float)):
        return [f"{fact_key}: {value}"]
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                # Extract meaningful string representation
                parts = [str(v) for v in item.values() if v]
                result.append(", ".join(parts) if parts else "")
            else:
                result.append(str(item))
        return [r for r in result if r.strip()]
    if isinstance(value, dict):
        # For dict values, stringify each value as separate items
        items = []
        for k, v in value.items():
            if v:
                items.append(f"{k}: {v}")
        return [f"{fact_key}: {'; '.join(items)}"] if items else []
    return [f"{fact_key}: {value}"] if value else []


def _extract_vsl_marketing_assertions(vsl_text: str) -> list:
    """Extract attributed marketing assertions and search intent from a VSL.

    These records describe what the marketer said. They are never treated as
    substantiated product facts and cannot satisfy mandatory evidence gates.
    """
    if not vsl_text or len(vsl_text.strip()) < 50:
        return []
    from research_product import call_claude
    prompt = f"""Analyze this video-sales-letter page text as advertising evidence.

Return ONLY a JSON array. Each item must contain:
- claim: a short verbatim or very close quotation of the marketing assertion
- search_intent: the underlying question/problem a consumer is trying to solve
- topic: ingredient, benefit, mechanism, testimonial, urgency, guarantee, safety, or other
- evidence_needed: what independent evidence would be needed to evaluate it

Do not silently accept a claim as fact. Capture what was said, the strongest
factually supportable client-positive angle it suggests, and the evidence needed
to use that angle at the compliance boundary. Maximum 30 material assertions.

VSL PAGE TEXT:
{vsl_text[:50000]}
"""
    response = call_claude(
        prompt,
        system=(
            "You are a client-positive evidence strategist. Assume good faith, "
            "attribute marketing claims accurately, and identify the strongest "
            "fully substantiated, compliant positioning without treating an "
            "unsupported assertion as established fact."
        ),
        max_tokens=5000,
    )
    if not response:
        return []
    try:
        clean = re.sub(r"```json\s*|```", "", response).strip()
        match = re.search(r"\[[\s\S]*\]", clean)
        parsed = json.loads(match.group() if match else clean)
        return [item for item in parsed if isinstance(item, dict) and item.get("claim")]
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []


def handle_extract(job: Job) -> dict:
    """Stage: EXTRACT — Extract atomic claims from artifacts.

    Claims are tied to source artifacts via source_artifact_id, with
    exact_excerpt populated from the literal artifact text when possible.
    Confidence is computed via authority scoring from real artifact properties.

    In update mode (is_update=True in identify result), re-extracts product
    data from the newly acquired artifact content so new facts reach the ledger.
    """
    identify_result = job.get_stage_result(PipelineStage.IDENTIFY)
    acquire_result = job.get_stage_result(PipelineStage.ACQUIRE)
    product_data = identify_result.get("product_data", {})
    is_update = identify_result.get("is_update", False)
    claims_stored = 0
    extraction_errors = []

    try:
        from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus
        ledger = ClaimsLedger()

        # Determine the source artifact ID (official page stored in ACQUIRE)
        # and load its real properties for authority scoring
        source_artifact_id = None
        source_artifact = None
        for art in acquire_result.get("artifacts", []):
            if art.get("type") == "official_page":
                source_artifact_id = art.get("artifact_id")
                break

        # Load the actual artifact from the evidence lake for:
        # 1. Real authority scoring (source_class, relationship, tls)
        # 2. Literal excerpt extraction from stored content
        artifact_source_class = None
        artifact_relationship = None
        artifact_tls = True
        artifact_text = ""  # The raw text content for excerpt extraction
        try:
            from evidence import EvidenceLake, SourceClass, SourceRelationship
            lake = EvidenceLake()
            if source_artifact_id:
                source_artifact = lake.get(source_artifact_id)
                artifact_text = lake.get_content(source_artifact_id)
            if source_artifact:
                artifact_source_class = SourceClass(source_artifact.source_class) \
                    if isinstance(source_artifact.source_class, str) \
                    else source_artifact.source_class
                artifact_relationship = SourceRelationship(source_artifact.source_relationship) \
                    if isinstance(source_artifact.source_relationship, str) \
                    else source_artifact.source_relationship
                artifact_tls = bool(source_artifact.tls_verified)
            else:
                # No artifact found — use conservative defaults
                artifact_source_class = SourceClass.OFFICIAL_VENDOR
                artifact_relationship = SourceRelationship.FIRST_PARTY
        except ImportError:
            from evidence import SourceClass, SourceRelationship
            artifact_source_class = SourceClass.OFFICIAL_VENDOR
            artifact_relationship = SourceRelationship.FIRST_PARTY

        # Compute authority-based confidence from actual artifact properties
        try:
            from authority import score_authority
            vendor_confidence = score_authority(
                artifact_source_class,
                artifact_relationship,
                tls_verified=artifact_tls,
                extraction_method="llm_extraction",
            )
        except ImportError:
            vendor_confidence = 0.4  # Fallback if authority module unavailable

        # Derive source_class string from artifact for claims records
        artifact_sc_str = artifact_source_class.value \
            if artifact_source_class else "official_vendor"

        # In update mode, re-extract product data from the new artifact.
        # CRITICAL: Only extract claims from genuinely NEW data — old claims
        # already exist in the ledger with their original artifact provenance.
        # The merged product_data is still built for downstream stages (RESEARCH
        # etc.) but claims extraction uses new-only data to avoid false provenance.
        extract_data = product_data  # Default: extract from all data
        if is_update and artifact_text:
            try:
                from research_product import phase1_extract_product
                new_product_data = phase1_extract_product(
                    artifact_text, job.url
                )
                if isinstance(new_product_data, dict):
                    # Use new-only data for claims extraction
                    extract_data = new_product_data

                    # Merge into product_data for downstream stages only
                    new_supp = new_product_data.get("supplement_facts", {})
                    old_supp = product_data.get("supplement_facts", {})
                    if new_supp.get("ingredients"):
                        existing_names = {
                            i.get("name", "").lower()
                            for i in old_supp.get("ingredients", [])
                        }
                        merged_ings = list(old_supp.get("ingredients", []))
                        for new_ing in new_supp["ingredients"]:
                            if new_ing.get("name", "").lower() not in existing_names:
                                merged_ings.append(new_ing)
                        product_data = dict(product_data)
                        product_data["supplement_facts"] = dict(old_supp)
                        product_data["supplement_facts"]["ingredients"] = merged_ings
                    # Merge pricing
                    new_pricing = new_product_data.get("pricing", {})
                    if new_pricing:
                        old_pricing = dict(product_data.get("pricing", {}))
                        old_pricing.update(new_pricing)
                        product_data["pricing"] = old_pricing
                    # Merge claims
                    new_claims = new_product_data.get("claims", [])
                    if new_claims:
                        old_claims = list(product_data.get("claims", []))
                        old_texts = {
                            (c.get("claim", c) if isinstance(c, dict) else c).lower()
                            for c in old_claims
                        }
                        for nc in new_claims:
                            nc_text = nc.get("claim", nc) if isinstance(nc, dict) else nc
                            if nc_text.lower() not in old_texts:
                                old_claims.append(nc)
                        product_data["claims"] = old_claims
            except Exception:
                pass  # Fall through to extract from existing product_data

        claims_batch = []

        # VSL marketing claims are extracted from their own artifact. They are
        # useful for search intent and substantiation research, but explicitly remain
        # unsubstantiated and cannot satisfy mandatory product facts.
        vsl_artifact_id = next(
            (a.get("artifact_id") for a in acquire_result.get("artifacts", [])
             if a.get("type") == "vsl_page"),
            None,
        )
        if vsl_artifact_id:
            try:
                vsl_text = lake.get_content(vsl_artifact_id)
                vsl_artifact = lake.get(vsl_artifact_id)
                vsl_source_class = (
                    vsl_artifact.source_class.value
                    if vsl_artifact and hasattr(vsl_artifact.source_class, "value")
                    else "anonymous"
                )
                for assertion in _extract_vsl_marketing_assertions(vsl_text):
                    assertion_text = str(assertion.get("claim", "")).strip()
                    if not assertion_text:
                        continue
                    excerpt, location = _find_literal_excerpt(
                        vsl_text, [assertion_text]
                    )
                    claims_batch.append(Claim(
                        offering_id=job.offering_id,
                        claim_text=assertion_text,
                        claim_type=ClaimType.MANUFACTURER_CLAIM,
                        source_artifact_id=vsl_artifact_id,
                        exact_excerpt=excerpt or assertion_text,
                        page_location=location or "VSL page",
                        source_class=vsl_source_class,
                        confidence=0.25,
                        extraction_method="marketing_copy",
                        review_status=ReviewStatus.NEEDS_VERIFICATION,
                        metadata={
                            "fact_key": "vsl_marketing_assertion",
                            "claim_nature": "marketing_assertion",
                            "attribution_verified": bool(excerpt),
                            "substantiation_state": "unverified",
                            "cannot_satisfy_mandatory": True,
                            "allowed_use": "client_positive_compliant_positioning",
                            "search_intent": assertion.get("search_intent", ""),
                            "topic": assertion.get("topic", ""),
                            "evidence_needed": assertion.get("evidence_needed", ""),
                            "vsl_url": job.metadata.get("vsl_url", ""),
                        },
                    ))
            except Exception as vsl_err:
                extraction_errors.append(f"VSL marketing extraction failed: {vsl_err}")

        # Extract ingredient claims — uses extract_data (new-only in update mode).
        # A user-supplied label image is authoritative for Supplement Facts and
        # must be sent through vision OCR rather than ignored as a local path.
        supp_facts = extract_data.get("supplement_facts", {})
        supplement_artifact_id = source_artifact_id
        supplement_method = "llm_extraction"
        supplement_confidence = vendor_confidence
        supplement_source_class = artifact_sc_str
        label_source = job.metadata.get("label_image", "")
        label_artifact_id = next(
            (a.get("artifact_id") for a in acquire_result.get("artifacts", [])
             if a.get("type") == "label_image"),
            None,
        )
        if label_artifact_id and label_source and os.path.isfile(label_source):
            try:
                from research_product import extract_label_image
                label_result = extract_label_image(label_source)
                if isinstance(label_result, dict) and label_result.get("ingredients"):
                    supp_facts = label_result
                    supplement_artifact_id = label_artifact_id
                    supplement_method = "machine_ocr"
                    supplement_source_class = "official_vendor"
                    try:
                        from authority import score_authority as _score_label
                        supplement_confidence = _score_label(
                            SourceClass.OFFICIAL_VENDOR,
                            SourceRelationship.FIRST_PARTY,
                            tls_verified=True,
                            extraction_method="machine_ocr",
                        )
                    except ImportError:
                        supplement_confidence = min(vendor_confidence, 0.4)
            except Exception as label_err:
                extraction_errors.append(f"Label OCR failed: {label_err}")
        ingredients = supp_facts.get("ingredients", [])

        for ing in ingredients:
            name = ing.get("name", "")
            amount = ing.get("amount", "")
            if name:
                ext_method = ing.get("extraction_method", supplement_method)
                # Use authority scoring from real artifact properties
                try:
                    from authority import score_authority
                    if supplement_method == "machine_ocr":
                        conf = supplement_confidence
                    else:
                        conf = score_authority(
                            artifact_source_class,
                            artifact_relationship,
                            tls_verified=artifact_tls,
                            extraction_method=ext_method,
                        )
                except ImportError:
                    conf = vendor_confidence

                claim_text = f"{name}: {amount}" if amount else name
                # Try to find literal excerpt in artifact content
                # Composite: require both name AND amount for literal match
                search_terms = [name, amount] if amount else [name]
                if supplement_method == "machine_ocr":
                    excerpt, location = "", ""
                else:
                    excerpt, location = _find_literal_excerpt(
                        artifact_text, search_terms,
                        require_all=bool(amount),
                    )
                claim = Claim(
                    offering_id=job.offering_id,
                    claim_text=claim_text,
                    claim_type=ClaimType.INGREDIENT_AMOUNT,
                    source_artifact_id=supplement_artifact_id,
                    exact_excerpt=excerpt or claim_text,
                    page_location=location or "Supplement Facts panel",
                    source_class=supplement_source_class,
                    confidence=conf,
                    extraction_method=ext_method,
                    metadata={"ingredient_name": name, "amount": amount,
                              "form": ing.get("form", ""),
                              "excerpt_is_literal": bool(excerpt),
                              "fact_key": "ingredients_with_amounts",
                              "image_ocr": supplement_method == "machine_ocr",
                              "artifact_transcription_verified":
                                  supplement_method == "machine_ocr",
                              "label_source": label_source},
                )
                claims_batch.append(claim)

        # Extract serving info
        serving = supp_facts.get("serving_size", "")
        if serving:
            if supplement_method == "machine_ocr":
                excerpt, location = "", ""
            else:
                excerpt, location = _find_literal_excerpt(
                    artifact_text, [serving, "serving size"]
                )
            claims_batch.append(Claim(
                offering_id=job.offering_id,
                claim_text=f"Serving size: {serving}",
                claim_type=ClaimType.SERVING_INFO,
                source_artifact_id=supplement_artifact_id,
                exact_excerpt=excerpt or f"Serving size: {serving}",
                page_location=location or "Supplement Facts panel",
                source_class=supplement_source_class,
                confidence=supplement_confidence,
                extraction_method=supplement_method,
                metadata={"serving_size": serving,
                          "excerpt_is_literal": bool(excerpt),
                          "fact_key": "serving_size",
                          "image_ocr": supplement_method == "machine_ocr",
                          "artifact_transcription_verified":
                              supplement_method == "machine_ocr",
                          "label_source": label_source},
            ))

        # Extract servings_per_container (distinct from serving_size)
        servings_ct = supp_facts.get("servings_per_container", "")
        if not servings_ct:
            servings_ct = supp_facts.get("servings", "")
        if servings_ct:
            if supplement_method == "machine_ocr":
                excerpt, location = "", ""
            else:
                excerpt, location = _find_literal_excerpt(
                    artifact_text,
                    [str(servings_ct), "servings per container", "servings"],
                )
            claims_batch.append(Claim(
                offering_id=job.offering_id,
                claim_text=f"Servings per container: {servings_ct}",
                claim_type=ClaimType.SERVING_INFO,
                source_artifact_id=supplement_artifact_id,
                exact_excerpt=excerpt or f"Servings per container: {servings_ct}",
                page_location=location or "Supplement Facts panel",
                source_class=supplement_source_class,
                confidence=supplement_confidence,
                extraction_method=supplement_method,
                metadata={"servings_per_container": str(servings_ct),
                          "excerpt_is_literal": bool(excerpt),
                          "fact_key": "servings_per_container",
                          "image_ocr": supplement_method == "machine_ocr",
                          "artifact_transcription_verified":
                              supplement_method == "machine_ocr",
                          "label_source": label_source},
            ))

        # Extract manufacturer / company info if available
        manufacturer = extract_data.get("manufacturer", "")
        if not manufacturer:
            manufacturer = extract_data.get("brand", {}).get("manufacturer", "") \
                if isinstance(extract_data.get("brand"), dict) else ""
        if manufacturer:
            excerpt, location = _find_literal_excerpt(
                artifact_text, [manufacturer]
            )
            claims_batch.append(Claim(
                offering_id=job.offering_id,
                claim_text=f"Manufacturer: {manufacturer}",
                claim_type=ClaimType.COMPANY_INFO,
                source_artifact_id=source_artifact_id,
                exact_excerpt=excerpt or f"Manufacturer: {manufacturer}",
                page_location=location or "Product info",
                source_class=artifact_sc_str,
                confidence=vendor_confidence,
                extraction_method="llm_extraction",
                metadata={"manufacturer": manufacturer,
                          "excerpt_is_literal": bool(excerpt),
                          "fact_key": "manufacturer"},
            ))

        country = extract_data.get("country_of_manufacture", "")
        if not country:
            country = extract_data.get("made_in", "")
        if country:
            excerpt, location = _find_literal_excerpt(
                artifact_text, [country, "made in", "manufactured in"]
            )
            claims_batch.append(Claim(
                offering_id=job.offering_id,
                claim_text=f"Country of manufacture: {country}",
                claim_type=ClaimType.COMPANY_INFO,
                source_artifact_id=source_artifact_id,
                exact_excerpt=excerpt or f"Country of manufacture: {country}",
                page_location=location or "Product info",
                source_class=artifact_sc_str,
                confidence=vendor_confidence,
                extraction_method="llm_extraction",
                metadata={"country": country,
                          "excerpt_is_literal": bool(excerpt),
                          "fact_key": "country_of_manufacture"},
            ))

        # Extract pricing — handles both dict and list formats
        pricing = extract_data.get("pricing", {})
        if isinstance(pricing, dict):
            for pkg_name, price_val in pricing.items():
                if pkg_name and price_val:
                    price_str = str(price_val)
                    excerpt, location = _find_literal_excerpt(
                        artifact_text, [price_str, pkg_name],
                        require_all=True,
                    )
                    claims_batch.append(Claim(
                        offering_id=job.offering_id,
                        claim_text=f"{pkg_name}: {price_val}",
                        claim_type=ClaimType.PRICING,
                        source_artifact_id=source_artifact_id,
                        exact_excerpt=excerpt or f"{pkg_name}: {price_val}",
                        page_location=location or "Pricing section",
                        source_class=artifact_sc_str,
                        confidence=vendor_confidence,
                        extraction_method="llm_extraction",
                        metadata={"package": pkg_name, "price": price_str,
                                  "excerpt_is_literal": bool(excerpt),
                                  "fact_key": "pricing"},
                    ))
        elif isinstance(pricing, list):
            for item in pricing:
                if isinstance(item, dict):
                    pkg = item.get("name", item.get("package", ""))
                    price = item.get("price", item.get("amount", ""))
                    if pkg or price:
                        text = f"{pkg}: {price}" if pkg and price else str(pkg or price)
                        excerpt, location = _find_literal_excerpt(
                            artifact_text, [str(price), pkg] if price else [pkg],
                            require_all=bool(price and pkg),
                        )
                        claims_batch.append(Claim(
                            offering_id=job.offering_id,
                            claim_text=text,
                            claim_type=ClaimType.PRICING,
                            source_artifact_id=source_artifact_id,
                            exact_excerpt=excerpt or text,
                            page_location=location or "Pricing section",
                            source_class=artifact_sc_str,
                            confidence=vendor_confidence,
                            extraction_method="llm_extraction",
                            metadata={"package": pkg, "price": str(price),
                                      "excerpt_is_literal": bool(excerpt),
                                      "fact_key": "pricing"},
                        ))

        # Extract health benefit / manufacturer claims
        # Claims that make health assertions are high-risk and require
        # literal evidence (excerpt from the source artifact). If no
        # literal match is found, they are flagged NEEDS_VERIFICATION.
        from claims import ReviewStatus as _RS
        HIGH_RISK = ClaimsLedger.HIGH_RISK_CLAIM_TYPES if hasattr(
            ClaimsLedger, 'HIGH_RISK_CLAIM_TYPES'
        ) else {ClaimType.HEALTH_BENEFIT, ClaimType.CLINICAL_RESULT,
                ClaimType.DRUG_INTERACTION, ClaimType.SAFETY_WARNING}

        for claim_data in extract_data.get("claims", []):
            claim_text = ""
            claim_type_override = None
            if isinstance(claim_data, dict):
                claim_text = claim_data.get("claim", claim_data.get("text", ""))
                # Allow explicit type from extraction
                ct_str = claim_data.get("type", "")
                if ct_str:
                    try:
                        claim_type_override = ClaimType(ct_str)
                    except ValueError:
                        pass
            elif isinstance(claim_data, str):
                claim_text = claim_data
            if claim_text:
                ct = claim_type_override or ClaimType.MANUFACTURER_CLAIM
                # Manufacturer claims are unverified marketing — score lower
                try:
                    from authority import score_authority
                    mfr_confidence = score_authority(
                        artifact_source_class,
                        artifact_relationship,
                        tls_verified=artifact_tls,
                        extraction_method="marketing_copy",
                    )
                except ImportError:
                    mfr_confidence = 0.35
                excerpt, location = _find_literal_excerpt(
                    artifact_text, [claim_text]
                )
                # High-risk claims without literal evidence → NEEDS_VERIFICATION
                review = _RS.UNREVIEWED
                if ct in HIGH_RISK and not excerpt:
                    review = _RS.NEEDS_VERIFICATION
                # Derive fact_key from the resolved claim type
                ct_fact_key = ct.value  # e.g. "health_benefit", "safety_warning"
                claims_batch.append(Claim(
                    offering_id=job.offering_id,
                    claim_text=claim_text,
                    claim_type=ct,
                    source_artifact_id=source_artifact_id,
                    exact_excerpt=excerpt or claim_text,
                    page_location=location or "Product claims section",
                    source_class=artifact_sc_str,
                    confidence=mfr_confidence,
                    extraction_method="marketing_copy",
                    review_status=review,
                    metadata={"excerpt_is_literal": bool(excerpt),
                              "fact_key": ct_fact_key},
                ))

        # Extract allergens — from supplement_facts or top-level
        allergens_val = supp_facts.get("allergens", "") or \
            extract_data.get("allergens", "")
        if allergens_val:
            allergen_list = allergens_val if isinstance(allergens_val, list) \
                else [allergens_val]
            for allergen in allergen_list:
                a_str = str(allergen).strip()
                if not a_str:
                    continue
                excerpt, location = _find_literal_excerpt(
                    artifact_text, [a_str, "allergen", "contains"]
                )
                claims_batch.append(Claim(
                    offering_id=job.offering_id,
                    claim_text=f"Allergen: {a_str}",
                    claim_type=ClaimType.ALLERGEN,
                    source_artifact_id=source_artifact_id,
                    exact_excerpt=excerpt or f"Allergen: {a_str}",
                    page_location=location or "Allergen info",
                    source_class=artifact_sc_str,
                    confidence=vendor_confidence,
                    extraction_method="llm_extraction",
                    metadata={"allergen": a_str,
                              "excerpt_is_literal": bool(excerpt),
                              "fact_key": "allergens"},
                ))

        # Extract other_ingredients if present
        other_ings = supp_facts.get("other_ingredients", "") or \
            extract_data.get("other_ingredients", "")
        if other_ings:
            oi_str = other_ings if isinstance(other_ings, str) \
                else ", ".join(str(x) for x in other_ings)
            excerpt, location = _find_literal_excerpt(
                artifact_text, [oi_str[:50], "other ingredients"]
            )
            claims_batch.append(Claim(
                offering_id=job.offering_id,
                claim_text=f"Other ingredients: {oi_str}",
                claim_type=ClaimType.INGREDIENT_FORM,
                source_artifact_id=source_artifact_id,
                exact_excerpt=excerpt or f"Other ingredients: {oi_str}",
                page_location=location or "Supplement Facts panel",
                source_class=artifact_sc_str,
                confidence=vendor_confidence,
                extraction_method="llm_extraction",
                metadata={"other_ingredients": oi_str,
                          "excerpt_is_literal": bool(excerpt),
                          "fact_key": "other_ingredients"},
            ))

        # Extract proprietary_blend_flag
        prop_blend = extract_data.get("proprietary_blend_flag", "")
        if not prop_blend:
            prop_blend = supp_facts.get("proprietary_blend_flag", "")
        if prop_blend:
            claims_batch.append(Claim(
                offering_id=job.offering_id,
                claim_text=f"Proprietary blend: {prop_blend}",
                claim_type=ClaimType.MANUFACTURER_CLAIM,
                source_artifact_id=source_artifact_id,
                exact_excerpt=f"Proprietary blend: {prop_blend}",
                page_location="Supplement Facts panel",
                source_class=artifact_sc_str,
                confidence=vendor_confidence,
                extraction_method="llm_extraction",
                metadata={"proprietary_blend": str(prop_blend),
                          "excerpt_is_literal": False,
                          "fact_key": "proprietary_blend_flag"},
            ))

        # --- Generic pack-aware extraction for non-supplement mandatory facts ---
        # The above blocks handle supplement-shaped data (supplement_facts,
        # pricing, manufacturer, claims). For other offering types (device,
        # telehealth, cannabis, etc.) the LLM may return data under keys
        # matching the intelligence pack's required_facts. Extract those here.
        ALREADY_HANDLED = {
            "ingredients_with_amounts", "serving_size", "servings_per_container",
            "manufacturer", "country_of_manufacture", "pricing",
            "allergens", "other_ingredients", "proprietary_blend_flag",
        }
        try:
            from intelligence_packs import get_pack
            from entities import OfferingType
            offering_type_str = identify_result.get("offering_type", "supplement")
            try:
                ot = OfferingType(offering_type_str)
                pack = get_pack(ot)
                pack_facts = pack.get("required_facts", [])
            except (ValueError, KeyError):
                pack_facts = []

            for fact_key in pack_facts:
                if fact_key in ALREADY_HANDLED:
                    continue
                # Check if extract_data has a value for this fact key
                value = extract_data.get(fact_key, "")
                if not value:
                    continue
                # Deterministic claim type selection — explicit primary type
                # per fact_key, falling back to FEATURE for unmapped keys
                ct = _get_fact_key_primary_type().get(fact_key, ClaimType.FEATURE)
                try:
                    # Normalize value to list of string items for uniform handling
                    items = _normalize_fact_value(fact_key, value)
                    for item_str in items:
                        if not item_str.strip():
                            continue
                        excerpt, location = _find_literal_excerpt(
                            artifact_text, [item_str]
                        )
                        claims_batch.append(Claim(
                            offering_id=job.offering_id,
                            claim_text=item_str,
                            claim_type=ct,
                            source_artifact_id=source_artifact_id,
                            exact_excerpt=excerpt or item_str,
                            page_location=location or "Product info",
                            source_class=artifact_sc_str,
                            confidence=vendor_confidence,
                            extraction_method="llm_extraction",
                            metadata={"excerpt_is_literal": bool(excerpt),
                                      "fact_key": fact_key},
                        ))
                except Exception as e:
                    extraction_errors.append(
                        f"Failed to extract {fact_key}: {e}"
                    )
        except ImportError:
            pass  # Intelligence packs not available; skip generic extraction

        if claims_batch:
            ids = ledger.add_claims_batch(claims_batch)
            claims_stored = len(ids)

    except ImportError:
        pass  # Claims ledger not yet wired

    result = {
        "claims_stored": claims_stored,
        "source_artifact_id": source_artifact_id,
        "ingredients_found": len(
            product_data.get("supplement_facts", {}).get("ingredients", [])
        ),
        "extraction_errors": extraction_errors,
    }
    # In update mode, propagate the merged product snapshot so downstream
    # stages (SOURCE_PACK etc.) see the complete picture, not just IDENTIFY's
    # stale original.
    if is_update:
        result["merged_product_data"] = product_data
    return result


class RecoveryError(ValueError):
    """Raised when a recovery operation fails validation."""
    pass


def _validate_recovery_context(offering_id: str, job_id: str,
                                target_facts: list,
                                db_path: str = "") -> "Job":
    """Validate that a recovery operation is authorized.

    Checks:
    - offering_id, job_id, and target_facts are non-empty
    - The job exists in the database
    - The job belongs to the specified offering
    - The job is in AWAITING_REVIEW or COMPLETED state (evidence
      recovery is only meaningful for reviewed/blocked jobs)
    - target_facts are required/mandatory for the offering type

    Returns the loaded Job on success. Raises RecoveryError on failure.
    """
    from workflow import JobStore, JobStatus

    if not offering_id or not offering_id.strip():
        raise RecoveryError("offering_id is required")
    if not job_id or not job_id.strip():
        raise RecoveryError("job_id is required")
    if not target_facts:
        raise RecoveryError("target_facts must be a non-empty list")

    store = JobStore(db_path=db_path or None)
    job = store.load(job_id)
    if job is None:
        raise RecoveryError(f"Job {job_id} does not exist")
    if job.offering_id != offering_id:
        raise RecoveryError(
            f"Job {job_id} belongs to offering {job.offering_id}, "
            f"not {offering_id}"
        )
    allowed_states = (JobStatus.AWAITING_REVIEW, JobStatus.COMPLETED)
    if job.status not in allowed_states:
        raise RecoveryError(
            f"Job {job_id} is in state {job.status.value} — "
            f"recovery is only allowed in {[s.value for s in allowed_states]}"
        )

    # Validate target_facts against offering's intelligence pack
    try:
        from entities import OfferingType
        from intelligence_packs import get_required_facts
    except ImportError:
        return job  # Modules not available — skip fact validation

    offering_type_str = job.get_stage_result(
        __import__("workflow").PipelineStage.IDENTIFY
    ).get("offering_type", "")
    if offering_type_str:
        try:
            ot = OfferingType(offering_type_str)
        except ValueError:
            raise RecoveryError(
                f"Unknown offering type: {offering_type_str}"
            )
        valid_facts = set(get_required_facts(ot))
        invalid = [f for f in target_facts if f not in valid_facts]
        if invalid:
            raise RecoveryError(
                f"Facts {invalid} are not required for "
                f"offering type {offering_type_str}"
            )

    return job


def recover_evidence(url: str, offering_id: str, job_id: str,
                     target_facts: list, db_path: str = "") -> dict:
    """Backend handler for evidence recovery — single fetch, full provenance.

    Validates job/offering ownership and state before fetching.
    Fetches the URL once, stores the immutable artifact, extracts product data
    from the stored content, and creates claims with full provenance for any
    target_facts found.

    Returns dict with:
    - artifact_id: the stored artifact's ID
    - claims_added: count of new claims
    - facts_found: list of fact_keys successfully extracted
    - facts_missing: list of fact_keys not found in the content
    - errors: list of any extraction errors

    Raises RecoveryError if validation fails.
    """
    from evidence import EvidenceLake
    from acquire import Acquirer
    from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus
    from authority import score_authority
    from evidence import SourceClass, SourceRelationship

    effective_db = db_path or None

    # Validate authorization before any network activity
    try:
        _validate_recovery_context(offering_id, job_id, target_facts,
                                    db_path=db_path)
    except RecoveryError as auth_err:
        _log_recovery_audit(
            "recovery_auth_failure", offering_id, job_id,
            db_path=db_path, url=url, target_facts=list(target_facts),
            error=str(auth_err),
        )
        raise

    _log_recovery_audit(
        "recovery_attempt", offering_id, job_id,
        db_path=db_path, url=url, target_facts=list(target_facts),
    )

    lake = EvidenceLake(db_path=effective_db)
    acq = Acquirer(lake, offering_id=offering_id, job_id=job_id)

    # Direct label-image URLs must go through vision extraction, not the HTML
    # text extractor. The normal third-party fetch is intentionally small and
    # returns no useful text for PNG/JPEG bytes.
    image_exts = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    is_label_image = url.lower().split("?", 1)[0].endswith(image_exts)
    image_ocr_data = None

    # Single fetch — artifact stores this exact response
    try:
        if is_label_image:
            from net import safe_fetch
            image_result = safe_fetch(url, max_bytes=5_000_000)
            if image_result.error or image_result.status_code != 200 \
                    or not image_result.content:
                raise ValueError(
                    image_result.error
                    or f"Label image fetch returned HTTP {image_result.status_code}"
                )
            art_id = acq.store_label_image(
                image_result.content,
                source_description=url,
                source_url=url,
                phase="EVIDENCE_RECOVERY",
            )
            suffix = os.path.splitext(url.split("?", 1)[0])[1] or ".png"
            tmp_path = ""
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(image_result.content)
                    tmp_path = tmp.name
                from research_product import extract_label_image
                image_ocr_data = extract_label_image(tmp_path)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            text_content = ""  # Images have no literal text excerpt.
        else:
            art_id, text_content = acq.fetch_third_party(
                url, phase="EVIDENCE_RECOVERY",
                notes="Recovery fetch for missing mandatory facts",
            )
    except Exception as fetch_err:
        _log_recovery_audit(
            "recovery_failure", offering_id, job_id,
            db_path=db_path, url=url, target_facts=list(target_facts),
            error=f"Fetch failed: {fetch_err}",
        )
        raise

    # Load artifact properties for authority scoring
    source_artifact = lake.get(art_id)
    if source_artifact:
        sc = SourceClass(source_artifact.source_class) \
            if isinstance(source_artifact.source_class, str) \
            else source_artifact.source_class
        sr = SourceRelationship(source_artifact.source_relationship) \
            if isinstance(source_artifact.source_relationship, str) \
            else source_artifact.source_relationship
        tls = bool(source_artifact.tls_verified)
    else:
        sc = SourceClass.USER_GENERATED
        sr = SourceRelationship.THIRD_PARTY
        tls = True

    extraction_method = "machine_ocr" if is_label_image else "llm_extraction"
    confidence = score_authority(sc, sr, tls_verified=tls,
                                 extraction_method=extraction_method)
    sc_str = sc.value if sc else "user_generated"

    # Extract product data from the SAME artifact we just stored. Label images
    # use the vision result; web pages use the text extractor.
    try:
        if is_label_image:
            if isinstance(image_ocr_data, dict):
                new_data = {"supplement_facts": image_ocr_data}
            elif isinstance(image_ocr_data, list):
                new_data = {"supplement_facts": {
                    "ingredients": image_ocr_data,
                    "serving_size": "",
                }}
            else:
                new_data = {}
        else:
            from research_product import phase1_extract_product
            new_data = phase1_extract_product(text_content, url)
    except Exception as extract_err:
        _log_recovery_audit(
            "recovery_failure", offering_id, job_id,
            db_path=db_path, url=url, target_facts=list(target_facts),
            artifact_id=art_id,
            error=f"Extraction raised: {extract_err}",
        )
        return {
            "artifact_id": art_id,
            "claims_added": 0,
            "duplicates_skipped": 0,
            "facts_found": [],
            "facts_missing": list(target_facts),
            "errors": [f"Extraction failed: {extract_err}"],
        }
    if not isinstance(new_data, dict):
        _log_recovery_audit(
            "recovery_failure", offering_id, job_id,
            db_path=db_path, url=url, target_facts=list(target_facts),
            artifact_id=art_id,
            error="Extraction returned non-dict: could not parse product data",
        )
        return {
            "artifact_id": art_id,
            "claims_added": 0,
            "duplicates_skipped": 0,
            "facts_found": [],
            "facts_missing": list(target_facts),
            "errors": ["Could not extract product data from URL content"],
        }

    ledger = ClaimsLedger(db_path=effective_db)
    claims_added = 0
    duplicates_skipped = 0
    facts_found = []
    facts_missing = []
    errors = []
    type_map = _get_fact_key_primary_type()

    # Build dedup index: (source_artifact_id, fact_key, normalized_value)
    # This preserves corroborating claims from DIFFERENT artifacts while
    # preventing exact duplicates from the SAME artifact.
    existing_claims = ledger.get_claims(offering_id)
    existing_keys = set()
    existing_by_key = {}
    for c in existing_claims:
        fk = c.metadata.get("fact_key", "")
        existing_key = (c.source_artifact_id, fk, c.claim_text.lower())
        existing_keys.add(existing_key)
        existing_by_key[existing_key] = c

    # Composite fact keys: literal matching must find ALL components,
    # not just one.  e.g. ingredient name AND amount, package AND price.
    _COMPOSITE_FACTS = {
        "ingredients_with_amounts", "pricing", "serving_size",
        "servings_per_container",
    }

    for fact in target_facts:
        # Use the shared extraction adapter — same data-shape logic
        # as handle_extract() (reads supplement_facts.ingredients, etc.)
        fact_items = _extract_targeted_fact(fact, new_data)
        if not fact_items:
            facts_missing.append(fact)
            continue

        ct = type_map.get(fact, ClaimType.FEATURE)
        composite = fact in _COMPOSITE_FACTS
        claims_created_for_fact = 0
        try:
            for claim_text, search_terms in fact_items:
                if not claim_text.strip():
                    continue
                # Deduplicate: skip if same artifact+fact+value already exists
                dedup_key = (art_id, fact, claim_text.lower())
                if dedup_key in existing_keys:
                    # Self-heal claims created before label OCR was correctly
                    # recognized as verified artifact transcription. Retrying
                    # the same immutable image upgrades metadata in place.
                    if is_label_image:
                        existing_claim = existing_by_key.get(dedup_key)
                        if existing_claim:
                            existing_claim.extraction_method = "machine_ocr"
                            existing_claim.metadata["image_ocr"] = True
                            existing_claim.metadata[
                                "artifact_transcription_verified"
                            ] = True
                            ledger.add_claim(existing_claim)
                    duplicates_skipped += 1
                    claims_created_for_fact += 1  # Count as found
                    continue
                excerpt, location = _find_literal_excerpt(
                    text_content, search_terms,
                    require_all=composite,
                )
                ledger.add_claim(Claim(
                    offering_id=offering_id,
                    claim_text=claim_text,
                    claim_type=ct,
                    source_artifact_id=art_id,
                    exact_excerpt=excerpt or claim_text,
                    page_location=location or "Product info",
                    source_class=sc_str,
                    confidence=confidence,
                    extraction_method=extraction_method,
                    metadata={
                        "fact_key": fact,
                        "excerpt_is_literal": bool(excerpt),
                        "recovery_source": url,
                        "image_ocr": is_label_image,
                        "artifact_transcription_verified": is_label_image,
                    },
                ))
                existing_keys.add(dedup_key)
                claims_added += 1
                claims_created_for_fact += 1
            # Only mark as found if at least one claim was actually created
            if claims_created_for_fact > 0:
                facts_found.append(fact)
            else:
                facts_missing.append(fact)
        except Exception as e:
            errors.append(f"Failed to extract {fact}: {e}")
            facts_missing.append(fact)

    result = {
        "artifact_id": art_id,
        "claims_added": claims_added,
        "duplicates_skipped": duplicates_skipped,
        "facts_found": facts_found,
        "facts_missing": facts_missing,
        "errors": errors,
    }

    _log_recovery_audit(
        "recovery_success" if claims_added > 0 else "recovery_failure",
        offering_id, job_id, db_path=db_path,
        url=url, target_facts=list(target_facts),
        facts_found=facts_found, facts_missing=facts_missing,
        artifact_id=art_id, claims_added=claims_added,
        result="success" if claims_added > 0 else "no_facts_extracted",
    )

    return result


def record_manual_entry(offering_id: str, fact_key: str, value: str,
                        reviewer: str, db_path: str = "") -> str:
    """Record a manually entered fact with appropriate safety flags.

    Manual entries:
    - Use NEEDS_VERIFICATION status (never auto-satisfy evidence gates)
    - Have no source_artifact_id (no supporting evidence)
    - Record reviewer identity and timestamp
    - Are extraction_method="manual_entry"

    Raises RecoveryError if required fields are empty.
    Returns the claim_id.
    """
    from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus
    from datetime import datetime, timezone

    if not offering_id or not offering_id.strip():
        raise RecoveryError("offering_id is required")
    if not fact_key or not fact_key.strip():
        raise RecoveryError("fact_key is required")
    if not value or not value.strip():
        raise RecoveryError("value is required")
    if not reviewer or not reviewer.strip():
        raise RecoveryError("reviewer name is required")

    effective_db = db_path or None

    # Validate that offering exists and fact_key belongs to the pack
    try:
        from entities import OfferingType
        from intelligence_packs import get_required_facts
        from workflow import JobStore, PipelineStage

        store = JobStore(db_path=effective_db)
        # Find the most recent job for this offering (list_jobs returns
        # newest first via ORDER BY created_at DESC)
        jobs = store.list_jobs(offering_id=offering_id)
        if not jobs:
            raise RecoveryError(
                f"No jobs found for offering '{offering_id}'. "
                f"Cannot validate manual entry without a research job."
            )
        latest = jobs[0]  # Newest first
        ot_str = latest.get_stage_result(
            PipelineStage.IDENTIFY
        ).get("offering_type", "")
        if not ot_str:
            raise RecoveryError(
                "Cannot validate manual entry: offering has no "
                "identified type. Run the IDENTIFY stage first."
            )
        try:
            ot = OfferingType(ot_str)
        except ValueError:
            raise RecoveryError(
                f"Cannot validate manual entry: offering type "
                f"'{ot_str}' is not a recognized OfferingType."
            )
        valid_facts = set(get_required_facts(ot))
        if fact_key not in valid_facts:
            raise RecoveryError(
                f"Fact '{fact_key}' is not required for "
                f"offering type {ot_str}. "
                f"Valid facts: {sorted(valid_facts)}"
            )
    except ImportError as imp_err:
        raise RecoveryError(
            f"Cannot validate manual entry: required module unavailable "
            f"({imp_err}). Validation cannot be skipped."
        )

    ct = _get_fact_key_primary_type().get(fact_key, ClaimType.FEATURE)
    ledger = ClaimsLedger(db_path=effective_db)
    now = datetime.now(timezone.utc).isoformat()
    claim = Claim(
        offering_id=offering_id,
        claim_text=f"{fact_key}: {value}",
        claim_type=ct,
        source_artifact_id=None,
        exact_excerpt="",
        page_location="",
        source_class="manual",
        confidence=0.0,
        extraction_method="manual_entry",
        review_status=ReviewStatus.NEEDS_VERIFICATION,
        reviewed_by=reviewer,
        reviewed_at=now,
        metadata={
            "fact_key": fact_key,
            "manual_entry": True,
            "entered_by": reviewer,
            "entered_at": now,
        },
    )
    claim_id = ledger.add_claim(claim)

    # Find job_id for audit logging
    _job_id = ""
    try:
        from workflow import JobStore
        _store = JobStore(db_path=effective_db)
        _jobs = _store.list_jobs(offering_id=offering_id)
        if _jobs:
            _job_id = _jobs[0].job_id
    except Exception:
        pass

    _log_recovery_audit(
        "manual_entry", offering_id, _job_id,
        db_path=db_path, target_facts=[fact_key],
        reviewer=reviewer, claims_added=1,
        result=f"manual: {fact_key}={value}",
    )

    return claim_id


def handle_reconcile(job: Job) -> dict:
    """Stage: RECONCILE — Detect conflicts between claims from different sources.

    Runs the claims ledger conflict detection for the offering.
    """
    conflicts = []

    try:
        from claims import ClaimsLedger
        ledger = ClaimsLedger()
        conflicts = ledger.detect_conflicts(job.offering_id)
    except ImportError:
        pass

    return {
        "conflicts_found": len(conflicts),
        "conflicts": [
            {"claim_a": a, "claim_b": b, "description": desc}
            for a, b, desc in conflicts
        ],
    }


def handle_research(job: Job) -> dict:
    """Stage: RESEARCH — PubMed research + safety/drug interactions.

    Routes through intelligence packs: only runs PubMed/safety research
    for offering types that require ingredient research. Non-ingestible
    types skip PubMed and get a research summary noting why.
    """
    from research_product import phase2_pubmed_research, phase3_safety_research

    identify_result = job.get_stage_result(PipelineStage.IDENTIFY)
    product_data = identify_result.get("product_data", {})
    offering_type_str = identify_result.get("offering_type", "unknown")

    # Check if this offering type needs ingredient research
    needs_research = True
    try:
        from intelligence_packs import get_pack
        from entities import OfferingType
        try:
            offering_type = OfferingType(offering_type_str)
        except ValueError:
            offering_type = OfferingType.UNKNOWN

        try:
            pack = get_pack(offering_type)
            evidence_reqs = pack.get("evidence_requirements", {})
            pubmed_req = evidence_reqs.get("pubmed_research", "optional")
            needs_research = pubmed_req in ("required", "required_for_medications", "recommended")
        except ValueError:
            # UNKNOWN type — no pack available. Should have been caught
            # at IDENTIFY, but if we get here, skip research rather than
            # waste budget on inappropriate research.
            needs_research = False
    except ImportError:
        # Intelligence packs module not available — default to research
        needs_research = True

    if needs_research:
        ingredient_research = phase2_pubmed_research(product_data)
        safety_data = phase3_safety_research(product_data, ingredient_research)
    else:
        ingredient_research = {}
        safety_data = {"skipped": True, "reason": f"Not required for {offering_type_str}"}

    return {
        "ingredient_research": ingredient_research,
        "safety_data": safety_data,
        "ingredients_researched": len(ingredient_research) if isinstance(ingredient_research, dict) else 0,
        "research_skipped": not needs_research,
    }


def handle_comply(job: Job) -> dict:
    """Stage: COMPLY — Compliance pre-check.

    Uses the new ComplianceEngine for rule-based evaluation filtered by
    offering type, channel, and jurisdiction. Falls back to legacy
    phase7_compliance_check() if the new engine is not available.
    """
    identify_result = job.get_stage_result(PipelineStage.IDENTIFY)
    product_data = identify_result.get("product_data", {})
    offering_type_str = identify_result.get("offering_type", "unknown")

    try:
        from compliance import ComplianceEngine, ComplianceState
        from entities import OfferingType

        try:
            offering_type = OfferingType(offering_type_str)
        except ValueError:
            offering_type = OfferingType.UNKNOWN

        engine = ComplianceEngine()
        channel = _normalize_publishing_channel(
            job.metadata.get("channel", "wordpress")
        )
        jurisdiction = job.metadata.get("jurisdiction", "US")

        # Build text corpus from product claims and descriptions
        text_parts = []
        for claim in product_data.get("claims", []):
            if isinstance(claim, dict):
                text_parts.append(claim.get("claim", claim.get("text", "")))
            elif isinstance(claim, str):
                text_parts.append(claim)
        text_parts.append(product_data.get("description", ""))
        corpus = " ".join(t for t in text_parts if t)

        report = engine.evaluate(corpus, offering_type, channel, jurisdiction)

        # NOTE: FDA disclaimer check is NOT performed here. The vendor's
        # raw claims naturally won't contain our editorial disclaimer.
        # FDA disclaimer validation belongs in post-generation content
        # review (after SOURCE_PACK generates the final article).

        # Map compliance state to risk level for downstream consumers
        state_to_risk = {
            ComplianceState.BLOCKED: "critical",
            ComplianceState.HUMAN_REVIEW_REQUIRED: "high",
            ComplianceState.READY_FOR_EDITORIAL_REVIEW: "medium",
            ComplianceState.CLEARED: "low",
        }
        risk_level = state_to_risk.get(report.overall_state, "unknown")

        return {
            "compliance": {
                "state": report.overall_state.value,
                "risk_level": risk_level,
                "blocks": report.blocks,
                "reviews": report.reviews,
                "warnings": report.warnings,
                "results": [
                    {
                        "rule_id": r.rule_id,
                        "state": r.state.value,
                        "matched_text": r.matched_text,
                        "safe_alternative": r.safe_alternative,
                        "description": r.description,
                    }
                    for r in report.results
                ],
                "summary": report.summary(),
            },
            "risk_level": risk_level,
        }

    except ImportError:
        # Fall back to legacy compliance check
        from research_product import phase7_compliance_check
        compliance = phase7_compliance_check(product_data)
        risk = "unknown"
        if isinstance(compliance, dict):
            risk = str(compliance.get("risk_level", "unknown")).strip().lower()
        return {
            "compliance": compliance,
            "risk_level": risk,
        }


def handle_analyze_site(job: Job) -> dict:
    """Stage: ANALYZE_SITE — Keyword research.

    Wraps phase4_keyword_research().
    """
    from research_product import phase4_keyword_research

    identify_result = job.get_stage_result(PipelineStage.IDENTIFY)
    product_data = identify_result.get("product_data", {})

    keywords = phase4_keyword_research(product_data)

    return {"keywords": keywords}


def handle_analyze_market(job: Job) -> dict:
    """Stage: ANALYZE_MARKET — Reputation + competitive landscape.

    Wraps phase5_reputation_check() and phase6_competitive_landscape().
    """
    from research_product import phase5_reputation_check, phase6_competitive_landscape

    identify_result = job.get_stage_result(PipelineStage.IDENTIFY)
    product_data = identify_result.get("product_data", {})

    reputation = phase5_reputation_check(product_data)
    competitive = phase6_competitive_landscape(product_data)

    return {
        "reputation": reputation,
        "competitive": competitive,
    }


def handle_plan(job: Job) -> dict:
    """Stage: PLAN — Content planning.

    Currently a passthrough that assembles data for the output stage.
    Future: intelligent content opportunity identification.
    """
    return {"plan_status": "ready"}


def _validate_resolution(rule_id: str, severity: str, resolution: dict) -> str:
    """Validate that a resolution action is allowed for the rule's severity.

    Returns empty string if valid, or an error message if invalid.

    Severity-specific rules:
    - BLOCKED: Only "substitute" with non-empty substitute_text is allowed.
    - HUMAN_REVIEW (review): "accept" or "substitute" with a non-empty note.
    - EDITORIAL (warning): "accept" or "waive" — note is optional.
    """
    action = resolution.get("action", "")
    note = resolution.get("note", "").strip()
    substitute_text = resolution.get("substitute_text", "").strip()

    if severity == "blocked":
        if action != "substitute":
            return (
                f"{rule_id}: BLOCK-severity rules can only be resolved with "
                f"'substitute' (got '{action}')"
            )
        if not substitute_text:
            return (
                f"{rule_id}: BLOCK-severity substitute requires non-empty "
                f"substitute_text"
            )
    elif severity == "human_review":
        if action not in ("accept", "substitute"):
            return (
                f"{rule_id}: REVIEW-severity rules require 'accept' or "
                f"'substitute' (got '{action}')"
            )
        if not note:
            return (
                f"{rule_id}: REVIEW-severity resolution requires a "
                f"justification note"
            )
    elif severity in ("editorial", "warning"):
        if action not in ("accept", "waive"):
            return (
                f"{rule_id}: WARNING-severity rules allow 'accept' or "
                f"'waive' (got '{action}')"
            )
    else:
        # Unknown severity — require note at minimum
        if not note:
            return f"{rule_id}: resolution requires a justification note"

    return ""


def handle_review(job: Job) -> dict:
    """Stage: REVIEW — Human review gate.

    Blocks the pipeline (raises ReviewBlockError) when:
    - Any claim conflicts are detected
    - Risk level is anything other than "low"
    - Offering type is UNKNOWN (unclassified)

    The job transitions to AWAITING_REVIEW and cannot proceed
    to SOURCE_PACK until a human approves via Pipeline.approve_review().

    Supports per-rule resolution: if approve_review() was called with
    rule_resolutions, each compliance finding is checked individually.
    Severity-specific validation enforces:
    - BLOCK rules: only substitute (with actual text) is accepted
    - REVIEW rules: accept or substitute, must include justification
    - WARNING rules: accept or waive
    """
    from workflow import ReviewBlockError

    reconcile_result = job.get_stage_result(PipelineStage.RECONCILE)
    comply_result = job.get_stage_result(PipelineStage.COMPLY)
    identify_result = job.get_stage_result(PipelineStage.IDENTIFY)

    conflicts = reconcile_result.get("conflicts_found", 0)
    risk = str(comply_result.get("risk_level", "unknown")).strip().lower()
    offering_type = identify_result.get("offering_type", "unknown")

    # Per-rule resolutions — no blanket approval path
    rule_resolutions = job.metadata.get("rule_resolutions", {})
    reviewer = job.metadata.get("review_approved_by", "")

    # Build unresolved reasons
    reasons = []

    if conflicts > 0:
        conflict_res = rule_resolutions.get("CLAIM_CONFLICTS")
        if not conflict_res:
            reasons.append(f"{conflicts} claim conflicts detected")
        elif not conflict_res.get("note", "").strip():
            reasons.append(
                f"{conflicts} claim conflicts: resolution requires a note "
                f"explaining which values are correct"
            )

    if offering_type == "unknown":
        if not rule_resolutions.get("UNKNOWN_TYPE"):
            reasons.append("offering type not classified")

    if risk not in ("low",):
        # Check compliance results for unresolved rules
        compliance = comply_result.get("compliance", {})
        results = compliance.get("results", [])

        if results:
            unresolved = []
            invalid = []
            resolved_rules = []
            for r in results:
                rule_id = r.get("rule_id", "")
                severity = r.get("state", "")
                resolution = rule_resolutions.get(rule_id)
                if resolution:
                    # Validate the resolution against severity
                    error = _validate_resolution(rule_id, severity, resolution)
                    if error:
                        invalid.append(error)
                        continue
                    resolved_rules.append({
                        "rule_id": rule_id,
                        "action": resolution.get("action", ""),
                        "note": resolution.get("note", ""),
                        "substitute_text": resolution.get("substitute_text", ""),
                        "reviewer": reviewer,
                    })
                    continue
                # Unresolved
                unresolved.append(rule_id)

            if invalid:
                reasons.extend(invalid)
            if unresolved:
                reasons.append(
                    f"risk level: {risk} ({len(unresolved)} unresolved rules: "
                    f"{', '.join(unresolved[:5])})"
                )

            if resolved_rules and not unresolved and not invalid and not reasons:
                # Apply substitutions: reject matched claims, create replacements
                substitution_audit = _apply_substitutions(
                    job, resolved_rules, results, reviewer
                )
                return {
                    "auto_approved": False,
                    "previously_approved": True,
                    "approved_by": reviewer,
                    "rule_resolutions": resolved_rules,
                    "substitutions_applied": substitution_audit,
                    "reason": f"All {len(resolved_rules)} compliance findings resolved by reviewer",
                }
        else:
            # Risk is elevated but no specific results to resolve
            if not rule_resolutions:
                reasons.append(f"risk level: {risk}")

    if reasons:
        raise ReviewBlockError(
            f"Human review required: {'; '.join(reasons)}"
        )

    return {
        "auto_approved": True,
        "needs_human_review": False,
        "reason": "No conflicts, low risk, classified type",
    }


def _apply_substitutions(job: Job, resolved_rules: list,
                         compliance_results: list, reviewer: str) -> list:
    """Apply substitute resolutions to the claims ledger.

    For each resolved rule with action=substitute:
    1. Find the claim(s) whose text matches the compliance finding's matched_text
    2. Reject the original claim with metadata linking to the replacement
    3. Create a new claim with the substitute_text, linked back to the original

    Returns a list of audit records: {original_claim_id, replacement_claim_id,
    rule_id, original_text, substitute_text}.
    """
    audit = []

    # Build a lookup: rule_id → resolution
    substitutes = {}
    for r in resolved_rules:
        if r.get("action") == "substitute" and r.get("substitute_text", "").strip():
            substitutes[r["rule_id"]] = r

    if not substitutes:
        return audit

    # Build a lookup: rule_id → matched_text from compliance results
    matched_texts = {}
    for cr in compliance_results:
        if isinstance(cr, dict):
            rid = cr.get("rule_id", "")
            mt = cr.get("matched_text", "")
            if rid and mt:
                matched_texts[rid] = mt

    try:
        import sqlite3
        from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus

        ledger = ClaimsLedger()
        all_claims = ledger.get_claims(job.offering_id)

        for rule_id, resolution in substitutes.items():
            matched = matched_texts.get(rule_id, "")
            if not matched:
                continue

            substitute_text = resolution["substitute_text"]
            note = resolution.get("note", "")
            matched_lower = matched.lower()

            # Find claims whose text contains the matched compliance text
            for claim in all_claims:
                if claim.review_status == ReviewStatus.REJECTED:
                    continue
                if matched_lower not in claim.claim_text.lower():
                    continue

                # Reject the original
                ledger.update_review_status(
                    claim.claim_id, ReviewStatus.REJECTED, reviewer=reviewer
                )

                # Create replacement claim
                replacement = Claim(
                    offering_id=claim.offering_id,
                    claim_text=substitute_text,
                    claim_type=claim.claim_type,
                    source_artifact_id=claim.source_artifact_id,
                    exact_excerpt=claim.exact_excerpt,
                    page_location=claim.page_location,
                    source_class=claim.source_class,
                    confidence=claim.confidence,
                    extraction_method="reviewer_substitution",
                    review_status=ReviewStatus.ACCEPTED,
                    reviewed_by=reviewer,
                    metadata={
                        "supersedes_claim_id": claim.claim_id,
                        "original_text": claim.claim_text,
                        "substitution_rule_id": rule_id,
                        "substitution_note": note,
                    },
                )
                replacement_id = ledger.add_claim(replacement)

                audit.append({
                    "original_claim_id": claim.claim_id,
                    "replacement_claim_id": replacement_id,
                    "rule_id": rule_id,
                    "original_text": claim.claim_text,
                    "substitute_text": substitute_text,
                })

    except ImportError:
        pass  # Claims ledger not available
    except sqlite3.OperationalError:
        pass  # Claims table not present (e.g. stub test environments)

    return audit


def handle_source_pack(job: Job) -> dict:
    """Stage: SOURCE_PACK — Generate source document from evidence and claims ledgers.

    Instead of calling legacy phase8_output(), builds the source document
    directly from accepted claims in the claims ledger and their linked
    artifacts in the evidence lake. Every fact in the output is traceable
    to a specific artifact and page location.
    """
    identify_result = job.get_stage_result(PipelineStage.IDENTIFY)
    acquire_result = job.get_stage_result(PipelineStage.ACQUIRE)
    extract_result = job.get_stage_result(PipelineStage.EXTRACT)
    research_result = job.get_stage_result(PipelineStage.RESEARCH)
    comply_result = job.get_stage_result(PipelineStage.COMPLY)
    review_result = job.get_stage_result(PipelineStage.REVIEW)
    site_result = job.get_stage_result(PipelineStage.ANALYZE_SITE)
    market_result = job.get_stage_result(PipelineStage.ANALYZE_MARKET)

    # Prefer merged product from EXTRACT (includes new ingredients/prices
    # from update mode) over stale IDENTIFY data
    product_data = extract_result.get("merged_product_data") or \
        identify_result.get("product_data", {})
    product_name = product_data.get("product_name", "Unknown Product")

    # Load claims and evidence
    claims_by_type = {}
    artifacts_used = {}
    all_artifacts = {}
    try:
        from claims import ClaimsLedger, ClaimType, ReviewStatus
        from evidence import EvidenceLake

        ledger = ClaimsLedger()
        lake = EvidenceLake()

        all_claims = ledger.get_claims(job.offering_id)

        # Preserve every acquired source in the pack, even when it does not
        # substantiate an accepted factual claim (VSLs and briefs included).
        acquired_artifacts = list(lake.list_for_job(job.job_id))
        acquired_ids = {art.artifact_id for art in acquired_artifacts}
        # Content-addressed artifacts can be reused across jobs. In that case
        # the immutable row retains its original job_id, so also follow the
        # artifact IDs recorded by this job's ACQUIRE result.
        for acquired in acquire_result.get("artifacts", []):
            acquired_id = acquired.get("artifact_id")
            if acquired_id and acquired_id not in acquired_ids:
                reused = lake.get(acquired_id)
                if reused:
                    acquired_artifacts.append(reused)
                    acquired_ids.add(acquired_id)
        for art in acquired_artifacts:
            all_artifacts[art.artifact_id] = {
                "artifact_type": art.artifact_type.value
                    if hasattr(art.artifact_type, "value") else art.artifact_type,
                "source_url": art.source_url,
                "source_class": art.source_class.value
                    if hasattr(art.source_class, "value") else art.source_class,
                "captured_at": art.captured_at,
                "tls_verified": bool(art.tls_verified),
                "is_usable": art.is_usable,
                "acquisition_phase": art.acquisition_phase,
            }

        # Group claims by type, prioritizing accepted > unreviewed > rejected
        for c in all_claims:
            if c.review_status == ReviewStatus.REJECTED:
                continue  # Skip rejected claims
            ct = c.claim_type.value if hasattr(c.claim_type, 'value') else c.claim_type
            if ct not in claims_by_type:
                claims_by_type[ct] = []
            claims_by_type[ct].append({
                "text": c.claim_text,
                "excerpt": c.exact_excerpt,
                "location": c.page_location,
                "confidence": c.confidence,
                "source_class": c.source_class,
                "review_status": c.review_status.value
                    if hasattr(c.review_status, 'value') else c.review_status,
                "artifact_id": c.source_artifact_id,
                "extraction_method": c.extraction_method,
                "metadata": c.metadata,
            })
            # Track which artifacts contributed
            if c.source_artifact_id and c.source_artifact_id not in artifacts_used:
                art = lake.get(c.source_artifact_id)
                if art:
                    artifacts_used[c.source_artifact_id] = {
                        "source_url": art.source_url,
                        "source_class": art.source_class.value
                            if hasattr(art.source_class, 'value')
                            else art.source_class,
                        "captured_at": art.captured_at,
                        "tls_verified": bool(art.tls_verified),
                        "is_usable": art.is_usable,
                    }
    except ImportError:
        pass  # Ledgers not available — fall back to legacy data

    # Build the source document text
    sections = []
    sections.append(f"SOURCE INTELLIGENCE PACK: {product_name}")
    sections.append(f"{'=' * 60}")
    sections.append(f"Job ID: {job.job_id}")
    sections.append(f"Offering ID: {job.offering_id}")
    sections.append("")

    intake_manifest = {
        "product_url": job.url,
        "product_name": job.product_name,
        "label_source_url": job.metadata.get("label_source_url", ""),
        "vsl_url": job.metadata.get("vsl_url", ""),
        "affiliate_link": job.metadata.get("affiliate_link", ""),
        "previous_releases": job.metadata.get("previous_releases", ""),
        "competitor_releases": job.metadata.get("competitor_releases", ""),
        "publishing_channel": job.metadata.get("channel", ""),
        "client_locked_title": job.metadata.get("client_locked_title", ""),
        "operator_notes": job.metadata.get("operator_notes", ""),
    }
    intake_manifest_hash = hashlib.sha256(
        json.dumps(intake_manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    source_manifest = acquire_result.get("source_manifest", [])
    sections.append("INTAKE SOURCE MANIFEST")
    sections.append("-" * 40)
    sections.append(f"  Manifest SHA-256: {intake_manifest_hash}")
    for source in source_manifest:
        status = str(source.get("status", "unknown")).upper()
        source_type = source.get("type", "source")
        source_url = source.get("url", "intake context")
        sections.append(f"  [{status}] {source_type}: {source_url}")
        if source.get("error"):
            sections.append(f"    Capture error: {source['error']}")
        if (source_type == "vsl" and
                source.get("spoken_transcript_status") != "confirmed"):
            sections.append(
                "    Scope warning: rendered VSL page captured; a complete "
                "spoken-word transcript has not been confirmed."
            )
    sections.append(
        "  VSL statements are attributed marketing assertions. They inform the "
        "strongest compliant client-positive positioning, but do not become "
        "established product facts without substantiation."
    )
    sections.append("")

    # Ingredients section
    ingredients = claims_by_type.get("ingredient_amount", [])
    if ingredients:
        sections.append("INGREDIENTS (from claims ledger)")
        sections.append("-" * 40)
        for claim in ingredients:
            conf_pct = f"{claim['confidence'] * 100:.0f}%"
            literal = "[literal]" if claim.get("location", "").startswith("chars") else "[normalized]"
            sections.append(
                f"  {claim['text']}  (confidence: {conf_pct}, "
                f"source: {claim['source_class']}, {literal})"
            )
            if claim["excerpt"] and claim["excerpt"] != claim["text"]:
                sections.append(f"    Excerpt: \"{claim['excerpt']}\"")
        sections.append("")

    # Serving info
    serving_claims = claims_by_type.get("serving_info", [])
    if serving_claims:
        sections.append("SERVING INFORMATION")
        sections.append("-" * 40)
        for claim in serving_claims:
            sections.append(f"  {claim['text']}")
        sections.append("")

    # Pricing
    pricing_claims = claims_by_type.get("pricing", [])
    if pricing_claims:
        sections.append("PRICING (from claims ledger)")
        sections.append("-" * 40)
        for claim in pricing_claims:
            sections.append(f"  {claim['text']}")
        sections.append("")

    # Health claims / manufacturer claims
    mfr_claims = claims_by_type.get("manufacturer_claim", [])
    if mfr_claims:
        sections.append("CLIENT MARKETING ASSERTIONS (attributed; substantiation tracked)")
        sections.append("-" * 40)
        for claim in mfr_claims:
            conf_pct = f"{claim['confidence'] * 100:.0f}%"
            sections.append(f"  \"{claim['text']}\"  (confidence: {conf_pct})")
        sections.append("")

    # Substitution audit trail — show what was replaced by reviewer
    substitutions = review_result.get("substitutions_applied", [])
    # Also find substitutions from claim metadata in case review_result
    # was not captured (e.g., direct handle_source_pack calls)
    if not substitutions:
        for ct_claims in claims_by_type.values():
            for claim in ct_claims:
                meta = claim.get("metadata", {})
                if meta.get("supersedes_claim_id"):
                    substitutions.append({
                        "original_claim_id": meta["supersedes_claim_id"],
                        "original_text": meta.get("original_text", ""),
                        "substitute_text": claim["text"],
                        "rule_id": meta.get("substitution_rule_id", ""),
                    })
    if substitutions:
        sections.append("COMPLIANCE SUBSTITUTIONS (reviewer-approved)")
        sections.append("-" * 40)
        for sub in substitutions:
            sections.append(f"  Rule: {sub.get('rule_id', 'unknown')}")
            sections.append(f"  Original:    \"{sub.get('original_text', '')}\"")
            sections.append(f"  Replaced by: \"{sub.get('substitute_text', '')}\"")
            sections.append("")

    # High-risk claims without literal evidence — editorial warning
    unverified_high_risk = []
    for ct_key in ("health_benefit", "clinical_result",
                   "drug_interaction", "safety_warning"):
        for claim in claims_by_type.get(ct_key, []):
            meta = claim.get("metadata", {}) if isinstance(claim, dict) else {}
            is_literal = meta.get("excerpt_is_literal", True)
            needs_ver = claim.get("review_status") == "needs_verification"
            if not is_literal or needs_ver:
                unverified_high_risk.append(claim)
    if unverified_high_risk:
        sections.append("WARNING: HIGH-RISK CLAIMS WITHOUT LITERAL EVIDENCE")
        sections.append("-" * 40)
        sections.append("  The following claims could not be matched to literal")
        sections.append("  text in the source artifact. They require manual")
        sections.append("  verification before use in published content.")
        for claim in unverified_high_risk:
            sections.append(f"  [UNVERIFIED] \"{claim['text']}\"")
        sections.append("")

    # Required-facts coverage check (from intelligence pack)
    required_facts_result = None
    mandatory_missing = []
    mandatory_manual_only = []
    mandatory_provisional = []
    mandatory_needs_review = []
    try:
        from entities import OfferingType
        from intelligence_packs import get_required_facts, get_mandatory_facts
        offering_type_str = identify_result.get("offering_type", "")
        if offering_type_str:
            ot = OfferingType(offering_type_str)
            req_facts = get_required_facts(ot)
            required_facts_result = ledger.check_required_facts(
                job.offering_id, req_facts
            )
            # Enforce mandatory facts — these block source pack generation
            # strict=True: manual entries, provisional legacy coverage,
            # and non-literal inferred claims do NOT satisfy mandatory
            mand_facts = get_mandatory_facts(ot)
            if mand_facts:
                mand_result = ledger.check_required_facts(
                    job.offering_id, mand_facts, strict=True
                )
                mandatory_missing = mand_result["missing"]
                mandatory_manual_only = mand_result.get("manual_only", [])
                mandatory_provisional = mand_result.get("provisional", [])
                mandatory_needs_review = mand_result.get("needs_review", [])
    except (ValueError, ImportError):
        pass  # Unknown type or module not available

    if mandatory_missing:
        parts = []
        truly_absent = [f for f in mandatory_missing
                        if f not in mandatory_manual_only
                        and f not in mandatory_provisional
                        and f not in mandatory_needs_review]
        if truly_absent:
            parts.append(f"no evidence: {', '.join(truly_absent)}")
        if mandatory_needs_review:
            parts.append(
                f"inferred (needs human acceptance): "
                f"{', '.join(mandatory_needs_review)}"
            )
        if mandatory_manual_only:
            parts.append(
                f"manual entry only (needs artifact): "
                f"{', '.join(mandatory_manual_only)}"
            )
        if mandatory_provisional:
            parts.append(
                f"provisional legacy match (needs re-extraction): "
                f"{', '.join(mandatory_provisional)}"
            )
        raise ReviewBlockError(
            f"Cannot generate source pack: mandatory facts not satisfied — "
            f"{'; '.join(parts)}. "
            f"Acquire evidence-backed claims before proceeding.",
            details={
                "blocked_facts": mandatory_missing,
                "no_evidence": truly_absent,
                "needs_review": mandatory_needs_review,
                "manual_only": mandatory_manual_only,
                "provisional": mandatory_provisional,
            },
        )

    if required_facts_result and required_facts_result["missing"]:
        sections.append("MISSING REQUIRED FACTS")
        sections.append("-" * 40)
        ratio = required_facts_result["coverage_ratio"]
        sections.append(
            f"  Coverage: {ratio:.0%} "
            f"({len(required_facts_result['covered'])} of "
            f"{len(required_facts_result['covered']) + len(required_facts_result['missing'])} "
            f"required facts found)"
        )
        for fact_name in required_facts_result["missing"]:
            sections.append(f"  [MISSING] {fact_name}")
        sections.append("")

    if required_facts_result and required_facts_result.get("provisional"):
        sections.append("PROVISIONAL COVERAGE")
        sections.append("-" * 40)
        sections.append(
            "  The following facts are covered by legacy untagged claims "
            "(inferred from claim type, not explicit fact_key). "
            "Re-extract with current pipeline for precise attribution."
        )
        for fact_name in required_facts_result["provisional"]:
            sections.append(f"  [PROVISIONAL] {fact_name}")
        sections.append("")

    if required_facts_result and required_facts_result.get("manual_only"):
        sections.append("MANUAL-ONLY COVERAGE")
        sections.append("-" * 40)
        sections.append(
            "  The following facts are covered only by manual entries "
            "(no source artifact). They carry NEEDS_VERIFICATION status "
            "and cannot independently satisfy mandatory requirements."
        )
        for fact_name in required_facts_result["manual_only"]:
            sections.append(f"  [MANUAL] {fact_name}")
        sections.append("")

    # Research data (from RESEARCH stage)
    ingredient_research = research_result.get("ingredient_research", {})
    if ingredient_research:
        sections.append("INGREDIENT RESEARCH")
        sections.append("-" * 40)
        for ing_name, data in ingredient_research.items():
            if isinstance(data, dict):
                sections.append(f"  {ing_name}:")
                if data.get("studies"):
                    sections.append(f"    Studies found: {len(data['studies'])}")
                if data.get("summary"):
                    sections.append(f"    Summary: {data['summary'][:200]}")
        sections.append("")

    # Safety data
    safety_data = research_result.get("safety_data", {})
    if safety_data:
        sections.append("SAFETY DATA")
        sections.append("-" * 40)
        interactions = safety_data.get("drug_interactions", [])
        if interactions:
            for ix in interactions[:5]:
                sections.append(f"  Drug interaction: {ix}")
        contraindications = safety_data.get("contraindications", [])
        if contraindications:
            for cx in contraindications[:5]:
                sections.append(f"  Contraindication: {cx}")
        sections.append("")

    # Compliance
    compliance = comply_result.get("compliance", {})
    comply_state = comply_result.get("state", compliance.get("state", ""))
    if comply_state:
        sections.append("COMPLIANCE STATUS")
        sections.append("-" * 40)
        sections.append(f"  State: {comply_state}")
        rules = comply_result.get("results", compliance.get("results", []))
        if rules:
            for r in rules[:10]:
                if isinstance(r, dict):
                    sections.append(
                        f"  Rule: {r.get('rule_id', 'unknown')} — "
                        f"{r.get('description', r.get('claim_text', ''))}"
                    )
        sections.append("")

    # Evidence provenance
    if artifacts_used:
        sections.append("EVIDENCE PROVENANCE")
        sections.append("-" * 40)
        for aid, art_info in artifacts_used.items():
            tls = "TLS verified" if art_info.get("tls_verified") else "TLS unverified"
            sections.append(
                f"  [{aid[:12]}...] {art_info['source_class']} | "
                f"{art_info['source_url']} | {art_info['captured_at']} | {tls}"
            )
        sections.append("")

    if all_artifacts:
        sections.append("ALL CAPTURED SOURCE MATERIAL")
        sections.append("-" * 40)
        for aid, art_info in all_artifacts.items():
            sections.append(
                f"  [{aid[:12]}...] {art_info['artifact_type']} | "
                f"{art_info['source_class']} | {art_info['source_url']}"
            )
        sections.append("")

    doc_text = "\n".join(sections)

    # Build structured data for downstream consumption
    full_data = {
        "product": product_data,
        "offering_id": job.offering_id,
        "claims_by_type": claims_by_type,
        "artifacts_used": artifacts_used,
        "all_artifacts": all_artifacts,
        "intake_manifest": intake_manifest,
        "intake_manifest_hash": intake_manifest_hash,
        "source_manifest": source_manifest,
        "intake_complete": acquire_result.get("intake_complete", False),
        "ingredient_research": ingredient_research,
        "safety": safety_data,
        "compliance": compliance,
        "keywords": site_result.get("keywords", {}),
        "reputation": market_result.get("reputation", {}),
        "competitive": market_result.get("competitive", {}),
        "total_claims": sum(len(v) for v in claims_by_type.values()),
        "total_artifacts": len(all_artifacts),
        "required_facts": required_facts_result,
    }

    return {
        "doc_text": doc_text,
        "full_data": full_data,
        "doc_text_length": len(doc_text),
        "claims_included": full_data["total_claims"],
        "artifacts_referenced": full_data["total_artifacts"],
    }


# ============================================================================
# PIPELINE FACTORY
# ============================================================================

def create_default_pipeline(progress_callback=None, db_path=None) -> "Pipeline":
    """Create a pipeline with all standard handlers registered.

    Returns a ready-to-run Pipeline instance.
    """
    from workflow import Pipeline, JobStore

    store = JobStore(db_path=db_path)
    pipeline = Pipeline(store, progress_callback=progress_callback)

    pipeline.register(PipelineStage.IDENTIFY, handle_identify)
    pipeline.register(PipelineStage.ACQUIRE, handle_acquire)
    pipeline.register(PipelineStage.EXTRACT, handle_extract)
    pipeline.register(PipelineStage.RECONCILE, handle_reconcile)
    pipeline.register(PipelineStage.RESEARCH, handle_research)
    pipeline.register(PipelineStage.COMPLY, handle_comply)
    pipeline.register(PipelineStage.ANALYZE_SITE, handle_analyze_site)
    pipeline.register(PipelineStage.ANALYZE_MARKET, handle_analyze_market)
    pipeline.register(PipelineStage.PLAN, handle_plan)
    pipeline.register(PipelineStage.REVIEW, handle_review)
    pipeline.register(PipelineStage.SOURCE_PACK, handle_source_pack)

    return pipeline


def create_update_pipeline(existing_data: dict,
                           progress_callback=None,
                           db_path=None) -> "Pipeline":
    """Create a pipeline for incremental updates to existing research.

    Instead of running the full pipeline, this:
    1. Pre-populates IDENTIFY with existing product data
    2. Runs ACQUIRE → EXTRACT → RECONCILE with new data
    3. Merges new claims with existing claims
    4. Re-runs COMPLY, REVIEW, SOURCE_PACK

    The existing_data dict is the full_data from a previous pipeline run.
    """
    from workflow import Pipeline, JobStore, StageStatus

    store = JobStore(db_path=db_path)
    pipeline = Pipeline(store, progress_callback=progress_callback)

    product = existing_data.get("product", {})

    def handle_identify_from_existing(job: Job) -> dict:
        """Use existing product data instead of re-classifying.

        Preserves the original offering_id so update claims join
        the same ledger as the original run.
        """
        from entities import OfferingType
        offering_type_str = product.get("offering_type", "supplement")
        try:
            OfferingType(offering_type_str)
        except ValueError:
            offering_type_str = "supplement"

        # Preserve original offering_id — critical for update provenance
        original_offering_id = existing_data.get("offering_id", "")
        if original_offering_id:
            job.offering_id = original_offering_id

        return {
            "product_data": product,
            "offering_type": offering_type_str,
            "is_update": True,
            "original_offering_id": original_offering_id,
            "existing_claims_count": len(
                existing_data.get("claims_by_type", {}).get("ingredient_amount", [])
            ),
        }

    pipeline.register(PipelineStage.IDENTIFY, handle_identify_from_existing)
    pipeline.register(PipelineStage.ACQUIRE, handle_acquire)
    pipeline.register(PipelineStage.EXTRACT, handle_extract)
    pipeline.register(PipelineStage.RECONCILE, handle_reconcile)
    pipeline.register(PipelineStage.RESEARCH, lambda job: {"research_skipped": True})
    pipeline.register(PipelineStage.COMPLY, handle_comply)
    pipeline.register(PipelineStage.ANALYZE_SITE, lambda job: {})
    pipeline.register(PipelineStage.ANALYZE_MARKET, lambda job: {})
    pipeline.register(PipelineStage.PLAN, handle_plan)
    pipeline.register(PipelineStage.REVIEW, handle_review)
    pipeline.register(PipelineStage.SOURCE_PACK, handle_source_pack)

    return pipeline


# ============================================================================
# HELPERS
# ============================================================================

def _get_browser_session():
    """Try to create a browser session for JS rendering."""
    try:
        from browser_fetch import BrowserSession, PLAYWRIGHT_AVAILABLE
        if PLAYWRIGHT_AVAILABLE:
            session = BrowserSession()
            session.__enter__()
            if session.available:
                return session
            session.__exit__(None, None, None)
    except ImportError:
        pass
    return None


def _cleanup_browser(session):
    """Clean up a browser session."""
    if session:
        try:
            session.__exit__(None, None, None)
        except Exception:
            pass
