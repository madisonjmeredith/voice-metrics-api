from types import SimpleNamespace

import pytest

from app.aligner import fold_accents
from tests.conftest import vowel_alignment


def stand_in_tokenizer(*vocabulary: str):
    """Lowercases, splits on whitespace, and reports the words the vocabulary lacks."""

    def tokenize(text: str) -> SimpleNamespace:
        words = [word.strip(",.") for word in text.lower().split()]
        return SimpleNamespace(oovs=[word for word in words if word not in vocabulary])

    return tokenize


def test_speech_coverage_leaves_silence_out():
    alignment = vowel_alignment()
    assert alignment.speech_coverage(1.5) == pytest.approx(1.3 / 1.5)
    assert alignment.speech_coverage(13.0) == pytest.approx(0.1)


@pytest.mark.parametrize("typed", ["José", "Jose\u0301"], ids=["composed", "decomposed"])
def test_folds_an_unknown_accented_word_onto_the_known_spelling(typed):
    assert fold_accents(f"Hola, {typed}.", stand_in_tokenizer("hola", "jose")) == "Hola, Jose."


@pytest.mark.parametrize("typed", ["Rosé", "Rose\u0301"], ids=["composed", "decomposed"])
def test_an_accented_headword_wins_over_the_fold(typed):
    assert fold_accents(typed, stand_in_tokenizer("rose", "rosé")) == "Rosé"


@pytest.mark.parametrize(
    "typed, kept",
    [
        ("Zorblàx", "Zorblàx"),
        ("Zorbla\u0300x", "Zorblàx"),
        ("Łukasz", "Łukasz"),
        ("Łódź", "Łódź"),
    ],
    ids=["composed", "decomposed", "no mark to strip", "half strips"],
)
def test_a_word_unknown_either_way_keeps_its_spelling(typed, kept):
    assert fold_accents(typed, stand_in_tokenizer("hello", "lodz")) == kept


def test_an_ascii_transcript_never_reaches_the_tokenizer():
    transcript = "Hello there, world."
    assert fold_accents(transcript, None) is transcript
