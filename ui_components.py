"""
Source Intelligence — Reusable Streamlit UI Components
=======================================================
Extracted UI building blocks for the simplified VA workflow.

These components provide beginner-friendly views of the underlying
intelligence data, hiding expert complexity behind plain-language labels.
"""

import streamlit as st
from typing import Optional, List

from workflow import Job, JobStatus, StageStatus, PipelineStage


def readiness_badge(job: Optional[Job] = None,
                    compliance_state: str = "",
                    conflicts: int = 0) -> str:
    """Render a plain-language readiness badge.

    Returns the status label (also renders via st.status if in Streamlit context).
    Maps internal states to VA-friendly labels.
    """
    if job is None and not compliance_state:
        label, icon, color = "Not Started", "gray", "secondary"
    elif job and job.status == JobStatus.RUNNING:
        stage = job.current_stage or "starting"
        label = f"Collecting ({stage})"
        icon, color = "hourglass", "primary"
    elif job and job.status == JobStatus.FAILED:
        label = "Needs Help"
        icon, color = "warning", "error"
    elif job and job.status == JobStatus.PAUSED:
        label = "Paused (Budget Limit)"
        icon, color = "pause", "warning"
    elif conflicts > 0:
        label = f"Needs Help ({conflicts} conflicts)"
        icon, color = "warning", "warning"
    elif compliance_state == "blocked":
        label = "Blocked (Compliance Issue)"
        icon, color = "error", "error"
    elif compliance_state == "human_review":
        label = "Needs Compliance Review"
        icon, color = "review", "warning"
    elif compliance_state in ("editorial", ""):
        if job and job.status == JobStatus.COMPLETED:
            label = "Ready to Plan"
            icon, color = "check", "success"
        else:
            label = "Ready for Editorial Review"
            icon, color = "check", "primary"
    elif compliance_state == "cleared":
        label = "Approved"
        icon, color = "check_circle", "success"
    else:
        label = compliance_state.replace("_", " ").title()
        icon, color = "info", "secondary"

    return label


def source_provenance_card(artifact_id: str = "",
                           source_url: str = "",
                           source_class: str = "",
                           captured_at: str = "",
                           tls_verified: bool = True,
                           content_length: int = 0) -> dict:
    """Build data for a source provenance display card.

    Returns a dict of display fields suitable for rendering.
    """
    class_labels = {
        "official_vendor": "Official (Vendor)",
        "regulatory_database": "Regulatory Database",
        "peer_reviewed": "Peer-Reviewed Journal",
        "independent_lab": "Independent Lab",
        "news_media": "News Media",
        "user_generated": "User Review",
        "search_result": "Search Result",
        "anonymous": "Anonymous Source",
    }

    tls_label = "Verified" if tls_verified else "NOT VERIFIED"
    size_label = _format_size(content_length)
    date_label = captured_at[:19].replace("T", " ") if captured_at else "Unknown"

    return {
        "source_type": class_labels.get(source_class, source_class),
        "url": source_url,
        "captured": date_label,
        "tls": tls_label,
        "size": size_label,
        "artifact_id": artifact_id[:12] + "..." if len(artifact_id) > 12 else artifact_id,
    }


def conflict_resolution_data(claim_a: dict, claim_b: dict,
                              conflict_description: str = "") -> dict:
    """Build data for a side-by-side conflict comparison.

    Takes two claim dicts and returns structured comparison data.
    """
    return {
        "description": conflict_description,
        "claim_a": {
            "text": claim_a.get("claim_text", ""),
            "source": claim_a.get("source_class", "unknown"),
            "confidence": claim_a.get("confidence", 0.0),
            "excerpt": claim_a.get("exact_excerpt", ""),
        },
        "claim_b": {
            "text": claim_b.get("claim_text", ""),
            "source": claim_b.get("source_class", "unknown"),
            "confidence": claim_b.get("confidence", 0.0),
            "excerpt": claim_b.get("exact_excerpt", ""),
        },
    }


def pipeline_progress(job: Job) -> List[dict]:
    """Build pipeline progress display data.

    Returns a list of stage dicts with name, status, and icon.
    """
    stages = job.get_stages()
    progress = []

    status_icons = {
        StageStatus.PENDING: "circle",
        StageStatus.RUNNING: "hourglass",
        StageStatus.COMPLETED: "check",
        StageStatus.FAILED: "x",
        StageStatus.SKIPPED: "minus",
        StageStatus.CANCELLED: "slash",
    }

    stage_labels = {
        "identify": "Identify Product",
        "acquire": "Fetch Pages",
        "extract": "Extract Facts",
        "reconcile": "Check Conflicts",
        "research": "PubMed Research",
        "comply": "Compliance Check",
        "analyze_site": "Keyword Research",
        "analyze_market": "Market Analysis",
        "plan": "Content Planning",
        "review": "Quality Review",
        "source_pack": "Build Source Pack",
    }

    for stage in stages:
        status = job.get_stage_status(stage)
        progress.append({
            "stage": stage.value,
            "label": stage_labels.get(stage.value, stage.value),
            "status": status.value,
            "icon": status_icons.get(status, "circle"),
        })

    return progress


def _format_size(bytes_count: int) -> str:
    """Format byte count as human-readable size."""
    if bytes_count < 1024:
        return f"{bytes_count} B"
    elif bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f} KB"
    return f"{bytes_count / (1024 * 1024):.1f} MB"
