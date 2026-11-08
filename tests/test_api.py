"""
Endpoint tests with the aligner replaced by a fake.

The fake returns a fixed alignment that covers the synthetic vowel, which is
enough to exercise validation, the response shape and the error mapping
without a conda environment.
"""

import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import __version__
from app.aligner import Alignment, AlignmentError, EmptyTranscriptError, Tokenized
import app.analyzer_pool as analyzer_pool
from app.analyzer_pool import AnalyzerUnavailable
from app.converter import AudioDecodeError, DecodeTimeout
from app.measurement import MeasuredPhone
from app.pool import PoolState
from app.worker import AlignerBusy, AlignerUnavailable, AlignmentTimeout
from tests.conftest import needs_ffmpeg, post_analyze, use_token, vowel_alignment

NAME = "Quillonae"


class FakeAligner:
    state = PoolState.READY

    def __init__(self, result=None, error=None, lexicon=None, load_error=None):
        self.result = result
        self.error = error
        self.lexicon = lexicon or {}
        self.load_error = load_error
        self.calls = []
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    async def wait_ready(self):
        if self.load_error is not None:
            raise self.load_error

    def shutdown(self):
        self.stopped = True

    async def align(self, wav_path: Path, duration: float, transcript: str) -> Alignment:
        self.calls.append((wav_path.name, duration, transcript))
        if self.error is not None:
            raise self.error
        return self.result

    async def tokenize(self, texts: list[str]) -> list[Tokenized]:
        if self.error is not None:
            raise self.error
        return [Tokenized(*self.lexicon.get(text, (text.lower(), []))) for text in texts]


class InlineAnalyzer:
    """Real Praat readings, taken in the test process so no test spawns a worker."""

    state = PoolState.READY

    def __init__(self, error=None):
        self.error = error
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    async def wait_ready(self):
        pass

    def shutdown(self):
        self.stopped = True

    async def measure(self, wav_path: Path, alignment: Alignment) -> list[MeasuredPhone]:
        if self.error is not None:
            raise self.error
        return analyzer_pool._measure(wav_path, alignment)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED", "1")

    def make(aligner, analyzer=None, **kwargs):
        monkeypatch.setattr(main, "create_aligner", lambda: aligner)
        monkeypatch.setattr(main, "create_analyzer", lambda: analyzer or InlineAnalyzer())
        return TestClient(main.app, **kwargs)

    return make


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.INFO)
    return caplog


def request_lines(logs) -> list[logging.LogRecord]:
    return [record for record in logs.records if record.name == "app.request"]


@needs_ffmpeg
def test_analyze_returns_prosody_compatible_aggregates(client, vowel_a):
    aligner = FakeAligner(vowel_alignment())
    with client(aligner) as c:
        response = post_analyze(c, vowel_a)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == __version__
    assert body["duration"] == pytest.approx(1.5, abs=0.01)
    assert body["sample_count"] == body["phone_count"] == 4
    assert body["scored_phone_count"] == 4
    assert body["word_count"] == 1
    assert body["oovs"] == []
    assert body["speech_coverage"] == pytest.approx(0.867, abs=0.001)
    assert set(body["pitch"]) == {"mean", "median", "stdev", "min", "max", "unit"}
    assert body["pitch"]["mean"] == pytest.approx(140.0, abs=3.0)
    assert set(body["resonance"]) == {"mean", "median", "stdev", "balanced"}
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


def error_of(response) -> tuple:
    body = response.json()
    return (response.status_code, body["code"], body["retryable"])


def test_error_codes_are_the_published_contract():
    assert {error.__name__: tuple(kind) for error, kind in main.ERRORS.items()} == {
        "InvalidTokenError": (401, "unauthorized", True),
        "AudioDecodeError": (400, "audio_undecodable", False),
        "AudioTooShortError": (400, "audio_too_short", False),
        "AudioTooLongError": (400, "audio_too_long", False),
        "UploadTooLargeError": (400, "upload_too_large", False),
        "EmptyTranscriptError": (400, "transcript_empty", False),
        "AlignmentError": (422, "alignment_failed", False),
        "TooLittleSpeechError": (422, "too_little_speech", False),
        "TooLittleVoicingError": (422, "too_little_voicing", False),
        "DecodeTimeout": (504, "decode_timeout", True),
        "AlignmentTimeout": (504, "alignment_timeout", True),
        "AlignerUnavailable": (503, "aligner_unavailable", True),
        "AlignerBusy": (503, "aligner_busy", True),
        "AnalyzerUnavailable": (503, "analyzer_unavailable", True),
    }
    assert main.FRAMEWORK_ERROR_CODES == {
        400: "invalid_request",
        404: "not_found",
        405: "method_not_allowed",
    }
    codes = [kind.code for kind in main.ERRORS.values()]
    assert len(set(codes)) == len(codes)


