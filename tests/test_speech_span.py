"""
The speech span over real Silero, which faster-whisper ships, so these run without MFA.
"""

import pytest

from app.converter import get_audio_duration, read_samples
from app.speech_span import speech_span
from tests.conftest import HELLO_LEAD_IN, HELLO_WAV


def test_the_span_leaves_out_a_low_level_lead_in_and_the_quiet_after(hello_after_lead_in):
    speech_end = HELLO_LEAD_IN + get_audio_duration(HELLO_WAV)

    start, end = speech_span(read_samples(hello_after_lead_in))

    assert 1.0 < start <= HELLO_LEAD_IN
    assert speech_end <= end < get_audio_duration(hello_after_lead_in)


def test_a_clip_that_is_all_speech_is_one_span():
    assert speech_span(read_samples(HELLO_WAV)) == (0.0, get_audio_duration(HELLO_WAV))


@pytest.mark.parametrize("audio", ["silence", "hiss", "tone"])
def test_audio_without_speech_has_no_span(request, audio):
    assert speech_span(read_samples(request.getfixturevalue(audio))) is None
