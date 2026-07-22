from __future__ import annotations

import tomllib
from pathlib import Path

import zerg


def test_package_version_matches_project_metadata():
    root = Path(__file__).resolve().parent.parent
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    assert zerg.__version__ == metadata["project"]["version"]
