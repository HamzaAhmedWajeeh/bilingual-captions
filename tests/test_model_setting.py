"""Which speech model a run asks for, and who gets to decide.

`stt-rt-v6` landed on 2026-11-03 and `stt-rt-v5` is removed on 2026-12-15, so
for six weeks this project has two engine builds it can stream to and one
reason to be able to go back: a bad evening. The model therefore stopped
being a string in `base_config` and became the one value in the config frame
that `.env` can move.

These tests cover the three things that makes true, and one thing it must not
break:

- the default is a decision in code, not an accident of a missing `.env` line
- an unknown name stops the server coming up, rather than arriving as a fatal
  `model_not_available` when the room is already seated
- every run says which model it used, in the two files that outlive it: its
  event log and its response log
- and the operator page can say so too, without the word Soniox appearing in
  anything served (the security rules)

Nothing here reaches the network. The client takes its `connect` as an
argument, which is the same discipline `tests/test_soniox_client.py` relies
on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import main as server  # noqa: E402
from app import session_log, usage  # noqa: E402
from app.audio_source import SAMPLE_RATE, FileSource  # noqa: E402
from app.main import (  # noqa: E402
    CaptionSession,
    NO_MODEL_MESSAGE,
    admin_status,
    require_known_model,
)
from app.settings import settings  # noqa: E402
from app.soniox_client import (  # noqa: E402
    DEFAULT_MODEL,
    MODELS,
    SonioxClient,
    active_model,
    base_config,
)
from test_soniox_client import FakeServer, audio_stream, collect, tone_chunk  # noqa: E402
from captures import needs_captures  # noqa: E402

TOKEN = "model-setting-test-token"
WEB = ROOT / "web"


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    return TOKEN


@pytest.fixture(autouse=True)
def quarantine_usage_log(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "LOG_DIR", tmp_path / "logs")
    return tmp_path / "logs"


@pytest.fixture(autouse=True)
async def fresh_session(monkeypatch):
    made = CaptionSession()
    monkeypatch.setattr(server, "session", made)
    yield made
    await made.shutdown()


@pytest.fixture
def tone(tmp_path, monkeypatch):
    """Half a second of tone, and a `logs/` nobody real is reading."""
    import wave

    from app import audio_source

    directory = tmp_path / "recordings"
    directory.mkdir()
    samples = np.rint(
        np.sin(2 * np.pi * 440 * np.arange(SAMPLE_RATE // 2) / SAMPLE_RATE)
        * 0.5 * 32767
    ).astype(np.int16)
    with wave.open(str(directory / "tone.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.tobytes())
    monkeypatch.setattr(server, "RECORDING_DIR", directory)
    monkeypatch.setattr(audio_source, "RECORDING_DIR", directory)
    monkeypatch.setattr(server, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(session_log, "LOG_DIR", tmp_path / "logs")
    return directory / "tone.wav"


async def finish(session, limit: int = 2000) -> None:
    import asyncio

    for _ in range(limit):
        if not session.running:
            return
        await asyncio.sleep(0.005)
    await session.stop()


def events(directory: Path) -> list[dict]:
    found = sorted(Path(directory).glob("events-*.jsonl"))
    assert found, f"no event log in {directory}"
    return [json.loads(line) for line
            in found[0].read_text(encoding="utf-8").splitlines() if line.strip()]


def start_line(directory: Path) -> dict:
    for line in events(directory):
        if line.get("event") == session_log.SESSION_START:
            return line
    raise AssertionError("no SESSION_START line")


# ---------------------------------------------------------------------------
# The setting itself
# ---------------------------------------------------------------------------


def test_the_known_models_are_the_two_that_exist():
    """Both, and in this order, until `stt-rt-v5` is removed on 2026-12-15.

    Pinned rather than inferred so that dropping v5 is a decision somebody
    makes on purpose, on or after that date, rather than a tidy-up.
    """
    assert MODELS == ("stt-rt-v6", "stt-rt-v5")
    assert DEFAULT_MODEL in MODELS


def test_the_default_is_what_a_missing_env_line_selects(monkeypatch):
    """An operator with no `SONIOX_MODEL` gets the measured model.

    Blank and whitespace too: a `.env` line someone emptied rather than
    deleted must not mean "no model", which the engine answers with a fatal
    `invalid_request` at Start.
    """
    for value in ("", "   ", None):
        monkeypatch.setattr(settings, "soniox_model", value or "")
        assert active_model() == DEFAULT_MODEL
        assert base_config({})["model"] == DEFAULT_MODEL


@pytest.mark.parametrize("model", MODELS)
def test_the_env_var_selects_either_model(monkeypatch, model):
    monkeypatch.setattr(settings, "soniox_model", model)
    assert active_model() == model
    assert base_config({})["model"] == model
    # And surrounding whitespace is forgiven, because `.env` is typed by hand.
    monkeypatch.setattr(settings, "soniox_model", f"  {model} ")
    assert active_model() == model


def test_an_explicit_model_beats_the_environment(monkeypatch):
    """`tools/replay_recording.py --model` has to win, or a comparison run
    would silently measure whatever `.env` happens to say."""
    monkeypatch.setattr(settings, "soniox_model", MODELS[1])
    assert base_config({}, model=MODELS[0])["model"] == MODELS[0]


def test_an_unknown_model_is_refused_rather_than_passed_through(monkeypatch):
    monkeypatch.setattr(settings, "soniox_model", "stt-rt-v9")
    with pytest.raises(ValueError) as raised:
        active_model()
    # The message has to name what *is* known, because the person reading it
    # is at a venue with a typo and no docs.
    for model in MODELS:
        assert model in str(raised.value)


def test_the_server_refuses_to_start_on_an_unknown_model(monkeypatch):
    """Same shape as the missing admin token: fail in the terminal the
    operator is already looking at, not when the talk starts."""
    monkeypatch.setattr(settings, "soniox_model", "whisper-large")
    with pytest.raises(RuntimeError) as raised:
        require_known_model()
    message = str(raised.value)
    assert "whisper-large" in message
    for model in MODELS:
        assert model in message
    assert ".env" in message


@pytest.mark.parametrize("model", MODELS)
def test_a_known_model_lets_the_server_start(monkeypatch, model):
    monkeypatch.setattr(settings, "soniox_model", model)
    require_known_model()


def test_the_check_is_actually_wired_into_startup():
    """Read off `main.py`, because the property is that `lifespan` calls it.

    A check nothing calls is the same as no check, and this one passes its
    own test either way - so the wiring is what has to be asserted.
    """
    body = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    body = body.split("async def lifespan", 1)[1].split("\napp = ", 1)[0]
    assert "require_known_model()" in body
    assert "ensure_admin_token()" in body


def test_the_refusal_message_has_a_place_for_both_halves():
    """It is a template, and a missing field would render as a brace."""
    rendered = NO_MODEL_MESSAGE.format(model="x", known="a, b", env="/tmp/.env")
    assert "{" not in rendered and "}" not in rendered


# ---------------------------------------------------------------------------
# What goes on the wire, and what the client says about it
# ---------------------------------------------------------------------------


async def test_the_config_frame_carries_the_chosen_model(monkeypatch):
    monkeypatch.setattr(settings, "soniox_model", MODELS[0])
    fake = FakeServer()
    await collect(SonioxClient(audio_stream([tone_chunk()]), api_key="k",
                               connect=fake))
    assert json.loads(fake.sockets[0].sent_text[0])["model"] == MODELS[0]


async def test_the_client_reports_the_model_it_sent_not_the_environment(
    monkeypatch,
):
    """A `.env` edited mid-talk must not make the page misreport a running
    stream. The config frame is the thing that was actually on the wire."""
    config = base_config({}, model=MODELS[0])
    monkeypatch.setattr(settings, "soniox_model", MODELS[1])
    client = SonioxClient(audio_stream([tone_chunk()]), api_key="k",
                          connect=FakeServer(), config=config)
    assert client.model == MODELS[0]
    assert client.status()["model"] == MODELS[0]


# ---------------------------------------------------------------------------
# What `/admin` is told, and what it may not be told
# ---------------------------------------------------------------------------


async def test_the_status_payload_names_the_model_before_anything_runs(
    monkeypatch,
):
    monkeypatch.setattr(settings, "soniox_model", MODELS[0])
    body = json.loads((await admin_status(token=TOKEN)).body)
    assert body["model"] == MODELS[0]
    # Section 8 again: the model name is not the engine's name, and this
    # payload still must not carry one.
    assert "soniox" not in json.dumps(body).lower()


async def test_a_bad_env_edit_does_not_take_the_status_poll_down(monkeypatch):
    """The startup check has already refused this value, so the only way to
    get here is a file edited while the server runs. A poll that threw would
    take the whole operator page down over the typo it is reporting."""
    monkeypatch.setattr(settings, "soniox_model", "stt-rt-v0")
    body = json.loads((await admin_status(token=TOKEN)).body)
    assert "stt-rt-v0" in body["model"]
    assert "not a known model" in body["model"]


def test_the_operator_page_has_a_row_and_a_note_for_it():
    """Both, and for two different moments: the note answers "what will Start
    put on the wire" and the row answers "what is this run on"."""
    html = (WEB / "admin.html").read_text(encoding="utf-8")
    script = (WEB / "admin.js").read_text(encoding="utf-8")
    for ident in ("s-model", "model-note"):
        assert f'id="{ident}"' in html, f"{ident} is not on the page"
        assert ident in script, f"{ident} is on the page but never written"
    # The served files still name no engine (the security rules).
    assert "SONIOX" not in html.upper()
    assert "SONIOX" not in script.upper()


