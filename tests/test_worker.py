"""
AlignerPool's restart rules, exercised over a worker with no aligner in it.

The stand-ins below run in the spawned worker, so they have to be importable
by name from there.
"""

import asyncio
import time
from pathlib import Path

import pytest

import app.worker as worker
from app.worker import AlignerBusy, AlignerPool, AlignerUnavailable, AlignmentTimeout
from tests.conftest import noted

WAV = Path("unused.wav")
SPAWN_TIMEOUT = 60.0
SLOW = 1.5


def _no_init(acoustic_dir: Path, dictionary_path: Path) -> None:
    pass


def _fake_align(wav_path: Path, duration: float, transcript: str) -> str:
    if transcript == "hang":
        time.sleep(60)
    if transcript == "slow":
        time.sleep(SLOW)
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
    pool._warmup.result(timeout=SPAWN_TIMEOUT)
    return pool


def test_calls_queued_on_a_worker_that_dies_restart_it_once(pool):
    async def scenario():
        calls = [asyncio.create_task(pool.align(WAV, 0.0, "hang")) for _ in range(4)]
        await asyncio.sleep(0.1)
        for process in list(pool._pool._processes.values()):
            process.kill()
        results = await asyncio.gather(*calls, return_exceptions=True)
        return results, await pool.align(WAV, 0.0, "ready")

    results, after = asyncio.run(scenario())

    assert [type(r) for r in results] == [AlignerUnavailable] * 4
    assert pool.restarts == 1
    assert after == "ready"


def test_calls_queued_behind_a_timeout_fail_without_a_second_restart(pool):
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


def test_a_reload_that_hangs_is_restarted(start_pool):
    pool = start_pool(warmup="hang", timeout=0.2, warmup_timeout=1.0)

    async def scenario():
        with pytest.raises(AlignerBusy):
            await pool.align(WAV, 0.0, "ready")
        await asyncio.sleep(1.0)
        with pytest.raises(AlignerUnavailable):
            await pool.align(WAV, 0.0, "ready")

    asyncio.run(scenario())
    assert pool.restarts == 1
