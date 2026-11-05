"""Dependency drift watch (public registries only, no keys, no model APIs).

Two sources, never invented:
1. Legacy declared entries in the profile (`dependencies` with pin_file/pin_key)
   keep working exactly as before.
2. Real manifest discovery (package.json incl. example/, pubspec.yaml) when
   the profile declares nothing. Discovered rows carry declared/installed/
   latest, a conservative classification, and a main/next lane.

A `latest_fn` hook lets tests and the scheduler inject a fake registry so
fixture runs never touch the real network.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .util import utcnow

TIMEOUT = 15

_latest_fn = None


def _emit(progress_fn, rows: list, row: dict) -> None:
    """Append a finished row; stream it to progress_fn (if any) right away.

    Registry checks run sequentially and each may stall up to TIMEOUT, so
    callers that only print after check() returns look wedged for minutes.
    Firing exactly once per returned row, in order, keeps them live.
    """
    rows.append(row)
    if progress_fn is not None:
        progress_fn(row)


def set_latest_fn(fn) -> None:
    global _latest_fn
    _latest_fn = fn


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "openmainty/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise LookupError(f"not published at {url}") from exc
        raise


def _latest_sdk(ident: str) -> str:
    """Latest stable toolchain from official release evidence (no keys).

    dart-sdk: Dart archive stable VERSION file (JSON with `version`).
    flutter: Flutter release index `current_release.stable` hash resolved
    against the `releases` list. Raises on network/format errors so the
    caller records an honest error row instead of a placeholder.
    """
    if ident == "dart-sdk":
        data = _get_json("https://storage.googleapis.com/storage/v1/b/"
                         "dart-archive/o/channels%2Fstable%2Frelease%2F"
                         "latest%2FVERSION?alt=media")
        ver = str(data.get("version", "")).strip()
        if not ver:
            raise ValueError("dart VERSION file has no version field")
        return ver
    if ident == "flutter":
        data = _get_json("https://storage.googleapis.com/flutter_infra_release/"
                         "releases/releases_linux.json")
        cur = (data.get("current_release") or {}).get("stable", "")
        for rel in data.get("releases", []) or []:
            if isinstance(rel, dict) and rel.get("hash") == cur:
                ver = str(rel.get("version", "")).strip()
                if ver:
                    return ver.lstrip("v")
        raise ValueError("flutter release index has no stable version")
    raise ValueError(f"unknown sdk: {ident}")


def latest(kind: str, ident: str) -> str:
    """Latest published version for kind+id. Raises on network/format errors."""
    if _latest_fn is not None:
        return _latest_fn(kind, ident)
    if kind == "sdk":
        return _latest_sdk(ident)
    if kind == "npm":
        data = _get_json(f"https://registry.npmjs.org/{ident}/latest")
        return data["version"]
    if kind == "pub":
        data = _get_json(f"https://pub.dev/api/packages/{ident}")
        return data["latest"]["version"]
    if kind == "maven":
        group, _, artifact = ident.partition(":")
        path = f"{group.replace('.', '/')}/{artifact}/maven-metadata.xml"
        try:
            req = urllib.request.Request(
                f"https://repo1.maven.org/maven2/{path}",
                headers={"User-Agent": "openmainty/0.1"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                xml_text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LookupError(f"maven: {ident} not published") from exc
            raise
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        latest_el = root.find("./versioning/latest")
        if latest_el is not None and latest_el.text:
            return latest_el.text.strip()
        versions = [v.text.strip() for v in root.findall("./versioning/versions/version") if v.text]
        if not versions:
            raise ValueError(f"maven: no versions in metadata for {ident}")
        return versions[-1]
    if kind == "github":
        data = _get_json(f"https://api.github.com/repos/{ident}/releases/latest")
        tag = data.get("tag_name", "")
        return tag.lstrip("v")
    raise ValueError(f"unknown dependency kind: {kind}")


def read_pin(checkout, entry: dict) -> str:
    import re
    path = checkout / entry["pin_file"]
    text = path.read_text(encoding="utf-8")
    key = entry.get("pin_key")
    if path.suffix == ".json":
        node = json.loads(text)
        for part in (key or "").split("."):
            if part:
                node = node[part]
    else:
        # minimal `key: value` scan for pubspec.yaml-style pin files
        node = None
        for line in text.splitlines():
            m = re.match(rf"^{re.escape(key or '')}\s*:\s*([^\s#]+)", line.strip())
            if m:
                node = m.group(1).strip("'\"")
                break
        if node is None:
            raise ValueError(f"pin key '{key}' not found in {entry.get('pin_file')}")
    if not isinstance(node, str):
        raise ValueError(f"pin {entry.get('pin_file')}:{key} is not a version string")
    return node


def _classify_row(declared, installed, latest_ver) -> dict:
    from . import branches, versions
    # `declared` is usually a RANGE (^1.2.3, >=a <b), never a version:
    # compare the installed lock first, else the range floor. A bare
    # range string must never enter update_kind lexically.
    current = installed or versions.floor_of_range(declared or "") or None
    if current and versions.parse(current) is None:
        current = None
    kind = versions.update_kind(current or "", latest_ver or "") \
        if current and latest_ver else "unknown"
    if not current or not latest_ver:
        kind = "unknown"
    info = branches.route("dep", current, latest_ver)
    # branches.route(None-branch) for unknown/downgrade: keep kind, no branch
    drift = False
    if kind not in ("none", "unknown"):
        drift = True
        if kind == "downgrade":
            drift = False  # risk, not drift: surfaced separately
    return {"update_kind": kind, "lane": info["lane"],
            "branch": info.get("branch"), "reason": info.get("reason", ""),
            "drift": drift, "compared": current}


def _discovered_rows(checkout, progress_fn=None) -> list:
    from . import branches, manifests
    disc = manifests.discover(checkout)
    rows = []
    for dep in disc["deps"]:
        lock_local = manifests.local_resolution_reason(
            dep.get("lock_resolution")) if dep.get("manager") == "npm" else None
        if lock_local and not dep.get("unmanaged"):
            # Resolver verdict beats declared text: the lock proves this
            # selector is workspace-local (e.g. example `*` resolving to
            # `workspace:.`), so it is never a registry package. Kept as
            # an unmanaged row with lock provenance — never queried,
            # never drift, never queued.
            _emit(progress_fn, rows, {
                "name": dep["name"], "kind": dep["manager"],
                "id": dep["name"],
                "scope": dep.get("scope"), "file": dep.get("file"),
                "declared": dep.get("declared"), "pinned": dep.get("installed"),
                "installed": dep.get("installed"), "latest": None,
                "drift": False, "unpublished": False, "error": None,
                "note": lock_local,
                "lock_resolution": dep.get("lock_resolution"),
                "update_kind": "unmanaged",
                "lane": "main", "branch": None,
                "reason": ("self-workspace/local source per lockfile; "
                           "reviewed by hand, not by version drift."),
                "checked_at": utcnow(), "source": "manifest:" + str(dep.get("file")),
            })
            continue
        if dep["name"] in ("flutter", "flutter_test") or dep.get("unmanaged"):
            _emit(progress_fn, rows, {
                "name": dep["name"], "kind": "sdk"
                if dep["name"] in ("flutter", "flutter_test") else dep["manager"],
                "id": dep["name"],
                "scope": dep.get("scope"), "file": dep.get("file"),
                "declared": dep.get("declared"), "pinned": dep.get("installed"),
                "installed": dep.get("installed"), "latest": None,
                "drift": False, "unpublished": False, "error": None,
                "note": ("SDK-pinned dependency; toolchain constraint, not a versioned package"
                         if dep["name"] in ("flutter", "flutter_test")
                         else f"unmanaged spec ({dep.get('unmanaged')}); "
                              "never sent to a registry, never drift"),
                "update_kind": "sdk" if dep["name"] in ("flutter", "flutter_test") else "unmanaged",
                "lane": "main", "branch": None,
                "reason": ("SDK bindings follow the installed toolchain, not the package registry."
                           if dep["name"] in ("flutter", "flutter_test")
                           else "local/workspace/git specs are reviewed by hand, not by version drift."),
                "checked_at": utcnow(), "source": "manifest:" + str(dep.get("file")),
            })
            continue
        manager = dep["manager"]
        row = {"name": dep["name"], "kind": manager, "id": dep["name"],
               "scope": dep.get("scope"), "file": dep.get("file"),
               "declared": dep.get("declared"), "pinned": dep.get("installed") or dep.get("declared"),
               "installed": dep.get("installed"), "latest": None,
               "drift": False, "unpublished": False, "error": None,
               "lock_file": dep.get("lock_file"),
               "lock_tracked": dep.get("lock_tracked"),
               "checked_at": utcnow(), "source": "manifest:" + str(dep.get("file"))}
        try:
            row["latest"] = latest(manager, dep["name"])
        except LookupError as exc:
            row["unpublished"] = True
            row["note"] = str(exc)
            row["update_kind"] = "unknown"
            row["lane"] = "main"
            row["reason"] = f"registry has no release info ({exc}); not drift."
            _emit(progress_fn, rows, row)
            continue
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"registry check failed: {exc}"
            row["update_kind"] = "unknown"
            row["lane"] = "main"
            row["reason"] = f"could not read registry: {exc}; not drift."
            _emit(progress_fn, rows, row)
            continue
        cls = _classify_row(dep.get("declared"), dep.get("installed"), row["latest"])
        row.update({k: cls[k] for k in ("update_kind", "lane", "reason") if k in cls})
        row["compared"] = cls.get("compared")
        if cls.get("branch") or cls["lane"]:
            row["branch"] = branches.slugify_topic(f"{dep['name']}-{row['latest']}")
            row["branch"] = (f"codex/{'maint' if cls['lane'] == 'main' else 'next'}/"
                             f"{row['branch']}") if cls["update_kind"] not in ("unknown",) else None
        else:
            row["branch"] = None
        row["drift"] = cls["drift"]
        # A declared range already covering latest is not drift even if a
        # newer release exists (e.g. ^0.86.2 vs 0.86.2 installed).
        from . import versions as _v
        if row["drift"] and dep.get("declared"):
            sat = _v.satisfies(dep["declared"], row["latest"])
            if sat is True and cls["update_kind"] in ("patch",):
                pass  # patch inside range still worth a bump row; keep drift
            elif sat is True and row.get("installed") == row["latest"]:
                row["drift"] = False
                row["note"] = "installed latest already satisfies declared range"
            elif sat is True and _v.satisfies(dep["declared"], dep.get("installed") or "") is True:
                # Both installed and latest satisfy the declared range and the
                # move is more than a patch: the manifest already permits it
                # (e.g. `flutter pub upgrade` territory). Legacy drift stays
                # False, but the row is explicitly actionable as a lock
                # refresh: the user asked for continued upgrades and a stale
                # lock must yield a real proposal, not vanish as inventory.
                from . import branches as _br
                info = _br.route(dep["name"], dep.get("installed"),
                                 row["latest"])
                row["drift"] = False
                row["outdated"] = True
                row["update_kind"] = "lock-refresh"
                row["lane"] = info.get("lane", "next")
                if info.get("branch"):
                    row["branch"] = info["branch"]
                row["note"] = "declared range already admits latest; lock refresh only"
                row["reason"] = (f"lock pins {dep.get('installed')} but declared "
                                 f"{dep.get('declared')} admits latest {row['latest']}; "
                                 f"refresh the lock on the {row['lane']} lane. "
                                 + (info.get("reason", "") or ""))
        ignored_note = _ignored_pub_lock_note(dep, row)
        if ignored_note is not None:
            row["drift"] = False
            row.pop("outdated", None)
            row["update_kind"] = "none"
            row["lane"] = "main"
            row["branch"] = None
            row["compared"] = row["latest"]
            row["note"] = ignored_note
            row["reason"] = ignored_note
        _emit(progress_fn, rows, row)
    for sdk in disc["sdks"]:
        row = {"name": sdk["name"], "kind": "sdk", "id": sdk["name"],
               "scope": "environment", "file": sdk.get("file"),
               "declared": sdk.get("constraint"), "pinned": sdk.get("constraint"),
               "installed": None, "latest": None, "drift": False,
               "unpublished": False, "error": None,
               "update_kind": "sdk", "lane": "main", "branch": None,
               "checked_at": utcnow(), "source": "manifest:" + str(sdk.get("file"))}
        # Real toolchain compatibility: compare the declared SDK constraint
        # against the official stable release. Failures stay visible as
        # errors, never as silent "no changes".
        try:
            row["latest"] = latest("sdk", sdk["name"])
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"sdk release check failed: {exc}"
            row["note"] = "toolchain latest unavailable; constraint kept, not drift."
            row["reason"] = row["note"]
            _emit(progress_fn, rows, row)
            continue
        from . import versions as _vsdk
        sat = _vsdk.satisfies(sdk.get("constraint") or "", row["latest"])
        if sat is True:
            row["note"] = (f"declared {sdk.get('constraint')} admits "
                           f"stable {row['latest']}")
            row["reason"] = row["note"]
        elif sat is False:
            row["drift"] = True
            row["lane"] = "next"
            row["update_kind"] = "sdk-major"
            row["branch"] = (f"codex/next/"
                             f"{branches.slugify_topic(sdk['name'] + '-' + row['latest'])}")
            row["reason"] = (f"declared {sdk.get('constraint')} does NOT admit "
                             f"stable {row['latest']}; toolchain review on next lane.")
        else:
            row["note"] = (f"constraint {sdk.get('constraint')} not machine-readable; "
                           "kept for hand review, not drift.")
            row["reason"] = row["note"]
        _emit(progress_fn, rows, row)
    _apply_peer_conflicts(rows)
    for warn in disc["warnings"]:
        _emit(progress_fn, rows, {"name": "(manifest)", "kind": "note", "id": "(manifest)",
                                  "declared": None, "pinned": None, "installed": None,
                                  "latest": None, "drift": False, "unpublished": False,
                                  "error": None, "note": warn, "update_kind": "note",
                                  "lane": "main", "branch": None, "reason": warn,
                                  "checked_at": utcnow(), "source": "manifest"})
    if not rows and disc.get("sources"):
        _emit(progress_fn, rows, {"name": "(discovery)", "kind": "note", "id": "(discovery)",
                                  "declared": None, "pinned": None, "installed": None,
                                  "latest": None, "drift": False, "unpublished": False,
                                  "error": ("manifests found but no dependencies discovered "
                                            f"({', '.join(disc['sources'])}); refusing silent 0"),
                                  "update_kind": "note", "lane": "main", "branch": None,
                                  "reason": "empty discovery on manifest-bearing checkout",
                                  "checked_at": utcnow(), "source": "manifest"})
    return rows


def _ignored_pub_lock_note(dep: dict, row: dict) -> str | None:
    """Non-None when this pub row's lag is ignored-dev-lock-only noise.

    A git-ignored lock (e.g. a library root pubspec.lock kept out of the
    repo by .gitignore) pinning an older version while the declared range
    already admits the registry latest is NOT a source job: no tracked
    file can change, so no reviewable upgrade exists. The row keeps its
    latest + provenance and is explicitly marked non-actionable; genuine
    outdated TRACKED locks, outside-range constraints, unknown provenance,
    and non-pub managers all stay on their existing paths.
    """
    if dep.get("manager") != "pub" or dep.get("unmanaged"):
        return None
    if row.get("update_kind") in ("unknown",):
        return None
    if row.get("lock_tracked") is not False:
        return None
    latest = row.get("latest")
    if not latest:
        return None
    from . import versions as _v
    try:
        admits = _v.satisfies(dep.get("declared") or "", latest)
    except Exception:  # noqa: BLE001 - unprovable, stays actionable
        return None
    if admits is not True:
        return None
    if (dep.get("installed") or "") == latest:
        return None  # already current; the existing none-path owns it
    lock = dep.get("lock_file") or "pubspec.lock"
    return (f"ignored dev lock {lock} pins {dep.get('installed')} but declared "
            f"{dep.get('declared')} admits latest {latest}; refresh locally "
            f"with pub get — not a source job. Latest {latest} shown "
            f"for provenance.")


def _apply_peer_conflicts(rows: list) -> None:
    """Linked-peer evidence: a peer range that rejects the registry latest
    forces the row onto the next lane (never an independent upgrade).

    Mutates rows in place; records the conflicting peer range in the
    reason so the update task carries the evidence. Compatibility is
    still proven only by the repo checks, never by this text."""
    from . import versions as _v
    peers: dict = {}
    for r in rows:
        scope = str(r.get("scope") or "")
        if "peer" in scope.lower() and r.get("declared"):
            peers.setdefault(r["name"], []).append(str(r["declared"]))
    if not peers:
        return
    for r in rows:
        scope = str(r.get("scope") or "")
        if "peer" in scope.lower() or not r.get("drift") or not r.get("latest"):
            continue
        for prange in peers.get(r["name"], []):
            try:
                sat = _v.satisfies(prange, r["latest"])
            except Exception:  # noqa: BLE001
                sat = None
            if sat is False:
                r["lane"] = "next"
                r["reason"] = ((r.get("reason") or "")
                               + f" Linked peer range {prange} does NOT admit "
                               f"{r['latest']}; coupled upgrade stays on next "
                               "until checks prove compatibility.")
                break


def check(profile: dict, checkout, progress_fn=None) -> list:
    """Return [{name, kind, id, pinned, latest, drift, error}]. Never raises.

    progress_fn(row) fires exactly once per returned row, in order, as each
    row completes — so sequential registry stalls stream instead of hanging
    silently until the whole check finishes.
    """
    results = []
    declared = profile.get("dependencies") or []
    if not declared:
        try:
            return _discovered_rows(checkout, progress_fn=progress_fn)
        except Exception as exc:  # noqa: BLE001
            row = {"name": "(discovery)", "kind": "note", "id": "(discovery)",
                   "pinned": None, "latest": None, "drift": False,
                   "unpublished": False, "error": f"manifest discovery failed: {exc}",
                   "checked_at": utcnow(), "source": "manifest"}
            _emit(progress_fn, results, row)
            return results
    for entry in declared:
        row = {"name": entry.get("name"), "kind": entry.get("kind"),
               "id": entry.get("id"), "pinned": None, "latest": None,
               "drift": False, "unpublished": False, "error": None,
               "checked_at": utcnow(), "source": "profile"}
        try:
            row["pinned"] = read_pin(checkout, entry)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            row["error"] = f"pin read failed: {exc}"
            _emit(progress_fn, results, row)
            continue
        try:
            row["latest"] = latest(entry["kind"], entry["id"])
        except LookupError as exc:
            row["unpublished"] = True
            row["error"] = None
            row["note"] = str(exc)
            _emit(progress_fn, results, row)
            continue
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"registry check failed: {exc}"
            _emit(progress_fn, results, row)
            continue
        row["drift"] = (row["pinned"] != row["latest"])
        _emit(progress_fn, results, row)
    return results


def summarize(rows: list) -> dict:
    """Truthful counts: only real dependencies count as checked.

    Manifest/discovery notes (`kind == "note"`, `(manifest)` rows) are
    warnings, not successful checks; registry failures are errors. A
    checkout with no supported manifest reports checked=0 with a
    visible warning — never a silent "no drift".
    """
    real = [r for r in rows
            if r.get("kind") != "note" and r.get("name") not in
            ("(manifest)", "(discovery)")]
    notes = [r for r in rows if r not in real]
    errors = [r for r in real if r.get("error")]
    return {"checked": len(real),
            "warnings": len(notes),
            "warning_notes": [n.get("note") or n.get("reason") or ""
                              for n in notes],
            "errors": len(errors),
            "unsupported": not real}


def drift_body(row: dict) -> str:
    lane = row.get("lane")
    extra = f" Suggested lane: {lane}." if lane else ""
    return (
        f"Dependency drift: {row['name']} ({row['kind']}:{row['id']}) "
        f"pinned at {row.get('pinned') or row.get('declared')}, registry latest is {row['latest']}."
        + extra +
        " Update the pin, mirrors (docs/tables), and any lockfiles; "
        "verify with the repo's checks before drafting release notes."
    )
