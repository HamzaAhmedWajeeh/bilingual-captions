"""A tone of a chosen length, for the tests whose run has to outlive something.

The fixtures in `test_server_audio.py` and `test_transcript.py` make half a
second and one second of audio, which is right for a test that just needs the
audio path to carry something. It is wrong for a test that waits for an event
and then asserts the run is still going, because the source runs out first:

- one reconnect backoff is `soniox_client.BACKOFF_BASE`, 0.5 s, which is the
  whole of `test_server_audio.py`'s tone, so a test waiting for a second
  connection could have the run end underneath it
- a transcript's header says "Still running" only until `finalize`, so a test
  reading it needs the session to still be in flight

Both of those failed on Linux and macOS and passed on Windows on 2026-11-23,
which is the day this file was written. A length is the fix: the window the
test needs exists by construction rather than by how the runner happened to
schedule it.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from app.audio_source import SAMPLE_RATE


def write_tone(path: str | Path, *, seconds: float,
               freq: int = 440, rate: int = SAMPLE_RATE) -> Path:
    """Write `seconds` of a 440 Hz tone as 16-bit mono PCM, and return it."""
    path = Path(path)
    samples = np.rint(
        np.sin(2 * np.pi * freq * np.arange(int(rate * seconds)) / rate)
        * 0.5 * 32767
    ).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return path
