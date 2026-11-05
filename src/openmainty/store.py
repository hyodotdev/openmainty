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


def record_answer(run_id: str, answer: str) -> dict:
    """Record an operator answer to a run's question; clears the wait."""
    from .util import utcnow

    answer = (answer or "").strip()
    if not answer:
        raise SystemExit("error: answer is required")
    state = load(run_id)
    wait = state.get("waitReason") or ""
    if not wait.startswith("question:"):
        raise SystemExit(f"error: run {run_id} is not waiting for an answer")
    question = wait[len("question:"):].strip()
    state.setdefault("answers", []).append({"q": question, "a": answer[:2000]})
    state.setdefault("interventions", []).append(
        {"at": utcnow(), "kind": "answer", "detail": answer[:300]})
    state["waitReason"] = None
    qstep = state.pop("question_step", None)
    if qstep in (state.get("steps") or {}):
        entry = state["steps"][qstep]
        if entry.get("status") == "blocked":
            entry["status"] = "pending"
            entry["note"] = "answered, awaiting resume"
    # A valid answer resolves a WAITING triage verdict with the SAME run
    # identity: the durable disposition clears so resume proceeds. A
    # DUPLICATE verdict is terminal and never answerable (its wait text
    # cannot reach this path).
    disp = state.get("disposition")
    if isinstance(disp, dict) and disp.get("kind") in ("INFO-NEEDED", "UNKNOWN"):
        state.pop("disposition", None)
        state.setdefault("interventions", []).append(
            {"at": utcnow(), "kind": "answer-clears-info-needed",
             "detail": f"operator answered {disp.get('kind')}; same run resumable"})
    save(state)
    return state


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
        # Content identity (runner.source_snapshot): HEAD alone cannot tell
        # a dirty tree from a clean one, so each verification also records
        # the content fingerprint it actually tested.
        "workTreeFingerprint": None,
        "verifiedFingerprints": {},
        "sourceKind": "unknown",
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


# Sentinel: use the stored workTreeFingerprint cache (execute/resume paths
# and direct callers that already refreshed it). Read paths (API/graph)
# must pass an explicitly read fingerprint — or None when unreadable —
# so a stale cache can never fabricate green.
USE_STORED = object()


def invalidate_stale_verifications(state: dict, current_fingerprint=None) -> list:
    """Reset verify-steps no longer bound to the current source content.

    HEAD-only comparison is insufficient: a dirty-tree edit keeps the same
    HEAD, so a step is fresh only when BOTH its verified commit and its
    verified content fingerprint match. Steps verified before fingerprints
    existed (legacy runs) carry no content proof and are treated as
    historical, never current-verified.
    """
    reset = []
    current = (current_fingerprint if current_fingerprint is not None
               else state.get("workTreeFingerprint"))
    for step in VERIFY_STEPS:
        # An honest not-applicable skip carries no content proof and
        # needs no re-verification when the source changes.
        if step == "reproduce" and _is_not_applicable_skip(state, step):
            continue
        entry = state["steps"][step]
        if entry["status"] != "done":
            continue
        verified_fp = (state.get("verifiedFingerprints") or {}).get(step)
        if (state.get("verifiedCommits", {}).get(step) != state.get("workCommit")
                or verified_fp is None or current is None
                or verified_fp != current):
            entry.update(status="pending", started_at=None, ended_at=None,
                         note="invalidated: source changed after verification")
            reset.append(step)
    if reset:
        save(state)
    return reset


def _is_not_applicable_skip(state: dict, step: str) -> bool:
    """True when a done step is an honest not-applicable skip, not a test.

    Dependency-maintenance runs have no upstream bug to reproduce: the
    reproduce step is marked done with an explicit note and no shell
    commands or content proof. That skip must never fabricate tested
    proof, and it must never drag the aggregate proof to legacy.
    """
    if step != "reproduce":
        return False
    entry = (state.get("steps") or {}).get(step, {})
    if entry.get("status") != "done":
        return False
    note = str(entry.get("note") or "")
    if "no upstream bug to reproduce" not in note:
        return False
    shell_cmds = [c for c in (entry.get("command") or [])
                  if isinstance(c, list) and c]
    if shell_cmds:
        return False
    return True


def verification_status(state: dict,
                        current_fingerprint=USE_STORED,
                        current_head=USE_STORED) -> dict:
    """Per verify-step proof state: fresh | stale | legacy | open | unknown | skipped.

    - fresh: done AND bound (commit + fingerprint) to the current content.
    - stale: done but bound to different content. A moved HEAD is stale
      even when the bytes are identical: the strict contract requires
      BOTH the verified commit to equal the live HEAD and the verified
      fingerprint to equal the live bytes.
    - legacy: done but recorded no content proof (pre-fingerprint runs) —
      honest history, never current verification.
    - open: not done.
    - unknown: done with content proof, but the current bytes or HEAD
      could not be read (worktree missing/unreadable) — unverified,
      never green.
    - skipped: done as an explicit not-applicable skip (dep-maintenance
      reproduce with no upstream bug) — no test claimed, excluded from
      the required-proof aggregate.

    The defaults read the stored workCommit/workTreeFingerprint cache
    (execute/resume paths that already refreshed it). Read paths must
    pass a freshly read head AND fingerprint (or None when unreadable)
    so proof is validated at read time, not at the last resume: a HEAD
    that moved after the last engine write can never keep false current
    proof from the old recorded commit.
    """
    out = {}
    current = (state.get("workTreeFingerprint")
               if current_fingerprint is USE_STORED
               else current_fingerprint)
    if current_head is USE_STORED:
        # Stored-cache contract for execute/resume paths (and non-git
        # fixtures with no commit identity): compare against the recorded
        # commit exactly as before.
        head_now = state.get("workCommit")
        head_readable = True
    else:
        # Read paths pass the freshly read live HEAD (None when the live
        # source is missing/unreadable): no readable HEAD means unknown,
        # never stored-values-passed-off-as-current.
        head_now = current_head
        head_readable = head_now not in (None, "", "unknown")
    for step in VERIFY_STEPS:
        entry = (state.get("steps") or {}).get(step, {})
        if entry.get("status") != "done":
            out[step] = "open"
            continue
        if _is_not_applicable_skip(state, step):
            out[step] = "skipped"
            continue
        verified_fp = (state.get("verifiedFingerprints") or {}).get(step)
        if verified_fp is None:
            out[step] = "legacy"
        elif current is None or not head_readable:
            out[step] = "unknown"
        elif ((state.get("verifiedCommits", {}).get(step) == head_now)
              and verified_fp == current):
            out[step] = "fresh"
        else:
            out[step] = "stale"
    return out


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


def has_active(repo: str) -> bool:
    """True when any local run for repo is active or waiting."""
    d = paths.runs_dir()
    if not d.is_dir():
        return False
    for found in d.glob("*/run.json"):
        try:
            state = json.loads(found.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(state.get("repo")) != str(repo):
            continue
        if active_for(repo, str(state.get("issue", ""))):
            return True
    return False


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


def list_runs_with_total(limit: int = 20) -> tuple:
    """Newest-first runs plus the total readable count (L10h).

    Single scan: the graph build reports truncation from the same
    pass that feeds its nodes, so the metadata can never disagree
    with the content. Unreadable files count on neither side, exactly
    as list_runs has always skipped them.
    """
    d = paths.runs_dir()
    if not d.is_dir():
        return [], 0
    states = []
    for f in d.glob("*/run.json"):
        try:
            states.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    states.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return states[:limit], len(states)


def list_runs(limit: int = 20) -> list:
    return list_runs_with_total(limit)[0]
