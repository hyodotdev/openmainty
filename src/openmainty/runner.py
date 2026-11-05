"""Step runner: order, isolation, resume, evidence.

Boundary for a future shared coordinator: `Runner` only depends on the
`Executor` interface (worker + shell) and the `Store`-shaped helpers in
store.py. Swapping local files for a shared backend later means replacing
those two seams, not the step logic.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import artifacts, muse, paths, planner, store
from .util import Cancelled, run_cmd, utcnow

QUOTA_HINTS = ("quota", "rate limit", "429", "usage limit", "subscription limit")

ASK_RE = re.compile(r"(?m)^OPENMAINTY_ASK:\s*(.+?)\s*$")

# Conventional-commit subject the repo hooks accept: `type(scope): summary`.
# The run id must never be prefixed ahead of the type — commitlint reads
# the leading word as the type, so `openmainty <id> fix: ...` fails with
# subject-empty/type-empty. Run/step tracing belongs in the body/trailer.
_CONVENTIONAL_SUBJECT_RE = re.compile(
    r"^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(\([^()\n]{1,40}\))?(!)?:\s+\S")


def commit_subject_for(state: dict, step: str = "") -> tuple[str, str]:
    """Repo-conforming (subject, body) for a local work commit.

    Uses the run's own conventional task title when it already parses as
    a conventional subject, else a safe conventional fallback. The run id
    and step travel in the body/trailer only — never ahead of the type.
    Titles are data, never shell: callers pass them as argv elements.
    """
    title = (state.get("title") or "").strip()
    raw = title.splitlines()[0] if title else ""
    subject = raw[:80] if _CONVENTIONAL_SUBJECT_RE.match(raw) \
        else "chore(maint): scheduled maintenance work"
    run_id = state.get("id", "")
    body = f"openmainty {run_id} {step}".strip() + \
        f"\n\nRun: {run_id}\nStep: {step}"
    return subject, body


def extract_ask(out: str):
    """The operator question when a step pauses instead of guessing."""
    m = ASK_RE.search(out or "")
    if not m:
        return None
    return m.group(1)[:500] or None


class QuotaHit(Exception):
    pass


class WorkerBusy(Exception):
    """Another live holder owns the single-worker OS lease.

    Raised by `execute()` BEFORE any model call or state mutation, so a
    refused start/resume consumes nothing: no attempts, no false
    done/failed outcome, no lost run identity, no source mutation."""


class Executor:
    """Real executor: Muse subscription worker + local shell."""

    kind = "muse"

    def __init__(self, model=None, cancel_event=None):
        self.model = muse.resolve_model(model)
        self.cancel_event = cancel_event

    def muse_step(self, step, prompt, workdir):
        code, out, usage = muse.exec_prompt(prompt, workdir=workdir, model=self.model,
                                            cancel_event=self.cancel_event)
        self._raise_if_quota(code, out)
        return code, out, usage

    def shell(self, cmd, cwd):
        return run_cmd(cmd, cwd=cwd, cancel_event=self.cancel_event)

    def muse_step_stream(self, step, prompt, workdir, on_delta, timeout=1200):
        """Stream run_output_delta text while running.

        Returns (code, raw_stream, usage); usage carries terminal_text when
        the run reported a final text. Cancelled propagates (worker killed).
        """
        import subprocess
        from .util import iter_proc_lines
        path = muse.which()
        if not path:
            return 127, "", {"error": "muse CLI not found"}
        cmd = muse.build_cmd(prompt, workdir=workdir, contributor=True, model=self.model)
        env = dict(os.environ)
        env.update(muse.worker_cache_env(workdir))
        proc = subprocess.Popen(
            cmd, cwd=str(workdir) if workdir else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env,
        )
        raw_lines = []
        terminal_text = ""
        try:
            for line in iter_proc_lines(proc, timeout, self.cancel_event):
                raw_lines.append(line)
                kind, text = muse.parse_delta_line(line)
                if kind == "delta":
                    on_delta(text)
                elif kind == "terminal":
                    terminal_text = text
        except subprocess.TimeoutExpired:
            partial = "".join(raw_lines)
            return 124, partial, {"timeout_s": timeout, "tokens_measurable": False,
                                  "tokens": None, "partial": True}
        finally:
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        raw = "".join(raw_lines)
        usage = muse.parse_usage(raw)
        usage["exit_code"] = proc.returncode
        usage["cli_version"] = muse.version()
        if terminal_text:
            usage["terminal_text"] = terminal_text
        self._raise_if_quota(proc.returncode, raw)
        return proc.returncode, raw, usage

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

    def muse_step_stream(self, step, prompt, workdir, on_delta, timeout=1200):
        code, out, usage = self.muse_step(step, prompt, workdir)
        if out.strip():
            on_delta(out.strip())
        return code, out, usage


class FakeExecutor(Executor):
    """Scripted results for tests. script: {step: [(exit, text), ...]}.

    The default (unscripted) triage output carries an ACTIONABLE verdict
    because the fixture stands in for a prompt-obeying worker, and the
    triage prompt mandates a `DISPOSITION:` line. Explicitly scripted
    verdict-less output still parses as UNKNOWN and blocks — the fake
    never silently upgrades a real ambiguous verdict.
    """

    def _default_text(self, step: str) -> str:
        if step == "triage":
            return ("DISPOSITION: ACTIONABLE: fixture task is actionable\n"
                    "fake triage ok")
        if step in ("review", "review_verify"):
            # Faithful to the explicit-clean contract: the fixture stands
            # in for a prompt-obeying reviewer, and the review prompt
            # requires an explicit no-findings verdict. An unscripted
            # ambiguous review must still be scripted explicitly in tests.
            return ("VALID: none. No must-fix findings; safe to proceed.\n"
                    "REVIEW-VERDICT: CLEAN")
        if step == "apply_review":
            # Faithful to the apply contract: the fixture stands in for
            # a prompt-obeying apply worker, and the apply prompt
            # requires APPLIED:/SKIPPED: lines per finding. A bare
            # exit-0-with-neither stays unaddressed, so explicitly
            # scripted marker-less output still fails the apply gate.
            return ("APPLIED: fixture finding addressed (fake apply ok)\n"
                    "fake apply ok")
        return f"fake {step} ok"

    kind = "fake"

    def __init__(self, script=None, cancel_event=None, model=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []
        self.cancel_event = cancel_event
        self.model = model

    def _check_cancel(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled("cancelled by operator")

    def muse_step(self, step, prompt, workdir):
        self.calls.append(step)
        if os.environ.get("OPENMAINTY_FAKE_QUOTA") == step:
            raise QuotaHit("fake quota exhausted")
        if os.environ.get("OPENMAINTY_FAKE_BLOCK") and self.cancel_event is not None:
            self.cancel_event.wait(timeout=30)
        self._check_cancel()
        queue = self.script.get(step, [(0, self._default_text(step))])
        if len(queue) > 1:
            code, text = queue.pop(0)
            self.script[step] = queue
        else:
            code, text = queue[0]
        return code, text, {"fake": True, "tokens_measurable": False, "tokens": None}

    def muse_step_stream(self, step, prompt, workdir, on_delta, timeout=1200):
        code, text, usage = self.muse_step(step, prompt, workdir)
        if text:
            on_delta(text)
        return code, text, usage

    def shell(self, cmd, cwd):
        key = "shell:" + " ".join(cmd[:2])
        self.calls.append(key)
        self._check_cancel()
        queue = self.script.get(key, [(0, "fake shell ok")])
        if len(queue) > 1:
            code, text = queue.pop(0)
            self.script[key] = queue
        else:
            code, text = queue[0]
        return code, text, ""


def make_executor(name: str, model=None, cancel_event=None) -> Executor:
    if name == "muse":
        return Executor(model=model, cancel_event=cancel_event)
    if name == "echo":
        return EchoExecutor()
    if name == "fake":
        return FakeExecutor(cancel_event=cancel_event)
    raise SystemExit(f"error: unknown executor '{name}' (muse|echo|fake)")


# ---- isolated workspace ----

def ensure_workdir(state: dict, checkout: Path, base_branch: str) -> Path:
    """Legacy entry: detached worktree at the base (chat/interactive runs)."""
    return ensure_branch_workdir(state, checkout, base_branch, branch=None)


def ensure_branch_workdir(state: dict, checkout: Path, base_branch: str,
                          branch: str | None = None) -> Path:
    """Named feature worktree from the lane's exact base.

    - `branch` set: `git worktree add -b <branch> <work> <base>` (or attach
      to the existing local branch). The run persists branch/base so review
      can prove the strategy executed.
    - Missing base/branch refs fail explicitly (no HEAD fallback) and the
      caller's checkout is never modified: all writes stay under the run dir.
    - Installed dependencies are NEVER shared with the original checkout:
      any `node_modules` symlink escaping the worktree is removed, so an
      install/update inside the worktree cannot mutate the user's checkout
      and verification cannot pass against stale packages.
    """
    work = store.run_dir(state["id"]) / "work"
    if work.exists():
        _isolate_node_modules(work)
        return work
    # `.git` is a FILE inside linked worktrees — `.is_dir()` would miss
    # them and silently copy the checkout without git metadata. `.exists()`
    # keeps every git checkout (file or dir) on the worktree path.
    if (checkout / ".git").exists():
        base, code, err = git_rev_strict(checkout, base_branch)
        if code != 0:
            state.setdefault("interventions", []).append(
                {"at": utcnow(), "kind": "worktree-base-missing",
                 "detail": f"missing base ref '{base_branch}': {err.strip()[-200:]}"})
            store.save(state)
            raise SystemExit(f"error: missing base ref '{base_branch}' "
                             f"(no HEAD fallback): {err.strip()[-160:]}")
        if branch:
            # M2.9b: a post-checkout hook would run outside any
            # sandbox — worktree adds stay hook-free like every
            # other app git call.
            code, _, err = run_cmd(
                ["git"] + _GIT_NO_HOOKS + ["-C", str(checkout),
                                           "worktree", "add",
                                           "-b", branch, str(work),
                                           base])
            if code != 0:
                # Branch may already exist locally: attach to it instead of
                # forking a duplicate.
                code2, _, err2 = run_cmd(
                    ["git"] + _GIT_NO_HOOKS + ["-C", str(checkout),
                                               "worktree", "add",
                                               str(work), branch])
                if code2 != 0:
                    state.setdefault("interventions", []).append(
                        {"at": utcnow(), "kind": "worktree-branch-missing",
                         "detail": f"branch '{branch}': {(err2 or err).strip()[-200:]}"})
                    store.save(state)
                    raise SystemExit(
                        f"error: cannot attach branch '{branch}': "
                        f"{(err2 or err).strip()[-160:]}")
        else:
            code, _, err = run_cmd(
                ["git"] + _GIT_NO_HOOKS + ["-C", str(checkout),
                                           "worktree", "add",
                                           "--detach", str(work), base])
            if code == 0:
                _isolate_node_modules(work)
                return work
            state.setdefault("interventions", []).append(
                {"at": utcnow(), "kind": "worktree-fallback",
                 "detail": err.strip()[-300:]})
        _isolate_node_modules(work)
        return work
    shutil.copytree(checkout, work, ignore=shutil.ignore_patterns(".git"))
    _isolate_node_modules(work)
    return work


def _isolate_node_modules(work: Path) -> str | None:
    """Remove worktree dependency links that escape into other checkouts.

    Returns a note when something was removed (callers record it as an
    intervention); None when the worktree was already isolated. Only
    touches paths inside the run dir — never the original checkout.
    """
    removed = []
    for name in ("node_modules", ".pnp.cjs", ".pnp.loader.mjs"):
        link = work / name
        try:
            if link.is_symlink():
                try:
                    target = link.resolve()
                except OSError:
                    target = None
                try:
                    if target is not None:
                        target.relative_to(work.resolve())
                        continue  # points inside the worktree: fine
                except ValueError:
                    pass
                link.unlink()
                removed.append(name)
        except OSError:
            pass
    if removed:
        return ("removed worktree dependency link(s) escaping to another "
                f"checkout: {', '.join(removed)}; install inside the worktree")
    return None


def git_rev(path: Path, ref: str):
    code, out, _ = run_cmd(["git", "-C", str(path), "rev-parse", ref])
    if code != 0:
        code2, out2, _ = run_cmd(["git", "-C", str(path), "rev-parse", "HEAD"])
        return (out2.strip() if code2 == 0 else "unknown"), code2
    return out.strip(), 0


def git_rev_strict(path: Path, ref: str):
    """(rev, code, err) for a ref that MUST exist. No HEAD fallback."""
    code, out, err = run_cmd(
        ["git", "-C", str(path), "rev-parse", "--verify", ref])
    if code != 0:
        return "", code, err
    return out.strip(), 0, ""


def work_commit(work: Path, state: dict) -> str:
    """HEAD only. It CANNOT identify dirty-tree content: an edited worktree
    keeps the same HEAD, so callers must also bind `source_snapshot`."""
    code, out, _ = run_cmd(["git", "-C", str(work), "rev-parse", "HEAD"])
    if code == 0 and out.strip():
        return out.strip()
    return f"copyseq-{state.get('workSeq', 0)}"


# ---- source identity: bind verification to content, not just HEAD ----

_FINGERPRINT_NOISE_DIRS = frozenset({
    "node_modules", ".git", "__pycache__", ".expo", "Pods", ".yarn",
    "build", "dist", "out", ".next", ".turbo", "coverage", ".venv",
    "venv", "target", "DerivedData", ".gradle",
    # Run-owned private toolchain caches (muse.CACHE_DIR_NAME) are never
    # source: worker yarn/npm/corepack writes must not invalidate
    # review/check proof, enter commit candidates, or block recovery.
    muse.CACHE_DIR_NAME,
})
_FINGERPRINT_NOISE_FILES = frozenset({
    ".pnp.cjs", ".pnp.loader.mjs", ".DS_Store",
})

# Watchman sync cookies (.watchman-cookie-<host>-<pid>-<n>) land in the
# ROOT of linked worktrees (which carry a .git FILE, so watchman cannot
# use a .git dir) on every watchman sync sweep (~60 s cadence, ~20 ms
# life). They are untracked, never git-ignored, and never repo content:
# without this rule a sweep-aligned status read reports uncommitted
# changes and a sweep-aligned snapshot hashes them into the fingerprint.
# Name-pattern only; arbitrary dotfiles and real untracked source are
# unaffected.
_WATCHMAN_COOKIE_PREFIX = ".watchman-cookie"


def _is_watchman_cookie(rel: str) -> bool:
    """True for watchman sync-cookie basenames at any depth.

    Shared predicate used by both _noise_rel and _worktree_changed so the
    two agree on cookies without sharing any other rule: in particular
    _worktree_changed must NOT adopt the whole _noise_rel (a fix step
    whose only change is a TRACKED file under a noise-named dir such as
    dist/ is still a real change there, because the local work commit
    stages tracked changes with `git add -u`).
    """
    parts = [p for p in str(rel or "").replace("\\", "/").split("/")
             if p not in ("", ".")]
    if not parts:
        return False
    return parts[-1] == _WATCHMAN_COOKIE_PREFIX or parts[-1].startswith(
        _WATCHMAN_COOKIE_PREFIX + "-")


def _noise_rel(rel: str) -> bool:
    """Generated/cache paths never count as source (git ignores are honoured
    separately via status/check-ignore; this only skips well-known junk even
    when it is not ignored)."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return True
    if parts[-1] in _FINGERPRINT_NOISE_FILES:
        return True
    if _is_watchman_cookie(rel):
        return True
    return any(p in _FINGERPRINT_NOISE_DIRS for p in parts[:-1])


def _hash_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


class SourceUnreadable(Exception):
    """A critical source read failed — never a content identity.

    `op` names the failed read category (list/status/read verb plus the
    repo-relative path when one applies) and `rc` the git exit when one
    applies. Only the category travels outward: no stderr text, no file
    bytes, no secrets. Callers must report unknown/retryable and preserve
    stored proofs — never an empty or UNREADABLE-marked fingerprint.
    """

    def __init__(self, op: str, rc=None):
        super().__init__(f"source unreadable ({op}"
                         f"{f', exit {rc}' if rc is not None else ''})")
        self.op = op
        self.rc = rc


# Read-only git probes must fail fast: a stuck worktree (e.g. a wedged
# `git commit` elsewhere) must degrade to SourceUnreadable, never stall
# overview for the 600s run_cmd default.
_GIT_PROBE_TIMEOUT = 30


def _git_z(work: Path, args: list, op: str) -> list:
    code, out, _ = run_cmd(["git", "-C", str(work)] + args,
                           timeout=_GIT_PROBE_TIMEOUT)
    if code != 0:
        raise SourceUnreadable(op, rc=code)
    return [p for p in out.split("\0") if p]


def _is_ignored(work: Path, rel: str) -> bool:
    code, _, _ = run_cmd(["git", "-C", str(work), "check-ignore", "-q", "--", rel],
                         timeout=_GIT_PROBE_TIMEOUT)
    return code == 0


def _classify(p: Path, rel: str) -> tuple:
    """(is_dir, is_file) with stat failures told apart from deletion.

    `Path.is_file/is_dir` swallow stat errors as False, so a bare
    (False, False) is ambiguous: re-stat once. Genuinely absent
    (ENOENT, or ENOTDIR where an ancestor became a file) stays
    (False, False) — callers record DELETED or skip as before. Any
    other stat failure (permission/stat denial) raises
    `SourceUnreadable` instead of masquerading as a deletion or a
    silent skip.
    """
    try:
        is_dir = p.is_dir()
        is_file = False if is_dir else p.is_file()
    except OSError:
        raise SourceUnreadable(f"stat {rel}")
    if not is_dir and not is_file:
        try:
            p.stat()
        except (FileNotFoundError, NotADirectoryError):
            # Genuinely absent (or unresolvable because an ancestor
            # became a file): a real deletion, not a read failure.
            pass
        except OSError:
            raise SourceUnreadable(f"stat {rel}")
    return is_dir, is_file


