"""Local dashboard: conversations + maintenance console (loopback only).

`openmainty serve --port 8901` exposes run state, drift, logs, and saved
chat sessions, plus operator-initiated actions: chat turns, drift/issue
checks, and background maintain/resume jobs. It binds 127.0.0.1 only and
must stay that way: POST endpoints spend subscription quota and mutate
local state (never target checkouts, which jobs only read via worktrees).

House rules still apply: jobs produce drafts; nothing posts, merges,
deploys, or sends channel messages by itself. Quota-spending endpoints
enforce the subscriber gate (no `muse` CLI, or META_API_KEY set → 403).
For login-persistent operation see `openmainty app install` (launchd,
macOS).
"""
from __future__ import annotations

import html
import json
import os
import secrets
import subprocess
import queue as _queue
import threading
import time
from concurrent import futures as _futures
from concurrent.futures import TimeoutError as _FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import batches, branches, chat, cli, deps, doctor, events, gc, graph, manifests, muse, paths, preflight, repos, runner, sched, store
from .util import Cancelled


def _page() -> str:
    """Dashboard UI. Read per request so UI edits need no restart."""
    try:
        return (Path(__file__).resolve().parent / "dashboard.html").read_text(encoding="utf-8")
    except OSError:
        return ("<!doctype html><html><body><p>dashboard.html is missing next "
                "to serve.py; reinstall openmainty.</p></body></html>")


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#12141a"/><g fill="none" stroke="#eceef1" stroke-width="4.8" stroke-linecap="round"><ellipse cx="32" cy="32" rx="24" ry="11.2" transform="rotate(-24 32 32)" opacity=".55"/><circle cx="32" cy="32" r="8.5"/></g><circle cx="51.7" cy="17.6" r="5" fill="#8fb4e8"/></svg>"""


class ApiError(Exception):
    def __init__(self, status: int, message: str, code=None):
        super().__init__(message)
        self.status = status
        self.message = message
        # Optional machine-readable failure code. Only endpoints
        # that explicitly set it emit "code"; every other raise
        # behaves exactly as before (no code key meaning change).
        self.code = code


# ---- G9: bounded request handling (never wedge on a blocked file) ----
# A wedged checkout volume can park a handler thread inside open()
# forever. Two guards keep that from killing the app: (1) a semaphore
# caps concurrent non-reserve handlers — when full, new requests get a
# fast 503 with Retry-After instead of queueing another stuck thread;
# (2) checkout filesystem access runs in a small worker pool with a
# timeout, so a blocked open() fails closed instead of parking the
# handler. Reserve paths (static page, favicon, /api/app) never take
# the semaphore: they touch no checkout files and stay answerable.
_HANDLER_CAP = 32
_HANDLER_RESERVE_PATHS = ("/", "/favicon.svg", "/api/app")
_HANDLER_SEM = threading.BoundedSemaphore(_HANDLER_CAP)
_HANDLER_LOCK = threading.Lock()
_HANDLER_IN_FLIGHT = 0
_BUSY_MSG = "지금 요청이 많아 잠시 바쁩니다 — 잠시 뒤 다시 시도하세요"
_RETRY_AFTER_S = "5"
# ---- G9b: per-read checkout threads, no shared pool (never wedge a
# healthy root because another one is stuck) ----
# A wedged checkout volume can park a thread inside open() forever.
# The G9 shared pool let a few parked reads starve every other root,
# so G9b removes it: every checkout_io call starts its own daemon
# thread at once. There is no queue, so unstarted work does not
# exist — "cancel on timeout" holds because a timed-out call never
# runs later; only its already-running thread may stay parked.
# Two guards bound the damage:
# (1) per-path fail-fast: while a read on a path is parked and the
# file is unchanged (same device/inode/mode), a new read on that
# path fails fast (a lock plus a stat, well under 50 ms) with the
# same honest message instead of parking another thread. A replaced
# file has a new identity, so recovery probes normally and a success
# clears the root — no restart needed.
# (2) a global cap on parked (timed-out, still blocked) threads.
# Bounds, stated plainly: HTTP-triggered reads are bounded by the
# handler cap (32 in flight); at most _CHECKOUT_PARKED_CAP (16)
# timed-out-but-still-blocked threads are tolerated — the next stuck
# read fails fast naming the stuck paths (code "checkout_busy"),
# never the healthy path being asked about. Per-path state entries
# are pruned past 1024 idle keys.
_CHECKOUT_PARKED_CAP = 16
_CHECKOUT_STATE_PRUNE_AT = 1024
_CHECKOUT_LOCK = threading.Lock()
# path string -> {"stuck": set of futures, "wedged": bool,
#                 "identity": (st_dev, st_ino, st_mode) or None}
_CHECKOUT_STATE: dict = {}
# Futures of timed-out reads whose thread has not returned yet.
_CHECKOUT_STUCK: set = set()
_CHECKOUT_TIMEOUT_S = 5.0


def _checkout_timeout_message(path: str) -> str:
    return (f"저장소 폴더({path})를 읽는 데 응답이 없습니다 — "
            f"외장 디스크 연결이나 macOS 접근 권한을 확인하세요")


def _checkout_identity(p: str):
    """Identity of a path, or None. stat never blocks.

    Includes the directory mtime: replacing a file inside the
    checkout changes the directory mtime while (dev, ino, mode)
    stay the same, and that replacement is exactly the recovery
    the fail-fast must notice instead of failing fast forever.
    """
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mode, st.st_mtime_ns)


def _parked_cap_message(stuck) -> str:
    names = ", ".join(stuck[:5])
    more = f" 외 {len(stuck) - 5}곳" if len(stuck) > 5 else ""
    return ("동시 읽기 상한에 도달했습니다 — 응답 없는 저장소: "
            f"{names}{more}. 외장 디스크 연결이나 macOS 접근 권한을 확인하세요")


def _run_checkout_io(path, fn, args, kwargs, fut):
    try:
        out = fn(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - record, then account
        fut.set_exception(exc)
        ok = False
    else:
        fut.set_result(out)
        ok = True
    # A counted (timed-out, still blocked) thread uncounts itself on return.
    # Any success proves the root is readable right now, so it clears
    # the wedged flag and identity: older stuck entries belong to dead
    # files (a replaced FIFO, a remounted volume) and stay counted,
    # bounded by the global cap, instead of failing fast forever.
    with _CHECKOUT_LOCK:
        if fut in _CHECKOUT_STUCK:
            _CHECKOUT_STUCK.discard(fut)
            st = _CHECKOUT_STATE.get(path)
            if st is not None:
                st["stuck"].discard(fut)
                if ok:
                    st["wedged"] = False
                    st["identity"] = None


def checkout_io(fn, *args, path="", timeout=None, **kwargs):
    """Run checkout filesystem access with a bounded wait (G9b).

    `fn` runs on its own daemon thread started at once; when it does
    not finish within `timeout` seconds (default _CHECKOUT_TIMEOUT_S)
    this raises ApiError(502) with an honest Korean message. A
    timeout is a fail-closed unreadable checkout — never stale data
    presented as fresh, never a retry loop. While a read on `path`
    is parked and the file is unchanged, further reads on `path`
    fail fast without spawning; once _CHECKOUT_PARKED_CAP threads
    are stuck, new stuck reads fail fast (code "checkout_busy")
    naming the stuck paths.
    """
    budget = _CHECKOUT_TIMEOUT_S if timeout is None else timeout
    with _CHECKOUT_LOCK:
        st = _CHECKOUT_STATE.get(path)
        if st is None:
            st = _CHECKOUT_STATE[path] = {"stuck": set(),
                                          "wedged": False,
                                          "identity": None}
        elif len(_CHECKOUT_STATE) > _CHECKOUT_STATE_PRUNE_AT:
            for k in [k for k, s in _CHECKOUT_STATE.items()
                      if k != path and not s["stuck"] and not s["wedged"]]:
                del _CHECKOUT_STATE[k]
        if st["wedged"]:
            ident = _checkout_identity(path)
            if ident is not None and ident == st["identity"]:
                # Same file still parked: fail fast without spawning.
                raise ApiError(502,
                               _checkout_timeout_message(path or "checkout"))
            # Identity changed (replaced) or unreadable: fall through
            # and probe normally; a success clears the root.
        if len(_CHECKOUT_STUCK) >= _CHECKOUT_PARKED_CAP:
            stuck = sorted(p for p, s in _CHECKOUT_STATE.items()
                           if s["stuck"])
            raise ApiError(502, _parked_cap_message(stuck),
                           code="checkout_busy")
    fut = _futures.Future()
    t = threading.Thread(target=_run_checkout_io,
                         args=(path, fn, args, kwargs, fut),
                         daemon=True)
    t.start()
    try:
        return fut.result(timeout=budget)
    except _FuturesTimeout:
        with _CHECKOUT_LOCK:
            if fut.done():
                done = True
            else:
                _CHECKOUT_STUCK.add(fut)
                st = _CHECKOUT_STATE.get(path)
                if st is None:
                    st = _CHECKOUT_STATE[path] = {
                        "stuck": set(), "wedged": False,
                        "identity": None}
                st["stuck"].add(fut)
                st["wedged"] = True
                st["identity"] = _checkout_identity(path)
                done = False
        if done:
            # Finished between the timeout and this check: report the
            # genuine result, never a timeout.
            return fut.result()
        raise ApiError(502,
                       _checkout_timeout_message(path or "checkout"))


def _admit_request(handler, path: str) -> bool:
    """G9 admission: True when the request may proceed.

    Reserve paths are counted but never take the semaphore. Other
    paths take it without blocking; when full this sends a fast 503
    with Retry-After and returns False. Admitted requests are
    released by Handler.handle_one_request's finally block.
    """
    global _HANDLER_IN_FLIGHT
    if path not in _HANDLER_RESERVE_PATHS:
        if not _HANDLER_SEM.acquire(blocking=False):
            body = json.dumps({"error": _BUSY_MSG},
                              ensure_ascii=False).encode()
            handler.send_response(503)
            handler.send_header("Content-Type",
                                "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Retry-After", _RETRY_AFTER_S)
            handler.end_headers()
            try:
                handler.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return False
        handler._g9_held = True
    else:
        handler._g9_held = False
    with _HANDLER_LOCK:
        _HANDLER_IN_FLIGHT += 1
    handler._g9_counted = True
    return True


def _release_request(handler) -> None:
    """Release a slot admitted by _admit_request (G9)."""
    global _HANDLER_IN_FLIGHT
    if getattr(handler, "_g9_counted", False):
        handler._g9_counted = False
        with _HANDLER_LOCK:
            _HANDLER_IN_FLIGHT -= 1
        if getattr(handler, "_g9_held", False):
            handler._g9_held = False
            _HANDLER_SEM.release()


# ---- background jobs (one running at a time in v1) ----

_JOBS: dict = {}
_JOB_CANCEL: dict = {}
_JOBS_LOCK = threading.Lock()

# ---- off-thread summary refresh (single-flight) ----
# POST /api/m2/policy and POST /api/repo/profile used to block for
# 10-26 s on a synchronous snapshot refresh. They now return at once
# with the new state and queue one background refresh; the glance
# poll picks it up. At most one refresh runs, with at most one more
# queued behind it — two quick flips cause one extra refresh, never
# a pile-up.

_REFRESH_LOCK = threading.Lock()
_REFRESH_RUNNING = False
_REFRESH_PENDING = None  # (kind, reason) or None
_REFRESH_GEN = 0
_REFRESH_THREAD = None  # current worker Thread, if any


def _refresh_soon(kind: str, reason: str) -> str:
    """Queue a background snapshot refresh (single-flight).

    kind "summary" refreshes the glance snapshot; kind "all"
    refreshes the batches envelope AND the glance in one pass
    (hold/attest freshness: both screens move together). Returns
    "started" (a worker now runs) or "queued" (one already runs;
    ours follows right after). Never blocks on the refresh."""
    global _REFRESH_RUNNING, _REFRESH_PENDING, _REFRESH_GEN, \
        _REFRESH_THREAD
    with _REFRESH_LOCK:
        if _REFRESH_RUNNING:
            # N1: merge, never downgrade — a queued "all" (hold /
            # attest freshness) survives a later "summary" request,
            # so the batches envelope refresh is never lost.
            prev = _REFRESH_PENDING
            if prev is None:
                _REFRESH_PENDING = (kind, reason)
            else:
                merged = "all" if "all" in (prev[0], kind) \
                    else "summary"
                _REFRESH_PENDING = (merged, reason)
            return "queued"
        _REFRESH_RUNNING = True
        _REFRESH_GEN += 1
        gen = _REFRESH_GEN

    def _worker():
        global _REFRESH_RUNNING, _REFRESH_PENDING
        try:
            pending = None
            cur_kind, cur_reason = kind, reason
            while True:
                try:
                    if cur_kind == "all":
                        batches.refresh_all_snapshots(cur_reason)
                    else:
                        batches.refresh_summary_snapshot(cur_reason)
                except Exception:
                    pass
                with _REFRESH_LOCK:
                    pending = _REFRESH_PENDING
                    _REFRESH_PENDING = None
                    if pending is None:
                        _REFRESH_RUNNING = False
                        return
                    cur_kind, cur_reason = pending
        finally:
            # The normal exit already cleared the flag; this only
            # covers an escaping error. The generation check keeps a
            # stale worker from clearing a newer worker's flag.
            with _REFRESH_LOCK:
                if _REFRESH_GEN == gen:
                    _REFRESH_RUNNING = False

    _th = threading.Thread(target=_worker, daemon=True)
    with _REFRESH_LOCK:
        _REFRESH_THREAD = _th
    _th.start()
    return "started"


def wait_refresh_idle(timeout: float = 10.0) -> bool:
    """Join the background refresh worker (test seam).

    Waits until no refresh worker runs and none is pending —
    the same worker consumes its queued follow-up, and a fresh
    request starts a new worker the loop then joins too. Never
    starts a refresh itself. Returns True when idle, False on
    timeout. Harmless in production (joins only). Never raises.
    """
    import time as _time
    try:
        budget = max(0.0, float(timeout))
    except (TypeError, ValueError):
        budget = 0.0
    deadline = _time.monotonic() + budget
    joined = None
    try:
        while True:
            with _REFRESH_LOCK:
                th = _REFRESH_THREAD
                idle = not _REFRESH_RUNNING \
                    and _REFRESH_PENDING is None
            if idle:
                return True
            if _time.monotonic() >= deadline:
                return False
            if th is not None and th is not joined:
                th.join(timeout=max(0.0,
                                    deadline - _time.monotonic()))
                joined = th
            else:
                _time.sleep(0.01)
    except Exception:  # noqa: BLE001 - a seam never fails the suite
        return False


def refresh_summary_soon(reason: str) -> str:
    """Queue a background summary-snapshot refresh. Returns
    "started" (a worker now runs) or "queued" (one already runs;
    ours follows right after). Never blocks on the refresh."""
    return _refresh_soon("summary", reason)


def refresh_all_soon(reason: str) -> str:
    """Queue a background batches+summary refresh (hold/attest
    freshness). Same single-flight as refresh_summary_soon."""
    return _refresh_soon("all", reason)


def _job_new(kind: str, label: str):
    with _JOBS_LOCK:
        for job in _JOBS.values():
            if job["status"] == "running":
                return None
        jid = "job-" + secrets.token_hex(4)
        from .util import utcnow
        job = {"id": jid, "kind": kind, "label": label, "status": "running",
               "rc": None, "started_at": utcnow(), "ended_at": None, "note": ""}
        _JOBS[jid] = job
        _JOB_CANCEL[jid] = threading.Event()
        finished = sorted((v for v in _JOBS.values() if v["status"] != "running"),
                          key=lambda v: v.get("ended_at", ""))
        for old in finished[:-9]:
            _JOBS.pop(old["id"], None)
            _JOB_CANCEL.pop(old["id"], None)
        return job


def _job_startup_failed(job: dict, note: str) -> None:
    """Close an allocated job slot that never started, terminally.

    `_job_new` reserves a running slot before the cancel-event lookup
    and the worker thread start. Either startup fault must still end
    that slot with the existing terminal contract (status done, rc 1,
    ended_at) so no phantom running record survives; the recovery
    attempt/latest are marked separately by the caller and the lease
    is released by the caller.
    """
    from .util import utcnow
    job["status"] = "done"
    job["rc"] = 1
    job["note"] = note[:300]
    job["ended_at"] = utcnow()
    _JOB_CANCEL.pop(job["id"], None)


def _job_run(job: dict, fn) -> None:
    def target():
        from .util import utcnow
        try:
            rc = fn()
            job["rc"] = rc
            job["note"] = "done" if rc == 0 else f"exit {rc}"
        except Cancelled:
            job["rc"] = 130
            job["note"] = "cancelled by operator"
            job["status"] = "cancelled"
        except SystemExit as exc:
            job["rc"] = 1
            job["note"] = str(exc)[:300]
        except Exception as exc:  # noqa: BLE001 - jobs must always resolve
            job["rc"] = 1
            job["note"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            if job["status"] == "running":
                job["status"] = "done"
            job["ended_at"] = utcnow()
    threading.Thread(target=target, daemon=True).start()


def _require_gate() -> None:
    err = chat.subscription_error()
    if err:
        raise ApiError(403, err)


def _load_profile(repo: str) -> dict:
    try:
        return repos.load(repo)
    except SystemExit as exc:
        raise ApiError(404, str(exc)) from exc


def _resolve_checkout(profile: dict) -> Path:
    from . import pcconfig
    co = (pcconfig.load().get("checkouts", {}) or {}).get(profile["name"])
    if not co:
        raise ApiError(400, f"no checkout linked for '{profile['name']}'")
    path = Path(co)
    # G9: the checkout lives outside OPENMAINTY_HOME (often an external
    # volume) and stat can block there — bound it like every other
    # checkout access. A timeout reads as unreadable (fail-closed).
    if not checkout_io(path.is_dir, path=str(path)):
        raise ApiError(400, f"checkout not found: {path}")
    return path


def api_chat_start(payload: dict) -> dict:
    _require_gate()
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    profile = _load_profile(repo)
    checkout = _resolve_checkout(profile)
    model = _settings_model()
    session = chat.create(repo, payload.get("issue", ""), payload.get("title", ""),
                          muse.agent_info(model=model))
    workdir = chat.ensure_workdir(session, checkout, profile.get("baseBranch", "main"))
    message = (payload.get("message") or "").strip()
    if not message:
        return {"id": session["id"], "reply": None, "wait": False}
    try:
        turn = chat.send(session, profile, message, runner.Executor(model=model), workdir)
    except runner.QuotaHit:
        return {"id": session["id"], "reply": None, "wait": True}
    return {"id": session["id"], "reply": turn["text"], "wait": False}


def _chat_load_or_refuse(sid: str) -> dict:
    # F1: malformed ids are a bad request (400); well-formed but
    # unknown ids stay 404. Never let a traversal id reach the fs.
    try:
        return chat.load(sid)
    except SystemExit as exc:
        msg = str(exc)
        if "invalid chat session id" in msg:
            raise ApiError(400, msg) from exc
        raise ApiError(404, msg) from exc


def api_chat_send(payload: dict) -> dict:
    _require_gate()
    sid = (payload.get("id") or "").strip()
    message = (payload.get("message") or "").strip()
    if not sid or not message:
        raise ApiError(400, "id and message are required")
    session = _chat_load_or_refuse(sid)
    profile = _load_profile(session["repo"])
    checkout = _resolve_checkout(profile)
    workdir = chat.ensure_workdir(session, checkout, profile.get("baseBranch", "main"))
    try:
        turn = chat.send(session, profile, message,
                         runner.Executor(model=_settings_model()), workdir)
    except runner.QuotaHit:
        return {"id": sid, "reply": None, "wait": True}
    return {"id": sid, "reply": turn["text"], "wait": False}


def api_chat_delete(payload: dict) -> dict:
    sid = (payload.get("id") or "").strip()
    if not sid:
        raise ApiError(400, "id is required")
    session = _chat_load_or_refuse(sid)
    checkout = None
    try:
        checkout = _resolve_checkout(_load_profile(session["repo"]))
    except ApiError:
        pass
    try:
        chat.delete(session["id"], checkout)
    except SystemExit as exc:
        # Mismatch / escape: the stored record surprised us — bad
        # request, and nothing was deleted.
        raise ApiError(400, str(exc)) from exc
    return {"deleted": session["id"]}


def _git(args, cwd):
    from .util import run_cmd
    return run_cmd(["git", "-C", str(cwd)] + list(args))


def _detect_github(checkout: Path):
    """owner/repo from the origin remote (local read, no network) or None."""
    import re
    code, out, _ = _git(["remote", "get-url", "origin"], checkout)
    if code != 0:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", out.strip())
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def _detect_branch(checkout: Path) -> str:
    code, out, _ = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], checkout)
    if code == 0 and out.strip():
        return out.strip().split("/")[-1]
    code, out, _ = _git(["branch", "--show-current"], checkout)
    if code == 0 and out.strip():
        return out.strip()
    return "main"


def _register(name: str, github: str, checkout: Path) -> dict:
    """Save an auto-built profile + link its checkout."""
    from . import pcconfig
    try:
        name = repos.validate_name(name)
    except SystemExit as exc:
        raise ApiError(400, str(exc)) from exc
    if name in repos.list_all():
        raise ApiError(409, f"repo '{name}' is already registered")
    knowledge = []
    if (checkout / "AGENTS.md").is_file():
        knowledge.append("AGENTS.md")
    profile = {
        "name": name,
        "github": github,
        "baseBranch": _detect_branch(checkout),
        "knowledge": knowledge,
        "checks": [],
        "e2e": [],
        "release": {},
        "announce": {"medium": False, "discord": False, "slack": False},
    }
    try:
        repos.save(profile)
    except SystemExit as exc:
        raise ApiError(400, str(exc)) from exc
    pc = pcconfig.load()
    pc.setdefault("checkouts", {})[name] = str(checkout)
    pcconfig.save(pc)
    # M3.2 review 5: the first thing a registered repo gets is one
    # scan-only run — next_scan=now, execute stays off, every m2_*
    # toggle stays OFF (unset). The glance shows it within one tick.
    try:
        sched.configure(name, enabled=True)
    except SystemExit:
        pass
    return profile


def api_repo_detect(payload: dict) -> dict:
    """Propose a fastcheck block for a registered repo (M3.2).

    Read-only: lockfiles + manifests in, proposal out. Nothing is
    saved and no tool runs — the existing profile probe button
    confirms tools, and the profile save API validates. Returns the
    proposal plus its validate_fastcheck errors (empty = savable).
    """
    from . import pcconfig
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    try:
        repos.load(repo)
    except SystemExit:
        raise ApiError(404, f"unknown repo: {repo!r}") from None
    checkout = pcconfig.checkout_for(repo)
    if not checkout:
        raise ApiError(400, f"repo '{repo}' has no linked checkout")
    # G9: direct manifest read — bound and fail-closed.
    detection = checkout_io(repos.detect_fastcheck, checkout,
                            path=str(checkout))
    errors = []
    if "unsupported" not in detection:
        errors = repos.validate_fastcheck(
            {"steps": detection.get("steps") or [],
             "toolchain": detection.get("toolchain") or []})
    return {"repo": repo, "detection": detection,
            "validation": errors}


def api_onboard_caps() -> dict:
    """The default caps the onboarding wizard shows (M3.2 review 7).

    Every value is pinned to the real constant; there is no repo
    cap, and the panel says so instead of hiding it.
    """
    from . import m2 as _m2
    return {"scan_interval_s": sched.DEFAULT_INTERVAL_S,
            "prepare_per_tick": 1,
            "refresh_per_tick": 1,
            "refresh_per_day": _m2._REFRESH_PER_DAY,
            "rollup_per_lane_tick": 1,
            "cooldown_blocks": _m2._COOLDOWN_BLOCKS,
            "cooldown_hours": _m2._COOLDOWN_S // 3600,
            "repo_limit": None,
            "repo_limit_note": "등록 수 제한 없음"}


def api_repo_add_local(payload: dict) -> dict:
    raw_path = (payload.get("path") or "").strip()
    if not raw_path:
        raise ApiError(400, "path is required")
    checkout = Path(raw_path).expanduser()
    if not checkout.is_absolute():
        raise ApiError(400, "path must be absolute")
    if not checkout.is_dir():
        raise ApiError(400, f"not a directory: {checkout}")
    if not (checkout / ".git").exists():
        raise ApiError(400, f"not a git repository (no .git): {checkout}")
    github = (payload.get("github") or "").strip() or _detect_github(checkout)
    if not github:
        raise ApiError(400, "could not detect owner/repo from origin; pass github explicitly")
    name = (payload.get("name") or "").strip() or repos.slugify(checkout.name)
    return {"repo": _register(name, github, checkout)}


def _profile_probe_cwd(repo: str):
    """S16: directory the profile probe runs `--version` in — the
    registered checkout when it exists (read-only; `--version`
    writes nothing into it), else None (server cwd). Lets a
    project-pinned manager report its pinned version."""
    try:
        from . import pcconfig as _pc
        cand = _pc.checkout_for(repo)
    except Exception:  # noqa: BLE001 - unknown checkout keeps default
        return None
    try:
        if cand and Path(cand).is_dir():
            return str(cand)
    except OSError:
        pass
    return None


def api_repo_profile(payload: dict) -> dict:
    """M3.0: edit-profile API. GET {repo} reads the profile plus its
    fastcheck validation — side-effect free, NO probes (a GET must
    never run a process; S9 lets GETs through without an Origin).
    POST {repo, probe:true} runs the per-tool --version probes
    explicitly (the UI "도구 확인" button). POST
    {repo, fastcheck, confirm:true} replaces ONLY the fastcheck
    block: validated by repos.validate_fastcheck AND every tool must
    probe ok, else 400 names the failing tool. Source, branches and
    lanes are never touched here. Every save is a Korean timeline
    event (actor owner). Refreshes the glance afterwards (same
    trigger other mutations use)."""
    from . import events as _events
    from . import fastcheck as _fastcheck
    repo = str((payload or {}).get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    try:
        profile = repos.load(repo)
    except SystemExit as exc:
        raise ApiError(404, str(exc)) from None
    if "fastcheck" not in (payload or {}) and not (payload or {}).get(
            "probe", False):
        fc = profile.get("fastcheck") or {}
        return {"repo": repo, "profile": profile,
                "validation": repos.validate_fastcheck(fc),
                "probes": []}
    if "fastcheck" not in (payload or {}):
        fc = profile.get("fastcheck") or {}
        return {"repo": repo, "profile": profile,
                "validation": repos.validate_fastcheck(fc),
                "probes": _fastcheck.probe_profile_tools(
                    fc, cwd=_profile_probe_cwd(repo))}
    if not bool((payload or {}).get("confirm", False)):
        raise ApiError(400, "confirm:true needed to save the profile")
    fc = (payload or {}).get("fastcheck")
    errors = repos.validate_fastcheck(fc)
    if errors:
        raise ApiError(400, "검사 설정 오류: " + "; ".join(errors)[:500])
    probes = _fastcheck.probe_profile_tools(fc if isinstance(fc, dict)
                                            else {},
                                            cwd=_profile_probe_cwd(repo))
    failed = [p for p in probes if not p.get("ok")]
    if failed:
        raise ApiError(400, "도구 확인 실패: " + "; ".join(
            f"{p.get('tool')}({p.get('error')})" for p in failed)[:500])
    old_fc = profile.get("fastcheck") or {}
    profile["fastcheck"] = fc
    try:
        repos.save(profile)
    except SystemExit as exc:
        raise ApiError(400, str(exc)) from None
    # P3: a profile save edits commands that later run unattended —
    # record what changed (step tools + gates, never env values).
    def _sig(block):
        return [f"{((s or {}).get('argv') or ['?'])[0]}"
                f"[{str((s or {}).get('gate') or '?')}]"
                for s in ((block or {}).get("steps") or [])
                if isinstance(s, dict)]
    _events.append(
        repo, "repo-profile", "",
        f"검사 설정 변경: {len(_sig(fc))}단계 · "
        f"도구 {', '.join(sorted({p.get('tool', '') for p in probes}))}",
        f"이전: {', '.join(_sig(old_fc)) or '없음'} → "
        f"새 설정: {', '.join(_sig(fc)) or '없음'}",
        actor="owner")
    # S14: the synchronous refresh blocked this POST for 10+ s on
    # the real home — queue it and return at once with the new state.
    refresh = refresh_summary_soon("repo-profile")
    return {"repo": repo, "profile": profile, "validation": [],
            "probes": probes, "refresh": refresh}


def api_repo_delete(payload: dict) -> dict:
    from . import pcconfig
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    if repo not in repos.list_all():
        raise ApiError(404, f"unknown repo '{repo}'")
    if store.has_active(repo):
        raise ApiError(409, f"repo '{repo}' has an active run; wait or cancel it first")
    repos.delete(repo)
    pc = pcconfig.load()
    pc.get("checkouts", {}).pop(repo, None)
    pcconfig.save(pc)
    return {"deleted": repo}


def _clone_repo(url: str, dest: Path, cancel_event=None,
                _run=None) -> None:
    """Clone for onboarding (M3.2, Root review 1-3).

    Full history with lazy blobs (--filter=blob:none, never
    --depth 1: OpenMainty needs history for merge-base, rev-list and
    candidate cherry-picks, and never unshallows later). Never hangs
    on credentials: GIT_TERMINAL_PROMPT=0, a false askpass, stdin
    DEVNULL and a timeout. `_run` injects the runner for tests (same
    kwargs as util.run_cmd); production always uses run_cmd.
    """
    import os
    import shutil
    import subprocess
    from .util import run_cmd
    run = _run if _run is not None else run_cmd
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_ASKPASS="/usr/bin/false")
    try:
        code, _, err = run(["git", "clone", "--filter=blob:none",
                            url, str(dest)],
                           timeout=600, cancel_event=cancel_event,
                           env=env, stdin=subprocess.DEVNULL)
    except Cancelled:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    if code != 0:
        shutil.rmtree(dest, ignore_errors=True)
        lowered = (err or "").lower()
        if ("authentication failed" in lowered
                or "could not read username" in lowered
                or "permission denied" in lowered
                or "repository not found" in lowered
                or " 403" in (err or "")):
            raise SystemExit("error: 비공개 저장소는 로컬 체크아웃으로 "
                             "추가하세요")
        if code == 124:
            raise SystemExit("error: 클론 시간 초과 — 네트워크를 "
                             "확인하고 다시 시도하세요")
        raise SystemExit(f"error: git clone failed: {err.strip()[-300:]}")


def api_job_clone(payload: dict) -> dict:
    import re
    github = (payload.get("github") or "").strip()
    if "://" in github or "@" in github:
        # A pasted token URL never reaches the network: only
        # owner/repo is accepted, and the clone URL is built here.
        raise ApiError(400, "토큰 포함 URL은 등록할 수 없음 — "
                            "owner/repo만 입력하세요")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github or ""):
        raise ApiError(400, "github must look like owner/repo")
    owner, repo = github.split("/")
    dest = paths.home() / "checkouts" / owner / repo
    if dest.exists():
        raise ApiError(409, f"directory already exists: {dest}")
    job = _job_new("clone", github)
    if job is None:
        raise ApiError(409, "another job is already running")
    name = (payload.get("name") or "").strip() or repos.slugify(github.split("/")[1])
    ev = _JOB_CANCEL[job["id"]]
    url = f"https://github.com/{github}.git"

    def _run():
        _clone_repo(url, dest, ev)
        _register(name, github, dest)
        return 0

    _job_run(job, _run)
    return {"job": job}


def _settings_model() -> str:
    from . import pcconfig
    return pcconfig.load().get("model") or muse.DEFAULT_MODEL


def api_settings_get() -> dict:
    from . import pcconfig
    return {"workspace_root": str(pcconfig.workspace_root()),
            "model": _settings_model(),
            "billing": muse.billing_path()}


