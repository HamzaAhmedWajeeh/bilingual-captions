"""Everything about a session except what anyone said.

**Why this exists.** On 2026-10-24 the projector was reported showing the
wrong language half an hour into a talk. The response log let the captions be
replayed offline, and they were right; nothing else about that evening was
recorded at all. What page was on the wall, what it had on screen, whether a
browser was rewriting it, whether anything reconnected, whether a setting
moved - none of it existed anywhere, so a day of work could only rule things
out. These two files are what makes the next one answerable.

    logs/events-<session>.jsonl        one line per thing that happened
    logs/screen-health-<session>.jsonl one line per report from every page

Both sit beside `logs/live-<session>.jsonl`, the response log, and share its
name, so a session is one stem and four files.

**What may go in here.** Counts, flags, enumerations, identifiers, user
agents and fingerprints. **Never caption text, and never a session detail** -
no speaker name, no talk title, no word anyone said. The two files that do
hold speech are the response log and the transcript, both of which the
operator opts into and both of which are documented as verbatim records. A
reader of these two should be able to reconstruct *what the room saw* without
learning a word of *what was said in it*.

`logs/` is gitignored, is never served except through `/admin` behind its
token, and nothing here ever deletes anything: a session that went wrong is
exactly the session whose evidence must still be there a week later.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.settings import LOG_DIR

#: `events-<session>.jsonl` and `screen-health-<session>.jsonl`. The stem is
#: the response log's, so one session is one name.
EVENT_PREFIX = "events-"
HEALTH_PREFIX = "screen-health-"
SUFFIX = ".jsonl"

#: Enough for a user agent, a path or an error message, and short enough that
#: no single field can turn a log line into a paragraph.
MAX_FIELD_CHARS = 300

#: Event names. Written as constants because `/admin` and the tests both
#: match on them, and a typo in a log line is invisible until it matters.
SESSION_START = "SESSION_START"
SESSION_STOP = "SESSION_STOP"
#: The idle auto-stop closed the Soniox stream, and the pause was released
#: again. Both carry the input level and the device, which is the one thing
#: 2026-11-07 could not be argued from: the session paused over speech the
#: level could not see, and nothing anywhere recorded what the input was
#: reading at the moment it happened.
#:
#: `IDLE_STOP` is the name this wore until 2026-11-08 and is kept so an
#: older event log still reads - anything matching on the pause should
#: accept both.
PAUSED = "PAUSED"
RESUMED = "RESUMED"
IDLE_STOP = "IDLE_STOP"
ENGINE_STATE = "ENGINE_STATE"
#: One name for both of the things `on_drop` reports - a reconnect after an
#: error and a proactive rollover at the stream cap - because they arrive
#: through the same callback. The line carries both counters, so which one
#: moved is read rather than inferred.
ENGINE_DROP = "ENGINE_DROP"
AUDIO_DROPPED = "AUDIO_DROPPED"
VIEWER_OPEN = "VIEWER_OPEN"
VIEWER_CLOSE = "VIEWER_CLOSE"
SCREEN_RELOAD = "SCREEN_RELOAD"
SETTING_CHANGED = "SETTING_CHANGED"
APPLY_NOW = "APPLY_NOW"
WRONG_LANGUAGE = "WRONG_LANGUAGE"
PAGE_ERROR = "PAGE_ERROR"
OPERATOR_MARK = "OPERATOR_MARK"
#: The engine stopped labelling any speech English and started writing
#: English words in Arabic letters. `LANG_STUCK` is acted on - it reconnects
#: through the rollover path - and `LANG_DRIFT` is only written down. See
#: `app/lang_watch.py` for why those two are not treated alike.
LANG_STUCK = "LANG_STUCK"
LANG_DRIFT = "LANG_DRIFT"
#: The hourly reconnect cap was reached and the watcher gave up asking.
LANG_CAPPED = "LANG_CAPPED"
#: The input has been below the quiet floor for a while. A count and two
#: dB figures; the one thing that would have answered "was it the right
#: microphone?" on 2026-10-31, when no level was recorded anywhere.
AUDIO_QUIET = "AUDIO_QUIET"


def clip(value: Any) -> Any:
    """One field, short enough to stay a field."""
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS - 1] + "…"
    return value


def session_stamp(when: float | None = None) -> str:
    """The name a session's files share."""
    return time.strftime("%Y-%m-%d-%H%M%S", time.localtime(when))


