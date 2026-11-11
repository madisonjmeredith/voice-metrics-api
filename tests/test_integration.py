"""
The real aligner, end to end. Runs only where MFA is installed (the Docker
image, and therefore CI); skipped on a laptop without the conda environment.
"""

import time

import numpy as np
import pytest

from app.aligner import AlignmentTimeout
from app.converter import get_audio_duration
from tests.conftest import (
    FIXTURES,
    FS,
    HELLO_LEAD_IN,
    HELLO_WAV,
    needs_mfa,
    needs_whisper,
    post_analyze,
    use_token,
    write_wav,
)
from tests.textgrids import parse_textgrid

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
    assert alignment.speech_coverage(duration) > 0.7
    assert elapsed < ALIGN_BUDGET_SECONDS


@needs_mfa
def test_an_alignment_out_of_time_is_stopped_and_the_aligner_still_works(aligner):
    duration = get_audio_duration(HELLO_WAV)
    with pytest.raises(AlignmentTimeout):
        aligner.align(HELLO_WAV, duration, HELLO_TEXT, budget=0)
    alignment = aligner.align(HELLO_WAV, duration, HELLO_TEXT)
    assert [w.label for w in alignment.speech_words] == ["hello", "there", "world"]


@needs_mfa
def test_coverage_is_low_when_the_audio_is_not_the_transcript(aligner, tmp_path):
    seconds = 4.0
    tone = 0.5 * np.sin(2 * np.pi * 440.0 * np.arange(int(FS * seconds)) / FS)
    wav = write_wav(tmp_path / "tone.wav", tone)

    alignment = aligner.align(wav, seconds, "Can you hear me okay?")

    assert [w.label for w in alignment.speech_words] == ["can", "you", "hear", "me", "okay"]
    assert alignment.speech_coverage(seconds) < 0.3
    assert alignment.speech_span is None


@needs_mfa
def test_a_low_level_lead_in_is_left_out_of_the_alignment(aligner, hello_after_lead_in):
    alignment = aligner.align(hello_after_lead_in, get_audio_duration(hello_after_lead_in), HELLO_TEXT)
    plain = aligner.align(HELLO_WAV, get_audio_duration(HELLO_WAV), HELLO_TEXT)

    assert [w.label for w in alignment.speech_words] == ["hello", "there", "world"]
    assert alignment.speech_span[0] > 1.0
    assert alignment.speech_phones[0].start == pytest.approx(
        HELLO_LEAD_IN + plain.speech_phones[0].start, abs=0.05
    )


@needs_mfa
def test_reports_out_of_vocabulary_words(aligner):
    alignment = aligner.align(HELLO_WAV, get_audio_duration(HELLO_WAV), "Hello there, zorblax.")
    assert alignment.oovs == ["zorblax"]
    assert "<unk>" in [w.label for w in alignment.words]


@needs_mfa
@pytest.mark.parametrize(
    "accented, plain",
    [
        ("José", "Jose"),
        ("Jose\u0301", "Jose"),
        ("Zoë's", "Zoe's"),
        ("café-crème", "cafe-creme"),
        ("coup d'état", "coup d'etat"),
        ('"José," she said.', '"Jose," she said.'),
    ],
)
def test_an_accented_word_tokenizes_as_its_known_unaccented_spelling(aligner, accented, plain):
    tokenized = aligner.tokenize(accented)
    assert tokenized == aligner.tokenize(plain)
    assert tokenized.in_vocabulary


@needs_mfa
@pytest.mark.parametrize(
    "typed, expected",
    [
        ("Łukasz", ("łukasz", ["łukasz"])),
        ("L'Oréal", ("l' oréal", ["oréal"])),
        ("Zorbla\u0300x", ("zorblàx", ["zorblàx"])),
    ],
)
def test_a_word_unknown_without_its_accents_keeps_them(aligner, typed, expected):
    assert aligner.tokenize(typed) == expected