def api_settings_set(payload: dict) -> dict:
    import re
    from . import pcconfig
    out = {}
    if "workspace_root" in payload:
        try:
            root = pcconfig.set_workspace_root(payload.get("workspace_root") or "")
        except SystemExit as exc:
            raise ApiError(400, str(exc)) from exc
        out["workspace_root"] = str(root)
    if "model" in payload:
        model = (payload.get("model") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", model):
            raise ApiError(400, "invalid model id")
        pc = pcconfig.load()
        pc["model"] = model
        pcconfig.save(pc)
        out["model"] = model
    if not out:
        raise ApiError(400, "nothing to set (workspace_root, model)")
    return out


def api_browse(payload: dict) -> dict:
    """List one directory for the file picker. Names only, no contents."""
    raw = (payload.get("path") or "").strip() or str(Path.home())
    show_hidden = str(payload.get("hidden") or "").lower() in ("1", "true", "yes")
    try:
        target = Path(raw).expanduser().resolve()
    except OSError as exc:
        raise ApiError(400, f"bad path: {exc}") from exc
    # G9: the file picker can point at a wedged volume — stat and
    # listing run behind the bounded helper. Timeout is fail-closed.
    if not checkout_io(target.is_dir, path=str(target)):
        raise ApiError(404, f"not a directory: {target}")
    try:
        children = checkout_io(
            lambda: sorted(target.iterdir(),
                           key=lambda p: (not p.is_dir(),
                                          p.name.lower())),
            path=str(target))
    except PermissionError as exc:
        raise ApiError(403, f"permission denied: {target}") from exc
    entries = []
    truncated = False
    for child in children:
        name = child.name
        if name.startswith(".") and not show_hidden:
            continue
        try:
            # G9: per-entry stats also run in the pool (bounded).
            is_dir = checkout_io(child.is_dir, path=str(child))
        except OSError:
            continue
        entry = {"name": name, "path": str(child), "is_dir": is_dir,
                 "hidden": name.startswith(".")}
        if is_dir:
            try:
                entry["is_git"] = checkout_io(
                    (child / ".git").exists, path=str(child))
            except OSError:
                entry["is_git"] = False
        entries.append(entry)
        if len(entries) >= 500:
            truncated = True
            break
    try:
        is_git = checkout_io((target / ".git").exists, path=str(target))
    except OSError:
        is_git = False
    return {"path": str(target), "parent": str(target.parent) if target.parent != target else None,
            "home": str(Path.home()), "is_git": is_git,
            "entries": entries, "truncated": truncated}


def _issue_detail(decision: str, card: dict | None) -> tuple:
    """(one-line result/bottleneck, question or '') for a diagnosed issue."""
    if card is None:
        return "", ""
    if decision == "skip-done":
        kinds = [d["kind"] for d in card.get("drafts") or []]
        total = (card.get("files_total") or "").split(",")[0]
        head = "PR 초안" if "PR 본문" in kinds else "완료"
        return (f"{head} · {total}" if total else head), ""
    if decision == "skip-draft":
        return "답글 초안", ""
    if decision == "skip-waiting":
        return (card.get("question") or "")[:140], (card.get("question") or "")[:140]
    if decision == "skip-active":
        return "진행 중", ""
    return "", ""


def _diagnose(repo: str) -> dict:
    """Drift + issue scan. No quota: registry/gh reads only."""
    profile = _load_profile(repo)
    checkout = _resolve_checkout(profile)
    # G9: pin reads open checkout files and can block on a wedged
    # volume. The registry leg needs longer than the 5 s display
    # budget, so this call gets 30 s — still bounded and fail-closed,
    # never a parked handler.
    rows = checkout_io(deps.check, profile, checkout,
                       path=str(checkout), timeout=30)
    issues = []
    try:
        diag_issues = cli._open_issues(profile["github"])
    except Exception:  # noqa: BLE001 - gh may be absent/offline; drift still shows
        diag_issues = []
    # M3.5-2b: issues an external PR closes (and OpenMainty never
    # ran) read as "외부 PR 진행 중" instead of "start" — the scan
    # never queued them, and the board must not offer a duplicate
    # run. Deterministic, from the scan's stored last_prs.
    from openmainty import prs as _prs
    try:
        import openmainty.sched as _sched
        _closers = _prs.closing_map(
            (_sched.load(repo).get("last_prs") or []))
    except (Exception, SystemExit):  # noqa: BLE001 - no map, no rule
        _closers = {}
    for item in diag_issues:
        number = str(item["number"])
        decision = cli.decide_issue(repo, number)
        closer = _closers.get(int(number)) if number.isdigit() else None
        decision = _prs.board_decision(
            decision, number, _closers,
            store.latest_run(repo, number) is not None)
        entry = {"number": number, "title": item.get("title", ""),
                 "decision": decision, "session_id": None,
                 "detail": "", "question": ""}
        if decision == "skip-external-pr" and closer is not None:
            entry["detail"] = (f"외부 PR #{closer.get('number')} "
                               f"({closer.get('author') or '?'}) 진행 중")
        prev = store.latest_run(repo, number)
        sess = chat.session_for_run(prev["id"]) if prev else None
        if sess is None:
            sess = chat.reply_session_for(repo, number)
        if sess is not None:
            entry["session_id"] = sess["id"]
            entry["detail"], entry["question"] = _issue_detail(
                decision, chat.session_card(sess))
        issues.append(entry)
    summary = deps.summarize(rows)
    return {"profile": profile,
            "drift": [r for r in rows if r["drift"]],
            "errors": [r for r in rows if r["error"]],
            "checked": summary["checked"],
            "warnings": summary["warnings"],
            "warning_notes": summary["warning_notes"],
            "registry_errors": summary["errors"],
            "unsupported": summary["unsupported"],
            "issues": issues}


def _checkout_unlinked(repo: str) -> bool:
    """True when the repo never linked a checkout (a normal state).

    Unknown repos answer False so _diagnose below raises the honest
    404; linked-but-missing checkouts are not unlinked, so they keep
    their 400. Only the never-linked case is a typed non-error."""
    from . import pcconfig
    try:
        profile = _load_profile(repo)
    except ApiError:
        return False
    co = (pcconfig.load().get("checkouts", {}) or {}).get(profile["name"])
    return not co


def api_watch(payload: dict) -> dict:
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    if _checkout_unlinked(repo):
        return {"no_checkout": True, "drift": [], "errors": [],
                "checked": 0, "warnings": 0, "warning_notes": [],
                "registry_errors": [], "unsupported": False, "issues": []}
    d = _diagnose(repo)
    return {"drift": d["drift"], "errors": d["errors"],
            "checked": d["checked"], "warnings": d["warnings"],
            "warning_notes": d["warning_notes"],
            "registry_errors": d["registry_errors"],
            "unsupported": d["unsupported"], "issues": d["issues"]}


def api_sched_status(payload: dict) -> dict:
    repo = (payload.get("repo") or "").strip()
    if repo:
        return sched.load(repo)
    return {name: sched.load(name) for name in repos.list_all()}


def api_m2_policy(payload: dict) -> dict:
    """M2 autonomy policy: GET {repo} reads; POST {repo, stage, on}
    flips one toggle (the button is the confirmation, as sched_config).
    POST takes an optional `by` ("owner"|"root"; "worker" is
    refused) attributing the flip on the timeline — the UI button
    sends nothing and stays "owner".
    Always returns the policy, its glance line and the promoted guard."""
    from . import m2 as _m2
    repo = str((payload or {}).get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    try:
        if "stage" in (payload or {}):
            stage = str(payload.get("stage") or "")
            if stage not in _m2.STAGES:
                raise ApiError(400, f"unknown M2 stage '{stage}'")
            policy = _m2.set_policy(repo, stage,
                                    bool(payload.get("on", False)),
                                    by=str(payload.get("by", "owner")
                                           or "owner"))
            # The glance line is baked at refresh: queue one in the
            # background so the flip returns at once (S14 — the
            # synchronous refresh took 10-26 s on the real home).
            # The glance poll picks the new line up.
            refresh = refresh_summary_soon("m2-policy")
        else:
            policy = _m2.get_policy(repo)
            refresh = None
    except SystemExit as exc:
        raise ApiError(404, str(exc)) from None
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    out = {"repo": repo, "policy": policy,
           "line": _m2.policy_line(policy),
           "guard": _m2.promoted_guard()}
    if refresh is not None:
        out["refresh"] = refresh
    return out


def api_sched_config(payload: dict) -> dict:
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    interval = payload.get("interval_s", payload.get("interval", 1800))
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        raise ApiError(400, "interval must be seconds") from None
    execute = payload.get("execute_enabled", payload.get("execute", None))
    return sched.configure(repo, enabled=bool(payload.get("enabled", True)),
                           interval_s=interval,
                           execute_enabled=None if execute is None else bool(execute))


def api_sched_scan(payload: dict) -> dict:
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    _load_profile(repo)
    return sched.scan(repo)


def api_sched_cancel(payload: dict) -> dict:
    repo = (payload.get("repo") or "").strip()
    key = (payload.get("key") or "").strip()
    if not repo or not key:
        raise ApiError(400, "repo and key are required")
    return sched.cancel(repo, key)


def api_sched_run_due(payload: dict) -> dict:
    """Autonomous execution control: run the next due queue item now,
    or exactly the proposal named by optional `key`.

    Explicit operator action (button/CLI). Real `muse` executor spends
    subscription quota and is subscriber-gated; `fake` only proves flow.
    With `key`, sched validates it against the actual queue and refuses
    unknown/stale/non-queued/backoff-held keys with a reason — the
    lane/branch/target always come from the stored item, so any
    target/branch fields in the payload are ignored, never trusted.
    """
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    _load_profile(repo)
    key = (payload.get("key") or "").strip() or None
    use_fake = bool(payload.get("fake"))
    if use_fake:
        ex = runner.FakeExecutor()
    else:
        _require_gate()
        ex = runner.Executor(model=_settings_model())
    return sched.run_due(repo, ex, model=_settings_model(), key=key)


def api_sched_daemon(payload: dict) -> dict:
    """One maintenance-service step now: scan due repos, execute where
    autonomous mode is ON. Same entry the launchd service runs."""
    use_fake = bool(payload.get("fake"))
    if use_fake:
        ex = runner.FakeExecutor()
    else:
        _require_gate()
        ex = runner.Executor(model=_settings_model())
    return sched.daemon_tick(ex, model=_settings_model())


def api_watch_inputs(payload: dict) -> dict:
    """Edit an active external-watch registry baseline through the app.

    Baseline configuration only, never a watch lift or an upgrade:
    the queue key and run identity resolve server-side, every refusal
    (unknown/mismatched/cancelled/inactive/stale/duplicate/unsupported/
    lookup-failed/busy) happens before any write, and fresh latest pins
    for newly added peers are observed server-side — client-injected
    versions are never trusted. The caller echoes the fingerprint and
    settled_at it read with the watch report; a concurrent policy
    change refuses instead of overwriting.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "JSON object is required")
    repo = (payload.get("repo") or "").strip() if isinstance(
        payload.get("repo"), str) else ""
    key = (payload.get("key") or "").strip() if isinstance(
        payload.get("key"), str) else ""
    if not repo or not key:
        raise ApiError(400, "repo and key are required")
    add = payload.get("add", [])
    remove = payload.get("remove", [])
    for name, val in (("add", add), ("remove", remove)):
        if not isinstance(val, (list, tuple)) or any(
                not isinstance(k, str) for k in val):
            raise ApiError(400, f"{name} must be a list of watched keys")
    expected_fp = payload.get("expected_fp")
    if not isinstance(expected_fp, str) or not expected_fp:
        raise ApiError(400, "expected_fp from the watch report is required")
    expected_settled = payload.get("expected_settled_at")
    if not isinstance(expected_settled, str) or not expected_settled:
        raise ApiError(400, "expected_settled_at from the watch report is required")
    _load_profile(repo)
    res = sched.configure_watch_inputs(
        repo, key, add=list(add), remove=list(remove),
        expected_fp=expected_fp, expected_settled_at=expected_settled,
        actor="dashboard")
    if res.get("ok"):
        return res
    status = res.get("status")
    if status == "unknown":
        raise ApiError(404, res.get("reason") or "unknown watch")
    if status == "invalid":
        raise ApiError(400, res.get("reason") or "invalid watch edit")
    raise ApiError(409, res.get("reason") or "watch edit refused")


def _maintenance_service() -> dict:
    """Actual separate maintenance launchd service state (not claimed)."""
    import subprocess as _sp
    label = "dev.openmainty.maintenance"
    try:
        proc = _sp.run(["launchctl", "list", label], capture_output=True,
                       text=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return {"installed": None, "note": f"launchctl unavailable: {exc}"[:160]}
    if proc.returncode != 0:
        return {"installed": False, "running": False,
                "note": "maintenance service not installed"}
    pid = None
    for line in (proc.stdout or "").splitlines():
        if '"PID"' in line:
            digits = "".join(c for c in line if c.isdigit())
            pid = int(digits) if digits else None
    log_tail = ""
    try:
        log_tail = (paths.home() / "maintenance.log").read_text(
            encoding="utf-8")[-1500:]
    except OSError:
        pass
    return {"installed": True, "running": pid is not None, "pid": pid,
            "log_tail": log_tail}


def api_graph(payload: dict) -> dict:
    return graph.build((payload.get("repo") or "").strip())


def api_events(payload: dict) -> dict:
    repo = (payload.get("repo") or "").strip()
    try:
        limit = int(payload.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    return {"events": events.list_events(repo, limit=min(limit, 500))}


def api_deps_discover(payload: dict) -> dict:
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    profile = _load_profile(repo)
    checkout = _resolve_checkout(profile)
    # G9: manifest reads can block on a wedged volume — bounded and
    # fail-closed (502), never a parked handler.
    return checkout_io(manifests.discover, str(checkout),
                       path=str(checkout))


def _parse_diffstat_text(text: str) -> tuple:
    """([{path, changes, additions, deletions}], total) from a recorded
    diffstat. Parsed from the saved artifact only — never invented."""
    import re as _re
    files = []
    for line in (text or "").splitlines():
        if "|" not in line:
            continue
        left, right = line.split("|", 1)
        name = left.strip()
        if not name:
            continue
        m = _re.search(r"(\d+)", right)
        files.append({"path": name,
                      "changes": int(m.group(1)) if m else 0,
                      "additions": right.count("+"),
                      "deletions": right.count("-")})
    total = next((line.strip() for line in (text or "").splitlines()
                  if _re.search(r"\d+ files? changed", line)), "")
    return files, total


def _parse_numstat_text(text: str) -> list:
    """([{path, changes, additions, deletions}]) from `git diff --numstat`.

    Full relative paths (never the abbreviated `...`-truncated display
    paths of `git diff --stat`) and real added/removed line counts —
    the stat bar-character counting was wrong for large changes.
    Rename entries (`old => new`, incl. `{a => b}c` braces) resolve to
    the new side. Binary files (`- - path`) carry 0/0 counts with the
    path itself staying truthful. Tab-separated, so deep nested paths
    and paths with spaces survive intact.
    """
    files = []
    for line in (text or "").splitlines():
        if "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_s, del_s = parts[0].strip(), parts[1].strip()
        raw = "\t".join(parts[2:]).strip().strip('"')
        if not raw:
            continue
        if " => " in raw:
            new = raw.rsplit(" => ", 1)[1]
            if "{" in raw and "}" in raw:
                pre = raw.split("{", 1)[0]
                suf = raw.split("}", 1)[1]
                path = (pre + new.rstrip("}").strip() + suf).strip()
            else:
                path = new.strip()
            path = path.strip('"')
        else:
            path = raw
        if not path:
            continue
        try:
            additions = 0 if add_s == "-" else int(add_s)
            deletions = 0 if del_s == "-" else int(del_s)
        except ValueError:
            continue
        files.append({"path": path,
                      "changes": additions + deletions,
                      "additions": additions,
                      "deletions": deletions})
    return files


def _live_snapshot(state: dict):
    """One read-only live source snapshot for a read request, or None.

    A single `source_snapshot` covers proof validation, current-source
    display, and change lists — callers must reuse it, never hash the
    worktree twice for one request. None means the live source is
    missing/unreadable (callers report unknown, never stored-as-current).
    Never saves, never mutates anything (GET-safe).
    """
    from . import runner as _runner
    try:
        return _runner.fresh_snapshot(state)
    except Exception:  # noqa: BLE001 - unreadable means unverified
        return None


def _live_head_fp_kind(snap):
    """(live_head|None, live_fp|None, live_kind) from a live snapshot.

    The live HEAD is the readable git HEAD only — never the stored
    commit. Non-git snapshots carry no commit identity ("unknown" head),
    so their head reads as None (unknown), never as a commit match.
    """
    if snap is None:
        return None, None, "unknown"
    kind = snap.get("kind") or "unknown"
    head = snap.get("head")
    live_head = head if head not in (None, "", "unknown") else None
    return live_head, snap.get("fingerprint"), kind


def _changed_from_source_changes(sc: dict) -> dict:
    """Current file list mapped from an evaluated sourceChanges payload.

    Same files/total/provenance the inspector shows — one current truth,
    never a divergent second computation.
    """
    files = []
    for f in sc.get("fileDiffs") or []:
        files.append({"path": f["path"],
                      "changes": f.get("changes", 0),
                      "additions": f.get("additions", 0),
                      "deletions": f.get("deletions", 0)})
    return {"files": sorted(files, key=lambda f: f["path"]),
            "total": sc.get("filesTotal", ""),
            "source": sc.get("filesSource", "none")}


def _recorded_changed_files(state: dict) -> dict:
    """Historical recorded patch (review/fix diffstats), explicitly stale.

    Only for when the live worktree is unavailable: the recorded
    artifacts stay readable as historical evidence, never as current.
    """
    from . import store as _store
    rdir = _store.run_dir(state["id"])
    merged: dict = {}
    sources = []
    for step in ("review", "fix"):
        p = rdir / f"{step}.diffstat.txt"
        if not p.is_file():
            continue
        try:
            files, _ = _parse_diffstat_text(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        if files:
            sources.append(f"{step}.diffstat.txt")
        for f in files:
            merged.setdefault(f["path"], f)
    review_total = ""
    for step in ("review", "fix"):
        p = rdir / f"{step}.diffstat.txt"
        if p.is_file():
            try:
                _, total = _parse_diffstat_text(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            if total:
                review_total = total
                break
    if merged:
        files = sorted(merged.values(), key=lambda f: f["path"])
        return {"files": files, "total": review_total,
                "source": "+".join(sources)}
    return {"files": [], "total": "", "source": "none"}


def _changed_files(state: dict) -> dict:
    """Actual changed files: the live isolated worktree first.

    Current file lists use base..ACTUAL_HEAD plus staged/unstaged/
    untracked (via the live sourceChanges evaluation) — never
    base..stored-workCommit and never a stale recorded diffstat. A true
    live empty is authoritative and stays empty; recorded fix/review
    diffstats are historical evidence for when the live worktree is
    gone, preserved untouched (never overwritten/deleted here).
    """
    sc = _source_changes(state)
    if sc.get("availability") == "live":
        return _changed_from_source_changes(sc)
    return _recorded_changed_files(state)


def _evidence_files(state: dict) -> list:
    """Saved run-dir files with sizes (evidence links for the UI)."""
    from . import store as _store
    rdir = _store.run_dir(state["id"])
    try:
        names = sorted(p.name for p in rdir.iterdir() if p.is_file())
    except OSError:
        return []
    out = []
    for name in names[:200]:
        kind = ("live-log" if name.endswith(".live.log.txt")
                else "log" if name.endswith(".log.txt")
                else "diffstat" if name.endswith(".diffstat.txt")
                else "draft" if name.endswith(".md")
                else "summary" if name in ("summary.json", "outcome.json")
                else "file")
        try:
            size = (rdir / name).stat().st_size
        except OSError:
            size = -1
        out.append({"name": name, "bytes": size, "kind": kind})
    return out


def _step_kind(entry: dict) -> str:
    """model = Muse worker session(s); shell = actually-executed commands;
    local = on-machine op; none = never ran."""
    cmd = entry.get("command") or []
    if not cmd:
        return "none"
    if all(isinstance(c, str) for c in cmd):
        if cmd and cmd[0] == "muse":
            return "model"
        return "local"
    if any(isinstance(c, list) for c in cmd):
        return "shell"
    return "local"


def _verification_blocks(state: dict,
                         current_fingerprint=store.USE_STORED,
                         current_head=store.USE_STORED) -> list:
    """Actual shell verification commands/scopes vs model workflow steps.

    The e2e step is labelled by what its commands really covered
    (types/lint/unit/build/native-runtime), not by its name.
    `current_fingerprint`/`current_head` are validated at read time
    (None when the worktree is unreadable → unknown, never green; a
    moved HEAD is stale even when the bytes are identical).
    """
    proof = store.verification_status(
        state, current_fingerprint=current_fingerprint,
        current_head=current_head)
    blocks = []
    for name in ("reproduce", "review", "e2e"):
        entry = (state.get("steps") or {}).get(name, {})
        kind = _step_kind(entry)
        shell_cmds = [c for c in (entry.get("command") or [])
                      if isinstance(c, list)]
        blocks.append({
            "step": name,
            "kind": "shell" if shell_cmds else kind,
            "status": entry.get("status"),
            "exit_code": entry.get("exit_code"),
            "note": entry.get("note", ""),
            "evidence": entry.get("evidence", []),
            "shell_commands": [" ".join(map(str, c)) for c in shell_cmds],
            "scopes": sorted(branches.classify_commands(shell_cmds)),
            "proof": proof.get(name, "open"),
            "verified_commit": (state.get("verifiedCommits") or {}).get(name),
            "verified_fingerprint": (
                (state.get("verifiedFingerprints") or {}).get(name) or "")[:12],
        })
    return blocks


def _run_source(state: dict,
                current_fingerprint=store.USE_STORED,
                current_head=store.USE_STORED,
                live_kind=None,
                worktree_available=None) -> dict:
    """Current source as actually readable, plus explicitly recorded history.

    `head_commit`/`fingerprint`/`kind` are the LIVE worktree HEAD +
    content fingerprint + source kind — matching the proof comparison and
    the live diff. The last engine-recorded identifiers ride separately
    as `recorded_*` historical fields so both stay distinguishable. A
    missing/unreadable live source reads "unknown"/"" — stored values
    are never silently passed off as current.

    `current_head`/`current_fingerprint` are validated at read time: bytes
    that changed after the last resume report stale even if nothing
    re-ran yet, a moved HEAD is stale even with identical bytes, and an
    unreadable worktree reports unknown — never a fabricated green. The
    stored-cache defaults preserve the execute-path contract; read paths
    must pass freshly read values (or None when unreadable).
    """
    proof = store.verification_status(
        state, current_fingerprint=current_fingerprint,
        current_head=current_head)
    # Not-applicable skips (dep-maintenance reproduce with no upstream
    # bug) are honest non-tests: they neither prove nor block. The
    # aggregate reflects only required proofs (review/e2e plus any real
    # reproduce run), so fresh review+e2e reads current even when the
    # skipped reproduce carries no content proof. Legacy/unknown/stale
    # on a REQUIRED step still blocks green.
    required = {k: v for k, v in proof.items() if v != "skipped"}
    states = set(required.values())
    overall = ("stale" if "stale" in states
               else "unknown" if "unknown" in states
               else "legacy" if "legacy" in states
               else "current" if "fresh" in states else "none")
    stored_head = state.get("workCommit")
    stored_fp = state.get("workTreeFingerprint") or ""
    stored_kind = state.get("sourceKind") or "unknown"
    live_mode = not (current_fingerprint is store.USE_STORED
                     and current_head is store.USE_STORED)
    if live_mode:
        live_head = (current_head
                     if current_head not in (None, "", "unknown") else None)
        head_disp = live_head or "unknown"
        fp_disp = current_fingerprint or ""
        kind_disp = live_kind or "unknown"
        if worktree_available is None:
            worktree_available = bool(live_head or current_fingerprint)
        head_match = ((live_head == stored_head)
                      if (live_head and stored_head not in (None, "", "unknown"))
                      else None)
    else:
        head_disp, fp_disp, kind_disp = stored_head, stored_fp, stored_kind
        head_match = None
    return {"base_commit": state.get("baseCommit"),
            "head_commit": head_disp,
            "fingerprint": fp_disp,
            "fingerprint_short": fp_disp[:12] if fp_disp else "",
            "kind": kind_disp,
            "proof": overall,
            "per_step": proof,
            "recorded_head_commit": stored_head,
            "recorded_fingerprint": stored_fp,
            "recorded_fingerprint_short": stored_fp[:12] if stored_fp else "",
            "recorded_kind": stored_kind,
            "head_match": head_match,
            "worktree_available": worktree_available}


def _legacy_triage_verdict(text: str):
    """Strict legacy verdict from old three-section prose (no DISPOSITION line).

    Section headers (`##DUPLICATE` etc.) are NOT verdicts — only one
    unambiguous positive body counts. Returns (kind | None, reason):
    exactly one positive section yields its kind, otherwise None
    (zero or several positives, or no sections at all) so the caller
    reports unknown instead of guessing from a header word. Never
    mutates anything; pure read of the given text.
    """
    import re as _re
    sections: dict = {}
    current = None
    buf: list = []

    def _flush():
        if current is not None:
            sections.setdefault(current, [])
            sections[current].extend(buf)

    for line in (text or "").splitlines():
        m = _re.match(
            r"\s*#{0,6}\s*(DUPLICATE|ACTIONABLE|INFO-NEEDED|INFO_NEEDED)\s*:?\s*$",
            line, _re.IGNORECASE)
        if m:
            _flush()
            current = m.group(1).upper().replace("_", "-")
            buf = []
            continue
        buf.append(line)
    _flush()

    def _negative(body: str) -> bool:
        first = (body.strip().splitlines() or [""])[0].strip().lower()
        if not first:
            return True
        return bool(_re.match(
            r"(none|n/?a\b|no\b|not\b|nothing|unknown|never|nobody|nil\b"
            r"|tbd\b|tbc\b|—|-|~)\b",
            first))

    if not sections:
        # No section headers: a bare `KIND: detail` verdict line still
        # counts when its detail is affirmative — it is a verdict with
        # content, not a header word. A bare kind alone stays unknown.
        m = _re.search(
            r"(?im)^\s*(DUPLICATE|ACTIONABLE|INFO-NEEDED|INFO_NEEDED)"
            r"\s*:\s*(.+?)\s*$", text or "")
        if not m:
            return None, "no triage sections"
        kind = m.group(1).upper().replace("_", "-")
        detail = m.group(2).strip()
        if _negative(detail):
            return None, "bare verdict detail is negative"
        if kind == "DUPLICATE" and not _re.search(r"#\d+", detail):
            # A terminal duplicate verdict must name its target issue.
            return None, "bare duplicate verdict names no issue"
        return kind, "bare positive verdict line"

    def _body(kind: str) -> str:
        return "\n".join(sections.get(kind, [])).strip()

    def _positive(kind: str, body: str) -> bool:
        if not body.strip() or _negative(body):
            return False
        if kind == "DUPLICATE":
            # A confirmed duplicate names the target issue.
            return bool(_re.search(
                r"(?i)\bduplicate\s+of\s+#\d+|\bdupl\w*\s+#\d+|#\d+\s*\(.*\bdupl",
                body))
        if kind == "INFO-NEEDED":
            # A real open question names missing facts.
            return bool("?" in body or _re.search(
                r"(?i)\b(need|needs|needed|missing|unclear|provide|required|"
                r"please\s+(clarify|confirm|provide|specify))", body))
        # ACTIONABLE: an affirmative verdict plus an actual plan.
        return bool(_re.match(r"(?i)\s*yes\b", body)
                    or _re.search(
                        r"(?i)\b(implement|fix|upgrade|change|add|bump|migrate|"
                        r"proceed|actionable)\b", body))

    positives = [k for k in ("DUPLICATE", "INFO-NEEDED", "ACTIONABLE")
                 if _positive(k, _body(k))]
    if len(positives) == 1:
        return positives[0], "single positive legacy section"
    if not positives:
        return None, "no positive legacy section"
    return None, f"ambiguous legacy positives: {'+'.join(positives)}"


def _watch_history(state: dict) -> dict:
    """Whitelisted watch-baseline audit for the UI.

    Projects only `watch-inputs-configured` interventions to the small
    structured fields the editor may show (when/actor/identity/added/
    removed/old-new pins/source pins). Raw whole-history JSON never
    reaches the rendered surface. Bounded: newest 5 entries + total.
    Baseline configuration only — never a verification or an upgrade.
    """
    items = [iv for iv in (state.get("interventions") or [])
             if isinstance(iv, dict) and iv.get("kind") == "watch-inputs-configured"]
    proj = []
    for iv in items:
        old_inputs = iv.get("old_inputs")
        new_inputs = iv.get("new_inputs")
        old_reg = (old_inputs.get("registry")
                   if isinstance(old_inputs, dict) else None) or {}
        new_reg = (new_inputs.get("registry")
                   if isinstance(new_inputs, dict) else None) or {}
        if not isinstance(old_reg, dict):
            old_reg = {}
        if not isinstance(new_reg, dict):
            new_reg = {}
        observed = iv.get("observed")
        source = iv.get("source")
        if not isinstance(source, dict):
            source = {}
        proj.append({
            "at": str(iv.get("at") or ""),
            "actor": str(iv.get("actor") or ""),
            "repo": str(iv.get("repo") or state.get("repo") or ""),
            "key": str(iv.get("key") or state.get("queue_key") or ""),
            "run_id": str(iv.get("run_id") or state.get("id") or ""),
            "added": sorted(set(new_reg) - set(old_reg)),
            "removed": sorted(set(old_reg) - set(new_reg)),
            "old": {str(k): str(v) for k, v in old_reg.items()},
            "new": {str(k): str(v) for k, v in new_reg.items()},
            "observed": ({str(k): str(v) for k, v in observed.items()}
                         if isinstance(observed, dict) else {}),
            "source": {
                "workCommit": str(source.get("workCommit") or ""),
                "workTreeFingerprint": str(source.get("workTreeFingerprint") or ""),
            },
        })
    return {"entries": proj[-5:], "total": len(proj)}


def _run_outcome(state: dict,
                 current_fingerprint=store.USE_STORED,
                 current_head=store.USE_STORED) -> dict:
    """Concise decision/outcome grounded in persisted worker artifacts.

    Triage authority order (read-only, never mutates the run): the durable
    `state.disposition` first; then an explicit `DISPOSITION:` verdict
    line in the saved triage log; then a strict unambiguous positive
    legacy section — otherwise unknown (None), never a guess from a
    bare header word. `triageSource` names which provenance applied:
    disposition | disposition-line | legacy-positive | unknown | none
    (no triage log at all). The optional summary is passed through ONLY
    from an explicit structured artifact contract (summary.json/
    outcome.json) — never model-rendered here, never invented for
    legacy runs. Promotion is validated against the read-time
    fingerprint, so a post-verification edit blocks promotion at read.
    """
    import re as _re
    from . import planner as _planner
    rdir = store.run_dir(state["id"])
    triage = None
    triage_source = "none"
    disp = state.get("disposition")
    if isinstance(disp, dict) and disp.get("kind"):
        triage = disp.get("kind")
        triage_source = "disposition"
    else:
        try:
            text = runner.human_text((rdir / "triage.log.txt").read_text(
                encoding="utf-8"))
        except OSError:
            text = None
        if text is None:
            triage, triage_source = None, "none"
        elif _re.search(r"(?im)^\s*DISPOSITION\s*:", text):
            kind, _detail = _planner.parse_triage_disposition(text)
            if kind != "UNKNOWN":
                triage, triage_source = kind, "disposition-line"
            else:
                # A DISPOSITION line exists but its verdict is malformed:
                # execution blocks as UNKNOWN too, so the API stays
                # unknown rather than guessing from legacy sections.
                triage, triage_source = None, "unknown"
        else:
            legacy, _reason = _legacy_triage_verdict(text)
            if legacy is not None:
                triage, triage_source = legacy, "legacy-positive"
            else:
                triage, triage_source = None, "unknown"
    steps = state.get("steps") or {}
    failed = sorted(s for s in store.STEP_ORDER
                    if (steps.get(s) or {}).get("status") == "failed")
    blocked = sorted(s for s in store.STEP_ORDER
                     if (steps.get(s) or {}).get("status") == "blocked")
    summary = None
    for fname in ("summary.json", "outcome.json"):
        p = rdir / fname
        if not p.is_file():
            continue
        try:
            import json as _json
            parsed = _json.loads(p.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                summary = parsed
                break
        except (OSError, ValueError):
            continue
    try:
        promo = branches.promotion_readiness(
            state, current_fingerprint=current_fingerprint,
            current_head=current_head)
    except Exception:  # noqa: BLE001
        promo = {"ready": False, "criteria": []}
    blockers = [c.get("name", "") for c in promo.get("criteria", [])
                if not c.get("ok")]
    watch = state.get("watch") if isinstance(state.get("watch"), dict) else None
    try:
        watch_fp = sched.watch_inputs_fingerprint(
            (watch or {}).get("inputs") or {}) if watch else None
    except Exception:  # noqa: BLE001 - fingerprint never breaks the report
        watch_fp = None
    return {"triage": triage,
            "triageSource": triage_source,
            "disposition": state.get("disposition"),
            "watch": watch,
            "watch_inputs_fp": watch_fp,
            "watch_history": _watch_history(state),
            "lane": state.get("lane"),
            "branch": state.get("branch"),
            "failed_steps": failed,
            "blocked_steps": blocked,
            "wait": state.get("waitReason"),
            "promotion_ready": promo.get("ready", False),
            "promotion_blockers": blockers,
            "summary": summary}


def api_work(payload: dict) -> dict:
    """One work item: trigger/why/evidence/files/branch/tests/links/next."""
    run_id = (payload.get("run") or payload.get("id") or "").strip()
    if not run_id:
        raise ApiError(400, "run is required")
    try:
        state = store.load(run_id)
    except SystemExit as exc:
        raise ApiError(404, str(exc)) from exc
    cmds, tests = [], []
    for name in store.STEP_ORDER:
        entry = state.get("steps", {}).get(name, {})
        if entry.get("command"):
            cmds.append({"step": name, "command": entry["command"],
                         "exit_code": entry.get("exit_code"),
                         "status": entry.get("status"), "note": entry.get("note", "")})
        if name in ("e2e", "reproduce") or entry.get("evidence"):
            tests.append({"step": name, "status": entry.get("status"),
                          "evidence": entry.get("evidence", []),
                          "exit_code": entry.get("exit_code"),
                          "note": entry.get("note", "")})
    artifacts = state.get("artifacts", {})
    # Read-time source: ONE live snapshot covers proof validation,
    # current-source display, and change lists — never hashed twice and
    # never mutated (GET-safe). An external edit or a moved HEAD after
    # the last resume reports stale/unknown here instead of a fabricated
    # CURRENT; an unreadable worktree reports unknown, never stored
    # values passed off as current.
    snap = _live_snapshot(state)
    live_head, live_fp, live_kind = _live_head_fp_kind(snap)
    worktree_available = snap is not None
    if snap is not None and (snap.get("kind") or "unknown") != "git":
        # Non-git snapshots carry no commit identity: validate proof
        # against the recorded commit (as before) but still display the
        # live bytes/kind that were actually read.
        proof_head = store.USE_STORED
    else:
        proof_head = live_head
    try:
        promo = branches.promotion_readiness(
            state, current_fingerprint=live_fp, current_head=proof_head)
    except Exception:  # noqa: BLE001
        promo = {"ready": False, "criteria": []}
    related = events.list_events(state.get("repo", ""), limit=20)
    links = []
    profile = None
    try:
        profile = repos.load(state.get("repo", ""))
    except SystemExit:
        pass
    if profile and profile.get("github") and state.get("issue", "").isdigit():
        links.append(f"https://github.com/{profile['github']}/issues/{state['issue']}")
    sc = _source_changes(state)
    if sc.get("availability") == "live":
        changed = _changed_from_source_changes(sc)
    else:
        changed = _recorded_changed_files(state)
    details = {name: {"status": (state.get("steps", {}).get(name) or {}).get("status"),
                      "exit_code": (state.get("steps", {}).get(name) or {}).get("exit_code"),
                      "note": (state.get("steps", {}).get(name) or {}).get("note", ""),
                      "evidence": (state.get("steps", {}).get(name) or {}).get("evidence", [])}
               for name in store.STEP_ORDER}
    try:
        integration_prep = batches.latest_integration_op(run_id)
    except Exception:  # noqa: BLE001 - prep state must never break work
        integration_prep = None
    try:
        # M2.6b: release-train siblings for the family-retry button
        # (read-only; a missing train is simply no button).
        train = batches.family_train(state.get("repo", ""), run_id)
        family_siblings = [m for m in train
                           if m.get("run_id") != run_id]
    except Exception:  # noqa: BLE001 - siblings never break work
        family_siblings = []
    try:
        report = _work_report(state, run_id)
    except Exception:  # noqa: BLE001 - report must never break work
        report = {"fix": "", "fix_missing": True,
                  "analysis": "", "analysis_missing": True,
                  "review": "", "review_missing": True,
                  "review_scope": {"status": "unknown",
                                   "error": "확인 실패"},
                  "review_scope_files": [],
                  "update_kind": "",
                  "fix_files": 0, "scope_note": ""}
    try:
        current_qa = _current_qa_for_run(state, run_id)
    except Exception:  # noqa: BLE001 - QA must never break work
        current_qa = {"state": "unknown", "reason": "확인 실패"}
    try:
        fastcheck_report = fastcheck_report_for_run(
            state, run_id, prep=integration_prep, current_qa=current_qa)
    except Exception:  # noqa: BLE001 - report never breaks work
        fastcheck_report = {"state": "unknown", "reason": "확인 실패"}
    return {"state": state, "commands": cmds, "tests": tests,
            "artifacts": artifacts, "promotion": promo,
            "related_events": related[:10], "links": links,
            "integration_prep": integration_prep,
            "fastcheck_report": fastcheck_report,
            "family_siblings": family_siblings,
            "report": report,
            "current_qa": current_qa,
            "changed_files": changed["files"],
            "changed_files_total": changed["total"],
            "changed_files_source": changed["source"],
            "sourceChanges": sc,
            "evidence_files": _evidence_files(state),
            "verification": _verification_blocks(
                state, current_fingerprint=live_fp,
                current_head=proof_head),
            "source": _run_source(state, current_fingerprint=live_fp,
                                  current_head=proof_head,
                                  live_kind=live_kind,
                                  worktree_available=worktree_available),
            "outcome": _run_outcome(state, current_fingerprint=live_fp,
                                    current_head=proof_head),
            "details": details}


_SOURCE_DIFF_TOTAL_CAP = 60000
_UNTRACKED_LIST_CAP = 200
_UNTRACKED_CONTENT_FILES = 20
_UNTRACKED_PER_FILE_CHARS = 3000
_UNTRACKED_TOTAL_CHARS = 30000
_UNTRACKED_READ_LIMIT = 100 * 1024


# Bound for read-only git probes on request (GET) paths: a wedged worktree
# must fail fast with code 124, never hang the response for the 600 s
# run_cmd default. Write paths, scheduler/runner job paths and POST paths
# never use this helper.
_REQUEST_GIT_TIMEOUT_S = 8


def _serve_git_bound(state: dict):
    """(bound_or_None, refused) for this run's live worktree.

    Bound is the prep record from the run state (recorded when the
    app created the tree, before any model step). refused is True
    when the tree is git but the record is missing or unprovable:
    live views then fail closed (stored/historical payload, never
    live bytes from a model-touched tree).
    """
    from . import runner as _runner
    try:
        work = store.run_dir(state.get("id")) / "work"
    except Exception:  # noqa: BLE001 - no provable tree, no refusal
        return None, False
    try:
        if not _runner._git_present(work):
            return None, False
    except Exception:  # noqa: BLE001 - unprovable presence refuses
        return None, True
    rec = state.get("workGit")
    if not isinstance(rec, dict) or not rec:
        return None, True
    return rec, False


def _git_text(work, args: list, *, git_bound=None):
    """(code, stdout, stderr) for a read-only git command; never raises.

    Code 124 means the probe timed out: the live state is UNKNOWN, never
    clean and never "no changes". Callers must not present a timeout as a
    verified result. 301: bound to the prep record on model-written
    trees (pinned gitdir, scrubbed env, nested .git refused);
    unbound reads use the same hardened flags + scrubbed env.
    A refused pointer reads as code 128 with no output.
    301b: index-refreshing verbs (diff, status) run against a
    private per-call index copy (M2.21b), bound and unbound alike,
    so a dashboard GET never writes the worktree's index while a
    worker may hold it. When the copy cannot be made the read
    keeps the env rule only — never a refusal for that.
    302: reads ignore in-tree .gitattributes (--attr-source), so
    no planted filter/diff driver runs. A git too old for the
    flag fails closed through the 128 path below, bound and
    unbound alike (this probe never raises by contract).
    302c: a hung probe surfaces as this call's own timeout
    (124), never as "unsupported": the probe raises
    TimeoutExpired uncached and the outer handler below maps it
    to 124, so one transient stall cannot brick later reads."""
    from . import runner as _runner
    from .util import git_readonly_child_env as _ro_env
    try:
        if git_bound is not None:
            try:
                paths = _runner.reprove_git_pointer(work, git_bound)
            except Exception as exc:  # noqa: BLE001 - fail closed
                return 128, "", str(exc)[:200]
            argv = _runner.hardened_git_argv(
                list(args), gitdir=paths["gitdir"],
                worktree=paths["worktree"])
        else:
            argv = _runner.hardened_git_argv(
                ["-C", str(work)] + list(args))
        try:
            with _ro_env(argv, _runner.hardened_git_env(),
                         cwd=None) as env:
                proc = subprocess.run(
                    argv, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=_REQUEST_GIT_TIMEOUT_S, text=True,
                    env=env)
        except subprocess.TimeoutExpired:
            return 124, "", "timed out"
    except subprocess.TimeoutExpired:
        # 302c: hung attr-source probe — this call's own timeout,
        # never a refusal.
        return 124, "", "timed out"
    except Exception as exc:  # noqa: BLE001
        return 128, "", str(exc)[:200]
    return (proc.returncode, proc.stdout or "",
            (proc.stderr or "")[:200])


def _relevant_untracked(work, rels: list, *, git_bound=None) -> tuple:
    """Split `??` relpaths into relevant source vs skipped noise/ignored.

    Generated, cache, and git-ignored paths never count as source
    changes; only real source files get content attached.
    301: the ignore probe runs bound to the prep record.
    """
    from . import runner as _runner
    relevant, skipped = [], 0
    for rel in rels:
        rel = (rel or "").strip()
        if not rel:
            continue
        # Collapsed dir entries (`?? node_modules/`) must not escape the
        # noise filter: probe them as inside-paths so the directory name
        # itself is screened like any other generated/cache dir.
        probe = rel + "/x" if rel.endswith("/") else rel
        try:
            if _runner._noise_rel(probe) or _runner._is_ignored(
                    work, rel, git_bound=git_bound):
                skipped += 1
                continue
        except Exception:  # noqa: BLE001 - unreadable tree: keep it visible
            pass
        relevant.append(rel)
    return relevant, skipped


def _untracked_contents(work, rels: list) -> tuple:
    """Bounded contents for relevant untracked source files.

    Returns (contents, capped): contents is [{path, bytes, truncated,
    binary, content|null}]; binary/unreadable/oversize files carry
    provenance without content. At most 20 files, 3000 chars each,
    30000 chars total — never unbounded.
    """
    from pathlib import Path as _Path
    work = _Path(work)
    contents, used, capped = [], 0, False
    for rel in rels[:_UNTRACKED_CONTENT_FILES]:
        if used >= _UNTRACKED_TOTAL_CHARS:
            capped = True
            break
        p = work / rel
        entry = {"path": rel, "bytes": -1, "truncated": False,
                 "binary": False, "content": None}
        try:
            if p.is_symlink():
                # Never read through a symlink: provenance only, no
                # content — the target may leave the worktree.
                contents.append(entry)
                continue
            if not p.is_file():
                entry["content"] = None
                contents.append(entry)
                continue
            size = p.stat().st_size
            entry["bytes"] = size
            raw = p.read_bytes()[:_UNTRACKED_READ_LIMIT + 1]
            if b"\x00" in raw[:8192]:
                entry["binary"] = True
                entry["truncated"] = size > _UNTRACKED_READ_LIMIT
                contents.append(entry)
                continue
            text = raw[:_UNTRACKED_READ_LIMIT].decode("utf-8", errors="replace")
            if len(raw) > _UNTRACKED_READ_LIMIT:
                entry["truncated"] = True
            budget = min(_UNTRACKED_PER_FILE_CHARS,
                         _UNTRACKED_TOTAL_CHARS - used)
            if len(text) > budget:
                text, entry["truncated"] = text[:budget], True
            entry["content"] = text
            used += len(text)
        except OSError:
            entry["content"] = None
        contents.append(entry)
    if len(rels) > _UNTRACKED_CONTENT_FILES:
        capped = True
    return contents, capped


def _merge_stat_files(sections: list) -> list:
    """Merge per-section diffstat parses into one file list.

    Each section is (files, section_name). Counts add across sections;
    entries keep `committed`/`dirty` provenance flags. Untracked files
    are appended by the caller with real line counts.
    """
    merged: dict = {}
    for files, section in sections:
        for f in files:
            cur = merged.setdefault(
                f["path"],
                {"path": f["path"], "changes": 0, "additions": 0,
                 "deletions": 0, "committed": False, "dirty": False})
            cur["changes"] += f.get("changes", 0)
            cur["additions"] += f.get("additions", 0)
            cur["deletions"] += f.get("deletions", 0)
            if section == "committed":
                cur["committed"] = True
            else:
                cur["dirty"] = True
    return sorted(merged.values(), key=lambda f: f["path"])


def _source_changes(state: dict) -> dict:
    """Inspectable local source diff for the UI (never a remote commit URL).

    Live worktree first: committed (`base..HEAD`) PLUS staged
    (`--cached`) PLUS unstaged PLUS bounded untracked source contents —
    so a dirty tree with `base == HEAD` still shows real patches.
    Unpublished local commits have no remote URL, so none is generated —
    the UI opens this payload, not github.com.

    Stable keys (never removed): baseRef/baseCommit/head/branch/lane/
    fileDiffs/filesTotal/filesSource/untracked/dirtyStatus/diff/
    diffCapped/hasChanges/planned/localOnly/note. Added keys document
    patch sections, provenance, and availability: liveHead/headMatch/
    diffCommitted/diffStaged/diffUnstaged/untrackedContents/
    untrackedCapped/untrackedSkipped/availability/historical/
    worktreeAvailable/patchSource/error.
    """
    rdir = store.run_dir(state["id"])
    work = rdir / "work"
    base = state.get("baseCommit")
    recorded = state.get("workCommit")
    git_ok = work.is_dir() and (work / ".git").exists()
    # 301: live reads need the prep record. A git tree with a
    # missing/unprovable record fails closed to the stored path
    # below (never live bytes from a model-touched tree).
    bound, refused = _serve_git_bound(state) if git_ok else (None, False)
    if refused:
        git_ok = False
    if git_ok:
        code, out, _ = _git_text(work, ["rev-parse", "HEAD"],
                                 git_bound=bound)
        # A timed-out probe (124) is not a missing worktree: the live state
        # is UNKNOWN, and the stored path below must say so.
        rev_timed_out = (code == 124)
        live_head = out.strip() if code == 0 and out.strip() else None
    else:
        rev_timed_out = False
        live_head = None
        bound = None
    if git_ok and live_head:
        return _live_source_changes(state, work, base, recorded, live_head,
                                    git_bound=bound)
    if _run_never_started(state) and not _has_stored_artifacts(rdir):
        return _planned_source_changes(state, base, recorded,
                                       work_available=work.is_dir())
    result = _stored_source_changes(state, rdir, base, recorded, live_head,
                                    work_available=work.is_dir())
    if rev_timed_out:
        # Fail closed: the work folder did not answer in time, so the live
        # state is UNKNOWN. The stored payload stands, but it must never
        # read as clean or as "no changes": hasChanges stays fail-closed
        # True, headMatch stays unknown (None), every stable key stays, and
        # the note says the folder did not answer. Verified input and lease
        # identity are untouched (no read here changes them).
        result["availability"] = "unknown"
        result["hasChanges"] = True
        if result.get("historical"):
            result["note"] = "작업 폴더가 제때 응답하지 않아 저장된 기록을 보여줍니다"
        else:
            result["note"] = ("작업 폴더가 제때 응답하지 않아 확인할 수 없습니다 "
                              "(변경 없음이 아닙니다)")
    return result


def _run_never_started(state: dict) -> bool:
    """True when no step ever ran: the planned state is genuine, not a
    missing-worktree cover story."""
    steps = state.get("steps") or {}
    return bool(steps) and all(
        (e or {}).get("status") == "pending" for e in steps.values())


def _has_stored_artifacts(rdir) -> bool:
    for name in ("review.diff.txt", "fix.diff.txt",
                 "review.diffstat.txt", "fix.diffstat.txt"):
        try:
            if (rdir / name).is_file():
                return True
        except OSError:
            continue
    return False


def _planned_source_changes(state: dict, base, recorded,
                            work_available: bool) -> dict:
    """sourceChanges for a run that never started: honestly planned."""
    return {"baseRef": state.get("base_branch"),
            "baseCommit": base,
            "head": recorded,
            "liveHead": None,
            "headMatch": None,
            "branch": state.get("branch"),
            "lane": state.get("lane"),
            "fileDiffs": [],
            "filesTotal": "",
            "filesSource": "none",
            "untracked": [],
            "untrackedContents": [],
            "untrackedCapped": False,
            "untrackedSkipped": 0,
            "dirtyStatus": "",
            "diff": "",
            "diffCommitted": "",
            "diffStaged": "",
            "diffUnstaged": "",
            "diffCapped": False,
            "hasChanges": False,
            "planned": True,
            "availability": "planned",
            "historical": False,
            "worktreeAvailable": work_available,
            "patchSource": "none",
            "error": "",
            "localOnly": True,
            "note": "no changes yet: planned state, no diff link"}


def _live_source_changes(state: dict, work, base, recorded, live_head,
                         *, git_bound=None) -> dict:
    """sourceChanges from the live isolated worktree (availability live).

    301: every git read below runs bound to the prep record."""
    sections = []
    provenance = []
    committed_stat_files: list = []
    if base not in (None, "", "unknown"):
        # numstat: full relative paths + real add/delete counts (the
        # abbreviated `--stat` display truncated deep paths with `...`
        # and its bar chars miscounted large changes).
        code, out, _ = _git_text(
            work, ["diff", "--numstat", f"{base}..HEAD", "--"],
            git_bound=git_bound)
        if code == 0 and out.strip():
            committed_stat_files = _parse_numstat_text(out)
            sections.append((committed_stat_files, "committed"))
            provenance.append("committed(base..HEAD)")
        code, committed_diff, _ = _git_text(
            work, ["diff", f"{base}..HEAD", "--"],
            git_bound=git_bound)
        committed_diff = committed_diff if code == 0 else ""
    else:
        committed_diff = ""
    code, out, _ = _git_text(work, ["diff", "--cached", "--numstat", "--"],
                             git_bound=git_bound)
    staged_stat_files: list = []
    if code == 0 and out.strip():
        staged_stat_files = _parse_numstat_text(out)
        sections.append((staged_stat_files, "staged"))
        provenance.append("staged")
    code, staged_diff, _ = _git_text(work, ["diff", "--cached", "--"],
                                    git_bound=git_bound)
    staged_diff = staged_diff if code == 0 else ""
    code, out, _ = _git_text(work, ["diff", "--numstat", "--"],
                             git_bound=git_bound)
    unstaged_stat_files: list = []
    if code == 0 and out.strip():
        unstaged_stat_files = _parse_numstat_text(out)
        sections.append((unstaged_stat_files, "unstaged"))
        provenance.append("unstaged")
    code, unstaged_diff, _ = _git_text(work, ["diff", "--"],
                                       git_bound=git_bound)
    unstaged_diff = unstaged_diff if code == 0 else ""
    code, status_out, status_err = _git_text(
        work, ["status", "--porcelain"], git_bound=git_bound)
    dirty_status = status_out[:6000] if code == 0 else ""
    raw_untracked = []
    if code == 0:
        for line in status_out.splitlines():
            if line.startswith("??"):
                raw_untracked.append(line[3:].strip().strip('"'))
    relevant, skipped = _relevant_untracked(work, raw_untracked,
                                            git_bound=git_bound)
    untracked = relevant[:_UNTRACKED_LIST_CAP]
    if len(relevant) > _UNTRACKED_LIST_CAP:
        provenance.append(f"untracked-list-capped({len(relevant)})")
    untracked_contents, untracked_capped = _untracked_contents(work, relevant)
    if relevant:
        provenance.append(
            f"untracked({len(relevant)} shown, {skipped} ignored/noise skipped)")
    elif skipped:
        provenance.append(f"untracked-none-relevant({skipped} ignored/noise skipped)")
    files = _merge_stat_files(sections)
    known = {f["path"] for f in files}
    for entry in untracked_contents:
        if entry["path"] in known or entry.get("binary"):
            continue
        if entry.get("content") is None and entry.get("bytes", -1) < 0:
            # Unreadable or non-file entry: provenance only, not a change.
            continue
        lines = entry.get("content") or ""
        nlines = len(lines.splitlines())
        files.append({"path": entry["path"], "changes": nlines,
                      "additions": nlines, "deletions": 0,
                      "dirty": True, "committed": False, "untracked": True})
    files = sorted(files, key=lambda f: f["path"])
    parts: list = []
    diff_capped = False

    def _take(header: str, text: str, cap: int) -> None:
        nonlocal diff_capped
        if not (text or "").strip():
            return
        room = _SOURCE_DIFF_TOTAL_CAP - len("".join(parts))
        if room <= 0:
            diff_capped = True
            return
        chunk = text[:min(cap, room)]
        if len(text) > len(chunk):
            diff_capped = True
        parts.append(f"{header}\n{chunk}")
        if not chunk.endswith("\n"):
            parts.append("\n")

    _take("--- committed (base..HEAD) ---", committed_diff, 30000)
    _take("--- staged (--cached) ---", staged_diff, 15000)
    _take("--- unstaged ---", unstaged_diff, 15000)
    for entry in untracked_contents:
        content = entry.get("content")
        if content:
            _take(f"--- untracked: {entry['path']} ---", content,
                  _UNTRACKED_PER_FILE_CHARS)
    diff_text = "".join(parts)[:_SOURCE_DIFF_TOTAL_CAP]
    total_ins = sum(f.get("additions", 0) for f in files)
    total_del = sum(f.get("deletions", 0) for f in files)
    files_total = (f"{len(files)}개 파일 변경, "
                   f"+{total_ins}/-{total_del}"
                   if files else "")
    has_changes = bool(files) or bool((dirty_status or "").strip())
    head_match = (live_head == recorded
                  if live_head and recorded not in (None, "", "unknown")
                  else None)
    if not has_changes:
        note = "no changes yet: planned state, no diff link"
    elif head_match is False:
        note = ("local worktree diff only (live HEAD moved past the recorded "
                "commit); unpublished commits have no remote URL")
    else:
        note = ("local worktree diff only; unpublished commits have no remote URL")
    return {"baseRef": state.get("base_branch"),
            "baseCommit": base,
            "head": recorded,
            "liveHead": live_head,
            "headMatch": head_match,
            "branch": state.get("branch"),
            "lane": state.get("lane"),
            "fileDiffs": files,
            "filesTotal": files_total,
            "filesSource": ("live-worktree-diff-vs-base"
                            if base not in (None, "", "unknown")
                            else "live-worktree-dirty-only"),
            "untracked": untracked,
            "untrackedContents": untracked_contents,
            "untrackedCapped": untracked_capped,
            "untrackedSkipped": skipped,
            "dirtyStatus": dirty_status,
            "diff": diff_text,
            "diffCommitted": committed_diff[:30000],
            "diffStaged": staged_diff[:15000],
            "diffUnstaged": unstaged_diff[:15000],
            "diffCapped": diff_capped,
            "hasChanges": has_changes,
            "planned": not has_changes,
            "availability": "live",
            "historical": False,
            "worktreeAvailable": True,
            "patchSource": "+".join(provenance) if provenance else "none",
            "error": "" if code == 0 else f"git status failed: {status_err.strip()[-160:]}",
            "localOnly": True,
            "note": note}


def _stored_source_changes(state: dict, rdir, base, recorded, live_head,
                           work_available: bool) -> dict:
    """sourceChanges when the live worktree is gone: an explicitly
    HISTORICAL stored patch (never presented as current), or an explicit
    unavailable state (never a planned/no-changes lie)."""
    files: list = []
    sources: list = []
    for step in ("review", "fix"):
        p = rdir / f"{step}.diffstat.txt"
        if not p.is_file():
            continue
        try:
            parsed, _ = _parse_diffstat_text(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        if parsed:
            sources.append(f"{step}.diffstat.txt")
        for f in parsed:
            if f["path"] not in {e["path"] for e in files}:
                files.append(f)
    files = sorted(files, key=lambda f: f["path"])
    patch = ""
    patch_name = ""
    for name in ("review.diff.txt", "fix.diff.txt"):
        p = rdir / name
        if p.is_file():
            try:
                patch = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if patch.strip():
                patch_name = name
                break
            patch = ""
    if patch_name or files:
        total = next(
            (ln.strip() for src in sources
             for ln in _read_text_lines(rdir / src)
             if "file" in ln and "changed" in ln),
            f"{len(files)}개 파일 변경 (과거 기록)")
        return {"baseRef": state.get("base_branch"),
                "baseCommit": base,
                "head": recorded,
                "liveHead": live_head,
                "headMatch": None,
                "branch": state.get("branch"),
                "lane": state.get("lane"),
                "fileDiffs": files,
                "filesTotal": total,
                "filesSource": "+".join(sources) if sources else "stored-patch-artifact",
                "untracked": [],
                "untrackedContents": [],
                "untrackedCapped": False,
                "untrackedSkipped": 0,
                "dirtyStatus": "",
                "diff": patch[:_SOURCE_DIFF_TOTAL_CAP],
                "diffCommitted": "",
                "diffStaged": "",
                "diffUnstaged": "",
                "diffCapped": len(patch) > _SOURCE_DIFF_TOTAL_CAP,
                "hasChanges": True,
                "planned": False,
                "availability": "historical",
                "historical": True,
                "worktreeAvailable": work_available,
                "patchSource": (f"stored:{patch_name}"
                                if patch_name else "stored:diffstat-only"),
                "error": "",
                "localOnly": True,
                "note": ("historical stored patch: the live worktree is unavailable, "
                         "so this is the recorded artifact — not current source")}
    reason = ("run work dir missing" if not work_available
              else "worktree has no readable git metadata")
    return {"baseRef": state.get("base_branch"),
            "baseCommit": base,
            "head": recorded,
            "liveHead": live_head,
            "headMatch": None,
            "branch": state.get("branch"),
            "lane": state.get("lane"),
            "fileDiffs": [],
            "filesTotal": "",
            "filesSource": "none",
            "untracked": [],
            "untrackedContents": [],
            "untrackedCapped": False,
            "untrackedSkipped": 0,
            "dirtyStatus": "",
            "diff": "",
            "diffCommitted": "",
            "diffStaged": "",
            "diffUnstaged": "",
            "diffCapped": False,
            "hasChanges": False,
            "planned": False,
            "availability": "unavailable",
            "historical": False,
            "worktreeAvailable": work_available,
            "patchSource": "none",
            "error": f"source unavailable: {reason}; no stored patch artifact",
            "localOnly": True,
            "note": ("source unavailable: worktree missing and no stored patch; "
                     "not a no-changes state")}


def _read_text_lines(path) -> list:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


_FILE_PATCH_COMMITTED_CAP = 30000
_FILE_PATCH_STAGED_CAP = 15000
_FILE_PATCH_UNSTAGED_CAP = 15000
_FILE_PATCH_TOTAL_CAP = 60000
_FILE_PATCH_UNTRACKED_CHARS = 30000


def _normalize_selected_path(raw) -> str:
    """Exact repo-relative path for filePatch, or raise ApiError.

    Absolute paths, `..` segments, and empty values are rejected —
    never resolved to another file.
    """
    rel = (raw or "").strip().strip('"')
    if not rel:
        raise ApiError(400, "path is required")
    if len(rel) > 1024:
        raise ApiError(400, "path is too long")
    if rel.startswith("/"):
        raise ApiError(400, f"absolute path not allowed: {raw!r}")
    rel = rel.replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in rel.split("/")):
        raise ApiError(400, f"invalid path: {raw!r}")
    return "/".join(parts)


def _patch_section_paths(patch_text: str) -> list:
    """New-side repo-relative paths from unified-diff `diff --git` headers."""
    import re as _re
    paths = []
    for line in (patch_text or "").splitlines():
        m = _re.match(r"diff --git\s+\S+\s+(\S+)\s*$", line)
        if not m:
            continue
        new = m.group(1)
        if new.startswith("b/"):
            new = new[2:]
        elif new == "/dev/null":
            continue
        new = new.strip('"')
        if new and new not in paths:
            paths.append(new)
    return paths


def _extract_file_section(patch_text: str, rel: str) -> str:
    """The exact unified-diff section for one new-side path, else ""."""
    import re as _re
    cur: list | None = None
    out: list = []
    for line in (patch_text or "").splitlines():
        m = _re.match(r"diff --git\s+\S+\s+(\S+)\s*$", line)
        if m:
            new = m.group(1)
            if new.startswith("b/"):
                new = new[2:]
            new = new.strip('"')
            cur = [] if new == rel else None
            if cur is not None:
                out = cur
            continue
        if cur is not None:
            cur.append(line)
    return "\n".join(out).strip("\n")


def _single_untracked_entry(work, rel: str):
    """One untrackedContents entry (existing format) for an exact path.

    Full per-file budget (30000 chars), independent of the aggregate
    20-file/30000-total cap — so a file the aggregate summary cut still
    renders in full here. Binary/unreadable files carry provenance
    with content null. Returns None when the path is not a readable
    regular file.
    """
    from pathlib import Path as _Path
    p = _Path(work) / rel
    entry = {"path": rel, "bytes": -1, "truncated": False,
             "binary": False, "content": None}
    try:
        if p.is_symlink() or not p.is_file():
            return None
        try:
            p.resolve().relative_to(_Path(work).resolve())
        except ValueError:
            raise ApiError(400, f"path escapes the run worktree: {rel}")
        size = p.stat().st_size
        entry["bytes"] = size
        raw = p.read_bytes()[:_UNTRACKED_READ_LIMIT + 1]
        if b"\x00" in raw[:8192]:
            entry["binary"] = True
            entry["truncated"] = size > _UNTRACKED_READ_LIMIT
            return entry
        text = raw[:_UNTRACKED_READ_LIMIT].decode("utf-8", errors="replace")
        if len(raw) > _UNTRACKED_READ_LIMIT:
            entry["truncated"] = True
        if len(text) > _FILE_PATCH_UNTRACKED_CHARS:
            text = text[:_FILE_PATCH_UNTRACKED_CHARS]
            entry["truncated"] = True
        entry["content"] = text
    except ApiError:
        raise
    except OSError:
        return None
    return entry


def _live_file_patch(work, base, rel: str, untracked_relevant: set,
                     *, git_bound=None) -> dict:
    """Exact per-file live patch: committed + staged + unstaged + untracked.

    Safe git arg lists only (`-- <path>`); never falls back to another
    file or the repo-wide diff. Each section is bounded and `capped`
    says so explicitly. 301: bound to the prep record.
    """
    committed = staged = unstaged = ""
    if base not in (None, "", "unknown"):
        code, out, _ = _git_text(work, ["diff", f"{base}..HEAD", "--", rel],
                                 git_bound=git_bound)
        committed = out if code == 0 else ""
    code, out, _ = _git_text(work, ["diff", "--cached", "--", rel],
                             git_bound=git_bound)
    staged = out if code == 0 else ""
    code, out, _ = _git_text(work, ["diff", "--", rel],
                             git_bound=git_bound)
    unstaged = out if code == 0 else ""
    capped = False

    def _cut(text: str, cap: int) -> str:
        nonlocal capped
        if len(text) > cap:
            capped = True
            return text[:cap]
        return text

    committed = _cut(committed, _FILE_PATCH_COMMITTED_CAP)
    staged = _cut(staged, _FILE_PATCH_STAGED_CAP)
    unstaged = _cut(unstaged, _FILE_PATCH_UNSTAGED_CAP)
    untracked_contents = []
    if rel in untracked_relevant:
        entry = _single_untracked_entry(work, rel)
        if entry is not None:
            untracked_contents = [entry]
    parts = []
    for header, text in (("--- committed (base..HEAD) ---", committed),
                         ("--- staged (--cached) ---", staged),
                         ("--- unstaged ---", unstaged)):
        if (text or "").strip():
            parts.append(f"{header}\n{text}")
            if not text.endswith("\n"):
                parts.append("\n")
    diff = "".join(parts)
    if len(diff) > _FILE_PATCH_TOTAL_CAP:
        diff = diff[:_FILE_PATCH_TOTAL_CAP]
        capped = True
    return {"path": rel, "diff": diff,
            "diffCommitted": committed, "diffStaged": staged,
            "diffUnstaged": unstaged,
            "untrackedContents": untracked_contents,
            "availability": "live", "historical": False,
            "capped": capped, "error": ""}


def _stored_file_patch(rdir, rel: str) -> dict:
    """Exact per-file historical patch from stored artifacts, else explicit.

    The section for this exact path comes from the stored unified patch
    (`review.diff.txt`/`fix.diff.txt`); when the path is listed but no
    section exists (diffstat-only history) the file is explicitly
    unavailable — never planned, never another file's patch.
    """
    patch_name, patch_text = "", ""
    for name in ("review.diff.txt", "fix.diff.txt"):
        p = rdir / name
        try:
            text = p.read_text(encoding="utf-8") if p.is_file() else ""
        except OSError:
            continue
        if text.strip():
            patch_name, patch_text = name, text
            break
    section = _extract_file_section(patch_text, rel) if patch_text else ""
    if section:
        if len(section) > _FILE_PATCH_TOTAL_CAP:
            section = section[:_FILE_PATCH_TOTAL_CAP]
            capped = True
        else:
            capped = False
        return {"path": rel, "diff": section,
                "diffCommitted": "", "diffStaged": "", "diffUnstaged": "",
                "untrackedContents": [],
                "availability": "historical", "historical": True,
                "capped": capped, "error": ""}
    return {"path": rel, "diff": "",
            "diffCommitted": "", "diffStaged": "", "diffUnstaged": "",
            "untrackedContents": [],
            "availability": "unavailable", "historical": False,
            "capped": False,
            "error": (f"source unavailable for {rel}: stored patch "
                      + (f"{patch_name} has no section for this path"
                         if patch_name else "no stored patch artifact")
                      + "; not a no-changes state")}


def _file_patch(state: dict, sc: dict, rel: str) -> dict:
    """filePatch for one validated changed path (live or historical).

    Unknown paths, traversal, and symlink escapes raise ApiError
    (400/404 with a reason) — never another file's content.
    """
    rdir = store.run_dir(state["id"])
    work = rdir / "work"
    git_ok = work.is_dir() and (work / ".git").exists()
    # 301: per-file live patch needs the prep record too; without
    # it the request falls through to the historical section below
    # (explicit unavailable, never live bytes).
    bound, refused = _serve_git_bound(state) if git_ok else (None, False)
    if refused:
        git_ok = False
    if sc.get("availability") == "live" and git_ok:
        allowed = ({f["path"] for f in sc.get("fileDiffs", [])}
                   | set(sc.get("untracked", [])))
        if rel not in allowed:
            raise ApiError(
                404, f"unknown path '{rel}': not in this run's changed "
                     "files; no other file substituted")
        target = (work / rel).resolve() if (work / rel).exists() \
            else (work.resolve() / rel)
        try:
            target.relative_to(work.resolve())
        except ValueError:
            raise ApiError(400, f"path escapes the run worktree: {rel}")
        if (work / rel).is_symlink():
            try:
                (work / rel).resolve().relative_to(work.resolve())
            except ValueError:
                raise ApiError(400, f"path escapes the run worktree: {rel}")
        code, status_out, _ = _git_text(work, ["status", "--porcelain"],
                                        git_bound=bound)
        relevant: set = set()
        if code == 0:
            raw = [ln[3:].strip().strip('"')
                   for ln in status_out.splitlines() if ln.startswith("??")]
            relevant_set, _ = _relevant_untracked(work, raw,
                                                  git_bound=bound)
            relevant = set(relevant_set)
        return _live_file_patch(work, state.get("baseCommit"), rel, relevant,
                                git_bound=bound)
    # No live worktree: the exact historical section, or explicit per-file
    # unavailable. `planned` is never claimed here.
    allowed = ({f["path"] for f in sc.get("fileDiffs", [])})
    for name in ("review.diff.txt", "fix.diff.txt"):
        p = rdir / name
        try:
            text = p.read_text(encoding="utf-8") if p.is_file() else ""
        except OSError:
            continue
        allowed |= set(_patch_section_paths(text))
    if rel not in allowed:
        raise ApiError(
            404, f"unknown path '{rel}': not in this run's recorded "
                 "changes; no other file substituted")
    return _stored_file_patch(rdir, rel)


def api_source_changes(payload: dict) -> dict:
    """GET /api/work/changes?run=<id>[&path=<exact relative path>].

    Run-only shape is unchanged (backward compatible; `filePatch` is
    None then). With `path`, adds `filePatch` for exactly that changed
    file — committed/staged/unstaged/untracked sections, bounded and
    explicit — or a 400/404 reason, never another file's patch.
    """
    run_id = (payload.get("run") or payload.get("id") or "").strip()
    if not run_id:
        raise ApiError(400, "run is required")
    try:
        state = store.load(run_id)
    except SystemExit as exc:
        raise ApiError(404, str(exc)) from exc
    sc = _source_changes(state)
    raw_path = payload.get("path")
    if raw_path is None or not str(raw_path).strip():
        return {"run": run_id, "sourceChanges": sc, "filePatch": None}
    rel = _normalize_selected_path(raw_path)
    return {"run": run_id, "sourceChanges": sc,
            "filePatch": _file_patch(state, sc, rel)}


def api_refs(payload: dict) -> dict:
    """GET /api/refs?repo=<name>&numbers=1,2: cached reference metadata.

    Repo-scoped, batched, cached: stored metadata first, one gh read per
    unknown number at most, explicit unknown/error entries otherwise.
    """
    from . import refs as _refs
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    profile = _load_profile(repo)
    raw = payload.get("numbers", "")
    if isinstance(raw, list):
        parts = [str(n).strip().lstrip("#") for n in raw]
    else:
        parts = [p.strip().lstrip("#") for p in str(raw or "").split(",")]
    numbers = [p for p in parts if p]
    return {"repo": repo,
            "references": _refs.lookup_many(profile.get("github", ""), numbers)}


def api_plan_start(payload: dict) -> dict:
    """Create a plan session. No quota: the plan turn streams afterwards."""
    _require_gate()
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    _load_profile(repo)
    session = chat.create(repo, "", "유지보수 계획",
                          muse.agent_info(model=_settings_model()))
    return {"id": session["id"]}


def api_plan_approve(payload: dict) -> dict:
    """Approve a pending plan: clear the gate and start autonomous maintain."""
    _require_gate()
    sid = (payload.get("id") or "").strip()
    if not sid:
        raise ApiError(400, "id is required")
    try:
        session = chat.load(sid)
    except SystemExit as exc:
        raise ApiError(404, str(exc)) from exc
    repo = session.get("plan_pending")
    if not repo:
        raise ApiError(409, "no pending plan for this session")
    job = api_job_maintain({"repo": repo})["job"]
    session.pop("plan_pending", None)
    chat.save(session)
    return {"job": job}


def api_job_maintain(payload: dict) -> dict:
    _require_gate()
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    _load_profile(repo)
    if not _lease_probe():
        raise ApiError(409, "worker busy: another maintenance worker holds "
                            "the global lock; nothing started")
    job = _job_new("maintain", repo)
    if job is None:
        raise ApiError(409, "another job is already running")
    import argparse as _ap
    ns = _ap.Namespace(repo=repo, start_run=True, no_issues=False,
                       executor="muse", model=_settings_model(), only=None)
    ev = _JOB_CANCEL[job["id"]]
    _job_run(job, _guarded_resume_fn(repo, None,
                                     lambda: cli.cmd_watch(ns, cancel_event=ev)))
    return {"job": job}


def api_job_run(payload: dict) -> dict:
    """Start or resume one repo+issue run as a background job."""
    _require_gate()
    repo = (payload.get("repo") or "").strip()
    issue = str(payload.get("issue") or "").strip()
    if not repo or not issue:
        raise ApiError(400, "repo and issue are required")
    profile = _load_profile(repo)
    decision = cli.decide_issue(repo, issue)
    if decision in ("skip-waiting", "skip-active", "skip-done", "skip-draft"):
        raise ApiError(409, f"issue #{issue} is {decision}; nothing to run")
    if not _lease_probe():
        raise ApiError(409, "worker busy: another maintenance worker holds "
                            "the global lock; nothing started")
    job = _job_new("run", f"{repo}#{issue}")
    if job is None:
        raise ApiError(409, "another job is already running")
    import argparse as _ap
    ev = _JOB_CANCEL[job["id"]]
    if decision == "resume":
        prev = store.latest_run(repo, issue)
        ns = _ap.Namespace(run=prev["id"], executor="muse", model=_settings_model(), only=None)
        _job_run(job, _guarded_resume_fn(repo, prev["id"],
                                         lambda: cli.cmd_resume(ns, cancel_event=ev)))
    else:
        title = ""
        try:
            for item in cli._open_issues(profile["github"]):
                if str(item["number"]) == issue:
                    title = item.get("title", "")
        except Exception:  # noqa: BLE001 - title stays empty when gh is offline
            pass
        body = cli._github_issue_body(profile["github"], issue)
        ns = _ap.Namespace(repo=repo, issue=issue, title=title, body=body,
                           body_file=None, allow_duplicate=False, only=None,
                           executor="muse", model=_settings_model())
        _job_run(job, _guarded_resume_fn(repo, None,
                                         lambda: cli.cmd_run(ns, cancel_event=ev)))
    return {"job": job}


def _lease_probe() -> bool:
    """Real non-blocking OS lease attempt, used only as a routing hint.

    Acquires and immediately releases the single-worker lease. The actual
    execution decision always re-acquires inside `runner.execute`, so a
    race lost after a free probe still refuses safely (no overlap) via
    the guarded thread wrapper below — never a metadata read.
    """
    owner = sched.acquire_worker_lease()
    if owner is None:
        return False
    sched.release_worker_lease(owner)
    return True


def _guarded_resume_fn(repo: str, run_id: str | None, fn):
    """Background-thread wrapper: a busy refusal never strands the run.

    On `WorkerBusy` (or a busy exit from the entry point) the SAME run is
    re-queued for later safe dispatch (when known) and the job records
    the truthful refusal — no model call happened, nothing was forked
    or lost.
    """
    def _run():
        try:
            rc = fn()
        except runner.WorkerBusy:
            rc = cli.WORKER_BUSY_EXIT
        if rc == cli.WORKER_BUSY_EXIT:
            if run_id is not None:
                try:
                    sched.requeue_run_for_dispatch(repo, run_id)
                except Exception:  # noqa: BLE001 - refusal note is authoritative
                    pass
                raise SystemExit(
                    f"worker busy; no work started, run {run_id} kept "
                    f"for later dispatch")
            raise SystemExit("worker busy; no work started (nothing created)")
        return rc
    return _run


def api_job_resume(payload: dict) -> dict:
    _require_gate()
    run_id = (payload.get("run") or "").strip()
    if not run_id:
        raise ApiError(400, "run is required")
    try:
        state = store.load(run_id)
    except SystemExit as exc:
        raise ApiError(404, str(exc)) from exc
    # Gate preflight BEFORE lease/job creation: a waiting, duplicate, or
    # watched run refuses with a truthful 409 and the existing reason —
    # no thread, no model call, no attempt, no fork. Strictly READ-ONLY:
    # never save here, so a concurrent api_job_answer durable write can
    # never be clobbered by this snapshot. Only a real recorded answer
    # (which clears the wait) unblocks a later resume; a stale refusal
    # after a concurrent answer is a benign retry.
    gate = runner.resume_gate(state)
    if gate is not None:
        wait = (state.get("waitReason") or gate)
        if gate == "duplicate":
            raise ApiError(409, f"run is a confirmed duplicate ({wait}); "
                                "run unchanged")
        if gate == "watch":
            raise ApiError(409, f"run is deferred to an external watch "
                                f"({wait}); run unchanged")
        raise ApiError(409, f"run waits for operator ({wait}); answer "
                            "first, run unchanged")
    if not _lease_probe():
        raise ApiError(409, "worker busy: another maintenance worker holds "
                            "the global lock; run unchanged")
    job = _job_new("resume", run_id)
    if job is None:
        raise ApiError(409, "another job is already running")
    import argparse as _ap
    ns = _ap.Namespace(run=run_id, executor="muse", model=_settings_model(),
                       only=None, actor="dashboard")
    ev = _JOB_CANCEL[job["id"]]
    _job_run(job, _guarded_resume_fn(state.get("repo", ""), run_id,
                                     lambda: cli.cmd_resume(ns, cancel_event=ev)))
    return {"job": job}


def api_job_answer(payload: dict) -> dict:
    """Answer a run's question; resume now when the worker is free.

    The answer is always saved durably first. While another worker owns
    the lease, no resume thread starts: the SAME run is queued for later
    safe dispatch and the response says saved/queued (started=false)
    instead of faking a running job. The `job` key is always present so
    existing `r.job.id` + poll flows keep working untouched.
    """
    _require_gate()
    run_id = (payload.get("run") or "").strip()
    answer = (payload.get("answer") or "").strip()
    if not run_id:
        raise ApiError(400, "run is required")
    try:
        state = store.record_answer(run_id, answer)
    except SystemExit as exc:
        msg = str(exc)
        if "not waiting" in msg:
            raise ApiError(409, msg) from exc
        if "answer is required" in msg:
            raise ApiError(400, msg) from exc
        raise ApiError(404, msg) from exc
    repo = state.get("repo", "")
    queued = False
    try:
        queued = bool(sched.requeue_run_for_dispatch(repo, run_id).get("requeued"))
    except Exception:  # noqa: BLE001 - the durable answer is already saved
        queued = False
    if not _lease_probe():
        from .util import utcnow as _utcnow
        job = _job_new("resume", run_id)
        note = ("answer saved; worker busy so the same run is queued "
                "for later dispatch (not started)")
        if job is None:
            # Process-local job slot busy too: stay truthful without a
            # phantom job record — the dashboard error path toasts this.
            raise ApiError(409, f"answer saved; {note}")
        job["status"] = "done"
        job["rc"] = 0
        job["note"] = note
        job["ended_at"] = _utcnow()
        return {"job": job, "saved": True, "queued": queued,
                "started": False, "run": run_id, "note": note}
    job = _job_new("resume", run_id)
    if job is None:
        raise ApiError(409, "answer saved but another job is already "
                            "running; run queued for later dispatch")
    import argparse as _ap
    ns = _ap.Namespace(run=run_id, executor="muse", model=_settings_model(), only=None)
    ev = _JOB_CANCEL[job["id"]]
    _job_run(job, _guarded_resume_fn(repo, run_id,
                                     lambda: cli.cmd_resume(ns, cancel_event=ev)))
    return {"job": job, "saved": True, "queued": queued,
            "started": True, "run": run_id}


def api_job_cancel(payload: dict) -> dict:
    jid = (payload.get("id") or "").strip()
    if not jid:
        raise ApiError(400, "id is required")
    job = _JOBS.get(jid)
    if job is None:
        raise ApiError(404, f"unknown job '{jid}'")
    if job["status"] != "running":
        raise ApiError(409, f"job {jid} is not running")
    ev = _JOB_CANCEL.get(jid)
    if ev is None:
        raise ApiError(409, f"job {jid} cannot be cancelled")
    ev.set()
    return {"cancelled": jid}


def _probe_checkout_manifest(checkout) -> None:
    """Open the first manifest file that exists (lexists).

    Runs inside checkout_io: on a wedged root the open blocks and
    the caller times out honestly. `Path.is_file` is False for FIFOs,
    so without this probe `manifests.discover` would skip a wedged
    package.json entirely and the repo would look healthy.
    A missing manifest returns normally and lets discover() decide;
    a broken link raises OSError (not ApiError) and is reported as
    a check failure, not a wedge.
    """
    for manifest in ("package.json", "pubspec.yaml"):
        cand = Path(checkout) / manifest
        if os.path.lexists(cand):
            with open(cand, "rb"):
                pass
            return


def _repo_drift_summary(name: str, profile: dict, checkout) -> tuple:
    """(drift text, rows-or-None). Cached scan rows preferred over live network."""
    st = sched.load(name)
    cached = None
    try:
        hist = (st.get("history") or [])[-1:]
        _ = hist
    except Exception:
        pass
    last_rows = st.get("last_rows")
    if isinstance(last_rows, list) and last_rows:
        rows = last_rows
    else:
        rows = None
    if profile.get("dependencies") or True:
        # G9: checkout stat/discover can block on a wedged volume —
        # both run behind the bounded helper. A timeout is an
        # unreadable checkout (fail-closed), never stale-as-fresh.
        if checkout and checkout_io(Path(checkout).is_dir,
                                     path=str(checkout)):
            # Prove the checkout is readable before trusting discover:
            # a FIFO package.json is skipped by is_file() checks, so
            # without this the wedged repo would look healthy.
            checkout_io(_probe_checkout_manifest, checkout,
                        path=str(checkout))
            if rows is None:
                try:
                    disc = checkout_io(manifests.discover,
                                       Path(checkout),
                                       path=str(checkout))
                    rows = [{"name": d["name"], "kind": d["manager"],
                             "declared": d["declared"], "installed": d.get("installed"),
                             "latest": None, "drift": False, "lane": "main"}
                            for d in disc["deps"][:40]]
                    drift = (f"manifest {len(disc['deps'])}개 추적 중 "
                             f"(최근 스캔 {st.get('last_scan') or '없음'}; "
                             "정확한 drift는 스캔 실행 후 표시)")
                except ApiError as exc:
                    # G9: a checkout timeout is not a row-level check
                    # failure — let it reach overview() so the row is
                    # marked unreadable and the band stays degraded.
                    raise
                except Exception as exc:  # noqa: BLE001
                    drift = f"check failed: {exc}"
                    return drift, None
            else:
                bad = [r for r in rows if r.get("drift")]
                err = [r for r in rows if r.get("error")]
                unpub = [r for r in rows if r.get("unpublished")]
                parts = []
                if bad:
                    parts.append(f"DRIFT: {', '.join(r['name'] for r in bad)}")
                if unpub:
                    parts.append(f"unpublished: {', '.join(r['name'] for r in unpub)}")
                if err:
                    parts.append(f"errors: {len(err)}")
                drift = "; ".join(parts) if parts else f"ok ({len(rows)} tracked)"
        else:
            drift = "checkout not linked"
            rows = None
    else:
        drift = "no dependencies declared"
    return drift, rows


SCAN_OVERDUE_GRACE_S = 600


def _parse_ts(value) -> float | None:
    """Epoch seconds for an ISO timestamp, or None when missing/unparsable."""
    if not value or not isinstance(value, str):
        return None
    try:
        from datetime import datetime as _dt
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = _dt.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    try:
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _scan_freshness(st: dict, *, now_ts: float, worker_busy: bool) -> dict:
    """Honest scan delay indication (read-only, never guesses a cause).

    Enabled repos whose next_scan is overdue past a modest grace with no
    recent completion report delayed; a genuinely fresh scan clears the
    warning. Historical scan_ok is kept separately and untouched.
    """
    import time as _time
    now = now_ts if now_ts is not None else _time.time()
    last_ts = _parse_ts(st.get("last_scan"))
    next_ts = _parse_ts(st.get("next_scan"))
    if last_ts is not None and (now - last_ts) < SCAN_OVERDUE_GRACE_S:
        return {"state": "fresh", "detail": "최근 스캔됨"}
    if not st.get("enabled"):
        return {"state": "off", "detail": "스케줄러 꺼짐"}
    if next_ts is None:
        return {"state": "unknown", "detail": "다음 스캔 미정"}
    if now > next_ts + SCAN_OVERDUE_GRACE_S:
        if worker_busy:
            return {"state": "delayed-processing",
                    "detail": "다음 스캔 예정이 지났으나 작업 처리 중이라 지연됨"}
        return {"state": "delayed-check",
                "detail": "다음 스캔 예정이 지남 — 서비스 확인 필요"}
    return {"state": "ok", "detail": "예정 내"}


def _sane_stamp_pair(stamp, st: dict, now_ts) -> bool:
    """Non-empty sane observation stamp for display only.

    True only when the stamp parses, is not in the future beyond
    a small clock-skew allowance, and is not older than this
    repo's expected scan cadence plus the overdue grace (the same
    interval+grace horizon the scan-freshness readout uses).
    Invalid, future, or stale stamps project unknown.
    """
    import time as _time
    from . import sched as _sched
    ts = _parse_ts(stamp)
    if ts is None:
        return False
    now = now_ts if now_ts is not None else _time.time()
    try:
        now_f = float(now)
    except (TypeError, ValueError):
        return False
    if ts > now_f + 60:
        return False
    try:
        interval = int(st.get("interval_s", _sched.DEFAULT_INTERVAL_S))
    except (TypeError, ValueError):
        interval = _sched.DEFAULT_INTERVAL_S
    return (now_f - ts) <= (interval + SCAN_OVERDUE_GRACE_S)


def _branch_projection(st: dict, checkout, now_ts=None) -> dict:
    """Pure snapshot projection of the scan-time branch observation.

    Returns a dict with has_next True/False/None plus
    branch_next_observed_at/checkout/reason for the operator. No
    git, no scheduler writes. True/False only when scan_ok is
    exactly True, branch_next is an explicit bool, and the stored
    observation and scan timestamps are non-empty, sane, matching,
    and not stale or future relative to now_ts. Any legacy,
    missing, mismatched, stale, future, invalid, in-progress, or
    failed state projects None (unknown), never absence. True and
    False are last-scan observations, never guaranteed current
    branch state; display code must not authorize branch actions
    from them. Checkout binding is display-only and never rebinds
    source identity.
    """
    unknown = {"has_next": None, "branch_next_observed_at": None,
               "branch_next_checkout": None,
               "branch_next_reason": "아직 확인하지 않음"}
    if not checkout:
        return dict(unknown, branch_next_reason="연결된 체크아웃 없음")
    if st.get("current_job") or st.get("stale"):
        return dict(unknown, branch_next_reason="스캔 상태 불안정")
    if st.get("scan_ok") is not True:
        return dict(unknown, branch_next_reason="마지막 스캔 실패")
    state = st.get("branch_next")
    if state is not True and state is not False:
        return dict(unknown, branch_next_reason="관찰 기록 없음")
    if st.get("branch_next_checkout") != checkout:
        return dict(unknown, branch_next_reason="체크아웃 변경됨")
    observed_at = st.get("branch_next_at")
    last_scan = st.get("last_scan")
    if (not observed_at or not last_scan or observed_at != last_scan
            or not _sane_stamp_pair(observed_at, st, now_ts)):
        return dict(unknown, branch_next_reason="관찰 기록 없음")
    return {"has_next": state,
            "branch_next_observed_at": observed_at,
            "branch_next_checkout": checkout,
            "branch_next_reason": "마지막 스캔 관찰"}


def _is_actionable_review(item: dict) -> bool:
    """waiting-review items resting in an active external watch are not
    actionable human review: they wait on upstream, not on a person."""
    if item.get("status") != "waiting-review":
        return False
    watch = item.get("watch")
    return not (isinstance(watch, dict) and watch.get("active", True))


def _chat_summary(session: dict) -> dict:
    """Chat-list projection for /api/overview (S8-c): full sessions
    carry transcripts (39 KB for 73). List sites read only id, repo,
    issue, title, run_id and created_at (chatHeadFor, sidebar, palette,
    scope line); the transcript loads from /api/session on open
    (renderChatShell -> loadTranscript). `turns` is a count, since
    nothing on the list path reads turn bodies."""
    s = session if isinstance(session, dict) else {}
    turns = s.get("turns")
    return {"id": s.get("id"), "repo": s.get("repo"),
            "issue": s.get("issue"), "title": s.get("title"),
            "run_id": s.get("run_id"),
            "created_at": s.get("created_at"),
            "turns": len(turns) if isinstance(turns, list)
                     else (turns if isinstance(turns, int) else 0)}


def _run_summary(state: dict) -> dict:
    """Run-list projection for /api/overview (S8-b): the bytes hog was
    200 full records (848 KB: commands, evidence, artifacts, history).

    Every field the dashboard reads off O.runs items is kept: id, repo,
    issue, title, dep, target, lane, branch, created_at, waitReason,
    steps as {name: {status, started_at}}, disposition (whole, small),
    and watch projected to {active, settled_at} when present. Detail
    views (work inspector, run inspector, palette open) fetch the full
    record from /api/work or /api/run on open — never from this list.

    Deliberately absent (absent on stored records too, so behavior is
    identical): top-level kind/status/updated_at/outcome. Status
    derives from steps + waitReason; outcome is detail-computed."""
    r = state if isinstance(state, dict) else {}
    steps = r.get("steps") or {}
    slim = {}
    if isinstance(steps, dict):
        for name, entry in steps.items():
            if isinstance(entry, dict):
                slim[name] = {"status": entry.get("status"),
                              "started_at": entry.get("started_at")}
    out = {"id": r.get("id"), "repo": r.get("repo"),
           "issue": r.get("issue"), "title": r.get("title"),
           "dep": r.get("dep"), "target": r.get("target"),
           "lane": r.get("lane"), "branch": r.get("branch"),
           "created_at": r.get("created_at"),
           "waitReason": r.get("waitReason"), "steps": slim,
           "disposition": r.get("disposition")}
    watch = r.get("watch")
    if isinstance(watch, dict):
        out["watch"] = {"active": watch.get("active"),
                        "settled_at": watch.get("settled_at")}
    return out


def overview(payload=None) -> dict:
    """Full dashboard envelope. S8-a: per-section ms timings.

    Pass {"timing": True} (GET /api/overview?timing=1) to get a
    "timing" breakdown field; sections sum to roughly the total. A
    debug line goes to stderr (dashboard.log) whenever the total
    exceeds 1 s, so the real-home hog is measured, never guessed."""
    from .util import pc_id, utcnow
    from . import pcconfig
    import sys as _sys
    import time as _time

    _t0 = _time.perf_counter()
    _marks = {}
    _last = [_t0]

    def _mark(name):
        now = _time.perf_counter()
        _marks[name] = int((now - _last[0]) * 1000)
        _last[0] = now

    pc = pcconfig.load()
    try:
        lock_status = sched.global_lock_status()
    except Exception:  # noqa: BLE001 - missing/failed probe reads as unknown
        lock_status = None
    worker_busy_global = bool((lock_status or {}).get("live"))
    now_ts = _time.time()
    # M2.25b: the reviews tab's stored rollup drafts come from the
    # glance summary snapshot (one read-only file read, never
    # recomputed per request) — the same rows the glance shows.
    rollup_rows = _overview_rollup_rows()
    repo_rows = []
    for name in repos.list_all():
        profile = repos.load(name)
        checkout = (pc.get("checkouts", {}) or {}).get(name)
        # G9: a checkout timeout raises ApiError(502) out of
        # _repo_drift_summary — the row keeps the honest message as
        # its drift and is flagged unreadable (fail-closed), so the
        # glance band can stay degraded instead of saying 정상.
        # G9b: a global parked-cap refusal (code "checkout_busy")
        # names stuck roots, never this one — it must not mark a
        # healthy repo 응답 없음.
        checkout_busy = False
        try:
            drift, _ = _repo_drift_summary(name, profile, checkout)
            checkout_unreadable = False
        except ApiError as exc:
            if getattr(exc, "code", None) == "checkout_busy":
                drift, checkout_unreadable = exc.message, False
                checkout_busy = True
            else:
                drift, checkout_unreadable = exc.message, True
        st = sched.load(name)
        queue = st.get("queue", [])
        counts = {"queued": 0, "running": 0, "blocked": 0, "waiting-review": 0,
                  "deferred": 0}
        for item in queue:
            s = item.get("status")
            watch = item.get("watch")
            watched = isinstance(watch, dict) and watch.get("active", True)
            live_status = s in ("queued", "running", "blocked",
                                "waiting-review")
            if s == "waiting-review" and watched:
                # External-watch items rest in waiting-review in storage
                # but need no human review action: count them only as
                # deferred, never as both review-needed and deferred.
                pass
            elif s in counts:
                counts[s] += 1
            if watched and live_status:
                # Cancelled/terminal items may retain historical watch
                # fields; they are not deferred work.
                counts["deferred"] += 1
        last_outcome = None
        if st.get("history"):
            last_outcome = st["history"][-1]
        next_action = "스캔 실행"
        if counts["blocked"]:
            next_action = f"차단 {counts['blocked']}건 검토"
        elif counts["waiting-review"]:
            next_action = f"리뷰 대기 {counts['waiting-review']}건 확인"
        elif counts["queued"]:
            next_action = f"대기 {counts['queued']}건 실행 검토"
        elif not st.get("enabled"):
            next_action = "스케줄러 시작"
        worker_busy = worker_busy_global or bool(st.get("current_job"))
        freshness = _scan_freshness(st, now_ts=now_ts,
                                    worker_busy=worker_busy)
        repo_rows.append({"name": name, "github": profile.get("github"),
                          "checkout": checkout, "drift": drift,
                          "checkout_unreadable": checkout_unreadable,
                          "checkout_busy": checkout_busy,
                          "checks_configured": bool(profile.get("checks")),
                          "sched_enabled": st.get("enabled", False),
                          "execute_enabled": st.get("execute_enabled", False),
                          "last_scan": st.get("last_scan"),
                          "next_scan": st.get("next_scan"),
                          "heartbeat": st.get("heartbeat"),
                          "queue_counts": counts,
                          "current_job": st.get("current_job"),
                          "sched_error": st.get("error"),
                          "scan_ok": st.get("scan_ok"),
                          "scan_freshness": freshness,
                          "sources": st.get("sources", {}),
                          "issues_error": st.get("issues_error"),
                          "last_outcome": last_outcome,
                          "next_action": next_action,
                          **_branch_projection(st, checkout, now_ts)})
        if name in rollup_rows:
            repo_rows[-1]["rollup"] = rollup_rows[name]
    _mark("repos")
    log = paths.home() / "watch.log"
    tail = ""
    try:
        tail = log.read_text(encoding="utf-8")[-4000:]
    except OSError:
        pass
    jobs = sorted(_JOBS.values(), key=lambda j: j.get("started_at", ""), reverse=True)
    try:
        service = _maintenance_service()
    except Exception:  # noqa: BLE001
        service = {"installed": None, "note": "service probe failed"}
    if isinstance(lock_status, dict) and lock_status:
        worker_lock = {"owner": lock_status.get("owner"),
                       "pid": lock_status.get("pid"),
                       "at": lock_status.get("at"),
                       "heartbeat": lock_status.get("heartbeat"),
                       "live": bool(lock_status.get("live"))}
    else:
        # Missing/failed probe reads as unknown: never claim running
        # solely because stale owner metadata exists. Historical owner
        # metadata stays in the lock file itself as inactive evidence.
        worker_lock = None
    _mark("meta")
    # S8-b: review_batches comes from the batches snapshot file (same
    # obs the reviews tab shows) — no fresh live map on the request
    # path. Cold start computes once synchronously, as /api/batches
    # does; batches must still never break overview.
    snap = _read_batches_snapshot()
    if snap is None:
        try:
            batches.refresh_batches_snapshot("overview-coldstart")
        except Exception:  # noqa: BLE001 - fall through to re-read
            pass
        snap = _read_batches_snapshot()
    if isinstance(snap, dict) and isinstance(snap.get("summary"), dict):
        review_summary = snap["summary"]
        cards = snap.get("batches") or []
        review_progress_at = max(
            [(c.get("updated_at") or "") for c in cards
             if isinstance(c, dict)] + [""])
        rb_obs, rb_at = snap.get("obs") or "", snap.get("at") or ""
    else:
        try:
            batch_list = batches.list_batches()
            review_summary = batches.summarize(
                batch_list, _live_map_for_batches(batch_list))
            review_progress_at = max(
                [b.get("updated_at") or "" for b in batch_list] + [""])
        except Exception:  # noqa: BLE001 - batches never break overview
            review_summary, review_progress_at = {}, ""
        rb_obs, rb_at = "live", utcnow()
    _mark("batches")
    runs = [_run_summary(r) for r in store.list_runs(200)]
    chats = [_chat_summary(s) for s in chat.list_sessions(200)]
    _mark("runs_chats")
    out = {"home": str(paths.home()), "pc": pc_id(), "at": utcnow(),
           "repos": repo_rows, "runs": runs,
           "chats": chats, "jobs": jobs, "watch_log_tail": tail,
           "review_batches": review_summary,
           "review_batches_obs": rb_obs, "review_batches_at": rb_at,
           "review_progress_at": review_progress_at,
           "maintenance_service": service,
           "worker_lock": worker_lock}
    total_ms = int((_time.perf_counter() - _t0) * 1000)
    if total_ms > 1000:
        print(f"overview slow: total_ms={total_ms} " +
              " ".join(f"{k}_ms={v}" for k, v in _marks.items()),
              file=_sys.stderr, flush=True)
    if bool((payload or {}).get("timing")):
        out["timing"] = {"total_ms": total_ms, "sections": dict(_marks)}
    return out


def safe_run_file(run_id: str, name: str) -> Path | None:
    if not run_id or not name or "/" in name or name.startswith("."):
        return None
    base = paths.home() / "drafts" if run_id == "_drafts" else store.run_dir(run_id)
    dest = (base / name).resolve()
    try:
        dest.relative_to(base.resolve())
    except ValueError:
        return None
    return dest if dest.is_file() else None


class ReadGateError(Exception):
    """A shared batch-source read generation failed; fail closed."""


class ReadGateTimeout(ReadGateError):
    """A coalesced read waiter expired before its generation published."""


# Coalescing gate for batch source reads (revision 66). Concurrent
# dashboard polls (tabs, watchers, cron curl) used to each run a full
# live_map_with_diag pass; now requests whose closure needs the exact
# same read-key set share one (live, diag) pass per generation.
#
# Freshness (next-generation): a request that arrives while generation k
# is in flight targets k+1, which starts when k finishes; a response only
# ever consumes a generation that started after the request arrived. No
# TTL, no last-good: entries are deleted when nothing is active or
# pending, and a lone later request starts a fresh read.
#
# Fail-closed: a failed generation is delivered to its waiters, each of
# whom raises their OWN exception instance chained from the leader's (one
# shared exception object is never raised across threads); the existing
# endpoint error paths then shape the outcome (overview: {} summary,
# current_qa: unknown, batches/batch: the existing error response). A
# waiter that outlives _READGATE_WAITER_BUDGET_S raises ReadGateTimeout
# instead of reading on its own (a fallback read under a wedge would
# re-create the pile-up) and removes itself from the pending set; a
# generation left with no consumers is never started. The leader
# publishes even when the read raises BaseException, so waiters always
# wake. The shared (live, diag) pair is never mutated downstream:
# evaluate/card_diagnostic/summarize only read it.
_READGATE_LOCK = threading.Lock()
_READGATE_COND = threading.Condition(_READGATE_LOCK)
_READGATE = {}

# Worst legitimate wait is remainder(k) + one full k+1: an idle full
# frame is ~5 s wall, a loaded one ~25 s, so ~60 s even piled up. Each
# git probe already caps itself at runner._GIT_PROBE_TIMEOUT (30 s,
# user-owned). 150 s = 5x the probe cap with wide headroom over any
# legitimate chained wait; beyond it the read path is wedged, so fail
# closed now and let the next generation retry from scratch.
_READGATE_WAITER_BUDGET_S = 150


def _readgate_run(read_key, entry, gen, read_fn):
    """Lead generation gen: read outside the lock, then publish."""
    try:
        outcome = (True, read_fn())
    except BaseException as exc:  # noqa: BLE001 - waiters must wake
        outcome = (False, exc)
    _readgate_publish(read_key, entry, gen, outcome)
    ok, payload = outcome
    if ok:
        return payload
    raise payload


def _readgate_publish(read_key, entry, gen, outcome):
    """Deliver one generation to its waiters; elect the next leader."""
    ok, payload = outcome
    with _READGATE_COND:
        if _READGATE.get(read_key) is not entry or \
                entry["in_flight"] != gen:
            return  # entry was reset; a late publish is dropped
        entry["in_flight"] = None
        keep = []
        for w in entry["waiters"]:
            if w["target"] <= gen:
                w["result"] = (True, payload) if ok else (False, payload)
            else:
                keep.append(w)
        entry["waiters"] = keep
        if keep:
            # Every still-pending waiter arrived before this publish, so
            # the next generation (which starts now) is fresh for all of
            # them; normalize targets and let the first waiter lead it.
            new_gen = min(w["target"] for w in keep)
            if new_gen <= gen:
                new_gen = gen + 1
            entry["in_flight"] = new_gen
            entry["started"] = time.monotonic()
            for w in keep:
                w["target"] = new_gen
            keep[0]["is_leader"] = True
            keep[0]["lead_gen"] = new_gen
        else:
            # No consumers left: the next generation is never started and
            # nothing is retained.
            del _READGATE[read_key]
        _READGATE_COND.notify_all()


def _readgate_wait(read_key, entry, waiter, read_fn):
    """Wait for a generation that starts after this request arrived."""
    deadline = time.monotonic() + _READGATE_WAITER_BUDGET_S
    with _READGATE_COND:
        while True:
            if waiter["is_leader"]:
                gen = waiter["lead_gen"]
                try:
                    entry["waiters"].remove(waiter)
                except ValueError:
                    pass
                break
            if waiter["result"] is not None:
                ok, payload = waiter["result"]
                if ok:
                    return payload
                raise ReadGateError(
                    "shared batch source read failed") from payload
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    entry["waiters"].remove(waiter)
                except ValueError:
                    pass
                if not entry["waiters"] and entry["in_flight"] is None \
                        and _READGATE.get(read_key) is entry:
                    del _READGATE[read_key]
                raise ReadGateTimeout(
                    "batch source read still in flight after %ds"
                    % _READGATE_WAITER_BUDGET_S)
            _READGATE_COND.wait(timeout=remaining)
    return _readgate_run(read_key, entry, gen, read_fn)


def _readgate_join(read_key, read_fn):
    """Run read_fn coalesced by read_key; return its (live, diag) pair."""
    with _READGATE_COND:
        entry = _READGATE.get(read_key)
        if entry is not None and entry["in_flight"] is None:
            # Unreachable by construction (publish elects or deletes
            # under the same lock hold); reset defensively.
            entry = None
            _READGATE.pop(read_key, None)
        if entry is not None and not entry["waiters"] and \
                time.monotonic() - entry["started"] > \
                _READGATE_WAITER_BUDGET_S:
            # A leader that never published with nobody waiting on it
            # (normal exits always publish via finally): drop the
            # poisoned entry so the key is usable again. Its late
            # publish, if any, is dropped by identity check.
            entry = None
            _READGATE.pop(read_key, None)
        if entry is None:
            entry = {"in_flight": 1,
                     "started": time.monotonic(), "waiters": []}
            _READGATE[read_key] = entry
            mode, gen, waiter = "lead", 1, None
        else:
            waiter = {"target": entry["in_flight"] + 1, "result": None,
                      "is_leader": False, "lead_gen": None}
            entry["waiters"].append(waiter)
            mode, gen = "wait", None
    if mode == "lead":
        return _readgate_run(read_key, entry, gen, read_fn)
    return _readgate_wait(read_key, entry, waiter, read_fn)


def _live_for_batches(batch_list: list) -> tuple:
    """(live_by_id, diag_by_id): one shared snapshot pass over the
    reachable-inheritance closure, plus per-key read diagnostics for
    failed keys only. Inherited batches are evaluated inside the
    parent's map, so one pass covers owned primaries too; missing
    entries mean unreadable, never matching.

    Concurrent requests needing the exact same read-key set share one
    pass per generation (revision 66 gate above); per-request evaluate
    and obs semantics are unchanged."""
    closed = batches.closure(batch_list)
    read_key = frozenset(batches.read_keys(closed))
    shared, shared_diag = _readgate_join(
        read_key, lambda: batches.live_map_with_diag(closed))
    return ({b.get("id"): shared for b in batch_list},
            {b.get("id"): shared_diag for b in batch_list})


def _live_map_for_batches(batch_list: list) -> dict:
    """Fresh source states incl. the reachable-inheritance closure.

    Inherited batches are evaluated inside the parent's map, so one
    shared snapshot pass covers owned primaries too; missing entries
    mean unreadable, never matching.
    """
    by_live, _ = _live_for_batches(batch_list)
    return by_live


# Only raster proofs render inline. SVG/XML/HTML/logs and anything else
# are served as inert plaintext so an opened proof can never execute
# script on the application origin.
_RASTER_MIME = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".gif": "image/gif",
                ".webp": "image/webp"}


def _evidence_mime(name: str) -> str:
    import os
    hit = _RASTER_MIME.get(os.path.splitext(name)[1].lower())
    if hit:
        return hit
    return "text/plain; charset=utf-8"


def _review_terminal_text(run_id: str) -> str:
    """Registered final review text (never the raw JSON stream).

    Parses review.reviewer.log.txt with the existing delta parser and
    keeps the last terminal summary. Empty when no final was recorded:
    callers must say so explicitly instead of inventing findings.
    """
    path = store.run_dir(run_id) / "review.reviewer.log.txt"
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    final = ""
    for line in text.splitlines():
        try:
            kind, chunk = muse.parse_delta_line(line)
        except Exception:  # noqa: BLE001 - one bad line never kills report
            continue
        if kind == "terminal" and chunk:
            final = chunk
    return final


def _rewrite_report_links(text: str, workdir: str, changed=()) -> str:
    """Make authoritative changed paths open the in-app diff viewer.

    `[label](/abs/.../work/<rel>)` becomes `[label](workfile:<rel>)`,
    and a bare `` `<rel>` `` span naming an authoritative changed file
    becomes `[<rel>](workfile:<rel>)` — every extension alike (.lock,
    .pbxproj included; no extension guessing). Either renders as a
    button opening the recorded per-file diff. Any other
    absolute-filesystem link text is kept as plain label text so a
    broken localhost/Users URL is never shown as a link.
    """
    import re as _re
    prefix = workdir.rstrip("/") + "/"

    def _sub(match):
        label, target = match.group(1), match.group(2)
        if target.startswith(prefix):
            rel = target[len(prefix):].lstrip("/")
            if rel and ".." not in rel.split("/"):
                return f"[{label}](workfile:{rel})"
        return label

    out = _re.sub(r"\[([^\]]+)\]\((/[^)\s]+)\)", _sub, text or "")
    known = {c for c in (changed or [])
             if c and ".." not in c.split("/") and "*" not in c}

    def _tick(match):
        rel = match.group(1)
        if rel in known:
            return f"[{rel}](workfile:{rel})"
        return match.group(0)

    out = _re.sub(r"`([^`\n]{1,160})`", _tick, out)
    if known:
        # Bare authoritative paths become viewer links too, so lock and
        # project files written as plain words (yarn.lock, Podfile.lock,
        # project.pbxproj) still open the in-app diff. Only paths the run
        # actually changed — no extension guessing. Already-linked
        # workfile: targets and markdown/link contexts are left alone.
        ordered = sorted(known, key=len, reverse=True)
        pat = "|".join(_re.escape(k) for k in ordered)

        def _bare(match):
            pre, rel, post = (match.group(1), match.group(2),
                              match.group(3))
            return f"{pre}[{rel}](workfile:{rel}){post}"

        out = _re.sub(r"(^|[^A-Za-z0-9_./`\[(\-:])(" + pat + r")"
                      r"($|[^A-Za-z0-9_./`)\]:])", _bare, out)
    return out


def _current_qa_for_run(state: dict, run_id: str) -> dict:
    """Fresh current-integration QA projection for one run (read-time).

    Finds the registered batch in the SAME repo whose primary is bound
    to this run and evaluates it live: integration worktree + HEAD +
    fingerprint + clean must all match the recorded source and every
    required gate must pass with fresh inputs. Computed on every call
    — never cached client- or server-side — so a drifted source or a
    newer import can never keep showing an old green. Anything
    unreadable degrades to an explicit none/unknown state, never to a
    borrowed pass.
    """
    repo = state.get("repo") or ""
    try:
        all_batches = batches.list_batches()
    except Exception:  # noqa: BLE001 - QA must never break work
        return {"state": "unknown", "reason": "묶음 목록을 읽지 못함"}
    hit = None
    for b in all_batches:
        if not isinstance(b, dict) or b.get("repo") != repo:
            continue
        for p in b.get("primaries") or []:
            if isinstance(p, dict) and p.get("run_id") == run_id:
                hit = (b, p)
                break
        if hit:
            break
    if not hit:
        return {"state": "none"}
    b, p = hit
    batch_id = b.get("id") or ""
    try:
        by_live, by_diag = _live_for_batches([b])
        live = by_live.get(batch_id) or {}
        bdiag = by_diag.get(batch_id)
    except Exception:  # noqa: BLE001 - unreadable source is a state
        return {"state": "unknown", "batch": batch_id,
                "reason": "현재 소스를 읽지 못함"}
    try:
        ev = batches.evaluate(b, live, live_diag=bdiag, record=True)
    except Exception:  # noqa: BLE001 - evaluation failure is a state
        return {"state": "unknown", "batch": batch_id,
                "reason": "평가 실패"}
    key = batches.candidate_key(repo, p.get("dep"), p.get("target"),
                                p.get("lane"))
    res = next((r for r in ev.get("results") or []
                if r.get("key") == key), None)
    if res is None:
        return {"state": "unknown", "batch": batch_id,
                "reason": "후보 평가 없음"}
    lane = p.get("lane") or ""
    catalog = ((b.get("policy") or {}).get("gates") or {})
    gates = []
    src = {"worktree": "", "head": "", "fingerprint": "",
           "clean": False, "status": ""}
    scope_files: list | None = None
    scope_error = ""
    scope_base = ""
    for integ in b.get("integrations") or []:
        if (integ.get("lane") or "") != lane:
            continue
        src = {"worktree": integ.get("worktree") or "",
               "head": integ.get("head") or "",
               "fingerprint": integ.get("fingerprint") or "",
               "clean": bool((live.get("wt:" + (integ.get("worktree")
                                                or "")) or {}).get("clean")),
               "status": integ.get("status") or ""}
        for g in integ.get("gates") or []:
            if not isinstance(g, dict):
                continue
            file_ev = [e for e in g.get("evidence") or []
                       if isinstance(e, dict) and e.get("kind") != "note"]
            key = g.get("key") or ""
            spec = catalog.get(key) or {}
            gates.append({"key": key,
                          "label": spec.get("label") or key,
                          "status": g.get("status") or "",
                          "note": g.get("note") or "",
                          "evidence": len(file_ev)})
        # Integration file scope: prepared worktree, recorded base
        # against recorded integration head — the same range the batch
        # changes viewer reads. Candidate-only diffs are a different
        # (smaller) scope and stay labeled as original history.
        scope_base = (b.get("origin") or {}).get("base_head") or ""
        try:
            scope_files, scope_error = _integration_changed_files(
                src["worktree"], scope_base, src["head"])
        except Exception:  # noqa: BLE001 - scope degrades, QA survives
            scope_files, scope_error = None, "변경 목록을 읽지 못함"
    passed = sum(1 for g in gates if g["status"] == "pass")
    review = p.get("review") or {}
    inherited = []
    for h in b.get("inherited") or []:
        if isinstance(h, dict) and h.get("batch"):
            inherited.append({"batch": h.get("batch"),
                              "group": h.get("group") or "",
                              "count": h.get("count") or 0})
    return {"state": "ready" if res.get("ready") else "stale",
            "batch": batch_id, "purpose": b.get("purpose") or "",
            "lane": lane, "ready": bool(res.get("ready")),
            "reasons": (res.get("reasons") or [])[:3],
            "leaf": res.get("leaf") or "",
            "diag": batches._bound_diag(res.get("diag")),
            "review_verdict": review.get("verdict") or "",
            "review_findings": review.get("findings") or [],
            "gates": gates, "gates_passed": passed,
            "gates_total": len(gates),
            "source": src, "inherited": inherited,
            "files": scope_files or [],
            "files_error": scope_error or "",
            "files_base": scope_base[:12] if scope_base else "",
            "evaluated_at": ev.get("evaluated_at") or ""}


_FASTCHECK_REPORT_MAX_BYTES = 1048576
_FASTCHECK_REPORT_TAIL_CHARS = 4000
_FASTCHECK_REPORT_TAIL_LINES = 60
_FASTCHECK_REPORT_MAX_STEPS = 8
_FASTCHECK_REPORT_MAX_WARNINGS = 5


def _fc_tail(text, recorded_truncated=False):
    """Bounded tail view of a recorded log tail (chars + lines).

    Returns (tail, truncated): truncation is True when our caps cut
    anything or the recorded log_truncated flag says the stored log
    itself was cut. Never invents content.
    """
    if not isinstance(text, str):
        return "", recorded_truncated is True
    truncated = bool(recorded_truncated is True)
    if len(text) > _FASTCHECK_REPORT_TAIL_CHARS:
        text = text[-_FASTCHECK_REPORT_TAIL_CHARS:]
        truncated = True
    lines = text.splitlines()
    if len(lines) > _FASTCHECK_REPORT_TAIL_LINES:
        lines = lines[-_FASTCHECK_REPORT_TAIL_LINES:]
        truncated = True
    return "\n".join(lines), truncated


def _fc_isolation_view(step):
    """Provenance flags of one recorded step; missing reads unknown.

    A missing flag is None (displayed as unrecorded), never a
    default claim such as network-blocked.
    """
    writes = step.get("write_allow")
    writes_view = None
    writes_omitted = 0
    if isinstance(writes, list):
        writes_view = [str(p) for p in writes[:8]]
        writes_omitted = max(0, len(writes) - len(writes_view))
    net = step.get("network_sandbox")
    prof = step.get("sandbox_profile")
    files = step.get("file_sandbox")
    kids = step.get("children_verified_gone")
    inst = step.get("install")
    log_bytes = step.get("log_bytes")
    digest = step.get("log_sha256")
    return {"network": net if isinstance(net, (str, bool)) else None,
            "install": inst if isinstance(inst, bool) else None,
            "profile": prof if isinstance(prof, str) else None,
            "files": files if isinstance(files, str) else None,
            "writes": writes_view, "writes_omitted": writes_omitted,
            "children_gone": kids if isinstance(kids, bool) else None,
            "log_truncated": step.get("log_truncated") is True,
            "log_bytes": log_bytes if isinstance(log_bytes, int)
            and not isinstance(log_bytes, bool) else None,
            "log_sha256": digest if isinstance(digest, str) else None}


def _fastcheck_step_view(step):
    """Compact display view of one recorded fastcheck step receipt.

    Strict types: rc is int or None, timed_out and elapsed_s keep
    their recorded types only when they are bool/number, otherwise
    None (unknown). Isolation flags ride along; missing is unknown.
    """
    argv = step.get("argv")
    if isinstance(argv, list):
        argv = " ".join(str(a) for a in argv[:12])[:500]
    elif not isinstance(argv, str):
        argv = ""
    rc = step.get("rc")
    rc = rc if isinstance(rc, int) and not isinstance(rc, bool) else None
    timed = step.get("timed_out")
    timed = timed if isinstance(timed, bool) else None
    elapsed = step.get("elapsed_s")
    elapsed = elapsed if isinstance(elapsed, (int, float)) \
        and not isinstance(elapsed, bool) else None
    tail, truncated = _fc_tail(step.get("log_tail") or step.get("tail"),
                               step.get("log_truncated"))
    return {"name": str(step.get("name") or ""),
            "gate": str(step.get("gate") or ""),
            "rc": rc,
            "timed_out": timed,
            "elapsed_s": elapsed,
            "argv": argv,
            "head": str(step.get("head") or ""),
            "fingerprint": str(step.get("fingerprint") or ""),
            "tail": tail,
            "truncated": truncated,
            "isolation": _fc_isolation_view(step)}


def _fc_num(value):
    """Number view: int/float stay, anything else is None (unknown)."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


def _fc_rc(value):
    """Exit-code view: int stays, anything else is None (unknown)."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _fc_step_label(argv):
    """Display label derived from the actual recorded command shape.

    The real Turbo substeps carry no name; the label only redescribes
    the recorded argv tokens (version check, dry-run graph, forced
    run) with a generic fallback. Never invents execution.
    """
    text = argv if isinstance(argv, str) else ""
    if "--version" in text:
        return "버전 확인"
    if "--dry" in text:
        return "그래프 확인(dry-run)"
    if "--force" in text:
        return "실제 실행(force)"
    parts = text.split()
    if len(parts) > 1:
        return " ".join(parts[:2])[:40]
    if parts:
        return parts[0][:40]
    return "단계"


def _fastcheck_consumer_view(consumer):
    """Bounded view of the recorded consumer gate, or None.

    Two recorded shapes stay projectable: the Turbo consumer (actual
    version/dry-graph/run substep evidence with strict types and
    provenance flags) and the generic consumer (actual top-level
    argv/rc/tail/isolation). Turbo markers are the turbo_steps key,
    a recorded expected version, or turbo in the recorded script
    name — only a Turbo receipt must carry Turbo substeps, so a
    generic consumer pass (e.g. a Webpack build consumer) is never
    turned unknown for lacking them. A pass claim contradicting its
    own evidence (nonzero rc, timeout, malformed substeps) is not
    projectable (None). Skip/fail records project what is there.
    """
    if not isinstance(consumer, dict):
        return None
    status = consumer.get("status")
    if status not in ("pass", "fail", "skip"):
        return None
    script = str(consumer.get("script") or "")
    name = str(consumer.get("name") or "")
    is_turbo = ("turbo_steps" in consumer
                or bool(str(consumer.get("turbo_version_expected")
                            or ""))
                or "turbo" in (script + " " + name).lower())
    views = []
    if is_turbo:
        raw_steps = consumer.get("turbo_steps")
        if not isinstance(raw_steps, list):
            return None
        for entry in raw_steps[:8]:
            if not isinstance(entry, dict):
                return None
            argv = entry.get("argv")
            if isinstance(argv, list):
                argv = " ".join(str(a) for a in argv[:12])[:300]
            elif not isinstance(argv, str):
                argv = ""
            tail, truncated = _fc_tail(entry.get("tail"),
                                       entry.get("log_truncated"))
            views.append({"name": str(entry.get("name") or ""),
                          "label": _fc_step_label(argv),
                          "argv": argv, "rc": _fc_rc(entry.get("rc")),
                          "timed_out": entry.get("timed_out")
                          if isinstance(entry.get("timed_out"), bool)
                          else None,
                          "elapsed_s": _fc_num(entry.get("elapsed_s")),
                          "tail": tail, "truncated": truncated,
                          "isolation": _fc_isolation_view(entry)})
        if status == "pass" and not views:
            return None
        if status == "pass":
            # Every recorded substep of a claimed pass must itself
            # be an actual integer rc 0 with timed_out false: a red,
            # missing, boolean or timed-out substep behind an outer
            # pass is a contradiction, never a green result.
            for v in views:
                if v["rc"] != 0 or v["timed_out"] is not False:
                    return None
    rc = _fc_rc(consumer.get("rc"))
    timed = consumer.get("timed_out")
    timed = timed if isinstance(timed, bool) else None
    if status == "pass" and (rc != 0 or timed is not False):
        return None
    out = {"status": status, "turbo": is_turbo,
           "script": script, "label": str(consumer.get("label") or ""),
           "manager": str(consumer.get("manager") or ""),
           "name": name, "rc": rc, "timed_out": timed,
           "elapsed_s": _fc_num(consumer.get("elapsed_s")),
           "reason": str(consumer.get("reason") or ""),
           "note": str(consumer.get("note") or "")}
    if is_turbo:
        out["turbo_version_expected"] = str(
            consumer.get("turbo_version_expected") or "")
        out["turbo_version_actual"] = str(
            consumer.get("turbo_version_actual") or "")
        out["turbo_task_count"] = _fc_num(
            consumer.get("turbo_task_count"))
        out["turbo_scope"] = str(consumer.get("turbo_scope") or "")
        out["turbo_steps"] = views
        tasks = consumer.get("turbo_tasks")
        if isinstance(tasks, list):
            out["turbo_tasks"] = [str(t) for t in tasks[:16]]
    else:
        argv = consumer.get("argv")
        if isinstance(argv, list):
            argv = " ".join(str(a) for a in argv[:12])[:300]
        elif not isinstance(argv, str):
            argv = ""
        out["argv"] = argv
        tail, truncated = _fc_tail(consumer.get("log_tail"),
                                   consumer.get("log_truncated"))
        out["tail"] = tail
        out["truncated"] = truncated
        rec = consumer.get("receipt")
        out["isolation"] = _fc_isolation_view(
            rec if isinstance(rec, dict) else consumer)
    return out


def _fastcheck_warnings(summary, consumer_view=None):
    """Observed warning lines from recorded tails (display only).

    Consumer evidence first so the Turbo outputs warning is not
    buried behind install logs; duplicates collapsed; capped with
    an explicit omitted count. Returns (warnings, omitted).
    """
    candidates = []
    if isinstance(consumer_view, dict):
        for entry in consumer_view.get("turbo_steps") or []:
            if isinstance(entry, dict) and entry.get("tail"):
                candidates.append(("consumer", entry["tail"]))
    for step in (summary.get("steps") or []):
        if isinstance(step, dict):
            tail = step.get("log_tail") or step.get("tail") or ""
            if isinstance(tail, str) and tail:
                candidates.append((str(step.get("name") or ""), tail))
    found = []
    seen = set()
    omitted = 0
    for name, tail in candidates:
        for line in tail.splitlines():
            low = line.lower()
            if "warn" in low or "경고" in line:
                short = line.strip()[:200]
                if short in seen:
                    omitted += 1
                    continue
                seen.add(short)
                if len(found) >= _FASTCHECK_REPORT_MAX_WARNINGS:
                    omitted += 1
                    continue
                found.append({"step": name, "line": short})
    return found, omitted


def _fastcheck_hold_view(current_qa):
    """Bounded structured view of the actual registered hold, if any.

    Reads the hold ({by, reason, at}) from the registered QA batch —
    the stored decision verbatim, bounded. No hold on the batch means
    no hold claim; an unreadable batch means explicit unknown. Never
    invents a hold or reuses an unrelated timestamp.
    """
    if not isinstance(current_qa, dict):
        return {"state": "unknown", "reason": "현재 QA를 읽지 못함"}
    if current_qa.get("state") == "none":
        return {"state": "none"}
    batch_id = current_qa.get("batch")
    if not isinstance(batch_id, str) or not batch_id:
        return {"state": "unknown", "reason": "묶음 식별자 없음"}
    try:
        target = None
        for b in batches.list_batches():
            if isinstance(b, dict) and b.get("id") == batch_id:
                target = b
                break
    except Exception:  # noqa: BLE001 - batch reads never break the report
        target = None
    if target is None:
        return {"state": "unknown", "reason": "묶음을 읽지 못함"}
    held = target.get("hold")
    if not isinstance(held, dict) or not (held.get("by")
                                          or held.get("reason")):
        return {"state": "none"}
    at = held.get("at")
    raw_reason = str(held.get("reason") or "")
    return {"state": "held",
            "by": str(held.get("by") or "")[:80],
            "reason": raw_reason[:500],
            "reason_truncated": len(raw_reason) > 500,
            "at": at if isinstance(at, str) else None}


def fastcheck_report_for_run(state, run_id, prep=None, current_qa=None):
    """Bounded latest-run fastcheck report for one work run (read-only).

    Projects the op-owned .fastcheck.json receipt plus the live
    current_qa snapshot into one operator verdict. The verdict binds
    the actual receipt bytes (sha256 vs the gate's receipt_sha256),
    the receipt's recorded source pins (ordinary profile step
    head/fingerprint vs the op result identity) and the live source
    (current_qa.source head/fingerprint/clean). Anything missing,
    malformed, unreadable, hash-mismatched, op-mismatched or drifted
    reports explicit unknown/stale/historical — never a current pass.
    A hold-driven stale QA is approval state, not byte drift: only
    HEAD/fingerprint/clean differences count as drift.
    """
    import hashlib
    from . import m2 as _m2
    try:
        if prep is None:
            prep = batches.latest_integration_op(run_id)
    except Exception:  # noqa: BLE001 - prep state never breaks work
        return {"state": "unknown", "reason": "준비 기록을 읽지 못함"}
    if not isinstance(prep, dict):
        return {"state": "missing", "reason": "준비된 통합 기록 없음"}
    if str(prep.get("run_id") or "") != str(run_id or ""):
        return {"state": "unknown", "reason": "준비/run 불일치"}
    op_id = str(prep.get("op_id") or prep.get("op") or "")
    try:
        safe_op = _m2.check_op_id(op_id)
    except ValueError:
        return {"state": "unknown", "reason": "op 식별자 오류"}
    gate = None
    for g in prep.get("gates") or []:
        if isinstance(g, dict) and g.get("key") == "fastchecks":
            gate = g
            break
    result = prep.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    recorded = {"head": str(result.get("head") or ""),
                "fingerprint": str(result.get("fingerprint") or ""),
                "path": str(result.get("path") or "")}
    if gate is None:
        return {"state": "missing", "op": safe_op,
                "reason": "검사 기록 없음", "recorded": recorded}
    if not isinstance(gate.get("status"), str):
        return {"state": "unknown", "op": safe_op,
                "reason": "게이트 형식 오류", "recorded": recorded}
    try:
        from . import fastcheck as _fc
        receipt_path = _fc.op_fastcheck_path(safe_op)
        ops_dir = receipt_path.parent.resolve()
        if receipt_path.resolve().parent != ops_dir:
            return {"state": "unknown", "op": safe_op,
                    "reason": "기록 경로 이탈", "recorded": recorded}
        with open(receipt_path, "rb") as fh:
            raw = fh.read(_FASTCHECK_REPORT_MAX_BYTES + 1)
        if len(raw) > _FASTCHECK_REPORT_MAX_BYTES:
            return {"state": "unknown", "op": safe_op,
                    "reason": "기록 oversize", "recorded": recorded}
    except FileNotFoundError:
        # Genuinely never executed: no receipt bytes exist. A pending
        # gate then truthfully reports not_run — but the prepare-
        # conflict wording requires an actually blocked prepare; an
        # ordinary done/pending op must not be labeled a conflict.
        # Anything else stays unknown, never a pass.
        if gate.get("status") == "pending":
            if str(prep.get("status") or "") == "blocked":
                return {"state": "not_run", "op": safe_op,
                        "reason": "준비 충돌로 미실행 — 검사가 실행되지 않음",
                        "recorded": recorded}
            return {"state": "not_run", "op": safe_op,
                    "reason": "검사 실행 기록 없음 — 검사가 실행되지 않음",
                    "recorded": recorded}
        return {"state": "unknown", "op": safe_op,
                "reason": "기록을 읽지 못함", "recorded": recorded}
    except (OSError, ValueError):
        return {"state": "unknown", "op": safe_op,
                "reason": "기록을 읽지 못함", "recorded": recorded}
    try:
        summary = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return {"state": "unknown", "op": safe_op,
                "reason": "기록 손상", "recorded": recorded}
    rop = summary.get("op") if isinstance(summary, dict) else None
    if (not isinstance(summary, dict) or not isinstance(rop, str)
            or not rop or rop not in (safe_op, op_id)):
        return {"state": "unknown", "op": safe_op,
                "reason": "기록 op 불일치", "recorded": recorded}
    if not isinstance(summary.get("ok"), bool):
        return {"state": "unknown", "op": safe_op,
                "reason": "기록 판정 형식 오류", "recorded": recorded}
    if not isinstance(summary.get("steps"), list):
        return {"state": "unknown", "op": safe_op,
                "reason": "기록 단계 형식 오류", "recorded": recorded}
    if summary.get("ok") is True:
        for s in summary.get("steps"):
            if not isinstance(s, dict):
                return {"state": "unknown", "op": safe_op,
                        "reason": "기록 단계 형식 오류",
                        "recorded": recorded}
            # Strict: bool rc (False == 0 in Python) or a non-bool
            # timeout string must not slip through as a clean step.
            if _fc_rc(s.get("rc")) != 0 \
                    or s.get("timed_out") is not False:
                return {"state": "unknown", "op": safe_op,
                        "reason": "기록 내부 모순",
                        "recorded": recorded}
    sha = hashlib.sha256(raw).hexdigest()
    gate_sha = gate.get("receipt_sha256")
    gate_sha = gate_sha if isinstance(gate_sha, str) else ""
    if not gate_sha or gate_sha != sha:
        return {"state": "unknown", "op": safe_op,
                "reason": "게이트/기록 해시 불일치", "recorded": recorded,
                "receipt_sha256": sha}
    attested = (summary.get("worker_attested") is True
                and gate.get("attested_by") == "worker")
    if not attested:
        return {"state": "unknown", "op": safe_op,
                "reason": "앱 검사 attestation 미기록/불일치",
                "recorded": recorded, "receipt_sha256": sha}
    pins = []
    for s in summary.get("steps"):
        if not isinstance(s, dict):
            continue
        h, f = s.get("head"), s.get("fingerprint")
        if h is None and f is None:
            continue
        if (not isinstance(h, str) or not h
                or not isinstance(f, str) or not f):
            return {"state": "unknown", "op": safe_op,
                    "reason": "기록 원본 식별자 형식 오류",
                    "recorded": recorded, "receipt_sha256": sha}
        pins.append((h, f))
    if not pins:
        return {"state": "unknown", "op": safe_op,
                "reason": "기록에 원본 식별자 없음", "recorded": recorded,
                "receipt_sha256": sha}
    if not recorded["head"] or not recorded["fingerprint"]:
        return {"state": "unknown", "op": safe_op,
                "reason": "준비 기록 식별자 없음", "recorded": recorded,
                "receipt_sha256": sha}
    if any(h != recorded["head"] or f != recorded["fingerprint"]
           for h, f in pins):
        return {"state": "stale", "op": safe_op,
                "reason": "기록 이후 준비 원본 변경", "recorded": recorded,
                "receipt_sha256": sha}
    if summary.get("ok") != (gate.get("status") == "pass"):
        return {"state": "unknown", "op": safe_op,
                "reason": "게이트/기록 판정 불일치",
                "recorded": recorded, "receipt_sha256": sha}
    consumer_view = _fastcheck_consumer_view(summary.get("consumer"))
    if summary.get("consumer") is not None and consumer_view is None:
        return {"state": "unknown", "op": safe_op,
                "reason": "consumer 증거 형식 오류",
                "recorded": recorded, "receipt_sha256": sha}
    if (summary.get("ok") is True and consumer_view is not None
            and consumer_view.get("status") == "fail"):
        return {"state": "unknown", "op": safe_op,
                "reason": "기록 내부 모순",
                "recorded": recorded, "receipt_sha256": sha}
    try:
        if current_qa is None:
            current_qa = _current_qa_for_run(state, run_id)
    except Exception:  # noqa: BLE001 - live QA never breaks the report
        current_qa = None
    if not isinstance(current_qa, dict):
        return {"state": "unknown", "op": safe_op,
                "reason": "현재 원본을 읽지 못함", "recorded": recorded,
                "receipt_sha256": sha}
    src = current_qa.get("source")
    src = src if isinstance(src, dict) else {}
    live_head = src.get("head")
    live_fp = src.get("fingerprint")
    live_wt = src.get("worktree")
    live_clean = src.get("clean")
    if (not isinstance(live_head, str) or not live_head
            or not isinstance(live_fp, str) or not live_fp):
        return {"state": "unknown", "op": safe_op,
                "reason": "현재 원본 식별자 없음", "recorded": recorded,
                "receipt_sha256": sha}
    live = {"head": live_head, "fingerprint": live_fp,
            "clean": live_clean is True,
            "worktree": live_wt if isinstance(live_wt, str) else ""}
    pending = {"count": 0, "labels": [],
               "by_status": {"fail": 0, "blocked": 0, "pending": 0,
                             "other": 0}}
    labels = []
    groups = pending["by_status"]
    for g in current_qa.get("gates") or []:
        if not isinstance(g, dict) or g.get("status") == "pass":
            continue
        labels.append(str(g.get("label") or g.get("key") or ""))
        st = g.get("status")
        if st == "fail":
            groups["fail"] += 1
        elif st == "blocked":
            groups["blocked"] += 1
        elif st == "pending":
            groups["pending"] += 1
        else:
            groups["other"] += 1
    pending = {"count": len(labels), "labels": labels[:6],
               "by_status": groups}
    hold = _fastcheck_hold_view(current_qa)
    at = summary.get("at")
    wall = summary.get("wall_s")
    if live["worktree"] != recorded["path"] or not recorded["path"]:
        live_state = "unknown"
    elif live_head != recorded["head"]:
        live_state = "drift"
    elif live_fp != recorded["fingerprint"]:
        live_state = "drift"
    elif live_clean is not True:
        live_state = "drift" if live_clean is False else "unknown"
    else:
        live_state = "current"
    base = {"op": safe_op, "recorded": recorded, "live": live,
            "pending_gates": pending, "hold": hold,
            "receipt_sha256": sha,
            "at": at if isinstance(at, str) else None,
            "wall_s": wall if isinstance(wall, (int, float))
            and not isinstance(wall, bool) else None,
            "worker_attested": True,
            "root_attested": summary.get("root_attested") is True,
            "ok": summary.get("ok")}
    if consumer_view is not None:
        base["consumer"] = consumer_view
    base["steps"] = [_fastcheck_step_view(s)
                     for s in summary.get("steps")
                     if isinstance(s, dict)][:_FASTCHECK_REPORT_MAX_STEPS]
    warnings, omitted = _fastcheck_warnings(summary, consumer_view)
    base["warnings"] = warnings
    base["warnings_omitted"] = omitted
    if summary.get("ok") is not True:
        failed = summary.get("failed")
        failed = failed if isinstance(failed, dict) else {}
        base["state"] = "fail-worker"
        base["historical"] = live_state == "drift"
        base["reason"] = ("자동 검사 실패(과거 기록 · 현재 원본과 다름)"
                          if live_state == "drift" else "자동 검사 실패")
        argv = failed.get("argv")
        argv = argv if isinstance(argv, list) else []
        frc = failed.get("rc")
        base["failed"] = {
            "argv": " ".join(str(a) for a in argv[:4])[:200],
            "rc": frc if isinstance(frc, int)
            and not isinstance(frc, bool) else None,
            "timed_out": failed.get("timed_out") is True}
        return base
    if live_state == "unknown":
        base["state"] = "unknown"
        base["reason"] = ("준비 경로 미기록"
                          if not recorded["path"] else "현재 원본 미확인")
        return base
    if live_state == "drift":
        base["state"] = "stale"
        base["reason"] = "현재 원본이 기록과 다름"
        return base
    base["state"] = "pass-worker"
    base["reason"] = "앱 자동 검사 통과 · 최종 판단은 별도 기록 참조"
    return base


_REPORT_TEXT_CAP = 20000
_REVIEW_SCOPE_READ_CAP = 131072


def _read_review_scope(run_id: str, state: dict) -> dict:
    """Bounded recorded review scope (review.scope.json identity only).

    Reads only the run-owned `review.scope.json`: the fixed filename is
    canonically resolved and must stay directly under the resolved run
    dir (outside symlinks refused). At most cap+1 bytes are ever read,
    so an oversized file is refused without loading it. Returns status
    plus short identity fields; full diffs stay behind file evidence
    links. `recorded` requires full base + head + fingerprint
    identities matching the run's recorded baseCommit / workCommit /
    workTreeFingerprint — partial identities never bind. A recorded
    match is history, never a
    current green: live source drift still invalidates via the proof
    comparison. Anything else is explicit (missing/malformed/oversized /
    mismatch/unknown) so the UI never certifies from stored fields only.
    """
    import re as _re
    rdir = store.run_dir(run_id)
    try:
        rdir_r = rdir.resolve()
        path = (rdir / "review.scope.json").resolve()
    except OSError:
        return {"status": "unknown", "error": "범위 파일 경로 확인 불가"}
    if path.parent != rdir_r or path.name != "review.scope.json":
        return {"status": "unknown", "error": "범위 파일 경로 확인 불가"}
    try:
        with open(path, "rb") as fh:
            data = fh.read(_REVIEW_SCOPE_READ_CAP + 1)
    except FileNotFoundError:
        return {"status": "missing", "error": ""}
    except OSError:
        return {"status": "unknown", "error": "범위 파일 읽기 실패"}
    if len(data) > _REVIEW_SCOPE_READ_CAP:
        return {"status": "oversized", "error": "범위 파일이 너무 큼"}
    try:
        scope = json.loads(data.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - one bad file never kills report
        return {"status": "malformed", "error": "범위 JSON 해석 불가"}
    if not isinstance(scope, dict):
        return {"status": "malformed", "error": "범위 형식 오류"}
    for key in ("base_commit", "base_ref", "head", "fingerprint",
                "lane", "branch", "base_stat"):
        val = scope.get(key)
        if val is not None and not isinstance(val, str):
            return {"status": "malformed",
                    "error": f"범위 필드 형식 오류({key})"}
    for key in ("diff_capped", "bare_git_diff_empty"):
        val = scope.get(key)
        if val is not None and not isinstance(val, bool):
            return {"status": "malformed",
                    "error": f"범위 필드 형식 오류({key})"}
    base = scope.get("base_commit") or ""
    head = scope.get("head") or ""
    fp = scope.get("fingerprint") or ""
    if not base or not head or not fp:
        return {"status": "unknown", "error": "범위 식별자 없음"}
    if (not _re.fullmatch(r"[0-9a-fA-F]{40}", base)
            or not _re.fullmatch(r"[0-9a-fA-F]{40}", head)
            or not _re.fullmatch(r"[0-9a-fA-F]{64}", fp)):
        return {"status": "unknown", "error": "범위 식별자 불완전"}
    stat = scope.get("base_stat") or ""
    stat_short = "\n".join(stat.strip().splitlines()[:8])[:800]
    info = {
        "base_ref": scope.get("base_ref") or "",
        "base_short": base[:12],
        "head_short": head[:12],
        "fingerprint_short": fp[:12],
        "lane": scope.get("lane") or "",
        "branch": scope.get("branch") or "",
        "stat_short": stat_short,
        "diff_capped": scope.get("diff_capped") is True,
        "bare_empty": scope.get("bare_git_diff_empty"),
    }
    recorded_base = state.get("baseCommit") or ""
    recorded_head = state.get("workCommit") or ""
    recorded_fp = state.get("workTreeFingerprint") or ""
    if not recorded_base or not recorded_head or not recorded_fp:
        info["status"] = "unknown"
        info["error"] = "실행 기록 식별자 없음"
        return info
    if (base == recorded_base and head == recorded_head
            and fp == recorded_fp):
        info["status"] = "recorded"
        info["error"] = ""
    else:
        info["status"] = "mismatch"
        info["error"] = "실행 기록과 범위 불일치(현재 검증 아님)"
    return info


def _review_scope_evidence(run_id: str) -> list:
    """Run-owned review scope/diff files that actually exist (bounded)."""
    rdir = store.run_dir(run_id)
    out = []
    for name in ("review.scope.json", "review.diff.txt"):
        try:
            if (rdir / name).is_file() and not (rdir / name).is_symlink():
                out.append(name)
        except OSError:
            continue
    return out


def _work_report(state: dict, run_id: str) -> dict:
    """Readable work report reusing registered evidence (no model calls).

    Groups the stored analysis judgment, the meaningful fix summary
    (FILES CHANGED / HOW TO VERIFY) and the registered final review
    text (scope/method/findings/verdict or explicit insufficient
    evidence). Never presents a truncated step note as review proof.
    Counts (files, local checks) are derived per run so history labels
    stay truthful for every run instead of hardcoding one run's scope.
    The recorded review scope is surfaced as record identity only and
    never certifies current green on its own.
    """
    steps = state.get("steps") or {}
    fix = ((steps.get("fix") or {}).get("usage") or {}).get(
        "terminal_text") or ""
    analysis = ((steps.get("analyze") or {}).get("usage") or {}).get(
        "terminal_text") or ""
    if isinstance(analysis, str) and len(analysis) > _REPORT_TEXT_CAP:
        analysis = (analysis[:_REPORT_TEXT_CAP]
                    + "\n…(잘림: 원본 로그에서 확인)")
    review = _review_terminal_text(run_id)
    workdir = str(store.run_dir(run_id) / "work")
    try:
        changed = [f.get("path") for f in
                   (_changed_files(state).get("files") or [])
                   if isinstance(f, dict) and f.get("path")]
    except Exception:  # noqa: BLE001 - links degrade, report survives
        changed = []
    e2e = steps.get("e2e") or {}
    scope_note = str(e2e.get("note") or "").strip()
    try:
        scope_info = _read_review_scope(run_id, state)
    except Exception:  # noqa: BLE001 - scope must never break report
        scope_info = {"status": "unknown", "error": "범위 확인 실패"}
    try:
        scope_files = _review_scope_evidence(run_id)
    except Exception:  # noqa: BLE001 - evidence list is best-effort
        scope_files = []
    return {"fix": _rewrite_report_links(fix, workdir, changed),
            "fix_missing": not (isinstance(fix, str) and fix.strip()),
            "analysis": _rewrite_report_links(analysis, workdir, changed),
            "analysis_missing": not (isinstance(analysis, str)
                                     and analysis.strip()),
            "review": _rewrite_report_links(review, workdir, changed),
            "review_missing": not (isinstance(review, str)
                                   and review.strip()),
            "review_scope": scope_info,
            "review_scope_files": scope_files,
            "update_kind": str(state.get("update_kind") or ""),
            "fix_files": len(changed),
            "scope_note": scope_note}


def api_integration_prepare(payload: dict) -> dict:
    """App-owned integration preparation (synchronous, lease-guarded).

    Payload carries registry ids only (run + batch); every path and
    commit derives server-side from the registered records. While the
    worker lease is held elsewhere the call reports 409 busy and starts
    nothing. Drift, conflict, and unknown ids never become success.
    """
    _require_gate()
    run_id = (payload.get("run") or "").strip()
    batch_id = (payload.get("batch") or "").strip()
    if not run_id or not batch_id:
        raise ApiError(400, "run and batch are required")
    token = sched.acquire_worker_lease(
        f"integration-prepare:{run_id}+{batch_id}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    # Normal lease heartbeat for the bounded git ops below (the same
    # mechanism run_due uses); release stays in finally either way.
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        return batches.prepare_integration(run_id, batch_id)
    except batches.IntegrationError as exc:
        raise ApiError(exc.http_status, str(exc)) from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_integration_register(payload: dict) -> dict:
    """Register a done issue prepare op as a pending-review batch.

    Payload carries ONLY {op, confirm: true}: every path, branch,
    head and manifest derives server-side from the registered op, the
    current passed fastcheck receipt and the registered profile.
    Without confirm:true the call refuses with 400 and writes nothing.
    Lease-guarded like prepare: while the worker lease is held
    elsewhere the call reports 409 busy and starts nothing.
    """
    _require_gate()
    op_id = (payload.get("op") or "").strip()
    if not op_id:
        raise ApiError(400, "op is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{op, confirm: true} to register the "
                            "completed issue integration; nothing started")
    token = sched.acquire_worker_lease(
        f"integration-register:{op_id}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    # Normal lease heartbeat for the bounded register work below (the
    # same mechanism run_due uses); release stays in finally either way.
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        return batches.register_issue_batch(op_id)
    except batches.IntegrationError as exc:
        raise ApiError(exc.http_status, str(exc)) from exc
    except batches.BatchError as exc:
        raise ApiError(409, str(exc)) from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_integration_reresolve(payload: dict) -> dict:
    """Derived lock re-resolve on a blocked prepare op (explicit only).

    Payload carries the blocked prepare op id plus confirm:true; every
    path, commit, and command derives server-side from the registered
    records. Without confirm:true the call refuses with 400 and starts
    nothing. With retry:true a BLOCKED prepare or derived op re-runs
    explicitly (attempt archived, leftover branch verified). Lease-
    guarded like prepare: while the worker lease is held elsewhere the
    call reports 409 busy and starts nothing.
    """
    _require_gate()
    op_id = (payload.get("op") or "").strip()
    if not op_id:
        raise ApiError(400, "op is required")
    retry = payload.get("retry") is True
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{op, confirm: true"
                            + (", retry: true" if retry else "") +
                            "} to derive a lock "
                            "re-resolved source; nothing started")
    token = sched.acquire_worker_lease(
        f"integration-reresolve:{op_id}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        if retry:
            return batches.retry_integration_op(op_id)
        return batches.reresolve_on_base(op_id)
    except batches.IntegrationError as exc:
        raise ApiError(exc.http_status, str(exc)) from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_integration_fastcheck(payload: dict) -> dict:
    """On-demand fastcheck on a done prepare op (explicit only).

    Payload carries the done prepare op id plus confirm:true; without
    confirm:true the call refuses with 400 and starts nothing.
    Lease-guarded like reresolve: while the worker lease is held
    elsewhere the call reports 409 busy and starts nothing. A busy
    op lock also reports 409. The receipt records origin "ui" and
    stays worker-attested, never root_attested. L2: {op,
    confirm:true, retry:true} re-runs a RED receipt (archived, never
    overwritten); a passing re-run clears that op's block
    contribution.
    """
    _require_gate()
    op_id = (payload.get("op") or "").strip()
    if not op_id:
        raise ApiError(400, "op is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{op, confirm: true} to run the fastcheck "
                            "on a done prepare op; nothing started")
    token = sched.acquire_worker_lease(
        f"integration-fastcheck:{op_id}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        return batches.run_fastcheck_for_op(
            op_id, origin="ui", retry=payload.get("retry") is True)
    except batches.IntegrationError as exc:
        raise ApiError(exc.http_status, str(exc)) from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_integration_fastcheck_rerun(payload: dict) -> dict:
    """POST /api/integration/fastcheck-rerun {op, by, confirm:true}.

    M2.16c: one explicit human re-verification of a done prepare
    op whose receipt predates a new gate (the plain manual
    fastcheck answers 409 while any receipt exists). A worker by
    or a missing confirm refuses with 400 and starts nothing;
    without the op it refuses with 400. Lease-guarded like the
    manual run: while the worker lease is held elsewhere the call
    reports 409 busy and starts nothing. A busy op lock also
    reports 409. Unknown ops and non-done (or non-prepare) ops
    report 409 with nothing started. The contract lives in
    batches.rerun_fastcheck_for_op (prev-stamp archive, one run,
    one "검증 다시 실행" event). The M2 driver never calls this.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    op_id = str(payload.get("op") or "").strip()
    by = str(payload.get("by") or "").strip()
    if not op_id:
        raise ApiError(400, "op is required")
    if by not in ("owner", "root"):
        raise ApiError(400, "by must be owner or root (a worker "
                            "can never re-run)")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{op, by, confirm: true} to re-run the "
                            "fastcheck on a done prepare op; "
                            "nothing started")
    token = sched.acquire_worker_lease(
        f"integration-fastcheck-rerun:{op_id}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        return batches.rerun_fastcheck_for_op(op_id, by=by)
    except batches.IntegrationError as exc:
        raise ApiError(exc.http_status, str(exc)) from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_integration_joint_retry(payload: dict) -> dict:
    """POST /api/integration/joint-retry {run, partner_run, batch,
    by, confirm}. M2.14b: one explicit human retry deriving the
    joint tree for two candidates that only ship together.

    Only owner/root with confirm:true, under the worker lease
    like prepare-retry (409 busy starts nothing). The M2 driver
    never calls this. The contract lives in batches.joint_retry.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    run_id = str(payload.get("run") or "").strip()
    partner_run_id = str(payload.get("partner_run") or "").strip()
    batch_id = str(payload.get("batch") or "").strip()
    by = payload.get("by")
    if not run_id or not partner_run_id or not batch_id:
        raise ApiError(400, "run, partner_run and batch are required")
    if by not in ("owner", "root"):
        raise ApiError(400, "by must be owner or root")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{run, partner_run, batch, by, confirm: true} "
                            "to derive the joint tree; nothing started")
    token = sched.acquire_worker_lease(
        f"integration-joint-retry:{run_id}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        return batches.joint_retry(run_id, partner_run_id, batch_id,
                                   by=by)
    except batches.IntegrationError as exc:
        raise ApiError(exc.http_status or 400, str(exc)) from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_integration_joint_refresh(payload: dict) -> dict:
    """POST /api/integration/joint-refresh {x_op, y_run, base,
    by, confirm}. M2.17b: one explicit human refreshed-joint
    derive for an already-derived, batch-refused member (live:
    expo 58.0.19, refused for the mixed RN tree while the
    58.0.17 joint stands).

    Only owner/root with confirm:true, under the worker lease
    like joint-retry (409 busy starts nothing). A worker caller
    or a missing confirm refuses with 400 and starts nothing. A
    pair already covered by a done or blocked joint reports 409;
    a refused derive reports 409 with the message. Success
    returns the done joint record (id x_op + "-joint-<dep>-<target>").
    The M2 driver never calls this (its path is the tick divert).
    The contract lives in batches.derive_joint_refresh.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    x_op_id = str(payload.get("x_op") or "").strip()
    y_run_id = str(payload.get("y_run") or "").strip()
    base_id = str(payload.get("base") or "").strip()
    by = payload.get("by")
    if not x_op_id or not y_run_id or not base_id:
        raise ApiError(400, "x_op, y_run and base are required")
    if by not in ("owner", "root"):
        raise ApiError(400, "by must be owner or root")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{x_op, y_run, base, by, confirm: true} "
                            "to derive the refreshed joint; "
                            "nothing started")
    try:
        x_rec = batches._load_op(x_op_id)[1]
    except batches.IntegrationError as exc:
        raise ApiError(409, f"unknown partner op: {exc}; "
                            "nothing started") from exc
    if not isinstance(x_rec, dict) or not x_rec:
        raise ApiError(409, "unknown partner op; nothing started")
    repo = str(x_rec.get("repo") or "")
    lane = str(x_rec.get("lane") or "")
    x_run = str(x_rec.get("run_id") or "")
    if not repo or not lane or not x_run:
        raise ApiError(409, "partner op carries no usable repo, lane "
                            "or run; nothing started")
    try:
        from . import m2 as _m2
        covered = any(
            str(r.get("batch_id") or "") == base_id
            and x_run in set(r.get("runs") or [])
            and y_run_id in set(r.get("runs") or [])
            for r in _m2._joint_recs(repo, lane,
                                     ("done", "blocked")))
    except Exception:  # noqa: BLE001 - unreadable never covers
        covered = False
    if covered:
        raise ApiError(409, "a joint already covers this pair on "
                            "this base; nothing started")
    token = sched.acquire_worker_lease(
        f"integration-joint-refresh:{x_op_id}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        rec = batches.derive_joint_refresh(
            repo, lane, base_id, x_op_id, y_run_id, by=by)
    except batches.IntegrationError as exc:
        raise ApiError(409, f"{exc}; nothing started") from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)
    if not isinstance(rec, dict) \
            or str(rec.get("status") or "") != "done":
        raise ApiError(409, f"{str((rec or {}).get('error') or '')[:300] or 'joint refresh refused'}; "
                            "nothing shipped")
    return rec


