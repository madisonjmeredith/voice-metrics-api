"""
voice-metrics-api: pitch and resonance scoring for a recording and its transcript.

Flow: upload → ffmpeg → forced alignment (MFA, resident in a worker process)
→ Praat readings at each phone's midpoint (in a pool of worker processes)
→ z-scores against per-phoneme population statistics → JSON.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, NamedTuple, Optional

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.aligner import Alignment, AlignmentError, EmptyTranscriptError
from app.analyzer_pool import AnalyzerPool, AnalyzerUnavailable
from app.converter import AudioDecodeError, convert_to_wav, get_audio_duration
from app.measurement import MeasuredPhone
from app.scoring import score
from app.request_log import RequestLog
from app.telemetry import configure_logging, note, timed
from app.worker import AlignerBusy, AlignerPool, AlignerUnavailable, AlignmentTimeout

configure_logging()
logger = logging.getLogger(__name__)

MIN_DURATION = 1.0
MAX_DURATION = 60.0
MIN_SCORED_PHONES = 3
MAX_LEXICON_WORDS = 1000
MAX_WORD_LENGTH = 200

API_TOKEN = os.environ.get("API_TOKEN")


def create_aligner() -> AlignerPool:
    return AlignerPool()


def create_analyzer() -> AnalyzerPool:
    return AnalyzerPool()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.aligner = create_aligner()
    app.state.analyzer = create_analyzer()
    app.state.aligner.start()
    app.state.analyzer.start()
    logger.info("Warming up the aligner and the Praat workers")
    await asyncio.gather(app.state.aligner.wait_ready(), app.state.analyzer.wait_ready())
    logger.info("Aligner and Praat workers ready")
    yield
    app.state.aligner.shutdown()
    app.state.analyzer.shutdown()


app = FastAPI(
    title="voice-metrics-api",
    description="Pitch and resonance scoring from a recording and its transcript",
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(RequestLog, quiet=frozenset({"/health"}))


class InvalidTokenError(Exception):
    """The bearer token is missing or does not match API_TOKEN."""


class AudioTooShortError(Exception):
    """The decoded audio is shorter than MIN_DURATION."""


class AudioTooLongError(Exception):
    """The decoded audio is longer than MAX_DURATION."""


class TooLittleSpeechError(Exception):
    """Fewer than MIN_SCORED_PHONES vowel and sonorant phones went into the resonance statistics."""


class TooLittleVoicingError(Exception):
    """Fewer than MIN_SCORED_PHONES phones went into the pitch statistics."""


class ErrorKind(NamedTuple):
    status: int
    code: str
    retryable: bool


# Callers branch on code and retryable: never rename a code; False is a verdict on the audio or transcript.
ERRORS: dict[type[Exception], ErrorKind] = {
    InvalidTokenError: ErrorKind(401, "unauthorized", True),
    AudioDecodeError: ErrorKind(400, "audio_undecodable", False),
    AudioTooShortError: ErrorKind(400, "audio_too_short", False),
    AudioTooLongError: ErrorKind(400, "audio_too_long", False),
    EmptyTranscriptError: ErrorKind(400, "transcript_empty", False),
    AlignmentError: ErrorKind(422, "alignment_failed", False),
    TooLittleSpeechError: ErrorKind(422, "too_little_speech", False),
    TooLittleVoicingError: ErrorKind(422, "too_little_voicing", False),
    AlignmentTimeout: ErrorKind(504, "alignment_timeout", True),
    AlignerUnavailable: ErrorKind(503, "aligner_unavailable", True),
    AlignerBusy: ErrorKind(503, "aligner_busy", True),
    AnalyzerUnavailable: ErrorKind(503, "analyzer_unavailable", True),
}

FRAMEWORK_ERROR_CODES = {400: "invalid_request", 404: "not_found", 405: "method_not_allowed"}


def _error_response(status: int, detail, code: str, retryable: bool, headers=None) -> JSONResponse:
    note(code=code)
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "code": code, "retryable": retryable},
        headers=headers,
    )


def _error_handler(kind: ErrorKind):
    async def handler(request: Request, exc: Exception):
        return _error_response(kind.status, str(exc), kind.code, kind.retryable)

    return handler


for _error_type, _kind in ERRORS.items():
    app.add_exception_handler(_error_type, _error_handler(_kind))


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    return _error_response(422, jsonable_encoder(exc.errors()), "invalid_request", True)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    code = FRAMEWORK_ERROR_CODES.get(exc.status_code, "http_error")
    return _error_response(exc.status_code, exc.detail, code, True, exc.headers)


def require_token(authorization: Optional[str] = Header(None)) -> None:
    """Shared-secret check, skipped entirely when no API_TOKEN is configured."""
    if API_TOKEN is None:
        return
    if authorization != f"Bearer {API_TOKEN}":
        raise InvalidTokenError("Invalid or missing bearer token")


def _round(value, digits: int):
    return None if value is None else round(value, digits)


def _round_stats(stats: dict, digits: int = 1) -> dict:
    return {key: _round(value, digits) for key, value in stats.items()}


def _phone_payload(phone: MeasuredPhone) -> dict:
    return {
        "phone": phone.label,
        "word_index": phone.span.word_index,
        "start": _round(phone.span.start, 3),
        "end": _round(phone.span.end, 3),
        "midpoint": _round(phone.span.midpoint, 3),
        "f0": _round(phone.F[0], 1),
        "f1": _round(phone.F[1], 1),
        "f2": _round(phone.F[2], 1),
        "f3": _round(phone.F[3], 1),
        "z": None if phone.z is None else [_round(v, 3) for v in phone.z],
        "resonance": _round(phone.resonance, 4),
        "outlier": phone.outlier,
    }


def _word_payload(alignment: Alignment) -> list[dict]:
    return [
        {"word": w.label, "start": _round(w.start, 3), "end": _round(w.end, 3)}
        for w in alignment.speech_words
    ]


def _save_as_wav(upload: UploadFile, tmp_path: Path) -> tuple[Path, float]:
    """Stream the upload to disk, transcode it, and read back its duration."""
    input_path = tmp_path / "input_audio"
    with open(input_path, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    wav_path = convert_to_wav(input_path, tmp_path / "audio.wav")
    return wav_path, get_audio_duration(wav_path)


@app.get("/health")
async def health_check():
    aligner: AlignerPool = app.state.aligner
    return {
        "version": __version__,
        "status": "healthy",
        "aligner": "ready" if aligner.ready else "warming",
    }


@app.post("/analyze", dependencies=[Depends(require_token)])
async def analyze(
    audio: UploadFile = File(..., description="Audio in any format ffmpeg decodes"),
    transcript: str = Form(..., description="The text that was read aloud"),
    include_phones: bool = Query(False, description="Return per-word and per-phone detail"),
):
    with tempfile.TemporaryDirectory() as tmp_dir:
        with timed("decode"):
            wav_path, duration = await run_in_threadpool(_save_as_wav, audio, Path(tmp_dir))
        note(audio_s=round(duration, 1))
        if duration < MIN_DURATION:
            raise AudioTooShortError(f"Audio too short. Record at least {MIN_DURATION:.0f} second.")
        if duration > MAX_DURATION:
            raise AudioTooLongError(f"Audio too long. Keep it under {MAX_DURATION:.0f} seconds.")

        alignment = await app.state.aligner.align(wav_path, duration, transcript)
        note(words=len(alignment.speech_words), oovs=len(alignment.oovs))
        phones = await app.state.analyzer.measure(wav_path, alignment)
        aggregates = score(phones)

    if aggregates["readings"]["balanced"] < MIN_SCORED_PHONES:
        raise TooLittleSpeechError(
            "Too little scorable speech. Speak clearly and make sure the transcript matches."
        )
    if aggregates["readings"]["pitch"] < MIN_SCORED_PHONES:
        raise TooLittleVoicingError(
            "Too little voiced speech to measure pitch. Check the microphone and speak aloud."
        )

    result = {
        "version": __version__,
        "duration": round(duration, 2),
        "sample_count": aggregates["phone_count"],
        "phone_count": aggregates["phone_count"],
        "scored_phone_count": aggregates["scored_phone_count"],
        "word_count": len(alignment.speech_words),
        "oovs": alignment.oovs,
        "speech_coverage": round(alignment.speech_coverage(duration), 3),
        "pitch": {**_round_stats(aggregates["pitch"]), "unit": "Hz"},
        "resonance": _round_stats(aggregates["resonance"], 4),
        "formants": {
            **{key: _round_stats(value) for key, value in aggregates["formants"].items()},
            "unit": "Hz",
        },
    }
    if include_phones:
        result["words"] = _word_payload(alignment)
        result["phones"] = [_phone_payload(p) for p in phones]
    return result


class LexiconRequest(BaseModel):
    words: list[Annotated[str, StringConstraints(max_length=MAX_WORD_LENGTH)]] = Field(
        min_length=1, max_length=MAX_LEXICON_WORDS
    )


@app.post("/lexicon", dependencies=[Depends(require_token)])
async def lexicon(body: LexiconRequest):
    note(entries=len(body.words))
    tokenized = await app.state.aligner.tokenize(body.words)
    return {
        "version": __version__,
        "words": [
            {
                "word": word,
                "normalized": entry.normalized,
                "oovs": entry.oovs,
                "in_vocabulary": entry.in_vocabulary,
            }
            for word, entry in zip(body.words, tokenized)
        ]
    }


@app.get("/")
async def root():
    return {
        "name": "voice-metrics-api",
        "version": __version__,
        "endpoints": {
            "POST /analyze": "Score a recording against its transcript",
            "POST /lexicon": "Report which words the pronunciation dictionary has",
            "GET /health": "Liveness check",
        },
    }
