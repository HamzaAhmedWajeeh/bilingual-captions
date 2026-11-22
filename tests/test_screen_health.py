"""Why the 2026-10-24 report cannot come from the auto stream, and the
machinery that makes the next one answerable in a glance.

The projector was described as showing the English **original** half an hour
into a talk, with the words appearing grey and then turning white. That
detail is what this file is built around: grey text is the `.partial` span,
and only `createRenderer` makes one. The translation-only view does not have
that span, cannot have a partial to put in it, and never receives a non-final
token - so the thing the room saw was not the auto stream, whatever else it
was.

Four groups of tests:

1. **Grey is impossible in auto.** On the shipped files, and in the state.
2. **Nothing switches the view on its own.** The only writer of the setting
   is `set_screen`, the only callers are the operator's route and startup,
   and a session starting or stopping leaves it alone.
3. **Every page says what it is rendering**, read off its own DOM, and the
   operator page turns red when a page disagrees with the setting - including
   on the one check that is true of every theory about that night: *is the
   room reading the language the server sent?*
4. **Reload without Stop.** Every page can be told to start over while the
   session keeps running.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import screen_audit  # noqa: E402
from app.broadcast import (  # noqa: E402
    BEHIND_LIMIT,
    HEALTH_STALE,
    RELOAD,
    WRONG_LANGUAGE_SECONDS,
    BroadcastHub,
)
from app.captions import AUTO, CaptionState  # noqa: E402
from app.settings import WEB_DIR  # noqa: E402

APP_JS = (WEB_DIR / "app.js").read_text(encoding="utf-8")
AUTO_JS = (WEB_DIR / "auto.js").read_text(encoding="utf-8")
SCREEN_JS = (WEB_DIR / "screen.js").read_text(encoding="utf-8")
MAIN_PY = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
STYLE = (WEB_DIR / "style.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Grey text cannot happen in the translation-only view
# ---------------------------------------------------------------------------


def test_only_the_plain_renderer_makes_a_partial_span():
    """Grey text is `.partial`, and `auto.js` never creates one.

    The operator saw words arrive grey and turn white, which is the
    partial-then-final rendering of *original* speech. `createRenderer` in
    app.js builds a `<span class="partial">` per paragraph; `auto.js` writes
    one text node and no spans at all. So the pane the room was looking at
    was not the translation-only one - which is what makes this a test and
    not an argument.
    """
    assert 'className = \'partial\'' in APP_JS, (
        "the plain renderer no longer marks its partial span; this test is "
        "measuring the wrong thing")
    assert "partial" in STYLE, "nothing styles the partial span"
    # The reveal in auto.js reads `para.partial` - it would render one if the
    # server ever sent it - but it never puts it in a span, so it can never
    # be grey.
    assert "class" not in AUTO_JS.split("function paint(")[1].split("}")[0]
    assert "createElement('span')" not in AUTO_JS, (
        "auto.js has grown a span; if it is for a partial, the projector can "
        "now render grey text and the 2026-10-24 reasoning no longer holds")


def test_the_partial_style_is_the_grey_the_operator_described():
    """`.partial` is dimmed text, and nothing in the auto pane shares it."""
    rule = re.search(r"\.partial\s*\{([^}]*)\}", STYLE).group(1)
    assert "opacity" in rule, "the partial span is no longer dimmed"
    # `.auto-para` itself must not be dimmed, or live captions would look
    # like unconfirmed ones.
    auto_rule = re.search(r"\.auto-para\s*\{([^}]*)\}", STYLE).group(1)
    assert "opacity" not in auto_rule


def test_the_auto_stream_can_never_hold_a_partial():
    """`_apply_partials` covers the two output languages and not `auto`.

    Measured over both 2026-10-24 sessions as well (see
    `tests/test_long_session.py`): 94,841 non-final tokens between them, and
    not one reached `auto` - in the state, in a snapshot, or in any diff.
    """
    state = CaptionState()
    state.add_response({"tokens": [
        {"text": "hello", "is_final": False, "language": "en",
         "translation_status": "original"},
        {"text": "مرحبا", "is_final": False, "language": "ar",
         "translation_status": "original"},
    ]})
    assert state.streams["en"][-1].partial, "the test fed nothing non-final"
    assert all(not p.partial for p in state.streams[AUTO])
    snapshot = state.snapshot()
    assert all(not p.get("partial") for p in snapshot["streams"][AUTO])
    assert all(not p.get("partial")
               for p in state.diff_since(0)["streams"][AUTO])


def test_a_non_final_translation_token_is_still_kept_out_of_auto():
    """The engine has never sent one; `auto` is built from finals anyway.

    Not a hypothetical worth leaving open: if Soniox ever does send a
    non-final translation, the projector must not start showing text that
    can still be withdrawn.
    """
    state = CaptionState()
    state.add_response({"tokens": [
        {"text": "مرحبا", "is_final": True, "language": "ar",
         "translation_status": "original"},
        {"text": " Hello", "is_final": False, "language": "en",
         "source_language": "ar", "translation_status": "translation"},
    ]})
    assert not state.render(AUTO).strip(), (
        "a non-final translation token reached the projector's stream")


def test_no_page_may_be_translated_by_the_browser():
    """Every page says `translate="no"`, three ways.

    Chrome and Edge translate a page whose detected language is not the
    reader's, and an operator who does not read Arabic may well have "always
    translate Arabic" turned on. On a caption screen that is a caption fault
    wearing a caption fault's clothes: once a stretch of Arabic fills the
    pane the browser decides the page is Arabic and translates it **back
    into English in place**, so the room reads the words it has just heard -
    starting minutes after the talk changed language, which is the shape of
    the 2026-10-24 report exactly.

    `translate="no"` on the root element is what stops it; the meta is the
    older form; `notranslate` on the caption containers is belt and braces
    for the part that must never be touched.
    """
    for name in ("screen.html", "index.html", "admin.html"):
        markup = (WEB_DIR / name).read_text(encoding="utf-8")
        root = re.search(r"<html\b[^>]*>", markup).group(0)
        assert 'translate="no"' in root, (
            f"{name}: the root element does not refuse translation")
        assert 'name="google" content="notranslate"' in markup, (
            f"{name}: the notranslate meta is missing")
        # ...and `lang` stays, because it is what makes the type and the
        # direction right - and it is also what the browser sniffs, which is
        # why the refusal has to be explicit rather than implied.
        assert 'lang="en"' in root

    for name, ids in (("screen.html", ("screen-auto", "screen-en",
                                       "screen-ar")),
                      ("index.html", ("stream-auto", "stream-en",
                                      "stream-ar"))):
        markup = (WEB_DIR / name).read_text(encoding="utf-8")
        for ident in ids:
            tag = re.search(rf'<section[^>]*id="{ident}"[^>]*>', markup)
            assert tag, f"{name}: no {ident} container"
            assert "notranslate" in tag.group(0), (
                f"{name}: {ident} is not marked notranslate")


def test_a_page_reports_when_the_browser_is_translating_it():
    """The markup says no; this is the check that it held.

    Chrome marks the root element `translated-ltr` or `translated-rtl` and
    wraps what it rewrote in `<font>` - neither of which these pages produce
    themselves, so either is proof. Read from the source here and exercised
    in the browser in `tests/test_screen_health_browser.py`.
    """
    body = SCREEN_JS.split("function translated()")[1].split("\n  }")[0]
    assert "translated-(ltr|rtl)" in body, (
        "the page no longer looks for the class the browser adds")
    assert "getElementsByTagName('font')" in body, (
        "the injected <font> elements are not checked; the class alone is "
        "one signal and this is the other")
    assert "translated: translated()" in SCREEN_JS
    # The audience page too: it is the one that defaults to a single
    # language, so it is the one most likely to be translated.
    assert "translated-(ltr|rtl)" in APP_JS


def test_a_translated_page_is_reported_and_counted():
    hub = BroadcastHub(CaptionState())
    hub.set_screen(view="auto")
    normal, translating = _viewer(hub), _viewer(hub)
    hub.note_health(normal, {"rendered": "auto", "seq": 0})
    hub.note_health(translating, {"rendered": "auto", "seq": 0,
                                  "translated": True})

    screens = hub.screens()["screens"]
    assert screens["translated"] == 1
    assert screens["rows"][0]["translated"] is False
    assert screens["rows"][1]["translated"] is True
    # It is not a "wrong view": the page is rendering exactly the pane it
    # was told to, and the words in it are being rewritten underneath.
    assert screens["wrong_view"] == 0


def test_the_operator_is_told_to_turn_auto_translate_off():
    """The one fault whose remedy is not in this app, so it says so."""
    admin_js = (WEB_DIR / "admin.js").read_text(encoding="utf-8")
    body = admin_js.split("function renderScreenHealth(")[1].split("\n  /*")[0]
    assert "screens.translated" in body
    assert "auto-translate" in body, (
        "the warning does not name the thing the operator has to change")


# ---------------------------------------------------------------------------
# 2. Nothing switches the view on its own
# ---------------------------------------------------------------------------


def test_the_view_has_exactly_one_writer():
    """`set_screen` is the only thing that assigns the main-screen view.

    Read from the source, because the point is to catch a *second* writer
    being added later - which is the shape of the fault that was looked for
    on 2026-10-24 and not found.
    """
    source = (ROOT / "app" / "broadcast.py").read_text(encoding="utf-8")
    assignments = re.findall(r'self\.screen\[[^\]]+\]\s*=', source)
    assert len(assignments) == 1, (
        f"the screen settings are assigned in {len(assignments)} places; "
        f"the audit line and the single-writer argument both assume one")
    # ...and it is inside `apply`, the helper that records the change.
    body = source.split("def set_screen(")[1].split("\n    def ")[0]
    assert "self.screen[name] = value" in body


def test_only_the_operator_route_and_startup_can_set_the_view():
    """Every caller of `set_screen` in the server, named.

    `adopt_screen` at startup and the `/admin/screen` route. If anything
    automatic ever gains the ability to move the projector, this fails and
    the next person reads the 2026-10-24 write-up before shipping it.
    """
    assert MAIN_PY.count("set_screen(") == 1, (
        "the server calls set_screen in more than one place; the view must "
        "move only through the operator's route")
    route = MAIN_PY.split("async def admin_screen(")[1].split("\n@app.")[0]
    assert "set_screen(" in route, (
        "the one call is no longer the operator's route - something else in "
        "the server can now move the projector")
    assert MAIN_PY.count("adopt_screen(") == 1


def test_starting_and_stopping_a_session_leaves_the_view_alone():
    """Stop + Start was the operator's remedy, and it changes no setting.

    Which is why it could not have "fixed" a wrong view: rebinding the hub
    to a new `CaptionState` resets the captions and nothing else. What it
    does reset is every page's paragraph nodes, through the empty snapshot.
    """
    state = CaptionState()
    hub = BroadcastHub(state)
    hub.set_screen(view="auto", size="xl")
    before = dict(hub.screen)
    for _ in range(3):
        hub.rebind(CaptionState())
    assert dict(hub.screen) == before, "a session change moved a setting"
    assert hub.public_screen()["view"] == "auto"


def test_an_unknown_view_is_ignored_rather_than_applied():
    """A typo must leave the projector where it is."""
    hub = BroadcastHub(CaptionState())
    hub.set_screen(view="auto")
    assert not hub.set_screen(view="english")
    assert hub.screen["view"] == "auto"
    assert not hub.set_screen(view=None)
    assert hub.screen["view"] == "auto"


def test_the_page_only_takes_a_view_it_knows():
    """`screen.js` must fall back to what it has, never to a default.

    A message with no `screen.view` - or a key it does not recognise - has
    to leave the projector showing what it is showing.
    """
    assert "VIEWS.indexOf(next) === -1) { next = view; }" in SCREEN_JS
    assert "forcedView || screen.view" in SCREEN_JS


# ---------------------------------------------------------------------------
# 3. Every page says what it is rendering
# ---------------------------------------------------------------------------


def test_the_page_reports_what_it_measured_not_what_it_meant():
    """`rendered` is read off the DOM.

    The whole value of the report is that it cannot agree with a bug: if the
    English pane is visible when the setting says Auto, the page says `en`
    because it looked.
    """
    body = SCREEN_JS.split("function renderedView()")[1].split("\n  }")[0]
    assert "panes[key].hidden" in body, (
        "the rendered view is no longer measured from the panes themselves")
    health = SCREEN_JS.split("function health()")[1].split("\n  }")[0]
    assert "rendered: renderedView()" in health
    assert "forcedView !== null" in health, "a pinned page must say so"
    assert "querySelectorAll('.partial').length" in health, (
        "the grey count is what names the 2026-10-24 symptom; without it the "
        "report cannot tell that story")


def test_a_health_report_is_validated_by_shape():
    """It arrives from a browser, so it is untrusted input."""
    hub = BroadcastHub(CaptionState())
    viewer = _viewer(hub)

    hub.note_health(viewer, {"rendered": "nonsense", "seq": 1})
    assert viewer.health is None, "an unknown view was accepted"
    hub.note_health(viewer, {"rendered": "auto", "seq": -1})
    assert viewer.health is None, "a negative sequence number was accepted"
    hub.note_health(viewer, {"rendered": "auto", "seq": True})
    assert viewer.health is None, "a boolean was accepted as a seq"

    hub.note_health(viewer, {
        "rendered": "auto", "seq": 4, "pinned": "yes", "visible": 1,
        "grey": 2, "secret": "some caption text", "seq_extra": 9})
    assert viewer.health == {"rendered": "auto", "pinned": True, "seq": 4,
                             "visible": True, "grey": 2, "page": "screen",
                             "translated": False, "script": "none",
                             "para": -1}, (
        "the health record carries something other than the nine fields")


def test_the_operator_sees_a_page_that_is_showing_the_wrong_stream():
    """The line that was missing. Counts and enums, no text."""
    state = CaptionState()
    hub = BroadcastHub(state)
    hub.set_screen(view="auto")
    good, bad = _viewer(hub), _viewer(hub)

    hub.note_health(good, {"rendered": "auto", "seq": 0, "visible": True})
    hub.note_health(bad, {"rendered": "en", "seq": 0, "pinned": True})

    screens = hub.screens()["screens"]
    assert screens["count"] == 2
    assert screens["reporting"] == 2
    assert screens["want"] == "auto"
    assert screens["wrong_view"] == 1
    assert screens["rows"][1]["pinned"] is True
    assert screens["rows"][0]["wrong_view"] is False
    assert screens["rows"][1]["wrong_view"] is True
    blob = json.dumps(screens)
    assert "some caption text" not in blob
    for word in ("final", "partial", "text"):
        assert f'"{word}"' not in blob, (
            f"the screens summary carries a {word} field; it must be counts "
            f"and enums only")


def test_the_audience_page_is_listed_but_never_called_wrong():
    """It follows its own reader by design, and it is worth seeing anyway.

    **It is also the best fit for the 2026-10-24 report**: the audience page
    draws with the plain renderer, so its text arrives grey and turns white;
    it defaults to English on an English-locale browser with no Auto default
    at all; and it ignores the main-screen setting completely. One open
    during a session now shows up on the operator page instead of being
    indistinguishable from a projector in the viewer count.
    """
    hub = BroadcastHub(CaptionState())
    hub.set_screen(view="auto")
    screen, phone = _viewer(hub), _viewer(hub)
    hub.note_health(screen, {"rendered": "auto", "seq": 0, "page": "screen"})
    hub.note_health(phone, {"rendered": "en", "seq": 0, "page": "audience"})

    screens = hub.screens()["screens"]
    assert screens["count"] == 2
    assert screens["audience"] == 1
    assert screens["wrong_view"] == 0, (
        "the audience page was flagged for following its own reader")
    assert screens["rows"][1]["page"] == "audience"
    assert screens["rows"][1]["rendered"] == "en"


def test_a_page_that_does_not_say_what_it_is_counts_as_a_projector():
    """An older page, and the side to err on: that is the one being driven."""
    hub = BroadcastHub(CaptionState())
    hub.set_screen(view="auto")
    viewer = _viewer(hub)
    hub.note_health(viewer, {"rendered": "en", "seq": 0})
    screens = hub.screens()["screens"]
    assert screens["audience"] == 0
    assert screens["wrong_view"] == 1


def test_the_audience_page_reports_its_own_view():
    """Read from `app.js`: the handler is wired and says which page it is."""
    body = APP_JS.split("var feed = createFeed({")[1].split(
        "onParagraph:")[0]
    assert "onHealth" in body, "the audience page does not report at all"
    assert "page: 'audience'" in body
    assert "rendered: view" in body, (
        "the audience page must report the view it is on, not a constant")


def test_a_page_that_stops_keeping_up_is_called_behind():
    """Measured in seconds, not in sequence numbers.

    One paragraph of lag and ten minutes of it are the same number of seqs
    behind, so the clock is what matters: a page is behind when it has not
    been level with the server for `BEHIND_LIMIT`.
    """
    now = [1000.0]
    state = CaptionState()
    hub = BroadcastHub(state, clock=lambda: now[0])
    viewer = _viewer(hub)

    state.add_response(_said("hello"))
    hub.note_health(viewer, {"rendered": "auto", "seq": state.seq})
    assert hub.screens()["screens"]["behind"] == 0

    # The talk moves on and the page stops applying anything.
    for _ in range(5):
        state.add_response(_said("more"))
    now[0] += BEHIND_LIMIT - 1
    hub.note_health(viewer, {"rendered": "auto", "seq": 1})
    assert hub.screens()["screens"]["behind"] == 0, "called behind too early"
    now[0] += 2
    hub.note_health(viewer, {"rendered": "auto", "seq": 1})
    screens = hub.screens()["screens"]
    assert screens["behind"] == 1
    assert screens["rows"][0]["behind_ms"] >= int(BEHIND_LIMIT * 1000)

    # ...and it catches up.
    now[0] += 1
    hub.note_health(viewer, {"rendered": "auto", "seq": state.seq})
    assert hub.screens()["screens"]["behind"] == 0


def test_a_page_that_stops_reporting_is_called_stale():
    now = [500.0]
    hub = BroadcastHub(CaptionState(), clock=lambda: now[0])
    viewer = _viewer(hub)
    hub.note_health(viewer, {"rendered": "auto", "seq": 0})
    assert hub.screens()["screens"]["stale"] == 0
    now[0] += HEALTH_STALE + 1
    assert hub.screens()["screens"]["stale"] == 1


def test_a_screen_that_has_never_reported_is_counted_but_not_judged():
    """An older page, or one that has just connected."""
    hub = BroadcastHub(CaptionState())
    _viewer(hub)
    screens = hub.screens()["screens"]
    assert screens["count"] == 1
    assert screens["reporting"] == 0
    assert screens["wrong_view"] == 0, (
        "a page that has not said anything yet must not be reported as wrong")
    assert screens["rows"] == [{"reporting": False}]


def test_the_operator_page_turns_red_only_on_real_trouble():
    """Read from `admin.js`: the class is the whole alarm."""
    admin_js = (WEB_DIR / "admin.js").read_text(encoding="utf-8")
    body = admin_js.split("function renderScreenHealth(")[1].split(
        "\n  /*")[0]
    assert "health bad" in body and "health ok" in body
    for signal in ("wrong_view", "behind", "grey", "stale"):
        assert signal in body, f"{signal} does not reach the operator's line"
    assert "up to date" in body


# ---------------------------------------------------------------------------
# 3b. Is the room reading the language the server sent?
# ---------------------------------------------------------------------------


def test_the_server_knows_which_language_it_last_put_on_the_screen():
    """`newest_auto_language` is what the page's report is measured against.

    The newest `auto` paragraph that has text in it - an empty one has just
    been opened and is not on the wall yet, so reading its language would
    make every paragraph boundary a disagreement.
    """
    state = CaptionState()
    hub = BroadcastHub(state)
    assert hub.newest_auto_language() == "", "an empty talk has no language"

    _translate(state, "مرحبا بالجميع في هذه الجلسة", into="ar", source="en")
    assert hub.newest_auto_language() == "ar"
    # Long enough to be a turn rather than a blip: a paragraph's direction is
    # decided by the dominant language, and `MIN_SWITCH_CHARS` is what keeps
    # an embedded term from flipping it.
    _translate(state, "hello everyone and welcome to the session",
               into="en", source="ar")
    assert hub.newest_auto_language() == "en"


def test_a_page_showing_the_wrong_language_is_flagged_but_not_at_once():
    """The fault of 2026-10-24, in the one form that is true of every theory.

    Translation trails its speech, so a page is legitimately behind on
    language at every turn in the talk; the longest honest disagreement
    measured over both sessions that night was 12.1 s. So the watch waits
    `WRONG_LANGUAGE_SECONDS` before it says anything, and the moment it does
    it asks the page what it looks like from the inside.
    """
    now = [1000.0]
    state = CaptionState()
    hub = BroadcastHub(state, clock=lambda: now[0])
    events = []
    hub.on_event = lambda kind, fields: events.append((kind, fields))
    viewer = _viewer(hub)

    # The server's newest auto paragraph is Arabic; the page says the text on
    # screen is Latin. Nothing else is wrong: not pinned, not translated,
    # the right pane, no grey.
    _translate(state, "مرحبا بالجميع", into="ar", source="en")
    report = {"rendered": "auto", "seq": state.seq, "visible": True,
              "script": "en", "para": 1}
    hub.note_health(viewer, report)
    assert hub.screens()["screens"]["wrong_language"] == 0, (
        "a disagreement was called a fault immediately")
    assert not viewer.want_diagnose

    now[0] += WRONG_LANGUAGE_SECONDS - 1
    hub.note_health(viewer, report)
    assert hub.screens()["screens"]["wrong_language"] == 0, (
        "called wrong inside the time translation itself takes")

    now[0] += 2
    hub.note_health(viewer, report)
    screens = hub.screens()["screens"]
    assert screens["wrong_language"] == 1
    assert screens["rows"][0]["script"] == "en"
    assert viewer.want_diagnose, "the page was never asked to describe itself"

    kinds = [kind for kind, _ in events]
    assert "wrong_language" in kinds
    fault = [fields for kind, fields in events if kind == "wrong_language"][0]
    assert fault["showing"] == "en" and fault["expected"] == "ar"
    assert fault["seconds"] >= WRONG_LANGUAGE_SECONDS
    assert fault["recent"], "the reports leading up to the fault were not kept"
    # Counts, flags and enums - never the words that were on the screen.
    assert "مرحبا" not in json.dumps(events, ensure_ascii=False)


def test_the_watch_resets_when_the_page_catches_up():
    """A language switch is a disagreement that ends by itself."""
    now = [1000.0]
    state = CaptionState()
    hub = BroadcastHub(state, clock=lambda: now[0])
    events = []
    hub.on_event = lambda kind, fields: events.append((kind, fields))
    viewer = _viewer(hub)

    _translate(state, "مرحبا", into="ar", source="en")
    hub.note_health(viewer, {"rendered": "auto", "seq": state.seq,
                             "script": "en"})
    now[0] += WRONG_LANGUAGE_SECONDS + 1
    hub.note_health(viewer, {"rendered": "auto", "seq": state.seq,
                             "script": "en"})
    assert hub.screens()["screens"]["wrong_language"] == 1

    # The page is showing Arabic after all - the translation arrived.
    now[0] += 1
    hub.note_health(viewer, {"rendered": "auto", "seq": state.seq,
                             "script": "ar"})
    assert hub.screens()["screens"]["wrong_language"] == 0
    assert viewer.wrong_language_since is None
    assert "language_recovered" in [kind for kind, _ in events]


def test_a_page_that_is_not_showing_auto_is_not_judged_on_language():
    """The audience page follows its own reader, by design.

    `/` defaults to English and ignores the operator entirely, so measuring
    it against the projector's stream would make every phone in the room a
    fault. It is listed; it is not warned about.
    """
    now = [1000.0]
    state = CaptionState()
    hub = BroadcastHub(state, clock=lambda: now[0])
    viewer = _viewer(hub)

    _translate(state, "مرحبا", into="ar", source="en")
    for _ in range(4):
        hub.note_health(viewer, {"rendered": "en", "seq": state.seq,
                                 "page": "audience", "script": "en"})
        now[0] += WRONG_LANGUAGE_SECONDS
    assert hub.screens()["screens"]["wrong_language"] == 0


def test_a_page_with_nothing_on_it_disagrees_with_nobody():
    """`none` is not a language, and neither is a server that has said
    nothing yet. Both sides have to have an opinion before there can be a
    disagreement, or a talk's first seconds would be a fault."""
    now = [1000.0]
    state = CaptionState()
    hub = BroadcastHub(state, clock=lambda: now[0])
    viewer = _viewer(hub)

    # Nothing said yet, and the page is blank.
    for _ in range(4):
        hub.note_health(viewer, {"rendered": "auto", "seq": 0,
                                 "script": "none"})
        now[0] += WRONG_LANGUAGE_SECONDS
    assert hub.screens()["screens"]["wrong_language"] == 0

    # The talk starts, but this page has not drawn anything yet.
    _translate(state, "مرحبا", into="ar", source="en")
    for _ in range(4):
        hub.note_health(viewer, {"rendered": "auto", "seq": state.seq,
                                 "script": "none"})
        now[0] += WRONG_LANGUAGE_SECONDS
    assert hub.screens()["screens"]["wrong_language"] == 0, (
        "a page with no text on it was called wrong")


