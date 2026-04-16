# File Ranking Algorithm

Port of `SimpleJudge._prepare_relevant_files` (`simple.py:372-474`) and its
helper `_get_whitelisted_files` (`simple.py:247-336`).

## Inputs

- `task`: the leaf `TaskNode` being graded.
- `task.task_category` ∈ `{Code Development, Code Execution, Result Analysis, Subtree}`.
- `submission_dir`: root of the candidate codebase.
- `paper_md`, `joined_addendum` (addendum + judge_addendum, joined with `\n`;
  defaults to `"(NO ADDENDUM GIVEN)"` when empty).
- `tree_structures[task.task_category]`: precomputed ASCII tree for this
  category (see §Tree Preparation).
- `avail_context_lens[task.task_category]`: token budget for this category.
- `max_files = 10`.

## Steps

### 1. Whitelisted file discovery

Walk `submission_dir` depth-first (bounded by `max_file_depth=4` by default
when budget is tight; unlimited otherwise).

**Blacklisted base dirs** (any path part matching triggers exclusion):
```
venv, .venv, .env, wandb, .egg-info, .git, .github, __pycache__, node_modules
```

**Whitelisted extensions** depend on `task_category`:

| Category | Extensions |
|---|---|
| `Code Development` / `Code Execution` / *(default)* | docs ∪ code |
| `Result Analysis` | docs ∪ tables |
| `Subtree` | docs ∪ code ∪ tables |

Where:
- docs = `{.md, .txt, .rst}`
- code = `{.py, .R, .Rmd, .m, .jl, .c, .h, .cpp, .hpp, .cc, .cxx, .hxx, .java, .js, .ts, .scala, .go, .rs, .sh, .config, .cfg, .json, .yaml, .yml, .toml, .ini}`
- tables = `{.csv, .tsv, .psv, .json, .jsonl, .html, .xml, .yaml, .yml, .toml, .arff, .tex, .svm, .libsvm}`

For `Result Analysis` (and the non-doc files of `Subtree`): a file is included
only if it's a doc **or** its mtime is `>= reproduction_log_creation_time_utc`
(i.e., it was touched by `reproduce.sh`). If *no* non-doc file qualifies for
Result Analysis, set `reproduce_touched_files = False` and all Result Analysis
leaves auto-score 0 (see `simple.py:329-334, 557-567`).

Files with `mtime != mtime` (NaN) are dropped.

### 2. Tree preparation

Build an ASCII tree from the whitelisted relative paths using `├── `, `└── `,
`│   `, `    ` as connectors (`simple.py:220-245`).

Token-budget fallback ladder (`simple.py:352-370`):
1. Try full depth.
2. If too big, try `max_depth=4`.
3. If still too big, truncate the tree string in token space.

### 3. File ranking LLM call

Messages sent:
```
[system]  FILE_RANKING_PROMPT
[user]    "The paper is below:\n<paper>\n{paper_md}\n</paper>"
[user]    "If included with the paper, you will now be shown an addendum which
           provides clarification for the paper and how to evaluate its
           reproduction:\n<addendum>\n{joined_addendum}\n</addendum>"
[user]    "Here is the criterion that you are grading:\n<criterion>\n
           {task.requirements}\n</criterion>"
[user]    "Here are the files in the submission attempt:\n\nDirectory
           structure:\n{tree_structure}\n\nNow return a list of the 10 most
           relevant files in order of relevance (descending) to the resolution
           criteria, to be provided for your inspection. Your response must
           contain each filename separated by newlines, with each file
           containing the full path. Do not write anything else."
```

The phrase `"10 "` is `str(max_files) + " "` when `max_files` is truthy;
omitted when `max_files=None` (`simple.py:410`).

**Expected output:** newline-separated relative filepaths; top-10 taken.

### 4. File content assembly

For each relative path returned (up to `max_files`):

1. Resolve to `submission_dir / rel_path.strip().strip("/")`.
2. Read up to `SIZE_LIMIT_BYTES = 200_000` bytes (utf-8 first, latin-1 fallback).
3. Wrap via `format_file`:
   ```
   <FILE:{rel_path}>
   {content or "(FILE IS EMPTY)"}
   </FILE:{rel_path}>
   ```
4. Tokenize content + `"\n\n"`.
5. Enforce running total ≤ `avail_context_lens[task_category] - 2000` tokens.
   - If adding this file would exceed the budget, truncate in token space and
     break.
6. Silently skip files that are missing, are directories, or fail to decode.

Return: the decoded concatenation, with the final (possibly incomplete) line
dropped via `rsplit("\n", 1)[0]`.

### 5. Claude Code adaptation

The skill's per-leaf driver (in `SKILL.md`) MUST:

1. Build the tree structure (bounded by depth-4 by default).
2. Write the four ranking messages into the leaf's scratch file.
3. **Read them, think about it, and write back the ordered file list** — this
   is Claude replacing `self.completer.async_completion(...)`.
4. Fetch each file up to 200 kB, wrap with `format_file`, cap at
   `available_tokens - 2000` tokens total.

The ranking is NOT skipped when the tree is small. Upstream always calls the
ranker; this skill does the same. If you think "I can eyeball which files
are relevant," don't — run the ranking step. That's the spec.
