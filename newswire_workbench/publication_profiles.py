"""Canonical publisher × vertical publication contracts."""

from __future__ import annotations


_DEFAULT = {
    "hard_floor": 0,
    "target_min": 0,
    "target_max": 0,
    "recovery_target": 0,
    "label": "general article",
}

_PROFILES = {
    ("Barchart Advertorial", "device"): {
        "hard_floor": 1400,
        "target_min": 1600,
        "target_max": 1900,
        "recovery_target": 1800,
        "label": "device Barchart",
    },
    ("AccessNewsWire", "financial"): {
        "hard_floor": 1800,
        "target_min": 1800,
        "target_max": 2400,
        "recovery_target": 2200,
        "label": "financial AccessNewsWire",
    },
}


def publication_profile(platform: str, vertical: str) -> dict:
    """Return one immutable depth contract for every workflow consumer."""
    return dict(_PROFILES.get((platform, vertical), _DEFAULT))
