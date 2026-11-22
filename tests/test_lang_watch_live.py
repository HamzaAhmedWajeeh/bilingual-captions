"""The detector against the real evenings, when they are on disk.

`logs/` is gitignored, so a clone has none of these and every test here
skips. On the operator's laptop they are the only evidence that the
threshold separates a locked session from a healthy one, and they are what
turns `app/lang_watch.py`'s constants from a guess into a measurement.

Nothing here prints or asserts on caption text. The assertions are shares,
counts and minute offsets, which is also what makes them safe to read in a
failure message.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.lang_watch import DRIFT, STUCK, LanguageWatch

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"

#: Response logs, named by the session they actually hold. On 2026-10-31
#: each was written under the *previous* session's stem - fixed now, but the
#: files on disk still carry the old names.
LOCKED = {
    # 2026-10-31 session 1406: no English-labelled original from 19:00, an
    # English speaker transcribed in Arabic script from about 24:50.
    "1406": "live-2026-10-31-135908.jsonl",
    # 2026-10-24 session 1432: the evening of the unexplained Auto-view
    # report. Same signature from 35:00.
    "2026-10-24": "live-2026-10-24-143245.jsonl",
}
#: The mixer session of the same afternoon, which did not lock.
CLEAN = "live-2026-10-31-145958.jsonl"
#: It locked, but its English was transliterated at only a few per cent, so
#: the stuck signal cannot see it and must not pretend to.
PARTIAL = "live-2026-10-31-140624.jsonl"


#: One pass per log for the whole module. These are 8-16 MB files and the
#: tests below ask about the same four of them repeatedly; without this the
#: module alone took 43 seconds.
_CACHE: dict[str, dict] = {}


def run(name: str) -> dict:
    """Drive the watcher over a log at the log's own pace."""
    if name in _CACHE:
        return _CACHE[name]
    _CACHE[name] = result = _run(name)
    return result


def _run(name: str) -> dict:
    path = LOGS / name
    if not path.is_file():
        pytest.skip(f"{name} is not on this machine; logs/ is gitignored")
    watch = LanguageWatch(act=False)
    stuck: list[tuple[float, float]] = []
    drift: list[float] = []
    peak = 0.0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not row.get("response"):
                continue
            now = row.get("recv_offset_ms", 0) / 1000.0
            for signal in watch.note_response(row["response"], now):
                if signal.kind == STUCK:
                    stuck.append((now, signal.share))
                    # Nothing reconnects here, so tell the watcher itself or
                    # one lock reports every response for the rest of the log.
                    watch.reconnected(now)
                elif signal.kind == DRIFT:
                    drift.append(now)
            share, words = watch.score(now)
            if words >= watch.min_window_words:
                peak = max(peak, share)
    return {"stuck": stuck, "drift": drift, "peak": peak,
            "words": watch.words_seen}


@pytest.mark.parametrize("nick", sorted(LOCKED))
def test_the_detector_catches_every_lock_we_know_about(nick):
    result = run(LOCKED[nick])
    assert result["stuck"], f"session {nick} locked and must be caught"
    first_at, first_share = result["stuck"][0]
    assert first_share >= 0.12
    # Both locks are caught in the second half of a long talk, never in the
    # first few minutes: a cold start is not a lock.
    assert first_at > 20 * 60


def test_the_1406_lock_is_caught_within_half_a_minute():
    """The English speaker entered a locked stream at about 24:50.

    10 seconds on the measurement that produced the threshold. Pinned
    loosely, because the figure that matters is "before the room has read a
    paragraph", not the exact second.
    """
    result = run(LOCKED["1406"])
    first_at, _ = result["stuck"][0]
    assert 24.8 * 60 <= first_at <= 25.5 * 60


def test_the_2026_10_24_report_is_the_same_fault():
    """The evening whose cause was never proved.

    `/screen` was reported showing live English captions over English
    speech about half an hour in. Session 1432 stops labelling any speech
    English at 35:00, runs at 0% Latin for ten minutes, and the detector
    finds transliterated English in it - so the English the room was reading
    was the translation of its own speech, arriving as Arabic.

    This does not explain the grey the report also described. `auto.js`
    writes one text node per paragraph and creates no spans, so the
    translation-only view still cannot render a partial; that half of the
    report remains unexplained and this test does not claim otherwise.
    """
    result = run(LOCKED["2026-10-24"])
    assert result["stuck"]
    first_at, _ = result["stuck"][0]
    assert 35 * 60 <= first_at <= 45 * 60
    assert result["peak"] >= 0.20


def test_the_mixer_session_never_fires():
    """36 minutes, the same device, the same config, the same afternoon.

    The session that behaved. Its score must stay well under the threshold
    throughout, or the detector would have reconnected a working talk.
    """
    result = run(CLEAN)
    assert result["stuck"] == []
    assert result["drift"] == []
    assert result["words"] > 1500, "and it must really have been scored"
    assert result["peak"] <= 0.06


def test_the_partly_locked_session_is_only_reported_as_drift():
    """Honest about what the stuck signal cannot see.

    Session 1436 locked - no English-labelled original from 12:00 but for
    two brief recoveries - and its English was transliterated at only a few
    per cent, so it peaks at 0.067 and never fires. The drift signal is what
    catches it, and drift only writes a line.
    """
    result = run(PARTIAL)
    assert result["stuck"] == []
    assert result["drift"], "drift is the only signal that sees this one"
    assert 0.0 < result["peak"] < 0.12


def test_the_margin_between_locked_and_healthy_still_holds():
    """The one number the threshold rests on.

    If a lexicon change pushes a healthy session up or a locked one down,
    this is where it shows. The recorded figures are: worst healthy window
    0.067, lowest firing share 0.125.
    """
    healthy = max(run(CLEAN)["peak"], run(PARTIAL)["peak"])
    lowest_fire = min(share for nick in LOCKED
                      for _, share in run(LOCKED[nick])["stuck"])
    assert healthy <= 0.08, f"a healthy session scored {healthy:.3f}"
    assert lowest_fire >= 0.12
    assert lowest_fire > healthy * 1.5, (
        f"margin too thin: healthy {healthy:.3f} vs fired {lowest_fire:.3f}"
    )
