"""Audit the `auto` stream of a saved capture, minute by minute. Offline.

`auto` is the translated half only: Arabic speech shown in English, English
speech shown in Arabic, **never the original**. On 2026-10-24 a live session
appeared to break that promise after about half an hour - the projector showed
English words while English was being spoken - and nothing in the repo could
say whether the fault was in `CaptionState` or on the page. This tool answers
the first half of that question from a response log, with no Soniox call and
no cost.

What it checks, per minute of talk, against the stream `CaptionState` builds:

1. **Paragraphs opened** - the shape of the stream over time.
2. **Wrong side.** The dominant script of the text written into `auto` must be
   the *other* language from the one being spoken. This is the headline number:
   a minute with a non-zero count here is a minute the room was shown words it
   could already hear.
3. **Mislabelled direction.** A paragraph's declared `lang`/`dir` must match
   the script of its own text, or the projector sets Arabic left-to-right in
   the Latin face.
4. **Pass-through tokens.** Soniox occasionally sends a token marked
   `translation` whose `source_language` equals its target - an original in a
   translation's clothing. Those are originals by any honest reading, and they
   land in `auto` through the front door.
5. **Mixed paragraphs.** Characters written into a paragraph in the opposite
   language from the one it is labelled with - the `MIN_SWITCH_CHARS` holding
   buffer being flushed into the block it interrupted.

Everything is a count. The tool never prints caption text: a live log is a
verbatim record of a room (a standing rule), and an audit has no business quoting it.

    uv run python tools/audit_auto.py logs/live-2026-10-24-143245.jsonl
    uv run python tools/audit_auto.py tests/fixtures/run1.jsonl --quiet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.captions import AUTO, CaptionState  # noqa: E402
from app.replay import load_capture  # noqa: E402

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


BUCKET_MS = 60_000
#: Arabic, Arabic Supplement and Extended-A. Enough to tell the two scripts
#: this project shows apart, which is all that is being asked.
ARABIC_RANGES = ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF))


def script_of(text: str) -> str:
    """`"ar"`, `"en"` or `"none"` - which script this text is mostly in."""
    arabic = latin = 0
    for char in text:
        code = ord(char)
        if any(lo <= code <= hi for lo, hi in ARABIC_RANGES):
            arabic += 1
        elif char.isascii() and char.isalpha():
            latin += 1
    if not arabic and not latin:
        return "none"
    return "ar" if arabic >= latin else "en"


def new_bucket() -> dict:
    return {"opened": 0, "spoken_en": 0, "spoken_ar": 0,
            "auto_en": 0, "auto_ar": 0, "wrong_side": 0,
            "passthrough": 0, "mislabelled": 0, "mixed": 0}


class Audit:
    """Replays a capture and keeps the counts above."""

    def __init__(self, path: Path, max_paragraph_chars: int | None = None) -> None:
        self.path = path
        kwargs = {}
        if max_paragraph_chars:
            kwargs["max_paragraph_chars"] = max_paragraph_chars
        self.state = CaptionState(**kwargs)
        self.buckets: dict[int, dict] = {}
        #: Per auto paragraph: chars written in each script, its `lang`, and
        #: the minute it was opened in - so a fault found once the stream is
        #: final can still be attributed to the minute it happened in.
        self.paragraphs: dict[int, dict] = {}
        self._lengths: dict[int, int] = {}
        self.first_wrong_ms: int | None = None
        self.first_mislabelled_ms: int | None = None
        self.responses = 0
        self.duration_ms = 0

    def run(self) -> None:
        _, records = load_capture(self.path)
        for row in records:
            offset = row.get("recv_offset_ms") or 0
            self.duration_ms = max(self.duration_ms, offset)
            bucket = self.buckets.setdefault(offset // BUCKET_MS, new_bucket())
            response = row["response"]
            self._count_tokens(response, bucket)
            self.responses += 1
            self.state.add_response(response)
            self._harvest(bucket, offset)
        self._verdict()

    def _count_tokens(self, response: dict, bucket: dict) -> None:
        """What the engine said: who spoke, and any pass-through tokens."""
        for token in response.get("tokens") or []:
            if not token.get("is_final"):
                continue
            text = token.get("text") or ""
            if not text or text == "<end>":
                continue
            status = token.get("translation_status")
            language = token.get("language")
            if status == "original" and language in ("en", "ar"):
                bucket[f"spoken_{language}"] += len(text)
            elif status == "translation" and language is not None:
                if token.get("source_language") == language:
                    # Target == source: this is the original coming back
                    # dressed as its own translation.
                    bucket["passthrough"] += len(text)

    def _harvest(self, bucket: dict, offset: int) -> None:
        """What `CaptionState` did: the text newly written into `auto`."""
        for para in self.state.streams[AUTO]:
            seen = self._lengths.get(para.id, 0)
            if len(para.final) < seen:
                # A retroactive sentence cut moved text into the next
                # paragraph. Nothing new arrived; re-baseline and move on.
                self._lengths[para.id] = len(para.final)
                continue
            if para.id not in self._lengths:
                bucket["opened"] += 1
                self.paragraphs[para.id] = {"lang": para.lang, "ar": 0, "en": 0,
                                            "minute": offset // BUCKET_MS}
            added = para.final[seen:]
            self._lengths[para.id] = len(para.final)
            if not added:
                continue
            record = self.paragraphs[para.id]
            record["lang"] = para.lang
            script = script_of(added)
            if script == "none":
                continue
            record[script] += len(added)
            bucket[f"auto_{script}"] += len(added)
            # The wrong side: the room is shown the language it just heard.
            spoken = ("en" if bucket["spoken_en"] > bucket["spoken_ar"]
                      else "ar" if bucket["spoken_ar"] else None)
            if spoken is not None and script == spoken:
                bucket["wrong_side"] += len(added)
                if self.first_wrong_ms is None:
                    self.first_wrong_ms = offset

    def _verdict(self) -> None:
        """Per-paragraph checks, once the stream is final.

        Both faults are properties of a whole paragraph rather than of one
        token, so they can only be judged at the end - and are charged to the
        minute the paragraph was opened in, which is when the room saw it.
        """
        for record in self.paragraphs.values():
            bucket = self.buckets.setdefault(record["minute"], new_bucket())
            dominant = "ar" if record["ar"] >= record["en"] else "en"
            if record["lang"] and record["lang"] != dominant:
                bucket["mislabelled"] += 1
                if self.first_mislabelled_ms is None:
                    self.first_mislabelled_ms = record["minute"] * BUCKET_MS
            bucket["mixed"] += min(record["ar"], record["en"])


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", help="path to a JSONL capture or response log")
    parser.add_argument("--max-chars", type=int, default=None,
                        help="override the soft paragraph break")
    parser.add_argument("--quiet", action="store_true",
                        help="the verdict only, without the per-minute table")
    args = parser.parse_args()

    path = Path(args.capture)
    if not path.exists():
        print(NO_CAPTURE.format(path=path), file=sys.stderr)
        return 2

    audit = Audit(path, args.max_chars)
    audit.run()

    if not args.quiet:
        print(f"capture:   {path.name}")
        print(f"responses: {audit.responses}")
        print(f"talk:      {audit.duration_ms / 60000:.1f} min")
        print()
        print(" min  opened  spoke  auto_en  auto_ar  WRONG  passthru  "
              "mislabel  mixed")
        for key in sorted(audit.buckets):
            b = audit.buckets[key]
            spoke = ("en" if b["spoken_en"] > b["spoken_ar"]
                     else "ar" if b["spoken_ar"] else "--")
            print(f"{key:4d} {b['opened']:7d} {spoke:>6} {b['auto_en']:8d} "
                  f"{b['auto_ar']:8d} {b['wrong_side']:6d} "
                  f"{b['passthrough']:9d} {b['mislabelled']:9d} "
                  f"{b['mixed']:6d}")
        print()

    wrong = sum(b["wrong_side"] for b in audit.buckets.values())
    mislabelled = sum(b["mislabelled"] for b in audit.buckets.values())
    mixed = sum(b["mixed"] for b in audit.buckets.values())
    passthrough = sum(b["passthrough"] for b in audit.buckets.values())
    print(f"auto paragraphs:            {len(audit.paragraphs)}")
    print(f"chars on the wrong side:    {wrong}"
          + (f"   first at {audit.first_wrong_ms / 1000:.0f} s"
             if audit.first_wrong_ms is not None else ""))
    print(f"paragraphs mislabelled:     {mislabelled}")
    print(f"chars in the minority side: {mixed}")
    print(f"pass-through tokens:        {passthrough} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
