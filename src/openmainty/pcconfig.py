"""PC-specific configuration (never portable, never committed to target repos).

pc.json schema:
{
  "checkouts": {"<repo-name>": "/abs/path/to/checkout"},
  "capabilities": {"ios_device": false, "xcode": false, ...},
  "workRoot": null | "/abs/path (default: <home>/work)",
  "notes": "..."
}
Auth material always stays on the PC: this file stores only *references*
(tool names), never credential values.
"""
from __future__ import annotations

from pathlib import Path

from . import paths
from .util import read_json, write_json

DEFAULT_CAPABILITIES = {
    "ios_device": False,
    "ios_simulator": False,
    "android_emulator": False,
    "xcode": False,
}


def load() -> dict:
    data = read_json(paths.pc_file(), default=None)
    if data is None:
        return {"checkouts": {}, "capabilities": dict(DEFAULT_CAPABILITIES)}
    data.setdefault("checkouts", {})
    caps = data.setdefault("capabilities", {})
    for key, val in DEFAULT_CAPABILITIES.items():
        caps.setdefault(key, val)
    return data


def save(data: dict) -> Path:
    path = paths.pc_file()
    write_json(path, data)
    return path


def checkout_for(repo_name: str):
    return load().get("checkouts", {}).get(repo_name)


def workspace_root() -> Path:
    """Where checkouts live by default. Visible dir; state stays in ~/.openmainty."""
    root = load().get("workspace_root") or str(Path.home() / "openmainty")
    return Path(root).expanduser()


def set_workspace_root(path: str) -> Path:
    """Validate + persist. Never moves existing checkouts (non-destructive)."""
    import os
    candidate = Path(path or "").expanduser()
    if not candidate.is_absolute():
        raise SystemExit(f"error: workspace root must be absolute: {path!r}")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"error: cannot create workspace root: {exc}") from exc
    if not os.access(candidate, os.W_OK):
        raise SystemExit(f"error: workspace root not writable: {candidate}")
    data = load()
    data["workspace_root"] = str(candidate)
    save(data)
    return candidate
