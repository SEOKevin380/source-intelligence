#!/usr/bin/env python3
"""Inspect or resume one durable newswire project without WordPress delivery."""

from __future__ import annotations

import argparse
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
        choices=("inspect", "recover", "continue", "rebuild"),
        default="inspect",
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

        action = engine.run_action(
            args.project_id, WORKBENCH_SOURCE_CONTEXT_VERSION
        )
        if action["action"] != "rebuild_corrected_transaction":
            raise RuntimeError(
                "A corrected transaction can only replace an exhausted, "
                "exact-hash-rejected project."
            )
        old = engine.get(args.project_id)
        new_id = engine.create_project_from_pack(
            extract_sealed_pack(old["source_text"]),
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


if __name__ == "__main__":
    main()
