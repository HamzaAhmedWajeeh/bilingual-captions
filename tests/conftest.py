"""Suite-wide safety net: the tests may not touch the operator's own files.

`config/session.yaml` and `config/screen.yaml` are the two files `/admin`
writes - the talk's speaker names, the waiting screen's words, the credit
line and the WhatsApp invite link - and `transcripts/` is where a live
session writes itself down. All three are local-only and gitignored, which
also means that when a test overwrites or litters one there is no history to
recover it from: whatever the operator had is simply gone, and they find out
on the morning of a talk.

**This has now happened three times.** A test server started against this
repository is an operator as far as the save path is concerned, so the
browser suites were saving their own settings over the real `screen.yaml`;
when the write path moved to `STATE_DIR`, `tests/test_session_privacy.py`'s
quarantine fixture was still only patching `CONFIG_DIR` and wrote its
sentinel speaker names into the real `session.yaml`; and when transcripts
landed (2026-10-20), every live-session test in `test_server_audio.py` and
`test_session_privacy.py` wrote a folder into the operator's real
`transcripts/`. That one left **111 folders** sitting beside a real recorded
session, and it was found by the operator, not by the suite.

So this file does two things now.

**It prevents.** `transcripts_go_somewhere_temporary` is autouse and
redirects the transcript directory for *every* test, whatever its own
fixtures do or forget to do. A test that wants its own folder still patches
it again; a test that has never heard of transcripts is covered anyway. The
two config files keep their per-test arrangements, because those are read
through several names and a blanket patch would hide a real mistake.

**It notices.** The run fails at the end, loudly, naming what was touched -
the config files' contents are put back, and folders the run created under
the real `transcripts/` are removed, since by definition they are test
output.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Redirect `logs/` **before anything imports `app`**, which the fixtures
#: below cannot do.
#:
#: `app/main.py` builds its `CaptionSession` at module scope, so the
#: main-screen settings are adopted - and the "startup" audit lines written -
#: the moment a test module imports it, during collection. That is long
#: before an autouse fixture runs, so the first thing a test session did was
#: append the operator's real `logs/screen-settings.log`, and the
#: size-watching guard below could not even see it: the file had already
#: grown when it took its "before" snapshot.
#:
#: `setdefault`, so a suite that wants its own directory still wins.
os.environ.setdefault(
    "CAPTIONS_LOG_DIR", tempfile.mkdtemp(prefix="captions-test-logs-"))

#: The files `/admin` writes. Not `glossary.yaml`: it is committed, nothing
#: writes it, and git would show a change in it anyway.
OPERATOR_FILES = (
    ROOT / "config" / "session.yaml",
    ROOT / "config" / "screen.yaml",
    # Added 2026-11-03, the day it was written by a test run. This one is
    # created rather than edited - a fresh laptop has none - so a stray test
    # leaves a file where there was nothing, which `read_bytes() is None`
    # catches the same way.
    ROOT / "config" / "mic.yaml",
)

#: Where a live session writes its readable transcript. A folder per talk,
#: so what a stray test leaves behind is a new directory rather than a
#: changed file - which is why this is watched by listing rather than by
#: comparing bytes.
TRANSCRIPTS = ROOT / "transcripts"

#: The files in `logs/` that a run *appends* to. Watched by size: they are
#: the operator's record of what has been streamed and what was changed on
#: the projector, and a test adding a line to either is invisible in git.
APPENDED_LOGS = (
    ROOT / "logs" / "usage.jsonl",
    ROOT / "logs" / "screen-settings.log",
)

#: ...and the files a session *creates*: one event log and one health log per
#: run, named for the session. Watched by listing rather than by size, like
#: the transcript folders and for the same reason - a stray test leaves a new
#: file rather than a longer one.
SESSION_LOG_GLOBS = ("events-*.jsonl", "screen-health-*.jsonl",
                     "live-*.jsonl")


@pytest.fixture(autouse=True)
def transcripts_go_somewhere_temporary(tmp_path, monkeypatch):
    """Point the transcript directory at this test's own temp directory.

    Autouse and suite-wide on purpose. Every other guard in this project is
    per-test - each suite patches what it knows it touches - and that is
    exactly how 111 folders ended up in the operator's real `transcripts/`:
    the tests that write one are the *live-session* tests, which live in
    files that had no reason to have heard of transcripts.

    Both names are patched, because `app.main` binds the value at import
    time: patching only `app.settings` leaves the server using the real
    folder, which is the same shape of mistake as patching `CONFIG_DIR` and
    not `STATE_DIR`.
    """
    import app.settings as settings_module

    target = tmp_path / "transcripts"
    monkeypatch.setattr(settings_module, "TRANSCRIPT_DIR", target)
    try:
        import app.main as server
    except Exception:                 # pragma: no cover - import-time failure
        return target
    monkeypatch.setattr(server, "TRANSCRIPT_DIR", target)
    return target


@pytest.fixture(autouse=True)
def the_state_directory_is_temporary(tmp_path, monkeypatch):
    """Point the files `/admin` writes at this test's own temp directory.

    **The fourth time this lesson has been learned, and the first time it
    was caught by a guard rather than by the operator.** Every test that
    starts a mic, file or live session now persists the operator's capture
    choices, and the tests that start sessions are - again - the ones whose
    own fixtures had never heard of the file. One run wrote a real
    `config/mic.yaml`.

    So this is autouse and suite-wide, like the transcript and log fixtures
    above, and for exactly the same reason: a per-suite quarantine protects
    the suites that know they need it, and the files that write a *new*
    artefact are rarely those. `STATE_DIR` is patched on `app.settings` and
    the three names bound from it at import time.
    """
    import app.settings as settings_module

    target = tmp_path / "state"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings_module, "STATE_DIR", target, raising=False)
    return target


@pytest.fixture(autouse=True)
def logs_go_somewhere_temporary(tmp_path, monkeypatch):
    """Point the three things a run writes down at a temporary directory.

    Same reasoning as the transcripts above, and found the same way: the
    suite had put **968 replay lines into the operator's `logs/usage.jsonl`**,
    122 of them from `run1.jsonl`, because every test that starts a session
    writes a ledger line when it ends and no fixture had heard of it. The
    spend total survived - replay lines are `billable: false` - but the
    record of what was really streamed did not.

    Patched on each module that bound the name at import time, which is why
    this is a list rather than one line.
    """
    import app.screen_audit as audit_module
    import app.settings as settings_module
    import app.usage as usage_module

    # Not `tmp_path / "logs"`: `test_usage.py` writes a *file* at that name
    # on purpose, to prove a ledger write cannot take a session down with it.
    target = tmp_path / "run-logs"
    target.mkdir(parents=True, exist_ok=True)
    for module in (settings_module, usage_module, audit_module):
        monkeypatch.setattr(module, "LOG_DIR", target, raising=False)
    try:
        import app.main as server
    except Exception:                 # pragma: no cover - import-time failure
        return target
    monkeypatch.setattr(server, "LOG_DIR", target, raising=False)
    return target


def _log_sizes() -> dict[Path, int]:
    """Sizes of the files in `logs/` that a run appends to."""
    return {path: path.stat().st_size for path in APPENDED_LOGS
            if path.exists()}


def _session_logs() -> set[str]:
    """Every per-session log file in the operator's real `logs/`."""
    directory = ROOT / "logs"
    if not directory.is_dir():
        return set()
    return {path.name for pattern in SESSION_LOG_GLOBS
            for path in directory.glob(pattern)}


