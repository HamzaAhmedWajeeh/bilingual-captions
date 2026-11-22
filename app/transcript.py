"""The readable record of a talk: three markdown files and a stats sheet.

What this is for: after the session the organiser wants a summary, a
recap, an infographic and some numbers. None of that can be built from
`logs/live-*.jsonl` without a tool, and none of it should need one.

**Only a real live session writes anything.** Replay, the microphone level
check and the audio-file check all run through `CaptionState` exactly the way
a live run does, which is the whole point of them - so nothing here may key
off "did captions appear". `CaptionSession.start_live` is the only caller
that builds a recorder, and `start_replay`/`start_audio` have no path to one.
That is the enforcement; the checkbox on `/admin` only decides whether the
live run gets one.

Three design points worth keeping:

- **The text is the caption text.** Paragraphs are harvested from
  `CaptionState` once they close, which is the same text the screen showed
  and the same text Soniox finalized - not the raw token stream, and not the
  grey partials, which are text the engine had not committed to.

- **It is written as it goes.** A closed paragraph is appended to all three
  files immediately and flushed, so a power cut at minute 90 costs the last
  sentence rather than the talk. The headers and the stats are written on
  Stop, by rewriting each file from the segments held in memory - which makes
  finalizing idempotent, so the idle pause can take a checkpoint and Stop can
  do it again without duplicating a line.

- **It is clockless, like `captions.py`.** Every entry point takes the
  elapsed seconds from the caller. A live run passes wall time; rebuilding
  from a saved log passes the log's own `recv_offset_ms`, which is what lets
  the 2026-10-17 session be turned into a transcript months later and come
  out with the timestamps it had on the night.

The folder is `transcripts/<YYYY-MM-DD_HHMM>/`, gitignored, and reachable
only through `/admin` behind its token. The header carries the date, the
duration and the languages; the talk title is included only when the operator
asks for it, and speaker names never are.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from app.captions import AUTO, CaptionState

#: The files a finished transcript folder holds. `/admin` offers exactly
#: these for download and the zip contains exactly these, so a stray file in
#: the folder is never served.
ENGLISH = "english.md"
ARABIC = "arabic.md"
BILINGUAL = "bilingual.md"
STATS_MD = "stats.md"
STATS_JSON = "stats.json"
FILES: tuple[str, ...] = (ENGLISH, ARABIC, BILINGUAL, STATS_MD, STATS_JSON)

#: A transcript folder's name. Checked against this before any path is built
#: from a request, so `..` and absolute paths are rejected by shape rather
#: than by sanitizing - `/admin` is behind a token, but a traversal there
#: would still read any file on the laptop.
FOLDER_NAME = re.compile(r"\A\d{4}-\d{2}-\d{2}_\d{4}(-\d+)?\Z")

#: Two spans of the same language closer together than this are one stretch
#: of speech. Soniox emits a span per sub-word token, so without this every
#: word would count as its own stretch and "longest uninterrupted stretch"
#: would measure syllables.
GAP_TOLERANCE_MS = 400

#: A backwards jump in `start_ms` bigger than this means a *new* Soniox
#: stream - a reconnect, or the 280-minute rollover - whose clock starts
#: again at zero. Without the guard a two-hour talk's second connection
#: would overlay the first and halve the measured speech time.
EPOCH_JUMP_MS = 5_000

#: How many terms the stats sheet lists.
TOP_TERMS = 15

#: A Latin-script word inside Arabic speech is a technical term Soniox was
#: told to keep in English (`translation_terms`, confirmed live on run7), so
#: it is worth counting even when it is not in the glossary. Two characters
#: minimum: a stray initial is not a term.
LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'’+./-]*[A-Za-z0-9]")

#: Any Arabic letter. Used to decide a glossary term's word boundaries, and
#: by the tests to assert that `english.md` carries no Arabic at all.
ARABIC_CHAR = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")

#: Latin words that are not terms however often they are said, compared
#: case-folded so a sentence-initial "Do" is caught with "do". The
#: capitalisation rule in `looks_like_a_term` does most of this work; this
#: list is for the ordinary words that still get past it, which in practice
#: means the ones that start a sentence.
NOT_TERMS = frozenset({
    "ok", "okay", "yeah", "yes", "no", "the", "and", "or", "so", "but",
    "a", "an", "is", "it", "we", "you", "i", "to", "of", "in", "on",
    # Added after the 2026-10-24 session, whose stats sheet listed `do`,
    # `can`, `like`, `time`, `that` and `they` as technical terms.
    "do", "does", "did", "done", "can", "cannot", "could", "should",
    "would", "will", "shall", "may", "might", "must",
    "this", "that", "these", "those", "they", "them", "their", "there",
    "then", "than", "he", "she", "him", "her", "his", "its", "my", "our",
    "me", "us", "am", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "like", "likes", "liked", "time", "times",
    "what", "when", "where", "which", "who", "whom", "whose", "why",
    "how", "if", "else", "because", "about", "from", "with", "without",
    "for", "at", "by", "as", "all", "any", "some", "every", "each",
    "more", "most", "less", "much", "many", "very", "really", "just",
    "now", "here", "one", "two", "three", "first", "last", "next",
    "also", "too", "only", "even", "still", "again", "well", "good",
    "bad", "big", "small", "new", "old", "same", "other", "another",
    "thing", "things", "people", "way", "ways", "day", "days", "year",
    "years", "let", "lets", "get", "got", "go", "going", "come", "came",
    "see", "saw", "say", "said", "know", "knew", "think", "thought",
    "want", "need", "make", "made", "take", "took", "give", "gave",
    "use", "used", "using", "work", "works", "working", "back", "up",
    "down", "out", "over", "under", "between", "through", "after",
    "before", "while", "during", "asking", "ask", "asked", "please",
    "thanks", "thank", "sorry", "hello", "hi", "bye", "sure", "right",
    "left", "true", "false", "maybe", "perhaps", "actually", "basically",
    "anyone", "someone", "everyone", "nobody", "something", "anything",
    "everything", "nothing", "everybody", "anybody",
})

#: A contraction's suffix. `I'm` and `We're` are the pronoun, not a term, so
#: the stopword test is applied to what is left of the word in front of the
#: apostrophe as well as to the whole of it.
CONTRACTION = re.compile(r"['’](?:m|s|re|ve|ll|d|t)\Z", re.IGNORECASE)

#: What a term looks like when it is not in the glossary. A technical term or
#: a proper noun carries a capital - `RAG`, `LLM`, `MCP`, `PHP`, `ChatGPT`,
#: `DeepSeek`, `Anthropic` - and an ordinary English word spoken inside an
#: Arabic sentence does not. Before this rule the 2026-10-24 stats sheet
#: listed `do`, `can`, `like`, `time`, `that` and `they` among the terms of a
#: talk about language models, which is worse than listing nothing: the sheet
#: is read as "what this talk was about". A lower-case term that genuinely
#: matters belongs in the glossary, which is the operator's own list and is
#: matched whatever its case.
TERM_SHAPE = re.compile(r"\A(?:[A-Z0-9][A-Za-z0-9'’+./-]*[A-Za-z0-9]"
                        r"|[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*)\Z")

LANGUAGE_NAMES = {"ar": "Arabic", "en": "English"}


# ---------------------------------------------------------------------------
# What a transcript is made of
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    """One closed caption paragraph, in one language, with its timestamp.

    `spoken` is the half of this that cannot be recovered later: within the
    `en` stream an English turn and the English translation of an Arabic turn
    are both just English text, and `bilingual.md` has to be able to tell
    them apart to put an original above its translation.
    """

    at: float
    stream: str
    text: str
    spoken: bool
    para_id: int
    notice: bool = False

    def as_row(self) -> dict:
        return {"at": round(self.at, 2), "stream": self.stream,
                "text": self.text, "spoken": self.spoken,
                "id": self.para_id, "notice": self.notice}


def clock(seconds: float) -> str:
    """`mm:ss` from the start of the talk, or `h:mm:ss` past an hour."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def duration_text(seconds: float) -> str:
    """How long the talk ran, for a human reading the header."""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# Measuring the speech
