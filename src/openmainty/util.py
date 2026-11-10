"""Small shared helpers (stdlib only)."""
from __future__ import annotations

import contextlib
import datetime
import getpass
import json
import os
import shutil
import socket
import subprocess
import tempfile
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


class Cancelled(Exception):
    """Operator cancellation. Raised mid-flight; callers must leave resumable state."""


def _communicate(proc, timeout, cancel_event):
    """Wait for proc; honor timeout and cooperative cancellation.

    Returns (stdout, stderr). Raises Cancelled (proc killed) or
    subprocess.TimeoutExpired (proc killed) — never returns partial output.
    """
    import time as _time
    deadline = None if timeout is None else _time.monotonic() + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            raise Cancelled("cancelled by operator")
        slice_s = 0.2
        if deadline is not None:
            slice_s = min(slice_s, max(0.0, deadline - _time.monotonic()))
            if slice_s <= 0:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            return proc.communicate(timeout=slice_s)
        except subprocess.TimeoutExpired:
            continue


def iter_proc_lines(proc, timeout, cancel_event):
    """Yield stdout lines as they arrive. Kill + raise on cancel/timeout.

    Raises Cancelled or subprocess.TimeoutExpired (proc reaped in both
    cases). The generator's finally never orphans a live proc on unwind.
    """
    import queue as _queue
    import threading as _threading
    import time as _time
    q: _queue.Queue = _queue.Queue()

    def _pump():
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)

    _threading.Thread(target=_pump, daemon=True).start()
    deadline = None if timeout is None else _time.monotonic() + timeout

    def _kill():
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _kill()
                raise Cancelled("cancelled by operator")
            slice_s = 0.2
            if deadline is not None:
                slice_s = min(slice_s, max(0.0, deadline - _time.monotonic()))
                if slice_s <= 0:
                    _kill()
                    raise subprocess.TimeoutExpired(proc.args, timeout)
            try:
                line = q.get(timeout=slice_s)
            except _queue.Empty:
                continue
            if line is None:
                return
            yield line
    finally:
        try:
            if proc.poll() is None and cancel_event is not None and cancel_event.is_set():
                _kill()
        except Exception:
            pass


# M2.21: git subcommands that never write. A read-only probe
# (status, diff, ls-files, ...) must not refresh the index of the
# repo it inspects — linked worktrees keep their index inside the
# owner's .git. Writing commands are deliberately absent.
_GIT_READONLY = frozenset({
    "status", "diff", "ls-files", "rev-parse", "show", "log",
    "rev-list", "merge-base", "check-ignore", "show-ref",
    "cat-file", "for-each-ref", "describe",
})


