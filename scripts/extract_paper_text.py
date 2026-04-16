#!/usr/bin/env python3
"""Extract text from a paper PDF into a markdown-ish text file.

Mirrors the fallback logic of `rubric-creator` Pass 1: pymupdf first
(highest fidelity for research papers), pdftotext second, a final
error otherwise. Output is not Markdown proper — it is plain text with
page breaks preserved — but SimpleJudge only ever treats the paper as
a bag of tokens, so this is fidelity-compatible.

Usage:
    python extract_paper_text.py <pdf_path> <output_md_path>
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def extract_with_pymupdf(pdf_path: Path) -> str | None:
    try:
        import fitz  # pymupdf
    except ImportError:
        return None
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        print(f"[extract_paper_text] pymupdf failed to open: {e}", file=sys.stderr)
        return None
    chunks: list[str] = []
    for i, page in enumerate(doc):
        chunks.append(f"\n\n<!-- page {i + 1} -->\n\n")
        chunks.append(page.get_text("text"))
    doc.close()
    return "".join(chunks)


def extract_with_pdftotext(pdf_path: Path) -> str | None:
    if not shutil.which("pdftotext"):
        return None
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            check=True,
            timeout=300,
        )
        return out.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[extract_paper_text] pdftotext failed: {e}", file=sys.stderr)
        return None


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: extract_paper_text.py <pdf_path> <output_md_path>", file=sys.stderr)
        return 2

    pdf_path = Path(sys.argv[1]).resolve()
    out_path = Path(sys.argv[2]).resolve()

    if not pdf_path.exists():
        print(f"[extract_paper_text] Not found: {pdf_path}", file=sys.stderr)
        return 1

    text = extract_with_pymupdf(pdf_path)
    if text is None:
        text = extract_with_pdftotext(pdf_path)
    if text is None:
        print(
            "[extract_paper_text] No extractor available (pymupdf or pdftotext).",
            file=sys.stderr,
        )
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"[extract_paper_text] Wrote {len(text)} chars to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
