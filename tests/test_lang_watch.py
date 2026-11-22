"""Does the detector catch a language lock, and leave a healthy talk alone?

Every token in here is synthetic. The real evidence is six evenings of
response logs in `logs/`, which `tools/lang_calibrate.py` scores and which
`test_lang_watch_live.py` pins as a regression when they are present; this
file is the mechanism, built out of made-up words so it runs on a clone with
no logs at all.

No real caption text, here or anywhere: the Arabic in these tests is a
handful of transliterated English function words - the detector's own
lexicon, which is committed - and a few invented Arabic words that are not
in it.
"""

from __future__ import annotations

import pytest

from app.lang_watch import (
    ARABIC_LOOKALIKES,
    DRIFT,
    FUNCTION_WORDS,
    MIN_LETTERS,
    STUCK,
    LanguageWatch,
    fold,
    is_function_word,
    is_translation,
    score_words,
)

# Transliterated English function words, from the lexicon: *of, is, from,
# this, not, now, think, how, what, because*.
TRANSLIT = ["اوف", "ايز", "فروم", "ذس", "نوت", "ناو", "ثينك", "هاو", "وات",
            "بيكوز"]

# Ordinary Arabic words that are not in the lexicon and are not English.
ARABIC = ["مرحبا", "الكلام", "نتكلم", "اليوم", "موضوع", "فكرة", "شركة",
          "السوق", "مشروع", "تجربة"]

# English terms a bilingual speaker drops into Arabic speech, which is the
# healthy case the detector must not mistake for the fault.
TERMS = ["embeddings", "RAG", "fine-tuning", "transformers", "LLM"]


def token(text: str, *, lang: str = "ar", final: bool = True,
          translation: bool = False, source: str | None = None) -> dict:
    out = {
        "text": text,
        "is_final": final,
        "language": lang,
        "translation_status": "translation" if translation else "original",
    }
    if source is not None:
        out["source_language"] = source
    return out


def response(*texts: str, **kwargs) -> dict:
    """One response whose tokens each carry a leading space, as Soniox's do."""
    return {"tokens": [token(" " + text, **kwargs) for text in texts]}


def feed(watch: LanguageWatch, words: list[str], *, start: float = 0.0,
         step: float = 0.5, **kwargs) -> list:
    """Hand `words` over one response each, `step` seconds apart."""
    signals = []
    now = start
    for word in words:
        signals.extend(watch.note_response(response(word, **kwargs), now))
        now += step
    # A word is only complete once the whitespace after it arrives, so the
    # last one is still half-built. Close it the way the next turn would.
    signals.extend(watch.note_response(response(""), now))
    return signals


# ---------------------------------------------------------------------------
# The lexicon
# ---------------------------------------------------------------------------


def test_the_lexicon_holds_no_form_shorter_than_the_floor():
    """A one or two letter form is a fragment, not a word worth judging.

    This is what kept `هي`, `شي`, `مي` and `نو` out, and they are where
    Arabic and transliterated English collide hardest.
    """
    short = [word for word in FUNCTION_WORDS if len(word) < MIN_LETTERS]
    assert short == []


def test_the_lexicon_is_folded_already():
    """Every entry is its own folded form, or it could never match.

    `fold` is applied to the *incoming* word, so an entry spelled with a
    hamza or a ta-marbuta would sit in the set unreachable. Cheap to assert
    and invisible otherwise.
    """
    unfolded = sorted(word for word in FUNCTION_WORDS if fold(word) != word)
    assert unfolded == []


def test_the_arabic_lookalikes_are_not_in_the_lexicon():
    """The four forms calibration found, kept out on purpose.

    Each transliterates an English function word *and* spells an ordinary
    Arabic one, and between them they were 20 of the first lexicon's 43
    false matches across 8,928 words of healthy speech.
    """
    assert ARABIC_LOOKALIKES
    assert not (FUNCTION_WORDS & ARABIC_LOOKALIKES)
    for word in ARABIC_LOOKALIKES:
        assert not is_function_word(word), word


