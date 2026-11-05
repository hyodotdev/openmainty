"""Muse worker wrapper.

Primary path: subscription login (`muse login`) + `muse exec` with
`--provider meta --model muse-spark-1.3 --contributor-mode on`.
No model API is called directly and no API key is required; when
META_API_KEY is set in the environment it takes priority over the login
(report only its presence, never its value).

Usage honesty: the `--json` event stream exposes model id, session id,
stream byte counts and request/response ids, but no token counts.
Record exactly those; tokens are stored as {"tokens_measurable": false}.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pwd
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time

from . import __version__
from .util import run_cmd

DEFAULT_MODEL = "muse-spark-1.3"
# Back-compat alias: existing callers/tests reference muse.MODEL.
MODEL = DEFAULT_MODEL

# Pinned installed-CLI identity for the DEDICATED app-owned source-repair
# profile (Stage2B preflight + target-A worker). The updating PATH
# launcher (`muse`) is NEVER executed for this profile: the pinned
# absolute binary below is re-resolved and re-hashed at every launch,
# and any byte drift refuses with a typed error. Recorded from the
# reviewed installed binary; update only with a new reviewed pin.
# Content identity is what matters: the candidate itself must be a
# real file (a symlink/launcher candidate refuses outright), and
# every resolution records real path, full SHA256, device, inode,
# and size. PINNED_MUSE_PATH below is a RECORD of the last reviewed
# DIRECT install binary only — it is never used as a silent
# default: the live path is configured explicitly per machine via
# OPENMAINTY_MUSE_BINARY (explicit, portable; unset refuses with an
# actionable error instead of guessing). Reviewed record: direct
# install binary muse-bin-1.4.14-R7309.1 (the PATH `muse` launcher
# on this machine is a small updater script with a different hash
# and can never match the pin). Never copy the binary or change
# user pins/settings to satisfy a check.
PINNED_MUSE_PATH = "/Users/crossplatformkorea/.local/bin/muse-bin-1.4.14-R7309.1"
PINNED_MUSE_SHA256 = (
    "1c96114408acbeade9a8fb79e3b4425e5b64d50425938568aef01bdc04ce2e03"
)
MUSE_BINARY_ENV = "OPENMAINTY_MUSE_BINARY"
_DEDICATED_STREAM_MAX_BYTES = 4 * 1024 * 1024

# The ONE command contract for this profile: explicit
# meta/muse-spark-1.3/contributor-ON, exactly one network mode
# (`restricted`), approval never with the judge off, no interactive
# input, and the CONFIRMED CLI step/output bounds below (help
# surface: --max-model-steps "Cap the number of steps",
# --max-tool-output-bytes "Cap tool output bytes fed back to the
# model", 0 or >=9). No other bounding flags are invented; app
# timeout + stream caps + owned process-group reaping add the
# runtime side. Raw flags below are REQUESTED state, not observed
# enforcement; effective file-only/MCP/hooks/delegation proof is a
# separate blocking gate (preflight channels stay UNKNOWN until
# genuinely observed).
SOURCE_REPAIR_PROFILE = {
    "profile": "source-repair",
    "provider": "meta",
    "model": "muse-spark-1.3",
    "contributor_mode": True,
    "approval_mode": "never",
    "approval_judge": "off",
    "network_mode": "restricted",
    "disable_request_user_input": True,
    "trust_workspace": True,
    "max_model_steps": 25,
    "max_tool_output_bytes": 200000,
}

# Explicit minimal child environment for the dedicated profile (no
# ambient credential/config inheritance: only PATH/TMPDIR/HOME plus
# locale/timezone display variables pass through, then the per-run
# toolchain cache dirs; everything else must be passed explicitly).
# META_API_KEY is never copied here — its presence in the final
# child env refuses the spawn (stored keys take priority over
# subscription login).
_DEDICATED_PASSTHROUGH_ENV = ("PATH", "TMPDIR", "HOME", "LANG",
                              "LC_ALL", "TZ", "TERM")


_PINNED_BINARY_MAX_BYTES = 1 << 30


def _snapshot_binary(path: str) -> dict:
    """Snapshot a direct binary: real path, full SHA256, dev/ino/size.

    The candidate itself must be a real file: symlinks (including the
    updating PATH launcher) refuse outright instead of resolving.
    Opens with O_NOFOLLOW | O_NONBLOCK (a planted FIFO can never
    block the open: FIFOs fail the regular-file proof next),
    proves regular-file identity with fstat BEFORE reading, hashes
    with bounded total reads, and re-checks identity after the read
    — closing timestamps plus a name re-resolution — so a swapped
    file cannot pass as the pinned bytes. Raises OSError on any
    failure.
    """
    try:
        lst = os.lstat(path)
    except OSError as exc:
        raise OSError(f"pinned muse missing: {path}: {exc}") from exc
    if stat.S_ISLNK(lst.st_mode):
        raise OSError(f"pinned muse must be a direct binary, not a "
                      f"symlink/launcher: {path}")
    try:
        fd = os.open(path,
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise OSError(f"pinned muse open failed: {path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"pinned muse not a regular file: {path}")
        if before.st_size < 0 or before.st_size > \
                _PINNED_BINARY_MAX_BYTES:
            raise OSError(f"pinned muse size out of bounds: {path}: "
                          f"{before.st_size}")
        digest = hashlib.sha256()
        remaining = before.st_size + 1
        while remaining > 0:
            try:
                part = os.read(fd, min(65536, remaining))
            except OSError as exc:
                raise OSError(f"pinned muse bytes unreadable: {path}: "
                              f"{exc}") from exc
            if not part:
                break
            digest.update(part)
            remaining -= len(part)
        else:
            raise OSError(f"pinned muse larger than bound: {path}")
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns) != (
                before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns):
            raise OSError(f"pinned muse changed during read: {path}")
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise OSError(f"pinned muse name no longer resolves: "
                          f"{path}: {exc}") from exc
        if (current.st_dev, current.st_ino) != (
                before.st_dev, before.st_ino):
            raise OSError(f"pinned muse replaced during read: {path}")
        real = os.path.realpath(path)
        return {"path": path, "real_path": real,
                "sha256": digest.hexdigest(),
                "dev": before.st_dev, "ino": before.st_ino,
                "size": before.st_size}
    finally:
        os.close(fd)


def _same_binary_snapshot(first: dict, second: dict) -> bool:
    return (
        first.get("real_path") == second.get("real_path")
        and first.get("sha256") == second.get("sha256")
        and first.get("dev") == second.get("dev")
        and first.get("ino") == second.get("ino")
        and first.get("size") == second.get("size"))


def resolve_pinned_muse(candidate=None):
    """Resolve the pinned installed binary, proving executable identity.

    `candidate` or the OPENMAINTY_MUSE_BINARY environment names the
    direct binary; there is no PATH-launcher fallback. Returns the
    full snapshot (path, real_path, sha256, dev, ino, size). Raises
    OSError on any failure: unconfigured path, symlink/launcher
    candidate, non-regular target, unreadable bytes, mid-read change,
    or content drift versus PINNED_MUSE_SHA256. Callers re-snapshot
    immediately before spawn and refuse on any change.
    """
    path = candidate or os.environ.get(MUSE_BINARY_ENV) or ""
    if not path:
        raise OSError(
            "no direct muse binary configured: set "
            f"{MUSE_BINARY_ENV} to the reviewed direct install binary "
            "(the PATH launcher is never used for this profile)")
    snap = _snapshot_binary(path)
    if snap["sha256"] != PINNED_MUSE_SHA256:
        raise OSError(
            f"pinned muse content drift: {snap['real_path']} sha256 "
            f"{snap['sha256']} != pinned {PINNED_MUSE_SHA256}; re-pin "
            f"only with a reviewed binary")
    return snap


def source_repair_fingerprint(identity: dict) -> str:
    """Full canonical identity for the dedicated profile (no execution).

    Covers the whole authority surface: pinned binary SHA + resolved
    path, exact managed profile flags, FULL config identity (status
    and files — a status-only key would reuse history across a
    config change), FULL source identity (version AND managed file
    SHAs), child-env key shape, and auth facts (key presence +
    credential source). Returns the FULL 64-hex digest — display
    sites may show a prefix, but only the full digest is
    authoritative. Identity data is an input to matching, never a
    trusted observed verdict.
    """
    if not isinstance(identity, dict):
        raise ValueError("source_repair_fingerprint needs an identity dict")
    binary = identity.get("binary") or {}
    config = identity.get("config") or {}
    source = identity.get("source") or {}
    env = identity.get("env") or {}
    auth = identity.get("auth") or {}
    canon = json.dumps({
        "binary_sha256": str(binary.get("sha256") or ""),
        "binary_real_path": str(binary.get("real_path") or ""),
        "profile": {key: SOURCE_REPAIR_PROFILE[key]
                    for key in sorted(SOURCE_REPAIR_PROFILE)},
        "config": {"status": str(config.get("status") or ""),
                   "files": list(config.get("files") or [])},
        "source": {"app_version": str(source.get("app_version") or ""),
                   "files": list(source.get("files") or [])},
        "env": {"passthrough_keys": list(
            env.get("passthrough_keys") or []),
            "cache_keys": list(env.get("cache_keys") or [])},
        "auth": {"meta_api_key_present": bool(
            auth.get("meta_api_key_present")),
            # Legacy (key absent) digests as null, never as False:
            # absent != False, so a legacy identity can never
            # share a digest with a current one.
            "contributor_env_present": (
                bool(auth["contributor_env_present"])
                if "contributor_env_present" in auth else None),
            "credential_source": str(auth.get("credential_source")
                                     or "unknown")},
    }, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def pinned_fingerprint():
    """Display-only short prefix (full digest is authoritative)."""
    return "muse:%s:model=%s:contrib=on:net=%s" % (
        PINNED_MUSE_SHA256[:16], SOURCE_REPAIR_PROFILE["model"],
        SOURCE_REPAIR_PROFILE["network_mode"])


def resolve_model(name=None) -> str:
    """Return the explicit model or the default. No catalog validation."""
    if isinstance(name, str) and name.strip():
        return name.strip()
    return DEFAULT_MODEL


def which() -> str | None:
    return shutil.which("muse")


def version() -> str | None:
    path = which()
    if not path:
        return None
    code, out, _ = run_cmd([path, "--version"])
    if code != 0:
        return None
    return out.strip().splitlines()[0] if out.strip() else "unknown"


def billing_path() -> str:
    """Which billing path would `muse exec` take? Name only, no values."""
    if os.environ.get("META_API_KEY"):
        return "api-key (META_API_KEY is set; takes priority over login)"
    return "subscription login (no META_API_KEY in environment)"


def build_cmd(prompt: str, workdir=None, contributor: bool = True, extra=None, model=None) -> list:
    # Headless-service invocation: the workspace is the run-owned worktree
    # (created and isolated by runner.ensure_branch_workdir), so trust it
    # explicitly, and never block on interactive approval in a service
    # context. This does not widen permissions: prompts still forbid posts,
    # merges, comments, and any destination outside the working directory.
    cmd = [
        "muse", "exec",
        "--provider", "meta",
        "--model", resolve_model(model),
        "--contributor-mode", "on" if contributor else "off",
        "--disable-request-user-input",
        "--approval-mode", "never",
        "--trust-workspace",
        # Dependency/gh networking (npm/yarn registry, gh API) needs direct
        # egress: the CLI default is proxy-only, under which worker node DNS
        # fails (ENOTFOUND) while the host is healthy (run-34e91bec exit124).
        # This keeps the filesystem sandbox ON; only network mode is opened.
        "--sandbox-network", "enabled",
        "--json",
    ]
    if workdir:
        cmd += ["--workspace", str(workdir)]
    if extra:
        cmd += list(extra)
    cmd.append(prompt)
    return cmd


CACHE_DIR_NAME = ".openmainty-cache"


def worker_cache_env(workdir=None) -> dict:
    """Per-run writable JS toolchain cache locations (machine-independent).

    Worker shells run under the managed filesystem sandbox: the user's home
    caches (~/.npm, ~/.yarn/berry, ~/.cache/node/corepack) are NOT writable
    there, so default `yarn add`/`npm install`/corepack fail with EPERM even
    with healthy networking. Point the toolchain at per-run dirs rooted at
    the run-owned workdir (TMPDIR fallback when no workdir is given) instead
    of committing machine-local overrides into repos. Values are paths only.

    The per-run dirs nest under a `node_modules` segment inside the
    reserved `.openmainty-cache` prefix (run-71c1de11 follow-up): ESLint's
    default traversal ignores `node_modules` at any depth, so downloaded
    toolchain JS (corepack yarn shims) can never surface as lint errors,
    while the reserved prefix keeps fingerprint/commit-noise isolation.
    The toolchain dirs themselves must NOT live under the target repo's
    real `node_modules`: `yarn install` manages that tree and would delete
    its own active package manager.
    """
    if workdir:
        base = os.path.join(str(workdir), CACHE_DIR_NAME, "node_modules")
    else:
        base = os.path.join(tempfile.gettempdir(), "openmainty-cache", "node_modules")
    return {
        "COREPACK_HOME": os.path.join(base, "corepack"),
        "YARN_CACHE_FOLDER": os.path.join(base, "yarn-cache"),
        "YARN_GLOBAL_FOLDER": os.path.join(base, "yarn-global"),
        "YARN_ENABLE_GLOBAL_CACHE": "false",
        "npm_config_cache": os.path.join(base, "npm-cache"),
    }


def parse_usage(jsonl_text: str) -> dict:
    """Extract only measurable fields from an exec --json stream."""
    model_id = None
    provider_model = None
    session_id = None
    request_id = None
    response_id = None
    stream_bytes = len(jsonl_text.encode("utf-8"))
    for line in jsonl_text.splitlines():
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        payload = evt.get("payload", {}) if isinstance(evt, dict) else {}
        if not isinstance(payload, dict):
            continue
        if payload.get("kind") == "run_model_configured":
            model_id = payload.get("model_id", model_id)
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        details = payload.get("details") or event.get("details") or {}
        if isinstance(details, dict):
            for facet in details.get("facets", []) or []:
                if not isinstance(facet, dict):
                    continue
                detail = facet.get("detail", {}) or {}
                if isinstance(detail, dict):
                    if detail.get("request_id"):
                        request_id = detail["request_id"]
                    if detail.get("response_id"):
                        response_id = detail["response_id"]
                    if detail.get("model"):
                        provider_model = detail["model"]
        stream = evt.get("stream", {}) if isinstance(evt, dict) else {}
        if isinstance(stream, dict) and stream.get("kind") == "session" and not session_id:
            session_id = stream.get("id")
    usage = {
        "model": model_id,
        "provider_model": provider_model,
        "contributor_applied": (provider_model or "").endswith("-contributor"),
        "session_id": session_id,
        "stream_bytes": stream_bytes,
        "tokens_measurable": False,
        "tokens": None,
    }
    if request_id:
        usage["request_id"] = request_id
    if response_id:
        usage["response_id"] = response_id
    return usage


def parse_delta_line(line: str):
    """Per-line parse of an exec --json stream. Returns (kind, text).

    kind is delta (incremental output chunk), terminal (final full text),
    or none. Pure function: safe to call on every streamed line.
    """
    line = line.strip()
    if not line.startswith("{"):
        return "none", ""
    try:
        evt = json.loads(line)
    except ValueError:
        return "none", ""
    if not isinstance(evt, dict):
        return "none", ""
    payload = evt.get("payload", {})
    if not isinstance(payload, dict):
        return "none", ""
    kind = payload.get("kind")
    if kind == "run_output_delta":
        text = payload.get("text", "")
        return ("delta", text) if isinstance(text, str) and text else ("none", "")
    if kind == "run_terminal":
        text = payload.get("text", "")
        return ("terminal", text) if isinstance(text, str) else ("none", "")
    return "none", ""


def exec_prompt(prompt: str, workdir=None, contributor: bool = True, timeout: int = 1200,
                model=None, cancel_event=None, extra=None, env=None):
    """Run one headless Muse step. Returns (exit, output_text, usage).

    Cancelled propagates (worker killed); the caller records resumable state.
    `extra` appends CLI flags (e.g. read-only sandbox flags for review
    jobs). `env`, when given, replaces os.environ as the base mapping
    (callers use it to scrub billing keys); the worker cache env is
    still layered on top. Neither changes the default call path.
    """
    from .util import _communicate
    path = which()
    if not path:
        return 127, "", {"error": "muse CLI not found"}
    cmd = build_cmd(prompt, workdir=workdir, contributor=contributor,
                    model=model, extra=extra)
    env = dict(env) if env is not None else dict(os.environ)
    env.update(worker_cache_env(workdir))
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(workdir) if workdir else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", {"error": "muse CLI not found"}
    try:
        out, _ = _communicate(proc, timeout, cancel_event)
    except subprocess.TimeoutExpired:
        return 124, "", {"timeout_s": timeout, "tokens_measurable": False, "tokens": None}
    usage = parse_usage(out)
    usage["exit_code"] = proc.returncode
    usage["cli_version"] = version()
    # Human-readable tail is stdout's last run_output_delta text; the raw
    # stream is large, so callers persist a trimmed log separately.
    return proc.returncode, out, usage


def agent_info(model=None) -> dict:
    return {
        "openmainty": __version__,
        "muse": version(),
        "model": resolve_model(model),
        "contributor_mode": True,
        "billing": billing_path(),
    }


def build_source_repair_cmd(prompt: str, workdir, output_schema=None) -> list:
    """Argv for the DEDICATED source-repair profile (restricted network).

    `workdir` (the isolated fixture/preview workspace) is REQUIRED:
    this profile never runs without an explicit workspace. The ONLY
    accepted shaping parameter is `output_schema`: an absolute
    `--output-schema <file>.json` path constraining the response
    shape (never its truth). There is deliberately NO arbitrary
    `extra` passthrough at this safe boundary: equals-form aliases,
    unsafe flags (sandbox/yolo bypasses, key material via stdin,
    alternate endpoints, session reuse), unknown options, and
    duplicates cannot be expressed. A prompt that starts with "-"
    is refused outright (no `--` separator is assumed: parser
    support for a positional boundary is unproved, so an
    option-looking prompt is a ValueError, never a guessed flag).
    The model is pinned to
    muse-spark-1.3 contributor-ON; there is no model parameter and no
    API-key path. Returned argv uses the literal "muse" argv[0];
    callers that execute must substitute the resolved pinned binary
    path (see exec_source_repair). Step/output bounds ride on the
    CONFIRMED CLI flags below plus app timeout/stream caps — no
    invented flags. Adds the supported file-only flags
    `--disable-shell`, `--disable-web-tools`, `--disable-reminders`,
    and `--no-foreign-personal-context` alongside the existing
    request-input/approval guards.
    """
    if not workdir:
        raise ValueError("source-repair profile requires an explicit workdir")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("source-repair profile requires a prompt string")
    if prompt.startswith("-"):
        raise ValueError(
            "refusing option-like prompt: prompts must not start "
            "with '-' (option parsing is unproved; pass prose, not "
            "CLI flags)")
    if output_schema is not None:
        if not isinstance(output_schema, str) or not output_schema:
            raise ValueError("output_schema must be a schema path string")
        if not output_schema.startswith("/") \
                or ".." in output_schema.split("/") \
                or not output_schema.endswith(".json"):
            raise ValueError(
                "output_schema must be an absolute owned .json path "
                "without parent traversal")
    cmd = [
        "muse", "exec",
        "--provider", SOURCE_REPAIR_PROFILE["provider"],
        "--model", SOURCE_REPAIR_PROFILE["model"],
        "--contributor-mode", "on",
        "--disable-shell",
        "--disable-web-tools",
        "--disable-reminders",
        "--no-foreign-personal-context",
        "--disable-request-user-input",
        "--approval-mode", SOURCE_REPAIR_PROFILE["approval_mode"],
        "--approval-judge", SOURCE_REPAIR_PROFILE["approval_judge"],
        "--trust-workspace",
        # Exactly ONE network mode for this profile: restricted. The
        # older dependency-worker default (`enabled`) is NOT reused
        # here; appending it would be a duplicate the parser rejects.
        "--sandbox-network", SOURCE_REPAIR_PROFILE["network_mode"],
        "--max-model-steps",
        str(SOURCE_REPAIR_PROFILE["max_model_steps"]),
        "--max-tool-output-bytes",
        str(SOURCE_REPAIR_PROFILE["max_tool_output_bytes"]),
        "--json",
    ]
    cmd += ["--workspace", str(workdir)]
    if output_schema is not None:
        cmd += ["--output-schema", output_schema]
    cmd.append(prompt)
    return cmd


def _dedicated_child_env(workdir, extra=None, trace: bool = False):
    """Narrow child env: explicit passthrough + toolchain caches only.

    Never inherits ambient credentials/config wholesale: only the
    display/toolchain variables in _DEDICATED_PASSTHROUGH_ENV pass
    through, then worker_cache_env(workdir). `extra` carries only
    explicit single-purpose markers (e.g. the probe id);
    META_API_KEY is never copied — ambient presence refuses the spawn
    at the caller before authority I/O, and any key reaching the final
    env is an internal error. MUSE_TRANSPORT_TRACE is set ONLY
    when trace=True (nonsensitive OWNED preflight fixture); actual
    target jobs never enable it.
    """
    base = {}
    # NOTE (audit): the loop below reads ONLY the audited non-secret
    # allowlist above (display/toolchain keys); it never reads
    # credential values, and META_API_KEY is refused at the caller
    # before this env is even built.
    for name in _DEDICATED_PASSTHROUGH_ENV:
        if name in os.environ:
            base[name] = os.environ[name]
    base.update(worker_cache_env(workdir))
    if extra:
        for key, value in extra.items():
            base[str(key)] = str(value)
    if trace:
        base["MUSE_TRANSPORT_TRACE"] = "1"
    return base


def _cancel_requested(cancel_event) -> bool:
    if cancel_event is None:
        return False
    try:
        if hasattr(cancel_event, "is_set"):
            return bool(cancel_event.is_set())
        if callable(cancel_event):
            return bool(cancel_event())
    except Exception:
        return False
    return False


def _kill_owned_group(pid: int) -> bool:
    """SIGKILL ONLY the owned child process group. Never another PID.

    The dedicated spawn uses start_new_session=True, so proc.pid is
    the group leader of a group containing only our child tree.
    Returns True when the signal was delivered.
    """
    try:
        os.killpg(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return False
    return True


def _check_bounded_budgets(timeout, max_bytes) -> None:
    """Validate bounded-I/O budgets BEFORE any spawn.

    _communicate_bounded validates again after spawn, but a
    ValueError there would leak the already-spawned child. Call
    this first so an invalid budget refuses with no Popen.
    """
    if isinstance(timeout, bool) or not isinstance(
            timeout, (int, float)) or not timeout > 0 \
            or not math.isfinite(timeout):
        raise ValueError("timeout must be a positive FINITE number "
                         "(inf/nan remove the leader bound)")
    if isinstance(max_bytes, bool) or not isinstance(
            max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive int")


def _communicate_bounded(proc, timeout, cancel_event, max_bytes):
    """Bounded wait for an owned child.

    Returns (out, err, outcome, out_cut, err_cut, joined_ok,
    reaped_ok) with outcome one of "ok", "timeout", "cancelled",
    "read-error", "overflow". TWO monotonic budgets: the leader
    budget (timeout, required finite) for the child to exit, then a
    SEPARATE bounded cleanup budget for reader joins and reaping.
    Kill precedes the join AND the join has its own budget, so
    joined_ok/reaped_ok report what actually happened, never a claim
    from a zero-budget join. Overflow and reader errors stop the
    owned child promptly (no draining to the long leader timeout)
    and refuse distinctly with the kept prefix; the post-cut drain
    exits on the leader's stop flag so it cannot outlive the kill.
    Close never runs
    under a live reader (a buffered close can wait on the reader's
    lock without a timeout); stuck readers are daemon threads
    reported via joined_ok=False. On timeout/cancel/overflow/error
    ONLY the owned process group receives SIGKILL (same owned pgid,
    never another PID) and the wait stops promptly — no unbounded
    stream-close, no descendant-held-pipe hang. util.py is untouched:
    this dedicated path does not share the generic buffering helper.
    """
    _check_bounded_budgets(timeout, max_bytes)
    out_chunks, err_chunks = [], []
    out_len, err_len = [0], [0]
    out_cut, err_cut = [False], [False]
    read_error = [False]
    stop = [False]

    def _reader(stream, chunks, total, cut):
        try:
            while not stop[0]:
                try:
                    part = stream.read(65536)
                except Exception:
                    read_error[0] = True
                    break
                if not part:
                    break
                if total[0] + len(part) > max_bytes:
                    keep = max_bytes - total[0]
                    if keep > 0:
                        chunks.append(part[:keep])
                        total[0] = max_bytes
                    cut[0] = True
                    # Bounded post-cut drain: exit as soon as the
                    # leader stops the child (stop flag) instead of
                    # draining an unkillable spewer forever. The
                    # outcome is already decided; this loop only
                    # avoids pipe-blocking a child that exits
                    # cleanly on its own.
                    try:
                        while not stop[0] and stream.read(65536):
                            pass
                    except Exception:
                        read_error[0] = True
                    break
                chunks.append(part)
                total[0] += len(part)
        except Exception:
            read_error[0] = True

    # Daemon threads: a descendant-held pipe can strand a reader
    # past every budget; a stuck reader must never block interpreter
    # teardown. Liveness is still reported honestly via joined_ok.
    t_out = threading.Thread(target=_reader, daemon=True,
                             args=(proc.stdout, out_chunks, out_len,
                                   out_cut))
    t_err = threading.Thread(target=_reader, daemon=True,
                             args=(proc.stderr, err_chunks, err_len,
                                   err_cut))
    t_out.start()
    t_err.start()
    outcome = "ok"
    leader_deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if _cancel_requested(cancel_event):
            outcome = "cancelled"
            break
        if read_error[0]:
            # A failed stream read refuses promptly: no draining
            # until the long leader timeout.
            outcome = "read-error"
            break
        if out_cut[0] or err_cut[0]:
            # Over the cap: stop the owned child at once and report
            # the distinct overflow refusal with the kept prefix.
            outcome = "overflow"
            break
        if time.monotonic() >= leader_deadline:
            outcome = "timeout"
            break
        time.sleep(0.05)
    if outcome != "ok":
        # Stop the readers' drain first, then kill: the readers can
        # only observe EOF once the group holding the write ends is
        # gone, and the drain must not outlive the kill decision.
        stop[0] = True
        _kill_owned_group(proc.pid)
    # Separate bounded cleanup budget (never zero on the timeout
    # path): floored at 5s, capped at 30s, scaled from the leader
    # budget in between so a unit probe never waits far past its
    # own scale.
    cleanup_budget = min(30.0, max(5.0, timeout / 4.0))
    cleanup_deadline = time.monotonic() + cleanup_budget
    for thread in (t_out, t_err):
        remaining = cleanup_deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    joined_ok = not t_out.is_alive() and not t_err.is_alive()
    if not joined_ok:
        # A descendant still holds a pipe open past the cleanup
        # budget: signal the owned group again (same owned pgid,
        # never another PID) and re-join with what remains. The
        # outcome stands as the leader phase decided it (a leader
        # that exited on its own is not rewritten to timeout); only
        # joined_ok reports the stuck reader honestly.
        _kill_owned_group(proc.pid)
        for thread in (t_out, t_err):
            remaining = cleanup_deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        joined_ok = not t_out.is_alive() and not t_err.is_alive()
    for stream, thread in ((proc.stdout, t_out), (proc.stderr, t_err)):
        # Never close under a live reader: a buffered close can wait
        # on the reader's read lock without a timeout, which would
        # reintroduce an unbounded step. A joined (dead) reader owns
        # no lock, so its close is prompt; a stuck reader's fd is
        # left to process teardown (daemon thread) and reported via
        # joined_ok=False.
        if thread.is_alive():
            continue
        try:
            if stream:
                stream.close()
        except Exception:
            pass
    try:
        remaining = cleanup_deadline - time.monotonic()
        if remaining > 0:
            proc.wait(timeout=remaining)
    except Exception:
        pass
    reaped_ok = proc.poll() is not None
    if read_error[0] and outcome == "ok":
        outcome = "read-error"
    if (out_cut[0] or err_cut[0]) and outcome == "ok":
        outcome = "overflow"
    return (b"".join(out_chunks), b"".join(err_chunks), outcome,
            out_cut[0], err_cut[0], joined_ok, reaped_ok)


def exec_source_repair(prompt: str, workdir, timeout: int = 600,
                       env=None, cancel_event=None, output_schema=None,
                       trace: bool = False, preflight_home=None,
                       admission_kind=None, preflight_pfid=None):
    """Run one dedicated-profile step. Returns (exit, output, usage).

    Execution boundary (API-only gating cannot protect direct
    callers): `admission_kind` is REQUIRED — "stage0" for the owned
    bootstrap probe (stage0 admission: trusted auth + native
    containment BEFORE the first spawn) or "target" for source work
    (full receipt authority + native containment). No
    caller-supplied verified=True/model report/self-declared dict
    shortcut exists: both legs re-check live facts themselves; a
    missing/stale/unproved leg means NO Popen, exit 125. Today both
    legs refuse in production (unproved auth/containment) — the
    refusal IS the honest record.

    The pinned binary is snapshotted before argv build and
    re-snapshotted immediately before spawn; any dev/ino/size/SHA
    change refuses (narrowed TOCTOU). META_API_KEY presence — even
    empty-valued — in the ACTUAL final child env refuses the spawn,
    since stored keys take priority over subscription login; the
    trusted session proof itself stays UNVERIFIED here and is
    observed only by the admitted observer.

    Streams are bounded under a finite leader budget plus a
    separate bounded cleanup budget (owned process group, joined
    readers, reaped child; only owned PIDs ever signalled);
    overflow and reader errors stop the child promptly and refuse
    distinctly. Budgets validate BEFORE Popen (no spawn on
    invalid input), and an "ok" leader with unjoined readers or
    an unreaped child still refuses: partial capture is never a
    trusted result. Streams never close under a live reader.
    MUSE_TRANSPORT_TRACE is enabled ONLY for stage0 owned probes
    (trace=True AND admission_kind="stage0"): the bounded trace is
    pinned to a private file under the owned workdir and only its
    bytes/SHA travel in `usage`; target jobs refuse trace and their
    stderr is counted, never silently kept. `usage` records the
    requested profile, never an observed enforcement.
    """
    from . import preflight as _preflight
    if admission_kind not in ("stage0", "target"):
        return 125, "", {"error": "refused: admission_kind is required "
                                  "('stage0' for the owned bootstrap "
                                  "probe, 'target' for source work); "
                                  "no implicit admission",
                         "code": "no-authority",
                         "tokens_measurable": False, "tokens": None}
    if trace and admission_kind != "stage0":
        return 125, "", {"error": "refused: transport trace is only "
                                  "allowed on the owned stage0 probe, "
                                  "never on target jobs",
                         "code": "no-authority",
                         "tokens_measurable": False, "tokens": None}
    # Ambient credential hygiene BEFORE any authority I/O or spawn: a
    # stored key takes priority over subscription login, so an
    # explicitly key-bearing operator environment (even empty-valued)
    # must not run subscription-claimed jobs. The narrowed child env
    # below never copies the key; this gate refuses the ambient fact.
    if os.environ.get("META_API_KEY") is not None:
        return 125, "", {"error": "refused: META_API_KEY is present in "
                                  "this environment and would take "
                                  "priority over subscription login; "
                                  "unset it for subscription-only runs",
                         "code": "api-key-present",
                         "tokens_measurable": False, "tokens": None}
    # Ambient contributor-mode hygiene, same shape: the env var sets
    # the mode for a run, so an explicitly contributor-divergent
    # operator environment (even empty-valued) must not run
    # contributor-ON-claimed jobs. Presence only, never the value.
    if os.environ.get("MUSE_CONTRIBUTOR_MODE") is not None:
        return 125, "", {"error": "refused: MUSE_CONTRIBUTOR_MODE "
                                  "is present in this environment and "
                                  "would fight the pinned "
                                  "contributor-ON request; unset it "
                                  "for dedicated runs",
                         "code": "contributor-env-present",
                         "tokens_measurable": False, "tokens": None}
    try:
        if admission_kind == "stage0":
            authority = _preflight.stage0_admission(preflight_home)
        else:
            authority = _preflight.check_target_authority(
                preflight_home, pfid=preflight_pfid)
            _preflight.native_containment()
    except Exception as exc:
        code = getattr(exc, "code", "") or "no-authority"
        return 125, "", {"error": "refused: no admission for dedicated "
                                  f"spawn ({admission_kind}): {exc}",
                         "code": code,
                         "tokens_measurable": False, "tokens": None}
    try:
        snap = resolve_pinned_muse()
    except OSError as exc:
        return 127, "", {"error": f"pinned muse unavailable: {exc}",
                         "tokens_measurable": False, "tokens": None}
    try:
        cmd = build_source_repair_cmd(prompt, workdir,
                                      output_schema=output_schema)
    except ValueError as exc:
        return 125, "", {"error": f"refused: {exc}",
                         "tokens_measurable": False, "tokens": None}
    cmd[0] = snap["real_path"]
    probe_id = None
    if isinstance(env, dict) and env.get("OPENMAINTY_PREFLIGHT_PROBE"):
        probe_id = str(env["OPENMAINTY_PREFLIGHT_PROBE"])
    child_extra = {}
    if probe_id:
        child_extra["OPENMAINTY_PREFLIGHT_PROBE"] = probe_id
    base = _dedicated_child_env(workdir, child_extra, trace=trace)
    # Defense in depth: the narrowed env never copies the key, so
    # its presence here is an internal error, not operator state.
    if "META_API_KEY" in base:
        return 125, "", {"error": "refused: internal error: META_API_KEY "
                                  "reached the narrowed child environment",
                         "code": "api-key-present",
                         "tokens_measurable": False, "tokens": None}
    # Defense in depth, same shape: the narrowed env never copies
    # the contributor var either.
    if "MUSE_CONTRIBUTOR_MODE" in base:
        return 125, "", {"error": "refused: internal error: "
                                  "MUSE_CONTRIBUTOR_MODE reached the "
                                  "narrowed child environment",
                         "code": "contributor-env-present",
                         "tokens_measurable": False, "tokens": None}
    try:
        recheck = _snapshot_binary(snap["path"])
    except OSError as exc:
        return 127, "", {"error": f"pinned muse changed before spawn: "
                                  f"{exc}",
                         "tokens_measurable": False, "tokens": None}
    if not _same_binary_snapshot(snap, recheck):
        return 127, "", {"error": "refused: pinned binary identity "
                                  "changed between resolve and spawn",
                         "code": "binary-drift",
                         "tokens_measurable": False, "tokens": None}
    try:
        _check_bounded_budgets(timeout, _DEDICATED_STREAM_MAX_BYTES)
    except ValueError as exc:
        return 125, "", {"error": f"refused: {exc}",
                         "tokens_measurable": False, "tokens": None}
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(workdir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=base,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as exc:
        return 127, "", {"error": f"pinned muse spawn failed: {exc}",
                         "tokens_measurable": False, "tokens": None}
    try:
        out_b, err_b, outcome, out_cut, err_cut, joined_ok, \
            reaped_ok = _communicate_bounded(
                proc, timeout, cancel_event,
                _DEDICATED_STREAM_MAX_BYTES)
    except Exception as exc:
        # Bounded cleanup on the exception path too: signal the
        # owned group, best-effort reap, never propagate with a
        # live child behind us.
        _kill_owned_group(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return 1, "", {"error": "worker capture failed "
                               f"({type(exc).__name__}); owned group "
                               "signalled, nothing trusted",
                       "tokens_measurable": False, "tokens": None}
    bounded = {"stdout_bytes": len(out_b),
               "stderr_bytes": len(err_b),
               "stdout_truncated": out_cut,
               "stderr_truncated": err_cut,
               "readers_joined": joined_ok,
               "child_reaped": reaped_ok,
               "tokens_measurable": False, "tokens": None}
    if outcome == "ok" and not (joined_ok and reaped_ok):
        # The leader exited but capture is incomplete (a descendant
        # still holds a pipe, or the child was never reaped): the
        # bytes may be a prefix, never a result. Daemonizing the
        # stranded reader is telemetry, not cleanup — refuse
        # before any parse/report/trace trust, and put the group
        # down so nothing lingers past this decision.
        _kill_owned_group(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return 1, "", dict(
            bounded, error="worker capture incomplete (stranded "
                           "reader or unreaped child); partial "
                           "output untrusted, nothing recorded")
    if outcome == "timeout":
        return 124, "", dict(bounded, timeout_s=timeout,
                             error="worker step exceeded its total "
                                   "deadline; owned group signalled, "
                                   "no partial output trusted")
    if outcome == "cancelled":
        return 130, "", dict(bounded, cancelled=True)
    if outcome == "read-error":
        return 1, "", dict(
            bounded, error="worker output unreadable (bounded reader "
                           "fault); output untrusted, nothing recorded")
    if outcome == "overflow":
        return 1, "", dict(
            bounded, error="worker output exceeded bounded capture "
                           "(4MiB/stream); truncated output untrusted, "
                           "nothing recorded")
    out = out_b.decode("utf-8", errors="replace")
    usage = parse_usage(out)
    usage["exit_code"] = proc.returncode
    usage["requested_profile"] = dict(SOURCE_REPAIR_PROFILE)
    usage["pinned_binary"] = {"path": snap["path"],
                              "real_path": snap["real_path"],
                              "sha256": snap["sha256"]}
    usage["billing"] = ("subscription login expected "
                        "(META_API_KEY absent; session UNVERIFIED)")
    usage["authority"] = authority
    usage["stdout_bytes"] = len(out_b)
    usage["stdout_truncated"] = out_cut
    usage["stderr_bytes"] = len(err_b)
    usage["stderr_truncated"] = err_cut
    usage["readers_joined"] = joined_ok
    usage["child_reaped"] = reaped_ok
    if trace:
        trace_path = os.path.join(str(workdir), "transport.trace")
        try:
            with open(trace_path, "wb") as fh:
                fh.write(err_b[:_DEDICATED_STREAM_MAX_BYTES])
            usage["transport_trace"] = {
                "path": trace_path,
                "bytes": len(err_b),
                "sha256": hashlib.sha256(err_b).hexdigest(),
                "note": "private owned-probe trace: parse/redact "
                        "before any UI; never enable on target jobs",
            }
        except OSError as exc:
            usage["transport_trace"] = {
                "path": "", "bytes": len(err_b), "sha256": "",
                "note": f"trace pin failed: {exc}",
            }
    return proc.returncode, out, usage


# ---------------------------------------------------------------------------
# App-owned MSP host factory: pinned `muse serve` child over stdio.
#
# Metadata diagnostics ONLY — never a worker, never admission. The
# factory ALWAYS resolves the configured pinned binary itself and
# ALWAYS wraps the child in exactly one native offline policy built
# by ncontain for the fresh owned root (no unwrapped fallback, no
# caller argv, no override callbacks). The child speaks strict
# newline-delimited JSON-RPC 2.0 over stdio: the fixed initialize
# handshake, the initialized{} notification, and the four read-only
# metadata queries with EMPTY params. No model turn, no session/
# login/usage/transport-trace/approval dispatch, no general RPC, no
# prompt text anywhere near the child.
#
# The public surface is spawn/collect/close plus the typed parsers.
# Request/notification construction is private with fixed methods and
# fixed params — there is no public method/params passthrough.
# Boundaries that stay fail-closed: the child env is built from
# scratch inside a fresh, empty owned root (no ambient API keys,
# credential paths, proxy variables, or config pointers leak in;
# META_API_KEY presence — even empty — refuses before spawn; env
# subdirs are created exclusively, never reused). Stdout/stderr
# drain concurrently with bounded cumulative budgets; stdin uses
# real nonblocking fds with deadlines. Handles are private registry
# ids: callers can never forge a stale/reused/closed/exited/
# binary-drifted handle, pass a PID, or present a report as proof.
# Binary full snapshot (via _snapshot_binary/_same_binary_snapshot),
# PIDs, nonce, argv, root/policy identity, and env are measured at
# spawn and re-proved before every use; requested flags are never
# observed enforcement. A positive diagnostic never authorizes a
# model worker (native_containment still always refuses; effective
# auth/config/Meta-ready channels remain UNPROVEN until observed).
# ---------------------------------------------------------------------------

MSP_HOST_SCHEMA = 1
# NDJSON only: the pinned `muse serve` stdio dialect is one JSON-RPC
# 2.0 object per line. No Content-Length framing dialect exists here.
_MSP_LINE_MAX_BYTES = 1 << 20
_MSP_STDOUT_MAX_BYTES = 4 << 20
_MSP_STDERR_MAX_BYTES = 1 << 20
_MSP_INBOX_MAX_MESSAGES = 1024
_MSP_ID_MAX = 1 << 30
_MSP_WRITE_MAX_BYTES = 1 << 16
_MSP_SELECT_GRANULE_S = 0.005
_MSP_CLEANUP_S = 20.0

_MSP_METHODS = ("initialize", "account/read", "settings/read",
                "permissionProfile/list", "mcpServer/list")

# Pinned initialize contract, observed on the reviewed 1.4.14 host.
# version/name/schema-fingerprint/experimentalApi/durability are all
# REQUIRED exactly — anything else is a typed refusal, never a quiet
# acceptance, and no later query runs.
_MSP_SERVER_NAME = "muse"
_MSP_SERVER_VERSION = "1.4.14"
_MSP_SCHEMA_FINGERPRINT = ("sha256:c94c3d1f2f51e6424074b9ec43eac"
                           "3eedcf532d3dcd77351ec1383a87d3049c6")

_MSP_KNOWN_ACCOUNT_STATES = ("loggedOut", "envKey", "apiKey",
                             "accountLogin")

# Whitelisted settings observations: the ONLY settings keys ever
# retained (with typed values). Every other key is dropped and
# counted — never retained, never logged. No token/apiKey/auth
# string can leak because unknown keys never survive parsing.
_MSP_SETTINGS_WHITELIST = (
    ("run.workflow_trigger_mode", str, 64),
    ("tools.artifact.enabled", bool, 0),
    ("tools.web_fetch.enabled", bool, 0),
    ("tools.web_search.mode", str, 64),
)

# Minimal MCP projection: scalar identity/status strings only, from
# this closed key set. No command/args/env/headers/URLs/tokens or
# raw error strings are ever retained.
_MSP_SERVER_IDENT_KEYS = ("name", "id", "status", "state")


class MspError(Exception):
    """Typed MSP-host failure (transport, protocol, or honesty gate)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code or ""


