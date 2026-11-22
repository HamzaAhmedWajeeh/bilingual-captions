"""What a session writes down about itself, and what it must never write.

A session now leaves three files behind: the response log (opt-in, verbatim),
an event log and a screen-health log. The last two exist because of
2026-10-24, when the captions could be replayed perfectly and *nothing else
about the evening had been recorded at all* - not what page was on the wall,
not what it had on screen, not whether a browser was rewriting it. A day of
work could only rule things out.

So these tests ask two questions of every line: **is what happened in here**,
and **is nothing anyone said in here**. The second is checked with values
that cannot occur by accident, the same way `tests/test_session_privacy.py`
checks the viewer socket.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import session_log  # noqa: E402
from app.broadcast import BroadcastHub, Viewer  # noqa: E402
from app.captions import CaptionState  # noqa: E402
from captures import needs_captures  # noqa: E402

#: The operator token these tests use. Obviously fake, like every other one
#: in the suite: the real value lives in `.env` and is never written down.
TOKEN = "session-log-test-token"


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "admin_token", TOKEN)
    return TOKEN


#: Words that could only have come from the captions or the session details,
#: so any of them in an event line is a leak.
NEVER = ("Quintilian", "Harbottle", "زنجبيل", "some speech",
         "Northwind", "hello everyone")


class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        pass


def _viewer(hub: BroadcastHub) -> Viewer:
    viewer = Viewer(_Socket(), ident=len(hub.viewers) + 1)
    hub.viewers.append(viewer)
    return viewer


async def _drain(session, limit: float = 10.0) -> None:
    """Let an unpaced replay finish.

    A task that is cancelled before it has taken its first step never runs
    its body, so a test that starts a replay and stops it in the same breath
    measures an empty session. This is the one await that lets the pipeline
    actually run.
    """
    import asyncio

    waited = 0.0
    while session.running and waited < limit:
        await asyncio.sleep(0.05)
        waited += 0.05


def read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The files themselves
# ---------------------------------------------------------------------------


def test_a_session_writes_an_event_log_and_a_health_log(tmp_path):
    logs = session_log.SessionLogs("2026-10-29-120000", 1000.0, tmp_path)
    logs.event(session_log.SESSION_START, mode="live", source="mic")
    logs.report(viewer=1, rendered="auto", seq=3)
    logs.event(session_log.SESSION_STOP, reason="finished", responses=12)
    logs.close()

    events = read(session_log.event_path("2026-10-29-120000", tmp_path))
    health = read(session_log.health_path("2026-10-29-120000", tmp_path))
    assert [row["event"] for row in events] == [
        session_log.SESSION_START, session_log.SESSION_STOP]
    assert len(health) == 1
    for row in events + health:
        # Every line is placeable against the response log and the
        # transcript, which is the whole point of writing it down.
        assert row["t"].endswith("Z")
        assert isinstance(row["at"], (int, float))


def test_the_three_files_share_one_name(tmp_path):
    """One session is one stem, so a reader does not have to correlate."""
    stem = "2026-10-29-120000"
    assert session_log.event_path(stem, tmp_path).name == f"events-{stem}.jsonl"
    assert session_log.health_path(stem, tmp_path).name == (
        f"screen-health-{stem}.jsonl")
    assert session_log.session_stamp(0).count("-") == 3


def test_a_log_that_cannot_be_opened_is_a_no_op(tmp_path):
    """A session must not end because a log did."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    logs = session_log.SessionLogs("2026-10-29-120000", 0.0, blocker)
    logs.event(session_log.SESSION_START)      # must not raise
    logs.report(viewer=1)
    logs.close()
    assert logs.events.written == 0


def test_a_long_field_cannot_turn_a_line_into_a_paragraph(tmp_path):
    logs = session_log.SessionLogs("2026-10-29-120000", 0.0, tmp_path)
    logs.event(session_log.PAGE_ERROR, message="x" * 5000)
    logs.close()
    row = read(session_log.event_path("2026-10-29-120000", tmp_path))[0]
    assert len(row["message"]) <= session_log.MAX_FIELD_CHARS


