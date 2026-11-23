"""The readable record of a talk, and the rule that only a live talk gets one.

Three things are held in place here.

**Only a live session writes.** Replay, the microphone level check and the
audio-file check all drive `CaptionState` exactly the way a live run does -
that is what they are for - so nothing may key off "did captions appear".
`start_live` is the only caller that can build a recorder, and these tests
drive the other three with the box deliberately ticked to prove the server
does not care what the page asked for.

**The text is the caption text.** Not the token stream and not the grey
partials: the paragraphs the screen showed, which is what a summary has to be
made of if it is going to say what was said.

**It never leaves the laptop by accident.** `transcripts/` is gitignored, it
is not in `ASSETS`, and the four routes that can reach it are behind the
admin token. Every test here writes into a temporary directory; a test that
touched the real folder would be writing into the operator's own record of a
real evening, which has no history to recover it from.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
import wave
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.main as server  # noqa: E402
import app.settings as settings_module  # noqa: E402
import app.transcript as transcript  # noqa: E402
from app import usage  # noqa: E402
from app.audio_source import FileSource  # noqa: E402
from app.captions import CaptionState  # noqa: E402
from app.main import (  # noqa: E402
    CaptionSession,
    admin_start,
    admin_stop,
    admin_transcript_build,
    admin_transcript_delete,
    admin_transcript_file,
    admin_transcript_zip,
    admin_transcripts,
)
from app.replay import responses  # noqa: E402
from app.settings import settings  # noqa: E402
from audio import write_tone  # noqa: E402
from test_soniox_client import CHUNK_MS, FakeServer  # noqa: E402
from app.transcript import (  # noqa: E402
    ARABIC,
    ARABIC_CHAR,
    BILINGUAL,
    ENGLISH,
    FILES,
    STATS_JSON,
    STATS_MD,
    Segment,
    SpeechClock,
    TranscriptRecorder,
    build_from_log,
    build_stats,
    clock,
    count_terms,
    duration_text,
    is_safe_folder,
    list_transcripts,
    session_folder,
    stamp_from_log,
)
import captures  # noqa: E402
from captures import needs_captures  # noqa: E402
from repo import needs_git  # noqa: E402

TOKEN = "transcript-test-token"

#: A Latin letter, for asking whether a line of Arabic has English in it.
LATIN_CHAR = re.compile(r"[A-Za-z]")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    return TOKEN


@pytest.fixture(autouse=True)
def quarantine(tmp_path, monkeypatch):
    """Nothing here may touch the operator's own files.

    `transcripts/` is the new one and the one that matters: it holds a
    verbatim record of a real evening, it is gitignored, and there is
    therefore nothing to restore it from. Both the module the routes read
    and the module the session reads are pointed at `tmp_path`, because
    patching one and not the other is exactly how `config/session.yaml`
    was overwritten once before.
    """
    transcripts = tmp_path / "transcripts"
    monkeypatch.setattr(server, "TRANSCRIPT_DIR", transcripts)
    monkeypatch.setattr(settings_module, "TRANSCRIPT_DIR", transcripts)
    monkeypatch.setattr(server, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(usage, "LOG_DIR", tmp_path / "logs")
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(ROOT / "config" / "glossary.yaml", config / "glossary.yaml")
    monkeypatch.setattr(settings_module, "CONFIG_DIR", config)
    monkeypatch.setattr(settings_module, "STATE_DIR", config)
    return tmp_path


@pytest.fixture
def transcripts(quarantine):
    return quarantine / "transcripts"


@pytest.fixture
def live_logs(quarantine):
    """A `logs/` holding one live session log, as a real run leaves behind.

    Built from a capture, which is not committed - see `tests/captures.py`.
    Skipping here rather than erroring is what keeps a fresh clone's suite
    green: every test that takes this fixture needs real caption text, and
    there is no honest way to invent it at this level.
    """
    source = captures.path("run1")
    if not source.is_file():
        pytest.skip(captures._reason())
    directory = quarantine / "logs"
    directory.mkdir(exist_ok=True)
    shutil.copy(source, directory / "live-2026-10-17-011114.jsonl")
    return directory


@pytest.fixture
def tone(quarantine, monkeypatch):
    """A short tone, to drive the audio path without a microphone."""
    import numpy as np

    directory = quarantine / "recordings"
    directory.mkdir(exist_ok=True)
    samples = np.rint(
        np.sin(2 * np.pi * 440 * np.arange(16000) / 16000) * 0.5 * 32767
    ).astype(np.int16)
    path = directory / "tone.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(samples.tobytes())
    monkeypatch.setattr(server, "RECORDING_DIR", directory)
    import app.audio_source as audio_source
    monkeypatch.setattr(audio_source, "RECORDING_DIR", directory)
    return path


@pytest.fixture
async def fresh_session(monkeypatch, quarantine):
    made = CaptionSession()
    monkeypatch.setattr(server, "session", made)
    yield made
    await made.shutdown()


async def drain(session, *, limit: int = 600) -> None:
    """Let a session run until its source is exhausted."""
    for _ in range(limit):
        await asyncio.sleep(0.005)
        if not session.running:
            return


def body_of(path: Path) -> str:
    """A markdown file past its header, which is where the speech is."""
    return path.read_text(encoding="utf-8").split("---", 1)[1]


def paragraphs_of(path: Path) -> list[str]:
    """The timestamped lines of `english.md` / `arabic.md`, text only."""
    out = []
    for line in body_of(path).splitlines():
        found = re.match(r"\*\*[\d:]+\*\* (.*)", line.strip())
        if found:
            out.append(found.group(1))
    return out


# ---------------------------------------------------------------------------
# The little pure pieces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seconds,shown", [
    (0, "00:00"), (9.6, "00:09"), (61, "01:01"), (599, "09:59"),
    (3600, "1:00:00"), (7442, "2:04:02"), (-5, "00:00"),
])
def test_the_clock_reads_as_a_position_in_the_talk(seconds, shown):
    assert clock(seconds) == shown


@pytest.mark.parametrize("seconds,shown", [
    (0, "0s"), (45, "45s"), (125, "2m 05s"), (7442, "2h 04m"),
])
def test_a_duration_reads_as_a_length(seconds, shown):
    assert duration_text(seconds) == shown


def test_a_folder_name_is_checked_by_shape_not_sanitized():
    """It is interpolated into a path, and `/admin` is one token away from
    anything on the laptop. An allowlist of shapes, not an escaping pass."""
    assert is_safe_folder("2026-10-17_0111")
    assert is_safe_folder("2026-10-17_0111-2")
    for bad in ("", "..", "../../etc", "2026-10-17", "logs",
                "2026-10-17_0111/..", "2026-10-17_0111\\x",
                "C:/Windows", "2026-10-17_011", "2026-10-17_0111-"):
        assert not is_safe_folder(bad), f"{bad!r} was accepted as a folder"


def test_a_second_start_in_the_same_minute_gets_its_own_folder(tmp_path):
    """"If the server restarts mid-talk, the next Start continues a new
    folder rather than overwriting." A restart inside a talk lands in the
    same minute, which is the only way two Starts ever collide."""
    first = session_folder(tmp_path, "2026-10-17_0111")
    first.mkdir()
    second = session_folder(tmp_path, "2026-10-17_0111")
    assert second != first
    assert second.name == "2026-10-17_0111-2"
    second.mkdir()
    assert session_folder(tmp_path, "2026-10-17_0111").name == (
        "2026-10-17_0111-3")


@pytest.mark.parametrize("name,stamp", [
    ("live-2026-10-17-011114.jsonl", "2026-10-17_0111"),
    ("live-2026-10-24-193000.jsonl", "2026-10-24_1930"),
])
def test_a_live_log_names_the_night_it_came_from(name, stamp):
    assert stamp_from_log(Path(name)) == (stamp, stamp.split("_")[0])


@needs_captures
@pytest.mark.parametrize("name", [
    "run1.jsonl", "usage.jsonl", "live.jsonl", "docs-live-2026.jsonl",
    "live-2026-10.jsonl",
])
def test_anything_that_is_not_a_live_log_has_no_transcript_to_make(name):
    """A fixture and a capture file are recordings that were
    *replayed*, and a replay produces no transcript. That has to hold from
    this direction too, or the rule could be walked around by rebuilding."""
    assert stamp_from_log(Path(name)) is None


# ---------------------------------------------------------------------------
# Measuring the speech
# ---------------------------------------------------------------------------


def test_speech_time_is_talking_time_and_a_run_is_floor_time():
    """The two measurements this file keeps apart.

    "Minutes of Arabic" is how long somebody was actually talking, so a
    pause in the middle does not count. "Longest uninterrupted stretch" is
    how long the talk stayed in one language, so a pause does not end it -
    only the other language does.
    """
    speech = SpeechClock()
    speech.add(0, 2_000, "ar")
    speech.add(5_000, 7_000, "ar")       # same language, after a 3 s pause
    speech.add(20_000, 21_000, "en")
    assert speech.seconds_by_language() == {"ar": 4.0, "en": 1.0}
    language, seconds = speech.longest_stretch()
    assert (language, seconds) == ("ar", 7.0)
    assert speech.switches() == {"ar_to_en": 1, "en_to_ar": 0}


def test_a_reconnect_does_not_rewind_the_talk():
    """Soniox counts `start_ms` from the start of a *stream*, and a
    reconnect or the 280-minute rollover opens a new one at zero. Without
    the guard the second connection would overlay the first and a two-hour
    talk would measure as one hour."""
    speech = SpeechClock()
    speech.add(600_000, 620_000, "ar")
    speech.add(0, 20_000, "ar")          # a fresh stream, counting again
    assert speech.seconds_by_language()["ar"] == pytest.approx(40.0)


def test_only_final_original_tokens_are_timed(tmp_path):
    made = TranscriptRecorder(tmp_path / "t", date="2026-10-17")
    try:
        made.note_response({"tokens": [
            {"text": "a", "is_final": True, "language": "ar",
             "translation_status": "original",
             "start_ms": 0, "end_ms": 1000},
            # A translation: no timing, and not speech even if it had one.
            {"text": "b", "is_final": True, "language": "en",
             "translation_status": "translation",
             "start_ms": 0, "end_ms": 9_000},
            # Not final yet: it will be re-sent, and counting it twice
            # would inflate the minutes.
            {"text": "c", "is_final": False, "language": "ar",
             "translation_status": "original",
             "start_ms": 1000, "end_ms": 9_000},
            # `<end>` has no timing at all (Phase 1, finding 2).
            {"text": "<end>", "is_final": True,
             "translation_status": "original"},
        ]})
        assert made.speech.seconds_by_language() == {"ar": 1.0, "en": 0.0}
    finally:
        made.close()


# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------


def test_the_terms_are_the_glossary_plus_the_english_kept_in_arabic():
    """Two sources, because neither alone is enough: the glossary says which
    words were worth listening for, and a Latin word sitting inside Arabic
    speech is a term Soniox kept in English on purpose."""
    texts = {
        "en": "We used RAG and then more RAG, plus embeddings.",
        "ar": "استخدمنا RAG مع الـ embeddings وكمان MCP.",
    }
    found = {row["term"]: row["count"]
             for row in count_terms(texts, ["RAG", "embeddings"])}
    assert found["RAG"] == 3
    assert found["embeddings"] == 2
    # MCP is not in the glossary passed in, and was still found - it is a
    # Latin word inside the Arabic captions.
    assert found["MCP"] == 1


def test_a_term_is_matched_as_a_word_in_either_script():
    """`\\b` is defined against Latin word characters and does nothing at
    the edges of an Arabic term, so the boundaries are lookarounds."""
    texts = {"en": "storage and storages", "ar": "نستخدم المحولات اليوم"}
    found = {row["term"]: row["count"]
             for row in count_terms(texts, ["storage", "المحولات"])}
    assert found["storage"] == 1, "`storages` was counted as `storage`"
    assert found["المحولات"] == 1


def test_the_top_terms_are_capped_and_ordered():
    texts = {"en": " ".join(f"term{i} " * (20 - i) for i in range(20)),
             "ar": ""}
    rows = count_terms(texts, [f"term{i}" for i in range(20)], limit=15)
    assert len(rows) == 15
    counts = [row["count"] for row in rows]
    assert counts == sorted(counts, reverse=True)
    assert rows[0]["term"] == "term0"


def test_an_ordinary_english_word_inside_arabic_speech_is_not_a_term():
    """The 2026-10-24 sheet listed `do`, `can`, `like`, `time`, `that` and
    `they` as terms of a talk about language models.

    English words land in the Arabic captions for ordinary reasons - a
    speaker reaching for one, or Soniox labelling an English clause `ar` - so
    a word outside the glossary has to look like a term to be counted. The
    stats sheet is read as "what this talk was about", and a list of filler
    is worse than no list.
    """
    texts = {
        "en": "",
        "ar": "هو like يعني do يعني can نفس الـ time that they هذا.",
    }
    found = {row["term"] for row in count_terms(texts, [])}
    assert not found & {"like", "do", "can", "time", "that", "they"}, (
        f"filler words counted as terms: {sorted(found)}")


def test_a_capitalised_word_inside_arabic_speech_is_still_a_term():
    """The rule has to keep the terms it was built for.

    Acronyms, mixed caps and proper nouns all survive; the real session's
    `Unsloth`, `Anthropic` and `DeepSeek` were in none of the lists.
    """
    texts = {"en": "", "ar": "استخدمنا RAG و ChatGPT و DeepSeek و Anthropic."}
    found = {row["term"]: row["count"] for row in count_terms(texts, [])}
    for term in ("RAG", "ChatGPT", "DeepSeek", "Anthropic"):
        assert found.get(term) == 1, f"{term} was dropped: {sorted(found)}"


def test_a_lower_case_term_is_counted_when_the_glossary_claims_it():
    """The shape rule must not cost the glossary anything.

    `embeddings` and `fine-tuning` are lower case and are exactly what
    `translation_terms` keeps in English, so the operator's own list is
    matched whatever its case - which is the reason the shape rule can be
    strict about everything else.
    """
    texts = {"en": "", "ar": "نستخدم embeddings مع fine-tuning و kubernetes."}
    found = {row["term"]: row["count"]
             for row in count_terms(texts, ["embeddings", "fine-tuning"])}
    assert found["embeddings"] == 1
    assert found["fine-tuning"] == 1
    assert "kubernetes" not in found, (
        "a lower-case word outside the glossary is not a term")


def test_a_contraction_is_the_pronoun_and_not_a_term():
    """`I'm` and `We're` pass the capital test and are not terms."""
    texts = {"en": "", "ar": "قال I'm هنا و We're هناك و It's تمام."}
    found = {row["term"] for row in count_terms(texts, [])}
    assert not found & {"I'm", "We're", "It's"}, sorted(found)


# ---------------------------------------------------------------------------
# Rebuilding from a saved live log - the Oct 17 session
# ---------------------------------------------------------------------------


@pytest.fixture
def rebuilt(live_logs, transcripts):
    """A transcript made from a live log, the way `/admin` makes one."""
    from app.settings import load_glossary

    return build_from_log(live_logs / "live-2026-10-17-011114.jsonl",
                          transcripts, glossary=load_glossary().terms)


def test_a_live_log_rebuilds_into_the_same_five_files(rebuilt):
    assert rebuilt.name == "2026-10-17_0111"
    assert sorted(p.name for p in rebuilt.iterdir()) == sorted(FILES)
    for name in FILES:
        assert (rebuilt / name).stat().st_size > 0, f"{name} is empty"


def test_the_rebuilt_text_is_the_caption_text(rebuilt, live_logs):
    """The one assertion that says this is a transcript of the talk and not
    of the token stream: the same capture through `CaptionState` on its own
    has to produce the same paragraphs, in the same order."""
    state = CaptionState()
    for response in responses(live_logs / "live-2026-10-17-011114.jsonl"):
        state.add_response(response)

    for name, stream in ((ENGLISH, "en"), (ARABIC, "ar")):
        expected = [(p.final + p.partial).strip()
                    for p in state.streams[stream]
                    if (p.final + p.partial).strip()]
        assert paragraphs_of(rebuilt / name) == expected, (
            f"{name} is not the {stream} stream the screen showed")


def test_english_md_carries_no_arabic_at_all(rebuilt):
    """The whole point of the English file: it is what a summary is made
    from, and a paragraph of untranslated Arabic in the middle of it is a
    hole in the summary rather than a cosmetic problem."""
    found = ARABIC_CHAR.findall(body_of(rebuilt / ENGLISH))
    assert not found, (
        f"english.md carries {len(found)} Arabic characters: "
        f"{''.join(sorted(set(found))[:20])}")


def test_arabic_md_is_arabic_with_terms_in_it_not_english(rebuilt):
    """"No Latin sentences, technical terms excepted" - measured two ways,
    because the speakers really do say English words inside Arabic speech
    (`عشان to fold one protein` is in this very capture) and a test that
    banned Latin outright would fail on a true positive.

    So: no paragraph may be *entirely* Latin, which is what an untranslated
    English block would look like, and the file as a whole has to stay
    overwhelmingly Arabic. Measured at 92.5% on this capture.
    """
    lines = paragraphs_of(rebuilt / ARABIC)
    assert lines, "arabic.md has no paragraphs; this test proved nothing"
    latin_only = [line for line in lines if not ARABIC_CHAR.search(line)]
    assert not latin_only, (
        f"{len(latin_only)} paragraph(s) of arabic.md are pure Latin, which "
        f"is an English block that never got translated: {latin_only[:2]}")

    body = "\n".join(lines)
    arabic = len(ARABIC_CHAR.findall(body))
    latin = len(LATIN_CHAR.findall(body))
    share = arabic / (arabic + latin)
    assert share >= 0.90, (
        f"arabic.md is only {share:.0%} Arabic script, so it is carrying "
        f"English sentences rather than the odd English term")


def test_bilingual_md_says_which_half_each_block_is(rebuilt):
    """No pairing is invented - the two streams break at different points -
    so each block is labelled instead, and both labels have to appear or the
    file is a list with no way to read it."""
    body = body_of(rebuilt / BILINGUAL)
    assert "Arabic (spoken)" in body
    assert "English (translation)" in body
    # ...and this capture is Arabic speech, so the reverse pair must not
    # appear: an "Arabic (translation)" block here would mean the recorder
    # had lost track of which side was which.
    assert "Arabic (translation)" not in body
    assert "English (spoken)" not in body


@needs_captures
def test_the_stats_are_right_for_a_known_capture(rebuilt):
    """`run1` is five minutes of Egyptian Arabic with English translation,
    and the numbers below are checkable against `a capture report` and
    against the note that its 35 Latin-script words inside Arabic
    speech produce **0** direction changes."""
    stats = json.loads((rebuilt / STATS_JSON).read_text(encoding="utf-8"))

    assert stats["duration_seconds"] == pytest.approx(300, abs=2)
    assert stats["duration"] == "5m 00s"
    # Arabic was spoken; English never was. The English in this transcript
    # is all translation, and translation is not time in the room.
    assert stats["speech_minutes"]["ar"] == pytest.approx(3.65, abs=0.1)
    assert stats["speech_minutes"]["en"] == 0.0
    # Not one direction change, which is the measured finding this project
    # already relies on elsewhere.
    assert stats["switches"] == {"ar_to_en": 0, "en_to_ar": 0}
    # So the whole capture is one stretch of Arabic.
    assert stats["longest_stretch"]["language"] == "ar"
    assert stats["longest_stretch"]["seconds"] == pytest.approx(299, abs=2)
    assert stats["notices"] == 0
    # Both streams carry a few hundred words, and the counts are of the
    # transcript on disk rather than of anything recomputed here.
    assert stats["words"]["en"] == pytest.approx(857, abs=5)
    assert stats["words"]["ar"] == pytest.approx(724, abs=5)
    assert stats["paragraphs"]["en"] == len(paragraphs_of(rebuilt / ENGLISH))
    assert stats["paragraphs"]["ar"] == len(paragraphs_of(rebuilt / ARABIC))
    # `AI` is the most-said term in this capture, by a distance.
    terms = {row["term"]: row["count"] for row in stats["terms"]}
    assert terms, "no terms at all came out of a talk about AI"
    assert 0 < len(stats["terms"]) <= 15
    assert terms.get("AI", 0) >= 20


def test_the_stats_sheet_says_the_same_as_the_json(rebuilt):
    stats = json.loads((rebuilt / STATS_JSON).read_text(encoding="utf-8"))
    sheet = (rebuilt / STATS_MD).read_text(encoding="utf-8")
    assert stats["duration"] in sheet
    assert str(stats["speech_minutes"]["ar"]) in sheet
    assert str(stats["words"]["en"]) in sheet
    assert stats["terms"][0]["term"] in sheet


def test_no_header_names_a_speaker_and_the_title_is_opt_in(live_logs,
                                                           transcripts):
    """"No speaker names or talk details unless I tick include talk title."
    The tick is for the title; a speaker's name has no switch at all,
    because there is no reason for one to be in this file."""
    plain = build_from_log(live_logs / "live-2026-10-17-011114.jsonl",
                           transcripts)
    for name in FILES:
        head = (plain / name).read_text(encoding="utf-8").split("---", 1)[0]
        assert "Talk:" not in head, f"{name} named the talk unasked"
        assert "Date:" in head or name == STATS_JSON
        assert "Duration:" in head or name == STATS_JSON

    titled = build_from_log(live_logs / "live-2026-10-17-011114.jsonl",
                            transcripts, title="Fine-tuning in practice")
    assert titled != plain, "the second build overwrote the first"
    head = (titled / ENGLISH).read_text(encoding="utf-8").split("---", 1)[0]
    assert "Fine-tuning in practice" in head


# ---------------------------------------------------------------------------
# Live sessions, and only live sessions
# ---------------------------------------------------------------------------


@needs_captures
async def test_a_replay_writes_no_transcript_however_it_is_asked(
    fresh_session, transcripts
):
    """The box is deliberately ticked. A replay drives `CaptionState`
    exactly as a live run does - that is the whole point of it - so the
    rule cannot be "did captions appear", and the page cannot be where it
    is decided."""
    await admin_start(token=TOKEN, source="replay", capture="run1.jsonl",
                      paced=False, transcript=True, transcript_title=True)
    await drain(fresh_session)
    await admin_stop(token=TOKEN)

    assert fresh_session.state.streams["en"], (
        "the replay produced no captions, so this proved nothing")
    assert fresh_session._transcript is None
    assert not transcripts.exists() or not list(transcripts.iterdir()), (
        "a replay wrote a transcript")


async def test_a_level_check_writes_no_transcript(fresh_session, tone,
                                                  transcripts):
    """Mic and file runs capture audio and measure it. No engine, no
    captions, and nothing to write down."""
    await admin_start(token=TOKEN, source="file", file="tone.wav",
                      paced=False, transcript=True)
    await drain(fresh_session)
    await admin_stop(token=TOKEN)
    assert fresh_session._transcript is None
    assert not transcripts.exists() or not list(transcripts.iterdir())


class BilingualServer(FakeServer):
    """A `connect` double that answers with real-looking bilingual tokens.

    A **subclass** of the Soniox client's own double, not a wrapper around
    one: `FakeServer._session` hands the socket the server it was built from,
    so a wrapper's `on_audio` is never reached and the test silently measures
    the default " word" reply instead. That is how the first version of this
    passed its word count and reported nought minutes of Arabic.

    Each chunk answers with one Arabic original, an `<end>` to close it, and
    the English translation - which is what endpoint detection does, and what
    makes paragraphs close *during* the run rather than all at Stop. The
    timings are on the originals only, because that is where Soniox puts
    them.

    Nothing here reaches the network: `SonioxClient` takes its `connect` as
    an argument and imports `websockets` lazily, so that is true by
    construction rather than by care.
    """

    SPEECH = [
        ("\u0647\u0630\u0627 \u0627\u062e\u062a\u0628\u0627\u0631.",
         " This is a test."),
        (" \u0646\u062a\u0643\u0644\u0645 \u0639\u0646 RAG.",
         " We are talking about RAG."),
        (" \u0648\u0639\u0646 embeddings.", " And about embeddings."),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.turn = 0
        self.at_ms = 0

    async def on_keepalive(self, socket):
        pass

    async def on_audio(self, socket, chunk):
        self.acked_ms[socket] = self.acked_ms.get(socket, 0) + CHUNK_MS
        arabic, english = self.SPEECH[self.turn % len(self.SPEECH)]
        self.turn += 1
        start, self.at_ms = self.at_ms, self.at_ms + 900
        socket.reply({"tokens": [
            {"text": arabic, "is_final": True, "language": "ar",
             "translation_status": "original",
             "start_ms": start, "end_ms": start + 800},
            # The segment boundary. Never displayed; it is what closes the
            # original side and arms the translated one.
            {"text": "<end>", "is_final": True,
             "translation_status": "original"},
            {"text": english, "is_final": True, "language": "en",
             "translation_status": "translation", "source_language": "ar"},
        ], "final_audio_proc_ms": self.acked_ms[socket]})


async def live_run(session, tone_path, **start_kwargs):
    """Drive a whole simulated live session and stop it the way Stop does."""
    fake = BilingualServer()
    source = FileSource(tone_path, realtime=False)
    await session.start_live(source, connect=fake, api_key="test-key",
                             **start_kwargs)
    await drain(session)
    await admin_stop(token=TOKEN)
    return fake


@pytest.fixture
def allow_live(monkeypatch):
    monkeypatch.setattr(settings, "soniox_api_key", "test-key")


async def test_a_live_session_writes_the_captions_it_showed(
    fresh_session, tone, transcripts, allow_live
):
    """The other half of the rule, and the one that has to actually work:
    a live run produces the folder, and what is in it is the text the
    screen showed - not the token stream it was assembled from."""
    await live_run(fresh_session, tone)

    folders = sorted(transcripts.iterdir())
    assert len(folders) == 1, f"expected one transcript folder, got {folders}"
    folder = folders[0]
    assert sorted(p.name for p in folder.iterdir()) == sorted(FILES)

    shown = {
        stream: [(p.final + p.partial).strip()
                 for p in fresh_session.state.streams[stream]
                 if (p.final + p.partial).strip()]
        for stream in ("en", "ar")
    }
    assert shown["en"], "the fake engine produced no captions"
    for name, stream in ((ENGLISH, "en"), (ARABIC, "ar")):
        assert paragraphs_of(folder / name) == shown[stream], (
            f"{name} is not the {stream} captions the screen showed")

    stats = json.loads((folder / STATS_JSON).read_text(encoding="utf-8"))
    assert stats["words"]["en"] > 0
    assert stats["speech_minutes"]["ar"] > 0
    # The glossary is loaded for a live run too, so `RAG` is a known term
    # rather than only a Latin word that happened to be in the Arabic.
    assert any(row["term"] == "RAG" for row in stats["terms"])


async def test_nothing_is_written_when_the_box_is_unticked(
    fresh_session, tone, transcripts, allow_live
):
    """A speaker who does not want to be recorded. Captions still run."""
    await live_run(fresh_session, tone, transcript=False)
    assert fresh_session.state.streams["en"], (
        "captions stopped too, which is not what unticking the box means")
    assert not transcripts.exists() or not list(transcripts.iterdir())


async def test_the_talk_title_reaches_the_header_only_when_asked(
    fresh_session, tone, transcripts, allow_live, monkeypatch
):
    from app.settings import SessionContext

    fresh_session.details = SessionContext(title="A private working title",
                                           speakers=["Someone Real"])
    await live_run(fresh_session, tone, transcript_title=True)
    folder = sorted(transcripts.iterdir())[0]
    head = (folder / ENGLISH).read_text(encoding="utf-8").split("---", 1)[0]
    assert "A private working title" in head
    # The title was asked for. A speaker's name never is.
    for name in FILES:
        whole = (folder / name).read_text(encoding="utf-8")
        assert "Someone Real" not in whole, (
            f"{name} carries a speaker's name")


async def test_a_speaker_name_is_never_in_a_transcript(
    fresh_session, tone, transcripts, allow_live
):
    """The default path, with details loaded and the title *not* asked for:
    neither the name nor the title may appear."""
    from app.settings import SessionContext

    fresh_session.details = SessionContext(title="Zzqq Title Sentinel",
                                           speakers=["Zzqq Speaker Sentinel"])
    await live_run(fresh_session, tone)
    folder = sorted(transcripts.iterdir())[0]
    for name in FILES:
        whole = (folder / name).read_text(encoding="utf-8")
        assert "Zzqq Speaker Sentinel" not in whole
        assert "Zzqq Title Sentinel" not in whole


async def test_the_paragraphs_so_far_are_on_disk_before_the_end(
    fresh_session, tone, transcripts, allow_live
):
    """A power cut at minute 90 must cost the last sentence, not the talk.

    The run is cut off without ever reaching Stop - no `finalize`, no
    headers, no stats - and what was appended as it went has to still be
    readable. That is the difference between flushing per paragraph and
    holding the whole thing in memory until the end.

    **Both halves need the run to still be going when the test looks.** The
    header says "Still running" only until `finalize`, and there is no task to
    cancel once the session has ended. With `realtime=False` the whole file
    went through in milliseconds, so on Linux and macOS the run had already
    finished by the first look and the header read `Duration: 0s` (2026-11-23).
    Paced audio, five seconds of it, and a deadline rather than a count of
    sleeps: the run is certainly still in flight, and a slow runner waits
    longer instead of giving up early.
    """
    fake = BilingualServer()
    source = FileSource(
        write_tone(tone.parent / "unfinished.wav", seconds=5), realtime=True)
    await fresh_session.start_live(source, connect=fake, api_key="test-key")
    folder = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        await asyncio.sleep(0.005)
        folder = (sorted(transcripts.iterdir()) or [None])[0]
        if folder is not None and paragraphs_of(folder / ENGLISH):
            break
    else:
        pytest.fail("nothing reached disk while the session was running")

    assert fresh_session.running, (
        "the session ended before the test could look at it, so the "
        "unfinished-transcript assertions below would prove nothing")

    written = paragraphs_of(folder / ENGLISH)
    assert written, "english.md had a header and no speech"
    # Still running, so the header says so and the stats are not there yet.
    head = (folder / ENGLISH).read_text(encoding="utf-8").split("---", 1)[0]
    assert "Still running" in head
    assert not (folder / STATS_JSON).exists()

    # Now kill it the way a power cut does: cancel the task outright, with
    # no flush and no finalize.
    task, fresh_session._task = fresh_session._task, None
    task.cancel()
    await asyncio.wait({task})
    await fresh_session.shutdown()

    survived = paragraphs_of(folder / ENGLISH)
    assert survived[:len(written)] == written, (
        "paragraphs that had been written were lost when the run died")


async def test_a_transcript_that_could_not_be_started_says_so(
    fresh_session, tone, transcripts, allow_live, monkeypatch
):
    """A read-only disk must not stop a talk - captions matter more than the
    write-up - but it must not be silent either, or an operator finds out
    afterwards that the evening was never written down.

    The ordering is the real subject here. `_begin` clears `self.error`, so
    opening the transcript *before* it wiped the message on the same line it
    was written, and the page said nothing at all.
    """
    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(server, "session_folder", refuse)
    await live_run(fresh_session, tone)

    assert fresh_session.state.streams["en"], (
        "the talk stopped because the transcript could not be written")
    assert not transcripts.exists() or not list(transcripts.iterdir())
    assert fresh_session.error, "the failure was swallowed"
    assert "transcript" in fresh_session.error
    assert "read-only file system" in fresh_session.error


async def test_the_status_poll_names_the_folder_and_not_the_text(
    fresh_session, tone, transcripts, allow_live
):
    from app.main import admin_status

    await live_run(fresh_session, tone)
    body = (await admin_status(token=TOKEN)).body.decode("utf-8")
    status = json.loads(body)
    folder = sorted(transcripts.iterdir())[0]
    assert status["transcript"] == folder.name
    # ...and not a word of what is in it.
    spoken = paragraphs_of(folder / ENGLISH)[0]
    assert spoken[:20] not in body, "the status poll carries caption text"


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handler,kwargs", [
    (admin_transcripts, {}),
    (admin_transcript_file, {"name": "2026-10-17_0111", "file": ENGLISH}),
    (admin_transcript_zip, {"name": "2026-10-17_0111"}),
    (admin_transcript_delete, {"name": "2026-10-17_0111"}),
    (admin_transcript_build, {"capture": "live-2026-10-17-011114.jsonl"}),
])
async def test_every_transcript_route_needs_the_token(rebuilt, handler,
                                                      kwargs):
    """A transcript is a verbatim record of a room. There is no route to one
    that is not behind the token, and `/` and `/screen` do not know it
    exists."""
    from fastapi import HTTPException

    for bad in (None, "", "wrong"):
        with pytest.raises(HTTPException) as raised:
            await handler(token=bad, **kwargs)
        assert raised.value.status_code == 401


async def test_the_listing_is_names_and_counts(rebuilt):
    body = json.loads((await admin_transcripts(token=TOKEN)).body)
    rows = body["transcripts"]
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "2026-10-17_0111"
    assert sorted(row["files"]) == sorted(FILES)
    assert row["duration"] == "5m 00s"
    assert row["finished"] is True
    assert row["bytes"] > 0
    assert row["words"] > 0
    # Counts, never text - the rule `/admin/status` already follows.
    spoken = paragraphs_of(rebuilt / ENGLISH)[0]
    assert spoken[:20] not in json.dumps(body, ensure_ascii=False)


async def test_a_file_comes_back_named_for_its_session(rebuilt):
    got = await admin_transcript_file(token=TOKEN, name=rebuilt.name,
                                      file=ENGLISH)
    assert Path(got.path) == rebuilt / ENGLISH
    assert got.media_type.startswith("text/markdown")
    disposition = got.headers["content-disposition"]
    assert "captions-2026-10-17_0111-english.md" in disposition, (
        "three talks' transcripts would all download as english.md")


@pytest.fixture
def planted(rebuilt, quarantine):
    """A file a traversal would actually reach, if one were allowed.

    Without this the traversal test passes for the wrong reason: asking for
    `../../.env` 404s because there is no `.env` in a pytest temp directory,
    not because the request was refused - and the file allowlist could be
    deleted with every assertion still green. Watched failing with the
    allowlist removed, which is the only way to know.
    """
    (quarantine / "planted-secret.txt").write_text("sk-not-a-real-key",
                                                   encoding="utf-8")
    (rebuilt / "notes.md").write_text("an operator's own scribble",
                                      encoding="utf-8")
    return quarantine


@pytest.mark.parametrize("name,file", [
    # Out of the folder, to a file that is really there.
    ("2026-10-17_0111", "../../planted-secret.txt"),
    ("2026-10-17_0111", "..\\..\\planted-secret.txt"),
    ("2026-10-17_0111", "../planted-secret.txt"),
    # Inside the folder, but not one of the five files it may serve.
    ("2026-10-17_0111", "notes.md"),
    # ...and the same from the folder side.
    ("../config", ENGLISH),
    ("..", ENGLISH),
    ("", ENGLISH),
    ("2026-10-17_0111/..", ENGLISH),
])
async def test_no_request_can_name_a_path_of_its_own(planted, name, file):
    """The folder is checked by shape and the file against an allowlist, so
    a traversal is refused before any path is built. `/admin` is behind a
    token, but a traversal there would read anything on the laptop.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        await admin_transcript_file(token=TOKEN, name=name, file=file)
    assert raised.value.status_code == 404


