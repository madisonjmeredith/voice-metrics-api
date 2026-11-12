"""
How much of the transcript a recognizer heard, and which of its words.

The transcript's side is the aligned words, so MFA's tokenization and accent
fold carry over and out-of-vocabulary words are already marked. Both sides go
through Whisper's English normalizer, so case, punctuation, contractions, and
spelled-out numbers don't count as differences.
"""

from difflib import SequenceMatcher
from typing import NamedTuple, Optional

from more_itertools import split_at

from app.aligner import OOV_LABEL, Alignment
from app.normalizers import EnglishTextNormalizer

_normalize = EnglishTextNormalizer()


class Token(NamedTuple):
    text: str
    words: frozenset[int]


class Comparison(NamedTuple):
    word_coverage: Optional[float]
    compared_words: int
    heard: dict[int, bool]


def normalize(text: str) -> list[str]:
    return _normalize(text).split()


def transcript_tokens(alignment: Alignment) -> list[Token]:
    """The aligned words without OOVs, normalized a run at a time so nothing joins across one."""
    runs = split_at(enumerate(alignment.words), lambda item: item[1].label == OOV_LABEL)
    return [
        token
        for run in runs
        for token in _run_tokens([(index, word.label) for index, word in run if word.is_speech])
    ]


def _run_tokens(words: list[tuple[int, str]]) -> list[Token]:
    """The run's tokens, each with the indices of the words it came from."""
    together = normalize(" ".join(label for _, label in words))
    alone = [(text, index) for index, label in words for text in normalize(label)]
    matcher = SequenceMatcher(None, [text for text, _ in alone], together, autojunk=False)
    tokens = []
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            tokens += [Token(text, frozenset({index})) for (_, index), text in zip(alone[a1:a2], together[b1:b2])]
        else:
            sources = frozenset(index for _, index in alone[a1:a2])
            tokens += [Token(text, sources) for text in together[b1:b2]]
    return tokens


def transcript_words(alignment: Alignment) -> list[str]:
    return [token.text for token in transcript_tokens(alignment)]


def matched(transcript: list[str], heard: list[str]) -> list[bool]:
    """Which transcript words appear, in order, in what was heard; ties keep the earliest words."""
    table = [[0] * (len(heard) + 1)]
    for word in transcript:
        above, row = table[-1], [0]
        for j, other in enumerate(heard):
            row.append(above[j] + 1 if word == other else max(above[j + 1], row[j]))
        table.append(row)

    found = [False] * len(transcript)
    i, j = len(transcript), len(heard)
    while i and j:
        if table[i - 1][j] == table[i][j]:
            i -= 1
        elif table[i][j - 1] == table[i][j]:
            j -= 1
        else:
            found[i - 1] = True
            i, j = i - 1, j - 1
    return found


def word_coverage(transcript: list[str], heard: list[str]) -> Optional[float]:
    """The share of the transcript's words that appear, in order, in what was heard."""
    return _share(matched(transcript, heard))


def _share(found: list[bool]) -> Optional[float]:
    return sum(found) / len(found) if found else None


def heard_words(tokens: list[Token], found: list[bool]) -> dict[int, bool]:
    """Each aligned word that gave a token, and whether every token it gave was heard."""
    heard: dict[int, bool] = {}
    for token, was_found in zip(tokens, found):
        for index in token.words:
            heard[index] = heard.get(index, True) and was_found
    return heard


def compare(alignment: Alignment, text: str) -> Comparison:
    tokens = transcript_tokens(alignment)
    found = matched([token.text for token in tokens], normalize(text))
    return Comparison(_share(found), len(tokens), heard_words(tokens, found))
