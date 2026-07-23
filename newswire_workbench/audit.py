"""Offline publication-contract audit with no model or network calls."""

from __future__ import annotations

from dataclasses import asdict

from .formatting import repair_publication_gates
from .learning import PUBLICATION_BLOCKER_IDS, deterministic_findings, partition_findings
from .routing import route_for


MECHANICAL_GATES = frozenset({
    "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9",
    "D10", "D11", "D12", "D13", "D14", "D17",
})
SEMANTIC_GATES = frozenset({"D18", "D19", "D20"})
QUALITY_GATES = frozenset({"D15", "D16"})
REQUIRED_ROUTES = (
    "draft",
    "compliance_repair",
    "quality_rescue",
    "war_room_rebuild",
    "final_signoff",
    "post_seo_signoff",
    "independent_rescue_signoff",
    "executive_rescue_signoff",
    "war_room_signoff",
)


def audit_system_contract(vertical: str) -> dict:
    """Prove that every known blocker is owned and every route has a budget."""
    owned = MECHANICAL_GATES | SEMANTIC_GATES
    missing_owners = sorted(PUBLICATION_BLOCKER_IDS - owned)
    unknown_owners = sorted(owned - PUBLICATION_BLOCKER_IDS)
    routes = {}
    route_errors = []
    for purpose in REQUIRED_ROUTES:
        try:
            route = route_for(purpose, vertical)
            routes[purpose] = asdict(route)
            if route.max_calls < 1 or route.max_tokens < 1:
                route_errors.append(f"{purpose}: unusable call/token budget")
        except Exception as exc:  # pragma: no cover - defensive contract report
            route_errors.append(f"{purpose}: {exc}")
    return {
        "passed": not missing_owners and not unknown_owners and not route_errors,
        "blocker_count": len(PUBLICATION_BLOCKER_IDS),
        "mechanical_gates": sorted(MECHANICAL_GATES),
        "semantic_gates": sorted(SEMANTIC_GATES),
        "quality_gates": sorted(QUALITY_GATES),
        "missing_gate_owners": missing_owners,
        "unknown_gate_owners": unknown_owners,
        "route_errors": route_errors,
        "routes": routes,
    }


def audit_article(
    article: str,
    platform: str,
    vertical: str,
    affiliate_href: str = "",
    max_mechanical_passes: int = 5,
) -> dict:
    """Expose all gates together and replay deterministic repairs to fixed point."""
    original_findings = deterministic_findings(article, platform, vertical)
    current = article
    passes = []
    seen = {current}
    for pass_number in range(1, max_mechanical_passes + 1):
        before = deterministic_findings(current, platform, vertical)
        mechanical_before = [
            item for item in before if item.get("id") in MECHANICAL_GATES
        ]
        repaired = repair_publication_gates(
            current, platform, vertical, affiliate_href
        )
        after = deterministic_findings(repaired, platform, vertical)
        passes.append({
            "pass": pass_number,
            "before": [item["id"] for item in before],
            "mechanical_before": [item["id"] for item in mechanical_before],
            "after": [item["id"] for item in after],
            "changed": repaired != current,
        })
        current = repaired
        if not mechanical_before or repaired in seen:
            break
        seen.add(repaired)

    final_findings = deterministic_findings(current, platform, vertical)
    blockers, recommendations = partition_findings(final_findings)
    mechanical_remaining = [
        item for item in blockers if item.get("id") in MECHANICAL_GATES
    ]
    semantic_remaining = [
        item for item in blockers if item.get("id") in SEMANTIC_GATES
    ]
    contract = audit_system_contract(vertical)
    return {
        "passed": (
            contract["passed"]
            and not blockers
            and not mechanical_remaining
        ),
        "system_contract": contract,
        "initial_findings": original_findings,
        "repair_passes": passes,
        "final_findings": final_findings,
        "blockers": blockers,
        "recommendations": recommendations,
        "mechanical_remaining": mechanical_remaining,
        "semantic_remaining": semantic_remaining,
        "article": current,
    }