def test_the_listing_is_names_and_sizes(tmp_path):
    for stem in ("2026-10-28-200000", "2026-10-29-120000"):
        logs = session_log.SessionLogs(stem, 0.0, tmp_path)
        logs.event(session_log.SESSION_START)
        logs.close()
    (tmp_path / "live-2026-10-29-120000.jsonl").write_text("{}\n",
                                                           encoding="utf-8")
    rows = session_log.sessions(tmp_path)
    assert [row["session"] for row in rows] == ["2026-10-29-120000",
                                                "2026-10-28-200000"]
    assert "responses" in rows[0]["files"]
    assert "responses" not in rows[1]["files"]
    assert all(row["bytes"] > 0 for row in rows)
    assert session_log.total_bytes(tmp_path) > 0
    # Sizes, never contents: this goes out on a one-second poll.
    assert not any("text" in row for row in rows)


# ---------------------------------------------------------------------------
# A whole session, driven the way the server drives one
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path, monkeypatch):
    """The real `CaptionSession`, with its logs in a temporary directory."""
    import app.main as server

    monkeypatch.setattr(server.session_log, "LOG_DIR", tmp_path,
                        raising=False)
    monkeypatch.setattr(server, "LOG_DIR", tmp_path, raising=False)
    return server.session


@needs_captures
async def test_a_replayed_session_writes_every_log(session, tmp_path):
    """Start to stop, with a page connecting in the middle.

    Driven through the same methods the server uses rather than through the
    routes, so the assertions are about what gets written rather than about
    FastAPI.
    """
    capture = ROOT / "tests" / "fixtures" / "run1.jsonl"
    await session.start_replay(capture, paced=False)
    stem = session.session_id
    assert stem, "the session has no name, so its logs have none either"

    # While it is running: these are all synchronous, so they land before
    # the replay's first await gets a chance to finish the session.
    viewer = _viewer(session.hub)
    viewer.agent = "Mozilla/5.0 (Test)"
    session.hub.on_event("viewer_open", {"viewer": viewer.ident,
                                         "agent": viewer.agent})
    session.hub.note_health(viewer, {
        "rendered": "auto", "seq": 1, "page": "screen", "visible": True,
        "path": "/screen", "script": "en", "para": 4,
    })
    session.log_event(session_log.OPERATOR_MARK, note="looks wrong")
    await _drain(session)
    await session.stop()

    events = read(session_log.event_path(stem, tmp_path))
    health = read(session_log.health_path(stem, tmp_path))
    kinds = [row["event"] for row in events]
    assert kinds[0] == session_log.SESSION_START
    assert session_log.VIEWER_OPEN in kinds
    assert session_log.OPERATOR_MARK in kinds
    assert kinds[-1] == session_log.SESSION_STOP
    assert health and health[0]["rendered"] == "auto"
    assert health[0]["path"] == "/screen"
    assert health[0]["agent"] == "Mozilla/5.0 (Test)"

    # The session that just ran produced captions, so a leak would show.
    blob = json.dumps(events + health, ensure_ascii=False)
    for word in NEVER:
        assert word not in blob
    # ...and nothing that looks like a caption: the response log and the
    # transcript are the two files that hold speech, and neither of these
    # is one of them.
    assert "final" not in blob and "partial" not in blob
    rendered = session.state.render("ar")
    assert rendered, "the replay produced no Arabic, so this proved nothing"
    for line in rendered.splitlines():
        words = line.split()
        if len(words) >= 4:
            assert " ".join(words[:4]) not in blob, (
                "caption text reached an event or health line")


@needs_captures
async def test_a_session_stop_line_says_how_it_ended(session, tmp_path):
    capture = ROOT / "tests" / "fixtures" / "run1.jsonl"
    await session.start_replay(capture, paced=False)
    stem = session.session_id
    await _drain(session)
    await session.stop()
    events = read(session_log.event_path(stem, tmp_path))
    stop = [row for row in events if row["event"] == session_log.SESSION_STOP]
    assert len(stop) == 1, "a session ended more than once, or not at all"
    # "stopped" when the operator ended it, "finished" when the source ran
    # out, "error" when it fell over. An unpaced replay stopped in the same
    # breath can legitimately be any of the three, depending on whether the
    # task had taken a step yet.
    assert stop[0]["reason"] in ("finished", "error", "stopped")
    assert stop[0]["responses"] >= 0
    assert stop[0]["seconds"] >= 0


