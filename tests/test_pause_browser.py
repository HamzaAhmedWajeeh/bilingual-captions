"""The paused banner and the Resume button, in a real browser.

On 2026-11-07 the operator page's whole account of the idle pause was the
word PAUSED in the top bar. There was nothing to read and nothing to press,
so the only move left was Stop and Start, which ends the talk.

The banner is what replaced that, and a banner is exactly the kind of thing
the Python tests cannot see: `tests/test_admin_page.py` can check that the
ids exist and that the payload carries the fields, and it still cannot tell
whether the sentence is on the screen or whether the button is wired to
anything. So the status poll is intercepted in the page itself, a paused
payload is handed to it, and the rendered banner is read back off the DOM.

    uv run --with playwright pytest tests/test_pause_browser.py

Nothing here starts a session, so no engine and no Soniox: the server is up
only to serve the page and its script, and every payload the page sees is
written by the test.
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
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 8117
BASE = f"http://127.0.0.1:{PORT}"
#: Obviously fake. The real token lives in `.env` and is never written down.
TOKEN = "pause-browser-test-token"

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False

needs_browser = pytest.mark.skipif(
    not HAVE_PLAYWRIGHT,
    reason="Playwright is not a project dependency; "
           "run with `uv run --with playwright`",
)

#: A stopped session's payload, as `/admin/status` sends one, with the keys
#: the page reads. The tests below copy it and set the pause on top, so each
#: one differs from a working page by exactly the thing it is about.
IDLE = {
    "running": True,
    "mode": "mic",
    "source": "Line In (USB Audio Mixer) @ 16000 Hz, ch 1/2",
    "paced": False,
    "responses": 2747,
    "dropped_seconds": 0.0,
    "capture": None,
    "transcript": "2026-11-08_1200",
    "details": {"title": False, "speakers": 0, "companies": 0, "terms": 0,
                "notes_chars": 0},
    "details_pending": False,
    "error": None,
    "elapsed": 721.0,
    "audio_ms": 721000,
    "audio_total_s": 0,
    "captures": [],
    "audio_files": [],
    "model": "stt-rt-v6",
    "screen": {"view": "auto", "size": "xl", "logo": True, "footer": True,
               "labels": True, "credit": "", "glow": True,
               "eyebrow": "", "headline": "", "highlight": True,
               "subline": "", "pill": "", "cta": "", "note": ""},
    "qr": {"source": "default", "label": "the default link",
           "ext": "svg", "tag": "00000000"},
    "usage": {"minutes": 0.0, "cost": 0.0, "runs": 0},
    "viewers": 1,
    "max_viewers": 1,
    "seq": 12,
    "rejected": 0,
    "dropped": 0,
    "screens": {"count": 1, "reporting": 1, "stale": False, "views": {},
                "trouble": [], "pinned": 0, "translated": 0, "partials": 0,
                "behind": 0, "audience": 0},
    "session": "2026-11-08-120000",
    "sessions": [],
    "logs_bytes": 0,
    "lang": None,
    "lang_drifted": False,
    "quiet": None,
    "pause": None,
    "engine_state": "streaming",
    "lag_seconds": 0.3,
    "reconnects": 0,
    "rollovers": 0,
    "silent_seconds": 0.0,
    "streamed_seconds": 630.9,
    "recording": None,
    "dropped_chunks": 0,
    "level": {"rms": 0.001, "peak": 0.003, "db": -60.0, "peak_db": -50.4,
              "clipping": False},
}

#: The session of 2026-11-07, in the state the operator found it in.
PAUSE = {
    "device": "Line In (USB Audio Mixer)",
    "silent_seconds": 601.4,
    #: The level the pause is judged on, off the engine rather than off the
    #: meter: the meter clamps at -60 dBFS for its bar, and reading it put
    #: "Input -60.0 dB, needs -66.0 dB" on the page - a level that looks
    #: above the threshold beside a pause still waiting for it. -86.0 is the
    #: real reading on this line between words.
    "input_db": -86.0,
    "idle_stop_seconds": 600.0,
    "meter_peak_db": -50.4,
    "silence_db": -66.0,
    "pauses": 1,
}


@pytest.fixture(scope="module")
def live_server():
    """A server with its state, logs and transcripts in a temporary folder.

    No session is ever started, so this is only here to serve `/admin` and
    `/static/admin.js`. The redirects are set anyway: a test run must never
    be able to write the operator's own files, whatever it believes it is
    doing.
    """
    state = tempfile.mkdtemp(prefix="captions-pause-")
    logs = Path(state) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
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


@contextlib.contextmanager
def operator(browser, base, status):
    """The operator page, with `/admin/status` answered by the test.

    `fetch` is replaced before the page's own script runs, so the page never
    reaches the real endpoint: the status payload is this test's, the level
    endpoint gets the level out of it, and every POST is recorded on
    `window.__posts` instead of being sent. That is what makes a paused page
    reachable at all - a real pause needs a real engine and ten minutes of
    silence.
    """
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    page = context.new_page()
    try:
        page.add_init_script("""
        window.__posts = [];
        window.__status = %s;
        var real = window.fetch.bind(window);
        window.fetch = function (input, init) {
          var url = String(typeof input === 'string' ? input : input.url);
          var method = (init && init.method) || 'GET';
          function reply(body) {
            return Promise.resolve(new Response(JSON.stringify(body), {
              status: 200, headers: {'Content-Type': 'application/json'}}));
          }
          if (method === 'POST') {
            window.__posts.push(url);
            return reply(window.__status);
          }
          if (url.indexOf('/admin/status') === 0) {
            return reply(window.__status);
          }
          if (url.indexOf('/admin/level') === 0) {
            return reply({level: window.__status.level});
          }
          if (url.indexOf('/admin/devices') === 0) {
            return reply({devices: [], remembered: null, record: false});
          }
          if (url.indexOf('/admin/session') === 0) {
            return reply({});
          }
          if (url.indexOf('/admin/transcripts') === 0) {
            return reply({transcripts: []});
          }
          return real(input, init);
        };
        """ % json.dumps(status))
        page.goto(f"{base}/admin?token={TOKEN}",
                  wait_until="domcontentloaded")
        # Two polls, so the first render is definitely behind us.
        page.wait_for_timeout(1400)
        yield page
    finally:
        context.close()


def paused_status() -> dict:
    status = json.loads(json.dumps(IDLE))
    status["engine_state"] = "paused"
    status["silent_seconds"] = PAUSE["silent_seconds"]
    status["pause"] = dict(PAUSE)
    return status


@needs_browser
def test_a_working_session_shows_no_paused_banner(live_server):
    """It is a banner, so it has to be absent when nothing is wrong."""
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with operator(browser, live_server, IDLE) as page:
                assert page.locator("#pause-banner").is_visible() is False
        finally:
            browser.close()


@needs_browser
def test_the_paused_banner_names_the_device_the_seconds_and_the_level(
        live_server):
    """The sentence the operator asked for, read off the rendered page.

    "Paused: no sound from <device> for N s. Check the mic or mixer." plus
    the two levels, because "no sound" is a threshold and not a fact - on
    2026-11-07 the line was carrying speech the whole time it said so.
    """
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with operator(browser, live_server, paused_status()) as page:
                banner = page.locator("#pause-banner")
                assert banner.is_visible(), "the pause has no banner"
                text = page.locator("#pause-text").inner_text()
                assert "Paused" in text
                assert "Line In (USB Audio Mixer)" in text
                assert "601 s" in text
                assert "Check the mic or mixer" in text
                # The pair has to read as a condition, not a contradiction:
                # a level below the threshold, and a threshold labelled with
                # what reaching it would do.
                assert "Input -86.0 dB" in text
                assert "needs -66.0 dB to resume" in text
                # And the pill still says it, which is the other half of
                # the same message rather than a replacement for it.
                assert page.locator("#state-pill").inner_text() == "PAUSED"
        finally:
            browser.close()


@needs_browser
def test_resume_is_a_button_on_the_banner_and_posts_to_resume(live_server):
    """The control that did not exist. One press, no new session."""
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with operator(browser, live_server, paused_status()) as page:
                button = page.locator("#resume")
                assert button.is_visible(), "there is no Resume button"
                button.click()
                page.wait_for_timeout(200)
                posts = page.evaluate("window.__posts")
                assert any("/admin/resume" in url for url in posts), (
                    f"Resume posted nowhere useful: {posts}")
                # Nothing else was pressed on the operator's behalf. Start
                # and Stop are the two things this button must not be.
                assert not any("/admin/start" in url or "/admin/stop" in url
                               for url in posts)
        finally:
            browser.close()


@needs_browser
def test_the_banner_goes_when_the_stream_comes_back(live_server):
    """A banner that outlives the pause is worse than no banner."""
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with operator(browser, live_server, paused_status()) as page:
                assert page.locator("#pause-banner").is_visible()
                page.evaluate("""
                    window.__status.engine_state = 'streaming';
                    window.__status.pause = null;
                """)
                page.wait_for_timeout(1400)
                assert page.locator("#pause-banner").is_visible() is False
        finally:
            browser.close()


@needs_browser
def test_a_quiet_input_that_is_being_transcribed_is_not_called_wrong(
        live_server):
    """Question 4, on the page. Two sentences, not one threshold.

    On 2026-11-07 the operator page asked whether the device was the right
    one for 667 seconds while 5,655 final tokens arrived through it. A line
    the engine is transcribing is the right device at any level.
    """
    quiet = {"seconds": 667.0, "peak_db": -50.4, "db": -60.0,
             "device": "Line In (USB Audio Mixer)", "speech": True}
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            status = json.loads(json.dumps(IDLE))
            status["quiet"] = dict(quiet)
            with operator(browser, live_server, status) as page:
                row = page.locator("#engine-health")
                assert row.is_visible()
                text = row.inner_text()
                assert "being transcribed" in text
                assert "right input" not in text
                # Grey, not red: nothing is wrong with the device.
                assert "bad" not in (row.get_attribute("class") or "")

                # The same levels with no words coming out of it is the
                # warning this check was built for.
                page.evaluate("window.__status.quiet.speech = false;")
                page.wait_for_timeout(1400)
                text = page.locator("#engine-health").inner_text()
                assert "right input" in text
                assert "bad" in (
                    page.locator("#engine-health").get_attribute("class")
                    or "")
        finally:
            browser.close()
