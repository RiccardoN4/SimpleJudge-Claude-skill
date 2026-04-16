# Aggregation Algorithm

Port of `score_from_children` (`graded_task_node.py:145-153`) and the recursion
in `Judge.grade` (`base.py:121-169`).

## Formula

Given a list of graded children:

```python
def score_from_children(children):
    total_weight = sum(child.weight for child in children)
    if total_weight == 0:
        return 0.0
    return sum(child.score * child.weight for child in children) / total_weight
```

- Weights are **local to siblings** — they are normalised against the sum of
  the same parent's children.
- If the sum of sibling weights is 0, the node gets score `0.0` (not NaN).

## Tree walk

Bottom-up (post-order). For each node:

1. If `node.is_leaf()`: keep the score assigned by grading (or by the short-
   circuit path for Result Analysis / exception handler).
2. Else: recurse on children first, then compute
   `node.score = score_from_children(graded_children)`.

Internal-node fields are filled as in `base.py:160-169`:

```python
GradedTaskNode(
    id=task.id,
    requirements=task.requirements,
    weight=task.weight,
    sub_tasks=graded_sub_tasks,
    score=weighted_score,
    valid_score=True,
    explanation="Aggregated score from sub-tasks.",
    judge_metadata=None,
)
```

## Score types

- **Leaf** (non-Subtree): integer `0` or `1`. Upstream emits these as ints in
  JSON (`ParsedJudgeResponseInt.score: int`). When serialising, the skill
  writes them as ints to preserve compatibility.
- **Leaf** (Subtree shim, only when `max_depth` truncation occurs): float in
  `[0, 1]`.
- **Internal node**: float in `[0, 1]` (the weighted average).
- **Failed leaf** (`valid_score=False`): score `0.0`, and counts like any other
  0-scored child in aggregation.

## Root score

The root score equals `score_from_children(root.sub_tasks)` (or the root's own
score if the root is a leaf, which is not expected for real rubrics).

## Validation

`scripts/validate_output.py` verifies:
- Every internal node's score equals `score_from_children(its children)` within
  1e-9 tolerance.
- Every leaf has `valid_score` set and a `score` in `[0, 1]`.
- `root_score` at the top level matches `tree.score`.

## Weights of zero

Zero-weight nodes (e.g., introduced by `resources_provided()` to zero out
"Dataset and Model Acquisition" leaves) contribute nothing to the weighted
average. If ALL siblings have weight 0, the parent node's score is 0.0 per the
formula above, not a divide-by-zero.
