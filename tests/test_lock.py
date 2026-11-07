"""
env.lock and requirements.lock against the inputs spike/lock.py solved them
from. The image installs only the locks, so an input edited without a re-solve
changes nothing in it.
"""

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(("lock", "source"), [("env.lock", "env.yaml"), ("requirements.lock", "requirements.txt")])
def test_lock_was_solved_from_the_current_input(lock, source):
    header = (ROOT / lock).read_text(encoding="utf8").splitlines()[0]
    assert hashlib.sha256((ROOT / source).read_bytes()).hexdigest() in header, "rerun spike/lock.py"


def test_every_conda_package_carries_an_md5():
    lines = (ROOT / "env.lock").read_text(encoding="utf8").splitlines()
    urls = [line for line in lines if line and not line.startswith(("#", "@"))]
    assert urls
    for url in urls:
        assert re.fullmatch(r"https://\S+#[0-9a-f]{32}", url), url
