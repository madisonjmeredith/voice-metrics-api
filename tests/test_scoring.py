import pytest

from app.aligner import PhoneSpan
from app.measurement import MeasuredPhone
from app.scoring import STATS, WEIGHTS, phone_resonance, score
from tests.parity_data import PARITY_PHONES

# Produced by spike/parity_expected.py from the reference implementation.
UPSTREAM = {
    "meanPitch": 180.63,
    "medianPitch": 179.65,
    "stdevPitch": 8.179792173399026,
    "meanResonance": 0.45648990247208454,
    "medianResonance": 0.4445483970851509,
    "stdevResonance": 0.15940729049319508,
    "per_phone": [
        0.5495665042125542, 0.6508621150651217, 0.41802411842044657,
        0.3817463937830889, 0.4445483970851509, None, 0.7941007028200111,
        0.44590593260430833, 0.3235594880387537, None, 0.3023925739336284,
        0.46598178142520097, None, 0.4852218599845192, 0.24470091980466524, None,
    ],
    "outliers": [
        False, False, False, False, False, False, False, False,
        False, True, False, False, True, True, False, True,
    ],
}


def _phones(readings=PARITY_PHONES) -> list[MeasuredPhone]:
    return [
        MeasuredPhone(PhoneSpan(label, i * 0.1, i * 0.1 + 0.08, 0), list(F))
        for i, (label, F) in enumerate(readings)
    ]


def test_weights_are_the_fitted_upstream_values():
    assert WEIGHTS == pytest.approx([0.7321428571428571, 0.26785714285714285, 0.0])
    assert sum(WEIGHTS) == pytest.approx(1.0)


def test_parity_with_reference_implementation():
    phones = _phones()
    aggregates = score(phones)

    assert [p.outlier for p in phones] == UPSTREAM["outliers"]
    for phone, expected in zip(phones, UPSTREAM["per_phone"]):
        if expected is None:
            assert phone.resonance is None
        else:
            assert phone.resonance == pytest.approx(expected)

    assert aggregates["pitch"]["mean"] == pytest.approx(UPSTREAM["meanPitch"])
    assert aggregates["pitch"]["median"] == pytest.approx(UPSTREAM["medianPitch"])
    assert aggregates["pitch"]["stdev"] == pytest.approx(UPSTREAM["stdevPitch"])
    assert aggregates["resonance"]["mean"] == pytest.approx(UPSTREAM["meanResonance"])
    assert aggregates["resonance"]["median"] == pytest.approx(UPSTREAM["medianResonance"])
    assert aggregates["resonance"]["stdev"] == pytest.approx(UPSTREAM["stdevResonance"])


def test_counts_distinguish_measured_from_scored():
    aggregates = score(_phones())
    assert aggregates["phone_count"] == len(PARITY_PHONES)
    assert aggregates["scored_phone_count"] == sum(
        1 for r in UPSTREAM["per_phone"] if r is not None
    )


def test_phone_missing_from_stats_is_unscored():
    assert "JH" not in STATS
    phones = _phones([("JH", [180.0, 500.0, 1800.0, 2650.0]), ("AH0", [180.0, 560.0, 1500.0, 2550.0])])
    score(phones)
    assert phones[0].z is None
    assert phones[0].resonance is None
    assert phones[1].resonance is not None


def test_population_mean_scores_one_half():
    reference = STATS["AH0"]
    z = [0.0, 0.0, 0.0, 0.0]
    assert phone_resonance(z) == pytest.approx(0.5)
    phones = _phones([("AH0", [reference[i]["mean"] for i in range(4)])])
    score(phones)
    assert phones[0].resonance == pytest.approx(0.5)


def test_resonance_is_clamped():
    assert phone_resonance([0.0, 10.0, 10.0, 10.0]) == 1.0
    assert phone_resonance([0.0, -10.0, -10.0, -10.0]) == 0.0


def test_missing_formant_leaves_phone_unscored():
    assert phone_resonance([0.0, None, 1.0, 1.0]) is None
    # F3 has weight zero but the original still required it.
    assert phone_resonance([0.0, 1.0, 1.0, None]) is None


def test_empty_input():
    aggregates = score([])
    assert aggregates["pitch"]["mean"] is None
    assert aggregates["resonance"]["median"] is None
    assert aggregates["scored_phone_count"] == 0


def test_single_phone_has_no_stdev():
    phones = _phones([("AH0", [180.0, 560.0, 1500.0, 2550.0])])
    aggregates = score(phones)
    assert aggregates["pitch"]["stdev"] is None
    assert aggregates["resonance"]["stdev"] is None
    assert aggregates["resonance"]["mean"] == pytest.approx(phones[0].resonance)
