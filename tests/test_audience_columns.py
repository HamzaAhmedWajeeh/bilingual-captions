"""In "Both", each language column scrolls and follows the talk on its own.

**The bug this file exists for.** The two columns were reported as sharing one
scroll container. They were not - each `.stream` has had its own
`overflow-y: auto` since Phase 3, and `app.js` has tracked `following[lang]`
per language just as long. What was actually broken was two different things
that look the same from the reader's seat:

1. **"Both" was not two columns on a laptop at all.** `body:not(.admin) main`
   sets `grid-template-columns: 1fr` at specificity (0,1,2); the override was
   written as a bare `main.both`, which is (0,1,1), and a media query adds no
   specificity. So the single-column declaration won at *every* width and the
   two streams were laid out as stacked grid tracks - one explicit `1fr` row
   and one implicit content-sized row, measuring 336px and 646px at
   1920x1080. Two badly mismatched stacked panes is exactly what "the other
   column is not at its own latest line" looks like.

2. **"Jump to live" was one page-level button for both columns.** It appeared
   when *either* visible column fell behind and scrolled *both* back to the
   live edge, so catching the English up threw away the place the reader had
   scrolled to in the Arabic.

The layout assertions below are the regression guard for (1): the specificity
trap is invisible in review and silently reverts the desktop layout, so it is
checked by measuring a real browser rather than by reading the stylesheet.

The browser tests need Playwright, which is **not** a project dependency
(the dependency list). They are skipped unless it is importable:

    uv run --with playwright pytest tests/test_audience_columns.py
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.settings import WEB_DIR  # noqa: E402
from captures import needs_capture, needs_captures  # noqa: E402

APP_JS = (WEB_DIR / "app.js").read_text(encoding="utf-8")
STYLE = (WEB_DIR / "style.css").read_text(encoding="utf-8")
INDEX = (WEB_DIR / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Read from the shipped files: cheap, and they run in the ordinary suite
# ---------------------------------------------------------------------------


def test_each_column_has_its_own_jump_button():
    """One button per language, and no page-level one left behind."""
    assert 'id="jump-en"' in INDEX
    assert 'id="jump-ar"' in INDEX
    assert 'id="jump"' not in INDEX, "the single shared button must be gone"


def test_the_both_layout_rules_outrank_the_single_column_default():
    """The specificity trap, guarded in the stylesheet as well as in a browser.

    `main.both` alone loses to `body:not(.admin) main`, so the prefix is not
    cosmetic - dropping it silently turns the laptop layout back into two
    mismatched stacked panes.
    """
    assert "body:not(.admin) main.both" in STYLE
    # ...and no bare `main.both` rule that would lose the cascade.
    for line in STYLE.splitlines():
        stripped = line.strip()
        if stripped.startswith("main.both"):
            pytest.fail(f"bare `main.both` rule loses to the default: {stripped}")


def test_the_jump_button_is_positioned_inside_its_column():
    """`position: fixed` would pin one button to the viewport, which cannot
    work when two are on screen at once."""
    assert ".jump {" in STYLE
    jump_block = STYLE.split(".jump {", 1)[1].split("}", 1)[0]
    assert "position: absolute" in jump_block
    assert "position: fixed" not in jump_block
    assert ".column {" in STYLE
    column_block = STYLE.split(".column {", 1)[1].split("}", 1)[0]
    assert "position: relative" in column_block


def test_a_column_is_hidden_as_a_whole():
    """Hiding only the stream would leave that column's button floating over
    nothing."""
    assert "columns[lang].hidden" in APP_JS
    assert ".column[hidden]" in STYLE


def test_jump_visibility_is_decided_per_language():
    """The old version used `.some(...)` over the visible languages, which is
    what made one column's button answer for the other."""
    assert "jumps[lang].hidden" in APP_JS
    assert "var behind = visibleLangs().some" not in APP_JS


def test_each_button_is_labelled_in_its_own_language():
    assert "TEXT[lang].jump" in APP_JS


# ---------------------------------------------------------------------------
# The browser tests: measure a real layout, with real captions in it
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

PORT = 8112
BASE = f"http://127.0.0.1:{PORT}"

