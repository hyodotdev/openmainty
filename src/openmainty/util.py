"""Small shared helpers (stdlib only)."""
from __future__ import annotations

import datetime
import getpass
import json
import os
import socket
import subprocess
from pathlib import Path


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def pc_id() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown-host"
    return f"{user}@{host}"


def run_cmd(cmd, cwd=None, timeout=600):
    """Run a command; return (exit_code, stdout, stderr). Never raises on failure."""
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


def looks_like_secret_name(name: str) -> bool:
    lowered = name.upper()
    return any(k in lowered for k in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
