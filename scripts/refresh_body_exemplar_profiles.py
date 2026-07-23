#!/usr/bin/env python3
"""Incrementally fetch approved releases and build fact-safe body profiles."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from body_exemplar_corpus import (  # noqa: E402
    BODY_CORPUS_PATH,
    build_cluster_playbooks,
    extract_article_body,
    heading_role,
    load_body_corpus,
    profile_article_body,
)
from exemplar_corpus import (  # noqa: E402
    infer_niche,
    load_approved_release_index,
    normalize_platform,
)


ALLOWED_HOSTS = {
    "barchart.com", "accessnewswire.com", "accesswire.com",
    "newswire.com", "globenewswire.com",
}
DEFAULT_MBK_DB = Path.home() / "Desktop/Code Projects/mbk-recovery/data/mbk.db"


def _fetch(record: dict, timeout: int = 12) -> tuple[dict, str]:
    host = urlparse(record["live_url"]).netloc.casefold().removeprefix("www.")
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"Unrecognized approved publisher host: {host}")
    response = requests.get(
        record["live_url"],
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/126 Safari/537.36"
            )
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return record, response.text


def _select(records: list[dict], platform: str, niche: str,
            limit_per_cluster: int) -> list[dict]:
    selected = [
        item for item in records
        if (not platform or item["platform"] == platform)
        and (not niche or item["niche"] == niche)
    ]
    grouped = {}
    for item in selected:
        grouped.setdefault((item["platform"], item["niche"]), []).append(item)
    result = []
    for items in grouped.values():
        items.sort(
            key=lambda value: value.get("published_date", ""), reverse=True
        )
        result.extend(items[:limit_per_cluster])
    return result


def _import_mbk_bodies(db_path: Path, profiles: dict[str, dict]) -> int:
    """Reuse bodies already imported by mbk-recovery before any network fetch."""
    if not db_path.exists():
        return 0
    imported = 0
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT url,platform,title,content,product_name,category,
            approved_date,status FROM published_exemplars
            WHERE status='active' AND length(COALESCE(content,'')) > 500"""
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()
    for row in rows:
        url = str(row["url"] or "")
        if not url or url in profiles:
            continue
        platform = normalize_platform(str(row["platform"] or ""), url)
        niche = infer_niche(
            str(row["product_name"] or ""),
            str(row["category"] or ""),
            str(row["title"] or ""),
        )
        try:
            profiles[url] = profile_article_body(
                str(row["content"]),
                url=url,
                platform=platform,
                niche=niche,
                title=str(row["title"] or ""),
                product_name=str(row["product_name"] or ""),
                published_date=str(row["approved_date"] or ""),
            )
            imported += 1
        except ValueError:
            continue
    return imported


def refresh(output: Path, platform: str = "", niche: str = "",
            limit_per_cluster: int = 12, workers: int = 4,
            mbk_db: Path = DEFAULT_MBK_DB, timeout: int = 12) -> dict:
    metadata = {"releases": load_approved_release_index()}
    existing = load_body_corpus(str(output))
    profiles = {
        item["url"]: item for item in existing.get("profiles", [])
    }
    for profile in profiles.values():
        # Migrate early profile builds that retained a factual opening excerpt.
        # The runtime brain needs structure, never historical product prose.
        profile.pop("opening_excerpt", None)
        profile["heading_role_sequence"] = [
            heading_role(value)
            for value in profile.get("heading_sequence", [])
        ]
    local_imported = _import_mbk_bodies(mbk_db, profiles)
    targets = [
        item for item in _select(
            metadata.get("releases", []), platform, niche, limit_per_cluster
        )
        if item["live_url"] not in profiles
    ]
    failures = []
    imported = 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        futures = {
            pool.submit(_fetch, item, timeout): item for item in targets
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                _, raw_html = future.result()
                body = extract_article_body(raw_html, record["platform"])
                profile = profile_article_body(
                    body,
                    url=record["live_url"],
                    platform=record["platform"],
                    niche=record["niche"],
                    title=record.get("title", ""),
                    product_name="",
                    published_date=record.get("published_date", ""),
                )
                profiles[record["live_url"]] = profile
                imported += 1
            except Exception as exc:
                failures.append({
                    "url": record["live_url"],
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                })
    ordered = sorted(
        profiles.values(),
        key=lambda item: (
            item["platform"], item["niche"],
            item.get("published_date", ""), item["url"],
        ),
    )
    payload = {
        "schema_version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "profile_count": len(ordered),
        "profiles": ordered,
        "clusters": build_cluster_playbooks(ordered),
        "last_refresh": {
            "selected": len(targets),
            "imported": imported,
            "local_imported": local_imported,
            "failed": len(failures),
            "failures": failures,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=output.name + ".", suffix=".tmp", dir=output.parent
    )
    os.close(fd)
    try:
        with gzip.open(
            temporary_name, "wt", encoding="utf-8", compresslevel=9
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary_name, output)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return payload["last_refresh"] | {
        "profile_count": len(ordered),
        "cluster_count": len(payload["clusters"]),
        "output": str(output),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=BODY_CORPUS_PATH)
    parser.add_argument("--platform", default="")
    parser.add_argument("--niche", default="")
    parser.add_argument("--limit-per-cluster", type=int, default=12)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--mbk-db", default=str(DEFAULT_MBK_DB))
    parser.add_argument("--timeout", type=int, default=12)
    args = parser.parse_args()
    print(json.dumps(refresh(
        Path(args.output), args.platform, args.niche,
        args.limit_per_cluster, args.workers, Path(args.mbk_db), args.timeout,
    ), indent=2))


if __name__ == "__main__":
    main()