def test_the_diagnostic_is_logged_whole_and_still_carries_no_text():
    """The page's answer to `diagnose`.

    A dump on purpose: the reason that night cost a day is that nobody had
    guessed in advance which fact would matter. Every field is an attribute,
    a count, a flag or the page's own address, and anything long is clipped
    rather than dropped - a user agent is evidence, a paragraph is not.
    """
    hub = BroadcastHub(CaptionState())
    events = []
    hub.on_event = lambda kind, fields: events.append((kind, fields))
    viewer = _viewer(hub)

    hub.note_diagnostic(viewer, {
        "path": "/screen", "lang": "en", "translate": "no",
        "classes": "screen " + "x" * 500, "fonts": 0, "panes": "auto",
        "view": "auto", "pinned": False, "seq": 12, "visibility": "visible",
        "pixel_ratio": 2, "agent": "Mozilla/5.0", "script": "ar",
        "para": 7, "chars": 80,
        "secret": "every word the speaker just said",
    })
    assert len(events) == 1
    kind, fields = events[0]
    assert kind == "diagnostic"
    assert fields["path"] == "/screen" and fields["translate"] == "no"
    assert "secret" not in fields, (
        "an unknown field was copied out of a browser's message")
    assert len(fields["classes"]) <= 300, "a long field was not clipped"


