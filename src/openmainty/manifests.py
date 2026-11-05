"""Real manifest discovery for the registered targets (stdlib only).

npm: package.json (+ example/package.json where relevant), installed pins
from yarn.lock (v1 + berry) with package-lock.json fallback.
pub: pubspec.yaml declared ranges + pubspec.lock installed pins +
environment sdk/flutter constraints.

Never invents versions. Missing files/registries are reported as
unsupported/unavailable, never as "no changes".
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _split_key_name_selector(key: str) -> tuple:
    """('name', 'selector') for v1 (`name@range`) and berry (`name@npm:range`).

    Scoped packages keep their scope: `@scope/pkg@npm:^1.0.0` ->
    (`@scope/pkg`, `^1.0.0`). Keys without a selector yield ('name', '').
    """
    key = key.strip().strip('":')
    if "@npm:" in key:
        name, _, selector = key.partition("@npm:")
        return name.strip().strip('"'), selector.strip().strip('":')
    head = key.strip().strip('":')
    if head.startswith("@"):
        at = head.find("@", 1)
        if at > 0:
            return head[:at], head[at + 1:].strip().strip('":')
        return head, ""
    if "@" in head:
        name, _, selector = head.rpartition("@")
        return name, selector.strip().strip('":')
    return head, ""


def _parse_yarn_lock(text: str) -> dict:
    """Map bare package name -> locked version. Best effort, both formats.

    Returns {"by_name": {name: version (first seen)},
             "by_selector": {(name, selector): version},
             "by_resolution": {(name, selector): lock resolution string}}
    so callers can resolve the exact manifest selector instead of the
    first duplicate. Berry `resolution: "pkg@npm:1.2.3"` lines pin the
    exact version per selector and are preferred over the shared
    `version:` field. The raw resolution is kept because it — not the
    declared text — proves workspace/local identity
    (`pkg@workspace:.`, `file:../x`, ...): a `*` range resolving to a
    workspace is self-source, while a `*` resolving to `npm:x.y.z` is
    an ordinary external package.
    """
    by_name: dict = {}
    by_selector: dict = {}
    by_resolution: dict = {}
    # v1: `"name@range":\n  version "x.y.z"` ; berry: `name@npm:range:\n  version: x.y.z`
    chunks = re.split(r"\n(?=\S)", text)
    for chunk in chunks:
        lines = chunk.splitlines()
        if not lines:
            continue
        head = lines[0]
        keys = [k.strip().strip('"') for k in head.rstrip(":").split(",")]
        m = re.search(r'(?m)^\s*version\s*:?\s+["\']?([0-9][^\s"\']*)', chunk)
        if not m:
            continue
        ver = m.group(1).strip('"').strip("'")
        rm = re.search(r'(?m)^\s*resolution\s*:?\s+["\']?([^\s"\']+)', chunk)
        resolution = rm.group(1).strip('"').strip("'") if rm else ""
        for key in keys:
            name, selector = _split_key_name_selector(key)
            if not name:
                continue
            exact = ver
            if resolution:
                rn, _, rsel = resolution.partition("@npm:")
                rsel = (rsel or "").strip()
                if not rsel and "@" in rn:
                    _, _, rsel = rn.rpartition("@")
                if (rn.strip().strip('"') == name or not rn.strip()) and rsel:
                    exact = rsel
                elif "@npm:" in resolution and name in resolution:
                    mm = re.search(r"(\d+\.\d+\.\d+[^:\s\"']*)", resolution)
                    if mm:
                        exact = mm.group(1)
            if selector:
                by_selector.setdefault((name, selector), exact)
            if resolution:
                by_resolution.setdefault((name, selector), resolution)
            by_name.setdefault(name, exact)
    return {"by_name": by_name, "by_selector": by_selector,
            "by_resolution": by_resolution}


def _npm_installed(checkout: Path) -> dict:
    """Return {"by_name": ..., "by_selector": ..., "by_resolution": ...}."""
    empty = {"by_name": {}, "by_selector": {}, "by_resolution": {}}
    lock = checkout / "yarn.lock"
    if lock.is_file():
        try:
            return _parse_yarn_lock(lock.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return empty
    plock = checkout / "package-lock.json"
    data = _read_json(plock) if plock.is_file() else None
    if isinstance(data, dict):
        by_name, by_selector, by_resolution = {}, {}, {}
        for key, val in (data.get("packages", {}) or {}).items():
            if not key.startswith("node_modules/"):
                continue
            if not isinstance(val, dict):
                continue
            # `node_modules/@scope/pkg` stays scoped; nested copies
            # (`node_modules/a/node_modules/b`) resolve to the leaf name.
            rest = key[len("node_modules/"):]
            leaf = rest.split("/node_modules/")[-1]
            if val.get("version"):
                by_name.setdefault(leaf, str(val["version"]))
            # A workspace symlink (`"link": true`) is self-source, not a
            # registry install — record it as a local resolution.
            if val.get("link") is True:
                by_resolution.setdefault(
                    (leaf, ""),
                    f"link:{val.get('resolved') or key}")
        return {"by_name": by_name, "by_selector": by_selector,
                "by_resolution": by_resolution}
    return empty


def installed_for(installed: dict, name: str, declared: str | None) -> str | None:
    """Locked version for one manifest selector; None when unavailable."""
    if not isinstance(installed, dict):
        return None
    by_sel = installed.get("by_selector") or {}
    by_name = installed.get("by_name") or {}
    if declared:
        hit = by_sel.get((name, declared.strip()))
        if hit:
            return hit
    # Legacy shape tolerance: a plain {name: version} map.
    if name in installed and isinstance(installed[name], str):
        return installed[name]
    return by_name.get(name)


def resolution_for(installed: dict, name: str,
                   declared: str | None) -> str | None:
    """Raw lock resolution for one manifest selector; None when unknown.

    Mirrors `installed_for`: the exact (name, declared) selector wins so
    two contexts declaring different selectors for the same package keep
    their own identity; falls back to the bare-name entry (package-lock
    `link:` records). A workspace resolution never leaks across
    selectors — an `npm:` selector resolving to the registry stays
    external even when a sibling selector resolves locally.
    """
    if not isinstance(installed, dict):
        return None
    by_res = installed.get("by_resolution") or {}
    if declared:
        hit = by_res.get((name, declared.strip()))
        if hit:
            return hit
    return by_res.get((name, ""))


_LOCAL_RESOLUTION_RE = re.compile(
    r"(?:^|\s)(?:workspace|file|link|portal|patch)\s*:", re.IGNORECASE)
_LOCAL_AT_RESOLUTION_RE = re.compile(
    r"@(?:workspace|file|link|portal|patch)\s*:", re.IGNORECASE)


def local_resolution_reason(resolution: str | None) -> str | None:
    """Non-None when the LOCK resolution proves workspace/local identity.

    This is the resolver's verdict, not the declared text: only the lock
    knows that `*` means `workspace:.` here and `npm:1.2.3` there. A bare
    `*` (or any range) resolving through the registry is NOT local — do
    not treat every wildcard as self-source.
    """
    text = (resolution or "").strip().strip("\"'")
    if not text:
        return None
    if _LOCAL_RESOLUTION_RE.search(text) or _LOCAL_AT_RESOLUTION_RE.search(text):
        return (f"lock resolves to local source ({text}); self-workspace "
                "identity, never a registry upgrade")
    return None


_UNMANAGED_PREFIXES = ("file:", "link:", "workspace:", "portal:", "patch:",
                       "git+", "git:", "http://", "https://", "github:",
                       "bitbucket:", "gitlab:", "npm:")


def unmanaged_reason(declared: str | None) -> str | None:
    """Non-None when `declared` is not a registry version range.

    Such specs (local path, workspace, git URL, portal) must never be
    sent to a package registry as ordinary external packages.
    """
    text = (declared or "").strip()
    if not text:
        return "empty declaration"
    if text.startswith("{") or "sdk:" in text or "(sdk)" in text or text.startswith("path:"):
        return "local/sdk/path spec, not a registry range"
    if "/" in text.split()[0] and not re.match(r"^[~^<>=]", text) and "@" not in text.split()[0]:
        # `owner/repo`, `owner/repo#ref`: git shorthand, not a range.
        if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
            return "git shorthand spec, not a registry range"
    lowered = text.lower()
    for prefix in _UNMANAGED_PREFIXES:
        if lowered.startswith(prefix):
            return f"{prefix.rstrip(':')} spec, not a registry range"
    return None


def _npm_from_file(pkg_file: Path, installed: dict, scope_prefix: str) -> list:
    data = _read_json(pkg_file)
    if not isinstance(data, dict):
        return []
    rows = []
    for scope in ("dependencies", "devDependencies", "peerDependencies"):
        group = data.get(scope) or {}
        if not isinstance(group, dict):
            continue
        for name, declared in group.items():
            if not isinstance(declared, str):
                declared = str(declared)
            rows.append({
                "manager": "npm",
                "scope": scope if not scope_prefix else f"{scope_prefix}:{scope}",
                "file": str(pkg_file.name if not scope_prefix else f"{scope_prefix}/{pkg_file.name}"),
                "name": name,
                "declared": declared,
                "installed": installed_for(installed, name, declared),
                "unmanaged": unmanaged_reason(declared),
                "lock_resolution": resolution_for(installed, name, declared),
            })
    return rows


def _pub_value(lines: list, key: str):
    for line in lines:
        m = re.match(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*(?:#.*)?$", line)
        if m:
            return m.group(1).strip().strip("'\"")
    return None


def _pub_section(lines: list, section: str) -> dict:
    """Parse top-level `section:` entries: name -> constraint (or sdk marker)."""
    out = {}
    in_sec, indent = False, None
    cur, buf = None, []
    for line in lines + [""]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        top = re.match(r"^(\S[^:]*):\s*(.*)$", line)
        if top and not line.startswith((" ", "\t")):
            if in_sec and cur:
                out[cur] = _pub_constraint(buf)
            in_sec = (top.group(1).strip() == section)
            indent, cur, buf = None, None, []
            continue
        if not in_sec:
            continue
        m = re.match(r"^(\s+)(\S[^:]*):\s*(.*)$", line)
        if not m:
            continue
        ind, key, rest = len(m.group(1)), m.group(2).strip(), m.group(3).strip()
        if indent is None:
            indent = ind
        if ind == indent:
            if cur:
                out[cur] = _pub_constraint(buf)
            cur, buf = key, [rest] if rest else []
        elif cur and ind > indent:
            buf.append(line.strip())
    if in_sec and cur:
        out[cur] = _pub_constraint(buf)
    return out


def _pub_constraint(buf: list) -> str:
    text = " ".join(b for b in buf if b).strip()
    if not text:
        return "* (sdk)"
    if text.startswith("{"):
        return "* (sdk)"
    if "sdk:" in text:
        # e.g. `sdk: flutter` possibly with version line
        mv = re.search(r"version\s*:\s*([^\s}]+)", text)
        if mv:
            return mv.group(1)
        return "* (sdk)"
    return text.strip("'\"") or "* (sdk)"


def _pub_lock_versions_file(lock: Path) -> dict:
    if not lock.is_file():
        return {}
    try:
        text = lock.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out = {}
    for chunk in re.split(r"\n(?=\S)", text):
        mname = re.match(r"^(\s*)(\S+):\s*$", chunk.splitlines()[0] if chunk.splitlines() else "")
        if not mname:
            continue
        mv = re.search(r'(?m)^\s+version:\s*"([^"]+)"', chunk)
        if mv:
            out[mname.group(2)] = mv.group(1)
    # lock format is `packages:\n  name:\n    version:` — fix names
    fixed = {}
    for chunk in re.split(r"\n  (?=\S)", text):
        first = chunk.splitlines()[0] if chunk.splitlines() else ""
        name = first.strip().rstrip(":")
        if not name or name == "packages:":
            continue
        mv = re.search(r'version:\s*"([^"]+)"', chunk)
        if mv and "/" not in name:
            fixed[name] = mv.group(1)
    return fixed or out


def _pub_lock_versions(checkout: Path) -> dict:
    """Root pubspec.lock pins (legacy wrapper; prefer the file form)."""
    return _pub_lock_versions_file(Path(checkout) / "pubspec.lock")


def lock_provenance(checkout, relpath: str) -> dict:
    """Tracked/ignored provenance for one lock file (read-only git).

    Returns {lock_file, present, ignored, tracked}; ignored/tracked are
    None when unprovable (missing file, non-git checkout, git failure) —
    callers must stay conservative on None, never claim healthy.
    """
    checkout = Path(checkout)
    info = {"lock_file": relpath, "present": False,
            "ignored": None, "tracked": None}
    if not (checkout / relpath).is_file():
        return info
    info["present"] = True
    if not (checkout / ".git").exists():
        return info
    try:
        from .util import run_cmd
    except ImportError:
        return info
    try:
        code, _, _ = run_cmd(["git", "-C", str(checkout), "check-ignore",
                              "-q", "--", relpath], timeout=30)
        info["ignored"] = (code == 0)
        code, _, _ = run_cmd(["git", "-C", str(checkout), "ls-files",
                              "--error-unmatch", "--", relpath], timeout=30)
        info["tracked"] = (code == 0)
    except Exception:  # noqa: BLE001 - unprovable, stays conservative
        info["ignored"], info["tracked"] = None, None
    return info


def _pub_from_file(pub_file: Path, lock: dict, provenance: dict,
                   scope_prefix: str, file_label: str) -> list:
    """One pubspec.yaml's deps with per-scope lock pins + provenance."""
    try:
        lines = pub_file.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for sec, scope in (("dependencies", "dependencies"),
                       ("dev_dependencies", "devDependencies")):
        for name, constraint in _pub_section(lines, sec).items():
            rows.append({
                "manager": "pub",
                "scope": scope if not scope_prefix else f"{scope_prefix}:{scope}",
                "file": file_label, "name": name,
                "declared": constraint,
                "installed": lock.get(name),
                "unmanaged": unmanaged_reason(constraint),
                "lock_file": provenance.get("lock_file"),
                "lock_tracked": provenance.get("tracked"),
            })
    return rows


