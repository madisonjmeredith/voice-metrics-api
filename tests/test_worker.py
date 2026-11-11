"""
AlignerPool's restart rules, exercised over a worker with no aligner in it.

The stand-ins below run in the spawned worker, so they have to be importable
by name from there.
"""

import asyncio
import gc
import logging
import re
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

import app.worker as worker
from app.worker import AlignerBusy, AlignerPool, AlignerUnavailable, AlignmentTimeout
from tests.conftest import eventually, kill_workers, noted, pool_lines

WAV = Path("unused.wav")
SPAWN_TIMEOUT = 60.0
SLOW = 1.5
BRIEF = 0.2


def _no_init(acoustic_dir: Path, dictionary_path: Path) -> None:
    pass


def _fake_align(wav_path: Path, duration: float, transcript: str, budget=None) -> object:
    if transcript == "fail":
        raise RuntimeError("bad dictionary")
    if transcript == "stopped":
        raise AlignmentTimeout("The alignment ran out of time and was stopped")
    if transcript == "budget":
        return budget
    if transcript == "hang":
        time.sleep(60)
    if transcript == "slow":
        time.sleep(SLOW)
    if transcript == "brief":
        time.sleep(BRIEF)
    if transcript.startswith("touch:"):
        Path(transcript.removeprefix("touch:")).touch()
    return transcript


def _fake_tokenize(texts: list[str]) -> list[str]:
    if texts == ["hang"]:
        time.sleep(60)
    return texts


class CountingPool(AlignerPool):
    restarts = 0

    def restart(self) -> None:
        self.restarts += 1
        super().restart()


@pytest.fixture
def start_pool(monkeypatch, request):
    monkeypatch.setattr(worker, "_init", _no_init)
    monkeypatch.setattr(worker, "_align", _fake_align)
    monkeypatch.setattr(worker, "_tokenize", _fake_tokenize)

    def start(
        warmup: str = worker.WARMUP_TRANSCRIPT, timeout: float = SPAWN_TIMEOUT, **kwargs
    ) -> CountingPool:
        monkeypatch.setattr(worker, "WARMUP_TRANSCRIPT", warmup)
        pool = CountingPool(timeout=timeout, **kwargs)
        pool.start()
        request.addfinalizer(pool.shutdown)
        return pool

    return start


@pytest.fixture
def pool(start_pool):
    pool = start_pool()
    pool._warmups[0].result(timeout=SPAWN_TIMEOUT)
    return pool


def test_calls_queued_on_a_worker_that_dies_restart_it_once(pool, caplog):
    async def scenario():
        calls = [asyncio.create_task(pool.align(WAV, 0.0, "hang")) for _ in range(4)]
        await asyncio.sleep(0.1)
        kill_workers(pool)
        results = await asyncio.gather(*calls, return_exceptions=True)
        return results, await pool.align(WAV, 0.0, "ready")

    results, after = asyncio.run(scenario())

    assert [type(r) for r in results] == [AlignerUnavailable] * 4
    assert pool.restarts == 1
    assert after == "ready"
    [line] = pool_lines(caplog)
    assert re.fullmatch(r"aligner restart reason=worker_lost exitcode=-9 uptime_s=\d+\.\d", line)


def test_calls_queued_behind_a_timeout_fail_without_a_second_restart(pool, caplog):
    async def scenario():
        pool.timeout = 0.5
        hung = asyncio.create_task(pool.align(WAV, 0.0, "hang"))
        await asyncio.sleep(0.1)
        # The hung call has already taken its timeout; these must outlast it.
        pool.timeout = SPAWN_TIMEOUT
        queued = [asyncio.create_task(pool.align(WAV, 0.0, "ready")) for _ in range(3)]
        queued += [asyncio.create_task(pool.tokenize(["ready"])) for _ in range(2)]
        results = await asyncio.gather(hung, *queued, return_exceptions=True)
        return results, await pool.align(WAV, 0.0, "ready")

    results, after = asyncio.run(scenario())

    assert [type(r) for r in results] == [AlignmentTimeout] + [AlignerUnavailable] * 5
    assert pool.restarts == 1
    assert after == "ready"
    [line] = pool_lines(caplog)
    assert re.fullmatch(r"aligner restart reason=alignment_timeout uptime_s=\d+\.\d", line)


def test_an_alignment_the_worker_stops_itself_restarts_nothing(pool, caplog):
    async def scenario():
        with pytest.raises(AlignmentTimeout):
            await pool.align(WAV, 0.0, "stopped")
        return await pool.align(WAV, 0.0, "ready")

    assert asyncio.run(scenario()) == "ready"
    assert pool.restarts == 0
    assert pool_lines(caplog) == []


def test_the_worker_stops_an_alignment_before_the_pool_would(pool):
    pool.timeout = 2.0
    budget = asyncio.run(pool.align(WAV, 0.0, "budget"))
    assert 0 < budget < pool.timeout


def test_a_lexicon_check_that_runs_too_long_restarts_the_worker(pool):
    pool.timeout = 0.1
    with pytest.raises(AlignmentTimeout):
        asyncio.run(pool.tokenize(["hang"]))
    assert pool.restarts == 1


def test_the_timeout_starts_when_the_worker_picks_up_the_call(pool):
    async def scenario():
        pool.timeout = SLOW + 1.0
        return await asyncio.gather(
            noted(pool.align(WAV, 0.0, "slow")), noted(pool.align(WAV, 0.0, "slow"))
        )

    (first, waited_less), (second, waited_more) = asyncio.run(scenario())
    assert [first, second] == ["slow", "slow"]
    assert pool.restarts == 0
    assert waited_less["aligner_wait_ms"] < SLOW * 1000 <= waited_more["aligner_wait_ms"]
    assert waited_less["aligner_ms"] >= SLOW * 1000 and waited_more["aligner_ms"] >= SLOW * 1000


