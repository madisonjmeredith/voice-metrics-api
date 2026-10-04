import pytest

from app.aligner import NON_SPEECH_LABELS
from tests.textgrids import parse_textgrid
from tests.conftest import FIXTURES


def test_hello_words_and_phones():
    alignment = parse_textgrid(FIXTURES / "hello.TextGrid")

    assert [w.label for w in alignment.speech_words] == ["hello", "there", "world"]
    assert [w.label for w in alignment.words if not w.is_speech] == ["<eps>"] * 3
    assert [p.label for p in alignment.speech_phones] == [
        "HH", "EH0", "L", "OW1", "DH", "EH1", "R", "W", "ER1", "L", "D",
    ]


def test_phones_point_at_their_word():
    alignment = parse_textgrid(FIXTURES / "hello.TextGrid")
    by_word = {}
    for phone in alignment.speech_phones:
        by_word.setdefault(alignment.words[phone.word_index].label, []).append(phone.label)

    assert by_word == {
        "hello": ["HH", "EH0", "L", "OW1"],
        "there": ["DH", "EH1", "R"],
        "world": ["W", "ER1", "L", "D"],
    }


def test_silence_phones_have_no_word():
    alignment = parse_textgrid(FIXTURES / "squirrel.TextGrid")
    silences = [p for p in alignment.phones if p.label in NON_SPEECH_LABELS]

    assert silences, "the passage has pauses between clauses"
    assert all(p.word_index is None for p in silences)
    assert all(not p.is_speech for p in silences)


def test_intervals_are_contiguous_and_ordered():
    alignment = parse_textgrid(FIXTURES / "squirrel.TextGrid")
    for tier in (alignment.words, alignment.phones):
        for a, b in zip(tier, tier[1:]):
            assert a.end <= b.start + 1e-6
            assert a.start < a.end


def test_not_suspicious_for_a_normal_reading():
    assert not parse_textgrid(FIXTURES / "squirrel.TextGrid").suspicious


def test_speech_coverage_for_a_normal_reading():
    alignment = parse_textgrid(FIXTURES / "hello.TextGrid")
    assert alignment.speech_coverage(alignment.phones[-1].end) == pytest.approx(0.779, abs=0.001)
