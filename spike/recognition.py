"""
Recognition spike for #25: how far each candidate recognizer separates genuine
reads from mismatched ones, and what it costs to run.

It runs in an image built on the shipped one, with the candidates added and
their models under /opt/asr (Systran/faster-whisper-{tiny,base,small}.en from
Hugging Face and vosk-model-small-en-us-0.15 from alphacephei.com):

    FROM voice-metrics-api
    ARG MAMBA_DOCKERFILE_ACTIVATE=1
    RUN pip install faster-whisper==1.2.1 vosk==0.3.45

The repo goes at /src and a data directory at /data:

    docker run --rm --platform linux/amd64 -e PYTHONPATH=/src \
      -v "$PWD:/src:ro" -v "$DATA:/data" voice-metrics-api:spike \
      python /src/spike/recognition.py separation /data/manifest.json /data/separation.json

    ... python /src/spike/recognition.py timing /data/long.wav /data/short.wav [--busy]

The manifest is a JSON list of {id, set, audio, transcript}, with audio
relative to the manifest. Items pair only with transcripts from their own set.
"""

import argparse
import json
import multiprocessing
import statistics
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

MODELS = Path("/opt/asr")
CANDIDATES = ["whisper-tiny.en", "whisper-base.en", "whisper-small.en", "vosk-small"]
THREADS = 2
FS = 16000
HELLO = Path(__file__).parents[1] / "tests" / "fixtures" / "hello.wav"