def _transcript_folders() -> set[str]:
    if not TRANSCRIPTS.is_dir():
        return set()
    return {child.name for child in TRANSCRIPTS.iterdir() if child.is_dir()}


@pytest.fixture(scope="session", autouse=True)
def operator_files_are_untouched():
    """Fail the run if a test wrote the operator's config or transcripts."""
    before = {path: (path.read_bytes() if path.exists() else None)
              for path in OPERATOR_FILES}
    folders_before = _transcript_folders()
    logs_before = _log_sizes()
    session_logs_before = _session_logs()

    yield

    damaged = []
    for path, original in before.items():
        now = path.read_bytes() if path.exists() else None
        if now == original:
            continue
        damaged.append(path)
        # Put it back before reporting, so the operator's settings survive
        # even a run that is killed after this point.
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)

    # A transcript is a folder rather than a file, so a stray test adds one
    # instead of changing one. Anything that appeared while the suite was
    # running is test output by definition, so it is removed - but every one
    # is named, because 111 of them accumulated silently once already.
    littered = sorted(_transcript_folders() - folders_before)
    for name in littered:
        shutil.rmtree(TRANSCRIPTS / name, ignore_errors=True)

    assert not damaged, (
        "the test run wrote "
        + ", ".join(str(p.relative_to(ROOT)) for p in damaged)
        + " - the operator's own settings, which are gitignored and have no "
        "history to recover from. The contents have been restored. Point "
        "the test at a temporary directory instead: `CAPTIONS_STATE_DIR` for "
        "a server started as a subprocess, or monkeypatch "
        "`app.settings.STATE_DIR` for an in-process one."
    )
    # Appended-to rather than created, so this is watched by size. Nothing is
    # put back: truncating the operator's ledger to "what it was" would throw
    # away whatever a real run wrote while the suite happened to be running,
    # which is worse than the extra lines. Naming it is the remedy.
    grown = sorted(
        str(path.relative_to(ROOT))
        for path, size in _log_sizes().items()
        if size > logs_before.get(path, 0)
    )

    # A session's own logs are new files rather than longer ones, so they
    # are listed. Removed, because anything that appeared while the suite ran
    # is test output by definition - but every one is named.
    new_session_logs = sorted(_session_logs() - session_logs_before)
    for name in new_session_logs:
        (ROOT / "logs" / name).unlink(missing_ok=True)

    assert not littered, (
        f"the test run left {len(littered)} folder(s) in the operator's real "
        f"transcripts/: " + ", ".join(littered) + ". They have been removed, "
        "but a real session's transcript sits in that directory and is "
        "gitignored. Point the test at a temporary directory: "
        "`CAPTIONS_TRANSCRIPT_DIR` for a server started as a subprocess - the "
        "autouse fixture above covers every in-process test already, so this "
        "firing means something escaped it."
    )
    assert not new_session_logs, (
        f"the test run wrote {len(new_session_logs)} per-session log file(s) "
        f"into the operator's real logs/: " + ", ".join(new_session_logs)
        + ". They have been removed, but a real session's response log sits "
        "in that directory and is gitignored. `CAPTIONS_LOG_DIR` moves them; "
        "`tests/conftest.py` sets it before anything imports `app`, so this "
        "firing means a server was started without the environment."
    )

    assert not grown, (
        "the test run appended to " + ", ".join(grown) + " - the operator's "
        "own record of what has been streamed and what was changed on the "
        "projector, gitignored and with no history to recover from. The lines "
        "have been left in place, because truncating the file would throw "
        "away anything a real run wrote in the meantime. Point the test at a "
        "temporary directory: `CAPTIONS_LOG_DIR` for a server started as a "
        "subprocess - the autouse fixture above covers every in-process test, "
        "so this firing means something escaped it."
    )