# ---------------------------------------------------------------------------


@dataclass
class SpeechClock:
    """How long each language was actually spoken for.

    Fed the *original* tokens only. A translation token carries no `start_ms`
    at all (checked across all five captures), which is the engine telling us
    the same thing this class assumes: translated text is not speech and has
    no duration of its own.

    Spans arrive per sub-word token and overlap freely, so they are merged -
    same language, less than `GAP_TOLERANCE_MS` apart - and the merged list is
    what answers all three questions: minutes per language, how many times the
    room changed language, and the longest stretch nobody interrupted.
    """

    spans: list[tuple[int, int, str]] = field(default_factory=list)
    _offset: int = 0
    _last_end: int = 0

    def add(self, start_ms: int, end_ms: int, language: str) -> None:
        if language not in LANGUAGE_NAMES or end_ms < start_ms:
            return
        if start_ms + self._offset < self._last_end - EPOCH_JUMP_MS:
            # A new Soniox stream, counting from zero again. Carry on from
            # where the last one ended rather than rewinding the talk.
            self._offset = self._last_end - start_ms
        start = start_ms + self._offset
        end = end_ms + self._offset
        self._last_end = max(self._last_end, end)
        self.spans.append((start, end, language))

    def merged(self) -> list[tuple[int, int, str]]:
        """Contiguous stretches, in time order, one language each."""
        out: list[tuple[int, int, str]] = []
        for start, end, language in sorted(self.spans):
            if out:
                last_start, last_end, last_lang = out[-1]
                if language == last_lang and start <= last_end + GAP_TOLERANCE_MS:
                    out[-1] = (last_start, max(last_end, end), language)
                    continue
                if start < last_end:
                    # The other language overlapping this one: Soniox has
                    # relabelled the tail of a span. Start where the last
                    # one stopped rather than counting the overlap twice.
                    start = last_end
                    if end <= start:
                        continue
            out.append((start, end, language))
        return out

    def runs(self) -> list[tuple[int, int, str]]:
        """Stretches the room held one language for, pauses included.

        The difference from `merged` is what a pause does. `merged` breaks on
        one, because it is measuring how long somebody was actually talking;
        a run breaks only when the *other* language starts, because it is
        measuring how long the talk stayed in one language. Drawing breath
        does not hand the floor over.
        """
        out: list[tuple[int, int, str]] = []
        for start, end, language in self.merged():
            if out and out[-1][2] == language:
                out[-1] = (out[-1][0], max(out[-1][1], end), language)
                continue
            out.append((start, end, language))
        return out

    def seconds_by_language(self) -> dict[str, float]:
        """Minutes actually spoken, which is `merged` and not `runs`: the
        silence in the middle of a ten-minute answer is not speech."""
        totals = {name: 0.0 for name in LANGUAGE_NAMES}
        for start, end, language in self.merged():
            totals[language] += (end - start) / 1000
        return totals

    def switches(self) -> dict[str, int]:
        """How many times the room changed language, in each direction."""
        counts = {"ar_to_en": 0, "en_to_ar": 0}
        previous: str | None = None
        for _start, _end, language in self.runs():
            if previous is not None and language != previous:
                counts[f"{previous}_to_{language}"] += 1
            previous = language
        return counts

    def longest_stretch(self) -> tuple[str, float]:
        """The longest stretch the talk stayed in one language.

        Measured off `runs`, so a speaker pausing for breath does not end
        their turn. Against `merged` this answered "the longest anyone spoke
        without stopping", which on `run1` - five minutes of Arabic and no
        English at all - came out as 11 seconds and told nobody anything.
        """
        best_lang, best = "", 0.0
        for start, end, language in self.runs():
            seconds = (end - start) / 1000
            if seconds > best:
                best_lang, best = language, seconds
        return best_lang, best


# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------


def _glossary_pattern(term: str) -> re.Pattern[str]:
    """Match `term` as a whole word in either script.

    `\\b` is defined against Latin word characters, so it does nothing
    useful on either side of an Arabic term - `المحولات` sits between Arabic
    letters, which `\\b` does not consider boundaries. Lookarounds against
    "any letter at all" work for both.
    """
    return re.compile(
        r"(?<![\w؀-ۿ])" + re.escape(term)
        + r"(?![\w؀-ۿ])",
        re.IGNORECASE,
    )


def count_terms(texts: Mapping[str, str], glossary: Sequence[str],
                limit: int = TOP_TERMS) -> list[dict]:
    """The technical terms this talk actually mentioned, most first.

    Two sources, because neither alone is enough. The glossary is what the
    engine was *told* to listen for, so it names the terms worth counting but
    says nothing about which were used. A Latin-script word sitting inside
    Arabic speech is the other half: that is a term Soniox kept in English on
    purpose (`translation_terms`), and it may be one nobody thought to put in
    the glossary.

    The second source needs a shape test, or it is not a term list at all.
    English words turn up inside Arabic speech for ordinary reasons - a
    speaker reaching for a word, or Soniox labelling a whole English clause
    `ar` - so the 2026-10-24 sheet listed `do`, `can`, `like`, `time`, `that`
    and `they` alongside `LLM` and `ChatGPT`. A term outside the glossary now
    has to carry a capital (`TERM_SHAPE`) and not be an ordinary word
    (`NOT_TERMS`); a lower-case term that matters belongs in the glossary.

    Counted locally against the caption text. No external API, by the same
    rule as everything else here: a transcript is a verbatim record of a
    room, and it does not leave the laptop.
    """
    english = texts.get("en", "")
    arabic = texts.get("ar", "")
    both = f"{english}\n{arabic}"

    counts: Counter[str] = Counter()
    claimed: set[str] = set()
    for term in glossary:
        term = str(term).strip()
        if not term:
            continue
        found = len(_glossary_pattern(term).findall(both))
        if found:
            counts[term] += found
        claimed.add(term.casefold())

    # Latin words kept inside the Arabic captions. Skipped when the glossary
    # already claimed the word, or it would be counted twice under two
    # spellings ("RAG" and "rag"), and skipped unless it carries a capital:
    # see `TERM_SHAPE`. The whole word must match, so a capital inside an
    # otherwise ordinary word is not enough.
    for word in LATIN_WORD.findall(arabic):
        folded = word.casefold()
        if folded in claimed:
            continue
        if folded in NOT_TERMS or CONTRACTION.sub("", folded) in NOT_TERMS:
            continue
        if not TERM_SHAPE.match(word):
            continue
        counts[word] += 1

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    return [{"term": term, "count": count} for term, count in ranked[:limit]]