def discover(checkout) -> dict:
    """Return {deps: [...], sdks: [...], sources: [...], warnings: [...] }."""
    checkout = Path(checkout)
    deps, sdks, sources, warnings = [], [], [], []
    pkg = checkout / "package.json"
    if pkg.is_file():
        sources.append("package.json")
        installed = _npm_installed(checkout)
        deps.extend(_npm_from_file(pkg, installed, ""))
        example = checkout / "example" / "package.json"
        if example.is_file():
            sources.append("example/package.json")
            deps.extend(_npm_from_file(example, installed, "example"))
        else:
            warnings.append("example/package.json not found; root manifest only")
    pub = checkout / "pubspec.yaml"
    if pub.is_file():
        sources.append("pubspec.yaml")
        try:
            lines = pub.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            warnings.append(f"pubspec.yaml unreadable: {exc}")
            lines = []
        if lines:
            sdk = _pub_value(lines, "sdk")
            if sdk:
                sdks.append({"name": "dart-sdk", "constraint": sdk,
                             "file": "pubspec.yaml", "kind": "sdk"})
        root_lock = _pub_lock_versions(checkout)
        deps.extend(_pub_from_file(pub, root_lock,
                                   lock_provenance(checkout, "pubspec.lock"),
                                   "", "pubspec.yaml"))
        example_pub = checkout / "example" / "pubspec.yaml"
        if example_pub.is_file():
            sources.append("example/pubspec.yaml")
            example_lock = _pub_lock_versions_file(
                checkout / "example" / "pubspec.lock")
            deps.extend(_pub_from_file(
                example_pub, example_lock,
                lock_provenance(checkout, "example/pubspec.lock"),
                "example", "example/pubspec.yaml"))
        if not root_lock:
            warnings.append("pubspec.lock not found; installed pins unavailable")
    if not sources:
        warnings.append("no supported manifest found (package.json/pubspec.yaml)")
    return {"deps": deps, "sdks": sdks, "sources": sources, "warnings": warnings}
