"""What the input was actually doing, per minute, out of the recordings.

Written for 2026-11-07, when a talk through the venue mixer paused 10.5
minutes in while it was being transcribed, and the only numbers anyone had
afterwards were the two the event log happened to carry. The recordings hold
the whole answer, and this is the tool that reads it out:

    uv run python tools/level_report.py                    # every recording
    uv run python tools/level_report.py --day 2026-11-07
    uv run python tools/level_report.py --per-minute
    uv run python tools/level_report.py --thresholds       # the calibration

Three measurements, because they answer three different questions.

**Peak per minute** is the quiet warning's question: is this the microphone
we meant? `main.QUIET_PEAK_DB` watches the loudest 100 ms chunk in the last
minute, so that is what gets printed.

**RMS against the silence floor** is the idle auto-stop's question: has the
room been empty for ten minutes? `soniox_client.SILENCE_RMS` is a per-chunk
RMS, and what matters is not the share of chunks below it but the longest
unbroken run of them, because that run is what the timer measures.

**The noise floor**, as low percentiles of chunk RMS, is the check on
lowering the silence floor: a threshold under the line's own hiss would
never see silence at all and the auto-stop would quietly stop existing.

Costs nothing, reaches no network, and prints levels, seconds and counts.
A recording is a verbatim record of a room, so nothing here decodes, plays
or transcribes one - it reads sample amplitudes and nothing else, and the
output carries no word anyone said.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.audio_source import CHUNK_MS, SAMPLE_RATE  # noqa: E402
from app.main import QUIET_PEAK_DB, QUIET_SECONDS  # noqa: E402
from app.session_log import event_path  # noqa: E402
from app.settings import LOG_DIR, ROOT as STATE_ROOT  # noqa: E402
from app.soniox_client import (  # noqa: E402
    IDLE_STOP_SECONDS,
    SILENCE_RMS,
    rms_db,
)

#: Where the operator's own recordings land. Gitignored, never served.
RECORDING_DIR = STATE_ROOT / "recordings"

#: 100 ms at 16 kHz: the chunk the whole pipeline is measured in, so that
#: every figure here lines up with a threshold in the running code.
CHUNK = SAMPLE_RATE * CHUNK_MS // 1000

FULL_SCALE = 32768.0

#: Candidate silence floors for `--thresholds`, in dBFS. The first is what
#: the auto-stop used until 2026-11-08 and the second is what replaced it.
CANDIDATES = (-54.0, -60.0, -66.0, -72.0)


def floor_db(amplitude: float) -> float:
    """dBFS with no floor at all. `rms_db` clamps for the page; this is for
    measuring a noise floor, which is the one case where the clamp is the
    thing being asked about."""
    return -200.0 if amplitude <= 0 else 20 * math.log10(amplitude)


def chunks_of(path: Path) -> tuple[np.ndarray, np.ndarray, float]:
    """Per-chunk peak and RMS in 0..1, plus the duration in seconds."""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        samples = np.frombuffer(handle.readframes(handle.getnframes()),
                                dtype=np.int16)
    audio = samples.astype(np.float32) / FULL_SCALE
    usable = (len(audio) // CHUNK) * CHUNK
    frames = audio[:usable].reshape(-1, CHUNK)
    if frames.size == 0:
        return np.zeros(0), np.zeros(0), 0.0
    peak = np.max(np.abs(frames), axis=1)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    return peak, rms, len(audio) / rate


def longest_run(below: np.ndarray) -> float:
    """The longest unbroken stretch of True, in seconds.

    The one measurement the idle auto-stop actually makes. A share is the
    wrong summary for it: a line that is below the floor 99% of the time but
    never for ten minutes together never pauses, and one that is below it 4%
    of the time can still pause if those chunks are consecutive - which is
    exactly what 2026-11-07_1418 did, reaching 84% of the timeout in a
    session that transcribed 26,495 tokens.
    """
    best = current = 0
    for quiet in below:
        current = current + 1 if quiet else 0
        best = max(best, current)
    return best * CHUNK_MS / 1000


def tokens_of(stem: str) -> int | None:
    """Final tokens in that session's response log, or None if there is none.

    The number that turns "the input was quiet" into "the input was quiet
    and the engine was transcribing it anyway", which is the whole argument
    for `SonioxClient.note_speech`. Counts tokens; reads no text beyond
    checking for the `<end>` marker, which is a boundary and not a word.
    """
    for directory in (LOG_DIR, ROOT / "logs"):
        path = directory / f"live-{stem}.jsonl"
        if not path.is_file():
            continue
        total = 0
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                response = row.get("response") or {}
                for token in response.get("tokens") or ():
                    if token.get("is_final") and token.get("text") != "<end>":
                        total += 1
        return total
    return None


def paused_at(stem: str) -> float | None:
    """When the idle auto-stop fired in that session, in session seconds.

    Reads both event names: `PAUSED` is what it is called from 2026-11-08
    and `IDLE_STOP` is what every log written before then carries.
    """
    for directory in (LOG_DIR, ROOT / "logs"):
        path = event_path(stem, directory)
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("event") in ("PAUSED", "IDLE_STOP"):
                    return row.get("at")
        return None
    return None


def measure(path: Path) -> dict:
    peak, rms, duration = chunks_of(path)
    if peak.size == 0:
        return {"stem": path.stem, "minutes": 0.0, "empty": True}
    peak_db = np.array([floor_db(float(x)) for x in peak])
    per_minute = []
    step = int(60 * 1000 / CHUNK_MS)
    for start in range(0, len(peak_db), step):
        window = peak_db[start:start + step]
        if window.size:
            per_minute.append(round(float(window.max()), 1))
    below = rms < SILENCE_RMS
    return {
        "stem": path.stem,
        "minutes": round(duration / 60, 1),
        "loudest_peak_db": round(float(peak_db.max()), 1),
        "p99_peak_db": round(float(np.percentile(peak_db, 99)), 1),
        "median_peak_db": round(float(np.median(peak_db)), 1),
        "per_minute_peak_db": per_minute,
        "minutes_under_quiet_floor": sum(1 for x in per_minute
                                         if x < QUIET_PEAK_DB),
        "longest_silence_s": round(longest_run(below), 1),
        "share_above_silence_pct": round(float((~below).mean()) * 100, 2),
        "noise_floor_p10_db": round(
            floor_db(float(np.percentile(rms, 10))), 1),
        "noise_floor_p1_db": round(floor_db(float(np.percentile(rms, 1))), 1),
        "final_tokens": tokens_of(path.stem),
        "paused_at_s": paused_at(path.stem),
    }


def thresholds(paths: list[Path]) -> None:
    """The calibration table: longest silence per session, per candidate.

    What to read off it: a session that transcribed must stay well under
    `IDLE_STOP_SECONDS` in the chosen column, a session with no input must
    still reach it, and the chosen floor must sit above every noise floor in
    the last column or the auto-stop stops existing.
    """
    print(f"Longest unbroken silence, seconds, against the "
          f"{IDLE_STOP_SECONDS:.0f} s timeout")
    header = f"{'session':24} {'min':>5} {'tokens':>7}"
    for level in CANDIDATES:
        header += f" {level:>8.0f}"
    print(header + f" {'floor p10':>10}")
    for path in paths:
        peak, rms, duration = chunks_of(path)
        if rms.size == 0:
            continue
        tokens = tokens_of(path.stem)
        row = (f"{path.stem:24} {duration / 60:5.1f} "
               f"{'-' if tokens is None else tokens:>7}")
        for level in CANDIDATES:
            run = longest_run(rms < 10 ** (level / 20))
            mark = "*" if run >= IDLE_STOP_SECONDS else " "
            row += f" {run:7.1f}{mark}"
        row += f" {floor_db(float(np.percentile(rms, 10))):10.1f}"
        print(row)
    print()
    print(f"* reached the timeout and would have paused. In use: "
          f"{rms_db(SILENCE_RMS):.1f} dBFS.")


def report(paths: list[Path], per_minute: bool) -> None:
    print(f"Quiet warning: loudest 100 ms chunk under {QUIET_PEAK_DB:.0f} "
          f"dBFS for {QUIET_SECONDS:.0f} s")
    print(f"Idle auto-stop: chunk RMS under {rms_db(SILENCE_RMS):.1f} dBFS "
          f"for {IDLE_STOP_SECONDS:.0f} s")
    print()
    print(f"{'session':24} {'min':>5} {'loudest':>8} {'p99':>7} "
          f"{'quiet min':>9} {'silence s':>9} {'tokens':>7} {'paused':>7}")
    for path in paths:
        row = measure(path)
        if row.get("empty"):
            print(f"{row['stem']:24} {'(no audio)':>8}")
            continue
        tokens = row["final_tokens"]
        paused = row["paused_at_s"]
        print(f"{row['stem']:24} {row['minutes']:5.1f} "
              f"{row['loudest_peak_db']:8.1f} {row['p99_peak_db']:7.1f} "
              f"{row['minutes_under_quiet_floor']:4d}/"
              f"{len(row['per_minute_peak_db']):<4d} "
              f"{row['longest_silence_s']:9.1f} "
              f"{'-' if tokens is None else tokens:>7} "
              f"{'-' if paused is None else round(paused):>7}")
        if per_minute:
            print("   peak per minute, dBFS:")
            line = "   "
            for index, value in enumerate(row["per_minute_peak_db"]):
                line += f"{value:7.1f}"
                if index % 10 == 9:
                    print(line)
                    line = "   "
            if line.strip():
                print(line)
            print(f"   noise floor p10 {row['noise_floor_p10_db']:.1f} dBFS, "
                  f"p1 {row['noise_floor_p1_db']:.1f} dBFS")
    print()
    print("quiet min: minutes whose loudest chunk sat under the quiet floor.")
    print("silence s: longest unbroken run under the silence floor.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=RECORDING_DIR,
                        help="where the recordings are")
    parser.add_argument("--day", default="",
                        help="only recordings whose name starts with this")
    parser.add_argument("--per-minute", action="store_true",
                        help="print every minute's loudest chunk")
    parser.add_argument("--thresholds", action="store_true",
                        help="the silence-floor calibration table")
    args = parser.parse_args(argv)

    paths = sorted(p for p in args.dir.glob("*.wav")
                   if p.name.startswith(args.day))
    if not paths:
        print(f"no .wav recordings in {args.dir}"
              + (f" starting {args.day}" if args.day else ""))
        return 1
    if args.thresholds:
        thresholds(paths)
    else:
        report(paths, args.per_minute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