def _git_subcommand(argv):
    """(subcommand, rest) for a git argv, skipping -C/-c pairs,
    --git-dir/--work-tree pins and bare flags. Returns (None, [])
    when there is no subcommand."""
    args = [str(a) for a in (argv or [])]
    if not args or args[0] != "git":
        return None, []
    i = 1
    while i < len(args):
        tok = args[i]
        if tok in ("-C", "-c", "--git-dir", "--work-tree"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok, args[i + 1:]
    return None, []


def git_env_for_readonly(argv, env=None):
    """Child env for a git probe: GIT_OPTIONAL_LOCKS=0 when the
    subcommand is read-only, else the caller's env unchanged.
    symbolic-ref counts only with a single name (no new value).
    Never raises."""
    try:
        sub, rest = _git_subcommand(argv)
        if sub is None:
            return env
        if sub == "symbolic-ref":
            names = [a for a in rest if not a.startswith("-")]
            if len(names) != 1:
                return env
        elif sub not in _GIT_READONLY:
            return env
        child = dict(env) if env is not None else dict(os.environ)
        child["GIT_OPTIONAL_LOCKS"] = "0"
        return child
    except Exception:
        return env


# M2.21b: subcommands that may refresh the index even with
# GIT_OPTIONAL_LOCKS=0 (diff does not honour optional locks in
# racy-timestamp cases). These run against a PRIVATE COPY of the
# repo's index via GIT_INDEX_FILE, so the real index — which for a
# linked worktree lives inside the owner's .git — is never touched.
# Any other index-reading read-only probe may be added here; writing
# commands must never be.
_GIT_INDEX_REFRESH = frozenset({"diff", "status"})


def _git_workdir(argv, cwd=None):
    """Worktree path for a git argv: the last -C value, else the
    pinned --work-tree value, else cwd. Never rely on parsing -C:
    bound (pinned) argv carries no -C."""
    args = [str(a) for a in (argv or [])]
    found = None
    i = 1 if args and args[0] == "git" else 0
    while i < len(args):
        tok = args[i]
        if tok == "-C" and i + 1 < len(args):
            found = args[i + 1]
            i += 2
            continue
        if tok == "--work-tree" and i + 1 < len(args):
            found = args[i + 1]
            i += 2
            continue
        if tok in ("-C", "-c", "--git-dir"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    if found is not None:
        return found
    return cwd


# 301c: hardened flags for the unpinned index probe, kept as
# a literal duplicate of runner._HARDENED_GIT_CONFIG so util
# stays import-cycle free (runner imports util). A pin test in
# test_301b_private_index asserts the two lists stay equal.
_HARDENED_PROBE_CONFIG = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", "/dev/null"),
    ("core.sshCommand", ""),
    ("core.attributesFile", "/dev/null"),
)


def _pinned_gitdir(argv):
    """The pinned --git-dir of a hardened bound argv, or None.

    Never raises; unrecognized shapes read as unpinned."""
    try:
        args = [str(a) for a in (argv or [])]
        i = 1 if args and args[0] == "git" else 0
        while i < len(args):
            tok = args[i]
            if tok == "--git-dir" and i + 1 < len(args):
                return args[i + 1]
            if tok.startswith("--git-dir="):
                return tok[len("--git-dir="):]
            if tok in ("-C", "-c", "--work-tree"):
                i += 2
                continue
            i += 1
    except Exception:
        pass
    return None


def _private_index_copy(workdir, argv=None, env=None):
    """Per-call temp copy of the index; path or None.

    Pinned argv (a bound call carrying --git-dir): the index is
    <pinned gitdir>/index, taken directly with no spawn and no
    discovery — a rewritten worktree .git pointer cannot divert
    the copy. Missing file reads as None (fallback, as before).

    Unpinned argv: resolves the index with a read-only
    `rev-parse --git-path index` run through the same hardened
    -c set as the call itself (fsmonitor/hooks off), with the
    caller's env (GIT_INDEX_FILE popped so a caller-set copy
    never resolves to itself) — never os.environ. env=None
    keeps the old os.environ-based base plus the hardened flags.

    Either way the bytes go to a product-owned temp file. Any
    failure (unresolvable, missing index, copy error) returns
    None: the caller falls back to today's behaviour plus the
    env rule, never a refusal. Never raises.
    """
    try:
        pinned = _pinned_gitdir(argv)
        if pinned is not None:
            index = Path(pinned) / "index"
            if not index.is_file():
                return None
            return _copy_index_file(index)
        if not workdir:
            return None
        probe = ["git"]
        for key, value in _HARDENED_PROBE_CONFIG:
            probe += ["-c", "%s=%s" % (key, value)]
        probe += ["-C", str(workdir), "rev-parse",
                  "--git-path", "index"]
        base = dict(env) if env is not None else dict(os.environ)
        base.pop("GIT_INDEX_FILE", None)
        base["GIT_OPTIONAL_LOCKS"] = "0"
        out = subprocess.run(probe, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             timeout=30, text=True, env=base)
        if out.returncode != 0:
            return None
        rel = (out.stdout or "").strip().splitlines()
        if not rel:
            return None
        index = Path(rel[0])
        if not index.is_absolute():
            index = Path(str(workdir)) / index
        if not index.is_file():
            return None
        return _copy_index_file(index)
    except Exception:
        return None


def _copy_index_file(index):
    """Copy an index file to a product-owned temp path; path or None."""
    try:
        fd, tmp = tempfile.mkstemp(prefix="om-index-")
        os.close(fd)
        # M2.22b: keep the index file's mtime on the copy.
        # copyfile stamps "now", which breaks git's racy-entry
        # rule (Apple git compares at second granularity): a
        # same-size edit in the same second as the index write is
        # then trusted, and status wrongly reads clean. copystat
        # keeps the rule firing exactly as on the real index.
        shutil.copy2(index, tmp)
        return tmp
    except Exception:
        return None


@contextlib.contextmanager
def git_readonly_child_env(argv, env=None, cwd=None):
    """Yield the child env for a git probe.

    Same env rule as git_env_for_readonly (GIT_OPTIONAL_LOCKS=0
    for read-only subcommands), plus for index-refreshing
    subcommands (diff, status) GIT_INDEX_FILE points at a private
    per-call copy of the repo's index: <pinned gitdir>/index for
    a bound argv (no discovery spawn), else a hardened probe with
    the caller's env. The copy is removed on exit (best effort).
    Fallback without a copy never raises.
    """
    child = git_env_for_readonly(argv, env)
    tmp = None
    try:
        if child is not None and child is not env:
            try:
                sub, _ = _git_subcommand(argv)
            except Exception:
                sub = None
            if sub in _GIT_INDEX_REFRESH:
                tmp = _private_index_copy(
                    _git_workdir(argv, cwd), argv, child)
                if tmp is not None:
                    child = dict(child)
                    child["GIT_INDEX_FILE"] = tmp
        yield child
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def run_cmd(cmd, cwd=None, timeout=600, cancel_event=None,
            env=None, stdin=None):
    """Run a command; return (exit_code, stdout, stderr). Never raises on failure.

    Cancelled (from cancel_event) is not a failure: it propagates so the
    caller can record resumable state instead of a result. `env` (a
    mapping or None for inherit) and `stdin` (e.g. DEVNULL) are
    optional and default to the old behavior, so existing callers are
    untouched.

    M2.21: read-only git probes additionally get
    GIT_OPTIONAL_LOCKS=0 in the child env (the caller's mapping is
    copied, never mutated). Writing git commands are unaffected.
    M2.21b: diff/status additionally run against a private
    per-call copy of the repo's index (GIT_INDEX_FILE), removed
    afterwards; fallback keeps only the env rule. Never raises.
    """
    with git_readonly_child_env(cmd, env, cwd) as child_env:
        try:
            proc = subprocess.Popen(
                list(cmd),
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=stdin,
                env=child_env,
                text=True,
            )
        except FileNotFoundError as exc:
            return 127, "", f"command not found: {exc}"
        try:
            out, err = _communicate(proc, timeout, cancel_event)
        except subprocess.TimeoutExpired:
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:
                out, err = "", ""
            return 124, out or "", (err or "") + f"\ntimed out after {timeout}s"
        return proc.returncode, out, err


def looks_like_secret_name(name: str) -> bool:
    lowered = name.upper()
    return any(k in lowered for k in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
