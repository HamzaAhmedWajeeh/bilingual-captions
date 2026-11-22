"""`/screen` in Auto: the projector must never look stuck, jumpy or confusing.

The caption logic behind this view is tested in `tests/test_auto_stream.py`;
this file is about what the room sees. Each test names the way the screen
looked wrong without it.

The browser tests need Playwright, which is **not** a project dependency
(the dependency list). They are skipped unless it is importable:

    uv run --with playwright pytest tests/test_screen_auto.py
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.broadcast import DEFAULT_SCREEN, SCREEN_SIZES, SCREEN_VIEWS  # noqa: E402
from app.settings import WEB_DIR  # noqa: E402
from captures import needs_capture, needs_wordmarks  # noqa: E402

AUTO_JS = (WEB_DIR / "auto.js").read_text(encoding="utf-8")

#: `brand-sizes.css` is generated from whatever wordmarks are in `brand/` and
#: is gitignored, so a fresh clone does not have one. That is not a broken
#: checkout: `style.css` declares a default for every `--wm-*` variable, and
#: the sizes below are read from whichever file actually defines them.
SIZES_CSS = (WEB_DIR / "brand-sizes.css").read_text(encoding="utf-8") \
    if (WEB_DIR / "brand-sizes.css").is_file() else ""


def wordmark_sizes() -> dict[str, float]:
    """The wordmarks' native sizes, from the generated file or the defaults.

    Read rather than hard-coded, so the size assertions below keep meaning
    "against the artwork that is actually there" when it is replaced - and
    keep working on a clone with no artwork, where the stylesheet's own
    defaults are what the browser will use.
    """
    sizes = {name: float(value) for name, value
             in re.findall(r"--(wm-[\w-]+):\s*([\d.]+);", STYLE)}
    sizes.update({name: float(value) for name, value
                  in re.findall(r"--(wm-[\w-]+):\s*([\d.]+);", SIZES_CSS)})
    return sizes
SCREEN_JS = (WEB_DIR / "screen.js").read_text(encoding="utf-8")
SCREEN_HTML = (WEB_DIR / "screen.html").read_text(encoding="utf-8")
STYLE = (WEB_DIR / "style.css").read_text(encoding="utf-8")


def constant(source: str, name: str) -> int:
    return int(re.search(rf"var {name} = (\d+)", source).group(1))


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def contrast(a: str, b: str) -> float:
    """WCAG 2.1 relative-luminance contrast, as a devtools audit reports it."""
    def luminance(colour: str) -> float:
        raw = colour.lstrip("#")
        r, g, b_ = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
        return (0.2126 * _channel(r) + 0.7152 * _channel(g)
                + 0.0722 * _channel(b_))

    first, second = luminance(a), luminance(b)
    hi, lo = max(first, second), min(first, second)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
# Read from the shipped files
# ---------------------------------------------------------------------------


def test_the_reveal_cannot_add_more_than_half_a_second():
    """UX rule 2 is a number, so it is checked as one.

    The whole backlog is cleared inside `REVEAL_WINDOW_MS`, and anything
    still waiting after `MAX_LAG_MS` is painted at once - so the reveal's
    worst case is the larger of the two, and both have to stay small.
    """
    window = constant(AUTO_JS, "REVEAL_WINDOW_MS")
    hard = constant(AUTO_JS, "MAX_LAG_MS")
    assert window <= 500, "the reveal is allowed ~0.5 s and no more"
    assert hard <= 700, "the escape hatch must fire while it still matters"
    assert constant(AUTO_JS, "MIN_STEP_MS") < constant(AUTO_JS, "MAX_STEP_MS")


def test_the_hard_flush_keeps_the_loop_running():
    """The flush branch must schedule another frame, not just return.

    Returning there left the loop stopped with nothing settled, so
    `onIdle(true)` never fired and the listening dots stayed down for the
    rest of the session - the exact "looks stuck" this view exists to
    prevent. It fires whenever a chunk is too big to pace inside
    `MAX_LAG_MS`, which includes the first snapshot a page receives, so a
    phone joining mid-talk hit it every time.
    """
    branch = AUTO_JS.split("if (now - pendingSince >= MAX_LAG_MS) {", 1)[1]
    branch = branch.split("}", 1)[0]
    assert "schedule();" in branch, (
        "the hard flush returns without rescheduling; the reveal loop stops "
        "and the listening indicator never comes back")


def test_the_reveal_speeds_up_when_it_falls_behind():
    """A fixed per-word delay turns a long chunk into a crawl."""
    assert "REVEAL_WINDOW_MS / pending" in AUTO_JS, (
        "the step must be divided across the backlog, not fixed")
    assert "now - pendingSince >= MAX_LAG_MS" in AUTO_JS


def test_the_listening_indicator_is_three_dots_and_no_text():
    """UX rule 1: calm, and never a spinner."""
    dots = re.search(r'<div class="listening"[^>]*>(.*?)</div>',
                     SCREEN_HTML, re.S).group(1)
    assert dots.count("<i>") == 3
    assert not re.sub(r"<[^>]+>", "", dots).strip(), (
        "the listening indicator carries text; it must be dots alone")
    # "No spinner" means nothing rotates, which is checkable; the word
    # itself appears in the stylesheet's own comment explaining why.
    assert "rotate" not in STYLE.lower()
    assert "spin" not in re.findall(r"@keyframes (\w+)", STYLE)
    # It must also respect a viewer who asked for no motion.
    assert "prefers-reduced-motion" in STYLE


def test_the_screen_shows_no_error_banner_while_reconnecting():
    """UX rule 5: a projector never shows the audience an error.

    `/screen` has no banner element at all, which is a stronger guarantee
    than remembering not to fill one in.
    """
    assert "banner" not in SCREEN_HTML
    assert "linkdot" in SCREEN_HTML
    assert "error" not in SCREEN_JS.lower() or "onStatus" in SCREEN_JS


def test_the_title_card_is_bilingual_and_not_an_error():
    """UX rule 4: when nothing is live, a branded card - never a blank, and
    never an error."""
    card = SCREEN_HTML.split('class="titlecard"', 1)[1]
    assert 'lang="en"' in card and 'lang="ar"' in card
    assert "Live translation" in card
    assert "\u062a\u0631\u062c\u0645\u0629" in card, "no Arabic lead line"
    assert "error" not in card[:1200].lower()
    assert 'class="sparkle"' in card, "the card has no accent"


def test_the_title_card_fades_and_respects_reduced_motion():
    """It appears in front of a room, so it fades - unless the system has
    been asked for no motion."""
    block = STYLE.split(".titlecard {", 1)[1].split("}", 1)[0]
    assert "transition:" in block
    # It cannot use `hidden`: the global rule is `display: none !important`
    # and nothing fades out of that.
    assert "visibility" in block
    assert 'id="titlecard"' in SCREEN_HTML
    head = SCREEN_HTML.split('id="titlecard"', 1)[1][:40]
    assert "hidden" not in head, "a hidden card cannot be faded in"
    chunks = STYLE.split("@media (prefers-reduced-motion: reduce)")
    assert any(".titlecard" in chunk[:220] for chunk in chunks[1:]), (
        "the card animates for someone who asked it not to")


def test_the_title_card_lockup_is_one_knob_and_never_upscaled():
    """Two wordmarks, one variable.

    They become SVGs later, so the size stays a single knob - and until
    then neither may be blown up past its master's own width: 389px for the
    Latin mark, and the Arabic one derived from it so both land on native
    together.
    """
    assert "--title-lockup:" in STYLE
    value = STYLE.split("--title-lockup:", 1)[1].split(";", 1)[0]
    assert "var(--wm-en-w)" in value and "min(" in value, (
        "the title lockup can be upscaled into blur, or its cap is a "
        "literal rather than the artwork's own width")
    for mark in ("en", "ar"):
        block = STYLE.split(f".titlecard-wordmark.{mark} {{", 1)[1] \
                     .split("}", 1)[0]
        assert "var(--title-lockup)" in block, (
            f"the {mark} wordmark is sized by something other than the knob")
    # The Arabic mark is a fraction of the English one, not a second number.
    arabic = STYLE.split(".titlecard-wordmark.ar {", 1)[1].split("}", 1)[0]
    assert "calc(" in arabic, "a second hard-coded width would drift"


def test_the_title_card_shows_both_scripts_of_the_official_mark():
    """The card is the first thing the room sees, and the brand is
    bilingual. One file each, so each is sized against its own artwork."""
    card = SCREEN_HTML.split('class="titlecard"', 1)[1].split("</div>", 1)[0]
    assert "/brand/wordmark-ar.png" in card
    assert "/brand/wordmark-en.png" in card
    # Latin first, Arabic second - the order the default theme uses, and
    # the order the row renders left to right.
    assert card.index("wordmark-en.png") < card.index("wordmark-ar.png"), (
        "the lockup is reversed against the official artwork")
    assert "/brand/mark.png" not in card, (
        "the stacked tile is still on the card beside the wordmarks")


def test_the_lockup_is_one_unwrappable_row_with_a_forced_direction():
    """Side by side, and it cannot come apart.

    `/screen` renders Arabic constantly, so the card lives inside RTL
    content; without a direction forced on the row itself an RTL context
    reverses flex order and puts the Arabic mark on the left, which is not
    the lockup. `nowrap` is the other half: a row that wraps stops being a
    lockup and becomes a list.
    """
    lockup = SCREEN_HTML.split('class="titlecard-lockup"', 1)[1][:80]
    assert 'dir="ltr"' in lockup, (
        "the lockup row has no forced direction; an RTL context will swap "
        "the two marks")
    block = STYLE.split(".titlecard-lockup {", 1)[1].split("}", 1)[0]
    assert "flex-direction: row" in block and "row-reverse" not in block
    assert "flex-wrap: nowrap" in block
    assert "justify-content: center" in block, "the row is not centred"
    # A gap, and not a pixel count - it has to track the text size.
    gap = re.search(r"gap:\s*([^;]+);", block).group(1)
    assert "px" not in gap, f"the lockup gap is a pixel value: {gap}"
    assert re.search(r"[\d.]+(em|vw)", gap), f"unexpected gap unit: {gap}"


def test_the_footer_strip_is_dim_still_and_reserves_its_own_band():
    """It must never overlap a caption, and never draw the eye."""
    assert 'id="screenfooter"' in SCREEN_HTML
    footer = SCREEN_HTML.split('id="screenfooter"', 1)[1].split("</div>", 1)[0]
    assert 'lang="en"' in footer and 'lang="ar"' in footer
    assert "may contain mistakes" in footer
    assert 'body.screen[data-footer="1"] main { padding-bottom' in STYLE, (
        "the footer is overlaid rather than given space of its own")
    block = STYLE.split(".screenfooter {", 1)[1].split("}", 1)[0]
    assert "animation" not in block
    # Dim relative to the captions, but readable from the back of a room.
    # The first version was #5d5d66 - 3.2:1 on black - and vanished on a
    # projector, so this is pinned as a ratio rather than "looks grey".
    colour = re.search(r"color:\s*(#[0-9a-fA-F]{6})", block).group(1)
    footer = contrast(colour, "#000000")
    captions = contrast("#f4f4f5", "#000000")
    assert footer >= 5.0, f"the footer reads {footer:.2f}:1 on black"
    assert footer < captions * 0.6, (
        f"the footer is {footer:.2f}:1 against captions at {captions:.2f}:1; "
        f"it is competing with them")


def test_the_footer_height_is_defined_once():
    """The band, the padding that reserves it and the dots above it all read
    the same variable; a second number would drift."""
    assert "--footer-h:" in STYLE
    assert STYLE.count("var(--footer-h)") >= 3


def test_a_direction_label_says_which_way_it_turned():
    assert "data-turn" in AUTO_JS, "auto.js never records the turn"
    assert "\u2192" in AUTO_JS, "the label has no arrow"
    assert 'body.screen[data-labels="1"] .auto-para.turn::before' in STYLE
    assert "content: attr(data-turn)" in STYLE


def test_the_screen_background_is_black_edge_to_edge():
    """The grid paints `--line` for the audience page's column hairline; on
    the projector that showed through the stream's top fade as a grey wash.

    The pane must paint over it, and with the brand black specifically -
    which black, and that there is only one of them, is
    `tests/test_brand.py`'s business."""
    assert "body.screen main { background: var(--bg-brand); }" in STYLE