def api_integration_family_retry(payload: dict) -> dict:
    """One release train retried as a family (explicit only, M2.6b).

    Payload: {repo, run_id, by, confirm:true} with by owner|root. A
    worker by is refused with 403 and writes nothing; without
    confirm:true the call refuses with 400 and starts nothing. The
    contract lives in batches.family_retry (train lookup, reviewed-
    only 409, current default base, one prepare_family_integration
    run with retry, old blocked ops marked superseded_by, one
    timeline event). Lease-guarded like reresolve: while the worker
    lease is held elsewhere the call reports 409 busy and starts
    nothing. Never automatic — the driver tick never calls this.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    run_id = str(payload.get("run_id") or "").strip()
    if not repo or not run_id:
        raise ApiError(400, "repo and run_id are required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{repo, run_id, by, confirm: true} to retry "
                            "the train as one family; nothing started")
    token = sched.acquire_worker_lease(
        f"integration-family-retry:{run_id}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        return batches.family_retry(
            repo, run_id, str(payload.get("by") or ""))
    except batches.IntegrationError as exc:
        raise ApiError(exc.http_status, str(exc)) from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_integration_prepare_retry(payload: dict) -> dict:
    """M2.11a: one explicit human retry of a blocked prepare.

    Payload: {run, batch, by: owner|root, confirm: true}. A worker
    by or a missing confirm refuses with 400 and starts nothing;
    without run/batch it refuses with 400. A non-blocked op
    refuses with 409. Lease-guarded like prepare: while the worker
    lease is held elsewhere the call reports 409 busy and starts
    nothing. The contract lives in batches.prepare_retry (one
    prepare_integration(retry=True, origin="ui") run, attempt
    archived, one timeline event). Never automatic — the M2 driver
    never calls this.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    run_id = str(payload.get("run") or "").strip()
    batch = str(payload.get("batch") or "").strip()
    if not run_id or not batch:
        raise ApiError(400, "run and batch are required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{run, batch, by, confirm: true} to retry "
                            "the blocked prepare; nothing started")
    token = sched.acquire_worker_lease(
        f"integration-prepare-retry:{run_id}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        return batches.prepare_retry(
            run_id, batch, str(payload.get("by") or ""))
    except batches.IntegrationError as exc:
        raise ApiError(exc.http_status, str(exc)) from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_integration_rollup(payload: dict) -> dict:
    """Merge one lane's READY leaf batches into a single branch (explicit).

    Payload: {repo, lane, base_batch, leaves:[ids], confirm:true,
    retry:true?}. Without confirm:true the call refuses with 400 and
    starts nothing. Lease-guarded like prepare: while the worker lease
    is held elsewhere the call reports 409 busy and starts nothing.
    OpenMainty never pushes or opens PRs; the PR draft in the result is
    copyable text for the owner only. With redraft:true on a DONE op
    ({op, redraft:true, confirm:true, qa?}) only the draft fields are
    rebuilt (old draft archived, optional qa note persisted); the
    branch and worktrees are never touched.
    """
    _require_gate()
    repo = (payload.get("repo") or "").strip()
    lane = (payload.get("lane") or "").strip()
    base_batch = (payload.get("base_batch") or "").strip()
    leaves = payload.get("leaves") or []
    retry = payload.get("retry") is True
    redraft = payload.get("redraft") is True
    if redraft:
        op = (payload.get("op") or "").strip()
        if not op:
            raise ApiError(400, "op is required for redraft")
        if payload.get("confirm") is not True:
            raise ApiError(400, "confirmation required: post "
                                "{op, redraft: true, confirm: true} to "
                                "rebuild a done rollup's drafts; the "
                                "branch and worktrees are never touched")
        qa = payload.get("qa")
        if qa is not None and not isinstance(qa, dict):
            raise ApiError(400, "qa must be an object "
                                "{pr1?: ..., pr2?: ...}")
        token = sched.acquire_worker_lease(
            f"integration-rollup-redraft:{op[-12:]}")
        if token is None:
            try:
                owner = (sched.global_lock_status() or {}).get("owner") \
                    or "another worker"
            except Exception:  # noqa: BLE001 - busy reason best-effort
                owner = "another worker"
            raise ApiError(409, f"worker busy ({owner}); nothing "
                                "started and nothing queued — submit "
                                "again when free")
        try:
            sched._start_heartbeat(token)
        except Exception:  # noqa: BLE001 - heartbeat display-only
            pass
        try:
            return batches.regenerate_rollup_drafts(op, qa=qa)
        except batches.IntegrationError as exc:
            raise ApiError(exc.http_status, str(exc)) from exc
        finally:
            try:
                sched._stop_heartbeat(token)
            except Exception:  # noqa: BLE001 - release must run
                pass
            sched.release_worker_lease(token)
    if not repo or not lane or not base_batch or not leaves:
        raise ApiError(400, "repo, lane, base_batch and a non-empty "
                            "leaves list are required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{repo, lane, base_batch, leaves, confirm: true"
                            + (", retry: true" if retry else "") +
                            "} to merge the lane; nothing started")
    op_tag = f"rollup-{lane}-{(base_batch or 'base')[-12:]}"
    token = sched.acquire_worker_lease(f"integration-rollup:{op_tag}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        return batches.rollup_leaves(repo, lane, base_batch, leaves,
                                     retry=retry)
    except batches.IntegrationError as exc:
        raise ApiError(exc.http_status, str(exc)) from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_summary(_payload: dict) -> dict:
    """Serve the precomputed glance snapshot bytes verbatim.

    No evaluation, git, registry, or model at request time: the
    background refresh (tick / batch events / import) owns the cost.
    Deliberately ungated (read-only stored bytes; a user with no muse
    login still sees the overview). Injects `served_at` at serve time
    only (never stored) plus a live `subscription` hint computed from
    the login gate (presence check only, no quota). A missing snapshot
    reads as a typed unknown envelope, never a 500.
    """
    # F7: honest remedy hint. The reason mirrors chat.subscription_error()'s
    # own order (missing CLI first, then API-key billing) and reports
    # presence only — the key value is never read or echoed.
    if chat.subscription_error() is None:
        sub = {"login_needed": False}
    elif muse.which() is None:
        sub = {"login_needed": True, "reason": "cli_missing"}
    else:
        sub = {"login_needed": True, "reason": "api_key_set"}
    path = batches.summary_snapshot_path()
    try:
        snap = batches.read_json(path, default=None)
    except Exception:  # noqa: BLE001 - unreadable file degrades openly
        snap = None
    if not isinstance(snap, dict) or not snap.get("at"):
        from .util import utcnow as _now
        return {"unknown": True,
                "reason": "상태 요약을 아직 만들지 못했습니다 "
                          "(다음 스캔·가져오기 때 생성)",
                "served_at": _now(), "repos": [],
                "subscription": sub}
    out = dict(snap)
    from .util import utcnow as _now
    out["served_at"] = _now()
    out["subscription"] = sub
    return out


