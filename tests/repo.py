"""Whether this is a git checkout, for the tests that ask git what it tracks.

Several tests here prove a negative that only git can answer: that
`config/session.yaml` is *ignored* rather than merely absent, that no
transcript is tracked, that no committed file carries a join link. Those are
the assertions that keep private files private across a fork, and they are
worth having.

They need a repository. A source tarball, a vendored copy, or a checkout with
no `.git` is a legitimate way to have this code and cannot answer the
question - so these tests skip there rather than failing, and the rest of the
suite is unaffected.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def is_checkout() -> bool:
    if not (ROOT / ".git").exists():
        return False
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=ROOT, capture_output=True, text=True, check=False)
    except OSError:
        return False
    return done.returncode == 0


needs_git = pytest.mark.skipif(
    not is_checkout(),
    reason="not a git checkout, so git cannot be asked what it tracks")
