"""Screenshot the main screen in each state it matters in.

    uv run --with playwright python tools/screen_screenshots.py
    uv run --with playwright python tools/screen_screenshots.py --real

Drives the **real** `/screen` page - the shipped HTML, stylesheet, app.js and
auto.js - with only the viewer WebSocket faked, so the snapshots the page
folds in are invented rather than recorded. Nothing here touches Soniox, and
nothing here needs the server: the page is served from `web/` over a throwaway
static server and the socket is replaced before any script runs.

**Every word on screen is invented**, in the same spirit as
`tools/admin_screenshots.py`. A screenshot taken from a real run shows a
verbatim transcript of whatever was said in the room, which is the same
judgment as `tests/fixtures/*.jsonl`: those belong in a gitignored directory,
which is gitignored. The shots this tool writes are the ones that get
committed.

Playwright is deliberately **not** a project dependency (the design brief section
2); `uv run --with playwright` keeps it out of `pyproject.toml`.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.broadcast import BroadcastHub       # noqa: E402
from app.captions import CaptionState        # noqa: E402
from app.invite_qr import DEFAULT_LINK, resolve  # noqa: E402
from app.invite_qr import render as render_qr  # noqa: E402

WEB = ROOT / "web"
OUT = ROOT / "docs" / "screens"
#: The pair every page is shot at: a projector and a phone.
SIZES = {"1920x1080": {"width": 1920, "height": 1080},
         "390x844": {"width": 390, "height": 844}}

#: The other projector resolution worth a shot of its own. The title card
#: is the one thing whose layout is tighter here than at 1080p: the lockup
#: is capped at the artwork's own width, so it is the same 793px of marks
#: inside a card that is 564px narrower.
SMALL_PROJECTOR = {"width": 1280, "height": 720}
SIZE = SIZES["1920x1080"]

#: An invented exchange: an English speaker opening a talk, an Arabic
#: speaker asking a question, and the answer. Written for these screenshots -
#: it is nobody's speech, and the `auto` stream shows only the *translated*
#: side, so an English turn appears in Arabic and vice versa.
BLOCKS = [
    # The host, speaking English -> shown in Arabic.
    {"id": 1, "lang": "ar", "dir": "rtl",
     "final": "مرحبًا بالجميع في لقاء اليوم. سنتحدث عن بناء الأنظمة التي "
              "تعمل في الوقت الحقيقي، وكيف نقيس زمن الاستجابة من طرف إلى "
              "طرف بدلًا من قياس كل خدمة على حدة."},
    {"id": 2, "lang": "ar", "dir": "rtl",
     "final": "سنبدأ بنظرة عامة على البنية، ثم ننتقل إلى الأسئلة في النصف "
              "الثاني من الجلسة."},
    # A guest, speaking Arabic -> shown in English. The direction change.
    {"id": 3, "lang": "en", "dir": "ltr",
     "final": "Thank you. My question is about the retrieval layer: how do "
              "you keep the index fresh when the underlying data changes "
              "every few minutes?"},
    # Back to the host.
    {"id": 4, "lang": "ar", "dir": "rtl",
     "final": "سؤال جيد. نحدّث الفهرس على دفعات صغيرة بدل إعادة بنائه "
              "بالكامل، ونحتفظ بنسخة سابقة حتى تكتمل الدفعة."},
]

#: The last block arrives mid-reveal in the "listening" shot, so the screen
#: shows a line still filling in and then a pause - which is exactly when the
#: dots are supposed to appear.
PARTIAL_TAIL = "This is the part that is still arriving"

STUB = """
(() => {
  class FakeSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      window.__socket = this;
      setTimeout(() => {
        this.readyState = 1;
        if (this.onopen) this.onopen({});
        window.__open = true;
      }, 0);
    }
    send() {}
    close() {
      this.readyState = 3;
      if (this.onclose) this.onclose({});
    }
  }
  FakeSocket.prototype.deliver = function (obj) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) });
  };
  window.WebSocket = FakeSocket;
})();
"""


#: What the committed screenshots show in the card: a code for the
#: organizer's public website, drawn the same way the server would draw it.
#:
#: **Never the real QR.** `config/qr.svg` encodes the group invite and
#: `config/screen.yaml` holds the link; both are gitignored, and a committed
#: screenshot of either is that invite published. `--real` writes one shot
#: of the live code into a gitignored directory, which is gitignored too -
#: that is the only way the real card gets photographed.
SCREENSHOT_LINK = DEFAULT_LINK

#: An empty directory to resolve the QR source against, so these pictures
#: describe a fresh install and can never pick up this laptop's `qr.svg`.
EMPTY_STATE = Path(tempfile.mkdtemp(prefix="captions-shots-"))

#: Set by `--real`: the organizer's own artwork, served instead of a drawn
#: code, for the local-only shot.
REAL_QR: bytes | None = None


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):          # noqa: N802 - the base class names it
        """The one route that is not a file.

        The server draws the QR, so a static copy of `web/` cannot serve it.
        Drawing it here with the same function keeps the screenshot honest:
        what this writes is the code the projector would show.
        """
        if self.path.split("?", 1)[0] == "/screen/qr.svg":
            body = REAL_QR if REAL_QR is not None else (
                render_qr(SCREENSHOT_LINK).encode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/", "/index.html"):
            return str(WEB / "index.html")
        if path == "/screen":
            return str(WEB / "screen.html")
        if path.startswith("/static/"):
            return str(WEB / path[len("/static/"):])
        return str(WEB / path.lstrip("/"))

    def log_message(self, *args):
        pass


def serve() -> tuple[str, socketserver.TCPServer]:
    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


#: Invented, and obviously so. Never a real name.
SAMPLE_CREDIT = "Live translation by the captions team"


def chrome(**overrides) -> dict:
    """The screen settings a snapshot carries, with the defaults filled in.

    Built by the real hub rather than by a dict written out here, so the
    defaults in these pictures are the defaults a room gets - including the
    waiting screen's words, which exist in exactly one place
    (`broadcast.DEFAULT_SCREEN`) and should not be copied into a second.
    `public_screen` is also what strips the invite link, so this tool cannot
    accidentally screenshot one.
    """
    hub = BroadcastHub(CaptionState())
    hub.set_screen(**overrides)
    # Resolved against a directory that is deliberately empty, so these
    # pictures describe a fresh install rather than this laptop: no
    # `qr.svg`, no saved invite, and therefore the default link.
    hub.set_qr(resolve("", state_dir=EMPTY_STATE))
    return hub.public_screen()


def snapshot(blocks, *, live=True, seq=10, status="live", **screen) -> dict:
    return {
        "type": "snapshot", "seq": seq, "live": live, "status": status,
        "streams": {"en": [], "ar": [], "auto": list(blocks)},
        "screen": chrome(**screen),
    }


def deliver(page, message: dict) -> None:
    page.wait_for_function("window.__open === true")
    page.evaluate("m => window.__socket.deliver(m)", message)


def load_invite() -> str:
    """The operator's saved link, for `--real` only."""
    from app.settings import load_screen

    return str(load_screen().get("invite") or "")


