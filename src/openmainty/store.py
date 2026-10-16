"""Run state store: one directory per run under <home>/runs/<run-id>/.

run.json records everything section 6 requires: repo/issue/run/pc/worker
ids, base + work commits, per-step status/timestamps/commands/exit codes,
evidence files, device/OS/SDK info, measurable usage only, and human
interventions with wait reasons.

Cross-PC dedup is NOT provided here: `index.json` only tracks runs created
by this home directory. A local lock prevents accidental double-runs on one
PC; it must never be described as global protection.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from . import paths
from .util import read_json, utcnow, write_json

STEP_ORDER = [
    "triage",
    "reproduce",
    "analyze",
    "fix",
    "review",
    "e2e",
    "release",
    "announce",
]

# Steps whose "done" is only valid for the exact commit they verified.
VERIFY_STEPS = {"reproduce", "review", "e2e"}

TERMINAL = {"done", "failed", "blocked"}


def new_run_id() -> str:
    return "run-" + secrets.token_hex(4)


def run_dir(run_id: str) -> Path:
    return paths.runs_dir() / run_id


def run_file(run_id: str) -> Path:
    return run_dir(run_id) / "run.json"


def load(run_id: str) -> dict:
    data = read_json(run_file(run_id), default=None)
    if data is None:
        raise SystemExit(f"error: unknown run '{run_id}'")
    return data


def save(state: dict) -> None:
    write_json(run_file(state["id"]), state)


def create(repo: str, issue: str, title: str, base_commit: str, worker: dict) -> dict:
    from .util import pc_id

    state = {
        "schema": 1,
        "id": new_run_id(),
        "repo": repo,
        "issue": str(issue),
        "title": title or "",
        "pc": pc_id(),
        "worker": worker,
        "created_at": utcnow(),
        "baseCommit": base_commit,
        "workCommit": base_commit,
        "verifiedCommits": {},
        "steps": {
            name: {
                "status": "pending",
                "started_at": None,
                "ended_at": None,
                "command": [],
                "exit_code": None,
                "evidence": [],
                "session_id": None,
                "usage": {},
                "note": "",
            }
            for name in STEP_ORDER
        },
        "artifacts": {},
        "deviceInfo": {},
        "interventions": [],
        "waitReason": None,
    }
    run_dir(state["id"]).mkdir(parents=True, exist_ok=True)
    save(state)
    return state


def mark(state: dict, step: str, status: str, **fields) -> None:
    entry = state["steps"][step]
    entry["status"] = status
    for key, val in fields.items():
        entry[key] = val
    now = utcnow()
    if status == "running" and not entry.get("started_at"):
        entry["started_at"] = now
    if status in TERMINAL:
        entry["ended_at"] = now
    save(state)


def invalidate_stale_verifications(state: dict) -> list:
    """Reset verify-steps whose verified commit no longer matches workCommit."""
    reset = []
    for step in VERIFY_STEPS:
        entry = state["steps"][step]
        if entry["status"] == "done" and state["verifiedCommits"].get(step) != state["workCommit"]:
            entry.update(status="pending", ended_at=None, note="invalidated: code changed after verification")
            reset.append(step)
    if reset:
        save(state)
    return reset


# ---- local dedup index (single-PC only) ----

def _index_file() -> Path:
    return paths.runs_dir() / "index.json"


def active_for(repo: str, issue: str):
    idx = read_json(_index_file(), default={})
    key = f"{repo}#{issue}"
    run_id = idx.get(key)
    if not run_id:
        return None
    state = read_json(run_file(run_id), default=None)
    if state is None:
        return None
    pending = [s for s, e in state["steps"].items() if e["status"] not in TERMINAL]
    if pending and state.get("waitReason") is None:
        return run_id
    if state.get("waitReason"):
        return run_id
    return None


def claim(repo: str, issue: str, run_id: str) -> None:
    idx = read_json(_index_file(), default={})
    idx[f"{repo}#{issue}"] = run_id
    write_json(_index_file(), idx)


def release(repo: str, issue: str, run_id: str) -> None:
    idx = read_json(_index_file(), default={})
    if idx.get(f"{repo}#{issue}") == run_id:
        del idx[f"{repo}#{issue}"]
        write_json(_index_file(), idx)


def latest_run(repo: str, issue: str):
    """Newest run for repo#issue, or None. Scans all local run dirs."""
    d = paths.runs_dir()
    if not d.is_dir():
        return None
    best = None
    for f in d.glob("*/run.json"):
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(state.get("repo")) == str(repo) and str(state.get("issue")) == str(issue):
            if best is None or state.get("created_at", "") > best.get("created_at", ""):
                best = state
    return best


def list_runs(limit: int = 20) -> list:
    d = paths.runs_dir()
    if not d.is_dir():
        return []
    states = []
    for f in d.glob("*/run.json"):
        try:
            states.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    states.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return states[:limit]
