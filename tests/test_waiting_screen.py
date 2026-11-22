"""The waiting screen: the poster the room sees before Start and after Stop.

Three things are being held in place here.

**The link is local.** It lives in the gitignored `config/screen.yaml`, it
reaches `/admin` behind its token and the pixels of a QR code, and it reaches
nothing else - not the viewer socket, not a page, not a log. That is the same
rule `config/session.yaml` follows and for the same reason: a WhatsApp invite
is an open door for anyone holding it, and must never be committed.

**The code has to scan.** Not "a QR element is present" - a decoder reads the
rendered page back and gets the URL out. The logo in the middle covers real
modules, so this is the test that says whether it may stay.

**Nothing overlaps.** At 1280x720, 1920x1080 and 3840x2160, measured in a
browser rather than read off the stylesheet, because a poster that works on a
laptop and collides on the venue projector is the failure this project has
already had once.

Every URL in this file is invented. The real one is never committed, never
printed and never screenshotted.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.broadcast import (  # noqa: E402
    DEFAULT_SCREEN,
    MAX_SCREEN_TEXT,
    MAX_SUBLINE_LINES,
    BroadcastHub,
)
from app.captions import CaptionState  # noqa: E402
from app.invite_qr import (  # noqa: E402
    DEFAULT_LINK,
    MAX_INVITE_CHARS,
    clean_invite,
    render,
    resolve,
    tag,
)
from repo import needs_git  # noqa: E402
from captures import needs_wordmarks  # noqa: E402

WEB_DIR = ROOT / "web"
STYLE = (WEB_DIR / "style.css").read_text(encoding="utf-8")
SCREEN_HTML = (WEB_DIR / "screen.html").read_text(encoding="utf-8")
SCREEN_JS = (WEB_DIR / "screen.js").read_text(encoding="utf-8")
ADMIN_HTML = (WEB_DIR / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (WEB_DIR / "admin.js").read_text(encoding="utf-8")

#: Invented. A group that does not exist, with an invite code that is not one.
SAMPLE_INVITE = "https://chat.whatsapp.com/SAMPLEINVITECODE00000"
OTHER_INVITE = "https://chat.whatsapp.com/ADIFFERENTSAMPLE00000"


# ---------------------------------------------------------------------------
# The link
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [
    SAMPLE_INVITE,
    "http://example.invalid/join",
    "  https://chat.whatsapp.com/SPACED  ",
])
def test_a_join_link_is_accepted(value):
    assert clean_invite(value) == value.strip()


@pytest.mark.parametrize("value", [
    "", "   ", None, 0,
    "chat.whatsapp.com/NOSCHEME",
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "ftp://example.invalid/join",
    "https://",
])
def test_anything_that_is_not_a_web_link_is_ignored(value):
    """Ignored, not rejected: this is a control that changes what a room
    sees, and the failure mode of a typo must be "no QR", never a page that
    will not load. A `javascript:` or `data:` URL in a QR code is a trick
    played on whoever scans it, which is why the schemes are an allowlist."""
    assert clean_invite(value) == ""


def test_a_pasted_essay_cannot_become_a_qr_code():
    """Past a certain length the code gets so dense that nothing in the room
    can read it, which looks like a broken projector rather than a long
    link."""
    assert len(clean_invite("https://x.invalid/" + "y" * 900)) == (
        MAX_INVITE_CHARS)


def test_the_fingerprint_is_a_hash_and_not_the_link():
    """It is what rides on the viewer socket, so it has to say nothing. A
    hash also means the image's URL changes exactly when the link does."""
    fingerprint = tag(SAMPLE_INVITE)
    assert len(fingerprint) == 8
    assert re.fullmatch(r"[0-9a-f]{8}", fingerprint)
    assert fingerprint not in SAMPLE_INVITE
    assert tag(OTHER_INVITE) != fingerprint
    assert tag("") == ""


# ---------------------------------------------------------------------------
# The code itself
# ---------------------------------------------------------------------------


def test_the_code_is_an_svg_sized_in_modules():
    """No pixel size anywhere in it: the page scales it with CSS, so the same
    file is sharp on a 1280x720 projector and on a 4K one."""
    svg = render(SAMPLE_INVITE)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    # The outer element only: the logo is a nested <svg> and is placed in
    # module units like everything else here.
    outer = svg[:svg.index(">") + 1]
    assert "viewBox" in outer
    assert "width=" not in outer and "height=" not in outer, (
        f"the code carries its own size; the card should decide: {outer}")


def test_the_code_carries_its_own_quiet_zone():
    """Four modules of white on every side. Without it a reader has to find
    the edge of the code against whatever is behind it, which from a few
    metres is the difference between scanning and not."""
    import segno

    from app.invite_qr import BORDER, ERROR_LEVEL

    modules = len(segno.make(SAMPLE_INVITE, error=ERROR_LEVEL).matrix)
    svg = render(SAMPLE_INVITE)
    side = int(re.search(r'viewBox="0 0 (\d+)', svg).group(1))
    assert BORDER >= 4
    assert side == modules + 2 * BORDER


def test_nothing_is_drawn_over_the_middle_of_the_code():
    """There was a WhatsApp mark there. A code on a screen read from across
    a room needs every module it has, and the line beside the card already
    says where the link goes - so the mark bought recognition the room did
    not need at a cost it could not afford."""
    svg = render(SAMPLE_INVITE)
    assert "circle" not in svg and "stroke" not in svg
    # One white ground and one black path, and nothing else.
    assert svg.count("<") == 4, f"something else is in the code: {svg[:200]}"
    source = (ROOT / "app" / "invite_qr.py").read_text(encoding="utf-8")
    assert "logo" not in source.lower(), (
        "the logo option is still there to be switched back on")


def test_the_modules_are_as_large_as_the_link_allows():
    """Error correction M, not H.

    Recovery is worth nothing on a drawn code - there is no crease or
    coffee ring - and it is paid for in modules, which is the one thing
    that decides whether a phone across a room can resolve it.
    """
    import segno

    from app.invite_qr import ERROR_LEVEL

    assert ERROR_LEVEL == "m"
    at_m = len(segno.make(SAMPLE_INVITE, error="m").matrix)
    at_h = len(segno.make(SAMPLE_INVITE, error="h").matrix)
    assert at_m < at_h, "M is not buying bigger modules for this link"
    assert at_m / at_h < 0.9, (
        f"only {1 - at_m / at_h:.0%} fewer modules; check the link length")