def test_the_view_is_bottom_anchored():
    """UX rule 3: the newest line is at the bottom and nothing above it moves."""
    block = STYLE.split("body.screen .stream {", 1)[1].split("}", 1)[0]
    assert "justify-content: flex-end" in block
    assert "overflow: hidden" in block


def test_each_auto_paragraph_styles_itself():
    """Consecutive paragraphs disagree about direction, so the container
    cannot state it once."""
    assert '.auto-para[dir="rtl"]' in STYLE
    assert '.auto-para[dir="ltr"]' in STYLE
    assert ".auto-para.turn" in STYLE, "no separator at a direction change"


def test_hidden_really_hides_whatever_display_a_rule_sets():
    """The `/screen` panes are laid out by a (0,2,1) selector, so a plain
    `[hidden] { display: none }` lost and all three panes stayed on the grid -
    the visible one got a third of the projector."""
    assert "[hidden] { display: none !important; }" in STYLE


def test_the_defaults_are_auto_and_a_readable_size():
    assert DEFAULT_SCREEN["view"] == "auto"
    assert DEFAULT_SCREEN["view"] in SCREEN_VIEWS
    assert DEFAULT_SCREEN["size"] in SCREEN_SIZES


# ---------------------------------------------------------------------------
# The credit line: deploy-time text, shown only when the operator says so
# ---------------------------------------------------------------------------


def test_the_credit_is_its_own_switch_and_empty_by_default():
    """The words are the toggle.

    There is deliberately no "configured but hidden" state: a credit that is
    set is a credit that shows, so there is never a string sitting on the hub
    waiting to be leaked by a flag flipping the wrong way.
    """
    from app.broadcast import BroadcastHub
    from app.captions import CaptionState

    hub = BroadcastHub(CaptionState())
    assert hub._with_screen({})["screen"]["credit"] == ""

    assert hub.set_screen(credit="Live translation by Someone") is True
    assert hub._with_screen({})["screen"]["credit"] == (
        "Live translation by Someone")

    assert hub.set_screen(credit="") is True
    assert hub._with_screen({})["screen"]["credit"] == ""


def test_the_credit_is_tidied_and_bounded():
    """It goes on a projector, so it is one line and it cannot run away."""
    from app.broadcast import MAX_CREDIT_CHARS, BroadcastHub
    from app.captions import CaptionState

    hub = BroadcastHub(CaptionState())
    hub.set_screen(credit="  lots   of\n  space  ")
    assert hub.screen["credit"] == "lots of space"

    hub.set_screen(credit="x" * (MAX_CREDIT_CHARS + 50))
    assert len(hub.screen["credit"]) == MAX_CREDIT_CHARS


def test_the_screen_settings_survive_a_restart():
    """The operator sets this before a talk; a restart must not undo it."""
    import tempfile
    from pathlib import Path as _Path

    from app.broadcast import BroadcastHub
    from app.captions import CaptionState
    from app.settings import load_screen, save_screen

    with tempfile.TemporaryDirectory() as directory:
        path = _Path(directory) / "screen.yaml"
        hub = BroadcastHub(CaptionState())
        hub.on_change = lambda data: save_screen(data, path)
        hub.set_screen(view="ar", size="xl", logo=True,
                       credit="Captions by Someone")

        fresh = BroadcastHub(CaptionState())
        fresh.adopt_screen(load_screen(path))
        assert fresh.screen["view"] == "ar"
        assert fresh.screen["size"] == "xl"
        assert fresh.screen["logo"] is True
        assert fresh.screen["credit"] == "Captions by Someone"


def test_a_damaged_settings_file_cannot_stop_the_server():
    """It is read on the morning of a talk, so it fails soft."""
    from app.broadcast import DEFAULT_SCREEN, BroadcastHub
    from app.captions import CaptionState

    hub = BroadcastHub(CaptionState())
    hub.adopt_screen({"view": "nonsense", "size": 42, "credit": None,
                      "unknown": "ignored"})
    assert hub.screen["view"] == DEFAULT_SCREEN["view"]
    assert hub.screen["size"] == DEFAULT_SCREEN["size"]
    assert "unknown" not in hub.screen

    hub.adopt_screen("not a mapping")
    assert hub.screen["view"] == DEFAULT_SCREEN["view"]


