"""
Praat-based pitch and formant measurement using parselmouth.

Adapted from formantry's analyzer. The analysis parameters are the ones the
reference project's textgrid-formants.praat used when it built stats.json,
and they are kept for parity with that data rather than tuned.
"""

import math
from pathlib import Path
from typing import Optional

import parselmouth

PITCH_FLOOR = 75.0
PITCH_CEILING = 600.0

# Burg formant settings from the reference Praat script:
# To Formant (burg)... 0 5 5000 0.025 50
MAX_FORMANTS = 5
FORMANT_CEILING = 5000.0
FORMANT_WINDOW = 0.025
PRE_EMPHASIS_FROM = 50.0

# F1-F3 are what stats.json describes; F4 and F5 are computed and ignored.
READ_FORMANTS = 3


def _finite(value: float) -> Optional[float]:
    """Convert Praat's NaN-for-undefined into None."""
    return None if math.isnan(value) else value


class AudioAnalyzer:
    """Pitch and formant tracks for one WAV, queried at arbitrary times."""

    def __init__(self, audio_path: Path) -> None:
        sound = parselmouth.Sound(str(audio_path))
        self._pitch = sound.to_pitch(
            time_step=None,
            pitch_floor=PITCH_FLOOR,
            pitch_ceiling=PITCH_CEILING,
        )
        self._formant = sound.to_formant_burg(
            time_step=None,
            max_number_of_formants=MAX_FORMANTS,
            maximum_formant=FORMANT_CEILING,
            window_length=FORMANT_WINDOW,
            pre_emphasis_from=PRE_EMPHASIS_FROM,
        )

    def get_pitch_at(self, time: float) -> Optional[float]:
        """F0 in Hz at `time`, or None where Praat found no voicing."""
        value = _finite(self._pitch.get_value_at_time(time))
        return None if value == 0 else value

    def get_formants_at(self, time: float) -> list[Optional[float]]:
        """[F1, F2, F3] in Hz at `time`, None where a formant is unmeasurable."""
        return [
            _finite(self._formant.get_value_at_time(i, time))
            for i in range(1, READ_FORMANTS + 1)
        ]
