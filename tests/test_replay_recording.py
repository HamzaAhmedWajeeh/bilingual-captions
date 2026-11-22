"""The offline half of `tools/replay_recording.py`.

The streaming half spends money and is not tested here; what is tested is
everything that decides *what* gets streamed, because an off-by-a-minute
window would be paid for twice.
"""

from __future__ import annotations

import contextlib
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import replay_recording as R  # noqa: E402


def make_wav(path: Path, seconds: float, rate: int = 16000,
             channels: int = 1) -> Path:
    """A WAV whose every sample encodes its own frame index.

    That makes a slice checkable: the first sample of the cut says which
    frame it came from, so an off-by-one in the seek shows up as a number
    rather than as audio that sounds fine.
    """
    frames = int(seconds * rate)
    with contextlib.closing(wave.open(str(path), "wb")) as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"".join(
            (index % 1000).to_bytes(2, "little", signed=False)
            for index in range(frames)
        ))
    return path


def first_sample(path: Path) -> int:
    with contextlib.closing(wave.open(str(path), "rb")) as wav:
        return int.from_bytes(wav.readframes(1), "little", signed=False)


# ---------------------------------------------------------------------------
# Offsets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,seconds", [
    ("0", 0.0),
    ("90", 90.0),
    ("1:30", 90.0),
    ("24:30", 1470.0),
    ("30:27", 1827.0),
    ("1:30:00", 5400.0),
    ("1:30.5", 90.5),
])
def test_offsets_parse(text, seconds):
    assert R.parse_offset(text) == pytest.approx(seconds)


def test_an_absent_offset_passes_through():
    assert R.parse_offset(None) is None
    assert R.parse_offset("") is None


def test_a_bad_offset_is_refused_rather_than_guessed():
    """A misread offset is money spent on the wrong minutes."""
    with pytest.raises(Exception):
        R.parse_offset("half past two")


def test_the_window_label_is_filename_safe_and_distinct():
    """Two runs must never collide on one log file."""
    first = R.label(1470.0, 1827.0)
    second = R.label(1080.0, 1827.0)
    assert first != second
    for name in (first, second, R.label(0.0, None)):
        assert name == Path(name).name
        assert all(char.isalnum() or char in "-m" for char in name), name
    assert R.label(0.0, None).endswith("end")


# ---------------------------------------------------------------------------
# Slicing
# ---------------------------------------------------------------------------


def test_a_window_is_cut_at_the_right_frame(tmp_path):
    source = make_wav(tmp_path / "talk.wav", 60.0)
    cut, seconds = R.slice_wav(source, 10.0, 20.0)
    try:
        assert seconds == pytest.approx(10.0)
        # 10 s in at 16 kHz is frame 160000, and the samples count from 0.
        assert first_sample(cut) == 160000 % 1000
        with contextlib.closing(wave.open(str(cut), "rb")) as wav:
            assert wav.getnframes() == 160000
            assert wav.getframerate() == 16000
            assert wav.getnchannels() == 1
    finally:
        cut.unlink(missing_ok=True)


def test_no_end_means_the_rest_of_the_file(tmp_path):
    source = make_wav(tmp_path / "talk.wav", 30.0)
    cut, seconds = R.slice_wav(source, 20.0, None)
    try:
        assert seconds == pytest.approx(10.0)
    finally:
        cut.unlink(missing_ok=True)


def test_an_end_past_the_file_is_clamped(tmp_path):
    """`--end 30:27` against a file that is 30:20 is a typo, not a crash."""
    source = make_wav(tmp_path / "talk.wav", 30.0)
    cut, seconds = R.slice_wav(source, 25.0, 9999.0)
    try:
        assert seconds == pytest.approx(5.0)
    finally:
        cut.unlink(missing_ok=True)


def test_an_empty_window_is_refused(tmp_path):
    source = make_wav(tmp_path / "talk.wav", 30.0)
    with pytest.raises(SystemExit):
        R.slice_wav(source, 20.0, 10.0)
    with pytest.raises(SystemExit):
        R.slice_wav(source, 40.0, None)


def test_describe_reads_the_format_back(tmp_path):
    source = make_wav(tmp_path / "talk.wav", 12.5)
    info = R.describe(source)
    assert info["seconds"] == pytest.approx(12.5)
    assert info["rate"] == 16000
    assert info["channels"] == 1
    assert info["width"] == 2


# ---------------------------------------------------------------------------
# The guards before any money is spent
# ---------------------------------------------------------------------------


def test_a_dry_run_connects_to_nothing(tmp_path, monkeypatch, capsys):
    source = make_wav(tmp_path / "talk.wav", 120.0)
    monkeypatch.setattr(sys, "argv", [
        "replay_recording.py", str(source), "--start", "0:30",
        "--end", "1:30", "--dry-run",
    ])

    def forbidden(*args, **kwargs):
        raise AssertionError("a dry run must not stream anything")

    monkeypatch.setattr(R.asyncio, "run", forbidden)
    assert R.main() == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "1.00 min" in out


def test_a_file_that_is_not_wav_is_refused(tmp_path, monkeypatch):
    other = tmp_path / "talk.m4a"
    other.write_bytes(b"not a wav")
    monkeypatch.setattr(sys, "argv",
                        ["replay_recording.py", str(other), "--dry-run"])
    assert R.main() == 2


def test_the_wrong_sample_rate_is_refused(tmp_path, monkeypatch):
    """The engine is configured for 16 kHz mono; anything else would be
    streamed at the wrong speed and billed for."""
    source = make_wav(tmp_path / "talk.wav", 5.0, rate=44100)
    monkeypatch.setattr(sys, "argv",
                        ["replay_recording.py", str(source), "--dry-run"])
    assert R.main() == 2


def test_a_missing_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "replay_recording.py", str(tmp_path / "nope.wav"), "--dry-run"])
    assert R.main() == 2


def test_it_never_builds_a_transcript_recorder():
    """`transcripts/` is the operator's record of real evenings.

    `CaptionSession.start_live` is the only thing in the project that may
    build a recorder, and `tests/test_transcript.py` asserts that by reading
    `main.py`. This tool drives a `CaptionState` the same way a session does,
    so the same rule has to hold here.
    """
    text = Path(R.__file__).read_text(encoding="utf-8")
    assert "TranscriptRecorder" not in text
    assert "TRANSCRIPT_DIR" not in text
    assert "transcripts/" in text, "and it should say why"