def _git_present(work: Path) -> bool:
    """Explicit .git presence via lstat: version-independent.

    Missing (ENOENT/ENOTDIR) means non-git. Any other OSError
    (EACCES, ...) means the answer is unknowable and the tree can
    be trusted in neither branch: SourceUnreadable.
    `Path.exists()` cannot be used here: it reports EACCES as
    False on some versions, silently flipping an unreadable git
    tree into the copy branch (an empty identity on 3.14).
    """
    try:
        (work / ".git").lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        raise SourceUnreadable("git-present")
    return True


def _noise_covered_dir(rel: str) -> bool:
    """True when every file under directory rel is noise-skipped anyway.

    `_noise_rel` skips a file when any PARENT part is a noise dir, so
    a directory whose own rel path already contains a noise part can
    never contribute source: pruning it (or excusing its unreadable
    warning) changes no fingerprint. Exactly `_noise_rel(rel +
    "/<anything>")`; the root itself is never pruned.
    """
    return bool(rel) and _noise_rel(rel + "/_")


def _check_ignore_set(work: Path, rels: list) -> set:
    """Subset of rels that git ignores (one batch call).

    A failed batch answers empty, which callers treat as "nothing
    proven ignored" (fail closed). Odd names that come back quoted
    simply never match (fail closed).
    """
    if not rels:
        return set()
    code, out, _ = run_cmd(
        ["git", "-C", str(work), "check-ignore", "--"] + list(rels),
        timeout=_GIT_PROBE_TIMEOUT)
    if code not in (0, 1):
        return set()
    return {ln for ln in (out or "").splitlines() if ln}


# A directory level wider than this gets one batched check-ignore
# so big ignored trees (generated output, vendored deps) are pruned
# without walking them. Narrow levels skip the spawn: a healthy
# snapshot costs zero added subprocesses.
_WALK_LEVEL_BATCH_MIN = 200


def _ensure_untracked_complete(work: Path) -> None:
    """Prove git's untracked listing could not have missed source.

    `git status` exits 0 and only warns when it cannot open a
    directory, silently dropping hidden source from the listing (a
    false green). This independent check verifies every directory
    git would need to open is listable — no stderr parsing, so it
    holds in every locale. Noise-covered subtrees are pruned
    locally (they never enter the fingerprint); levels wider than
    _WALK_LEVEL_BATCH_MIN get one batched check-ignore to prune
    git-ignored subtrees the same way. Unreadable directories that
    remain are attributed with one final batch check-ignore, because
    git-ignored subtrees are outside untracked discovery by
    definition and cannot hide source. Anything else unlistable
    raises SourceUnreadable. A mid-walk deletion (ENOENT/ENOTDIR)
    is a real deletion, not unreadability.
    """
    failed = []
    current = [""]
    while current:
        nxt = []
        for rel_dir in current:
            abs_dir = work if not rel_dir else work / rel_dir
            try:
                with os.scandir(abs_dir) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                failed.append(rel_dir or ".")
                continue
            for entry in entries:
                rel = (entry.name if not rel_dir
                       else rel_dir + "/" + entry.name)
                if rel == ".git" or rel.startswith(".git/"):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError:
                    # Unstattable: file or dir is unknowable, so it
                    # could hide source. Fail closed.
                    raise SourceUnreadable("stat %s" % rel)
                if not is_dir:
                    continue
                if _noise_covered_dir(rel):
                    continue
                nxt.append(rel)
        if len(nxt) > _WALK_LEVEL_BATCH_MIN:
            ignored = _check_ignore_set(work, nxt)
            if ignored:
                nxt = [d for d in nxt if d not in ignored]
        current = nxt
    if "." in failed:
        raise SourceUnreadable("list .")
    if not failed:
        return
    ignored = _check_ignore_set(work, failed)
    for rel in failed:
        if rel not in ignored:
            raise SourceUnreadable("list %s" % rel)


def _list_tree_strict(work: Path, root_rel: str, prune) -> list:
    """Sorted [(rel, Path)] of every entry under root_rel. Never silent.

    rglob-equivalent listing with error propagation: an unreadable
    directory or entry (anything but a mid-walk deletion) raises
    SourceUnreadable instead of vanishing from the listing.
    `prune(rel)` skips descending a subdir whose whole subtree the
    caller excludes anyway (noise/ignored); the .git tree is never
    descended. Callers sort hashed entries themselves, so output is
    identical to the old walk.
    """
    out = []
    root_rel = (root_rel or "").rstrip("/")
    root = work if not root_rel else work / root_rel
    stack = [(root_rel, root)]
    while stack:
        rdir, adir = stack.pop()
        try:
            with os.scandir(adir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            raise SourceUnreadable("list %s" % (rdir or "."))
        for entry in entries:
            rel = entry.name if not rdir else rdir + "/" + entry.name
            if rel == ".git" or rel.startswith(".git/"):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                raise SourceUnreadable("stat %s" % rel)
            out.append((rel, work / rel))
            if is_dir and not (prune and prune(rel)):
                stack.append((rel, work / rel))
    out.sort(key=lambda t: t[0])
    return out


def _resolve_gitdir(work: Path, dotgit: Path) -> Path | None:
    """Worktree's git dir: `.git` itself, or its `gitdir:` target.

    Every real run worktree is linked: `.git` is a pointer FILE
    ("gitdir: <path>", relative to the worktree when not absolute).
    Returns None when the pointer is unparsable or unreadable.
    """
    try:
        if dotgit.is_dir():
            return dotgit
        if not dotgit.is_file():
            return None
        text = dotgit.read_text(
            encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:"):].strip())
    if not str(target):
        return None
    return target if target.is_absolute() else work / target


def _git_common_dir(gitdir: Path) -> Path | None:
    """Shared ref store: `gitdir/commondir` target, else the gitdir.

    The commondir pointer is relative to the gitdir when not
    absolute. Returns None when present-but-unreadable.
    """
    try:
        pointer = gitdir / "commondir"
        if not pointer.is_file():
            return gitdir
        text = pointer.read_text(
            encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    target = Path(text)
    return target if target.is_absolute() else gitdir / target


_SYSTEM_CONFIG_CANDIDATES = (
    "/etc/gitconfig",
    "/opt/homebrew/etc/gitconfig",
    "/usr/local/etc/gitconfig",
)

# Scope-cache window: matches graph._SNAP_MEMO_TTL by design
# (pinned equal by P1ScopeCacheTest).
_SCOPE_CACHE_TTL = 300.0
_SCOPE_ENV_VARS = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                    "GIT_CONFIG_NOSYSTEM", "XDG_CONFIG_HOME", "HOME")
_SCOPE_CACHE = {"at": 0.0, "env": None, "origins": (), "valid": False}

# Test seam for the scope-cache clock (mirrors graph._mono): tests
# pin this to control refresh timing explicitly.
_scope_now = time.monotonic


def _git_config_origins():
    """(rc, stdout) of the scope-listing git call (seam for tests).

    Lists config key NAMES with their file origins and scopes from
    a neutral empty directory under the app home (never a
    worktree); reads no values. Tests mock this; production runs
    real git with stdin closed and a short timeout.
    """
    probe_dir = paths.home() / "tmp" / "scope-probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    code, out, _err = run_cmd(
        ["git", "config", "--list", "--show-origin", "--show-scope",
         "--name-only"],
        cwd=probe_dir, timeout=5, stdin=subprocess.DEVNULL)
    return code, out


def _parse_scope_origins(text):
    """((scope, path)...) out of --show-origin --show-scope output.

    Collects system/global/unknown file origins (Apple's
    developer-dir file reports scope "unknown"; plain include
    targets appear as their own origins). Returns None —
    fail-closed — on any unparseable line, any non-file origin
    (env-injected config is unstatable), or any quoted/relative
    path. local/worktree lines are ignored: repo-local configs
    stay covered by the common/gitdir scan.
    """
    origins = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            return None
        scope, origin, _name = parts
        if not origin.startswith("file:"):
            return None
        if scope not in ("system", "global", "unknown"):
            continue
        path = origin[len("file:"):]
        if not path or not os.path.isabs(path):
            return None
        if '"' in path or "\\" in path:
            return None
        origins.append((scope, path))
    return tuple(dict.fromkeys(origins))


def refresh_scope_cache():
    """Refresh the process-level git config scope cache (P1-live).

    Runs at most one `git config` subprocess per _SCOPE_CACHE_TTL,
    plus one whenever the scope-determining env values change; a
    failed refresh still throttles (no git-spam while git is
    broken) but marks the cache invalid. Reads only scope/origin/
    key-name lines, never values.

    Staleness rule (exact): edits to any known origin file or
    excludes target invalidate the signature immediately; a scope
    file that is CREATED (or first gains entries) after the last
    refresh is seen at the next refresh, at most one window later.
    No unbounded trust: an invalid, stale, or env-changed cache
    keeps the 300 s memo backstop.

    worktree_signature never calls this (it stays subprocess-free);
    the memo refreshes before using the signature.
    """
    global _SCOPE_CACHE
    now = _scope_now()
    env = tuple(os.environ.get(v) for v in _SCOPE_ENV_VARS)
    cur = _SCOPE_CACHE
    if cur["env"] is not None and cur["env"] == env \
            and now - cur["at"] < _SCOPE_CACHE_TTL:
        return
    origins: tuple = ()
    valid = False
    try:
        code, out = _git_config_origins()
        if code == 0:
            parsed = _parse_scope_origins(out)
            if parsed is not None:
                origins = parsed
                valid = True
    except Exception:  # noqa: BLE001 - any failure keeps the backstop
        valid = False
    _SCOPE_CACHE = {"at": now, "env": env, "origins": origins,
                    "valid": valid}


def _cached_scope_origins():
    """(origins, fresh_and_valid) from the process scope cache.

    Pure read: never spawns. A stale (older than the window),
    env-changed, or invalid cache reads as empty/invalid — the
    memo refreshes before use, so direct callers safely fall back
    to the legacy uncertain behavior.
    """
    cur = _SCOPE_CACHE
    if cur["env"] is None:
        return (), False
    env = tuple(os.environ.get(v) for v in _SCOPE_ENV_VARS)
    if cur["env"] != env:
        return (), False
    if _scope_now() - cur["at"] >= _SCOPE_CACHE_TTL:
        return (), False
    if not cur["valid"]:
        return (), False
    return cur["origins"], True


def _env_skips_system() -> bool:
    """True when GIT_CONFIG_NOSYSTEM tells git to skip system config.

    Probed boolean vocabulary (isolated git): true/yes/on in any
    case, or a nonzero signed decimal integer (surrounding space
    tolerated), skips; unset, empty, 0, false/no/off and anything
    unrecognised keep the system scope active.
    """
    raw = os.environ.get("GIT_CONFIG_NOSYSTEM")
    if raw is None:
        return False
    val = raw.strip().lower()
    if val in ("true", "yes", "on"):
        return True
    if re.fullmatch(r"[+-]?\d{1,18}", val):
        try:
            return int(val) != 0
        except ValueError:
            return False
    return False


def _config_scope_files() -> tuple:
    """(config files, default excludes path, uncertain) git consults.

    The exact config files in scope. Only a truthy
    GIT_CONFIG_NOSYSTEM (probed boolean) or a fresh, valid scope
    cache (P1-live: git itself lists the system/global/unknown
    origins, including Apple's unknown-scope developer-dir file
    and plain include targets) locates the system scope; a set
    GIT_CONFIG_SYSTEM without a valid cache is still statted and
    scanned (coverage kept) but stays uncertain, and an unset one
    lists every known candidate AND stays uncertain.
    GIT_CONFIG_GLOBAL, when present — even empty (probed) —
    replaces ~/.gitconfig and the XDG config; an empty value
    reads no global file. Cached global origins join the legacy
    set as a superset. The default excludes path
    (~/.config/git/ignore, or under XDG_CONFIG_HOME when set)
    always applies, independent of which global config file is
    read (probed).

    `uncertain` when some scope cannot be located cheaply: any
    non-skipped system scope without a fresh valid cache, an
    empty HOME, or a ~/relative XDG/GLOBAL/SYSTEM path. Raises
    (OSError, RuntimeError, KeyError, ValueError) when the home
    directory cannot be resolved; callers fail closed in their own
    convention.
    """
    home = Path.home()
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    files = []
    uncertain = False
    if "HOME" in os.environ and not os.environ["HOME"]:
        # Present-but-empty HOME: unresolvable without guessing.
        uncertain = True
    xdg_ok = (not xdg) or (not xdg.startswith("~")
                           and os.path.isabs(xdg))
    if xdg and not xdg_ok:
        # The default excludes path itself is unresolvable, no
        # matter which global config file is in scope.
        uncertain = True
    cached, cache_ok = _cached_scope_origins()
    cached_sys = []
    cached_glob = []
    if cache_ok:
        for scope, path in cached:
            if scope == "global":
                cached_glob.append(path)
            else:
                cached_sys.append(path)
    if _env_skips_system():
        pass
    elif cache_ok:
        for path in cached_sys:
            files.append(("scope-origin-sys", Path(path)))
    else:
        # F1: only a skipping NOSYSTEM (or, now, a fresh valid
        # scope cache) locates the system scope. A set SYSTEM file
        # is still statted and scanned (coverage kept), but Apple
        # Git reads an additional unknown-scope file, so the scope
        # stays uncertain either way.
        uncertain = True
        if "GIT_CONFIG_SYSTEM" in os.environ:
            val = os.environ["GIT_CONFIG_SYSTEM"]
            if val and not val.startswith("~") and os.path.isabs(val):
                files.append(("gitconfig-system-env", Path(val)))
        else:
            for cand in _SYSTEM_CONFIG_CANDIDATES:
                files.append(("gitconfig-system-cand", Path(cand)))
    if "GIT_CONFIG_GLOBAL" in os.environ:
        val = os.environ["GIT_CONFIG_GLOBAL"]
        if not val:
            pass  # probed: present-but-empty reads no global file
        elif not val.startswith("~") and os.path.isabs(val):
            files.append(("gitconfig-global-env", Path(val)))
        else:
            uncertain = True
    else:
        files.append(("gitconfig", home / ".gitconfig"))
        if xdg and xdg_ok:
            xdg_cfg = Path(xdg) / "git" / "config"
            if xdg_cfg != home / ".config" / "git" / "config":
                files.append(("gitconfig-xdg-env", xdg_cfg))
        files.append(("gitconfig-xdg", home / ".config" / "git" / "config"))
    for path in cached_glob:
        files.append(("scope-origin-glob", Path(path)))
    default_ignore = (Path(xdg) / "git" / "ignore" if xdg
                      else home / ".config" / "git" / "ignore")
    return files, default_ignore, uncertain


def _global_excludes_paths(gitdir: Path, common: Path) -> list:
    """(tag, path) pairs covering git's global exclude/config inputs.

    The scope config files git consults (system/global per the
    env, the common-dir config and the worktree-local gitdir
    config) plus the default excludes file. Raises
    `SourceUnreadable` when the home directory cannot even be
    resolved — fail closed, never a partial set.
    """
    try:
        scope_files, default_ignore, _uncertain = _config_scope_files()
        paths = list(scope_files)
        paths.append(("excludes", default_ignore))
        paths.append(("common-config", common / "config"))
        paths.append(("local-config", gitdir / "config"))
        return paths
    except (OSError, RuntimeError, KeyError, ValueError) as e:
        raise SourceUnreadable("excludes-home") from e


def _strip_config_comment(value: str) -> str:
    """Cut an unquoted trailing `;`/`#` comment (small tolerance)."""
    quote = None
    for i, c in enumerate(value):
        if quote:
            if c == quote:
                quote = None
        elif c in ("'", '"'):
            quote = c
        elif c in (";", "#"):
            return value[:i]
    return value


def _scan_core_excludes(text: str) -> tuple:
    """(excludesfile values, uncertain) out of one config file's text.

    Collects every `[core] excludesfile` value (case-insensitive
    key), including a key on the same line as its section header
    (git honours `[core] excludesfile = ...`); later files win at
    the call site, so all candidates are kept. `uncertain` when an
    include/includeIf section (whose target could name another
    excludes file), a header without a closing `]`, or an exactly
    unparseable value line is present.
    """
    values = []
    uncertain = False
    section = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        i += 1
        if not raw or raw[0] in "#;":
            continue
        if raw.startswith("["):
            head, sep, rest = raw[1:].partition("]")
            if not sep:
                uncertain = True
                continue
            name = head.strip().split()
            sec = name[0].lower() if name else ""
            section = sec
            if sec in ("include", "includeif"):
                uncertain = True
            raw = rest.strip()
            if not raw or raw[0] in "#;":
                continue
        if "=" not in raw:
            continue
        key, _, val = raw.partition("=")
        # Backslash-newline continuation (fold at most a few lines).
        joined = val.rstrip()
        while joined.endswith("\\") and i < len(lines):
            joined = joined[:-1] + lines[i].strip()
            i += 1
        val = _strip_config_comment(joined).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if section == "core" and key.strip().lower() == "excludesfile":
            if val.count('"') % 2 or val.count("'") % 2 or "\\" in val:
                uncertain = True
                continue
            if val:
                values.append(val)
    return values, uncertain


def _resolve_excludes_target(value: str, work: Path, home: Path) -> Path:
    """Resolve one excludesfile value the way git does (probed).

    `~` expands against the given home; git does NOT expand
    `$vars` (left literal); a relative path resolves against the
    worktree (verified: git honours repo-relative, not
    home-relative, targets). `~user` forms are left literal — the
    scan flags them uncertain so they keep the time backstop.
    """
    if value == "~" or value.startswith("~/"):
        expanded = str(home) + value[1:]
    else:
        expanded = value
    target = Path(expanded)
    return target if target.is_absolute() else work / target


def _excludes_scan(common: Path, gitdir: Path) -> tuple:
    """(candidate target strings, uncertain) over the config scopes.

    Reads every config file in scope (system/global per the env,
    common-dir and worktree-local); every excludesfile value found
    anywhere is a candidate (precedence does not matter for stat
    coverage). Unreadable content, an unlocatable scope, or any
    include/includeIf section yields uncertainty, never a guess.
    """
    try:
        scope_files, _ignore, scope_uncertain = _config_scope_files()
    except (OSError, RuntimeError, KeyError, ValueError):
        return [], True
    files = [path for _tag, path in scope_files]
    files.append(common / "config")
    files.append(gitdir / "config")
    targets: list = []
    uncertain = scope_uncertain
    seen = set()
    for path in files:
        if path in seen:
            continue
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except (NotADirectoryError, OSError):
            uncertain = True
            continue
        found, weird = _scan_core_excludes(text)
        targets.extend(found)
        uncertain = uncertain or weird
    for value in targets:
        if value.startswith("~") and value != "~" \
                and not value.startswith("~/"):
            uncertain = True
    return targets, uncertain


def worktree_signature_backstop(work) -> bool:
    """True when this worktree's memo entries need a time backstop.

    Only excludes-uncertain git worktrees (include/includeIf in
    scope, any non-skipped system scope without a fresh valid
    scope cache, or otherwise unresolvable excludes inputs) need
    one; plain worktrees and fully resolved ones revalidate purely
    by signature. Any failure here fails toward the backstop
    (bounded re-reads), never toward unbounded trust.
    """
    try:
        work = Path(work)
        dotgit = work / ".git"
        if not (dotgit.exists() or dotgit.is_symlink()):
            return False
        gitdir = _resolve_gitdir(work, dotgit)
        if gitdir is None:
            return True
        common = _git_common_dir(gitdir)
        if common is None:
            return True
        _targets, uncertain = _excludes_scan(common, gitdir)
        return bool(uncertain)
    except Exception:  # noqa: BLE001 - uncertainty takes the backstop
        return True


def _packed_ref_lookup(raw: bytes, ref: str) -> str | None:
    """Loose-less ref value out of packed-refs bytes (pure)."""
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref and sha.strip():
            return sha.strip()
    return None


def worktree_signature(work: Path) -> str | None:
    """Cheap exact-change signature for memoising source_snapshot.

    Git mode (`.git` present — a plain dir, or a linked worktree's
    `gitdir:` pointer file) covers, read directly with no subprocess:
    the per-worktree HEAD content; for a symref HEAD, the resolved
    ref sha (loose ref in the common dir, else packed-refs); the
    packed-refs content hash plus (size, mtime_ns, ctime_ns) when
    present; the per-worktree index (size, mtime_ns, ctime_ns) when
    present; the common info/exclude (size, mtime_ns, ctime_ns) when
    present; every config file in scope — system (skipped, or the
    fresh scope-cache origins, else per GIT_CONFIG_SYSTEM / every
    known candidate), global (per GIT_CONFIG_GLOBAL, else
    ~/.gitconfig and the XDG config, plus cached global origins),
    common-dir and worktree-local — plus the default excludes file
    and every resolved core.excludesFile target (size, mtime_ns,
    ctime_ns) when present, so ignore and config edits invalidate;
    plus the scope-determining env values themselves
    (GIT_CONFIG_GLOBAL/SYSTEM/NOSYSTEM, XDG_CONFIG_HOME, HOME),
    so a scope flip invalidates even when no file exists yet; plus
    the sorted cached origin list, so a newly discovered origin
    invalidates. Plus per-file (size, mtime_ns, ctime_ns) over
    every non-noise file outside `.git` — a superset of what the
    snapshot hashes, so in-tree ignore-rule edits can only cause
    an extra re-read, never a stale hit.

    ctime_ns is kernel-set on every write and cannot be forged via
    utime, so even a same-size rewrite with the old mtime restored
    changes the signature.

    NOT covered inline: config scopes using include/includeIf
    (plain include targets surface as scope-cache origins, but a
    conditional target can hide from the neutral-dir listing), any
    non-skipped system scope without a fresh valid scope cache
    (only a truthy GIT_CONFIG_NOSYSTEM or a fresh cache removes
    the backstop for the system side), or otherwise unresolvable
    excludes inputs — those worktrees keep a bounded 300 s memo
    backstop via worktree_signature_backstop instead of unbounded
    trust. A scope file CREATED after the last cache refresh is
    seen at the next refresh, at most one window later; edits to
    known files invalidate immediately. Env-injected config
    surfaces as a non-file origin and invalidates the cache
    (bounded backstop, never silent); GIT_DIR-style overrides are
    out of scope: process env is assumed stable, as before.
    Everything else the snapshot reads is covered, so resolved
    worktrees revalidate purely by signature.
    Returns None whenever the worktree or any covered git input is
    missing-but-required or unreadable — callers must re-read (or
    report unknown), never serve a memoised snapshot.
    """
    import hashlib
    try:
        if not work.is_dir():
            return None
        dotgit = work / ".git"
        git_present = dotgit.exists() or dotgit.is_symlink()
        head_bits = []
        stat_bits = []
        packed_hash = None
        if git_present:
            gitdir = _resolve_gitdir(work, dotgit)
            if gitdir is None:
                raise SourceUnreadable("gitdir")
            common = _git_common_dir(gitdir)
            if common is None:
                raise SourceUnreadable("commondir")
            try:
                head_raw = (gitdir / "HEAD").read_text(
                    encoding="utf-8", errors="replace").strip()
            except OSError:
                raise SourceUnreadable("HEAD")
            head_bits.append(head_raw)
            if head_raw.startswith("ref:"):
                ref = head_raw[4:].strip()
                if not ref:
                    raise SourceUnreadable("HEAD ref")
                try:
                    sha = (common / ref).read_text(
                        encoding="utf-8", errors="replace").strip()
                except FileNotFoundError:
                    sha = None
                except OSError:
                    raise SourceUnreadable("ref " + ref)
                try:
                    packed_raw = (common / "packed-refs").read_bytes()
                    packed_st = (common / "packed-refs").stat()
                except FileNotFoundError:
                    packed_raw = None
                except OSError:
                    raise SourceUnreadable("packed-refs")
                if packed_raw is not None:
                    packed_hash = hashlib.sha256(packed_raw).hexdigest()
                    stat_bits.append(("packed-refs", packed_st.st_size,
                                      packed_st.st_mtime_ns,
                                      packed_st.st_ctime_ns))
                    if not sha:
                        sha = _packed_ref_lookup(packed_raw, ref)
                if not sha:
                    raise SourceUnreadable("unresolved " + ref)
                head_bits.append(sha)
            for tag, path in (("index", gitdir / "index"),
                              ("exclude", common / "info" / "exclude")):
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                except OSError:
                    raise SourceUnreadable(tag)
                stat_bits.append((tag, st.st_size,
                                  st.st_mtime_ns, st.st_ctime_ns))
            # P1: global exclude/config inputs. Untracked discovery
            # (status/check-ignore) honours them, so the signature
            # stats the default excludes file, every config file that
            # can name core.excludesFile, and each resolved custom
            # target. Includes (or anything unresolvable) keep a
            # per-worktree 300 s backstop via
            # worktree_signature_backstop; the memo consults it.
            for tag, path in _global_excludes_paths(gitdir, common):
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                except OSError:
                    raise SourceUnreadable(tag)
                stat_bits.append((tag, st.st_size,
                                  st.st_mtime_ns, st.st_ctime_ns))
            try:
                home = Path.home()
            except (OSError, RuntimeError, KeyError, ValueError) as e:
                raise SourceUnreadable("excludes-home") from e
            cand_targets, _c_uncertain = _excludes_scan(common, gitdir)
            for i, raw_target in enumerate(cand_targets):
                target = _resolve_excludes_target(raw_target, work, home)
                tag = f"excludes-target-{i}"
                try:
                    st = target.stat()
                except FileNotFoundError:
                    continue
                except OSError:
                    raise SourceUnreadable(tag)
                stat_bits.append((tag, st.st_size,
                                  st.st_mtime_ns, st.st_ctime_ns))
        # Explicit stack (not rglob) so .git and noise dirs are pruned
        # before descent: the snapshot never hashes inside them either.
        rows = []
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            abs_dir = work if not rel_dir else work / rel_dir
            try:
                with os.scandir(abs_dir) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                return None
            for entry in entries:
                rel = entry.name if not rel_dir else rel_dir + "/" + entry.name
                if rel == ".git" or rel.startswith(".git/"):
                    continue
                if _noise_rel(rel):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(rel)
                        continue
                    is_file = entry.is_file(follow_symlinks=True)
                except OSError:
                    return None
                if not is_file:
                    continue
                try:
                    st = entry.stat(follow_symlinks=True)
                except OSError:
                    return None
                rows.append((os.fsencode(rel), st.st_size,
                             st.st_mtime_ns, st.st_ctime_ns))
        rows.sort(key=lambda r: r[0])
        digest = hashlib.sha256()
        digest.update(b"git\0" if git_present else b"plain\0")
        for var in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                    "GIT_CONFIG_NOSYSTEM", "XDG_CONFIG_HOME", "HOME"):
            marker = os.environ.get(var)
            digest.update(b"env\0")
            digest.update(var.encode("ascii"))
            digest.update(b"\0")
            if marker is None:
                digest.update(b"unset\0")
            else:
                digest.update(marker.encode("utf-8", "replace"))
                digest.update(b"\0")
        cached, cache_ok = _cached_scope_origins()
        if cache_ok:
            for scope, path in sorted(set(cached)):
                digest.update(b"scope-origin\0")
                digest.update(scope.encode("ascii"))
                digest.update(b"\0")
                digest.update(path.encode("utf-8", "replace"))
                digest.update(b"\0")
        else:
            digest.update(b"scope-cache-off\0")
        for bit in head_bits:
            digest.update(bit.encode("utf-8", "replace"))
            digest.update(b"\0")
        for tag, size, mt, ct in stat_bits:
            digest.update(tag.encode("utf-8", "replace"))
            digest.update(b"\0")
            digest.update(("%d\0%d\0%d\0" % (size, mt, ct)).encode())
        if packed_hash is not None:
            digest.update(b"packed-refs-content\0")
            digest.update(packed_hash.encode("utf-8", "replace"))
            digest.update(b"\0")
        for rel_b, size, mt, ct in rows:
            digest.update(rel_b)
            digest.update(("%d\0%d\0%d\0" % (size, mt, ct)).encode())
        return digest.hexdigest()
    except SourceUnreadable:
        return None
    except OSError:
        return None


