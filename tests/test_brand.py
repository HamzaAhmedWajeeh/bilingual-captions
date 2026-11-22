"""The theme system: the neutral default, and what a custom theme may not break.

This file used to assert a particular organisation's palette. It now asserts
the *properties* that palette had to satisfy, against whatever colours are
configured - which is the only version of this test that survives being
open-sourced. A fork that themes the app in its own colours should still be
able to run the suite, and should still fail it if the theme makes the screen
unreadable.

Three things are guarded:

* **Contrast.** The accent carries text and has text drawn on it, both at
  WCAG AA or better. A theme that picks something pale and forgets
  `accent_ink` fails here rather than in a room.
* **Separation.** The accent must not be mistakable for a colour that means
  something - the red that says "this spends money or something broke", the
  amber that says "saved but not applied". Across a dim room, mid-talk, the
  eye gets one chance.
* **One surface.** `/screen` paints exactly one background colour. It used to
  mix `#000` with a near-black tile: invisible on a laptop, a visible patch
  on a calibrated projector.

Plus the plumbing: that no artwork is committed, that the generated
stylesheet is optional, and that `/brand.css` cannot be used to inject CSS.

The contrast figures are WCAG 2.1 relative luminance, the same formula a
browser devtools audit uses.
"""

from __future__ import annotations

import colorsys
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import brand as brand_mod  # noqa: E402
from app.main import ASSETS, BRAND_SLOTS  # noqa: E402
from app.settings import WEB_DIR  # noqa: E402
from repo import needs_git  # noqa: E402

STYLE = (WEB_DIR / "style.css").read_text(encoding="utf-8")
INDEX = (WEB_DIR / "index.html").read_text(encoding="utf-8")
SCREEN_HTML = (WEB_DIR / "screen.html").read_text(encoding="utf-8")
ADMIN_HTML = (WEB_DIR / "admin.html").read_text(encoding="utf-8")
PAGES = {"index.html": INDEX, "screen.html": SCREEN_HTML,
         "admin.html": ADMIN_HTML}

#: Minimum WCAG AA contrast for normal-size text.
AA = 4.5
#: Hue degrees the accent must keep from any colour that means something else.
MIN_HUE_GAP = 25


def token(name: str) -> str:
    """Read a custom property out of the stylesheet's `:root`."""
    root = STYLE.split(":root {", 1)[1].split("}", 1)[0]
    match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", root)
    assert match, f"--{name} is not defined in :root"
    return match.group(1)


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hue(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    r, g, b = (int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)[0] * 360


def hue_gap(a: str, b: str) -> float:
    delta = abs(hue(a) - hue(b))
    return min(delta, 360 - delta)


#: `STYLE` with every comment removed. Several tests below ask "does any rule
#: declare X", and a comment explaining why a rule does *not* declare X would
#: otherwise answer yes.
BARE = re.compile(r"/\*.*?\*/", re.S).sub("", STYLE)


def rules() -> list[tuple[str, str]]:
    """`(selector, declarations)` for every rule in the stylesheet."""
    return [(" ".join(m.group(1).split()), m.group(2))
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", BARE)]


def declarations(body: str) -> list[tuple[str, str]]:
    return re.findall(r"([a-z-]+)\s*:\s*([^;]+);", body)


# ---------------------------------------------------------------------------
# The default theme and the stylesheet agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key,css", [("accent", "accent"),
                                     ("accent_ink", "accent-ink"),
                                     ("background", "bg-brand")])
def test_the_default_theme_matches_the_stylesheet(key, css):
    """`app/brand.py` has to be able to answer "what colour is the accent"
    for `/brand.css`, so it carries the defaults too. Two copies of a value
    is a drift risk, which is what this closes."""
    assert brand_mod.DEFAULTS[key].upper() == token(css).upper(), (
        f"DEFAULTS[{key!r}] and --{css} have drifted apart")


def test_the_example_file_is_the_default_theme():
    """`config/brand.example.json` is what a fork copies first. If it does
    not match the shipped defaults, copying it changes the app."""
    raw = json.loads((ROOT / "config" / "brand.example.json")
                     .read_text(encoding="utf-8"))
    for key in ("accent", "accent_ink", "background"):
        assert raw[key].upper() == brand_mod.DEFAULTS[key].upper(), (
            f"brand.example.json {key} is not the default")


# ---------------------------------------------------------------------------
# Contrast
# ---------------------------------------------------------------------------


