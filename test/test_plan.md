# Sanity test — paperbench-judge

End-to-end smoke test of the full skill pipeline on a real PaperBench 1
submission. Confirms the pipeline (init → per-leaf grading → finalize →
validation) runs without crashing and produces a `grader_output.json` of the
correct shape. Only 3 leaves are graded to keep total time under a few
minutes; the algorithm itself is unchanged.

## Inputs used

All taken from:

```
/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/pai-replicator vs paper2code/replication_20260406_123706/
```

| Skill input | Source path inside `replication_20260406_123706/` |
|---|---|
| `paper.md` | `input/paper.md` — 64.7 kB markdown of *Stochastic Interpolants with Data-Dependent Couplings* (Albergo et al. 2024) |
| `addendum.md` | `input/addendum.md` — 707-byte PaperBench 1 addendum (batch size, grad steps, HuggingFace ImageNet download) |
| `rubric.json` | `input/rubric.json` — 58.5 kB original PaperBench 1 rubric (69 leaves: 58 Code Development, 7 Code Execution, 4 Result Analysis) |
| `submission/` | `code_workspace/stochastic_interpolants/` — the generated candidate codebase (README, baselines, configs, docs, requirements, results, scripts, src, tests) |

Confirmed present in `input/`: `paper.md`, `paper.pdf`, `addendum.md`,
`rubric.json`, `blacklist.txt`. No `reproduce.sh` / `reproduce.log` are
present (the top-level `code_workspace/stochastic_interpolants/` has no such
files either), so the skill correctly treats them as `(Does not exist)`.

## Test command

Start in the repo root, then:

```bash
# 1. Build the test workdir (symlinks, idempotent).
bash test/run_sanity_test.sh
```

Or step-by-step:

```bash
# Build test workdir (symlinks)
SRC_INPUT="/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/pai-replicator vs paper2code/replication_20260406_123706/input"
SRC_SUB="/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/pai-replicator vs paper2code/replication_20260406_123706/code_workspace/stochastic_interpolants"
DST=test/sanity_run
mkdir -p "$DST"
ln -sf "$SRC_INPUT/rubric.json"  "$DST/rubric.json"
ln -sf "$SRC_INPUT/paper.md"     "$DST/paper.md"
ln -sf "$SRC_INPUT/addendum.md"  "$DST/addendum.md"
ln -sf "$SRC_SUB"                "$DST/submission"

# Init — enumerates leaves, builds per-leaf ranking prompts
python scripts/judge_driver.py init test/sanity_run --max-leaves 3

# For each leaf: Claude writes the ranked_files list, then the prose verdict,
# then record-ranking and record-verdict are called.
python scripts/judge_driver.py status test/sanity_run   # lists pending leaves

# (Claude writes .judge/leaves/<leaf_id>/ranked_files.txt, then:)
python scripts/judge_driver.py record-ranking test/sanity_run <leaf_id> .judge/leaves/<leaf_id>/ranked_files.txt

# (Claude writes .judge/leaves/<leaf_id>/verdict.md in # Expectations / # Reality / # Score form, then:)
python scripts/judge_driver.py record-verdict test/sanity_run <leaf_id> .judge/leaves/<leaf_id>/verdict.md --score 0|1 --valid-score true --justification "..."

# Aggregate + write grader_output.json + validate
python scripts/judge_driver.py finalize test/sanity_run

# Standalone validation (also auto-run by finalize)
python scripts/validate_output.py test/sanity_run/grader_output.json
```

## Leaves graded in this run

DFS pre-order from the root of `rubric.json`:

| # | leaf id (first 8) | category | requirement (truncated) | score |
|---|---|---|---|---|
| 1 | `fa71af11` | Code Development | U-Net from lucidrains' denoising-diffusion-pytorch | 1 |
| 2 | `46ca1cd4` | Code Development | ImageNet train/val access | 1 |
| 3 | `e04c1c97` | Code Development | 8×8 = 64 tile mask with p=0.3, per sample per minibatch | 1 |

All three are subtrees of "U-Net and ImageNet are available" (leaves 1-2)
and "The Dependent Coupling model for in-painting has been implemented →
Training → During training the Dependent Coupling model for in-painting,
for each i-th sample in each mini-batch, the mask is constructed → ..." (leaf 3).

## Results

Obtained by running `finalize`:

- `grader_output.json` written (size ≈ rubric-shaped)
- `validate_output.py` exits 0: **OK**
- `root_score`: **0.0916**
- `simple_judge_compat.root_score`: **0.0916** (matches)
- `leaf_verdicts`: 69 entries (3 graded, 66 marked `valid_score=false,
  score=0, short_circuit_reason="not_graded"` by `--max-leaves 3`)
- `token_usage_total.leaves_graded`: 3
- `cost_estimate_usd`: `null` (expected — Claude Code is subscription-based)

The `root_score ≈ 0.0916` is the expected consequence of only grading 3 of
69 leaves with `--max-leaves 3`; the remaining leaves are zero-scored
because they were not graded. The rubric weighting pushes three top-of-tree
Code-Development leaves to account for ~9% of the root score, which is
consistent with hand-computation:

- leaf `fa71af11` has weight 1 inside a parent of weight 2
- leaf `46ca1cd4` has weight 1 inside the same parent of weight 2
- leaf `e04c1c97` has weight 2 inside a deep subtree

Their weighted contribution, after bubbling up through the rubric's
weight-normalised aggregation, is ~0.09 — matches the output.

## What this test does NOT cover

- **Full-rubric grading.** 66 leaves are skipped by `--max-leaves 3`.
- **LLM judgement agreement with SimpleJudge's `o3-mini`.** The whole point
  of the skill is to substitute a different model; per-leaf agreement is
  not expected to be byte-identical.
- **`reproduce.sh` execution paths** (this submission has none).
- **`code_only` mode** (not exercised here).
- **RESUME protocol** (not exercised here; state file exists and works
  after interruption based on code inspection, but not formally tested).

## Re-running

`test/sanity_run/` can be deleted and rebuilt idempotently from the symlink
commands above. The `.judge/` state can also be removed in isolation to
re-run just the grading loop without moving the inputs.
