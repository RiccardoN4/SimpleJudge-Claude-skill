---
name: paperbench-judge
description: Byte-for-byte Claude Code port of OpenAI PaperBench SimpleJudge. Grades a reproduction submission against a rubric, using Claude itself as the grading LLM instead of an OpenAI API call. Preserves every prompt, traversal, aggregation rule, and output field; only the LLM backend changes. Trigger when a user asks to "judge a submission with SimpleJudge", "score a reproduction against a rubric", or invokes /paperbench-judge.
---

# paperbench-judge

**You (Claude) are the grader.** This skill is a port of OpenAI PaperBench's
`SimpleJudge`. Every prompt, traversal order, file-ranking cutoff, context
budget, aggregation rule, and output schema is copied verbatim from upstream.
The single substitution is the LLM transport: instead of OpenAI HTTP requests,
each of SimpleJudge's three per-leaf LLM calls is performed by you reading a
prepared prompt file and writing an output file. All three calls are preserved.

**The zero-deviation rule overrides your instincts.** If a prompt is verbose,
use it anyway. If the message order looks redundant, keep it. You do NOT merge
grading (Step 1.2) and score extraction (Step 1.3) into a single reasoning
pass — they are separate LLM calls in upstream, and they stay separate here.
Upstream reference:

- `reference/simple_judge_port_notes.md` — component-by-component mapping with
  upstream line numbers
- `reference/prompts/*` — verbatim prompt texts
- `reference/algorithms/*` — algorithm descriptions

Read these before Pass 1 on the first invocation. On RESUME, skip to the
current step.

## The three LLM calls per leaf (know this cold)

Upstream SimpleJudge issues three LLM calls for every graded leaf:

1. **File ranking** (`_prepare_relevant_files`, `simple.py:372-474`) — picks the
   top-10 most relevant files from a category-filtered tree.
2. **Grading** (`_construct_grade_leaf_messages` + `grade_leaf`,
   `simple.py:476-547` + `simple.py:571-582`) — reads the top-10 file contents,
   the paper, the addendum, the optional `reproduce.sh` / `reproduce.log`,
   the criterion with its preceding-criteria chain, and the final `GRADING_PROMPT`.
   Returns prose in `# Expectations / # Reality / # Score` format.
3. **Score extraction** (`_parse_model_response`, `simple.py:666-710`) —
   parses the prose into a strict `{valid_score, score, explanation}` object.

The skill's per-leaf loop (§Pass 1 below) has one sub-step per LLM call,
plus two driver-only plumbing steps around them. Collapsing any of the three
LLM calls into fewer passes is a Zero Deviation Rule violation.

## Inputs

The skill recognises **two** input layouts.

### Canonical layout

Pass a workdir containing:

- `rubric.json` — PaperBench-format rubric (`TaskNode` tree).
- `paper.pdf` or `paper.md` — the paper (`paper.md` preferred; if only the
  PDF exists, Pass 0 extracts it).
- `addendum.md` (optional) — paper addendum.
- `submission/` — the candidate codebase directory. If `reproduce.sh` /
  `reproduce.log` exist, they live under `submission/`.

Outputs (`grader_output.json`, `judge_log.md`, `token_usage.json`, `.judge/`)
are written directly into the workdir.

### pAI-Replicator layout (auto-detected)

When the workdir is a pAI-Replicator replication root, the skill auto-detects
this structure:

```
<workdir>/
├── input/
│   ├── rubric.json          ← used as rubric
│   ├── paper.md or .pdf     ← used as paper (paper.md preferred)
│   └── addendum.md          ← used if present
├── code_workspace/
│   └── <paper_short_name>/  ← used as submission (must be exactly one subdir)
└── ...other pAI-Replicator artifacts (preserved untouched)
```

Detection is automatic. If `<workdir>/input/` and `<workdir>/code_workspace/`
both exist with the right contents, the skill uses them. Outputs are written
into `<workdir>/judge_output/` to keep them separate from pAI-Replicator's
own artifacts (the replication directory stays exactly as pAI-Replicator
left it, plus a new `judge_output/` subdirectory).

To invoke on a pAI-Replicator replication, just pass the replication root:

> "Use paperbench-judge to evaluate this replication, code-only:
> /path/to/replication_<timestamp>/"

If `code_workspace/` is empty or contains 2+ subdirectories, `init` aborts
with a clear error rather than guessing which directory is the submission.

## Flags (parsed from the user prompt)

**Default behaviour: grade all nodes (Code Development, Code Execution,
Result Analysis).** Subtree leaves are only produced when `max_depth`
truncation kicks in, which is off by default. Matches SimpleJudge's default
at `simple.py:80` and `simple.py:81`.

