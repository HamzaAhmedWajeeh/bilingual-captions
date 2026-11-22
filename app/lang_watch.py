"""Has the engine stopped hearing English?

**Why this exists.** On 2026-10-31, 18 minutes into one talk and 12 into the
next, Soniox stopped labelling any speech English. Every original token from
then on carried `language: "ar"`, every translation carried
`source_language: "ar"`, and an English speaker five minutes later was
transcribed *phonetically in Arabic script* and translated back into English.
The captions were correct for the labels they were given: the room read the
English speaker's own words, arriving as a translation, a few seconds late.
Nothing in the pipeline was wrong and nothing in the pipeline could tell.

The server's existing wrong-language check cannot see this. That check asks
"is the page showing the language the server sent?", and here the page was
showing exactly what the server sent - both sides agreed, and both were
wrong about the room. The only place the fault is visible is in the *shape*
of the Arabic: English function words written in Arabic letters.

**Two signals, deliberately unequal.**

*Stuck* is the one that acts. English function words do not appear in Arabic
speech - a bilingual speaker reaches for an English *term* (`embeddings`,
`RAG`), never for `of`, `is`, `from` or `because`. So a run of Arabic whose
function words are transliterated English is not Arabic at all. Measured as
a share of a rolling window of Arabic-labelled original words, which is why
this module reassembles words before it counts anything: Soniox tokens are
sub-word fragments, and counting fragments measures nothing. (That mistake
was made first, and it put single Arabic letters at the top of the table.)

*Drift* only writes a line. The Latin share of Arabic speech falling to zero
and staying there - after the same talk ran at 12-29% - is the earlier,
softer version of the same fault: English *terms* stopped surviving as
Latin script. It is real and it is measured, but on 2026-10-31 it also held
for 17 minutes of a session that recovered on its own twice, so acting on it
would have ended two talks that did not need ending.

**Pure, and clockless like `captions.py`.** No network, no async, no I/O, no
`time.time()`: every entry point takes `now` in seconds from the caller. A
live run passes wall time and the calibration tool passes the response log's
own `recv_offset_ms`, which is what lets a threshold be checked against six
real evenings instead of argued about.
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from functools import lru_cache
from dataclasses import dataclass, field
from typing import Iterable

#: What `CaptionState` calls a translated token. Kept here rather than
#: imported so this module depends on nothing.
TRANSLATION = "translation"
END_TOKEN = "<end>"

#: **Letters, not the Arabic block.** The block runs from U+0600 and holds
#: the comma, the semicolon, the question mark, the full stop, the
#: Arabic-Indic digits and every diacritic alongside the letters. Matching
#: "anything in the Arabic block" made `اوف،` a different word from `اوف`,
#: and made a word of pure Latin followed by an Arabic comma look as though
#: it had Arabic letters in it.
#:
#: So the class is spelled out: the letter ranges, and nothing else.
ARABIC_LETTER_CLASS = (
    r"ؠ-ي"      # alef through ya, the ordinary letters
    r"ٮٯ"       # dotless beh and qaf
    r"ٱ-ۓ"      # alef wasla through yeh barree
    r"ە"             # ae
    r"ۥۦ"       # small waw and yeh
    r"ۮۯ"       # dal and reh with inverted V
    r"ۺ-ۿ"      # sheen through heh with inverted V
    r"ݐ-ݿ"      # Arabic Supplement
)
ARABIC_LETTERS = re.compile(f"[{ARABIC_LETTER_CLASS}]")
LATIN_LETTERS = re.compile(r"[A-Za-z]")
WHITESPACE = re.compile(r"(\s+)")

#: Everything that is not an Arabic letter, stripped before a lookup - so
#: punctuation, digits and diacritics never change a verdict.
NON_LETTERS = re.compile(f"[^{ARABIC_LETTER_CLASS}]")

#: Characters that spell the same sound more than one way. Soniox writes
#: both `اوف` and `أوف` for *of* in the same session, so the lexicon holds
#: one form and everything is folded onto it.
FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي",
    "ة": "ه",
    "ؤ": "و",
    "ـ": "",           # tatweel, a stretch mark rather than a letter
})

#: A form shorter than this is a fragment or a particle, not a word worth
#: judging. Two-letter forms are where Arabic and transliterated English
#: collide hardest - `هي`, `شي`, `مي`, `نو`, `كان` are all ordinary Arabic.
#:
#: Measured rather than assumed. Dropping the floor to 2 would let *this*,
#: *are* and *then* count, and across all 19 logs it raised the peak inside
#: a locked window from 0.250-0.379 to 0.267-0.425 - but it raised the worst
#: healthy window from 0.067 to 0.075 too, taking the margin from 1.9x to
#: 1.7x and the catch time from 10 s to 9 s. More signal, less room, so the
#: floor stays at 3 and those three forms are out of the lexicon rather
#: than sitting in it unreachable.
MIN_LETTERS = 3

#: English function words as Soniox writes them in Arabic script, folded by
#: `FOLD` and at least `MIN_LETTERS` long.
#:
#: Three rules decided what is in here, and they are the whole reason this
#: list can be matched strictly:
#:
#: 1. **Function words and the commonest verbs only.** An English *noun* in
#:    Arabic script is ordinary borrowing - a speaker saying `کود` for *code*
#:    has said an Arabic sentence. An English *preposition* in Arabic script
#:    means the sentence was English.
#: 2. **Nothing that is also an Arabic word.** `كان` (was), `ذا` (this),
#:    `اول` (first), `هي` (she), `شي` (thing), `بين` (between) and `ذات`
#:    (self) all turn up transliterating English too, and every one of them
#:    is common in real Arabic. They are out, and the margin below is what
#:    they bought.
#: 3. **Observed, then extended.** Every form here was either read out of a
#:    real response log or is a spelling variant of one. Nothing was invented
#:    from an English word list, because a form Soniox never writes adds
#:    nothing and cannot be checked.
FUNCTION_WORDS = frozenset({
    # it / it's / this / these / those / they / them / your / my / I'm
    "ايت", "اتس", "ذيس", "ديس", "ذيز", "ذوز", "ديز",
    "ذاي", "ذيم", "يور", "ماي", "ايم", "ميني",
    # is / are / was / were / have / has / had / does / will / would / should
    "ايز", "واز", "وير", "ويز", "هاف", "هاز", "هاد", "داز",
    "ويل", "وود", "شود",
    # of / from / for / with / and / but / because / about / into / without
    "اوف", "فروم", "فور", "ويذ", "ويث", "اند", "بات", "بوت",
    "بيكوز", "بيكوس", "اباوت", "ابوت", "اباود", "انتو", "ويثاوت",
    "بيفور", "افتر", "بتوين", "اوفر",
    # what / when / where / how / why / which / who
    "وات", "ووت", "هاو", "واي", "ويتش", "هوو",
    # now / then / there / here / just / very / really / also / only
    "ناو", "ذير", "دير", "هير", "جست", "جاست", "فيري",
    "ريلي", "ريالي", "اولسو", "الصو", "اونلي", "ايفر", "اجين",
    "ستيل", "نيفر", "ميبي", "اكتشوالي", "اكتشلي", "بيسكلي",
    # not / don't / doesn't / didn't
    "نوت", "نات", "دونت", "دوسنت", "ديدنت",
    # some / every / each / both / more / most / less / much / few
    "سام", "ايفري", "ايتش", "بوث", "مور", "موست", "ليس", "ماتش",
    "فيو",
    # something / everything / anything / nothing / thing / things
    "سمثين", "سمثينق", "سامثينق", "ايفريثينق", "انيثينق", "نثينق",
    "ثين", "ثينق", "ثينج", "ثينز", "ثينجز", "ثينغ",
    # think / know / make / take / give / need / use / learn / build
    "ثينك", "سينك", "نوو", "ميك", "ميكس", "تيك", "جيف",
    "نيد", "يوز", "ليرن", "بيلد", "ليتس", "مينتين", "كمين",
    # right / like / good / better / best / easy / easier
    "رايت", "لايك", "جود", "قود", "بيتر", "بيست", "ايزي", "ايزير",
    # people / time / day / part / back / out / work
    "بيبل", "تايم", "داي", "بارت", "باك", "اوت", "وورك", "ورك",
    # thank / sorry / okay
    "ثانك", "سوري", "اوكي", "اوكيه",
})

#: Forms that transliterate an English function word *and* spell an ordinary
#: Arabic one. They are kept out of `FUNCTION_WORDS` on purpose, and listed
#: here so that a later addition cannot quietly put one back.
#:
#: Every one of these was found by the calibration tool, not by reasoning:
#: they were the **only** four forms the first lexicon matched more than
#: twice across 8,928 Arabic words of healthy speech, and between them they
#: were 20 of its 43 false matches. The rest of that tail is singletons.
ARABIC_LOOKALIKES = frozenset({
    "كانت",   # MSA "she was" - 12 of the 43
    "وانت",   # "wa-anta", *and you* - a Gulf-dialect commonplace
    "وين",    # Saudi dialect for *where*, which this room speaks
    "ماني",   # dialect "I am not"
})


def is_translation(token: dict) -> bool:
    """`CaptionState._is_translation`, duplicated on purpose.

    This module takes nothing from `captions.py`, so that the detector can be
    driven straight off a response log by a tool that never builds a
    `CaptionState`. The pass-through case is the same one documented there:
    a token marked `translation` whose `source_language` is its own target is
    an original. It happened again twice on 2026-10-31, once per session,
    21 and 16 tokens in a single response each time.
    """
    if token.get("translation_status") != TRANSLATION:
        return False
    source = token.get("source_language")
    return source is None or source != token.get("language")


@lru_cache(maxsize=8192)
def fold(word: str) -> str:
    """One spelling for one sound, letters only.

    Memoised because a talk reuses a few hundred word forms over tens of
    thousands of tokens, and this is the one function on the hot path that
    normalizes Unicode.

    The order matters and got this wrong once. Decomposing first turns `أ`
    into an alef plus a *combining hamza*, and U+0654 sits inside the Arabic
    block - so `NON_LETTERS` keeps it and `أوف` folded to four characters
    while `اوف` folded to three. They never matched.

    So: fold the precomposed forms first, then decompose and drop combining
    marks (which is what removes fatha, damma, sukun and a hamza that
    arrived decomposed), then keep only Arabic letters.
    """
    plain = word.translate(FOLD)
    plain = "".join(
        char for char in unicodedata.normalize("NFKD", plain)
        if not unicodedata.combining(char)
    ).translate(FOLD)
    return NON_LETTERS.sub("", plain)


def is_function_word(word: str) -> bool:
    """Is this an English function word written in Arabic letters?"""
    folded = fold(word)
    return len(folded) >= MIN_LETTERS and folded in FUNCTION_WORDS


# -- what the watcher reports -------------------------------------------------

STUCK = "LANG_STUCK"
DRIFT = "LANG_DRIFT"


@dataclass(frozen=True)
class Signal:
    """One thing the watcher noticed, with the numbers behind it.

    Carries no words - a share, a count and a duration. The event log this
    ends up in is the one file that is allowed to hold neither speech nor
    session details, and a diagnostic that quoted the window would break
    that for the sake of a detail nobody could act on.
    """

    kind: str
    at: float
    share: float
    words: int
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "at": round(self.at, 1),
            "share": round(self.share, 4),
            "words": self.words,
            **self.detail,
        }


# -- the watcher --------------------------------------------------------------

#: The rolling window the stuck score is measured over. Words rather than
#: seconds because speech density swings by a factor of ten: 90 s of a dense
#: Arabic stretch is 600 words and 90 s of a sparse one is 40.
WINDOW_WORDS = 120
#: ...and a time bound as well, so a window cannot be stitched together out
#: of two stretches ten minutes apart.
WINDOW_SECONDS = 180.0
#: Below this many words in the window there is nothing to judge.
MIN_WINDOW_WORDS = 40

#: Calibrated by `tools/lang_calibrate.py` against every response log on
#: disk: 19 sessions over six evenings, 2026-10-13 to 2026-10-31.
#:
#:   highest score in any healthy window      0.067
#:     ...in a session that never locked      0.025
#:   score at which it fired                  0.125 - 0.233
#:   peak score inside a locked window        0.250 - 0.379
#:   caught the lock within                   10 s - 206 s
#:
#: 0.12 is 1.9x the worst healthy window and about 5x the worst window of a
#: session that stayed healthy. The sweep is flat: anything from 0.08 to
#: 0.20 catches both locks and fires in nothing else, and 0.06 starts firing
#: on the one session that is labelled healthy but did in fact lock. Sitting
#: at 0.12 rather than 0.08 costs 7 s on the one real English speaker we can
#: measure and buys most of the room between the two.
STUCK_SHARE = 0.12

#: A Latin share this high earlier in the session is what makes a later zero
#: mean something: it says this room *was* code-switching in Latin script.
DRIFT_LATIN_SHARE = 0.10
#: ...and this long at zero is what makes it more than a quiet patch.
DRIFT_ZERO_SECONDS = 180.0
#: Letters of Arabic-labelled original text needed in the tail before a zero
#: Latin share is worth believing. A quiet patch is not a drift.
DRIFT_MIN_LETTERS = 150

#: No second reconnect inside this, whatever the score says. A reconnect
#: costs the room nothing visible but it is still a new Soniox stream, and a
#: detector that fired every 20 seconds would be worse than the fault.
COOLDOWN_SECONDS = 120.0
#: ...and no more than this many in an hour. At the cap the watcher stops
#: asking and `/admin` goes red: six reconnects that did not fix it means the
#: remedy is wrong, and the operator should be told rather than have the
#: engine restarted for the rest of the talk.
MAX_PER_HOUR = 6
HOUR_SECONDS = 3600.0


class LanguageWatch:
    """Watches the original tokens and says when English stopped arriving.

    Fed every response; it picks out the final original tokens itself. The
    caller supplies `now`, acts on whatever `note_response` returns, and
    calls `reconnected()` when it has done something about it.
    """

    def __init__(
        self,
        *,
        window_words: int = WINDOW_WORDS,
        window_seconds: float = WINDOW_SECONDS,
        min_window_words: int = MIN_WINDOW_WORDS,
        stuck_share: float = STUCK_SHARE,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        max_per_hour: int = MAX_PER_HOUR,
        act: bool = True,
    ) -> None:
        self.window_words = window_words
        self.window_seconds = window_seconds
        self.min_window_words = min_window_words
        self.stuck_share = stuck_share
        self.cooldown_seconds = cooldown_seconds
        self.max_per_hour = max_per_hour
        #: False in replay: the signal is logged and nothing is reconnected.
        #: A replay is driven by `FakeSonioxClient`, which has no connection
        #: to re-open, and the point of replaying a locked session is to see
        #: the detector fire, not to watch it restart a file.
        self.act = act

        #: Arabic-labelled original words, newest last: (when, word, hit).
        #: `hit` is the lexicon verdict, taken once when the word arrives.
        self._window: deque[tuple[float, str, bool]] = deque()
        #: How many of `_window` are lexicon hits. Kept in step with the
        #: deque rather than recomputed; see `_trim`.
        self._hits = 0
        #: Half-built words, one per language, because the fragments of the
        #: two languages interleave inside a single response.
        self._pending: dict[str, str] = {}
        self._pending_at: dict[str, float] = {}

        #: Latin and Arabic letters seen in Arabic-labelled originals, for
        #: the drift signal. A session total and a rolling tail.
        self._latin_tail: deque[tuple[float, int, int]] = deque()
        self._tail_latin = 0
        self._tail_arabic = 0
        self.ever_latin = False
        self._zero_since: float | None = None
        self._drift_reported = False

        self.reconnects: list[float] = []
        self.last_signal_at: float | None = None
        self.stuck_since: float | None = None
        self.capped = False
        #: Highest share this session has reached, for the status line. It is
        #: the one number that says "nearly fired" rather than "fired".
        self.peak_share = 0.0
        self.words_seen = 0

    # -- feeding -------------------------------------------------------------

    def note_response(self, response: dict, now: float) -> list[Signal]:
        """One Soniox response. Returns whatever it made the watcher notice."""
        for token in response.get("tokens", ()) or ():
            if not token.get("is_final"):
                continue
            text = token.get("text") or ""
            if not text or text.strip() == END_TOKEN:
                continue
            if is_translation(token):
                continue
            lang = token.get("language")
            if lang not in ("ar", "en"):
                continue
            self._feed(lang, text, now)
        return self.poll(now)

    def _feed(self, lang: str, text: str, now: float) -> None:
        """Reassemble words out of sub-word fragments.

        Soniox sends ` compan` and then `y`; a word exists only once the
        whitespace after it arrives. Each language keeps its own half-built
        word, and only Arabic-labelled originals are scored - the English
        ones are fed so that the *absence* of English is measurable, which is
        what `words_seen` and the drift signal are for.
        """
        for piece in WHITESPACE.split(text):
            if not piece:
                continue
            if piece.isspace():
                self._flush(lang, now)
                continue
            if not self._pending.get(lang):
                self._pending_at[lang] = now
            self._pending[lang] = self._pending.get(lang, "") + piece

    def _flush(self, lang: str, now: float) -> None:
        word = self._pending.pop(lang, "")
        at = self._pending_at.pop(lang, now)
        if not word:
            return
        if lang == "en":
            # An English-labelled original is the thing whose disappearance
            # this module is about. Nothing to score, but it clears the
            # drift clock: the engine is plainly still hearing English.
            self._zero_since = None
            return
        arabic = len(ARABIC_LETTERS.findall(word))
        latin = len(LATIN_LETTERS.findall(word))
        if not arabic and not latin:
            return
        # Every Arabic-labelled original word feeds the Latin tail, including
        # the ones with no Arabic letters in them at all. Those are the whole
        # point of the drift signal: an English term that survived as Latin
        # script inside Arabic speech (`embeddings`, `RAG`) is one word of
        # pure Latin, and dropping it - which this did at first - measured
        # the Latin share of Arabic speech as 1-4% in a session that was
        # really running at 15-37%.
        self._latin_tail.append((at, latin, arabic))
        self._tail_latin += latin
        self._tail_arabic += arabic
        if not arabic:
            return
        self.words_seen += 1
        # **The verdict is taken once, here.** `score` used to fold every
        # word in the window on every response, which is 120 Unicode
        # normalizations per response - a 20,000-response soak spent seconds
        # of event loop on it and starved the paced replay. Now a word is
        # judged when it arrives and the window carries a running count.
        hit = is_function_word(word)
        self._window.append((at, word, hit))
        self._hits += hit

    # -- scoring -------------------------------------------------------------

    def _drop_oldest(self) -> None:
        self._hits -= self._window.popleft()[2]

    def _trim(self, now: float) -> None:
        """Age the two windows, keeping the running counters in step.

        Every pop goes through `_drop_oldest` and the two `_tail_*` lines
        below for one reason: a counter that is maintained in some branches
        and recomputed in others drifts, and a share that drifts is a
        threshold that means nothing.
        """
        cutoff = now - self.window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._drop_oldest()
        while len(self._window) > self.window_words:
            self._drop_oldest()
        tail_cutoff = now - DRIFT_ZERO_SECONDS
        while self._latin_tail and self._latin_tail[0][0] < tail_cutoff:
            _, latin, arabic = self._latin_tail.popleft()
            self._tail_latin -= latin
            self._tail_arabic -= arabic

    def score(self, now: float) -> tuple[float, int]:
        """(share of the window that is transliterated English, window size).

        O(the words that just aged out), not O(the window): the per-word
        verdict was taken in `_flush`.
        """
        self._trim(now)
        total = len(self._window)
        if not total:
            return 0.0, 0
        return self._hits / total, total

    def latin_share(self) -> float:
        """Latin letters as a share of letters in the Arabic-original tail."""
        total = self._tail_latin + self._tail_arabic
        return self._tail_latin / total if total else 0.0

    # -- deciding ------------------------------------------------------------

    def poll(self, now: float) -> list[Signal]:
        """Anything worth reporting as of `now`."""
        out: list[Signal] = []
        share, words = self.score(now)
        if words >= self.min_window_words:
            self.peak_share = max(self.peak_share, share)
        out.extend(self._check_drift(now))
        out.extend(self._check_stuck(now, share, words))
        return out

    def _check_stuck(self, now: float, share: float,
                     words: int) -> list[Signal]:
        if words < self.min_window_words or share < self.stuck_share:
            self.stuck_since = None
            return []
        if self.stuck_since is None:
            self.stuck_since = now
        if not self.may_reconnect(now):
            return []
        signal = Signal(
            kind=STUCK, at=now, share=share, words=words,
            detail={
                "threshold": self.stuck_share,
                "window_seconds": round(self.window_seconds, 1),
                "latin_share": round(self.latin_share(), 4),
                "latin_letters": self._tail_latin,
                "reconnects": len(self.reconnects),
                "acting": self.act,
                "ar_words_seen": self.words_seen,
            },
        )
        self.last_signal_at = now
        return [signal]

    def _check_drift(self, now: float) -> list[Signal]:
        """Latin script gone from Arabic speech. Logged, never acted on."""
        share = self.latin_share()
        letters = self._tail_latin + self._tail_arabic
        if share >= DRIFT_LATIN_SHARE:
            self.ever_latin = True
            self._zero_since = None
            self._drift_reported = False
            return []
        if not self.ever_latin or share > 0 or letters < DRIFT_MIN_LETTERS:
            return []
        if self._zero_since is None:
            self._zero_since = now
            return []
        if self._drift_reported:
            return []
        if now - self._zero_since < DRIFT_ZERO_SECONDS:
            return []
        self._drift_reported = True
        return [Signal(
            kind=DRIFT, at=now, share=0.0, words=len(self._window),
            detail={
                "zero_seconds": round(now - self._zero_since, 1),
                "letters": letters,
                "ar_words_seen": self.words_seen,
            },
        )]

    # -- the caller's half ---------------------------------------------------

    def may_reconnect(self, now: float) -> bool:
        """Is a reconnect allowed right now?

        False while the cooldown is running, and False for good once the
        hourly cap is reached - at which point `capped` is what `/admin`
        reads. In replay (`act=False`) the signal is still produced, so the
        calibration tool and the tests see every firing; it is the caller
        that does nothing with it.
        """
        self._forget_old_reconnects(now)
        if len(self.reconnects) >= self.max_per_hour:
            self.capped = True
            return False
        if self.last_signal_at is not None:
            if now - self.last_signal_at < self.cooldown_seconds:
                return False
        if self.reconnects and now - self.reconnects[-1] < self.cooldown_seconds:
            return False
        return True

    def _forget_old_reconnects(self, now: float) -> None:
        cutoff = now - HOUR_SECONDS
        self.reconnects = [at for at in self.reconnects if at >= cutoff]
        if len(self.reconnects) < self.max_per_hour:
            self.capped = False

    def reconnected(self, now: float) -> None:
        """The caller acted. Start again from nothing.

        The window is emptied rather than aged out: the words in it were
        produced by the stream that has just been replaced, and leaving them
        in would fire again on the next response and spend the cooldown
        proving the same thing twice.
        """
        self.reconnects.append(now)
        self._window.clear()
        self._hits = 0
        self._latin_tail.clear()
        self._tail_latin = 0
        self._tail_arabic = 0
        self._pending.clear()
        self._pending_at.clear()
        self.stuck_since = None
        self._zero_since = None
        self._drift_reported = False
        self.last_signal_at = now

    # -- for `/admin` --------------------------------------------------------

    def status(self, now: float | None = None) -> dict:
        """Counts and flags. No words, like everything else on that page."""
        share, words = (self.score(now) if now is not None
                        else (0.0, len(self._window)))
        return {
            "lang_reconnects": len(self.reconnects),
            "lang_last_reconnect": (round(self.reconnects[-1], 1)
                                    if self.reconnects else None),
            "lang_capped": self.capped,
            "lang_share": round(share, 4),
            "lang_peak_share": round(self.peak_share, 4),
            "lang_window_words": words,
            "lang_latin_share": round(self.latin_share(), 4),
            "lang_drifted": self._drift_reported,
        }


def score_words(words: Iterable[str]) -> float:
    """The stuck share of a bare list of words. For tests and tools."""
    words = list(words)
    if not words:
        return 0.0
    return sum(1 for word in words if is_function_word(word)) / len(words)