def test_a_caller_that_leaves_mid_run_keeps_the_turn_until_the_worker_is_free(pool):
    async def scenario():
        pool.timeout = SLOW + 1.0
        left = asyncio.create_task(pool.align(WAV, 0.0, "slow"))
        await asyncio.sleep(0.3)
        left.cancel()
        return left, await noted(pool.align(WAV, 0.0, "brief"))

    left, (result, fields) = asyncio.run(scenario())
    assert left.cancelled()
    assert result == "brief"
    assert pool.restarts == 0
    assert fields["aligner_wait_ms"] >= (SLOW - 0.3) * 1000 * 0.8
    assert fields["aligner_ms"] < (BRIEF + 0.5) * 1000


def test_a_hung_call_its_caller_left_still_restarts_the_worker(pool, caplog):
    async def scenario():
        pool.timeout = 0.5
        left = asyncio.create_task(pool.align(WAV, 0.0, "hang"))
        await asyncio.sleep(0.1)
        left.cancel()
        pool.timeout = SPAWN_TIMEOUT
        await asyncio.sleep(1.0)
        return await pool.align(WAV, 0.0, "ready")

    assert asyncio.run(scenario()) == "ready"
    gc.collect()
    assert pool.restarts == 1
    [line] = pool_lines(caplog)
    assert line.startswith("aligner restart reason=alignment_timeout ")
    assert pool_lines(caplog, logging.ERROR, logger="asyncio") == []


def test_a_caller_that_leaves_while_queued_never_reaches_the_worker(pool, tmp_path):
    marker = tmp_path / "reached"

    async def scenario():
        ahead = asyncio.create_task(pool.align(WAV, 0.0, "brief"))
        await asyncio.sleep(0.05)
        left = asyncio.create_task(pool.align(WAV, 0.0, f"touch:{marker}"))
        await asyncio.sleep(0.05)
        left.cancel()
        await ahead
        return await pool.align(WAV, 0.0, "ready")

    assert asyncio.run(scenario()) == "ready"
    assert not marker.exists()
    assert pool.restarts == 0


@pytest.mark.parametrize(
    "call, ready",
    [
        (lambda pool, text: pool.align(WAV, 0.0, text), "ready"),
        (lambda pool, text: pool.tokenize([text]), ["ready"]),
    ],
    ids=["align", "tokenize"],
)
def test_a_call_that_gives_up_on_a_reload_never_reaches_the_worker(start_pool, call, ready):
    pool = start_pool(warmup="slow", timeout=0.5)

    async def scenario():
        with pytest.raises(AlignerBusy):
            await call(pool, "hang")
        await pool.wait_ready()
        return await call(pool, "ready")

    assert asyncio.run(scenario()) == ready
    assert pool.restarts == 0


def test_a_reload_that_hangs_is_restarted(start_pool, caplog):
    pool = start_pool(warmup="hang", timeout=0.2, warmup_timeout=1.0)

    async def scenario():
        with pytest.raises(AlignerBusy):
            await pool.align(WAV, 0.0, "ready")
        await asyncio.sleep(1.0)
        with pytest.raises(AlignerUnavailable):
            await pool.align(WAV, 0.0, "ready")

    asyncio.run(scenario())
    assert pool.restarts == 1
    [line] = pool_lines(caplog)
    assert line.startswith("aligner restart reason=warmup_timeout uptime_s=")
    assert pool_lines(caplog, logging.ERROR) == []


def test_a_load_is_logged_with_its_time(start_pool, caplog):
    caplog.set_level(logging.INFO)
    pool = start_pool(warmup="brief")
    asyncio.run(pool.wait_ready())

    [line] = pool_lines(caplog, logging.INFO)
    assert re.fullmatch(r"aligner ready load_s=\d+\.\d", line)
    assert float(line.split("=")[1]) >= BRIEF


def test_a_load_that_fails_is_logged(start_pool, caplog):
    pool = start_pool(warmup="fail")
    with pytest.raises(RuntimeError):
        asyncio.run(pool.wait_ready())

    assert pool_lines(caplog, logging.ERROR) == ["aligner load failed: RuntimeError: bad dictionary"]
    assert pool.state == "down"


def test_a_worker_lost_during_the_load_is_logged_with_its_exit_code(start_pool, caplog):
    pool = start_pool(warmup="hang")

    async def scenario():
        await asyncio.sleep(0.1)
        kill_workers(pool)
        await pool.wait_ready()

    with pytest.raises(BrokenProcessPool):
        asyncio.run(scenario())

    [line] = pool_lines(caplog, logging.ERROR)
    assert line.startswith("aligner load failed exitcode=-9: BrokenProcessPool: ")


def test_a_load_that_outlasts_the_warm_up_timeout_is_logged(start_pool, caplog):
    pool = start_pool(warmup="hang", warmup_timeout=0.3)
    assert pool.state == "warming"
    with pytest.raises(TimeoutError):
        asyncio.run(pool.wait_ready())

    assert pool_lines(caplog, logging.ERROR) == ["aligner did not load within 0.3 s"]
    assert pool.state == "stalled"


def test_the_state_follows_the_load_and_the_worker(start_pool):
    pool = start_pool(warmup="brief")
    assert pool.state == "warming"
    asyncio.run(pool.wait_ready())
    assert pool.state == "ready"

    kill_workers(pool)
    eventually(lambda: pool.state == "down")
    with pytest.raises(AlignerUnavailable):
        asyncio.run(pool.align(WAV, 0.0, "ready"))
    assert pool.state == "warming"

