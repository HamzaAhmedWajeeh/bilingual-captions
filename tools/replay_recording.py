"""Stream a saved recording to the real Soniox, through the real client.

**This spends money.** It is the only tool in here besides
`tools/probe_soniox.py --file` and `eval_run.py` that opens a connection to
Soniox, and unlike those two it uses `app.soniox_client.SonioxClient` and
`app.audio_source.FileSource` - the same two objects a live session uses - so
what comes back is what would have come back on the night.

**Why it exists.** On 2026-10-31 Soniox stopped labelling speech English 18
minutes into a talk, and twice more that afternoon. The question that costs
money to answer is whether that is the audio or the engine: does the same
recording lock again from a fresh connection, does it lock only after a
while, or does it not lock at all? Answering it needs a *window* of a
recording streamed at real time with the session's own config, and nothing
did that - `probe_soniox.py` streams a whole file with its own config and its
own websocket code.

    # the whole recording, real time, the config the live session used
    uv run python tools/replay_recording.py recordings/talk.wav

    # one window, at real time: minutes 24:30 to 30:27
    uv run python tools/replay_recording.py recordings/talk.wav \
        --start 24:30 --end 30:27

    # what it would cost, without connecting
    uv run python tools/replay_recording.py recordings/talk.wav --dry-run

It writes a response log to `logs/replaytest-<stem>-<range>-<stamp>.jsonl`,
in exactly the envelope `app/replay.py` reads, so the result replays offline
and scores through `tools/lang_calibrate.py` like any other session. It
records the audio it sent in `logs/usage.jsonl` as a real billable run,
because it is one.

**It never writes a transcript.** `transcripts/` is the operator's record of
real evenings and a test run has no business in it - the same rule
`CaptionSession.start_live` enforces by being the only thing that can build a
recorder. This tool builds a `CaptionState` so the stream is exercised the
way a session exercises it, and that is all.

WAV only, 16 kHz mono - which is what the server's own recorder writes. A
window is cut with the standard library rather than ffmpeg, so there is one
fewer thing between the file and the number.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import usage  # noqa: E402
from app.audio_source import SAMPLE_RATE, FileSource  # noqa: E402
from app.captions import CaptionState  # noqa: E402
from app.lang_watch import DRIFT, STUCK, LanguageWatch  # noqa: E402
from app.replay import CaptureWriter  # noqa: E402
from app.settings import LOG_DIR, build_context, load_glossary, load_session  # noqa: E402
from app.settings import settings  # noqa: E402
from app.soniox_client import MODELS, active_model, base_config  # noqa: E402
from app.soniox_client import SonioxClient  # noqa: E402


def parse_offset(text: str | None) -> float | None:
    """`90`, `1:30` or `1:30.5` to seconds. None passes through."""
    if text is None or text == "":
        return None
    parts = str(text).split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"not a time offset: {text!r} (try 90, 1:30 or 24:30)"
        ) from exc
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def label(start: float, end: float | None) -> str:
    """A filename-safe name for the window, so two runs never collide."""
    def stamp(value: float) -> str:
        return f"{int(value // 60):02d}m{int(value % 60):02d}"
    return f"{stamp(start)}-{stamp(end) if end is not None else 'end'}"


def slice_wav(source: Path, start: float, end: float | None) -> tuple[Path, float]:
    """Cut `start`..`end` out of a 16 kHz mono WAV into a temporary file.

    Returns the temporary path and its duration. The caller deletes it.
    `FileSource` reads a whole file from its beginning, and teaching it to
    seek would put a new parameter in the live audio path for the sake of an
    offline tool - so the window is made into a file instead.
    """
    with contextlib.closing(wave.open(str(source), "rb")) as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        total = wav.getnframes()
        first = min(total, int(round(start * rate)))
        last = total if end is None else min(total, int(round(end * rate)))
        if last <= first:
            raise SystemExit(
                f"empty window: {start:.1f}s to "
                f"{'end' if end is None else f'{end:.1f}s'} of a "
                f"{total / rate:.1f}s file"
            )
        wav.setpos(first)
        frames = wav.readframes(last - first)
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    handle.close()
    out = Path(handle.name)
    with contextlib.closing(wave.open(str(out), "wb")) as cut:
        cut.setnchannels(channels)
        cut.setsampwidth(width)
        cut.setframerate(rate)
        cut.writeframes(frames)
    return out, (last - first) / rate


def describe(path: Path) -> dict:
    with contextlib.closing(wave.open(str(path), "rb")) as wav:
        return {
            "seconds": wav.getnframes() / wav.getframerate(),
            "rate": wav.getframerate(),
            "channels": wav.getnchannels(),
            "width": wav.getsampwidth(),
        }


async def stream(args, cut: Path, seconds: float, out_path: Path) -> int:
    """One run. Returns a process exit code."""
    details = load_session()
    config = base_config(build_context(load_glossary(), details),
                         model=args.model)
    source = FileSource(cut, realtime=not args.fast, channel=1)
    await source.open()

    states: list[str] = []
    client = SonioxClient(
        source,
        config=config,
        on_state=states.append,
    )
    safe_config = {k: v for k, v in config.items() if k != "context"}
    safe_config["context"] = details.summary()
    writer = CaptureWriter(
        out_path,
        meta={"source": f"replay_recording {args.file}", "config": safe_config},
    )
    state = CaptionState()
    state.set_live(True)
    # `act=False`: there is a connection here and it *could* be reconnected,
    # but the point of a measurement run is to see where the engine goes on
    # its own. A run with the detector acting is a different experiment and
    # is asked for with `--detect`.
    watch = LanguageWatch(act=False)
    signals: list[tuple[str, float, float]] = []

    started = time.monotonic()
    responses = 0
    try:
        async for response in client:
            responses += 1
            writer.add(response,
                       sent_ms=round(client.audio_seconds_sent * 1000))
            state.add_response(response)
            now = time.monotonic() - started
            for signal in watch.note_response(response, now):
                signals.append((signal.kind, now, signal.share))
                mark = "STUCK" if signal.kind == STUCK else "drift"
                print(f"  [{now / 60:5.2f}m] {mark}: share "
                      f"{signal.share:.3f} over {signal.words} words")
                if signal.kind == STUCK:
                    if args.detect:
                        if client.reconfigure(config):
                            watch.reconnected(now)
                            print(f"  [{now / 60:5.2f}m] reconnected through "
                                  "the rollover path")
                    else:
                        # Otherwise one lock reports itself on every response
                        # for the rest of the run.
                        watch.reconnected(now)
            if args.progress and responses % 200 == 0:
                share, words = watch.score(now)
                print(f"  [{now / 60:5.2f}m] {responses} responses, "
                      f"share {share:.3f} over {words} words")
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        streamed = client.audio_seconds_sent
        await client.close()
        await source.close()
        writer.close()

    # The ledger, because this is a billable run and the whole point of that
    # file is that no spend is invisible.
    usage.record_run(
        engine="soniox",
        source="replay_recording",
        audio_seconds=streamed,
        # The model goes in the ledger line too: two runs of the same window
        # differ only by it, and "which one cost that" is the question.
        detail=f"{Path(args.file).name} {label(args.start or 0, args.end)} "
               f"{config['model']}",
    )

    print()
    print(f"model      {config['model']}")
    print(f"streamed   {streamed:.1f} s ({streamed / 60:.2f} min)")
    print(f"responses  {responses}")
    print(f"engine     {client.state}, {client.reconnects} reconnects, "
          f"{client.rollovers} rollovers, "
          f"{client.reconfigures} reconfigures")
    if client.last_error:
        print(f"error      {client.last_error}")
    print(f"log        {out_path}")
    stuck = [s for s in signals if s[0] == STUCK]
    drift = [s for s in signals if s[0] == DRIFT]
    if stuck:
        print(f"LANG_STUCK {len(stuck)} time(s), first at "
              f"{stuck[0][1] / 60:.2f}m (share {stuck[0][2]:.3f})")
    else:
        print("LANG_STUCK never fired")
    if drift:
        print(f"LANG_DRIFT first at {drift[0][1] / 60:.2f}m")
    print()
    print("Score it per minute with:")
    print(f"  uv run python tools/lang_calibrate.py --timeline "
          f"2>&1 | grep -A40 {out_path.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stream a recording to Soniox. This spends money.")
    parser.add_argument("file", help="a 16 kHz mono WAV recording")
    parser.add_argument("--start", type=parse_offset, default=None,
                        help="window start, e.g. 24:30 (default: the start)")
    parser.add_argument("--end", type=parse_offset, default=None,
                        help="window end, e.g. 30:27 (default: the end)")
    parser.add_argument("--out", default=None,
                        help="response log path (default: under logs/)")
    # The reason this tool exists is to compare one engine build against
    # another on the same audio, so the model has to be sayable per run and
    # not only per `.env`.
    parser.add_argument("--model", default=None, choices=MODELS,
                        help=f"speech model (default: {active_model()})")
    parser.add_argument("--fast", action="store_true",
                        help="stream as fast as the file reads. Changes what "
                             "the engine sees - real time is the experiment")
    parser.add_argument("--detect", action="store_true",
                        help="reconnect on LANG_STUCK, the way a live "
                             "session now does, instead of only reporting it")
    parser.add_argument("--progress", action="store_true",
                        help="a line every 200 responses")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what it would stream and cost, and stop")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    source = Path(args.file)
    if not source.is_file():
        print(f"no such file: {source}", file=sys.stderr)
        return 2
    if source.suffix.lower() not in (".wav", ".wave"):
        print(f"WAV only, got {source.suffix}. The server's recorder writes "
              f"WAV; convert anything else first.", file=sys.stderr)
        return 2

    info = describe(source)
    if info["rate"] != SAMPLE_RATE or info["channels"] != 1:
        print(f"expected 16 kHz mono, got {info['rate']} Hz "
              f"{info['channels']} ch", file=sys.stderr)
        return 2

    start = args.start or 0.0
    end = args.end
    window = (end if end is not None else info["seconds"]) - start
    rate = settings.soniox_rate_per_minute or 0.0
    print(f"file     {source}  ({info['seconds'] / 60:.2f} min, "
          f"{info['rate']} Hz mono)")
    print(f"model    {args.model or active_model()}")
    print(f"window   {start / 60:.2f}m to "
          f"{'end' if end is None else f'{end / 60:.2f}m'} "
          f"= {window / 60:.2f} min")
    if rate:
        print(f"cost     about ${window / 60 * rate:.3f} at "
              f"${rate:.4f}/min")
    else:
        print("cost     SONIOX_RATE_PER_MINUTE is not set, so no estimate")
    if args.dry_run:
        print("\ndry run: nothing connected, nothing spent")
        return 0

    if not settings.soniox_api_key:
        print("\nSONIOX_API_KEY is empty. Copy .env.example to .env and set "
              "it.", file=sys.stderr)
        return 2

    if not args.yes:
        print(f"\nThis will stream {window / 60:.1f} minutes of audio to "
              "Soniox and be billed for it.")
        try:
            answer = input("Type yes to go ahead: ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            print("stopped; nothing spent")
            return 1

    cut, seconds = slice_wav(source, start, end)
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    # The model is in the filename because the whole point of this tool now
    # is two runs of the same window that differ only by it, and a stamp
    # tells them apart only if you remember which stamp was which.
    out_path = Path(args.out) if args.out else (
        LOG_DIR / f"replaytest-{source.stem.replace(' ', '-')}"
                  f"-{label(start, end)}-{args.model or active_model()}"
                  f"-{stamp}.jsonl"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"\nstreaming {seconds / 60:.2f} min at "
          f"{'real time' if not args.fast else 'file speed'}...\n")
    try:
        return asyncio.run(stream(args, cut, seconds, out_path))
    finally:
        cut.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