def api_decisions(payload: dict) -> dict:
    """GET /api/decisions?repo= — read-only decision-desk projection.

    Pure projection over the precomputed summary snapshot bytes; no
    run/store reads, no evaluation, no model. Unknown snapshot reads
    as a typed unknown envelope, never a 500.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    from . import decisions as _dec
    repo = str(payload.get("repo") or "").strip() or None
    try:
        return _dec.desk_snapshot(repo)
    except (Exception, SystemExit) as exc:
        return {"unknown": True,
                "error": f"decision desk unreadable: {exc}"[:200],
                "repos": [], "decisions": [], "supported": [],
                "unsupported": [], "revision": None}


def api_decision_detail(payload: dict) -> dict:
    """GET /api/decision/detail?repo=&id= — one bounded run read.

    Resolves the selected decision's full question, source identity,
    and step state from the stored run record. No writes, no model,
    no Git. Missing/corrupt/foreign records are explicit typed
    errors, never fabricated healthy content.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    decision_id = str(payload.get("id") or "").strip()
    if not decision_id:
        raise ApiError(400, "decision id is required")
    from . import decisions as _dec
    return _dec.decision_detail(repo, decision_id)


def api_app(_payload: dict) -> dict:
    """Which tree serves this dashboard (read-only, cached, tiny).

    `app_sha` is the promoted sha (None on the dev tree); `dev_differs`
    says whether the dev checkout moved past the promotion on the
    watched files (None when unknown). The glance fills its footer
    from /api/summary's baked app_sha and this endpoint's flag.

    Deliberately ungated: read-only promote metadata, no model."""
    from . import promote as _promote
    try:
        sha = _promote.running_sha()
    except Exception:  # noqa: BLE001 - unknown reads as unknown
        sha = None
    try:
        differs = _promote.dev_differs()
    except Exception:  # noqa: BLE001 - unknown reads as unknown
        differs = None
    # G9 health: admitted-handler count, the cap, and the open-fd
    # count where cheaply available. All home-local reads — no
    # checkout I/O — so this stays answerable while wedged.
    with _HANDLER_LOCK:
        inflight = _HANDLER_IN_FLIGHT
    try:
        open_fds = len(os.listdir("/dev/fd"))
    except OSError:
        open_fds = None
    return {"app_sha": sha, "dev_differs": differs,
            "in_flight": inflight, "handler_cap": _HANDLER_CAP,
            "open_fds": open_fds,
            "degraded": inflight >= _HANDLER_CAP - 4}


