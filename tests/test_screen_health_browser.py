"""The projector, in a real browser, over a long session - and the two
things that were missing on 2026-10-24: a page that says what it is showing,
and a way to fix one without ending the talk.

Each test is named after a question that could not be answered that night:

- *What is that screen actually rendering?* A bare page says `auto`, a page
  opened with `?lang=en` says `en` **and** that a URL pinned it, and the
  operator page turns red for the second one.
- *Is the room reading the language the server sent?* A running page is made
  to hold Arabic on screen while the server's newest `auto` paragraph is
  English; the operator's line goes red and the event log gets the fault
  with the page's own account of itself beside it.
- *Is something rewriting the page?* A browser translating it says so.
- *Can I fix it without stopping the talk?* Refresh screens reloads every
  page while the session keeps running.
- *Could the Auto view ever show unconfirmed text?* Over a three-hour
  synthetic talk - 2,000+ paragraphs, past the 200-paragraph trim, through
  two forced network drops - the translation-only pane never renders a
  `.partial` span and never disagrees with its own language.

The synthetic talk is written to the server's own log directory, which
`CAPTIONS_LOG_DIR` has pointed at a temporary folder, so nothing lands in the
repository. It is the same generator the offline soak uses
(`tests/test_long_session.py`), where "never an original" is checked against
labelled words; here the question is what the *page* does with it.

    uv run --with playwright pytest tests/test_screen_health_browser.py
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import re  # noqa: E402

from app.broadcast import WRONG_LANGUAGE_SECONDS  # noqa: E402
from app.captions import MAX_PARAGRAPHS  # noqa: E402
from app.settings import WEB_DIR  # noqa: E402

#: What the page keeps, read from the page rather than repeated here.
KEEP_PARAGRAPHS = int(re.search(
    r"var KEEP_PARAGRAPHS = (\d+)",
    (WEB_DIR / "app.js").read_text(encoding="utf-8")).group(1))

sys.path.insert(0, str(ROOT / "tests"))

from test_long_session import Talk  # noqa: E402
from captures import needs_captures  # noqa: E402

PORT = 8116
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "screen-health-test-token"

#: Named `live-*.jsonl` because that is what a log directory exposes - the
#: same rule that keeps `usage.jsonl` from being offered as a session.
SOAK_CAPTURE = "live-synthetic-soak.jsonl"

#: The server's log directory for this module, set by the fixture. The
#: wrong-language test reads the event log back off disk, because "it is
#: written down" is half of what is being tested.
LOGS_DIR: Path | None = None

#: What the page is rendering, read off the page itself.
HEALTH = "() => window.CaptionScreen.health()"

#: Everything the auto pane is showing, with the two things that must never
#: be true of it: an unconfirmed (`.partial`) span, and a paragraph whose
#: text is not in the language it claims.
AUDIT = r"""
() => {
  function script(text) {
    var ar = 0, la = 0;
    for (var i = 0; i < text.length; i += 1) {
      var c = text.charCodeAt(i);
      if (c >= 0x0600 && c <= 0x06FF) { ar += 1; }
      else if ((c >= 65 && c <= 90) || (c >= 97 && c <= 122)) { la += 1; }
    }
    if (ar === 0 && la === 0) { return 'none'; }
    return ar >= la ? 'ar' : 'en';
  }
  var pane = document.getElementById('screen-auto');
  var paras = pane.querySelectorAll('.auto-para');
  var mismatched = 0, originals = 0, shown = 0;
  for (var i = 0; i < paras.length; i += 1) {
    var text = paras[i].textContent;
    if (!text.trim()) { continue; }
    shown += 1;
    var lang = paras[i].getAttribute('lang') || '';
    if (script(text) !== 'none' && script(text) !== lang) { mismatched += 1; }
    // Every word of the synthetic talk says what it is.
    if (text.indexOf('orig') !== -1) { originals += 1; }
  }
  return {
    paragraphs: paras.length,
    shown: shown,
    grey: pane.querySelectorAll('.partial').length,
    mismatched: mismatched,
    originals: originals,
    en: document.getElementById('screen-en').children.length,
    rendered: window.CaptionScreen.health().rendered,
    translated: window.CaptionScreen.health().translated,
    loads: Number(window.name || 0)
  };
}
"""


try:
    import playwright.sync_api  # noqa: F401

    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False

needs_browser = pytest.mark.skipif(
    not HAVE_PLAYWRIGHT,
    reason="Playwright is not a project dependency; "
           "run with `uv run --with playwright`",
)


def write_soak(path: Path) -> int:
    """A three-hour synthetic talk, as a capture the server can replay."""
    talk = Talk(seed=19)
    responses = talk.script()
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"recv_at": "2026-10-29T00:00:00+00:00",
                             "meta": {"source": "synthetic soak"}}) + "\n")
        # Paced replay reads `recv_offset_ms`, so the timings are the talk's
        # own: three hours of offsets, played at `speed`.
        step = (talk.clock or 1) / max(1, len(responses))
        for index, response in enumerate(responses):
            fh.write(json.dumps({
                "recv_at": "2026-10-29T00:00:00+00:00",
                "recv_offset_ms": int(index * step),
                "sent_ms": int(index * step),
                "response": response,
            }, ensure_ascii=False) + "\n")
    return len(responses)


@pytest.fixture(scope="module")
def live_server():
    """A server whose state *and* logs go to a temporary directory.

    `CAPTIONS_LOG_DIR` is what lets the synthetic capture live outside the
    repository: the server can replay a capture from its own log directory,
    which is the same rule that makes a real session's log replayable.
    """
    global LOGS_DIR
    state = tempfile.mkdtemp(prefix="captions-health-")
    logs = Path(state) / "logs"
    LOGS_DIR = logs
    logs.mkdir(parents=True, exist_ok=True)
    responses = write_soak(logs / SOAK_CAPTURE)
    assert responses > 20_000, "the synthetic talk is too short to be a soak"

    env = dict(os.environ, ADMIN_TOKEN=TOKEN, PYTHONIOENCODING="utf-8",
               CAPTIONS_STATE_DIR=state, CAPTIONS_LOG_DIR=str(logs),
               CAPTIONS_TRANSCRIPT_DIR=str(Path(state) / "transcripts"))
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(BASE + "/healthz", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("the test server did not start")
        yield BASE
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(state, ignore_errors=True)


def admin(base: str, path: str, **params) -> dict:
    params["token"] = TOKEN
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def status(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/admin/status?token={TOKEN}",
                                timeout=10) as response:
        return json.loads(response.read())


@contextlib.contextmanager
def projector(browser, base, query=""):
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = context.new_page()
    try:
        # A load counter that survives a reload, which is how the refresh
        # test sees that the page really did start over.
        page.add_init_script(
            "window.name = String(Number(window.name || 0) + 1);")
        page.goto(base + "/screen" + query, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        yield context, page
    finally:
        context.close()


def wait_for_screens(base, predicate, timeout=20.0):
    """Poll the operator's status until the screens summary satisfies us."""
    deadline = time.monotonic() + timeout
    seen = None
    while time.monotonic() < deadline:
        seen = status(base).get("screens") or {}
        if predicate(seen):
            return seen
        time.sleep(0.4)
    return seen


