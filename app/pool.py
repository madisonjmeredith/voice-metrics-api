"""
A process pool that runs a warm-up on every start and logs its loads and restarts.

The aligner and the Praat workers both build on it. A line about a dead worker
carries its exit code: -6 is abort(), -9 a kill, -11 a segfault.
"""

import asyncio
import logging
import os
import time
from concurrent.futures import Future, ProcessPoolExecutor
from functools import partial
from pathlib import Path
from typing import Optional

WARMUP_TIMEOUT = float(os.environ.get("WARMUP_TIMEOUT", "60"))
WARMUP_WAV = Path(__file__).parent / "warmup.wav"

logger = logging.getLogger(__name__)


def _failed(future: Future) -> bool:
    return future.done() and not future.cancelled() and future.exception() is not None


class WarmPool:
    name = "pool"

    def __init__(self, warmup_timeout: float = WARMUP_TIMEOUT) -> None:
        self.warmup_timeout = warmup_timeout
        self._pool: Optional[ProcessPoolExecutor] = None
        self._warmups: list[Future] = []
        self._started_at = 0.0

    def _create_executor(self) -> ProcessPoolExecutor:
        raise NotImplementedError

    def _submit_warmups(self, pool: ProcessPoolExecutor) -> list[Future]:
        raise NotImplementedError

    def start(self) -> None:
        self._started_at = time.monotonic()
        pool = self._pool = self._create_executor()
        self._warmups = self._submit_warmups(pool)
        for future in self._warmups:
            future.add_done_callback(partial(self._log_load, pool))

    async def wait_ready(self) -> None:
        """Block until every warm-up passes; raises if one failed or the load outlasts warmup_timeout."""
        try:
            async with asyncio.timeout(self._started_at + self.warmup_timeout - time.monotonic()):
                await asyncio.gather(*(asyncio.wrap_future(f) for f in self._warmups))
        except TimeoutError:
            logger.error("%s did not load within %g s", self.name, self.warmup_timeout)
            raise

    def restart(self) -> None:
        self.shutdown()
        self.start()

    def shutdown(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            # Futures cannot interrupt a busy worker, so kill it outright.
            for process in list(pool._processes.values()):
                process.kill()
            # No cancel_futures: it races the kill and surfaces queued calls as CancelledError.
            pool.shutdown(wait=False)

    def _restart_if_current(self, pool: Optional[ProcessPoolExecutor], reason: str) -> None:
        if self._pool is pool:
            uptime = time.monotonic() - self._started_at
            logger.warning(
                "%s restart reason=%s%s uptime_s=%.1f", self.name, reason, _exited(pool), uptime
            )
            self.restart()

    def _log_load(self, pool: ProcessPoolExecutor, future: Future) -> None:
        # The executor's thread sets results serially, so only one callback logs the outcome.
        if future.cancelled() or self._pool is not pool:
            return
        failed = [f for f in self._warmups if _failed(f)]
        if failed == [future]:
            error = future.exception()
            logger.error(
                "%s load failed%s: %s: %s", self.name, _exited(pool), type(error).__name__, error
            )
        elif not failed and all(f.done() for f in self._warmups):
            logger.info("%s ready load_s=%.1f", self.name, time.monotonic() - self._started_at)


def _exited(pool: ProcessPoolExecutor) -> str:
    codes = [str(p.exitcode) for p in pool._processes.values() if p.exitcode is not None]
    return f" exitcode={','.join(codes)}" if codes else ""
