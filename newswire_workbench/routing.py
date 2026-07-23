"""Stage-specific model routing and budget policy for the newswire pipeline.

Models are deliberately pinned. A newer catalog entry is not promoted until it
beats the current route on MBK's regression corpus.
"""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    max_tokens: int
    max_calls: int
    input_per_million: float
    output_per_million: float


RISK_TIERS = {
    "general_consumer": 0,
    "device": 0,
    "collectible": 0,
    "software": 0,
    "gaming": 1,
    "health": 2,
    "telehealth": 2,
    "financial": 3,
    "political": 3,
}


def risk_tier(vertical: str) -> int:
    return RISK_TIERS.get((vertical or "").strip().lower(), 1)


def route_for(stage: str, vertical: str = "general_consumer") -> ModelRoute:
    """Return the pinned production route for one bounded job."""
    routes = {
        "draft": ModelRoute(
            "anthropic-direct",
            os.environ.get("NEWSWIRE_DRAFT_MODEL", "claude-haiku-4-5-20251001"),
            12000, 1, 1.0, 5.0,
        ),
        "compliance_repair": ModelRoute(
            "anthropic-direct",
            os.environ.get("NEWSWIRE_REPAIR_MODEL", "claude-haiku-4-5-20251001"),
            9000, 2, 1.0, 5.0,
        ),
        # SEO repair is a later, independent job. It must never inherit the
        # paid-call count from the initial compliance repair.
        "seo_repair": ModelRoute(
            "anthropic-direct",
            os.environ.get("NEWSWIRE_REPAIR_MODEL", "claude-haiku-4-5-20251001"),
            9000, 2, 1.0, 5.0,
        ),
        # Mandatory editorial gates never terminate in an operator queue.
        # This independent rescue budget uses the stronger writer with enough
        # output room to rebuild a complete long-form article.
        "quality_rescue": ModelRoute(
            "anthropic-direct",
            os.environ.get(
                "NEWSWIRE_QUALITY_RESCUE_MODEL",
                "claude-sonnet-4-5-20250929",
            ),
            14000, 2, 3.0, 15.0,
        ),
        "war_room_rebuild": ModelRoute(
            "anthropic-direct",
            os.environ.get(
                "NEWSWIRE_WAR_ROOM_MODEL",
                os.environ.get(
                    "NEWSWIRE_DRAFT_MODEL", "claude-sonnet-4-5-20250929"
                ),
            ),
            16000, 1, 3.0, 15.0,
        ),
        "seo": ModelRoute(
            "anthropic-direct",
            os.environ.get("NEWSWIRE_SEO_MODEL", "claude-haiku-4-5-20251001"),
            9000, 1, 1.0, 5.0,
        ),
        "compliance": ModelRoute(
            "openai-direct",
            os.environ.get("NEWSWIRE_COMPLIANCE_MODEL", "gpt-5.4-mini"),
            5000, 1, 0.75, 4.5,
        ),
        "final_signoff": ModelRoute(
            "openai-direct",
            os.environ.get(
                "NEWSWIRE_FINAL_MODEL",
                "gpt-5.4" if risk_tier(vertical) >= 2 else "gpt-5.4-mini",
            ),
            5000, 2, 2.5 if risk_tier(vertical) >= 2 else 0.75,
            15.0 if risk_tier(vertical) >= 2 else 4.5,
        ),
        # This is a distinct regression checkpoint after Claude's SEO pass.
        # It must not share the pre-SEO final-signoff call counter.
        "post_seo_signoff": ModelRoute(
            "openai-direct",
            os.environ.get(
                "NEWSWIRE_FINAL_MODEL",
                "gpt-5.4" if risk_tier(vertical) >= 2 else "gpt-5.4-mini",
            ),
            5000, 2, 2.5 if risk_tier(vertical) >= 2 else 0.75,
            15.0 if risk_tier(vertical) >= 2 else 4.5,
        ),
        # A semantic rescue or adjudicated rewrite must be reviewed by a
        # provider independent of the writer. This route can never be replaced
        # by a synthetic deterministic "approved" report.
        "independent_rescue_signoff": ModelRoute(
            "openai-direct",
            os.environ.get(
                "NEWSWIRE_FINAL_MODEL",
                "gpt-5.4" if risk_tier(vertical) >= 2 else "gpt-5.4-mini",
            ),
            5000, 2, 2.5 if risk_tier(vertical) >= 2 else 0.75,
            15.0 if risk_tier(vertical) >= 2 else 4.5,
        ),
        # If the normal independent reviewer has rejected three materially
        # different revisions, escalate the exact artifact and its latest
        # objections to the strongest editorial adjudicator. This is a
        # separate bounded job, not a fourth call to an exhausted route.
        "executive_rescue_signoff": ModelRoute(
            "openai-direct",
            os.environ.get("NEWSWIRE_EXECUTIVE_MODEL", "gpt-5.4"),
            7000, 1, 2.5, 15.0,
        ),
        "war_room_signoff": ModelRoute(
            "openai-direct",
            os.environ.get("NEWSWIRE_EXECUTIVE_MODEL", "gpt-5.4"),
            7000, 1, 2.5, 15.0,
        ),
    }
    if stage not in routes:
        raise KeyError(f"No model route for stage: {stage}")
    return routes[stage]


def estimated_cost(route: ModelRoute, input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens / 1_000_000 * route.input_per_million
        + output_tokens / 1_000_000 * route.output_per_million,
        6,
    )
