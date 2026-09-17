"""
Timing spike: load the aligner once, align clips repeatedly, compare to the CLI.

Run inside the image:

    python spike/align_spike.py short.wav "transcript" long.wav "transcript"
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app.aligner import LoadedAligner
from app.converter import get_audio_duration
from app.worker import ACOUSTIC_DIR, DICTIONARY_PATH


def stopwatch(label, started):
    elapsed = time.perf_counter() - started
    print(f"{label:<40} {elapsed:7.2f} s", flush=True)
    return time.perf_counter()


def main(pairs):
    t = time.perf_counter()
    aligner = LoadedAligner(ACOUSTIC_DIR, DICTIONARY_PATH)
    t = stopwatch("LoadedAligner (model + lexicon)", t)

    for round_name in ("cold", "warm"):
        for wav, text in pairs:
            wav = Path(wav)
            duration = get_audio_duration(wav)
            t = time.perf_counter()
            alignment = aligner.align(wav, duration, text)
            stopwatch(f"[{round_name}] align {wav.name} ({duration:.1f}s)", t)
            if round_name == "warm":
                print("  oovs:", alignment.oovs, "| suspicious:", alignment.suspicious)
                for word in alignment.speech_words[:12]:
                    print(f"  {word.label:>12} {word.start:6.2f}-{word.end:6.2f}")

    # CLI comparison, cold then with --no_clean.
    wav, text = pairs[0]
    with tempfile.TemporaryDirectory() as tmp:
        txt = Path(tmp) / (Path(wav).stem + ".txt")
        txt.write_text(text)
        for flag in ([], ["--no_clean"]):
            t = time.perf_counter()
            result = subprocess.run(
                ["mfa", "align_one", wav, str(txt), str(DICTIONARY_PATH), str(ACOUSTIC_DIR),
                 str(Path(tmp) / "cli.TextGrid"), "-q", *flag],
                capture_output=True, text=True,
            )
            stopwatch(f"CLI align_one {' '.join(flag) or '(clean)'} rc={result.returncode}", t)
            if result.returncode != 0:
                print(result.stderr[-2000:])


if __name__ == "__main__":
    args = sys.argv[1:]
    main([(args[i], args[i + 1]) for i in range(0, len(args), 2)])
