"""Persistent scheduler/worker owned by OpenMainty (stdlib only).

Per-repo state at <home>/sched/<repo>.json:
{enabled, execute_enabled, interval_s, last_scan, next_scan, heartbeat,
 current_job, stale, error, scan_ok, sources, backoff_s, failures,
 queue: [...], history: [...], last_rows, last_issues, issues_error}

- scan: manifest discovery + registry latest + open-issue refresh.
  Read-only; never spends quota. Queue items dedup by lifecycle key.
- execute: one bounded worker (global atomic owner lock, one item per
  call), named feature worktree from the lane's base, same-run
  crash recovery, bounded retries with backoff, pause/cancel/quota.
- tick: due scans only. daemon_tick: tick + one execute step per repo
  whose autonomous execute mode is ON (maintenance launchd service).
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from . import cli, deps, events, paths, repos, store
from .util import read_json, utcnow, write_json

DEFAULT_INTERVAL_S = 30 * 60
MAX_RETRIES = 3
RETRY_BACKOFF_S = [300, 900, 3600]
LOCK_TTL_S = 1800

# Queue statuses that still own their dedup key (nothing new may reuse it).
LIVE_STATUSES = ("queued", "running", "blocked", "waiting-review")
# Same dep declared in root/example/peer manifests is ONE job, not three.
COUPLED_SCOPES = ("dependencies", "devDependencies", "peerDependencies")
# E5: the m2 lane-cooldown map key. Defined here (sched owns the state
# defaults) and shared by m2's writer, reader and clearer — one
# constant, no string duplicates. m2 reads it as sched._COOLDOWN_KEY
# (sched never imports m2 at module level, so no cycle).
_COOLDOWN_KEY = "m2_cooldown_until"


def _file(repo: str) -> Path:
    return paths.home() / "sched" / f"{repo}.json"


def load(repo: str) -> dict:
    data = read_json(_file(repo), default=None)
    if data is None:
        return {"repo": repo, "enabled": False, "execute_enabled": False,
                "interval_s": DEFAULT_INTERVAL_S,
                "last_scan": None, "next_scan": None, "heartbeat": None,
                "current_job": None, "stale": False, "error": None,
                "scan_ok": None, "sources": {},
                "last_dispatch": None, "next_dispatch": None,
                "worker": None,
                "backoff_s": 0, "failures": 0, "queue": [], "history": [],
                "last_rows": [], "last_issues": [], "issues_error": None,
                "last_prs": [], "prs_error": None,
                "m2_prepare": False, "m2_fastcheck": False,
                "m2_batch": False, "m2_rollup": False}
    data.setdefault("queue", [])
    data.setdefault("history", [])
    data.setdefault("failures", 0)
    data.setdefault("backoff_s", 0)
    data.setdefault("execute_enabled", False)
    for key in ("m2_prepare", "m2_fastcheck", "m2_batch", "m2_rollup"):
        data.setdefault(key, False)
    data.setdefault("m2_lease_busy_since", None)
    data.setdefault("m2_consec_blocks", {})
    data.setdefault(_COOLDOWN_KEY, {})
    data.setdefault("m2_last_note", {})
    data.setdefault("m2_notes", {})
    data.setdefault("m2_max_prepare_per_tick", 1)
    data.setdefault("m2_last_guard_ok", None)
    data.setdefault("last_rows", [])
    data.setdefault("last_issues", [])
    data.setdefault("issues_error", None)
    data.setdefault("last_prs", [])
    data.setdefault("prs_error", None)
    data.setdefault("sources", {})
    data.setdefault("scan_ok", None)
    data.setdefault("last_dispatch", None)
    data.setdefault("next_dispatch", None)
    data.setdefault("worker", None)
    return data


def save(state: dict) -> None:
    write_json(_file(state["repo"]), state)


@contextlib.contextmanager
def locked(repo: str):
    """Advisory per-repo mutex so scan/config/worker updates can't lose
    each other's queue edits across processes (POSIX flock)."""
    import fcntl
    mtx = paths.home() / "sched" / f".{repo}.mtx"
    mtx.parent.mkdir(parents=True, exist_ok=True)
    with open(mtx, "a+b") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass
        try:
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def configure(repo: str, enabled: bool = True,
              interval_s: int = DEFAULT_INTERVAL_S,
              execute_enabled: bool | None = None) -> dict:
    repos.load(repo)  # unknown repo -> SystemExit
    with locked(repo):
        st = load(repo)
        st["enabled"] = bool(enabled)
        st["interval_s"] = max(60, int(interval_s))
        if execute_enabled is not None:
            st["execute_enabled"] = bool(execute_enabled)
        if st["enabled"] and not st.get("next_scan"):
            st["next_scan"] = utcnow()
        save(st)
    mode = "execute=on" if st["execute_enabled"] else "execute=off(read-only)"
    events.append(repo, "sched-config", "",
                  f"scheduler {'enabled' if enabled else 'paused'} ({mode})",
                  f"interval={st['interval_s']}s")
    return st


# ---- global one-worker lock: OS advisory lock held during execution ----
#
# The single worker identity is an flock(LOCK_EX) held open on
# `.worker.flock` for the whole execution. The OS releases it on crash,
# so a dead worker never blocks the next one. `.worker.lock` keeps
# owner/pid/heartbeat JSON for display only — it never decides
# ownership by itself (a just-created empty file is NOT stealable).
# Legacy TTL metadata is honored only as a compatibility refusal: a
# fresh heartbeat from a live pid written by a pre-flock release still
# refuses a new contender until it goes stale.

def _lock_file() -> Path:
    return paths.home() / "sched" / ".worker.lock"


def _flock_file() -> Path:
    return paths.home() / "sched" / ".worker.flock"


def _lock_owner() -> str:
    return f"pid-{os.getpid()}"


