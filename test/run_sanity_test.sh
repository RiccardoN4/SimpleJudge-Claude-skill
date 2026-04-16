#!/usr/bin/env bash
# Sanity test: set up sanity_run/ with symlinks to a real PaperBench 1
# stochastic-interpolants submission, run init, and print status.
#
# The per-leaf LLM steps (ranking + verdict) are NOT automated by this
# script — that is the skill's job in an actual Claude Code session.
# After init, follow the steps in test/test_plan.md to grade each leaf
# then call `finalize`.
#
# Idempotent: re-running is safe; symlinks are refreshed with -sf and
# init's state file is regenerated.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

SRC_BASE="/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/pai-replicator vs paper2code/replication_20260406_123706"
SRC_INPUT="$SRC_BASE/input"
SRC_SUB="$SRC_BASE/code_workspace/stochastic_interpolants"
DST="$ROOT/test/sanity_run"

if [[ ! -d "$SRC_INPUT" ]]; then
    echo "ERROR: $SRC_INPUT not found."
    echo "Please verify the replication_20260406_123706/ directory exists."
    exit 1
fi
if [[ ! -d "$SRC_SUB" ]]; then
    echo "ERROR: $SRC_SUB not found."
    exit 1
fi
if [[ ! -f "$SRC_INPUT/paper.md" ]]; then
    echo "ERROR: $SRC_INPUT/paper.md missing."
    exit 1
fi
if [[ ! -f "$SRC_INPUT/rubric.json" ]]; then
    echo "ERROR: $SRC_INPUT/rubric.json missing."
    exit 1
fi

mkdir -p "$DST"
ln -sf "$SRC_INPUT/rubric.json"  "$DST/rubric.json"
ln -sf "$SRC_INPUT/paper.md"     "$DST/paper.md"
[[ -f "$SRC_INPUT/addendum.md" ]] && ln -sf "$SRC_INPUT/addendum.md" "$DST/addendum.md"
ln -sf "$SRC_SUB" "$DST/submission"

echo "=== sanity_run/ ready ==="
ls -la "$DST"

echo
echo "=== running init with --max-leaves 3 ==="
python3 "$ROOT/scripts/judge_driver.py" init "$DST" --max-leaves 3

echo
echo "=== status ==="
python3 "$ROOT/scripts/judge_driver.py" status "$DST"

echo
echo "Next steps (interactive — performed by Claude in the skill session):"
echo "  1. Open .judge/leaves/<leaf_id>/ranking_prompt.md, write ordered filepaths"
echo "     to .judge/leaves/<leaf_id>/ranked_files.txt, then:"
echo "       python3 scripts/judge_driver.py record-ranking $DST <leaf_id> \\"
echo "         $DST/.judge/leaves/<leaf_id>/ranked_files.txt"
echo "  2. Open .judge/leaves/<leaf_id>/grading_prompt.md, write the verdict"
echo "     to .judge/leaves/<leaf_id>/verdict.md, then:"
echo "       python3 scripts/judge_driver.py record-verdict $DST <leaf_id> \\"
echo "         $DST/.judge/leaves/<leaf_id>/verdict.md --score 0|1 \\"
echo "         --valid-score true --justification '...'"
echo "  3. After all 3 leaves are done:"
echo "       python3 scripts/judge_driver.py finalize $DST"
