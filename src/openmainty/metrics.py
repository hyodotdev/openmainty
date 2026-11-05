"""Honest autonomy metrics (M3.4a).

Read-only over an OpenMainty home: events, integration op
records, fastcheck receipts and batch files. No network, no
writes, no imports beyond the stdlib plus paths/repos/store.
Every number regenerates with `openmainty metrics`; pushes are
always 0 — there is no push code path (tests/test_metrics.py
greps src/ for one).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

CENSUS_KEYS = ("direct", "via-tool", "not-run", "unknown", "ci-only")

_HOLD_RE = re.compile(r"lockstep|podfile\.lock|pod install",
                      re.IGNORECASE)
_DUP_RE = re.compile(r"자율 묶음 건너뜀\s+(\d+)건")


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _parse_at(value) -> float | None:
    """Epoch seconds from an ISO 'at', or None when unreadable.

    Unreadable stamps count as inside any window — dropping
    evidence on a parse miss would undercount, silently."""
    try:
        text = str(value or "").strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _iter_events(home: Path):
    try:
        with open(home / "events.jsonl", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    yield ev
    except OSError:
        return


def _in_window(stamp: float | None, cutoff: float | None) -> bool:
    if cutoff is None:
        return True
    if stamp is None:
        return True
    return stamp >= cutoff


def _repo_of_run(base: Path, run_id: str) -> str:
    """Repo for a run id, read straight from base (never store.load:
    a missing run is a skip, not a SystemExit)."""
    if not run_id:
        return ""
    state = _read_json(base / "runs" / run_id / "run.json",
                       default=None)
    if not isinstance(state, dict):
        return ""
    return str(state.get("repo") or "")


def _derived_pipeline_done(base: Path, rec: dict) -> bool:
    """The M2.5d resume rule, read from base: a done reresolve whose
    derived_from names an m2-origin prepare op for the SAME run.
    M27FIXA-F1: the source prepare must be autonomy's own work — a
    manual prepare recovered by a manual reresolve is not an
    autonomous recovery."""
    if rec.get("op") != "reresolve" or rec.get("status") != "done":
        return False
    src_id = str(rec.get("derived_from") or "")
    run_id = str(rec.get("run_id") or "")
    if not (src_id and run_id):
        return False
    src = _read_json(base / "integration_ops" / (src_id + ".json"),
                     default=None)
    return isinstance(src, dict) and src.get("op") == "prepare" \
        and str(src.get("origin") or "") == "m2" \
        and str(src.get("run_id") or "") == run_id


def _op_files(home: Path):
    try:
        return sorted((home / "integration_ops").glob("*.json"))
    except OSError:
        return []


def collect(home=None, days: int | None = None) -> dict:
    """Regenerate every number from home (paths.home() default).

    `days` windows flow counts (events, receipts, ops) to the
    last N days; batch hold/waiting state is always current and
    labelled so. Never raises on bad data — skips it."""
    from . import paths as _paths
    base = Path(home) if home is not None else _paths.home()
    now = time.time()
    cutoff = now - days * 86400 if days else None
    try:
        repos = sorted(p.stem for p in (base / "repos").glob("*.json"))
    except OSError:
        repos = []

    def blank():
        return {"autonomy_since": None,
                "prepares": {"done": 0, "re_resolve": 0,
                             "rederive": 0,
                             "blocked": 0, "stale": 0},
                "fastcheck": {"green": 0, "red": 0, "direct": 0,
                              "via-tool": 0, "not-run": 0,
                              "ci-only": 0,
                              "unknown": 0, "archived_attempts": 0},
                "auto_batches": {"total": 0, "held": [],
                                 "waiting_on_root_batches": 0,
                                 "waiting_on_root_gates": 0,
                                 "lockstep_or_native_holds": []},
                "duplicate_skips": 0}

    rows = {name: blank() for name in repos}
    dup_latest: dict = {}
    # Autonomous prepare-dones come from the m2-done done-run
    # events (one per run that reached it); the "파생 잠금 출처"
    # detail marks the lock-re-resolve recoveries. Counting op
    # records instead blames the blocked parent for a
    # lockfix-derived done.
    done_runs: dict = {}
    # Events: autonomy start, duplicate skips, prepare-dones.
    for ev in _iter_events(base):
        repo = str(ev.get("repo") or "")
        if repo not in rows:
            continue
        if not _in_window(_parse_at(ev.get("at")), cutoff):
            continue
        kind = str(ev.get("kind") or "")
        title = str(ev.get("title") or "")
        ref = str(ev.get("ref") or "")
        if kind == "m2-policy" and "켜짐" in title:
            at = str(ev.get("at") or "") or None
            cur = rows[repo]["autonomy_since"]
            if at and (cur is None or at < cur):
                rows[repo]["autonomy_since"] = at
        # Refs read "done-<run_id>" (e.g. done-run-5ba27aee
        # for run run-5ba27aee) — strip only the "done-" prefix.
        if kind == "m2-done" and ref.startswith("done-"):
            run_id = ref[len("done-"):]
            if run_id and run_id not in done_runs:
                done_runs[run_id] = (
                    repo,
                    "파생 잠금 출처" in str(ev.get("detail") or ""),
                    # M2.9a: re-derived dones count next to
                    # re-resolves (a sub-count, not instead).
                    "기준에서 다시 적용" in str(ev.get("detail") or ""))
        if kind == "m2-skip" and title.startswith("자율 묶음 건너뜀"):
            # The CURRENT number is the latest note — summing every
            # past title would count the same backlog many times.
            m = _DUP_RE.search(title)
            if m:
                at = _parse_at(ev.get("at"))
                cur = dup_latest.get(repo)
                if cur is None or (at is not None and at >= cur[0]):
                    dup_latest[repo] = (
                        at if at is not None
                        else (cur[0] if cur else 0.0),
                        int(m.group(1)))
    # Done runs from the events above; blocked runs are m2
    # pipeline records (prepare, or a reresolve derived from that
    # run's prepare — the M2.5d resume rule) with a block and no
    # done-run event. "Other" is anything else m2 touched.
    for run_id, (repo, via_lockfix,
                 via_rederive) in done_runs.items():
        if repo not in rows:
            continue
        rows[repo]["prepares"]["done"] += 1
        if via_lockfix:
            rows[repo]["prepares"]["re_resolve"] += 1
        if via_rederive:
            rows[repo]["prepares"]["re_resolve"] += 1
            rows[repo]["prepares"]["rederive"] += 1
    ops_dir = base / "integration_ops"
    # FIX-a3: one current outcome per run, deduplicated against
    # the events above. A run is done when its pipeline record is
    # done — a prepare done, or a done reresolve derived from that
    # run's prepare (any origin: a manual lockfix recovery proves
    # the same recovery). It is blocked only when records exist
    # but no done record does. The old loop counted done-via-event
    # runs only through events and blamed the blocked parent for
    # every lockfix-derived done.
    by_run: dict = {}
    for path in _op_files(base):
        name = path.name
        if name.endswith(".fastcheck.json") or ".fastcheck.attempt-" \
                in name:
            continue
        try:
            if cutoff is not None and path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        rec = _read_json(path, default=None)
        if not isinstance(rec, dict):
            continue
        if rec.get("op") not in ("prepare", "reresolve"):
            continue
        run_id = str(rec.get("run_id") or "")
        if not run_id or run_id in done_runs:
            continue
        if rec.get("op") == "prepare" \
                and str(rec.get("origin") or "") != "m2":
            # M27FIXA-F1: a run whose prepare is not m2-origin is
            # excluded entirely — manual work is never autonomy's
            # count (not done, not blocked, not stale).
            continue
        if str(rec.get("origin") or "") != "m2" \
                and not rec.get("derived_from"):
            # Standalone manual work is not autonomy's count — but
            # a derived lockfix linked to an m2 prepare still
            # proves the recovery below.
            continue
        repo = str(rec.get("repo") or "")
        if repo not in rows:
            continue
        by_run.setdefault((repo, run_id), []).append(rec)
    for (repo, run_id), recs in by_run.items():
        if not any(str(r.get("origin") or "") == "m2"
                   for r in recs):
            # Fully manual run (only derived lockfix records got
            # this far) — excluded entirely.
            continue
        done_recs = [
            r for r in recs
            if str(r.get("status") or "") == "done"
            and (r.get("op") == "prepare"
                 or _derived_pipeline_done(base, r))]
        if done_recs:
            rows[repo]["prepares"]["done"] += 1
            if any(r.get("op") == "reresolve" for r in done_recs):
                rows[repo]["prepares"]["re_resolve"] += 1
            if any(r.get("op") == "reresolve"
                   and r.get("kind") == "rederive"
                   for r in done_recs):
                rows[repo]["prepares"]["rederive"] += 1
            continue
        # FIX-a4: a stale-marked record counts as stale (refresh-
        # stage work), never as blocked.
        if any(isinstance(r.get("stale"), dict) for r in recs):
            rows[repo]["prepares"]["stale"] += 1
            continue
        if any(str(r.get("status") or "") == "blocked"
               for r in recs):
            rows[repo].setdefault("_blocked_runs", set()).add(run_id)
    for row in rows.values():
        blocked_runs = row.pop("_blocked_runs", set())
        row["prepares"]["blocked"] = len(blocked_runs)
    for repo, (_at, num) in dup_latest.items():
        if repo in rows:
            rows[repo]["duplicate_skips"] = num
    try:
        receipts = sorted(ops_dir.glob("*.fastcheck.json"))
    except OSError:
        receipts = []
    try:
        archived = list(ops_dir.glob("*.fastcheck.attempt-*.json"))
    except OSError:
        archived = []
    for path in receipts:
        try:
            if cutoff is not None and path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        summary = _read_json(path, default=None)
        if not isinstance(summary, dict):
            continue
        op_id = path.name[: -len(".fastcheck.json")]
        op_rec = _read_json(ops_dir / (op_id + ".json"), default=None)
        if not isinstance(op_rec, dict):
            continue
        repo = _repo_of_run(base, str(op_rec.get("run_id") or ""))
        if repo not in rows:
            continue
        row = rows[repo]["fastcheck"]
        if summary.get("ok"):
            row["green"] += 1
        else:
            row["red"] += 1
        exercised = summary.get("exercised") or {}
        status = exercised.get("status")
        # M2.8a1: a not-run receipt whose dep runs in PR CI gets
        # its own bucket (the not-run count drops by the same).
        if status == "not-run" and exercised.get("ci_consumers"):
            status = "ci-only"
        row[status if status in CENSUS_KEYS else "unknown"] += 1
    for path in archived:
        try:
            if cutoff is not None and path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        op_id = path.name.split(".fastcheck.attempt-")[0]
        op_rec = _read_json(ops_dir / (op_id + ".json"), default=None)
        repo = ""
        if isinstance(op_rec, dict):
            repo = _repo_of_run(base, str(op_rec.get("run_id") or ""))
        if repo in rows:
            rows[repo]["fastcheck"]["archived_attempts"] += 1
    # Batches: current state (never windowed — labelled so).
    try:
        batch_files = sorted((base / "batches").glob("*.json"))
    except OSError:
        batch_files = []
    for path in batch_files:
        if path.name == "index.json":
            continue
        batch = _read_json(path, default=None)
        if not isinstance(batch, dict):
            continue
        if not batch.get("origin_m2"):
            continue
        repo = str(batch.get("repo") or "")
        if repo not in rows:
            continue
        auto = rows[repo]["auto_batches"]
        auto["total"] += 1
        hold = batch.get("hold")
        if isinstance(hold, dict):
            reason = str(hold.get("reason") or "")
            entry = {"id": str(batch.get("id") or path.stem),
                     "by": str(hold.get("by") or ""),
                     "reason": reason}
            auto["held"].append(entry)
            if _HOLD_RE.search(reason):
                auto["lockstep_or_native_holds"].append(entry)
        pending = 0
        for integ in batch.get("integrations") or []:
            if not isinstance(integ, dict):
                continue
            for gate in integ.get("gates") or []:
                if not isinstance(gate, dict):
                    continue
                key = str(gate.get("key") or "")
                if not key.startswith("worker:") and \
                        gate.get("status") == "pending":
                    pending += 1
        if pending:
            auto["waiting_on_root_batches"] += 1
            auto["waiting_on_root_gates"] += pending
    return {"generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "window_days": days,
            "repos": rows,
            "pushes_by_openmainty": 0,
            "push_basis": "no push code path: every push/PR "
                          "command literal in src/ lives in a "
                          "draft-builder function (copy-only text the "
                          "owner runs by hand)"}


def render_text(data: dict, lang: str = "ko") -> str:
    """Plain-language report. English with lang='en'."""
    en = str(lang or "").lower().startswith("en")
    lines = []
    days = data.get("window_days")
    scope = f"last {days} days" if days else "all time"
    if en:
        lines.append(f"OpenMainty autonomy metrics ({scope}, "
                     f"generated {data.get('generated_at')})")
    else:
        lines.append(f"OpenMainty 자율 지표 "
                     f"({'최근 %s일' % days if days else '전체 기간'}, "
                     f"{data.get('generated_at')} 기준)")
    missing = "none" if en else "기록 없음"
    for name, row in (data.get("repos") or {}).items():
        prep = row["prepares"]
        fc = row["fastcheck"]
        auto = row["auto_batches"]
        since = row["autonomy_since"] or missing
        if en:
            lines.append(
                f"[{name}] autonomy since {since}; "
                f"prepares done {prep['done']} "
                f"({prep['re_resolve']} via lock re-resolve, "
                f"{prep['rederive']} via re-derive), "
                f"blocked {prep['blocked']}, "
                f"stale {prep.get('stale', 0)}; fastcheck green "
                f"{fc['green']}, "
                f"red {fc['red']} (direct {fc['direct']}, via-tool "
                f"{fc['via-tool']}, not-run {fc['not-run']}, CI-only "
                f"{fc['ci-only']}, unknown "
                f"{fc['unknown']}, archived attempts "
                f"{fc['archived_attempts']}); auto batches "
                f"{auto['total']}, held {len(auto['held'])}, waiting "
                f"on human review {auto['waiting_on_root_batches']} "
                f"batches ({auto['waiting_on_root_gates']} gates); "
                f"duplicate skips {row['duplicate_skips']}")
            for held in auto["held"]:
                lines.append(f"  held: {held['id']} "
                             f"by {held['by'] or missing} — "
                             f"{held['reason'] or missing}")
        else:
            lines.append(
                f"[{name}] 자율 시작 {since}; 준비 완료 "
                f"{prep['done']}건(그중 잠금 재해결 "
                f"{prep['re_resolve']}건·다시 적용 "
                f"{prep['rederive']}건)·막힘 {prep['blocked']}건"
                f"·오래됨 {prep.get('stale', 0)}건; "
                f"빠른 검사 "
                f"통과 {fc['green']}건·실패 {fc['red']}건(직접 실행 "
                f"{fc['direct']}건·도구 경유 {fc['via-tool']}건·직접 "
                f"실행 안 됨 {fc['not-run']}건·CI에서만 실행 "
                f"{fc['ci-only']}건·확인 전 "
                f"{fc['unknown']}건·보관된 재시도 "
                f"{fc['archived_attempts']}건); 자동 묶음 "
                f"{auto['total']}개·보류 {len(auto['held'])}개·사람 "
                f"확인 대기 {auto['waiting_on_root_batches']}개 묶음"
                f"({auto['waiting_on_root_gates']}개 게이트); 중복 "
                f"건너뜀 {row['duplicate_skips']}건")
            for held in auto["held"]:
                lines.append(f"  보류: {held['id']} "
                             f"({held['by'] or missing}) — "
                             f"{held['reason'] or missing}")
    if en:
        lines.append(f"pushes by OpenMainty: "
                     f"{data.get('pushes_by_openmainty')} "
                     f"({data.get('push_basis')})")
        lines.append("Batch hold/waiting state is current, never "
                     "windowed.")
    else:
        lines.append(f"OpenMainty가 푸시한 횟수: "
                     f"{data.get('pushes_by_openmainty')}회 "
                     f"({data.get('push_basis')})")
        lines.append("묶음 보류/대기 상태는 항상 현재 기준이며, 기간 "
                     "필터와 무관합니다.")
    return "\n".join(lines) + "\n"
