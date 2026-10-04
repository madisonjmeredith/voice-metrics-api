import pytest

from tests.conftest import vowel_alignment


def test_speech_coverage_leaves_silence_out():
    alignment = vowel_alignment()
    assert alignment.speech_coverage(1.5) == pytest.approx(1.3 / 1.5)
    assert alignment.speech_coverage(13.0) == pytest.approx(0.1)
