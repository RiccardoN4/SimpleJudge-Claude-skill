# SimpleJudge Port Notes

This document maps every component of OpenAI's PaperBench `SimpleJudge` to its
counterpart in the `paperbench-judge` Claude Code skill. Upstream references are
given as `path:line` relative to:

```
/Users/riccardoneumarker/Desktop/ETH/TESI/esperimenti/frontier-evals/project/paperbench/
```

**Zero-deviation rule**: except for the LLM backend (Claude Code replacing the
OpenAI `o3-mini` HTTP calls), every prompt, schema, algorithm, and output shape
must match upstream byte-for-byte. When in doubt, re-read `simple.py`.

---

## 1. Source files ported

| Upstream file | Role |
|---|---|
| `paperbench/judge/simple.py` | `SimpleJudge` class — grading orchestration |
| `paperbench/judge/constants.py` | verbatim prompt templates |
| `paperbench/judge/base.py` | `Judge` base class, tree traversal, `grade()` |
| `paperbench/judge/graded_task_node.py` | `GradedTaskNode`, `score_from_children` aggregation |
| `paperbench/judge/utils.py` | file walking, formatting, log reduction |
| `paperbench/judge/token_usage.py` | per-leaf token bookkeeping |
| `paperbench/rubric/tasks.py` | `TaskNode`, `TASK_CATEGORY_QUESTIONS`, `get_prior_nodes` |

---

## 2. Prompts — verbatim copies

All prompts live in `reference/prompts/`. They are copied **character-for-character**
from the upstream `constants.py`, including placeholders. The skill runner must
fill placeholders using `str.format()`-style substitution — never paraphrase.

| Prompt file | Upstream source | Substitutions |
|---|---|---|
| `system_prompt.txt` | `constants.py:1-16` (`build_judge_task_prompt`) | `{code_only_clause}` — the optional sentence about `reproduce.sh` when `code_only=False` |
| `file_ranking_prompt.txt` | `constants.py:56-60` (`FILE_RANKING_PROMPT`) | none (system-role prompt; context is sent in separate messages) |
| `grading_prompt.txt` | `constants.py:19-41` (`GRADING_PROMPT`) | `{score_instruction}` — binary vs continuous sentence |
| `criterion_prompt.txt` | `constants.py:44-54` (`CRITERION_PROMPT`) | `{preceding_criteria}`, `{criterion}`, `{task_category}`, `{task_category_question}` |
| `score_extraction_prompt.txt` | `simple.py:679` (inline system prompt in `_parse_model_response`) | `{score_instruction}` — `(either 0 or 1)` or `(between 0 and 1)` |

The exact `score_instruction` values:
- Binary (Code Development / Code Execution / Result Analysis leaves): `"Give a score of either 0 or 1 depending on whether the criteria has been met, and explain why."`
- Continuous (Subtree leaves, only reached when `max_depth` truncates the tree): `"Give a score between 0 and 1 depending on how well the criteria has been met, and explain why."`

The extraction-prompt variants mirror upstream `simple.py:675`:
- Binary: `(either 0 or 1)`
- Continuous: `(between 0 and 1)`

---

## 3. Per-leaf message ordering (exact)

Upstream `simple.py:513-547` (`_construct_grade_leaf_messages`) builds the
grading LLM's messages in this exact order. The skill MUST feed Claude Code
the same context in the same order. In the skill, the driver's
`prepare-grading-context` subcommand (Step 1.1) materialises all eight
messages verbatim into a single `grading_context.md` file under
`.judge/leaves/<leaf_id>/`, substituting every placeholder (paper text,
addendum, `<FILE:...>` blocks, preceding-criteria chain, reproduce.sh/log,
criterion, grading instruction) before Claude reads it in Step 1.2.

The eight messages:

1. **system**: `build_judge_task_prompt(code_only)` — from `system_prompt.txt`
2. **user**: `"The paper is below:\n{paper_md}"`
3. **user**: `"If included with the paper, you will now be shown an addendum which provides clarification for the paper and how to evaluate its reproduction:\n<addendum>\n{joined_addendum}\n</addendum>"`
4. **user**: relevant-files block — see §4. The `<FILE:...>...</FILE:...>`
   blocks are **inlined** into this message by `prepare-grading-context`
   during Step 1.1; they are NOT a separate attachment. This materialization
   is exactly what forces grading to be grounded in file contents rather
   than filename inference.
