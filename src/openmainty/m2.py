"""M2 autonomy: policy toggles, promoted-only guard, op locks, events.

M2.1a covers the frame: per-repo stage toggles (default OFF), the
promoted-only guard with its plain glance message, a per-op exclusion
lock shared by prepare/reresolve/rollup/import/fastcheck, and Korean
timeline events for every flip and refusal. M2.1b-1 adds the daemon
driver for auto-prepare; M2.1b-2 adds the in-app fastcheck runner
(repo-profile steps, disk guard, cleanup) wired inline after each
auto-prepare. Batch/rollup stages in M2.2/M2.3.
"""
from __future__ import annotations

import contextlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import events, paths, promote, repos, sched

STAGES = ("prepare", "fastcheck", "batch", "rollup", "refresh")
_STAGE_KO = {"prepare": "준비", "fastcheck": "검증",
             "batch": "묶음", "rollup": "합치기", "refresh": "재실행"}
GUARD_MESSAGE = ("자율 실행은 검토된 앱 사본으로 전환한 뒤에만 동작합니다")
# Stages the server actually runs: prepare in M2.1b-1, fastcheck in
# M2.1b-2, batch/rollup in M2.2/M2.3. STAGE_FLIP governs the toggle
# itself: prepare/fastcheck stay flippable (their buttons are live
# since M2.1a), while batch/rollup flips are refused (F1) until
# M2.2/M2.3. A flipped-on stage that is not open yet never acts —
# the driver says "다음 단계에서 열림".
STAGE_OPEN = {"prepare": True, "fastcheck": True,
              "batch": True, "rollup": True, "refresh": True}
STAGE_FLIP = {"prepare": True, "fastcheck": True,
              "batch": True, "rollup": True, "refresh": True}
NOT_OPEN_NOTE = "다음 단계에서 열림"
# M2.5b: packages React Native publishes in lockstep with
# react-native itself. Bumping one without its react-native minor
# mixes two Metro/RN trees in one lockfile (live: metro-config
# 0.88 on an RN 0.86 workspace pulled metro 0.87.1 next to
# 0.84.4, and no fastcheck gate runs Metro). M2 never prepares or
# auto-batches one alone; the owner moves it with its RN bump.
RN_LOCKSTEP_FAMILY = ("@react-native/babel-preset",
                      "@react-native/metro-config",
                      "@react-native/jest-preset",
                      "@react-native/typescript-config",
                      "@react-native/eslint-config",
                      "@react-native/codegen",
                      "@react-native/gradle-plugin")
_RN_DEP_SECTIONS = ("dependencies", "devDependencies",
                    "optionalDependencies")


def _version_minor(ver: str):
    """(major, minor) from a declared range, or None when unreadable."""
    import re as _re
    m = _re.search(r"(\d+)\.(\d+)", str(ver or ""))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def lockstep_status(tree: str, dep: str, target: str):
    """M2.5b read-only lockstep check over one worktree directory.

    Returns None when the dep may go alone, else a dict with a
    Korean reason. Fires only when a workspace package.json (root
    or example) declares BOTH the family dep and react-native, and
    the target major.minor differs from the workspace's
    react-native. An unreadable tree holds as unverifiable (a
    grouped skip, retried next tick — never a block); unparseable
    versions proceed, documented as stable properties. No network.
    """
    dep = str(dep or "")
    if dep not in RN_LOCKSTEP_FAMILY:
        return None
    # M2.5c-N1: an empty tree would read Path("package.json")
    # against the daemon cwd — refuse explicitly instead.
    if not str(tree or ""):
        return {"reason": f"{dep} lockstep 확인 불가 — 작업실 경로 없음",
                "unverifiable": True, "workspace": "",
                "current": "", "target": str(target or "")}
    want = _version_minor(target)
    if want is None:
        return None
    from .util import read_json
    readable = False
    hits = []
    for rel in ("", "example"):
        try:
            pkg = read_json(Path(tree, rel, "package.json"),
                            default=None)
        except (Exception, SystemExit):  # noqa: BLE001 - unreadable
            continue
        if not isinstance(pkg, dict):
            continue
        readable = True
        label = rel or "root"
        declared = any(
            isinstance(pkg.get(key), dict) and dep in pkg.get(key)
            for key in _RN_DEP_SECTIONS)
        if not declared:
            continue
        current = None
        for key in _RN_DEP_SECTIONS:
            sect = pkg.get(key)
            if isinstance(sect, dict) and "react-native" in sect:
                current = str(sect["react-native"])
                break
        if current is None:
            continue
        if _version_minor(current) != want:
            hits.append((label, current))
    if not readable:
        return {"reason": f"{dep} lockstep 확인 불가 — 작업실 읽기 실패",
                "unverifiable": True, "workspace": "",
                "current": "", "target": str(target or "")}
    if not hits:
        return None
    label, current = hits[0]
    return {"reason": f"react-native {target} 함께 옮겨야 함 "
                      f"(현재 {current})",
            "unverifiable": False, "workspace": label,
            "current": current, "target": str(target or "")}


def _fresh_lockstep_held(base_id: str, lane: str, item) -> str | None:
    """Lockstep reason for a fresh candidate, or None.

    Read-only against the BASE integration worktree the candidate
    would prepare onto. Unreadable bases hold as unverifiable (a
    grouped skip — prepare itself would refuse there too). Never
    a block."""
    dep = str((item or {}).get("dep") or "")
    if dep not in RN_LOCKSTEP_FAMILY:
        return None
    target = str((item or {}).get("target") or "")
    try:
        from . import batches as _batches
        batch = _batches.load_batch(base_id)
        integs = [i for i in (batch.get("integrations") or [])
                  if isinstance(i, dict)]
        if len(integs) == 1:
            worktree = str(integs[0].get("worktree") or "")
        else:
            matches = [i for i in integs
                       if str(i.get("lane") or "") == lane]
            worktree = str(matches[0].get("worktree") or "") \
                if len(matches) == 1 else ""
    except (Exception, SystemExit):  # noqa: BLE001 - unreadable base
        return f"{dep} lockstep 확인 불가 — 기준 묶음 읽기 실패"
    if not worktree:
        return f"{dep} lockstep 확인 불가 — 기준 작업실 없음"
    st = lockstep_status(worktree, dep, target)
    return st["reason"] if st is not None else None


def _base_lane_worktree(base_id: str, lane: str) -> str:
    """M2.17: the base batch's lane integration worktree, else "".

    Same resolution as _fresh_lockstep_held (one integration, else
    the lane match). Read-only; unreadable bases give "" and the
    caller fails open — the stage itself decides. Never raises."""

    try:
        from . import batches as _batches
        batch = _batches.load_batch(base_id)
        integs = [i for i in (batch.get("integrations") or [])
                  if isinstance(i, dict)]
        if len(integs) == 1:
            return str(integs[0].get("worktree") or "")
        matches = [i for i in integs
                   if str(i.get("lane") or "") == lane]
        if len(matches) == 1:
            return str(matches[0].get("worktree") or "")
        return ""
    except (Exception, SystemExit):  # noqa: BLE001 - unreadable base
        return ""


def _base_declared_ranges(worktree: str) -> dict:
    """M2.17: {dep: set(ranges)} from root+example manifests.

    Read-only; unreadable manifests read as absent. Never
    raises."""

    declared: dict = {}
    try:
        from .util import read_json
        for rel in ("", "example"):
            try:
                pkg = read_json(Path(worktree, rel, "package.json"),
                                default=None)
            except (Exception, SystemExit):  # noqa: BLE001 - absent
                continue
            if not isinstance(pkg, dict):
                continue
            for key in _RN_DEP_SECTIONS:
                sect = pkg.get(key)
                if not isinstance(sect, dict):
                    continue
                for name, rng in sect.items():
                    if isinstance(rng, str) and rng.strip():
                        declared.setdefault(str(name),
                                            set()).add(rng.strip())
    except (Exception, SystemExit):  # noqa: BLE001 - unknown
        pass
    return declared


def _preprepare_expo_held(base_wt: str, lane: str, item,
                          queue) -> str | None:
    """M2.17 A1: expo major line changes need their RN group.

    An expo MAJOR bump (target major differs from the base
    tree's locked expo major) while a NEWER react-native minor
    is queued on the same lane but absent from the base is
    doomed: the batch stage would refuse the mixed RN tree
    (live run-23fdef89: expo 57.0.11 -> 58.0.19 on an RN 0.86
    base, refused for RN 그룹 혼재). Skip with the reason so
    no prepare+fastcheck is spent; the candidate stays queued
    for a human manual prepare. Same-lane normal bumps and
    trees that already carry the RN proceed. Never a block,
    never a cooldown count. Never raises."""

    try:
        target = str((item or {}).get("target") or "")
        want_major = _version_major(target)
        if want_major is None:
            return None
        declared = _base_declared_ranges(base_wt)
        expo_ranges = declared.get("expo") or set()
        rn_ranges = declared.get("react-native") or set()
        if not expo_ranges or not rn_ranges:
            return None
        base_majors = {_version_major(r) for r in expo_ranges}
        base_majors.discard(None)
        if not base_majors or want_major in base_majors:
            return None
        base_minors = set()
        for r in rn_ranges:
            minor = _version_minor(r)
            if minor is not None:
                base_minors.add((minor, r))
        if not base_minors:
            return None
        base_minor, base_rn = sorted(base_minors)[-1]
        newer = []
        for q in queue or []:
            if not isinstance(q, dict):
                continue
            if str(q.get("lane") or "") != lane:
                continue
            if str(q.get("dep") or "") != "react-native":
                continue
            q_target = str(q.get("target") or "")
            q_minor = _version_minor(q_target)
            if q_minor is not None and q_minor > base_minor:
                newer.append((q_minor, q_target))
        if not newer:
            return None
        _new_minor, new_target = sorted(newer)[-1]
        return (f"expo {target} 메이저는 RN {new_target}와 함께 "
                f"(기준 {base_rn}) — 단독 준비 건너뜀")
    except (Exception, SystemExit):  # noqa: BLE001 - fail open
        return None


def _preprepare_compiler_held(base_wt: str, dep: str,
                             target: str) -> str | None:
    """M2.17 A2: runtime-helper majors above the base compiler.

    The batch stage refuses a MAJOR runtime-helpers bump whose
    compiler in the same tree stays on the old major (M2.16);
    the check runs here against the BASE tree the candidate
    would prepare onto, with the candidate's own target as the
    runtime major. Same label shape, so the skip reason reads
    exactly like the batch refusal it pre-empts. Unreadable or
    compiler-less trees fail open — the stage decides. Never a
    block, never a cooldown count. Never raises."""

    try:
        comp = _compiler_for(dep)
        cand = _version_major(target)
        if not comp or cand is None:
            return None
        declared = _base_declared_ranges(base_wt)
        comp_ranges = declared.get(comp) or set()
        if not comp_ranges:
            return None
        from . import batches as _batches
        try:
            lock_text = Path(base_wt, "yarn.lock").read_text(
                encoding="utf-8", errors="ignore")
        except OSError:
            return None
        majors = set()
        for rng in comp_ranges:
            try:
                got = _batches._declared_resolutions(
                    lock_text, comp, {rng})
            except Exception:  # noqa: BLE001 - no resolution
                continue
            for ver in (got or {}).values():
                vs = ver if isinstance(ver, list) else [ver]
                for one in vs:
                    mm = _version_major(one)
                    if mm is not None:
                        majors.add(mm)
        if not majors or cand <= max(majors):
            return None
        return (f"컴파일러와 메이저 불일치 ({comp} {max(majors)} · "
                f"{dep} {cand})")
    except (Exception, SystemExit):  # noqa: BLE001 - fail open
        return None


# M2.20c: build tools that ship their own nested copy of a
# dep (locked hard range below) do not decide the tree — the
# root upgrade may still be possible, so the hold stays
# "생태계 대기" with a 참고 line instead of unblocking.
_ECO_NESTED_ALLOW = frozenset({"react-native-builder-bob"})


def _base_direct_deps(base_wt: str) -> set | None:
    """Direct dep names of the base tree (root + example manifests).

    dependencies + devDependencies + optionalDependencies keys
    of both package.json files. None when both manifests are
    unreadable (fail open: the caller keeps plain 생태계 대기).
    Never raises."""

    try:
        import json as _json
        out = set()
        seen = 0
        for rel in ("package.json", "example/package.json"):
            try:
                text = Path(base_wt, rel).read_text(
                    encoding="utf-8", errors="ignore")
            except (OSError, ValueError):
                continue
            try:
                manifest = _json.loads(text)
            except (ValueError, TypeError):
                continue
            if not isinstance(manifest, dict):
                continue
            seen += 1
            for sect in ("dependencies", "devDependencies",
                         "optionalDependencies"):
                scope = manifest.get(sect) or {}
                if isinstance(scope, dict):
                    out.update(str(k) for k in scope.keys())
        if seen == 0:
            return None
        return out
    except (Exception, SystemExit):  # noqa: BLE001 - fail open
        return None


