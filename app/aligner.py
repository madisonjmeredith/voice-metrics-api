"""
Forced alignment of a transcript to a recording with Montreal Forced Aligner.

The acoustic model and lexicon are loaded once per process and reused, which
is what keeps a request to about a second. MFA's own `align_one` command does
the same work but re-extracts the model and recompiles the lexicon on every
call; this module mirrors its internals
(montreal_forced_aligner/command_line/align_one.py) without that overhead.

Everything from kalpy and MFA is imported lazily so the dataclasses and label
sets here can be used on machines without the conda environment.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Labels MFA emits for things that are not speech: praatio gaps, MFA's silence
# word and phone, and the out-of-vocabulary word and phone.
NON_SPEECH_LABELS = frozenset({"", "<eps>", "sil", "sp", "spn", "<unk>"})

DEFAULT_BEAM = 10
DEFAULT_RETRY_BEAM = 40
WIDE_BEAM = 40
WIDE_RETRY_BEAM = 160

# Longer than these and the aligner is almost certainly stretching a label
# over audio the transcript does not cover.
SUSPICIOUS_PHONE_SECONDS = 0.8
SUSPICIOUS_WORD_SECONDS = 2.0


@dataclass(frozen=True)
class Span:
    label: str
    start: float
    end: float

    @property
    def is_speech(self) -> bool:
        return self.label not in NON_SPEECH_LABELS

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2


class WordSpan(Span):
    pass


@dataclass(frozen=True)
class PhoneSpan(Span):
    word_index: Optional[int] = None


@dataclass(frozen=True)
class Alignment:
    words: list[WordSpan]
    phones: list[PhoneSpan]
    oovs: list[str]

    @property
    def speech_words(self) -> list[WordSpan]:
        return [w for w in self.words if w.is_speech]

    @property
    def speech_phones(self) -> list[PhoneSpan]:
        return [p for p in self.phones if p.is_speech]

    @property
    def suspicious(self) -> bool:
        return any(p.duration > SUSPICIOUS_PHONE_SECONDS for p in self.speech_phones) or any(
            w.duration > SUSPICIOUS_WORD_SECONDS for w in self.speech_words
        )


class AlignmentError(Exception):
    """MFA could not align the transcript to the audio, even with a wide beam."""


class EmptyTranscriptError(ValueError):
    """Nothing was left of the transcript after normalization."""


class LoadedAligner:
    """english_us_arpa acoustic model plus compiled lexicon, resident in memory."""

    def __init__(self, acoustic_dir: Path, dictionary_path: Path) -> None:
        from kalpy.aligner import KalpyAligner
        from kalpy.feat.cmvn import CmvnComputer
        from kalpy.fstext.lexicon import LexiconCompiler
        from montreal_forced_aligner.data import (
            BRACKETED_WORD,
            CUTOFF_WORD,
            LAUGHTER_WORD,
            OOV_WORD,
        )
        from montreal_forced_aligner.dictionary.mixins import (
            DEFAULT_BRACKETS,
            DEFAULT_CLITIC_MARKERS,
            DEFAULT_COMPOUND_MARKERS,
            DEFAULT_PUNCTUATION,
            DEFAULT_WORD_BREAK_MARKERS,
        )
        from montreal_forced_aligner.models import AcousticModel
        from montreal_forced_aligner.tokenization.simple import SimpleTokenizer

        # A directory source makes AcousticModel skip zip extraction.
        self._acoustic_model = AcousticModel(acoustic_dir)
        params = self._acoustic_model.parameters

        self._lexicon = LexiconCompiler(
            disambiguation=False,
            silence_probability=params["silence_probability"],
            initial_silence_probability=params["initial_silence_probability"],
            final_silence_correction=params["final_silence_correction"],
            final_non_silence_correction=params["final_non_silence_correction"],
            silence_phone=params["optional_silence_phone"],
            oov_phone=params["oov_phone"],
            position_dependent_phones=params["position_dependent_phones"],
            phones=params["non_silence_phones"],
            ignore_case=True,
        )
        self._lexicon.load_pronunciations(dictionary_path)
        self._lexicon.create_fsts()
        self._lexicon.clear()

        self._tokenizer = SimpleTokenizer(
            word_table=self._lexicon.word_table,
            word_break_markers=DEFAULT_WORD_BREAK_MARKERS,
            punctuation=DEFAULT_PUNCTUATION,
            clitic_markers=DEFAULT_CLITIC_MARKERS,
            compound_markers=DEFAULT_COMPOUND_MARKERS,
            brackets=DEFAULT_BRACKETS,
            laughter_word=LAUGHTER_WORD,
            oov_word=OOV_WORD,
            bracketed_word=BRACKETED_WORD,
            cutoff_word=CUTOFF_WORD,
            ignore_case=True,
        )
        # The property builds a fresh computer on each access, so hold one.
        self._mfcc_computer = self._acoustic_model.mfcc_computer
        self._cmvn_computer = CmvnComputer()
        self._aligners = (
            KalpyAligner(
                self._acoustic_model, self._lexicon,
                beam=DEFAULT_BEAM, retry_beam=DEFAULT_RETRY_BEAM,
            ),
            KalpyAligner(
                self._acoustic_model, self._lexicon,
                beam=WIDE_BEAM, retry_beam=WIDE_RETRY_BEAM,
            ),
        )

    def tokenize(self, transcript: str) -> tuple[str, list[str]]:
        """Normalize a transcript the way MFA will; returns (text, oov words)."""
        tokenized = self._tokenizer(transcript)
        return tokenized.normalized_text, sorted(set(tokenized.oovs))

    def align(self, wav_path: Path, duration: float, transcript: str) -> Alignment:
        from kalpy.exceptions import AlignerError
        from kalpy.utterance import Segment
        from kalpy.utterance import Utterance as KalpyUtterance

        normalized, oovs = self.tokenize(transcript)
        if not normalized.strip():
            raise EmptyTranscriptError("Transcript has no alignable words.")

        utterance = KalpyUtterance(Segment(str(wav_path), 0.0, duration, 0), normalized)
        utterance.generate_mfccs(self._mfcc_computer)
        cmvn = self._cmvn_computer.compute_cmvn_from_features([utterance.mfccs])
        utterance.apply_cmvn(cmvn)

        last_error: Optional[Exception] = None
        for aligner in self._aligners:
            try:
                ctm = aligner.align_utterance(utterance)
            except AlignerError as e:
                last_error = e
                continue
            return _alignment_from_ctm(ctm, oovs)
        raise AlignmentError(str(last_error))


def _alignment_from_ctm(ctm, oovs: list[str]) -> Alignment:
    words: list[WordSpan] = []
    phones: list[PhoneSpan] = []
    for word_index, interval in enumerate(ctm.word_intervals):
        word = WordSpan(interval.label, interval.begin, interval.end)
        words.append(word)
        phones.extend(
            PhoneSpan(p.label, p.begin, p.end, word_index if word.is_speech else None)
            for p in interval.phones
        )
    return Alignment(words=words, phones=phones, oovs=oovs)
