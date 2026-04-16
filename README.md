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

The pipeline mirrors SimpleJudge's three LLM steps **per leaf**:

1. **File ranking.** Claude picks the top-10 most relevant files from a
   category-filtered tree of the submission.
2. **Prose verdict.** Claude writes an `# Expectations / # Reality / # Score`
   verdict using the top-10 files' contents, the paper, the addendum,
   optionally `reproduce.sh` / `reproduce.log`, the criterion, and the chain
   of preceding rubric criteria.
3. **Score extraction.** Claude parses its own prose into a strict
   `{valid_score, score, justification}` object.

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

In Claude Code, invoke the skill with a path to a working directory that has
the expected layout:

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

Ask Claude something like:

> "Run paperbench-judge on /path/to/workdir."

Or with flags:

> "Run paperbench-judge on /path/to/workdir with --code-only."
> "Run paperbench-judge on /path/to/workdir but only the first 3 leaves
> (testing)."

### Flags

| Flag | Default | Effect |
|---|---|---|
| `--code-only` | off | Prune rubric to `Code Development` leaves; omit `reproduce.sh` / `reproduce.log` blocks from grading context (upstream `tasks.py:338-344` + `simple.py:492-493`). |
| `--max-leaves N` | unlimited | Cap the leaf-grading loop (testing only; does not change the algorithm). |
| `--max-prior-nodes N` | unlimited | Truncate the preceding-criteria chain for each leaf. |
| `--max-files N` | 10 | Top-K used in the file-ranking step (upstream default). |

### Resume

If the skill is interrupted mid-run, re-invoke with `RESUME:` prepended to
your prompt, pointing at the same working directory. The skill reads
`<workdir>/.judge/judge_state.json` and continues from the first pending leaf.
No re-grading is done.

---

## Outputs (written to `<workdir>/`)

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