# ---------------------------------------------------------------------------
# What a run leaves behind
# ---------------------------------------------------------------------------


async def test_a_live_run_records_its_model_in_both_files_it_leaves(
    fresh_session, tone, tmp_path, monkeypatch
):
    """The event log and the response log, which are the two files that
    outlive the session. A transcript that reads oddly in a month is the
    moment somebody needs to know which engine build wrote it."""
    monkeypatch.setattr(settings, "soniox_api_key", "test-key")
    monkeypatch.setattr(settings, "soniox_model", MODELS[0])
    source = FileSource(tone, realtime=False)
    await fresh_session.start_live(source, connect=FakeServer(),
                                   api_key="test-key", capture=True,
                                   transcript=False)
    await finish(fresh_session)

    assert start_line(tmp_path / "logs")["model"] == MODELS[0]

    written = list((tmp_path / "logs").glob("live-*.jsonl"))
    assert len(written) == 1, f"no response log written: {written}"
    first = json.loads(written[0].read_text(encoding="utf-8").splitlines()[0])
    assert first["meta"]["config"]["model"] == MODELS[0]


@needs_captures
async def test_a_replay_records_no_model_because_it_asked_for_none(
    fresh_session, tmp_path, monkeypatch
):
    """Same shape as the transcript and the detector rules: the mode decides.
    A replay reaches no engine, so "which model" has no answer for it, and a
    line claiming one would be the sort of thing somebody later believes."""
    monkeypatch.setattr(server, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(session_log, "LOG_DIR", tmp_path / "logs")
    await fresh_session.start_replay(
        ROOT / "tests" / "fixtures" / "run5-english.jsonl", paced=False)
    await finish(fresh_session)
    assert start_line(tmp_path / "logs")["model"] is None
