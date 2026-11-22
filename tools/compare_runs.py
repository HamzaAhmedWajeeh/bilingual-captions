"""Put two response logs side by side, one metric per row.

Built for one question and it will be asked again: the same audio was
streamed to two engine builds, so what changed? Everything here is counted
off the token labels and the envelope timings, which means it works on any
log `app/replay.py` can read - a live session, a paid replay, a committed
fixture - and it reaches no network and costs nothing.

    uv run python tools/compare_runs.py logs/a.jsonl logs/b.jsonl
    uv run python tools/compare_runs.py --names v5 v6 logs/a.jsonl logs/b.jsonl

The rows are the ones the live-run measurements compares, plus the three the
v6 changelog made worth watching: how late a caption is, how far behind the
audio the translated half runs, and how much text the engine gathers before
it translates.

**It prints no speech.** Every number below is a count, a share or a number
of seconds; the only strings are language codes, filenames and the names of
the metrics. Same rule as `tools/lang_calibrate.py`, and for the same reason:
a measurement of a talk must be publishable where the talk is not.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.lang_watch import (  # noqa: E402
    ARABIC_LETTERS, DRIFT, END_TOKEN, LATIN_LETTERS, STUCK, TRANSLATION,
    WHITESPACE, LanguageWatch, is_function_word, is_translation,
)

#: The floor `tools/lang_calibrate.py` scores above, kept the same so a peak
#: window share printed here is the same number that table prints.
MIN_WINDOW_WORDS = 40

#: A minute counts as "Arabic with no Latin in it" only once it holds enough
#: Arabic to mean anything. the live-run measurements uses 20 words.
DRY_MINUTE_WORDS = 20

#: A backwards jump in `end_ms` bigger than this is a *new* Soniox stream
#: whose clock starts again at zero - a reconnect or the rollover. Same rule
#: and same constant as `transcript.EPOCH_JUMP_MS`, and it is load-bearing
#: here: the live 15:21 log reconnected at 20:14, and without the guard its
#: caption lag read a p90 of 1214 seconds, which is the length of the first
#: connection rather than anything the room waited for.
EPOCH_JUMP_SECONDS = 5.0


def rows(path: Path):
    """The envelope rows of a capture, in order, responses only."""
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


def meta(path: Path) -> dict:
    """The capture's first line, which carries the config it was made with."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                return {}
            if "meta" in row:
                return row["meta"] or {}
            return {}
    return {}


