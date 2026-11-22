# Bilingual Captions

Live two-way captions on a venue screen. Someone speaks Arabic, the screen
shows English. Someone answers in English, the screen shows Arabic. One
laptop, one microphone or mixer line, one browser window dragged onto the
projector.

It was built for bilingual events where half the room reads one language
and half reads the other, and speakers switch mid-sentence. The screen
shows the *translated* side only, in the order it was spoken, so the room
always reads the half it could not follow.

![The projector view: the waiting screen, then Arabic speech captioned in English, then a switch to English captioned in Arabic](docs/demo.gif)

<sub>Recorded from `/screen` on the neutral theme, replaying a synthetic
capture. Every word in it was invented for the recording.</sub>

## What it is

- **Two-way.** Arabic to English and English to Arabic in the same session,
  with no operator switching anything. Direction is decided per paragraph
  from the speech itself.
- **Main-screen first.** The projector view is the product. There is no app
  to install and nothing for the audience to scan unless you want a join
  link on the waiting screen.
- **Offline-testable.** Saved engine responses replay through the real
  caption pipeline at real speed, so you can judge the screen without
  speaking, without a mixer and without spending anything.
- **One process.** FastAPI, a WebSocket, three static pages. No database, no
  frontend framework, no build step.
- **Unbranded.** Colours, wording, logos and the join link all come from one
  gitignored file and one folder. Nothing in this repo is anyone's identity.

