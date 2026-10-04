"""
Endpoint tests with the aligner replaced by a fake.

The fake returns a fixed alignment that covers the synthetic vowel, which is
enough to exercise validation, the response shape and the error mapping
without a conda environment.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.aligner import Alignment, AlignmentError
from app.worker import AlignerUnavailable, AlignmentTimeout
from tests.conftest import needs_ffmpeg, post_analyze, vowel_alignment


class FakeAligner:
    ready = True

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def start(self):
        pass

    async def wait_ready(self):
        pass

    def shutdown(self):
        pass

    async def align(self, wav_path: Path, duration: float, transcript: str) -> Alignment:
        self.calls.append((wav_path.name, duration, transcript))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def client(monkeypatch):
    def make(aligner):
        monkeypatch.setattr(main, "create_aligner", lambda: aligner)
        return TestClient(main.app)

    return make


@needs_ffmpeg
def test_analyze_returns_prosody_compatible_aggregates(client, vowel_a):
    aligner = FakeAligner(vowel_alignment())
    with client(aligner) as c:
        response = post_analyze(c, vowel_a)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["duration"] == pytest.approx(1.5, abs=0.01)
    assert body["sample_count"] == body["phone_count"] == 4
    assert body["scored_phone_count"] == 4
    assert body["word_count"] == 1
    assert body["oovs"] == []
    assert body["suspicious"] is False
    assert body["speech_coverage"] == pytest.approx(0.867, abs=0.001)
    assert set(body["pitch"]) == {"mean", "median", "stdev", "min", "max", "unit"}
    assert body["pitch"]["mean"] == pytest.approx(140.0, abs=3.0)
    assert set(body["resonance"]) == {"mean", "median", "stdev"}
    assert 0.0 <= body["resonance"]["mean"] <= 1.0
    assert set(body["formants"]) == {"f1", "f2", "f3", "unit"}
    assert "phones" not in body and "words" not in body
    assert aligner.calls[0][2] == "ah"


@needs_ffmpeg
def test_include_phones_adds_detail(client, vowel_a):
    with client(FakeAligner(vowel_alignment())) as c:
        body = post_analyze(c, vowel_a, include_phones=1).json()

    assert body["words"] == [{"word": "ah", "start": 0.1, "end": 1.4}]
    assert [p["phone"] for p in body["phones"]] == ["AA1", "AH0", "AA1", "AH0"]
    first = body["phones"][0]
    assert set(first) == {
        "phone", "word_index", "start", "end", "midpoint",
        "f0", "f1", "f2", "f3", "z", "resonance", "outlier",
    }
    assert first["word_index"] == 1
    assert len(first["z"]) == 4


@needs_ffmpeg
def test_oovs_pass_through(client, vowel_a):
    with client(FakeAligner(vowel_alignment(oovs=["zorblax"]))) as c:
        body = post_analyze(c, vowel_a).json()
    assert body["oovs"] == ["zorblax"]


@needs_ffmpeg
def test_too_short(client, vowel_a_short):
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, vowel_a_short)
    assert response.status_code == 400
    assert "short" in response.json()["detail"]


@needs_ffmpeg
def test_undecodable_audio(client, garbage):
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, garbage)
    assert response.status_code == 400


def test_missing_transcript(client, vowel_a):
    with client(FakeAligner(vowel_alignment())) as c:
        with open(vowel_a, "rb") as f:
            response = c.post("/analyze", files={"audio": ("a.wav", f, "audio/wav")})
    assert response.status_code == 422


@needs_ffmpeg
@pytest.mark.parametrize(
    "error, status",
    [
        (AlignmentError("beam too narrow"), 422),
        (AlignmentTimeout("Alignment exceeded 10 s"), 504),
        (AlignerUnavailable("worker died"), 503),
    ],
)
def test_aligner_failures_map_to_status_codes(client, vowel_a, error, status):
    with client(FakeAligner(error=error)) as c:
        response = post_analyze(c, vowel_a)
    assert response.status_code == status
    assert response.json()["detail"] == str(error)


@needs_ffmpeg
def test_too_few_scored_phones(client, silence):
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, silence)
    assert response.status_code == 422


@needs_ffmpeg
def test_bearer_token_when_configured(client, vowel_a, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "s3cret")
    with client(FakeAligner(vowel_alignment())) as c:
        denied = post_analyze(c, vowel_a)
        c.headers["Authorization"] = "Bearer s3cret"
        allowed = post_analyze(c, vowel_a)
    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_health(client):
    with client(FakeAligner(vowel_alignment())) as c:
        assert c.get("/health").json() == {"status": "healthy", "aligner": "ready"}
