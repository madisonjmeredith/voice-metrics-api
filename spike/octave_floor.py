"""
Octave-low pitch spike for #46: how often Praat's 75 Hz floor follows the
octave below a voice, and what a second pass above a speaker-relative floor
does with those frames.

    .venv/bin/python spike/octave_floor.py AUDIO...

Any format ffmpeg reads. It works on Praat's frames, with no alignment, so it
says nothing about phone midpoints; replay through the service for those.
Takes are labeled by position so file names stay out of the output.
"""

import subprocess
import sys

import numpy as np
import parselmouth

from app.analyzer import PITCH_FLOOR, SPEAKER_FLOOR_RATIO, speaker_floor, track_pitch

FS = 16000
MIN_RUN = 4
MIN_VOICED = 20


def load(path: str) -> parselmouth.Sound:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(FS), "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    return parselmouth.Sound(np.frombuffer(raw, dtype=np.float32).astype(np.float64), FS)


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    edges = np.flatnonzero(np.diff(np.concatenate([[0], mask.astype(int), [0]])))
    return [(a, b) for a, b in zip(edges[::2], edges[1::2]) if b - a >= MIN_RUN]


def main(paths: list[str]) -> None:
    takes = with_runs = quartile_misses = 0
    low_total = fixed = unvoiced = still_low = 0
    in_range = in_range_unvoiced = in_range_moved = 0
    landed: list[float] = []
    for n, path in enumerate(paths, 1):
        sound = load(path)
        first = track_pitch(sound, PITCH_FLOOR)
        times, f1 = first.xs(), first.selected_array["frequency"]
        voiced = f1[f1 > 0]
        if voiced.size < MIN_VOICED:
            continue
        takes += 1
        median = float(np.median(voiced))
        floor = speaker_floor(f1)
        low = (f1 > 0) & (f1 < floor)
        found = runs(low)
        if not found:
            continue
        with_runs += 1
        if max(PITCH_FLOOR, 0.75 * np.percentile(voiced, 25)) <= np.max(f1[low]):
            quartile_misses += 1

        second = track_pitch(sound, floor)
        f2 = np.nan_to_num([second.get_value_at_time(t) for t in times])
        low_total += int(low.sum())
        fixed += int((low & (f2 >= floor)).sum())
        unvoiced += int((low & (f2 <= 0)).sum())
        still_low += int((low & (f2 > 0) & (f2 < floor)).sum())
        landed += list(f2[low & (f2 >= floor)] / median)

        rest = (f1 > 0) & ~low
        in_range += int(rest.sum())
        in_range_unvoiced += int((rest & (f2 <= 0)).sum())
        in_range_moved += int((rest & (f2 > 0) & (np.abs(f2 - f1) > 0.03 * f1)).sum())

        spans = "; ".join(f"{times[a]:.2f}-{times[b - 1]:.2f} s at {np.median(f1[a:b]):.0f} Hz" for a, b in found)
        print(f"take{n:03d}  median {median:.0f} Hz  floor {floor:.0f}  low {low.sum() / voiced.size:.0%}  {spans}")

    print(f"\n{with_runs} of {takes} takes have a run of {MIN_RUN}+ frames under {SPEAKER_FLOOR_RATIO}x their median")
    print(f"0.75 x the 25th percentile leaves low frames in {quartile_misses} of them")
    print(f"{low_total} low frames: {fixed} re-tracked in range, {unvoiced} unvoiced, {still_low} still low")
    if landed:
        p = np.percentile(landed, [5, 25, 50, 75, 95])
        print("re-tracked frames over the median: p5 {:.2f}, p25 {:.2f}, p50 {:.2f}, p75 {:.2f}, p95 {:.2f}".format(*p))
    print(
        f"a full second pass on those takes: {in_range_unvoiced} of {in_range} in-range frames unvoiced, "
        f"{in_range_moved} moved more than 3%"
    )


if __name__ == "__main__":
    main(sys.argv[1:])