def test_the_credit_is_saved_outside_the_session_file():
    """It says who is running the captions, not who is speaking. Speaker
    names live in `config/session.yaml`; this lives in its own file, and
    both are gitignored because neither may ever be committed."""
    from app.settings import Settings

    assert "screen_credit" in Settings.model_fields, (
        "the .env default should remain, for a fresh install")
    assert Settings.model_fields["screen_credit"].default == ""

    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "load_screen()" in main and "save_screen" in main
    # ...and the saved file is never committed.
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "config/screen.yaml" in ignored


def test_the_operator_types_the_credit_rather_than_toggling_it():
    admin_html = (WEB_DIR / "admin.html").read_text(encoding="utf-8")
    field = admin_html.split('id="screen-credit"', 1)[0][-200:]
    assert 'type="text"' in field, "the credit is still a checkbox"
    assert "Credit line" in admin_html


def test_the_toggles_all_have_a_control_on_the_operator_page():
    admin_html = (WEB_DIR / "admin.html").read_text(encoding="utf-8")
    for name in ("screen-logo", "screen-glow", "screen-footer",
                 "screen-labels", "screen-credit"):
        assert f'id="{name}"' in admin_html, f"no control for {name}"
    admin_js = (WEB_DIR / "admin.js").read_text(encoding="utf-8")
    for name in ("logo", "glow", "footer", "labels", "credit"):
        assert f"{name}: els.screen" in admin_js or f"'{name}'" in admin_js, (
            f"{name} is never sent to /admin/screen")
    assert "els.screenCredit.value" in admin_js, (
        "the credit is sent as a flag rather than as its text")


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

PORT = 8113
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "screen-auto-test-token"

#: The 2026-10-17 dry run: the operator's own voice in English, then Arabic
#: clip, so both directions appear. It replays from `logs/` unchanged - a
#: live response log is already a capture.
CAPTURE = "live-2026-10-17-011114.jsonl"


@pytest.fixture(scope="module")
def live_server():
    import os
    import urllib.request

    # These tests drive `/admin/screen`, and the server saves what the
    # operator sets. `CAPTIONS_STATE_DIR` sends the two writable config files
    # to a temporary directory, so the suite cannot overwrite the real
    # `config/screen.yaml` on the laptop - which is a quiet way to lose a
    # setting on the morning of a talk.
    import shutil
    import tempfile

    state = tempfile.mkdtemp(prefix="captions-screen-")

    env = dict(os.environ, ADMIN_TOKEN=TOKEN, PYTHONIOENCODING="utf-8",
               CAPTIONS_STATE_DIR=state,
               # ...and the transcripts, for the same reason. No test here
               # starts a live session, so nothing should write one - which
               # is exactly why it costs nothing to make it impossible.
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


def admin(base: str, path: str, **params) -> None:
    import urllib.parse
    import urllib.request

    params["token"] = TOKEN
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, method="POST")
    urllib.request.urlopen(request, timeout=15).read()