@needs_mfa
def test_aligns_an_accented_transcript(aligner):
    alignment = aligner.align(HELLO_WAV, get_audio_duration(HELLO_WAV), "Hélló thére, wörld.")
    assert [w.label for w in alignment.speech_words] == ["hello", "there", "world"]
    assert alignment.oovs == []


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
def test_features_do_not_depend_on_earlier_alignments(aligner):
    hello = (HELLO_WAV, 0.0, get_audio_duration(HELLO_WAV), aligner.tokenize(HELLO_TEXT).normalized)

    first = aligner._utterance(*hello)
    aligner.align(PLAYLIST_WAV, get_audio_duration(PLAYLIST_WAV), PLAYLIST_TEXT)
    again = aligner._utterance(*hello)

    assert np.array_equal(again.mfccs.numpy(), first.mfccs.numpy())


@needs_mfa
def test_matches_checked_in_textgrid(aligner):
    live = aligner.align(HELLO_WAV, get_audio_duration(HELLO_WAV), HELLO_TEXT)
    stored = parse_textgrid(FIXTURES / "hello.TextGrid")
    assert [p.label for p in live.speech_phones] == [p.label for p in stored.speech_phones]
    for a, b in zip(live.speech_phones, stored.speech_phones):
        assert a.start == pytest.approx(b.start, abs=0.02)


@pytest.fixture(scope="module")
def app_client():
    from fastapi.testclient import TestClient

    import app.main as main

    with pytest.MonkeyPatch.context() as mp:
        use_token(mp)
        with TestClient(main.app, headers={"Authorization": "Bearer s3cret"}) as client:
            yield client


@needs_mfa
def test_full_request_through_the_app(app_client):
    assert app_client.get("/ready").status_code == 200
    response = post_analyze(app_client, HELLO_WAV, HELLO_TEXT, include_phones=1)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [w["word"] for w in body["words"]] == ["hello", "there", "world"]
    assert body["scored_phone_count"] >= 3
    assert 0.0 <= body["resonance"]["median"] <= 1.0
    assert body["recognition"] == {"text": "Hello there, world.", "word_coverage": 1.0, "compared_words": 3}


@needs_mfa
def test_a_low_level_lead_in_does_not_move_the_scores(app_client, hello_after_lead_in):
    plain = post_analyze(app_client, HELLO_WAV, HELLO_TEXT)
    padded = post_analyze(app_client, hello_after_lead_in, HELLO_TEXT)

    assert padded.status_code == plain.status_code == 200, padded.text
    plain, padded = plain.json(), padded.json()
    assert padded["pitch"]["median"] == pytest.approx(plain["pitch"]["median"], abs=5.0)
    assert padded["resonance"]["balanced"] == pytest.approx(plain["resonance"]["balanced"], abs=0.05)


@needs_mfa
@needs_whisper
def test_a_fluent_read_of_other_words_is_not_heard_as_the_transcript(app_client):
    response = post_analyze(app_client, HELLO_WAV, "Can you hear me okay?")

    assert response.status_code == 200, response.text
    assert response.json()["recognition"] == {
        "text": "Hello there, world.", "word_coverage": 0.0, "compared_words": 5,
    }


@needs_mfa
def test_lexicon_through_the_app(app_client):
    words = ["Hello", "Siobhán", "Niamh's", "L'Oréal", "...", "Zorblax's"]
    response = app_client.post("/lexicon", json={"words": words})

    assert response.status_code == 200, response.text
    assert response.json()["words"] == [
        {"word": "Hello", "normalized": "hello", "oovs": [], "in_vocabulary": True},
        {"word": "Siobhán", "normalized": "siobhan", "oovs": [], "in_vocabulary": True},
        {"word": "Niamh's", "normalized": "niamh 's", "oovs": [], "in_vocabulary": True},
        {"word": "L'Oréal", "normalized": "l' oréal", "oovs": ["oréal"], "in_vocabulary": False},
        {"word": "...", "normalized": "", "oovs": [], "in_vocabulary": False},
        {"word": "Zorblax's", "normalized": "zorblax 's", "oovs": ["zorblax"], "in_vocabulary": False},
    ]
