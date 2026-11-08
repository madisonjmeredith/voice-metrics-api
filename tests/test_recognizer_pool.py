"""
RecognizerPool over a worker with no model in it. The queue rules it shares with
the aligner are covered in test_worker.py; these cover what is the recognizer's.

The stand-ins below run in the spawned worker, so they have to be importable
by name from there.
"""

import asyncio
import logging
import re
import time
from pathlib import Path

import pytest

import app.recognizer_pool as recognizer_pool
from app.recognizer import LoadedRecognizer
from app.recognizer_pool import (
    WHISPER_MODEL_DIR,
    RecognitionTimeout,
    RecognizerBusy,
    RecognizerPool,
    RecognizerUnavailable,
)
from tests.conftest import FIXTURES, eventually, kill_workers, needs_whisper, noted, pool_lines

SPAWN_TIMEOUT = 60.0
_SETUP: dict = {}


def _fake_init(model_dir: Path, threads: int) -> None:
    _SETUP.update(model_dir=model_dir, threads=threads)


def _fake_recognize(wav_path: Path) -> str:
    if wav_path.name == "hang.wav":
        time.sleep(60)
    return f"{_SETUP['model_dir']} {_SETUP['threads']}"


class CountingPool(RecognizerPool):
    restarts = 0

    def restart(self) -> None:
        self.restarts += 1
        super().restart()


@pytest.fixture
def start_pool(monkeypatch, request):
    monkeypatch.setattr(recognizer_pool, "_init", _fake_init)
    monkeypatch.setattr(recognizer_pool, "_recognize", _fake_recognize)

    def start(warmup: Path = Path("warmup.wav"), timeout: float = SPAWN_TIMEOUT, **kwargs) -> CountingPool:
        monkeypatch.setattr(recognizer_pool, "WARMUP_WAV", warmup)
        pool = CountingPool(timeout=timeout, **kwargs)
        pool.start()
        request.addfinalizer(pool.shutdown)
        return pool

    return start


def test_a_recognition_reaches_the_worker_with_its_model_and_threads(start_pool, caplog):
    caplog.set_level(logging.INFO)
    pool = start_pool(model_dir=Path("/models/whisper"), threads=3)
    asyncio.run(pool.wait_ready())

    heard, fields = asyncio.run(noted(pool.recognize(Path("clip.wav"))))

    assert heard == "/models/whisper 3"
    assert {"recognizer_wait_ms", "recognizer_ms"} <= set(fields)
    [line] = pool_lines(caplog, logging.INFO)
    assert re.fullmatch(r"recognizer ready load_s=\d+\.\d", line)
    assert pool.state == "ready"


def test_a_recognition_that_runs_too_long_restarts_the_worker(start_pool, caplog):
    pool = start_pool(timeout=0.5)
    pool._warmups[0].result(timeout=SPAWN_TIMEOUT)

    with pytest.raises(RecognitionTimeout):
        asyncio.run(pool.recognize(Path("hang.wav")))

    assert pool.restarts == 1
    [line] = pool_lines(caplog)
    assert re.fullmatch(r"recognizer restart reason=recognition_timeout uptime_s=\d+\.\d", line)


def test_a_lost_worker_is_down_until_a_call_restarts_it(start_pool, caplog):
    pool = start_pool()
    pool._warmups[0].result(timeout=SPAWN_TIMEOUT)

    kill_workers(pool)
    eventually(lambda: pool.state == "down")
    with pytest.raises(RecognizerUnavailable):
        asyncio.run(pool.recognize(Path("clip.wav")))

    assert pool.restarts == 1
    assert pool.state == "warming"
    [line] = pool_lines(caplog)
    assert line.startswith("recognizer restart reason=worker_lost exitcode=-9 ")


def test_a_call_that_cannot_reach_its_turn_is_busy(start_pool):
    pool = start_pool(warmup=Path("hang.wav"), timeout=0.3)

    with pytest.raises(RecognizerBusy):
        asyncio.run(pool.recognize(Path("clip.wav")))

    assert pool.restarts == 0


@needs_whisper
def test_whisper_hears_the_hello_fixture():
    assert LoadedRecognizer(WHISPER_MODEL_DIR, 2).recognize(FIXTURES / "hello.wav") == "Hello there, world."
