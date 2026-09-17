"""
One F0/F1-F3 reading per aligned phone, taken at the phone's midpoint.

This is the measurement contract of the reference project's
textgrid-formants.praat: the population statistics it produced describe
midpoint readings, so a midpoint reading is what gets compared to them.
"""

from dataclasses import dataclass
from typing import Optional

from app.aligner import Alignment, PhoneSpan
from app.analyzer import AudioAnalyzer


@dataclass
class MeasuredPhone:
    span: PhoneSpan
    # [F0, F1, F2, F3], None where unmeasurable. Index-aligned with stats.json.
    F: list[Optional[float]]
    outlier: bool = False
    z: Optional[list[Optional[float]]] = None
    resonance: Optional[float] = None

    @property
    def label(self) -> str:
        return self.span.label


def measure_phones(analyzer: AudioAnalyzer, alignment: Alignment) -> list[MeasuredPhone]:
    return [
        MeasuredPhone(
            span=phone,
            F=[analyzer.get_pitch_at(phone.midpoint), *analyzer.get_formants_at(phone.midpoint)],
        )
        for phone in alignment.speech_phones
    ]