def test_the_accent_carries_text_and_has_text_on_it():
    """Both directions, because the accent is used both ways: as a word in
    the headline, and as a filled control with a label on it."""
    accent, ink, page = token("accent"), token("accent-ink"), token("bg")
    assert contrast(accent, page) >= AA, (
        f"the accent reads {contrast(accent, page):.2f}:1 on the page "
        f"background, below AA ({AA}:1) - it is used for text")
    assert contrast(ink, accent) >= AA, (
        f"--accent-ink reads {contrast(ink, accent):.2f}:1 on the accent, "
        f"below AA ({AA}:1) - set accent_ink to something darker or lighter")


def test_white_on_the_accent_is_not_assumed():
    """A theme is free to pick an accent white cannot sit on - the previous
    one was exactly that at 2.86:1. What it may not do is use white there
    anyway, which is why `--accent-ink` exists and is asserted above.

    Read off the parsed rules rather than off the raw text: a comment that
    happens to contain the word "white" is not a declaration.
    """
    accent = token("accent")
    if contrast("#ffffff", accent) >= AA:
        return  # this accent happens to take white; nothing to prove
    whites = {"#fff", "#ffffff", "white"}
    for selector, body in rules():
        declared = dict(declarations(body))
        if "var(--accent)" not in declared.get("background", "") + declared.get(
                "background-color", ""):
            continue
        colour = declared.get("color", "").strip().lower()
        assert colour not in whites, (
            f"`{selector}` puts white on the accent, which this accent "
            f"cannot carry ({contrast('#ffffff', accent):.2f}:1); use "
            f"var(--accent-ink)")


@pytest.mark.parametrize("name", ["fg", "dim"])
def test_the_body_colours_clear_aa_on_the_page(name):
    assert contrast(token(name), token("bg")) >= AA


# ---------------------------------------------------------------------------
# Separation from the signal colours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("signal", ["warn", "bad", "bad-strong", "ok"])
def test_the_accent_is_not_mistakable_for_a_status(signal):
    """These four mean something. The accent means nothing - it is just the
    design - so it has to be clearly a different colour across a dim room.

    `--teal` is deliberately not in this list: nothing on the waiting screen
    is a status, so it is free to sit anywhere in the wheel."""
    gap = hue_gap(token("accent"), token(signal))
    assert gap >= MIN_HUE_GAP, (
        f"--accent is {gap:.0f}deg from --{signal}, under the {MIN_HUE_GAP}deg "
        f"floor; they will be confused on a projector")


# ---------------------------------------------------------------------------
# One surface colour on the projector
# ---------------------------------------------------------------------------


#: Properties that paint a surface. `mask-image` is deliberately absent: its
#: `#000` is an alpha stop meaning "fully opaque", not a colour anyone sees.
SURFACE_PROPERTIES = ("background", "background-color")

#: Rules that paint something on `/screen`.
SCREEN_SELECTORS = ("html.screen", "body.screen", ".titlecard",
                    ".screenfooter", ".screenmark", ".titlecard-lockup",
                    ".titlecard-wordmark", ".screenglow")

#: Rules that only apply while the operator's background glow is on. The
#: one-surface-colour rule is about the plain caption view, and these are
#: the surfaces that deliberately stop being plain.
GLOW_SELECTORS = ('[data-glow="1"]', ".screenglow")


def lit(selector: str) -> bool:
    return any(s in selector for s in GLOW_SELECTORS)


def screen_blocks() -> list[tuple[str, str]]:
    """`(selector, declarations)` for every rule that can paint on /screen."""
    found = [(selector, body) for selector, body in rules()
             if any(s in selector for s in SCREEN_SELECTORS)]
    assert found, "no /screen rules found; the parser is wrong"
    return found


def test_every_surface_on_the_screen_is_one_token():
    """`/screen` used to mix `#000` for the page and panes with a near-black
    tile. On a laptop that difference is invisible; on a calibrated projector
    it is a patch. Every painted surface names one token, so a theme changes
    the projector's colour by changing `background` in `config/brand.json`
    and nothing else.

    **With the background glow off**, which is what this rule is about. The
    glow is two soft radial washes the operator can switch on, and the
    surfaces over it go transparent on purpose so the wash is not cut into
    bands. Those rules are exempt here and held instead by the contrast floor
    in `test_screen_auto.py`, which measures the rendered projector.
    """
    allowed = {"var(--bg-brand)", "none", "transparent", "inherit"}
    for selector, body in screen_blocks():
        if lit(selector):
            continue
        for prop, value in declarations(body):
            if prop not in SURFACE_PROPERTIES:
                continue
            assert value.strip() in allowed, (
                f"`{selector}` paints `{prop}: {value.strip()}`; every "
                f"surface on /screen must be var(--bg-brand) unless the "
                f"glow is on")