# Spawn gating only (held for microseconds around fork, never for
# the walk itself). Single-flight across requests: at most one walk
# started per _DISK_RESPAWN_S, since du on ~138 GB takes minutes.
_DISK_LOCK = threading.Lock()
_DISK_SPAWNED_AT = 0.0
_DISK_TTL_S = 1800
_DISK_RESPAWN_S = 600


def _disk_cache_path():
    return paths.home() / "cache" / "disk.json"


# The in-flight walk: a Popen handle plus its tmp output file and
# deadline. Every api_disk call poll()s it and never waits — a
# finished walk is parsed and cached in-process (tiny), an overdue
# one is killed and reaped. At most one walk in flight.
_DISK_PROC = None
_DISK_TMP = None
_DISK_DEADLINE = 0.0
_DISK_LAST_RC = None


def _spawn_disk_walk(argv=None, timeout_s=None) -> bool:
    """Start the home-size walk as fork+exec (S17r, S17/D3).

    subprocess.Popen of nice'd `du -sk -x` writing to a tmp file
    under cache/: fork+exec is safe in this threaded dashboard,
    while fork-without-exec is not (thread-held locks, Apple
    frameworks). No thread, no GIL walk, never blocking. `argv`
    and `timeout_s` exist only for the timeout test (a slow fake
    command); production always walks the home. Returns True when
    a walk was started."""
    global _DISK_PROC, _DISK_TMP, _DISK_DEADLINE
    if _DISK_PROC is not None:
        return False
    cmd = list(argv) if argv else ["nice", "-n", "10", "du", "-sk",
                                   "-x", str(paths.home())]
    wait = gc._DU_TIMEOUT_S if timeout_s is None else timeout_s
    tmp = _disk_cache_path().with_suffix(".du.tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        out = open(tmp, "wb")
    except OSError:
        return False
    newsession = {"start_new_session": True} if os.name == "posix" else {}
    try:
        _DISK_PROC = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.DEVNULL, close_fds=True, **newsession)
    except (OSError, ValueError):
        try:
            out.close()
        except OSError:
            pass
        return False
    # The child holds its own fd; closing ours pins nothing.
    try:
        out.close()
    except OSError:
        pass
    _DISK_TMP = tmp
    _DISK_DEADLINE = time.time() + wait
    return True


