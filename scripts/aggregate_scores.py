#!/usr/bin/env python3
"""Weighted bottom-up score aggregation, matching SimpleJudge's
`score_from_children` exactly.

Can be invoked standalone on a tree JSON (either the extended
grader_output.json or a bare GradedTaskNode tree dict), OR imported
as a module by the skill driver.

Usage (CLI):
    python aggregate_scores.py <tree.json>

    If the JSON is a full grader_output.json, pass --field tree to read
    the `tree` subkey:
        python aggregate_scores.py grader_output.json --field tree
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def score_from_children(children: list[dict[str, Any]]) -> float:
    """Port of graded_task_node.py:145-153."""
    total_weight = sum(child["weight"] for child in children)
    if total_weight == 0:
        return 0.0
    return sum(child["score"] * child["weight"] for child in children) / total_weight


def aggregate(node: dict[str, Any]) -> dict[str, Any]:
    """Bottom-up aggregation. Leaves keep their score; internals get weighted avg.

    The returned dict has the same shape as the input; only scores on internal
    nodes and their `explanation` / `valid_score` fields are updated.
    """
    sub_tasks = node.get("sub_tasks", []) or []
    if not sub_tasks:
        # Leaf: keep as-is, but coerce score to a number
        score = node.get("score", 0)
        if isinstance(score, bool):
            score = int(score)
        node = dict(node)
        node["score"] = float(score) if isinstance(score, float) else score
        return node

    graded_children = [aggregate(c) for c in sub_tasks]
    weighted = score_from_children(graded_children)

    updated = dict(node)
    updated["sub_tasks"] = graded_children
    updated["score"] = weighted
    # Only set these if the caller didn't already set them; upstream mirrors base.py:160-169.
    updated.setdefault("valid_score", True)
    if not node.get("explanation"):
        updated["explanation"] = "Aggregated score from sub-tasks."
    # judge_metadata should be null for internal nodes (base.py:168)
    if "judge_metadata" not in updated:
        updated["judge_metadata"] = None
    return updated


def collect_leaf_scores(node: dict[str, Any], out: dict[str, float]) -> None:
    sub_tasks = node.get("sub_tasks", []) or []
    if not sub_tasks:
        out[node["id"]] = node["score"]
        return
    for c in sub_tasks:
        collect_leaf_scores(c, out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path", type=Path)
    ap.add_argument("--field", default=None,
                    help="If the JSON is a full grader_output.json, read this subkey as the tree (e.g. 'tree').")
    ap.add_argument("--print-leaves", action="store_true",
                    help="Also print the flat leaf_id -> score map.")
    args = ap.parse_args()

    data = json.loads(args.json_path.read_text())
    tree = data[args.field] if args.field else data

    aggregated = aggregate(tree)
    print(f"root_score = {aggregated['score']:.6f}")

    if args.print_leaves:
        leaves: dict[str, float] = {}
        collect_leaf_scores(aggregated, leaves)
        print(json.dumps(leaves, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    sys.exit(main())