def source_snapshot(work: Path) -> dict:
    """Stable content snapshot of the exact source being verified.

    - `head`: exact git HEAD (or "unknown"); kept as a DISTINCT field, never
      as the content identity.
    - `fingerprint`: sha256 over tracked staged/unstaged worktree content
      plus relevant untracked source files. Dirty-tree edits change it even
      when HEAD is identical; ignored/generated/cache files never do.
    - `untracked`: relevant untracked source relpaths (used to stage only
      real source for local work commits, never bulk junk).
    Non-git fixture copies hash the visible tree the same way.

    Raises `SourceUnreadable` when a critical git list/status read or a
    relevant file read fails: a failed read is unknown, never an empty or
    marker-filled identity. A path genuinely absent from the worktree is
    still recorded as DELETED (a deletion is real content, not a read
    failure). The success-path fingerprint algorithm is unchanged.
    """
    import hashlib
    work = Path(work)
    if _git_present(work):
        code, out, _ = run_cmd(["git", "-C", str(work), "rev-parse", "HEAD"],
                               timeout=_GIT_PROBE_TIMEOUT)
        head = out.strip() if code == 0 else ""
        if not head:
            # No readable HEAD: identity is unavailable, never a valid
            # fingerprint paired with an "unknown" head.
            raise SourceUnreadable("head", rc=code if code != 0 else None)
        # Prove the untracked listing below cannot silently miss
        # source hiding in directories git could not open.
        _ensure_untracked_complete(work)
        entries, untracked = [], []
        for rel in _git_z(work, ["ls-files", "-z"], "tracked-list"):
            if _noise_rel(rel):
                continue
            p = work / rel
            try:
                is_dir, is_file = _classify(p, rel)
                if is_dir:
                    code2, out2, _ = run_cmd(
                        ["git", "-C", str(work), "ls-files", "-s", "--", rel],
                        timeout=_GIT_PROBE_TIMEOUT)
                    if code2 != 0:
                        raise SourceUnreadable(f"gitlink {rel}", rc=code2)
                    entries.append(f"{rel}\0gitlink:{out2.strip()[:100]}")
                elif is_file:
                    try:
                        entries.append(f"{rel}\0{_hash_bytes(p.read_bytes())}")
                    except OSError:
                        raise SourceUnreadable(f"read {rel}")
                else:
                    entries.append(f"{rel}\0DELETED")
            except SourceUnreadable:
                raise
            except OSError:
                raise SourceUnreadable(f"stat {rel}")
        for token in _git_z(work, ["status", "--porcelain=v1", "-z",
                                   "--untracked-files=all"], "untracked-status"):
            # `-z` separates entries with NUL; an untracked entry is `?? <path>`.
            if len(token) < 4 or not token.startswith("??"):
                continue
            rel = token[3:].strip().strip('"')
            if not rel or _noise_rel(rel):
                continue
            if rel.rstrip("/") in _FINGERPRINT_NOISE_DIRS:
                # A bare top-level generated/cache name as the walk root
                # (e.g. an untracked `node_modules/`): never source, even
                # when its stat/read would fail. Anything with a real
                # parent dir is still decided per-file below.
                continue
            p = work / rel
            try:
                is_dir, is_file = _classify(p, rel)
                if is_file:
                    try:
                        entries.append(
                            f"{rel}\0untracked:{_hash_bytes(p.read_bytes())}")
                    except OSError:
                        raise SourceUnreadable(f"read {rel}")
                    untracked.append(rel)
                elif is_dir:
                    # `--untracked-files=all` normally expands dirs already;
                    # walk defensively, still honouring ignores. The
                    # strict walk raises on unreadable dirs instead of
                    # silently skipping them; noise/ignored subtrees are
                    # pruned during descent (their files would be
                    # skipped below anyway).
                    listed = _list_tree_strict(
                        work, rel,
                        lambda r: _noise_covered_dir(r)
                        or _is_ignored(work, r))
                    for srel, sub in listed:
                        if _noise_rel(srel):
                            continue
                        sub_is_dir, sub_is_file = _classify(sub, srel)
                        if not sub_is_file:
                            continue
                        if _is_ignored(work, srel):
                            continue
                        try:
                            entries.append(
                                f"{srel}\0untracked:"
                                f"{_hash_bytes(sub.read_bytes())}")
                        except OSError:
                            raise SourceUnreadable(f"read {srel}")
                        untracked.append(srel)
            except SourceUnreadable:
                raise
            except OSError:
                raise SourceUnreadable(f"stat {rel}")
        fp = hashlib.sha256(("\n".join(sorted(entries))).encode("utf-8")).hexdigest()
        return {"kind": "git", "head": head, "fingerprint": fp,
                "files": len(entries), "untracked": sorted(untracked)}
    entries = []
    # Strict listing: an unreadable dir raises instead of silently
    # vanishing (rglob skips it). Noise subtrees are pruned during
    # descent; their files would be skipped below anyway.
    listed = _list_tree_strict(work, "", _noise_covered_dir)
    for rel, sub in listed:
        if _noise_rel(rel):
            continue
        _, sub_is_file = _classify(sub, rel)
        if not sub_is_file:
            continue
        try:
            entries.append(f"{rel}\0{_hash_bytes(sub.read_bytes())}")
        except OSError:
            raise SourceUnreadable(f"read {rel}")
    fp = hashlib.sha256(("\n".join(sorted(entries))).encode("utf-8")).hexdigest()
    return {"kind": "copy", "head": "unknown", "fingerprint": fp,
            "files": len(entries), "untracked": []}


def _refresh_source(state: dict, work: Path) -> dict:
    """Record the current content snapshot (base/head stay distinct fields).

    Raises `SourceUnreadable` without touching stored proofs when the
    live source cannot be read: callers stop retryably and keep history.
    """
    snap = source_snapshot(work)
    state["workTreeFingerprint"] = snap["fingerprint"]
    state["sourceKind"] = snap["kind"]
    if (state.get("workCommit") in (None, "unknown") and snap["kind"] == "git"
            and snap.get("head") not in (None, "", "unknown")):
        state["workCommit"] = snap["head"]
    store.save(state)
    return snap


def fresh_fingerprint(state: dict) -> str | None:
    """Current content fingerprint, read-only (never saves, never writes).

    Read paths (API/work, graph, promotion) call this at read time so an
    external worktree edit cannot keep reporting CURRENT proof until the
    next resume. Returns None when the run worktree is missing or
    unreadable — callers must report unknown/unverified, never green.
    Only `git` read commands and file hashing run here; user files are
    never modified.
    """
    snap = fresh_snapshot(state)
    return snap["fingerprint"] if snap else None