@needs_ffmpeg
def test_too_short(client, vowel_a_short):
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, vowel_a_short)
    assert error_of(response) == tuple(main.ERRORS[main.AudioTooShortError])
    assert "short" in response.json()["detail"]


@needs_ffmpeg
def test_too_long(client, vowel_a, monkeypatch):
    monkeypatch.setattr(main, "MAX_DURATION", 1.0)
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, vowel_a)
    assert error_of(response) == tuple(main.ERRORS[main.AudioTooLongError])
    assert "long" in response.json()["detail"]


@needs_ffmpeg
def test_a_slow_decode_answers_504(client, vowel_a, monkeypatch):
    monkeypatch.setattr(main, "DECODE_TIMEOUT", 0.001)
    aligner = FakeAligner(vowel_alignment())
    with client(aligner) as c:
        response = post_analyze(c, vowel_a)
    assert error_of(response) == tuple(main.ERRORS[DecodeTimeout])
    assert aligner.calls == []


@needs_ffmpeg
def test_undecodable_audio(client, garbage):
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, garbage)
    assert error_of(response) == tuple(main.ERRORS[AudioDecodeError])
    assert isinstance(response.json()["detail"], str)


def test_missing_transcript(client, vowel_a):
    with client(FakeAligner(vowel_alignment())) as c:
        with open(vowel_a, "rb") as f:
            response = c.post("/analyze", files={"audio": ("a.wav", f, "audio/wav")})
    assert error_of(response) == (422, "invalid_request", True)
    assert [error["loc"] for error in response.json()["detail"]] == [["body", "transcript"]]


def test_a_transcript_over_the_cap_is_refused_before_decoding(client, vowel_a):
    aligner = FakeAligner(vowel_alignment())
    with client(aligner) as c:
        response = post_analyze(c, vowel_a, transcript="a" * (main.MAX_TRANSCRIPT_LENGTH + 1))
    assert error_of(response) == (422, "invalid_request", True)
    assert [error["loc"] for error in response.json()["detail"]] == [["body", "transcript"]]
    assert aligner.calls == []


@needs_ffmpeg
def test_a_transcript_at_the_cap_is_aligned(client, vowel_a):
    aligner = FakeAligner(vowel_alignment())
    with client(aligner) as c:
        response = post_analyze(c, vowel_a, transcript="a" * main.MAX_TRANSCRIPT_LENGTH)
    assert response.status_code == 200
    assert len(aligner.calls[0][2]) == main.MAX_TRANSCRIPT_LENGTH


def test_a_body_over_the_cap_is_refused_before_decoding(client, vowel_a, monkeypatch):
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 1000)
    aligner = FakeAligner(vowel_alignment())
    with client(aligner) as c:
        response = post_analyze(c, vowel_a)
    assert error_of(response) == tuple(main.ERRORS[main.UploadTooLargeError])
    assert "too large" in response.json()["detail"]
    assert aligner.calls == []


def test_a_streamed_body_is_refused_once_it_passes_the_cap(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 1000)

    def chunks():
        yield b'--b\r\nContent-Disposition: form-data; name="audio"; filename="a.webm"\r\n\r\n'
        for _ in range(10):
            yield b"\0" * 500

    with client(FakeAligner(vowel_alignment())) as c:
        response = c.post(
            "/analyze", content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=b"}
        )
    assert "content-length" not in response.request.headers
    assert error_of(response) == tuple(main.ERRORS[main.UploadTooLargeError])


def test_the_body_cap_applies_before_the_token_check(client, vowel_a, monkeypatch):
    use_token(monkeypatch)
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 1000)
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, vowel_a)
    assert error_of(response) == tuple(main.ERRORS[main.UploadTooLargeError])


@needs_ffmpeg
@pytest.mark.parametrize(
    "error",
    [
        EmptyTranscriptError("Transcript has no alignable words."),
        AlignmentError("beam too narrow"),
        AlignmentTimeout("Alignment exceeded 10 s"),
        AlignerUnavailable("worker died"),
    ],
)
def test_aligner_failures_map_to_status_and_code(client, vowel_a, error):
    with client(FakeAligner(error=error)) as c:
        response = post_analyze(c, vowel_a)
    assert error_of(response) == tuple(main.ERRORS[type(error)])
    assert response.json()["detail"] == str(error)