@contextlib.contextmanager
def projector(browser, base, query="", width=1920, height=1080):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    try:
        page.goto(base + "/screen" + query, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        yield context, page
    finally:
        context.close()


def fill(page, base, capture=CAPTURE):
    admin(base, "/admin/start", capture=capture, paced="false")
    page.wait_for_function(
        "() => document.querySelectorAll('#screen-auto .auto-para').length > 2",
        timeout=25000)
    page.wait_for_timeout(1200)
    page.evaluate("() => window.CaptionScreen.finishReveal()")
    page.wait_for_timeout(150)


@contextlib.contextmanager
def mid_talk(page, base, capture=CAPTURE):
    """Captions on screen with the session still **running**.

    `fill` is the quick way to get text on the page, but it pushes the
    whole capture through and the session then ends - which brings the
    waiting screen up over everything. Anything that measures what the
    projector is painting has to hold the talk open instead, or it is
    looking at the poster: a white QR card reads as a caption background
    of #FFFFFF, and the corner marks are behind the cover entirely.
    """
    admin(base, "/admin/start", capture=capture, paced="true")
    try:
        page.wait_for_function(
            "() => document.querySelectorAll("
            "'#screen-auto .auto-para').length > 2", timeout=40000)
        page.evaluate("() => window.CaptionScreen.finishReveal()")
        page.wait_for_timeout(200)
        assert not page.evaluate("() => window.CaptionScreen.carded()"), (
            "the title card is up, so the captions are covered")
        assert not page.evaluate("() => window.CaptionScreen.waiting()"), (
            "the waiting screen is up, so the captions are covered")
        yield
    finally:
        admin(base, "/admin/stop")


@needs_browser
@pytest.mark.parametrize("width,height", [(1920, 1080), (1280, 720)])
@needs_wordmarks
def test_the_lockup_renders_as_one_row_latin_first(live_server, width,
                                                   height):
    """Measured in the browser, because the stylesheet cannot tell you
    this. Flex order, writing direction, the `--wm-ar-en-ratio` width and
    the baseline nudge all have to come out right together, and reading
    any one of them says nothing about where the two marks actually land.

    Both projector sizes: at 1280x720 the cap still binds, so the row is
    the same 793px of artwork inside a narrower card and is the tighter
    fit of the two.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server, "?card=1", width,
                           height) as (_, page):
                # `?card=1` holds the card up without a session. Since the
                # waiting screen took over the before-and-after, the card is
                # only ever on screen during the few seconds between the
                # first word and its translation, and racing a replay for
                # that window is no way to measure a layout.
                assert page.evaluate("() => window.CaptionScreen.carded()"), (
                    "the title card is not up, so there is no lockup to "
                    "measure")
                box = page.evaluate("""() => {
                  const q = s => document.querySelector(s)
                                   .getBoundingClientRect();
                  const card = q('.titlecard');
                  const pad = parseFloat(getComputedStyle(
                      document.querySelector('.titlecard')).paddingLeft);
                  return {
                    en: q('.titlecard-wordmark.en'),
                    ar: q('.titlecard-wordmark.ar'),
                    row: q('.titlecard-lockup'),
                    lead: q('.titlecard-lead'),
                    avail: card.width - 2 * pad,
                    overflow: document.documentElement.scrollWidth
                              > document.documentElement.clientWidth,
                  };
                }""")
        finally:
            browser.close()

    en, ar, row = box["en"], box["ar"], box["row"]

    # Side by side, Latin on the left, with clear air between them.
    assert en["right"] <= ar["x"], (
        f"the Latin mark ends at {en['right']:.0f} but the Arabic one "
        f"starts at {ar['x']:.0f}; they are not side by side left to right")
    gap = ar["x"] - en["right"]
    assert gap > 8, f"only {gap:.0f}px between the two marks"

    # One row: their vertical ranges have to overlap, or it has wrapped.
    overlap = (min(en["bottom"], ar["bottom"])
               - max(en["y"], ar["y"]))
    assert overlap > 0, (
        f"the marks do not overlap vertically (en {en['y']:.0f}-"
        f"{en['bottom']:.0f}, ar {ar['y']:.0f}-{ar['bottom']:.0f}); the "
        f"row has wrapped onto two lines")
    assert overlap >= en["height"] * 0.8, (
        "the Latin mark is barely on the same line as the Arabic one")

    # The Arabic mark keeps its share of the lockup.
    ratio = ar["width"] / en["width"]
    expected = wordmark_sizes()["wm-ar-en-ratio"]
    assert abs(ratio - expected) < 0.02, (
        f"the Arabic mark is {ratio:.3f} of the Latin one, not the "
        f"artwork's {expected:.3f}")

    # It fits, with room, and the page does not scroll sideways.
    assert row["width"] <= box["avail"], (
        f"the row is {row['width']:.0f}px in {box['avail']:.0f}px of card")
    assert not box["overflow"], "the page scrolls sideways"

    # ...and the two text lines still sit under the row, not beside it.
    assert box["lead"]["y"] >= row["y"] + row["height"] - 1, (
        "the lead line is not below the lockup")


@needs_browser
@needs_wordmarks
def test_the_two_scripts_stand_on_one_baseline(live_server):
    """The marks are very different shapes - the Arabic one carries
    sparkles above its script and dots below - so centring the boxes is
    not the same as levelling the writing. `tools/brand_assets.py`
    measures each baseline and emits `--wm-ar-nudge` to close the gap;
    this checks the result on the rendered page.

    Without the nudge the Arabic script sat 16px low at 1080p, which is
    10% of its own height and reads as the word sagging.
    """
    from io import BytesIO

    from playwright.sync_api import sync_playwright
    Image = pytest.importorskip("PIL.Image", reason="Pillow is not installed")

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server, "?card=1") as (_, page):
                page.wait_for_timeout(300)
                boxes = page.evaluate("""() => {
                  const q = s => { const r = document.querySelector(s)
                                     .getBoundingClientRect();
                    return {x:r.x, y:r.y, w:r.width, h:r.height}; };
                  return {en: q('.titlecard-wordmark.en'),
                          ar: q('.titlecard-wordmark.ar')};
                }""")
                shot = page.screenshot(path=None)
        finally:
            browser.close()

    image = Image.open(BytesIO(shot)).convert("RGB")
    pixels = image.load()

    def baseline(box):
        """Page y of the bottom of the mark's densest ink band."""
        x0, y0 = int(box["x"]), int(box["y"])
        x1, y1 = int(box["x"] + box["w"]), int(box["y"] + box["h"])
        rows = [(y, sum(1 for x in range(x0, x1) if max(pixels[x, y]) > 90))
                for y in range(y0, y1)]
        peak = max(n for _, n in rows)
        return [y for y, n in rows if n >= peak * 0.25][-1]

    delta = baseline(boxes["ar"]) - baseline(boxes["en"])
    assert abs(delta) <= 4, (
        f"the two scripts sit {delta:+d}px apart; they should stand on one "
        f"line. Re-run tools/brand_assets.py if the artwork changed")


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_the_projector_paints_one_black_and_no_other(live_server):
    """What the room sees, not what the stylesheet says.

    **With the background glow off**, which is what this rule is about
    now. `?glow=0` pins it down the same way `?logo=` pins the corner
    marks, so this measures the plain caption view whatever the operator
    last saved. Glow on is held by the contrast floor below instead - two
    soft washes are the point there, and one surface colour is not.

    `tests/test_brand.py` checks no second near-black is *declared*; this
    checks none is *painted*, which is the thing that matters and can come
    from somewhere no declaration names.

    One unit of tolerance, and it is spent: the stream's top fade is a
    `mask-image`, and compositing that masked layer over the page rounds the
    surface colour down by one across the fade band - about 5% of the screen.
    It is 1/255, invisible at any distance, and removing it would mean
    giving up the fade that keeps the clipped top line from looking broken.
    Anything wider than that is a real second surface and should fail here.
    """
    from io import BytesIO

    from playwright.sync_api import sync_playwright
    Image = pytest.importorskip("PIL.Image", reason="Pillow is not installed")

    raw = re.search(r"--bg-brand:\s*#([0-9a-fA-F]{6})", STYLE).group(1)
    brand = tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server, "?logo=1&glow=0") as (_, page):
                # Paced, and screenshotted while it is still running. The
                # unpaced `fill()` pushes the whole capture through and the
                # session then *ends*, which puts the title card back up -
                # and the card is one flat `--bg-brand` covering everything,
                # so the test would pass without sampling a single pane.
                admin(live_server, "/admin/start", capture=CAPTURE,
                      paced="true")
                try:
                    page.wait_for_function(
                        "() => document.querySelectorAll("
                        "'#screen-auto .auto-para').length > 2",
                        timeout=40000)
                    page.evaluate("() => window.CaptionScreen.finishReveal()")
                    page.wait_for_timeout(200)
                    assert not page.evaluate(
                        "() => window.CaptionScreen.carded()"), (
                        "the title card is up, so no pane is on screen and "
                        "this test is measuring the card")
                    shot = page.screenshot(path=None)
                finally:
                    admin(live_server, "/admin/stop")
        finally:
            browser.close()

    image = Image.open(BytesIO(shot)).convert("RGB")
    pixels = image.load()
    strays = {}
    for y in range(0, image.height, 2):
        for x in range(0, image.width, 2):
            pixel = pixels[x, y]
            # Only surfaces: text, the mark and the accent are not blacks.
            if max(pixel) > 40:
                continue
            if max(abs(c - b) for c, b in zip(pixel, brand)) > 1:
                strays[pixel] = strays.get(pixel, 0) + 1

    sampled = (image.width // 2) * (image.height // 2)
    for pixel, count in sorted(strays.items(), key=lambda kv: -kv[1])[:1]:
        share = count / sampled
        assert share < 0.01, (
            f"#{pixel[0]:02X}{pixel[1]:02X}{pixel[2]:02X} covers "
            f"{share:.1%} of the projector and is not the brand black "
            f"#{raw.upper()}; /screen paints one surface colour")


#: Everything that is ink on `/screen`, for the tests that want to look at
#: the surfaces alone. The element boxes stay where they are - only what is
#: drawn inside them goes - so anything left that is not smooth is a real
#: edge between two surfaces rather than a letterform.
#:
#: A **stylesheet**, not inline styles on the nodes that happen to exist.
#: These tests hold a paced replay open, so paragraphs keep arriving: one
#: that landed between the `querySelectorAll` and the screenshot stayed
#: visible and read as a cliff in the middle of the glow. That is a flake
#: that passes alone and fails in a full run, which is the worst kind. A
#: rule applies to whatever is on the page when the pixels are taken.
STRIP_INK = """() => {
  const style = document.createElement('style');
  style.id = 'strip-ink';
  style.textContent =
    '#screen-auto .auto-para, #screen-en p, #screen-ar p,'
    + ' #screenmark img, .screenfooter span,'
    + ' #listening { visibility: hidden !important; }';
  document.head.appendChild(style);
}"""


def pixel_luminance(pixel) -> float:
    """WCAG relative luminance of an `(r, g, b)` triple off a screenshot.

    `contrast` near the top of this file takes the hex strings written in
    the stylesheet. These two take what a pixel actually came out as, which
    is the only way to ask a question about a gradient - and they are named
    apart from it rather than overloading one helper, because a `contrast`
    that silently accepted both would read a tuple as a string one day.
    """
    red, green, blue = (_channel(c) for c in pixel)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def pixel_contrast(one, other) -> float:
    first, second = pixel_luminance(one), pixel_luminance(other)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
@pytest.mark.parametrize("width,height", [(1920, 1080), (1280, 720)])
def test_the_corner_marks_stand_at_both_ends_of_their_band(live_server,
                                                           width, height):
    """The caption view carries the branding the poster does:
    the Latin mark at the left end and the Arabic one at the right, which
    is how they sit at the ends of the waiting screen's rule.

    Four things, and the last is the one that matters on a projector:

    * the ends, and mirrored margins - neither mark nearer its edge;
    * both centred in the band, with the Arabic mark clear of the top -
      it is 2.2x the Latin one's height, and centring the boxes alone
      crowded it against the edge on the poster;
    * one baseline, measured off the rendered pixels the way the title
      card's is, because `--wm-ar-nudge` is the only thing holding it;
    * nothing painted above the band's bottom edge inside the pane, so a
      caption can never arrive under either mark.
    """
    from io import BytesIO

    from playwright.sync_api import sync_playwright
    Image = pytest.importorskip("PIL.Image", reason="Pillow is not installed")

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server, "?logo=1",
                           width=width, height=height) as (_, page):
                with mid_talk(page, live_server):
                    seen = page.evaluate("""() => {
                      const q = s => { const r = document.querySelector(s)
                                         .getBoundingClientRect();
                        return {x:r.x, y:r.y, w:r.width, h:r.height,
                                right:r.right, bottom:r.bottom}; };
                      return {band: q('#screenmark'),
                              en: q('#screenmark .titlecard-wordmark.en'),
                              ar: q('#screenmark .titlecard-wordmark.ar'),
                              pane: q('#screen-auto')};
                    }""")
                    shot = page.screenshot(path=None)
        finally:
            browser.close()

    band, en, ar, pane = (seen[k] for k in ("band", "en", "ar", "pane"))

    # The ends of the band, and the same air outside each.
    assert abs(en["x"] - band["x"]) < 1, (
        f"the Latin mark starts {en['x'] - band['x']:.0f}px inside the "
        f"band; it belongs at the end of it")
    assert abs(ar["right"] - band["right"]) < 1, (
        f"the Arabic mark stops {band['right'] - ar['right']:.0f}px short "
        f"of the right end of the band")
    assert abs(en["x"] - (width - ar["right"])) < 1, (
        f"margins are {en['x']:.0f}px left and "
        f"{width - ar['right']:.0f}px right; they should mirror")

    # Centred in the band, and the taller mark not crowding the top. The
    # Arabic artwork carries sparkles above its script, so "clear of the
    # edge" is about its box, which is what the padding re-centres.
    above, below = ar["y"] - band["y"], band["bottom"] - ar["bottom"]
    assert above > 0.1 * ar["h"], (
        f"the Arabic mark sits {above:.0f}px below the top of the band "
        f"and {below:.0f}px above the bottom; it is touching the edge")
    assert abs(above - below) <= max(3.0, 0.08 * ar["h"]), (
        f"the pair sits {above:.0f}px from the top and {below:.0f}px from "
        f"the bottom of its band; it is not centred")

    image = Image.open(BytesIO(shot)).convert("RGB")
    pixels = image.load()
    scale = image.width / width          # device pixel ratio, if any

    def baseline(box):
        """Page y of the bottom of this mark's densest ink band."""
        x0, y0 = int(box["x"] * scale), int(box["y"] * scale)
        x1 = int((box["x"] + box["w"]) * scale)
        y1 = int((box["y"] + box["h"]) * scale)
        rows = [(y, sum(1 for x in range(x0, x1) if max(pixels[x, y]) > 90))
                for y in range(y0, y1)]
        peak = max(n for _, n in rows)
        return [y for y, n in rows if n >= peak * 0.25][-1] / scale

    delta = baseline(ar) - baseline(en)
    assert abs(delta) <= 4, (
        f"the two corner marks sit {delta:+.0f}px apart; they should stand "
        f"on one line. Re-run tools/brand_assets.py if the artwork changed")

    # And the captions stay out of the band. The pane is what clips them,
    # so this is really "the band is reserved", checked on the pixels.
    assert pane["y"] >= band["bottom"] - 1, (
        f"the caption pane starts at {pane['y']:.0f}px, inside a band that "
        f"runs to {band['bottom']:.0f}px; a caption can reach the marks")
    top_strip = int((band["bottom"] - 1) * scale)
    ink = sum(1 for y in range(int(pane["y"] * scale), top_strip)
              for x in range(0, image.width, 3) if max(pixels[x, y]) > 90)
    assert ink == 0, f"{ink} caption pixels painted inside the corner band"


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
@pytest.mark.parametrize("width,height", [(1920, 1080), (1280, 720)])
def test_the_glow_never_dims_a_caption_below_twelve_to_one(live_server,
                                                           width, height):
    """The caption view carries the poster's two lights now, and the words
    are still the content. Measured on the rendered projector rather than
    on the stylesheet: what a room reads is the text against whatever is
    actually behind it, and the alpha in a gradient does not say what that
    comes to once two of them overlap.

    The ground is sampled with the captions **hidden**, because the dark
    half of the text's own antialiasing reads as a bright background
    otherwise - the first version of this measured 10:1 and was looking at
    the edges of letters.

    12:1 is the floor this project sets. Flat black gives 19.8:1, so the
    glow is
    allowed to spend about half the headroom there and no more.
    """
    from io import BytesIO

    from playwright.sync_api import sync_playwright
    Image = pytest.importorskip("PIL.Image", reason="Pillow is not installed")

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server, "?logo=1&glow=1",
                           width=width, height=height) as (_, page):
                with mid_talk(page, live_server):
                    assert page.evaluate(
                        "() => window.CaptionScreen.glow()"), (
                        "the glow is off, so this measures nothing")
                    seen = page.evaluate("""() => {
                      const pane = document.getElementById('screen-auto')
                                     .getBoundingClientRect();
                      return {
                        pane: [pane.left, pane.top, pane.right, pane.bottom],
                        colour: getComputedStyle(document.querySelector(
                          '#screen-auto .auto-para')).color,
                        lines: [...document.querySelectorAll(
                          '#screen-auto .auto-para')].map(p => {
                            const b = p.getBoundingClientRect();
                            return [b.left, b.top, b.right, b.bottom]; }),
                      };
                    }""")
                    page.evaluate(STRIP_INK)
                    page.wait_for_timeout(120)
                    ground = page.screenshot(path=None)
        finally:
            browser.close()

    image = Image.open(BytesIO(ground)).convert("RGB")
    pixels = image.load()
    scale = image.width / width
    text = tuple(int(v) for v in
                 seen["colour"].removeprefix("rgb(").rstrip(")").split(","))
    pane_left, pane_top, pane_right, pane_bottom = seen["pane"]

    worst = None
    for index, (left, top, right, bottom) in enumerate(seen["lines"]):
        # Only what the room can see: the pane clips whatever runs past it,
        # and a line scrolled off the top has no background to measure.
        left, top = max(left, pane_left), max(top, pane_top + 1)
        right, bottom = min(right, pane_right), min(bottom, pane_bottom)
        if bottom <= top or right <= left:
            continue
        brightest = max(
            (pixels[x, y]
             for y in range(int(top * scale), int(bottom * scale))
             for x in range(int(left * scale), int(right * scale))),
            key=pixel_luminance)
        ratio = pixel_contrast(text, brightest)
        if worst is None or ratio < worst[0]:
            worst = (ratio, brightest, index)

    assert worst is not None, "no caption line was on screen to measure"
    ratio, pixel, index = worst
    assert ratio >= 12.0, (
        f"caption line {index} runs over "
        f"#{pixel[0]:02X}{pixel[1]:02X}{pixel[2]:02X}, which is only "
        f"{ratio:.1f}:1 against the caption colour; the glow is too strong")


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_the_glow_runs_under_every_band_without_an_edge(live_server):
    """One wash, not a wash with the chrome cut out of it.

    Three surfaces used to paint the brand black on top of the glow - the
    panes, the footer strip and `main` itself - and each left a flat
    rectangle in the middle of it. The footer's was the worst: a bar the
    full width of the screen along the bottom, exactly where the warm
    glow is
    brightest.

    Measured with every mark of ink hidden and the element boxes left
    where they are, so what is sampled is only the surfaces. A gradient
    moves by at most a step or two per row; a surface boundary is a cliff.
    """
    from io import BytesIO

    from playwright.sync_api import sync_playwright
    Image = pytest.importorskip("PIL.Image", reason="Pillow is not installed")

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server,
                           "?logo=1&glow=1") as (_, page):
                with mid_talk(page, live_server):
                    # The bands this has to cross, so a failure can say
                    # which boundary it found.
                    edges = page.evaluate("""() => ({
                      band: document.getElementById('screenmark')
                              .getBoundingClientRect().bottom,
                      footer: document.getElementById('screenfooter')
                                .getBoundingClientRect().top,
                    })""")
                    page.evaluate(STRIP_INK)
                    page.wait_for_timeout(120)
                    shot = page.screenshot(path=None)
        finally:
            browser.close()

    image = Image.open(BytesIO(shot)).convert("RGB")
    pixels = image.load()

    cliffs = []
    for column in range(6):
        x = min(image.width - 1, int(image.width * (0.05 + column * 0.18)))
        previous = None
        for y in range(image.height):
            pixel = pixels[x, y]
            if previous is not None:
                step = max(abs(a - b) for a, b in zip(pixel, previous))
                if step > 2:
                    cliffs.append((x, y, previous, pixel, step))
            previous = pixel

    assert not cliffs, (
        f"{len(cliffs)} surface edges in the glow; the first is at "
        f"x={cliffs[0][0]} y={cliffs[0][1]}, {cliffs[0][2]} -> "
        f"{cliffs[0][3]} (corner band ends at {edges['band']:.0f}, the "
        f"footer starts at {edges['footer']:.0f})")

    # ...and the wash is actually there, or the test above passes on a
    # perfectly smooth flat black.
    levels = [pixel_luminance(pixels[x, y])
              for y in range(0, image.height, 7)
              for x in range(0, image.width, 7)]
    spread = max(levels) / max(min(levels), 1e-9)
    assert spread > 1.5, (
        f"the brightest surface is only {spread:.2f}x the darkest; the "
        f"screen is uniform, so there is no glow to be continuous")


