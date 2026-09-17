import pytest

from app.analyzer import AudioAnalyzer
from app.measurement import measure_phones
from tests.conftest import VOWEL_A, VOWEL_F0, vowel_alignment


def test_midpoint_reading_of_a_synthetic_vowel(vowel_a):
    phones = measure_phones(AudioAnalyzer(vowel_a), vowel_alignment(labels=("AA1",)))

    assert len(phones) == 1
    phone = phones[0]
    assert phone.span.midpoint == pytest.approx(0.75)
    assert phone.F[0] == pytest.approx(VOWEL_F0, abs=3.0)
    assert phone.F[1] == pytest.approx(VOWEL_A[0], rel=0.08)
    assert phone.F[2] == pytest.approx(VOWEL_A[1], rel=0.08)
    assert phone.F[3] == pytest.approx(VOWEL_A[2], rel=0.08)


def test_silence_reads_as_unvoiced(silence):
    phones = measure_phones(AudioAnalyzer(silence), vowel_alignment(labels=("AA1",)))
    assert phones[0].F[0] is None


def test_non_speech_phones_are_skipped(vowel_a):
    phones = measure_phones(AudioAnalyzer(vowel_a), vowel_alignment(labels=("AA1",)))
    assert [p.label for p in phones] == ["AA1"]
    assert phones[0].span.word_index == 1