def test_the_code_is_pure_black_on_pure_white_and_unsmoothed():
    """A phone camera wants the most contrast available and the hardest
    edges. Anti-aliasing a module into grey is a module it has to guess."""
    svg = render(SAMPLE_INVITE)
    assert 'shape-rendering="crispEdges"' in svg
    assert 'fill="#ffffff"' in svg and 'fill="#000000"' in svg
    assert not re.search(r"fill=\"#(?!000000|ffffff)", svg), (
        "the code is drawn in something other than black and white")


def test_drawing_the_code_reaches_no_network():
    """`segno` is pure Python and this module imports it lazily, so the same
    thing that is true of the Soniox client is true here: nothing in the
    test suite can dial out, even by accident."""
    source = (ROOT / "app" / "invite_qr.py").read_text(encoding="utf-8")
    assert "import segno" in source
    assert source.index("def render") < source.index("import segno"), (
        "segno is imported at module scope")
    # Imports only. The prose in this module talks about requests and URLs,
    # and should be free to.
    allowed = {"__future__", "hashlib", "re", "pathlib", "app", "segno"}
    imported = set(re.findall(r"^\s*(?:from|import)\s+(\w+)", source,
                              re.MULTILINE))
    assert imported <= allowed, (
        f"invite_qr reaches for {sorted(imported - allowed)}")


# ---------------------------------------------------------------------------
# What a viewer is told, and what it is not
# ---------------------------------------------------------------------------


def hub(**screen) -> BroadcastHub:
    made = BroadcastHub(CaptionState())
    if screen:
        made.set_screen(**screen)
    return made


def test_the_invite_never_reaches_a_viewer(tmp_path):
    """The one setting on this card that is secret in the ordinary sense.
    Everything else describes something the room can see anyway."""
    live = hub(invite=SAMPLE_INVITE)
    live.set_qr(resolve(SAMPLE_INVITE, state_dir=tmp_path))
    public = live.public_screen()
    assert live.screen["invite"] == SAMPLE_INVITE
    assert "invite" not in public
    assert SAMPLE_INVITE not in json.dumps(public)
    assert public["qr_tag"] == tag(SAMPLE_INVITE)


def test_the_page_is_told_which_image_to_ask_for_not_what_is_in_it(tmp_path):
    """Two values, and neither is the link: the extension to request and a
    fingerprint of whatever is live. There is no "is there a code" flag any
    more, because there always is one."""
    live = hub()
    live.set_qr(resolve("", state_dir=tmp_path))
    public = live.public_screen()
    assert public["qr_ext"] == "svg"
    assert public["qr_tag"] == tag(DEFAULT_LINK)
    assert "qr" not in public, "the dead show/hide flag is still on the wire"

    (tmp_path / "qr.png").write_bytes(b"not really a png")
    live.set_qr(resolve("", state_dir=tmp_path))
    assert live.public_screen()["qr_ext"] == "png"


def test_the_organizers_own_file_wins(tmp_path):
    """Somebody putting artwork in the state directory is the more
    deliberate act, and it may be a code they have already printed and
    handed round - which a regenerated one would silently replace with a
    different image making the same promise. SVG before PNG: it is
    resolution-independent, which on a 4K projector is the difference
    between crisp modules and a resampled grid."""
    (tmp_path / "qr.png").write_bytes(b"png bytes")
    source = resolve(SAMPLE_INVITE, state_dir=tmp_path)
    assert source.from_file and source.extension == "png"

    (tmp_path / "qr.svg").write_text("<svg/>", encoding="utf-8")
    source = resolve(SAMPLE_INVITE, state_dir=tmp_path)
    assert source.from_file and source.extension == "svg"
    assert source.path.name == "qr.svg"


def test_the_fingerprint_follows_the_file(tmp_path):
    """Replacing the artwork has to change the image's URL, or a projector
    that has been up all morning keeps showing the old code."""
    target = tmp_path / "qr.svg"
    target.write_text("<svg>one</svg>", encoding="utf-8")
    first = resolve("", state_dir=tmp_path).tag
    target.write_text("<svg>two</svg>", encoding="utf-8")
    assert resolve("", state_dir=tmp_path).tag != first


def test_the_card_is_never_empty(tmp_path):
    """Three sources and the last one always answers. The card is part of
    the composition now, so "no code" is not a state the poster has."""
    assert resolve("", state_dir=tmp_path).kind == "default"
    assert resolve("", state_dir=tmp_path).url == DEFAULT_LINK
    assert resolve(SAMPLE_INVITE, state_dir=tmp_path).kind == "generated"
    assert resolve("not a link", state_dir=tmp_path).kind == "default"


def test_the_operator_can_see_which_source_is_live(tmp_path):
    """A card quietly showing the fallback because a file landed in the
    wrong directory looks exactly like one that is working. The label says
    what is on screen and never what is in it."""
    labels = [
        resolve("", state_dir=tmp_path).label(),
        resolve(SAMPLE_INVITE, state_dir=tmp_path).label(),
    ]
    (tmp_path / "qr.svg").write_text("<svg/>", encoding="utf-8")
    labels.append(resolve(SAMPLE_INVITE, state_dir=tmp_path).label())
    assert len(set(labels)) == 3, f"the sources are not told apart: {labels}"
    for label in labels:
        assert SAMPLE_INVITE not in label


def test_the_defaults_are_the_words_and_not_a_link():
    """A fresh clone shows the welcome and no card. The link is the one
    value here with no default, because the only possible default would be
    somebody's real group."""
    assert DEFAULT_SCREEN["invite"] == ""
    assert DEFAULT_SCREEN["headline"] == "Captions start shortly."
    assert DEFAULT_SCREEN["eyebrow"] == "LIVE CAPTIONS"
    assert DEFAULT_SCREEN["highlight"] is True
    # The waiting screen is **not** bilingual, deliberately: the design has
    # no Arabic line on it, the header's Arabic wordmark carries the second
    # script, and the bilingual promise is made by the title card and the
    # footer strip - which are what the room reads once the talk starts.
    assert "arabic" not in DEFAULT_SCREEN
    for name, cap in MAX_SCREEN_TEXT.items():
        assert len(str(DEFAULT_SCREEN[name])) <= cap, (
            f"the default {name} is longer than its own cap")
    # The three lines around the card are the operator's words too, and
    # their defaults are the ones the design was drawn with.
    assert DEFAULT_SCREEN["pill"] == "Scan to join"
    assert DEFAULT_SCREEN["cta"] == "Scan to open the link"
    assert DEFAULT_SCREEN["note"] == "Opens the event link"


def test_the_words_are_tidied_and_bounded():
    """They go on a projector and the layout is sized for them, not the
    other way round."""
    live = hub(headline="  lots   of\n  space  ")
    assert live.screen["headline"] == "lots of space"
    live.set_screen(subline="x" * 900)
    assert len(live.screen["subline"]) == MAX_SCREEN_TEXT["subline"]


