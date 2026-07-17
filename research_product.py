#!/usr/bin/env python3
"""
Product Source Intelligence Tool
================================
Takes a product URL and produces a comprehensive, standardized, compliance-ready
source document with PubMed research, keyword intelligence, safety data, and
compliance pre-checks.

Usage:
    python3 research_product.py --url "https://product-website.com/"
    python3 research_product.py --url "https://product.com/" --vsl "https://product.com/vsl"
    python3 research_product.py --name "GlycoReset"
    python3 research_product.py --url "https://product.com/" --quick
    python3 research_product.py --csv products_to_research.csv
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

# Local config
from config import (
    ANTHROPIC_API_KEY, PUBMED_SEARCH_URL, PUBMED_FETCH_URL,
    PUBMED_DELAY, PUBMED_MAX_RESULTS, PUBMED_MAX_INGREDIENTS,
    INGREDIENT_DB_PATH, OUTPUT_DIR, USER_AGENT,
    ACCESSWIRE_BLOCKLIST, YMYL_CATEGORIES, CLAIM_RED_FLAGS,
    HEDGE_ALTERNATIVES, SITE_CATEGORIES, PUBMED_API_KEY,
    CVD9_DISEASE_TERMS, CVD9_REVERSAL_VERBS,
)

# ============================================================================
# PROGRESS CALLBACK (enables CLI + Streamlit dual-mode)
# ============================================================================

_progress_callback = None

def _emit(message, level="info"):
    """Route progress messages to CLI (print) or Streamlit (callback)."""
    if _progress_callback:
        _progress_callback(message, level)
    else:
        print(message)  # noqa: T201 — intentional print for CLI mode


# ============================================================================
# INGREDIENT KB COMPOUNDING
# ============================================================================

def _compute_evidence_grade(studies):
    """Compute evidence grade from a list of studies."""
    gold_count = sum(1 for s in studies if s.get("quality_tier") == "gold")
    silver_count = sum(1 for s in studies if s.get("quality_tier") == "silver")
    if gold_count >= 3:
        return "Strong"
    elif gold_count >= 1 or silver_count >= 3:
        return "Moderate"
    elif silver_count >= 1:
        return "Preliminary"
    elif studies:
        return "Traditional"
    return "Insufficient"


def merge_ingredient_research(existing, new_entry):
    """Merge new research into an existing ingredient KB entry.

    Deduplicates by PMID. Upgrades evidence grade if new studies warrant it.
    Merges safety data additively (new interactions/side effects added, not overwritten).
    """
    # Merge studies — dedup by PMID
    existing_pmids = {s.get("pmid") for s in existing.get("studies", []) if s.get("pmid")}
    for study in new_entry.get("studies", []):
        pmid = study.get("pmid")
        if pmid and pmid not in existing_pmids:
            existing.setdefault("studies", []).append(study)
            existing_pmids.add(pmid)

    # Recompute evidence grade from merged study set
    existing["evidence_grade"] = _compute_evidence_grade(existing.get("studies", []))

    # Merge safety data additively
    for field in ["side_effects", "contraindications"]:
        existing_items = set(existing.get(field, []))
        for item in new_entry.get(field, []):
            if item and item not in existing_items:
                existing.setdefault(field, []).append(item)

    # Merge drug interactions (dedup by drug_class + interaction)
    existing_interactions = {
        (d.get("drug_class", ""), d.get("interaction", ""))
        for d in existing.get("drug_interactions", [])
    }
    for di in new_entry.get("drug_interactions", []):
        key = (di.get("drug_class", ""), di.get("interaction", ""))
        if key not in existing_interactions:
            existing.setdefault("drug_interactions", []).append(di)

    # Update clinical dose range if new data provides one and existing doesn't
    if new_entry.get("clinical_dose_range") and not existing.get("clinical_dose_range"):
        existing["clinical_dose_range"] = new_entry["clinical_dose_range"]

    existing["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    return existing


# ============================================================================
# UTILITIES
# ============================================================================

def slugify(text):
    """Convert text to URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')


def fetch_url(url, max_bytes=60000):
    """Fetch a URL's content with proper headers. Returns text or empty string."""
    if not url:
        return ""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            raw = resp.read()[:max_bytes]
            return raw.decode("utf-8", errors="ignore")
    except Exception as e:
        _emit(f"  [!] Fetch failed for {url}: {e}")
        return ""


def strip_html(html):
    """Remove HTML tags, collapse whitespace."""
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _decode_cloudflare_emails(html):
    """Decode Cloudflare-obfuscated email addresses in HTML.

    Cloudflare uses data-cfemail encoding:
    <a href="/cdn-cgi/l/email-protection" data-cfemail="HEXSTRING">
    The first 2 hex chars are the XOR key, remaining chars are the encoded email.
    """
    if not html or 'data-cfemail' not in html:
        return html

    def decode_cfemail(match):
        encoded = match.group(1)
        try:
            key = int(encoded[:2], 16)
            decoded = ''.join(
                chr(int(encoded[i:i+2], 16) ^ key)
                for i in range(2, len(encoded), 2)
            )
            return decoded
        except (ValueError, IndexError):
            return match.group(0)

    # Replace data-cfemail attributes with decoded emails
    result = re.sub(
        r'<a[^>]*data-cfemail="([0-9a-fA-F]+)"[^>]*>\[email[^<]*\]</a>',
        lambda m: decode_cfemail(m),
        html
    )
    # Also replace standalone [email protected] placeholders
    result = re.sub(
        r'data-cfemail="([0-9a-fA-F]+)"',
        lambda m: f'data-decoded-email="{decode_cfemail(m)}"',
        result
    )
    return result


def call_claude(prompt, system="You are a product research assistant. Extract ONLY verifiable facts. Never invent data.", max_tokens=4000, model="claude-haiku-4-5-20251001", images=None):
    """Call Claude API for intelligent extraction. Supports text and image inputs."""
    if not ANTHROPIC_API_KEY:
        _emit("  [!] ANTHROPIC_API_KEY not set — skipping AI extraction")
        return ""
    try:
        import anthropic
        import base64
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        # Build message content (text + optional images)
        content = []
        if images:
            for img_path in images:
                with open(img_path, "rb") as f:
                    raw = f.read()
                img_data = base64.standard_b64encode(raw).decode("utf-8")
                # Detect media type from magic bytes first, fall back to extension
                media_type = None
                if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
                    media_type = "image/webp"
                elif raw[:8] == b'\x89PNG\r\n\x1a\n':
                    media_type = "image/png"
                elif raw[:2] == b'\xff\xd8':
                    media_type = "image/jpeg"
                elif raw[:4] == b'GIF8':
                    media_type = "image/gif"
                if not media_type:
                    ext = img_path.rsplit(".", 1)[-1].lower()
                    media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
                content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_data}})
        content.append({"type": "text", "text": prompt})

        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        return msg.content[0].text
    except Exception as e:
        _emit(f"  [!] Claude API error: {e}")
        return ""


def extract_label_image(image_path):
    """Extract supplement facts from a label image using Claude's vision."""
    _emit(f"  Reading label image: {image_path}")
    if not os.path.exists(image_path):
        _emit(f"  [!] Image not found: {image_path}")
        return []

    prompt = """Extract the complete Supplement Facts from this product label image.

Return ONLY a valid JSON object with this structure:
{
    "serving_size": "1 capsule",
    "servings_per_container": "30",
    "ingredients": [
        {"name": "Ingredient Name", "amount": "500mg", "daily_value": "100%", "form": "as extract"}
    ]
}

Rules:
- Extract the Serving Size line (e.g., "1 Capsule", "2 Tablets", "1 Scoop (5g)")
- Extract the Servings Per Container line (e.g., "30", "60", "90")
- Include EVERY ingredient visible on the label
- Capture exact amounts (mg, mcg, IU, etc.)
- Capture daily value percentages
- Capture the form if specified (e.g., "as Chromium Picolinate")
- Include proprietary blend ingredients even without individual amounts
- Capture "Other Ingredients" separately at the end with amount=""
- Be precise — this is a legal document
- If serving size or servings per container is not visible, use "" for that field

If this image does NOT contain a Supplement Facts panel or ingredient list (e.g., it
is a product mockup, marketing graphic, or unrelated image), return exactly:
{"error": "no_supplement_facts", "ingredients": []}"""

    response = call_claude(prompt, max_tokens=3000, images=[image_path])
    if not response:
        return []

    try:
        clean = re.sub(r'```json\s*', '', response)
        clean = re.sub(r'```\s*$', '', clean)

        # Try parsing as a dict first (new format with serving info)
        obj_match = re.search(r'\{[\s\S]*\}', clean)
        if obj_match:
            parsed = json.loads(obj_match.group())
            # Check for "no supplement facts" response
            if isinstance(parsed, dict) and parsed.get("error") == "no_supplement_facts":
                _emit("  [!] Label image does not contain a Supplement Facts panel")
                _emit("      Provide a direct image of the Supplement Facts label (not a product mockup)")
                return []
            if isinstance(parsed, dict) and "ingredients" in parsed:
                ingredients = parsed["ingredients"]
                if isinstance(ingredients, list) and len(ingredients) > 0:
                    for ing in ingredients:
                        ing["source"] = "label_image"
                        ing["verified"] = True
                    _emit(f"  Extracted {len(ingredients)} ingredients from label image")
                    if parsed.get("serving_size"):
                        _emit(f"  Serving Size: {parsed['serving_size']}")
                    if parsed.get("servings_per_container"):
                        _emit(f"  Servings Per Container: {parsed['servings_per_container']}")
                    return {
                        "ingredients": ingredients,
                        "serving_size": parsed.get("serving_size", ""),
                        "servings_per_container": parsed.get("servings_per_container", ""),
                    }
                else:
                    _emit("  [!] Label image OCR returned 0 ingredients — image may not show a Supplement Facts panel")
                    return []

        # Fallback: try parsing as array (old format, ingredients only)
        arr_match = re.search(r'\[[\s\S]*\]', clean)
        if arr_match:
            ingredients = json.loads(arr_match.group())
            if isinstance(ingredients, list):
                for ing in ingredients:
                    ing["source"] = "label_image"
                    ing["verified"] = True
                _emit(f"  Extracted {len(ingredients)} ingredients from label image")
                return ingredients
    except (json.JSONDecodeError, AttributeError):
        _emit(f"  [!] Failed to parse label extraction")
    return []