def fresh_snapshot(state: dict) -> dict | None:
    """Current source snapshot (head + fingerprint + kind), read-only.

    One `source_snapshot` read covers the whole read path (proof check,
    source display, change lists) so callers never hash the worktree
    twice for one request. Returns None when the run worktree is missing
    or unreadable — callers must report unknown/unverified, never green.
    Never saves, never writes, never modifies user files.
    """
    try:
        work = store.run_dir(state["id"]) / "work"
    except Exception:  # noqa: BLE001
        return None
    try:
        if not work.is_dir():
            return None
        return source_snapshot(work)
    except Exception:  # noqa: BLE001
        return None


def _bind_verification(state: dict, work: Path, step: str) -> None:
    """Bind a passed check/review to the exact content just tested.

    Snapshots AFTER the check so the proof covers the tested bytes, then
    records both the HEAD and the content fingerprint. A later model or
    generated change alters the fingerprint and honestly invalidates this
    proof even when HEAD is unchanged.

    Raises `SourceUnreadable` without recording anything when the live
    source cannot be read: a passed worker output with an unbindable
    proof is a retryable failure, never a false green.
    """
    snap = source_snapshot(work)
    state["workTreeFingerprint"] = snap["fingerprint"]
    state["sourceKind"] = snap["kind"]
    if snap["kind"] == "git" and snap.get("head") not in (None, "", "unknown"):
        state["workCommit"] = snap["head"]
    state.setdefault("verifiedCommits", {})[step] = state["workCommit"]
    state.setdefault("verifiedFingerprints", {})[step] = snap["fingerprint"]
    store.save(state)


# Candidate identity bound: past this many changed paths the recorded
# identity is marked incomplete and can never auto-resolve or
# auto-retry (explicit manual reconciliation instead of truncated proof).
_PATH_CAP = 256


def _candidate_path_set(work: Path, base) -> tuple:
    """Full changed path set: tracked diff vs base + relevant untracked.

    Returns (tracked_names, untracked_names). The untracked half reuses
    the snapshot's relevance filter (noise/ignored excluded), so build
    junk never counts as candidate content.

    Any required list read that fails signals unavailable via
    `SourceUnreadable` — never an empty or partial success. An unknown
    base legitimately falls back to the cached/work diffs, but a failed
    known-base diff or a failed cached/work diff read is unknown, not
    an empty set. Callers degrade to incomplete/unknown candidates and
    stay blocked; nothing auto-commits from a partial set.
    """
    work = Path(work)
    tracked: list = []
    if base not in (None, "", "unknown"):
        code, out, _ = run_cmd(
            ["git", "-C", str(work), "diff", "--name-only", str(base), "--"])
        if code != 0:
            raise SourceUnreadable("candidate-list", rc=code)
        tracked = [ln.strip() for ln in (out or "").splitlines()
                   if ln.strip()]
    if not tracked:
        for args in (["diff", "--cached", "--name-only"],
                     ["diff", "--name-only"]):
            code, out, _ = run_cmd(["git", "-C", str(work)] + args)
            if code != 0:
                raise SourceUnreadable("candidate-list", rc=code)
            for ln in (out or "").splitlines():
                if ln.strip() and ln.strip() not in tracked:
                    tracked.append(ln.strip())
    untracked: list = []
    try:
        snap = source_snapshot(work)
    except SourceUnreadable:
        # A failed source read is unknown, never an empty untracked
        # list: propagate so candidate capture degrades to the
        # incomplete/unknown branch instead of minting a complete
        # identity over a partial path set.
        raise
    except Exception as exc:  # noqa: BLE001 - unknown reads stay unknown
        raise SourceUnreadable(
            f"candidate-untracked ({type(exc).__name__})")
    for rel in (snap or {}).get("untracked") or []:
        if rel and rel not in tracked and rel not in untracked:
            untracked.append(rel)
    return tracked, untracked


def _blocked_candidate_snapshot(state: dict, work: Path) -> dict:
    """Content identity of the failed commit candidate (exact set + bytes).

    Records the run's dep/target/declared/scopes plus the COMPLETE
    changed path set (tracked diff vs base plus relevant untracked
    source) with sha256 per path. A tracked path missing from the
    worktree is recorded as DELETED (a deletion is candidate content
    too — its omission must never read as proof). Bounds are explicit:
    past _PATH_CAP the identity is marked incomplete and can never
    auto-resolve or auto-retry (manual reconciliation instead).
    """
    work = Path(work)
    base = state.get("baseCommit")
    tracked, untracked = _candidate_path_set(work, base)
    names = tracked + [u for u in untracked if u not in tracked]
    complete = len(names) <= _PATH_CAP
    files = {}

    def _identity(extra_complete):
        return {"dep": state.get("dep"), "target": state.get("target"),
                "declared": state.get("declared"),
                "scopes": state.get("scopes"),
                "queue_key": state.get("queue_key"),
                "base": base, "paths": sorted(names[:_PATH_CAP]),
                "untracked": sorted(u for u in untracked if u in files),
                "complete": extra_complete, "files": files}

    for name in names[:_PATH_CAP]:
        try:
            raw = (work / name).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            # Genuinely absent: a deletion is candidate content too.
            if name in tracked:
                files[name] = "DELETED"
            continue
        except OSError:
            # Present but unreadable: these bytes cannot be proven, so
            # the candidate is incomplete and can never auto-resolve or
            # auto-commit — never a partial complete identity.
            return _identity(False)
        files[name] = _hash_bytes(raw)
    return _identity(complete)


def _current_candidate_files(work: Path, candidate: dict) -> dict:
    """Current worktree bytes under the recorded identity's rules."""
    work = Path(work)
    recorded = candidate.get("files") or {}
    untracked = set(candidate.get("untracked") or [])
    current = {}
    for name in recorded:
        try:
            current[name] = _hash_bytes((work / name).read_bytes())
        except (FileNotFoundError, NotADirectoryError):
            if name not in untracked:
                current[name] = "DELETED"
        except OSError:
            # Present but unreadable now: current bytes are unprovable,
            # so comparison must fail closed, never match as DELETED.
            raise SourceUnreadable(f"candidate-read {name}")
    return current


def _record_commit_blocked(state: dict, work: Path, step: str, stage: str,
                           code: int, out: str, err: str,
                           subject: str = "") -> dict:
    """Record an explicit blocked-commit outcome and stop (never a skip).

    Used for commit failures other than hooks (worker commits run
    hook-free) AND for stage/add operational errors on required
    run-owned commits (e.g. `.git/index.lock` contention).
    The worktree is preserved as-is; the block carries the exact
    candidate identity so only the exact candidate proven committed can
    resolve it. Returns the helper result the step handlers switch on.
    """
    work = Path(work)
    err = (err or "").strip()
    out = (out or "").strip()
    if stage == "commit":
        detail = (f"commit blocked (exit {code}): {err[-300:]}"
                  if err else f"commit blocked (exit {code})")
    else:
        why = (err or out).strip()
        detail = (f"stage {stage} blocked (exit {code}): {why[-300:]}"
                  if why else f"stage {stage} blocked (exit {code})")
    state.setdefault("interventions", []).append(
        {"at": utcnow(), "kind": "commit-blocked",
         "detail": f"{step}: {detail} — files preserved in worktree"})
    try:
        candidate = _blocked_candidate_snapshot(state, work)
    except Exception:  # noqa: BLE001 - identity unknown stays blocked
        candidate = {"dep": state.get("dep"), "target": state.get("target"),
                     "declared": state.get("declared"),
                     "scopes": state.get("scopes"),
                     "queue_key": state.get("queue_key"),
                     "base": state.get("baseCommit"), "paths": [],
                     "untracked": [], "complete": False, "files": {}}
    state["commitBlocked"] = {"step": step, "stage": stage, "exit": code,
                              "stdout": out[-500:], "stderr": err[-500:],
                              "subject": subject, "at": utcnow(),
                              "candidate": candidate}
    store.save(state)
    return {"committed": False, "blocked": True, "exit": code,
            "reason": detail}


# M2.8b1: worker commits never run repo hooks (nitro's
# core.hooksPath=.husky/_ would execute dependency code unsandboxed,
# with network, and --fix would mutate the tree). Quality is not
# lost: the message stays commitlint-shaped by construction, and
# fastcheck re-runs typecheck/lint/unit inside the sandbox before
# anything is batched.
_GIT_NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]


def maybe_commit_worktree(state: dict, work: Path, step: str) -> dict:
    """Create a LOCAL work commit of owned worktree changes at a fix/review
    boundary so named feature branches contain the maintained code.

    Strictly local: `git add` + `git commit` only, never push/merge/rebase/
    force or history rewriting. Only runs with a recorded named branch, only
    inside this run's own worktree (never the user's checkout, never
    old/manual checkouts or legacy runs), and only tracked edits plus
    relevant untracked source (never bulk junk like node_modules).
    Hooks stay OFF (`core.hooksPath=/dev/null` on every git call in
    this path): the subject is repo-conforming by construction (run
    id in the body, never ahead of the type), and a commit failure
    is recorded as an explicit blocked-commit outcome — staged
    files preserved, never a silent success and never no-code. The
    helper is idempotent, so the same run can retry the commit alone
    without rerunning the fix model.
    """
    work = Path(work)
    branch = state.get("branch")
    if not branch:
        return {"committed": False,
                "reason": "no named branch (detached/interactive run)"}
    try:
        owned = (work.resolve()
                 == (store.run_dir(state["id"]) / "work").resolve())
    except OSError:
        owned = False
    if not owned:
        return {"committed": False, "reason": "not the run-owned worktree"}
    if not _git_present(work):
        # Raises SourceUnreadable (callers block with provenance) when
        # .git presence itself is unknowable; only a truly missing
        # .git is a non-git fixture.
        return {"committed": False, "reason": "non-git fixture workdir"}
    if not _worktree_changed(work):
        return {"committed": False, "reason": "clean tree"}
    snap = source_snapshot(work)
    code, add_out, add_err = run_cmd(
        ["git"] + _GIT_NO_HOOKS + ["-C", str(work), "add", "-u"])
    if code != 0:
        # A stage failure for a required run-owned commit is an
        # operational block, not an optional skip: record it explicitly
        # (exit/stdout/stderr preserved) and stop, leaving the worktree
        # untouched. Legitimate skips (detached/non-owned/clean/noise)
        # return above without blocked; this path is only reachable with
        # real changes in the run-owned worktree.
        return _record_commit_blocked(
            state, work, step, "add-tracked", code, add_out, add_err)
    if snap.get("untracked"):
        code, add_out, add_err = run_cmd(
            ["git"] + _GIT_NO_HOOKS + ["-C", str(work), "add", "--"]
            + snap["untracked"])
        if code != 0:
            return _record_commit_blocked(
                state, work, step, "add-untracked", code, add_out, add_err)
    code, _, _ = run_cmd(
        ["git"] + _GIT_NO_HOOKS + ["-C", str(work), "diff", "--cached",
                                   "--quiet"])
    if code == 0:
        return {"committed": False,
                "reason": "nothing stageable after filtering noise"}
    subject, body = commit_subject_for(state, step)
    code, _, err = run_cmd(
        ["git"] + _GIT_NO_HOOKS + ["-C", str(work),
                                   "-c", "user.name=openmainty",
                                   "-c", "user.email=openmainty@localhost",
                                   "commit", "-m", subject, "-m", body])
    if code != 0:
        # Explicit blocked-commit outcome (hooks can no longer cause
        # this — only other commit failures, e.g. nothing staged or
        # a git error): the index is left untouched (staged files
        # preserved), the model step keeps its own result, and this
        # is never reported as a successful fix, a verified upgrade,
        # or no-source/no-code. The failed candidate's content
        # identity is recorded so a later unrelated manifest edit can
        # never clear this block — only the exact candidate proven
        # committed resolves it.
        return _record_commit_blocked(
            state, work, step, "commit", code, "", err,
            subject=subject)
    state.pop("commitBlocked", None)
    head = work_commit(work, state)
    state["workCommit"] = head
    state.setdefault("interventions", []).append(
        {"at": utcnow(), "kind": "work-commit",
         "detail": f"{step}: local commit {head[:12]} on {branch} (no push)"})
    store.save(state)
    return {"committed": True, "head": head, "branch": branch}


def _candidate_bytes_match(recorded: dict, current: dict) -> bool:
    """True only when every recorded candidate file matches right now."""
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return False
    files = recorded.get("files") or {}
    if not files:
        return False
    for name, digest in files.items():
        if current.get(name) != digest:
            return False
    return True


def _head_file_hashes(work: Path, names) -> dict:
    """sha256 of committed HEAD bytes for the given repo paths.

    A path absent from HEAD records DELETED under the same rule as the
    worktree side, so a committed deletion proves exactly like
    committed bytes (and a missing path never silently drops out).
    """
    import hashlib as _hashlib
    hashes = {}
    for name in list(names or [])[:_PATH_CAP]:
        code, out, _ = run_cmd(
            ["git", "-C", str(work), "show", f"HEAD:{name}"])
        if code != 0:
            hashes[name] = "DELETED"
            continue
        hashes[name] = _hashlib.sha256(out.encode()).hexdigest()
    return hashes


def _recover_set_matches(work: Path, state: dict, candidate: dict) -> bool:
    """True only when the worktree still holds exactly the candidate.

    Both the full changed path set AND every recorded byte (DELETED
    markers included) must match. An incomplete identity never matches:
    truncated proof stays a manual-reconciliation wait.
    """
    if not candidate.get("complete"):
        return False
    recorded_paths = set(candidate.get("paths") or [])
    if not recorded_paths and not (candidate.get("files") or {}):
        return False
    try:
        tracked, untracked = _candidate_path_set(
            work, state.get("baseCommit"))
        current_paths = set(tracked + [u for u in untracked
                                       if u not in tracked])
        if current_paths != recorded_paths:
            return False
        return _candidate_bytes_match(
            candidate, _current_candidate_files(work, candidate))
    except SourceUnreadable:
        # Current bytes unprovable: no match, never an error, never a
        # commit. Callers keep waiting for manual reconciliation.
        return False


def _candidate_already_committed(state: dict, work: Path) -> bool:
    """True only when the EXACT blocked candidate provably landed on HEAD.

    Requires the worktree to be clean, HEAD advanced past the run base,
    the run still targeting the blocked dep/target, AND every recorded
    candidate file's committed HEAD bytes to hash-match the bytes that
    were present at failure. A discarded candidate followed by an
    unrelated manifest commit (any-delta proof) never clears the block.
    Legacy blocks recorded without candidate identity never clear here:
    they wait for explicit manual reconciliation rather than a
    fabricated success.
    """
    work = Path(work)
    blocked = state.get("commitBlocked") or {}
    candidate = blocked.get("candidate") or {}
    if not candidate.get("complete"):
        return False
    if not (candidate.get("files") or {}):
        return False
    base = state.get("baseCommit")
    if not base or base in ("unknown", ""):
        return False
    if candidate.get("base") not in (None, base):
        return False
    for field in ("dep", "target"):
        if candidate.get(field) is not None and (
                state.get(field) != candidate.get(field)):
            return False
    # Clean means no RELEVANT changes: run-owned private caches
    # (muse.CACHE_DIR_NAME, via _worktree_changed noise rules) must not
    # keep an exact manually-committed candidate blocked. A status
    # failure still refuses proof, as before.
    try:
        if _worktree_changed(work):
            return False
    except Exception:  # noqa: BLE001 - unprovable stays blocked
        return False
    code, head, _ = run_cmd(["git", "-C", str(work), "rev-parse", "HEAD"])
    if code != 0 or not (head or "").strip() or head.strip() == base:
        return False
    try:
        committed = _head_file_hashes(work, candidate.get("files"))
    except Exception:  # noqa: BLE001 - unprovable stays blocked
        return False
    return _candidate_bytes_match(candidate, committed)


def _note_wait_reason(state: dict, reason: str) -> None:
    """Record a source-unreadable wait reason without clearing others.

    Sets waitReason only when empty or already ours (source-unreadable /
    commit-blocked family). A different pending reason — operator
    question, quota, another block — is preserved; the new reason still
    lands in interventions/history.
    """
    cur = state.get("waitReason") or ""
    if not cur or cur.startswith(("source-unreadable", "commit-blocked")):
        state["waitReason"] = reason[:500]


