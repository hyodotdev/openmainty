"""Portable repository profiles.

A repo profile describes *how to maintain* one repository and can be copied
to another PC via `repo show --export`. It must not contain PC paths,
credentials, or OpenMainty-core assumptions about any specific project.

Schema:
{
  "name": "openiap",
  "github": "hyodotdev/openiap",
  "baseBranch": "main",
  "knowledge": ["knowledge/internal/04-platform-packages.md"],
  "checks": [{"name": "parity", "cmd": ["bun", "audit:parity"], "requires": []}],
  "e2e": [{"name": "ios-device", "cmd": [...], "requires": ["ios_device"]}],
  "release": {"notes": "CHANGELOG.md"},
  "announce": {"medium": true, "discord": true, "slack": true}
}
"""
from __future__ import annotations

from pathlib import Path

from . import paths
from .util import read_json, write_json


def profile_path(name: str) -> Path:
    return paths.repos_dir() / f"{name}.json"


def load(name: str) -> dict:
    data = read_json(profile_path(name), default=None)
    if data is None:
        raise SystemExit(f"error: unknown repo '{name}' (see: openmainty repo list)")
    return data


def list_all() -> list:
    d = paths.repos_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def validate(profile: dict) -> list:
    errors = []
    for field in ("name", "github", "baseBranch"):
        if not profile.get(field):
            errors.append(f"missing required field: {field}")
    for group in ("checks", "e2e"):
        steps = profile.get(group, [])
        if not isinstance(steps, list):
            errors.append(f"'{group}' must be a list")
            continue
        for i, step in enumerate(steps):
            if not step.get("name"):
                errors.append(f"'{group}[{i}]' missing name")
            if not step.get("cmd"):
                errors.append(f"'{group}[{i}]' missing cmd")
    return errors


def save(profile: dict) -> Path:
    errors = validate(profile)
    if errors:
        raise SystemExit("error: invalid repo profile:\n- " + "\n- ".join(errors))
    path = profile_path(profile["name"])
    write_json(path, profile)
    return path