def _ecosystem_hold(base_wt: str, dep: str, target: str) -> str | None:
    """M2.20b: the installed toolchain cannot take this MAJOR.

    For a MAJOR candidate X -> N, scans the BASE lockfile's hard
    `dependencies:` (yarn v1/berry inline; read-only, no
    install). Any resolved package P depending on X with a range
    excluding N blocks: "생태계 대기" — no prepare, no
    fastcheck, no batch, staying queued for a human manual
    prepare. Recomputed every scan from the lock, so it clears
    itself when the toolchain ships a range admitting N. Peer
    `*` alone never blocks (only hard `dependencies:` are
    read); minor/patch candidates are never checked; a major
    already resolved in the tree is not a fresh introduction;
    unreadable or unresolvable inputs fail open — the stage
    decides. Never a block, never a cooldown count. Never
    raises.

    M2.20c: when every excluder ships its own nested copy
    (allow-list above), the hold stays with a 참고 line
    instead of unblocking. When every excluder is a direct
    repo dep, the label names the family path instead: a
    newer P in the lock whose own range admits N gives
    "함께 올려야 함", otherwise "함께 올려야 할 수 있음".
    Mixed/transitive excluders keep plain "생태계 대기"."""

    try:
        from . import batches as _batches
        from . import versions as _versions
        dep = str(dep or "")
        target = str(target or "")
        cand = _version_major(target)
        if not dep or cand is None:
            return None
        try:
            lock_text = Path(base_wt, "yarn.lock").read_text(
                encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            return None
        cur = set()
        try:
            resolved = _batches._yarn_lock_versions(
                lock_text).get(dep, [])
        except Exception:  # noqa: BLE001 - unparsable lock
            return None
        for ver in resolved:
            mm = _version_major(ver)
            if mm is not None:
                cur.add(mm)
        if not cur or not all(m < cand for m in cur):
            return None
        blockers = []
        try:
            pairs = _batches._yarn_hard_dep_ranges(lock_text, dep)
        except Exception:  # noqa: BLE001 - unparsable lock
            return None
        for pkg, ver, rng in pairs:
            try:
                ok = _versions.satisfies(rng, target)
            except (Exception, SystemExit):  # noqa: BLE001 - odd
                continue
            if ok is False:
                blockers.append((pkg, ver or "?", rng))
        if not blockers:
            return None
        # M2.20d: show the blockers the owner recognises first:
        # direct repo deps, then packages many others depend on,
        # then alphabetical. Stable across ticks.
        pkgs = sorted({p for p, _, _ in blockers})
        direct = _base_direct_deps(base_wt)
        direct_set = direct if direct is not None else set()
        try:
            dep_counts = _batches._yarn_hard_dependent_counts(
                lock_text)
        except Exception:  # noqa: BLE001 - unparsable lock
            dep_counts = {}

        def _order(entry):
            pkg = entry[0]
            return (0 if pkg in direct_set else 1,
                    -dep_counts.get(pkg, 0), entry)

        blockers.sort(key=_order)
        shown = [f"{p}@{v} needs {dep} {r}"
                 for p, v, r in blockers[:3]]
        if len(blockers) > 3:
            shown.append(f"+{len(blockers) - 3} more")
        tail = (f"해제 조건: 위 패키지의 {dep} 허용 범위가 "
                f"{target} 포함")
        base_reason = (f"생태계 대기: {', '.join(shown)} — "
                       f"{tail}")
        if all(p in _ECO_NESTED_ALLOW for p in pkgs):
            ref = "; ".join(
                f"{p} keeps its own {dep}; peer ranges not "
                f"checked" for p in pkgs)
            return f"{base_reason} — 참고: {ref}"
        if direct is not None and all(p in direct for p in pkgs):
            by_pkg = {}
            for pkg, ver, rng in pairs:
                by_pkg.setdefault(pkg, []).append((ver, rng))
            together = []
            for pkg, ver, _ in blockers:
                if not ver or ver == "?":
                    continue
                for ver2, rng2 in by_pkg.get(pkg, []):
                    if ver2 == ver:
                        continue
                    try:
                        newer = _versions.compare(ver2, ver) == 1
                    except (Exception, SystemExit):  # noqa: odd
                        continue
                    if not newer:
                        continue
                    try:
                        admits = _versions.satisfies(rng2, target)
                    except (Exception, SystemExit):  # noqa: odd
                        continue
                    if admits is True:
                        together.append((pkg, ver2))
                        break
            if together:
                fam = [f"{p} {v} + {dep} {target}"
                       for p, v in sorted(set(together))[:3]]
                if len(set(together)) > 3:
                    fam.append(f"+{len(set(together)) - 3} more")
                return (f"함께 올려야 함: {', '.join(fam)} "
                        f"(주인 결정)")
            want = ([f"{p}" for p in pkgs[:3]]
                    + ([f"+{len(pkgs) - 3} more"]
                       if len(pkgs) > 3 else []))
            return (f"함께 올려야 할 수 있음: "
                    f"{', '.join(want)} 확인 필요 — {tail}")
        return base_reason
    except (Exception, SystemExit):  # noqa: BLE001 - fail open
        return None


def preprepare_refusal(lane: str, item, base_id: str,
                       queue) -> str | None:
    """M2.17 A + M2.20b: read-only pre-prepare refusal for doomed
    singletons.

    Runs the refusal rules against the BASE tree before any
    prepare+fastcheck is spent: (1) expo major line changes
    needing an RN minor the base lacks; (2) a runtime-helper
    MAJOR above the base tree's compiler major; (3) a MAJOR
    the installed toolchain cannot take (생태계 대기, computed
    from the lockfile, never hard-coded). Returns the skip
    reason, else None. Skips never block and never count as
    cooldown; the candidate stays queued for a human manual
    prepare. Unreadable inputs fail open — the stage decides.
    Never raises."""

    try:
        dep = str((item or {}).get("dep") or "")
        target = str((item or {}).get("target") or "")
        if not dep or not target:
            return None
        base_wt = _base_lane_worktree(base_id, lane)
        if not base_wt:
            return None
        if dep == "expo":
            return _preprepare_expo_held(base_wt, lane, item, queue)
        if _compiler_for(dep):
            return _preprepare_compiler_held(base_wt, dep, target)
        eco = _ecosystem_hold(base_wt, dep, target)
        if eco:
            return eco
        return None
    except (Exception, SystemExit):  # noqa: BLE001 - fail open
        return None


def _joint_recs(repo: str, lane: str,
                statuses=("done",)) -> list:
    """M2.17 B: kind=joint op records for repo+lane and statuses.

    Read-only scan of the integration ops dir. Unreadable homes
    or files give []. Never raises."""

    try:
        from . import batches as _batches
        from .util import read_json
        try:
            files = sorted(
                _batches.integration_ops_dir().glob("prep-*.json"))
        except (OSError, ValueError):
            return []
        out = []
        for path in files:
            try:
                rec = read_json(path, default=None)
            except (Exception, SystemExit):  # noqa: BLE001 - corrupt
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("kind") != "joint" \
                    or str(rec.get("status") or "") not in statuses:
                continue
            if str(rec.get("repo") or "") != repo \
                    or str(rec.get("lane") or "") != lane:
                continue
            out.append(rec)
        return out
    except (Exception, SystemExit):  # noqa: BLE001 - unknown
        return []


def _done_joint_recs(repo: str, lane: str) -> list:
    """M2.17 B: done kind=joint op records for repo+lane."""
    return _joint_recs(repo, lane, ("done",))


def _joint_refresh_match(repo: str, lane: str, dep: str,
                         target: str, y_run: str,
                         base_id: str) -> dict | None:
    """M2.17 B core: the refresh plan for a newer member target.

    Shared by the fresh-item decision (M2.17) and the refused
    derived-op decision (M2.17b): when a done joint on the same
    repo+lane+base batch covers the partner (same dep pair, this
    member's target semver-greater), returns the plan. None when
    no done joint matches, when a done or blocked joint already
    covers the pair (never again), or when the partner's solo op
    is unreadable. Read-only; never raises."""

    try:
        from . import versions as _versions
        if not dep or not target or not base_id or not y_run:
            return None
        joints = _done_joint_recs(repo, lane)
        if not joints:
            return None
        for rec in joints:
            if str(rec.get("batch_id") or "") != str(base_id or ""):
                continue
            cands = [c for c in (rec.get("joint_candidates") or [])
                     if isinstance(c, dict)]
            if len(cands) != 2:
                continue
            mine = [c for c in cands
                    if str(c.get("dep") or "") == dep]
            others = [c for c in cands
                      if str(c.get("dep") or "") != dep]
            if len(mine) != 1 or len(others) != 1:
                continue
            try:
                if _versions.compare(
                        target, str(mine[0].get("target") or "")) != 1:
                    continue
            except (Exception, SystemExit):  # noqa: BLE001 - no cmp
                continue
            x_run = str(others[0].get("run") or "")
            if not x_run or x_run == y_run:
                continue
            # B2: a joint already covering this pair means an
            # earlier tick derived the refresh — never again. A
            # BLOCKED refresh covers too: the next tick must not
            # re-derive the same refusal, the item falls through
            # to the pre-prepare skip below instead.
            covered = False
            for rec2 in _joint_recs(repo, lane,
                                    ("done", "blocked")):
                if str(rec2.get("batch_id") or "") != \
                        str(base_id or ""):
                    continue
                runs = set(rec2.get("runs") or [])
                if x_run in runs and y_run in runs:
                    covered = True
                    break
            if covered:
                return None
            # The partner's solo op must be readable with a
            # candidate commit, or the derive cannot run.
            from . import batches as _batches
            from .util import read_json
            x_op_id = str(rec.get("derived_from") or "")
            if not x_op_id:
                continue
            try:
                xop = read_json(_batches.op_file(x_op_id),
                                default=None)
            except (Exception, SystemExit):  # noqa: BLE001 - unread
                continue
            if not isinstance(xop, dict):
                continue
            if str(xop.get("run_id") or "") != x_run:
                continue
            if not str((xop.get("candidate") or {}).get("head")
                       or ""):
                continue
            return {"x_run": x_run, "y_run": y_run,
                    "x_dep": str(others[0].get("dep") or ""),
                    "x_target": str(others[0].get("target") or ""),
                    "y_dep": dep, "y_target": target,
                    "x_op_id": x_op_id,
                    "old_joint_op": str(rec.get("op_id") or ""),
                    "base_id": str(base_id or "")}
        return None
    except (Exception, SystemExit):  # noqa: BLE001 - fail open
        return None


def joint_refresh_for(repo: str, lane: str, item, base_id: str,
                      queue=None) -> dict | None:
    """M2.17 B decision: refresh an existing done joint instead of
    preparing a doomed newer-member singleton.

    When a fresh item is a NEWER target of a done joint's member
    (same repo+lane+base batch, same dep, semver-greater, same
    partner run), returns the refresh plan
    {x_run, y_run, x_dep, x_target, y_dep, y_target, x_op_id,
    old_joint_op, base_id}; the driver derives ONE refreshed
    joint through the M2.14 joint path instead of preparing the
    standalone. Returns None when no done joint matches, when a
    done joint already covers the pair (second tick), or when
    the inputs are unreadable. Read-only; never raises, never
    blocks, never a cooldown count."""

    try:
        dep = str((item or {}).get("dep") or "")
        target = str((item or {}).get("target") or "")
        y_run = str((item or {}).get("run_id") or "")
        return _joint_refresh_match(repo, lane, dep, target,
                                    y_run, base_id)
    except (Exception, SystemExit):  # noqa: BLE001 - fail open
        return None


def joint_refresh_for_refused(repo: str, lane: str, op_rec,
                              base_id: str) -> dict | None:
    """M2.17b A decision: refresh a joint for an already-derived,
    batch-refused member.

    When a DONE single-member derived op (op reresolve, any kind
    but joint — live: the expo 58.0.19 rederive) trips the batch
    stage's own rn_group_mixed refusal on its result tree, AND a
    done joint on the same repo+lane+base batch covers the
    partner (same rule as joint_refresh_for: same deps, this
    member's target semver-greater), returns the same refresh
    plan so the driver derives ONE refreshed joint instead of
    resuming into another doomed batch. The same covered guard
    applies (done or blocked covering both runs means never
    again). Read-only recomputation with the stage's own rule;
    never raises, never blocks, never a cooldown count."""

    try:
        rec = op_rec or {}
        if not isinstance(rec, dict):
            return None
        if rec.get("op") != "reresolve" \
                or str(rec.get("kind") or "") == "joint":
            return None
        if str(rec.get("status") or "") != "done":
            return None
        if str(rec.get("repo") or "") != str(repo or "") \
                or str(rec.get("lane") or "") != str(lane or ""):
            return None
        y_run = str(rec.get("run_id") or "")
        y_dep = str(rec.get("dep") or "")
        y_target = str(rec.get("target") or "")
        if not y_run or not y_dep or not y_target \
                or not base_id:
            return None
        from . import batches as _batches
        try:
            base_rec = _batches.load_batch(base_id)
        except (Exception, SystemExit):  # noqa: BLE001 - no base
            return None
        result_path = str((rec.get("result") or {}).get("path")
                          or "")
        if not result_path:
            return None
        dupes = _lane_lock_dupes(base_rec, lane, result_path)
        if rn_group_mixed(dupes) is None:
            return None
        return _joint_refresh_match(repo, lane, y_dep, y_target,
                                    y_run, base_id)
    except (Exception, SystemExit):  # noqa: BLE001 - fail open
        return None


def rn_group_mixed(lock_dupes: dict) -> str | None:
    """M2.8b3: refuse an auto-batch whose lock diff mixes two
    react-native minors (read-only, display data in).

    The M2.5c duplicate-tree report lists added second trees as
    "name v1 + v2". When the name is react-native itself or an
    RN_LOCKSTEP_FAMILY member at two different minors, the lane
    result would ship two RN trees (live: expo 57->58 held for
    exactly this). Returns the lockstep reason naming the newest
    minor group, else None. Same-minor dupes and unrelated dupes
    pass. Never raises."""
    try:
        items = (lock_dupes or {}).get("items") or []
    except Exception:  # noqa: BLE001 - no report means no refusal
        return None
    for item in items:
        try:
            name, _, rest = str(item).partition(" ")
            if name != "react-native" and name not in RN_LOCKSTEP_FAMILY:
                continue
            vers = [v for v in rest.split(" + ") if v]
            minors = set()
            for v in vers:
                mm = _version_minor(v)
                if mm is not None:
                    minors.add(mm)
            if len(minors) > 1:
                newest = max(minors)
                return (f"RN 그룹 혼재: {name} {' + '.join(vers)} — "
                        f"RN {newest[0]}.{newest[1]} 그룹과 함께")
        except Exception:  # noqa: BLE001 - one bad line never acts
            continue
    return None


_COMPILER_RUNTIME_PAIRS = (
    ("@babel/runtime", "@babel/core"),
    ("@babel/runtime-corejs3", "@babel/core"),
    ("@swc/helpers", "@swc/core"),
    ("tslib", "typescript"),
)


def _version_major(ver: str):
    """Major only, or None when unreadable."""
    import re as _re
    m = _re.search(r"(\d+)", str(ver or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _compiler_for(dep: str) -> str:
    """The compiler paired with a runtime-helpers package, else ""."""
    for runtime, compiler in _COMPILER_RUNTIME_PAIRS:
        if runtime == str(dep or ""):
            return compiler
    return ""


def compiler_major_mismatch(worktree: str, dep: str) -> str | None:
    """M2.16b: a MAJOR bump of a runtime-helpers package whose
    compiler in the same worktree stays on the old major must
    never auto-batch (live run-160ec8d7: @babel/runtime 8 with
    @babel/core 7 side by side in the lock).

    Read-only over the derived tree's manifests (root + example)
    and yarn.lock: each declared range of the runtime and its
    compiler resolves through the lock, and a runtime major
    above the compiler major refuses. Same-major trees, unknown
    packages (not in the table) and unreadable inputs pass —
    missing or unparsable data never refuses. Returns the Korean
    label naming both majors, else None. Refusal-only, like
    rn_group_mixed: never a block, never a cooldown count;
    manual UI import stays possible. Never raises.
    """
    dep = str(dep or "")
    comp = _compiler_for(dep)
    if not comp or not str(worktree or ""):
        return None
    try:
        from . import batches as _batches
        from .util import read_json
        declared: dict = {}
        for rel in ("", "example"):
            try:
                pkg = read_json(Path(worktree, rel, "package.json"),
                                default=None)
            except (Exception, SystemExit):  # noqa: BLE001 -
                # unreadable manifest reads as absent
                continue
            if not isinstance(pkg, dict):
                continue
            for key in _RN_DEP_SECTIONS:
                sect = pkg.get(key)
                if not isinstance(sect, dict):
                    continue
                for name in (dep, comp):
                    if name in sect:
                        declared.setdefault(name, set()).add(
                            str(sect[name]))
        if dep not in declared or comp not in declared:
            return None
        try:
            lock_text = Path(worktree, "yarn.lock").read_text(
                encoding="utf-8", errors="ignore")
        except OSError:
            return None
        majors: dict = {}
        for name in (dep, comp):
            found = set()
            for rng in declared.get(name) or ():
                try:
                    got = _batches._declared_resolutions(
                        lock_text, name, {rng})
                except Exception:  # noqa: BLE001 - no resolution
                    continue
                for ver in (got or {}).values():
                    vers = ver if isinstance(ver, list) else [ver]
                    for one in vers:
                        mm = _version_major(one)
                        if mm is not None:
                            found.add(mm)
            if found:
                majors[name] = found
        if dep not in majors or comp not in majors:
            return None
        rmax, cmax = max(majors[dep]), max(majors[comp])
        if rmax > cmax:
            return (f"컴파일러와 메이저 불일치 ({comp} {cmax} · "
                    f"{dep} {rmax})")
        return None
    except Exception:  # noqa: BLE001 - unreadable never refuses
        return None


def compiler_major_mismatch_same_manifest(worktree: str,
                                          dep: str) -> str | None:
    """M2.16d: the same check, but strict — runtime and compiler
    must sit in the SAME manifest (root+example are each read
    alone, never unioned).

    Existing batches predate the M2.16 union rule, so their card
    gets a WARNING (never a refusal) only when the mismatch is
    co-located, with the label naming both majors plus
    " — 같은 매니페스트". Anything else passes: same-major
    trees, unknown packages, manifests that declare only one
    side, and unreadable inputs (missing or unparsable data
    never warns, never raises).
    """
    dep = str(dep or "")
    comp = _compiler_for(dep)
    if not comp or not str(worktree or ""):
        return None
    try:
        from . import batches as _batches
        from .util import read_json
        try:
            lock_text = Path(worktree, "yarn.lock").read_text(
                encoding="utf-8", errors="ignore")
        except OSError:
            return None
        for rel in ("", "example"):
            try:
                pkg = read_json(Path(worktree, rel, "package.json"),
                                default=None)
            except (Exception, SystemExit):  # noqa: BLE001 -
                # unreadable manifest reads as absent
                continue
            if not isinstance(pkg, dict):
                continue
            declared = {}
            for key in _RN_DEP_SECTIONS:
                sect = pkg.get(key)
                if not isinstance(sect, dict):
                    continue
                for name in (dep, comp):
                    if name in sect:
                        declared.setdefault(name, set()).add(
                            str(sect[name]))
            if dep not in declared or comp not in declared:
                continue
            majors = {}
            for name in (dep, comp):
                found = set()
                for rng in declared.get(name) or ():
                    try:
                        got = _batches._declared_resolutions(
                            lock_text, name, {rng})
                    except Exception:  # noqa: BLE001 - no resolution
                        continue
                    for ver in (got or {}).values():
                        vers = ver if isinstance(ver, list) else [ver]
                        for one in vers:
                            mm = _version_major(one)
                            if mm is not None:
                                found.add(mm)
                if found:
                    majors[name] = found
            if dep not in majors or comp not in majors:
                continue
            rmax, cmax = max(majors[dep]), max(majors[comp])
            if rmax > cmax:
                return (f"컴파일러와 메이저 불일치 ({comp} {cmax} · "
                        f"{dep} {rmax}) — 같은 매니페스트")
        return None
    except Exception:  # noqa: BLE001 - unreadable never warns
        return None


def _lane_lock_dupes(base: dict, lane: str, worktree: str) -> dict:
    """M2.5c duplicate-tree census over the two root locks (base vs
    result worktree). Shared by the manifest builder (display) and
    the batch stage (M2.8b3 RN-group refusal). Read-only; missing
    locks mean no report. Never raises."""
    try:
        from . import batches as _batches2
        base_integs = [i for i in ((base or {}).get("integrations")
                                   or [])
                       if isinstance(i, dict)]
        if len(base_integs) == 1:
            base_wt = str(base_integs[0].get("worktree") or "")
        else:
            matches = [i for i in base_integs
                       if str(i.get("lane") or "") == lane]
            base_wt = str(matches[0].get("worktree") or "") \
                if len(matches) == 1 else ""
        if base_wt and worktree:
            return _batches2.lock_duplicate_versions(
                base_wt, str(worktree))
    except (Exception, SystemExit):  # noqa: BLE001 - no report
        pass
    return {"items": [], "total": 0}


def _lock_minor_keys(lock_text):
    """{pkg: {minor: set(descriptors)}} for RN-family pkgs in one lock.

    M2.14a helper: which RN-family minors a lock tree pins, keyed
    by exact descriptor strings (berry "npm:" forms kept as-is).
    Unparseable locks read as empty — never raises."""
    out = {}
    try:
        from . import batches as _batches
        stanzas = _batches._split_yarn_stanzas(lock_text or "")
    except Exception:  # noqa: BLE001 - unparseable means no pairs
        return out
    for descs, body in stanzas or []:
        try:
            ver = _batches._body_version(body)
            mm = _version_minor(ver)
            if mm is None:
                continue
            for d in descs or []:
                name = _batches._yarn_pkg_name(d)
                if name in RN_LOCKSTEP_FAMILY:
                    out.setdefault(name, {}).setdefault(mm, set()).add(d)
        except Exception:  # noqa: BLE001 - one bad stanza never acts
            continue
    return out


def _mixed_pairs(minor_keys: dict) -> dict:
    """{name: (old_minor, new_minor)} for names pinned at 2+ minors."""
    out = {}
    for name in sorted(minor_keys or {}):
        minors = sorted((minor_keys.get(name) or {}))
        if len(minors) > 1:
            out[name] = (minors[0], minors[-1])
    return out


def _pair_text(pair) -> str:
    return f"{pair[0][0]}.{pair[0][1]}+{pair[1][0]}.{pair[1][1]}"


def joint_pair_match(x_dep: str, x_target: str, x_keys: dict,
                     y_dep: str, y_target: str, y_keys: dict,
                     base_keys: dict):
    """M2.14a core: do X and Y only ship together? Pure (no IO).

    x_keys/y_keys/base_keys are {pkg: {minor: set(descriptors)}}
    maps (see _lock_minor_keys). Returns the shared (old, new)
    minor pair or None. Rules:
    - both trees mix the SAME pair on >= 1 RN-family name;
    - the pair's new minor is targeted by X or by Y (the mix is
      about a bump one side actually makes — never incidental);
    - not both deps in the lockstep family (two RN members move
      the same direction through the RN group itself — same
      direction, never a joint pair);
    - X retains old-minor descriptors the base holds, and Y adds
      new-minor descriptors the base lacks (X leaves the old
      minor, Y adds the new one — opposite directions of the
      same pair).
    Deterministic: the pair comes from X's first sorted mixed
    name. Never raises.
    """
    try:
        return _joint_pair_match_inner(
            str(x_dep or ""), str(x_target or ""), x_keys or {},
            str(y_dep or ""), str(y_target or ""), y_keys or {},
            base_keys or {})
    except (Exception, SystemExit):  # noqa: BLE001 - no match, no hint
        return None


def _joint_pair_match_inner(x_dep, x_target, x_keys,
                            y_dep, y_target, y_keys, base_keys):
    if not x_dep or not y_dep or x_dep == y_dep:
        return None
    x_pairs = _mixed_pairs(x_keys)
    y_pairs = _mixed_pairs(y_keys)
    if not x_pairs or not y_pairs:
        return None
    pair = x_pairs[sorted(x_pairs)[0]]
    if not any(y_pairs.get(n) == pair for n in y_pairs):
        return None
    x_minor = _version_minor(x_target)
    y_minor = _version_minor(y_target)
    if pair[1] != x_minor and pair[1] != y_minor:
        return None
    x_in = x_dep in RN_LOCKSTEP_FAMILY
    y_in = y_dep in RN_LOCKSTEP_FAMILY
    if x_in and y_in:
        return None
    old, new = pair
    x_retains = any(
        bool(((x_keys.get(name) or {}).get(old) or set())
             & ((base_keys.get(name) or {}).get(old) or set()))
        for name in x_pairs if x_pairs[name] == pair)
    if not x_retains:
        return None
    y_adds = any(
        bool(((y_keys.get(name) or {}).get(new) or set())
             - ((base_keys.get(name) or {}).get(new) or set()))
        for name in y_pairs if y_pairs.get(name) == pair)
    if not y_adds:
        return None
    return pair


def detect_joint_partner(repo: str, lane: str, x_rec: dict):
    """M2.14a: find the partner candidate X must ship with (read-only
    except one bounded joint_hint cache key on X's op record).

    X was just refused by rn_group_mixed: X's tree mixes an RN
    minor pair {old, new} where new is X's own target minor. A
    partner Y in the same lane qualifies when Y's own tree mixes
    the SAME pair, Y's dep sits outside the RN lockstep family,
    Y's lock adds new-minor RN descriptors the base lacks (Y adds
    X's new minor), and X's lock retains old-minor descriptors
    the base already holds (X leaves the old minor). First match
    by run_id wins; anything unreadable simply never matches.

    Returns the hint dict (also cached on X's op record) or None.
    Recomputed only when the lane base head, X's op, or the lane
    candidate set changes; otherwise the cached hint is reused.
    Never raises.
    """
    try:
        return _detect_joint_partner_inner(repo, lane, x_rec or {})
    except (Exception, SystemExit):  # noqa: BLE001 - hint is advisory
        return None


def _detect_joint_partner_inner(repo: str, lane: str, x_rec: dict):
    from . import batches as _batches
    from .util import read_json
    lane = str(lane or "")
    x_dep = str((x_rec or {}).get("dep") or "")
    x_target = str((x_rec or {}).get("target") or "")
    x_run = str((x_rec or {}).get("run_id") or "")
    x_op = str((x_rec or {}).get("op_id") or "")
    if not (lane and x_dep and x_target and x_run and x_op):
        return None
    want_new = _version_minor(x_target)
    if want_new is None:
        return None
    try:
        base = _batches.default_base(repo, lane, ambiguous="empty")
    except (Exception, SystemExit):
        return None
    base_id = str((base or {}).get("id") or "")
    if not base_id:
        return None
    try:
        base_batch = _batches.load_batch(base_id)
    except (Exception, SystemExit):
        return None
    base_head, base_wt = "", ""
    for integ in (base_batch.get("integrations") or []):
        if not isinstance(integ, dict):
            continue
        if str(integ.get("lane") or "") != lane:
            continue
        base_head = str(integ.get("head") or "")
        cand = str(integ.get("worktree") or integ.get("path") or "")
        if cand and Path(cand).is_dir():
            base_wt = cand
        break
    if not base_head or not base_wt:
        return None
    try:
        base_lock = Path(base_wt, "yarn.lock").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    x_lock = ""
    x_wt = str(((x_rec or {}).get("result") or {}).get("path") or "")
    if x_wt:
        try:
            x_lock = Path(x_wt, "yarn.lock").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            x_lock = ""
    if not x_lock:
        # A blocked prepare has no result tree — read the
        # candidate lock blob from the base checkout (same repo).
        x_commit = str((x_rec.get("candidate") or {}).get("head")
                       or "")
        if x_commit and base_wt:
            try:
                shown = _batches._run_git(
                    ["show", f"{x_commit}:yarn.lock"], base_wt)
                if shown.returncode == 0:
                    x_lock = shown.stdout or ""
            except (Exception, SystemExit):
                x_lock = ""
    if not x_lock:
        return None
    x_keys = _lock_minor_keys(x_lock)
    base_keys = _lock_minor_keys(base_lock)
    if not _mixed_pairs(x_keys):
        return None
    try:
        queue = sched.load(repo).get("queue", [])
    except (Exception, SystemExit):
        return None
    cands = []
    for item in queue or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("lane") or "") != lane:
            continue
        if _queue_kind(item) != "dep-update":
            continue
        rid = str(item.get("run_id") or "")
        dep = str(item.get("dep") or "")
        if not rid or not dep or rid == x_run or dep == x_dep:
            continue
        cands.append((rid, dep))
    cands.sort()
    # Cache fingerprint: recompute when the base, X's op, or the
    # lane candidate set (run/op/updated) changes.
    fp = []
    y_ops = {}
    for rid, _dep in cands:
        try:
            y_rec = _batches.latest_integration_op(rid)
        except (Exception, SystemExit):
            y_rec = None
        if not isinstance(y_rec, dict):
            fp.append([rid, "", ""])
            continue
        fp.append([rid, str(y_rec.get("op_id") or ""),
                   str(y_rec.get("updated_at") or "")])
        y_ops[rid] = y_rec
    try:
        x_file = _batches.op_file(x_op)
        x_stored = read_json(x_file, default=None)
    except (Exception, SystemExit):
        x_stored = None
    cached = (x_stored or {}).get("joint_hint") \
        if isinstance(x_stored, dict) else None
    if isinstance(cached, dict) and cached.get("base") == base_head \
            and cached.get("x_at") == str((x_stored or {}).get(
                "updated_at") or "") \
            and cached.get("cand_fp") == fp:
        if cached.get("partner_run"):
            return cached
        return None
    found = None
    for rid, y_dep in cands:
        y_rec = y_ops.get(rid)
        if not isinstance(y_rec, dict):
            continue
        y_kind = str(y_rec.get("kind") or "")
        if y_rec.get("op") == "prepare":
            if y_rec.get("status") != "blocked":
                continue
        elif y_rec.get("op") == "reresolve":
            if y_rec.get("status") != "done":
                continue
            if y_kind not in (None, "", "lockfix", "rederive"):
                continue
        else:
            continue
        y_op = str(y_rec.get("op_id") or "")
        if not y_op:
            continue
        y_wt = str((y_rec.get("result") or {}).get("path") or "")
        y_lock = ""
        if y_wt:
            try:
                y_lock = Path(y_wt, "yarn.lock").read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                y_lock = ""
        if not y_lock:
            y_commit = str((y_rec.get("candidate") or {}).get("head")
                           or "")
            if y_commit and base_wt:
                try:
                    shown = _batches._run_git(
                        ["show", f"{y_commit}:yarn.lock"], base_wt)
                    if shown.returncode == 0:
                        y_lock = shown.stdout or ""
                except (Exception, SystemExit):
                    y_lock = ""
        if not y_lock:
            continue
        y_keys = _lock_minor_keys(y_lock)
        y_target = str(y_rec.get("target") or "")
        pair = joint_pair_match(x_dep, x_target, x_keys, y_dep,
                                y_target, y_keys, base_keys)
        if pair is None:
            continue
        pair_text = _pair_text(pair)
        found = {"partner_run": rid, "partner_op": y_op,
                 "partner_dep": y_dep, "partner_target": y_target,
                 "pair": pair_text, "base": base_head,
                 "x_at": str((x_stored or {}).get("updated_at") or ""),
                 "y_at": str(y_rec.get("updated_at") or ""),
                 "cand_fp": fp,
                 "at": datetime.now(timezone.utc).isoformat(
                     timespec="seconds")}
        break
    hint = dict(found) if found else {
        "partner_run": None, "partner_op": None, "pair": "",
        "base": base_head,
        "x_at": str((x_stored or {}).get("updated_at") or ""),
        "y_at": "", "cand_fp": fp,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        if isinstance(x_stored, dict):
            x_stored["joint_hint"] = hint
            from .util import write_json
            write_json(_batches.op_file(x_op), x_stored)
    except (Exception, SystemExit):  # noqa: BLE001 - cache is bonus
        pass
    return found


def _queue_kind(item) -> str:
    """M2.5b wording: dep-update vs issue-triage for backlog counts.

    Old queue rows predate `kind`; a row with a dep is a dep-update,
    otherwise an issue run."""
    kind = str((item or {}).get("kind") or "")
    if kind:
        return kind
    return "dep-update" if (item or {}).get("dep") else "issue-triage"


def lockstep_companions(queue: list, lane: str, target: str) -> list:
    """Family candidates riding the same react-native bump.

    Dep-update queue items in `lane` whose dep is in the family and
    whose target minor equals the RN `target` minor — shown next to
    a prepared react-native bump as "같이 옮길 후보". Read-only."""
    want = _version_minor(target)
    if want is None:
        return []
    out = []
    for item in queue or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind != "dep-update":
            continue
        if str(item.get("lane") or "") != lane:
            continue
        dep = str(item.get("dep") or "")
        tgt = str(item.get("target") or "")
        if dep in RN_LOCKSTEP_FAMILY and _version_minor(tgt) == want:
            out.append(f"{dep} {tgt}")
    return sorted(set(out))


def _family_key(dep: str) -> str:
    """M2.6: release-train family key — the npm scope, with an
    unscoped name joining its own scope (commitlint rides with
    @commitlint/*). Empty dep means no family."""
    dep = str(dep or "")
    if not dep:
        return ""
    if "/" in dep:
        return dep.split("/", 1)[0]
    return "@" + dep


def _supersede_map(items: list) -> dict:
    """M2.6: older run -> newer target for same (lane, dep) groups.

    When the queue holds the same dep on the same lane at two
    reviewed (verified) semver targets, the older run is superseded
    — it is skipped, never prepared, never a block. String-only
    targets (branches, SHAs) never supersede: both sides must
    parse as semver. Read-only."""
    from functools import cmp_to_key
    from . import versions as _versions
    groups: dict = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "waiting-review":
            continue
        if item.get("verified") is not True:
            continue
        dep = str(item.get("dep") or "")
        target = str(item.get("target") or "")
        lane = str(item.get("lane") or "")
        run_id = str(item.get("run_id") or "")
        if not (dep and target and lane and run_id):
            continue
        if _versions.parse(target) is None:
            continue
        groups.setdefault((lane, dep), []).append(item)
    out = {}
    for (_lane, _dep), members in groups.items():
        if len({m.get("target") for m in members}) < 2:
            continue
        best = max(members, key=cmp_to_key(
            lambda a, b: _versions.compare(
                str(a.get("target") or ""),
                str(b.get("target") or ""))))
        for m in members:
            rid = str(m.get("run_id") or "")
            if rid != str(best.get("run_id") or "") and \
                    _versions.compare(str(m.get("target") or ""),
                                      str(best.get("target") or "")) < 0:
                out[rid] = str(best.get("target") or "")
    return out


def _family_siblings(queue: list, lane: str, run_id: str,
                     dep: str) -> list:
    """M2.6: waiting-review siblings of one lone member.

    Same lane + same train scope, a different run, not superseded,
    with no done op on record — the family the lone member should
    have ridden with. Returns "dep target" labels. Read-only (one
    op-record read per sibling at most)."""
    from . import batches as _batches
    fam = _family_key(dep)
    if not (lane and fam):
        return []
    try:
        superseded = _supersede_map(queue or [])
    except Exception:  # noqa: BLE001 - no map means no siblings
        superseded = {}
    out = []
    for item in queue or []:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "waiting-review":
            continue
        sib = str(item.get("run_id") or "")
        if not sib or sib == run_id:
            continue
        if str(item.get("lane") or "") != lane:
            continue
        if _family_key(str(item.get("dep") or "")) != fam:
            continue
        if sib in superseded:
            continue
        try:
            existing = _batches.latest_integration_op(sib)
        except Exception:  # noqa: BLE001 - unreadable keeps it listed
            existing = {"status": "opaque"}
        if isinstance(existing, dict) and \
                str(existing.get("status") or "") == "done":
            continue
        out.append(f"{item.get('dep') or ''} "
                   f"{item.get('target') or ''}".rstrip())
    return sorted(set(out))


def _family_groups(items: list) -> dict:
    """M2.6: (lane, target, created_at, family) -> members.

    Covers every waiting-review dep-update item, verified or not,
    so the driver can tell "all verified, prepare together" from
    "review still missing, wait". Items without dep/target/lane/
    created_at never group. Read-only."""
    groups: dict = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "waiting-review":
            continue
        if _queue_kind(item) != "dep-update":
            continue
        dep = str(item.get("dep") or "")
        target = str(item.get("target") or "")
        lane = str(item.get("lane") or "")
        created = str(item.get("created_at") or "")
        fam = _family_key(dep)
        if not (dep and target and lane and created and fam):
            continue
        slot = groups.setdefault((lane, target, created, fam),
                                 {"verified": [], "unverified": []})
        if item.get("verified") is True:
            slot["verified"].append(item)
        else:
            slot["unverified"].append(item)
    return groups
# Lease busy is not failure: only 45 minutes of continuous busy counts
# as blocked. After 3 consecutive blocks a lane cools down for 6 hours.
_BUSY_LIMIT_S = 45 * 60
_COOLDOWN_BLOCKS = 3
_COOLDOWN_S = 6 * 3600
# E5: the cooldown-until map key lives in sched (state owner);
# writer, reader and clearer share it — no string duplicates (a
# wrong key here once made the flip claim a release it never
# performed).
_COOLDOWN_KEY = sched._COOLDOWN_KEY


def _kst_short(at: str) -> str:
    """One timestamp as 'MM/DD HH:MM' in KST (fixed UTC+9, no DST),
    the same convention the glance uses. Unparseable input passes
    through raw — never dropped."""
    try:
        dt = datetime.fromisoformat(str(at or ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        kst = timezone(timedelta(hours=9))
        return dt.astimezone(kst).strftime("%m/%d %H:%M")
    except (ValueError, TypeError, OverflowError):
        return str(at or "")


def policy_keys() -> list:
    return ["m2_" + stage for stage in STAGES]


def get_policy(repo: str) -> dict:
    """Stage toggles for a repo; unknown repos raise via repos.load."""
    repos.load(repo)
    st = sched.load(repo)
    return {stage: bool(st.get("m2_" + stage, False)) for stage in STAGES}


def set_policy(repo: str, stage: str, on: bool,
               by: str = "owner") -> dict:
    """Flip one toggle under the repo lock; records a timeline event.

    F1: flipping ON a stage with no runner is refused (the API
    must not accept what cannot act). All five stages are open
    since S15; only unknown stage names refuse.
    M2.5d: `by` ("owner"|"root") attributes the flip on the
    timeline; "worker" is refused — only a person flips."""
    if stage not in STAGES:
        raise ValueError(f"unknown M2 stage '{stage}'")
    by = str(by or "owner")
    if by not in ("owner", "root"):
        raise ValueError("policy flip은 owner 또는 root만 — "
                         f"'{by}' 거부")
    if on and not STAGE_FLIP.get(stage, False):
        raise ValueError(f"자율 {_STAGE_KO[stage]} 단계는 "
                         f"{NOT_OPEN_NOTE}")
    repos.load(repo)
    with sched.locked(repo):
        st = sched.load(repo)
        prev = bool(st.get("m2_" + stage, False))
        st["m2_" + stage] = bool(on)
        # E3+E5: an owner off->on flip releases the lane cooldowns
        # (the key the tick actually reads) and the
        # consecutive-block counts for a fresh start.
        cleared_detail = ""
        if on and not prev:
            cool = st.get(_COOLDOWN_KEY) or {}
            blocks = st.get("m2_consec_blocks") or {}
            if cool:
                lanes = ", ".join(
                    f"{lane} 레인 대기 해제 (원래 {_kst_short(until)}까지)"
                    for lane, until in sorted(cool.items()))
                cleared_detail = lanes
                st[_COOLDOWN_KEY] = {}
            if blocks:
                names = ", ".join(sorted(blocks))
                bit = f"연속 막힘 기록 지움({names})"
                cleared_detail = (f"{cleared_detail} · {bit}"
                                  if cleared_detail else bit)
                st["m2_consec_blocks"] = {}
        sched.save(st)
    # M2.5d: the flip is attributed to who flipped it (the
    # existing UI button sends nothing, so it stays "owner").
    events.append(repo, "m2-policy", stage,
                  f"자율 {_STAGE_KO[stage]} "
                  f"{'켜짐' if on else '꺼짐'}",
                  detail="다음 단계부터 적용됩니다",
                  actor=by)
    if cleared_detail:
        # E5: say only what was actually cleared — no claim when
        # nothing was waiting.
        who_txt = "Root가 다시 켬" if by == "root" \
            else "소유자가 다시 켬"
        events.append(repo, "m2-policy", stage,
                      f"대기 해제: {who_txt}",
                      detail=cleared_detail,
                      actor=by)
    return get_policy(repo)


def effective_chain(policy: dict) -> list:
    """Longest switch-on prefix of STAGES: a stage runs only when its
    own toggle and every earlier toggle are on (re-read at stage
    start). Later toggles past the first OFF stage never act."""
    chain = []
    for stage in STAGES:
        if (policy or {}).get(stage):
            chain.append(stage)
        else:
            break
    return chain


def stage_prefix_ok(policy: dict, stage: str) -> tuple:
    """FIX-a1 (Codex F8): the enabled-stage prefix, recomputed.

    A stage acts only when its own toggle and every earlier
    toggle are on — the same rule policy_line shows on the
    glance. Returns (True, "") or (False, reason) where reason is
    the glance's own plain wording, so execution can never
    outrun what the UI promises."""
    chain = effective_chain(policy or {})
    if stage in chain:
        return True, ""
    return False, policy_line(policy or {})


def policy_line(policy: dict) -> str:
    """One-line glance summary, over the EFFECTIVE chain (F2).

    꺼짐 / 준비만 / 준비·검증만 / ... / 전체, plus a parenthetical
    naming any toggle that is on but cannot act because an earlier
    stage is off (e.g. 검증 on + 준비 off)."""
    on = [stage for stage in STAGES if (policy or {}).get(stage)]
    if not on:
        return "꺼짐"
    chain = effective_chain(policy)
    # S15: refresh re-feeds the review pipeline off prepare alone
    # (a stale re-run, not a pipeline tail) — it acts whenever
    # prepare does, so the line shows it there. Anything else keeps
    # the generic prefix rule.
    display = list(chain)
    if (policy or {}).get("refresh") and display[:1] == ["prepare"] \
            and "refresh" not in display:
        display.append("refresh")
    extra = [stage for stage in on if stage not in display]
    names = "·".join(_STAGE_KO[stage] for stage in extra)
    if not display:
        return f"꺼짐 (켜져 있으나 동작 안 함: {names})"
    if len(display) == len(STAGES):
        return "전체"
    line = "·".join(_STAGE_KO[stage] for stage in display) + "만"
    if extra:
        line += f" (동작 안 함: {names} — 앞 단계가 꺼져 있음)"
    return line


FC_COUNT_DAYS = 7


def recent_fastcheck_counts(repo: str, days: int = FC_COUNT_DAYS,
                            home=None, now: float | None = None) -> dict:
    """M2.5a/d: passing fastcheck receipts in the window, per repo.

    {"verified": N, "direct": M, "via": V, "notrun": X,
    "unconfirmed": K} — M counts exercised.status == "direct", V
    counts "via-tool", X counts "not-run"; K (legacy/unknown) is
    the remainder, never negative. Best-effort read-only over
    integration_ops; never raises, so the glance never breaks."""
    from . import store as _store
    from .util import read_json
    base = paths.home() if home is None else Path(home)
    cutoff = (now if now is not None else time.time()) - days * 86400
    verified, direct, via, notrun = 0, 0, 0, 0
    try:
        files = sorted((base / "integration_ops").glob(
            "*.fastcheck.json"))
    except OSError:
        return {"verified": 0, "direct": 0, "via": 0, "notrun": 0,
                "unconfirmed": 0}
    for path in files:
        # M25a-F1: one bad receipt (missing run, corrupt JSON — both
        # surface as SystemExit from read_json/store.load) never kills
        # the glance. KeyboardInterrupt still propagates.
        try:
            if path.stat().st_mtime < cutoff:
                continue
            summary = read_json(path, default=None)
            if not isinstance(summary, dict) or not summary.get("ok"):
                continue
            op_id = path.name[: -len(".fastcheck.json")]
            op_rec = read_json(base / "integration_ops" / (op_id + ".json"),
                               default=None)
            if not isinstance(op_rec, dict):
                continue
            run_id = str(op_rec.get("run_id") or "")
            if not run_id:
                continue
            state = _store.load(run_id)
            if not isinstance(state, dict) \
                    or str(state.get("repo") or "") != repo:
                continue
        except (Exception, SystemExit):  # noqa: BLE001 - skip, count on
            continue
        verified += 1
        exercised = summary.get("exercised") or {}
        status = exercised.get("status")
        if status == "direct":
            direct += 1
        elif status == "via-tool":
            via += 1
        elif status == "not-run":
            notrun += 1
    # M2.5c/d: legacy/unknown receipts are "확인 전", never "not
    # run" — and "not-run" census receipts get their own part.
    out = {"verified": verified, "direct": direct, "via": via,
           "notrun": notrun,
           "unconfirmed": max(
               0, verified - direct - via - notrun)}
    return out


def fastcheck_count_line(repo: str, **kwargs) -> str:
    """M2.5c/d glance wording (no bare "검증" — the verdict line
    owns that word for reviews). Zero parts are omitted, except N:
    "빠른 검사 통과 N건(7일) · 직접 실행 확인 M건 · 도구 경유 V건 ·
    직접 실행 안 됨 X건 · 확인 전 K건"."""
    counts = recent_fastcheck_counts(repo, **kwargs)
    parts = [f"빠른 검사 통과 {counts['verified']}건({FC_COUNT_DAYS}일)"]
    if counts["direct"]:
        parts.append(f"직접 실행 확인 {counts['direct']}건")
    if counts["via"]:
        parts.append(f"도구 경유 {counts['via']}건")
    if counts["notrun"]:
        parts.append(f"직접 실행 안 됨 {counts['notrun']}건")
    if counts["unconfirmed"]:
        parts.append(f"확인 전 {counts['unconfirmed']}건")
    return " · ".join(parts)


def promoted_guard(home=None, _running=None) -> dict:
    """M2 starts only on the promoted copy. Returns {"ok", "reason"}.

    `_running` injects the running sha in tests; production reads
    promote.running_sha(). The reason is the exact glance wording —
    never a silent no-op."""
    sha = _running if _running is not None else promote.running_sha(home)
    if sha:
        return {"ok": True, "reason": ""}
    return {"ok": False, "reason": GUARD_MESSAGE}


class OpBusy(Exception):
    """Another check holds this op's exclusion lock."""

    def __init__(self, op_id: str):
        self.op_id = op_id
        super().__init__(f"다른 확인 작업이 {op_id}에서 실행 중입니다")


def check_op_id(op_id: str) -> str:
    """Sanitized op id; empty-after-sanitize raises ValueError (never a
    shared catch-all lock)."""
    safe = "".join(c for c in str(op_id or "")
                   if c.isalnum() or c in ("-", "_"))[:120]
    if not safe:
        raise ValueError(f"invalid op id {op_id!r}")
    return safe


def op_lock_path(op_id: str, home=None) -> Path:
    base = paths.home() if home is None else Path(home)
    return base / "integration_ops" / ("." + check_op_id(op_id) + ".lock")


@contextlib.contextmanager
def op_locked(op_id: str, home=None):
    """Per-op exclusion shared by prepare/reresolve/rollup and
    fastcheck: same op id, one holder.

    ACQUIRE ORDER (hard rule): global worker lease -> op lock, never
    the reverse. A fastcheck holds an op lock for minutes and NEVER
    takes the global lease while holding one; UI entries take the
    lease first (serve layer) and this decorator second. A blocking
    LOCK_EX here would hang an HTTP thread while it holds the lease
    and stall every repo — hence LOCK_EX|LOCK_NB: contention raises
    OpBusy (409 at the API, naming the op) instead of waiting. Any
    other OSError stays best-effort (yields None), mirroring the
    other locks."""
    fh = None
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is not None:
        # ValueError (bad op id) propagates: never run guarded work
        # under a catch-all lock. Only flock failures stay best-effort.
        path = op_lock_path(op_id, home)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a+b")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fh.close()
                fh = None
                raise OpBusy(check_op_id(op_id))
            except OSError:
                fh.close()
                fh = None
        except OpBusy:
            raise
        except OSError:
            fh = None
    try:
        yield fh
    finally:
        if fh is not None:
            try:
                import fcntl as _fc
                try:
                    _fc.flock(fh.fileno(), _fc.LOCK_UN)
                finally:
                    fh.close()
            except Exception:
                pass


def op_guarded(id_of, busy=None):
    """Decorator: run the entry under the per-op exclusion lock(s).

    `id_of` maps the entry's args to one op id or a list (acquired in
    sorted order). The key is always the op whose worktree is written
    or whose evidence is read. `busy(op_id)` converts OpBusy to the
    caller's error type (batches passes IntegrationError); without it
    OpBusy propagates. ValueError (bad op id) always propagates."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrap(*args, **kwargs):
            ids = id_of(*args, **kwargs)
            if isinstance(ids, str):
                ids = [ids]
            try:
                with contextlib.ExitStack() as stack:
                    for op_id in sorted(set(ids)):
                        stack.enter_context(op_locked(op_id))
                    return fn(*args, **kwargs)
            except OpBusy as exc:
                if busy is not None:
                    raise busy(exc.op_id) from exc
                raise
        return wrap
    return deco


def refuse(repo: str, reason: str, stage: str = "") -> dict:
    """Record an M2 refusal as a Korean timeline event; return it."""
    events.append(repo, "m2-blocked", stage, f"자율 {stage} 막힘" if stage
                  else "자율 막힘", detail=reason, actor="m2")
    return {"ok": False, "reason": reason}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _m2_defaults(st: dict) -> dict:
    st.setdefault("m2_lease_busy_since", None)
    st.setdefault("m2_consec_blocks", {})
    st.setdefault("m2_block_epoch", {})
    st.setdefault(_COOLDOWN_KEY, {})
    st.setdefault("m2_notes", {})
    st.setdefault("m2_max_prepare_per_tick", 1)
    st.setdefault("m2_last_guard_ok", None)
    return st


def _note_once(repo: str, key: str, kind: str, title: str,
               detail: str = "", sig: str = None,
               run: str = None) -> bool:
    """Timeline event with per-key memory (R3): each key remembers its
    last signature and re-emits only on change, so N skipping
    candidates cost N events total — not N per tick. `run` tags
    per-candidate notes so _prune_notes can drop keys whose run has
    left the queue."""
    if sig is None:
        sig = title + "\n" + detail
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        notes = st.get("m2_notes") or {}
        prev = notes.get(key) or {}
        if prev.get("sig") == sig:
            return False
        notes[key] = {"sig": sig, "at": _now_iso(), "run": run}
        st["m2_notes"] = notes
        st.pop("m2_last_note", None)
        sched.save(st)
    events.append(repo, kind, key, title, detail, actor="m2")
    return True


def _prune_notes(repo: str, run_ids: set) -> None:
    """Drop per-candidate note memory for runs no longer queued."""
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        notes = st.get("m2_notes") or {}
        kept = {key: val for key, val in notes.items()
                if not (val or {}).get("run")
                or (val or {}).get("run") in run_ids}
        if len(kept) != len(notes):
            st["m2_notes"] = kept
            sched.save(st)


def _guard_transition(repo: str, ok: bool, reason: str = "") -> None:
    """Guard-state change only: steady states stay quiet, a flip (dev
    to promoted or back) records one honest event."""
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        if st.get("m2_last_guard_ok") is ok:
            return
        st["m2_last_guard_ok"] = ok
        sched.save(st)
    if ok:
        events.append(repo, "m2-guard", "promoted",
                      "자율 실행 가능해짐",
                      "검토된 앱 사본에서 실행 중입니다", actor="m2")
    else:
        events.append(repo, "m2-guard", "dev",
                      "자율 실행 대기 중", reason, actor="m2")


def _lane_in_cooldown(st: dict, lane: str, now: float) -> bool:
    try:
        until = (st.get(_COOLDOWN_KEY) or {}).get(lane) or ""
        if not until:
            return False
        return now < datetime.fromisoformat(until).timestamp()
    except (ValueError, TypeError, OverflowError):
        return False


def _count_block(repo: str, lane: str) -> None:
    """One more consecutive block; the third opens a 6-hour cooldown."""
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        blocks = st.get("m2_consec_blocks") or {}
        count = int(blocks.get(lane) or 0) + 1
        blocks[lane] = count
        st["m2_consec_blocks"] = blocks
        if count >= _COOLDOWN_BLOCKS:
            until = datetime.fromtimestamp(time.time() + _COOLDOWN_S,
                                           timezone.utc).isoformat()
            cool = st.get(_COOLDOWN_KEY) or {}
            cool[lane] = until
            st[_COOLDOWN_KEY] = cool
            blocks[lane] = 0
            # L2: the epoch marks the reset, so a later passing
            # re-run can tell whether its stamped contribution was
            # consumed by this opening or is still in the run.
            epochs = st.get("m2_block_epoch") or {}
            epochs[lane] = int(epochs.get(lane) or 0) + 1
            st["m2_block_epoch"] = epochs
        sched.save(st)
    if count >= _COOLDOWN_BLOCKS:
        events.append(repo, "m2-cooldown", lane,
                      f"자율 {lane} 레인 6시간 대기",
                      "3회 연속 막힘 — 식은 뒤 사람이 확인하고 다시 켜세요",
                      actor="m2")


def _clear_blocks(repo: str, lane: str) -> None:
    """Clear one lane's consecutive-block count after a success.

    FIX-b2: the clear starts a new counting generation — the
    epoch bumps even when the count was already absent, so an old
    fastcheck stamp can never cancel a newer failure's count.
    Release honours same-generation stamps only.
    """
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        blocks = st.get("m2_consec_blocks") or {}
        if lane in blocks:
            del blocks[lane]
            st["m2_consec_blocks"] = blocks
        epochs = st.get("m2_block_epoch") or {}
        epochs[lane] = int(epochs.get(lane) or 0) + 1
        st["m2_block_epoch"] = epochs
        sched.save(st)


def tick_repo(repo: str, home=None, _running=None, _prepare=None,
              _prepare_family=None) -> dict:
    """M2.1b-1 daemon driver: auto-prepare verified candidates.

    Runs inside the daemon tick process and checks the promoted guard
    in THIS process at every start (never trusts the glance value). A
    stage runs only when its own toggle and every earlier toggle are
    on, re-read at stage start. Lease then op lock (the documented
    order); lease busy is a skip, blocked only after 45 minutes of
    continuous busy. Never auto-retries: any existing op record for a
    run (done, blocked or started) means hands off. Every start with
    work to do, done, blocked and skip becomes a Korean timeline
    event (idle repeats deduplicated). Never raises: failures return
    {"ran": False, "reason": ...} so one repo never kills the tick.
    """
    try:
        return _tick_repo_inner(repo, home=home, _running=_running,
                                _prepare=_prepare,
                                _prepare_family=_prepare_family)
    except Exception as exc:  # noqa: BLE001 - one repo never kills a tick
        return {"ran": False,
                "reason": f"{type(exc).__name__}: {exc}"[:200]}


def _tick_repo_inner(repo, home=None, _running=None, _prepare=None,
                     _prepare_family=None):
    from . import batches as _batches
    from .batches import IntegrationError as _IntegrationError
    try:
        policy = get_policy(repo)
    except SystemExit:
        return {"ran": False, "reason": "unknown repo"}
    if not any(policy.get(stage) for stage in STAGES):
        # Fully off: strictly inert. No guard call, no events, no
        # timeline or snapshot writes — a daemon tick running this
        # tree stays a read-only no-op for this repo.
        return {"ran": False, "reason": "다음 단계 꺼짐"}
    guard = promoted_guard(home=home, _running=_running)
    if not guard["ok"]:
        _guard_transition(repo, False, guard["reason"])
        return {"ran": False, "reason": guard["reason"]}
    _guard_transition(repo, True)
    if not policy.get("prepare"):
        if any(policy.get(stage) for stage in STAGES):
            _note_once(repo, "skip-off", "m2-skip",
                       "자율 다음 단계 꺼짐",
                       "준비 단계가 꺼져 있어 아무것도 실행하지 않습니다")
        return {"ran": False, "reason": "다음 단계 꺼짐"}
    if not STAGE_OPEN.get("prepare"):
        _note_once(repo, "skip-unopened", "m2-skip",
                   f"자율 준비 {NOT_OPEN_NOTE}",
                   "준비 단계가 아직 열리지 않았습니다")
        return {"ran": False, "reason": NOT_OPEN_NOTE}
    prepare = (_prepare if _prepare is not None
               else _batches.prepare_integration)
    # M2.6: the family entry resolves lazily at the use site —
    # batches.prepare_family_integration may not exist yet, and
    # resolving it eagerly would fail every tick (even with no
    # family in the queue). _prepare_family=None means "use the
    # real entry when a family actually prepares".
    prepare_family = _prepare_family
    queue = [i for i in (sched.load(repo).get("queue") or [])
             if isinstance(i, dict)]
    # M2.6: superseded runs (older reviewed target of the same dep
    # on the same lane) and release-train families (same lane,
    # target, scan and scope) are computed once, read-only, before
    # the per-item loop.
    superseded = _supersede_map(queue)
    fam_groups = _family_groups(queue)
    # M2.5b wording: dep-update vs issue-triage per run, for the
    # backlog count (issue runs already have their own line).
    queue_kinds = {str(i.get("run_id") or ""): _queue_kind(i)
                   for i in queue if i.get("run_id")}
    _prune_notes(repo, {str(i.get("run_id") or "")
                        for i in queue if i.get("run_id")})
    now = time.time()
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        cool = dict(st.get(_COOLDOWN_KEY) or {})
        busy_since = st.get("m2_lease_busy_since")
        try:
            cap = int(st.get("m2_max_prepare_per_tick") or 1)
        except (TypeError, ValueError):
            cap = 1
        cap = max(1, cap)
    prepared, skipped = [], []
    skip_groups = {}
    cooling = {}
    tick = {"refresh_queued": False}

    def _skip(run_id, group, reason, note=""):
        # D4: per-run reasons stay in the return value, but the
        # timeline gets ONE summary event per tick and reason. The
        # optional note overrides that run's detail entry (used by
        # the superseded group for "<dep> <old> -> <new>").
        skipped.append({"run": run_id, "reason": reason})
        g = skip_groups.setdefault(group, {"reason": reason,
                                           "runs": [], "notes": {}})
        g["runs"].append(run_id)
        g["notes"][run_id] = note or run_id

    def _emit_skips():
        # D4: one summary event per tick and reason (per-key memory
        # re-emits only when the count or sample changes, so a
        # steady backlog stays quiet after the first line).
        # E5: when the backlog-cooling note below fired, it already
        # names the cooling lane and until-time — the cooldown group
        # stays quiet so exactly one event says it per tick.
        for group in sorted(skip_groups):
            if group == "cooldown" and tick.get("cooling_noted"):
                continue
            g = skip_groups[group]
            runs = g["runs"]
            if group == "superseded":
                # M2.6b: the title never carries one run's version —
                # per-run "<dep> <old> -> <new>" goes in the detail.
                _note_once(
                    repo, "skip-superseded", "m2-skip",
                    f"대체됨 {len(runs)}건 (더 새 버전이 대기 중)",
                    ", ".join(g["notes"].get(r, r) for r in runs[:5])
                    + (f" 외 {len(runs) - 5}건" if len(runs) > 5
                       else ""))
                continue
            _note_once(repo, f"skip-{group}", "m2-skip",
                       f"자율 준비 건너뜀 {len(runs)}건: {g['reason']}",
                       ", ".join(g["notes"].get(r, r) for r in runs[:5])
                       + (f" 외 {len(runs) - 5}건" if len(runs) > 5
                          else ""))

    # Phase 1 (read-only): the R1 predicate, the review-accepted
    # pre-flight, the op-record check and the base choice. Nothing
    # here takes the lease or mutates.
    eligible = []
    fresh = []
    resume = []
    fc_resume = []
    # M2.6b: blocked members rejoining their train (see above) —
    # bucketed with the fresh items so the train runs once.
    retry_trains = []
    retry_runs = set()
    # M2.17 B: fresh items diverted to a refreshed joint derive
    # (at most one derive per tick, in Phase 2/3).
    joint_todo = []
    # M2.4d: already-batched resumes are dropped in Phase 1 (below)
    # before they can consume the per-tick cap; their grouped note is
    # emitted with the others in Phase 3.
    dup_skips = []
    lockstep_skips = []
    compiler_skips = []
    dup_map = None
    # M2.14b: one op runs once per tick — X and Y items of a done
    # joint record would otherwise resume (and fastcheck/batch) the
    # same op twice. The partner rides along via the joint record.
    resumed_ops = set()
    for item in sorted(
            queue, key=lambda i: (str(i.get("created_at") or ""),
                                  str(i.get("run_id") or ""))):
        if item.get("status") != "waiting-review" or not item.get(
                "run_id"):
            continue
        run_id = str(item["run_id"])
        lane = str(item.get("lane") or "")
        if cool and _lane_in_cooldown({_COOLDOWN_KEY: cool},
                                      lane, now):
            # E5: a cooling lane is an honest grouped skip — one
            # m2-skip event per tick naming the lane and its
            # release time, never a silent pass.
            cooling[lane] = cool.get(lane) or ""
            _skip(run_id, "cooldown",
                  f"{lane} 레인 {_kst_short(cool.get(lane))}까지 쉼 "
                  f"(연속 막힘 {_COOLDOWN_BLOCKS}회)")
            continue
        if item.get("verified") is not True:
            # R1: sched parks no-code / watch / operator-wait items in
            # waiting-review with verified False — never act on them.
            _skip(run_id, "unverified", "리뷰 승인 전")
            continue
        if run_id in superseded:
            # M2.6: an older reviewed target of the same dep on the
            # same lane — the newer target supersedes it. Skipped
            # once (grouped note), never prepared, never a block.
            # M2.6b: the grouped title stays version-free; the
            # per-run "<dep> <old> -> <new>" rides the note.
            _skip(run_id, "superseded",
                  f"대체됨 ({superseded[run_id]} 있음)",
                  note=f"{item.get('dep') or ''} "
                       f"{item.get('target') or ''} -> "
                       f"{superseded[run_id]}")
            continue
        proof_error = _batches.candidate_proof_error(run_id)
        if proof_error:
            _skip(run_id, "proof", proof_error[:200])
            continue
        try:
            existing = _batches.latest_integration_op(run_id)
        except Exception:  # noqa: BLE001 - R4: an unreadable op
            # record fails CLOSED: skip, never prepare blind.
            _skip(run_id, "opaque", "작업 기록 확인 실패")
            continue
        if isinstance(existing, dict):
            # M2.6b2: a blocked family attempt never auto-retries —
            # the train waits for the explicit family-retry with one
            # grouped note, never a new attempt and never a new
            # count. Done family records fall through to resume.
            if str(existing.get("status") or "") == "blocked" \
                    and isinstance(existing.get("family"), dict):
                _skip(run_id, "family-blocked", _FAMILY_BLOCKED_NOTE)
                continue
            # M2.6b2: reunion is single-records only (no family
            # field, not superseded_by) and at most once per train
            # (no non-done family-role record anywhere in it) — a
            # family attempt that blocked must not loop every tick.
            _rgrp = fam_groups.get(
                (lane, str(item.get("target") or ""),
                 str(item.get("created_at") or ""),
                 _family_key(str(item.get("dep") or ""))))
            _single_reunion = (
                str(existing.get("status") or "") == "blocked"
                and not isinstance(existing.get("family"), dict)
                and not str(existing.get("superseded_by") or "")
                and _rgrp is not None
                and len(_rgrp["verified"]) >= 2
                and not _rgrp["unverified"]
                and run_id in {str(v.get("run_id") or "")
                               for v in _rgrp["verified"]})
            _fam_rec, _fam_ok = None, True
            if _single_reunion:
                _fam_rec, _fam_ok = _train_family_record(
                    _rgrp["verified"])
            if _single_reunion and _fam_ok and _fam_rec is not None:
                # The train already tried as a family and blocked —
                # wait for the explicit retry, same grouped note.
                _skip(run_id, "family-blocked", _FAMILY_BLOCKED_NOTE)
                continue
            if _single_reunion and _fam_ok and _fam_rec is None:
                # A stranded single rejoins its train: the family
                # runs once with retry, prior blocked member records
                # archive (P-HIST) and are marked superseded_by,
                # never deleted.
                _retry_base = _batches.default_base(
                    repo, lane, ambiguous="empty")
                _retry_base_id = (_retry_base or {}).get("id") or ""
                if _retry_base_id and not _base_present(
                        repo, run_id, _retry_base_id):
                    retry_trains.append((item, _retry_base_id))
                    retry_runs.add(run_id)
                    continue
            # M2.5b: a done prepare (or a done lockfix derived from
            # one) held by the RN lockstep never resumes anywhere —
            # neither fastcheck nor batch could ship it alone. One
            # grouped skip, never a block.
            pipe = _pipeline_prepare_rec(existing)
            if pipe is not None \
                    and str(pipe.get("status") or "") == "done":
                held = lockstep_status(
                    str((pipe.get("result") or {}).get("path") or ""),
                    pipe.get("dep"), pipe.get("target"))
                if held is not None:
                    _skip(run_id, "lockstep", held["reason"])
                    skipped.append({"run": run_id,
                                    "reason": held["reason"]})
                    continue
            # M2.17b A: a DONE single-member derived op the batch
            # stage refuses for a mixed RN group diverts to a
            # refreshed joint instead of resuming into another
            # doomed batch — when a done joint covers the partner.
            # Read-only decision; the derive runs at most once per
            # tick with the fresh-item diverts below.
            if isinstance(existing, dict) \
                    and str(existing.get("status") or "") == "done":
                _rbase = _batches.default_base(
                    repo, lane, ambiguous="empty")
                _rbase_id = (_rbase or {}).get("id") or ""
                if _rbase_id and not _base_present(
                        repo, run_id, _rbase_id):
                    _rplan = joint_refresh_for_refused(
                        repo, lane, existing, _rbase_id)
                    if _rplan is not None:
                        joint_todo.append((item, _rbase_id, _rplan))
                        continue
            # S18a: a done prepare with no fastcheck receipt yet
            # resumes into the fastcheck stage first (any origin —
            # the live UI-derived done that strands receipt-less
            # converges here too). A passing receipt still goes to
            # _batch_resume; a red one stays owner work.
            fc_res = _fc_resume_eligible(policy, run_id, lane,
                                         existing)
            if fc_res is not None:
                # FIX-a1: a disabled predecessor stops the stage —
                # skip with the glance's wording, never the slot.
                _fok, _fwhy = stage_prefix_ok(policy, "fastcheck")
                if not _fok:
                    _skip(run_id, "stage-prefix", _fwhy)
                    continue
                _fc_op = str((fc_res[0] or {}).get("op_id") or "")
                if _fc_op and _fc_op in resumed_ops:
                    _skip(run_id, "joint-carried",
                          "함께 올림 기록이 이번 틱에 이미 실행 중")
                    continue
                if _fc_op:
                    resumed_ops.add(_fc_op)
                fc_resume.append((run_id, lane) + tuple(fc_res))
                continue
            # A done prepare with a passing fastcheck receipt but no
            # batch yet resumes into the batch stage (lease-busy
            # strands and toggled-late stages converge here).
            # Everything else recorded stays for the owner: no
            # auto-retry of done, blocked or interrupted work.
            resumed = _batch_resume(policy, run_id, lane, existing)
            if resumed is not None:
                # FIX-a1: the batch stage cannot act past a disabled
                # predecessor — honest skip, no slot spent.
                _bok, _bwhy = stage_prefix_ok(policy, "batch")
                if not _bok:
                    _skip(run_id, "stage-prefix", _bwhy)
                    continue
                _b_op = str((resumed[0] or {}).get("op_id") or "")
                if _b_op and _b_op in resumed_ops:
                    _skip(run_id, "joint-carried",
                          "함께 올림 기록이 이번 틱에 이미 실행 중")
                    continue
                # M2.4d: an already-batched candidate never enters
                # `resume` — with cap 1 it would spend the only slot
                # on a dup skip and starve fresh batch work. One
                # read-only index scan per tick; the in-stage check
                # stays as the backstop.
                if dup_map is None:
                    dup_map = _batched_candidate_map(repo)
                dup_bid = _phase1_dup(repo, run_id, lane, dup_map)
                # M2.17d: a refreshed joint is exempt from its old
                # joint's dup (same helper as the stage backstop);
                # and the op is claimed only once the run passes
                # this check, so a skipped run never starves its
                # partner with a false "already running" note.
                if dup_bid and not _refreshed_joint_exempts(
                        repo, resumed[0], dup_bid):
                    dup_skips.append((run_id, dup_bid))
                    skipped.append({"run": run_id,
                                    "reason": f"dup of {dup_bid}"})
                    continue
                if _b_op:
                    resumed_ops.add(_b_op)
                resume.append((run_id, lane) + tuple(resumed))
            else:
                # FIX-a4: a stale record is refresh-stage work on
                # every tick — never a block, never a recount.
                if isinstance((existing or {}).get("stale"), dict):
                    _skip(run_id, "stale", "후보가 기준보다 오래됨",
                          note=_stale_skip_detail(existing))
                    continue
                # D2: an m2-origin BLOCKED op with no m2-blocked
                # event yet gets exactly one honest correction
                # (once per op via the blocked-<op_id> key); the
                # earlier false done keeps its history line.
                if (str(existing.get("status") or "") == "blocked"
                        and str(existing.get("origin") or "") == "m2"
                        and not _reconcile_done(
                            repo, str(existing.get("op_id") or ""))
                        # M2.6: family-deferred lone blocks carry
                        # their grouped note already, and member-role
                        # family records are covered by the primary's
                        # event — reconciling either would count a
                        # block the family path explicitly avoided.
                        and not str(((existing.get("result") or {})
                                     .get("family_needed")) or "")
                        and str(((existing.get("family") or {})
                                 .get("role")) or "") != "member"):
                    _block_prepare(
                        repo, lane, run_id,
                        str(existing.get("op_id") or ""),
                        _blocked_message(existing), rec=existing,
                        correcting=_had_false_done(repo, run_id),
                        count=not _refresh_pending_for(repo, run_id))
                skipped.append({"run": run_id,
                                "reason": "이미 기록된 작업 있음"})
            continue
        base = _batches.default_base(repo, lane, ambiguous="empty")
        base_id = (base or {}).get("id") or ""
        if not base_id:
            # R6: an ambiguous tie is the owner's call, not a guess.
            reason = ("기준 묶음 선택 필요"
                      if (base or {}).get("tie", 0) > 1
                      else "기준 묶음 없음")
            _skip(run_id, "nobase", reason)
            continue
        if _base_present(repo, run_id, base_id):
            # E4: recorded as already integrated on this base — skip
            # early, without spending the prepare slot.
            _skip(run_id, "present", "이미 기준 묶음에 포함")
            continue
        held = _fresh_lockstep_held(base_id, lane, item)
        if held is not None:
            # M2.5b: an RN-lockstep family member never prepares
            # alone — a grouped skip, never a block.
            _skip(run_id, "lockstep", held)
            continue
        # M2.17 B: a newer target of a done joint's member
        # diverts to a refreshed joint below (joint action) —
        # never a doomed standalone, never an A-skip.
        joint_plan = joint_refresh_for(repo, lane, item, base_id,
                                       queue)
        if joint_plan is not None:
            joint_todo.append((item, base_id, joint_plan))
            continue
        refused = preprepare_refusal(lane, item, base_id, queue)
        if refused is not None:
            # M2.17 A: the batch stage would refuse this exact
            # singleton — skip the prepare+fastcheck spend with
            # a grouped note. The candidate stays queued: a
            # human can still prepare it manually from the UI.
            # Never a block, never a cooldown count.
            _skip(run_id, "preprepare", refused)
            continue
        # M2.6: a verified member whose train siblings still await
        # review waits with them — never prepares partially.
        _grp = fam_groups.get(
            (lane, str(item.get("target") or ""),
             str(item.get("created_at") or ""),
             _family_key(str(item.get("dep") or ""))))
        if _grp is not None and _grp["unverified"]:
            _skip(run_id, "family-wait",
                  f"가족 준비 대기: 리뷰 남음 "
                  f"{len(_grp['unverified'])}건")
            continue
        if _grp is not None and len(_grp["verified"]) >= 2 \
                and not _grp["unverified"]:
            # M2.6b2: a train with a blocked family attempt waits
            # whole — a record-less member must not solo into the
            # same conflict. Unreadable member records fail closed.
            _fam_rec, _fam_ok = _train_family_record(
                _grp["verified"])
            if _fam_rec is not None:
                _skip(run_id, "family-blocked", _FAMILY_BLOCKED_NOTE)
                continue
            if not _fam_ok:
                _skip(run_id, "opaque", "작업 기록 확인 실패")
                continue
        fresh.append((item, base_id))
    # M2.6: release-train families — fresh items sharing lane,
    # target, scan, scope and the chosen base prepare as ONE
    # combined candidate. A member already riding a batch diverts
    # the whole family to the dup note (never a partial prepare).
    # M2.6b: retry trains bucket with the fresh items — same key,
    # same base resolution — so a stranded single rejoins its
    # siblings instead of stranding them in turn.
    fresh.extend(retry_trains)
    fam_buckets: dict = {}
    for item, base_id in fresh:
        fam_buckets.setdefault(
            (str(item.get("lane") or ""),
             str(item.get("target") or ""),
             str(item.get("created_at") or ""),
             _family_key(str(item.get("dep") or "")),
             base_id), []).append((item, base_id))
    families = []
    for _key, members in sorted(
            fam_buckets.items(),
            key=lambda kv: (str(kv[1][0][0].get("created_at") or ""),
                            str(kv[1][0][0].get("run_id") or ""))):
        if len(members) < 2:
            eligible.append(members[0])
            continue
        if dup_map is None:
            dup_map = _batched_candidate_map(repo)
        dup_bid = ""
        for item, _base_id in members:
            dup_bid = _phase1_dup(
                repo, str(item.get("run_id") or ""),
                str(item.get("lane") or ""), dup_map)
            if dup_bid:
                break
        if dup_bid:
            for item, _base_id in members:
                run_id = str(item.get("run_id") or "")
                dup_skips.append((run_id, dup_bid))
                skipped.append({"run": run_id,
                                "reason": f"dup of {dup_bid}"})
            continue
        families.append(members)
    # Phase 2 (R2): at most `cap` actions per repo per tick, oldest
    # first — resumed batch work (further along the pipeline) goes
    # before fresh prepares; one note says how many still wait. A
    # family consumes one slot for all its members.
    resume_waiting = max(0, len(resume) - cap)
    resume = resume[:cap]
    # S18a: fastcheck-resume shares the same per-tick cap — resumes
    # (further along) first, then receipt-less done, then fresh.
    fc_room = max(0, cap - len(resume))
    fc_waiting = max(0, len(fc_resume) - fc_room)
    fc_resume = fc_resume[:fc_room]
    actions = [("single", [pair]) for pair in eligible]
    actions += [("family", members) for members in families]
    actions.sort(key=lambda a: (
        str(a[1][0][0].get("created_at") or ""),
        str(a[1][0][0].get("run_id") or "")))
    room = max(0, cap - len(resume) - len(fc_resume))
    fresh_runs = sum(len(members) for _kind, members in actions)
    # A family takes one action slot whole — oldest first, never
    # split across ticks.
    cut = actions[:room]
    eligible = [members[0] for kind, members in cut
                if kind == "single"]
    families_todo = [members for kind, members in cut
                     if kind == "family"]
    cut_runs = sum(len(members) for _kind, members in cut)
    fresh_waiting = max(0, fresh_runs - cut_runs)
    if cooling and not resume and not eligible and not families_todo:
        # E5: every actionable item is in a cooling lane — the old
        # "이번 틱에 N건씩 준비" promise under this same key would be
        # a lie, so the cooling truth replaces it (same key, new
        # signature re-emits; the promise returns when work resumes).
        desc = ", ".join(
            f"{lane} 레인 {_kst_short(until)}까지"
            for lane, until in sorted(cooling.items()))
        _note_once(repo, "backlog", "m2-skip",
                   f"자율 준비 대기 중: {desc} 쉼 "
                   f"(연속 막힘 {_COOLDOWN_BLOCKS}회)",
                   ", ".join(skip_groups.get(
                       "cooldown", {}).get("runs", [])[:5]))
        tick["cooling_noted"] = True
    elif resume_waiting + fc_waiting + fresh_waiting > 0:
        _note_once(repo, "backlog", "m2-skip",
                   f"자율 준비 대기 "
                   f"{resume_waiting + fc_waiting + fresh_waiting}건",
                   f"이번 틱에 {cap}건씩 순서대로 준비합니다")
    else:
        # M2.5b wording: issue-triage runs are already counted as
        # "이슈 검토 대기" — this line counts dep-update runs only,
        # and stays silent when only issue runs wait. (M3.3a: the
        # silent-0 case — same backlog key, so this line replaces
        # the 대기 line when work resumes.)
        dep_pending = [r for r in skip_groups.get("unverified", {})
                       .get("runs", [])
                       if queue_kinds.get(r, "dep-update") == "dep-update"]
        if dep_pending and not resume and not fc_resume \
                and not eligible and not families_todo:
            _note_once(repo, "backlog", "m2-skip",
                       f"검토 대기 {len(dep_pending)}건 — 리뷰 승인 후 자율 준비",
                       ", ".join(dep_pending[:5]))
    # Phase 3: lease then op lock (the documented order), one
    # candidate at a time. Resumes first: they only need the batch
    # stage, never a new prepare.
    batched = []
    # `dup_skips` is filled in Phase 1 (above) and here; both feed
    # the single grouped note below.

    def _batch_one(run_id, lane, rec, base_id, summary):
        # M2.17d: a resumed joint always batches as its own
        # primary run (the fresh member) — whichever member item
        # carried it here. The manifest binds rec dep/target to
        # that run, so any other run_id would misbind the queue
        # metadata or duplicate a run at import.
        if isinstance(rec, dict) and rec.get("kind") == "joint":
            _primary_run = str(rec.get("run_id") or "")
            if _primary_run:
                run_id = _primary_run
        extras, divert = _family_extras(repo, rec)
        if extras is None and not divert:
            # M2.14b: a joint record batches with its runs inline
            # (a partner riding a held standalone batch never
            # diverts — the joint supersedes it).
            extras, divert = _joint_extras(repo, rec)
        if divert.startswith("dup:"):
            # M2.6b: a member already rides a batch — the whole
            # resume diverts to the grouped dup note, never a
            # partial batch missing its train.
            dup_skips.append((run_id, divert[len("dup:"):]))
            return
        if divert == "wait":
            _skip(run_id, "family-resume",
                  "가족 구성원 확인 실패 — 다음 틱에 계속")
            return
        bout = _run_batch_stage(repo, run_id, lane, rec, base_id,
                                summary, extra_primaries=extras)
        if (bout or {}).get("ran"):
            batched.append({"run": run_id,
                            "batch": (bout or {}).get("batch")})
        elif str((bout or {}).get("reason") or "").startswith(
                "stage off"):
            # FIX-a1 backstop for direct callers: the stage-entry
            # refusal surfaces as an honest skip.
            _skip(run_id, "stage-prefix",
                  str(bout["reason"])[len("stage off: "):])
        elif str((bout or {}).get("reason") or "").startswith(
                "dup of "):
            dup_skips.append(
                (run_id,
                 str(bout["reason"])[len("dup of "):]))
        elif str((bout or {}).get("reason") or "").startswith(
                "lockstep "):
            lockstep_skips.append(
                (run_id,
                 str(bout["reason"])[len("lockstep "):]))
        elif str((bout or {}).get("reason") or "").startswith(
                "compiler "):
            # M2.16b: the label rides the glance (grouped note
            # below) and the run's card (per-run skip entry) —
            # still refusal-only: never a block, never a
            # cooldown count.
            _clabel = str(bout["reason"])[len("compiler "):]
            compiler_skips.append((run_id, _clabel))
            skipped.append({"run": run_id, "reason": _clabel})

    def _emit_dup_skips():
        # M2.4c: one grouped line per tick (per-key memory re-emits
        # only on change), never a block.
        if not dup_skips:
            return
        bids = sorted({bid for _, bid in dup_skips})
        _note_once(repo, "dup-batch", "m2-skip",
                   f"자율 묶음 건너뜀 {len(dup_skips)}건: "
                   f"이미 묶음 {', '.join(bids)}에 포함",
                   ", ".join(run for run, _ in dup_skips[:5]))

    def _emit_lockstep_skips():
        # M2.5b: family members waiting for their react-native bump —
        # one grouped line per tick, never a block.
        if not lockstep_skips:
            return
        reasons = sorted({reason for _, reason in lockstep_skips})
        _note_once(repo, "lockstep-batch", "m2-skip",
                   f"자율 묶음 보류 {len(lockstep_skips)}건: "
                   + " · ".join(reasons[:3]),
                   ", ".join(run for run, _ in lockstep_skips[:5]))

    def _emit_compiler_skips():
        # M2.16b: runtime-helpers MAJOR ahead of its compiler —
        # one grouped line per tick, never a block.
        if not compiler_skips:
            return
        reasons = sorted({reason for _, reason in compiler_skips})
        _note_once(repo, "compiler-batch", "m2-skip",
                   f"자율 묶음 보류 {len(compiler_skips)}건: "
                   + " · ".join(reasons[:3]),
                   ", ".join(run for run, _ in compiler_skips[:5]))

    for run_id, lane, rec, base_id, summary in resume:
        _batch_one(run_id, lane, rec, base_id, summary)
    # S18a execution: receipt-less done runs go through the SAME
    # fastcheck stage as fresh prepares — a red step blocks the lane
    # with its tail exactly as today, and a pass continues to batch.
    for run_id, lane, rec, base_id in fc_resume:
        fc_out = _run_fastcheck_stage(repo, run_id, lane, rec,
                                      base_id)
        if fc_out.get("ran"):
            _batch_one(run_id, lane, rec, base_id,
                       fc_out.get("summary") or {})
        elif str((fc_out or {}).get("reason") or "").startswith(
                "stage off"):
            # FIX-a1 backstop: same honest skip as Phase 1.
            _skip(run_id, "stage-prefix",
                  str(fc_out["reason"])[len("stage off: "):])
    _emit_dup_skips()
    _emit_lockstep_skips()
    _emit_compiler_skips()
    # M2.17 B: at most one refreshed-joint derive per tick. The
    # derive runs the M2.14 joint path with the FRESH run (y) as
    # primary, so the next tick resumes the new joint op instead
    # of re-deriving. A refusal is an honest block; the diverted
    # standalone is skipped, never prepared.
    if joint_todo:
        _jitem, _jbase, _jplan = joint_todo[0]
        _jrun = str(_jitem.get("run_id") or "")
        _jlane = str(_jitem.get("lane") or "")
        _jlease = sched.acquire_worker_lease(f"m2-{repo}")
        if _jlease is None:
            _emit_skips()
            return _lease_busy(repo, busy_since, now, prepared, skipped)
        try:
            from . import batches as _batches
            _jrec = _batches.derive_joint_refresh(
                repo, _jlane, _jbase,
                str(_jplan.get("x_op_id") or ""),
                str(_jplan.get("y_run") or ""), by="m2")
        except _IntegrationError as exc:
            # Pre-flow refusal (unreadable inputs, moved base):
            # nothing committed — the honest block, as today.
            _record_block(repo, _jlane, _jrun, str(exc))
            _jrec = None
        finally:
            try:
                sched.release_worker_lease(_jlease)
            except Exception:  # noqa: BLE001 - release never fails
                pass
        if isinstance(_jrec, dict) \
                and str(_jrec.get("status") or "") == "done":
            _jx = (f"{_jplan.get('x_dep')} {_jplan.get('x_target')}"
                   ).strip()
            _jy = (f"{_jplan.get('y_dep')} {_jplan.get('y_target')}"
                   ).strip()
            _note_once(repo, f"done-{_jrun}", "m2-done",
                       f"자율 함께 올림 완료 {_jy} + {_jx}".rstrip(),
                       f"새로 도출 {_jy} + {_jx}",
                       run=_jrun)
            _jres = _jrec.get("result") or {}
            prepared.append({"run": _jrun, "base": _jbase,
                             "branch": "",
                             "head": _jres.get("head")})
        else:
            if _jrec is not None:
                # The flow wrote the blocked joint record; the
                # lane accounting lands here, as today.
                _record_block(
                    repo, _jlane, _jrun,
                    str(_jrec.get("error") or "joint refresh blocked"))
            _jrefused = preprepare_refusal(_jlane, _jitem, _jbase,
                                           queue)
            if _jrefused is not None:
                _skip(_jrun, "preprepare", _jrefused)
            else:
                _jerr = str((_jrec or {}).get("error") or "")[:200] \
                    or "joint refresh refused"
                _skip(_jrun, "joint-blocked", _jerr)
        if len(joint_todo) > 1:
            _note_once(repo, "joint-wait", "m2-skip",
                       f"함께 올림 대기 {len(joint_todo) - 1}건 — "
                       f"이번 틱에 1건씩 순서대로 도출",
                       ", ".join(str(i.get("run_id") or "")
                                 for i, _b, _p in joint_todo[1:6]))
    for item, base_id in eligible:
        run_id = str(item["run_id"])
        lane = str(item.get("lane") or "")
        lease = sched.acquire_worker_lease(f"m2-{repo}")
        if lease is None:
            _emit_skips()
            return _lease_busy(repo, busy_since, now, prepared, skipped)
        try:
            with sched.locked(repo):
                st = _m2_defaults(sched.load(repo))
                st["m2_lease_busy_since"] = None
                sched.save(st)
            rec = prepare(run_id, base_id, origin="m2")
        except _IntegrationError as exc:
            message = str(exc)
            if "already present in base" in message:
                # E4: the work already rides the base — a skip, never
                # a block and never a 사람이 할 일 entry; remembered
                # per run+base so later ticks skip it early.
                _remember_base_present(repo, run_id, base_id)
                _skip(run_id, "present", "이미 기준 묶음에 포함")
                continue
            if (getattr(exc, "http_status", None) == 409
                    and "실행 중입니다" in message):
                # R5: op-lock busy is a skip, never a block.
                _skip(run_id, "opbusy", "다른 확인 작업 실행 중")
                continue
            if "not fresh" in message or "drifted" in message:
                # Not verified yet: skip quietly until the next tick
                # re-checks (the queue still shows the candidate).
                _skip(run_id, "unverified", message[:200])
                continue
            if "touches more than" in message:
                # M2.6b: a lone candidate refused for reaching into
                # other lines defers to the family path when train
                # siblings wait in the queue — never a counted lone
                # block (the live ts-eslint shape).
                deferred = _defer_family_need(
                    queue, lane, run_id,
                    str(item.get("dep") or ""), None)
                if deferred:
                    _skip(run_id, "family-needed", deferred)
                    continue
            _record_block(repo, lane, run_id, message)
            continue
        finally:
            try:
                sched.release_worker_lease(lease)
            except Exception:  # noqa: BLE001 - release never fails a tick
                pass
        rec = rec or {}
        status = str(rec.get("status") or "")
        op_id = str(rec.get("op_id") or "")
        result = rec.get("result") or {}
        head = str(result.get("head") or "")
        branch = str(result.get("branch") or "")
        via_lockfix = False
        if status == "blocked":
            # S15(a): a fresh lock-only/adjacent block is worth one
            # automatic reresolve attempt (part of prepare, on with
            # it). An already-attempted op stays recorded — no
            # auto-retry of recorded work, so earlier blocked ops
            # keep the D2 path and the manual 다시 시도 button.
            scope = _batches.prepare_reresolve_scope(rec)
            if scope.get("eligible") and not _lockfix_exists(op_id):
                out = _attempt_lockfix(op_id)
                if out.get("ok"):
                    rec = out["rec"]
                    via_lockfix = True
                    status = str(rec.get("status") or "")
                    op_id = str(rec.get("op_id") or op_id)
                    result = rec.get("result") or {}
                    head = str(result.get("head") or "")
                    branch = str(result.get("branch") or "")
                    # Fall through to the shared success path: the
                    # derived done continues the pipeline, marked.
                elif out.get("busy"):
                    _skip(run_id, "opbusy", "다른 확인 작업 실행 중")
                    continue
                else:
                    deferred = _defer_family_need(
                        queue, lane, run_id,
                        str(((out.get("rec") or rec) or {})
                            .get("dep") or ""),
                        out.get("rec") or rec)
                    if deferred:
                        _skip(run_id, "family-needed", deferred)
                        continue
                    _block_prepare(
                        repo, lane, run_id, op_id,
                        out.get("error") or "재해결 실패",
                        rec=out.get("rec") or rec,
                        correcting=_had_false_done(repo, run_id))
                    continue
            elif scope.get("scope") == "same-line":
                # S15(b): stale declaration — queue one refresh run
                # on the current base instead of retrying the old op.
                # FIX-a4: a record the prepare path already marked
                # stale is refresh-stage work, never a counted
                # block; anything else keeps the old block-with-
                # exemption behaviour.
                outcome = _maybe_queue_refresh(
                    repo, lane, run_id, rec, base_id, tick)
                if isinstance((rec or {}).get("stale"), dict):
                    _skip(run_id, "stale", "후보가 기준보다 오래됨",
                          note=_stale_skip_detail(rec, outcome))
                    continue
                _block_prepare(
                    repo, lane, run_id, op_id,
                    _blocked_message(rec), rec=rec,
                    correcting=_had_false_done(repo, run_id),
                    count=(outcome not in ("queued", "duplicate")))
                continue
            else:
                # D1: a returned block is a block, never a done —
                # unless train siblings wait (M2.6 item 3 defers to
                # the family path instead of counting a lone block).
                deferred = _defer_family_need(
                    queue, lane, run_id,
                    str((rec or {}).get("dep") or ""), rec)
                if deferred:
                    _skip(run_id, "family-needed", deferred)
                    continue
                _block_prepare(repo, lane, run_id, op_id,
                               _blocked_message(rec), rec=rec,
                               correcting=_had_false_done(repo, run_id))
                continue
        if status != "done" or not head:
            _block_prepare(repo, lane, run_id, op_id,
                           f"예상치 못한 준비 상태: {status or '없음'}",
                           rec=rec)
            continue
        if not branch.startswith("codex/"):
            _record_block(repo, lane, run_id,
                          f"unexpected branch outside codex/: {branch}")
            continue
        _clear_blocks(repo, lane)
        detail = (f"기준 {base_id} · 브랜치 {branch} · "
                  f"head {str(result.get('head') or '')[:12]}"
                  + (" · 파생 잠금 출처" if via_lockfix else "")
                  # M2.9a: a re-derived done says it was re-applied
                  # on the base — never equal to the original.
                  + (" · 기준에서 다시 적용 — 원본 커밋과 같은 버전 변경"
                     if (rec or {}).get("kind") == "rederive" else ""))
        # M2.5b: a prepared react-native bump names the family
        # members riding with it, so the owner moves them together.
        if str((rec or {}).get("dep") or "") == "react-native":
            buddies = lockstep_companions(
                queue, lane, str((rec or {}).get("target") or ""))
            if buddies:
                detail += " · 같이 옮길 후보: " + ", ".join(buddies)
        # M2.10b: a group re-derive titles its companions and
        # lists them in the detail.
        _group = (rec or {}).get("group")
        _group = list(_group) if isinstance(_group, list) else None
        if _group and len(_group) > 1:
            detail += " · 함께 올림: " + ", ".join(
                str(g) for g in _group)
        _note_once(repo, f"done-{run_id}", "m2-done",
                   prepare_done_title(
                       (rec or {}).get("dep") or item.get("dep") or "",
                       _item_from_version(item),
                       (rec or {}).get("target")
                       or item.get("target") or "",
                       group=_group),
                   detail, run=run_id)
        prepared.append({"run": run_id, "base": base_id,
                         "branch": branch,
                         "head": result.get("head")})
        # The next stage is re-read fresh: a mid-flight flip stops it.
        # Fastcheck runs inline in the prepare tick only: later ticks
        # see the op record and hands off, so a fastcheck toggled on
        # after the fact does not re-run here (M2.2 batch reads the
        # receipts instead).
        nxt = get_policy(repo)
        if nxt.get("fastcheck") and STAGE_OPEN.get("fastcheck"):
            fc_out = _run_fastcheck_stage(repo, run_id, lane, rec,
                                          base_id)
            if fc_out.get("ran"):
                _run_batch_stage(repo, run_id, lane, rec, base_id,
                                 fc_out.get("summary") or {})
        elif nxt.get("fastcheck"):
            events.append(repo, "m2-skip", "fastcheck",
                          f"자율 검증 {NOT_OPEN_NOTE}",
                          "검증 단계가 아직 열리지 않았습니다", actor="m2")
        else:
            events.append(repo, "m2-skip", "fastcheck",
                          "자율 다음 단계 꺼짐",
                          "검증 단계가 꺼져 있어 준비된 작업실에서 멈춥니다",
                          actor="m2")
    for members in families_todo:
        run_ids = [str(item.get("run_id") or "") for item, _ in members]
        lane = str(members[0][0].get("lane") or "")
        base_id = members[0][1]
        primary = run_ids[0]
        member_refs = [{"run_id": r,
                        "dep": str(item.get("dep") or ""),
                        "target": str(item.get("target") or "")}
                       for item, _ in members
                       for r in [str(item.get("run_id") or "")]]
        lease = sched.acquire_worker_lease(f"m2-{repo}")
        if lease is None:
            _emit_skips()
            return _lease_busy(repo, busy_since, now, prepared, skipped)
        try:
            with sched.locked(repo):
                st = _m2_defaults(sched.load(repo))
                st["m2_lease_busy_since"] = None
                sched.save(st)
            pf = (prepare_family
                  if prepare_family is not None
                  else _batches.prepare_family_integration)
            pf_retry = any(r in retry_runs for r in run_ids)
            out = pf(repo, lane, base_id, member_refs,
                     origin="m2", retry=pf_retry)
        except _IntegrationError as exc:
            message = str(exc)
            if "already present in base" in message:
                for r in run_ids:
                    _remember_base_present(repo, r, base_id)
                    _skip(r, "present", "이미 기준 묶음에 포함")
                continue
            _record_block(repo, lane, primary, message)
            continue
        finally:
            try:
                sched.release_worker_lease(lease)
            except Exception:  # noqa: BLE001 - release never fails a tick
                pass
        out = out or {}
        if not out.get("ok"):
            err = str(out.get("error") or "가족 준비 실패")
            if "already present in base" in err:
                for r in run_ids:
                    _remember_base_present(repo, r, base_id)
                    _skip(r, "present", "이미 기준 묶음에 포함")
                continue
            if "not fresh" in err or "drifted" in err:
                for r in run_ids:
                    _skip(r, "unverified", err[:200])
                continue
            if "기준이 같은 의존성 선언을 이미 변경함" in err:
                # S15(b) for trains: the base moved the members'
                # lines — every stale member earns a refresh run,
                # and a queued (or live duplicate) refresh exempts
                # the family block from the counter, exactly like a
                # stale single.
                outcomes = [_maybe_queue_refresh(
                    repo, lane, r, out.get("rec"), base_id, tick)
                    for r in run_ids]
                _block_prepare(
                    repo, lane, primary,
                    str(out.get("primary_op") or ""), err,
                    rec=out.get("rec"),
                    correcting=_had_false_done(repo, primary),
                    count=not any(o in ("queued", "duplicate")
                                  for o in outcomes))
                continue
            # One lane-block for the whole family (primary counts);
            # member records are already written without counting.
            _block_prepare(repo, lane, primary,
                           str(out.get("primary_op") or ""), err,
                           rec=out.get("rec"),
                           correcting=_had_false_done(repo, primary))
            continue
        rec = out.get("rec") or {}
        result = rec.get("result") or {}
        head = str(result.get("head") or "")
        branch = str(result.get("branch") or "")
        if str(rec.get("status") or "") != "done" or not head:
            _block_prepare(repo, lane, primary,
                           str(rec.get("op_id") or ""),
                           "예상치 못한 가족 준비 상태", rec=rec)
            continue
        if not branch.startswith("codex/"):
            _record_block(repo, lane, primary,
                          "unexpected branch outside codex/")
            continue
        _clear_blocks(repo, lane)
        note = str(out.get("note") or
                   f"함께 준비: {', '.join(run_ids)}")
        detail = (f"기준 {base_id} · 브랜치 {branch} · "
                  f"head {head[:12]} · {note}")
        if pf_retry:
            # The train reunited after a stranded single: prior
            # blocked member records are marked superseded_by the
            # family op (field only), and one event says who did
            # it — the same observability as the explicit retry.
            marked = _batches.mark_train_superseded(
                set(run_ids), str(out.get("primary_op") or ""),
                base_id)
            if marked:
                detail += f" · 대체된 막힘 기록 {len(marked)}건"
            events.append(
                repo, "m2-family-retry",
                str(out.get("primary_op") or primary),
                f"가족으로 다시 시도: {note} — m2",
                f"구성원 {len(run_ids)}건 · 기준 {base_id} · "
                f"대체된 막힘 기록 {len(marked)}건", actor="m2")
        for item, _ in members:
            r = str(item.get("run_id") or "")
            _note_once(repo, f"done-{r}", "m2-done",
                       prepare_done_title(
                           item.get("dep") or "",
                           _item_from_version(item),
                           item.get("target") or ""),
                       detail, run=r)
            prepared.append({"run": r, "base": base_id,
                             "branch": branch, "head": head})
        _note_once(repo, f"family-done-{primary}", "m2-done",
                   note, detail, run=primary)
        nxt = get_policy(repo)
        if nxt.get("fastcheck") and STAGE_OPEN.get("fastcheck"):
            fc_out = _run_fastcheck_stage(repo, primary, lane, rec,
                                          base_id)
            if fc_out.get("ran"):
                _run_batch_stage(
                    repo, primary, lane, rec, base_id,
                    fc_out.get("summary") or {},
                    extra_primaries=[
                        {"run_id": r,
                         "dep": str(item.get("dep") or ""),
                         "target": str(item.get("target") or "")}
                        for item, _ in members[1:]
                        for r in [str(item.get("run_id") or "")]])
        elif nxt.get("fastcheck"):
            events.append(repo, "m2-skip", "fastcheck",
                          f"자율 검증 {NOT_OPEN_NOTE}",
                          "검증 단계가 아직 열리지 않았습니다", actor="m2")
        else:
            events.append(repo, "m2-skip", "fastcheck",
                          "자율 다음 단계 꺼짐",
                          "검증 단계가 꺼져 있어 준비된 작업실에서 멈춥니다",
                          actor="m2")
    _emit_skips()
    # Rollup last: at most one per repo per tick, next lane then
    # main. Only READY leaves qualify (the plan says so); a done
    # rollup for the same set means hands off.
    rolled = []
    nxt = get_policy(repo)
    if nxt.get("rollup") and STAGE_OPEN.get("rollup"):
        for lane in ("next", "main"):
            rout = _run_rollup_stage(repo, lane)
            if (rout or {}).get("ran"):
                rolled.append({"lane": lane,
                               "rollup": (rout or {}).get("rollup")})
                break
    return {"ran": bool(prepared or batched or rolled),
            "prepared": prepared, "batched": batched,
            "rolled": rolled, "skipped": skipped,
            "reason": "" if (prepared or batched or rolled)
            else "준비할 후보 없음"}


def _lease_busy(repo, busy_since, now, prepared, skipped):
    """Lease busy is a skip, not failure — until 45 minutes pass."""
    if busy_since:
        try:
            waited = now - float(busy_since)
        except (TypeError, ValueError):
            waited = 0.0
    else:
        waited = 0.0
    if waited > _BUSY_LIMIT_S:
        _note_once(repo, "busy-blocked", "m2-blocked", "자율 막힘",
                   "45분 연속 작업 중 — 다음 틱에 계속 확인합니다")
        return {"ran": False, "reason": "45분 연속 작업 중",
                "prepared": prepared, "skipped": skipped}
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        if not st.get("m2_lease_busy_since"):
            st["m2_lease_busy_since"] = now
            sched.save(st)
    return {"ran": False, "reason": "작업 중 — 다음 틱에 계속",
            "prepared": prepared, "skipped": skipped}


def _record_block(repo, lane, run_id, message):
    _count_block(repo, lane)
    refuse(repo, f"{run_id}: {message[:300]}", stage="준비")


# M2.6b2: the grouped note a train waits with after its family
# attempt blocked — the explicit family-retry (button/API) owns the
# next attempt, never the tick.
_FAMILY_BLOCKED_NOTE = "가족 준비 막힘 — 가족으로 다시 시도 필요"


def _train_family_record(members):
    """Newest non-done family-role record among train members.

    Returns (record_or_None, readable). A member with no record is
    simply skipped; any unreadable member record fails closed
    (readable False) so the caller never solos into — or reunites
    — a train it cannot fully see. Read-only.
    """
    from . import batches as _batches
    found = None
    for item in members or []:
        rid = str((item or {}).get("run_id") or "")
        if not rid:
            return None, False
        try:
            rec = _batches.latest_integration_op(rid)
        except Exception:  # noqa: BLE001 - unreadable fails closed
            return None, False
        if not isinstance(rec, dict):
            continue
        if str((rec.get("family") or {}).get("role") or "") in \
                ("primary", "member") \
                and str(rec.get("status") or "") != "done":
            found = rec
    return found, True


def _defer_family_need(queue, lane, run_id, dep, rec) -> str:
    """M2.6 item 3: a lone member's cherry-pick conflict where train
    siblings wait in the queue is "가족 준비 필요", not a block.

    Flags the blocked op record (so the later D2 reconcile never
    counts it) and returns the grouped skip reason; the caller emits
    the skip itself (this helper is module-level — the tick's _skip
    closure is not in scope here). A skip never touches the
    consecutive-block counter. Returns "" when nothing defers."""
    from . import batches as _batches
    sibs = _family_siblings(queue, lane, run_id, dep)
    if not sibs:
        return ""
    if isinstance(rec, dict) and str(rec.get("op_id") or ""):
        try:
            result = rec.get("result")
            if isinstance(result, dict):
                result["family_needed"] = True
            _batches.write_json(
                _batches.op_file(str(rec.get("op_id") or "")), rec)
        except (OSError, ValueError, SystemExit):
            pass
    return (f"가족 준비 필요: {', '.join(sibs[:4])}"
            + (f" 외 {len(sibs) - 4}건" if len(sibs) > 4 else ""))


def _stamp_fc_block(repo, lane, rec) -> None:
    """Stamp this fastcheck red's counted contribution (L2).

    Best-effort: a lost op record never fails the tick — the count
    itself already landed via _record_block."""
    from . import batches as _batches
    op_id = str((rec or {}).get("op_id") or "")
    if not op_id:
        return
    try:
        with sched.locked(repo):
            st = _m2_defaults(sched.load(repo))
            epoch = int((st.get("m2_block_epoch") or {}).get(lane)
                        or 0)
    except Exception:  # noqa: BLE001 - lost state never fails a tick
        epoch = 0
    try:
        _batches.stamp_counted_block(op_id, lane, epoch)
    except Exception:  # noqa: BLE001 - lost op never fails a tick
        pass


def release_counted_block(repo: str, op_id: str) -> bool:
    """Clear one op's stamped contribution after a passing re-run.

    Returns True when a stamp existed. Same-epoch: the contribution
    is still in the current consecutive run, so decrement it (floor
    0). Older epoch: a reset consumed it already — but if that reset
    opened the STILL-ACTIVE cooldown and nothing has counted since,
    this op was one of the opening three, so lift the cooldown with
    an honest event. Anything else stays untouched.
    """
    from . import batches as _batches
    try:
        stamp = _batches.clear_counted_block(op_id)
    except Exception:  # noqa: BLE001 - unreadable never acts
        return False
    if not isinstance(stamp, dict):
        return False
    lane = str(stamp.get("lane") or "")
    if not lane:
        return True
    try:
        epoch = int(stamp.get("epoch") or 0)
    except (TypeError, ValueError):
        epoch = 0
    lifted = False
    decremented = False
    with sched.locked(repo):
        try:
            st = _m2_defaults(sched.load(repo))
        except SystemExit:
            return True
        cur_epoch = int((st.get("m2_block_epoch") or {}).get(lane)
                        or 0)
        blocks = st.get("m2_consec_blocks") or {}
        if epoch == cur_epoch:
            blocks[lane] = max(0, int(blocks.get(lane) or 0) - 1)
            st["m2_consec_blocks"] = blocks
            sched.save(st)
            decremented = True
        else:
            cool = st.get(_COOLDOWN_KEY) or {}
            if (cool.get(lane) and epoch == cur_epoch - 1
                    and int(blocks.get(lane) or 0) == 0):
                cool.pop(lane, None)
                st[_COOLDOWN_KEY] = cool
                sched.save(st)
                lifted = True
    if lifted:
        events.append(repo, "m2-cooldown", lane,
                      f"{lane} 레인 대기 해제",
                      f"{op_id}: 재실행 통과로 막힘 1건 취소 — "
                      "대기를 열었던 3건 중 1건이 빠짐",
                      actor="m2")
    elif decremented:
        events.append(repo, "m2-blocked", lane,
                      f"{op_id}: 재실행 통과로 막힘 1건 취소",
                      "연속 횟수에서 1건 차감 — 기록은 유지",
                      actor="m2")
    else:
        events.append(repo, "m2-blocked", lane,
                      f"{op_id}: 재실행 통과(횟수 유지)",
                      "기여분이 이미 소진된 대기열이라 차감할 횟수 없음 — "
                      "조용히 지우지 않음",
                      actor="m2")
    return True


def _stale_skip_detail(rec: dict, outcome=None) -> str:
    """FIX-a4: the stale skip note — the record's stale sentence
    plus what the refresh stage did with it."""
    base = _blocked_message(rec)
    how = {"queued": "새 기준에서 다시 실행 요청",
           "duplicate": "이미 재실행 요청됨",
           "off": "재실행 단계가 꺼져 있어 대기",
           "unopened": "재실행 단계가 아직 열리지 않음",
           "capped": "재실행 일일 한도로 대기"}.get(outcome or "")
    if not how:
        how = "재실행 단계에서 다룸"
    return f"{base} · {how}"


def prepare_done_title(dep, from_v, target, lang: str = "ko",
                       group=None) -> str:
    """M2.8a2: honest major-version titles (read-only, never blocks).

    Shows from -> to from the record's own data (the queue item's
    installed lock version, else its declared range). A semver
    major — or a 0.x minor, which is breaking until proven
    otherwise — earns the flag ("(메이저)"/"(major)", via the same
    versions.update_kind the scanner uses). Unknown from keeps
    today's title; never guessed.

    M2.10b: a group re-derive names the companions riding with the
    primary ("외 N개 같이 올림" / "plus N others").
    """
    dep = str(dep or "")
    target = str(target or "")
    today = f"자율 준비 완료 {dep} {target}".rstrip()
    frm = str(from_v or "")
    if not frm:
        return today
    title = f"자율 준비 완료 {dep} {frm} -> {target}".rstrip()
    try:
        from . import versions as _versions
        # A declared range reads at its floor (^/~ stripped for
        # the kind only — the display keeps the raw record data).
        kind = _versions.update_kind(frm.lstrip("^~=v").strip(),
                                      target)
    except Exception:  # noqa: BLE001 - no flag, never no title
        kind = "unknown"
    if kind in ("major", "rn-minor"):
        title += " (메이저)" if lang == "ko" else " (major)"
    others = [g for g in (group or [])
              if not str(g).startswith(dep + " ")] if dep else []
    if others:
        n = len(others)
        title += (f" 외 {n}개 같이 올림" if lang == "ko"
                  else f" plus {n} others")
    return title


def _item_from_version(item: dict):
    """The pre-bump version from the queue item's own data: the
    installed lock version first, else the declared range."""
    if not isinstance(item, dict):
        return ""
    return str(item.get("installed") or item.get("declared") or "")


def _blocked_message(rec: dict) -> str:
    """First error line plus conflicted paths of a blocked record."""
    err = str((rec or {}).get("error") or "").strip().splitlines()
    msg = (err[0] if err else "준비 막힘")[:300]
    paths = ((rec or {}).get("result") or {}).get("conflicted_paths")
    if isinstance(paths, list) and paths:
        msg += " (충돌: " + ", ".join(str(p) for p in paths[:5]) + ")"
    return msg


def _had_false_done(repo, run_id) -> bool:
    """Whether a done note already exists for this run (D2: the
    earlier false completion that a reconcile must correct)."""
    try:
        notes = (sched.load(repo).get("m2_notes") or {})
    except SystemExit:
        return False
    return f"done-{run_id}" in notes


def _cleanup_blocked_branch(rec: dict) -> str:
    """D3: remove the app-created leftover branch of a blocked m2
    prepare under exactly the S4 rule. The git work itself lives in
    batches, so this driver never touches a child process. E2: the
    first outcome is recorded once in the op record and reused, so
    the detail never drifts across ticks."""
    from . import batches as _batches
    result = (rec or {}).get("result") or {}
    stored = str(result.get("branch_cleanup") or "")
    if stored:
        return stored
    branch = str(result.get("branch") or "")
    if not branch.startswith("codex/"):
        return ""
    base = (rec or {}).get("base") or {}
    note = _batches.remove_leftover_branch_at_base(
        str(base.get("checkout") or ""), branch,
        str(base.get("head") or ""),
        str(result.get("path") or ""))
    op_id = str((rec or {}).get("op_id") or "")
    if op_id and note:
        stored_now = _batches.record_branch_cleanup(op_id, note)
        if stored_now:
            return stored_now
    return note


def _reconcile_done(repo, op_id) -> bool:
    """E1: whether this op's block was already recorded persistently
    (st["m2_reconciled"]). The note signature drifts when the
    cleanup note changes, so it can never be the guard."""
    if not op_id:
        return False
    try:
        marked = sched.load(repo).get("m2_reconciled") or {}
    except SystemExit:
        return False
    return isinstance(marked, dict) and str(op_id) in marked


def _mark_reconciled(repo, op_id):
    """Record the block persistently (bounded). Best-effort: a lost
    marker only risks one duplicate line, never a lost block."""
    if not op_id:
        return
    try:
        with sched.locked(repo):
            st = sched.load(repo)
            marked = dict(st.get("m2_reconciled") or {})
            marked[str(op_id)] = _now_iso()
            st["m2_reconciled"] = dict(list(marked.items())[-200:])
            sched.save(st)
    except Exception:  # noqa: BLE001 - marker loss is tolerated
        pass


def _count_once(repo, op_id) -> bool:
    """E1: a block counts at most once per op id across all ticks.
    Run-keyed blocks (no op id) keep the old behavior."""
    if not op_id:
        return True
    try:
        with sched.locked(repo):
            st = sched.load(repo)
            seen = list(st.get("m2_counted_ops") or [])
            if str(op_id) in seen:
                return False
            seen.append(str(op_id))
            st["m2_counted_ops"] = seen[-200:]
            sched.save(st)
    except Exception:  # noqa: BLE001 - marker loss fails open here
        return True
    return True


def _block_prepare(repo, lane, run_id, op_id, message, rec=None,
                   correcting=False, count=True):
    """D1/D2: a returned blocked record is a block, never a done.

    Exactly ONE m2-blocked timeline event per op (the blocked-<op_id>
    key) — S15(c): refuse() only appends an event too, so the old
    _record_block call doubled the line. The single event carries
    the op id, the D3 cleanup note and the correction prefix, and
    사람이 할 일 reads m2-blocked events, so nothing is lost.
    `count=False` records the block without feeding the cooldown
    counter (S15(b): a stale block that queued a refresh). History
    is never rewritten."""
    if rec is not None:
        cleanup = _cleanup_blocked_branch(rec)
        if cleanup:
            message = f"{message} · {cleanup}"
    detail = f"{run_id}: {message[:300]}"
    if correcting:
        detail = "정정: " + detail
    if _note_once(repo, f"blocked-{op_id or run_id}", "m2-blocked",
                  "자율 준비 막힘", detail, run=run_id):
        _mark_reconciled(repo, op_id)
        if count and _count_once(repo, op_id):
            _count_block(repo, lane)


def _lockfix_exists(op_id, home=None) -> bool:
    """Whether a derived op file already exists for this op (a prior
    lockfix or M2.9a re-derive attempt — manual or automatic — so
    the driver never re-attempts). Read-only."""
    from . import paths as _paths
    try:
        base = _paths.home() if home is None else Path(home)
        ops = base / "integration_ops"
        return ((ops / (str(op_id or "") + "-lockfix.json")).is_file()
                or (ops / (str(op_id or "") + "-rederive.json"))
                .is_file())
    except OSError:
        return False


def _attempt_lockfix(op_id) -> dict:
    """S15(a) worker: one reresolve_on_base call under the caller's
    (driver-held) lease. The batches function takes no lease itself,
    and op_guarded takes the op locks non-blocking in sorted
    {source, derived} order, so the lease -> op-lock order holds.
    Returns {"ok": True, "rec"} on a derived done,
    {"ok": False, "busy": True} on op-lock contention,
    {"ok": False, "error", "rec"} otherwise. Records nothing — the
    caller owns timeline/cooldown. Only raises for unexpected
    (non-IntegrationError) failures."""
    from . import batches as _batches
    from .batches import IntegrationError as _IntegrationError
    try:
        # S17: driver-initiated derived ops carry origin "m2" so the
        # UI can tell them from owner-clicked re-resolves.
        derived = _batches.reresolve_on_base(op_id, origin="m2")
    except _IntegrationError as exc:
        message = str(exc)
        if (getattr(exc, "http_status", None) == 409
                and "실행 중입니다" in message):
            return {"ok": False, "busy": True}
        return {"ok": False, "busy": False, "rec": None,
                "error": message[:300]}
    derived = derived or {}
    dhead = str((derived.get("result") or {}).get("head") or "")
    if str(derived.get("status") or "") == "done" and dhead:
        return {"ok": True, "rec": derived}
    return {"ok": False, "busy": False, "rec": derived,
            "error": _blocked_message(derived)}


_REFRESH_PER_DAY = 4


def _remember_base_present(repo, run_id, base_id):
    """E4: remember that this run's work already rides this base, so
    later ticks skip it without spending the prepare slot. Bounded;
    entries for departed queue items are pruned here."""
    try:
        with sched.locked(repo):
            st = sched.load(repo)
            live = {str(i.get("run_id") or "")
                    for i in (st.get("queue") or [])
                    if isinstance(i, dict)}
            mem = {r: b for r, b in
                   ((st.get("m2_base_present") or {}).items())
                   if r in live}
            mem[str(run_id)] = str(base_id)
            st["m2_base_present"] = dict(list(mem.items())[-200:])
            sched.save(st)
    except Exception:  # noqa: BLE001 - memory loss only costs a cycle
        pass


def _base_present(repo, run_id, base_id) -> bool:
    """E4: whether this run was recorded as already integrated on
    this exact base (a new base re-opens the question honestly)."""
    try:
        mem = sched.load(repo).get("m2_base_present") or {}
    except SystemExit:
        return False
    return isinstance(mem, dict) \
        and mem.get(str(run_id)) == str(base_id)


def _refresh_pending_for(repo, run_id) -> bool:
    """Whether a live m2-refresh queue item already supersedes this
    run (progress pending — stale re-blocks stay cooldown-exempt)."""
    try:
        queue = sched.load(repo).get("queue") or []
    except SystemExit:
        return False
    return any(isinstance(i, dict)
               and i.get("origin") == "m2-refresh"
               and str(i.get("supersedes") or "") == str(run_id)
               and str(i.get("status") or "") in sched.LIVE_STATUSES
               for i in queue)


def _maybe_queue_refresh(repo, lane, run_id, rec, base_id, tick) -> str:
    """S15(b): queue one refresh run for a stale same-line block.

    The new item re-does dep+target on the current lane base through
    the normal run queue (status queued, no run binding, no verified
    flag — it earns review like any scan finding; the driver never
    self-accepts it). At most 1 per tick (tick["refresh_queued"])
    and 4 per repo per UTC day (m2_refresh_log); same
    dep+target+base never duplicates a live item. Returns queued |
    capped | duplicate | off | unopened | error. A queued refresh
    (execute off included — said plainly, never silent) exempts the
    stale block from the cooldown counter; every other outcome
    counts. Never raises into the tick for expected failures.
    """
    try:
        policy = get_policy(repo)
    except SystemExit:
        return "error"
    if not (policy or {}).get("refresh"):
        return "off"
    if not STAGE_OPEN.get("refresh"):
        return "unopened"
    if (tick or {}).get("refresh_queued"):
        return "capped"
    try:
        with sched.locked(repo):
            st = sched.load(repo)
            log = [e for e in (st.get("m2_refresh_log") or [])
                   if isinstance(e, dict)]
            today = _now_iso()[:10]
            if sum(1 for e in log
                   if str(e.get("day") or "") == today) \
                    >= _REFRESH_PER_DAY:
                return "capped"
            queue = st.get("queue") or []
            old = next((i for i in queue if isinstance(i, dict)
                        and str(i.get("run_id") or "") == run_id),
                       None)
            dep = str(rec.get("dep") or (old or {}).get("dep") or "")
            target = str(rec.get("target") or (old or {}).get("target")
                         or "")
            if not (dep and target):
                return "error"
            channel = str((old or {}).get("channel") or "stable")
            old_key = str((old or {}).get("key") or "")
            if not old_key:
                old_key = f"{repo}+{dep}+{target}+{channel}"
            new_key = f"{old_key}+refresh-{base_id}"
            if any(isinstance(i, dict)
                   and str(i.get("key") or "") == new_key
                   and str(i.get("status") or "") in sched.LIVE_STATUSES
                   for i in queue):
                return "duplicate"
            item = dict(old) if isinstance(old, dict) else {}
            item.update({"kind": "dep-update", "repo": repo,
                         "dep": dep, "target": target, "lane": lane,
                         "channel": channel, "status": "queued",
                         "attempts": 0, "next_retry_at": None,
                         "origin": "m2-refresh", "supersedes": run_id,
                         "refresh_base": base_id, "key": new_key,
                         "note": "새 기준에서 다시 실행 요청",
                         "created_at": _now_iso()})
            item.pop("run_id", None)
            item.pop("run_issue", None)
            item.pop("verified", None)
            st["queue"].append(item)
            log.append({"day": today, "at": _now_iso(), "run": run_id,
                        "item": new_key})
            st["m2_refresh_log"] = log[-40:]
            sched.save(st)
            execute = bool(st.get("execute_enabled"))
    except Exception:  # noqa: BLE001 - a lost queue never fails a tick
        return "error"
    tick["refresh_queued"] = True
    detail = (f"{dep} {target} — {run_id} 대신 새 기준({base_id})에서 "
              f"다시 실행 요청")
    if not execute:
        detail += (" · 실행이 꺼져 있어 대기만 합니다 — 사람이 실행을 "
                   "켜야 진행됩니다")
    _note_once(repo, f"refresh-{new_key}", "m2-refresh",
               "새 기준에서 다시 실행 요청", detail, run=run_id)
    return "queued"


def batch_id_for_op(op_id: str) -> str:
    """A: the auto batch id derives from the prepare op id (never
    dep+date — two same-day runs must give two batches). Lowercase,
    batch-id charset, max 64 chars.

    M2.17g: ids that fit stay byte-identical. Longer ones keep a
    53-char prefix plus a stable sha256 suffix of the full op id,
    so two ops sharing a 64-char prefix (a refreshed joint and
    its next refresh) never collide."""
    import hashlib
    import re
    slug = re.sub(r"[^a-z0-9\-_]",
                  "-", str(op_id or "").lower()).strip("-")
    bid = "auto-" + slug
    if len(bid) <= 64:
        if not bid or len(bid) < 6:
            raise ValueError(
                f"cannot derive batch id from op {op_id!r}")
        return bid
    digest = hashlib.sha256(
        str(op_id or "").encode("utf-8")).hexdigest()[:10]
    bid = ("auto-" + slug)[:53].rstrip("-") + "-" + digest
    if not bid or len(bid) < 6:
        raise ValueError(
            f"cannot derive batch id from op {op_id!r}")
    return bid


def build_batch_manifest(repo: str, run_id: str, lane: str, rec: dict,
                         base_id: str, fc_summary: dict,
                         profile: dict,
                         extra_primaries: list | None = None) -> dict:
    """G3-shaped auto-batch manifest (2b, pure builder for tests).

    Every worker step maps onto its worker:<gate> companion (grown
    into the manifest policy with root:false/runtime:false) — never
    directly onto a catalog gate, so a worker receipt can never
    satisfy a root/runtime gate. Every base required gate stays
    pending for Root/human attestation. The primary review verdict
    is pending — never upgraded without an independent review, which
    is why an auto batch is never READY until its Root gates land.
    Raises ValueError with a Korean reason when the inputs cannot
    become a manifest."""
    from . import batches as _batches
    from . import store as _store
    try:
        base = _batches.load_batch(base_id)
    except Exception as exc:  # noqa: BLE001 - unreadable never acts
        raise ValueError(f"기준 묶음을 읽을 수 없음: {exc}") from exc
    base_policy = base.get("policy") or {}
    catalog = base_policy.get("gates") or {}
    required = list(base_policy.get("required_gates") or [])
    steps = (profile or {}).get("steps") or []
    companions = {}
    for step in steps:
        gate_key = str((step or {}).get("gate") or "")
        if not gate_key:
            raise ValueError("검사 단계 게이트가 비어 있음")
        if gate_key.startswith("worker:"):
            raise ValueError(f"검사 단계 게이트 {gate_key!r}는 직접 지정할 "
                             "수 없음 — 실제 게이트 이름을 쓰세요")
        # M2.4a (+L2 live fix): every worker step maps onto its
        # worker:<gate> companion — never directly onto a catalog
        # gate, so a worker receipt can never satisfy a
        # root/runtime gate (J1, first direction). Base batches use
        # different gate vocabularies (install vs immutable-install
        # vs none), so membership in the base catalog is NOT
        # required: the companion is independent. The companions
        # grow into the manifest policy below with
        # root:false/runtime:false.
        spec = catalog.get(gate_key) or {}
        companions[gate_key] = {
            "label": ("자동 검사 " + str(spec.get("label")
                                        or gate_key))[:200],
            "root": False, "runtime": False}
    result = (rec or {}).get("result") or {}
    op_id = str((rec or {}).get("op_id") or "")
    head = str(result.get("head") or "")
    fp = str(result.get("fingerprint") or "")
    worktree = str(result.get("path") or "")
    dep = str((rec or {}).get("dep") or "")
    target = str((rec or {}).get("target") or "")
    if not (op_id and head and fp and worktree and dep and target):
        raise ValueError("준비 기록이 불완전하여 묶음을 만들 수 없음")
    try:
        bid = batch_id_for_op(op_id)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    by_gate: dict = {}
    for pos, step in enumerate(steps):
        gate_key = "worker:" + str(step.get("gate"))
        entry = by_gate.setdefault(gate_key, [])
        step_receipts = (fc_summary.get("steps") or [])
        srec = step_receipts[pos] if pos < len(step_receipts) else {}
        if srec.get("log"):
            entry.append({"label": f"검사 기록 {pos + 1}", "kind": "file",
                          "origin": srec["log"]})
        entry.append({"label": "검사 요약", "kind": "file",
                      "origin": str(_fc_receipt_path(op_id))})
    gates = []
    for gate_key, ev in by_gate.items():
        gates.append({"key": gate_key, "status": "pass",
                      "root_attested": False,
                      "inputs": {"head": head, "fingerprint": fp},
                      "note": "앱 자동 검사 통과 (Root 확인 아님)",
                      "evidence": ev})
    for name in required:
        gates.append({"key": name, "status": "pending",
                      "root_attested": False,
                      "note": "Root 확인 대기", "evidence": []})
    base_origin = base.get("origin") or {}
    grown_catalog = dict(catalog)
    for gate_key, spec in companions.items():
        grown_catalog["worker:" + gate_key] = spec
    # M2.5c: the duplicate-tree census over the two root locks
    # (base vs result) rides into the manifest — display only,
    # never a block.
    lock_dupes = _lane_lock_dupes(base, lane, worktree)
    # M2.6: release-train families register every member as a
    # primary of the one batch — each resolved from its own live
    # run state exactly like the primary below.
    def _primary_entry(r: str) -> dict:
        try:
            st = _store.load(r)
        except Exception as exc:  # noqa: BLE001 - unreadable refuses
            raise ValueError(
                f"실행 기록을 읽을 수 없음: {exc}") from exc
        branch = ""
        try:
            from . import sched as _sched
            for item in (_sched.load(repo).get("queue") or []):
                if (isinstance(item, dict)
                        and str(item.get("run_id") or "") == r):
                    branch = str(item.get("branch") or "")
                    break
        except Exception:  # noqa: BLE001 - missing branch fails below
            pass
        return {"dep": dep if r == run_id
                else str(st.get("dep") or ""),
                "target": target if r == run_id
                else str(st.get("target") or ""),
                "lane": lane, "run_id": r,
                "source": {"worktree": str(_store.run_dir(r) / "work"),
                           "branch": branch,
                           "head": str(st.get("workCommit") or head),
                           "fingerprint": str(
                               st.get("workTreeFingerprint") or fp)},
                "review": {"verdict": "pending", "at": _now_iso(),
                           "findings": ["자동 등록 — 독립 검토 대기"]}}
    primaries = [_primary_entry(run_id)]
    for extra in extra_primaries or []:
        if isinstance(extra, dict) and str(extra.get("run_id") or ""):
            primaries.append(
                _primary_entry(str(extra.get("run_id") or "")))
    manifest = {
        "id": bid, "repo": repo,
        "target_branch": str(base.get("target_branch") or lane),
        "purpose": (f"자동 등록(앱 자동 검사, Root 확인 대기 "
                    f"{len(required)}건) · "
                    "네이티브/기기 검증 아님")[:200],
        "origin": {"checkout": base_origin.get("checkout"),
                   "base_branch": base_origin.get("base_branch"),
                   "base_head": base_origin.get("base_head")},
        "origin_m2": op_id,
        "primaries": primaries,
        "integrations": [{"lane": lane, "worktree": worktree,
                          "status": "testing", "gates": gates,
                          "lock_dupes": lock_dupes}],
        "inherited": [{"group": lane, "batch": base_id,
                       "count": len(base.get("primaries") or [])}],
        "policy": {"required_gates": required, "gates": grown_catalog},
    }
    if len(primaries) > 1:
        manifest["family"] = {
            "primary": run_id,
            "members": [str(p.get("run_id") or "") for p in primaries],
            "note": (f"함께 준비: "
                     f"{', '.join(str(p.get('dep') or '') for p in primaries)}"
                     f" {target}")}
    return manifest


def build_issue_batch_manifest(repo: str, run_id: str, lane: str,
                               rec: dict, issue_ctx: dict, base_id: str,
                               fc_summary: dict,
                               profile: dict) -> dict:
    """G3-shaped issue-batch manifest (pure builder for tests).

    Same companion/policy/review-pending contract as
    build_batch_manifest, but for a typed issue prepare op: the single
    primary carries dep/target "" plus a validated issue block
    {issue, repo, lane, url}, and the source binds the ORIGINAL
    candidate (canonical run work dir + queue-owned branch + recorded
    original head/fingerprint) — never the current live bytes and
    never rebound to the changed integration result. The integrated
    bytes bind separately through the lane integration + gate inputs;
    required Root/runtime/source-review gates on them stay pending.
    Worker:<gate> companions stay root:false/runtime:false; every base
    required gate stays pending. Raises ValueError with a Korean reason
    on invalid inputs."""
    from . import batches as _batches
    try:
        base = _batches.load_batch(base_id)
    except Exception as exc:  # noqa: BLE001 - unreadable never acts
        raise ValueError(f"기준 묶음을 읽을 수 없음: {exc}") from exc
    base_policy = base.get("policy") or {}
    catalog = base_policy.get("gates") or {}
    required = list(base_policy.get("required_gates") or [])
    # The profile's check steps live in its nested fastcheck section —
    # the same mapping _run_fastcheck_once uses — never a flat
    # top-level "steps" key.
    _fc_profile = (profile or {}).get("fastcheck")
    if not isinstance(_fc_profile, dict):
        raise ValueError("등록 프로필에 fastcheck 설정이 없음")
    steps = _fc_profile.get("steps") or []
    companions = {}
    for step in steps:
        gate_key = str((step or {}).get("gate") or "")
        if not gate_key:
            raise ValueError("검사 단계 게이트가 비어 있음")
        if gate_key.startswith("worker:"):
            raise ValueError(f"검사 단계 게이트 {gate_key!r}는 직접 지정할 "
                             "수 없음 — 실제 게이트 이름을 쓰세요")
        spec = catalog.get(gate_key) or {}
        companions[gate_key] = {
            "label": ("자동 검사 " + str(spec.get("label")
                                        or gate_key))[:200],
            "root": False, "runtime": False}
    from . import store as _store
    result = (rec or {}).get("result") or {}
    op_id = str((rec or {}).get("op_id") or "")
    head = str(result.get("head") or "")
    fp = str(result.get("fingerprint") or "")
    worktree = str(result.get("path") or "")
    integ_branch = str(result.get("branch") or "")
    if not (op_id and head and fp and worktree and integ_branch):
        raise ValueError("준비 기록이 불완전하여 묶음을 만들 수 없음")
    if str((rec or {}).get("op") or "") != "prepare" \
            or str((rec or {}).get("status") or "") != "done":
        raise ValueError("완료된 이슈 준비 작업이 아님")
    number = str((issue_ctx or {}).get("number") or "")
    if _batches._strict_issue_number(number) is None:
        raise ValueError("이슈 번호가 올바르지 않음")
    if str((issue_ctx or {}).get("lane") or "") != lane \
            or lane not in _batches._KNOWN_LANES:
        raise ValueError("이슈 레인이 올바르지 않음")
    url = str((issue_ctx or {}).get("url") or "")
    if not url:
        raise ValueError("이슈 원본 URL이 없음")
    if str((rec.get("issue") or {}).get("issue") or "") != number:
        raise ValueError("준비 기록의 이슈가 요청과 다름")
    if str((rec or {}).get("run_id") or "") != run_id \
            or str((rec or {}).get("repo") or "") != repo:
        raise ValueError("준비 기록의 실행/저장소 바인딩이 다름")
    # Original-candidate source (MINIMAL EXISTING CONTRACT): the
    # canonical run work dir + queue-owned original branch + recorded
    # original head/fingerprint from rec.candidate. No fallback to the
    # changed integration result, or vice versa.
    cand = (rec or {}).get("candidate") or {}
    try:
        canon_wt = str(_store.run_dir(run_id) / "work")
    except Exception as exc:  # noqa: BLE001 - unresolvable refuses
        raise ValueError(f"원본 후보 경로를 확인할 수 없음: {exc}") from exc
    orig_branch = str((issue_ctx or {}).get("branch") or "")
    if str(cand.get("worktree") or "") != canon_wt \
            or str(cand.get("branch") or "") != orig_branch:
        raise ValueError("준비 기록의 원본 후보가 등록된 원본과 다름")
    orig_head = str(cand.get("head") or "")
    orig_fp = str(cand.get("fingerprint") or "")
    if not orig_head or not orig_fp:
        raise ValueError("준비 기록에 원본 후보 식별자가 없음")
    try:
        bid = batch_id_for_op(op_id)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    by_gate: dict = {}
    for pos, step in enumerate(steps):
        gate_key = "worker:" + str(step.get("gate"))
        entry = by_gate.setdefault(gate_key, [])
        step_receipts = (fc_summary.get("steps") or [])
        srec = step_receipts[pos] if pos < len(step_receipts) else {}
        if srec.get("log"):
            entry.append({"label": f"검사 기록 {pos + 1}", "kind": "file",
                          "origin": srec["log"]})
        entry.append({"label": "검사 요약", "kind": "file",
                      "origin": str(_fc_receipt_path(op_id))})
    gates = []
    for gate_key, ev in by_gate.items():
        gates.append({"key": gate_key, "status": "pass",
                      "root_attested": False,
                      "inputs": {"head": head, "fingerprint": fp},
                      "note": "앱 자동 검사 통과 (Root 확인 아님)",
                      "evidence": ev})
    for name in required:
        gates.append({"key": name, "status": "pending",
                      "root_attested": False,
                      "note": "Root 확인 대기", "evidence": []})
    base_origin = base.get("origin") or {}
    grown_catalog = dict(catalog)
    for gate_key, spec in companions.items():
        grown_catalog["worker:" + gate_key] = spec
    lock_dupes = _lane_lock_dupes(base, lane, worktree)
    # The primary source is the ORIGINAL candidate (canonical run
    # work dir + queue-owned original branch + recorded original
    # head/fingerprint). The integrated bytes bind separately through
    # integrations[0] + gate inputs; review gates on them stay pending.
    primary = {"dep": "", "target": "", "lane": lane, "run_id": run_id,
               "issue": {"issue": number, "repo": repo, "lane": lane,
                         "url": url},
               "source": {"worktree": canon_wt, "branch": orig_branch,
                          "head": orig_head, "fingerprint": orig_fp},
               "review": {"verdict": "pending", "at": _now_iso(),
                          "findings": ["자동 등록 — 독립 검토 대기"]}}
    manifest = {
        "id": bid, "repo": repo,
        "target_branch": str(base.get("target_branch") or lane),
        "purpose": (f"이슈 #{number} 자동 등록(앱 자동 검사, Root 확인 "
                    f"대기 {len(required)}건) · "
                    "네이티브/기기 검증 아님")[:200],
        "origin": {"checkout": base_origin.get("checkout"),
                   "base_branch": base_origin.get("base_branch"),
                   "base_head": base_origin.get("base_head")},
        "origin_m2": op_id,
        "primaries": [primary],
        "integrations": [{"lane": lane, "worktree": worktree,
                          "status": "testing", "gates": gates,
                          "lock_dupes": lock_dupes}],
        "inherited": [{"group": lane, "batch": base_id,
                       "count": len(base.get("primaries") or [])}],
        "policy": {"required_gates": required, "gates": grown_catalog},
    }
    return manifest


def _fc_receipt_path(op_id: str) -> str:
    from . import fastcheck as _fc
    return str(_fc.op_fastcheck_path(op_id))


def _batched_candidate_map(repo: str) -> dict:
    """One read-only pass: candidate key -> newest batch id (M2.4d).

    Phase 1 builds this once per tick so already-batched resumes
    never consume the per-tick cap. Same newest-wins rule as
    batches.find_candidate_batch, which stays as the in-stage
    backstop."""
    from . import batches as _batches
    out = {}
    at = {}
    try:
        files = sorted(_batches.batches_dir().glob("*.json"))
    except OSError:
        return out
    for path in files:
        if path.name == "index.json":
            continue
        try:
            batch = _batches.load_batch(path.stem)
        except Exception:  # noqa: BLE001 - one bad file skips it
            continue
        if (batch.get("repo") or "") != repo:
            continue
        for primary in batch.get("primaries") or []:
            if not isinstance(primary, dict):
                continue
            key = _batches.candidate_key(
                repo, primary.get("dep"), primary.get("target"),
                primary.get("lane"))
            upd = str(batch.get("updated_at") or "")
            if upd >= at.get(key, ""):
                out[key] = str(batch.get("id") or path.stem)
                at[key] = upd
    return out


def _phase1_dup(repo: str, run_id: str, lane: str,
                dup_map: dict) -> str:
    """Batch id already carrying this run's candidate, or "".

    Reads the same run-state dep/target as the in-stage backstop,
    so Phase 1 and the stage always agree."""
    try:
        from . import store as _store
        state = _store.load(run_id)
    except SystemExit:
        state = {}
    except Exception:  # noqa: BLE001 - unreadable never acts
        state = {}
    if not isinstance(state, dict):
        state = {}
    from . import batches as _batches
    return dup_map.get(_batches.candidate_key(
        repo, state.get("dep"), state.get("target"), lane), "")


def _refreshed_joint_exempts(repo: str, rec: dict, dup_bid: str) -> bool:
    """M2.17c: a refreshed joint batches even when the OLD joint
    batch still holds one of its member keys — iff the
    _joint_supersede_map marks that old batch as replaced by THIS
    refreshed joint op. Same held-superseded pattern as the
    M2.14c joint exemption, applied to the stage dedupe instead
    of the import hold check. Narrow: the current record must be
    a done joint reresolve and the map value for the holding
    batch must equal this joint's op id. A non-superseded
    duplicate keeps today's skip. Read-only; never raises."""
    try:
        from . import batches as _batches
        rec = rec or {}
        if rec.get("kind") != "joint" \
                or rec.get("op") != "reresolve" \
                or str(rec.get("status") or "") != "done" \
                or str(rec.get("repo") or "") != str(repo or ""):
            return False
        op_id = str(rec.get("op_id") or "")
        if not op_id or not dup_bid:
            return False
        return _batches._joint_supersede_map().get(
            str(dup_bid)) == op_id
    except (Exception, SystemExit):
        return False


def _pipeline_prepare_rec(rec):
    """M24d-F2: the pipeline record for resume checks.

    A DONE prepare qualifies as today. A DONE reresolve derived
    from a prepare op for the SAME run also qualifies — the
    prepare(blocked) -> lockfix(done) -> fastcheck strand would
    otherwise strand forever under "이미 기록된 작업 있음". A
    manual reresolve with no prepare behind it, and anything
    blocked or interrupted, keeps today's plain skip. Read-only."""
    rec = rec or {}
    if rec.get("op") == "prepare":
        return rec
    if rec.get("op") != "reresolve":
        return None
    # M2.17 B: a DONE joint is the pipeline record of every run
    # it covers. The refreshed joint runs fresh-run-primary
    # (run_id=Y, derived_from=X's solo op), so the same-run rule
    # below would strand it — the manual fastcheck gate and the
    # driver's resume would both refuse, and Y's item would fall
    # through to a fresh standalone prepare every tick.
    # Membership in runs is the precise qualifier (a blocked
    # joint is never pipeline state); malformed joints keep
    # today's rule.
    if str(rec.get("kind") or "") == "joint" \
            and str(rec.get("status") or "") == "done":
        runs = rec.get("runs")
        if isinstance(runs, list) \
                and str(rec.get("run_id") or "") \
                in [str(r) for r in runs]:
            return rec
    src_id = str(rec.get("derived_from") or "")
    run_id = str(rec.get("run_id") or "")
    if not (src_id and run_id):
        return None
    try:
        from . import batches as _batches
        from .util import read_json
        src = read_json(_batches.op_file(src_id), default=None)
    except (Exception, SystemExit):  # noqa: BLE001 - unreadable
        # never acts (read_json raises SystemExit on corrupt JSON).
        return None
    if not isinstance(src, dict):
        return None
    if src.get("op") != "prepare":
        return None
    if str(src.get("run_id") or "") != run_id:
        return None
    return rec


def _batch_resume(policy: dict, run_id: str, lane: str, rec: dict):
    """Collect a batch-resume tuple (rec, base_id, summary) or None.

    Only a DONE prepare with a passing fastcheck receipt and no batch
    yet qualifies, and only while the batch toggle is on. Blocked,
    interrupted and already-batched work keeps the plain skip."""
    rec = _pipeline_prepare_rec(rec)
    if rec is None:
        return None
    if (rec or {}).get("status") != "done":
        return None
    # M2.6: non-primary family members never resume alone — the
    # primary's pipeline carries the whole train.
    if str(((rec or {}).get("family") or {}).get("role") or "") \
            == "member":
        return None
    if not (policy or {}).get("batch"):
        return None
    op_id = str(rec.get("op_id") or "")
    base_id = str(rec.get("batch_id") or "")
    if not (op_id and base_id):
        return None
    try:
        bid = batch_id_for_op(op_id)
    except ValueError:
        return None
    from . import batches as _batches
    try:
        if isinstance(_batches.load_batch(bid), dict):
            return None
    except Exception:  # noqa: BLE001 - missing means new
        pass
    from . import fastcheck as _fc
    from .util import read_json
    try:
        summary = read_json(_fc.op_fastcheck_path(op_id), default=None)
    except Exception:  # noqa: BLE001 - unreadable never acts
        return None
    if not isinstance(summary, dict) or not summary.get("ok"):
        return None
    return (rec, base_id, summary)


def _family_extras(repo: str, rec: dict):
    """M2.6b: fellow-member primaries for a resumed family primary.

    A family prepared in-tick batches with all members inline, but a
    family that finishes fastcheck on a LATER tick resumes through
    _batch_one, which otherwise batches the primary alone. Returns
    (extras, divert): extras is [{run_id, dep, target}, ...] for the
    fellow members (None when rec is not a done family primary, and
    the caller batches normally); divert is "" to proceed, "dup:<bid>"
    when any member already rides a batch (the whole resume diverts
    to the grouped dup note, never a partial batch), or "wait" when
    a member is unreadable (grouped skip, retried next tick).
    """
    from . import batches as _batches
    from . import store as _store
    fam = (rec or {}).get("family") or {}
    if not isinstance(fam, dict) or fam.get("role") != "primary":
        return None, ""
    if str((rec or {}).get("status") or "") != "done":
        return None, ""
    members = [m for m in (fam.get("members") or [])
               if isinstance(m, str) and m]
    if len(members) < 2 \
            or str((rec or {}).get("run_id") or "") != members[0]:
        return None, ""
    lane = str((rec or {}).get("lane") or "")
    extras = []
    for rid in members[1:]:
        try:
            state = _store.load(rid)
        except (Exception, SystemExit):  # noqa: BLE001 - unreadable
            # waits (SystemExit: store.load on a missing run)
            return None, "wait"
        if not state.get("dep") or not state.get("target"):
            return None, "wait"
        try:
            dup = _batches.find_candidate_batch(
                repo, state.get("dep"), state.get("target"), lane)
        except Exception:  # noqa: BLE001 - unreadable waits
            return None, "wait"
        if dup:
            return None, f"dup:{dup}"
        extras.append({"run_id": rid,
                       "dep": str(state.get("dep") or ""),
                       "target": str(state.get("target") or "")})
    return extras, ""


def _joint_extras(repo: str, rec: dict):
    """M2.14b: fellow primaries for a resumed joint record.

    A done joint (kind "joint") batches with all its runs inline:
    extras is [{run_id, dep, target}, ...] for the joint_with ops.
    Unlike family members, a joint partner may already ride a
    (held, unshippable) standalone batch — that never diverts,
    because the joint supersedes it; the held card names the
    joint op instead. Returns (extras, divert) with divert ""
    to proceed or "wait" when a partner is unreadable (grouped
    skip, retried next tick). Read-only.
    """
    from . import batches as _batches
    from . import store as _store
    if not isinstance(rec, dict) or rec.get("kind") != "joint":
        return None, ""
    if str(rec.get("status") or "") != "done":
        return None, ""
    ops = rec.get("joint_with")
    if not isinstance(ops, list) or not ops:
        return None, ""
    extras = []
    for op_id in ops:
        op_id = str(op_id or "")
        if not op_id:
            return None, "wait"
        try:
            opf = _batches.op_file(op_id)
            from .util import read_json
            y_rec = read_json(opf, default=None)
        except (Exception, SystemExit):  # noqa: BLE001 - unreadable
            return None, "wait"
        if not isinstance(y_rec, dict):
            return None, "wait"
        rid = str(y_rec.get("run_id") or "")
        try:
            state = _store.load(rid) if rid else None
        except (Exception, SystemExit):  # noqa: BLE001 - unreadable
            return None, "wait"
        if not isinstance(state, dict) or not state.get("dep") \
                or not state.get("target"):
            return None, "wait"
        extras.append({"run_id": rid,
                       "dep": str(state.get("dep") or ""),
                       "target": str(state.get("target") or "")})
    return extras, ""


def _fc_resume_eligible(policy, run_id, lane, rec):
    """Collect a fastcheck-resume tuple (rec, base_id) or None.

    S18a: a DONE prepare (any origin) with NO fastcheck receipt yet
    resumes into the fastcheck stage while the fastcheck toggle is
    on. An existing receipt — passing OR red — never re-runs here:
    a red result stays owner work, and a pass moves to batch via
    _batch_resume instead."""
    rec = _pipeline_prepare_rec(rec)
    if rec is None:
        return None
    if (rec or {}).get("status") != "done":
        return None
    # M2.6: non-primary family members never resume alone — the
    # primary's pipeline carries the whole train.
    if str(((rec or {}).get("family") or {}).get("role") or "") \
            == "member":
        return None
    if not (policy or {}).get("fastcheck"):
        return None
    if not STAGE_OPEN.get("fastcheck"):
        return None
    op_id = str(rec.get("op_id") or "")
    if not op_id:
        return None
    from . import fastcheck as _fc
    from .util import read_json
    try:
        summary = read_json(_fc.op_fastcheck_path(op_id), default=None)
    except Exception:  # noqa: BLE001 - unreadable never resumes
        return None
    if isinstance(summary, dict):
        return None
    return (rec, str(rec.get("batch_id") or ""))


def _run_fastcheck_stage(repo, run_id, lane, rec, base_id,
                         _runner=None):
    """Run the in-app fastcheck after an auto-prepare, inline.

    `_runner` injects fastcheck.run in tests. A red step blocks the
    lane with its log tail; a skip records a skip note. Fastcheck
    holds only the op lock (never the lease), so a concurrent UI
    retry on the same op gets OpBusy instead of interleaving (R5
    live). Unexpected errors block loudly, never silently."""
    from . import fastcheck as _fc
    run = _runner if _runner is not None else _fc.run
    result = (rec or {}).get("result") or {}
    op_id = str((rec or {}).get("op_id") or "")
    worktree = str(result.get("path") or "")
    head = str(result.get("head") or "")
    fp = str(result.get("fingerprint") or "")
    try:
        profile = repos.load(repo)
    except SystemExit:
        _note_once(repo, f"skip-fc-{run_id}", "m2-skip",
                   "자율 검증 건너뜀", f"{run_id}: 등록된 저장소 없음",
                   run=run_id)
        return {"ran": False, "reason": "unknown repo"}
    fc_profile = profile.get("fastcheck") or {}
    if not (fc_profile.get("steps") or []):
        _record_block(repo, lane, run_id, "fastcheck_steps 없음")
        return {"ran": False, "reason": "fastcheck_steps 없음"}
    # FIX-a1: prepare off stops verification too — same prefix rule.
    try:
        _pol = get_policy(repo)
    except SystemExit:
        _pol = {}
    _ok, _why = stage_prefix_ok(_pol, "fastcheck")
    if not _ok:
        return {"ran": False, "reason": "stage off: " + _why}
    _note_once(repo, f"fc-start-{run_id}", "m2-fastcheck",
               f"자율 검증 시작 {(rec or {}).get('dep') or ''} "
               f"{(rec or {}).get('target') or ''}".rstrip(),
               f"기준 {base_id} · 작업실 {worktree}", run=run_id)
    try:
        summary = run(op_id, worktree, fc_profile, head, fp)
    except _fc.FastcheckSkip as exc:
        _note_once(repo, f"skip-fc-{run_id}", "m2-skip",
                   "자율 검증 건너뜀",
                   f"{run_id}: {exc}", run=run_id)
        return {"ran": False, "reason": str(exc)[:200]}
    except _fc.FastcheckBlocked as exc:
        _record_block(repo, lane, run_id, str(exc)[:300])
        _stamp_fc_block(repo, lane, rec)
        return {"ran": False, "reason": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 - loud, never silent
        _record_block(repo, lane, run_id,
                      f"검사 실행 오류 "
                      f"{type(exc).__name__}: {exc}"[:300])
        _stamp_fc_block(repo, lane, rec)
        return {"ran": False, "reason": f"{type(exc).__name__}"[:200]}
    if not summary.get("ok"):
        failed = summary.get("failed") or {}
        _record_block(
            repo, lane, run_id,
            f"빠른 검사 실패 "
            f"({' '.join(failed.get('argv', [])[:2])}): "
            f"{failed.get('log_tail', '')}"[:400])
        _stamp_fc_block(repo, lane, rec)
        return {"ran": False, "reason": "검사 실패"}
    # M2.5a: a green fastcheck that never directly executed the
    # bumped package must not read as a full verification. Legacy
    # receipts without the field keep today's title.
    # M2.5c: a native lock drift rides in the same detail line.
    pod = summary.get("pod_drift") or {}
    pod_note = ""
    if isinstance(pod, dict) and pod.get("label"):
        pod_note = (f" · {pod['label']} "
                    f"({pod.get('pod', '')} {pod.get('pinned', '')} → "
                    f"{pod.get('target', '')})")
    exercised = summary.get("exercised") or {}
    if exercised.get("status") == "not-run":
        # M2.8a1: a dep that no local step ran but PR CI does gets
        # the CI-only sentence (status stays not-run). Legacy
        # receipts without ci_consumers keep today's wording.
        ci = [str(f) for f in (exercised.get("ci_consumers") or [])]
        consumers = ", ".join(exercised.get("consumer_scripts") or [])
        if ci:
            _note_once(
                repo, f"fc-done-{run_id}", "m2-done",
                "로컬 검사에서 실행되지 않음 — PR CI에서만 실행됨"
                f"({', '.join(ci)}) · PR CI 결과로 확인",
                f"{len(summary.get('steps') or [])}단계 전부 통과 "
                f"({summary.get('wall_s', 0):.0f}초) · "
                f"PR CI 결과로 확인 ({', '.join(ci)})"
                + pod_note,
                run=run_id)
        else:
            # M2.15: a passing consumer build step renames the
            # label — the dep ran in its consumer's one-shot build
            # (the glance and batch card read this title).
            cons = summary.get("consumer") or {}
            cons_label = str(cons.get("label") or "") \
                if isinstance(cons, dict) else ""
            if isinstance(cons, dict) and cons.get("status") \
                    == "pass" and cons_label:
                _note_once(
                    repo, f"fc-done-{run_id}", "m2-done",
                    cons_label,
                    f"{len(summary.get('steps') or [])}단계 전부 통과 "
                    f"({summary.get('wall_s', 0):.0f}초) · "
                    f"{cons_label}"
                    + pod_note,
                    run=run_id)
            else:
                _note_once(
                    repo, f"fc-done-{run_id}", "m2-done",
                    "빠른 검사 통과 — 이 패키지를 직접 실행한 단계 없음",
                    f"{len(summary.get('steps') or [])}단계 전부 통과 "
                    f"({summary.get('wall_s', 0):.0f}초) · "
                    f"이 패키지를 쓰는 스크립트: {consumers or '없음'}"
                    + pod_note,
                    run=run_id)
    elif exercised.get("status") == "via-tool":
        # M2.5d: a plugin/config/types package a gate tool loads by
        # convention — said as a guess, with the gate named.
        via = exercised.get("via") or {}
        tool = str(via.get("gate") or via.get("label") or "검사")
        _note_once(repo, f"fc-done-{run_id}", "m2-done",
                   f"빠른 검사 통과 — {tool} 단계가 불러오는 "
                   "패키지(규칙 추정)",
                   f"{len(summary.get('steps') or [])}단계 전부 통과 "
                   f"({summary.get('wall_s', 0):.0f}초) · "
                   f"경유 단계: {via.get('label') or tool}"
                   + pod_note,
                   run=run_id)
    else:
        _note_once(repo, f"fc-done-{run_id}", "m2-done",
                   "자율 검증 완료",
                   f"{len(summary.get('steps') or [])}단계 전부 통과 "
                   f"({summary.get('wall_s', 0):.0f}초)"
                   + pod_note,
                   run=run_id)
    return {"ran": True, "summary": summary}


def _run_batch_stage(repo, run_id, lane, rec, base_id, fc_summary,
                     _import=None, extra_primaries=None):
    """Register the auto batch after a passing fastcheck (2b).

    Builds the G3-shaped manifest and passes it through the SAME
    import_batch validation as the UI file import (never a side
    door). Holds the worker lease across the import; lease busy is a
    skip, import refusals block the lane with the validator's own
    message. An existing batch id means hands off (no duplicates).
    Unexpected errors block loudly, never silently."""
    from . import batches as _batches
    import_ = (_import if _import is not None
               else _batches.import_batch)
    nxt = get_policy(repo)
    if not (nxt.get("batch") and STAGE_OPEN.get("batch")):
        if nxt.get("batch"):
            events.append(repo, "m2-skip", "batch",
                          f"자율 묶음 {NOT_OPEN_NOTE}",
                          "묶음 단계가 아직 열리지 않았습니다", actor="m2")
        else:
            _note_once(repo, f"skip-batch-{run_id}", "m2-skip",
                       "자율 다음 단계 꺼짐",
                       "묶음 단계가 꺼져 있어 검증된 작업실에서 멈춥니다",
                       run=run_id)
        return {"ran": False, "reason": "batch stage off"}
    op_id = str((rec or {}).get("op_id") or "")
    # M2.4c live fix: 12 of 16 verified ops already ride existing
    # batches as primaries. A duplicate is a skip with a grouped
    # note — never a block, never a cooldown. Read-only, before
    # the lease.
    try:
        from . import store as _store
        state = _store.load(run_id)
    except SystemExit:
        state = {}
    dup = _batches.find_candidate_batch(
        repo, state.get("dep"), state.get("target"), lane)
    # M2.17c: a refreshed joint gets its own batch even when the
    # OLD joint batch still holds a member key — iff the supersede
    # map marks that old batch replaced by THIS joint op. The old
    # batch keeps its holds/attests and is never READY (M2.14c
    # rule). A non-superseded duplicate keeps today's skip.
    if dup and not _refreshed_joint_exempts(repo, rec, dup):
        return {"ran": False, "reason": f"dup of {dup}"}
    # M2.17f: a joint's every member key and member run is
    # pre-checked with the same supersede exemption — a
    # non-superseded clash skips with the dup note instead of
    # walking into the import refusal (and the lane cooldown).
    _clash = _batches.joint_candidate_clash(repo, lane, rec)
    if _clash:
        return {"ran": False, "reason": f"dup of {_clash}"}
    # M2.5b backstop: an RN-lockstep family member is never
    # auto-batched alone — read the integrated tree itself.
    held = lockstep_status(
        str(((rec or {}).get("result") or {}).get("path") or ""),
        (state.get("dep") if isinstance(state, dict) else None),
        (state.get("target") if isinstance(state, dict) else None))
    if held is not None:
        return {"ran": False, "reason": f"lockstep {held['reason']}"}
    # M2.8b3: a lock diff mixing two react-native minors never
    # auto-batches — the lane result would ship two RN trees (live:
    # expo 57->58). Read-only, refusal-only, never a block and
    # never a cooldown count; manual UI import stays possible.
    try:
        _base_rec = _batches.load_batch(base_id)
    except (Exception, SystemExit):  # noqa: BLE001 - builder says it
        _base_rec = None
    _mixed = rn_group_mixed(_lane_lock_dupes(
        _base_rec, lane,
        str(((rec or {}).get("result") or {}).get("path") or "")))
    if _mixed is not None:
        # M2.14a: a refused mix may have a partner that supplies
        # the other half — record the hint for the joint retry.
        # Advisory only: the refusal below is unchanged.
        try:
            detect_joint_partner(repo, lane, rec or {})
        except (Exception, SystemExit):  # noqa: BLE001 - hint bonus
            pass
        return {"ran": False, "reason": f"lockstep {_mixed}"}
    # M2.16b: a MAJOR runtime-helpers bump with its compiler on
    # the old major never auto-batches — the derived tree itself
    # says so (read-only). Refusal-only, like the lockstep above:
    # never a block, never a cooldown count; manual UI import
    # stays possible.
    _cm = compiler_major_mismatch(
        str(((rec or {}).get("result") or {}).get("path") or ""),
        str(state.get("dep") if isinstance(state, dict) else ""))
    if _cm is not None:
        return {"ran": False, "reason": f"compiler {_cm}"}
    # FIX-a1: a disabled predecessor stops this stage — recomputed
    # here, right before acting (after the refusal-only pre-checks
    # above, which never act), with the glance's own wording.
    _ok, _why = stage_prefix_ok(nxt, "batch")
    if not _ok:
        return {"ran": False, "reason": "stage off: " + _why}
    try:
        bid = batch_id_for_op(op_id)
    except ValueError as exc:
        _record_block(repo, lane, run_id, str(exc)[:300])
        return {"ran": False, "reason": str(exc)[:200]}
    try:
        existing = _batches.load_batch(bid)
    except Exception:  # noqa: BLE001 - missing means new
        existing = None
    if isinstance(existing, dict):
        _note_once(repo, f"skip-batched-{run_id}", "m2-skip",
                   "자율 묶음 건너뜀",
                   f"{run_id}: {bid} 이미 등록됨", run=run_id)
        return {"ran": False, "reason": "already batched"}
    try:
        profile = repos.load(repo)
    except SystemExit:
        _note_once(repo, f"skip-batch-{run_id}", "m2-skip",
                   "자율 묶음 건너뜀", f"{run_id}: 등록된 저장소 없음",
                   run=run_id)
        return {"ran": False, "reason": "unknown repo"}
    try:
        manifest = build_batch_manifest(
            repo, run_id, lane, rec, base_id, fc_summary,
            profile.get("fastcheck") or {},
            extra_primaries=extra_primaries)
    except ValueError as exc:
        _record_block(repo, lane, run_id, str(exc)[:300])
        return {"ran": False, "reason": str(exc)[:200]}
    now = time.time()
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        busy_since = st.get("m2_lease_busy_since")
    lease = sched.acquire_worker_lease(f"m2-{repo}")
    if lease is None:
        return _lease_busy(repo, busy_since, now, [], [])
    started = time.time()
    try:
        with sched.locked(repo):
            st = _m2_defaults(sched.load(repo))
            st["m2_lease_busy_since"] = None
            sched.save(st)
        batch = import_(manifest)
    except _batches.BatchError as exc:
        _record_block(repo, lane, run_id, str(exc)[:300])
        return {"ran": False, "reason": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 - loud, never silent
        _record_block(repo, lane, run_id,
                      f"묶음 등록 오류 "
                      f"{type(exc).__name__}: {exc}"[:300])
        return {"ran": False, "reason": f"{type(exc).__name__}"[:200]}
    finally:
        try:
            sched.release_worker_lease(lease)
        except Exception:  # noqa: BLE001 - release never fails a tick
            pass
    wall_fc = (fc_summary or {}).get("wall_s", 0)
    _note_once(repo, f"batch-done-{run_id}", "m2-done",
               f"자율 묶음 등록 {bid}",
               f"Root 확인 대기 · 검사 {wall_fc:.0f}초 · 가져오기 "
               f"{time.time() - started:.0f}초",
               run=run_id)
    return {"ran": True, "batch": batch}


def _rollup_done_ops(repo: str, lane: str) -> list:
    """DONE rollup op records for (repo, lane): [{op_id, leaves}]."""
    from .util import read_json
    out = []
    try:
        files = sorted((paths.home() / "integration_ops").glob(
            "rollup-*.json"))
    except OSError:
        return out
    for f in files:
        try:
            rec = read_json(f, default=None)
        except Exception:  # noqa: BLE001 - unreadable never acts
            continue
        if (not isinstance(rec, dict) or rec.get("op") != "rollup"
                or rec.get("status") != "done"
                or rec.get("repo") != repo
                or rec.get("lane") != lane):
            continue
        out.append({"op_id": str(rec.get("op_id") or f.stem),
                    "leaves": [str(x.get("batch") or "")
                               for x in (rec.get("leaves") or [])
                               if isinstance(x, dict)],
                    "updated_at": str(rec.get("updated_at") or "")})
    return out


def _record_reshaped_note(repo: str, lane: str, op_id: str) -> None:
    """N1: stash the reshaped-lane note the glance reads.

    The driver evaluated; the summary only displays. Shown while the
    named op is still the newest done rollup for the lane (then a
    newer rollup hides it); cleared on same-set hands-off."""
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        st["m2_rollup_reshaped"] = {"lane": lane, "op": op_id,
                                   "at": _now_iso()}
        sched.save(st)


def _clear_reshaped_note(repo: str, lane: str) -> None:
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        cur = st.get("m2_rollup_reshaped") or {}
        if cur.get("lane") == lane:
            st.pop("m2_rollup_reshaped", None)
            sched.save(st)


def _rollup_plan(repo: str, lane: str):
    """READY leaf set for (repo, lane): (base_id, [leaf ids]) or None.

    M3: leaves qualify only through evaluate()=READY on the shared
    live map — one closure, one live-map pass, no shortcuts. Leaves
    are READY single-primary lane batches under a READY default
    base; the base itself is never a leaf.
    """
    from . import batches as _batches
    lane_batches = [b for b in _batches.list_batches()
                    if isinstance(b, dict)
                    and b.get("repo") == repo
                    and (b.get("target_branch") or "") == lane]
    if not lane_batches:
        return None
    try:
        base = _batches.default_base(repo, lane)
    except Exception:  # noqa: BLE001 - no base, no rollup
        return None
    base_id = (base or {}).get("id") or ""
    if not base_id:
        return None
    try:
        closed = _batches.closure(lane_batches)
        live_all, diag_all = _batches.live_map_with_diag(closed)
    except Exception:  # noqa: BLE001 - unreadable never acts
        return None
    try:
        ancestors = _batches._rollup_ancestor_ids(
            _batches.load_batch(base_id))
    except Exception:  # noqa: BLE001 - unreadable base, no rollup
        return None
    ready = {}
    for b in lane_batches:
        try:
            res = _batches.evaluate(b, live_all, live_diag=diag_all)
        except Exception:  # noqa: BLE001 - one batch never kills it
            continue
        results = res.get("results") or []
        if results and all(r.get("ready") for r in results):
            ready[b.get("id")] = b
    if base_id not in ready:
        return None
    ancestors.add(base_id)
    leaves = sorted(
        bid for bid, b in ready.items()
        if bid != base_id
        and len([p for p in (b.get("primaries") or [])
                 if isinstance(p, dict)]) == 1
        and base_id in ancestors.union(
            _rollup_ancestors_safe(bid)))
    if not leaves:
        return None
    return (base_id, leaves)


def _rollup_ancestors_safe(batch_id: str) -> set:
    from . import batches as _batches
    try:
        return _batches._rollup_ancestor_ids(
            _batches.load_batch(batch_id))
    except Exception:  # noqa: BLE001 - unreadable means no ancestry
        return set()


def _run_rollup_stage(repo: str, lane: str):
    """Roll READY leaves onto their READY base (M2.3 driver).

    Same-set-as-done means hands off (M2); a strictly larger set
    proceeds as a NEW op (its drafts say "supersedes"). Holds the
    worker lease across the call; lease busy and op-lock busy (409)
    are skips; refusals block the lane with the validator's own
    message. Drafts stay text only — the driver never publishes.
    """
    from . import batches as _batches
    nxt = get_policy(repo)
    if not (nxt.get("rollup") and STAGE_OPEN.get("rollup")):
        return {"ran": False, "reason": "rollup stage off"}
    plan = _rollup_plan(repo, lane)
    if plan is None:
        _note_once(repo, f"skip-rollup-{lane}", "m2-skip",
                   "자율 합치기 건너뜀",
                   f"{lane}: 합칠 준비 완료 묶음 없음")
        return {"ran": False, "reason": "no READY leaves"}
    base_id, leaves = plan
    current = set(leaves)
    # M2: the same READY set as a done rollup means hands off; only
    # the NEWEST done rollup may be superseded (a strict subset of
    # current), while its op file stays byte-identical.
    # N1: any other relation to the newest done rollup (a leaf
    # dropped out or swapped — even when an OLDER op would fit) is
    # the owner's call: skip with a note into 사람이 할 일, never a
    # parallel rollup with competing drafts.
    newest = None
    for done in _rollup_done_ops(repo, lane):
        old = set(done["leaves"])
        if not old:
            continue
        if newest is None or done["updated_at"] > newest["updated_at"]:
            newest = done
    if newest is not None:
        old = set(newest["leaves"])
        if old == current:
            _clear_reshaped_note(repo, lane)
            _note_once(repo, f"skip-rolled-{lane}", "m2-skip",
                       "자율 합치기 건너뜀",
                       f"{lane}: {newest['op_id']}와 같은 묶음 이미 합쳐짐")
            return {"ran": False, "reason": "already rolled"}
        if old < current:
            supersedes = {"op": newest["op_id"],
                          "new": sorted(current - old)}
        else:
            _record_reshaped_note(repo, lane, newest["op_id"])
            _note_once(repo, f"skip-reshaped-{lane}", "m2-skip",
                       "자율 합치기 건너뜀",
                       f"{lane}: 레인 구성이 바뀜 — 이전 합치기 "
                       f"{newest['op_id']}와 다름, 사람이 확인")
            return {"ran": False, "reason": "레인 구성이 바뀜"}
    else:
        supersedes = None
    now = time.time()
    with sched.locked(repo):
        st = _m2_defaults(sched.load(repo))
        busy_since = st.get("m2_lease_busy_since")
    lease = sched.acquire_worker_lease(f"m2-{repo}")
    if lease is None:
        return _lease_busy(repo, busy_since, now, [], [])
    started = time.time()
    try:
        with sched.locked(repo):
            st = _m2_defaults(sched.load(repo))
            st["m2_lease_busy_since"] = None
            sched.save(st)
        out = _batches.rollup_leaves(repo, lane, base_id, leaves,
                                       supersedes=supersedes)
    except _batches.IntegrationError as exc:
        message = str(exc)
        if (getattr(exc, "http_status", None) == 409
                and "실행 중입니다" in message):
            _note_once(repo, f"skip-rollup-busy-{lane}", "m2-skip",
                       "자율 합치기 건너뜀",
                       f"{lane}: 다른 확인 작업 실행 중")
            return {"ran": False, "reason": "다른 확인 작업 실행 중"}
        _record_block(repo, lane, f"rollup:{base_id}", message)
        return {"ran": False, "reason": message[:200]}
    except Exception as exc:  # noqa: BLE001 - loud, never silent
        _record_block(repo, lane, f"rollup:{base_id}",
                      f"합치기 오류 "
                      f"{type(exc).__name__}: {exc}"[:300])
        return {"ran": False, "reason": f"{type(exc).__name__}"[:200]}
    finally:
        try:
            sched.release_worker_lease(lease)
        except Exception:  # noqa: BLE001 - release never fails a tick
            pass
    _note_once(repo, f"rollup-done-{lane}", "m2-done",
               f"자율 합치기 완료 {lane} {len(leaves)}개 묶음",
               f"초안은 글만 · 가져오기 "
               f"{time.time() - started:.0f}초")
    return {"ran": True, "rollup": out}