def _recover_commit_blocked(state: dict, work: Path) -> str:
    """Retry ONLY the blocked local commit for the same run (no model calls).

    Returns "committed" after a proven new commit, "resolved" after a
    truthful already-committed resolution, else "blocked". The stale
    block clears only on proof; every failure path leaves it (with
    fresh hook evidence) so finalization and promotion keep refusing.
    A legacy persisted block without candidate identity (pre-identity
    record) never clears here: it waits for explicit manual
    reconciliation before any commit, model, or check runs.
    When the blocked candidate's bytes are no longer in the worktree
    (changed/discarded), recovery refuses to auto-commit the unrelated
    bytes and waits for explicit manual reconciliation instead.
    The originating fix/review step is marked done without rerunning
    its model — the content was already produced; only the commit was
    missing. No new run, no new branch.
    """
    work = Path(work)
    blocked = state.get("commitBlocked") or {}
    step = blocked.get("step") or "fix"
    candidate = blocked.get("candidate") or {}
    if blocked.get("reason") == "source-unreadable" and not (
            candidate.get("paths") or candidate.get("files")):
        # Commit-boundary read failure with no recordable candidate: no
        # staging, no committing, no model rerun. The failure-time bytes
        # are unknowable, so a later tree — possibly touched during the
        # outage — must never be auto-committed as the intended
        # candidate. Stay blocked: reconciliation must be explicit
        # (verify the worktree against the recorded step output), and a
        # plain retry alone changes nothing. There is currently no safe
        # in-app recovery for this unproven boundary.
        manual = ("source-unreadable: commit boundary unprovable "
                  f"({blocked.get('op')}); no candidate identity was "
                  "recordable — verify the worktree against the recorded "
                  "step output and reconcile explicitly; retry alone does "
                  "not clear this, and no safe in-app recovery exists "
                  "for this boundary yet")
        _note_wait_reason(state, manual)
        state.setdefault("interventions", []).append(
            {"at": utcnow(), "kind": "commit-blocked",
             "detail": f"{step}: {manual} — files preserved, "
                       "no auto-commit, no model rerun"})
        store.save(state)
        return "blocked"
    if not candidate.get("complete") or not (
            candidate.get("paths") or candidate.get("files")):
        # Legacy/no-identity/incomplete-identity persisted block (pre-identity
        # 25/26 record): there is nothing to prove ownership with, so stop
        # BEFORE any commit, model, or check. The existing block and the
        # worktree stay exactly as they are; the operator reconciles by
        # hand. No invented proof, no history rewrite.
        legacy = ("commit-blocked: legacy persisted block lacks candidate "
                  "identity (pre-identity record); unrelated work "
                  "preserved uncommitted — reconcile by hand, then retry "
                  "the same run")
        if state.get("waitReason") != legacy:
            state["waitReason"] = legacy[:500]
            state.setdefault("interventions", []).append(
                {"at": utcnow(), "kind": "commit-blocked",
                 "detail": f"{step}: {legacy} — files preserved"})
            store.save(state)
        return "blocked"
    if (candidate.get("paths") or candidate.get("files")) and not (
            _recover_set_matches(work, state, candidate)):
        # The blocked candidate is no longer exactly what is in the
        # worktree: paths were added/removed (e.g. an unrelated new file
        # appeared) or bytes changed in place, or the identity itself is
        # incomplete. Same-run auto-recovery must NOT commit whatever
        # happens to be there as if it were the intended recovery. A
        # clean tree still falls through to the exact-HEAD proof below
        # (a hand-committed candidate resolves there); anything dirty
        # waits for explicit manual reconciliation, preserving the tree.
        try:
            tracked, untracked = _candidate_path_set(
                work, state.get("baseCommit"))
        except SourceUnreadable as exc:
            # Current path set unprovable: neither divergence nor
            # sameness can be shown — stay blocked, never commit.
            unreadable = ("source-unreadable: current paths unprovable "
                          f"({exc.op}); work preserved uncommitted — "
                          "reconcile explicitly")
            _note_wait_reason(state, unreadable)
            state.setdefault("interventions", []).append(
                {"at": utcnow(), "kind": "commit-blocked",
                 "detail": f"{step}: {unreadable} — files preserved"})
            store.save(state)
            return "blocked"
        if tracked or untracked:
            diverged = ("commit-blocked: blocked candidate changed or "
                        "discarded since failure; unrelated work preserved "
                        "uncommitted — reconcile by hand, then retry the "
                        "same run")
            if state.get("waitReason") != diverged:
                state["waitReason"] = diverged[:500]
                state.setdefault("interventions", []).append(
                    {"at": utcnow(), "kind": "commit-blocked",
                     "detail": f"{step}: {diverged} — files preserved"})
                store.save(state)
            return "blocked"
    try:
        res = maybe_commit_worktree(state, work, step)
    except SourceUnreadable as exc:
        diverged = ("source-unreadable: live source cannot be read "
                    f"({exc.op}); unrelated work preserved uncommitted — "
                    "reconcile by hand, then retry the same run")
        _note_wait_reason(state, diverged)
        state.setdefault("interventions", []).append(
            {"at": utcnow(), "kind": "commit-blocked",
             "detail": f"{step}: {diverged} — files preserved"})
        store.save(state)
        return "blocked"
    return _apply_commit_retry_result(state, work, step, res)


def _apply_commit_retry_result(state: dict, work: Path, step: str,
                               res: dict) -> str:
    """Shared commit-retry outcome: committed, resolved, or blocked.

    Commit-only recovery never reruns the model: a recovered step is
    marked done from its already-recorded output. waitReason clearing
    covers both the hook ("commit-blocked:") and the read-failure
    ("source-unreadable:") families; anything else is preserved.
    """
    if res.get("committed"):
        entry = (state.get("steps") or {}).get(step) or {}
        if entry.get("status") != "done":
            prev_note = entry.get("note") or ""
            extra = (f"commit-only recovery: local commit {res['head'][:12]} "
                     f"on {res.get('branch')} (no fix-model rerun)")
            store.mark(state, step, "done",
                       note=f"{prev_note} + {extra}" if prev_note else extra)
        if (state.get("waitReason") or "").startswith(
                ("commit-blocked:", "source-unreadable:")):
            state["waitReason"] = None
            store.save(state)
        return "committed"
    if res.get("blocked"):
        # New hook evidence replaces our own read-failure note; a
        # foreign pending reason (operator question, quota) is preserved.
        if not (state.get("waitReason") or "").startswith("commit-blocked:"):
            state["waitReason"] = (
                f"commit-blocked: {step} commit still rejected "
                f"(exit {res.get('exit')}); work preserved uncommitted — "
                "same-run commit-only recovery available")[:500]
            store.save(state)
        return "blocked"
    if _candidate_already_committed(state, work):
        state.pop("commitBlocked", None)
        entry = (state.get("steps") or {}).get(step) or {}
        if entry.get("status") != "done":
            store.mark(state, step, "done",
                       note=((entry.get("note") or "")
                             + " + commit-only recovery: candidate already "
                               "committed on HEAD (no fix-model rerun)"))
        if (state.get("waitReason") or "").startswith(
                ("commit-blocked:", "source-unreadable:")):
            state["waitReason"] = None
            store.save(state)
        return "resolved"
    return "blocked"


def snapshot_diff(work: Path, run_dir: Path, name: str,
                base_commit: str | None = None) -> str | None:
    """Changed-files record anchored at the run base when known.

    Committed work changes vanish from a bare `git diff`, so prefer
    `git diff <base> -- .` (committed + dirty vs the run base); fall back to
    the legacy worktree diff when the base is unknown or not a git workdir.
    """
    if (work / ".git").exists() and base_commit not in (None, "", "unknown"):
        code, out, _ = run_cmd(
            ["git", "-C", str(work), "diff", "--stat", base_commit, "--"])
        if code == 0 and out.strip():
            dest = run_dir / f"{name}.diffstat.txt"
            dest.write_text(out, encoding="utf-8")
            return dest.name
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


def _cancel_reset(state, step):
    """Leave Cancelled steps resumable: pending + recorded intervention."""
    store.mark(state, step, "pending", note="cancelled by operator")
    state.setdefault("interventions", []).append(
        {"at": utcnow(), "kind": "cancelled",
         "detail": f"operator cancel during {step}"})
    store.save(state)


_NOISE_PATHS = ("node_modules", ".pnp.cjs", ".pnp.loader.mjs", ".yarn/",
                ".expo/", "Pods/", muse.CACHE_DIR_NAME + "/")


def _worktree_changed(work: Path) -> bool:
    """True when the fix step left real file changes (noise ignored)."""
    code, out, _ = run_cmd(["git", "-C", str(work), "status", "--porcelain"])
    if code != 0:
        return True
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        # Legacy prefix screen unchanged, plus ONLY the shared watchman
        # cookie predicate (a cookie alone is never a real change). The
        # whole _noise_rel is deliberately not applied here: a TRACKED
        # edit under a noise-named dir (dist/, build/, ...) is still a
        # real change for the `git add -u` work commit below.
        if path and not path.startswith(_NOISE_PATHS) \
                and not _is_watchman_cookie(path):
            return True
    return False


def _answers_text(state: dict) -> str:
    pairs = state.get("answers") or []
    if not pairs:
        return ""
    lines = [f"Q: {p.get('q', '')}\nA: {p.get('a', '')}" for p in pairs]
    return "Operator answers to earlier questions:\n" + "\n".join(lines)


# ---- durable triage disposition: evaluated at EVERY entry point ----
#
# A confirmed duplicate (or a missing-info verdict) is a structured,
# persisted run outcome — never a `waitReason` string that one resume
# path pops and another path ignores. `execute()`, `cmd_resume`, the
# scheduler retry/daemon paths, and the fixture driver all consult
# `resume_gate()` so a duplicate can never be retried into reproduce/
# fix, forked into a new run, or marked all-green, and missing info
# never consumes attempts or backoff. An operator answer clears a
# WAITING disposition (same run identity); a DUPLICATE is terminal.

TERMINAL_DISPOSITIONS = {"DUPLICATE"}
WAITING_DISPOSITIONS = {"INFO-NEEDED", "UNKNOWN"}


def get_disposition(state: dict) -> dict | None:
    """The persisted triage verdict, or None when never recorded."""
    disp = state.get("disposition")
    if isinstance(disp, dict) and disp.get("kind"):
        return disp
    return None


def set_disposition(state: dict, kind: str, detail: str = "",
                    ref: str | None = None) -> dict:
    """Persist a structured triage verdict on the run (same identity)."""
    state["disposition"] = {
        "kind": kind,
        "detail": (detail or "")[:300],
        "ref": ref,
        "at": utcnow(),
    }
    store.save(state)
    return state["disposition"]


def resume_gate(state: dict) -> str | None:
    """Durable resume barrier: "duplicate" | "waiting" | "watch" | None.

    A CURRENT wait text blocks from ANY phase, even under a positive
    triage disposition: a later step may ask a question (or defer to a
    watch) after triage recorded ACTIONABLE, and that open question must
    stop scheduler/CLI/UI resume until a real recorded answer clears it.
    Only a terminal DUPLICATE verdict outranks a stale question text.
    Callers must return without worker calls when this is not None.
    """
    disp = get_disposition(state)
    if disp is not None and disp.get("kind") in TERMINAL_DISPOSITIONS:
        return "duplicate"
    wait = state.get("waitReason") or ""
    if wait.startswith("duplicate:"):
        return "duplicate"
    if wait.startswith("question:"):
        return "waiting"
    if wait.startswith("watch:"):
        return "watch"
    if disp is not None:
        if disp.get("kind") in WAITING_DISPOSITIONS:
            return "waiting"
        if disp.get("kind") in WATCH_DISPOSITIONS:
            return "watch"
        return None
    return None


def enforce_gate(state: dict, gate: str) -> str:
    """Restore the wait text a resume path may have popped and record a
    bounded intervention. Never runs workers; never touches attempts."""
    disp = get_disposition(state) or {}
    if gate == "duplicate":
        want = f"duplicate: {disp.get('ref') or disp.get('detail') or 'confirmed duplicate'}"[:300]
        if not (state.get("waitReason") or "").startswith("duplicate:"):
            state["waitReason"] = want
        _bounded_intervention(state, "duplicate-resume-blocked",
                              (state.get("waitReason") or "")[:300])
    elif gate == "waiting":
        want = (state.get("waitReason") or ""
                or f"question: {disp.get('detail') or 'waiting on operator'}"[:500])
        if not (state.get("waitReason") or "").startswith("question:"):
            state["waitReason"] = want if want.startswith("question:") else f"question: {want}"[:500]
        _bounded_intervention(state, "waiting-resume-blocked",
                              (state.get("waitReason") or "")[:300])
    elif gate == "watch":
        watch = get_watch(state) or {}
        reason = (watch.get("reason") or disp.get("detail")
                  or "deferred until upstream trigger")
        want = f"watch: {reason}"[:500]
        if not (state.get("waitReason") or "").startswith("watch:"):
            state["waitReason"] = want
        _bounded_intervention(state, "watch-resume-blocked",
                              (state.get("waitReason") or "")[:300])
    store.save(state)
    return gate


def _bounded_intervention(state: dict, kind: str, detail: str) -> None:
    """Append an intervention unless the last one already says this."""
    hist = state.setdefault("interventions", [])
    if hist and hist[-1].get("kind") == kind and hist[-1].get("detail") == detail:
        return
    hist.append({"at": utcnow(), "kind": kind, "detail": detail})


# ---- external watch: settled conditional no-change ----
#
# A fix that honors a recorded conditional decision (no migration until an
# upstream trigger) must not be marked failed and retried, blessed as a
# verified upgrade, or re-asked as a human permission question. It settles
# as EXTERNAL-WATCH on the SAME run+key: attempts and history preserved,
# reason/evidence/trigger plus last/next check visible, no review green.
# Scans cheaply re-compare the watched inputs (zero model calls while
# unchanged); a real relevant change makes the same run eligible for
# re-assessment — never a new run, never an automatic unsafe merge.
# Failed or missing upstream evidence never wakes a watch and never
# counts as healthy. A vague 'no change' without the structured block
# below is an ordinary step failure, never a deferred success.

WATCH_DISPOSITIONS = {"EXTERNAL-WATCH"}

WATCH_DECISION_LINE = "WATCH-DECISION: DEFER"


def parse_watch_block(text: str) -> dict | None:
    """Parse an explicit deferred-outcome block from fix worker text.

    Requires all four single-line markers: `WATCH-DECISION: DEFER` plus
    non-empty `WATCH-REASON:` / `WATCH-EVIDENCE:` / `WATCH-TRIGGER:`.
    Anything less explicit (bare 'FILES CHANGED: none', prose about
    deferral, a missing trigger) returns None so normal no-edit failure
    behavior is preserved.
    """
    import re
    lines = (text or "").splitlines()
    if not any(l.strip().upper() == WATCH_DECISION_LINE for l in lines):
        return None
    fields: dict = {}
    for prefix in ("WATCH-REASON:", "WATCH-EVIDENCE:", "WATCH-TRIGGER:"):
        vals = [l[len(prefix):].strip()
                for l in lines if l.strip().upper().startswith(prefix)]
        val = next((v for v in vals if v), "")
        if not val:
            return None
        fields[prefix.lower().replace("watch-", "").rstrip(":")] = val[:500]
    return {"reason": fields["reason"],
            "evidence": fields["evidence"],
            "trigger": fields["trigger"]}


def get_watch(state: dict) -> dict | None:
    """The active external-watch record, or None when not deferred."""
    watch = state.get("watch")
    if isinstance(watch, dict) and watch.get("active"):
        return watch
    return None


def _valid_issue_pin(pin) -> bool:
    """Actual `gh api` issue contract: number/state/updated_at are
    non-empty strings, title is a string (empty allowed), comments is a
    non-negative int (0 is a valid count, bools are not counts)."""
    if not isinstance(pin, dict):
        return False
    try:
        number = pin["number"]
        state = pin["state"]
        updated_at = pin["updated_at"]
        title = pin["title"]
        comments = pin["comments"]
    except KeyError:
        return False
    return (isinstance(number, str) and number != ""
            and isinstance(state, str) and state != ""
            and isinstance(updated_at, str) and updated_at != ""
            and isinstance(title, str)
            and isinstance(comments, int)
            and not isinstance(comments, bool) and comments >= 0)


def _valid_version_pin(want, val) -> bool:
    """Registry/sdk pins are name -> non-empty version string maps."""
    return (isinstance(want, str) and want != ""
            and isinstance(val, str) and val != "")


def _resettle_inputs(prev_inputs, lift_observation) -> tuple:
    """Re-defer pins from the validated lift observation.

    Returns (new_inputs, complete, note, observed_at). When the
    observation completely covers the previously configured keys/classes
    with contract-valid pins, every pin is refreshed to the newly
    observed value so the same release never re-lifts; observed_at is
    the real observation timestamp T0 (never the resettle clock T1).
    Only keys already in the prior policy are carried — extra
    observation keys never become configured policy. A malformed prior
    baseline (non-record, unknown class, contract-breaking pins)
    raises ValueError BEFORE the caller mutates anything, so configured
    policy is never deleted with a warning as justification. A
    missing/invalid/partial observation keeps every prior input with an
    honest note, no fresh check claim, no healthy reset.
    """
    if not isinstance(prev_inputs, dict):
        raise ValueError("resettle refused: prior watch baseline is not "
                         "a record; run unchanged")
    if not prev_inputs:
        return {}, True, None, None
    # Baseline validation by class contract. Registry/sdk classes are
    # name -> version-string maps; the issue class is ONE flat fetcher
    # snapshot {number,title,state,updated_at,comments}, never a map of
    # issue IDs to snapshots.
    for cls, vals in prev_inputs.items():
        if cls in ("registry", "sdk"):
            if not isinstance(vals, dict):
                raise ValueError(f"resettle refused: unsupported prior "
                                 f"baseline class {cls!r}; run unchanged")
            for key, want in vals.items():
                if not isinstance(want, str) or not want:
                    raise ValueError(f"resettle refused: malformed prior "
                                     f"baseline pin {cls}:{key}; run unchanged")
        elif cls == "issue":
            if not _valid_issue_pin(vals):
                raise ValueError("resettle refused: malformed prior "
                                 "baseline issue record; run unchanged")
        else:
            raise ValueError(f"resettle refused: unsupported prior baseline "
                             f"class {cls!r}; run unchanged")
    if not isinstance(lift_observation, dict):
        return (dict(prev_inputs), False,
                "resettle kept prior pins: no lift observation", None)
    obs_at = lift_observation.get("observed_at")
    if not isinstance(obs_at, str) or not obs_at:
        return (dict(prev_inputs), False,
                "resettle kept prior pins: no observation timestamp", None)
    lift_inputs = lift_observation.get("inputs")
    if not isinstance(lift_inputs, dict):
        return (dict(prev_inputs), False,
                "resettle kept prior pins: incomplete lift observation", None)
    fresh = {}
    for cls, vals in prev_inputs.items():
        if cls in ("registry", "sdk"):
            obs_cls = lift_inputs.get(cls)
            if not isinstance(obs_cls, dict):
                return (dict(prev_inputs), False,
                        "resettle kept prior pins: incomplete lift observation",
                        None)
            cls_fresh = {}
            for key in vals.keys():
                if key not in obs_cls or not _valid_version_pin(
                        vals[key], obs_cls[key]):
                    return (dict(prev_inputs), False,
                            "resettle kept prior pins: incomplete lift observation",
                            None)
                cls_fresh[key] = obs_cls[key]
            fresh[cls] = cls_fresh
        else:
            obs_issue = lift_inputs.get("issue")
            if not _valid_issue_pin(obs_issue):
                return (dict(prev_inputs), False,
                        "resettle kept prior pins: incomplete lift observation",
                        None)
            rec = {}
            for field in vals.keys():
                if field not in obs_issue:
                    return (dict(prev_inputs), False,
                            "resettle kept prior pins: incomplete lift observation",
                            None)
                rec[field] = obs_issue[field]
            fresh["issue"] = rec
    return fresh, True, None, obs_at


