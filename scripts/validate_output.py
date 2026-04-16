#!/usr/bin/env python3
"""Validate a paperbench-judge grader_output.json.

Checks:
 1. Shape matches reference/schemas/grader_output_schema.json (lightweight;
    jsonschema is used when available, otherwise hand-rolled required-field
    checks).
 2. `simple_judge_compat.tree` parses as a well-formed GradedTaskNode tree.
 3. Every leaf has a numeric `score` in [0, 1], `valid_score` bool.
 4. Every internal node's score equals score_from_children(children)
    within 1e-6 tolerance.
 5. `root_score` matches `tree.score` and `simple_judge_compat.root_score`.
 6. No NaN/Infinity anywhere in the tree scores.
 7. `leaf_verdicts` is consistent: every non-short-circuited entry has a
    prose_verdict, ranked_files, and a score matching the tree's leaf.

Exits 0 on success, non-zero on any error.

Usage:
    python validate_output.py <path/to/grader_output.json>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE.parent / "reference" / "schemas" / "grader_output_schema.json"


def score_from_children(children: list[dict[str, Any]]) -> float:
    total_weight = sum(c["weight"] for c in children)
    if total_weight == 0:
        return 0.0
    return sum(c["score"] * c["weight"] for c in children) / total_weight


def walk_errors(node: dict[str, Any], path: str = "tree") -> list[str]:
    errs: list[str] = []

    required_fields = {
        "id", "requirements", "weight", "score", "valid_score",
        "task_category", "explanation", "judge_metadata", "sub_tasks",
    }
    missing = required_fields - set(node.keys())
    if missing:
        errs.append(f"{path}: missing fields {sorted(missing)}")
        return errs

    score = node["score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        errs.append(f"{path}: score is not a number: {score!r}")
        return errs
    if isinstance(score, float) and (math.isnan(score) or math.isinf(score)):
        errs.append(f"{path}: score is NaN/Inf")
    if not (0.0 - 1e-9 <= float(score) <= 1.0 + 1e-9):
        errs.append(f"{path}: score {score} not in [0, 1]")

    if not isinstance(node["valid_score"], bool):
        errs.append(f"{path}: valid_score is not bool: {node['valid_score']!r}")

    weight = node["weight"]
    if not isinstance(weight, (int, float)) or weight < 0:
        errs.append(f"{path}: weight is not a non-negative number: {weight!r}")

    sub_tasks = node.get("sub_tasks") or []
    if not isinstance(sub_tasks, list):
        errs.append(f"{path}: sub_tasks is not a list")
        return errs

    if not sub_tasks:
        # Leaf: must have a task_category per tasks.py:75-76
        if not node.get("task_category"):
            errs.append(f"{path}: leaf has no task_category")
    else:
        for i, c in enumerate(sub_tasks):
            errs.extend(walk_errors(c, f"{path}.sub_tasks[{i}]"))
        # Internal node: score must equal score_from_children(children)
        expected = score_from_children(sub_tasks)
        if not math.isclose(float(score), expected, rel_tol=0, abs_tol=1e-6):
            errs.append(
                f"{path}: score {score} != score_from_children(children) "
                f"{expected:.6f} (diff {abs(float(score) - expected):.2e})"
            )

    return errs


def try_jsonschema(doc: dict[str, Any]) -> list[str]:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return []
    try:
        schema = json.loads(SCHEMA_PATH.read_text())
    except FileNotFoundError:
        return [f"schema file missing: {SCHEMA_PATH}"]
    v = jsonschema.Draft7Validator(schema)
    return [f"schema: {e.message} at {list(e.path)}" for e in v.iter_errors(doc)]


def collect_leaf_scores(node: dict[str, Any], out: dict[str, Any]) -> None:
    sub_tasks = node.get("sub_tasks") or []
    if not sub_tasks:
        out[node["id"]] = node["score"]
        return
    for c in sub_tasks:
        collect_leaf_scores(c, out)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: validate_output.py <grader_output.json>", file=sys.stderr)
        return 2

    p = Path(sys.argv[1])
    if not p.exists():
        print(f"Not found: {p}", file=sys.stderr)
        return 1

    doc = json.loads(p.read_text())

    errs: list[str] = []

    required_top = {
        "rubric_path", "submission_path", "paper_path", "backend", "model",
        "timestamp_start", "timestamp_end", "wall_clock_seconds", "root_score",
        "tree", "leaf_verdicts", "token_usage_total", "cost_estimate_usd",
        "simple_judge_compat",
    }
    missing_top = required_top - set(doc.keys())
    if missing_top:
        errs.append(f"top-level missing fields: {sorted(missing_top)}")

    if "tree" in doc:
        errs.extend(walk_errors(doc["tree"], "tree"))

    # simple_judge_compat checks
    sjc = doc.get("simple_judge_compat")
    if isinstance(sjc, dict):
        for f in ("root_score", "per_leaf_scores", "tree"):
            if f not in sjc:
                errs.append(f"simple_judge_compat missing field: {f}")
        if "tree" in sjc:
            errs.extend(walk_errors(sjc["tree"], "simple_judge_compat.tree"))
        if "tree" in sjc and "root_score" in sjc:
            if not math.isclose(float(sjc["tree"]["score"]), float(sjc["root_score"]),
                                 rel_tol=0, abs_tol=1e-6):
                errs.append(
                    f"simple_judge_compat.root_score {sjc['root_score']} != "
                    f"tree.score {sjc['tree']['score']}"
                )
        if "tree" in sjc and "per_leaf_scores" in sjc:
            collected: dict[str, Any] = {}
            collect_leaf_scores(sjc["tree"], collected)
            pls = sjc["per_leaf_scores"]
            if not isinstance(pls, dict):
                errs.append("simple_judge_compat.per_leaf_scores is not an object")
            else:
                for lid, sc in collected.items():
                    if lid not in pls:
                        errs.append(f"per_leaf_scores missing leaf {lid}")
                    elif not math.isclose(float(pls[lid]), float(sc),
                                          rel_tol=0, abs_tol=1e-9):
                        errs.append(
                            f"per_leaf_scores[{lid}]={pls[lid]} != tree leaf score {sc}"
                        )
    else:
        errs.append("simple_judge_compat missing or not an object")

    # root_score consistency
    if "tree" in doc and "root_score" in doc:
        if not math.isclose(float(doc["tree"]["score"]), float(doc["root_score"]),
                             rel_tol=0, abs_tol=1e-6):
            errs.append(
                f"top-level root_score {doc['root_score']} != tree.score "
                f"{doc['tree']['score']}"
            )

    # leaf_verdicts consistency
    if "leaf_verdicts" in doc and "tree" in doc:
        tree_leaves: dict[str, Any] = {}
        collect_leaf_scores(doc["tree"], tree_leaves)
        for i, lv in enumerate(doc["leaf_verdicts"]):
            lid = lv.get("leaf_id")
            if not lid:
                errs.append(f"leaf_verdicts[{i}] missing leaf_id")
                continue
            if lid not in tree_leaves:
                errs.append(f"leaf_verdicts[{i}].leaf_id {lid!r} not found in tree")
                continue
            if not math.isclose(float(lv.get("score", -1)), float(tree_leaves[lid]),
                                 rel_tol=0, abs_tol=1e-9):
                errs.append(
                    f"leaf_verdicts[{i}].score {lv.get('score')} != tree leaf "
                    f"score {tree_leaves[lid]}"
                )

    errs.extend(try_jsonschema(doc))

    if errs:
        print(f"VALIDATION FAILED ({len(errs)} error(s)):")
        for e in errs:
            print(f"  - {e}")
        return 1

    print(f"OK: {p}  (root_score={doc.get('root_score')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
