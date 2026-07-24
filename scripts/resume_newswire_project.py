#!/usr/bin/env python3
"""Inspect or resume one durable newswire project without WordPress delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newswire_workbench import WorkbenchEngine


def snapshot(engine: WorkbenchEngine, project_id: str) -> dict:
    project = engine.get(project_id)
    preflight = engine.offline_preflight(project_id)
    return {
        "project_id": project_id,
        "stage": project["stage"],
        "article_hash": project["article_hash"],
        "word_count": engine._article_word_count(project["article_text"]),
        "paid_calls": engine.usage_summary(project_id)["calls"],
        "recoverable": engine.can_recover_locked_pre_signoff(project_id),
        "blockers": [
            {"id": item.get("id"), "issue": item.get("issue")}
            for item in preflight["blockers"]
        ],
        "provenance_passed": preflight["claim_provenance"]["passed"],
        "semantic_review": preflight["semantic_review"],
        "ready_for_packaging": preflight["ready_for_packaging"],
        "publication_ready": preflight["publication_ready"],
        "wordpress_delivery": preflight["wordpress_delivery"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--action",
        choices=(
            "inspect", "recover", "continue", "rebuild", "import", "deliver"
        ),
        default="inspect",
    )
    parser.add_argument(
        "--source-correction",
        help=(
            "Operator-approved JSON correction to merge into the new sealed "
            "pack. Valid only with --action rebuild; never mutates the rejected "
            "project."
        ),
    )
    parser.add_argument(
        "--article-file",
        help=(
            "Publication-ready HTML to import at zero model cost. Valid only "
            "with --action import; all deterministic and provenance gates "
            "still run before final sign-off."
        ),
    )
    args = parser.parse_args()
    engine = WorkbenchEngine()
    print(json.dumps({"before": snapshot(engine, args.project_id)}, indent=2))
    if args.action == "recover":
        recovered = engine._recover_locked_pre_signoff(args.project_id)
        print(json.dumps({
            "recovered": recovered,
            "after": snapshot(engine, args.project_id),
        }, indent=2))
    elif args.action == "continue":
        master_path = (
            Path(__file__).resolve().parents[1]
            / "MBK_Project_Instructions_All_Platforms.txt"
        )
        result = engine.run_to_completion(
            args.project_id,
            master_path.read_text(encoding="utf-8"),
        )
        print(json.dumps({
            "run_stage": result["stage"],
            "after": snapshot(engine, args.project_id),
        }, indent=2))
    elif args.action == "rebuild":
        from article_provenance import extract_sealed_pack
        from newswire_workbench.engine import WORKBENCH_SOURCE_CONTEXT_VERSION
        from source_pack_contract import seal_source_pack

        action = engine.run_action(
            args.project_id, WORKBENCH_SOURCE_CONTEXT_VERSION
        )
        if action["action"] != "rebuild_corrected_transaction":
            raise RuntimeError(
                "A corrected transaction can only replace an exhausted, "
                "exact-hash-rejected project."
            )
        old = engine.get(args.project_id)
        pack = extract_sealed_pack(old["source_text"])
        if args.source_correction:
            correction_path = Path(args.source_correction).resolve()
            correction = json.loads(correction_path.read_text(encoding="utf-8"))
            expected_name = str(correction.get("product_name") or "").strip()
            actual_name = str(
                (pack.get("product") or {}).get("product_name") or ""
            ).strip()
            if not expected_name or expected_name.casefold() != actual_name.casefold():
                raise RuntimeError(
                    "Source correction product identity does not match the "
                    "rejected transaction."
                )
            product_patch = correction.get("product_patch") or {}
            if not isinstance(product_patch, dict) or not product_patch:
                raise RuntimeError("Source correction has no product_patch.")
            pack.setdefault("product", {}).update(product_patch)
            correction_bytes = json.dumps(
                correction, sort_keys=True, ensure_ascii=False
            ).encode("utf-8")
            correction_id = hashlib.sha256(correction_bytes).hexdigest()
            pack.setdefault("all_artifacts", {})[correction_id] = {
                "artifact_type": "structured_data",
                "source_url": "intake://operator-approved-source-correction",
                "source_class": "official_vendor",
                "captured_at": correction.get("approved_at", ""),
                "tls_verified": True,
                "is_usable": True,
                "acquisition_phase": "OPERATOR_SOURCE_CORRECTION",
                "metadata": {
                    "approved_by": correction.get("approved_by", ""),
                    "basis": correction.get("basis", ""),
                    "correction_sha256": correction_id,
                },
            }
            required = pack.setdefault("required_facts", {})
            covered = set(required.get("covered") or [])
            missing = set(required.get("missing") or [])
            for fact in correction.get("covered_facts") or []:
                covered.add(str(fact))
                missing.discard(str(fact))
            required["covered"] = sorted(covered)
            required["missing"] = sorted(missing)
            pack.setdefault("source_corrections", []).append({
                "artifact_id": correction_id,
                "approved_by": correction.get("approved_by", ""),
                "approved_at": correction.get("approved_at", ""),
                "basis": correction.get("basis", ""),
            })
            pack = seal_source_pack(pack)
        new_id = engine.create_project_from_pack(
            pack,
            old["platform"],
            vertical=old["vertical"],
            force_new=True,
        )
        if new_id == args.project_id:
            raise RuntimeError("Corrected transaction reused the rejected project.")
        if engine.usage_summary(new_id)["calls"] != 0:
            raise RuntimeError("Corrected transaction inherited paid-call usage.")
        print(json.dumps({
            "rebuild_action": action,
            "new_project": snapshot(engine, new_id),
        }, indent=2))
    elif args.action == "import":
        if not args.article_file:
            raise RuntimeError("--action import requires --article-file")
        article_path = Path(args.article_file).resolve()
        article = article_path.read_text(encoding="utf-8")
        engine.import_manual_article(args.project_id, article)
        print(json.dumps({
            "imported_from": str(article_path),
            "after": snapshot(engine, args.project_id),
        }, indent=2))
    elif args.action == "deliver":
        result = engine.send_to_wordpress_draft(args.project_id)
        print(json.dumps({
            "wordpress_draft": result,
            "after": snapshot(engine, args.project_id),
        }, indent=2))


if __name__ == "__main__":
    main()