@needs_browser
def test_a_bare_screen_reports_the_view_the_operator_chose(live_server):
    """The good case, and the shape of the answer that was missing."""
    from playwright.sync_api import sync_playwright

    admin(live_server, "/admin/screen", view="auto", size="xl")
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                page.evaluate("() => window.CaptionScreen.report()")
                health = page.evaluate(HEALTH)
                assert health["rendered"] == "auto"
                assert health["pinned"] is False
                assert health["grey"] == 0

                screens = wait_for_screens(
                    live_server, lambda s: s.get("reporting") == 1)
                assert screens["count"] == 1
                assert screens["want"] == "auto"
                assert screens["wrong_view"] == 0
                assert screens["behind"] == 0
                assert screens["grey"] == 0
                assert screens["rows"][0]["rendered"] == "auto"
                assert screens["rows"][0]["pinned"] is False
        finally:
            browser.close()


@needs_browser
def test_a_pinned_screen_is_reported_as_not_obeying_the_operator(live_server):
    """The 2026-10-24 question, answerable in one glance.

    `?lang=en` pins a page against the operator and keeps winning - that is
    deliberate, it is the testing hatch - and until now nothing said so. A
    pinned page renders the English stream, which carries the original when
    English is spoken, and that is exactly what was described on the night.
    """
    from playwright.sync_api import sync_playwright

    admin(live_server, "/admin/screen", view="auto")
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server, "?lang=en") as (_, page):
                page.evaluate("() => window.CaptionScreen.report()")
                health = page.evaluate(HEALTH)
                assert health["rendered"] == "en", (
                    "a pinned page is not reporting the pane it is showing")
                assert health["pinned"] is True

                screens = wait_for_screens(
                    live_server, lambda s: s.get("wrong_view") == 1)
                assert screens["wrong_view"] == 1, (
                    "the operator is not told that a screen is showing "
                    "something other than what they chose")
                assert screens["rows"][0]["pinned"] is True
        finally:
            browser.close()


