"""
Read an MFA TextGrid into the same spans the in-process aligner produces.

Test-only: the service gets its intervals straight from kalpy without
touching a file. This is how checked-in alignments become fixtures.
"""

from pathlib import Path
from typing import Optional

from praatio import textgrid

from app.aligner import Alignment, PhoneSpan, WordSpan

WORDS_TIER = "words"
PHONES_TIER = "phones"


def parse_textgrid(path: Path) -> Alignment:
    grid = textgrid.openTextgrid(str(path), includeEmptyIntervals=True, reportingMode="silence")

    words = [WordSpan(label, start, end) for start, end, label in grid.getTier(WORDS_TIER).entries]
    phones = []
    for start, end, label in grid.getTier(PHONES_TIER).entries:
        span = PhoneSpan(label, start, end)
        phones.append(PhoneSpan(label, start, end, _word_index_at(words, span.midpoint)))
    return Alignment(words=words, phones=phones, oovs=[])


def _word_index_at(words: list[WordSpan], time: float) -> Optional[int]:
    for index, word in enumerate(words):
        if word.is_speech and word.start <= time < word.end:
            return index
    return None
