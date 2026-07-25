#!/usr/bin/env python3
"""Zero-model-call replay of editorial-truth controls across stored projects."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from article_provenance import extract_sealed_pack
from newswire_workbench import WorkbenchEngine
from newswire_workbench.editorial_truth import audit_editorial_truth


def audit_rows(
    rows: list[dict],
    *,
    project_id: str = "",
    limit: int = 0,
    include_clean: bool = False,
) -> dict:
    if project_id:
        rows = [row for row in rows if row["id"] == project_id]
    rows = [row for row in rows if str(row.get("article_text") or "").strip()]
    if limit > 0:
        rows = rows[:limit]

    projects = []
    rule_counts = Counter()
    cta_issue_counts = Counter()
    failed = 0
    review_candidate_total = 0
    for row in rows:
        pack = extract_sealed_pack(row.get("source_text") or "")
        result = audit_editorial_truth(
            pack,
            row.get("article_text") or "",
            str(
                (pack.get("intake_manifest") or {}).get("affiliate_link")
                or ""
            ),
        )
        grounding = result["grounding_violations"]
        cta = result["cta_integrity_violations"]
        candidates = result["review_candidates"]
        failed += int(not result["passed"])
        review_candidate_total += len(candidates)
        rule_counts.update(item.get("rule") or "unknown" for item in grounding)
        cta_issue_counts.update(item.get("category") or "unknown" for item in cta)
        if result["passed"] and not include_clean:
            continue
        projects.append({
            "project_id": row["id"],
            "title": row.get("title") or "",
            "platform": row.get("platform") or "",
            "vertical": row.get("vertical") or "",
            "stage": row.get("stage") or "",
            "article_hash": row.get("article_hash") or "",
            "passed": result["passed"],
            "grounding_violation_count": len(grounding),
            "cta_violation_count": len(cta),
            "review_candidate_count": len(candidates),
            "grounding_violations": grounding,
            "cta_integrity_violations": cta,
        })

    return {
        "passed": failed == 0,
        "model_calls": 0,
        "projects_with_articles": len(rows),
        "projects_failed": failed,
        "projects_clean": len(rows) - failed,
        "review_candidate_total": review_candidate_total,
        "grounding_rule_counts": dict(sorted(rule_counts.items())),
        "cta_issue_counts": dict(sorted(cta_issue_counts.items())),
        "projects": projects,
    }


def audit_projects(
    engine: WorkbenchEngine,
    *,
    project_id: str = "",
    limit: int = 0,
    include_clean: bool = False,
) -> dict:
    return audit_rows(
        engine.list_projects(),
        project_id=project_id,
        limit=limit,
        include_clean=include_clean,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        help=(
            "Workbench data root. Omit to use the configured production/local "
            "root selected by WorkbenchEngine."
        ),
    )
    parser.add_argument("--project-id", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-clean", action="store_true")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Omit per-project findings from the printed JSON.",
    )
    parser.add_argument(
        "--stdin-json",
        action="store_true",
        help=(
            "Read a JSON list of project rows from stdin instead of opening "
            "the workbench database. This enables read-only pre-deploy replay."
        ),
    )
    parser.add_argument(
        "--fail-on-violations",
        action="store_true",
        help="Exit nonzero when any stored article fails the new controls.",
    )
    args = parser.parse_args()
    if args.stdin_json:
        rows = json.load(sys.stdin)
        if not isinstance(rows, list):
            raise RuntimeError("--stdin-json requires a JSON list.")
        result = audit_rows(
            rows,
            project_id=args.project_id,
            limit=args.limit,
            include_clean=args.include_clean,
        )
    else:
        engine = WorkbenchEngine(
            Path(args.root).resolve() if args.root else None
        )
        result = audit_projects(
            engine,
            project_id=args.project_id,
            limit=args.limit,
            include_clean=args.include_clean,
        )
    if args.summary_only:
        result = {
            key: value
            for key, value in result.items()
            if key != "projects"
        }
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    if args.fail_on_violations and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
