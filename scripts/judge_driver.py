#!/usr/bin/env python3
"""paperbench-judge driver.

This is the mechanical plumbing for the SimpleJudge port: walking
the submission directory, building the category-filtered tree
structure, assembling leaf prompts, running aggregation, writing
the output JSON. The LLM calls themselves are performed by Claude
Code (the skill runner) reading the prepared prompts and writing
back prose verdicts — see SKILL.md for that half of the loop.

Subcommands:

  init <workdir> [--code-only] [--max-prior-nodes N] [--max-files 10]
      [--max-leaves N]
      Validate inputs, enumerate leaves, write judge_state.json
      and per-leaf prompt bundles under workdir/.judge/.

  prepare-leaf <workdir> <leaf_id>
      Build the file-ranking prompt for a single leaf.
      (Already called by init for every leaf; this is for re-prep.)

  record-ranking <workdir> <leaf_id> <ranked_files_path>
      Ingest Claude's ranked file list, fetch file contents within
      the token budget, assemble the leaf-grading prompt bundle.

  record-verdict <workdir> <leaf_id> <prose_path> --score 0|1 [--valid-score true]
      [--justification TEXT] [--short-circuit REASON]
      Ingest Claude's prose verdict + extracted score. Updates the
      in-memory graded tree and writes a checkpoint.

  finalize <workdir>
      Aggregate scores, write grader_output.json, judge_log.md,
      token_usage.json. Validate output shape.

  status <workdir>
      Print what's done and what's pending.

See reference/simple_judge_port_notes.md for the algorithmic contract.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROMPTS = ROOT / "reference" / "prompts"

# ---------------------------------------------------------------------------
# SimpleJudge defaults (simple.py:67-119)
# ---------------------------------------------------------------------------

DEFAULT_MAX_FILES = 10              # simple.py:375
DEFAULT_BUFFER_TOKENS = 10_000      # simple.py:79
DEFAULT_FILE_CONTENT_BUFFER = 2_000  # simple.py:423
DEFAULT_MAX_FILE_DEPTH = 4          # simple.py:83
SIZE_LIMIT_BYTES = 200_000          # utils.py:20

# Context window budget (tokens). SimpleJudge uses the completer's n_ctx.
# For the port we default to a generous 200k-token window (Claude); the
# truncation formula still matches upstream shape.
DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000

# Whitelisted extensions (simple.py:258-273).
WHITELISTED_DOCS = {".md", ".txt", ".rst"}
WHITELISTED_CODE = {
    ".py", ".R", ".Rmd", ".m", ".jl",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx",
    ".java", ".js", ".ts", ".scala", ".go", ".rs",
    ".sh",
    ".config", ".cfg", ".json", ".yaml", ".yml", ".toml", ".ini",
}
WHITELISTED_TABLES = {
    ".csv", ".tsv", ".psv", ".json", ".jsonl", ".html", ".xml", ".yaml", ".yml",
    ".toml", ".arff", ".tex", ".svm", ".libsvm",
}
BLACKLISTED_BASE_DIRS = {
    "venv", ".venv", ".env", "wandb", ".egg-info", ".git", ".github",
    "__pycache__", "node_modules",
}

TASK_CATEGORY_QUESTIONS = {
    "Code Development": (
        "Does the code in the submission contain a correct implementation of this?"
        " Note: mere instructions or descriptions of implementations are not sufficient;"
        " you should check whether there is actual code that implements the requirement."
    ),
    "Code Execution": "Does running the reproduce.sh script lead to this being successfully executed?",
    "Result Analysis": "Did the reproduce.sh script execution produce evidence that agrees with these results?",
    "Subtree": "What is the weighted score of all the criteria in the subtree?",
}
TASK_CATEGORY_DEFAULT_QUESTION = "Does the submission satisfy this criterion?"


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def _get_token_encoder():
    """Return a token encoder matching o200k_base (SimpleJudge default for o-series),
    or None if tiktoken is unavailable."""
    try:
        import tiktoken
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


_ENCODER = _get_token_encoder()


def count_tokens(text: str) -> int:
    if _ENCODER is not None:
        return len(_ENCODER.encode(text, disallowed_special=()))
    # Fallback: SimpleJudge-compatible rough estimate (~4 chars/token).
    return max(1, len(text) // 4)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    if _ENCODER is not None:
        tokens = _ENCODER.encode(text, disallowed_special=())
        if len(tokens) <= max_tokens:
            return text
        return _ENCODER.decode(tokens[:max_tokens])
    # Char-based fallback
    max_chars = max_tokens * 4
    return text if len(text) <= max_chars else text[:max_chars]


# ---------------------------------------------------------------------------
# File utilities (ports of paperbench.judge.utils)
# ---------------------------------------------------------------------------

def safe_read_file(path: Path, max_bytes: int = SIZE_LIMIT_BYTES) -> str:
    """Port of utils.py:23-35."""
    try:
        with path.open(encoding="utf-8") as f:
            return f.read(max_bytes)
    except UnicodeDecodeError:
        with path.open(encoding="latin1") as f:
            return f.read(max_bytes)


def format_file(rel_path: Path, content: str) -> str:
    """Port of utils.py:186-189."""
    body = content if content.strip() else "(FILE IS EMPTY)"
    return f"<FILE:{rel_path}>\n{body}\n</FILE:{rel_path}>"


def walk_with_mtimes(dir_path: Path):
    """Port of utils.py:68-81."""
    for root, dirs, files in os.walk(dir_path):
        mtimes: list[float] = []
        for f in files:
            fp = os.path.join(root, f)
            try:
                mtimes.append(os.stat(fp).st_mtime)
            except OSError:
                mtimes.append(0.0)
        yield root, dirs, files, mtimes


# ---------------------------------------------------------------------------
# File discovery + tree building (simple.py:247-370)
# ---------------------------------------------------------------------------

def get_whitelisted_files(
    submission_dir: Path,
    task_category: str,
    max_file_depth: Optional[int] = None,
    reproduction_log_creation_time: Optional[dt.datetime] = None,
) -> tuple[list[Path], bool]:
    """Returns (files_sorted, reproduce_touched_files_flag).

    Port of simple.py:247-336. reproduce_touched_files is True by default; it
    flips to False only for Result Analysis when no non-doc file was touched.
    """
    if task_category == "Result Analysis":
        exts = WHITELISTED_DOCS | WHITELISTED_TABLES
    elif task_category == "Subtree":
        exts = WHITELISTED_DOCS | WHITELISTED_CODE | WHITELISTED_TABLES
    else:
        exts = WHITELISTED_DOCS | WHITELISTED_CODE

    files: list[Path] = []
    mtimes: list[float] = []

    submission_dir_resolved = submission_dir.resolve()

    for root, dirs, fs, ms in walk_with_mtimes(submission_dir):
        root_path = Path(root).resolve()
        # depth limit
        try:
            current_depth = len(root_path.relative_to(submission_dir_resolved).parts)
        except ValueError:
            current_depth = 0
        if max_file_depth is not None and current_depth >= max_file_depth:
            dirs[:] = []
        # blacklist: any path part matching a blacklisted base dir triggers skip
        if any(b in part for b in BLACKLISTED_BASE_DIRS for part in root_path.parts):
            continue
        for f, mtime in zip(fs, ms):
            full = Path(root) / f
            if full.suffix not in exts:
                continue
            # mtime NaN guard
            if mtime != mtime:
                continue
            if task_category == "Result Analysis":
                file_time = dt.datetime.fromtimestamp(mtime, tz=dt.timezone.utc)
                if (full.suffix not in WHITELISTED_DOCS
                        and reproduction_log_creation_time is not None
                        and file_time < reproduction_log_creation_time):
                    continue
            elif task_category == "Subtree":
                file_time = dt.datetime.fromtimestamp(mtime, tz=dt.timezone.utc)
                if (full.suffix not in WHITELISTED_DOCS
                        and full.suffix not in WHITELISTED_CODE
                        and reproduction_log_creation_time is not None
                        and file_time < reproduction_log_creation_time):
                    continue
            files.append(full)
            mtimes.append(mtime)

    reproduce_touched_files = True
    if task_category == "Result Analysis" and reproduction_log_creation_time is not None:
        if files and all(
            dt.datetime.fromtimestamp(m, tz=dt.timezone.utc) < reproduction_log_creation_time
            for m in mtimes
        ):
            reproduce_touched_files = False

    files.sort()
    return files, reproduce_touched_files


def build_tree_structure(relative_paths: list[Path]) -> str:
    """Port of simple.py:220-245."""
    tree: dict[str, Any] = {}
    for file in relative_paths:
        current = tree
        for part in file.parts:
            if part not in current:
                current[part] = {}
            current = current[part]

    def _build(node: dict, prefix: str = "") -> str:
        lines: list[str] = []
        items = list(node.items())
        for i, (name, sub) in enumerate(items):
            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{name}")
            if sub:
                ext = "    " if is_last else "│   "
                lines.append(_build(sub, prefix + ext))
        return "\n".join(lines)

    return _build(tree)


def prepare_tree_structure(
    submission_dir: Path,
    task_category: str,
    avail_context_len: int,
    reproduction_log_creation_time: Optional[dt.datetime],
) -> tuple[str, list[Path], bool]:
    """Port of simple.py:352-370."""
    for depth in (None, DEFAULT_MAX_FILE_DEPTH):
        files, touched = get_whitelisted_files(
            submission_dir, task_category, depth, reproduction_log_creation_time
        )
        rels = [p.relative_to(submission_dir) for p in files]
        tree_str = build_tree_structure(rels)
        if count_tokens(tree_str) < avail_context_len:
            return tree_str, files, touched
    tree_str = truncate_to_tokens(tree_str, avail_context_len)
    return tree_str, files, touched


# ---------------------------------------------------------------------------
# Rubric handling
# ---------------------------------------------------------------------------

def load_rubric(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def code_only_rubric(node: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Port of tasks.py:386-407 (reduce_to_category with 'Code Development')."""
    sub = node.get("sub_tasks") or []
    if not sub:
        if node.get("task_category") == "Code Development":
            return node
        return None
    filtered = []
    for s in sub:
        p = code_only_rubric(s)
        if p is not None:
            filtered.append(p)
    if not filtered and node.get("task_category") != "Code Development":
        return None
    new = dict(node)
    new["sub_tasks"] = filtered
    if filtered:
        new["task_category"] = None
    return new


