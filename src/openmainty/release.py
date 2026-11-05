"""Owner self-run kit: release / rollback (M3.1).

The owner updates and rolls back OpenMainty alone — no Claude needed.
All owner-run, all Korean output, all refuse-first. No publishing,
merging, registry-uploading or commenting paths anywhere in this
slice (the word-boundary grep invariant in tests/test_release.py
covers this file). Python 3.9 portable.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# The Py3.9 subset, versioned here so `release` runs exactly what Root
# runs independently (S16b 36-module list).
PY39_MODULES = [
    "tests.test_work_report", "tests.test_source_read_failure",
    "tests.test_source_identity", "tests.test_commit_repair",
    "tests.test_runner_ask", "tests.test_runner_workdir",
    "tests.test_batches", "tests.test_batches_api",
    "tests.test_current_qa", "tests.test_integration_prepare",
    "tests.test_work_ui_logic", "tests.test_serve",
    "tests.test_watchman_cookie", "tests.test_readiness_diag",
    "tests.test_readiness_gate66", "tests.test_s1_ui_backlog",
    "tests.test_prep_lane_ui", "tests.test_work_sidebar_init",
    "tests.test_prepare_multicommit", "tests.test_prepare_lockfix",
    "tests.test_s1b_ui_followups", "tests.test_sched_due",
    "tests.test_rollup", "tests.test_gc", "tests.test_offthread",
    "tests.test_fastcheck", "tests.test_m2_driver", "tests.test_m2",
    "tests.test_repo_profile", "tests.test_api_origin",
    "tests.test_m2_rollup", "tests.test_m2_batch", "tests.test_m1_status",
    "tests.test_batches_snapshot", "tests.test_promote",
    "tests.test_sched_refresh",
]

APP_VERIFY_WAIT_S = 20


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_sources(root) -> dict:
    """Hash exactly what promote copies (Root review change 1).

    All of src/** (minus __pycache__), tests/**/*.py and
    pyproject.toml — the same set as Root's sources_sha256, keyed by
    repo-relative path. promote verifies the COPIED bytes against
    this manifest, so anything changing between the snapshot and the
    copy refuses there too."""
    root = Path(root)
    files = sorted(
        [p for p in (root / "src").rglob("*")
         if p.is_file() and "__pycache__" not in p.parts]
        + [p for p in (root / "tests").rglob("*.py")
           if "__pycache__" not in p.parts]
        + ([root / "pyproject.toml"]
           if (root / "pyproject.toml").is_file() else []))
    out = {}
    for path in files:
        try:
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
        except OSError:
            out[str(path.relative_to(root))] = "unreadable"
    return out


def _timeline(kind: str, title: str, detail: str = "") -> None:
    """One Korean timeline event under every registered repo, so the
    glance and the digest show owner-run updates and cleanups."""
    try:
        from . import events as _events
        from . import repos as _repos
        names = _repos.list_all()
    except Exception:  # noqa: BLE001 - event never fails the command
        return
    for name in names or []:
        try:
            _events.append(name, kind, "", title, detail, actor="owner")
        except Exception:  # noqa: BLE001 - best-effort per repo
            continue


def _run_suite(cmd: list, home: str, cwd: str) -> tuple:
    """Run one suite in a fresh temp HOME (argv list, no shell)."""
    import os as _os
    env = dict(_os.environ, OPENMAINTY_HOME=str(home))
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=3600,
                              text=True)
        return proc.returncode, proc.stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"suite start failed: {exc}"


def _dashboard_port() -> int:
    """Dashboard port from its plist ProgramArguments (--port), else
    8901. Read-only plistlib parse, never writes."""
    try:
        import plistlib as _plist
        data = _plist.loads(
            (Path.home() / "Library/LaunchAgents" /
             "dev.openmainty.dashboard.plist").read_bytes())
    except Exception:  # noqa: BLE001 - missing plist means default
        return 8901
    try:
        argv = list(data.get("ProgramArguments") or [])
        i = argv.index("--port")
        return int(argv[i + 1])
    except (ValueError, IndexError):
        return 8901


def _wait_app_sha(port: int, sha: str,
                  timeout_s: int = APP_VERIFY_WAIT_S) -> bool:
    """Poll GET /api/app until app_sha == sha (bounded, ~20 s)."""
    import urllib.request as _url
    deadline = time.time() + max(1, timeout_s)
    want = (sha or "")[:12]
    while time.time() < deadline:
        try:
            with _url.urlopen(
                    f"http://127.0.0.1:{port}/api/app",
                    timeout=5) as resp:
                body = json.loads(resp.read().decode() or "{}")
            if str(body.get("app_sha") or "")[:12] == want and want:
                return True
        except Exception:  # noqa: BLE001 - restarting; keep polling
            pass
        time.sleep(1)
    return False


def _kickstart_dashboard(_run=None) -> tuple:
    """launchctl kickstart the dashboard; returns (rc, output)."""
    run = _run if _run is not None else subprocess.run
    try:
        proc = run(["launchctl", "kickstart", "-k",
                    f"gui/{os.getuid()}/dev.openmainty.dashboard"],
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   timeout=60, text=True)
        return proc.returncode, proc.stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"kickstart start failed: {exc}"


def release(dry_run: bool = False, home=None, root=None,
            _minute=None, _runner=None, _kick=None,
            _wait=None, _sleep=None,
            _py39_path="/usr/bin/python3") -> dict:
    """Run the release pipeline (raises ReleaseError on refusal)."""
    from . import gc as _gc
    from . import paths as _paths
    from . import promote as _promote
    home = Path(home) if home is not None else _paths.home()
    root = Path(root) if root is not None else _promote.dev_root()
    idle = _gc.idle_refusals(home, _minute=_minute)
    if idle:
        raise ReleaseError("; ".join(idle))
    if dry_run:
        return {"dry_run": True,
                "steps": ["FULL (temp HOME)", "Py3.9 subset",
                          "promote --manifest", "kickstart",
                          "/api/app verify"],
                "home": str(home), "root": str(root)}
    run = _runner if _runner is not None else _run_suite
    before = snapshot_sources(root)
    tmp_home = Path(tempfile.mkdtemp(prefix="openmainty-release-"))
    full_cmd = [sys.executable, "-m", "unittest", "discover",
                "-s", "tests"]
    full_rc, full_log = run(full_cmd, str(tmp_home), str(root))
    full_n = _ran_count(full_log)
    py39_rc, py39_log, py39_n, py39_skipped = 0, "", 0, ""
    if Path(_py39_path).is_file():
        py39_rc, py39_log = run(
            [_py39_path, "-m", "unittest"] + PY39_MODULES,
            str(tmp_home), str(root))
        py39_n = _ran_count(py39_log)
    else:
        py39_skipped = (f"{_py39_path} 없음 — Py3.9 검사를 건너뜀 "
                        "(FULL만으로 출시)")
    after = snapshot_sources(root)
    changed = [k for k in after if before.get(k) != after.get(k)]
    rel_dir = home / "releases"
    try:
        rel_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if full_rc != 0:
        _write_log(rel_dir, "full.log", full_log)
        raise ReleaseError(
            f"FULL 실패 (rc={full_rc}) — 출시 안 함. 로그: "
            f"{rel_dir / 'full.log'}")
    if py39_rc != 0:
        _write_log(rel_dir, "py39.log", py39_log)
        raise ReleaseError(
            f"Py3.9 실패 (rc={py39_rc}) — 출시 안 함. 로그: "
            f"{rel_dir / 'py39.log'}")
    if changed:
        raise ReleaseError(
            "실행 중 원본 변경 (" + ", ".join(changed[:5]) + ") — "
            "출시 안 함")
    # F2: ~14 minutes of suites ran since the start gate —
    # re-check right before promote. If busy, poll every 15 s for
    # up to 10 minutes; if still busy, refuse WITHOUT promoting
    # (the tests passed, so say exactly that).
    _wait_idle(home, _minute=_minute, _sleep=_sleep)
    manifest = {"sources_sha256": after}
    try:
        out = _promote.promote(manifest, root=root, home=home,
                               at=_utcnow())
    except _promote.PromoteError as exc:
        raise ReleaseError(f"승격 거부: {exc.message}") from exc
    finally:
        # The suite HOME served its purpose (logs are copied under
        # releases/): remove it so one release leaves no tmp weight.
        # A red suite keeps its tmp dir for diagnosis.
        if full_rc == 0 and py39_rc == 0 and not changed:
            import shutil as _shutil
            _shutil.rmtree(tmp_home, ignore_errors=True)
    sha = str(out.get("sha") or "")
    _write_log(home / "releases" / sha, "full.log", full_log)
    if py39_log:
        _write_log(home / "releases" / sha, "py39.log", py39_log)
    kick = _kick if _kick is not None else _kickstart_dashboard
    krc, kout = kick()
    wait = _wait if _wait is not None else _wait_app_sha
    port = _dashboard_port()
    verified = wait(port, sha) if krc == 0 else False
    title = f"앱 업데이트 {sha[:12]} · FULL {full_n}건 OK"
    if py39_skipped:
        title += " (Py3.9 건너뜀)"
    elif py39_log:
        title += f" · Py3.9 {py39_n}건 OK"
    _timeline("app-release", title,
              "" if krc == 0 and verified
              else f"대시보드 재시작 확인 실패: {kout[:200]}")
    result = {"sha": sha, "full": full_n, "py39": py39_n,
              "py39_skipped": py39_skipped, "kickstart_rc": krc,
              "verified": bool(verified)}
    if krc != 0 or not verified:
        # Truthful text (Root review 3): current IS the new copy —
        # never claim the old sha was kept, and never roll back
        # automatically.
        result["warning"] = (
            "새 사본으로 전환됨 · 대시보드 재시작 실패 — "
            "`openmainty rollback --yes` 또는 kickstart 재시도")
    return result


def _wait_idle(home, _minute=None, _sleep=None) -> None:
    """Re-check the idle gate right before promote (F2).

    Polls gc.idle_refusals every 15 s for up to 10 minutes (40
    polls). Still busy afterwards refuses with the exact sentence
    and never promotes. Raises ReleaseError."""
    from . import gc as _gc
    sleep = _sleep if _sleep is not None else time.sleep
    reasons = []
    for _ in range(40):
        reasons = _gc.idle_refusals(home, _minute=_minute)
        if not reasons:
            return
        sleep(15)
    reasons = _gc.idle_refusals(home, _minute=_minute)
    if reasons:
        raise ReleaseError(
            "검사는 통과 — 작업이 끝난 뒤 다시 실행하세요 (승격 안 함)"
            + ": " + "; ".join(reasons))


def _ran_count(log_text: str) -> int:
    for line in (log_text or "").splitlines():
        if line.startswith("Ran ") and " tests" in line:
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                continue
    return 0


def _write_log(d: Path, name: str, text: str) -> None:
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text or "", encoding="utf-8")
    except OSError:
        pass


def rollback(to: str = "", yes: bool = False, home=None,
             _kick=None, _wait=None, _minute=None) -> dict:
    """Repoint current at the previous (or named) sha (needs --yes)."""
    from . import gc as _gc
    from . import paths as _paths
    from . import promote as _promote
    if not yes:
        raise ReleaseError(
            "롤백은 대시보드를 재시작합니다 — `--yes`로 확인하세요")
    home = Path(home) if home is not None else _paths.home()
    idle = _gc.idle_refusals(home, _minute=_minute)
    if idle:
        raise ReleaseError("; ".join(idle))
    try:
        out = _promote.rollback(to or "", home=home, at=_utcnow())
    except _promote.PromoteError as exc:
        raise ReleaseError(f"롤백 거부: {exc.message}") from exc
    sha = str(out.get("sha") or "")
    kick = _kick if _kick is not None else _kickstart_dashboard
    krc, kout = kick()
    wait = _wait if _wait is not None else _wait_app_sha
    port = _dashboard_port()
    verified = wait(port, sha) if krc == 0 else False
    _timeline("app-release", f"앱 롤백 {sha[:12]}",
              "" if krc == 0 and verified
              else f"대시보드 재시작 확인 실패: {kout[:200]}")
    result = {"sha": sha, "previous": str(out.get("previous") or ""),
              "kickstart_rc": krc, "verified": bool(verified)}
    if krc != 0 or not verified:
        result["warning"] = (
            "사본은 전환됨 · 대시보드 재시작 실패 — kickstart 재시도")
    return result


class ReleaseError(Exception):
    """Owner-visible release/rollback refusal (message is Korean)."""
