"""
Praat measurement in a pool of worker processes.

parselmouth holds the GIL for a whole analysis, so in a thread it would stall
the event loop, and with it every aligner timeout, for as long as Praat runs.
Pitch and Formant objects can't be pickled, so the alignment goes to the
worker and the measured phones come back.
"""

import asyncio
import multiprocessing
import os
import time
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Optional

from app.aligner import Alignment
from app.analyzer import AudioAnalyzer
from app.measurement import MeasuredPhone, measure_phones
from app.telemetry import note, to_ms
from app.worker import WARMUP_WAV

ANALYZER_WORKERS = int(os.environ.get("ANALYZER_WORKERS", "2"))

NO_PHONES = Alignment(words=[], phones=[], oovs=[])


class AnalyzerUnavailable(Exception):
    """A Praat process died; a fresh pool is starting."""


def _warm_up() -> None:
    AudioAnalyzer(WARMUP_WAV)


def _measure(wav_path: Path, alignment: Alignment) -> list[MeasuredPhone]:
    return measure_phones(AudioAnalyzer(wav_path), alignment)


def _timed(fn, *args):
    started = time.perf_counter()
    return fn(*args), time.perf_counter() - started


class AnalyzerPool:
    def __init__(self, workers: int = ANALYZER_WORKERS) -> None:
        self.workers = workers
        self._pool: Optional[ProcessPoolExecutor] = None
        self._warmups: list[Future] = []

    def start(self) -> None:
        self._pool = ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_warm_up,
        )
        # Workers spawn on demand, so one task per worker is what starts them all at boot.
        self._warmups = [self._pool.submit(_warm_up) for _ in range(self.workers)]

    async def wait_ready(self) -> None:
        """Block until the warm-up passes finish; raises if one failed."""
        await asyncio.gather(*(asyncio.wrap_future(f) for f in self._warmups))

    def restart(self) -> None:
        self.shutdown()
        self.start()

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None

    async def measure(self, wav_path: Path, alignment: Alignment) -> list[MeasuredPhone]:
        """Read pitch and F1-F3 at the midpoint of each speech phone."""
        pool = self._pool
        started = time.perf_counter()
        try:
            phones, ran = await asyncio.wrap_future(pool.submit(_timed, _measure, wav_path, alignment))
        except BrokenProcessPool as e:
            if self._pool is pool:
                self.restart()
            raise AnalyzerUnavailable(str(e))
        # Calls queue inside the executor, so only the worker knows when its pass began.
        note(praat_wait_ms=to_ms(time.perf_counter() - started - ran), praat_ms=to_ms(ran))
        return phones
