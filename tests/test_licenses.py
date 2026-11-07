"""The license files beside the code, so a run in the image fails if a build leaves one out."""

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("path", ["LICENSE", "NOTICE", "dictionary/LICENSE.cmudict"])
def test_license_file_ships_with_the_code(path):
    assert (ROOT / path).is_file()
