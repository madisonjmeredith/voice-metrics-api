"""
Forced alignment of a transcript to a recording with Montreal Forced Aligner.

The acoustic model and lexicon are loaded once per process and reused, which
is what keeps a request to about a second. MFA's own `align_one` command does
the same work but re-extracts the model and recompiles the lexicon on every
call; this module mirrors its internals
(montreal_forced_aligner/command_line/align_one.py) without that overhead.
The decode itself mirrors kalpy's gmm_align_compiled (Kaldi's
AlignUtteranceWrapper) a chunk of frames at a time, so a deadline can stop a
slow one without killing the process.

Everything from kalpy and MFA is imported lazily so the dataclasses and label
sets here can be used on machines without the conda environment.
"""

import ctypes
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional

from app.converter import read_samples
from app.speech_span import speech_span

# Labels MFA emits for things that are not speech: praatio gaps, MFA's silence
# word and phone, and the out-of-vocabulary word and phone.
OOV_LABEL = "<unk>"
NON_SPEECH_LABELS = frozenset({"", "<eps>", "sil", "sp", "spn", OOV_LABEL})

BEAMS = (10, 40, 160)
CHUNK_FRAMES = 25
DITHER_SEED = 1


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
    speech_span: Optional[tuple[float, float]] = None

    @property
    def speech_words(self) -> list[WordSpan]:
        return [w for w in self.words if w.is_speech]

    @property
    def speech_phones(self) -> list[PhoneSpan]:
        return [p for p in self.phones if p.is_speech]

    def speech_coverage(self, duration: float) -> float:
        return sum(p.duration for p in self.speech_phones) / duration


class Tokenized(NamedTuple):
    normalized: str
    oovs: list[str]

    @property
    def in_vocabulary(self) -> bool:
        return bool(self.normalized.strip()) and not self.oovs


def _strip_marks(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize(
        "NFC", "".join(c for c in decomposed if not unicodedata.combining(c))
    )


def fold_accents(transcript: str, tokenizer) -> str:
    """Drop the combining marks from each word that is OOV with them and known without."""
    if transcript.isascii():
        return transcript

    def fold(match: re.Match) -> str:
        chunk = match.group()
        if chunk.isascii() or not tokenizer(chunk).oovs:
            return chunk
        stripped = _strip_marks(chunk)
        return chunk if tokenizer(stripped).oovs else stripped

    return re.sub(r"\S+", fold, unicodedata.normalize("NFC", transcript))


class AlignmentError(Exception):
    """MFA could not align the transcript to the audio, even with a wide beam."""


class AlignmentTimeout(Exception):
    """The alignment ran past its time; the worker was restarted only if it didn't stop on its own."""


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
        self._srand = ctypes.CDLL(None).srand
        self._cmvn_computer = CmvnComputer()
        aligner = KalpyAligner(self._acoustic_model, self._lexicon)
        self._graph_compiler = aligner.graph_compiler
        # kalpy aligns with the alignment model whenever there's no fMLLR transform, as here.
        self._gmm = aligner.ali_aligner if aligner.ali_aligner is not None else aligner.aligner

    def tokenize(self, transcript: str) -> Tokenized:
        """Normalize a transcript the way MFA will; returns (text, oov words)."""
        tokenized = self._tokenizer(fold_accents(transcript, self._tokenizer))
        return Tokenized(tokenized.normalized_text, sorted(set(tokenized.oovs)))

    def _utterance(self, wav_path: Path, begin: float, end: float, normalized: str):
        from kalpy.utterance import Segment
        from kalpy.utterance import Utterance as KalpyUtterance

        utterance = KalpyUtterance(Segment(str(wav_path), begin, end, 0), normalized)
        # Kaldi seeds the model's dither from libc's rand(); without this each call gets a new draw.
        self._srand(DITHER_SEED)
        utterance.generate_mfccs(self._mfcc_computer)
        cmvn = self._cmvn_computer.compute_cmvn_from_features([utterance.mfccs])
        utterance.apply_cmvn(cmvn)
        return utterance

    def _align_utterance(self, utterance, deadline: float):
        from _kalpy.decoder import FasterDecoder, FasterDecoderOptions
        from _kalpy.fstext import GetLinearSymbolSequence
        from _kalpy.gmm import DecodableAmDiagGmmScaled
        from _kalpy.hmm import AddTransitionProbs
        from kalpy.gmm.data import Alignment as KalpyAlignment

        gmm = self._gmm
        features = utterance.generate_features(self._acoustic_model)
        graph = self._graph_compiler.compile_fst(utterance.transcript)
        if graph.NumStates() == 0:
            # FasterDecoder asserts on a graph with no start state, and the assert is abort().
            raise AlignmentError("The transcript compiled to an empty graph.")
        AddTransitionProbs(
            gmm.transition_model, [], gmm.transition_scale, gmm.self_loop_scale, graph
        )
        decodable = DecodableAmDiagGmmScaled(
            gmm.acoustic_model, gmm.transition_model, features, gmm.acoustic_scale
        )
        options = FasterDecoderOptions()
        for beam in BEAMS:
            options.beam = beam
            decoder = FasterDecoder(graph, options)
            decoder.InitDecoding()
            while decoder.NumFramesDecoded() < features.NumRows():
                if time.monotonic() >= deadline:
                    raise AlignmentTimeout("The alignment ran out of time and was stopped")
                decoder.AdvanceDecoding(decodable, CHUNK_FRAMES)
            if decoder.ReachedFinal():
                break
        else:
            raise AlignmentError(
                f"Could not align the transcript to the audio with a beam of {BEAMS[-1]}."
            )

        _, path = decoder.GetBestPath()
        transition_ids, words, _ = GetLinearSymbolSequence(path)
        phones = KalpyAlignment("", transition_ids, words).generate_ctm(
            gmm.transition_model, self._lexicon.phone_table, self._mfcc_computer.frame_shift
        )
        ctm = self._lexicon.phones_to_pronunciations(
            words, phones, transcription=False, text=utterance.transcript
        )
        ctm.update_utterance_boundaries(utterance.segment.begin, utterance.segment.end)
        return ctm

    def align(
        self, wav_path: Path, duration: float, transcript: str, budget: float = math.inf
    ) -> Alignment:
        """Align, raising AlignmentTimeout once decoding runs past budget seconds from the call."""
        deadline = time.monotonic() + budget
        normalized, oovs = self.tokenize(transcript)
        if not normalized.strip():
            raise EmptyTranscriptError("Transcript has no alignable words.")

        span = speech_span(read_samples(wav_path))
        begin, end = span or (0.0, duration)
        utterance = self._utterance(wav_path, begin, end, normalized)
        return _alignment_from_ctm(self._align_utterance(utterance, deadline), oovs, span)


def _alignment_from_ctm(ctm, oovs: list[str], span: Optional[tuple[float, float]]) -> Alignment:
    words: list[WordSpan] = []
    phones: list[PhoneSpan] = []
    for word_index, interval in enumerate(ctm.word_intervals):
        word = WordSpan(interval.label, interval.begin, interval.end)
        words.append(word)
        phones.extend(
            PhoneSpan(p.label, p.begin, p.end, word_index if word.is_speech else None)
            for p in interval.phones
        )
    return Alignment(words=words, phones=phones, oovs=oovs, speech_span=span)