| Flag | Default | Effect |
|---|---|---|
| `--code-only` | **off (grade all categories)** | Prune the rubric to `Code Development` leaves only (upstream `tasks.py:338-344`) AND omit the `reproduce.sh` / `reproduce.log` blocks from the grading prompt (upstream `simple.py:492-493`) AND use the system-prompt variant that omits the `reproduce.sh` clause (upstream `constants.py:9-10`). |
| `--max-leaves N` | unlimited | Cap the leaf-grading loop (testing only; algorithm unchanged). |
| `--max-prior-nodes N` | unlimited | Truncate the preceding-criteria chain (matches `simple.py:82`). |
| `--max-files N` | 10 | Top-K for file ranking (matches `simple.py:375`). |

## Outputs

All four output locations live under the **output_root**, which is:

- the `<workdir>` itself for the canonical layout, and
- `<workdir>/judge_output/` for the pAI-Replicator layout.

Outputs:

- `<output_root>/grader_output.json` — extended SimpleJudge-compatible report.
- `<output_root>/judge_log.md` — human-readable trace.
- `<output_root>/token_usage.json` — per-leaf and total token-count estimates.
- `<output_root>/.judge/` — internal state & per-leaf artefacts (also used
  for RESUME).

### Per-leaf artefacts under `.judge/leaves/<leaf_id>/`

```
leaf.json             — the TaskNode for this leaf (metadata)
ranking_prompt.md     — Step 1.0 input  (written by init)
ranked_files.txt      — Step 1.0 output (written by you in Step 1.0)
grading_context.md    — Step 1.1 output (written by prepare-grading-context)
grading_messages.json — same content as grading_context.md, structured form
files_shown.json      — list of files whose contents were inlined
verdict.md            — Step 1.2 output (written by you in Step 1.2)
score.json            — Step 1.3 output (written by you in Step 1.3)
verdict.json          — Step 1.4 output (written by record-verdict)
```

## RESUME protocol

If the user's invocation begins with `RESUME:`, or if a `judge_state.json`
already exists (under `<workdir>/.judge/` for canonical layout, or under
`<workdir>/judge_output/.judge/` for pAI-Replicator layout), do NOT re-init.
The driver auto-resolves the state location from either place — just pass
the workdir. Run:

```
python scripts/judge_driver.py status <workdir>
```

and continue from the first non-`done` leaf. Status values and their meaning:

- `pending`           — Step 1.0 not yet done; needs file-ranking
- `ranked`            — Step 1.0 done; Step 1.1 (prepare-grading-context) is next
- `context_prepared`  — Step 1.1 done; Step 1.2 (grading LLM call) is next
- `done`              — Step 1.4 done; verdict recorded

Never re-grade leaves marked `done`. The short-circuit cache is honoured
(Result Analysis leaves pre-marked `done` at init time by init).

---

## Pass 0 — Input validation & setup

1. Confirm `<workdir>/rubric.json` and `<workdir>/submission/` exist. If any
   required input is missing, STOP and tell the user.
2. If `paper.md` is absent but `paper.pdf` is present, the driver will extract
   automatically via `scripts/extract_paper_text.py`. You don't need to run it.
3. Read `reference/simple_judge_port_notes.md`, and at least skim every file
   under `reference/prompts/`. These are your spec.
4. Run:
   ```
   python scripts/judge_driver.py init <workdir> [--code-only] \
       [--max-leaves N] [--max-prior-nodes N] [--max-files 10]
   ```
   This:
   - validates inputs,
   - if `--code-only`, prunes the rubric to Code Development leaves
     (pre-pruning exactly as upstream does before SimpleJudge is instantiated),
   - builds the filtered directory trees per category,
   - enumerates leaves in depth-first pre-order,
   - **immediately short-circuits** Result Analysis leaves to score=0 (with
     `valid_score=True`) when `reproduce.sh` touched no files (upstream
     `simple.py:557-568`). These leaves skip all three LLM calls.
   - for every non-short-circuited leaf, precomputes the Step 1.0 file-ranking
     prompt under `.judge/leaves/<leaf_id>/`,
   - writes `.judge/judge_state.json`.

5. Print leaf counts (total, short-circuited, to-grade) and proceed to Pass 1.

---

## Pass 1 — Per-leaf grading loop (five explicit steps)

For each non-`done` leaf in the order printed by `status`, execute ALL FIVE
steps. Do not skip, reorder, batch across leaves, or collapse any pair. The
three LLM calls (Steps 1.0, 1.2, 1.3) and the two driver-plumbing steps
(Steps 1.1, 1.4) together reproduce upstream's `grade_leaf`.

### Step 1.0 — File ranking LLM call  *(LLM call #1)*

Read:
```
.judge/leaves/<leaf_id>/ranking_prompt.md
```