@needs_ffmpeg
def test_a_lost_praat_worker_answers_503(client, vowel_a):
    error = AnalyzerUnavailable("A child process terminated abruptly")
    with client(FakeAligner(vowel_alignment()), InlineAnalyzer(error=error)) as c:
        response = post_analyze(c, vowel_a)
    assert error_of(response) == tuple(main.ERRORS[AnalyzerUnavailable])
    assert response.json()["detail"] == str(error)


@needs_ffmpeg
def test_too_few_scored_phones(client, silence):
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, silence)
    assert error_of(response) == tuple(main.ERRORS[main.TooLittleSpeechError])


@needs_ffmpeg
def test_too_few_vowels_and_sonorants(client, vowel_a):
    with client(FakeAligner(vowel_alignment(labels=("S", "T", "SH", "AA1")))) as c:
        response = post_analyze(c, vowel_a)
    assert error_of(response) == tuple(main.ERRORS[main.TooLittleSpeechError])


@needs_ffmpeg
def test_hiss_is_too_little_voicing(client, hiss):
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, hiss)
    assert error_of(response) == tuple(main.ERRORS[main.TooLittleVoicingError])


def voice_only(monkeypatch, count: int) -> None:
    """Keep the pitch reading of the first `count` phones and drop the rest."""
    measure = InlineAnalyzer.measure

    async def measure_some(self, wav_path, alignment):
        phones = await measure(self, wav_path, alignment)
        for phone in phones[count:]:
            phone.F[0] = None
        return phones

    monkeypatch.setattr(InlineAnalyzer, "measure", measure_some)


@needs_ffmpeg
def test_one_voiced_phone_short_is_too_little_voicing(client, vowel_a, monkeypatch):
    voice_only(monkeypatch, main.MIN_SCORED_PHONES - 1)
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, vowel_a)
    assert error_of(response) == tuple(main.ERRORS[main.TooLittleVoicingError])


@needs_ffmpeg
def test_enough_voiced_phones_leave_no_null_figure(client, vowel_a, monkeypatch):
    voice_only(monkeypatch, main.MIN_SCORED_PHONES)
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, vowel_a)

    assert response.status_code == 200, response.text
    body = response.json()
    for stats in (body["pitch"], body["resonance"], *(body["formants"][f] for f in ("f1", "f2", "f3"))):
        assert None not in stats.values()


@pytest.mark.parametrize(
    "path, status, code, allow",
    [("/nowhere", 404, "not_found", None), ("/analyze", 405, "method_not_allowed", "POST")],
)
def test_routing_errors_carry_a_code(client, path, status, code, allow):
    with client(FakeAligner()) as c:
        response = c.get(path)
    assert error_of(response) == (status, code, True)
    assert response.headers.get("allow") == allow


@needs_ffmpeg
def test_bearer_token_when_configured(client, vowel_a, monkeypatch):
    use_token(monkeypatch)
    with client(FakeAligner(vowel_alignment())) as c:
        denied = post_analyze(c, vowel_a)
        c.headers["Authorization"] = "Bearer s3cret"
        allowed = post_analyze(c, vowel_a)
    assert error_of(denied) == tuple(main.ERRORS[main.InvalidTokenError])
    assert allowed.status_code == 200


def test_a_non_ascii_token_is_unauthorized(client, monkeypatch):
    use_token(monkeypatch)
    with client(FakeAligner()) as c:
        response = c.post(
            "/lexicon", json={"words": ["hello"]}, headers={"Authorization": b"Bearer caf\xc3\xa9"}
        )
    assert error_of(response) == tuple(main.ERRORS[main.InvalidTokenError])


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"API_TOKEN": ""},
        {"API_TOKEN": "s3cret", "ALLOW_UNAUTHENTICATED": "1"},
        {"ALLOW_UNAUTHENTICATED": "true"},
    ],
)
def test_it_refuses_to_start_unless_the_auth_mode_is_clear(client, monkeypatch, logs, env):
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    aligner, analyzer = FakeAligner(), InlineAnalyzer()
    with pytest.raises(main.ConfigError):
        with client(aligner, analyzer):
            pass
    assert not aligner.started and not analyzer.started
    assert [r.levelno for r in logs.records if r.name == "app.main"] == [logging.ERROR]
    assert "s3cret" not in logs.text


@pytest.mark.parametrize(
    "token, level, mode",
    [("s3cret", logging.INFO, "auth=bearer"), (None, logging.WARNING, "auth=off")],
)
def test_startup_logs_the_auth_mode(client, monkeypatch, logs, token, level, mode):
    if token is not None:
        use_token(monkeypatch, token)
    with client(FakeAligner()):
        pass
    [line] = [r for r in logs.records if r.getMessage().startswith(f"{mode}:")]
    assert line.levelno == level
    assert token is None or token not in logs.text