def get_leaf_nodes_dfs(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Port of tasks.py:295-301 (depth-first leaf enumeration)."""
    sub = node.get("sub_tasks") or []
    if not sub:
        return [node]
    out: list[dict[str, Any]] = []
    for s in sub:
        out.extend(get_leaf_nodes_dfs(s))
    return out


def find_path_to_descendant(root: dict[str, Any], dst_id: str) -> Optional[list[dict[str, Any]]]:
    """Port of tasks.py:210-219."""
    if root["id"] == dst_id:
        return [root]
    for s in root.get("sub_tasks") or []:
        sp = find_path_to_descendant(s, dst_id)
        if sp:
            return [root] + sp
    return None


def get_prior_nodes(root: dict[str, Any], node_id: str,
                    max_prior_nodes: Optional[int] = None) -> list[dict[str, Any]]:
    """Port of tasks.py:221-274."""
    if root["id"] == node_id:
        return []
    path = find_path_to_descendant(root, node_id)
    if path is None:
        raise ValueError(f"Task with id '{node_id}' not found.")
    required = [root]
    for i in range(1, len(path)):
        node = path[i]
        parent = path[i - 1]
        siblings = parent.get("sub_tasks") or []
        ids = [s["id"] for s in siblings]
        idx = ids.index(node["id"])
        required += siblings[: idx + 1]
    required = required[:-1]
    if max_prior_nodes is not None:
        required = required[-max_prior_nodes:]
    return required


# ---------------------------------------------------------------------------
# Prompt loading / rendering
# ---------------------------------------------------------------------------

def _load(name: str) -> str:
    return (PROMPTS / name).read_text()


def render_system_prompt(code_only: bool) -> str:
    template = _load("system_prompt.txt")
    clause = "" if code_only else _load("system_prompt_code_only_clause.txt")
    return template.replace("{code_only_clause}", clause)


def render_criterion_prompt(*, preceding_criteria: str, criterion: str,
                             task_category: Optional[str],
                             task_category_question: str) -> str:
    template = _load("criterion_prompt.txt")
    return (template
            .replace("{preceding_criteria}", preceding_criteria)
            .replace("{criterion}", criterion)
            .replace("{task_category}", str(task_category))
            .replace("{task_category_question}", task_category_question))


def render_grading_prompt(continuous: bool) -> str:
    template = _load("grading_prompt.txt")
    if continuous:
        instr = "Give a score between 0 and 1 depending on how well the criteria has been met, and explain why."
    else:
        instr = "Give a score of either 0 or 1 depending on whether the criteria has been met, and explain why."
    return template.replace("{score_instruction}", instr)


def render_score_extraction_prompt(continuous: bool) -> str:
    template = _load("score_extraction_prompt.txt")
    instr = "(between 0 and 1)" if continuous else "(either 0 or 1)"
    return template.replace("{score_instruction}", instr)


def render_file_ranking_user_message(*, paper_md: str, addendum: str, criterion: str,
                                       tree_structure: str, max_files: int) -> list[dict[str, str]]:
    """Builds the 4 user-role messages for the file-ranking call.
    Preserves exact wording from simple.py:396-411."""
    max_files_str = f"{max_files} " if max_files else ""
    return [
        {"role": "user", "content": f"The paper is below:\n<paper>\n{paper_md}\n</paper>"},
        {"role": "user", "content": (
            "If included with the paper, you will now be shown an addendum which provides "
            "clarification for the paper and how to evaluate its reproduction:\n<addendum>\n"
            f"{addendum}\n</addendum>"
        )},
        {"role": "user", "content": f"Here is the criterion that you are grading:\n<criterion>\n{criterion}\n</criterion>"},
        {"role": "user", "content": (
            "Here are the files in the submission attempt:\n\nDirectory structure:\n"
            f"{tree_structure}\n\n"
            f"Now return a list of the {max_files_str}most relevant files in order of relevance (descending) to the resolution criteria, "
            "to be provided for your inspection. Your response must contain each filename separated by newlines, "
            "with each file containing the full path. Do not write anything else."
        )},
    ]


# ---------------------------------------------------------------------------
# Per-leaf prompt bundle
# ---------------------------------------------------------------------------

@dataclass
class LeafPromptBundle:
    leaf_id: str
    messages: list[dict[str, str]]
    avail_tokens: int
    ranked_files: list[str] = field(default_factory=list)
    files_shown: list[str] = field(default_factory=list)
    relevant_files_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "leaf_id": self.leaf_id,
            "messages": self.messages,
            "avail_tokens": self.avail_tokens,
            "ranked_files": self.ranked_files,
            "files_shown": self.files_shown,
            "relevant_files_text_tokens_est": count_tokens(self.relevant_files_text),
        }


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _workdir_paths(workdir: Path) -> dict[str, Path]:
    judge = workdir / ".judge"
    return {
        "judge": judge,
        "state": judge / "judge_state.json",
        "leaves": judge / "leaves",
        "log": judge / "run.log",
    }


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text())


def _write_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def _resolve_paper(workdir: Path) -> tuple[Path, Path]:
    """Returns (paper_md_path, paper_source_path)."""
    paper_md = workdir / "paper.md"
    paper_pdf = workdir / "paper.pdf"
    if paper_md.exists():
        return paper_md, paper_md
    if paper_pdf.exists():
        # Extract via helper script.
        from subprocess import run as sprun
        out_md = workdir / "paper.md"
        r = sprun(
            [sys.executable, str(HERE / "extract_paper_text.py"), str(paper_pdf), str(out_md)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"paper extraction failed:\n{r.stderr}")
        return out_md, paper_pdf
    raise FileNotFoundError(f"no paper.md or paper.pdf under {workdir}")


# ---------------------------------------------------------------------------
# init subcommand
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        print(f"workdir not found or not a dir: {workdir}", file=sys.stderr)
        return 1

    submission = workdir / "submission"
    if not submission.is_dir():
        print(f"submission/ missing under {workdir}", file=sys.stderr)
        return 1

    rubric_path = workdir / "rubric.json"
    if not rubric_path.exists():
        print(f"rubric.json missing under {workdir}", file=sys.stderr)
        return 1

    paper_md_path, paper_source = _resolve_paper(workdir)
    paper_md = paper_md_path.read_text()

    addendum_path = workdir / "addendum.md"
    addendum = addendum_path.read_text() if addendum_path.exists() else ""
    judge_addendum_path = workdir / "judge_addendum.md"
    judge_addendum = judge_addendum_path.read_text() if judge_addendum_path.exists() else ""
    joined_addendum = f"{addendum}\n{judge_addendum}".strip() or "(NO ADDENDUM GIVEN)"

    # reproduce.sh / reproduce.log (may live under submission/)
    reproduce_sh_content = "(Does not exist)"
    reproduce_log_content = "(Does not exist)"
    reproduction_log_creation_time: Optional[dt.datetime] = None
    reproduce_sh = submission / "reproduce.sh"
    reproduce_log = submission / "reproduce.log"
    reproduce_log_ct = submission / "reproduce.log.creation_time"
    if reproduce_sh.exists():
        reproduce_sh_content = safe_read_file(reproduce_sh)
    if reproduce_log.exists():
        reproduce_log_content = safe_read_file(reproduce_log)
        try:
            reproduction_log_creation_time = dt.datetime.fromtimestamp(
                reproduce_log.stat().st_mtime, tz=dt.timezone.utc,
            )
        except Exception:
            pass
    if reproduce_log_ct.exists():
        try:
            reproduction_log_creation_time = dt.datetime.fromtimestamp(
                int(reproduce_log_ct.read_text().strip()), tz=dt.timezone.utc,
            )
        except Exception:
            pass

    rubric = load_rubric(rubric_path)
    if args.code_only:
        new_rubric = code_only_rubric(rubric)
        if new_rubric is None:
            print("code-only mode produced empty rubric", file=sys.stderr)
            return 1
        rubric = new_rubric

    leaves = get_leaf_nodes_dfs(rubric)
    if args.max_leaves is not None:
        leaves = leaves[: args.max_leaves]

    paths = _workdir_paths(workdir)
    paths["judge"].mkdir(parents=True, exist_ok=True)
    paths["leaves"].mkdir(parents=True, exist_ok=True)

    # Precompute tree structures per category (simple.py:155-158)
    context_window = args.context_window_tokens
    buffer_tokens = args.buffer_tokens
    paper_tokens = count_tokens(paper_md)
    log_tokens = count_tokens(reproduce_log_content) if reproduce_log_content != "(Does not exist)" else 0

    avail_context_lens = {
        "Code Development": context_window - (paper_tokens + buffer_tokens),
        "Code Execution": context_window - (paper_tokens + log_tokens + buffer_tokens),
        "Result Analysis": context_window - (paper_tokens + log_tokens + buffer_tokens),
        "Subtree": context_window - (paper_tokens + log_tokens + buffer_tokens),
    }

    tree_structures: dict[str, str] = {}
    reproduce_touched_by_cat: dict[str, bool] = {}
    for cat in ("Code Development", "Code Execution", "Result Analysis", "Subtree"):
        tree_str, _files, touched = prepare_tree_structure(
            submission, cat, max(0, avail_context_lens[cat]), reproduction_log_creation_time,
        )
        tree_structures[cat] = tree_str
        reproduce_touched_by_cat[cat] = touched

    # Per-leaf prompt bundles: we only prepare the FILE RANKING messages here.
    # The leaf-grading bundle is finalised in record-ranking once Claude returns the ordered list.
    for leaf in leaves:
        leaf_id = leaf["id"]
        tcat = leaf.get("task_category") or "Subtree"
        tree_str = tree_structures[tcat]

        ranking_system = _load("file_ranking_prompt.txt")
        ranking_messages = [{"role": "system", "content": ranking_system}]
        ranking_messages += render_file_ranking_user_message(
            paper_md=paper_md,
            addendum=joined_addendum,
            criterion=leaf["requirements"],
            tree_structure=tree_str,
            max_files=args.max_files,
        )

        leaf_dir = paths["leaves"] / _safe_id(leaf_id)
        leaf_dir.mkdir(parents=True, exist_ok=True)
        _write_json(leaf_dir / "ranking_messages.json", ranking_messages)
        # Human-readable concatenation for the skill runner to read easily.
        (leaf_dir / "ranking_prompt.md").write_text(
            _messages_to_md(ranking_messages),
        )
        # Placeholder for Claude's output.
        ranked_out = leaf_dir / "ranked_files.txt"
        if not ranked_out.exists():
            ranked_out.write_text("")  # to be filled in

        # Dump leaf metadata for convenience.
        _write_json(leaf_dir / "leaf.json", leaf)

    state = {
        "workdir": str(workdir),
        "submission_path": str(submission),
        "rubric_path": str(rubric_path),
        "paper_path": str(paper_source),
        "paper_md_path": str(paper_md_path),
        "addendum_path": str(addendum_path) if addendum_path.exists() else None,
        "code_only": args.code_only,
        "max_prior_nodes": args.max_prior_nodes,
        "max_files": args.max_files,
        "max_leaves": args.max_leaves,
        "buffer_tokens": args.buffer_tokens,
        "context_window_tokens": args.context_window_tokens,
        "joined_addendum": joined_addendum,
        "reproduce_sh_content": reproduce_sh_content,
        "reproduce_log_content": reproduce_log_content,
        "reproduction_log_creation_time": (
            reproduction_log_creation_time.isoformat() if reproduction_log_creation_time else None
        ),
        "avail_context_lens": avail_context_lens,
        "reproduce_touched_by_cat": reproduce_touched_by_cat,
        "tree_structures": tree_structures,
        "rubric": rubric,  # possibly pruned
        "leaves": [l["id"] for l in leaves],
        "leaf_statuses": {l["id"]: "pending" for l in leaves},
        "verdicts": {},
        "timestamp_start": dt.datetime.now(dt.timezone.utc).isoformat(),
        "token_usage_total": {"input_tokens_est": 0, "output_tokens_est": 0, "leaves_graded": 0},
    }
    _write_json(paths["state"], state)

    print(f"[init] workdir:   {workdir}")
    print(f"[init] leaves:    {len(leaves)}")
    print(f"[init] code_only: {args.code_only}")
    print(f"[init] paper:     {paper_source}")
    print(f"[init] rubric:    {rubric_path}")
    print(f"[init] submission:{submission}")
    print(f"[init] state:     {paths['state']}")
    print(f"[init] token encoder: {'tiktoken.o200k_base' if _ENCODER else 'len//4 fallback'}")
    return 0


def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:200] or "leaf"


def _messages_to_md(messages: list[dict[str, str]]) -> str:
    parts = []
    for m in messages:
        parts.append(f"## [{m['role']}]\n\n{m['content']}\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# record-ranking subcommand
# ---------------------------------------------------------------------------

def cmd_record_ranking(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    paths = _workdir_paths(workdir)
    state = _read_json(paths["state"])

    leaf_id = args.leaf_id
    leaf_dir = paths["leaves"] / _safe_id(leaf_id)

    # Load ranking output.
    raw = Path(args.ranked_files_path).read_text()
    selected = [line.strip() for line in raw.splitlines() if line.strip()]

    # Look up the leaf
    rubric = state["rubric"]
    leaf = next((l for l in get_leaf_nodes_dfs(rubric) if l["id"] == leaf_id), None)
    if leaf is None:
        print(f"leaf {leaf_id} not in rubric", file=sys.stderr)
        return 1

    tcat = leaf.get("task_category") or "Subtree"

    # Check Result Analysis short-circuit (simple.py:557-567)
    if tcat == "Result Analysis" and not state["reproduce_touched_by_cat"].get("Result Analysis", True):
        verdict = {
            "score": 0,
            "valid_score": True,
            "justification": "Reproduce.sh did not touch any files, so there are no reproduced results to analyze.",
            "prose_verdict": "",
            "ranked_files": selected,
            "files_shown_to_grader": [],
            "short_circuit_reason": "reproduce_did_not_touch_files",
            "tokens_input_est": 0,
            "tokens_output_est": 0,
            "grading_seconds": 0.0,
        }
        _write_json(leaf_dir / "verdict.json", verdict)
        state["verdicts"][leaf_id] = verdict
        state["leaf_statuses"][leaf_id] = "done"
        _write_json(paths["state"], state)
        print(f"[record-ranking] leaf {leaf_id}: short-circuited (Result Analysis, no touched files)")
        return 0

    # Fetch file contents under token budget. (simple.py:419-474)
    submission_dir = Path(state["submission_path"])
    avail_tokens = state["avail_context_lens"][tcat]
    max_tokens = max(0, avail_tokens - DEFAULT_FILE_CONTENT_BUFFER)
    max_files = state.get("max_files") or DEFAULT_MAX_FILES

    selected_file_tokens: list = []  # list of encoded token chunks
    total_tokens = 0
    num_files = 0
    files_shown: list[str] = []

    if _ENCODER is not None:
        selected_token_stream: list[int] = []
        for rel in selected[:max_files]:
            full = submission_dir / rel.strip().strip("/")
            try:
                if not full.exists():
                    continue
                if full.is_dir():
                    continue
                content = safe_read_file(full)
                formatted = format_file(full.relative_to(submission_dir), content)
                content_tokens = _ENCODER.encode(formatted + "\n\n", disallowed_special=())
                if total_tokens + len(content_tokens) > max_tokens:
                    remaining = max_tokens - total_tokens
                    if remaining > 0:
                        selected_token_stream.extend(content_tokens[:remaining])
                        num_files += 1
                        files_shown.append(str(full.relative_to(submission_dir)))
                    break
                selected_token_stream.extend(content_tokens)
                total_tokens += len(content_tokens)
                num_files += 1
                files_shown.append(str(full.relative_to(submission_dir)))
                if num_files >= max_files:
                    break
            except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
                continue
            except Exception:
                continue
        relevant_files_text = _ENCODER.decode(selected_token_stream).rsplit("\n", 1)[0]
    else:
        # Char-based fallback.
        max_chars = max_tokens * 4
        chunks: list[str] = []
        total_chars = 0
        for rel in selected[:max_files]:
            full = submission_dir / rel.strip().strip("/")
            try:
                if not full.exists() or full.is_dir():
                    continue
                content = safe_read_file(full)
                formatted = format_file(full.relative_to(submission_dir), content) + "\n\n"
                if total_chars + len(formatted) > max_chars:
                    remaining = max_chars - total_chars
                    if remaining > 0:
                        chunks.append(formatted[:remaining])
                        num_files += 1
                        files_shown.append(str(full.relative_to(submission_dir)))
                    break
                chunks.append(formatted)
                total_chars += len(formatted)
                num_files += 1
                files_shown.append(str(full.relative_to(submission_dir)))
                if num_files >= max_files:
                    break
            except Exception:
                continue
        relevant_files_text = ("".join(chunks)).rsplit("\n", 1)[0]

    # Build grading messages (simple.py:513-547)
    paper_md = Path(state["paper_md_path"]).read_text()
    joined_addendum = state["joined_addendum"]
    code_only = state.get("code_only", False)
    system_prompt = render_system_prompt(code_only)

    relevant_files_prompt = (
        f"Here are the most relevant files included in the submission attempt, concatenated:\n<files>\n{relevant_files_text}\n</files>"
        if tcat != "Result Analysis"
        else f"Here are the most relevant docs and the files touched (i.e. modified or created) during the reproduce.sh execution, concatenated:\n<files>\n{relevant_files_text}\n</files>"
    )

    reproduce_msgs: list[dict[str, str]] = []
    if not code_only:
        if tcat == "Code Development":
            reproduce_msgs = [
                {"role": "user", "content": (
                    f"Here is the `reproduce.sh` provided in the submission, if any:\n<reproduce.sh>\n{state['reproduce_sh_content']}\n</reproduce.sh>"
                )},
            ]
        else:
            reproduce_msgs = [
                {"role": "user", "content": (
                    f"Here is the `reproduce.sh` provided in the submission, if any:\n<reproduce.sh>\n{state['reproduce_sh_content']}\n</reproduce.sh>"
                )},
                {"role": "user", "content": (
                    f"Here is the `reproduce.log` provided in the submission, if any:\n<reproduce.log>\n{state['reproduce_log_content']}\n</reproduce.log>"
                )},
            ]

    # Preceding criteria via get_prior_nodes
    prior = get_prior_nodes(rubric, leaf_id, state.get("max_prior_nodes"))
    preceding_criteria = "".join(f" -> {n['requirements']}\n" for n in prior)

    criterion_msg = render_criterion_prompt(
        preceding_criteria=preceding_criteria,
        criterion=leaf["requirements"],
        task_category=leaf.get("task_category"),
        task_category_question=TASK_CATEGORY_QUESTIONS.get(
            leaf.get("task_category"), TASK_CATEGORY_DEFAULT_QUESTION,
        ),
    )
    grading_msg = render_grading_prompt(continuous=(tcat == "Subtree"))

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"The paper is below:\n{paper_md}"},
        {"role": "user", "content": (
            "If included with the paper, you will now be shown an addendum which provides "
            "clarification for the paper and how to evaluate its reproduction:\n<addendum>\n"
            f"{joined_addendum}\n</addendum>"
        )},
        {"role": "user", "content": relevant_files_prompt},
        *reproduce_msgs,
        {"role": "user", "content": criterion_msg},
        {"role": "user", "content": grading_msg},
    ]

    _write_json(leaf_dir / "grading_messages.json", messages)
    (leaf_dir / "grading_prompt.md").write_text(_messages_to_md(messages))
    _write_json(leaf_dir / "files_shown.json", files_shown)

    # Update state
    state["leaf_statuses"][leaf_id] = "ranked"
    _write_json(paths["state"], state)

    print(f"[record-ranking] leaf {leaf_id}: {len(files_shown)} files shown, tokens(files) ~= {count_tokens(relevant_files_text)}")
    return 0


# ---------------------------------------------------------------------------
# record-verdict subcommand
# ---------------------------------------------------------------------------

def cmd_record_verdict(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    paths = _workdir_paths(workdir)
    state = _read_json(paths["state"])
    leaf_id = args.leaf_id
    leaf_dir = paths["leaves"] / _safe_id(leaf_id)

    leaf = next((l for l in get_leaf_nodes_dfs(state["rubric"]) if l["id"] == leaf_id), None)
    if leaf is None:
        print(f"leaf {leaf_id} not in rubric", file=sys.stderr)
        return 1

    tcat = leaf.get("task_category") or "Subtree"
    continuous = tcat == "Subtree"

    prose = ""
    if args.prose_path:
        prose_p = Path(args.prose_path)
        if prose_p.exists():
            prose = prose_p.read_text()

    score = args.score
    if continuous:
        try:
            score_val = float(score)
        except (TypeError, ValueError):
            score_val = 0.0
        score_val = max(0.0, min(1.0, score_val))
    else:
        try:
            score_val = int(float(score))
        except (TypeError, ValueError):
            score_val = 0
        score_val = 1 if score_val == 1 else 0

    # input-token estimate: grading_messages content
    gm_path = leaf_dir / "grading_messages.json"
    tokens_input_est = 0
    if gm_path.exists():
        try:
            msgs = _read_json(gm_path)
            tokens_input_est = sum(count_tokens(m.get("content", "")) for m in msgs)
        except Exception:
            tokens_input_est = 0
    tokens_output_est = count_tokens(prose)

    ranked_files: list[str] = []
    rf = leaf_dir / "ranked_files.txt"
    if rf.exists():
        ranked_files = [line.strip() for line in rf.read_text().splitlines() if line.strip()]
    files_shown = []
    fs = leaf_dir / "files_shown.json"
    if fs.exists():
        try:
            files_shown = _read_json(fs)
        except Exception:
            pass

    verdict = {
        "score": score_val,
        "valid_score": bool(args.valid_score),
        "justification": args.justification or "",
        "prose_verdict": prose,
        "ranked_files": ranked_files,
        "files_shown_to_grader": files_shown,
        "short_circuit_reason": args.short_circuit,
        "tokens_input_est": tokens_input_est,
        "tokens_output_est": tokens_output_est,
        "grading_seconds": args.grading_seconds or 0.0,
    }
    _write_json(leaf_dir / "verdict.json", verdict)
    state["verdicts"][leaf_id] = verdict
    state["leaf_statuses"][leaf_id] = "done"
    totals = state["token_usage_total"]
    totals["input_tokens_est"] += tokens_input_est
    totals["output_tokens_est"] += tokens_output_est
    totals["leaves_graded"] += 1
    _write_json(paths["state"], state)
    print(f"[record-verdict] leaf {leaf_id}: score={score_val} valid={verdict['valid_score']}")
    return 0


# ---------------------------------------------------------------------------
# finalize subcommand
# ---------------------------------------------------------------------------

def _apply_verdicts_to_tree(node: dict[str, Any], verdicts: dict[str, dict]) -> dict[str, Any]:
    """Bottom-up: attach leaf verdicts, aggregate internals via score_from_children."""
    sub = node.get("sub_tasks") or []
    out = {
        "id": node["id"],
        "requirements": node["requirements"],
        "weight": node["weight"],
        "task_category": node.get("task_category"),
        "finegrained_task_category": node.get("finegrained_task_category"),
    }
    if not sub:
        v = verdicts.get(node["id"])
        if v is None:
            out.update({
                "score": 0.0,
                "valid_score": False,
                "explanation": "Leaf was not graded (skipped or max_leaves truncation).",
                "judge_metadata": None,
                "sub_tasks": [],
            })
        else:
            out.update({
                "score": v["score"],
                "valid_score": v.get("valid_score", True),
                "explanation": v.get("justification", ""),
                "judge_metadata": {
                    "full_judge_response": v.get("prose_verdict", ""),
                    "token_usage": {
                        "tokens_input_est": v.get("tokens_input_est", 0),
                        "tokens_output_est": v.get("tokens_output_est", 0),
                    },
                    "short_circuit_reason": v.get("short_circuit_reason"),
                },
                "sub_tasks": [],
            })
        return out

    children = [_apply_verdicts_to_tree(s, verdicts) for s in sub]
    total_weight = sum(c["weight"] for c in children)
    weighted = 0.0 if total_weight == 0 else sum(c["score"] * c["weight"] for c in children) / total_weight
    out.update({
        "score": weighted,
        "valid_score": True,
        "explanation": "Aggregated score from sub-tasks.",
        "judge_metadata": None,
        "sub_tasks": children,
    })
    return out


def _simple_judge_compat_tree(node: dict[str, Any]) -> dict[str, Any]:
    """Strip extensions from tree nodes so the compat subsection matches upstream to_dict (graded_task_node.py:48-60)."""
    sub_tasks = [_simple_judge_compat_tree(c) for c in (node.get("sub_tasks") or [])]
    return {
        "id": node["id"],
        "requirements": node["requirements"],
        "weight": node["weight"],
        "score": node["score"],
        "valid_score": node.get("valid_score", True),
        "task_category": node.get("task_category"),
        "explanation": node.get("explanation", ""),
        "judge_metadata": node.get("judge_metadata"),
        "sub_tasks": sub_tasks,
    }


def _collect_leaf_scores(node: dict[str, Any], out: dict[str, Any]) -> None:
    sub = node.get("sub_tasks") or []
    if not sub:
        out[node["id"]] = node["score"]
        return
    for c in sub:
        _collect_leaf_scores(c, out)


def cmd_finalize(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    paths = _workdir_paths(workdir)
    state = _read_json(paths["state"])

    verdicts = state.get("verdicts") or {}
    rubric = state["rubric"]

    graded_tree = _apply_verdicts_to_tree(rubric, verdicts)
    root_score = graded_tree["score"]

    compat_tree = _simple_judge_compat_tree(graded_tree)
    leaf_scores: dict[str, Any] = {}
    _collect_leaf_scores(compat_tree, leaf_scores)

    # Build leaf_verdicts list in DFS order
    leaf_verdicts_out: list[dict[str, Any]] = []
    for leaf in get_leaf_nodes_dfs(rubric):
        v = verdicts.get(leaf["id"])
        if v is None:
            leaf_verdicts_out.append({
                "leaf_id": leaf["id"],
                "requirement": leaf["requirements"],
                "task_category": leaf.get("task_category"),
                "finegrained_task_category": leaf.get("finegrained_task_category"),
                "weight": leaf["weight"],
                "score": 0,
                "valid_score": False,
                "justification": "Leaf was not graded (skipped or max_leaves truncation).",
                "ranked_files": [],
                "files_shown_to_grader": [],
                "prose_verdict": "",
                "tokens_input_est": 0,
                "tokens_output_est": 0,
                "grading_seconds": 0.0,
                "short_circuit_reason": "not_graded",
            })
        else:
            leaf_verdicts_out.append({
                "leaf_id": leaf["id"],
                "requirement": leaf["requirements"],
                "task_category": leaf.get("task_category"),
                "finegrained_task_category": leaf.get("finegrained_task_category"),
                "weight": leaf["weight"],
                "score": v["score"],
                "valid_score": v.get("valid_score", True),
                "justification": v.get("justification", ""),
                "ranked_files": v.get("ranked_files", []),
                "files_shown_to_grader": v.get("files_shown_to_grader", []),
                "prose_verdict": v.get("prose_verdict", ""),
                "tokens_input_est": v.get("tokens_input_est", 0),
                "tokens_output_est": v.get("tokens_output_est", 0),
                "grading_seconds": v.get("grading_seconds", 0.0),
                "short_circuit_reason": v.get("short_circuit_reason"),
            })

    ts_end = dt.datetime.now(dt.timezone.utc)
    ts_start = dt.datetime.fromisoformat(state["timestamp_start"])
    wall = (ts_end - ts_start).total_seconds()

    grader_output = {
        "rubric_path": state["rubric_path"],
        "submission_path": state["submission_path"],
        "paper_path": state["paper_path"],
        "addendum_path": state.get("addendum_path"),
        "backend": "claude-code-skill",
        "model": "claude (via Claude Code)",
        "code_only": state.get("code_only", False),
        "max_prior_nodes": state.get("max_prior_nodes"),
        "max_files": state.get("max_files"),
        "max_leaves": state.get("max_leaves"),
        "timestamp_start": state["timestamp_start"],
        "timestamp_end": ts_end.isoformat(),
        "wall_clock_seconds": wall,
        "root_score": root_score,
        "tree": graded_tree,
        "leaf_verdicts": leaf_verdicts_out,
        "token_usage_total": state["token_usage_total"],
        "cost_estimate_usd": None,
        "simple_judge_compat": {
            "root_score": compat_tree["score"],
            "per_leaf_scores": leaf_scores,
            "tree": compat_tree,
        },
    }

    out_json = workdir / "grader_output.json"
    _write_json(out_json, grader_output)

    # token_usage.json (per-leaf + totals, Claude-style)
    token_usage = {
        "total": state["token_usage_total"],
        "per_leaf": {
            lid: {
                "tokens_input_est": v.get("tokens_input_est", 0),
                "tokens_output_est": v.get("tokens_output_est", 0),
            }
            for lid, v in verdicts.items()
        },
    }
    _write_json(workdir / "token_usage.json", token_usage)

    # judge_log.md
    lines = [
        f"# paperbench-judge log",
        "",
        f"- backend: claude-code-skill",
        f"- rubric: `{state['rubric_path']}`",
        f"- submission: `{state['submission_path']}`",
        f"- paper: `{state['paper_path']}`",
        f"- code_only: {state.get('code_only', False)}",
        f"- wall_clock_seconds: {wall:.1f}",
        f"- leaves graded: {state['token_usage_total']['leaves_graded']}",
        f"- total leaves: {len(state['leaves'])}",
        f"- root_score: **{root_score:.4f}**",
        "",
        "## Leaf verdicts",
        "",
    ]
    for lv in leaf_verdicts_out:
        lines.append(
            f"### {lv['leaf_id']} (score={lv['score']}, weight={lv['weight']}, cat={lv['task_category']})"
        )
        lines.append("")
        lines.append(f"**Requirement:** {lv['requirement']}")
        lines.append("")
        if lv.get("short_circuit_reason"):
            lines.append(f"*Short-circuited: {lv['short_circuit_reason']}*")
            lines.append("")
        if lv.get("justification"):
            lines.append(f"**Justification:** {lv['justification']}")
            lines.append("")
        if lv.get("ranked_files"):
            lines.append("**Ranked files (top 5):**")
            for f in lv["ranked_files"][:5]:
                lines.append(f"- `{f}`")
            lines.append("")
    (workdir / "judge_log.md").write_text("\n".join(lines))

    print(f"[finalize] wrote {out_json}")
    print(f"[finalize] root_score = {root_score:.4f}")
    print(f"[finalize] leaves graded = {state['token_usage_total']['leaves_graded']} / {len(state['leaves'])}")

    # Run validation
    val_script = HERE / "validate_output.py"
    import subprocess
    r = subprocess.run([sys.executable, str(val_script), str(out_json)], capture_output=True, text=True)
    print(r.stdout.strip())
    if r.returncode != 0:
        print(r.stderr.strip(), file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# status subcommand
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    paths = _workdir_paths(workdir)
    if not paths["state"].exists():
        print("no state — run init first.")
        return 1
    state = _read_json(paths["state"])
    total = len(state["leaves"])
    done = sum(1 for v in state["leaf_statuses"].values() if v == "done")
    ranked = sum(1 for v in state["leaf_statuses"].values() if v == "ranked")
    pending = total - done - ranked
    print(f"workdir: {workdir}")
    print(f"leaves total: {total}")
    print(f"  pending:    {pending}")
    print(f"  ranked:     {ranked}")
    print(f"  done:       {done}")
    if pending + ranked == 0:
        print("all leaves graded — ready for finalize.")
    else:
        print("next pending leaves:")
        i = 0
        for lid, st in state["leaf_statuses"].items():
            if st != "done":
                print(f"  {st:8}  {lid}")
                i += 1
                if i >= 10:
                    break
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("workdir")
    p_init.add_argument("--code-only", action="store_true")
    p_init.add_argument("--max-prior-nodes", type=int, default=None)
    p_init.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    p_init.add_argument("--max-leaves", type=int, default=None)
    p_init.add_argument("--buffer-tokens", type=int, default=DEFAULT_BUFFER_TOKENS)
    p_init.add_argument("--context-window-tokens", type=int, default=DEFAULT_CONTEXT_WINDOW_TOKENS)
    p_init.set_defaults(func=cmd_init)

    p_rr = sub.add_parser("record-ranking")
    p_rr.add_argument("workdir")
    p_rr.add_argument("leaf_id")
    p_rr.add_argument("ranked_files_path")
    p_rr.set_defaults(func=cmd_record_ranking)

    p_rv = sub.add_parser("record-verdict")
    p_rv.add_argument("workdir")
    p_rv.add_argument("leaf_id")
    p_rv.add_argument("prose_path", nargs="?")
    p_rv.add_argument("--score", required=True)
    p_rv.add_argument("--valid-score", type=_parse_bool, default=True)
    p_rv.add_argument("--justification", default="")
    p_rv.add_argument("--short-circuit", default=None)
    p_rv.add_argument("--grading-seconds", type=float, default=0.0)
    p_rv.set_defaults(func=cmd_record_verdict)

    p_fin = sub.add_parser("finalize")
    p_fin.add_argument("workdir")
    p_fin.set_defaults(func=cmd_finalize)

    p_st = sub.add_parser("status")
    p_st.add_argument("workdir")
    p_st.set_defaults(func=cmd_status)

    args = ap.parse_args()
    return args.func(args)


def _parse_bool(s: str) -> bool:
    return s.lower() in ("1", "true", "yes", "y", "t")


if __name__ == "__main__":
    sys.exit(main())
