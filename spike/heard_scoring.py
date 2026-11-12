"""
Scoring spike for #42: how far scoring only the words the recognizer heard
moves a take's figures, on genuine reads and on partial ones.

It runs in an image built from this branch, with the repo at /src and a data
directory at /data, and reads spike/recognition.py's manifest format:

    docker run --rm --platform linux/amd64 -e PYTHONPATH=/src \
      -v "$PWD:/src:ro" -v "$DATA:/data" voice-metrics-api:heard \
      python /src/spike/heard_scoring.py /data/manifest.json /data/heard.json

Each take is tried four ways. Each try is scored over every phone, over the
heard words' phones only, and, when a word went unheard, over every phone of a
second alignment of only the heard words:

- genuine: its own audio and transcript.
- stopped_early: its audio cut after the first half of its words, with the whole transcript.
- skipped: the middle third of its words spliced out of its audio, with the whole transcript.
- other_words: its audio with another take's transcript from its own set.

A partial try's truth is its cut audio aligned with only the words it holds.
Takes with OOV words or fewer than MIN_WORDS_TO_CUT words aren't cut, and takes
with OOV words aren't realigned, since the aligned labels lose their spelling.
"""

import argparse
import dataclasses
import json
import statistics
import sys
import tempfile
import wave
from collections import Counter
from pathlib import Path

import numpy as np

FIGURES = ("pitch_median", "resonance_median", "balanced")
METHODS = ("heard", "realigned")
MIN_WORDS_TO_CUT = 6
FADE_S = 0.01


def read_pcm(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64), w.getframerate()