#: Two-way translation puts every utterance in both streams, so no capture is
#: wildly lopsided. This is the most uneven one saved: 18 English paragraphs
#: and 5333 characters against 13 Arabic and 3962, which is enough for the
#: columns to have visibly different scroll heights - the condition that made
#: a shared scroll container show up as a wrong line.
CAPTURE = "run4-saudi.jsonl"

#: An obvious fake, as `tests/test_server.py` requires of every test.
TOKEN = "columns-test-token"

PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1920, "height": 1080}


@pytest.fixture(scope="module")
def live_server():
    import os
    import shutil
    import tempfile
    import urllib.request

    # A server started here is an operator as far as every write path is
    # concerned: `CAPTIONS_STATE_DIR` keeps it off `config/session.yaml` and
    # `config/screen.yaml`, `CAPTIONS_LOG_DIR` keeps it out of the spend
    # ledger and the screen audit, and `CAPTIONS_TRANSCRIPT_DIR` keeps it out
    # of a real session's transcript. This suite had none of them: it was
    # writing the operator's `logs/usage.jsonl` on every run.
    state = tempfile.mkdtemp(prefix="captions-columns-")
    env = dict(os.environ, ADMIN_TOKEN=TOKEN, PYTHONIOENCODING="utf-8",
               CAPTIONS_STATE_DIR=state,
               CAPTIONS_LOG_DIR=str(Path(state) / "logs"),
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


def admin(base: str, path: str, **params) -> None:
    import urllib.parse
    import urllib.request

    params["token"] = TOKEN
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, method="POST")
    urllib.request.urlopen(request, timeout=10).read()


GEOMETRY = """() => {
  const main = document.getElementById('streams');
  const out = { mainScrolls: main.scrollHeight > main.clientHeight };
  for (const lang of ['en', 'ar']) {
    const el = document.getElementById('stream-' + lang);
    const box = el.getBoundingClientRect();
    out[lang] = {
      x: Math.round(box.x), y: Math.round(box.y),
      w: Math.round(box.width), h: Math.round(box.height),
      ownScroller: getComputedStyle(el).overflowY === 'auto',
      scrollHeight: el.scrollHeight,
      scrollTop: Math.round(el.scrollTop),
      gapToBottom: Math.round(el.scrollHeight - el.scrollTop - el.clientHeight),
      jumpShown: !document.getElementById('jump-' + lang).hidden,
    };
  }
  return out;
}"""


@contextlib.contextmanager
def both_view(browser, base: str, viewport: dict):
    """A page showing "Both", filled from a capture, scrolled to the live edge."""
    context = browser.new_context(viewport=viewport)
    # Set before any script runs, so the page comes up in "Both" directly
    # rather than being switched into it afterwards.
    context.add_init_script(
        "try { localStorage.setItem('captions.view', 'both'); } catch (e) {}")
    page = context.new_page()
    try:
        page.goto(base + "/", wait_until="domcontentloaded")
        admin(base, "/admin/start", capture=CAPTURE, paced="false")
        for lang in ("en", "ar"):
            page.wait_for_function(
                "(l) => document.getElementById('stream-' + l)"
                "         .innerText.length > 200",
                arg=lang, timeout=20000)
        page.wait_for_timeout(600)       # let the throttled tail land
        yield page
    finally:
        with contextlib.suppress(Exception):
            admin(base, "/admin/stop")
        context.close()


