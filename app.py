"""
Source Intelligence Tool — Product Intelligence CRM
====================================================
Browser-based UI for the product research engine + CRM.
Persistent product database, cross-site coverage tracking,
SERP strategy, and publication management.

Usage (local):
    streamlit run app.py
"""

import json
import os
import secrets
import subprocess
import tempfile
import time
import streamlit as st

# Auto-install Playwright browsers on first run (needed for Streamlit Cloud)
_pw_marker = os.path.join(tempfile.gettempdir(), ".playwright_installed")
if not os.path.exists(_pw_marker):
    try:
        subprocess.run(["python3", "-m", "playwright", "install", "chromium"],
                       capture_output=True, timeout=120)
        open(_pw_marker, "w").close()
    except Exception:
        pass

# Must be first Streamlit call
st.set_page_config(
    page_title="Source Intelligence Tool",
    page_icon="🔬",
    layout="wide",
)

# ── Dark theme CSS overrides ──
st.markdown("""
<style>
    /* Fix top padding — prevents buttons being cut off */
    .block-container { padding-top: 1.5rem; }

    /* Code blocks — dark with good contrast */
    .stCodeBlock { border: 1px solid #2d3748; border-radius: 8px; }

    /* Metric cards */
    [data-testid="stMetric"] {
        background: #1a1d27;
        border: 1px solid #2d3748;
        border-radius: 8px;
        padding: 12px 16px;
    }

    /* Expander styling */
    .streamlit-expanderHeader {
        background: #1a1d27;
        border-radius: 8px;
    }

    /* Input label styling */
    .stTextInput label, .stSelectbox label, .stTextArea label { font-weight: 500; }

    /* Download buttons */
    .stDownloadButton button {
        border: 1px solid #3b82f6;
        color: #3b82f6;
    }

    /* Section dividers */
    hr { border-color: #2d3748; }

    /* Tab styling */
    .stTabs [data-baseweb="tab-list"] { gap: 4px; }
    .stTabs [data-baseweb="tab"] {
        background: #1a1d27;
        border-radius: 6px 6px 0 0;
        padding: 8px 16px;
    }

    /* Form section headers */
    .form-section-header {
        font-size: 0.9rem;
        font-weight: 600;
        color: #a0aec0;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 0.5rem;
    }

    /* Coverage heat map colors */
    .coverage-published { color: #48bb78; }
    .coverage-recommended { color: #ecc94b; }
    .coverage-irrelevant { color: #718096; }

    /* Quality score badge */
    .quality-badge {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 0.8rem;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# ============================================================================
# AUTH GATE (with "Remember Me" via URL token)
# ============================================================================

def _get_secret(key):
    """Get a secret from Streamlit secrets or environment variables."""
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    # Also check legacy key name for backward compatibility
    if key == "SI_APP_PASSWORD":
        try:
            if hasattr(st, "secrets") and "app_password" in st.secrets:
                return st.secrets["app_password"]
        except Exception:
            pass
    return os.environ.get(key, "")

_TOKEN_TTL_SECONDS = 7 * 24 * 3600  # 7 days

def _make_auth_token():
    """Generate a cryptographically random token with an embedded timestamp."""
    ts = int(time.time())
    rand = secrets.token_hex(16)
    return f"{ts}-{rand}"

def _validate_auth_token(token):
    """Validate a remember-me token stored in session_state (not URL)."""
    if not token or "-" not in token:
        return False
    try:
        ts_str = token.split("-", 1)[0]
        ts = int(ts_str)
        if time.time() - ts > _TOKEN_TTL_SECONDS:
            return False
        return True
    except (ValueError, IndexError):
        return False

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

# Check session token (server-side, not URL-based)
if not st.session_state.authenticated:
    stored_token = st.session_state.get("_auth_token", "")
    if stored_token and _validate_auth_token(stored_token):
        st.session_state.authenticated = True

if not st.session_state.authenticated:
    st.title("Source Intelligence Tool")
    app_pw = _get_secret("SI_APP_PASSWORD")
    if not app_pw:
        st.error("No app password configured. Set `SI_APP_PASSWORD` in Streamlit secrets or environment.")
        st.stop()
    st.markdown("Enter the team password to continue.")
    password = st.text_input("Password", type="password", key="login_pw")
    remember = st.checkbox("Remember me on this browser", value=True)
    if password and app_pw and password == app_pw:
        st.session_state.authenticated = True
        if remember:
            st.session_state["_auth_token"] = _make_auth_token()
        st.rerun()
    elif password:
        st.error("Incorrect password.")
    st.stop()


def _build_form_values_from_result(data):
    """Build form_values dict from a loaded result's data for the results phase."""
    product = data.get("product", {})
    return {
        "product_url": product.get("official_url", ""),
        "product_name": product.get("product_name", "Unknown"),
        "rd_affiliate": "",
        "rd_platform": "Barchart Advertorial",
        "rd_previous": "FIRST RELEASE",
        "rd_competitor": "",
        "rd_client_title": "",
        "rd_notes": "",
    }


# ============================================================================
# IMPORTS (after auth, so Streamlit Cloud doesn't fail on missing deps)
# ============================================================================

from research_product import research_product, extract_label_image
from config import OUTPUT_DIR, INGREDIENT_DB_PATH, CATEGORY_DISPLAY_LABELS
try:
    from site_configs_loader import get_site_config as _get_site_cfg, get_site_names, get_l1_sites
    # Build SITE_CONFIGS dict for backward compat
    SITE_CONFIGS = {k: _get_site_cfg(k) for k, _ in get_site_names() if _get_site_cfg(k)}
except ImportError:
    from site_configs import SITE_CONFIGS, get_site_names
from prompt_builders import (
    build_l1_ingredient_prompt,
    build_l3_safety_prompt,
    build_l6_review_prompt,
    build_l6_press_release_prompt,
)

# CRM imports (graceful — don't break if DB not ready)
try:
    from database import ProductDatabase, _slugify
    from product_manager import (
        get_coverage_report, get_serp_strategy, get_previous_releases,
        get_prompt_completeness, get_stale_products, get_low_quality_products,
    )
    CRM_AVAILABLE = True
except Exception:
    CRM_AVAILABLE = False


# ============================================================================
# SESSION STATE INITIALIZATION
# ============================================================================

if "show_form" not in st.session_state:
    st.session_state.show_form = True
if "form_key" not in st.session_state:
    st.session_state.form_key = 0
if "selected_product_key" not in st.session_state:
    st.session_state.selected_product_key = None

has_results = "result_data" in st.session_state
show_form = st.session_state.show_form or not has_results


# ============================================================================
# DATABASE INITIALIZATION & IMPORT
# ============================================================================

def _get_db():
    """Get database instance (cached per session)."""
    if not CRM_AVAILABLE:
        return None
    try:
        return ProductDatabase()
    except Exception:
        return None


def _run_import_if_needed(db):
    """Auto-import existing data on first run (empty DB)."""
    if db is None:
        return
    stats = db.get_stats()
    if stats["total_products"] > 0:
        return  # Already has data

    imported_json = 0
    imported_master = 0

    # Import existing output/*.json files
    if os.path.isdir(OUTPUT_DIR):
        keys = db.import_all_json_files(OUTPUT_DIR)
        imported_json = len(keys)

    # Import master product list (cross-site publication data)
    from config import MASTER_PRODUCT_LIST_PATH
    if os.path.exists(MASTER_PRODUCT_LIST_PATH):
        result = db.import_from_master_list(MASTER_PRODUCT_LIST_PATH)
        imported_master = result.get("products_imported", 0)

    if imported_json or imported_master:
        st.toast(
            f"Imported {imported_json} researched products + "
            f"{imported_master} from master list",
            icon="✅",
        )


# ============================================================================
# SIDEBAR — Product Library + Navigation
# ============================================================================

db = _get_db()
if db:
    _run_import_if_needed(db)

st.sidebar.title("Source Intelligence")

# Browser rendering status
try:
    from browser_fetch import PLAYWRIGHT_AVAILABLE
    if PLAYWRIGHT_AVAILABLE:
        st.sidebar.caption("Browser rendering: Enabled")
    else:
        st.sidebar.caption("Browser rendering: Disabled (urllib only)")
except ImportError:
    st.sidebar.caption("Browser rendering: Not installed")

# CRM status
if CRM_AVAILABLE and db:
    stats = db.get_stats()
    st.sidebar.caption(
        f"CRM: {stats['researched_products']} researched / "
        f"{stats['total_products']} total products"
    )

st.sidebar.markdown("---")

# ── Navigation Buttons ──
nav_col1, nav_col2 = st.sidebar.columns(2)
with nav_col1:
    if st.button("+ New Research", use_container_width=True, type="primary"):
        for key in ["result_data", "result_report", "result_json_path",
                    "_label_page_text", "form_values", "update_mode",
                    "selected_product_key"]:
            st.session_state.pop(key, None)
        st.session_state.form_key += 1
        st.session_state.show_form = True
        st.rerun()
with nav_col2:
    if st.button("Import Data", use_container_width=True):
        if db:
            with st.sidebar.status("Importing...", expanded=True) as status:
                # Force re-import
                imported_json = 0
                imported_master = 0
                if os.path.isdir(OUTPUT_DIR):
                    keys = db.import_all_json_files(OUTPUT_DIR)
                    imported_json = len(keys)
                    status.write(f"Imported {imported_json} JSON files")
                from config import MASTER_PRODUCT_LIST_PATH
                if os.path.exists(MASTER_PRODUCT_LIST_PATH):
                    result = db.import_from_master_list(MASTER_PRODUCT_LIST_PATH)
                    imported_master = result.get("products_imported", 0)
                    pubs = result.get("publications_imported", 0)
                    status.write(f"Imported {imported_master} products, {pubs} publications")
                status.update(label="Import complete!", state="complete")
            st.rerun()

st.sidebar.markdown("---")

# ── Product Library ──
st.sidebar.markdown("**Product Library**")

if CRM_AVAILABLE and db:
    # Search box
    search_query = st.sidebar.text_input(
        "Search products",
        placeholder="Search by name, brand, category...",
        key="product_search",
        label_visibility="collapsed",
    )

    # Filter by category
    category_filter = st.sidebar.selectbox(
        "Filter by category",
        ["All Categories"] + sorted(set(
            p.get("category", "") for p in db.get_all_product_summaries()
            if p.get("category")
        )),
        key="category_filter",
        label_visibility="collapsed",
    )

    # Get filtered product list
    filter_cat = category_filter if category_filter != "All Categories" else None
    products = db.list_products(
        search=search_query or None,
        category=filter_cat,
    )

    # Display product list — grouped by brand
    if products:
        # Group products by brand
        from collections import OrderedDict
        brand_groups = OrderedDict()
        for p in products[:30]:
            brand = (p.get("brand") or "").strip()
            if brand not in brand_groups:
                brand_groups[brand] = []
            brand_groups[brand].append(p)

        # Render: brands with multiple products get a header
        for brand, brand_products in brand_groups.items():
            if brand and len(brand_products) > 1:
                st.sidebar.markdown(f"**{brand}** ({len(brand_products)} products)")

            for p in brand_products:
                pkey = p["product_key"]
                pname = p["product_name"]
                ptype = p.get("product_type", "")
                quality = p.get("quality_score", 0)
                pub_count = p.get("publication_count", 0)
                updated = (p.get("last_updated", "") or "")[:10]

                # Quality indicator
                if quality >= 80:
                    q_icon = "🟢"
                elif quality >= 60:
                    q_icon = "🟡"
                elif quality > 0:
                    q_icon = "🟠"
                else:
                    q_icon = "⚫"

                # Show short name if brand is displayed as group header
                if brand and len(brand_products) > 1:
                    display_name = pname.replace(brand, "").strip(" -–—:,")
                    if not display_name:
                        display_name = pname
                    label = f"  {q_icon} {display_name[:28]}"
                else:
                    label = f"{q_icon} {pname[:30]}"
                if pub_count:
                    label += f" ({pub_count})"

                if st.sidebar.button(
                    label, key=f"prod_{pkey}",
                    use_container_width=True,
                    help=f"Brand: {brand} | Type: {ptype} | Quality: {quality}/100 | Updated: {updated}",
                ):
                    # Load product from DB
                    product_rec = db.get_product(pkey)
                if product_rec and product_rec.get("research_data"):
                    st.session_state.result_data = product_rec["research_data"]
                    st.session_state.result_json_path = os.path.join(
                        OUTPUT_DIR, f"{pkey}_source.json"
                    )
                    st.session_state.form_values = _build_form_values_from_result(
                        product_rec["research_data"]
                    )
                    st.session_state.selected_product_key = pkey
                    st.session_state.show_form = False
                    st.rerun()
                elif product_rec:
                    st.sidebar.warning(f"No research data for {pname} — run research first")

        if len(products) > 30:
            st.sidebar.caption(f"Showing 30 of {len(products)} products")
    else:
        st.sidebar.caption("No products found.")
else:
    # Fallback: show existing JSON files (pre-CRM behavior)
    st.sidebar.markdown("**Previous Reports**")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    existing = sorted(
        [f for f in os.listdir(OUTPUT_DIR) if f.endswith("_source.json")],
        key=lambda f: os.path.getmtime(os.path.join(OUTPUT_DIR, f)),
        reverse=True,
    )
    if existing:
        for fname in existing[:10]:
            display_name = fname.replace("_source.json", "").replace("-", " ").title()
            if st.sidebar.button(f"📄 {display_name}", key=f"hist_{fname}", use_container_width=True):
                json_path = os.path.join(OUTPUT_DIR, fname)
                md_path = json_path.replace("_source.json", "_source_report.md")
                with open(json_path) as f:
                    loaded_data = json.load(f)
                st.session_state.result_data = loaded_data
                if os.path.exists(md_path):
                    with open(md_path) as f:
                        st.session_state.result_report = f.read()
                st.session_state.result_json_path = json_path
                st.session_state.form_values = _build_form_values_from_result(loaded_data)
                st.session_state.show_form = False
                st.rerun()
    else:
        st.sidebar.caption("No reports yet. Run your first research above.")


# ============================================================================
# MAIN AREA — Two-phase UI: FORM or RESULTS
# ============================================================================

if show_form:
    # ================================================================
    # PHASE 1: INPUT FORM
    # ================================================================

    is_update = st.session_state.get("update_mode", False)

    st.title("Source Intelligence")
    if is_update and "result_data" in st.session_state:
        existing = st.session_state.result_data
        existing_product = existing.get("product", {})
        st.caption(f"**Updating report for: {existing_product.get('product_name', 'Unknown')}** — Add new data below")
        st.info(
            f"Existing data: {len(existing_product.get('supplement_facts', {}).get('ingredients', []))} ingredients, "
            f"{len(existing_product.get('claims', []))} claims, "
            f"{sum(len(r.get('studies', [])) for r in existing.get('ingredient_research', {}).values())} studies. "
            "New data will be MERGED with existing research."
        )
    else:
        st.caption("Enter product details below to generate a research-backed prompt.")

    fk = st.session_state.form_key

    with st.form(f"research_form_{fk}", border=True):

        # ── Required Fields ──
        st.markdown('<p class="form-section-header">Required</p>', unsafe_allow_html=True)

        req_col1, req_col2 = st.columns(2)
        with req_col1:
            product_url = st.text_input(
                "Product URL",
                placeholder="https://product-website.com/",
                help="The main product sales page URL",
                key=f"product_url_{fk}",
            )
        with req_col2:
            product_name = st.text_input(
                "Product Name",
                placeholder="Memovance PRO",
                help="Exact product name as displayed on the site",
                key=f"product_name_{fk}",
            )

        req_col3, req_col4 = st.columns(2)
        with req_col3:
            rd_affiliate = st.text_input(
                "Affiliate Link",
                placeholder="https://doctortrusted.org/product",
                help="Your tracking/affiliate URL for this product",
                key=f"rd_affiliate_{fk}",
            )
        with req_col4:
            rd_platform = st.selectbox(
                "Publishing Platform",
                ["Barchart Advertorial", "Accesswire", "Newswire.com",
                 "Globe Newswire", "Domain Site"],
                key=f"rd_platform_{fk}",
            )

        # ── Optional Fields (collapsed by default) ──
        with st.expander("Optional Fields", expanded=False):
            opt_col1, opt_col2 = st.columns(2)
            with opt_col1:
                vsl_url = st.text_input(
                    "VSL URL",
                    placeholder="https://product.com/vsl-page",
                    help="Video sales letter page if separate from main product page",
                    key=f"vsl_url_{fk}",
                )
                label_url = st.text_input(
                    "Label Image URL",
                    placeholder="https://example.com/supplement-facts-label.png",
                    help="Direct URL to a supplement facts label image for OCR extraction",
                    key=f"label_url_{fk}",
                )
                rd_previous = st.text_input(
                    "Previous Release(s)",
                    value="FIRST RELEASE",
                    help="URLs of your previous articles about this product (comma-separated)",
                    key=f"rd_previous_{fk}",
                )
            with opt_col2:
                rd_competitor = st.text_input(
                    "Competitor Release(s)",
                    placeholder="https://competitor.com/their-review",
                    help="URLs of competitor articles about this product",
                    key=f"rd_competitor_{fk}",
                )
                rd_client_title = st.text_input(
                    "Client Locked Title",
                    placeholder="Leave blank unless client requires a specific title",
                    help="If the client mandates a specific headline",
                    key=f"rd_client_title_{fk}",
                )
                rd_notes = st.text_area(
                    "Notes",
                    placeholder="Verified contact info, special instructions, extra context...",
                    height=100,
                    help="Any additional context for the research engine",
                    key=f"rd_notes_{fk}",
                )

        # ── Update Mode: Additional Data Fields ──
        if is_update and "result_data" in st.session_state:
            with st.expander("Additional Data (for update)", expanded=True):
                st.markdown("Add new information to merge with existing research:")
                upd_col1, upd_col2 = st.columns(2)
                with upd_col1:
                    update_url = st.text_input(
                        "Additional URL to fetch",
                        placeholder="https://product.com/new-page",
                        help="New page to fetch and merge (e.g., updated sales page, new info page)",
                        key=f"update_url_{fk}",
                    )
                    update_label_url = st.text_input(
                        "New Label Image URL",
                        placeholder="https://example.com/updated-label.png",
                        help="Updated supplement facts label for re-OCR",
                        key=f"update_label_url_{fk}",
                    )
                with upd_col2:
                    update_notes = st.text_area(
                        "Additional Notes / Context",
                        placeholder="New info: price is $49.99/bottle, company is based in Austin TX, etc.",
                        height=120,
                        help="Free-text notes to merge into the report (pricing, contact info, corrections)",
                        key=f"update_notes_{fk}",
                    )

        # ── Submit Button ──
        submitted = st.form_submit_button(
            "Update Report" if is_update else "Run Research",
            type="primary",
            use_container_width=True,
        )

    # ── Handle form submission ──
    if submitted:
        # Input validation
        errors = []
        warnings = []

        if not product_url and not product_name:
            errors.append("Provide at least a **Product URL** or **Product Name**.")

        if product_url and not product_url.strip().startswith(("http://", "https://")):
            errors.append("**Product URL** must start with http:// or https://")

        if vsl_url and vsl_url.strip() and not vsl_url.strip().startswith(("http://", "https://")):
            errors.append("**VSL URL** must start with http:// or https://")

        if label_url and label_url.strip() and not label_url.strip().startswith(("http://", "https://")):
            errors.append("**Label Image URL** must start with http:// or https://")

        if not rd_affiliate or not rd_affiliate.strip():
            warnings.append("No **Affiliate Link** provided — prompt will use 'TRAFFIC-FIRST' as default.")

        if rd_previous and rd_previous.strip() == "FIRST RELEASE" and rd_competitor and rd_competitor.strip():
            warnings.append("You have competitor releases but this is marked as **FIRST RELEASE** — is that correct?")

        if errors:
            for err in errors:
                st.error(err)
            st.stop()

        for warn in warnings:
            st.warning(warn)

        # Store form values in session state for the results phase
        st.session_state.form_values = {
            "product_url": product_url,
            "product_name": product_name,
            "vsl_url": vsl_url,
            "label_url": label_url,
            "rd_affiliate": rd_affiliate,
            "rd_previous": rd_previous,
            "rd_competitor": rd_competitor,
            "rd_platform": rd_platform,
            "rd_client_title": rd_client_title,
            "rd_notes": rd_notes,
        }

        # Handle label image — from URL
        label_path = None
        if label_url and label_url.strip():
            from net import safe_fetch
            try:
                resp = safe_fetch(label_url.strip())
                if resp.error:
                    st.warning(f"Label URL fetch error: {resp.error}")
                elif resp.status_code == 200:
                    ct = resp.headers.get("Content-Type", "").lower()
                    ct_map = {
                        "image/jpeg": ".jpg", "image/jpg": ".jpg",
                        "image/png": ".png", "image/webp": ".webp",
                        "image/gif": ".gif",
                    }
                    if ct in ct_map:
                        ext = ct_map[ct]
                        if len(resp.content) > 1000:
                            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                            tmp.write(resp.content)
                            tmp.close()
                            label_path = tmp.name
                            st.success(f"Label image downloaded ({len(resp.content)//1024}KB)")
                        else:
                            st.warning("Label image too small (< 1KB) — may not be valid")
                    elif "text/html" in ct:
                        st.info("Label URL is a webpage — rendering with browser...")
                        try:
                            from playwright.sync_api import sync_playwright
                            from browser_fetch import PLAYWRIGHT_AVAILABLE
                            if PLAYWRIGHT_AVAILABLE:
                                pw = sync_playwright().start()
                                browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
                                ctx = browser.new_context(
                                    user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                                    viewport={'width': 1280, 'height': 800},
                                )
                                page = ctx.new_page()
                                page.goto(label_url.strip(), wait_until='networkidle', timeout=25000)
                                page.wait_for_timeout(3000)
                                page_text = page.inner_text('body')
                                has_supp_facts = any(kw in page_text.lower() for kw in
                                                      ['supplement facts', 'serving size', 'amount per serving'])

                                if has_supp_facts and len(page_text) > 100:
                                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
                                    page.screenshot(path=tmp.name, full_page=True)
                                    label_path = tmp.name
                                    st.success(f"Label page rendered — supplement facts found ({len(page_text)} chars)")
                                    st.session_state['_label_page_text'] = page_text
                                else:
                                    img_urls = page.evaluate("""() => {
                                        const imgs = document.querySelectorAll('img');
                                        return Array.from(imgs).map(img => ({
                                            src: img.src,
                                            alt: img.alt || '',
                                            width: img.naturalWidth || img.width,
                                            height: img.naturalHeight || img.height
                                        })).filter(i => i.src && (i.width > 200 || i.height > 200));
                                    }""")
                                    label_img = None
                                    for img in img_urls:
                                        itext = (img.get('alt', '') + ' ' + img.get('src', '')).lower()
                                        if any(kw in itext for kw in ['label', 'supplement', 'facts', 'ingredient']):
                                            label_img = img['src']
                                            break
                                    if not label_img and img_urls:
                                        img_urls.sort(key=lambda x: (x.get('width', 0) * x.get('height', 0)), reverse=True)
                                        label_img = img_urls[0]['src']

                                    if label_img:
                                        img_resp = safe_fetch(label_img)
                                        if img_resp.status_code == 200 and len(img_resp.content) > 1000:
                                            img_ct = img_resp.headers.get("Content-Type", "").lower()
                                            ext = ct_map.get(img_ct, ".png")
                                            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                                            tmp.write(img_resp.content)
                                            tmp.close()
                                            label_path = tmp.name
                                            st.success(f"Label image extracted from page ({len(img_resp.content)//1024}KB)")
                                        else:
                                            st.warning("Found image on page but couldn't download it")
                                    else:
                                        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
                                        page.screenshot(path=tmp.name, full_page=True)
                                        label_path = tmp.name
                                        st.success("Took screenshot of label page for OCR")
                                page.close()
                                ctx.close()
                                browser.close()
                                pw.stop()
                            else:
                                st.warning("Label URL is HTML (not an image). Install Playwright for browser rendering, or provide a direct image URL (.png/.jpg)")
                        except Exception as e:
                            st.warning(f"Browser rendering of label page failed: {e}")
                    else:
                        if len(resp.content) > 1000:
                            ext = ".png"
                            for e in [".jpg", ".jpeg", ".png", ".webp"]:
                                if e in label_url.lower():
                                    ext = e
                                    break
                            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                            tmp.write(resp.content)
                            tmp.close()
                            label_path = tmp.name
                            st.success(f"Label downloaded ({len(resp.content)//1024}KB, type: {ct})")
                        else:
                            st.warning(f"Could not download label image (content-type: {ct}, size: {len(resp.content)} bytes)")
                else:
                    st.warning(f"Could not download label image (HTTP {resp.status_code})")
            except Exception as e:
                st.warning(f"Label download failed: {e}")

        # Progress tracking
        progress_bar = st.progress(0, text="Starting research...")
        progress_container = st.status("Researching product...", expanded=True)
        progress_log = []

        phase_progress = {
            "PHASE 1": 0.10,
            "PHASE 2": 0.25,
            "PHASE 3": 0.45,
            "PHASE 4": 0.55,
            "PHASE 5": 0.65,
            "PHASE 6": 0.75,
            "PHASE 7": 0.85,
            "PHASE 8": 0.95,
        }

        def streamlit_callback(message, level="info"):
            """Route research progress to Streamlit UI."""
            msg = message.strip()
            if not msg:
                return
            progress_log.append(msg)
            if "PHASE" in msg:
                progress_container.update(label=msg.strip("= "))
                for phase_key, pct in phase_progress.items():
                    if phase_key in msg:
                        progress_bar.progress(pct, text=msg.strip("= "))
                        break
            progress_container.write(msg)

        # Run the research engine (update mode vs new research)
        try:
            if is_update and "result_data" in st.session_state:
                # UPDATE MODE — merge new data into existing report
                from research_product import update_research
                _upd_url = st.session_state.get(f"update_url_{fk}", "") or (product_url if product_url else None)
                _upd_label = st.session_state.get(f"update_label_url_{fk}", "") or None
                _upd_notes = st.session_state.get(f"update_notes_{fk}", "") or None

                result = update_research(
                    existing_data=st.session_state.result_data,
                    new_url=_upd_url or None,
                    new_label_url=_upd_label or None,
                    new_notes=_upd_notes or None,
                    progress_callback=streamlit_callback,
                )
            else:
                # NEW RESEARCH — full pipeline
                result = research_product(
                    url=product_url or None,
                    vsl_url=vsl_url or None,
                    product_name=product_name or None,
                    quick=False,
                    label_image=label_path,
                    progress_callback=streamlit_callback,
                )

            if result:
                json_path, doc_path, doc_text, full_data = result
                st.session_state.result_data = full_data
                st.session_state.result_report = doc_text
                st.session_state.result_json_path = json_path
                st.session_state.show_form = False
                st.session_state.pop("update_mode", None)
                # Set product key for CRM
                pname = full_data.get("product", {}).get("product_name", "")
                if pname and CRM_AVAILABLE:
                    st.session_state.selected_product_key = _slugify(pname)
                progress_container.update(label="Research complete!", state="complete")
                progress_bar.progress(1.0, text="Research complete!")
                st.rerun()
            else:
                progress_container.update(label="Research failed", state="error")
                st.error("Could not extract product data. Try providing the product name or a label image.")
        except Exception as e:
            progress_container.update(label="Error", state="error")
            st.error(f"Research failed: {e}")
        finally:
            if label_path and os.path.exists(label_path):
                os.unlink(label_path)


else:
    # ================================================================
    # PHASE 2: RESULTS DISPLAY — 4-Tab CRM Interface
    # ================================================================

    data = st.session_state.result_data
    product = data.get("product", {})
    name = product.get("product_name", "Unknown")
    compliance_data = data.get("compliance", {})
    sf = product.get("supplement_facts", {})
    product_key = st.session_state.get("selected_product_key") or _slugify(name) if CRM_AVAILABLE else ""

    # Read form values from session state
    fv = st.session_state.get("form_values", {})
    rd_platform = fv.get("rd_platform", "Barchart Advertorial")
    rd_affiliate = fv.get("rd_affiliate", "")
    rd_previous = fv.get("rd_previous", "FIRST RELEASE")
    rd_competitor = fv.get("rd_competitor", "")
    rd_client_title = fv.get("rd_client_title", "")
    rd_notes = fv.get("rd_notes", "")

    # ── Product Header ──
    st.markdown("")  # Spacer so header isn't clipped by Streamlit chrome
    header_col1, header_col2 = st.columns([6, 2])
    with header_col1:
        st.title(name)
    with header_col2:
        st.markdown("")  # Align button vertically with title
        if st.button("Update Report", use_container_width=True):
            st.session_state.update_mode = True
            st.session_state.show_form = True
            st.rerun()

    # Quick stats bar
    ing_count = len(sf.get("ingredients", []))
    study_count = sum(len(r.get("studies", [])) for r in data.get("ingredient_research", {}).values())
    risk = compliance_data.get("risk_level", "Unknown")
    aw_pass = compliance_data.get("accesswire_blocklist_check", {}).get("passes", False)
    bc_pass = compliance_data.get("barchart_compliance", {}).get("passes", False)
    gc_pass = compliance_data.get("globe_compliance", {}).get("passes", False)

    # Platform-aware compliance metrics
    is_globe_platform = "globe" in rd_platform.lower()
    is_barchart_platform = "barchart" in rd_platform.lower()

    stat_cols = st.columns(6)
    stat_cols[0].metric("Ingredients", ing_count)
    stat_cols[1].metric("PubMed Studies", study_count)
    stat_cols[2].metric("Risk Level", risk)

    if is_globe_platform:
        stat_cols[3].metric("Globe", "PASS" if gc_pass else "FAIL")
        stat_cols[4].metric("R12 (ACW)", "PASS" if aw_pass else "FAIL")
    elif is_barchart_platform:
        stat_cols[3].metric("R12 (ACW)", "PASS" if aw_pass else "FAIL")
        stat_cols[4].metric("Barchart", "PASS" if bc_pass else "REVIEW")
    else:
        stat_cols[3].metric("AccessWire", "PASS" if aw_pass else "FAIL")
        stat_cols[4].metric("Barchart", "PASS" if bc_pass else "REVIEW")

    # Quality score from CRM
    if CRM_AVAILABLE and db and product_key:
        product_rec = db.get_product(product_key)
        quality_score = product_rec.get("quality_score", 0) if product_rec else 0
        stat_cols[5].metric("Quality", f"{quality_score}/100")

    st.divider()

    # ================================================================
    # QUICK EXPORT — Always visible, zero clicks
    # ================================================================
    # Build a default prompt so the user can grab it immediately
    _quick_intake = {
        "platform": rd_platform,
        "affiliate_link": rd_affiliate or "TRAFFIC-FIRST",
        "previous_releases": rd_previous or "FIRST RELEASE",
        "release_type": "Single Product",
        "ymyl_category": "Yes" if compliance_data.get("risk_level") in ["High", "Very High", "Moderate"] else "No",
        "competitor_release": rd_competitor or "",
        "editor_title": rd_client_title or "",
        "notes": rd_notes or "",
    }
    _quick_prompt = build_l6_press_release_prompt(data, _quick_intake)
    _quick_slug = name.lower().replace(" ", "-")
    _quick_json = json.dumps(data, indent=2, default=str)

    st.markdown("#### Copy Prompt for Claude")
    st.caption("Click the copy icon (top-right of the box) → paste into [claude.ai](https://claude.ai)")
    st.code(_quick_prompt, language="text", wrap_lines=True)
    with st.expander("Download files", expanded=False):
        dl_col1, dl_col2, dl_col3 = st.columns(3)
        with dl_col1:
            st.download_button(
                "Prompt (.txt)",
                data=_quick_prompt,
                file_name=f"{_quick_slug}_prompt.txt",
                mime="text/plain",
                use_container_width=True,
            )
        with dl_col2:
            st.download_button(
                "Source Data (.json)",
                data=_quick_json,
                file_name=f"{_quick_slug}_source_data.json",
                mime="application/json",
                use_container_width=True,
            )
        with dl_col3:
            report_md = st.session_state.get("result_report", "")
            st.download_button(
                "Report (.md)",
                data=report_md if report_md else "No report available",
                file_name=f"{_quick_slug}_report.md",
                mime="text/markdown",
                use_container_width=True,
            )

    st.divider()

    # ================================================================
    # 4-TAB INTERFACE
    # ================================================================

    tab_dashboard, tab_generate, tab_research, tab_history = st.tabs([
        "Dashboard", "Generate", "Research Details", "History",
    ])

    # ────────────────────────────────────────────────────
    # TAB 1: DASHBOARD — Coverage & SERP Strategy
    # ────────────────────────────────────────────────────
    with tab_dashboard:
        dash_col1, dash_col2 = st.columns(2)

        with dash_col1:
            st.markdown("#### Product Details")
            st.markdown(f"- **Brand:** {product.get('brand_name', 'N/A')}")
            st.markdown(f"- **Type:** {product.get('product_type', 'N/A')}")
            st.markdown(f"- **Category:** {CATEGORY_DISPLAY_LABELS.get(product.get('category', ''), product.get('category', 'N/A'))}")
            if product.get("official_url"):
                st.markdown(f"- **URL:** {product['official_url']}")

            pricing = product.get("pricing", [])
            if pricing:
                st.markdown("**Pricing**")
                st.table(pricing)

            rp = product.get("refund_policy", {})
            if rp and rp.get("duration_days"):
                st.markdown(f"**Refund:** {rp['duration_days']}-day money-back guarantee")

        with dash_col2:
            # ── Quality Checks & Balances ──
            if CRM_AVAILABLE and product_key:
                st.markdown("#### Quality Checks")
                completeness = get_prompt_completeness(product_key, db)
                score = completeness.get("score", 0)

                if score >= 80:
                    st.success(f"Prompt Completeness: {score}% — Ready for production")
                elif score >= 60:
                    st.warning(f"Prompt Completeness: {score}% — Minor gaps")
                elif score > 0:
                    st.error(f"Prompt Completeness: {score}% — Significant gaps")
                else:
                    st.info("No completeness data available")

                # Section status
                sections = completeness.get("sections", {})
                if sections:
                    for sec_key, sec_data in sections.items():
                        status = sec_data.get("status", "missing")
                        icon = {"complete": "✅", "partial": "⚠️", "missing": "❌"}.get(status, "⚫")
                        st.caption(f"{icon} {sec_key}: {sec_data.get('detail', '')}")

                # Missing critical
                missing = completeness.get("missing_critical", [])
                if missing:
                    for m in missing:
                        st.error(f"Critical: {m}")

                # Data freshness
                if db:
                    freshness = db.check_data_freshness(product_key)
                    if freshness["days_old"] >= 0:
                        if freshness["is_fresh"]:
                            st.caption(f"🟢 {freshness['message']}")
                        else:
                            st.warning(freshness["message"])

        st.divider()

        # ── Cross-Site Coverage Matrix ──
        if CRM_AVAILABLE and product_key:
            st.markdown("#### Cross-Site Coverage")
            coverage = get_coverage_report(product_key, db)

            published = coverage.get("published_sites", [])
            recommended = coverage.get("recommended_sites", [])
            not_relevant = coverage.get("not_relevant_sites", [])
            coverage_pct = coverage.get("coverage_pct", 0)
            total_relevant = coverage.get("total_relevant", 0)

            if total_relevant > 0:
                st.progress(coverage_pct / 100, text=f"{len(published)} of {total_relevant} relevant sites ({coverage_pct}%)")

            # Coverage grid
            cov_cols = st.columns(5)
            all_items = (
                [(p["site_key"], "published", p.get("slug", "")) for p in published] +
                [(r["site_key"], "recommended", "") for r in recommended] +
                [(n["site_key"], "not_relevant", "") for n in not_relevant]
            )

            for i, (site_key, status, slug) in enumerate(all_items):
                col = cov_cols[i % 5]
                if status == "published":
                    col.markdown(f"🟢 **{site_key}**")
                    if slug:
                        col.caption(f"/{slug}/")
                elif status == "recommended":
                    col.markdown(f"🟡 {site_key}")
                    col.caption("Not yet published")
                else:
                    col.markdown(f"⚫ ~~{site_key}~~")

            st.divider()

            # ── SERP Strategy Panel ──
            st.markdown("#### SERP Stacking Strategy")
            serp = get_serp_strategy(product_key, db)

            serp_col1, serp_col2 = st.columns(2)

            with serp_col1:
                st.markdown("**Used Angles**")
                used = serp.get("used_angles", [])
                if used:
                    for a in used:
                        st.markdown(f"- **{a['angle']}** on {a['site_key']}")
                        if a.get("slug"):
                            st.caption(f"  /{a['slug']}/")
                else:
                    st.caption("No publications yet — all angles available")

            with serp_col2:
                st.markdown("**Available Angles**")
                available = serp.get("available_angles", [])
                previews = serp.get("slug_previews", {})
                if available:
                    for a in available:
                        slug_preview = previews.get(a["site_key"], "")
                        st.markdown(f"- **{a['angle']}** → {a['site_key']}")
                        if slug_preview:
                            st.caption(f"  Suggested: /{slug_preview}/")
                else:
                    st.caption("Full coverage achieved")

            # Strategy notes
            notes = serp.get("strategy_notes", [])
            if notes:
                for note in notes:
                    if "WARNING" in note:
                        st.warning(note)
                    else:
                        st.info(note)

    # ────────────────────────────────────────────────────
    # TAB 2: GENERATE — Prompt Generation + Record Publication
    # ────────────────────────────────────────────────────
    with tab_generate:
        # Determine flow from platform
        is_domain = rd_platform == "Domain Site"

        # Site selector (only for domain site flow)
        site_config = None
        if is_domain:
            site_names = get_site_names()
            site_display = [s[1] for s in site_names]
            site_keys_list = [s[0] for s in site_names]
            selected_site_idx = st.selectbox(
                "Target Site",
                range(len(site_display)),
                format_func=lambda i: site_display[i],
                key="target_site",
            )
            site_config = SITE_CONFIGS.get(site_keys_list[selected_site_idx])

        # Store release details in data
        data["release_details"] = {
            "affiliate_link": rd_affiliate or "",
            "previous_releases": rd_previous or "FIRST RELEASE",
            "competitor_releases": rd_competitor or "",
            "publishing_platform": rd_platform,
            "client_locked_title": rd_client_title or "",
        }

        st.markdown("### Generated Prompt")

        # Content layer selector (simplified)
        if is_domain:
            layer_type = st.selectbox("Content Type", [
                "L6: Product Review",
                "L1: Ingredient Profile",
                "L3: Safety & Interactions Guide",
            ], key="layer_type")
        else:
            layer_type = "L6: MBK Production"
            if is_globe_platform:
                st.caption(f"Platform: **{rd_platform}** — Globe v1.12 Format C submission")
            elif is_barchart_platform:
                st.caption(f"Platform: **{rd_platform}** — Barchart v2.4 submission")
            else:
                st.caption(f"Platform: **{rd_platform}** — ACW/NW v2.16 submission")

        # Build the prompt based on selection
        prompt = ""
        ingredients_list = sf.get("ingredients", [])
        safety = data.get("safety", {})

        # Shared intake fields
        intake_fields = {
            "platform": rd_platform,
            "affiliate_link": rd_affiliate or "TRAFFIC-FIRST",
            "previous_releases": rd_previous or "FIRST RELEASE",
            "release_type": "Single Product",
            "ymyl_category": "Yes" if compliance_data.get("risk_level") in ["High", "Very High", "Moderate"] else "No",
            "competitor_release": rd_competitor or "",
            "editor_title": rd_client_title or "",
            "notes": rd_notes or "",
        }

        # --- Readiness gate: surface data gaps before prompt generation ---
        _gaps = []
        if not ingredients_list and product.get("product_type", "supplement") in ("supplement", "food", "topical", "cannabis"):
            _gaps.append("No ingredients extracted")
        if not data.get("ingredient_research"):
            _gaps.append("No PubMed research")
        if not product.get("pricing"):
            _gaps.append("No pricing data")
        _bc = compliance_data.get("barchart_compliance", {})
        if _bc.get("passes") is None:
            _gaps.append("Compliance not evaluated (requires manual review)")
        if _gaps:
            st.warning("**Data gaps detected — review before publishing:**\n- " + "\n- ".join(_gaps))

        if layer_type.startswith("L1"):
            ing_names = [ing.get("name", "") for ing in ingredients_list if ing.get("name")]
            if ing_names:
                selected_ing = st.selectbox("Select Ingredient", ing_names, key="l1_ingredient")
                prompt = build_l1_ingredient_prompt(selected_ing, data, safety, site_config)
            else:
                st.warning("No ingredients available. Research a product first.")

        elif layer_type.startswith("L3"):
            prompt = build_l3_safety_prompt(data, safety, site_config)

        elif is_domain:
            prompt = build_l6_review_prompt(data, site_config, intake_fields)

        else:
            # Pre-generation data sufficiency gate
            cat_conflict = compliance_data.get("category_conflict")
            has_ingredients = bool(ingredients_list)

            if cat_conflict and not has_ingredients:
                st.warning("**Limited Data Mode** — Category conflict detected with zero verified "
                           "ingredients. The brief will instruct the production system to write ONLY "
                           "from verified claims.")

            prompt = build_l6_press_release_prompt(data, intake_fields)

        # Display the output
        if prompt:
            slug = name.lower().replace(" ", "-")
            layer_tag = layer_type.split(":")[0].strip().lower()

            # ── Download buttons FIRST — always visible at top ──
            st.markdown("---")
            st.markdown("#### Get Your Prompt")
            st.caption("Download the file, then upload or paste it into your Claude chat.")
            dl_col1, dl_col2, dl_col3 = st.columns(3)
            with dl_col1:
                st.download_button(
                    "Download Prompt (.txt)",
                    data=prompt,
                    file_name=f"{slug}_{layer_tag}_prompt.txt",
                    mime="text/plain",
                    use_container_width=True,
                )
            with dl_col2:
                report_md = st.session_state.get("result_report", "")
                st.download_button(
                    "Download Report (.md)",
                    data=report_md if report_md else "No report available",
                    file_name=f"{slug}_source_report.md",
                    mime="text/markdown",
                    use_container_width=True,
                )
            with dl_col3:
                # Raw JSON source data — upload this to a Claude Project for persistent access
                source_json = json.dumps(data, indent=2, default=str)
                st.download_button(
                    "Download Source Data (.json)",
                    data=source_json,
                    file_name=f"{slug}_source_data.json",
                    mime="application/json",
                    use_container_width=True,
                )

            st.info(
                "**How to use with Claude:**\n"
                "1. Download the **Prompt (.txt)** file above\n"
                "2. Open a new Claude chat at [claude.ai](https://claude.ai)\n"
                "3. Click the paperclip icon and upload the .txt file\n"
                "4. Send it — Claude will generate your article\n\n"
                "**For persistent access:** Upload the **Source Data (.json)** to a "
                "[Claude Project](https://claude.ai) as a Project Knowledge file. "
                "Then any chat in that project can reference the research data."
            )

            # ── Prompt preview (collapsed by default to keep page clean) ──
            with st.expander("Preview Full Prompt", expanded=False):
                st.code(prompt, language="text", wrap_lines=True)

            # Log generation to CRM (dedup: only log once per unique prompt)
            if CRM_AVAILABLE and db and product_key:
                import hashlib as _hl
                _gen_key = _hl.md5(f"{product_key}:{layer_type}:{rd_platform}:{prompt[:200]}".encode()).hexdigest()
                if _gen_key not in st.session_state.get("_logged_generations", set()):
                    db.log_generation(
                        product_key, rd_platform,
                        content_type=layer_type,
                        target_site=site_config.get("site_key", "") if site_config else "",
                        prompt_text=prompt,
                    )
                    if "_logged_generations" not in st.session_state:
                        st.session_state["_logged_generations"] = set()
                    st.session_state["_logged_generations"].add(_gen_key)

        # ── Record Publication Form (collapsed) ──
        if CRM_AVAILABLE and db and product_key:
            st.divider()
            with st.expander("Record Publication (after publishing)", expanded=False):
                st.caption("After publishing, record it here to track coverage and SERP stacking.")
                with st.form("record_publication", border=True):
                    rec_col1, rec_col2 = st.columns(2)
                    with rec_col1:
                        rec_site = st.text_input(
                            "Site Key",
                            placeholder="pvmedcenter",
                            help="Site key from wp-sites.json (e.g., pvmedcenter, hollyherman)",
                        )
                        rec_slug = st.text_input(
                            "Post Slug",
                            placeholder="product-name-review",
                            help="The URL slug used for this publication",
                        )
                        rec_angle = st.text_input(
                            "Slug Angle",
                            placeholder="research_evidence",
                            help="The SERP angle (e.g., clinical_physician, consumer_investigative)",
                        )
                    with rec_col2:
                        rec_url = st.text_input(
                            "Post URL (optional)",
                            placeholder="https://site.com/post-slug/",
                        )
                        rec_content_type = st.selectbox(
                            "Content Type",
                            ["L6_review", "L1_ingredient", "L3_safety", "funnel_hub",
                             "funnel_comparison", "funnel_guide"],
                        )
                        rec_date = st.text_input(
                            "Published Date",
                            value=__import__("datetime").datetime.utcnow().strftime("%Y-%m-%d"),
                        )

                    rec_submitted = st.form_submit_button("Record Publication", use_container_width=True)

                if rec_submitted and rec_site and rec_slug:
                    warnings = db.check_publishing_compliance(product_key, rec_site, rec_slug)
                    has_errors = any("ERROR" in w for w in warnings)

                    for w in warnings:
                        if "ERROR" in w:
                            st.error(w)
                        elif "WARNING" in w:
                            st.warning(w)

                    if not has_errors:
                        db.add_publication(
                            product_key=product_key,
                            site_key=rec_site,
                            slug=rec_slug,
                            slug_angle=rec_angle,
                            post_url=rec_url,
                            content_type=rec_content_type,
                            platform=rd_platform,
                            published_date=rec_date,
                        )
                        st.success(f"Publication recorded: {rec_site} / {rec_slug}")
                        st.rerun()

    # ────────────────────────────────────────────────────
    # TAB 3: RESEARCH DETAILS
    # ────────────────────────────────────────────────────
    with tab_research:
        detail_tabs = st.tabs([
            "Overview", "Ingredients", "Research", "Safety",
            "Compliance", "Claims", "Images", "Ingredient KB", "Raw JSON",
        ])

        # --- Overview ---
        with detail_tabs[0]:
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Product Details**")
                st.markdown(f"- **Name:** {name}")
                st.markdown(f"- **Brand:** {product.get('brand_name', 'N/A')}")
                st.markdown(f"- **Type:** {product.get('product_type', 'N/A')}")
                st.markdown(f"- **Category:** {product.get('category', 'N/A')}")
                if product.get("official_url"):
                    st.markdown(f"- **URL:** {product['official_url']}")

            with c2:
                pricing = product.get("pricing", [])
                if pricing:
                    st.markdown("**Pricing**")
                    st.table(pricing)

                rp = product.get("refund_policy", {})
                if rp and rp.get("duration_days"):
                    st.markdown(f"**Refund:** {rp['duration_days']}-day money-back guarantee")
                    if rp.get("conditions"):
                        st.caption(rp["conditions"])

            recs = data.get("publishing_recommendations", {})
            if recs:
                st.markdown("**Publishing Recommendations**")
                for site, info in recs.items():
                    st.markdown(f"- **{site}**: Category IDs {info.get('category_ids', [])}")

        # --- Ingredients ---
        with detail_tabs[1]:
            ingredients = sf.get("ingredients", [])
            if ingredients:
                if sf.get("proprietary_blend"):
                    st.warning(f"Proprietary Blend (Total: {sf.get('proprietary_blend_total', 'Not disclosed')})")
                table_data = []
                for ing in ingredients:
                    studies = data.get("ingredient_research", {}).get(ing.get("name", ""), {}).get("studies", [])
                    table_data.append({
                        "Ingredient": ing.get("name", ""),
                        "Amount": ing.get("amount", ""),
                        "Daily Value": ing.get("daily_value", ""),
                        "Form": ing.get("form", ""),
                        "Studies": len(studies),
                    })
                st.table(table_data)
                if sf.get("allergen_warnings"):
                    st.warning(f"Allergen Warnings: {', '.join(sf['allergen_warnings'])}")

                # Data source badge
                src = sf.get("_source", "page_extraction")
                src_labels = {
                    "dsld_verified": "NIH DSLD (government-verified)",
                    "auto_label_ocr": "Label Image OCR (auto-detected)",
                    "manual_label_ocr": "Label Image OCR (manual upload)",
                }
                if src in src_labels:
                    st.success(f"Data Source: {src_labels[src]}")

                # DSLD cross-reference
                dsld_xref = data.get("product", {}).get("dsld_cross_reference")
                if dsld_xref and dsld_xref.get("ingredients"):
                    with st.expander(f"NIH DSLD Cross-Reference (Label ID: {dsld_xref.get('dsld_id', 'N/A')})", expanded=False):
                        st.caption(f"Product: {dsld_xref.get('dsld_product_name', '')} by {dsld_xref.get('dsld_brand', '')}")
                        dsld_table = []
                        for ding in dsld_xref["ingredients"]:
                            dsld_table.append({
                                "Ingredient": ding.get("name", ""),
                                "Amount": ding.get("amount", ""),
                                "Category": ding.get("category", ""),
                            })
                        st.table(dsld_table)
                        st.caption("Use DSLD data to verify extracted ingredient accuracy.")

                dsld_id = data.get("product", {}).get("dsld_id")
                if dsld_id:
                    st.markdown(f"[View in NIH DSLD](https://dsld.od.nih.gov/label/{dsld_id})")
            else:
                st.info("No ingredients extracted.")

        # --- Research ---
        with detail_tabs[2]:
            research = data.get("ingredient_research", {})
            if research:
                for ing_name, ing_data in research.items():
                    with st.expander(f"{ing_name} — {ing_data.get('evidence_grade', 'N/A')} ({len(ing_data.get('studies', []))} studies)", expanded=False):
                        c1, c2 = st.columns(2)
                        with c1:
                            st.markdown(f"**Product Dose:** {ing_data.get('product_dose', 'N/A')}")
                        with c2:
                            st.markdown(f"**Clinical Dose:** {ing_data.get('clinical_dose_range', 'Not determined')}")
                        for study in ing_data.get("studies", []):
                            tier = study.get("quality_tier", "standard").upper()
                            badge = {"GOLD": "🟡", "SILVER": "⚪", "BRONZE": "🟤"}.get(tier, "⚫")
                            st.markdown(
                                f"{badge} **[{tier}]** PMID:{study.get('pmid', '')} — "
                                f"{study.get('title', '')} (*{study.get('journal', '')}*, {study.get('year', '')})"
                            )
            else:
                st.info("No PubMed research available.")

        # --- Safety ---
        with detail_tabs[3]:
            safety_data = data.get("safety", {})
            if safety_data:
                for ing_name, sdata in safety_data.items():
                    has_data = sdata.get("side_effects") or sdata.get("drug_interactions") or sdata.get("contraindications")
                    if has_data:
                        with st.expander(f"{ing_name}", expanded=True):
                            if sdata.get("drug_interactions"):
                                for di in sdata["drug_interactions"]:
                                    severity = di.get("severity", "Unknown")
                                    icon = {"High": "🔴", "Moderate": "🟡", "Low": "🟢"}.get(severity, "⚫")
                                    st.markdown(f"{icon} **{severity}** — {di.get('drug_class', '')}: {di.get('interaction', '')}")
                            if sdata.get("side_effects"):
                                st.markdown("**Side Effects:** " + ", ".join(sdata["side_effects"]))
                            if sdata.get("contraindications"):
                                st.markdown("**Contraindications:** " + ", ".join(sdata["contraindications"]))
            else:
                st.info("No safety data collected.")

            # FDA CAERS adverse event data
            reputation_data = data.get("reputation", {})
            caers = reputation_data.get("fda_caers", {})
            if caers and caers.get("total_reports", 0) > 0:
                st.markdown("---")
                st.markdown(f"### FDA Adverse Event Reports (CAERS)")
                st.markdown(f"**{caers['total_reports']}** adverse event reports found for \"{caers.get('query_matched', '')}\"")
                top_reactions = caers.get("top_reactions", [])
                if top_reactions:
                    reaction_table = [{"Reaction": r["reaction"], "Reports": r["count"]} for r in top_reactions[:10]]
                    st.table(reaction_table)
                outcomes = caers.get("outcomes", [])
                if outcomes:
                    st.markdown("**Outcome Types:**")
                    for o in outcomes:
                        st.markdown(f"- {o['outcome']}: {o['count']}")
                st.caption("CAERS reports are unverified consumer/healthcare provider submissions. They do not establish causation.")

        # --- Compliance ---
        with detail_tabs[4]:
            if compliance_data:
                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    color = {"High": "🔴", "Very High": "🔴", "Moderate": "🟡", "Low": "🟢"}.get(risk, "⚫")
                    st.markdown(f"**Risk Level:** {color} {risk}")
                with c2:
                    st.markdown(f"**AccessWire:** {'✅ PASS' if aw_pass else '❌ FAIL'}")
                with c3:
                    st.markdown(f"**Barchart:** {'✅ PASS' if bc_pass else '⚠️ REVIEW'}")
                with c4:
                    st.markdown(f"**Globe:** {'✅ PASS' if gc_pass else '❌ FAIL'}")

                # Globe-specific compliance detail
                gc_data = compliance_data.get("globe_compliance", {})
                if not gc_data.get("passes") and gc_data.get("flagged_categories"):
                    st.markdown(f"### Globe Phrase Blocklist Flags")
                    st.warning(
                        "These terms/phrases are confirmed Globe rejection triggers."
                    )
                    for cat, terms in gc_data.get("flagged_categories", {}).items():
                        cat_label = cat.split("_", 1)[-1].replace("_", " ").title() if "_" in cat else cat
                        st.error(f"**Category {cat.split('_')[0]}** ({cat_label}): {', '.join(terms)}")

                bl_blocked = compliance_data.get("accesswire_blocklist_check", {}).get("blocked_claims", [])
                if bl_blocked:
                    st.markdown(f"### Blocklist-Blocked Claims ({len(bl_blocked)})")
                    for item in bl_blocked:
                        st.error(f"**BLOCKED:** \"{item.get('claim', '')}\"")
                        st.caption(f"Banned terms: *{', '.join(item.get('matched_terms', []))}*")

                cvd9 = compliance_data.get("cvd9_blocked_claims", [])
                if cvd9:
                    st.markdown(f"### CVD-9 Blocked Claims ({len(cvd9)})")
                    for item in cvd9:
                        st.error(f"**BLOCKED:** \"{item.get('claim', '')}\"")
                        st.caption(f"Trigger: *{item.get('verb', '')}* + *{item.get('disease', '')}*")

                audit = compliance_data.get("claim_audit", [])
                if audit:
                    st.markdown(f"### Flagged Claims ({len(audit)})")
                    for item in audit:
                        st.error(f"**Claim:** \"{item.get('claim', '')}\"")
                        for issue in item.get("issues", []):
                            st.caption(f"Issue: {issue}")
                        st.success(f"**Safe Alternative:** \"{item.get('safe_alternative', '')}\"")

                disclaimers = compliance_data.get("required_disclaimers", [])
                if disclaimers:
                    st.markdown("### Required Disclaimers")
                    for d in disclaimers:
                        st.markdown(f"- {d}")
            else:
                st.info("No compliance data.")

        # --- Claims ---
        with detail_tabs[5]:
            claims = product.get("claims", [])
            if claims:
                st.markdown(f"**{len(claims)} marketing claims captured** (verbatim, unverified)")
                for c in claims:
                    if isinstance(c, dict):
                        st.markdown(f"- [{c.get('source', 'unknown')}] \"{c.get('claim', '')}\"")
                    else:
                        st.markdown(f"- \"{c}\"")

                testimonials = product.get("testimonials", [])
                if testimonials:
                    st.markdown("---")
                    st.markdown(f"**{len(testimonials)} testimonials** (reference only)")
                    for t in testimonials:
                        if isinstance(t, dict) and t.get("text"):
                            st.caption(f"**{t.get('name', 'Anonymous')}** ({t.get('location', '')}): \"{t['text'][:300]}...\"")
            else:
                st.info("No marketing claims captured.")

        # --- Images ---
        with detail_tabs[6]:
            images = product.get("product_images", [])
            if images:
                st.markdown(f"**{len(images)} product images extracted**")
                cols = st.columns(3)
                for i, img in enumerate(images):
                    with cols[i % 3]:
                        local_path = img.get("local_path", "")
                        if local_path and os.path.exists(local_path):
                            st.image(local_path, caption=img.get("alt", f"Image {i+1}"), use_container_width=True)
                        else:
                            st.markdown(f"[Image {i+1}]({img.get('url', '')})")
                        st.caption(f"{img.get('width', '?')}x{img.get('height', '?')} | {img.get('size_bytes', 0)//1024}KB")
            else:
                st.info("No product images extracted.")

        # --- Ingredient KB ---
        with detail_tabs[7]:
            st.markdown("### Ingredient Knowledge Base")
            st.caption("Accumulated research across all products.")

            kb = {}
            if os.path.exists(INGREDIENT_DB_PATH):
                with open(INGREDIENT_DB_PATH) as f:
                    kb = json.load(f)

            if kb:
                total_studies = sum(len(entry.get("studies", [])) for entry in kb.values())
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.metric("Ingredients", len(kb))
                with c2:
                    st.metric("Total Studies", total_studies)
                with c3:
                    strong = sum(1 for e in kb.values() if e.get("evidence_grade") == "Strong")
                    st.metric("Strong Evidence", strong)

                search = st.text_input("Search ingredients", placeholder="e.g., magnesium", key="kb_search")

                for key in sorted(kb.keys()):
                    if search and search.lower() not in key.lower():
                        continue
                    entry = kb[key]
                    studies = entry.get("studies", [])
                    grade = entry.get("evidence_grade", "Unknown")
                    grade_icon = {"Strong": "🟢", "Moderate": "🟡", "Preliminary": "🟠", "Insufficient": "🔴"}.get(grade, "⚫")
                    updated = entry.get("last_updated", "unknown")

                    with st.expander(f"{grade_icon} **{key.title()}** — {grade} ({len(studies)} studies) — Updated: {updated}"):
                        if entry.get("clinical_dose_range"):
                            st.markdown(f"**Clinical Dose Range:** {entry['clinical_dose_range']}")
                        safety_items = []
                        if entry.get("side_effects"):
                            safety_items.append(f"Side effects: {', '.join(entry['side_effects'])}")
                        if entry.get("drug_interactions"):
                            for di in entry["drug_interactions"]:
                                if isinstance(di, dict):
                                    safety_items.append(f"[{di.get('severity', '?')}] {di.get('drug_class', '')}: {di.get('interaction', '')}")
                        if entry.get("contraindications"):
                            safety_items.append(f"Contraindications: {', '.join(entry['contraindications'])}")
                        if safety_items:
                            st.markdown("**Safety:**")
                            for item in safety_items:
                                st.caption(item)
                        if studies:
                            st.markdown("**Studies:**")
                            for s in studies:
                                tier = s.get("quality_tier", "standard").upper()
                                badge = {"GOLD": "🟡", "SILVER": "⚪", "BRONZE": "🟤"}.get(tier, "⚫")
                                st.markdown(
                                    f"{badge} **[{tier}]** PMID:{s.get('pmid', '')} — "
                                    f"{s.get('title', '')} (*{s.get('journal', '')}*, {s.get('year', '')})"
                                )
            else:
                st.info("No ingredients in the knowledge base yet. Run your first product research to start building it.")

        # --- Raw JSON ---
        with detail_tabs[8]:
            st.code(json.dumps(data, indent=2, default=str), language="json", wrap_lines=True)

    # ────────────────────────────────────────────────────
    # TAB 4: HISTORY — Generation Log & Publications
    # ────────────────────────────────────────────────────
    with tab_history:
        if CRM_AVAILABLE and db and product_key:
            hist_col1, hist_col2 = st.columns(2)

            with hist_col1:
                st.markdown("#### Publication Log")
                pubs = db.get_publications(product_key)
                if pubs:
                    for p in pubs:
                        st.markdown(
                            f"- **{p['site_key']}** — /{p.get('slug', '')}/ "
                            f"({p.get('published_date', 'N/A')})"
                        )
                        if p.get("slug_angle"):
                            st.caption(f"  Angle: {p['slug_angle']} | Type: {p.get('content_type', '')}")
                else:
                    st.caption("No publications recorded yet.")

            with hist_col2:
                st.markdown("#### Generation Log")
                gens = db.get_generation_history(product_key)
                if gens:
                    for g in gens[:20]:
                        ts = (g.get("generated_at", "") or "")[:19]
                        st.markdown(
                            f"- **{g.get('platform', 'N/A')}** — "
                            f"{g.get('content_type', '')} ({ts})"
                        )
                        if g.get("target_site"):
                            st.caption(f"  Target: {g['target_site']}")
                else:
                    st.caption("No generations logged yet.")

            # Product record info
            st.divider()
            product_rec = db.get_product(product_key)
            if product_rec:
                st.markdown("#### Product Record")
                info_cols = st.columns(4)
                info_cols[0].metric("Research Version", product_rec.get("research_version", 1))
                info_cols[1].metric("Quality Score", f"{product_rec.get('quality_score', 0)}/100")
                info_cols[2].metric("First Researched", (product_rec.get("first_researched", "") or "")[:10])
                info_cols[3].metric("Last Updated", (product_rec.get("last_updated", "") or "")[:10])

                # Quality flags
                flags = product_rec.get("quality_flags_list", [])
                if flags:
                    st.markdown("**Quality Flags:**")
                    for f in flags:
                        if "QUALITY:" in f:
                            st.info(f)
                        elif "WARNING:" in f:
                            st.warning(f)
                        elif "ERROR:" in f or "MISSING:" in f:
                            st.error(f)
                        else:
                            st.caption(f)
        else:
            st.info("CRM database not available. History tracking requires the database module.")