def test_the_glow_is_the_only_thing_that_paints_a_second_colour():
    """The exemption above is narrow by construction: everything that stops
    being the one surface colour does so under `[data-glow="1"]`, so turning
    the switch off puts the caption view back to one surface.

    Without this, exempting a selector pattern would be a hole anyone could
    widen by naming a rule after the glow."""
    lit_rules = [(s, b) for s, b in screen_blocks() if lit(s)]
    assert lit_rules, "no glow rules found; the parser or the name changed"
    glow = {"var(--screen-glow)", "transparent", "none"}
    for selector, body in lit_rules:
        assert "data-glow" in selector or selector.endswith(".screenglow"), (
            f"`{selector}` is exempt from the one-surface rule but is not "
            f"gated on the glow switch")
        for prop, value in declarations(body):
            if prop not in SURFACE_PROPERTIES + ("background-image",):
                continue
            assert value.strip() in glow, (
                f"`{selector}` paints `{prop}: {value.strip()}`; a lit "
                f"surface is the glow token or nothing at all")


def test_no_near_black_literal_survives_in_a_screen_rule():
    """Belt to the braces above: catches a colour arriving through a property
    the surface list does not name, such as a gradient."""
    near_black = re.compile(r"#(?:0[0-9a-f]{5}|000\b|[01]{3}\b)",
                            re.IGNORECASE)
    for selector, body in screen_blocks():
        for prop, value in declarations(body):
            if "mask" in prop:          # an alpha stop, not a surface
                continue
            assert not near_black.search(value), (
                f"`{selector}` has the near-black literal "
                f"`{prop}: {value.strip()}`; use var(--bg-brand)")


def test_the_surface_is_not_pure_black():
    """Deliberate: see the token's own comment. #000 next to a near-black
    tile is the bug this guards."""
    assert token("bg-brand").upper() != "#000000"


# ---------------------------------------------------------------------------
# No artwork in the repository
# ---------------------------------------------------------------------------


def test_no_brand_image_is_served_from_the_committed_directory():
    """Artwork belongs in the gitignored `brand/`, reached through
    `/brand/{name}`. `web/` is committed, so an image served from there is
    an image in the repository."""
    for name in ASSETS:
        assert not name.endswith((".png", ".jpg", ".jpeg", ".svg", ".avif")), (
            f"{name} is served from web/ - brand images go in brand/")


@needs_git
def test_no_image_file_is_committed_anywhere():
    """The whole point of the split. Checked against git rather than the
    filesystem, so a local `brand/` full of artwork does not fail it."""
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT,
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    images = [line for line in result.stdout.split("\n")
              if line.strip().lower().endswith(
                  (".png", ".jpg", ".jpeg", ".avif", ".svg", ".webp"))]
    assert not images, f"image files are committed: {images}"


@pytest.mark.parametrize("name", sorted(brand_mod.SLOTS))
def test_every_slot_is_reachable_only_through_the_brand_route(name):
    assert name in BRAND_SLOTS
    assert name not in ASSETS, (
        f"{name} is in both allowlists; /static would serve it from web/")


def test_the_generated_stylesheet_is_optional():
    """`brand-sizes.css` is written by `tools/brand_assets.py` from somebody's
    artwork, so it is gitignored and a fresh clone has none. The stylesheet
    must therefore declare every variable it would have set."""
    generated = {"--wm-en-w", "--wm-en-h", "--wm-ar-w", "--wm-ar-h",
                 "--wm-ar-en-ratio", "--wm-ar-nudge"}
    root = STYLE.split(":root {", 1)[1].split("}", 1)[0]
    for variable in sorted(generated):
        assert f"{variable}:" in root, (
            f"{variable} has no default in style.css, so a clone with no "
            f"artwork would lay out with an invalid calc()")


# ---------------------------------------------------------------------------
# /brand.css
# ---------------------------------------------------------------------------