5. *(conditional)* **user**: `"Here is the `reproduce.sh` provided in the submission, if any:\n<reproduce.sh>\n{reproduce_sh_content}\n</reproduce.sh>"`
   - Included when `code_only=False`.
   - For `task_category == "Code Development"` **only the sh** is included.
6. *(conditional)* **user**: `"Here is the `reproduce.log` provided in the submission, if any:\n<reproduce.log>\n{reproduce_log_content}\n</reproduce.log>"`
   - Included when `code_only=False` **and** `task_category != "Code Development"` (i.e., Code Execution / Result Analysis / Subtree).
7. **user**: `CRITERION_PROMPT.format(preceding_criteria=..., criterion=..., task_category=..., task_category_question=...)`
8. **user**: `GRADING_PROMPT(continuous=(task.task_category == "Subtree"))`

Notes:
- Message 4 text differs for Result Analysis: `simple.py:480-484`. See `reference/algorithms/file_ranking.md`.
- Placeholders use Python-style `{name}` braces in the prompt files for clarity; `prepare-grading-context` substitutes them. Claude reads the already-substituted `grading_context.md` in Step 1.2.
- The grading-LLM response (Step 1.2 output, `verdict.md`) is then fed to the **separate** score-extraction LLM call (Step 1.3) along with the verbatim `score_extraction_prompt.txt` system message. The extraction-LLM response is `score.json` — matching `ParsedJudgeResponseInt` / `ParsedJudgeResponseFloat`. This is a second, independent structured-output LLM call, exactly as upstream `simple.py:666-710` does.

---

## 4. File ranking algorithm

Upstream: `simple.py:372-474` (`_prepare_relevant_files`).

Faithfully captured in `reference/algorithms/file_ranking.md`:

- **`max_files = 10`** — top-K selected files (`simple.py:375`).
- Whitelisted extensions depend on `task_category` (`simple.py:258-282`):
  - `Code Development`, `Code Execution`: docs + code
  - `Result Analysis`: docs + tables
  - `Subtree`: docs + code + tables
- Blacklisted base dirs: `{venv, .venv, .env, wandb, .egg-info, .git, .github, __pycache__, node_modules}` (`simple.py:258-261`).
- For `Result Analysis`: non-doc files must have mtime `>= reproduction_log_creation_time_utc` (touched by reproduce.sh). If none touched, the `reproduce_touched_files` flag flips to False and all Result Analysis leaves auto-score 0.
- Tree structure built via `_create_tree_structure` (`simple.py:220-245`) — ASCII tree with `├──`, `└──`, `│   `, `    `.
- File ranking LLM call sees: `FILE_RANKING_PROMPT` (system) + paper + addendum + criterion + tree.
- Response: newline-separated filepaths; top-`max_files` taken.
- Each selected file is wrapped via `format_file` (`utils.py:186-189`):
  ```
  <FILE:{rel_path}>
  {content or "(FILE IS EMPTY)"}
  </FILE:{rel_path}>
  ```
- Per-file size cap: **`SIZE_LIMIT_BYTES = 200_000`** bytes (`utils.py:20`).
- Total file content truncated to `avail_context_lens[task_category] - 2000` tokens (`simple.py:422-424`).
- Missing files, directories, or unreadable files are skipped silently (logged).

### Skill adaptation

The skill runs in a local filesystem (no `ComputerInterface`). We use
`safe_read_file` semantics: utf-8 first, latin-1 fallback, 200k byte cap per
file. Token truncation is simulated with a character-based heuristic — the
limit becomes `(available_tokens - 2000) * 4` characters — unless `tiktoken` is
available, in which case we use the exact `o200k_base` encoding as upstream
does via `completer.encoding_name`. This is the **only** intentional fidelity
compromise, and it's documented in `README.md`.

---

## 5. Parent context — `get_prior_nodes`

Upstream: `tasks.py:221-274`.

`relevant_rubric_context` is built at `simple.py:486-489`:

```python
relevant_rubric_nodes = task.get_prior_nodes(self.rubric, self.max_prior_nodes)
relevant_rubric_context = ""
for node in relevant_rubric_nodes:
    relevant_rubric_context += f" -> {node.requirements}\n"
```

