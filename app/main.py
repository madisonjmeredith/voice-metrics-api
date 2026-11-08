"""
voice-metrics-api: pitch and resonance scoring for a recording and its transcript.

Flow: upload → ffmpeg → forced alignment (MFA, resident in a worker process)
→ Praat readings at each phone's midpoint (in a pool of worker processes)
→ z-scores against per-phoneme population statistics → JSON. Recognition
(Whisper, in a worker process of its own) runs beside the alignment and Praat,
and its words are compared with the aligned ones.
"""

import asyncio
import logging
import os
import secrets
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Callable, NamedTuple, Optional

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.aligner import Alignment, AlignmentError, EmptyTranscriptError
from app.analyzer_pool import AnalyzerPool, AnalyzerUnavailable
from app.converter import AudioDecodeError, DecodeTimeout, convert_to_wav, get_audio_duration
from app.measurement import MeasuredPhone
from app.scoring import score
from app.pool import PoolState, retrieve
from app.recognizer_pool import (
    RecognitionTimeout,
    RecognizerBusy,
    RecognizerPool,
    RecognizerUnavailable,
)
from app.request_log import RequestLog
from app.telemetry import configure_logging, note, timed
from app.transcript_match import normalize, transcript_words, word_coverage
from app.worker import AlignerBusy, AlignerPool, AlignerUnavailable, AlignmentTimeout

configure_logging()
logger = logging.getLogger(__name__)

MIN_DURATION = 1.0
MAX_DURATION = 60.0
DECODE_TIMEOUT = 5.0
MIN_SCORED_PHONES = 3
MAX_LEXICON_WORDS = 1000
MAX_WORD_LENGTH = 200
MAX_TRANSCRIPT_LENGTH = 4000
MAX_BODY_BYTES = 16 * 1024 * 1024
SOURCE_URL = "https://github.com/madisonjmeredith/voice-metrics-api"


class ConfigError(RuntimeError):
    """The environment doesn't say how requests are authenticated."""


def _api_token() -> Optional[str]:
    """API_TOKEN, or None when ALLOW_UNAUTHENTICATED=1 turns auth off; raises on neither or both."""
    token = os.environ.get("API_TOKEN") or None
    allow = os.environ.get("ALLOW_UNAUTHENTICATED", "")
    if allow not in ("", "1"):
        raise ConfigError("ALLOW_UNAUTHENTICATED must be 1 or unset")
    if token and allow:
        raise ConfigError("Set API_TOKEN or ALLOW_UNAUTHENTICATED=1, not both")
    if not token and not allow:
        raise ConfigError("API_TOKEN is not set. Set it, or set ALLOW_UNAUTHENTICATED=1 to run without auth")
    return token


def create_aligner() -> AlignerPool:
    return AlignerPool()


def create_analyzer() -> AnalyzerPool:
    return AnalyzerPool()


def create_recognizer() -> RecognizerPool:
    return RecognizerPool()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        app.state.api_token = _api_token()
    except ConfigError as exc:
        logger.error("%s", exc)
        raise
    if app.state.api_token is None:
        logger.warning("auth=off: /analyze and /lexicon take requests without a token")
    else:
        logger.info("auth=bearer: /analyze and /lexicon need the API_TOKEN bearer token")
    app.state.aligner = create_aligner()
    app.state.analyzer = create_analyzer()
    app.state.recognizer = create_recognizer()
    pools = (app.state.aligner, app.state.analyzer, app.state.recognizer)
    for pool in pools:
        pool.start()
    logger.info(
        "voice-metrics-api %s is loading the aligner, the Praat workers, and the recognizer",
        __version__,
    )
    try:
        await asyncio.gather(*(pool.wait_ready() for pool in pools))
        yield
    finally:
        for pool in pools:
            pool.shutdown()


