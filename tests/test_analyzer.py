import numpy as np
import parselmouth
import pytest

from app.analyzer import PITCH_CEILING, PITCH_FLOOR, AudioAnalyzer
from tests.conftest import VOWEL_A, pulse_vowel, vowel_signal, write_wav


def plain_reads(path, times) -> list[float]:
    """What a single 75-600 Hz pass reads, as the reference project's script does."""
    pitch = parselmouth.Sound(str(path)).to_pitch(
        time_step=None, pitch_floor=PITCH_FLOOR, pitch_ceiling=PITCH_CEILING
    )
    return [pitch.get_value_at_time(t) for t in times]


@pytest.fixture(scope="module")
def doubled_tail(audio_dir):
    """A 200 Hz /a/ whose last 0.5 s is period-doubled, which a 75 Hz floor reads an octave low."""
    return write_wav(audio_dir / "doubled_tail.wav", pulse_vowel(1.5, lambda t: 200.0, doubled_from=1.0))


@pytest.fixture(scope="module")
def glide(audio_dir):
    """A 200 Hz /a/ that falls to 125 Hz over its second half."""
    def f0(t: float) -> float:
        return 200.0 if t < 0.75 else 200.0 - 75.0 * (t - 0.75) / 0.75

    return write_wav(audio_dir / "glide.wav", pulse_vowel(1.5, f0))


def test_octave_low_readings_are_reread(doubled_tail):
    tail = np.arange(1.1, 1.45, 0.025)
    assert plain_reads(doubled_tail, tail) == pytest.approx([100.0] * len(tail), rel=0.05)

    analyzer = AudioAnalyzer(doubled_tail)
    readings = [analyzer.get_pitch_at(t) for t in tail]

    assert readings == pytest.approx([200.0] * len(tail), rel=0.05)


def test_in_range_readings_keep_the_first_pass_value(doubled_tail):
    head = np.arange(0.1, 0.9, 0.025)
    analyzer = AudioAnalyzer(doubled_tail)

    assert [analyzer.get_pitch_at(t) for t in head] == plain_reads(doubled_tail, head)


def test_a_real_dip_is_not_reread(glide):
    times = np.arange(0.05, 1.45, 0.025)
    analyzer = AudioAnalyzer(glide)
    readings = [analyzer.get_pitch_at(t) for t in times]

    assert readings == plain_reads(glide, times)
    assert readings[-1] == pytest.approx(130.0, abs=5.0)


def test_a_low_voice_keeps_the_wide_range(audio_dir):
    path = write_wav(audio_dir / "a_100.wav", vowel_signal(100.0, VOWEL_A, 1.5))
    analyzer = AudioAnalyzer(path)

    assert analyzer.pitch_floor == PITCH_FLOOR
    assert analyzer.get_pitch_at(0.75) == pytest.approx(100.0, abs=3.0)


def test_silence_keeps_the_wide_range(silence):
    analyzer = AudioAnalyzer(silence)

    assert analyzer.pitch_floor == PITCH_FLOOR
    assert analyzer.get_pitch_at(1.0) is None