def test_the_subline_is_the_one_line_that_keeps_a_typed_break():
    """Every other word on the poster is collapsed to one line, newlines
    included. The subline is the exception: the design breaks the sentence
    in one particular place and nothing automatic puts it there, so the
    operator types the break and it survives to the screen.

    Everything else about it is still tidied - runs of spaces, blank lines,
    a Windows `\r\n` from a paste - because none of those is a decision.
    """
    live = hub(subline="first   line\nsecond  line")
    assert live.screen["subline"] == "first line\nsecond line"

    live.set_screen(subline="windows\r\npaste")
    assert live.screen["subline"] == "windows\npaste"

    # A blank line is whitespace, not a gap to reproduce on a projector.
    live.set_screen(subline="  \n\n  one  \n\n  two  \n ")
    assert live.screen["subline"] == "one\ntwo"

    # The poster is drawn for two lines, so a third is dropped rather than
    # shrinking the type below what the back of the room can read.
    live.set_screen(subline="one\ntwo\nthree")
    assert live.screen["subline"] == "one\ntwo"
    assert MAX_SUBLINE_LINES == 2

    # The character cap still bounds the whole thing, and the cut may not
    # leave a break hanging off the end with nothing after it.
    live.set_screen(subline="a" * (MAX_SCREEN_TEXT["subline"] - 1) + "\nb")
    assert len(live.screen["subline"]) <= MAX_SCREEN_TEXT["subline"]
    assert not live.screen["subline"].endswith("\n")

    # And the headline next door is unchanged: a newline there is space.
    live.set_screen(headline="one\ntwo")
    assert live.screen["headline"] == "one two"


def test_the_default_subline_carries_the_designs_break():
    """Two lines, split where the default types it. It is data rather
    than a rule in
    the stylesheet because no rule puts the break there - see the note on
    `tidy_subline`."""
    sub = DEFAULT_SCREEN["subline"]
    assert sub.count("\n") == 1, "the default subline is not two lines"
    first, second = sub.split("\n")
    assert first.endswith("side,"), (
        f"the default breaks after {first.split()[-1]!r}, not after "
        f'"side,"')
    assert second == "as the talk happens."


def test_a_typed_break_round_trips_through_the_saved_file():
    """It is typed once, before a talk, and the laptop gets restarted on
    the morning of one. YAML quotes a newline; a file that lost it would
    silently give the room a one-line subline."""
    import tempfile

    from app.settings import load_screen, save_screen

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "screen.yaml"
        live = hub()
        live.on_change = lambda data: save_screen(data, path)
        live.set_screen(subline="first line\nsecond line")

        fresh = hub()
        fresh.adopt_screen(load_screen(path))
        assert fresh.screen["subline"] == "first line\nsecond line"


def test_the_waiting_screen_survives_a_restart():
    """It is set before a talk and the laptop gets restarted on the morning
    of one."""
    import tempfile

    from app.settings import load_screen, save_screen

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "screen.yaml"
        live = hub()
        live.on_change = lambda data: save_screen(data, path)
        live.set_screen(headline="Welcome back.", invite=SAMPLE_INVITE,
                        highlight=False)

        fresh = hub()
        fresh.adopt_screen(load_screen(path))
        assert fresh.screen["headline"] == "Welcome back."
        assert fresh.screen["invite"] == SAMPLE_INVITE
        assert fresh.screen["highlight"] is False
        # ...and the file it round-tripped through is not committed.
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "config/screen.yaml" in ignored


def test_a_damaged_file_cannot_put_a_bad_link_on_screen():
    live = hub()
    live.adopt_screen({"invite": "javascript:alert(1)", "headline": None,
                       "qr": "yes please"})
    assert live.screen["invite"] == ""
    assert live.screen["headline"] == DEFAULT_SCREEN["headline"]


@needs_git
def test_no_committed_file_carries_a_whatsapp_invite():
    """The same rule the admin token follows. A link in a doc, a test
    fixture or a screenshot is a link that has left the laptop.

    "Obviously invented" is spelled out as **upper case and digits only**.
    A real WhatsApp invite code is 22 mixed-case characters, so a code that
    shouts is one nobody can mistake for live - and it is a rule a reader
    can follow without having to guess what counts.
    """
    listed = subprocess.run(["git", "ls-files"], cwd=ROOT, text=True,
                            capture_output=True, check=True).stdout.split()
    pattern = re.compile(r"chat\.whatsapp\.com/([A-Za-z0-9]{6,})")
    for name in listed:
        path = ROOT / name
        if path.suffix.lower() in (".png", ".jpg", ".ico", ".lock"):
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for code in pattern.findall(body):
            assert code.upper() == code, (
                f"{name} carries what looks like a real invite link "
                f"(.../{code}); sample links are upper case")


# ---------------------------------------------------------------------------
# The operator page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "screen-eyebrow", "screen-headline", "screen-highlight",
    "screen-subline", "screen-pill", "screen-cta",
    "screen-noteline", "screen-invite",
])
def test_every_waiting_screen_setting_has_a_control(name):
    assert f'id="{name}"' in ADMIN_HTML, f"no control for {name}"


def test_the_subline_control_takes_a_line_break():
    """A textarea, not a one-line input: the break in this field is the
    operator's and Enter has to be able to make one. An `<input>` cannot
    hold a newline at all, so this is the whole feature on that page."""
    assert re.search(r'<textarea id="screen-subline"', ADMIN_HTML), (
        "the subline control is not a textarea, so no break can be typed")


@pytest.mark.parametrize("name", [
    "eyebrow", "headline", "highlight", "subline", "pill", "cta",
    "note", "invite",
])
def test_every_waiting_screen_setting_is_sent(name):
    assert f"{name}: els.screen" in ADMIN_JS, (
        f"{name} is never sent to /admin/screen")


def test_the_operator_page_is_the_only_place_the_link_is_shown():
    """`/admin/status` is behind the token and is where the field is filled
    back in from. Nothing else in the project hands the link back as text."""
    assert "els.screenInvite, 'invite'" in ADMIN_JS
    # The projector's script never reads an `invite` off the payload,
    # because the payload never carries one. Comments are stripped first:
    # that file explains at length *why* the link is not there, and saying
    # so is documentation rather than a dependency on it.
    code = re.sub(r"/\*.*?\*/", "", SCREEN_JS, flags=re.S)
    assert "invite" not in code, (
        "screen.js reads the invite link; it is never sent one")