def load_ingredient_db():
    """Load the reusable ingredient research database."""
    try:
        with open(INGREDIENT_DB_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_ingredient_db(db):
    """Save the ingredient research database."""
    with open(INGREDIENT_DB_PATH, "w") as f:
        json.dump(db, f, indent=2)


def print_phase(num, name):
    """Print a phase header."""
    _emit(f"\n{'='*60}")
    _emit(f"  PHASE {num}: {name}")
    _emit(f"{'='*60}")


# ============================================================================
# PHASE 1: Product Page Scraping & Structured Extraction
# ============================================================================

def _web_search_product(name, search_type="ingredients"):
    """Get product info by fetching known review/info sites directly."""
    combined = []

    # Strategy: fetch product info from known review sites that are scrapeable
    slug = slugify(name)
    name_lower = name.lower().replace(" ", "")

    # Try common review/info site URL patterns
    review_urls = []
    if search_type == "ingredients":
        review_urls = [
            f"https://www.supplementcritique.com/{slug}-review/",
            f"https://www.healthline.com/nutrition/{slug}",
            f"https://www.supplementcritique.com/{slug}-reviews/",
        ]
    elif search_type == "pricing":
        review_urls = [
            f"https://www.supplementcritique.com/{slug}-review/",
        ]
    elif search_type == "reviews":
        review_urls = [
            f"https://www.trustpilot.com/review/{name_lower}.com",
        ]

    for review_url in review_urls:
        html = fetch_url(review_url, max_bytes=30000)
        if html and len(html) > 1000:
            text = strip_html(html)
            if len(text) > 200:
                combined.append(text[:4000])

    # Strategy 3: DuckDuckGo Instant Answer API (for well-known products)
    try:
        ddg_api = f"https://api.duckduckgo.com/?q={urllib.parse.quote_plus(name + ' product')}&format=json&no_html=1"
        resp = fetch_url(ddg_api, max_bytes=30000)
        if resp:
            data = json.loads(resp)
            abstract = data.get("AbstractText", "")
            if abstract:
                combined.append(f"OVERVIEW: {abstract}")
            for topic in data.get("RelatedTopics", [])[:5]:
                if isinstance(topic, dict) and topic.get("Text"):
                    combined.append(topic["Text"])
    except (json.JSONDecodeError, Exception):
        pass

    return "\n\n".join(combined) if combined else ""


def _extract_json_ld(html):
    """Extract product data from JSON-LD structured data embedded in HTML."""
    results = []
    target_types = (
        "Product", "IndividualProduct", "Offer",
        "DietarySupplement", "Drug", "MedicalDevice",
    )
    pattern = r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>'
    for match in re.findall(pattern, html, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(match.strip())
            # Handle both single objects and arrays
            items = data if isinstance(data, list) else [data]
            for item in items:
                item_type = item.get("@type", "")
                # Handle type as string or list
                if isinstance(item_type, list):
                    if any(t in target_types for t in item_type):
                        results.append(item)
                elif item_type in target_types:
                    results.append(item)
                elif item.get("@graph"):
                    for node in item["@graph"]:
                        node_type = node.get("@type", "")
                        if isinstance(node_type, list):
                            if any(t in target_types for t in node_type):
                                results.append(node)
                        elif node_type in target_types:
                            results.append(node)
                # Also extract FAQPage structured data for content enrichment
                elif item_type == "FAQPage":
                    results.append(item)
                elif item.get("@graph"):
                    for node in item["@graph"]:
                        if node.get("@type") == "FAQPage":
                            results.append(node)
        except (json.JSONDecodeError, TypeError):
            continue
    return results


def _try_woocommerce_api(url):
    """Try to fetch product data from WooCommerce Store API (public, no auth needed).

    Works for JS-rendered WooCommerce sites where direct scraping gets empty HTML.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.hostname}"

    # Extract product slug from URL path
    path = parsed.path.strip("/")
    slug = path.split("/")[-1] if path else ""
    if not slug:
        return ""

    # Try WooCommerce Store API (public endpoints, no auth required)
    api_urls = [
        f"{base}/wp-json/wc/store/v1/products?slug={slug}",
        f"{base}/wp-json/wc/store/products?slug={slug}",
        f"{base}/?rest_route=/wc/store/v1/products&slug={slug}",
    ]

    for api_url in api_urls:
        resp = fetch_url(api_url, max_bytes=60000)
        if resp and resp.strip().startswith(("[", "{")):
            try:
                data = json.loads(resp)
                items = data if isinstance(data, list) else [data]
                texts = []
                for item in items:
                    name = item.get("name", "")
                    desc = strip_html(item.get("description", ""))
                    short_desc = strip_html(item.get("short_description", ""))
                    price = item.get("prices", {})
                    if isinstance(price, dict):
                        raw_price = price.get("price", "")
                        currency = price.get("currency_code", "USD")
                        # WooCommerce Store API returns prices in minor units (cents)
                        try:
                            decimal_places = int(price.get("currency_minor_unit", 2))
                            price_val = int(raw_price) / (10 ** decimal_places)
                            price_str = f"${price_val:.2f} {currency}"
                        except (ValueError, TypeError):
                            price_str = str(raw_price)
                    else:
                        price_str = str(price) if price else ""

                    text = f"PRODUCT: {name}\n"
                    if short_desc:
                        text += f"SHORT DESCRIPTION: {short_desc}\n"
                    if desc:
                        text += f"FULL DESCRIPTION: {desc}\n"
                    if price_str:
                        text += f"PRICE: {price_str}\n"

                    # WooCommerce product attributes (often contain specs)
                    for attr in item.get("attributes", []):
                        attr_name = attr.get("name", "")
                        attr_terms = ", ".join(t.get("name", str(t)) if isinstance(t, dict) else str(t) for t in attr.get("terms", attr.get("options", [])))
                        if attr_name and attr_terms:
                            text += f"{attr_name}: {attr_terms}\n"

                    # Categories
                    cats = [c.get("name", "") for c in item.get("categories", []) if isinstance(c, dict)]
                    if cats:
                        text += f"CATEGORIES: {', '.join(cats)}\n"

                    texts.append(text)

                if texts:
                    _emit(f"    WooCommerce API returned product data ({len(texts[0])} chars)")
                    return "\n\n".join(texts)
            except (json.JSONDecodeError, TypeError):
                continue

    return ""


def _query_dsld(product_name, brand_name=""):
    """Query the NIH Dietary Supplement Label Database for verified label data.

    DSLD contains government-verified supplement facts panels. When a match is
    found, this is the highest-quality ingredient data available — more reliable
    than scraping or OCR. Returns dict with ingredients, serving info, contact,
    and DSLD label ID, or empty dict if no match.
    """
    if not product_name:
        return {}

    # Search by product name
    query = product_name.strip()
    search_url = f"https://api.ods.od.nih.gov/dsld/v9/search-filter?q={urllib.parse.quote_plus(query)}&size=5"

    try:
        resp = fetch_url(search_url, max_bytes=100000)
        if not resp:
            return {}
        data = json.loads(resp)
        hits = data.get("hits", [])
        if not hits:
            # Try brand name if product name didn't match
            if brand_name and brand_name.lower() != product_name.lower():
                search_url2 = f"https://api.ods.od.nih.gov/dsld/v9/search-filter?q={urllib.parse.quote_plus(brand_name + ' ' + product_name)}&size=5"
                resp2 = fetch_url(search_url2, max_bytes=100000)
                if resp2:
                    data2 = json.loads(resp2)
                    hits = data2.get("hits", [])
            if not hits:
                return {}

        # Find best match — require meaningful name overlap to avoid false matches
        best_hit = None
        query_lower = product_name.lower().strip()
        query_words = set(query_lower.split())

        for hit in hits:
            src = hit.get("_source", {})
            full_name = (src.get("fullName") or "").lower().strip()
            hit_words = set(full_name.split())

            # Exact match — best possible
            if full_name == query_lower:
                best_hit = hit
                break

            # Strip generic supplement/product words to isolate brand-specific terms
            generic_words = {"supplement", "support", "formula", "pro", "plus",
                             "max", "ultra", "advanced", "daily", "natural",
                             "premium", "gummies", "capsules", "tablets", "powder",
                             "blend", "complex", "health", "care", "nerve",
                             "brain", "joint", "weight", "loss", "eye", "skin",
                             "hair", "bone", "heart", "immune", "gut", "sleep",
                             "energy", "mood", "focus", "memory", "vision",
                             "review", "reviews", "the", "and", "for", "with",
                             # Common product-type and marketing words
                             "tea", "coffee", "drink", "shake", "bar", "drops",
                             "slim", "thin", "lean", "fat", "burn", "burner",
                             "green", "red", "gold", "silver", "black", "white",
                             "super", "mega", "extra", "complete", "total",
                             "original", "pure", "organic", "herbal", "vital",
                             "essential", "basic", "mens", "womens", "men", "women"}
            # The product's UNIQUE name words (brand-specific, not generic)
            unique_query = query_words - generic_words
            unique_hit = hit_words - generic_words

            # Require MAJORITY of query's unique words to appear in the hit
            # "Cardio Slim Tea" unique = {"cardio"}, hit "Green Tea Slim" unique = {} → no match
            if unique_query:
                unique_overlap = unique_query & unique_hit
                overlap_ratio = len(unique_overlap) / len(unique_query)
                if overlap_ratio >= 0.5 and len(unique_overlap) >= 1:
                    best_hit = hit
                    break
            else:
                # No unique words (e.g., "Nerve Support") — require exact match
                if full_name == query_lower:
                    best_hit = hit
                    break

            # Or if the brand matches AND there's some word overlap
            hit_brand = (src.get("brandName") or "").lower()
            overlap = query_words & hit_words
            if brand_name and brand_name.lower() == hit_brand and overlap:
                best_hit = hit
                break

        if not best_hit:
            _emit(f"    DSLD: No close match found (best candidate: {hits[0]['_source'].get('fullName', 'N/A')})")
            return {}

        label_id = best_hit.get("_id")
        source = best_hit.get("_source", {})
        _emit(f"    DSLD match: {source.get('fullName')} by {source.get('brandName')} (ID: {label_id})")

        # Fetch full label for ingredient amounts
        label_data = {}
        if label_id:
            label_url = f"https://api.ods.od.nih.gov/dsld/v9/label/{label_id}"
            label_resp = fetch_url(label_url, max_bytes=200000)
            if label_resp:
                label_data = json.loads(label_resp)

        # Build ingredient list from search allIngredients + label ingredientRows
        ingredients = []
        all_ings = source.get("allIngredients", [])
        ing_rows = label_data.get("ingredientRows", [])

        # Build amounts lookup from ingredientRows
        # Rows have their own "order" field and an ingredientId — build
        # a simple lookup by order so we can try to match
        row_by_order = {}
        for row in ing_rows:
            quantities = row.get("quantity", [])
            amt = ""
            dv_pct = ""
            if quantities and isinstance(quantities, list) and quantities:
                q = quantities[0] if isinstance(quantities[0], dict) else {}
                qty_val = q.get("quantity", "")
                unit = q.get("unit", "")
                if qty_val:
                    amt = f"{qty_val} {unit}".strip() if unit else str(qty_val)
                for dv_group in q.get("dailyValueTargetGroup", []):
                    if isinstance(dv_group, dict):
                        dv = dv_group.get("percent")
                        if dv is not None:
                            dv_pct = f"{dv}%"
                            break
            row_by_order[row.get("order")] = {"amount": amt, "dv": dv_pct}

        # Map amounts to allIngredients by order (1-indexed)
        for i, ing in enumerate(all_ings):
            cat = ing.get("category", "")
            if cat == "other":
                continue

            name = ing.get("name", "")
            notes = ing.get("notes", "")
            order = i + 1

            # Try to find amount from ingredientRows
            row_data = row_by_order.get(order, {})
            amount = row_data.get("amount", "")
            dv_pct = row_data.get("dv", "")

            form = notes if notes else ""
            ingredients.append({
                "name": name,
                "amount": amount,
                "daily_value": dv_pct,
                "form": form,
                "category": cat,
                "source": "dsld_verified",
                "verified": True,
            })

        # Serving info
        serving_sizes = label_data.get("servingSizes", [])
        serving_size = ""
        if serving_sizes:
            ss = serving_sizes[0]
            qty = ss.get("minQuantity", "")
            unit = ss.get("unit", "")
            serving_size = f"{qty} {unit}".strip()

        servings_per_container = label_data.get("servingsPerContainer", "")

        # Contact info
        contacts = label_data.get("contacts", [])
        contact = {}
        if contacts:
            cd = contacts[0].get("contactDetails", {})
            contact = {
                "name": cd.get("name", ""),
                "address": f"{cd.get('city', '')}, {cd.get('state', '')} {cd.get('zipCode', '')}".strip(", "),
                "phone": cd.get("phoneNumber", ""),
                "email": cd.get("email", ""),
                "website": cd.get("webAddress", ""),
            }

        result = {
            "dsld_id": label_id,
            "dsld_product_name": source.get("fullName", ""),
            "dsld_brand": source.get("brandName", ""),
            "ingredients": ingredients,
            "serving_size": serving_size,
            "servings_per_container": str(servings_per_container) if servings_per_container else "",
            "contact": contact,
            "other_ingredients": [
                o.get("name", "") for o in
                (label_data.get("otheringredients", {}).get("ingredients", [])
                 if isinstance(label_data.get("otheringredients"), dict)
                 else label_data.get("otheringredients", []))
                if isinstance(o, dict) and o.get("name")
            ],
            "claims": [
                c.get("langualCodeDescription", "") for c in label_data.get("claims", [])
                if isinstance(c, dict) and c.get("langualCodeDescription")
            ],
        }

        _emit(f"    DSLD: {len(ingredients)} ingredients, serving: {serving_size}, servings/container: {servings_per_container}")
        return result

    except (json.JSONDecodeError, Exception) as e:
        _emit(f"    [!] DSLD query error: {e}")
        return {}


def _query_fda_caers(product_name, brand_name=""):
    """Query FDA CFSAN Adverse Event Reporting System (CAERS) for safety signals.

    Returns dict with adverse event count, common reactions, and outcomes.
    This data enriches the safety profile — not used as a sole data source.
    """
    if not product_name:
        return {}

    # Try product name first, then brand
    queries = [product_name]
    if brand_name and brand_name.lower() != product_name.lower():
        queries.append(brand_name)

    for query in queries:
        encoded = urllib.parse.quote_plus(f'"{query}"')
        api_url = f"https://api.fda.gov/food/event.json?search=products.name_brand:{encoded}&limit=100"

        try:
            resp = fetch_url(api_url, max_bytes=200000)
            if not resp:
                continue
            data = json.loads(resp)
            total = data.get("meta", {}).get("results", {}).get("total", 0)

            if total == 0:
                continue

            results = data.get("results", [])

            # Aggregate reactions and outcomes
            reaction_counts = {}
            outcome_counts = {}
            for report in results:
                for reaction in report.get("reactions", []):
                    r = reaction.strip().title()
                    reaction_counts[r] = reaction_counts.get(r, 0) + 1
                for outcome in report.get("outcomes", []):
                    o = outcome.strip()
                    outcome_counts[o] = outcome_counts.get(o, 0) + 1

            # Sort by frequency
            top_reactions = sorted(reaction_counts.items(), key=lambda x: x[1], reverse=True)[:15]
            top_outcomes = sorted(outcome_counts.items(), key=lambda x: x[1], reverse=True)

            caers_data = {
                "total_reports": total,
                "query_matched": query,
                "top_reactions": [{"reaction": r, "count": c} for r, c in top_reactions],
                "outcomes": [{"outcome": o, "count": c} for o, c in top_outcomes],
                "reports_analyzed": len(results),
                "note": "CAERS reports are unverified consumer submissions. "
                        "They do not establish causation. Use for signal detection only.",
            }

            _emit(f"    FDA CAERS: {total} adverse event reports for '{query}'")
            if top_reactions:
                top3 = ", ".join(f"{r[0]} ({r[1]})" for r in top_reactions[:3])
                _emit(f"    Top reactions: {top3}")
            return caers_data

        except (json.JSONDecodeError, Exception) as e:
            _emit(f"    [!] FDA CAERS query error: {e}")
            continue

    return {}


def _discover_linked_pages(html, base_url):
    """Discover ingredient, FAQ, and policy pages linked from the main page.

    Supplement sites often have separate pages for ingredients, FAQs, policies,
    etc. This function finds those links so _try_multiple_urls can fetch them.
    Returns list of discovered URLs to try.
    """
    if not html or not base_url:
        return []

    from urllib.parse import urljoin, urlparse

    base_parsed = urlparse(base_url)
    base_domain = base_parsed.hostname or ""

    # Patterns for useful subpages
    link_patterns = re.compile(
        r'(?:ingredient|supplement.?fact|formula|what.?s.?inside|label|'
        r'how.?it.?works|science|research|clinical|lab.?test|'
        r'faq|frequently.?asked|question|'
        r'refund|return|guarantee|money.?back|'
        r'shipping|delivery|'
        r'about|contact|privacy|terms|'
        r'review|testimonial|result)',
        re.IGNORECASE
    )

    discovered = []
    seen = set()

    # Find all <a href="..."> links
    for match in re.finditer(r'<a[^>]*href=["\']([^"\'#]+)["\']', html, re.IGNORECASE):
        href = match.group(1).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue

        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Only follow same-domain links
        if parsed.hostname and parsed.hostname != base_domain:
            continue

        # Skip if already seen or is the main page
        clean_url = full_url.split("?")[0].split("#")[0].rstrip("/")
        if clean_url in seen or clean_url == base_url.rstrip("/"):
            continue

        # Check if the URL path or anchor text matches our patterns
        path = parsed.path.lower()
        # Get surrounding text for context
        tag_text = match.group(0).lower()

        if link_patterns.search(path) or link_patterns.search(tag_text):
            seen.add(clean_url)
            discovered.append(clean_url)

    return discovered[:15]  # Cap to prevent runaway crawling


def _try_multiple_urls(url, browser_session=None):
    """Try fetching a URL and common subpages to maximize content capture."""
    results = {}
    site_needs_browser = False

    # Try main URL (120KB — landing pages with VSLs can be very long)
    main = fetch_url(url, max_bytes=120000)

    # If URL path is a funnel slug (/pv, /v4, /checkout, etc.), also fetch root domain
    # — checkout funnels often have zero product info, while root may have a sales page
    parsed_url = urllib.parse.urlparse(url)
    path = parsed_url.path.rstrip("/")
    funnel_patterns = re.compile(r'^/(?:pv|v\d+|checkout|order|buy|cart|offer|special|promo|lp|landing|sales?)$', re.IGNORECASE)
    if path and funnel_patterns.match(path):
        root_url = f"{parsed_url.scheme}://{parsed_url.netloc}/"
        _emit(f"    Funnel page detected ({path}) — also checking root domain")
        root_html = fetch_url(root_url, max_bytes=120000)
        if root_html and len(strip_html(root_html)) > len(strip_html(main or "")):
            _emit(f"    Root domain has more content ({len(strip_html(root_html)):,} chars)")
            results["root_page"] = root_html

    # If content looks like a JS shell or missing pricing, retry with browser
    if browser_session and browser_session.available:
        try:
            from browser_fetch import is_content_thin
            reason = ""
            if is_content_thin(main):
                reason = "thin"
            elif main and not re.search(r'\$\d+|data-(?:price|total|bottles)|"price"\s*:', main, re.IGNORECASE):
                # Page has content but zero pricing — common on BuyGoods/funnel pages
                if len(strip_html(main)) < 15000:
                    reason = "no-pricing"

            if reason:
                msg = "Content thin" if reason == "thin" else "No pricing in static HTML"
                _emit(f"    {msg} — retrying with browser rendering...")
                rendered = browser_session.fetch(url)
                if rendered and len(strip_html(rendered)) > len(strip_html(main)):
                    main = rendered
                    site_needs_browser = True
                    _emit(f"    Browser recovered {len(strip_html(main)):,} chars of visible text")

                # If still no pricing, look for checkout URL and fetch it
                if not re.search(r'\$\d+', main):
                    checkout_urls = re.findall(
                        r'href=["\x27]([^"\x27]*(?:/checkout|/order|/cart|/buy)[^"\x27]*)["\x27]',
                        main, re.IGNORECASE
                    )
                    # Deduplicate and resolve relative URLs
                    seen_checkout = set()
                    for ck_url in checkout_urls:
                        # Skip asset/image/font URLs
                        if re.search(r'\.(png|jpg|gif|svg|css|js|ico|woff|ttf)$', ck_url, re.IGNORECASE):
                            continue
                        if ck_url.startswith('/'):
                            parsed = urllib.parse.urlparse(url)
                            ck_url = f"{parsed.scheme}://{parsed.netloc}{ck_url}"
                        if ck_url in seen_checkout or not ck_url.startswith('http'):
                            continue
                        seen_checkout.add(ck_url)
                        _emit(f"    Fetching checkout page for pricing: {ck_url}")
                        ck_html = browser_session.fetch(ck_url, wait_until='domcontentloaded',
                                                        timeout_ms=20000)
                        if ck_html and re.search(r'\$\d+', ck_html):
                            results["checkout"] = ck_html
                            _emit(f"    Checkout page: {len(strip_html(ck_html)):,} chars with pricing data")
                            break  # Only need one checkout page
        except ImportError:
            pass

    # Decode Cloudflare-obfuscated emails before storing
    if main:
        main = _decode_cloudflare_emails(main)

    if main:
        results["main"] = main

        # Extract JSON-LD product data from main page
        json_ld = _extract_json_ld(main)
        if json_ld:
            ld_text = ""
            for product in json_ld:
                ptype = product.get("@type", "")
                # Handle FAQPage separately
                if ptype == "FAQPage" or (isinstance(ptype, list) and "FAQPage" in ptype):
                    faq_entries = product.get("mainEntity", [])
                    if faq_entries:
                        ld_text += "\nFAQ (structured data):\n"
                        for faq in faq_entries[:20]:
                            q = faq.get("name", "")
                            a_obj = faq.get("acceptedAnswer", {})
                            a = a_obj.get("text", "") if isinstance(a_obj, dict) else ""
                            if q:
                                ld_text += f"Q: {q}\nA: {a}\n\n"
                    continue

                ld_text += f"\nPRODUCT (structured data): {product.get('name', '')}\n"
                ld_text += f"Description: {product.get('description', '')}\n"
                # DietarySupplement-specific fields
                if product.get("activeIngredient"):
                    ld_text += f"Active Ingredient: {product['activeIngredient']}\n"
                if product.get("nonProprietaryName"):
                    ld_text += f"Non-Proprietary Name: {product['nonProprietaryName']}\n"
                if product.get("dosageForm"):
                    ld_text += f"Dosage Form: {product['dosageForm']}\n"
                if product.get("mechanismOfAction"):
                    ld_text += f"Mechanism: {product['mechanismOfAction']}\n"
                if product.get("offers"):
                    offers = product["offers"] if isinstance(product["offers"], list) else [product["offers"]]
                    for o in offers:
                        ld_text += f"Price: {o.get('price', '')} {o.get('priceCurrency', '')}\n"
                if product.get("sku"):
                    ld_text += f"SKU: {product['sku']}\n"
            if ld_text.strip():
                results["json_ld"] = ld_text
                _emit(f"    Found JSON-LD product data")

    # Always try WooCommerce API — many product pages are JS-rendered
    # and the HTML scrape gets boilerplate even when text length looks OK
    wc_data = _try_woocommerce_api(url)
    if wc_data:
        results["woocommerce_api"] = wc_data

    # Try WordPress REST API for policy/info pages (bypasses JS rendering)
    from urllib.parse import urlparse
    parsed = urlparse(url)
    site_root = f"{parsed.scheme}://{parsed.hostname}"
    product_base = url.split("?")[0].rstrip("/")

    wp_page_slugs = [
        "about", "about-us", "contact", "contact-us",
        "shipping", "shipping-info", "shipping-policy",
        "refund-policy", "return-policy", "returns",
        "terms", "terms-and-conditions", "terms-of-service",
        "faq", "faqs", "privacy-policy", "warranty", "guarantee",
    ]
    wp_api_found = 0
    for slug in wp_page_slugs:
        api_url = f"{site_root}/wp-json/wp/v2/pages?slug={slug}"
        import io, contextlib
        stderr_capture = io.StringIO()
        with contextlib.redirect_stdout(stderr_capture):
            resp = fetch_url(api_url, max_bytes=60000)
        if resp and resp.strip().startswith("["):
            try:
                pages = json.loads(resp)
                for page in pages:
                    title = page.get("title", {}).get("rendered", slug)
                    content_html = page.get("content", {}).get("rendered", "")
                    content_text = strip_html(content_html)
                    if content_text and len(content_text) > 50:
                        key = f"wp_page_{slug}"
                        results[key] = f"PAGE: {title}\n{content_text}"
                        wp_api_found += 1
            except (json.JSONDecodeError, TypeError):
                pass
    if wp_api_found:
        _emit(f"    WordPress API returned {wp_api_found} page(s)")

    # Fallback: try direct HTML fetch for sites without WP API
    if wp_api_found == 0:
        product_subpages = ["/ingredients", "/supplement-facts", "/label"]
        site_subpages = [
            "/about", "/about-us", "/contact", "/contact-us",
            "/shipping-policy", "/shipping", "/delivery",
            "/refund-policy", "/return-policy", "/returns",
            "/terms", "/terms-of-service", "/terms-and-conditions",
            "/privacy-policy", "/faq", "/faqs",
            "/warranty", "/guarantee",
        ]
        seen_urls = set()
        for sub, base in [(s, product_base) for s in product_subpages] + [(s, site_root) for s in site_subpages]:
            sub_url = base + sub
            if sub_url in seen_urls:
                continue
            seen_urls.add(sub_url)
            stderr_capture = io.StringIO()
            with contextlib.redirect_stdout(stderr_capture):
                if site_needs_browser and browser_session and browser_session.available:
                    content = browser_session.fetch(sub_url, max_bytes=30000)
                else:
                    content = fetch_url(sub_url, max_bytes=30000)
            if content and len(content) > 500:
                results[sub] = content
                _emit(f"    Found subpage: {sub} ({len(content):,} bytes)")

    # Discover linked pages from main HTML (ingredients, FAQ, policies)
    main_html = results.get("main", "")
    if main_html:
        discovered = _discover_linked_pages(main_html, url)
        if discovered:
            _emit(f"    Discovered {len(discovered)} linked pages to check")
            fetch_count = 0
            for disc_url in discovered:
                # Skip if we already have this page
                disc_path = urllib.parse.urlparse(disc_url).path
                if any(disc_path.rstrip("/").endswith(k.strip("/")) for k in results if k not in ("main", "woocommerce_api", "json_ld")):
                    continue
                import io, contextlib
                stderr_capture = io.StringIO()
                with contextlib.redirect_stdout(stderr_capture):
                    if site_needs_browser and browser_session and browser_session.available:
                        content = browser_session.fetch(disc_url, max_bytes=30000)
                    else:
                        content = fetch_url(disc_url, max_bytes=30000)
                if content and len(content) > 500:
                    key = f"discovered_{disc_path.strip('/').replace('/', '_')}"
                    results[key] = content
                    fetch_count += 1
                    _emit(f"    Found linked page: {disc_path} ({len(content):,} bytes)")
                if fetch_count >= 5:  # Cap discovered page fetches
                    break

    return results


def _extract_supplement_facts_html(html):
    """Try to extract product facts from raw HTML using regex patterns.

    Handles supplements, peptides, research chemicals, devices, and other product types.
    """
    facts_text = ""
    patterns = [
        # Supplement patterns
        r'(?i)supplement\s*facts.*?(?:</table>|</div>|</section>)',
        r'(?i)ingredients?\s*(?:list|panel)?:?\s*[^<]*(?:<[^>]+>[^<]*){1,80}',
        r'(?i)(?:active|key|main|our)\s+ingredients?.*?(?:</ul>|</div>|</table>|</section>)',
        r'(?i)(?:what.?s\s+inside|formula|blend).*?(?:</ul>|</div>|</table>|</section>)',
        r'(?i)<(?:table|div|section)[^>]*class="[^"]*(?:ingredient|supplement|formula)[^"]*".*?</(?:table|div|section)>',
        # Peptide / research chemical patterns
        r'(?i)(?:product\s+)?specifications?.*?(?:</table>|</div>|</section>|</ul>)',
        r'(?i)(?:certificate\s+of\s+analysis|COA|HPLC|purity).*?(?:</table>|</div>|</section>)',
        r'(?i)(?:molecular\s+weight|sequence|amino\s+acid).*?(?:</table>|</div>|</section>)',
        r'(?i)<(?:table|div|section)[^>]*class="[^"]*(?:product-details|product-info|woocommerce-product)[^"]*".*?</(?:table|div|section)>',
        # Device / general product patterns
        r'(?i)(?:key\s+features|specifications|tech\s+specs).*?(?:</table>|</div>|</section>|</ul>)',
        r'(?i)(?:description|product.description|short.description).*?(?:</div>|</section>)',
        # WooCommerce-specific
        r'(?i)<div[^>]*class="[^"]*woocommerce-product-details__short-description[^"]*".*?</div>',
        r'(?i)<div[^>]*class="[^"]*product-short-description[^"]*".*?</div>',
        r'(?i)<div[^>]*id="tab-description"[^>]*>.*?</div>',
    ]
    for pat in patterns:
        matches = re.findall(pat, html, re.DOTALL)
        for m in matches:
            cleaned = strip_html(m)
            if len(cleaned) > 20 and cleaned not in facts_text:
                facts_text += f"\n{cleaned}\n"
    return facts_text


def _extract_buygoods_pricing(html):
    """Extract pricing from BuyGoods/ClickBank checkout links with data attributes.

    BuyGoods pages store pricing in anchor tags like:
    <a class="buylink kit3" data-bottles="6" data-total="294" data-full="1074"
       data-shipping="Free" data-guarantee="60" ...>

    ClickBank pages have similar patterns with data-price or inline pricing.
    """
    if not html:
        return []

    pricing = []
    seen = set()

    # Pattern 1: BuyGoods data-attribute buylinks
    buylinks = re.findall(
        r'<a[^>]*class="[^"]*buylink[^"]*"[^>]*>',
        html, re.IGNORECASE | re.DOTALL
    )
    for tag in buylinks:
        bottles = re.search(r'data-bottles="(\d+)"', tag)
        total = re.search(r'data-total="([\d.]+)"', tag)
        full = re.search(r'data-full="([\d.]+)"', tag)
        shipping = re.search(r'data-shipping="([^"]*)"', tag)
        guarantee = re.search(r'data-guarantee="(\d+)"', tag)
        title = re.search(r'title="([^"]*)"', tag)
        headline = re.search(r'data-headline="([^"]*)"', tag)

        if total:
            total_val = total.group(1)
            bottles_val = bottles.group(1) if bottles else "1"
            # Skip duplicates
            key = f"{bottles_val}-{total_val}"
            if key in seen:
                continue
            seen.add(key)

            try:
                per_unit = f"${float(total_val) / int(bottles_val):.2f}"
            except (ValueError, ZeroDivisionError):
                per_unit = ""

            pkg_name = title.group(1) if title else f"{bottles_val} Bottles"
            if headline:
                pkg_name += f" ({headline.group(1)})"

            pricing.append({
                "package": pkg_name,
                "total": total_val,
                "original": full.group(1) if full else "",
                "bottles": bottles_val,
                "per_unit": per_unit,
                "shipping": shipping.group(1) if shipping else "",
                "guarantee": guarantee.group(1) if guarantee else "",
            })

    # Pattern 2: Generic data-price attributes (ClickBank and others)
    if not pricing:
        price_tags = re.findall(
            r'<[^>]*data-price="([\d.]+)"[^>]*>',
            html, re.IGNORECASE
        )
        for i, price in enumerate(price_tags):
            key = price
            if key not in seen:
                seen.add(key)
                pricing.append({
                    "package": f"Option {i+1}",
                    "total": price,
                    "original": "",
                    "bottles": "1",
                    "per_unit": f"${price}",
                    "shipping": "",
                    "guarantee": "",
                })

    # Pattern 3: Inline text pricing (e.g., "6 Bottles ... $49 Per bottle ... Total: $294")
    # Common on BuyGoods funnel pages that don't use data attributes
    if not pricing:
        text = strip_html(html) if html else ""
        # Look for patterns like "X Bottles" followed by pricing
        bottle_blocks = re.finditer(
            r'(\d+)\s*Bottle[s]?\s*(\d+)-day\s*supply\s*\$\s*(\d+)\s*Per\s*bottle'
            r'.*?Total:\s*\$?\s*[\d,]+\s*\$\s*(\d+)',
            text, re.IGNORECASE | re.DOTALL
        )
        for match in bottle_blocks:
            bottles = match.group(1)
            per_unit = match.group(3)
            total = match.group(4)
            key = f"{bottles}-{total}"
            if key not in seen:
                seen.add(key)
                shipping_text = ""
                # Check for shipping info near this block
                after = text[match.end():match.end()+100]
                if "FREE SHIPPING" in after.upper():
                    shipping_text = "Free"
                elif "SHIPPING" in after.upper():
                    shipping_text = "Paid"
                # Check for guarantee
                guarantee_match = re.search(r'(\d+)\s*-?\s*(?:day|days)\s*(?:money\s*back\s*)?guarantee', text, re.IGNORECASE)
                guarantee = guarantee_match.group(1) if guarantee_match else ""

                pricing.append({
                    "package": f"{bottles} Bottles",
                    "total": total,
                    "original": "",
                    "bottles": bottles,
                    "per_unit": f"${per_unit}",
                    "shipping": shipping_text,
                    "guarantee": guarantee,
                })

    # Sort by bottle count
    pricing.sort(key=lambda x: int(x.get("bottles", 0)), reverse=True)
    return pricing


def _extract_product_images(html, base_url):
    """Extract product-relevant images from HTML. Returns list of image dicts."""
    if not html or not base_url:
        return []

    from urllib.parse import urljoin, urlparse

    # Find all img tags
    img_tags = re.findall(r'<img[^>]+>', html, re.IGNORECASE)
    images = []
    seen_urls = set()

    # Skip patterns — icons, tracking pixels, tiny images, social media buttons
    skip_patterns = re.compile(
        r'(favicon|icon|logo-small|pixel|tracking|spacer|spinner|loader|'
        r'facebook|twitter|instagram|pinterest|youtube|linkedin|'
        r'payment|visa|mastercard|amex|paypal|badge|seal|'
        r'arrow|chevron|caret|close|menu|hamburger|'
        r'1x1|blank\.gif|data:image)',
        re.IGNORECASE
    )

    for tag in img_tags:
        # Extract src
        src_match = re.search(r'src=["\']([^"\']+)["\']', tag)
        if not src_match:
            continue
        src = src_match.group(1).strip()

        # Skip data URIs, tiny base64, tracking pixels
        if src.startswith('data:') and len(src) < 500:
            continue

        # Make absolute URL
        img_url = urljoin(base_url, src)

        # Skip duplicates
        if img_url in seen_urls:
            continue
        seen_urls.add(img_url)

        # Skip non-image URLs
        parsed = urlparse(img_url)
        path_lower = parsed.path.lower()
        if not any(path_lower.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif']):
            # Allow if no extension (could be CDN URL with query params)
            if '.' in path_lower.split('/')[-1] and not any(path_lower.endswith(ext) for ext in ['.php', '.html']):
                continue

        # Skip known non-product patterns
        if skip_patterns.search(img_url) or skip_patterns.search(tag):
            continue

        # Extract alt text and dimensions
        alt_match = re.search(r'alt=["\']([^"\']*)["\']', tag)
        alt = alt_match.group(1) if alt_match else ""

        width_match = re.search(r'width=["\']?(\d+)', tag)
        height_match = re.search(r'height=["\']?(\d+)', tag)
        width = int(width_match.group(1)) if width_match else None
        height = int(height_match.group(1)) if height_match else None

        # Skip tiny images (icons, badges)
        if width and width < 80:
            continue
        if height and height < 80:
            continue

        images.append({
            "url": img_url,
            "alt": alt,
            "width": width,
            "height": height,
        })

    return images


def _download_product_images(images, output_dir, slug, max_images=10):
    """Download product images to output directory. Returns list of saved paths."""
    if not images:
        return []

    img_dir = os.path.join(output_dir, f"{slug}_images")
    os.makedirs(img_dir, exist_ok=True)
    saved = []

    for i, img in enumerate(images[:max_images]):
        try:
            url = img["url"]
            # Determine file extension
            from urllib.parse import urlparse
            path = urlparse(url).path.lower()
            ext = ".jpg"
            for e in [".png", ".webp", ".jpeg", ".gif", ".avif"]:
                if e in path:
                    ext = e
                    break

            filename = f"{slug}_img_{i+1:02d}{ext}"
            filepath = os.path.join(img_dir, filename)

            # Download
            raw = b""
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                raw = resp.read()[:5_000_000]  # 5MB max per image

            if len(raw) > 1000:  # Skip if too small (probably broken)
                with open(filepath, "wb") as f:
                    f.write(raw)
                img["local_path"] = filepath
                img["filename"] = filename
                img["size_bytes"] = len(raw)
                saved.append(img)
        except Exception:
            continue

    return saved


def _validate_product_category(data):
    """Cross-check the auto-detected category against actual ingredients.

    Claude sometimes miscategorizes products based on marketing text rather
    than the actual ingredient panel. This function overrides the category
    when the ingredients clearly indicate a different category.
    """
    ingredients = data.get("supplement_facts", {}).get("ingredients", [])
    if not ingredients:
        return

    # Normalize ingredient names for matching
    ing_names = {ing.get("name", "").lower().strip() for ing in ingredients}
    ing_text = " ".join(ing_names)
    current_cat = data.get("category", "")

    # Category indicator ingredients — if 3+ match, override category
    category_indicators = {
        "male_enhancement": {
            "keywords": [
                "tribulus", "maca", "horny goat weed", "epimedium",
                "muira puama", "catuaba", "tongkat ali", "l-arginine",
                "l-citrulline", "fenugreek", "yohimbe", "saw palmetto",
                "boron", "zinc", "d-aspartic acid", "fadogia",
            ],
            "min_matches": 3,
        },
        "brain_health": {
            "keywords": [
                "bacopa", "lion's mane", "huperzine", "phosphatidylserine",
                "alpha-gpc", "ginkgo", "vinpocetine", "citicoline",
                "noopept", "aniracetam", "piracetam", "dmae",
            ],
            "min_matches": 3,
        },
        "weight_loss": {
            "keywords": [
                "garcinia", "glucomannan", "conjugated linoleic",
                "green coffee", "raspberry ketone", "forskolin",
                "orlistat", "chitosan", "hydroxycitric",
            ],
            "min_matches": 2,
        },
        "blood_sugar": {
            "keywords": [
                "berberine", "bitter melon", "gymnema", "chromium picolinate",
                "banaba", "cinnamon", "alpha lipoic acid", "vanadium",
            ],
            "min_matches": 2,
        },
        "joint_health": {
            "keywords": [
                "glucosamine", "chondroitin", "msm", "boswellia",
                "hyaluronic acid", "collagen type ii", "turmeric",
                "curcumin",
            ],
            "min_matches": 2,
        },
        "nerve_health": {
            "keywords": [
                "alpha lipoic acid", "benfotiamine", "acetyl-l-carnitine",
                "b12", "methylcobalamin", "passionflower", "skullcap",
            ],
            "min_matches": 2,
        },
    }

    best_match = None
    best_count = 0

    for cat, config in category_indicators.items():
        match_count = sum(
            1 for kw in config["keywords"]
            if kw in ing_text
        )
        if match_count >= config["min_matches"] and match_count > best_count:
            best_match = cat
            best_count = match_count

    if best_match and best_match != current_cat:
        _emit(f"  [C15] Category override: {current_cat} → {best_match} "
              f"(based on {best_count} matching ingredients)")
        data["category"] = best_match
        data["_category_override"] = {
            "original": current_cat,
            "corrected": best_match,
            "reason": f"{best_count} ingredients match {best_match} profile",
        }


def phase1_extract_product(url, vsl_url=None, product_name=None, browser_session=None):
    """Scrape product page and extract structured data via Claude.

    Multi-layer extraction strategy:
    1. Direct page scrape (main URL + subpages)
    2. HTML supplement facts regex extraction
    3. Fallback to web search if direct scrape is thin
    4. Claude Haiku for structured extraction from combined text
    5. Quality check — if key fields missing, run targeted enrichment
    """
    print_phase(1, "PRODUCT PAGE EXTRACTION")

    all_pages = {}
    vsl_content = ""

    # Layer 1: Direct scrape (main URL + subpages)
    if url:
        _emit(f"  Fetching: {url}")
        all_pages = _try_multiple_urls(url, browser_session=browser_session)
        main_size = len(all_pages.get("main", ""))
        if main_size:
            _emit(f"  Main page: {main_size:,} bytes")
            _emit(f"  Total pages fetched: {len(all_pages)}")
        else:
            _emit(f"  Direct fetch failed — will try web search fallback")

    if vsl_url:
        _emit(f"  Fetching VSL: {vsl_url}")
        vsl_content = fetch_url(vsl_url, max_bytes=120000)
        # VSL pages are typically JS-heavy funnels — try browser if thin
        if browser_session and browser_session.available:
            try:
                from browser_fetch import is_content_thin
                if is_content_thin(vsl_content, min_text_chars=500):
                    _emit(f"    VSL content thin — retrying with browser rendering...")
                    rendered = browser_session.fetch(vsl_url)
                    if rendered and len(strip_html(rendered)) > len(strip_html(vsl_content)):
                        vsl_content = rendered
                        _emit(f"    Browser recovered {len(strip_html(vsl_content)):,} chars from VSL")
            except ImportError:
                pass
        if vsl_content:
            _emit(f"  Got {len(vsl_content):,} bytes from VSL")

    # Layer 2: Extract supplement facts from raw HTML
    supplement_facts_raw = ""
    for page_key, page_html in all_pages.items():
        sf = _extract_supplement_facts_html(page_html)
        if sf:
            supplement_facts_raw += sf
    # Also check VSL page for supplement facts — many landing pages list
    # ingredients below the video, not on the main product page
    if vsl_content:
        vsl_sf = _extract_supplement_facts_html(vsl_content)
        if vsl_sf and vsl_sf not in supplement_facts_raw:
            supplement_facts_raw += vsl_sf
            _emit(f"  Found supplement facts in VSL page")

    # Build combined text for extraction
    # Priority order: structured API data > WP API pages > main page HTML > raw subpages
    has_structured = "woocommerce_api" in all_pages or "json_ld" in all_pages
    has_wp_pages = any(k.startswith("wp_page_") for k in all_pages)
    page_texts = []

    # 1. Structured data sources first (most reliable)
    for key in ("woocommerce_api", "json_ld"):
        if key in all_pages:
            page_texts.append(f"=== {key.upper()} DATA (HIGH PRIORITY — USE THIS) ===\n{all_pages[key][:10000]}")

    # 2. WordPress API pages (real rendered content, not JS boilerplate)
    for key, content in all_pages.items():
        if key.startswith("wp_page_"):
            page_texts.append(f"=== SITE PAGE: {key.replace('wp_page_', '').upper()} ===\n{content[:6000]}")

    # 3. Main page HTML — cap aggressively if we have structured data
    if "main" in all_pages:
        text = strip_html(all_pages["main"])
        main_cap = 5000 if (has_structured or has_wp_pages) else 15000
        page_texts.append(f"MAIN PRODUCT PAGE:\n{text[:main_cap]}")

    # 4. Raw HTML subpages (only if no WP API pages — these are often JS-rendered garbage)
    if not has_wp_pages:
        for key, content in all_pages.items():
            if key not in ("main", "woocommerce_api", "json_ld") and not key.startswith("wp_page_"):
                text = strip_html(content)
                page_texts.append(f"SUBPAGE ({key}):\n{text[:4000]}")

    combined_text = "\n\n".join(page_texts)
    if supplement_facts_raw:
        combined_text += f"\n\nEXTRACTED SUPPLEMENT FACTS:\n{supplement_facts_raw}"
    if vsl_content:
        vsl_text = strip_html(vsl_content)
        # VSL pages often contain the FULL landing page below the video:
        # ingredients, pricing tiers, testimonials, guarantee, FAQs.
        # Give it generous space — this is primary source material.
        vsl_cap = 20000 if len(vsl_text) > 8000 else 8000
        combined_text += f"\n\nVIDEO SALES LETTER PAGE:\n{vsl_text[:vsl_cap]}"

    # Derive product name from URL if not provided
    if not product_name and url:
        from urllib.parse import urlparse
        parsed_url = urlparse(url)
        domain = (parsed_url.hostname or "").replace("www.", "")
        path = parsed_url.path.strip("/")

        # Try to extract from URL path first (e.g., /product/glp-3r/ → GLP-3R)
        path_parts = [p for p in path.split("/") if p and p not in ("product", "products", "shop", "item", "collections")]
        if path_parts:
            name_from_path = path_parts[-1].replace("-", " ").replace("_", " ").strip()
            if len(name_from_path) > 2:
                product_name = name_from_path.upper() if len(name_from_path) <= 8 else name_from_path.title()
                _emit(f"  Inferred product name from URL path: {product_name}")

        # Fallback to domain name
        if not product_name:
            name_from_url = domain.split(".")[0] if domain else ""
            if name_from_url and len(name_from_url) > 2:
                product_name = name_from_url.title()
                _emit(f"  Inferred product name from domain: {product_name}")

    # Layer 3: Web search fallback if direct scrape is thin
    main_text_len = len(strip_html(all_pages.get("main", "")))
    if main_text_len < 2000 or not supplement_facts_raw:
        name_for_search = product_name or ""
        if name_for_search:
            _emit(f"  [FALLBACK] Page content thin ({main_text_len} chars) — searching web for: {name_for_search}")
            for search_type in ["ingredients", "pricing", "reviews"]:
                search_result = _web_search_product(name_for_search, search_type)
                if search_result:
                    combined_text += f"\n\nWEB SEARCH ({search_type}):\n{search_result}"
                    _emit(f"    Got web search results for: {search_type}")
                time.sleep(1)  # Respect search rate limits

    # Layer 3b: Extract BuyGoods / ClickBank pricing from HTML data attributes
    main_html = all_pages.get("main", "")
    buygoods_pricing = _extract_buygoods_pricing(main_html)
    # Also check VSL page for pricing (often has buy buttons below the video)
    if not buygoods_pricing and vsl_content:
        buygoods_pricing = _extract_buygoods_pricing(vsl_content)
        if buygoods_pricing:
            _emit(f"  Found pricing in VSL page")
    if buygoods_pricing:
        pricing_text = "\n\nEXTRACTED PRICING (from checkout links):\n"
        for pkg in buygoods_pricing:
            pricing_text += f"- {pkg['package']}: ${pkg['total']} (was ${pkg['original']}) — {pkg['bottles']} bottles — Shipping: {pkg['shipping']} — {pkg['guarantee']}-day guarantee\n"
        combined_text += pricing_text
        _emit(f"  Extracted {len(buygoods_pricing)} pricing tiers from checkout links")

    if not combined_text and not product_name:
        _emit("  [!] No URL content and no product name — cannot proceed")
        return None

    # Layer 4: Claude extraction
    name_hint = f"Product Name: {product_name}\n" if product_name else ""
    url_hint = f"Official URL: {url}\n" if url else ""

    prompt = f"""TASK: Extract ALL verifiable product information from this source material.
{name_hint}{url_hint}
RULES:
- Extract ONLY what is explicitly stated in the source material. Do NOT invent data.
- Mark ALL health/efficacy claims as "verified": false
- CRITICAL: Extract EVERY ingredient, active compound, or key component mentioned anywhere — supplement facts panels, ingredient lists, "what's inside" sections, product specifications, descriptions, etc. Include ALL with their exact amounts and forms (mg, mcg, IU, etc.)
- For PEPTIDES or RESEARCH CHEMICALS: treat the peptide/compound itself as an ingredient. Extract purity %, molecular weight, sequence, CAS number, form (lyophilized, solution, etc.), and amount per vial/unit. Put purity in the "daily_value" field and form details in the "form" field.
- If a proprietary blend is listed, include the total blend amount and list each ingredient (even without individual amounts)
- For pricing: capture ALL tiers, per-unit cost, shipping costs
- For policies: capture EXACT refund duration, conditions, contact methods
- Extract the company name, address, email, phone from any contact/about sections
- If information is NOT present, use empty string "" or empty array []

Return ONLY valid JSON with this exact structure:
{{
    "product_name": "",
    "brand_name": "",
    "product_type": "supplement|peptide|research_chemical|telehealth|device|info_product|food|topical",
    "category": "weight_loss|brain_health|blood_sugar|male_enhancement|heart_health|anti_aging|sleep|joint_health|vision|dental|skin_care|immune_health|gut_health|nerve_health|respiratory|pain_relief|telehealth|financial|device|info_product",
    "official_url": "{url or ''}",
    "supplement_facts": {{
        "serving_size": "",
        "servings_per_container": "",
        "ingredients": [
            {{"name": "", "amount": "", "daily_value": "", "form": ""}}
        ],
        "other_ingredients": [],
        "proprietary_blend": false,
        "proprietary_blend_total": null,
        "allergen_warnings": []
    }},
    "pricing": [
        {{"package": "", "price": "", "per_unit": "", "shipping": ""}}
    ],
    "payment_processor": "",
    "subscription_available": false,
    "refund_policy": {{
        "duration_days": null,
        "conditions": "",
        "return_shipping": "",
        "contact_method": "",
        "verbatim": ""
    }},
    "shipping_policy": {{
        "domestic": "",
        "international": "",
        "delivery_time": ""
    }},
    "warranty": "",
    "company": {{
        "name": "",
        "address": "",
        "email": "",
        "phone": "",
        "website": ""
    }},
    "claims": [
        {{"claim": "", "source": "sales_page", "verified": false}}
    ],
    "testimonials": [
        {{"name": "", "location": "", "text": "", "source": "sales_page"}}
    ],
    "brand_faqs": [
        {{"q": "", "a": ""}}
    ]
}}

SOURCE MATERIAL:
{combined_text[:80000] if combined_text else f'Product name only: {product_name}. You may use your training knowledge about this product but mark ALL facts as needing verification.'}"""

    _emit("  Extracting structured data via Claude Haiku...")
    response = call_claude(prompt, max_tokens=6000)

    if not response:
        return _empty_product_data(url, product_name)

    # Parse JSON from response
    data = _parse_claude_json(response, product_name)

    # Ensure required fields
    data.setdefault("product_name", product_name or "Unknown")
    data.setdefault("official_url", url or "")
    data.setdefault("supplement_facts", {"ingredients": []})
    data.setdefault("pricing", [])
    data.setdefault("claims", [])

    # Layer 5: Quality check — if we still got no ingredients, try one more targeted extraction
    ingredient_count = len(data.get("supplement_facts", {}).get("ingredients", []))
    if ingredient_count == 0 and data.get("product_name") and data["product_name"] != "Unknown":
        _emit(f"  [ENRICHMENT] No ingredients found — running targeted ingredient search...")
        enrichment = _enrich_ingredients(data["product_name"])
        if enrichment:
            data["supplement_facts"]["ingredients"] = enrichment
            ingredient_count = len(enrichment)
            _emit(f"  [ENRICHMENT] Found {ingredient_count} ingredients via targeted search")

    # Layer 5b: NIH DSLD cross-reference — government-verified label data
    product_type = data.get("product_type", "supplement")
    if product_type in ("supplement", "food", "topical"):
        pname = data.get("product_name", product_name or "")
        bname = data.get("brand_name", "")
        if pname:
            _emit(f"  [DSLD] Querying NIH Dietary Supplement Label Database for: {pname}")
            dsld_data = _query_dsld(pname, bname)
            if dsld_data and dsld_data.get("ingredients"):
                dsld_ingredients = dsld_data["ingredients"]
                current_count = len(data.get("supplement_facts", {}).get("ingredients", []))

                if current_count == 0:
                    # No ingredients from any source — use DSLD as primary
                    data["supplement_facts"]["ingredients"] = dsld_ingredients
                    data["supplement_facts"]["_source"] = "dsld_verified"
                    data["supplement_facts"]["_dsld_match_name"] = dsld_data.get("dsld_product_name", "")
                    data["supplement_facts"]["_dsld_match_brand"] = dsld_data.get("dsld_brand", "")
                    data["supplement_facts"]["_dsld_id"] = dsld_data.get("dsld_id", "")
                    _emit(f"  [DSLD] Using DSLD as PRIMARY ingredient source ({len(dsld_ingredients)} ingredients)")
                    _emit(f"  [DSLD] Matched DSLD product: {dsld_data.get('dsld_product_name', '')} by {dsld_data.get('dsld_brand', '')}")
                else:
                    # We have ingredients already — store DSLD as cross-reference
                    data["dsld_cross_reference"] = dsld_data
                    _emit(f"  [DSLD] Stored as cross-reference ({len(dsld_ingredients)} DSLD vs {current_count} extracted)")

                # DSLD serving info fills gaps
                if dsld_data.get("serving_size") and not data["supplement_facts"].get("serving_size"):
                    data["supplement_facts"]["serving_size"] = dsld_data["serving_size"]
                    _emit(f"  [DSLD] Serving size from DSLD: {dsld_data['serving_size']}")
                if dsld_data.get("servings_per_container") and not data["supplement_facts"].get("servings_per_container"):
                    data["supplement_facts"]["servings_per_container"] = dsld_data["servings_per_container"]

                # DSLD contact info fills gaps
                if dsld_data.get("contact"):
                    co = data.get("company", {})
                    dsld_co = dsld_data["contact"]
                    if not co.get("name") and dsld_co.get("name"):
                        data.setdefault("company", {})["name"] = dsld_co["name"]
                    if not co.get("phone") and dsld_co.get("phone"):
                        data.setdefault("company", {})["phone"] = dsld_co["phone"]
                    if not co.get("address") and dsld_co.get("address"):
                        data.setdefault("company", {})["address"] = dsld_co["address"]

                # Store DSLD ID for reference
                data["dsld_id"] = dsld_data.get("dsld_id")
            elif dsld_data:
                _emit(f"  [DSLD] Match found but no ingredient data")
            else:
                _emit(f"  [DSLD] No match in DSLD database")

    # Layer 5c: Category validation — cross-check category vs actual ingredients
    _validate_product_category(data)

    # Image extraction — grab product images for reference
    product_images = []
    main_html = all_pages.get("main", "")
    if main_html and url:
        _emit("  Extracting product images...")
        raw_images = _extract_product_images(main_html, url)
        if raw_images:
            slug = slugify(data.get("product_name", "product"))
            product_images = _download_product_images(raw_images, OUTPUT_DIR, slug)
            _emit(f"  Downloaded {len(product_images)} product images")
    data["product_images"] = product_images

    # Layer 6: Auto-OCR label images if ingredients are still empty
    ingredient_count = len(data.get("supplement_facts", {}).get("ingredients", []))
    if ingredient_count == 0 and product_images:
        _emit("  [LABEL OCR] No ingredients found — scanning downloaded images for supplement facts labels...")
        # Patterns that indicate a label/supplement facts image
        label_patterns = re.compile(
            r'(rotulo|label|supplement.?facts|nutrition.?facts|ingredients|'
            r'supp.?fact|product.?label|sfp|back.?label|panel)',
            re.IGNORECASE
        )
        for img in product_images:
            local_path = img.get("local_path", "")
            img_url = img.get("url", "")
            alt = img.get("alt", "")
            filename = img.get("filename", "")
            # Check if image looks like a label
            if label_patterns.search(img_url) or label_patterns.search(alt) or label_patterns.search(filename):
                _emit(f"  [LABEL OCR] Found likely label image: {os.path.basename(local_path)}")
                if local_path and os.path.exists(local_path):
                    label_result = extract_label_image(local_path)
                    if label_result:
                        # Handle both dict (new) and list (old) return formats
                        if isinstance(label_result, dict):
                            data["supplement_facts"]["ingredients"] = label_result["ingredients"]
                            if label_result.get("serving_size"):
                                data["supplement_facts"]["serving_size"] = label_result["serving_size"]
                            if label_result.get("servings_per_container"):
                                data["supplement_facts"]["servings_per_container"] = label_result["servings_per_container"]
                        else:
                            data["supplement_facts"]["ingredients"] = label_result
                        data["supplement_facts"]["_source"] = "auto_label_ocr"
                        ingredient_count = len(data["supplement_facts"]["ingredients"])
                        _emit(f"  [LABEL OCR] Extracted {ingredient_count} ingredients from label image")
                        break

        # If no label-pattern match found but still no ingredients, try ALL images
        # (some sites use generic image names for labels)
        if ingredient_count == 0 and len(product_images) <= 5:
            _emit("  [LABEL OCR] No label-named images — trying all downloaded images...")
            for img in product_images:
                local_path = img.get("local_path", "")
                if local_path and os.path.exists(local_path):
                    size = img.get("size_bytes", 0)
                    # Skip tiny images (icons) and huge images (hero banners are usually > 1MB)
                    if size < 10000 or size > 2000000:
                        continue
                    _emit(f"  [LABEL OCR] Trying: {os.path.basename(local_path)} ({size // 1024}KB)")
                    label_result = extract_label_image(local_path)
                    if label_result:
                        ings = label_result["ingredients"] if isinstance(label_result, dict) else label_result
                        if len(ings) >= 2:
                            data["supplement_facts"]["ingredients"] = ings
                            if isinstance(label_result, dict):
                                if label_result.get("serving_size"):
                                    data["supplement_facts"]["serving_size"] = label_result["serving_size"]
                                if label_result.get("servings_per_container"):
                                    data["supplement_facts"]["servings_per_container"] = label_result["servings_per_container"]
                            data["supplement_facts"]["_source"] = "auto_label_ocr"
                            ingredient_count = len(ings)
                            _emit(f"  [LABEL OCR] Extracted {ingredient_count} ingredients from image")
                            break

    _emit(f"  Extracted: {data.get('product_name', 'Unknown')}")
    _emit(f"  Category: {data.get('category', 'unknown')}")
    _emit(f"  Ingredients found: {ingredient_count}")
    _emit(f"  Pricing tiers: {len(data.get('pricing', []))}")
    _emit(f"  Claims captured: {len(data.get('claims', []))}")
    _emit(f"  Images saved: {len(product_images)}")

    return data


def _empty_product_data(url=None, product_name=None):
    """Return empty product data structure."""
    return {
        "product_name": product_name or "Unknown",
        "brand_name": "",
        "product_type": "supplement",
        "category": "",
        "official_url": url or "",
        "supplement_facts": {"serving_size": "", "servings_per_container": "", "ingredients": [], "other_ingredients": [], "proprietary_blend": False, "proprietary_blend_total": None, "allergen_warnings": []},
        "pricing": [],
        "payment_processor": "",
        "subscription_available": False,
        "refund_policy": {"duration_days": None, "conditions": "", "return_shipping": "", "contact_method": "", "verbatim": ""},
        "shipping_policy": {"domestic": "", "international": "", "delivery_time": ""},
        "warranty": "",
        "company": {"name": "", "address": "", "email": "", "phone": "", "website": ""},
        "claims": [],
        "testimonials": [],
        "brand_faqs": [],
    }


def _parse_claude_json(response, fallback_name=None):
    """Parse JSON from Claude's response, handling markdown fences and partial JSON."""
    try:
        # Remove markdown code fences
        clean = re.sub(r'```json\s*', '', response)
        clean = re.sub(r'```\s*$', '', clean)
        # Find the outermost JSON object
        brace_count = 0
        start = None
        for i, c in enumerate(clean):
            if c == '{':
                if start is None:
                    start = i
                brace_count += 1
            elif c == '}':
                brace_count -= 1
                if brace_count == 0 and start is not None:
                    return json.loads(clean[start:i+1])
        # Fallback: try regex
        json_match = re.search(r'\{[\s\S]*\}', clean)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(clean)
    except json.JSONDecodeError:
        _emit(f"  [!] Failed to parse extraction JSON — using minimal data")
        return {"product_name": fallback_name or "Unknown", "category": ""}


def _enrich_ingredients(product_name):
    """Last-resort ingredient extraction via Claude's training knowledge + web search."""

    # First, try to get ingredients from web search results
    search_text = _web_search_product(product_name, "ingredients")
    if search_text and len(search_text) > 100:
        prompt = f"""Based on these search results about {product_name}, extract the ingredient list.

SEARCH RESULTS:
{search_text[:5000]}

Return ONLY a valid JSON array of ingredients:
[
    {{"name": "Ingredient Name", "amount": "500mg", "daily_value": "", "form": "extract"}}
]

Rules:
- Only include ingredients explicitly mentioned in these search results
- Include exact amounts if shown
- If no ingredients found, return an empty array: []"""

        response = call_claude(prompt, max_tokens=2000)
        ingredients = _parse_ingredient_array(response)
        if ingredients:
            for ing in ingredients:
                ing["verified"] = False
                ing["source"] = "web_search"
            return ingredients

    # Fallback: ask Claude from training knowledge
    prompt = f"""What are the known ingredients, active compounds, or key components in the product called "{product_name}"?

This could be a dietary supplement, peptide, research chemical, device, or other product sold online.
For supplements: list ingredients with amounts and forms.
For peptides: list the peptide compound(s) with purity, amount per vial, and form (lyophilized, etc).
For devices: list key functional components or active technologies.

Return ONLY a valid JSON array:
[
    {{"name": "Component Name", "amount": "500mg", "daily_value": "", "form": "extract"}}
]

Rules:
- Only include components you are confident are in this specific product
- Include exact amounts if known
- If you're not sure about this product, return an empty array: []
- Do NOT invent or guess — if you don't know, return []"""

    response = call_claude(prompt, max_tokens=2000)
    ingredients = _parse_ingredient_array(response)
    if ingredients:
        for ing in ingredients:
            ing["verified"] = False
            ing["source"] = "ai_knowledge"
    return ingredients


def _parse_ingredient_array(response):
    """Parse a JSON array of ingredients from Claude's response."""
    if not response:
        return []
    try:
        clean = re.sub(r'```json\s*', '', response)
        clean = re.sub(r'```\s*$', '', clean)
        arr_match = re.search(r'\[[\s\S]*\]', clean)
        if arr_match:
            ingredients = json.loads(arr_match.group())
            if isinstance(ingredients, list) and ingredients:
                return ingredients
    except (json.JSONDecodeError, AttributeError):
        pass
    return []


# ============================================================================
# PHASE 2: PubMed Research Harvesting
# ============================================================================

def pubmed_search(ingredient, max_results=None):
    """Search PubMed for an ingredient. Returns list of PMIDs."""
    if max_results is None:
        max_results = PUBMED_MAX_RESULTS

    query = f"{ingredient}[Title/Abstract] AND (supplement OR efficacy OR clinical trial OR safety OR health)"
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": str(max_results),
        "retmode": "json",
        "sort": "relevance",
    }
    if PUBMED_API_KEY:
        params["api_key"] = PUBMED_API_KEY

    url = f"{PUBMED_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    try:
        resp = fetch_url(url, max_bytes=50000)
        data = json.loads(resp)
        pmids = data.get("esearchresult", {}).get("idlist", [])
        return pmids
    except Exception as e:
        _emit(f"    [!] PubMed search error for '{ingredient}': {e}")
        return []


def pubmed_fetch_abstracts(pmids):
    """Fetch abstracts for a list of PMIDs. Returns list of study dicts."""
    if not pmids:
        return []

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "xml",
        "retmode": "xml",
    }
    if PUBMED_API_KEY:
        params["api_key"] = PUBMED_API_KEY

    url = f"{PUBMED_FETCH_URL}?{urllib.parse.urlencode(params)}"
    try:
        resp = fetch_url(url, max_bytes=500000)
        if not resp:
            return []

        root = ET.fromstring(resp)
        studies = []

        for article in root.findall('.//PubmedArticle'):
            try:
                pmid = article.findtext('.//PMID', '')
                title = article.findtext('.//ArticleTitle', '')
                journal = article.findtext('.//Journal/Title', '') or article.findtext('.//ISOAbbreviation', '')
                year_el = article.find('.//PubDate/Year')
                year = int(year_el.text) if year_el is not None and year_el.text else None

                # Authors (first 3)
                authors = []
                for author in article.findall('.//Author')[:3]:
                    last = author.findtext('LastName', '')
                    init = author.findtext('Initials', '')
                    if last:
                        authors.append(f"{last} {init}".strip())
                author_str = ", ".join(authors)
                if len(article.findall('.//Author')) > 3:
                    author_str += " et al."

                # Abstract
                abstract_parts = []
                for abs_text in article.findall('.//AbstractText'):
                    label = abs_text.get('Label', '')
                    text = abs_text.text or ''
                    if label:
                        abstract_parts.append(f"{label}: {text}")
                    else:
                        abstract_parts.append(text)
                abstract = " ".join(abstract_parts)[:2000]

                if title and pmid:
                    studies.append({
                        "pmid": pmid,
                        "title": title,
                        "journal": journal,
                        "year": year,
                        "authors": author_str,
                        "abstract": abstract,
                    })
            except Exception:
                continue

        return studies
    except Exception as e:
        _emit(f"    [!] PubMed fetch error: {e}")
        return []