# ---------------------------------------------------------------------------
# 4. Reload without Stop
# ---------------------------------------------------------------------------


async def test_a_reload_reaches_every_page_and_moves_no_caption():
    hub = BroadcastHub(CaptionState())
    first, second = _viewer(hub), _viewer(hub)
    first.last_seq = second.last_seq = hub.state.seq

    assert hub.reload_screens() == 2
    await hub.flush()
    for viewer in (first, second):
        assert viewer._pending is not None
        advance_to, text = viewer._pending
        assert text == RELOAD
        assert advance_to == 0, (
            "a reload advanced a viewer's sequence number; it carries no "
            "caption state and must not stand in for one")


async def test_a_reload_that_could_not_be_sent_is_offered_again():
    """Same rule as the snapshot flag: cleared on the way out, not before."""
    hub = BroadcastHub(CaptionState())
    viewer = _viewer(hub)
    viewer._busy = True                      # a write already in flight
    hub.reload_screens()
    await hub.flush()
    assert viewer.want_reload, "the reload was dropped"
    viewer._busy = False
    viewer._pending = None
    await hub.flush()
    assert viewer._pending is not None
    assert viewer._pending[1] == RELOAD


def test_the_page_reloads_itself_and_stops_reconnecting_first():
    """A page that reloads must not race its own watchdog."""
    branch = APP_JS.split("if (message.type === 'reload')")[1].split("}")[0]
    assert "stopped = true" in branch, (
        "the page reconnects while it is reloading")
    assert "location.reload()" in branch