def test_folding_makes_one_spelling_of_one_sound():
    """Soniox writes both `اوف` and `أوف` for *of* in the same session."""
    assert is_function_word("اوف")
    assert is_function_word("أوف")
    assert is_function_word("اوف،")      # punctuation is not a letter
    assert is_function_word("اوف.")


def test_an_ordinary_arabic_word_is_not_a_function_word():
    for word in ARABIC:
        assert not is_function_word(word), word
    assert score_words(ARABIC) == 0.0


def test_a_latin_word_is_never_a_function_word():
    """The lexicon is Arabic-script only: `of` in Latin letters is English
    text in an English stream, which is the healthy case."""
    for word in ["of", "is", "think", "RAG"]:
        assert not is_function_word(word)


# ---------------------------------------------------------------------------
# Words, not tokens
# ---------------------------------------------------------------------------


def test_words_are_reassembled_from_sub_word_fragments():
    """Soniox tokens are fragments, and counting fragments measures nothing.

    This was got wrong first, and the symptom was single Arabic letters at
    the top of the frequency table. `بيكوز` arriving as three pieces must
    score exactly as it does arriving whole.
    """
    watch = LanguageWatch(act=False, min_window_words=1)
    watch.note_response({"tokens": [
        token(" بي"), token("كو"), token("ز"), token(" مرحبا"),
    ]}, 0.0)
    # Two words so far, not four tokens: the three fragments are one word,
    # and the fourth token's leading space is what completed it. `مرحبا` is
    # still half-built until the next space arrives.
    watch.note_response(response("مرحبا"), 1.0)
    share, words = watch.score(1.0)
    assert words == 2
    assert share == pytest.approx(0.5)


def test_a_word_split_across_two_responses_is_still_one_word():
    watch = LanguageWatch(act=False, min_window_words=1)
    watch.note_response({"tokens": [token(" ثي")]}, 0.0)
    watch.note_response({"tokens": [token("نك"), token(" مرحبا")]}, 0.5)
    watch.note_response(response("مرحبا"), 1.0)
    share, words = watch.score(1.0)
    assert words == 2
    assert share == pytest.approx(0.5)


def test_non_final_tokens_are_ignored():
    """Non-finals are re-sent in full every response; counting them would
    score the same words over and over."""
    watch = LanguageWatch(act=False, min_window_words=1)
    for _ in range(10):
        watch.note_response(response(*TRANSLIT, final=False), 0.0)
    assert watch.score(1.0) == (0.0, 0)


def test_translations_are_ignored():
    """The question is what the *originals* look like. A translation into
    Arabic is Arabic by construction and says nothing about the room."""
    watch = LanguageWatch(act=False, min_window_words=1)
    feed(watch, TRANSLIT * 6, translation=True, source="en")
    assert watch.score(100.0)[1] == 0


def test_a_pass_through_token_is_treated_as_an_original():
    """`CaptionState._is_translation`'s rule, and the same reasoning.

    It happened again twice on 2026-10-31, once per session, 21 and 16
    tokens in a single response each time.
    """
    assert not is_translation(token("x", translation=True, source="ar"))
    assert is_translation(token("x", translation=True, source="en"))
    assert is_translation(token("x", translation=True))
    assert not is_translation(token("x"))


# ---------------------------------------------------------------------------
# What must not fire
# ---------------------------------------------------------------------------


def test_normal_code_switching_does_not_fire():
    """Arabic speech with English terms in Latin script is the healthy case,
    and it is what `translation_terms` is configured to produce."""
    words = []
    for index in range(80):
        words.append(ARABIC[index % len(ARABIC)])
        if index % 4 == 0:
            words.append(TERMS[index % len(TERMS)])
    watch = LanguageWatch(act=False)
    signals = feed(watch, words)
    assert signals == []
    assert watch.peak_share == 0.0


def test_pure_arabic_does_not_fire():
    watch = LanguageWatch(act=False)
    signals = feed(watch, ARABIC * 20)
    assert signals == []


