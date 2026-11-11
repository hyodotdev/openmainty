"""M2 in-app fastcheck: run repo-profile steps in a prepared worktree.

Runs newly published dependency code on the owner's Mac with nobody
watching, so the containment boundary is explicit and every claim is
recorded per step:

- argv-only spawn, never a shell; steps come from the repo profile.
- Allowlist environment: the profile's explicit toolchain dirs plus
  system dirs, LANG (en_US default), CI=1, HOME/TMPDIR pointed at a
  per-op temp dir. SSH, tokens, keys, secrets, passwords and
  NPM_CONFIG_* (except the registry) are dropped and never logged.
- Install steps keep network (they need the registry) but run with
  lifecycle scripts force-disabled; the registry host is recorded.
- Non-install steps run under sandbox-exec with network denied where
  the OS allows it; per step records whether the sandbox applied. A
  missing sandbox-exec skips the step with a recorded reason — never
  runs unsandboxed while claiming otherwise. A `flutter test` step
  may carry "sandbox": "loopback" for the loopback-only profile
  (the tester binds loopback TCP; remote TCP and DNS stay denied).
- Per-step timeouts kill the whole process group (setsid) and verify
  no children survive; escapees (setsid daemons: watchman, Metro,
  Gradle) are found by worktree/HOME argv or cwd, confirmed per-PID
  via lsof, and killed by explicit PID only — never by pattern.
- File writes are denied by default under sandbox-exec; ONLY the
  op worktree, the per-op temp HOME/TMPDIR, the app cache dirs
  (yarn / corepack / pub) and /dev may be written — never the
  worktree's git admin dir. The allowlist is recorded per step
  as `write_allow`.
- One fastcheck at a time (slot flock); the op exclusion lock is held
  for the whole run and the global lease is never taken while
  holding it, so a concurrent prepare-retry/reresolve on the same op
  gets OpBusy (409/skip), never a block.
- After receipts land, only the worktree's node_modules and the
  per-op temp HOME are removed (regenerable, app-owned). Receipts,
  logs and diffs are never touched.

Python 3.9 portable. POSIX only (darwin for sandbox-exec); other
platforms refuse with a recorded reason.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import paths
from .util import utcnow, write_json

MIN_DISK_BYTES = 30 * 1024 ** 3
LOG_CAP_BYTES = 2 * 1024 * 1024
# L2: macOS AF_UNIX paths cap at ~103 bytes, and tsx appends
# tsx-<uid>/<pid>.pipe under TMPDIR — the old fc-<op>-<pid> name hit
# 114 bytes and failed falsely with EADDRINUSE. The per-op temp HOME
# carries only the op hash (op.txt inside names the op), and the run
# refuses up front when even the spare cannot fit.
_UNIX_PATH_MAX = 103
_TMP_SPARE = 40


def op_tmp_home(base: Path, op_id: str) -> Path:
    """Short per-op temp HOME: tmp/fc-<10 hex>-<pid>."""
    digest = hashlib.sha256(op_id.encode("utf-8")).hexdigest()[:10]
    return base / "tmp" / ("fc-%s-%d" % (digest, os.getpid()))
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_PROFILE = ("(version 1)(allow default)(deny network*)"
                   "(allow network* (local unix))"
                   "(allow network* (remote unix))"
                   "(deny network-outbound (literal "
                   "\"/private/var/run/mDNSResponder\"))")
SANDBOX_LOOPBACK_PROFILE = (
    "(version 1)(allow default)(deny network*)"
    "(allow network* (local unix))"
    "(allow network* (remote unix))"
    "(allow network-bind (local ip \"localhost:*\"))"
    "(allow network-outbound (to ip \"localhost:*\"))"
    "(allow network-inbound (from ip \"localhost:*\"))"
    "(deny network-outbound (literal "
    "\"/private/var/run/mDNSResponder\"))")
# Loopback is honored ONLY for `flutter test` steps (the tester
# binds a loopback HttpServer at startup; the strict profile fails
# it with EPERM). Every other non-install step stays strict —
# run_step falls back silently and the receipt profile says so.


def _allows_loopback(argv: list) -> bool:
    """True only for a flutter test runner invocation."""
    return list(argv or [])[:2] == ["flutter", "test"]
SANDBOX_BASE_PROFILE = "(version 1)(allow default)"
SYSTEM_PATH_DIRS = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


class FastcheckBlocked(Exception):
    """Lane-blocking failure with an owner-readable reason."""


class FastcheckSkip(Exception):
    """Transient skip (slot busy, sandbox missing): try a later tick."""


def step_network_sandbox(step):
    """Network-sandbox flag for one step record.

    New receipts carry "network_sandbox"; receipts written before S18
    carry "sandbox" — both read the same way so history keeps its
    meaning. Values are False (no sandbox), True (old strict
    receipts), "strict", or "loopback-only" — all truthy except
    False, so bool() keeps working for every generation."""
    if not isinstance(step, dict):
        return False
    if "network_sandbox" in step:
        return bool(step["network_sandbox"])
    return bool(step.get("sandbox", False))


def sandbox_mode(step) -> object:
    """Raw sandbox mode for the compact op-record summary (M3.3b).

    step_network_sandbox() collapses to bool, which made loopback
    steps read as fully "네트워크 차단" in the UI — overstating the
    isolation. The summary stores the raw mode instead: False (no
    sandbox), "strict", or "loopback-only". Legacy receipts carrying
    True read as "strict". Anything unrecognized reads as False
    (fail-closed: never claim more isolation than recorded)."""
    if not isinstance(step, dict):
        return False
    value = step.get("network_sandbox", False)
    if value is True:
        return "strict"
    if value in ("strict", "loopback-only"):
        return value
    return False


def _disk_avail_bytes(path) -> int:
    """Conservative free bytes (statvfs available-to-user).

    Deliberately SMALLER than macOS
    volumeAvailableCapacityForImportantUsage (which adds purgeable
    space): the guard blocks more often, never less. Injectable in
    tests via run(_disk_avail=...)."""
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize


_EX_DEP_KEYS = ("dependencies", "devDependencies",
                 "optionalDependencies")
_EX_PKG_DIRS = ("", "example")
_TOKEN_SPLIT_RE = re.compile(r"[\s;&|()={}\"'`]+")


def _read_package_json(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _gate_script(worktree: str, argv: list):
    """Resolve a profile step to the package script it runs, if any.

    Understands `yarn <script>` / `npm run <script>` /
    `yarn workspace <dir> <script>`; anything else (install, raw
    binaries, flags) resolves to None and never counts as direct.
    Returns (pkg_label, script_name, script_text)."""
    if len(argv) < 2:
        return None
    rest = list(argv[1:])
    pkgdir = Path(worktree)
    if rest[0] == "workspace" and len(rest) >= 3 \
            and str(argv[0]) == "yarn":
        pkgdir = Path(worktree) / rest[1]
        name = rest[2]
    elif rest[:1] == ["run"] and len(rest) >= 2:
        name = rest[1]
    elif str(rest[0]).startswith("-"):
        return None
    else:
        name = rest[0]
    pkg = _read_package_json(pkgdir / "package.json")
    if pkg is None:
        return None
    scripts = pkg.get("scripts")
    if not isinstance(scripts, dict):
        return None
    text = scripts.get(name)
    if not isinstance(text, str):
        return None
    rel = str(pkgdir.relative_to(worktree)) \
        if str(pkgdir) != str(worktree) else "root"
    return (rel, str(name), text)


def _script_mentions_exact(script: str, names: list) -> bool:
    """Exact token matches only: the dep name or one of its bins.

    M2.5d: this decides "direct". The old companion-prefix guess
    (`webpack` -> `webpack-dev-server`) over-claimed, so it lives
    only in _script_mentions now (consumer-script listing)."""
    toks = set(_TOKEN_SPLIT_RE.split(script or ""))
    for name in names:
        if not name:
            continue
        if name in toks or f".bin/{name}" in (script or ""):
            return True
    return False


def _script_mentions(script: str, names: list) -> bool:
    if _script_mentions_exact(script, names):
        return True
    toks = set(_TOKEN_SPLIT_RE.split(script or ""))
    for name in names:
        if not name:
            continue
        # Companion binaries: `webpack serve` runs webpack-dev-server
        # without naming it — a script token that is a hyphen-prefix
        # of the name counts (webpack -> webpack-serve). The hyphen
        # boundary keeps `web` from matching `webpack`. Listing-only:
        # never decides "direct".
        for tok in toks:
            if len(tok) >= 2 and (name.startswith(tok + "-")
                                  or name.startswith(tok + "/")):
                return True
    return False


def _via_tool_gate(dep: str, gate: str, script_text: str):
    """M2.5d: which gate tool conventionally loads this dep, if any.

    Plugin/config/types packages run inside a gate tool without
    being named in its script: eslint plugins in lint, babel
    plugins/presets in prepare or unit, prettier plugins in lint
    when the lint script itself runs prettier, @types in typecheck.
    Returns the gate key the dep rides with, else None. A recorded
    convention ("규칙 추정"), never proof — the timeline says so.
    """
    dep = str(dep or "")
    gate = str(gate or "")
    if gate == "lint" and (
            dep.startswith("eslint-plugin-")
            or dep.startswith("eslint-config-")
            or "/eslint-plugin" in dep or dep.endswith("/eslint-plugin")
            or "/eslint-config" in dep or dep.endswith("/eslint-config")):
        return "lint"
    if gate in ("prepare", "unit") and (
            dep.startswith("babel-plugin-")
            or dep.startswith("babel-preset-")
            or dep.startswith("@babel/")):
        return gate
    if gate == "lint" and dep.startswith("prettier-plugin-") \
            and "prettier" in (script_text or ""):
        return "lint"
    if gate == "typecheck" and dep.startswith("@types/"):
        return "typecheck"
    return None


def _step_label(step: dict, resolved=None) -> str:
    """M2.5d: a step reads as its gate key + script, never bare argv.

    E.g. "unit (test: jest)". `resolved` is the _gate_script
    triple (pkg_label, script_name, script_text)."""
    argv = list((step or {}).get("argv") or [])
    gate = str((step or {}).get("gate") or "")
    if not gate:
        gate = str((step or {}).get("name") or (argv[0] if argv else "?"))
    if resolved is not None:
        _rel, name, text = resolved
        first = (str(text or "").split() or [""])[0]
        if first and first != name:
            return f"{gate} ({name}: {first})"
        return f"{gate} ({name})"
    if len(argv) > 1:
        return f"{gate} ({' '.join(str(a) for a in argv[1:])})"
    return gate


def _dep_bin_names(worktree: str, dep: str) -> list | None:
    """Entry-point names for dep: itself plus its package bins.

    Read from the installed node_modules copy (root, then example);
    None when unreadable — the caller reports unknown, never a
    guess. No network."""
    for nm in (Path(worktree) / "node_modules",
               Path(worktree) / "example" / "node_modules"):
        pkg = _read_package_json(nm / dep / "package.json")
        if pkg is None:
            continue
        names = [dep]
        bins = pkg.get("bin")
        if isinstance(bins, str):
            names.append(Path(bins).name)
        elif isinstance(bins, dict):
            names.extend(list(bins.keys()))
            names.extend(Path(v).name for v in bins.values()
                         if isinstance(v, str))
        return sorted({n for n in names if n})
    return None


def check_exercised(worktree: str, dep, steps, *,
                    git_bound=None) -> dict:
    """M2.5a/d: did any gate step execute the bumped dep?

    Pure read-only census over the worktree: which package.json(s)
    declare dep, and whether any gate step's script names dep (or
    one of its installed bins) exactly ("direct"), or the dep is a
    plugin/config/types package a gate tool loads by convention
    ("via-tool", with the gate recorded — a guess, said as one).
    "not-run" names the consumer scripts; "unknown" means anything
    unreadable. Never raises, never blocks — a plain read the
    timeline wording uses honestly. 301: the tracked-file census
    is bound to the prep record."""
    out = {"dep": dep, "status": "unknown", "declared_in": [],
           "direct_steps": [], "via": {}, "consumer_scripts": [],
           "ci_consumers": [], "reason": ""}
    try:
        return _check_exercised_inner(str(worktree), dep, steps, out,
                                      git_bound=git_bound)
    except Exception:  # noqa: BLE001 - unknown, never an error
        out["reason"] = "읽기 실패"
        return out


_CI_WORKFLOW_DIR = ".github/workflows"
_CI_WORKFLOW_EXTS = (".yml", ".yaml")
# Root tool-config files, bound by filename stem == dep or bin.
# Extend only with a file shown in a real tree (turbo.json is in
# every nitro worktree; lefthook.yml exists but its dep's bin is
# not proven, so it stays out).
_TOOL_CONFIG_FILES = ("turbo.json",)
_COMMENT_RE = re.compile(r"(^|\s)#.*$")


def _fc_bound(worktree: str, git_bound):
    """Prep record for this worktree read, or None (fail-open shape).

    A passed record is used as-is (re-proved before every spawn).
    Otherwise the record is taken at entry: app-side reads before
    any step runs, so no model plant is trusted. Post-step callers
    must pass the pre-step record (run() returns it as
    summary["git"]) instead of recording fresh. Non-git fixtures
    yield None (callers fall back to on-disk).
    """
    if git_bound is not None:
        return git_bound
    try:
        from . import runner as _runner
        return _runner.record_git_pointer(worktree)
    except Exception:
        return None


def _fc_run(worktree: str, args: list, git_bound=None,
            timeout: int = 30):
    """Hardened git read: pinned + scrubbed when bound, hardened
    flags + scrubbed env otherwise. (None, False) on any failure,
    including a refused pointer — never a partial answer.
    301b: index-refreshing verbs (diff, status) run against a
    private per-call index copy (M2.21b), bound and unbound alike.
    When the copy cannot be made the read keeps the env rule
    only — never a refusal for that.
    302: reads ignore in-tree .gitattributes (--attr-source), so
    no planted filter/diff driver runs. A git too old for the
    flag fails closed: SourceUnreadable when bound, (None, False)
    here when unbound.
    302c: a hung probe surfaces as this call's own failure —
    SourceUnreadable when bound, (None, False) here when
    unbound — never as a cached refusal, so one transient stall
    cannot brick later reads."""
    try:
        from . import runner as _runner
        from .util import git_readonly_child_env as _ro_env
        if git_bound is not None:
            try:
                supported = _runner.attr_source_supported()
            except subprocess.TimeoutExpired as exc:
                raise _runner.SourceUnreadable(
                    "attr-source") from exc
            if not supported:
                raise _runner.SourceUnreadable("attr-source")
            try:
                paths = _runner.reprove_git_pointer(
                    worktree, git_bound)
            except Exception:
                return None, False
            argv = _runner.hardened_git_argv(
                list(args), gitdir=paths["gitdir"],
                worktree=paths["worktree"])
            cwd = None
        else:
            try:
                supported = _runner.attr_source_supported()
            except subprocess.TimeoutExpired:
                return None, False
            if not supported:
                return None, False
            argv = _runner.hardened_git_argv(
                ["-C", str(worktree)] + list(args))
            cwd = None
        with _ro_env(argv, _runner.hardened_git_env(),
                     cwd=cwd) as env:
            proc = subprocess.run(
                argv, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=timeout, check=False, env=env)
    except Exception as exc:
        # 302: an attr-source refusal fails closed as a raise when
        # bound; every other failure keeps the (None, False) shape.
        if getattr(exc, "op", None) == "attr-source":
            raise
        return None, False
    if proc.returncode != 0 or proc.stdout is None:
        return None, False
    try:
        text = proc.stdout.decode("utf-8", "replace")
    except Exception:
        return None, False
    return text, True


def _git_tracked_files(worktree: str, *, git_bound=None):
    """Tracked paths relative to the worktree, or None when git
    cannot answer (non-git fixture trees fall back to on-disk).

    M2.8b2: no configured helper program may run — fsmonitor and
    hooks stay off on every call. 301: bound to the prep record
    (pinned gitdir, scrubbed env, nested .git refused); a refused
    pointer reads as None, never a forged file list."""
    text, ok = _fc_run(worktree, ["ls-files", "-z"],
                       git_bound=_fc_bound(worktree, git_bound))
    if not ok:
        return None
    return {p for p in text.split("\0") if p}


def _strip_yaml_comments(text: str) -> str:
    """Cut `#` comments so a dep named only in prose or a URL is
    never reported as used (heuristic: `#` at line start or after
    whitespace starts a comment)."""
    kept = []
    for line in (text or "").splitlines():
        cut = _COMMENT_RE.search(line)
        kept.append(line[:cut.start()] if cut else line)
    return "\n".join(kept)


def _ci_consumers(worktree: str, names: list, *,
                  git_bound=None) -> list:
    """M2.8a1: CI-side consumers of the dep (read-only, never
    blocks). Tracked workflow files mentioning the dep or a bin as
    an exact token, plus allowlisted root tool configs whose stem
    IS the dep/bin. Sorted basenames, deduplicated. The status
    stays not-run — no local step ran it; this only says where CI
    runs it instead. 301: bound to the prep record."""
    names = [str(n or "") for n in (names or []) if n]
    if not names:
        return []
    try:
        return _ci_consumers_inner(str(worktree), names,
                                   git_bound=git_bound)
    except Exception:
        return []


def _ci_consumers_inner(worktree: str, names: list, *,
                        git_bound=None) -> list:
    found = []
    tracked = _git_tracked_files(worktree, git_bound=git_bound)
    wf_dir = Path(worktree) / _CI_WORKFLOW_DIR
    try:
        entries = sorted(wf_dir.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.suffix not in _CI_WORKFLOW_EXTS \
                or not entry.is_file():
            continue
        rel = entry.relative_to(worktree).as_posix()
        if tracked is not None and rel not in tracked:
            continue
        try:
            text = entry.read_text(encoding="utf-8",
                                   errors="replace")
        except OSError:
            continue
        if _script_mentions_exact(_strip_yaml_comments(text),
                                  names):
            found.append(entry.name)
    for cfg in _TOOL_CONFIG_FILES:
        if Path(cfg).stem not in names:
            continue
        if tracked is not None and cfg not in tracked:
            continue
        if Path(worktree, cfg).is_file() \
                and cfg not in found:
            found.append(cfg)
    return sorted(found)


def _check_exercised_inner(worktree: str, dep, steps, out: dict, *,
                           git_bound=None) -> dict:
    dep = str(dep or "")
    if not dep:
        out["reason"] = "dep 없음"
        return out
    out["dep"] = dep
    declarers = []
    for rel in _EX_PKG_DIRS:
        pkg = _read_package_json(Path(worktree) / rel / "package.json")
        if pkg is None:
            continue
        label = rel or "root"
        for key in _EX_DEP_KEYS:
            deps = pkg.get(key)
            if isinstance(deps, dict) and dep in deps:
                declarers.append((label, pkg))
                break
    if not declarers:
        out["reason"] = "선언 없음"
        return out
    out["declared_in"] = [label for label, _ in declarers]
    bins = _dep_bin_names(worktree, dep)
    names = [dep] + [b for b in (bins or []) if b != dep]
    # M2.8a1: CI-side consumers are independent of the install
    # record — workflows and root tool configs name the dep even
    # when no local step ran it. The status below is unchanged.
    out["ci_consumers"] = _ci_consumers(worktree, names,
                                          git_bound=git_bound)
    if bins is None:
        # Without the install record the entry points are unknown,
        # so no honest claim either way (spec: missing
        # node_modules -> unknown).
        out["reason"] = "설치 정보 없음"
        return out
    gate_scripts = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        got = _gate_script(worktree, list(step.get("argv") or []))
        if got is None:
            continue
        gate = str(step.get("gate") or "")
        gate_scripts.append((_step_label(step, got), gate,
                            got[2], got[0]))
    # M2.16a: a census rule credits a step only when that step
    # resolves the bumped tree. A bump touching only workspace
    # manifest(s) (no root declaration) is never credited by a
    # root-level step — the status stays not-run with that
    # workspace's consumer scripts, and the M2.15 consumer gate
    # then runs the workspace's one-shot build. Root-manifest
    # bumps read exactly as today.
    decl_labels = {label for label, _pkg in declarers}
    workspace_only = bool(decl_labels) and "root" not in decl_labels
    # M2.5d: "direct" is exact dep/bin tokens only — the
    # companion-prefix guess no longer decides it (it only lists
    # consumer scripts below).
    direct = sorted({label for label, _gate, text, rel in gate_scripts
                     if _script_mentions_exact(text, names)
                     and (not workspace_only or rel in decl_labels)})
    if direct:
        out["status"] = "direct"
        out["direct_steps"] = direct
        return out
    via_hits = []
    for label, gate, text, rel in gate_scripts:
        rode = _via_tool_gate(dep, gate, text)
        if rode:
            if workspace_only and rel not in decl_labels:
                continue
            via_hits.append({"gate": rode, "label": label})
    if via_hits:
        # Deterministic pick when several gates could load it:
        # pipeline order (prepare < typecheck < lint < unit).
        order = {"prepare": 0, "typecheck": 1, "lint": 2, "unit": 3}
        via_hits.sort(key=lambda h: (order.get(h["gate"], 9),
                                     h["label"]))
        out["status"] = "via-tool"
        out["via"] = via_hits[0]
        return out
    out["status"] = "not-run"
    seen = []
    for rel, pkg in declarers:
        scripts = pkg.get("scripts")
        if not isinstance(scripts, dict):
            continue
        for sname, text in scripts.items():
            if isinstance(text, str) and _script_mentions(text, names) \
                    and f"{rel}: {sname}" not in seen:
                seen.append(f"{rel}: {sname}")
    if workspace_only:
        # M2.16a: the bumped tree's one-shot builds are consumer
        # candidates even when no script names the dep (live
        # run-160ec8d7: example's build:web exercises the 8.x
        # runtime through babel-loader without naming it). Only
        # allow-listed one-shots qualify — never start/serve,
        # never android/ios. Root-manifest bumps list as today.
        for rel, pkg in declarers:
            if rel == "root":
                continue
            scripts = pkg.get("scripts")
            if not isinstance(scripts, dict):
                continue
            for sname in sorted(scripts):
                entry = f"{rel}: {sname}"
                if entry not in seen \
                        and consumer_script_allowed(sname):
                    seen.append(entry)
    out["consumer_scripts"] = seen
    return out


# M2.15 consumer build step (new "consumer" gate).
#
# A dep no fastcheck step directly executed (census "not-run") may
# still be exercised by a one-shot build script (live: webpack runs
# only in example's build:web). The consumer step runs at most ONE
# such script — never in the derived/review worktree (a build:web
# writes into the TRACKED example/dist and would dirty the recorded
# fingerprint), but in a scratch APFS-clone copy under the app tmp
# dir, with the same sandbox (deny-default writes for the scratch
# dir + app caches, network strict, scrubbed env, hooks off).
CONSUMER_TIMEOUT_S = 600
_CONSUMER_SCRIPT_RE = re.compile(r"^build(:[a-z0-9-]+)?$")
_CONSUMER_DENY_TOKENS = frozenset({"android", "ios"})
_CONSUMER_MANAGERS = ("yarn", "npm", "pnpm")
_WORKSPACE_DELEGATE_RE = re.compile(r"\bworkspace\s+(\S+)\s+(\S+)")


def consumer_script_allowed(name: str) -> bool:
    """Allow-list for the consumer build step (M2.15, pure).

    One-shot `build*` scripts only (`^build(:[a-z0-9-]+)?$`):
    never start/web/serve/dev/android/ios/build:android/build:ios
    or anything needing a device, a simulator, a native toolchain
    or a long-running server. The bare names never match the
    regex; android/ios tokens are denied even in the build: form.
    """
    text = str(name or "")
    if not _CONSUMER_SCRIPT_RE.match(text):
        return False
    toks = set(re.split(r"[:\-]", text))
    return not (toks & _CONSUMER_DENY_TOKENS)


def _consumer_script_text(worktree: str, rel: str, name: str) -> str:
    """The raw script text for a "rel: name" census entry."""
    pkgdir = Path(worktree) if rel == "root" \
        else Path(worktree) / rel
    pkg = _read_package_json(pkgdir / "package.json")
    scripts = (pkg or {}).get("scripts")
    if not isinstance(scripts, dict):
        return ""
    text = scripts.get(name)
    return text if isinstance(text, str) else ""


def pick_consumer_script(worktree: str,
                         consumer_scripts: list) -> dict | None:
    """Pick at most ONE consumer script (M2.15, pure read-only).

    Only allow-listed one-shot builds qualify. When a root script
    delegates to a workspace script (`yarn workspace <dir> <name>`),
    the workspace entry wins (live: root build:web delegates to
    example's build:web). Otherwise workspace (non-root) entries
    come before root ones, alphabetical — deterministic. Returns
    {"rel", "name", "label"} or None when nothing qualifies (the
    census wording then stays as today). Never raises.
    """
    try:
        entries = []
        for entry in consumer_scripts or []:
            text = str(entry or "")
            if ": " in text:
                rel, name = text.split(": ", 1)
            elif ":" in text:
                rel, name = text.split(":", 1)
            else:
                continue
            rel, name = rel.strip(), name.strip()
            if rel and name and consumer_script_allowed(name):
                entries.append((rel, name))
        if not entries:
            return None
        delegated = set()
        for rel, name in entries:
            if rel != "root":
                continue
            script_text = _consumer_script_text(
                str(worktree), rel, name)
            match = _WORKSPACE_DELEGATE_RE.search(script_text)
            if match and consumer_script_allowed(match.group(2)):
                delegated.add(match.group(2))
        if delegated:
            cands = sorted((rel, name) for rel, name in entries
                           if rel != "root" and name in delegated)
            if cands:
                rel, name = cands[0]
                return {"rel": rel, "name": name,
                        "label": f"{rel}: {name}"}
        ordered = sorted(entries,
                         key=lambda e: (e[0] == "root", e[0], e[1]))
        rel, name = ordered[0]
        return {"rel": rel, "name": name, "label": f"{rel}: {name}"}
    except Exception:  # noqa: BLE001 - no pick, never an error
        return None


def consumer_manager(profile_steps: list, worktree: str) -> str:
    """The package manager for the consumer script (M2.15, pure).

    The profile's own manager first (its install step argv), then
    the worktree lockfile, else yarn. Never raises.
    """
    try:
        for step in profile_steps or []:
            argv = (step or {}).get("argv") or []
            if argv and Path(str(argv[0])).name in _CONSUMER_MANAGERS:
                return Path(str(argv[0])).name
        for lock, manager in (("yarn.lock", "yarn"),
                              ("pnpm-lock.yaml", "pnpm"),
                              ("package-lock.json", "npm")):
            if Path(str(worktree), lock).is_file():
                return manager
    except Exception:  # noqa: BLE001 - default below
        pass
    return "yarn"


_TURBO_DEP = "turbo"
_TURBO_TASK = "build:web"
_TURBO_VERSION_FULL_RE = re.compile(
    r"v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")
_TURBO_VERSION_LINE_RE = re.compile(
    r"(?m)^\s*v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\s*$")
_TURBO_EVIDENCE_TAIL = 2000
_TURBO_LOG_WINDOW = 65536
_TURBO_GRAPH_MAX_BYTES = 1024 * 1024


def _turbo_expected_target(op_rec: dict) -> str:
    """Normalized x.y.z the turbo upgrade targets (M2.17, pure).

    Explicit full-version validation only (optional leading v);
    junk with an embedded semver never qualifies. "" when the
    record carries no valid target — the caller stays honest
    not-run instead of guessing.
    """
    try:
        raw = str((op_rec or {}).get("target") or "").strip()
    except Exception:  # noqa: BLE001 - never guess
        return ""
    match = _TURBO_VERSION_FULL_RE.fullmatch(raw)
    return match.group(1) if match else ""


def _turbo_actual_version(text: str) -> tuple:
    """(version, problem) from a --version log tail (M2.17, pure).

    The version must come from the actual CLI stdout: only
    full-line version matches count, so a yarn banner version
    can never be mistaken for the turbo version. Zero matches
    report "확인 불가"; several distinct values report
    "모호함" — both fail closed downstream.
    """
    found = _TURBO_VERSION_LINE_RE.findall(text or "")
    distinct = sorted(set(found))
    if not distinct:
        return "", "확인 불가"
    if len(distinct) > 1:
        return "", "모호함 (" + ", ".join(distinct) + ")"
    return distinct[0], ""


def _turbo_declared_tasks(worktree: str) -> tuple:
    """(declared task names, readable) from turbo.json (M2.17, pure).

    Accepts both the v2 "tasks" and the v1 "pipeline" keys.
    Unreadable/missing/invalid files report readable False — the
    caller stays not-run, never a pass.
    """
    try:
        raw = Path(str(worktree), "turbo.json").read_text(
            encoding="utf-8")
    except OSError:
        return set(), False
    try:
        obj = json.loads(raw)
    except ValueError:
        return set(), False
    if not isinstance(obj, dict):
        return set(), False
    tasks = obj.get("tasks", obj.get("pipeline"))
    if not isinstance(tasks, dict):
        return set(), False
    return ({str(name) for name in tasks}, True)


def _read_log_tail(log_path, limit: int = _TURBO_LOG_WINDOW) -> str:
    """Bounded tail of a step log file (M2.17, read-only).

    Real run_step receipts carry no stdout field — command output
    lives in the log. A binary seek reads only the tail window;
    the file is preserved and never fully loaded into memory.
    "" when unreadable; the caller fails closed.
    """
    try:
        handle = open(str(log_path), "rb")
    except OSError:
        return ""
    try:
        try:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
        except OSError:
            return ""
    finally:
        try:
            handle.close()
        except OSError:
            pass
    return data.decode("utf-8", "ignore")


def _read_log_bounded(log_path, limit: int) -> tuple:
    """(full text, too_big) of a step log (M2.17, read-only).

    Graph parsing needs the complete output, not a tail slice —
    a truncated tail must never parse as whole JSON. Past the
    accepted bound the file is left unread and reported too_big;
    the file itself is preserved.
    """
    try:
        size = Path(str(log_path)).stat().st_size
    except OSError:
        return "", False
    if size > limit:
        return "", True
    try:
        with open(str(log_path), "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return "", False
    if len(data) > limit:
        return "", True
    return data.decode("utf-8", "ignore"), False


def _turbo_task_records(obj) -> list:
    """Raw dry-run task entries from a parsed object (M2.17).

    Shape as documented: a list of task records, or a dict of
    them. Entries are NOT filtered here — malformed or unknown
    entries refuse the run downstream instead of being silently
    ignored.
    """
    tasks = obj.get("tasks") if isinstance(obj, dict) else None
    if isinstance(tasks, dict):
        tasks = list(tasks.values())
    if not isinstance(tasks, list):
        return []
    return list(tasks)


def _turbo_record_id(entry: dict) -> str:
    for key in ("taskId", "task", "id", "name"):
        val = entry.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _turbo_record_deps(entry: dict) -> list:
    """Declared dependency refs of one dry-run task record."""
    refs: list = []
    for key in ("dependsOn", "dependencies"):
        val = entry.get(key)
        if isinstance(val, list):
            refs.extend(val)
    return [str(ref) for ref in refs if str(ref or "").strip()]


def _turbo_dep_base(ref: str) -> str:
    """Base task name of a dependency ref (^topo and pkg#task OK)."""
    text = str(ref or "").strip().lstrip("^")
    return text.split("#")[-1]


def _turbo_parse_tasks(text: str, declared: set) -> list:
    """Task ids from a --dry=json log tail (M2.17, pure).

    A task counts only as a structured CLI task record with an
    actual non-empty command matching the declared web task —
    bare strings, dict keys, and no-op records
    (missing/empty/"<NONEXISTENT>" command) never qualify. The
    graph must be Web-only: every dry-run task and every
    build:web dependency must resolve to the declared build:web
    task (several packages' build:web is fine); anything native
    or otherwise non-web refuses the run before it executes.
    Raises ValueError with the fail reason otherwise — an empty
    rc0 graph is never a pass.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Turbo 그래프 파싱 실패 (JSON 없음)")
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        raise ValueError("Turbo 그래프 파싱 실패 (형식 오류)")
    if _TURBO_TASK not in declared:
        raise ValueError("Turbo 선언 작업 없음 (build:web 미선언)")
    records = _turbo_task_records(obj)
    if not records:
        raise ValueError("Turbo 그래프 비어 있음 (작업 없음)")
    ids: list = []
    for entry in records:
        if not isinstance(entry, dict):
            raise ValueError(
                "Turbo 그래프 형식 오류 (작업 기록 아님)")
        tid = _turbo_record_id(entry)
        if not tid:
            raise ValueError("Turbo 작업 식별 불가 (ID 없음)")
        if tid.split("#")[-1] != _TURBO_TASK:
            raise ValueError(
                "Turbo 비-Web 작업 포함 "
                f"({tid}) — Web 전용 검증만 해당")
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip() \
                or command.strip() == "<NONEXISTENT>":
            raise ValueError(
                "Turbo 실행 불가 작업 (명령 없음 "
                f"{tid})")
        package = entry.get("package")
        if not isinstance(package, str) or not package.strip():
            raise ValueError(
                f"Turbo 작업 소속 불명 ({tid})")
        for ref in _turbo_record_deps(entry):
            if _turbo_dep_base(ref) != _TURBO_TASK:
                raise ValueError(
                    "Turbo 비-Web 의존 포함 "
                    f"({tid} -> {ref}) — Web 전용 검증만 해당")
        if tid not in ids:
            ids.append(tid)
    if not ids:
        raise ValueError("Turbo 선언 작업 없음 (build:web 미포함)")
    return ids


def consumer_scratch_dir(base, op_id: str) -> Path:
    """Scratch clone dir under the app tmp dir (fresh every call)."""
    digest = hashlib.sha256(
        str(op_id or "").encode("utf-8")).hexdigest()[:10]
    return Path(str(base)) / "tmp" / (
        "consumer-%s-%d-%d" % (digest, int(time.time()),
                               os.getpid()))


def clone_tree(src: str, dst: str) -> str:
    """APFS-clone copy of the worktree (M2.15, read-only source).

    `cp -c -R` (clonefile: instant, copy-on-write). "" on success;
    otherwise the honest skip reason ("복제 실패 — 소비 스크립트
    미실행 ...") — a clone failure is never a pass. Not APFS or no
    space fails here instead of running anywhere else.
    """
    try:
        proc = subprocess.run(
            ["cp", "-c", "-R", str(src), str(dst)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=300, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"복제 실패 — 소비 스크립트 미실행 ({exc})"[:200]
    if proc.returncode != 0:
        tail = ((proc.stderr or "").strip().splitlines() or [""])[
            -1].strip()
        detail = tail or f"rc={proc.returncode}"
        return f"복제 실패 — 소비 스크립트 미실행 ({detail})"[:200]
    return ""


def tree_identity(worktree: str, *, git_bound=None) -> tuple:
    """(head, fingerprint) of a worktree (M2.15, read-only).

    ("", "") when unreadable — the caller fails closed instead of
    claiming an unchanged tree it could not read. 301: bound to
    the prep record when passed (re-proved before any spawn).
    """
    try:
        from . import runner as _runner
        bound = git_bound if git_bound is not None else _fc_bound(
            str(worktree), None)
        snap = _runner.source_snapshot(Path(worktree),
                                       git_bound=bound)
    except Exception:  # noqa: BLE001 - unreadable, never an error
        return ("", "")
    if not isinstance(snap, dict):
        return ("", "")
    return (str(snap.get("head") or ""),
            str(snap.get("fingerprint") or ""))


def _neutralize_scratch_git(scratch: Path) -> None:
    """Drop the cloned git pointer inside the scratch copy.

    Prepared worktrees are linked checkouts: .git is a gitfile
    pointing at the owner's real git dir. Left in place, the
    sandbox write-allowlist would admit the owner's git admin dir
    (the consumer allows ONLY the scratch dir + the app caches).
    The scratch is ours (removed after the step), so dropping its
    copy is containment, not data loss. Best-effort.
    """
    try:
        dotgit = scratch / ".git"
        if dotgit.is_symlink() or dotgit.is_file():
            dotgit.unlink()
        elif dotgit.is_dir():
            shutil.rmtree(dotgit)
    except OSError:
        pass


def consumer_dep_names(op_rec) -> list:
    """[primary dep, *group member deps] from an op record (pure).

    Derived family records carry "dep from->to" strings in group;
    joint records carry member dicts. Anything else is ignored —
    never raises.
    """
    names = []
    try:
        if not isinstance(op_rec, dict):
            return []
        dep = str(op_rec.get("dep") or "")
        if dep:
            names.append(dep)
        for entry in (op_rec.get("group") or []):
            if isinstance(entry, str) and entry.strip():
                names.append(entry.strip().split()[0])
            elif isinstance(entry, dict) and entry.get("dep"):
                names.append(str(entry.get("dep")))
        for member in (op_rec.get("members") or []):
            if isinstance(member, dict) and member.get("dep"):
                names.append(str(member.get("dep")))
    except Exception:  # noqa: BLE001 - best effort
        pass
    seen = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return seen


def apply_consumer_result(summary: dict, cons: dict | None) -> dict:
    """Fold a consumer record into a fastcheck summary (M2.15, pure).

    None (nothing qualified) leaves the summary untouched. Pass and
    skip only add the record (+ receipt); fail also marks the run
    red with the consumer tail (the driver then blocks the lane —
    never batch). Returns the same summary.
    """
    if not isinstance(summary, dict) or cons is None:
        return summary
    if not isinstance(cons, dict):
        return summary
    summary["consumer"] = {
        key: cons.get(key) for key in
        ("status", "script", "name", "manager", "argv",
         "rc", "timed_out", "elapsed_s", "label", "reason",
         "log", "log_tail", "turbo_version_expected",
         "turbo_version_actual", "turbo_tasks", "turbo_task_count",
         "turbo_scope", "turbo_steps", "note") if key in cons}
    if isinstance(cons.get("receipt"), dict):
        summary["steps"].append(cons["receipt"])
    if cons.get("status") == "fail":
        summary["ok"] = False
        summary["failed"] = {
            "pos": len(summary.get("steps") or []) - 1,
            "argv": cons.get("argv") or [],
            "rc": cons.get("rc"),
            "timed_out": cons.get("timed_out", False),
            "log_tail": cons.get("log_tail") or ""}
    return summary


def _turbo_scope_filter(worktree: str, rel: str) -> list:
    """Workspace scope flag for the turbo build (M2.17, pure).

    `turbo run build:web` unfiltered would also run a delegating
    root script; scoping to the actual declared workspace package
    executes exactly the recorded workspace build. [] when the
    build lives at root or the workspace has no readable name —
    the caller then runs unfiltered, never guessing a scope.
    """
    if rel == "root":
        return []
    try:
        pkg = _read_package_json(
            Path(str(worktree)) / rel / "package.json")
    except Exception:  # noqa: BLE001 - unfiltered, never guess
        return []
    name = (pkg or {}).get("name") if isinstance(pkg, dict) else ""
    if isinstance(name, str) and name.strip():
        return ["--filter=" + name.strip()]
    return []


def _run_turbo_consumer_gate(worktree: str, op_rec: dict,
                             profile_steps: list, scripts: list,
                             env: dict, base, safe_op: str,
                             tmp_home: str, toolchain=(),
                             timeout_s: float = CONSUMER_TIMEOUT_S,
                             _clone=None, _run_step=None,
                             _identity=None,
                             _keep_scratch: bool = False
                             ) -> dict | None:
    """Three-step turbo Web consumer probe (M2.17, scratch-only).

    Only for the recorded primary turbo upgrade with the supported
    yarn manager and an actual declared one-shot build:web graph.
    Anything missing or unsupported returns None (honest not-run,
    census wording unchanged) — never a pass. Otherwise all three
    tool steps run in one scratch APFS-clone under the app tmp dir,
    reusing the generic gate's clone/git-neutralization/sandbox/
    identity/timeout/receipt machinery:

    1. resolved `turbo --version` must equal the recorded target
       (avoids a declared-version/old-node_modules false pass);
    2. `turbo run build:web --dry=json` must parse and hold at
       least one actual declared matching task (an empty rc0
       graph is never a pass);
    3. `turbo run build:web --force` (workspace-scoped when the
       build lives in a named workspace) must execute the real
       uncached build with rc 0.

    Exact static argv lists through the existing runner, never
    shell-constructed. The three steps share the one existing
    consumer budget (no fresh 600s per step). Version/dry alone
    never mark the consumer pass. Any failure or unreadable
    source identity fails closed ("작업실이 바뀜"); no retry.
    The record carries every output plus expected/actual version,
    task ids/count and scope. Human label covers the Turbo Web
    orchestration / real-Web-consumer verification only — the
    Android/native CI stays explicitly unverified.
    """
    try:
        target = _turbo_expected_target(op_rec)
    except Exception:  # noqa: BLE001 - never guess
        return None
    if not target:
        return None
    try:
        manager = consumer_manager(profile_steps, str(worktree))
    except Exception:  # noqa: BLE001 - unsupported, not-run
        return None
    if manager != "yarn":
        return None
    declared, readable = _turbo_declared_tasks(str(worktree))
    if not readable or _TURBO_TASK not in declared:
        return None
    candidates = [str(s) for s in (scripts or []) if str(s or "")]
    # Fallback: turbo is orchestrated, rarely named by the scripts
    # it runs, so a dep-mention census may list nothing. Discover
    # actually-declared one-shot build:web scripts directly from
    # the manifests — same allow-list, same delegation preference,
    # never anything the manifests do not declare.
    for rel_dir in _EX_PKG_DIRS:
        label = rel_dir or "root"
        entry = f"{label}: {_TURBO_TASK}"
        if entry in candidates:
            continue
        try:
            pkg = _read_package_json(
                Path(str(worktree)) / rel_dir / "package.json")
        except Exception:  # noqa: BLE001 - skip unreadable
            continue
        found = (pkg or {}).get("scripts") \
            if isinstance(pkg, dict) else None
        if isinstance(found, dict) \
                and isinstance(found.get(_TURBO_TASK), str) \
                and consumer_script_allowed(_TURBO_TASK):
            candidates.append(entry)
    try:
        picked = pick_consumer_script(str(worktree), candidates)
    except Exception:  # noqa: BLE001 - not-run, never pass
        return None
    if not picked or str(picked.get("name") or "") != _TURBO_TASK:
        return None
    rel = str(picked.get("rel") or "")
    label = str(picked.get("label") or "")
    scope = _turbo_scope_filter(str(worktree), rel)
    note = "Android/네이티브 CI 미검증 — Web 소비 검증만 해당"
    identify = _identity or tree_identity
    try:
        before = identify(str(worktree))
    except Exception:  # noqa: BLE001 - fail closed below
        before = ("", "")
    if not before[0] or not before[1]:
        return _turbo_fail(
            label, "", manager, [], 0, False, 0.0, None, [],
            "", "", [], "작업실이 바뀜", "작업실이 바뀜")
    clone = _clone or clone_tree
    scratch = consumer_scratch_dir(base, safe_op)
    if scratch.exists():
        return _turbo_skip(
            label, "복제 실패 — 소비 스크립트 미실행 (임시 경로 충돌)")
    reason = clone(str(worktree), str(scratch))
    if reason:
        return _turbo_skip(label, reason)
    started = time.time()
    steps: list = []
    runner = _run_step or run_step
    base_argv = [manager, "exec", _TURBO_DEP]
    # Dry and real runs use the EXACT same workspace scope: a
    # graph from another workspace must never qualify a filtered
    # run that would then do nothing.
    dry_argv = base_argv + ["run", _TURBO_TASK, "--dry=json"] + scope
    run_argv = base_argv + ["run", _TURBO_TASK, "--force"] + scope

    def _run_one(argv: list, log_name: str):
        """One tool step (M2.17).

        Returns (receipt, "") on rc 0, else (None, fail-name).
        The step record lands in steps either way; timeouts and
        spawn errors never run anything afterwards.
        """
        remaining = float(timeout_s or 0) - (time.time() - started)
        log_path = Path(str(base)) / "integration_ops" / (
            safe_op + ".fastcheck.turbo-consumer."
            + log_name + ".log")
        if remaining <= 0:
            # Explicit failed attempt: callers read steps[-1],
            # so a missing record would IndexError into the
            # outer generic skip instead of failing here.
            steps.append({"argv": list(argv), "rc": None,
                          "timed_out": True, "elapsed_s": 0.0,
                          "log": str(log_path),
                          "tail": "소비 검사 시간 초과"})
            return None, "소비 검사 시간 초과"
        try:
            receipt = runner(list(argv), str(scratch), env,
                             remaining, False, log_path,
                             loopback=False, toolchain=toolchain,
                             cwd=str(scratch))
        except Exception as exc:  # noqa: BLE001 - fail closed
            steps.append({"argv": list(argv), "rc": None,
                          "timed_out": False, "elapsed_s": 0.0,
                          "log": str(log_path),
                          "tail": f"소비 검사 오류: {exc}"[:2000]})
            return None, "소비 검사 오류"
        # Preserve the actual runner receipt unchanged — sandbox,
        # network, write-allowlist, truncation and children fields
        # are provenance, never reinvented. The bounded tail is
        # added alongside the receipt's own log tail.
        entry = dict(receipt) if isinstance(receipt, dict) else {}
        entry["argv"] = list(argv)
        entry["log"] = str(receipt.get("log", log_path)) \
            if isinstance(receipt, dict) else str(log_path)
        entry["tail"] = _read_log_tail(
            entry["log"])[-_TURBO_EVIDENCE_TAIL:]
        if "timed_out" in entry:
            entry["timed_out"] = bool(entry["timed_out"])
        steps.append(entry)
        if receipt.get("rc") != 0 or receipt.get("timed_out"):
            return None, "rc"
        return receipt, ""

    def _step_fail(step_name: str, argv: list, receipt):
        if receipt is None:
            last = steps[-1] if steps else {}
            problem = str(last.get("tail") or "")
            if problem.startswith("소비 검사 오류"):
                return _turbo_fail(
                    label, target, manager, argv, None, False, 0.0,
                    None, steps, "", "", [],
                    problem[:200], problem[:2000])
            if problem == "소비 검사 시간 초과":
                return _turbo_fail(
                    label, target, manager, argv, None, True, 0.0,
                    None, steps, "", "", [],
                    "소비 검사 시간 초과", "소비 검사 시간 초과")
        detail = (steps[-1]["tail"]
                  or f"rc={steps[-1]['rc']}")[-500:]
        return _turbo_fail(
            label, target, manager, argv, steps[-1]["rc"],
            bool(steps[-1]["timed_out"]),
            steps[-1]["elapsed_s"], steps[-1] if receipt is None
            else receipt, steps, "", "", [],
            f"{step_name} (rc={steps[-1]['rc']})",
            f"{step_name} — {detail}"[:2000])

    try:
        _neutralize_scratch_git(scratch)
        # 1. resolved turbo --version must equal the recorded
        # target — validated NOW, before anything else runs.
        receipt, _err = _run_one(
            base_argv + ["--version"], "version")
        if receipt is None:
            return _step_fail("Turbo 버전 확인 실패",
                              base_argv + ["--version"], receipt)
        actual, problem = _turbo_actual_version(steps[0]["tail"])
        if actual != target:
            detail = problem if problem else \
                f"예상 {target}, 실제 {actual}"
            return _turbo_fail(
                label, target, manager, base_argv + ["--version"],
                steps[0]["rc"], steps[0]["timed_out"],
                steps[0]["elapsed_s"], None, steps, target,
                actual, [],
                f"Turbo 버전 불일치 (예상 {target}, 실제 "
                f"{actual or problem})",
                f"Turbo 버전 불일치 — {detail}")
        # 2. dry graph must parse and be Web-only — validated NOW;
        # an invalid graph never reaches the real build.
        receipt, _err = _run_one(dry_argv, "dry")
        if receipt is None:
            return _step_fail("Turbo 그래프 확인 실패", dry_argv,
                              receipt)
        graph_text, too_big = _read_log_bounded(
            steps[1]["log"], _TURBO_GRAPH_MAX_BYTES)
        if too_big:
            return _turbo_fail(
                label, target, manager, dry_argv, steps[1]["rc"],
                steps[1]["timed_out"], steps[1]["elapsed_s"],
                None, steps, target, actual, [],
                "Turbo 그래프 로그 초과", "Turbo 그래프 로그 초과")
        try:
            task_ids = _turbo_parse_tasks(graph_text, declared)
        except ValueError as exc:
            return _turbo_fail(
                label, target, manager, dry_argv, steps[1]["rc"],
                steps[1]["timed_out"], steps[1]["elapsed_s"],
                None, steps, target, actual, [],
                str(exc)[:200], str(exc)[:2000])
        # 3. real uncached build, exact same scope as the dry run.
        receipt, _err = _run_one(run_argv, "run")
        if receipt is None:
            return _step_fail("Turbo 실제 빌드 실패", run_argv,
                              receipt)
        try:
            after = identify(str(worktree))
        except Exception:  # noqa: BLE001 - fail closed, never skip
            after = ("", "")
        if not after[0] or not after[1] or before != after:
            return _turbo_fail(
                label, target, manager, run_argv, steps[2]["rc"],
                steps[2]["timed_out"], steps[2]["elapsed_s"],
                None, steps, target, actual, task_ids,
                "작업실이 바뀜",
                ("작업실이 바뀜 — "
                 + str(steps[2]["tail"] or ""))[:2000])
        elapsed = time.time() - started
        # The successful consumer keeps the actual step-2 runner
        # receipt (same provenance contract as the generic gate),
        # only the gate/name mark it as the consumer record.
        run_receipt = dict(receipt) if isinstance(receipt, dict) \
            else dict(steps[2])
        run_receipt["gate"] = "consumer"
        run_receipt["name"] = f"consumer ({_TURBO_DEP}: {_TURBO_TASK})"
        return {"status": "pass",
                "script": f"{label} (turbo)",
                "name": f"consumer ({_TURBO_DEP}: {_TURBO_TASK})",
                "manager": manager,
                "argv": run_argv,
                "rc": 0, "timed_out": False, "elapsed_s": elapsed,
                "label": (f"Turbo Web 작업 실행 검증 통과 "
                          f"({elapsed:.0f}초)"),
                "log": steps[2]["log"],
                "log_tail": steps[2]["tail"],
                "receipt": run_receipt,
                "turbo_version_expected": target,
                "turbo_version_actual": actual,
                "turbo_tasks": task_ids,
                "turbo_task_count": len(task_ids),
                "turbo_scope": (" ".join(scope) if scope else "미지정"),
                "turbo_steps": steps,
                "note": note}
    finally:
        if not _keep_scratch:
            shutil.rmtree(str(scratch), ignore_errors=True)


def _turbo_skip(label: str, reason: str) -> dict:
    """Honest turbo skip (M2.17): never blocks, never a pass."""
    return {"status": "skip", "script": label,
            "name": f"consumer ({_TURBO_DEP}: {_TURBO_TASK})",
            "turbo_steps": [],
            "note": "Android/네이티브 CI 미검증 — Web 소비 검증만 해당",
            "reason": reason[:200]}


def _turbo_fail(label: str, target: str, manager: str, argv: list,
                rc, timed_out: bool, elapsed, receipt,
                steps: list, expected: str, actual: str,
                task_ids: list, reason: str, log_tail: str) -> dict:
    """Fail-closed turbo record (M2.17): blocks the lane, never batch."""
    return {"status": "fail", "script": label,
            "name": f"consumer ({_TURBO_DEP}: {_TURBO_TASK})",
            "manager": manager, "argv": list(argv or []),
            "rc": rc, "timed_out": bool(timed_out),
            "elapsed_s": elapsed,
            "log": (steps[-1]["log"] if steps else ""),
            "log_tail": str(log_tail or "")[:2000],
            "receipt": receipt,
            "turbo_version_expected": target,
            "turbo_version_actual": actual,
            "turbo_tasks": list(task_ids or []),
            "turbo_task_count": len(task_ids or []),
            "turbo_scope": "",
            "turbo_steps": steps,
            "note": "Android/네이티브 CI 미검증 — Web 소비 검증만 해당",
            "reason": str(reason or "")[:200]}


def run_consumer_gate(worktree: str, op_rec: dict,
                      profile_steps: list, step_receipts: list,
                      env: dict, base, safe_op: str, tmp_home: str,
                      toolchain=(), timeout_s: int = CONSUMER_TIMEOUT_S,
                      _clone=None, _run_step=None, _identity=None,
                      _keep_scratch: bool = False) -> dict | None:
    """Run the one consumer build script (M2.15).

    Only when the census for the record's dep (or any group member)
    says no fastcheck step ran it AND a consumer script qualifies.
    Returns None when nothing qualifies (census wording unchanged —
    native-only scripts stay "PR CI에서만 실행됨"). Otherwise a
    record: pass carries the label ("소비 스크립트 build:web 통과
    (N초)"); fail carries the scrubbed tail (the caller marks the
    run red); clone failure is an honest skip (never a pass); a
    changed derived-tree fingerprint fails loudly ("작업실이 바뀜",
    never batch). Never raises.
    """
    try:
        return _run_consumer_gate_inner(
            str(worktree), op_rec, profile_steps, step_receipts,
            env, base, safe_op, tmp_home, toolchain, timeout_s,
            _clone, _run_step, _identity, _keep_scratch)
    except Exception as exc:  # noqa: BLE001 - skip, never a block
        return {"status": "skip", "script": "",
                "reason": f"소비 검사 오류: {exc}"[:200]}


def _run_consumer_gate_inner(worktree: str, op_rec: dict,
                             profile_steps: list,
                             step_receipts: list, env: dict, base,
                             safe_op: str, tmp_home: str,
                             toolchain=(), timeout_s: int = 600,
                             _clone=None, _run_step=None,
                             _identity=None,
                             _keep_scratch: bool = False
                             ) -> dict | None:
    deps = consumer_dep_names(op_rec)
    if not deps:
        return None
    censuses = {}
    for dep in deps:
        try:
            censuses[dep] = check_exercised(
                str(worktree), dep, step_receipts)
        except Exception:  # noqa: BLE001 - unknown, never an error
            censuses[dep] = {"status": "unknown",
                             "consumer_scripts": []}
    notrun = [dep for dep in deps
              if str((censuses.get(dep) or {}).get("status") or "")
              == "not-run"]
    if not notrun:
        return None
    scripts = []
    for dep in notrun:
        for entry in ((censuses.get(dep) or {})
                      .get("consumer_scripts") or []):
            if entry not in scripts:
                scripts.append(entry)
    primary = str((op_rec or {}).get("dep") or "") \
        if isinstance(op_rec, dict) else ""
    if primary == _TURBO_DEP:
        # M2.17: only an explicit primary turbo upgrade takes this
        # path (a group-only turbo never becomes primary). A plain
        # generic one-shot-script run would not execute the turbo
        # binary, so the probe below returns a record or None
        # (honest not-run); all other ops keep the exact generic
        # selection/labels below, untouched.
        return _run_turbo_consumer_gate(
            str(worktree), op_rec, profile_steps, scripts, env,
            base, safe_op, tmp_home, toolchain, timeout_s,
            _clone, _run_step, _identity, _keep_scratch)
    picked = pick_consumer_script(str(worktree), scripts)
    if picked is None:
        return None
    label = str(picked.get("label") or "")
    rel = str(picked.get("rel") or "")
    name = str(picked.get("name") or "")
    manager = consumer_manager(profile_steps, str(worktree))
    argv = [manager, name]
    rundir = rel if rel != "root" else "."
    identify = _identity or tree_identity
    before = identify(str(worktree))
    clone = _clone or clone_tree
    scratch = consumer_scratch_dir(base, safe_op)
    if scratch.exists():
        return {"status": "skip", "script": label,
                "reason": "복제 실패 — 소비 스크립트 미실행 "
                          "(임시 경로 충돌)"}
    reason = clone(str(worktree), str(scratch))
    if reason:
        return {"status": "skip", "script": label, "reason": reason}
    try:
        _neutralize_scratch_git(scratch)
        cwd = str(scratch) if rundir == "." \
            else str(scratch / rundir)
        if not Path(cwd).is_dir():
            return {"status": "skip", "script": label,
                    "reason": "복제 실패 — 소비 스크립트 미실행 "
                              "(작업 경로 없음)"}
        runner = _run_step or run_step
        log_path = Path(str(base)) / "integration_ops" / (
            safe_op + ".fastcheck.consumer.log")
        receipt = runner(argv, str(scratch), env, timeout_s,
                         False, log_path, loopback=False,
                         toolchain=toolchain, cwd=cwd)
        receipt["gate"] = "consumer"
        try:
            resolved = (rel, name, _consumer_script_text(
                str(worktree), rel, name))
        except Exception:  # noqa: BLE001 - label falls back
            resolved = None
        receipt["name"] = str(
            _step_label({"gate": "consumer"}, resolved)
            if resolved else f"consumer ({name})")
        elapsed = receipt.get("elapsed_s", 0)
        try:
            secs = float(elapsed)
        except (TypeError, ValueError):
            secs = 0.0
        after = identify(str(worktree))
        if not before[0] or not before[1] or before != after:
            return {"status": "fail", "script": label,
                    "name": name, "argv": list(argv),
                    "rc": receipt.get("rc"),
                    "timed_out": receipt.get("timed_out", False),
                    "elapsed_s": elapsed,
                    "log": receipt.get("log"),
                    "log_tail": ("작업실이 바뀜 — "
                                 + str(receipt.get("log_tail")
                                       or ""))[:2000],
                    "receipt": receipt,
                    "reason": "작업실이 바뀜"}
        if receipt.get("rc") != 0 or receipt.get("timed_out"):
            return {"status": "fail", "script": label,
                    "name": name, "argv": list(argv),
                    "rc": receipt.get("rc"),
                    "timed_out": receipt.get("timed_out", False),
                    "elapsed_s": elapsed,
                    "log": receipt.get("log"),
                    "log_tail": str(
                        receipt.get("log_tail") or ""),
                    "receipt": receipt,
                    "reason": (f"소비 스크립트 실패 "
                               f"(rc={receipt.get('rc')})")}
        return {"status": "pass", "script": label, "name": name,
                "manager": manager, "argv": list(argv),
                "rc": 0, "timed_out": False, "elapsed_s": elapsed,
                "label": (f"소비 스크립트 {name} 통과 "
                          f"({secs:.0f}초)"),
                "log": receipt.get("log"),
                "receipt": receipt}
    finally:
        if not _keep_scratch:
            shutil.rmtree(str(scratch), ignore_errors=True)


_POD_LOCK_SPOTS = ("", "ios", "example", "example/ios")
_POD_PIN_RE = re.compile(r"^\s{2}(?:-\s+)?(\S+)\s+\(([^)]+)\)")
_PODSPEC_NAME_RE = re.compile(r"s\.name\s*=\s*['\"]([^'\"]+)['\"]")


def _podspec_name(pkgdir) -> str:
    """s.name from a shipped podspec (.podspec or .podspec.json)."""
    try:
        pods = sorted(Path(pkgdir).glob("*.podspec")) + sorted(
            Path(pkgdir).glob("*.podspec.json"))
    except OSError:
        return ""
    for pod in pods[:5]:
        try:
            text = pod.read_text(encoding="utf-8",
                                 errors="ignore")[:20000]
        except OSError:
            continue
        if pod.name.endswith(".json"):
            try:
                name = json.loads(text).get("name")
            except ValueError:
                continue
            if name:
                return str(name)
        else:
            m = _PODSPEC_NAME_RE.search(text)
            if m:
                return m.group(1)
    return ""


def _podfile_pins_by_file(worktree: str, *, git_bound=None) -> list:
    """[(rel, {pod: version})] per Podfile.lock, discovery order.

    Same discovery as the merged map below (`git ls-files`
    tracked truth, else the conventional spots), capped and
    best-effort — unreadable files are skipped, never raised.
    The rel path names the lock's directory for owner
    commands (`example/ios/Podfile.lock` -> `example/ios`).
    301: the ls-files discovery runs over the hardened path
    bound to the prep record; a refused pointer falls back to
    the conventional spots, never a forged file list."""
    files = []
    try:
        text, ok = _fc_run(
            worktree, ["ls-files", "-z", "--", "*Podfile.lock"],
            git_bound=_fc_bound(worktree, git_bound))
        if ok:
            files = [f for f in text.split("\0") if f]
    except (OSError, ValueError):
        files = []
    if not files:
        for rel in _POD_LOCK_SPOTS:
            cand = Path(worktree, rel, "Podfile.lock")
            if cand.is_file():
                files.append((rel + "/Podfile.lock") if rel
                             else "Podfile.lock")
    out = []
    for rel in files[:10]:
        try:
            text = Path(worktree, rel).read_text(
                encoding="utf-8", errors="ignore")
        except OSError:
            continue
        pins = {}
        in_pods = False
        for line in text.splitlines():
            s = line.strip()
            if s == "PODS:":
                in_pods = True
                continue
            if in_pods:
                if line and not line.startswith((" ", "\t")):
                    break
                m = _POD_PIN_RE.match(line)
                if m:
                    pins.setdefault(m.group(1), m.group(2))
        out.append((rel, pins))
    return out


def _podfile_pins(worktree: str, *, git_bound=None) -> dict:
    """{pod: version} from tracked Podfile.locks (read-only).

    Prefers `git ls-files` (tracked truth); falls back to the
    conventional spots when git cannot answer. Capped and
    best-effort — an empty map never blocks. 301: bound to the
    prep record."""
    pins = {}
    try:
        for _rel, _file_pins in _podfile_pins_by_file(
                str(worktree or ""), git_bound=git_bound):
            for _name, _ver in _file_pins.items():
                pins.setdefault(_name, _ver)
    except Exception:  # noqa: BLE001 - unknown, never an error
        pass
    return pins


_PREBUILT_PODS = ("ReactNativeDependencies", "React-Core-prebuilt",
                  "hermes-engine")


def prebuilt_pod_update(worktree, *, git_bound=None) -> str:
    """M2.16e: the exact owner `pod update` command for an RN tree.

    Read-only over the worktree's Podfile.lock PODS sections:
    the first lock (discovery order) pinning any of
    ReactNativeDependencies, React-Core-prebuilt or hermes-engine
    names exactly those pods —
    `cd <lock dir> && pod update <pods> --no-repo-update`
    (bare `pod update …` when the lock sits at the tree root).
    Live RN 0.88 joint: `pod install` fails on those bytes
    (prebuilt podspec versions differ from the lock), while the
    named update gives rc 0. Copyable text only — OpenMainty
    never runs pod. Unreadable or pod-free trees give "".
    Never raises.

    Pure "which pods, which dir": this helper does NOT decide
    drift — callers (the drift label, rollup drafts) only use
    its command when they have established a real version
    mismatch first."""
    try:
        for rel, pins in _podfile_pins_by_file(
                str(worktree or ""), git_bound=git_bound):
            pods = [p for p in _PREBUILT_PODS if p in pins]
            if not pods:
                continue
            loc = str(rel or "").rpartition("/")[0]
            base = f"cd {loc} && " if loc else ""
            return (f"{base}pod update {' '.join(pods)} "
                    "--no-repo-update")
        return ""
    except Exception:  # noqa: BLE001 - unknown, never an error
        return ""


_EXACT_VERSION_RE = re.compile(r"\d+\.\d+\.\d+"
                               r"(?:[-+][0-9A-Za-z.\-]+)?")


def tree_react_native_version(worktree) -> str:
    """M2.16e2: the exact react-native version a tree declares.

    Reads `example/package.json` first, else the root
    `package.json` (`dependencies`, then `devDependencies`);
    a range is resolved through the tree's `yarn.lock` (first
    resolvable manifest wins). Unreadable, missing or
    unresolvable declarations give "". Read-only; never
    raises."""
    try:
        from .util import read_json as _read_json
        base = str(worktree or "")
        if not base:
            return ""
        lock_text = None
        for rel in ("example", ""):
            try:
                pkg = _read_json(Path(base, rel, "package.json"),
                                 default=None)
            except (Exception, SystemExit):  # noqa: BLE001 -
                # unreadable manifest reads as absent
                continue
            if not isinstance(pkg, dict):
                continue
            for sect in ("dependencies", "devDependencies"):
                rng = (pkg.get(sect) or {}).get("react-native")
                if not rng:
                    continue
                rng = str(rng).strip()
                if _EXACT_VERSION_RE.fullmatch(rng):
                    return rng
                if lock_text is None:
                    try:
                        lock_text = Path(base, "yarn.lock").read_text(
                            encoding="utf-8", errors="ignore")
                    except OSError:
                        lock_text = ""
                if lock_text:
                    from .batches import (
                        _declared_resolutions as _resolve)
                    try:
                        got = _resolve(lock_text, "react-native",
                                       {rng})
                    except Exception:  # noqa: BLE001 - no resolution
                        got = None
                    vers = []
                    for ver in (got or {}).values():
                        vs = ver if isinstance(ver, list) else [ver]
                        vers.extend(str(v) for v in vs
                                    if _EXACT_VERSION_RE.fullmatch(
                                        str(v)))
                    if vers:
                        from . import versions as _versions
                        best = vers[0]
                        for cand in vers[1:]:
                            try:
                                if _versions.compare(cand, best) == 1:
                                    best = cand
                            except Exception:  # noqa: BLE001 - keep
                                continue
                        return best
        return ""
    except (Exception, SystemExit):  # noqa: BLE001 - unknown,
        # never an error
        return ""


def rollup_pod_command(worktree, *, git_bound=None) -> str:
    """M2.16e2: owner pod command for a rollup tree, drift-gated.

    Reads the tree's declared react-native version and reuses
    check_pod_drift (React-Core-prebuilt, else
    ReactNativeDependencies pin): the command only when they
    really differ — an in-sync lock (RN 0.86.2, prebuilt pins
    0.86.2) gives "". Unreadable manifests give "", never a
    raise. Copyable text only — OpenMainty never runs pod."""
    try:
        rn_ver = tree_react_native_version(worktree)
        if not rn_ver:
            return ""
        for pod in ("React-Core-prebuilt",
                    "ReactNativeDependencies"):
            try:
                drift = check_pod_drift(worktree, "react-native",
                                        rn_ver, fallback_pod=pod,
                                        git_bound=git_bound)
            except (Exception, SystemExit):  # noqa: BLE001 -
                # unknown, never fatal
                drift = None
            if drift and drift.get("command"):
                return str(drift["command"])
        return ""
    except (Exception, SystemExit):  # noqa: BLE001 - unknown,
        # never an error
        return ""


def check_pod_drift(worktree: str, dep, target,
                    fallback_pod: str = "", *,
                    git_bound=None) -> dict | None:
    """M2.5c native lock drift (read-only, no network, never blocks).

    When the bumped dep ships a podspec and a tracked Podfile.lock
    pins that pod at another version, the iOS tree is unfinished
    until the owner syncs the lock (never auto-run — the label
    only). Pod name comes from the podspec's s.name;
    `fallback_pod` (the package name, used for old receipts
    whose node_modules is already cleaned) is best-effort and
    documented as such. When the lock pins RN prebuilt pods the
    label names the exact `pod update` owner command (plus the
    `command` key); other drift keeps the plain `pod install`
    label with `command` "".
    Returns None or {dep, pod, pinned, target, label,
    command}."""
    try:
        return _check_pod_drift_inner(str(worktree), dep, target,
                                      fallback_pod,
                                      git_bound=git_bound)
    except Exception:  # noqa: BLE001 - unknown, never an error
        return None


def _podfile_spot_for_pod(worktree: str, pod: str, *,
                              git_bound=None) -> str:
    """Dir of the Podfile.lock pinning pod ("" = tree root).

    First lock (discovery order) whose PODS section pins pod —
    the same precedence the merged pin map uses. "" when none
    is found. Read-only; never raises."""

    try:
        for rel, pins in _podfile_pins_by_file(
                str(worktree or ""), git_bound=git_bound):
            if pod in (pins or {}):
                return str(rel or "").rpartition("/")[0]
    except Exception:  # noqa: BLE001 - unknown, never an error
        pass
    return ""


def _mixed_pod_command(worktree: str, pod: str, *,
                           git_bound=None) -> str:
    """One update command for non-prebuilt drift on an RN tree.

    Verdict-only RN check: the tree's declared react-native
    version against the merged prebuilt pins (no podspec
    lookup, so this can never recurse into per-pod logic —
    _check_pod_drift_inner calls it only for non-prebuilt
    pods). When the tree also carries RN prebuilt drift, the
    prebuilt command with this pod appended (prebuilt pods
    first); else "". Read-only; never raises."""

    # Same pair rollup_pod_command gates on: the core prebuilt
    # pins track the RN version literally, while hermes-engine
    # pins a nightly-style tag (e.g. 250829098.0.16) that never
    # equals it — string-comparing hermes would misfire on every
    # in-sync tree.
    _RN_PREBUILT_PAIR = ("React-Core-prebuilt",
                         "ReactNativeDependencies")
    try:
        rn_ver = tree_react_native_version(str(worktree or ""))
        if not rn_ver:
            return ""
        pins = _podfile_pins(str(worktree or ""),
                           git_bound=git_bound)
        if not any(pb in pins and pins[pb] != rn_ver
                   for pb in _RN_PREBUILT_PAIR):
            return ""
        cmd = prebuilt_pod_update(str(worktree or ""),
                                git_bound=git_bound)
        if not cmd:
            return ""
        # prebuilt_pod_update format (single source): "<cd … &&
        # >pod update <pods> --no-repo-update".
        return cmd.replace(" --no-repo-update",
                           f" {pod} --no-repo-update", 1)
    except Exception:  # noqa: BLE001 - unknown, never an error
        return ""


def _check_pod_drift_inner(worktree: str, dep, target,
                           fallback_pod: str, *,
                           git_bound=None) -> dict | None:
    dep = str(dep or "")
    target = str(target or "")
    if not (dep and target):
        return None
    pod = ""
    saw_install = False
    for nm in (Path(worktree) / "node_modules" / dep,
               Path(worktree) / "example" / "node_modules" / dep):
        if not nm.is_dir():
            continue
        saw_install = True
        name = _podspec_name(nm)
        if name:
            pod = name
            break
    if not pod:
        if saw_install:
            # Installed but podspec-less: not a native dep.
            return None
        # No install record (e.g. node_modules already cleaned):
        # best-effort package-name match, documented as such.
        pod = str(fallback_pod or "")
    if not pod:
        return None
    pins = _podfile_pins(worktree, git_bound=git_bound)
    if pod not in pins:
        return None
    pinned = str(pins[pod] or "")
    if not pinned or pinned == target:
        return None
    # M2.23: the prebuilt command (and its "pod install fails"
    # claim) is only for prebuilt-pod drift — every RN tree pins
    # prebuilt pods, so naming it for any other pod misdirects
    # the owner. Other pods get their own `pod update <pod>` in
    # the lock dir pinning them; when the tree ALSO carries RN
    # prebuilt drift, one command names the prebuilt pods first,
    # then this pod (failure claim kept).
    if pod in _PREBUILT_PODS:
        command = prebuilt_pod_update(worktree, git_bound=git_bound)
        if command:
            label = ("iOS Podfile.lock 갱신 필요 — `" + command +
                     "` (pod install만으로는 실패)")
            return {"dep": dep, "pod": pod, "pinned": pinned,
                    "target": target, "label": label,
                    "command": command}
    else:
        mixed = _mixed_pod_command(worktree, pod, git_bound=git_bound)
        if mixed:
            label = ("iOS Podfile.lock 갱신 필요 — `" + mixed +
                     "` (pod install만으로는 실패)")
            return {"dep": dep, "pod": pod, "pinned": pinned,
                    "target": target, "label": label,
                    "command": mixed}
    spot = _podfile_spot_for_pod(worktree, pod, git_bound=git_bound)
    base = f"cd {spot} && " if spot else ""
    command = f"{base}pod update {pod} --no-repo-update"
    label = f"iOS Podfile.lock 갱신 필요 — `{command}`"
    return {"dep": dep, "pod": pod, "pinned": pinned,
            "target": target, "label": label, "command": command}


def _family_member_targets(op_rec, base) -> list:
    """[(dep, target)] for family member run_ids (read-only).

    Resolves each member run_id against the integration_ops
    records under base (member op records carry their own
    dep/target). Unreadable or unresolvable members are skipped.
    Never raises."""
    out = []
    try:
        fam = (op_rec or {}).get("family") or {}
        members = fam.get("members") if isinstance(fam, dict) else None
        if not members or base is None:
            return out
        ops_dir = Path(str(base)) / "integration_ops"
        try:
            files = sorted(ops_dir.glob("*.json"))
        except OSError:
            return out
        want = {str(m) for m in members if m}
        if not want:
            return out
        for path in files:
            name = path.name
            if ".fastcheck." in name or ".prev-" in name:
                continue
            try:
                rec = json.loads(path.read_text(encoding="utf-8",
                                                errors="ignore"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            if str(rec.get("run_id") or "") not in want:
                continue
            if rec.get("dep"):
                out.append((rec.get("dep"), rec.get("target")))
    except Exception:  # noqa: BLE001 - unknown, never an error
        pass
    return out


def member_pod_drifts(worktree, op_rec, base=None, *,
                      git_bound=None) -> list:
    """M2.22: pod drift for every member of a joint/family record.

    Runs check_pod_drift for the op's own dep/target, every
    joint_candidates dep/target, every family member's dep/target
    (resolved read-only from integration_ops), plus the tree-based
    react-native check that rollup_pod_command already does (an RN
    bump whose podspec name never appears in the lock is otherwise
    invisible). Returns [drift, ...] with duplicate labels
    removed, or []. Display only — never a block, never a pod
    run. Never raises."""
    drifts = []
    seen = set()

    def _take(drift):
        if isinstance(drift, dict) and drift.get("label"):
            if drift["label"] not in seen:
                seen.add(drift["label"])
                drifts.append(drift)

    rec = op_rec if isinstance(op_rec, dict) else {}
    pairs = []
    if rec.get("dep"):
        pairs.append((rec.get("dep"), rec.get("target")))
    for cand in rec.get("joint_candidates") or []:
        if isinstance(cand, dict) and cand.get("dep"):
            pairs.append((cand.get("dep"), cand.get("target")))
    pairs.extend(_family_member_targets(rec, base))
    for dep, target in pairs:
        _take(check_pod_drift(str(worktree or ""), dep, target,
                              git_bound=git_bound))
    try:
        rn_ver = tree_react_native_version(str(worktree or ""))
    except (Exception, SystemExit):  # noqa: BLE001 - unknown
        rn_ver = ""
    if rn_ver:
        for pod in ("React-Core-prebuilt",
                    "ReactNativeDependencies"):
            _take(check_pod_drift(str(worktree or ""), "react-native",
                                  rn_ver, fallback_pod=pod,
                                  git_bound=git_bound))
    return drifts


def op_fastcheck_path(op_id: str, home=None) -> Path:
    from . import m2 as _m2
    base = paths.home() if home is None else Path(home)
    return base / "integration_ops" / (_m2.check_op_id(op_id)
                                       + ".fastcheck.json")


def slot_path(home=None) -> Path:
    base = paths.home() if home is None else Path(home)
    return base / "sched" / ".fastcheck.flock"


class _SlotBusy(Exception):
    pass


def _slot_take(home=None):
    """One fastcheck at a time, process-wide. Raises _SlotBusy."""
    import fcntl
    path = slot_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+b")
    except OSError as exc:
        raise FastcheckSkip(f"검사 슬롯을 열 수 없음: {exc}") from exc
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise _SlotBusy("다른 빠른 검사가 실행 중") from exc
    except OSError as exc:
        fh.close()
        raise FastcheckSkip(f"검사 슬롯 잠금 실패: {exc}") from exc
    return fh


def scrub_env(toolchain: list, tmp_home: str, cache_root: str) -> dict:
    """Allowlist environment for a step (C4). Values are never logged,
    only the fact that scrubbing ran."""
    path_dirs = [str(d) for d in (toolchain or []) if d]
    path_dirs.extend(d for d in SYSTEM_PATH_DIRS
                     if d not in path_dirs)
    env = {"PATH": os.pathsep.join(path_dirs),
           "LANG": os.environ.get("LANG") or "en_US.UTF-8",
           "CI": "1",
           "HOME": str(tmp_home),
           "TMPDIR": str(tmp_home),
           "YARN_ENABLE_SCRIPTS": "false",
           "YARN_ENABLE_TELEMETRY": "0",
           "HUSKY": "0",
           "npm_config_ignore_scripts": "true",
           "YARN_CACHE_FOLDER": str(Path(cache_root) / "yarn"),
           "COREPACK_HOME": str(Path(cache_root) / "corepack"),
           "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
           # M3.3a: pub shares the app-owned cache root (warm
           # installs; the gc "cache" category lists it as
           # regenerable). Analytics off. Whether analyze/test need
           # the network is decided empirically: they run
           # network-denied, and the M3.3a offline proof records it.
           "PUB_CACHE": str(Path(cache_root) / "pub"),
           "FLUTTER_SUPPRESS_ANALYTICS": "true"}
    keep_registry = os.environ.get("NPM_CONFIG_REGISTRY")
    if keep_registry:
        env["NPM_CONFIG_REGISTRY"] = keep_registry
    return env


def probe_profile_tools(profile: dict, timeout: int = 30,
                        cwd=None) -> list:
    """M3.0: --version probe of every tool a profile would run.

    Each unique steps argv[0] resolves under the scrubbed env PATH
    (profile toolchain + system dirs) and runs `[tool, --version]`
    with the scrubbed env — no secrets, no shell, 30 s timeout,
    stdin DEVNULL. Deliberately NOT sandboxed: a corepack-managed
    yarn downloads itself on first run, so the probe may fetch the
    package manager (and fails cold only if the registry is
    unreachable). The UI says exactly that.
    S16: `cwd` is the registered checkout (read-only — `--version`
    writes nothing into it; corepack downloads land in COREPACK_HOME
    only), so a project-pinned manager (e.g. yarn 4.18.0 via
    packageManager/corepack) reports its pinned version instead of
    the global one. Each result records the cwd it ran in.
    Returns [{tool, path, version, ok, error, cwd}]. A temp HOME under
    the system tmp holds the probe; it is removed afterwards.
    """
    import shutil
    import tempfile
    toolchain = [str(d) for d in (profile or {}).get("toolchain", [])
                 if d]
    names = []
    for step in (profile or {}).get("steps") or []:
        argv = (step or {}).get("argv") or []
        if argv and argv[0] not in names:
            names.append(str(argv[0]))
    tmp_home = tempfile.mkdtemp(prefix="openmainty-probe-")
    try:
        from . import paths as _paths
        cache = _paths.home() / "cache"
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        env = scrub_env(toolchain, tmp_home, str(cache))
        from pathlib import Path as _Path
        run_cwd = None
        if cwd is not None:
            try:
                cand = _Path(cwd)
                if cand.is_dir():
                    run_cwd = str(cand)
            except OSError:
                run_cwd = None
        out = []
        for name in names:
            path = shutil.which(name, path=env.get("PATH") or None)
            if not path:
                out.append({"tool": name, "path": "", "version": "",
                            "ok": False,
                            "error": "도구 없음 (toolchain + 시스템 PATH)",
                            "cwd": run_cwd or ""})
                continue
            try:
                proc = subprocess.run(
                    [path, "--version"], capture_output=True,
                    text=True, timeout=timeout, env=env,
                    stdin=subprocess.DEVNULL, cwd=run_cwd)
            except (OSError, subprocess.SubprocessError) as exc:
                out.append({"tool": name, "path": path,
                            "version": "", "ok": False,
                            "error": f"실행 실패: {exc}"[:200],
                            "cwd": run_cwd or ""})
                continue
            if proc.returncode != 0:
                out.append({"tool": name, "path": path,
                            "version": "", "ok": False,
                            "error": f"--version rc={proc.returncode}",
                            "cwd": run_cwd or ""})
                continue
            out.append({"tool": name, "path": path,
                        "version": ((proc.stdout or "").strip()
                                    .splitlines() or [""])[0][:64],
                        "ok": True, "error": "",
                        "cwd": run_cwd or ""})
        return out
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


def _tool_version(argv0: str, env: dict, cwd: str = None) -> str:
    try:
        proc = subprocess.run(
            [argv0, "--version"], capture_output=True, text=True,
            timeout=60, env=env, cwd=cwd)
        if proc.returncode == 0:
            return (proc.stdout or "").strip().splitlines()[0][:64]
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _ps_table() -> list:
    """[(pid, uid, command)] via ps. Small seam for tests."""
    try:
        proc = subprocess.run(["ps", "-axo", "pid=,uid=,command="],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if (len(parts) == 3 and parts[0].isdigit()
                and parts[1].lstrip("-").isdigit()):
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def _ppid_of(pid: int):
    """Parent pid via ps. Small seam for tests."""
    try:
        proc = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=30)
        text = (proc.stdout or "").strip().split()
        if text and text[0].isdigit():
            return int(text[0])
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _uid_of(pid: int):
    """Owner uid via ps. Small seam for tests."""
    try:
        proc = subprocess.run(["ps", "-o", "uid=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=30)
        text = (proc.stdout or "").strip().split()
        if text and text[0].lstrip("-").isdigit():
            return int(text[0])
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _parent_chain() -> set:
    """Our own ancestor pids up to init (K3: never touch them)."""
    seen = set()
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        nxt = _ppid_of(pid)
        if nxt is None or nxt == pid:
            break
        pid = nxt
    return seen


def _all_cwds(uid: int) -> dict:
    """{pid: cwd} for one uid in ONE lsof call (K4). Small seam."""
    try:
        proc = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-u", str(uid), "-F", "pn"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    out, pid = {}, None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n/") and pid is not None:
            out.setdefault(pid, line[1:])
    return out


def _cwd_of(pid: int) -> str:
    """cwd of a single pid via lsof (re-verify before kill)."""
    try:
        proc = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-F", "n"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("n/"):
            return line[1:]
    return ""


def _is_under(path: str, roots: list) -> bool:
    for root in roots:
        if not root:
            continue
        try:
            Path(str(path)).resolve().relative_to(
                Path(str(root)).resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def validate_reaper_roots(roots: list, home) -> tuple:
    """K1: every root must be absolute, resolved, deep, and inside the
    app-owned home (the op worktree under reviews/ or the per-op temp
    HOME the runner created). Returns (valid_roots, reason); reason
    "ok" or the refusal. Anything else means the reaper stays off."""
    base = Path(str(home)).resolve()
    floor = max(4, len(base.parts) + 2)
    valid = []
    for root in roots:
        if not root:
            return [], "empty root"
        candidate = Path(str(root))
        if not candidate.is_absolute():
            return [], f"not absolute: {root}"
        resolved = candidate.resolve()
        try:
            resolved.relative_to(base)
        except ValueError:
            return [], f"outside app home: {root}"
        if resolved == base or len(resolved.parts) < floor:
            return [], f"too shallow: {root}"
        valid.append(str(resolved))
    return valid, "ok"


def _argv_hit(cmd: str, roots: list) -> bool:
    """Token-boundary argv match (K2): token == root or starts with
    root + os.sep, so .../prep-x never matches .../prep-x-lockfix."""
    for tok in cmd.split():
        for root in roots:
            if not root:
                continue
            if tok == root or tok.startswith(root + os.sep):
                return True
    return False


def find_escaped(roots: list) -> dict:
    """Split processes into cwd-confirmed kill candidates and
    argv-only unverified sightings (K2: the latter are reported, never
    killed). Excludes our own pid, our parent chain, pid 1 and other
    uids (K3). Roots must already be validated; matching is token
    bounded. Returns {"kill": [...], "unverified": [...]}."""
    clean = [str(r) for r in (roots or []) if r]
    excluded = _parent_chain() | {os.getpid(), 1}
    euid = os.geteuid()
    cwds = _all_cwds(euid)
    kill, unverified = [], []
    for pid, uid, cmd in _ps_table():
        if pid in excluded or uid != euid:
            continue
        cwd = cwds.get(pid, "")
        item = {"pid": pid, "cmd": cmd[:200], "cwd": cwd}
        if cwd and _is_under(cwd, clean):
            kill.append(item)
        elif _argv_hit(cmd, clean):
            unverified.append(item)
    return {"kill": kill, "unverified": unverified}


def reap_escaped(items: list, roots: list, home) -> list:
    """SIGKILL explicit PIDs only (never by pattern). Before each kill
    the cwd is re-checked (pid-reuse guard), the pid is re-checked
    against self/parents/1, and the owner uid is re-checked (K2/K3).
    `home` is MANDATORY (H4): unsafe roots refuse everything, so no
    caller can skip the K1 validation. Returns per-pid gone flags."""
    clean = [str(r) for r in (roots or []) if r]
    valid, reason = validate_reaper_roots(roots, home)
    if reason != "ok":
        return [{"pid": int(i["pid"]),
                 "cmd": i.get("cmd", "")[:200],
                 "cwd": i.get("cwd", ""), "gone": False,
                 "note": "unsafe root: " + reason}
                for i in (items or [])]
    clean = valid
    excluded = _parent_chain() | {os.getpid(), 1}
    euid = os.geteuid()
    out = []
    for item in (items or []):
        pid = int(item["pid"])
        note = ""
        uid_now = _uid_of(pid)
        if pid in excluded:
            note = "excluded pid"
        elif uid_now is not None and uid_now != euid:
            note = "another owner's process"
        if not note:
            cwd = _cwd_of(pid)
            if not cwd or not _is_under(cwd, clean):
                note = "cwd moved or unreadable"
        if note:
            out.append({"pid": pid, "cmd": item.get("cmd", "")[:200],
                        "cwd": item.get("cwd", ""), "gone": False,
                        "note": note})
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        gone = False
        for _ in range(10):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone = True
                break
            except (PermissionError, OSError):
                break
            time.sleep(0.2)
        out.append({"pid": pid, "cmd": item.get("cmd", "")[:200],
                    "cwd": item.get("cwd", ""), "gone": gone})
    return out


def _kill_group(proc) -> bool:
    """SIGKILL the whole process group; verify nothing survives."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=10)
    except (subprocess.SubprocessError, OSError):
        pass
    for _ in range(25):
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        time.sleep(0.2)
    return False


def _sandbox_prefix(profile_text: str = SANDBOX_PROFILE,
                    sandbox_exec: str = SANDBOX_EXEC) -> list:
    return [sandbox_exec, "-p", profile_text]


def _sbq(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _git_dirs(worktree: str, *, git_bound=None) -> tuple:
    """(common_dir, git_dir), resolved absolute; ("", "") when unknown.

    Read-only `git rev-parse` calls over the hardened path, bound to
    the prep record when one is passed (re-proved before any
    spawn); never sandboxed (they only read). Prepared worktrees
    are linked checkouts of the owner's repo, so the common dir is
    the owner's .git and git_dir is this worktree's own admin dir
    under <common>/worktrees/<name>. 301: kept for diagnostics
    only — the step sandbox no longer admits the gitdir for
    writing, so this result never widens a write allowlist."""
    out = []
    bound = _fc_bound(worktree, git_bound)
    for flag in ("--git-common-dir", "--git-dir"):
        text, ok = _fc_run(worktree, ["rev-parse", flag],
                           git_bound=bound)
        if not ok:
            return ("", "")
        val = (text or "").strip().splitlines()
        val = val[0].strip() if val else ""
        if not val:
            return ("", "")
        if not os.path.isabs(val):
            val = os.path.join(str(worktree), val)
        out.append(os.path.realpath(val))
    return (out[0], out[1])


def _write_allowlist(worktree: str, env: dict,
                     toolchain=(), *, git_bound=None) -> tuple:
    """(rules, note, paths): deny-all file-write* plus an explicit
    allowlist (M2.7-SEC F2). ONLY the op worktree, the step's
    HOME/TMPDIR, the app cache dirs named in env
    (yarn / corepack / pub), and /dev may be written — never the
    owner's checkout, the shared .git common dir, the worktree's
    own git admin dir, or the real HOME.
    301: the gitdir is NOT writable. Profile steps (install /
    typecheck / lint / unit) never need git writes — git reads
    need no write allow — and a writable gitdir let a
    model-written step plant hooks/config/fsmonitor that the
    app's own later git calls would obey. Later rules win, so the
    deny comes first and the allows after.
    ([], reason, []) when the worktree is unresolvable — the caller
    then refuses rather than running uncontained.
    M2.7-SEC2: a toolchain entry that is a Flutter SDK bin dir (an
    executable `flutter` next to a `cache` dir) contributes ONLY its
    bin/cache — the tool rewrites stamps and lockfiles there on
    every command. Never the SDK root, never packages."""
    try:
        tree = os.path.realpath(str(worktree or ""))
    except OSError:
        tree = ""
    if not tree or not os.path.isdir(tree):
        return ([], "작업실 확인 불가 — 격리 없이 실행하지 않습니다",
                [])
    paths = [tree]
    env = env if isinstance(env, dict) else {}
    for key in ("HOME", "TMPDIR", "YARN_CACHE_FOLDER",
                "COREPACK_HOME", "PUB_CACHE"):
        val = env.get(key)
        if not val:
            continue
        try:
            real = os.path.realpath(str(val))
        except OSError:
            continue
        if real and real not in paths:
            paths.append(real)
    sdk_caches = []
    for entry in toolchain or ():
        try:
            tdir = os.path.realpath(str(entry or ""))
        except OSError:
            continue
        if not tdir or not os.path.isdir(tdir):
            continue
        exe = os.path.join(tdir, "flutter")
        cache_dir = os.path.join(tdir, "cache")
        if os.path.isfile(exe) and os.access(exe, os.X_OK) \
                and os.path.isdir(cache_dir):
            real_cache = os.path.realpath(cache_dir)
            if real_cache and real_cache not in paths:
                paths.append(real_cache)
                sdk_caches.append(real_cache)
    rules = ["(deny file-write*)"]
    for path in paths:
        rules.append("(allow file-write* (subpath %s))" % _sbq(path))
    rules.append("(allow file-write* (subpath \"/dev\"))")
    note = "쓰기 허용 %d곳(작업실)" % len(paths)
    if sdk_caches:
        note += " · 공유 Flutter SDK 캐시 쓰기 허용"
    return (rules, note, paths)


def run_step(argv: list, worktree: str, env: dict, timeout_s: int,
             install: bool, log_path: Path,
             sandbox_exec: str = SANDBOX_EXEC,
             loopback: bool = False, toolchain=(),
             cwd: str = "", git_bound=None) -> dict:
    """Run one profile step. Returns the step receipt (never raises
    for step outcomes; spawn failures raise FastcheckBlocked).

    Every step runs under sandbox-exec with deny-all file-write*
    plus an explicit allowlist (M2.7-SEC F2): only the op worktree,
    the step HOME/TMPDIR, the app cache dirs and /dev may be
    written — never the worktree's own git admin dir (301:
    model-written steps must not be able to plant hooks/config/
    fsmonitor the app's later git calls would obey). Install steps
    keep the network allowed
    so the registry fetch works; other steps add the network deny
    (unix-domain IPC allowed, TCP/DNS denied), except a
    `flutter test` step with loopback=True, which gets the
    loopback-only profile (loopback TCP allowed, remote TCP and DNS
    denied) because the tester binds a loopback HttpServer. loopback
    for any other argv falls back to strict — the receipt profile
    name always says which ran. A missing sandbox-exec, or an
    unresolvable worktree, raises FastcheckSkip/FastcheckBlocked
    rather than running uncontained. Timeouts kill the process
    group and verify it. Logs are bounded at LOG_CAP_BYTES."""
    if not (sandbox_exec and os.path.isfile(sandbox_exec)):
        raise FastcheckSkip("sandbox-exec 없음 — 격리 없이 "
                            "실행하지 않습니다")
    file_rules, file_note, allow_paths = _write_allowlist(
        worktree, env, toolchain, git_bound=git_bound)
    if not file_rules:
        raise FastcheckBlocked(file_note)
    loop = bool(loopback) and not install and _allows_loopback(argv)
    if install:
        profile_text = SANDBOX_BASE_PROFILE + "".join(file_rules)
        profile_name = "files"
        sandbox_value = False
    elif loop:
        profile_text = SANDBOX_LOOPBACK_PROFILE + "".join(file_rules)
        profile_name = "files+network-loopback"
        sandbox_value = "loopback-only"
    else:
        profile_text = SANDBOX_PROFILE + "".join(file_rules)
        profile_name = "files+network"
        sandbox_value = "strict"
    final_argv = (_sandbox_prefix(profile_text, sandbox_exec)
                   + list(argv))
    if install and list(argv)[:1] in (["npm"], ["pnpm"]):
        final_argv = final_argv + ["--ignore-scripts"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    # Output goes straight to the log FILE, never a pipe: a
    # double-forked escapee inherits the fd, and a pipe would make the
    # wait below hang until the escapee exits. wait() returns when the
    # direct child exits regardless of inherited fds. `cwd` overrides
    # only the child's directory (M2.15 consumer: a workspace subdir
    # of the scratch clone); the write allowlist stays rooted at the
    # worktree.
    run_cwd = str(cwd or worktree)
    try:
        log_fh = open(log_path, "w", encoding="utf-8")
    except OSError as exc:
        raise FastcheckBlocked(f"검사 기록을 열 수 없음: {exc}") from exc
    try:
        proc = subprocess.Popen(
            final_argv, cwd=run_cwd, env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True, text=True)
    except OSError as exc:
        log_fh.close()
        raise FastcheckBlocked(f"검사 실행 실패 "
                               f"({' '.join(argv[:2])}): {exc}") from exc
    try:
        try:
            proc.wait(timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        try:
            log_fh.close()
        except OSError:
            pass
        children_gone = True
        if proc.poll() is None:
            children_gone = False
        if timed_out or proc.poll() is None:
            children_gone = _kill_group(proc)
    rc = proc.returncode
    try:
        raw = open(log_path, "rb").read()
    except OSError:
        raw = b""
    truncated = False
    if len(raw) > LOG_CAP_BYTES:
        # Bound the stored log: head plus a marker plus the tail.
        raw = (raw[:LOG_CAP_BYTES] +
               b"\n...[truncated]\n" + raw[-65536:])
        try:
            open(log_path, "wb").write(raw)
        except OSError:
            pass
        truncated = True
    log_text = raw.decode("utf-8", "ignore")
    written = len(raw)
    tail = log_text[-2000:]
    digest = hashlib.sha256(raw).hexdigest()
    return {"argv": list(argv), "sandboxed_argv": final_argv != argv,
            "rc": rc, "timed_out": timed_out,
            "elapsed_s": round(time.time() - started, 1),
            "timeout_s": timeout_s, "install": bool(install),
            "network_sandbox": sandbox_value,
            "sandbox_profile": profile_name,
            "file_sandbox": file_note,
            "write_allow": sorted(allow_paths),
            "scripts_off": True,
            "log": str(log_path), "log_sha256": digest,
            "log_bytes": written, "log_truncated": truncated,
            "log_tail": tail,
            "children_verified_gone": children_gone,
            "escaped_processes": [],
            "escaped_unverified": [],
            "reaper": "ok"}


def _dir_bytes(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(str(path)):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _receipt_sha(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _update_op_gate(home: Path, op_id: str, ok: bool, note: str,
                    attested_by: str = "worker",
                    receipt_sha: str = "", steps: list = None) -> None:
    """Mark the prepare op's fastchecks gate (H1: who attested plus
    the receipt sha). Receipts live outside the worktree, so cleanup
    can never touch them. Also stores a compact fastcheck_summary
    (per-step install + network-sandbox flags) so the UI can say
    honestly how each step ran without serving the whole receipt."""
    from .util import read_json
    opf = home / "integration_ops" / (op_id + ".json")
    try:
        rec = read_json(opf, default=None)
    except Exception:  # noqa: BLE001 - a lost op never fails the run
        return
    if not isinstance(rec, dict):
        return
    for gate in rec.get("gates") or []:
        if isinstance(gate, dict) and gate.get("key") == "fastchecks":
            gate["status"] = "pass" if ok else "fail"
            gate["note"] = note[:300]
            gate["attested_by"] = attested_by
            if receipt_sha:
                gate["receipt_sha256"] = receipt_sha
    if isinstance(steps, list):
        rec["fastcheck_summary"] = {
            "ok": bool(ok), "at": utcnow(),
            "steps": [{"name": str((s or {}).get("name") or ""),
                       "gate": str((s or {}).get("gate") or ""),
                       "install": bool((s or {}).get("install", False)),
                       "network_sandbox": sandbox_mode(s),
                       "rc": (s or {}).get("rc")}
                      for s in steps if isinstance(s, dict)][:32]}
    rec["updated_at"] = utcnow()
    try:
        write_json(opf, rec)
    except OSError:
        pass


def run(op_id: str, worktree: str, profile: dict, head: str = "",
        fingerprint: str = "", home=None, _disk_avail=None,
        _sandbox_exec: str = SANDBOX_EXEC, origin=None,
        git_bound=None) -> dict:
    """Run the profile's fastcheck steps in the op worktree.

    Holds the slot and the op exclusion lock for the whole run; never
    takes the global lease. Returns the summary (ok False on the first
    red step — the driver blocks the lane with its log tail).
    Raises FastcheckSkip (transient) or FastcheckBlocked (needs the
    owner). Writes the receipt + logs under integration_ops, updates
    the op's fastchecks gate, then removes only node_modules and the
    per-op temp HOME. `origin` stamps who started it ("ui" for the
    owner button); the driver passes nothing. Never root_attested —
    a worker receipt stays worker-attested until Root QA lands.
    301: the prep record is taken at entry (before any step runs
    model-written code) and every step + internal git read is bound
    to it; it is returned as summary["git"] so post-step callers
    reuse the pre-step record instead of recording fresh."""
    from . import m2 as _m2
    if os.name != "posix":
        raise FastcheckBlocked("빠른 검사는 POSIX에서만 실행됩니다")
    safe_op = _m2.check_op_id(op_id)
    base = paths.home() if home is None else Path(home)
    steps = (profile or {}).get("steps") or []
    if not steps:
        raise FastcheckBlocked("fastcheck_steps 없음")
    toolchain = [str(d) for d in (profile or {}).get("toolchain", [])
                 if d]
    disk_avail = (_disk_avail or _disk_avail_bytes)(base)
    if disk_avail < MIN_DISK_BYTES:
        have_gb = round(disk_avail / 1024 ** 3, 1)
        raise FastcheckBlocked(
            f"디스크 여유 {have_gb}GB — 30GB 필요 "
            f"(빠른 검사가 node_modules를 내려받습니다)")
    cache_root = base / "cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    wall_start = time.time()
    tmp_home = op_tmp_home(base, safe_op)
    if len(str(tmp_home)) + _TMP_SPARE > _UNIX_PATH_MAX:
        raise FastcheckBlocked(
            "임시 경로가 김(유닉스 소켓 경로 제한 초과) — "
            f"{len(str(tmp_home)) + _TMP_SPARE}자 "
            f"(op {safe_op[:12]}…, pid {os.getpid()}); "
            "실행하지 않았으니 경로를 줄인 뒤 다시 시도하세요")
    try:
        tmp_home.mkdir(parents=True, exist_ok=True)
        (tmp_home / "op.txt").write_text(safe_op + "\n",
                                         encoding="utf-8")
    except OSError as exc:
        raise FastcheckBlocked(
            f"임시 작업실을 만들 수 없음: {exc}") from exc
    env = scrub_env(toolchain, str(tmp_home), str(cache_root))
    # 301: prep record before any step runs model-written code on
    # this tree; every step sandbox + internal git read below is
    # bound to it.
    bound = _fc_bound(str(worktree), git_bound)
    node_v = _tool_version("node", env, str(worktree))
    manager_v = ""
    summary = {"op": safe_op, "ok": True, "steps": [], "failed": None,
               "origin": origin, "git": bound,
               "disk_avail_gb": round(disk_avail / 1024 ** 3, 1),
               "disk_measure": "statvfs f_bavail (conservative)",
               "cache": {"root": str(cache_root),
                         "regenerable": True,
                         "checksums": "enforced (default)"},
               "registry_host": env.get("NPM_CONFIG_REGISTRY",
                                        "default"),
               "worker_attested": True, "root_attested": False,
               "attestor": "worker", "at": utcnow()}
    try:
        slot = _slot_take(base)
    except _SlotBusy as exc:
        raise FastcheckSkip(str(exc)) from exc
    try:
        with _m2.op_locked(safe_op, base):
            for pos, step in enumerate(steps):
                argv = list(step.get("argv") or [])
                install = bool(step.get("install", False))
                timeout = step.get("timeout_s") or (
                    1200 if install else 600)
                if pos == 0:
                    manager_v = _tool_version(argv[0], env,
                                              str(worktree))
                log_path = (base / "integration_ops" /
                            f"{safe_op}.fastcheck.{pos}.log")
                receipt = run_step(
                    argv, worktree, env, timeout, install, log_path,
                    sandbox_exec=_sandbox_exec,
                    loopback=(step.get("sandbox") == "loopback"),
                    toolchain=toolchain, git_bound=bound)
                # M2.5d: a step reads as its gate key + script
                # ("unit (test: jest)"), never bare argv ("yarn").
                receipt["gate"] = str(step.get("gate") or "")
                try:
                    _resolved = _gate_script(str(worktree), argv)
                except Exception:  # noqa: BLE001 - label falls back
                    _resolved = None
                receipt["name"] = str(
                    step.get("name")
                    or _step_label(step, _resolved))
                receipt["head"] = head
                receipt["fingerprint"] = fingerprint
                receipt["escaped_unverified"] = []
                valid_roots, why = validate_reaper_roots(
                    [worktree, str(tmp_home)], base)
                if why != "ok":
                    receipt["reaper"] = "disabled: " + why
                else:
                    receipt["reaper"] = "ok"
                    found = find_escaped(valid_roots)
                    receipt["escaped_unverified"] = found["unverified"]
                    if found["kill"]:
                        receipt["escaped_processes"] = reap_escaped(
                            found["kill"], valid_roots, base)
                summary["steps"].append(receipt)
                if receipt["rc"] != 0 or receipt["timed_out"]:
                    summary["ok"] = False
                    summary["failed"] = {
                        "pos": pos, "argv": argv,
                        "rc": receipt["rc"],
                        "timed_out": receipt["timed_out"],
                        "log_tail": receipt["log_tail"]}
                    break
    except (FastcheckSkip, FastcheckBlocked):
        # No receipts yet: drop the per-op temp HOME so a skip or an
        # early refusal leaves no weight behind, then re-raise.
        shutil.rmtree(tmp_home, ignore_errors=True)
        raise
    finally:
        try:
            slot.close()
        except Exception:  # noqa: BLE001 - slot release never fails
            pass
    # M2.5a: the exercised census runs here — after the steps, while
    # node_modules is still on disk (cleanup below removes it), and
    # before the receipt lands. Read-only; unknown on anything
    # unreadable, never a block.
    _op_rec = None
    try:
        from .util import read_json as _read_json
        _op_rec = _read_json(
            base / "integration_ops" / (safe_op + ".json"),
            default=None)
        _dep = (_op_rec or {}).get("dep") \
            if isinstance(_op_rec, dict) else None
        _target = (_op_rec or {}).get("target") \
            if isinstance(_op_rec, dict) else None
        # FIX-a2: the census reads EXECUTED step receipts only —
        # a planned step that never ran (a red step stopped the
        # run first) must not mark its dep direct or via-tool.
        summary["exercised"] = check_exercised(
            str(worktree), _dep, summary.get("steps"))
        # M2.5c: the native lock drift census rides the same
        # worktree read (node_modules still on disk here).
        # M2.22: every member (joint candidates, family), plus
        # the tree-based react-native check — a joint whose own
        # dep is expo would otherwise miss an RN drift. The first
        # labelled drift stays pod_drift (today's card shape).
        drifts = member_pod_drifts(str(worktree), _op_rec, base)
        summary["pod_drifts"] = drifts
        summary["pod_drift"] = drifts[0] if drifts else None
    except Exception:  # noqa: BLE001 - unknown, never an error
        summary["exercised"] = check_exercised(str(worktree), None,
                                               steps)
        summary["pod_drift"] = None
        summary["pod_drifts"] = []
    # M2.15: the consumer build step runs here — same window (the
    # clone below includes the installed node_modules), same
    # sandbox, before the receipt lands. A pass appends the
    # "consumer" gate receipt; a red build or a changed derived-tree
    # fingerprint fails the run (red, never batch); a clone failure
    # is an honest skip (never a pass). None when nothing
    # qualifies — the census wording then stays as today.
    try:
        cons = run_consumer_gate(
            str(worktree), _op_rec, steps,
            summary.get("steps"), env, base, safe_op,
            str(tmp_home), toolchain)
    except Exception:  # noqa: BLE001 - skip, never a block
        cons = {"status": "skip", "script": "",
                "reason": "소비 검사 오류"}
    if cons is not None:
        summary = apply_consumer_result(summary, cons)
    summary["node_version"] = node_v
    summary["manager_version"] = manager_v
    summary["toolchain"] = toolchain
    summary["wall_s"] = round(time.time() - wall_start, 1)
    try:
        write_json(op_fastcheck_path(safe_op, base), summary)
    except OSError as exc:
        raise FastcheckBlocked(f"검사 기록 저장 실패: {exc}") from exc
    # Cleanup before the final receipt write: only regenerable
    # weight — the worktree's node_modules and the per-op temp HOME.
    # Receipts, logs, diffs and sources are never touched.
    freed = 0
    nm = Path(worktree) / "node_modules"
    if nm.is_dir() and not nm.is_symlink():
        freed += _dir_bytes(nm)
        shutil.rmtree(nm, ignore_errors=True)
    freed += _dir_bytes(tmp_home)
    shutil.rmtree(tmp_home, ignore_errors=True)
    summary["cleanup"] = {"removed": ["<worktree>/node_modules",
                                      "<tmp-home>"],
                          "freed_bytes": freed,
                          "receipts_kept": True}
    try:
        write_json(op_fastcheck_path(safe_op, base), summary)
    except OSError:
        pass
    # H1 last: the gate's receipt sha matches the final bytes.
    receipt_sha = _receipt_sha(op_fastcheck_path(safe_op, base))
    if summary["ok"]:
        _update_op_gate(base, safe_op, True, "빠른 검사 전부 통과",
                        receipt_sha=receipt_sha,
                        steps=summary.get("steps"))
    else:
        failed = summary["failed"] or {}
        _update_op_gate(
            base, safe_op, False,
            f"{' '.join(failed.get('argv', [])[:2])} 실패 "
            f"(rc={failed.get('rc')})", receipt_sha=receipt_sha,
            steps=summary.get("steps"))
    return summary