def test_the_fields_push_on_change_rather_than_on_every_keystroke():
    """The operator is typing a sentence a room will read. `input` would put
    half-written words on a projector."""
    block = ADMIN_JS.split("els.screenEyebrow, els.screenHeadline", 1)[1]
    assert "addEventListener('change', pushScreen)" in block[:400]
    assert "addEventListener('input'" not in block[:400]


# ---------------------------------------------------------------------------
# The page, read as source
# ---------------------------------------------------------------------------


def test_the_three_states_are_exclusive():
    """Waiting, carded, captions. The card is for the gap *inside* a running
    session; the poster is for before and after. Getting this the other way
    round shows a room a "come and join us" poster three seconds after the
    speaker has started."""
    assert "var carded = forcedCard === null ? (live && !hasText())" in SCREEN_JS
    assert "var waiting = !live && !carded;" in SCREEN_JS


def test_the_poster_fades_and_respects_reduced_motion():
    block = STYLE.split(".waitscreen {", 1)[1].split("}", 1)[0]
    assert "transition: opacity" in block
    assert "visibility: hidden" in block, (
        "`hidden` cannot be faded out of - the global rule is "
        "`display: none !important`")
    reduced = STYLE.split("@media (prefers-reduced-motion: reduce)")
    assert any(".waitscreen" in chunk.split("}}", 1)[0] for chunk in reduced[1:])


def test_the_poster_is_sized_for_a_projector_not_a_laptop():
    """Every size in the section is `vw`/`vh`, so the poster scales as one
    object from 720p to 4K. A `px` here is a thing that stays the same size
    while everything around it doubles."""
    section = STYLE.split("-- the waiting screen", 1)[1] \
                   .split("-- operator page", 1)[0]
    section = re.sub(r"/\*.*?\*/", "", section, flags=re.S)
    for prop, value in re.findall(r"(font-size|width|height|padding|gap)"
                                  r"\s*:\s*([^;]+);", section):
        if "px" not in value:
            continue
        # The hairline and the artwork's own cap are the two exceptions: one
        # is a line that should not thicken, the other is the generated
        # native size the mark may never exceed.
        assert "var(--wm-" in value or value.strip() in ("1px",), (
            f"`{prop}: {value.strip()}` is a pixel size on the poster")


def test_the_header_reuses_the_title_card_lockup():
    """Same classes, same rules, one knob turned down - so a new master
    moves the header with the card instead of leaving it behind.

    **Three** lockups on this page as of 2026-10-20: the title card, this
    header, and the caption view's corner band. All one implementation, so
    the forced direction and the baseline nudge cannot disagree between
    them. A fourth copy of the markup is the thing to notice here, not a
    number to bump.
    """
    assert SCREEN_HTML.count('class="titlecard-lockup"') == 3, (
        "the title card, the poster header and the corner band each use "
        "the shared lockup; a different count means one was hand-rolled")
    block = STYLE.split(".wait-head .titlecard-lockup {", 1)[1] \
                 .split("}", 1)[0]
    assert "--title-lockup" in block
    assert "var(--wm-en-w)" in block, (
        "the header lockup is capped at a number rather than at the "
        "artwork's own width")


def test_the_qr_route_is_not_in_the_static_allowlist():
    """It is drawn, not served from `web/`, so it cannot be reached by
    guessing a filename."""
    from app.main import ASSETS

    assert not any("qr" in name for name in ASSETS)
    assert "'/screen/qr.'" in SCREEN_JS


# ---------------------------------------------------------------------------
# The browser
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

#: Its own port. Two pytest runs at once on one port look like regressions
#: and are not - see the note in `tests/test_screen_auto.py`.
PORT = 8115
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "waiting-screen-test-token"

SIZES = [(1920, 1080), (1280, 720), (3840, 2160)]


@pytest.fixture(scope="module")
def state_dir():
    """Where this module's server keeps the files `/admin` writes.

    The server saves what the operator sets, and these tests *are* an
    operator. `CAPTIONS_STATE_DIR` points the two writable config files at a
    temporary directory for the life of that server, so the suite can never
    touch the real `config/screen.yaml` - the credit line, the waiting
    screen's words and the invite link - nor the real `config/qr.svg`. It is
    also where a test can drop artwork to stand in for the organizer's.
    """
    state = tempfile.mkdtemp(prefix="captions-waiting-")
    yield state
    shutil.rmtree(state, ignore_errors=True)


@pytest.fixture(scope="module")
def live_server(state_dir):
    import os
    import urllib.request

    state = state_dir

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


def admin(base: str, **params) -> None:
    import urllib.parse
    import urllib.request

    params["token"] = TOKEN
    url = f"{base}/admin/screen?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, method="POST")
    urllib.request.urlopen(request, timeout=15).read()


@contextlib.contextmanager
def poster(browser, base, width=1920, height=1080):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    try:
        page.goto(base + "/screen", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.CaptionScreen && window.CaptionScreen.waiting()",
            timeout=15000)
        yield page
    finally:
        context.close()


def wait_for_code(page) -> None:
    page.wait_for_function(
        "() => { const i = document.getElementById('wait-qr');"
        "        return i && i.complete && i.naturalWidth > 0; }",
        timeout=15000)


@needs_browser
@pytest.mark.parametrize("width,height", SIZES)
def test_nothing_on_the_poster_overlaps_anything_else(live_server, width,
                                                      height):
    """Measured in a browser at all three projector sizes.

    The one overlap on this screen is deliberate - the "scan to join" pill
    sits across the card's top edge - so it is named rather than excluded by
    accident, and everything else has to keep its distance.
    """
    from playwright.sync_api import sync_playwright

    admin(live_server, invite=SAMPLE_INVITE)
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server, width, height) as page:
                wait_for_code(page)
                boxes = page.evaluate("""() => {
                  const names = ['.wait-head', '#wait-eyebrow',
                                 '#wait-headline', '#wait-sub',
                                 '.wait-card', '.wait-cta', '.wait-note'];
                  const out = {};
                  for (const name of names) {
                    const el = document.querySelector(name);
                    const r = el.getBoundingClientRect();
                    out[name] = {x: r.x, y: r.y, w: r.width, h: r.height,
                                 right: r.right, bottom: r.bottom};
                  }
                  out.__page = {
                    w: document.documentElement.clientWidth,
                    h: document.documentElement.clientHeight,
                    sw: document.documentElement.scrollWidth,
                    sh: document.documentElement.scrollHeight};
                  return out;
                }""")
        finally:
            browser.close()

    page_box = boxes.pop("__page")
    assert page_box["sw"] <= page_box["w"], "the poster scrolls sideways"
    assert page_box["sh"] <= page_box["h"], "the poster scrolls down"

    for name, box in boxes.items():
        assert box["w"] > 0 and box["h"] > 0, f"{name} has no size"
        assert box["x"] >= -1 and box["right"] <= page_box["w"] + 1, (
            f"{name} runs off the side at {width}x{height}")
        assert box["y"] >= -1 and box["bottom"] <= page_box["h"] + 1, (
            f"{name} runs off the bottom at {width}x{height}")

    names = sorted(boxes)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            a, b = boxes[first], boxes[second]
            overlap_x = min(a["right"], b["right"]) - max(a["x"], b["x"])
            overlap_y = min(a["bottom"], b["bottom"]) - max(a["y"], b["y"])
            assert overlap_x <= 0 or overlap_y <= 0, (
                f"{first} and {second} overlap by "
                f"{overlap_x:.0f}x{overlap_y:.0f}px at {width}x{height}")


