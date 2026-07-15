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
st.sidebar.markdown("Research any product in under 3 minutes.")

product_url = st.sidebar.text_input("Product URL", placeholder="https://product-website.com/")
vsl_url = st.sidebar.text_input("VSL URL (optional)", placeholder="https://product.com/vsl-page")
product_name = st.sidebar.text_input("Product Name (if no URL)", placeholder="GlycoReset")
label_file = st.sidebar.file_uploader("Label Image (optional)", type=["jpg", "jpeg", "png"])

run_button = st.sidebar.button("Run Research", type="primary", use_container_width=True)

st.sidebar.caption("Every product runs the full 8-phase research pipeline. No shortcuts.")

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
        name = fname.replace("_source.json", "").replace("-", " ").title()
        if st.sidebar.button(f"📄 {name}", key=f"hist_{fname}", use_container_width=True):
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

st.title("Product Source Intelligence")

# Run research
if run_button:
    if not product_url and not product_name:
        st.error("Provide a Product URL or Product Name.")
        st.stop()

    # Handle label image upload
    label_path = None
    if label_file:
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
        # Update the status label with phase info
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
        # Clean up temp file
        if label_path and os.path.exists(label_path):
            os.unlink(label_path)


# ============================================================================
# DISPLAY RESULTS
# ============================================================================

