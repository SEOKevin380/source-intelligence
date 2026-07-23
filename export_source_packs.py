"""Export sealed Source Intelligence CRM records for publishing automation."""

import argparse
import json
from pathlib import Path

from database import ProductDatabase
from source_pack_contract import seal_source_pack, validate_source_pack


def export_pack(product_key, output_dir, db_path=None):
    db = ProductDatabase(db_path=db_path)
    record = db.get_product(product_key)
    if not record or not record.get("research_data"):
        raise ValueError(f"No completed source pack found for '{product_key}'")
    pack = seal_source_pack(record["research_data"])
    validate_source_pack(pack)
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{product_key}_source_pack.json"
    destination.write_text(
        json.dumps(pack, indent=2, default=str), encoding="utf-8"
    )
    return destination


def main():
    parser = argparse.ArgumentParser(
        description="Export a verified publication source pack from the CRM"
    )
    parser.add_argument("product_key")
    parser.add_argument(
        "--output-dir",
        default="~/master-publisher-data/source-packs",
    )
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    path = export_pack(args.product_key, args.output_dir, args.db)
    print(path)


if __name__ == "__main__":
    main()
