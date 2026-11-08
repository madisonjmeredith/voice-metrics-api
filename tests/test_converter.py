import pytest

from app.converter import convert_to_wav, get_audio_duration
from tests.conftest import needs_ffmpeg


@needs_ffmpeg
def test_decoding_stops_at_max_seconds(vowel_a, tmp_path):
    wav = convert_to_wav(vowel_a, tmp_path / "out.wav", max_seconds=1.0)
    assert get_audio_duration(wav) == pytest.approx(1.0, abs=0.01)