def tag_study_relevance(study):
    """Tag a study with relevance categories and assign quality tier."""
    text = f"{study.get('title', '')} {study.get('abstract', '')}".lower()
    tags = []

    tag_patterns = {
        "efficacy": ["efficacy", "effective", "benefit", "improve", "support", "enhance", "reduce"],
        "safety": ["adverse", "side effect", "tolerability", "toxicity", "safety"],
        "dosage": ["dosage", "dose", "bioavailability", "absorption", "pharmacokinetic"],
        "mechanism": ["mechanism", "pathway", "receptor", "enzyme", "signal"],
        "clinical_trial": ["clinical trial", "randomized", "controlled", "double-blind", "placebo"],
        "review": ["review", "meta-analysis", "systematic"],
        "human_study": ["human", "participant", "subject", "patient", "volunteer", "men", "women", "adult"],
        "preclinical": ["in vitro", "cell culture", "in vivo", "animal", "rat", "mouse", "mice"],
    }

    for tag, patterns in tag_patterns.items():
        if any(p in text for p in patterns):
            tags.append(tag)

    # Quality tier
    if ("clinical_trial" in tags and "human_study" in tags) or "review" in tags:
        tier = "gold"
    elif "human_study" in tags:
        tier = "silver"
    elif "preclinical" in tags:
        tier = "bronze"
    else:
        tier = "standard"

    study["relevance_tags"] = tags
    study["quality_tier"] = tier
    return study


