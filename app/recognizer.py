"""
Speech recognition with Whisper tiny.en through faster-whisper, loaded once per process.

faster_whisper is imported lazily so the API process never loads CTranslate2;
only the recognizer's worker does.
"""

from pathlib import Path

from app.converter import read_samples


class LoadedRecognizer:
    """The CTranslate2 conversion of Whisper tiny.en, quantized to int8, resident in memory."""

    def __init__(self, model_dir: Path, threads: int) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(str(model_dir), device="cpu", compute_type="int8", cpu_threads=threads)

    def recognize(self, wav_path: Path) -> str:
        # Never pass the transcript as a prompt: Whisper would then hear it whether or not it was read.
        # Samples, not a path: faster-whisper 1.2.1 decodes a path with an av.open() argument PyAV 19 dropped.
        segments, _ = self._model.transcribe(
            read_samples(wav_path),
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        return "".join(segment.text for segment in segments).strip()
