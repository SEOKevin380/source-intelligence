"""
Source Intelligence Tool — Streamlit Web App
=============================================
Browser-based UI for the product research engine.
Deploy to Streamlit Community Cloud for team access.

Usage (local):
    streamlit run app.py
"""

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
# AUTH GATE
# ============================================================================

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("Source Intelligence Tool")
    st.markdown("Enter the team password to continue.")
    password = st.text_input("Password", type="password", key="login_pw")
    app_pw = st.secrets.get("app_password", "sourceintel2026")
    if password and password == app_pw:
        st.session_state.authenticated = True
        st.rerun()
    elif password:
        st.error("Incorrect password.")
    st.stop()


# ============================================================================
# IMPORTS (after auth, so Streamlit Cloud doesn't fail on missing deps)
# ============================================================================

from research_product import research_product, extract_label_image
from config import OUTPUT_DIR


# ============================================================================
# SIDEBAR — INPUTS
# ============================================================================

st.sidebar.title("Source Intelligence")
st.sidebar.markdown("Research any product in under 3 minutes.")

product_url = st.sidebar.text_input("Product URL", placeholder="https://product-website.com/")
vsl_url = st.sidebar.text_input("VSL URL (optional)", placeholder="https://product.com/vsl-page")
product_name = st.sidebar.text_input("Product Name (if no URL)", placeholder="GlycoReset")
label_file = st.sidebar.file_uploader("Label Image (optional)", type=["jpg", "jpeg", "png"])
quick_mode = st.sidebar.checkbox("Quick Mode", value=True, help="Skip keyword research, reputation, and competitive analysis")

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
            quick=quick_mode,
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

    # Download buttons
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "Download JSON",
            data=json.dumps(data, indent=2, default=str),
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
    tabs = st.tabs(["Overview", "Ingredients", "Research", "Safety", "Compliance", "Claims", "Raw JSON"])

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

    # --- TAB: Raw JSON ---
    with tabs[6]:
        st.json(data)
