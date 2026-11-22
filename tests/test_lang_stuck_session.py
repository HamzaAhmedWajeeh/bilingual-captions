"""The language watch inside a session, and the microphone fixes.

Everything here is offline. The Soniox client is driven by the scripted
socket from `test_soniox_client.py`, so "the signal triggers a rollover and
no audio is lost" is measured against the frames the fake server actually
received rather than asserted about the code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from pathlib import Path

from app import main as main_module
from app import session_log
from app.lang_watch import LanguageWatch
from app.main import (
    QUIET_PEAK_DB,
    admin_devices,
    admin_devices_refresh,
    device_payload,
    session,
)
from app.soniox_client import STREAMING, SonioxClient
from fastapi import HTTPException

from tests.test_soniox_client import (
    CHUNK_BYTES,
    FakeServer,
    audio_stream,
    tone_chunk,
)
from captures import needs_captures  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token"

# The detector's own lexicon: transliterated *of, is, from, this, not, now,
# think, how, what, because*. No real caption text.
TRANSLIT = ["اوف", "ايز", "فروم", "ذس", "نوت", "ناو", "ثينك", "هاو", "وات",
            "بيكوز"]
ARABIC = ["مرحبا", "الكلام", "نتكلم", "اليوم", "موضوع"]


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(main_module.settings, "admin_token", TOKEN)


@pytest.fixture(autouse=True)
def _state_dir(monkeypatch, tmp_path):
    """Never the operator's `config/`.

    `update_mic` writes `config/mic.yaml` under `STATE_DIR`, and a test run
    is an operator as far as that path is concerned - the same mistake that
    cost the real `screen.yaml` and `session.yaml` before `CAPTIONS_STATE_DIR`
    existed. Both names are redirected into a dict, so nothing here can
    reach the disk at all.
    """
    _state_dir.saved = {}
    # Read through the attribute rather than a closed-over dict, so a test
    # that replaces `_state_dir.saved` wholesale still drives both names.
    monkeypatch.setattr(main_module, "load_mic",
                        lambda *a, **k: dict(_state_dir.saved))

    def fake_update(changes, *a, **k):
        _state_dir.saved.update(changes)
        return dict(_state_dir.saved)

    monkeypatch.setattr(main_module, "update_mic", fake_update)
    yield


# ---------------------------------------------------------------------------
# The remembered microphone
# ---------------------------------------------------------------------------


DEVICES = [
    {"index": 0, "name": "Built-in Microphone Array", "channels": 2,
     "sample_rate": 44100, "api": "MME", "default": False},
    {"index": 3, "name": "Line In (USB Audio Mixer)", "channels": 2,
     "sample_rate": 16000, "api": "MME", "default": True},
]


def test_the_payload_preselects_the_device_that_worked_last_time():
    _state_dir.saved = {"name": "Line In (USB Audio Mixer)", "index": 3, "channel": 2,
                        "at": "2026-10-31 15:21"}
    payload = device_payload(DEVICES)
    assert payload["remembered"] == "Line In (USB Audio Mixer)"
    assert payload["remembered_index"] == 3
    assert payload["remembered_channel"] == 2
    assert payload["missing"] is False


def test_the_index_is_taken_from_the_list_not_from_the_file():
    """PortAudio renumbers when anything is plugged in, so last night's
    index may be a different device tonight. The name decides."""
    _state_dir.saved = {"name": "Line In (USB Audio Mixer)", "index": 11, "channel": 1}
    payload = device_payload(DEVICES)
    assert payload["remembered_index"] == 3
    assert payload["missing"] is False


def test_a_missing_device_is_reported_as_missing():
    """The one thing on 2026-10-31 that nothing anywhere said."""
    _state_dir.saved = {"name": "Line In (USB Audio Mixer)", "index": 3, "channel": 1}
    payload = device_payload([DEVICES[0]])
    assert payload["missing"] is True
    assert payload["remembered"] == "Line In (USB Audio Mixer)"


def test_nothing_remembered_is_not_a_warning():
    _state_dir.saved = {}
    payload = device_payload(DEVICES)
    assert payload["missing"] is False
    assert payload["remembered"] == ""


async def test_the_devices_route_carries_the_remembered_device(monkeypatch):
    _state_dir.saved = {"name": "Line In (USB Audio Mixer)", "index": 3, "channel": 1}
    monkeypatch.setattr(main_module, "describe_devices", lambda: DEVICES)
    body = json.loads((await admin_devices(token=TOKEN)).body)
    assert body["remembered"] == "Line In (USB Audio Mixer)"
    assert body["missing"] is False


async def test_refresh_devices_re_reads_the_driver(monkeypatch):
    """The old Refresh button only re-fetched PortAudio's cached snapshot,
    so a mixer switched on after the server started stayed invisible however
    often it was pressed."""
    called: list[bool] = []

    def reload():
        called.append(True)
        return DEVICES

    monkeypatch.setattr(main_module, "reload_devices", reload)
    body = json.loads((await admin_devices_refresh(token=TOKEN)).body)
    assert called == [True]
    assert len(body["devices"]) == 2


async def test_refresh_devices_is_refused_while_a_session_runs(monkeypatch):
    """Re-reading restarts the audio stack, which mid-talk would take the
    audio away to fix a dropdown."""
    monkeypatch.setattr(type(session), "running", property(lambda self: True))
    with pytest.raises(HTTPException) as raised:
        await admin_devices_refresh(token=TOKEN)
    assert raised.value.status_code == 409
    assert "Stop the session first" in raised.value.detail


async def test_refresh_devices_rechecks_the_token():
    with pytest.raises(HTTPException) as raised:
        await admin_devices_refresh(token=None)
    assert raised.value.status_code == 401


def test_the_device_is_remembered_by_the_name_the_driver_resolved():
    """Not the name the request asked for.

    Those are different questions when the request says "default", which is
    how 2026-10-31 came to have three sessions whose logs name a mixer the
    operator did not believe was selected.
    """
    class Source:
        device = None                      # the request said "default"
        device_name = "Line In (USB Audio Mixer)"       # what PortAudio opened
        channel = 2
        device_rate = 16000

    _state_dir.saved = {}
    session._remember_mic(Source())
    assert _state_dir.saved["name"] == "Line In (USB Audio Mixer)"
    assert _state_dir.saved["channel"] == 2
    assert "at" in _state_dir.saved


def test_a_source_with_no_device_name_is_not_remembered():
    """A file or a replay is not "the thing that worked in the room"."""
    class Source:
        device_name = ""

    _state_dir.saved = {}
    session._remember_mic(Source())
    assert _state_dir.saved == {}


# ---------------------------------------------------------------------------
# The quiet-input warning
# ---------------------------------------------------------------------------


class FakeLevel:
    def __init__(self, peak_db: float, db: float) -> None:
        self._peak, self._db = peak_db, db

    def as_dict(self) -> dict:
        return {"db": self._db, "peak_db": self._peak, "rms": 0.0,
                "peak": 0.0, "clipping": False}


class FakeAudio:
    device_name = "Built-in Microphone Array"

    def __init__(self, peak_db: float) -> None:
        self.level = FakeLevel(peak_db, peak_db - 10)

    def describe(self) -> str:
        return self.device_name


@pytest.fixture
def quiet_session(monkeypatch):
    """A session that looks live, with a settable input level and clock."""
    monkeypatch.setattr(type(session), "running", property(lambda self: True))
    session._quiet_since = None
    session._quiet_logged = False
    session.quiet_note = None
    session._quiet_peak = -120.0
    yield session
    session._audio = None
    session._quiet_since = None
    session.quiet_note = None


def test_a_loud_enough_input_never_warns(quiet_session, monkeypatch):
    quiet_session._audio = FakeAudio(QUIET_PEAK_DB + 5)
    now = [1000.0]
    monkeypatch.setattr(main_module.time, "time", lambda: now[0])
    for _ in range(200):
        quiet_session._watch_quiet()
        now[0] += 1.0
    assert quiet_session.quiet_note is None


def test_a_quiet_input_warns_only_after_a_minute(quiet_session, monkeypatch):
    quiet_session._audio = FakeAudio(QUIET_PEAK_DB - 10)
    now = [1000.0]
    monkeypatch.setattr(main_module.time, "time", lambda: now[0])
    quiet_session._watch_quiet()
    assert quiet_session.quiet_note is None
    now[0] += 30.0
    quiet_session._watch_quiet()
    assert quiet_session.quiet_note is None, "30 s is not a minute"
    now[0] += 40.0
    quiet_session._watch_quiet()
    assert quiet_session.quiet_note is not None
    assert quiet_session.quiet_note["device"] == FakeAudio.device_name
    assert quiet_session.quiet_note["peak_db"] == pytest.approx(
        QUIET_PEAK_DB - 10)


def test_one_loud_moment_clears_the_warning(quiet_session, monkeypatch):
    """A pause between speakers is not a wrong microphone."""
    now = [1000.0]
    monkeypatch.setattr(main_module.time, "time", lambda: now[0])
    quiet_session._audio = FakeAudio(QUIET_PEAK_DB - 10)
    quiet_session._watch_quiet()
    now[0] += 90.0
    quiet_session._watch_quiet()
    assert quiet_session.quiet_note is not None
    quiet_session._audio = FakeAudio(QUIET_PEAK_DB + 1)
    now[0] += 1.0
    quiet_session._watch_quiet()
    assert quiet_session.quiet_note is None


def test_the_threshold_clears_the_one_session_we_know_worked():
    """The mixer session of 2026-10-31, measured off its own recording.

    Its loudest 100 ms chunk in 36 minutes was -35.2 dBFS, and the spec's
    original "-35 dB average for a minute" would have been on screen for all
    of it. The floor has to sit below that peak by a real margin or the
    warning means nothing.
    """
    assert QUIET_PEAK_DB < -35.2
    # ...and not so low that an input which is off would slip past. The
    # quietest minute of that recording peaked at -51.8 dB, so a floor below
    # about -52 would never fire at all.
    assert QUIET_PEAK_DB > -52.0


def test_the_warning_is_logged_once(quiet_session, monkeypatch):
    lines: list[tuple[str, dict]] = []
    monkeypatch.setattr(quiet_session, "log_event",
                        lambda kind, **fields: lines.append((kind, fields)))
    now = [1000.0]
    monkeypatch.setattr(main_module.time, "time", lambda: now[0])
    quiet_session._audio = FakeAudio(QUIET_PEAK_DB - 10)
    for _ in range(200):
        quiet_session._watch_quiet()
        now[0] += 1.0
    quiet = [line for line in lines if line[0] == session_log.AUDIO_QUIET]
    assert len(quiet) == 1
    assert quiet[0][1]["floor_db"] == QUIET_PEAK_DB


# ---------------------------------------------------------------------------
# The response log's own name
# ---------------------------------------------------------------------------


def test_the_response_log_is_named_after_its_own_session(monkeypatch,
                                                         tmp_path):
    """Not after the one before it.

    `start_live` read `self.session_id` to name the capture, and
    `session_id` is assigned in `_begin`, which ran afterwards - so every
    response log but the first of a server run was written under the
    previous session's stem. That is how the three logs of 2026-10-31 came
    to sit under the names of the three sessions that preceded them, and how
    "Download diagnostics" came to zip the wrong talk's responses.

    Read off `main.py` rather than by starting two sessions, for the same
    reason `test_transcript.py` reads it: the property is about the order of
    two statements, and the test should fail if they are swapped back.
    """
    source = main_module.__file__
    text = open(source, encoding="utf-8").read()
    body = text.split("async def start_live", 1)[1].split(
        "def _open_transcript", 1)[0]
    begin = body.index("self._begin(mode=source.kind")
    writer = body.index("self._capture = CaptureWriter(")
    assert begin < writer, (
        "the capture must be built after _begin, which is where session_id "
        "is assigned"
    )
    assert "LOG_DIR / f\"live-{self.session_id}.jsonl\"" in body
    # And nothing falls back to a fresh stamp, which is what silently made
    # the first session of a run look correct and hid the bug.
    assert "session_log.session_stamp()" not in body


# ---------------------------------------------------------------------------
# The signal, inside a real client, against a scripted socket
# ---------------------------------------------------------------------------


class TransliteratingServer(FakeServer):
    """A Soniox that has stopped hearing English.

    Answers every audio chunk with one Arabic-labelled original token, as
    the engine did for the last 11.5 minutes of session 1406: no
    English-labelled original, every translation sourced from `ar`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.index = 0

    async def on_audio(self, socket, chunk: bytes) -> None:
        self.acked_ms[socket] = self.acked_ms.get(socket, 0) + 100
        word = TRANSLIT[self.index % len(TRANSLIT)]
        self.index += 1
        socket.reply({
            "tokens": [{
                "text": " " + word, "is_final": True, "language": "ar",
                "translation_status": "original",
            }],
            "final_audio_proc_ms": self.acked_ms[socket],
        })


