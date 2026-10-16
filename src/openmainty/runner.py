"""Step runner: order, isolation, resume, evidence.

Boundary for a future shared coordinator: `Runner` only depends on the
`Executor` interface (worker + shell) and the `Store`-shaped helpers in
store.py. Swapping local files for a shared backend later means replacing
those two seams, not the step logic.
"""
from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

from . import artifacts, muse, planner, store
from .util import run_cmd, utcnow

QUOTA_HINTS = ("quota", "rate limit", "429", "usage limit", "subscription limit")


class QuotaHit(Exception):
    pass


class Executor:
    """Real executor: Muse subscription worker + local shell."""

    kind = "muse"

    def __init__(self, model=None):
        self.model = muse.resolve_model(model)

    def muse_step(self, step, prompt, workdir):
        code, out, usage = muse.exec_prompt(prompt, workdir=workdir, model=self.model)
        self._raise_if_quota(code, out)
        return code, out, usage

    def shell(self, cmd, cwd):
        return run_cmd(cmd, cwd=cwd)

    @staticmethod
    def _raise_if_quota(code, out):
        if code != 0 and any(h in out.lower() for h in QUOTA_HINTS):
            raise QuotaHit(out[-500:])


class EchoExecutor(Executor):
    """Cheap connectivity check: echo provider, no subscription spend."""

    kind = "echo"

    def muse_step(self, step, prompt, workdir):
        code, out, _ = run_cmd(["muse", "exec", "--provider", "echo", f"[{step}] {prompt[:120]}"])
        return code, out, {"echo": True, "tokens_measurable": False, "tokens": None}


class FakeExecutor(Executor):
    """Scripted results for tests. script: {step: [(exit, text), ...]}."""

    kind = "fake"

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []

    def muse_step(self, step, prompt, workdir):
        self.calls.append(step)
        if os.environ.get("OPENMAINTY_FAKE_QUOTA") == step:
            raise QuotaHit("fake quota exhausted")
        queue = self.script.get(step, [(0, f"fake {step} ok")])
        if len(queue) > 1:
            code, text = queue.pop(0)
            self.script[step] = queue
        else:
            code, text = queue[0]
        return code, text, {"fake": True, "tokens_measurable": False, "tokens": None}

    def shell(self, cmd, cwd):
        key = "shell:" + " ".join(cmd[:2])
        self.calls.append(key)
        queue = self.script.get(key, [(0, "fake shell ok")])
        if len(queue) > 1:
            code, text = queue.pop(0)
            self.script[key] = queue
        else:
            code, text = queue[0]
        return code, text, ""


def make_executor(name: str, model=None) -> Executor:
    if name == "muse":
        return Executor(model=model)
    if name == "echo":
        return EchoExecutor()
    if name == "fake":
        return FakeExecutor()
    raise SystemExit(f"error: unknown executor '{name}' (muse|echo|fake)")


# ---- isolated workspace ----

def ensure_workdir(state: dict, checkout: Path, base_branch: str) -> Path:
    work = store.run_dir(state["id"]) / "work"
    if work.exists():
        return work
    if (checkout / ".git").is_dir():
        base, _ = git_rev(checkout, base_branch)
        code, _, err = run_cmd(["git", "-C", str(checkout), "worktree", "add", "--detach", str(work), base])
        if code == 0:
            return work
        state.setdefault("interventions", []).append(
            {"at": utcnow(), "kind": "worktree-fallback", "detail": err.strip()[-300:]})
    shutil.copytree(checkout, work, ignore=shutil.ignore_patterns(".git"))
    return work


def git_rev(path: Path, ref: str):
    code, out, _ = run_cmd(["git", "-C", str(path), "rev-parse", ref])
    if code != 0:
        code2, out2, _ = run_cmd(["git", "-C", str(path), "rev-parse", "HEAD"])
        return (out2.strip() if code2 == 0 else "unknown"), code2
    return out.strip(), 0


def work_commit(work: Path, state: dict) -> str:
    code, out, _ = run_cmd(["git", "-C", str(work), "rev-parse", "HEAD"])
    if code == 0 and out.strip():
        return out.strip()
    return f"copyseq-{state.get('workSeq', 0)}"


def snapshot_diff(work: Path, run_dir: Path, name: str) -> str | None:
    code, out, _ = run_cmd(["git", "-C", str(work), "diff", "--stat"])
    if code != 0:
        return None
    dest = run_dir / f"{name}.diffstat.txt"
    dest.write_text(out, encoding="utf-8")
    return dest.name


# ---- steps ----

def _record_text(run_dir: Path, step: str, text: str, suffix="log.txt") -> str:
    dest = run_dir / f"{step}.{suffix}"
    dest.write_text(text[-20000:], encoding="utf-8")
    return dest.name