@needs_browser
@pytest.mark.parametrize("name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
@needs_capture("run4-saudi.jsonl")
def test_each_column_is_its_own_scroll_container(live_server, name, viewport):
    """Neither `main` nor the page scrolls; both streams do, separately."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with both_view(browser, live_server, viewport) as page:
                geo = page.evaluate(GEOMETRY)
                assert not geo["mainScrolls"], (
                    "the grid that holds the columns must not scroll; if it "
                    "does, one language's growth moves the other")
                for lang in ("en", "ar"):
                    assert geo[lang]["ownScroller"], f"{lang} has no scroller"
                    assert geo[lang]["scrollHeight"] > geo[lang]["h"], (
                        f"{lang} did not overflow, so this proves nothing - "
                        f"the capture was too short for this viewport")
                # The premise of the whole test: the columns differ in length.
                assert geo["en"]["scrollHeight"] != geo["ar"]["scrollHeight"]
        finally:
            browser.close()


@needs_browser
@pytest.mark.parametrize("name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
@needs_capture("run4-saudi.jsonl")
def test_both_lays_the_columns_out_evenly(live_server, name, viewport):
    """Side by side once there is room, stacked on a phone - and equal either
    way. This is the regression guard for the CSS specificity trap."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with both_view(browser, live_server, viewport) as page:
                geo = page.evaluate(GEOMETRY)
                en, ar = geo["en"], geo["ar"]
                assert abs(en["h"] - ar["h"]) <= 2, (
                    f"the columns are {en['h']}px and {ar['h']}px; in "
                    f"{viewport['width']}x{viewport['height']} they should "
                    f"share the space evenly")
                if viewport["width"] >= 736:      # the 46rem breakpoint
                    assert en["x"] != ar["x"] and en["y"] == ar["y"], (
                        "on a laptop the two languages belong side by side")
                else:
                    assert en["y"] != ar["y"] and en["x"] == ar["x"], (
                        "on a phone the two languages belong stacked")
        finally:
            browser.close()


@needs_captures
@needs_browser
@pytest.mark.parametrize("name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
def test_scrolling_one_column_back_leaves_the_other_following(
        live_server, name, viewport):
    """The reader re-reads a line in English while the Arabic keeps up.

    Only the column that stopped following offers to catch up, and taking that
    offer must not move the column that never left the live edge.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with both_view(browser, live_server, viewport) as page:
                page.evaluate(
                    "() => { document.getElementById('stream-en').scrollTop = 0; }")
                page.wait_for_timeout(250)

                geo = page.evaluate(GEOMETRY)
                assert geo["en"]["scrollTop"] == 0
                assert geo["en"]["jumpShown"], "English fell behind silently"
                assert geo["ar"]["gapToBottom"] == 0, (
                    "scrolling the English moved the Arabic - the columns "
                    "share a scroll container")
                assert not geo["ar"]["jumpShown"], (
                    "the Arabic is at its own live edge but offers to jump; "
                    "that button is answering for the other column")

                # More captions: the Arabic follows, the English stays put.
                admin(live_server, "/admin/stop")
                admin(live_server, "/admin/start",
                      capture="run1.jsonl", paced="false")
                page.wait_for_timeout(1500)

                geo = page.evaluate(GEOMETRY)
                assert geo["en"]["scrollTop"] == 0, (
                    "new captions dragged the English column back down")
                assert geo["ar"]["gapToBottom"] == 0, (
                    "the Arabic stopped following its own newest text")

                # Taking the English offer catches it up and nothing else.
                before = page.evaluate(
                    "() => document.getElementById('stream-ar').scrollTop")
                page.click("#jump-en")
                page.wait_for_timeout(250)
                geo = page.evaluate(GEOMETRY)
                after = page.evaluate(
                    "() => document.getElementById('stream-ar').scrollTop")
                assert geo["en"]["gapToBottom"] == 0, "the jump did not catch up"
                assert not geo["en"]["jumpShown"]
                assert abs(after - before) <= 2, (
                    "jumping the English column also moved the Arabic one")
        finally:
            browser.close()


@needs_browser
@needs_capture("run4-saudi.jsonl")
def test_one_language_view_shows_only_that_columns_button(live_server):
    """A hidden column must not offer to jump, and its button must not show
    over the language that is on screen."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with both_view(browser, live_server, DESKTOP) as page:
                page.click("#view-en")
                page.evaluate(
                    "() => { document.getElementById('stream-en').scrollTop = 0; }")
                page.wait_for_timeout(250)
                geo = page.evaluate(GEOMETRY)
                assert geo["en"]["jumpShown"]
                assert not geo["ar"]["jumpShown"], (
                    "the hidden Arabic column is still offering to jump")
                assert page.evaluate(
                    "() => document.getElementById('column-ar').hidden") is True
        finally:
            browser.close()