def test_english_speech_in_english_does_not_fire():
    """The engine working: English arrives labelled English, in Latin."""
    watch = LanguageWatch(act=False)
    signals = feed(watch, ["this", "is", "working", "fine"] * 30, lang="en")
    assert signals == []
    assert watch.words_seen == 0


def test_a_short_burst_does_not_fire():
    """Below the floor there is nothing to judge: a handful of borrowed
    words is not a locked stream."""
    watch = LanguageWatch(act=False)
    signals = feed(watch, TRANSLIT[:3] + ARABIC[:5])
    assert signals == []


def test_a_window_cannot_be_stitched_out_of_two_stretches():
    """Two bursts ten minutes apart are not one window.

    Without the time bound, a talk that borrowed four English words every
    ten minutes would eventually fill a word-counted window with them.
    """
    watch = LanguageWatch(act=False)
    now = 0.0
    signals = []
    for _ in range(12):
        signals.extend(feed(watch, TRANSLIT[:4], start=now, step=0.5))
        now += 600.0
    assert signals == []


# ---------------------------------------------------------------------------
# What must fire
# ---------------------------------------------------------------------------


def test_transliterated_english_fires():
    """The 2026-10-31 fault: an English speaker written in Arabic letters."""
    watch = LanguageWatch(act=False)
    # A realistic mix - function words are about a quarter of running
    # English text, and the real sessions scored 0.125 to 0.379.
    words = []
    for index in range(60):
        words.append(TRANSLIT[index % len(TRANSLIT)])
        words.append(ARABIC[index % len(ARABIC)])
        words.append(ARABIC[(index + 3) % len(ARABIC)])
    signals = feed(watch, words)
    stuck = [s for s in signals if s.kind == STUCK]
    assert stuck, "a third of the window transliterated English must fire"
    assert stuck[0].share >= watch.stuck_share
    assert stuck[0].words >= watch.min_window_words
    assert stuck[0].as_dict()["threshold"] == watch.stuck_share


def test_the_signal_carries_numbers_and_no_words():
    """The event log holds neither speech nor session details."""
    watch = LanguageWatch(act=False)
    signals = feed(watch, (TRANSLIT * 8))
    stuck = [s for s in signals if s.kind == STUCK]
    assert stuck
    payload = stuck[0].as_dict()
    blob = repr(payload)
    for word in TRANSLIT + ARABIC:
        assert word not in blob
    assert set(payload) >= {"at", "share", "words", "threshold"}
    assert all(isinstance(value, (int, float, bool, str))
               for value in payload.values())


# ---------------------------------------------------------------------------
# Cooldown, cap and reset
# ---------------------------------------------------------------------------


def test_the_cooldown_holds_a_second_signal_back():
    watch = LanguageWatch(act=False, cooldown_seconds=120.0)
    first = feed(watch, TRANSLIT * 8)
    assert [s for s in first if s.kind == STUCK]
    fired_at = watch.last_signal_at
    # Still stuck, and still inside the cooldown.
    again = feed(watch, TRANSLIT * 8, start=fired_at + 30.0)
    assert [s for s in again if s.kind == STUCK] == []
    # Past it, and it may fire again.
    later = feed(watch, TRANSLIT * 8, start=fired_at + 200.0)
    assert [s for s in later if s.kind == STUCK]


def test_the_hourly_cap_stops_it_and_says_so():
    """Six reconnects that did not fix it means the remedy is wrong."""
    watch = LanguageWatch(act=False, max_per_hour=3, cooldown_seconds=10.0)
    now = 0.0
    fired = 0
    for _ in range(8):
        signals = feed(watch, TRANSLIT * 8, start=now, step=0.2)
        for signal in signals:
            if signal.kind == STUCK:
                fired += 1
                watch.reconnected(signal.at)
        now += 60.0
    assert fired == 3
    assert watch.capped is True
    assert watch.may_reconnect(now) is False


