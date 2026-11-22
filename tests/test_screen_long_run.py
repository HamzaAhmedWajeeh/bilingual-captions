"""The projector over a long session: does the page keep up, and keep still?

`tests/test_screen_auto.py` is about what the room sees in the first minute.
This file is about hour three, and it exists because of the 2026-10-24 report
that the Auto view was showing the original half an hour in. Nothing in
`CaptionState` or the protocol reproduces that - `tests/test_long_session.py`
measures both, against the recording - so what is left to check is the page:
that it stays in step with the server over a whole session, through
reconnects, and that it does not accumulate the talk in the DOM while it does.

The browser tests need Playwright, which is **not** a project dependency:

    uv run --with playwright pytest tests/test_screen_long_run.py
"""

from __future__ import annotations

import contextlib
import json
import os
import re
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

from app.captions import MAX_PARAGRAPHS  # noqa: E402
from app.settings import WEB_DIR  # noqa: E402
from captures import needs_capture  # noqa: E402

APP_JS = (WEB_DIR / "app.js").read_text(encoding="utf-8")
AUTO_JS = (WEB_DIR / "auto.js").read_text(encoding="utf-8")

#: A port of its own: the suite runs serially and two modules sharing one
#: port surface as goto timeouts that look like real regressions.
PORT = 8115
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "screen-long-run-test-token"

#: Committed, five minutes, both directions. Short enough to replay inside a
#: test and long enough to cross languages several times.
FIXTURE = "run1.jsonl"

#: The 52-minute session of 2026-10-24. In `logs/`, which is gitignored, so
#: everything that uses it skips on a fresh clone.
LIVE_LOG = ROOT / "logs" / "live-2026-10-24-143245.jsonl"

#: What the page shows of the auto stream, with its language and the script
#: it is actually written in. The newest paragraph is what a bottom-anchored
#: pane puts in front of the room.
PROBE = r"""
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
  var newest = null;
  for (var i = paras.length - 1; i >= 0; i -= 1) {
    if (paras[i].textContent.trim()) { newest = paras[i]; break; }
  }
  return {
    auto: paras.length,
    en: document.getElementById('screen-en').children.length,
    ar: document.getElementById('screen-ar').children.length,
    nodes: document.querySelectorAll('p').length,
    id: newest ? Number(newest.dataset.id) : -1,
    lang: newest ? (newest.getAttribute('lang') || '') : '',
    chars: newest ? newest.textContent.length : 0,
    script: newest ? script(newest.textContent) : 'none',
    view: window.CaptionScreen.view()
  };
}
"""


def constant(source: str, name: str) -> int:
    return int(re.search(rf"var {name} = (\d+)", source).group(1))


# ---------------------------------------------------------------------------
# Read from the shipped files
# ---------------------------------------------------------------------------


def test_the_page_caps_what_it_keeps_above_the_servers_own_cap():
    """Trimming is invisible in a diff, so the page has to cap itself.

    The server drops the oldest paragraphs at `MAX_PARAGRAPHS` and says
    nothing about it; a page only finds out when a snapshot arrives, which in
    a talk that never reconnects is never. The 2026-10-24 session left 977
    `<p>` elements on the projector against the 555 the server still held.

    The cap has to sit *above* the server's, or the page could drop a
    paragraph the server can still update.
    """
    app = constant(APP_JS, "KEEP_PARAGRAPHS")
    auto = constant(AUTO_JS, "KEEP_PARAGRAPHS")
    assert app == auto, (
        "the two renderers cap at different numbers; they hold the same kind "
        "of thing and the difference would only ever be a bug")
    assert app > MAX_PARAGRAPHS, (
        f"the page keeps {app} paragraphs and the server holds "
        f"{MAX_PARAGRAPHS}; the page must not drop one the server can still "
        f"send an update for")
    assert app < MAX_PARAGRAPHS * 3, (
        "the cap is so far above the server's that it barely bounds anything")


# ---------------------------------------------------------------------------
# The browser tests
# ---------------------------------------------------------------------------


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


@pytest.fixture(scope="module")
def live_server():
    """A server of this module's own, writing to a temporary state directory.

    `CAPTIONS_STATE_DIR` and `CAPTIONS_TRANSCRIPT_DIR` for the same reason as
    everywhere else: a test run must not touch the operator's `screen.yaml`
    or write into their `transcripts/`.
    """
    state = tempfile.mkdtemp(prefix="captions-long-run-")
    env = dict(os.environ, ADMIN_TOKEN=TOKEN, PYTHONIOENCODING="utf-8",
               CAPTIONS_STATE_DIR=state,
               CAPTIONS_TRANSCRIPT_DIR=str(Path(state) / "transcripts"),
               # ...and the three things a run writes down. Without
               # this a test server appends to the operator's own
               # spend ledger and screen audit, which is how 968
               # replay lines got into `logs/usage.jsonl`.
               CAPTIONS_LOG_DIR=str(Path(state) / "logs"))
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
    with urllib.request.urlopen(request, timeout=20) as response:
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
        page.goto(base + "/screen" + query, wait_until="domcontentloaded")
        page.wait_for_timeout(400)
        yield context, page
    finally:
        context.close()