def phase2_pubmed_research(product_data):
    """Research each ingredient via PubMed. Returns ingredient research dict."""
    print_phase(2, "PUBMED RESEARCH HARVESTING")

    ingredients = product_data.get("supplement_facts", {}).get("ingredients", [])
    if not ingredients:
        _emit("  No ingredients found — skipping PubMed research")
        return {}

    # Load cached research
    ingredient_db = load_ingredient_db()

    # Limit ingredients to research
    ingredient_names = []
    for ing in ingredients[:PUBMED_MAX_INGREDIENTS]:
        name = ing.get("name", "").strip()
        if name and len(name) > 2:
            # Normalize: remove form descriptors for search
            clean = re.sub(r'\s*\(.*?\)\s*', '', name)
            clean = re.sub(r'\s+(?:as|from|extract|powder|root|leaf|bark|fruit|seed)\b.*', '', clean, flags=re.IGNORECASE)
            clean = clean.strip()
            if clean:
                ingredient_names.append((clean, ing.get("amount", "")))

    _emit(f"  Researching {len(ingredient_names)} ingredients...")
    research = {}

    for name, dose in ingredient_names:
        key = name.lower().strip()

        # Check cache — use if fresh and has enough studies
        cached = ingredient_db.get(key, {})
        cache_date = cached.get("last_updated", "")
        is_fresh = False
        if cache_date:
            try:
                days_old = (datetime.now() - datetime.strptime(cache_date, "%Y-%m-%d")).days
                is_fresh = days_old < 30
            except ValueError:
                pass

        if cached.get("studies") and is_fresh and len(cached["studies"]) >= 5:
            _emit(f"  [CACHE] {name}: {len(cached['studies'])} studies (fresh)")
            research[name] = cached.copy()
            research[name]["product_dose"] = dose
            continue

        _emit(f"  [SEARCH] {name}...")
        time.sleep(PUBMED_DELAY)

        # Search PubMed
        pmids = pubmed_search(name)
        if not pmids:
            _emit(f"    No results for '{name}'")
            if cached.get("studies"):
                # Use stale cache rather than nothing
                _emit(f"    Using cached data ({len(cached['studies'])} studies)")
                research[name] = cached.copy()
                research[name]["product_dose"] = dose
            else:
                research[name] = {
                    "product_dose": dose,
                    "clinical_dose_range": "",
                    "evidence_grade": "Insufficient",
                    "studies": [],
                }
            continue

        time.sleep(PUBMED_DELAY)

        # Fetch abstracts
        studies = pubmed_fetch_abstracts(pmids)
        tagged = [tag_study_relevance(s) for s in studies]

        grade = _compute_evidence_grade(tagged)

        entry = {
            "product_dose": dose,
            "clinical_dose_range": "",  # Will be enriched in Phase 3
            "evidence_grade": grade,
            "studies": tagged,
            "last_updated": datetime.now().strftime("%Y-%m-%d"),
        }

        # Merge with existing cache instead of overwriting (compounding KB)
        if cached.get("studies"):
            cache_entry = {k: v for k, v in cached.items()}
            merge_ingredient_research(cache_entry, entry)
            ingredient_db[key] = cache_entry
            # Use merged data for this product too
            research[name] = cache_entry.copy()
            research[name]["product_dose"] = dose
            _emit(f"    Merged: {len(cache_entry['studies'])} total studies (grade: {cache_entry['evidence_grade']})")
        else:
            research[name] = entry
            # Cache in ingredient_db (without product_dose which is product-specific)
            cache_entry = {k: v for k, v in entry.items() if k != "product_dose"}
            ingredient_db[key] = cache_entry
            _emit(f"    Found {len(tagged)} studies (grade: {grade})")

    # Save updated cache
    save_ingredient_db(ingredient_db)
    _emit(f"  Ingredient DB now has {len(ingredient_db)} ingredients cached")

    return research


