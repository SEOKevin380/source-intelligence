"""
Source Intelligence Tool — Streamlit Web App
=============================================
Browser-based UI for the product research engine.
Deploy to Streamlit Community Cloud for team access.

Usage (local):
    streamlit run app.py
"""

import hashlib
import json
import os
import tempfile
import streamlit as st

# Must be first Streamlit call
st.set_page_config(
    page_title="Source Intelligence Tool",
    page_icon="🔬",
    layout="wide",
)

# ── Dark theme CSS overrides ──
st.markdown("""
<style>
    /* Tighten spacing */
    .block-container { padding-top: 2rem; }

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
    .stTextInput label, .stSelectbox label { font-weight: 500; }

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
</style>
""", unsafe_allow_html=True)


# ============================================================================
# AUTH GATE (with "Remember Me" via URL token)
# ============================================================================

def _make_auth_token(password):
    return hashlib.sha256(f"si-{password}-salt2026".encode()).hexdigest()[:16]

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

# Check URL token first — auto-login if valid
if not st.session_state.authenticated:
    app_pw = st.secrets.get("app_password", "sourceintel2026")
    url_token = st.query_params.get("t", "")
    if url_token and url_token == _make_auth_token(app_pw):
        st.session_state.authenticated = True

if not st.session_state.authenticated:
    st.title("Source Intelligence Tool")
    st.markdown("Enter the team password to continue.")
    password = st.text_input("Password", type="password", key="login_pw")
    remember = st.checkbox("Remember me on this browser", value=True)
    app_pw = st.secrets.get("app_password", "sourceintel2026")
    if password and password == app_pw:
        st.session_state.authenticated = True
        if remember:
            st.query_params["t"] = _make_auth_token(app_pw)
        st.rerun()
    elif password:
        st.error("Incorrect password.")
    st.stop()


# ============================================================================
# IMPORTS (after auth, so Streamlit Cloud doesn't fail on missing deps)
# ============================================================================

from research_product import research_product, extract_label_image
from config import OUTPUT_DIR, INGREDIENT_DB_PATH
from site_configs import SITE_CONFIGS, get_site_names
from prompt_builders import (
    build_l1_ingredient_prompt,
    build_l3_safety_prompt,
    build_l6_review_prompt,
    build_l6_press_release_prompt,
)


# ============================================================================
# SIDEBAR — INPUTS
# ============================================================================

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

product_url = st.sidebar.text_input("Product URL", placeholder="https://product-website.com/")
product_name = st.sidebar.text_input("Product Name", placeholder="Memovance PRO")
vsl_url = st.sidebar.text_input("VSL URL (optional)", placeholder="https://product.com/vsl-page")
label_url = st.sidebar.text_input(
    "Label Image URL (optional)",
    placeholder="https://example.com/supplement-facts-label.png",
    help="Direct URL to a supplement facts label image for OCR extraction",
    key="label_url",
)
label_file = None  # kept for API compatibility
rd_affiliate = st.sidebar.text_input(
    "Affiliate Link",
    placeholder="https://naturalsupplementreviews.com/product",
    key="rd_affiliate",
)
rd_previous = st.sidebar.text_input(
    "Previous Release(s)",
    value="FIRST RELEASE",
    help="URLs of your previous articles about this product (comma-separated)",
    key="rd_previous",
)
rd_competitor = st.sidebar.text_input(
    "Competitor Release(s)",
    placeholder="https://competitor.com/their-review",
    key="rd_competitor",
)
rd_platform = st.sidebar.selectbox(
    "Publishing Platform",
    ["Barchart Advertorial", "Accesswire", "Newswire.com", "Globe Newswire", "Domain Site"],
    key="rd_platform",
)
rd_client_title = st.sidebar.text_input(
    "Client Locked Title (optional)",
    placeholder="Leave blank unless client requires a specific title",
    key="rd_client_title",
)
rd_notes = st.sidebar.text_area(
    "Notes (optional)",
    placeholder="Verified contact info, special instructions, extra context...",
    height=100,
    key="rd_notes",
)

st.sidebar.markdown("---")
run_button = st.sidebar.button("Run Research", type="primary", use_container_width=True)

# Show previously researched products
st.sidebar.markdown("---")
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
                st.session_state.result_data = json.load(f)
            if os.path.exists(md_path):
                with open(md_path) as f:
                    st.session_state.result_report = f.read()
            st.session_state.result_json_path = json_path
            st.rerun()
else:
    st.sidebar.caption("No reports yet. Run your first research above.")


# ============================================================================
# MAIN AREA
# ============================================================================

st.title("Source Intelligence")