def test_a_missing_brand_file_is_the_normal_case(tmp_path):
    brand = brand_mod.load_brand(path=tmp_path / "nothing.json")
    assert brand["accent"] == brand_mod.DEFAULTS["accent"]
    assert brand["event_name"] == ""
    assert brand["waiting"] == {}


def test_malformed_json_falls_back_rather_than_failing(tmp_path):
    """A bad file must not stop the server coming up on the morning of a
    talk. A slightly wrong screen beats no screen."""
    bad = tmp_path / "brand.json"
    bad.write_text("{not json at all", encoding="utf-8")
    assert brand_mod.load_brand(path=bad)["accent"] == (
        brand_mod.DEFAULTS["accent"])


@pytest.mark.parametrize("value", [
    "red", "#ff", "#1234567", "rgb(1,2,3)", "", "   ",
    "#fff; } body { display: none",
    "#000000; background-image: url(http://evil.example/x)",
])
def test_a_colour_that_is_not_a_colour_is_ignored(tmp_path, value):
    """`/brand.css` interpolates these into a stylesheet, so an unvalidated
    string here is CSS injection with extra steps."""
    path = tmp_path / "brand.json"
    path.write_text(json.dumps({"accent": value}), encoding="utf-8")
    brand = brand_mod.load_brand(path=path)
    assert brand["accent"] == brand_mod.DEFAULTS["accent"], (
        f"{value!r} was accepted as a colour")
    css = brand_mod.brand_css(brand, directory=tmp_path)
    # The rejected string must not appear anywhere in the generated
    # stylesheet - not as a value, not smuggled through a comment.
    if value.strip():
        assert value.strip() not in css, f"{value!r} reached the stylesheet"
    assert "url(" not in css
    # And the file is still a well-formed stylesheet: every brace matched.
    assert css.count("{") == css.count("}")


def test_a_valid_colour_reaches_the_stylesheet(tmp_path):
    path = tmp_path / "brand.json"
    path.write_text(json.dumps({"accent": "#AABBCC"}), encoding="utf-8")
    css = brand_mod.brand_css(brand_mod.load_brand(path=path),
                              directory=tmp_path)
    assert "--accent: #AABBCC;" in css


def test_an_empty_slot_is_hidden_rather_than_drawn_broken(tmp_path):
    """A clone with no artwork must not show a row of broken images."""
    css = brand_mod.brand_css(brand_mod.DEFAULTS | {"waiting": {}},
                              directory=tmp_path)
    for selector in (".brandmark", ".titlecard-wordmark.en",
                     ".titlecard-wordmark.ar"):
        assert selector in css, f"{selector} is not hidden when it has no file"
    assert "display: none" in css


def test_a_present_slot_is_left_alone(tmp_path):
    (tmp_path / "mark.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    css = brand_mod.brand_css(brand_mod.DEFAULTS | {"waiting": {}},
                              directory=tmp_path)
    assert ".brandmark" not in css, "a slot with artwork was hidden anyway"
    assert ".titlecard-wordmark.en" in css, "the absent wordmark was not"


