"""Calibrate `app.lang_watch` against every response log on disk.

Runs the watcher over each log at the log's own pace - `recv_offset_ms` is
the clock, so a session months old scores exactly as it did on the night -
and prints the highest score reached in windows known to be healthy against
the lowest reached in windows known to be stuck. The threshold is whatever
sits in the gap, and this tool is how anyone checks that the gap is still
there after the lexicon changes.

    uv run python tools/lang_calibrate.py
    uv run python tools/lang_calibrate.py --threshold 0.12 --window-words 120
    uv run python tools/lang_calibrate.py --timeline

Costs nothing and reaches no network. Prints shares, counts and seconds and
never a word of anyone's speech: the regions below are time ranges, which is
the only way to name a piece of a talk without quoting it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.lang_watch import (  # noqa: E402
    DRIFT, STUCK, LanguageWatch, STUCK_SHARE, WINDOW_SECONDS, WINDOW_WORDS,
)
from app.settings import LOG_DIR  # noqa: E402

#: Where response logs live. A live log in `logs/` is already a capture, and
#: the committed fixtures are captures too, so all three are fair game.
SEARCH = [
    (LOG_DIR, "live-*.jsonl"),
    (ROOT / "logs", "live-*.jsonl"),
    # What `tools/replay_recording.py` writes. Same envelope as a live log,
    # so it scores the same way - and the point of a paid replay is to put
    # its result in this table beside the session it came from.
    (LOG_DIR, "replaytest-*.jsonl"),
    (ROOT / "logs", "replaytest-*.jsonl"),
    (ROOT / "tests" / "fixtures", "*.jsonl"),
]

#: Windows in which an English speaker is known to have been transcribed in
#: Arabic script, measured in the live-run measurements.
#:
#: These are **not** the same as "the language identification had locked".
#: The lock is visible earlier - from 19:00 in session 1406 no English-
#: labelled original token arrived at all - but between the lock and the next
#: English speaker the room really was speaking Arabic, and a detector that
#: reads the shape of the words cannot see a fault in correctly transcribed
#: Arabic. The window starts where an English voice enters a locked stream,
#: because that is the first moment the fault exists in the output.
#:
#: The file names are the response logs', which on 2026-10-31 were each
#: written under the *previous* session's stem. The naming is fixed now; the
#: files on disk still carry the old names, so these do too.
STUCK_FROM = {
    # session 1406: lock from 19:00, English speaker from ~24:50
    "live-2026-10-31-135908.jsonl": (24.8 * 60.0, float("inf")),
    # 2026-10-24 session 1432: the evening of the unexplained Auto-view
    # report. Same signature - no English-labelled original and 0% Latin
    # from 35:00 - and the detector finds transliterated English in it. The
    # window stops at 45:00, where the Latin share jumps to 99% and the
    # engine starts emitting Latin text under an `ar` label instead.
    "live-2026-10-24-143245.jsonl": (35 * 60.0, 45 * 60.0),
}

#: Where the healthy region of a partly-locked log ends. Every log not named
#: here is healthy end to end. Named rather than inferred, so that adding a
#: log cannot silently relabel a region.
#:
#: Session 1436 is deliberately absent: it locked - no English-labelled
#: original from 12:00 except two brief recoveries - but its English was
#: transliterated at only a few per cent, so there is no stuck window in it
#: to measure. It is scored as healthy on purpose, which makes it the
#: hardest case in the table rather than an excused one.
HEALTHY_UNTIL = {
    "live-2026-10-31-135908.jsonl": 24.0 * 60.0,
    "live-2026-10-24-143245.jsonl": 34.0 * 60.0,
}


def responses(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("response"):
                yield row


def regions(name: str) -> tuple[float, float, float]:
    """(healthy_until, stuck_from, stuck_until) in seconds.

    A log with no entry in either table is healthy end to end, so its
    healthy region runs to infinity and it has no stuck region.
    """
    stuck_from, stuck_until = STUCK_FROM.get(name, (float("inf"),
                                                    float("inf")))
    healthy_until = HEALTHY_UNTIL.get(
        name, float("inf") if name not in STUCK_FROM else 0.0)
    return healthy_until, stuck_from, stuck_until


def run(path: Path, args) -> dict:
    watch = LanguageWatch(
        window_words=args.window_words,
        window_seconds=args.window_seconds,
        min_window_words=args.min_words,
        stuck_share=args.threshold,
        act=False,
    )
    healthy_until, stuck_from, stuck_until = regions(path.name)
    peak_healthy = 0.0
    peak_healthy_at = 0.0
    max_stuck = 0.0
    fired: list[tuple[float, float]] = []
    drifted: list[float] = []
    timeline: list[tuple[float, float, int]] = []
    last_bucket = -1
    duration = 0.0

    for row in responses(path):
        now = row.get("recv_offset_ms", 0) / 1000.0
        duration = max(duration, now)
        for signal in watch.note_response(row["response"], now):
            if signal.kind == STUCK:
                fired.append((now, signal.share))
                # `act=False`, so nothing reconnects; tell the watcher
                # anyway, or one lock reports itself every response for the
                # rest of the talk and the table says nothing.
                watch.reconnected(now)
            elif signal.kind == DRIFT:
                drifted.append(now)
        share, words = watch.score(now)
        if words >= args.min_words:
            if now <= healthy_until and share > peak_healthy:
                peak_healthy, peak_healthy_at = share, now
            if stuck_from <= now < stuck_until:
                max_stuck = max(max_stuck, share)
        bucket = int(now // 60)
        if args.timeline and bucket != last_bucket:
            timeline.append((now, share, words))
            last_bucket = bucket

    # How long a labelled lock went undetected: from the moment an English
    # voice entered the locked stream to the first firing. This is the number
    # the room would have felt, so it is the one worth printing.
    in_window = [(at, share) for at, share in fired
                 if stuck_from <= at < stuck_until]
    return {
        "name": path.name,
        "duration": duration,
        "words": watch.words_seen,
        "peak_healthy": peak_healthy,
        "peak_healthy_at": peak_healthy_at,
        "max_stuck": max_stuck,
        "first_fire": in_window[0] if in_window else None,
        "delay": (in_window[0][0] - stuck_from) if in_window else None,
        "fired": fired,
        "drifted": drifted,
        "timeline": timeline,
        "labelled": path.name in STUCK_FROM,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=STUCK_SHARE)
    parser.add_argument("--window-words", type=int, default=WINDOW_WORDS)
    parser.add_argument("--window-seconds", type=float,
                        default=WINDOW_SECONDS)
    parser.add_argument("--min-words", type=int, default=40)
    parser.add_argument("--timeline", action="store_true",
                        help="per-minute score for every log")
    parser.add_argument("--min-duration", type=float, default=60.0,
                        help="skip logs shorter than this many seconds")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    seen: set[Path] = set()
    paths: list[Path] = []
    for directory, pattern in SEARCH:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(pattern)):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            paths.append(path)

    if not paths:
        print("no response logs found")
        return 1

    print(f"threshold {args.threshold:.3f} | window {args.window_words} words "
          f"/ {args.window_seconds:.0f} s | floor {args.min_words} words\n")
    header = (f"{'log':42} {'mins':>5} {'ar_w':>6} {'healthy_max':>11} "
              f"{'stuck_max':>9} {'caught_in':>9} {'fired':>5} {'drift':>5}")
    print(header)
    print("-" * len(header))

    worst_healthy = 0.0
    worst_healthy_log = ""
    lowest_fire = None
    lowest_fire_log = ""
    false_alarms: list[str] = []
    rows = []

    for path in paths:
        result = run(path, args)
        if result["duration"] < args.min_duration:
            continue
        rows.append(result)
        ph = result["peak_healthy"]
        shown_max = f"{result['max_stuck']:.3f}" if result["labelled"] else "-"
        delay = result["delay"]
        shown_delay = f"{delay:.0f}s" if delay is not None else "-"
        print(f"{result['name'][:42]:42} {result['duration']/60:5.1f} "
              f"{result['words']:6d} {ph:11.3f} "
              f"{shown_max:>9} {shown_delay:>9} "
              f"{len(result['fired']):5d} {len(result['drifted']):5d}")
        if ph > worst_healthy:
            worst_healthy, worst_healthy_log = ph, result["name"]
        for at, share in result["fired"]:
            if lowest_fire is None or share < lowest_fire:
                lowest_fire, lowest_fire_log = share, result["name"]
        if not result["labelled"] and result["fired"]:
            false_alarms.append(
                f"{result['name']} at "
                + ", ".join(f"{at/60:.1f}m (share {s:.3f})"
                            for at, s in result["fired"])
            )
        if result["labelled"]:
            for at, share in result["fired"]:
                limits = regions(result["name"])
                region = ("healthy" if at <= limits[0]
                          else "stuck" if limits[1] <= at < limits[2]
                          else "between")
                print(f"{'':44}fired at {at/60:5.1f}m "
                      f"share {share:.3f}  [{region}]")

    print("\n-- separation " + "-" * 50)
    print(f"highest score in a healthy window:    {worst_healthy:.3f} "
          f"({worst_healthy_log})")
    if lowest_fire is not None:
        print(f"lowest score it ever fired at:        {lowest_fire:.3f} "
              f"({lowest_fire_log})")
        if worst_healthy:
            print(f"margin over the worst healthy window: "
                  f"{lowest_fire - worst_healthy:+.3f} "
                  f"({lowest_fire / worst_healthy:.1f}x)")
    peaks = [r["max_stuck"] for r in rows if r["labelled"]]
    if peaks:
        print(f"peak score inside a stuck window:     "
              f"{min(peaks):.3f} - {max(peaks):.3f}")
    delays = [r["delay"] for r in rows if r.get("delay") is not None]
    if delays:
        print(f"time to catch a lock:                 "
              f"{min(delays):.0f}s - {max(delays):.0f}s")
    missed = [r["name"] for r in rows
              if r["labelled"] and r.get("delay") is None]
    if missed:
        print("labelled stuck window never caught:   " + ", ".join(missed))
    if false_alarms:
        print("\nfired in a log with no labelled lock:")
        for line in false_alarms:
            print(f"  {line}")
    else:
        print("\nno firing in any log without a labelled lock.")

    if args.timeline:
        for result in rows:
            print(f"\n### {result['name']}  (per minute: share / window words)")
            for now, share, words in result["timeline"]:
                bar = "#" * int(share * 60)
                print(f"  {now/60:5.1f}m  {share:6.3f}  {words:4d}  {bar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
