#!/usr/bin/env python3
"""Atomically refresh publisher intelligence from the latest MBK workbook."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
from pathlib import Path

from build_approved_release_index import ROOT, build


DEFAULT_WORKBOOKS = (
    Path.home() / "Desktop" / "MBK2026 (1).xlsx",
    Path.home() / "Desktop" / "Spreadsheets & Data" / "MBK2025 Repository Data.xlsx",
)
DEFAULT_OUTPUT = Path(ROOT) / "approved_release_index.json.gz"


def _load(path: Path) -> dict:
    if not path.exists():
        return {"_meta": {}, "releases": []}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _select_workbook(explicit: str = "") -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        return path
    available = [path for path in DEFAULT_WORKBOOKS if path.exists()]
    if not available:
        raise FileNotFoundError(
            "No MBK publishing workbook found; pass --workbook explicitly"
        )
    return max(available, key=lambda path: path.stat().st_mtime)


def refresh(workbook: Path, output: Path, allow_decrease: bool = False) -> dict:
    previous = _load(output)
    payload = build(str(workbook))
    old_urls = {
        item.get("live_url") for item in previous.get("releases", [])
        if item.get("live_url")
    }
    new_urls = {
        item.get("live_url") for item in payload.get("releases", [])
        if item.get("live_url")
    }
    if old_urls and len(new_urls) < len(old_urls) and not allow_decrease:
        raise RuntimeError(
            "Refusing corpus regression: "
            f"{len(old_urls):,} existing releases versus {len(new_urls):,} "
            "from the selected workbook. Use --allow-decrease only after "
            "verifying the workbook intentionally removed approvals."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=output.name + ".", suffix=".tmp", dir=output.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with gzip.open(
            temporary, "wt", encoding="utf-8", compresslevel=9
        ) as handle:
            json.dump(
                payload, handle, ensure_ascii=False, separators=(",", ":")
            )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "workbook": str(workbook),
        "output": str(output),
        "previous_release_count": len(old_urls),
        "current_release_count": len(new_urls),
        "new_release_count": len(new_urls - old_urls),
        "removed_release_count": len(old_urls - new_urls),
        "new_release_urls": sorted(new_urls - old_urls),
        "built_at": payload.get("_meta", {}).get("built_at", ""),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", default="")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--allow-decrease", action="store_true")
    args = parser.parse_args()
    report = refresh(
        _select_workbook(args.workbook),
        Path(args.output).expanduser().resolve(),
        allow_decrease=args.allow_decrease,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