@needs_browser
def test_an_audience_page_shows_up_as_what_it_is(live_server):
    """The page that best fits the 2026-10-24 report, now visible.

    `/` renders with the plain renderer - grey partials turning white - and
    defaults to English on an English-locale browser, with no Auto default at
    all. It ignores the main-screen setting entirely, so it looks right while
    Arabic is spoken and shows the original the moment the talk switches to
    English. One open during a session used to be indistinguishable from a
    projector in the viewer count; the operator page now lists it separately
    and does not call it wrong, because following its own reader is what it
    is for.
    """
    from playwright.sync_api import sync_playwright

    admin(live_server, "/admin/screen", view="auto")
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            context = browser.new_context(
                viewport={"width": 390, "height": 844}, locale="en-GB")
            page = context.new_page()
            page.goto(live_server + "/", wait_until="domcontentloaded")
            page.wait_for_timeout(600)

            screens = wait_for_screens(
                live_server, lambda s: s.get("audience") == 1)
            assert screens["audience"] == 1, (
                "an audience page is open and the operator cannot see it")
            assert screens["wrong_view"] == 0, (
                "the audience page was flagged as disobeying the operator")
            row = [r for r in screens["rows"]
                   if r.get("page") == "audience"][0]
            assert row["rendered"] in ("en", "ar", "both", "auto")
            context.close()
        finally:
            browser.close()


@needs_browser
def test_the_report_notices_the_fault_that_was_described(live_server):
    """Break it on purpose, and check the new machinery sees it.

    Two faults are injected into a running page, because a detector nobody
    has seen fire is a detector nobody should trust:

    1. The English pane is made visible while the setting says Auto - the
       shape of what was reported on 2026-10-24. The page must stop saying
       `auto`, and the operator's line must go red.
    2. An unconfirmed (`.partial`) span is put into the translation-only
       pane - the grey text the operator described. The count must pick it
       up, because that is the field whose whole job is to name it.

    Neither can happen by itself today; that is the point. What this proves
    is that if one ever does, it will be visible in a glance instead of
    taking a day of replaying logs.
    """
    from playwright.sync_api import sync_playwright

    admin(live_server, "/admin/screen", view="auto")
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                page.evaluate("() => window.CaptionScreen.report()")
                healthy = wait_for_screens(
                    live_server, lambda s: s.get("reporting") == 1)
                assert healthy["wrong_view"] == 0 and healthy["grey"] == 0

                page.evaluate(
                    "() => { document.getElementById('screen-en')"
                    ".hidden = false; window.CaptionScreen.report(); }")
                seen = page.evaluate(HEALTH)
                assert seen["rendered"] != "auto", (
                    "the English pane is visible and the page still reports "
                    "Auto; the report is describing its intent, not the room")
                broken = wait_for_screens(
                    live_server, lambda s: s.get("wrong_view") == 1)
                assert broken["wrong_view"] == 1, (
                    "a screen showing a pane the operator did not choose is "
                    "not reaching the operator page")

                page.evaluate(
                    "() => { document.getElementById('screen-en')"
                    ".hidden = true;"
                    " var span = document.createElement('span');"
                    " span.className = 'partial';"
                    " span.textContent = 'x';"
                    " document.getElementById('screen-auto')"
                    ".appendChild(span);"
                    " window.CaptionScreen.report(); }")
                grey = wait_for_screens(live_server,
                                        lambda s: s.get("grey") == 1)
                assert grey["grey"] == 1, (
                    "unconfirmed text in the translation-only pane is not "
                    "reported; that is the exact symptom this exists for")
                assert grey["wrong_view"] == 0, (
                    "the view recovered but is still reported as wrong")
        finally:
            browser.close()


