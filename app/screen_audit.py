"""An audit line for every change to the main-screen settings.

**Why this exists.** On 2026-10-24 the projector was reported showing the
wrong stream in the middle of a talk, with nobody near `/admin`. Answering
"did a setting change, and who changed it" took a day of replaying logs and
reading an mtime on `config/screen.yaml` - which says only *that* the file
was last written four days earlier, not what happened during the session. One
line per changed field, with where the change came from, settles that
question in a second the next time it is asked.

    2026-10-29T11:02:04Z  admin    view      auto -> en
    2026-10-29T11:02:09Z  admin    size      l -> xl
    2026-10-29T10:58:00Z  startup  headline  '' -> 'Captions start shortly.'
    2026-10-29T11:04:00Z  admin    invite    <hidden:0> -> <hidden:3f8a1c92>

Lands in `logs/`, which is gitignored and never served over HTTP, for the
same reason everything else there is: it holds the operator's own words.

Two rules about what goes in a line:

- **The invite link is never written.** It is the one setting that is a
  secret (`config/screen.yaml`, `/admin` behind its token and the
  QR's pixels, and nowhere else - not a log). It is recorded as the same
  eight-character fingerprint the viewer socket already carries, so a reader
  can tell one link from another without being handed either.
- **No caption text, ever.** Nothing here comes from the engine; every value
  is something an operator typed or a default the server shipped with.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.invite_qr import tag as fingerprint
from app.settings import LOG_DIR

AUDIT_FILE = "screen-settings.log"

#: Long enough for any of the waiting screen's lines, short enough that one
#: change is one line in a terminal.
MAX_VALUE_CHARS = 90

#: Settings whose value must never be written down. Only one, and it is the
#: reason this list exists rather than a comment.
SECRET_FIELDS = frozenset({"invite"})

#: Settings whose values are a handful of known words, written bare. Quoting
#: `'auto' -> 'en'` reads as text that happens to be short; `auto -> en`
#: reads as the switch it is, and these are the two lines anyone scanning
#: this file after a talk is looking for.
ENUM_FIELDS = frozenset({"view", "size"})


def audit_path(log_dir: Path | None = None) -> Path:
    return (log_dir or LOG_DIR) / AUDIT_FILE


def render_value(field: str, value: object) -> str:
    """One field's value, as a line may carry it."""
    if field in SECRET_FIELDS:
        text = str(value or "")
        # A hash, not the link - and `<hidden:0>` for "there wasn't one",
        # so setting a link for the first time is distinguishable from
        # replacing one.
        return f"<hidden:{fingerprint(text) if text else '0'}>"
    if isinstance(value, bool):
        return "on" if value else "off"
    if field in ENUM_FIELDS:
        return str(value)
    text = str(value).replace("\n", "\\n").replace("\r", "")
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS - 1] + "…"
    return f"'{text}'"


def record(changes: list[tuple[str, object, object]], source: str,
           log_dir: Path | None = None) -> int:
    """Append one line per changed field. Returns how many were written.

    `source` says where the change came from - `admin` for the operator's
    page, `startup` for settings read back from disk, and its own name for
    anything else that ever gains the ability to move these. Never raises: an
    audit line is worth having and is not worth losing a talk over, so a
    read-only disk costs the line and nothing more.
    """
    if not changes:
        return 0
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines = [
        f"{stamp}  {source:<8} {field:<9} "
        f"{render_value(field, old)} -> {render_value(field, new)}"
        for field, old, new in changes
    ]
    path = audit_path(log_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        return 0
    return len(lines)
