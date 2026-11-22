"""Three hours of talk, synthesized: what must still be true at the end.

The first real session (2026-10-24) ran 52 minutes and then 33 more, and the
projector was reported showing the original in the Auto view somewhere after
the half-hour mark. Both logs replay clean through `CaptionState` - see
the live-run measurements - so this file exists to make the *shape* of a long
session checkable without one: everything here is synthetic, deterministic and
offline, and it runs the pipeline for about three hours of simulated talk.

Why synthetic rather than a capture. The longest log in the repo is 52
minutes and no capture holds a two-hour talk, so a saved file cannot answer
"what happens at hour three". And a synthetic stream can be *labelled*: every
original word carries `orig` and every translation `trans`, so "`auto` never
shows the original" stops being an argument about routing and becomes a
substring test.

What it asserts, continuously:

1. **`auto` is translation only.** No original word ever reaches it.
2. **Each `auto` paragraph's `lang`/`dir` matches its own text**, so the
   projector never sets Arabic left-to-right in the Latin face.
3. **A snapshot equals the diffs** a client accumulated - the invariant
   `web/app.js` depends on, over tens of thousands of responses.
4. **Nothing grows without bound**: paragraph lists stay inside the cap, and
   ids are unique and ascending for the whole run.
5. **No exception**, through language switches, 12-minute monologues, rapid
   back-and-forth, embedded English terms inside Arabic, and the engine
   reconnects that a three-hour session will see.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.captions import AUTO, MAX_PARAGRAPHS, CaptionState  # noqa: E402
from app.replay import load_capture  # noqa: E402

#: The two sessions of 2026-10-24, in `logs/` - which is gitignored, so these
#: tests skip on a fresh clone. They are here because they are the only
#: recording of the session the Auto view was reported wrong on, and the
#: measurement they make is the one that would have caught a server-side
#: version of it.
LIVE_LOGS = ("live-2026-10-24-143245.jsonl", "live-2026-10-24-154831.jsonl")
LOG_DIR = ROOT / "logs"

#: How long the projector may legitimately show the language just spoken.
#: Translation trails its speech by about a second and the first chunk after
#: a language switch can take several, so the newest `auto` paragraph is in
#: the spoken language briefly at every turn. Measured over both sessions:
#: 12.1 s and 7.2 s at worst. Minutes of it is the reported fault.
MAX_WRONG_SIDE_SECONDS = 20.0

#: How far back "what is being spoken" looks.
SPOKEN_WINDOW_MS = 10_000

#: Every word says what it is. `auto` holding one `orig` is the bug this
#: file was written for, and it is a substring test rather than a judgement.
ORIGINAL_EN = "englishorig{n}"
ORIGINAL_AR = "عربيةorig{n}"
TRANSLATED_EN = "englishtrans{n}"
TRANSLATED_AR = "عربيةtrans{n}"

#: Latin-script technical terms spoken inside Arabic sentences. Soniox labels
#: these `ar` (measured across all five captures), so they are originals in
#: the Arabic stream and must not reach `auto` or flip a direction.
TERMS = ("RAG", "LLM", "MCP", "embeddings", "ChatGPT")

#: Roughly how long a word takes to say. Only used to make `start_ms` look
#: like speech; `CaptionState` has no clock.
WORD_MS = 320


class Talk:
    """A synthetic bilingual talk, in turns, with its own audio clock."""

    def __init__(self, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self.clock = 0
        self.word = 0
        self.spoken = {"en": 0, "ar": 0}

    def turn(self, language: str, words: int, *, terms: bool = False
             ) -> list[dict]:
        """One turn of speech, as the responses Soniox would send for it.

        Originals arrive first and their translation about a second later,
        which is what the captures show; both are split into small chunks so
        the stream looks like the real thing rather than one block per turn.
        """
        other = "ar" if language == "en" else "en"
        responses: list[dict] = []
        pending: list[dict] = []
        for index in range(words):
            self.word += 1
            if terms and index and index % 11 == 0:
                # A term spoken inside the sentence, in the other script,
                # labelled with the language of the sentence around it.
                text = " " + TERMS[self.word % len(TERMS)]
            else:
                template = ORIGINAL_EN if language == "en" else ORIGINAL_AR
                text = " " + template.format(n=self.word)
            start = self.clock
            self.clock += WORD_MS
            self.spoken[language] += 1
            pending.append({
                "text": text, "is_final": True, "language": language,
                "translation_status": "original",
                "start_ms": start, "end_ms": self.clock,
            })
            template = TRANSLATED_AR if other == "ar" else TRANSLATED_EN
            translation = {
                "text": " " + template.format(n=self.word), "is_final": True,
                "language": other, "source_language": language,
                "translation_status": "translation",
            }
            # Chunked, and the translation trails the speech by a word or
            # two - the measured 0.8-1.0 s.
            if len(pending) >= self.rng.choice((1, 2, 3, 4)):
                responses.append({"tokens": list(pending)})
                pending = []
            responses.append({"tokens": [translation]})
        if pending:
            responses.append({"tokens": list(pending)})
        return responses

    def endpoint(self, language: str) -> dict:
        return {"tokens": [{"text": "<end>", "is_final": True,
                            "translation_status": "none"}]}

    def script(self) -> list[dict]:
        """About three hours of talk: monologues, Q&A, and a long stretch.

        Shaped after the real sessions. Minutes are approximate - what
        matters is the total volume and that every pattern appears.
        """
        out: list[dict] = []

        def speech(language: str, minutes: float, **kwargs) -> None:
            # ~150 words a minute.
            words = int(minutes * 150)
            while words > 0:
                chunk = min(words, self.rng.randint(20, 60))
                out.extend(self.turn(language, chunk, **kwargs))
                out.append(self.endpoint(language))
                words -= chunk

        speech("ar", 6, terms=True)          # the welcome
        speech("en", 4)
        # The 12-minute single-language stretch: run1 measured one and the
        # 2026-10-24 talk held 12m42s of Arabic.
        speech("ar", 13, terms=True)
        speech("en", 14)
        for _ in range(40):                  # rapid back-and-forth: Q&A
            speech("ar", 0.15, terms=True)
            speech("en", 0.2)
        speech("en", 20)
        speech("ar", 20, terms=True)
        speech("en", 25)
        speech("ar", 25, terms=True)
        speech("en", 30)
        speech("ar", 25, terms=True)
        return out


class Client:
    """`web/app.js`'s half of the protocol, so the diffs can be checked.

    A snapshot replaces and is authoritative; a diff upserts by id; a diff
    whose `base` is ahead of what this client has asks for a snapshot instead
    of painting a hole.
    """

    def __init__(self) -> None:
        self.seq = 0
        self.streams: dict[str, dict[int, dict]] = {}
        self.resyncs = 0
        self.want_snapshot = True

    def handle(self, message: dict) -> None:
        if message["type"] == "snapshot":
            self.streams = {
                name: {p["id"]: p for p in paras}
                for name, paras in message["streams"].items()
            }
            self.want_snapshot = False
        else:
            base = message.get("base")
            if isinstance(base, int) and base > self.seq:
                self.resyncs += 1
                self.want_snapshot = True
                return
            for name, paras in message["streams"].items():
                held = self.streams.setdefault(name, {})
                for para in paras:
                    held[para["id"]] = para
        self.seq = message["seq"]

    def text(self, stream: str, ids: set[int]) -> str:
        return "\u0001".join(
            self.streams.get(stream, {})[pid].get("final", "")
            + self.streams.get(stream, {})[pid].get("partial", "")
            for pid in sorted(ids)
        )


def server_text(state: CaptionState, stream: str, ids: set[int]) -> str:
    return "\u0001".join(
        p.final + p.partial for p in state.streams[stream] if p.id in ids
    )


def script_of(text: str) -> str:
    arabic = sum(1 for ch in text if 0x0600 <= ord(ch) <= 0x06FF)
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if not arabic and not latin:
        return "none"
    return "ar" if arabic >= latin else "en"


def test_a_three_hour_talk_never_puts_the_original_on_the_projector():
    """The headline invariant, over about three hours of simulated talk.

    Every word is labelled, so "`auto` is the translated half only" is
    checked on the text itself rather than on the routing that produced it.
    """
    talk = Talk()
    state = CaptionState()
    client = Client()
    rng = random.Random(11)
    last_sent = 0
    seen_ids: set[int] = set()
    highest = 0
    responses = talk.script()
    assert len(responses) > 20_000, "the script is too short to be a soak"

    checkpoints = 0
    for index, response in enumerate(responses):
        state.add_response(response)

        # Only what this response touched: a paragraph cannot go wrong while
        # nothing is writing to it, and scanning all 200 of them on each of
        # 20,000 responses turns a soak into a four-minute test.
        for para in state.streams[AUTO]:
            if para.changed_seq != state.seq:
                continue
            text = para.final + para.partial
            assert "orig" not in text, (
                f"response {index}: an original reached the auto stream "
                f"(paragraph {para.id})")
            # Partials as well as finals, which is what the page renders -
            # and the 2026-10-24 report was of *grey* text, which is a
            # partial being drawn. `auto` must never have one: no translation
            # token is ever non-final, and `_apply_partials` covers the two
            # output languages only.
            assert not para.partial, (
                f"response {index}: auto paragraph {para.id} is carrying "
                f"unconfirmed text, which the projector would draw grey")
            if not text.strip():
                continue
            assert para.lang in ("en", "ar"), (
                f"response {index}: auto paragraph {para.id} has no language")
            assert para.direction == ("rtl" if para.lang == "ar" else "ltr")
            shown = script_of(text)
            if shown != "none":
                assert shown == para.lang, (
                    f"response {index}: auto paragraph {para.id} is labelled "
                    f"{para.lang} and written in {shown}")

        for name, paras in state.streams.items():
            assert len(paras) <= MAX_PARAGRAPHS, (
                f"{name} holds {len(paras)} paragraphs, over the cap")
            if paras:
                tail = paras[-1]
                assert tail.id >= highest or tail.id in seen_ids, (
                    f"paragraph id {tail.id} went backwards")
                seen_ids.add(tail.id)
                highest = max(highest, tail.id)

        # Flush like the hub does, at a varying rate, and compare the
        # client's reconstruction with the server's streams.
        if index % rng.choice((1, 2, 5, 9, 17)) == 0:
            if client.want_snapshot:
                message = state.snapshot()
                client.handle(message)
                last_sent = message["seq"]
            elif last_sent < state.seq:
                client.handle(state.diff_since(last_sent))
                last_sent = state.seq
            if rng.random() < 0.002:
                # A viewer reconnecting: a fresh page, opened with a
                # snapshot. A three-hour session will see several.
                client = Client()
                last_sent = 0
            if not client.want_snapshot and rng.random() < 0.05:
                checkpoints += 1
                for name in state.streams:
                    held = {p.id for p in state.streams[name]}
                    mine = set(client.streams.get(name, {}))
                    shared = held & mine
                    assert client.text(name, shared) == server_text(
                        state, name, shared), (
                        f"response {index}: the page's {name} stream has "
                        f"drifted from the server's")
                    assert not held - mine, (
                        f"response {index}: the page is missing "
                        f"{len(held - mine)} {name} paragraph(s)")

    assert checkpoints > 50, "the protocol was barely checked"
    # The talk really was three hours of both languages, in both directions.
    assert talk.clock > 3 * 3600 * 1000 * 0.8, "less talk than intended"
    assert talk.spoken["en"] > 5_000 and talk.spoken["ar"] > 5_000
    assert highest > 2_000, f"only {highest} paragraphs over three hours"


def test_the_auto_stream_switches_as_often_as_the_talk_does():
    """A soak that never changed direction would prove nothing.

    The rapid back-and-forth in the middle of the script is there to make the
    hysteresis work; this checks it actually produced both languages, in
    alternation, rather than one long block.
    """
    talk = Talk(seed=3)
    state = CaptionState()
    switches = 0
    previous = None
    langs: set[str] = set()
    for response in talk.script():
        state.add_response(response)
        # As it goes, not at the end: the 200-paragraph cap means the last
        # snapshot holds only the final few minutes, which is one language.
        tail = state.streams[AUTO][-1] if state.streams[AUTO] else None
        if tail is None or not tail.final.strip() or not tail.lang:
            continue
        langs.add(tail.lang)
        if tail.lang != previous:
            switches += 1
            previous = tail.lang
    assert langs == {"en", "ar"}, f"auto only ever showed {langs}"
    assert switches >= 40, (
        f"the auto stream changed direction {switches} times; the script has "
        f"40 rapid exchanges alone")


# ---------------------------------------------------------------------------
# The 2026-10-24 sessions themselves
# ---------------------------------------------------------------------------


def longest_wrong_side(path: Path) -> tuple[float, float]:
    """`(longest run, when it started)` in seconds, over a response log.

    "Wrong side" is the newest `auto` paragraph being written in the language
    the room has just been hearing - the projector showing the original. The
    newest paragraph is what a bottom-anchored pane shows, so this is as
    close to "what the room read" as a log can get.
    """
    _, records = load_capture(path)
    state = CaptionState()
    recent: list[tuple[int, str, int]] = []
    worst = 0.0
    worst_at = 0.0
    run_from: int | None = None
    for row in records:
        offset = row.get("recv_offset_ms") or 0
        response = row["response"]
        for token in response.get("tokens") or []:
            if not token.get("is_final"):
                continue
            text = token.get("text") or ""
            if not text or text == "<end>":
                continue
            if token.get("translation_status") == "original":
                language = token.get("language")
                if language in ("en", "ar"):
                    recent.append((offset, language, len(text)))
        state.add_response(response)

        recent = [r for r in recent if r[0] >= offset - SPOKEN_WINDOW_MS]
        english = sum(n for _at, lang, n in recent if lang == "en")
        arabic = sum(n for _at, lang, n in recent if lang == "ar")
        if not english and not arabic:
            continue
        spoken = "en" if english > arabic else "ar"
        newest = None
        for para in reversed(state.streams[AUTO]):
            if para.final.strip():
                newest = para
                break
        if newest is None:
            continue
        if script_of(newest.final) == spoken:
            if run_from is None:
                run_from = offset
            elif (offset - run_from) / 1000 > worst:
                worst = (offset - run_from) / 1000
                worst_at = run_from / 1000
        else:
            run_from = None
    return worst, worst_at


@pytest.mark.parametrize("name", LIVE_LOGS)
def test_the_projector_never_held_the_spoken_language_for_long(name):
    """The 2026-10-24 incident, measured against the recording of it.

    The Auto view was reported showing English while English was being
    spoken, somewhere after the half-hour mark. Replayed through
    `CaptionState`, neither session does that for longer than the time
    translation takes to arrive - so whatever the room saw, the caption
    stream behind it was right, and this is the measurement that says so.
    """
    path = LOG_DIR / name
    if not path.exists():
        pytest.skip(f"{name} is not in logs/ (gitignored; operator's own)")
    worst, worst_at = longest_wrong_side(path)
    assert worst <= MAX_WRONG_SIDE_SECONDS, (
        f"{name}: the auto stream showed the language being spoken for "
        f"{worst:.1f} s from {worst_at:.0f} s in")


@pytest.mark.parametrize("name", LIVE_LOGS)
def test_no_original_reached_the_projector_in_either_session(name):
    """A pass-through token is the one way an original can get in.

    One response in the 52-minute session held 21 of them - 43 characters of
    Arabic marked as a translation of Arabic - and they landed in an
    English-labelled `auto` paragraph. With the guard in `_is_translation`
    they do not, and this says so against the log itself.
    """
    path = LOG_DIR / name
    if not path.exists():
        pytest.skip(f"{name} is not in logs/ (gitignored; operator's own)")
    _, records = load_capture(path)
    state = CaptionState()
    found = 0
    for row in records:
        response = row["response"]
        # Joined, and checked in the response it arrived in. The tokens are
        # sub-word pieces of one or two characters, so a single one matches
        # ordinary Arabic by accident; the phrase they spell does not. And
        # the 200-paragraph cap means a check at the end of a 52-minute
        # session could be looking past the place it happened.
        phrase = "".join(
            token.get("text") or "" for token in response.get("tokens") or []
            if token.get("translation_status") == "translation"
            and token.get("language") is not None
            and token.get("language") == token.get("source_language")
        ).strip()
        state.add_response(response)
        if len(phrase) < 8:
            continue
        found += 1
        assert phrase not in state.render(AUTO), (
            f"{name}: {len(phrase)} characters of pass-through text - an "
            f"original marked as its own translation - reached the projector")
    if name == "live-2026-10-24-143245.jsonl":
        assert found == 1, (
            f"the 52-minute session held one pass-through burst, not {found}; "
            f"if the log changed, this test is measuring something else")