@needs_browser
def test_a_browser_translating_the_page_is_reported(live_server):
    """The candidate that explains 2026-10-24 best, made visible.

    Chrome and Edge translate a page whose detected language is not the
    reader's. An operator who does not read Arabic may have "always translate
    Arabic" on, and on a caption screen that is ruinous in a way that looks
    exactly like a caption bug: once a stretch of Arabic fills the pane the
    browser decides the page is Arabic and translates it **back into English
    in place**, so the room reads the words it just heard - beginning minutes
    after the talk changed language, which is the report's own shape.

    Every page now says `translate="no"`, which is the fix. This is the
    check that the fix held on the machine in the room: the browser marks the
    root element and wraps what it rewrote in `<font>`, so both are watched,
    and the operator's line names the setting to change because nothing in
    this app can change it for them.
    """
    from playwright.sync_api import sync_playwright

    admin(live_server, "/admin/screen", view="auto")
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                page.evaluate("() => window.CaptionScreen.report()")
                healthy = wait_for_screens(
                    live_server, lambda s: s.get("reporting") == 1)
                assert healthy["translated"] == 0

                # What Chrome does to a page it has translated.
                page.evaluate(
                    "() => { document.documentElement.classList"
                    ".add('translated-ltr');"
                    " window.CaptionScreen.report(); }")
                assert page.evaluate(HEALTH)["translated"] is True
                flagged = wait_for_screens(
                    live_server, lambda s: s.get("translated") == 1)
                assert flagged["translated"] == 1, (
                    "a translated page is not reaching the operator page")
                assert flagged["wrong_view"] == 0, (
                    "a translated page was also called a wrong view; it is "
                    "rendering the right pane, with the words rewritten "
                    "underneath")

                # The other signal, on its own: the injected <font>.
                page.evaluate(
                    "() => { document.documentElement.classList"
                    ".remove('translated-ltr');"
                    " var f = document.createElement('font');"
                    " f.textContent = 'x';"
                    " document.getElementById('screen-auto').appendChild(f);"
                    " window.CaptionScreen.report(); }")
                assert page.evaluate(HEALTH)["translated"] is True, (
                    "an injected <font> in the caption pane is not noticed")
        finally:
            browser.close()


