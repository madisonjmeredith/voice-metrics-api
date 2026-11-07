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

from app.aligner import Alignment
from app.analyzer import AudioAnalyzer
from app.measurement import MeasuredPhone, measure_phones
from app.pool import WARMUP_TIMEOUT, WARMUP_WAV, WarmPool
from app.telemetry import note, to_ms

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


class AnalyzerPool(WarmPool):
    name = "analyzer"

    def __init__(
        self, workers: int = ANALYZER_WORKERS, warmup_timeout: float = WARMUP_TIMEOUT
    ) -> None:
        super().__init__(warmup_timeout)
        self.workers = workers

    def _create_executor(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_warm_up,
        )

    def _submit_warmups(self, pool: ProcessPoolExecutor) -> list[Future]:
        # Workers spawn on demand, so one task per worker is what starts them all at boot.
        return [pool.submit(_warm_up) for _ in range(self.workers)]

    async def measure(self, wav_path: Path, alignment: Alignment) -> list[MeasuredPhone]:
        """Read pitch and F1-F3 at the midpoint of each speech phone."""
        pool = self._pool
        started = time.perf_counter()
        try:
            phones, ran = await asyncio.wrap_future(pool.submit(_timed, _measure, wav_path, alignment))
        except BrokenProcessPool as e:
            self._restart_if_current(pool, "worker_lost")
            raise AnalyzerUnavailable(str(e))
        # Calls queue inside the executor, so only the worker knows when its pass began.
        note(praat_wait_ms=to_ms(time.perf_counter() - started - ran), praat_ms=to_ms(ran))
        return phones
