"""House style, enforced by character: no em dash anywhere we write.

They have been asked for out three times now, and twice they came straight
back from the file nobody happened to be looking at. A character is the one
kind of rule a test can hold absolutely, so this file holds it, over every
page and script under `web/` and over every committed markdown file.

**Comments and docs count.** The next status line is copied from the one
above it, and the next paragraph of `the project notes` is written in the style of
the paragraph before it, so a dash left in a comment or in a note to
ourselves is where the next one on screen comes from.

**Quoted speech is the one exception, and it is evidence rather than
prose.** `a capture report` and the engine notes quote what the
engine actually emitted, down to its punctuation: 41 inside inline code
spans and 8 inside blockquotes as of 2026-11-03. Rewriting those would make
the file misreport the measurement it exists to record. So an em dash is
allowed inside a fenced code block, an inline code span or a blockquote
line, and nowhere else. That leaves a loophole - our own prose inside a
blockquote - and `a capture report` had two of exactly that, now fixed;
anything new there has to be spotted in review.

Replace one with a comma, a period, a colon, parentheses or a spaced hyphen,
whichever the sentence already wants. An empty value in the operator page's
top bar or status tables is a plain hyphen.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from repo import needs_git  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"

# `tests/conftest.py` redirects the operator's directories for every test,
# and reaches into `app` to do it. This file needs nothing from `app`, so
# nothing else would put the repo root on the path when it runs alone.
sys.path.insert(0, str(ROOT))

#: Written as an escape, not as the character: this file is a
#: committed .py and the guard below reads it like any other.
EM_DASH = "\u2014"

#: Text under `web/` that a browser reads, source and comments alike. The
#: binaries are excluded by suffix rather than by trying to decode them: an
#: em dash is three bytes that can occur inside a PNG by chance, and a guard
#: that failed on artwork would be switched off the first time it did.
TEXT_SUFFIXES = (".html", ".js", ".css", ".svg", ".json", ".txt")

#: Files whose em dashes would be quoted engine output rather than our own
#: prose. **Empty here on purpose.** The private project had two such files
#: and this named them, so the exemption was a decision about those files
#: rather than a hole anywhere a backtick appears. Neither is published, so
#: nothing is exempt - which is the stricter rule and the simpler one.
#:
#: If you add a file that quotes verbatim engine output, name it here rather
#: than widening the pattern.
QUOTES_SPEECH: tuple[str, ...] = ()


def web_text_files() -> list[Path]:
    return sorted(
        p for p in WEB_DIR.rglob("*")
        if p.is_file() and p.suffix in TEXT_SUFFIXES
        and "brand" not in p.relative_to(WEB_DIR).parts
    )


def committed_markdown() -> list[str]:
    """Every markdown file git tracks, in repo-relative posix form."""
    listed = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, text=True,
        capture_output=True, check=False)
    return sorted(line.strip() for line in listed.stdout.splitlines()
                  if line.strip())


def committed_python() -> list[str]:
    """Every Python file git tracks.

    `tools/probe_soniox.py` is why this list exists: it *writes*
    `a capture report`, and the line that printed each segment header
    carried an em dash, so every regenerated capture brought six of them
    back whatever the markdown guard said. A rule about text has to cover
    the program that writes the text.
    """
    listed = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, text=True,
        capture_output=True, check=False)
    return sorted(line.strip() for line in listed.stdout.splitlines()
                  if line.strip())


def quoted_spans(raw: str) -> list[tuple[int, int]]:
    """Character ranges where a word of someone's speech may appear.

    Fenced code blocks, inline code spans and blockquote lines. Deliberately
    simple: it over-approximates rather than parsing markdown, because a
    guard that disagreed with a reader about what is quoted would be argued
    with instead of obeyed.
    """
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"^```.*?^```", raw, re.S | re.M):
        spans.append(match.span())
    for match in re.finditer(r"(?<!`)(`{1,2})(?!`)(.+?)(?<!`)\1(?!`)",
                             raw, re.S):
        spans.append(match.span())
    for match in re.finditer(r"^[ \t]*>.*$", raw, re.M):
        spans.append(match.span())
    return spans


def em_dashes_outside_quotes(raw: str) -> list[int]:
    spans = quoted_spans(raw)
    return [m.start() for m in re.finditer(EM_DASH, raw)
            if not any(a <= m.start() < b for a, b in spans)]


def line_of(raw: str, pos: int) -> int:
    return raw.count("\n", 0, pos) + 1


# --- web/ ------------------------------------------------------------------


def test_every_text_file_under_web_is_covered_by_the_em_dash_guard():
    """The guard below is only worth anything if it reads the pages.

    `TEXT_SUFFIXES` is an allowlist, so a page added with a suffix nobody
    thought of would be silently unchecked. Assert the three pages and every
    script are in the list the guard walks.
    """
    names = {p.name for p in web_text_files()}
    for name in ("admin.html", "index.html", "screen.html",
                 "admin.js", "app.js", "auto.js", "screen.js", "style.css"):
        assert name in names, f"{name} is not covered by the em dash guard"


@pytest.mark.parametrize("name", [p.name for p in web_text_files()])
def test_no_em_dash_survives_in_the_operator_facing_text(name):
    """No em dash anywhere under `web/`, in copy or in a comment.

    No exemption here: nothing under `web/` quotes anybody, so the answer is
    simply zero.
    """
    blob = (WEB_DIR / name).read_text(encoding="utf-8")
    assert EM_DASH not in blob, (
        f"{name} contains an em dash; use a comma, a period, a colon, "
        f"parentheses, or a plain hyphen for an empty value"
    )


# --- the docs --------------------------------------------------------------


@needs_git
def test_the_markdown_guard_reads_the_files_that_matter():
    """`git ls-files` is the list, so a new doc is covered the day it lands.

    Checked against the one file this repo always has, because an empty
    listing - a clone with no git, say - would otherwise pass this file in
    silence.
    """
    listed = committed_markdown()
    for name in ("README.md",):
        assert name in listed, f"{name} is not covered by the em dash guard"


@pytest.mark.parametrize("rel", committed_markdown())
def test_no_em_dash_survives_in_the_docs(rel):
    """No em dash in our own prose, in any committed markdown file."""
    raw = (ROOT / rel).read_text(encoding="utf-8")
    found = em_dashes_outside_quotes(raw)
    assert not found, (
        f"{rel} has an em dash in prose at line(s) "
        f"{sorted({line_of(raw, pos) for pos in found})}; use a comma, a "
        f"period, a colon, parentheses or a spaced hyphen"
    )


def test_the_quoted_speech_exemption_is_only_used_where_it_was_meant():
    """The exemption is for engine output, and only two files hold any.

    Counted rather than described: if a dash appears inside a code span in a
    file that is not quoting the engine, the exemption has started covering
    our own writing and the guard above has stopped meaning what it says.
    """
    for rel in committed_markdown():
        raw = (ROOT / rel).read_text(encoding="utf-8")
        total = raw.count(EM_DASH)
        if not total:
            continue
        assert rel.startswith(QUOTES_SPEECH), (
            f"{rel} carries {total} em dash(es) inside quoted material, and "
            f"it is not one of the files that quote the engine"
        )


@pytest.mark.parametrize("rel", committed_python())
def test_no_em_dash_survives_in_the_python(rel):
    """No em dash in a docstring, a comment or a string we print.

    No exemption: nothing in `app/`, `tools/` or `tests/` quotes a room. The
    fixtures do hold speech and they are `.jsonl`, which this does not read.
    """
    raw = (ROOT / rel).read_text(encoding="utf-8")
    found = [m for m in re.finditer(EM_DASH, raw)]
    assert not found, (
        f"{rel} has an em dash at line(s) "
        f"{sorted({line_of(raw, m.start()) for m in found})}"
    )
