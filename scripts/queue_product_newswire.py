#!/usr/bin/env python3
"""Reseal one CRM product and queue its durable newswire transaction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import ProductDatabase
from newswire_workbench import (
    WORKBENCH_RUNTIME_REVISION,
    WORKBENCH_SOURCE_CONTEXT_VERSION,
    WorkbenchEngine,
)
from newswire_workbench.run_worker import submit_project_run
from source_pack_contract import seal_source_pack, validate_source_pack


PLATFORMS = (
    "AccessNewsWire",
    "Barchart Advertorial",
    "Globe Newswire",
    "Newswire.com",
)


def queue_product(
    product_key: str,
    platform: str,
    *,
    db_path: str | None = None,
    force_new: bool = False,
) -> dict:
    """Migrate, persist, and queue one exact CRM source pack."""
    db = ProductDatabase(db_path=db_path)
    record = db.get_product(product_key)
    if not record or not record.get("research_data"):
        raise ValueError(
            f"No completed source pack found for '{product_key}'"
        )
    pack = seal_source_pack(record["research_data"])
    validate_source_pack(pack, allow_limited=True)
    db.upsert_product(product_key, pack)

    engine = WorkbenchEngine()
    project_id = engine.create_project_from_pack(
        pack,
        platform,
        vertical="auto",
        force_new=force_new,
    )
    project = engine.get(project_id)
    job, created = submit_project_run(
        engine,
        project_id,
        idempotency_key=(
            f"operator-queue:{product_key}:{platform}:"
            f"{WORKBENCH_SOURCE_CONTEXT_VERSION}:{project_id}"
        ),
    )
    manifest = pack.get("intake_manifest") or {}
    return {
        "product_key": product_key,
        "platform": platform,
        "source_pack_hash": (
            (pack.get("source_pack_contract") or {}).get("sha256") or ""
        ),
        "source_pack_readiness": (
            (pack.get("source_pack_contract") or {}).get("readiness") or ""
        ),
        "contact_information": manifest.get("contact_information") or {},
        "refund_terms": manifest.get("refund_terms") or "",
        "workflow_version": WORKBENCH_SOURCE_CONTEXT_VERSION,
        "runtime_revision": WORKBENCH_RUNTIME_REVISION,
        "project_id": project_id,
        "project_stage": project["stage"],
        "project_paid_calls": engine.usage_summary(project_id)["calls"],
        "queue_job_id": job.id,
        "queue_job_status": job.status,
        "queue_job_created": created,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-key", required=True)
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--db", default=None)
    parser.add_argument(
        "--force-new",
        action="store_true",
        help="Create a fresh immutable transaction for the resealed pack.",
    )
    args = parser.parse_args()
    print(json.dumps(
        queue_product(
            args.product_key,
            args.platform,
            db_path=args.db,
            force_new=args.force_new,
        ),
        indent=2,
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