def settle_external_watch(state: dict, *, reason: str, evidence: str,
                           trigger: str, inputs: dict | None = None,
                           settled_by: str = "fix-worker") -> dict:
    """Settle the run as a deferred external wait (same identity).

    Validates the structured outcome (non-empty reason/evidence/trigger,
    dict-shaped inputs) and persists the disposition, the watch record
    with baseline inputs + last/next check, and the legacy `watch:` wait
    text. Touches no steps, no attempts, no branches: callers decide
    those. Raises ValueError on an invalid outcome instead of settling.

    Explicitly provided inputs are used verbatim (initial and CLI-driven
    settlements keep their intentional semantics). An omitted inputs
    (None) on a state that already holds a watch resettles after a
    worker reassessment: the previously configured keys/classes are kept
    and their pins refreshed from the validated lift observation, so
    monitoring continues instead of forgetting the policy; a
    missing/invalid/partial observation keeps the prior pins with an
    honest check note. An omitted inputs with no previous watch is an
    ordinary fresh settlement (empty baseline, no resettle record).
    """
    reason, evidence, trigger = ((reason or "").strip(), (evidence or "").strip(),
                                 (trigger or "").strip())
    if not reason or not evidence or not trigger:
        raise ValueError("external watch needs non-empty reason/evidence/trigger")
    if inputs is not None and not isinstance(inputs, dict):
        raise ValueError("external watch inputs must be a dict")
    now = utcnow()
    resettled = False
    resettle_note = None
    obs_at = None
    if inputs is None:
        prev_watch = state.get("watch")
        if isinstance(prev_watch, dict):
            resettled = True
            prev_inputs = prev_watch.get("inputs")
            prev_check = prev_watch.get("last_check")
            prev_error = prev_watch.get("check_error")
            lift_obs = prev_watch.get("lift_observation")
            inputs, complete, resettle_note, obs_at = _resettle_inputs(
                prev_inputs, lift_obs)
        else:
            inputs = {}
            complete = True
    else:
        prev_check = None
        prev_error = None
        complete = True
    if resettled and complete and obs_at:
        last_check = obs_at
    elif resettled and not complete:
        last_check = prev_check
    else:
        last_check = now
    watch = {
        "active": True,
        "reason": reason[:500],
        "evidence": evidence[:800],
        "trigger": trigger[:500],
        "inputs": dict(inputs or {}),
        "settled_at": now,
        "settled_by": settled_by,
        "last_check": last_check,
        "next_check": now,
        "check_error": None if complete else (prev_error or resettle_note),
        "lifted_at": None,
    }
    state["watch"] = watch
    state["disposition"] = {
        "kind": "EXTERNAL-WATCH",
        "detail": reason[:300],
        "ref": state.get("queue_key"),
        "at": now,
    }
    state["waitReason"] = f"watch: {reason}"[:500]
    state.setdefault("interventions", []).append(
        {"at": now, "kind": "external-watch-settled",
         "detail": f"{reason[:200]} | trigger: {trigger[:200]}"})
    if resettled:
        kept = {cls: sorted(vals.keys()) for cls, vals in inputs.items()
                if isinstance(vals, dict)}
        state.setdefault("interventions", []).append(
            {"at": now, "kind": "external-watch-resettled",
             "run_id": state.get("id"),
             "complete": complete,
             "kept": kept,
             "detail": ("resettled onto lift-observation pins"
                        if complete else resettle_note or
                        "resettled with prior pins")[:300]})
    store.save(state)
    return watch


def settle_fix_watch_if_declared(state: dict, work, readable: str) -> bool:
    """Settle a no-change fix as external-watch when explicitly declared.

    Only applies when the fix worker exited 0, left the tree unchanged,
    and ended its human terminal text with a valid WATCH block. Anything
    else (nonzero exit, real edits, vague no-change prose) returns False
    so normal failure behavior is preserved. On success the fix step is
    marked done with a watch note — a settled decision, not a failure —
    and the caller must stop before review/commit: the watch gate blocks
    every later entry point.
    """
    fix = (state.get("steps") or {}).get("fix", {})
    if fix.get("exit_code") != 0:
        return False
    try:
        if _worktree_changed(Path(work)):
            return False
    except Exception:  # noqa: BLE001 - unprovable change is not a settlement
        return False
    block = parse_watch_block(readable or "")
    if block is None:
        return False
    settle_external_watch(state, reason=block["reason"],
                           evidence=block["evidence"],
                           trigger=block["trigger"],
                           settled_by="fix-worker")
    store.mark(state, "fix", "done", exit_code=fix.get("exit_code"),
               evidence=fix.get("evidence", []), usage=fix.get("usage", {}),
               session_id=fix.get("session_id"),
               note=f"no source change: settled as external-watch "
                    f"({block['reason'][:120]})")
    return True


def run_context_for(state: dict, profile: dict) -> dict:
    """Immutable actual-run identity for worker prompts (context plumbing).

    Lane/branch/base ref+commit come from the recorded run, never from the
    profile default; task kind/target/scopes keep a dependency-update run's
    maintenance intent through every stage.
    """
    dep, target = state.get("dep"), state.get("target")
    scopes = state.get("scopes")
    return {
        "lane": state.get("lane") or "",
        "branch": state.get("branch") or "",
        "base_ref": state.get("base_branch") or profile.get("baseBranch", "main"),
        "base_commit": state.get("baseCommit") or "",
        "task_kind": ("dep-update" if (dep or target) else "issue"),
        "target": target or "",
        "scopes": ", ".join(scopes) if scopes else "",
        "knowledge": ", ".join(profile.get("knowledge", []) or []) or "(none)",
    }


def review_scope_artifact(state: dict, work: Path) -> dict:
    """Persist the ACTUAL change under review: base..head + dirty remainder.

    A committed fix leaves `git diff` empty, so the reviewer gets the
    recorded scope instead: base ref/commit, current head, content
    fingerprint, base..head stat+diff, and the dirty/untracked remainder
    with real paths. Saved as `review.scope.json` + `review.diff.txt` and
    returned (also recorded on the review step as evidence).
    """
    from .util import run_cmd as _run
    rdir = store.run_dir(state["id"])
    work = Path(work)
    base = state.get("baseCommit") or ""
    scope = {
        "base_commit": base,
        "base_ref": state.get("base_branch") or "",
        "branch": state.get("branch") or "",
        "lane": state.get("lane") or "",
        "head": work_commit(work, state),
        "fingerprint": state.get("workTreeFingerprint") or "",
        "bare_git_diff_empty": None,
        "base_stat": "",
        "dirty_status": "",
        "diff_capped": False,
    }
    code0, out0, _ = _run(["git", "-C", str(work), "diff", "--stat"])
    scope["bare_git_diff_empty"] = (code0 == 0 and not out0.strip())
    if (work / ".git").exists() and base not in (None, "", "unknown"):
        code, out, _ = _run(["git", "-C", str(work), "diff", "--stat",
                             f"{base}..HEAD", "--"])
        if code == 0:
            scope["base_stat"] = out[-6000:]
        code, out, _ = _run(["git", "-C", str(work), "diff",
                             f"{base}..HEAD", "--"])
        if code == 0:
            scope["base_diff"] = out[-30000:]
            scope["diff_capped"] = len(out) > 30000
        else:
            scope["base_diff"] = ""
    else:
        scope["base_diff"] = ""
    code, out, _ = _run(["git", "-C", str(work), "status", "--porcelain"])
    scope["dirty_status"] = out[-6000:] if code == 0 else ""
    code, out, _ = _run(["git", "-C", str(work), "diff"])
    scope["dirty_diff"] = out[-20000:] if code == 0 else ""
    try:
        (rdir / "review.scope.json").write_text(
            __import__("json").dumps(scope, indent=2, ensure_ascii=False),
            encoding="utf-8")
        (rdir / "review.diff.txt").write_text(
            (scope.get("base_diff") or "")
            + "\n--- dirty remainder ---\n"
            + (scope.get("dirty_diff") or ""),
            encoding="utf-8")
    except OSError:
        pass
    state.setdefault("artifacts", {})["review_scope"] = "review.scope.json"
    state.setdefault("artifacts", {})["review_diff"] = "review.diff.txt"
    store.save(state)
    return scope


def review_evidence_text(scope: dict) -> str:
    lines = [
        f"scope: base {scope.get('base_ref') or '?'} @ "
        f"{(scope.get('base_commit') or '?')[:12]} .. head "
        f"{(scope.get('head') or '?')[:12]} on {scope.get('branch') or '(detached)'} "
        f"(lane {scope.get('lane') or '?'}, fingerprint "
        f"{(scope.get('fingerprint') or '?')[:12]})",
        f"bare `git diff` empty: {scope.get('bare_git_diff_empty')}",
        "base..HEAD stat:",
        scope.get("base_stat") or "(empty)",
        "base..HEAD diff (capped):",
        scope.get("base_diff") or "(empty)",
        "dirty/untracked remainder (`git status --porcelain`):",
        scope.get("dirty_status") or "(clean)",
    ]
    return "\n".join(lines)[-20000:]


def run_worker_step(state, profile, step, executor, work, body, context=""):
    rdir = store.run_dir(state["id"])
    active_model = getattr(executor, "model", None) or muse.DEFAULT_MODEL
    store.mark(state, step, "running",
               command=muse.build_cmd(f"[{step}]", workdir=work,
                                      contributor=True, model=active_model)[:-1]
               + [f"[{step}]"])
    try:
        qa = _answers_text(state)
        full_context = f"{context}\n{qa}" if (context and qa) else (qa or context)
        prompt = planner.prompt_for(step, profile, state["issue"], state["title"], body, full_context,
                                    run_ctx=run_context_for(state, profile))
        live_path = rdir / f"{step}.live.log.txt"
        live_path.write_text("", encoding="utf-8")
        buf = {"text": ""}

        def _on_delta(chunk):
            if not chunk or chunk == buf["text"]:
                return
            if chunk.startswith(buf["text"]) and len(chunk) > len(buf["text"]):
                new, buf["text"] = chunk[len(buf["text"]):], chunk
            else:
                new, buf["text"] = chunk, buf["text"] + chunk
            with live_path.open("a", encoding="utf-8") as fh:
                fh.write(new)

        code, out, usage = executor.muse_step_stream(step, prompt, work, _on_delta)
        if not (out or "").strip():
            try:
                out = live_path.read_text(encoding="utf-8")
            except OSError:
                pass
    except QuotaHit as exc:
        store.mark(state, step, "pending", note="quota wait")
        state["waitReason"] = f"subscription-quota at step {step}"
        state.setdefault("interventions", []).append(
            {"at": utcnow(), "kind": "quota-wait", "detail": str(exc)[-300:]})
        store.save(state)
        raise
    except Cancelled:
        _cancel_reset(state, step)
        raise
    # Real worker output is exec --json: scan the human-readable text, where
    # markers live, not the raw envelopes.
    readable = human_text(out)
    ask = extract_ask(readable)
    if ask:
        ev = _record_text(rdir, step, out)
        try:
            (rdir / f"{step}.live.log.txt").unlink()
        except OSError:
            pass
        store.mark(state, step, "blocked", exit_code=code, evidence=[ev],
                   usage=usage, note=f"asked: {ask[:300]}")
        state["waitReason"] = f"question: {ask}"
        state["question_step"] = step
        state.setdefault("interventions", []).append(
            {"at": utcnow(), "kind": "question", "detail": ask[:300]})
        store.save(state)
        return False
    ok = code == 0
    # A clean process exit is NOT success: the step output must show real work.
    success_markers = {"triage": ["DUPLICATE", "ACTIONABLE", "INFO-NEEDED"],
                       "analyze": ["package", "verif", "impact"],
                       "fix": ["CHANGED", "FIX", "TEST"]}
    is_live = isinstance(executor, Executor) and executor.kind == "muse"
    if ok and step in success_markers and is_live:
        lowered = readable.lower()
        if not any(m.lower() in lowered for m in success_markers[step]):
            ok = False
            out = out + "\n[openmainty] step output lacked expected markers; marking failed."
    fail_note = None
    if ok and step == "fix" and is_live and not _worktree_changed(work):
        ok = False
        fail_note = "fix step changed no files"
        out = out + "\n[openmainty] fix step changed no files; marking failed."
    ev = _record_text(rdir, step, out)
    try:
        (rdir / f"{step}.live.log.txt").unlink()
    except OSError:
        pass
    entry_extra = {"exit_code": code, "evidence": [ev],
                   "usage": usage, "session_id": (usage or {}).get("session_id"),
                   "note": fail_note or ("muse worker exit 0 with expected markers"
                                         if ok else f"worker failed (exit {code})")}
    if isinstance(executor, FakeExecutor) and not fail_note:
        entry_extra["note"] = f"fake executor exit {code}"
    store.mark(state, step, "done" if ok else "failed", **entry_extra)
    if step in store.VERIFY_STEPS and ok:
        try:
            _bind_verification(state, work, step)
        except SourceUnreadable as exc:
            # Worker output passed but the proof cannot bind: the bytes
            # are unknown, so this is a retryable failure with prior
            # proofs preserved — never a done step with fresh proof.
            store.mark(state, step, "failed", exit_code=code,
                       evidence=entry_extra.get("evidence"),
                       usage=entry_extra.get("usage"),
                       session_id=entry_extra.get("session_id"),
                       note=(f"worker exit 0 but proof unbindable: source "
                             f"unreadable ({exc.op}) — retryable, prior "
                             f"proofs preserved"))
            state["waitReason"] = (
                f"source-unreadable at {step} bind ({exc.op}): retry the "
                f"same run — stored proofs preserved")[:500]
            store.save(state)
            return False
    if step == "fix" and ok:
        state["workSeq"] = state.get("workSeq", 0) + 1
        # Named scheduler worktrees keep the maintained code as real local
        # commits on the feature branch; the proof below covers the exact
        # resulting content either way (commit first, snapshot after).
        try:
            commit = maybe_commit_worktree(state, work, "fix")
        except SourceUnreadable as exc:
            # The fix output is recorded; only the local commit is
            # missing. Park commitBlocked provenance WITHOUT candidate
            # identity (nothing was readable to record) so the next
            # same-run resume retries ONLY the commit — never the fix
            # model over already-authored WIP. Foreign waitReasons
            # (operator questions, quota) are preserved, not cleared.
            store.mark(state, step, "blocked",
                       note=(f"fix ready but source unreadable ({exc.op}): "
                             "work preserved uncommitted; manual "
                             "reconciliation required, no model rerun"))
            state["commitBlocked"] = {"step": "fix",
                                      "reason": "source-unreadable",
                                      "op": exc.op, "candidate": {},
                                      "at": utcnow()}
            _note_wait_reason(
                state, f"source-unreadable at fix commit ({exc.op}): "
                "output recorded — reconcile explicitly against the "
                "recorded output; retry alone does not clear this")
            state.setdefault("interventions", []).append(
                {"at": utcnow(), "kind": "commit-blocked",
                 "detail": f"fix: source-unreadable ({exc.op}) — files "
                           "preserved, no auto-commit, no model rerun"})
            store.save(state)
            return False
        if commit.get("committed"):
            prev_note = state["steps"][step].get("note") or ""
            extra = (f"local commit {commit['head'][:12]} "
                     f"on {commit['branch']}")
            store.mark(state, step, "done",
                       note=f"{prev_note} + {extra}" if prev_note else extra)
        elif commit.get("blocked"):
            # The local commit was rejected by the repo hook: the fix
            # content stands in the worktree/index, but nothing is
            # committed. This is an explicit bounded wait — never a done
            # fix, never verified, never no-code. Execution stops here
            # (before review/checks) and the same run can later retry
            # ONLY the commit, without rerunning the fix model.
            prev_note = state["steps"][step].get("note") or ""
            extra = (f"{commit['reason']} — work preserved uncommitted; "
                     "same-run commit-only recovery available")
            store.mark(state, step, "blocked",
                       note=f"{prev_note} + {extra}" if prev_note else extra)
            state["waitReason"] = (
                f"commit-blocked: fix commit rejected "
                f"(exit {commit.get('exit')}); work preserved uncommitted — "
                "same-run commit-only recovery available")[:500]
            try:
                snap = source_snapshot(work)
            except SourceUnreadable:
                snap = None
            if snap is not None:
                state["workTreeFingerprint"] = snap["fingerprint"]
                state["sourceKind"] = snap["kind"]
            store.save(state)
            return False
        try:
            snap = source_snapshot(work)
        except SourceUnreadable as exc:
            state["waitReason"] = (
                f"source-unreadable at fix refresh ({exc.op}): retry the "
                f"same run — stored proofs preserved")[:500]
            store.save(state)
            return False
        state["workTreeFingerprint"] = snap["fingerprint"]
        state["sourceKind"] = snap["kind"]
        state["workCommit"] = (snap["head"] if snap["kind"] == "git"
                               and snap.get("head") not in (None, "", "unknown")
                               else f"copyseq-{state.get('workSeq', 0)}")
        store.save(state)
    return ok


