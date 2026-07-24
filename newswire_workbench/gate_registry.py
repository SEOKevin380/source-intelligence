"""Single ownership and severity registry for publication checks."""

from __future__ import annotations


_BLOCKERS = {
    "D1", "D2", "D3", "D5", "D6", "D7", "D17", "D18", "D19", "D20",
}
_MECHANICAL = {
    "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9",
    "D10", "D11", "D12", "D13", "D14", "D17",
}
_SEMANTIC = {"D18", "D19", "D20"}

GATE_REGISTRY = {
    f"D{number}": {
        "publication_blocker": f"D{number}" in _BLOCKERS,
        "repair_owner": (
            "mechanical"
            if f"D{number}" in _MECHANICAL
            else "semantic"
            if f"D{number}" in _SEMANTIC
            else "advisory"
        ),
    }
    for number in range(1, 21)
}

ALL_GATE_IDS = frozenset(GATE_REGISTRY)
PUBLICATION_BLOCKER_IDS = frozenset(
    gate_id for gate_id, rule in GATE_REGISTRY.items()
    if rule["publication_blocker"]
)
MECHANICAL_GATES = frozenset(
    gate_id for gate_id, rule in GATE_REGISTRY.items()
    if rule["repair_owner"] == "mechanical"
)
SEMANTIC_GATES = frozenset(
    gate_id for gate_id, rule in GATE_REGISTRY.items()
    if rule["repair_owner"] == "semantic"
)
QUALITY_GATES = frozenset(
    gate_id for gate_id, rule in GATE_REGISTRY.items()
    if not rule["publication_blocker"]
)