@needs_browser
@pytest.mark.parametrize("width,height", SIZES)
@pytest.mark.parametrize("shrink", [1.0, 0.5])
def test_the_code_on_screen_actually_scans(live_server, width, height,
                                           shrink):
    """A decoder reads the rendered card back - not the SVG, the pixels -
    and has to get the URL out of it.

    **Also at half size**, which is this suite's stand-in for distance: a
    phone four metres back sees the code across half as many sensor pixels
    as one at two metres, and that is the condition the first version of
    this screen failed in the room. A desktop decoder on a clean screenshot
    is still an easier reader than a phone camera at an angle in a lit
    hall, so passing here is a floor and not a guarantee.

    `segno` cannot decode, so this needs a reader that is not a project
    dependency and skips without one.
    """
    from io import BytesIO

    from playwright.sync_api import sync_playwright
    zxing = pytest.importorskip(
        "zxingcpp",
        reason="no QR reader; run with `uv run --with zxing-cpp`")
    Image = pytest.importorskip("PIL.Image", reason="Pillow is not installed")

    admin(live_server, invite=SAMPLE_INVITE)
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server, width, height) as page:
                wait_for_code(page)
                page.wait_for_timeout(700)      # the fade
                # The served code as the room sees it, card and all - not
                # the <img> alone, so the white border and the card's own
                # edge are in the picture a decoder has to work from.
                shot = page.locator(".wait-card").screenshot()
        finally:
            browser.close()

    image = Image.open(BytesIO(shot)).convert("RGB")
    if shrink != 1.0:
        image = image.resize(
            (max(1, int(image.width * shrink)),
             max(1, int(image.height * shrink))), Image.LANCZOS)

    found = zxing.read_barcodes(image)
    assert found, (
        f"nothing decoded at {width}x{height} scaled to {shrink:.0%} "
        f"({image.width}x{image.height}px of code); the modules are too "
        f"small or too soft for a camera at that distance")
    assert found[0].text == SAMPLE_INVITE


