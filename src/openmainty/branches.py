"""Branch strategy: main vs next, feature branches, promotion readiness.

Persisted per run and per repo state:
- main: backward-compatible, verified maintenance (patch-only for 0.x).
- next: breaking/major/pre-release/RN-compat migrations.
- Feature branches: codex/maint/<topic> off main, codex/next/<topic> off next.
- Never touch the user's dirty checkout: work happens in run worktrees.
- next is created locally only when a next-lane item actually needs it;
  main history is never rewritten; no force push.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import store as _store
from . import versions


def slugify_topic(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug or "update")[:48]


def route(name: str, current: str | None, latest: str | None,
          peer_support: str | None = None) -> dict:
    """Decide lane + branch for one dependency update."""
    kind = versions.update_kind(current or "", latest or "")
    if not current or not latest:
        kind = "unknown"
    peer_blocked = bool(peer_support and "conflict" in peer_support.lower())
    if kind == "none":
        return {"lane": "main", "kind": kind, "branch": None,
                "reason": f"{name} {current} is already at latest; no lane needed.",
                "peer_blocked": peer_blocked}
    if kind in ("unknown", "downgrade"):
        lane = "next" if kind == "downgrade" else "main"
        reason = ("declared/latest comparison unavailable; keep on main, "
                  "verify before any change" if kind == "unknown"
                  else "registry latest is older than declared: downgrade risk, "
                       "hold on next lane for review, do not apply")
        if kind == "downgrade":
            lane = "next"
        return {"lane": lane, "kind": kind, "branch": None, "reason": reason,
                "peer_blocked": peer_blocked}
    lane = versions.safe_lane(kind)
    if peer_blocked and lane == "main":
        lane = "next"
    prefix = "codex/maint" if lane == "main" else "codex/next"
    topic = slugify_topic(f"{name}-{latest}")
    reason = _reason(name, current, latest, kind, lane, peer_blocked)
    return {"lane": lane, "kind": kind, "branch": f"{prefix}/{topic}",
            "reason": reason, "peer_blocked": peer_blocked}


def _reason(name, current, latest, kind, lane, peer_blocked) -> str:
    base = {
        "patch": f"{name} {current} -> {latest} is patch-only; backward compatible.",
        "minor": f"{name} {current} -> {latest} is a minor move; next lane for review.",
        "major": f"{name} {current} -> {latest} is MAJOR; breaking until proven otherwise.",
        "rn-minor": (f"{name} {current} -> {latest}: major-zero 0.x minor moves "
                     "may break compatibility (e.g. React Native 0.x); "
                     "next lane required."),
        "prerelease": (f"{name} {current} -> {latest} involves a pre-release channel; "
                       "next lane required."),
    }.get(kind, f"{name} {current} -> {latest} ({kind}).")
    if peer_blocked:
        base += " Peer dependency conflict detected; stays on next."
    base += f" Target: {'main' if lane == 'main' else 'next'}."
    return base


def local_branches(checkout: Path) -> list:
    from .util import run_cmd
    code, out, _ = run_cmd(["git", "-C", str(checkout), "branch", "--list"])
    if code != 0:
        return []
    # Markers: `*` current, `+` checked out in a linked worktree — both
    # are local branches, never filter them out.
    return [l.strip().lstrip("*+ ").strip() for l in out.splitlines() if l.strip()]


def has_next(checkout: Path) -> bool:
    return "next" in local_branches(Path(checkout))


def probe_next(checkout) -> bool | None:
    """Tri-state branch observation for display only.

    True/False only on a successful local listing of an explicit
    absolute checkout; None on empty, relative, missing, or
    invalid input, or a rejected git read. Uses the same bounded
    read-only argv as local_branches with a small explicit timeout
    for this display probe. Never action authority: display code
    must treat True as a last-scan observation, not a guaranteed
    current state.
    """
    from .util import run_cmd
    if not isinstance(checkout, (str, Path)) or not str(checkout):
        return None
    try:
        path = checkout if isinstance(checkout, Path) else Path(checkout)
    except Exception:
        return None
    # Display lookups bind an explicit absolute checkout path: a
    # relative path (including Path("") which reads as ".") must
    # never silently probe the process working directory.
    if not path.is_absolute():
        return None
    try:
        if not path.is_dir():
            return None
        code, out, _ = run_cmd(["git", "-C", str(path), "branch", "--list"],
                               timeout=10)
    except Exception:
        return None
    if code != 0:
        return None
    names = [line.strip().lstrip("*+ ").strip()
             for line in out.splitlines() if line.strip()]
    return "next" in names


def remote_next_exists(checkout: Path) -> bool:
    """True when origin/next exists (after a fetch). Local read only."""
    from .util import run_cmd
    code, _, _ = run_cmd(["git", "-C", str(Path(checkout)), "rev-parse",
                          "--verify", "origin/next"])
    return code == 0


def _remote_ref_exists(checkout: Path, ref: str) -> bool:
    """True when a remote-tracking ref resolves (after a fetch). Read only."""
    from .util import run_cmd
    code, _, _ = run_cmd(["git", "-C", str(Path(checkout)), "rev-parse",
                          "--verify", ref])
    return code == 0


def ensure_next(checkout: Path, base: str = "main") -> dict:
    """Create local `next` only when missing. Prefers the existing remote
    `origin/next`, then the freshly fetched `origin/<base>`, over a
    possibly stale local base; never resets, rebases, force-pushes, or
    touches the working tree."""
    from .util import run_cmd
    checkout = Path(checkout)
    if has_next(checkout):
        return {"created": False, "reason": "next already exists locally"}
    if remote_next_exists(checkout):
        start = "origin/next"
    elif base and _remote_ref_exists(checkout, f"origin/{base}"):
        start = f"origin/{base}"
    else:
        start = base or "main"
    code, _, err = run_cmd(["git", "-C", str(checkout), "branch", "next", start])
    if code != 0:
        return {"created": False, "reason": f"could not create next: {err.strip()[-200:]}"}
    if start == "origin/next":
        run_cmd(["git", "-C", str(checkout), "branch",
                 "--set-upstream-to=origin/next", "next"])
    fresh = " (fresh remote-tracking ref)" if start.startswith("origin/") else ""
    return {"created": True, "reason": f"created local next from {start}{fresh} (no push)"}


def _next_checked_out(checkout: Path) -> str | None:
    """Worktree path holding `next` checked out, or None. Read only."""
    from .util import run_cmd
    code, out, _ = run_cmd(["git", "-C", str(Path(checkout)), "worktree",
                            "list", "--porcelain"])
    if code != 0:
        return None
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.strip() == "branch refs/heads/next" and path:
            return path
    return None


def maybe_fast_forward_next(checkout: Path, main_branch: str = "main") -> dict:
    """Advance an existing local `next` to its fresh remote line when safe.

    Only fast-forwards: `next` must be a strict ancestor of the fresh ref
    (no unique commits), must not be checked out in any worktree, and the
    target must be a remote-tracking ref (origin/next, else origin/<main>)
    — never a local branch, never force/rebase/reset. Anything else is
    preserved as-is with an explicit reason so callers report an
    integration need instead of silently claiming latest.
    """
    from .util import run_cmd
    checkout = Path(checkout)
    if not has_next(checkout):
        return {"advanced": False, "reason": "no local next (creation owns seeding)"}
    target = None
    if remote_next_exists(checkout):
        target = "origin/next"
    elif main_branch and _remote_ref_exists(checkout, f"origin/{main_branch}"):
        target = f"origin/{main_branch}"
    if target is None:
        return {"advanced": False, "reason": "no fresh remote ref (offline?); next preserved"}
    holder = _next_checked_out(checkout)
    if holder:
        return {"advanced": False,
                "reason": f"next is checked out at {holder}; preserved, needs integration"}
    code, before, _ = run_cmd(["git", "-C", str(checkout), "rev-parse", "next"])
    code2, after, _ = run_cmd(["git", "-C", str(checkout), "rev-parse", target])
    if code != 0 or code2 != 0:
        return {"advanced": False, "reason": "could not resolve next/target; preserved"}
    before, after = before.strip(), after.strip()
    if before == after:
        return {"advanced": True, "before": before, "after": after,
                "reason": f"next already at {target} ({after[:12]}); no-op"}
    ancestor, _, _ = run_cmd(["git", "-C", str(checkout), "merge-base",
                              "--is-ancestor", "next", target])
    if ancestor != 0:
        _, ahead, _ = run_cmd(["git", "-C", str(checkout), "rev-list",
                               "--count", f"{target}..next"])
        unique = ahead.strip() if ahead.strip().isdigit() else "?"
        return {"advanced": False, "diverged": True,
                "reason": f"local next has {unique} unique commit(s) vs {target}; "
                          f"preserved as-is (no reset), needs integration"}
    code, _, err = run_cmd(["git", "-C", str(checkout), "update-ref",
                            "refs/heads/next", after, before])
    if code != 0:
        return {"advanced": False,
                "reason": f"fast-forward refused: {err.strip()[-160:]}"}
    return {"advanced": True, "before": before, "after": after,
            "reason": f"fast-forwarded next {before[:12]} -> {after[:12]} from {target}"}


BREAKING_KINDS = {"major", "minor", "rn-minor", "prerelease", "downgrade"}

_CHECK_SCOPE_PATTERNS = (
    ("types", ("tsc", "typecheck", "pyright", "mypy")),
    ("lint", ("eslint", "lint", "flake8", "ruff")),
    ("unit", ("jest", "vitest", "pytest", "unittest", "go test",
              "flutter test", "yarn test", "npm test", "rspec")),
    ("build", ("build", "assemble", "xcodebuild", "gradle", "eas build")),
    ("native-runtime", ("simulator", "device", "maestro", "connectedcheck",
                        "connectedandroidtest", "xctest", "emulator")),
)


def classify_commands(cmds: list) -> set:
    """Scope labels for actually-executed shell checks.

    A step named 'e2e' may be only `yarn typecheck`: the label says what ran
    (types/lint/unit/build/native-runtime), never what the step is called.
    """
    cmd_lists = []
    for c in cmds or []:
        if isinstance(c, list):
            cmd_lists.append(c)
        elif isinstance(c, str):
            cmd_lists.append(c.split())
    text = " ".join(" ".join(map(str, c)) for c in cmd_lists).lower()
    scopes = {scope for scope, keys in _CHECK_SCOPE_PATTERNS
              if any(k in text for k in keys)}
    if cmd_lists and not scopes:
        scopes.add("other")
    return scopes


def e2e_scopes(state: dict) -> set:
    """Scopes of the shell commands actually executed for the e2e step."""
    entry = (state.get("steps") or {}).get("e2e", {})
    return classify_commands(entry.get("command") or [])


def promotion_readiness(state: dict,
                        current_fingerprint=_store.USE_STORED,
                        current_head=_store.USE_STORED) -> dict:
    """next -> main criteria from run evidence. All must hold.

    Defaults validate against the stored workCommit/workTreeFingerprint
    cache — for execute paths that already refreshed it. Read paths pass
    a freshly read head AND fingerprint, or None when the current source
    is unreadable (never promotable, never green). A moved HEAD blocks
    promotion even when the bytes are identical.
    """
    steps = state.get("steps", {})
    proof = _store.verification_status(
        state, current_fingerprint=current_fingerprint,
        current_head=current_head)
    checks = [
        ("no hook-blocked local commit",
         not state.get("commitBlocked"),
         "the local work commit was rejected by the repo hook; the "
         "candidate is uncommitted, so no verification bound to it "
         "can qualify for promotion"),
        ("review done and bound to current source",
         steps.get("review", {}).get("status") == "done"
         and proof.get("review") == "fresh",
         "review must be done with commit+fingerprint proof of current bytes; "
         "legacy runs without content proof do not qualify"),
        ("e2e verified on current source",
         steps.get("e2e", {}).get("status") == "done"
         and proof.get("e2e") == "fresh",
         "e2e must be done with commit+fingerprint proof of current bytes; "
         "a dirty-tree edit invalidates it even when HEAD is unchanged"),
        ("no open question",
         not (state.get("waitReason") or "").startswith("question:"),
         ""),
        ("release notes drafted",
         bool((state.get("artifacts", {}) or {}).get("release_notes")),
         ""),
    ]
    lane = state.get("lane") or ""
    update_kind = state.get("update_kind") or ""
    if lane == "next" or update_kind in BREAKING_KINDS:
        scopes = e2e_scopes(state)
        covers = bool(scopes & {"unit", "build", "native-runtime"})
        observed = ",".join(sorted(scopes)) if scopes else "no shell checks observed"
        checks.append(
            ("breaking-change coverage beyond typecheck (unit/build/runtime)",
             covers,
             f"a breaking {lane or update_kind} proposal is not promotion-ready "
             f"from typecheck/lint alone; observed e2e scopes: {observed}"))
    ready = all(ok for _, ok, _ in checks)
    return {"ready": ready,
            "criteria": [{"name": n, "ok": bool(ok),
                          **({"detail": d} if d else {})}
                         for n, ok, d in checks]}