async def test_the_signal_reconnects_and_loses_no_audio():
    """The remedy, end to end: a rollover rather than Stop and Start.

    Checks the two things that matter. The engine really did open a second
    connection (`reconfigures` moved, not `reconnects` - this is a planned
    rollover, not a drop), and **every chunk of audio that was sent before
    the reconnect is still accounted for afterwards**, because the ring
    buffer covers the join. Stop and Start would have ended the talk.
    """
    server = TransliteratingServer()
    chunks = [tone_chunk()] * 400
    client = SonioxClient(audio_stream(chunks, hold=True), api_key="k",
                          connect=server)
    watch = LanguageWatch(act=True, cooldown_seconds=0.0)
    fired: list[float] = []

    async def pump():
        now = 0.0
        async for response in client:
            now += 0.1
            for signal in watch.note_response(response, now):
                fired.append(signal.share)
                if client.reconfigure(client._config):
                    watch.reconnected(now)
            # **Keep iterating after asking.** `reconfigure` only raises a
            # flag; the rollover itself happens inside the client's own loop,
            # which only advances while something is consuming it. Returning
            # here left the request pending for ever, which is how this test
            # first "proved" that nothing reconnects.
            if fired and server.connections >= 2:
                return

    # `hold=True` is a live microphone: it never ends on its own, so `pump`
    # either returns once the second connection is up or is bounded here.
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(pump(), timeout=5.0)
    sent_before = len(server.sockets[0].sent_audio)
    await client.close()

    assert fired, "a stream of transliterated English must fire"
    assert server.connections >= 2, "it must have opened a second connection"
    assert client.reconfigures >= 1
    assert client.reconnects == 0, "a rollover is not a drop"
    assert client.last_error is None
    assert sent_before > 0
    # Nothing was dropped on the way: the ring buffer is what makes this a
    # remedy the room cannot see.
    assert client.dropped_seconds == 0.0
    assert all(len(chunk) == CHUNK_BYTES
               for socket in server.sockets for chunk in socket.sent_audio)


