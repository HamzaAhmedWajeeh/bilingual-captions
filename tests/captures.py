"""Whether this checkout has capture files to replay, and how to say it does not.

A *capture* is a saved Soniox response stream - one JSON object per line,
exactly what the engine sent. `app/replay.py` feeds one back through the real
`CaptionState`, so a capture is a full recording of a session that costs
nothing to replay and never touches the network. It is the single most useful
test input this project has.

**None are committed, and that is deliberate.** A capture is a verbatim
transcript of whatever was said in front of the microphone, which is not ours
to publish - and the four this project was built against are transcripts of
third-party audio. So `tests/fixtures/*.jsonl` is gitignored, and the tests
that need one skip with the message below rather than failing.

To run them, put your own captures in `tests/fixtures/` under these names:

    run1.jsonl          two-way, Arabic in and English out, many endpoints
    run2-oneway.jsonl   a one-way configuration
    run4-saudi.jsonl    one very long segment - the no-paragraph-break case
    run5-english.jsonl  English in, Arabic out

The names matter because the tests are not interchangeable: each one pins
down behaviour the others cannot reach, which is why they are named rather
than globbed. Record your own with:

    uv run python tools/probe_soniox.py --file talk.m4a --out tests/fixtures/run1.jsonl

or tick "Save the audio to this laptop" on /admin and copy a live log out of
`logs/`. Everything else in the suite runs without them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "tests" / "fixtures"

#: The captures the suite asks for by name, in the order they are described
#: above. Not a glob: a test that wants the single-long-segment case cannot
#: use whatever file happens to be first in the directory.
NAMES = ("run1", "run2-oneway", "run4-saudi", "run5-english")


def path(name: str) -> Path:
    return DIR / f"{name}.jsonl"


def missing() -> list[str]:
    return [name for name in NAMES if not path(name).is_file()]


def _reason() -> str:
    absent = missing()
    if not absent:
        return ""
    return (
        "no capture files in tests/fixtures/ "
        f"(missing: {', '.join(n + '.jsonl' for n in absent)}). "
        "These are verbatim transcripts and are not committed - see "
        "tests/captures.py for what to put there and how to record one. "
        "The rest of the suite does not need them."
    )


#: Put this on any test that reads a capture. Module-level `pytestmark` where
#: a whole file is capture-driven, a decorator where only some tests are.
needs_captures = pytest.mark.skipif(bool(missing()), reason=_reason())


def have(name: str) -> bool:
    """Is one named capture resolvable, wherever the server looks for it?

    Not just `tests/fixtures/`. A live response log in `logs/` is a capture
    too - same envelope, same `meta` line - and the browser tests drive the
    projector from one, so this asks the same question `app.main` does.
    """
    from app.main import CAPTURE_DIRS

    for directory in CAPTURE_DIRS:
        if (Path(directory) / name).is_file():
            return True
    return False


def needs_capture(name: str):
    """Skip unless this one capture is on the machine.

    Browser tests name a specific recording, because what they measure - a
    direction change, a reconnect mid-talk, three hours of paragraphs - is a
    property of that recording rather than of captures in general. Any file
    would not do, so the skip names the file.
    """
    return pytest.mark.skipif(
        not have(name),
        reason=(f"{name} is not on this machine. Captures are verbatim "
                f"transcripts and are not committed - see tests/captures.py. "
                f"Put one at tests/fixtures/{name} (or logs/{name}) to run "
                f"this."))


#: Brand artwork is gitignored, so a clone has none and the slots are hidden
#: by `/brand.css`. A test that measures where a wordmark sits on the page has
#: nothing to measure then, and would otherwise fail on a correct install.
def _have_wordmarks() -> bool:
    brand = ROOT / "brand"
    return all((brand / f"wordmark-{side}.png").is_file()
               for side in ("en", "ar"))


needs_wordmarks = pytest.mark.skipif(
    not _have_wordmarks(),
    reason=("no wordmark artwork in brand/ - it is gitignored, so a clone "
            "has none and the slots are hidden. Drop wordmark-en.png and "
            "wordmark-ar.png in to run the layout tests."))