if "result_data" in st.session_state:
    data = st.session_state.result_data
    product = data.get("product", {})
    name = product.get("product_name", "Unknown")

    st.markdown(f"## {name} — Source Intelligence Report")

    # Quick Copy bar — most-used action front and center
    json_str = json.dumps(data, indent=2, default=str)
    with st.expander("**Copy Source Data** — click to expand, then use the copy icon (top-right of code block)", expanded=False):
        copy_tab1, copy_tab2 = st.tabs(["Full JSON", "Markdown Report"])
        with copy_tab1:
            st.code(json_str, language="json", wrap_lines=True)
        with copy_tab2:
            report_md = st.session_state.get("result_report", "")
            if report_md:
                st.code(report_md, language="markdown", wrap_lines=True)
            else:
                st.info("No markdown report available.")

    # Download buttons (secondary)
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "Download JSON",
            data=json_str,
            file_name=f"{name.lower().replace(' ', '-')}_source.json",
            mime="application/json",
        )
    with col2:
        if "result_report" in st.session_state:
            st.download_button(
                "Download Report (MD)",
                data=st.session_state.result_report,
                file_name=f"{name.lower().replace(' ', '-')}_source_report.md",
                mime="text/markdown",
            )

    # Tabbed results
    tabs = st.tabs(["Overview", "Ingredients", "Research", "Safety", "Compliance", "Claims", "Images", "Ingredient KB", "Export Prompt", "Raw JSON"])

    # --- TAB: Overview ---
    with tabs[0]:
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
            st.markdown("**Quick Stats**")
            sf = product.get("supplement_facts", {})
            ing_count = len(sf.get("ingredients", []))
            study_count = sum(len(r.get("studies", [])) for r in data.get("ingredient_research", {}).values())
            st.metric("Ingredients", ing_count)
            st.metric("PubMed Studies", study_count)
            compliance = data.get("compliance", {})
            st.metric("Flagged Claims", compliance.get("flagged_claims_count", 0))

        # Pricing table
        pricing = product.get("pricing", [])
        if pricing:
            st.markdown("**Pricing**")
            st.table(pricing)

        # Refund policy
        rp = product.get("refund_policy", {})
        if rp and rp.get("duration_days"):
            st.markdown(f"**Refund Policy:** {rp['duration_days']}-day money-back guarantee")
            if rp.get("conditions"):
                st.caption(rp["conditions"])

        # Publishing recommendations
        recs = data.get("publishing_recommendations", {})
        if recs:
            st.markdown("**Publishing Recommendations**")
            for site, info in recs.items():
                st.markdown(f"- **{site}**: Category IDs {info.get('category_ids', [])}")

    # --- TAB: Ingredients ---
    with tabs[1]:
        sf = product.get("supplement_facts", {})
        ingredients = sf.get("ingredients", [])
        if ingredients:
            if sf.get("proprietary_blend"):
                st.warning(f"Proprietary Blend (Total: {sf.get('proprietary_blend_total', 'Not disclosed')})")

            # Build table data
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
        else:
            st.info("No ingredients extracted. Try providing a label image.")

    # --- TAB: Research ---
    with tabs[2]:
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
                        badge_color = {"GOLD": "🟡", "SILVER": "⚪", "BRONZE": "🟤"}.get(tier, "⚫")
                        st.markdown(
                            f"{badge_color} **[{tier}]** PMID:{study.get('pmid', '')} — "
                            f"{study.get('title', '')} (*{study.get('journal', '')}*, {study.get('year', '')})"
                        )
        else:
            st.info("No PubMed research available.")

    # --- TAB: Safety ---
    with tabs[3]:
        safety = data.get("safety", {})
        if safety:
            for ing_name, sdata in safety.items():
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

    # --- TAB: Compliance ---
    with tabs[4]:
        compliance = data.get("compliance", {})
        if compliance:
            c1, c2, c3 = st.columns(3)
            with c1:
                risk = compliance.get("risk_level", "Unknown")
                color = {"High": "🔴", "Very High": "🔴", "Moderate": "🟡", "Low": "🟢"}.get(risk, "⚫")
                st.markdown(f"**Risk Level:** {color} {risk}")
            with c2:
                aw = compliance.get("accesswire_blocklist_check", {})
                st.markdown(f"**AccessWire:** {'✅ PASS' if aw.get('passes') else '❌ FAIL'}")
            with c3:
                bc = compliance.get("barchart_compliance", {})
                st.markdown(f"**Barchart:** {'✅ PASS' if bc.get('passes') else '❌ FAIL'}")

            # Flagged claims
            audit = compliance.get("claim_audit", [])
            if audit:
                st.markdown(f"### Flagged Claims ({len(audit)})")
                for item in audit:
                    st.error(f"**Claim:** \"{item.get('claim', '')}\"")
                    for issue in item.get("issues", []):
                        st.caption(f"Issue: {issue}")
                    st.success(f"**Safe Alternative:** \"{item.get('safe_alternative', '')}\"")

            # Required disclaimers
            disclaimers = compliance.get("required_disclaimers", [])
            if disclaimers:
                st.markdown("### Required Disclaimers")
                for d in disclaimers:
                    st.markdown(f"- {d}")
        else:
            st.info("No compliance data.")

    # --- TAB: Claims ---
    with tabs[5]:
        claims = product.get("claims", [])
        if claims:
            st.markdown(f"**{len(claims)} marketing claims captured** (verbatim, unverified)")
            for c in claims:
                if isinstance(c, dict):
                    st.markdown(f"- [{c.get('source', 'unknown')}] \"{c.get('claim', '')}\"")
                else:
                    st.markdown(f"- \"{c}\"")

            # Testimonials
            testimonials = product.get("testimonials", [])
            if testimonials:
                st.markdown("---")
                st.markdown(f"**{len(testimonials)} testimonials** (reference only — do not republish as verified)")
                for t in testimonials:
                    if isinstance(t, dict) and t.get("text"):
                        st.caption(f"**{t.get('name', 'Anonymous')}** ({t.get('location', '')}): \"{t['text'][:300]}...\"")
        else:
            st.info("No marketing claims captured.")

    # --- TAB: Images ---
    with tabs[6]:
        images = product.get("product_images", [])
        if images:
            st.markdown(f"**{len(images)} product images extracted** (reference only — create original images from these)")
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

    # --- TAB: Ingredient KB ---
    with tabs[7]:
        st.markdown("### Ingredient Knowledge Base")
        st.markdown("Accumulated research across all products. Grows with every research run.")

        # Load ingredient KB
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

                    # Safety summary
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

                    # Studies
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

    # --- TAB: Export Prompt ---
    with tabs[8]:
        st.markdown("### Export to Claude Projects")
        st.markdown("Select a content layer, configure, and copy the prompt into Claude Projects.")

        st.divider()

        # Layer type selector
        layer_type = st.selectbox("Content Layer", [
            "L6: Product Review (Domain Site)",
            "L6: Product Review (Press Release)",
            "L1: Ingredient Profile",
            "L3: Safety & Interactions Guide",
        ], key="layer_type")

        # Collect structured data
        compliance = data.get("compliance", {})
        safety = data.get("safety", {})
        ingredients_list = product.get("supplement_facts", {}).get("ingredients", [])

        # Site selector (for L1, L3, and L6 domain)
        site_config = None
        if "Press Release" not in layer_type:
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

        prompt = ""

        # === L1: Ingredient Profile ===
        if layer_type.startswith("L1"):
            ing_names = [ing.get("name", "") for ing in ingredients_list if ing.get("name")]
            if ing_names:
                selected_ing = st.selectbox("Select Ingredient", ing_names, key="l1_ingredient")
                prompt = build_l1_ingredient_prompt(selected_ing, data, safety, site_config)
            else:
                st.warning("No ingredients available. Research a product first.")

        # === L3: Safety & Interactions Guide ===
        elif layer_type.startswith("L3"):
            prompt = build_l3_safety_prompt(data, safety, site_config)

        # === L6: Product Review (Domain Site) ===
        elif "Domain" in layer_type:
            col1, col2 = st.columns(2)
            with col1:
                affiliate_link = st.text_input(
                    "Affiliate Link",
                    placeholder="https://hop.clickbank.net/?affiliate=XXX&vendor=product",
                    help="Tracking link, or type TRAFFIC-FIRST if no affiliate link",
                    key="l6_aff_link",
                )
                ymyl_status = "Yes" if compliance.get("risk_level") in ["High", "Very High", "Moderate"] else "No"
                ymyl_category = st.selectbox("YMYL Category", ["Yes", "No"],
                                             index=0 if ymyl_status == "Yes" else 1, key="l6_ymyl")
            with col2:
                previous_releases = st.text_input("Previous Release(s)", value="FIRST RELEASE",
                                                  help="URLs of previous articles, or FIRST RELEASE", key="l6_prev")
                release_type = st.selectbox("Release Type", ["Single Product", "Multi-Product Brand Guide"], key="l6_type")

            with st.expander("Optional Fields"):
                opt1, opt2 = st.columns(2)
                with opt1:
                    editor_title = st.text_input("Editor-Locked Title", key="l6_title")
                    release_summary = st.text_input("Release Summary (140 chars)", key="l6_summary")
                with opt2:
                    subtitle = st.text_input("Subtitle", key="l6_subtitle")
                    release_tags = st.text_input("Release Tags (comma-separated)", key="l6_tags")
                competitor_release = st.text_input("Competitor Release (optional)", key="l6_comp")

            intake_fields = {
                "platform": "Domain Site",
                "affiliate_link": affiliate_link or "TRAFFIC-FIRST",
                "previous_releases": previous_releases,
                "release_type": release_type,
                "ymyl_category": ymyl_category,
                "editor_title": editor_title if 'editor_title' in dir() else "",
                "subtitle": subtitle if 'subtitle' in dir() else "",
                "release_summary": release_summary if 'release_summary' in dir() else "",
                "release_tags": release_tags if 'release_tags' in dir() else "",
                "competitor_release": competitor_release if 'competitor_release' in dir() else "",
            }
            prompt = build_l6_review_prompt(data, site_config, intake_fields)

        # === L6: Product Review (Press Release) ===
        elif "Press Release" in layer_type:
            col1, col2 = st.columns(2)
            with col1:
                pr_platform = st.selectbox("Platform", [
                    "Barchart Advertorial", "Accesswire", "Newswire.com", "Globe Newswire",
                ], key="pr_platform")
                affiliate_link = st.text_input("Affiliate Link",
                                               placeholder="https://hop.clickbank.net/...",
                                               key="pr_aff_link")
            with col2:
                previous_releases = st.text_input("Previous Release(s)", value="FIRST RELEASE", key="pr_prev")
                release_type = st.selectbox("Release Type", ["Single Product", "Multi-Product Brand Guide"], key="pr_type")

            ymyl_status = "Yes" if compliance.get("risk_level") in ["High", "Very High", "Moderate"] else "No"
            ymyl_category = st.selectbox("YMYL Category", ["Yes", "No"],
                                         index=0 if ymyl_status == "Yes" else 1, key="pr_ymyl")

            with st.expander("Optional Fields"):
                opt1, opt2 = st.columns(2)
                with opt1:
                    editor_title = st.text_input("Editor-Locked Title", key="pr_title")
                    release_summary = st.text_input("Release Summary (140 chars)", key="pr_summary")
                with opt2:
                    subtitle = st.text_input("Subtitle", key="pr_subtitle")
                    release_tags = st.text_input("Release Tags (comma-separated)", key="pr_tags")
                competitor_release = st.text_input("Competitor Release (optional)", key="pr_comp")

            intake_fields = {
                "platform": pr_platform,
                "affiliate_link": affiliate_link or "TRAFFIC-FIRST",
                "previous_releases": previous_releases,
                "release_type": release_type,
                "ymyl_category": ymyl_category,
                "editor_title": editor_title if 'editor_title' in dir() else "",
                "subtitle": subtitle if 'subtitle' in dir() else "",
                "release_summary": release_summary if 'release_summary' in dir() else "",
                "release_tags": release_tags if 'release_tags' in dir() else "",
                "competitor_release": competitor_release if 'competitor_release' in dir() else "",
            }
            prompt = build_l6_press_release_prompt(data, intake_fields)

        # Display the generated prompt
        if prompt:
            st.markdown("**Click the copy icon (top-right of box) to copy the full prompt:**")
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

    # --- TAB: Raw JSON ---
    with tabs[9]:
        st.markdown("**Click the copy icon (top-right) to copy the full JSON:**")
        st.code(json.dumps(data, indent=2, default=str), language="json", wrap_lines=True)