@needs_captures
async def test_the_start_line_carries_no_session_details(session, tmp_path):
    """Counts of the talk's details, never the details.

    `SessionContext.summary()` is the same shape the status poll carries, and
    it is here for the same reason: a reader needs to know whether speaker
    names were in play, not what they were.
    """
    capture = ROOT / "tests" / "fixtures" / "run1.jsonl"
    await session.start_replay(capture, paced=False)
    stem = session.session_id
    await _drain(session)
    await session.stop()
    start = read(session_log.event_path(stem, tmp_path))[0]
    assert start["event"] == session_log.SESSION_START
    assert isinstance(start["details"], dict)
    assert set(start["details"]) <= {"title", "speakers", "companies",
                                     "terms", "notes_chars"}
    for value in start["details"].values():
        assert isinstance(value, (bool, int)), (
            "the start line carries a session detail rather than a count")
    # The screen settings are logged, but never the invite link.
    assert start["screen"]["invite"] == ""


# ---------------------------------------------------------------------------
# The two routes an operator uses when something looks wrong
# ---------------------------------------------------------------------------


@needs_captures
async def test_mark_issue_writes_a_line_at_the_moment_it_is_pressed(
        session, tmp_path):
    """The timestamp is the point.

    On 2026-10-24 the best timestamp anyone had for the fault was "somewhere
    after about half an hour", and placing that against the logs took an
    afternoon. One button at the moment it is seen turns that into a `grep`.
    """
    capture = ROOT / "tests" / "fixtures" / "run1.jsonl"
    await session.start_replay(capture, paced=False)
    stem = session.session_id
    session.log_event(session_log.OPERATOR_MARK, note="auto looks English")
    await _drain(session)
    await session.stop()

    events = read(session_log.event_path(stem, tmp_path))
    marks = [row for row in events
             if row["event"] == session_log.OPERATOR_MARK]
    assert len(marks) == 1
    assert marks[0]["note"] == "auto looks English"
    assert marks[0]["at"] >= 0


@needs_captures
async def test_the_diagnostics_zip_holds_the_session_and_nothing_else(
        session, tmp_path, monkeypatch):
    """What gets handed over after a bad night, assembled rather than hunted.

    The response log and the transcript hold speech; the event log and the
    health log hold counts and flags. All four belong in the same envelope,
    because the question being answered is "what happened", and answering it
    needs both halves.
    """
    import zipfile

    import app.main as server

    capture = ROOT / "tests" / "fixtures" / "run1.jsonl"
    await session.start_replay(capture, paced=False)
    stem = session.session_id
    await _drain(session)
    await session.stop()

    # A second session, whose files must not be in the first one's zip.
    await session.start_replay(capture, paced=False)
    other = session.session_id
    await _drain(session)
    await session.stop()
    assert other != stem

    monkeypatch.setattr(server, "LOG_DIR", tmp_path, raising=False)
    response = await server.admin_diagnostics(token=TOKEN, name=stem)
    assert response.media_type == "application/zip"
    with zipfile.ZipFile(__import__("io").BytesIO(response.body)) as archive:
        names = sorted(archive.namelist())
    assert names == [f"captions-{stem}/events.jsonl",
                     f"captions-{stem}/screen-health.jsonl"], names
    assert all(other not in name for name in names)


async def test_a_session_name_is_checked_by_shape(session):
    """A traversal is refused for being the wrong shape, not sanitized."""
    import app.main as server
    from fastapi import HTTPException

    for bad in ("../../.env", "2026-10-29", "", "events-2026-10-29-120000",
                "2026-10-29-120000/../../x"):
        with pytest.raises(HTTPException) as caught:
            await server.admin_diagnostics(token=TOKEN, name=bad)
        assert caught.value.status_code in (400, 404)
