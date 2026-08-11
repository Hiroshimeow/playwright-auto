import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def playwright_tmpdir(tmp_path_factory):
    tmp_path_factory.getbasetemp()
    tmp_root = (Path(__file__).resolve().parents[1] / "test-results" / "playwright-tmp").resolve()
    tmp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_root.chmod(0o700)

    previous = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(tmp_root)
    try:
        yield tmp_root
    finally:
        if previous is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous
