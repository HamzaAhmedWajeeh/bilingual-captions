"""The `auto` stream: translation only, direction decided per paragraph.

`auto` is what the projector shows. It carries the *translated* half of both
directions in the order it was spoken - Arabic speech in English, English
speech in Arabic - and never the original, because the room can already hear
that. Each paragraph carries its own `lang`/`dir`, since consecutive ones
disagree.

**What the real data says**, measured 2026-10-18 over
`tests/fixtures/run1.jsonl`, `run4-saudi.jsonl` and
the two 2026-10-17 dry-run response logs:

- Translation tokens carry `language` (the *target*) and `source_language`.
  Both are always present; no translation token in any file lacked either.
- **Embedded terms never move the label.** run1 and run3 together hold 64
  Latin-script words sitting inside Arabic speech - "AI", "reinforcement
  learning", "transformers", "hallucinations" - and Soniox labels every one
  of them `ar`. Zero Latin words were labelled `en` inside Arabic speech, and
  zero Arabic words `en`, across all five files. An English term inside an
  Arabic sentence therefore produces no English-targeted translation token at
  all, and cannot flip the direction whatever the hysteresis does.
- **Translation tokens are always final** - 1,123 of 1,123 in the main dry
  run, 613 of 613 in the second, 0 non-final. So `auto` never has a partial.
- Translation arrives about 0.8-1.0 s after the speech it covers (p99 1.6 s
  and 3.9 s in the two dry runs), in chunks of 20-28 characters, roughly
  every 2 s with gaps up to 8 s.

The hysteresis is therefore defensive rather than load-bearing, and these
tests say so: the embedded-term case is proved against the real capture, and
the blip case is constructed, because the engine has never produced one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.captions import (  # noqa: E402
    AUTO,
    MIN_SWITCH_CHARS,
    CaptionState,
)
from app.replay import responses  # noqa: E402
from captures import needs_captures  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def original(text: str, lang: str) -> dict:
    return {"text": text, "is_final": True, "language": lang,
            "translation_status": "original"}


def translated(text: str, *, into: str, source: str) -> dict:
    """A translation token, shaped exactly as Soniox sends one.

    No `start_ms`/`end_ms`: translation tokens carry neither, which is why
    lag had to be measured against the originals rather than read off them.
    """
    return {"text": text, "is_final": True, "language": into,
            "source_language": source, "translation_status": "translation"}


def feed(state: CaptionState, *tokens: dict) -> None:
    state.add_response({"tokens": list(tokens)})


def auto_blocks(state: CaptionState) -> list[tuple[str, str]]:
    """`(lang, text)` per non-empty `auto` paragraph."""
    return [(p.lang, (p.final + p.partial).strip())
            for p in state.streams[AUTO]
            if (p.final + p.partial).strip()]


def replay(name: str) -> CaptionState:
    state = CaptionState()
    for response in responses(FIXTURES / f"{name}.jsonl"):
        state.add_response(response)
    return state


# ---------------------------------------------------------------------------
# The six cases named in the brief
# ---------------------------------------------------------------------------


def test_pure_english_speech_becomes_one_arabic_block():
    state = CaptionState()
    feed(state,
         original("The index rebuilds", "en"),
         translated("يُعاد بناء الفهرس", into="ar", source="en"))
    feed(state,
         original(" every few minutes.", "en"),
         translated(" كل بضع دقائق.", into="ar", source="en"))

    assert auto_blocks(state) == [("ar", "يُعاد بناء الفهرس كل بضع دقائق.")]
    assert state.streams[AUTO][0].direction == "rtl"
    # The original never appears in `auto`.
    assert "The index rebuilds" not in state.render(AUTO)


def test_pure_arabic_speech_becomes_one_english_block():
    state = CaptionState()
    feed(state,
         original("يُعاد بناء الفهرس", "ar"),
         translated("The index rebuilds", into="en", source="ar"))
    feed(state,
         original(" كل بضع دقائق.", "ar"),
         translated(" every few minutes.", into="en", source="ar"))

    assert auto_blocks(state) == [("en", "The index rebuilds every few minutes.")]
    assert state.streams[AUTO][0].direction == "ltr"
    assert "يُعاد بناء الفهرس" not in state.render(AUTO)


@needs_captures
def test_arabic_with_english_terms_never_flips_direction():
    """The case the brief is most worried about, proved on the real capture.

    `run1` is Arabic speech about AI: 35 Latin-script words - "AI",
    "reinforcement learning", "transformers" - sit inside it, and Soniox
    labels all of them `ar`. The whole capture must come out as one
    direction.
    """
    state = replay("run1")
    blocks = auto_blocks(state)
    assert blocks, "the auto stream is empty"
    languages = {lang for lang, _ in blocks}
    assert languages == {"en"}, (
        f"Arabic speech with English terms produced {languages}; an embedded "
        f"term flipped the direction")
    # And the terms really are in there, so this is not passing vacuously.
    text = state.render(AUTO)
    assert "AI" in text


def test_an_english_term_inside_arabic_does_not_open_a_block():
    """The constructed blip: a stray English-targeted token mid-Arabic.

    Soniox has never emitted this - an embedded term keeps the `ar` label -
    so it is built by hand. It is short and ends no sentence, so it is kept
    in the block it interrupted rather than becoming one of its own.
    """
    state = CaptionState()
    feed(state, original("حديث طويل", "ar"),
         translated("A long talk about models", into="en", source="ar"))
    feed(state, original(" عن", "ar"),
         translated(" RAG", into="ar", source="en"))      # the blip
    feed(state, original(" النماذج.", "ar"),
         translated(" and retrieval.", into="en", source="ar"))

    blocks = auto_blocks(state)
    assert len(blocks) == 1, f"the blip opened a block: {blocks}"
    assert blocks[0][0] == "en"
    # Nothing was thrown away: the blip is still on screen.
    assert "RAG" in blocks[0][1]


def test_english_with_an_arabic_phrase_opens_a_block_when_it_is_a_sentence():
    """A real turn, however short, gets its own direction.

    A short turn is five words and ends a sentence, so it is not a blip.
    Turns like this are the common case at the start of a session, and they
    have to break cleanly rather than being folded into the block before.
    """
    state = CaptionState()
    feed(state, original("We will start with the layout", "en"),
         translated("سنبدأ بالتخطيط العام", into="ar", source="en"))
    feed(state, original(" والسؤال عن الفهرس.", "ar"),
         translated(" And the question is about the index.", into="en",
                    source="ar"))
    feed(state, original(" Now the second half.", "en"),
         translated(" الآن النصف الثاني.", into="ar", source="en"))

    blocks = auto_blocks(state)
    assert [lang for lang, _ in blocks] == ["ar", "en", "ar"], blocks
    assert blocks[1][1] == "And the question is about the index."
    assert [p.direction for p in state.streams[AUTO] if p.final.strip()] == [
        "rtl", "ltr", "rtl"]


def test_question_and_answer_alternating_languages():
    """Four turns, four blocks, alternating direction."""
    state = CaptionState()
    turns = [
        ("en", "ar", "What is the plan for the next quarter?",
         "ما هي الخطة للربع القادم؟"),
        ("ar", "en", "سنركز على المنتج أولًا.",
         "We will focus on the product first."),
        ("en", "ar", "And what about hiring more people?",
         "وماذا عن توظيف المزيد من الأشخاص؟"),
        ("ar", "en", "التوظيف يأتي بعد ذلك.",
         "Hiring comes after that."),
    ]
    for source, target, spoken, rendered in turns:
        feed(state, original(" " + spoken, source),
             translated(" " + rendered, into=target, source=source))

    blocks = auto_blocks(state)
    assert [lang for lang, _ in blocks] == ["ar", "en", "ar", "en"], blocks
    assert blocks[1][1] == "We will focus on the product first."
    assert blocks[3][1] == "Hiring comes after that."


def test_untranslated_audio_leaves_auto_empty():
    """Speech with no translation at all puts nothing on the main screen.

    A one-way or untranslated session must show an empty `auto` rather than
    the original text - the whole promise of this view is that it never shows
    what the room can already hear.
    """
    state = CaptionState()
    feed(state, original("This is only ever transcribed", "en"))
    feed(state, original(" and never translated.", "en"))

    assert state.streams[AUTO] == []
    assert state.render(AUTO) == ""
    assert state.render("en").strip()        # it did reach the normal stream


# ---------------------------------------------------------------------------
# Shape, hysteresis and bookkeeping
# ---------------------------------------------------------------------------


def test_auto_is_a_third_stream_in_the_snapshot():
    state = CaptionState()
    feed(state, original("سؤالي عن الفهرس", "ar"),
         translated("My question is about the index", into="en", source="ar"))
    snap = state.snapshot()
    assert set(snap["streams"]) == {"ar", "en", AUTO}
    para = snap["streams"][AUTO][0]
    assert para["lang"] == "en" and para["dir"] == "ltr"


def test_only_auto_paragraphs_carry_a_direction():
    """`ar` and `en` state their language once on the container."""
    state = CaptionState()
    feed(state, original("سؤالي عن الفهرس", "ar"),
         translated("My question is about the index", into="en", source="ar"))
    for stream in ("ar", "en"):
        for para in state.streams[stream]:
            assert para.lang == ""
            assert "dir" not in para.as_dict()


def test_a_long_blip_does_open_a_block():
    """Below `MIN_SWITCH_CHARS` it is absorbed; above it, it is a turn."""
    long_enough = "x" * (MIN_SWITCH_CHARS + 1)
    state = CaptionState()
    feed(state, original("حديث", "ar"),
         translated("A talk about things", into="en", source="ar"))
    feed(state, original(" كلام", "ar"),
         translated(" " + long_enough, into="ar", source="en"))

    assert [lang for lang, _ in auto_blocks(state)] == ["en", "ar"]


def test_auto_has_no_partials():
    """Translation tokens are always final, so nothing in `auto` is grey."""
    state = CaptionState()
    feed(state,
         {"text": "mid", "is_final": False, "language": "en",
          "translation_status": "original"},
         original("سؤالي عن الفهرس", "ar"),
         translated("My question is about the index", into="en", source="ar"))
    assert all(p.partial == "" for p in state.streams[AUTO])


@needs_captures
@pytest.mark.parametrize("name", ["run1", "run2-oneway", "run4-saudi",
                                  "run5-english"])
def test_auto_is_exactly_the_translated_text_of_both_streams(name):
    """No text is invented and none is lost.

    Every character in `auto` came from a translation token, so the two
    language streams together must contain all of it.
    """
    state = replay(name)
    auto_text = "".join(state.render(AUTO).split())
    both = "".join((state.render("ar") + state.render("en")).split())
    assert auto_text, f"{name} produced an empty auto stream"
    # `auto` is a subsequence of the translated halves; compare character
    # multisets, which is strong enough to catch dropped or duplicated runs.
    missing = [c for c in set(auto_text) if auto_text.count(c) > both.count(c)]
    assert not missing, f"{name}: auto invented characters {missing[:10]}"


def test_a_notice_reaches_the_main_screen_too():
    state = CaptionState()
    feed(state, original("سؤالي عن الفهرس", "ar"),
         translated("My question is about the index", into="en", source="ar"))
    state.add_notice()
    notices = [p for p in state.streams[AUTO] if p.notice]
    assert len(notices) == 1
    assert "انقطع" in notices[0].final and "interrupted" in notices[0].final


def test_a_notice_resets_the_direction_hysteresis():
    """After a gap the next translation starts its own block, whatever it is."""
    state = CaptionState()
    feed(state, original("سؤالي عن الفهرس", "ar"),
         translated("My question is about the index, please", into="en",
                    source="ar"))
    state.add_notice()
    feed(state, original("Right", "en"),
         translated("بالتأكيد", into="ar", source="en"))

    tail = [p for p in state.streams[AUTO] if not p.notice][-1]
    assert tail.lang == "ar" and tail.direction == "rtl"


def test_a_reconnect_does_not_glue_words_together_in_auto():
    """The run7 `behindMicrosoft` bug, on the third stream."""
    state = CaptionState()
    feed(state, original("مرحبا", "ar"),
         translated("behind", into="en", source="ar"))
    state.mark_gap()
    feed(state, original(" نعم", "ar"),
         translated("Microsoft", into="en", source="ar"))

    assert "behindMicrosoft" not in state.render(AUTO)
    assert "behind Microsoft" in state.render(AUTO)


def test_a_translation_into_its_own_source_language_is_an_original():
    """Soniox sends these, and `auto` must not take them.

    One response 18.9 minutes into the 2026-10-24 talk held 21 tokens marked
    `translation` whose `source_language` was `ar` and whose target was also
    `ar` - 43 characters of Arabic described as its own translation. The
    `auto` stream's one promise is that the room never reads the words it
    just heard, so a pass-through goes to its language's captions as the
    original it is, and nowhere near the projector's stream.
    """
    state = CaptionState()
    feed(state, original("مرحبا بالجميع", "ar"),
         translated(" Hello everyone", into="en", source="ar"))
    before = state.render(AUTO)

    feed(state, {"text": " كلام عربي", "is_final": True, "language": "ar",
                 "source_language": "ar",
                 "translation_status": "translation"})

    assert "كلام عربي" not in state.render(AUTO), (
        "a translation of Arabic into Arabic reached the auto stream")
    assert state.render(AUTO) == before, "auto changed at all"
    assert "كلام عربي" in state.render("ar"), (
        "the text was dropped instead of being kept as an original")
    tail = state.streams["ar"][-1]
    assert tail.spoken_chars and not tail.translated_chars, (
        "a pass-through was counted as translated text, which would put it "
        "under the wrong heading in bilingual.md")


def test_a_translation_with_no_target_language_still_counts_as_one():
    """The guard must not catch the `language: null` case.

    Five tokens in the same log carried no `language` at all, only
    `source_language: en`. Those are real translations - the target is "the
    one that isn't the source" - and they belong in `auto`.
    """
    state = CaptionState()
    feed(state, original("Hello", "en"),
         {"text": " مرحبا", "is_final": True, "source_language": "en",
          "translation_status": "translation"})
    assert "مرحبا" in state.render(AUTO)
    assert state.streams[AUTO][-1].lang == "ar"


def test_auto_breaks_long_blocks_on_size():
    """One speaker talking for a long time must not become one paragraph."""
    state = CaptionState()
    for i in range(60):
        feed(state, original(f" كلمة{i}", "ar"),
             translated(f" word{i} and some more text here", into="en",
                        source="ar"))
    paras = [p for p in state.streams[AUTO] if p.final.strip()]
    assert len(paras) > 1, "the auto stream never broke a long monologue"
    assert all(p.lang == "en" for p in paras)