`get_prior_nodes` returns, in order:
- All ancestors of the target, starting with root.
- Preceding siblings of the target.
- Preceding siblings of every ancestor.
- Excludes the target node itself.
- Excludes subsequent siblings (anything that comes after).

If `max_prior_nodes` is provided, only the **last** `max_prior_nodes` entries
are kept (the most-recent context wins).

The skill defaults to `max_prior_nodes=None` (unlimited) to match SimpleJudge's
default behaviour at `simple.py:82` and `simple.py:115`.

---

## 6. Aggregation

Upstream: `graded_task_node.py:145-153` (`score_from_children`) and
`base.py:155-169` (`grade` recursion).

```python
def score_from_children(children):
    total_weight = sum(child.weight for child in children)
    if total_weight == 0:
        return 0.0
    return sum(child.score * child.weight for child in children) / total_weight
```

- Weights are interpreted **locally among siblings** (normalised by their sum).
- Zero total weight → `0.0` score (not NaN).
- Leaf scores are integers (0 or 1) for standard leaves; floats (0..1) for
  Subtree shim leaves. When aggregated upward they become floats.
- Internal-node `explanation` is fixed to `"Aggregated score from sub-tasks."`
  (`base.py:167`).
- `valid_score` on an internal node is `True` unless overridden (`base.py:166`).

See `reference/algorithms/aggregation.md` and `scripts/aggregate_scores.py`.

---

## 7. Traversal order

Upstream: `base.py:121-169` (`grade`). Leaves are graded concurrently via
`asyncio.gather` in reality, but for a deterministic Claude-driven port the
skill walks the tree **depth-first pre-order** (via `TaskNode.get_leaf_nodes`,
`tasks.py:295-301`). This yields the exact same set of leaves in the same
order regardless of whether we parallelise, so output order in `leaf_verdicts`
is deterministic.

---

## 8. Binary vs continuous semantics

Upstream: `simple.py:44-53` (`ParsedJudgeResponseFloat`, `ParsedJudgeResponseInt`).

- Non-Subtree leaves: **integer** score, strictly `0` or `1`. Skill outputs
  `0` or `1` (int type), not `0.0`/`1.0`.
- Subtree leaves (only produced when `max_depth` forces subtree
  approximation via `grade_subtree`, `simple.py:644-664`): **float** score in
  `[0, 1]`.
- Internal-node (aggregated) scores: **float** in `[0, 1]`.

---

## 9. Failure handling

Upstream: `base.py:121-153` wraps each leaf in try/except. On exception:
- `GradedTaskNode.from_task(..., score=0.0, valid_score=False, explanation=str(e), judge_metadata=None)`

Upstream: `simple.py:557-568` Result Analysis short-circuit. If
`reproduce_touched_files` is False:
- `GradedTaskNode.from_task(..., score=0, valid_score=True, explanation="Reproduce.sh did not touch any files, so there are no reproduced results to analyze.", judge_metadata=None)`

Both behaviours are preserved in `SKILL.md` Pass 1 §Leaf-level guards.

---

## 10. Category filtering (code-only mode)

Upstream: `tasks.py:338-344` (`code_only`) and `simple.py:492-511`.

When `code_only=True`:
- `reproduce.sh` and `reproduce.log` blocks are NOT included in leaf messages
  (`simple.py:492-493`).
- The system prompt omits the `reproduce.sh` sentence (`constants.py:9-10`).
- The rubric itself is typically pre-pruned to Code Development leaves via
  `TaskNode.code_only()` before instantiating `SimpleJudge`.

The skill supports a `--code-only` flag that performs both transforms.

---

## 11. Token counting

Upstream: `simple.py:100` uses `tiktoken.get_encoding(completer.encoding_name)`
(varies by model; for `o3-mini` this is `o200k_base`). It is the ground truth
for all truncation decisions.

The skill:
- Tries `tiktoken` with `o200k_base` if installed (matches upstream exactly
  when grading against o3-mini-equivalent context budgets).
- Falls back to `len(text) // 4` character-based estimate otherwise.

Token usage in `grader_output.json` is **estimated**, never billed (Claude Code
usage is subscription-based, not per-call). The `cost_estimate_usd` field is
always `null` to reflect this.

---

## 12. Output format — `grader_output.json`