def run_worker_step(state, profile, step, executor, work, body, context=""):
    rdir = store.run_dir(state["id"])
    active_model = getattr(executor, "model", None) or muse.DEFAULT_MODEL
    store.mark(state, step, "running",
               command=["muse", "exec", "--provider", "meta", "--model", active_model,
                        "--contributor-mode", "on", "--workspace", str(work)])
    try:
        prompt = planner.prompt_for(step, profile, state["issue"], state["title"], body, context)
        code, out, usage = executor.muse_step(step, prompt, work)
    except QuotaHit as exc:
        store.mark(state, step, "pending", note="quota wait")
        state["waitReason"] = f"subscription-quota at step {step}"
        state.setdefault("interventions", []).append(
            {"at": utcnow(), "kind": "quota-wait", "detail": str(exc)[-300:]})
        store.save(state)
        raise
    ok = code == 0
    # A clean process exit is NOT success: the step output must show real work.
    success_markers = {"triage": ["DUPLICATE", "ACTIONABLE", "INFO-NEEDED"],
                       "analyze": ["package", "verif", "impact"],
                       "fix": ["CHANGED", "FIX", "TEST"]}
    if ok and step in success_markers and isinstance(executor, Executor) and executor.kind == "muse":
        lowered = out.lower()
        if not any(m.lower() in lowered for m in success_markers[step]):
            ok = False
            out = out + "\n[openmainty] step output lacked expected markers; marking failed."
    ev = _record_text(rdir, step, out)
    entry_extra = {"exit_code": code, "evidence": [ev],
                   "usage": usage, "session_id": (usage or {}).get("session_id"),
                   "note": "muse worker exit 0 with expected markers" if ok else f"worker failed (exit {code})"}
    if isinstance(executor, FakeExecutor):
        entry_extra["note"] = f"fake executor exit {code}"
    store.mark(state, step, "done" if ok else "failed", **entry_extra)
    if step in store.VERIFY_STEPS and ok:
        state["verifiedCommits"][step] = state["workCommit"]
        store.save(state)
    if step == "fix" and ok:
        state["workSeq"] = state.get("workSeq", 0) + 1
        state["workCommit"] = work_commit(work, state)
        store.save(state)
    return ok


def run_shell_checks(state, step, checks, executor, work, capabilities) -> bool:
    """Run profile-configured commands; missing capability/command -> BLOCKED."""
    rdir = store.run_dir(state["id"])
    store.mark(state, step, "running", command=[c.get("cmd", []) for c in checks])
    logs = []
    for check in checks:
        missing = [r for r in check.get("requires", []) if not capabilities.get(r)]
        if missing:
            store.mark(state, step, "blocked",
                       note=f"check '{check['name']}' needs missing capability: {', '.join(missing)}")
            return False
        code, out, err = executor.shell(check["cmd"], work)
        logs.append(f"$ {' '.join(check['cmd'])}\nexit={code}\n{out[-4000:]}\n{err[-2000:]}")
        if code != 0:
            ev = _record_text(rdir, step, "\n".join(logs))
            store.mark(state, step, "failed", exit_code=code, evidence=[ev],
                       note=f"check '{check['name']}' failed (exit {code})")
            return False
    ev = _record_text(rdir, step, "\n".join(logs) or "(no checks configured)")
    store.mark(state, step, "done", exit_code=0, evidence=[ev],
               note=f"{len(checks)} check(s) passed" if checks else "no checks configured for this repo")
    if step in store.VERIFY_STEPS:
        state["verifiedCommits"][step] = state["workCommit"]
        store.save(state)
    return True


def _has_valid_findings(review_out: str) -> bool:
    """True only when the reviewer names a concrete VALID finding.

    A bare 'VALID: none' / 'no VALID findings' must not trigger an apply
    session (substring matching alone over-triggers on those phrases).
    """
    import re
    if re.search(r"VALID\s*:\s*none\b", review_out, re.IGNORECASE):
        return False
    if re.search(r"no\s+VALID\s+findings?", review_out, re.IGNORECASE):
        return False
    return bool(re.search(r"(?m)^[\s\-\*\d\.]*VALID\s*[:\-]", review_out, re.IGNORECASE))