class BodyLimit:
    """Refuses a body over MAX_BODY_BYTES as it arrives: FastAPI reads all of it before any dependency."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length", "")
        declared_too_large = declared.isdigit() and int(declared) > MAX_BODY_BYTES
        received = 0

        async def limited_receive():
            nonlocal received
            if declared_too_large:
                raise UploadTooLargeError()
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_BODY_BYTES:
                raise UploadTooLargeError()
            return message

        await self.app(scope, limited_receive, send)


app = FastAPI(
    title="voice-metrics-api",
    description="Pitch and resonance scoring from a recording and its transcript",
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(BodyLimit)
app.add_middleware(RequestLog, quiet=frozenset({"/health", "/ready"}))


class InvalidTokenError(Exception):
    """The bearer token is missing or does not match API_TOKEN."""


# HTTPException: FastAPI wraps anything else raised while reading the body in a generic 400.
class UploadTooLargeError(StarletteHTTPException):
    """The request body is larger than MAX_BODY_BYTES."""

    def __init__(self) -> None:
        super().__init__(400, f"Upload too large. Keep it under {MAX_BODY_BYTES / 2**20:.0f} MB.")

    def __str__(self) -> str:
        return self.detail


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
    UploadTooLargeError: ErrorKind(400, "upload_too_large", False),
    EmptyTranscriptError: ErrorKind(400, "transcript_empty", False),
    AlignmentError: ErrorKind(422, "alignment_failed", False),
    TooLittleSpeechError: ErrorKind(422, "too_little_speech", False),
    TooLittleVoicingError: ErrorKind(422, "too_little_voicing", False),
    DecodeTimeout: ErrorKind(504, "decode_timeout", True),
    AlignmentTimeout: ErrorKind(504, "alignment_timeout", True),
    AlignerUnavailable: ErrorKind(503, "aligner_unavailable", True),
    AlignerBusy: ErrorKind(503, "aligner_busy", True),
    AnalyzerUnavailable: ErrorKind(503, "analyzer_unavailable", True),
}

FRAMEWORK_ERROR_CODES = {400: "invalid_request", 404: "not_found", 405: "method_not_allowed"}

RECOGNITION_FAILURES: dict[type[Exception], str] = {
    RecognizerBusy: "recognizer_busy",
    RecognitionTimeout: "recognition_timeout",
    RecognizerUnavailable: "recognizer_unavailable",
}


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
    """Shared-secret check against API_TOKEN, skipped only when ALLOW_UNAUTHENTICATED=1."""
    token = app.state.api_token
    if token is None:
        return
    # compare_digest raises TypeError on a non-ASCII str, which a header can carry.
    if not secrets.compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
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
    wav_path = convert_to_wav(
        input_path, tmp_path / "audio.wav", max_seconds=MAX_DURATION + 1, timeout=DECODE_TIMEOUT
    )
    return wav_path, get_audio_duration(wav_path)


async def _recognition(task: asyncio.Task, alignment: Alignment) -> Optional[dict]:
    """What the recognizer heard and how much of the transcript that covers; None if it couldn't say."""
    try:
        text = await task
    except tuple(RECOGNITION_FAILURES) as exc:
        note(recognition=RECOGNITION_FAILURES[type(exc)])
        return None
    words = transcript_words(alignment)
    figures = {"word_coverage": _round(word_coverage(words, normalize(text)), 3), "compared_words": len(words)}
    note(recognition="ok", **figures)
    return {"text": text, **figures}


def _probe(passes: Callable[[PoolState], bool], verdicts: tuple[str, str]) -> JSONResponse:
    deciding = {"aligner": app.state.aligner.state, "analyzer": app.state.analyzer.state}
    states = {**deciding, "recognizer": app.state.recognizer.state}
    note(**states)
    ok = all(passes(state) for state in deciding.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"version": __version__, "status": verdicts[0] if ok else verdicts[1], **states},
    )


@app.get("/health")
async def health_check():
    """Liveness: 503 while the aligner or the analyzer is stalled or down."""
    return _probe(lambda state: state.live, ("healthy", "unhealthy"))


@app.get("/ready")
async def readiness_check():
    """Readiness: 200 only once the aligner and the analyzer have loaded."""
    return _probe(lambda state: state is PoolState.READY, ("ready", "not_ready"))


@app.post("/analyze", dependencies=[Depends(require_token)])
async def analyze(
    audio: UploadFile = File(..., description="Audio in any format ffmpeg decodes"),
    transcript: str = Form(
        ..., max_length=MAX_TRANSCRIPT_LENGTH, description="The text that was read aloud"
    ),
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

        recognizing = asyncio.create_task(app.state.recognizer.recognize(wav_path))
        recognizing.add_done_callback(retrieve)
        try:
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
            recognition = await _recognition(recognizing, alignment)
        finally:
            recognizing.cancel()

    result = {
        "version": __version__,
        "duration": round(duration, 2),
        "sample_count": aggregates["phone_count"],
        "phone_count": aggregates["phone_count"],
        "scored_phone_count": aggregates["scored_phone_count"],
        "word_count": len(alignment.speech_words),
        "oovs": alignment.oovs,
        "speech_coverage": round(alignment.speech_coverage(duration), 3),
        "recognition": recognition,
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
        "source": SOURCE_URL,
        "endpoints": {
            "POST /analyze": "Score a recording against its transcript",
            "POST /lexicon": "Report which words the pronunciation dictionary has",
            "GET /health": "Liveness check",
            "GET /ready": "Readiness check",
        },
    }
