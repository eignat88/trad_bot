"""Shared test fixtures."""
from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest


_TMP_ROOT = Path(__file__).parent / ".tmp"


@pytest.fixture
def tmp_path() -> Path:
    """Provide an isolated temporary directory without pytest's 0700 factory.

    On Windows, pytest's built-in ``tmp_path`` creates directories with mode
    ``0700``.  That ACL is unusable in the constrained runner used by this
    project, while ordinary workspace directories are writable.  Use a
    per-test directory under the ignored test scratch root instead.
    """
    _TMP_ROOT.mkdir(exist_ok=True)
    path = _TMP_ROOT / uuid4().hex
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
