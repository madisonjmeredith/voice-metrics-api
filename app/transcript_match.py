"""
How much of the transcript a recognizer heard.

The transcript's side is the aligned words, so MFA's tokenization and accent
fold carry over and out-of-vocabulary words are already marked. Both sides go
through Whisper's English normalizer, so case, punctuation, contractions, and
spelled-out numbers don't count as differences.
"""

from typing import Optional

from more_itertools import split_at

from app.aligner import OOV_LABEL, Alignment
from app.normalizers import EnglishTextNormalizer

_normalize = EnglishTextNormalizer()


def normalize(text: str) -> list[str]:
    return _normalize(text).split()


def transcript_words(alignment: Alignment) -> list[str]:
    """The aligned words without OOVs, normalized a run at a time so nothing joins across one."""
    runs = split_at(alignment.words, lambda word: word.label == OOV_LABEL)
    return [token for run in runs for token in normalize(" ".join(w.label for w in run if w.is_speech))]


def word_coverage(transcript: list[str], heard: list[str]) -> Optional[float]:
    """The share of the transcript's words that appear, in order, in what was heard."""
    if not transcript:
        return None
    return _common_subsequence(transcript, heard) / len(transcript)


def _common_subsequence(a: list[str], b: list[str]) -> int:
    previous = [0] * (len(b) + 1)
    for x in a:
        current = [0]
        for j, y in enumerate(b):
            current.append(previous[j] + 1 if x == y else max(previous[j + 1], current[j]))
        previous = current
    return previous[-1]