def test_the_reload_route_does_not_touch_the_session():
    """It is the alternative to Stop + Start, so it must not stop anything."""
    body = MAIN_PY.split("async def admin_screens_reload(")[1].split(
        "\n@app.")[0]
    assert "reload_screens()" in body
    # The code, not the prose: the docstring explains what Stop + Start does
    # and would match every word below.
    code = "\n".join(line for line in body.splitlines()
                     if line.strip().startswith("session.")
                     or line.strip().startswith("return")
                     or line.strip().startswith("require_admin"))
    for forbidden in ("stop(", "start_replay", "start_live", "rebind",
                      "finalize"):
        assert forbidden not in code, (
            f"the reload route calls {forbidden}; the whole point is that "
            f"the talk and the transcript carry on")


# ---------------------------------------------------------------------------
# The settings audit log
# ---------------------------------------------------------------------------


def test_every_change_writes_one_line_naming_its_source(tmp_path):
    hub = BroadcastHub(CaptionState())
    written: list[tuple] = []
    hub.on_audit = lambda changes, source: written.append((changes, source))

    hub.set_screen(view="en", size="xl")
    assert len(written) == 1
    changes, source = written[0]
    assert source == "admin"
    assert sorted(field for field, _old, _new in changes) == ["size", "view"]
    assert ("view", "auto", "en") in changes

    screen_audit.record(changes, source, log_dir=tmp_path)
    lines = screen_audit.audit_path(tmp_path).read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert any("view" in line and "auto -> en" in line for line in lines)
    assert all("admin" in line for line in lines)