@needs_browser
@pytest.mark.parametrize("width,height", SIZES)
@needs_wordmarks
def test_the_two_marks_sit_at_the_ends_of_the_rule(live_server, width,
                                                   height):
    """Latin left, Arabic right, each flush with an end of the hairline -
    the way the default theme sets them out.

    The direction is the part that needs watching: `space-between` puts
    whichever mark comes first *in the writing direction* at the left end,
    and this page is full of Arabic. `dir="ltr"` on the row is what holds
    it, and reading it off the stylesheet would not tell you whether it
    worked.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server, width, height) as page:
                box = page.evaluate("""() => {
                  const q = s => {
                    const r = document.querySelector(s).getBoundingClientRect();
                    return {x: r.x, right: r.right, w: r.width};
                  };
                  return {en: q('.wait-head .titlecard-wordmark.en'),
                          ar: q('.wait-head .titlecard-wordmark.ar'),
                          rule: q('.wait-head')};
                }""")
        finally:
            browser.close()

    en, ar, rule = box["en"], box["ar"], box["rule"]
    assert abs(en["x"] - rule["x"]) < 2, (
        f"the Latin mark starts {en['x'] - rule['x']:.0f}px from the left "
        f"end of the rule at {width}x{height}")
    assert abs(ar["right"] - rule["right"]) < 2, (
        f"the Arabic mark ends {rule['right'] - ar['right']:.0f}px short of "
        f"the right end of the rule at {width}x{height}")
    # ...and they are that way round, with the width of the poster between
    # them rather than a lockup's worth of air.
    assert en["right"] < ar["x"], "the Arabic mark has flipped to the left"
    assert ar["x"] - en["right"] > rule["w"] * 0.4, (
        "the marks are bunched rather than spanning the rule")


@needs_browser
def test_the_headline_keeps_the_space_before_its_accented_word(live_server):
    """The bug this test exists for rendered "Captions start shortly." as
    "Captions startshortly.".

    The cause was not the markup - `textContent` was right the whole time -
    but the global `h1` rule's `display: flex`, which made the text and the
    accent span two flex items, and **whitespace between flex items is
    dropped**. So the text is checked twice: once as characters, which is
    what the operator typed, and once as *width* against the same string
    laid out as one plain run, which is the only way a lost space shows up.
    """
    from playwright.sync_api import sync_playwright

    headline = "Captions start shortly."
    admin(live_server, headline=headline, highlight=True)
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server) as page:
                page.wait_for_function(
                    "text => document.getElementById('wait-headline')"
                    "          .textContent === text",
                    arg=headline, timeout=15000)
                measured = page.evaluate("""() => {
                  const h = document.getElementById('wait-headline');
                  const style = getComputedStyle(h);

                  /* Two clones of the headline on one unwrapped line: one
                     keeping its markup, the text and the accent span, and
                     one as a single plain run of the same characters. If
                     the space between the two boxes is being dropped, the
                     first is about a quarter of the type size narrower.
                     Measuring the headline in place would instead measure
                     where it happens to wrap. */
                  const measure = (fill) => {
                    const probe = h.cloneNode(false);
                    fill(probe);
                    probe.style.position = 'absolute';
                    probe.style.visibility = 'hidden';
                    probe.style.width = 'max-content';
                    probe.style.maxWidth = 'none';
                    probe.style.whiteSpace = 'nowrap';
                    probe.style.textWrap = 'nowrap';
                    h.parentNode.appendChild(probe);
                    const width = probe.getBoundingClientRect().width;
                    probe.remove();
                    return width;
                  };

                  return {
                    text: h.textContent,
                    display: style.display,
                    family: style.fontFamily,
                    inline: measure(p => { p.innerHTML = h.innerHTML; }),
                    plain: measure(p => { p.textContent = h.textContent; }),
                  };
                }""")
        finally:
            browser.close()

    assert measured["text"] == headline
    # A dropped space is about a quarter of the type size - far outside the
    # rounding this comparison can produce.
    assert abs(measured["inline"] - measured["plain"]) < 4, (
        f"the headline lays out {measured['plain'] - measured['inline']:.0f}"
        f"px narrower than its own text does; a space has been dropped "
        f"between the text and its accented last word")
    assert measured["display"] != "flex", (
        "the headline is a flex container again; its words will be laid "
        "out as flex items and the spaces between them dropped")
    assert measured["family"].startswith('"Inter Tight"'), (
        f"the headline is set in {measured['family']}, not the display "
        f"face; a more specific rule has taken the font back")


@needs_browser
@pytest.mark.parametrize("width,height", SIZES)
def test_the_headline_is_one_line_that_fills_the_column(live_server, width,
                                                        height):
    """One line, as large as the column allows, at every projector size.

    Two lines was the earlier reading of the design and it was wrong on a
    real screen: a wrapped headline is a short block with a hole beside
    it, and the left column stopped filling the space up to the card.
    """
    from playwright.sync_api import sync_playwright

    from app.broadcast import DEFAULT_SCREEN

    admin(live_server, headline=DEFAULT_SCREEN["headline"])
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server, width, height) as page:
                page.wait_for_timeout(400)
                head = page.evaluate("() => window.CaptionScreen.headline()")
                lines = page.evaluate("""() => {
                  const h = document.getElementById('wait-headline');
                  return h.getClientRects().length;
                }""")
        finally:
            browser.close()

    assert lines == 1, (
        f"the headline wrapped onto {lines} lines at {width}x{height}")
    assert head["line"] <= head["room"] + 1, (
        f"the headline is {head['line']}px in {head['room']}px of column")
    # ...and it is actually filling that column rather than sitting in the
    # middle of it: the dead strip between the text and the card is what
    # this whole change is about.
    assert head["line"] > head["room"] * 0.9, (
        f"the headline fills only {head['line'] / head['room']:.0%} of its "
        f"column at {width}x{height}")
    # Scaled with the screen, not pinned to a pixel size.
    assert head["size"] > height * 0.07, (
        f"the headline is {head['size']:.0f}px on a {height}px screen")


@needs_browser
def test_a_long_headline_shrinks_rather_than_wrapping(live_server):
    """The operator can type anything into that field, and the one thing
    the headline may not do is become two lines - so it gets smaller."""
    from playwright.sync_api import sync_playwright

    from app.broadcast import DEFAULT_SCREEN

    long_one = "Welcome to the whole of this evening's long programme."
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server) as page:
                admin(live_server, headline=DEFAULT_SCREEN["headline"])
                page.wait_for_function(
                    "t => document.getElementById('wait-headline')"
                    "       .textContent === t",
                    arg=DEFAULT_SCREEN["headline"], timeout=15000)
                page.wait_for_timeout(300)
                short = page.evaluate("() => window.CaptionScreen.headline()")

                admin(live_server, headline=long_one)
                page.wait_for_function(
                    "t => document.getElementById('wait-headline')"
                    "       .textContent === t",
                    arg=long_one, timeout=15000)
                page.wait_for_timeout(300)
                long_head = page.evaluate(
                    "() => window.CaptionScreen.headline()")
                lines = page.evaluate(
                    "() => document.getElementById('wait-headline')"
                    "        .getClientRects().length")
        finally:
            admin(live_server, headline=DEFAULT_SCREEN["headline"])
            browser.close()

    assert lines == 1, "a long headline wrapped instead of shrinking"
    assert long_head["size"] < short["size"], (
        "a headline half again as long was not scaled down")
    assert long_head["line"] <= long_head["room"] + 1


@needs_browser
@pytest.mark.parametrize("width,height", SIZES)
def test_the_subline_breaks_where_the_operator_typed_it(live_server, width,
                                                        height):
    """Exactly two lines, split where the operator typed the break.

    Measured off the rendered line boxes rather than read off the string,
    because the question is where the browser put the break. It used to be
    `text-wrap: balance`, which evened the two lines out and was better
    than the natural break - that left "next." alone on line two - but
    where it broke still moved with the words, the viewport and the face.
    The break is now typed and rendered as a `<br>`, so it is in the same
    place on every projector.

    The subline **only**. The headline above is one `nowrap` line that
    `screen.js` sizes to fit, and it has no break to carry.
    """
    from playwright.sync_api import sync_playwright

    admin(live_server, subline=DEFAULT_SCREEN["subline"])
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server, width=width,
                        height=height) as page:
                page.wait_for_timeout(400)
                sub = page.evaluate("() => window.CaptionScreen.subline()")
        finally:
            browser.close()

    assert sub["breaks"] == 1, (
        f"the subline carries {sub['breaks']} <br> elements, not the one "
        f"the operator typed")
    assert len(sub["widths"]) == 2, (
        f"the subline rendered {len(sub['widths'])} lines at "
        f"{width}x{height}, not two")
    first, second = sub["text"].split("\n")
    # Derived from the shipped default rather than naming a word, so a
    # theme that changes the subline still proves the typed break survives
    # rather than failing on wording it has every right to change.
    wanted, wanted_second = DEFAULT_SCREEN["subline"].split("\n")
    assert first == wanted, (
        f"line one is {first!r}, not the default's first line {wanted!r}")
    assert second == wanted_second, (
        "line two is not the default's second line")


@needs_browser
@pytest.mark.parametrize("width,height", [(1920, 1080), (1280, 720)])
def test_each_subline_line_stays_on_one_line(live_server, width, height):
    """`nowrap` per line, and the type shrinks when a line is too long -
    the headline's contract, now the subline's too.

    The break is the only break there may be. A line that wrapped would
    put three rows under a headline the poster has room for two under, and
    would break the phrase somewhere the operator did not choose.
    """
    from playwright.sync_api import sync_playwright

    # Each half past the column at both sizes, so the fit has to bite. The
    # default's longest line is 531px in 666px of column at 1280x720; this
    # one's is 719px, which has to come back inside it.
    long_one = ("Where every builder, founder, researcher and curious "
                "newcomer in the room meets,\n"
                "to think out loud together, to share what they have "
                "learned, and to shape it.")
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server, width, height) as page:
                admin(live_server, subline=DEFAULT_SCREEN["subline"])
                page.wait_for_function(
                    "t => window.CaptionScreen.subline().text === t",
                    arg=DEFAULT_SCREEN["subline"], timeout=15000)
                page.wait_for_timeout(300)
                short = page.evaluate(
                    "() => window.CaptionScreen.subline()")

                admin(live_server, subline=long_one)
                page.wait_for_function(
                    "t => window.CaptionScreen.subline().text === t",
                    arg=long_one, timeout=15000)
                page.wait_for_timeout(300)
                long_sub = page.evaluate(
                    "() => window.CaptionScreen.subline()")
        finally:
            admin(live_server, subline=DEFAULT_SCREEN["subline"])
            browser.close()

    for name, sub in (("default", short), ("long", long_sub)):
        assert len(sub["widths"]) == 2, (
            f"the {name} subline rendered {len(sub['widths'])} lines at "
            f"{width}x{height}; one of its two halves wrapped")
        # The line boxes, not `scrollWidth`: that reports the column's own
        # width whenever the text fits inside it, so it cannot tell "just
        # fits" from "half empty" and would pass an un-fitted overflow by
        # the width of the box rather than of the text.
        assert max(sub["widths"]) <= sub["room"] + 1, (
            f"the {name} subline's widest line is {max(sub['widths']):.0f}px "
            f"in {sub['room']}px of column at {width}x{height}")
    assert long_sub["size"] < short["size"], (
        f"a longer subline was not scaled down at {width}x{height}: "
        f"{long_sub['size']}px against the default's {short['size']}px")
    assert long_sub["fitted"], "the long subline was never measured"
    # ...and the default is left at the size the design was drawn at.
    assert not short["fitted"], (
        f"the default subline is being shrunk at {width}x{height}, so the "
        f"stylesheet's size is already too big for the column")


@needs_browser
def test_the_subline_field_cannot_put_markup_on_the_projector(live_server):
    """The operator's words go on a screen, and the only markup this field
    may produce is the break. `paintSubline` builds text nodes and a `<br>`
    and never goes near `innerHTML`; typed tags are shown, not run."""
    from playwright.sync_api import sync_playwright

    typed = "<b>bold</b> &amp; <img src=x onerror=alert(1)>\nsecond line"
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server) as page:
                admin(live_server, subline=typed)
                page.wait_for_function(
                    "() => window.CaptionScreen.subline().text"
                    "        .indexOf('<b>') !== -1", timeout=15000)
                shape = page.evaluate("""() => {
                  const sub = document.getElementById('wait-sub');
                  return {
                    tags: [...sub.children].map(n => n.tagName),
                    text: sub.textContent,
                  };
                }""")
        finally:
            admin(live_server, subline=DEFAULT_SCREEN["subline"])
            browser.close()

    assert shape["tags"] == ["BR"], (
        f"the subline built {shape['tags']} out of what was typed; the only "
        f"element it may create is the break")
    assert "<b>bold</b>" in shape["text"], "the typed tag was not shown as text"
    assert "&amp;" in shape["text"], "the typed entity was decoded"


def test_the_subline_is_never_built_from_innerhtml():
    """The reason the test above passes, pinned where a reviewer will see
    it: this string comes out of a form field.

    Comments are stripped first - the file says at length why it does not
    use `innerHTML`, and saying so is documentation rather than the thing
    being asserted."""
    code = re.sub(r"/\*.*?\*/", "", SCREEN_JS, flags=re.S)
    assert "innerHTML" not in code


@needs_browser
def test_there_is_no_dead_strip_between_the_text_and_the_card(live_server):
    """What was seen on a real screen: the left column stopped well short
    of the card and the middle of the poster was empty.

    The subline is the other half of it - it used to carry a measure of
    its own and ran even shorter than the headline.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server) as page:
                page.wait_for_timeout(400)
                box = page.evaluate("""() => {
                  const head = document.getElementById('wait-headline');
                  const sub = document.getElementById('wait-sub');
                  const card = document.querySelector('.wait-card')
                                 .getBoundingClientRect();
                  const h = head.getBoundingClientRect();
                  const s = sub.getBoundingClientRect();
                  const line = parseFloat(getComputedStyle(sub).lineHeight);
                  return {
                    headRight: h.x + head.scrollWidth,
                    subRight: s.x + sub.scrollWidth,
                    subLines: Math.round(s.height / line),
                    cardLeft: card.x,
                    page: document.documentElement.clientWidth,
                  };
                }""")
        finally:
            browser.close()

    text_right = max(box["headRight"], box["subRight"])
    gap = box["cardLeft"] - text_right
    assert gap > 0, "the text runs into the card"
    assert gap < box["page"] * 0.17, (
        f"{gap:.0f}px of empty screen between the text and the card")
    assert box["subLines"] <= 2, (
        f"the subline runs to {box['subLines']} lines at 1920x1080")