Speech recognition and translation are done by [Soniox](https://soniox.com).
You need an API key. Everything else runs on your machine.

## Setup on Windows

Windows is the primary target. It runs anywhere Python does, but the audio
device names and the one-liners below are Windows-shaped.

### 1. Python and dependencies

Python **3.11 or newer**. The project uses [uv](https://docs.astral.sh/uv/):

```powershell
winget install astral-sh.uv
git clone https://github.com/HamzaAhmedWajeeh/bilingual-captions
cd bilingual-captions
uv sync
```

`uv sync` creates `.venv` and installs everything pinned in `uv.lock`. For the
test suite as well:

```powershell
uv sync --extra dev
```

### 2. A Soniox API key

Sign in at [soniox.com](https://soniox.com), then **Account -> API keys ->
create**. Copy `.env.example` to `.env` and paste it in:

```powershell
Copy-Item .env.example .env
notepad .env
```

```
SONIOX_API_KEY=your-key-here
```

That is the only required value. `.env` is gitignored and is the only place a
secret belongs here. Do not pass the key or the admin token on the command
line, where they land in shell history.

### 3. Start it

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On a first run with no `ADMIN_TOKEN` set, the server generates one, writes it
to `.env` and prints it once with the URL to open. Keep that URL. To be issued
a new one, delete the `ADMIN_TOKEN` line and restart.

### 4. Choose the input

Open the operator page at the URL it printed, then **Audio -> Input**. The
dropdown lists every input device PortAudio can see.

- From a venue mixer, pick the **line input** your interface presents, usually
  named after the interface rather than after "microphone".
- On Windows, prefer the **WASAPI** entry for a device when both appear. MME
  entries work but resample.
- If you plugged the interface in after starting the server, press **Refresh
  devices**. PortAudio answers from a snapshot taken when it loaded, so a
  device connected later is invisible until you do.
- Watch the level meter and talk. If it never moves: wrong input, a muted
  device, or Windows blocking microphone access to your terminal
  (**Settings -> Privacy & security -> Microphone**).

To list devices from the command line instead:

```powershell
uv run python tools/list_devices.py
```

The input you start a session with is remembered by name in
`config/mic.yaml`, so you do not have to find it again next time.

## The two pages

### `/screen` - the projector

Open `http://127.0.0.1:8000/screen`, drag the window onto the projector and
make it fullscreen. No controls, no cursor, nothing to click. It has three
states and switches between them on its own:

- **Waiting.** Before Start and after Stop. An eyebrow, a headline, a
  subline, and a card that can hold a QR code for a join link.
- **Title card.** Running, but with nothing to show yet - the gap between the
  first word and its translation. A room that has just heard "we are starting"
  should not be looking at a come-and-join-us poster.
- **Captions.** The talk.

Query parameters are testing hatches, not settings: `?lang=en|ar|both|auto`
pins the view against the operator, `?size=s|m|l|xl` the text size,
`?card=1|0` holds the title card up or down, `?glow=0` turns the background
wash off.

### `/admin` - the operator

`http://127.0.0.1:8000/admin?token=YOUR_TOKEN`. One page, from one laptop,
behind a constant-time token check. It is never linked from anywhere.

- **Start / Stop**, with a confirmation step before a live run because a live
  run spends money.
- **Audio.** Input device, level meter, and whether to save the audio to the
  laptop. Saving is off unless you tick it: the room has not consented to
  being recorded.
- **Main screen.** View, text size, the corner mark, the footer disclaimer,
  the background glow, the credit line, and every word on the waiting screen.
  Changes reach an open projector immediately, without touching that browser.
- **Session.** The talk's title, speakers, companies and your own notes. These
  are fed to the engine as context, which is what makes names come out spelled
  right. They are saved to `config/session.yaml`, which is gitignored, and
  they never reach the audience pages, the viewer socket or any log.
- **Transcripts.** Every live session writes `english.md`, `arabic.md`,
  `bilingual.md` and a stats sheet into `transcripts/`, readable and
  downloadable here and nowhere else.
- **Diagnostics.** Mark a moment when something looks wrong, and download one
  session's logs as a zip.

A session can also be driven without the page:

```powershell
curl -X POST "http://127.0.0.1:8000/admin/start?token=YOUR_TOKEN&capture=run1.jsonl&paced=true"
curl -X POST "http://127.0.0.1:8000/admin/stop?token=YOUR_TOKEN"
```

`capture=` replays a saved file instead of opening the microphone, which costs
nothing and reaches no network. `paced=true` replays at real time, including
caption lag; `paced=false` pushes the whole file through in under a second.

## Branding

Nothing in this repo carries anyone's identity. Out of the box it runs on a
neutral theme: a blue accent chosen for contrast, a near-black projector
background, and generic wording on the waiting screen.

Your own branding goes in two places, both gitignored, so it never ends up in
a fork or in this repo's history.

### Words and colours

```powershell
Copy-Item config\brand.example.json config\brand.json
notepad config\brand.json
```

```json
{
  "event_name": "Thursday Night AI",
  "accent": "#4DA3FF",
  "accent_ink": "#0B0B0D",
  "background": "#0B0B0D",
  "event_url": "https://example.com/join",
  "waiting": {
    "eyebrow": "LIVE CAPTIONS",
    "headline": "Captions start shortly.",
    "subline": "Arabic and English captions, side by side,\nas the talk happens.",
    "pill": "Scan to join",
    "cta": "Scan to open the link",
    "note": "Opens the event link"
  }
}
```

Every key is optional; delete one and the neutral default applies. Colours are
six-digit hex and are validated, so a typo falls back rather than breaking the
stylesheet.

**`accent` and `accent_ink` are a pair.** The accent carries text and has text
drawn on it. If you pick something pale, set `accent_ink` to something dark or
the label on it becomes unreadable. The test suite asserts both directions
clear WCAG AA, so a theme that breaks it fails the suite rather than the room.

The waiting screen's wording layers: these values beat the built-in defaults,
and anything typed on `/admin` beats these. So an event sets its wording once
and still lets the operator change it on the day.

### Images

Drop files into `brand/` under these exact names. Any you leave out are hidden
rather than drawn broken.

| File | Where it appears |
| --- | --- |
| `brand/mark.png` | the square mark in both page headers |
| `brand/wordmark-en.png` | the Latin wordmark, on the title card |
| `brand/wordmark-ar.png` | the Arabic wordmark, beside it |
| `brand/favicon-32.png` | the browser tab |
| `brand/apple-touch-icon-180.png` | a phone home screen |

With no wordmark artwork at all, the title card sets `event_name` in type
instead. With artwork, the typed name is not shown, because you have already
said how your name should look.

The wordmarks are drawn white on transparent and only ever appear on the dark
projector view. After adding or replacing them:

```powershell
uv run --with pillow python tools/brand_assets.py
```

That measures the files and writes `web/brand-sizes.css`, so the title card is
sized from your artwork rather than from a number in the stylesheet. Larger
artwork makes the card larger by itself. It is optional: without it the layout
uses sane defaults.

### A join link

The waiting screen's card always holds something. In order of precedence:
`config/qr.svg` or `config/qr.png` if you drop one in, served byte for byte;
otherwise a code drawn from the link you type on `/admin`; otherwise
`event_url`. All gitignored. A real invite link is an open door for anyone
holding it, so it never leaves the laptop and never reaches the viewer socket.

## Cost

Soniox bills audio and text tokens separately. With two-way translation
running, this project measured roughly **$0.20 per hour of live audio** on its
own usage, so a two-hour evening is about 40 cents.

Your figure moves with how much is actually said, so check
[soniox.com/pricing](https://soniox.com/pricing) against your own invoice
rather than trusting the number above. `SONIOX_RATE_PER_MINUTE` in `.env`
feeds the running spend estimate on `/admin`; it is a counter, not a cap.

Three things keep the bill honest:

- **Replay costs nothing.** Development, tests and judging the screen all run
  off saved files.
- **A session that goes quiet pauses itself** rather than streaming silence,
  with a banner on `/admin` saying what it is waiting for and a Resume button.
- **Minutes are logged** to `logs/usage.jsonl`, with the running total on
  `/admin`.

Only a live session and the two paid tools in `tools/` ever reach Soniox. The
entire test suite does not.

## Tests

```powershell
uv sync --extra dev
uv run pytest
```

The offline suite needs no network and no key. For the browser and image tests
as well, after a one-off `uv run --with playwright playwright install
chromium`:

```powershell
uv run --with playwright --with pillow --with zxing-cpp pytest
```

**The capture-based tests are skipped on a fresh clone, and that is expected.**
A capture is a verbatim transcript of whatever was said in front of the
microphone, so none are committed here. Everything else runs without them. To
turn those tests on, put your own recordings in `tests/fixtures/` under the
names `tests/captures.py` documents, either by recording one:

```powershell
uv run python tools/probe_soniox.py --file talk.m4a --out tests/fixtures/run1.jsonl
```

or by copying a live response log out of `logs/` after a session. Both files
carry real speech, which is why `tests/fixtures/*.jsonl`, `logs/`,
`recordings/` and `transcripts/` are all gitignored.

## What is private

Gitignored, and deliberately so. None of it has history to recover it from, so
back up anything you care about.

| Path | What it holds |
| --- | --- |
| `.env` | the API key and the admin token |
| `config/brand.json`, `brand/` | your branding |
| `config/session.yaml` | speaker names, the talk title, your notes |
| `config/screen.yaml` | the operator's saved screen wording and join link |
| `config/qr.svg`, `config/qr.png` | your own QR artwork |
| `config/mic.yaml` | which input this laptop last opened |
| `transcripts/` | a verbatim record of each session |
| `logs/`, `recordings/` | response logs, event logs, saved audio |
| `tests/fixtures/*.jsonl` | captures, which are transcripts too |

Committed example files show the shape of each one: `.env.example`,
`config/brand.example.json`, `config/session.example.yaml`.

## Layout

```
app/        the server. captions.py is pure and has no I/O, which is what
            makes replay testing possible. Keep it that way.
web/        three pages, plain JS, no build step
config/     glossary, plus the gitignored files above
brand/       your logos (gitignored)
tools/      device listing, capture replay, analysis, screenshots
tests/      the suite
```

## Licence

MIT. See [LICENSE](LICENSE).
