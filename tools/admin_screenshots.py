"""Screenshot the operator page in each state it matters in.

    uv run uvicorn app.main:app --port 8000      # in another terminal
    uv run --with playwright python tools/admin_screenshots.py

**A server has to be running on port 8000**, and it is not optional: only
the page's own HTML and the five `/admin/*` answers are faked here, so
`style.css` and `admin.js` are fetched from it. Without one they simply fail
to load and every shot comes out as unstyled black-on-white HTML - which
looks like a broken page rather than like a missing server, and is easy to
commit by accident. Start it with `CAPTIONS_STATE_DIR`, `CAPTIONS_LOG_DIR` and
`CAPTIONS_TRANSCRIPT_DIR` pointed at a temporary directory, for the same
reason the test fixtures do.

Drives the **real** page against the **real** server, with only the server's
JSON answers faked by intercepting the five `/admin/*` requests the page
makes. So the HTML, the stylesheet and admin.js are exactly what ships, and
no Soniox call is made - the live and error states are states the server can
reach, not states it is put into here.

Playwright is deliberately **not** a project dependency: the dependency list
lists what this project installs, and a browser engine is not on it. Run it
with `uv run --with playwright`, which keeps it out of `pyproject.toml`.

Every value shown is invented. Real speaker names never appear in a file that
gets committed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.broadcast import DEFAULT_SCREEN  # noqa: E402
from app.soniox_client import DEFAULT_MODEL  # noqa: E402
from app.transcript import FILES as TRANSCRIPT_FILES  # noqa: E402

OUT = ROOT / "docs" / "screens"
BASE = "http://127.0.0.1:8000"

SIZES = {"desktop": (1920, 1080), "phone": (390, 844)}

#: Invented, and obviously so. Never a real speaker.
DUMMY_DETAILS = {
    "title": "Building with agents",
    "speakers": ["Alex Rivera", "ليلى منصور"],
    "companies": ["Northwind Labs"],
    "terms": ["retrieval", "guardrails"],
    "notes": "Demo in the second half.",
}

DEVICES = [
    {"index": 0, "name": "Sound Mapper - Input", "api": "MME",
     "channels": 2, "sample_rate": 44100, "default": True},
    {"index": 17, "name": "Headset Microphone", "api": "Windows WASAPI",
     "channels": 4, "sample_rate": 48000, "default": False},
]

CAPTURES = ["run1.jsonl", "run2-oneway.jsonl", "run4-saudi.jsonl",
            "run5-english.jsonl",
            # A live run'"'"'s own log. It is on this list because a live log
            # replays unchanged, and it is the only kind of capture a
            # transcript can be rebuilt from - so the Transcripts card has
            # something to offer. Invented, like every other value here.
            "live-2026-10-24-193000.jsonl"]

#: Invented, like everything else here. The real invite lives in
#: `config/screen.yaml` on the laptop and is never committed or photographed.
SAMPLE_INVITE = "https://chat.whatsapp.com/SAMPLEINVITECODE00000"

BASE_STATUS = {
    # Which speech model a live run will ask for. The real server fills this
    # on every poll, so leaving it out photographed an empty note and a
    # hyphen where the Status card says "Speech model" - a picture of a
    # feature that looks broken. From the code rather than typed, so these
    # shots cannot claim a model this build does not default to.
    "model": DEFAULT_MODEL,
    # The main-screen card, so it photographs in a real state rather than
    # falling back to whatever the markup defaults to. Built from the hub's
    # own defaults - including the waiting screen's words - so these
    # pictures show what an operator actually opens the page to.
    "screen": dict(DEFAULT_SCREEN, invite=SAMPLE_INVITE),
    # Which QR source is live. The generated one here, so the link field
    # photographs in use rather than greyed out - the organizer's own file
    # is the other state and it is on this laptop, not in a screenshot.
    "qr": {"kind": "generated", "ext": "svg", "tag": "0f1e2d3c",
           "label": "generated from the invite link"},
    # What the open pages say they are rendering. Nothing connected in the
    # default state, which is what the operator opens the page to before a
    # talk; the `replay` and `live-pending` scenarios below photograph the
    # two states that matter.
    "screens": {"count": 0, "want": "auto", "rows": [], "reporting": 0,
                "audience": 0, "wrong_view": 0, "behind": 0, "grey": 0,
                "stale": 0, "translated": 0, "wrong_language": 0},
    # The session logs card. Two invented sessions, so it photographs with
    # something in it; the names are dates and the sizes are sizes, which is
    # all the real listing carries either.
    "session": None,
    "sessions": [
        {"session": "2026-10-24-143245",
         "files": ["events", "health", "responses"], "bytes": 17_301_504},
        {"session": "2026-10-24-142741",
         "files": ["events", "health"], "bytes": 81_920},
    ],
    "logs_bytes": 28_311_552,
    "running": False,
    "mode": "replay",
    "source": None,
    "paced": True,
    "responses": 0,
    "dropped_seconds": 0.0,
    "capture": None,
    # The folder a live run is writing into, which is all the status poll
    # ever carries about a transcript - never a word of what is in it.
    "transcript": None,
    "details": {"title": True, "speakers": 2, "companies": 1, "terms": 2,
                "notes_chars": 24},
    "details_pending": False,
    "error": None,
    "elapsed": None,
    "audio_ms": 0,
    "audio_total_s": 0,
    "captures": CAPTURES,
    "audio_files": ["tone.wav"],
    "usage": {
        "runs": 8, "billable_runs": 7, "billable_minutes": 27.7,
        "free_minutes": 19.1, "estimated_usd": None, "budget_usd": 10.0,
        "budget_remaining_usd": None, "rate_per_minute": None,
        "unreadable_lines": 0,
    },
    "viewers": 0, "max_viewers": 500, "rejected": 0, "dropped": 0,
    "seq": 0, "live": False,
    # The language watch and the quiet-input check. Both absent between
    # sessions, which is the state most of these shots are in.
    "lang": None,
    "quiet": None,
    "lang_drifted": False,
    # The idle pause, which is absent in every state but one.
    "pause": None,
}


def merged(**extra) -> dict:
    status = dict(BASE_STATUS)
    usage = dict(status["usage"])
    usage.update(extra.pop("usage", {}))
    status.update(extra)
    status["usage"] = usage
    return status


SCENARIOS = {
    "stopped": {
        "status": merged(),
        "level": None,
        "note": "nothing running, nothing left over",
    },
    "replay": {
        "status": merged(
            running=True, mode="replay", source="run1.jsonl", elapsed=68.4,
            audio_ms=68400, audio_total_s=300.0, responses=412, seq=398,
            viewers=3, live=True,
            # Two projectors, both showing what was asked for. The good
            # state of the line added after 2026-10-24.
            screens={
                "count": 2, "want": "auto", "reporting": 2, "audience": 0,
                "wrong_view": 0, "behind": 0, "grey": 0, "stale": 0,
                "translated": 0, "wrong_language": 0,
                "rows": [
                    {"reporting": True, "page": "screen", "rendered": "auto",
                     "pinned": False, "visible": True, "grey": 0,
                     "stale": False, "age_ms": 1200, "behind_ms": 0,
                     "wrong_view": False, "behind": False},
                    {"reporting": True, "page": "screen", "rendered": "auto",
                     "pinned": False, "visible": True, "grey": 0,
                     "stale": False, "age_ms": 3400, "behind_ms": 0,
                     "wrong_view": False, "behind": False},
                ],
            },
        ),
        "level": None,
        "note": "a saved recording playing to the main screen",
    },
    "live-pending": {
        "status": merged(
            running=True, mode="mic", source="Headset Microphone @ 16000 Hz",
            elapsed=742.0, audio_ms=742000, audio_total_s=0, responses=2140,
            seq=2098, viewers=23, live=True, details_pending=True,
            engine_state="streaming", lag_seconds=0.42, reconnects=1,
            rollovers=0, silent_seconds=0, streamed_seconds=742.0,
            dropped_chunks=0,
            # Saved, so the `audio` figure in the top bar reads `saving`.
            # That line exists because on 2026-10-31 nobody could tell
            # afterwards whether the audio of two locked talks was kept.
            recording="recordings/2026-10-24-193012.wav",
            capture="logs/live-demo.jsonl",
            transcript="2026-10-24_1930",
            usage={"billable_minutes": 12.4, "estimated_usd": 1.24,
                   "budget_remaining_usd": 8.76, "rate_per_minute": 0.1},
            # The state this line exists for: a projector showing the
            # English stream while the setting says Auto, and an audience
            # page open as well. This is the shape of the 2026-10-24
            # report, and it is worth a picture precisely because it used
            # to be invisible.
            screens={
                "count": 2, "want": "auto", "reporting": 2, "audience": 1,
                "wrong_view": 1, "behind": 0, "grey": 0, "stale": 0,
                "translated": 0, "wrong_language": 1,
                "rows": [
                    {"reporting": True, "page": "screen", "rendered": "en",
                     "pinned": True, "visible": True, "grey": 0,
                     "stale": False, "age_ms": 900, "behind_ms": 0,
                     "wrong_view": True, "behind": False},
                    {"reporting": True, "page": "audience", "rendered": "en",
                     "pinned": False, "visible": True, "grey": 0,
                     "stale": False, "age_ms": 2100, "behind_ms": 0,
                     "wrong_view": False, "behind": False},
                ],
            },
        ),
        "level": {"rms": 0.08, "peak": 0.42, "db": -22.0, "peak_db": -7.5,
                  "clipping": False},
        "note": "live, with talk details saved but not yet applied",
    },
    "live-confirm": {
        "status": merged(
            running=False, mode="replay", source=None,
            usage={"billable_minutes": 12.4, "estimated_usd": 1.24,
                   "budget_remaining_usd": 8.76, "rate_per_minute": 0.1},
        ),
        "level": {"rms": 0.02, "peak": 0.1, "db": -34.0, "peak_db": -20.0,
                  "clipping": False},
        "note": "the confirm step before a live run starts",
    },
    # 2026-11-07, as the operator found it, and as the page answers it now.
    # The banner is the whole point of the shot: that afternoon the page had
    # the pill and nothing else, and the only move it offered was Stop.
    "paused": {
        "status": merged(
            running=True, mode="mic",
            source="Line In (USB Audio Mixer) @ 16000 Hz, ch 1/2", elapsed=703.0,
            audio_ms=703000, audio_total_s=0, responses=2740, seq=2698,
            viewers=1, live=True,
            engine_state="paused", lag_seconds=0.0, reconnects=0,
            rollovers=0, silent_seconds=601.4, streamed_seconds=630.9,
            dropped_chunks=0, transcript="2026-11-07_1405",
            capture="logs/live-demo.jsonl",
            # `input_db` is the engine's figure and floors at -100, not
            # the meter's, which floors at -60: read off the meter the
            # banner said "Input -60.0 dB, needs -66.0 dB", a level that
            # looks above the threshold beside a pause waiting for it.
            pause={"device": "Line In (USB Audio Mixer)", "silent_seconds": 601.4,
                   "idle_stop_seconds": 600.0, "input_db": -86.0,
                   "meter_peak_db": -50.4, "silence_db": -66.0,
                   "pauses": 1},
            # Quiet *and* being transcribed, which is the grey line rather
            # than the red one. Both facts were true that afternoon and the
            # page used to report only the first.
            quiet={"seconds": 667.0, "peak_db": -50.4, "db": -60.0,
                   "device": "Line In (USB Audio Mixer)", "speech": True},
            usage={"billable_minutes": 11.7, "estimated_usd": 1.17,
                   "budget_remaining_usd": 8.83, "rate_per_minute": 0.1},
            screens={
                "count": 1, "want": "auto", "reporting": 1, "audience": 0,
                "wrong_view": 0, "behind": 0, "grey": 0, "stale": 0,
                "translated": 0, "wrong_language": 0,
                "rows": [
                    {"reporting": True, "page": "screen", "rendered": "auto",
                     "pinned": False, "visible": True, "grey": 0,
                     "stale": False, "age_ms": 1100, "behind_ms": 0,
                     "wrong_view": False, "behind": False},
                ],
            },
        ),
        "level": {"rms": 0.001, "peak": 0.003, "db": -60.0,
                  "peak_db": -50.4, "clipping": False},
        "note": "the idle pause, with the reason and the way out of it",
    },
    "error": {
        "status": merged(
            running=False, mode="mic",
            source="Headset Microphone @ 16000 Hz", elapsed=96.3,
            responses=184, seq=180, viewers=0,
            error="unauthenticated: invalid API key",
            engine_state=None, dropped_seconds=18.6,
            usage={"billable_minutes": 12.4, "estimated_usd": 1.24,
                   "budget_remaining_usd": 8.76, "rate_per_minute": 0.1},
        ),
        "level": None,
        "note": "a run that failed, with last-run values marked as past",
    },
}


#: Two invented sessions, so the Transcripts card is photographed with
#: something in it. **Not a real session**: a transcript is a verbatim
#: record of a room, and a committed screenshot of one would be a committed
#: transcript. These are names, sizes and durations - which is all the real
#: listing carries either, the text itself needing a second request.
DUMMY_TRANSCRIPTS = [
    {"name": "2026-10-24_1930", "files": list(TRANSCRIPT_FILES),
     "bytes": 184_320, "duration": "1h 12m", "words": 11480,
     "finished": True},
    {"name": "2026-10-17_0111", "files": list(TRANSCRIPT_FILES),
     "bytes": 318_464, "duration": "2h 04m", "words": 19231,
     "finished": True},
]


def install_routes(page, scenario: dict) -> None:
    """Answer every request the page makes, dispatching on path.

    The page itself is served from `web/admin.html` rather than fetched from
    `/admin`, so **no admin token is involved anywhere in this script**. A
    token in a URL ends up in Playwright error messages, in a browser's own
    logs and in whatever terminal this is run from; the only way to be sure
    it does not is for it never to be here. The stylesheet and admin.js still
    come from the running server, so what is photographed is what is served.
    """
    import json
    from urllib.parse import urlparse

    page_html = (ROOT / "web" / "admin.html").read_text(encoding="utf-8")

    bodies = {
        "/admin/status": scenario["status"],
        "/admin/level": {"running": scenario["status"]["running"],
                         "level": scenario["level"]},
        # The shape `/admin/devices` really returns, so the shots show the
        # dropdown as the operator sees it: the input that worked last time
        # preselected and labelled, and the record tick restored from it.
        "/admin/devices": {
            "devices": DEVICES,
            "remembered": DEVICES[1]["name"],
            "remembered_index": DEVICES[1]["index"],
            "remembered_channel": 1,
            "remembered_at": "2026-11-02 19:40",
            "missing": False,
            "remembered_record": True,
        },
        "/admin/transcripts": {"transcripts": DUMMY_TRANSCRIPTS},
        "/admin/session": {
            "details": DUMMY_DETAILS,
            "pending": scenario["status"]["details_pending"],
            "running": scenario["status"]["running"],
            "glossary_terms": ["RAG", "MCP", "LLM", "fine-tuning",
                               "embeddings", "transformers", "ChatGPT"],
        },
    }

    def handler(route, request):
        path = urlparse(request.url).path
        if path == "/admin":
            route.fulfill(status=200, content_type="text/html; charset=utf-8",
                          body=page_html)
        elif path in bodies:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(bodies[path]))
        else:
            route.continue_()

    page.route("**/*", handler)


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    url = BASE + "/admin"

    written: list[Path] = []
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            for name, scenario in SCENARIOS.items():
                for size_name, (width, height) in SIZES.items():
                    page = browser.new_page(
                        viewport={"width": width, "height": height},
                        device_scale_factor=1,
                    )
                    install_routes(page, scenario)
                    # Not "networkidle": the page polls once a second for ever,
                    # so the network is never idle and the wait would time out.
                    page.goto(url, wait_until="domcontentloaded")
                    if name.startswith("live"):
                        # The source dropdown drives the red Start button and
                        # the live-only rows; set it as an operator would.
                        page.select_option("#source", "live")
                        page.check("#arm")
                    if name == "live-confirm":
                        # ...and the confirm bar only appears after Start is
                        # pressed, which is the point of it.
                        page.click("#start")
                    page.wait_for_timeout(700)
                    target = OUT / f"admin-{name}-{size_name}.png"
                    page.screenshot(path=str(target), full_page=True)
                    written.append(target)
                    page.close()
        finally:
            browser.close()

    for path in written:
        print(f"  {path.relative_to(ROOT)}  {path.stat().st_size // 1024} kB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