async def test_a_healthy_stream_never_reconnects():
    """The default `FakeServer` answers in English. Nothing may happen."""
    server = FakeServer()
    client = SonioxClient(audio_stream([tone_chunk()] * 200), api_key="k",
                          connect=server)
    watch = LanguageWatch(act=True)
    fired = []
    async for response in client:
        fired.extend(watch.note_response(response, client.audio_seconds_sent))
    await client.close()
    assert fired == []
    assert server.connections == 1
    assert client.reconfigures == 0


# ---------------------------------------------------------------------------
# What reaches the logs and the page
# ---------------------------------------------------------------------------


def test_the_session_writes_lang_stuck_and_reconnects(monkeypatch):
    """`_watch_language`, with the engine faked out.

    The event has to be written whether or not the reconnect worked: the
    line is the evidence and the reconnect is the remedy, and on a night
    when the remedy fails the evidence is what is left.
    """
    lines: list[tuple[str, dict]] = []
    calls: list[dict] = []

    class Engine:
        state = STREAMING
        reconnects = 0
        rollovers = 0
        reconfigures = 0
        audio_seconds_sent = 600.0
        lag_seconds = 0.4

        def reconfigure(self, config):
            calls.append(config)
            return True

    monkeypatch.setattr(type(session), "running", property(lambda self: True))
    monkeypatch.setattr(session, "log_event",
                        lambda kind, **fields: lines.append((kind, fields)))
    monkeypatch.setattr(session, "_elapsed", lambda: 1500.0)
    session._engine = Engine()
    session._audio = None
    session._lang = LanguageWatch(act=True)
    session.lang_note = None
    try:
        for index in range(240):
            word = (TRANSLIT[index % len(TRANSLIT)] if index % 3
                    else ARABIC[index % len(ARABIC)])
            session._watch_language({"tokens": [{
                "text": " " + word, "is_final": True, "language": "ar",
                "translation_status": "original",
            }]})
    finally:
        session._engine = None
        session._lang = None

    stuck = [line for line in lines if line[0] == session_log.LANG_STUCK]
    assert stuck, "the session must write the event"
    assert calls, "and reconnect through the rollover path"
    payload = stuck[0][1]
    assert payload["share"] >= 0.12
    assert payload["acting"] is True
    assert payload["diagnostic"]["engine_state"] == STREAMING
    assert session.lang_note["reconnected"] is True
    # The event log holds neither speech nor session details.
    blob = json.dumps(payload, ensure_ascii=False)
    for word in TRANSLIT + ARABIC:
        assert word not in blob