@needs_browser
def test_the_screen_waits_with_the_poster(live_server):
    """UX rule 4, before anything has ever been live.

    The waiting screen stands in here now, not the title card: a room that
    is filling up is looking at the poster with the join code on it. The
    card is for the gap *inside* a running session, which is a different
    thing to say and is checked separately.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                assert page.evaluate("() => window.CaptionScreen.waiting()")
                assert not page.evaluate(
                    "() => window.CaptionScreen.carded()"), (
                    "the title card is up before the talk; the waiting "
                    "screen is what belongs there")
                # Whitespace-insensitive: the headline's last word is its
                # own element so that it can carry the accent, and a wrap
                # between the two puts a newline in `inner_text`.
                text = " ".join(page.inner_text("#waitscreen").split())
                assert "Captions start shortly." in text
                # **Not bilingual, and that is the design.** The Arabic
                # welcome line came off on 2026-10-19: the header's Arabic
                # wordmark carries the second script here, and the
                # bilingual promise is made by the title card and the
                # footer strip - the two things the room reads once the
                # talk starts, which is where it matters.
                assert not any("؀" <= c <= "ۿ" for c in text), (
                    "the waiting screen has grown an Arabic line again")
                # ...and no error anywhere on the projector.
                assert "error" not in page.inner_text("body").lower()
        finally:
            browser.close()


@needs_browser
def test_the_bilingual_promise_is_made_by_the_card_and_the_footer(live_server):
    """Where "both scripts" is checked now that the poster is English only.

    These two are what a room actually reads: the card stands in during
    the gap after Start, and the strip runs along the bottom all talk. If
    either loses its Arabic, the audience is being told in one language
    that they are getting two.
    """
    from playwright.sync_api import sync_playwright

    def arabic(text):
        return any("؀" <= c <= "ۿ" for c in text)

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server, "?card=1") as (_, page):
                card = page.inner_text("#titlecard")
                assert "Live translation" in card
                assert arabic(card), "the title card is not bilingual"
                footer = page.inner_text("#screenfooter")
                assert arabic(footer), "the footer strip is not bilingual"
                assert "mistakes" in footer.lower()
                # ...and the disclaimer is alone in there. Read off the
                # rendered page rather than the markup, because a rule
                # elsewhere could put a background image in the band.
                images = page.evaluate(
                    "() => document.getElementById('screenfooter')"
                    "        .querySelectorAll('img, svg, picture').length")
                assert images == 0, (
                    f"the footer strip rendered {images} image(s); it is "
                    f"text only since 2026-10-20")
        finally:
            browser.close()


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_the_screen_defaults_to_auto_and_shows_only_translation(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                assert page.evaluate("() => window.CaptionScreen.view()") == "auto"
                fill(page, live_server)
                shown = page.evaluate(
                    "() => [...document.querySelectorAll('#screen-auto .auto-para')]"
                    "        .map(p => p.getAttribute('dir'))")
                assert shown, "nothing on the screen"
                assert set(shown) <= {"ltr", "rtl"}
                # Both directions really are present in this capture.
                assert len(set(shown)) == 2, (
                    "the dry run has English and Arabic speech; the screen "
                    "showed only one direction")
                # Not asserting the title card here: `paced=false` runs the
                # capture out in under a second, so by now the session has
                # genuinely ended and the card belongs back on screen. What
                # this test is about is that the captions underneath are
                # translation only, which the directions above prove.
        finally:
            admin(live_server, "/admin/stop")
            browser.close()


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_a_direction_change_gets_a_separator_and_nothing_else_does(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                fill(page, live_server)
                marks = page.evaluate("""() =>
                  [...document.querySelectorAll('#screen-auto .auto-para')]
                    .map(p => ({dir: p.getAttribute('dir'),
                                turn: p.classList.contains('turn')}))""")
                assert marks[0]["turn"] is False, "the first block is not a turn"
                for before, after in zip(marks, marks[1:]):
                    assert after["turn"] == (after["dir"] != before["dir"]), (
                        f"separator disagrees with the direction change: "
                        f"{before} -> {after}")
                assert any(m["turn"] for m in marks), "no direction change seen"
        finally:
            admin(live_server, "/admin/stop")
            browser.close()


@needs_browser
def test_the_operator_can_move_every_screen_without_touching_it(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                admin(live_server, "/admin/screen", view="ar", size="xl")
                page.wait_for_function(
                    "() => document.body.dataset.view === 'ar'", timeout=10000)
                assert page.evaluate("() => document.body.dataset.size") == "xl"

                admin(live_server, "/admin/screen", view="auto", size="s")
                page.wait_for_function(
                    "() => document.body.dataset.view === 'auto'", timeout=10000)
                assert page.evaluate("() => document.body.dataset.size") == "s"
        finally:
            admin(live_server, "/admin/screen",
                  view=DEFAULT_SCREEN["view"], size=DEFAULT_SCREEN["size"])
            browser.close()


@needs_browser
def test_a_url_pins_a_screen_against_the_operator(live_server):
    """The testing hatch: `?lang=` must keep winning, not just win once."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server, "?lang=en&size=m") as (_, page):
                admin(live_server, "/admin/screen", view="auto", size="xl")
                page.wait_for_timeout(800)
                assert page.evaluate("() => document.body.dataset.view") == "en"
                assert page.evaluate("() => document.body.dataset.size") == "m"
        finally:
            admin(live_server, "/admin/screen",
                  view=DEFAULT_SCREEN["view"], size=DEFAULT_SCREEN["size"])
            browser.close()


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_the_screen_keeps_its_text_when_the_session_stops(live_server):
    """UX rule 4: silence must not clear the room's last line."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                fill(page, live_server)
                before = page.inner_text("#screen-auto")
                admin(live_server, "/admin/stop")
                page.wait_for_timeout(1500)
                # The waiting screen returns after Stop, but it *covers*
                # the captions: the text is still underneath, so a session
                # that resumes does not start from a blank screen.
                assert page.inner_text("#screen-auto") == before, (
                    "stopping the session wiped the screen")
                assert page.evaluate("() => window.CaptionScreen.waiting()"), (
                    "nothing is live and the screen shows no waiting screen")
        finally:
            browser.close()


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_a_reconnect_shows_a_dot_and_no_words(live_server):
    """UX rule 5: at most a tiny dot in a corner."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (context, page):
                fill(page, live_server)
                text_before = page.inner_text("#screen-auto")

                context.set_offline(True)
                page.wait_for_function(
                    "() => !document.getElementById('linkdot').hidden",
                    timeout=30000)

                body = page.inner_text("body")
                for word in ("reconnect", "error", "disconnected", "offline"):
                    assert word not in body.lower(), (
                        f"the projector is showing the word {word!r}")
                # The captions stay up through the outage.
                assert page.inner_text("#screen-auto") == text_before

                dot = page.evaluate("""() => {
                  const d = document.getElementById('linkdot');
                  const b = d.getBoundingClientRect();
                  return {w: b.width, h: b.height,
                          right: window.innerWidth - b.right,
                          top: b.top};
                }""")
                assert dot["w"] < 40 and dot["h"] < 40, "that is not a tiny dot"
                assert dot["right"] < 120 and dot["top"] < 120, "not in a corner"

                context.set_offline(False)
                page.wait_for_function(
                    "() => document.getElementById('linkdot').hidden",
                    timeout=30000)
        finally:
            admin(live_server, "/admin/stop")
            browser.close()


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_the_reveal_finishes_quickly_and_does_not_paint_in_blocks(live_server):
    """UX rule 2, measured: text must arrive in pieces, then settle fast."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                page.evaluate("""() => {
                  window.__lengths = [];
                  const pane = document.getElementById('screen-auto');
                  new MutationObserver(() => {
                    window.__lengths.push(pane.textContent.length);
                  }).observe(pane, {subtree: true, childList: true,
                                    characterData: true});
                }""")
                admin(live_server, "/admin/start", capture=CAPTURE,
                      paced="true")
                page.wait_for_function(
                    "() => document.querySelectorAll('#screen-auto .auto-para')"
                    "        .length > 1", timeout=40000)
                page.wait_for_timeout(4000)
                admin(live_server, "/admin/stop")

                steps = page.evaluate("() => window.__lengths")
                assert len(steps) > 10, (
                    f"the pane was repainted only {len(steps)} times; text is "
                    f"landing in blocks rather than word by word")
                grows = [b - a for a, b in zip(steps, steps[1:]) if b > a]
                assert grows, "the text never grew"
                typical = sorted(grows)[len(grows) // 2]
                assert typical < 25, (
                    f"the median repaint added {typical} characters, which is "
                    f"a chunk, not a word")

                # And once the audio stops, it settles rather than dribbling.
                page.wait_for_timeout(1500)
                assert page.evaluate(
                    "() => window.CaptionScreen"
                    "      && document.querySelectorAll('#screen-auto').length === 1")
                settled_a = page.inner_text("#screen-auto")
                page.wait_for_timeout(900)
                assert page.inner_text("#screen-auto") == settled_a, (
                    "the reveal is still running well after the audio stopped")
        finally:
            with contextlib.suppress(Exception):
                admin(live_server, "/admin/stop")
            browser.close()


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_a_page_joining_mid_talk_still_reaches_the_listening_state(live_server):
    """The regression for the hard-flush bug, in a real browser.

    A page that connects while a session is already running is handed one
    big snapshot, which is far more text than the reveal can pace inside
    `MAX_LAG_MS`. It takes the flush path on its very first frame, and used
    to leave the loop stopped there.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            admin(live_server, "/admin/start", capture=CAPTURE, paced="true")
            # Let a decent backlog build before the page ever connects.
            time.sleep(12)
            with projector(browser, live_server) as (_, page):
                assert page.evaluate(
                    "() => document.querySelectorAll("
                    "       '#screen-auto .auto-para').length") > 0, (
                    "the joining page got no snapshot")
                page.wait_for_function(
                    "() => !document.getElementById('listening').hidden",
                    timeout=20000)
        finally:
            admin(live_server, "/admin/stop")
            browser.close()


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_the_listening_dots_show_while_waiting_for_translation(live_server):
    """UX rule 1: live, nothing left to reveal, so the dots are up."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                admin(live_server, "/admin/start", capture=CAPTURE,
                      paced="true")
                page.wait_for_function(
                    "() => !document.getElementById('listening').hidden",
                    timeout=30000)
                assert not page.evaluate(
                    "() => window.CaptionScreen.carded()"), (
                    "the dots and the title card are both up")
                # They are not permanent: text arriving puts them away.
                page.wait_for_function(
                    "() => document.getElementById('listening').hidden",
                    timeout=30000)
        finally:
            admin(live_server, "/admin/stop")
            browser.close()


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_nothing_above_the_newest_line_moves_as_it_grows(live_server):
    """UX rule 3, measured: the block above the newest one must hold still."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                admin(live_server, "/admin/start", capture=CAPTURE,
                      paced="true")
                page.wait_for_function(
                    "() => document.querySelectorAll('#screen-auto .auto-para')"
                    "        .length > 2", timeout=40000)

                def rows():
                    return page.evaluate("""() =>
                      [...document.querySelectorAll('#screen-auto .auto-para')]
                        .filter(p => !p.hidden)
                        .map(p => Math.round(p.getBoundingClientRect().bottom))""")

                # Bottom anchoring means finished blocks *do* move up as the
                # newest one grows - that is rule 3's "older text moves up".
                # What must not happen is a *reflow*: they have to travel
                # together, as one rigid stack, rather than re-wrapping or
                # drifting apart. So the test is that every displaced block
                # moved by the same amount and kept its own height.
                def geometry():
                    return page.evaluate("""() =>
                      [...document.querySelectorAll('#screen-auto .auto-para')]
                        .filter(p => !p.hidden)
                        .map(p => {
                          const b = p.getBoundingClientRect();
                          return {bottom: Math.round(b.bottom),
                                  height: Math.round(b.height)};
                        })""")

                before = geometry()
                page.wait_for_timeout(700)
                after = geometry()

                # Compare the blocks that existed both times, newest excluded.
                common = min(len(before), len(after)) - 1
                if common > 0:
                    old_rows = before[len(before) - 1 - common:len(before) - 1]
                    new_rows = after[len(after) - 1 - common:len(after) - 1]
                    shifts = {n["bottom"] - o["bottom"]
                              for o, n in zip(old_rows, new_rows)}
                    # One pixel of spread is `Math.round` landing either side
                    # of a fractional `vh` boundary, not a reflow: a block
                    # that actually re-wrapped would move by a line height.
                    assert max(shifts) - min(shifts) <= 1, (
                        f"finished blocks moved by different amounts {shifts}; "
                        f"the text above the newest line is reflowing")
                    reflowed = [(o["height"], n["height"])
                                for o, n in zip(old_rows, new_rows)
                                if o["height"] != n["height"]]
                    assert not reflowed, (
                        f"a finished block changed height: {reflowed[:3]}")
                    assert all(s <= 0 for s in shifts), (
                        f"older text moved *down* by {shifts}")
        finally:
            admin(live_server, "/admin/stop")
            browser.close()


