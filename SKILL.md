---
name: paperbench-judge
description: Byte-for-byte Claude Code port of OpenAI PaperBench SimpleJudge. Grades a reproduction submission against a rubric, using Claude itself as the grading LLM instead of an OpenAI API call. Preserves every prompt, traversal, aggregation rule, and output field; only the LLM backend changes. Trigger when a user asks to "judge a submission with SimpleJudge", "score a reproduction against a rubric", or invokes /paperbench-judge.
---

# paperbench-judge

**You (Claude) are the grader.** This skill is a port of OpenAI PaperBench's
`SimpleJudge`. Every prompt, traversal order, file-ranking cutoff, context
budget, aggregation rule, and output schema is copied verbatim from upstream.
The single substitution is the LLM call: instead of an OpenAI HTTP request, you
read the prompts and write the verdicts.

**The zero-deviation rule overrides your instincts.** If a prompt is verbose,
use it anyway. If the message order looks redundant, keep it. If you want to
merge the grading and extraction steps into one, don't. Upstream reference:

- `reference/simple_judge_port_notes.md` — component-by-component mapping with
  upstream line numbers
- `reference/prompts/*` — verbatim prompt texts
- `reference/algorithms/*` — algorithm descriptions

Read these before Pass 1 on the first invocation. On RESUME, skip to the
current pass.

## Inputs

The skill is invoked with a path to a **working directory** containing:

- `rubric.json` — PaperBench-format rubric (`TaskNode` tree).
- `paper.pdf` **or** `paper.md` — the paper. `paper.md` is preferred. If only
  the PDF exists, Pass 0 extracts it.
- `addendum.md` (optional) — paper addendum.
- `submission/` — the candidate codebase directory. If `reproduce.sh` /
  `reproduce.log` exist, they live under `submission/`.

Optional flags (parsed from the user prompt):

- `--code-only` — prune rubric to `Code Development` leaves and omit
  reproduce.sh/log from grading context.
- `--max-leaves N` — cap the leaf-grading loop at N leaves (testing only; the
  algorithm itself is unchanged).
- `--max-prior-nodes N` — cap the parent-context chain length. Default: unlimited.
- `--max-files N` — top-K for file ranking. Default: 10 (upstream default).

## Outputs (produced in the working directory)

- `grader_output.json` — extended SimpleJudge-compatible report.
- `judge_log.md` — human-readable trace.
- `token_usage.json` — per-leaf and total token-count estimates.
- `.judge/` — internal state & per-leaf prompt bundles (also used for RESUME).

## RESUME protocol

If the user's invocation begins with `RESUME:` **or** `.judge/judge_state.json`
already exists under the working directory, do NOT re-init. Run:

```
python scripts/judge_driver.py status <workdir>
```

and continue from the first non-done leaf. A leaf with status `ranked` means
`record-ranking` already ran — skip straight to the grading step (step 3 of
Pass 1). A `pending` leaf needs the full five-step flow. Do not re-grade leaves
marked `done`.

---

## Pass 0 — Input validation & setup

1. Confirm `<workdir>/rubric.json` and `<workdir>/submission/` exist. If a
   required input is missing, STOP and tell the user.
2. If `paper.md` is absent but `paper.pdf` is present, the driver will extract
   automatically via `scripts/extract_paper_text.py`. You don't need to run it
   yourself.
3. Read `reference/simple_judge_port_notes.md`, and at least skim every file
   under `reference/prompts/`. These are your spec.
4. Run:
   ```
   python scripts/judge_driver.py init <workdir> [--code-only] [--max-leaves N] [--max-prior-nodes N] [--max-files 10]
   ```
   This:
   - validates inputs,
   - builds the filtered directory trees per category,
   - enumerates leaves depth-first,
   - precomputes the `file_ranking` prompt bundle for every leaf under
     `.judge/leaves/<leaf_id>/`,
   - writes `.judge/judge_state.json`.
5. Print the leaf count and proceed to Pass 1.

If `--max-leaves` is set, `init` takes the first N leaves in DFS order.

---

## Pass 1 — Per-leaf grading loop

For each leaf in order (use `status` to list pending leaves), run these five
steps. **Do not batch across leaves.** Write each leaf's outputs before moving
on.

### Step 1 — Read the ranking prompt

```
.judge/leaves/<leaf_id>/ranking_prompt.md
```