def _poll_disk_walk():
    """Non-blocking reap of the in-flight walk (S17r).

    Finished: parse the tmp output, write cache/disk.json in-process
    (tiny), unlink the tmp. Overdue: SIGKILL, record the error cache
    at once, and reap on a later poll (returncode set, no zombie).
    Never blocks the request; never raises. F2: the whole reap runs
    under _DISK_LOCK, so two concurrent requests cannot reap one
    finished walk twice — the second would see no tmp and overwrite
    the good cache with an error for 30 minutes. F3: du exits rc=1
    when any entry is unreadable but still prints a total; a
    parseable total is kept as a partial sum with an honest note,
    and only an unparseable output counts as failed."""
    global _DISK_PROC, _DISK_TMP, _DISK_LAST_RC
    proc = _DISK_PROC
    if proc is None:
        return
    with _DISK_LOCK:
        if _DISK_PROC is not proc:
            return
        if proc.poll() is None:
            if time.time() < _DISK_DEADLINE:
                return
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            gc.write_disk_cache(str(paths.home()), home_bytes=None,
                                error="walk timeout")
            return
        _DISK_PROC = None
        _DISK_LAST_RC = proc.returncode
        tmp, _DISK_TMP = _DISK_TMP, None
        size = None
        if tmp is not None and proc.returncode in (0, 1):
            try:
                size = int(tmp.read_text(
                    encoding="utf-8").strip().split()[0]) * 1024
            except (OSError, IndexError, ValueError):
                size = None
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        if size is not None and proc.returncode == 0:
            error = None
        elif size is not None:
            error = "일부 항목 읽기 실패(부분 합계)"
        else:
            error = f"du rc={proc.returncode}"
        gc.write_disk_cache(str(paths.home()), home_bytes=size,
                            error=error)


def _maybe_spawn_disk_walk() -> bool:
    """Single-flight spawn: at most one walk attempt per
    _DISK_RESPAWN_S. The attempt time is recorded up front, so a
    failing fork cannot fork-storm every request either."""
    global _DISK_SPAWNED_AT
    now = time.time()
    if now - _DISK_SPAWNED_AT < _DISK_RESPAWN_S:
        return False
    _DISK_SPAWNED_AT = now
    return _spawn_disk_walk()


def api_disk(_payload: dict) -> dict:
    """Dashboard disk meter (read-only, tiny, never blocking).

    D2: free_bytes comes from statvfs LIVE on every request (instant)
    — it never waits on the home walk. D1: stale_s is null while no
    cache exists (never now-minus-epoch). D3: the home walk is the
    Popen fork+exec above (never fork-without-exec); "계산 중" covers
    the home size only, and a failed walk reads "크기 계산 실패" with
    free still shown."""
    from .util import read_json
    now = time.time()
    try:
        st = os.statvfs(str(paths.home()))
        free = int(st.f_bavail * st.f_frsize)
    except OSError:
        free = None
    # Reap the in-flight walk first (non-blocking): a finished walk
    # lands in the cache before this same response reads it.
    try:
        _poll_disk_walk()
    except Exception:  # noqa: BLE001 - the meter never fails a request
        pass
    try:
        cached = read_json(_disk_cache_path(), default=None) or {}
    except BaseException:  # noqa: BLE001 - unreadable means recompute
        cached = {}
    if not isinstance(cached, dict):
        cached = {}
    at = cached.get("at_epoch")
    stale = None
    if isinstance(at, (int, float)) and at > 0:
        stale = int(now - at)
    home_bytes = cached.get("home_bytes")
    error = cached.get("error")
    computing = stale is None or stale > _DISK_TTL_S
    note = ""
    if home_bytes is None:
        if error and not computing:
            note = "크기 계산 실패"
        else:
            note = "계산 중"
            if computing:
                with _DISK_LOCK:
                    _maybe_spawn_disk_walk()
    elif computing:
        with _DISK_LOCK:
            _maybe_spawn_disk_walk()
    return {"free_bytes": free, "home_bytes": home_bytes,
            "at_epoch": at, "stale_s": stale,
            "computing": computing, "note": note}


def api_digest(payload: dict) -> dict:
    """Serve one stored in-app daily digest (read-only). `date` defaults
    to today (UTC). Missing days read as unknown, never a 500. NEVER
    touches ~/Desktop (those files belong to the owner's own cron).
    Deliberately ungated: stored bytes only, no model."""
    date_str = (payload.get("date") or "").strip()
    if not date_str:
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not batches._re_safe_date(date_str):
        raise ApiError(400, "date must be YYYY-MM-DD")
    data = batches.read_digest(date_str)
    if data is None:
        from .util import utcnow as _now
        return {"unknown": True,
                "reason": f"{date_str} 요약을 찾을 수 없습니다",
                "served_at": _now(), "date": date_str, "repos": []}
    return data



def api_integration_followup(payload: dict) -> dict:
    """Queue a source-bound correction on a prepared integration.

    Accepts only a registered operation id plus the operator request
    text; workspace and expected source derive from the record. The
    request is validated synchronously (unknown/drift/overlap refuse
    with 4xx before anything runs); the subscription model run executes
    as a cancellable job with the worker lease held throughout, so a
    second worker truthfully reports busy instead of overlapping.
    """
    _require_gate()
    op_id = (payload.get("op") or "").strip()
    request_text = payload.get("request") or ""
    if not op_id:
        raise ApiError(400, "op is required")
    owner = f"integration-followup:{op_id}"
    token = sched.acquire_worker_lease(owner)
    if token is None:
        try:
            busy_owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            busy_owner = "another worker"
        raise ApiError(409, f"worker busy ({busy_owner}); nothing started "
                            "and nothing queued — submit again when free")

    def _release():
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)

    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    # Every failure from here to the job handoff releases the lease:
    # only IntegrationError maps to 4xx, anything else still unlocks.
    try:
        _, fid = batches.request_followup(op_id, request_text)
    except batches.IntegrationError as exc:
        _release()
        raise ApiError(exc.http_status, str(exc)) from exc
    except Exception:
        _release()
        raise
    job = _job_new("followup", fid)
    if job is None:
        # The queued entry is rolled back, so "nothing queued" below is
        # literal; the lease releases even when the rollback itself
        # fails.
        try:
            batches.abandon_queued_followup(op_id, fid,
                                            "no dashboard job slot")
        except Exception:  # noqa: BLE001 - release must still run
            pass
        _release()
        raise ApiError(409, "another job is already running in this "
                            "dashboard process; nothing started and "
                            "nothing queued — submit again when free")
    ev = _JOB_CANCEL[job["id"]]

    def _run():
        try:
            rec = batches.execute_followup(op_id, fid, cancel_event=ev)
            return _followup_job_rc(rec, fid)
        except batches.IntegrationError:
            return 1
        finally:
            _release()

    _job_run(job, _run)
    # The worker thread now owns the lease lifecycle (_release in _run):
    # drop this thread's reentrant entry so a later same-thread acquire
    # cannot falsely bypass a live holder. The OS lock stays held.
    sched.disown_worker_lease(token)
    return {"job": job, "op": op_id, "followup": fid}


def api_integration_recovery(payload: dict) -> dict:
    """GET-bound cached read: blocked typed-issue conflict investigation.

    Pure cached read (no network, no subscription gate, no lease):
    the validated blocked-op summary, human cause with the actual
    conflicted paths vs original touched paths, fastcheck non-run
    truth, followup gating, and latest recovery-review state.
    Unknown/moved/unreadable/corrupt identities refuse with 4xx/5xx
    and preserve bytes; nothing is invented or rebound.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    op_id = str(payload.get("op") or "").strip()
    if not op_id:
        raise ApiError(400, "op is required")
    from . import issue_recovery as _ir
    try:
        return _ir.read_status(op_id)
    except _ir.IssueRecoveryError as exc:
        raise ApiError(exc.http_status or 500, str(exc)) from exc


def api_integration_recovery_review(payload: dict) -> dict:
    """POST /api/integration/recovery/review {op, confirm:true}.

    Start one app-owned READ-ONLY conflict-investigation review on a
    blocked typed-issue prepare op (owner/root confirm; the M2 driver
    never calls it -> 400). Reads pinned Git blobs and runs one
    bounded subscription model review with read-only flags; it writes
    only immutable input/report/attempt artifacts in its own
    namespace, never the parent op, run, queue, or target sources.
    409 when busy. Returns {started, job|reason}.

    Optional ``context`` carries ONE supplemental operator note for
    the CURRENT valid report's questions::

        {op, confirm: true,
         context: {text, question_refs?, input, report,
                   expected: {input_sha256, report_sha256}}}

    ``expected`` is REQUIRED for every contextual POST: it must echo
    the exact ``origin_shas`` the status response displayed when the
    note was composed. None, empty, partial, or malformed revisions
    refuse 400 with no writes and no model call; a well-formed but
    stale revision refuses 409. The context-free review call
    (``{op, confirm: true}`` with no ``context``) is unchanged.

    The text is operator-provided investigation context — never a
    code citation, never an approval or owner-policy decision, and
    any submitted author label is ignored (the stored actor is always
    "operator"). The context record is written only as part of this
    explicit confirmed call — never from GET or from saving alone —
    and the named origin triple must equal the current latest chain
    with a still-current live binding, otherwise 409 before any
    write. Identical bytes reuse the stored record and the normal
    dedupe path; a new review reuses/attaches exactly like the
    context-free flow.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    op_id = str(payload.get("op") or "").strip()
    if not op_id:
        raise ApiError(400, "op is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{op, confirm: true} to start a read-only "
                            "conflict-investigation review; nothing started")
    context = payload.get("context")
    if context is not None and not isinstance(context, dict):
        raise ApiError(400, "context must be an object")
    import re as _re

    from . import issue_recovery as _ir
    from . import m2
    # Global worker lease FIRST — nothing is written before this.
    # A busy worker refuses with 409 and no phantom record exists.
    owner = f"integration-recovery:{op_id}"
    token = sched.acquire_worker_lease(owner)
    if token is None:
        try:
            busy_owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            busy_owner = "another worker"
        raise ApiError(409, f"worker busy ({busy_owner}); nothing started "
                            "and nothing queued — submit again when free")

    def _release():
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)

    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    # Same-op nonblocking exclusion, held through request validation.
    # The worker thread re-acquires it around execute_review so it is
    # held through state, model run, and final source validation.
    context_out = None
    try:
        with m2.op_locked(op_id):
            context_id = None
            if context is not None:
                if not isinstance(context.get("text"), str):
                    raise ApiError(400, "context.text must be a string")
                # The displayed exact revision is required for every
                # NEW contextual POST: without both raw-byte hashes
                # the server cannot prove the operator answered the
                # questions they actually saw. Refused before any
                # write and before any model call.
                expected = context.get("expected")
                if not isinstance(expected, dict):
                    raise ApiError(400, "context.expected must be an "
                                        "object naming the displayed "
                                        "origin revision")
                exp_in = expected.get("input_sha256")
                exp_rep = expected.get("report_sha256")
                for label, value in (("input_sha256", exp_in),
                                     ("report_sha256", exp_rep)):
                    if not isinstance(value, str) or not _re.fullmatch(
                            r"[0-9a-f]{64}", value):
                        raise ApiError(400, "context.expected." + label +
                                            " must be a 64-hex SHA-256 "
                                            "of the displayed origin")
                rec = _ir.record_recovery_context(
                    op_id, context.get("text"),
                    question_refs=context.get("question_refs"),
                    origin_input=context.get("input"),
                    origin_report=context.get("report"),
                    expected_input_sha=exp_in,
                    expected_report_sha=exp_rep, by="owner")
                context_id = rec["context"]
                context_out = {"id": context_id,
                               "reused": bool(rec.get("reused"))}
            req = _ir.request_review(op_id, by="owner",
                                     context_id=context_id)
    except m2.OpBusy as exc:
        _release()
        raise ApiError(409, f"op busy ({exc}); nothing started — "
                            "submit again when free") from exc
    except _ir.IssueRecoveryError as exc:
        _release()
        raise ApiError(exc.http_status or 500, str(exc)) from exc
    except Exception:
        _release()
        raise
    if req.get("reused"):
        _release()
        attached = bool(req.get("attached"))
        if attached:
            reason = "review already running; attached, no duplicate"
        elif req.get("report") is not None:
            reason = "identical review already recorded; no new run"
        else:
            reason = "identical review state; no new run"
        resp = {"started": False, "reused": True, "attached": attached,
                "op": op_id, "rid": req["rid"], "reason": reason}
        if req.get("job_id"):
            resp["job_id"] = req["job_id"]
        if context_out is not None:
            resp["context"] = context_out
        return resp
    job = _job_new("recovery-review", req["rid"])
    if job is None:
        # No running artifact may survive a slot failure: the queued
        # attempt is truthfully marked startup-failed and preserved.
        try:
            _ir.mark_startup_failed(op_id, req["rid"],
                                    "no dashboard job slot")
        except Exception:  # noqa: BLE001 - release must still run
            pass
        _release()
        raise ApiError(409, "another job is already running in this "
                            "dashboard process; nothing started and "
                            "nothing queued — submit again when free")
    try:
        ev = _JOB_CANCEL[job["id"]]
    except KeyError:
        _job_startup_failed(job, "worker slot vanished before start")
        try:
            _ir.mark_startup_failed(op_id, req["rid"],
                                    "worker slot vanished before start")
        except Exception:  # noqa: BLE001 - release must still run
            pass
        _release()
        raise ApiError(500, "worker slot vanished before start")
    from .util import Cancelled as _Cancelled

    def _run():
        try:
            with m2.op_locked(op_id):
                rc = _ir.execute_review(op_id, req["rid"],
                                        job_id=job["id"],
                                        cancel_event=ev)
            # A 130 with the cancel event set is a genuine operator
            # cancel: surface it so the job reads cancelled, matching
            # the existing _job_run cancel/rc contract. A 130 with
            # no cancel event is just the worker's exit code.
            if rc == 130 and ev.is_set():
                raise _Cancelled("operator cancelled the review")
            return rc
        except m2.OpBusy:
            try:
                _ir.mark_startup_failed(op_id, req["rid"],
                                        "op lock lost before execution")
            except Exception:  # noqa: BLE001 - record best-effort
                pass
            return 1
        except _ir.IssueRecoveryError:
            return 1
        finally:
            _release()

    try:
        _job_run(job, _run)
    except Exception:
        _job_startup_failed(job, "worker thread failed to start")
        try:
            _ir.mark_startup_failed(op_id, req["rid"],
                                    "worker thread failed to start")
        except Exception:  # noqa: BLE001 - release must still run
            pass
        _release()
        raise ApiError(500, "worker thread failed to start")
    # The worker thread now owns the lease lifecycle (_release in _run):
    # drop this thread's reentrant entry so a later same-thread acquire
    # cannot falsely bypass a live holder. The OS lock stays held.
    sched.disown_worker_lease(token)
    started = {"started": True, "reused": False, "attached": False,
               "job": job, "op": op_id, "rid": req["rid"]}
    if context_out is not None:
        started["context"] = context_out
    return started


def api_integration_recovery_owner_decision(payload: dict) -> dict:
    """POST /api/integration/recovery/owner-decision.

    Record one immutable owner-decision set against the current
    review chain. Body: {op, confirm:true, input, report, decisions,
    expected:{input_sha256, report_sha256}}. The recorder is always
    Root: by "root" is hardcoded server-side and any client "by" is
    rejected as an unknown field. Lease-free pure record write under
    same-op exclusion; no model call. Every 4xx raises before any
    write; a stale or mismatched expected refuses 409 with no write.
    Returns {id, reused, digest}.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    op_id = str(payload.get("op") or "").strip()
    if not op_id:
        raise ApiError(400, "op is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{op, confirm: true, input, report, "
                            "decisions, expected} to record an "
                            "owner decision; nothing recorded")
    for key in ("input", "report"):
        if not isinstance(payload.get(key), str) \
                or not payload.get(key):
            raise ApiError(400, f"{key} is required and must name "
                                "the displayed origin record")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise ApiError(400, "decisions must be a list")
    expected = payload.get("expected")
    if not isinstance(expected, dict):
        raise ApiError(400, "expected must be an object naming the "
                            "displayed origin revision")
    import re as _re
    exp_in = expected.get("input_sha256")
    exp_rep = expected.get("report_sha256")
    for label, value in (("input_sha256", exp_in),
                         ("report_sha256", exp_rep)):
        if not isinstance(value, str) or not _re.fullmatch(
                r"[0-9a-f]{64}", value):
            raise ApiError(400, "expected." + label + " must be a "
                                "64-hex SHA-256 of the displayed origin")
    unknown = set(payload) - {"op", "confirm", "input", "report",
                              "decisions", "expected"}
    if unknown:
        raise ApiError(400, "unknown fields: "
                            + ", ".join(sorted(unknown)))
    from . import issue_recovery as _ir
    from . import m2
    try:
        with m2.op_locked(op_id):
            rec = _ir.record_owner_decision(
                op_id, decisions, payload["input"], payload["report"],
                exp_in, exp_rep)
    except m2.OpBusy as exc:
        raise ApiError(409, f"op busy ({exc}); nothing recorded — "
                            "submit again when free") from exc
    except _ir.IssueRecoveryError as exc:
        raise ApiError(exc.http_status or 500, str(exc)) from exc
    return {"id": rec["id"], "reused": bool(rec.get("reused")),
            "digest": rec["digest"]}


def api_derived_fix_start(payload: dict) -> dict:
    """POST /api/integration/derived-fix/start {op, confirm, hypothesis, context, retry_of?}.

    Start ONE app-owned private source-repair worker on a blocked
    typed-issue prepare op. The body carries exactly one operator
    hypothesis plus the displayed origin revision::

        {op, confirm: true,
         hypothesis: {id: "H-A"|"H-B", note: str},
         context: {input, report,
                   expected: {input_sha256, report_sha256}},
         retry_of?: "dfix-<32hex>"}

    No paths, commands, refs, or new SHAs are accepted from the
    browser: the server derives everything from pinned records.
    The worker may edit ONLY ``example/webpack.config.js`` and
    ``src/__tests__/webpack-web.test.ts`` inside a fresh isolated
    worktree; the prompt asks for source edits but is not an OS
    tool ban — confinement is the managed FS sandbox plus the
    post-run diff gate. Result is ``source_ready_unverified``
    (a validated proposal, never a verified fix); no build, no
    tests, no registration happen here. 409 when busy; identical
    origin+hypothesis reuses/attaches without duplicates, and a
    terminal-BAD same-key record restarts only with an explicit
    ``retry_of`` naming it (bounded, no auto-retry).
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    op_id = str(payload.get("op") or "").strip()
    if not op_id:
        raise ApiError(400, "op is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{op, confirm: true, hypothesis, context} "
                            "to start a private source repair; "
                            "nothing started")
    hypothesis = payload.get("hypothesis")
    if not isinstance(hypothesis, dict):
        raise ApiError(400, "hypothesis must be one object "
                            "{id: H-A|H-B, note}")
    context = payload.get("context")
    if not isinstance(context, dict):
        raise ApiError(400, "context must be an object naming the "
                            "displayed origin revision")
    expected = context.get("expected")
    if not isinstance(expected, dict):
        raise ApiError(400, "context.expected must be an object naming "
                            "the displayed origin revision")
    retry_of = payload.get("retry_of")
    if retry_of is not None and not isinstance(retry_of, str):
        raise ApiError(400, "retry_of must name a derived record")

    from . import derived_fix as _df
    from . import m2
    # Live table snapshot for the read-only attach proof: kind,
    # label, and status come from the REAL dashboard entries, never
    # from id membership or a self-claimed persisted link.
    live = {}
    for jid, job in list(_JOBS.items()):
        if isinstance(job, dict) and job.get("status") == "running":
            live[jid] = {"kind": job.get("kind"),
                         "label": job.get("label"),
                         "status": "running"}
    # Lock-free attach fast-path BEFORE any lease or heartbeat: a
    # duplicate click while our own job runs attaches without
    # acquiring the global lease (which the running job still owns),
    # so a live same-key duplicate never 409s as foreign-busy. No
    # lease is held on this return, so nothing is released. The
    # advisory peek is re-verified under the lease by create(); a
    # stale advisory attach (job died between peek and poll) renders
    # unknown on the next read and reconciles on explicit start.
    # Foreign-busy (no attachable same-key job) falls through to the
    # lease gate below and 409s there. Shape errors raise before any
    # lease exists.
    try:
        pre = _df.peek_derived_attach(
            op_id, str(context.get("input") or ""),
            str(context.get("report") or ""), expected, hypothesis,
            live)
    except _df.DerivedFixError as exc:
        raise ApiError(exc.http_status or 500,
                       str(exc.message)) from exc
    if pre is not None:
        job = pre.get("job") or {}
        return {"started": False, "reused": True, "attached": True,
                "op": op_id, "dfix": pre["dfix"],
                "reason": pre.get("reason") or "",
                "job": {"id": job.get("id"),
                        "kind": job.get("kind"),
                        "label": job.get("label")}}
    # Preflight authority gate for NEW spawn/mutation only: an
    # existing live worker still attaches/displays even if the
    # receipt later went stale (read-only attach above returns
    # first). Fresh authority is required before any lease,
    # record, job, or model step — no mutation, no new dispatch
    # without it, and never a duplicate worker to bypass this.
    # The refusal message carries the typed reason plus the next
    # action for the UI.
    try:
        preflight.check_target_authority(None)
    except preflight.PreflightError as exc:
        raise ApiError(exc.status, str(exc.message),
                       code=exc.code or "no-authority") from exc
    owner = f"derived-fix:{op_id}"
    token = sched.acquire_worker_lease(owner)
    if token is None:
        try:
            busy_owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            busy_owner = "another worker"
        raise ApiError(409, f"worker busy ({busy_owner}); nothing started "
                            "and nothing queued — submit again when free")

    def _release():
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)

    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        with m2.op_locked(op_id):
            out = _df.create_derived_fix(
                op_id, str(context.get("input") or ""),
                str(context.get("report") or ""), expected, hypothesis,
                live_jobs=live, retry_of=retry_of)
            dfix_id = out["dfix"]
            if out.get("reused"):
                _release()
                resp = {"started": False, "reused": True,
                        "attached": bool(out.get("attached")),
                        "op": op_id, "dfix": dfix_id,
                        "reason": out.get("reason") or ""}
                if out.get("job"):
                    resp["job"] = out["job"]
                return resp
            reconciled = bool(out.get("reconciled"))
            try:
                run = _df.start_derived_run(dfix_id)
            except _df.DerivedFixError as exc:
                try:
                    _df.refuse_derived(dfix_id, str(exc.message))
                except Exception:  # noqa: BLE001 - record best-effort
                    pass
                _release()
                raise ApiError(exc.http_status or 500,
                               str(exc.message)) from exc
            att = {"attempt_id": run["attempt_id"]}
    except m2.OpBusy as exc:
        _release()
        raise ApiError(409, f"op busy ({exc}); nothing started — "
                            "submit again when free") from exc
    except _df.DerivedFixError as exc:
        _release()
        raise ApiError(exc.http_status or 500,
                       str(exc.message)) from exc
    except Exception:
        _release()
        raise
    job = _job_new("derived-fix", att["attempt_id"])
    if job is None:
        try:
            _df.mark_derived_startup_failed(dfix_id, att["attempt_id"],
                                            "no dashboard job slot")
        except Exception:  # noqa: BLE001 - release must still run
            pass
        _release()
        raise ApiError(409, "another job is already running in this "
                            "dashboard process; nothing started and "
                            "nothing queued — submit again when free")
    # The job link is mandatory ownership, never best-effort: a
    # kind/attempt mismatch here means the "running" record could
    # neither cancel nor attach. ANY binding fault — typed refusal
    # or an unexpected write failure — terminalizes ONLY this new
    # job slot (never another job), closes the queued attempt as a
    # preserved terminal, releases the lease, and raises truthfully.
    try:
        _df.note_job(dfix_id, job)
    except Exception as exc:  # noqa: BLE001 - any binding fault
        _job_startup_failed(job, f"job link refused: {exc}")
        try:
            _df.mark_derived_startup_failed(dfix_id, att["attempt_id"],
                                            f"job link refused: {exc}")
        except Exception:  # noqa: BLE001 - release must still run
            pass
        _release()
        if isinstance(exc, _df.DerivedFixError):
            raise ApiError(exc.http_status or 500,
                           f"job link refused: {exc.message}") from exc
        raise ApiError(500, f"job link failed: {exc}") from exc
    try:
        ev = _JOB_CANCEL[job["id"]]
    except KeyError:
        _job_startup_failed(job, "worker slot vanished before start")
        try:
            _df.mark_derived_startup_failed(dfix_id, att["attempt_id"],
                                            "worker slot vanished")
        except Exception:  # noqa: BLE001 - release must still run
            pass
        _release()
        raise ApiError(500, "worker slot vanished before start")
    from .util import Cancelled as _Cancelled

    def _run():
        try:
            with m2.op_locked(op_id):
                rc = _df.run_repair_worker(dfix_id, att["attempt_id"],
                                           job_id=job["id"],
                                           cancel_event=ev)
            if rc == 130 and ev.is_set():
                raise _Cancelled("operator cancelled the repair worker")
            return rc
        except m2.OpBusy:
            return 1
        except _df.DerivedFixError:
            return 1
        finally:
            _release()

    try:
        _job_run(job, _run)
    except Exception:
        _job_startup_failed(job, "worker thread failed to start")
        try:
            _df.mark_derived_startup_failed(dfix_id, att["attempt_id"],
                                            "worker thread failed to start")
        except Exception:  # noqa: BLE001 - release must still run
            pass
        _release()
        raise ApiError(500, "worker thread failed to start")
    sched.disown_worker_lease(token)
    return {"started": True, "reused": False, "attached": False,
            "job": job, "op": op_id, "dfix": dfix_id,
            "attempt": att["attempt_id"],
            "reconciled": bool(reconciled)}


def api_derived_fix(payload: dict) -> dict:
    """GET /api/integration/derived-fix?op= — read-only status.

    Never writes, never mutates attempts, never persists orphan
    terminals: a running attempt whose owner job is not live renders
    ``unknown`` and stays that way until an explicit start call
    reconciles it under the lease.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    op_id = str(payload.get("op") or "").strip()
    if not op_id:
        raise ApiError(400, "op is required")
    from . import derived_fix as _df
    try:
        live = {jid for jid, job in list(_JOBS.items())
                if isinstance(job, dict)
                and job.get("status") == "running"}
        return _df.describe_derived(op_id, live)
    except _df.DerivedFixError as exc:
        raise ApiError(exc.http_status or 500,
                       str(exc.message)) from exc


def api_derived_fix_input(payload: dict) -> dict:
    """GET /api/integration/derived-fix/input?op=&dfix=&attempt=.

    Private read-only view of one immutable app-sent prompt. Never
    writes, never mutates attempts, never rebinds history. The
    dfix/attempt must belong to the given parent op; every lookup
    failure (bad id, foreign op, unknown attempt, missing or moved
    bytes) is a typed error, never an empty view.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    op_id = str(payload.get("op") or "").strip()
    dfix_id = str(payload.get("dfix") or "").strip()
    attempt_id = str(payload.get("attempt") or "").strip()
    if not op_id:
        raise ApiError(400, "op is required")
    if not dfix_id:
        raise ApiError(400, "dfix is required")
    if not attempt_id:
        raise ApiError(400, "attempt is required")
    from . import derived_fix as _df
    try:
        return _df.describe_full_input(op_id, dfix_id, attempt_id)
    except _df.DerivedFixError as exc:
        raise ApiError(exc.http_status or 500,
                       str(exc.message),
                       code=exc.code) from exc