def test_a_setting_that_did_not_change_writes_nothing(tmp_path):
    hub = BroadcastHub(CaptionState())
    written: list[tuple] = []
    hub.on_audit = lambda changes, source: written.append((changes, source))
    hub.set_screen(view="auto", size="l")       # the defaults
    assert not written, "an unchanged setting produced an audit line"
    assert screen_audit.record([], "admin", log_dir=tmp_path) == 0
    assert not screen_audit.audit_path(tmp_path).exists()


def test_the_invite_link_is_never_written_to_the_audit_log(tmp_path):
    """The one setting that is a secret. the project notes: never a log.

    An obviously invented link, as everywhere: a committed invite code is
    upper case and digits only, where a real one is 22 mixed-case
    characters.
    """
    link = "https://chat.whatsapp.com/INVITEDCODE1234567890"
    hub = BroadcastHub(CaptionState())
    recorded: list[tuple] = []
    hub.on_audit = lambda changes, source: recorded.append((changes, source))
    hub.set_screen(invite=link)
    screen_audit.record(recorded[0][0], recorded[0][1], log_dir=tmp_path)

    body = screen_audit.audit_path(tmp_path).read_text(encoding="utf-8")
    assert "INVITEDCODE" not in body, "the invite link reached the audit log"
    assert "whatsapp" not in body.lower()
    assert "invite" in body, "the change itself was not recorded"
    assert "<hidden:" in body, "the fingerprint is missing"
    # A different link has a different fingerprint, so a reader can tell that
    # it changed without being handed either value.
    first = re.search(r"<hidden:(\w+)>", body.split("->")[1]).group(1)
    assert len(first) == 8
    assert first not in link


