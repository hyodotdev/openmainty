"""Relationship graph backed by actual evidence (no fabricated nodes).

Nodes: repo, dependency (from the last scan rows), queue work items,
run, verification (only fresh: step done AND verified for the current
work commit), branch (repo-scoped, planned vs created), issue
(repo-scoped via the owner/repo slug, only with an explicit reference,
a run/queue-owned numeric issue, or a persisted chat reference),
question (only a live operator question).

Grounding rules (core7 revision):

* A run/queue-owned numeric ``issue`` creates a repo-scoped issue node
  (``issue:<slug>#<n>``) with the actual stored title as the main
  label and the technical ``slug#n`` as the secondary field, plus an
  ``about`` edge issue -> run/work. Synthetic ids (``dep-xxx``,
  empties, non-numerics) are never issue refs.
* Persisted chat sessions contribute their owned numeric issue and
  explicit ``mentions`` edges to referenced numbers (relation is
  MENTIONS/REFERENCE unless structured proof says otherwise — a mere
  mention never becomes a fixes link). Titles come from the stored
  session/run data plus the persisted refs cache.
* The refs cache (``<home>/refs/``) is read, never fetched: the graph
  performs zero GH/network calls per poll. Absent cache renders
  ``object_kind: unknown`` truthfully.
* Branch nodes distinguish ``planned`` (scan routing only — no branch
  was created) from ``created`` (a run actually recorded the branch),
  with ``exists`` verified once per repo per build against local refs
  (read-only ``git show-ref`` in the linked checkout, no mutation, no
  fetch). Unreadable/missing checkouts render ``unknown``.
* Every edge is validated: both endpoints must exist, every edge
  carries its source provenance, and no stale verified/answered-blocked
  status is ever emitted. Saved history is legacy proof, never current
  verified.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from . import events, paths, repos, sched, store

ISSUE_REF_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(\d+)")
_NUM_RE = re.compile(r"^[0-9]+$")


def valid_issue_number(raw) -> str | None:
    """Canonical numeric issue id, or None when not a real GitHub number.

    Only actual positive integer ids qualify: synthetic run/queue ids
    such as ``dep-xxx``, empties, signs, and decimals never become
    issue nodes.
    """
    s = str(raw or "").strip()
    if s.startswith("#"):
        s = s[1:].strip()
    if not s or not _NUM_RE.match(s):
        return None
    try:
        n = int(s, 10)
    except ValueError:
        return None
    if n < 1:
        return None
    return str(n)


def issue_edges_from_text(text: str, source: str, repo_slug: str = "") -> list:
    """Issue edges with owner/repo scope. Bare `#n` resolves against the
    repo that stored the text; explicit `owner/repo#n` keeps its scope."""
    edges = []
    for m in ISSUE_REF_RE.finditer(text or ""):
        slug = (m.group(1) or repo_slug or "").strip()
        num = m.group(2)
        target = f"issue:{slug}#{num}" if slug else f"issue:#{num}"
        edges.append({"from": source, "to": target,
                      "kind": "related",
                      "provenance": f"explicit reference '{m.group(0)}' in {source}"})
    # cap: graph stays readable
    return edges[:20]


def _read_live_snapshot(run: dict):
    """Fresh source snapshot for a read path, or None.

    Returns None when no verified step could render fresh anyway
    (nothing to prove — skip the filesystem read) or when the worktree
    cannot be read. Read-only: never saves, never mutates user files.
    One snapshot covers head + fingerprint so callers never hash twice.
    Snapshots are memoised on the worktree signature (see
    _memo_live_snapshot): identical bytes serve the identical
    snapshot, any change re-reads.
    """
    steps = run.get("steps") or {}
    vfps = run.get("verifiedFingerprints") or {}
    candidate = any(steps.get(s, {}).get("status") == "done" and vfps.get(s)
                    for s in store.VERIFY_STEPS)
    if not candidate:
        return None
    try:
        work = store.run_dir(run["id"]) / "work"
    except Exception:  # noqa: BLE001 - unreadable means unverified
        return None
    return _memo_live_snapshot(run, work)


_SNAP_MEMO = {}
_SNAP_MEMO_CAP = 1024
# Backstop for excludes-UNCERTAIN worktrees only (include/includeIf
# in scope, or otherwise unresolvable excludes inputs): those
# re-read after at most TTL seconds. Fully resolved worktrees
# revalidate purely by signature with no timer expiry.
_SNAP_MEMO_TTL = 300.0
# Test seam for the memo clock (F5): tests pin this to control memo
# time explicitly instead of writing absolute timestamps. Production
# always uses the real monotonic clock.
_mono = time.monotonic


# Persistent snapshot seed (261 restart-cold): the in-memory memo
# starts empty after every dashboard restart, so the first graph
# open re-hashes every worktree (~24 s on live data). Each build
# persists its memo here; after a restart, entries whose stored
# signature still equals the freshly computed one serve without
# re-hashing. Trust rule is identical to the memory memo —
# signature match, plus the TTL for excludes-uncertain worktrees
# (wall-clock age; future-dated entries re-read fail-closed) — so
# a changed signature, a corrupt file, or an unreadable source can
# never serve stale proof. Atomic tmp+replace: readers only ever
# see complete files. Best effort: any failure degrades to "no
# seed" and the build recomputes in memory as before.
_SNAP_DISK_VERSION = 1
_SNAP_DISK_NAME = "graph_snapshots.json"
_SNAP_DISK_LOADED = {"key": None, "entries": {}}
_SNAP_DISK_LOCK = threading.Lock()
# Digest of the seed content last written or read: the store
# rewrites the file only when the merged content differs, so quiet
# polls cost zero SSD writes.
_SNAP_DISK_KNOWN = {"digest": None}


def _snap_disk_path():
    return paths.home() / "cache" / _SNAP_DISK_NAME


def _snap_disk_replace(src, dst):
    """Atomically publish a fully written tmp file (seam for tests)."""
    os.replace(src, dst)


def _snap_disk_content_digest(shaped):
    """sha256 over canonical {key: {sig, snap}} content.

    Walls are deliberately excluded: they are recomputed re-read
    timestamps, not content, and including them would rewrite the
    file on every build. Skipping a wall-only difference keeps the
    older (fail-closed direction) timestamps.
    """
    canon = {key: {"sig": item["sig"], "snap": item["snap"]}
             for key, item in shaped.items()}
    payload = json.dumps({"v": _SNAP_DISK_VERSION, "entries": canon},
                         sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _snap_disk_valid_entry(entry):
    """(sig, snap, wall) for a well-shaped disk entry, else None.

    Strict: sig non-empty, snap a dict with the source_snapshot
    shape (kind/head/fingerprint/files/untracked, fingerprint
    non-empty), wall a real number. Anything else is never served.
    """
    if not isinstance(entry, dict):
        return None
    sig = entry.get("sig")
    snap = entry.get("snap")
    wall = entry.get("wall")
    if not isinstance(sig, str) or not sig:
        return None
    if not isinstance(snap, dict):
        return None
    if snap.get("kind") not in ("git", "copy"):
        return None
    if not isinstance(snap.get("head"), str):
        return None
    if not isinstance(snap.get("fingerprint"), str) \
            or not snap["fingerprint"]:
        return None
    if not isinstance(snap.get("files"), int):
        return None
    untracked = snap.get("untracked")
    if not isinstance(untracked, list) \
            or not all(isinstance(x, str) for x in untracked):
        return None
    if isinstance(wall, bool) or not isinstance(wall, (int, float)):
        return None
    return sig, snap, float(wall)


def _snap_disk_load():
    """Validated {path key: (sig, snap, wall)} from the disk seed.

    Parsed content is cached per (path, size, mtime) so a miss costs
    one stat; any content change re-parses. Version mismatch,
    corrupt JSON, or a missing file all read as empty (recompute).
    Never raises.
    """
    global _SNAP_DISK_LOADED
    try:
        path = _snap_disk_path()
    except Exception:  # noqa: BLE001 - no seed without a home
        return {}
    try:
        st = path.stat()
    except OSError:
        _SNAP_DISK_LOADED = {"key": None, "entries": {}}
        _SNAP_DISK_KNOWN["digest"] = _snap_disk_content_digest({})
        return {}
    key = (str(path), st.st_size, st.st_mtime_ns)
    cached = _SNAP_DISK_LOADED
    if cached.get("key") == key:
        return cached.get("entries") or {}
    entries = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict) and raw.get("v") == _SNAP_DISK_VERSION \
            and isinstance(raw.get("entries"), dict):
        for ekey, entry in raw["entries"].items():
            if not isinstance(ekey, str) or not ekey:
                continue
            valid = _snap_disk_valid_entry(entry)
            if valid is not None:
                entries[ekey] = valid
            if len(entries) >= _SNAP_MEMO_CAP:
                break
    _SNAP_DISK_LOADED = {"key": key, "entries": entries}
    shaped = {ekey: {"sig": sig, "snap": snap}
              for ekey, (sig, snap, _wall) in entries.items()}
    _SNAP_DISK_KNOWN["digest"] = _snap_disk_content_digest(shaped)
    return entries


def _snap_disk_lookup(key, sig, work):
    """Disk-seed snapshot for a memory miss, or None.

    Served only when the stored signature equals the freshly
    computed one; excludes-uncertain worktrees additionally need a
    fresh wall-clock age (0 <= age < TTL — a future-dated entry,
    i.e. the clock moved back, re-reads fail-closed). The returned
    timestamp preserves the original re-read time so a promoted
    uncertain entry keeps its remaining TTL instead of extending
    it. Never raises; the served snapshot is an owned copy.
    """
    try:
        entries = _snap_disk_load()
    except Exception:  # noqa: BLE001 - unreadable seed recomputes
        return None
    hit = entries.get(key)
    if hit is None:
        return None
    stored_sig, snap, wall = hit
    if stored_sig != sig:
        return None
    try:
        from . import runner as _runner
        if _runner.worktree_signature_backstop(work):
            age = time.time() - wall
            if not 0 <= age < _SNAP_MEMO_TTL:
                return None
    except Exception:  # noqa: BLE001 - doubt re-reads
        return None
    kept_ts = _mono() - max(0.0, time.time() - wall)
    return copy.deepcopy(snap), kept_ts


def _snap_disk_store():
    """Merge the memory memo into the disk seed. Never raises.

    Memory entries win; disk-only entries survive (a filtered build
    that visits nothing new must not wipe the seed); the total is
    capped. Memory timestamps convert to wall clock preserving each
    entry's age, so uncertain entries keep their TTL across the
    restart. Writes only when the merged content differs from what
    was last written or read. Atomic unique-tmp+replace under a
    lock; a failed publish removes only the tmp just created.
    Returns True when it wrote, False when it skipped or failed.
    """
    try:
        with _SNAP_DISK_LOCK:
            path = _snap_disk_path()
            disk = _snap_disk_load()
            wall_now = time.time()
            mono_now = _mono()
            merged = {}
            for key, item in list(_SNAP_MEMO.items()):
                try:
                    sig, snap, ts = item
                except (TypeError, ValueError):
                    continue
                if not isinstance(key, str) or not key:
                    continue
                if not isinstance(sig, str) or not sig:
                    continue
                if not isinstance(snap, dict):
                    continue
                try:
                    json.dumps(snap)
                except (TypeError, ValueError):
                    continue
                try:
                    age = mono_now - ts
                except TypeError:
                    continue
                if age < 0:
                    age = 0
                merged[key] = {"sig": sig, "snap": snap,
                               "wall": wall_now - age}
                if len(merged) >= _SNAP_MEMO_CAP:
                    break
            for key, item in disk.items():
                if len(merged) >= _SNAP_MEMO_CAP:
                    break
                if key not in merged:
                    sig, snap, wall = item
                    merged[key] = {"sig": sig, "snap": snap, "wall": wall}
            digest = _snap_disk_content_digest(merged)
            if digest == _SNAP_DISK_KNOWN.get("digest"):
                return False
            payload = json.dumps({"v": _SNAP_DISK_VERSION,
                                  "entries": merged}, sort_keys=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / ("%s.%d.%s.tmp"
                                 % (_SNAP_DISK_NAME, os.getpid(),
                                    secrets.token_hex(8)))
            try:
                tmp.write_text(payload, encoding="utf-8")
                _snap_disk_replace(tmp, path)
            except Exception:  # noqa: BLE001 - drop only our own tmp
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return False
            _SNAP_DISK_KNOWN["digest"] = digest
            return True
    except Exception:  # noqa: BLE001 - the seed never breaks a build
        return False


def _memo_live_snapshot(run, work):
    """fresh_snapshot memoised on the worktree signature (L1c, P1).

    Hits serve a copy of the identical snapshot (callers here only
    read it; the copy keeps a future mutation from poisoning the
    cache). The scope cache is refreshed first (at most one `git
    config` subprocess per window, usually none). The entry is
    revalidated by signature on every call because the signature
    covers HEAD, refs, index, every file and the resolved global
    exclude/config inputs; only excludes-uncertain worktrees
    additionally expire after _SNAP_MEMO_TTL. A worktree is
    certain only with no includes, resolvable excludes inputs,
    and a truthy GIT_CONFIG_NOSYSTEM or a fresh valid scope cache
    for the system side. Staleness rule: edits to known files
    invalidate immediately; a scope file CREATED after the last
    refresh is seen at the next refresh, at most one window
    later. Any covered change re-reads immediately; unreadable
    worktrees never hit: they re-read (None) and evict any older
    entry, so unknown is always reported, never a stale green.
    On a memory miss the persistent disk seed (261, written by
    build) is consulted under the identical trust rule before any
    re-hash, so a restart costs signatures, not snapshots.
    """
    from . import runner as _runner
    _runner.refresh_scope_cache()
    key = str(work)
    try:
        sig = _runner.worktree_signature(work)
    except Exception:  # noqa: BLE001 - unreadable means re-read
        sig = None
    now = _mono()
    if sig is not None:
        hit = _SNAP_MEMO.get(key)
        if hit and hit[0] == sig and (
                not _runner.worktree_signature_backstop(work)
                or now - hit[2] < _SNAP_MEMO_TTL):
            return copy.deepcopy(hit[1])
        disk_hit = _snap_disk_lookup(key, sig, work)
        if disk_hit is not None:
            disk_snap, disk_ts = disk_hit
            if len(_SNAP_MEMO) >= _SNAP_MEMO_CAP:
                _SNAP_MEMO.clear()
            _SNAP_MEMO[key] = (sig, disk_snap, disk_ts)
            return copy.deepcopy(disk_snap)
    try:
        snap = _runner.fresh_snapshot(run)
    except Exception:  # noqa: BLE001 - unreadable means unverified
        snap = None
    if sig is not None and snap is not None:
        if len(_SNAP_MEMO) >= _SNAP_MEMO_CAP:
            _SNAP_MEMO.clear()
        _SNAP_MEMO[key] = (sig, snap, now)
        return copy.deepcopy(snap)
    _SNAP_MEMO.pop(key, None)
    return snap


def _read_fingerprint(run: dict):
    """Fresh content fingerprint for a read path, or store.USE_STORED.

    Returns USE_STORED when no verified step could render fresh anyway
    (nothing to prove — skip the filesystem read); otherwise the live
    fingerprint, or None when the worktree cannot be read. Read-only:
    never saves, never mutates user files.
    """
    snap = _read_live_snapshot(run)
    if snap is None:
        # No candidate, or unreadable: callers that need the distinction
        # use _read_live_snapshot directly; here preserve the legacy
        # contract (USE_STORED vs None) via a cheap readability probe.
        from . import runner as _runner
        steps = run.get("steps") or {}
        vfps = run.get("verifiedFingerprints") or {}
        candidate = any(steps.get(s, {}).get("status") == "done" and vfps.get(s)
                        for s in store.VERIFY_STEPS)
        if not candidate:
            return store.USE_STORED
        return None
    return snap["fingerprint"]


def _live_head_of(snap) -> object:
    """Live HEAD for proof validation: the readable git HEAD, else None.

    A missing/unreadable HEAD is unknown — never the stored commit.
    Non-git snapshots carry no commit identity ("unknown"), so they
    report None here and proof falls back to fingerprint-only honesty
    via unknown, never a fabricated commit match.
    """
    if not isinstance(snap, dict):
        return None
    head = snap.get("head")
    return head if head not in (None, "", "unknown") else None


def _fresh_verifications(run: dict,
                         current_fingerprint=store.USE_STORED,
                         current_head=store.USE_STORED) -> list:
    """Only verifications bound to the CURRENT source content.

    A step marked done for different bytes, for a moved HEAD with
    identical bytes, or with no content proof at all (legacy runs) — is
    stale/historical evidence, not a verified node. HEAD equality alone
    never qualifies: a dirty tree keeps HEAD; fingerprint equality alone
    never qualifies either: a forward commit can restore identical bytes.
    Read paths pass a freshly read head AND fingerprint (None when
    unreadable) so proof cannot stay CURRENT after the source moved.
    """
    status = store.verification_status(
        run, current_fingerprint=current_fingerprint,
        current_head=current_head)
    return [s for s in ("reproduce", "review", "e2e")
            if status.get(s) == "fresh"]


def _run_status(run: dict) -> str:
    """Run lifecycle state for the graph.

    Durable triage verdicts (core6 ``disposition``) render their actual
    outcome: a terminal DUPLICATE is never ``active`` forever just
    because unused steps sit pending, and INFO-NEEDED/UNKNOWN waits
    render waiting even before the ``question:`` text lands.
    """
    disp = run.get("disposition")
    kind = disp.get("kind") if isinstance(disp, dict) else None
    wait = run.get("waitReason") or ""
    if kind == "DUPLICATE" or wait.startswith("duplicate:"):
        return "duplicate"
    if kind == "EXTERNAL-WATCH" or wait.startswith("watch:"):
        return "watch"
    if wait.startswith("question:"):
        return "waiting"
    if kind in ("INFO-NEEDED", "UNKNOWN"):
        return "waiting"
    steps = run.get("steps", {}) or {}
    if steps and all(e.get("status") == "done" for e in steps.values()):
        return "done"
    if any(e.get("status") not in store.TERMINAL for e in steps.values()):
        return "active"
    if any(e.get("status") == "blocked" for e in steps.values()):
        return "blocked"
    if any(e.get("status") == "failed" for e in steps.values()):
        return "failed"
    return "active"


def _cached_ref(github: str, number: str) -> dict | None:
    """Persisted refs-cache metadata only — never fetches, never network.

    Returns the stored entry dict, or None when nothing was ever cached
    (or it is unreadable). Callers render ``unknown`` in that case.
    """
    try:
        from . import refs as _refs
        path = _refs.cache_file(github or "", number)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _issue_scope(repo_name: str, github: str) -> str:
    """Repo scope for an issue node: the owner/repo slug when known,
    else the registered repo name. Same number in different repos (or
    slugs) always yields separate nodes."""
    return (github or "").strip() or repo_name


def _issue_id(scope: str, number: str) -> str:
    return f"issue:{scope}#{number}"


def _local_ref_branches(checkout) -> set | None:
    """Local branch names from read-only ``git show-ref`` (heads plus
    remote-tracking, tags excluded), or None when unreadable.

    No mutation, no fetch, no network: a missing/unreadable checkout
    renders ``unknown``, never a fabricated exists flag.
    """
    if checkout is None:
        return None
    try:
        from .util import run_cmd as _run
        code, out, _ = _run(["git", "-C", str(checkout), "show-ref"],
                            timeout=60)
    except Exception:  # noqa: BLE001 - unreadable means unknown
        return None
    if code != 0:
        return None
    names = set()
    for line in (out or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if ref.startswith("refs/heads/"):
            names.add(ref[len("refs/heads/"):])
        elif ref.startswith("refs/remotes/"):
            rest = ref[len("refs/remotes/"):]
            _, _, branch = rest.partition("/")
            if branch and branch != "HEAD":
                names.add(branch)
    return names


def _checkout_for(repo_name: str):
    """Linked checkout dir for local ref reads, or None (unknown)."""
    try:
        from . import pcconfig
        co = (pcconfig.load().get("checkouts", {}) or {}).get(repo_name)
    except Exception:  # noqa: BLE001 - config unreadable means unknown
        return None
    if not co:
        return None
    path = Path(co)
    return path if path.is_dir() else None


def build(repo: str = "") -> dict:
    names = [repo] if repo else repos.list_all()
    nodes, edges = [], []
    seen = set()
    by_id = {}

    def add(node):
        if node["id"] not in seen:
            seen.add(node["id"])
            by_id[node["id"]] = node
            nodes.append(node)

    def link(edge):
        # Registered now; validated (both endpoints exist) at the end.
        edges.append(edge)

    def ensure_issue(repo_name: str, github: str, number: str,
                     title: str = "", source_chat: str = "",
                     provenance: str = "") -> str:
        """Repo-scoped issue node (deduped across run/queue/chat/text).

        The main label is the actual human title where stored; the
        technical ``scope#n`` id stays available as ``technical_label``.
        Cached kind/title/url come from the persisted refs cache only.
        Merges provenance on repeat encounters; never downgrades a
        known title to a fallback.
        """
        scope = _issue_scope(repo_name, github)
        iid = _issue_id(scope, number)
        prov = (provenance or f"issue #{number} in {repo_name}")[:300]
        meta = _cached_ref(github, number) if github else None
        object_kind, cached_title, url = "unknown", "", ""
        if isinstance(meta, dict) and meta.get("kind") in ("issue", "pr"):
            object_kind = meta["kind"]
            cached_title = str(meta.get("title") or "")
            url = str(meta.get("url") or "")
        human = (title or "").strip() or cached_title
        cur = by_id.get(iid)
        if cur is None:
            node = {"id": iid, "type": "issue",
                    "label": human or f"{scope}#{number}",
                    "technical_label": f"{scope}#{number}",
                    "title": human, "repo": repo_name,
                    "github": github or "", "issue": number,
                    "object_kind": object_kind, "url": url,
                    "status": "ref", "detail": prov,
                    "sources": [prov]}
            if source_chat:
                node["source_chat"] = source_chat
            add(node)
            return iid
        # Merge: provenance always; title only when still unknown.
        sources = cur.setdefault("sources", [])
        if prov not in sources:
            sources.append(prov)
            cur["detail"] = prov
        if not cur.get("title") and human:
            cur["title"] = human
            cur["label"] = human
        if object_kind != "unknown":
            if cur.get("object_kind", "unknown") == "unknown":
                cur["object_kind"] = object_kind
                cur["url"] = url or cur.get("url", "")
            if not cur.get("title") and cached_title:
                cur["title"] = cached_title
                cur["label"] = cached_title
        if source_chat and not cur.get("source_chat"):
            cur["source_chat"] = source_chat
        return iid

    def upsert_branch(repo_name: str, github: str, branch: str,
                      created: bool, exists, provenance: str) -> str:
        """Branch node with a truthful planned/created/unknown status.

        Scan routing only *plans* a branch — nothing is created. A run
        that actually recorded the branch upgrades the node to
        ``created`` (an earlier planned entry never freezes the node:
        creation wins, never downgrades back to planned).
        """
        bid = f"branch:{repo_name}:{branch}"
        prov = provenance[:300]
        if created:
            status = "created"
        elif exists is None:
            status = "unknown"
        else:
            status = "planned"
        cur = by_id.get(bid)
        if cur is None:
            add({"id": bid, "type": "branch", "label": branch,
                 "technical_label": bid, "title": branch,
                 "repo": repo_name, "github": github or "",
                 "planned": not created, "exists": exists,
                 "status": status, "source": prov,
                 "detail": f"lane repo={repo_name} {prov}"[:300],
                 "sources": [prov]})
            return bid
        sources = cur.setdefault("sources", [])
        if prov not in sources:
            sources.append(prov)
        if created:
            # Actual run-branch creation updates the earlier plan entry.
            cur["planned"] = False
            cur["status"] = "created"
            cur["exists"] = exists
            cur["source"] = prov
            cur["detail"] = f"lane repo={repo_name} {prov}"[:300]
        elif cur.get("exists") is None and exists is not None:
            cur["exists"] = exists
            if cur.get("status") == "unknown" and not created:
                cur["status"] = "planned"
        return bid

    # L1(243): scan the home once per build, not once per repo. The
    # per-repo passes only filter these snapshots; results are identical
    # (and a mid-build chat write can no longer skew repos differently).
    # L10h: the truncation metadata comes from this same scan, so it
    # can never disagree with the built nodes. It is recomputed on
    # every build (builds are not memoized), and the per-worktree P1
    # memo below is untouched: worktree entries keep their signature
    # revalidation, TTL backstop and fail-closed unknowns.
    all_runs, runs_total = store.list_runs_with_total(200)
    try:
        from . import chat as _chat_mod
        all_sessions = _chat_mod.list_sessions(limit=500)
    except Exception:  # noqa: BLE001 - chats are optional evidence
        all_sessions = []
    for name in names:
        try:
            profile = repos.load(name)
        except SystemExit:
            continue
        slug = profile.get("github", "")
        add({"id": f"repo:{name}", "type": "repo", "label": name,
             "status": "ok", "detail": slug})
        # Local refs, read once per repo per build (read-only, no fetch).
        local_branches = _local_ref_branches(_checkout_for(name))

        def _exists(branch: str):
            if local_branches is None:
                return None
            return branch in local_branches

        st = sched.load(name)
        # -- dependency nodes from the actual last scan rows --
        for row in st.get("last_rows", []) or []:
            if row.get("kind") in ("note", None) and not row.get("name"):
                continue
            if not row.get("name"):
                continue
            did = f"dep:{name}:{row['name']}"
            status = ("drift" if row.get("drift") else
                      ("error" if row.get("error") else "ok"))
            detail = (f"declared={row.get('declared')} "
                      f"installed={row.get('installed')} "
                      f"latest={row.get('latest')} "
                      f"[{row.get('update_kind', '')} lane={row.get('lane', '')}] "
                      f"{row.get('reason') or row.get('note') or ''} "
                      f"@ {row.get('checked_at', '')}").strip()
            add({"id": did, "type": "dependency", "label": row["name"],
                 "status": status, "detail": detail[:500]})
            link({"from": f"repo:{name}", "to": did, "kind": "tracks",
                  "provenance": f"{row.get('source', 'manifest')} @ "
                                f"{row.get('checked_at', '')}"})
        # -- queue work items (+history) --
        queue_items = list(st.get("queue", []) or [])
        for item in queue_items:
            dep = item.get("dep") or (
                f"issue-{item.get('issue')}" if item.get("issue") else
                item.get("key", "?"))
            qid = f"queue:{name}:{item.get('key', dep)}"
            raw_status = item.get("status", item.get("result", "queued"))
            # An actually-watching queue item renders as an external wait,
            # never as a human review: the stored raw status stays on the
            # node (raw_status) and in the scheduler untouched. A
            # cancelled historical record stays cancelled even if it
            # carries an old watch payload.
            status = raw_status
            _watch = item.get("watch") or {}
            if _watch.get("active") and raw_status != "cancelled":
                status = "watch"
            item_title = (item.get("title") or item.get("target")
                          or dep or "")
            label = f"{dep} -> {item.get('target', item.get('title', '?'))}"
            _detail = (f"{item.get('update_kind', item.get('kind', ''))} "
                       f"lane={item.get('lane', '')} "
                       f"attempts={item.get('attempts', 0)} "
                       f"{item.get('why', '')[:200]}").strip()
            if status == "watch":
                _detail = (f"{_detail} watch={str(_watch.get('reason', ''))[:200]} "
                           f"trigger={str(_watch.get('trigger', ''))[:200]}").strip()
            add({"id": qid, "type": "work",
                 "label": label, "technical_label": label,
                 "title": str(item_title)[:200],
                 "repo": name, "github": slug,
                 "issue": valid_issue_number(item.get("issue")) or "",
                 "status": status, "raw_status": raw_status,
                 "detail": _detail})
            link({"from": f"repo:{name}", "to": qid, "kind": "proposes",
                  "provenance": f"scheduler queue key {item.get('key')} "
                                f"({item.get('created_at', '')})"})
            # Owned numeric issue -> queue work link (repo-scoped, deduped).
            qnum = valid_issue_number(item.get("issue"))
            if qnum:
                iid = ensure_issue(
                    name, slug, qnum, title=str(item.get("title") or ""),
                    provenance=f"scheduler queue key {item.get('key')} "
                               f"owns issue #{qnum} (repo {name})")
                link({"from": iid, "to": qid, "kind": "about",
                      "provenance": f"scheduler queue key {item.get('key')} "
                                    f"issue {qnum} (repo {name})"})
            if item.get("dep") and item.get("target"):
                dep_id = f"dep:{name}:{item['dep']}"
                if dep_id in seen:
                    link({"from": dep_id, "to": qid, "kind": "drives",
                          "provenance": f"scan row {item['dep']} "
                                        f"latest={item.get('target')}"})
            if item.get("branch"):
                bid = upsert_branch(
                    name, slug, item["branch"], created=False,
                    exists=_exists(item["branch"]),
                    provenance="branch routing recorded at scan time "
                               f"(queue key {item.get('key')}; planned, "
                               "not created)")
                link({"from": qid, "to": bid, "kind": "targets",
                      "provenance": "branch routing recorded at scan time "
                                    "(planned)"})
            if item.get("run_id"):
                rid = f"run:{item['run_id']}"
                # Endpoint may not be built yet; validated below.
                link({"from": qid, "to": rid, "kind": "executed-as",
                      "provenance": f"queue item run_id {item['run_id']}"})
        # -- runs, fresh verifications, branches, live questions only --
        for run in [r for r in all_runs if r.get("repo") == name]:
            rid = f"run:{run['id']}"
            status = _run_status(run)
            run_title = run.get("title", "") or ""
            tech = f"{run['id']} #{run.get('issue')}"
            detail_bits = [run_title[:120]]
            if run.get("lane") or run.get("branch"):
                detail_bits.append(
                    f"lane={run.get('lane', '')} branch={run.get('branch', '')} "
                    f"base={run.get('base_branch', '')}")
            if status == "duplicate":
                disp = run.get("disposition") or {}
                detail_bits.append(
                    "terminal duplicate verdict "
                    f"({str(disp.get('detail', ''))[:160]}; no code change — "
                    "not active, never retried/forked)")
            # Read-time validation: an external worktree edit — or a
            # moved HEAD with identical bytes — must not keep rendering
            # verified until the next resume. Unreadable current source
            # renders unverified, never green.
            _snap = _read_live_snapshot(run)
            if _snap is None:
                fresh_fp = _read_fingerprint(run)
                fresh_head = store.USE_STORED if fresh_fp is store.USE_STORED else None
            elif (_snap.get("kind") or "unknown") != "git":
                # Non-git snapshots carry no commit identity: validate
                # proof against the recorded commit (as before) with the
                # live bytes that were actually read.
                fresh_fp = _snap["fingerprint"]
                fresh_head = store.USE_STORED
            else:
                fresh_fp = _snap["fingerprint"]
                fresh_head = _live_head_of(_snap)
            proof = store.verification_status(
                run, current_fingerprint=fresh_fp,
                current_head=fresh_head)
            legacy = sorted(s for s, v in proof.items() if v == "legacy")
            unknown = sorted(s for s, v in proof.items() if v == "unknown")
            if unknown:
                detail_bits.append(
                    "current source unreadable; proof unverified (not current): "
                    + ",".join(unknown))
            if legacy:
                detail_bits.append(
                    "legacy source proof (historical, not current verified): "
                    + ",".join(legacy))
            rnum = valid_issue_number(run.get("issue"))
            add({"id": rid, "type": "run",
                 "label": run_title[:200] or tech,
                 "technical_label": tech,
                 "title": run_title[:200], "repo": name,
                 "github": slug, "issue": rnum or "",
                 "run_id": run["id"],
                 "status": status, "detail": " · ".join(b for b in detail_bits if b)})
            link({"from": f"repo:{name}", "to": rid, "kind": "executed",
                  "provenance": f"run.json created_at {run.get('created_at')}"})
            # Owned numeric issue -> run link (repo-scoped, deduped with
            # the queue/chat copies of the same own issue).
            if rnum:
                iid = ensure_issue(
                    name, slug, rnum, title=run_title,
                    provenance=f"run.json {run['id']} owns issue #{rnum} "
                               f"(repo {name})")
                link({"from": iid, "to": rid, "kind": "about",
                      "provenance": f"run.json {run['id']} issue {rnum} "
                                    f"(repo {name})"})
            for step in _fresh_verifications(run, fresh_fp, fresh_head):
                vid = f"verify:{run['id']}:{step}"
                vc = (run.get("verifiedCommits", {}) or {})[step]
                vf = (run.get("verifiedFingerprints", {}) or {})[step]
                add({"id": vid, "type": "verification",
                     "label": f"{step} @ {str(vc)[:12]}",
                     "status": "verified",
                     "detail": ", ".join((run.get("steps", {}).get(step, {})
                                           .get("evidence") or [])[:3])})
                link({"from": rid, "to": vid, "kind": "verified-by",
                      "provenance": f"verifiedCommits.{step} matches workCommit "
                                    f"{str(run.get('workCommit'))[:12]} and "
                                    f"verifiedFingerprints.{step} "
                                    f"{str(vf)[:12]} matches current source"})
            if run.get("branch"):
                bid = upsert_branch(
                    name, slug, run["branch"], created=True,
                    exists=_exists(run["branch"]),
                    provenance=f"run.json branch {run['branch']} "
                               f"(base {run.get('base_branch', '')}; "
                               "actually recorded, not a scan plan)")
                link({"from": rid, "to": bid, "kind": "works-on",
                      "provenance": f"run.json branch {run['branch']} "
                                    f"(base {run.get('base_branch', '')})"})
            # Only a LIVE operator question blocks; answered history is
            # resolved and must not render as blocked.
            if (run.get("waitReason") or "").startswith("question:"):
                qid2 = f"question:{run['id']}"
                qlabel = run["waitReason"][len("question:"):][:80]
                add({"id": qid2, "type": "question",
                     "label": qlabel, "technical_label": qid2,
                     "title": qlabel, "repo": name, "github": slug,
                     "status": "waiting", "detail": str(run.get("question_step", ""))})
                link({"from": rid, "to": qid2, "kind": "blocked-by",
                      "provenance": "run.json waitReason (live question)"})
            # Issue references with owner/repo scope.
            blob = f"{run.get('title', '')} {(run.get('interventions') or []) and '' or ''}"
            for edge in issue_edges_from_text(blob, rid, slug):
                _add_issue_edge(ensure_issue, link, edge,
                                repo_name=name, github=slug)
        # -- persisted chat evidence: owned issues + explicit mentions --
        _chat_mentions(name, slug, ensure_issue, link, all_sessions)
        # -- event-backed issue references (repo -> scoped issue) --
        for ev in events.list_events(name, limit=50):
            for edge in issue_edges_from_text(ev.get("detail", ""),
                                              f"repo:{name}", slug):
                _add_issue_edge(ensure_issue, link, edge,
                                repo_name=name, github=slug,
                                at=ev.get("at", ""), kind=ev.get("kind", ""))
    valid = [e for e in edges if e["from"] in seen and e["to"] in seen]
    try:
        _snap_disk_store()
    except Exception:  # noqa: BLE001 - the seed never breaks a build
        pass
    return {"nodes": nodes, "edges": valid,
            "limits": {"runs": {"included": len(all_runs),
                                "total": runs_total}}}


def _chat_mentions(repo_name: str, github: str, ensure_issue, link,
                   sessions) -> None:
    """Owned-issue and explicit-reference edges from persisted chats.

    Local persisted sessions only (``session_card`` reads saved logs and
    drafts — quota-free, no network, no mutation). Each session's owned
    numeric issue merges into the repo-scoped issue node (deduped with
    the run/queue copies); each explicit related ref becomes a
    ``mentions`` edge from that owned issue node with the actual source
    chat provenance plus cached kind/title/url. A mere mention never
    becomes a fixes relationship. ``sessions`` is the build-wide
    snapshot (L1(243): listed once per build, filtered per repo).
    """
    try:
        from . import chat as _chat
    except Exception:  # noqa: BLE001 - chats are optional evidence
        return
    for session in sessions or []:
        try:
            if session.get("repo") != repo_name:
                continue
            card = _chat.session_card(session)
        except Exception:  # noqa: BLE001 - one bad session skips, not all
            continue
        if not isinstance(card, dict):
            continue
        own = valid_issue_number(card.get("issue") or session.get("issue"))
        if not own:
            continue
        sid = session.get("id", "")
        own_title = (card.get("title") or session.get("title") or "")
        iid = ensure_issue(
            repo_name, github, own, title=str(own_title),
            source_chat=sid,
            provenance=f"persisted chat {sid} owns issue #{own} "
                       f"(repo {repo_name})")
        for rel in card.get("related") or []:
            try:
                num = valid_issue_number((rel or {}).get("issue"))
            except (AttributeError, TypeError):
                continue
            if not num or num == own:
                continue
            via = str((rel or {}).get("via") or "")
            ref_title = str((rel or {}).get("title") or "")
            ref_id = ensure_issue(
                repo_name, github, num, title=ref_title,
                source_chat=sid,
                provenance=f"persisted chat {sid} references #{num} "
                           f"(repo {repo_name})")
            link({"from": iid, "to": ref_id, "kind": "mentions",
                  "provenance": f"persisted chat {sid} ({via}) "
                                f"mentions #{num}"[:300]})


def _add_issue_edge(ensure_issue, link, edge, repo_name: str = "",
                    github: str = "", at: str = "", kind: str = "") -> None:
    """Free-text issue reference routed through the deduped issue node."""
    target = edge["to"]
    if not target.startswith("issue:"):
        return
    scope_num = target[len("issue:"):]
    if "#" in scope_num:
        scope, _, num = scope_num.rpartition("#")
    else:
        scope, num = "", scope_num
    num = valid_issue_number(num)
    if not num:
        return
    detail = edge["provenance"] + (f" (event {kind} @ {at})" if at else "")
    if scope and "/" in scope:
        # Explicit owner/repo scope keeps its own node identity
        # (same number in another slug stays a separate node).
        ensure_issue(repo_name, scope, num, provenance=detail[:300])
        node_id = _issue_id(scope, num)
    else:
        node_id = ensure_issue(repo_name, github, num,
                               provenance=detail[:300])
    link({"from": edge["from"], "to": node_id, "kind": edge["kind"],
          "provenance": detail[:300]})
