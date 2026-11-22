"""Environment and YAML configuration.

Secrets come from the environment (.env on the venue laptop). Everything an
operator edits during a session lives in config/*.yaml so it survives restarts.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Written at the top of every saved session file, so someone who opens it
#: in an editor learns the two things that matter about it.
SCREEN_FILE_HEADER = (
    "# What the main screen shows. Written by /admin - change it there, not\n"
    "# here. LOCAL ONLY: gitignored. The credit line is typed by the operator\n"
    "# and the invite link is an open door; neither is ever committed.\n"
)

SESSION_FILE_HEADER = (
    "# This talk's details. Written by /admin - edit them there, not here.\n"
    "# LOCAL ONLY: gitignored. Speaker names and titles are private and are\n"
    "# never committed. See config/session.example.yaml.\n"
)

MIC_FILE_HEADER = (
    "# What the operator last chose about capturing audio: which input a live\n"
    "# session opened, and whether the audio was being saved. /admin reads\n"
    "# both back, so neither has to be set again from memory. Written by the\n"
    "# server at Start - change it by starting a session the way you want it.\n"
    "# LOCAL ONLY: gitignored. It names this laptop's hardware and belongs to\n"
    "# the machine, not to the project.\n"
)

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
CONFIG_DIR = ROOT / "config"

#: Where the two files `/admin` *writes* live: `session.yaml` and
#: `screen.yaml`. Normally `config/`, and overridable with
#: `CAPTIONS_STATE_DIR`.
#:
#: The override exists for one reason: a test server started against this
#: repository is an operator as far as the save path is concerned, and the
#: browser tests drive `/admin/screen`. They were overwriting the real
#: settings - the credit line, the waiting screen's words and the WhatsApp
#: invite link - and a backup-and-restore in the fixture only narrowed the
#: window. Pointing the tests at a temporary directory closes it.
#:
#: `config/glossary.yaml` deliberately stays on `CONFIG_DIR`: it is public
#: vocabulary, it is committed, nothing writes it, and a test that silently
#: lost it would be testing a context with no terms in it.
STATE_DIR = Path(os.environ.get("CAPTIONS_STATE_DIR") or CONFIG_DIR)

#: Where the three things a run writes down go: the response log, the spend
#: ledger and the main-screen audit line. Gitignored and never served.
#:
#: `CAPTIONS_LOG_DIR` moves it, and it exists for the same reason the other
#: two overrides do, found the same way. The suite had appended **968 replay
#: lines to the operator's `logs/usage.jsonl`** - 122 of them `run1.jsonl` -
#: because every test that starts a session writes a ledger line at the end
#: and no fixture had any reason to know that. They are all `billable:
#: false`, so the dollar total survived; the record of what was actually
#: streamed did not. A third writer landing in this directory
#: (`screen_audit`) is what made it worth closing properly rather than
#: per-suite.
#:
#: The capture *search* path in `app/main.py` deliberately keeps using
#: `ROOT / "logs"`: a test server must still be able to replay the
#: operator's saved sessions, which is the whole point of a live log being
#: a capture.
LOG_DIR = Path(os.environ.get("CAPTIONS_LOG_DIR") or (ROOT / "logs"))
RECORDING_DIR = ROOT / "recordings"

#: Where a live session's readable transcript is written: three markdown
#: files and a stats sheet per talk, in a folder named for the date.
#: Gitignored, never served except through `/admin` behind its token, and
#: written by live sessions only - see `app/transcript.py`.
#:
#: `CAPTIONS_TRANSCRIPT_DIR` moves it, for the same reason `CAPTIONS_STATE_DIR`
#: exists: a test server started against this repository is an operator as
#: far as the write path is concerned, and a transcript is a verbatim record
#: of a room. In-process tests monkeypatch `main.TRANSCRIPT_DIR`; anything
#: that starts a real server sets the variable, and then no test run can put
#: a file in the operator's own folder even by accident.
TRANSCRIPT_DIR = Path(
    os.environ.get("CAPTIONS_TRANSCRIPT_DIR") or (ROOT / "transcripts")
)


class Settings(BaseSettings):
    """Secrets and deploy-time values. Never serialized to a client."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    soniox_api_key: str = ""
    #: Which real-time model a live session asks for. Empty means
    #: `soniox_client.DEFAULT_MODEL`, which is the model this project has
    #: measured; the only other accepted value is the other entry in
    #: `soniox_client.MODELS`.
    #:
    #: It is here rather than in `config/*.yaml` because it is not a per-talk
    #: choice: it says which build of the engine this laptop streams to, it
    #: cannot be changed without a new connection, and a wrong value should
    #: stop the server coming up rather than surface as a fatal engine error
    #: at Start. The reason it is settable at all is `stt-rt-v5`'s removal
    #: date of 2026-12-15: until then, going back is one line in `.env` and a
    #: restart, which is a thing an operator can do on a bad evening.
    soniox_model: str = ""
    admin_token: str = ""
    #: Unused today. It was for the QR code and the Cloudflare tunnel, which
    #: are not built: the captions are the main screen only, and a hosted
    #: version will be a separate branch. Kept so that branch has a home for
    #: it; deliberately absent from `.env.example` so nobody sets it expecting
    #: something to happen.
    public_url: str = "http://localhost:8000"

    #: Soniox credit, in dollars, that the whole project has to live inside
    #: (a standing rule). Shown on `/admin` beside what has been spent.
    soniox_budget_usd: float = 10.0
    #: Dollars per streamed minute. Left at 0 because this repo has never
    #: recorded the real figure, and `app.usage` would rather show counted
    #: minutes than an invented dollar amount. Set it in `.env` to turn the
    #: dollar estimate on.
    soniox_rate_per_minute: float = 0.0

    #: An optional *default* for the title card's credit line, used only on a
    #: fresh install where `config/screen.yaml` has nothing saved yet. The
    #: operator edits it on `/admin` from then on, and that file wins.
    #:
    #: **The one value in this class that is meant to reach a client.** The
    #: docstring above says these are never serialized, and that still holds
    #: for every other field - this one exists to be read by a room. It is not
    #: a session detail: it says who is running the captions, not who is
    #: speaking, and `config/session.yaml` remains the only home for speaker
    #: names (`tests/test_session_privacy.py`).
    screen_credit: str = ""


class Glossary(BaseModel):
    terms: list[str] = Field(default_factory=list)
    translation_terms: list[dict[str, str]] = Field(default_factory=list)


class SessionContext(BaseModel):
    """This talk's private details. **Never public.**

    Speaker names, the title and the operator's notes are private and must
    never be committed. `config/session.yaml` is gitignored; the committed
    shape is `config/session.example.yaml`. These values reach exactly two places: the
    Soniox `context` object, and `/admin` behind its token. A test asserts
    they appear in neither the audience pages, the viewer socket, nor any log.

    An absent file is normal, not an error - a fresh clone has none, and the
    session simply starts with everything empty.
    """

    title: str = ""
    speakers: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    #: Vocabulary for this talk only, merged with the public glossary.
    terms: list[str] = Field(default_factory=list)
    notes: str = ""

    def is_empty(self) -> bool:
        return not (self.title or self.speakers or self.companies
                    or self.terms or self.notes)

    def summary(self) -> dict[str, int | bool]:
        """Key names and counts, no values. Safe to log or to show anywhere.

        The point of this method is that there is an obvious thing to reach
        for when something needs to be recorded about the context, so nobody
        reaches for the context itself.
        """
        return {
            "title": bool(self.title),
            "speakers": len(self.speakers),
            "companies": len(self.companies),
            "terms": len(self.terms),
            "notes_chars": len(self.notes),
        }


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_glossary(path: Path | None = None) -> Glossary:
    return Glossary(**_load_yaml(path or CONFIG_DIR / "glossary.yaml"))


def load_session(path: Path | None = None) -> SessionContext:
    return SessionContext(**_load_yaml(path or STATE_DIR / "session.yaml"))


def load_screen(path: Path | None = None) -> dict:
    """The operator's saved main-screen settings, or `{}` on a fresh clone.

    Deliberately untyped: `BroadcastHub` owns what the keys mean and ignores
    anything it does not recognise, so a file written by an older build can
    never stop the server starting.
    """
    data = _load_yaml(path or STATE_DIR / "screen.yaml")
    return data if isinstance(data, dict) else {}


def save_screen(settings_map: dict, path: Path | None = None) -> None:
    """Write the main-screen settings atomically.

    Same reasoning as `save_session`: the operator may be changing this
    mid-talk, and a half-written file read at startup is a parse error on the
    morning of a session.
    """
    target = Path(path or STATE_DIR / "screen.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(SCREEN_FILE_HEADER)
            yaml.safe_dump(dict(settings_map), fh, allow_unicode=True,
                           sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def load_mic(path: Path | None = None) -> dict:
    """What the operator last chose about capturing audio, or `{}`.

    Two things, written at different moments: which input a live session
    actually opened, and whether "Save the audio to this laptop" was ticked.

    Written by name as well as by index, because the index is PortAudio's
    position in a list that changes when anything is plugged in: on
    2026-10-31 the mixer was the Windows default and `/admin` offered it only
    as the default entry, so what the operator picked and what the stream
    opened were two different questions that nothing could tell apart
    afterwards. The name is what an operator recognises and the index is
    only a hint.

    Untyped and tolerant like `load_screen`: a file from an older build must
    never stop the server starting.
    """
    data = _load_yaml(path or STATE_DIR / "mic.yaml")
    return data if isinstance(data, dict) else {}


def update_mic(changes: dict, path: Path | None = None) -> dict:
    """Merge into the remembered capture choices and write them back.

    A merge rather than a straight write because the two halves are decided
    at different moments: the record flag is the operator's literal tick,
    known when Start is pressed, and the device name is whatever the driver
    resolved, known only once it is open. Writing the whole file from either
    one would clobber the other.
    """
    data = load_mic(path)
    data.update(changes)
    save_mic(data, path)
    return data


def save_mic(mic: dict, path: Path | None = None) -> None:
    """Write the remembered capture choices. Atomic, like the others."""
    target = Path(path or STATE_DIR / "mic.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(MIC_FILE_HEADER)
            yaml.safe_dump(dict(mic), fh, allow_unicode=True, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def save_session(session: SessionContext, path: Path | None = None) -> None:
    """Write the session file atomically.

    Temp file in the same directory, then `os.replace`, which is atomic on
    both Windows and POSIX. The operator may be saving this mid-talk while
    the session is reading it to build a context; a half-written YAML file
    would be a parse error at the worst possible moment.
    """
    target = Path(path or STATE_DIR / "session.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(SESSION_FILE_HEADER)
            yaml.safe_dump(
                session.model_dump(), fh, allow_unicode=True, sort_keys=False
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def soniox_context(
    glossary_path: Path | None = None, session_path: Path | None = None
) -> dict:
    """The Soniox `context` object from the two YAML files.

    the design brief: `general` carries the title, speakers and companies
    as key-value items, `text` the operator's free notes, and `terms` /
    `translation_terms` come from the glossary. `speakers` is the cheapest
    quality win available and is usually the empty list - see
    the engine notes on proper nouns.

    `tools/probe_soniox.py` has its own copy of this from Phase 1. It is left
    alone deliberately: it is the tool that produced the five captures, and
    the value of being able to re-run it unchanged is worth more than removing
    a duplicated dict.
    """
    glossary = load_glossary(glossary_path)
    session = load_session(session_path)
    return build_context(glossary, session)


def build_context(glossary: Glossary, session: SessionContext) -> dict:
    """The same thing from values already in memory.

    Separate from `soniox_context` so the running session can build a context
    from the edits it is holding without a round trip through the file.
    """
    general: list[dict[str, str]] = []
    if session.title:
        general.append({"key": "title", "value": session.title})
    for name in session.speakers:
        general.append({"key": "speaker", "value": name})
    for company in session.companies:
        general.append({"key": "company", "value": company})
    # This talk's vocabulary on top of the public glossary, de-duplicated and
    # order-preserving so the file reads the way the operator wrote it.
    terms = list(glossary.terms)
    for term in session.terms:
        if term and term not in terms:
            terms.append(term)
    return {
        "general": general,
        "text": session.notes,
        "terms": terms,
        "translation_terms": glossary.translation_terms,
    }


settings = Settings()