@needs_captures
@needs_browser
def test_a_screen_showing_the_wrong_language_is_caught_and_written_down(
        live_server, tmp_path):
    """The check that catches 2026-10-24 **whatever caused it**.

    Everything else added after that night answers a particular theory: is
    the browser translating, is the page pinned, is it the audience page, is
    there grey text. This one asks the only question that is true of all of
    them - *is the room reading the language the server last sent* - by
    comparing the script of the text actually rendered in the pane against
    the language of the server's own newest `auto` paragraph.

    Here the page is made to show Arabic while the server's newest `auto`
    paragraph is English - `run1` is five minutes of Arabic speech, so its
    translated half is English all the way through. That is the fault in its
    purest form: no translation, no pin, no wrong pane, no grey - just the
    wrong words on the wall. The operator's line must go red and the event
    log must carry a `WRONG_LANGUAGE` line with the page's own diagnostic
    beside it.
    """
    from playwright.sync_api import sync_playwright

    admin(live_server, "/admin/screen", view="auto", size="l")
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                # `run1.jsonl` is five minutes of Arabic speech and
                # nothing else, so the translated half - and the server's
                # newest `auto` paragraph - is English for the whole run.
                # That matters: a talk that changes language every few
                # seconds resets the watch at every turn, which is exactly
                # what it is supposed to do.
                admin(live_server, "/admin/start", capture="run1.jsonl",
                      paced="true", speed="4")
                session = status(live_server)["session"]
                assert session, "the session has no name"

                page.wait_for_function(
                    "() => window.CaptionScreen.health().script === 'en'",
                    timeout=60000)

                # A block of Arabic at the end of the pane, re-asserted
                # as the talk goes on. The reveal repaints the paragraphs it
                # owns, so overwriting one is undone within a frame and the
                # page's own five-second report would sample the repaint and
                # reset the watch - which is the watch working. A paragraph
                # the reveal does not own, kept last, is a screen that is
                # genuinely showing the wrong language.
                overwrite = (
                    "() => { var pane = document.getElementById("
                    "'screen-auto');"
                    " var mine = document.getElementById('wrong-language');"
                    " if (!mine) {"
                    "   mine = document.createElement('p');"
                    "   mine.id = 'wrong-language';"
                    "   mine.className = 'auto-para';"
                    "   mine.setAttribute('lang', 'ar');"
                    "   mine.textContent = "
                    "     'نص تجريبي "
                    "للعرض على "
                    "الشاشة '.repeat(8);"
                    " }"
                    " pane.appendChild(mine);"
                    " window.CaptionScreen.report(); }")
                page.evaluate(overwrite)
                assert page.evaluate(HEALTH)["script"] == "ar"

                # It has to hold: a few seconds of disagreement is ordinary
                # at every language switch, which is why the watch waits.
                deadline = time.monotonic() + WRONG_LANGUAGE_SECONDS + 25
                flagged = {}
                while time.monotonic() < deadline:
                    page.evaluate(overwrite)
                    flagged = status(live_server).get("screens") or {}
                    if flagged.get("wrong_language") == 1:
                        break
                    page.wait_for_timeout(500)
                assert flagged["wrong_language"] == 1, (
                    "a screen showing the wrong language is not reaching the "
                    "operator page")
                row = [r for r in flagged["rows"] if r.get("reporting")][0]
                assert row["script"] == "ar"

                page.wait_for_timeout(2000)      # let the diagnostic land
        finally:
            admin(live_server, "/admin/stop")
            browser.close()

    # ...and the evidence is on disk, with the page's own account of itself.
    events = [json.loads(line) for line in
              (LOGS_DIR / f"events-{session}.jsonl")
              .read_text(encoding="utf-8").splitlines() if line.strip()]
    kinds = [row["event"] for row in events]
    assert "WRONG_LANGUAGE" in kinds, (
        f"the fault was not written down; the log holds {sorted(set(kinds))}")
    wrong = [row for row in events if row["event"] == "WRONG_LANGUAGE"][0]
    assert wrong["showing"] == "ar" and wrong["expected"] == "en"
    assert wrong["seconds"] >= WRONG_LANGUAGE_SECONDS
    assert wrong["recent"], "the minute before the fault was not kept"

    diagnostic = [row for row in events if row["event"] == "DIAGNOSTIC"]
    assert diagnostic, "the page was never asked what it looked like"
    snapshot = diagnostic[0]
    assert snapshot["path"].startswith("/screen")
    assert snapshot["translate"] == "no"
    assert "panes" in snapshot and "agent" in snapshot
    # Counts and flags, and not one word of what anyone said.
    blob = json.dumps(events, ensure_ascii=False)
    assert "كلام عربي" not in blob, (
        "the page's rendered text reached the event log")