def read_samples(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


def load(name: str):
    if name.startswith("whisper-"):
        from faster_whisper import WhisperModel

        model = WhisperModel(
            str(MODELS / f"faster-whisper-{name.removeprefix('whisper-')}"),
            device="cpu", compute_type="int8", cpu_threads=THREADS, num_workers=1,
            local_files_only=True,
        )

        def recognize(samples: np.ndarray) -> str:
            segments, _ = model.transcribe(
                samples, language="en", beam_size=5, temperature=0.0,
                condition_on_previous_text=False, vad_filter=False, without_timestamps=True,
            )
            return "".join(s.text for s in segments).strip()

        return recognize

    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(-1)
    model = Model(str(MODELS / "vosk-model-small-en-us-0.15"))

    def recognize(samples: np.ndarray) -> str:
        recognizer = KaldiRecognizer(model, FS)
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        parts = []
        for i in range(0, len(pcm), 8000):
            if recognizer.AcceptWaveform(pcm[i : i + 8000]):
                parts.append(json.loads(recognizer.Result())["text"])
        parts.append(json.loads(recognizer.FinalResult())["text"])
        return " ".join(p for p in parts if p)

    return recognize


def write_half(wav: Path, out: Path) -> Path:
    with wave.open(str(wav), "rb") as source:
        frames = source.readframes(source.getnframes() // 2)
        params = source.getparams()
    with wave.open(str(out), "wb") as half:
        half.setparams(params)
        half.writeframes(frames)
    return out


def rss_mb() -> tuple[float, float]:
    fields = dict(
        line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line
    )
    return int(fields["VmRSS"].split()[0]) / 1024, int(fields["VmHWM"].split()[0]) / 1024


def run_candidate(name: str, clips: dict[str, str], out) -> None:
    """In a fresh process: load the candidate, recognize every clip, report texts, times, memory."""
    started = time.perf_counter()
    recognize = load(name)
    recognize(read_samples(HELLO))
    load_s = time.perf_counter() - started
    rss_loaded, _ = rss_mb()
    texts, seconds = {}, {}
    for key, path in clips.items():
        samples = read_samples(Path(path))
        started = time.perf_counter()
        texts[key] = recognize(samples)
        seconds[key] = time.perf_counter() - started
    out.put({"load_s": load_s, "rss_loaded_mb": rss_loaded, "rss_peak_mb": rss_mb()[1],
             "texts": texts, "seconds": seconds})


def in_subprocess(name: str, clips: dict[str, Path]) -> dict:
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    process = ctx.Process(target=run_candidate, args=(name, {k: str(v) for k, v in clips.items()}, out))
    process.start()
    result = out.get()
    process.join()
    return result


def take_200(aligner, wav: Path, duration: float, transcript: str):
    """The alignment /analyze would score, or the code it would refuse with."""
    from app.aligner import AlignmentError, EmptyTranscriptError
    from app.analyzer_pool import _measure
    from app.main import MIN_SCORED_PHONES
    from app.scoring import score

    try:
        alignment = aligner.align(wav, duration, transcript)
    except AlignmentError:
        return None, "alignment_failed"
    except EmptyTranscriptError:
        return None, "transcript_empty"
    readings = score(_measure(wav, alignment))["readings"]
    if readings["balanced"] < MIN_SCORED_PHONES:
        return None, "too_little_speech"
    if readings["pitch"] < MIN_SCORED_PHONES:
        return None, "too_little_voicing"
    return alignment, "ok"


def separation(manifest_path: Path, out_path: Path, candidates: list[str]) -> None:
    from app.aligner import LoadedAligner
    from app.converter import convert_to_wav, get_audio_duration
    from app.transcript_match import normalize, transcript_words, word_coverage
    from app.worker import ACOUSTIC_DIR, DICTIONARY_PATH

    items = json.loads(manifest_path.read_text())
    tmp = Path(tempfile.mkdtemp())
    clips = {"hello": HELLO}
    for item in items:
        wav = convert_to_wav(manifest_path.parent / item["audio"], tmp / f"{item['id']}.wav", 61, 30)
        half_wav = write_half(wav, tmp / f"{item['id']}-half.wav")
        item["wav"], item["half_wav"] = wav, half_wav
        clips[item["id"]], clips[item["id"] + "/half"] = wav, half_wav

    pairs = []
    for kind_set in ("ref", "client"):
        members = [i for i in items if i["set"] == kind_set]
        for n, item in enumerate(members):
            pairs.append(("genuine", item["id"], item["wav"], item["id"], item["transcript"]))
            other = next(o for o in members[n + 1 :] + members[:n] if o["transcript"] != item["transcript"])
            pairs.append(("other_words", item["id"], item["wav"], item["id"], other["transcript"]))
            pairs.append(("half_read", item["id"], item["half_wav"], item["id"] + "/half", item["transcript"]))
    pairs.append(("other_words", "hello", HELLO, "hello", "Can you hear me okay?"))

    aligner = LoadedAligner(ACOUSTIC_DIR, DICTIONARY_PATH)
    rows = []
    for kind, item_id, wav, clip, transcript in pairs:
        duration = get_audio_duration(wav)
        alignment, code = take_200(aligner, wav, duration, transcript)
        row = {"kind": kind, "id": item_id, "clip": clip, "code": code, "duration": round(duration, 2),
               "transcript_words": len(transcript.split())}
        if alignment is not None:
            row["speech_coverage"] = round(alignment.speech_coverage(duration), 3)
            row["oovs"] = len(alignment.oovs)
            row["words"] = transcript_words(alignment)
        rows.append(row)
        print(f"{kind:<12} {item_id:<8} {code}", file=sys.stderr, flush=True)

    results = {}
    for name in candidates:
        print(f"recognizing with {name}", file=sys.stderr, flush=True)
        result = in_subprocess(name, clips)
        for row in rows:
            if "words" in row:
                coverage = word_coverage(row["words"], normalize(result["texts"][row["clip"]]))
                row.setdefault("word_coverage", {})[name] = None if coverage is None else round(coverage, 3)
                row["compared_words"] = len(row["words"])
        results[name] = result

    out_path.write_text(json.dumps({"rows": rows, "candidates": results}, indent=1, default=str))
    summarize(rows, candidates, results)


def summarize(rows, candidates, results) -> None:
    scored = [r for r in rows if r["code"] == "ok"]
    print(f"\n{len(rows)} pairs, {len(scored)} would be a 200")
    for name in candidates:
        print(f"\n== {name}  load {results[name]['load_s']:.1f} s, "
              f"RSS {results[name]['rss_loaded_mb']:.0f} MB loaded, {results[name]['rss_peak_mb']:.0f} MB peak")
        for kind in ("genuine", "other_words", "half_read"):
            values = sorted(r["word_coverage"][name] for r in scored
                            if r["kind"] == kind and r["word_coverage"][name] is not None)
            if values:
                print(f"  {kind:<12} n={len(values):<3} min {values[0]:.3f}  "
                      f"p10 {values[len(values) // 10]:.3f}  median {statistics.median(values):.3f}  "
                      f"p90 {values[len(values) * 9 // 10]:.3f}  max {values[-1]:.3f}")
        seconds = results[name]["seconds"]
        per_audio_s = [seconds[k] for k in seconds if "/" not in k]
        print(f"  recognition s: median {statistics.median(per_audio_s):.2f}, max {max(per_audio_s):.2f}")


def _busy(stop, wav: str, which: str) -> None:
    from app.converter import get_audio_duration

    if which == "aligner":
        from app.aligner import LoadedAligner
        from app.worker import ACOUSTIC_DIR, DICTIONARY_PATH

        aligner = LoadedAligner(ACOUSTIC_DIR, DICTIONARY_PATH)
        duration = get_audio_duration(Path(wav))
        while not stop.is_set():
            aligner.align(Path(wav), duration, "the " * 120)
    else:
        from app.analyzer import AudioAnalyzer

        while not stop.is_set():
            AudioAnalyzer(Path(wav))


def timing(long_wav: Path, short_wav: Path, candidates: list[str], busy: bool, repeats: int) -> None:
    ctx = multiprocessing.get_context("spawn")
    stop = ctx.Event()
    workers = []
    if busy:
        workers = [ctx.Process(target=_busy, args=(stop, str(long_wav), which))
                   for which in ("aligner", "praat", "praat")]
        for w in workers:
            w.start()
        time.sleep(15)
    clips = {f"{label}/{n}": path for label, path in (("long", long_wav), ("short", short_wav))
             for n in range(repeats)}
    for name in candidates:
        result = in_subprocess(name, clips)
        for label in ("short", "long"):
            times = [result["seconds"][k] for k in result["seconds"] if k.startswith(label)]
            print(f"{name:<18} {label:<5} min {min(times):.2f} s  max {max(times):.2f} s  "
                  f"(busy={busy}, threads={THREADS})")
        print(f"{name:<18} load {result['load_s']:.1f} s  RSS peak {result['rss_peak_mb']:.0f} MB")
        print(f"{name:<18} long text: {result['texts']['long/0'][:120]!r}")
    stop.set()
    for w in workers:
        w.join(timeout=30)
        w.kill()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    sep = sub.add_parser("separation")
    sep.add_argument("manifest", type=Path)
    sep.add_argument("out", type=Path)
    sep.add_argument("--candidates", nargs="+", default=CANDIDATES)
    tim = sub.add_parser("timing")
    tim.add_argument("long", type=Path)
    tim.add_argument("short", type=Path)
    tim.add_argument("--candidates", nargs="+", default=CANDIDATES)
    tim.add_argument("--busy", action="store_true")
    tim.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.mode == "separation":
        separation(args.manifest, args.out, args.candidates)
    else:
        timing(args.long, args.short, args.candidates, args.busy, args.repeats)


if __name__ == "__main__":
    main()