# ============================================================================
# PHASE 3: Safety & Drug Interaction Research
# ============================================================================

def phase3_safety_research(product_data, ingredient_research):
    """Research safety data for each ingredient."""
    print_phase(3, "SAFETY & DRUG INTERACTION RESEARCH")

    ingredients = product_data.get("supplement_facts", {}).get("ingredients", [])
    if not ingredients:
        _emit("  No ingredients — skipping safety research")
        return {}

    ingredient_db = load_ingredient_db()
    safety_data = {}

    for ing in ingredients[:PUBMED_MAX_INGREDIENTS]:
        name = ing.get("name", "").strip()
        if not name:
            continue

        key = name.lower().strip()
        clean_name = re.sub(r'\s*\(.*?\)\s*', '', name).strip()

        # Check cache for safety data — use if it has meaningful content
        cached_safety = ingredient_db.get(key, {})
        has_cached_safety = (
            cached_safety.get("side_effects")
            or cached_safety.get("drug_interactions")
            or cached_safety.get("contraindications")
        )
        if has_cached_safety:
            _emit(f"  [CACHE] {name}: safety data cached")
            safety_data[name] = {
                "side_effects": cached_safety.get("side_effects", []),
                "drug_interactions": cached_safety.get("drug_interactions", []),
                "contraindications": cached_safety.get("contraindications", []),
            }
            continue

        # Search PubMed for safety-specific studies
        _emit(f"  [SEARCH] Safety: {clean_name}...")
        time.sleep(PUBMED_DELAY)

        query_name = clean_name
        pmids = pubmed_search(f"{query_name} AND (adverse OR interaction OR contraindication OR safety OR side effect)", max_results=5)

        safety_studies = []
        if pmids:
            time.sleep(PUBMED_DELAY)
            safety_studies = pubmed_fetch_abstracts(pmids)

        # Use Claude to synthesize safety data from abstracts
        if safety_studies:
            abstracts_text = "\n".join([
                f"- {s.get('title', '')} ({s.get('journal', '')}, {s.get('year', '')}): {s.get('abstract', '')[:500]}"
                for s in safety_studies[:5]
            ])

            safety_prompt = f"""Based on these PubMed studies about {clean_name}, extract safety information.
Return ONLY valid JSON:
{{
    "side_effects": ["list of known side effects"],
    "drug_interactions": [
        {{"drug_class": "", "interaction": "", "severity": "Low|Moderate|High"}}
    ],
    "contraindications": ["list of contraindications"],
    "clinical_dose_range": "e.g. 500-1500mg/day"
}}

Studies:
{abstracts_text}

Extract ONLY what the studies support. If no data, use empty arrays."""

            resp = call_claude(safety_prompt, max_tokens=1500)
            try:
                json_match = re.search(r'\{[\s\S]*\}', resp)
                if json_match:
                    parsed = json.loads(json_match.group())
                    safety_data[name] = {
                        "side_effects": parsed.get("side_effects", []),
                        "drug_interactions": parsed.get("drug_interactions", []),
                        "contraindications": parsed.get("contraindications", []),
                    }
                    # Merge safety data into ingredient_db cache
                    if key not in ingredient_db:
                        ingredient_db[key] = {}
                    merge_ingredient_research(ingredient_db[key], {
                        "side_effects": parsed.get("side_effects", []),
                        "drug_interactions": parsed.get("drug_interactions", []),
                        "contraindications": parsed.get("contraindications", []),
                        "clinical_dose_range": parsed.get("clinical_dose_range", ""),
                    })

                    _emit(f"    {len(parsed.get('side_effects', []))} side effects, {len(parsed.get('drug_interactions', []))} interactions")
                    continue
            except (json.JSONDecodeError, AttributeError):
                pass

        safety_data[name] = {"side_effects": [], "drug_interactions": [], "contraindications": []}
        _emit(f"    No safety data found")

    save_ingredient_db(ingredient_db)
    return safety_data