def test_the_cap_frees_up_after_an_hour():
    watch = LanguageWatch(act=False, max_per_hour=2, cooldown_seconds=10.0)
    watch.reconnected(0.0)
    watch.reconnected(30.0)
    assert watch.may_reconnect(100.0) is False
    assert watch.capped is True
    assert watch.may_reconnect(3700.0) is True
    assert watch.capped is False


def test_the_window_is_emptied_after_a_reconnect():
    """The words in it came from the stream that was just replaced.

    Left in, they would fire again on the next response and spend the
    cooldown proving the same thing twice.
    """
    watch = LanguageWatch(act=False)
    feed(watch, TRANSLIT * 8)
    assert watch.score(100.0)[1] > 0
    watch.reconnected(100.0)
    assert watch.score(100.0) == (0.0, 0)
    assert watch.stuck_since is None
    assert watch.reconnects == [100.0]


def test_peak_share_is_kept_for_the_status_line():
    """The one number that says "nearly fired" rather than "fired"."""
    watch = LanguageWatch(act=False, stuck_share=0.9)
    feed(watch, TRANSLIT * 8)
    assert watch.peak_share > 0.5
    assert watch.status(100.0)["lang_peak_share"] == pytest.approx(
        watch.peak_share, abs=1e-4)


def test_status_is_counts_and_flags_only():
    watch = LanguageWatch(act=False)
    feed(watch, TRANSLIT * 8)
    status = watch.status(100.0)
    assert set(status) == {
        "lang_reconnects", "lang_last_reconnect", "lang_capped",
        "lang_share", "lang_peak_share", "lang_window_words",
        "lang_latin_share", "lang_drifted",
    }
    blob = repr(status)
    for word in TRANSLIT + ARABIC:
        assert word not in blob


# ---------------------------------------------------------------------------
# Drift: measured, logged, never acted on
# ---------------------------------------------------------------------------


def test_drift_fires_when_latin_stops_appearing():
    """Latin script gone from Arabic speech, after a talk that had it.

    This is the earlier, softer version of the same fault, and the reason it
    only writes a line is that on 2026-10-31 it held for 17 minutes of a
    session that recovered on its own twice.
    """
    watch = LanguageWatch(act=False)
    # First: Arabic with English terms in Latin, well above the 10% floor.
    words = []
    for index in range(60):
        words.append(TERMS[index % len(TERMS)])
        words.append(ARABIC[index % len(ARABIC)])
    signals = feed(watch, words, start=0.0, step=0.5)
    assert watch.ever_latin is True
    assert [s for s in signals if s.kind == DRIFT] == []
    # Then: the same Arabic, no Latin at all, for longer than the floor.
    drifted = feed(watch, ARABIC * 30, start=100.0, step=1.0)
    drifted += feed(watch, ARABIC * 30, start=400.0, step=1.0)
    assert [s for s in drifted if s.kind == DRIFT]


def test_drift_needs_latin_to_have_been_there_first():
    """A talk that never code-switched in Latin has not drifted; it is just
    a talk in Arabic, which is the ordinary case."""
    watch = LanguageWatch(act=False)
    signals = feed(watch, ARABIC * 40, step=1.0)
    signals += feed(watch, ARABIC * 40, start=400.0, step=1.0)
    assert [s for s in signals if s.kind == DRIFT] == []
    assert watch.ever_latin is False


def test_drift_is_reported_once_per_session():
    watch = LanguageWatch(act=False)
    feed(watch, (TERMS * 20), step=0.5)
    signals = []
    for start in (100.0, 400.0, 700.0, 1000.0):
        signals.extend(feed(watch, ARABIC * 30, start=start, step=1.0))
    assert len([s for s in signals if s.kind == DRIFT]) == 1


