"""Portable repository profiles.

A repo profile describes *how to maintain* one repository and can be copied
to another PC via `repo show --export`. It must not contain PC paths,
credentials, or OpenMainty-core assumptions about any specific project.

Schema:
{
  "name": "openiap",
  "github": "hyodotdev/openiap",
  "baseBranch": "main",
  "knowledge": ["knowledge/internal/04-platform-packages.md"],
  "checks": [{"name": "parity", "cmd": ["bun", "audit:parity"], "requires": []}],
  "e2e": [{"name": "ios-device", "cmd": [...], "requires": ["ios_device"]}],
  "release": {"notes": "CHANGELOG.md"},
  "announce": {"medium": true, "discord": true, "slack": true},
  "fastcheck": {
    "steps": [{"argv": ["yarn", "install", "--immutable"],
               "gate": "install", "install": true, "timeout_s": 1200}],
    "toolchain": ["/opt/homebrew/bin"]
  }
}

`fastcheck` (M2 autonomy) is the deliberate exception to the no-PC-paths
rule: the daemon runs under launchd with a minimal PATH, so the repo
carries its own absolute toolchain directories (validated as existing
directories at save time, owner-editable per machine). Each step is an
argv list executed with no shell; exactly the steps listed run, in
order. A missing `fastcheck` blocks the lane ("fastcheck_steps 없음"),
never a guessed default.
"""
from __future__ import annotations

from pathlib import Path

from . import paths
from .util import read_json, write_json


def profile_path(name: str) -> Path:
    return paths.repos_dir() / f"{name}.json"


def load(name: str) -> dict:
    data = read_json(profile_path(name), default=None)
    if data is None:
        raise SystemExit(f"error: unknown repo '{name}' (see: openmainty repo list)")
    return data


def list_all() -> list:
    d = paths.repos_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def validate(profile: dict) -> list:
    errors = []
    for field in ("name", "github", "baseBranch"):
        if not profile.get(field):
            errors.append(f"missing required field: {field}")
    for group in ("checks", "e2e"):
        steps = profile.get(group, [])
        if not isinstance(steps, list):
            errors.append(f"'{group}' must be a list")
            continue
        for i, step in enumerate(steps):
            if not step.get("name"):
                errors.append(f"'{group}[{i}]' missing name")
            if not step.get("cmd"):
                errors.append(f"'{group}[{i}]' missing cmd")
    if "fastcheck" in profile:
        errors.extend(validate_fastcheck(profile.get("fastcheck")))
    return errors


def validate_fastcheck(fc) -> list:
    """Validate the M2 fastcheck profile block (argv lists, no shell).

    Returns a list of error strings (empty = valid). Toolchain
    directories must exist at save time: the daemon cannot guess them
    from a login shell under launchd."""
    import os
    errors = []
    if not isinstance(fc, dict):
        return ["'fastcheck' must be an object"]
    steps = fc.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("'fastcheck.steps' must be a non-empty list")
    else:
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append(f"'fastcheck.steps[{i}]' must be an object")
                continue
            argv = step.get("argv")
            if (not isinstance(argv, list) or not argv
                    or not all(isinstance(a, str) and a
                               for a in argv)):
                errors.append(f"'fastcheck.steps[{i}].argv' must be a "
                              f"non-empty list of strings")
            gate = step.get("gate")
            if not isinstance(gate, str) or not gate or len(gate) > 80:
                errors.append(f"'fastcheck.steps[{i}].gate' must be a "
                              f"non-empty gate key (<=80 chars): no "
                              f"guessing from argv (M2.2-B)")
            if "install" in step and not isinstance(step["install"],
                                                    bool):
                errors.append(f"'fastcheck.steps[{i}].install' must be "
                              f"a boolean")
            sandbox = step.get("sandbox", "strict")
            if sandbox not in ("strict", "loopback"):
                errors.append(f"'fastcheck.steps[{i}].sandbox' must be "
                              f"'strict' or 'loopback'")
            elif (sandbox == "loopback"
                    and list(argv or [])[:2] != ["flutter", "test"]):
                errors.append(f"'fastcheck.steps[{i}].sandbox' "
                              f"'loopback' is only for "
                              f"'flutter test' steps")
            timeout = step.get("timeout_s")
            if (timeout is not None
                    and (not isinstance(timeout, int)
                         or isinstance(timeout, bool) or timeout <= 0)):
                errors.append(f"'fastcheck.steps[{i}].timeout_s' must be "
                              f"a positive integer")
    toolchain = fc.get("toolchain", [])
    if not isinstance(toolchain, list):
        errors.append("'fastcheck.toolchain' must be a list")
    else:
        for entry in toolchain:
            if not isinstance(entry, str) or not os.path.isabs(entry):
                errors.append("'fastcheck.toolchain' entries must be "
                              f"absolute paths: {entry!r}")
            elif not os.path.isdir(entry):
                errors.append("'fastcheck.toolchain' directory does not "
                              f"exist: {entry!r}")
    return errors