def _read_lock() -> dict | None:
    try:
        data = json.loads(_lock_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _lock_live(cur: dict | None, ttl_s: int = LOCK_TTL_S) -> bool:
    if not cur:
        return False
    try:
        stamp = float(cur.get("heartbeat", cur.get("at", 0)))
    except (TypeError, ValueError):
        return False
    return (time.time() - stamp) < ttl_s


def _pid_alive(pid) -> bool:
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return False
    if p <= 0:
        return False
    if p == os.getpid():
        return True
    try:
        os.kill(p, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _flock_try_hold():
    """Open the flock file and take LOCK_EX|LOCK_NB. Returns the fd on
    success, None when another process (or this process) holds it."""
    import fcntl
    fp = _flock_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(fp), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


def _flock_busy_by_other() -> bool:
    """True when some live holder owns the flock. Our own held fd counts
    as busy for a *second* acquire in this process (correct: one worker)."""
    import threading as _th
    with _HELD_GUARD:
        if _HELD_FDS:
            return True
    fd = _flock_try_hold()
    if fd is None:
        return True
    import fcntl as _fc
    try:
        _fc.flock(fd, _fc.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    return False


import threading as _threading

_HELD_FDS: dict = {}
_HELD_GUARD = _threading.Lock()
_HEARTBEATS: dict = {}
# Thread-local nesting of the worker lease. Only the thread that took the
# lease may re-enter it (scheduler -> CLI -> runner call stacks); a second
# thread in this process is a genuine contender and must refuse, exactly
# like a second process.
_THREAD_STATE = _threading.local()


def _thread_owners() -> list:
    owners = getattr(_THREAD_STATE, "owners", None)
    if owners is None:
        owners = []
        _THREAD_STATE.owners = owners
    return owners


def acquire_worker_lease(owner: str | None = None) -> str | None:
    """Take the single-worker OS lease for a whole actual execution.

    Same mutual exclusion as `_acquire_global_lock`, but reentrant along
    the owning thread's call stack: scheduler-owned execution that calls
    back into CLI resume/start (which re-acquire for the nested
    execution) reuses the held lease instead of self-deadlocking or
    double-acquiring. A different thread, like a different process, gets
    None while the lease is held. Returns the owner token, or None when
    another live holder owns it. No state or model side effects either
    way: the caller decides, the OS lease is the decision.
    """
    if _thread_owners():
        # Nested acquire on the owning thread: count the depth so an
        # inner release does not drop the OS lease while the outer
        # scope is still held. Only the outermost release frees it
        # (see release_worker_lease).
        _thread_owners().append(_thread_owners()[-1])
        return _thread_owners()[-1]
    token = _acquire_global_lock(owner=owner)
    if token is not None:
        _thread_owners().append(token)
    return token


def disown_worker_lease(owner: str) -> None:
    """Hand an acquired lease to another thread without releasing it.

    Removes the owner from THIS thread's reentrancy stack only: the OS
    lock, heartbeat registration, and metadata stay held for the
    receiving thread, which must later release via release_worker_lease
    (that path keys off the global fd table, not thread state, so it is
    safe cross-thread). Without this handoff the acquiring thread keeps
    a stale reentrant entry and a later same-thread acquire would
    falsely bypass a live holder. Never raises.
    """
    try:
        owners = _thread_owners()
        while owner in owners:
            owners.remove(owner)
    except Exception:  # noqa: BLE001 - handoff bookkeeping never fails
        pass


def release_worker_lease(owner: str) -> None:
    """Release one nesting level of the worker lease.

    The OS lease (and its heartbeat) drops only when the outermost level
    releases, so nested scheduler -> CLI -> runner execution unwinds
    safely without stranding or early release.
    """
    owners = _thread_owners()
    if owners and owners[-1] == owner:
        owners.pop()
    elif owner in owners:
        owners.remove(owner)
    if owner in owners:
        return
    _release_global_lock(owner)


def worker_lease_held() -> bool:
    """True when this thread currently owns the worker lease."""
    return bool(_thread_owners())


def _write_lock_meta(owner: str) -> None:
    lf = _lock_file()
    now = time.time()
    payload = json.dumps({"owner": owner, "pid": os.getpid(),
                          "at": now, "heartbeat": now})
    tmp = lf.parent / f".lock.tmp.{os.getpid()}"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, lf)


def _acquire_global_lock(owner: str | None = None,
                         ttl_s: int = LOCK_TTL_S) -> str | None:
    """Take the one-worker lock, held until `_release_global_lock`.

    Returns the owner token, or None when another live worker holds it.
    Two racing contenders cannot both win: the OS serializes flock, and
    a freshly created (possibly still empty) metadata file is never
    treated as stale/corrupt while its holder is alive."""
    owner = owner or _lock_owner()
    fd = _flock_try_hold()
    if fd is None:
        return None
    # Compatibility: a pre-flock release may have left a fresh lease file
    # while still running elsewhere (no flock fd on our side to prove
    # death). Refuse to steal a fresh heartbeat from a live pid.
    try:
        cur = _read_lock()
    except Exception:  # noqa: BLE001
        cur = None
    if cur and cur.get("owner") not in (None, owner):
        if _lock_live(cur, ttl_s) and _pid_alive(cur.get("pid")):
            import fcntl as _fc
            try:
                _fc.flock(fd, _fc.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            return None
    try:
        _write_lock_meta(owner)
    except OSError:
        import fcntl as _fc
        try:
            _fc.flock(fd, _fc.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    with _HELD_GUARD:
        _HELD_FDS[owner] = fd
    return owner


def renew_global_lock(owner: str) -> bool:
    """Heartbeat: extend our own lease. False when we no longer own it."""
    with _HELD_GUARD:
        fd = _HELD_FDS.get(owner)
    if fd is None:
        return False
    lf = _lock_file()
    cur = _read_lock()
    if not cur or cur.get("owner") != owner:
        return False
    cur["heartbeat"] = time.time()
    cur["pid"] = os.getpid()
    tmp = lf.parent / f".lock.tmp.{os.getpid()}"
    try:
        tmp.write_text(json.dumps(cur), encoding="utf-8")
        os.replace(tmp, lf)
    except OSError:
        return False
    return True


def _release_global_lock(owner: str | None = None) -> None:
    """Release only our own lock (legacy ownerless locks: remove)."""
    _stop_heartbeat(owner)
    with _HELD_GUARD:
        owned = dict(_HELD_FDS)
    if owner is None:
        _thread_owners().clear()
        import fcntl as _fc
        for own, fd in owned.items():
            try:
                _fc.flock(fd, _fc.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            with _HELD_GUARD:
                _HELD_FDS.pop(own, None)
        try:
            _lock_file().unlink()
        except OSError:
            pass
        return
    with _HELD_GUARD:
        fd = _HELD_FDS.pop(owner, None)
    if fd is not None:
        import fcntl as _fc
        try:
            _fc.flock(fd, _fc.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        cur = _read_lock()
        if cur is None or cur.get("owner") in (None, owner):
            try:
                _lock_file().unlink()
            except OSError:
                pass
        return
    # No flock fd (legacy path): only remove our own metadata.
    cur = _read_lock()
    if cur is None or cur.get("owner") in (None, owner):
        try:
            _lock_file().unlink()
        except OSError:
            pass


def _lock_held_recently() -> bool:
    if _flock_busy_by_other():
        return True
    # Legacy lease without a live flock holder: honor a fresh heartbeat
    # from a live pid so a rolling upgrade cannot steal a live worker,
    # but a stale/crashed lease never blocks (flock already proved free).
    cur = _read_lock()
    return bool(cur and _lock_live(cur) and _pid_alive(cur.get("pid")))


def global_lock_status() -> dict:
    """Display-only worker identity: owner/pid/heartbeat + live probe."""
    cur = _read_lock()
    live = _flock_busy_by_other() or bool(
        cur and _lock_live(cur) and _pid_alive(cur.get("pid")))
    out = dict(cur or {})
    out["live"] = live
    return out


def _start_heartbeat(owner: str, interval_s: float = 15.0) -> None:
    """Renew our heartbeat for the whole execution. Called by run_due."""
    with _HELD_GUARD:
        if owner not in _HELD_FDS or owner in _HEARTBEATS:
            return
        stop = _threading.Event()
        _HEARTBEATS[owner] = stop

    def _loop():
        while not stop.wait(interval_s):
            try:
                if not renew_global_lock(owner):
                    break
            except Exception:  # noqa: BLE001 - heartbeat never kills work
                break

    th = _threading.Thread(target=_loop, daemon=True,
                           name=f"openmainty-heartbeat-{owner}")
    th.start()


def _stop_heartbeat(owner: str | None) -> None:
    if owner is None:
        owners = list(_HEARTBEATS)
        for own in owners:
            _stop_heartbeat(own)
        return
    stop = None
    with _HELD_GUARD:
        stop = _HEARTBEATS.pop(owner, None)
    if stop is not None:
        stop.set()


# ---- dedup keys ----

def dedup_key(item: dict) -> str:
    if item.get("kind") == "issue-triage":
        return f"{item.get('repo')}#issue-{item.get('issue')}"
    return (f"{item.get('repo')}+{item.get('dep')}+"
            f"{item.get('target') or item.get('lane')}+{item.get('channel') or 'stable'}")


def couple_key(item: dict) -> str:
    """Same dependency, any manifest context: one job, not three."""
    if item.get("kind") != "dep-update":
        return item.get("key") or dedup_key(item)
    return f"{item.get('repo')}+{item.get('dep')}+{item.get('channel') or 'stable'}"


def _known_keys(st: dict) -> set:
    """Every key still owned by the lifecycle or finished in history."""
    known = set()
    for item in st.get("queue", []):
        if item.get("status") in LIVE_STATUSES + ("cancelled",):
            if item.get("key"):
                known.add(item["key"])
        ck = couple_key(item)
        if item.get("status") in LIVE_STATUSES and item.get("kind") == "dep-update":
            known.add("couple:" + ck)
    for hist in st.get("history", []):
        if hist.get("key"):
            known.add(hist["key"])
    return known


def _same_release(a: dict, b: dict) -> bool:
    """Same releasable job: identical target, lane, branch, and channel.

    Same dep scanned from another manifest context (scopes/file) folds
    together only here. A different version is a different candidate and
    must never merge into a run-bound row.
    """
    return (a.get("target") == b.get("target")
            and a.get("lane") == b.get("lane")
            and a.get("branch") == b.get("branch")
            and (a.get("channel") or "stable") == (b.get("channel") or "stable"))


def _is_bound(row: dict) -> bool:
    """A row owned by a run lifecycle: proof binds its identity."""
    return bool(row.get("run_id") or row.get("verified")
                or row.get("status") in ("waiting-review", "running"))


def _lane_branch(dep: str, target: str, lane: str) -> str:
    """Deterministic branch for a lane, mirroring scan construction."""
    from . import branches as _b
    slug = _b.slugify_topic(f"{dep}-{target}")
    return f"codex/{'maint' if lane == 'main' else 'next'}/{slug}"


def _resolve_lane(old: str, new: str) -> str:
    """Conservative, order-independent lane for unbound rows.

    Next is the cautious lane, so it wins whenever any scanned context
    says next, regardless of manifest order. Neither context alone may
    downgrade a next row back to main.
    """
    if "next" in (old, new):
        return "next"
    return new or old or "main"


def _record_lane_conflict(existing: dict, item: dict) -> None:
    """Honest blocked conflict: the scan disagrees with bound proof.

    The bound row keeps its run binding, target, lane, branch, and key;
    only its status moves to blocked with a reason and re-review as the
    next action. Old proof is never grafted onto the other lane, and no
    review-complete is pretended.
    """
    existing["status"] = "blocked"
    existing["blocker"] = (
        f"lane conflict: bound lane {existing.get('lane')} vs scanned "
        f"lane {item.get('lane')} for {existing.get('dep')} "
        f"{existing.get('target')}; run proof not transferable; "
        f"re-review required")


def _fresh_candidate(item: dict) -> dict:
    """A newer release starts life with no inherited run binding.

    The bound row keeps its run_id/run_issue/verified/status; the new row
    is plain queued work that earns its own run later.
    """
    fresh = dict(item)
    fresh["status"] = "queued"
    fresh["attempts"] = 0
    fresh["next_retry_at"] = None
    fresh.pop("run_id", None)
    fresh.pop("run_issue", None)
    fresh.pop("verified", None)
    fresh["key"] = fresh.get("key") or dedup_key(fresh)
    return fresh


def _enqueue(st: dict, item: dict) -> bool:
    """True when newly added. Any key owned by the live lifecycle,
    cancelled set, or finished history is refreshed in place, never
    duplicated (waiting-review included). Same release across manifest
    contexts coalesces into one job; scan evidence is retained. A newer
    (or otherwise different) release never retargets a run-bound row:
    it queues as a fresh candidate instead."""
    key = item.get("key") or dedup_key(item)
    item["key"] = key
    known = _known_keys(st)
    if key in known:
        for existing in st["queue"]:
            if existing.get("key") == key:
                if (existing.get("kind") == "dep-update"
                        and existing.get("lane") != item.get("lane")
                        and _is_bound(existing)):
                    _record_lane_conflict(existing, item)
                else:
                    _refresh_same_key(existing, item)
                save(st)
                return False
        # Owned by history/cancelled only: keep history, do not requeue.
        return False
    if item.get("kind") == "dep-update" and ("couple:" + couple_key(item)) in known:
        for existing in st["queue"]:
            if (existing.get("kind") == "dep-update"
                    and couple_key(existing) == couple_key(item)
                    and existing.get("status") in LIVE_STATUSES
                    and _same_release(existing, item)):
                _coalesce(existing, item)
                save(st)
                return False
        # Same dep, different release: the bound row stays exactly as the
        # run proved it; the new release queues fresh below.
        st["queue"].append(_fresh_candidate(item))
        save(st)
        return True
    st["queue"].append(item)
    save(st)
    return True


def _refresh_same_key(existing: dict, item: dict) -> None:
    """Merge scan evidence, never run-bound identity.

    target/key bind the row to the run that proved it (and to recorded
    batch gates); status/attempts/created_at/run_id/run_issue/verified
    belong to that lifecycle. Refreshing them from a later scan is how
    21.2.3 rows became fake 21.2.4 rows. Lane resolves conservatively
    (next wins, either order) on unbound rows only, and the branch is
    recomputed to stay consistent with the resolved lane; bound rows keep
    both untouched (lane conflicts are recorded, not merged).
    """
    # Lane normalization is strictly a dep-update concern: other row kinds
    # (issue triage, questions) carry no lane, and inventing one plus a
    # slug branch would corrupt them. They keep legacy refresh semantics.
    if existing.get("kind") == "dep-update":
        lane = _resolve_lane(existing.get("lane"), item.get("lane"))
        if lane != existing.get("lane") and not _is_bound(existing):
            existing["lane"] = lane
            existing["branch"] = _lane_branch(existing.get("dep"),
                                              existing.get("target"), lane)
        elif not _is_bound(existing):
            want = _lane_branch(existing.get("dep"), existing.get("target"),
                                existing.get("lane"))
            if existing.get("branch") != want:
                existing["branch"] = want
    old_scopes = set(existing.get("scopes") or [])
    for k, v in item.items():
        if k not in ("status", "attempts", "created_at", "run_id",
                     "run_issue", "verified", "key", "target", "lane",
                     "branch", "scopes"):
            existing[k] = v
    scopes = set(old_scopes)
    for s in item.get("scopes") or []:
        scopes.add(s)
    if scopes:
        existing["scopes"] = sorted(scopes)


def _refresh(existing: dict, item: dict) -> None:
    """Legacy entry point: same-key evidence merge (see _refresh_same_key)."""
    _refresh_same_key(existing, item)


def _coalesce(existing: dict, item: dict) -> None:
    """Fold one more manifest context into the same release job.

    Callers guarantee _same_release; no version bump happens here anymore.
    """
    _refresh_same_key(existing, item)


def _queue_fingerprint(st: dict) -> str:
    import hashlib
    blob = json.dumps({"queue": st.get("queue", []),
                       "history": st.get("history", [])},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _binding_repair_plan(repo: str, st: dict) -> dict:
    """Detect run-bound dep-update rows drifted from their immutable record.

    Generic: no dep or version is named. For each live dep-update row
    carrying a run_id, the run record must completely and observably own
    the binding: same repo, same dep, and present dep/target/lane/branch/
    issue (plus queue_key or a computable key). Only then is drift
    planned for restoration, and only a strictly newer recorded release
    is owed a fresh, run-free queued candidate. Anything else —
    non-dep rows, unreadable/foreign/incomplete records, dep mismatches,
    restored-key collisions, non-newer "newer" targets — is reported
    skipped/blocked with a reason, never touched, never relabeled.
    """
    from . import store as _store
    from . import versions as _v
    repairs, enqueues, skipped, issue_repairs = [], [], [], []
    live = [i for i in st.get("queue", [])
            if i.get("status") in LIVE_STATUSES]
    live_keys = {i.get("key") for i in live}
    hist_keys = {h.get("key") for h in st.get("history", []) if h.get("key")}
    planned_new = set()
    for row in st.get("queue", []):
        if (row.get("kind") == "issue-triage"
                and not row.get("run_id") and not row.get("verified")
                and row.get("status") == "queued"
                and row.get("dep") is None and row.get("target") is None
                and row.get("lane") == "main"
                and row.get("branch") == "codex/maint/none-none"):
            # Exact injected signature from the generic refresh regression:
            # unbound issue rows gained a lane plus a slug branch built
            # from missing dep/target. Normal issue state carries neither,
            # so both keys go away (not relabeled, not revalued).
            issue_repairs.append({
                "row_key": row.get("key"),
                "from": {"lane": "main",
                         "branch": "codex/maint/none-none"}})
            continue
        rid = row.get("run_id")
        if not rid:
            continue
        if row.get("kind") != "dep-update":
            skipped.append({"key": row.get("key"), "run_id": rid,
                            "reason": "non-dep-update row with run_id; "
                                      "not eligible, left alone"})
            continue
        try:
            run = json.loads(_store.run_file(rid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append({"key": row.get("key"), "run_id": rid,
                            "reason": "run record unreadable; left alone"})
            continue
        if run.get("repo") != repo or run.get("dep") != row.get("dep"):
            skipped.append({"key": row.get("key"), "run_id": rid,
                            "reason": "run record is foreign "
                                      f"(repo={run.get('repo')} "
                                      f"dep={run.get('dep')}); left alone"})
            continue
        if not (run.get("dep") and run.get("target") and run.get("lane")
                and run.get("branch") is not None and run.get("issue")
                and (run.get("queue_key") or row.get("channel") is not None)):
            skipped.append({"key": row.get("key"), "run_id": rid,
                            "reason": "run record identity incomplete "
                                      "(dep/target/lane/branch/issue/key "
                                      "required); left alone"})
            continue
        exp = {"dep": run.get("dep"), "target": run.get("target"),
               "lane": run.get("lane"), "branch": run.get("branch"),
               "run_issue": run.get("issue")}
        exp["key"] = run.get("queue_key") or dedup_key(
            {"repo": repo, "dep": exp["dep"], "target": exp["target"],
             "lane": exp["lane"],
             "channel": row.get("channel") or "stable"})
        drift = sorted({f for f in ("dep", "target", "lane", "branch",
                                    "run_issue") if row.get(f) != exp[f]}
                       | ({"key"} if row.get("key") != exp["key"] else set()))
        if not drift:
            continue
        if any(r.get("key") == exp["key"] and r is not row for r in live):
            skipped.append({"key": row.get("key"), "run_id": rid,
                            "reason": "restored key "
                                      f"{exp['key']} already live on another "
                                      f"row; ambiguous duplicate, left alone"})
            continue
        # Version-scoped evidence comes from the immutable run where
        # available; the reason becomes a plain repair note naming the old
        # subject and the separate new candidate — never the retained
        # newer-version reason, never invented history. The corrupt why
        # stays in the audit as from.why.
        to = {"dep": exp["dep"], "target": exp["target"],
              "lane": exp["lane"], "branch": exp["branch"],
              "key": exp["key"], "run_issue": exp["run_issue"]}
        audited = ["dep", "target", "lane", "branch", "key", "run_issue"]
        for f in ("scopes", "update_kind", "base_branch"):
            if run.get(f) is not None:
                to[f] = run.get(f)
                audited.append(f)
        # Human reason stays brief Korean on the old subject only: the
        # run id, queue key, and full before/after live in the audit, and
        # the newer candidate (when one is owed) carries its own scan
        # reason — so no unconditional "continues queued" claim here.
        to["why"] = (f"실행 기록 기준 바인딩 복원: "
                     f"{exp['dep']} {exp['target']}")
        audited.append("why")
        repairs.append({
            "row_key": row.get("key"), "run_id": rid, "drift": drift,
            "from": {f: row.get(f) for f in audited},
            "to": to})
        if row.get("key") == exp["key"]:
            continue
        try:
            newer_ahead = _v.compare(str(row.get("target") or ""),
                                     str(exp["target"] or "")) > 0
        except Exception:  # noqa: BLE001 - unparsable is not "newer"
            newer_ahead = False
        if not newer_ahead:
            skipped.append({"key": row.get("key"),
                            "reason": "recorded release "
                                      f"{row.get('target')} is not newer than "
                                      f"restored {exp['target']}; no owed "
                                      f"candidate"})
            continue
        newer = {"kind": "dep-update", "repo": repo,
                 "dep": row.get("dep"), "target": row.get("target"),
                 "channel": row.get("channel") or "stable",
                 "lane": row.get("lane"), "branch": row.get("branch"),
                 "update_kind": row.get("update_kind"),
                 "scopes": row.get("scopes") or [],
                 "file": row.get("file"), "why": row.get("why") or "",
                 "declared": row.get("declared"),
                 "installed": row.get("installed"),
                 "compared": row.get("compared"),
                 "key": row.get("key")}
        if (newer["key"] in (live_keys - {row.get("key")})
                or newer["key"] in hist_keys
                or newer["key"] in planned_new):
            skipped.append({"key": newer["key"],
                            "reason": "newer key already live/historic/planned"})
            continue
        planned_new.add(newer["key"])
        enqueues.append(newer)
    return {"repairs": repairs, "enqueues": enqueues, "skipped": skipped,
            "issue_repairs": issue_repairs}


def repair_version_bindings(repo: str, apply: bool = False,
                            fingerprint: str | None = None) -> dict:
    """Restore run-bound queue identities corrupted by cross-version merges.

    Dry-run by default (read-only plan + state fingerprint for CAS).
    Apply requires the dry-run fingerprint, runs under the existing
    per-repo lock, re-validates every planned from-value plus restored-key
    uniqueness, then commits restores + owed candidates + an audit record
    in ONE atomic save. Any validation failure aborts with nothing
    written. Run files, worktrees, proofs, and batch history are never
    written here.
    """
    if not apply:
        st = load(repo)
        plan = _binding_repair_plan(repo, st)
        return {"ok": True, "applied": False, "repo": repo,
                "fingerprint": _queue_fingerprint(st),
                "repairs": plan["repairs"], "enqueues": plan["enqueues"],
                "skipped": plan["skipped"],
                "issue_repairs": plan["issue_repairs"],
                "note": "dry-run only; re-run with --apply under the repo lock"}
    if not fingerprint:
        return {"ok": False, "applied": False, "repo": repo,
                "reason": "CAS fingerprint required: dry-run first, then "
                          "--apply with that fingerprint"}
    with locked(repo):
        st = load(repo)
        if _queue_fingerprint(st) != fingerprint:
            return {"ok": False, "applied": False, "repo": repo,
                    "reason": "state moved since dry-run; re-plan before applying"}
        plan = _binding_repair_plan(repo, st)
        if plan["skipped"] and any("already live" in s.get("reason", "")
                                   or "ambiguous" in s.get("reason", "")
                                   for s in plan["skipped"]):
            return {"ok": False, "applied": False, "repo": repo,
                    "reason": "ambiguous duplicate keys; resolve by hand, "
                              "then re-plan"}
        if (not plan["repairs"] and not plan["enqueues"]
                and not plan["issue_repairs"]):
            # True no-op: no save, no audit overwrite. Idempotent applies
            # keep the real repair record intact.
            return {"ok": True, "applied": False, "repo": repo,
                    "reason": "nothing to repair; state untouched",
                    "skipped": plan["skipped"]}
        restored, queued, issue_fixed = [], [], []
        for rep in plan["repairs"]:
            hit = [r for r in st.get("queue", [])
                   if r.get("key") == rep["row_key"]]
            if not hit:
                return {"ok": False, "applied": False, "repo": repo,
                        "reason": f"row {rep['row_key']} vanished; re-plan"}
            row = hit[0]
            for f, v in rep["from"].items():
                if row.get(f) != v:
                    return {"ok": False, "applied": False, "repo": repo,
                            "reason": f"row {rep['row_key']} field {f} moved; "
                                      f"re-plan"}
            if any(r.get("key") == rep["to"]["key"] and r is not row
                   for r in st.get("queue", [])
                   if r.get("status") in LIVE_STATUSES):
                return {"ok": False, "applied": False, "repo": repo,
                        "reason": f"restored key {rep['to']['key']} collides "
                                  f"with a live row; re-plan"}
            row.update(rep["to"])
            restored.append(rep["row_key"])
        for cand in plan["enqueues"]:
            fresh = _fresh_candidate({
                **cand, "status": "queued", "attempts": 0,
                "next_retry_at": None, "run_id": None, "run_issue": None,
                "verified": None, "created_at": utcnow()})
            dup = [r for r in st.get("queue", [])
                   if r.get("key") == fresh["key"]
                   and r.get("status") in LIVE_STATUSES]
            hist = [h for h in st.get("history", [])
                    if h.get("key") == fresh["key"]]
            if dup or hist:
                return {"ok": False, "applied": False, "repo": repo,
                        "reason": f"owed key {fresh['key']} taken during "
                                  f"apply; re-plan"}
            st["queue"].append(fresh)
            queued.append(fresh["key"])
        for fix in plan["issue_repairs"]:
            hit = [r for r in st.get("queue", [])
                   if r.get("key") == fix["row_key"]]
            if not hit:
                return {"ok": False, "applied": False, "repo": repo,
                        "reason": f"row {fix['row_key']} vanished; re-plan"}
            row = hit[0]
            for f, v in fix["from"].items():
                if row.get(f) != v:
                    return {"ok": False, "applied": False, "repo": repo,
                            "reason": f"row {fix['row_key']} field {f} moved; "
                                      f"re-plan"}
            for f in fix["from"]:
                row.pop(f, None)
            issue_fixed.append(fix["row_key"])
        # Append-only product audit (separate from lifecycle history, no
        # dedup keys): the actual before/after plan, including the corrupt
        # from.why. Committed in the SAME atomic save as the repair.
        at = utcnow()
        hist = st.get("binding_repair_history") or []
        hist.append({"at": at, "fingerprint": fingerprint,
                     "restored": restored, "queued": queued,
                     "issue_fixed": issue_fixed,
                     "repairs": plan["repairs"],
                     "enqueues": plan["enqueues"],
                     "issue_repairs": plan["issue_repairs"],
                     "skipped": plan["skipped"]})
        st["binding_repair_history"] = hist
        st["last_binding_repair"] = {
            "at": at, "history_ref": len(hist) - 1,
            "restored": restored, "queued": queued,
            "issue_fixed": issue_fixed, "fingerprint": fingerprint}
        save(st)
        return {"ok": True, "applied": True, "repo": repo,
                "restored": restored, "queued": queued,
                "issue_fixed": issue_fixed,
                "skipped": plan["skipped"]}


# ---- scan ----

def _recover_interrupted(st: dict, repo: str) -> None:
    """Restart recovery: the SAME item (fields + run id + attempts) goes
    back to queued. No generic placeholder items."""
    job = st.get("current_job")
    if not job:
        return
    if _lock_held_recently():
        st["stale"] = True
        return
    st.pop("current_job", None)
    snap = dict(job.get("item") or {})
    if not snap.get("key"):
        return
    snap["status"] = "queued"
    snap["attempts"] = snap.get("attempts", 0)
    snap["note"] = "worker stopped mid-job; recovered with same run identity"
    found = False
    for existing in st["queue"]:
        if existing.get("key") == snap["key"]:
            existing.update(snap)
            found = True
    if not found:
        st["queue"].append(snap)
    st["stale"] = True
    events.append(repo, "stale-recovered", str(snap.get("key", "")),
                  "interrupted job re-queued with same identity",
                  f"run={job.get('run_id') or '-'} attempts={snap.get('attempts', 0)}")


def scan(repo: str, issue_limit: int = 20, tick_start: str = None) -> dict:
    """Read-only scan: drift + issues -> durable queue. Never spends quota."""
    from . import pcconfig
    profile = repos.load(repo)
    with locked(repo):
        st = load(repo)
        _recover_interrupted(st, repo)
        # Invalidate only the new branch observation at scan start:
        # an interrupted/in-progress scan must not leave a stale
        # known value. (The scan path sets no current_job/stale
        # flags of its own, so only this invalidation protects the
        # observation.) No scheduler flags change here.
        st["branch_next"] = None
        st.pop("branch_next_at", None)
        st.pop("branch_next_checkout", None)
        save(st)
    checkout = pcconfig.checkout_for(repo)
    rows: list = []
    scan_error = None
    sources: dict = {"manifest": "ok"}
    if checkout and Path(checkout).is_dir():
        try:
            rows = deps.check(profile, Path(checkout))
        except Exception as exc:  # noqa: BLE001 - scan must not raise
            scan_error = str(exc)
            sources["manifest"] = f"error: {exc}"[:200]
    else:
        scan_error = "checkout not linked"
        sources["manifest"] = "error: checkout not linked"
    row_errors = [r for r in rows if r.get("error")]
    if row_errors:
        sources["registry"] = f"{len(row_errors)} row error(s), kept as error rows"
    else:
        sources["registry"] = "ok"
    added, updated = 0, 0
    with locked(repo):
        st = load(repo)
        for row in rows:
            # Drift rows queue as upgrades; outdated lock-refresh rows queue
            # as lock refreshes (declared range already admits latest but the
            # lock is behind). Legacy drift semantics are preserved: drift
            # stays False on those rows, `outdated` carries the action.
            if not (row.get("drift") or row.get("outdated")):
                continue
            if row.get("update_kind") in ("unknown", "unmanaged", "sdk", "note"):
                continue
            scopes = [row.get("scope") or "dependencies"]
            item = {"kind": "dep-update", "repo": repo, "dep": row["name"],
                    "declared": row.get("declared"), "installed": row.get("installed"),
                    "compared": row.get("compared") or row.get("installed"),
                    "target": row.get("latest"), "channel": "stable",
                    "lane": row.get("lane", "next"), "branch": row.get("branch"),
                    "update_kind": row.get("update_kind"),
                    "scopes": scopes, "file": row.get("file"),
                    "why": row.get("reason", ""), "status": "queued",
                    "attempts": 0, "next_retry_at": None, "run_id": None,
                    "created_at": utcnow()}
            item["key"] = dedup_key(item)
            if _enqueue(st, item):
                added += 1
            else:
                updated += 1
        # Upstream issues: refresh, persist, and queue triage for new ones.
        # Failures keep last known data and stay observable (never healthy).
        issues: list = []
        issues_error = None
        try:
            issues = cli._open_issues(profile.get("github", ""), limit=issue_limit)
            sources["issues"] = f"ok ({len(issues)} open)"
        except Exception as exc:  # noqa: BLE001 - gh may be absent/offline
            issues_error = f"{type(exc).__name__}: {exc}"[:200]
            sources["issues"] = f"error: {issues_error}"
            issues = []
        except SystemExit as exc:
            issues_error = str(exc)[:200]
            sources["issues"] = f"error: {issues_error}"
            issues = []
        # Open PRs (read-only, like issues): refresh, persist, and
        # defer triage for issues an external PR already closes.
        # Failures keep last known data and stay observable. This
        # runs BEFORE the triage-queueing loop below so deferred
        # issues never queue.
        from openmainty import prs as _prs
        fresh_prs: list = []
        prs_error = None
        try:
            fresh_prs = [ _prs.sanitize_pr(r)
                for r in cli._open_prs(profile.get("github", ""),
                                       limit=30) ]
            sources["prs"] = f"ok ({len(fresh_prs)} open)"
        except Exception as exc:  # noqa: BLE001 - gh may be absent/offline
            prs_error = f"{type(exc).__name__}: {exc}"[:200]
            sources["prs"] = f"error: {prs_error}"
            fresh_prs = []
        except SystemExit as exc:
            prs_error = str(exc)[:200]
            sources["prs"] = f"error: {prs_error}"
            fresh_prs = []
        if not prs_error:
            st["last_prs"] = fresh_prs[:30]
            st["prs_error"] = None
        else:
            st["prs_error"] = prs_error
        # M3.5-2b: an issue with no OpenMainty run that an external
        # PR closes is NOT queued for triage — the board row shows
        # the PR instead. A skip reason, never a block; render reads
        # the same last_prs deterministically.
        pr_closers = _prs.closing_map(st.get("last_prs") or [])
        pr_deferred = 0
        if not issues_error and not prs_error:
            kept = []
            for issue in issues:
                number = str(issue.get("number"))
                if number.isdigit() and int(number) in pr_closers \
                        and store.latest_run(repo, number) is None:
                    pr_deferred += 1
                    continue
                kept.append(issue)
            if pr_deferred:
                sources["issues"] = (f"ok ({len(issues)} open, "
                                     f"{pr_deferred} deferred to "
                                     f"external PRs)")
            issues = kept
        if not issues_error:
            st["last_issues"] = [{"number": i.get("number"), "title": i.get("title", ""),
                                  "fetched_at": utcnow()} for i in issues]
            st["issues_error"] = None
            for issue in issues:
                number = str(issue.get("number"))
                prev = store.latest_run(repo, number)
                if prev is not None and _external_watch_run(prev):
                    continue  # deferred external wait: only the watch
                    # check below re-arms it, never a fresh queue entry
                if prev is not None and _terminal_no_code(prev):
                    continue  # confirmed duplicate: terminal, never re-queued
                if prev is not None and _run_finished(prev) and not prev.get("waitReason"):
                    continue  # done already; no spam
                if prev is not None and _run_active(prev):
                    continue  # a live run owns it
                item = {"kind": "issue-triage", "repo": repo, "issue": number,
                        "title": issue.get("title", ""), "status": "queued",
                        "attempts": 0, "next_retry_at": None, "run_id": None,
                        "why": f"upstream open issue #{number}",
                        "created_at": utcnow()}
                item["key"] = dedup_key(item)
                if _enqueue(st, item):
                    added += 1
                else:
                    updated += 1
        else:
            st["issues_error"] = issues_error
        # Queue/issue updates are durable now. The watch reconciliation
        # below takes locked(repo) itself, so it MUST run outside this
        # mutex: POSIX flock on a second fd self-blocks even in the same
        # process (two .<repo>.mtx fds = wedged scan). The final
        # bookkeeping re-loads fresh and touches only scalar scan fields,
        # never the queue the watch step just settled.
        save(st)
    # Deferred external waits: cheaply re-compare watched upstream
    # inputs (zero model calls). Unchanged stays deferred; real change
    # lifts the same run; failed evidence stays deferred. Never fatal.
    # Deliberately outside any locked(repo) scope (see above).
    try:
        watch_report = check_repo_watches(repo, rows, issues)
    except Exception as exc:  # noqa: BLE001 - scan must not raise
        watch_report = {"checked": 0, "changed": [], "failed": [str(exc)[:200]]}
    # Discussions (read-only, dedicated namespace): refresh the list
    # snapshot and label census on the same cadence. This NEVER queues
    # anything (no discussion-triage queue kind exists) and never spends
    # model quota. Deliberately outside any locked(repo) scope: the
    # namespace is separate files and network reads must not wedge it.
    from openmainty import discussions as _disc
    disc_open = -1
    disc_error = None
    disc_disabled = False
    disc_labels_error = None
    try:
        slug = (profile.get("github") or "").strip()
        if not slug:
            sources["discussions"] = "skipped: repo has no github slug"
        else:
            try:
                scan_res = _disc.scan_source(slug)
                disc_open = len(scan_res.get("items") or [])
                status = (scan_res.get("status") or {})
                if status.get("partial"):
                    sources["discussions"] = (
                        f"ok ({disc_open} tracked, partial backfill "
                        f"continues next pass)")
                else:
                    sources["discussions"] = f"ok ({disc_open} tracked)"
            except _disc.DiscussionError as exc:
                if str(exc) == "disabled":
                    disc_disabled = True
                    sources["discussions"] = (
                        "disabled: repo has no Discussions enabled")
                else:
                    disc_error = f"DiscussionError: {exc}"[:200]
                    sources["discussions"] = f"error: {disc_error}"
            except Exception as exc:  # noqa: BLE001 - gh may be absent/offline
                disc_error = f"{type(exc).__name__}: {exc}"[:200]
                sources["discussions"] = f"error: {disc_error}"
            except SystemExit as exc:
                disc_error = str(exc)[:200]
                sources["discussions"] = f"error: {disc_error}"
            try:
                census = _disc.fetch_labels(slug)
                sources["discussion_labels"] = (
                    f"ok ({len(census.get('labels') or [])} labels, "
                    f"{census.get('pages', 0)} page(s), "
                    f"complete={bool(census.get('labels_complete'))})")
            except Exception as exc:  # noqa: BLE001 - gh may be absent/offline
                disc_labels_error = f"{type(exc).__name__}: {exc}"[:200]
                sources["discussion_labels"] = f"error: {disc_labels_error}"
            except SystemExit as exc:
                disc_labels_error = str(exc)[:200]
                sources["discussion_labels"] = f"error: {disc_labels_error}"
    except Exception as exc:  # noqa: BLE001 - scan must not raise
        sources["discussions"] = f"error: {type(exc).__name__}: {exc}"[:200]
    now = utcnow()
    slim = []
    for r in rows:
        if r.get("kind") in ("note",):
            continue
        slim.append({k: r.get(k) for k in
                     ("name", "kind", "id", "declared", "installed", "pinned",
                      "compared", "latest", "drift", "outdated", "error",
                      "unpublished",
                      "update_kind", "lane", "branch", "reason", "checked_at",
                      "source", "scope", "file", "note",
                      "lock_file", "lock_tracked")})
    # Display-only branch observation for the overview: True/False
    # only from a successful probe of the current checkout, None on
    # any failure. Computed once here so every scan terminal below
    # stores a coherent value; never action or identity authority.
    try:
        from . import branches as _branches
        branch_state = _branches.probe_next(checkout)
    except Exception:
        branch_state = None
    with locked(repo):
        # Fresh load: check_repo_watches above settled queue/watch state
        # under its own lock; merging those changes is required, so only
        # scalar scan bookkeeping lands here — the queue is never restored
        # from a stale snapshot.
        st = load(repo)
        st["last_scan"] = now
        st["heartbeat"] = now
        st["last_rows"] = slim[:200]
        # Bound atomically with this scan's scalars: legacy states
        # lack these keys and read as unknown. Only a scan whose own
        # outcome is successful persists a True/False observation;
        # any other terminal stores None with this scan's stamps so
        # a prior True cannot survive a failure. Display-only data,
        # never source or admission identity.
        scan_ok_final = (scan_error is None and not row_errors
                         and issues_error is None
                         and prs_error is None
                         and disc_error is None)
        st["branch_next"] = branch_state if scan_ok_final else None
        st["branch_next_at"] = now
        st["branch_next_checkout"] = checkout or None
        # Anchor the next scan to the launching tick start (not to this
        # scan's completion) so the cadence tracks ticks instead of
        # drifting a few seconds per cycle. Direct (non-tick) scans keep
        # the old completion-anchored behavior.
        st["next_scan"] = _anchor_next(
            tick_start, now, st.get("interval_s", DEFAULT_INTERVAL_S))
        st["error"] = scan_error
        st["sources"] = sources
        st["scan_ok"] = scan_ok_final
        st["stale"] = bool(st.get("current_job"))
        save(st)
    drifted = [r for r in rows if r.get("drift")]
    outdated = [r for r in rows if r.get("outdated") and not r.get("drift")]
    summary = deps.summarize(rows)
    events.append(repo, "scan", "",
                  f"scan: {summary['checked']} deps, {len(drifted)} drift, "
                  f"{len(outdated)} lock-refresh "
                  f"(+{added} queued, {updated} refreshed)",
                  "; ".join(f"{r['name']}->{r.get('latest')}"
                            for r in drifted + outdated)[:1500])
    if summary["unsupported"] and scan_error is None:
        scan_error = ("no supported manifest found; checkout unscannable "
                      "(see warnings)")
        sources["manifest"] = "unavailable: no package.json/pubspec.yaml"
        with locked(repo):
            st = load(repo)
            st["error"] = scan_error
            st["scan_ok"] = False
            st["sources"] = sources
            # The checkout proved unscannable at the manifest layer:
            # invalidate any known branch observation atomically here.
            st["branch_next"] = None
            st["branch_next_at"] = now
            st["branch_next_checkout"] = checkout or None
            save(st)
    return {"repo": repo, "checked": summary["checked"],
            "warnings": summary["warnings"],
            "warning_notes": summary["warning_notes"],
            "registry_errors": summary["errors"],
            "drifted": len(drifted),
            "queued_added": added, "queued_existing": updated,
            "issues_open": len(st["last_issues"]) if not issues_error else -1,
            "issues": issues, "issues_error": issues_error,
            "prs_open": len(st["last_prs"]) if not prs_error else -1,
            "prs": list(st.get("last_prs") or []), "prs_error": prs_error,
            "prs_deferred": pr_deferred,
            "discussions_open": disc_open, "discussions_error": disc_error,
            "discussions_disabled": disc_disabled,
            "discussion_labels_error": disc_labels_error,
            "last_scan": now, "next_scan": st["next_scan"],
            "error": scan_error,
            "scan_ok": False if summary["unsupported"] else st["scan_ok"],
            "sources": sources, "rows": rows, "watches": watch_report}


def _run_active(run: dict) -> bool:
    if run.get("waitReason"):
        return True
    return any(e.get("status") not in store.TERMINAL
               for e in (run.get("steps") or {}).values())


def _run_finished(run: dict) -> bool:
    steps = run.get("steps") or {}
    return bool(steps) and all(e.get("status") == "done" for e in steps.values())


def _terminal_no_code(run: dict) -> bool:
    """True when the run settled as a confirmed duplicate: terminal
    no-code disposition, never retried into reproduce/fix, never forked
    into a new run. Reads the durable disposition first (legacy
    waitReason text is the conservative fallback)."""
    from . import runner as _runner
    try:
        return _runner.resume_gate(run) == "duplicate"
    except Exception:  # noqa: BLE001 - a corrupt run never blocks dispatch
        return False


def _waiting_on_operator(run: dict) -> bool:
    """True when the run needs an operator answer before any more work."""
    from . import runner as _runner
    try:
        return _runner.resume_gate(run) == "waiting"
    except Exception:  # noqa: BLE001
        return False


def _external_watch_run(run: dict) -> bool:
    """True when the run settled as a deferred external wait.

    A watch is never resumed into worker steps by retry, daemon, resume,
    or keyed dispatch: only a real upstream input change lifts it (same
    run, same key, attempts preserved). A corrupt run never blocks
    dispatch.
    """
    from . import runner as _runner
    try:
        return _runner.resume_gate(run) == "watch"
    except Exception:  # noqa: BLE001
        return False


def _resumable(run: dict) -> bool:
    """A run worth continuing in place: unfinished, not terminally
    settled as a duplicate, not waiting on an operator answer, and not
    deferred as an external wait. execute() skips done steps, so failed
    runs resume too — retries keep the same run identity instead of
    forking copies."""
    if _run_finished(run):
        return False
    if _terminal_no_code(run):
        return False
    if _waiting_on_operator(run):
        return False
    if _external_watch_run(run):
        return False
    return True


def _plus_seconds(iso: str, seconds: int) -> str:
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        dt = datetime.datetime.now(datetime.timezone.utc)
    return (dt + datetime.timedelta(seconds=int(seconds))).isoformat(timespec="seconds")


# A launchd StartInterval tick can start seconds BEFORE a repo's
# next_scan (which used to be anchored to the previous scan's
# completion + interval), skipping the repo for a whole interval.
# Fix: a repo is due when its next_scan falls within a small tolerance
# of the tick start, and each scan anchors the next one to the tick
# start so the cadence tracks ticks instead of drifting. Tolerance is
# capped per repo at half its interval, so two scans of the same repo
# can never run back-to-back (launches stay >= interval - tol apart).
_DUE_TOLERANCE_S = 60


def _parse_ts(iso):
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _due_tol(interval_s, cap=_DUE_TOLERANCE_S):
    try:
        interval = int(interval_s)
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_S
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = _DUE_TOLERANCE_S
    return min(cap, max(0, interval // 2))


def _anchor_next(tick_start, now, interval_s):
    """next_scan for a scan launched by a tick: tick start + interval.

    tick_start None/unparseable (direct scans) keeps the old
    completion-anchored behavior.
    """
    anchor = tick_start if _parse_ts(tick_start) else now
    return _plus_seconds(anchor, interval_s)


def due_repos(now=None, tolerance_s=_DUE_TOLERANCE_S) -> list:
    from .util import utcnow as _now
    import datetime
    now_dt = _parse_ts(now or _now())
    if now_dt is None:
        now_dt = datetime.datetime.now(datetime.timezone.utc)
    out = []
    d = paths.home() / "sched"
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        if f.name.startswith("."):
            continue
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not st.get("enabled"):
            continue
        tol = _due_tol(st.get("interval_s", DEFAULT_INTERVAL_S),
                       tolerance_s)
        nxt = st.get("next_scan")
        if not nxt:
            out.append(st["repo"])
            continue
        nxt_dt = _parse_ts(nxt)
        if nxt_dt is None:
            # Unparseable state self-heals on the next scan.
            out.append(st["repo"])
            continue
        if nxt_dt <= now_dt + datetime.timedelta(seconds=tol):
            out.append(st["repo"])
    return out


def tick() -> dict:
    """Run every due scan (read-only). Returns per-repo summaries."""
    from .util import utcnow as _now
    tick_start = _now()
    results = {}
    for repo in due_repos(now=tick_start):
        try:
            results[repo] = scan(repo, tick_start=tick_start)
        except Exception as exc:  # noqa: BLE001
            with locked(repo):
                st = load(repo)
                st["failures"] = st.get("failures", 0) + 1
                idx = min(st["failures"] - 1, len(RETRY_BACKOFF_S) - 1)
                st["backoff_s"] = RETRY_BACKOFF_S[idx]
                st["error"] = f"{type(exc).__name__}: {exc}"[:300]
                st["scan_ok"] = False
                import datetime
                st["next_scan"] = (datetime.datetime.now(datetime.timezone.utc)
                                   + datetime.timedelta(seconds=st["backoff_s"])).isoformat(timespec="seconds")
                save(st)
            results[repo] = {"repo": repo, "error": st["error"]}
    _m1_after_tick()
    return results


def _m1_after_tick() -> None:
    """M1: background glance refresh + daily digest. Best-effort only:
    failures append an event and never break the tick. The import path
    (batches.import_batch) refreshes the same way; request handlers only
    ever read the snapshot file."""
    try:
        from . import batches as _b
    except Exception:  # noqa: BLE001 - without batches, nothing to do
        return
    snap = None
    try:
        snap = (_b.refresh_all_snapshots("tick") or {}).get("summary")
    except Exception as exc:  # noqa: BLE001 - tick already succeeded
        try:
            events.append("", "summary", "",
                          "상태 요약 갱신 실패",
                          f"{type(exc).__name__}: {exc}"[:300])
        except Exception:  # noqa: BLE001 - event write is best-effort
            pass
    try:
        today = _b.m1_local_day()
        try:
            last = _b.summary_digest_day_path().read_text(
                encoding="utf-8").strip()
        except OSError:
            last = ""
        if last != today:
            # First tick after local midnight: finalize the previous
            # local day, then mark today started (the final write
            # marks yesterday, so today is recorded explicitly).
            _b.write_digest(_b.m1_local_day(-1))
            try:
                _b.summary_digest_day_path().write_text(
                    today, encoding="utf-8")
            except OSError:
                pass
        _b.write_digest(today, provisional=True, snap=snap)
    except Exception as exc:  # noqa: BLE001 - digest never breaks tick
        try:
            events.append("", "summary", "",
                          "하루 요약 작성 실패",
                          f"{type(exc).__name__}: {exc}"[:300])
        except Exception:  # noqa: BLE001 - event write is best-effort
            pass


def _retry_at(attempts: int) -> str:
    import datetime
    wait = RETRY_BACKOFF_S[min(max(attempts - 1, 0), len(RETRY_BACKOFF_S) - 1)]
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=wait)).isoformat(timespec="seconds")


def _retry_ready(item: dict) -> bool:
    nxt = item.get("next_retry_at")
    if not nxt:
        return True
    try:
        return nxt <= utcnow()
    except Exception:  # noqa: BLE001
        return True


def _pick_next(st: dict) -> dict | None:
    # S16b: refresh-deferred items never head-block the automatic
    # pick — they rejoin via _release_resolved_refreshes once their
    # base batch resolves. Keyed (operator) dispatch is unaffected.
    for item in st.get("queue", []):
        if item.get("status") == "queued" and _retry_ready(item) \
                and not item.get("refresh_deferred"):
            return item
    return None


def _release_resolved_refreshes(repo: str, st: dict) -> bool:
    """S16b: deferred refreshes whose base batch now resolves rejoin
    the automatic pick (flag off, plain note). Returns whether
    anything changed. Read-only on git/batches; only the flag and
    the note are written, under the caller's lock."""
    from . import pcconfig as _pc
    try:
        checkout = _pc.checkout_for(repo)
    except Exception:  # noqa: BLE001 - unresolvable stays deferred
        return False
    changed = False
    for item in st.get("queue", []):
        if not isinstance(item, dict) or not item.get("refresh_deferred"):
            continue
        if str(item.get("status") or "") != "queued":
            continue
        head = _refresh_base_head(str(item.get("lane") or ""),
                                  str(item.get("refresh_base") or ""),
                                  checkout)
        if not head:
            continue
        item.pop("refresh_deferred", None)
        item["note"] = (f"새 기준(배치 {item.get('refresh_base')}) 확인됨 "
                        f"— 대기열로 복귀")
        changed = True
    return changed


def _pick_keyed(st: dict, key: str) -> tuple:
    """Validate an operator-chosen key against the ACTUAL queue.

    Returns (item, None) when exactly that proposal may run, else
    (None, refusal-dict). Unknown, finished (history), cancelled,
    non-queued, and backoff-held keys are refused — never silently
    replaced by the next unrelated job.
    """
    item = next((i for i in st.get("queue", [])
                 if i.get("key") == key), None)
    if item is None:
        for hist in st.get("history", []):
            if hist.get("key") == key:
                return None, {"reason":
                              f"stale: {key} already finished "
                              f"({hist.get('result', 'done')})",
                              "status": "finished"}
        return None, {"reason": f"unknown key: {key}", "status": "unknown"}
    watch = item.get("watch") if isinstance(item.get("watch"), dict) else None
    if watch is not None and watch.get("active", True):
        return None, {"reason":
                      f"external watch: {watch.get('reason', 'deferred')} — "
                      f"reassess when {watch.get('trigger', 'upstream change')} "
                      f"(same run, no new attempt, no bypass)",
                      "status": "watch"}
    status = item.get("status")
    if status == "cancelled":
        return None, {"reason": f"stale: {key} was cancelled",
                      "status": status}
    if status != "queued":
        return None, {"reason": f"not queued: {key} is {status}",
                      "status": status}
    if not _retry_ready(item):
        return None, {"reason": f"backoff: next retry at "
                                f"{item.get('next_retry_at')}",
                      "status": status}
    return item, None


def _recheck_applicability(repo: str, item: dict) -> str | None:
    """Proven-stale reasons only; None means still actionable.

    Read-only local evidence (checkout manifests + lockfile, no
    registry, no writes): a dep-update whose declaration vanished,
    turned workspace-local, or already has the target installed must
    not execute a stale upgrade. Anything unprovable stays actionable
    — staleness is never guessed.
    """
    if item.get("kind") != "dep-update":
        return None
    dep, target = item.get("dep"), item.get("target")
    if not dep or not target:
        return f"stale: proposal {item.get('key')} lacks dep/target"
    from . import manifests, pcconfig
    try:
        checkout = pcconfig.checkout_for(repo)
    except Exception:  # noqa: BLE001 - unprovable, stays actionable
        return None
    if not checkout or not Path(checkout).is_dir():
        return None
    try:
        disc = manifests.discover(Path(checkout))
    except Exception:  # noqa: BLE001 - unprovable, stays actionable
        return None
    entries = [d for d in disc.get("deps", []) if d.get("name") == dep]
    if not entries:
        return f"stale: {dep} is no longer declared in the linked checkout"
    managed = [e for e in entries
               if not e.get("unmanaged")
               and not manifests.local_resolution_reason(
                   e.get("lock_resolution"))]
    if not managed:
        return (f"stale: {dep} is now a local/workspace declaration; "
                f"registry upgrade to {target} no longer applies")
    tracked = [e for e in managed if e.get("lock_tracked") is True]
    if tracked:
        tpins = {e.get("installed") for e in tracked}
        if tpins and tpins == {target}:
            proven = sorted({str(e.get("lock_file") or e.get("file"))
                             for e in tracked})
            return (f"stale: {dep} already at {target} in tracked locks "
                    f"({', '.join(proven)}); "
                    f"ignored dev-lock lag is not a source job")
        return None
    pins = {e.get("installed") for e in managed}
    if pins and pins == {target}:
        return f"stale: {dep} already at {target} in the linked checkout"
    return None


def run_due(repo: str, executor, model=None, key: str | None = None) -> dict:
    """Execute one queued item through the validated worker.

    Exactly one item per call. Persists running status, the lane/branch/
    exact base, and the run association; same-run resume on retry;
    bounded retries with backoff; uncaught failures always clear state.

    `key=None` keeps the existing Next1 order (first retry-ready
    queued item), but a head proven stale against the live checkout is
    superseded with a reason and the next actionable item runs instead.
    With `key`, exactly that current repo proposal runs:
    the lane/branch/target come ONLY from the stored queue item — the
    caller supplies no target/branch payload — and unknown, stale
    (finished/cancelled/removed/local/up-to-date), non-queued, paused,
    and backoff-held keys are refused with a reason, never silently
    replaced by the next unrelated job. A keyed dep-update is
    rechecked against the live checkout just before launch so an
    already-satisfied or withdrawn proposal cannot execute.
    """
    from . import runner
    key = (key or "").strip() or None
    with locked(repo):
        st = load(repo)
        if not st.get("enabled"):
            out = {"repo": repo, "ran": False, "reason": "paused"}
            if key:
                out["key"] = key
            return out
        if key is None:
            # S16b: a deferred refresh whose base batch appeared
            # since rejoins the pick here; the rest stay parked.
            if _release_resolved_refreshes(repo, st):
                save(st)
            nxt = _pick_next(st)
            if nxt is None:
                waiting = [i for i in st["queue"]
                           if i.get("status") == "queued" and not _retry_ready(i)]
                if waiting:
                    return {"repo": repo, "ran": False,
                            "reason": f"backoff: next retry at {waiting[0].get('next_retry_at')}"}
                deferred = [i for i in st.get("queue", [])
                            if isinstance(i, dict)
                            and i.get("refresh_deferred")
                            and str(i.get("status") or "") == "queued"]
                if deferred:
                    base = str(deferred[0].get("refresh_base") or "?")
                    return {"repo": repo, "ran": False,
                            "reason": "refresh deferred: "
                                      f"새 기준(배치 {base}) 확인 필요"}
                return {"repo": repo, "ran": False, "reason": "queue empty"}
            snapshot = dict(nxt)
        else:
            item, refusal = _pick_keyed(st, key)
            if refusal is not None:
                return {"repo": repo, "ran": False, "key": key, **refusal}
            snapshot = dict(item)
    if key is None:
        # Automatic/Next1 path: never execute a proven-stale proposal.
        # Supersede stale heads with a reason (bounded: one pass over the
        # queue) and run the next still-actionable item; when everything
        # queued is stale, report it instead of blocking unrelated work.
        # Selection order is preserved — stale items are cancelled, never
        # silently skipped over while staying queued.
        with locked(repo):
            _st = load(repo)
            bound = sum(1 for i in _st.get("queue", [])
                        if i.get("status") == "queued") + 1
        for _ in range(max(bound, 1)):
            stale_head = _recheck_applicability(repo, snapshot)
            if stale_head is None:
                break
            with locked(repo):
                _st = load(repo)
                _itm = next((i for i in _st.get("queue", [])
                             if i.get("key") == snapshot.get("key")), None)
                if _itm is None or _itm.get("status") != "queued":
                    return {"repo": repo, "ran": False,
                            "reason": "queue changed before start"}
            cancel(repo, snapshot["key"], reason=stale_head)
            with locked(repo):
                _st = load(repo)
                nxt_head = _pick_next(_st)
                if nxt_head is None:
                    waiting = [i for i in _st["queue"]
                               if i.get("status") == "queued"
                               and not _retry_ready(i)]
                    if waiting:
                        return {"repo": repo, "ran": False,
                                "reason": f"backoff: next retry at "
                                          f"{waiting[0].get('next_retry_at')}"}
                    return {"repo": repo, "ran": False,
                            "reason": f"head proposal proven stale and superseded "
                                      f"({stale_head}); queue empty"}
                snapshot = dict(nxt_head)
        else:
            return {"repo": repo, "ran": False,
                    "reason": "queue did not converge on an actionable item"}
        if _recheck_applicability(repo, snapshot) is not None:
            return {"repo": repo, "ran": False,
                    "reason": "head proposal proven stale and superseded; "
                              "queue did not converge"}
    if key is not None:
        stale = _recheck_applicability(repo, snapshot)
        if stale is not None:
            # cancel() takes the repo lock itself, so validate here and
            # cancel after releasing (never nest the repo mutex).
            with locked(repo):
                st = load(repo)
                item = next((i for i in st.get("queue", [])
                             if i.get("key") == key), None)
                if item is None or item.get("status") != "queued":
                    return {"repo": repo, "ran": False, "key": key,
                            "reason": "stale: proposal changed mid-flight",
                            "status": (item or {}).get("status", "unknown")}
            cancel(repo, key, reason=stale)
            return {"repo": repo, "ran": False, "key": key,
                    "reason": stale, "status": "cancelled"}
    with locked(repo):
        st = load(repo)
        if key is not None:
            # Re-validate under lock: the item must still be the queued
            # proposal we checked — no swap, no silent fallback.
            item = next((i for i in st.get("queue", [])
                         if i.get("key") == key), None)
            if item is None or item.get("status") != "queued":
                return {"repo": repo, "ran": False, "key": key,
                        "reason": "stale: proposal changed before start",
                        "status": (item or {}).get("status", "unknown")}
            nxt = item
        else:
            nxt = next((i for i in st.get("queue", [])
                        if i.get("key") == snapshot.get("key")), None)
            if nxt is None or nxt.get("status") != "queued":
                return {"repo": repo, "ran": False,
                        "reason": "queue changed before start"}
            snapshot = dict(nxt)
        # Single OS-level worker lease for the whole actual execution.
        # Nested scheduler -> CLI -> runner entries reuse it (same
        # thread); any other live holder — thread or process — refuses
        # here with a re-queue, never an overlap.
        owner = acquire_worker_lease()
        if owner is None:
            # Transient contention: stay queued with a near-term retry,
            # never permanently blocked (no manual unblock needed).
            import datetime as _dt
            nxt["status"] = "queued"
            nxt["blocker"] = "another maintenance worker holds the global lock"
            nxt["next_retry_at"] = (
                _dt.datetime.now(_dt.timezone.utc)
                + _dt.timedelta(seconds=60)).isoformat(timespec="seconds")
            save(st)
            events.append(repo, "worker-overlap", nxt["key"],
                          "second worker refused; item re-queued", "")
            return {"repo": repo, "ran": False,
                    "reason": "worker busy; item re-queued"}
        nxt["status"] = "running"
        nxt.pop("blocker", None)
        st["current_job"] = {"id": nxt["key"], "run_id": nxt.get("run_id"),
                             "started_at": utcnow(), "owner": owner,
                             "item": {k: nxt.get(k) for k in
                                      ("key", "kind", "repo", "dep", "issue",
                                       "declared", "installed", "compared",
                                       "target", "channel", "lane", "branch",
                                       "base_branch",
                                       "update_kind", "scopes", "file", "why",
                                       "attempts", "run_id", "title")}}
        st["worker"] = {"owner": owner, "started_at": st["current_job"]["started_at"]}
        save(st)
        snapshot = dict(nxt)
    _start_heartbeat(owner)
    _attach_cancel_watcher(repo, snapshot["key"], snapshot.get("run_id"), executor, owner)
    try:
        # A cancel flag set between pick and start still wins over work.
        with locked(repo):
            _pre = load(repo)
            _itm = next((i for i in _pre.get("queue", [])
                         if i.get("key") == snapshot["key"]), None)
            if _itm is not None and _itm.get("cancel_requested"):
                return _finalize(repo, snapshot["key"], "failed-retry",
                                 detail="cancelled before start",
                                 title=snapshot.get("dep") or "")
        return _execute_item(repo, snapshot, executor, model, owner)
    except runner.QuotaHit as exc:
        return _finalize(repo, snapshot["key"], "quota",
                         detail=f"quota wait: {exc}"[:300])
    except SystemExit as exc:
        # Explicit setup refusal (missing ref/branch): no new run, state clears.
        return _finalize(repo, snapshot["key"], "failed-retry",
                         detail=str(exc)[:300], title=snapshot.get("dep") or "")
    except Exception as exc:  # noqa: BLE001 - state must always clear
        from .util import Cancelled as _Cancelled
        if isinstance(exc, _Cancelled):
            with locked(repo):
                try:
                    _st = load(repo)
                    _it = next((i for i in _st.get("queue", [])
                                if i.get("key") == snapshot["key"]), None)
                    if _it is not None:
                        _it["cancel_requested"] = True
                    save(_st)
                except Exception:  # noqa: BLE001
                    pass
            return _finalize(repo, snapshot["key"], "error",
                             detail=f"cancelled: {exc}"[:300])
        return _finalize(repo, snapshot["key"], "error",
                         detail=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        _detach_cancel_watcher(owner)
        with locked(repo):
            try:
                _st = load(repo)
                _st["worker"] = None
                save(_st)
            except Exception:  # noqa: BLE001
                pass
        release_worker_lease(owner)


def _attach_cancel_watcher(repo: str, key: str, run_id, executor, owner: str) -> None:
    """Cross-process cancel: poll the queue flag and trip the executor's
    cancel event so running shell/worker commands raise Cancelled."""
    import threading as _th
    ev = getattr(executor, "cancel_event", None)
    if ev is None:
        ev = _th.Event()
        try:
            executor.cancel_event = ev
        except Exception:  # noqa: BLE001
            return
    stop = _th.Event()
    with _HELD_GUARD:
        _HEARTBEATS[f"cancel:{owner}"] = stop

    def _poll():
        while not stop.wait(1.0):
            try:
                with locked(repo):
                    st = load(repo)
                item = next((i for i in st.get("queue", [])
                             if i.get("key") == key), None)
                if item is not None and item.get("cancel_requested"):
                    ev.set()
                    return
                if not st.get("enabled"):
                    # Pause stops starting new jobs; a deliberate cancel
                    # still trips via the flag above. Do not kill on pause.
                    pass
            except Exception:  # noqa: BLE001
                return

    _th.Thread(target=_poll, daemon=True,
               name=f"openmainty-cancel-{owner}").start()


def _detach_cancel_watcher(owner: str) -> None:
    _stop_heartbeat(f"cancel:{owner}")


def _finalize(repo: str, key: str, outcome: str, detail: str = "",
              ok: bool = False, title: str = "") -> dict:
    """Reload under lock and transition the item; always clears current_job."""
    with locked(repo):
        st = load(repo)
        job = st.get("current_job")
        if job and job.get("id") == key:
            st.pop("current_job", None)
        item = next((i for i in st.get("queue", []) if i.get("key") == key), None)
        if item is None:
            save(st)
            return {"repo": repo, "ran": False, "reason": "item vanished mid-run"}
        if item.get("cancel_requested"):
            item.pop("cancel_requested", None)
            item["status"] = "cancelled"
            item.pop("blocker", None)
            item["next_retry_at"] = None
            st["history"].append({"key": key, "at": utcnow(),
                                  "result": "cancelled",
                                  "run_id": item.get("run_id")})
            events.append(repo, "cancelled", key,
                          "operator cancel honored at finalize", detail[:300])
            save(st)
            return {"repo": repo, "ran": False, "reason": "cancelled by operator"}
        if outcome == "done-ok":
            if (item.get("status") == "waiting-review"
                    and item.get("verified") is True):
                # Already final for this exact link (e.g. guarded
                # reconciliation ran first, then the normal finish path
                # re-asserts): re-assert without duplicating history.
                save(st)
                return {"repo": repo, "ran": True, "ok": True,
                        "item": item, "note": "already final"}
            item["status"] = "waiting-review"
            item["verified"] = True
            item.pop("blocker", None)
            # A valid repair supersedes the stale test/plan-only no-code
            # note (history/events keep the record); other notes stay.
            if (item.get("note") or "").startswith("test/plan-only diff:"):
                item.pop("note", None)
            item["next_retry_at"] = None
            st["history"].append({"key": key, "at": utcnow(),
                                  "result": "waiting-review",
                                  "run_id": item.get("run_id")})
            events.append(repo, "run-done", key, title, detail[:800])
            save(st)
            return {"repo": repo, "ran": True, "ok": True, "item": item}
        if outcome == "no-code":
            # Confirmed duplicate: structured no-code disposition owned by
            # the same run. Terminal for dispatch (waiting-review with an
            # explicit note, never retried): no attempts, no backoff, no
            # new run forked. The run's own steps stay as they are —
            # triage done, the rest pending — never all-green.
            item["status"] = "waiting-review"
            item["verified"] = False
            item["note"] = detail[:300]
            item.pop("blocker", None)
            item["next_retry_at"] = None
            st["history"].append({"key": key, "at": utcnow(),
                                  "result": "no-code",
                                  "run_id": item.get("run_id")})
            events.append(repo, "run-no-code", key, title, detail[:500])
            save(st)
            return {"repo": repo, "ran": True, "ok": True,
                    "outcome": "no-code", "item": item}
        if outcome == "watch":
            # Deferred external wait: same run+key, attempts preserved,
            # history kept, no review green. The item rests in
            # waiting-review with an explicit watch summary (never
            # dispatched); only a real upstream input change lifts it
            # back to queued for the SAME run. Never a verified upgrade.
            item["status"] = "waiting-review"
            item["verified"] = False
            item["note"] = detail[:300]
            item["watch"] = _watch_summary(item.get("run_id"), detail)
            item.pop("blocker", None)
            item["next_retry_at"] = None
            st["history"].append({"key": key, "at": utcnow(),
                                  "result": "external-watch",
                                  "run_id": item.get("run_id")})
            events.append(repo, "run-watch", key, title, detail[:500])
            save(st)
            return {"repo": repo, "ran": True, "ok": True,
                    "outcome": "watch", "item": item}
        if outcome == "quota":
            item["status"] = "queued"
            item["blocker"] = detail
            item["next_retry_at"] = _retry_at((item.get("attempts", 0) or 0) + 1)
            # Keep the run association so the retry resumes the same run
            # instead of forking a copy after the quota clears.
            if not item.get("run_id") and item.get("run_issue"):
                prev = store.latest_run(repo, str(item["run_issue"]))
                if prev is not None:
                    item["run_id"] = prev["id"]
                    job = st.get("current_job")
                    if job and job.get("id") == key:
                        job["run_id"] = prev["id"]
            st["failures"] = st.get("failures", 0) + 1
            events.append(repo, "quota-wait", key, "quota hit; backing off", detail[:500])
            save(st)
            return {"repo": repo, "ran": False, "reason": "quota wait"}
        if outcome == "failed-retry":
            item["attempts"] = item.get("attempts", 0) + 1
            if item["attempts"] >= MAX_RETRIES:
                item["status"] = "blocked"
                item["blocker"] = "bounded retries exhausted; needs operator review"
                st["history"].append({"key": key, "at": utcnow(),
                                      "result": "blocked", "run_id": item.get("run_id")})
                events.append(repo, "run-blocked", key, title, item["blocker"])
            else:
                item["status"] = "queued"
                item["next_retry_at"] = _retry_at(item["attempts"])
                events.append(repo, "run-retry", key, title,
                              f"attempt {item['attempts']}/{MAX_RETRIES} failed; "
                              f"retry at {item['next_retry_at']}")
            save(st)
            return {"repo": repo, "ran": True, "ok": False, "item": item}
        # outcome == "error": uncaught failure — bounded retries with
        # backoff like any other failure (attempts increment; never
        # infinite, never leaves running behind).
        item["attempts"] = item.get("attempts", 0) + 1
        if item["attempts"] >= MAX_RETRIES:
            item["status"] = "blocked"
            item["blocker"] = f"bounded retries exhausted ({detail})"[:300]
            st["history"].append({"key": key, "at": utcnow(),
                                  "result": "blocked",
                                  "run_id": item.get("run_id")})
            events.append(repo, "run-blocked", key, title or key,
                          item["blocker"])
        else:
            item["status"] = "queued"
            item["blocker"] = detail
            item["next_retry_at"] = _retry_at(item["attempts"])
            events.append(repo, "run-error", key, title or key,
                          f"attempt {item['attempts']}/{MAX_RETRIES}: "
                          f"{detail}"[:500])
        save(st)
        return {"repo": repo, "ran": False, "reason": detail}


def peer_context(checkout, dep: str | None, target: str | None) -> str:
    """Linked-peer evidence for the update task (never a proof claim).

    Reads the actual checkout manifests and reports every declaration
    of `dep` across scopes/files. Framework-coupled upgrades
    (react/react-native/nitro) must be verified by the repo checks —
    this text says what is declared, not that it is compatible."""
    if not dep or not checkout:
        return ""
    try:
        from . import manifests as _m
        disc = _m.discover(Path(checkout))
    except Exception:  # noqa: BLE001
        return ""
    hits = [d for d in disc.get("deps", []) if d.get("name") == dep]
    if not hits:
        return (f"\nLinked peers: {dep} is not declared in the linked "
                "checkout manifests.\n")
    lines = []
    for h in hits:
        lines.append(f"- [{h.get('scope')}] {h.get('file')}: "
                     f"declared={h.get('declared')} "
                     f"installed={h.get('installed')}")
    coupled = {"react", "react-native", "react-dom", "expo",
               "nitro-codegen", "react-native-nitro-modules"}
    note = ""
    if dep in coupled:
        note = (" This is a framework-coupled package: upgrade it only "
                "together with its linked peers per the manifests above; "
                "compatibility is NOT proven until the repo checks pass.")
    return ("\nLinked-peer compatibility (evidence, not proof):\n"
            + "\n".join(lines)
            + f"\nProposed target: {target}.{note}\n")


def _refresh_base_head(lane: str, refresh_base_id: str, checkout) -> str:
    """S16: integration head of base batch <refresh_base_id> for lane.

    A refresh run re-does a stale candidate on the CURRENT lane base —
    the same head prepare_integration uses — never the older lane head
    the candidate was verified on. Returns the head when the batch
    exists, carries this lane, and resolves in the checkout; ""
    otherwise (the caller defers the run fail-closed and says so
    plainly — it never falls back to the lane base).
    Read-only: no checkout mutation, no batch writes.
    """
    from pathlib import Path as _Path
    bid = str(refresh_base_id or "").strip()
    if not bid:
        return ""
    try:
        from . import batches as _batches
        batch = _batches.load_batch(bid)
    except Exception:  # noqa: BLE001 - unknown batch keeps lane base
        return ""
    head = ""
    try:
        for integ in batch.get("integrations") or []:
            if isinstance(integ, dict) \
                    and str(integ.get("lane") or "") == str(lane or ""):
                head = str(integ.get("head") or "")
                break
    except Exception:  # noqa: BLE001
        return ""
    if not head or checkout is None:
        return ""
    try:
        co = _Path(checkout)
        if not co.is_dir() or not (co / ".git").exists():
            return ""
        code, _, _ = _strict_rev_parse(co, head)
    except Exception:  # noqa: BLE001
        return ""
    return head if code == 0 else ""


def _execute_item(repo: str, snapshot: dict, executor, model, owner) -> dict:
    """Start or resume the run for one queue item (spends quota)."""
    import argparse as _ap
    from . import branches, pcconfig, runner
    profile = repos.load(repo)
    checkout = pcconfig.checkout_for(repo)
    lane = snapshot.get("lane") or "next"
    base_branch = "next" if lane == "next" else profile.get("baseBranch", "main")
    if snapshot.get("branch"):
        branch = snapshot["branch"]
    else:
        slug = branches.slugify_topic(
            f"{snapshot.get('dep') or 'issue'}-{snapshot.get('target') or snapshot.get('issue')}")
        branch = f"codex/{'maint' if lane == 'main' else 'next'}/{slug}"
    refresh_base_id = str(snapshot.get("refresh_base") or "")
    refresh_base_head = _refresh_base_head(lane, refresh_base_id, checkout) \
        if refresh_base_id else ""
    if refresh_base_id and not refresh_base_head:
        # S16 fail-closed: an unresolvable refresh base must never fall
        # back to the lane base — that re-creates the exact stale
        # conflict and burns a Muse run. No execution, no block count:
        # one plain note, visible on the glance through the
        # refresh-deferred need_you gate. The item stays queued, so a
        # later tick retries once the base batch exists.
        note = (f"새 기준(배치 {refresh_base_id})을 찾지 못해 새로고침 "
                f"실행을 보류함 — 사람이 확인")
        with locked(repo):
            st = load(repo)
            item = next((i for i in st.get("queue", [])
                         if i.get("key") == snapshot["key"]), None)
            if item is not None:
                item["note"] = note
                item["refresh_deferred"] = True
                # run_due marked it running before dispatch; a
                # deferred refresh stays queued for a later tick.
                item["status"] = "queued"
                item["next_retry_at"] = None
                save(st)
        _release_current(repo, snapshot["key"])
        return {"repo": repo, "ran": False,
                "reason": "refresh base unresolvable; deferred"}
    fetch_evidence = {"ok": None, "note": "non-git checkout; no fetch"}
    divergence_note = ""
    if checkout and Path(checkout).is_dir() and (Path(checkout) / ".git").exists():
        fetch_evidence = _fetch_remote_refs(Path(checkout))
        if lane == "next":
            made = branches.ensure_next(Path(checkout),
                                        base=profile.get("baseBranch", "main"))
            if not made.get("created") and "already exists" not in made.get("reason", ""):
                return _finalize(repo, snapshot["key"], "failed-retry",
                                 detail=f"next setup failed: {made.get('reason')}",
                                 title=f"next branch setup for {snapshot.get('dep')}")
            ff = branches.maybe_fast_forward_next(
                Path(checkout), main_branch=profile.get("baseBranch", "main"))
            base_branch = _fresh_next_base(Path(checkout), base_branch,
                                           profile.get("baseBranch", "main"))
            divergence_note = _next_divergence_note(Path(checkout))
            if ff.get("advanced") and ff.get("before") != ff.get("after"):
                divergence_note += (f"\nlocal next fast-forwarded "
                                    f"{ff['before'][:12]} -> {ff['after'][:12]} "
                                    f"({ff.get('reason')})")
            elif not ff.get("advanced"):
                divergence_note += (f"\nlocal next kept as-is: {ff.get('reason')}"
                                    + (" — needs integration" if ff.get("diverged")
                                       else ""))
        else:
            # Main lane: a stale local main must not anchor new work when
            # the freshly fetched origin/<base> exists. Existing runs keep
            # their recorded base untouched (resume path below).
            base_branch = _resolve_main_base(
                Path(checkout), profile.get("baseBranch", "main"))
        if refresh_base_head:
            # S16: refresh runs build on the base batch's integration
            # head (the same head prepare uses), applied AFTER the
            # lane base resolution so no lane path can clobber it. A
            # raw sha rev-parses exactly — no HEAD fallback anywhere
            # on this path.
            base_branch = refresh_base_head
        code, _, err = _strict_rev_parse(Path(checkout), base_branch)
        if code != 0:
            return _finalize(repo, snapshot["key"], "failed-retry",
                             detail=f"missing base ref '{base_branch}': "
                                    f"{(err or '').strip()[-200:]} (no HEAD fallback)",
                             title=f"base ref check for {snapshot.get('dep')}")
    if snapshot.get("kind") == "issue-triage":
        issue = str(snapshot.get("issue"))
        fetched = cli.fetch_issue_content(profile.get("github", ""), issue)
        if fetched.get("error"):
            # Observable deferral: never fabricate an actionable fix from a
            # bare title when the issue cannot even be read.
            return _finalize(repo, snapshot["key"], "failed-retry",
                             detail=f"issue content unreadable, deferred: "
                                    f"{fetched['error']}"[:300],
                             title=f"upstream issue #{issue}")
        if (fetched.get("state") or "").upper() not in ("OPEN", "UNKNOWN", ""):
            # The item is already marked running with a current_job: flag
            # the cancel and finalize through the single path that clears
            # state, records history, and keeps the reason on the item.
            reason = (f"upstream issue #{issue} is {fetched['state']}; "
                      "no longer actionable")
            cancel(repo, snapshot["key"], reason=reason)
            out = _finalize(repo, snapshot["key"], "failed-retry",
                            detail=reason,
                            title=f"upstream issue #{issue}")
            with locked(repo):
                try:
                    _st = load(repo)
                    _itm = next((i for i in _st.get("queue", [])
                                 if i.get("key") == snapshot["key"]), None)
                    if _itm is not None:
                        _itm["note"] = reason[:300]
                        save(_st)
                except Exception:  # noqa: BLE001
                    pass
            return out
        title = fetched.get("title") or snapshot.get("title") or f"upstream issue #{issue}"
        body = (f"Upstream issue #{issue}: {title}\nWhy: {snapshot.get('why', '')}\n"
                f"Provenance: {fetched.get('provenance')} at {fetched.get('fetched_at')}\n"
                f"State: {fetched.get('state')} URL: {fetched.get('url')}\n"
                f"--- untrusted issue body below ---\n{fetched.get('body') or '(empty)'}\n"
                f"--- end untrusted issue body ---\n")
    else:
        issue = (f"dep-{snapshot.get('dep')}-{snapshot.get('target')}"
                 ).replace("/", "-").replace(" ", "")[:80]
        title = (f"chore(deps): {snapshot.get('dep')} "
                 f"{snapshot.get('installed') or snapshot.get('compared') or snapshot.get('declared')} "
                 f"-> {snapshot.get('target')}")
        body = (f"Scheduler proposal [{snapshot.get('update_kind')} lane={lane}]\n"
                f"Why: {snapshot.get('why')}\nBranch: {branch}\n"
                f"Lane: {lane} (integration lane) Base ref: {base_branch} "
                f"(actual ref used; lane identity is distinct)\n"
                f"Fetch: {'ok' if fetch_evidence.get('ok') else 'fallback'} - "
                f"{fetch_evidence.get('note')}\n"
                + (f"{divergence_note}\n" if divergence_note else "") +
                f"Scopes: {', '.join(snapshot.get('scopes') or [])}\n")
    if snapshot.get("kind") != "issue-triage":
        body += peer_context(checkout, snapshot.get("dep"),
                             snapshot.get("target"))
    run_fields = {"lane": lane, "branch": branch, "base_branch": base_branch,
                  "queue_key": snapshot["key"],
                  "dep": snapshot.get("dep"), "target": snapshot.get("target"),
                  "scopes": snapshot.get("scopes"),
                  "update_kind": snapshot.get("update_kind"),
                  "refresh_base": refresh_base_id or None}
    if refresh_base_id and snapshot.get("kind") != "issue-triage":
        # Reachable only with a resolved head (unresolvable defers
        # above, fail-closed).
        body += (f"Refresh: stale-candidate refresh — base is base "
                 f"batch {refresh_base_id} integration head "
                 f"{refresh_base_head[:12]} (same head prepare uses)\n")
    # Same-run resume: keep executing the recorded run instead of forking
    # a new one on every retry.
    prev_run_id = snapshot.get("run_id")
    resumed = False
    # Finished-run reconciliation (real and fixture executors alike): a
    # completed existing run settles its exact linked item — never a
    # forked copy, never new model calls. An empty upgrade delta settles
    # exactly as a fresh fork would have (terminal no-code, same run);
    # any other refusal stays queued/unverified for the operator.
    if prev_run_id:
        try:
            _prev = store.load(prev_run_id)
        except SystemExit:
            _prev = None
        if (_prev is not None and _run_finished(_prev)
                and not _prev.get("waitReason")
                and (_prev.get("queue_key") or snapshot.get("key"))
                == snapshot.get("key")):
            _fin = finalize_finished_run(repo, snapshot["key"], prev_run_id,
                                         actor="sched-retry")
            _release_current(repo, snapshot["key"])
            if _fin.get("ok"):
                return {"repo": repo, "ran": True, "ok": True,
                        "item": _fin.get("item"), "run_id": prev_run_id,
                        "reconciled": True}
            if _fin.get("unproven_upgrade"):
                return _finalize(repo, snapshot["key"], "no-code",
                                 detail=("test/plan-only diff: no lockfile or "
                                         "manifest dependency change in "
                                         "base..HEAD — not a dependency "
                                         "upgrade; no source change to review"),
                                 title=title)
            return {"repo": repo, "ran": False,
                    "reason": _fin.get("reason")}
    if prev_run_id and not _is_fake(executor):
        try:
            prev = store.load(prev_run_id)
        except SystemExit:
            prev = None
        if prev is not None and _resumable(prev):
            ns = _ap.Namespace(run=prev_run_id, executor=_executor_name(executor),
                               model=model, only=None, actor="scheduler")
            rc = cli.cmd_resume(ns)
            resumed = True
        elif prev is not None and _terminal_no_code(prev):
            # Confirmed duplicate: terminal no-code outcome owned by the
            # same run — never retried, never forked, no attempts spent.
            detail = (prev.get("waitReason")
                      or "duplicate: confirmed duplicate; no code change")
            _release_current(repo, snapshot["key"])
            return _finalize(repo, snapshot["key"], "no-code",
                             detail=detail[:300], title=title)
        elif prev is not None and _waiting_on_operator(prev):
            with locked(repo):
                st = load(repo)
                item = next((i for i in st["queue"] if i.get("key") == snapshot["key"]), None)
                if item is not None:
                    item["status"] = "waiting-review"
                    item["verified"] = False
                    item["blocker"] = f"run waits for operator: {prev.get('waitReason')}"[:300]
                    item["next_retry_at"] = None
                    save(st)
            _release_current(repo, snapshot["key"])
            return {"repo": repo, "ran": False,
                    "reason": "run waits for operator answer"}
        elif prev is not None and _external_watch_run(prev):
            # Deferred external wait: never retried into workers, never
            # forked. Rest the queue item in the watch outcome (same
            # identity, attempts preserved) instead of executing.
            detail = (prev.get("waitReason")
                      or "external watch: deferred until upstream trigger")
            _release_current(repo, snapshot["key"])
            return _finalize(repo, snapshot["key"], "watch",
                             detail=detail[:300], title=title)
    if resumed:
        ok = (rc == 0)
        settled = _settle_waiting_or_nocode(repo, snapshot, prev_run_id, title)
        if settled is not None:
            return settled
        return _finish_run(repo, snapshot, ok, title, body)
    def _persist_identity(new_run_id, extra_issue=issue):
        with locked(repo):
            st = load(repo)
            item = next((i for i in st["queue"]
                         if i.get("key") == snapshot["key"]), None)
            if item is not None:
                item["run_issue"] = extra_issue
                if new_run_id:
                    item["run_id"] = new_run_id
                    item["base_branch"] = base_branch
                    item["branch"] = branch
                    # A deferred refresh that finally started is no
                    # longer deferred.
                    item.pop("refresh_deferred", None)
            job = st.get("current_job")
            if job and job.get("id") == snapshot["key"] and new_run_id:
                job["run_id"] = new_run_id
                job["item"]["run_id"] = new_run_id
                job["item"]["base_branch"] = base_branch
                job["item"]["branch"] = branch
            save(st)

    with locked(repo):
        st = load(repo)
        item = next((i for i in st["queue"] if i.get("key") == snapshot["key"]), None)
        if item is not None:
            item["run_issue"] = issue
            save(st)
    if _is_fake(executor):
        rc, run_id = _drive_fake(repo, issue, title, body, executor,
                                 run_fields, prev_run_id,
                                 on_created=_persist_identity)
    else:
        ns = _ap.Namespace(repo=repo, issue=issue, title=title, body=body,
                           body_file=None, allow_duplicate=True, only=None,
                           executor=_executor_name(executor), model=model)
        rc, run_id = cli._start_run_with(ns, executor, run_fields=run_fields,
                                         raise_quota=True,
                                         on_created=_persist_identity)
    ok = (rc == 0)
    with locked(repo):
        st = load(repo)
        item = next((i for i in st["queue"] if i.get("key") == snapshot["key"]), None)
        if item is not None:
            if run_id:
                item["run_id"] = run_id
            job = st.get("current_job")
            if job and job.get("id") == snapshot["key"] and run_id:
                job["run_id"] = run_id
            save(st)
    # Lock consistency: a real worker must prove the worktree lock
    # actually contains the target version — never trust a stale install.
    # The fixture executor changes no files by construction, so it can
    # neither produce nor honestly satisfy lock evidence; its runs carry
    # an explicit note instead of a proof claim.
    if ok and snapshot.get("kind") == "dep-update" and run_id:
        if _is_fake(executor):
            try:
                state = store.load(run_id)
                state.setdefault("interventions", []).append(
                    {"at": utcnow(), "kind": "lock-unproven",
                     "detail": ("fixture executor performs no install; "
                                f"{snapshot.get('dep')}@{snapshot.get('target')} "
                                "lock match not proven")})
                store.save(state)
            except SystemExit:
                pass
        else:
            bad = _lock_mismatch(run_id, snapshot.get("dep"),
                                 snapshot.get("target"),
                                 snapshot.get("declared"))
            if bad:
                return _finalize(repo, snapshot["key"], "failed-retry",
                                 detail=f"lock consistency failed: {bad}",
                                 title=title)
        # A dependency upgrade with no lock/manifest change in its own
        # diff is test/plan-only noise, never a proven upgrade: settle it
        # as no-code (same identity, history kept) instead of blessing it
        # as a verified done-ok upgrade. Unprovable diffs stay actionable.
        try:
            _state = store.load(run_id)
        except SystemExit:
            _state = None
        if _state is not None:
            _evidence = _dep_diff_evidence(store.run_dir(run_id) / "work",
                                           _state.get("baseCommit"))
            if _evidence == []:
                return _finalize(repo, snapshot["key"], "no-code",
                                 detail=("test/plan-only diff: no lockfile or manifest "
                                         "dependency change in base..HEAD — not a dependency "
                                         "upgrade; no source change to review"),
                                 title=title)
    if not ok and run_id:
        # A fresh run that just settled as duplicate/waiting must not
        # collapse into failed-retry (no attempt consumed, no backoff).
        settled = _settle_waiting_or_nocode(repo, snapshot, run_id, title)
        if settled is not None:
            return settled
    return _finish_run(repo, snapshot, ok, title, body)


def _release_current(repo: str, key: str) -> None:
    with locked(repo):
        st = load(repo)
        if st.get("current_job", {}).get("id") == key:
            st.pop("current_job", None)
            save(st)


def requeue_run_for_dispatch(repo: str | None, run_id: str) -> dict:
    """Queue the SAME scheduler-managed run for later safe dispatch.

    Used after an operator answer is saved while another worker owns the
    lease (and as a busy-refusal backstop): flips the run's own queue
    item to queued with its recorded run_id and retry counters intact —
    never a new item, never a forked run, never a duplicate key, never a
    phantom running job. Answered runs in waiting-review would otherwise
    strand forever (dispatch only picks queued items).

    No-ops with a reason when the run is not scheduler-managed, is
    already queued/running, or is terminally settled (cancelled). Runs
    and items owned by a different run id are never touched.
    """
    try:
        state = store.load(run_id)
    except SystemExit as exc:
        return {"requeued": False, "reason": str(exc)[:200]}
    repo = repo or state.get("repo") or ""
    key = state.get("queue_key")
    if not repo or not key:
        return {"requeued": False,
                "reason": "not scheduler-managed (no queue key)"}
    with locked(repo):
        st = load(repo)
        item = next((i for i in st.get("queue", [])
                     if i.get("key") == key), None)
        if item is None:
            return {"requeued": False, "reason": f"unknown key: {key}"}
        if item.get("run_id") and item["run_id"] != run_id:
            return {"requeued": False,
                    "reason": "queue item already owns a different run"}
        status = item.get("status")
        if status == "queued" and item.get("run_id") == run_id:
            return {"requeued": True, "key": key, "status": "queued",
                    "note": "already queued"}
        if status == "running":
            return {"requeued": False,
                    "reason": "live execution owns it"}
        if status == "cancelled":
            return {"requeued": False,
                    "reason": "cancelled; operator decision stands"}
        try:
            _run = store.load(run_id)
        except SystemExit:
            _run = None
        if _run is not None and _external_watch_run(_run):
            return {"requeued": False,
                    "reason": "external watch active; lift the watch instead "
                              "of requeueing"}
        if status not in ("waiting-review", "blocked", "queued"):
            return {"requeued": False,
                    "reason": f"not requeueable from {status}"}
        # Adopt (when unowned) and flip to queued, preserving the run
        # identity and the bounded retry counters.
        item["run_id"] = run_id
        item["status"] = "queued"
        item["next_retry_at"] = None
        item.pop("blocker", None)
        item["note"] = "operator answered; same run awaiting dispatch"[:300]
        save(st)
    events.append(repo, "answer-queued", key,
                  "answered run queued for dispatch", f"run={run_id}")
    return {"requeued": True, "key": key, "status": "queued"}


def finalize_finished_run(repo: str, key: str, run_id: str, *,
                          reason: str | None = None,
                          correct_cancelled: bool = False,
                          actor: str = "cli-resume") -> dict:
    """Guarded same-run finalization: a completed existing run settles its
    exact linked queue item as waiting-review verified-true — never a
    forked copy, never new model calls, never a touched attempt counter.

    Used by normal CLI resume success and by scheduler retry/entry when
    the referenced run already finished. Every guard below must pass;
    any refusal leaves queue AND run byte-identical:

    - global OS worker lease (reentrant on the owning thread, so nested
      scheduler -> CLI -> runner execution reuses it; another live
      holder refuses with no mutation);
    - single repo-lock critical section for validate + mutate (no
      validate-then-act split across lock releases);
    - exact identity: item key exists, item run_id == run_id, run repo
      == repo, run queue_key == key, plus dep/target/branch (dep-update)
      or issue (issue-triage) equality — never an unrelated item or a
      different run, never a blind trust of an old run ok flag;
    - full real completion: every step done, no waitReason, no durable
      resume gate (duplicate/waiting/watch excluded) — a --only subset,
      blocked, or watch/no-code settlement is not full completion;
    - no persisted commit block (commitBlocked refuses);
    - fresh proof: recorded review+e2e HEAD/fingerprint agree with each
      other AND with a fresh live snapshot of the run workdir —
      stale/missing proof refuses and leaves the item unverified;
    - dep-update honesty: base..worktree holds a real lock/manifest
      delta (test/plan-only diffs report unproven_upgrade instead of a
      verified blessing).

    Cancelled/superseded items NEVER auto-resurrect: the default path
    refuses them. Only an explicit administrative correction
    (correct_cancelled=True with a non-empty reason, i.e. the `sched
    reconcile` CLI action) finalizes an actually-complete cancelled run,
    recording the prior cancelled state + reason in history, event, and
    run interventions while preserving attempts, run id, logs, and
    archives. Genuine cancellations stay cancelled.
    """
    from . import runner as _runner
    base_out = {"repo": repo, "key": key, "run_id": run_id}
    owner = acquire_worker_lease(owner=f"finalize:{repo}:{key}")

    def _refuse(why: str, extra: dict | None = None):
        out = dict(base_out)
        out.update({"ok": False, "reason": why})
        if extra:
            out.update(extra)
        return out

    if owner is None:
        return _refuse("worker busy: another maintenance worker holds "
                       "the global lock; run unchanged")
    try:
        with locked(repo):
            st = load(repo)
            item = next((i for i in st.get("queue", [])
                         if i.get("key") == key), None)
            if item is None:
                return _refuse(f"unknown key: {key}")
            status = item.get("status")
            if item.get("run_id") != run_id:
                return _refuse("queue/run identity mismatch; refusing")
            job = st.get("current_job") or {}
            claimed = status == "running" or job.get("id") == key
            holder = job.get("run_id") if job.get("id") == key else None
            # A scheduler retry entry claims the key for this exact run
            # before driving it: that own-dispatch hold proceeds (released
            # by the mutation below, like _finalize done-ok). Any other
            # live holder refuses.
            own_hold = claimed and holder == run_id
            if claimed and not own_hold:
                return _refuse("live execution owns it")
            if item.get("cancel_requested"):
                return _refuse("operator cancel pending; refusing to override")
            prior = str(status or "")
            correcting = prior == "cancelled" or prior.startswith("cancelled")
            allowed = ("queued", "blocked", "waiting-review")
            if own_hold:
                allowed = allowed + ("running",)
            if correcting:
                if not correct_cancelled:
                    return _refuse("cancelled; operator decision stands "
                                   "(explicit reasoned correction required)")
                if not (reason or "").strip():
                    return _refuse("correction of a cancelled item requires "
                                   "a non-empty reason")
            elif status not in allowed:
                return _refuse(f"not finalizable from {status}")
            try:
                run = store.load(run_id)
            except SystemExit as exc:
                return _refuse(str(exc)[:200])
            if run.get("repo") != repo or run.get("queue_key") != key:
                return _refuse("queue/run identity mismatch; refusing")
            if item.get("kind") == "dep-update":
                for field in ("dep", "target", "branch"):
                    if (run.get(field) or "") != (item.get(field) or ""):
                        return _refuse("queue/run identity mismatch; refusing")
            elif item.get("kind") == "issue-triage":
                if str(run.get("issue")) != str(item.get("issue")):
                    return _refuse("queue/run identity mismatch; refusing")
            if not _run_finished(run) or run.get("waitReason"):
                return _refuse("run not fully complete (steps pending or "
                               "waiting); refusing verified upgrade")
            try:
                gate = _runner.resume_gate(run)
            except Exception:  # noqa: BLE001 - unprovable gate never finalizes
                gate = "unknown"
            if gate is not None:
                return _refuse(f"run terminally settled ({gate}); "
                               "refusing verified upgrade")
            blocked = run.get("commitBlocked")
            if isinstance(blocked, dict) and blocked:
                step = blocked.get("step")
                exit_c = blocked.get("exit")
                tail = (blocked.get("stderr") or "").strip()[-200:]
                return _refuse(f"commit blocked at {step}: exit {exit_c}: "
                               f"{tail} — work preserved uncommitted")
            vcs = run.get("verifiedCommits") or {}
            vfs = run.get("verifiedFingerprints") or {}
            r_head, e_head = vcs.get("review"), vcs.get("e2e")
            r_fp, e_fp = vfs.get("review"), vfs.get("e2e")
            if not (r_head and e_head and r_fp and e_fp):
                return _refuse("missing verification proof (review+e2e "
                               "HEAD/fingerprint); refusing")
            if r_head != e_head or r_fp != e_fp:
                return _refuse("review/e2e proofs disagree; refusing "
                               "verified upgrade")
            if r_head in ("", "unknown") or not r_fp:
                return _refuse("unusable verification proof; refusing")
            work = store.run_dir(run_id) / "work"
            if not work.is_dir():
                return _refuse("missing proof: run workdir gone; refusing")
            try:
                fresh = _runner.source_snapshot(work)
            except Exception:  # noqa: BLE001 - unprovable stays unverified
                fresh = None
            if fresh is None:
                return _refuse("missing proof: live source unreadable; "
                               "refusing (unknown, not stale)")
            if fresh.get("head") != r_head or fresh.get("fingerprint") != r_fp:
                return _refuse("stale proof: live HEAD/fingerprint moved "
                               "since review+e2e; refusing")
            if item.get("kind") == "dep-update":
                try:
                    evidence = _dep_diff_evidence(work, run.get("baseCommit"))
                except Exception:  # noqa: BLE001 - unprovable, not empty
                    evidence = None
                if evidence is None:
                    return _refuse("unprovable upgrade delta; refusing")
                if evidence == []:
                    return _refuse("test/plan-only diff: no lockfile or "
                                   "manifest dependency change in base..HEAD "
                                   "— not a dependency upgrade",
                                   extra={"unproven_upgrade": True})
            if item.get("status") == "waiting-review" and item.get("verified") is True:
                return {**base_out, "ok": True, "status": "waiting-review",
                        "attempts": item.get("attempts", 0),
                        "note": "already final",
                        "proof": {"head": r_head, "fingerprint": r_fp}}
            # Same mutation semantics as _finalize "done-ok" (kept inline:
            # _finalize re-locks, and this validate+mutate MUST stay one
            # critical section). Attempts, run id, logs, and archives are
            # untouched — only status/verified/bookkeeping move.
            job = st.get("current_job")
            if job and job.get("id") == key:
                st.pop("current_job", None)
            item["status"] = "waiting-review"
            item["verified"] = True
            item.pop("blocker", None)
            if (item.get("note") or "").startswith("test/plan-only diff:"):
                item.pop("note", None)
            item["next_retry_at"] = None
            hist = {"key": key, "at": utcnow(),
                    "result": "waiting-review", "run_id": run.get("id")}
            if correcting:
                hist["corrected_from"] = prior
                hist["correction_reason"] = (reason or "").strip()[:300]
                item["note"] = (f"corrected cancellation: "
                                f"{(reason or '').strip()}"[:300])
            elif (reason or "").strip():
                hist["reason"] = (reason or "").strip()[:300]
            st["history"].append(hist)
            detail = (f"corrected cancellation ({(reason or '').strip()}); "
                      if correcting else "") + (run.get("title") or key)
            events.append(repo, "run-done", key, run.get("title") or key,
                          detail[:800])
            run.setdefault("interventions", []).append(
                {"at": utcnow(),
                 "kind": ("cancel-corrected" if correcting
                          else "queue-finalized"),
                 "detail": (f"{key} -> waiting-review verified-true by "
                            f"{actor}"
                            + (f" (corrected from {prior}: "
                               f"{(reason or '').strip()}"[:300] + ")"
                               if correcting else ""))[:500]})
            store.save(run)
            save(st)
            return {**base_out, "ok": True, "status": "waiting-review",
                    "attempts": item.get("attempts", 0),
                    "corrected_from": prior if correcting else None,
                    "proof": {"head": r_head, "fingerprint": r_fp}}
    finally:
        release_worker_lease(owner)


def _settle_waiting_or_nocode(repo: str, snapshot: dict,
                              run_id: str | None, title: str) -> dict | None:
    """Route a just-finished run that settled as duplicate/waiting/watch
    away from the generic failed-retry path.

    A confirmed duplicate becomes a terminal no-code queue outcome owned
    by the same run (no attempts, no backoff, no fork). Missing info
    stays waiting-review for the operator answer (same identity, no
    failure attempt consumed). A declared external watch becomes the
    deferred watch outcome (same identity, attempts preserved, no review
    green). Returns None when the run settled normally and the caller
    should use the standard finish path.
    """
    if not run_id:
        return None
    try:
        state = store.load(run_id)
    except SystemExit:
        return None
    if _terminal_no_code(state):
        detail = (state.get("waitReason")
                  or "duplicate: confirmed duplicate; no code change")
        return _finalize(repo, snapshot["key"], "no-code",
                         detail=detail[:300], title=title)
    if _external_watch_run(state):
        detail = (state.get("waitReason")
                  or "external watch: deferred until upstream trigger")
        return _finalize(repo, snapshot["key"], "watch",
                         detail=detail[:300], title=title)
    if _waiting_on_operator(state):
        with locked(repo):
            st = load(repo)
            item = next((i for i in st.get("queue", [])
                         if i.get("key") == snapshot["key"]), None)
            if item is not None:
                item["status"] = "waiting-review"
                item["verified"] = False
                item["blocker"] = (f"run waits for operator: "
                                   f"{state.get('waitReason')}")[:300]
                item["next_retry_at"] = None
                save(st)
        _release_current(repo, snapshot["key"])
        return {"repo": repo, "ran": False,
                "reason": "run waits for operator answer"}
    return None


def _commit_block_for(snapshot: dict) -> dict | None:
    """Persisted hook-rejected commit, if any, for the snapshot's run.

    Defensive finalization boundary: a direct-ok call must never bless
    an uncommitted candidate as a verified upgrade just because the
    caller reported success — the helper field alone does not suffice.
    """
    run_id = (snapshot or {}).get("run_id")
    if not run_id:
        return None
    try:
        state = store.load(run_id)
    except SystemExit:
        return None
    blocked = state.get("commitBlocked")
    return blocked if isinstance(blocked, dict) and blocked else None


def _finish_run(repo: str, snapshot: dict, ok: bool, title: str, body: str) -> dict:
    if ok:
        blocked = _commit_block_for(snapshot)
        if blocked is not None:
            return _finalize(
                repo, snapshot["key"], "failed-retry",
                detail=(f"commit blocked at {blocked.get('step')}: "
                        f"exit {blocked.get('exit')}: "
                        f"{(blocked.get('stderr') or '').strip()[-200:]} — "
                        "work preserved uncommitted; same-run "
                        "commit-only recovery available"),
                title=title)
        return _finalize(repo, snapshot["key"], "done-ok",
                         detail=body, title=title)
    return _finalize(repo, snapshot["key"], "failed-retry",
                     detail="worker run failed; retry scheduled",
                     title=title)


def _is_fake(executor) -> bool:
    from . import runner
    return isinstance(executor, runner.FakeExecutor)


# ---- external watch: cheap scan re-check + supervisory settle/lift ----
#
# A watch waits on explicit upstream inputs (tracked issue activity,
# watched registry releases). Scans re-observe those inputs with the
# same cheap read-only paths they already use — zero model calls. An
# unchanged observation only refreshes last/next check; a real relevant
# change lifts the SAME run+key back to queued (attempts preserved) for
# re-assessment, never a new run and never an automatic merge. Failed or
# missing evidence records a check error and stays deferred — it never
# wakes the watch and never counts as healthy.

def _watch_summary(run_id: str | None, detail: str = "") -> dict:
    """Small queue-item summary; the run record carries the full watch."""
    summary: dict = {}
    if run_id:
        try:
            run = store.load(run_id)
        except SystemExit:
            run = None
        watch = (run or {}).get("watch") or {}
        if isinstance(watch, dict):
            for field in ("reason", "trigger", "next_check", "last_check"):
                if watch.get(field):
                    summary[field] = watch[field]
    if not summary and detail:
        summary["reason"] = detail[:300]
    summary["active"] = True
    return summary


def _fetch_issue_watch_inputs(github: str, number) -> dict | None:
    """One small read-only `gh api` snapshot: number/title/state/updated_at/comments.

    Returns None on any failure (offline, missing issue, unparsable
    output): the caller records a failed check and stays deferred.
    """
    from .util import run_cmd
    number = str(number or "")
    github = (github or "").strip()
    if not number.isdigit() or "/" not in github:
        return None
    code, out, _ = run_cmd(
        ["gh", "api", f"repos/{github}/issues/{number}",
         "--jq", "{number,title,state,updated_at,comments}"],
        timeout=30)
    if code != 0:
        return None
    try:
        parsed = json.loads(out or "null")
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        comments = int(parsed.get("comments", 0) or 0)
    except (TypeError, ValueError):
        comments = 0
    if not parsed.get("updated_at"):
        return None
    return {"number": str(parsed.get("number", number)),
            "title": str(parsed.get("title", "") or ""),
            "state": str(parsed.get("state", "") or ""),
            "updated_at": str(parsed.get("updated_at")),
            "comments": comments}


WATCH_REGISTRY_MANAGERS = ("npm", "pub", "maven", "github", "sdk")


def _split_registry_key(key: str) -> tuple:
    """(manager, ident) for an explicit `manager:id` watched key.

    Plain package names return (None, key): the manager is inferred from
    the scan rows. The prefixes are registry managers, never run or
    dependency names, so the generic engine stays free of hardcoded
    runs.
    """
    head, sep, tail = str(key).partition(":")
    if sep and head in WATCH_REGISTRY_MANAGERS and tail:
        return head, tail
    return None, str(key)


def _scan_row_index(rows: list | None) -> dict:
    """Scan rows by package name (first occurrence wins)."""
    index: dict = {}
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if name and name not in index:
            index[name] = row
    return index


def _infer_registry_manager(key: str, ident: str, item: dict, index: dict):
    """Manager for a plain watched key: the item's own row kind first,
    then any same-named row kind. None when nothing says which
    registry answers — the caller records a failed check instead of
    guessing."""
    for row in (index.get(key), index.get(ident),
                index.get(item.get("dep") or "")):
        if isinstance(row, dict) and row.get("kind") in WATCH_REGISTRY_MANAGERS:
            return row["kind"]
    return None


def _read_registry_latest(manager: str | None, ident: str) -> str | None:
    """One small read-only registry lookup. None on any failure."""
    if not manager or not ident:
        return None
    try:
        value = deps.latest(manager, ident)
    except Exception:  # noqa: BLE001 - failed evidence, never a guess
        return None
    value = str(value or "").strip()
    return value or None


def observe_watch_inputs(repo: str, item: dict, run: dict,
                         rows: list | None, issues: list | None) -> dict | None:
    """Cheap read-only snapshot of the inputs a watch waits on.

    Observes only the input classes the watch baseline declares (an empty
    baseline observes whatever is available so the first check records
    it, except a fresh dep-update watch infers the registry class from
    its own dependency instead of also demanding an upstream issue for
    its synthetic issue marker). Registry observation covers EVERY watched key in the baseline —
    the item's own dependency plus any baseline-declared peer keys —
    never just the item's own row, so a watched peer release wakes the
    watch. Watched keys may be plain package names or explicit
    `manager:id` inputs; a stable `sdk` baseline class observes the
    official toolchain release the same way. Scan rows are used first;
    a watched key absent from (or errored in) the rows gets one small
    read-only registry lookup. Returns None when a needed input cannot
    be observed: the check is recorded as failed and the watch stays
    deferred, never silently healthy.
    """
    baseline = ((run.get("watch") or {}).get("inputs") or {})
    needs = set(baseline) if isinstance(baseline, dict) else set()
    observed: dict = {}
    kind = item.get("kind")
    fresh = not needs
    # Baseline-class inference for a fresh (empty-baseline) dep watch:
    # it waits on the dependency's registry pin while its issue slot is
    # a synthetic `dep-...` marker with no upstream issue to fetch, so
    # issue evidence must not gate the first registry seed — otherwise
    # every new automatic dep watch stays uninitializable forever.
    # Real issue-triage watches, explicit `issue` baselines, and unknown
    # legacy tasks (no dep to infer from) still require issue evidence
    # fail-closed.
    want_issue = (kind == "issue-triage" or "issue" in needs or
                  (fresh and not (kind == "dep-update" and item.get("dep"))))
    want_registry = kind == "dep-update" or "registry" in needs or (
        not needs and bool(item.get("dep")))
    want_sdk = "sdk" in needs and bool(
        isinstance(baseline.get("sdk"), dict) and baseline["sdk"])
    if want_issue:
        number = str(item.get("issue") or run.get("issue") or "")
        got = None
        if number.isdigit():
            try:
                github = repos.load(repo).get("github", "")
            except SystemExit:
                github = ""
            got = _fetch_issue_watch_inputs(github, number)
        if got is None:
            if "issue" in needs or not needs:
                return None
        else:
            observed["issue"] = got
    if want_registry:
        watched: list = []
        base_reg = baseline.get("registry") if isinstance(baseline, dict) else None
        if isinstance(base_reg, dict):
            watched.extend([k for k in base_reg.keys() if k])
        if item.get("dep") and item["dep"] not in watched:
            # The item dep is already covered when the baseline watches
            # the same package under an explicit `manager:id` key: a
            # plain duplicate would double-observe one input.
            _dep = item["dep"]
            if not any(_split_registry_key(k)[1] == _dep for k in watched):
                watched.append(_dep)
        index = _scan_row_index(rows)
        reg: dict = {}
        for key in watched:
            manager, ident = _split_registry_key(key)
            row = index.get(key) if key in index else index.get(ident)
            latest_v = None
            if (isinstance(row, dict) and not row.get("error")
                    and row.get("latest")):
                latest_v = str(row["latest"])
                if manager is None and row.get("kind") in WATCH_REGISTRY_MANAGERS:
                    manager = row["kind"]
            if latest_v is None:
                if manager is None:
                    manager = _infer_registry_manager(key, ident, item, index)
                latest_v = _read_registry_latest(manager, ident)
            if not latest_v:
                if "registry" in needs or not needs:
                    return None
                continue
            reg[key] = latest_v
        if reg:
            observed["registry"] = reg
        elif watched and ("registry" in needs or not needs):
            return None
    if want_sdk:
        index = _scan_row_index(rows)
        sdk_obs: dict = {}
        for name in baseline["sdk"].keys():
            latest_v = None
            row = index.get(name)
            if (isinstance(row, dict) and not row.get("error")
                    and row.get("latest")):
                latest_v = str(row["latest"])
            if latest_v is None:
                latest_v = _read_registry_latest("sdk", name)
            if not latest_v:
                return None
            sdk_obs[name] = latest_v
        if sdk_obs:
            observed["sdk"] = sdk_obs
    return observed or None


def compare_watch_inputs(baseline: dict, observed: dict) -> tuple:
    """(changed, detail): real relevant differences only.

    Compares issue comments/updated_at/state, every watched registry
    latest pin, and every watched stable SDK release. Fields missing on
    either side are uncomparable, never changes. Observation metadata
    alone (timestamps of the fetch itself) never wakes a watch: only
    the semantic input fields above count. A change means re-assess —
    never a claim that the semantic trigger is already fulfilled.
    """
    baseline, observed = baseline or {}, observed or {}
    changes = []
    b_issue, o_issue = baseline.get("issue") or {}, observed.get("issue") or {}
    if isinstance(b_issue, dict) and isinstance(o_issue, dict):
        for field in ("comments", "updated_at", "state"):
            old, new = b_issue.get(field), o_issue.get(field)
            if old is None or new is None:
                continue
            if str(old) != str(new):
                changes.append(f"issue {field}: {old!r} -> {new!r}")
    b_reg, o_reg = baseline.get("registry") or {}, observed.get("registry") or {}
    if isinstance(b_reg, dict) and isinstance(o_reg, dict):
        for pkg, old in b_reg.items():
            if pkg not in o_reg or o_reg[pkg] is None:
                continue
            if str(o_reg[pkg]) != str(old):
                changes.append(f"{pkg} latest: {old} -> {o_reg[pkg]}")
    b_sdk, o_sdk = baseline.get("sdk") or {}, observed.get("sdk") or {}
    if isinstance(b_sdk, dict) and isinstance(o_sdk, dict):
        for name, old in b_sdk.items():
            if name not in o_sdk or o_sdk[name] is None:
                continue
            if str(o_sdk[name]) != str(old):
                changes.append(f"sdk {name} stable: {old} -> {o_sdk[name]}")
    return (bool(changes), "; ".join(changes)[:500])


def _missing_watch_keys(baseline: dict, observed: dict) -> list:
    """Baseline input keys with no comparable observation.

    A partial observation never wakes a watch: the lift needs a
    reliable, complete, comparable snapshot.
    """
    missing = []
    for cls in ("issue", "registry", "sdk"):
        base = (baseline or {}).get(cls)
        if base is None:
            continue
        obs = (observed or {}).get(cls)
        if isinstance(base, dict):
            if not isinstance(obs, dict):
                missing.append(cls)
            else:
                missing.extend(f"{cls}:{k}" for k in base.keys() if k not in obs)
        elif obs is None:
            missing.append(cls)
    return missing


WATCH_LIFT_REOPEN_STEPS = ("triage", "analyze", "fix")


def _archive_lift_receipts(run_id: str, run: dict, steps) -> list:
    """Copy previous step receipts aside before a watch lift re-runs them.

    Evidence files named on the step entries plus the step logs are
    preserved as `*.historical-*` siblings, never deleted, so the
    re-assessment starts from archived (not overwritten) history.
    Returns the archived filenames.
    """
    archived = []
    try:
        rdir = Path(store.run_dir(run_id))
    except Exception:  # noqa: BLE001 - unresolvable run dir archives nothing
        return archived
    try:
        stamp = utcnow().replace(":", "").replace("-", "").replace("+", "")[:15]
    except Exception:  # noqa: BLE001
        stamp = "unknown"
    names: set = set()
    for step in steps:
        entry = ((run.get("steps") or {}).get(step) or {})
        for ev in (entry.get("evidence") or []):
            if isinstance(ev, str) and ev:
                names.add(ev.split("/")[-1])
        names.add(f"{step}.log.txt")
        names.add(f"{step}.live.log.txt")
    for name in sorted(names):
        src = rdir / name
        try:
            if not src.is_file():
                continue
            if ".historical-" in name:
                continue
            stem, suffix = (src.stem, src.suffix) if src.suffix else (name, "")
            dest = rdir / f"{stem}.historical-{stamp}{suffix}"
            if dest.exists():
                continue
            dest.write_bytes(src.read_bytes())
            archived.append(dest.name)
        except OSError:
            continue
    return archived


def _reopen_watch_lift_chain(run: dict, detail: str, now: str) -> dict:
    """Make a lifted watch SAME-run re-assess its changed evidence.

    Archives the previous assessment receipts, reopens the
    triage/analyze/fix chain to pending (done/failed/blocked steps rerun
    against the changed inputs instead of skipping into stale checks or
    drafts), invalidates any old done proof on verify steps (an honest
    not-applicable reproduce skip stays done), and records the
    changed-evidence context on the reopened steps plus a `watch.lift.txt`
    run artifact. Commits nothing: a no-source re-assessment must never
    manufacture plan/test noise commits, and no-code is not an upgrade.
    Returns {"reopened": [...], "invalidated": [...], "archived": [...]};
    the caller persists the run.
    """
    steps = (run.get("steps") or {})
    reopened, invalidated = [], []
    for step in WATCH_LIFT_REOPEN_STEPS:
        entry = steps.get(step)
        if not isinstance(entry, dict):
            continue
        if entry.get("status") in ("done", "failed", "blocked"):
            entry["status"] = "pending"
            entry["started_at"] = None
            entry["ended_at"] = None
            entry["note"] = (
                f"reopened: watched inputs changed ({detail}); "
                f"re-assessing changed evidence")[:300]
            reopened.append(step)
    for step in store.VERIFY_STEPS:
        if step in reopened:
            continue
        entry = steps.get(step)
        if not isinstance(entry, dict) or entry.get("status") != "done":
            continue
        try:
            if step == "reproduce" and store._is_not_applicable_skip(run, step):
                continue
        except Exception:  # noqa: BLE001 - unprovable skip still invalidates
            pass
        entry["status"] = "pending"
        entry["started_at"] = None
        entry["ended_at"] = None
        entry["note"] = ("invalidated: watch lifted on changed upstream "
                         "evidence; re-assessment required")[:300]
        invalidated.append(step)
    archived = _archive_lift_receipts(run.get("id") or "", run,
                                     list(dict.fromkeys(
                                         list(reopened) + list(invalidated))))
    try:
        rdir = Path(store.run_dir(run.get("id") or ""))
        lift_text = (f"lifted at {now}\nchanged evidence: {detail}\n"
                     f"reopened: {', '.join(reopened) or '(none)'}\n"
                     f"proof invalidated: {', '.join(invalidated) or '(none)'}\n"
                     f"archived receipts: {', '.join(archived) or '(none)'}\n"
                     "Re-assess the changed evidence above; a change means "
                     "re-assess only, never a claim the trigger is fulfilled.\n")
        (rdir / "watch.lift.txt").write_text(lift_text, encoding="utf-8")
        run.setdefault("artifacts", {})["watch_lift"] = "watch.lift.txt"
    except (OSError, TypeError):
        pass
    return {"reopened": reopened, "invalidated": invalidated,
            "archived": archived}


def refresh_external_watch(repo: str, key: str, observed: dict | None) -> dict:
    """Apply one watch check for an exact queue key (scan bookkeeping).

    `observed` comes from observe_watch_inputs (None when upstream
    evidence failed or is missing). Unchanged observations refresh
    last/next check with zero model calls; a real change lifts the SAME
    run+key back to queued (attempts preserved); failed evidence records
    a check error and stays deferred. Never raises for queue/run state.
    """
    with locked(repo):
        st = load(repo)
        item = next((i for i in st.get("queue", []) if i.get("key") == key), None)
        if item is None:
            return {"checked": False, "reason": f"unknown key: {key}"}
        if (item.get("status") == "running" or
                (st.get("current_job") or {}).get("id") == key):
            return {"checked": False, "reason": "live execution owns it"}
        # A cancelled (or otherwise terminally settled) item never
        # reopens: cancellation is an operator decision that a later
        # watch refresh must not undo. Return without touching the
        # item, the run, or history.
        status = str(item.get("status") or "")
        if status == "cancelled" or status.startswith("cancelled"):
            return {"checked": False,
                    "reason": "cancelled; operator decision stands"}
        run_id = item.get("run_id")
        try:
            run = store.load(run_id) if run_id else None
        except SystemExit:
            run = None
        if run is None or not _external_watch_run(run):
            return {"checked": False, "reason": "not watch-gated"}
        # Non-owned runs (identity drift, superseded forks) never move
        # through this key: refuse instead of lifting unowned work.
        if run.get("queue_key") and run.get("queue_key") != key:
            return {"checked": False,
                    "reason": "queue/run identity mismatch; refusing"}
        if run.get("repo") and run.get("repo") != repo:
            return {"checked": False,
                    "reason": "queue/run identity mismatch; refusing"}
        watch = run.get("watch") or {}
        now = utcnow()
        interval = st.get("interval_s", DEFAULT_INTERVAL_S)
        if observed is None:
            watch["last_check"] = now
            watch["next_check"] = _plus_seconds(now, interval)
            watch["check_error"] = ("upstream evidence unavailable "
                                    "(registry/issue fetch failed); staying deferred")
            run["watch"] = watch
            store.save(run)
            item["watch"] = _watch_summary(run_id, "")
            save(st)
            return {"checked": True, "changed": False,
                    "reason": "evidence-unavailable; staying deferred"}
        if not isinstance(observed, dict):
            watch["last_check"] = now
            watch["next_check"] = _plus_seconds(now, interval)
            watch["check_error"] = ("unknown watch schema: observation is not "
                                    "a comparable record; staying deferred")
            run["watch"] = watch
            store.save(run)
            item["watch"] = _watch_summary(run_id, "")
            save(st)
            return {"checked": True, "changed": False,
                    "reason": "unknown watch schema; staying deferred"}
        baseline = watch.get("inputs") or {}
        if not isinstance(baseline, dict):
            watch["last_check"] = now
            watch["next_check"] = _plus_seconds(now, interval)
            watch["check_error"] = ("unknown watch schema: baseline inputs are "
                                    "not a comparable record; staying deferred")
            run["watch"] = watch
            store.save(run)
            item["watch"] = _watch_summary(run_id, "")
            save(st)
            return {"checked": True, "changed": False,
                    "reason": "unknown watch schema; staying deferred"}
        if not baseline:
            watch["inputs"] = observed
            watch["last_check"] = now
            watch["next_check"] = _plus_seconds(now, interval)
            watch["check_error"] = None
            run["watch"] = watch
            store.save(run)
            item["watch"] = _watch_summary(run_id, "")
            save(st)
            return {"checked": True, "changed": False,
                    "reason": "baseline recorded; staying deferred"}
        missing = _missing_watch_keys(baseline, observed)
        if missing:
            watch["last_check"] = now
            watch["next_check"] = _plus_seconds(now, interval)
            watch["check_error"] = (
                f"partial watch input (missing: {', '.join(missing)[:200]}); "
                f"staying deferred until a complete observation")
            run["watch"] = watch
            store.save(run)
            item["watch"] = _watch_summary(run_id, "")
            save(st)
            return {"checked": True, "changed": False,
                    "reason": "partial watch input; staying deferred"}
        changed, detail = compare_watch_inputs(baseline, observed)
        watch["last_check"] = now
        watch["next_check"] = _plus_seconds(now, interval)
        watch["check_error"] = None
        if not changed:
            run["watch"] = watch
            store.save(run)
            item["watch"] = _watch_summary(run_id, "")
            save(st)
            return {"checked": True, "changed": False, "reason": "inputs unchanged"}
        # Lift: the SAME run re-assesses the changed evidence. Previous
        # receipts are archived, the triage/analyze/fix chain reopens
        # (done steps rerun instead of skipping into stale checks), old
        # proof is invalidated, and the changed evidence travels with the
        # run. Attempts and history are preserved; nothing is committed
        # and nothing is blessed — a change means re-assess only.
        chain = _reopen_watch_lift_chain(run, detail, now)
        watch["active"] = False
        watch["lifted_at"] = now
        watch["lift_detail"] = f"watched inputs changed: {detail}"[:500]
        # Resettlement snapshot: the complete validated observation that
        # lifted this watch, stamped with the server clock T0 (the same
        # instant recorded as watch.last_check above — never the
        # fetched_at fetch metadata, which is untrusted noise). Baseline
        # classes only. A later worker re-defer (clock T1) resettles onto
        # these pins while keeping last_check at T0, so monitoring
        # continues across reassessment instead of forgetting the
        # configured policy. Only reachable with a complete observation
        # (partial observations return early above, never lift).
        snap = {}
        if isinstance(observed, dict):
            for cls, vals in observed.items():
                if cls == "fetched_at" or not isinstance(vals, dict):
                    continue
                snap[cls] = dict(vals)
        watch["lift_observation"] = {"observed_at": now, "inputs": snap}
        run["watch"] = watch
        run["disposition"] = {
            "kind": "ACTIONABLE",
            "detail": f"watch inputs changed ({detail}); same-run re-assessment eligible"[:300],
            "ref": run.get("queue_key"),
            "at": now,
        }
        run["waitReason"] = None
        run.setdefault("interventions", []).append(
            {"at": now, "kind": "external-watch-lifted",
             "detail": (f"changed inputs: {detail}; reopened "
                        f"{'/'.join(chain['reopened']) or '(none)'}; proof reset "
                        f"{'/'.join(chain['invalidated']) or '(none)'}"[:300])})
        store.save(run)
        item["status"] = "queued"
        item["next_retry_at"] = None
        item.pop("blocker", None)
        item.pop("watch", None)
        item["note"] = (f"watch lifted ({detail}); same run re-assessing "
                        f"({', '.join(chain['reopened']) or 'no chain steps'})")[:300]
        st["history"].append({"key": key, "at": now,
                              "result": "watch-lifted",
                              "run_id": run_id})
        save(st)
        events.append(repo, "watch-lifted", key,
                      "watched inputs changed; same run re-assessing",
                      detail[:500])
        return {"checked": True, "changed": True, "reason": detail}


def check_repo_watches(repo: str, rows: list | None, issues: list | None) -> dict:
    """Re-check every watch-gated run of one repo (called from scan).

    Observation is read-only and cheap; each key finalizes through
    refresh_external_watch. Never raises: a broken check is reported,
    never fatal to the scan.

    Takes locked(repo) itself: callers MUST NOT hold the repo mutex when
    calling (POSIX flock self-blocks on a second fd in the same process).
    """
    report: dict = {"checked": 0, "changed": [], "failed": []}
    with locked(repo):
        try:
            st = load(repo)
        except Exception:  # noqa: BLE001 - scan reports this separately
            return report
        targets = []
        for item in st.get("queue", []):
            if not item.get("run_id"):
                continue
            # Cancelled items stay cancelled: never observe, never
            # refresh, never reopen — the scan must not undo the
            # operator decision even if the run record still reads
            # watch-gated.
            _st = str(item.get("status") or "")
            if _st == "cancelled" or _st.startswith("cancelled"):
                continue
            try:
                run = store.load(item["run_id"])
            except SystemExit:
                continue
            if _external_watch_run(run):
                targets.append((item["key"], dict(item), dict(run)))
    for key, item, run in targets:
        try:
            observed = observe_watch_inputs(repo, item, run, rows, issues)
        except Exception as exc:  # noqa: BLE001 - failed check, stay deferred
            observed = None
            report["failed"].append(f"{key}: observe error {exc}"[:200])
        try:
            res = refresh_external_watch(repo, key, observed)
        except Exception as exc:  # noqa: BLE001
            report["failed"].append(f"{key}: refresh error {exc}"[:200])
            continue
        if res.get("checked"):
            report["checked"] += 1
            if res.get("changed"):
                report["changed"].append(key)
    return report


def set_external_watch(repo: str, key: str, *, reason: str, evidence: str,
                       trigger: str, inputs: dict | None = None,
                       reopen_steps: tuple = (),
                       actor: str = "supervisory") -> dict:
    """Supported narrowly-scoped op: settle an owned run as deferred watch.

    Holds the global worker lease so the daemon cannot overlap, then (under
    the repo lock) validates the exact queue key + same-run identity,
    settles the run via runner.settle_external_watch, optionally reopens
    named steps to pending (proven-wrong receipts only, with a recorded
    reason), and rests the item in waiting-review with attempts, run id,
    and history preserved. No model calls (no executor exists on this
    path), no new run, no attempts reset. Refuses unknown/cancelled/
    running keys and identity mismatches without touching state.
    """
    from . import runner as _runner
    owner = acquire_worker_lease(owner=f"set-watch:{repo}:{key}")
    if owner is None:
        return {"ok": False, "repo": repo, "key": key,
                "reason": "worker busy: another maintenance worker holds "
                          "the global lock; run unchanged"}
    try:
        with locked(repo):
            st = load(repo)
            item = next((i for i in st.get("queue", []) if i.get("key") == key), None)
            if item is None:
                return {"ok": False, "repo": repo, "key": key,
                        "reason": f"unknown key: {key}"}
            status = item.get("status")
            if status == "cancelled":
                return {"ok": False, "repo": repo, "key": key,
                        "reason": f"stale: {key} was cancelled", "status": status}
            if status == "running":
                return {"ok": False, "repo": repo, "key": key,
                        "reason": "live execution owns it", "status": status}
            run_id = item.get("run_id")
            if not run_id:
                return {"ok": False, "repo": repo, "key": key,
                        "reason": "no owned run on this item; a watch settles "
                                  "an executed run, never a fresh proposal"}
            try:
                run = store.load(run_id)
            except SystemExit as exc:
                return {"ok": False, "repo": repo, "key": key,
                        "reason": str(exc)[:200]}
            if run.get("queue_key") != key or run.get("repo") != repo:
                return {"ok": False, "repo": repo, "key": key,
                        "reason": "queue/run identity mismatch; refusing"}
            # Validate EVERYTHING before mutating anything: settle saves
            # the run, so an unknown reopen step must be refused up front
            # rather than leaving a half-mutated settled run behind.
            for step in (reopen_steps or ()):
                if step not in store.STEP_ORDER or step not in (run.get("steps") or {}):
                    return {"ok": False, "repo": repo, "key": key,
                            "reason": f"unknown step to reopen: {step}"}
            try:
                watch = _runner.settle_external_watch(
                    run, reason=reason, evidence=evidence, trigger=trigger,
                    inputs=inputs, settled_by=actor)
            except ValueError as exc:
                return {"ok": False, "repo": repo, "key": key,
                        "reason": f"invalid watch outcome: {exc}"}
            for step in (reopen_steps or ()):
                entry = run["steps"][step]
                entry["status"] = "pending"
                entry["note"] = (f"reopened by {actor}: recorded receipt proven "
                                 f"wrong; awaiting honest re-check")[:300]
                run.setdefault("interventions", []).append(
                    {"at": utcnow(), "kind": "step-reopened",
                     "detail": f"{step} reopened by {actor}"})
            store.save(run)
            item["status"] = "waiting-review"
            item["verified"] = False
            item["note"] = f"external-watch: {reason}"[:300]
            item["watch"] = {"reason": watch["reason"],
                             "trigger": watch["trigger"],
                             "next_check": watch["next_check"],
                             "active": True}
            item.pop("blocker", None)
            item["next_retry_at"] = None
            st["history"].append({"key": key, "at": utcnow(),
                                  "result": "external-watch",
                                  "run_id": run_id})
            save(st)
            events.append(repo, "watch-deferred", key,
                          f"settled as external watch by {actor}",
                          f"{reason} | trigger: {trigger}"[:500])
            return {"ok": True, "repo": repo, "key": key, "run_id": run_id,
                    "status": "waiting-review", "attempts": item.get("attempts", 0),
                    "reopened": list(reopen_steps or [])}
    finally:
        release_worker_lease(owner)


WATCH_BASELINE_MAX_REGISTRY_KEYS = 8
WATCH_BASELINE_MAX_ADD_PER_CALL = 4
# Finite56 scope: the app editor offers npm/pub peers only, and the core
# accepts only those for NEW peers, so UI and server stay coherent (no
# unusable maven/github choices). The observe engine itself still honors
# whatever the settle path recorded.
WATCH_CONFIG_MANAGERS = ("npm", "pub")


def watch_inputs_fingerprint(inputs: dict) -> str:
    """Stable sha256 over the canonical baseline-inputs record.

    The UI reads this fingerprint with the watch report and echoes it
    back on save: a concurrent policy change in between refuses instead
    of silently overwriting. Canonical form matches
    json.dumps(sort_keys=True, separators=(",", ":"),
    ensure_ascii=True, default=str).
    """
    import hashlib
    canon = json.dumps(inputs or {}, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _valid_watch_registry_key(key) -> tuple | None:
    """(manager_or_None, ident) for a syntactically safe watched key.

    Plain package names return (None, name): the manager is resolved
    server-side from the repo scan rows. Explicit `manager:id` keys
    require a supported registry manager. URLs, whitespace, shell
    metacharacters, and unknown explicit managers are unsupported —
    the caller refuses them without touching state. Versions never
    live in keys: latest pins are observed server-side on save.
    """
    if not isinstance(key, str):
        return None
    text = key.strip()
    if not text or len(text) > 128:
        return None
    if "://" in text:
        return None
    if any(ch.isspace() for ch in text):
        return None
    if any(ch in text for ch in "\"'$`\\;|&<>(){}[]!*?~#%"):
        return None
    if text.startswith(("-", ".", "/")):
        return None
    manager, ident = _split_registry_key(text)
    if ":" in text and manager is None:
        return None
    if not ident:
        return None
    return manager, ident


def configure_watch_inputs(repo: str, key: str, *, add=(), remove=(),
                           expected_fp=None, expected_settled_at=None,
                           actor: str = "dashboard") -> dict:
    """Supported narrowly-scoped op: edit an active watch's registry peers.

    Baseline configuration only — NOT a successful full watch
    observation. Holds the global worker lease so the daemon cannot
    overlap, then (under the repo lock) validates the exact queue key +
    same-run identity, refuses unknown/mismatched/cancelled/inactive/
    stale/duplicate/unsupported/lookup-failed requests BEFORE any write,
    observes ONLY the newly added peers via fresh bounded server-side
    registry reads (scan rows prove membership/manager only and are
    never trusted as fresh pins; client-injected versions are never
    trusted), and updates just the inputs record + structured audit +
    next_check. settled_at, reason, evidence, trigger, active,
    last_check, check_error, disposition, answers, steps, proof
    payloads, work HEAD/fingerprint, and attempts are preserved
    byte-for-byte; other input classes (issue/sdk) pass through
    untouched. No model calls, no new run, no attempts reset, and
    set_external_watch is never called (the settlement timestamp is
    not reset). A lookup failure refuses the whole op with zero partial
    baseline writes.
    """
    def _refuse(status: str, reason: str) -> dict:
        return {"ok": False, "repo": repo, "key": key,
                "status": status, "reason": reason}
    if not isinstance(add, (list, tuple)) or not isinstance(remove, (list, tuple)):
        return _refuse("invalid", "add/remove must be lists of watched keys")
    if any(not isinstance(k, str) for k in list(add) + list(remove)):
        return _refuse("invalid", "add/remove must be lists of watched keys")
    adds = list(dict.fromkeys(k.strip() for k in add if k.strip()))
    removes = list(dict.fromkeys(k.strip() for k in remove if k.strip()))
    if not adds and not removes:
        return _refuse("invalid", "no baseline changes requested")
    if len(adds) > WATCH_BASELINE_MAX_ADD_PER_CALL:
        return _refuse("invalid",
                       f"at most {WATCH_BASELINE_MAX_ADD_PER_CALL} peers per save")
    if not isinstance(expected_fp, str) or not expected_fp:
        return _refuse("stale", "expected baseline fingerprint from the "
                                "watch report is required")
    if not isinstance(expected_settled_at, str) or not expected_settled_at:
        return _refuse("stale", "expected settled_at from the watch "
                                "report is required")
    owner = acquire_worker_lease(owner=f"configure-watch:{repo}:{key}")
    if owner is None:
        return _refuse("busy", "worker busy: another maintenance worker holds "
                               "the global lock; run unchanged")
    try:
        with locked(repo):
            st = load(repo)
            item = next((i for i in st.get("queue", []) if i.get("key") == key), None)
            if item is None:
                return _refuse("unknown", f"unknown key: {key}")
            status = str(item.get("status") or "")
            if status == "cancelled" or status.startswith("cancelled"):
                return _refuse("cancelled",
                               f"stale: {key} was cancelled; operator decision stands")
            if status == "running" or (st.get("current_job") or {}).get("id") == key:
                return _refuse("busy", "live execution owns it; run unchanged")
            run_id = item.get("run_id")
            if not run_id:
                return _refuse("unknown", "no owned run on this item; a watch "
                                          "baseline edits an executed run only")
            try:
                run = store.load(run_id)
            except SystemExit as exc:
                return _refuse("unknown", str(exc)[:200])
            if run.get("queue_key") != key or run.get("repo") != repo:
                return _refuse("mismatch", "queue/run identity mismatch; refusing")
            watch = run.get("watch")
            if (not isinstance(watch, dict) or not watch.get("active") or
                    watch.get("lifted_at") or not _external_watch_run(run)):
                return _refuse("inactive", "not an active deferred watch; refusing")
            inputs = watch.get("inputs")
            if not isinstance(inputs, dict):
                return _refuse("invalid", "unknown watch schema: baseline inputs "
                                          "are not a comparable record")
            if watch_inputs_fingerprint(inputs) != expected_fp:
                return _refuse("stale", "watch baseline changed since read; "
                                        "re-read the watch report and retry")
            if str(watch.get("settled_at") or "") != expected_settled_at:
                return _refuse("stale", "watch settlement changed since read; "
                                        "re-read the watch report and retry")
            base_reg = inputs.get("registry")
            if base_reg is None:
                base_reg = {}
            if not isinstance(base_reg, dict):
                return _refuse("invalid", "unknown watch schema: registry baseline "
                                          "is not a comparable record")
            watched_idents = set()
            for bk in base_reg.keys():
                parsed = _valid_watch_registry_key(bk)
                if parsed is None:
                    return _refuse("invalid", f"stored baseline key unsupported: "
                                              f"{bk}; refusing")
                watched_idents.add(parsed[1])
            parsed_adds = []
            for ak in adds:
                parsed = _valid_watch_registry_key(ak)
                if parsed is None:
                    return _refuse("invalid", f"unsupported watched key: {ak}; "
                                              "refusing without changing anything")
                if (parsed[0] is not None and
                        parsed[0] not in WATCH_CONFIG_MANAGERS):
                    return _refuse("invalid", f"unsupported manager for watch "
                                              f"peers: {parsed[0]} (npm/pub "
                                              "only); refusing")
                if parsed[1] in watched_idents:
                    return _refuse("invalid", f"duplicate watched peer: {ak} "
                                              "is already watched; refusing")
                if ak in removes:
                    return _refuse("invalid", f"contradictory edit for {ak}; refusing")
                parsed_adds.append((ak, parsed[0], parsed[1]))
                watched_idents.add(parsed[1])
            for rk in removes:
                if rk not in base_reg:
                    return _refuse("unknown", f"unknown baseline key: {rk}; refusing")
            primary = item.get("dep")
            if primary:
                for rk in removes:
                    parsed = _valid_watch_registry_key(rk)
                    if parsed is not None and parsed[1] == primary:
                        return _refuse("invalid", f"primary target {primary} must "
                                                  "stay watched; refusing")
            # Membership: EVERY selected peer — plain or explicit
            # `manager:id` — must be an actual repo package. An explicit
            # manager never authorizes an arbitrary remote key: the scan
            # pool must hold the same ident under the same manager.
            # Rows prove membership/manager only, never fresh pins.
            # Unmanaged/local/workspace rows (self-workspace lock verdicts,
            # file/link/portal/patch specs) and sdk-bound rows are not
            # registry packages even when they carry kind npm: selecting
            # them fails here, before any lookup or write.
            rows = [r for r in (st.get("last_rows") or [])
                    if isinstance(r, dict)]
            resolved = []
            for ak, manager, ident in parsed_adds:
                match = None
                for row in rows:
                    if row.get("name") != ident:
                        continue
                    if row.get("update_kind") in ("unmanaged", "sdk"):
                        continue
                    kind = row.get("kind")
                    if kind not in WATCH_CONFIG_MANAGERS:
                        continue
                    if manager is None:
                        match = kind
                        break
                    elif kind == manager:
                        match = manager
                        break
                if match is None:
                    return _refuse("unknown", f"unknown key: {ak} is not a "
                                              "registry package in this repo; "
                                              "refusing")
                resolved.append((ak, match, ident))
            resulting = [k for k in base_reg.keys() if k not in removes]
            resulting_idents = set()
            for k in resulting:
                resulting_idents.add(_valid_watch_registry_key(k)[1])
            resulting_idents.update(ident for _, _, ident in resolved)
            if len(resulting) + len(resolved) > WATCH_BASELINE_MAX_REGISTRY_KEYS:
                return _refuse("invalid", f"at most {WATCH_BASELINE_MAX_REGISTRY_KEYS} "
                                          "watched registry keys")
            if primary and primary not in resulting_idents:
                return _refuse("invalid", f"missing primary: {primary} would no "
                                          "longer be watched; refusing")
            # Fresh pins: every ADDED peer gets a bounded live registry
            # read, even when a cached scan row carries a latest value.
            # A failed read refuses the whole op with zero partial
            # baseline writes. The lease heartbeat stays up across the
            # bounded (<=4 x 15s) network loop.
            observed_new = {}
            _start_heartbeat(owner)
            try:
                for ak, manager, ident in resolved:
                    latest_v = _read_registry_latest(manager, ident)
                    if not latest_v:
                        return _refuse("lookup", f"registry lookup failed for "
                                                 f"{ak}; refusing without "
                                                 "changing anything")
                    observed_new[ak] = latest_v
            finally:
                _stop_heartbeat(owner)
            now = utcnow()
            old_keys = sorted(base_reg.keys())
            new_reg = {k: v for k, v in base_reg.items() if k not in removes}
            new_reg.update(observed_new)
            new_inputs = dict(inputs)
            new_inputs["registry"] = new_reg
            watch["inputs"] = new_inputs
            watch["next_check"] = now
            run["watch"] = watch
            run.setdefault("interventions", []).append(
                {"at": now, "kind": "watch-inputs-configured",
                 "repo": repo, "key": key, "run_id": run_id, "actor": actor,
                 "old_inputs": dict(inputs), "new_inputs": new_inputs,
                 "observed": dict(observed_new),
                 "source": {"workCommit": run.get("workCommit"),
                            "workTreeFingerprint":
                                run.get("workTreeFingerprint")},
                 "detail": (f"{actor}: registry {old_keys} -> "
                            f"{sorted(new_reg.keys())}")})
            store.save(run)
            st["history"].append({"key": key, "at": now,
                                  "result": "watch-inputs-configured",
                                  "run_id": run_id, "actor": actor,
                                  "old_keys": old_keys,
                                  "new_keys": sorted(new_reg.keys())})
            try:
                item["watch"] = _watch_summary(run_id, "")
            except Exception:  # noqa: BLE001 - summary is display-only
                pass
            save(st)
            events.append(repo, "watch-inputs-configured", key,
                          f"watch baseline edited by {actor}",
                          f"registry {old_keys} -> {sorted(new_reg.keys())}"[:500])
            return {"ok": True, "repo": repo, "key": key, "run_id": run_id,
                    "status": item.get("status"),
                    "attempts": item.get("attempts", 0),
                    "old_keys": old_keys, "new_keys": sorted(new_reg.keys()),
                    "observed": observed_new,
                    "fingerprint": watch_inputs_fingerprint(new_inputs),
                    "settled_at": watch.get("settled_at")}
    finally:
        release_worker_lease(owner)


def _executor_name(executor) -> str:
    from . import runner
    if isinstance(executor, runner.FakeExecutor):
        return "fake"
    kind = getattr(executor, "kind", "muse")
    return kind if kind in ("muse", "echo", "fake") else "muse"


def _drive_fake(repo: str, issue: str, title: str, body: str,
                executor, run_fields: dict, prev_run_id: str | None,
                on_created=None):
    """Fixture path: same run create/resume semantics without quota."""
    from . import pcconfig, runner
    profile = repos.load(repo)
    checkout = Path(pcconfig.checkout_for(repo))
    if prev_run_id:
        try:
            state = store.load(prev_run_id)
        except SystemExit:
            state = None
        gate = runner.resume_gate(state) if state is not None else None
        if state is not None and gate is not None:
            # Duplicate/waiting runs keep their identity and their wait
            # text: no pop, no worker calls, no fork. execute() itself
            # re-checks the durable gate, so this is defense in depth.
            # Finished runs fall through to the normal path below.
            runner.enforce_gate(state, gate)
            return 1, prev_run_id
        if state is not None and _resumable(state):
            if on_created is not None:
                try:
                    on_created(prev_run_id)
                except Exception:  # noqa: BLE001
                    pass
            ok = runner.execute(state, profile, executor, checkout, body=body)
            return (0 if ok else 1), prev_run_id
        if (state is not None and _run_finished(state)
                and not state.get("waitReason")
                and (state.get("queue_key") or "") == (run_fields.get("queue_key") or "")):
            # Same reconciliation as the real path (mirrors the
            # _execute_item early branch for direct callers): settle the
            # completed run into its linked item instead of forking.
            fin = finalize_finished_run(
                repo, state.get("queue_key"), prev_run_id,
                actor="sched-retry")
            if fin.get("ok"):
                if on_created is not None:
                    try:
                        on_created(prev_run_id)
                    except Exception:  # noqa: BLE001
                        pass
                return 0, prev_run_id
            return 1, prev_run_id
    import argparse as _ap
    ns = _ap.Namespace(repo=repo, issue=issue, title=title, body=body,
                       body_file=None, allow_duplicate=True, only=None,
                       executor="fake", model=None)
    return cli._start_run_with(ns, executor, run_fields=run_fields,
                               raise_quota=True, on_created=on_created)


def _strict_rev_parse(checkout: Path, ref: str):
    from .util import run_cmd
    return run_cmd(["git", "-C", str(checkout), "rev-parse", "--verify", ref])


def _fetch_remote_refs(checkout: Path) -> dict:
    """Fetch remote refs for scheduled work without touching the checkout.

    Updates remote-tracking refs only (never the working tree, index, or
    local branches), so a dirty user checkout is safe. Fetches the whole
    remote (`git fetch origin --prune`) instead of naming `next`/`main`
    explicitly: both real repos currently have NO origin/next, and naming
    a missing ref can fail the entire fetch. Returns fetch evidence
    {ok, note}; offline work falls back to local refs and the strict base
    check still refuses silently stale strategies."""
    from .util import run_cmd
    try:
        code, _, err = run_cmd(["git", "-C", str(checkout), "fetch",
                                "origin", "--prune", "--quiet"], timeout=120)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "note": f"fetch failed: {exc}"[:200]}
    if code != 0:
        return {"ok": False,
                "note": f"fetch exit {code}: {(err or '').strip()[-160:]}"}
    return {"ok": True, "note": "fetched origin remote-tracking refs"}


def _resolve_main_base(checkout: Path, base_branch: str) -> str:
    """Fresh base for main-lane runs: the freshly fetched
    `origin/<base>` when it exists, else the local base.

    Read-only selection only: never checks out or pulls the user's
    branch — the feature worktree is created detached at this ref.
    """
    from .util import run_cmd
    remote = f"origin/{base_branch or 'main'}"
    code, _, _ = run_cmd(["git", "-C", str(checkout), "rev-parse",
                          "--verify", remote])
    if code == 0:
        return remote
    return base_branch or "main"


def _fresh_next_base(checkout: Path, base_branch: str, main_branch: str) -> str:
    """Prefer a freshly fetched remote ref over a stale local `main`.

    Never creates, resets, rebases, or rewrites anything: just selects
    which existing ref the worktree should detach the feature branch
    from. Order: origin/next (fetched above) > local next >
    origin/<base> (fresh main) > requested base > main branch."""
    from .util import run_cmd
    base_branch = base_branch or "next"
    cands = ["origin/next", "next"]
    if main_branch and main_branch != base_branch:
        cands.append(f"origin/{main_branch}")
    cands += [base_branch, main_branch or "main"]
    for cand in cands:
        if not cand:
            continue
        code, _, _ = run_cmd(["git", "-C", str(checkout), "rev-parse",
                              "--verify", cand])
        if code == 0:
            return cand
    return base_branch


def _next_divergence_note(checkout: Path) -> str:
    """Honest behind/ahead of the preserved local `next` vs origin/next.

    The existing local next is never force-reset; this text only reports
    the relationship so the run evidence shows which integration strategy
    is in play. Empty when either side is missing."""
    from .util import run_cmd
    code, out, _ = run_cmd(
        ["git", "-C", str(checkout), "rev-list", "--left-right", "--count",
         "next...origin/next"])
    if code != 0 or not out.strip():
        return ""
    try:
        ahead, behind = out.strip().split()
        return (f"local next is {ahead} ahead / {behind} behind origin/next; "
                "preserved as-is (no reset), feature work detached at the "
                "selected base")
    except ValueError:
        return ""


# Lock/manifest files that prove a dependency update changed something
# reviewable. Basenames only: example/ and root scopes share them.
_DEP_EVIDENCE_BASENAMES = frozenset({
    "yarn.lock", "package-lock.json", "package.json",
    "pubspec.lock", "pubspec.yaml",
})


def _dep_diff_evidence(work, base_commit: str | None) -> list | None:
    """Changed lock/manifest paths in base..worktree proving a dependency update.

    Compares the base against the live worktree (committed + staged +
    unstaged), never HEAD alone: a real upgrade whose local commit was
    blocked by hooks still shows its manifest/lock delta, so a failed
    commit or a staged candidate can never read as test/plan-only.
    Returns the sorted evidence paths, [] when the delta is empty or holds
    only test/plan/docs noise (never a dependency upgrade), and None when
    unprovable (non-git worktree, unknown base, git failure) — staleness
    is never guessed, so unprovable stays actionable.
    """
    from pathlib import Path as _Path
    work = _Path(work)
    if not base_commit or base_commit in ("unknown", ""):
        return None
    if not (work / ".git").exists():
        return None
    from .util import run_cmd
    code, out, _ = run_cmd(["git", "-C", str(work), "diff", "--name-only",
                            f"{base_commit}", "--"])
    if code != 0:
        return None
    from pathlib import PurePosixPath as _Pure
    evidence = sorted({line.strip() for line in out.splitlines()
                       if line.strip()
                       and _Pure(line.strip()).name in _DEP_EVIDENCE_BASENAMES})
    return evidence


def _lock_mismatch(run_id: str, dep: str | None, target: str | None,
                   declared: str | None = None) -> str | None:
    """None when the run worktree proves `dep@target` is installed.

    Exact evidence only: the worktree manifest must declare `dep`, and
    the worktree lockfile must resolve that exact selector to `target`
    via the manifest selector mapping. A missing lockfile, a missing
    entry, or a version mismatch returns a reason (the caller retries
    with backoff, then blocks for review) — never success. Substring
    occurrence of the version anywhere in the file is NOT evidence.
    """
    from . import manifests as _m
    if not dep or not target:
        return None
    work = store.run_dir(run_id) / "work"
    if not work.is_dir():
        return f"run worktree missing for {dep}@{target}; cannot verify install"
    try:
        disc = _m.discover(work)
    except Exception as exc:  # noqa: BLE001
        return f"worktree manifest unreadable for {dep}@{target}: {exc}"[:300]
    entries = [d for d in disc.get("deps", []) if d.get("name") == dep]
    if not entries:
        return (f"{dep} not declared in run worktree manifests; "
                f"cannot prove {dep}@{target} installed")
    managed = [e for e in entries if not e.get("unmanaged")]
    if not managed:
        return (f"{dep} has only unmanaged specs in the worktree; "
                f"registry target {target} is not installable evidence")
    selectors = {(e.get("declared") or "") for e in managed}
    if declared:
        selectors.add(declared)
    installed = None
    installer = None
    try:
        npm_map = _m._npm_installed(work)
    except Exception:  # noqa: BLE001
        npm_map = {"by_name": {}, "by_selector": {}}
    try:
        pub_map = _m._pub_lock_versions(work)
    except Exception:  # noqa: BLE001
        pub_map = {}
    for sel in selectors:
        hit = _m.installed_for(npm_map, dep, sel or None)
        if hit:
            installed, installer = hit, "yarn.lock/package-lock.json"
            if hit == target:
                break
    if installed != target and pub_map.get(dep):
        installed, installer = pub_map[dep], "pubspec.lock"
    if installed != target:
        # Scoped consumer locks (e.g. example/pubspec.lock) prove the
        # target too: any managed manifest entry pinning it counts, with
        # the proving lock file named.
        for entry in managed:
            if entry.get("installed") == target:
                installed = target
                installer = entry.get("lock_file") or entry.get("file") or "manifest lock"
                break
    if installed is None:
        have = sorted({k for k in list(npm_map.get("by_name", {})) +
                       list(pub_map)} )[:8]
        return (f"no lockfile entry for {dep} (selectors "
                f"{sorted(s for s in selectors if s) or ['(none)']}); "
                f"cannot prove {dep}@{target} installed"
                + (f" (locks know: {', '.join(have)})" if have else ""))
    if installed != target:
        return (f"worktree lock pins {dep}@{installed} via {installer}, "
                f"not the proposed target {target}; install the target in "
                f"the isolated worktree before verification")
    if not _install_evidence(run_id):
        events.append(store.load(run_id).get("repo", ""), "install-note",
                      run_id, f"{dep}@{target} lock match without recorded "
                      "install/check command; lock evidence accepted", "")
    return None


def _install_evidence(run_id: str) -> bool:
    """True when the run recorded a real install/check command."""
    try:
        state = store.load(run_id)
    except SystemExit:
        return False
    needles = ("npm install", "yarn", "pub get", "pub upgrade",
               "pod install", "bundle install", "pip install")
    blobs = []
    for step in (state.get("steps") or {}).values():
        cmd = step.get("command") or []
        blobs.append(json.dumps(cmd))
        for ev in step.get("evidence") or []:
            blobs.append(str(ev))
        blobs.append(str(step.get("note") or ""))
    hay = "\n".join(blobs).lower()
    return any(n in hay for n in needles)


def _dispatch_file() -> Path:
    return paths.home() / "sched" / ".dispatch.json"


def dispatch_status() -> dict:
    try:
        data = json.loads(_dispatch_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_tick": None, "next_tick": None}
    return data if isinstance(data, dict) else {"last_tick": None,
                                               "next_tick": None}


def _record_dispatch_tick() -> dict:
    """Service dispatch timestamps, separate from per-repo scan times."""
    now = utcnow()
    nxt = _plus_seconds(now, DEFAULT_INTERVAL_S)
    try:
        _dispatch_file().parent.mkdir(parents=True, exist_ok=True)
        _dispatch_file().write_text(json.dumps({"last_tick": now,
                                                "next_tick": nxt}),
                                    encoding="utf-8")
    except OSError:
        pass
    return {"last_tick": now, "next_tick": nxt}


def daemon_tick(executor=None, model=None) -> dict:
    """Maintenance-service step: scan due repos, then execute one due item
    per repo whose autonomous execute mode is ON. One worker overall."""
    try:
        from . import promote as _promote
        print(_promote.app_start_line(), flush=True)
    except Exception:  # noqa: BLE001 - the start line never kills a tick
        pass
    scanned = tick()
    dispatch = _record_dispatch_tick()
    executed = {}
    for repo in repos.list_all():
        st = load(repo)
        if not (st.get("enabled") and st.get("execute_enabled")):
            continue
        with locked(repo):
            try:
                _st = load(repo)
                _st["last_dispatch"] = dispatch["last_tick"]
                _st["next_dispatch"] = dispatch["next_tick"]
                save(_st)
            except Exception:  # noqa: BLE001
                pass
            st = load(repo)
        if _pick_next(st) is None:
            continue
        ex = executor or _default_executor(model)
        try:
            executed[repo] = run_due(repo, ex, model=model)
        except Exception as exc:  # noqa: BLE001 - one repo never kills the tick
            executed[repo] = {"repo": repo, "ran": False,
                              "reason": f"{type(exc).__name__}: {exc}"[:200]}
    m2out = {}
    for repo in repos.list_all():
        try:
            from . import m2 as _m2
            m2out[repo] = _m2.tick_repo(repo)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - M2
            # never kills a tick (corrupt JSON raises SystemExit;
            # KeyboardInterrupt still propagates).
            m2out[repo] = {"ran": False,
                           "reason": f"{type(exc).__name__}: {exc}"[:200]}
    return {"scanned": scanned, "executed": executed, "m2": m2out,
            "dispatch": dispatch}


def _default_executor(model=None):
    from . import runner
    return runner.make_executor("muse", model=model)


def cancel(repo: str, key: str, reason: str | None = None) -> dict:
    """Non-destructive cancel: the item stays in the queue as cancelled
    (history preserved), with the reason recorded on the item and the
    event timeline. `reason` carries stale-supersede explanations;
    operator cancels use the default text."""
    with locked(repo):
        st = load(repo)
        for item in st["queue"]:
            if item.get("key") != key:
                continue
            if item.get("status") in ("queued", "blocked"):
                item["status"] = "cancelled"
                item["next_retry_at"] = None
                if reason:
                    item["note"] = reason[:300]
                st["history"].append({"key": key, "at": utcnow(),
                                      "result": "cancelled",
                                      "run_id": item.get("run_id")})
                save(st)
                events.append(repo, "cancelled", key,
                              reason or "operator cancelled queued item", "")
                return {"cancelled": key, "status": "cancelled"}
            if item.get("status") == "waiting-review":
                item["status"] = "cancelled"
                if reason:
                    item["note"] = reason[:300]
                st["history"].append({"key": key, "at": utcnow(),
                                      "result": "cancelled",
                                      "run_id": item.get("run_id")})
                save(st)
                events.append(repo, "cancelled", key,
                              reason or "operator cancelled review item", "")
                return {"cancelled": key, "status": "cancelled"}
            if item.get("status") == "running":
                # A synchronous worker can't be killed mid-step from here;
                # flag it so the finalize path records the operator intent.
                item["cancel_requested"] = True
                save(st)
                events.append(repo, "cancel-requested", key,
                              "operator asked to cancel a running job; "
                              "it will be marked cancelled at finalize", "")
                return {"cancelled": key, "note": "running; flagged for finalize"}
        return {"cancelled": None, "reason": "no such active item"}