# ============================================================================
# PHASE 4: Keyword Research (via web search simulation)
# ============================================================================

def phase4_keyword_research(product_data):
    """Generate keyword intelligence for the product."""
    print_phase(4, "KEYWORD RESEARCH")

    name = product_data.get("product_name", "")
    category = product_data.get("category", "")
    brand = product_data.get("brand_name", "")

    if not name:
        _emit("  No product name — skipping keyword research")
        return {}

    _emit(f"  Generating keyword intelligence for: {name}")

    # Build keyword sets from product data
    keywords = {
        "primary": [
            f"{name} review",
            f"{name} review 2026",
            f"{name} supplement",
            f"{name} reviews",
        ],
        "buyer_intent": [
            f"{name} where to buy",
            f"{name} official website",
            f"{name} coupon code",
            f"{name} discount",
            f"{name} pricing",
            f"buy {name}",
        ],
        "informational": [],
        "comparison": [
            f"{name} vs",
            f"{name} alternatives",
            f"is {name} worth it",
        ],
        "safety_queries": [
            f"{name} side effects",
            f"{name} ingredients",
            f"is {name} safe",
            f"is {name} legit",
            f"{name} complaints",
            f"{name} scam",
        ],
        "people_also_ask": [
            f"Does {name} really work?",
            f"What are the ingredients in {name}?",
            f"Is {name} FDA approved?",
            f"How long does {name} take to work?",
            f"Can you buy {name} on Amazon?",
            f"What are the side effects of {name}?",
        ],
    }

    # Add category-specific informational keywords
    category_keywords = {
        "blood_sugar": ["best blood sugar supplements 2026", "natural blood sugar support", "berberine supplements review"],
        "weight_loss": ["best weight loss supplements 2026", "natural fat burners", "metabolism boosters review"],
        "brain_health": ["best nootropics 2026", "memory supplements review", "cognitive support supplements"],
        "male_enhancement": ["best male enhancement pills 2026", "natural testosterone boosters"],
        "heart_health": ["best heart health supplements 2026", "CoQ10 supplements review"],
        "anti_aging": ["best anti-aging supplements 2026", "NMN supplements review", "NAD+ supplements"],
        "sleep": ["best natural sleep aids 2026", "melatonin alternatives"],
        "joint_health": ["best joint supplements 2026", "glucosamine alternatives"],
        "vision": ["best eye health supplements 2026", "lutein supplements review"],
        "gut_health": ["best probiotics 2026", "gut health supplements review"],
        "immune_health": ["best immune support supplements 2026", "elderberry supplements review"],
    }
    keywords["informational"] = category_keywords.get(category, [f"best {category} supplements 2026"])

    # Add brand-specific queries
    if brand and brand.lower() != name.lower():
        keywords["primary"].append(f"{brand} {name}")
        keywords["primary"].append(f"{brand} review")

    # Ingredient-specific keywords
    ingredients = product_data.get("supplement_facts", {}).get("ingredients", [])
    for ing in ingredients[:5]:
        ing_name = ing.get("name", "")
        if ing_name:
            keywords["informational"].append(f"{ing_name} benefits")
            keywords["informational"].append(f"{ing_name} supplement dosage")

    total = sum(len(v) for v in keywords.values())
    _emit(f"  Generated {total} keyword targets across {len(keywords)} categories")

    return keywords


# ============================================================================
# PHASE 5: Third-Party Reputation Check
# ============================================================================

def phase5_reputation_check(product_data):
    """Check third-party reputation signals. Returns reputation dict."""
    print_phase(5, "THIRD-PARTY REPUTATION CHECK")

    name = product_data.get("product_name", "")
    brand = product_data.get("brand_name", "")

    if not name:
        return {}

    _emit(f"  Generating reputation check queries for: {name}")

    # We generate the search queries that should be run
    # (actual WebSearch happens at runtime when the tool is invoked via Claude Code)
    reputation = {
        "search_queries_to_run": [
            f'"{name}" review site:reddit.com',
            f'"{name}" site:trustpilot.com',
            f'"{name}" OR "{brand}" site:bbb.org' if brand else f'"{name}" site:bbb.org',
            f'"{name}" complaint OR scam OR warning',
            f'"{brand}" FDA warning letter' if brand else f'"{name}" FDA warning',
            f'"{brand}" lawsuit' if brand else f'"{name}" lawsuit',
        ],
        "bbb_rating": "Check required",
        "trustpilot_rating": "Check required",
        "reddit_sentiment": "Check required",
        "fda_warnings": "Check required",
        "lawsuits": "Check required",
        "common_complaints": [],
        "common_praise": [],
        "note": "Run these searches manually or via WebSearch to populate this section",
    }

    _emit(f"  Generated {len(reputation['search_queries_to_run'])} reputation check queries")

    # Query FDA CAERS for adverse event reports
    product_type = product_data.get("product_type", "supplement")
    if product_type in ("supplement", "food", "topical"):
        _emit(f"  [FDA CAERS] Querying adverse event reports...")
        caers = _query_fda_caers(name, brand)
        if caers and caers.get("total_reports", 0) > 0:
            reputation["fda_caers"] = caers
            reputation["fda_caers_summary"] = (
                f"{caers['total_reports']} adverse event reports found. "
                f"Top reactions: {', '.join(r['reaction'] for r in caers.get('top_reactions', [])[:5])}. "
                f"Note: CAERS reports are unverified and do not establish causation."
            )
            _emit(f"  [FDA CAERS] {caers['total_reports']} reports found")
        else:
            reputation["fda_caers"] = {"total_reports": 0, "note": "No adverse event reports found in FDA CAERS"}
            _emit(f"  [FDA CAERS] No adverse event reports found")

    return reputation


