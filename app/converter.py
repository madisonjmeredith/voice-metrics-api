"""
Audio conversion utilities.

Converts various audio formats (WebM, etc.) to WAV format
suitable for Praat / parselmouth analysis.
"""

import subprocess
import wave
from pathlib import Path


class AudioDecodeError(Exception):
    """
    The upload could not be decoded as audio.

    Its own type so the HTTP layer can answer 400 rather than 500: a file
    ffmpeg cannot read is the caller's problem, not the server's.
    """


def convert_to_wav(input_path: Path, output_path: Path) -> Path:
    """
    Convert audio file to 16 kHz mono 16-bit PCM WAV.

    Args:
        input_path: Path to input audio file (WebM, MP3, etc.)
        output_path: Path for output WAV file

    Returns:
        Path to the converted WAV file

    Raises:
        AudioDecodeError: If ffmpeg cannot decode the input
    """
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",  # Overwrite output file if exists
                "-i", str(input_path),
                "-acodec", "pcm_s16le",  # 16-bit PCM
                "-ac", "1",  # Mono
                "-ar", "16000",  # 16kHz sample rate
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.CalledProcessError as e:
        detail = e.stderr.strip().splitlines()
        raise AudioDecodeError(detail[-1] if detail else "ffmpeg could not decode the file") from e

    return output_path


def get_audio_duration(wav_path: Path) -> float:
    """
    Get the duration of a WAV file in seconds.

    Reads the header rather than shelling out to ffprobe. This only runs on
    output from convert_to_wav(), whose format is ours to guarantee, so the
    generality of ffprobe buys nothing and costs a subprocess per request.

    Args:
        wav_path: Path to a WAV file

    Returns:
        Duration in seconds

    Raises:
        AudioDecodeError: If the file is not a readable WAV
    """
    try:
        with wave.open(str(wav_path), "rb") as w:
            frame_rate = w.getframerate()
            if frame_rate == 0:
                raise AudioDecodeError("WAV file reports a zero sample rate")
            return w.getnframes() / frame_rate
    except (wave.Error, EOFError) as e:
        raise AudioDecodeError(str(e)) from e