@pytest.mark.parametrize(
    "aligner, analyzer, live, ready",
    [
        ("ready", "ready", 200, 200),
        ("warming", "ready", 200, 503),
        ("ready", "warming", 200, 503),
        ("stalled", "ready", 503, 503),
        ("down", "warming", 503, 503),
        ("ready", "down", 503, 503),
    ],
)
def test_the_probes_answer_for_both_pools(client, aligner, analyzer, live, ready):
    fake, inline = FakeAligner(), InlineAnalyzer()
    with client(fake, inline) as c:
        fake.state, inline.state = PoolState(aligner), PoolState(analyzer)
        health, readiness = c.get("/health"), c.get("/ready")

    states = {"aligner": aligner, "analyzer": analyzer}
    assert (health.status_code, readiness.status_code) == (live, ready)
    assert health.json().items() >= states.items() and readiness.json().items() >= states.items()


def test_a_probe_body_names_its_verdict(client):
    fake = FakeAligner()
    with client(fake) as c:
        passing = [c.get("/health").json()["status"], c.get("/ready").json()["status"]]
        fake.state = PoolState.STALLED
        failing = [c.get("/health").json()["status"], c.get("/ready").json()["status"]]
    assert (passing, failing) == (["healthy", "ready"], ["unhealthy", "not_ready"])


def test_a_failing_probe_is_logged_with_both_states(client, logs):
    fake = FakeAligner()
    with client(fake) as c:
        fake.state = PoolState.WARMING
        c.get("/health")
        c.get("/ready")
    [line] = request_lines(logs)
    assert line.levelno == logging.WARNING
    assert re.fullmatch(r"GET /ready 503 total_ms=\d+ aligner=warming analyzer=ready", line.getMessage())


def test_every_endpoint_reports_the_one_version(client):
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert main.app.openapi()["info"]["version"] == __version__
    with client(FakeAligner()) as c:
        assert c.get("/").json()["version"] == __version__
        assert c.get("/health").json()["version"] == __version__
        assert c.get("/ready").json()["version"] == __version__
        assert c.post("/lexicon", json={"words": ["hello"]}).json()["version"] == __version__


def test_root_links_to_the_source(client):
    with client(FakeAligner()) as c:
        assert c.get("/").json()["source"] == "https://github.com/madisonjmeredith/voice-metrics-api"


def test_lexicon_reports_each_word_in_order(client):
    lexicon = {"Zorblax": ("zorblax", ["zorblax"]), "...": ("", [])}
    with client(FakeAligner(lexicon=lexicon)) as c:
        response = c.post("/lexicon", json={"words": ["Hello", *lexicon]})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "version": __version__,
        "words": [
            {"word": "Hello", "normalized": "hello", "oovs": [], "in_vocabulary": True},
            {"word": "Zorblax", "normalized": "zorblax", "oovs": ["zorblax"], "in_vocabulary": False},
            {"word": "...", "normalized": "", "oovs": [], "in_vocabulary": False},
        ]
    }


def test_lexicon_accepts_a_batch_at_the_caps(client):
    words = ["a" * main.MAX_WORD_LENGTH] * main.MAX_LEXICON_WORDS
    with client(FakeAligner()) as c:
        response = c.post("/lexicon", json={"words": words})
    assert response.status_code == 200
    assert len(response.json()["words"]) == main.MAX_LEXICON_WORDS


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"words": []},
        {"words": "hello"},
        {"words": [3]},
        {"words": ["a"] * (main.MAX_LEXICON_WORDS + 1)},
        {"words": ["a" * (main.MAX_WORD_LENGTH + 1)]},
    ],
    ids=["no words", "empty list", "not a list", "not a string", "too many", "too long"],
)
def test_lexicon_rejects_a_malformed_batch(client, body):
    with client(FakeAligner()) as c:
        response = c.post("/lexicon", json=body)
    assert error_of(response) == (422, "invalid_request", True)


@pytest.mark.parametrize(
    "error",
    [AlignerUnavailable("worker died"), AlignerBusy("The aligner did not answer within 10 s")],
)
def test_lexicon_answers_503_when_the_aligner_cannot(client, error):
    with client(FakeAligner(error=error)) as c:
        response = c.post("/lexicon", json={"words": ["hello"]})
    assert error_of(response) == tuple(main.ERRORS[type(error)])
    assert response.json()["detail"] == str(error)


