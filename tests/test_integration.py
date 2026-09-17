"""
The real aligner, end to end. Runs only where MFA is installed (the Docker
image, and therefore CI); skipped on a laptop without the conda environment.
"""

import time

import pytest

from app.converter import get_audio_duration
from app.worker import ACOUSTIC_DIR, DICTIONARY_PATH
from tests.conftest import FIXTURES, needs_mfa, post_analyze
from tests.textgrids import parse_textgrid

HELLO_WAV = FIXTURES / "hello.wav"
HELLO_TEXT = "Hello there, world."

# Generous even under emulation; native x86 lands well under a second.
ALIGN_BUDGET_SECONDS = 5.0


@pytest.fixture(scope="module")
def aligner():
    from app.aligner import LoadedAligner

    return LoadedAligner(ACOUSTIC_DIR, DICTIONARY_PATH)


@needs_mfa
def test_aligns_hello(aligner):
    started = time.perf_counter()
    alignment = aligner.align(HELLO_WAV, get_audio_duration(HELLO_WAV), HELLO_TEXT)
    elapsed = time.perf_counter() - started

    assert [w.label for w in alignment.speech_words] == ["hello", "there", "world"]
    assert all(p.label[-1].isdigit() for p in alignment.speech_phones if p.label[0] in "AEIOU")
    assert alignment.oovs == []
    assert not alignment.suspicious
    assert elapsed < ALIGN_BUDGET_SECONDS


@needs_mfa
def test_reports_out_of_vocabulary_words(aligner):
    alignment = aligner.align(HELLO_WAV, get_audio_duration(HELLO_WAV), "Hello there, zorblax.")
    assert alignment.oovs == ["zorblax"]
    assert "<unk>" in [w.label for w in alignment.words]


@needs_mfa
def test_matches_checked_in_textgrid(aligner):
    live = aligner.align(HELLO_WAV, get_audio_duration(HELLO_WAV), HELLO_TEXT)
    stored = parse_textgrid(FIXTURES / "hello.TextGrid")
    assert [p.label for p in live.speech_phones] == [p.label for p in stored.speech_phones]
    for a, b in zip(live.speech_phones, stored.speech_phones):
        assert a.start == pytest.approx(b.start, abs=0.02)


@needs_mfa
def test_full_request_through_the_app():
    from fastapi.testclient import TestClient

    import app.main as main

    with TestClient(main.app) as client:
        assert client.get("/health").json()["aligner"] == "ready"
        response = post_analyze(client, HELLO_WAV, HELLO_TEXT, include_phones=1)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [w["word"] for w in body["words"]] == ["hello", "there", "world"]
    assert body["scored_phone_count"] >= 3
    assert 0.0 <= body["resonance"]["median"] <= 1.0