@needs_browser
def test_refresh_screens_reloads_the_page_without_ending_the_talk(live_server):
    """The operator's mid-talk fix, instead of Stop + Start.

    Stop + Start closes the Soniox stream and finalizes the transcript to fix
    a browser. This makes the page start over while the session keeps
    running - which is checked here by watching the response count keep
    climbing across the reload.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                admin(live_server, "/admin/start", capture=SOAK_CAPTURE,
                      paced="true", speed="400")
                page.wait_for_function(
                    "() => document.querySelectorAll("
                    "'#screen-auto .auto-para').length > 1", timeout=30000)
                before = page.evaluate(AUDIT)
                assert before["loads"] == 1
                responses_before = status(live_server)["responses"]

                admin(live_server, "/admin/screens/reload")
                page.wait_for_function(
                    "() => Number(window.name || 0) > 1", timeout=20000)
                page.wait_for_function(
                    "() => window.CaptionScreen "
                    "&& document.querySelectorAll("
                    "'#screen-auto .auto-para').length > 0", timeout=30000)

                after = page.evaluate(AUDIT)
                assert after["loads"] == 2, "the page did not reload"
                assert after["rendered"] == "auto"
                state = status(live_server)
                assert state["live"], "the reload ended the session"
                assert state["responses"] > responses_before, (
                    "the session stopped producing captions across a reload")
        finally:
            admin(live_server, "/admin/stop")
            browser.close()


@needs_browser
def test_three_hours_in_auto_never_shows_grey_text_or_an_original(live_server):
    """The long one: 2,000+ paragraphs, two drops, past the trim.

    Every word of the synthetic talk is labelled `orig` or `trans`, so "the
    projector never shows the original" is a substring test on what is
    actually rendered rather than an argument about routing. The `.partial`
    count is the other half: grey text is what the operator described, and
    the translation-only pane must never produce any.
    """
    from playwright.sync_api import sync_playwright

    admin(live_server, "/admin/screen", view="auto", size="l")
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (context, page):
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)[:200]))
                # Three hours in about 72 seconds of wall clock, which has to
                # leave room for two 17-second outages and still be sampling
                # in between.
                admin(live_server, "/admin/start", capture=SOAK_CAPTURE,
                      paced="true", speed="150")
                page.wait_for_function(
                    "() => document.querySelectorAll("
                    "'#screen-auto .auto-para').length > 2", timeout=40000)

                worst = {"paragraphs": 0, "en": 0}
                drops = 0
                started = time.monotonic()
                # Wall-clock, not paragraph counts: the page caps what it
                # keeps, so its own paragraph count stops being a clock.
                drop_at = (10.0, 30.0)
                deadline = started + 180
                while time.monotonic() < deadline:
                    seen = page.evaluate(AUDIT)
                    assert seen["grey"] == 0, (
                        "the translation-only pane is rendering unconfirmed "
                        "text; no translation token is ever non-final and "
                        "auto has no partials, so this cannot happen by "
                        "accident")
                    assert seen["originals"] == 0, (
                        f"{seen['originals']} paragraph(s) of original "
                        f"speech are on the projector")
                    assert seen["mismatched"] == 0, (
                        f"{seen['mismatched']} paragraph(s) are labelled one "
                        f"language and written in the other")
                    assert seen["rendered"] == "auto"
                    worst["paragraphs"] = max(worst["paragraphs"],
                                              seen["paragraphs"])
                    worst["en"] = max(worst["en"], seen["en"])

                    if (drops < len(drop_at)
                            and time.monotonic() - started >= drop_at[drops]):
                        # Forced reconnects, each past the 12 s silence
                        # limit, at two different points in the talk.
                        drops += 1
                        context.set_offline(True)
                        page.wait_for_timeout(14000)
                        context.set_offline(False)
                        page.wait_for_timeout(3000)

                    if not status(live_server).get("live"):
                        break
                    page.wait_for_timeout(1500)

                assert drops == 2, (
                    f"only {drops} forced reconnect(s) happened; the test did "
                    f"not exercise what it claims")
                # The page caps what it keeps at `KEEP_PARAGRAPHS`, which is
                # above the server's `MAX_PARAGRAPHS` - so a page *at* its own
                # cap has been sent more paragraphs than the server holds,
                # which is the trim having happened.
                assert worst["en"] >= KEEP_PARAGRAPHS, (
                    f"the talk only reached {worst['en']} English paragraphs "
                    f"on the page, so the server's {MAX_PARAGRAPHS}-paragraph "
                    f"trim was never crossed and this test proves less than "
                    f"it says")
                assert not errors, f"the page threw: {errors[:3]}"
        finally:
            admin(live_server, "/admin/stop")
            browser.close()