def main() -> int:
    global OUT, REAL_QR

    from playwright.sync_api import sync_playwright

    real = "--real" in sys.argv
    if real:
        # The live card, including the organizer's own artwork if it is
        # there. Straight into a gitignored directory, which is gitignored,
        # because this is a picture of the group invite.
        from app.settings import STATE_DIR
        source = resolve(load_invite())
        if source.from_file:
            REAL_QR = source.path.read_bytes()
        else:
            REAL_QR = render_qr(source.url).encode("utf-8")
        print(f"using the live QR: {source.label()}  (state dir {STATE_DIR})")
        OUT = ROOT / "docs" / "screens" / "local"

    OUT.mkdir(parents=True, exist_ok=True)
    base, httpd = serve()
    written = []

    with sync_playwright() as play:
        browser = play.chromium.launch()

        def fresh(path="/screen", viewport=SIZE, before=None):
            context = browser.new_context(viewport=viewport,
                                          device_scale_factor=2)
            context.add_init_script(STUB)
            if before:
                context.add_init_script(before)
            page = context.new_page()
            page.goto(base + path, wait_until="domcontentloaded")
            return context, page

        # 1. The title card: live, but nothing to show yet. That gap - the
        #    first word waiting on its translation - is the card's whole
        #    job now that the waiting screen has the before and the after.
        context, page = fresh()
        deliver(page, snapshot([], live=True, seq=0, status="live",
                               credit=SAMPLE_CREDIT))
        page.wait_for_timeout(900)          # let it fade in
        page.screenshot(path=str(OUT / "screen-title-card.png"))
        written.append("screen-title-card.png")
        context.close()

        # ...and at 720p, where the lockup is the same width of artwork in
        # a narrower card and so is the tighter fit of the two. The row
        # must still be one line here.
        context, page = fresh(viewport=SMALL_PROJECTOR)
        deliver(page, snapshot([], live=True, seq=0, status="live",
                               credit=SAMPLE_CREDIT))
        page.wait_for_timeout(900)
        page.screenshot(path=str(OUT / "screen-title-card-1280x720.png"))
        written.append("screen-title-card-1280x720.png")
        context.close()

        # The same card without the credit line, which is the default.
        context, page = fresh()
        deliver(page, snapshot([], live=True, seq=0, status="live"))
        page.wait_for_timeout(900)
        page.screenshot(path=str(OUT / "screen-title-card-plain.png"))
        written.append("screen-title-card-plain.png")
        context.close()

        # 1b. The waiting screen: what the room looks at before Start and
        #     after Stop, at both projector sizes. The card is always there,
        #     so there is no second layout to photograph.
        for name, viewport in (
            ("screen-waiting-1920x1080.png", SIZE),
            ("screen-waiting-1280x720.png", SMALL_PROJECTOR),
        ):
            context, page = fresh(viewport=viewport)
            deliver(page, snapshot([], live=False, seq=0, status="idle"))
            # The fade, and then the code: an <img> that has not decoded yet
            # is a white card in the picture.
            page.wait_for_timeout(900)
            page.wait_for_function(
                "() => { const i = document.getElementById('wait-qr');"
                "        return i && i.complete && i.naturalWidth > 0; }",
                timeout=10000)
            page.screenshot(path=str(OUT / name))
            written.append(name)
            context.close()

        # 2. A direction change: Arabic above, the rule, English below.
        context, page = fresh()
        deliver(page, snapshot(BLOCKS[:3]))
        page.wait_for_timeout(500)
        page.evaluate("() => window.CaptionScreen.finishReveal()")
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / "screen-direction-change.png"))
        written.append("screen-direction-change.png")
        context.close()

        # 3. Listening: text on screen, a line still mid-reveal, then a gap
        #    long enough for the dots to come up.
        context, page = fresh()
        deliver(page, snapshot(BLOCKS[:2]))
        page.wait_for_timeout(400)
        page.evaluate("() => window.CaptionScreen.finishReveal()")
        deliver(page, {
            "type": "diff", "seq": 11, "base": 10, "live": True,
            "status": "live",
            "streams": {"en": [], "ar": [], "auto": [
                {"id": 3, "final": PARTIAL_TAIL, "partial": "",
                 "lang": "en", "dir": "ltr"}]},
            "screen": {"view": "auto", "size": "l"},
        })
        # Long enough for the reveal to settle and the dots to earn their
        # place (screen.js holds them back for LISTENING_AFTER_MS).
        page.wait_for_function(
            "() => !document.getElementById('listening').hidden", timeout=15000)
        page.screenshot(path=str(OUT / "screen-listening.png"))
        written.append("screen-listening.png")
        context.close()

        # 4. Reconnecting: the dot, the captions, and nothing else.
        context, page = fresh()
        deliver(page, snapshot(BLOCKS))
        page.wait_for_timeout(500)
        page.evaluate("() => window.CaptionScreen.finishReveal()")
        page.evaluate("() => window.__socket.close()")
        page.wait_for_function(
            "() => !document.getElementById('linkdot').hidden", timeout=15000)
        page.screenshot(path=str(OUT / "screen-reconnecting.png"))
        written.append("screen-reconnecting.png")
        context.close()

        # 5. The two pages at both sizes, for the record.
        for label, viewport in SIZES.items():
            # The audience page, in Auto so the branding and the
            # translation-only column are both in frame.
            context, page = fresh(
                "/", viewport,
                before="try{localStorage.setItem('captions.view','auto')}"
                       "catch(e){}")
            deliver(page, snapshot(BLOCKS))
            page.wait_for_timeout(500)
            page.evaluate("""() => {
              const s = document.getElementById('stream-auto');
              if (s) { s.scrollTop = s.scrollHeight; }
            }""")
            page.wait_for_timeout(900)
            page.screenshot(path=str(OUT / f"audience-{label}.png"))
            written.append(f"audience-{label}.png")
            context.close()

            # The main screen, with the corner marks turned on so the one
            # place they can appear is on the record too.
            context, page = fresh("/screen?logo=1", viewport)
            deliver(page, snapshot(BLOCKS, logo=True))
            page.wait_for_timeout(500)
            page.evaluate("() => window.CaptionScreen.finishReveal()")
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / f"screen-auto-{label}.png"))
            written.append(f"screen-auto-{label}.png")
            context.close()

        # 6. The caption view both ways round, which is the pair to look at
        #    together: the background glow is the operator's switch, and
        #    the question it answers is whether the branding costs the
        #    words anything. Same text, same marks, one setting apart.
        for name, query, lit in (
            ("screen-auto-glow-on.png", "/screen?logo=1&glow=1", True),
            ("screen-auto-glow-off.png", "/screen?logo=1&glow=0", False),
        ):
            context, page = fresh(query, SIZE)
            deliver(page, snapshot(BLOCKS, logo=True, glow=lit))
            page.wait_for_timeout(500)
            page.evaluate("() => window.CaptionScreen.finishReveal()")
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / name))
            written.append(name)
            context.close()

        browser.close()

    httpd.shutdown()
    for name in written:
        print(f"wrote {OUT.relative_to(ROOT).as_posix()}/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