def api_derived_fix_preflight(payload: dict) -> dict:
    """GET /api/integration/derived-fix/preflight — read-only status.

    Never writes, never creates directories or records: a missing
    layout reads as state "absent" with an actionable reason. Shows
    the newest verified receipt (or not-verified), per-channel
    required-vs-observed state, and bounded history.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    try:
        return preflight.describe_preflight()
    except preflight.PreflightError as exc:
        raise ApiError(exc.status, str(exc.message),
                       code=exc.code or "") from exc


def api_derived_fix_preflight_create(payload: dict) -> dict:
    """POST /api/.../preflight/create {confirm: true} — request a check.

    Snapshots the live dedicated-profile identity into an immutable
    input plus a created status. A live receipt for the same world
    attaches ({reused True}) instead of duplicating; a terminal
    verified one is reused. No model contact, no target mutation.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{confirm: true} to request an isolation "
                            "check; nothing created")
    try:
        current = preflight.current_identity()
        # Dedupe on the FULL managed identity (binary SHA + resolved
        # path, full config, full source incl. file SHAs, child-env
        # shape, auth facts): a status-only key would reuse or
        # relabel historical proof after a config/auth/source change.
        key = muse.source_repair_fingerprint(current)
        return preflight.create_preflight(None, key)
    except preflight.PreflightError as exc:
        raise ApiError(exc.status, str(exc.message),
                       code=exc.code or "") from exc


def api_derived_fix_preflight_run(payload: dict) -> dict:
    """POST /api/.../preflight/run {pfid, confirm: true} — run the probe.

    Quota-spending: requires the subscriber gate and the global
    worker lease (409 while busy, nothing duplicated). Synchronous
    and bounded; the immutable outcome records verified, unknown, or
    a typed refusal (auth/quota/offline/format/channel) — never a
    phantom success. Runs under the later execution gate only.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    _require_gate()
    pfid = str(payload.get("pfid") or "").strip()
    if not pfid:
        raise ApiError(400, "pfid is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{pfid, confirm: true} to run the probe; "
                            "nothing spawned")
    owner = f"preflight:{pfid}"
    token = sched.acquire_worker_lease(owner)
    if token is None:
        try:
            busy_owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            busy_owner = "another worker"
        raise ApiError(409, f"worker busy ({busy_owner}); probe not "
                            "started and nothing queued — submit again "
                            "when free")

    def _release():
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)

    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        return preflight.run_preflight(None, pfid)
    except preflight.PreflightError as exc:
        raise ApiError(exc.status, str(exc.message),
                       code=exc.code or "") from exc
    finally:
        _release()


def api_derived_fix_preflight_cancel(payload: dict) -> dict:
    """POST /api/.../preflight/cancel {pfid, confirm: true}.

    Terminalizes a created/running check as cancelled. Terminal
    receipts refuse (409); nothing is deleted. A probe already
    holding the lease releases it when it finishes; the recorded
    outcome (if any) stays as evidence while the status honors the
    cancel.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    pfid = str(payload.get("pfid") or "").strip()
    if not pfid:
        raise ApiError(400, "pfid is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required")
    try:
        return preflight.cancel_preflight(None, pfid)
    except preflight.PreflightError as exc:
        raise ApiError(exc.status, str(exc.message),
                       code=exc.code or "") from exc


def api_derived_fix_cancel(payload: dict) -> dict:
    """POST /api/integration/derived-fix/cancel {dfix, confirm: true}.

    Signal the running repair worker to stop via the existing job
    cancel event. The event fires ONLY for the record's own live
    derived-fix job: the stored job id must name a running
    ``derived-fix`` job whose label is the record's current attempt,
    under the record's own lease owner. Anything else — foreign
    kind/label, stale link, no live attempt, missing event — refuses
    or reports no-op without signaling another task. Idempotent and
    side-effect free beyond the event: no record is rewritten here;
    the worker's own finally records the terminal state.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    dfix_id = str(payload.get("dfix") or "").strip()
    if not dfix_id:
        raise ApiError(400, "dfix is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required")
    from . import derived_fix as _df
    try:
        rec = _df._read_record(dfix_id)
    except _df.DerivedFixError as exc:
        raise ApiError(exc.http_status or 500,
                       str(exc.message)) from exc
    if rec.get("lease_owner") != f"derived-fix:{rec.get('derived_from')}":
        raise ApiError(409, "foreign derived record; refusing to signal")
    job = rec.get("job") or {}
    jid = job.get("id") or ""
    latest = rec.get("latest_attempt") or {}
    live_status = latest.get("status")
    if not jid or live_status not in ("queued", "started",
                                      "source-running"):
        return {"cancelled": False, "dfix": dfix_id,
                "reason": "no live worker to signal"}
    # The stored link itself must name the live attempt: a stale
    # link from an older attempt never signals, even before the
    # live table is consulted.
    if job.get("kind") != "derived-fix" \
            or job.get("label") != latest.get("id"):
        raise ApiError(409, "stored job link does not name the live "
                            "attempt; refusing to signal")
    entry = _JOBS.get(jid)
    if not isinstance(entry, dict) or entry.get("kind") != "derived-fix" \
            or entry.get("status") != "running" \
            or entry.get("label") != latest.get("id"):
        raise ApiError(409, "stored job link is not the live owned "
                            "worker; refusing to signal")
    ev = _JOB_CANCEL.get(jid)
    if ev is None:
        return {"cancelled": False, "dfix": dfix_id,
                "reason": "no live worker to signal"}
    ev.set()
    return {"cancelled": True, "dfix": dfix_id}


def _followup_job_rc(rec: dict, fid: str) -> int:
    """Map the recorded followup outcome to a job exit code."""
    for entry in (rec.get("followups") or []):
        if isinstance(entry, dict) and entry.get("fid") == fid:
            return 0 if entry.get("status") == "done" else 1
    return 1


def api_integration_log_file(op_id: str, fid: str):
    """Registered raw model log file for one followup, or None.

    The fid must be recorded on the operation and match the pinned
    shape, so no arbitrary path can be addressed.
    """
    import re as _re
    if not op_id or not _re.fullmatch(r"fu-[0-9a-f]{8}", fid or ""):
        return None
    try:
        opf = batches.op_file(op_id)
        rec = batches.read_json(opf, default=None)
    except (batches.IntegrationError, OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not any(
            isinstance(e, dict) and e.get("fid") == fid
            for e in (rec.get("followups") or [])):
        return None
    dest = batches.integration_ops_dir() / f"{op_id}-{fid}.log.txt"
    try:
        resolved = dest.resolve()
        resolved.relative_to(batches.integration_ops_dir().resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def api_batch_import(payload: dict) -> dict:
    """Operator-trusted batch manifest import (local dashboard only).

    Thin wrapper: the entire contract lives in batches.import_batch
    (queue binding, branch ownership, snapshot match, fresh review
    proof, content-addressed evidence, size budgets). Nothing is
    weakened or skipped here; a refusal surfaces the validator's own
    message. Evidence `origin` paths resolve operator-trusted local
    files, exactly as the CLI path does.
    """
    _require_gate()
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise ApiError(400, "manifest must be a JSON object")
    try:
        batch = batches.import_batch(manifest, allowed_roots=None)
    except batches.BatchError as exc:
        raise ApiError(409, str(exc)) from exc
    return {"id": batch.get("id"), "repo": batch.get("repo"),
            "purpose": batch.get("purpose") or "",
            "primaries": len(batch.get("primaries") or []),
            "inherited": len(batch.get("inherited") or []),
            "integrations": len(batch.get("integrations") or [])}


def api_batch_attest(payload: dict) -> dict:
    """Human attestation of one batch gate (explicit only).

    Payload: {batch, gate, attestor, note, mode, receipt_sha?,
    log_text?, na_reason?, confirm:true}. Without confirm:true the
    call refuses with 400 and writes nothing. The contract lives in
    batches.attest_gate (human attestor required, evidence
    re-verified, N/A needs a reason); a refusal surfaces its
    message. One call attests one gate; the review screen posts one
    call per checked gate.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{batch, gate, attestor, note, mode, "
                            "confirm: true} to attest; nothing written")
    try:
        out = batches.attest_gate(
            str(payload.get("batch") or ""),
            str(payload.get("gate") or ""),
            payload.get("attestor"), payload.get("note"),
            str(payload.get("mode") or ""),
            receipt_sha=str(payload.get("receipt_sha") or ""),
            log_text=str(payload.get("log_text") or ""),
            na_reason=str(payload.get("na_reason") or ""),
            refresh=False)
    except batches.BatchError as exc:
        raise ApiError(409, str(exc)) from exc
    # Same freshness as hold: envelope + glance move together.
    refresh_all_soon("attest")
    return out


def api_batch_qa_pack(payload: dict) -> dict:
    """POST /api/batch/qa-pack {batch, by, confirm:true}.

    M2.18: build one human QA evidence pack for a batch
    (diffcheck/peer/source/consumer facts + suggested notes).
    by is owner|root only (a worker or the M2 driver never
    builds one) -> 400; missing confirm -> 400, nothing written;
    unknown batch -> 409. Under the worker lease like the other
    build paths: 409 busy starts nothing and writes nothing.
    Nothing attests — attestation stays one human call per gate.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    batch_id = str(payload.get("batch") or "").strip()
    by = str(payload.get("by") or "").strip()
    if not batch_id:
        raise ApiError(400, "batch is required")
    if by not in ("owner", "root"):
        raise ApiError(400, "by must be owner or root (a worker "
                            "and the M2 driver never build a pack)")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{batch, by, confirm: true} to build "
                            "the QA evidence pack; nothing written")
    token = sched.acquire_worker_lease(
        f"integration-qa-pack:{batch_id}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        return batches.build_qa_pack(batch_id, by=by)
    except batches.BatchError as exc:
        raise ApiError(409, str(exc)) from exc
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_batch_qa_pack_get(payload: dict) -> dict:
    """GET /api/batch/qa-pack?batch= — the newest pack, if any.

    Deliberately ungated: stored pack bytes only, no model."""
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    batch_id = str(payload.get("batch") or "").strip()
    if not batch_id:
        raise ApiError(400, "batch is required")
    try:
        pack = batches.newest_qa_pack(batch_id)
    except batches.BatchError as exc:
        raise ApiError(400, str(exc)) from exc
    if not isinstance(pack, dict) or not pack:
        raise ApiError(404, "no QA pack for this batch yet")
    return pack


def _discussions_slug(repo: str) -> str:
    """GitHub slug for a registered repo (404 on unknown)."""
    profile = _load_profile(repo)
    slug = (profile.get("github") or "").strip()
    if not slug:
        raise ApiError(400, f"repo '{repo}' has no github slug")
    return slug


def _discussions_root(repo: str):
    """Linked checkout dir for a repo, or None (server-side only).

    The request never carries paths: evidence and source re-checks
    resolve here, so a forged repo_root cannot escape the checkout.
    """
    try:
        from . import pcconfig as _pc
        from pathlib import Path as _Path
        co = (_pc.load().get("checkouts", {}) or {}).get(repo)
        if co and _Path(co).is_dir():
            return co
    except (Exception, SystemExit):
        pass
    return None


def _discussions_model_gate() -> dict:
    """Subscription-model availability for the review action (info only).

    Cached reads never require it; the review POST enforces it.
    """
    try:
        err = chat.subscription_error()
    except Exception:  # noqa: BLE001 - gate failure reads as unavailable
        err = "subscription check failed"
    return {"ok": not err, "reason": err}


def api_discussions(payload: dict) -> dict:
    """GET /api/discussions?repo= — list snapshot + census, stored only.

    Pure cached read (no subscription gate: stored reports stay
    readable when the model is unavailable): list items, poll status,
    label census and chip catalog come from the Discussions namespace
    files. Never touches the network — the sched scan (or manual
    refresh) owns fetching. Missing files read as never-checked,
    corrupt files as an explicit error state, never healthy zeros.
    The source-evidence re-check uses the same server-side checkout
    root as the detail view, so list and detail never disagree.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    from . import discussions as _disc
    slug = _discussions_slug(repo)
    root = _discussions_root(repo)
    # One authoritative status read drives both the status payload
    # and item visibility: an invalid/corrupt record yields an error
    # state with no items (never a healthy zero), and the bytes stay
    # on disk until a successful refresh replaces them (nothing here
    # moves or deletes files).
    status = _disc.snapshot_status(slug)
    items: list = []
    # Disabled hides rows exactly like an unreadable record: the
    # capability line (state "disabled") carries the meaning, not rows.
    if status.get("state") not in ("never", "error", "disabled"):
        try:
            snap = _disc._read_snapshot(slug)
        except (Exception, SystemExit):
            snap = {}
        if (isinstance(snap, dict)
                and _disc._snapshot_problems(snap) is None):
            items = list(snap.get("items") or [])
    enriched = []
    for item in items:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        try:
            outcome = _disc.review_outcome(slug, number, root)
        except (Exception, SystemExit):
            outcome = {"status": "error",
                       "error": "review outcome unreadable"}
        row = dict(item)
        job = outcome.get("job") or {}
        row["review"] = {
            "status": outcome.get("status"),
            "stale": bool(outcome.get("stale")),
            "partial": bool(outcome.get("partial")),
            "detail_complete": outcome.get("detail_complete", True),
            "labels": outcome.get("labels") or [],
            "label_facets": outcome.get("label_facets") or {},
            "source_unavailable": bool(outcome.get("source_unavailable")),
            "job_status": job.get("status") or "idle",
            "job_error": job.get("error"),
        }
        enriched.append(row)
    return {"repo": repo, "slug": slug,
            "items": enriched,
            "status": status,
            "labels": _disc.label_census(slug),
            "catalog": _disc.label_catalog(),
            "model": _discussions_model_gate()}


def api_discussions_node(payload: dict) -> dict:
    """GET /api/discussions/node?repo=&number= — stored detail + review.

    Cached read with no network and no subscription gate (stored
    detail stays readable when the model is unavailable). repo_root
    for the source-evidence re-check resolves server-side from the
    linked checkout (never from the request); unlinked checkouts
    simply skip that check.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    from . import discussions as _disc
    slug = _discussions_slug(repo)
    try:
        number = _disc._safe_number(payload.get("number"))
    except _disc.DiscussionError as exc:
        raise ApiError(400, str(exc)) from exc
    nstatus, node = _disc._read_json_safe(_disc.node_path(slug, number))
    if nstatus != "ok":
        raise ApiError(404, "no stored discussion detail yet; "
                            "fetch it from the list first")
    root = _discussions_root(repo)
    try:
        outcome = _disc.review_outcome(slug, number, root)
    except (Exception, SystemExit):
        outcome = {"status": "error",
                   "error": "review outcome unreadable"}
    return {"repo": repo, "slug": slug, "node": node,
            "review": outcome,
            "model": _discussions_model_gate()}


def api_discussions_detail(payload: dict) -> dict:
    """POST /api/discussions/detail {repo, number, confirm:true}.

    Model-free selected-detail fetch: pulls one discussion node
    (body/comments/labels) into the cache under the global worker
    lease (409 when busy, nothing started). No subscription gate —
    GitHub auth applies, not Muse billing. Missing confirm refuses
    with 400. A title click calls this first, then reads the cached
    GET — full body before any model quota is spent.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{repo, number, confirm: true} to fetch "
                            "one discussion detail; nothing fetched "
                            "otherwise")
    from . import discussions as _disc
    slug = _discussions_slug(repo)
    try:
        number = _disc._safe_number(payload.get("number"))
    except _disc.DiscussionError as exc:
        raise ApiError(400, str(exc)) from exc
    token = sched.acquire_worker_lease(f"discussions-detail:{slug}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        try:
            node = _disc.fetch_node(slug, number)
        except _disc.DiscussionError as exc:
            raise ApiError(502, str(exc)) from exc
        return {"repo": repo, "slug": slug, "number": number,
                "node": node,
                "review": _disc.review_outcome(slug, number)}
    finally:
        sched.release_worker_lease(token)


_SKIP_DIR_NAMES = frozenset({
    ".git", "node_modules", "Pods", "build", "DerivedData",
    ".gradle", "__pycache__", "dist", "out",
})
# Case-folded view of the same policy: typed variants (pods/PODS) and
# canonical aliases on case-insensitive checkouts must not bypass it.
_SKIP_DIR_NAMES_FOLDED = frozenset(s.casefold()
                                  for s in _SKIP_DIR_NAMES)


def api_discussions_attempt(payload: dict) -> dict:
    """GET /api/discussions/attempt?repo=&number=&artifact= — one attempt.

    Cached read of a single immutable per-review artifact (exact model
    input, sent source excerpts, raw stream, parsed report, receipt).
    Lets the in-app reviewer inspect what the model actually saw,
    including older attempts named by the review history. No network,
    no model, no subscription gate (stored reports stay readable when
    the model is unavailable). The artifact name is strictly validated
    (review-<number>-<rid>[-N].json, matching the requested number) —
    no traversal, no arbitrary file reads. Missing file reads as 404.
    """
    import re as _re
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    from . import discussions as _disc
    slug = _discussions_slug(repo)
    try:
        number = _disc._safe_number(payload.get("number"))
    except _disc.DiscussionError as exc:
        raise ApiError(400, str(exc)) from exc
    name = str(payload.get("artifact") or "")
    if not _re.fullmatch(r"review-\d+-[0-9A-Za-z-]+(-\d+)?\.json",
                          name):
        raise ApiError(400, "unknown attempt artifact name")
    if not name.startswith(f"review-{number}-"):
        raise ApiError(400, "attempt artifact does not belong to "
                            "this discussion")
    # The name cannot contain a slash (strict pattern above), so this
    # stays inside the discussion namespace directory.
    path = _disc._slug_dir(slug) / name
    try:
        status, data = _disc._read_json_safe(path)
    except (Exception, SystemExit):
        raise ApiError(404, "attempt artifact unreadable") from None
    if status != "ok" or not isinstance(data, dict):
        raise ApiError(404, "attempt artifact not found")
    return {"repo": repo, "slug": slug, "number": number,
            "artifact": name, "attempt": data}


def api_discussions_sources(payload: dict) -> dict:
    """GET /api/discussions/sources?repo=&dir= — bounded file listing.

    Lets the UI offer repo-relative evidence files for a review:
    plain names under one checkout subdirectory (default root), no
    dotfiles, no dependency/build trees, directories and files
    separated, capped entries. dir must stay inside the checkout —
    anything else is a 400. Cached metadata only, no model, no
    subscription gate.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    from . import discussions as _disc
    slug = _discussions_slug(repo)
    root = _discussions_root(repo)
    if not root:
        raise ApiError(404, "no linked checkout for this repo; "
                            "link one before picking evidence files")
    from pathlib import Path as _Path
    rel = str(payload.get("dir") or "")
    if _disc._safe_rel(rel or ".") is None and rel not in ("", "."):
        raise ApiError(400, "dir must be a repo-relative path")
    # Child-entry filtering alone does not cover an explicitly requested
    # dir: reject hidden/dependency/build components in the raw request
    # (a trailing slash reads the same folder) before listing.
    norm = rel.rstrip("/") if rel not in ("", ".") else rel
    if norm not in ("", "."):
        for _part in norm.split("/"):
            if (not _part or _part.startswith(".")
                    or _part.casefold() in _SKIP_DIR_NAMES_FOLDED):
                raise ApiError(400, "dir must be a repo-relative path")
    rel = norm
    base = _Path(root).resolve()
    try:
        target = (base / rel).resolve() if rel not in ("", ".") else base
    except (OSError, RuntimeError, ValueError):
        # Unresolvable/looping input is a structured 400, never an
        # uncaught resolve exception (resolve differs across 3.9/3.14).
        raise ApiError(400, "dir cannot be resolved in the checkout") \
            from None
    try:
        target.relative_to(base)
    except ValueError:
        raise ApiError(400, "dir escapes the checkout") from None
    try:
        # G9: the checkout may sit on a wedged volume — bound the stat.
        # ApiError (incl. the 502 timeout) propagates fail-closed.
        _is_dir = checkout_io(target.is_dir, path=str(target))
    except (OSError, RuntimeError, ValueError):
        raise ApiError(400, "dir cannot be resolved in the checkout") \
            from None
    if not _is_dir:
        # A looping or otherwise unresolvable symlink stays a link
        # after resolution: invalid input (400). A dangling alias
        # resolves to a missing target, and a regular file or truly
        # missing path was never a link: those keep the old 404.
        if os.path.islink(target):
            raise ApiError(400, "dir cannot be resolved in the checkout")
        raise ApiError(404, "no such directory in the checkout")
    # Symlink aliases must not bypass the exclusion: re-check the
    # canonical in-repo components after resolution, before listing.
    for _part in target.relative_to(base).parts:
        if (_part.startswith(".")
                or _part.casefold() in _SKIP_DIR_NAMES_FOLDED):
            raise ApiError(400, "dir must be a repo-relative path")
    dirs: list[str] = []
    files: list[dict] = []
    try:
        # G9: bound like the stat above; a timeout fails closed as 502.
        entries = checkout_io(
            lambda: sorted(target.iterdir(), key=lambda p: p.name),
            path=str(target))
    except OSError as exc:
        raise ApiError(502, f"cannot list checkout dir: {exc}") from exc
    for entry in entries[:400]:
        name = entry.name
        if (name.startswith(".")
                or name.casefold() in _SKIP_DIR_NAMES_FOLDED):
            continue
        if entry.is_dir():
            dirs.append(name)
        elif entry.is_file():
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            files.append({"name": name, "size": size,
                          "too_large": size > _disc._SOURCE_BYTES_CAP})
        if len(dirs) + len(files) >= 200:
            break
    prefix = "" if rel in ("", ".") else rel.rstrip("/") + "/"
    return {"repo": repo, "slug": slug, "dir": prefix,
            "dirs": dirs, "files": files,
            "truncated": len(dirs) + len(files) >= 200}


def api_discussions_refresh(payload: dict) -> dict:
    """POST /api/discussions/refresh {repo, by, confirm:true}.

    Manual model-free refresh: list scan + label census under the
    global worker lease (409 when busy, nothing started). by is
    owner|root only; missing confirm refuses with 400. Failures keep
    prior data and surface as degraded source state, never zeros.
    No subscription gate — GitHub auth applies, not Muse billing.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    by = str(payload.get("by") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    if by not in ("owner", "root"):
        raise ApiError(400, "by must be owner or root")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{repo, by, confirm: true} to refresh "
                            "Discussions from GitHub; nothing written "
                            "otherwise")
    from . import discussions as _disc
    slug = _discussions_slug(repo)
    token = sched.acquire_worker_lease(f"discussions-refresh:{slug}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        sched._start_heartbeat(token)
    except Exception:  # noqa: BLE001 - heartbeat is display-only
        pass
    try:
        try:
            result = _disc.scan_source(slug)
        except _disc.DiscussionError as exc:
            if str(exc) != "disabled":
                raise ApiError(
                    409 if exc.http_status == 409 else 502,
                    str(exc)) from exc
            # Disabled is a normal capability state: report it cleanly
            # with preserved items (200), never a 502. The scheduler
            # keeps its disabled signal from the scan exception itself.
            snap = _disc._read_snapshot(slug) or {}
            try:
                census = _disc.fetch_labels(slug)
            except _disc.DiscussionError as cexc:
                census = {"labels": [], "labels_complete": False,
                          "pages": 0, "error": str(cexc)}
            result = {"items": list(snap.get("items") or []),
                      "status": _disc.snapshot_status(slug),
                      "labels": census}
        else:
            try:
                census = _disc.fetch_labels(slug)
            except _disc.DiscussionError as exc:
                census = {"labels": [], "labels_complete": False,
                          "pages": 0, "error": str(exc)}
            result["labels"] = census
        return result
    finally:
        try:
            sched._stop_heartbeat(token)
        except Exception:  # noqa: BLE001 - release must still run
            pass
        sched.release_worker_lease(token)


def api_discussions_review(payload: dict) -> dict:
    """POST /api/discussions/review {repo, number, by, confirm:true}.

    Start one app-owned async review (owner/root confirm; the M2
    driver and workers never call it — 400). The model runs on a
    worker thread under the global lease with --disable-write,
    --disable-shell, a bounded step cap and no API key; 409 when
    busy. Evidence files come from an optional `evidence` list of
    repo-relative paths, each re-validated server-side against the
    linked checkout (must resolve inside it, be a file, fit the byte
    cap) — the request never carries a repo_root. Returns
    {started, job|reason}.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    by = str(payload.get("by") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    if by not in ("owner", "root"):
        raise ApiError(400, "by must be owner or root (a worker "
                            "and the M2 driver never start a review)")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{repo, number, by, confirm: true} to "
                            "start an app-owned review; nothing started "
                            "otherwise")
    from . import discussions as _disc
    slug = _discussions_slug(repo)
    try:
        number = _disc._safe_number(payload.get("number"))
    except _disc.DiscussionError as exc:
        raise ApiError(400, str(exc)) from exc
    root = _discussions_root(repo)
    evidence = payload.get("evidence") or []
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list):
        raise ApiError(400, "evidence must be a list of repo-relative "
                            "paths")
    if len(evidence) > _disc._SOURCE_FILE_CAP:
        raise ApiError(400, f"too many evidence files "
                            f"({len(evidence)} > "
                            f"{_disc._SOURCE_FILE_CAP}); pick at most "
                            f"{_disc._SOURCE_FILE_CAP} — nothing started")
    picked: list[str] = []
    for rel in evidence:
        if not isinstance(rel, str) or _disc._safe_rel(rel) is None:
            raise ApiError(400, f"rejected evidence path: {rel!r}"[:160])
        if root is None:
            raise ApiError(409, "no linked checkout: evidence files "
                                "cannot be gathered")
        full = _disc._resolve_under(root, rel)
        if full is None or not full.is_file():
            raise ApiError(400, f"evidence file not in checkout: {rel}")
        try:
            if full.stat().st_size > _disc._SOURCE_BYTES_CAP:
                raise ApiError(400, f"evidence file too large: {rel}")
        except OSError as exc:
            raise ApiError(400,
                           f"evidence file unreadable: {rel}") from exc
        if rel not in picked:
            picked.append(rel)
    try:
        return _disc.start_review(slug, number, by=by, repo_root=root,
                                  evidence_paths=picked)
    except _disc.DiscussionError as exc:
        raise ApiError(exc.http_status or 500, str(exc)) from exc


def api_prs(payload: dict) -> dict:
    """GET /api/prs?repo= — stored PR inventory + review state, cached only.

    Rows come from the scan's stored last_prs (number/title/author/bot/
    head/closes/counts); each row is enriched with the stored PR review
    outcome without touching the network. Missing state reads as never
    checked; corrupt records read as explicit error, never healthy.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    from . import pr_detail as _prd
    from . import pr_reviews as _prr
    slug = _discussions_slug(repo)
    scan = {"state": "unknown", "error": None, "last_scan": None,
            "prs_error": None}
    try:
        from . import sched as _sched
        sched_state = _sched.load(repo)
        stored = [p for p in (sched_state.get("last_prs") or [])
                  if isinstance(p, dict)]
        scan = {"state": "ready" if stored else "empty",
                "error": None,
                "last_scan": sched_state.get("last_scan"),
                "prs_error": sched_state.get("prs_error")}
        if sched_state.get("prs_error"):
            scan["state"] = "partial"
    except (Exception, SystemExit) as exc:
        stored = []
        scan = {"state": "error",
                "error": f"sched state unreadable: {exc}"[:200],
                "last_scan": None, "prs_error": None}
    enriched = []
    for row in stored:
        try:
            number = _prd.safe_pr_number(row.get("number"))
        except _prd.PrError:
            continue
        try:
            # List polls never read immutable attempt files: status,
            # staleness, and job only. The detail route resolves the
            # actual attempt evidence lazily on selection.
            outcome = _prr.review_outcome(slug, number,
                                          resolve_files=False)
        except (Exception, SystemExit):
            outcome = {"status": "error",
                       "error": "review outcome unreadable"}
        enriched.append({**row, "review": {
            "status": outcome.get("status"),
            "stale": bool(outcome.get("stale")),
            "partial": bool(outcome.get("partial")),
            "job_status": (outcome.get("job") or {}).get("status")
            or "idle",
            "job_error": (outcome.get("job") or {}).get("error"),
            "content_fp": outcome.get("content_fp"),
        }})
    return {"repo": repo, "slug": slug, "items": enriched, "scan": scan,
            "model": _discussions_model_gate()}


def api_prs_detail(payload: dict) -> dict:
    """GET /api/prs/detail?repo=&number= — stored PR snapshot + review.

    Cached read with no network and no subscription gate (stored detail
    stays readable when the model is unavailable).
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    from . import pr_detail as _prd
    from . import pr_reviews as _prr
    slug = _discussions_slug(repo)
    try:
        number = _prd.safe_pr_number(payload.get("number"))
    except _prd.PrError as exc:
        raise ApiError(400, str(exc)) from exc
    status, snapshot = _prd._read_json_safe(_prd.pr_path(slug, number))
    if status == "missing":
        raise ApiError(404, "no stored PR snapshot yet; fetch it first")
    if status != "ok" or not isinstance((snapshot or {}).get("detail"),
                                        dict):
        raise ApiError(502, "stored PR snapshot unreadable (kept in place)")
    try:
        outcome = _prr.review_outcome(slug, number)
    except (Exception, SystemExit):
        outcome = {"status": "error",
                   "error": "review outcome unreadable"}
    linked_runs = {}
    try:
        from . import store as _store
        detail = snapshot.get("detail") or {}
        for issue_no in (detail.get("closes") or [])[:10]:
            try:
                run_state = _store.latest_run(repo, str(issue_no))
            except (Exception, SystemExit):
                run_state = None
            if not isinstance(run_state, dict):
                linked_runs[str(issue_no)] = None
                continue
            files = run_state.get("files") or []
            linked_runs[str(issue_no)] = {
                "run": run_state.get("id") or run_state.get("run_id"),
                "issue": str(issue_no),
                "created_at": run_state.get("created_at"),
                "branch": run_state.get("branch"),
                "head": (run_state.get("source") or {}).get("head")
                if isinstance(run_state.get("source"), dict)
                else run_state.get("head"),
                "fingerprint": (run_state.get("source") or {}).get(
                    "fingerprint")
                if isinstance(run_state.get("source"), dict)
                else run_state.get("fingerprint"),
                "files": [str(f)[:260] for f in files[:50]]
                if isinstance(files, list) else [],
                "files_note": ("showing 50 of %d" % len(files))
                if isinstance(files, list) and len(files) > 50 else None,
            }
    except (Exception, SystemExit) as exc:
        linked_runs = {"_error": f"run lookup failed: {exc}"[:200]}
    return {"repo": repo, "slug": slug, "snapshot": snapshot,
            "review": outcome, "linked_runs": linked_runs,
            "model": _discussions_model_gate()}


def api_prs_attempt(payload: dict) -> dict:
    """GET /api/prs/attempt?repo=&number=&artifact= — one stored attempt."""
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    from . import pr_detail as _prd
    slug = _discussions_slug(repo)
    try:
        number = _prd.safe_pr_number(payload.get("number"))
        rid = str(payload.get("artifact") or "")
        path = _prd.attempt_path(slug, number, rid)
    except _prd.PrError as exc:
        raise ApiError(400, str(exc)) from exc
    status, data = _prd._read_json_safe(path)
    if status == "missing":
        raise ApiError(404, "no such PR attempt")
    if status != "ok":
        raise ApiError(502, "stored PR attempt unreadable (kept in place)")
    # Bind the record to the requested identity: a corrupt, foreign,
    # wrong-number, or wrong-rid record must be an explicit error, never
    # a healthy attempt from another PR or a fabricated history entry.
    # Missing or unknown outcome shapes are not successes either.
    if not isinstance(data, dict) or data.get("number") != number \
            or data.get("repo") != slug or data.get("rid") != rid:
        raise ApiError(502, "stored PR attempt identity mismatch "
                            "(kept in place)")
    # The same shared structural contract the backend history
    # validator enforces: list/detail/API can never disagree on what
    # counts as healthy review evidence.
    ok, result = _prd.attempt_review_evidence_ok(data)
    if not ok:
        raise ApiError(502, f"stored PR attempt {result} "
                            "(kept in place)")
    return {"repo": repo, "slug": slug, "attempt": result}


def api_prs_fetch(payload: dict) -> dict:
    """POST /api/prs/fetch {repo, number, confirm:true}.

    Model-free selected-PR fetch: pulls one PR snapshot (view + diff +
    checks + threads) into the cache under the global worker lease (409
    when busy, nothing started). No subscription gate — GitHub auth
    applies, not Muse billing. Missing confirm refuses with 400. A
    coherence failure (upstream moved mid-collection) stores nothing
    and reports explicitly; prior bytes stay in place.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{repo, number, confirm: true} to fetch "
                            "one PR snapshot; nothing fetched otherwise")
    from . import pr_detail as _prd
    from . import pr_reviews as _prr
    slug = _discussions_slug(repo)
    try:
        number = _prd.safe_pr_number(payload.get("number"))
    except _prd.PrError as exc:
        raise ApiError(400, str(exc)) from exc
    token = sched.acquire_worker_lease(f"pr-fetch:{slug}#{number}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        try:
            snapshot = _prd.fetch_pr_detail(slug, number)
        except _prd.PrError as exc:
            raise ApiError(exc.http_status or 502, str(exc)) from exc
        _prd.store_snapshot(slug, snapshot)
        return {"repo": repo, "slug": slug, "number": number,
                "snapshot": _prd.snapshot_status(slug, number),
                "review": _prr.review_outcome(slug, number)}
    finally:
        sched.release_worker_lease(token)


def _query_single(q: dict, key: str):
    """One non-blank value for an optional identity query parameter.

    Absent/blank means the client asserts nothing. Repeated keys with
    different non-blank values are a conflicting client claim and
    refuse with 400 rather than silently picking one.
    """
    vals = [v for v in q.get(key, []) if v != ""]
    if not vals:
        return None
    first = vals[0]
    for other in vals[1:]:
        if other != first:
            raise ApiError(400, f"conflicting {key} query values")
    return first


def _check_client_issue_source(payload: dict, slug: str,
                               number: int) -> None:
    """Optional client source fields must agree with the server-derived
    identity; anything conflicting (including booleans) is rejected.
    Server identity always derives from the registered profile plus the
    stored run — never from these parameters."""
    want = {"issue": str(int(number)), "slug": str(slug),
            "url": f"https://github.com/{slug}/issues/{int(number)}"}
    for key, expected in want.items():
        if key not in payload or payload.get(key) is None:
            continue
        got = payload.get(key)
        if isinstance(got, bool) or str(got).strip() != expected:
            raise ApiError(400,
                           f"conflicting {key}: server derives issue "
                           f"context from the stored run (expected "
                           f"{expected!r})")


def api_run_issue_content(payload: dict) -> dict:
    """GET /api/run/issue-content?repo=&run= — cached issue context only.

    Pure cached read (no network, no subscription gate): the latest
    stored CURRENT GitHub observation for the run's issue, or an
    explicit never-fetched/unknown state. A current observation is
    NEVER the historical trigger; fetched_at-only change is not
    content change. Runs without a numeric issue read applicable:false.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    from . import issue_context as _ic
    try:
        ctx = _ic.resolve_run_context(str(payload.get("repo") or ""),
                                      str(payload.get("run") or ""))
    except _ic.IssueContextError as exc:
        if exc.code == "not-applicable":
            return {"applicable": False, "cached": False,
                    "observation": None, "state": None,
                    "reason": str(exc)}
        raise ApiError(exc.http_status or 500, str(exc)) from exc
    _check_client_issue_source(payload, ctx["slug"], ctx["number"])
    try:
        return _ic.read_cached(ctx["repo"], ctx["run_id"])
    except _ic.IssueContextError as exc:
        raise ApiError(exc.http_status or 500, str(exc)) from exc


def api_run_issue_content_fetch(payload: dict) -> dict:
    """POST /api/run/issue-content/fetch {repo, run, confirm:true}.

    Explicit model-free fetch of the CURRENT upstream issue body/state
    into a NEW immutable observation in the issue-context namespace
    (global worker lease, 409 when busy, nothing started). Never
    writes the old run record, its trigger, prompts, logs, or proofs.
    Closed issues are valid data (shown closed), not fetch errors.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    run = str(payload.get("run") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    if not run:
        raise ApiError(400, "run is required")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{repo, run, confirm: true} to fetch "
                            "the current issue context; nothing "
                            "fetched otherwise")
    from . import issue_context as _ic
    try:
        ctx = _ic.resolve_run_context(repo, run)
    except _ic.IssueContextError as exc:
        if exc.code == "not-applicable":
            raise ApiError(400, str(exc)) from exc
        raise ApiError(exc.http_status or 500, str(exc)) from exc
    _check_client_issue_source(payload, ctx["slug"], ctx["number"])
    token = sched.acquire_worker_lease(
        f"issue-context:{ctx['slug']}#{ctx['number']}")
    if token is None:
        try:
            owner = (sched.global_lock_status() or {}).get("owner") \
                or "another worker"
        except Exception:  # noqa: BLE001 - busy reason is best-effort
            owner = "another worker"
        raise ApiError(409, f"worker busy ({owner}); nothing started "
                            "and nothing queued — submit again when free")
    try:
        try:
            result = _ic.fetch_and_store(repo, run)
        except _ic.IssueContextError as exc:
            raise ApiError(exc.http_status or 502, str(exc)) from exc
        return {"repo": repo, "run": run, "issue": ctx["number"],
                "observation": result["observation"],
                "changed": result["changed"],
                "previous_latest": result["previous_latest"]}
    finally:
        sched.release_worker_lease(token)


def api_prs_review(payload: dict) -> dict:
    """POST /api/prs/review {repo, number, by, confirm:true, evidence?}.

    Start one app-owned async PR review (owner/root confirm; the M2
    driver and workers never call it -> 400). Evidence is an optional
    list of repo-relative GitHub paths fetched at the captured head
    SHA (never a local repo_root); each entry is validated server-side
    for shape only. Empty selection auto-selects bounded changed-file
    contexts. 409 when busy. Returns {started, job|reason}.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    repo = str(payload.get("repo") or "").strip()
    by = str(payload.get("by") or "").strip()
    if not repo:
        raise ApiError(400, "repo is required")
    if by not in ("owner", "root"):
        raise ApiError(400, "by must be owner or root (a worker "
                            "and the M2 driver never start a review)")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{repo, number, by, confirm: true} to "
                            "start an app-owned PR review; nothing started "
                            "otherwise")
    from . import pr_detail as _prd
    from . import pr_reviews as _prr
    slug = _discussions_slug(repo)
    try:
        _prd.safe_pr_number(payload.get("number"))
    except _prd.PrError as exc:
        raise ApiError(400, str(exc)) from exc
    evidence = payload.get("evidence")
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list):
        raise ApiError(400, "evidence must be a list of repo-relative "
                            "GitHub paths")
    if len(evidence) > _prr._PR_SOURCE_CAP_FILES:
        raise ApiError(400, f"too many evidence files "
                            f"({len(evidence)} > "
                            f"{_prr._PR_SOURCE_CAP_FILES}); pick at most "
                            f"{_prr._PR_SOURCE_CAP_FILES} — nothing started")
    picked: list[str] = []
    for rel in evidence:
        if not isinstance(rel, str) or not _prd._valid_code_path(rel):
            raise ApiError(400, f"rejected evidence path: {rel!r}"[:160])
        if rel not in picked:
            picked.append(rel)
    try:
        return _prr.start_pr_review(slug, payload.get("number"), by=by,
                                    evidence_paths=picked)
    except _prr.PrReviewError as exc:
        raise ApiError(exc.http_status or 500, str(exc)) from exc


def api_batch_review(payload: dict) -> dict:
    """POST /api/batch/review {batch, run, verdict, by, findings,
    confirm:true} — one human verdict on one batch primary.

    M2.16c: auto-registered batches wait on an independent review
    and nothing could ever record one. by is owner|root only (a
    worker or the driver never sets a verdict) -> 400; missing
    confirm -> 400, nothing written; unknown run -> 409. The
    contract lives in batches.set_review (identity re-shot,
    proof check for accept verdicts, previous kept, one event).
    One call sets one primary. The request path never refreshes
    synchronously — it queues refresh_all_soon("review").
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    batch_id = str(payload.get("batch") or "").strip()
    run_id = str(payload.get("run") or "").strip()
    verdict = str(payload.get("verdict") or "").strip()
    by = str(payload.get("by") or "").strip()
    if not batch_id:
        raise ApiError(400, "batch is required")
    if not run_id:
        raise ApiError(400, "run is required")
    if by not in ("owner", "root"):
        raise ApiError(400, "by must be owner or root (a worker "
                            "and the M2 driver never set a verdict)")
    if verdict not in ("accept", "accept-with-notes",
                       "request-changes"):
        raise ApiError(400, "verdict must be accept, "
                            "accept-with-notes or request-changes")
    findings = payload.get("findings") or []
    if not isinstance(findings, list):
        raise ApiError(400, "findings must be a list of one-line "
                            "strings")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{batch, run, verdict, by, findings, "
                            "confirm: true} to record an independent "
                            "review verdict; nothing written")
    try:
        out = batches.set_review(batch_id, run_id, verdict, by,
                                 findings)
    except batches.ReviewValidationError as exc:
        raise ApiError(400, str(exc)) from exc
    except batches.BatchError as exc:
        raise ApiError(409, str(exc)) from exc
    # Same freshness as attest/hold: the screens move together
    # off the request path.
    refresh_all_soon("review")
    return out


def api_batch_hold(payload: dict) -> dict:
    """Owner/root hold ("보류") on a batch (explicit only).

    Payload: {batch, hold:true/false, attestor, reason?, confirm:
    true}. Without confirm:true the call refuses with 400 and
    writes nothing. `hold` must be an explicit boolean — a missing
    key refuses instead of releasing (M2.5c-N2). The contract
    lives in batches.hold_batch (owner/root only, reason required
    to hold, release restores the untouched gates). The snapshot
    refresh runs in the background (M2.5c latency): the call
    returns at once and the glance poll picks up the change.
    """
    _require_gate()
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be a JSON object")
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{batch, hold, attestor, confirm: true} "
                            "to hold; nothing written")
    if not isinstance(payload.get("hold"), bool):
        raise ApiError(400, "hold must be an explicit boolean "
                            "(true to hold, false to release)")
    try:
        out = batches.hold_batch(
            str(payload.get("batch") or ""),
            payload.get("hold"),
            payload.get("attestor"),
            reason=str(payload.get("reason") or ""),
            refresh=False)
    except batches.BatchError as exc:
        raise ApiError(409, str(exc)) from exc
    # Hold freshness: the batches envelope AND the glance refresh
    # together in the background, never on the request path.
    refresh_all_soon("hold")
    return out


