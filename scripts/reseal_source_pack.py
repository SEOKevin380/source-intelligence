#!/usr/bin/env python3
"""Reseal one stored source pack after a contract-only recovery change."""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from source_pack_contract import seal_source_pack


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--product-key", required=True)
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT id, research_json FROM products WHERE product_key = ?",
        (args.product_key,),
    ).fetchone()
    if not row:
        raise SystemExit(f"Product not found: {args.product_key}")

    pack = seal_source_pack(json.loads(row["research_json"]))
    encoded = json.dumps(pack, ensure_ascii=False, default=str)
    research_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with connection:
        connection.execute(
            """
            UPDATE products
            SET research_json = ?, research_hash = ?
            WHERE id = ?
            """,
            (encoded, research_hash, row["id"]),
        )

    contract = pack["source_pack_contract"]
    summary = pack["publication_claim_summary"]
    print(json.dumps({
        "product_key": args.product_key,
        "readiness": contract["readiness"],
        "readiness_reasons": contract["readiness_reasons"],
        "publication_claim_count": summary["publication_claim_count"],
        "source_pack_hash": contract["sha256"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
