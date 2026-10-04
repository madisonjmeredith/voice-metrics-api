"""
voice-metrics-api: pitch and resonance scoring for a recording and its transcript.

Flow: upload → ffmpeg → forced alignment (MFA, resident in a worker process)
→ Praat readings at each phone's midpoint → z-scores against per-phoneme
population statistics → JSON.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.aligner import Alignment, AlignmentError, EmptyTranscriptError
from app.analyzer import AudioAnalyzer
from app.converter import AudioDecodeError, convert_to_wav, get_audio_duration
from app.measurement import MeasuredPhone, measure_phones
from app.scoring import score
from app.worker import AlignerPool, AlignerUnavailable, AlignmentTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

MIN_DURATION = 1.0
MAX_DURATION = 60.0
MIN_SCORED_PHONES = 3

API_TOKEN = os.environ.get("API_TOKEN")


def create_aligner() -> AlignerPool:
    return AlignerPool()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.aligner = create_aligner()
    app.state.aligner.start()
    logger.info("Warming up the aligner")
    await app.state.aligner.wait_ready()
    logger.info("Aligner ready")
    yield
    app.state.aligner.shutdown()


app = FastAPI(
    title="voice-metrics-api",
    description="Pitch and resonance scoring from a recording and its transcript",
    version="1.0.0",
    lifespan=lifespan,
)

# Anything a caller can cause, or the aligner can do to itself, has its own
# exception type; the mapping to a status code lives here and nowhere else.
ERROR_STATUS = {
    AudioDecodeError: 400,
    EmptyTranscriptError: 400,
    AlignmentError: 422,
    AlignmentTimeout: 504,
    AlignerUnavailable: 503,
}


def _status_handler(status: int):
    async def handler(request: Request, exc: Exception):
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    return handler


for _error_type, _status in ERROR_STATUS.items():
    app.add_exception_handler(_error_type, _status_handler(_status))


def require_token(authorization: Optional[str] = Header(None)) -> None:
    """Shared-secret check, skipped entirely when no API_TOKEN is configured."""
    if API_TOKEN is None:
        return
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


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
    return {"status": "healthy", "aligner": "ready" if aligner.ready else "warming"}


@app.post("/analyze", dependencies=[Depends(require_token)])
async def analyze(
    audio: UploadFile = File(..., description="Audio in any format ffmpeg decodes"),
    transcript: str = Form(..., description="The text that was read aloud"),
    include_phones: bool = Query(False, description="Return per-word and per-phone detail"),
):
    with tempfile.TemporaryDirectory() as tmp_dir:
        wav_path, duration = await run_in_threadpool(_save_as_wav, audio, Path(tmp_dir))
        if duration < MIN_DURATION:
            raise HTTPException(400, f"Audio too short. Record at least {MIN_DURATION:.0f} second.")
        if duration > MAX_DURATION:
            raise HTTPException(400, f"Audio too long. Keep it under {MAX_DURATION:.0f} seconds.")

        # The Praat tracks need only the WAV, so they are computed while the
        # aligner process works.
        alignment, analyzer = await asyncio.gather(
            app.state.aligner.align(wav_path, duration, transcript),
            run_in_threadpool(AudioAnalyzer, wav_path),
        )
        phones = measure_phones(analyzer, alignment)
        aggregates = score(phones)

    if aggregates["scored_phone_count"] < MIN_SCORED_PHONES:
        raise HTTPException(
            422, "Too little scorable speech. Speak clearly and make sure the transcript matches."
        )

    result = {
        "duration": round(duration, 2),
        "sample_count": aggregates["phone_count"],
        "phone_count": aggregates["phone_count"],
        "scored_phone_count": aggregates["scored_phone_count"],
        "word_count": len(alignment.speech_words),
        "oovs": alignment.oovs,
        "suspicious": alignment.suspicious,
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


@app.get("/")
async def root():
    return {
        "name": "voice-metrics-api",
        "endpoints": {
            "POST /analyze": "Score a recording against its transcript",
            "GET /health": "Liveness check",
        },
    }
