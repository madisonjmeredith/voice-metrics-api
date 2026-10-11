"""
Regenerate dictionary/cmudict.dict and dictionary/LICENSE.cmudict: the CMUdict
entries whose headword is purely alphabetic and absent from the english_us_arpa
release dictionary, with the license they travel under. Run it when
CMUDICT_COMMIT or the release dictionary changes, and only then:

    python spike/cmudict_gap.py
"""

import re
import urllib.request
from pathlib import Path

CMUDICT_COMMIT = "74790861f652b15e4ac49015a90074ad62a27690"
CMUDICT_URL = f"https://raw.githubusercontent.com/cmusphinx/cmudict/{CMUDICT_COMMIT}"
RELEASE_URL = (
    "https://github.com/MontrealCorpusTools/mfa-models/releases/download/"
    "dictionary-english_us_arpa-v3.0.0/english_us_arpa.dict"
)

DICTIONARY_DIR = Path(__file__).parents[1] / "dictionary"

ALPHABETIC_HEADWORD = re.compile(r"([a-z]+)(?:\(\d+\))?")


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf8")


def main() -> None:
    release_words = {line.split("\t")[0] for line in fetch(RELEASE_URL).splitlines()}

    entries = set()
    for line in fetch(f"{CMUDICT_URL}/cmudict.dict").splitlines():
        word, _, phones = line.split(" #")[0].partition(" ")
        headword = ALPHABETIC_HEADWORD.fullmatch(word)
        if headword and headword[1] not in release_words:
            entries.add(f"{headword[1]}\t{phones}")

    lines = sorted(entries)
    (DICTIONARY_DIR / "cmudict.dict").write_text("\n".join(lines) + "\n", encoding="utf8")
    (DICTIONARY_DIR / "LICENSE.cmudict").write_text(fetch(f"{CMUDICT_URL}/LICENSE"), encoding="utf8")

    headwords = {line.split("\t")[0] for line in lines}
    print(f"{len(headwords)} headwords, {len(lines)} lines")


if __name__ == "__main__":
    main()