def write_spliced(samples: np.ndarray, rate: int, spans: list[tuple[float, float]], out: Path) -> Path:
    """Keep these (start, end) spans in order, fading each join so a splice doesn't click."""
    fade = int(FADE_S * rate)
    pieces = []
    for start, end in spans:
        piece = samples[int(start * rate) : int(end * rate)].copy()
        ramp = np.linspace(0.0, 1.0, min(fade, len(piece) // 2))
        if start > 0:
            piece[: len(ramp)] *= ramp
        if int(end * rate) < len(samples):
            piece[len(piece) - len(ramp) :] *= ramp[::-1]
        pieces.append(piece)
    data = np.clip(np.concatenate(pieces), -32768, 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return out


def boundary(words, i: int) -> float:
    """Halfway through the gap between speech words i - 1 and i."""
    return (words[i - 1].end + words[i].start) / 2


def outcome(aggregates: dict) -> dict:
    from app.main import MIN_SCORED_PHONES

    if aggregates["readings"]["balanced"] < MIN_SCORED_PHONES:
        return {"code": "too_little_speech"}
    if aggregates["readings"]["pitch"] < MIN_SCORED_PHONES:
        return {"code": "too_little_voicing"}
    return {
        "code": None,
        "pitch_median": aggregates["pitch"]["median"],
        "resonance_median": aggregates["resonance"]["median"],
        "balanced": aggregates["resonance"]["balanced"],
    }


def attempt(aligner, recognizer, wav: Path, transcript: str, text=None) -> tuple[dict, object, str]:
    """Align, recognize, and score one try every way; returns the row, the alignment, and the text."""
    from app.aligner import AlignmentError, EmptyTranscriptError
    from app.analyzer_pool import _measure
    from app.converter import get_audio_duration
    from app.scoring import score
    from app.transcript_match import compare

    duration = get_audio_duration(wav)
    try:
        alignment = aligner.align(wav, duration, transcript)
    except (AlignmentError, EmptyTranscriptError) as exc:
        return {"duration": round(duration, 2), "align_error": type(exc).__name__}, None, text
    if text is None:
        text = recognizer.recognize(wav)
    comparison = compare(alignment, text)
    phones = _measure(wav, alignment)
    every = score([dataclasses.replace(p, F=list(p.F)) for p in phones])
    kept = [p for p in phones if comparison.heard.get(p.span.word_index, True)]
    row = {
        "duration": round(duration, 2),
        "word_coverage": comparison.word_coverage,
        "compared_words": comparison.compared_words,
        "unheard_words": sum(not heard for heard in comparison.heard.values()),
        "phones": len(phones),
        "kept_phones": len(kept),
        "all": outcome(every),
        "heard": outcome(score(kept)),
        "realigned": (
            outcome(every)
            if all(comparison.heard.values())
            else realigned(aligner, wav, alignment, comparison.heard)
        ),
    }
    return row, alignment, text


def realigned(aligner, wav: Path, alignment, heard: dict[int, bool]) -> dict:
    """Every phone of a second alignment that holds only the words that were heard."""
    if alignment.oovs:
        return {"code": "skipped_oov"}
    return aligned_to(aligner, wav, [w.label for i, w in enumerate(alignment.words) if w.is_speech and heard.get(i, True)])


def aligned_to(aligner, wav: Path, words: list[str]) -> dict:
    """Every phone of the clip aligned with just these words."""
    from app.aligner import AlignmentError, EmptyTranscriptError
    from app.analyzer_pool import _measure
    from app.converter import get_audio_duration
    from app.scoring import score

    try:
        alignment = aligner.align(wav, get_audio_duration(wav), " ".join(words))
    except (AlignmentError, EmptyTranscriptError) as exc:
        return {"code": type(exc).__name__}
    return outcome(score(_measure(wav, alignment)))


def run(manifest_path: Path, out_path: Path) -> None:
    from app.aligner import LoadedAligner
    from app.converter import convert_to_wav, get_audio_duration
    from app.main import MIN_DURATION
    from app.recognizer import LoadedRecognizer
    from app.recognizer_pool import RECOGNIZER_THREADS, WHISPER_MODEL_DIR
    from app.worker import ACOUSTIC_DIR, DICTIONARY_PATH

    items = json.loads(manifest_path.read_text())
    tmp = Path(tempfile.mkdtemp())
    aligner = LoadedAligner(ACOUSTIC_DIR, DICTIONARY_PATH)
    recognizer = LoadedRecognizer(WHISPER_MODEL_DIR, RECOGNIZER_THREADS)
    rows, texts = [], {}

    def add(item, case, row):
        rows.append({"id": item["id"], "set": item["set"], "case": case, **row})
        print(f"{case:<14} {item['id']:<9} all={row.get('all', {}).get('code')} "
              f"heard={row.get('heard', {}).get('code')} realigned={row.get('realigned', {}).get('code')} "
              f"unheard={row.get('unheard_words')}", file=sys.stderr, flush=True)

    for item in items:
        wav = convert_to_wav(manifest_path.parent / item["audio"], tmp / f"{item['id']}.wav", 61, 30)
        item["wav"] = wav
        row, alignment, texts[item["id"]] = attempt(aligner, recognizer, wav, item["transcript"])
        add(item, "genuine", row)
        if alignment is None or alignment.oovs or len(alignment.speech_words) < MIN_WORDS_TO_CUT:
            continue

        words = alignment.speech_words
        samples, rate = read_pcm(wav)
        end = len(samples) / rate
        half, third, two_thirds = len(words) // 2, len(words) // 3, 2 * len(words) // 3
        cuts = [
            ("stopped_early", [(0.0, boundary(words, half))], words[:half]),
            ("skipped", [(0.0, boundary(words, third)), (boundary(words, two_thirds), end)],
             words[:third] + words[two_thirds:]),
        ]
        for case, spans, held in cuts:
            path = write_spliced(samples, rate, spans, tmp / f"{item['id']}-{case}.wav")
            if get_audio_duration(path) < MIN_DURATION:
                continue
            row, _, _ = attempt(aligner, recognizer, path, item["transcript"])
            add(item, case, {**row, "truth": aligned_to(aligner, path, [w.label for w in held])})

    for kind_set in ("ref", "client"):
        members = [i for i in items if i["set"] == kind_set]
        for n, item in enumerate(members):
            other = next(o for o in members[n + 1 :] + members[:n] if o["transcript"].split() != item["transcript"].split())
            row, _, _ = attempt(aligner, recognizer, item["wav"], other["transcript"], texts[item["id"]])
            add(item, "other_words", row)

    out_path.write_text(json.dumps(rows, indent=1))
    summarize(rows)


def spread(values: list[float]) -> str:
    if not values:
        return "n=0"
    return f"n={len(values):<3} median {statistics.median(values):.3f}  max {max(values):.3f}"


def summarize(rows: list[dict]) -> None:
    def scored(row, *keys):
        return all(row.get(k, {}).get("code", "missing") is None for k in keys)

    def codes(rs, method):
        return dict(Counter(r[method]["code"] for r in rs if not scored(r, method)))

    genuine = [r for r in rows if r["case"] == "genuine" and scored(r, "all")]
    unheard = [r for r in genuine if r["unheard_words"]]
    print(f"\n== genuine: {len(genuine)} scored on every phone, {len(unheard)} with an unheard word")
    for method in METHODS:
        print(f"  {method}: not scored {codes(genuine, method)}")
        for fig in FIGURES:
            moved = [abs(r[method][fig] - r["all"][fig]) for r in unheard if scored(r, method)]
            print(f"    |{method} - all| {fig:<17} {spread(moved)}")

    for case in ("stopped_early", "skipped"):
        tries = [r for r in rows if r["case"] == case]
        aligned = [r for r in tries if scored(r, "all")]
        print(f"\n== {case}: {len(tries)} tries, {len(aligned)} scored on every phone")
        for method in METHODS:
            both = [r for r in aligned if scored(r, method, "truth")]
            print(f"  {method}: not scored {codes(aligned, method)}")
            for fig in FIGURES:
                every = [abs(r["all"][fig] - r["truth"][fig]) for r in both]
                other = [abs(r[method][fig] - r["truth"][fig]) for r in both]
                closer = sum(o < e for o, e in zip(other, every))
                print(f"    {fig:<17} every phone {spread(every)}")
                print(f"    {'':<17} {method:<11} {spread(other)}  closer in {closer} of {len(both)}")

    others = [r for r in rows if r["case"] == "other_words" and scored(r, "all")]
    print(f"\n== other_words: {len(others)} scored on every phone")
    for method in METHODS:
        print(f"  {method}: not scored {codes(others, method)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    run(args.manifest, args.out)


if __name__ == "__main__":
    main()