def test_english_arriving_restarts_the_drift_clock():
    """An English-labelled original is the engine plainly still hearing
    English, whatever the Arabic looks like.

    It restarts the countdown rather than cancelling it: if English stops
    arriving again, the next three minutes of Latin-free Arabic still earn a
    line. That is why session 1436 reported drift once despite two English
    speakers getting through - the gap before the first of them was six
    minutes.
    """
    watch = LanguageWatch(act=False)
    # 0 s to 50 s: Arabic with English terms in Latin script. The Latin tail
    # is itself a 180 s window, so the countdown cannot start until these
    # have aged out of it - which is the point of the gaps below.
    feed(watch, TERMS * 10, start=0.0, step=0.5)
    assert watch.ever_latin is True
    # 300 s: Latin-free Arabic, which starts the countdown.
    feed(watch, ARABIC * 5, start=300.0, step=0.5)
    first = watch._zero_since
    assert first is not None and first >= 300.0
    # 350 s: English arrives, labelled English. The countdown restarts from
    # here rather than being cancelled.
    feed(watch, ["still", "here"], start=350.0, step=0.5, lang="en")
    assert watch._zero_since is not None
    assert watch._zero_since > first
    # 400 s: only 50 s since the restart, so nothing is reported yet.
    assert [s for s in feed(watch, ARABIC * 5, start=400.0, step=0.5)
            if s.kind == DRIFT] == []
    # 600 s: past the floor with no English in between, so it is.
    assert [s for s in feed(watch, ARABIC * 20, start=600.0, step=0.5)
            if s.kind == DRIFT]


def test_a_latin_only_word_counts_toward_the_latin_share():
    """The bug that made drift unmeasurable.

    An English term inside Arabic speech is one word of pure Latin with no
    Arabic letters in it. Dropped before the Latin share was taken - which
    it was at first - it measured a session running at 15-37% as 1-4%.
    """
    watch = LanguageWatch(act=False)
    feed(watch, ["embeddings", "مرحبا"], step=0.1)
    assert watch.latin_share() > 0.5
    # ...and it is still not a word the stuck score judges, because there is
    # no Arabic in it to be the wrong script.
    assert watch.words_seen == 1


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_scoring_does_not_cost_the_projector_its_frame_rate():
    """The window is scored incrementally, not re-folded every response.

    This was a real regression. `score` originally folded every word in the
    window on each response - up to 120 Unicode normalizations per response -
    and a 20,000-response soak replayed at 400x spent enough of the event
    loop on it that no caption reached the page inside 30 seconds and two
    browser tests timed out.

    The budget is the one already written down for a broadcast message,
    0.1-1.1 ms. This asserts an order of magnitude under it, because the
    watcher is on top of that work rather than instead of it.
    """
    import time

    watch = LanguageWatch(act=False)
    words = [(TRANSLIT if index % 3 else ARABIC)[index % 5]
             for index in range(4000)]
    started = time.perf_counter()
    now = 0.0
    for word in words:
        watch.note_response(response(word), now)
        now += 0.5
    each = (time.perf_counter() - started) / len(words)
    assert each < 100e-6, f"{each * 1e6:.1f} us per response"
    # ...and the window really did fill, or the measurement is of nothing.
    assert watch.score(now)[1] == watch.window_words


def test_the_running_hit_count_stays_in_step_with_the_window():
    """A counter maintained in some branches and recomputed in others
    drifts, and a share that drifts is a threshold that means nothing."""
    watch = LanguageWatch(act=False, window_words=20)
    now = 0.0
    for index in range(500):
        word = TRANSLIT[index % len(TRANSLIT)] if index % 2 else ARABIC[0]
        watch.note_response(response(word), now)
        now += 0.5
        share, total = watch.score(now)
        # The authority: fold every word in the window from scratch.
        honest = sum(1 for _, held, _ in watch._window
                     if is_function_word(held))
        assert watch._hits == honest, f"drifted at response {index}"
        if total:
            assert share == pytest.approx(honest / total)


def test_the_counters_survive_a_reconnect():
    watch = LanguageWatch(act=False)
    feed(watch, TRANSLIT * 8)
    assert watch._hits > 0
    watch.reconnected(100.0)
    assert watch._hits == 0
    assert watch.latin_share() == 0.0
    feed(watch, ARABIC * 10, start=100.0)
    assert watch._hits == 0
