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

import asyncio
import multiprocessing
import os
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Optional

from app.aligner import Alignment, LoadedAligner
from app.converter import get_audio_duration

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/opt/mfa-models"))
ACOUSTIC_DIR = MODEL_DIR / "acoustic" / "english_us_arpa"
DICTIONARY_PATH = MODEL_DIR / "dictionary" / "english_us_arpa.dict"
ALIGN_TIMEOUT = float(os.environ.get("ALIGN_TIMEOUT", "10"))

WARMUP_WAV = Path(__file__).parent / "warmup.wav"
WARMUP_TRANSCRIPT = "Hello there, world."

_ALIGNER: Optional[LoadedAligner] = None


class AlignmentTimeout(Exception):
    """The aligner did not answer in time; it has been restarted."""


class AlignerUnavailable(Exception):
    """The aligner process died; a fresh one is starting."""


def _init(acoustic_dir: Path, dictionary_path: Path) -> None:
    global _ALIGNER
    _ALIGNER = LoadedAligner(acoustic_dir, dictionary_path)


def _align(wav_path: Path, duration: float, transcript: str) -> Alignment:
    assert _ALIGNER is not None
    return _ALIGNER.align(wav_path, duration, transcript)


class AlignerPool:
    def __init__(
        self,
        acoustic_dir: Path = ACOUSTIC_DIR,
        dictionary_path: Path = DICTIONARY_PATH,
        timeout: float = ALIGN_TIMEOUT,
    ) -> None:
        self.acoustic_dir = acoustic_dir
        self.dictionary_path = dictionary_path
        self.timeout = timeout
        self._pool: Optional[ProcessPoolExecutor] = None
        self._warmup: Optional[Future] = None

    def start(self) -> None:
        self._pool = ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init,
            initargs=(self.acoustic_dir, self.dictionary_path),
        )
        self._warmup = self._pool.submit(
            _align, WARMUP_WAV, get_audio_duration(WARMUP_WAV), WARMUP_TRANSCRIPT
        )

    async def wait_ready(self) -> None:
        """Block until the warm-up alignment finishes; raises if it failed."""
        await asyncio.wrap_future(self._warmup)

    @property
    def ready(self) -> bool:
        return (
            self._warmup is not None
            and self._warmup.done()
            and self._warmup.exception() is None
        )

    def restart(self) -> None:
        self.shutdown()
        self.start()

    def shutdown(self) -> None:
        if self._pool is not None:
            # Futures cannot interrupt a busy worker, so kill it outright.
            for process in list(self._pool._processes.values()):
                process.kill()
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    async def align(self, wav_path: Path, duration: float, transcript: str) -> Alignment:
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._pool, _align, wav_path, duration, transcript),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            self.restart()
            raise AlignmentTimeout(f"Alignment exceeded {self.timeout:.0f} s")
        except BrokenProcessPool as e:
            self.restart()
            raise AlignerUnavailable(str(e))
