"""
Speech recognition in a worker process of its own.

It runs beside the aligner rather than inside it, so a Kaldi abort() can't take
it down and a recognition never waits behind an alignment. Its calls queue the
way the aligner's do (see SerialPool).
"""

import os
from pathlib import Path
from typing import Optional

from app.pool import WARMUP_TIMEOUT, WARMUP_WAV, SerialPool
from app.recognizer import LoadedRecognizer

WHISPER_MODEL_DIR = Path(os.environ.get("WHISPER_MODEL_DIR", "/opt/whisper/tiny.en"))
RECOGNIZE_TIMEOUT = float(os.environ.get("RECOGNIZE_TIMEOUT", "10"))
RECOGNIZER_THREADS = int(os.environ.get("RECOGNIZER_THREADS", "2"))

_RECOGNIZER: Optional[LoadedRecognizer] = None


class RecognitionTimeout(Exception):
    """The recognition ran past the timeout once the worker picked it up; the worker has been restarted."""


class RecognizerUnavailable(Exception):
    """The recognizer process died or never finished loading; a fresh one is starting."""


class RecognizerBusy(Exception):
    """The recognizer did not get to the call in time; nothing was restarted."""


def _init(model_dir: Path, threads: int) -> None:
    global _RECOGNIZER
    _RECOGNIZER = LoadedRecognizer(model_dir, threads)


def _recognize(wav_path: Path) -> str:
    assert _RECOGNIZER is not None
    return _RECOGNIZER.recognize(wav_path)


class RecognizerPool(SerialPool):
    name = "recognizer"
    timeout_reason = "recognition_timeout"
    TimedOut = RecognitionTimeout
    Unavailable = RecognizerUnavailable
    Busy = RecognizerBusy

    def __init__(
        self,
        model_dir: Path = WHISPER_MODEL_DIR,
        threads: int = RECOGNIZER_THREADS,
        timeout: float = RECOGNIZE_TIMEOUT,
        warmup_timeout: float = WARMUP_TIMEOUT,
    ) -> None:
        super().__init__(timeout, warmup_timeout)
        self.model_dir = model_dir
        self.threads = threads

    def _initializer(self):
        return _init, (self.model_dir, self.threads)

    def _warmup(self):
        return _recognize, (WARMUP_WAV,)

    async def recognize(self, wav_path: Path) -> str:
        """What the recognizer heard in the clip."""
        return await self._call(_recognize, wav_path)