# ---- executor-owned install path ----
#
# Verification runs in the run-owned worktree, but only the sandboxed
# model worker used to install there — and its network/loopback is
# denied, so installs and install-dependent checks failed opaquely.
# The executor shell (daemon/CLI process context) is NOT sandboxed: it
# owns one install attempt inside the isolated worktree before profile
# checks run, and check failures caused by a missing/broken environment
# are labeled as environment failures — never as code verdicts. Global
# Muse settings and billing are untouched; installer choice follows the
# worktree's own lockfiles.

def detect_install_cmd(work) -> list | None:
    """Install command from the worktree's own lockfiles, or None."""
    work = Path(work)
    if (work / "package.json").is_file():
        if (work / "yarn.lock").is_file():
            return ["yarn", "install", "--immutable"]
        if (work / "package-lock.json").is_file():
            return ["npm", "ci"]
        if (work / "pnpm-lock.yaml").is_file():
            return ["pnpm", "install", "--frozen-lockfile"]
        return ["npm", "install"]
    if (work / "pubspec.yaml").is_file():
        return ["flutter", "pub", "get"]
    return None


def deps_installed(work) -> bool:
    """True when the worktree already has an installed dependency tree."""
    work = Path(work)
    if (work / "package.json").is_file():
        if (work / ".pnp.cjs").is_file():
            return True
        return (work / "node_modules").is_dir()
    if (work / "pubspec.yaml").is_file():
        return (work / ".dart_tool" / "package_config.json").is_file()
    return True


def ensure_worktree_installed(state, executor, work,
                              install_cmd: list | None = None) -> dict:
    """Install worktree dependencies via the executor shell (unsandboxed).

    Runs at most one install, only when deps_installed() is False, only
    inside the run-owned worktree (never the user's checkout). Records
    `install.log.txt` evidence plus an intervention either way. Returns
    {ran, installed, note}. An install failure is an honest environment
    outcome, never a pass.
    """
    work = Path(work)
    if deps_installed(work):
        return {"ran": False, "installed": True, "note": "dependencies present"}
    cmd = list(install_cmd) if install_cmd else detect_install_cmd(work)
    if not cmd:
        return {"ran": False, "installed": False,
                "note": "no supported manifest for install"}
    rdir = store.run_dir(state["id"])
    try:
        code, out, err = executor.shell(cmd, work)
    except Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - executor failure is env failure
        code, out, err = 127, "", str(exc)[:500]
    (rdir / "install.log.txt").write_text(
        f"$ {' '.join(cmd)}\nexit={code}\n{(out or '')[-6000:]}\n{(err or '')[-2000:]}",
        encoding="utf-8")
    ok = code == 0 and deps_installed(work)
    note = (f"installed via executor: {' '.join(cmd)}" if ok
            else f"environment failure: install exited {code} "
                 f"({(err or out or 'no output').strip()[-160:]}); "
                 f"checks cannot run — not a code verdict")
    state.setdefault("interventions", []).append(
        {"at": utcnow(), "kind": "deps-installed" if ok else "deps-install-failed",
         "detail": note[:300]})
    store.save(state)
    return {"ran": True, "installed": ok, "note": note}


# Substrings proving a check never ran its code: missing install,
# denied network/loopback/sandbox writes. Matched against combined
# check output; genuine assertion/compile errors never contain these.
ENV_FAILURE_HINTS = (
    "node_modules state file",
    "running an install might help",
    "findPackageLocation",
    "ENOTFOUND",
    "EAI_AGAIN",
    "ECONNREFUSED",
    "ENETUNREACH",
    "EPERM",
    "Failed to create server socket",
)


def _env_failure_hint(text: str) -> str | None:
    lowered = (text or "").lower()
    for hint in ENV_FAILURE_HINTS:
        if hint.lower() in lowered:
            return hint
    return None


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
        try:
            code, out, err = executor.shell(check["cmd"], work)
        except Cancelled:
            _cancel_reset(state, step)
            raise
        logs.append(f"$ {' '.join(check['cmd'])}\nexit={code}\n{out[-4000:]}\n{err[-2000:]}")
        if code != 0:
            ev = _record_text(rdir, step, "\n".join(logs))
            hint = _env_failure_hint(f"{out}\n{err}")
            if hint is not None:
                store.mark(state, step, "failed", exit_code=code, evidence=[ev],
                           note=f"environment failure: check '{check['name']}' "
                                f"could not run ({hint}); install/network/sandbox "
                                f"blocked — not a code verdict (exit {code})")
            else:
                store.mark(state, step, "failed", exit_code=code, evidence=[ev],
                           note=f"check '{check['name']}' failed (exit {code})")
            return False
    ev = _record_text(rdir, step, "\n".join(logs) or "(no checks configured)")
    store.mark(state, step, "done", exit_code=0, evidence=[ev],
               note=f"{len(checks)} check(s) passed" if checks else "no checks configured for this repo")
    if step in store.VERIFY_STEPS:
        try:
            _bind_verification(state, work, step)
        except SourceUnreadable as exc:
            store.mark(state, step, "failed", exit_code=0, evidence=[ev],
                       note=(f"checks passed but proof unbindable: source "
                             f"unreadable ({exc.op}) — retryable, prior "
                             "proofs preserved"))
            _note_wait_reason(
                state, f"source-unreadable at {step} bind ({exc.op}): "
                "retry the same run — stored proofs preserved")
            store.save(state)
            return False
    return True


def _has_valid_findings(review_out: str) -> bool:
    """True only when the reviewer names a concrete VALID finding.

    Callers pass the HUMAN terminal text (see human_text), never the raw
    serialized event lines: inside JSONL envelopes no finding sits at a
    line start, so raw matching silently hides real findings. Every
    candidate line is classified on its own: a line-leading `VALID:`
    marker names a finding unless that same line is the explicit
    no-findings verdict (`VALID: none`). An unrelated negative sentence
    elsewhere — quoted instructions, a `Quoted format: VALID: none`
    example, summary prose, `reviewer found no VALID findings` — never
    cancels a concrete `- VALID:` finding on its own line. A bare
    'VALID: none' verdict names no finding and stays negative.
    """
    import re
    positive = re.compile(r"^[\s\-\*\d\.]*VALID\s*[:\-]", re.IGNORECASE)
    negative = re.compile(r"^[\s\-\*\d\.]*VALID\s*:\s*none\b", re.IGNORECASE)
    for line in (review_out or "").splitlines():
        if not positive.search(line):
            continue
        if negative.search(line):
            continue
        return True
    return False


# A reviewer verdict counts as clean ONLY with an explicit recognizable
# no-findings verdict. Any other nonempty text — unknown prose, a refusal,
# an inability-to-inspect note — is NOT clean: without this, arbitrary text
# that merely lacks a `VALID:` line binds a false green. Concrete
# actionable findings always win over a clean marker (callers check
# _has_valid_findings first), so a contradictory review stays actionable.
_CLEAN_POSITIVE = None


def _clean_patterns():
    import re
    global _CLEAN_POSITIVE
    if _CLEAN_POSITIVE is None:
        _CLEAN_POSITIVE = (
            # Requested verdict marker (see planner review prompt).
            re.compile(r"(?im)^[\s\-\*\d\.]*REVIEW-VERDICT\s*:\s*CLEAN\b"),
            # Legacy explicit verdicts, preserved conservatively.
            re.compile(r"(?im)^[\s\-\*\d\.]*VALID\s*:\s*none\b"),
            re.compile(r"(?im)^[\s\-\*\d\.]*no VALID findings\b"),
            re.compile(r"(?im)^[\s\-\*\d\.]*.*\bfound no VALID findings\b"),
        )
    return _CLEAN_POSITIVE


def _has_explicit_clean_verdict(review_out: str) -> bool:
    """True only when the reviewer states an explicit clean verdict.

    Accepts the requested `REVIEW-VERDICT: CLEAN` marker or the legacy
    explicit verdicts (`VALID: none`, `no VALID findings` prose). Empty,
    unknown, refusal, or inability-to-inspect text returns False: it must
    fail honestly, never count as clean.
    """
    text = review_out or ""
    if not text.strip():
        return False
    return any(p.search(text) for p in _clean_patterns())


# Phrases showing the reviewer refused or could not inspect the actual
# scope. Used only to write an honest failure note; behavior is the same
# as any other unrecognized verdict (fail, never green).
_REFUSAL_HINTS = ("cannot inspect", "can't inspect", "unable to inspect",
                  "no verdict", "cannot review", "unable to review",
                  "refuse to bless", "did not receive")


def _looks_like_refusal(review_out: str) -> bool:
    lowered = (review_out or "").lower()
    return any(h in lowered for h in _REFUSAL_HINTS)


# Markers a faithful apply-review report carries per applied finding.
# Mirrors the existing step-marker contracts (FILES CHANGED / DISPOSITION):
# the apply prompt requires `APPLIED:` / `SKIPPED:` lines, so an apply
# that exits 0 without any of them showed no addressed findings.
APPLY_EVIDENCE_MARKERS = ("APPLIED", "FIXED", "ADDRESSED", "CHANGED", "SKIPPED")


def _apply_shows_addressed_findings(apply_out: str) -> bool:
    import re
    lowered = human_text(apply_out)
    return any(re.search(rf"(?im)^[\s\-\*\d\.]*{m}\s*[:\-]", lowered)
               for m in APPLY_EVIDENCE_MARKERS)


def run_review(state, profile, executor, work, body) -> bool:
    """Separate reviewer session + apply valid findings. Three sessions, all recorded.

    The reviewer receives the recorded base..head scope (review.scope.json /
    review.diff.txt), never a bare `git diff` that is empty after the local
    fix commit. After valid findings are applied, earlier verifications are
    honestly invalidated against the new content — and one bounded fresh
    reviewer pass re-checks the FINAL scope before anything binds green.
    A marker (APPLIED:/SKIPPED:) or an unrelated edit is not verification:
    a nonzero/missing re-review or remaining VALID findings fails
    truthfully, exactly once, never a retry loop.
    """
    rdir = store.run_dir(state["id"])
    store.mark(state, "review", "running",
               command=["muse", "exec", "(reviewer session)", "muse", "exec", "(apply session)"])
    scope = review_scope_artifact(state, work)
    evidence = review_evidence_text(scope)
    try:
        code1, review_out, usage1 = executor.muse_step(
            "review", planner.prompt_for("review", profile, state["issue"], state["title"], body,
                                         run_ctx=run_context_for(state, profile),
                                         review_evidence=evidence), work)
    except QuotaHit as exc:
        store.mark(state, "review", "pending", note="quota wait")
        state["waitReason"] = "subscription-quota at step review"
        store.save(state)
        raise
    except Cancelled:
        _cancel_reset(state, "review")
        raise
    _record_text(rdir, "review", review_out, "reviewer.log.txt")
    if code1 != 0:
        store.mark(state, "review", "failed", exit_code=code1,
                       evidence=["review.reviewer.log.txt"], usage=usage1,
                       note=f"reviewer session failed (exit {code1})")
        return False
    # Findings live in the human terminal text, not the serialized event
    # lines: raw JSONL never has a finding at a line start, so scanning it
    # hides real `- VALID:` findings and binds a false green.
    # Concrete findings are checked first so they always win over any
    # unrelated clean-looking prose; a clean bind needs an EXPLICIT
    # no-findings verdict — unknown/refusal/inability-to-inspect text
    # fails honestly instead of counting as clean.
    review_readable = human_text(review_out)
    if _has_valid_findings(review_readable):
        pass  # actionable findings: fall through to the apply session below
    elif _has_explicit_clean_verdict(review_readable):
        store.mark(state, "review", "done", exit_code=0,
                   evidence=["review.reviewer.log.txt", "review.scope.json",
                             "review.diff.txt"], usage=usage1,
                   note="reviewer found no VALID findings")
        try:
            _bind_verification(state, work, "review")
        except SourceUnreadable as exc:
            store.mark(state, "review", "failed", exit_code=code1,
                       evidence=["review.reviewer.log.txt", "review.scope.json",
                                 "review.diff.txt"], usage=usage1,
                       note=(f"clean verdict unbindable: source unreadable "
                             f"({exc.op}) — retryable, prior proofs preserved"))
            _note_wait_reason(
                state, f"source-unreadable at review bind ({exc.op}): "
                "retry the same run — stored proofs preserved")
            store.save(state)
            return False
        return True
    else:
        if not review_readable.strip():
            note = ("reviewer returned no readable verdict; "
                    "findings unverified, not green")
        elif _looks_like_refusal(review_readable):
            note = ("reviewer gave no recognizable clean verdict "
                    "(refused or could not inspect the scope); not green")
        else:
            note = ("reviewer gave no recognizable clean verdict "
                    "(unknown text, no explicit no-findings verdict); not green")
        store.mark(state, "review", "failed", exit_code=code1,
                   evidence=["review.reviewer.log.txt", "review.scope.json",
                             "review.diff.txt"], usage=usage1,
                   note=note)
        return False
    try:
        pre_apply_fp = source_snapshot(work)["fingerprint"]
    except Exception:  # noqa: BLE001 - post-apply compare degrades to text-only
        pre_apply_fp = None
    try:
        code2, apply_out, usage2 = executor.muse_step(
            "apply_review", planner.prompt_for("apply_review", profile, state["issue"],
                                               state["title"], body, context=review_readable), work)
    except Cancelled:
        _cancel_reset(state, "review")
        raise
    _record_text(rdir, "review", apply_out, "apply.log.txt")
    ok = code2 == 0
    if ok:
        # A clean process exit is NOT success: the apply must demonstrably
        # address the findings (source bytes changed) or say so per finding
        # (APPLIED:/SKIPPED: lines). An exit-0 no-op with actionable
        # findings remaining stays failed and never binds review green.
        try:
            post_apply_fp = source_snapshot(work)["fingerprint"]
        except Exception:  # noqa: BLE001 - text evidence decides alone
            post_apply_fp = pre_apply_fp
        changed = (pre_apply_fp is not None and post_apply_fp is not None
                   and pre_apply_fp != post_apply_fp)
        if not changed and not _apply_shows_addressed_findings(apply_out):
            store.mark(state, "review", "failed", exit_code=code2,
                       evidence=["review.reviewer.log.txt", "review.apply.log.txt",
                                 "review.scope.json", "review.diff.txt"],
                       usage={"reviewer": usage1, "apply": usage2},
                       session_id=(usage1 or {}).get("session_id"),
                       note=("apply exited 0 but VALID findings remain unaddressed "
                             "(no source change, no addressed-findings evidence)"))
            return False
    if ok:
        state["workSeq"] = state.get("workSeq", 0) + 1
        # Reviewer edits are real source changes: commit them on named
        # feature branches first, then bind the review to the exact
        # resulting content (earlier checks become honestly stale).
        try:
            commit = maybe_commit_worktree(state, work, "review")
        except SourceUnreadable as exc:
            # Applied output is recorded; only the local commit is
            # missing. Same commit-only recovery contract as fix: park
            # provenance without candidate identity, never rerun the
            # review sessions by default, never bind green.
            store.mark(state, "review", "blocked",
                       exit_code=code2,
                       evidence=["review.reviewer.log.txt",
                                 "review.apply.log.txt",
                                 "review.scope.json", "review.diff.txt"],
                       usage={"reviewer": usage1, "apply": usage2},
                       session_id=(usage1 or {}).get("session_id"),
                       note=(f"VALID findings applied but source unreadable "
                             f"({exc.op}): work preserved uncommitted; "
                             "manual reconciliation required, "
                             "no model rerun"))
            state["commitBlocked"] = {"step": "review",
                                      "reason": "source-unreadable",
                                      "op": exc.op, "candidate": {},
                                      "at": utcnow()}
            _note_wait_reason(
                state, f"source-unreadable at review commit ({exc.op}): "
                "applied output recorded — reconcile explicitly against "
                "the recorded output; retry alone does not clear this")
            state.setdefault("interventions", []).append(
                {"at": utcnow(), "kind": "commit-blocked",
                 "detail": f"review: source-unreadable ({exc.op}) — files "
                           "preserved, no auto-commit, no model rerun"})
            store.save(state)
            return False
        note = "VALID findings applied"
        if commit.get("committed"):
            note += (f" (local commit {commit['head'][:12]} "
                     f"on {commit['branch']})")
        elif commit.get("blocked"):
            # The applied findings are real source changes the hook
            # refused to commit: nothing below may bind green on
            # uncommitted bytes. Fail explicitly; the same run can
            # later retry ONLY the commit, then a fresh HEAD-bound
            # re-review runs as required.
            fail_note = (f"{note} ({commit['reason']} — work preserved "
                         "uncommitted; same-run commit-only recovery "
                         "available)")
            store.mark(state, "review", "blocked",
                       exit_code=commit.get("exit"),
                       evidence=["review.reviewer.log.txt",
                                 "review.apply.log.txt",
                                 "review.scope.json", "review.diff.txt"],
                       usage={"reviewer": usage1, "apply": usage2},
                       session_id=(usage1 or {}).get("session_id"),
                       note=fail_note)
            state["waitReason"] = (
                "commit-blocked: review commit rejected "
                f"(exit {commit.get('exit')}); work preserved uncommitted — "
                "same-run commit-only recovery available")[:500]
            store.save(state)
            return False
        # The applied edit changes the bytes under every earlier proof:
        # invalidate them against the new content instead of silently
        # blessing modified source with former checks.
        try:
            fresh = source_snapshot(work)["fingerprint"]
            state["workTreeFingerprint"] = fresh
            store.save(state)
            reset = store.invalidate_stale_verifications(
                state, current_fingerprint=fresh)
            if reset:
                note += f" (re-verify: {', '.join(reset)} stale)"
        except Exception:  # noqa: BLE001 - re-bind below still records truth
            pass
        # A marker or an unrelated edit is not verification: one bounded
        # fresh reviewer pass runs against the FINAL scope (recomputed
        # after the apply + commit above). Only a clean re-review binds
        # green. A nonzero/missing re-review, or remaining VALID findings
        # (including a justified no-change dismissal the re-reviewer still
        # flags), fails truthfully — exactly once, never a retry loop.
        verify_evidence = review_evidence_text(review_scope_artifact(state, work))
        try:
            code3, verify_out, usage3 = executor.muse_step(
                "review_verify", planner.prompt_for(
                    "review", profile, state["issue"], state["title"], body,
                    run_ctx=run_context_for(state, profile),
                    review_evidence=verify_evidence), work)
        except QuotaHit:
            store.mark(state, "review", "pending", note="quota wait at re-review")
            state["waitReason"] = "subscription-quota at step review"
            store.save(state)
            raise
        except Cancelled:
            _cancel_reset(state, "review")
            raise
        _record_text(rdir, "review", verify_out, "verify.log.txt")
        usage_all = {"reviewer": usage1, "apply": usage2, "verify": usage3}
        verify_readable = human_text(verify_out)
        verify_evidence_files = ["review.reviewer.log.txt", "review.apply.log.txt",
                                "review.verify.log.txt", "review.scope.json",
                                "review.diff.txt"]
        if code3 != 0:
            store.mark(state, "review", "failed", exit_code=code3,
                       evidence=verify_evidence_files, usage=usage_all,
                       session_id=(usage1 or {}).get("session_id"),
                       note=("fresh re-review after apply failed "
                             f"(exit {code3}); findings unverified, not green"))
            return False
        if not verify_readable.strip():
            store.mark(state, "review", "failed", exit_code=code3,
                       evidence=verify_evidence_files, usage=usage_all,
                       session_id=(usage1 or {}).get("session_id"),
                       note=("fresh re-review after apply returned no readable "
                             "verdict; findings unverified, not green"))
            return False
        if _has_valid_findings(verify_readable):
            store.mark(state, "review", "failed", exit_code=code3,
                       evidence=verify_evidence_files, usage=usage_all,
                       session_id=(usage1 or {}).get("session_id"),
                       note=("VALID findings remain after apply (fresh re-review "
                             "still reports VALID findings); not green"))
            return False
        # No marker and no unrelated edit can prove resolution: only an
        # explicit clean verdict on the FINAL scope binds green. Unknown,
        # refusal, or inability-to-inspect text fails honestly.
        if not _has_explicit_clean_verdict(verify_readable):
            if _looks_like_refusal(verify_readable):
                verify_note = ("fresh re-review gave no recognizable clean "
                               "verdict (refused or could not inspect the "
                               "final scope); findings unverified, not green")
            else:
                verify_note = ("fresh re-review gave no recognizable clean "
                               "verdict (unknown text, no explicit "
                               "no-findings verdict); findings unverified, "
                               "not green")
            store.mark(state, "review", "failed", exit_code=code3,
                       evidence=verify_evidence_files, usage=usage_all,
                       session_id=(usage1 or {}).get("session_id"),
                       note=verify_note)
            return False
        note += "; fresh re-review clean"
    store.mark(state, "review", "done" if ok else "failed", exit_code=code2,
               evidence=["review.reviewer.log.txt", "review.apply.log.txt",
                         "review.verify.log.txt", "review.scope.json",
                         "review.diff.txt"],
               usage=usage_all if ok else {"reviewer": usage1, "apply": usage2},
               session_id=(usage1 or {}).get("session_id"),
               command=["muse", "exec", "(reviewer session)", "muse", "exec",
                        "(apply session)", "muse", "exec", "(verify session)"],
               note=note if ok else f"apply failed (exit {code2})")
    if ok:
        try:
            _bind_verification(state, work, "review")
        except SourceUnreadable as exc:
            store.mark(state, "review", "failed", exit_code=code2,
                       evidence=["review.reviewer.log.txt",
                                 "review.apply.log.txt",
                                 "review.verify.log.txt", "review.scope.json",
                                 "review.diff.txt"],
                       usage=usage_all,
                       session_id=(usage1 or {}).get("session_id"),
                       note=(f"re-review clean but proof unbindable: source "
                             f"unreadable ({exc.op}) — retryable, prior "
                             "proofs preserved"))
            _note_wait_reason(
                state, f"source-unreadable at review bind ({exc.op}): "
                "retry the same run — stored proofs preserved")
            store.save(state)
            return False
    return ok