@needs_browser
def test_the_three_lines_around_the_card_are_the_operators_words(live_server):
    """The pill, the line under the card, and the small line after the dot.

    They were fixed text, which is fine until the code stops pointing at
    WhatsApp - the organiser's own file goes to a redirect, so the note is
    a claim the operator should be able to change without an edit here.
    The coloured dot is markup and stays whatever the words are.
    """
    from playwright.sync_api import sync_playwright

    from app.broadcast import DEFAULT_SCREEN

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server) as page:
                # The defaults the design was drawn with.
                assert page.inner_text(".wait-cta") == DEFAULT_SCREEN["cta"]
                # `text_content`, not `inner_text`: the stylesheet renders
                # the pill in capitals and `inner_text` reports what is
                # painted, which is not what is stored.
                assert (page.text_content("#wait-pill")
                        == DEFAULT_SCREEN["pill"])
                assert DEFAULT_SCREEN["note"] in page.inner_text(".wait-note")
                # ...shown in capitals without being stored that way, so the
                # operator types a sentence and the design decides the case.
                assert page.evaluate(
                    "() => getComputedStyle(document.querySelector("
                    "'.wait-pill')).textTransform") == "uppercase"

                admin(live_server, pill="Join us", cta="Join the circle",
                      note="Opens the group chat")
                page.wait_for_function(
                    "() => document.getElementById('wait-pill')"
                    "        .textContent === 'Join us'", timeout=15000)
                assert page.inner_text(".wait-cta") == "Join the circle"
                assert "Opens the group chat" in page.inner_text(".wait-note")
                assert page.locator(".wait-dot").count() == 1, (
                    "the coloured dot went with the words")
        finally:
            admin(live_server, pill=DEFAULT_SCREEN["pill"],
                  cta=DEFAULT_SCREEN["cta"], note=DEFAULT_SCREEN["note"])
            browser.close()


