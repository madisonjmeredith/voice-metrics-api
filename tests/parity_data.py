"""
A fixed set of per-phone readings for the scoring parity test.

The expected values in test_scoring.py were produced by running the
reference project's resonance.py::compute_resonance
over exactly this data (see spike/parity_expected.py). If this changes,
regenerate them.
"""

# (phone label, [F0, F1, F2, F3]) in Hz. Mixed vowels and consonants, one
# unvoiced consonant with no F0, one phone absent from stats.json ("JH"),
# and one wild F2 reading that the outlier pass should remove.
PARITY_PHONES = [
    ("DH", [178.0, 420.0, 1650.0, 2600.0]),
    ("AH0", [182.5, 560.0, 1500.0, 2550.0]),
    ("K", [None, 610.0, 1720.0, 2700.0]),
    ("W", [171.0, 380.0, 900.0, 2450.0]),
    ("IH1", [190.2, 430.0, 2050.0, 2800.0]),
    ("JH", [176.0, 500.0, 1800.0, 2650.0]),
    ("AH1", [185.9, 700.0, 1400.0, 2500.0]),
    ("M", [180.0, 350.0, 1200.0, 2300.0]),
    ("OW1", [168.4, 480.0, 950.0, 2400.0]),
    ("ER0", [174.7, 470.0, 1450.0, 1900.0]),
    ("L", [179.3, 400.0, 1100.0, 2650.0]),
    ("EY1", [195.0, 450.0, 2200.0, 2750.0]),
    ("Z", [183.0, 330.0, 3400.0, 2900.0]),
    ("IY0", [201.6, 360.0, 2300.0, 2850.0]),
    ("S", [None, 520.0, 1600.0, 2600.0]),
    ("AA1", [172.2, 740.0, 1250.0, 2450.0]),
]
