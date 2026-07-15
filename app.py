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
    tabs = st.tabs(["Overview", "Ingredients", "Research", "Safety", "Compliance", "Claims", "Images", "Export Prompt", "Raw JSON"])

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

    # --- TAB: Export Prompt ---
    with tabs[7]:
        st.markdown("### Export to Claude Projects (v3.8 Intake)")
        st.markdown("Fill in the fields below. Copy the generated prompt — paste it directly into Claude Projects. Source intelligence data is embedded inline. No Google Drive needed.")

        st.divider()

        # Collect structured data for building the prompt
        sf = product.get("supplement_facts", {})
        ingredients = sf.get("ingredients", [])
        compliance = data.get("compliance", {})
        safety = data.get("safety", {})
        research = data.get("ingredient_research", {})
        pricing = product.get("pricing", [])
        claims = product.get("claims", [])
        rp = product.get("refund_policy", {})

        # ---- Intake Fields ----
        col1, col2 = st.columns(2)
        with col1:
            publishing_platform = st.selectbox("Publishing Platform", [
                "Barchart Advertorial",
                "Accesswire",
                "Newswire.com",
                "Globe Newswire",
                "Domain Site",
            ])
            affiliate_link = st.text_input(
                "Affiliate Link",
                placeholder="https://hop.clickbank.net/?affiliate=XXX&vendor=product",
                help="Tracking link, or type TRAFFIC-FIRST if no affiliate link"
            )
        with col2:
            previous_releases = st.text_input(
                "Previous Release(s)",
                value="FIRST RELEASE",
                help="URLs of previous articles about this product, or FIRST RELEASE"
            )
            release_type = st.selectbox("Release Type", [
                "Single Product",
                "Multi-Product Brand Guide",
            ])

        col3, col4 = st.columns(2)
        with col3:
            ymyl_status = "Yes" if compliance.get("risk_level") in ["High", "Very High", "Moderate"] else "No"
            ymyl_category = st.selectbox(
                "YMYL Category",
                ["Yes", "No"],
                index=0 if ymyl_status == "Yes" else 1,
            )
        with col4:
            competitor_release = st.text_input(
                "Competitor Release (optional)",
                placeholder="URL of competitor article to beat"
            )

        # Optional fields
        with st.expander("Optional Fields (leave blank to let the system generate)"):
            opt1, opt2 = st.columns(2)
            with opt1:
                editor_title = st.text_input("Editor-Locked Title", placeholder="Leave blank for archetype selection")
                release_summary = st.text_input("Release Summary (140 chars)", placeholder="Auto-generated if blank")
            with opt2:
                subtitle = st.text_input("Subtitle", placeholder="Auto-generated if blank")
                release_tags = st.text_input("Release Tags (3-5, comma-separated)", placeholder="Auto-generated if blank")

        # ---- Build the combined prompt ----
        # Section 1: v3.8 Intake Fields
        prompt = f"""PRODUCT NAME: {name}
OFFICIAL WEBSITE URL: {product.get('official_url', '')}
PUBLISHING PLATFORM: {publishing_platform}

AFFILIATE LINK: {affiliate_link or 'TRAFFIC-FIRST'}
RELEASE TYPE: {release_type}
YMYL CATEGORY: {ymyl_category}
PREVIOUS RELEASES: {previous_releases}
GOOGLE DRIVE PRODUCT DATA: none — source intelligence data provided inline below"""

        if competitor_release:
            prompt += f"\nCOMPETITOR RELEASE: {competitor_release}"

        if editor_title:
            prompt += f"\nEDITOR-LOCKED TITLE: {editor_title}"
        if subtitle:
            prompt += f"\nSUBTITLE: {subtitle}"
        if release_summary:
            prompt += f"\nRELEASE SUMMARY (140 chars): {release_summary}"
        if release_tags:
            prompt += f"\nRELEASE TAGS: {release_tags}"

        # Section 2: Inline Source Intelligence Data
        prompt += f"""

════════════════════════════════════════════════════════
SOURCE INTELLIGENCE DATA (Pre-Verified — Use for Phase 0)
════════════════════════════════════════════════════════

Product Name: {name}
Brand: {product.get('brand_name', '')}
Product Type: {product.get('product_type', 'supplement')}
Category: {product.get('category', '')}
Official URL: {product.get('official_url', '')}
Risk Level: {compliance.get('risk_level', 'Unknown')}
AccessWire: {'PASS' if compliance.get('accesswire_blocklist_check', {}).get('passes') else 'FAIL'}
Barchart: {'PASS' if compliance.get('barchart_compliance', {}).get('passes') else 'FAIL'}

--- SUPPLEMENT FACTS ---
"""
        if sf.get("proprietary_blend"):
            prompt += f"PROPRIETARY BLEND — Total: {sf.get('proprietary_blend_total', 'Not disclosed')}\n"

        for ing in ingredients:
            line = f"- {ing.get('name', '')}"
            if ing.get("amount"):
                line += f" — {ing['amount']}"
            if ing.get("daily_value"):
                line += f" ({ing['daily_value']} DV)"
            if ing.get("form"):
                line += f" [Form: {ing['form']}]"
            prompt += line + "\n"
        if not ingredients:
            prompt += "No ingredients extracted — invoke Thin Web Presence Protocol\n"

        # Ingredient research with PubMed citations
        prompt += "\n--- INGREDIENT RESEARCH (PubMed-Verified) ---\n"
        for ing_name, ing_data in research.items():
            studies = ing_data.get("studies", [])
            grade = ing_data.get("evidence_grade", "N/A")
            prompt += f"\n{ing_name} — Evidence: {grade} — {len(studies)} studies\n"
            if ing_data.get("product_dose"):
                prompt += f"  Product Dose: {ing_data['product_dose']}\n"
            if ing_data.get("clinical_dose_range"):
                prompt += f"  Clinical Dose Range: {ing_data['clinical_dose_range']}\n"
            for s in studies:
                tier = s.get("quality_tier", "standard").upper()
                prompt += f"  [{tier}] PMID:{s.get('pmid', '')} — {s.get('title', '')} ({s.get('journal', '')}, {s.get('year', '')})\n"
        if not research:
            prompt += "No PubMed research available\n"

        # Safety & interactions
        prompt += "\n--- DRUG INTERACTIONS & SAFETY ---\n"
        has_safety = False
        for ing_name, sdata in safety.items():
            interactions = sdata.get("drug_interactions", [])
            side_fx = sdata.get("side_effects", [])
            contras = sdata.get("contraindications", [])
            if interactions or side_fx or contras:
                has_safety = True
                prompt += f"\n{ing_name}:\n"
                for di in interactions:
                    prompt += f"  [{di.get('severity', 'Unknown')}] {di.get('drug_class', '')}: {di.get('interaction', '')}\n"
                if side_fx:
                    prompt += f"  Side Effects: {', '.join(side_fx)}\n"
                if contras:
                    prompt += f"  Contraindications: {', '.join(contras)}\n"
        if not has_safety:
            prompt += "No significant drug interactions identified\n"

        # Pricing
        prompt += "\n--- PRICING (Verified from live page) ---\n"
        for p in pricing:
            prompt += f"- {p.get('package', '')}: {p.get('price', '')} ({p.get('per_unit', '')}/unit) — Shipping: {p.get('shipping', 'N/A')}\n"
        if not pricing:
            prompt += "No pricing extracted — verify from live page\n"

        # Refund policy
        prompt += "\n--- REFUND POLICY ---\n"
        if rp.get("duration_days"):
            prompt += f"{rp['duration_days']}-day money-back guarantee\n"
            if rp.get("conditions"):
                prompt += f"Conditions: {rp['conditions']}\n"
            if rp.get("verbatim"):
                prompt += f"Verbatim: \"{rp['verbatim']}\"\n"
        else:
            prompt += "No refund policy extracted — verify from live page\n"

        # Shipping
        shipping = product.get("shipping", {})
        if shipping:
            prompt += "\n--- SHIPPING ---\n"
            for k, v in shipping.items():
                if v:
                    prompt += f"{k.title()}: {v}\n"

        # Company info
        prompt += "\n--- COMPANY / CONTACT ---\n"
        company = product.get("company", {})
        if company:
            for k, v in company.items():
                if v:
                    prompt += f"{k}: {v}\n"
        else:
            prompt += f"Name: {product.get('brand_name', name)}\n"
            prompt += f"Website: {product.get('official_url', '')}\n"

        # Marketing claims
        prompt += "\n--- MARKETING CLAIMS (VERBATIM — UNVERIFIED, DO NOT REPUBLISH AS FACT) ---\n"
        for c in claims:
            if isinstance(c, dict):
                prompt += f"- [{c.get('source', 'unknown')}] \"{c.get('claim', '')}\" (Verified: False)\n"
        if not claims:
            prompt += "No marketing claims captured\n"

        # Compliance flagged claims
        flagged = compliance.get("claim_audit", [])
        if flagged:
            prompt += f"\n--- COMPLIANCE FLAGS ({len(flagged)} flagged claims) ---\n"
            for item in flagged:
                prompt += f"FLAGGED: \"{item.get('claim', '')}\"\n"
                for issue in item.get("issues", []):
                    prompt += f"  Issue: {issue}\n"
                prompt += f"  Safe Alternative: \"{item.get('safe_alternative', '')}\"\n"

        # Required disclaimers
        req_disclaimers = compliance.get("required_disclaimers", [])
        if req_disclaimers:
            prompt += "\n--- REQUIRED DISCLAIMERS ---\n"
            for d in req_disclaimers:
                prompt += f"- {d}\n"

        # Testimonials (reference only)
        testimonials = product.get("testimonials", [])
        if testimonials:
            prompt += "\n--- TESTIMONIALS (Reference Only — Do Not Republish as Verified) ---\n"
            for t in testimonials:
                if isinstance(t, dict) and t.get("text"):
                    prompt += f"- {t.get('name', 'Anonymous')} ({t.get('location', '')}): \"{t['text'][:300]}...\"\n"

        # Publishing recommendations
        recs = data.get("publishing_recommendations", {})
        if recs:
            prompt += "\n--- PUBLISHING RECOMMENDATIONS ---\n"
            for site, info in recs.items():
                prompt += f"- {site}: Category IDs {info.get('category_ids', [])}\n"

        prompt += "\n════════════════════════════════════════════════════════\n"

        st.text_area("Copy this prompt into Claude Projects", value=prompt, height=500, key="export_prompt")

        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                "Download Prompt (.txt)",
                data=prompt,
                file_name=f"{name.lower().replace(' ', '-')}_prompt.txt",
                mime="text/plain",
                use_container_width=True,
            )
        with col2:
            st.download_button(
                "Download Full Report (.md)",
                data=st.session_state.get("result_report", ""),
                file_name=f"{name.lower().replace(' ', '-')}_source_report.md",
                mime="text/markdown",
                use_container_width=True,
            )

    # --- TAB: Raw JSON ---
    with tabs[8]:
        st.json(data)
