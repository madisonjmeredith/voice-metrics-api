"""
A process pool that runs a warm-up on every start and logs its loads and restarts.

The aligner and the Praat workers both build on it, and the aligner on
SerialPool's queue. A line about a dead worker carries its exit code: -6 is
abort(), -9 a kill, -11 a segfault.
"""

import asyncio
import logging
import multiprocessing
import multiprocessing.connection
import os
import time
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Callable, Optional

from app.telemetry import timed

WARMUP_TIMEOUT = float(os.environ.get("WARMUP_TIMEOUT", "60"))
WARMUP_WAV = Path(__file__).parent / "warmup.wav"

logger = logging.getLogger(__name__)


class PoolState(StrEnum):
    READY = "ready"
    WARMING = "warming"
    STALLED = "stalled"
    DOWN = "down"

    @property
    def live(self) -> bool:
        """False for the states a pool leaves only when a call arrives and restarts it."""
        return self not in (PoolState.STALLED, PoolState.DOWN)


def _failed(future: Future) -> bool:
    return future.done() and not future.cancelled() and future.exception() is not None


def retrieve(task: asyncio.Future) -> None:
    """Mark a task's exception as seen, so one nobody awaits isn't logged as never retrieved."""
    if not task.cancelled():
        task.exception()


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

    @property
    def state(self) -> PoolState:
        """STALLED is still loading past warmup_timeout; DOWN is a failed load or a dead worker."""
        pool = self._pool
        if pool is None or any(map(_failed, self._warmups)) or _dead_workers(pool):
            return PoolState.DOWN
        if self._load_overdue(self._warmups, self._started_at):
            return PoolState.STALLED
        if not all(f.done() for f in self._warmups):
            return PoolState.WARMING
        return PoolState.READY

    def _load_overdue(self, warmups: list[Future], started_at: float) -> bool:
        unfinished = not all(f.done() for f in warmups)
        return unfinished and time.monotonic() - started_at > self.warmup_timeout

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


class SerialPool(WarmPool):
    """One worker whose calls queue in this process, each timed once to reach its turn and once to run."""

    Busy: type[Exception]
    Unavailable: type[Exception]
    TimedOut: type[Exception]
    timeout_reason: str

    def __init__(self, timeout: float, warmup_timeout: float = WARMUP_TIMEOUT) -> None:
        super().__init__(warmup_timeout)
        self.timeout = timeout
        self._turn = asyncio.Lock()

    def _initializer(self) -> tuple[Callable, tuple]:
        raise NotImplementedError

    def _warmup(self) -> tuple[Callable, tuple]:
        raise NotImplementedError

    def _create_executor(self) -> ProcessPoolExecutor:
        initializer, initargs = self._initializer()
        return ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=initializer,
            initargs=initargs,
        )

    def _submit_warmups(self, pool: ProcessPoolExecutor) -> list[Future]:
        fn, args = self._warmup()
        return [pool.submit(fn, *args)]

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
        """Run fn in the worker; a caller that leaves mid-run leaves the call timed and holding the turn."""
        pool, [warmup], started_at = self._pool, self._warmups, self._started_at
        with timed(f"{self.name}_wait"):
            reached = await self._wait_for_turn(warmup)
        if not reached:
            if self._load_overdue([warmup], started_at):
                self._restart_if_current(pool, "warmup_timeout")
                raise self.Unavailable(
                    f"The {self.name} did not load within {self.warmup_timeout:.0f} s"
                )
            raise self.Busy(f"The {self.name} did not reach the call within {self.timeout:.0f} s")
        run = asyncio.create_task(self._run(pool, fn, args))
        run.add_done_callback(retrieve)
        return await asyncio.shield(run)

    async def _run(self, pool: Optional[ProcessPoolExecutor], fn, args):
        try:
            if self._pool is not pool:
                raise self.Unavailable(f"The {self.name} restarted while the call waited")
            future = pool.submit(fn, *args)
            with timed(self.name):
                return await asyncio.wait_for(asyncio.wrap_future(future), timeout=self.timeout)
        except asyncio.TimeoutError:
            self._restart_if_current(pool, self.timeout_reason)
            raise self.TimedOut(f"The call ran longer than {self.timeout:.0f} s")
        except BrokenProcessPool as e:
            self._restart_if_current(pool, "worker_lost")
            raise self.Unavailable(str(e))
        finally:
            self._turn.release()


def _dead_workers(pool: ProcessPoolExecutor) -> list[multiprocessing.Process]:
    workers = {p.sentinel: p for p in pool._processes.values()}
    return [workers[s] for s in multiprocessing.connection.wait(list(workers), timeout=0)]


def _exit_code(worker: multiprocessing.Process, deadline: float) -> Optional[int]:
    # The executor's thread may be mid-reap, when exitcode reads None until that thread records it.
    while worker.exitcode is None and time.monotonic() < deadline:
        time.sleep(0.005)
    return worker.exitcode


def _exited(pool: ProcessPoolExecutor) -> str:
    deadline = time.monotonic() + 0.1
    codes = [_exit_code(worker, deadline) for worker in _dead_workers(pool)]
    return f" exitcode={','.join(map(str, codes))}" if codes else ""