def test_the_corner_lockup_survives_a_missing_square_mark(tmp_path):
    """`.screenmark` is the corner lockup on /screen and is built out of the
    two wordmarks, not out of `mark.png`. Listing it against the mark hid the
    corner on any install that had wordmarks but no square mark."""
    for name in ("wordmark-en.png", "wordmark-ar.png"):
        (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    css = brand_mod.brand_css(brand_mod.DEFAULTS | {"waiting": {}},
                              directory=tmp_path)
    assert ".screenmark" not in css, (
        "the corner lockup was hidden although both wordmarks are present")
    assert ".brandmark" in css, "the absent square mark was not hidden"


def test_an_empty_corner_lockup_is_hidden(tmp_path):
    """With no wordmark artwork it is an empty box in the projector's top
    band, so it goes rather than reserving space for nothing."""
    css = brand_mod.brand_css(brand_mod.DEFAULTS | {"waiting": {}},
                              directory=tmp_path)
    assert ".screenmark { display: none; }" in css


def test_the_title_card_lockup_is_never_hidden(tmp_path):
    """The typed event name lives inside it, so hiding the container would
    take the fallback with it."""
    brand = brand_mod.DEFAULTS | {"event_name": "Some Event", "waiting": {}}
    css = brand_mod.brand_css(brand, directory=tmp_path)
    assert ".titlecard-lockup" not in css


def test_the_event_name_replaces_absent_wordmark_artwork(tmp_path):
    brand = brand_mod.DEFAULTS | {"event_name": "Some Event", "waiting": {}}
    css = brand_mod.brand_css(brand, directory=tmp_path)
    assert ".brand-eventname { display: block; }" in css


def test_the_event_name_does_not_compete_with_artwork(tmp_path):
    """One or the other, never both: an event that supplies a wordmark has
    already said how its name should look."""
    (tmp_path / "wordmark-en.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    brand = brand_mod.DEFAULTS | {"event_name": "Some Event", "waiting": {}}
    css = brand_mod.brand_css(brand, directory=tmp_path)
    assert ".brand-eventname { display: block; }" not in css


def test_no_name_and_no_artwork_shows_nothing(tmp_path):
    css = brand_mod.brand_css(brand_mod.DEFAULTS | {"waiting": {}},
                              directory=tmp_path)
    assert ".brand-eventname { display: block; }" not in css


def test_the_name_element_is_hidden_by_default():
    """`/brand.css` switches it on. Without that rule it must stay dark, or
    a page would paint a name before the stylesheet loaded."""
    assert ".brand-eventname { display: none; }" in STYLE


def test_slot_names_from_a_request_never_reach_the_filesystem(tmp_path):
    for attempt in ["../.env", "..\\.env", "mark.png/../../.env",
                    "/etc/passwd", "anything.png", ""]:
        assert brand_mod.image_path(attempt, tmp_path) is None, (
            f"{attempt!r} resolved to a path")


# ---------------------------------------------------------------------------
# The pages are wired to the theme
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_page_loads_the_generated_theme(page):
    assert "/brand.css" in PAGES[page], f"{page} does not load /brand.css"


@pytest.mark.parametrize("page", sorted(PAGES))
def test_the_theme_is_loaded_after_the_stylesheet(page):
    """It overrides tokens `style.css` declares, so order decides."""
    html = PAGES[page]
    if "/static/style.css" not in html:
        pytest.skip(f"{page} does not load style.css")
    assert html.index("/brand.css") > html.index("/static/style.css"), (
        f"{page} loads /brand.css before style.css, so the theme loses")


@pytest.mark.parametrize("page", sorted(PAGES))
def test_no_page_references_a_brand_image_under_static(page):
    assert "/static/logo.png" not in PAGES[page]
    assert "/static/wordmark" not in PAGES[page]
    assert "/static/favicon" not in PAGES[page]
    assert "/static/apple-touch" not in PAGES[page]


@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_page_has_a_favicon(page):
    assert "/brand/favicon-32.png" in PAGES[page], f"{page} has no favicon"


def test_no_wordmark_carries_its_size_in_the_markup():
    """Dimensions in the markup would be a second copy of the artwork's own,
    stale the moment a bigger file lands. The CSS reserves the box from
    `aspect-ratio` instead."""
    for tag in re.findall(r"<img[^>]*wordmark-(?:en|ar)\.png[^>]*>",
                          SCREEN_HTML):
        assert "width=" not in tag and "height=" not in tag, (
            f"a wordmark carries hard-coded dimensions: {tag}")


@pytest.mark.parametrize("variant,variable", [
    (".titlecard-wordmark.en", "--wm-en-w"),
    (".titlecard-wordmark.ar", "--wm-ar-w")])
def test_each_wordmark_box_is_reserved_from_the_artwork(variant, variable):
    block = STYLE.split(variant + " {", 1)[1].split("}", 1)[0]
    assert "aspect-ratio" in block, f"{variant} does not reserve its box"
    assert variable in STYLE


def test_the_waiting_screen_words_come_from_the_theme():
    """Every word on the poster is overridable, which is the whole promise of
    `config/brand.json`."""
    from app.broadcast import DEFAULT_SCREEN

    example = json.loads((ROOT / "config" / "brand.example.json")
                         .read_text(encoding="utf-8"))
    for key in example["waiting"]:
        assert key in DEFAULT_SCREEN, (
            f"brand.example.json sets {key!r}, which the screen ignores")


def test_the_theme_carries_no_secret():
    """`/brand.json` is public by construction - a roomful of people read it
    off a projector. Nothing from `.env` may be in it."""
    assert "api_key" not in brand_mod.DEFAULTS
    assert "token" not in brand_mod.DEFAULTS
    for key in brand_mod.DEFAULTS:
        assert "secret" not in key and "key" not in key
