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
                    img_data = base64.standard_b64encode(f.read()).decode("utf-8")
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

Return ONLY a valid JSON array with EVERY ingredient listed:
[
    {"name": "Ingredient Name", "amount": "500mg", "daily_value": "100%", "form": "as extract"}
]

Rules:
- Include EVERY ingredient visible on the label
- Capture exact amounts (mg, mcg, IU, etc.)
- Capture daily value percentages
- Capture the form if specified (e.g., "as Chromium Picolinate")
- Include proprietary blend ingredients even without individual amounts
- Capture "Other Ingredients" separately at the end with amount=""
- Be precise — this is a legal document"""

    response = call_claude(prompt, max_tokens=3000, images=[image_path])
    if not response:
        return []

    try:
        clean = re.sub(r'```json\s*', '', response)
        clean = re.sub(r'```\s*$', '', clean)
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

    # Also try the DuckDuckGo API (different from HTML search)
    try:
        ddg_api = f"https://api.duckduckgo.com/?q={urllib.parse.quote_plus(name + ' product')}&format=json&no_html=1"
        resp = fetch_url(ddg_api, max_bytes=30000)
        if resp:
            data = json.loads(resp)
            abstract = data.get("AbstractText", "")
            if abstract:
                combined.append(f"OVERVIEW: {abstract}")
            # Related topics
            for topic in data.get("RelatedTopics", [])[:5]:
                if isinstance(topic, dict) and topic.get("Text"):
                    combined.append(topic["Text"])
    except (json.JSONDecodeError, Exception):
        pass

    return "\n\n".join(combined) if combined else ""


def _extract_json_ld(html):
    """Extract product data from JSON-LD structured data embedded in HTML."""
    results = []
    pattern = r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>'
    for match in re.findall(pattern, html, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(match.strip())
            # Handle both single objects and arrays
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") in ("Product", "IndividualProduct", "Offer"):
                    results.append(item)
                elif item.get("@graph"):
                    for node in item["@graph"]:
                        if node.get("@type") in ("Product", "IndividualProduct"):
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


def _try_multiple_urls(url):
    """Try fetching a URL and common subpages to maximize content capture."""
    results = {}

    # Try main URL
    main = fetch_url(url)
    if main:
        results["main"] = main

        # Extract JSON-LD product data from main page
        json_ld = _extract_json_ld(main)
        if json_ld:
            ld_text = ""
            for product in json_ld:
                ld_text += f"\nPRODUCT (structured data): {product.get('name', '')}\n"
                ld_text += f"Description: {product.get('description', '')}\n"
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
        product_subpages = ["/ingredients", "/supplement-facts"]
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
                content = fetch_url(sub_url, max_bytes=30000)
            if content and len(content) > 500:
                results[sub] = content
                _emit(f"    Found subpage: {sub} ({len(content):,} bytes)")

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


def phase1_extract_product(url, vsl_url=None, product_name=None):
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
        all_pages = _try_multiple_urls(url)
        main_size = len(all_pages.get("main", ""))
        if main_size:
            _emit(f"  Main page: {main_size:,} bytes")
            _emit(f"  Total pages fetched: {len(all_pages)}")
        else:
            _emit(f"  Direct fetch failed — will try web search fallback")

    if vsl_url:
        _emit(f"  Fetching VSL: {vsl_url}")
        vsl_content = fetch_url(vsl_url)
        if vsl_content:
            _emit(f"  Got {len(vsl_content):,} bytes from VSL")

    # Layer 2: Extract supplement facts from raw HTML
    supplement_facts_raw = ""
    for page_key, page_html in all_pages.items():
        sf = _extract_supplement_facts_html(page_html)
        if sf:
            supplement_facts_raw += sf

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
        combined_text += f"\n\nVIDEO SALES LETTER PAGE:\n{strip_html(vsl_content)[:8000]}"

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

    # AccessWire blocklist check
    all_text = json.dumps(product_data).lower()
    flagged_terms = [term for term in ACCESSWIRE_BLOCKLIST if term in all_text]

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
        "required_disclaimers": disclaimers,
        "accesswire_blocklist_check": {
            "passes": len(flagged_terms) == 0,
            "flagged_terms": flagged_terms,
        },
        "barchart_compliance": {
            "passes": category != "male_enhancement",
            "notes": "Male enhancement products restricted on Barchart" if category == "male_enhancement" else "Category allowed",
        },
    }

    _emit(f"  Claims audited: {len(claims)}")
    _emit(f"  Flagged claims: {len(claim_audit)}")
    _emit(f"  AccessWire blocklist: {'PASS' if not flagged_terms else f'FAIL ({len(flagged_terms)} terms)'}")

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
        lines.append(f"- Barchart: {'PASS' if bc.get('passes') else 'FAIL'} — {bc.get('notes', '')}")

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

    # Phase 1: Extract product data
    product_data = phase1_extract_product(url, vsl_url, product_name)

    # If label image provided, use it to override/verify ingredients
    if label_image and product_data:
        label_ingredients = extract_label_image(label_image)
        if label_ingredients:
            _emit(f"  Label override: {len(label_ingredients)} verified ingredients from label image")
            product_data["supplement_facts"]["ingredients"] = label_ingredients
            product_data["supplement_facts"]["_source"] = "label_image_verified"
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
