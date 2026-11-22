"""Viewer WebSocket hub: one `CaptionState`, many phones.

The hub never parses Soniox and never builds caption text. It decides only
*who* gets *which* snapshot or diff, and *when*; assembly stays in
`captions.py`, which is pure. So this file is about fan-out and backpressure.

Three requirements from the design brief, Phase 3, shape it:

- **"Throttle broadcasts to at most 10 per second; coalesce diffs in
  between."** State changes far faster than that - run1 moves `CaptionState`
  1,247 times in 300 s - so the loop sleeps `1/hz` after every flush and lets
  `diff_since` collapse everything that happened during the sleep into one
  message.
- **"Drop the slowest if its send buffer backs up."** Every viewer owns a
  writer task and a one-message slot. A phone still writing simply misses a
  flush; because its `last_seq` has not advanced, the next flush hands it a
  *wider* diff rather than a backlog of narrow ones. Only a viewer that
  accepts nothing for `stall_timeout` is closed, and its own reconnect brings
  it back with a fresh snapshot.
- **"Read-only: ignore any client message except `{"type":"resync"}`."** A
  resync does not write from the receive path - two concurrent `send_text`
  calls on one socket interleave frames - it raises a flag the next flush
  honours. Every byte a viewer receives comes from its own writer task.

`flush()` does no socket I/O: it only fills slots. That keeps the broadcast
loop immune to one slow phone, and makes the hub testable with a fake socket
and no event-loop tricks (`tests/test_broadcast.py`).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Callable, Protocol

from app.captions import AUTO, CaptionState
from app.invite_qr import clean_invite

#: The design brief: "Limit to 500 concurrent viewer sockets."
MAX_VIEWERS = 500

#: The design brief: "at most 10 per second".
BROADCAST_HZ = 10

#: Heartbeat on the viewer socket. An application-level `{"type":"ping"}`
#: rather than a WebSocket control frame: Starlette exposes no API for
#: protocol pings, and an application ping is the more useful signal anyway -
#: it proves the caption path is alive, not just the socket, and it stops the
#: Cloudflare tunnel reaping an idle connection between talks.
#:
#: **the design brief says 20 s; this is 5.** On run9 (2026-10-13) the laptop's
#: Wi-Fi was switched off and the phone's socket died without either end
#: sending a FIN, so the phone sat in `OPEN` believing it was connected and
#: showed the same frozen line for the rest of the talk. The client now
#: reconnects when it has heard nothing for 12 s, and that watchdog is only
#: as good as the heartbeat feeding it: at 20 s the page would have to sit
#: dead for most of a minute before noticing. Five seconds costs each phone
#: about 20 bytes every five seconds.
PING_INTERVAL = 5.0

#: What the operator may put on the projector, and how big. Carried on every
#: snapshot and diff so a `/screen` page picks it up the moment it connects
#: and whenever it changes, without the projector browser being touched.
#:
#: It lives on the hub rather than on `CaptionState` because the state is
#: rebuilt for every session (`Session._begin` makes a new one and calls
#: `rebind`) and the main-screen setting has to outlive that - it is set once
#: before the talk, not once per start.
SCREEN_VIEWS = ("auto", "en", "ar", "both")
SCREEN_SIZES = ("s", "m", "l", "xl")
#: What the projector shows besides captions. Everything here is chrome, and
#: the captions are the point, so each piece either stays out of the caption
#: area entirely or is off by default.
#:
#: - `logo`   the mark in a top corner. Off: a logo sitting over the text all
#:            talk is worse than no logo.
#: - `footer` the dim bilingual "may contain mistakes" strip along the bottom.
#:            On, because the audience is reading machine translation and has
#:            a right to know; it gets a reserved band so it cannot overlap.
#: - `labels` the small "EN -> AR" tag on a direction change. On: in `auto`
#:            the language flips with no speaker change to see, and the tag is
#:            the only cue that it did.
#: - `credit` the operator's line on the title card, as the text itself.
#:            Empty means no line: the words are the switch, so there is no
#:            state where a credit is configured but hidden, and nothing to
#:            leak while it is "off".
#:
#: The rest is the waiting screen - what the room looks at before Start and
#: after Stop. It is the organizer's poster rather than a caption view, so
#: every word on it is the operator's and every one has a default:
#:
#: - `eyebrow`   the small accent-coloured line above the headline.
#: - `headline`  the large line. `highlight` paints its last word in the
#:               accent, which is the one word of it the design emphasises.
#: - `subline`   two lines of muted English under it. **The only text on
#:               the poster that may carry a break the operator typed**: a
#:               newline in the field is a newline on screen. The design
#:               breaks the sentence where it reads best, and nothing
#:               automatic
#:               puts it there - `text-wrap: balance` came closest and still
#:               moved with the words, the viewport and the face. So the
#:               break is typed, and each line is then held on one line the
#:               way the headline is, by shrinking rather than wrapping.
#:
#: There was an Arabic welcome line under the subline until 2026-10-19. It
#: is not in the design and it is gone - the Arabic wordmark in the header
#: carries the second script, and the bilingual promise is made by the
#: title card and the footer strip, which are what the room reads once the
#: talk starts. A value for it left in an old `config/screen.yaml` is
#: ignored, like any other key this does not know.
#: - `pill`      the label across the card's top edge, set in caps by the
#:               stylesheet, so it is typed in sentence case here.
#: - `cta`       the line under the card.
#: - `note`      the small line under that, after the coloured dot. The
#:               dot is
#:               markup and stays; this is only its words.
#: - `invite`    the join link the QR encodes, when the code is generated
#:               rather than taken from the organizer's own file. **Never
#:               sent to a viewer** - see `public_screen` - and never
#:               committed: it lives in the gitignored `config/screen.yaml`.
#:
#: There is deliberately no "show the QR" switch. The card is part of the
#: composition rather than an optional extra, so it is always on screen and
#: there is always something in it: the organizer's file, the operator's
#: link, or the default. A toggle that could empty half the poster was a
#: layout with two shapes to get right and no occasion to use the second.
DEFAULT_SCREEN: dict[str, object] = {
    "view": "auto", "size": "l",
    "logo": False, "footer": True, "labels": True, "credit": "",
    # The two soft glows behind the caption view. On by default so the
    # room does not need an operator to find the switch before it looks
    # right. Off is the plain background the captions were designed
    # against, which is also what the "one surface colour" test measures.
    "glow": True,
    # Everything below is the waiting screen, and every word of it is
    # overridable: `config/brand.json` sets an event's own wording, and
    # `/admin` overrides that again on the day. These are the neutral
    # defaults, so a clone with no branding at all still shows a finished
    # screen rather than gaps.
    "eyebrow": "LIVE CAPTIONS",
    "headline": "Captions start shortly.",
    "highlight": True,
    # The break is data rather than a rule in the stylesheet, because the
    # phrase decides where it reads best - see `tidy_subline`.
    "subline": "Arabic and English captions, side by side,\n"
               "as the talk happens.",
    "pill": "Scan to join",
    "cta": "Scan to open the link",
    "note": "Opens the event link",
    "invite": "",
}

#: The credit goes on a projector, so it is one line. Long enough for a name
#: and a role, short enough that it cannot push the title card around.
MAX_CREDIT_CHARS = 120

#: Caps for the waiting screen's text. Each is sized for the band it sits in
#: at 1280x720, the tightest projector this has to survive: past these the
#: type would have to shrink to fit, and a welcome nobody can read from the
#: back of the room is worse than one that is a few words shorter.
MAX_SCREEN_TEXT: dict[str, int] = {
    "eyebrow": 60,
    "headline": 80,
    "subline": 220,
    # The three beside the card are bounded by the card, not by the band:
    # the pill has to stay inside the card's width at 1280x720 with the
    # stylesheet's caps and 0.2em of tracking on it, and the two lines
    # under it are centred on a column about a quarter of the screen wide.
    "pill": 24,
    "cta": 60,
    "note": 80,
}

#: How many lines the subline may be. The poster is drawn for two, and the
#: column under the headline has room for two; a third would either push into
#: the card's column or shrink the type below what the back of the room can
#: read. A longer paste is cut rather than refused, like every other cap.
MAX_SUBLINE_LINES = 2


def tidy_line(value: object, cap: int) -> str:
    """One line, whitespace collapsed, cut to `cap`.

    What every word on the poster goes through except the subline: the
    layout is sized for this text rather than the other way round, and a
    newline pasted into a one-line field is whitespace, not an instruction.
    """
    return " ".join(str(value).split())[:cap]


def tidy_subline(value: object, cap: int) -> str:
    """The subline, keeping the operator's own line breaks and nothing else.

    `tidy_line` collapses every run of whitespace, newlines included, which
    is right for a headline that is one line by construction. The subline is
    the one place a break is a decision: the design breaks the sentence
    where it reads best and nothing automatic puts it there, so the
    operator
    types it and this keeps it.

    Everything else is still tidied. Each line is collapsed on its own, blank
    lines are dropped rather than becoming a gap on the projector, and the
    result is capped at `MAX_SUBLINE_LINES` lines and `cap` characters.

    The break survives as a newline, and the page turns it into a `<br>` -
    see `paintSubline` in `web/screen.js`, which builds text nodes and never
    goes near `innerHTML`, because this string came out of a form field.
    """
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(part.split()) for part in text.split("\n")]
    lines = [line for line in lines if line][:MAX_SUBLINE_LINES]
    # Cut after joining, so the cap bounds what reaches the screen rather
    # than each line separately, then drop a break left dangling by the cut.
    return "\n".join(lines)[:cap].strip("\n")


#: How long a viewer may fail to accept anything before it is dropped.
STALL_TIMEOUT = 5.0

#: 1013 is "try again later", which is what a rejected or dropped phone
#: should do - and its reconnect backoff already does.
CLOSE_TRY_LATER = 1013


class ViewerSocket(Protocol):
    """The slice of Starlette's WebSocket the hub actually uses."""

    async def send_text(self, text: str) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


