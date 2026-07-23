"""Single, code-owned budget for one complete autonomous publication run."""

from __future__ import annotations

import os


REQUIRED_CALL_PATH = (
    "draft",
    "compliance",
    "compliance_repair",
    "final_signoff",
)
RUN_CALL_LIMIT = len(REQUIRED_CALL_PATH)
RUN_COST_LIMIT_USD = 1.50
RUN_SECONDS_LIMIT = 480.0


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
        "calls": RUN_CALL_LIMIT,
        "estimated_cost": RUN_COST_LIMIT_USD,
        "seconds": RUN_SECONDS_LIMIT,
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