def test_a_replay_logs_the_signal_and_reconnects_nothing(monkeypatch):
    """`act=False` in replay. A replay has no connection to re-open, and the
    reason to replay a locked session is to watch the detector fire."""
    lines: list[tuple[str, dict]] = []
    monkeypatch.setattr(type(session), "running", property(lambda self: True))
    monkeypatch.setattr(session, "log_event",
                        lambda kind, **fields: lines.append((kind, fields)))
    monkeypatch.setattr(session, "_elapsed", lambda: 900.0)

    class Engine:
        state = STREAMING
        reconnects = rollovers = reconfigures = 0
        audio_seconds_sent = 0.0
        lag_seconds = 0.0

        def reconfigure(self, config):
            raise AssertionError("a replay must not reconnect anything")

    session._engine = Engine()
    session._lang = LanguageWatch(act=False)
    session.lang_note = None
    try:
        for index in range(240):
            session._watch_language({"tokens": [{
                "text": " " + TRANSLIT[index % len(TRANSLIT)],
                "is_final": True, "language": "ar",
                "translation_status": "original",
            }]})
    finally:
        session._engine = None
        session._lang = None
    stuck = [line for line in lines if line[0] == session_log.LANG_STUCK]
    assert stuck
    assert stuck[0][1]["acting"] is False
    assert session.lang_note is None


