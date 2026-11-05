"""Derived lock re-resolve in prepare (S3/D1) + attempt history (P-HIST).

Covers batches.reresolve_on_base with real git fixture repos under an
isolated OPENMAINTY_HOME and a fake package-manager shim on PATH (no
registry, no network): lock-only conflict derives done with a DERIVED
commit, non-lock conflicts block naming paths, tool failure blocks with
rc, unrelated lock moves block with a bounded list, original op bytes
are never rewritten, and re-prepare archives prior evidence in a
bounded attempts list. Also covers replay_failed_at on multi-commit
conflicts (P-LINE data).
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, m2, paths, runner, sched, store  # noqa: E402
from openmainty.util import read_json, write_json  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout


FOO_V1 = '''foo@^1.0.0:
  version "1.0.0"
  resolved "https://registry.yarnpkg.com/foo/-/foo-1.0.0.tgz#aaa"
  dependencies:
    mini "1.0.0"

'''

FOO_BASE = '''foo@^1.0.0:
  version "1.0.1"
  resolved "https://registry.yarnpkg.com/foo/-/foo-1.0.1.tgz#bbb"
  dependencies:
    mini "1.0.0"

'''

FOO_DERIVED = '''foo@^1.0.0:
  version "1.2.3"
  resolved "https://registry.yarnpkg.com/foo/-/foo-1.2.3.tgz#ccc"
  dependencies:
    mini "1.0.0"

'''

BAR = '''bar@^2.0.0:
  version "2.0.0"
  resolved "https://registry.yarnpkg.com/bar/-/bar-2.0.0.tgz#ddd"

'''

BAR_BUMPED = '''bar@^2.1.0:
  version "2.1.0"
  resolved "https://registry.yarnpkg.com/bar/-/bar-2.1.0.tgz#eee"

'''

MINI = '''mini@1.0.0:
  version "1.0.0"
  resolved "https://registry.yarnpkg.com/mini/-/mini-1.0.0.tgz#fff"

'''

REACT_V1 = '''react@^19.0.0:
  version "19.0.0"
  resolved "https://registry.yarnpkg.com/react/-/react-19.0.0.tgz#r00"

'''

REACT_NEW = '''react@^19.3.0:
  version "19.3.1"
  resolved "https://registry.yarnpkg.com/react/-/react-19.3.1.tgz#r31"

'''

REACTDOM_V1 = '''react-dom@^19.0.0:
  version "19.0.0"
  resolved "https://registry.yarnpkg.com/react-dom/-/react-dom-19.0.0.tgz#d00"

'''

REACTDOM_NEW = '''react-dom@^19.3.0:
  version "19.3.1"
  resolved "https://registry.yarnpkg.com/react-dom/-/react-dom-19.3.1.tgz#d31"

'''

FOO_TARGET = '''foo@^1.2.0:
  version "1.2.3"
  resolved "https://registry.yarnpkg.com/foo/-/foo-1.2.3.tgz#ccc"
  dependencies:
    mini "1.0.0"

'''

FOO_NPM_V1 = '''foo@npm:^1.0.0:
  version "1.0.0"
  resolved "https://registry.yarnpkg.com/foo/-/foo-1.0.0.tgz#aaa"
  dependencies:
    mini "1.0.0"

'''

FOO_NPM_NEW = '''foo@npm:^1.2.0:
  version "1.2.3"
  resolved "https://registry.yarnpkg.com/foo/-/foo-1.2.3.tgz#ccc"
  dependencies:
    mini "1.0.0"

'''

BAR_NPM_V1 = '''bar@npm:^2.0.0:
  version "2.0.0"
  resolved "https://registry.yarnpkg.com/bar/-/bar-2.0.0.tgz#ddd"

'''

BAR_NPM_NEW = '''bar@npm:^2.1.0:
  version "2.1.0"
  resolved "https://registry.yarnpkg.com/bar/-/bar-2.1.0.tgz#eee"

'''

# M2.14 joint fixtures: RN-family berry names with npm: descriptors,
# mirroring the live RN 0.88 + expo 58 stanza shapes.
J_RN_V1 = '''react-native@npm:0.86.2:
  version "0.86.2"
  resolved "https://registry.yarnpkg.com/react-native/-/react-native-0.86.2.tgz#r1"

'''
J_RN_NEW = '''react-native@npm:0.88.0:
  version "0.88.0"
  resolved "https://registry.yarnpkg.com/react-native/-/react-native-0.88.0.tgz#r2"

'''
J_METRO_V1 = '''@react-native/metro-config@npm:0.86.2:
  version "0.86.2"
  resolved "https://registry.yarnpkg.com/metro-config/-/metro-config-0.86.2.tgz#m1"

'''
J_METRO_CARET_V1 = '''"@react-native/metro-config@npm:^0.86.0":
  version "0.86.2"
  resolved "https://registry.yarnpkg.com/metro-config/-/metro-config-0.86.2.tgz#m1"

'''
J_METRO_NEW = '''@react-native/metro-config@npm:0.88.0:
  version "0.88.0"
  resolved "https://registry.yarnpkg.com/metro-config/-/metro-config-0.88.0.tgz#m2"

'''
# M2.14: the same range in v1 bare form, pinned to a different
# version — a cross-form pin conflict with J_METRO_NEW above.
J_METRO_BARE_DIFF = '''"@react-native/metro-config@0.88.0":
  version "0.88.1"
  resolved "https://registry.yarnpkg.com/metro-config/-/metro-config-0.88.1.tgz#m3"

'''
J_CODEGEN_V1 = '''@react-native/codegen@npm:0.86.2:
  version "0.86.2"
  resolved "https://registry.yarnpkg.com/codegen/-/codegen-0.86.2.tgz#c1"

'''
J_CODEGEN_NEW = '''@react-native/codegen@npm:0.88.0:
  version "0.88.0"
  resolved "https://registry.yarnpkg.com/codegen/-/codegen-0.88.0.tgz#c2"

'''
J_EXPO_V1 = '''expo@npm:^57.0.11:
  version "57.0.11"
  resolved "https://registry.yarnpkg.com/expo/-/expo-57.0.11.tgz#e1"

'''
J_EXPO_NEW = '''expo@npm:^58.0.17:
  version "58.0.17"
  resolved "https://registry.yarnpkg.com/expo/-/expo-58.0.17.tgz#e2"

'''
J_EXPOCLI_V1 = '''@expo/cli@npm:^57.0.13:
  version "57.0.13"
  resolved "https://registry.yarnpkg.com/cli/-/cli-57.0.13.tgz#x1"

'''
J_EXPOCLI_NEW = '''@expo/cli@npm:^58.1.14:
  version "58.1.14"
  resolved "https://registry.yarnpkg.com/cli/-/cli-58.1.14.tgz#x2"

'''
J_NEIGH_V1 = '''neighbor@npm:^1.0.0:
  version "1.0.0"
  resolved "https://registry.yarnpkg.com/neighbor/-/neighbor-1.0.0.tgz#n1"

'''


META = '''__metadata:
  version: 8
  cacheKey: 8

'''


def lock_text(foo, bar=BAR):
    return (META + foo + bar + MINI)


SHIM = '''#!/bin/sh
# Fake berry yarn: --version, config get, and install
# --mode=update-lockfile only. Refuses script-enabled or modeless runs
# (R3) and the removed `up` path (R2). Test config arrives via files
# next to the shim (cfg/FOO, cfg/BAR, cfg/RC) — never via env, because
# the regen runs under the scrubbed M1 env which drops every FAKE_*
# var. The shim re-exports them for its own embedded python only.
CFG="$(dirname "$0")/cfg"
cfg() { cat "$CFG/$1" 2>/dev/null; }
FAKE_YARN_RC="$(cfg RC)"; export FAKE_YARN_RC
FAKE_YARN_FOO="$(cfg FOO)"; export FAKE_YARN_FOO
FAKE_YARN_BAR="$(cfg BAR)"; export FAKE_YARN_BAR
FAKE_YARN_GROUP="$(cfg GROUP)"; export FAKE_YARN_GROUP
FAKE_YARN_GROUP_DEPS="$(cfg GROUP_DEPS)"; export FAKE_YARN_GROUP_DEPS
if [ "$1" = "--version" ]; then echo "4.9.2"; exit 0; fi
if [ "$1" = "config" ]; then echo "https://registry.yarnpkg.com"; exit 0; fi
if [ "$1" != "install" ]; then
  echo "fake-yarn: unsupported argv $*" >&2; exit 2
fi
has_mode=0
for a in "$@"; do
  [ "$a" = "--mode=update-lockfile" ] && has_mode=1
done
[ "$has_mode" = "1" ] || {
  echo "fake-yarn: missing --mode=update-lockfile" >&2; exit 3; }
[ "$YARN_ENABLE_SCRIPTS" = "0" ] || {
  echo "fake-yarn: scripts not disabled" >&2; exit 3; }
if [ -n "$FAKE_YARN_RC" ] && [ "$FAKE_YARN_RC" != "0" ]; then
  echo "error: network unreachable https://registry.yarnpkg.com" >&2
  exit "$FAKE_YARN_RC"
fi
python3 - "$FAKE_YARN_FOO" "$FAKE_YARN_BAR" <<'PYEOF'
import json
import os as _os
import re as _re
import sys
foo_ver, bar_flag = sys.argv[1], sys.argv[2]
text = open("yarn.lock", encoding="utf-8").read()
pkg = json.load(open("package.json", encoding="utf-8"))
if _os.environ.get("FAKE_YARN_GROUP", "") == "1":
    # M2.10 group mode: GROUP_DEPS lists "dep:version" lines; each
    # dep's descriptor is renamed to its declared range and its
    # stanza version is set — a generic fake resolve, no registry.
    _deps = []
    for _line in (_os.environ.get("FAKE_YARN_GROUP_DEPS", "")
                  or "").splitlines():
        if ":" in _line:
            _d, _v = _line.split(":", 1)
            _d, _v = _d.strip(), _v.strip()
            if _d and _v:
                _deps.append((_d, _v))
    _dev = pkg.get("devDependencies", {})
    for _dep, _ver in _deps:
        _rng = _dev.get(_dep, "")
        if _rng:
            # M2.12: consume the whole descriptor (berry writes an
            # "npm:" protocol prefix) and keep its protocol, so the
            # regen output mirrors real yarn output shapes.
            _m = _re.search(
                r'(?m)^"?' + _re.escape(_dep)
                + r'@((?:npm:)?[^,":\\s]+)("?):', text)
            if _m:
                _proto = "npm:" if _m.group(1).startswith("npm:") \
                    else ""
                text = (text[:_m.start(1)] + _proto + _rng
                        + _m.group(2) + text[_m.end(2):])
        _parts = text.split("\\n\\n")
        for _i, _ch in enumerate(_parts):
            _first = _ch.split("\\n", 1)[0] if _ch else ""
            if _first.lstrip('"').startswith(_dep + "@"):
                _parts[_i] = _re.sub(
                    r'(?m)^(\\s*version:?\\s*"?)[^"\\s]+("?)',
                    lambda _m: _m.group(1) + _ver + (_m.group(2)
                                                     or ""),
                    _ch, count=1)
                break
        text = "\\n\\n".join(_parts)
    open("yarn.lock", "w", encoding="utf-8").write(text)
    sys.exit(0)
rng = pkg.get("devDependencies", {}).get("foo", "")
if rng and ("foo@%s:" % rng) not in text:
    text = text.replace("foo@^1.0.0:", "foo@%s:" % rng, 1)
if "foo@^1.0.0:" in text or ("foo@%s:" % rng) in text:
    text = text.replace('version "1.0.1"', 'version "%s"' % foo_ver, 1)
    text = text.replace("#bbb", "#ccc", 1)
else:
    stanza = ('foo@^1.0.0:\\n  version "%s"\\n  resolved '
              '"https://registry.yarnpkg.com/foo/-/foo-%s.tgz#ccc"\\n'
              '  dependencies:\\n    mini "1.0.0"\\n\\n'
              % (foo_ver, foo_ver))
    text = text.rstrip("\\n") + "\\n\\n" + stanza
if bar_flag == "1":
    text = text.replace('version "2.0.0"', 'version "2.1.0"', 1)
    text = text.replace("#ddd", "#eee", 1)
open("yarn.lock", "w", encoding="utf-8").write(text)
PYEOF
exit $?
'''


class LockfixMixin(unittest.TestCase):
    """Origin @ c0; candidate c1 (lock-only); base moves to c2.

    MODE selects the candidate shape: "lockonly" (yarn.lock only) or
    "manifest" (package.json + yarn.lock, conflicting manifest).
    """

    MODE = "lockonly"

    def _pkg(self, foo, neighbor):
        return json.dumps({"devDependencies": {"foo": foo,
                                               "neighbor": neighbor}},
                          indent=2) + "\n"

    def _group_pkg(self, foo, bar, neighbor):
        return json.dumps({"devDependencies": {"foo": foo, "bar": bar,
                                               "neighbor": neighbor}},
                          indent=2) + "\n"

    def _pair_pkg(self, react, dom, neighbor):
        return json.dumps({"devDependencies": {"react": react,
                                               "react-dom": dom,
                                               "neighbor": neighbor}},
                          indent=2) + "\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        self.old_path = os.environ.get("PATH", "")
        bindir = self.root / "bin"
        bindir.mkdir()
        shim = bindir / "yarn"
        shim.write_text(SHIM, encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        os.environ["PATH"] = str(bindir) + ":" + self.old_path
        self.addCleanup(os.environ.__setitem__, "PATH", self.old_path)
        # S18(f): hermetic disk — the regen guard reads the host disk
        # by default, so report plenty here (the refusal itself is
        # covered by explicit _avail_bytes unit tests). Kept on self:
        # a manual mix.setUp() elsewhere never runs a tearDown, so
        # that test stops it explicitly.
        self._disk_patcher = mock.patch.object(
            batches, "_regen_disk_ok",
            return_value=(True, "픽스처 99.0GB"))
        self._disk_patcher.start()
        self.addCleanup(self._disk_patcher.stop)
        self.cfgdir = bindir / "cfg"
        self.cfgdir.mkdir(exist_ok=True)
        for var in ("RC", "FOO", "BAR"):
            self.cfg_pop(var)
        self.cfg_set("FOO", "1.2.3")
        self.cfg_set("BAR", "0")
        if self.MODE in ("group", "groupmoved", "groupscripts",
                         "groupalready", "groupnpm", "groupmissing"):
            self.cfg_set("GROUP", "1")
            (self.cfgdir / "GROUP_DEPS").write_text(
                "foo:1.2.3\nbar:2.1.0\n", encoding="utf-8")
        if self.MODE == "grouppair":
            self.cfg_set("GROUP", "1")
            (self.cfgdir / "GROUP_DEPS").write_text(
                "react:19.3.1\nreact-dom:19.3.1\n", encoding="utf-8")
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git("init", "-b", "main", cwd=self.origin)
        git("config", "user.email", "test@localhost", cwd=self.origin)
        git("config", "user.name", "test", cwd=self.origin)
        if self.MODE in ("adjacent", "sameline", "twoline",
                           "adjacent2", "groupstale"):
            (self.origin / "package.json").write_text(
                self._pkg("^1.0.0", "^1.0.0"), encoding="utf-8")
        elif self.MODE in ("group", "groupmoved", "groupscripts",
                           "groupalready", "groupnpm", "groupmissing"):
            (self.origin / "package.json").write_text(
                self._group_pkg("^1.0.0", "^2.0.0", "^1.0.0"),
                encoding="utf-8")
        elif self.MODE == "grouppair":
            (self.origin / "package.json").write_text(
                self._pair_pkg("^19.0.0", "^19.0.0", "^1.0.0"),
                encoding="utf-8")
        else:
            (self.origin / "package.json").write_text(
                json.dumps({"devDependencies": {"foo": "^1.0.0"}}),
                encoding="utf-8")
        c0_lock = lock_text(FOO_V1)
        if self.MODE == "grouppair":
            c0_lock = META + REACT_V1 + REACTDOM_V1
        if self.MODE == "groupnpm":
            c0_lock = META + FOO_NPM_V1 + BAR_NPM_V1 + MINI
        (self.origin / "yarn.lock").write_text(c0_lock, encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "c0", cwd=self.origin)
        self.c0 = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-qb", "cand", cwd=self.origin)
        if self.MODE in ("manifest", "bump"):
            (self.origin / "package.json").write_text(
                json.dumps({"devDependencies": {"foo": "^1.2.0",
                                                "note": "cand"}}),
                encoding="utf-8")
        if self.MODE in ("adjacent", "sameline"):
            (self.origin / "package.json").write_text(
                self._pkg("^1.2.0", "^1.0.0"), encoding="utf-8")
        if self.MODE == "twoline":
            (self.origin / "package.json").write_text(
                self._pkg("^1.2.0", "^2.0.0"), encoding="utf-8")
        if self.MODE in ("group", "groupmoved", "groupalready",
                         "groupnpm", "groupmissing"):
            (self.origin / "package.json").write_text(
                self._group_pkg("^1.2.0", "^2.1.0", "^1.0.0"),
                encoding="utf-8")
        if self.MODE == "groupstale":
            (self.origin / "package.json").write_text(
                self._pkg("^1.2.0", "^1.0.0"), encoding="utf-8")
        if self.MODE == "groupscripts":
            (self.origin / "package.json").write_text(
                json.dumps({"devDependencies": {"foo": "^1.2.0",
                                                "bar": "^2.1.0",
                                                "neighbor": "^1.0.0"},
                            "scripts": {"postinstall": "echo hi"}},
                           indent=2) + "\n",
                encoding="utf-8")
        if self.MODE == "grouppair":
            (self.origin / "package.json").write_text(
                self._pair_pkg("^19.3.0", "^19.3.0", "^1.0.0"),
                encoding="utf-8")
        if self.MODE == "nested-pod":
            pod = self.origin / "example" / "ios" / "Podfile.lock"
            pod.parent.mkdir(parents=True, exist_ok=True)
            pod.write_text("PODS:\n  - PodA (1.0)\nSPEC CHECKSUMS:\n",
                           encoding="utf-8")
        if self.MODE == "adjacent2":
            # Two-commit range: the first commit moves foo ^1.0.0 ->
            # ^1.1.0, the tip moves it again ^1.1.0 -> ^1.2.0. Both
            # touch the manifest; the conflict under test is at pos 1.
            (self.origin / "package.json").write_text(
                self._pkg("^1.1.0", "^1.0.0"), encoding="utf-8")
            (self.origin / "yarn.lock").write_text(
                lock_text(FOO_DERIVED), encoding="utf-8")
            git("add", "-A", cwd=self.origin)
            git("commit", "-qm", "candidate foo part 1", cwd=self.origin)
            (self.origin / "package.json").write_text(
                self._pkg("^1.2.0", "^1.0.0"), encoding="utf-8")
        c1_lock = lock_text(FOO_DERIVED)
        if self.MODE in ("group", "groupmoved", "groupscripts",
                         "groupalready"):
            c1_lock = META + FOO_DERIVED + BAR_BUMPED + MINI
        if self.MODE == "groupnpm":
            c1_lock = META + FOO_NPM_NEW + BAR_NPM_NEW + MINI
        if self.MODE == "groupmissing":
            c1_lock = META + FOO_DERIVED + BAR + MINI
        if self.MODE == "groupstale":
            c1_lock = META + FOO_TARGET + BAR + MINI
        if self.MODE == "grouppair":
            c1_lock = META + REACT_NEW + REACTDOM_NEW
        (self.origin / "yarn.lock").write_text(c1_lock, encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "candidate foo 1.0.0 -> 1.2.3",
            cwd=self.origin)
        self.c1 = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        if self.MODE == "manifest":
            (self.origin / "package.json").write_text(
                json.dumps({"devDependencies": {"foo": "^1.0.0",
                                                "note": "base"}}),
                encoding="utf-8")
        if self.MODE in ("adjacent", "twoline", "adjacent2"):
            (self.origin / "package.json").write_text(
                self._pkg("^1.0.0", "^2.0.0"), encoding="utf-8")
        if self.MODE in ("group", "groupscripts", "groupnpm",
                         "groupmissing"):
            (self.origin / "package.json").write_text(
                self._group_pkg("^1.0.0", "^2.0.0", "^2.0.0"),
                encoding="utf-8")
        if self.MODE == "groupalready":
            (self.origin / "package.json").write_text(
                self._group_pkg("^1.0.0", "^2.1.0", "^2.0.0"),
                encoding="utf-8")
        if self.MODE == "groupstale":
            (self.origin / "package.json").write_text(
                self._pkg("^1.2.0", "^2.0.0"), encoding="utf-8")
        if self.MODE == "grouppair":
            (self.origin / "package.json").write_text(
                self._pair_pkg("^19.0.0", "^19.0.0", "^2.0.0"),
                encoding="utf-8")
        if self.MODE == "groupmoved":
            (self.origin / "package.json").write_text(
                self._group_pkg("^1.0.0", "^2.0.5", "^2.0.0"),
                encoding="utf-8")
        if self.MODE == "sameline":
            (self.origin / "package.json").write_text(
                self._pkg("^1.0.5", "^1.0.0"), encoding="utf-8")
        if self.MODE == "nested-pod":
            pod = self.origin / "example" / "ios" / "Podfile.lock"
            pod.parent.mkdir(parents=True, exist_ok=True)
            pod.write_text("PODS:\n  - PodA (2.0)\nSPEC CHECKSUMS:\n",
                           encoding="utf-8")
        c2_lock = lock_text(FOO_BASE)
        if self.MODE == "grouppair":
            c2_lock = META + REACT_V1 + REACTDOM_V1
        if self.MODE == "groupnpm":
            c2_lock = META + FOO_NPM_V1 + BAR_NPM_V1 + MINI
        if self.MODE == "groupalready":
            c2_lock = META + FOO_BASE + BAR_BUMPED + MINI
        if self.MODE == "groupstale":
            c2_lock = META + FOO_TARGET + BAR + MINI
        (self.origin / "yarn.lock").write_text(c2_lock, encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "base moves lock", cwd=self.origin)
        self.c2 = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("branch", "-f", "next", cwd=self.origin)
        self.base_wt = self.root / "home" / "base-wt"
        git("worktree", "add", "--detach", str(self.base_wt), self.c2,
            cwd=self.origin)
        snap = runner.source_snapshot(self.base_wt)
        self.base_snap = {"head": snap["head"],
                          "fingerprint": snap["fingerprint"]}
        self.run_id = "run-testlockfix1"
        run_work = paths.runs_dir() / self.run_id / "work"
        git("worktree", "add", "--detach", str(run_work), self.c1,
            cwd=self.origin)
        csnap = runner.source_snapshot(run_work)
        self.cand_fp = csnap["fingerprint"]
        state = store.create("fakerepo", "dep-foo-1.2.3",
                             "chore(deps): foo 1.0.0 -> 1.2.3",
                             self.c0, {})
        state["id"] = self.run_id
        state["dep"] = "foo"
        state["target"] = "1.2.3"
        state["lane"] = "next"
        state["branch"] = "codex/next/foo-1-2-3"
        if self.MODE == "grouppair":
            state["dep"] = "react"
            state["target"] = "19.3.1"
            state["branch"] = "codex/next/react-19-3-1"
        state["workCommit"] = self.c1
        state["workTreeFingerprint"] = self.cand_fp
        state["verifiedCommits"] = {"review": self.c1, "e2e": self.c1}
        state["verifiedFingerprints"] = {"review": self.cand_fp,
                                         "e2e": self.cand_fp}
        state["steps"] = {"review": {"status": "done", "exit_code": 0},
                          "e2e": {"status": "done", "exit_code": 0}}
        store.save(state)
        # R2 regression: the run log holds only prose about yarn, never
        # an executable command. Derivation must use the structured
        # install --mode=update-lockfile plan, never this text.
        (paths.runs_dir() / self.run_id / "fix.log.txt").write_text(
            "note: yarn lock for foo 1.0.0 -> 1.2.3 on branch\n"
            "prose, not a command: do not execute\n",
            encoding="utf-8")
        self.base_run_id = "run-testlockfix0"
        base_run_work = paths.runs_dir() / self.base_run_id / "work"
        git("worktree", "add", "--detach", str(base_run_work), self.c2,
            cwd=self.origin)
        bsnap = runner.source_snapshot(base_run_work)
        bstate = store.create("fakerepo", "dep-bar-9.9.9",
                              "chore(deps): bar 9.9.8 -> 9.9.9",
                              self.c2, {})
        bstate["id"] = self.base_run_id
        bstate["dep"] = "bar"
        bstate["target"] = "9.9.9"
        bstate["lane"] = "next"
        bstate["branch"] = "codex/next/bar-9-9-9"
        bstate["workCommit"] = self.c2
        bstate["workTreeFingerprint"] = bsnap["fingerprint"]
        bstate["verifiedCommits"] = {"review": self.c2, "e2e": self.c2}
        bstate["verifiedFingerprints"] = {"review": bsnap["fingerprint"],
                                          "e2e": bsnap["fingerprint"]}
        bstate["steps"] = {"review": {"status": "done", "exit_code": 0},
                           "e2e": {"status": "done", "exit_code": 0}}
        store.save(bstate)
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        ev_dir = batches.evidence_dir("test-batch")
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "proof.txt").write_text("proof\n", encoding="utf-8")
        ev_sha = batches.sha256_file(ev_dir / "proof.txt")
        write_json(batches.batches_dir() / "test-batch.json", {
            "schema": 3, "id": "test-batch", "repo": "fakerepo",
            "target_branch": "next", "primaries": [{
                "dep": "bar", "target": "9.9.9", "lane": "next",
                "run_id": self.base_run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(base_run_work),
                           "head": self.c2,
                           "fingerprint": bsnap["fingerprint"]}}],
            "inherited": [],
            "origin": {"checkout": str(self.base_wt),
                       "base_branch": "next", "base_head": self.c2},
            "integrations": [{"lane": "next", "worktree": str(self.base_wt),
                              "head": self.c2,
                              "fingerprint": self.base_snap["fingerprint"],
                              "status": "integration-testing",
                              "gates": [{
                                  "key": "unit", "status": "pass",
                                  "inputs": {
                                      "head": self.c2,
                                      "fingerprint":
                                      self.base_snap["fingerprint"]},
                                  "evidence": [{
                                      "label": "unit proof",
                                      "kind": "file", "stored": True,
                                      "stored_path": "proof.txt",
                                      "sha256": ev_sha, "bytes": 6}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})
        self.batch_id = "test-batch"
        self.op_id = f"prep-{self.run_id}-{self.batch_id}"

    def cfg_set(self, key, value):
        (self.cfgdir / key).write_text(value, encoding="utf-8")

    def cfg_pop(self, key):
        try:
            (self.cfgdir / key).unlink()
        except OSError:
            pass

    def blocked_op(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("cherry-pick", rec["error"])
        return rec

    def derived_path(self):
        return paths.home() / "reviews" / (
            f"prep-{self.batch_id}-foo-1-2-3-lockfix")


class ReresolveSuccessTests(LockfixMixin):
    def test_lock_only_conflict_derives_done(self):
        before = self.blocked_op()
        before_bytes = (batches.op_file(self.op_id).read_bytes())
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "done")
        self.assertEqual(drec["op"], "reresolve")
        self.assertEqual(drec["op_id"], self.op_id + "-lockfix")
        self.assertEqual(drec["derived_from"], self.op_id)
        self.assertTrue(drec["result"]["branch"].endswith("-lockfix"))
        self.assertNotEqual(drec["result"]["head"], self.c1)
        res_path = Path(drec["result"]["path"])
        self.assertTrue(self.derived_path().exists())
        lock = (res_path / "yarn.lock").read_text(encoding="utf-8")
        self.assertIn('version "1.2.3"', lock)
        self.assertIn('version "2.0.0"', lock)
        head_msg = git("log", "-1", "--format=%B", cwd=res_path)
        self.assertIn("DERIVED", head_msg)
        self.assertIn(self.op_id, head_msg)
        by = drec["regenerated_by"]
        self.assertEqual(by["tool"], "yarn")
        self.assertEqual(by["argv"],
                         ["yarn", "install", "--mode=update-lockfile"])
        self.assertEqual(by["rc"], 0)
        # R2: prose-only run log never becomes argv; the structured
        # plan derives from lockfile + manifest hunks + op identity.
        self.assertEqual(by["command_origin"], "lock-refresh")
        self.assertNotIn("intent_source", by)
        self.assertEqual(by["tool_version"], "4.9.2")
        self.assertEqual(by["registry_host"],
                         "registry.yarnpkg.com")
        self.assertEqual(by["mode"], "update-lockfile")
        self.assertIn("YARN_ENABLE_SCRIPTS", by["env_flags"])
        self.assertIn("CI", by["env_flags"])
        self.assertEqual(by["resolved_version"], "1.2.3")
        self.assertEqual(by["package_manager"], "")
        gates = {g["key"]: g["status"] for g in drec["gates"]}
        self.assertEqual(gates["source-prepared"], "pass")
        self.assertTrue(drec["gates"][0]["note"].startswith("파생:"))
        self.assertIn("DERIVED", drec["result"]["proof_note"])
        # Original blocked op keeps its record byte-identical.
        self.assertEqual(batches.op_file(self.op_id).read_bytes(),
                         before_bytes)
        self.assertEqual(before["status"], "blocked")

    def test_no_recorded_command_needed(self):
        self.blocked_op()
        (paths.runs_dir() / self.run_id / "fix.log.txt").unlink()
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "done")
        by = drec["regenerated_by"]
        self.assertEqual(by["command_origin"], "lock-refresh")
        self.assertEqual(by["argv"],
                         ["yarn", "install", "--mode=update-lockfile"])

    def test_wrong_resolved_version_blocked(self):
        self.blocked_op()
        self.cfg_set("FOO", "9.9.9")
        try:
            drec = batches.reresolve_on_base(self.op_id)
        finally:
            self.cfg_set("FOO", "1.2.3")
        self.assertEqual(drec["status"], "blocked")
        self.assertIn("후보 목표 1.2.3", drec["error"])
        self.assertIn("9.9.9", drec["error"])
        self.assertFalse(self.derived_path().exists())

    def test_idempotent_retry_reconciles(self):
        self.blocked_op()
        first = batches.reresolve_on_base(self.op_id)
        second = batches.reresolve_on_base(self.op_id)
        self.assertTrue(second.get("existing"))
        self.assertEqual(second["result"]["head"],
                         first["result"]["head"])


class ReresolveOriginTests(LockfixMixin):
    def test_driver_origin_recorded_never_root_attested(self):
        # S17: driver-initiated (S15a) derived ops carry origin "m2".
        self.blocked_op()
        drec = batches.reresolve_on_base(self.op_id, origin="m2")
        self.assertEqual(drec["status"], "done")
        self.assertEqual(drec.get("origin"), "m2")
        text = batches.op_file(self.op_id + "-lockfix").read_text(
            encoding="utf-8")
        self.assertIn('"origin": "m2"', text)
        # J1: a worker-built derived op never claims Root attestation.
        self.assertNotIn('"root_attested": true', text)

    def test_ui_shape_has_no_origin(self):
        # UI calls pass nothing: today's record shape is unchanged.
        self.blocked_op()
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "done")
        self.assertNotIn("origin", drec)

    def test_bad_origin_refused(self):
        self.blocked_op()
        with self.assertRaises(batches.IntegrationError):
            batches.reresolve_on_base(self.op_id, origin="owner")


class ReresolveManifestTests(LockfixMixin):
    MODE = "manifest"

    def test_nonlock_conflict_blocked_naming_paths(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        before_bytes = batches.op_file(self.op_id).read_bytes()
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "blocked")
        # The candidate manifest touches more than dep's own range
        # line, so D1-d refuses instead of rebuilding.
        self.assertIn("package.json", drec["error"])
        self.assertIn("own range line", drec["error"])
        self.assertFalse(self.derived_path().exists())
        self.assertEqual(batches.op_file(self.op_id).read_bytes(),
                         before_bytes)


class ReresolveBumpTests(LockfixMixin):
    MODE = "bump"

    def test_manifest_bump_derives_done(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("cherry-pick", rec["error"])
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "done")
        by = drec["regenerated_by"]
        self.assertEqual(by["command_origin"], "manifest-bump")
        self.assertEqual(by["argv"],
                         ["yarn", "install", "--mode=update-lockfile"])
        self.assertEqual(by["resolved_version"], "1.2.3")
        res_path = Path(drec["result"]["path"])
        pkg = json.loads((res_path / "package.json").read_text(
            encoding="utf-8"))
        self.assertEqual(pkg["devDependencies"]["foo"], "^1.2.0")


class ReresolveNestedTests(LockfixMixin):
    MODE = "nested-pod"

    def test_nested_lockfile_conflict_refused(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "blocked")
        self.assertIn("example/ios/Podfile.lock", drec["error"])
        self.assertIn("재생성하지 않음", drec["error"])
        self.assertFalse(self.derived_path().exists())


class ReresolveRefusalTests(LockfixMixin):
    def test_tool_failure_blocked_with_rc(self):
        self.blocked_op()
        self.cfg_set("RC", "1")
        try:
            drec = batches.reresolve_on_base(self.op_id)
        finally:
            self.cfg_pop("RC")
        self.assertEqual(drec["status"], "blocked")
        self.assertIn("rc=1", drec["error"])
        self.assertIn("network unreachable", drec["error"])
        self.assertNotIn("token", drec["error"].lower())
        self.assertFalse(self.derived_path().exists())

    def test_unrelated_lock_move_blocked(self):
        self.blocked_op()
        self.cfg_set("BAR", "1")
        try:
            drec = batches.reresolve_on_base(self.op_id)
        finally:
            self.cfg_set("BAR", "0")
        self.assertEqual(drec["status"], "blocked")
        self.assertIn("관련 없는 잠금 이동", drec["error"])
        self.assertIn("bar", drec["error"])
        self.assertFalse(self.derived_path().exists())

    def test_done_op_refused(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        # Single-commit lock-only candidate on a moved base blocks;
        # force a done record via the derived path instead.
        if rec["status"] == "blocked":
            rec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(rec["status"], "done")
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.reresolve_on_base(rec["op_id"])
        self.assertIn("not a prepare", str(ctx.exception))

    def test_unknown_op_refused(self):
        with self.assertRaises(batches.IntegrationError):
            batches.reresolve_on_base("prep-nope-nope")

    def test_non_cherrypick_blocked_refused(self):
        opf = batches.op_file(self.op_id)
        write_json(opf, {"op": "prepare", "op_id": self.op_id,
                         "status": "blocked",
                         "error": "base batch is not READY",
                         "updated_at": "2026-10-19T00:00:00+00:00",
                         "gates": []})
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.reresolve_on_base(self.op_id)
        self.assertIn("no cherry-pick conflict", str(ctx.exception))

    def test_no_implicit_caller(self):
        srcdir = Path(__file__).resolve().parent.parent / "src" \
            / "openmainty"
        for name in ("sched.py", "runner.py", "muse.py", "store.py",
                     "watch.py"):
            path = srcdir / name
            if path.is_file():
                self.assertNotIn(
                    "reresolve_on_base",
                    path.read_text(encoding="utf-8"),
                    f"implicit caller in {name}")


BERRY_BASE = '''# This file is generated by running "yarn install" inside your project.

__metadata:
  version: 8
  cacheKey: 8

"release-it@npm:^21.0.1":
  version: 21.0.1
  resolution: "release-it@npm:21.0.1"
  dependencies:
    open: "npm:^10.1.0"
    semver: "npm:^7.7.1"
  checksum: aaa

"open@npm:^10.1.0":
  version: 10.1.0
  resolution: "open@npm:10.1.0"
  checksum: bbb

"semver@npm:^7.7.1":
  version: 7.7.1
  resolution: "semver@npm:7.7.1"
  checksum: ccc

"unrelated@npm:^1.0.0":
  version: 1.0.0
  resolution: "unrelated@npm:1.0.0"
  checksum: ddd
'''

BERRY_NEW = '''# This file is generated by running "yarn install" inside your project.

__metadata:
  version: 8
  cacheKey: 9

"release-it@npm:^21.1.1":
  version: 21.1.1
  resolution: "release-it@npm:21.1.1"
  dependencies:
    open: "npm:^10.1.0"
    semver: "npm:^7.7.1"
    fresh: "npm:^2.0.0"
  checksum: eee

"open@npm:^10.1.0":
  version: 10.1.0
  resolution: "open@npm:10.1.0"
  checksum: bbb

"semver@npm:^7.7.1":
  version: 7.7.1
  resolution: "semver@npm:7.7.1"
  checksum: ccc

"fresh@npm:^2.0.0":
  version: 2.0.0
  resolution: "fresh@npm:2.0.0"
  dependencies:
    semver: "npm:^7.7.1"
  checksum: fff

"unrelated@npm:^1.0.0":
  version: 1.0.0
  resolution: "unrelated@npm:1.0.0"
  checksum: ddd
'''

WS_BASE = BERRY_BASE + '''
"app@workspace:.":
  version: 0.0.0-use.local
  resolution: "app@workspace:."
  dependencies:
    dep: "npm:^1.0.0"
  languageName: unknown
  linkType: soft
'''

WS_NEW_DEP_ONLY = WS_BASE.replace('dep: "npm:^1.0.0"',
                                  'dep: "npm:^1.2.0"')

WS_NEW_OTHER_LINE = WS_BASE.replace('linkType: soft',
                                    'linkType: hard')


class LockGuardUnitTests(unittest.TestCase):
    def test_berry_closure_covers_new_transitives(self):
        # release-it shape: new version adds `fresh`, which exists only
        # in the new graph; union closure must admit it.
        bad = batches._lock_delta_guard("yarn", BERRY_BASE, BERRY_NEW,
                                        "release-it")
        self.assertEqual(bad, [])

    def test_berry_unrelated_addition_flagged(self):
        new = BERRY_NEW.replace(
            '"unrelated@npm:^1.0.0":\n  version: 1.0.0',
            '"unrelated@npm:^1.0.0":\n  version: 9.9.9')
        bad = batches._lock_delta_guard("yarn", BERRY_BASE, new,
                                        "release-it")
        self.assertEqual(bad, ["unrelated"])

    def test_v1_closure_parsed(self):
        base = lock_text(FOO_V1)
        new = lock_text(FOO_DERIVED)
        self.assertEqual(batches._lock_delta_guard("yarn", base, new,
                                                   "foo"), [])
        self.assertIn("mini", batches._yarn_closure(
            batches._split_yarn_stanzas(base), "foo"))

    def test_workspace_dep_line_change_allowed(self):
        bad = batches._lock_delta_guard("yarn", WS_BASE,
                                        WS_NEW_DEP_ONLY, "dep")
        self.assertEqual(bad, [])

    def test_workspace_other_line_change_flagged(self):
        bad = batches._lock_delta_guard("yarn", WS_BASE,
                                        WS_NEW_OTHER_LINE, "dep")
        self.assertEqual(bad, ["app"])

    def test_metadata_only_diff_ignored(self):
        new = BERRY_BASE.replace("cacheKey: 8", "cacheKey: 10c0")
        bad = batches._lock_delta_guard("yarn", BERRY_BASE, new,
                                        "release-it")
        self.assertEqual(bad, [])

    def test_family_duplicate_versions_allowed(self):
        base = ('"cli@npm:^1.0.0":\n  version: 1.0.0\n'
                '  resolution: "cli@npm:1.0.0"\n'
                '  dependencies:\n    fam: "npm:^1.0.0"\n'
                '  checksum: a\n\n'
                '"fam@npm:^1.0.0":\n  version: 1.0.0\n'
                '  resolution: "fam@npm:1.0.0"\n  checksum: b\n')
        new = (base + '\n"fam@npm:^1.0.0, fam@npm:^2.0.0":\n'
               '  version: 2.0.0\n'
               '  resolution: "fam@npm:2.0.0"\n  checksum: c\n')
        bad = batches._lock_delta_guard("yarn", base, new, "cli")
        self.assertEqual(bad, [])

    def test_resolved_versions_both_flavors(self):
        self.assertEqual(batches._yarn_resolved_versions(
            BERRY_NEW, "release-it"), {"21.1.1"})
        self.assertEqual(batches._yarn_resolved_versions(
            lock_text(FOO_DERIVED), "foo"), {"1.2.3"})
        self.assertEqual(batches._yarn_flavor(BERRY_NEW), "berry")
        self.assertEqual(batches._yarn_flavor(lock_text(FOO_V1)),
                         "berry")

    def test_non_yarn_guard_refuses(self):
        with self.assertRaises(batches.IntegrationError):
            batches._lock_delta_guard("pnpm", "a", "b", "dep")

    def test_peer_only_move_blocked(self):
        # R6: peers never expand the closure, so a moved peer-only
        # package is unrelated.
        base = ('"dep@npm:^1.0.0":\n  version: 1.0.0\n'
                '  resolution: "dep@npm:1.0.0"\n'
                '  peerDependencies:\n    peer: "npm:^1.0.0"\n'
                '  checksum: a\n\n'
                '"peer@npm:^1.0.0":\n  version: 1.0.0\n'
                '  resolution: "peer@npm:1.0.0"\n  checksum: b\n')
        new = base.replace('  version: 1.0.0\n'
                           '  resolution: "peer@npm:1.0.0"',
                           '  version: 2.0.0\n'
                           '  resolution: "peer@npm:2.0.0"')
        self.assertEqual(batches._lock_delta_guard("yarn", base, new,
                                                   "dep"), ["peer"])

    def test_workspace_sibling_package_line_flagged(self):
        # R8: "@types/react" must not cover a "@types/react-dom" line.
        ws = ('"pkg@workspace:.":\n  version: 0.0.0-use.local\n'
              '  resolution: "pkg@workspace:."\n  dependencies:\n'
              '    "@types/react": "npm:^19.0.0"\n'
              '    "@types/react-dom": "npm:^19.0.0"\n'
              '  languageName: unknown\n  linkType: soft\n')
        moved_sib = ws.replace('"@types/react-dom": "npm:^19.0.0"',
                               '"@types/react-dom": "npm:^19.1.0"')
        bad = batches._lock_delta_guard("yarn", ws, moved_sib,
                                        "@types/react")
        self.assertEqual(bad, ["pkg"])
        moved_dep = ws.replace('"@types/react": "npm:^19.0.0"',
                               '"@types/react": "npm:^19.3.0"')
        self.assertEqual(batches._lock_delta_guard(
            "yarn", ws, moved_dep, "@types/react"), [])

    def test_retained_older_version_allowed(self):
        # R7: the transitive ^29.0.1 node may stay at 29.16.0 while the
        # declared ^29.16.0 descriptor resolves to 29.17.0.
        base = ('"jest@npm:^29.16.0":\n  version: 29.16.0\n'
                '  resolution: "jest@npm:29.16.0"\n  checksum: a\n\n'
                '"jest@npm:^29.0.1":\n  version: 29.16.0\n'
                '  resolution: "jest@npm:29.16.0"\n  checksum: b\n')
        new = base.replace('"jest@npm:^29.16.0":\n  version: 29.16.0\n'
                           '  resolution: "jest@npm:29.16.0"',
                           '"jest@npm:^29.16.0":\n  version: 29.17.0\n'
                           '  resolution: "jest@npm:29.17.0"')
        ok, seen = batches._check_declared_resolution(
            new, base, "jest", {"^29.16.0"}, "29.17.0")
        self.assertTrue(ok)
        self.assertEqual(seen, "29.17.0")
        self.assertEqual(batches._lock_delta_guard("yarn", base, new,
                                                   "jest"), [])

    def test_declared_resolving_newer_blocked(self):
        base = ('"jest@npm:^29.16.0":\n  version: 29.16.0\n'
                '  resolution: "jest@npm:29.16.0"\n  checksum: a\n')
        new = base.replace("29.16.0", "29.17.1")
        ok, seen = batches._check_declared_resolution(
            new, base, "jest", {"^29.16.0"}, "29.17.0")
        self.assertFalse(ok)
        self.assertEqual(seen, "29.17.1")


class AliasNameTests(unittest.TestCase):
    """L1: npm-alias descriptors belong to the alias package.

    rsplit("@", 1) once named "string-width-cjs@npm:string-width@^4.2.0"
    as "string-width-cjs@npm:string-width" and falsely tripped the
    closure guard on the live jest 30 re-resolve.
    """

    def test_alias_names(self):
        name = batches._yarn_pkg_name
        self.assertEqual(
            name("string-width-cjs@npm:string-width@^4.2.0"),
            "string-width-cjs")
        self.assertEqual(
            name("string-width-cjs@npm:string-width@npm:^4.2.0"),
            "string-width-cjs")
        self.assertEqual(
            name("@scope/a-cjs@npm:@scope/a@^1"), "@scope/a-cjs")
        self.assertEqual(name("foo@^1.0.0"), "foo")
        self.assertEqual(name("@scope/a@^1.0.0"), "@scope/a")
        self.assertEqual(name("foo"), "foo")

    def test_alias_closure_accepts_live_jest_shape(self):
        # Live shape (jest 29.7.0 -> 30.5.2): the new graph gains
        # @isaacs/cliui, whose combined alias stanzas must resolve to
        # [] — the exact three names from the false refusal.
        base = (
            '"jest@npm:^29.7.0":\n  version: 29.7.0\n'
            '  resolution: "jest@npm:29.7.0"\n  dependencies:\n'
            '    glob: "npm:^9.0.0"\n  checksum: a\n\n'
            '"glob@npm:^9.0.0":\n  version: 9.3.1\n'
            '  resolution: "glob@npm:9.3.1"\n  checksum: b\n')
        cliui_body = (
            '  dependencies:\n'
            '    string-width: "npm:^4.2.0"\n'
            '    string-width-cjs: "npm:string-width@^4.2.0"\n'
            '    strip-ansi: "npm:^6.0.1"\n'
            '    strip-ansi-cjs: "npm:strip-ansi@^6.0.1"\n'
            '    wrap-ansi: "npm:^7.0.0"\n'
            '    wrap-ansi-cjs: "npm:wrap-ansi@^7.0.0"\n')
        pair = ('"%(a)s@npm:%(b)s@%(r)s, %(b)s@npm:%(r)s":\n'
                '  version: 4.2.0\n'
                '  resolution: "%(a)s@npm:%(b)s@4.2.0"\n  checksum: c\n\n')
        new = (
            '"jest@npm:^30.5.2":\n  version: 30.5.2\n'
            '  resolution: "jest@npm:30.5.2"\n  dependencies:\n'
            '    glob: "npm:^10.0.0"\n'
            '    "@isaacs/cliui": "npm:^8.0.0"\n  checksum: d\n\n'
            '"glob@npm:^10.0.0":\n  version: 10.3.0\n'
            '  resolution: "glob@npm:10.3.0"\n  checksum: e\n\n'
            '"@isaacs/cliui@npm:^8.0.0":\n  version: 8.0.0\n'
            '  resolution: "@isaacs/cliui@npm:8.0.0"\n' +
            cliui_body + '  checksum: f\n\n' +
            pair % {"a": "string-width-cjs", "b": "string-width",
                    "r": "^4.2.0"} +
            pair % {"a": "strip-ansi-cjs", "b": "strip-ansi",
                    "r": "^6.0.1"} +
            pair % {"a": "wrap-ansi-cjs", "b": "wrap-ansi",
                    "r": "^7.0.0"})
        self.assertEqual(
            batches._lock_delta_guard("yarn", base, new, "jest"), [])

    def test_unreachable_alias_still_flagged(self):
        base = ('"jest@npm:^29.7.0":\n  version: 29.7.0\n'
                '  resolution: "jest@npm:29.7.0"\n  checksum: a\n')
        new = (base + '\n"left-pad-cjs@npm:left-pad@^1.0.0":\n'
               '  version: 1.0.0\n'
               '  resolution: "left-pad-cjs@npm:left-pad@1.0.0"\n'
               '  checksum: z\n')
        self.assertEqual(
            batches._lock_delta_guard("yarn", base, new, "jest"),
            ["left-pad-cjs"])


class AdjacentManifestTests(LockfixMixin):
    MODE = "adjacent"

    def test_adjacent_conflict_rebuilds_and_derives(self):
        # M2.9a: prepare itself re-derives a pure adjacent bump —
        # done as kind rederive through the same machinery, never
        # the original commit and never a separate reresolve call.
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done", rec.get("error"))
        self.assertEqual(rec["op"], "reresolve")
        self.assertEqual(rec.get("kind"), "rederive")
        self.assertEqual(rec["derived_from"], self.op_id)
        by = rec["regenerated_by"]
        self.assertEqual(by["command_origin"], "rederive")
        self.assertEqual(by["manifest_reapplied"],
                         [{"file": "package.json", "dep": "foo",
                           "from": "^1.0.0", "to": "^1.2.0"}])
        self.assertEqual(by["resolved_version"], "1.2.3")
        res_path = Path(rec["result"]["path"])
        pkg = json.loads((res_path / "package.json").read_text(
            encoding="utf-8"))
        self.assertEqual(pkg["devDependencies"]["foo"], "^1.2.0")
        self.assertEqual(pkg["devDependencies"]["neighbor"], "^2.0.0")
        lock = (res_path / "yarn.lock").read_text(encoding="utf-8")
        self.assertIn('version "1.2.3"', lock)
        head_msg = git("log", "-1", "--format=%B", cwd=res_path)
        self.assertTrue(
            head_msg.startswith("chore(deps): foo ^1.0.0 -> ^1.2.0"),
            head_msg)
        self.assertIn("re-derived from", head_msg)
        self.assertIn(self.c1[:12], head_msg)
        # The original op stays blocked (the pick itself failed).
        orig = read_json(batches.op_file(self.op_id), default=None)
        self.assertEqual(orig["status"], "blocked")
        self.assertIn(rec["op_id"], orig.get("error") or "")


class RederivePrepareTests(LockfixMixin):
    MODE = "adjacent"
    # M2.9a: the adjacent bump itself is covered by
    # AdjacentManifestTests above; here only the idempotency rule
    # (one derived attempt per op id, however often prepare runs).

    def test_second_prepare_never_rederives_twice(self):
        first = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(first["status"], "done")
        before = batches.op_file(first["op_id"]).read_bytes()
        second = batches.prepare_integration(self.run_id, self.batch_id)
        after = batches.op_file(first["op_id"]).read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(second["status"], "blocked")
        heads = git("branch", "--list", "codex/review-foo-1-2-3*",
                    cwd=self.origin).strip().splitlines()
        names = [h.strip().lstrip("*+ ") for h in heads if h.strip()]
        self.assertEqual(
            [b for b in names if b.endswith("-rederive")],
            ["codex/review-foo-1-2-3-rederive"])


class RederiveRefusalTests(LockfixMixin):
    MODE = "twoline"
    # M2.9a: a candidate touching any other line kept today's
    # honest block — no partial re-derive. M2.10 rule 3: the
    # twoline shape is a group whose sibling line the base
    # already holds at the target, so it derives with that line
    # skipped (recorded as already). A sibling line the base
    # holds at NEITHER end still refuses (GroupMovedLineTests).

    def test_second_line_already_in_base_derives(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done", rec.get("error"))
        self.assertEqual(rec.get("kind"), "rederive")
        self.assertEqual(rec.get("group"), ["foo ^1.0.0->^1.2.0"])
        self.assertEqual(rec.get("already"), ["neighbor ^2.0.0"])
        res_path = Path(rec["result"]["path"])
        pkg = json.loads((res_path / "package.json").read_text(
            encoding="utf-8"))
        self.assertEqual(pkg["devDependencies"]["foo"], "^1.2.0")
        self.assertEqual(pkg["devDependencies"]["neighbor"], "^2.0.0")
        head_msg = git("log", "-1", "--format=%B", cwd=res_path)
        self.assertTrue(
            head_msg.startswith("chore(deps): foo ^1.0.0 -> ^1.2.0"),
            head_msg)
        self.assertIn("already in base: neighbor ^2.0.0", head_msg)


class GroupRederiveTests(LockfixMixin):
    MODE = "group"
    # M2.10a: an RN-shaped group (foo + bar, the base moved an
    # unrelated adjacent line) derives as ONE record — every
    # member line moves, the unrelated line is kept, and the batch
    # stage still applies its own lockstep refusals later.

    def test_group_derives_together(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done", rec.get("error"))
        self.assertEqual(rec["op"], "reresolve")
        self.assertEqual(rec.get("kind"), "rederive")
        self.assertEqual(rec["derived_from"], self.op_id)
        self.assertEqual(rec.get("group"),
                         ["bar ^2.0.0->^2.1.0", "foo ^1.0.0->^1.2.0"])
        by = rec["regenerated_by"]
        self.assertEqual(by["command_origin"], "rederive")
        self.assertEqual(by["manifest_reapplied"],
                         [{"file": "package.json", "dep": "bar",
                           "from": "^2.0.0", "to": "^2.1.0"},
                          {"file": "package.json", "dep": "foo",
                           "from": "^1.0.0", "to": "^1.2.0"}])
        self.assertEqual(by["resolved_version"], "1.2.3")
        res_path = Path(rec["result"]["path"])
        pkg = json.loads((res_path / "package.json").read_text(
            encoding="utf-8"))
        self.assertEqual(pkg["devDependencies"]["foo"], "^1.2.0")
        self.assertEqual(pkg["devDependencies"]["bar"], "^2.1.0")
        self.assertEqual(pkg["devDependencies"]["neighbor"], "^2.0.0")
        lock = (res_path / "yarn.lock").read_text(encoding="utf-8")
        self.assertIn('version "1.2.3"', lock)
        self.assertIn('version "2.1.0"', lock)
        head_msg = git("log", "-1", "--format=%B", cwd=res_path)
        self.assertTrue(
            head_msg.startswith("chore(deps): foo ^1.0.0 -> ^1.2.0"),
            head_msg)
        self.assertIn("group re-applied together: "
                      "bar ^2.0.0->^2.1.0", head_msg)
        self.assertIn("re-derived from", head_msg)
        self.assertIn(self.c1[:12], head_msg)
        orig = read_json(batches.op_file(self.op_id), default=None)
        self.assertEqual(orig["status"], "blocked")
        self.assertIn(rec["op_id"], orig.get("error") or "")


class GroupMovedLineTests(LockfixMixin):
    MODE = "groupmoved"
    # M2.10a: one group dep whose base range is neither the
    # candidate's `from` nor its `to` refuses, naming that dep
    # and both values — never a partial derive of the rest.

    def test_moved_group_line_refused(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("bar", rec["error"])
        self.assertIn("^2.0.5", rec["error"])
        self.assertIn("^2.0.0->^2.1.0", rec["error"])
        self.assertIn("refresh work", rec["error"])
        self.assertFalse(
            list((paths.home() / "integration_ops").glob(
                "*-rederive.json")))


class GroupAlreadyTests(LockfixMixin):
    MODE = "groupalready"
    # M2.10 rule 3: a group line the base already holds at the
    # target is skipped (no edit) while the rest of the group is
    # re-applied — recorded as "already", never a refusal.

    def test_base_held_line_skipped(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done", rec.get("error"))
        self.assertEqual(rec.get("kind"), "rederive")
        self.assertEqual(rec.get("group"), ["foo ^1.0.0->^1.2.0"])
        self.assertEqual(rec.get("already"), ["bar ^2.1.0"])
        res_path = Path(rec["result"]["path"])
        pkg = json.loads((res_path / "package.json").read_text(
            encoding="utf-8"))
        self.assertEqual(pkg["devDependencies"]["foo"], "^1.2.0")
        self.assertEqual(pkg["devDependencies"]["bar"], "^2.1.0")
        self.assertEqual(pkg["devDependencies"]["neighbor"], "^2.0.0")
        lock = (res_path / "yarn.lock").read_text(encoding="utf-8")
        self.assertIn('version "1.2.3"', lock)
        self.assertIn('version "2.1.0"', lock)
        head_msg = git("log", "-1", "--format=%B", cwd=res_path)
        self.assertTrue(
            head_msg.startswith("chore(deps): foo ^1.0.0 -> ^1.2.0"),
            head_msg)
        self.assertIn("already in base: bar ^2.1.0", head_msg)
        self.assertIn("re-derived from", head_msg)


class PrepareRetryTests(LockfixMixin):
    MODE = "groupmoved"
    # M2.11a: one explicit human retry of a blocked prepare — the
    # attempt is archived on the record and one timeline event
    # names the op and the result. The driver never calls this.

    def test_retry_blocked_archives_and_events(self):
        from openmainty import events
        first = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(first["status"], "blocked")
        before = list(first.get("attempts") or [])
        out = batches.prepare_retry(self.run_id, self.batch_id, "root")
        self.assertEqual(out["status"], "blocked")
        self.assertIn("bar", out.get("error") or "")
        reread = read_json(batches.op_file(self.op_id), default=None)
        self.assertTrue(len(reread.get("attempts") or [])
                        > len(before))
        found = [e for e in events.list_events("fakerepo")
                 if e.get("kind") == "m2-prepare-retry"]
        self.assertTrue(found)
        last = found[-1]
        self.assertIn("by root", last.get("title") or "")
        self.assertIn(self.op_id, last.get("detail") or "")
        self.assertIn("blocked", last.get("detail") or "")
        self.assertEqual(last.get("actor"), "root")

    def test_retry_worker_refused(self):
        batches.prepare_integration(self.run_id, self.batch_id)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_retry(self.run_id, self.batch_id, "worker")
        self.assertEqual(ctx.exception.http_status, 400)

    def test_retry_unknown_run_refused(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_retry("run-nope", self.batch_id, "root")
        self.assertEqual(ctx.exception.http_status, 404)


class PrepareRetryDoneTests(LockfixMixin):
    MODE = "group"
    # M2.11a: a non-blocked op refuses with 409 and starts nothing.
    # (A derived-covered source op stays "blocked"-with-note by
    # design, so retry there returns the done derived record
    # without re-running — covered by RederivePrepareTests.)

    def test_retry_done_refused(self):
        op_id = f"prep-{self.run_id}-test-batch"
        write_json(batches.op_file(op_id), {
            "op": "prepare", "op_id": op_id, "repo": "fakerepo",
            "run_id": self.run_id, "batch_id": "test-batch",
            "dep": "foo", "target": "1.2.3", "lane": "next",
            "status": "done", "error": "",
            "candidate": {"head": self.c1, "commit_list": [self.c1]},
            "base": {"batch": "test-batch", "head": self.c2},
            "result": {"head": self.c2,
                       "path": str(self.base_wt)}})
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_retry(self.run_id, self.batch_id, "root")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertIn("not a blocked prepare", str(ctx.exception))


class PrepareHintTests(LockfixMixin):
    MODE = "group"
    # M2.11c: the re-derive hint is computed once per (op, current
    # base head), cached on the op record, and recomputed only when
    # the base head changes. Non-conflict blocks get no hint.

    def _write_snapshot(self):
        write_json(batches.batches_snapshot_path(), {
            "batches": [{
                "id": "test-batch", "repo": "fakerepo", "ready": 1,
                "integrations": [{"lane": "next"}],
                "inherited": [], "updated_at": "2026-10-23T00:00:00Z"}]})

    def _set_base_head(self, sha, worktree):
        bfile = batches.batches_dir() / "test-batch.json"
        bdoc = read_json(bfile, default=None)
        bdoc["integrations"] = [{"lane": "next", "head": sha,
                                 "worktree": str(worktree),
                                 "fingerprint": "f" * 64}]
        write_json(bfile, bdoc)

    def _blocked_op(self, error):
        op_id = f"prep-{self.run_id}-test-batch"
        write_json(batches.op_file(op_id), {
            "op": "prepare", "op_id": op_id, "repo": "fakerepo",
            "run_id": self.run_id, "batch_id": "test-batch",
            "dep": "foo", "target": "1.2.3", "lane": "next",
            "status": "blocked", "error": error,
            "candidate": {"head": self.c1, "commit_list": [self.c1]},
            "base": {"batch": "test-batch", "head": self.c2},
            "updated_at": "2026-10-23T00:00:00Z"})
        return op_id

    def test_ok_hint_cached_and_reused(self):
        self._write_snapshot()
        self._set_base_head(self.c0, self.origin)
        op_id = self._blocked_op("cherry-pick conflicted or failed")
        import unittest.mock as mock
        real = batches._rederive_eligible
        calls = []

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)

        with mock.patch.object(batches, "_rederive_eligible",
                               side_effect=counting):
            first = batches.prepare_rederive_hint("fakerepo", op_id)
            second = batches.prepare_rederive_hint("fakerepo", op_id)
        self.assertEqual(first.get("verdict"), "ok")
        self.assertEqual(first.get("base"), self.c0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second, first)
        reread = read_json(batches.op_file(op_id), default=None)
        self.assertEqual(reread.get("rederive_hint"), first)
        items = batches._m1_prepare_retry_items("fakerepo", 1793040000.0)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.get("kind"), "prepare-retry")
        self.assertEqual(item.get("ref"), self.run_id)
        self.assertEqual(
            item.get("question"),
            "기준에서 다시 적용 가능 — '다시 시도'로 진행 (외 1개 같이 올림)")

    def test_base_change_recomputes(self):
        self._write_snapshot()
        self._set_base_head(self.c0, self.origin)
        op_id = self._blocked_op("cherry-pick conflicted or failed")
        first = batches.prepare_rederive_hint("fakerepo", op_id)
        self.assertEqual(first.get("verdict"), "ok")
        rec = read_json(batches.op_file(op_id), default=None)
        rec["rederive_hint"] = {"base": "0" * 40, "verdict": "no",
                                "note": "stale cache", "at": "x"}
        write_json(batches.op_file(op_id), rec)
        import unittest.mock as mock
        real = batches._rederive_eligible
        calls = []

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)

        with mock.patch.object(batches, "_rederive_eligible",
                               side_effect=counting):
            second = batches.prepare_rederive_hint("fakerepo", op_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.get("base"), self.c0)
        self.assertEqual(second.get("verdict"), "ok")

    def test_non_conflict_block_has_no_hint(self):
        self._write_snapshot()
        self._set_base_head(self.c0, self.origin)
        op_id = self._blocked_op("boom: something else failed")
        self.assertIsNone(
            batches.prepare_rederive_hint("fakerepo", op_id))
        reread = read_json(batches.op_file(op_id), default=None)
        self.assertNotIn("rederive_hint", reread)
        items = batches._m1_prepare_retry_items("fakerepo", 1793040000.0)
        self.assertEqual(items, [])


class TwoSectionReplaceTests(unittest.TestCase):
    # M2.10 live shape: root package.json declares react twice
    # (devDependencies "19.2.8" plus peerDependencies "*"). The
    # textual replace targets the one line carrying the old range.

    TWO = ('{\n  "devDependencies": {\n    "react": "19.2.8",\n'
           '    "other": "^1.0.0"\n  },\n  "peerDependencies": {\n'
           '    "react": "*"\n  }\n}\n')

    def test_picks_line_with_old_range(self):
        out = batches._replace_dep_range_line(self.TWO, "react",
                                              "19.2.8", "19.3.1")
        self.assertIsNotNone(out)
        self.assertIn('"react": "19.3.1"', out)
        self.assertIn('"react": "*"', out)

    def test_two_lines_with_old_range_refuse(self):
        text = self.TWO.replace('"*"', '"19.2.8"')
        self.assertIsNone(
            batches._replace_dep_range_line(text, "react",
                                            "19.2.8", "19.3.1"))

    def test_no_line_with_old_range_refuses(self):
        self.assertIsNone(
            batches._replace_dep_range_line(self.TWO, "react",
                                            "18.0.0", "19.3.1"))


class GroupNpmPrefixTests(LockfixMixin):
    MODE = "groupnpm"
    # M2.12: berry "npm:" descriptors in the CANDIDATE lock pin
    # fine — the pin accepts the descriptor form ("npm:^2.1.0")
    # and the bare manifest form ("^2.1.0") alike. The candidate
    # lock below mirrors the real RN shapes
    # ("dep@npm:<range>").

    def test_npm_prefixed_candidate_lock_pins(self):
        # The regression shape: the candidate lock keys carry the
        # protocol prefix, so a bare-form lookup alone finds
        # nothing.
        c1_lock = git("show", f"{self.c1}:yarn.lock",
                      cwd=self.origin)
        want = batches._declared_resolutions(c1_lock, "bar",
                                             {"^2.1.0"})
        self.assertEqual(set(want), {"npm:^2.1.0"})
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done", rec.get("error"))
        self.assertEqual(rec.get("kind"), "rederive")
        self.assertEqual(rec.get("group"),
                         ["bar ^2.0.0->^2.1.0", "foo ^1.0.0->^1.2.0"])
        res_path = Path(rec["result"]["path"])
        lock = (res_path / "yarn.lock").read_text(encoding="utf-8")
        self.assertIn('version "1.2.3"', lock)
        self.assertIn('version "2.1.0"', lock)


class GroupMissingSiblingTests(LockfixMixin):
    MODE = "groupmissing"
    # M2.12: a sibling with no stanza for its new range in the
    # candidate lock still refuses (genuinely unpinned).

    def test_missing_sibling_pin_refused(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("pins no single version for sibling bar",
                      rec.get("error") or "")


class GroupStaleTests(LockfixMixin):
    MODE = "groupstale"
    # M2.10 rule 3: every group line already in base is a dup —
    # the existing stale outcome, never an empty derive.

    def test_all_lines_in_base_is_stale(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual((rec.get("stale") or {}).get("dep"), "foo")
        self.assertEqual((rec.get("stale") or {}).get("target"),
                         "1.2.3")
        self.assertIn("기준이 이미 foo를 1.2.3로 올림", rec["error"])
        self.assertFalse(
            list((paths.home() / "integration_ops").glob(
                "*-rederive.json")))


class GroupScriptsTests(LockfixMixin):
    MODE = "groupscripts"
    # M2.10a: a candidate that also touches a non-range line
    # (scripts) refuses — the group rule covers range lines only.

    def test_non_range_change_refused(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("dependency ranges", rec["error"])
        self.assertFalse(
            list((paths.home() / "integration_ops").glob(
                "*-rederive.json")))


class GroupPairTests(LockfixMixin):
    MODE = "grouppair"
    # M2.10a: the react + react-dom pair re-derives together.

    def test_react_pair_derives(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done", rec.get("error"))
        self.assertEqual(rec.get("kind"), "rederive")
        self.assertEqual(rec.get("group"),
                         ["react ^19.0.0->^19.3.0",
                          "react-dom ^19.0.0->^19.3.0"])
        res_path = Path(rec["result"]["path"])
        pkg = json.loads((res_path / "package.json").read_text(
            encoding="utf-8"))
        self.assertEqual(pkg["devDependencies"]["react"], "^19.3.0")
        self.assertEqual(pkg["devDependencies"]["react-dom"],
                         "^19.3.0")
        self.assertEqual(pkg["devDependencies"]["neighbor"], "^2.0.0")
        lock = (res_path / "yarn.lock").read_text(encoding="utf-8")
        self.assertIn('version "19.3.1"', lock)
        head_msg = git("log", "-1", "--format=%B", cwd=res_path)
        self.assertTrue(
            head_msg.startswith(
                "chore(deps): react ^19.0.0 -> ^19.3.0"),
            head_msg)
        self.assertIn("group re-applied together: "
                      "react-dom ^19.0.0->^19.3.0", head_msg)


class RederiveGuardTests(LockfixMixin):
    MODE = "adjacent"

    def test_unrelated_lock_move_refused(self):
        self.cfg_set("BAR", "1")
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("관련 없는 잠금 이동", rec["error"])
        # The refused attempt stays as a blocked derived record
        # (the lockfix pattern) — never a done one.
        dex = read_json(
            batches.op_file(self.op_id + "-rederive"), default=None)
        self.assertEqual(dex["status"], "blocked")
        self.assertIn("관련 없는 잠금 이동", dex["error"])


class AdjacentMultiCommitTests(LockfixMixin):
    MODE = "adjacent2"

    def test_conflicting_commit_compared_not_tip(self):
        # D1-d on a multi-commit range compares the CONFLICTING commit
        # (pos 1: ^1.0.0 -> ^1.1.0) against its parent — never the tip
        # (^1.2.0). The old tip comparison rebuilt ^1.2.0 up front and
        # then compared the stale original base (^1.0.0) against the
        # second commit's parent (^1.1.0) at pos 2, blocking with a
        # stale-candidate refusal. The accumulated rebuild reapplies
        # each conflicting commit in turn.
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "done")
        by = drec["regenerated_by"]
        self.assertEqual(by["command_origin"], "manifest-adjacent")
        self.assertEqual(by["manifest_reapplied"],
                         [{"file": "package.json", "dep": "foo",
                           "from": "^1.0.0", "to": "^1.1.0"},
                          {"file": "package.json", "dep": "foo",
                           "from": "^1.1.0", "to": "^1.2.0"}])
        res_path = Path(drec["result"]["path"])
        pkg = json.loads((res_path / "package.json").read_text(
            encoding="utf-8"))
        self.assertEqual(pkg["devDependencies"]["foo"], "^1.2.0")
        self.assertEqual(pkg["devDependencies"]["neighbor"], "^2.0.0")


class SameLineManifestTests(LockfixMixin):
    MODE = "sameline"

    def test_base_moved_same_line_refused(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "blocked")
        self.assertIn("기준이 같은 의존성 선언을 이미 변경함(^1.0.5)",
                      drec["error"])
        self.assertIn("새로 만들어야 함", drec["error"])
        self.assertFalse(self.derived_path().exists())


class TwoLineManifestTests(LockfixMixin):
    # M2.10 rule 3: the UI reresolve path (kind=None) keeps the
    # single-dep rule — a multi-line candidate refuses there even
    # though in-prepare re-derive handles groups. The fixture uses
    # the moved-line shape so prepare itself stays blocked (the
    # plain twoline shape now derives with already-skip).
    MODE = "groupmoved"

    def test_candidate_second_line_refused(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "blocked")
        self.assertIn("own range line", drec["error"])
        self.assertFalse(self.derived_path().exists())


class ConflictPathsTests(LockfixMixin):
    def test_blocked_prepare_records_conflicted_paths(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual(rec["result"]["conflicted_paths"],
                         ["yarn.lock"])


class ConflictPathsManifestTests(LockfixMixin):
    MODE = "manifest"

    def test_manifest_conflict_paths_recorded(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual(sorted(rec["result"]["conflicted_paths"]),
                         ["package.json", "yarn.lock"])


def _pkg_text(foo, neighbor="^1.0.0"):
    return json.dumps({"devDependencies": {"foo": foo,
                                            "neighbor": neighbor}},
                      indent=2) + "\n"


class ScopeClassifierTests(unittest.TestCase):
    """S15(4d): fixture-free samples of the pure scope classifier —
    the same rule the reresolve enforcer applies."""

    def texts(self, parent_foo="^1.0.0", cand_foo="^1.2.0",
              base_foo="^1.0.0"):
        return {"package.json": {
            "parent": _pkg_text(parent_foo),
            "candidate": _pkg_text(cand_foo),
            "base": _pkg_text(base_foo)}}

    def test_lock_only(self):
        self.assertEqual(batches.classify_conflict_scope(
            ["yarn.lock"], "foo", {}), "lock-only")
        self.assertEqual(batches.classify_conflict_scope(
            ["yarn.lock", "example/ios/Podfile.lock"], "foo", {}),
            "lock-only")

    def test_adjacent(self):
        self.assertEqual(batches.classify_conflict_scope(
            ["package.json", "yarn.lock"], "foo", self.texts()),
            "adjacent")

    def test_same_line(self):
        self.assertEqual(batches.classify_conflict_scope(
            ["package.json"], "foo",
            self.texts(base_foo="^1.0.5")), "same-line")

    def test_other_lines(self):
        texts = {"package.json": {
            "parent": _pkg_text("^1.0.0"),
            "candidate": _pkg_text("^1.2.0", "^9.9.9"),
            "base": _pkg_text("^1.0.0")}}
        self.assertEqual(batches.classify_conflict_scope(
            ["package.json"], "foo", texts), "other")

    def test_other_file(self):
        self.assertEqual(batches.classify_conflict_scope(
            ["src/index.js", "yarn.lock"], "foo", self.texts()),
            "other")

    def test_unknown_on_missing_evidence(self):
        self.assertEqual(batches.classify_conflict_scope(
            [], "foo", self.texts()), "unknown")
        self.assertEqual(batches.classify_conflict_scope(
            None, "foo", self.texts()), "unknown")
        self.assertEqual(batches.classify_conflict_scope(
            ["package.json"], "foo", {}), "unknown")


class ScopeAgreementTests(LockfixMixin):
    """S15(4d): the classifier and the real reresolve_on_base agree —
    eligible scopes derive done, same-line refuses as stale."""

    MODE = "lockonly"

    def test_lockonly_scope_agrees_with_derive(self):
        rec = self.blocked_op()
        scope = batches.prepare_reresolve_scope(rec)
        self.assertTrue(scope["eligible"])
        self.assertEqual(scope["scope"], "lock-only")
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "done")


class ScopeAgreementAdjacentTests(ScopeAgreementTests):
    MODE = "adjacent"

    def test_lockonly_scope_agrees_with_derive(self):
        # M2.9a: prepare itself derives the adjacent bump now; the
        # classifier still agrees on the (blocked) original record.
        drec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(drec["status"], "done")
        self.assertEqual(drec.get("kind"), "rederive")
        orig = read_json(batches.op_file(self.op_id), default=None)
        scope = batches.prepare_reresolve_scope(orig)
        self.assertTrue(scope["eligible"])
        self.assertEqual(scope["scope"], "adjacent")


class ScopeAgreementSameLineTests(ScopeAgreementTests):
    MODE = "sameline"

    def test_lockonly_scope_agrees_with_derive(self):
        rec = self.blocked_op()
        scope = batches.prepare_reresolve_scope(rec)
        self.assertFalse(scope["eligible"])
        self.assertEqual(scope["scope"], "same-line")
        drec = batches.reresolve_on_base(self.op_id)
        self.assertEqual(drec["status"], "blocked")
        self.assertIn("이미 변경함", drec["error"])


class RetryTests(LockfixMixin):
    def _blocked(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        return rec

    def test_retry_blocked_prepare_rearchives(self):
        first = self._blocked()
        second = batches.prepare_integration(self.run_id, self.batch_id,
                                             retry=True)
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(len(second["attempts"]), 1)
        self.assertIn("cherry-pick", second["attempts"][0]["error"])
        self.assertIn("cherry-pick", second["error"])
        _ = first

    def test_retry_without_flag_stays_terminal(self):
        self._blocked()
        again = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertTrue(again.get("existing"))
        self.assertEqual(again["status"], "blocked")
        self.assertNotIn("attempts", again)

    def test_retry_done_op_refused(self):
        self._blocked()
        done = batches.reresolve_on_base(self.op_id)
        self.assertEqual(done["status"], "done")
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.retry_integration_op(done["op_id"])
        self.assertIn("not blocked", str(ctx.exception))

    def test_retry_unknown_op_refused(self):
        with self.assertRaises(batches.IntegrationError):
            batches.retry_integration_op("prep-nope-nope")

    def test_retry_attempt_cap_refused(self):
        self._blocked()
        opf = batches.op_file(self.op_id)
        rec = read_json(opf)
        rec["attempts"] = [{"status": "blocked", "error": f"e{i}",
                            "at": f"t{i}", "gates": []}
                           for i in range(10)]
        write_json(opf, rec)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id,
                                        retry=True)
        self.assertIn("cap", str(ctx.exception))

    def test_retry_branch_with_commits_refused(self):
        rec = self._blocked()
        branch = rec["result"]["branch"]
        git("branch", "-f", branch, self.c1, cwd=self.origin)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id,
                                        retry=True)
        self.assertIn("own commits", str(ctx.exception))

    def test_retry_branch_in_use_refused(self):
        rec = self._blocked()
        branch = rec["result"]["branch"]
        holder = self.root / "holder-wt"
        git("worktree", "add", str(holder), branch, cwd=self.origin)
        self.addCleanup(git, "worktree", "remove", "--force",
                        str(holder), cwd=self.origin)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id,
                                        retry=True)
        self.assertIn("checked out", str(ctx.exception))

    def test_retry_path_exists_refused(self):
        rec = self._blocked()
        Path(rec["result"]["path"]).mkdir(parents=True, exist_ok=True)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id,
                                        retry=True)
        self.assertIn("still exists", str(ctx.exception))

    def test_retry_missing_branch_name_refused(self):
        self._blocked()
        opf = batches.op_file(self.op_id)
        rec = read_json(opf)
        rec["result"]["branch"] = ""
        write_json(opf, rec)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id,
                                        retry=True)
        self.assertIn("no branch", str(ctx.exception))

    def test_retry_dispatches_prepare(self):
        self._blocked()
        out = batches.retry_integration_op(self.op_id)
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(len(out["attempts"]), 1)

    def test_api_retry_dispatch_and_confirm(self):
        from openmainty import serve
        from unittest import mock as _mock
        self._blocked()
        with _mock.patch.object(serve, "_require_gate",
                                lambda: None):
            out = serve.api_integration_reresolve(
                {"op": self.op_id, "confirm": True, "retry": True})
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(len(out["attempts"]), 1)
        with _mock.patch.object(serve, "_require_gate",
                                lambda: None):
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_integration_reresolve(
                    {"op": self.op_id, "retry": True})
        self.assertEqual(ctx.exception.status, 400)


class RetryDerivedTests(LockfixMixin):
    MODE = "manifest"

    def test_retry_blocked_derived_rearchives(self):
        self.assertEqual(
            batches.prepare_integration(self.run_id,
                                        self.batch_id)["status"],
            "blocked")
        first = batches.reresolve_on_base(self.op_id)
        self.assertEqual(first["status"], "blocked")
        second = batches.retry_integration_op(first["op_id"])
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(len(second["attempts"]), 1)
        self.assertIn("own range line", second["attempts"][0]["error"])


class ChainUnitTests(unittest.TestCase):
    def test_three_attempts_keep_first(self):
        r1 = {"status": "blocked", "error": "first", "updated_at": "t1",
              "gates": []}
        n2 = {}
        batches._push_attempt(n2, r1)
        r2 = {"status": "blocked", "error": "second",
              "updated_at": "t2", "gates": [],
              "attempts": n2["attempts"]}
        n3 = {}
        batches._push_attempt(n3, r2)
        self.assertEqual([a["error"] for a in n3["attempts"]],
                         ["first", "second"])

    def test_legacy_chain_carries(self):
        legacy = {"status": "blocked", "error": "old",
                  "updated_at": "t0", "gates": [],
                  "attempts": [{"status": "blocked", "error": "older",
                                "at": "t-1", "gates": []}]}
        rec: dict = {}
        batches._push_attempt(rec, legacy)
        self.assertEqual([a.get("error") or a.get("at")
                          for a in rec["attempts"]],
                         ["older", "old"])


class DashboardS31Tests(unittest.TestCase):
    HTML = (Path(__file__).resolve().parent.parent / "src"
            / "openmainty" / "dashboard.html").read_text(
                encoding="utf-8")

    def test_rr_labels(self):
        self.assertIn("잠금 파일만 충돌 — 기준 위에서 잠금 다시 풀기",
                      self.HTML)
        self.assertIn("선언 줄 인접 충돌 — 의존성 선언만 다시 적용하고 "
                      "잠금 다시 풀기", self.HTML)
        self.assertIn("충돌 해소 시도 — 서버가 범위를 확인", self.HTML)

    def test_derived_chip_gated(self):
        self.assertIn("파생 시도 막힘", self.HTML)
        self.assertIn("파생 준비됨(원본과 다름)", self.HTML)

    def test_retry_button(self):
        self.assertIn('id="prep-retry-go"', self.HTML)
        self.assertIn("다시 시도", self.HTML)
        self.assertIn("retry: true", self.HTML)


class AttemptHistoryTests(unittest.TestCase):
    def test_reconcile_archives_interrupted_attempt(self):
        import tempfile as _tf
        tmp = _tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        use_temp_home(self, str(Path(tmp.name) / "home"))
        opf = batches.op_file("prep-runX-batchY")
        rec = {"op": "prepare", "op_id": "prep-runX-batchY",
               "status": "started", "error": "",
               "updated_at": "2026-10-19T00:00:00+00:00",
               "gates": [{"key": "source-prepared", "status": "pending",
                          "note": ""}],
               "result": {"path": str(Path(tmp.name) / "missing")}}
        write_json(opf, rec)
        out = batches._reconcile_existing(
            opf, rec, Path(tmp.name) / "missing")
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(len(out["attempts"]), 1)
        self.assertEqual(out["attempts"][0]["status"], "started")

    def test_attempts_bounded(self):
        rec: dict = {}
        for i in range(12):
            batches._push_attempt(
                rec, {"status": "blocked", "error": f"e{i}",
                      "updated_at": f"2026-10-19T00:00:{i:02d}+00:00",
                      "gates": []})
        self.assertEqual(len(rec["attempts"]), 10)
        self.assertEqual(rec["attempts"][0]["error"], "e2")
        self.assertEqual(rec["attempts"][-1]["error"], "e11")

    def test_replay_failed_at_recorded(self):
        saved_home = os.environ.get("OPENMAINTY_HOME")
        saved_path = os.environ.get("PATH", "")
        mix = LockfixMixin("blocked_op")
        mix.setUp()
        self.addCleanup(mix.tmp.cleanup)
        self.addCleanup(os.environ.__setitem__, "PATH", saved_path)
        if saved_home is None:
            self.addCleanup(os.environ.pop, "OPENMAINTY_HOME")
        else:
            self.addCleanup(os.environ.__setitem__, "OPENMAINTY_HOME",
                            saved_home)
        # mix.setUp() registers on mix's own stack, which never runs
        # (no tearDown for a manual setUp): stop the fixture disk
        # patch here or it leaks into later tests in this process.
        self.addCleanup(mix._disk_patcher.stop)
        for var in ("RC", "FOO", "BAR"):
            self.addCleanup(mix.cfg_pop, var)
        git("checkout", "-qb", "two", mix.c0, cwd=mix.origin)
        (mix.origin / "f0.txt").write_text("v0\n", encoding="utf-8")
        git("add", "-A", cwd=mix.origin)
        git("commit", "-qm", "first", cwd=mix.origin)
        (mix.origin / "yarn.lock").write_text(
            lock_text(FOO_V1).replace('version "1.0.0"',
                                      'version "9.9.9"', 1),
            encoding="utf-8")
        git("add", "-A", cwd=mix.origin)
        git("commit", "-qm", "second", cwd=mix.origin)
        tip = git("rev-parse", "HEAD", cwd=mix.origin).strip()
        run_work = paths.runs_dir() / mix.run_id / "work"
        git("worktree", "remove", "--force", str(run_work),
            cwd=mix.origin)
        git("worktree", "add", "--detach", str(run_work), tip,
            cwd=mix.origin)
        snap = runner.source_snapshot(run_work)
        state = store.load(mix.run_id)
        state.update(workCommit=tip,
                     workTreeFingerprint=snap["fingerprint"],
                     verifiedCommits={"review": tip, "e2e": tip},
                     verifiedFingerprints={
                         "review": snap["fingerprint"],
                         "e2e": snap["fingerprint"]})
        store.save(state)
        rec = batches.prepare_integration(mix.run_id, mix.batch_id)
        self.assertEqual(rec["status"], "blocked")
        failed = rec["result"]["replay_failed_at"]
        self.assertEqual((failed["pos"], failed["total"]), (2, 2))
        self.assertEqual(failed["subject"], "second")


class PubspecConflictScopeTests(unittest.TestCase):
    """M3.3a prepare-only: pub conflicts never enter the yarn-only
    regen — pubspec.lock must not read as JS lock-only, and
    pubspec.yaml is not a re-resolvable manifest."""

    def test_pubspec_lock_is_other(self):
        self.assertEqual(
            batches.classify_conflict_scope(["pubspec.lock"], "intl",
                                            {}), "other")

    def test_pubspec_yaml_is_other(self):
        self.assertEqual(
            batches.classify_conflict_scope(["pubspec.yaml"], "intl",
                                            {}), "other")

    def test_mixed_yarn_and_pub_lock_is_other(self):
        self.assertEqual(
            batches.classify_conflict_scope(
                ["yarn.lock", "example/pubspec.lock"], "intl", {}),
            "other")

    def test_yarn_lock_only_stays_lock_only(self):
        self.assertEqual(
            batches.classify_conflict_scope(["yarn.lock"], "foo", {}),
            "lock-only")

    def test_scope_refuses_pub_before_any_git(self):
        rec = {"op": "prepare", "status": "blocked",
               "error": "cherry-pick 충돌: pubspec.lock",
               "dep": "intl",
               "result": {"conflicted_paths": ["pubspec.lock"]},
               "base": {"checkout": "/nonexistent",
                        "head": "a" * 40},
               "candidate": {"head": "b" * 40}}
        out = batches.prepare_reresolve_scope(rec)
        self.assertFalse(out["eligible"])
        self.assertEqual(out["scope"], "other")
        self.assertIn("pubspec", out["reason"])


class JointFixture(unittest.TestCase):
    # M2.14: an RN-group bump (react-native + metro-config) and an
    # expo bump that only ship together. No full prepare runs —
    # the ops are recorded directly; the SHIM yarn answers the
    # joint regen. Berry npm: descriptors throughout.
    MODE = "joint"  # or "jointmixed" (retained old metro stanza)
    # or "jointconflict" (Y also edits the RN line)
    # or "jointpindiff" (Y pins metro-config 0.88.0 to 0.88.1 in v1
    # bare form — a cross-form pin conflict with X's berry stanza)

    X_RUN = "run-jointx"
    Y_RUN = "run-jointy"
    BATCH = "base-1"

    def _pkg(self, rn, metro, expo, neighbor):
        return json.dumps({"devDependencies": {
            "react-native": rn, "@react-native/metro-config": metro,
            "expo": expo, "neighbor": neighbor}}, indent=2) + "\n"

    def _base_lock(self):
        extra = J_METRO_CARET_V1 if self.MODE == "jointmixed" else ""
        return (META + J_RN_V1 + J_METRO_V1 + extra + J_CODEGEN_V1
                + J_EXPO_V1 + J_EXPOCLI_V1 + J_NEIGH_V1)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        self.old_path = os.environ.get("PATH", "")
        bindir = self.root / "bin"
        bindir.mkdir()
        shim = bindir / "yarn"
        shim.write_text(SHIM, encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        os.environ["PATH"] = str(bindir) + ":" + self.old_path
        self.addCleanup(os.environ.__setitem__, "PATH", self.old_path)
        self._disk_patcher = mock.patch.object(
            batches, "_regen_disk_ok",
            return_value=(True, "픽스처 99.0GB"))
        self._disk_patcher.start()
        self.addCleanup(self._disk_patcher.stop)
        cfgdir = bindir / "cfg"
        cfgdir.mkdir(exist_ok=True)
        (cfgdir / "RC").write_text("0", encoding="utf-8")
        (cfgdir / "FOO").write_text("0.88.0", encoding="utf-8")
        (cfgdir / "BAR").write_text("0", encoding="utf-8")
        (cfgdir / "GROUP").write_text("1", encoding="utf-8")
        (cfgdir / "GROUP_DEPS").write_text(
            "react-native:0.88.0\n@react-native/metro-config:0.88.0\n"
            "expo:58.0.17\n",
            encoding="utf-8")
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git("init", "-b", "main", cwd=self.origin)
        git("config", "user.email", "test@localhost", cwd=self.origin)
        git("config", "user.name", "test", cwd=self.origin)
        (self.origin / "package.json").write_text(
            self._pkg("0.86.2", "0.86.2", "^57.0.11", "^1.0.0"),
            encoding="utf-8")
        (self.origin / "yarn.lock").write_text(self._base_lock(),
                                               encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "c0", cwd=self.origin)
        git("checkout", "-qb", "x", cwd=self.origin)
        (self.origin / "package.json").write_text(
            self._pkg("0.88.0", "0.88.0", "^57.0.11", "^1.0.0"),
            encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            META + J_RN_NEW + J_METRO_NEW + J_CODEGEN_NEW
            + J_CODEGEN_V1 + J_EXPO_V1 + J_EXPOCLI_V1 + J_NEIGH_V1,
            encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "candidate rn group", cwd=self.origin)
        self.c1x = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        git("checkout", "-qb", "y", cwd=self.origin)
        y_rn = "0.87.0" if self.MODE == "jointconflict" else "0.86.2"
        (self.origin / "package.json").write_text(
            self._pkg(y_rn, "0.86.2", "^58.0.17", "^1.0.0"),
            encoding="utf-8")
        y_extra = (J_METRO_BARE_DIFF
                   if self.MODE == "jointpindiff" else "")
        (self.origin / "yarn.lock").write_text(
            META + J_RN_V1 + J_METRO_V1 + J_CODEGEN_V1 + J_CODEGEN_NEW
            + J_EXPO_NEW + J_EXPOCLI_NEW + J_NEIGH_V1 + y_extra,
            encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "candidate expo", cwd=self.origin)
        self.c1y = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        (self.origin / "package.json").write_text(
            self._pkg("0.86.2", "0.86.2", "^57.0.11", "^1.0.1"),
            encoding="utf-8")
        git("commit", "-qam", "c2 neighbor", cwd=self.origin)
        self.c2 = git("rev-parse", "HEAD", cwd=self.origin).strip()
        self.base_wt = self.root / "basewt"
        proc = subprocess.run(
            ["git", "clone", "--no-hardlinks", str(self.origin),
             str(self.base_wt)], capture_output=True, text=True,
            timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        snap = batches._snapshot(str(self.base_wt))
        self.assertEqual(snap["head"], self.c2)
        for run, dep, target in (
                (self.X_RUN, "react-native", "0.88.0"),
                (self.Y_RUN, "expo", "58.0.17")):
            store.save({"id": run, "repo": "fakerepo", "dep": dep,
                        "target": target, "lane": "next"})
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["repo"] = "fakerepo"
            st["queue"] = [
                {"run_id": self.X_RUN, "lane": "next",
                 "kind": "dep-update", "dep": "react-native",
                 "target": "0.88.0", "verified": True},
                {"run_id": self.Y_RUN, "lane": "next",
                 "kind": "dep-update", "dep": "expo",
                 "target": "58.0.17", "verified": True}]
            sched.save(st)
        self._base_mocks = mock.patch.object(
            batches, "default_base",
            return_value={"id": self.BATCH, "kin": 1, "tie": 0,
                          "via_ancestor": False})
        self._base_mocks.start()
        self.addCleanup(self._base_mocks.stop)
        self._load_mocks = mock.patch.object(
            batches, "load_batch",
            return_value={"id": self.BATCH, "integrations": [
                {"lane": "next", "head": self.c2,
                 "worktree": str(self.base_wt)}]})
        self._load_mocks.start()
        self.addCleanup(self._load_mocks.stop)
        self.x_op = self._op(self.X_RUN, "react-native", "0.88.0",
                             self.c1x)
        self.y_op = self._op(self.Y_RUN, "expo", "58.0.17", self.c1y)

    def _op(self, run, dep, target, commit):
        snap = batches._snapshot(str(self.base_wt))
        op_id = "prep-%s-%s" % (run, self.BATCH)
        write_json(batches.op_file(op_id), {
            "schema": 1, "op": "prepare", "op_id": op_id,
            "repo": "fakerepo", "run_id": run, "batch_id": self.BATCH,
            "dep": dep, "target": target, "lane": "next",
            "status": "blocked", "error": "cherry-pick conflicted",
            "created_at": snap.get("updated_at", ""),
            "updated_at": snap.get("updated_at", ""),
            "candidate": {"head": commit, "fingerprint": "c",
                          "branch": "codex/x", "worktree": ""},
            "base": {"batch": self.BATCH, "lane": "next",
                     "head": self.c2,
                     "fingerprint": snap["fingerprint"],
                     "checkout": str(self.base_wt)},
            "result": {"path": "", "branch": "", "head": "",
                       "fingerprint": ""}})
        return op_id

    def joint(self, by="root"):
        return batches.joint_retry(self.X_RUN, self.Y_RUN, self.BATCH,
                                   by=by)


class JointDetectTests(JointFixture):
    def test_detect_finds_expo_partner(self):
        x_rec = read_json(batches.op_file(self.x_op), default=None)
        hint = m2.detect_joint_partner("fakerepo", "next", x_rec)
        self.assertIsNotNone(hint)
        self.assertEqual(hint["partner_run"], self.Y_RUN)
        self.assertEqual(hint["partner_op"], self.y_op)
        self.assertEqual(hint["pair"], "0.86+0.88")
        # Cached on the op record; a second call reuses it.
        again = m2.detect_joint_partner("fakerepo", "next", x_rec)
        self.assertEqual(again["partner_op"], self.y_op)

    def test_detect_reverse_finds_rn(self):
        y_rec = read_json(batches.op_file(self.y_op), default=None)
        hint = m2.detect_joint_partner("fakerepo", "next", y_rec)
        self.assertIsNotNone(hint)
        self.assertEqual(hint["partner_run"], self.X_RUN)
        self.assertEqual(hint["partner_op"], self.x_op)

    def test_core_rules(self):
        old, new = (0, 86), (0, 88)
        base = {"codegen": {old: {"e@npm:^57"}}}
        x = {"codegen": {old: {"e@npm:^57"},
                         new: {"codegen@npm:0.88.0"}}}
        y = {"codegen": {old: {"r@npm:0.86.2"},
                         new: {"e@npm:^58"}}}
        base2 = {"codegen": {old: {"r@npm:0.86.2"}}}
        both = {"codegen": {old: {"e@npm:^57", "r@npm:0.86.2"}}}
        self.assertEqual(
            m2.joint_pair_match("react-native", "0.88.0", x, "expo",
                                "58.0.17", y, both),
            (old, new))
        # Same direction (two RN members) -> no hint.
        self.assertIsNone(m2.joint_pair_match(
            "@react-native/metro-config", "0.88.0", x,
            "@react-native/babel-preset", "0.88.0", y, both))
        # Different minor pair -> no hint.
        self.assertIsNone(m2.joint_pair_match(
            "react-native", "0.88.0", x, "expo", "58.0.17",
            {"codegen": {old: {"e"}, (0, 87): {"f"}}}, both))
        # Neither side targets the new minor -> no hint.
        self.assertIsNone(m2.joint_pair_match(
            "react-native", "0.86.0", x, "expo", "57.0.11", y, both))
        # Missing retained old / missing added new -> no hint.
        self.assertIsNone(m2.joint_pair_match(
            "react-native", "0.88.0",
            {"codegen": {new: {"codegen@npm:0.88.0"}}},
            "expo", "58.0.17", y, both))
        self.assertIsNone(m2.joint_pair_match(
            "react-native", "0.88.0", x, "expo", "58.0.17",
            {"codegen": {old: {"r@npm:0.86.2"}}}, both))


class JointRetryTests(JointFixture):
    def test_joint_retry_derives_done(self):
        out = self.joint()
        self.assertEqual(out["status"], "done", out.get("error"))
        self.assertEqual(out["kind"], "joint")
        self.assertEqual(out["op"], "reresolve")
        self.assertEqual(out["derived_from"], self.x_op)
        self.assertEqual(out["joint_with"], [self.y_op])
        self.assertEqual(out["runs"], [self.X_RUN, self.Y_RUN])
        entries = out["group"]
        self.assertIn("react-native 0.86.2->0.88.0", entries)
        self.assertIn("expo ^57.0.11->^58.0.17", entries)
        res = out["result"]
        lock = (Path(res["path"]) / "yarn.lock").read_text(
            encoding="utf-8")
        for needle in ('react-native@npm:0.88.0:',
                       '@react-native/metro-config@npm:0.88.0:',
                       'expo@npm:^58.0.17:',
                       'version "0.88.0"', 'version "58.0.17"'):
            self.assertIn(needle, lock)
        pkg = json.loads((Path(res["path"]) / "package.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(pkg["devDependencies"]["react-native"],
                         "0.88.0")
        self.assertEqual(pkg["devDependencies"]["expo"], "^58.0.17")
        # Single-minor tree: the mix preflight passed.
        dupes = batches.lock_duplicate_versions(str(self.base_wt),
                                                res["path"])
        self.assertIsNone(m2.rn_group_mixed(dupes))
        # Both runs now read the joint record.
        for run in (self.X_RUN, self.Y_RUN):
            latest = batches.latest_integration_op(run)
            self.assertEqual(latest["op_id"], out["op_id"])
        head_msg = git("log", "-1", "--format=%B",
                       cwd=res["path"])
        self.assertTrue(head_msg.startswith("chore(deps): "),
                        head_msg)
        self.assertIn(self.c1x[:12], head_msg)
        self.assertIn(self.c1y[:12], head_msg)

    def test_joint_retry_with_rederive_present(self):
        # Live shape: X has a blocked prepare AND a done -rederive
        # whose record carries the batch-stage joint_hint. The
        # joint retry still anchors on the blocked prepare, fresh
        # detection names the same partner, and the joint derives
        # done.
        x_rec = read_json(batches.op_file(self.x_op), default=None)
        hint = m2.detect_joint_partner("fakerepo", "next", x_rec)
        self.assertIsNotNone(hint)
        snap = batches._snapshot(str(self.base_wt))
        r_id = self.x_op + "-rederive"
        write_json(batches.op_file(r_id), {
            "schema": 1, "op": "reresolve", "op_id": r_id,
            "derived_from": self.x_op, "kind": "rederive",
            "repo": "fakerepo", "run_id": self.X_RUN,
            "batch_id": self.BATCH, "dep": "react-native",
            "target": "0.88.0", "lane": "next",
            "status": "done", "error": "",
            "created_at": snap.get("updated_at", ""),
            "updated_at": snap.get("updated_at", ""),
            "joint_hint": hint,
            "result": {"path": str(self.base_wt), "branch": "codex/r",
                       "head": self.c2, "fingerprint": "f"}})
        out = self.joint()
        self.assertEqual(out["status"], "done", out.get("error"))
        self.assertEqual(out["joint_with"], [self.y_op])

    def test_joint_latest_falls_back_when_blocked(self):
        out = self.joint()
        self.assertEqual(out["status"], "done")
        blocked = dict(out, status="blocked",
                       error="x", updated_at=out["updated_at"])
        write_json(batches.op_file(out["op_id"]), blocked)
        for run in (self.X_RUN, self.Y_RUN):
            latest = batches.latest_integration_op(run)
            self.assertNotEqual(latest["op_id"], out["op_id"])

    def test_joint_refusals(self):
        with self.assertRaises(batches.IntegrationError):
            batches.joint_retry(self.X_RUN, self.Y_RUN, self.BATCH,
                                by="worker")
        with self.assertRaises(batches.IntegrationError):
            batches.joint_retry(self.X_RUN, self.X_RUN, self.BATCH,
                                by="root")

    def test_joint_items_and_supersede_map(self):
        import time
        x_rec = read_json(batches.op_file(self.x_op), default=None)
        hint = m2.detect_joint_partner("fakerepo", "next", x_rec)
        self.assertIsNotNone(hint)
        items = batches._m1_joint_items("fakerepo", time.time())
        # M2.14b-fix: both entries point at X's run (the card with
        # the button); one names the partner, one names X itself.
        self.assertEqual([i["ref"] for i in items],
                         [self.X_RUN, self.X_RUN])
        self.assertEqual(sorted(i["question"] for i in items),
                         sorted(["expo 58.0.17과 함께 올려야 함 — "
                                 "'함께 다시 시도'로 진행",
                                 "react-native 0.88.0과 함께 올려야 함 — "
                                 "'함께 다시 시도'로 진행"]))
        for item in items:
            self.assertEqual(item["kind"], "joint-retry")
        out = self.joint()
        self.assertEqual(out["status"], "done", out.get("error"))
        sup = batches._joint_supersede_map()
        self.assertEqual(
            sup.get(m2.batch_id_for_op(self.y_op)), out["op_id"])


class JointStillMixedTests(JointFixture):
    MODE = "jointmixed"

    def test_still_mixed_blocks_honestly(self):
        out = self.joint()
        self.assertEqual(out["status"], "blocked")
        self.assertIn("함께 올려도 RN 혼재 남음", out["error"])
        self.assertIn("nothing committed", out["error"])
        res = out["result"]
        self.assertEqual(res["head"], "")
        self.assertFalse(Path(res["path"]).exists())
        self.assertFalse(_branch_exists(self.base_wt,
                                        res["branch"]))


class JointConflictTests(JointFixture):
    MODE = "jointconflict"

    def test_same_line_conflict_refused(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            self.joint()
        self.assertIn("different values", str(ctx.exception))
        self.assertFalse(list(
            Path(self.root / "home" / "reviews").glob("*joint*")))


class JointPinDiffTests(JointFixture):
    MODE = "jointpindiff"

    def test_cross_form_pin_conflict_refused(self):
        out = self.joint()
        self.assertEqual(out["status"], "blocked")
        self.assertIn("differently", out["error"])
        self.assertIn("@react-native/metro-config", out["error"])
        self.assertIn("nothing committed", out["error"])
        res = out["result"]
        self.assertEqual(res["head"], "")
        self.assertFalse(Path(res["path"]).exists())
        self.assertFalse(_branch_exists(self.base_wt,
                                        res["branch"]))


def _branch_exists(worktree, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--verify", branch],
        capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


if __name__ == "__main__":
    unittest.main()