# ============================================================================
# PHASE 6: Competitive Landscape
# ============================================================================

def phase6_competitive_landscape(product_data):
    """Identify competitive landscape. Returns competitor data."""
    print_phase(6, "COMPETITIVE LANDSCAPE")

    name = product_data.get("product_name", "")
    category = product_data.get("category", "")

    if not name:
        return {}

    _emit(f"  Generating competitive analysis queries for: {name} ({category})")

    competitive = {
        "search_queries_to_run": [
            f'"{name}" vs',
            f'best {category.replace("_", " ")} supplements 2026' if category else f'{name} alternatives',
            f'{name} alternative',
        ],
        "competitors": [],
        "note": "Run these searches to identify direct competitors",
    }

    _emit(f"  Generated {len(competitive['search_queries_to_run'])} competitive queries")
    return competitive


# ============================================================================
# PHASE 7: Compliance Pre-Check
# ============================================================================

def phase7_compliance_check(product_data):
    """Audit product claims for YMYL/FTC compliance. Returns compliance dict."""
    print_phase(7, "COMPLIANCE PRE-CHECK")

    category = product_data.get("category", "")
    claims = product_data.get("claims", [])

    # YMYL classification
    risk_level = YMYL_CATEGORIES.get(category, "Moderate")
    _emit(f"  YMYL Category: {category} (Risk: {risk_level})")

    # Audit each claim
    claim_audit = []
    for claim_obj in claims:
        claim_text = claim_obj.get("claim", "") if isinstance(claim_obj, dict) else str(claim_obj)
        issues = []
        safe_alt = claim_text

        claim_lower = claim_text.lower()
        for flag in CLAIM_RED_FLAGS:
            if flag.lower() in claim_lower:
                issues.append(f"Contains '{flag}' — must hedge for YMYL compliance")
                # Try to find a hedge replacement (match stems: "reverse"/"reverses", etc.)
                for original, replacement in HEDGE_ALTERNATIVES.items():
                    orig_lower = original.lower()
                    # Match exact or stem (e.g., "reverse" matches "reverses")
                    if orig_lower in claim_lower or orig_lower.rstrip("s") in claim_lower:
                        # Find the actual word in the claim to replace
                        pattern = re.compile(re.escape(orig_lower.rstrip("s")) + r'e?s?', re.IGNORECASE)
                        safe_alt = pattern.sub(replacement, claim_text)
                        break

        if issues:
            claim_audit.append({
                "claim": claim_text,
                "issues": issues,
                "safe_alternative": safe_alt,
            })

    # CVD-9: Disease-reversal claim detection
    # Claims that combine a reversal verb with a disease/condition term cannot be
    # attributed, hedged, or softened — they must be excluded entirely.
    # A reader with diabetes could plausibly delay medical care based on these claims.
    cvd9_blocked = []
    for claim_obj in claims:
        claim_text = claim_obj.get("claim", "") if isinstance(claim_obj, dict) else str(claim_obj)
        claim_lower = claim_text.lower()
        matched_verb = None
        matched_disease = None
        for verb in CVD9_REVERSAL_VERBS:
            if verb in claim_lower:
                matched_verb = verb
                break
        if matched_verb:
            for disease in CVD9_DISEASE_TERMS:
                if disease in claim_lower:
                    matched_disease = disease
                    break
        if matched_verb and matched_disease:
            cvd9_blocked.append({
                "claim": claim_text,
                "verb": matched_verb,
                "disease": matched_disease,
                "reason": f"Disease-reversal claim: '{matched_verb}' + '{matched_disease}' — cannot be attributed or hedged, must be excluded entirely",
            })

    # AccessWire blocklist check — scan PR-relevant fields AND individual claims
    # to identify which specific claims contain blocked terms
    pr_fields = []
    for key in ["product_name", "description", "tagline", "manufacturer_claims"]:
        val = product_data.get(key)
        if isinstance(val, str):
            pr_fields.append(val)
        elif isinstance(val, list):
            pr_fields.extend(str(v) for v in val)
    for ing in product_data.get("ingredients", []):
        if isinstance(ing, dict):
            pr_fields.append(ing.get("name", ""))
            pr_fields.append(ing.get("description", ""))
        elif isinstance(ing, str):
            pr_fields.append(ing)
    for claim in product_data.get("claims", []):
        if isinstance(claim, dict):
            pr_fields.append(claim.get("text", ""))
        elif isinstance(claim, str):
            pr_fields.append(claim)
    all_text = " ".join(pr_fields).lower()
    flagged_terms = [term for term in ACCESSWIRE_BLOCKLIST if term in all_text]

    # Identify which specific claims contain blocked terms so the prompt can tag them
    blocklist_blocked_claims = []
    for claim_obj in claims:
        claim_text = claim_obj.get("claim", "") if isinstance(claim_obj, dict) else str(claim_obj)
        claim_lower = claim_text.lower()
        matched_terms = [term for term in ACCESSWIRE_BLOCKLIST if term in claim_lower]
        if matched_terms:
            blocklist_blocked_claims.append({
                "claim": claim_text,
                "matched_terms": matched_terms,
                "reason": f"Contains banned terms: {', '.join(matched_terms)} — cannot appear in any publishable content",
            })

    # Required disclaimers
    disclaimers = [
        "FDA disclaimer: These statements have not been evaluated by the FDA. This product is not intended to diagnose, treat, cure, or prevent any disease.",
        "Individual results may vary.",
        "Consult your healthcare provider before starting any supplement regimen.",
    ]
    if product_data.get("pricing"):
        disclaimers.append("Affiliate disclosure: This article may contain affiliate links.")

    compliance = {
        "ymyl_category": f"Health - {category.replace('_', ' ').title()}",
        "risk_level": risk_level,
        "fda_disclaimer_required": product_data.get("product_type") in ["supplement", "telehealth", "food", "topical"],
        "ftc_affiliate_disclosure_required": True,
        "claim_audit": claim_audit,
        "flagged_claims_count": len(claim_audit),
        "cvd9_blocked_claims": cvd9_blocked,
        "required_disclaimers": disclaimers,
        "accesswire_blocklist_check": {
            "passes": len(flagged_terms) == 0,
            "flagged_terms": flagged_terms,
            "blocked_claims": blocklist_blocked_claims,
        },
        "barchart_compliance": {
            "passes": category not in ("male_enhancement",),
            "notes": "Male enhancement — manual review recommended (you've published this category before)" if category == "male_enhancement" else "Category allowed",
        },
    }

    _emit(f"  Claims audited: {len(claims)}")
    _emit(f"  Flagged claims: {len(claim_audit)}")
    if cvd9_blocked:
        _emit(f"  CVD-9 BLOCKED: {len(cvd9_blocked)} disease-reversal claims (will be excluded from prompt)")
    _emit(f"  AccessWire blocklist: {'PASS' if not flagged_terms else f'FAIL ({len(flagged_terms)} terms)'}")
    if blocklist_blocked_claims:
        _emit(f"  Blocklist-blocked claims: {len(blocklist_blocked_claims)} claims contain banned terms (will be excluded from prompt)")

    return compliance


# ============================================================================
# PHASE 8: Output Generation
# ============================================================================

def generate_publishing_recommendations(product_data):
    """Suggest which sites should cover this product and with what categories."""
    category = product_data.get("category", "")
    recommendations = {}

    for site, cats in SITE_CATEGORIES.items():
        if category in cats:
            recommendations[site] = {
                "category_ids": cats[category],
                "recommended": True,
            }

    return recommendations


