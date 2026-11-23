# Setup

The project is Python and [uv](https://docs.astral.sh/uv/), so it runs anywhere
Python does. **Live audio has only been tested on Windows.** The offline test
suite runs in CI on Windows, macOS and Linux, but the microphone path on macOS
and Linux is PortAudio doing what PortAudio does and nobody has stood in front
of it at an event yet. If you try it, the device naming notes below are where
it will differ.

## 1. Python and dependencies

Python **3.12 or newer**, plus uv:

- Windows: `winget install astral-sh.uv`
- macOS: `brew install uv`
- Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

```sh
git clone https://github.com/HamzaAhmedWajeeh/bilingual-captions
cd bilingual-captions
uv sync
```

`uv sync` creates `.venv` and installs everything pinned in `uv.lock`. For the
test suite as well:

```sh
uv sync --extra dev
```

## 2. PortAudio

Audio input goes through `sounddevice`, which is a binding for PortAudio.

- **Windows and macOS:** nothing to do. The wheel carries PortAudio with it.
- **Debian or Ubuntu:** `sudo apt-get install libportaudio2`
- **Fedora:** `sudo dnf install portaudio`
- **Arch:** `sudo pacman -S portaudio`
- **macOS from source**, or if the import fails anyway: `brew install portaudio`

The import is lazy, so the server starts and the whole offline suite runs on a
machine with no PortAudio at all. You need it the moment you open a microphone.

## 3. A Soniox API key

Sign in at [soniox.com](https://soniox.com), then **Account -> API keys ->
create**. Copy `.env.example` to `.env` and paste it in:

```sh
cp .env.example .env            # PowerShell: Copy-Item .env.example .env
```

```
SONIOX_API_KEY=your-key-here
```

That is the only required value. `.env` is gitignored and is the only place a
secret belongs here. Do not pass the key or the admin token on the command
line, where they land in shell history.

## 4. Start it

```sh
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On a first run with no `ADMIN_TOKEN` set, the server generates one, writes it
to `.env` and prints it once with the URL to open. Keep that URL. To be issued
a new one, delete the `ADMIN_TOKEN` line and restart.

## 5. Choose the input

Open the operator page at the URL it printed, then **Audio -> Input**. The
dropdown lists every input device PortAudio can see.

- From a venue mixer, pick the **line input** your interface presents, usually
  named after the interface rather than after "microphone".
- **On Windows**, prefer the **WASAPI** entry for a device when both appear.
  MME entries work but resample.
- **On macOS** every device arrives through Core Audio, so each one appears
  once and there is no host API to choose between. An aggregate device built in
  Audio MIDI Setup shows up like any other input.
- **On Linux** the same card can appear several times, once per host API (ALSA,
  PulseAudio, JACK). The PulseAudio or PipeWire entry is usually the one that
  works while something else is also using the card.
- If you plugged the interface in after starting the server, press **Refresh
  devices**. PortAudio answers from a snapshot taken when it loaded, so a
  device connected later is invisible until you do.
- Watch the level meter and talk. If it never moves: wrong input, a muted
  device, or the OS blocking microphone access to your terminal. On Windows,
  **Settings -> Privacy & security -> Microphone**; on macOS, **System
  Settings -> Privacy & Security -> Microphone**; on Linux, whatever your
  desktop's portal permissions are, if it has any.

To list devices from the command line instead:

```sh
uv run python tools/list_devices.py
```

The input you start a session with is remembered by name in `config/mic.yaml`,
so you do not have to find it again next time.

## Repository layout

```
app/        the server. captions.py is pure and has no I/O, which is what
            makes replay testing possible. Keep it that way.
web/        three pages, plain JS, no build step
config/     glossary, plus the gitignored files above
brand/       your logos (gitignored)
tools/      device listing, capture replay, analysis, screenshots
tests/      the suite
docs/       this directory
```

Next: [operating.md](operating.md) for running a session,
[testing.md](testing.md) for the suite.
