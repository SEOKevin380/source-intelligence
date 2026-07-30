"""Streamlit UI for the local Newswire Compliance Workbench."""

import json
import os
from pathlib import Path

import streamlit as st

from newswire_workbench import WorkbenchEngine
from newswire_workbench.prompts import PLATFORMS


st.set_page_config(page_title="Newswire Compliance Workbench", page_icon="📰", layout="wide")
st.title("Newswire Compliance Workbench")
st.caption("Sealed source pack → routed draft → independent compliance → bounded repair → SEO regression → approved package")


@st.cache_data(ttl=30, show_spinner=False)
def _cached_admin_review_action(
    workbench_root, project_id, project_updated_at
):
    """Cache only the explicitly selected durable action."""
    return WorkbenchEngine(workbench_root).admin_review_action(project_id)


engine = WorkbenchEngine()
caps = engine.capabilities()

if caps["anthropic"] and caps["openai"]:
    st.success("Automation ready: Claude and ChatGPT are connected.")
else:
    missing = []
    if not caps["anthropic"]:
        missing.append("Claude")
    if not caps["openai"]:
        missing.append("ChatGPT")
    st.warning("Manual fallback active for: " + ", ".join(missing) + ". Projects and audit history still work.")

master_path = Path(__file__).with_name("MBK_Project_Instructions_All_Platforms.txt")
master_instructions = master_path.read_text(encoding="utf-8") if master_path.exists() else ""

with st.sidebar:
    st.header("New project")
    title = st.text_input("Project name")
    platform = st.selectbox("Platform", PLATFORMS)
    vertical = st.selectbox("Product type", ["auto", "health", "financial", "gaming", "collectible", "device", "general_consumer"])
    uploaded = st.file_uploader("Publication source pack or source brief", type=["json", "txt", "md"])
    source_paste = st.text_area("Or paste source brief", height=180)
    if st.button("Create project", type="primary", use_container_width=True):
        source = uploaded.getvalue().decode("utf-8") if uploaded else source_paste
        try:
            pid = engine.create_project(title, platform, source, vertical)
            st.session_state.project_id = pid
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

_stuck = engine.admin_review_queue(resolve_actions=False)
if _stuck:
    _oldest = max((item.get("age_hours") or 0) for item in _stuck)
    with st.expander(
        f"⚠️ Kevin review queue — {len(_stuck)} waiting "
        f"(oldest {_oldest:.1f}h)",
        expanded=_oldest >= 4,
    ):
        _queue_labels = {
            item["project_id"]: (
                f"{item['title']} · {item['platform']} · "
                f"{item.get('age_hours') or 0:.1f}h"
            )
            for item in _stuck
        }
        _queue_id = st.selectbox(
            "Waiting transaction",
            list(_queue_labels),
            index=None,
            placeholder="Select a transaction to inspect",
            format_func=_queue_labels.get,
            key="admin_review_queue_project",
        )
        if _queue_id:
            _queue_item = next(
                item for item in _stuck
                if item["project_id"] == _queue_id
            )
            _queue_action = _cached_admin_review_action(
                str(engine.root),
                _queue_id,
                _queue_item["updated_at"],
            )
            st.markdown(f"**{_queue_action['label']}**")
            st.caption(_queue_action["reason"])
            if st.button(
                "Open waiting transaction",
                key=f"open_admin_review_{_queue_id}",
            ):
                st.session_state.project_id = _queue_id
                st.rerun()

projects = engine.list_projects()
if not projects:
    st.info("Create the first project from a Source Intelligence pack or source brief.")
    st.stop()

labels = {p["id"]: f"{p['title']} · {p['platform']} · {p['stage'].replace('_', ' ').title()}" for p in projects}
default_id = st.session_state.get("project_id", projects[0]["id"])
project_id = st.selectbox("Project", list(labels), format_func=labels.get,
                          index=list(labels).index(default_id) if default_id in labels else 0)
st.session_state.project_id = project_id
p = engine.get(project_id)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Stage", p["stage"].replace("_", " ").title())
c2.metric("Product type", p["vertical"].replace("_", " ").title())
c3.metric("Revision rounds", p["revision_round"])
c4.metric("Mandatory edits", p["last_report"].get("mandatory_count", "—"))
usage = engine.usage_summary(project_id)
c5.metric("AI cost", f"${usage['estimated_cost']:.3f}")

st.subheader("Next step")
st.write(engine.next_action(p))
if p["stage"] not in {"package_ready", "admin_review"}:
    run_one, run_all = st.columns(2)
    if run_one.button("Run next step", type="primary", use_container_width=True):
        try:
            with st.spinner(engine.next_action(p) + "…"):
                engine.run_next(project_id, master_instructions)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if run_all.button("Run entire workflow", use_container_width=True):
        try:
            with st.spinner("Running draft, review, revisions, SEO, and final sign-off…"):
                engine.run_to_completion(project_id, master_instructions)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
elif p["stage"] == "admin_review":
    st.error("Three automated repair rounds did not clear every mandatory issue. This project is in Kevin's review queue; the VA can move to the next project.")

tab_article, tab_source, tab_report, tab_manual, tab_audit = st.tabs(
    ["Current article", "Source record", "Compliance report", "Manual fallback", "Audit history"]
)
with tab_article:
    if p["article_text"]:
        st.code(p["article_text"], language="html")
        st.download_button("Download current article", p["article_text"],
                           file_name=f"{p['id']}-article.html", mime="text/html")
    else:
        st.info("No draft yet.")
with tab_source:
    st.code(p["source_text"], language="text")
with tab_report:
    st.json(p["last_report"])
with tab_manual:
    st.caption("Use this only when an API key is unavailable or a provider is temporarily down.")
    manual_article = st.text_area("Paste Claude article/revision", height=220)
    if st.button("Import article") and manual_article.strip():
        engine.import_manual_article(project_id, manual_article)
        st.rerun()
    manual_report = st.text_area("Paste ChatGPT report (JSON preferred)", height=220)
    if st.button("Import compliance report") and manual_report.strip():
        engine.import_manual_report(project_id, manual_report)
        st.rerun()
with tab_audit:
    st.dataframe(engine.events(project_id), use_container_width=True, hide_index=True)

if p["stage"] == "package_ready":
    folder = engine.projects_dir / project_id
    export = engine.export_path(project_id)
    st.success("Submission package passed final compliance and is ready.")
    if export.exists():
        st.download_button(
            "Download submission package",
            export.read_bytes(),
            file_name=f"{p['title']}-submission-package.zip",
            mime="application/zip",
            type="primary",
        )
    if caps.get("wordpress"):
        if st.button("Send approved draft to ZingFast WordPress", use_container_width=True):
            try:
                with st.spinner("Saving approved copy as a WordPress draft…"):
                    wp_result = engine.send_to_wordpress_draft(project_id)
                st.success("WordPress draft saved. Nothing was published.")
                st.link_button("Open draft in WordPress", wp_result["edit_url"])
            except Exception as exc:
                st.error(str(exc))
    st.caption(f"Local audit folder: {folder}")
