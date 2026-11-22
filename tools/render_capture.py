"""Render a saved capture through CaptionState, as the audience would see it.

This is the Phase 2 acceptance check - "rendering a fixture to text reads
sensibly" - in runnable form, and it costs no Soniox credit: everything comes
from a JSONL capture.

    uv run python tools/render_capture.py tests/fixtures/run1.jsonl
    uv run python tools/render_capture.py tests/fixtures/run5-english.jsonl --lang ar
    uv run python tools/render_capture.py tests/fixtures/run4-saudi.jsonl --stats
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.captions import CaptionState  # noqa: E402
from app.replay import load_capture, responses  # noqa: E402

#: Printed when the file named on the command line is not there. Spelled out
#: rather than left as "no such file", because on a fresh clone the usual
#: reason is that **no capture is committed** - a capture is a verbatim
#: transcript of whatever was said in front of the microphone, so none ship
#: with this repo. Somebody following a command out of the README deserves to
#: be told that rather than left wondering what they broke.
NO_CAPTURE = """no such capture: {path}

  Captures are not committed - they are verbatim transcripts of whatever was
  recorded, so none ship with this repository, and tests/fixtures/*.jsonl is
  gitignored. Everything that reads one is opt-in.

  To make one:

      uv run python tools/probe_soniox.py --file talk.m4a --out {path}

  which streams the audio to Soniox and costs about $0.20 an hour. Or copy a
  response log out of logs/ after a live session: same format, already paid
  for. See tests/captures.py for the names the test suite looks for.
"""



def force_utf8_output() -> None:
    """Arabic to a redirected stdout dies on cp1252 without this (Phase 1)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    force_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", help="path to a JSONL capture")
    parser.add_argument("--lang", choices=["ar", "en", "both"], default="both")
    parser.add_argument("--max-chars", type=int, default=None,
                        help="override the soft paragraph break")
    parser.add_argument("--stats", action="store_true",
                        help="paragraph/endpoint counts instead of the text")
    args = parser.parse_args()

    path = Path(args.capture)
    if not path.exists():
        print(NO_CAPTURE.format(path=path), file=sys.stderr)
        return 2

    kwargs = {}
    if args.max_chars:
        kwargs["max_paragraph_chars"] = args.max_chars
    state = CaptionState(**kwargs)

    endpoints = 0
    for response in responses(path):
        for token in response.get("tokens") or []:
            if (token.get("text") == "<end>"
                    and token.get("translation_status") != "translation"):
                endpoints += 1
        state.add_response(response)

    meta, records = load_capture(path)
    translation = meta.get("config", {}).get("translation", {})

    if args.stats:
        print(f"capture:     {path.name}")
        print(f"audio:       {meta.get('audio', '?')}")
        print(f"translation: {translation}")
        print(f"responses:   {len(records)}")
        print(f"endpoints:   {endpoints}")
        print(f"seq:         {state.seq}")
        for language in ("ar", "en"):
            paras = state.streams[language]
            chars = sum(len(p.final) for p in paras)
            longest = max((len(p.final) for p in paras), default=0)
            print(f"  {language}: {len(paras):>3} paragraphs, {chars:>5} chars, "
                  f"longest {longest}")
        return 0

    languages = ("ar", "en") if args.lang == "both" else (args.lang,)
    for language in languages:
        print("=" * 72)
        print(f"{language.upper()} stream - {len(state.streams[language])} paragraphs")
        print("=" * 72)
        for para in state.streams[language]:
            text = (para.final + para.partial).strip()
            if text:
                print(f"[{para.id}] {text}")
                print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