This file contains the exact file-ranking messages SimpleJudge sends: the
system prompt `FILE_RANKING_PROMPT`, the paper, the addendum, the criterion,
and the ASCII tree of the submission's whitelisted files (filtered by
`task_category`). Read it carefully.

### Step 2 — Rank files (LLM step 1 — you)

Think about which files most likely bear on this criterion. Then write a
**plain-text, newline-separated** list of relative file paths, in **descending
order of relevance**, to:

```
.judge/leaves/<leaf_id>/ranked_files.txt
```

Exactly the format SimpleJudge's ranking call emits: one path per line, no
prose, no bullets, no numbering. List **every** file from the tree (upstream's
prompt explicitly says "do not leave any out"). The driver will only take the
top `max_files` (default 10), so relevance ordering matters.

Then register the ranking with the driver:

```
python scripts/judge_driver.py record-ranking <workdir> <leaf_id> .judge/leaves/<leaf_id>/ranked_files.txt
```

This triggers two things:
- If the leaf is `Result Analysis` AND `reproduce.sh` touched no files, the
  leaf short-circuits to score=0 with the exact explanation from upstream
  (`simple.py:557-568`). No further LLM call is made; move to the next leaf.
- Otherwise the driver fetches the top-K file contents (200kB/file cap,
  `avail_context_lens[cat] - 2000` token cap total), formats them with
  `<FILE:…>` tags, and assembles the leaf-grading prompt bundle at
  `.judge/leaves/<leaf_id>/grading_prompt.md`.

### Step 3 — Read the grading prompt

```
.judge/leaves/<leaf_id>/grading_prompt.md
```

This is the full message list SimpleJudge sends for leaf grading, in the same
order (`simple.py:513-547`):

1. `system`: `build_judge_task_prompt(code_only)` from `system_prompt.txt`
2. `user`: paper markdown
3. `user`: addendum block
4. `user`: relevant-files block (wording varies for `Result Analysis` —
   upstream `simple.py:480-484`)
