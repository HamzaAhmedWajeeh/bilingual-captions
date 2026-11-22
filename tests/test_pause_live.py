"""The pause thresholds against the recordings of 2026-11-07.

`recordings/` is gitignored, so a clone has none of these and every test
here skips. On the operator's laptop they are the evidence that the silence
floor and the quiet floor are measurements rather than guesses, and the
reason `SILENCE_RMS` moved from -54 dBFS to -66: at -54, a talk that
transcribed 26,495 tokens came 96 seconds from being paused.

**Why these tests and not only the tool.** `tools/level_report.py` prints
the same numbers and is the thing to read when calibrating. These assert the
margins, so the next change to a threshold fails here rather than in a room:
the margin is the finding, and a finding with no test is a sentence in a
file nobody reads at the venue.

Nothing here decodes, plays or transcribes a recording. It reads sample
amplitudes, and every assertion is a level, a count or a number of seconds.
"""

from __future__ import annotations

import math
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.audio_source import CHUNK_MS, SAMPLE_RATE  # noqa: E402
from app.main import QUIET_PEAK_DB  # noqa: E402
from app.soniox_client import (  # noqa: E402
    IDLE_STOP_SECONDS,
    SILENCE_RMS,
    rms_db,
)

RECORDINGS = ROOT / "recordings"

CHUNK = SAMPLE_RATE * CHUNK_MS // 1000
FULL_SCALE = 32768.0

#: The session the operator reported: paused at 632.5 s with the venue's
#: wireless microphone running out of battery, and no way back but Stop and
#: Start.
PAUSED_SESSION = "2026-11-07-140559"
#: The 41.6-minute talk through the same line, the same afternoon, that
#: worked from end to end. The near miss, and the reason the old threshold
#: cannot be defended as "it only fires on a broken input".
WORKING_SESSION = "2026-11-07-141802"
#: A session whose input never carried anything. The other side of the
#: calibration: the auto-stop still has to fire on one of these.
EMPTY_SESSION = "2026-11-07-152933"

#: One pass per recording for the whole module. These are 20-80 MB files.
_CACHE: dict[str, tuple[np.ndarray, np.ndarray]] = {}


def chunks(stem: str) -> tuple[np.ndarray, np.ndarray]:
    """Per-chunk peak and RMS in 0..1, cached."""
    if stem in _CACHE:
        return _CACHE[stem]
    path = RECORDINGS / f"{stem}.wav"
    if not path.is_file():
        pytest.skip(f"{stem}.wav is not on this machine; "
                    "recordings/ is gitignored")
    with wave.open(str(path), "rb") as handle:
        samples = np.frombuffer(handle.readframes(handle.getnframes()),
                                dtype=np.int16)
    audio = samples.astype(np.float32) / FULL_SCALE
    usable = (len(audio) // CHUNK) * CHUNK
    frames = audio[:usable].reshape(-1, CHUNK)
    _CACHE[stem] = result = (
        np.max(np.abs(frames), axis=1),
        np.sqrt(np.mean(np.square(frames), axis=1)),
    )
    return result


def longest_silence(stem: str, floor_rms: float = SILENCE_RMS) -> float:
    """The longest unbroken run below the floor, in seconds.

    The quantity the idle auto-stop measures, which is not the share: a line
    below the floor 96% of the time pauses only if the quiet chunks are
    consecutive.
    """
    _, rms = chunks(stem)
    best = current = 0
    for quiet in rms < floor_rms:
        current = current + 1 if quiet else 0
        best = max(best, current)
    return best * CHUNK_MS / 1000


def db(amplitude: float) -> float:
    """Unfloored dBFS. The noise-floor measurement is the one case where the
    page's clamp is the thing being asked about."""
    return -200.0 if amplitude <= 0 else 20 * math.log10(float(amplitude))


def test_the_old_silence_floor_would_have_paused_a_working_talk():
    """-54 dBFS, measured off a laptop array, against the venue mixer.

    This is the finding. 41.6 minutes and 26,495 final tokens through the
    line, and the longest unbroken run below the old floor was 503.7 s of a
    600 s timeout - 84% of the way to ending a talk that was working.
    """
    old_floor = 10 ** (-54.0 / 20)
    run = longest_silence(WORKING_SESSION, old_floor)
    assert run > 0.8 * IDLE_STOP_SECONDS, (
        f"the near miss is not in this recording: {run:.1f} s")


def test_the_session_that_paused_was_silent_for_the_whole_talk_at_54_db():
    """...and the pause was therefore inevitable, not a fault in the timer.

    The old floor saw 690.2 s of unbroken silence in a session whose engine
    returned 5,655 final tokens. The timer did exactly what it was told; it
    was told the wrong thing.
    """
    old_floor = 10 ** (-54.0 / 20)
    assert longest_silence(PAUSED_SESSION, old_floor) > IDLE_STOP_SECONDS


def test_the_floor_in_use_leaves_a_working_talk_room_to_spare():
    """Both mixer-line talks, against the threshold the code actually uses."""
    for stem in (WORKING_SESSION, PAUSED_SESSION):
        run = longest_silence(stem)
        assert run < 0.4 * IDLE_STOP_SECONDS, (
            f"{stem} is within reach of the pause again: {run:.1f} s")


def test_the_floor_in_use_still_sits_well_above_the_lines_own_noise():
    """The worry that goes with lowering it, ruled out by measurement.

    A floor under the mixer's hiss would never see silence, and the
    overnight protection would quietly stop existing. The 10th-percentile
    chunk RMS on this line is about -91 dBFS, so there is 25 dB of margin.
    """
    for stem in (WORKING_SESSION, PAUSED_SESSION, EMPTY_SESSION):
        _, rms = chunks(stem)
        floor = db(np.percentile(rms, 10))
        assert floor < rms_db(SILENCE_RMS) - 15, (
            f"{stem}: noise floor {floor:.1f} dBFS is too close to the "
            f"silence floor {rms_db(SILENCE_RMS):.1f}")


def test_an_input_carrying_nothing_is_silent_at_every_candidate_floor():
    """The auto-stop still has to fire on a session left open overnight."""
    _, rms = chunks(EMPTY_SESSION)
    for level in (-54.0, -60.0, -66.0, -72.0):
        share = float((rms >= 10 ** (level / 20)).mean())
        assert share == 0.0, (
            f"{EMPTY_SESSION} has audio above {level} dBFS: {share:.4f}")


def test_the_quiet_floor_fires_on_a_mixer_line_that_is_working():
    """Question 4, answered with the per-minute peaks.

    -50 dBFS is not wrong and is not sufficient. Six of the 42 minutes of a
    session that transcribed 26,495 tokens had a loudest 100 ms chunk below
    it, so the warning was on screen for a working input - and no floor
    separates the two cases, because the same session's first minute peaked
    at -54.9. What separates them is whether words are arriving, which is
    why `quiet_note` now carries `speech`.
    """
    peak, _ = chunks(WORKING_SESSION)
    step = int(60 * 1000 / CHUNK_MS)
    per_minute = [db(peak[i:i + step].max())
                  for i in range(0, len(peak), step) if peak[i:i + step].size]
    under = [x for x in per_minute if x < QUIET_PEAK_DB]
    assert len(under) >= 5, (
        f"only {len(under)} of {len(per_minute)} minutes under the quiet "
        "floor; the false-alarm case is no longer in this recording")
    # ...and no plausible lower floor rescues it: the quietest working
    # minute is within a few dB of the one in use.
    assert min(per_minute) > QUIET_PEAK_DB - 6
