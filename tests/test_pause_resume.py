"""The pause as the operator meets it: a sentence, a button, two log lines.

2026-11-07, session 2026-11-07_1405. The idle auto-stop closed the stream at
632.5 s with the venue's wireless microphone running out of battery, `/admin`
showed the word PAUSED and nothing else, and the only way back was Stop and
Start - which ends the talk, closes the stream, finalizes the transcript and
clears every page's paragraphs to fix an engine that was waiting for a noise.

Three things are tested here, because three things were missing:

1. the status payload says what the pause is waiting for, from which device,
   and at what level,
2. `POST /admin/resume` releases it without starting a new session, and
   refuses with 409 when nothing is paused,
3. the event log carries `PAUSED` and `RESUMED` with the levels on them.

`tests/test_soniox_client.py` holds the client's half (the threshold, the
speech hold-off, the resume path) and `tests/test_pause_live.py` the
calibration against the recordings. Nothing here reaches Soniox: the engine
is a stand-in with the attributes the session reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import session_log  # noqa: E402
from app.soniox_client import PAUSED, STREAMING, rms_db  # noqa: E402
from captures import needs_captures  # noqa: E402

#: Obviously fake, like every other token in the suite.
TOKEN = "pause-test-token"


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "admin_token", TOKEN)
    return TOKEN


class FakeEngine:
    """Just the attributes `CaptionSession` reads off a paused client."""

    def __init__(self) -> None:
        self.state = STREAMING
        self.silent_seconds = 601.4
        self.idle_stop_seconds = 600.0
        self.silence_rms = 0.0005
        self.pauses = 0
        self.resumes = 0
        #: The RMS of the last chunk, which is what the pause is judged on.
        #: -86.0 dBFS: the real reading on this mixer line between words,
        #: and far below the -60 the admin meter clamps to.
        self.last_rms = 10 ** (-86.0 / 20)
        self.resume_reason = None
        self.reconnects = 0
        self.rollovers = 0
        self.reconfigures = 0
        self.keepalives = 0
        self.audio_seconds_sent = 42.0
        #: What the status payload reads off the client for the audio rows.
        self.sent_ms = 42000
        self.audio_seconds = 42.0
        self.lag_seconds = 0.3
        self.last_error = None
        self.resumed = False

    def resume(self) -> bool:
        if self.state != PAUSED:
            return False
        self.resumed = True
        self.resumes += 1
        self.resume_reason = "operator"
        self.state = STREAMING
        return True

    def status(self) -> dict:
        return {"engine_state": self.state}

    @property
    def model(self) -> str:
        return "stt-rt-v6"


class _UnfinishedTask:
    """What `CaptionSession.running` reads. A session is running when its
    feed task exists and has not finished, so that is what is faked here
    rather than the property over it."""

    @staticmethod
    def done() -> bool:
        return False


class FakeAudio:
    """An input that reads quiet, which is the whole situation."""

    device_name = "Line In (USB Audio Mixer)"
    #: `_open_logs` reports whether the audio was being saved, so the stand-in
    #: has to answer that question too. Not saved, which is the default and
    #: what both of the 2026-11-07 mixer sessions did.
    recording_path = None

    class _Level:
        @staticmethod
        def as_dict() -> dict:
            return {"db": -60.0, "peak_db": -50.4, "rms": 0.001,
                    "peak": 0.003, "clipping": False}

    level = _Level()

    def describe(self) -> str:
        return "Line In (USB Audio Mixer) @ 16000 Hz, ch 1/2"

    def status(self) -> dict:
        return {"level": self.level.as_dict()}


@pytest.fixture
def paused(tmp_path, monkeypatch):
    """A session with a paused engine and its logs in a temporary directory.

    The session object is the module-level one the routes use, so the state
    is put back afterwards - a leftover `_engine` would make every later
    test in the run think a talk was in progress.
    """
    import app.main as server

    monkeypatch.setattr(server.session_log, "LOG_DIR", tmp_path,
                        raising=False)
    monkeypatch.setattr(server, "LOG_DIR", tmp_path, raising=False)
    session = server.session
    engine = FakeEngine()
    audio = FakeAudio()
    monkeypatch.setattr(session, "_engine", engine, raising=False)
    monkeypatch.setattr(session, "_client", engine, raising=False)
    monkeypatch.setattr(session, "_audio", audio, raising=False)
    # `running` is a property over `_task`, so the session is made to look
    # live the way it actually is: a feed task that has not finished.
    monkeypatch.setattr(session, "_task", _UnfinishedTask(), raising=False)
    monkeypatch.setattr(session, "mode", "mic", raising=False)
    monkeypatch.setattr(session, "source", audio.describe(), raising=False)
    session.pause_note = None
    session._paused_at = None
    session._last_speech_at = None
    session._open_logs(mode="live", source=audio.describe(),
                       session_id="2026-11-08-120000")
    try:
        yield session, engine
    finally:
        session._close_logs("finished")
        session.pause_note = None
        session._paused_at = None
        session._last_speech_at = None


def events(tmp_path) -> list[dict]:
    path = session_log.event_path("2026-11-08-120000", tmp_path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def pause(session, engine) -> None:
    engine.state = PAUSED
    engine.pauses = 1
    session._on_engine_state(PAUSED)


# ---------------------------------------------------------------------------
# What the page is told
# ---------------------------------------------------------------------------


def test_the_pause_says_which_device_and_for_how_long(paused, tmp_path):
    """"Paused: no sound from <device> for N s." needs all three parts."""
    session, engine = paused
    pause(session, engine)

    note = session.status()["pause"]
    assert note is not None, "the page has nothing to say about the pause"
    assert note["device"] == "Line In (USB Audio Mixer)"
    assert note["silent_seconds"] == pytest.approx(601.4, abs=0.1)
    assert note["idle_stop_seconds"] == 600.0
    # **The level it is judging, off the engine rather than off the meter.**
    # They are the same RMS and the meter's copy is clamped at -60 dBFS for
    # the bar, so reading it put "Input -60.0 dB, needs -66.0 dB" on the
    # page: a level that looks above the threshold beside a pause waiting
    # for it. The number has to be able to go below the threshold.
    assert note["input_db"] == pytest.approx(-86.0, abs=0.1)
    assert note["input_db"] < note["silence_db"], (
        "the pause shows an input level at or above the level that would "
        "release it, which reads as a page contradicting itself")
    assert note["silence_db"] == round(rms_db(engine.silence_rms), 1)
    # The meter's peak keeps the meter's floor and its own name, so nothing
    # compares it against the threshold above.
    assert note["meter_peak_db"] == -50.4


def test_the_pause_note_goes_away_when_the_stream_comes_back(paused):
    """A banner that outlives the pause is worse than no banner."""
    session, engine = paused
    pause(session, engine)
    assert session.status()["pause"] is not None

    engine.state = STREAMING
    session._on_engine_state(STREAMING)
    assert session.status()["pause"] is None


def test_the_status_payload_has_no_speech_in_it(paused):
    """Same rule as everywhere else: counts, levels and device names."""
    session, engine = paused
    pause(session, engine)
    blob = json.dumps(session.status(), default=str)
    for word in ("Quintilian", "Harbottle", "زنجبيل", "hello everyone"):
        assert word not in blob


# ---------------------------------------------------------------------------
# What the log carries
# ---------------------------------------------------------------------------


def test_the_pause_and_the_resume_are_both_written_with_their_levels(
        paused, tmp_path):
    """The two lines 2026-11-07 did not have.

    It had one, `IDLE_STOP`, carrying the reconnect counters and not a single
    number about the audio - so the question "what was the input reading
    when it paused?" had no answer anywhere.
    """
    session, engine = paused
    pause(session, engine)
    engine.state = STREAMING
    engine.resumes = 1
    engine.resume_reason = "sound"
    session._on_engine_state(STREAMING)

    rows = events(tmp_path)
    kinds = [row["event"] for row in rows]
    assert session_log.PAUSED in kinds
    assert session_log.RESUMED in kinds

    paused_row = next(r for r in rows if r["event"] == session_log.PAUSED)
    assert paused_row["device"] == "Line In (USB Audio Mixer)"
    assert paused_row["input_db"] == pytest.approx(-86.0, abs=0.1)
    assert paused_row["silence_db"] == round(rms_db(engine.silence_rms), 1)
    assert paused_row["silent_seconds"] == pytest.approx(601.4, abs=0.1)

    resumed = next(r for r in rows if r["event"] == session_log.RESUMED)
    assert resumed["reason"] == "sound"
    assert resumed["input_db"] == pytest.approx(-86.0, abs=0.1)
    assert isinstance(resumed["paused_seconds"], (int, float))


def test_an_ordinary_state_change_is_still_an_engine_state_line(paused,
                                                                tmp_path):
    """Only the pause and the way out of it get their own names."""
    session, engine = paused
    session._on_engine_state("reconnecting")
    kinds = [row["event"] for row in events(tmp_path)
             if row["event"] != session_log.SESSION_START]
    assert kinds == [session_log.ENGINE_STATE]


def test_stopping_out_of_a_pause_is_not_logged_as_a_resume(paused, tmp_path):
    """Stop while paused ends the talk; it does not bring the stream back."""
    session, engine = paused
    pause(session, engine)
    session._on_engine_state("stopped")
    kinds = [row["event"] for row in events(tmp_path)]
    assert session_log.RESUMED not in kinds
    # ...and the banner does not survive the talk it was describing.
    assert session.pause_note is None


# ---------------------------------------------------------------------------
# The button
# ---------------------------------------------------------------------------


async def test_resume_reconnects_without_starting_a_new_session(paused,
                                                                tmp_path):
    """The point of the button. A Start would have made a new everything."""
    import app.main as server

    session, engine = paused
    pause(session, engine)
    before = session.session_id
    transcript = session.transcript_dir

    payload = await server.admin_resume(token=TOKEN)
    assert payload.status_code == 200
    assert engine.resumed is True
    # Same session, same name, same transcript folder, still running.
    assert session.session_id == before
    assert session.transcript_dir == transcript
    assert session.running is True


async def test_resume_refuses_when_nothing_is_paused(paused):
    """409 with a reason, because a button that always reports success
    teaches an operator to press Stop when it does nothing."""
    from fastapi import HTTPException

    import app.main as server

    session, engine = paused
    with pytest.raises(HTTPException) as caught:
        await server.admin_resume(token=TOKEN)
    assert caught.value.status_code == 409
    assert "paused" in str(caught.value.detail).lower()


async def test_resume_needs_the_admin_token(paused):
    from fastapi import HTTPException

    import app.main as server

    with pytest.raises(HTTPException) as caught:
        await server.admin_resume(token="not-the-token")
    assert caught.value.status_code == 401


@needs_captures
def test_a_replay_has_nothing_to_resume(tmp_path):
    """Same shape as the transcript rule: the mode decides, not a button."""
    from app.replay import FakeSonioxClient

    fixture = ROOT / "tests" / "fixtures" / "run1.jsonl"
    client = FakeSonioxClient(fixture)
    assert client.resume() is False
    client.note_speech()      # must not raise


# ---------------------------------------------------------------------------
# The quiet warning
# ---------------------------------------------------------------------------


def test_the_quiet_warning_says_so_differently_while_words_are_arriving(
        paused, monkeypatch):
    """Question 4. -50 dBFS fires on a working mixer line, so the note has
    to carry whether the engine is transcribing it.

    On 2026-11-07 the page asked whether the device was right for 667
    seconds while 5,655 final tokens came through it.
    """
    import time

    import app.main as server

    session, engine = paused
    # Quiet for longer than the window, with speech a moment ago.
    session._quiet_since = time.time() - server.QUIET_SECONDS - 5
    session._quiet_peak = -50.4
    session._last_speech_at = time.time()
    session._watch_quiet()
    assert session.quiet_note is not None
    assert session.quiet_note["speech"] is True

    # ...and the same levels with nothing being transcribed is the warning
    # this check was built for.
    session._quiet_since = time.time() - server.QUIET_SECONDS - 5
    session._quiet_peak = -50.4
    session._last_speech_at = time.time() - server.SPEECH_RECENT_SECONDS - 5
    session._watch_quiet()
    assert session.quiet_note["speech"] is False


def test_a_final_token_is_what_counts_as_speech():
    """`<end>` is a segment boundary and never displayed, so it is not a
    word arriving - it is what arrives when they stop."""
    from app.main import _has_final_token

    assert _has_final_token({"tokens": [{"text": "a", "is_final": True}]})
    assert not _has_final_token({"tokens": [{"text": "a", "is_final": False}]})
    assert not _has_final_token({"tokens": [{"text": "<end>",
                                             "is_final": True}]})
    assert not _has_final_token({})