def build_host_cmd() -> list:
    """Argv for the app-owned MSP metadata host (`muse serve`).

    Only flags with actually-read `muse serve --help` support are
    used: provider/model/contributor-mode/disable-write/
    disable-shell plus --no-session-log (memory-only sessions, no
    session-index DB writes). argv[0] is the literal "muse"
    placeholder the spawner replaces with the pinned real binary
    path (never the updating launcher).
    """
    return ["muse", "serve",
            "--provider", "meta",
            "--model", "muse-spark-1.3",
            "--contributor-mode", "on",
            "--disable-write",
            "--disable-shell",
            "--no-session-log"]


def _msp_validate_timeout(value, what: str) -> float:
    """Require a finite positive number of seconds (no NaN/inf/bool)."""
    if value is None or isinstance(value, bool) \
            or not isinstance(value, (int, float)) \
            or value != value or value <= 0 \
            or value == float("inf"):
        raise MspError(f"MSP {what} timeout must be a finite positive "
                       f"number of seconds", code="bad-input")
    return float(value)


def _check_msp_root(root) -> str:
    """Validate a fresh, empty owned root without creating anything.

    Requires an existing non-symlink directory, owned by the current
    uid, mode exactly 0700, whose canonical path equals itself, AND
    containing no entries at all: a reused profile/data/session
    directory is never accepted as a fresh metadata root.
    """
    if not isinstance(root, str) or not root or "\x00" in root:
        raise MspError("metadata root must be a nonempty path",
                       code="bad-input")
    try:
        st = os.lstat(root)
    except OSError as exc:
        raise MspError(f"metadata root unreadable: {exc}",
                       code="unreadable") from exc
    if stat.S_ISLNK(st.st_mode):
        raise MspError("metadata root is a symlink: refusing rebind",
                       code="unreadable")
    if not stat.S_ISDIR(st.st_mode):
        raise MspError("metadata root is not a directory",
                       code="bad-input")
    if st.st_uid != os.getuid():
        raise MspError("metadata root is not owned by this user",
                       code="unreadable")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise MspError("metadata root must be mode 0700",
                       code="unreadable")
    canon = os.path.realpath(root)
    if canon != os.path.normpath(os.path.abspath(root)):
        raise MspError("metadata root path is not canonical",
                       code="unreadable")
    try:
        entries = os.listdir(canon)
    except OSError as exc:
        raise MspError(f"metadata root unreadable: {exc}",
                       code="unreadable") from exc
    if entries:
        raise MspError("metadata root is not empty: refusing to "
                       "reuse an existing profile/data directory",
                       code="bad-input")
    return canon


