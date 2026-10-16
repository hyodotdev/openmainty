"""Home-directory resolution.

Portable repo settings live under <home>/repos/*.json and can be exported.
PC-specific settings live in <home>/pc.json (checkout paths, capabilities).
Run history lives under <home>/runs/ (never inside the target checkout).
"""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    override = os.environ.get("OPENMAINTY_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".openmainty"


def repos_dir() -> Path:
    return home() / "repos"


def runs_dir() -> Path:
    return home() / "runs"


def pc_file() -> Path:
    return home() / "pc.json"