5. *(conditional)* `user`: `reproduce.sh`
6. *(conditional)* `user`: `reproduce.log` (not for `Code Development`)
7. `user`: `CRITERION_PROMPT` rendered with preceding criteria chain
8. `user`: `GRADING_PROMPT` (with "either 0 or 1" for binary leaves, "between
   0 and 1" for Subtree leaves)

### Step 4 — Grade the leaf (LLM step 2 — you)

Follow the instructions in the grading prompt **exactly**. Produce a prose
verdict with three headed sections:

```
# Expectations
...what correct resolution looks like per the paper...

# Reality
...what the submission actually shows, citing file tags explicitly...

# Score
<reasoning> <0 or 1>
```

Be strict and thorough in your criterion's scope; do NOT check things outside
that scope. If something is missing from the submission, treat it as a failure
(upstream `GRADING_PROMPT` "Other notes" clause, `constants.py:38-41`).

Write the verdict to:

```
.judge/leaves/<leaf_id>/verdict.md
```

### Step 5 — Extract the score (LLM step 3 — you)

SimpleJudge uses a **separate** structured-output call to parse the prose into
`{valid_score, score, justification}`. You do the same: read your own prose
verdict, then apply the extraction prompt at
`reference/prompts/score_extraction_prompt.txt` with `{score_instruction}`
substituted:

- `(either 0 or 1)` for non-Subtree leaves (the common case)
- `(between 0 and 1)` for Subtree leaves (only produced when `max_depth`
  truncation forces a subtree shim — not used by default)

Extract:

- `score`: integer `0` or `1` (binary) or float in `[0, 1]` (continuous).
  Never boolean, never `0.0`/`1.0` for binary leaves.
- `valid_score`: `true` unless the prose clearly lacks a score.
- `justification`: short 1-3 sentence summary of the reasoning.

Register the verdict:

```
python scripts/judge_driver.py record-verdict <workdir> <leaf_id> \
  .judge/leaves/<leaf_id>/verdict.md \
  --score 0|1 \
  --valid-score true \
  --justification "<short summary>"
```

The driver updates `.judge/judge_state.json` after every verdict so
interruption is safe.

### Leaf-level guards (upstream parity)

- **Result Analysis short-circuit** — handled automatically by
  `record-ranking`; do not grade these leaves if the driver reports a
  short-circuit.
- **Grading exception** — if you get stuck and cannot produce a verdict, call
  `record-verdict` with `--score 0 --valid-score false --justification
  "<reason>"`. Upstream `base.py:146-153` does the equivalent on exception.

### Checkpointing

The driver writes state after every `record-verdict`. If interrupted, resume
by running `status` and continuing from the first non-done leaf.

---

## Pass 2 — Aggregation

After all leaves (or all N for `--max-leaves`) are `done`:

```
python scripts/judge_driver.py finalize <workdir>
```

The finalize subcommand walks the rubric tree bottom-up, applies each leaf's
verdict, and computes every internal node's score via the exact
`score_from_children` formula from `graded_task_node.py:145-153`:

```
total_weight = sum(child.weight for child in children)
score        = 0.0 if total_weight == 0 else
               sum(child.score * child.weight for child in children) / total_weight
```

Leaf scores stay as ints (0 or 1); internal nodes are floats. This matches
upstream exactly.

---

## Pass 3 — Output writing

`finalize` writes:

- `<workdir>/grader_output.json` — extended report per
  `reference/schemas/grader_output_schema.json`. The `simple_judge_compat`
  subsection holds the upstream shape so you can diff against a real SimpleJudge
  run.
- `<workdir>/judge_log.md` — human-readable trace with per-leaf verdicts.
- `<workdir>/token_usage.json` — per-leaf and total token-count estimates.

---

## Pass 4 — Validation

`finalize` automatically runs:

```
python scripts/validate_output.py <workdir>/grader_output.json
```

which checks:

- top-level required fields present;
- every internal node's score = `score_from_children(children)` within 1e-6;
- `root_score`, `tree.score`, and `simple_judge_compat.root_score` agree;
- `simple_judge_compat.per_leaf_scores` matches the tree leaves;
- no NaN/Inf;
- every leaf has a `valid_score` bool and a score in `[0, 1]`.

If validation fails, `finalize` exits non-zero and prints the error list.
STOP and show the user the errors — do not silently ignore.

Finish with a completion banner:

```
=== paperbench-judge complete ===
root_score: <x.xxxx>
leaves graded: <n> / <total>
grader_output.json: <path>
judge_log.md:      <path>
```

---

## Things that will silently break equivalence (re-read before grading)

- **Traversal order** — depth-first pre-order over leaves. Use the order
  printed by `status`; don't shuffle.
- **Parent context** — built by `get_prior_nodes` (ancestors + preceding
  siblings + preceding siblings of ancestors, excluding target). Already
  embedded in the grading prompt by the driver; don't add to it, don't
  summarise it.
- **File content truncation** — 200kB per file, (`avail_context_lens[cat] -
  2000`) tokens total. Done by the driver; don't re-truncate.
- **Top-K** — 10 by default. Don't cut to "just the obviously relevant 2-3
  files" — upstream always considers the top 10.
- **Binary score type** — `0` or `1` **int**, never `0.0`/`1.0`, never
  booleans. The driver enforces this but pass the flag `--score 0` or
  `--score 1` literally.
- **Code-only mode** — if invoked with `--code-only`, the rubric is
  pre-pruned and `reproduce.sh`/`.log` are absent from the grading prompt.
  Do not mention them in your verdict.
- **Missing files / exceptions** — score=0, `valid_score=false`,
  justification = short reason. Upstream `base.py:146-153`.
- **Do NOT retry with different prompts.** Upstream has no ensemble and no
  self-correction loop. One pass per leaf.
- **Do NOT improve the prompts.** If the spec looks clunky, it's still the
  spec.

---

## Quick reference — driver commands

```
python scripts/judge_driver.py init <workdir> [flags]
python scripts/judge_driver.py status <workdir>
python scripts/judge_driver.py record-ranking <workdir> <leaf_id> <ranked_files_path>
python scripts/judge_driver.py record-verdict <workdir> <leaf_id> <prose_path> --score 0|1 [--valid-score true|false] [--justification TEXT]
python scripts/judge_driver.py finalize <workdir>
```

Reference-only:

```
python scripts/aggregate_scores.py <tree.json> [--field tree] [--print-leaves]
python scripts/validate_output.py <grader_output.json>
python scripts/extract_paper_text.py <paper.pdf> <out.md>
```

---

## One last reminder

You are the LLM. Read the prompts the driver prepares, follow them verbatim,
write back your output in the format upstream expects. The only fidelity
hole is the identity of the judge; everything else has to match.
