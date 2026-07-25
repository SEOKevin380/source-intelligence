"""Executable publisher contracts for the automated newswire workbench.

Publisher rules are operational behavior, not prompt decoration.  Keeping the
disclosure, link, FAQ, and voice requirements in one registry prevents a new
publisher from silently inheriting AccessNewsWire or Barchart defaults.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


GLOBE_DISCLOSURE_TEXT = (
    "Paid Advertorial: Compensation may be received if a purchase is made "
    "through links in this advertorial."
)


@dataclass(frozen=True)
class PlatformContract:
    name: str
    automated: bool
    affiliate_cta_target: int
    disclosure_position: str
    faq_allowed: bool
    related_link_count: int
    voice: str
    rejection_reason: str = ""


_CONTRACTS = {
    "AccessNewsWire": PlatformContract(
        name="AccessNewsWire",
        automated=True,
        affiliate_cta_target=4,
        disclosure_position="opening",
        faq_allowed=True,
        related_link_count=0,
        voice="seller-attributed advertorial",
    ),
    "Barchart Advertorial": PlatformContract(
        name="Barchart Advertorial",
        automated=True,
        affiliate_cta_target=3,
        disclosure_position="opening",
        faq_allowed=True,
        related_link_count=0,
        voice="seller-attributed advertorial",
    ),
    "Globe Newswire": PlatformContract(
        name="Globe Newswire",
        automated=True,
        affiliate_cta_target=0,
        disclosure_position="end",
        faq_allowed=False,
        related_link_count=1,
        voice="brand-as-subject mechanism-forward Format C",
    ),
    "Newswire.com": PlatformContract(
        name="Newswire.com",
        automated=False,
        affiliate_cta_target=0,
        disclosure_position="unsupported",
        faq_allowed=False,
        related_link_count=0,
        voice="unsupported affiliate-advertorial route",
        rejection_reason=(
            "Newswire.com is not supported by this affiliate-advertorial "
            "workflow because the submission type may be rejected by the "
            "publisher. Use AccessNewsWire or another approved route."
        ),
    ),
}

PLATFORM_CONTRACTS = tuple(_CONTRACTS)
AUTOMATED_PLATFORMS = tuple(
    name for name, contract in _CONTRACTS.items() if contract.automated
)


def platform_contract(platform: str) -> PlatformContract:
    """Return the exact declared contract; unknown publishers fail closed."""
    try:
        return _CONTRACTS[str(platform or "").strip()]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported automated publishing platform: {platform!r}"
        ) from exc


def require_automated_platform(platform: str) -> PlatformContract:
    contract = platform_contract(platform)
    if not contract.automated:
        raise ValueError(contract.rejection_reason)
    return contract


def platform_contract_report(platform: str) -> dict:
    return asdict(platform_contract(platform))


def platform_prompt_rules(platform: str) -> str:
    """Return authoritative writer/reviewer rules for one platform."""
    contract = require_automated_platform(platform)
    if contract.name == "Globe Newswire":
        return f"""
GLOBE FORMAT C EXECUTION CONTRACT:
- This account-specific Format C contract controls over generic press-release
  or advertorial defaults.
- Do not place an affiliate or compensation disclosure in the opening.
- Use no CTA or FAQ/Q&A section.
- Use the product/brand as the grammatical subject and mechanism-forward
  language such as "[Brand] is designed to support ...".
- Do not write "according to the company," "according to the brand,"
  "according to the seller," "the brand states," or equivalent attribution.
- Include one Related Links block at the end with exactly one outbound product
  link and a neutral, non-CTA label.
- Make the final paragraph exactly: "{GLOBE_DISCLOSURE_TEXT}"
""".strip()
    target = contract.affiliate_cta_target
    return f"""
{contract.name.upper()} EXECUTION CONTRACT:
- Put one concise paid-advertorial/passive-compensation disclosure at the top.
- Use {target} naturally spaced affiliate CTAs in a full-length article.
- Keep seller/source attribution local to the governed product claim.
- FAQs are allowed when they add product-specific reader value.
""".strip()