def free_stamp(when: float | None = None, log_dir: Path | None = None) -> str:
    """A session name nothing has used yet.

    Two sessions can start in the same second - an operator pressing Stop and
    Start to clear something is the obvious way, and it is precisely what was
    done on the night this logging exists for. Sharing a name would append
    one session's events to another's and make both unreadable, so the
    second gets a suffix, the way a transcript folder does.
    """
    base = session_stamp(when)
    stamp = base
    attempt = 1
    while event_path(stamp, log_dir).exists():
        attempt += 1
        stamp = f"{base}-{attempt}"
    return stamp


def event_path(session: str, log_dir: Path | None = None) -> Path:
    return (log_dir or LOG_DIR) / f"{EVENT_PREFIX}{session}{SUFFIX}"


def health_path(session: str, log_dir: Path | None = None) -> Path:
    return (log_dir or LOG_DIR) / f"{HEALTH_PREFIX}{session}{SUFFIX}"


class JsonlLog:
    """Append-only JSONL, flushed per line, that never raises.

    Flushed per line for the same reason the response log is: the session
    worth reading these back for is the one that ended badly. Never raises
    for the same reason the spend ledger does not - a log write is not worth
    taking a talk down for - and a failed open simply makes every `add` a
    no-op, so callers need no second code path.
    """

    def __init__(self, path: Path, started_at: float) -> None:
        self.path = path
        self.started_at = started_at
        self.written = 0
        self._fh = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")
        except OSError:
            self._fh = None

    def add(self, row: dict) -> None:
        if self._fh is None:
            return
        now = time.time()
        line = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            # Session-relative seconds, which is how every other measurement
            # in this project is expressed and the only way to line a log up
            # against a response log or a transcript.
            "at": round(now - self.started_at, 2),
            **{key: clip(value) for key, value in row.items()},
        }
        try:
            self._fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            self._fh.flush()
            self.written += 1
        except (OSError, ValueError, TypeError):
            pass

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


class SessionLogs:
    """The event log and the health log for one session, opened together.

    One object so the server has one thing to hold, one thing to close, and
    one place where "is anything being written down" is answered.
    """

    def __init__(self, session: str, started_at: float,
                 log_dir: Path | None = None) -> None:
        self.session = session
        self.events = JsonlLog(event_path(session, log_dir), started_at)
        self.health = JsonlLog(health_path(session, log_dir), started_at)

    def event(self, kind: str, **fields: Any) -> None:
        self.events.add({"event": kind, **fields})

    def report(self, **fields: Any) -> None:
        self.health.add(fields)

    def close(self) -> None:
        self.events.close()
        self.health.close()


def sessions(log_dir: Path | None = None) -> list[dict]:
    """Every session with an event log, newest first. For `/admin`.

    Sizes rather than contents: the listing is a poll response, and what is
    in these files goes out only through the download route.
    """
    directory = log_dir or LOG_DIR
    out: list[dict] = []
    try:
        names = sorted(directory.glob(f"{EVENT_PREFIX}*{SUFFIX}"), reverse=True)
    except OSError:
        return out
    for path in names:
        session = path.name[len(EVENT_PREFIX):-len(SUFFIX)]
        files = {
            "events": path,
            "health": health_path(session, directory),
            "responses": directory / f"live-{session}{SUFFIX}",
        }
        present = {name: p for name, p in files.items() if p.is_file()}
        out.append({
            "session": session,
            "files": sorted(present),
            "bytes": sum(p.stat().st_size for p in present.values()),
        })
    return out


def total_bytes(log_dir: Path | None = None) -> int:
    """How much `logs/` is holding. Nothing here ever deletes anything, so
    the operator is the one who decides when it is too much - and they can
    only decide that if they can see it."""
    directory = log_dir or LOG_DIR
    try:
        return sum(p.stat().st_size for p in directory.iterdir() if p.is_file())
    except OSError:
        return 0