def test_a_broken_watcher_never_takes_the_talk_down(monkeypatch):
    """A detector that ends a session is worse than the fault it watches."""
    lines: list[tuple[str, dict]] = []
    monkeypatch.setattr(session, "log_event",
                        lambda kind, **fields: lines.append((kind, fields)))

    class Exploding:
        act = True

        def note_response(self, response, now):
            raise RuntimeError("scripted failure")

    session._lang = Exploding()
    try:
        session._watch_language({"tokens": []})
    finally:
        session._lang = None
    assert [line for line in lines if line[0] == "LANG_WATCH_FAILED"]


def test_the_session_starts_a_fresh_watcher(monkeypatch):
    """Every threshold in it is a rolling window, and a new talk must not
    inherit the last one's."""
    monkeypatch.setattr(session, "_open_logs", lambda **kwargs: None)
    monkeypatch.setattr(session.hub, "notify", lambda: None)
    session._begin(mode="replay", engine="replay", source="x.jsonl")
    first = session._lang
    assert first is not None and first.act is False
    session._begin(mode="mic", engine="soniox", source="mic")
    assert session._lang is not first
    assert session._lang.act is True, "a live run acts, a replay logs"
    session._lang = None


def test_the_hourly_cap_is_reported_even_though_it_stops_the_signals(
        monkeypatch):
    """The gap that reading the two gates against each other found.

    Once the cap is reached the watcher stops producing signals at all - that
    is what the cap is - so a check inside `_watch_language`'s signal loop is
    unreachable exactly when it matters, and `LANG_CAPPED` was never written.
    """
    lines: list[tuple[str, dict]] = []

    class Engine:
        state = STREAMING
        reconnects = rollovers = reconfigures = 0
        audio_seconds_sent = 600.0
        lag_seconds = 0.1

        def reconfigure(self, config):
            return True

    monkeypatch.setattr(type(session), "running", property(lambda self: True))
    monkeypatch.setattr(session, "log_event",
                        lambda kind, **fields: lines.append((kind, fields)))
    clock = [0.0]
    monkeypatch.setattr(session, "_elapsed", lambda: clock[0])
    session._engine = Engine()
    session._audio = None
    session._lang = LanguageWatch(act=True, max_per_hour=2,
                                  cooldown_seconds=10.0)
    session.lang_note = None
    session._lang_capped = False
    try:
        # Three bursts of transliterated English, two minutes apart, against
        # a cap of two reconnects an hour.
        for burst in range(4):
            clock[0] = burst * 120.0
            for index in range(240):
                clock[0] += 0.05
                session._watch_language({"tokens": [{
                    "text": " " + TRANSLIT[index % len(TRANSLIT)],
                    "is_final": True, "language": "ar",
                    "translation_status": "original",
                }]})
    finally:
        session._engine = None
        session._lang = None

    stuck = [line for line in lines if line[0] == session_log.LANG_STUCK]
    capped = [line for line in lines if line[0] == session_log.LANG_CAPPED]
    assert len(stuck) == 2, "the cap must hold the third reconnect back"
    assert len(capped) == 1, "and the cap itself must be written down once"
    assert capped[0][1]["reconnects"] == 2
    # ...and the operator's line says so, in the state that renders red.
    assert session.lang_note["capped"] is True
    assert session.lang_note["reconnected"] is False
    session.lang_note = None
    session._lang_capped = False


