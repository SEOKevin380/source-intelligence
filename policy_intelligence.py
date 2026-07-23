"""Governed, offline policy-source snapshots.

The registry detects authoritative-source changes. It never mutates compliance
rules automatically: a changed source enters a review queue and remains
unadopted until a human records the mapped rule/test version.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "policy_sources.json"
SNAPSHOT_PATH = ROOT / "policy_snapshot.json"

AUTHORITY_RANK = {
    "regulator": 600,
    "publisher_policy": 500,
    "search_policy": 400,
    "quality_guidance": 300,
    "search_status": 200,
    "approved_exemplar": 100,
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_bytes(content: bytes, content_type: str = "") -> bytes:
    if "html" not in content_type.casefold():
        return content
    text = content.decode("utf-8", errors="replace")
    text = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", text,
                  flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.encode("utf-8")


def content_hash(content: bytes, content_type: str = "") -> str:
    return hashlib.sha256(_canonical_bytes(content, content_type)).hexdigest()


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    ids = [item["id"] for item in payload.get("sources", [])]
    if len(ids) != len(set(ids)):
        raise ValueError("Policy source IDs must be unique")
    for item in payload.get("sources", []):
        if item.get("authority") not in AUTHORITY_RANK:
            raise ValueError(f"Unknown policy authority: {item.get('authority')}")
        if not str(item.get("url", "")).startswith("https://"):
            raise ValueError(f"Policy source must use HTTPS: {item.get('id')}")
    return payload


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict:
    if not path.exists():
        return {"schema_version": 1, "sources": {}, "adoptions": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def applicable_sources(vertical: str, registry: dict | None = None) -> list[dict]:
    registry = registry or load_registry()
    vertical = (vertical or "general_consumer").casefold()
    return [
        item for item in registry.get("sources", [])
        if "all" in item.get("verticals", []) or vertical in item.get("verticals", [])
    ]


def record_observation(
    snapshot: dict,
    source: dict,
    body: bytes,
    *,
    content_type: str = "",
    etag: str = "",
    last_modified: str = "",
    checked_at: str = "",
) -> dict:
    """Record an observation and return its change classification."""
    checked_at = checked_at or _now()
    digest = content_hash(body, content_type)
    prior = snapshot.setdefault("sources", {}).get(source["id"], {})
    prior_hash = prior.get("observed_hash", "")
    status = "new" if not prior_hash else "unchanged" if prior_hash == digest else "changed"
    adopted_hash = snapshot.setdefault("adoptions", {}).get(source["id"], {}).get(
        "adopted_hash", ""
    )
    snapshot["sources"][source["id"]] = {
        "title": source["title"],
        "url": source["url"],
        "authority": source["authority"],
        "authority_rank": AUTHORITY_RANK[source["authority"]],
        "verticals": source.get("verticals", ["all"]),
        "checked_at": checked_at,
        "observed_hash": digest,
        "content_type": content_type,
        "etag": etag,
        "last_modified": last_modified,
        "status": status,
        "requires_review": bool(adopted_hash and adopted_hash != digest),
    }
    snapshot["generated_at"] = checked_at
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    return {"id": source["id"], "status": status, "hash": digest}


def adopt_source(snapshot: dict, source_id: str, rule_version: str,
                 reviewer: str, note: str = "") -> None:
    observed = snapshot.get("sources", {}).get(source_id)
    if not observed:
        raise ValueError(f"No observed policy source: {source_id}")
    if not reviewer.strip() or not rule_version.strip():
        raise ValueError("Policy adoption requires reviewer and rule version")
    snapshot.setdefault("adoptions", {})[source_id] = {
        "adopted_hash": observed["observed_hash"],
        "rule_version": rule_version.strip(),
        "reviewer": reviewer.strip(),
        "note": note.strip(),
        "adopted_at": _now(),
    }
    observed["requires_review"] = False
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)


def snapshot_hash(snapshot: dict) -> str:
    material = {
        "sources": {
            key: value.get("observed_hash", "")
            for key, value in sorted(snapshot.get("sources", {}).items())
        },
        "adoptions": {
            key: {
                "adopted_hash": value.get("adopted_hash", ""),
                "rule_version": value.get("rule_version", ""),
            }
            for key, value in sorted(snapshot.get("adoptions", {}).items())
        },
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def policy_status(vertical: str, snapshot: dict | None = None) -> dict:
    registry = load_registry()
    snapshot = snapshot or load_snapshot()
    sources = applicable_sources(vertical, registry)
    observations = snapshot.get("sources", {})
    missing = [item["id"] for item in sources if item["id"] not in observations]
    changed = [
        item["id"] for item in sources
        if observations.get(item["id"], {}).get("requires_review")
    ]
    return {
        "snapshot_hash": snapshot.get("snapshot_hash", snapshot_hash(snapshot)),
        "applicable_source_count": len(sources),
        "missing_observations": missing,
        "changes_requiring_review": changed,
        "current": not missing and not changed,
    }


def format_policy_context(vertical: str, snapshot: dict | None = None) -> str:
    status = policy_status(vertical, snapshot)
    return "\n".join((
        "═══ GOVERNED POLICY SNAPSHOT ═══",
        f"Snapshot hash: {status['snapshot_hash']}",
        f"Applicable authoritative sources: {status['applicable_source_count']}",
        "Policy state: " + ("current" if status["current"] else "review_required"),
        "Missing observations: "
        + (", ".join(status["missing_observations"]) or "none"),
        "Changed sources awaiting rule review: "
        + (", ".join(status["changes_requiring_review"]) or "none"),
        "Authority order: regulator > publisher policy > search policy > "
        "quality guidance > approved exemplar.",
        "Policy snapshots control compliance only. They never supply product facts.",
        "═══════════════════════════════════════════════",
    ))