def _msp_host_env(root: str) -> dict:
    """Minimal child env built from scratch inside the owned root.

    HOME/XDG_*/TMPDIR/OPENMAINTY_HOME/cwd all live beneath the root;
    PATH is a fixed system list. Nothing is inherited from the parent
    environment, so no API keys, tokens, proxy settings, or config
    paths can leak into the child. Subdirectories are created
    EXCLUSIVELY (no exist_ok: a pre-existing entry refuses instead of
    being reused) with mode 0700 and re-checked (dir, owned,
    no symlink).
    """
    for leaf in ("home", "config", "data", "cache", "tmp"):
        path = os.path.join(root, leaf)
        try:
            os.mkdir(path, 0o700)
            st = os.lstat(path)
        except OSError as exc:
            raise MspError(f"metadata env dir failed (refusing "
                           f"reuse): {exc}",
                           code="unreadable") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise MspError("metadata env dir replaced",
                           code="unreadable")
        if st.st_uid != os.getuid():
            raise MspError("metadata env dir not owned",
                           code="unreadable")
        if stat.S_IMODE(st.st_mode) != 0o700:
            raise MspError("metadata env dir must be mode 0700",
                           code="unreadable")
    return {"HOME": os.path.join(root, "home"),
            "XDG_CONFIG_HOME": os.path.join(root, "config"),
            "XDG_DATA_HOME": os.path.join(root, "data"),
            "XDG_CACHE_HOME": os.path.join(root, "cache"),
            "TMPDIR": os.path.join(root, "tmp"),
            "OPENMAINTY_HOME": root,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


def _msp_reject_dupes(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key: {key!r}")
        out[key] = value
    return out


def _msp_reject_const(value: str):
    raise ValueError(f"non-finite JSON constant: {value!r}")


def parse_msp_initialize(result: dict,
                         expected_muse_home: str) -> dict:
    """Validate the initialize result; bind the observed muse home.

    schema.version must be 1 and schema.fingerprint a sha256 digest
    (format-checked, never trusted as enforcement). museHome must
    equal the owned <data>/muse path or the host is refused: a host
    writing session state anywhere else is not our isolated host.
    """
    if not isinstance(result, dict):
        raise MspError("initialize result must be an object",
                       code="malformed")
    server = result.get("serverInfo")
    if not isinstance(server, dict):
        raise MspError("initialize result missing serverInfo",
                       code="malformed")
    name = server.get("name")
    version = server.get("version")
    if name != _MSP_SERVER_NAME:
        raise MspError("initialize serverInfo.name is not the pinned "
                       "metadata host", code="unknown-host")
    if version != _MSP_SERVER_VERSION:
        raise MspError("initialize serverInfo.version is not the "
                       "pinned metadata host",
                       code="unknown-host")
    schema = result.get("schema")
    if not isinstance(schema, dict):
        raise MspError("initialize result missing schema",
                       code="malformed")
    if schema.get("version") != 1 or isinstance(
            schema.get("version"), bool):
        raise MspError("initialize schema version is not 1",
                       code="unknown-schema")
    fingerprint = schema.get("fingerprint")
    if fingerprint != _MSP_SCHEMA_FINGERPRINT:
        raise MspError("initialize schema fingerprint is not the "
                       "pinned contract", code="unknown-schema")
    muse_home = result.get("museHome")
    if not isinstance(muse_home, str) or not muse_home:
        raise MspError("initialize result missing museHome",
                       code="malformed")
    if os.path.normpath(muse_home) != muse_home:
        raise MspError("initialize museHome is not a canonical path",
                       code="malformed")
    if muse_home != expected_muse_home:
        raise MspError(
            "host museHome is not the owned session dir: refusing "
            "a host that writes elsewhere",
            code="muse-home-mismatch")
    durability = result.get("sessionDurability")
    if durability != "ephemeral":
        raise MspError("initialize sessionDurability is not "
                       "ephemeral: refusing a host that persists "
                       "session state", code="durability-not-ephemeral")
    sandbox = result.get("sandboxAvailability")
    if sandbox is not None and (
            not isinstance(sandbox, str) or len(sandbox) > 64):
        raise MspError("initialize sandboxAvailability invalid",
                       code="malformed")
    caps = result.get("grantedCapabilities")
    if caps is None:
        caps = []
    if not isinstance(caps, list) or len(caps) > 32 or any(
            not isinstance(c, str) or len(c) > 128 for c in caps):
        raise MspError("initialize grantedCapabilities invalid",
                       code="malformed")
    experimental = result.get("experimentalApi")
    if experimental is not True:
        raise MspError("initialize experimentalApi is not true: the "
                       "four metadata reads require it",
                       code="experimental-required")
    out = {"server_name": name, "server_version": version,
           "schema_version": 1, "schema_fingerprint": fingerprint,
           "muse_home": muse_home, "session_durability": durability,
           "sandbox_availability": sandbox,
           "granted_capabilities": list(caps),
           "experimental_api": experimental}
    user_agent = result.get("userAgent")
    if isinstance(user_agent, str) and user_agent:
        out["user_agent"] = user_agent[:256]
    return out


def parse_msp_account(result: dict) -> dict:
    """Validate the account/read result; unknown states stay unknown.

    credential_required must be a real bool. displayLabel/avatarURL
    and any other peer fields are dropped (never retained, never
    logged). An unrecognized state reads as "unknown", never as a
    known lane.
    """
    if not isinstance(result, dict):
        raise MspError("account result must be an object",
                       code="malformed")
    state = result.get("state")
    if not isinstance(state, str):
        raise MspError("account result missing state",
                       code="malformed")
    if state not in _MSP_KNOWN_ACCOUNT_STATES:
        state = "unknown"
    required = result.get("credentialRequired")
    if not isinstance(required, bool):
        raise MspError("account credentialRequired must be a bool",
                       code="malformed")
    return {"state": state, "credential_required": required}


def msp_credential_lane(account: dict) -> tuple:
    """Honest credential-lane label for an observed account result.

    credentialRequired=false means the endpoint needs NO caller
    credential (keyless/gateway): NEVER subscription proof.
    accountLogin+true is a candidate winning lane on THIS live host
    only — never billing, contributor, or config evidence. loggedOut
    or anything unrecognized stays unknown.
    """
    state = account.get("state")
    required = account.get("credential_required")
    if required is False:
        return ("keyless",
                "endpoint needs no caller credential "
                "(keyless/gateway): never subscription proof")
    if state == "accountLogin" and required is True:
        return ("accountLogin-candidate",
                "candidate winning lane on THIS host only: billing, "
                "contributor, and config remain unverified")
    if state == "envKey":
        return ("envKey",
                "caller key from the environment takes precedence: "
                "not subscription login")
    if state == "apiKey":
        return ("apiKey",
                "caller API key takes precedence: not subscription "
                "login")
    return ("unknown",
            "no established credential lane: subscription login "
            "unverified on this host")


def parse_msp_settings(result: dict) -> dict:
    """Validate the settings/read result; whitelist ONLY.

    Only the four actually-observed keys in _MSP_SETTINGS_WHITELIST
    are ever retained, with typed values. Every other key — tokens,
    apiKey/auth strings, labels, nested objects — is dropped and
    counted, never retained, never logged. No secret can leak
    because unknown keys never survive parsing.
    """
    if not isinstance(result, dict):
        raise MspError("settings result must be an object",
                       code="malformed")
    schema_version = result.get("schemaVersion")
    if not isinstance(schema_version, int) or isinstance(
            schema_version, bool):
        raise MspError("settings schemaVersion must be an int",
                       code="malformed")
    values = result.get("values")
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise MspError("settings values must be an object",
                       code="malformed")
    if len(values) > 128:
        raise MspError("settings values exceed 128 entries",
                       code="malformed")
    wanted = {key: (kind, cap) for key, kind, cap
              in _MSP_SETTINGS_WHITELIST}
    kept, dropped = {}, 0
    for key, value in values.items():
        if not isinstance(key, str) or not key or len(key) > 128:
            raise MspError("settings key invalid",
                           code="malformed")
        spec = wanted.get(key)
        if spec is None:
            dropped += 1
            continue
        kind, cap = spec
        if kind is bool:
            if not isinstance(value, bool):
                dropped += 1
                continue
            kept[key] = value
        elif kind is str:
            if not isinstance(value, str) or len(value) > cap:
                dropped += 1
                continue
            kept[key] = value
        else:
            dropped += 1
    return {"schema_version": schema_version, "values": kept,
            "dropped": dropped}


def parse_msp_profiles(result: dict) -> dict:
    """Validate the permissionProfile/list result (catalog, not proof).

    An available catalog entry never proves enforcement; this parser
    only bounds shapes. reviewerAuthority strings are recorded
    verbatim (bounded) — unknown values stay exactly as observed.
    """
    if not isinstance(result, dict):
        raise MspError("profiles result must be an object",
                       code="malformed")
    profiles = result.get("profiles")
    if not isinstance(profiles, list):
        raise MspError("profiles must be a list",
                       code="malformed")
    if len(profiles) > 32:
        raise MspError("profiles exceed 32 entries",
                       code="malformed")
    out = []
    for entry in profiles:
        if not isinstance(entry, dict):
            raise MspError("profile entry must be an object",
                           code="malformed")
        profile_id = entry.get("profileId")
        if not isinstance(profile_id, str) or not profile_id \
                or len(profile_id) > 128:
            raise MspError("profile profileId invalid",
                           code="malformed")
        item = {"profile_id": profile_id}
        for key, cap in (("label", 512), ("description", 512),
                         ("reviewerAuthority", 64)):
            value = entry.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > cap:
                raise MspError(f"profile {key} invalid",
                               code="malformed")
            item[key] = value
        available = entry.get("available")
        if not isinstance(available, bool):
            raise MspError("profile available must be a bool",
                           code="malformed")
        item["available"] = available
        out.append(item)
    return {"profiles": out}


def parse_msp_servers(result: dict) -> dict:
    """Validate the mcpServer/list result; minimal identity projection.

    Only the entry count plus per-entry scalar identity/status
    strings from _MSP_SERVER_IDENT_KEYS survive. No command/args/
    env/headers/URLs/tokens or raw error strings are ever retained:
    unknown keys are dropped and counted, non-dict entries are
    counted as malformed. Nothing here proves hooks are disabled or
    a profile is enforced.
    """
    if not isinstance(result, dict):
        raise MspError("servers result must be an object",
                       code="malformed")
    servers = result.get("servers")
    if not isinstance(servers, list):
        raise MspError("servers must be a list",
                       code="malformed")
    if len(servers) > 64:
        raise MspError("servers exceed 64 entries",
                       code="malformed")
    out, dropped, malformed = [], 0, 0
    for index, entry in enumerate(servers):
        if not isinstance(entry, dict):
            malformed += 1
            continue
        item = {"index": index}
        for key, value in entry.items():
            if key in _MSP_SERVER_IDENT_KEYS \
                    and isinstance(value, str) \
                    and value and len(value) <= 128:
                item[key] = value
            else:
                dropped += 1
        out.append(item)
    return {"count": len(servers), "servers": out,
            "fields_dropped": dropped, "malformed": malformed}


_MSP_MODEL_LIST_MAX = 64


def parse_msp_model_list(result: dict, session_id: str) -> dict:
    """Validate the model/list result; extract the isActive row.

    The top-level providerId MUST read meta (cross-provider data
    refuses as provider-mismatch, same as session binding). The
    catalog source is retained VERBATIM with a live flag
    (source == "providerCatalog"); liveness is judged by the
    verdict layer, never assumed here. displayLabel/description/
    cost/variants are never retained. Zero active rows means
    unknown (active None); more than one active row is
    contradictory data and refuses. session_id binds the query
    context: the isActive claim belongs to that session only.
    """
    sid = _msp_check_uuid(session_id, "model/list session",
                          version="7")
    if not isinstance(result, dict):
        raise MspError("model/list result must be an object",
                       code="malformed")
    provider = result.get("providerId")
    if not isinstance(provider, str) or not provider:
        raise MspError("model/list providerId invalid",
                       code="malformed")
    if provider != "meta":
        raise MspError("model/list catalog is not the meta "
                       "provider: refusing cross-provider data",
                       code="provider-mismatch")
    profile = result.get("profileId")
    if profile is not None and (
            not isinstance(profile, str) or len(profile) > 128):
        raise MspError("model/list profileId invalid",
                       code="malformed")
    source = result.get("source")
    if not isinstance(source, str) or not source \
            or len(source) > 64:
        raise MspError("model/list source invalid",
                       code="malformed")
    rows = result.get("models")
    if not isinstance(rows, list):
        raise MspError("model/list models must be a list",
                       code="malformed")
    if len(rows) > _MSP_MODEL_LIST_MAX:
        raise MspError("model/list exceeds 64 rows",
                       code="malformed")
    active = None
    for entry in rows:
        if not isinstance(entry, dict):
            raise MspError("model/list row must be an object",
                           code="malformed")
        model_id = entry.get("modelId")
        if not isinstance(model_id, str) or not model_id \
                or len(model_id) > 256:
            raise MspError("model/list row modelId invalid",
                           code="malformed")
        row_provider = entry.get("providerId")
        if not isinstance(row_provider, str) \
                or not row_provider or len(row_provider) > 64:
            raise MspError("model/list row providerId invalid",
                           code="malformed")
        flag = entry.get("isActive")
        if not isinstance(flag, bool):
            raise MspError("model/list row isActive must be a "
                           "bool", code="malformed")
        if flag:
            if active is not None:
                raise MspError("model/list has more than one "
                               "active row: contradictory data",
                               code="malformed")
            active = {"model_id": model_id,
                      "provider_id": row_provider}
    return {"for_session": sid, "provider_id": "meta",
            "profile_id": profile, "source": source,
            "source_live": source == "providerCatalog",
            "rows": len(rows), "active": active}


def _msp_check_usage_int(value: object, what: str,
                         minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MspError(f"usage {what} must be an integer",
                       code="malformed")
    if value < minimum:
        raise MspError(f"usage {what} below {minimum}",
                       code="malformed")
    return value


def parse_msp_usage(result: dict) -> dict:
    """Validate the usage/read result (subscription window, no turn).

    An omitted `usage` member is truthful absence
    ({observed: False}), never an error. A present member must
    carry a verbatim tier plus fully valid window/weekly blocks.
    over-quota percents above 100 are valid (provider-verbatim).
    Staleness is NOT judged here: any observedAtMs round-trips
    verbatim and the Stage-B check (msp_usage_fresh_after) decides
    freshness against the canary ack.
    """
    if not isinstance(result, dict):
        raise MspError("usage result must be an object",
                       code="malformed")
    usage = result.get("usage")
    if usage is None:
        return {"observed": False}
    if not isinstance(usage, dict):
        raise MspError("usage member must be an object",
                       code="malformed")
    stamp = _msp_check_usage_int(
        usage.get("observedAtMs"), "observedAtMs", 0)
    tier = usage.get("tier")
    if not isinstance(tier, str) or not tier or len(tier) > 128:
        raise MspError("usage tier invalid", code="malformed")
    window = usage.get("window")
    if not isinstance(window, dict):
        raise MspError("usage window must be an object",
                       code="malformed")
    weekly = usage.get("weekly")
    if not isinstance(weekly, dict):
        raise MspError("usage weekly must be an object",
                       code="malformed")
    return {
        "observed": True, "observed_at_ms": stamp,
        "tier": tier,
        "window": {
            "resetsAtMs": _msp_check_usage_int(
                window.get("resetsAtMs"), "window.resetsAtMs",
                0),
            "usedPercent": _msp_check_usage_int(
                window.get("usedPercent"),
                "window.usedPercent", 0),
            "windowDurationMins": _msp_check_usage_int(
                window.get("windowDurationMins"),
                "window.windowDurationMins", 1),
        },
        "weekly": {
            "resetsAtMs": _msp_check_usage_int(
                weekly.get("resetsAtMs"), "weekly.resetsAtMs",
                0),
            "usedPercent": _msp_check_usage_int(
                weekly.get("usedPercent"),
                "weekly.usedPercent", 0),
        },
    }


def msp_usage_fresh_after(usage: dict, ack_ms: int) -> bool:
    """Stage-B freshness predicate for a parsed usage record.

    True only when a window was observed strictly AFTER the
    canary turn ack (equal means the window predates the turn:
    stale). Pure; no handle, no I/O.
    """
    if not isinstance(usage, dict) or not usage.get("observed"):
        return False
    stamp = usage.get("observed_at_ms")
    if not isinstance(stamp, int) or isinstance(stamp, bool):
        return False
    if not isinstance(ack_ms, int) or isinstance(ack_ms, bool):
        return False
    return stamp > ack_ms


_MSP_HOSTS: dict = {}


def _msp_handle() -> str:
    import secrets
    return secrets.token_hex(16)


def _host_record(handle: str) -> dict:
    rec = _MSP_HOSTS.get(handle)
    if rec is None:
        raise MspError("unknown or closed MSP host handle: refusing "
                       "forged/stale/reused/closed identity",
                       code="unknown-handle")
    return rec


def _split_msp_line(buf: bytearray):
    """Split one NDJSON line; (None, 0) means need more bytes.

    Strict newline-delimited JSON-RPC 2.0 only. There is no
    Content-Length framing dialect on this host: a line that looks
    like a frame header is data, and over-long lines refuse. The
    1MiB cap applies to the line content whether or not the
    terminator has arrived yet: a terminator does not legitimize
    an over-long line.
    """
    if not buf:
        return None, 0
    if bytes(buf[:15]).lower().startswith(b"content-length:"):
        raise MspError("framed message rejected: this host speaks "
                       "NDJSON only", code="malformed")
    end = buf.find(b"\n")
    if end < 0:
        if len(buf) > _MSP_LINE_MAX_BYTES:
            raise MspError("line exceeds 1MiB",
                           code="overflow")
        return None, 0
    if end > _MSP_LINE_MAX_BYTES:
        raise MspError("line exceeds 1MiB",
                       code="overflow")
    line = bytes(buf[:end])
    if line.endswith(b"\r"):
        line = line[:-1]
    if not line:
        return b"", end + 1
    return line, end + 1


def _decode_msp_object(raw: bytes):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MspError("host bytes are not UTF-8",
                       code="malformed") from exc
    try:
        return json.loads(text, object_pairs_hook=_msp_reject_dupes,
                          parse_constant=_msp_reject_const)
    except ValueError as exc:
        raise MspError(f"host JSON malformed: {exc}",
                       code="malformed") from exc


def _msp_break(rec: dict, code: str):
    """Record the FIRST host break; later causes never overwrite it.

    Synchronized on a dedicated per-record lock so the stdout/stderr
    reader threads, the request/timeout path, and the account binder
    all observe the same earliest cause. The dedicated lock is
    always a leaf: it is taken alone, or while already holding the
    nonreentrant rec["lock"] (request timeout path) — never the
    reverse, since no code takes rec["lock"] while holding the break
    lock. That fixed ordering keeps first-break recording
    deadlock-free.
    """
    lock = rec.get("break_lock")
    if lock is None:
        # Only hand-built unit-test records lack the lock; their
        # single-threaded first-wins set matches production.
        if rec.get("broken") is None:
            rec["broken"] = code
        return rec.get("broken")
    with lock:
        if rec.get("broken") is None:
            rec["broken"] = code
        return rec.get("broken")


def _dispatch_msp_message(rec: dict, raw: bytes) -> None:
    """Route one inbound frame by record kind.

    Readonly metadata records keep the exact historical dispatch
    (every server notification is fatal except account/changed).
    Worker records take the stateful lifecycle dispatch, which
    ledgers legitimate session/turn/item notifications instead of
    treating them as fatal. Records without a worker kind keep
    readonly semantics, so existing behavior is byte-identical.
    """
    if rec.get("kind") == "worker":
        _dispatch_worker_inbox(rec, raw)
        return
    _dispatch_msp_inbox(rec, raw)


def _dispatch_msp_inbox(rec: dict, raw: bytes) -> None:
    """Classify one host message; protocol violations break the host.

    Strict JSON-RPC 2.0 envelope: exactly one of result/error with a
    matching pending id, each id accepted exactly once (a second
    response for an id the host already answered is a duplicate and
    breaks the host). Server requests are never answered. Server
    notifications are never trusted: account/changed invalidates the
    account binding immediately, and any other notification breaks
    the host — silence is not consent. The FIRST break wins:
    later bytes never overwrite the recorded cause.
    """
    if rec.get("broken"):
        return
    if len(rec["inbox"]) >= _MSP_INBOX_MAX_MESSAGES:
        _msp_break(rec, "inbox-overflow")
        return
    obj = _decode_msp_object(raw)
    if not isinstance(obj, dict):
        _msp_break(rec, "malformed")
        return
    if obj.get("jsonrpc") != "2.0":
        _msp_break(rec, "malformed")
        return
    has_id = "id" in obj
    has_method = "method" in obj
    has_result = "result" in obj
    has_error = "error" in obj
    if has_method and has_id:
        # A server request expects an answer. Never answer approvals
        # or server RPC: fail the host closed instead.
        _msp_break(rec, "server-request")
        return
    if has_method and not has_id:
        if obj.get("method") == "account/changed":
            _msp_break(rec, "account-changed")
        else:
            _msp_break(rec, "server-notification")
        return
    if has_id and (has_result ^ has_error):
        ident = obj["id"]
        if isinstance(ident, bool) or not isinstance(ident, int):
            _msp_break(rec, "malformed")
            return
        if ident in rec.get("seen", ()):
            _msp_break(rec, "duplicate-response")
            return
        if ident not in rec["pending"]:
            _msp_break(rec, "unknown-id")
            return
        rec["inbox"].append(obj)
        rec.setdefault("seen", set()).add(ident)
        return
    _msp_break(rec, "malformed")


def _msp_stdout_reader(rec: dict) -> None:
    # stdout_eof is set ONLY on a real clean EOF. A break for any
    # other reason (overflow, malformed bytes, read error) leaves it
    # False: closing code must not mistake a capped stream for a
    # drained one.
    buf = bytearray()
    while True:
        try:
            chunk = rec["proc"].stdout.read(65536)
        except (OSError, ValueError):
            break
        if not chunk:
            # Physical EOF is honest telemetry, but an
            # unterminated residue is a truncated stream, never a
            # clean close: only complete \n-terminated lines are
            # messages (terminated blank lines are skipped
            # mid-stream; ANY unterminated tail at EOF, even
            # whitespace-only, breaks the host as truncated-line).
            rec["stdout_eof"] = True
            if buf:
                _msp_break(rec, "truncated-line")
            break
        if not isinstance(chunk, (bytes, bytearray)):
            _msp_break(rec, "malformed")
            break
        rec["stdout_bytes"] = rec.get("stdout_bytes", 0) + len(chunk)
        if rec["stdout_bytes"] > _MSP_STDOUT_MAX_BYTES:
            _msp_break(rec, "stdout-overflow")
            break
        buf += chunk
        try:
            while True:
                msg, consumed = _split_msp_line(buf)
                if msg is None:
                    break
                del buf[:consumed]
                if msg == b"":
                    continue
                _dispatch_msp_message(rec, msg)
                if rec.get("broken"):
                    break
        except MspError as exc:
            _msp_break(rec, exc.code or "malformed")
            break
        if rec.get("broken"):
            break


def _msp_stderr_reader(rec: dict) -> None:
    # Drain only: stderr content is never retained or logged.
    # The byte count is honest telemetry about chatter volume.
    # stderr_eof is set ONLY on a real clean EOF. Past the cap the
    # reader stops consuming and marks the host broken: an unbounded
    # flood is not drained to completion and never counts as clean.
    total = 0
    while True:
        try:
            chunk = rec["proc"].stderr.read(65536)
        except (OSError, ValueError):
            break
        if not chunk:
            rec["stderr_eof"] = True
            break
        total += len(chunk)
        rec["stderr_bytes"] = total
        if total > _MSP_STDERR_MAX_BYTES:
            _msp_break(rec, "stderr-overflow")
            break


def _msp_write_all(proc, data: bytes, deadline: float) -> None:
    """Bounded stdin write over a real nonblocking fd with deadlines.

    No blocking-write fallback exists: stdin without a real fd, or a
    platform without nonblocking stdio, refuses instead of hanging.
    Payloads are tiny by contract.
    """
    try:
        fd = proc.stdin.fileno()
    except Exception as exc:
        raise MspError("stdin has no real file descriptor: "
                       f"refusing blocking write ({exc})",
                       code="transport") from exc
    try:
        import fcntl
        import selectors
    except ImportError as exc:
        raise MspError("nonblocking stdio unavailable: refusing "
                       "blocking write", code="transport") from exc
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except OSError as exc:
        raise MspError(f"stdin not writable: {exc}",
                       code="transport") from exc
    view = memoryview(data)
    sel = selectors.DefaultSelector()
    try:
        sel.register(fd, selectors.EVENT_WRITE)
        while len(view) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MspError("stdin write timed out",
                               code="timeout")
            if not sel.select(min(remaining, 1.0)):
                continue
            try:
                sent = os.write(fd, view)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise MspError(f"stdin write failed: {exc}",
                               code="transport") from exc
            view = view[sent:]
    finally:
        try:
            sel.close()
        except Exception:
            pass


def _msp_resolve_snapshot(binary):
    """Resolve the configured pinned binary (IO boundary for tests).

    The production path calls resolve_pinned_muse, which raises
    OSError on any failure (unconfigured pin, symlink/launcher,
    drift). Tests monkeypatch this seam — never the public spawn
    signature, which takes no injection callbacks.
    """
    try:
        return resolve_pinned_muse(binary)
    except OSError as exc:
        raise MspError(f"pinned Muse binary unavailable: {exc}",
                       code="binary-unavailable") from exc


def _msp_build_wrapper(root: str, snap: dict, *,
                     profile: str = "product",
                     home=None) -> dict:
    """Build the single native wrapper argv (IO boundary for tests).

    The production path creates the ncontain offline policy inside
    the fresh owned root and returns wrapper_argv — which itself
    revalidates the recorded root/policy/executable identity before
    constructing the argv — plus the policy identity for later live
    revalidation. The policy command runs the pinned binary itself;
    the caller appends the fixed `serve` flags after it. Containment
    failures refuse; there is no unwrapped fallback. Tests
    monkeypatch this seam; the public spawn signature takes no
    wrapper callback.

    profile "subscription" builds the contained
    subscription-worker policy (product A plus exactly the F2
    named read exception) for the given account home instead;
    anything else refuses. The default keeps existing callers
    on the product policy.
    """
    from . import ncontain as _ncontain
    if profile not in ("product", "subscription"):
        raise MspError(f"unknown worker profile: {profile!r}",
                       code="bad-input")
    try:
        st = os.lstat(snap["real_path"])
    except (OSError, KeyError, TypeError) as exc:
        raise MspError(f"pinned binary unreadable: {exc}",
                       code="binary-unavailable") from exc
    exe_pin = {"path": snap["real_path"],
               "real_path": snap["real_path"],
               "sha256": snap["sha256"], "size": snap["size"],
               "dev": st.st_dev, "ino": st.st_ino,
               "mode": oct(stat.S_IMODE(st.st_mode)),
               "offline_only": True, "admits_worker": False}
    try:
        if profile == "subscription":
            policy = _ncontain.create_subscription_worker_policy(
                root, exe_pin, home)
        else:
            policy = _ncontain.create_offline_policy(
                root, exe_pin)
        argv = _ncontain.wrapper_argv(policy, snap["real_path"])
    except Exception as exc:
        if isinstance(exc, MspError):
            raise
        raise MspError(f"offline wrapper unavailable: {exc}",
                       code="containment") from exc
    return {"argv": list(argv),
            "policy": {"path": policy["policy_path"],
                       "sha256": policy["policy_sha256"],
                       "size": policy["policy_size"],
                       "dev": policy["policy_dev"],
                       "ino": policy["policy_ino"]}}


def _msp_spawn_process(cmd, env, cwd):
    """Spawn the owned child (IO boundary for tests).

    The production path is subprocess.Popen with pipes, an owned
    session, and no text decoding. Tests monkeypatch this seam with
    a fake backed by real os.pipes; the parsers, registry, and
    ownership checks under test stay live.
    """
    return subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=cwd, env=env,
                            start_new_session=True, text=False,
                            bufsize=0)


def spawn_msp_host(root, binary=None, *, timeout_s=60) -> str:
    """Spawn the real pinned MSP host; return a private handle id.

    ALWAYS resolves the configured pinned binary itself (no caller
    override) and ALWAYS wraps the child in exactly one ncontain
    offline policy built for the fresh owned root (no unwrapped
    fallback, no caller argv, no injection callbacks). root must be
    a fresh, empty owned 0700 directory; only the child-env subdirs
    and the wrapper policy are created inside it. Raises MspError
    (fail-closed) instead of returning a bad host.
    """
    timeout_s = _msp_validate_timeout(timeout_s, "spawn")
    canon = _check_msp_root(root)
    if "META_API_KEY" in os.environ:
        raise MspError("META_API_KEY is present and would take "
                       "priority over subscription login: refusing "
                       "before spawn (presence, not value)",
                       code="api-key-present")
    if "MUSE_CONTRIBUTOR_MODE" in os.environ:
        raise MspError("MUSE_CONTRIBUTOR_MODE is present and would "
                       "fight the pinned contributor-ON request: "
                       "refusing before spawn (presence, not value)",
                       code="contributor-env-present")
    snap = _msp_resolve_snapshot(binary)
    env = _msp_host_env(canon)
    wrapped = _msp_build_wrapper(canon, snap)
    cmd = list(wrapped["argv"]) + ["serve"] + build_host_cmd()[2:]
    try:
        proc = _msp_spawn_process(cmd, env, canon)
    except (OSError, ValueError, TypeError) as exc:
        raise MspError(f"metadata host failed to spawn: {exc}",
                       code="transport") from exc
    import secrets
    nonce = secrets.token_hex(8)
    try:
        root_st = os.lstat(canon)
    except OSError as exc:
        try:
            proc.kill()
        except (OSError, ValueError):
            pass
        raise MspError(f"spawned root unreadable: {exc}",
                       code="unreadable") from exc
    rec = {"proc": proc, "cmd": list(cmd), "root": canon,
           "root_dev": root_st.st_dev, "root_ino": root_st.st_ino,
           "binary": {"path": snap.get("path"),
                      "real_path": snap.get("real_path"),
                      "sha256": snap.get("sha256"),
                      "dev": snap.get("dev"), "ino": snap.get("ino"),
                      "size": snap.get("size")},
           "policy": dict(wrapped["policy"]),
           "env_home": env["HOME"], "nonce": nonce,
           "started_at": time.monotonic(), "inbox": [],
           "pending": {}, "seen": set(), "next_id": 1,
           "broken": None, "stdout_eof": False,
           "stdout_bytes": 0, "stderr_eof": False,
           "stderr_bytes": 0, "closed": False,
           "account_binding": None,
           "lock": threading.Lock(),
           "break_lock": threading.Lock()}
    handle = _msp_handle()
    _MSP_HOSTS[handle] = rec
    out_t = threading.Thread(target=_msp_stdout_reader, args=(rec,),
                             daemon=True)
    err_t = threading.Thread(target=_msp_stderr_reader, args=(rec,),
                             daemon=True)
    out_t.start()
    err_t.start()
    rec["threads"] = (out_t, err_t)
    if proc.poll() is not None:
        msp_close(handle, reason="early-exit")
        raise MspError("metadata host exited immediately",
                       code="host-exited")
    return handle


_MSP_POLICY_MAX_BYTES = 1 << 20


def _msp_snapshot_policy(path: str) -> dict:
    """Bounded nofollow regular-file identity+SHA for wrapper policy.

    Mirrors the _snapshot_binary discipline (lstat, symlink refusal,
    O_NOFOLLOW|O_NONBLOCK open, fstat regular-file proof, bounded
    total read, closing dev/ino/size/mtime/ctime recheck plus a
    nofollow name re-resolution) with policy-appropriate bounds and
    messages. The original binary checker stays untouched.
    Raises OSError on any failure.
    """
    try:
        lst = os.lstat(path)
    except OSError as exc:
        raise OSError(f"wrapper policy missing: {path}: {exc}") from exc
    if stat.S_ISLNK(lst.st_mode):
        raise OSError(f"wrapper policy must be a direct file, not a "
                      f"symlink: {path}")
    try:
        fd = os.open(path,
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise OSError(f"wrapper policy open failed: {path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"wrapper policy not a regular file: {path}")
        if before.st_size < 0 or before.st_size > \
                _MSP_POLICY_MAX_BYTES:
            raise OSError(f"wrapper policy size out of bounds: {path}: "
                          f"{before.st_size}")
        digest = hashlib.sha256()
        remaining = before.st_size + 1
        while remaining > 0:
            try:
                part = os.read(fd, min(65536, remaining))
            except OSError as exc:
                raise OSError(f"wrapper policy bytes unreadable: "
                              f"{path}: {exc}") from exc
            if not part:
                break
            digest.update(part)
            remaining -= len(part)
        else:
            raise OSError(f"wrapper policy larger than bound: {path}")
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns) != (
                before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns):
            raise OSError(f"wrapper policy changed during read: {path}")
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise OSError(f"wrapper policy name no longer resolves: "
                          f"{path}: {exc}") from exc
        if (current.st_dev, current.st_ino) != (
                before.st_dev, before.st_ino):
            raise OSError(f"wrapper policy replaced during read: {path}")
        return {"path": path, "sha256": digest.hexdigest(),
                "dev": before.st_dev, "ino": before.st_ino,
                "size": before.st_size}
    finally:
        os.close(fd)


def _msp_check_live(rec: dict) -> None:
    """Re-prove exit/binary/root/policy identity; never heal drift."""
    proc = rec["proc"]
    try:
        exited = proc.poll() is not None
    except (OSError, ValueError):
        exited = True
    if exited:
        raise MspError("metadata host already exited: refusing "
                       "further requests (reap via close)",
                       code="host-exited")
    try:
        fresh = _snapshot_binary(rec["binary"]["real_path"])
    except OSError as exc:
        raise MspError(f"pinned binary unreadable now: {exc}",
                       code="binary-unreadable") from exc
    if not _same_binary_snapshot(rec["binary"], fresh):
        raise MspError("pinned binary changed since spawn "
                       "(content, size, or inode): refusing rebinding",
                       code="binary-drift")
    try:
        rst = os.lstat(rec["root"])
    except OSError as exc:
        raise MspError(f"owned root unreadable now: {exc}",
                       code="unreadable") from exc
    if (rst.st_dev, rst.st_ino) != (rec["root_dev"],
                                    rec["root_ino"]):
        raise MspError("owned root replaced since spawn: refusing "
                       "rebinding", code="root-changed")
    policy = rec.get("policy") or {}
    try:
        fresh_policy = _msp_snapshot_policy(policy["path"])
    except (OSError, KeyError, TypeError) as exc:
        # The bounded nofollow reader refuses symlinks,
        # non-regular files, and unreadable paths outright:
        # no rebind, no healing.
        raise MspError(f"wrapper policy unreadable now: {exc}",
                       code="policy-changed") from exc
    for key in ("sha256", "dev", "ino", "size"):
        if fresh_policy.get(key) != policy.get(key):
            raise MspError(
                "wrapper policy content changed since spawn "
                f"({key} differs, same inode/size is not enough): "
                "refusing rebinding", code="policy-changed")


def _msp_exchange(rec: dict, method: str, params: dict,
                  timeout: float, allowed) -> object:
    """One strict JSON-RPC 2.0 request/response on an owned record.

    Shared private transport for the readonly metadata factory and
    the stateful worker transport: the record (already resolved
    from its own registry), the closed check, the caller-supplied
    method allowlist, and the single absolute deadline are all
    fixed here. ONE absolute deadline is fixed at entry and carried
    through the identity check, the stdin write, and the response
    wait: there is no per-call budget multiplication.
    Unknown/duplicate ids, malformed bytes, server
    requests/notifications, overflows, exit, drift, or timeout fail
    the host closed (fail-closed) instead of returning a guess.
    """
    if rec.get("closed"):
        raise MspError("MSP host handle is closed",
                       code="unknown-handle")
    if method not in allowed:
        raise MspError(f"MSP method not allowed: {method}",
                       code="bad-method")
    if not isinstance(params, dict):
        raise MspError("MSP params must be an object",
                       code="bad-params")
    timeout = _msp_validate_timeout(timeout, "request")
    deadline = time.monotonic() + timeout
    _msp_check_live(rec)
    with rec["lock"]:
        req_id = rec["next_id"]
        if req_id >= _MSP_ID_MAX:
            raise MspError("MSP request id space exhausted",
                           code="transport")
        rec["next_id"] = req_id + 1
        payload = {"jsonrpc": "2.0", "id": req_id,
                   "method": method, "params": params}
        try:
            raw = json.dumps(payload, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MspError(f"MSP params not encodable: {exc}",
                           code="bad-params") from exc
        if len(raw) > _MSP_WRITE_MAX_BYTES:
            raise MspError("MSP request exceeds 64KiB",
                           code="bad-params")
        rec["pending"][req_id] = method
        try:
            _msp_write_all(rec["proc"], raw + b"\n", deadline)
        except MspError:
            rec["pending"].pop(req_id, None)
            raise
    while True:
        with rec["lock"]:
            if rec.get("broken"):
                rec["pending"].pop(req_id, None)
                raise MspError(
                    f"MSP host broken during {method}: "
                    f"{rec['broken']}", code=rec["broken"])
            # The entry deadline is checked BEFORE any queued
            # response is accepted: a response that only arrived
            # after the absolute entry deadline expired must not be
            # treated as a live answer. The break helper runs under
            # the record lock here; it takes only the leaf break
            # lock, so the fixed lock ordering holds.
            if time.monotonic() >= deadline:
                rec["pending"].pop(req_id, None)
                _msp_break(rec, "timeout")
                raise MspError(f"MSP method {method} timed out",
                               code="timeout")
            for index, obj in enumerate(rec["inbox"]):
                if obj.get("id") == req_id:
                    del rec["inbox"][index]
                    rec["pending"].pop(req_id, None)
                    if "error" in obj:
                        raise MspError(
                            f"MSP method {method} returned an error",
                            code="method-error")
                    return obj.get("result")
            try:
                exited = rec["proc"].poll() is not None
            except (OSError, ValueError):
                exited = True
            if exited and rec.get("stdout_eof"):
                rec["pending"].pop(req_id, None)
                raise MspError(
                    f"MSP host exited during {method} with no "
                    f"response", code="host-exited")
        time.sleep(_MSP_SELECT_GRANULE_S)


def _msp_send_request(handle: str, method: str, params: dict,
                      timeout: float):
    """Send one fixed-method request; return the parsed result.

    Private: readonly-factory handles only, allowlisted metadata
    methods with fixed params. The shared transport contract lives
    in _msp_exchange; this wrapper only fixes the readonly
    registry and its method allowlist.
    """
    return _msp_exchange(_host_record(handle), method, params,
                        timeout, _MSP_METHODS)


def _msp_bind_account(rec: dict, account) -> None:
    """Pin the first observed (state, credential) pair per host.

    Takes the PARSED account result (state/credential_required).
    A later account/read that reports a different pair fails the
    host closed: live auth drift must never silently rebind.
    """
    if not isinstance(account, dict):
        return
    state = account.get("state")
    required = account.get("credential_required")
    pair = (state if isinstance(state, str) else None,
            required if isinstance(required, bool) else None)
    if rec.get("account_binding") is None:
        rec["account_binding"] = pair
        return
    if rec["account_binding"] != pair:
        _msp_break(rec, "account-changed")
        raise MspError("account lane changed mid-host: refusing "
                       "rebind", code="account-changed")


def _msp_notify(rec: dict, method: str, timeout: float) -> None:
    """Fire the one fixed notification (no id, no response expected).

    Shared private notify path for the readonly metadata factory
    and the stateful worker transport: the record (already resolved
    from its own registry) is fixed here. Private: only
    "initialized" with empty params exists in this dialect. Strict
    JSON-RPC 2.0 envelope.
    """
    if rec.get("closed"):
        raise MspError("MSP host handle is closed",
                       code="unknown-handle")
    if method != "initialized":
        raise MspError(f"MSP notification not allowed: {method}",
                       code="bad-method")
    timeout = _msp_validate_timeout(timeout, "request")
    # The absolute end is fixed BEFORE the live identity check, so
    # an expensive binary/policy revalidation cannot stretch the
    # write budget: check and write share this one deadline.
    deadline = time.monotonic() + timeout
    _msp_check_live(rec)
    with rec["lock"]:
        try:
            raw = json.dumps({"jsonrpc": "2.0", "method": method,
                              "params": {}},
                             sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MspError(f"MSP params not encodable: {exc}",
                           code="bad-params") from exc
        if len(raw) > _MSP_WRITE_MAX_BYTES:
            raise MspError("MSP notification exceeds 64KiB",
                           code="bad-params")
        _msp_write_all(rec["proc"], raw + b"\n", deadline)


def _msp_send_notify(handle: str, method: str, timeout: float) -> None:
    """Fire the one fixed notification (no id, no response expected).

    Private: readonly-factory handles only. The shared notify
    contract lives in _msp_notify; this wrapper only fixes the
    readonly registry.
    """
    rec = _host_record(handle)
    if rec.get("closed"):
        raise MspError("MSP host handle is closed",
                       code="unknown-handle")
    _msp_notify(rec, method, timeout)


def collect_msp_metadata(handle: str, *, timeout: float = 30.0,
                         app_version: str = "") -> dict:
    """Run the fixed initialize + four-reads sequence; return parsed reads.

    The ONLY sequencing entrypoint. Sends initialize with the fixed
    clientInfo/capabilities params, the initialized{} notification,
    then the four read-only metadata queries with EMPTY params —
    nothing else can be sent through this path. timeout is ONE
    overall deadline across all six exchanges (each exchange gets
    only the time remaining), not a per-call budget. Raises
    MspError fail-closed; a partial sequence returns nothing.
    """
    timeout = _msp_validate_timeout(timeout, "collect")
    rec = _host_record(handle)
    if rec.get("closed"):
        raise MspError("MSP host handle is closed",
                       code="unknown-handle")
    deadline = time.monotonic() + timeout

    def _remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise MspError("MSP metadata sequence timed out",
                           code="timeout")
        return left

    expected_home = os.path.join(rec["root"], "data", "muse")
    init = parse_msp_initialize(
        _msp_send_request(
            handle, "initialize",
            {"clientInfo": {"name": "openmainty",
                            "version": app_version},
             "capabilities": {"experimentalApi": True,
                              "requestedCapabilities": [],
                              "userInputDialogs": False}},
            _remaining()),
        expected_home)
    _msp_send_notify(handle, "initialized", _remaining())
    account = parse_msp_account(
        _msp_send_request(handle, "account/read", {}, _remaining()))
    _msp_bind_account(rec, account)
    settings = parse_msp_settings(
        _msp_send_request(handle, "settings/read", {}, _remaining()))
    profiles = parse_msp_profiles(
        _msp_send_request(handle, "permissionProfile/list", {},
                          _remaining()))
    servers = parse_msp_servers(
        _msp_send_request(handle, "mcpServer/list", {},
                          _remaining()))
    if time.monotonic() >= deadline:
        raise MspError("MSP metadata sequence exceeded its overall "
                       "deadline after the final response",
                       code="timeout")
    return {"initialize": init, "account": account,
            "settings": settings, "profiles": profiles,
            "mcp": servers}


def _msp_close_rec(rec: dict, reason: str) -> dict:
    """Close stdin (EOF), drain both streams, reap the owned child.

    Shared cleanup contract for the readonly metadata factory and
    the stateful worker transport: ONE bounded cleanup deadline
    (_MSP_CLEANUP_S) covers graceful stdin-EOF, reader joins,
    owned-group kill only if needed, reap, pipe closes (which
    unblock the readers), and a final join. Only the owned
    PID/group is ever signalled. The record is marked closed even
    when cleanup itself fails, so a suspect record can never be
    reused; registry removal stays with the per-registry wrapper.

    "clean" requires ALL of: reaped exit, both reader threads
    joined, real EOF observed on both streams, no kill, and no
    broken flag. Anything else is "unknown" with qualified detail —
    a bare rc0/host exit alone is never clean, and a kill is
    reported as signalled (not as success).
    """
    outcome: dict = {"handle_closed": True, "reason": reason,
                     "state": "unknown", "returncode": None,
                     "signalled": False,
                     "stdout_bytes": int(rec.get("stdout_bytes") or 0),
                     "stderr_bytes": int(rec.get("stderr_bytes") or 0),
                     "stdout_eof": False, "stderr_eof": False,
                     "joined_stdout": False, "joined_stderr": False}
    deadline = time.monotonic() + _MSP_CLEANUP_S
    threads = rec.get("threads") or ()

    def _remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    def _joined() -> tuple:
        alive = [t.is_alive() for t in threads]
        while len(alive) < 2:
            alive.append(True)
        return (not alive[0], not alive[1])

    try:
        try:
            stdin = rec["proc"].stdin
            if stdin is not None:
                try:
                    stdin.close()
                except (OSError, ValueError):
                    pass
        except (OSError, ValueError, AttributeError):
            pass
        for thread in threads:
            thread.join(timeout=min(2.0, _remaining()))
        try:
            proc_alive = rec["proc"].poll() is None
        except (OSError, ValueError):
            proc_alive = True
        threads_alive = any(t.is_alive() for t in threads)
        if proc_alive or threads_alive:
            try:
                if _kill_owned_group(rec["proc"].pid):
                    outcome["signalled"] = True
            except Exception:
                pass
            try:
                outcome["returncode"] = rec["proc"].wait(
                    timeout=_remaining())
            except Exception:
                outcome["returncode"] = None
        else:
            try:
                outcome["returncode"] = rec["proc"].wait(
                    timeout=_remaining())
            except Exception:
                outcome["returncode"] = None
        for stream in ("stdout", "stderr"):
            try:
                getattr(rec["proc"], stream).close()
            except (OSError, ValueError, AttributeError):
                pass
        for thread in threads:
            thread.join(timeout=_remaining())
        joined_out, joined_err = _joined()
        outcome["joined_stdout"] = joined_out
        outcome["joined_stderr"] = joined_err
        outcome["stdout_eof"] = bool(rec.get("stdout_eof"))
        outcome["stderr_eof"] = bool(rec.get("stderr_eof"))
        outcome["stdout_bytes"] = int(rec.get("stdout_bytes") or 0)
        outcome["stderr_bytes"] = int(rec.get("stderr_bytes") or 0)
        if rec.get("broken"):
            outcome["host_broken"] = rec.get("broken")
        # Clean requires the expected normal rc0: a nonzero exit —
        # even with real EOF on both streams and joined readers — is
        # a qualified unknown with the exact return code preserved,
        # never reported as success.
        clean = (outcome["returncode"] == 0
                 and not isinstance(outcome["returncode"], bool)
                 and joined_out and joined_err
                 and outcome["stdout_eof"] and outcome["stderr_eof"]
                 and not outcome["signalled"]
                 and not rec.get("broken"))
        outcome["state"] = "clean" if clean else "unknown"
    finally:
        rec["closed"] = True
    return outcome


def msp_close(handle: str, reason: str = "done") -> dict:
    """Close stdin (EOF), drain both streams, reap the owned child.

    Public readonly-factory close. The shared cleanup contract
    lives in _msp_close_rec; this wrapper only fixes the readonly
    registry removal.
    """
    rec = _host_record(handle)
    try:
        return _msp_close_rec(rec, reason)
    finally:
        rec["closed"] = True
        _MSP_HOSTS.pop(handle, None)


# ---------------------------------------------------------------------------
# NEW WORKER TRANSPORT: stateful app-owned session lifecycle (Phase 1).
#
# This section owns the app-owned worker channel. It is SEPARATE from
# the readonly metadata factory above: worker handles live in their
# own registry, bind one live process/channel, and never accept a
# readonly handle, a forged id, a stale/closed handle, or a different
# PID as authority. Replaying another connection's bytes (including
# a readonly diagnostic transcript) is never identity.
#
# Phase 1 scope: spawn an owned offline worker host, run the fixed
# initialize handshake, bind the observed account lane, start ONE
# owned ephemeral session, read it back, and inventory hooks — all
# with strict schema validation against the pinned 1.4.14 contract.
# Model admission stays UNPROVED: msp_worker_turn always refuses
# with capability-unproved until a genuine Meta-ready native
# profile, a winning credential lane, and contributor/config proof
# exist on THIS alive host. No serialized verified bool, no caller
# auth dict, and no same-argv replay can admit a turn.
#
# Ephemeral profile rules (pinned contract): permissionProfile is
# OMITTED (an ephemeral host refuses any id); config is omitted
# (its mcpServers key only ADDS servers, never suppresses hooks);
# session/read never attaches; Session.path is "" under ephemeral
# and is recorded only — never handed to a filesystem call.
# ---------------------------------------------------------------------------

_MSP_WORKERS: dict = {}

_MSP_WORKER_METHODS = ("initialize", "account/read", "session/start",
                       "session/read", "hook/list", "model/list",
                       "usage/read")

_MSP_WORKER_LIFECYCLE_METHODS = ("session/started",
                                 "session/statusChanged",
                                 "session/listChanged",
                                 "session/modelChanged",
                                 "session/closed", "turn/started",
                                 "turn/completed",
                                 "turn/foregroundCompleted",
                                 "item/started", "item/completed",
                                 "view/gap")

_MSP_WORKER_ID_MAX = 256
_MSP_WORKER_STATUS_MAX = 64
_MSP_WORKER_PATH_MAX = 4096
_MSP_WORKER_PROMPT_MAX_BYTES = 32768
_MSP_WORKER_LIFECYCLE_MAX = 64


def _worker_record(handle: str) -> dict:
    """Resolve an owned worker handle; refuse anything else.

    Forged ids, readonly-factory handles, stale entries, and
    closed handles all raise unknown-handle: identity is the
    registry entry itself, never a replayed transcript.
    """
    rec = _MSP_WORKERS.get(handle)
    if rec is None:
        raise MspError("unknown worker handle: refusing forged, "
                       "stale, closed, or readonly-factory identity",
                       code="unknown-handle")
    return rec


def _msp_new_command_id() -> str:
    """Mint an SS2.5/SS3.1 idempotency handle (RFC 9562 UUIDv7).

    Layout: 48-bit current Unix milliseconds, 12 random bits
    with the version nibble 7, then 62 random bits with the
    RFC variant. The server never mints command ids:
    session/start and turn/start both require a client-minted
    UUIDv7. Compatible with supported Pythons without
    uuid.uuid7.
    """
    import secrets
    import time
    unix_ms = time.time_ns() // 1000000
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    raw = (unix_ms.to_bytes(6, "big")
           + bytes(((0x7 << 4) | (rand_a >> 8), rand_a & 0xFF))
           + bytes(((0x2 << 6) | (rand_b >> 56),))
           + (rand_b & 0xFFFFFFFFFFFFFF).to_bytes(7, "big"))
    h = raw.hex()
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16],
                               h[16:20], h[20:32])


def _msp_check_uuid(value: object, what: str, *,
                    version: object = None) -> str:
    """Validate a UUID identity string, never a bare non-empty.

    Requires the 8-4-4-4-12 hex/dash shape; when version is
    given, the version nibble must match exactly (server-minted
    session ids are UUIDv7). Returns the value unchanged.
    """
    if not isinstance(value, str) or len(value) != 36:
        raise MspError(f"{what} is not a UUID identity",
                       code="malformed")
    if (value[8] != "-" or value[13] != "-" or value[18] != "-"
            or value[23] != "-"):
        raise MspError(f"{what} is not a UUID identity",
                       code="malformed")
    hexpart = (value[0:8] + value[9:13] + value[14:18]
               + value[19:23] + value[24:36])
    for ch in hexpart:
        if ch not in "0123456789abcdefABCDEF":
            raise MspError(f"{what} is not a UUID identity",
                           code="malformed")
    if version is not None and value[14] != version:
        raise MspError(f"{what} is not a UUID version {version} "
                       "identity", code="malformed")
    if value[19] not in "89abAB":
        raise MspError(f"{what} is not an RFC 4122 variant "
                       "identity", code="malformed")
    return value


def _worker_check_owner(rec: dict) -> None:
    """Prove the live handle still owns its original child.

    The recorded Popen object, child pid, and spawning process
    must all match, and the nonce must equal the spawn pin: a
    substituted proc object, a pid drift, cross-parent use, or a
    corrupt/tampered nonce refuses before any wire write. Close
    calls this too, but on failure reclaims only the verified
    original child (or refuses without signals) — a legitimate
    exit still reaps cleanly.
    """
    if rec.get("proc") is not rec.get("_proc_ref"):
        raise MspError("worker child process object was "
                       "replaced: refusing a substituted child",
                       code="proc-replaced")
    pid = rec["proc"].pid
    if not isinstance(pid, int) or pid != rec.get("child_pid"):
        raise MspError("worker child pid drifted: refusing "
                       "rebinding",
                       code="proc-pid-mismatch")
    if os.getpid() != rec.get("owner_pid"):
        raise MspError("worker handle used from a non-owner "
                       "process: refusing cross-parent use",
                       code="owner-mismatch")
    nonce = rec.get("nonce")
    if not isinstance(nonce, str) or len(nonce) != 16:
        raise MspError("worker handle nonce is corrupt: "
                       "refusing",
                       code="nonce-corrupt")
    if nonce != rec.get("nonce_pin"):
        raise MspError("worker handle nonce does not match the "
                       "spawn pin: refusing a tampered handle",
                       code="nonce-mismatch")


def _worker_send_request(handle: str, method: str, params: dict,
                         timeout: float):
    """Send one fixed-method request on an owned worker handle.

    Private: closed handles refuse before any wire write; the live
    child/process binding is re-proved on every request; only
    _MSP_WORKER_METHODS with fixed params may be sent. The shared
    transport contract lives in _msp_exchange.
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    _worker_check_owner(rec)
    return _msp_exchange(rec, method, params, timeout,
                        _MSP_WORKER_METHODS)


def _worker_send_notify(handle: str, method: str, timeout: float) -> None:
    """Fire the one fixed notification on an owned worker handle.

    Private: closed handles refuse before any wire write.
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    _worker_check_owner(rec)
    _msp_notify(rec, method, timeout)


def _worker_session_params(workspace_root: str) -> dict:
    """Build fixed session/start params for one owned root.

    Sends exactly workspaceRoot plus the pinned constants
    providerId "meta" and modelId "muse-spark-1.3": in the
    observed run the CLI flags did not establish the requested
    session provider binding (provider-mismatch; actual field
    values were not retained and no null was observed), so both
    are sent as explicit pinned request constants and both
    returned fields are still strictly validated. The constants
    are fixed, never caller-controlled. sessionId is
    server-minted (a retained id would conflict), and
    approvalMode, permissionProfile, and config stay omitted (an
    ephemeral host refuses any permissionProfile id); omission
    is not proof of their effective values — effective approval,
    config, and native readiness remain unproved. Only
    commandId is added later by the start call.
    """
    if not isinstance(workspace_root, str) or not workspace_root:
        raise MspError("worker workspace root must be a "
                       "nonempty string", code="bad-input")
    if "\x00" in workspace_root:
        raise MspError("worker workspace root contains NUL",
                       code="bad-input")
    if len(workspace_root) > _MSP_WORKER_PATH_MAX:
        raise MspError("worker workspace root exceeds 4096 bytes",
                       code="bad-input")
    if not os.path.isabs(workspace_root):
        raise MspError("worker workspace root must be absolute",
                       code="bad-input")
    if os.path.normpath(workspace_root) != workspace_root:
        raise MspError("worker workspace root must be canonical",
                       code="bad-input")
    try:
        st = os.lstat(workspace_root)
    except OSError as exc:
        raise MspError("worker workspace root is not observable: "
                       f"{exc}", code="bad-input") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise MspError("worker workspace root is not a directory",
                       code="bad-input")
    return {"workspaceRoot": workspace_root,
            "providerId": "meta",
            "modelId": "muse-spark-1.3"}


def parse_msp_session(result: dict, workspace_root: str) -> dict:
    """Validate a served Session object and bind it to one root.

    Enforces the pinned 1.4.14 required members: activeTurnId must
    be present (null when idle), sessionId/providerId/modelId/
    workspaceRoot/status/turnCount/createdAt/updatedAt must verify.
    providerId must be exactly "meta", modelId exactly
    "muse-spark-1.3", and workspaceRoot must equal the requested
    canonical root — a null/omitted workspace is an unverified
    binding and refuses, never a pass. path is recorded only and
    is NEVER handed to a filesystem call (under the ephemeral
    profile it is the empty string). Unknown additive members are
    ignored, never authority.
    """
    if not isinstance(result, dict):
        raise MspError("session result must be an object",
                       code="malformed")
    for key in ("activeTurnId", "createdAt", "forkedFrom",
                "modelId", "path", "providerId", "sessionId",
                "status", "turnCount", "updatedAt",
                "workspaceRoot"):
        if key not in result:
            raise MspError(f"session result missing {key}",
                           code="malformed")
    sid = _msp_check_uuid(result.get("sessionId"),
                          "session id", version="7")
    if len(sid) > _MSP_WORKER_ID_MAX:
        raise MspError("session id is invalid", code="malformed")
    if result["providerId"] != "meta":
        raise MspError("session provider is not meta: refusing "
                       "cross-provider binding",
                       code="provider-mismatch")
    if result["modelId"] != "muse-spark-1.3":
        raise MspError("session model is not muse-spark-1.3: "
                       "refusing cross-model binding",
                       code="model-mismatch")
    ws = result["workspaceRoot"]
    if not isinstance(ws, str) or not ws:
        raise MspError("session workspace is unverified "
                       "(null/absent): refusing unbound session",
                       code="workspace-unverified")
    if ws != workspace_root:
        raise MspError("session workspace does not match the "
                       "requested root: refusing rebind",
                       code="workspace-mismatch")
    if not isinstance(result["activeTurnId"], (str, type(None))):
        raise MspError("session activeTurnId is invalid",
                       code="malformed")
    status = result["status"]
    if (not isinstance(status, str) or not status
            or len(status) > _MSP_WORKER_STATUS_MAX):
        raise MspError("session status is invalid", code="malformed")
    if (not isinstance(result["turnCount"], int)
            or isinstance(result["turnCount"], bool)):
        raise MspError("session turnCount is invalid",
                       code="malformed")
    for key in ("createdAt", "updatedAt"):
        if (not isinstance(result[key], str) or not result[key]
                or len(result[key]) > 512):
            raise MspError(f"session {key} is invalid",
                           code="malformed")
    path = result["path"]
    if not isinstance(path, str) or len(path) > _MSP_WORKER_PATH_MAX:
        raise MspError("session path is invalid", code="malformed")
    forked = result["forkedFrom"]
    if forked is not None and not isinstance(forked, dict):
        raise MspError("session forkedFrom is invalid",
                       code="malformed")
    return {"id": sid, "provider_id": "meta",
            "model_id": "muse-spark-1.3", "workspace_root": ws,
            "status": status,
            "active_turn_id": result["activeTurnId"],
            "turn_count": result["turnCount"],
            "path_recorded_only": path,
            "created_at": result["createdAt"],
            "updated_at": result["updatedAt"]}


def parse_msp_session_start(result: dict, workspace_root: str,
                            command_id: str):
    """Validate a session/start result and bind its session.

    Requires result.session (a full Session object bound to the
    requested root) and an opaque viewCursor string. A fresh
    session must be idle: a non-null activeTurnId refuses with
    session-not-idle instead of binding a running session.
    """
    if not isinstance(result, dict):
        raise MspError("session/start result must be an object",
                       code="malformed")
    if "session" not in result or "viewCursor" not in result:
        raise MspError("session/start result is missing "
                       "session/viewCursor", code="malformed")
    if (not isinstance(result["viewCursor"], str)
            or not result["viewCursor"]
            or len(result["viewCursor"]) > 65536):
        raise MspError("session/start viewCursor is invalid",
                       code="malformed")
    bound = parse_msp_session(result["session"], workspace_root)
    if bound["active_turn_id"] is not None:
        raise MspError("fresh session is not idle: refusing to "
                       "bind a running session",
                       code="session-not-idle")
    if bound["status"] != "idle":
        raise MspError("fresh session status is not idle: "
                       "refusing to bind a non-idle session",
                       code="session-not-idle")
    if bound["turn_count"] != 0:
        raise MspError("fresh session already has completed "
                       "turns: refusing to bind a used session",
                       code="session-not-idle")
    if bound["path_recorded_only"] != "":
        raise MspError("fresh ephemeral session must have an empty "
                       "path: refusing a non-empty startup session",
                       code="session-not-idle")
    bound["view_cursor"] = result["viewCursor"]
    bound["command_id"] = command_id
    return bound


def parse_msp_session_read(result: dict, workspace_root: str,
                           session_id: str) -> dict:
    """Validate a session/read result for one bound session.

    session/read never attaches: this is a point-in-time metadata
    read. Requires history/pendingRequests/session/viewCursor;
    queuedInputs absent means empty (never an error). Items and
    view snapshots are counted, never retained; cursors are
    opaque and relayed, never parsed.
    """
    if not isinstance(result, dict):
        raise MspError("session/read result must be an object",
                       code="malformed")
    for key in ("history", "pendingRequests", "session",
                "viewCursor"):
        if key not in result:
            raise MspError(f"session/read result missing {key}",
                           code="malformed")
    history = result["history"]
    if not isinstance(history, dict):
        raise MspError("session/read history is invalid",
                       code="malformed")
    for key in ("items", "mode", "snapshot"):
        if key not in history:
            raise MspError("session/read history is incomplete",
                           code="malformed")
    if not isinstance(history["mode"], str):
        raise MspError("session/read history mode is invalid",
                       code="malformed")
    items = history["items"]
    if items is not None and not isinstance(items, list):
        # Schema anyOf array/null: a scalar is malformed, but null
        # (snapshot/none modes serve no items) is valid.
        raise MspError("session/read history items is neither a "
                       "list nor null", code="malformed")
    item_count = len(items) if isinstance(items, list) else None
    snapshot = history["snapshot"]
    if snapshot is not None and not isinstance(snapshot, dict):
        raise MspError("session/read history snapshot is neither "
                       "an object nor null", code="malformed")
    pending = result["pendingRequests"]
    if not isinstance(pending, list):
        raise MspError("session/read pendingRequests is invalid",
                       code="malformed")
    queued = result.get("queuedInputs", [])
    if not isinstance(queued, list):
        raise MspError("session/read queuedInputs is invalid",
                       code="malformed")
    if (not isinstance(result["viewCursor"], str)
            or not result["viewCursor"]
            or len(result["viewCursor"]) > 65536):
        raise MspError("session/read viewCursor is invalid",
                       code="malformed")
    bound = parse_msp_session(result["session"], workspace_root)
    if bound["id"] != session_id:
        raise MspError("session/read returned a different session: "
                       "refusing rebind", code="session-mismatch")
    return {"session": bound,
            "history_mode": history["mode"],
            "history_items": item_count,
            "history_snapshot_present": history["snapshot"]
            is not None,
            "pending_requests": len(pending),
            "queued_inputs": len(queued),
            "view_cursor": result["viewCursor"],
            "has_blocked_prompt": result.get("blockedPrompt")
            is not None,
            "has_last_turn": result.get("lastTurn") is not None,
            "has_recovery_observation":
                result.get("recoveryObservation") is not None,
            "has_worktree": result.get("worktree") is not None}


def parse_msp_hook_list(result: dict) -> dict:
    """Validate a hook/list result: count rows, retain nothing.

    HookCatalogEntry rows carry per-handler identity the client
    must not echo: only the row count is observed. Unknown method
    (provisional on this host) refuses upstream as method-error;
    malformed rows refuse here.
    """
    if not isinstance(result, dict):
        raise MspError("hook/list result must be an object",
                       code="malformed")
    if "hooks" not in result:
        raise MspError("hook/list result is missing hooks",
                       code="malformed")
    hooks = result["hooks"]
    if not isinstance(hooks, list) or len(hooks) > 1024:
        raise MspError("hook/list hooks are invalid",
                       code="malformed")
    malformed = 0
    for entry in hooks:
        if not isinstance(entry, dict):
            malformed += 1
    if malformed:
        raise MspError("hook/list carries malformed rows",
                       code="malformed")
    return {"count": len(hooks)}


def parse_msp_turn_start(result: dict, command_id: str) -> dict:
    """Validate a turn/start ack against the submitted command.

    Phase 1 never sends turn/start (admission is unproved); this
    validator is the typed plumbing for the Phase 2 turn path.
    commandId echo, disposition, startedNewTurn, status, and
    turnId are all required: take the turn id from the ack, never
    derive it.
    """
    if not isinstance(result, dict):
        raise MspError("turn/start result must be an object",
                       code="malformed")
    for key in ("commandId", "disposition", "startedNewTurn",
                "status", "turnId"):
        if key not in result:
            raise MspError(f"turn/start result missing {key}",
                           code="malformed")
    if result["commandId"] != command_id:
        raise MspError("turn/start command echo mismatch: "
                       "refusing foreign ack",
                       code="command-mismatch")
    if not isinstance(result["disposition"], str):
        raise MspError("turn/start disposition is invalid",
                       code="malformed")
    if not isinstance(result["startedNewTurn"], bool):
        raise MspError("turn/start startedNewTurn is invalid",
                       code="malformed")
    if not isinstance(result["status"], str):
        raise MspError("turn/start status is invalid",
                       code="malformed")
    turn_id = _msp_check_uuid(result.get("turnId"), "turn/start turnId")
    if len(turn_id) > _MSP_WORKER_ID_MAX:
        raise MspError("turn/start turnId is invalid",
                       code="malformed")
    return {"command_id": command_id,
            "disposition": result["disposition"],
            "started_new_turn": result["startedNewTurn"],
            "status": result["status"], "turn_id": turn_id}


def spawn_msp_worker(root: str, binary: Optional[str] = None, *,
                     timeout_s: float = 60,
                     profile: str = "product") -> str:
    """Spawn an owned OFFLINE worker host and return its handle.

    Separate registry and lifecycle from the readonly metadata
    factory: the returned handle binds exactly one live process,
    one native wrapper policy, one pinned binary snapshot, and
    one owned root. Fresh caller-owned SSD homes only (never the
    real user home, keychain, target repos, or network); META_API
    key presence refuses exactly like the readonly path. The host
    starts offline (deny-all network, owned-root writes only) —
    this is Phase 1 session-lifecycle capability, NOT model
    admission: no model turn is authorized by spawning.

    profile "subscription" wraps the child in the contained
    subscription-worker policy instead (product A plus exactly
    the F2 named read exception, gate (2) root301) and points
    the child HOME and XDG_CONFIG_HOME at the real account
    home; museHome, cache, and TMPDIR stay owned. The default
    "product" keeps existing behaviour unchanged. Unknown
    profiles refuse.
    """
    if profile not in ("product", "subscription"):
        raise MspError(f"unknown worker profile: {profile!r}",
                       code="bad-input")
    timeout_s = _msp_validate_timeout(timeout_s, "worker spawn")
    canon = _check_msp_root(root)
    if "META_API_KEY" in os.environ:
        raise MspError("refusing worker host spawn with an API "
                       "key in the parent environment",
                       code="api-key-present")
    if "MUSE_CONTRIBUTOR_MODE" in os.environ:
        raise MspError("refusing worker host spawn with a "
                       "contributor-mode var in the parent "
                       "environment",
                       code="contributor-env-present")
    snap = _msp_resolve_snapshot(binary)
    env = _msp_host_env(canon)
    worker_home = None
    worker_exceptions = None
    if profile == "subscription":
        from . import ncontain as _ncontain
        try:
            worker_home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError as exc:
            raise MspError("worker account home unresolvable: "
                           f"{exc}", code="unreadable") from exc
        try:
            worker_exceptions = \
                _ncontain.subscription_worker_exceptions(
                    worker_home)
        except _ncontain.ContainmentError as exc:
            raise MspError(f"worker profile unavailable: {exc}",
                           code="containment") from exc
        env["HOME"] = worker_home
        env["XDG_CONFIG_HOME"] = os.path.join(
            worker_home, ".config")
        wrapped = _msp_build_wrapper(
            canon, snap, profile="subscription",
            home=worker_home)
    else:
        wrapped = _msp_build_wrapper(canon, snap)
    cmd = list(wrapped["argv"]) + ["serve"] + build_host_cmd()[2:]
    try:
        proc = _msp_spawn_process(cmd, env, canon)
    except (OSError, ValueError, TypeError) as exc:
        raise MspError(f"worker host failed to spawn: {exc}",
                       code="transport") from exc
    handle = _msp_handle()
    try:
        root_st = os.lstat(canon)
    except OSError as exc:
        # The child already exists: a bare kill without wait
        # would leave a zombie. Best-effort kill (when the seam
        # provides one) followed by a bounded owned wait/reap.
        kill = getattr(proc, "kill", None)
        if callable(kill):
            try:
                kill()
            except (OSError, ValueError):
                pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        raise MspError("worker root vanished during spawn: "
                       f"{exc}", code="unreadable") from exc
    import secrets
    nonce = secrets.token_hex(8)
    rec = {
        "proc": proc, "_proc_ref": proc,
        "child_pid": proc.pid, "owner_pid": os.getpid(),
        "cmd": list(cmd), "root": canon,
        "root_dev": root_st.st_dev, "root_ino": root_st.st_ino,
        "binary": {"path": snap.get("path"),
                   "real_path": snap.get("real_path"),
                   "sha256": snap.get("sha256"),
                   "dev": snap.get("dev"), "ino": snap.get("ino"),
                   "size": snap.get("size")},
        "policy": dict(wrapped["policy"]),
        "env_home": env["HOME"], "nonce": nonce,
        "nonce_pin": nonce,
        "started_at": time.monotonic(), "inbox": [],
        "pending": {}, "seen": set(), "next_id": 1,
        "broken": None, "stdout_eof": False,
        "stdout_bytes": 0, "stderr_eof": False,
        "stderr_bytes": 0, "closed": False,
        "account_binding": None,
        "lock": threading.Lock(),
        "break_lock": threading.Lock(),
        "kind": "worker", "worker_session": None,
        "worker_account": None, "lifecycle": [],
        "lifecycle_pending": [],
        "worker_bootstrapped": False,
        "worker_session_closed": False,
        "profile": profile,
        "worker_home": worker_home,
        "worker_profile_exceptions": worker_exceptions,
    }
    _MSP_WORKERS[handle] = rec
    out_t = threading.Thread(target=_msp_stdout_reader, args=(rec,),
                             daemon=True)
    err_t = threading.Thread(target=_msp_stderr_reader, args=(rec,),
                             daemon=True)
    out_t.start()
    err_t.start()
    rec["threads"] = (out_t, err_t)
    if proc.poll() is not None:
        try:
            msp_worker_close(handle, reason="early-exit")
        except MspError:
            pass
        raise MspError("worker host exited immediately: refusing "
                       "a non-running host as authority",
                       code="host-exited")
    return handle


def _worker_lifecycle_session_id(method: str, params: dict):
    """Extract the owning session id of a lifecycle broadcast.

    session/started and session/listChanged carry the full Session
    object under params.session (never a top-level sessionId);
    every other session-scoped broadcast carries sessionId.
    Returns None when no string id is present (fail-closed
    downstream).
    """
    if not isinstance(params, dict):
        return None
    if method in ("session/started", "session/listChanged"):
        session = params.get("session")
        if not isinstance(session, dict):
            return None
        sid = session.get("sessionId")
        return sid if isinstance(sid, str) else None
    sid = params.get("sessionId")
    return sid if isinstance(sid, str) else None


def _worker_lifecycle_shape_ok(method: str, params: dict) -> bool:
    """Check the required pinned shape of a lifecycle broadcast.

    session/statusChanged requires sessionId/status/viewCursor;
    session/closed requires reason/sessionId/viewCursor (viewCursor
    nullable). Other listed methods only need their session
    identity, already extracted. Returns False on any violation.
    """
    if method == "session/statusChanged":
        return (isinstance(params.get("sessionId"), str)
                and isinstance(params.get("status"), str)
                and "viewCursor" in params
                and (isinstance(params.get("viewCursor"), str)
                     or params.get("viewCursor") is None))
    if method == "session/closed":
        return (isinstance(params.get("reason"), str)
                and isinstance(params.get("sessionId"), str)
                and "viewCursor" in params
                and (isinstance(params.get("viewCursor"), str)
                     or params.get("viewCursor") is None))
    return True


def _dispatch_worker_inbox(rec: dict, raw: bytes) -> None:
    """Bounded typed lifecycle handling for worker handles.

    session/start auto-subscribes this connection, so legitimate
    session/turn/item/view-gap broadcasts arrive: they are
    validated, checked against the bound session, and ledgered
    (method + session id only, cap 64, no payload retention)
    instead of breaking the host. account/changed breaks like the
    readonly path; a durable model change under us breaks with
    model-changed rather than continuing on a changed model;
    unknown methods, unknown shapes, foreign session ids, and
    closed-session drift break fail-closed. Response-shaped
    frames reuse the exact readonly response validation.
    """
    if rec.get("broken"):
        return
    obj = _decode_msp_object(raw)
    if not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0":
        _msp_break(rec, "malformed")
        return
    has_id = "id" in obj
    has_method = "method" in obj
    if has_id or not has_method:
        _dispatch_msp_inbox(rec, raw)
        return
    method = obj.get("method")
    if method == "account/changed":
        _msp_break(rec, "account-changed")
        return
    if method not in _MSP_WORKER_LIFECYCLE_METHODS:
        _msp_break(rec, "server-notification")
        return
    params = obj.get("params")
    if not isinstance(params, dict):
        _msp_break(rec, "malformed")
        return
    sid = _worker_lifecycle_session_id(method, params)
    if sid is None or not _worker_lifecycle_shape_ok(method,
                                                     params):
        _msp_break(rec, "malformed")
        return
    # Lifecycle-state discipline: worker_session, worker_session_closed,
    # lifecycle, and lifecycle_pending are guarded by break_lock (the
    # established leaf: taken alone here, or under rec["lock"] on the
    # request timeout path — never the reverse). _msp_break is always
    # called AFTER releasing it (non-reentrant). session_start takes
    # the same lock around reconcile+assign, so a reader closure
    # arriving between reconcile and assignment cannot resurrect.
    action = None
    with rec["break_lock"]:
        bound = rec.get("worker_session")
        bound_id = (bound.get("id")
                    if isinstance(bound, dict) else None)
        if bound_id is None:
            pending = rec.get("lifecycle_pending")
            if not isinstance(pending, list):
                pending = rec["lifecycle_pending"] = []
            if len(pending) >= _MSP_WORKER_LIFECYCLE_MAX:
                # A full pending set followed by a closure must not
                # truncate the critical event away and green-bind:
                # overflow fails the host closed.
                action = "lifecycle-overflow"
            else:
                entry = {"method": method, "session_id": sid,
                         "detail": None}
                if method in ("session/started",
                              "session/listChanged"):
                    session_obj = params.get("session")
                    entry["detail"] = (dict(session_obj)
                                       if isinstance(session_obj,
                                                     dict)
                                       else None)
                elif method == "session/statusChanged":
                    entry["detail"] = params.get("status")
                elif method == "session/closed":
                    entry["detail"] = params.get("reason")
                pending.append(entry)
                ledger = rec.get("lifecycle")
                if not isinstance(ledger, list):
                    ledger = rec["lifecycle"] = []
                ledger.append({"method": method,
                               "session_id": sid})
                del ledger[:-_MSP_WORKER_LIFECYCLE_MAX]
        elif sid != bound_id:
            action = "session-mismatch"
        elif method == "session/modelChanged":
            action = "model-changed"
        else:
            if method == "session/closed":
                rec["worker_session"] = None
                rec["worker_session_closed"] = True
            if (method == "session/statusChanged"
                    and params.get("status") == "notLoaded"):
                rec["worker_session"] = None
                rec["worker_session_closed"] = True
            ledger = rec.get("lifecycle")
            if not isinstance(ledger, list):
                ledger = rec["lifecycle"] = []
            ledger.append({"method": method, "session_id": sid})
            del ledger[:-_MSP_WORKER_LIFECYCLE_MAX]
    if action is not None:
        _msp_break(rec, action)


def msp_worker_bootstrap(handle: str, *, timeout: float = 30.0,
                         app_version: str = "") -> dict:
    """Run the fixed initialize handshake and bind the account.

    Sends initialize (fixed params, ephemeral rules), the
    initialized notification, then account/read, and binds the
    observed credential lane as accountLogin-candidate or
    unknown — a lane label only, never billing/contributor
    proof. No session, no model turn: bootstrap alone authorizes
    nothing beyond metadata observation on THIS alive host.
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    if not isinstance(app_version, str) or len(app_version) > 128:
        raise MspError("worker app version must be a short "
                       "string", code="bad-input")
    timeout = _msp_validate_timeout(timeout, "worker bootstrap")
    deadline = time.monotonic() + timeout

    def _remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise MspError("worker bootstrap timed out",
                           code="timeout")
        return left

    if rec.get("worker_bootstrapped"):
        raise MspError("worker host already bootstrapped: refusing "
                       "reinitialize/rebind on the same handle",
                       code="already-bootstrapped")
    expected_home = os.path.join(rec["root"], "data", "muse")
    init = parse_msp_initialize(
        _worker_send_request(handle, "initialize", {
            "clientInfo": {
                "name": "openmainty",
                "version": app_version,
            },
            "capabilities": {
                "experimentalApi": True,
                "requestedCapabilities": [],
                "userInputDialogs": False,
            },
        }, _remaining()), expected_home)
    _worker_send_notify(handle, "initialized", _remaining())
    account = parse_msp_account(
        _worker_send_request(handle, "account/read", {},
                             _remaining()))
    _msp_bind_account(rec, account)
    lane, note = msp_credential_lane(account)
    rec["worker_account"] = dict(account)
    if time.monotonic() >= deadline:
        raise MspError("worker bootstrap timed out",
                       code="timeout")
    rec["worker_bootstrapped"] = True
    return {"initialize": init, "account": dict(account),
            "credential_lane": lane, "credential_note": note}


def _worker_check_workspace(rec: dict, workspace_root: str):
    """Prove the session workspace is owned by THIS worker root.

    Accepts only the worker root itself or a canonical descendant:
    normalized, absolute, with NO symlink in any component
    (realpath must equal the input — an intermediate symlink to a
    sibling/target/home tree refuses), same device as the root,
    owned by the same uid, a real directory, and empty (fresh
    startup state, checked with a bounded single-entry scan, not
    an unbounded listing). Foreign, real-home, target, sibling,
    replaced, or non-empty roots refuse BEFORE any wire write.
    Returns the pinned (dev, ino) identity for later rechecks.
    The native wrapper is session containment, not a substitute
    for this declared ownership proof.
    """
    root = rec["root"]
    if os.path.commonpath([root, workspace_root]) != root:
        raise MspError("worker workspace is outside the owned "
                       "worker root: refusing foreign root",
                       code="workspace-foreign")
    if os.path.realpath(workspace_root) != workspace_root:
        raise MspError("worker workspace contains a symlink: "
                       "refusing a non-canonical owned root",
                       code="workspace-symlink")
    try:
        root_st = os.lstat(root)
        ws_st = os.lstat(workspace_root)
    except OSError as exc:
        raise MspError("worker workspace is not observable: "
                       f"{exc}", code="bad-input") from exc
    if not stat.S_ISDIR(ws_st.st_mode):
        raise MspError("worker workspace is not a directory",
                       code="bad-input")
    if ws_st.st_dev != root_st.st_dev:
        raise MspError("worker workspace is on a different device "
                       "than the owned root",
                       code="workspace-foreign")
    if ws_st.st_uid != root_st.st_uid:
        raise MspError("worker workspace is not owned by the root "
                       "owner", code="workspace-foreign")
    try:
        scanner = os.scandir(workspace_root)
    except OSError as exc:
        raise MspError("worker workspace is not listable: "
                       f"{exc}", code="bad-input") from exc
    try:
        for _entry in scanner:
            raise MspError("worker workspace is not empty: "
                           "refusing a non-fresh startup state",
                           code="workspace-not-empty")
    finally:
        scanner.close()
    return (ws_st.st_dev, ws_st.st_ino)


def _worker_recheck_workspace(rec: dict, bound: dict) -> None:
    """Re-prove the pinned workspace identity on a live handle.

    The canonical path must still resolve symlink-free to itself
    and its (dev, ino) must still equal the pin recorded at
    session bind. A replaced/swapped workspace refuses before any
    further wire use; this never rebinds or heals.
    """
    ws = bound["workspace_root"]
    if os.path.realpath(ws) != ws:
        raise MspError("worker workspace no longer resolves "
                       "symlink-free: refusing a replaced root",
                       code="workspace-changed")
    try:
        st = os.lstat(ws)
    except OSError as exc:
        raise MspError("worker workspace is no longer "
                       f"observable: {exc}",
                       code="workspace-changed") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise MspError("worker workspace is no longer a "
                       "directory", code="workspace-changed")
    if ((st.st_dev, st.st_ino) != (bound.get("workspace_dev"),
                                   bound.get("workspace_ino"))):
        raise MspError("worker workspace identity changed since "
                       "bind: refusing a replaced root",
                       code="workspace-changed")


def _worker_reconcile_pending(rec: dict, bound: dict,
                               workspace_root: str) -> None:
    """Reconcile pre-bind lifecycle events against the new binding.

    Within one fresh owned host, no pre-ack event may be discarded
    and then admitted as a good binding: a foreign session id, a
    closed/notLoaded session, a model change, or a started session
    whose real Session fields (provider/model/workspace) do not
    verify refuses here with NO resurrection. Matching benign
    entries were already ledgered; the pending stash is cleared.
    Called with break_lock held (same discipline as the reader
    dispatch), so no interleaving closure can slip between
    reconcile and assignment.
    """
    pending = rec.get("lifecycle_pending")
    if not isinstance(pending, list):
        pending = rec["lifecycle_pending"] = []
    for entry in pending:
        if not isinstance(entry, dict):
            rec["lifecycle_pending"] = []
            raise MspError("pre-bind lifecycle entry is corrupt: "
                           "refusing binding",
                           code="malformed")
        if entry.get("session_id") != bound["id"]:
            rec["lifecycle_pending"] = []
            raise MspError("foreign session event before the "
                           "start ack: refusing binding",
                           code="session-mismatch")
        method = entry.get("method")
        if method == "session/closed":
            rec["lifecycle_pending"] = []
            raise MspError("session closed before the start ack: "
                           "refusing resurrection on this handle",
                           code="session-closed-before-ack")
        if method == "session/modelChanged":
            rec["lifecycle_pending"] = []
            raise MspError("model changed before the start ack: "
                           "refusing binding on this handle",
                           code="model-changed")
        if (method == "session/statusChanged"
                and entry.get("detail") == "notLoaded"):
            rec["lifecycle_pending"] = []
            raise MspError("session notLoaded before the start "
                           "ack: refusing binding",
                           code="session-not-loaded")
        if method in ("session/started", "session/listChanged"):
            # Validate the actual Session fields, not the sid
            # alone: provider/model/workspace drift before ack
            # refuses with the honest validator code.
            session_obj = entry.get("detail")
            if not isinstance(session_obj, dict):
                rec["lifecycle_pending"] = []
                raise MspError("pre-bind session object is "
                               "missing: refusing binding",
                               code="malformed")
            parse_msp_session(session_obj, workspace_root)
    rec["lifecycle_pending"] = []


def msp_worker_session_start(handle: str, *, workspace_root: str,
                             timeout: float = 30.0) -> dict:
    """Start ONE owned ephemeral session and bind it to the handle.

    Params carry exactly {commandId, workspaceRoot, providerId,
    modelId}: providerId "meta" and modelId "muse-spark-1.3" are
    explicit pinned request constants, since the observed run did
    not establish the requested session provider binding from
    its CLI flags. No sessionId (server-minted), no
    permissionProfile (an ephemeral host refuses any id), no
    approvalMode/config (remain omitted; effective values
    unproved). commandId is a fresh UUIDv7 per call. The result
    session must verify provider meta, model muse-spark-1.3, the
    requested root, and idle status, or the start refuses.
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    timeout = _msp_validate_timeout(timeout, "worker session start")
    if not rec.get("worker_bootstrapped"):
        raise MspError("worker host is not bootstrapped: refusing "
                       "session/start before the initialize handshake",
                       code="not-bootstrapped")
    if rec.get("worker_session") is not None:
        raise MspError("worker session already bound: close the "
                       "handle for a fresh worker session",
                       code="session-already-bound")
    if rec.get("worker_session_closed"):
        raise MspError("a worker session already closed on this "
                       "handle: refusing rebind",
                       code="session-closed")
    command_id = _msp_new_command_id()
    params = _worker_session_params(workspace_root)
    ws_dev, ws_ino = _worker_check_workspace(
        rec, params["workspaceRoot"])
    params["commandId"] = command_id
    try:
        result = _worker_send_request(handle, "session/start",
                                      params, timeout)
        bound = parse_msp_session_start(
            result, params["workspaceRoot"], command_id)
        bound["workspace_dev"] = ws_dev
        bound["workspace_ino"] = ws_ino
        with rec["break_lock"]:
            _worker_reconcile_pending(
                rec, bound, params["workspaceRoot"])
            rec["worker_session"] = bound
            rec["worker_session_closed"] = False
    except MspError:
        # Any start attempt/failure after the pre-wire checks is
        # non-rebindable on this handle: the host may hold a
        # session we cannot safely bind, so a retry must use a
        # fresh handle instead of duplicating here.
        with rec["break_lock"]:
            rec["worker_session"] = None
            rec["worker_session_closed"] = True
        raise
    return dict(bound)


def msp_worker_session_read(handle: str, *,
                            timeout: float = 30.0) -> dict:
    """Read back the bound session without attaching.

    session/read takes no lease, loads nothing, and subscribes to
    nothing: a point-in-time metadata read whose result is
    re-validated against the bound session id and root.
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    bound = rec.get("worker_session")
    if not isinstance(bound, dict) or not bound.get("id"):
        raise MspError("no worker session bound to this handle",
                       code="no-session")
    _worker_recheck_workspace(rec, bound)
    timeout = _msp_validate_timeout(timeout, "worker session read")
    result = _worker_send_request(
        handle, "session/read",
        {"sessionId": bound["id"], "excludeItems": True}, timeout)
    seen = parse_msp_session_read(result, bound["workspace_root"],
                                  bound["id"])
    return _worker_accept_read(rec, bound, seen)


def _worker_accept_read(rec: dict, bound: dict, seen: dict) -> dict:
    """Accept a validated read result into the live binding.

    Under break_lock (same discipline as the reader dispatch):
    if the host broke, the session closed, or the binding
    changed while the read was in flight, the stale result is
    discarded with a typed refusal instead of resurrecting
    worker_session. Otherwise the bind-time workspace pin is
    carried onto the refreshed session and the binding is
    updated. Pure state transition — no I/O, no healing.
    """
    with rec["break_lock"]:
        if rec.get("broken"):
            raise MspError("worker host broke during read: "
                           "discarding stale result",
                           code=rec.get("broken")
                           or "host-broken")
        cur = rec.get("worker_session")
        if cur is None or rec.get("worker_session_closed"):
            raise MspError("worker session closed during read: "
                           "discarding stale result",
                           code="session-closed")
        if not isinstance(cur, dict) or cur.get("id") != bound.get(
                "id"):
            raise MspError("worker session binding changed "
                           "during read: discarding stale result",
                           code="session-mismatch")
        if seen["session"]["status"] == "notLoaded":
            rec["worker_session"] = None
            rec["worker_session_closed"] = True
            raise MspError("session/read reports the bound "
                           "session is notLoaded: unbinding "
                           "without rebind",
                           code="session-not-loaded")
        # Carry the bind-time workspace pin forward: the recheck
        # before the read already proved the live workspace still
        # matches it, and the next read re-proves it again. Never
        # reset pins to unpinned.
        seen["session"]["workspace_dev"] = bound.get(
            "workspace_dev")
        seen["session"]["workspace_ino"] = bound.get(
            "workspace_ino")
        rec["worker_session"] = seen["session"]
    return seen


def msp_worker_hooks(handle: str, *, timeout: float = 30.0) -> dict:
    """Inventory the bound session's installed hook rows.

    A query, not a command: result carries one row per installed
    handler per event. Only the row count is observed; row
    contents are dropped, never echoed. Provisional against the
    pinned host: an unknown-method refusal is the honest result
    until Phase-2 live validation confirms the surface. The
    observed count is recorded on the worker record, so the
    canary's hooks leg reflects a real hook/list run (a refused
    query records nothing).
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    bound = rec.get("worker_session")
    if not isinstance(bound, dict) or not bound.get("id"):
        raise MspError("no worker session bound to this handle",
                       code="no-session")
    _worker_recheck_workspace(rec, bound)
    timeout = _msp_validate_timeout(timeout, "worker hooks")
    result = _worker_send_request(handle, "hook/list",
                                  {"sessionId": bound["id"]},
                                  timeout)
    parsed = parse_msp_hook_list(result)
    rec["worker_hook_count"] = parsed["count"]
    return {"session_id": bound["id"], "count": parsed["count"]}


def msp_worker_model_list(handle: str, *,
                          timeout: float = 30.0) -> dict:
    """Query model/list for the BOUND session's effective model row.

    The sessionId sent is always the bound session id: callers
    never choose the identity the isActive claim binds to (same
    discipline as hook/list and session/read). modelId/providerId
    echo only; labels, cost, and variants are dropped by the
    parser. An unknown-method refusal is the honest result until
    Phase-2 live validation confirms the surface.
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    bound = rec.get("worker_session")
    if not isinstance(bound, dict) or not bound.get("id"):
        raise MspError("no worker session bound to this handle",
                       code="no-session")
    _worker_recheck_workspace(rec, bound)
    timeout = _msp_validate_timeout(timeout, "worker model/list")
    result = _worker_send_request(handle, "model/list",
                                  {"sessionId": bound["id"]},
                                  timeout)
    return parse_msp_model_list(result, bound["id"])


def msp_worker_usage(handle: str, *,
                     timeout: float = 30.0) -> dict:
    """Query usage/read: the host's last-observed subscription window.

    Host-level (no session needed): tier + window/weekly blocks +
    arrival stamp, never a model call. An omitted usage member is
    truthful absence ({observed: False}). An unknown-method
    refusal is the honest result until Phase-2 live validation
    confirms the surface. The parsed window is recorded on the
    worker record, so the canary's usage leg reflects a real
    usage/read run (a refused query records nothing).
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    if not rec.get("worker_bootstrapped"):
        raise MspError("worker host is not bootstrapped",
                       code="not-bootstrapped")
    timeout = _msp_validate_timeout(timeout, "worker usage")
    result = _worker_send_request(handle, "usage/read", {},
                                  timeout)
    parsed = parse_msp_usage(result)
    rec["worker_usage"] = parsed
    return parsed


def canary_admission(handle: str) -> dict:
    """Stage-A verdict: admit ONE bounded canary turn, never authority.

    Pure over the registry record (no RPC): the session binding,
    credential lane, recorded hook count, recorded usage, config
    flag, and the single-use consumption flag. worker_hook_count
    and worker_usage are populated only by real hook/list and
    usage/read runs, so those legs clear honestly; production
    NEVER sets config_established in this slice, so production
    always refuses on config-unestablished. OPEN: the criterion
    that would establish config (an observer-backed config
    identity bound to the same alive host) is not designed yet;
    nothing here may set the flag by hand. On the single
    admitting combination the consumption flag is set: a second
    call refuses with canary-already-consumed. Full admission is
    never granted here (see msp_worker_capability, still hard
    False).
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    missing = []
    bound = rec.get("worker_session")
    session_ok = isinstance(bound, dict) and bool(bound.get("id"))
    if not session_ok:
        missing.append("session-unbound")
    account = rec.get("worker_account")
    lane = "unknown"
    if isinstance(account, dict):
        _label, _known = msp_credential_lane(account)
        lane = _label
    if lane != "accountLogin-candidate":
        missing.append("winning-credential")
    config_ok = rec.get("config_established") is True
    if not config_ok:
        missing.append("config-unestablished")
    hooks = rec.get("worker_hook_count")
    if hooks is None:
        missing.append("hooks-unobserved")
    elif hooks != 0:
        missing.append("hooks-present")
    usage = rec.get("worker_usage")
    if isinstance(usage, dict) and usage.get("observed"):
        missing.append("usage-already-fresh")
    if rec.get("worker_canary_consumed") is True:
        missing.append("canary-already-consumed")
    admitted = not missing
    if admitted:
        rec["worker_canary_consumed"] = True
    return {"admitted_once": admitted,
            "session_bound": session_ok,
            "credential_lane": lane,
            "config_established": config_ok,
            "hooks_zero": hooks == 0,
            "usage_needed": not (
                isinstance(usage, dict)
                and usage.get("observed")),
            "missing": missing}


def msp_worker_capability(handle: str) -> dict:
    """Report the honest capability verdict for a worker handle.

    Phase 1 always returns admitted False with the exact missing
    legs: a winning credential lane observed on THIS alive host,
    a Meta-ready native profile (no offline deny-all policy is
    model-network admission), and contributor/config proof
    (requested flags are not observed enforcement). Session
    binding is reported, never authority. No caller-supplied
    verified bool or auth dict can change this verdict.
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    missing = []
    account = rec.get("worker_account")
    if isinstance(account, dict):
        lane, note = msp_credential_lane(account)
    else:
        lane, note = "unknown", ("no account observation on this "
                                 "worker host")
    if lane != "accountLogin-candidate":
        missing.append("winning-credential: observed lane is %r "
                       "(%s)" % (lane, note))
    missing.append("metaready-native-profile: unproved (the "
                   "offline deny-all wrapper is session-lifecycle "
                   "capability only, not model-network admission)")
    missing.append("contributor-config: unproved (requested CLI "
                   "flags only; effective config unobserved)")
    bound = rec.get("worker_session")
    session_bound = isinstance(bound, dict) and bool(bound.get("id"))
    return {"admitted": False,
            "credential_lane": lane,
            "credential_note": note,
            "session_bound": session_bound,
            "session_status": (bound or {}).get("status"),
            "missing": missing}


def msp_worker_turn(handle: str, prompt: str, *,
                    timeout: float = 600) -> dict:
    """Request one bounded worker model turn. ALWAYS REFUSES.

    Phase 1 capability gate: input shape validates first
    (bad-input, no wire write), then the capability verdict
    refuses with capability-unproved because no winning
    credential, Meta-ready native profile, or contributor/config
    proof exists on this alive host. The turn transport itself
    lands in Phase 2; this path performs NO wire write and
    accepts NO caller-supplied verified bool or auth dict —
    admission cannot be argued into existence.
    """
    rec = _worker_record(handle)
    if rec.get("closed"):
        raise MspError("MSP worker handle is closed",
                       code="unknown-handle")
    if not isinstance(prompt, str) or not prompt:
        raise MspError("worker prompt must be a nonempty string",
                       code="bad-input")
    if len(prompt.encode("utf-8")) > _MSP_WORKER_PROMPT_MAX_BYTES:
        raise MspError("worker prompt exceeds 32768 UTF-8 bytes",
                       code="bad-input")
    _msp_validate_timeout(timeout, "worker turn")
    cap = msp_worker_capability(handle)
    raise MspError("worker model turn refused: capability "
                   "unproved (%s)" % "; ".join(cap["missing"]),
                   code="capability-unproved")


def msp_worker_close(handle: str, reason: str = "done") -> dict:
    """Close stdin (EOF), drain both streams, reap the owned child.

    Worker-registry close. The shared cleanup contract lives in
    _msp_close_rec; this wrapper only fixes the worker registry
    removal. Owns and reaps the actual child, joins readers,
    invalidates the handle even on cleanup failure — and never
    signals a substituted or unowned pid: when the live owner
    check fails at close time, cleanup falls back to the
    originally recorded child (whose pid still matches the spawn
    pin) instead of whatever object currently sits in the
    record. A legitimate exit still reaps cleanly: no live
    check gates cleanup itself. When even the original binding
    cannot be proved (foreign process, corrupt ref), cleanup
    refuses with the owner-check code after a signal-free
    release, never entering kill/reap.
    """
    rec = _worker_record(handle)
    try:
        try:
            _worker_check_owner(rec)
        except MspError as exc:
            # Cleanup must never signal a substituted or unowned
            # pid. Only the ORIGINAL Popen object, with pid still
            # equal to the spawn pin, reached from the CURRENT
            # original parent process, may be reclaimed and enter
            # bounded original cleanup: a replaced working pointer
            # alone does not invalidate those independent original
            # ownership facts. Anything else (foreign process,
            # corrupt original ref) invalidates the handle with an
            # honest qualified refusal and no signals: owned-known
            # streams are released and readers joined, but no kill,
            # no wait, no reap of a process we cannot prove is
            # ours.
            ref = rec.get("_proc_ref")
            try:
                ref_pid = ref.pid
            except Exception:
                ref_pid = None
            if (not isinstance(ref_pid, int)
                    or ref_pid != rec.get("child_pid")
                    or os.getpid() != rec.get("owner_pid")):
                _worker_close_no_signal(rec)
                raise MspError("worker close refused unsafe "
                               f"cleanup: {exc.code}",
                               code=exc.code) from exc
            rec["proc"] = ref
        return _msp_close_rec(rec, reason)
    finally:
        rec["closed"] = True
        _MSP_WORKERS.pop(handle, None)


def _worker_close_no_signal(rec: dict) -> None:
    """Release only originally owned resources, never signal/reap.

    Best-effort pipe close plus bounded reader join for a handle
    whose ownership cannot be proved. Only the originally
    recorded child's streams are touched — never the currently
    substituted foreign proc's. No poll, no wait, no kill.
    """
    ref = rec.get("_proc_ref")
    for stream in ("stdin", "stdout", "stderr"):
        try:
            getattr(ref, stream).close()
        except Exception:
            pass
    threads = rec.get("threads") or ()
    for thread in threads:
        try:
            if thread.is_alive():
                thread.join(timeout=5)
        except Exception:
            pass
