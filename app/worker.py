"""
A single aligner process, warm and isolated.

The aligner lives in its own process rather than a thread: Kaldi reports
fatal errors with abort(), which would take a uvicorn worker down with it,
and kalpy's thread-safety is undocumented. One process also means one copy
of the model in memory and natural serialization of requests, which is what
a queue consumer sends anyway.

Starting the pool queues a warm-up alignment, so the model is loading while
the server finishes booting, and again while a failure response goes out
after a restart.
"""

import math
import os
from pathlib import Path
from typing import Optional

from app.aligner import Alignment, AlignmentTimeout, LoadedAligner, Tokenized
from app.converter import get_audio_duration
from app.pool import WARMUP_TIMEOUT, WARMUP_WAV, SerialPool

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/opt/mfa-models"))
ACOUSTIC_DIR = MODEL_DIR / "acoustic" / "english_us_arpa"
DICTIONARY_PATH = MODEL_DIR / "dictionary" / "english_us_arpa.dict"
ALIGN_TIMEOUT = float(os.environ.get("ALIGN_TIMEOUT", "10"))
ALIGN_MARGIN = 0.5
WARMUP_TRANSCRIPT = "Hello there, world."

_ALIGNER: Optional[LoadedAligner] = None


class AlignerUnavailable(Exception):
    """The aligner process died or never finished loading; a fresh one is starting."""


class AlignerBusy(Exception):
    """The aligner did not get to the call in time; nothing was restarted."""


def _init(acoustic_dir: Path, dictionary_path: Path) -> None:
    global _ALIGNER
    _ALIGNER = LoadedAligner(acoustic_dir, dictionary_path)


def _align(
    wav_path: Path, duration: float, transcript: str, budget: float = math.inf
) -> Alignment:
    assert _ALIGNER is not None
    return _ALIGNER.align(wav_path, duration, transcript, budget)


def _tokenize(texts: list[str]) -> list[Tokenized]:
    assert _ALIGNER is not None
    return [_ALIGNER.tokenize(text) for text in texts]


class AlignerPool(SerialPool):
    name = "aligner"
    timeout_reason = "alignment_timeout"
    TimedOut = AlignmentTimeout
    Unavailable = AlignerUnavailable
    Busy = AlignerBusy

    def __init__(
        self,
        acoustic_dir: Path = ACOUSTIC_DIR,
        dictionary_path: Path = DICTIONARY_PATH,
        timeout: float = ALIGN_TIMEOUT,
        warmup_timeout: float = WARMUP_TIMEOUT,
    ) -> None:
        super().__init__(timeout, warmup_timeout)
        self.acoustic_dir = acoustic_dir
        self.dictionary_path = dictionary_path

    def _initializer(self):
        return _init, (self.acoustic_dir, self.dictionary_path)

    def _warmup(self):
        return _align, (WARMUP_WAV, get_audio_duration(WARMUP_WAV), WARMUP_TRANSCRIPT)

    async def align(self, wav_path: Path, duration: float, transcript: str) -> Alignment:
        """Align in the worker, which stops a slow decode itself just before the pool would restart it."""
        budget = self.timeout - ALIGN_MARGIN
        return await self._call(_align, wav_path, duration, transcript, budget)

    async def tokenize(self, texts: list[str]) -> list[Tokenized]:
        """Normalize each text the way an alignment would."""
        return await self._call(_tokenize, texts)
