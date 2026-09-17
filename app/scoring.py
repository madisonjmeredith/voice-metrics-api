"""
Resonance scoring against per-phoneme population statistics.

A port of the reference project's resonance.py (AGPL-3.0, see NOTICE). The
numbers it produces are on that project's scale: each phone's F1-F3 are
z-scored against the pooled male-and-female population for that phoneme,
combined with the fitted weights, and mapped so that 0.5 is the population
mean. It is not a midpoint between male and female references.

Differences from the original, on purpose:

- The aligned phone label is the lookup key. The original looked up the
  cmudict pronunciation by position within the word, which drifts on
  pronunciation variants; the aligner already chose the pronunciation.
- `is not None` replaces truthiness checks, so a reading of exactly 0.0 is
  kept rather than dropped.
"""

import json
import statistics
from pathlib import Path
from typing import Optional

from app.measurement import MeasuredPhone

REFERENCE_DIR = Path(__file__).parent / "reference"

# Index into F / stats: F0, F1, F2, F3.
FORMANT_COUNT = 4

# A reading further than this many clip-level standard deviations from the
# clip mean is treated as a tracking error and dropped.
OUTLIER_STDEVS = 2.0

# The original divided the weighted z-score by 3 and centered on 0.5; a phone
# 1.5 pooled standard deviations "brighter" than average scores 1.0.
Z_SCALE = 3.0
CENTER = 0.5


def _load_json(name: str):
    with open(REFERENCE_DIR / name) as f:
        return json.load(f)


# stats[phone] is a list of four {mean, stdev, median, max, min} for F0-F3.
STATS: dict[str, list[dict]] = _load_json("stats.json")
# Weights for z(F1), z(F2), z(F3); fitted upstream, sum to 1.
WEIGHTS: list[float] = _load_json("weights.json")


def clamp(value: float) -> float:
    return max(0.0, min(value, 1.0))


def flag_outliers(phones: list[MeasuredPhone]) -> None:
    """Null out per-formant readings far from the clip mean and mark the phone."""
    for i in range(FORMANT_COUNT):
        values = [p.F[i] for p in phones if p.F[i] is not None]
        if len(values) < 2:
            continue
        mean = statistics.mean(values)
        stdev = statistics.stdev(values)
        if stdev == 0:
            continue
        for phone in phones:
            value = phone.F[i]
            if value is not None and abs(value - mean) / stdev > OUTLIER_STDEVS:
                phone.outlier = True
                phone.F[i] = None


def z_scores(phone: MeasuredPhone) -> Optional[list[Optional[float]]]:
    reference = STATS.get(phone.label)
    if reference is None:
        return None
    return [
        None if phone.F[i] is None else (phone.F[i] - reference[i]["mean"]) / reference[i]["stdev"]
        for i in range(FORMANT_COUNT)
    ]


def phone_resonance(z: list[Optional[float]]) -> Optional[float]:
    if any(z[i] is None for i in (1, 2, 3)):
        return None
    weighted = WEIGHTS[0] * z[1] + WEIGHTS[1] * z[2] + WEIGHTS[2] * z[3]
    return clamp(weighted / Z_SCALE + CENTER)


def describe(values: list[float]) -> dict:
    if not values:
        return {"mean": None, "median": None, "stdev": None, "min": None, "max": None}
    return {
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else None,
        "min": min(values),
        "max": max(values),
    }


def score(phones: list[MeasuredPhone]) -> dict:
    """
    Score measured phones in place and return the clip-level aggregates.

    Mutates each phone: outlier flag, z-scores, resonance.
    """
    flag_outliers(phones)

    for phone in phones:
        phone.z = z_scores(phone)
        phone.resonance = phone_resonance(phone.z) if phone.z is not None else None

    kept = [p for p in phones if not p.outlier]
    resonance = describe([p.resonance for p in kept if p.resonance is not None])

    return {
        "pitch": describe([p.F[0] for p in kept if p.F[0] is not None]),
        "resonance": {k: resonance[k] for k in ("mean", "median", "stdev")},
        "formants": {
            f"f{i}": describe([p.F[i] for p in kept if p.F[i] is not None]) for i in (1, 2, 3)
        },
        "phone_count": len(phones),
        "scored_phone_count": sum(1 for p in phones if p.resonance is not None),
    }
