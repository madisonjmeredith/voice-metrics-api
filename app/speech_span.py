"""
Where the speech in a clip starts and ends, so the aligner can leave out the quiet around it.

Silero VAD, as shipped with faster-whisper, is imported lazily so the API
process never loads onnxruntime.
"""

from typing import Optional

import numpy as np

from app.converter import SAMPLE_RATE

SPAN_PAD = 0.3
# faster-whisper's default is 2 s, which runs the last segment to the end of any shorter tail.
MIN_SILENCE_MS = 100


def speech_span(samples: np.ndarray) -> Optional[tuple[float, float]]:
    """From the first speech Silero hears to the last, padded by SPAN_PAD; None if it hears none."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(min_silence_duration_ms=MIN_SILENCE_MS, speech_pad_ms=0)
    segments = get_speech_timestamps(samples, options, sampling_rate=SAMPLE_RATE)
    if not segments:
        return None
    return (
        max(segments[0]["start"] / SAMPLE_RATE - SPAN_PAD, 0.0),
        min(segments[-1]["end"] / SAMPLE_RATE + SPAN_PAD, len(samples) / SAMPLE_RATE),
    )
