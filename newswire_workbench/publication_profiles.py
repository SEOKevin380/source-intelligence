"""Canonical publisher × vertical publication contracts."""

from __future__ import annotations


_DEFAULT = {
    "hard_floor": 1000,
    "target_min": 1200,
    "target_max": 1800,
    "recovery_target": 1500,
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

_PLATFORM_DEFAULTS = {
    "Barchart Advertorial": {
        "hard_floor": 1400,
        "target_min": 1600,
        "target_max": 1900,
        "recovery_target": 1800,
        "label": "Barchart advertorial",
    },
    "AccessNewsWire": {
        "hard_floor": 1400,
        "target_min": 1600,
        "target_max": 2200,
        "recovery_target": 1900,
        "label": "AccessNewsWire release",
    },
    "Newswire.com": {
        "hard_floor": 1200,
        "target_min": 1400,
        "target_max": 2000,
        "recovery_target": 1700,
        "label": "Newswire.com release",
    },
    "Globe Newswire": {
        "hard_floor": 1200,
        "target_min": 1400,
        "target_max": 2000,
        "recovery_target": 1700,
        "label": "Globe Newswire release",
    },
    "Domain Site": {
        "hard_floor": 1000,
        "target_min": 1200,
        "target_max": 1800,
        "recovery_target": 1500,
        "label": "domain article",
    },
}


def publication_profile(platform: str, vertical: str) -> dict:
    """Return one immutable depth contract for every workflow consumer."""
    return dict(
        _PROFILES.get(
            (platform, vertical),
            _PLATFORM_DEFAULTS.get(platform, _DEFAULT),
        )
    )
