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
    resolved = (Path(override).expanduser() if override
                else Path.home() / ".openmainty")
    # M2.15b test-isolation guard: with OPENMAINTY_TEST_GUARD=1
    # (set for the whole suite by tests/__init__.py, never in
    # production) resolving the real home is a loud error, never a
    # silent read or write into the owner's data.
    if os.environ.get("OPENMAINTY_TEST_GUARD") == "1" \
            and resolved == Path.home() / ".openmainty":
        raise RuntimeError("test touched the real OpenMainty home")
    return resolved


def repos_dir() -> Path:
    return home() / "repos"


def runs_dir() -> Path:
    return home() / "runs"


def pc_file() -> Path:
    return home() / "pc.json"


def sessions_dir() -> Path:
    return home() / "sessions"
