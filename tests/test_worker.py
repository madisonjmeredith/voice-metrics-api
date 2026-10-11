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

WAV = Path("unused.wav")
SPAWN_TIMEOUT = 60.0


def _no_init(acoustic_dir: Path, dictionary_path: Path) -> None:
    pass


def _fake_align(wav_path: Path, duration: float, transcript: str) -> str:
    if transcript == "hang":
        time.sleep(60)
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
def pool(monkeypatch):
    monkeypatch.setattr(worker, "_init", _no_init)
    monkeypatch.setattr(worker, "_align", _fake_align)
    monkeypatch.setattr(worker, "_tokenize", _fake_tokenize)
    pool = CountingPool(timeout=SPAWN_TIMEOUT)
    pool.start()
    pool._warmup.result(timeout=SPAWN_TIMEOUT)
    yield pool
    pool.shutdown()


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


def test_a_lexicon_check_that_times_out_restarts_nothing(pool):
    pool.timeout = 0.1
    with pytest.raises(AlignerBusy):
        asyncio.run(pool.tokenize(["hang"]))
    assert pool.restarts == 0