def format_source_document(product_data, ingredient_research, safety_data, keywords, reputation, competitive, compliance):
    """Format all research into a human-readable source document."""
    name = product_data.get("product_name", "Unknown")
    url = product_data.get("official_url", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = []
    lines.append(f"# {name} — Source Intelligence Report")
    lines.append(f"Generated: {now} | Source: {url}")
    lines.append(f"Tool: research_product.py v1.0")
    lines.append("")

    # Section 1: Product Overview
    lines.append("## 1. PRODUCT OVERVIEW")
    lines.append(f"- Product Name: {name}")
    lines.append(f"- Brand: {product_data.get('brand_name', 'Unknown')}")
    lines.append(f"- Type: {product_data.get('product_type', 'supplement')}")
    lines.append(f"- Category: {product_data.get('category', 'Unknown')}")
    lines.append(f"- Official URL: {url}")
    if product_data.get("vsl_url"):
        lines.append(f"- VSL URL: {product_data['vsl_url']}")
    lines.append("")

    # Section 2: Supplement Facts
    lines.append("## 2. SUPPLEMENT FACTS / KEY FEATURES")
    sf = product_data.get("supplement_facts", {})
    if sf.get("serving_size"):
        lines.append(f"- Serving Size: {sf['serving_size']}")
    if sf.get("servings_per_container"):
        lines.append(f"- Servings Per Container: {sf['servings_per_container']}")
    if sf.get("proprietary_blend"):
        lines.append(f"- **PROPRIETARY BLEND** (Total: {sf.get('proprietary_blend_total', 'Not disclosed')})")
    lines.append("")

    ingredients = sf.get("ingredients", [])
    if ingredients:
        lines.append("| Ingredient | Amount | Daily Value | Form |")
        lines.append("|-----------|--------|------------|------|")
        for ing in ingredients:
            lines.append(f"| {ing.get('name', '')} | {ing.get('amount', '')} | {ing.get('daily_value', '')} | {ing.get('form', '')} |")
        lines.append("")

    if sf.get("other_ingredients"):
        lines.append(f"Other Ingredients: {', '.join(sf['other_ingredients'])}")
        lines.append("")
    if sf.get("allergen_warnings"):
        lines.append(f"Allergen Warnings: {', '.join(sf['allergen_warnings'])}")
        lines.append("")

    # Data source attribution
    sf_source = sf.get("_source", "page_extraction")
    if sf_source == "dsld_verified":
        lines.append("**Data Source:** NIH Dietary Supplement Label Database (government-verified)")
    elif sf_source == "auto_label_ocr":
        lines.append("**Data Source:** Label image OCR extraction")
    elif sf_source == "manual_label_ocr":
        lines.append("**Data Source:** Manual label image upload + OCR extraction")
    lines.append("")

    # DSLD cross-reference (when we have ingredients from another source but DSLD also matched)
    dsld_xref = product_data.get("dsld_cross_reference")
    if dsld_xref and dsld_xref.get("ingredients"):
        lines.append(f"### NIH DSLD Cross-Reference (Label ID: {dsld_xref.get('dsld_id', 'N/A')})")
        lines.append(f"Product: {dsld_xref.get('dsld_product_name', '')} by {dsld_xref.get('dsld_brand', '')}")
        lines.append("")
        lines.append("| DSLD Ingredient | Amount | Category |")
        lines.append("|----------------|--------|----------|")
        for ding in dsld_xref["ingredients"]:
            lines.append(f"| {ding.get('name', '')} | {ding.get('amount', '')} | {ding.get('category', '')} |")
        lines.append("")
        lines.append("*Use DSLD data to verify extracted ingredient accuracy. Discrepancies may indicate reformulation.*")
        lines.append("")

    if product_data.get("dsld_id"):
        lines.append(f"DSLD Label ID: {product_data['dsld_id']} — https://dsld.od.nih.gov/label/{product_data['dsld_id']}")
        lines.append("")

    # Section 3: Ingredient Research
    lines.append("## 3. INGREDIENT RESEARCH (PubMed-Verified)")
    if ingredient_research:
        for ing_name, data in ingredient_research.items():
            lines.append(f"\n### {ing_name}")
            lines.append(f"- Product Dose: {data.get('product_dose', 'N/A')}")
            lines.append(f"- Clinical Dose Range: {data.get('clinical_dose_range', 'Not determined')}")
            dose = data.get('product_dose', '')
            clin = data.get('clinical_dose_range', '')
            if dose and clin:
                lines.append(f"- **Dose Assessment:** Product provides {dose}; clinical literature typically uses {clin}")
            lines.append(f"- Evidence Grade: **{data.get('evidence_grade', 'Insufficient')}**")

            studies = data.get("studies", [])
            if studies:
                lines.append(f"- Studies Found: {len(studies)}")
                for s in studies[:5]:
                    tier_badge = f"[{s.get('quality_tier', 'standard').upper()}]"
                    lines.append(f"  - {tier_badge} PMID:{s.get('pmid', '')} — {s.get('title', '')} ({s.get('journal', '')}, {s.get('year', '')})")
            else:
                lines.append("- No PubMed studies found for this ingredient")
    else:
        lines.append("No ingredient research available (no ingredients extracted)")
    lines.append("")

    # Section 4: Safety & Interactions
    lines.append("## 4. SAFETY & INTERACTIONS")
    if safety_data:
        for ing_name, data in safety_data.items():
            if data.get("side_effects") or data.get("drug_interactions") or data.get("contraindications"):
                lines.append(f"\n### {ing_name}")
                if data.get("side_effects"):
                    lines.append(f"Side Effects: {', '.join(data['side_effects'])}")
                if data.get("drug_interactions"):
                    for di in data["drug_interactions"]:
                        sev = di.get("severity", "Unknown")
                        lines.append(f"- **{sev}** interaction with {di.get('drug_class', '')}: {di.get('interaction', '')}")
                if data.get("contraindications"):
                    lines.append(f"Contraindications: {', '.join(data['contraindications'])}")
    else:
        lines.append("No safety data collected")
    lines.append("")

    # Section 5: Pricing & Policies
    lines.append("## 5. PRICING & POLICIES")
    pricing = product_data.get("pricing", [])
    if pricing:
        lines.append("| Package | Price | Per Unit | Shipping |")
        lines.append("|---------|-------|----------|----------|")
        for p in pricing:
            lines.append(f"| {p.get('package', '')} | {p.get('price', '')} | {p.get('per_unit', '')} | {p.get('shipping', '')} |")
        lines.append("")

    rp = product_data.get("refund_policy", {})
    if rp and rp.get("duration_days"):
        lines.append(f"**Refund Policy:** {rp.get('duration_days', '')}-day money-back guarantee")
        if rp.get("conditions"):
            lines.append(f"- Conditions: {rp['conditions']}")
        if rp.get("return_shipping"):
            lines.append(f"- Return Shipping: {rp['return_shipping']}")
        if rp.get("contact_method"):
            lines.append(f"- Contact: {rp['contact_method']}")
        if rp.get("verbatim"):
            lines.append(f"- Verbatim: \"{rp['verbatim']}\"")
        lines.append("")

    sp = product_data.get("shipping_policy", {})
    if sp and any(sp.values()):
        lines.append("**Shipping:**")
        if sp.get("domestic"):
            lines.append(f"- Domestic: {sp['domestic']}")
        if sp.get("international"):
            lines.append(f"- International: {sp['international']}")
        if sp.get("delivery_time"):
            lines.append(f"- Delivery: {sp['delivery_time']}")
        lines.append("")

    if product_data.get("payment_processor"):
        lines.append(f"Payment Processor: {product_data['payment_processor']}")
        lines.append("")

    # Section 6: Company Information
    lines.append("## 6. COMPANY INFORMATION")
    co = product_data.get("company", {})
    if co:
        for field in ["name", "address", "email", "phone", "website"]:
            if co.get(field):
                lines.append(f"- {field.title()}: {co[field]}")
    else:
        lines.append("Company information not found on product page")
    lines.append("")

    # Section 7: Keyword Intelligence
    lines.append("## 7. KEYWORD INTELLIGENCE")
    if keywords:
        for ktype, kwords in keywords.items():
            if kwords and ktype != "search_queries_to_run":
                lines.append(f"\n**{ktype.replace('_', ' ').title()}:**")
                for kw in kwords:
                    lines.append(f"- {kw}")
    lines.append("")

    # Section 8: Third-Party Reputation
    lines.append("## 8. THIRD-PARTY REPUTATION")
    if reputation:
        queries = reputation.get("search_queries_to_run", [])
        if queries:
            lines.append("**Search queries to verify:**")
            for q in queries:
                lines.append(f"- {q}")
        lines.append("")
        lines.append(f"BBB Rating: {reputation.get('bbb_rating', 'Not checked')}")
        lines.append(f"Trustpilot: {reputation.get('trustpilot_rating', 'Not checked')}")
        lines.append(f"Reddit Sentiment: {reputation.get('reddit_sentiment', 'Not checked')}")
        lines.append(f"FDA Warnings: {reputation.get('fda_warnings', 'Not checked')}")
        lines.append(f"Lawsuits: {reputation.get('lawsuits', 'Not checked')}")

        # FDA CAERS data (if queried)
        caers = reputation.get("fda_caers", {})
        if caers.get("total_reports", 0) > 0:
            lines.append("")
            lines.append(f"### FDA Adverse Event Reports (CAERS)")
            lines.append(f"- Total Reports: {caers['total_reports']}")
            lines.append(f"- Reports Analyzed: {caers.get('reports_analyzed', 'N/A')}")
            lines.append(f"- Query: \"{caers.get('query_matched', '')}\"")
            top_reactions = caers.get("top_reactions", [])
            if top_reactions:
                lines.append("- Top Reported Reactions:")
                for r in top_reactions[:10]:
                    lines.append(f"  - {r['reaction']}: {r['count']} reports")
            outcomes = caers.get("outcomes", [])
            if outcomes:
                lines.append("- Outcome Types:")
                for o in outcomes:
                    lines.append(f"  - {o['outcome']}: {o['count']}")
            lines.append("")
            lines.append("*CAERS reports are unverified consumer/healthcare provider submissions.*")
            lines.append("*They do not establish causation and cannot estimate incidence rates.*")
            lines.append("*Use for signal detection and editorial context only.*")
        elif caers:
            lines.append("")
            lines.append("### FDA Adverse Event Reports (CAERS)")
            lines.append("- No adverse event reports found in FDA CAERS database")
    lines.append("")

    # Section 9: Competitive Landscape
    lines.append("## 9. COMPETITIVE LANDSCAPE")
    if competitive:
        queries = competitive.get("search_queries_to_run", [])
        if queries:
            lines.append("**Search queries to run:**")
            for q in queries:
                lines.append(f"- {q}")
        competitors = competitive.get("competitors", [])
        if competitors:
            for c in competitors:
                lines.append(f"\n**{c.get('name', '')}**")
                lines.append(f"- Price: {c.get('price', '')}")
                lines.append(f"- Key Ingredients: {c.get('key_ingredients', '')}")
                lines.append(f"- Differentiator: {c.get('differentiator', '')}")
    lines.append("")

    # Section 10: Compliance Pre-Check
    lines.append("## 10. COMPLIANCE PRE-CHECK")
    if compliance:
        lines.append(f"- YMYL Category: {compliance.get('ymyl_category', '')}")
        lines.append(f"- Risk Level: **{compliance.get('risk_level', '')}**")
        lines.append(f"- FDA Disclaimer Required: {'Yes' if compliance.get('fda_disclaimer_required') else 'No'}")
        lines.append(f"- FTC Affiliate Disclosure Required: {'Yes' if compliance.get('ftc_affiliate_disclosure_required') else 'No'}")

        aw = compliance.get("accesswire_blocklist_check", {})
        lines.append(f"- AccessWire: {'PASS' if aw.get('passes') else 'FAIL — ' + ', '.join(aw.get('flagged_terms', []))}")

        bc = compliance.get("barchart_compliance", {})
        bc_status = "PASS" if bc.get("passes") else "REVIEW"
        lines.append(f"- Barchart: {bc_status} — {bc.get('notes', '')}")

        audit = compliance.get("claim_audit", [])
        if audit:
            lines.append(f"\n**Flagged Claims ({len(audit)}):**")
            for item in audit:
                lines.append(f"\n- CLAIM: \"{item.get('claim', '')}\"")
                for issue in item.get("issues", []):
                    lines.append(f"  Issue: {issue}")
                lines.append(f"  Safe Alternative: \"{item.get('safe_alternative', '')}\"")

        disclaimers = compliance.get("required_disclaimers", [])
        if disclaimers:
            lines.append("\n**Required Disclaimers:**")
            for d in disclaimers:
                lines.append(f"- {d}")
    lines.append("")

    # Section 11: Marketing Claims (Verbatim)
    lines.append("## 11. MARKETING CLAIMS (VERBATIM — UNVERIFIED)")
    claims = product_data.get("claims", [])
    if claims:
        for c in claims:
            if isinstance(c, dict):
                lines.append(f"- [{c.get('source', 'unknown')}] \"{c.get('claim', '')}\" (Verified: {c.get('verified', False)})")
            else:
                lines.append(f"- \"{c}\" (Verified: False)")
    else:
        lines.append("No marketing claims captured")
    lines.append("")

    # Section 12: Publishing Recommendations
    lines.append("## 12. PUBLISHING RECOMMENDATIONS")
    recs = generate_publishing_recommendations(product_data)
    if recs:
        for site, info in recs.items():
            lines.append(f"- **{site}**: Category IDs {info['category_ids']}")
    else:
        lines.append("No site-specific recommendations (category not mapped)")
    lines.append("")

    # Section 13: Testimonials (for reference)
    testimonials = product_data.get("testimonials", [])
    if testimonials:
        lines.append("## 13. TESTIMONIALS (Reference Only — Do Not Republish as Verified)")
        for t in testimonials:
            if isinstance(t, dict) and t.get("text"):
                lines.append(f"- {t.get('name', 'Anonymous')} ({t.get('location', '')}): \"{t['text'][:200]}...\"")
        lines.append("")

    return "\n".join(lines)


def phase8_output(product_data, ingredient_research, safety_data, keywords, reputation, competitive, compliance):
    """Generate all output files."""
    print_phase(8, "OUTPUT GENERATION")

    name = product_data.get("product_name", "Unknown")
    slug = slugify(name)

    # Build complete source document
    full_data = {
        "meta": {
            "tool": "research_product.py",
            "version": "1.0",
            "generated_at": datetime.now().isoformat(),
        },
        "product": product_data,
        "ingredient_research": ingredient_research,
        "safety": safety_data,
        "keywords": keywords,
        "reputation": reputation,
        "competitive": competitive,
        "compliance": compliance,
        "publishing_recommendations": generate_publishing_recommendations(product_data),
    }

    # Output 1: JSON file
    json_path = os.path.join(OUTPUT_DIR, f"{slug}_source.json")
    with open(json_path, "w") as f:
        json.dump(full_data, f, indent=2, default=str)
    _emit(f"  JSON: {json_path}")

    # Output 2: Human-readable document
    doc_text = format_source_document(product_data, ingredient_research, safety_data, keywords, reputation, competitive, compliance)
    doc_path = os.path.join(OUTPUT_DIR, f"{slug}_source_report.md")
    with open(doc_path, "w") as f:
        f.write(doc_text)
    _emit(f"  Report: {doc_path}")

    # Print summary stats
    ing_count = len(product_data.get("supplement_facts", {}).get("ingredients", []))
    study_count = sum(len(r.get("studies", [])) for r in ingredient_research.values())
    safety_count = sum(1 for s in safety_data.values() if s.get("side_effects") or s.get("drug_interactions"))
    claim_flags = compliance.get("flagged_claims_count", 0) if compliance else 0

    _emit(f"\n{'='*60}")
    _emit(f"  SOURCE INTELLIGENCE REPORT COMPLETE")
    _emit(f"{'='*60}")
    _emit(f"  Product: {name}")
    _emit(f"  Ingredients: {ing_count}")
    _emit(f"  PubMed Studies: {study_count}")
    _emit(f"  Safety Profiles: {safety_count}")
    _emit(f"  Flagged Claims: {claim_flags}")
    _emit(f"  Output: {json_path}")
    _emit(f"{'='*60}\n")

    return json_path, doc_path, doc_text, full_data


# ============================================================================
# MAIN ORCHESTRATOR
# ============================================================================

def research_product(url=None, vsl_url=None, product_name=None, quick=False, label_image=None, progress_callback=None):
    """Run all 8 research phases for a single product.

    Args:
        progress_callback: Optional callable(message, level) for progress updates.
                          When None, defaults to print() via _emit().
    """
    global _progress_callback
    _progress_callback = progress_callback
    start = time.time()

    _emit(f"\n{'#'*60}")
    _emit(f"  PRODUCT SOURCE INTELLIGENCE TOOL")
    _emit(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _emit(f"{'#'*60}")
    if url:
        _emit(f"  URL: {url}")
    if vsl_url:
        _emit(f"  VSL: {vsl_url}")
    if product_name:
        _emit(f"  Name: {product_name}")
    if label_image:
        _emit(f"  Label: {label_image}")
    if quick:
        _emit(f"  Mode: QUICK (skipping keywords, reputation, competitive)")

    # Initialize browser session for JS-rendered pages (optional)
    browser_session = None
    try:
        from browser_fetch import BrowserSession, PLAYWRIGHT_AVAILABLE
        if PLAYWRIGHT_AVAILABLE:
            browser_session = BrowserSession()
            browser_session.__enter__()
            if browser_session.available:
                _emit(f"  Browser rendering: ENABLED")
            else:
                _emit(f"  Browser rendering: FAILED TO LAUNCH (urllib only)")
                browser_session = None
    except ImportError:
        _emit(f"  Browser rendering: NOT AVAILABLE (pip install playwright)")

    # Phase 1: Extract product data
    try:
        product_data = phase1_extract_product(url, vsl_url, product_name,
                                               browser_session=browser_session)

        # If label image provided, use it to override/verify ingredients
        if label_image and product_data:
            label_result = extract_label_image(label_image)
            if label_result:
                if isinstance(label_result, dict):
                    ings = label_result["ingredients"]
                    _emit(f"  Label override: {len(ings)} verified ingredients from label image")
                    product_data["supplement_facts"]["ingredients"] = ings
                    if label_result.get("serving_size"):
                        product_data["supplement_facts"]["serving_size"] = label_result["serving_size"]
                    if label_result.get("servings_per_container"):
                        product_data["supplement_facts"]["servings_per_container"] = label_result["servings_per_container"]
                else:
                    _emit(f"  Label override: {len(label_result)} verified ingredients from label image")
                    product_data["supplement_facts"]["ingredients"] = label_result
                product_data["supplement_facts"]["_source"] = "label_image_verified"
            else:
                _emit("  [!] Label image provided but no ingredients extracted")
                _emit("      Make sure the image URL points to a clear Supplement Facts label photo")
        if not product_data:
            _emit("\n[ABORT] Could not extract product data")
            return None

        if vsl_url:
            product_data["vsl_url"] = vsl_url

        # Phase 2: PubMed research
        ingredient_research = phase2_pubmed_research(product_data)

        # Phase 3: Safety research
        safety_data = phase3_safety_research(product_data, ingredient_research)

        # Phase 4-6: Optional in quick mode
        if quick:
            keywords = {}
            reputation = {}
            competitive = {}
            _emit("\n  [QUICK MODE] Skipping Phases 4-6")
        else:
            keywords = phase4_keyword_research(product_data)
            reputation = phase5_reputation_check(product_data)
            competitive = phase6_competitive_landscape(product_data)

        # Phase 7: Compliance (always runs)
        compliance = phase7_compliance_check(product_data)

        # Phase 8: Output
        json_path, doc_path, doc_text, full_data = phase8_output(
            product_data, ingredient_research, safety_data,
            keywords, reputation, competitive, compliance
        )

        elapsed = time.time() - start
        _emit(f"  Total time: {elapsed:.1f} seconds")

        # Reset callback
        _progress_callback = None

        return json_path, doc_path, doc_text, full_data

    finally:
        # Always clean up browser session
        if browser_session:
            browser_session.__exit__(None, None, None)


def main():
    parser = argparse.ArgumentParser(description="Product Source Intelligence Tool")
    parser.add_argument("--url", help="Product URL to research")
    parser.add_argument("--vsl", help="VSL/video sales letter URL")
    parser.add_argument("--name", help="Product name (if no URL available)")
    parser.add_argument("--label", help="Path to supplement facts label image (JPG/PNG) — uses Claude vision to extract verified ingredients")
    parser.add_argument("--csv", help="CSV file with products to research (columns: product_name,source_url)")
    parser.add_argument("--quick", action="store_true", help="Skip keyword research, reputation, competitive analysis")
    parser.add_argument("--gdrive", action="store_true", help="Upload report to Google Drive")

    args = parser.parse_args()

    if not any([args.url, args.name, args.csv]):
        parser.print_help()
        _emit("\nError: Provide --url, --name, or --csv")
        sys.exit(1)

    # Ensure output directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if args.csv:
        # Batch mode
        import csv
        with open(args.csv, "r") as f:
            reader = csv.DictReader(f)
            products = list(reader)

        _emit(f"\nBatch mode: {len(products)} products to research")
        for i, row in enumerate(products):
            _emit(f"\n{'*'*60}")
            _emit(f"  Product {i+1}/{len(products)}")
            _emit(f"{'*'*60}")
            research_product(
                url=row.get("source_url", ""),
                product_name=row.get("product_name", ""),
                quick=args.quick,
            )
    else:
        # Single product
        result = research_product(
            url=args.url,
            vsl_url=args.vsl,
            product_name=args.name,
            quick=args.quick,
            label_image=args.label,
        )
        if result:
            json_path = result[0]
            _emit(f"\nDone. Source JSON: {json_path}")
            if args.gdrive:
                _emit("\nTo upload to Google Drive, use Claude Code's Google Drive MCP tools")
                _emit(f"  File to upload: {json_path}")


if __name__ == "__main__":
    main()
