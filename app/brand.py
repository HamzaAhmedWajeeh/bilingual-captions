"""Branding, loaded from one gitignored file so the repo carries none of it.

Everything an event puts its name on lives in two places, neither of which is
committed:

* ``config/brand.json``  - the words and the colours. ``config/brand.example.json``
  is the committed shape, and it holds the neutral default theme.
* ``brand/``             - the images. Logos and wordmarks, dropped in by hand.

Both are optional. With neither present the app runs on the neutral defaults
below and looks finished: the accent is a blue chosen for contrast rather than
for a look, the waiting screen carries generic wording from
``broadcast.DEFAULT_SCREEN``, and the title card falls back to the typed
``event_name`` - or, with no name either, to nothing at all rather than a
broken image.

The reason the images are a *directory* rather than keys in the JSON is that
they are binaries an organiser hands over, usually more than once. A folder
they can be dropped into, with fixed slot names, means replacing artwork is a
file copy and never an edit.

Nothing here is a secret. The API key and the admin token come from ``.env``
and are the only two values in this project that are - see ``app/settings.py``.
Brand values are read by a roomful of people on a projector, which is the
whole point of them, so this module makes no attempt to keep them private.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = ROOT / "brand"
CONFIG_DIR = ROOT / "config"

#: A six-digit hex colour and nothing else. Anything that does not match is
#: ignored rather than passed through: these values are interpolated into a
#: stylesheet that the server generates, and an unvalidated string there is a
#: CSS injection with extra steps.
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: The neutral default theme. These are the same values `web/style.css`
#: declares, repeated here because this module is what answers "what colour
#: is the accent" for `/brand.css`, and a default that lived only in the
#: stylesheet could not be reported.
#:
#: `accent` reads 7.5:1 against the page background, and `accent_ink` reads
#: the same 7.5:1 on top of the accent - so text on the accent is as legible
#: as text beside it. A theme that moves the accent somewhere pale should set
#: `accent_ink` as well; `tests/test_brand.py` asserts the pair stays legible
#: whatever they are set to.
DEFAULTS: dict[str, object] = {
    "event_name": "",
    "accent": "#4DA3FF",
    "accent_ink": "#0B0B0D",
    "background": "#0B0B0D",
    "event_url": "",
}

#: The image slots, and the file name each one is served under. A fixed set,
#: because `/brand/{name}` resolves a flat name inside `brand/` and nothing
#: else: no path from a request ever reaches the filesystem, so a directory
#: holding whatever an organiser sent cannot be walked over HTTP.
#:
#: Each slot may be absent. `brand_css` emits a rule hiding the element that
#: would have shown it, which is why a clone with no artwork shows type
#: rather than a row of broken images.
SLOTS: dict[str, str] = {
    "mark.png": "image/png",
    "wordmark-en.png": "image/png",
    "wordmark-ar.png": "image/png",
    "favicon-32.png": "image/png",
    "apple-touch-icon-180.png": "image/png",
}

#: Which CSS selector goes dark when a slot has no file behind it.
#:
#: One slot, one element that draws it. `.screenmark` is deliberately **not**
#: here: it is the corner lockup on `/screen`, which is built out of the two
#: wordmarks rather than out of `mark.png`, and listing it against the mark
#: hid the corner on any install that had wordmarks but no square mark. It is
#: handled by `_EMPTY_LOCKUP` below instead.
_SLOT_SELECTOR = {
    "mark.png": ".brandmark",
    "wordmark-en.png": ".titlecard-wordmark.en",
    "wordmark-ar.png": ".titlecard-wordmark.ar",
}

#: The wordmark slots, as a set, because several rules below turn on "has this
#: install any wordmark artwork at all".
_WORDMARKS = frozenset({"wordmark-en.png", "wordmark-ar.png"})

#: Containers that hold **only** wordmarks, so with no wordmark artwork they
#: are an empty box rather than a lockup. `.titlecard-lockup` is not one of
#: them: the typed event name lives inside it, which is the whole fallback.
_EMPTY_LOCKUP = ".screenmark"

_COLOUR_TOKEN = {
    "accent": "--accent",
    "accent_ink": "--accent-ink",
    "background": "--bg-brand",
}


def brand_path(path: Path | None = None) -> Path:
    return Path(path) if path else CONFIG_DIR / "brand.json"


def load_brand(path: Path | None = None) -> dict:
    """The merged theme: the defaults, with whatever the file overrides.

    Deliberately forgiving. A missing file is the normal case on a fresh
    clone, and a malformed one must not stop the server coming up on the
    morning of a talk - a bad value is dropped and the default stands, which
    is a screen that looks slightly wrong rather than no screen at all.

    Colours are validated here, not at the point of use, so there is one
    answer to "is this a colour" and `/brand.css` can interpolate without
    thinking about it.
    """
    data: dict = {}
    target = brand_path(path)
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}

    brand = dict(DEFAULTS)
    for key in DEFAULTS:
        value = data.get(key)
        if value is None:
            continue
        if key in _COLOUR_TOKEN:
            if isinstance(value, str) and HEX.match(value.strip()):
                brand[key] = value.strip()
        elif isinstance(value, str):
            brand[key] = value.strip()

    # The waiting screen's words. Unvalidated beyond being strings, because
    # `BroadcastHub` already caps and tidies every one of them on the way in
    # - see `MAX_SCREEN_TEXT` - and it is the only thing that knows the caps.
    waiting = data.get("waiting")
    brand["waiting"] = {
        str(k): str(v) for k, v in waiting.items()
        if isinstance(v, (str, int, float))
    } if isinstance(waiting, dict) else {}

    return brand


def image_path(name: str, directory: Path | None = None) -> Path | None:
    """The file behind one slot, or `None` if the theme has no artwork for it.

    `name` must be one of `SLOTS`. Anything else returns `None` rather than
    raising, so a stale tab asking for a slot that no longer exists gets a
    404 from the route instead of a 500.
    """
    if name not in SLOTS:
        return None
    candidate = (directory or BRAND_DIR) / name
    return candidate if candidate.is_file() else None


def present_slots(directory: Path | None = None) -> set[str]:
    return {name for name in SLOTS if image_path(name, directory)}


def brand_css(brand: dict | None = None,
              directory: Path | None = None) -> str:
    """The stylesheet that turns a theme into the running design.

    Two jobs, and it is one file because they have to agree:

    1. Override the colour tokens `web/style.css` declares.
    2. Hide the image slots this theme has no artwork for, and show the typed
       event name in their place.

    The second is why this is generated rather than static. Only the server
    can see which files are in `brand/`, so only the server can say whether
    the title card should draw a wordmark or set a name in type. A page that
    guessed would flash the wrong one on every load.

    Served with `no-store`, like every other page here: an operator who drops
    new artwork in and reloads must see it, not yesterday's from a cache.
    """
    brand = load_brand() if brand is None else brand
    present = present_slots(directory)
    lines = [
        "/* Generated by app/brand.py from config/brand.json and brand/.",
        " * Not a file to edit - change the theme, not this. */",
        ":root {",
    ]
    for key, token in _COLOUR_TOKEN.items():
        value = str(brand.get(key, ""))
        if HEX.match(value):
            lines.append(f"  {token}: {value};")
    lines.append("}")

    missing = [name for name in _SLOT_SELECTOR if name not in present]
    for name in missing:
        lines += [
            f"/* no {name} in brand/ - nothing to draw */",
            f"{_SLOT_SELECTOR[name]} {{ display: none; }}",
        ]

    if not (_WORDMARKS & present):
        # The corner lockup on `/screen` is made of wordmarks and nothing
        # else, so with none it is an empty box in the top band.
        lines += [
            "/* no wordmark artwork - the corner lockup has nothing in it */",
            f"{_EMPTY_LOCKUP} {{ display: none; }}",
        ]
        # The title card is the exception: the typed name takes over there.
        # One or the other, never both - an event that supplies a wordmark
        # has already said how its name should look.
        if brand.get("event_name"):
            lines += [
                "/* the event name is set in type instead */",
                ".brand-eventname { display: block; }",
            ]
    return "\n".join(lines) + "\n"
