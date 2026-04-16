# paperbench-judge

A Claude Code **skill** that reproduces OpenAI PaperBench's `SimpleJudge`
byte-for-byte, substituting the OpenAI `o3-mini` HTTP call with Claude itself
as the grading LLM.

> **Zero-deviation rule.** Every prompt, traversal order, file-ranking cutoff,
> context-budget formula, and output field is copied verbatim from upstream.
> The only thing that changes is the identity of the LLM.

Upstream source (ported from):

```
/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/frontier-evals/project/paperbench/paperbench/judge/simple.py
/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/frontier-evals/project/paperbench/paperbench/judge/constants.py
/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/frontier-evals/project/paperbench/paperbench/judge/base.py
/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/frontier-evals/project/paperbench/paperbench/judge/graded_task_node.py
/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/frontier-evals/project/paperbench/paperbench/judge/utils.py
/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/frontier-evals/project/paperbench/paperbench/judge/token_usage.py
/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/frontier-evals/project/paperbench/paperbench/rubric/tasks.py
```

See `reference/simple_judge_port_notes.md` for a component-by-component mapping
with upstream line numbers.

---

## What this skill does

Given a rubric (`rubric.json`), a paper (`paper.pdf` or `paper.md`), optional
`addendum.md`, and a candidate reproduction (`submission/` directory), the
skill grades every leaf of the rubric against the submission and aggregates
the scores bottom-up into a root score in `[0, 1]`.

The pipeline mirrors SimpleJudge's three LLM steps **per leaf**, plus two
driver-plumbing steps around them:

1. **Step 1.0 — File ranking** *(LLM call #1)*. Claude picks the top-10 most
   relevant files from a category-filtered tree of the submission.
2. **Step 1.1 — Prepare grading context** *(driver plumbing)*. Driver reads
   the top-10 files (200 kB/file cap, token-budget capped total), wraps each
   one with `<FILE:path>...</FILE:path>` exactly as SimpleJudge does, and
   assembles the full 8-message grading prompt into a single
   `grading_context.md` file that Claude reads.
3. **Step 1.2 — Prose verdict** *(LLM call #2)*. Claude writes an
   `# Expectations / # Reality / # Score` prose verdict grounded in the
   `<FILE:...>` blocks. No structured score yet.
4. **Step 1.3 — Score extraction** *(LLM call #3)*. Claude applies the
   verbatim `score_extraction_prompt.txt` to the prose and writes a strict
   `{valid_score: bool, score: int|float, explanation: str}` object to
   `score.json`. This is a **separate** reasoning pass — upstream uses a
   different structured-output LLM for it and so does the skill.
5. **Step 1.4 — Record verdict** *(driver plumbing)*. Driver reads
   `score.json`, applies the `valid_score=false → score=0.0` fallback
   matching `simple.py:690-694`, writes `verdict.json`.

Result Analysis leaves are **short-circuited at init time** when
`reproduce.sh` touched no files (all three LLM calls skipped, verdict
canned per `simple.py:557-568`).

Scores are then aggregated via the exact upstream formula:

```python
score_from_children(children) = (
    0.0
    if sum(c.weight for c in children) == 0
    else sum(c.score * c.weight for c in children) / sum(c.weight for c in children)
)
```

---

## Invocation

The skill recognises **two** input layouts.

### Canonical layout

Pass a workdir containing:

```
<workdir>/
├── rubric.json
├── paper.pdf              # or paper.md (preferred)
├── addendum.md            # optional
└── submission/
    ├── ...your reproduction...
    ├── reproduce.sh       # optional; only used if not --code-only
    └── reproduce.log      # optional
```

Outputs (`grader_output.json`, `judge_log.md`, `token_usage.json`, `.judge/`)
are written directly into the workdir.

### pAI-Replicator layout (auto-detected)

When the workdir is a pAI-Replicator
([pAI-Replicator-claude](https://github.com/PoggioAI/pAI-Replicator-claude))
replication root, the skill auto-detects this structure:

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

Detection is automatic — there is no `--pai-replicator-mode` flag. If
`<workdir>/input/` and `<workdir>/code_workspace/` both exist with the right
contents, the skill uses them and writes ALL outputs into a NEW
`<workdir>/judge_output/` directory:

```
<workdir>/judge_output/
├── grader_output.json
├── judge_log.md
├── token_usage.json
└── .judge/
```

This keeps the replication's own artifacts (`analysis_workspace/`,
`verification_workspace/`, `persona_workspace/`, etc.) untouched. A user
looking at the replication directory sees a clear separation:
pAI-Replicator generated everything except `judge_output/`, which
paperbench-judge generated.

If `code_workspace/` is empty or contains 2+ subdirectories, `init` aborts
with a clear error rather than guessing.

### Invocation examples

> "Run paperbench-judge on /path/to/workdir."

> "Run paperbench-judge on /path/to/workdir with --code-only."

> "Use paperbench-judge to evaluate this replication, code-only:
> /path/to/replication_20260406_123706/"

> "Run paperbench-judge on /path/to/workdir but only the first 3 leaves
> (testing)."

### Flags

> **Default behavior: grade all nodes (Code Development, Code Execution,
> Result Analysis, Subtree).**
> Pass `--code-only` at init time to restrict grading to Code Development
> leaves only, matching SimpleJudge's `code_only=True` mode.

| Flag | Default | Effect |
|---|---|---|
| `--code-only` | **off (grade all categories)** | Prune rubric to `Code Development` leaves only AND omit `reproduce.sh` / `reproduce.log` blocks from the grading prompt AND use the system-prompt variant that drops the reproduce-script clause. Upstream: `tasks.py:338-344`, `simple.py:492-493`, `constants.py:9-10`. |
| `--max-leaves N` | unlimited | Cap the leaf-grading loop (testing only; does not change the algorithm). |
| `--max-prior-nodes N` | unlimited | Truncate the preceding-criteria chain for each leaf. |
| `--max-files N` | 10 | Top-K used in the file-ranking step (upstream default). |

### Resume

If the skill is interrupted mid-run, re-invoke with `RESUME:` prepended to
your prompt, pointing at the same working directory. The skill auto-finds
the state file at either:

- `<workdir>/.judge/judge_state.json`              (canonical layout)
- `<workdir>/judge_output/.judge/judge_state.json` (pAI-Replicator layout)

and continues from the first non-done leaf. No re-grading is done.

---

## Outputs (written to `<output_root>/`)

The `<output_root>` depends on the layout:

- canonical layout — `<output_root>` is `<workdir>`
- pAI-Replicator layout — `<output_root>` is `<workdir>/judge_output/`

Files written:

- `grader_output.json` — the extended report (see below).
- `judge_log.md` — human-readable trace of every leaf's verdict.
- `token_usage.json` — estimated per-leaf and total token counts.
- `.judge/` — internal state, per-leaf prompt bundles, and ranked-file lists.
  Used for RESUME; safe to delete after a successful run.

### `grader_output.json` — shape

```jsonc
{
  "rubric_path": "...",
  "submission_path": "...",
  "paper_path": "...",
  "backend": "claude-code-skill",
  "model": "claude (via Claude Code)",
  "code_only": false,
  "timestamp_start": "ISO8601",
  "timestamp_end":   "ISO8601",
  "wall_clock_seconds": 0.0,
  "root_score": 0.0,
  "tree": { /* recursive graded node — extended */ },
  "leaf_verdicts": [
    {
      "leaf_id": "...",
      "requirement": "...",
      "task_category": "Code Development",
      "score": 0,
      "valid_score": true,
      "justification": "...",
      "ranked_files": ["...", "..."],
      "files_shown_to_grader": ["...", "..."],
      "prose_verdict": "# Expectations ... # Reality ... # Score ...",
      "tokens_input_est": 0,
      "tokens_output_est": 0,
      "grading_seconds": 0.0
    }
  ],
  "token_usage_total": {
    "input_tokens_est": 0,
    "output_tokens_est": 0,
    "leaves_graded": 0
  },
  "cost_estimate_usd": null,
  "simple_judge_compat": {
    "root_score": 0.0,
    "per_leaf_scores": { "leaf-id": 0, "...": 1 },
    "tree": { /* recursive node matching upstream to_dict exactly */ }
  }
}
```

### `simple_judge_compat` — the diff-friendly subsection

`simple_judge_compat.tree` mirrors the upstream
`GradedTaskNode.to_dict()` output (`graded_task_node.py:48-60`) *exactly*:
only the fields `id, requirements, weight, score, valid_score, task_category,
explanation, judge_metadata, sub_tasks`. If you run native SimpleJudge on the
same inputs, you can `jq` this subsection and diff against upstream's
`grader_output.json` to measure agreement.

---

## Known limitations

- **No cost tracking.** Claude Code subscription usage is not per-call
  billable. `cost_estimate_usd` is always `null`.
- **Token counts are estimates.** If `tiktoken` is available (with the
  `o200k_base` encoding, matching upstream for `o3-mini`), we use it for
  truncation decisions. Otherwise we fall back to `len(text) // 4`. This is
  the only intentional fidelity compromise; it is documented in
  `reference/simple_judge_port_notes.md` §11.
- **No concurrency.** Upstream grades leaves in parallel under an
  `asyncio.Semaphore(100)`. The Claude-driven port is sequential by
  construction; final outputs are identical because traversal order and
  aggregation are deterministic.
- **No `ComputerInterface`.** Submissions are read from the local filesystem
  only. Upstream's optional Alcatraz sandbox path is not ported.
- **Judgement agreement is not byte-identical.** Claude and `o3-mini` are
  different models; they will sometimes disagree on individual leaf
  verdicts. Every prompt, traversal rule, and aggregation rule IS identical —
  any remaining disagreement is a property of the LLM, not the harness. This
  is intentional (the whole point of the skill is that you're swapping the
  model).

---

## Repository layout

```
paperbench2-judge/
├── SKILL.md                     # Claude Code skill entrypoint (5 passes)
├── README.md                    # this file
├── reference/
│   ├── simple_judge_port_notes.md
│   ├── prompts/                 # verbatim prompt files
│   ├── schemas/                 # JSON Schemas for outputs
│   └── algorithms/              # file-ranking & aggregation specs
├── scripts/
│   ├── judge_driver.py          # main plumbing (init/record/finalize)
│   ├── extract_paper_text.py    # PDF → text fallback
│   ├── aggregate_scores.py      # standalone weighted aggregation
│   └── validate_output.py       # grader_output.json validator
└── test/
    ├── test_plan.md             # sanity-test procedure
    └── run_sanity_test.sh       # smoke test
```

---

## Installing as a Claude Code skill

This directory *is* the skill — drop it anywhere Claude Code scans for skills
and the `paperbench-judge` name will appear in the skills list. The skill's
instructions live in `SKILL.md`; the frontmatter block at the top of that
file is the metadata Claude Code reads.

Alternatively, invoke it ad-hoc by asking Claude to read `SKILL.md` and run
the pipeline on your working directory.