async def test_the_zip_holds_only_the_five_files(planted, rebuilt):
    """A stray file in the folder - an operator's own notes, a leftover
    temp file - is not part of the transcript and does not go in the zip."""
    import io

    got = await admin_transcript_zip(token=TOKEN, name=rebuilt.name)
    with zipfile.ZipFile(io.BytesIO(got.body)) as archive:
        inside = {Path(n).name for n in archive.namelist()}
    assert inside == set(FILES)
    assert "notes.md" not in inside


async def test_delete_removes_the_strays_with_it(planted, rebuilt,
                                                 transcripts):
    """Delete means the session is gone. Leaving a folder behind because
    something unexpected was in it would be the one outcome an operator who
    pressed Delete did not ask for."""
    body = json.loads(
        (await admin_transcript_delete(token=TOKEN, name=rebuilt.name)).body)
    assert body["deleted"] is True
    assert not rebuilt.exists()
    assert transcripts.is_dir()


async def test_the_zip_holds_the_whole_session(rebuilt):
    import io

    got = await admin_transcript_zip(token=TOKEN, name=rebuilt.name)
    assert got.media_type == "application/zip"
    assert "captions-2026-10-17_0111.zip" in got.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(got.body)) as archive:
        names = sorted(archive.namelist())
        assert names == sorted(f"captions-{rebuilt.name}/{n}" for n in FILES)
        inside = archive.read(f"captions-{rebuilt.name}/{ENGLISH}")
        assert inside == (rebuilt / ENGLISH).read_bytes()