@needs_browser
def test_the_operator_toggles_reach_an_open_screen(live_server):
    """Footer, direction labels and the corner mark, pushed down the socket
    the projector already has."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                admin(live_server, "/admin/screen", footer="false",
                      labels="false", logo="true")
                page.wait_for_function(
                    "() => document.body.dataset.footer === '0'", timeout=10000)
                assert page.evaluate("() => document.body.dataset.labels") == "0"
                assert page.evaluate("() => document.body.dataset.logo") == "1"
                assert page.evaluate(
                    "() => document.getElementById('screenfooter').hidden")

                admin(live_server, "/admin/screen", footer="true",
                      labels="true", logo="false")
                page.wait_for_function(
                    "() => document.body.dataset.footer === '1'", timeout=10000)
                assert not page.evaluate(
                    "() => document.getElementById('screenfooter').hidden")
        finally:
            admin(live_server, "/admin/screen", footer="true", labels="true",
                  logo="false")
            browser.close()


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_the_footer_never_sits_on_a_caption(live_server):
    """Measured: the captions stop above the band, however long they run."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                admin(live_server, "/admin/screen", footer="true")
                page.wait_for_function(
                    "() => document.body.dataset.footer === '1'", timeout=10000)
                fill(page, live_server)

                box = page.evaluate("""() => {
                  const f = document.getElementById('screenfooter');
                  const paras = [...document.querySelectorAll(
                      '#screen-auto .auto-para')].filter(p => !p.hidden);
                  const last = paras[paras.length - 1];
                  return {
                    footerTop: Math.round(f.getBoundingClientRect().top),
                    textBottom: Math.round(last.getBoundingClientRect().bottom),
                    paneBottom: Math.round(document.getElementById('screen-auto')
                                  .getBoundingClientRect().bottom),
                  };
                }""")
                assert box["textBottom"] <= box["footerTop"], (
                    f"captions reach {box['textBottom']}px, the footer starts "
                    f"at {box['footerTop']}px")
                assert box["paneBottom"] <= box["footerTop"] + 2, (
                    "the caption pane overlaps the footer band")
        finally:
            browser.close()