# Run research
if run_button:
    if not product_url and not product_name:
        st.error("Provide a Product URL or Product Name.")
        st.stop()

    # Handle label image — from URL or file upload
    label_path = None
    if label_url and label_url.strip():
        import requests
        try:
            resp = requests.get(label_url.strip(), timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200 and len(resp.content) > 5000:
                # Determine extension from Content-Type header first (handles S3/CDN
                # URLs with no file extension), then fall back to URL path
                ext = ".png"
                ct = resp.headers.get("Content-Type", "").lower()
                ct_map = {
                    "image/jpeg": ".jpg", "image/jpg": ".jpg",
                    "image/png": ".png", "image/webp": ".webp",
                    "image/gif": ".gif",
                }
                if ct in ct_map:
                    ext = ct_map[ct]
                else:
                    for e in [".jpg", ".jpeg", ".png", ".webp"]:
                        if e in label_url.lower():
                            ext = e
                            break
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                tmp.write(resp.content)
                tmp.close()
                label_path = tmp.name
                st.sidebar.success(f"Label image downloaded ({len(resp.content)//1024}KB)")
            else:
                st.sidebar.warning(f"Could not download label image (status {resp.status_code})")
        except Exception as e:
            st.sidebar.warning(f"Label download failed: {e}")
    elif label_file:
        suffix = "." + label_file.name.rsplit(".", 1)[-1]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(label_file.read())
        tmp.close()
        label_path = tmp.name

    # Progress tracking
    progress_container = st.status("Researching product...", expanded=True)
    progress_log = []

    def streamlit_callback(message, level="info"):
        """Route research progress to Streamlit UI."""
        msg = message.strip()
        if not msg:
            return
        progress_log.append(msg)
        if "PHASE" in msg:
            progress_container.update(label=msg.strip("= "))
        progress_container.write(msg)

    # Run the research engine
    try:
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
            progress_container.update(label="Research complete!", state="complete")
        else:
            progress_container.update(label="Research failed", state="error")
            st.error("Could not extract product data. Try providing the product name or a label image.")
    except Exception as e:
        progress_container.update(label="Error", state="error")
        st.error(f"Research failed: {e}")
    finally:
        if label_path and os.path.exists(label_path):
            os.unlink(label_path)


# ============================================================================
# DISPLAY RESULTS — Clean workflow: Inputs → Output → Details
# ============================================================================

if "result_data" in st.session_state:
    data = st.session_state.result_data
    product = data.get("product", {})
    name = product.get("product_name", "Unknown")
    compliance_data = data.get("compliance", {})

    # ── Product Header ──
    st.markdown(f"## {name}")

    # Quick stats bar
    sf = product.get("supplement_facts", {})
    ing_count = len(sf.get("ingredients", []))
    study_count = sum(len(r.get("studies", [])) for r in data.get("ingredient_research", {}).values())
    risk = compliance_data.get("risk_level", "Unknown")
    aw_pass = compliance_data.get("accesswire_blocklist_check", {}).get("passes", False)
    bc_pass = compliance_data.get("barchart_compliance", {}).get("passes", False)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Ingredients", ing_count)
    m2.metric("PubMed Studies", study_count)
    m3.metric("Risk Level", risk)
    m4.metric("AccessWire", "PASS" if aw_pass else "FAIL")
    m5.metric("Barchart", "PASS" if bc_pass else "FAIL")

    st.divider()

    # ====================================================================
    # SECTION 1: GENERATED PROMPT — The output
    # ====================================================================

    # Determine flow from sidebar platform selector
    is_domain = rd_platform == "Domain Site"

    # Site selector (only for domain site flow)
    site_config = None
    if is_domain:
        site_names = get_site_names()
        site_display = [s[1] for s in site_names]
        site_keys = [s[0] for s in site_names]
        selected_site_idx = st.selectbox(
            "Target Site",
            range(len(site_display)),
            format_func=lambda i: site_display[i],
            key="target_site",
        )
        site_config = SITE_CONFIGS.get(site_keys[selected_site_idx])

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
        st.caption(f"Platform: **{rd_platform}** — MBK production submission (paste into Claude project, system runs autonomously)")

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

    if layer_type.startswith("L1"):
        # Ingredient Profile
        ing_names = [ing.get("name", "") for ing in ingredients_list if ing.get("name")]
        if ing_names:
            selected_ing = st.selectbox("Select Ingredient", ing_names, key="l1_ingredient")
            prompt = build_l1_ingredient_prompt(selected_ing, data, safety, site_config)
        else:
            st.warning("No ingredients available. Research a product first.")

    elif layer_type.startswith("L3"):
        # Safety & Interactions Guide
        prompt = build_l3_safety_prompt(data, safety, site_config)

    elif is_domain:
        # L6: Domain Site Review
        prompt = build_l6_review_prompt(data, site_config, intake_fields)

    else:
        # L6: MBK Production (press release platforms)
        prompt = build_l6_press_release_prompt(data, intake_fields)

    # Display the output
    if prompt:
        st.markdown("**Copy the prompt below and paste it into your Claude project chat:**")
        st.code(prompt, language="text", wrap_lines=True)

        slug = name.lower().replace(" ", "-")
        layer_tag = layer_type.split(":")[0].strip().lower()

        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                "Download Prompt (.txt)",
                data=prompt,
                file_name=f"{slug}_{layer_tag}_prompt.txt",
                mime="text/plain",
                use_container_width=True,
            )
        with col2:
            st.download_button(
                "Download Full Report (.md)",
                data=st.session_state.get("result_report", ""),
                file_name=f"{slug}_source_report.md",
                mime="text/markdown",
                use_container_width=True,
            )

    st.divider()

    # ====================================================================
    # SECTION 3: RESEARCH DETAILS — Hidden by default
    # ====================================================================

    with st.expander("View Research Details", expanded=False):
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
                        st.caption("Use DSLD data to verify extracted ingredient accuracy. Discrepancies may indicate reformulation.")

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
                c1, c2, c3 = st.columns(3)
                with c1:
                    color = {"High": "🔴", "Very High": "🔴", "Moderate": "🟡", "Low": "🟢"}.get(risk, "⚫")
                    st.markdown(f"**Risk Level:** {color} {risk}")
                with c2:
                    st.markdown(f"**AccessWire:** {'✅ PASS' if aw_pass else '❌ FAIL'}")
                with c3:
                    st.markdown(f"**Barchart:** {'✅ PASS' if bc_pass else '❌ FAIL'}")

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
