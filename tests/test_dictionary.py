"""
The entries in dictionary/custom.dict. A phone the acoustic model lacks loads
without complaint and crashes the worker once its word is aligned, so the
format checks run without MFA.
"""

from pathlib import Path

from app.worker import ACOUSTIC_DIR, DICTIONARY_PATH
from tests.conftest import needs_mfa

CUSTOM_DICTIONARY = Path(__file__).parents[1] / "dictionary" / "custom.dict"

CONSONANTS = "B CH D DH F G HH JH K L M N NG P R S SH T TH V W Y Z ZH".split()
VOWELS = "AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW".split()
ARPABET = frozenset(CONSONANTS) | {vowel + stress for vowel in VOWELS for stress in "012"}


def _lines(path: Path = CUSTOM_DICTIONARY) -> list[str]:
    return path.read_text(encoding="utf8").splitlines()


def _headwords() -> set[str]:
    return {line.split("\t")[0] for line in _lines()}


def test_entries_are_a_word_a_tab_and_arpabet_phones():
    for line in _lines():
        assert line.count("\t") == 1, line
        word, pronunciation = line.split("\t")
        assert word == word.lower() and word.split() == [word], line
        assert ARPABET.issuperset(pronunciation.split(" ")), line


def test_entries_are_sorted_and_unique():
    lines = _lines()
    assert lines == sorted(set(lines))


@needs_mfa
def test_arpabet_is_the_acoustic_models_phone_set():
    from montreal_forced_aligner.models import AcousticModel

    assert set(AcousticModel(ACOUSTIC_DIR).parameters["non_silence_phones"]) == ARPABET


@needs_mfa
def test_loaded_dictionary_has_exactly_these_entries_for_custom_words():
    custom_words = _headwords()
    loaded = [line for line in _lines(DICTIONARY_PATH) if line.split("\t")[0] in custom_words]
    assert loaded == _lines()


@needs_mfa
def test_custom_words_are_in_vocabulary(aligner):
    for word in _headwords():
        assert aligner.tokenize(word) == (word, [])