# ---------------------------------------------------------------------------
# "Save audio" remembers its last setting, and never defaults to on
# ---------------------------------------------------------------------------


def test_a_fresh_install_does_not_save_audio():
    """No file, no tick. The only acceptable default.

    The room has not consented to being recorded, so this stays off until
    somebody chooses it. Remembering the choice is about the *other* failure:
    a box ticked last week and silently cleared by every page load since.
    """
    _state_dir.saved = {}
    assert device_payload(DEVICES)["remembered_record"] is False


def test_the_tick_is_remembered_and_sent_back():
    _state_dir.saved = {"record": True}
    assert device_payload(DEVICES)["remembered_record"] is True


def test_an_untick_is_remembered_too():
    """Turning it off has to stick as hard as turning it on."""
    _state_dir.saved = {"record": True}
    _state_dir.saved.update({"record": False})
    assert device_payload(DEVICES)["remembered_record"] is False


def test_a_file_from_an_older_build_has_no_tick_in_it():
    """`mic.yaml` written before this existed carries only the device."""
    _state_dir.saved = {"name": "Line In (USB Audio Mixer)", "index": 3, "channel": 1}
    payload = device_payload(DEVICES)
    assert payload["remembered_record"] is False
    assert payload["remembered"] == "Line In (USB Audio Mixer)"


@pytest.mark.parametrize("source", ["mic", "file", "live"])
async def test_starting_remembers_the_tick(monkeypatch, source):
    """From the route, which is where the operator's literal choice is.

    Not from the audio source: `recording_path` says what the recorder did,
    and for a replay or a failed start there is no recorder to ask.
    """
    started: list[dict] = []

    async def fake_start(*args, **kwargs):
        started.append(kwargs)

    monkeypatch.setattr(session, "start_audio", fake_start)
    monkeypatch.setattr(session, "start_live", fake_start)
    monkeypatch.setattr(main_module, "MicSource", lambda *a, **k: object())
    monkeypatch.setattr(main_module, "FileSource", lambda *a, **k: object())
    monkeypatch.setattr(main_module, "resolve_audio", lambda name: Path(name))

    _state_dir.saved = {}
    await main_module.admin_start(token=TOKEN, source=source, file="x.wav",
                                 record=True)
    assert _state_dir.saved["record"] is True
    await main_module.admin_start(token=TOKEN, source=source, file="x.wav",
                                 record=False)
    assert _state_dir.saved["record"] is False


@needs_captures
async def test_a_replay_does_not_touch_the_tick(monkeypatch, tmp_path):
    """A replay cannot record anything, so it has no opinion to store."""
    async def fake_replay(*args, **kwargs):
        return None

    monkeypatch.setattr(session, "start_replay", fake_replay)
    monkeypatch.setattr(main_module, "resolve_capture",
                        lambda name: tmp_path / name)
    _state_dir.saved = {"record": True}
    await main_module.admin_start(token=TOKEN, source="replay",
                                 capture="run1.jsonl", record=False)
    assert _state_dir.saved["record"] is True, "a replay must not clear it"