def run_review(state, profile, executor, work, body) -> bool:
    """Separate reviewer session + apply valid findings. Two sessions, both recorded."""
    rdir = store.run_dir(state["id"])
    store.mark(state, "review", "running",
               command=["muse", "exec", "(reviewer session)", "muse", "exec", "(apply session)"])
    try:
        code1, review_out, usage1 = executor.muse_step(
            "review", planner.prompt_for("review", profile, state["issue"], state["title"], body), work)
    except QuotaHit as exc:
        store.mark(state, "review", "pending", note="quota wait")
        state["waitReason"] = "subscription-quota at step review"
        store.save(state)
        raise
    _record_text(rdir, "review", review_out, "reviewer.log.txt")
    if code1 != 0:
        store.mark(state, "review", "failed", exit_code=code1,
                       evidence=["review.reviewer.log.txt"], usage=usage1,
                       note=f"reviewer session failed (exit {code1})")
        return False
    if not _has_valid_findings(review_out):
        store.mark(state, "review", "done", exit_code=0,
                   evidence=["review.reviewer.log.txt"], usage=usage1,
                   note="reviewer found no VALID findings")
        state["verifiedCommits"]["review"] = state["workCommit"]
        store.save(state)
        return True
    code2, apply_out, usage2 = executor.muse_step(
        "apply_review", planner.prompt_for("apply_review", profile, state["issue"],
                                           state["title"], body, context=review_out), work)
    _record_text(rdir, "review", apply_out, "apply.log.txt")
    ok = code2 == 0
    if ok:
        state["workSeq"] = state.get("workSeq", 0) + 1
        state["workCommit"] = work_commit(work, state)
    store.mark(state, "review", "done" if ok else "failed", exit_code=code2,
               evidence=["review.reviewer.log.txt", "review.apply.log.txt"],
               usage={"reviewer": usage1, "apply": usage2},
               session_id=(usage1 or {}).get("session_id"),
               note="VALID findings applied" if ok else f"apply failed (exit {code2})")
    if ok:
        state["verifiedCommits"]["review"] = state["workCommit"]
        store.save(state)
    return ok


def write_drafts(state, profile, context=""):
    from . import artifacts as A

    rdir = store.run_dir(state["id"])
    drafts = {
        "pr": A.pr_description(state, profile, context),
        "release_notes": A.release_notes(state, profile, context),
        "medium": A.medium_draft(state, context),
        "discord": A.discord_draft(state),
        "slack": A.slack_draft(state),
    }
    for key, text in drafts.items():
        dest = rdir / f"{key}.md"
        dest.write_text(text, encoding="utf-8")
        state["artifacts"][key] = dest.name
    store.save(state)


def execute(state, profile, executor, checkout, body="", only=None, write_drafts_on=True):
    """Run pending steps in order. Returns True when all selected steps are done."""
    work = ensure_workdir(state, checkout, profile.get("baseBranch", "main"))
    if not state.get("baseCommit") or state["baseCommit"] == "unknown":
        base, _ = git_rev(checkout, profile.get("baseBranch", "main"))
        state["baseCommit"] = base
        if state["workCommit"] in (None, "unknown"):
            state["workCommit"] = base
        store.save(state)
    state["deviceInfo"] = {
        "os": f"{platform.system()} {platform.release()}",
        "model": getattr(executor, "model", None) or muse.DEFAULT_MODEL,
        "contributor": True,
    }
    store.save(state)
    store.invalidate_stale_verifications(state)

    steps = [s for s in store.STEP_ORDER if (only is None or s in only)]
    for step in steps:
        entry = state["steps"][step]
        if entry["status"] == "done":
            continue
        if step == "triage":
            if not run_worker_step(state, profile, step, executor, work, body):
                return False
        elif step == "reproduce":
            repro = profile.get("reproduce")
            if repro and repro.get("cmd"):
                caps = {} if not hasattr(executor, "caps") else executor.caps
                if not run_shell_checks(state, step, [repro], executor, work, caps):
                    # shell failure may still leave worker notes; keep step failed
                    return False
            else:
                if not run_worker_step(state, profile, step, executor, work, body):
                    return False
        elif step == "analyze":
            ctx = _tail(store.run_dir(state["id"]) / "triage.log.txt")
            if not run_worker_step(state, profile, step, executor, work, body, context=ctx):
                return False
        elif step == "fix":
            ctx = _tail(store.run_dir(state["id"]) / "analyze.log.txt")
            if not run_worker_step(state, profile, step, executor, work, body, context=ctx):
                return False
            snapshot_diff(work, store.run_dir(state["id"]), "fix")
        elif step == "review":
            if not run_review(state, profile, executor, work, body):
                return False
            snapshot_diff(work, store.run_dir(state["id"]), "review")
        elif step == "e2e":
            from . import pcconfig
            caps = pcconfig.load().get("capabilities", {})
            checks = list(profile.get("checks", [])) + list(profile.get("e2e", []))
            if not checks:
                store.mark(state, step, "blocked", note="no checks/e2e configured for this repo")
                return False
            if not run_shell_checks(state, step, checks, executor, work, caps):
                return False
        elif step in ("release", "announce"):
            store.mark(state, step, "running", command=["openmainty", f"draft:{step}"])
            ctx = _tail(store.run_dir(state["id"]) / "fix.log.txt")
            if write_drafts_on:
                write_drafts(state, profile, context=ctx)
            store.mark(state, step, "done", exit_code=0,
                       evidence=[state["artifacts"].get("release_notes", ""),
                                 state["artifacts"].get("medium", ""),
                                 state["artifacts"].get("discord", ""),
                                 state["artifacts"].get("slack", "")],
                       note="drafts written (dry-run only; nothing posted)")
    return all(state["steps"][s]["status"] == "done" for s in steps)


def _tail(path: Path, n: int = 3000) -> str:
    try:
        return path.read_text(encoding="utf-8")[-n:]
    except OSError:
        return ""