def _archive_draft(state, rdir, key: str) -> None:
    """Preserve a superseded draft as a historical artifact, never delete."""
    from pathlib import Path as _Path

    name = (state.get("artifacts") or {}).get(key)
    if not name:
        return
    src = _Path(rdir) / name
    try:
        if not src.is_file():
            return
        stamp = utcnow().replace(":", "").replace("-", "").replace("+", "")[:15]
        dest = _Path(rdir) / f"{key}.historical-{stamp}.md"
        dest.write_bytes(src.read_bytes())
        state.setdefault("artifacts", {})[f"{key}_historical"] = dest.name
    except OSError:
        pass


def _draft_context(state: dict, work) -> str:
    """Current-source draft context, never stale fix.log alone.

    A source-only forward correction does not rerun fix, so the stored
    fix.log can still name a removed test. Drafts must rebuild from the
    CURRENT base..HEAD stat, current head/fingerprint, and current
    check/review receipts instead.
    """
    parts = []
    try:
        base = state.get("baseCommit") or ""
        code, out, _ = run_cmd(
            ["git", "-C", str(work), "diff", "--stat", base, "--"]
            if base not in (None, "", "unknown")
            else ["git", "-C", str(work), "diff", "--stat"])
        if code == 0 and out.strip():
            parts.append("current diff (base..HEAD):\n" + out.strip())
    except Exception:  # noqa: BLE001 - context is best-effort
        pass
    head = state.get("workCommit") or ""
    fp = state.get("workTreeFingerprint") or ""
    parts.append(f"current head: {head[:12] if head else '?'} "
                 f"fingerprint: {fp[:12] if fp else '?'}")
    for name in ("review", "e2e"):
        entry = (state.get("steps") or {}).get(name, {})
        note = str(entry.get("note") or "")
        if note:
            parts.append(f"{name} receipt: {note[:300]}")
    # The superseded fix.log stays on disk plus the archived
    # *.historical-*.md drafts; it is deliberately NOT embedded in the
    # live draft, so a removed test can never leak back in.
    return "\n\n".join(parts)


def _invalidate_stale_drafts(state: dict, work, reset: list) -> None:
    """Reset derivative drafts when verification was invalidated.

    Release/announce drafts describe the verified source. When the
    source changed under them (review/e2e went stale), a done draft
    would otherwise keep the old head and the old removed-test claim.
    Reset them to pending with a cleared clock, archive the superseded
    files, so resume regenerates from the current source.
    """
    if not reset:
        return
    rdir = store.run_dir(state["id"])
    changed = False
    for step in ("release", "announce"):
        entry = (state.get("steps") or {}).get(step)
        if not entry or entry.get("status") != "done":
            continue
        for key in ("pr", "release_notes", "medium", "discord", "slack"):
            _archive_draft(state, rdir, key)
        entry.update(status="pending", started_at=None, ended_at=None,
                     exit_code=None,
                     note=("invalidated: source changed after verification; "
                           f"drafts archived ({', '.join(reset)} stale)"))
        changed = True
    if changed:
        state.setdefault("interventions", []).append(
            {"at": utcnow(), "kind": "drafts-invalidated",
             "detail": f"release/announce reset; verify reset: {', '.join(reset)}"})
        store.save(state)


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
    """Run pending steps in order. Returns True when all selected steps are done.

    Holds the single-worker OS lease for the whole actual execution
    (reentrant: scheduler-owned execution calling back in reuses the held
    lease). Raises `WorkerBusy` without any model call or state mutation
    when another live holder owns the lease.
    """
    from . import sched as _sched
    owner = _sched.acquire_worker_lease()
    if owner is None:
        raise WorkerBusy(
            "another maintenance worker holds the global lock; "
            "no work started (run unchanged)")
    try:
        try:
            return _execute_inner(state, profile, executor, checkout, body, only, write_drafts_on)
        finally:
            try:
                from . import chat as _chat
                _chat.sync_run_session(store.load(state["id"]))
            except Exception:
                pass
    finally:
        _sched.release_worker_lease(owner)


def _execute_inner(state, profile, executor, checkout, body="", only=None, write_drafts_on=True):
    from . import chat as _chat
    base_branch = state.get("base_branch") or profile.get("baseBranch", "main")
    work = ensure_branch_workdir(state, checkout, base_branch,
                                 branch=state.get("branch"))
    note = _isolate_node_modules(work)
    if note:
        state.setdefault("interventions", []).append(
            {"at": utcnow(), "kind": "deps-isolated", "detail": note})
        store.save(state)
    if not state.get("baseCommit") or state["baseCommit"] == "unknown":
        base, _ = git_rev(checkout, base_branch)
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
    # Resume/start: snapshot the actual current content and invalidate any
    # check bound to different bytes — even when HEAD is unchanged.
    try:
        snap = _refresh_source(state, work)
    except SourceUnreadable as exc:
        # No worker call, no proof change: stop retryably with history
        # intact instead of certifying against an unknown tree.
        _note_wait_reason(
            state, f"source-unreadable at resume refresh ({exc.op}): "
            "retry the same run — stored proofs preserved")
        state.setdefault("interventions", []).append(
            {"at": utcnow(), "kind": "source-unreadable",
             "detail": f"resume refresh ({exc.op}) — no proof change"})
        store.save(state)
        return False
    reset = store.invalidate_stale_verifications(
        state, current_fingerprint=snap["fingerprint"])
    _invalidate_stale_drafts(state, work, reset)
    # Durable triage barrier: a confirmed duplicate or an unanswered
    # missing-info verdict stops EVERY resume (same-run, scheduler,
    # daemon, CLI) before any worker call — never retried into code.
    gate = resume_gate(state)
    if gate is not None:
        enforce_gate(state, gate)
        return False
    # Commit-blocked recovery: a hook-rejected local commit from an
    # earlier fix/review step retries ONLY the commit of the existing
    # candidate — never the fix model, never a new run/branch. The
    # stale block clears only on a proven new commit or a truthful
    # already-committed resolution; anything else stays blocked with
    # zero model calls. A successful commit moves HEAD, so source
    # proofs refresh again before the steps loop runs.
    if state.get("commitBlocked"):
        recovered = _recover_commit_blocked(state, work)
        if recovered == "blocked":
            return False
        snap = _refresh_source(state, work)
        reset = store.invalidate_stale_verifications(
            state, current_fingerprint=snap["fingerprint"])
        _invalidate_stale_drafts(state, work, reset)

    steps = [s for s in store.STEP_ORDER if (only is None or s in only)]
    for step in steps:
        entry = state["steps"][step]
        if entry["status"] == "done":
            continue
        if step == "triage":
            if not run_worker_step(state, profile, step, executor, work, body):
                return False
            # Structured disposition: a confirmed duplicate or a missing-info
            # verdict must not autonomously generate code changes or repeated
            # attempts. Same run identity, blocked with a clear next action.
            # UNKNOWN/malformed verdicts are NEVER implicitly actionable.
            kind, detail = planner.parse_triage_disposition(
                _tail(store.run_dir(state["id"]) / "triage.log.txt", n=6000))
            if kind == "DUPLICATE":
                ref = detail or "an existing issue (see triage log)"
                store.mark(state, "triage", "done",
                           note=f"duplicate of {ref}; no code change")
                state["waitReason"] = f"duplicate: {ref}"[:300]
                set_disposition(state, "DUPLICATE",
                                detail=f"duplicate of {ref}; linked references in triage log",
                                ref=ref)
                state.setdefault("interventions", []).append(
                    {"at": utcnow(), "kind": "triage-duplicate",
                     "detail": f"duplicate of {ref}; linked references in triage log"})
                store.save(state)
                return False
            if kind == "INFO-NEEDED":
                need = detail or "more information (see triage log)"
                store.mark(state, "triage", "blocked", exit_code=0,
                           note=f"waiting on operator: {need}"[:300])
                state["waitReason"] = f"question: {need}"[:500]
                set_disposition(state, "INFO-NEEDED", detail=need)
                state.setdefault("interventions", []).append(
                    {"at": utcnow(), "kind": "triage-info-needed",
                     "detail": need[:300]})
                store.save(state)
                return False
            if kind == "UNKNOWN":
                need = detail or "no DISPOSITION line in triage output (see triage log)"
                store.mark(state, "triage", "blocked", exit_code=0,
                           note=f"waiting on operator: triage verdict unclear; {need}"[:300])
                state["waitReason"] = f"question: triage verdict unclear; {need}"[:500]
                set_disposition(state, "UNKNOWN", detail=need)
                state.setdefault("interventions", []).append(
                    {"at": utcnow(), "kind": "triage-unknown",
                     "detail": need[:300]})
                store.save(state)
                return False
            set_disposition(state, "ACTIONABLE", detail=detail)
        elif step == "reproduce":
            # Dependency updates are maintenance, not an upstream bug: the
            # run intent/target/scopes survive and no bug reproduction runs.
            if state.get("dep") or state.get("target"):
                store.mark(state, step, "done", exit_code=0,
                           note="dependency maintenance: no upstream bug to reproduce; "
                                f"target {state.get('dep')}->{state.get('target')}")
                try:
                    _chat.sync_run_session(state)
                except Exception:
                    pass
                continue
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
                # A settled conditional no-change is a first-class deferred
                # outcome, not a failure: stop here (before review/commit)
                # when the fix worker declared a structured watch result.
                # Anything else keeps normal failure behavior.
                if settle_fix_watch_if_declared(
                        state, work,
                        _tail(store.run_dir(state["id"]) / "fix.log.txt",
                              n=6000)):
                    try:
                        _chat.sync_run_session(state)
                    except Exception:
                        pass
                    return False
                return False
            snapshot_diff(work, store.run_dir(state["id"]), "fix",
                          state.get("baseCommit"))
        elif step == "review":
            if not run_review(state, profile, executor, work, body):
                return False
            snapshot_diff(work, store.run_dir(state["id"]), "review",
                          state.get("baseCommit"))
        elif step == "e2e":
            from . import pcconfig
            caps = pcconfig.load().get("capabilities", {})
            checks = list(profile.get("checks", [])) + list(profile.get("e2e", []))
            if not checks:
                store.mark(state, step, "blocked", note="no checks/e2e configured for this repo")
                return False
            # Executor-owned install (unsandboxed daemon/CLI context) before
            # profile checks: a missing worktree install is an environment
            # condition, and the sandboxed worker cannot fix it. A failed
            # install is recorded honestly; the checks below then report
            # environment failure distinctly instead of a code verdict.
            try:
                ensure_worktree_installed(state, executor, work)
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001 - checks report the truth below
                pass
            if not run_shell_checks(state, step, checks, executor, work, caps):
                return False
        elif step in ("release", "announce"):
            store.mark(state, step, "running", command=["openmainty", f"draft:{step}"])
            ctx = _draft_context(state, work)
            if write_drafts_on:
                write_drafts(state, profile, context=ctx)
            store.mark(state, step, "done", exit_code=0,
                       evidence=[state["artifacts"].get("release_notes", ""),
                                 state["artifacts"].get("medium", ""),
                                 state["artifacts"].get("discord", ""),
                                 state["artifacts"].get("slack", "")],
                       note="drafts written (dry-run only; nothing posted)")
        try:
            _chat.sync_run_session(state)
        except Exception:
            pass
    return all(state["steps"][s]["status"] == "done" for s in steps)


ENVELOPE_SMELLS = ("record_type", "payload_type", "task_stream", "run_stream",
                   "schema_version", "task_lifecycle", "run_terminal",
                   "causation_id", "payload_schema_version",
                   # wrapped fragments cut mid-word (e.g. a line starting
                   # `n_stream":...`) carry no full token; catch the tail.
                   "_stream", "command_id")


def human_text(raw: str) -> str:
    """Readable text from raw worker output.

    `muse exec --json` streams harness envelopes (session/task records)
    around the real text. Envelopes are dropped, the terminal text inside
    a run_terminal envelope is kept, and anything else passes through, so
    step context and published drafts never carry harness JSON.
    """
    import json as _json

    kept = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not any(smell in s for smell in ENVELOPE_SMELLS):
            kept.append((line, False))
            continue
        text, is_term = None, False
        if s.startswith("{"):
            try:
                payload = _json.loads(s).get("payload", {})
            except ValueError:
                payload = {}
            if isinstance(payload, dict):
                candidate = payload.get("text")
                if isinstance(candidate, str) and candidate.strip():
                    text = candidate.strip()
                    # Only the run_terminal text is authoritative; text from
                    # other envelopes is streamed partial content that the
                    # terminal wording supersedes when present.
                    is_term = payload.get("kind") == "run_terminal"
        if text:
            kept.append((text, is_term))
    terminals = [line for line, is_term in kept if is_term]
    if terminals:
        # The terminal text is the complete final wording; streamed plain
        # fragments duplicated inside it are dropped.
        blob = " ".join(" ".join(terminals).split())
        deduped = []
        for line, is_term in kept:
            norm = " ".join(line.split())
            if not is_term and norm and norm in blob:
                continue
            deduped.append(line)
        return "\n".join(deduped)
    return "\n".join(line for line, _ in kept)


def _tail(path: Path, n: int = 3000) -> str:
    try:
        text = human_text(path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    if len(text) <= n:
        return text
    _, _, rest = text[-n:].partition("\n")
    return rest if rest else text[-n:]