def _dump(payload: dict) -> str:
    """Compact JSON with Arabic left as Arabic.

    `ensure_ascii=False` is not cosmetic: escaping Arabic to a `\\uXXXX`
    sequence per character roughly triples the bytes of every Arabic caption,
    and the audience is on phone data in a basement.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def clip_field(value: object, limit: int = 300) -> object:
    """One diagnostic field, short enough to stay a field.

    A page's class list or user agent can be long, and this is written to a
    log line that somebody reads in a terminal at the end of a bad evening.
    """
    if isinstance(value, str) and len(value) > limit:
        return value[:limit - 1] + "…"
    return value


PING = _dump({"type": "ping"})

#: "Reload yourself." The operator's mid-talk fix for a projector that has
#: somehow stopped showing what it should: a page nobody can reach reloads
#: itself, the session keeps running and the transcript keeps writing into the
#: same folder. Stop + Start was the only remedy before this, and it ends the
#: talk to fix a browser.
RELOAD = _dump({"type": "reload"})

#: Passed to `Viewer.offer` for messages that must not advance a viewer's
#: sequence number - a ping tells it nothing about caption state.
NO_ADVANCE = 0

#: How long a page may sit behind the server's sequence number before
#: `/admin` calls it behind. Well past the 5 s health report and the ~10 Hz
#: flush, so ordinary lag never lights it up.
BEHIND_LIMIT = 10.0

#: A health report older than this means the page has stopped reporting -
#: three missed reports, so one lost message is not an alarm.
HEALTH_STALE = 16.0

#: How long a page may show a different language from the one the server has
#: on the end of the `auto` stream before it is called wrong.
#:
#: Translation trails its speech by about a second and the first chunk after
#: a language switch can take several, so a page is legitimately "behind" on
#: language at every turn. Measured over both 2026-10-24 sessions, the
#: longest honest disagreement was 12.1 s; fifteen leaves room above it and
#: is still a fraction of the minutes the report described.
WRONG_LANGUAGE_SECONDS = 15.0

#: How many health reports are kept per page, so a fault can be written down
#: with the minute leading up to it. At one report every five seconds this is
#: the last 100 seconds.
HEALTH_HISTORY = 20

#: "Tell me everything about yourself." Sent to one page when it has been
#: showing the wrong language for too long; the page answers with a
#: `diagnostic` message, which is the one place anything large crosses this
#: socket.
DIAGNOSE = _dump({"type": "diagnose"})


class Viewer:
    """One connected page: a socket, a writer task, and a one-message slot.

    `last_seq` is what this viewer has actually been *sent*. It advances in
    the writer after a successful send, never at offer time, so a diff
    computed from it can never skip text that failed to go out.
    """

    def __init__(self, socket: ViewerSocket, *, ident: int) -> None:
        self.socket = socket
        self.ident = ident
        self.last_seq = 0
        self.closed = False
        self.want_snapshot = False
        #: Set by `reload_screens`: send this page a reload and nothing else.
        self.want_reload = False
        self.stalled_since: float | None = None
        self.sent = 0
        #: What this page says it is actually rendering, and when it said so.
        #: `None` until the first report, which is also what an older page
        #: that does not report looks like. See `BroadcastHub.note_health`.
        self.health: dict[str, object] | None = None
        self.health_at: float | None = None
        #: Set by the wrong-language watch: ask this page for a full
        #: diagnostic snapshot on the next flush.
        self.want_diagnose = False
        #: What this page is, as far as a log reader is concerned. The agent
        #: comes from the socket's own headers rather than from anything the
        #: page says; the path arrives in its health reports.
        self.agent = ""
        self.path = ""
        #: The last few health reports, kept so that a fault can be written
        #: down with the minute before it rather than with the moment of it.
        self.recent: deque[dict[str, object]] = deque(maxlen=HEALTH_HISTORY)
        #: When this page first disagreed with the server about which
        #: language is on screen, and whether that has been reported.
        self.wrong_language_since: float | None = None
        self.wrong_language = False
        #: When this page was last level with the server's sequence number.
        #: "Behind for more than ten seconds" is measured from here rather
        #: than from a seq difference, because one paragraph of lag and ten
        #: minutes of it are the same number of seqs behind.
        self.caught_up_at: float | None = None
        self._pending: tuple[int, str] | None = None
        self._busy = False
        self._wake = asyncio.Event()

    @property
    def busy(self) -> bool:
        """True while a message is queued or in flight on the socket."""
        return self._busy

    def offer(self, advance_to: int, text: str) -> bool:
        """Fill the slot. False if the previous message has not gone out yet."""
        if self.closed or self._busy:
            return False
        self._pending = (advance_to, text)
        self._busy = True
        self._wake.set()
        return True

    async def run_writer(self) -> None:
        """Drain the slot onto the socket until the viewer is closed."""
        while not self.closed:
            await self._wake.wait()
            self._wake.clear()
            item, self._pending = self._pending, None
            if item is None:
                self._busy = False
                continue
            advance_to, text = item
            try:
                await self.socket.send_text(text)
            except Exception:
                # The phone vanished mid-write. Locking the slot and exiting
                # is enough: the receive side raises too, and the endpoint
                # removes the viewer.
                self.closed = True
                return
            self.last_seq = max(self.last_seq, advance_to)
            self.sent += 1
            self._busy = False

    def shutdown(self) -> None:
        """Make `run_writer` return at its next opportunity."""
        self.closed = True
        self._pending = None
        self._wake.set()


class BroadcastHub:
    """Fan-out from one `CaptionState` to every connected page."""

    def __init__(
        self,
        state: CaptionState,
        *,
        max_viewers: int = MAX_VIEWERS,
        hz: float = BROADCAST_HZ,
        ping_interval: float = PING_INTERVAL,
        stall_timeout: float = STALL_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state = state
        self.max_viewers = max_viewers
        self.interval = 1.0 / hz
        self.ping_interval = ping_interval
        self.stall_timeout = stall_timeout
        self.clock = clock
        self.viewers: list[Viewer] = []
        self.rejected = 0
        self.dropped = 0
        self._changed = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._writers: dict[int, asyncio.Task] = {}
        self._next_ident = 1
        self._next_ping = clock() + ping_interval
        self.screen: dict[str, object] = dict(DEFAULT_SCREEN)
        #: Which QR the projector is showing, as `kind`/`ext`/`tag`/`label`.
        #: Derived rather than set: the server resolves it from the state
        #: directory and the invite link and hands it over with `set_qr`.
        #: The default describes the fallback code, so a hub nobody has told
        #: anything still names a real image.
        self.qr: dict[str, str] = {
            "kind": "default", "ext": "svg", "tag": "",
            "label": "generated from the default link",
        }
        #: Called with the settings map whenever it changes, so the operator's
        #: choices survive a restart. Set by the server; left unset in tests,
        #: which is why it is a plain attribute rather than a constructor
        #: argument - a hub with nowhere to save to still works.
        self.on_change = None
        #: Called with `([(field, old, new), ...], source)` on every change,
        #: for the audit line. Separate from `on_change` because that one
        #: writes the *state* and this one writes the *history*, and because
        #: `BroadcastHub` has no business touching the filesystem either way:
        #: the server owns both ends.
        self.on_audit = None
        #: Called with `(event_name, fields)` for anything a log reader would
        #: want after the fact: a page connecting or going away, a health
        #: report, a reload, a page that has been showing the wrong language.
        #: The server points it at the session's event log; a hub with
        #: nowhere to write still works, which is how every test that does
        #: not care about logging gets away with ignoring it.
        self.on_event = None

    # -- the main screen ---------------------------------------------------

    def set_screen(self, *, view: str | None = None,
                   size: str | None = None,
                   logo: bool | None = None,
                   footer: bool | None = None,
                   labels: bool | None = None,
                   glow: bool | None = None,
                   credit: str | None = None,
                   eyebrow: str | None = None,
                   headline: str | None = None,
                   highlight: bool | None = None,
                   subline: str | None = None,
                   pill: str | None = None,
                   cta: str | None = None,
                   note: str | None = None,
                   invite: str | None = None,
                   source: str = "admin") -> bool:
        """Point every `/screen` page at a view and a size. True if changed.

        Unknown values are ignored rather than rejected: this is the one
        control that can blank the projector mid-talk, and the failure mode
        of a typo should be "nothing happened", not "the screen went away".

        `source` names where the change came from and goes in the audit line:
        `admin` for the operator's page, `startup` for settings read back
        from disk. **This is the only place any of these settings is ever
        written**, which is what makes the audit complete rather than
        best-effort - and what let "did a setting change during the talk" be
        answered after 2026-10-24.
        """
        changed: list[tuple[str, object, object]] = []

        def apply(name: str, value: object) -> None:
            """Set one field, remembering what it was. The audit's one door."""
            old = self.screen[name]
            if value == old:
                return
            self.screen[name] = value
            changed.append((name, old, value))

        if view in SCREEN_VIEWS:
            apply("view", view)
        if size in SCREEN_SIZES:
            apply("size", size)
        for name, value in (("logo", logo), ("footer", footer),
                            ("labels", labels), ("glow", glow),
                            ("highlight", highlight)):
            if value is not None:
                apply(name, bool(value))
        if credit is not None:
            apply("credit", tidy_line(credit, MAX_CREDIT_CHARS))
        # The waiting screen's words. Tidied to one line each and capped, the
        # same way the credit is: this text goes on a projector and the
        # layout is sized for it, not the other way round. The subline is the
        # exception - it keeps the break the operator typed.
        for name, value in (("eyebrow", eyebrow), ("headline", headline),
                            ("subline", subline), ("pill", pill),
                            ("cta", cta), ("note", note)):
            if value is None:
                continue
            cap = MAX_SCREEN_TEXT[name]
            apply(name, tidy_subline(value, cap) if name == "subline"
                  else tidy_line(value, cap))
        if invite is not None:
            apply("invite", clean_invite(invite))
        if changed:
            # A screen change moves no caption, so there is no `seq` for a
            # diff to carry it on. Every page takes a fresh snapshot instead,
            # which is cheap: the operator does this a handful of times a
            # talk, not ten times a second.
            for viewer in self.viewers:
                viewer.want_snapshot = True
            self.notify()
            if self.on_change is not None:
                self.on_change(dict(self.screen))
            if self.on_audit is not None:
                self.on_audit(list(changed), source)
        return bool(changed)

    def public_screen(self) -> dict[str, object]:
        """What a viewer is told about the screen. **Not the invite link.**

        Everything else here is chrome a room can see anyway. The link is
        different: it is the one value in these settings that is secret in
        the ordinary sense - anyone holding it can join the group without
        being in the room - so it stays on the server and `/screen` asks for
        the picture of it instead.

        The page is told the two things it needs to fetch the right image
        and to refetch it when it changes: which extension to ask for, and a
        fingerprint. **Both are derived from whichever source is live - the
        organizer's own file, the operator's link, or the default - and
        neither of them is the link.** `qr_tag` hashes the file's bytes or
        the URL's characters, so the image's address changes exactly when
        the image does and a projector can never hold a stale code.
        """
        public = {key: value for key, value in self.screen.items()
                  if key != "invite"}
        public["qr_ext"] = self.qr["ext"]
        public["qr_tag"] = self.qr["tag"]
        return public

    def set_qr(self, source) -> bool:
        """Adopt the QR source the server resolved. True if it changed.

        The hub does not go looking for it: `BroadcastHub` has no business
        touching the filesystem, and `_with_screen` runs on every diff - up
        to ten times a second - which is no place for a `stat`. The server
        resolves it at startup, whenever the settings change and on each
        operator poll, which is as often as a file can realistically appear.
        """
        now = {"kind": source.kind, "ext": source.extension,
               "tag": source.tag, "label": source.label()}
        if now == self.qr:
            return False
        self.qr = now
        for viewer in self.viewers:
            viewer.want_snapshot = True
        self.notify()
        return True

    def _with_screen(self, payload: dict) -> dict:
        payload["screen"] = self.public_screen()
        return payload

    def adopt_screen(self, saved: dict) -> None:
        """Take settings read back from disk at startup.

        Unknown keys are ignored and bad values fall back to the default, so
        a file from an older build - or one somebody hand-edited - cannot
        stop the server coming up with a usable screen.
        """
        if not isinstance(saved, dict):
            return
        self.set_screen(
            view=saved.get("view"), size=saved.get("size"),
            logo=saved.get("logo"), footer=saved.get("footer"),
            labels=saved.get("labels"), glow=saved.get("glow"),
            credit=saved.get("credit"),
            eyebrow=saved.get("eyebrow"), headline=saved.get("headline"),
            highlight=saved.get("highlight"), subline=saved.get("subline"),
            pill=saved.get("pill"), cta=saved.get("cta"),
            note=saved.get("note"), invite=saved.get("invite"),
            source="startup",
        )

    # -- viewers -----------------------------------------------------------

    @property
    def viewer_count(self) -> int:
        return len(self.viewers)

    async def add(self, socket: ViewerSocket,
                  agent: str = "") -> Viewer | None:
        """Accept a page - full snapshot first, then diffs. None if refused.

        The snapshot is written inline rather than through the slot: the
        writer task does not exist yet, so there is no second writer to
        interleave with, and a page that waited up to 100 ms for its first
        paint would feel broken.
        """
        if len(self.viewers) >= self.max_viewers:
            self.rejected += 1
            await _quietly(socket.close(CLOSE_TRY_LATER))
            return None

        viewer = Viewer(socket, ident=self._next_ident)
        self._next_ident += 1
        snapshot = self._with_screen(self.state.snapshot())
        try:
            await socket.send_text(_dump(snapshot))
        except Exception:
            viewer.closed = True
            await _quietly(socket.close(CLOSE_TRY_LATER))
            return None
        # From the snapshot, not from `state.seq`: the state may have moved
        # on during the await above, and this viewer has not seen that yet.
        viewer.last_seq = snapshot["seq"]
        viewer.sent = 1
        viewer.agent = agent[:300] if agent else ""

        self.viewers.append(viewer)
        if self.on_event is not None:
            # The page's own address arrives with its first health report;
            # this is the moment it connected and what the socket itself
            # could say about it.
            self.on_event("viewer_open", {
                "viewer": viewer.ident, "agent": viewer.agent,
                "viewers": len(self.viewers),
            })
        self._writers[viewer.ident] = asyncio.create_task(
            viewer.run_writer(), name=f"viewer-{viewer.ident}"
        )
        return viewer

    async def remove(self, viewer: Viewer) -> None:
        """Detach a viewer. Safe to call twice, and never blocks on its socket."""
        if viewer in self.viewers:
            self.viewers.remove(viewer)
            if self.on_event is not None:
                health = viewer.health or {}
                self.on_event("viewer_close", {
                    "viewer": viewer.ident, "path": viewer.path,
                    "agent": viewer.agent, "sent": viewer.sent,
                    "last_seq": viewer.last_seq,
                    "rendered": health.get("rendered"),
                    "viewers": len(self.viewers),
                })
        viewer.shutdown()
        task = self._writers.pop(viewer.ident, None)
        if task is not None:
            # Cancelled, not awaited to completion: a writer stuck in
            # `send_text` to a dead phone would otherwise stall the broadcast
            # loop here, which is the failure this whole file exists to avoid.
            await _settle(task)

    def rebind(self, state: CaptionState) -> None:
        """Point the hub at a fresh `CaptionState` and resync every viewer.

        Starting a second session builds a new state whose `seq` restarts at
        0. Without forcing a snapshot, every connected page would sit at a
        `last_seq` from the old session, decide it is already ahead, and show
        the previous talk forever.
        """
        self.state = state
        for viewer in self.viewers:
            viewer.last_seq = 0
            viewer.want_snapshot = True
        self.notify()

    def note_health(self, viewer: Viewer, payload: dict) -> None:
        """Record what a page says it is rendering. Validated by shape.

        **Why this exists.** On 2026-10-24 the projector was reported showing
        the English original in the middle of a talk, and nothing anywhere
        could say what any page was actually displaying - so "the captions
        are wrong" and "that page is not showing what I set" looked identical
        from the operator's chair, and a day of replaying logs could only
        rule things out. A page now says, every five seconds, which stream it
        has on screen, whether a URL pinned it there, the sequence number it
        has applied, and whether its tab is visible.

        The view is read off the page's own DOM rather than its intent, which
        is the whole point: a pane that is visible when it should not be is
        exactly the fault that could not be seen.

        This is the second thing a client may send, after `resync`, and it is
        untrusted input from a browser: every field is checked against a
        shape and anything else in the message is dropped. No caption text is
        accepted, and none is kept - `rendered` is one of five words, `grey`
        is a count of non-final spans in the translation-only pane, and the
        rest are a flag, an integer and a flag.
        """
        rendered = payload.get("rendered")
        if rendered not in SCREEN_VIEWS and rendered not in ("none", "mixed"):
            return
        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            return
        grey = payload.get("grey")
        if not isinstance(grey, int) or isinstance(grey, bool) or grey < 0:
            grey = 0
        now = self.clock()
        # Where the page lives, as the page sees it: `/screen?lang=en` and
        # `/` are the two answers that explain a fault on their own, and
        # neither is visible from the server side of a socket.
        path = payload.get("path")
        if isinstance(path, str) and path:
            viewer.path = path[:200]
        viewer.health = {
            "rendered": rendered,
            "pinned": bool(payload.get("pinned")),
            # A projector or the audience page. Anything unrecognised - and
            # an older page, which sends nothing - counts as a projector,
            # because that is the one the operator is driving and the one
            # worth warning about.
            "page": ("audience" if payload.get("page") == "audience"
                     else "screen"),
            "seq": seq,
            "visible": bool(payload.get("visible")),
            # The browser is translating the page. Every page this project
            # serves says `translate="no"`, so this should always be false -
            # and if it is ever true the room is being shown the captions
            # translated *back* into the language it just heard, which is a
            # caption fault that no amount of reading this code would find.
            "translated": bool(payload.get("translated")),
            # Non-final text in the translation-only pane. It should be
            # impossible - no translation token is ever non-final and `auto`
            # has no partials - so a number here names the thing the
            # 2026-10-24 report described, rather than leaving it to be
            # reconstructed from logs afterwards.
            "grey": min(grey, 999),
            # **What the room can actually read**, measured off the rendered
            # pane: which script the last stretch of visible text is in, and
            # the paragraph it belongs to. This is the one field that can
            # catch the 2026-10-24 symptom whatever caused it - a browser
            # rewriting the page, the wrong page entirely, a stream fault or
            # something nobody has thought of - because it compares what is
            # on the wall with what the server believes it sent.
            "script": (payload.get("script")
                       if payload.get("script") in ("ar", "en", "none")
                       else "none"),
            "para": (payload.get("para")
                     if isinstance(payload.get("para"), int)
                     and not isinstance(payload.get("para"), bool) else -1),
        }
        viewer.health_at = now
        viewer.recent.append({"at": round(now, 2), **viewer.health,
                              "server_seq": self.state.seq})
        if seq >= self.state.seq:
            viewer.caught_up_at = now
        elif viewer.caught_up_at is None:
            # First report and already behind: start the clock here rather
            # than at zero, or a page that joined mid-talk reads as behind
            # since the epoch.
            viewer.caught_up_at = now
        self._watch_language(viewer, now)
        if self.on_event is not None:
            self.on_event("health", {
                "viewer": viewer.ident, "path": viewer.path,
                "agent": viewer.agent, "server_seq": self.state.seq,
                **viewer.health,
            })

    def newest_auto_language(self) -> str:
        """The language of the newest `auto` paragraph with text in it.

        What the projector *should* be showing. An empty tail is skipped: a
        paragraph that has just been opened has no language on screen yet.
        """
        for para in reversed(self.state.streams.get(AUTO, [])):
            if para.final.strip() and para.lang:
                return str(para.lang)
        return ""

    def _watch_language(self, viewer: Viewer, now: float) -> None:
        """Is this page showing the language the server last sent it?

        Only for a page rendering `auto`, and only when both sides have an
        opinion. Disagreement is normal for a few seconds at every language
        switch - translation trails its speech - so it has to hold for
        `WRONG_LANGUAGE_SECONDS` before it counts, and the moment it does the
        page is asked for everything it knows about itself.
        """
        health = viewer.health or {}
        shown = health.get("script")
        want = self.newest_auto_language()
        disagrees = (
            health.get("rendered") == "auto"
            and shown in ("ar", "en")
            and want in ("ar", "en")
            and shown != want
        )
        if not disagrees:
            viewer.wrong_language_since = None
            if viewer.wrong_language:
                viewer.wrong_language = False
                if self.on_event is not None:
                    self.on_event("language_recovered", {
                        "viewer": viewer.ident, "path": viewer.path,
                        "showing": shown, "expected": want,
                    })
            return
        if viewer.wrong_language_since is None:
            viewer.wrong_language_since = now
            return
        if (now - viewer.wrong_language_since < WRONG_LANGUAGE_SECONDS
                or viewer.wrong_language):
            return
        # Long enough to be real. Flag it, and ask the page what it looks
        # like from the inside before anything about it changes.
        viewer.wrong_language = True
        viewer.want_diagnose = True
        self.notify()
        if self.on_event is not None:
            self.on_event("wrong_language", {
                "viewer": viewer.ident, "path": viewer.path,
                "agent": viewer.agent,
                "showing": shown, "expected": want,
                "seconds": round(now - viewer.wrong_language_since, 1),
                "translated": health.get("translated"),
                "pinned": health.get("pinned"),
                "rendered": health.get("rendered"),
                "paragraph": health.get("para"),
                "recent": list(viewer.recent),
            })

    def note_diagnostic(self, viewer: Viewer, payload: dict) -> None:
        """A page's answer to `diagnose`: everything about itself.

        Logged whole, because this arrives at most once per fault and the
        point of it is that nobody has to have guessed in advance which field
        mattered. Still no caption text: the page sends attributes, counts,
        flags and its own address.
        """
        snapshot = {
            key: clip_field(payload.get(key))
            for key in ("path", "lang", "translate", "classes", "fonts",
                        "panes", "view", "pinned", "seq", "visibility",
                        "pixel_ratio", "agent", "script", "para", "chars")
            if key in payload
        }
        if self.on_event is not None:
            self.on_event("diagnostic", {
                "viewer": viewer.ident, "path": viewer.path,
                "wrong_language": viewer.wrong_language,
                **snapshot,
            })

    def screens(self) -> dict[str, Any]:
        """What the open pages say they are showing. For `/admin`.

        Counts and enums only. `rows` is per page so the operator can see
        *which* one is wrong when there are two projectors, and it carries no
        address, no user agent and no text - a page is identified by nothing
        at all, because nothing more is needed to say "one of them is on the
        English stream".
        """
        now = self.clock()
        want = self.screen["view"]
        rows: list[dict[str, Any]] = []
        for viewer in self.viewers:
            health = viewer.health
            if health is None:
                rows.append({"reporting": False})
                continue
            age = now - (viewer.health_at or now)
            behind = (now - viewer.caught_up_at
                      if viewer.caught_up_at is not None else 0.0)
            projector = health["page"] == "screen"
            rows.append({
                "reporting": True,
                "page": health["page"],
                "rendered": health["rendered"],
                "pinned": health["pinned"],
                "visible": health["visible"],
                "grey": health["grey"],
                "translated": health["translated"],
                "script": health["script"],
                "wrong_language": viewer.wrong_language,
                "stale": age > HEALTH_STALE,
                "age_ms": int(age * 1000),
                "behind_ms": int(max(0.0, behind) * 1000),
                # Only a projector can be showing the "wrong" view: the
                # audience page follows its own reader by design, and
                # flagging that would be an alarm nobody can act on.
                "wrong_view": projector and health["rendered"] != want,
                "behind": behind > BEHIND_LIMIT,
            })
        reporting = [row for row in rows if row["reporting"]]
        return {"screens": {
            "count": len(self.viewers),
            "want": want,
            "rows": rows,
            "reporting": len(reporting),
            "audience": sum(1 for row in reporting
                            if row["page"] == "audience"),
            "wrong_view": sum(1 for row in reporting if row["wrong_view"]),
            "behind": sum(1 for row in reporting if row["behind"]),
            "grey": sum(1 for row in reporting if row["grey"]),
            "stale": sum(1 for row in reporting if row["stale"]),
            # Counted for every page, projector or not: a translated
            # audience page is showing the room the wrong language just as
            # surely as a translated projector would.
            "translated": sum(1 for row in reporting if row["translated"]),
            # The headline check: a page that has been showing a different
            # language from the one the server last put on the end of the
            # auto stream, for longer than translation can explain.
            "wrong_language": sum(1 for row in reporting
                                  if row["wrong_language"]),
        }}

    def reload_screens(self) -> int:
        """Tell every open page to reload itself. Returns how many were told.

        The session is untouched: captions keep arriving, the transcript keeps
        writing into the same folder, and each page comes back with a fresh
        snapshot because that is what a new socket is handed. This is the
        operator's mid-talk remedy for a projector nobody can walk over to -
        Stop + Start was the only one before, and it ends the talk.
        """
        for viewer in self.viewers:
            viewer.want_reload = True
        self.notify()
        if self.on_event is not None:
            self.on_event("screen_reload", {"screens": len(self.viewers)})
        return len(self.viewers)

    def resync(self, viewer: Viewer) -> None:
        """Honour `{"type":"resync"}`: a full snapshot on the next flush.

        A flag rather than a send, so the socket keeps exactly one writer.
        Repeat requests collapse into one snapshot, so a client stuck in a
        resync loop costs the server one message per flush at most.
        """
        viewer.want_snapshot = True
        self.notify()

    # -- broadcasting ------------------------------------------------------

    def notify(self) -> None:
        """Called when `CaptionState` changed. Sync and cheap on purpose."""
        self._changed.set()

    async def flush(self) -> None:
        """Hand every viewer whatever it is missing. No awaits on sockets.

        Diffs are computed per distinct `last_seq`, not per viewer: in the
        steady state every viewer sits at the same sequence number, so 500
        phones cost one `diff_since` and one `json.dumps` per flush.
        """
        now = self.clock()
        ping_due = now >= self._next_ping
        if ping_due:
            self._next_ping = now + self.ping_interval

        seq = self.state.seq
        diffs: dict[int, str] = {}
        drop: list[Viewer] = []

        for viewer in self.viewers:
            if viewer.closed:
                drop.append(viewer)
                continue

            if viewer.want_diagnose:
                # Ahead of everything: the page is in a state somebody will
                # want described, and the description has to be taken before
                # the next diff changes what is being described.
                text, advance_to, is_ping = DIAGNOSE, NO_ADVANCE, False
            elif viewer.want_reload:
                # Ahead of the snapshot: a page that is about to reload has
                # no use for one, and it will be handed a fresh snapshot the
                # moment its new socket opens. `NO_ADVANCE`, because this
                # carries no caption state.
                text, advance_to, is_ping = RELOAD, NO_ADVANCE, False
            elif viewer.want_snapshot:
                text, advance_to, is_ping = (
                    _dump(self._with_screen(self.state.snapshot())), seq, False)
            elif viewer.last_seq < seq:
                base = viewer.last_seq
                if base not in diffs:
                    diffs[base] = _dump(
                        self._with_screen(self.state.diff_since(base)))
                text, advance_to, is_ping = diffs[base], seq, False
            elif ping_due:
                text, advance_to, is_ping = PING, NO_ADVANCE, True
            else:
                viewer.stalled_since = None
                continue

            if viewer.offer(advance_to, text):
                if viewer.want_diagnose:
                    viewer.want_diagnose = False
                elif viewer.want_reload:
                    # Cleared only once it is on its way out, like the
                    # snapshot flag: a reload that failed to go must be
                    # offered again on the next flush.
                    viewer.want_reload = False
                else:
                    viewer.want_snapshot = False
                viewer.stalled_since = None
            elif is_ping:
                # A skipped ping is no evidence of a stall: the viewer is busy
                # receiving captions, which is better proof of life.
                continue
            elif viewer.stalled_since is None:
                viewer.stalled_since = now
            elif now - viewer.stalled_since >= self.stall_timeout:
                drop.append(viewer)

        for viewer in drop:
            self.dropped += 1
            await self.remove(viewer)
            await _quietly(viewer.socket.close(CLOSE_TRY_LATER))

    async def run(self) -> None:
        """Wake on a change or a due ping, flush, then throttle."""
        while True:
            timeout = max(self._next_ping - self.clock(), 0.0)
            try:
                await asyncio.wait_for(self._changed.wait(), timeout or 0.001)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            self._changed.clear()
            await self.flush()
            # The throttle, and the only reason diffs coalesce: everything
            # that changes during this sleep lands in one message.
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="broadcast")

    async def stop(self) -> None:
        """Stop the loop and close every viewer. Safe to call twice."""
        if self._task is not None:
            await _settle(self._task)
            self._task = None
        for viewer in list(self.viewers):
            await self.remove(viewer)
            await _quietly(viewer.socket.close(CLOSE_TRY_LATER))

    def status(self) -> dict[str, Any]:
        """Counters for `/admin`. No caption text, no secrets."""
        return {
            "viewers": self.viewer_count,
            "max_viewers": self.max_viewers,
            "rejected": self.rejected,
            "dropped": self.dropped,
            "seq": self.state.seq,
            "live": self.state.live,
        }


async def _quietly(awaitable) -> None:
    """Await something whose failure is not worth hearing about.

    Closing an already-dead socket is the normal case here, not an incident.
    """
    try:
        await awaitable
    except Exception:
        pass


async def _settle(task: asyncio.Task) -> None:
    """Cancel a task and wait for it, inheriting neither its result nor error.

    `asyncio.wait` is used rather than `await task` so the task's exception
    cannot propagate into the caller and its cancellation cannot be mistaken
    for the caller's own.
    """
    task.cancel()
    await asyncio.wait({task})
    if not task.cancelled():
        task.exception()