@needs_browser
def test_the_card_is_there_with_no_link_set(live_server):
    """A fresh install has no file and no link, and the poster is still the
    poster: two columns, a card, and a code for the default link in it.

    There used to be a second, centred layout for this. It is gone - one
    composition means one set of positions to get right, and the operator
    can no longer empty half the screen by clearing a field."""
    from playwright.sync_api import sync_playwright

    admin(live_server, invite="")
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server) as page:
                wait_for_code(page)
                assert page.locator("#wait-join").is_visible()
                columns = page.evaluate(
                    "() => getComputedStyle(document.querySelector("
                    "'.wait-body')).gridTemplateColumns.split(' ').length")
                assert columns == 2, "the poster lost its second column"
                left = page.evaluate("""() => {
                  const copy = document.querySelector('.wait-copy')
                                 .getBoundingClientRect();
                  const card = document.querySelector('.wait-card')
                                 .getBoundingClientRect();
                  return {copy: copy.x, card: card.x};
                }""")
                assert left["copy"] < left["card"], (
                    "the welcome is not to the left of the card")
        finally:
            browser.close()


@needs_browser
def test_the_organizers_file_is_what_the_projector_shows(live_server,
                                                         state_dir):
    """Dropped into the state directory while the server is running, and on
    screen by the next operator poll - nobody walks over to the projector.

    Served byte for byte, too: it is the organizer's artwork and may be the
    code they have already printed.
    """
    import urllib.request

    from playwright.sync_api import sync_playwright

    artwork = render(OTHER_INVITE)          # stands in for their file
    (Path(state_dir) / "qr.svg").write_text(artwork, encoding="utf-8")
    try:
        admin(live_server, invite=SAMPLE_INVITE)   # the file still wins
        with sync_playwright() as play:
            browser = play.chromium.launch()
            try:
                with poster(browser, live_server) as page:
                    wait_for_code(page)
                    src = page.get_attribute("#wait-qr", "src")
                    assert src.startswith("/screen/qr.svg")
            finally:
                browser.close()
        with urllib.request.urlopen(live_server + "/screen/qr.svg") as got:
            served = got.read().decode("utf-8")
            # `got.headers` matches case-insensitively; `dict()` of it does
            # not, and uvicorn sends the names lower-cased.
            headers = got.headers
    finally:
        (Path(state_dir) / "qr.svg").unlink(missing_ok=True)
        admin(live_server, invite="")

    assert served == artwork, "the file was re-drawn rather than served"
    assert headers.get("Content-Type", "").startswith("image/svg+xml")


@needs_browser
def test_a_served_svg_cannot_run_anything(live_server):
    """The one QR source that is **a file somebody dropped into a
    directory**, and an SVG is a document: opened as a page rather than
    through an `<img>` it can carry script. The page only ever references
    it from an `<img>`, which already neuters it; these headers say the
    same thing to a browser that arrives at the URL directly."""
    import urllib.request

    with urllib.request.urlopen(live_server + "/screen/qr.svg") as got:
        headers = got.headers

    policy = headers.get("Content-Security-Policy", "")
    assert "default-src 'none'" in policy, f"no CSP on the QR: {policy!r}"
    assert "sandbox" in policy
    assert "script" not in policy.replace("default-src 'none'", ""), (
        "the policy names a script source; it should allow none")
    assert headers.get("X-Content-Type-Options") == "nosniff"


@needs_browser
def test_the_page_never_inlines_the_svg():
    """It is referenced, not embedded. Inlining somebody else's SVG into
    the document puts its contents inside the page's own origin, which is
    the one thing the `<img>` boundary is doing for us."""
    assert "<img" in SCREEN_HTML.split('id="wait-qr"')[0][-300:]
    # Comments stripped, like the invite-link check above: `screen.js`
    # explains in prose why it builds nodes rather than markup, and saying
    # so must not be what makes this pass.
    assert "innerHTML" not in re.sub(r"/\*.*?\*/", "", SCREEN_JS, flags=re.S)
    assert "qr.svg" not in SCREEN_HTML, (
        "the page hard-codes the image rather than being told which to ask "
        "for")


@needs_browser
def test_changing_the_link_changes_the_code(live_server):
    """The image's URL carries the link's fingerprint, so there is no cache
    header to get subtly wrong - and a projector showing yesterday's code is
    a room joining the wrong group."""
    from playwright.sync_api import sync_playwright

    admin(live_server, invite=SAMPLE_INVITE)
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server) as page:
                wait_for_code(page)
                first = page.get_attribute("#wait-qr", "src")
                admin(live_server, invite=OTHER_INVITE)
                page.wait_for_function(
                    "src => document.getElementById('wait-qr')"
                    "         .getAttribute('src') !== src",
                    arg=first, timeout=15000)
                second = page.get_attribute("#wait-qr", "src")
        finally:
            browser.close()

    assert tag(SAMPLE_INVITE) in first
    assert tag(OTHER_INVITE) in second
    for src in (first, second):
        assert "whatsapp" not in src.lower(), (
            "the link itself is in the image's URL")


@needs_browser
def test_the_operator_can_change_the_words_without_touching_the_projector(
        live_server):
    """The same mechanism as the view and the text size: the projector is a
    browser nobody can reach once the room has filled up."""
    from playwright.sync_api import sync_playwright

    admin(live_server, headline=DEFAULT_SCREEN["headline"], highlight=True)
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            with poster(browser, live_server) as page:
                admin(live_server, headline="Good evening, everyone.",
                      eyebrow="EVENING NO. 12")
                page.wait_for_function(
                    "() => document.getElementById('wait-headline')"
                    "        .textContent === 'Good evening, everyone.'",
                    timeout=15000)
                assert page.inner_text("#wait-eyebrow") == "EVENING NO. 12"
                # The last word carries the accent, and only the last word.
                accent = page.inner_text("#wait-headline .accent")
                assert accent == "everyone."
                # Read the token rather than naming a colour, so a
                # theme that changes the accent still passes this and a
                # highlight that stops using the accent still fails it.
                colour, token = page.evaluate(
                    "() => {"
                    " const el = document.querySelector("
                    "   '#wait-headline .accent');"
                    " const root = document.documentElement;"
                    " const probe = document.createElement('span');"
                    " probe.style.color = 'var(--accent)';"
                    " root.appendChild(probe);"
                    " const want = getComputedStyle(probe).color;"
                    " probe.remove();"
                    " return [getComputedStyle(el).color, want];"
                    "}")
                assert colour == token, (
                    f"the highlighted word is {colour}, not the accent "
                    f"({token})")

                admin(live_server, highlight=False)
                page.wait_for_function(
                    "() => !document.querySelector('#wait-headline .accent')",
                    timeout=15000)
        finally:
            admin(live_server, headline=DEFAULT_SCREEN["headline"],
                  eyebrow=DEFAULT_SCREEN["eyebrow"], highlight=True)
            browser.close()
