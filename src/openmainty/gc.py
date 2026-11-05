"""openmainty gc --dry-run: report regenerable weight, delete nothing.

Categories (reclaimable only — receipts, logs, diffs, ops, batches
and events are never listed):
- run worktrees' node_modules (runs/*/work/node_modules)
- finished prepare/rollup worktrees older than N days whose op is
  done/blocked and whose branch is merged-or-abandoned (tip at the
  base head, or the ref already gone)
- fastcheck caches (cache/*: yarn, corepack — default checksums kept)
- promoted app copies beyond the newest 3 plus `current`
- /tmp scratch with OpenMainty-owned name prefixes
- snapshot tmps older than 1 h whose pid is not alive
  (summary/snapshot.<pid>.tmp, summary/batches-snapshot.<pid>.tmp)
Anything else (own commits on the branch, unreadable base checkout,
orphan dirs, unknown-age app copies) goes to needs_review, never to
the reclaimable list. Python 3.9 portable.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .util import git_env_for_readonly as _git_env

NEVER = ("receipts (*.json)", "logs (*.log)", "diffs", "ops",
         "batches", "events")
# M3.1 blocker fix: ONLY prefixes OpenMainty's own code creates via
# mkdtemp/TemporaryDirectory on the system tmp (exact literals, kept
# in sync by the drift-guard test). Never a bare "openmainty-" (that
# would match the supervision record) and never "om66-" (Root's
# scratch). The fastcheck per-op tmp HOME (fc-*) lives under the app
# home's tmp/, not the system tmp, so it is not listed here.
OWNED_TMP_PREFIXES = ("openmainty-probe-", "openmainty-release-")
# Hard exclusion (defense in depth): anything with "supervision" in
# the name is never listed and never deleted, whatever prefix it
# carries.
SUPERVISION_MARK = "supervision"
APP_KEEP = 3
# M3.1: --apply supports only these. stale_worktree stays report-only:
# deleting a linked-worktree dir leaves admin entries in the owner's
# .git, and pruning them would write the owner's .git, which we never
# do.
APPLY_CATEGORIES = ("node_modules", "cache", "old_app", "tmp_scratch",
                    "stale_tmp")
# M2.16b: only snapshot tmps are ever stale_tmp candidates — exact
# names, never a .json, never outside summary/.
_STALE_TMP_RE = re.compile(r"^(snapshot|batches-snapshot)\.\d+\.tmp$")
STALE_TMP_AGE_S = 3600.0


def _pid_alive(pid: int) -> bool:
    """True when a process with pid exists (ours or another uid's).

    Fail-closed: any answer but a clean "no such process" counts
    as alive, so a weird pid never becomes deletable. Never raises.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True
# Note: "runs" is deliberately NOT here — run worktrees'
# node_modules is a supported category. Evidence files under runs/
# (run.json) are still protected by the .json/.log suffix rule, and
# --apply only ever deletes fresh-report-listed paths.
NEVER_DIR_PARTS = ("integration_ops", "batches", "events", "diffs")
TERMINAL_OP_STATUS = ("done", "blocked")


class ApplyError(Exception):
    """Owner-visible gc --apply refusal (message is the reason)."""


def _utc_minute(_minute=None) -> int:
    if callable(_minute):
        return int(_minute())
    if _minute is not None:
        return int(_minute)
    return datetime.now(timezone.utc).minute


def idle_refusals(home, _minute=None) -> list:
    """Refuse-first gate shared by release/rollback/gc --apply.

    Refuses while the worker lease is held, while any integration op
    is non-terminal (a kickstart would kill a dashboard-driven
    prepare), or at :13-:15 UTC (the owner's cron). Returns the
    Korean reason list (empty = idle). Pure reads, never writes."""
    from . import sched
    reasons = []
    try:
        if sched.worker_lease_held():
            reasons.append("작업 잠금 사용 중 — 끝난 뒤 다시 실행하세요")
    except Exception:  # noqa: BLE001 - unreadable means unknown, say so
        reasons.append("작업 잠금 상태 읽기 실패 — 안전을 위해 중단합니다")
    try:
        running = nonterminal_ops(home)
    except Exception:  # noqa: BLE001 - same fail-closed rule
        running = ["작업 기록 읽기 실패"]
    for op in running:
        reasons.append(f"진행 중 작업 있음 ({op}) — 끝난 뒤 다시 실행하세요")
    if _utc_minute(_minute) in (13, 14, 15):
        reasons.append("소유자 정기 점검 시간(:13–:15) — 16분 이후에 실행하세요")
    return reasons


def nonterminal_ops(home) -> list:
    """Op ids whose status is neither done nor blocked (read-only)."""
    ops_dir = Path(home) / "integration_ops"
    try:
        files = sorted(ops_dir.glob("*.json"))
    except OSError:
        return []
    out = []
    for opf in files:
        if opf.name.endswith(".fastcheck.json"):
            continue
        try:
            rec = json.loads(opf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if str(rec.get("status") or "") not in TERMINAL_OP_STATUS:
            out.append(str(rec.get("op_id") or opf.stem))
    return out


def _is_never_path(path: Path) -> bool:
    """True when deleting this path could touch evidence (defense in
    depth — the report never lists these, and --apply re-checks)."""
    name = path.name
    if name.endswith((".json", ".log")):
        return True
    return any(part in NEVER_DIR_PARTS for part in path.parts)


def _op_locks_held(op_ids) -> set:
    """Subset of op_ids whose exclusion lock is held right now."""
    try:
        from . import m2 as _m2
    except Exception:  # noqa: BLE001 - no lock check, status covers it
        return set()
    held = set()
    for op_id in op_ids:
        try:
            with _m2.op_locked(op_id):
                pass
        except _m2.OpBusy:
            held.add(op_id)
        except Exception:  # noqa: BLE001 - best-effort only
            pass
    return held


def apply_category(home, category: str, confirm_bytes: int,
                   tmp_root=None, _minute=None) -> dict:
    """Delete exactly one category's fresh report set (owner CLI only).

    Fresh `dry_run_report()` is recomputed here; `confirm_bytes` must
    equal that category's fresh total or the run refuses with the
    exact total and deletes nothing. Symlinks are never followed
    (skipped with a reason); paths outside the home (or the owned
    tmp prefix for tmp_scratch) refuse; NEVER names refuse even if
    forced into the report. Held paths (non-terminal op worktree or
    held op lock) are skipped with reasons, never deleted. Returns
    the receipt dict and writes it to <home>/gc/<at>.json. Raises
    ApplyError on every refusal. No API endpoint and no dashboard
    button call this in M3.1 — CLI only.
    """
    home = Path(home)
    if category not in APPLY_CATEGORIES:
        if category == "stale_worktree":
            raise ApplyError(
                "stale_worktree는 보고서만 — 연결된 작업실 삭제는 소유자 .git을 "
                "건드리므로 --apply 대상이 아닙니다")
        raise ApplyError(f"알 수 없는 정리 범위: {category}")
    idle = idle_refusals(home, _minute=_minute)
    if idle:
        raise ApplyError("; ".join(idle))
    report = dry_run_report(home, tmp_root=tmp_root)
    items = [i for i in report.get("reclaimable", [])
             if i.get("category") == category]
    total = sum(int(i.get("bytes") or 0) for i in items)
    if int(confirm_bytes) != total:
        raise ApplyError(
            f"확인 바이트 불일치: 새로 계산한 합계는 {total}바이트 — "
            f"`--confirm-bytes {total}`로 다시 실행하세요. 삭제 안 함")
    ops_by_path: dict = {}
    ops_dir = home / "integration_ops"
    try:
        op_files = sorted(ops_dir.glob("*.json"))
    except OSError:
        op_files = []
    for opf in op_files:
        if opf.name.endswith(".fastcheck.json"):
            continue
        try:
            rec = json.loads(opf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        wt = (rec.get("result") or {}).get("path") or ""
        if wt:
            ops_by_path.setdefault(str(wt), []).append(rec)
    def _held_add(wt):
        # Resolved like the candidate paths below: on macOS /tmp is
        # a symlink (/var -> /private/var), so an unresolved record
        # path would never prefix-match a resolved candidate.
        try:
            held_paths.add(str(Path(wt).resolve()))
        except OSError:
            held_paths.add(str(wt))
    held_paths = set()
    for wt, recs in ops_by_path.items():
        live = [r for r in recs
                if str(r.get("status") or "") not in TERMINAL_OP_STATUS]
        if live:
            _held_add(wt)
    locked = _op_locks_held(
        [str((r.get("op_id") or "")) for recs in ops_by_path.values()
         for r in recs if r.get("op_id")])
    for wt, recs in ops_by_path.items():
        if any(str(r.get("op_id") or "") in locked for r in recs):
            _held_add(wt)
    deleted, skipped = [], []
    import shutil as _shutil
    for item in items:
        raw = str(item.get("path") or "")
        path = Path(raw)
        try:
            if path.is_symlink():
                skipped.append({"path": raw,
                                "reason": "심볼릭 링크 — 따라가지 않음"})
                continue
            real = path.resolve()
            if category == "tmp_scratch":
                troot = Path(str(tmp_root) if tmp_root
                             else os.environ.get("TMPDIR") or "/tmp")
                ok = str(real).startswith(str(troot.resolve()) + os.sep)
            else:
                ok = str(real).startswith(str(home.resolve()) + os.sep)
            if not ok:
                skipped.append({"path": raw, "reason": "범위 밖 — 건너뜀"})
                continue
        except OSError as exc:
            skipped.append({"path": raw, "reason": f"읽기 실패: {exc}"})
            continue
        if _is_never_path(real):
            skipped.append({"path": raw,
                            "reason": "증거 영역 — 절대 삭제 안 함"})
            continue
        if SUPERVISION_MARK in Path(raw).name:
            skipped.append({"path": raw,
                            "reason": "감독 기록 — 절대 삭제 안 함"})
            continue
        if any(str(real) == h or str(real).startswith(h + os.sep)
               for h in held_paths):
            skipped.append({"path": raw,
                            "reason": "진행 중 작업실 — 끝난 뒤 다시"})
            continue
        if category == "stale_tmp":
            # M2.16b: files, not trees — exact snapshot-tmp names
            # directly under <home>/summary/ only. Anything else
            # (a .json slipped into a forced report, a path
            # outside summary/, a symlink) is skipped, never
            # deleted.
            try:
                in_summary = (real.parent
                              == (home / "summary").resolve())
            except OSError:
                in_summary = False
            if (path.is_symlink() or not real.is_file()
                    or not _STALE_TMP_RE.match(Path(raw).name)
                    or not in_summary or _is_never_path(real)):
                skipped.append({"path": raw,
                                "reason": "정리 대상 아님 — 건너뜀"})
                continue
            try:
                weighed = real.stat().st_size
                real.unlink()
            except OSError as exc:
                skipped.append({"path": raw,
                                "reason": f"삭제 실패: {exc}"})
                continue
            deleted.append({"path": raw, "bytes": weighed})
            continue
        if not real.is_dir():
            skipped.append({"path": raw, "reason": "이미 없음 — 건너뜀"})
            continue
        weighed = _tree_bytes(real)
        try:
            _shutil.rmtree(real)
        except OSError as exc:
            skipped.append({"path": raw, "reason": f"삭제 실패: {exc}"})
            continue
        deleted.append({"path": raw, "bytes": weighed})
    freed = sum(int(d.get("bytes") or 0) for d in deleted)
    receipt = {"at": _now_iso(), "category": category,
               "confirmed_bytes": total, "deleted": deleted,
               "skipped": skipped, "freed_bytes": freed,
               "report_at": report.get("at")}
    try:
        gdir = home / "gc"
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / (receipt["at"].replace(":", "-") + ".json")).write_text(
            json.dumps(receipt, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError:
        pass
    return receipt


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tree_bytes(path: Path) -> int:
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


def _age_days(path: Path, now: float) -> float | None:
    try:
        return (now - Path(path).stat().st_mtime) / 86400.0
    except OSError:
        return None


def _git_rev_parse(branch: str, cwd: str) -> str | None:
    try:
        argv = ["git", "rev-parse", "--verify", branch]
        out = subprocess.run(argv,
                             cwd=str(cwd), stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=60,
                             text=True,
                             env=_git_env(argv))
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


def _promo_rank(home: Path) -> dict:
    """sha -> promotion order (newest last) from app/history.jsonl."""
    rank: dict = {}
    try:
        lines = (home / "app" / "history.jsonl").read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return rank
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("event") == "promote" \
                and e.get("to"):
            rank.setdefault(str(e["to"]), len(rank))
    return rank


_DU_TIMEOUT_S = 600


def home_bytes_du(home, timeout_s=_DU_TIMEOUT_S):
    """Home size, out of process: nice'd `du -sk -x` (same filesystem)
    with a timeout. Returns bytes, or None on any failure. Never
    raises; reads nothing into Python (S17/D3)."""
    try:
        proc = subprocess.run(
            ["nice", "-n", "10", "du", "-sk", "-x", str(home)],
            capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "").strip().split()[0]) * 1024
    except (IndexError, ValueError):
        return None


def write_disk_cache(home, home_bytes=None, error=None, at=None):
    """Atomically write cache/disk.json {home_bytes, at_epoch, error}.

    Only ever this one small file; receipts, logs, diffs, ops,
    batches and events are untouched by construction."""
    payload = {"home_bytes": home_bytes,
               "at_epoch": at if at is not None else time.time(),
               "error": error}
    path = Path(home) / "cache" / "disk.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return payload


def disk_status(home) -> dict:
    """Conservative free bytes (statvfs available-to-user — the same
    measure as the fastcheck disk guard, deliberately smaller than
    the macOS important-usage capacity) plus the home tree size.
    The walk can take minutes on a large home; callers cache it."""
    try:
        st = os.statvfs(str(home))
        free = int(st.f_bavail * st.f_frsize)
    except OSError:
        free = -1
    return {"free_bytes": free,
            "home_bytes": _tree_bytes(Path(home))}


def dry_run_report(home, older_than_days: int = 7,
                   tmp_root=None) -> dict:
    """Build the gc report. Reads only; never deletes anything."""
    home = Path(home)
    now = time.time()
    cutoff = float(older_than_days or 0) * 86400.0
    reclaimable: list = []
    needs_review: list = []

    def _item(category, path, note, age=None):
        return {"category": category, "path": str(path),
                "bytes": _tree_bytes(Path(path)),
                "age_days": None if age is None else round(age, 1),
                "note": note}

    # Op records keyed by result worktree path (fastcheck receipts
    # are *.fastcheck.json — read for nothing, listed for nothing).
    ops_by_path: dict = {}
    ops_dir = home / "integration_ops"
    try:
        op_files = sorted(ops_dir.glob("*.json"))
    except OSError:
        op_files = []
    for opf in op_files:
        if opf.name.endswith(".fastcheck.json"):
            continue
        try:
            rec = json.loads(opf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        wt = (rec.get("result") or {}).get("path") or ""
        if wt:
            ops_by_path.setdefault(wt, []).append(rec)
    stale_dirs: set = set()
    reviews = home / "reviews"
    try:
        worktrees = sorted(p for p in reviews.iterdir())
    except OSError:
        worktrees = []
    for wt in worktrees:
        recs = ops_by_path.get(str(wt)) or []
        done = [r for r in recs
                if str(r.get("status") or "") in ("done", "blocked")]
        if not done:
            age = _age_days(wt, now)
            if age is not None and age * 86400.0 > cutoff:
                needs_review.append(_item(
                    "stale_worktree", wt,
                    "orphan worktree (no done/blocked op) — "
                    "사람이 확인", age))
            continue
        rec = done[0]
        age = _age_days(wt, now)
        if age is None or age * 86400.0 <= cutoff:
            continue
        base = rec.get("base") or {}
        branch = str((rec.get("result") or {}).get("branch") or "")
        base_head = str(base.get("head") or "")
        checkout = str(base.get("checkout") or "")
        if branch and base_head and Path(checkout).is_dir():
            tip = _git_rev_parse(branch, checkout)
            if tip is None:
                stale_dirs.add(str(wt))
                reclaimable.append(_item(
                    "stale_worktree", wt,
                    f"op {rec.get('op_id')} "
                    f"{rec.get('status')} · 브랜치 {branch} 없음 "
                    f"(이미 정리됨)", age))
                continue
            if tip == base_head:
                stale_dirs.add(str(wt))
                reclaimable.append(_item(
                    "stale_worktree", wt,
                    f"op {rec.get('op_id')} "
                    f"{rec.get('status')} · 브랜치 {branch} 기준 "
                    f"head (자체 커밋 없음)", age))
                continue
            needs_review.append(_item(
                "stale_worktree", wt,
                f"op {rec.get('op_id')} · 브랜치 {branch} 자체 커밋 "
                f"({tip[:12]}) — 사람이 확인", age))
            continue
        needs_review.append(_item(
            "stale_worktree", wt,
            f"op {rec.get('op_id')} · 기준 작업실 확인 불가 — "
            f"사람이 확인", age))
    # Run worktrees' node_modules, plus reviews leftovers whose whole
    # worktree was NOT already listed (no double counting).
    nm_paths = sorted((home / "runs").glob("*/work/node_modules"))
    try:
        nm_paths += [p for p in reviews.glob("*/node_modules")
                     if str(p.parent) not in stale_dirs]
    except OSError:
        pass
    for nm in nm_paths:
        if nm.is_dir() and not nm.is_symlink():
            reclaimable.append(_item(
                "node_modules", nm,
                "run worktree (reinstall로 재생성)", None))
    # Fastcheck caches (regenerable by design).
    try:
        caches = sorted((home / "cache").iterdir())
    except OSError:
        caches = []
    for cache in caches:
        reclaimable.append(_item(
            "cache", cache, "재생성 가능 (기본 체크섬 유지)", None))
    # Promoted app copies beyond the newest APP_KEEP plus current.
    adir = home / "app"
    cur = ""
    try:
        link = adir / "current"
        cur = link.resolve().name if link.is_symlink() else ""
    except OSError:
        pass
    try:
        shas = sorted(p.name for p in adir.iterdir()
                      if p.is_dir() and not p.is_symlink())
    except OSError:
        shas = []
    rank = _promo_rank(home)
    ordered = sorted(
        shas, key=lambda s: (0, s) if s not in rank else (1, rank[s]))
    keep = set(ordered[-APP_KEEP:]) | ({cur} if cur else set())
    for sha in ordered:
        if sha in keep:
            continue
        if sha in rank:
            reclaimable.append(_item(
                "old_app", adir / sha,
                "오래된 앱 사본 (newest 3 + current 유지)", None))
        else:
            needs_review.append(_item(
                "old_app", adir / sha,
                "홍보 기록 없음 — 사람이 확인", None))
    # /tmp scratch with OpenMainty-owned prefixes.
    troot = Path(str(tmp_root) if tmp_root
                 else os.environ.get("TMPDIR") or "/tmp")
    try:
        tmp_entries = sorted(troot.iterdir())
    except OSError:
        tmp_entries = []
    for entry in tmp_entries:
        if SUPERVISION_MARK in entry.name:
            continue
        if not entry.name.startswith(OWNED_TMP_PREFIXES):
            continue
        age = _age_days(entry, now)
        reclaimable.append(_item(
            "tmp_scratch", entry, "OpenMainty 임시 파일", age))
    # M2.16b: stale snapshot tmps — snapshot.<pid>.tmp and
    # batches-snapshot.<pid>.tmp directly under summary/, older
    # than 1 h, whose pid is not alive (leaked by a HOME flip
    # before the single-dir fix). Exact names only: never a
    # .json, never anything outside summary/.
    try:
        sents = sorted((home / "summary").iterdir())
    except OSError:
        sents = []
    for sent in sents:
        if not _STALE_TMP_RE.match(sent.name):
            continue
        if sent.is_symlink() or not sent.is_file():
            continue
        try:
            pid = int(sent.name.rsplit(".", 2)[-2])
            mtime = sent.stat().st_mtime
            size = sent.stat().st_size
        except (OSError, ValueError):
            continue
        if now - mtime <= STALE_TMP_AGE_S or _pid_alive(pid):
            continue
        reclaimable.append({
            "category": "stale_tmp", "path": str(sent),
            "bytes": size,
            "age_days": round((now - mtime) / 86400.0, 1),
            "note": "죽은 프로세스의 스냅샷 임시 파일 "
                    f"(pid {pid}, 보관 snapshot 유지)"})
    total = sum(int(i.get("bytes") or 0) for i in reclaimable)
    return {"at": _now_iso(), "home": str(home),
            "older_than_days": int(older_than_days or 0),
            "reclaimable": reclaimable, "needs_review": needs_review,
            "never": list(NEVER),
            "total_reclaimable_bytes": total,
            "deletes_nothing": True}