async def test_deleting_one_removes_the_whole_folder(rebuilt, transcripts):
    body = json.loads(
        (await admin_transcript_delete(token=TOKEN, name=rebuilt.name)).body)
    assert body["deleted"] is True
    assert body["transcripts"] == []
    assert not rebuilt.exists()
    assert transcripts.is_dir(), "the whole transcripts folder was removed"


async def test_deleting_something_that_is_not_there_is_a_404(transcripts):
    from fastapi import HTTPException

    transcripts.mkdir(parents=True, exist_ok=True)
    with pytest.raises(HTTPException) as raised:
        await admin_transcript_delete(token=TOKEN, name="2026-01-01_0000")
    assert raised.value.status_code == 404


async def test_make_transcript_works_on_a_logged_live_run(live_logs,
                                                          transcripts):
    body = json.loads((await admin_transcript_build(
        token=TOKEN, capture="live-2026-10-17-011114.jsonl")).body)
    assert body["built"] == "2026-10-17_0111"
    assert [row["name"] for row in body["transcripts"]] == ["2026-10-17_0111"]
    folder = transcripts / "2026-10-17_0111"
    assert sorted(p.name for p in folder.iterdir()) == sorted(FILES)


@needs_captures
@pytest.mark.parametrize("capture", [
    "run1.jsonl", "usage.jsonl", "", "../../.env", "live-2026.jsonl",
])
async def test_make_transcript_refuses_anything_but_a_live_log(live_logs,
                                                               capture):
    """A fixture or a capture file is a recording that was
    *replayed*. Replays produce no transcript, and that has to hold when the
    request comes in from this side too."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        await admin_transcript_build(token=TOKEN, capture=capture)
    assert raised.value.status_code == 400


async def test_a_second_build_of_the_same_log_does_not_overwrite_the_first(
    live_logs, transcripts
):
    first = json.loads((await admin_transcript_build(
        token=TOKEN, capture="live-2026-10-17-011114.jsonl")).body)["built"]
    second = json.loads((await admin_transcript_build(
        token=TOKEN, capture="live-2026-10-17-011114.jsonl")).body)["built"]
    assert first != second
    assert sorted(p.name for p in transcripts.iterdir()) == [first, second]


# ---------------------------------------------------------------------------
# Nothing leaves the laptop
# ---------------------------------------------------------------------------


@needs_git
def test_the_transcripts_folder_is_not_tracked_by_git():
    """The whole point. These files are a verbatim record of a room."""
    listed = subprocess.run(["git", "ls-files", "transcripts"], cwd=ROOT,
                            text=True, capture_output=True,
                            check=True).stdout.split()
    assert not listed, f"transcripts are committed: {listed}"


@needs_git
def test_the_transcripts_folder_is_ignored_not_merely_absent():
    """A fresh clone has none, so "no files" proves nothing on its own."""
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "transcripts/" in ignored
    check = subprocess.run(
        ["git", "check-ignore", "-q", "transcripts/2026-10-17_0111/english.md"],
        cwd=ROOT)
    assert check.returncode == 0, "a transcript file is not gitignored"


def test_no_transcript_is_servable_as_a_static_asset():
    """`/static/{name}` is an allowlist of flat names inside `web/`, and the
    transcript files are neither in it nor in that directory."""
    for name in FILES:
        assert name not in server.ASSETS
    assert not (ROOT / "web" / ENGLISH).exists()


def test_the_audience_pages_do_not_know_transcripts_exist():
    """`/` and `/screen` are read-only and are never told. Checked the same
    way the "never links to /admin" rule is."""
    for name in ("index.html", "app.js", "auto.js", "screen.html",
                 "screen.js"):
        text = (ROOT / "web" / name).read_text(encoding="utf-8")
        assert "transcript" not in text.lower(), (
            f"web/{name} mentions transcripts")


def test_the_recorder_is_built_in_exactly_one_place():
    """"Only a live session writes one" is a property of the code. If a
    second caller appears, this test is where the decision gets made again
    rather than being assumed."""
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    code = re.sub(r"#.*", "", source)
    assert code.count("TranscriptRecorder(") == 1
    assert code.count("_open_transcript(") == 2, (
        "the recorder is opened from somewhere other than start_live")
    opened = source.split("_open_transcript(enabled=")[0]
    assert "async def start_live" in opened
    assert "async def start_replay" not in opened.rsplit(
        "async def start_live", 1)[-1]


# ---------------------------------------------------------------------------
# Finalizing twice
# ---------------------------------------------------------------------------


@needs_captures
def test_finalizing_twice_does_not_duplicate_a_line(tmp_path):
    """The idle pause takes a checkpoint and Stop comes through again. Each
    file is rewritten from the segments held in memory rather than appended
    to, which is what makes the second pass harmless."""
    state = CaptionState()
    for response in responses(ROOT / "tests" / "fixtures" / "run1.jsonl"):
        state.add_response(response)

    made = TranscriptRecorder(tmp_path / "t", date="2026-10-17")
    try:
        made.observe(state, 10.0)
        made.finalize(state, 300.0)
        once = (made.directory / ENGLISH).read_text(encoding="utf-8")
        made.finalize(state, 300.0)
        twice = (made.directory / ENGLISH).read_text(encoding="utf-8")
    finally:
        made.close()
    assert once == twice


def test_the_open_paragraph_is_included_once_at_the_end(tmp_path):
    """It is deliberately never appended as the talk goes - it can still
    change - so `finalize` is the only thing that may write it, and it must
    not then write it twice."""
    state = CaptionState()
    state.add_response({"tokens": [
        {"text": "An unfinished thought", "is_final": True, "language": "en",
         "translation_status": "original", "start_ms": 0, "end_ms": 1000},
    ]})
    assert not state.streams["en"][-1].closed

    made = TranscriptRecorder(tmp_path / "t", date="2026-10-17")
    try:
        assert made.observe(state, 1.0) == 0, "an open paragraph was appended"
        assert paragraphs_of(made.directory / ENGLISH) == []
        made.finalize(state, 2.0)
        assert paragraphs_of(made.directory / ENGLISH) == [
            "An unfinished thought"]
        made.finalize(state, 2.0)
        assert paragraphs_of(made.directory / ENGLISH) == [
            "An unfinished thought"]
    finally:
        made.close()


def test_a_connection_gap_is_in_the_record(tmp_path):
    """A notice is something the audience was shown, so it belongs in the
    transcript - a reader who cannot see the gap reads it as the speaker
    changing the subject, which is the same reason the notice exists."""
    state = CaptionState()
    state.add_response({"tokens": [
        {"text": "Before", "is_final": True, "language": "en",
         "translation_status": "original", "start_ms": 0, "end_ms": 500},
    ]})
    state.add_notice()

    made = TranscriptRecorder(tmp_path / "t", date="2026-10-17")
    try:
        made.observe(state, 5.0)
        stats = made.finalize(state, 6.0)
        body = body_of(made.directory / BILINGUAL)
    finally:
        made.close()
    assert stats["notices"] == 2, "one notice per language stream"
    assert "(notice)" in body
    assert "[connection interrupted]" in body


def test_the_stats_builder_is_pure(tmp_path):
    """No clock, no files, no network - the same contract `captions.py`
    keeps, and what makes the numbers above assertable."""
    segments = [
        Segment(at=0.0, stream="en", text="Two words here", spoken=True,
                para_id=1),
        Segment(at=1.0, stream="ar", text="\u0643\u0644\u0645\u062a\u0627\u0646",
                spoken=True, para_id=2),
    ]
    speech = SpeechClock()
    speech.add(0, 1_000, "en")
    stats = build_stats(segments, speech, duration=90.0, glossary=["words"])
    assert stats["duration"] == "1m 30s"
    assert stats["words"] == {"en": 3, "ar": 1}
    assert stats["paragraphs"] == {"en": 1, "ar": 1}
    assert stats["terms"] == [{"term": "words", "count": 1}]


def test_an_empty_talk_still_produces_a_readable_folder(tmp_path):
    """Start, nobody speaks, Stop. The files have to exist and say so
    rather than being absent or half-written - an operator checking that
    the transcript worked should not have to guess."""
    made = TranscriptRecorder(tmp_path / "2026-10-17_0111", date="2026-10-17")
    try:
        stats = made.finalize(CaptionState(), 0.0)
    finally:
        made.close()
    assert sorted(p.name for p in made.directory.iterdir()) == sorted(FILES)
    assert stats["words"] == {"en": 0, "ar": 0}
    assert stats["terms"] == []
    assert "None of the glossary's terms came up." in (
        made.directory / STATS_MD).read_text(encoding="utf-8")
    assert list_transcripts(tmp_path)[0]["duration"] == "0s"
