"""The waiting screen's QR code: one URL in, one SVG out.

The code is drawn here rather than in the browser so the page has nothing to
download from anywhere but this server - the venue projector may have no
internet at all, and a QR that silently fails to render is a blank white card
in front of a room.

**The invite URL is a local value.** It lives in `config/screen.yaml`, which
is gitignored, for the same reason `config/session.yaml` is: a WhatsApp group
link is an open door for anyone holding it, and **must never be committed**.
It reaches exactly two places - `/admin` behind its token, and the pixels of
this image. It is deliberately *not* carried on the viewer socket: `/screen`
asks for the image by URL and never learns the string inside it.

Nothing here touches the network. `segno` is pure Python with no dependencies
of its own, which is why it was the one addition worth asking for.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

#: A join link, with room for a long one. WhatsApp's own invites are about 45
#: characters; the cap is there so a paste accident cannot turn into a QR so
#: dense that nothing in the room can read it.
MAX_INVITE_CHARS = 300

#: Where the room is sent when nothing else is set. The card is part of the
#: layout now - it is always on screen - so there has to be something in it.
#: This is a placeholder on a reserved domain: set `event_url` in
#: `config/brand.json`, or type a join link on `/admin`, and that wins.
DEFAULT_LINK = "https://example.com"

#: The organizer's own artwork, dropped into the state directory by hand.
#: SVG first: it is resolution-independent, which on a 4K projector is the
#: difference between crisp modules and a resampled grid.
FILE_NAMES = (("qr.svg", "image/svg+xml"), ("qr.png", "image/png"))

#: What a QR can come from, in the order they are tried.
FILE = "file"
GENERATED = "generated"
DEFAULT = "default"


class QrSource:
    """Which QR the projector is showing, and where it comes from.

    Three sources, and the operator should be able to see which one is
    live - a card that silently shows the fallback because a file was
    dropped in the wrong place looks exactly like one that is working.
    """

    def __init__(self, kind: str, *, path=None, media_type: str = "image/svg+xml",
                 url: str = "", tag: str = "") -> None:
        self.kind = kind
        self.path = path
        self.media_type = media_type
        self.url = url
        self.tag = tag

    @property
    def extension(self) -> str:
        return "png" if self.media_type == "image/png" else "svg"

    @property
    def from_file(self) -> bool:
        return self.kind == FILE

    def label(self) -> str:
        """For `/admin`. Says what is on screen, never what is in it."""
        if self.kind == FILE:
            return f"the organizer's {self.path.name}"
        if self.kind == GENERATED:
            return "generated from the invite link"
        return f"generated from {DEFAULT_LINK} (no link set)"


def resolve(invite: str, *, state_dir=None) -> QrSource:
    """Pick the QR to serve: the operator's file, their link, or the default.

    The file wins because it is the more deliberate act - somebody put it
    there - and because it may be artwork the organizer has already printed
    and handed round, which a regenerated code would silently replace with a
    different one.
    """
    from app.settings import STATE_DIR

    directory = Path(state_dir) if state_dir is not None else STATE_DIR
    for name, media_type in FILE_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return QrSource(FILE, path=candidate, media_type=media_type,
                            # The file's own bytes, so replacing the artwork
                            # changes the image's URL and every open
                            # projector refetches it.
                            tag=_digest(candidate.read_bytes()))
    link = clean_invite(invite)
    if link:
        return QrSource(GENERATED, url=link, tag=tag(link))
    return QrSource(DEFAULT, url=DEFAULT_LINK, tag=tag(DEFAULT_LINK))


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:8]

#: Only the two schemes a phone's camera will actually offer to open. A
#: `javascript:` or `data:` URL in a QR code is a trick played on whoever
#: scans it, and the operator types this into a web form, so the check is
#: cheap and the failure mode - no QR at all - is the safe one.
_SCHEME = re.compile(r"^https?://\S", re.IGNORECASE)

#: Quiet zone, in modules. Four is what the spec asks for, and it is what
#: makes a code readable from a few metres rather than from arm's length -
#: the white card around it adds more on top.
BORDER = 4

#: Error correction level. **M, not H**, and this is a screen-reading
#: decision rather than a data one.
#:
#: A code is read from across a room, where the thing that decides whether a
#: phone can resolve it is how many pixels each module gets. H carries 30%
#: recovery and pays for it in modules: a 47-character invite needs 41x41 at
#: H and 33x33 at M, so in the same card every module is **24% wider**.
#: Recovery buys nothing here anyway - the code is drawn on a screen, not
#: printed, so there is no crease or coffee ring to recover from, and
#: nothing is laid over the middle of it any more.
ERROR_LEVEL = "m"


def clean_invite(value: object) -> str:
    """The operator's typed URL, tidied, or `""` if it is not usable.

    Ignoring a bad value rather than rejecting it is the same rule the rest
    of the screen settings follow: this is a control that can change what a
    room sees, and the failure mode of a typo should be "no QR", never "the
    projector went away".
    """
    text = " ".join(str(value or "").split())[:MAX_INVITE_CHARS]
    return text if _SCHEME.match(text) else ""


def tag(url: str) -> str:
    """A short fingerprint of the URL, for cache-busting the image.

    The page requests `/screen/qr.svg?v=<tag>`, so an operator who changes
    the link mid-setup gets a new URL and therefore a new image, with no
    cache header subtle enough to get wrong. It is a hash, not the link:
    eight hex characters of SHA-256 tell a viewer nothing about what was
    hashed, which is what lets this one ride on the viewer socket.
    """
    if not url:
        return ""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]


def render(url: str) -> str:
    """The code as a standalone SVG string, sized in module units.

    `viewBox` is the module grid and nothing carries a pixel size, so the
    page scales it with CSS and it is sharp on a 4K projector and on a
    1280x720 one alike.

    **Nothing is drawn over the middle.** There was a WhatsApp mark there
    and it is gone: a code on a screen, read from a distance, needs every
    module it has, and the line beside the card already says where the link
    goes. Decoding software tolerated the mark comfortably; a phone camera
    across a room is the test that matters, and it is a harder one.
    """
    import segno

    code = segno.make(url, error=ERROR_LEVEL, micro=False)
    matrix = [list(row) for row in code.matrix]
    modules = len(matrix)
    side = modules + 2 * BORDER

    # One path for every dark module rather than a few hundred <rect>
    # elements: a quarter of the bytes, and one less thing for a renderer to
    # anti-alias into grey at the edges.
    parts = []
    for y, row in enumerate(matrix):
        for x, bit in enumerate(row):
            if bit:
                parts.append(f"M{x + BORDER} {y + BORDER}h1v1h-1z")

    # Pure black on pure white, which is not the brand black. This is the
    # one surface in the project whose colour is a function of what a phone
    # camera can do rather than of what the artwork says, and the contrast
    # it wants is the maximum available.
    #
    # `crispEdges` turns off anti-aliasing: a module smeared into grey at
    # its edges is a module a camera has to guess at, and at projector
    # distance that is the whole margin.
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}"'
        ' shape-rendering="crispEdges" role="img"'
        ' aria-label="Scan to join the group">'
        f'<rect width="{side}" height="{side}" fill="#ffffff"/>'
        f'<path fill="#000000" d="{"".join(parts)}"/>'
        '</svg>'
    )
