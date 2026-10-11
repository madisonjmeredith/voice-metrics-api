"""
The real aligner, end to end. Runs only where MFA is installed (the Docker
image, and therefore CI); skipped on a laptop without the conda environment.
"""

import time

import numpy as np
import pytest

from app.converter import get_audio_duration
from tests.conftest import FIXTURES, FS, needs_mfa, post_analyze, write_wav
from tests.textgrids import parse_textgrid

HELLO_WAV = FIXTURES / "hello.wav"
HELLO_TEXT = "Hello there, world."
PLAYLIST_WAV = FIXTURES / "playlist.wav"
PLAYLIST_TEXT = "Update the playlist with a decaf vibe."

# Generous even under emulation; native x86 lands well under a second.
ALIGN_BUDGET_SECONDS = 5.0


@needs_mfa
def test_aligns_hello(aligner):
    duration = get_audio_duration(HELLO_WAV)
    started = time.perf_counter()
    alignment = aligner.align(HELLO_WAV, duration, HELLO_TEXT)
    elapsed = time.perf_counter() - started

    assert [w.label for w in alignment.speech_words] == ["hello", "there", "world"]
    assert all(p.label[-1].isdigit() for p in alignment.speech_phones if p.label[0] in "AEIOU")
    assert alignment.oovs == []
    assert not alignment.suspicious
    assert alignment.speech_coverage(duration) > 0.7
    assert elapsed < ALIGN_BUDGET_SECONDS


@needs_mfa
def test_coverage_is_low_when_the_audio_is_not_the_transcript(aligner, tmp_path):
    seconds = 4.0
    tone = 0.5 * np.sin(2 * np.pi * 440.0 * np.arange(int(FS * seconds)) / FS)
    wav = write_wav(tmp_path / "tone.wav", tone)

    alignment = aligner.align(wav, seconds, "Can you hear me okay?")

    assert [w.label for w in alignment.speech_words] == ["can", "you", "hear", "me", "okay"]
    assert alignment.speech_coverage(seconds) < 0.3


@needs_mfa
def test_reports_out_of_vocabulary_words(aligner):
    alignment = aligner.align(HELLO_WAV, get_audio_duration(HELLO_WAV), "Hello there, zorblax.")
    assert alignment.oovs == ["zorblax"]
    assert "<unk>" in [w.label for w in alignment.words]


@needs_mfa
def test_aligns_words_the_release_dictionary_lacks(aligner):
    duration = get_audio_duration(PLAYLIST_WAV)
    alignment = aligner.align(PLAYLIST_WAV, duration, PLAYLIST_TEXT)

    assert [w.label for w in alignment.speech_words] == [
        "update", "the", "playlist", "with", "a", "decaf", "vibe",
    ]
    assert alignment.oovs == []
    assert alignment.speech_coverage(duration) > 0.9


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
