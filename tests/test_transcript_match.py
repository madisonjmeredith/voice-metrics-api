import time

import pytest

from app.aligner import NON_SPEECH_LABELS, OOV_LABEL, Alignment, WordSpan
from app.transcript_match import (
    Token,
    compare,
    matched,
    normalize,
    transcript_tokens,
    transcript_words,
    word_coverage,
)


def aligned(text: str) -> Alignment:
    """The alignment of each space-separated label, with a silence before and after."""
    labels = ["<eps>", *text.split(), "<eps>"]
    words = [WordSpan(label, i * 0.3, (i + 1) * 0.3) for i, label in enumerate(labels)]
    return Alignment(words=words, phones=[], oovs=[])


def coverage(aligned_text: str, heard: str):
    return word_coverage(transcript_words(aligned(aligned_text)), normalize(heard))


def marks(aligned_text: str, heard: str) -> list[tuple[str, object]]:
    """Each aligned speech word with whether it was heard, or None when it gave no token."""
    alignment = aligned(aligned_text)
    heard_map = compare(alignment, heard).heard
    return [(w.label, heard_map.get(i)) for i, w in enumerate(alignment.words) if w.is_speech]


def test_a_read_of_the_transcript_is_all_heard():
    words = transcript_words(aligned("hi how are you it's really good to see you"))
    assert len(words) == 11
    assert word_coverage(words, normalize("Hi, how are you? It's really good to see you.")) == 1.0


def test_contractions_and_an_em_dash_match_either_way_they_are_written():
    assert coverage("hello i don't think we've met i'm sam", "Hello, I do not think we have met—I am Sam.") == 1.0


def test_other_words_are_not_heard():
    assert coverage("can you hear me okay", "Hello there, world.") == 0.0


def test_half_a_read_is_half_heard():
    assert coverage("the quick brown fox jumps over the lazy dog", "The quick brown fox.") == pytest.approx(4 / 9)


@pytest.mark.parametrize(
    "heard",
    [
        "Can you, can you hear me okay?",
        "Um, can you hear me okay?",
        "Can you hear me okay? Thank you.",
        "Okay so. Can you hear me okay?",
    ],
)
def test_extra_words_cost_nothing(heard):
    assert coverage("can you hear me okay", heard) == 1.0


def test_words_must_be_heard_in_order():
    assert coverage("red green blue gold", "gold blue green red") == 0.25


def test_an_oov_word_is_left_out_whatever_was_heard_for_it():
    words = transcript_words(aligned(f"hi i'm {OOV_LABEL} nice to meet you"))
    assert len(words) == 7
    assert word_coverage(words, normalize("Hi, I'm Shiv On. Nice to meet you.")) == 1.0


def test_nothing_joins_across_an_oov_word():
    assert transcript_words(aligned(f"twenty {OOV_LABEL} five")) == ["20", "5"]
    assert transcript_words(aligned("twenty five")) == ["25"]


def test_non_speech_labels_are_not_words():
    others = " ".join(sorted(NON_SPEECH_LABELS - {"", OOV_LABEL}))
    assert transcript_words(aligned(f"hello {others} world")) == ["hello", "world"]


def test_a_transcript_of_only_oov_words_has_no_coverage():
    assert coverage(f"{OOV_LABEL} {OOV_LABEL}", "Siobhan Niamh") is None


def test_nothing_heard_is_zero():
    assert coverage("hello there world", "") == 0.0


def test_a_long_comparison_stays_quick():
    transcript = transcript_words(aligned(" ".join(["the", "cat", "sat", "on", "a", "mat", "today"] * 50)))
    heard = normalize(" ".join(["a", "dog", "ran", "past", "the", "big", "red", "barn"] * 90))
    started = time.perf_counter()
    word_coverage(transcript, heard)
    assert time.perf_counter() - started < 0.25


def test_a_spelled_out_number_comes_from_every_word_in_it():
    assert transcript_tokens(aligned("i counted twenty five sheep")) == [
        Token("i", frozenset({1})),
        Token("counted", frozenset({2})),
        Token("25", frozenset({3, 4})),
        Token("sheep", frozenset({5})),
    ]


def test_a_contraction_gives_its_word_two_tokens():
    assert transcript_tokens(aligned("i don't")) == [
        Token("i", frozenset({1})),
        Token("do", frozenset({2})),
        Token("not", frozenset({2})),
    ]


def test_token_indices_count_the_words_before_an_oov_word():
    assert transcript_tokens(aligned(f"hi {OOV_LABEL} there")) == [
        Token("hi", frozenset({1})),
        Token("there", frozenset({3})),
    ]


def test_a_read_that_stops_early_leaves_the_rest_unheard():
    assert marks("the quick brown fox jumps over the lazy dog", "The quick brown fox.") == [
        ("the", True), ("quick", True), ("brown", True), ("fox", True),
        ("jumps", False), ("over", False), ("the", False), ("lazy", False), ("dog", False),
    ]


def test_every_word_of_a_misheard_number_is_unheard():
    assert marks("twenty five sheep", "Twenty six sheep.") == [
        ("twenty", False), ("five", False), ("sheep", True),
    ]


def test_a_word_is_heard_only_when_all_its_tokens_are():
    assert marks("i don't know", "I do know.") == [("i", True), ("don't", False), ("know", True)]


def test_a_word_that_normalizes_to_nothing_is_not_marked():
    assert marks("um hello there", "Hello there.") == [("um", None), ("hello", True), ("there", True)]


def test_ties_keep_the_earliest_transcript_words():
    assert matched(["a", "b", "a"], ["a"]) == [True, False, False]
    assert matched(["the", "cat", "the", "dog"], ["the", "dog"]) == [True, False, False, True]


def test_comparison_counts_the_same_coverage_as_word_coverage():
    alignment, heard = aligned("hi i'm sam nice to meet you"), "Hi, I'm Sam. Nice to see you."
    comparison = compare(alignment, heard)
    assert comparison.compared_words == len(transcript_words(alignment)) == 8
    assert comparison.word_coverage == word_coverage(transcript_words(alignment), normalize(heard)) == 7 / 8
