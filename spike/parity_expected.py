"""
Regenerate the expected numbers for tests/test_scoring.py from the reference
implementation. Run from the reference project's root, which is where its
resonance.py expects to find stats.json:

    cd <reference project checkout>
    PYTHONPATH=../voice-metrics-api python ../voice-metrics-api/spike/parity_expected.py
"""

import json
import sys

sys.path.insert(0, "acousticgender/library")
import resonance  # noqa: E402

from tests.parity_data import PARITY_PHONES  # noqa: E402

data = {
    "phones": [
        {"phoneme": label, "expected": label, "F": list(F), "time": i * 0.1}
        for i, (label, F) in enumerate(PARITY_PHONES)
    ]
}
resonance.compute_resonance(data, json.load(open("weights.json")))

print(json.dumps({
    "meanPitch": data["meanPitch"],
    "medianPitch": data["medianPitch"],
    "stdevPitch": data["stdevPitch"],
    "meanResonance": data["meanResonance"],
    "medianResonance": data["medianResonance"],
    "stdevResonance": data["stdevResonance"],
    "per_phone": [p.get("resonance") for p in data["phones"]],
    "outliers": [p.get("outlier", False) for p in data["phones"]],
}, indent=2))
