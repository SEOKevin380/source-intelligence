#!/usr/bin/env python3
"""Zero-model-call intake-to-workbench contract audit."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from entities import OfferingType
from intelligence_packs import INTELLIGENCE_PACKS
from newswire_workbench.audit import audit_system_contract
from newswire_workbench.prompts import (
    PRODUCT_TYPE_COVERAGE,
    detect_vertical,
)
from newswire_workbench.publication_profiles import publication_profile
from offering_taxonomy import (
    CANONICAL_PRODUCT_TYPES,
    assert_taxonomy_complete,
    exemplar_vertical,
    policy_vertical_aliases,
    risk_tier,
)


PLATFORMS = ("AccessNewsWire", "Barchart Advertorial")


def audit_pipeline_contract() -> dict:
    errors = []
    checks = []
    enum_values = {item.value for item in OfferingType}
    try:
        assert_taxonomy_complete(enum_values)
    except RuntimeError as exc:
        errors.append(str(exc))

    for product_type in CANONICAL_PRODUCT_TYPES:
        offering_type = OfferingType(product_type)
        pack = INTELLIGENCE_PACKS.get(offering_type) or {}
        required_keys = {
            "required_facts", "mandatory_facts", "authoritative_sources",
            "compliance_rules", "evidence_requirements",
            "content_opportunities",
        }
        missing = sorted(required_keys - set(pack))
        route = detect_vertical(json.dumps({
            "product": {"product_type": product_type}
        }))
        route_errors = []
        if route != product_type:
            route_errors.append(f"flattened_route:{route}")
        if missing:
            route_errors.append(f"missing_pack_keys:{missing}")
        if product_type not in PRODUCT_TYPE_COVERAGE:
            route_errors.append("missing_prompt_coverage")
        if not exemplar_vertical(product_type):
            route_errors.append("missing_exemplar_route")
        if not policy_vertical_aliases(product_type):
            route_errors.append("missing_policy_scope")
        if risk_tier(product_type) not in {0, 1, 2, 3}:
            route_errors.append("invalid_risk_tier")
        for platform in PLATFORMS:
            profile = publication_profile(platform, product_type)
            if not (
                0 < profile["hard_floor"]
                <= profile["target_min"]
                <= profile["target_max"]
            ):
                route_errors.append(f"invalid_depth_contract:{platform}")
            system = audit_system_contract(product_type)
            if not system["passed"]:
                route_errors.append(f"invalid_execution_contract:{platform}")
        checks.append({
            "product_type": product_type,
            "route": route,
            "risk_tier": risk_tier(product_type),
            "pack_required_facts": len(pack.get("required_facts", [])),
            "errors": route_errors,
        })
        errors.extend(f"{product_type}:{item}" for item in route_errors)

    return {
        "passed": not errors,
        "product_types_checked": len(checks),
        "platform_contracts_checked": len(checks) * len(PLATFORMS),
        "model_calls": 0,
        "errors": errors,
        "checks": checks,
    }


if __name__ == "__main__":
    result = audit_pipeline_contract()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)