async def test_a_start_that_fails_does_not_rewrite_the_tick(monkeypatch):
    """A device that will not open should not change the preference on its
    way to a 400."""
    def explode(*args, **kwargs):
        raise OSError("device busy")

    monkeypatch.setattr(main_module, "MicSource", explode)
    _state_dir.saved = {"record": True}
    with pytest.raises(HTTPException):
        await main_module.admin_start(token=TOKEN, source="mic", record=False)
    assert _state_dir.saved["record"] is True


def test_the_status_says_whether_audio_is_being_saved(monkeypatch):
    """What the *session* is doing, not what the checkbox says.

    Those differ for every source but live, and after a page reload they
    differ until Start is pressed - which is the whole reason this is read
    off the session.
    """
    class Recording:
        device_name = "Line In (USB Audio Mixer)"
        level = FakeLevel(-20.0, -30.0)
        audio_ms = 1000
        dropped_chunks = 0
        recording_path = "recordings/2026-11-03-120000.wav"

        def describe(self):
            return self.device_name

        def status(self):
            return {"source_kind": "mic", "source_detail": self.describe(),
                    "level": self.level.as_dict(), "audio_ms": self.audio_ms,
                    "dropped_chunks": self.dropped_chunks,
                    "recording": self.recording_path}

    monkeypatch.setattr(type(session), "running", property(lambda self: True))
    session._audio = Recording()
    try:
        assert session.status()["recording"] == Recording.recording_path
        Recording.recording_path = None
        assert session.status()["recording"] is None
    finally:
        Recording.recording_path = "recordings/2026-11-03-120000.wav"
        session._audio = None


def test_the_page_reads_the_indicator_from_the_session(monkeypatch):
    """And reads the fields it needs to tell the three states apart."""
    page = (ROOT / "web" / "admin.js").read_text(encoding="utf-8")
    assert "renderRecording" in page
    # The three states: not running, running and saving, running and not.
    assert "status.recording" in page
    assert "status.running" in page
    assert "remembered_record" in page


# ---------------------------------------------------------------------------
# One session, one name, for all four of its files
# ---------------------------------------------------------------------------


def test_the_recording_is_named_after_the_session():
    """`name_recording` renames the default and nothing else.

    The recorder is opened by `source.open()`, which runs *before* `_begin`
    assigns the session id, so the WAV used to take the clock a few
    microseconds earlier than the other three files. Normally identical,
    occasionally a second apart - and "one session, one stem" is only worth
    having if the operator can check it against a directory listing.
    """
    from app.audio_source import MicSource
    from app.settings import RECORDING_DIR

    wanted = MicSource(3, record=True)
    wanted.name_recording("2026-11-03-120000")
    assert wanted._record_request == RECORDING_DIR / "2026-11-03-120000.wav"

    # An explicit path was asked for by name and is left alone.
    chosen = MicSource(3, record=Path("recordings") / "keep-this.wav")
    chosen.name_recording("2026-11-03-120000")
    assert chosen._record_request == Path("recordings") / "keep-this.wav"

    # And off stays off: this must never turn recording on.
    off = MicSource(3, record=False)
    off.name_recording("2026-11-03-120000")
    assert off._record_request is False


def test_start_live_names_the_recording_before_it_opens_the_source():
    """Read off `main.py`, because the property is the order of two calls.

    `name_recording` after `open()` would be too late - the recorder is
    already writing - and the session id is only known after `_begin`, so the
    stamp has to be allocated before either.
    """
    body = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    body = body.split("async def start_live", 1)[1].split(
        "def _open_transcript", 1)[0]
    stamp = body.index("stamp = session_log.free_stamp()")
    named = body.index("source.name_recording(stamp)")
    opened = body.index("await source.open()")
    begun = body.index("self._begin(mode=source.kind")
    assert stamp < named < opened < begun
    # ...and the session is told to use that same stamp, or the two names
    # diverge again with nothing to show it.
    assert "session_id=stamp" in body
