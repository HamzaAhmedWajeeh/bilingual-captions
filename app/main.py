"""FastAPI app: the caption pages, the viewer socket, and the session runner.

One process, one event loop (the design brief). Three things live here:

- **Routes.** `/` audience, `/screen` big screen, `/admin` operator, `/ws`
  viewers, `/healthz`, and a deliberately narrow `/static/{name}`.
- **`CaptionSession`.** Owns a `CaptionState`, a `BroadcastHub`, and whatever
  is currently feeding them. Two feeds exist so far, and both are free:
  `_run_replay` plays a saved capture through `CaptionState`, and `_run_audio`
  (Phase 4) captures live audio and measures it without transcribing it.
  Phase 5 joins the two by putting the real Soniox client between them, as a
  third `_run_*` coroutine behind the same `start`/`stop`/`status`.
- **Lifespan.** Starts and stops the broadcast loop.

Phases 3 and 4 spend no Soniox credit, by construction: nothing in this
module, and nothing in `audio_source`, can open a network connection outward.
That is the budget rule made structural rather than remembered.

Every run is appended to `logs/usage.jsonl` when it ends, free runs included,
so the admin page can show total spend against the $10 budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response

from app import screen_audit, session_log, usage
from app.audio_source import (
    AudioSource, FileSource, MicSource, describe_devices, reload_devices,
)
from app.soniox_client import (
    MODELS,
    PAUSED,
    RECONNECTING,
    STOPPED,
    STREAMING,
    SonioxClient,
    rms_db,
    active_model,
    base_config,
)
from app.brand import (
    SLOTS as BRAND_SLOTS,
    brand_css,
    image_path as brand_image,
    load_brand,
)
from app.broadcast import BroadcastHub
from app.captions import CaptionState
from app.invite_qr import render as render_qr
from app.invite_qr import resolve as resolve_qr
from app.lang_watch import DRIFT, STUCK, LanguageWatch
from app.replay import CaptureWriter, FakeSonioxClient
from app.settings import (
    LOG_DIR,
    RECORDING_DIR,
    ROOT,
    TRANSCRIPT_DIR,
    WEB_DIR,
    SessionContext,
    build_context,
    load_glossary,
    load_mic,
    load_screen,
    load_session,
    save_mic,
    update_mic,
    save_screen,
    save_session,
    settings,
)
from app.transcript import (
    FILES as TRANSCRIPT_FILES,
    TranscriptRecorder,
    build_from_log,
    delete_transcript,
    is_safe_folder,
    list_transcripts,
    session_folder,
    stamp_from_log,
)

#: Where a replayable capture may come from. A request names a capture by
#: bare filename and it is looked up here, so no request can reach a path of
#: its own choosing - `recordings/` is not servable or readable through any
#: route (the security rules).
#:
#: `logs/` is on this list because a live session's raw response log is
#: already a capture: same `{recv_at, recv_offset_ms, sent_ms, response}`
#: envelope, same `meta` first line, and `replay.load_capture` reads one
#: unchanged. The 2026-10-17 dry run is the only recording of the operator's
#: voice plus Saudi Arabic, and it was unreplayable for no better reason than
#: the directory it landed in.
#:
#: This does **not** serve logs over HTTP. `resolve_capture` only ever opens
#: the file server-side to drive the pipeline, no route returns its bytes,
#: `logs/` stays out of `ASSETS` and stays gitignored, and the listing is
#: behind the admin token. A response log carries no session details either -
#: `tests/test_session_privacy.py` asserts that on every one written.
#: `LOG_DIR` is listed as well as `ROOT / "logs"`, and they are usually the
#: same directory. They differ when `CAPTIONS_LOG_DIR` moves the writable one
#: - a test server - and then *both* matter: the server must still be able to
#: replay the operator's saved sessions out of the repository, and it must be
#: able to replay a capture written into its own log directory, which is how
#: a three-hour synthetic session is driven through a real browser without
#: leaving a file in the tree.
CAPTURE_DIRS = tuple(dict.fromkeys((
    ROOT / "tests" / "fixtures", ROOT / "logs", LOG_DIR,
)))

#: What counts as a capture inside each directory. `logs/` also holds
#: `usage.jsonl`, the spend ledger, which is a different shape entirely and
#: would replay as silence - listing it as a capture would only invite the
#: operator to start a session that produces nothing.
CAPTURE_GLOBS = {ROOT / "logs": "live-*.jsonl", LOG_DIR: "live-*.jsonl"}

DEFAULT_CAPTURE_GLOB = "*.jsonl"

#: A session's name - the stem its response log, event log and health log
#: share. Checked against this before any path is built from a request, the
#: same way a transcript folder is: `/admin` is behind a token, but a
#: traversal there would still read any file on the laptop.
SESSION_NAME = re.compile(r"\A\d{4}-\d{2}-\d{2}-\d{6}(-\d+)?\Z")

#: "The input is too quiet to be the microphone you meant." Measured against
#: the one Oct 31 recording that survives; the reasoning is in
#: `CaptionSession._watch_quiet`, and the short version is that the loudest
#: 100 ms chunk of a session that worked was -35.2 dBFS, so a -35 dB warning
#: would have been on screen for all of it.
QUIET_PEAK_DB = -50.0
QUIET_SECONDS = 60.0
#: What `Level` reports for digital silence, and the starting point for
#: "loudest thing seen since the room went quiet".
QUIET_FLOOR_DB = -120.0

#: How long a window of transcribed speech still counts as "the engine is
#: hearing this input". Used by the quiet warning and by nothing else: a
#: line that reads quiet while words are arriving is not the wrong input,
#: it is a quiet input that works, and 2026-11-07 spent a session being
#: told otherwise. One minute, the same window the warning measures over.
SPEECH_RECENT_SECONDS = 60.0


def _has_final_token(response: dict) -> bool:
    """Did this response carry a word the engine committed to?

    `<end>` is a segment boundary and never displayed, so it does not count
    as speech here either - the question is whether words arrived, and a
    boundary is what arrives when they stop.
    """
    for token in response.get("tokens") or ():
        if token.get("is_final") and token.get("text") != "<end>":
            return True
    return False


def _model_name() -> str:
    """The configured speech model, for the status payload.

    Swallows a bad name rather than raising: `require_known_model` has
    already refused to start on one, so the only way to get here is a `.env`
    edited while the server runs, and a status poll that throws would take
    the whole operator page down over a typo it is trying to report.
    """
    try:
        return active_model()
    except ValueError:
        return f"{settings.soniox_model} (not a known model)"


#: Audio a `FileSource` run may read, by bare filename only, with the same
#: reasoning as `CAPTURE_DIRS`. Reading a recording locally is not serving it:
#: no route returns audio, and `recordings/` stays out of `ASSETS`.
AUDIO_SUFFIXES = (".wav", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus",
                  ".webm", ".mp4", ".wma")

#: Exactly what `/static` will serve, with its content type. An allowlist
#: rather than a `StaticFiles` mount on `web/`: a mount would also serve
#: `admin.html` to anyone who guessed the path, and would serve any stray
#: file that ever lands in that directory. Keeping the list explicit is also
#: what makes the Phase 8 "grep served static files for SONIOX" test exact.
ASSETS = {
    "style.css": "text/css; charset=utf-8",
    # Generated by `tools/brand_assets.py` from whatever wordmarks are in
    # `brand/`: their native pixel sizes as CSS variables, which is what lets
    # larger artwork resize the title card without anyone editing
    # `style.css`. Optional - it is gitignored because it describes somebody's
    # artwork, and `style.css` declares a usable default for every variable it
    # sets, so a clone that has never run the tool still lays out correctly.
    "brand-sizes.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "auto.js": "text/javascript; charset=utf-8",
    "screen.js": "text/javascript; charset=utf-8",
    "admin.js": "text/javascript; charset=utf-8",
    "brand.js": "text/javascript; charset=utf-8",
}

#: Brand images are **not** in `ASSETS` and are not served from `web/`.
#: They live in the gitignored `brand/` directory and go out through
#: `/brand/{name}`, which resolves against `app.brand.SLOTS` and nothing
#: else. Two reasons to keep them apart from the code's own files:
#:
#: * `web/` is committed, so anything served from it is in the repo. Brand
#:   artwork must not be, and a directory nothing serves from is a weaker
#:   promise than a route that cannot reach it.
#: * An organiser drops files into `brand/` by hand, sometimes more than
#:   the five slots. A request can only ever name a slot, so whatever else
#:   lands in there is unreachable over HTTP by construction rather than by
#:   anyone remembering to check.

#: Pages are never cached. A phone that reloads after a mid-talk fix must get
#: the fixed page, not yesterday's from its HTTP cache.
NO_STORE = {"Cache-Control": "no-store"}

#: Nothing a client legitimately sends is larger than this.
#:
#: It was 200 bytes while `{"type":"resync"}` was the only thing a page could
#: say. A health report is a few hundred, and the diagnostic a page sends
#: when it has been showing the wrong language - its address, its root
#: element's attributes and class list, its user agent - runs to about a
#: kilobyte. That one arrives at most once per fault and is the whole reason
#: the fault is readable afterwards, so the cap is raised to fit it and no
#: further. Oversized frames are still dropped unread, and every message is
#: validated by shape after it is parsed: the cap is about not reading
#: something enormous, not about trusting what fits.
MAX_CLIENT_MESSAGE = 4096

#: After the engine has flushed, how long to let the caption task drain the
#: responses that flush produced. Short: the tokens are already in the queue.
FINISH_DRAIN_SECONDS = 1.0


class CaptionSession:
    """The running talk: caption state, viewer hub, and the current feed."""

    def __init__(self) -> None:
        #: The name this run's files share - the response log, the event log
        #: and the health log. `None` between sessions. Declared first,
        #: because adopting the saved settings below writes an audit line and
        #: that path asks whether a session's logs are open.
        self.session_id: str | None = None
        self._logs: session_log.SessionLogs | None = None
        self.state = CaptionState()
        self.hub = BroadcastHub(self.state)
        # What the main screen says, in three layers, each one winning over
        # the one before it:
        #
        #   1. `broadcast.DEFAULT_SCREEN`  - the neutral words this repo ships
        #   2. `config/brand.json`         - this event's own wording
        #   3. `config/screen.yaml`        - what the operator last typed
        #
        # That order is the whole point of the brand file: an event sets its
        # wording once and gets it on every fresh clone and after every
        # restart, while the operator can still overrule any of it on the day
        # from `/admin` and have that stick. `.env` only seeds the credit
        # line, and only on a clone that has never saved.
        saved = {**load_brand().get("waiting", {}), **load_screen()}
        if "credit" not in saved and settings.screen_credit.strip():
            saved = dict(saved, credit=settings.screen_credit)
        # The audit hook goes on *before* the settings are adopted, so the
        # log opens with what the server came up holding - "startup" lines
        # naming every field that differs from the shipped default. The save
        # hook goes on *after*, deliberately: adopting is reading the file
        # back, and a save here would rewrite the operator's settings on
        # every start for no reason.
        #
        # One line per changed field, with where it came from. After
        # 2026-10-24 "did a setting move during the talk" could only be
        # answered from an mtime on `screen.yaml`, which says when the file
        # was last written and nothing about what happened during a session.
        self.hub.on_audit = self._on_screen_audit
        self.hub.adopt_screen(saved)
        self.hub.on_change = save_screen
        # Everything about a session except what anyone said. The hub reports
        # pages connecting, health arriving and a screen showing the wrong
        # language; this is where those lines are written.
        self.hub.on_event = self._on_hub_event
        self.refresh_qr()
        self.source: str | None = None
        self.mode = "replay"
        self.engine = "none"
        self.paced = True
        self.started_at: float | None = None
        self.responses = 0
        self.dropped_seconds = 0.0
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._client: FakeSonioxClient | None = None
        self._audio: AudioSource | None = None
        self._engine: SonioxClient | None = None
        self._capture: CaptureWriter | None = None
        #: The readable transcript of this run. **Only ever set by
        #: `start_live`.** `start_replay` and `start_audio` have no path to
        #: one, which is what makes "a test run writes nothing" a property of
        #: the code rather than of the operator leaving a box unticked.
        self._transcript: TranscriptRecorder | None = None
        self.transcript_dir: Path | None = None
        #: This talk's private details, held in memory so an edit applies
        #: without a restart. Loaded from the gitignored config/session.yaml,
        #: which may not exist - a fresh clone has none and that is normal.
        self.details = load_session()
        #: True once the details have changed under a running engine, until
        #: `apply_details` reconnects. The operator needs to know that what
        #: they typed is saved but not yet in effect.
        self.details_pending = False
        self._logged = True
        #: "Has Soniox stopped hearing English?" Rebuilt per session in
        #: `_begin`, because every threshold in it is a rolling window and a
        #: new talk must not inherit the last one's. None between sessions.
        self._lang: LanguageWatch | None = None
        #: The last thing the language watch did, for `/admin`. Outlives the
        #: session on purpose: the operator reads that line *after* pressing
        #: Stop as often as during the talk.
        self.lang_note: dict | None = None
        #: Whether the drift signal fired this session. Held here rather than
        #: read off the watcher because the watcher is torn down with the
        #: session and this flag has to survive Stop - and because the page
        #: reads it unconditionally, so it must be in every status payload,
        #: including the stopped one the page spends most of its life
        #: rendering.
        self.lang_drifted = False
        #: Whether the hourly reconnect cap has been reported. Same
        #: reasoning as `lang_drifted`: written once, survives Stop.
        self._lang_capped = False
        #: Quiet-input bookkeeping, same shape and the same reason.
        self._quiet_since: float | None = None
        self._quiet_logged = False
        self._quiet_peak = QUIET_FLOOR_DB
        self.quiet_note: dict | None = None
        #: "The idle auto-stop closed the stream, and here is why." Written
        #: when the engine goes `paused` and cleared when it comes back, so
        #: `/admin` can say what the pause was about rather than show a
        #: one-word pill the operator has to interpret. On 2026-11-07 that
        #: pill was the whole message and the operator read it as a dead
        #: session, pressed Stop and Start, and ended the talk.
        self.pause_note: dict | None = None
        self._paused_at: float | None = None
        #: When the engine last returned a committed word. Read by the quiet
        #: warning, which otherwise cannot tell a line that is quiet from a
        #: line that is dead - and on 2026-11-07 called a working mixer the
        #: wrong input for 667 seconds while 5,655 final tokens arrived
        #: through it.
        self._last_speech_at: float | None = None

    def refresh_qr(self) -> bool:
        """Re-resolve which QR to show, and tell the hub if it changed.

        Called at startup, after any settings change, and on every operator
        poll - which is how a file dropped into the state directory
        mid-setup reaches an open projector without anyone reloading it. It
        is a `stat` and, when a file is there, a read of a few kilobytes
        once a second on the admin page; the viewer diffs never do this.
        """
        return self.hub.set_qr(resolve_qr(str(self.hub.screen["invite"])))

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def level(self) -> dict[str, Any] | None:
        """Input level, or None when nothing is capturing audio."""
        return self._audio.level.as_dict() if self._audio is not None else None

    # -- the session's own logs --------------------------------------------

    def _on_screen_audit(self, changes: list, source: str) -> None:
        """A settings change: to the audit file always, and to this
        session's event log as well when one is running.

        Two places on purpose. The audit file answers "what has this
        projector been told, ever"; the event log answers "what happened
        during *that* talk", and the second question is the one asked at
        eleven at night with one evening's files in hand.
        """
        screen_audit.record(changes, source)
        for field, old_value, new_value in changes:
            self.log_event(
                session_log.SETTING_CHANGED, source=source, field=field,
                old=screen_audit.render_value(field, old_value),
                new=screen_audit.render_value(field, new_value),
            )

    def _on_hub_event(self, kind: str, fields: dict) -> None:
        """Write what the hub noticed into this session's logs.

        `health` goes to its own file - one line every five seconds from
        every page is a stream, not an event - and everything else is an
        event. Nothing here can raise: `JsonlLog` swallows its own failures,
        and a session must not end because a log did.
        """
        logs = self._logs
        if logs is None:
            return
        if kind == "health":
            logs.report(**fields)
            return
        name = {
            "viewer_open": session_log.VIEWER_OPEN,
            "viewer_close": session_log.VIEWER_CLOSE,
            "screen_reload": session_log.SCREEN_RELOAD,
            "wrong_language": session_log.WRONG_LANGUAGE,
        }.get(kind)
        if name is not None:
            logs.event(name, **fields)
            return
        # `diagnostic`, `language_recovered` and anything the hub grows
        # later: written under their own name rather than dropped, because
        # the whole point of this file is that nobody had to guess in
        # advance which line would matter.
        logs.event(kind.upper(), **fields)

    def log_event(self, kind: str, **fields) -> None:
        """One event, from the server side. No-op between sessions."""
        if self._logs is not None:
            self._logs.event(kind, **fields)

    def _open_logs(self, *, mode: str, source: str,
                   session_id: str | None = None) -> None:
        """Open this session's event and health logs.

        **Always**, for every mode. The response log is opt-in because it
        holds speech; these two hold counts and flags, so there is no reason
        to make the operator choose - and the session that needs them is
        never the session somebody remembered to tick a box for.
        """
        # `session_id` is passed in when the caller had to know the name
        # *before* `_begin` ran - a live run names its recording from it, and
        # the recorder is opened by `source.open()`, which is earlier.
        self.session_id = session_id or session_log.free_stamp(self.started_at)
        self._logs = session_log.SessionLogs(
            self.session_id, self.started_at or time.time())
        self._logs.event(
            session_log.SESSION_START, mode=mode, source=source,
            engine=self.engine, paced=self.paced,
            screen=dict(self.hub.screen, invite=""),
            details=self.details.summary(),
            # **Was the audio being saved?** The response log and the
            # transcript can be read back off the files they write. Whether
            # the *audio* was kept cannot be read back off anything: the
            # only trace is a WAV that either exists or does not, and a
            # missing one is indistinguishable from a deleted one.
            #
            # That is exactly the question that could not be answered about
            # 2026-10-31. Two sessions locked, neither can be replayed, and
            # whether "Save the audio to this laptop" had been ticked was a
            # matter of recollection. It is unticked on every page load - the
            # one box here that is off by default, because the room has not
            # consented to being recorded - which also makes it the easiest
            # to believe you left on. A flag costs nothing and settles it.
            #
            # Only this one, and not the other two: `_capture` and
            # `_transcript` are both built *after* `_begin`, so reporting
            # them here would report False every time.
            recording=bool(self._audio is not None
                           and self._audio.recording_path),
            # **Which engine build this talk streamed to.** Read off the
            # config the client holds, not off `.env`, because `.env` is a
            # file that moves and this line has to still be true in a month.
            # Absent for a replay or a level check, which reach no engine at
            # all, so the question has no answer for them - and the same
            # reasoning as `recording` above: a run whose numbers look odd
            # should not need anybody's recollection of what was selected.
            model=(self._engine.model if self._engine is not None else None),
        )

    def _close_logs(self, reason: str) -> None:
        if self._logs is None:
            return
        self._logs.event(
            session_log.SESSION_STOP, reason=reason,
            responses=self.responses,
            dropped_seconds=round(self.dropped_seconds, 1),
            seconds=round(self._elapsed(), 1),
            error=self.error,
            transcript=(self.transcript_dir.name
                        if self.transcript_dir else None),
        )
        self._logs.close()
        self._logs = None

    def _begin(self, *, mode: str, engine: str, source: str,
               session_id: str | None = None) -> None:
        """Reset everything a new run must not inherit from the last one."""
        self.state = CaptionState()
        self.hub.rebind(self.state)
        self.mode = mode
        self.engine = engine
        self.source = source
        self.started_at = time.time()
        self.responses = 0
        self.dropped_seconds = 0.0
        self.error = None
        self._logged = False
        # A fresh watcher per session, and one that only *acts* on a live
        # run. A replay has no connection to re-open, and the reason to
        # replay a locked session is to watch the detector fire, not to have
        # it restart a file - so in replay the signal is logged and nothing
        # else happens. Same shape as the transcript rule: the mode decides,
        # not a checkbox.
        self._lang = LanguageWatch(act=(engine == "soniox"))
        self.lang_note = None
        self.lang_drifted = False
        self._lang_capped = False
        self._quiet_since = None
        self._quiet_logged = False
        self._quiet_peak = QUIET_FLOOR_DB
        self.quiet_note = None
        self.pause_note = None
        self._paused_at = None
        self._last_speech_at = None
        self.state.set_live(True)
        self._open_logs(mode=mode, source=source, session_id=session_id)
        self.hub.notify()

    async def start_replay(self, capture: Path, *, paced: bool = True,
                           speed: float = 1.0) -> None:
        """Drive the pipeline from a saved capture. Costs no Soniox credit.

        `speed` compresses a paced replay's wall clock - 20 plays a 52-minute
        log in 2.6 minutes with every response in its original order and
        proportional spacing. It is the long-run soak's hatch: a bug that
        only shows up after half an hour of talk cannot be reproduced in a
        test at real time. Ignored when `paced` is false, which has no clock
        to compress.
        """
        await self.stop()
        self.paced = paced
        self._audio = None
        self._engine = None
        self._client = FakeSonioxClient(capture, paced=paced, speed=speed)
        self._begin(mode="replay", engine="replay", source=capture.name)
        self._task = asyncio.create_task(self._run_replay(self._client),
                                         name="replay")

    async def _run_replay(self, client: FakeSonioxClient) -> None:
        try:
            async for response in client:
                self.responses += 1
                if self.state.add_response(response):
                    # The hub decides when this reaches a phone; at 10 Hz most
                    # of these notifications coalesce into nothing.
                    self.hub.notify()
                self._watch_language(response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a malformed capture, not a live failure
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            # The capture ran out, or the operator stopped it. Either way the
            # session is no longer live, and the pages must say so.
            self._finish(client.sent_ms / 1000)

    async def start_audio(self, source: AudioSource) -> None:
        """Capture live audio and measure it. **Produces no captions.**

        Phase 4 has the microphone but not the engine, so this run exists to
        answer the operator's questions - is the right input selected, is the
        gain right, is anything clipping - and to prove the capture path end to
        end. Phase 5 turns the same source into captions by putting the Soniox
        client between this coroutine and `CaptionState`; nothing here changes.
        """
        await self.stop()
        # Opened here so a missing or busy device raises before the session
        # claims to be running.
        await source.open()
        self._client = None
        self._engine = None
        self._audio = source
        self.paced = getattr(source, "realtime", True)
        self._begin(mode=source.kind, engine="monitor",
                    source=source.describe())
        self._task = asyncio.create_task(self._run_audio(source), name="audio")

    async def _run_audio(self, source: AudioSource) -> None:
        try:
            async for _chunk in source:
                # Phase 5 sends the chunk to Soniox here. Until then the audio
                # is counted and levelled by the source itself and discarded:
                # holding it would be a memory leak with no reader.
                self.responses += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            seconds = source.audio_seconds
            await source.close()
            self._finish(seconds)

    async def start_live(self, source: AudioSource, *, capture: bool = False,
                         transcript: bool = True,
                         transcript_title: bool = False,
                         **client_kwargs) -> None:
        """The real thing: microphone -> Soniox -> captions -> phones.

        **This spends money.** Everything else in this class is free; this is
        the only path that opens a connection to Soniox, and the usage line it
        writes at the end is the billable one.
        """
        await self.stop()
        if not settings.soniox_api_key:
            raise HTTPException(
                status_code=400,
                detail="SONIOX_API_KEY is not set; live transcription needs it",
            )
        # **The name first, then open.** All four of a session's files share
        # one stem - the response log, the event log, the health log and the
        # recording - and the recording is the only one opened before
        # `_begin`, by `source.open()`. Allocating the stamp here and handing
        # it to both is what makes "one session, one name" true rather than
        # true-unless-Start-straddles-a-second.
        stamp = session_log.free_stamp()
        source.name_recording(stamp)
        await source.open()
        self._audio = source
        self._client = None
        self.paced = True
        self._remember_mic(source)
        client = SonioxClient(
            source,
            # Built from the details held in memory, not re-read from disk:
            # the operator may have edited them seconds ago.
            context=self.context(),
            on_state=self._on_engine_state,
            on_drop=self._on_engine_drop,
            on_audio_dropped=self._on_audio_dropped,
            **client_kwargs,
        )
        self._engine = client
        self._capture = None
        # The config *without* its context. A capture is kept for months
        # and may be committed as a fixture, and the context is where the
        # speaker names live - writing it here would put them in a log
        # file, which is exactly what the session details are kept out of.
        # The shape is recorded instead, which is what a reader of the
        # capture actually needs to know.
        safe_config = {
            key: value for key, value in client._config.items()
            if key != "context"
        }
        safe_config["context"] = self.details.summary()
        self._begin(mode=source.kind, engine="soniox",
                    source=source.describe(), session_id=stamp)
        if capture:
            # Opt-in: a capture is a verbatim transcript of the room. It is
            # also the only way to answer a token-level question after the
            # fact, and it produces a file that replays as a test fixture.
            #
            # **After `_begin`**, which is where `session_id` is assigned.
            # Before it, `self.session_id` still held the *previous*
            # session's name, so every response log but the first of a server
            # run was written under the stem of the session before it - which
            # is how the three logs of 2026-10-31 came to sit under the names
            # of the three sessions that preceded them, and how "Download
            # diagnostics" came to zip the wrong talk's responses.
            self._capture = CaptureWriter(
                LOG_DIR / f"live-{self.session_id}.jsonl",
                meta={"source": source.describe(), "config": safe_config},
            )
        # After `_begin`, which clears `self.error` - a transcript that could
        # not be started has to be able to say so on the page, and set before
        # this it was wiped on the same line it was written.
        self._open_transcript(enabled=transcript,
                              with_title=transcript_title)
        self.state.set_status("connecting")
        self._task = asyncio.create_task(self._run_live(client), name="live")

    def _open_transcript(self, *, enabled: bool, with_title: bool) -> None:
        """Start a transcript folder for this live run, if asked.

        Called from `start_live` and from nowhere else. A failure here must
        not stop a talk: the transcript is something to read afterwards, and
        a read-only disk is a worse reason to lose captions than it is to
        lose the write-up - so it is reported on the page and the run goes on.
        """
        self._transcript = None
        self.transcript_dir = None
        if not enabled:
            return
        stamp = time.strftime("%Y-%m-%d_%H%M")
        try:
            directory = session_folder(TRANSCRIPT_DIR, stamp)
            self._transcript = TranscriptRecorder(
                directory,
                date=time.strftime("%Y-%m-%d"),
                # The title only, and only when the operator ticked the box.
                # Speaker names never reach this file: they live in the
                # gitignored config/session.yaml and go to the Soniox context
                # and `/admin`, and a transcript is a file that gets mailed.
                title=self.details.title if with_title else "",
                glossary=load_glossary().terms,
            )
            self.transcript_dir = directory
        except OSError as exc:
            self.error = f"transcript not started: {exc}"

    def context(self) -> dict:
        """The Soniox `context` for the next connection.

        The public glossary is re-read each time, because it is a file anyone
        may edit; the private details come from memory, because `/admin` owns
        them.
        """
        return build_context(load_glossary(), self.details)

    def save_details(self, details: SessionContext) -> bool:
        """Persist and adopt new session details. True if a reconnect is
        needed to apply them."""
        save_session(details)
        self.details = details
        running = self._engine is not None and self.running
        self.details_pending = running
        return running

    def apply_details(self) -> bool:
        """Reconnect the engine so the new context takes effect.

        Returns False when nothing is running - then there is nothing to
        apply and the details are already in place for the next Start.
        """
        engine = self._engine
        if engine is None or not self.running:
            self.details_pending = False
            return False
        applied = engine.reconfigure(base_config(self.context()))
        self.details_pending = not applied
        # A rollover the operator asked for. Worth a line of its own: it ends
        # one Soniox stream and opens another mid-talk, and without this the
        # event log would show the reconnect with nothing to explain it.
        self.log_event(session_log.APPLY_NOW, applied=applied,
                       details=self.details.summary())
        return applied

    def _on_engine_state(self, state: str) -> None:
        """Mirror the engine's state onto the pages.

        `live` deliberately does not change here (the resilience design): a
        reconnect must not make the audience page say the talk ended. Only
        `stop()` clears `live`.
        """
        shown = {STREAMING: "live", RECONNECTING: "reconnecting",
                 PAUSED: "paused"}.get(state, state)
        engine = self._engine
        was_paused = self._paused_at is not None
        kind = session_log.ENGINE_STATE
        extra: dict[str, Any] = {}
        if state == PAUSED:
            kind = session_log.PAUSED
            extra = self._note_pause()
        elif was_paused and state != STOPPED:
            # Out of the pause and reconnecting. The event the operator
            # wants is "it came back", and which half brought it back - the
            # room making a noise or them pressing Resume - because those
            # are different faults.
            kind = session_log.RESUMED
            extra = self._note_resumed()
        elif was_paused:
            # Stop, pressed while paused. The talk is over, so the banner
            # must not survive it - but this is not a resume either: nothing
            # came back.
            self._paused_at = None
            self.pause_note = None
        self.log_event(
            kind,
            state=state, shown=shown,
            reconnects=getattr(engine, "reconnects", None),
            rollovers=getattr(engine, "rollovers", None),
            streamed_seconds=(round(engine.audio_seconds_sent, 1)
                              if engine is not None else None),
            **extra,
        )
        if state == PAUSED and self._transcript is not None:
            # The idle auto-stop ends the Soniox *stream*, not the session -
            # sound in the room resumes it - so this is not the end of the
            # talk and the recorder stays open. It is, though, the moment a
            # session left running overnight stops producing anything, so
            # take a checkpoint: `finalize` rewrites from the segments it
            # holds rather than appending, so Stop can do it again without
            # duplicating a line.
            try:
                self._transcript.finalize(self.state, self._elapsed())
            except OSError:
                pass
        if self.state.set_status(shown):
            self.hub.notify()

    def _note_pause(self) -> dict[str, Any]:
        """Write down what the input was doing when the stream paused.

        **This is the line that was missing on 2026-11-07.** The session
        paused at 632.5 s, `/admin` showed the word PAUSED and nothing else,
        and the operator - with no way to tell a quiet room from an input
        that had stopped carrying sound - stopped and started the talk. The
        event log had one line for it, `IDLE_STOP`, which carried the
        reconnect counters and not one number about the audio.

        So the pause now says the device, how long the input had been below
        the threshold, and both levels in dB. It is counts and figures: no
        caption text, no session detail, nothing about what was being said
        while it happened.
        """
        engine = self._engine
        audio = self._audio
        level = self.level or {}
        self._paused_at = time.time()
        note = {
            "device": ((getattr(audio, "device_name", "") or "")
                       if audio is not None else "") or self.source,
            "silent_seconds": (round(engine.silent_seconds, 1)
                               if engine is not None else None),
            "idle_stop_seconds": (engine.idle_stop_seconds
                                  if engine is not None else None),
            # **Off the engine, not off the meter.** They are the same
            # quantity - the RMS of the last chunk - and the meter's copy is
            # clamped at `audio_source.FLOOR_DB`, -60 dBFS, because that is
            # where a bar stops being worth painting. Read from there, the
            # banner said "Input -60.0 dB, needs -66.0 dB": a level that
            # looks *above* the threshold next to a pause that is waiting
            # for it, which reads as a page contradicting itself. The
            # engine's own figure floors at -100 and is the number the
            # decision was actually made on.
            "input_db": self._input_db(),
            # The meter's peak, and clamped at its -60 like the bar beside
            # it. Kept because it is what the operator can see on the page,
            # and never compared against the threshold above.
            "meter_peak_db": level.get("peak_db"),
            "silence_db": (round(rms_db(engine.silence_rms), 1)
                           if engine is not None else None),
            "pauses": getattr(engine, "pauses", None),
        }
        self.pause_note = note
        return dict(note)

    def _input_db(self) -> float | None:
        """The level the idle auto-stop judges, in dBFS, unclamped at -60.

        `SonioxClient.last_rms` is the RMS of the last chunk it pumped,
        which is the exact quantity compared against `SILENCE_RMS`. The
        admin meter reports the same RMS floored at -60 dBFS for the bar,
        and on this mixer line the real figure is far below that - the
        10th-percentile chunk is around -91 dBFS - so the meter's copy
        cannot be put beside a -66 dBFS threshold without lying about it.
        """
        engine = self._engine
        if engine is not None and hasattr(engine, "last_rms"):
            return round(rms_db(engine.last_rms), 1)
        level = self.level or {}
        return level.get("db")

    def _note_resumed(self) -> dict[str, Any]:
        """...and what brought it back, and how long it was gone."""
        engine = self._engine
        level = self.level or {}
        paused_for = (round(time.time() - self._paused_at, 1)
                      if self._paused_at else None)
        self._paused_at = None
        self.pause_note = None
        return {
            "reason": getattr(engine, "resume_reason", None),
            "paused_seconds": paused_for,
            # The same two as the pause, read the same way, so the pair of
            # lines can be compared without converting one of them.
            "input_db": self._input_db(),
            "meter_peak_db": level.get("peak_db"),
            "resumes": getattr(engine, "resumes", None),
        }

    def resume(self) -> bool:
        """Operator's Resume. Reconnects without starting a new session.

        The pause closed the Soniox stream and left everything else alone -
        the session, the recorder, the transcript folder, the pages - so
        coming back out of it is a reconnect through the path a rollover
        already uses, not a Start. That distinction is the whole point:
        Stop + Start was the only way out of a pause until now, and it ends
        the talk, closes the stream, finalizes the transcript and resets
        every page's paragraphs to fix an engine that is waiting for a
        noise.
        """
        engine = self._engine
        if engine is None or not self.running:
            return False
        return bool(engine.resume())

    def _on_engine_drop(self) -> None:
        """A connection just died. Mark the discontinuity.

        Two things, both learned from run7 (2026-10-13): an uncommitted
        partial would otherwise sit grey on every phone for the length of the
        reconnect, and the first token of the *new* stream carries no leading
        space, so appending it glued two sentences together
        (`behindMicrosoft`). `mark_gap` handles both.
        """
        engine = self._engine
        # The counters as well as the error, because `on_drop` is the one
        # callback a *rollover* and a *reconnect* share: the error is None
        # for the first and set for the second, and which counter moved is
        # what says so without inference.
        self.log_event(
            session_log.ENGINE_DROP,
            error=(engine.last_error if engine is not None else None),
            reconnects=getattr(engine, "reconnects", None),
            rollovers=getattr(engine, "rollovers", None),
        )
        if self.state.mark_gap():
            self.hub.notify()

    def _remember_mic(self, source: AudioSource) -> None:
        """Write down the input this live session opened.

        **After `open()`**, so the name is the one PortAudio resolved rather
        than the one the request asked for. Those are different questions
        when the request says "default", which is how 2026-10-31 came to have
        three sessions whose logs name a mixer the operator did not believe
        was selected.

        Live only, and never for a file or a replay: the point is to
        preselect the thing that worked last time in the room, and a wav
        file is not that.
        """
        name = getattr(source, "device_name", "") or ""
        if not name:
            return
        try:
            # A merge, not a write: the record flag in the same file was set
            # when Start was pressed and must survive this.
            update_mic({
                "name": name,
                "index": getattr(source, "device", None),
                "channel": getattr(source, "channel", 1),
                "rate": getattr(source, "device_rate", None),
                "at": time.strftime("%Y-%m-%d %H:%M"),
            })
        except OSError as exc:
            # A read-only state directory is not a reason to lose a talk.
            self.log_event("MIC_NOT_REMEMBERED",
                           error=f"{type(exc).__name__}: {exc}")

    def _watch_quiet(self) -> None:
        """Is the input too quiet to be the microphone we meant?

        Called from the status poll rather than from the audio callback: the
        callback runs on PortAudio's thread and must do nothing but convert
        and hand over, and a level read once a second is plenty for a
        question measured in minutes.

        **The threshold is not the one that was asked for, and the
        measurement says why.** The spec asked for a warning below -35 dB
        average for a minute. The only Oct 31 recording that survives is the
        session that behaved - 36 minutes through the mixer - and in all of
        it the *loudest* 100 ms chunk was -35.2 dBFS, the 99th percentile was
        -45.2 and the 60 s rolling mean never rose above -54.3. A -35 dB
        average warning would have been on screen for 100% of a session that
        was working, which is worse than no warning at all.

        So the check is on the loudest chunk in the window, not the mean, and
        the floor is -50 dBFS: on that recording the loudest chunk per minute
        ran -51.8 to -35.2 with a median of -44.0, so -50 clears the working
        case by 6 dB and still catches an input that is off, muted or
        unplugged. We have no recording of a *wrong* device to calibrate the
        other side against, which is exactly why the level is now written to
        the event log either way - the next incident should not have to be
        argued from memory.

        **-50 dBFS is not wrong and is not enough, and 2026-11-07 is the
        measurement that says so.** Nine sessions through the same mixer
        line, all nine recorded. The two loud ones peaked at -0.6 and -4.8
        dBFS; the three mixer-line talks that transcribed ran per-minute
        loudest peaks of -54.9 to -25.9, and **six of the 42 minutes of
        2026-11-07_1418 - 41.6 minutes and 26,495 final tokens, a session
        that worked - sat below -50**. No floor separates those two cases:
        at -55 the same session still fires on its first minute. The level
        alone cannot answer this question.

        What answers it is the engine. A line returning committed words is
        the right input at any level, so the note now carries `speech`, the
        warning stops claiming the device is wrong while words are arriving,
        and the level is still written down either way - which is what
        keeps the next calibration arguable.
        """
        audio = self._audio
        if audio is None or not self.running:
            self._quiet_since = None
            return
        level = audio.level.as_dict()
        now = time.time()
        peak = level.get("peak_db", QUIET_FLOOR_DB)
        self._quiet_peak = max(self._quiet_peak, peak)
        if peak >= QUIET_PEAK_DB:
            # Something in the room is loud enough. Start the clock again.
            self._quiet_since = None
            self._quiet_logged = False
            self.quiet_note = None
            self._quiet_peak = QUIET_FLOOR_DB
            return
        if self._quiet_since is None:
            self._quiet_since = now
            self._quiet_peak = peak
            return
        quiet_for = now - self._quiet_since
        if quiet_for < QUIET_SECONDS:
            return
        speech = (self._last_speech_at is not None
                  and now - self._last_speech_at <= SPEECH_RECENT_SECONDS)
        self.quiet_note = {
            "seconds": round(quiet_for, 1),
            "peak_db": round(self._quiet_peak, 1),
            "db": level.get("db"),
            "device": getattr(audio, "device_name", "") or audio.describe(),
            # "...and the engine is transcribing it anyway." The difference
            # between a quiet input and a wrong one, which no dB figure on
            # its own has ever been able to tell.
            "speech": speech,
        }
        if not self._quiet_logged:
            self._quiet_logged = True
            self.log_event(session_log.AUDIO_QUIET, **self.quiet_note,
                           floor_db=QUIET_PEAK_DB)

    def _watch_language(self, response: dict) -> None:
        """Has the engine stopped hearing English? Act if it has.

        The answer costs one pass over the response's final tokens and a
        lookup per word, which is why it runs on every response rather than
        on a timer: the window is the thing that smooths it, and a timer
        would only add a second clock to a module built not to have one.

        A stuck signal reconnects Soniox **through the rollover path** - the
        same `reconfigure` that "Apply now" uses, with the config unchanged.
        The current stream ends gracefully, another opens, the ring buffer
        covers the join, and the room sees nothing: no gap notice, no change
        to `live`, no paragraph lost. That is the whole reason this is the
        remedy and Stop + Start is not. Stop + Start was what the operator
        had on 2026-10-31, and it ends the talk to fix the engine.

        Nothing here may raise into the feed. A detector that takes a talk
        down is worse than the fault it watches for, so the reconnect is
        guarded and a failure is written down and left.
        """
        watch = self._lang
        if watch is None:
            return
        now = self._elapsed()
        try:
            signals = watch.note_response(response, now)
        except Exception as exc:        # a shape no token has ever had
            self._lang = None
            self.log_event("LANG_WATCH_FAILED",
                           error=f"{type(exc).__name__}: {exc}")
            return
        for signal in signals:
            if signal.kind == DRIFT:
                # Written down and nothing else, on purpose: on 2026-10-31
                # this held for 17 minutes of a session that recovered twice
                # on its own, so acting on it would have ended two talks
                # that did not need ending.
                self.log_event(session_log.LANG_DRIFT, **signal.as_dict())
                self.lang_drifted = True
                self.hub.notify()
                continue
            if signal.kind != STUCK:
                continue
            self.log_event(session_log.LANG_STUCK, **signal.as_dict(),
                           diagnostic=self._lang_diagnostic())
            if not watch.act:
                continue
            engine = self._engine
            applied = False
            if engine is not None and self.running:
                try:
                    applied = engine.reconfigure(base_config(self.context()))
                except Exception as exc:
                    self.log_event("LANG_RECONNECT_FAILED",
                                   error=f"{type(exc).__name__}: {exc}")
            if applied:
                watch.reconnected(now)
            self.lang_note = {
                "at": round(now, 1),
                "share": round(signal.share, 4),
                "reconnected": applied,
                "reconnects": len(watch.reconnects),
                "capped": watch.capped,
            }
            self.hub.notify()
        # **Outside the loop, on purpose.** Once the hourly cap is reached
        # the watcher stops producing signals at all - that is what the cap
        # *is* - so a check inside the loop above is unreachable exactly when
        # it matters, and `LANG_CAPPED` would never be written. Found by
        # reading the two gates against each other rather than by a test,
        # which is why there is one now.
        if watch.capped and not self._lang_capped:
            self._lang_capped = True
            self.log_event(session_log.LANG_CAPPED,
                           reconnects=len(watch.reconnects),
                           diagnostic=self._lang_diagnostic())
            # Six reconnects inside an hour that did not fix it means the
            # remedy is wrong. Say so in red and let the operator decide
            # rather than restarting the engine for the rest of the talk.
            self.lang_note = {
                "at": round(now, 1),
                "share": round(watch.peak_share, 4),
                "reconnected": False,
                "reconnects": len(watch.reconnects),
                "capped": True,
            }
            self.hub.notify()

    def _lang_diagnostic(self) -> dict:
        """What the session looked like when the signal fired. Counts only.

        The event log holds neither speech nor session details, so this is
        the engine's own counters and the watcher's shares - enough to tell
        a lock apart from a reconnect storm or a quiet room afterwards,
        without a word of what was being said while it happened.
        """
        engine = self._engine
        watch = self._lang
        out: dict[str, Any] = {
            "responses": self.responses,
            "dropped_seconds": round(self.dropped_seconds, 1),
            "view": self.hub.screen.get("view"),
            "viewers": self.hub.viewer_count,
        }
        if engine is not None:
            out.update({
                "engine_state": engine.state,
                "reconnects": engine.reconnects,
                "rollovers": engine.rollovers,
                "reconfigures": engine.reconfigures,
                "streamed_seconds": round(engine.audio_seconds_sent, 1),
                "lag_seconds": round(engine.lag_seconds, 2),
            })
        if watch is not None:
            out.update(watch.status(self._elapsed()))
        level = self.level
        if level is not None:
            out["level_db"] = level.get("db")
            out["peak_db"] = level.get("peak_db")
        return out

    def _on_audio_dropped(self, seconds: float) -> None:
        """Speech was captured and never sent. Say so on the page.

        The alternative to a replay that keeps the captions permanently late
        (the decision of 2026-10-13) is a visible gap, and a gap nobody is
        told about reads as the speaker changing the subject.
        """
        self.dropped_seconds += seconds
        self.log_event(session_log.AUDIO_DROPPED, seconds=round(seconds, 1),
                       total=round(self.dropped_seconds, 1))
        self.state.add_notice()
        self.hub.notify()

    async def _run_live(self, client: SonioxClient) -> None:
        try:
            async for response in client:
                self.responses += 1
                if self._capture is not None:
                    self._capture.add(response,
                                      sent_ms=round(client.audio_seconds_sent * 1000))
                if self._transcript is not None:
                    # Before the captions, because this reads the response's
                    # own word timings; after them it would be reading the
                    # same dict, but the order says which half is which.
                    self._transcript.note_response(response)
                if _has_final_token(response):
                    self._last_speech_at = time.time()
                    # The engine returned words, so the room is not empty
                    # whatever the level reads. See `SonioxClient.note_speech`
                    # - and 2026-11-07, the talk that paused while being
                    # transcribed because the mixer line never crossed a
                    # threshold measured off a laptop microphone.
                    client.note_speech()
                if self.state.add_response(response):
                    self.hub.notify()
                if self._transcript is not None:
                    # Every response, so the 200-paragraph trim can never
                    # drop a closed paragraph before it has been written.
                    self._transcript.observe(self.state, self._elapsed())
                self._watch_language(response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            if self._capture is not None:
                self._capture.close()
            if client.last_error and not self.error:
                self.error = "{}: {}".format(
                    client.last_error.get("error_type"),
                    client.last_error.get("error_message", ""),
                ).strip(": ")
            streamed = client.audio_seconds_sent
            await client.close()
            audio, self._audio = self._audio, None
            if audio is not None:
                await audio.close()
            self.state.set_status("idle")
            self._finish(streamed)

    def _elapsed(self) -> float:
        """Seconds since Start. The transcript's clock, supplied from here
        because `CaptionState` and `TranscriptRecorder` are both clockless."""
        return time.time() - self.started_at if self.started_at else 0.0

    def _close_transcript(self) -> None:
        """Write the headers and the stats, then let go of the files.

        Runs from `_finish`, which the feed's `finally` always reaches - so
        Stop, an engine error and a source running out all land here.
        """
        recorder, self._transcript = self._transcript, None
        if recorder is None:
            return
        try:
            recorder.finalize(self.state, self._elapsed())
        except OSError as exc:
            self.error = self.error or f"transcript not saved: {exc}"
        finally:
            recorder.close()

    def _finish(self, audio_seconds: float) -> None:
        """End of run: drop the live flag and write the usage line.

        Called from the feed's `finally`, so it runs whether the source ran
        out, failed, or was cancelled by the operator. `_logged` guards
        against a second line when `stop()` also tidies up.
        """
        self._close_transcript()
        self.state.set_live(False)
        self.hub.notify()
        self._close_logs("error" if self.error else "finished")
        if self._logged:
            return
        self._logged = True
        usage.record_run(
            engine=self.engine,
            source=self.mode,
            audio_seconds=audio_seconds,
            detail=self.source or "",
        )

    async def stop(self) -> None:
        # Ask the engine to flush before anything is cancelled. Soniox holds
        # an open segment until it is told the audio ended, so cutting the
        # socket here loses the last words of the talk - a live run stopped
        # at "and that is all from me". Bounded, so a dead link cannot
        # hang Stop.
        engine = self._engine
        if engine is not None and self.running:
            await engine.finish()
            if self._task is not None and not self._task.done():
                # ...and give `_run_live` a moment to fold the tokens that
                # flush produced into the captions before they are dropped.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(self._task), FINISH_DRAIN_SECONDS
                    )

        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        engine, self._engine = self._engine, None
        if engine is not None:
            await engine.close()
        audio, self._audio = self._audio, None
        if audio is not None:
            await audio.close()
        self.state.set_live(False)
        self.state.set_status("idle")
        self.hub.notify()
        # Belt and braces, and not hypothetical: a task cancelled before it
        # has taken its first step never runs its own `finally`, so a
        # session stopped in the same breath as it started would leave no
        # stop line and an open file. `_close_logs` is a no-op when the feed
        # already closed them, which is the ordinary case.
        self._close_logs("stopped")

    async def shutdown(self) -> None:
        await self.stop()
        await self.hub.stop()

    def status(self) -> dict[str, Any]:
        """Everything `/admin` shows. No API key, no caption text."""
        # The level check rides the poll rather than carrying its own timer:
        # it is a question measured in minutes and this is the one thing
        # that runs once a second while a session is up.
        self._watch_quiet()
        client = self._client
        audio = self._audio
        status: dict[str, Any] = {
            "running": self.running,
            "mode": self.mode,
            # `self.engine` is deliberately absent: it names the engine, and
            # both the served static files and this payload are checked
            # against that name (the security rules). `engine_state` below
            # carries everything the operator actually needs.
            "source": self.source,
            "paced": self.paced,
            "responses": self.responses,
            # From the session, not the engine, so it survives the engine
            # being torn down at Stop - the operator wants to know what was
            # lost *after* the talk, not only during it.
            "dropped_seconds": round(self.dropped_seconds, 1),
            "capture": str(self._capture.path) if self._capture else None,
            # The folder's name, not a word of what is in it. `/admin` uses
            # it to say "this run is being written down"; the text itself
            # leaves the laptop only through the download routes.
            "transcript": (self.transcript_dir.name
                           if self.transcript_dir else None),
            # Counts, never values. The full details go out only from
            # `/admin/session`, which is a separate request behind the same
            # token - so nothing that merely polls status ever carries a
            # speaker's name.
            "details": self.details.summary(),
            "details_pending": self.details_pending,
            "error": self.error,
            "elapsed": (
                round(time.time() - self.started_at, 1)
                if self.started_at else None
            ),
            "audio_ms": client.sent_ms if client else 0,
            "audio_total_s": round(client.audio_seconds, 1) if client else 0,
            "captures": available_captures(),
            "audio_files": available_audio(),
            # Which speech model a live run will ask for. Always present,
            # because the question "what will this start" is asked before
            # anything is running - and a live session overwrites it from
            # the config it actually sent (`SonioxClient.status`), so the
            # row never reports a `.env` edited mid-talk.
            "model": _model_name(),
            # What the projector is showing, so `/admin` can render the
            # control in the state the screens are actually in.
            "screen": dict(self.hub.screen),
            # Which of the three QR sources is live, so the operator can see
            # that the file they dropped in is the one on screen - and so a
            # card quietly showing the fallback does not look like one that
            # is working. `label` says what it is, never what is in it.
            "qr": dict(self.hub.qr),
            "usage": usage.totals().as_dict(),
            **self.hub.status(),
            # What the open pages say they are *actually* rendering, against
            # what the operator chose. Counts and enums; no address, no text.
            **self.hub.screens(),
            # This run's own name, so `/admin` can offer the download for the
            # session that is happening rather than only for finished ones.
            "session": self.session_id,
            # Every session with an event log, and what `logs/` is holding.
            # Nothing here ever deletes anything, so the operator is the one
            # who decides when to clean up - and can only decide it if they
            # can see it.
            "sessions": session_log.sessions()[:12],
            "logs_bytes": session_log.total_bytes(),
            # "The engine stopped hearing English and we reconnected", and
            # "this input is too quiet to be the microphone you meant".
            # Both outlive the session: the operator reads them after
            # pressing Stop at least as often as during the talk.
            "lang": self.lang_note,
            "quiet": self.quiet_note,
            # "The idle auto-stop closed the stream, and here is why", with
            # the device and the levels in it. Present only while the engine
            # is actually paused, so the page cannot show the pause of a run
            # that has since been stopped.
            "pause": self.pause_note,
        }
        if self._lang is not None:
            status.update(self._lang.status(self._elapsed()))
        # After the watcher, so the session's own flag wins: it is the one
        # that survives Stop, and it is always present.
        status["lang_drifted"] = self.lang_drifted
        if self._engine is not None:
            status.update(self._engine.status())
        if audio is not None:
            # A live source overrides `audio_ms`: for mic and file runs it is
            # audio actually captured, not a position in a recording. Only a
            # file knows its length, so `audio_total_s` stays absent for a mic.
            source_status = audio.status()
            total_ms = source_status.get("total_ms")
            status.update(source_status)
            status["audio_total_s"] = (
                round(total_ms / 1000, 1) if total_ms else 0
            )
        return status


session = CaptionSession()


#: Printed once, the first time a clone comes up with no token set. It is the
#: only time the token is ever written to a terminal, so it says where the
#: value went and what to do with it rather than just emitting a string.
NEW_TOKEN_MESSAGE = (
    "\n"
    "  ADMIN_TOKEN was not set, so one has been generated and written to\n"
    "  {env}\n"
    "\n"
    "  Open the operator page with it, on whatever host and port you started\n"
    "  the server on. For example:\n"
    "\n"
    "      {url}\n"
    "\n"
    "  This is printed once. The value is in that file from now on, which is\n"
    "  gitignored and is the only place the token belongs - do not pass one\n"
    "  on the command line, where it lands in shell history. Delete the line\n"
    "  to be issued a new one on the next start.\n"
)

#: The header a generated `.env` is created with, for a clone that has none.
_ENV_HEADER = (
    "# Local secrets. Gitignored - never commit this file.\n"
    "# See .env.example for every value this project reads.\n"
)


def _write_admin_token(token: str, env_path: Path) -> None:
    """Put `ADMIN_TOKEN` into `.env`, creating the file if there is none.

    Append-only and non-destructive: an existing file keeps every line it
    had, including a commented-out `ADMIN_TOKEN=` placeholder, because a
    rewrite here would be this code editing a file an operator hand-edits.
    `pydantic-settings` takes the last assignment, so appending wins.
    """
    exists = env_path.is_file()
    with env_path.open("a", encoding="utf-8") as handle:
        if not exists:
            handle.write(_ENV_HEADER)
        handle.write(f"\n# Generated on first run.\nADMIN_TOKEN={token}\n")


def _say(message: str) -> None:
    """Print, and actually flush it.

    `print` block-buffers when stdout is a pipe, and this server does not
    exit - so a token announced into a redirected log stayed in the buffer
    and the operator never saw the one thing that was printed once. Found
    exactly that way.
    """
    print(message, flush=True)


def ensure_admin_token(env_path: Path | None = None,
                       announce=_say) -> str:
    """Make sure there is an admin token, generating one on a fresh clone.

    A blank token used to be a refusal to start, on the grounds that a
    server which comes up and denies everything looks like a working one.
    That is still true, but it is the wrong trade for a project anyone can
    clone: the first run of a fresh checkout would fail on a missing secret
    before showing a single page.

    So instead of refusing, it generates. 32 bytes from `secrets`, written to
    `.env` and printed once, which is both a working first run *and* a real
    token - the thing a refusal was protecting against was an *empty* one,
    and `require_admin` still denies every request in that case.

    Returns the token in use. Idempotent: a token already set is returned
    untouched and nothing is written or printed.
    """
    existing = settings.admin_token.strip()
    if existing:
        return existing

    token = secrets.token_urlsafe(32)
    target = Path(env_path) if env_path else ROOT / ".env"
    try:
        _write_admin_token(token, target)
    except OSError as error:
        # A read-only checkout, or a directory somebody has locked down. The
        # token still works for this run; it just will not survive a restart,
        # which is worth saying out loud rather than failing over.
        announce(f"\n  Could not write {target}: {error}\n"
                 f"  Using a generated token for this run only:\n\n"
                 f"      ADMIN_TOKEN={token}\n")
        settings.admin_token = token
        return token

    settings.admin_token = token
    announce(NEW_TOKEN_MESSAGE.format(
        env=target, url=f"{settings.public_url}/admin?token={token}"))
    return token


NO_MODEL_MESSAGE = (
    "SONIOX_MODEL is {model!r}, which is not a model this build knows, so\n"
    "refusing to start.\n"
    "\n"
    "  The engine answers an unknown name with `model_not_available`, which\n"
    "  is fatal and does not arrive until Start - so the venue would find\n"
    "  out when the room was already seated.\n"
    "\n"
    "  Known models: {known}\n"
    "\n"
    "  Either fix the line in {env} or delete it, which selects the model\n"
    "  this project has measured."
)


def require_known_model() -> None:
    """Refuse to start on a model name this build does not know.

    Same reasoning as the admin token above: the failure is real either way,
    and the question is only whether the operator reads it in the terminal
    now or discovers it when the talk starts.
    """
    try:
        active_model()
    except ValueError:
        raise RuntimeError(NO_MODEL_MESSAGE.format(
            model=settings.soniox_model, known=", ".join(MODELS),
            env=ROOT / ".env",
        )) from None


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_admin_token()
    require_known_model()
    session.hub.start()
    try:
        yield
    finally:
        await session.shutdown()


app = FastAPI(
    title="Live Captions",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


# -- capture lookup --------------------------------------------------------


def available_captures() -> list[str]:
    """Replayable capture filenames, deduplicated, in a stable order."""
    names = {
        p.name
        for d in CAPTURE_DIRS if d.is_dir()
        for p in d.glob(CAPTURE_GLOBS.get(d, DEFAULT_CAPTURE_GLOB))
    }
    return sorted(names)


def available_audio() -> list[str]:
    """Recordings a `FileSource` run may be pointed at, newest first.

    Ordered by modification time because the useful one is almost always the
    clip just recorded, and `recordings/` accumulates.
    """
    if not RECORDING_DIR.is_dir():
        return []
    files = [
        p for p in RECORDING_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in files]


def resolve_audio(name: str) -> Path:
    """Look an audio file up inside `recordings/`, by filename only.

    Same containment as `resolve_capture`: only the final path component is
    used, so no request can name a file elsewhere on the laptop.
    """
    wanted = Path(name or "").name
    if Path(wanted).suffix.lower() not in AUDIO_SUFFIXES:
        raise HTTPException(status_code=400, detail="not an audio file")
    candidate = RECORDING_DIR / wanted
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="no such recording")
    return candidate


def resolve_capture(name: str) -> Path:
    """Look a capture up by filename inside `CAPTURE_DIRS`.

    Only the final path component is used, so `../../.env` and an absolute
    path both collapse to a harmless name that simply will not be found.
    """
    wanted = Path(name or "").name
    if not wanted.endswith(".jsonl"):
        raise HTTPException(status_code=400, detail="not a .jsonl capture")
    for directory in CAPTURE_DIRS:
        candidate = directory / wanted
        if not candidate.is_file():
            continue
        # A directory may only expose the files it lists; `logs/usage.jsonl`
        # is reachable by name otherwise, and it is not a capture.
        if not candidate.match(CAPTURE_GLOBS.get(directory,
                                                 DEFAULT_CAPTURE_GLOB)):
            continue
        return candidate
    raise HTTPException(status_code=404, detail="no such capture")


# -- admin auth ------------------------------------------------------------


def require_admin(token: str | None) -> None:
    """Constant-time token check (the security rules).

    An unset `ADMIN_TOKEN` denies everything rather than allowing everything:
    a fresh clone with no `.env` must not expose the operator page. The
    comparison runs on bytes because `compare_digest` rejects non-ASCII `str`,
    and a token is attacker-supplied text.
    """
    expected = settings.admin_token.encode()
    supplied = (token or "").encode()
    # Compared even when `expected` is empty, so the failure path costs the
    # same either way and reveals nothing about whether a token is configured.
    ok = secrets.compare_digest(supplied, expected) and bool(expected)
    if not ok:
        raise HTTPException(status_code=401, detail="admin token required")


# -- pages -----------------------------------------------------------------


def _page(name: str) -> FileResponse:
    return FileResponse(WEB_DIR / name, media_type="text/html; charset=utf-8",
                        headers=NO_STORE)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/")
async def index() -> FileResponse:
    return _page("index.html")


@app.get("/screen")
async def screen() -> FileResponse:
    """Big-screen view. `?lang=` and `?lines=` are read client-side."""
    return _page("screen.html")


#: The last code *generated*, as `(url, svg)`. Drawing one is a few
#: milliseconds, but the projector asks for it on every reconnect and a page
#: that reloads during a talk should cost nothing. Keyed by the URL itself,
#: so an operator changing the link invalidates it by definition rather than
#: by a timer. A code taken from the organizer's file is not cached here -
#: it is read off disk and sent as-is.
_qr_cache: tuple[str, str] | None = None

#: Served with every QR. The code is meant to be read by a roomful of phone
#: cameras, so there is nothing to protect about the image - but one of the
#: three sources is **a file somebody dropped into a directory by hand**, and
#: an SVG is a document: opened as a top-level page rather than through an
#: `<img>`, it can carry script. The page only ever references it from an
#: `<img>`, which already neuters it; this says the same thing to a browser
#: that arrives at the URL directly, and `nosniff` stops a PNG being
#: reinterpreted as something livelier.
QR_HEADERS = {
    "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


def _qr_response(wanted: str) -> Response:
    """The active QR, if it is the kind of file that was asked for.

    Three sources, in `app.invite_qr.resolve`: the organizer's own artwork
    in the state directory, a code generated from the operator's invite
    link, or one generated from the default link so the card is never
    empty. The page is told which extension to ask for, so a request for
    the other one is a stale tab rather than a normal path.
    """
    global _qr_cache

    source = resolve_qr(str(session.hub.screen["invite"]))
    if source.extension != wanted:
        raise HTTPException(status_code=404, detail="not the active QR")
    headers = {**NO_STORE, **QR_HEADERS, "ETag": f'"{source.tag}"'}
    if source.from_file:
        # Byte for byte. It is the organizer's artwork and may be the same
        # code they have already printed and handed round; re-drawing it
        # here would be a different image making the same promise.
        return Response(content=source.path.read_bytes(),
                        media_type=source.media_type, headers=headers)
    if _qr_cache is None or _qr_cache[0] != source.url:
        _qr_cache = (source.url, render_qr(source.url))
    return Response(content=_qr_cache[1], media_type="image/svg+xml",
                    # No-store, with the fingerprint in the page's query
                    # string doing the cache-busting instead. A projector
                    # that caches yesterday's code is a room full of people
                    # joining the wrong group.
                    headers=headers)


@app.get("/screen/qr.svg")
async def screen_qr_svg() -> Response:
    return _qr_response("svg")


@app.get("/screen/qr.png")
async def screen_qr_png() -> Response:
    return _qr_response("png")


@app.get("/admin")
async def admin(token: str | None = Query(default=None)) -> FileResponse:
    require_admin(token)
    return _page("admin.html")


@app.get("/static/{name}")
async def asset(name: str) -> FileResponse:
    media_type = ASSETS.get(name)
    if media_type is None:
        raise HTTPException(status_code=404, detail="not found")
    path = WEB_DIR / name
    if not path.is_file():
        # `brand-sizes.css` is generated and optional, so this is a normal
        # state on a clone that has never run `tools/brand_assets.py` - not
        # a 500.
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type=media_type, headers=NO_STORE)


@app.get("/brand.css")
async def brand_stylesheet() -> Response:
    """The theme, as a stylesheet the server writes on every request.

    It overrides the colour tokens and hides the image slots this install has
    no artwork for - see `app.brand.brand_css` for why that has to be
    generated rather than static. Cheap: a dict, a directory listing of at
    most five names, and some string joining.

    No-store, like the pages. An operator who drops new artwork into
    `brand/` and reloads the projector must see it.
    """
    return Response(content=brand_css(), media_type="text/css; charset=utf-8",
                    headers=NO_STORE)


@app.get("/brand.json")
async def brand_values() -> JSONResponse:
    """The theme's words, for the pages that set the event name in type.

    Only what a page needs to render: the name and the colours. The waiting
    screen's wording does not come through here - it rides on the viewer
    socket with the rest of the screen settings, because the operator can
    change it mid-talk and the projector has to follow.

    Public by construction. Everything in it is read by a roomful of people
    off a projector, and `tests/test_session_privacy.py` asserts that no
    session detail can reach this route.
    """
    brand = load_brand()
    return JSONResponse(
        {"event_name": brand["event_name"], "accent": brand["accent"]},
        headers=NO_STORE)


@app.get("/brand/{name}")
async def brand_asset(name: str) -> FileResponse:
    """One brand image out of the gitignored `brand/` directory.

    `name` is matched against `app.brand.SLOTS` before anything touches the
    filesystem, so a request can only ever name one of five files and no
    path from a client is ever joined onto a directory. A slot with no file
    behind it is a 404 - which the page never sees, because `/brand.css` has
    already hidden the element that would have asked for it.
    """
    media_type = BRAND_SLOTS.get(name)
    if media_type is None:
        raise HTTPException(status_code=404, detail="not found")
    path = brand_image(name)
    if path is None:
        raise HTTPException(status_code=404, detail="no artwork for this slot")
    return FileResponse(path, media_type=media_type, headers=NO_STORE)


# -- operator actions ------------------------------------------------------


@app.get("/admin/status")
async def admin_status(token: str | None = Query(default=None)) -> JSONResponse:
    require_admin(token)
    # The one place that looks for the organizer's QR file. A file dropped
    # in while the server is up reaches every open projector on the next
    # poll, without anyone touching the browser on the stage.
    session.refresh_qr()
    return JSONResponse(session.status(), headers=NO_STORE)


@app.get("/admin/session")
async def admin_session(token: str | None = Query(default=None)) -> JSONResponse:
    """This talk's details, in full.

    **The only route that returns these values**, and it is behind the admin
    token. Everything else - the audience pages, the viewer socket, the status
    poll, every log - carries counts at most.
    """
    require_admin(token)
    return JSONResponse(
        {
            "details": session.details.model_dump(),
            "pending": session.details_pending,
            "running": session.running,
            # So the operator can see what the glossary already covers and
            # not retype it. Public vocabulary, safe to show.
            "glossary_terms": load_glossary().terms,
        },
        headers=NO_STORE,
    )


@app.post("/admin/session")
async def admin_save_session(
    token: str | None = Query(default=None),
    details: SessionContext | None = None,
) -> JSONResponse:
    """Save this talk's details. Written atomically, adopted immediately.

    Applied to a *running* engine only on request - see `/admin/session/apply`.
    Reconnecting without being asked would be a surprise in the middle of a
    talk, and the operator may want to type several fields before any of it
    takes effect.
    """
    require_admin(token)
    needs_apply = session.save_details(details or SessionContext())
    return JSONResponse(
        {"saved": True, "pending": needs_apply,
         "details": session.details.model_dump()},
        headers=NO_STORE,
    )


@app.post("/admin/session/apply")
async def admin_apply_session(
    token: str | None = Query(default=None),
) -> JSONResponse:
    """Reconnect the engine so saved details take effect mid-talk.

    Uses the rollover path: the current stream ends gracefully, a new one
    opens with the new context, and the ring buffer covers the join. No audio
    is dropped and the audience sees no notice, because nothing went wrong.
    """
    require_admin(token)
    applied = session.apply_details()
    return JSONResponse(
        {"applied": applied, "pending": session.details_pending},
        headers=NO_STORE,
    )


@app.get("/admin/level")
async def admin_level(token: str | None = Query(default=None)) -> JSONResponse:
    """Just the input level.

    The meter wants several updates a second - a level that moves once a
    second cannot show a syllable - and `/admin/status` is too expensive to
    ask that often: it stats the capture directories and re-reads the usage
    ledger. This returns two numbers and touches no disk.
    """
    require_admin(token)
    return JSONResponse(
        {"running": session.running, "level": session.level},
        headers=NO_STORE,
    )


def device_payload(devices: list[dict]) -> dict:
    """The dropdown, plus what the last live session actually opened.

    `remembered` is the name, not the index: PortAudio's indices move when
    anything is plugged in, so the index is sent as a hint and the name is
    what decides. `missing` is the case the operator needs told about - the
    device that worked last time is not in this list, which on 2026-10-31
    was the whole fault and showed up nowhere.
    """
    mic = load_mic()
    name = str(mic.get("name") or "")
    present = next((d for d in devices if d.get("name") == name), None)
    return {
        "devices": devices,
        "remembered": name,
        "remembered_index": (present or {}).get("index", mic.get("index")),
        "remembered_channel": mic.get("channel", 1),
        "remembered_at": mic.get("at", ""),
        "missing": bool(name) and present is None,
        # **Remembered, and still off by default.** A fresh install has no
        # file and this is False, which is the only acceptable default: the
        # room has not consented to being recorded. What it fixes is the
        # *other* failure - an operator who ticked it last week, remembers
        # ticking it, and has had it silently reset by every page load since.
        # On 2026-10-31 that cost two locked talks their audio.
        "remembered_record": bool(mic.get("record", False)),
    }


@app.get("/admin/devices")
async def admin_devices(token: str | None = Query(default=None)) -> JSONResponse:
    """Input devices for the operator's dropdown.

    Its own endpoint rather than part of `/admin/status`: querying PortAudio
    takes tens of milliseconds and the device list does not change between
    polls, so doing it once a second would be a waste that grows with every
    page left open.
    """
    require_admin(token)
    return JSONResponse(device_payload(describe_devices()), headers=NO_STORE)


@app.post("/admin/devices/refresh")
async def admin_devices_refresh(
    token: str | None = Query(default=None),
) -> JSONResponse:
    """Re-read the device list from the driver.

    **Refused while a session is running**, with 409 and a reason the page
    shows: re-reading means re-initialising PortAudio, which invalidates
    every stream it holds, and doing that mid-talk would take the audio away
    to fix a dropdown. Stopped, it is free and takes a moment.
    """
    require_admin(token)
    if session.running:
        raise HTTPException(
            status_code=409,
            detail="Stop the session first - re-reading the device list "
                   "restarts the audio driver and would cut the stream.",
        )
    return JSONResponse(device_payload(reload_devices()), headers=NO_STORE)


@app.post("/admin/start")
async def admin_start(
    token: str | None = Query(default=None),
    # Plain defaults rather than `Query(default=...)`: `tests/test_server.py`
    # calls these handlers as the coroutines they are, and an unresolved
    # `Query` object would arrive as the argument's value. FastAPI treats a
    # scalar with a plain default as a query parameter either way.
    source: str = "replay",
    capture: str | None = None,
    file: str | None = None,
    device: int | None = None,
    channel: int = 1,
    record: bool = False,
    # Not `capture`: that already names the replay file to play back. This
    # one records the engine's raw responses from a live run.
    log_responses: bool = False,
    # The readable transcript. Default on, so a talk is written down unless
    # the operator unticks it - a speaker who does not want to be recorded is
    # the exception, not the rule. **Honoured only for `source=live`**: the
    # other three sources never reach `start_live`, which is the only thing
    # in this file that can build a recorder, so `transcript=true` on a
    # replay is accepted and does nothing.
    transcript: bool = True,
    transcript_title: bool = False,
    paced: bool = True,
    # Replay only, and a testing hatch like `?card=1` on the screen: see
    # `CaptionSession.start_replay`.
    speed: float = 1.0,
) -> JSONResponse:
    """Start a session.

    `source=replay` plays a saved capture through `CaptionState` - captions,
    no audio. `source=mic` and `source=file` capture audio and measure it -
    audio, no captions, because the engine arrives in Phase 5. Neither one
    can spend Soniox credit.
    """
    require_admin(token)
    if source == "replay":
        if not capture:
            raise HTTPException(status_code=400, detail="capture required")
        await session.start_replay(resolve_capture(capture), paced=paced,
                                   speed=speed)
    elif source == "mic":
        try:
            await session.start_audio(
                MicSource(device, channel=channel, record=record)
            )
        except Exception as exc:
            # A device that cannot be opened is the operator's problem to
            # fix, not a server fault: say which device and why, and leave
            # the session stopped rather than half-started.
            raise HTTPException(
                status_code=400,
                detail=f"could not open device {device}: {exc}",
            ) from exc
    elif source == "file":
        if not file:
            raise HTTPException(status_code=400, detail="file required")
        await session.start_audio(
            FileSource(resolve_audio(file), realtime=paced,
                       channel=channel, record=record)
        )
    elif source == "live":
        # The only route in this file that spends money. One shape, one
        # source: the microphone. Streaming a saved file to the engine is
        # what `tools/probe_soniox.py` is for, and keeping it out of here
        # means there is exactly one way to start billing.
        try:
            mic = MicSource(device, channel=channel, record=record)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"could not open device {device}: {exc}",
            ) from exc
        try:
            await session.start_live(mic, capture=log_responses,
                                     transcript=transcript,
                                     transcript_title=transcript_title)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"could not start live transcription: {exc}",
            ) from exc
    else:
        raise HTTPException(status_code=400, detail="source must be "
                                                   "replay, mic, file or live")
    if source in ("mic", "file", "live"):
        # **Remember the tick, from the route rather than from the source.**
        # This is the operator's literal choice, and it is the one thing
        # about a session that nothing could read back afterwards: a missing
        # WAV is indistinguishable from a deleted one. Persisted after the
        # start succeeded, so a device that would not open does not rewrite
        # the preference on its way to a 400.
        #
        # It is still off on a fresh install. What this fixes is the box
        # being silently cleared by every page load, which is what cost
        # 2026-10-31 the audio of the two talks that locked.
        try:
            update_mic({"record": bool(record)})
        except OSError:
            pass
    return JSONResponse(session.status(), headers=NO_STORE)


@app.post("/admin/resume")
async def admin_resume(token: str | None = Query(default=None)) -> JSONResponse:
    """Come out of the idle pause without starting a new session.

    **Refused with 409 when nothing is paused**, and the reason is on the
    page: a button that reports success whatever the state teaches an
    operator to press it twice, and the one thing they must not do mid-talk
    is reach for Stop because a control looked inert.

    The pause closed the Soniox stream and nothing else. This reopens it
    through the path a rollover uses, so the session, the recorder, the
    transcript folder and every open page carry straight on. Until now the
    only way out was Stop + Start, which is what 2026-11-07 had to do.
    """
    require_admin(token)
    if not session.resume():
        raise HTTPException(
            status_code=409,
            detail="Nothing is paused, so there is nothing to resume.",
        )
    return JSONResponse(session.status(), headers=NO_STORE)


@app.post("/admin/stop")
async def admin_stop(token: str | None = Query(default=None)) -> JSONResponse:
    require_admin(token)
    await session.stop()
    return JSONResponse(session.status(), headers=NO_STORE)


# -- transcripts -----------------------------------------------------------
#
# A transcript is a verbatim record of a room, so these four routes are the
# only way to reach one and every one of them is behind the admin token.
# `transcripts/` is gitignored, is not in `ASSETS`, and there is no route
# that takes a path - the folder is named by a request and checked against
# `transcript.FOLDER_NAME` before any path is built from it, so `..` is
# rejected by shape rather than by sanitizing.


def transcript_folder(name: str) -> Path:
    """Resolve one transcript folder from a request. 404 if it is not one."""
    if not is_safe_folder(name or ""):
        raise HTTPException(status_code=404, detail="no such transcript")
    directory = TRANSCRIPT_DIR / name
    if not directory.is_dir():
        raise HTTPException(status_code=404, detail="no such transcript")
    return directory


@app.get("/admin/transcripts")
async def admin_transcripts(
    token: str | None = Query(default=None),
) -> JSONResponse:
    """What has been written down, as names, sizes and durations.

    Counts, never text - the same rule `/admin/status` follows. Reading a
    transcript takes a second request for a named file.
    """
    require_admin(token)
    return JSONResponse({"transcripts": list_transcripts(TRANSCRIPT_DIR)},
                        headers=NO_STORE)


@app.get("/admin/transcripts/file")
async def admin_transcript_file(
    token: str | None = Query(default=None),
    name: str = "",
    file: str = "",
) -> FileResponse:
    """One file out of one folder, by name, from the allowlist."""
    require_admin(token)
    directory = transcript_folder(name)
    if file not in TRANSCRIPT_FILES:
        raise HTTPException(status_code=404, detail="no such file")
    path = directory / file
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    media = ("application/json" if file.endswith(".json")
             else "text/markdown; charset=utf-8")
    return FileResponse(path, media_type=media, headers={
        **NO_STORE,
        # Named for the session, so three downloads from three talks do not
        # all land in the operator's downloads folder as `english.md`.
        "Content-Disposition":
            f'attachment; filename="captions-{name}-{file}"',
    })


@app.get("/admin/transcripts/zip")
async def admin_transcript_zip(
    token: str | None = Query(default=None),
    name: str = "",
) -> Response:
    """The whole folder, for sending to the organiser in one go.

    Built in memory: a transcript is a few hundred kilobytes of text, and a
    temporary file would be a second copy of a verbatim transcript sitting
    somewhere nobody is watching.
    """
    require_admin(token)
    directory = transcript_folder(name)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in TRANSCRIPT_FILES:
            path = directory / member
            if path.is_file():
                archive.write(path, arcname=f"captions-{name}/{member}")
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={**NO_STORE, "Content-Disposition":
                 f'attachment; filename="captions-{name}.zip"'},
    )


@app.post("/admin/transcripts/delete")
async def admin_transcript_delete(
    token: str | None = Query(default=None),
    name: str = "",
) -> JSONResponse:
    """Remove one folder. The page asks first; this does not ask twice."""
    require_admin(token)
    transcript_folder(name)
    removed = delete_transcript(TRANSCRIPT_DIR, name)
    return JSONResponse({"deleted": removed,
                         "transcripts": list_transcripts(TRANSCRIPT_DIR)},
                        headers=NO_STORE)


@app.post("/admin/transcripts/build")
async def admin_transcript_build(
    token: str | None = Query(default=None),
    capture: str = "",
) -> JSONResponse:
    """Make a transcript out of a live session that was logged but not written.

    The 2026-10-17 session is the reason: two hours of real bilingual speech,
    logged, from a night when this feature did not exist.

    **Live logs only.** `logs/live-*.jsonl` is what a live run leaves behind;
    a fixture or a capture file is a recording that was replayed, and
    replays do not produce transcripts - which has to stay true whichever
    direction it is approached from, or the rule could be walked around by
    rebuilding.

    Resolved against `LOG_DIR` directly rather than through
    `resolve_capture`, which searches three directories: the only file this
    route may read is a live log, so there is nothing to search for. Only the
    final path component is used, so `../../.env` collapses to a name that is
    not a live log and is refused by shape before any path is built.

    Offline and free: the log replays through `CaptionState` exactly as
    `tools/render_capture.py` does, and nothing opens a socket.
    """
    require_admin(token)
    wanted = Path(capture or "").name
    if stamp_from_log(Path(wanted)) is None:
        raise HTTPException(
            status_code=400,
            detail="only a live session log (logs/live-*.jsonl) has a "
                   "transcript to make",
        )
    path = LOG_DIR / wanted
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such log")
    try:
        directory = await asyncio.to_thread(
            build_from_log, path, TRANSCRIPT_DIR,
            glossary=load_glossary().terms,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"built": directory.name,
                         "transcripts": list_transcripts(TRANSCRIPT_DIR)},
                        headers=NO_STORE)


@app.post("/admin/screen")
async def admin_screen(
    token: str | None = Query(default=None),
    view: str | None = None,
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
) -> JSONResponse:
    """Point every open `/screen` at a view and a text size.

    The projector is usually a browser nobody can reach once the talk starts -
    it is behind the lectern, or on a stick PC, or simply across the room. So
    the setting is pushed down the socket the page already has rather than
    being something the operator walks over and types.

    The same route carries the waiting screen's words and its join link.
    Carries no session details: speaker names and the talk title live in
    `config/session.yaml` and reach `/admin` and the Soniox context only, so
    nothing a speaker is called can arrive on the projector through here
    unless the operator types it in themselves.

    The link is the one value on this route that does not reach a viewer.
    `BroadcastHub.public_screen` drops it from every snapshot and diff; the
    page asks `/screen/qr.svg` for the picture instead.
    """
    require_admin(token)
    session.hub.set_screen(view=view, size=size, logo=logo, footer=footer,
                           labels=labels, glow=glow,
                           credit=credit, eyebrow=eyebrow,
                           headline=headline, highlight=highlight,
                           subline=subline, pill=pill, cta=cta, note=note,
                           invite=invite)
    # The link may have changed, which changes the generated code.
    session.refresh_qr()
    # The whole status, like `/admin/start` and `/admin/stop`, so the page can
    # feed it straight back into its one `render` path.
    return JSONResponse(session.status(), headers=NO_STORE)


@app.post("/admin/mark")
async def admin_mark(
    token: str | None = Query(default=None),
    note: str = "",
) -> JSONResponse:
    """Write an operator mark into the event log, now.

    The point is the timestamp. On 2026-10-24 the fault was noticed
    somewhere "after about half an hour", and placing that against the logs
    afterwards took an afternoon of arithmetic. One button at the moment it
    is seen turns that into a `grep`.

    The note is the operator's own words about what they are looking at, so
    it goes in as typed, capped at a line. Nothing from the captions.
    """
    require_admin(token)
    text = tidy_line(note, 200)
    session.log_event(session_log.OPERATOR_MARK, note=text,
                      screens=session.hub.screens()["screens"]["count"])
    return JSONResponse(session.status(), headers=NO_STORE)


def _transcript_folder_for(session: str) -> str:
    """The transcript folder a session would have written, by name.

    The two namings differ by history rather than by design - a session is
    `2026-10-24-143245` and its transcript folder is `2026-10-24_1432` - so
    this is the one place that knows both.
    """
    return f"{session[:10]}_{session[11:15]}"


@app.get("/admin/diagnostics")
async def admin_diagnostics(
    token: str | None = Query(default=None),
    name: str = "",
) -> Response:
    """One session's evidence, zipped: everything but the audio.

    The response log (if the operator kept one), the event log, the health
    log, the settings-audit lines that fall inside the session's window, and
    the transcript's `stats.json` if there is one. This is what gets handed
    over when something went wrong on a night, and it is assembled rather
    than described so that handing it over is one click and not an
    archaeology lesson.

    The response log and the transcript hold speech; the other three hold
    counts and flags. The README says which is which, because the operator
    is the one deciding who to send it to.
    """
    require_admin(token)
    stem = Path(name or "").name
    if not SESSION_NAME.match(stem):
        raise HTTPException(status_code=400, detail="not a session name")

    members: list[tuple[str, Path]] = [
        ("events.jsonl", session_log.event_path(stem)),
        ("screen-health.jsonl", session_log.health_path(stem)),
        ("responses.jsonl", LOG_DIR / f"live-{stem}.jsonl"),
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, path in members:
            if path.is_file():
                archive.write(path, arcname=f"captions-{stem}/{member}")
        audit = screen_audit.audit_path()
        if audit.is_file():
            # Only the lines from the day of the session: the audit file is
            # a running history and the rest of it is not this session's
            # evidence.
            day = stem[:10]
            try:
                lines = [line for line in audit.read_text(encoding="utf-8")
                         .splitlines() if line.startswith(day)]
            except OSError:
                lines = []
            if lines:
                archive.writestr(f"captions-{stem}/screen-settings.log",
                                 "\n".join(lines) + "\n")
        stats = TRANSCRIPT_DIR / _transcript_folder_for(stem) / "stats.json"
        if stats.is_file():
            archive.write(stats, arcname=f"captions-{stem}/stats.json")
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={**NO_STORE, "Content-Disposition":
                 f'attachment; filename="captions-diagnostics-{stem}.zip"'},
    )


@app.post("/admin/screens/reload")
async def admin_screens_reload(
    token: str | None = Query(default=None),
) -> JSONResponse:
    """Reload every open `/screen`, without touching the session.

    The operator's mid-talk remedy. A projector is a browser nobody can reach
    once the talk starts, and until now the only way to make one start over
    was Stop + Start - which ends the talk, closes the Soniox stream and
    finalizes the transcript to fix a browser. This sends each page a reload
    down the socket it already has: captions keep arriving, the recorder keeps
    writing into the same folder, and each page comes back with a fresh
    snapshot because that is what a new socket is handed.
    """
    require_admin(token)
    session.hub.reload_screens()
    return JSONResponse(session.status(), headers=NO_STORE)


# -- viewers ---------------------------------------------------------------


@app.websocket("/ws")
async def viewer_socket(websocket: WebSocket) -> None:
    """A page. Receives a snapshot then diffs, and is otherwise read-only."""
    await websocket.accept()
    # `getattr`, because a viewer socket is a protocol in this project and
    # headers are not part of it: the tests' fakes implement `send_text` and
    # `receive` and nothing else, and a log line is not worth making them
    # implement more.
    headers = getattr(websocket, "headers", None) or {}
    viewer = await session.hub.add(
        websocket, agent=headers.get("user-agent", ""))
    if viewer is None:  # at capacity, or the socket died on the snapshot
        return
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            # Binary frames and oversized text are ignored, not errors: the
            # contract is that the server reads nothing but a resync request.
            if not text or len(text) > MAX_CLIENT_MESSAGE:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            if kind == "resync":
                session.hub.resync(viewer)
            elif kind == "health":
                # What this page is actually rendering. Validated by shape in
                # the hub, which takes the fields it knows and drops the
                # rest. See `BroadcastHub.note_health` for why it exists.
                session.hub.note_health(viewer, payload)
            elif kind == "diagnostic":
                # The answer to a `diagnose` request: everything the page can
                # say about itself, asked for once when it has been showing
                # the wrong language. Logged whole.
                session.hub.note_diagnostic(viewer, payload)
            elif kind == "error":
                # `window.onerror` and `unhandledrejection`. A page that
                # threw is a page that stopped updating, and until now that
                # left no trace anywhere.
                session.log_event(
                    session_log.PAGE_ERROR,
                    viewer=viewer.ident, path=viewer.path,
                    message=str(payload.get("message", ""))[:300],
                    source=str(payload.get("source", ""))[:200],
                    line=payload.get("line") if isinstance(
                        payload.get("line"), int) else None,
                    kind=str(payload.get("kind", ""))[:40],
                )
    except Exception:
        # Any receive failure means the page is gone. Nothing to report.
        pass
    finally:
        await session.hub.remove(viewer)