@needs_browser
def test_a_whole_session_does_not_accumulate_in_the_projectors_dom(live_server):
    """Hour three must not be carrying hour one around.

    The 52-minute session produced 429 English and 424 Arabic paragraphs. The
    server holds 200 of each; before the page capped itself it held every one
    of them, and a three-hour talk would have left about 3,000 nodes with
    their text on a laptop driving a projector. Measured at 0.1-1.1 ms per
    message either way, so this is about memory, not speed - which is why it
    is asserted as a bound and not as a duration.
    """
    if not LIVE_LOG.exists():
        pytest.skip("the 2026-10-24 log is not in logs/ (gitignored)")
    from playwright.sync_api import sync_playwright

    cap = constant(APP_JS, "KEEP_PARAGRAPHS")
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                # Unpaced: the whole 52 minutes in a second or two, which is
                # the quick way to put a long session's worth of paragraphs
                # on the page.
                admin(live_server, "/admin/start", capture=LIVE_LOG.name,
                      paced="false")
                page.wait_for_function(
                    "() => document.querySelectorAll("
                    "'#screen-auto .auto-para').length > 20", timeout=60000)
                for _ in range(60):
                    if not status(live_server).get("live"):
                        break
                    page.wait_for_timeout(500)
                page.wait_for_timeout(1500)
                seen = page.evaluate(PROBE)

            assert seen["en"] <= cap and seen["ar"] <= cap, (
                f"the page is holding {seen['en']} English and {seen['ar']} "
                f"Arabic paragraphs, over its own cap of {cap}")
            assert seen["auto"] <= cap, (
                f"the auto pane is holding {seen['auto']} paragraphs")
            # ...and it really was a long session, or the bound proves nothing.
            assert seen["en"] >= MAX_PARAGRAPHS, (
                f"only {seen['en']} English paragraphs arrived; the log did "
                f"not replay far enough to test a cap")
        finally:
            admin(live_server, "/admin/stop")
            browser.close()


@needs_capture('run1.jsonl')
@needs_browser
def test_the_projector_stays_in_step_through_two_reconnects(live_server):
    """A page that has been open all talk must match one opened just now.

    A fresh page is handed a full snapshot, so it is ground truth. This runs a
    capture through at speed with the network dropped twice for longer than
    the 12 s silence limit - the watchdog's own reconnect path, which is what
    run9 bought - and compares the two pages at the end of each outage and at
    the end of the talk.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (context, page):
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)[:200]))
                admin(live_server, "/admin/start", capture=FIXTURE,
                      paced="true", speed="6")
                page.wait_for_function(
                    "() => document.querySelectorAll("
                    "'#screen-auto .auto-para').length > 2", timeout=40000)

                for _ in range(2):
                    context.set_offline(True)
                    page.wait_for_timeout(14000)   # past SILENCE_LIMIT (12 s)
                    context.set_offline(False)
                    page.wait_for_timeout(4000)

                    fresh = context.new_page()
                    fresh.goto(live_server + "/screen",
                               wait_until="domcontentloaded")
                    fresh.wait_for_timeout(2000)
                    fresh.evaluate("() => window.CaptionScreen.finishReveal()")
                    page.evaluate("() => window.CaptionScreen.finishReveal()")
                    fresh.wait_for_timeout(200)
                    theirs = fresh.evaluate(PROBE)
                    mine = page.evaluate(PROBE)
                    fresh.close()

                    assert mine["id"] == theirs["id"], (
                        f"after a reconnect the long-lived page is showing "
                        f"paragraph {mine['id']} and a fresh one "
                        f"{theirs['id']}")
                    assert mine["lang"] == theirs["lang"]
                    assert abs(mine["chars"] - theirs["chars"]) <= 40, (
                        "the two pages disagree about how much of the newest "
                        "paragraph has arrived")
                    assert mine["script"] == mine["lang"], (
                        f"the newest paragraph is labelled {mine['lang']} and "
                        f"written in {mine['script']}")
                assert not errors, f"the page threw: {errors[:3]}"
        finally:
            admin(live_server, "/admin/stop")
            browser.close()
