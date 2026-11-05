"""Promoted-copy management: unattended jobs run only frozen trees.

`openmainty promote --manifest <root66-*-independent-full.json>` verifies
the whole dev tree against Root's independent-run `sources_sha256`,
copies it to ~/.openmainty/app/<sha>/, and flips the atomic `current`
symlink. Anything unverified refuses with exit 2 and touches nothing.

The `openmainty` launcher shim is not in Root's manifest, so promote
pins its sha in PROMOTED.json and refuses when it drifts from the
active promotion (first promote records it).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from . import paths

APP_DIR = "app"
CURRENT = "current"
PROMOTED = "PROMOTED.json"
HISTORY = "history.jsonl"
KEEP_DEFAULT = 3
SKIP_DIRS = ("__pycache__", ".git")
SKIP_SUFFIX = (".pyc",)
DIFF_WATCH = ("src/openmainty/serve.py", "src/openmainty/batches.py",
              "src/openmainty/sched.py", "src/openmainty/dashboard.html")
_DIFF_CACHE = {"at": 0.0, "value": None}


class PromoteError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def app_dir(home=None) -> Path:
    base = paths.home() if home is None else Path(home)
    return base / APP_DIR


def current_link(home=None) -> Path:
    return app_dir(home) / CURRENT


def dev_root() -> Path:
    """The checkout containing this file (dev tree or promoted copy)."""
    return Path(__file__).resolve().parent.parent.parent


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def app_info(home=None) -> dict:
    """The designated promotion (current symlink target), or {} when
    none. `running` says whether THIS process runs from it — tests run
    from the dev tree, so designation and execution are separate."""
    try:
        link = current_link(home)
        if not link.is_symlink():
            return {}
        target = link.resolve()
        base = app_dir(home)
        try:
            base_resolved = base.resolve()
        except OSError:
            return {}
        if target.parent != base_resolved or not target.is_dir():
            return {}
        if not (target / PROMOTED).is_file():
            return {}
        try:
            running = Path(__file__).resolve().is_relative_to(target)
        except OSError:
            running = False
        return {"sha": target.name, "path": str(target),
                "running": bool(running)}
    except OSError:
        pass
    return {}


def running_sha(home=None):
    """Sha of the promotion THIS process runs from, else None.

    A dev-tree tick after promote (but before the owner's plist switch)
    still reports None: it runs dev bytes, and the snapshot must say so."""
    info = app_info(home)
    if info.get("running"):
        return info.get("sha")
    return None


def app_start_line(home=None) -> str:
    """First daemon-tick log line (stdout -> maintenance.log)."""
    sha = running_sha(home)
    tag = (sha or "")[:12] if sha else "dev-tree"
    return f"app {tag} daemon-tick start"


def _manifest_paths(manifest: dict) -> dict:
    src = manifest.get("sources_sha256")
    if not isinstance(src, dict) or not src:
        raise PromoteError("manifest has no sources_sha256 map")
    return {str(k): str(v) for k, v in src.items()}


def verify_tree(root: Path, manifest: dict) -> list:
    """Every manifest path must match; no extra file may hide in src/.

    Returns the error list (empty = clean). Pure read-only check."""
    want = _manifest_paths(manifest)
    errors = []
    for rel, sha in sorted(want.items()):
        if rel.startswith("/") or ".." in Path(rel).parts:
            errors.append(f"unsafe manifest path: {rel}")
            continue
        live = root / rel
        if not live.is_file():
            errors.append(f"missing: {rel}")
            continue
        try:
            actual = _sha_file(live)
        except OSError as exc:
            errors.append(f"unreadable {rel}: {exc}")
            continue
        if actual != sha:
            errors.append(f"changed: {rel}")
    allowed = set(want)
    srcd = root / "src"
    if srcd.is_dir():
        for path in sorted(srcd.rglob("*")):
            if not path.is_file():
                continue
            if path.name == "__pycache__" or path.suffix == ".pyc":
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            rel = str(path.relative_to(root))
            if rel not in allowed:
                errors.append(f"unlisted src file: {rel}")
    if (root / "pyproject.toml").is_file() and "pyproject.toml" not in allowed:
        errors.append("unlisted file: pyproject.toml")
    # The `openmainty` launcher shim is not in Root's manifest; promote()
    # pins its sha in PROMOTED.json instead (see launcher_sha below).
    return errors


def _manifest_id(want: dict) -> str:
    h = hashlib.sha256()
    for rel in sorted(want):
        h.update(rel.encode())
        h.update(want[rel].encode())
    return h.hexdigest()[:16]


def _history(home) -> Path:
    return app_dir(home) / HISTORY


def _record(home, event: dict) -> None:
    try:
        with open(_history(home), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _switch(home, sha: str) -> None:
    """Atomically repoint current at app/<sha> (tmp link + replace)."""
    link = current_link(home)
    tmp = link.with_name(f"{CURRENT}.{os.getpid()}.tmp")
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(sha)
        os.replace(tmp, link)
    finally:
        try:
            if tmp.is_symlink():
                tmp.unlink()
        except OSError:
            pass


def promote(manifest: dict, root=None, home=None, at: str = "") -> dict:
    """Verify the whole tree, copy it to app/<sha>/, flip current.

    Raises PromoteError on any mismatch (writes nothing under app/)."""
    root = Path(root) if root else dev_root()
    want = _manifest_paths(manifest)
    errors = verify_tree(root, manifest)
    if errors:
        raise PromoteError("refused: " + "; ".join(errors[:8]))
    launcher = root / "openmainty"
    launcher_sha = _sha_file(launcher) if launcher.is_file() else ""
    link = current_link(home)
    if link.is_symlink():
        try:
            prev_prom = json.loads(
                (link.resolve() / PROMOTED).read_text(encoding="utf-8"))
            prev_launch = str(prev_prom.get("launcher_sha") or "")
            if prev_launch and launcher_sha and prev_launch != launcher_sha:
                raise PromoteError(
                    "refused: launcher changed since last promote")
        except OSError:
            pass
    sha = _manifest_id(want)
    dest = app_dir(home) / sha
    if dest.exists():
        return _repair_if_identical(home, dest, sha, want, launcher_sha,
                                    launcher, at)
    tmpd = app_dir(home) / f".copy-{os.getpid()}.tmp"
    try:
        if tmpd.exists():
            shutil.rmtree(tmpd)
        app_dir(home).mkdir(parents=True, exist_ok=True)
        # Allowlist copy: only manifest files (minus tests/, which never
        # run) plus the pinned launcher. Nothing unreviewed rides along.
        tmpd.mkdir(parents=True)
        for rel in sorted(want):
            if rel.split("/")[0] == "tests":
                continue
            src_file, dst_file = root / rel, tmpd / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_file, dst_file)
        if launcher.is_file():
            # copy2 keeps the mode bits; the chmod guarantees +x even
            # when the source checkout lost it. A non-executable
            # launcher would crash-loop the KeepAlive dashboard, so the
            # X_OK check below refuses instead of shipping it.
            shutil.copy2(launcher, tmpd / "openmainty")
            os.chmod(tmpd / "openmainty", 0o755)
            if not os.access(tmpd / "openmainty", os.X_OK):
                raise PromoteError("refused: promoted launcher not executable")
        (tmpd / PROMOTED).write_text(json.dumps(
            {"sha": sha, "at": at, "launcher_sha": launcher_sha,
             "source_tree": str(root), "manifest": want},
            ensure_ascii=False), encoding="utf-8")
        os.replace(tmpd, dest)
    except Exception:
        try:
            if tmpd.exists():
                shutil.rmtree(tmpd)
        except OSError:
            pass
        raise
    prev = ""
    try:
        if link.is_symlink():
            prev = link.resolve().name
    except OSError:
        prev = ""
    _switch(home, sha)
    _record(home, {"at": at, "event": "promote", "from": prev, "to": sha})
    return {"sha": sha, "path": str(dest), "previous": prev}


def _repair_if_identical(home, dest: Path, sha: str, want: dict,
                         launcher_sha: str, launcher: Path, at: str) -> dict:
    """Byte-identical re-promote: verify, fix ONLY the launcher mode,
    record a repair event. Any byte difference refuses (never overwrite
    a different tree behind the same sha)."""
    diffs = []
    for rel in sorted(want):
        if rel.split("/")[0] == "tests":
            continue
        have = dest / rel
        if not have.is_file():
            diffs.append(f"missing in copy: {rel}")
            continue
        try:
            if _sha_file(have) != want[rel]:
                diffs.append(f"differs in copy: {rel}")
        except OSError as exc:
            diffs.append(f"unreadable in copy {rel}: {exc}")
    have_launch = dest / "openmainty"
    if launcher.is_file():
        if not have_launch.is_file():
            diffs.append("missing in copy: openmainty")
        else:
            try:
                if _sha_file(have_launch) != launcher_sha:
                    diffs.append("differs in copy: openmainty")
            except OSError as exc:
                diffs.append(f"unreadable in copy openmainty: {exc}")
    if diffs:
        raise PromoteError(f"refused: app/{sha} exists and differs: "
                           + "; ".join(diffs[:8]))
    if have_launch.is_file():
        os.chmod(have_launch, 0o755)
        if not os.access(have_launch, os.X_OK):
            raise PromoteError("refused: repaired launcher not executable")
    prev = ""
    try:
        if current_link(home).is_symlink():
            prev = current_link(home).resolve().name
    except OSError:
        prev = ""
    _record(home, {"at": at, "event": "repair", "sha": sha})
    return {"sha": sha, "path": str(dest), "previous": prev,
            "repaired": True}


def _history_targets(home) -> list:
    try:
        lines = _history(home).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def rollback(sha: str = "", home=None, at: str = "") -> dict:
    """Repoint current at the previous (or named) sha. Data untouched."""
    link = current_link(home)
    if not link.is_symlink():
        raise PromoteError("refused: no current promotion to roll back")
    cur = link.resolve().name
    if not sha:
        prev = ""
        for event in reversed(_history_targets(home)):
            if event.get("event") in ("promote", "rollback") \
                    and event.get("to") == cur:
                prev = str(event.get("from") or "")
                break
        if not prev:
            raise PromoteError("refused: no previous promotion on record")
        sha = prev
    dest = app_dir(home) / sha
    if not dest.is_dir() or not (dest / PROMOTED).is_file():
        raise PromoteError(f"refused: app/{sha} is not a promotion")
    _switch(home, sha)
    _record(home, {"at": at, "event": "rollback", "from": cur, "to": sha})
    return {"sha": sha, "path": str(dest), "previous": cur}


def _promo_order(home, shas: list) -> list:
    """Oldest-first promotion order from history.jsonl (newest last).

    A sha's age is its promote event, never its hash: lexical order can
    put a minutes-old tree first. Dirs with no promote event count as
    oldest (removed first). Deterministic: unknown ties break by name."""
    seq = [str(e.get("to")) for e in _history_targets(home)
           if e.get("event") == "promote" and e.get("to")]
    rank = {}
    for sha in seq:
        rank.setdefault(sha, len(rank))
    return sorted(shas,
                  key=lambda s: (0, s) if s not in rank else (1, rank[s]))


def prune(home=None, keep: int = KEEP_DEFAULT, assume_yes: bool = False,
          at: str = "") -> dict:
    """Delete old promoted trees. Explicit --yes required; current and
    the rollback target are never deleted; nothing outside app/<sha>."""
    if not assume_yes:
        raise PromoteError("refused: prune needs --yes")
    adir = app_dir(home)
    link = current_link(home)
    cur = link.resolve().name if link.is_symlink() else ""
    rollback_target = ""
    for event in reversed(_history_targets(home)):
        if event.get("event") in ("promote", "rollback") \
                and event.get("to") == cur:
            rollback_target = str(event.get("from") or "")
            break
    shas = []
    try:
        shas = [p.name for p in adir.iterdir()
                if p.is_dir() and (p / PROMOTED).is_file()]
    except OSError:
        pass
    ordered = _promo_order(home, shas)
    keep_set = set(ordered[-keep:] if keep > 0 else [])
    keep_set |= {s for s in (cur, rollback_target) if s}
    removed = []
    for name in shas:
        if name in keep_set:
            continue
        try:
            shutil.rmtree(adir / name)
            removed.append(name)
        except OSError:
            pass
    _record(home, {"at": at, "event": "prune", "removed": removed,
                   "kept": sorted(keep_set)})
    return {"removed": removed, "kept": sorted(keep_set)}


def dev_differs(home=None, dev=None) -> bool | None:
    """Does the dev tree differ from the running promotion on the
    watched files? None when unpromoted or the source is unreadable.
    Cached 5 minutes; mtime pre-check before hashing."""
    global _DIFF_CACHE
    now = time.time()
    if now - _DIFF_CACHE["at"] < 300 and _DIFF_CACHE["value"] is not None:
        return _DIFF_CACHE["value"]
    result = _dev_differs_now(home, dev)
    _DIFF_CACHE = {"at": now, "value": result}
    return result


def _dev_differs_now(home=None, dev=None) -> bool | None:
    info = app_info(home)
    if not info:
        return None
    try:
        prom = json.loads((Path(info["path"]) / PROMOTED).read_text(
            encoding="utf-8"))
    except OSError:
        return None
    src = dev or prom.get("source_tree") or ""
    if not src:
        return None
    for rel in DIFF_WATCH:
        a, b = Path(src) / rel, Path(info["path"]) / rel
        try:
            if a.stat().st_mtime_ns != b.stat().st_mtime_ns:
                if _sha_file(a) != _sha_file(b):
                    return True
        except OSError:
            return None
    return False