def test_lexicon_needs_the_bearer_token_when_configured(client, monkeypatch):
    use_token(monkeypatch)
    with client(FakeAligner()) as c:
        denied = c.post("/lexicon", json={"words": ["hello"]})
        c.headers["Authorization"] = "Bearer s3cret"
        allowed = c.post("/lexicon", json={"words": ["hello"]})
    assert error_of(denied) == tuple(main.ERRORS[main.InvalidTokenError])
    assert allowed.status_code == 200


@needs_ffmpeg
def test_a_request_logs_one_line_with_its_stages(client, vowel_a, logs):
    with client(FakeAligner(vowel_alignment())) as c:
        response = post_analyze(c, vowel_a)

    [line] = request_lines(logs)
    assert line.levelno == logging.INFO
    assert line.request_id == response.headers["x-request-id"]
    method, path, status, *fields = line.getMessage().split()
    assert (method, path, status) == ("POST", "/analyze", "200")
    assert [field.split("=")[0] for field in fields[:2]] == ["total_ms", "decode_ms"]
    assert fields[2:] == ["audio_s=1.5", "words=1", "oovs=0"]


@needs_ffmpeg
@pytest.mark.parametrize(
    "error, level",
    [(AlignmentError("beam too narrow"), logging.INFO), (AlignmentTimeout("ran long"), logging.WARNING)],
    ids=["verdict", "retryable"],
)
def test_an_error_line_carries_its_code(client, vowel_a, logs, error, level):
    with client(FakeAligner(error=error)) as c:
        response = post_analyze(c, vowel_a)

    status, code, _ = error_of(response)
    [line] = request_lines(logs)
    assert line.levelno == level
    assert line.getMessage().startswith(f"POST /analyze {status} code={code} total_ms=")


@needs_ffmpeg
def test_an_unhandled_exception_logs_a_500(client, vowel_a, logs):
    with client(FakeAligner(error=RuntimeError("boom")), raise_server_exceptions=False) as c:
        response = post_analyze(c, vowel_a)

    assert response.status_code == 500
    [line] = request_lines(logs)
    assert line.levelno == logging.ERROR
    assert line.getMessage().startswith("POST /analyze 500 error=RuntimeError total_ms=")


def test_probes_are_left_out_of_the_request_log(client, logs):
    with client(FakeAligner()) as c:
        c.get("/health")
        c.get("/ready")
        c.get("/")
    assert [line.getMessage().split()[:3] for line in request_lines(logs)] == [["GET", "/", "200"]]


@pytest.mark.parametrize(
    "sent, kept",
    [("job-41.a_b", True), ("has space", False), ("x" * 65, False), (None, False)],
    ids=["safe", "unsafe", "too long", "absent"],
)
def test_a_caller_request_id_is_kept_when_it_is_safe(client, logs, sent, kept):
    headers = {} if sent is None else {"X-Request-ID": sent}
    with client(FakeAligner()) as c:
        response = c.post("/lexicon", json={"words": ["hello"]}, headers=headers)

    returned = response.headers["x-request-id"]
    assert (returned == sent) is kept
    if not kept:
        assert re.fullmatch(r"[0-9a-f]{16}", returned)
    [line] = request_lines(logs)
    assert line.request_id == returned


@needs_ffmpeg
def test_no_log_line_carries_request_content(client, vowel_a, logs):
    word = NAME.lower()
    aligner = FakeAligner(vowel_alignment(oovs=[word]), lexicon={NAME: (word, [word])})
    with client(aligner) as c:
        with open(vowel_a, "rb") as f:
            scored = c.post(
                "/analyze",
                files={"audio": (f"{NAME}.wav", f, "audio/wav")},
                data={"transcript": f"Hello, {NAME}."},
            )
        checked = c.post("/lexicon", json={"words": [NAME]})
        too_long = c.post("/lexicon", json={"words": [NAME * 40]})
    with client(FakeAligner(error=AlignmentError(f"could not align {word}"))) as c:
        refused = post_analyze(c, vowel_a, transcript=NAME)

    assert [r.status_code for r in (scored, checked, too_long, refused)] == [200, 200, 422, 422]
    assert NAME in str(too_long.json()["detail"]) and word in refused.json()["detail"]
    assert len(request_lines(logs)) == 4
    assert word not in logs.text.lower()


def test_a_failed_load_stops_both_pools(client):
    aligner, analyzer = FakeAligner(load_error=TimeoutError()), InlineAnalyzer()
    with pytest.raises(TimeoutError):
        with client(aligner, analyzer):
            pass
    assert aligner.stopped and analyzer.stopped