def save(profile: dict) -> Path:
    errors = validate(profile)
    if errors:
        raise SystemExit("error: invalid repo profile:\n- " + "\n- ".join(errors))
    path = profile_path(profile["name"])
    write_json(path, profile)
    return path


def validate_name(name: str) -> str:
    """Repo names become `<name>.json` files: strict charset, no separators."""
    import re
    cleaned = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cleaned):
        raise SystemExit(
            f"error: invalid repo name {name!r} "
            "(letters, digits, dot, dash, underscore; must start alnum)")
    return cleaned


_NO_LOCK_REASON = ("잠금 파일 없음 — 재현 가능한 설치가 불가합니다. "
                    "저장소에 lockfile을 커밋한 뒤 다시 등록하세요")


def _pub_lock_state(pkgdir) -> str:
    """Three-way pubspec.lock rule (M3.3a), read-only git probes in
    the worktree: "tracked" | "ignored" | "absent". Anything else
    (not a git checkout, unreadable) is "absent" — without evidence
    the lock is committed or ignored, no reproducible install.
    """
    import subprocess as _sp
    lock = Path(pkgdir) / "pubspec.lock"
    try:
        if not lock.is_file():
            return "absent"
    except OSError:
        return "absent"

    def _git_ok(*argv):
        try:
            proc = _sp.run(["git", "-C", str(pkgdir)] + list(argv),
                           stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                           stdin=_sp.DEVNULL, timeout=30)
            return proc.returncode == 0
        except (OSError, _sp.SubprocessError):
            return False
    if _git_ok("ls-files", "--error-unmatch", "pubspec.lock"):
        return "tracked"
    if _git_ok("check-ignore", "-q", "pubspec.lock"):
        return "ignored"
    return "absent"


def _flutter_steps(state: str, extra_dir: str = "") -> list:
    """Install per the lock state; analyze/test never need the
    network (proven offline in M3.3a). Tracked locks are honored
    read-only (--enforce-lockfile). An ignored lock never survives
    into a fresh worktree (untracked files stay behind), so plain
    `pub get` regenerates it — gitignored, hence fingerprint-safe.
    """
    get = ["flutter", "pub", "get"]
    if state == "tracked":
        get = get + ["--enforce-lockfile"]
    if extra_dir:
        get = get + ["--directory", extra_dir]
    return [
        {"name": "설치" + (f"({extra_dir})" if extra_dir else ""),
         "argv": get, "gate": "install", "install": True,
         "timeout_s": 1200},
        {"name": "분석", "argv": ["flutter", "analyze"],
         "gate": "lint", "install": False, "timeout_s": 600},
        {"name": "테스트", "argv": ["flutter", "test"],
         "gate": "unit", "install": False, "timeout_s": 900,
         # The tester binds a loopback HttpServer at startup, so it
         # needs the loopback-only profile (remote TCP and DNS stay
         # denied). run_step honors this ONLY for flutter test.
         "sandbox": "loopback"},
    ]