def _overview_rollup_rows() -> dict:
    """{repo name: {lane: rollup row}} for the reviews tab, read from
    the glance summary snapshot (read-only; never recomputed per
    request). Missing or unreadable file degrades to {} — the
    reviews tab then shows no stored draft, exactly as before."""
    try:
        snap = batches.read_json(batches.summary_snapshot_path(),
                                 default=None)
    except (Exception, SystemExit):  # noqa: BLE001 - corrupt file
        # degrades openly too (read_json raises SystemExit on
        # invalid JSON); the reviews tab simply shows no draft.
        return {}
    out = {}
    if isinstance(snap, dict):
        for row in snap.get("repos") or []:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            rollup = row.get("rollup")
            if isinstance(rollup, dict) and rollup:
                out[str(row["name"])] = rollup
    return out


def _read_batches_snapshot() -> dict | None:
    """The precomputed batches envelope, or None when no usable file
    exists yet (cold start: the caller computes synchronously)."""
    try:
        snap = batches.read_json(batches.batches_snapshot_path(),
                                 default=None)
    except Exception:  # noqa: BLE001 - unreadable file degrades openly
        return None
    if not isinstance(snap, dict) or not snap.get("at") \
            or not isinstance(snap.get("batches"), list):
        return None
    return snap


_BATCHES_REFRESH_LOCK = threading.Lock()


def api_batches_refresh(payload: dict) -> dict:
    """POST /api/batches/refresh {confirm:true}: recompute the batches
    snapshot in a background thread and return at once — never on the
    request path. A refresh already running reports started:false so
    callers poll the snapshot stamp instead of stacking work."""
    if payload.get("confirm") is not True:
        raise ApiError(400, "confirmation required: post "
                            "{confirm: true} to refresh the batches "
                            "snapshot in the background")
    if not _BATCHES_REFRESH_LOCK.acquire(blocking=False):
        return {"started": False,
                "reason": "새로고침이 이미 진행 중 — 잠시 후 다시 확인"}
    def _run():
        try:
            batches.refresh_batches_snapshot("manual")
        except Exception:  # noqa: BLE001 - refresh never raises anyway
            pass
        finally:
            _BATCHES_REFRESH_LOCK.release()
    th = threading.Thread(target=_run, daemon=True)
    th.start()
    return {"started": True,
            "reason": "백그라운드에서 새로고침 중 — 잠시 후 다시 확인"}


def api_batches(_payload: dict) -> dict:
    """GET /api/batches: summaries with live readiness. Nothing hardcoded.

    Served from the precomputed snapshot file (M1 pattern): the
    background refresh (tick / import / rollup / redraft / manual)
    owns the evaluation cost, and the request only reads one file.
    Cold start (no usable file yet) computes once synchronously,
    exactly as before. The body carries the file's own `at` stamp
    (no served_at: repaint compares semantic content with obs/at/
    evaluated_at stripped, so a per-request clock would only add
    noise).

    One envelope, one evaluation per batch: cards and the repo summary
    below derive from the SAME already-computed evaluations, so a card
    and its summary can never be two observations. Every response
    stamps a fresh observation id on the envelope and all its cards
    (semantic content unchanged): two responses never share an id,
    while one response's cards always share its id.

    Obs semantics (shared with /api/summary): `refresh_obs` is the
    refresh pass id, identical in both snapshots and stable across
    responses — clients compare it for the one-observation-across-
    screens check. `obs` is per-response on the wire (/api/batches
    stamps it fresh; /api/summary serves the stored one).
    """
    snap = _read_batches_snapshot()
    if snap is not None:
        out = dict(snap)
        fresh = "obs-" + secrets.token_hex(8)
        out["obs"] = fresh
        for c in out.get("batches") or []:
            if isinstance(c, dict):
                c["obs"] = fresh
        return out
    batch_list = batches.list_batches()
    live, live_diag = _live_for_batches(batch_list)
    obs = "obs-" + secrets.token_hex(8)
    batch_by_id = {b.get("id"): b for b in batch_list
                   if isinstance(b, dict) and b.get("id")}
    evals: dict = {}
    for b in batch_list:
        bl = live.get(b.get("id")) or {}
        ev = batches.evaluate(b, bl,
                              live_diag=live_diag.get(b.get("id")),
                              record=True)
        evals[b.get("id")] = ev
    cards = []
    for b in batch_list:
        bl = live.get(b.get("id")) or {}
        ev = evals.get(b.get("id")) or {"results": []}
        ready = sum(1 for r in (ev.get("results") or [])
                    if isinstance(r, dict) and r.get("ready"))
        catalog = (b.get("policy") or {}).get("gates") or {}
        attention = []
        card_integs = []
        for i in b.get("integrations") or []:
            wl = bl.get("wt:" + (i.get("worktree") or "")) or {}
            sigs = []
            for g in i.get("gates") or []:
                if (g.get("status") in ("fail", "blocked")
                        and all(a["key"] != g.get("key")
                                for a in attention)):
                    spec = catalog.get(g.get("key") or "") or {}
                    attention.append({"key": g.get("key"),
                                      "label": spec.get("label")
                                      or g.get("key")})
                ins = g.get("inputs") or {}
                sigs.append({"key": g.get("key"),
                             "status": g.get("status"),
                             "inputs_head": ins.get("head"),
                             "inputs_fp": ins.get("fingerprint"),
                             "ev": sorted(e.get("sha256") or ""
                                          for e in g.get("evidence") or []
                                          if e.get("kind") != "note")})
            card_integs.append({"lane": i.get("lane"),
                                "worktree": i.get("worktree"),
                                "branch": (b.get("origin") or {}).get(
                                    "base_branch"),
                                "head": (i.get("head") or "")[:12],
                                "fingerprint": i.get("fingerprint"),
                                "live_head": wl.get("head"),
                                "live_fingerprint": wl.get("fingerprint"),
                                "live_clean": wl.get("clean"),
                                "status": i.get("status"),
                                "gates": sigs})
        diag = batches.card_diagnostic(b, ev, bl, batch_by_id, evals)
        cards.append({"id": b.get("id"), "repo": b.get("repo"),
                      "target_branch": b.get("target_branch"),
                      "purpose": b.get("purpose"),
                      "updated_at": b.get("updated_at"),
                      "primaries": len(b.get("primaries") or []),
                      "primary_items": batches._primary_items(b),
                      "inherited": b.get("inherited") or [],
                      "integrations": card_integs,
                      "ready": ready, "attention": attention,
                      "evaluated_at": ev.get("evaluated_at"),
                      "obs": obs, "evaluation": diag})
    # Repo-scoped unregistered projection: independent of existing
    # batches, so a first-ever natural review is visible with no batch.
    unregistered = {}
    try:
        repo_names = repos.list_all()
    except Exception:  # noqa: BLE001 - never break the list on registry read
        repo_names = []
    for name in repo_names:
        try:
            queue = sched.load(name).get("queue", [])
        except SystemExit:
            queue = []
        items = batches.uncovered_candidates(queue, batch_list, name)
        if items:
            unregistered[name] = items
    from .util import utcnow as _now2
    return {"batches": cards,
            "summary": batches.summarize(batch_list, live,
                                         precomputed=evals),
            "unregistered": unregistered, "obs": obs,
            "at": _now2(), "writer": "request-fallback"}


def api_batch(payload: dict) -> dict:
    """GET /api/batch?id=: full batch + live evaluation + unregistered."""
    batch_id = str(payload.get("id") or "")
    try:
        b = batches.load_batch(batch_id)
    except batches.BatchError as exc:
        raise ApiError(404, str(exc))
    by_live, by_diag = _live_for_batches([b])
    live = by_live.get(batch_id)
    ev = batches.evaluate(b, live,
                          live_diag=by_diag.get(batch_id), record=True)
    # Compact fresh observations for stale-pass labeling and detail
    # refresh (heads/fingerprints/clean only — no clock).
    live_out = {}
    for key, snap in (live or {}).items():
        if isinstance(snap, dict):
            live_out[key] = {"head": snap.get("head"),
                             "fingerprint": snap.get("fingerprint"),
                             "clean": snap.get("clean", True)}
        else:
            live_out[key] = None
    try:
        queue = sched.load(b.get("repo") or "").get("queue", [])
    except SystemExit:
        queue = []
    unregistered = batches.uncovered_candidates(
        queue, batches.list_batches(), b.get("repo"))
    queue_by_run = {}
    for it in queue:
        if isinstance(it, dict) and it.get("run_id"):
            queue_by_run[it["run_id"]] = {
                k: it.get(k) for k in ("dep", "target", "lane", "status",
                                      "branch", "declared", "installed",
                                      "compared", "scopes", "key")}
    review = _batch_review_block(b)
    return {"batch": b, "evaluation": ev, "unregistered": unregistered,
            "queue": queue_by_run, "live": live_out, "review": review,
            "obs": "obs-" + secrets.token_hex(8)}


def _batch_review_block(batch: dict):
    """Review-screen data for an auto batch (M2.4b).

    Only origin_m2 batches with waiting Root gates get a block:
    the op record's fastcheck summary (with the isolation line the
    UI renders), the pending gates with their catalog labels, and
    the already-landed ones. Anything else returns None — the
    screen stays hidden. Read-only; never raises.
    """
    try:
        if not isinstance(batch, dict) or not batch.get("origin_m2"):
            return None
        op_id = str(batch.get("origin_m2") or "")
        opf = batches.op_file(op_id)
        rec = batches.read_json(opf, default=None) or {}
        if not isinstance(rec, dict):
            return None
        pending = batches._root_pending_names(batch)
        if not pending:
            return None
        catalog = (batch.get("policy") or {}).get("gates") or {}
        summary = rec.get("fastcheck_summary") or {}
        return {"op": op_id,
                "fastcheck_summary": summary,
                "pending": [{"key": key,
                             "label": str((catalog.get(key) or {})
                                          .get("label") or key)}
                            for key in pending],
                "op_note": str((rec.get("result") or {}).get("note")
                               or "")[:500]}
    except Exception:  # noqa: BLE001 - review data never breaks detail
        return None


def _git_run(args: list, cwd: str, cap: int = 200000) -> str | None:
    import subprocess
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30)
    except (OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired):
        return None
    text = out.stdout
    return text[:cap] if len(text) > cap else text


def _integration_changed_files(worktree: str, base: str,
                               head: str) -> tuple:
    """(files|None, error|None): read-only name list base..head.

    Shared by the batch-changes endpoint and the work-view current-QA
    projection so both report the same integration scope: the prepared
    worktree's recorded base against its recorded integration head.
    Never raises; unreadable sources yield an explicit error.
    """
    if not base or not head:
        return None, "integration records no base/head to compare"
    for rev in (base, head):
        if _git_run(["cat-file", "-e", rev], worktree) is None:
            return None, (f"recorded revision {rev[:12]} not present "
                           "in the local checkout")
    files = _git_run(["diff", "--name-only", base, head], worktree)
    if files is None:
        return None, "diff unreadable"
    return [f for f in files.splitlines() if f][:200], None


def api_batch_changes(payload: dict) -> dict:
    """GET /api/batch/changes: integration-scoped commit/files/diff.

    Read-only git inspection of the PERSISTED integration worktree
    between the recorded base and the recorded integration head.
    Candidate-only run diffs are not whole-integration changes, so the
    viewer is scoped here. Per-file diff via path= (repo-relative,
    no traversal). Anything unresolvable is an honest error, never
    invented content.
    """
    batch_id = str(payload.get("batch") or payload.get("id") or "")
    lane = str(payload.get("lane") or "")
    path = str(payload.get("path") or "")
    try:
        b = batches.load_batch(batch_id)
    except batches.BatchError as exc:
        raise ApiError(404, str(exc))
    integ = None
    for i in b.get("integrations") or []:
        if not lane or (i.get("lane") or "") == lane:
            integ = i
            break
    if integ is None:
        raise ApiError(404, f"no such integration lane {lane!r}")
    wt = integ.get("worktree") or ""
    if (not wt or not Path(wt).is_dir()
            or batches._shared_git_dir(wt) is None):
        raise ApiError(409, "integration worktree not a readable checkout")
    base = (b.get("origin") or {}).get("base_head") or ""
    head = integ.get("head") or ""
    if not base or not head:
        raise ApiError(409, "integration records no base/head to compare")
    for rev in (base, head):
        if _git_run(["cat-file", "-e", rev], wt) is None:
            raise ApiError(409, f"recorded revision {rev[:12]} not present "
                                "in the local checkout")
    if path:
        if path.startswith("/") or ".." in Path(path).parts:
            raise ApiError(400, "path must be repo-relative")
        diff = _git_run(["diff", "--no-color", "-U3", base, head, "--",
                         path], wt)
        if diff is None:
            raise ApiError(409, "diff unreadable")
        return {"batch": batch_id, "lane": integ.get("lane"),
                "base": base, "head": head, "path": path, "diff": diff}
    raw = _git_run(["log", "--format=%H%x00%s%x00%an%x00%aI", base + ".." + head],
                   wt)
    commits = []
    if raw is None:
        raise ApiError(409, "commit range unreadable")
    for line in raw.splitlines():
        parts = line.split("\x00")
        if len(parts) == 4:
            commits.append({"sha": parts[0], "subject": parts[1],
                            "author": parts[2], "at": parts[3]})
        if len(commits) >= 50:
            break
    files = (_git_run(["diff", "--name-only", base, head], wt) or "")
    return {"batch": batch_id, "lane": integ.get("lane"), "base": base,
            "head": head, "commits": commits,
            "files": [f for f in files.splitlines() if f][:200]}


def safe_batch_evidence(batch_id: str, name: str):
    """Constrain /api/batch/evidence downloads to the batch evidence dir."""
    try:
        batches.load_batch(batch_id)
    except batches.BatchError:
        return None
    if not name or "/" in name or name.startswith("."):
        return None
    base = batches.evidence_dir(batch_id)
    try:
        dest = (base / name).resolve()
        dest.relative_to(base.resolve())
    except (OSError, ValueError):
        return None
    return dest if dest.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenMaintyDashboard/0.3"

    def handle_one_request(self):
        """G9: release the admission slot when the request finishes.

        handle_one_request runs once per request even with keep-alive,
        so the semaphore is always returned — including on errors.
        """
        try:
            super().handle_one_request()
        finally:
            _release_request(self)

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _gate_origin(self, need_json: bool) -> bool:
        """Loopback-only gate, first thing in every handler.

        Host must be exactly 127.0.0.1:<port> or localhost:<port>
        (port from the bound socket — kills DNS rebinding). Origin,
        when present, must equal http://<that host> ("null" and
        anything else refuse — kills CSRF page POSTs, which always
        carry Origin). POST additionally requires Content-Type
        application/json (parameters allowed). Absent Origin (curl,
        CLI, same-page GETs) is fine. No CORS headers are ever sent.
        Returns True when the request may proceed."""
        host = (self.headers.get("Host") or "").strip()
        try:
            port = self.server.server_address[1]
        except Exception:
            port = 8901
        name, sep, hport = host.partition(":")
        if not sep or hport != str(port) or \
                name.lower() not in ("127.0.0.1", "localhost"):
            self._send_json(403, {"error": "foreign host"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() != \
                "http://" + name.lower() + ":" + hport:
            self._send_json(403, {"error": "foreign origin"})
            return False
        if need_json:
            ctype = (self.headers.get("Content-Type") or "").split(
                ";")[0].strip().lower()
            if ctype != "application/json":
                self._send_json(
                    403, {"error": "content-type must be application/json"})
                return False
        return True

    def do_GET(self):  # noqa: N802
        if not self._gate_origin(need_json=False):
            return
        parsed = urlparse(self.path)
        if not _admit_request(self, parsed.path):
            return
        try:
            if parsed.path == "/":
                self._send(_page().encode(), "text/html; charset=utf-8")
            elif parsed.path == "/favicon.svg":
                self._send(FAVICON_SVG.encode(), "image/svg+xml")
            elif parsed.path == "/api/overview":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, overview(
                        {"timing": q.get("timing", [""])[0] == "1"}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/run":
                run_id = parse_qs(parsed.query).get("id", [""])[0]
                try:
                    self._send_json(200, store.load(run_id))
                except SystemExit as exc:
                    self._send_json(404, {"error": str(exc)})
            elif parsed.path == "/api/file":
                q = parse_qs(parsed.query)
                dest = safe_run_file(q.get("run", [""])[0], q.get("name", [""])[0])
                if dest is None:
                    self.send_error(404, "no such file")
                    return
                self._send(dest.read_bytes(), "text/plain; charset=utf-8")
            elif parsed.path == "/api/integration/log":
                q = parse_qs(parsed.query)
                dest = api_integration_log_file(q.get("op", [""])[0],
                                                q.get("fid", [""])[0])
                if dest is None:
                    self.send_error(404, "no such followup log")
                    return
                self._send(dest.read_bytes(), "text/plain; charset=utf-8")
            elif parsed.path == "/api/run/stream":
                run_id = parse_qs(parsed.query).get("id", [""])[0]
                try:
                    store.load(run_id)
                except SystemExit as exc:
                    self._send_json(404, {"error": str(exc)})
                    return
                self._handle_run_stream(run_id)
                return
            elif parsed.path == "/api/doctor":
                self._send_json(200, doctor.quick())
            elif parsed.path == "/api/settings":
                try:
                    self._send_json(200, api_settings_get())
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/session":
                session_id = parse_qs(parsed.query).get("id", [""])[0]
                try:
                    session = chat.load(session_id)
                except SystemExit as exc:
                    self._send_json(404, {"error": str(exc)})
                    return
                self._send_json(200, {"session": session,
                                      "transcript": chat.read_transcript(session_id),
                                      "summary": chat.session_card(session)})
            elif parsed.path == "/api/browse":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_browse(
                        {"path": q.get("path", [""])[0],
                         "hidden": q.get("hidden", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/job":
                job_id = parse_qs(parsed.query).get("id", [""])[0]
                job = _JOBS.get(job_id)
                if job is None:
                    self._send_json(404, {"error": f"unknown job '{job_id}'"})
                    return
                self._send_json(200, job)
            elif parsed.path == "/api/sched":
                q = parse_qs(parsed.query)
                self._send_json(200, api_sched_status(
                    {"repo": q.get("repo", [""])[0]}))
            elif parsed.path == "/api/m2/policy":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_m2_policy(
                        {"repo": q.get("repo", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/repo/profile":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_repo_profile(
                        {"repo": q.get("repo", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/graph":
                q = parse_qs(parsed.query)
                self._send_json(200, api_graph({"repo": q.get("repo", [""])[0]}))
            elif parsed.path == "/api/events":
                q = parse_qs(parsed.query)
                self._send_json(200, api_events(
                    {"repo": q.get("repo", [""])[0],
                     "limit": q.get("limit", ["100"])[0]}))
            elif parsed.path == "/api/deps/discover":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_deps_discover(
                        {"repo": q.get("repo", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/discussions":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_discussions(
                        {"repo": q.get("repo", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/discussions/node":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_discussions_node(
                        {"repo": q.get("repo", [""])[0],
                         "number": q.get("number", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/discussions/attempt":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_discussions_attempt(
                        {"repo": q.get("repo", [""])[0],
                         "number": q.get("number", [""])[0],
                         "artifact": q.get("artifact", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/discussions/sources":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_discussions_sources(
                        {"repo": q.get("repo", [""])[0],
                         "dir": q.get("dir", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/prs":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_prs(
                        {"repo": q.get("repo", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/prs/detail":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_prs_detail(
                        {"repo": q.get("repo", [""])[0],
                         "number": q.get("number", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/prs/attempt":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_prs_attempt(
                        {"repo": q.get("repo", [""])[0],
                         "number": q.get("number", [""])[0],
                         "artifact": q.get("artifact", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/run/issue-content":
                q = parse_qs(parsed.query)
                try:
                    payload = {
                        "repo": _query_single(q, "repo") or "",
                        "run": _query_single(q, "run") or "",
                    }
                    # Optional client identity hints reach the same
                    # validator the POST body uses; conflicting claims
                    # refuse here at the real HTTP boundary instead of
                    # being silently dropped.
                    for key in ("issue", "slug", "url"):
                        val = _query_single(q, key)
                        if val is not None:
                            payload[key] = val
                    self._send_json(200, api_run_issue_content(payload))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/integration/recovery":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_integration_recovery(
                        {"op": q.get("op", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/integration/derived-fix":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_derived_fix(
                        {"op": q.get("op", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/integration/derived-fix/input":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_derived_fix_input(
                        {"op": q.get("op", [""])[0],
                         "dfix": q.get("dfix", [""])[0],
                         "attempt": q.get("attempt", [""])[0]}))
                except ApiError as exc:
                    # Fail-closed HTTP is preserved (non-2xx), with a
                    # structured safe code so the viewer can tell a
                    # lost artifact from corruption from a fetch
                    # failure. Only this endpoint sets codes; every
                    # other registration is byte-identical.
                    self._send_json(exc.status, {
                        "error": exc.message,
                        "code": getattr(exc, "code", None) or ""})
            elif parsed.path == "/api/integration/derived-fix/preflight":
                try:
                    self._send_json(200, api_derived_fix_preflight({}))
                except ApiError as exc:
                    # Same fail-closed shape as the input viewer: the
                    # panel tells missing/stale/unknown/corrupt/auth
                    # refusals apart without inventing success.
                    self._send_json(exc.status, {
                        "error": exc.message,
                        "code": exc.code or ""})
            elif parsed.path == "/api/work":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_work({"run": q.get("run", [""])[0],
                                                  "id": q.get("id", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/work/changes":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_source_changes(
                        {"run": q.get("run", [""])[0],
                         "id": q.get("id", [""])[0],
                         "path": q.get("path", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/refs":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_refs(
                        {"repo": q.get("repo", [""])[0],
                         "numbers": q.get("numbers", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/batches":
                try:
                    self._send_json(200, api_batches({}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/summary":
                try:
                    self._send_json(200, api_summary({}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/decisions":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_decisions(
                        {"repo": q.get("repo", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/decision/detail":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_decision_detail(
                        {"repo": q.get("repo", [""])[0],
                         "id": q.get("id", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/onboard/caps":
                # M3.2b F1: the dashboard fetches caps with GET. This
                # branch serves it; the POST table never did.
                try:
                    self._send_json(200, api_onboard_caps())
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/app":
                try:
                    self._send_json(200, api_app({}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/disk":
                try:
                    self._send_json(200, api_disk({}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/digest":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_digest(
                        {"date": q.get("date", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/batch":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_batch(
                        {"id": q.get("id", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/batch/qa-pack":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_batch_qa_pack_get(
                        {"batch": q.get("batch", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/batch/changes":
                q = parse_qs(parsed.query)
                try:
                    self._send_json(200, api_batch_changes(
                        {"batch": q.get("batch", [""])[0],
                         "lane": q.get("lane", [""])[0],
                         "path": q.get("path", [""])[0]}))
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
            elif parsed.path == "/api/batch/evidence":
                q = parse_qs(parsed.query)
                dest = safe_batch_evidence(q.get("batch", [""])[0],
                                           q.get("name", [""])[0])
                if dest is None:
                    self.send_error(404, "no such file")
                    return
                body = dest.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type",
                                 _evidence_mime(dest.name))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "unknown path")
        except Exception as exc:  # noqa: BLE001 - never drop the connection raw
            self.send_error(500, f"dashboard error: {html.escape(str(exc))[:200]}")

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return None, "invalid JSON body"
        if not isinstance(payload, dict):
            return None, "JSON object body required"
        return payload, None

    def _handle_chat_stream(self, payload):
        err = chat.subscription_error()
        if err:
            self._send_json(403, {"error": err})
            return
        is_plan = bool(payload.get("plan"))
        message = (payload.get("message") or "").strip()
        if not message and not is_plan:
            self._send_json(400, {"error": "message is required"})
            return
        sid = (payload.get("id") or "").strip()
        if is_plan and not sid:
            self._send_json(400, {"error": "id is required for plan"})
            return
        try:
            if sid:
                session = chat.load(sid)
            else:
                repo = (payload.get("repo") or "").strip()
                if not repo:
                    self._send_json(400, {"error": "repo is required"})
                    return
                _load_profile(repo)
                session = chat.create(repo, payload.get("issue", ""),
                                      payload.get("title", ""),
                                      muse.agent_info(model=_settings_model()))
            profile = _load_profile(session["repo"])
            checkout = _resolve_checkout(profile)
        except SystemExit as exc:
            self._send_json(404, {"error": str(exc)})
            return
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        workdir = chat.ensure_workdir(session, checkout, profile.get("baseBranch", "main"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        # No Content-Length on a stream: the closing connection is the EOF.
        self.close_connection = True
        cancel_event = threading.Event()

        def _emit(obj):
            try:
                line = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                cancel_event.set()

        _emit({"t": "started", "id": session["id"]})
        if cancel_event.is_set():
            return
        prompt_override = None
        if is_plan:
            prompt_override = chat.plan_prompt_for(profile, _diagnose(session["repo"]))
        try:
            turn = chat.send_stream(
                session, profile, message,
                runner.Executor(model=_settings_model(), cancel_event=cancel_event),
                workdir,
                lambda chunk: _emit({"t": "chunk", "text": chunk}),
                prompt_override=prompt_override)
        except runner.QuotaHit:
            _emit({"t": "wait", "id": session["id"]})
            return
        except Cancelled:
            return  # client gone; worker already reaped
        except Exception as exc:  # noqa: BLE001 - stream open; frame the error
            _emit({"t": "error", "message": f"{type(exc).__name__}: {exc}"[:300]})
            return
        if is_plan:
            session["plan_pending"] = session["repo"]
            chat.save(session)
        _emit({"t": "done", "id": session["id"], "reply": turn["text"]})

    def _handle_run_stream(self, run_id):
        """SSE tail of the currently running step's live log."""
        import time as _time
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        alive = {"ok": True}

        def _emit(obj):
            if not alive["ok"]:
                return
            try:
                line = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                alive["ok"] = False

        deadline = _time.time() + 600
        cur_step, offset, primed = None, 0, False
        while _time.time() < deadline and alive["ok"]:
            try:
                state = store.load(run_id)
            except SystemExit:
                break
            running = [n for n, e in state["steps"].items()
                       if e.get("status") == "running"]
            if not running:
                _emit({"t": "done", "id": run_id})
                break
            step = running[0]
            if step != cur_step:
                cur_step, offset, primed = step, 0, False
                _emit({"t": "step", "id": run_id, "step": step})
            try:
                data = (store.run_dir(run_id) / f"{step}.live.log.txt").read_bytes()
            except OSError:
                data = b""
            if not primed:
                offset = max(0, len(data) - 20000)
                primed = True
            if len(data) > offset:
                try:
                    text = data[offset:].decode("utf-8")
                except UnicodeDecodeError:
                    text = data[offset:].decode("utf-8", "replace")
                offset = len(data)
                if text:
                    _emit({"t": "chunk", "id": run_id, "step": step, "text": text})
            _time.sleep(1)

    def do_POST(self):  # noqa: N802
        if not self._gate_origin(need_json=True):
            return
        parsed = urlparse(self.path)
        if not _admit_request(self, parsed.path):
            return
        if parsed.path == "/api/chat/stream":
            payload, err = self._read_json_body()
            if err:
                self._send_json(400, {"error": err})
                return
            self._handle_chat_stream(payload)
            return
        routes = {"/api/chat/start": api_chat_start,
                  "/api/chat/send": api_chat_send,
                  "/api/plan/start": api_plan_start,
                  "/api/plan/approve": api_plan_approve,
                  "/api/chat/delete": api_chat_delete,
                  "/api/watch": api_watch,
                  "/api/job/maintain": api_job_maintain,
                  "/api/job/run": api_job_run,
                  "/api/job/resume": api_job_resume,
                  "/api/job/answer": api_job_answer,
                  "/api/job/cancel": api_job_cancel,
                  "/api/repo/add-local": api_repo_add_local,
                  "/api/repo/detect": api_repo_detect,
                  "/api/repo/delete": api_repo_delete,
                  "/api/repo/profile": api_repo_profile,
                  "/api/job/clone": api_job_clone,
                  "/api/settings": api_settings_set,
                  "/api/sched/config": api_sched_config,
                  "/api/m2/policy": api_m2_policy,
                  "/api/sched/scan": api_sched_scan,
                  "/api/sched/cancel": api_sched_cancel,
                  "/api/sched/run-due": api_sched_run_due,
                  "/api/sched/daemon": api_sched_daemon,
                  "/api/sched/watch-inputs": api_watch_inputs,
                  "/api/integration/prepare": api_integration_prepare,
                  "/api/integration/register": api_integration_register,
                  "/api/integration/reresolve": api_integration_reresolve,
                  "/api/integration/fastcheck": api_integration_fastcheck,
                  "/api/integration/fastcheck-rerun":
                      api_integration_fastcheck_rerun,
                  "/api/integration/family-retry":
                      api_integration_family_retry,
                  "/api/integration/prepare-retry":
                      api_integration_prepare_retry,
                  "/api/integration/joint-retry":
                      api_integration_joint_retry,
                  "/api/integration/joint-refresh":
                      api_integration_joint_refresh,
                  "/api/integration/rollup": api_integration_rollup,
                  "/api/integration/followup": api_integration_followup,
                  "/api/integration/recovery/review":
                      api_integration_recovery_review,
                  "/api/integration/recovery/owner-decision":
                      api_integration_recovery_owner_decision,
                  "/api/integration/derived-fix/start":
                      api_derived_fix_start,
                  "/api/integration/derived-fix/cancel":
                      api_derived_fix_cancel,
                  "/api/integration/derived-fix/preflight/create":
                      api_derived_fix_preflight_create,
                  "/api/integration/derived-fix/preflight/run":
                      api_derived_fix_preflight_run,
                  "/api/integration/derived-fix/preflight/cancel":
                      api_derived_fix_preflight_cancel,
                  "/api/batch/import": api_batch_import,
                  "/api/batch/attest": api_batch_attest,
                  "/api/batch/review": api_batch_review,
                  "/api/discussions/refresh": api_discussions_refresh,
                  "/api/discussions/detail": api_discussions_detail,
                  "/api/discussions/review": api_discussions_review,
                  "/api/prs/fetch": api_prs_fetch,
                  "/api/prs/review": api_prs_review,
                  "/api/run/issue-content/fetch":
                      api_run_issue_content_fetch,
                  "/api/batch/qa-pack": api_batch_qa_pack,
                  "/api/batch/hold": api_batch_hold,
                  "/api/batches/refresh": api_batches_refresh,
                  "/api/graph": api_graph,
                  "/api/events": api_events,
                  "/api/deps/discover": api_deps_discover,
                  "/api/work": api_work}
        fn = routes.get(parsed.path)
        if fn is None:
            self.send_error(404, "unknown path")
            return
        payload, err = self._read_json_body()
        if err:
            self._send_json(400, {"error": err})
            return
        try:
            self._send_json(200, fn(payload))
        except ApiError as exc:
            # Only raises that explicitly set a code emit one; every
            # other POST route behaves exactly as before (no code
            # key). The preflight family sets codes so the panel can
            # tell missing/stale/unknown/corrupt/auth refusals apart.
            body = {"error": exc.message}
            if exc.code:
                body["code"] = exc.code
            self._send_json(exc.status, body)
        except Exception as exc:  # noqa: BLE001 - never drop the connection raw
            self.send_error(500, f"dashboard error: {html.escape(str(exc))[:200]}")

    def log_message(self, *args):  # keep cron logs quiet
        pass


def _maybe_startup_refresh() -> dict:
    """M2.9c: a restart must not drop a pending refresh. When the
    served batches snapshot predates the newest hold/attest event
    (or any batch file is newer), schedule the background refresh
    the killed worker never wrote. Read-only check; never raises."""
    try:
        from . import batches as _batches
        st = _batches.snapshot_stale_on_startup()
    except Exception:
        st = {"stale": True, "reason": "확인 실패"}
    if st.get("stale"):
        refresh_all_soon("startup")
    return st


def serve(port: int) -> None:
    _maybe_startup_refresh()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"openmainty dashboard on http://127.0.0.1:{httpd.server_port}/ (Ctrl-C to stop)")
    httpd.serve_forever()
