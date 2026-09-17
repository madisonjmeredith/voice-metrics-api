"""
Shared fixtures.

Synthetic vowels come from formantry's Klatt-style source-filter model, so
their formants are known exactly. Speech fixtures (hello.wav and the
TextGrids) were produced with macOS text-to-speech and MFA; see
tests/fixtures/README.md.
"""

import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from app.aligner import Alignment, PhoneSpan, WordSpan

FS = 16000
FIXTURES = Path(__file__).parent / "fixtures"

BANDWIDTHS = [60.0, 90.0, 120.0, 150.0, 200.0]

VOWEL_A = [750.0, 1200.0, 2574.0, 3500.0, 4500.0]
VOWEL_F0 = 140.0


def _resonator(x: np.ndarray, freq: float, bandwidth: float) -> np.ndarray:
    r = np.exp(-np.pi * bandwidth / FS)
    theta = 2 * np.pi * freq / FS
    a1 = 2 * r * np.cos(theta)
    a2 = -(r ** 2)
    gain = 1 - a1 - a2
    y = np.zeros_like(x)
    for n in range(len(x)):
        y[n] = gain * x[n]
        if n >= 1:
            y[n] += a1 * y[n - 1]
        if n >= 2:
            y[n] += a2 * y[n - 2]
    return y


def _glottal_source(n_samples: int, f0: float) -> np.ndarray:
    x = np.zeros(n_samples)
    x[:: int(FS / f0)] = 1.0
    for _ in range(2):
        y = np.zeros_like(x)
        for n in range(len(x)):
            y[n] = x[n] + 0.98 * (y[n - 1] if n >= 1 else 0.0)
        x = y
    return x


def vowel_signal(f0: float, formants: list[float], seconds: float) -> np.ndarray:
    signal = _glottal_source(int(FS * seconds), f0)
    for freq, bandwidth in zip(formants, BANDWIDTHS):
        signal = _resonator(signal, freq, bandwidth)
    signal = np.diff(signal, prepend=0.0)
    return signal / np.max(np.abs(signal)) * 0.8


def write_wav(path: Path, signal: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FS)
        w.writeframes((signal * 32767).astype(np.int16).tobytes())
    return path


def vowel_alignment(labels=("AA1", "AH0", "AA1", "AH0"), oovs=()) -> Alignment:
    """Phones spread evenly over the 1.5 s vowel_a clip, framed by silence."""
    words = [WordSpan("<eps>", 0.0, 0.1), WordSpan("ah", 0.1, 1.4), WordSpan("<eps>", 1.4, 1.5)]
    step = 1.3 / len(labels)
    phones = [PhoneSpan("sil", 0.0, 0.1)]
    for i, label in enumerate(labels):
        start = 0.1 + i * step
        phones.append(PhoneSpan(label, start, start + step, 1))
    phones.append(PhoneSpan("sil", 1.4, 1.5))
    return Alignment(words=words, phones=phones, oovs=list(oovs))


def post_analyze(client, path: Path, transcript="ah", **query):
    with open(path, "rb") as f:
        return client.post(
            "/analyze",
            params=query,
            files={"audio": (path.name, f, "audio/wav")},
            data={"transcript": transcript},
        )


@pytest.fixture(scope="session")
def audio_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("audio")


@pytest.fixture(scope="session")
def vowel_a(audio_dir: Path) -> Path:
    """1.5 s of a sustained /a/ at 140 Hz with exactly known formants."""
    return write_wav(audio_dir / "a.wav", vowel_signal(VOWEL_F0, VOWEL_A, 1.5))


@pytest.fixture(scope="session")
def vowel_a_short(audio_dir: Path) -> Path:
    """Half a second of the same vowel, under the minimum duration."""
    return write_wav(audio_dir / "a_short.wav", vowel_signal(VOWEL_F0, VOWEL_A, 0.5))


@pytest.fixture(scope="session")
def silence(audio_dir: Path) -> Path:
    return write_wav(audio_dir / "silence.wav", np.zeros(FS * 2))


@pytest.fixture
def garbage(tmp_path: Path) -> Path:
    path = tmp_path / "garbage.webm"
    path.write_bytes(b"this is not audio, not even slightly")
    return path


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _has_mfa() -> bool:
    try:
        import kalpy  # noqa: F401
        import montreal_forced_aligner  # noqa: F401
    except ImportError:
        return False
    return True


needs_mfa = pytest.mark.skipif(not _has_mfa(), reason="MFA is not installed")