This file is the verbatim set of ranking messages SimpleJudge sends: the
system prompt `FILE_RANKING_PROMPT`, the paper, the addendum, the criterion,
and the ASCII tree of the submission's whitelisted files (filtered by
`task_category`).

Think about which files most likely bear on this criterion. Write a
newline-separated list of relative file paths **in descending order of
relevance** to:

```
.judge/leaves/<leaf_id>/ranked_files.txt
```

One path per line, no prose, no numbering, no bullets — exactly the format
the upstream ranker emits. The driver keeps the top `max_files` (10 default);
relevance ordering matters.

Then:

```
python scripts/judge_driver.py record-ranking <workdir> <leaf_id>
```

(The driver reads `ranked_files.txt` from the standard location. Status
advances from `pending` → `ranked`.)

### Step 1.1 — Prepare grading context  *(driver plumbing, no LLM call)*

```
python scripts/judge_driver.py prepare-grading-context <workdir> <leaf_id>
```

The driver:
1. Reads `ranked_files.txt` (top-10 paths).
2. For each path, reads the file (utf-8 → latin-1 fallback, 200 kB cap per
   file — matches `utils.safe_read_file` and `SIZE_LIMIT_BYTES`).
3. Wraps each file with `<FILE:path>\n{content}\n</FILE:path>` — exact
   `format_file` format from `utils.py:186-189`.
4. Concatenates under the token budget
   `avail_context_lens[category] - 2000` — matches `simple.py:423`.
5. Assembles the 8-message grading prompt exactly as SimpleJudge does
   (`simple.py:513-547`):
   - `[system]` `build_judge_task_prompt(code_only)`
   - `[user]` the paper
   - `[user]` the addendum
   - `[user]` the relevant files block (wording varies for Result Analysis —
     `simple.py:480-484`), with the `<FILE:...>` blocks **inlined**
   - *(if not `code_only`)* `[user]` `reproduce.sh`
   - *(if not `code_only` and not Code Development)* `[user]` `reproduce.log`
   - `[user]` `CRITERION_PROMPT` with preceding-criteria chain
   - `[user]` `GRADING_PROMPT(continuous=...)`
6. Writes `grading_context.md` — the SINGLE file you read in Step 1.2. It
   contains every message in order with all placeholders substituted and
   every `<FILE:...>` block materialized.

Status advances `ranked` → `context_prepared`.

### Step 1.2 — Grading LLM call  *(LLM call #2)*

**Read `.judge/leaves/<leaf_id>/grading_context.md` IN FULL**, including
every `<FILE:...>` block. Do not skim. Do not infer from filenames. If the
file contents don't support a confident verdict, say so in the prose — do
not fabricate observations.

Follow the instructions in the embedded `GRADING_PROMPT` verbatim. Produce a
prose verdict with three headed sections:

```
# Expectations
...what correct resolution looks like per the paper...

# Reality
...what the submission actually shows, citing <FILE:...> blocks explicitly...

# Score
<reasoning that a binary 0/1 — or 0..1 float for Subtree leaves — would be
appropriate, and why>
```

The `# Score` section should contain reasoning in prose. **Do not write a
structured JSON or a clean extracted numeric score here** — that is
Step 1.3's job. The grading LLM in upstream produces natural prose which
the extraction LLM then parses; you do the same.

Write to:

```
.judge/leaves/<leaf_id>/verdict.md
```

### Step 1.3 — Score extraction LLM call  *(LLM call #3)*

This is a **separate** reasoning pass. Treat it as if a different model were
running: you have the prose verdict (the input) and the extraction prompt
(the instruction) — nothing else. Do not re-read the grading context; do not
revise the prose. Extract only.

Read:
1. `.judge/leaves/<leaf_id>/verdict.md` (Step 1.2 output)
2. `reference/prompts/score_extraction_prompt.txt` — the extraction system
   prompt. Substitute `{score_instruction}` with:
   - `(either 0 or 1)` for non-Subtree leaves (the common case)
   - `(between 0 and 1)` for Subtree leaves (only reached via `max_depth`
     truncation — not used by default)

Apply the extraction prompt to the prose verdict. Produce a strict JSON
object and write it to:

```
.judge/leaves/<leaf_id>/score.json
```

Shape (matches `ParsedJudgeResponseInt` / `ParsedJudgeResponseFloat` —
`simple.py:44-53`):

```json
{
  "valid_score": true,
  "score": 0,
  "explanation": "short 1-3 sentence summary of the judge's reasoning"
}
```

Rules:
- Non-Subtree leaf: `score` is an **integer** `0` or `1`. Never boolean,
  never `0.0`/`1.0`. If the prose doesn't clearly support a score, set
  `valid_score: false` and `score: 0`.