def percentile(values: list[float], fraction: float) -> float | None:
    """A plain nearest-rank percentile. No interpolation, no numpy."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


class Words:
    """Reassembles words out of Soniox's sub-word fragments, per language.

    The same trap `app/lang_watch.py` documents: ` compan` then `y` is one
    word, and a word only exists once the whitespace after it arrives.
    Counting fragments instead puts single Arabic letters at the top of the
    frequency table.
    """

    def __init__(self) -> None:
        self.pending: dict[str, str] = {}
        self.minute: dict[str, int] = {}
        self.words = {"en": 0, "ar": 0}
        self.latin_letters = 0
        self.arabic_letters = 0
        self.transliterated = 0
        self.scored = 0
        #: minute -> [arabic words, words holding a Latin letter]
        self.buckets: dict[int, list[int]] = {}

    def feed(self, lang: str, text: str, minute: int) -> None:
        for piece in WHITESPACE.split(text):
            if not piece:
                continue
            if piece.isspace():
                self._flush(lang)
                continue
            if not self.pending.get(lang):
                self.minute[lang] = minute
            self.pending[lang] = self.pending.get(lang, "") + piece

    def finish(self) -> None:
        for lang in list(self.pending):
            self._flush(lang)

    def _flush(self, lang: str) -> None:
        word = self.pending.pop(lang, "")
        minute = self.minute.pop(lang, 0)
        if not word:
            return
        self.words[lang] = self.words.get(lang, 0) + 1
        if lang != "ar":
            return
        latin = len(LATIN_LETTERS.findall(word))
        arabic = len(ARABIC_LETTERS.findall(word))
        if not latin and not arabic:
            return
        self.latin_letters += latin
        self.arabic_letters += arabic
        bucket = self.buckets.setdefault(minute, [0, 0])
        bucket[0] += 1
        bucket[1] += bool(latin)
        if not arabic:
            return
        self.scored += 1
        self.transliterated += is_function_word(word)

    @property
    def latin_share(self) -> float:
        total = self.latin_letters + self.arabic_letters
        return self.latin_letters / total if total else 0.0

    @property
    def transliteration(self) -> float:
        return self.transliterated / self.scored if self.scored else 0.0

    def dry_minutes(self) -> tuple[int, int]:
        """(minutes of Arabic with no Latin at all, minutes with any Arabic).

        The signature of a locked stream in the 2026-10-31 logs: a stretch of
        Arabic-labelled speech in which not one Latin letter survives.
        """
        wet = [m for m, (words, _) in self.buckets.items()
               if words >= DRY_MINUTE_WORDS]
        dry = [m for m in wet if self.buckets[m][1] == 0]
        return len(dry), len(wet)


def measure(path: Path) -> dict:
    """Everything one log can say about itself. No text, only numbers."""
    words = Words()
    watch = LanguageWatch(act=False)
    peak = 0.0
    fired: list[tuple[float, float]] = []
    drifted: list[float] = []

    responses = 0
    tokens = 0
    finals = 0
    ends = 0
    passthrough = 0
    translation_finals = {"ar": 0, "en": 0}
    duration = 0.0
    frontier = 0.0
    epoch = 0.0
    last_end = 0.0
    # How late a caption is: when a word's final form arrived, against when
    # it was spoken. The audio is streamed at real time from the start of the
    # window, so `end_ms` is also its wall-clock moment.
    final_lag: list[float] = []
    # How far behind the spoken audio the translated half runs. A translated
    # token carries no timing of its own, so the reference is the newest
    # originally-timed speech that had been finalized when it arrived.
    translation_lag: list[float] = []
    # How much text arrives per response that carries any translation, which
    # is what the v6 changelog's "adjusts how much speech it gathers before
    # translating" would move.
    translation_chars: list[int] = []

    for row in rows(path):
        responses += 1
        now = row.get("recv_offset_ms", 0) / 1000.0
        duration = max(duration, now)
        response = row["response"]
        for signal in watch.note_response(response, now):
            if signal.kind == STUCK:
                fired.append((now, signal.share))
                # Nothing reconnects here, so tell the watcher anyway or one
                # lock reports itself on every response for the rest of the
                # run and the count says nothing.
                watch.reconnected(now)
            elif signal.kind == DRIFT:
                drifted.append(now)
        share, seen = watch.score(now)
        if seen >= MIN_WINDOW_WORDS:
            peak = max(peak, share)

        chars = 0
        for token in response.get("tokens", ()) or ():
            tokens += 1
            text = token.get("text") or ""
            final = bool(token.get("is_final"))
            finals += final
            if text.strip() == END_TOKEN:
                ends += final
                continue
            status = token.get("translation_status")
            source = token.get("source_language")
            lang = token.get("language")
            # A token marked `translation` whose source language is its own
            # target is an original that came back through the translator -
            # `CaptionState._is_translation` and `lang_watch.is_translation`
            # both treat it as one, and it is the only way an original has
            # ever reached the projector's translated-only view.
            if final and status == TRANSLATION and source is not None \
                    and source == lang:
                passthrough += 1
            if not final:
                continue
            if is_translation(token):
                if source in translation_finals:
                    translation_finals[source] += 1
                chars += len(text)
                if frontier:
                    translation_lag.append(now - frontier)
                continue
            end_ms = token.get("end_ms")
            if end_ms is not None:
                end = end_ms / 1000.0
                if end + epoch < last_end - EPOCH_JUMP_SECONDS:
                    # A new connection, counting from zero again. Carry on
                    # from where the last one ended rather than rewinding.
                    epoch = last_end - end
                end += epoch
                last_end = max(last_end, end)
                frontier = max(frontier, end)
                final_lag.append(now - end)
            if lang in ("ar", "en"):
                words.feed(lang, text, int(now // 60))
        if chars:
            translation_chars.append(chars)

    words.finish()
    dry, wet = words.dry_minutes()
    config = (meta(path).get("config") or {})
    return {
        "name": path.name,
        "model": config.get("model") or "?",
        "minutes": duration / 60.0,
        "responses": responses,
        "tokens": tokens,
        "finals": finals,
        "resends": tokens - finals,
        "ends": ends,
        "words_en": words.words.get("en", 0),
        # Words carrying an Arabic letter, which is what `lang_watch` scores
        # and what the live-run measurements counts. The ar-labelled words of
        # pure Latin are a line of their own below, because they are the
        # surviving English terms and their *absence* is the drift signal.
        "words_ar": words.scored,
        "words_ar_latin": words.words.get("ar", 0) - words.scored,
        "latin_share": words.latin_share,
        "transliteration": words.transliteration,
        # The count behind that share, printed beside it because the share is
        # tiny in healthy speech and a ratio of two small integers reads as a
        # trend. Five words in 36 minutes against one is not a fivefold rise
        # in anything.
        "transliterated": words.transliterated,
        "peak": peak,
        "stuck": fired,
        "drift": drifted,
        "passthrough": passthrough,
        "translation_ar": translation_finals["ar"],
        "translation_en": translation_finals["en"],
        "dry_minutes": (dry, wet),
        "lag_median": percentile(final_lag, 0.5),
        "lag_p90": percentile(final_lag, 0.9),
        "tlag_median": percentile(translation_lag, 0.5),
        "tlag_p90": percentile(translation_lag, 0.9),
        "tchars": (statistics.mean(translation_chars)
                   if translation_chars else None),
        "tbursts": len(translation_chars),
    }


def seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f} s"


def table(results: list[dict], names: list[str]) -> list[tuple[str, ...]]:
    def every(key, form):
        return tuple(form(r[key]) for r in results)

    def signal(key):
        out = []
        for result in results:
            hits = result[key]
            out.append("never" if not hits else
                       f"{len(hits)}x, first "
                       f"{(hits[0][0] if key == 'stuck' else hits[0]) / 60:.1f}m")
        return tuple(out)

    return [
        ("model", every("model", str)),
        ("minutes streamed", every("minutes", lambda v: f"{v:.2f}")),
        ("responses", every("responses", lambda v: f"{v}")),
        ("tokens delivered", every("tokens", lambda v: f"{v}")),
        ("final tokens", every("finals", lambda v: f"{v}")),
        ("non-final re-sends", every("resends", lambda v: f"{v}")),
        ("endpoint tokens", every("ends", lambda v: f"{v}")),
        ("original words tagged en", every("words_en", lambda v: f"{v}")),
        ("original words tagged ar", every("words_ar", lambda v: f"{v}")),
        ("of those, pure Latin script",
         every("words_ar_latin", lambda v: f"{v}")),
        ("Latin share of ar originals",
         every("latin_share", lambda v: f"{v * 100:.1f}%")),
        ("transliteration score",
         every("transliteration", lambda v: f"{v * 100:.3f}%")),
        ("transliterated words behind it",
         every("transliterated", lambda v: f"{v}")),
        ("peak window share", every("peak", lambda v: f"{v:.3f}")),
        ("LANG_STUCK", signal("stuck")),
        ("LANG_DRIFT", signal("drift")),
        ("pass-through tokens", every("passthrough", lambda v: f"{v}")),
        ("final translation tokens, ar source",
         every("translation_ar", lambda v: f"{v}")),
        ("final translation tokens, en source",
         every("translation_en", lambda v: f"{v}")),
        ("minutes with 20+ ar words and 0% Latin",
         every("dry_minutes", lambda v: f"{v[0]} of {v[1]}")),
        ("caption lag, median", every("lag_median", seconds)),
        ("caption lag, p90", every("lag_p90", seconds)),
        ("translation behind audio, median", every("tlag_median", seconds)),
        ("translation behind audio, p90", every("tlag_p90", seconds)),
        ("responses carrying translation", every("tbursts", lambda v: f"{v}")),
        ("characters of translation per such response",
         every("tchars", lambda v: "-" if v is None else f"{v:.1f}")),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", help="response logs to compare")
    parser.add_argument("--names", nargs="+", default=None,
                        help="column headings (default: the model names)")
    parser.add_argument("--markdown", action="store_true",
                        help="print a markdown table, ready for docs/")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    paths = [Path(name) for name in args.logs]
    for path in paths:
        if not path.is_file():
            print(f"no such log: {path}", file=sys.stderr)
            return 2
    results = [measure(path) for path in paths]
    names = args.names or [result["model"] for result in results]
    if len(names) != len(results):
        print("--names needs one name per log", file=sys.stderr)
        return 2

    for result, name in zip(results, names):
        print(f"{name:12} {result['name']}")
    print()

    label_width = max(len(label) for label, _ in table(results, names))
    width = max(12, max(len(name) for name in names) + 2)
    if args.markdown:
        print("| | " + " | ".join(names) + " |")
        print("| --- |" + " --- |" * len(names))
        for label, values in table(results, names):
            print(f"| {label} | " + " | ".join(values) + " |")
    else:
        print(f"{'':{label_width}}  " + "".join(f"{n:>{width}}"
                                                for n in names))
        print("-" * (label_width + 2 + width * len(names)))
        for label, values in table(results, names):
            print(f"{label:{label_width}}  "
                  + "".join(f"{v:>{width}}" for v in values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