# ---------------------------------------------------------------------------
# The stats sheet
# ---------------------------------------------------------------------------


def _words(text: str) -> int:
    """Words, counting an Arabic word the same way as an English one.

    Whitespace-separated runs holding at least one letter or digit, so
    punctuation sitting alone - and the `[connection interrupted]` notice's
    brackets - are not words.
    """
    return sum(
        1 for chunk in text.split()
        if any(char.isalnum() for char in chunk)
    )


def build_stats(segments: Sequence[Segment], speech: SpeechClock, *,
                duration: float, glossary: Sequence[str] = ()) -> dict:
    """Everything `stats.json` holds. Pure: no clock, no files, no network."""
    texts = {
        stream: "\n\n".join(s.text for s in segments
                            if s.stream == stream and not s.notice)
        for stream in ("en", "ar")
    }
    spoken_seconds = speech.seconds_by_language()
    longest_lang, longest = speech.longest_stretch()
    return {
        "duration_seconds": round(duration, 1),
        "duration": duration_text(duration),
        "speech_minutes": {
            language: round(seconds / 60, 2)
            for language, seconds in spoken_seconds.items()
        },
        "words": {stream: _words(text) for stream, text in texts.items()},
        "paragraphs": {
            stream: sum(1 for s in segments if s.stream == stream)
            for stream in ("en", "ar")
        },
        "switches": speech.switches(),
        "longest_stretch": {
            "language": longest_lang,
            "seconds": round(longest, 1),
            "text": duration_text(longest),
        },
        "notices": sum(1 for s in segments if s.notice),
        "terms": count_terms(texts, glossary),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _header(kind: str, *, date: str, duration: str, note: str,
            title: str = "", finished: bool = True) -> list[str]:
    """The few lines at the top of every file.

    Date, duration, languages - and the talk title only when the operator
    asked for it. **Never a speaker name**: those live in the gitignored
    `config/session.yaml` and reach the Soniox context and `/admin`, and a
    transcript is a file that gets mailed around.
    """
    lines = [f"# Live captions - {kind}", ""]
    if title:
        lines.append(f"- **Talk:** {title}")
    lines.append(f"- **Date:** {date}")
    lines.append(f"- **Duration:** {duration}")
    lines.append("- **Languages:** Arabic, English")
    if not finished:
        lines.append("- **Still running.** Written as the talk goes; the "
                     "duration and the stats land when it stops.")
    lines.extend(["", note, "", "---", ""])
    return lines


def render_single(segments: Sequence[Segment], stream: str) -> list[str]:
    """One language, in spoken order, a timestamp per paragraph."""
    lines: list[str] = []
    for segment in segments:
        if segment.stream != stream:
            continue
        lines.append(f"**{clock(segment.at)}** {segment.text}")
        lines.append("")
    return lines


def render_bilingual(segments: Sequence[Segment]) -> list[str]:
    """Both languages interleaved, each block labelled.

    No pairing is invented. The two streams break at different points - they
    are independent under Option B, and a shared paragraph id would be a lie
    (`CaptionState`'s own docstring) - so this lists every paragraph in the
    order it closed and says what each one is. A translation follows its
    original within a second or two, so the file reads as original-then-
    translation without anything having to guess which belongs to which.
    """
    lines: list[str] = []
    for segment in segments:
        language = LANGUAGE_NAMES.get(segment.stream, segment.stream)
        if segment.notice:
            kind = "notice"
        else:
            kind = "spoken" if segment.spoken else "translation"
        lines.append(f"**{clock(segment.at)} - {language} ({kind})**")
        lines.append("")
        lines.append(segment.text)
        lines.append("")
    return lines


def render_stats(stats: Mapping) -> list[str]:
    """`stats.md`: the same numbers as `stats.json`, read by a person."""
    speech = stats["speech_minutes"]
    words = stats["words"]
    switches = stats["switches"]
    longest = stats["longest_stretch"]
    lines = [
        "| | |",
        "| --- | --- |",
        f"| Duration | {stats['duration']} |",
        f"| Arabic spoken | {speech.get('ar', 0)} min |",
        f"| English spoken | {speech.get('en', 0)} min |",
        f"| Words (English stream) | {words.get('en', 0)} |",
        f"| Words (Arabic stream) | {words.get('ar', 0)} |",
        f"| Arabic to English switches | {switches.get('ar_to_en', 0)} |",
        f"| English to Arabic switches | {switches.get('en_to_ar', 0)} |",
    ]
    if longest.get("language"):
        name = LANGUAGE_NAMES.get(longest["language"], longest["language"])
        lines.append(f"| Longest uninterrupted stretch | {longest['text']} "
                     f"({name}) |")
    if stats.get("notices"):
        lines.append(f"| Connection gaps | {stats['notices']} |")
    lines.extend(["", "## Terms mentioned", ""])
    terms = stats.get("terms") or []
    if not terms:
        lines.append("None of the glossary's terms came up.")
    else:
        lines.append("| Term | Times |")
        lines.append("| --- | --- |")
        for row in terms:
            lines.append(f"| {row['term']} | {row['count']} |")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------

NOTES = {
    ENGLISH: ("The whole talk in English, in the order it was spoken. "
              "English speech as spoken; Arabic speech as its English "
              "translation. This is the one to summarise from."),
    ARABIC: ("The whole talk in Arabic, in the order it was spoken. "
             "Arabic speech as spoken; English speech as its Arabic "
             "translation."),
    BILINGUAL: ("Every paragraph in the order it closed, labelled with its "
                "language and whether it was spoken or translated."),
}

KINDS = {ENGLISH: "English", ARABIC: "Arabic", BILINGUAL: "bilingual"}


class TranscriptRecorder:
    """Harvests closed caption paragraphs into a transcript folder.

    Built by `CaptionSession.start_live` and by nothing else, which is what
    makes "only a live session writes a transcript" a property of the code
    rather than of the operator remembering to untick a box.
    """

    def __init__(self, directory: Path, *, date: str, title: str = "",
                 glossary: Sequence[str] = ()) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.date = date
        self.title = title
        self.glossary = list(glossary)
        self.segments: list[Segment] = []
        self.speech = SpeechClock()
        self.duration = 0.0
        self.finished = False
        self._written: set[int] = set()
        self._open_files: dict[str, object] = {}
        for name in (ENGLISH, ARABIC, BILINGUAL):
            path = self.directory / name
            handle = path.open("w", encoding="utf-8")
            handle.write("\n".join(self._header(name, finished=False)) + "\n")
            handle.flush()
            self._open_files[name] = handle

    # -- as the talk goes --------------------------------------------------

    def _header(self, name: str, *, finished: bool) -> list[str]:
        return _header(
            KINDS[name], date=self.date,
            duration=duration_text(self.duration) if finished else "running",
            note=NOTES[name], title=self.title, finished=finished,
        )

    def note_response(self, response: dict) -> None:
        """Measure one Soniox response. Text comes from the captions, not here.

        Only the original tokens, and only their timings: this is the audio
        clock, which `CaptionState` deliberately does not keep (it is pure and
        has no clock at all) and which no amount of caption text can recover.
        """
        for token in response.get("tokens") or []:
            if token.get("translation_status") == "translation":
                continue
            if not token.get("is_final"):
                continue
            start = token.get("start_ms")
            end = token.get("end_ms")
            if start is None or end is None:
                continue          # `<end>` carries no timing (finding 2)
            self.speech.add(int(start), int(end), token.get("language") or "")

    def observe(self, state: CaptionState, at: float) -> int:
        """Append every paragraph that has closed since the last call.

        Closed is terminal in `CaptionState` - `_open` never reopens a closed
        tail and `_close` is a no-op on one - so a paragraph harvested here
        can never change again, which is what makes appending safe. Called
        after every response, so the 200-paragraph trim can never drop one
        before it has been seen.
        """
        written = 0
        for stream in ("ar", "en"):
            for para in state.streams[stream]:
                if not para.closed or para.id in self._written:
                    continue
                if self._append(para, stream, at):
                    written += 1
        return written

    def _append(self, para, stream: str, at: float) -> bool:
        text = para.final.strip()
        self._written.add(para.id)
        if not text:
            return False
        segment = Segment(at=at, stream=stream, text=text,
                          spoken=para.spoken, para_id=para.id,
                          notice=para.notice)
        self.segments.append(segment)
        self._emit(segment)
        return True

    def _emit(self, segment: Segment) -> None:
        """Write one segment to the three files and flush.

        Flushed per paragraph for the same reason `CaptureWriter` flushes per
        line: the run that ends in a crash is the run whose record is worth
        having, and a paragraph is a few hundred bytes every twenty seconds.
        """
        single = render_single([segment], segment.stream)
        both = render_bilingual([segment])
        name = ENGLISH if segment.stream == "en" else ARABIC
        for target, lines in ((name, single), (BILINGUAL, both)):
            handle = self._open_files.get(target)
            if handle is None:
                continue
            handle.write("\n".join(lines) + "\n")
            handle.flush()

    # -- the end -----------------------------------------------------------

    def finalize(self, state: CaptionState | None, at: float) -> dict:
        """Rewrite the files with their headers, and write the stats.

        Repeatable on purpose. The idle pause takes a checkpoint through
        here and Stop comes through again; because each file is rewritten
        from `self.segments` rather than appended to, the second pass cannot
        duplicate a line. The still-open paragraph is included here and never
        appended incrementally, for the same reason.
        """
        self.duration = max(self.duration, at)
        tail: list[Segment] = []
        if state is not None:
            for stream in ("ar", "en"):
                para = state.streams[stream][-1] if state.streams[stream] else None
                if para is None or para.closed or para.id in self._written:
                    continue
                text = para.final.strip()
                if text:
                    tail.append(Segment(at=at, stream=stream, text=text,
                                        spoken=para.spoken, para_id=para.id,
                                        notice=para.notice))
        segments = self.segments + tail
        stats = build_stats(segments, self.speech, duration=self.duration,
                            glossary=self.glossary)

        for name in (ENGLISH, ARABIC, BILINGUAL):
            if name == BILINGUAL:
                body = render_bilingual(segments)
            else:
                body = render_single(segments, "en" if name == ENGLISH else "ar")
            self._replace(name, self._header(name, finished=True) + body)
        self._replace(STATS_MD, _header(
            "statistics", date=self.date,
            duration=duration_text(self.duration),
            note="Counted on this laptop from the caption text and the "
                 "engine's word timings. Nothing was sent anywhere.",
            title=self.title,
        ) + render_stats(stats))
        self._write_bytes(
            STATS_JSON,
            json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
        self.finished = True
        return stats

    def close(self) -> None:
        for handle in self._open_files.values():
            try:
                handle.close()            # type: ignore[attr-defined]
            except Exception:
                pass
        self._open_files.clear()

    def _replace(self, name: str, lines: Iterable[str]) -> None:
        handle = self._open_files.pop(name, None)
        if handle is not None:
            try:
                handle.close()            # type: ignore[attr-defined]
            except Exception:
                pass
        self._write_bytes(name, "\n".join(lines) + "\n")

    def _write_bytes(self, name: str, body: str) -> None:
        """Atomically, the way `save_screen` does it.

        The files are rewritten at the end of the talk, which is the worst
        possible moment to be halfway through one: an operator closing the
        laptop lid would leave a truncated transcript where a whole one had
        been appended all evening.
        """
        target = self.directory / name
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


def session_folder(root: Path, stamp: str) -> Path:
    """A fresh folder for `stamp`, never an existing one.

    "If the server restarts mid-talk, the next Start continues a new folder
    rather than overwriting": two Starts inside the same minute - which is
    exactly what a restart in the middle of a talk looks like - must not land
    on the same name. The second one gets `-2`.
    """
    base = Path(root) / stamp
    if not base.exists():
        return base
    for suffix in range(2, 100):
        candidate = Path(root) / f"{stamp}-{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many transcripts stamped {stamp}")


def is_safe_folder(name: str) -> bool:
    return bool(FOLDER_NAME.match(name or ""))


def list_transcripts(root: Path) -> list[dict]:
    """What `/admin` lists. Names, sizes and durations - never the text."""
    directory = Path(root)
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for child in sorted(directory.iterdir(), reverse=True):
        if not child.is_dir() or not is_safe_folder(child.name):
            continue
        files = [name for name in FILES if (child / name).is_file()]
        row: dict = {
            "name": child.name,
            "files": files,
            "bytes": sum((child / name).stat().st_size for name in files),
            "duration": None,
            "finished": STATS_JSON in files,
        }
        stats_path = child / STATS_JSON
        if stats_path.is_file():
            try:
                saved = json.loads(stats_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
            if isinstance(saved, dict):
                row["duration"] = saved.get("duration")
                words = saved.get("words") or {}
                # Counts, never text - the same rule the status poll follows.
                row["words"] = sum(v for v in words.values()
                                   if isinstance(v, int))
        out.append(row)
    return out


def delete_transcript(root: Path, name: str) -> bool:
    """Remove one folder. False when there was nothing of that name."""
    if not is_safe_folder(name):
        return False
    target = Path(root) / name
    if not target.is_dir():
        return False
    for child in sorted(target.iterdir(), reverse=True):
        if child.is_file():
            child.unlink(missing_ok=True)
    target.rmdir()
    return True


# ---------------------------------------------------------------------------
# Rebuilding one from a saved live log
# ---------------------------------------------------------------------------

#: `logs/live-<YYYY-MM-DD-HHMMSS>.jsonl`, which is what a live run with "keep
#: a log" ticked leaves behind. The date in the name is the night of the
#: talk, and it is what the rebuilt folder is stamped with.
LOG_STAMP = re.compile(r"\Alive-(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})\d*\Z")


def stamp_from_log(path: Path) -> tuple[str, str] | None:
    """`(folder stamp, date)` for a live log, or None if it is not one."""
    found = LOG_STAMP.match(Path(path).stem)
    if not found:
        return None
    year, month, day, hour, minute = found.groups()
    return f"{year}-{month}-{day}_{hour}{minute}", f"{year}-{month}-{day}"


def build_from_log(path: Path, root: Path, *,
                   glossary: Sequence[str] = (),
                   title: str = "") -> Path:
    """Turn a saved live log into the same folder a live run would write.

    The 2026-10-17 session is why this exists: two hours of real bilingual
    speech, logged, and with no transcript because the feature did not exist
    on the night. The log replays through a fresh `CaptionState` exactly as
    `tools/render_capture.py` does, so the text is the text the room saw, and
    the timestamps come from the log's own `recv_offset_ms` rather than from
    the clock on the laptop doing the rebuilding.

    Offline and free: nothing here opens a socket.
    """
    from app.replay import load_capture

    stamped = stamp_from_log(path)
    if stamped is None:
        raise ValueError(f"{Path(path).name} is not a live session log")
    stamp, date = stamped

    _meta, records = load_capture(path)
    state = CaptionState()
    directory = session_folder(Path(root), stamp)
    recorder = TranscriptRecorder(directory, date=date, title=title,
                                  glossary=glossary)
    try:
        at = 0.0
        for row in records:
            at = row.get("recv_offset_ms", 0) / 1000
            response = row.get("response") or {}
            recorder.note_response(response)
            state.add_response(response)
            recorder.observe(state, at)
        recorder.finalize(state, at)
    finally:
        recorder.close()
    return directory


__all__ = [
    "ARABIC", "AUTO", "BILINGUAL", "ENGLISH", "FILES", "STATS_JSON",
    "STATS_MD", "Segment", "SpeechClock", "TranscriptRecorder",
    "build_from_log", "build_stats", "clock", "count_terms",
    "delete_transcript", "duration_text", "is_safe_folder",
    "list_transcripts", "session_folder", "stamp_from_log",
]