- Subtree leaf: `score` is a **float** in `[0.0, 1.0]`.
- `valid_score: false` forces the final recorded score to `0.0` (matches
  upstream `simple.py:690-694` + `base.py:142-153`).

### Step 1.4 — Record the verdict  *(driver plumbing, no LLM call)*

```
python scripts/judge_driver.py record-verdict <workdir> <leaf_id>
```

The driver:
- reads `score.json` (MANDATORY — fails loudly if missing),
- reads `verdict.md` (for the `prose_verdict` field),
- if `valid_score` is `false` OR the score is outside `[0, 1]`, falls back
  to `score=0.0, valid_score=False` (upstream `simple.py:690-694` / `base.py:142-153`),
- writes `verdict.json`,
- updates token-usage accounting and `judge_state.json`.

No CLI `--score` flag exists. The driver refuses scores unless they come from
`score.json`. This enforces the three-LLM-call separation.

Status advances `context_prepared` → `done`.

### Leaf-level guards (upstream parity)

- **Result Analysis short-circuit** — handled at `init` time. These leaves
  are already `done` when Pass 1 starts and need no action from you.
- **Grading failure** — if at any step you cannot produce a usable output
  (e.g., score.json parsing genuinely fails), write `score.json` with
  `valid_score: false, score: 0` and a short `explanation`. The driver
  records it as a failed leaf (`score=0.0, valid_score=False`), matching
  upstream's exception path at `base.py:142-153`.

---

## Pass 2 — Aggregation

After all non-done leaves are `done`:

```
python scripts/judge_driver.py finalize <workdir>
```

This walks the rubric tree bottom-up and computes every internal node's score
via the exact `score_from_children` formula from `graded_task_node.py:145-153`:

```
total_weight = sum(child.weight for child in children)
score        = 0.0 if total_weight == 0 else
               sum(child.score * child.weight for child in children) / total_weight
```

Leaf scores stay ints (0 or 1) for non-Subtree leaves; internal nodes are
floats.

---

## Pass 3 — Output writing

`finalize` writes:

- `<workdir>/grader_output.json` — extended report per
  `reference/schemas/grader_output_schema.json`. The `simple_judge_compat`
  subsection holds the upstream shape for byte-for-byte diffs with a real
  SimpleJudge run.
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
STOP and show the errors — do not ignore silently.

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
- **Parent context** — built by `get_prior_nodes` and embedded in the
  grading prompt by the driver. Don't add to it, don't summarise it.
- **File content truncation** — 200 kB per file, total ≤
  `avail_context_lens[cat] - 2000` tokens. The driver does this; don't
  re-truncate.
- **Top-K** — 10 by default. Don't cut to 2-3 "obviously relevant" files.
- **Binary score type** — integer `0` or `1` in `score.json`, never booleans,
  never `0.0/1.0`. The driver enforces this but your output has to be correct.
- **Code-only mode** — `--code-only` prunes the rubric and strips
  reproduce.sh/log from the prompt. Do not mention them in your verdict.
- **Do NOT fuse Steps 1.2 and 1.3.** They are two separate LLM calls in
  upstream. Writing the score into `verdict.md` and then copying it to
  `score.json` is a deviation even if the final numbers happen to match —
  don't do it. Reason about the prose in Step 1.2 without writing a clean
  score; reason about the prose-as-input in Step 1.3 to extract the score.
- **Missing files / exceptions** — `score=0.0, valid_score=false` via
  `score.json`. Do not fall through silently.
- **Do NOT retry with different prompts** (no ensembling, no self-correction).
- **Do NOT improve the prompts.**

---

## Quick reference — driver commands

```
python scripts/judge_driver.py init <workdir> [--code-only] [flags]
python scripts/judge_driver.py status <workdir>

# Per-leaf loop (all five steps per leaf, in order):
# Step 1.0: you write ranked_files.txt, then:
python scripts/judge_driver.py record-ranking <workdir> <leaf_id>

# Step 1.1:
python scripts/judge_driver.py prepare-grading-context <workdir> <leaf_id>

# Step 1.2: you read grading_context.md, write verdict.md  (no driver call)
# Step 1.3: you read verdict.md + score_extraction_prompt.txt, write score.json  (no driver call)

# Step 1.4:
python scripts/judge_driver.py record-verdict <workdir> <leaf_id>

python scripts/judge_driver.py finalize <workdir>
```

Reference-only helpers:

```
python scripts/aggregate_scores.py <tree.json> [--field tree] [--print-leaves]
python scripts/validate_output.py <grader_output.json>
python scripts/extract_paper_text.py <paper.pdf> <out.md>
```

---

## One last reminder

You are the LLM — three times per leaf. Read each prepared prompt in full,
follow it verbatim, write the output in the format upstream expects. The
only fidelity hole is LLM transport; everything else has to match.