def test_a_test_run_never_points_at_the_operators_logs():
    """Every module that writes to `logs/` must be pointed somewhere else.

    The autouse fixture in `conftest.py` cannot cover this on its own:
    `app/main.py` builds its `CaptionSession` at module scope, so the
    main-screen settings are adopted - and the startup audit lines written -
    while a test module is being *imported*, before any fixture runs. That is
    how the first version of this appended to the operator's real
    `logs/screen-settings.log` while the size guard sat there seeing nothing,
    its "before" snapshot having been taken after the damage.

    `CAPTIONS_LOG_DIR` is set at the top of `conftest.py` for that reason, and
    this is the assertion that it stayed set.
    """
    import app.main as server
    from app import screen_audit as audit
    from app import settings as settings_module
    from app import usage as usage_module

    real = (ROOT / "logs").resolve()
    for module in (settings_module, usage_module, audit, server):
        where = Path(getattr(module, "LOG_DIR")).resolve()
        assert where != real, (
            f"{module.__name__}.LOG_DIR points at the operator's own logs/; "
            f"a run of this suite would append to their spend ledger and "
            f"their screen audit")


def test_the_audit_log_survives_an_unwritable_directory(tmp_path):
    """A settings change is the wrong moment to fail on a log write."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    assert screen_audit.record([("view", "auto", "en")], "admin",
                               log_dir=blocker) == 0


def test_a_multi_line_subline_stays_one_audit_line(tmp_path):
    """The one setting that may carry a newline must not break the format."""
    hub = BroadcastHub(CaptionState())
    recorded: list[tuple] = []
    hub.on_audit = lambda changes, source: recorded.append((changes, source))
    hub.set_screen(subline="first line\nsecond line")
    screen_audit.record(recorded[0][0], recorded[0][1], log_dir=tmp_path)
    lines = screen_audit.audit_path(tmp_path).read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, "a typed break split the audit line in two"
    assert "\\n" in lines[0]


def test_startup_is_audited_before_the_save_hook_is_attached():
    """Read from `main.py`: the order is the whole behaviour.

    The audit hook goes on first, so the log opens with what the server came
    up holding. The save hook goes on last, because adopting settings is
    *reading* the file and a save there would rewrite the operator's file on
    every start.
    """
    body = MAIN_PY.split("def __init__(self)")[1].split("\n    def ")[0]
    audit = body.index("on_audit = self._on_screen_audit")
    adopt = body.index("adopt_screen(saved)")
    save = body.index("on_change = save_screen")
    assert audit < adopt < save, (
        "the hooks are attached in the wrong order: startup changes are "
        "either unaudited or written straight back to disk")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Socket:
    """The least a `Viewer` needs. Nothing is read back from it here."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        pass


def _viewer(hub: BroadcastHub):
    from app.broadcast import Viewer

    viewer = Viewer(_Socket(), ident=len(hub.viewers) + 1)
    hub.viewers.append(viewer)
    return viewer


def _said(word: str) -> dict:
    return {"tokens": [{"text": f" {word}", "is_final": True,
                        "language": "en",
                        "translation_status": "original"}]}


def _translate(state: CaptionState, text: str, *, into: str,
               source: str) -> None:
    """One translated token, which is what `auto` is made of.

    No `start_ms`/`end_ms`: a translation token carries neither, which is
    the engine agreeing that translated text is not time in the room.
    """
    state.add_response({"tokens": [
        {"text": f" {text}", "is_final": True, "language": into,
         "source_language": source, "translation_status": "translation"}]})