@needs_capture('live-2026-10-17-011114.jsonl')
@needs_browser
def test_a_direction_change_is_labelled_on_the_projector(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with projector(browser, live_server) as (_, page):
                fill(page, live_server)
                turns = page.evaluate("""() =>
                  [...document.querySelectorAll('#screen-auto .auto-para.turn')]
                    .map(p => p.dataset.turn)""")
                assert turns, "the capture produced no direction change"
                for label in turns:
                    assert label in ("EN \u2192 AR", "AR \u2192 EN"), label
                # And the label is actually drawn, not just recorded.
                drawn = page.evaluate("""() => {
                  const p = document.querySelector('#screen-auto .auto-para.turn');
                  return getComputedStyle(p, '::before').content;
                }""")
                assert "EN" in drawn or "AR" in drawn, (
                    f"the label is not rendered: {drawn}")
        finally:
            admin(live_server, "/admin/stop")
            browser.close()


# ---------------------------------------------------------------------------
# Sizes, judged from the back of a room
# ---------------------------------------------------------------------------
#
# Everything on the projector was first built at sizes that read fine on a
# laptop and disappeared at 1920x1080 across a hall. These pin the numbers in
# `vh`, which is what "a share of the screen height" means, and 1vh is 10.8px
# at 1080p - so 1.9vh is about 20px.


def vh(block: str, prop: str) -> float:
    """The `vh` term of one declaration.

    It looks inside the value rather than only at its start, because a size
    that is capped against the artwork's native pixels is written
    `min(5.5vh, 73px)` - still "5.5% of the screen height" on any projector
    where the cap is not the binding term, which at 1080p is all of them.
    """
    declaration = re.search(rf"{prop}:\s*([^;]+);", block)
    assert declaration, f"{prop} is not set: {block}"
    match = re.search(r"([\d.]+)vh", declaration.group(1))
    assert match, (
        f"{prop} is not expressed in vh: {declaration.group(1).strip()}")
    return float(match.group(1))


def test_the_footer_text_is_big_enough_to_read_across_a_room():
    block = STYLE.split(".screenfooter {", 1)[1].split("}", 1)[0]
    size = vh(block, "font-size")
    assert size >= 1.8, f"footer text is {size}vh, about {size * 10.8:.0f}px"
    # Tied to the screen, not to `--screen-size`: the disclaimer has to stay
    # readable whatever the operator does to the caption size.
    assert "--screen-size" not in block.split("font-size")[1][:60]


def test_the_footer_band_is_tall_enough_for_what_is_in_it():
    """One line of type, with room above and below it.

    The band used to have to clear a 2.3vh wordmark as well; it came off on
    2026-10-20 and the band kept its height, so the disclaimer now sits in
    a band with more air than it strictly needs. That is the right way
    round - shrinking it would move the captions down onto it.
    """
    root = STYLE.split("body.screen {", 1)[1].split("\n}", 1)[0]
    band = vh(root, "--footer-h")
    block = STYLE.split(".screenfooter {", 1)[1].split("}", 1)[0]
    text = vh(block, "font-size")
    line = float(re.search(r"line-height:\s*([\d.]+)", block).group(1)) \
        if "line-height:" in block else 1.2
    assert band >= text * line + 2, (
        f"{text}vh text does not fit in a {band}vh band without touching "
        f"the captions above it")


def test_the_footer_carries_no_mark_at_all():
    """Removed on 2026-10-20 at the organiser's request.

    `/screen` already signs itself twice - the corner lockup across the top
    and the title card between talks - and the one band whose whole job is
    a disclaimer was the place a third mark could come off. Asserted on the
    markup *and* the stylesheet, because either one alone would let it drift
    back: a rule with nothing to style is invisible in review, and an `<img>`
    with no rule would still render at its natural size.
    """
    footer = SCREEN_HTML.split('id="screenfooter"', 1)[1].split("</div>", 1)[0]
    assert "<img" not in footer, (
        f"the footer strip carries an image again: {footer.strip()}")
    for mark in ("wordmark-en.png", "wordmark-ar.png", "logo.png"):
        assert mark not in footer, f"the footer references {mark}"
    assert ".screenfooter img" not in re.sub(r"/\*.*?\*/", "", STYLE,
                                            flags=re.S), (
        "style.css still has a rule for an image in the footer")


def test_the_corner_mark_is_large_enough_to_recognise():
    """Sized by height: these are wordmarks, 5.3x wider than they are tall,
    so height is what decides legibility. 3.5vh is 38px at 1080p against
    the footer's 20px text - the mark still leads, but 5.5vh made a 59px
    banner of it and took 10.5vh of band away from the captions to do so.

    The Latin mark's height is still the knob, unchanged from when it
    stood in the corner alone; the Arabic mark beside it follows from the
    artwork's own ratios, and the band is what has to grow for it.
    """
    block = STYLE.split(".screenmark {", 1)[1].split("}", 1)[0]
    mark = vh(block, "--screenmark-h")
    footer_text = vh(STYLE.split(".screenfooter {", 1)[1].split("}", 1)[0],
                     "font-size")
    assert mark >= 3.0, (
        f"the corner mark is {mark}vh, about {mark * 10.8:.0f}px at 1080p; "
        f"too small to read from the back")
    assert mark >= 1.5 * footer_text, (
        f"the corner mark is {mark}vh against {footer_text}vh footer text; "
        f"it is the brand, not a disclaimer")
    # It is the artwork, not a tile: the PNGs carry their own transparency,
    # which is what lets the glow show through the letterforms.
    assert "background: none" in block


def test_the_corner_band_is_reserved_from_the_taller_mark():
    """The band has to clear the **Arabic** mark, which is 2.2x the Latin
    one's height, plus the padding that re-centres the pair after the
    baseline nudge lifts it. One knob, `--logo-band`, used by the band
    itself and by the padding that takes it out of `main`, so the two
    cannot drift apart - the same arrangement as `--footer-h`.

    Computed from the generated artwork numbers rather than written down,
    so a larger master that needs a taller band fails here instead of
    quietly landing on half-faded captions.
    """
    sizes = wordmark_sizes()

    def number(name):
        assert name in sizes, f"--{name} is defined nowhere"
        return sizes[name]

    screen = re.search(r"body\.screen \{(.*?)\n\}", STYLE, re.S).group(1)
    band = vh(screen, "--logo-band")
    block = STYLE.split(".screenmark {", 1)[1].split("}", 1)[0]
    latin_h = vh(block, "--screenmark-h")

    # The lockup is driven as a width; the Arabic mark is a share of it.
    lockup = latin_h * number("wm-en-w") / number("wm-en-h")
    arabic_h = (lockup * number("wm-ar-en-ratio")
                / (number("wm-ar-w") / number("wm-ar-h")))
    padding = lockup * number("wm-ar-nudge") * 2
    needs = arabic_h + padding

    assert band >= needs + 1.5, (
        f"the band is {band}vh and the lockup in it is {needs:.1f}vh "
        f"({arabic_h:.1f}vh of Arabic artwork plus {padding:.1f}vh of "
        f"re-centring padding); it will land on half-faded captions")
    rule = STYLE.split('body.screen[data-logo="1"] main {',
                       1)[1].split("}", 1)[0]
    reserved = re.search(r"padding-top:\s*([^;]+);", rule).group(1).strip()
    assert reserved == "var(--logo-band)", (
        f"`main` reserves `{reserved}` rather than the band's own knob; "
        f"the two will drift")


def test_the_corner_marks_are_both_wordmarks_not_the_stacked_tile():
    """Both scripts or neither. A projector showing half a bilingual
    lockup looks like a page that failed to load, not like a choice, so
    there is one switch and both marks are in the markup."""
    mark = SCREEN_HTML.split('id="screenmark"', 1)[1].split("</span>", 1)[0]
    assert "/brand/wordmark-en.png" in mark
    assert "/brand/wordmark-ar.png" in mark
    assert "/brand/mark.png" not in mark
    # Same guard the title card carries: an RTL ancestor would otherwise
    # swap which mark lands at which end of the band.
    assert 'dir="ltr"' in mark
    # No width/height attributes - the artwork's size lives in one place.
    assert "width=" not in mark and "height=" not in mark


def test_the_title_card_lockup_is_as_large_as_the_artwork_allows():
    """`min(40vw, var(--wm-en-w) * 1px)` - two terms, and which one binds
    depends on the artwork, which is the whole design.

    With today's 389px master the cap binds: the card is drawn at exactly
    native resolution, about a fifth of a 1080p screen, and cannot be made
    bigger without interpolating. With artwork three times the size the
    40vw term binds instead and the card is 768px - bigger, still never
    upscaled. Both are correct, so what is asserted is the property that
    has to hold either way: the card is a hero at 1080p, and it never
    exceeds the pixels it actually has.
    """
    value = STYLE.split("--title-lockup:", 1)[1].split(";", 1)[0]
    native = wordmark_sizes()["wm-en-w"]
    requested = float(re.search(r"([\d.]+)vw", value).group(1))

    drawn = min(requested * 19.2, native)
    assert drawn >= 300, (
        f"the lockup is drawn {drawn:.0f}px wide at 1920 - {requested}vw "
        f"against {native:.0f}px of artwork; too small to lead the card")
    assert drawn <= native, (
        f"{drawn:.0f}px drawn from {native:.0f}px of artwork is an upscale")
    assert "var(--wm-en-w)" in value, "nothing stops it upscaling past native"


def declarations(block: str) -> str:
    """The rules in a block, with the comments taken out.

    Worth the helper: the first version of the test below matched the word
    "background" inside the comment explaining why there isn't one.
    """
    return re.sub(r"/\*.*?\*/", "", block, flags=re.S)


def test_the_title_card_lockup_sits_on_the_page_not_in_a_box():
    for selector in (".titlecard-lockup {", ".titlecard-wordmark {"):
        block = declarations(STYLE.split(selector, 1)[1].split("}", 1)[0])
        assert "border-radius" not in block, (
            f"{selector} still reads as a tile")
        assert "background" not in block