def detect_fastcheck(checkout) -> dict:
    """Propose a fastcheck block from lockfiles + manifests (M3.2).

    Pure local reads, no network, no writes. The manager comes from
    the LOCKFILE (Root review 4), scripts are included only when
    they exist, and every returned profile passes validate_fastcheck
    (gate keys present, toolchain dirs absolute and existing).
    Returns {"manager", "steps", "toolchain"} or
    {"unsupported": <Korean reason>}.
    """
    import json as _json
    import os as _os
    import shutil as _shutil
    root = Path(checkout)
    try:
        pkg = _json.loads((root / "package.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        pkg = {}
    scripts = (pkg.get("scripts") or {}) if isinstance(pkg, dict) \
        else {}
    has_yarn_lock = (root / "yarn.lock").is_file()
    has_yarnrc = (root / ".yarnrc.yml").is_file()
    has_npm_lock = (root / "package-lock.json").is_file()
    has_pnpm_lock = (root / "pnpm-lock.yaml").is_file()
    has_pubspec = (root / "pubspec.yaml").is_file()
    if has_pnpm_lock and not (has_yarn_lock or has_npm_lock):
        return {"unsupported":
                "pnpm은 이 단계에서 지원하지 않음 — "
                "yarn/npm 저장소만 자동 등록됩니다"}

    def _tool_dir(binary: str):
        found = _shutil.which(binary)
        if not found:
            return None
        parent = str(Path(found).parent)
        if not _os.path.isabs(parent) or \
                not _os.path.isdir(parent):
            return None
        return parent

    def _toolchain(*binaries):
        seen, out = set(), []
        for binary in binaries:
            hit = _tool_dir(binary)
            if hit and hit not in seen:
                seen.add(hit)
                out.append(hit)
        return out
    if has_pubspec and not (has_yarn_lock or has_npm_lock):
        # M3.3a three-way rule, applied per package dir (root,
        # then example/): tracked -> enforce read-only; ignored ->
        # plain get (fresh worktrees never carry the ignored lock);
        # absent -> refuse.
        state = _pub_lock_state(root)
        if state == "absent":
            return {"unsupported": _NO_LOCK_REASON}
        steps = _flutter_steps(state)
        example = root / "example"
        if (example / "pubspec.yaml").is_file():
            ex_state = _pub_lock_state(example)
            if ex_state == "absent":
                return {"unsupported":
                        _NO_LOCK_REASON + " (example/)"}
            steps = (steps[:1]
                     + _flutter_steps(ex_state, "example")[:1]
                     + steps[1:])
        return {"manager": "flutter", "steps": steps,
                "toolchain": _toolchain("flutter")}
    if has_yarn_lock and has_yarnrc:
        manager, install_argv, runner = (
            "yarn-berry", ["yarn", "install", "--immutable"], "yarn")
    elif has_yarn_lock:
        manager, install_argv, runner = (
            "yarn-classic",
            ["yarn", "install", "--frozen-lockfile"], "yarn")
    elif has_npm_lock:
        manager, install_argv, runner = (
            "npm", ["npm", "ci"], "npm")
    else:
        # F2: no lockfile means no reproducible install. The old
        # fallback (`npm install --package-lock-only`) WROTE a
        # package-lock.json into the worktree — an untracked source
        # file that changes the fingerprint — while installing
        # nothing. Refuse instead.
        return {"unsupported": _NO_LOCK_REASON}
    steps = [{"name": "설치", "argv": install_argv,
              "gate": "install", "install": True, "timeout_s": 1200}]
    for script, gate, label in (("build", "build", "빌드"),
                                ("typecheck", "typecheck", "타입 검사"),
                                ("lint", "lint", "린트"),
                                ("test", "unit", "테스트")):
        if isinstance(scripts, dict) and scripts.get(script):
            if runner == "yarn":
                argv = ["yarn", script]
            else:
                argv = ["npm", "run", script]
            steps.append({"name": label, "argv": argv, "gate": gate,
                          "install": False, "timeout_s": 600})
    tools = ["yarn", "node"] if runner == "yarn" else ["npm", "node"]
    return {"manager": manager, "steps": steps,
            "toolchain": _toolchain(*tools)}


def slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-")
    return slug or "repo"


def delete(name: str) -> None:
    """Unregister a profile. Checkout files are never touched."""
    path = profile_path(name)
    if not path.is_file():
        raise SystemExit(f"error: unknown repo '{name}' (see: openmainty repo list)")
    path.unlink()
