"""
The entries in dictionary/cmudict.dict and dictionary/custom.dict. A phone the
acoustic model lacks loads without complaint and crashes the worker once its
word is aligned, so the format checks run without MFA.
"""

import unicodedata
from pathlib import Path

import pytest

from app.worker import ACOUSTIC_DIR, DICTIONARY_PATH
from tests.conftest import needs_mfa

DICTIONARY_DIR = Path(__file__).parents[1] / "dictionary"
CMUDICT = DICTIONARY_DIR / "cmudict.dict"
CUSTOM = DICTIONARY_DIR / "custom.dict"

CONSONANTS = "B CH D DH F G HH JH K L M N NG P R S SH T TH V W Y Z ZH".split()
VOWELS = "AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW".split()
ARPABET = frozenset(CONSONANTS) | {vowel + stress for vowel in VOWELS for stress in "012"}


each_file = pytest.mark.parametrize("path", [CMUDICT, CUSTOM], ids=lambda path: path.name)


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf8").splitlines()


def _headwords(path: Path) -> set[str]:
    return {line.split("\t")[0] for line in _lines(path)}


@each_file
def test_entries_are_a_word_a_tab_and_arpabet_phones(path):
    for line in _lines(path):
        assert line.count("\t") == 1, line
        word, pronunciation = line.split("\t")
        assert word == word.lower() and word.split() == [word], line
        assert word == unicodedata.normalize("NFC", word), line
        assert ARPABET.issuperset(pronunciation.split(" ")), line


@each_file
def test_entries_are_sorted_and_unique(path):
    lines = _lines(path)
    assert lines == sorted(set(lines))


def test_files_share_no_headword():
    assert not _headwords(CMUDICT) & _headwords(CUSTOM)


@needs_mfa
def test_arpabet_is_the_acoustic_models_phone_set():
    from montreal_forced_aligner.models import AcousticModel

    assert set(AcousticModel(ACOUSTIC_DIR).parameters["non_silence_phones"]) == ARPABET


@needs_mfa
@each_file
def test_loaded_dictionary_has_exactly_the_files_lines_for_its_words(path):
    words = _headwords(path)
    loaded = [line for line in _lines(DICTIONARY_PATH) if line.split("\t")[0] in words]
    assert loaded == _lines(path)


@needs_mfa
@each_file
def test_every_word_is_in_vocabulary(path, aligner):
    for word in _headwords(path):
        assert aligner.tokenize(word) == (word, [])
