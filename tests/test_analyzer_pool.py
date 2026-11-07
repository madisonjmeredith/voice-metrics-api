"""
AnalyzerPool over real Praat, plus its restart rule over a stand-in.

The stand-in runs in the spawned worker, so it has to be importable by name
from there.
"""

import asyncio
import time
from pathlib import Path

import numpy as np
import pytest

import app.analyzer_pool as analyzer_pool
from app.analyzer import AudioAnalyzer
from app.analyzer_pool import NO_PHONES, AnalyzerPool, AnalyzerUnavailable
from app.measurement import measure_phones
from tests.conftest import FS, noted, vowel_alignment, write_wav

HANG = Path("hang.wav")
SLOW = Path("slow.wav")
READY = Path("ready.wav")
SLOW_PASS = 1.0


def _fake_measure(wav_path: Path, alignment) -> list:
    if wav_path == HANG:
        time.sleep(60)
    if wav_path == SLOW:
        time.sleep(SLOW_PASS)
    return []


class CountingPool(AnalyzerPool):
    restarts = 0

    def restart(self) -> None:
        self.restarts += 1
        super().restart()


@pytest.fixture
def start_pool(request):
    def start(**kwargs) -> CountingPool:
        pool = CountingPool(**kwargs)
        pool.start()
        request.addfinalizer(pool.shutdown)
        return pool

    return start


def test_the_pool_measures_what_an_in_process_pass_does(start_pool, vowel_a):
    pool = start_pool()
    alignment = vowel_alignment()

    async def scenario():
        await pool.wait_ready()
        return await pool.measure(vowel_a, alignment)

    assert asyncio.run(scenario()) == measure_phones(AudioAnalyzer(vowel_a), alignment)


def test_the_event_loop_keeps_running_through_a_praat_pass(start_pool, tmp_path):
    t = np.arange(int(FS * 58.8)) / FS
    noise = np.random.default_rng(0).standard_normal(t.size)
    wav = write_wav(tmp_path / "long.wav", 0.5 * np.sin(2 * np.pi * 140.0 * t) + 0.05 * noise)
    started = time.perf_counter()
    AudioAnalyzer(wav)
    in_process = time.perf_counter() - started
    pool = start_pool()

    async def scenario():
        await pool.wait_ready()
        call = asyncio.create_task(pool.measure(wav, NO_PHONES))
        longest, last = 0.0, time.perf_counter()
        while not call.done():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            longest, last = max(longest, now - last), now
        await call
        return longest

    assert asyncio.run(scenario()) < in_process / 4


def test_calls_on_a_pool_whose_worker_dies_restart_it_once(start_pool, monkeypatch):
    monkeypatch.setattr(analyzer_pool, "_measure", _fake_measure)
    pool = start_pool(workers=2)

    async def scenario():
        await pool.wait_ready()
        calls = [asyncio.create_task(pool.measure(HANG, NO_PHONES)) for _ in range(4)]
        await asyncio.sleep(0.2)
        next(iter(pool._pool._processes.values())).kill()
        results = await asyncio.gather(*calls, return_exceptions=True)
        return results, await pool.measure(READY, NO_PHONES)

    results, after = asyncio.run(scenario())

    assert [type(r) for r in results] == [AnalyzerUnavailable] * 4
    assert pool.restarts == 1
    assert after == []


def test_a_pass_reports_its_wait_for_a_worker_apart_from_its_run(start_pool, monkeypatch):
    monkeypatch.setattr(analyzer_pool, "_measure", _fake_measure)
    pool = start_pool(workers=1)

    async def scenario():
        await pool.wait_ready()
        return await asyncio.gather(
            noted(pool.measure(SLOW, NO_PHONES)), noted(pool.measure(SLOW, NO_PHONES))
        )

    (_, first), (_, second) = asyncio.run(scenario())
    assert first["praat_wait_ms"] < SLOW_PASS * 1000 <= second["praat_wait_ms"]
    assert first["praat_ms"] >= SLOW_PASS * 1000 and second["praat_ms"] >= SLOW_PASS * 1000
