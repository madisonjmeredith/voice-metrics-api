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
import time
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Optional

from app.aligner import Alignment, LoadedAligner, Tokenized
from app.converter import get_audio_duration
from app.pool import WARMUP_TIMEOUT, WARMUP_WAV, WarmPool
from app.telemetry import timed

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/opt/mfa-models"))
ACOUSTIC_DIR = MODEL_DIR / "acoustic" / "english_us_arpa"
DICTIONARY_PATH = MODEL_DIR / "dictionary" / "english_us_arpa.dict"
ALIGN_TIMEOUT = float(os.environ.get("ALIGN_TIMEOUT", "10"))
WARMUP_TRANSCRIPT = "Hello there, world."

_ALIGNER: Optional[LoadedAligner] = None


class AlignmentTimeout(Exception):
    """The call ran past the timeout once the worker picked it up; the worker has been restarted."""


class AlignerUnavailable(Exception):
    """The aligner process died or never finished loading; a fresh one is starting."""


class AlignerBusy(Exception):
    """The aligner did not get to the call in time; nothing was restarted."""


def _init(acoustic_dir: Path, dictionary_path: Path) -> None:
    global _ALIGNER
    _ALIGNER = LoadedAligner(acoustic_dir, dictionary_path)


def _align(wav_path: Path, duration: float, transcript: str) -> Alignment:
    assert _ALIGNER is not None
    return _ALIGNER.align(wav_path, duration, transcript)


def _tokenize(texts: list[str]) -> list[Tokenized]:
    assert _ALIGNER is not None
    return [_ALIGNER.tokenize(text) for text in texts]


class AlignerPool(WarmPool):
    name = "aligner"

    def __init__(
        self,
        acoustic_dir: Path = ACOUSTIC_DIR,
        dictionary_path: Path = DICTIONARY_PATH,
        timeout: float = ALIGN_TIMEOUT,
        warmup_timeout: float = WARMUP_TIMEOUT,
    ) -> None:
        super().__init__(warmup_timeout)
        self.acoustic_dir = acoustic_dir
        self.dictionary_path = dictionary_path
        self.timeout = timeout
        self._turn = asyncio.Lock()

    def _create_executor(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init,
            initargs=(self.acoustic_dir, self.dictionary_path),
        )

    def _submit_warmups(self, pool: ProcessPoolExecutor) -> list[Future]:
        return [pool.submit(_align, WARMUP_WAV, get_audio_duration(WARMUP_WAV), WARMUP_TRANSCRIPT)]

    async def _wait_for_turn(self, warmup: Future) -> bool:
        """Wait out the warm-up and the calls ahead, then take the turn; False if time runs out."""
        loaded = asyncio.gather(asyncio.wrap_future(warmup), return_exceptions=True)
        try:
            async with asyncio.timeout(self.timeout):
                # Shielded so a timeout can't cancel the warm-up through wrap_future.
                await asyncio.shield(loaded)
                await self._turn.acquire()
        except asyncio.TimeoutError:
            return False
        return True

    async def _call(self, fn, *args):
        """Run fn in the worker, with separate timeouts for reaching the call and for running it."""
        pool, [warmup], started_at = self._pool, self._warmups, self._started_at
        with timed("aligner_wait"):
            reached = await self._wait_for_turn(warmup)
        if not reached:
            if self._load_overdue([warmup], started_at):
                self._restart_if_current(pool, "warmup_timeout")
                raise AlignerUnavailable(
                    f"The aligner did not load within {self.warmup_timeout:.0f} s"
                )
            raise AlignerBusy(f"The aligner did not reach the call within {self.timeout:.0f} s")
        try:
            if self._pool is not pool:
                raise AlignerUnavailable("The aligner restarted while the call waited")
            future = pool.submit(fn, *args)
            with timed("aligner"):
                return await asyncio.wait_for(asyncio.wrap_future(future), timeout=self.timeout)
        except asyncio.TimeoutError:
            self._restart_if_current(pool, "alignment_timeout")
            raise AlignmentTimeout(f"The call ran longer than {self.timeout:.0f} s")
        except BrokenProcessPool as e:
            self._restart_if_current(pool, "worker_lost")
            raise AlignerUnavailable(str(e))
        finally:
            self._turn.release()

    async def align(self, wav_path: Path, duration: float, transcript: str) -> Alignment:
        return await self._call(_align, wav_path, duration, transcript)

    async def tokenize(self, texts: list[str]) -> list[Tokenized]:
        """Normalize each text the way an alignment would."""
        return await self._call(_tokenize, texts)