Upstream `SimpleJudge` writes a `GradedTaskNode` tree as JSON via `to_dict()`
(`graded_task_node.py:48-60`). Our `grader_output.json` wraps that shape inside
a `simple_judge_compat` subsection so byte-for-byte diffs with upstream are
possible, and adds extended fields (timing, token estimates, ranked files,
prose verdicts) outside that subsection. See `reference/schemas/grader_output_schema.json`.

---

## 13. Defaults lifted verbatim

| Default | Value | Upstream |
|---|---|---|
| `max_files` (top-K for ranking) | `10` | `simple.py:375` |
| `buffer_tokens` | `10000` | `simple.py:79` |
| Context buffer after file-fill | `2000` tokens | `simple.py:423` |
| `max_file_depth` | `4` | `simple.py:83` |
| `max_depth` (tree grading) | `999` | `simple.py:80` |
| `max_prior_nodes` | `None` (unlimited) | `simple.py:82` |
| Per-file byte cap | `200_000` | `utils.py:20` |
| Leaf concurrency | 100 (`asyncio.Semaphore`) | `simple.py:114` (irrelevant for sequential skill) |
| Log truncation | half of context window | `simple.py:192` |

---

## 14. The one deviation

The ONLY deviation is that each of SimpleJudge's three LLM calls
(file ranking, grading, score extraction) is performed by Claude Code
reading a prepared prompt file and writing an output file, rather than
by an HTTP call to OpenAI. **All three calls are preserved.** All prompts,
message ordering, truncation, aggregation, traversal, output schema, and
failure handling are byte-for-byte upstream.

Mapping of the three calls to the skill's per-leaf steps:

| # | Upstream call | Upstream site | Skill input (read by Claude) | Skill output (written by Claude) |
|---|---|---|---|---|
| 1 | File ranking | `simple.py:372-474` (`_prepare_relevant_files`) | `ranking_prompt.md` | `ranked_files.txt` |
| 2 | Grading | `simple.py:476-547` + `simple.py:571-582` (`_construct_grade_leaf_messages` + `grade_leaf`) | `grading_context.md` (assembled by `prepare-grading-context`, contains all 8 messages and `<FILE:...>` contents inline) | `verdict.md` (prose only) |
| 3 | Score extraction | `simple.py:666-710` (`_parse_model_response`) | `verdict.md` + `reference/prompts/score_extraction_prompt.txt` | `score.json` (`ParsedJudgeResponseInt` / `Float` shape) |

Driver plumbing around the three calls:

- `init` — validates inputs, prunes rubric under `--code-only`, enumerates
  leaves in depth-first pre-order, short-circuits Result Analysis leaves
  per `simple.py:557-568`, precomputes file-ranking prompts.
- `record-ranking` — accepts the Step 1.0 output, advances status.
- `prepare-grading-context` — materialises the 8-message grading prompt
  with file contents inlined (the piece that made Step 2 possible).
- `record-verdict` — reads `score.json` (MANDATORY; no CLI-flag score path)
  and applies the `valid_score=false → score=0.0` fallback from
  `simple.py:690-694` + `base.py:142-153`.
- `finalize` — bottom-up weighted aggregation per `graded_task_node.py:145-153`,
  writes `grader_output.json` (with `simple_judge_compat` subsection for
  byte-for-byte diffs), runs `validate_output.py`.

What is NOT a deviation (all of these match upstream exactly):

- The eight-message ordering of the grading prompt.
- The top-K=10 file-ranking cutoff, whitelisted-extension sets, blacklisted
  base dirs, 200 kB per-file byte cap, `avail_context_lens[cat] - 2000`
  token budget.
- `format_file` wrapping with `<FILE:{path}>...</FILE:{path}>`.
- `get_prior_nodes` parent-context chain.
- `score_from_children` weighted aggregation (integer leaves, float
  internals, zero-total-weight → 0.0).
- Result Analysis auto-zero when `reproduce.sh` touched no files.
- Binary int vs continuous float score typing by leaf category.
- Failed-leaf handling (score=0.0, valid_score=False).
- `grader_output.json` shape (tree + `simple_judge_compat.{root_score,
  per_leaf_scores, tree}`).
- pAI-Replicator layout auto-detection is a convenience for input resolution
  only. It changes WHERE the driver reads paper/rubric/submission from, but
  does not change any prompt, message ordering, traversal, aggregation,
  output schema, or grading logic. SimpleJudge fidelity is unchanged.
