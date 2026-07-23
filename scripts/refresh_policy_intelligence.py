#!/usr/bin/env python3
"""Refresh authoritative policy hashes without changing production rules."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from policy_intelligence import (  # noqa: E402
    REGISTRY_PATH,
    SNAPSHOT_PATH,
    load_registry,
    load_snapshot,
    record_observation,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT_PATH)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    registry = load_registry(args.registry)
    snapshot = load_snapshot(args.snapshot)
    results = []
    for source in registry["sources"]:
        request = urllib.request.Request(
            source["url"],
            headers={"User-Agent": "SourceIntelligencePolicyMonitor/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                body = response.read()
                results.append(record_observation(
                    snapshot, source, body,
                    content_type=response.headers.get("Content-Type", ""),
                    etag=response.headers.get("ETag", ""),
                    last_modified=response.headers.get("Last-Modified", ""),
                ))
        except Exception as exc:
            results.append({"id": source["id"], "status": "error", "error": str(exc)})
    temp = args.snapshot.with_suffix(args.snapshot.suffix + ".tmp")
    temp.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    os.replace(temp, args.snapshot)
    print(json.dumps({"snapshot": str(args.snapshot), "results": results}, indent=2))
    return 1 if any(item["status"] == "error" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
