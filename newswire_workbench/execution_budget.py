"""Single, code-owned budget for one complete autonomous publication run."""

from __future__ import annotations

import os


REQUIRED_CALL_PATH = (
    "draft",
    "compliance",
    "compliance_repair",
    "final_signoff",
)
PURPOSE_CALL_LIMITS = {purpose: 1 for purpose in REQUIRED_CALL_PATH}
RUN_CALL_LIMIT = len(REQUIRED_CALL_PATH)
PROVIDER_TIMEOUT_SECONDS = 90.0
EXPECTED_COST_USD = 1.50


def execution_budget() -> dict:
    """Return the bounded budget and expose, but never obey, unsafe overrides.

    A lower call ceiling can pay for a draft while making its mandatory
    independent review impossible. A higher ceiling weakens cost control.
    The complete four-call path is therefore a code invariant.
    """
    configured_calls = os.environ.get("NEWSWIRE_MAX_RUN_CALLS")
    configured_cost = os.environ.get("NEWSWIRE_MAX_RUN_COST_USD")
    configured_seconds = os.environ.get("NEWSWIRE_MAX_RUN_SECONDS")
    return {
        "required_call_path": list(REQUIRED_CALL_PATH),
        "required_calls": RUN_CALL_LIMIT,
        "purpose_call_limits": dict(PURPOSE_CALL_LIMITS),
        "calls": RUN_CALL_LIMIT,
        "provider_timeout_seconds": PROVIDER_TIMEOUT_SECONDS,
        "expected_cost": EXPECTED_COST_USD,
        "hard_limits": {
            "paid_calls": RUN_CALL_LIMIT,
            "seconds_per_provider_call": PROVIDER_TIMEOUT_SECONDS,
        },
        "advisory_limits": {
            "expected_cost": EXPECTED_COST_USD,
        },
        "configured_overrides": {
            "calls": configured_calls,
            "estimated_cost": configured_cost,
            "seconds": configured_seconds,
        },
        "ignored_unsafe_overrides": {
            key: value
            for key, value in {
                "calls": configured_calls,
                "estimated_cost": configured_cost,
                "seconds": configured_seconds,
            }.items()
            if value not in {None, ""}
        },
    }
