import time

import pytest

from app.aligner import NON_SPEECH_LABELS, OOV_LABEL, Alignment, WordSpan
from app.transcript_match import normalize, transcript_words, word_coverage


def aligned(text: str) -> Alignment:
    """The alignment of each space-separated label, with a silence before and after."""
    labels = ["<eps>", *text.split(), "<eps>"]
    words = [WordSpan(label, i * 0.3, (i + 1) * 0.3) for i, label in enumerate(labels)]
    return Alignment(words=words, phones=[], oovs=[])


def coverage(aligned_text: str, heard: str):
    return word_coverage(transcript_words(aligned(aligned_text)), normalize(heard))


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
