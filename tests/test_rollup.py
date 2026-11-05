"""Lane rollup: merge READY same-lane leaf batches (S4).

Covers batches.rollup_leaves with real git fixture repos under an
isolated OPENMAINTY_HOME and a fake berry-yarn shim on PATH (no
registry, no network):

- 3 leaves (two manifest bumps + one lock-only refresh) replay
  oldest-first onto the common base, regenerate the lock ONCE, pass
  the union closure guard and per-leaf declared-resolution checks,
  and record a done op with a copyable-only PR draft.
- A non-lock conflict blocks naming the leaf batch + commit + paths,
  with the worktree cleaned up.
- A closure-guard violation blocks with "관련 없는 잠금 이동" and
  nothing committed; an explicit retry after fixing the cause
  archives the blocked attempt (P-HIST) and reaches done.
- Two lock-only leaves take the lock-refresh path (dep stanzas
  dropped before regenerate).
- Endpoint payload validation (confirm/fields/unknown batch/lease).
- The dashboard rollupCandidates() pure helper under node.

Py3.9 portable (no 3.10+ syntax in this file).
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, paths, runner, store  # noqa: E402
from openmainty.util import read_json, write_json  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(
            "git %s failed: %s" % (" ".join(args), proc.stderr[-300:]))
    return proc.stdout


META = '''__metadata:
  version: 8
  cacheKey: 8

'''

FOO = '''foo@^1.0.0:
  version "1.0.1"
  resolved "https://registry.yarnpkg.com/foo/-/foo-1.0.1.tgz#bbb"
  dependencies:
    mini "1.0.0"

'''

BAR = '''bar@^2.0.0:
  version "2.0.0"
  resolved "https://registry.yarnpkg.com/bar/-/bar-2.0.0.tgz#ddd"

'''

QUX = '''qux@^3.0.0:
  version "3.0.0"
  resolved "https://registry.yarnpkg.com/qux/-/qux-3.0.0.tgz#q0"

'''

MINI = '''mini@1.0.0:
  version "1.0.0"
  resolved "https://registry.yarnpkg.com/mini/-/mini-1.0.0.tgz#fff"

'''

ROGUE = '''rogue@^1.0.0:
  version "1.0.0"
  resolved "https://registry.yarnpkg.com/rogue/-/rogue-1.0.0.tgz#r0"

'''

BASE_PKG = {"devDependencies": {"foo": "^1.0.0", "bar": "^2.0.0",
                                "qux": "^3.0.0"}}


def lock_text(foo=FOO, bar=BAR, qux=QUX):
    return META + foo + bar + qux + MINI + ROGUE


SHIM = '''#!/bin/sh
# Fake berry yarn for rollup tests: --version, config get, and install
# --mode=update-lockfile only. Refuses script-enabled or modeless runs.
# Test config arrives via files next to the shim (cfg/VER_<DEP>,
# cfg/ROGUE, cfg/RC, cfg/LOG) — never via env, because the regen runs
# under the scrubbed M1 env which drops every FAKE_* var. The shim
# re-exports them for its own embedded python only.
CFG="$(dirname "$0")/cfg"
cfg() { cat "$CFG/$1" 2>/dev/null; }
FAKE_YARN_RC="$(cfg RC)"; export FAKE_YARN_RC
FAKE_ROGUE="$(cfg ROGUE)"; export FAKE_ROGUE
for _d in FOO BAR QUX; do
  _v="$(cfg VER_"$_d")"
  [ -n "$_v" ] && export "FAKE_VER_$_d=$_v"
done
FAKE_YARN_LOG="$(cfg LOG)"; export FAKE_YARN_LOG
unset _d _v
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
{
  echo "argv: $*"
  for d in foo bar qux; do
    if grep -q "^$d@" yarn.lock; then echo "saw:$d"; else echo "miss:$d"; fi
  done
} >> "$FAKE_YARN_LOG"
python3 - <<'PYEOF'
import json
import os
text = open("yarn.lock", encoding="utf-8").read()
pkg = json.load(open("package.json", encoding="utf-8"))
dev = pkg.get("devDependencies", {})
parts = text.split("\\n\\n")
out = []
for stanza in parts:
    lines = stanza.split("\\n")
    head = lines[0] if lines else ""
    dep = head.split("@")[0] if "@" in head else ""
    ver = os.environ.get("FAKE_VER_" + dep.upper(), "")
    if dep in ("foo", "bar", "qux") and ver:
        rng = dev.get(dep, "")
        if rng:
            lines[0] = "%s@%s:" % (dep, rng)
        for i, ln in enumerate(lines[1:], 1):
            s = ln.strip()
            if s.startswith('version "'):
                lines[i] = '  version "%s"' % ver
            elif s.startswith("resolved "):
                lines[i] = '  resolved "https://registry.yarnpkg.com/%s/-/%s-%s.tgz#gen"' % (dep, dep, ver)
        stanza = "\\n".join(lines)
    if dep == "rogue" and os.environ.get("FAKE_ROGUE", "") == "1":
        stanza = stanza.replace('version "1.0.0"', 'version "2.0.0"')
        stanza = stanza.replace("#r0", "#r1")
    out.append(stanza)
text = "\\n\\n".join(out)
for dep in ("foo", "bar", "qux"):
    ver = os.environ.get("FAKE_VER_" + dep.upper(), "")
    if ver and ("\\n%s@" % dep) not in text and not text.startswith("%s@" % dep):
        rng = dev.get(dep, "")
        text = text.rstrip("\\n") + "\\n\\n%s@%s:\\n  version \\"%s\\"\\n  resolved \\"https://registry.yarnpkg.com/%s/-/%s-%s.tgz#gen\\"\\n" % (dep, rng, ver, dep, dep, ver)
open("yarn.lock", "w", encoding="utf-8").write(text)
PYEOF
exit $?
'''


def _pkg(foo="^1.0.0", bar="^2.0.0", qux="^3.0.0"):
    return json.dumps({"devDependencies": {"foo": foo, "bar": bar,
                                           "qux": qux}},
                      indent=2) + "\n"


def _foo_lock(version="1.2.3", marker="#ccc", rng="^1.2.0"):
    return ('foo@%s:\n  version "%s"\n  resolved '
            '"https://registry.yarnpkg.com/foo/-/foo-%s.tgz%s"\n'
            '  dependencies:\n    mini "1.0.0"\n\n'
            % (rng, version, version, marker))


def _bar_lock(version="2.1.0", marker="#eee", rng="^2.1.0"):
    return ('bar@%s:\n  version "%s"\n  resolved '
            '"https://registry.yarnpkg.com/bar/-/bar-%s.tgz%s"\n\n'
            % (rng, version, version, marker))


def _qux_lock(version="3.0.1", marker="#q1", rng="^3.0.0"):
    return ('qux@%s:\n  version "%s"\n  resolved '
            '"https://registry.yarnpkg.com/qux/-/qux-%s.tgz%s"\n\n'
            % (rng, version, version, marker))


class RollupMixin(unittest.TestCase):
    """Origin @ c0 (= base head); one branch + tip per leaf dep.

    MODE selects the leaf shapes:
    - "trio": foo (manifest bump ^1.2.0), bar (manifest bump ^2.1.0),
      qux (lock-only 3.0.0 -> 3.0.1).
    - "conflict": two leaves touching README.md with different bytes.
    - "lockonly": two lock-only leaves (bar 2.0.0 -> 2.0.1,
      qux 3.0.0 -> 3.0.1) for the lock-refresh path.
    """

    MODE = "trio"

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
        # covered by RegenDiskGuardTests with injected readings).
        _disk = mock.patch.object(
            batches, "_regen_disk_ok",
            return_value=(True, "픽스처 99.0GB"))
        _disk.start()
        self.addCleanup(_disk.stop)
        self.cfgdir = bindir / "cfg"
        self.cfgdir.mkdir(exist_ok=True)
        for var in ("RC", "VER_FOO", "VER_BAR", "VER_QUX", "ROGUE",
                    "LOG"):
            self.cfg_pop(var)
        self.cfg_set("VER_FOO", "1.2.3")
        self.cfg_set("VER_BAR", "2.1.0")
        self.cfg_set("VER_QUX", "3.0.1")
        self.cfg_set("LOG", str(self.root / "yarn.log"))
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git("init", "-b", "main", cwd=self.origin)
        git("config", "user.email", "test@localhost", cwd=self.origin)
        git("config", "user.name", "test", cwd=self.origin)
        (self.origin / "package.json").write_text(
            _pkg(), encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            lock_text(), encoding="utf-8")
        (self.origin / "README.md").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "c0", cwd=self.origin)
        self.c0 = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("branch", "-f", "next", cwd=self.origin)
        self.leaf_base = self.c0
        if self.MODE == "stacked":
            # A base-chain commit between the stale target (c0) and
            # the base head: the draft must attribute it to its batch.
            (self.origin / "chain.txt").write_text("chain\n")
            git("add", "chain.txt", cwd=self.origin)
            git("commit", "-qm", "base chain work", cwd=self.origin)
            self.chain_sha = git("rev-parse", "HEAD",
                                 cwd=self.origin).strip()
            self.leaf_base = self.chain_sha
        self.base_wt = self.root / "home" / "base-wt"
        git("worktree", "add", "--detach", str(self.base_wt),
            self.leaf_base, cwd=self.origin)
        if self.MODE == "stacked":
            (self.base_wt / "chain2.txt").write_text("base\n")
            git("add", "chain2.txt", cwd=self.base_wt)
            git("commit", "-qm", "base integration work",
                cwd=self.base_wt)
            self.leaf_base = git("rev-parse", "HEAD",
                                 cwd=self.base_wt).strip()
            git("branch", "-f", "next", self.c0, cwd=self.origin)
            self.gp_wt = self.root / "home" / "gp-wt"
            git("worktree", "add", "--detach", str(self.gp_wt),
                self.chain_sha, cwd=self.origin)
        snap = runner.source_snapshot(self.base_wt)
        self.base_snap = {"head": snap["head"],
                          "fingerprint": snap["fingerprint"]}
        self.leaf_defs = self._leaf_defs()
        self.tips = {}
        for name, spec in self.leaf_defs:
            git("checkout", "-qb", "leaf-" + name, self.leaf_base,
                cwd=self.origin)
            for rel, text in spec["files"].items():
                p = self.origin / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            git("add", "-A", cwd=self.origin)
            git("commit", "-qm", spec["subject"], cwd=self.origin)
            self.tips[name] = git("rev-parse", "HEAD",
                                  cwd=self.origin).strip()
            git("checkout", "-q", "main", cwd=self.origin)
        if self.MODE == "stacked":
            # PR1 spans main..base: pin main at the stale target so the
            # chain commits (gp + base) read as unreleased, like tonight
            # (next/main both stale at efd5e54). update-ref: main is
            # checked out in this worktree, so branch -f refuses.
            git("update-ref", "refs/heads/main", self.c0,
                cwd=self.origin)
        self.base_id = "test-rollup-base"
        base_head = git("rev-parse", "HEAD",
                        cwd=self.base_wt).strip()
        if self.MODE == "stacked":
            self.gp_id = "test-rollup-gp"
            gp_snap = runner.source_snapshot(self.gp_wt)
            self._write_batch(
                self.gp_id, dep="gp", target="0",
                head=self.chain_sha, worktree=self.gp_wt,
                snap={"head": gp_snap["head"],
                      "fingerprint": gp_snap["fingerprint"]},
                inherited=[],
                updated_at="2026-10-20T00:00:00Z",
                run="run-testrollup-gp")
        self._write_batch(
            self.base_id, dep="base", target="0",
            head=base_head, worktree=self.base_wt,
            snap=self.base_snap,
            inherited=([self.gp_id] if self.MODE == "stacked"
                       else []),
            updated_at="2026-10-20T00:00:00Z", run="run-testrollup0")
        self.leaf_ids = []
        for i, (name, spec) in enumerate(self.leaf_defs):
            bid = "test-rollup-leaf-%s" % name
            self.leaf_ids.append(bid)
            wt = self.root / ("integ-%s" % name)
            git("worktree", "add", "--detach", str(wt),
                self.tips[name], cwd=self.origin)
            snap = runner.source_snapshot(wt)
            self._write_batch(
                bid, dep=spec["dep"], target=spec["target"],
                head=self.tips[name], worktree=wt,
                snap={"head": snap["head"],
                      "fingerprint": snap["fingerprint"]},
                inherited=[self.base_id],
                updated_at="2026-10-20T00:00:%02dZ" % (i + 1),
                run="run-testrollup-%s" % name)

    def _leaf_defs(self):
        if self.MODE == "conflict":
            return [
                ("ra", {"dep": "foo", "target": "1.2.0",
                        "subject": "candidate foo readme A",
                        "files": {
                            "README.md": "A\n",
                            "package.json": _pkg(foo="^1.2.0"),
                            "yarn.lock": lock_text(
                                foo=_foo_lock())}}),
                ("rb", {"dep": "bar", "target": "2.1.0",
                        "subject": "candidate bar readme B",
                        "files": {
                            "README.md": "B\n",
                            "package.json": _pkg(bar="^2.1.0"),
                            "yarn.lock": lock_text(
                                bar=_bar_lock())}}),
            ]
        if self.MODE in ("mixed", "stacked"):
            # S4.2-a: one manifest bump (foo) plus one lock-only leaf
            # (bar, range unchanged) whose range admits a newer
            # version. Both rewrite the same cacheKey header line
            # differently, so the second cherry-pick conflicts on
            # yarn.lock and the tree resets to the base lock —
            # tonight's shape (lock-only leaves revert to base pins).
            return [
                ("foo", {"dep": "foo", "target": "1.2.3",
                         "subject": "candidate foo 1.0.1 -> 1.2.3",
                         "files": {
                             "package.json": _pkg(foo="^1.2.0"),
                             "yarn.lock": lock_text(
                                 foo=_foo_lock()).replace(
                                     "cacheKey: 8",
                                     "cacheKey: 9")}}),
                ("bar", {"dep": "bar", "target": "2.1.0",
                         "subject": "candidate bar lock 2.0.0 -> 2.0.1",
                         "files": {
                             "package.json": _pkg(),
                             "yarn.lock": lock_text(
                                 bar=_bar_lock(
                                     version="2.1.0", marker="#gen",
                                     rng="^2.0.0")).replace(
                                         "cacheKey: 8",
                                         "cacheKey: 10")}}),
            ]
        if self.MODE == "lockonly":
            return [
                ("bar", {"dep": "bar", "target": "2.0.1",
                         "subject": "candidate bar lock 2.0.0 -> 2.0.1",
                         "files": {
                             "package.json": _pkg(),
                             "yarn.lock": lock_text(
                                 bar=_bar_lock(
                                     version="2.0.1",
                                     marker="#gen",
                                     rng="^2.0.0"))}}),
                ("qux", {"dep": "qux", "target": "3.0.1",
                         "subject": "candidate qux lock 3.0.0 -> 3.0.1",
                         "files": {
                             "package.json": _pkg(),
                             "yarn.lock": lock_text(
                                 qux=_qux_lock())}}),
            ]
        return [
            ("foo", {"dep": "foo", "target": "1.2.3",
                     "subject": "candidate foo 1.0.1 -> 1.2.3",
                     "files": {
                         "package.json": _pkg(foo="^1.2.0"),
                         "yarn.lock": lock_text(foo=_foo_lock())}}),
            ("bar", {"dep": "bar", "target": "2.1.0",
                     "subject": "candidate bar 2.0.0 -> 2.1.0",
                     "files": {
                         "package.json": _pkg(bar="^2.1.0"),
                         "yarn.lock": lock_text(bar=_bar_lock())}}),
            ("qux", {"dep": "qux", "target": "3.0.1",
                     "subject": "candidate qux lock 3.0.0 -> 3.0.1",
                     "files": {
                         "package.json": _pkg(),
                         "yarn.lock": lock_text(qux=_qux_lock())}}),
        ]

    def _write_batch(self, bid, dep, target, head, worktree, snap,
                     inherited, updated_at, run):
        run_work = paths.runs_dir() / run / "work"
        if not run_work.exists():
            git("worktree", "add", "--detach", str(run_work), head,
                cwd=self.origin)
        csnap = runner.source_snapshot(run_work)
        state = store.create("fakerepo", "dep-%s-%s" % (dep, target),
                             "chore(deps): %s" % dep, head, {})
        state["id"] = run
        state["dep"] = dep
        state["target"] = target
        state["lane"] = "next"
        state["branch"] = "codex/next/%s" % dep
        state["workCommit"] = head
        state["workTreeFingerprint"] = csnap["fingerprint"]
        state["verifiedCommits"] = {"review": head, "e2e": head}
        state["verifiedFingerprints"] = {"review": csnap["fingerprint"],
                                         "e2e": csnap["fingerprint"]}
        state["steps"] = {"review": {"status": "done", "exit_code": 0},
                          "e2e": {"status": "done", "exit_code": 0}}
        store.save(state)
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        ev_dir = batches.evidence_dir(bid)
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "proof.txt").write_text("proof\n", encoding="utf-8")
        ev_sha = batches.sha256_file(ev_dir / "proof.txt")
        write_json(batches.batches_dir() / (bid + ".json"), {
            "schema": 3, "id": bid, "repo": "fakerepo",
            "target_branch": "next", "purpose": "test %s" % bid,
            "updated_at": updated_at,
            "primaries": [{
                "dep": dep, "target": target, "lane": "next",
                "run_id": run,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(run_work),
                           "head": head,
                           "fingerprint": csnap["fingerprint"]}}],
            "inherited": [{"batch": h} for h in inherited],
            "origin": {"checkout": str(self.base_wt),
                       "base_branch": "next", "base_head": self.c0},
            "integrations": [{
                "lane": "next", "worktree": str(worktree),
                "head": head, "fingerprint": snap["fingerprint"],
                "status": "integration-testing",
                "gates": [{
                    "key": "unit", "status": "pass",
                    "inputs": {"head": head,
                               "fingerprint": snap["fingerprint"]},
                    "evidence": [{
                        "label": "unit proof", "kind": "file",
                        "stored": True, "stored_path": "proof.txt",
                        "sha256": ev_sha, "bytes": 6}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})

    def cfg_set(self, key, value):
        (self.cfgdir / key).write_text(value, encoding="utf-8")

    def cfg_pop(self, key):
        try:
            (self.cfgdir / key).unlink()
        except OSError:
            pass

    def yarn_log(self):
        p = self.root / "yarn.log"
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8")

    def regen_runs(self):
        return self.yarn_log().count("argv: install")


class RollupDoneTests(RollupMixin):
    def test_three_leaves_done_single_regen(self):
        rec = batches.rollup_leaves(
            "fakerepo", "next", self.base_id,
            [self.leaf_ids[2], self.leaf_ids[0], self.leaf_ids[1]])
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["op"], "rollup")
        expect_op = batches._rollup_op_id("next", self.base_id,
                                          self.leaf_ids)
        self.assertEqual(rec["op_id"], expect_op)
        # Oldest updated_at first, regardless of input order.
        got = [leaf["batch"] for leaf in rec["leaves"]]
        self.assertEqual(got, self.leaf_ids)
        self.assertEqual(rec["base"]["head"], self.c0)
        # Single lockfile regeneration for the whole lane.
        self.assertEqual(self.regen_runs(), 1)
        by = rec["regenerated_by"]
        self.assertEqual(by["tool"], "yarn")
        self.assertEqual(by["argv"],
                         ["yarn", "install", "--mode=update-lockfile"])
        self.assertEqual(by["command_origin"], "rollup-manifest-bump")
        self.assertEqual(by["tool_version"], "4.9.2")
        self.assertEqual(by["registry_host"], "registry.yarnpkg.com")
        self.assertIn("YARN_ENABLE_SCRIPTS", by["env_flags"])
        # Per-leaf declared resolutions match each leaf's own lock.
        res = by["resolutions"]
        self.assertEqual(res["foo"], {"^1.2.0": "1.2.3"})
        self.assertEqual(res["bar"], {"^2.1.0": "2.1.0"})
        self.assertEqual(res["qux"], {"^3.0.0": "3.0.1"})
        # bar's manifest hunk sits adjacent to foo's, so git
        # conflicts and D1-d rebuilds exactly bar's range line (the
        # real @types/react shape); the lock resets to base and the
        # single end regen recomputes every dep from the manifests.
        self.assertEqual(by["manifest_reapplied"],
                         [{"file": "package.json", "dep": "bar",
                           "from": "^2.0.0", "to": "^2.1.0"}])
        # Result tree: all three manifests moved, one commit.
        res_path = Path(rec["result"]["path"])
        pkg = json.loads((res_path / "package.json").read_text(
            encoding="utf-8"))
        self.assertEqual(pkg["devDependencies"]["foo"], "^1.2.0")
        self.assertEqual(pkg["devDependencies"]["bar"], "^2.1.0")
        self.assertEqual(pkg["devDependencies"]["qux"], "^3.0.0")
        lock = (res_path / "yarn.lock").read_text(encoding="utf-8")
        self.assertIn('version "1.2.3"', lock)
        self.assertIn('version "2.1.0"', lock)
        self.assertIn('version "3.0.1"', lock)
        self.assertIn('version "1.0.0"', lock)  # rogue untouched
        head_msg = git("log", "-1", "--format=%B", cwd=res_path)
        self.assertIn("ROLLUP SOURCE", head_msg)
        self.assertIn("Never equals any single candidate", head_msg)
        self.assertTrue(rec["result"]["branch"].startswith(
            "codex/rollup-next-"))
        self.assertIn("ROLLUP SOURCE", rec["result"]["proof_note"])
        # Commit dispositions recorded per leaf commit.
        disp = {}
        for leaf in rec["leaves"]:
            for c in leaf["commits"]:
                disp[leaf["dep"]] = c["disposition"]
        self.assertEqual(disp, {"foo": "applied",
                                "bar": "manifest-rebuilt+lock-resolved",
                                "qux": "applied"})
        # Gates: source prepared passes with an honest line; the rest
        # stay pending for Root QA — no readiness is claimed.
        gates = {g["key"]: g["status"] for g in rec["gates"]}
        self.assertEqual(gates,
                         {"source-prepared": "pass",
                          "fastchecks": "pending", "native": "pending"})
        self.assertIn("3개 묶음 합침", rec["gates"][0]["note"])
        # PR draft is copyable text only: title, per-leaf moves,
        # owner push/pr commands. Never executed here.
        draft = rec["pr_draft"]
        self.assertIn("3 leaves onto %s" % self.base_id,
                      draft["title"])
        # S4 nit: the draft states the actual gate status, never that
        # fastchecks already ran. User-facing words avoid Root/ship.
        # S4.5: the default PR language is English (no repo profile).
        self.assertIn("Fastchecks: pending (before final review)",
                      draft["body"])
        self.assertIn("Root QA receipts are attached at import.",
                      draft["body"])
        self.assertNotIn("Fastchecks run on the rollup tree",
                         draft["body"])
        # Scoped D8 exception: the PR draft names the QA receipt owner
        # in exactly one sentence (explicit S4.3 wording); every other
        # user-facing line stays free of internal names.
        self.assertEqual(
            draft["body"].count("Root"), 1, draft["body"])
        self.assertIn("Root QA receipts are attached at import.",
                      draft["body"])
        for dep, move in (("foo", "package.json (^1.0.0 → ^1.2.0)"),
                          ("bar", "package.json (^2.0.0 → ^2.1.0)"),
                          ("qux", "lockfile-only (3.0.1, lock refresh)")):
            self.assertIn("- %s: %s" % (dep, move), draft["body"])
        self.assertIn("git push -u origin %s"
                      % rec["result"]["branch"], rec["owner_commands"][0])
        self.assertIn("gh pr create", rec["owner_commands"][1])
        self.assertNotIn("attempts", rec)

    def test_repost_reconciles_without_new_work(self):
        first = batches.rollup_leaves("fakerepo", "next", self.base_id,
                                      self.leaf_ids)
        runs_before = self.regen_runs()
        second = batches.rollup_leaves("fakerepo", "next", self.base_id,
                                       list(reversed(self.leaf_ids)))
        self.assertTrue(second.get("existing"))
        self.assertEqual(second["result"]["head"],
                         first["result"]["head"])
        self.assertEqual(self.regen_runs(), runs_before)


class RollupStaleSnapshotTests(RollupMixin):
    """S7 condition 3: the snapshot advises, the server decides.

    (a) drift -> refresh -> the list snapshot shows NOT READY with a
    real reason. (b) a stale READY snapshot after a live drift still
    refuses server-side with the live reason — refusal, never unsafe.
    """

    def _cards(self):
        snap = json.loads(
            batches.batches_snapshot_path().read_text(
                encoding="utf-8"))
        return {b["id"]: b for b in snap.get("batches") or []}, snap

    def test_refresh_after_drift_shows_not_ready(self):
        batches.refresh_all_snapshots("test")
        cards, _ = self._cards()
        self.assertEqual(cards["test-rollup-leaf-foo"]["ready"], 1)
        wt = self.root / "integ-foo"
        (wt / "drift.txt").write_text("drift\n", encoding="utf-8")
        git("add", "drift.txt", cwd=wt)
        git("commit", "-qm", "drift", cwd=wt)
        batches.refresh_all_snapshots("test2")
        cards, snap = self._cards()
        foo = cards["test-rollup-leaf-foo"]
        self.assertEqual(foo["ready"], 0)
        ff = (foo.get("evaluation") or {}).get("first_failure")
        self.assertIsNotNone(ff)
        self.assertTrue(ff.get("reasons"), ff)

    def test_stale_ready_rollup_refuses(self):
        batches.refresh_all_snapshots("test")
        cards, _ = self._cards()
        # The stale premise: the file still says READY after the drift.
        self.assertEqual(cards["test-rollup-leaf-foo"]["ready"], 1)
        wt = self.root / "integ-foo"
        (wt / "drift.txt").write_text("drift\n", encoding="utf-8")
        git("add", "drift.txt", cwd=wt)
        git("commit", "-qm", "drift", cwd=wt)
        stale, _ = self._cards()
        self.assertEqual(stale["test-rollup-leaf-foo"]["ready"], 1)
        # Refusal, never unsafe: the readiness gate raises server-side
        # (serve maps it to 409 with the live reason) before any
        # worktree or branch is touched.
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.rollup_leaves("fakerepo", "next", self.base_id,
                                  self.leaf_ids)
        self.assertIn("not READY", str(ctx.exception))


class RollupConflictTests(RollupMixin):
    MODE = "conflict"

    def test_nonlock_conflict_blocked_naming_leaf(self):
        rec = batches.rollup_leaves("fakerepo", "next", self.base_id,
                                    self.leaf_ids)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("non-lock conflict", rec["error"])
        self.assertIn("test-rollup-leaf-rb", rec["error"])
        self.assertIn("README.md", rec["error"])
        self.assertIn("no success claimed", rec["error"])
        # Worktree cleaned up; op file records the block.
        stored = read_json(batches.op_file(rec["op_id"]))
        self.assertEqual(stored["status"], "blocked")
        self.assertFalse(
            Path(rec["result"]["path"]).exists(),
            "blocked rollup must not leave a worktree behind")
        self.assertEqual(self.regen_runs(), 0)


class RollupGuardTests(RollupMixin):
    def test_closure_violation_blocked_then_retry_done(self):
        self.cfg_set("ROGUE", "1")
        try:
            blocked = batches.rollup_leaves(
                "fakerepo", "next", self.base_id, self.leaf_ids)
        finally:
            self.cfg_pop("ROGUE")
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("관련 없는 잠금 이동", blocked["error"])
        self.assertIn("rogue", blocked["error"])
        self.assertIn("nothing committed", blocked["error"])
        op_id = blocked["op_id"]
        self.assertFalse(Path(blocked["result"]["path"]).exists())
        # Explicit retry after fixing the cause: archives the blocked
        # attempt (P-HIST) and reaches done with one more regen.
        runs_before = self.regen_runs()
        done = batches.retry_integration_op(op_id)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["op_id"], op_id)
        self.assertEqual(len(done.get("attempts") or []), 1)
        first = done["attempts"][0]
        self.assertEqual(first["status"], "blocked")
        self.assertIn("관련 없는 잠금 이동", first["error"])
        self.assertEqual(self.regen_runs(), runs_before + 1)
        res = done["regenerated_by"]["resolutions"]
        self.assertEqual(res["foo"], {"^1.2.0": "1.2.3"})

    def test_retry_unknown_op_refused(self):
        with self.assertRaises(batches.IntegrationError):
            batches.retry_integration_op("rollup-next-nope-0123456789ab")


class RollupLockRefreshTests(RollupMixin):
    MODE = "lockonly"

    def test_lockonly_leaves_refresh_path(self):
        # No leaf touches foo here, so its version must stay pinned:
        # the union closure guard would (correctly) flag any drift.
        self.cfg_pop("VER_FOO")
        self.cfg_set("VER_BAR", "2.0.1")
        try:
            rec = batches.rollup_leaves("fakerepo", "next",
                                        self.base_id, self.leaf_ids)
        finally:
            self.cfg_set("VER_FOO", "1.2.3")
            self.cfg_set("VER_BAR", "2.1.0")
        self.assertEqual(rec["status"], "done")
        by = rec["regenerated_by"]
        self.assertEqual(by["command_origin"], "rollup-lock-refresh")
        self.assertEqual(self.regen_runs(), 1)
        # The refresh dropped each dep's own stanzas before regen.
        log = self.yarn_log()
        self.assertIn("miss:bar", log)
        self.assertIn("miss:qux", log)
        res = by["resolutions"]
        self.assertEqual(res["bar"], {"^2.0.0": "2.0.1"})
        self.assertEqual(res["qux"], {"^3.0.0": "3.0.1"})
        res_path = Path(rec["result"]["path"])
        lock = (res_path / "yarn.lock").read_text(encoding="utf-8")
        self.assertIn('version "2.0.1"', lock)
        self.assertIn('version "3.0.1"', lock)
        draft = rec["pr_draft"]
        self.assertIn("- bar: lockfile-only (2.0.1, lock refresh)",
                      draft["body"])
        self.assertIn("- qux: lockfile-only (3.0.1, lock refresh)",
                      draft["body"])


class RollupMixedLockOnlyTests(RollupMixin):
    """S4.2-a: a lock-only leaf keeps its resolution in a mixed rollup.

    One manifest bump (foo) plus one lock-only leaf (bar, range
    unchanged) whose range admits a newer version. The locks conflict,
    the tree resets to the base lock, and the single regen must still
    resolve bar to its own target — not the base pin. Fails on the
    pre-S4.2 code with the revert error tonight's rollup hit."""

    MODE = "mixed"

    def test_lock_only_keeps_target(self):
        # qux is not a leaf here: keep the shim from moving it, or
        # the union guard (correctly) flags it as unrelated.
        self.cfg_pop("VER_QUX")
        try:
            rec = batches.rollup_leaves(
                "fakerepo", "next", self.base_id, self.leaf_ids)
        finally:
            self.cfg_set("VER_QUX", "3.0.1")
        self.assertEqual(rec["status"], "done")
        by = rec["regenerated_by"]
        self.assertIn("bar", by.get("lock_only_dropped") or [])
        self.assertNotIn("foo", by.get("lock_only_dropped") or [])
        res = by["resolutions"]
        self.assertEqual(res["bar"], {"^2.0.0": "2.1.0"})
        self.assertEqual(res["foo"], {"^1.2.0": "1.2.3"})
        lock = (Path(rec["result"]["path"]) / "yarn.lock").read_text(
            encoding="utf-8")
        self.assertIn('version "2.1.0"', lock)
        self.assertIn("- bar: lockfile-only (2.1.0, lock refresh)",
                      rec["pr_draft"]["body"])


class RollupBlockedHygieneTests(RollupMixin):
    """S4.2-b/c/d: a blocked rollup cleans up, refreshes the glance,
    and retries on the same op."""

    MODE = "trio"

    def _blocked_rogue(self):
        self.cfg_set("ROGUE", "1")
        try:
            return batches.rollup_leaves(
                "fakerepo", "next", self.base_id, self.leaf_ids)
        finally:
            self.cfg_pop("ROGUE")

    def test_blocked_removes_branch_and_refreshes(self):
        blocked = self._blocked_rogue()
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("관련 없는 잠금 이동", blocked["error"])
        branch = blocked["result"]["branch"]
        self.assertTrue(branch.startswith("codex/rollup-"))
        self.assertEqual(
            git("branch", "--list", branch,
                cwd=self.origin).strip(), "")
        self.assertIn("removed",
                      blocked["result"].get("branch_cleanup") or "")
        snap = batches.read_json(batches.summary_snapshot_path(),
                                 default=None)
        self.assertTrue(isinstance(snap, dict) and snap.get("at"))
        self.assertEqual(snap.get("writer"), "rollup")

    def test_retry_keeps_history(self):
        blocked = self._blocked_rogue()
        op_id = blocked["op_id"]
        self.cfg_set("ROGUE", "1")
        try:
            again = batches.retry_integration_op(op_id)
        finally:
            self.cfg_pop("ROGUE")
        self.assertEqual(again["status"], "blocked")
        self.assertEqual(again["op_id"], op_id)
        self.assertEqual(len(again.get("attempts") or []), 1)
        self.assertIn("관련 없는 잠금 이동",
                      (again["attempts"] or [{}])[0].get("error") or "")


class RollupStackDraftTests(RollupMixin):
    """S4.3: the PR draft lists every commit vs the target branch,
    grouped by batch, with the true diffstat, the unreleased-base
    sentence, the lane --base command, the ship-order choice and a
    fastchecks line read from the op gates."""

    MODE = "stacked"

    def test_stack_draft(self):
        self.cfg_pop("VER_QUX")
        try:
            rec = batches.rollup_leaves(
                "fakerepo", "next", self.base_id, self.leaf_ids)
        finally:
            self.cfg_set("VER_QUX", "3.0.1")
        self.assertEqual(rec["status"], "done")
        body = rec["pr_draft"]["body"]
        # S4.5: default PR language is English (no repo profile).
        self.assertIn(
            "<!-- openmainty: pr_language=en (source: default) -->",
            body)
        self.assertIn("Contents: 3 commits vs `next`", body)
        self.assertIn("### test-rollup-gp: 1 commits", body)
        self.assertIn("base chain work", body)
        self.assertIn("### test-rollup-base: 1 commits", body)
        self.assertIn("base integration work", body)
        self.assertIn("### rollup", body)
        self.assertIn("This PR also includes 2 not-yet-released "
                      "base batches", body)
        self.assertIn("test-rollup-gp", body)
        self.assertIn("Ship order, owner's choice", body)
        self.assertIn("(a) main chain first", body)
        self.assertIn("(b) all at once to `next`", body)
        self.assertIn("Fastchecks: pending", body)
        self.assertIn("Root QA receipts are attached at import.", body)
        self.assertTrue(any(cmd.startswith("gh pr create --base next")
                            for cmd in rec["owner_commands"]))
        self.assertIn("기준 묶음 2개 포함 · 전체 3 commits",
                      rec["pr_draft"]["stack_line"])


class RollupStackedDraftTests(RollupMixin):
    """Owner decision (chain-first): TWO copy-only drafts, PR1 to main
    then PR2 stacked. Never pushes, branches, or opens anything."""

    MODE = "stacked"

    def _done(self):
        self.cfg_pop("VER_QUX")
        try:
            return batches.rollup_leaves(
                "fakerepo", "next", self.base_id, self.leaf_ids)
        finally:
            self.cfg_set("VER_QUX", "3.0.1")

    def test_two_drafts(self):
        rec = self._done()
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec.get("ship_order"), "chain-first")
        stacked = rec.get("stacked_drafts") or {}
        pr1, pr2 = stacked.get("pr1") or {}, stacked.get("pr2") or {}
        base_head = git("rev-parse", "HEAD",
                        cwd=self.base_wt).strip()
        # PR1 → main: branch-create at the exact tip, --base/--head.
        self.assertEqual(
            pr1["commands"][0],
            f"git branch {pr1['branch']} {base_head}")
        self.assertTrue(any(c.startswith("gh pr create --base main "
                                         "--head codex/ship-main-")
                            for c in pr1["commands"]))
        self.assertIn("### test-rollup-gp: 1 commits", pr1["body"])
        self.assertIn("### test-rollup-base: 1 commits", pr1["body"])
        # S4.5: default PR language is English (no repo profile).
        self.assertIn("also includes 2 not-yet-released base batches",
                      pr1["body"])
        self.assertIn("owner's decision", pr1["body"])
        self.assertIn("Root QA in progress (claude66/qa/m0-ship-main-*)",
                      pr1["body"])
        self.assertIn("소유자 요약: PR1", stacked.get("summary_ko") or "")
        # PR2 → stacked: --base ship branch, retarget words, 1 commit.
        self.assertEqual(pr2["base"], pr1["branch"])
        self.assertTrue(any(c.startswith(
            "gh pr create --base codex/ship-main-")
            for c in pr2["commands"]))
        self.assertIn("`gh pr edit <number> --base next`", pr2["body"])
        self.assertIn("2 leaves", pr2["body"])
        # Glance line names both PRs with counts.
        steps = batches._m1_next_steps(
            "r", [], {},
            {"next": {"status": "done", "pr_title": pr2["title"],
                      "stacked": {"pr1": {"commits": 2},
                                  "pr2": {"leaves": 2}}}})
        self.assertEqual(len(steps), 1)
        self.assertIn("PR1 (main, 2 commits) → PR2 (next rollup, "
                      "2 leaves)", steps[0]["text"])

    def test_qa_lines_come_from_input(self):
        rec = self._done()
        base_head = git("rev-parse", "HEAD",
                        cwd=self.base_wt).strip()
        drafts = batches._rollup_stacked_drafts(
            repo="fakerepo", lane="next", base_id=self.base_id,
            base_head=base_head, head=rec["result"]["head"],
            leaves=[], changes={}, branch="codex/rollup-x",
            op_id="op-x", new_path=rec["result"]["path"], gates=[],
            qa={"pr1": "PR1-QA-PASS on <sha>",
                "pr2": "PR2-QA-PASS on <sha>"})
        self.assertIn("PR1-QA-PASS on <sha>",
                      drafts["pr1"]["body"])
        self.assertIn("PR2-QA-PASS on <sha>",
                      drafts["pr2"]["body"])


class RollupStackedCommandsTests(RollupMixin):
    """S4.4-R1/R3: PR2 names push + --base/--head explicitly; PR1's
    title names the batches' own purposes (fixes ride along)."""

    MODE = "stacked"

    def _done(self):
        self.cfg_pop("VER_QUX")
        try:
            return batches.rollup_leaves(
                "fakerepo", "next", self.base_id, self.leaf_ids)
        finally:
            self.cfg_set("VER_QUX", "3.0.1")

    def test_pr2_push_and_explicit_heads(self):
        rec = self._done()
        stacked = rec.get("stacked_drafts") or {}
        pr1, pr2 = stacked.get("pr1") or {}, stacked.get("pr2") or {}
        # PR2 pushes its own branch first, then creates with
        # explicit --base AND --head (gh would otherwise use
        # whatever branch the owner has checked out).
        self.assertEqual(pr2["commands"][0],
                         "git push -u origin %s" % pr2["branch"])
        self.assertIn("--base %s --head %s"
                      % (pr1["branch"], pr2["branch"]),
                      pr2["commands"][1])
        # Every gh command names --base AND --head, and every
        # --head branch is pushed by an earlier command.
        pushed = {c.split()[-1]
                  for cmds in (pr1["commands"], pr2["commands"])
                  for c in cmds if c.startswith("git push")}
        for cmds in (pr1["commands"], pr2["commands"]):
            for c in cmds:
                if not c.startswith("gh pr create"):
                    continue
                self.assertIn("--base ", c, c)
                self.assertIn("--head ", c, c)
                head = c.split("--head ")[1].split()[0]
                self.assertIn(head, pushed, c)

    def test_pr2_diffstat_covers_rollup_only(self):
        # S4.5-R1: PR2 ships base_head..head (the rollup commit:
        # package.json + yarn.lock = 2 files), not the whole stack
        # vs the lane target (which rides the base chain: 4+ files).
        rec = self._done()
        pr2 = (rec.get("stacked_drafts") or {}).get("pr2") or {}
        self.assertRegex(pr2.get("body", ""),
                         r"(?<!\d)2 files changed")

    def test_pr1_title_names_batches_not_commits(self):
        # S4.5: the English title names one data-derived segment per
        # chain batch (single primaries "gp 0" + "base 0" here).
        rec = self._done()
        title = (rec.get("stacked_drafts") or {}).get("pr1", {}) \
            .get("title", "")
        self.assertEqual(title, "chore: ship gp 0 and base 0 to main")


class RollupPrLanguageTests(RollupMixin):
    """S4.5-R2: per-repo `pr_language` ("en"|"ko") rendered from the
    same data; default "en"; the header prints the effective value
    and its source; the panel keeps a Korean owner summary."""

    MODE = "stacked"

    def _profile(self, obj):
        home = Path(os.environ["OPENMAINTY_HOME"])
        repod = home / "repos"
        repod.mkdir(parents=True, exist_ok=True)
        (repod / "fakerepo.json").write_text(
            json.dumps(obj), encoding="utf-8")

    def _done(self):
        self.cfg_pop("VER_QUX")
        try:
            return batches.rollup_leaves(
                "fakerepo", "next", self.base_id, self.leaf_ids)
        finally:
            self.cfg_set("VER_QUX", "3.0.1")

    def test_default_is_english(self):
        self.assertEqual(batches._pr_language("fakerepo"),
                         ("en", "default"))
        rec = self._done()
        stacked = rec.get("stacked_drafts") or {}
        for key in ("pr1", "pr2"):
            body = (stacked.get(key) or {}).get("body") or ""
            self.assertTrue(
                body.startswith(
                    "<!-- openmainty: pr_language=en "
                    "(source: default) -->"), body[:120])
        combo = stacked["pr1"]["body"] + stacked["pr2"]["body"] \
            + rec["pr_draft"]["body"]
        self.assertNotRegex(combo, r"[가-힣]")
        self.assertIn("소유자 요약: PR1", stacked.get("summary_ko")
                      or "")

    def test_ko_profile_renders_korean(self):
        self._profile({"name": "fakerepo", "pr_language": "ko"})
        self.assertEqual(batches._pr_language("fakerepo"),
                         ("ko", "repo profile"))
        rec = self._done()
        stacked = rec.get("stacked_drafts") or {}
        pr1 = stacked.get("pr1") or {}
        self.assertTrue(pr1["body"].startswith(
            "<!-- openmainty: pr_language=ko (source: repo profile)"
            " -->"))
        self.assertEqual(
            pr1["title"],
            "chore: ship base chain to main (test test-rollup-gp, "
            "test test-rollup-base)")
        self.assertIn("구성: 대상 `main`", pr1["body"])
        self.assertIn("`gh pr edit <번호> --base next`",
                      stacked["pr2"]["body"])

    def test_invalid_profile_value_falls_back(self):
        self._profile({"name": "fakerepo", "pr_language": "xx"})
        lang, src = batches._pr_language("fakerepo")
        self.assertEqual(lang, "en")
        self.assertIn("invalid", src)
        rec = self._done()
        pr1 = (rec.get("stacked_drafts") or {}).get("pr1") or {}
        self.assertIn("source: default (invalid 'xx' in profile)",
                      pr1["body"])
        self.assertIn("Contents:", pr1["body"])

    def test_seg_shared_target_prefix(self):
        fake = {"primaries": [
            {"dep": "@typescript-eslint/eslint-plugin",
             "target": "8.71.2"},
            {"dep": "@typescript-eslint/parser",
             "target": "8.71.2"}]}
        orig = batches.load_batch
        batches.load_batch = lambda bid: fake
        try:
            self.assertEqual(batches._pr1_seg_labels(["x"]),
                             ["typescript-eslint 8.71.2"])
        finally:
            batches.load_batch = orig

    def test_seg_mixed_primaries(self):
        fake = {"primaries": [
            {"dep": "prettier", "target": "3.9.10"},
            {"dep": "react-native-builder-bob", "target": "0.43.2"}]}
        orig = batches.load_batch
        batches.load_batch = lambda bid: fake
        try:
            self.assertEqual(batches._pr1_seg_labels(["x"]),
                             ["prettier +1"])
        finally:
            batches.load_batch = orig

    def test_title_en_fallback_and_cap(self):
        self.assertEqual(batches._pr1_title_en([], 5),
                         "chore: ship base chain to main (5 commits)")
        long_title = batches._pr1_title_en(["a" * 60, "b" * 60], 2)
        self.assertTrue(long_title.endswith("…"), long_title)
        self.assertLessEqual(len(long_title), 120)

    def test_seg_with_fixes_suffix(self):
        # S7-b: a group carrying fix commits says so in its segment.
        fake = {"primaries": [{"dep": "foo", "target": "1.2.3"}]}
        orig = batches.load_batch
        batches.load_batch = lambda bid: fake
        try:
            segs = batches._pr1_seg_labels(
                ["g1"], {"g1": [("a" * 40,
                                 "fix(ios): adopt scene lifecycle"),
                                ("b" * 40,
                                 "chore(deps): bump foo")]})
            self.assertEqual(segs, ["foo 1.2.3 (with fixes)"])
            segs = batches._pr1_seg_labels(
                ["g1"], {"g1": [("a" * 40,
                                 "feat(api): add endpoint")]})
            self.assertEqual(segs, ["foo 1.2.3 (with features)"])
            segs = batches._pr1_seg_labels(["g1"], {})
            self.assertEqual(segs, ["foo 1.2.3"])
        finally:
            batches.load_batch = orig

    def test_next_lane_note_singular_and_plural(self):
        # S7-b: one next-lane batch reads "is classified as a
        # next-lane batch"; two keep the plural.
        rec = self._done()
        base_head = git("rev-parse", "HEAD",
                        cwd=self.base_wt).strip()
        kw = dict(repo="fakerepo", lane="next", base_id=self.base_id,
                  base_head=base_head, head=rec["result"]["head"],
                  leaves=[], changes={}, branch="codex/rollup-x",
                  op_id="op-x", new_path=rec["result"]["path"],
                  gates=[])
        plural = batches._rollup_stacked_drafts(**kw)["pr1"]["body"]
        self.assertIn("are classified as next-lane batches", plural)
        orig = batches._rollup_group_is_main_lane
        batches._rollup_group_is_main_lane = \
            lambda bid: bid == "test-rollup-base"
        try:
            single = batches._rollup_stacked_drafts(**kw)["pr1"]["body"]
        finally:
            batches._rollup_group_is_main_lane = orig
        self.assertIn("is classified as a next-lane batch", single)
        self.assertNotIn("are classified", single)


class RollupRedraftTests(RollupMixin):
    """S4.4-R2: explicit redraft path for a done rollup op.

    Verifies head + fingerprint read-only, archives the old draft,
    persists an optional qa note, refreshes the glance, and never
    touches the branch or any worktree."""

    MODE = "stacked"

    def _done(self):
        self.cfg_pop("VER_QUX")
        try:
            return batches.rollup_leaves(
                "fakerepo", "next", self.base_id, self.leaf_ids)
        finally:
            self.cfg_set("VER_QUX", "3.0.1")

    def test_redraft_rebuilds_and_archives(self):
        rec = self._done()
        op_id = rec["op_id"]
        old_body = rec["pr_draft"]["body"]
        branch = rec["result"]["branch"]
        head = rec["result"]["head"]
        fp = rec["result"]["fingerprint"]
        self.assertEqual(
            git("rev-parse", branch, cwd=self.origin).strip(), head)
        redone = batches.regenerate_rollup_drafts(
            op_id, qa={"pr1": "PR1-QA-PASS on <r1>",
                       "pr2": "PR2-QA-PASS on <r2>"})
        self.assertEqual(redone["status"], "done")
        # The old drafts are archived, the new ones carry the note.
        self.assertEqual(
            (redone["pr_draft_previous"] or {}).get("body"),
            old_body)
        self.assertIn("PR1-QA-PASS on <r1>",
                      redone["stacked_drafts"]["pr1"]["body"])
        self.assertIn("PR2-QA-PASS on <r2>",
                      redone["stacked_drafts"]["pr2"]["body"])
        # Branch and worktree untouched (head + fingerprint same).
        self.assertEqual(
            git("rev-parse", branch, cwd=self.origin).strip(), head)
        snap = runner.source_snapshot(redone["result"]["path"])
        self.assertEqual(snap["head"], head)
        self.assertEqual(snap["fingerprint"], fp)
        # The qa note persists: a later redraft without qa keeps it.
        self.assertTrue(batches.rollup_qa_path(op_id).is_file())
        again = batches.regenerate_rollup_drafts(op_id)
        self.assertIn("PR1-QA-PASS on <r1>",
                      again["stacked_drafts"]["pr1"]["body"])
        # The glance snapshot was refreshed.
        self.assertTrue(batches.summary_snapshot_path().is_file())

    def test_redraft_names_held_leaf_in_caveats(self):
        # M2.24: a leaf held after the draft shows up in the
        # redrafted body and the returned caveats — never in the
        # stored op file.
        rec = self._done()
        leaf = self.leaf_ids[0]
        batches.hold_batch(leaf, True, "owner", reason="root QA 전")
        redone = batches.regenerate_rollup_drafts(rec["op_id"])
        self.assertEqual(redone["status"], "done")
        held = [c for c in redone["caveats"]
                if c.get("kind") == "held"
                and c.get("leaf") == leaf]
        self.assertEqual(len(held), 1)
        self.assertIn("보류 중 — root QA 전", held[0]["text"])
        body = redone["pr_draft"]["body"]
        self.assertIn("\nCaveats\n", body)
        self.assertIn("- %s" % held[0]["text"], body)
        stored = batches.read_json(batches.op_file(rec["op_id"]),
                                   default=None)
        self.assertNotIn("caveats", stored)

    def test_redraft_refuses_moved_branch(self):
        rec = self._done()
        wt = Path(rec["result"]["path"])
        (wt / "moved.txt").write_text("moved\n", encoding="utf-8")
        git("add", "moved.txt", cwd=wt)
        git("commit", "-qm", "owner moved the branch", cwd=wt)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.regenerate_rollup_drafts(rec["op_id"])
        self.assertIn("moved", str(ctx.exception))

    def test_redraft_refuses_missing_worktree(self):
        rec = self._done()
        shutil.rmtree(rec["result"]["path"])
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.regenerate_rollup_drafts(rec["op_id"])
        self.assertIn("gone", str(ctx.exception))


class RollupRedraftBlockedTests(RollupMixin):
    MODE = "conflict"

    def test_redraft_refuses_blocked_op(self):
        rec = batches.rollup_leaves("fakerepo", "next", self.base_id,
                                    self.leaf_ids)
        self.assertEqual(rec["status"], "blocked")
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.regenerate_rollup_drafts(rec["op_id"])
        self.assertIn("only a done rollup", str(ctx.exception))


class RollupValidationTests(RollupMixin):
    def test_unknown_batch_404(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.rollup_leaves("fakerepo", "next", "no-such-batch",
                                  self.leaf_ids)
        self.assertEqual(ctx.exception.http_status, 404)

    def test_empty_leaves_refused(self):
        with self.assertRaises(batches.IntegrationError):
            batches.rollup_leaves("fakerepo", "next", self.base_id, [])

    def test_duplicate_dep_refused(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.rollup_leaves("fakerepo", "next", self.base_id,
                                  [self.leaf_ids[0], self.leaf_ids[0]])
        self.assertIn("distinct", str(ctx.exception))

    def test_nonready_leaf_refused(self):
        bid = self.leaf_ids[0]
        data = read_json(batches.batches_dir() / (bid + ".json"))
        data["primaries"][0]["review"]["verdict"] = "needs-work"
        write_json(batches.batches_dir() / (bid + ".json"), data)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.rollup_leaves("fakerepo", "next", self.base_id,
                                  self.leaf_ids)
        self.assertIn("not READY", str(ctx.exception))


class RollupClosureTests(unittest.TestCase):
    """S4-R1: the rollup live map covers the transitive closure.

    A 3-level chain (grandparent -> parent -> base, leaf on base) must
    reach live_map_with_diag with the grandparent included. The spy
    stops the run right there, so the test fails while rollup builds
    base + leaves + direct parents only, and passes once rollup uses
    the shared transitive closure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _batch(self, bid, inherited):
        write_json(batches.batches_dir() / (bid + ".json"), {
            "schema": 3, "id": bid, "repo": "fakerepo",
            "target_branch": "next", "purpose": "closure test",
            "updated_at": "2026-10-20T00:00:00+00:00",
            "primaries": [{"dep": "dep-" + bid, "target": "1.0",
                           "lane": "next"}],
            "inherited": [{"batch": h} for h in inherited]})

    def test_live_map_includes_grandparent(self):
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        self._batch("grandparent", [])
        self._batch("parent", ["grandparent"])
        self._batch("base", ["parent"])
        self._batch("leaf1", ["base"])
        seen = {}
        real = batches.live_map_with_diag

        def spy(batch_list):
            seen["ids"] = sorted(
                str(b.get("id") or "") for b in batch_list)
            raise batches.IntegrationError("spy-stop")

        batches.live_map_with_diag = spy
        try:
            with self.assertRaises(batches.IntegrationError):
                batches.rollup_leaves("fakerepo", "next", "base",
                                      ["leaf1"])
        finally:
            batches.live_map_with_diag = real
        self.assertIn("grandparent", seen.get("ids", []))
        self.assertIn("parent", seen.get("ids", []))

    def test_shared_closure_covers_chain(self):
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        self._batch("grandparent", [])
        self._batch("parent", ["grandparent"])
        self._batch("base", ["parent"])
        self._batch("leaf1", ["base"])
        leaf = batches.load_batch("leaf1")
        ids = sorted(
            str(b.get("id") or "") for b in batches.closure([leaf]))
        self.assertEqual(ids, ["base", "grandparent", "leaf1",
                               "parent"])


class RollupReadyReasonTests(unittest.TestCase):
    """S4-R2: _rollup_ready reports evaluate()'s real reasons."""

    def test_real_reason_surfaced(self):
        real = batches.evaluate
        batches.evaluate = lambda *a, **k: {"results": [{
            "ready": False,
            "reasons": ["source drifted: head abc != def"]}]}

        try:
            with self.assertRaises(batches.IntegrationError) as ctx:
                batches._rollup_ready({"id": "b"}, {}, {})
        finally:
            batches.evaluate = real
        self.assertIn("source drifted", str(ctx.exception))
        self.assertNotIn("no ready primaries", str(ctx.exception))

    def test_empty_reasons_still_honest(self):
        real = batches.evaluate
        batches.evaluate = lambda *a, **k: {"results": []}
        try:
            with self.assertRaises(batches.IntegrationError) as ctx:
                batches._rollup_ready({"id": "b"}, {}, {})
        finally:
            batches.evaluate = real
        self.assertIn("not READY", str(ctx.exception))


class RollupEndpointTests(RollupMixin):
    def setUp(self):
        super().setUp()
        from openmainty import serve  # noqa: E402
        self.serve = serve
        self._gate = serve._require_gate
        serve._require_gate = lambda: None
        self.addCleanup(setattr, serve, "_require_gate", self._gate)

    def payload(self, **over):
        base = {"repo": "fakerepo", "lane": "next",
                "base_batch": self.base_id, "leaves": self.leaf_ids,
                "confirm": True}
        base.update(over)
        return base

    def test_missing_confirm_refuses_without_starting(self):
        with self.assertRaises(self.serve.ApiError) as ctx:
            self.serve.api_integration_rollup(self.payload(confirm=False))
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.regen_runs(), 0)

    def test_missing_fields_refused(self):
        with self.assertRaises(self.serve.ApiError) as ctx:
            self.serve.api_integration_rollup({"repo": "fakerepo",
                                               "confirm": True})
        self.assertEqual(ctx.exception.status, 400)

    def test_unknown_batch_404(self):
        with self.assertRaises(self.serve.ApiError) as ctx:
            self.serve.api_integration_rollup(
                self.payload(base_batch="no-such-batch"))
        self.assertEqual(ctx.exception.status, 404)

    def test_busy_lease_409_starts_nothing(self):
        # The worker lease is reentrant on one thread, so a foreign
        # holder must sit on another thread to read as busy.
        import threading  # noqa: E402
        from openmainty import sched  # noqa: E402
        entered = threading.Event()
        release = threading.Event()
        holder = {}

        def _hold():
            tok = sched.acquire_worker_lease("test-holder")
            holder["token"] = tok
            entered.set()
            release.wait(30)
            if tok is not None:
                sched.release_worker_lease(tok)

        th = threading.Thread(target=_hold)
        th.start()
        try:
            self.assertTrue(entered.wait(10))
            self.assertIsNotNone(holder.get("token"))
            with self.assertRaises(self.serve.ApiError) as ctx:
                self.serve.api_integration_rollup(self.payload())
            self.assertEqual(ctx.exception.status, 409)
        finally:
            release.set()
            th.join(30)
        self.assertEqual(self.regen_runs(), 0)

    def test_route_registered(self):
        src = Path(self.serve.__file__).read_text(encoding="utf-8")
        self.assertIn('"/api/integration/rollup": api_integration_rollup',
                      src)

    def test_endpoint_runs_rollup_done(self):
        rec = self.serve.api_integration_rollup(self.payload())
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["op"], "rollup")
        self.assertEqual(self.regen_runs(), 1)

    def test_redraft_needs_confirm(self):
        rec = self.serve.api_integration_rollup(self.payload())
        self.assertEqual(rec["status"], "done")
        with self.assertRaises(self.serve.ApiError) as ctx:
            self.serve.api_integration_rollup(
                {"op": rec["op_id"], "redraft": True})
        self.assertEqual(ctx.exception.status, 400)

    def test_redraft_roundtrip_archives(self):
        rec = self.serve.api_integration_rollup(self.payload())
        old_body = rec["pr_draft"]["body"]
        out = self.serve.api_integration_rollup(
            {"op": rec["op_id"], "redraft": True, "confirm": True,
             "qa": {"pr2": "PR2-QA-PASS on <r2>"}})
        self.assertEqual(out["status"], "done")
        self.assertEqual(
            (out["pr_draft_previous"] or {}).get("body"), old_body)
        self.assertIn("PR2-QA-PASS on <r2>",
                      out["stacked_drafts"]["pr2"]["body"])


HTML = Path(__file__).resolve().parent.parent / "src" / "openmainty" \
    / "dashboard.html"

DRIVER_HEAD = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = m[1];
function sliceBalanced(s, i) {
  let depth = 0, q = null, line = false, block = false, esc = false;
  for (; i < s.length; i++) {
    const c = s[i], n = s[i + 1];
    if (line) { if (c === "\n") line = false; continue; }
    if (block) { if (c === "*" && n === "/") { block = false; i++; } continue; }
    if (q) {
      if (esc) { esc = false; continue; }
      if (c === "\\") { esc = true; continue; }
      if (c === q) q = null;
      continue;
    }
    if (c === "/" && n === "/") { line = true; i++; continue; }
    if (c === "/" && n === "*") { block = true; i++; continue; }
    if (c === "'" || c === '"' || c === "`") { q = c; continue; }
    if (c === "{") depth++;
    if (c === "}") { depth--; if (depth === 0) return i + 1; }
  }
  throw new Error("unbalanced");
}
function extractFn(name) {
  const re = new RegExp("(?:^|\\n)(?:async\\s+)?function\\s+" + name + "\\s*\\(");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing function " + name);
  const brace = src.indexOf("{", mm.index);
  return src.slice(mm[0].startsWith("\n") ? mm.index + 1 : mm.index, sliceBalanced(src, brace));
}
const wantFn = (process.argv[3] || "").split(",").filter(Boolean);
let seized = "";
for (const f of wantFn) seized += extractFn(f) + "\n";
const driverBody = process.argv[4];
const prog = seized + "\n" + driverBody;
try {
  eval(prog);
} catch (e) {
  console.log("FAIL driver eval: " + (e && e.message));
  process.exit(1);
}
'''

FNS = ("rollupCandidates,broadestReady,batchClosureReady,"
       "prepBatchLanes,prepBaseFullyReady")

BODY_ROLLUP = """
let pass = 0;
function eq(a, b, msg) {
  if (JSON.stringify(a) === JSON.stringify(b)) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function batch(id, at, inh, ready) {
  return {id: id, updated_at: at, inherited: inh.map(function(p) { return {batch: p}; }),
          ready: ready === undefined ? 1 : ready, primaries: 1,
          integrations: [{lane: "next"}], purpose: "test " + id};
}
const base = batch("base", "2026-10-19T00:00:00Z", [], 1);
const l1 = batch("leaf-a", "2026-10-20T00:00:01Z", ["base"]);
const l2 = batch("leaf-b", "2026-10-20T00:00:02Z", ["base"]);
const l3 = batch("leaf-c", "2026-10-20T00:00:03Z", ["base"]);
const stale = batch("stale", "2026-10-18T00:00:00Z", ["base"], 0);
const other = batch("other", "2026-10-20T00:00:04Z", ["elsewhere"]);
let r = rollupCandidates([base, l3, l1, other, stale, l2], "next");
eq(r.baseId, "base", "sibling tie resolves to the common ancestor");
eq(r.leafIds, ["leaf-a", "leaf-b", "leaf-c"], "true leaves oldest first");
eq(r.note.indexOf("형제 묶음 3개") >= 0, true, "sibling count in the note");
const mid = batch("mid", "2026-10-20T00:00:05Z", ["base"]);
const deep = batch("deep", "2026-10-20T00:00:06Z", ["mid"]);
r = rollupCandidates([base, mid, deep], "next");
eq(r.baseId, "deep", "unique deepest stays the base");
eq(r.leafIds, [], "nothing inherits the deepest");
r = rollupCandidates([base, l1, l2, l3], "main");
eq(r.baseId, "", "wrong lane yields no default");
eq(r.leafIds, [], "wrong lane yields no leaves");
r = rollupCandidates([base], "next");
eq(r.baseId, "base", "lone base still resolves");
eq(r.leafIds, [], "lone base has no leaves");
console.log("ROLLUP-PASS " + pass);
"""

BODY_CAVEATS = """
let pass = 0;
function eq(a, b, msg) {
  if (JSON.stringify(a) === JSON.stringify(b)) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function has(s, sub) { return s.indexOf(sub) >= 0; }
const rec = {status: "done", op_id: "op-1",
  leaves: [{dep: "jest", batch: "b1"}],
  result: {branch: "br", head: "abcdef123456"},
  pr_draft: {title: "t", body: "hello"},
  owner_commands: [], gates: [],
  caveats: [{leaf: "b1", kind: "held", text: "jest (b1): <b>hold</b>"},
            {leaf: "b2", kind: "newer_ready", text: "x"}]};
const html = rollupResultHtml(rec);
eq(has(html, "초안 이후 확인할 점 2건"), true, "count line");
eq(has(html, "&lt;b&gt;hold&lt;/b&gt;"), true, "caveat text escaped");
eq(has(html, "<b>hold</b>"), false, "no raw html");
eq(html.indexOf("초안 이후 확인할 점") < html.indexOf("hello"), true, "caveats above body");
const clean = rollupResultHtml({status: "done", op_id: "op-1", leaves: [],
  result: {}, pr_draft: {title: "t", body: "hello"},
  owner_commands: [], gates: []});
eq(has(clean, "초안 이후 확인할 점"), false, "clean has no count");
console.log("CAVEATS-PASS " + pass);
"""

FNS_STORED = "storedRollupRec,effectiveRollupRec,stackedPrHtml,rollupResultHtml"

BODY_STORED = """
let pass = 0;
function eq(a, b, msg) {
  if (JSON.stringify(a) === JSON.stringify(b)) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function has(s, sub) { return s.indexOf(sub) >= 0; }
const row = {op: "op-9", status: "done", leaves: 9,
  leaves_list: [{batch: "b1", dep: "jest", target: "1.0.0"}],
  branch: "br", head: "abcdef1234567890",
  result: {branch: "br", head: "abcdef1234567890"},
  pr_title: "t<script>", pr_body: "hello<script>",
  stacked: {summary_ko: "sum", pr1: {title: "p1<script>", body: "b1"}, pr2: {title: "p2", body: "b2"}},
  owner_commands: ["git push"], gates: [{key: "fastchecks", status: "pending"}],
  caveats: [{leaf: "b1", kind: "compiler", text: "c1 <b>"}, {leaf: "b1", kind: "newer_ready", text: "c2"}]};
const stored = storedRollupRec(row);
eq(stored.op_id, "op-9", "op id");
eq(stored.leaves.length, 1, "slim leaves");
eq(stored.pr_draft.title, "t<script>", "title kept raw");
eq(storedRollupRec({status: "none", leaves: 0}), null, "none row");
eq(storedRollupRec(null), null, "null row");
const html = rollupResultHtml(stored);
eq(has(html, "초안 이후 확인할 점 2건"), true, "caveat count");
eq(has(html, "&lt;script&gt;"), true, "body escaped");
eq(has(html, "<script>"), false, "no raw script");
eq(has(html, "PR1 — main 기준 체인"), true, "pr1 label");
eq(has(html, "PR2 — next 합치기"), true, "pr2 label");
eq(has(html, "p1&lt;script&gt;"), true, "pr1 title escaped");
eq(has(html, 'data-op="op-9"'), true, "redraft button op");
const sess = {op_id: "op-new", status: "done", leaves: [],
  result: {}, pr_draft: {title: "new", body: "new"},
  owner_commands: [], gates: [], caveats: []};
eq(effectiveRollupRec(stored, sess).op_id, "op-new", "session wins");
eq(has(rollupResultHtml(effectiveRollupRec(stored, sess)), "new"), true, "session renders");
eq(effectiveRollupRec(stored, null), stored, "stored fallback");
console.log("STORED-PASS " + pass);
"""


class RollupPanelStructureTests(unittest.TestCase):
    """Inline render sites that cannot run under node: guarded by
    structural asserts (exact identifiers/wording present). Visible
    behavior is verified in the real browser during self-use."""

    def test_panel_wired_without_push(self):
        src = HTML.read_text(encoding="utf-8")
        for token in (
                "function rollupPanelHtml",
                "function rollupResultHtml",
                "function rollupGo",
                "rollupCandidates(mine, lane)",
                'data-k="rollup-go"',
                'data-k="rollup-retry"',
                "/api/integration/rollup",
                "레인 묶음 합치기 (rollup)",
                "개 묶음 합침",
                "초안 이후 확인할 점",
                "PR1 — main 기준 체인",
                "PR2 — next 합치기",
                "storedRollupRec",
                "pendingRollupScroll",
                "?tab=reviews&rollup=",
                "합치기 막힘",
                "게이트 대기 중",
                "푸시·PR 생성은 하지 않습니다",
                "esc(draft.body)",
        ):
            self.assertIn(token, src, "panel token missing: %s" % token)
        for forbidden in ("git push -u origin ${", "gh pr create ${",
                          "fetch(\"/api/integration/rollup\", {method: \"GET\""):
            self.assertNotIn(forbidden, src)


class RollupPanelHelperTests(unittest.TestCase):
    def test_rollup_candidates_under_node(self):
        driver = DRIVER_HEAD
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False) as fh:
            fh.write(driver)
            driver_path = fh.name
        try:
            proc = subprocess.run(
                ["node", driver_path, str(HTML), FNS, BODY_ROLLUP],
                capture_output=True, text=True, timeout=120)
        finally:
            os.unlink(driver_path)
        self.assertEqual(proc.returncode, 0,
                         "node driver failed: %s%s"
                         % (proc.stdout[-2000:], proc.stderr[-500:]))
        self.assertIn("ROLLUP-PASS 9", proc.stdout)

    def test_rollup_result_caveats_under_node(self):
        # esc() carries a regex literal the shared brace extractor
        # cannot parse, so seize only rollupResultHtml and prepend
        # esc's real single-line source (matched, never retyped).
        import re as _re
        esc_line = _re.search(
            r"^function esc\(s\) \{.*\}$",
            HTML.read_text(encoding="utf-8"), _re.M).group(0)
        driver = DRIVER_HEAD
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False) as fh:
            fh.write(driver)
            driver_path = fh.name
        try:
            proc = subprocess.run(
                ["node", driver_path, str(HTML),
                 "stackedPrHtml,rollupResultHtml",
                 esc_line + "\n" + BODY_CAVEATS],
                capture_output=True, text=True, timeout=120)
        finally:
            os.unlink(driver_path)
        self.assertEqual(proc.returncode, 0,
                         "node driver failed: %s%s"
                         % (proc.stdout[-2000:], proc.stderr[-500:]))
        self.assertIn("CAVEATS-PASS 5", proc.stdout)

    def test_stored_rollup_rec_renders_after_reload(self):
        # M2.25: a stored done lane renders the title, PR bodies,
        # PR1/PR2, caveats and the redraft button with the stored
        # op id; an in-session result takes precedence.
        import re as _re
        esc_line = _re.search(
            r"^function esc\(s\) \{.*\}$",
            HTML.read_text(encoding="utf-8"), _re.M).group(0)
        driver = DRIVER_HEAD
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False) as fh:
            fh.write(driver)
            driver_path = fh.name
        try:
            proc = subprocess.run(
                ["node", driver_path, str(HTML), FNS_STORED,
                 esc_line + "\n" + BODY_STORED],
                capture_output=True, text=True, timeout=120)
        finally:
            os.unlink(driver_path)
        self.assertEqual(proc.returncode, 0,
                         "node driver failed: %s%s"
                         % (proc.stdout[-2000:], proc.stderr[-500:]))
        self.assertIn("STORED-PASS 15", proc.stdout)


class RollupSupersedeTests(RollupMixin):
    """M2: a strictly larger READY set supersedes a done rollup.

    The new op names the old one in its drafts; the old op file,
    drafts and branch stay byte-identical; the regen receipt shows
    a scrubbed env with scripts off; the glance shows newest only.
    """

    MODE = "trio"

    def test_superset_supersedes_old_byte_identical(self):
        # qux unpinned first: the [foo, bar] regen then leaves qux
        # alone (a subset whose union regen moves an outsider is
        # correctly blocked — the guard, not the setup).
        self.cfg_pop("VER_QUX")
        try:
            rec1 = batches.rollup_leaves(
                "fakerepo", "next", self.base_id,
                self.leaf_ids[:2])
        finally:
            self.cfg_set("VER_QUX", "3.0.1")
        self.assertEqual(rec1["status"], "done")
        opf1 = batches.op_file(rec1["op_id"])
        before = opf1.read_bytes()
        os.environ["M2_TEST_TOKEN"] = "must-not-leak"
        try:
            rec2 = batches.rollup_leaves(
                "fakerepo", "next", self.base_id, self.leaf_ids,
                supersedes={"op": rec1["op_id"],
                            "new": [self.leaf_ids[2]]})
        finally:
            os.environ.pop("M2_TEST_TOKEN", None)
        self.assertEqual(rec2["status"], "done")
        self.assertNotEqual(rec2["op_id"], rec1["op_id"])
        self.assertEqual(rec2["supersedes"], rec1["op_id"])
        self.assertNotEqual(rec2["result"]["branch"],
                            rec1["result"]["branch"])
        body = rec2["stacked_drafts"]["pr2"]["body"]
        self.assertIn("supersedes `%s`" % rec1["op_id"], body)
        self.assertIn("qux", body)
        # M4: the old op file, drafts and branch are untouched.
        self.assertEqual(opf1.read_bytes(), before)
        # M4: the regen receipt shows a scrubbed env, scripts off.
        receipt = rec2["regen_receipt"]
        self.assertEqual(receipt["rc"], 0)
        self.assertTrue(receipt["scripts_off"])
        self.assertNotIn("M2_TEST_TOKEN", receipt["env_keys"])
        for key in receipt["env_keys"]:
            self.assertFalse(key.endswith("_TOKEN"))
            self.assertFalse(key.endswith("_SECRET"))
        self.assertNotIn("SSH_AUTH_SOCK", receipt["env_keys"])
        self.assertTrue(receipt["log_sha256"])
        # M2: the glance shows the newest rollup only.
        st = batches.m1_rollup_status("fakerepo")
        self.assertEqual(st["next"]["op"], rec2["op_id"])


class RegenDiskGuardTests(unittest.TestCase):
    def test_n2_blocks_under_30gb_naming_gb(self):
        ok, note = batches._regen_disk_ok(
            _avail_bytes=29 * 1024 ** 3)
        self.assertFalse(ok)
        self.assertIn("29.0", note)
        self.assertIn("30", note)

    def test_n2_passes_above_30gb(self):
        ok, note = batches._regen_disk_ok(
            _avail_bytes=31 * 1024 ** 3)
        self.assertTrue(ok)
        self.assertIn("31.0", note)


class RollupPlanReadyTests(RollupMixin):
    """M3/test 8: the driver plans READY leaves via evaluate() on the
    shared live map, then rolls them for real to stacked drafts."""

    MODE = "trio"

    def test_m3_plan_lists_ready_leaves_only(self):
        from openmainty import m2 as _m2
        plan = _m2._rollup_plan("fakerepo", "next")
        self.assertIsNotNone(plan)
        base_id, leaves = plan
        self.assertEqual(base_id, self.base_id)
        self.assertEqual(set(leaves), set(self.leaf_ids))

    def test_8_driver_tick_rolls_ready_leaves_to_stacked_drafts(self):
        from openmainty import m2 as _m2
        from openmainty import repos as _repos
        _repos.save({"name": "fakerepo", "github": "o/r",
                     "baseBranch": "next"})
        for stage in ("prepare", "fastcheck", "batch", "rollup"):
            _m2.set_policy("fakerepo", stage, True)
        out = _m2.tick_repo("fakerepo", _running="promoted")
        rolled = out.get("rolled") or []
        self.assertEqual(len(rolled), 1)
        self.assertEqual(rolled[0]["lane"], "next")
        rec = rolled[0]["rollup"]
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["op"], "rollup")
        pr2 = rec["stacked_drafts"]["pr2"]
        gh = [c for c in pr2["commands"]
              if c.strip().startswith("gh ")]
        self.assertTrue(gh)
        for cmd in gh:
            self.assertIn("--base ", cmd)
            self.assertIn("--head ", cmd)
        push = [c for c in pr2["commands"]
                if c.strip().startswith("git push")]
        self.assertTrue(push)
        self.assertLess(pr2["commands"].index(push[0]),
                        pr2["commands"].index(gh[0]))
        self.assertIn("gh pr edit", pr2["body"])
        self.assertIn("--base next", pr2["body"])


class RollupLockstepPreflightTests(unittest.TestCase):
    """M2.5d: a lane draft carrying a lockstep family member
    without its react-native bump is refused at draft time;
    config-only members draft with a WARNING line."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name) / "wt"
        self.wt.mkdir(parents=True)
        # FIX-b1: the fixture declares the family dep, like real
        # trees do — per-workspace validation only judges
        # workspaces that declare the dep.
        (self.wt / "package.json").write_text(
            json.dumps({"devDependencies":
                        {"react-native": "0.86.2",
                         "@react-native/metro-config": "0.86.1"}}),
            encoding="utf-8")

    def test_lane_rn_reads_declared_range(self):
        self.assertEqual(batches._rollup_lane_rn(str(self.wt)),
                         ("0.86.2", (0, 86)))

    def test_lane_rn_missing_is_silent(self):
        (self.wt / "package.json").write_text("{}", encoding="utf-8")
        self.assertEqual(batches._rollup_lane_rn(str(self.wt)),
                         ("", None))
        # No RN to compare against: everything passes.
        self.assertEqual(
            batches._rollup_lockstep_check(
                "", None,
                [{"dep": "@react-native/metro-config",
                  "target": "0.88.0"}]), [])

    def test_family_minor_mismatch_refuses_with_sentence(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches._rollup_lockstep_check(
                "0.86.2", (0, 86),
                [{"dep": "@react-native/metro-config",
                  "target": "0.88.0", "batch": "b"}],
                lang="ko")
        self.assertIn(
            "@react-native/metro-config 0.88.0은 react-native "
            "0.88와 함께 출시해야 함 (현재 0.86.2) — 빼거나 RN "
            "올림과 함께", str(ctx.exception))

    def test_same_minor_and_nonfamily_pass(self):
        leaves = [{"dep": "@react-native/metro-config",
                   "target": "0.86.3", "batch": "b"},
                  {"dep": "webpack", "target": "5.112.0",
                   "batch": "c"}]
        self.assertEqual(
            batches._rollup_lockstep_check("0.86.2", (0, 86),
                                           leaves), [])

    def test_config_only_warns_instead_of_refusing(self):
        leaves = [{"dep": "@react-native/typescript-config",
                   "target": "0.88.0", "batch": "b"}]
        warnings = batches._rollup_lockstep_check(
            "0.86.2", (0, 86), leaves, lang="ko")
        self.assertEqual(len(warnings), 1)
        self.assertIn("설정 전용", warnings[0])
        # Live PR2 shape: typescript-config 0.88.0 on RN 0.86.2.
        self.assertEqual(
            batches._rollup_lockstep_preflight(str(self.wt),
                                               leaves, lang="ko"),
            warnings)

    def test_draft_time_refusal_and_warning(self):
        kw = dict(repo="fakerepo", lane="next", base_id="no-batch",
                  base_head="0" * 40, head="1" * 40, changes={},
                  branch="codex/rollup-x", op_id="op-x",
                  new_path=str(self.wt), gates=[], lang="ko")
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches._rollup_stacked_drafts(
                leaves=[{"dep": "@react-native/metro-config",
                         "target": "0.88.0", "batch": "b",
                         "head": "2" * 40}],
                **kw)
        self.assertIn("함께 출시해야 함", str(ctx.exception))
        drafts = batches._rollup_stacked_drafts(
            leaves=[{"dep": "@react-native/typescript-config",
                     "target": "0.88.0", "batch": "b",
                     "head": "2" * 40}],
            **kw)
        self.assertIn("설정 전용", drafts["pr2"]["body"])


class RollupWorkspaceLockstepTests(unittest.TestCase):
    # FIX-b1 (Codex F7): rollup lockstep across workspaces. Root
    # RN 0.86 + example RN 0.85 with an example metro-config bump
    # to 0.86 must be refused naming the example workspace — the
    # old first-declaration-only read passed both preflights
    # against the root while the example stayed incompatible.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name) / "wt"
        (self.wt / "example").mkdir(parents=True)

    def _write(self, rel, data):
        d = self.wt / rel if rel else self.wt
        d.mkdir(parents=True, exist_ok=True)
        (d / "package.json").write_text(json.dumps(data),
                                        encoding="utf-8")

    def _f7_tree(self):
        self._write("", {"dependencies": {"react-native": "0.86.2"}})
        self._write("example",
                    {"dependencies": {"react-native": "0.85.3"},
                     "devDependencies":
                     {"@react-native/metro-config": "0.85.0"}})

    def _leaf(self, dep="@react-native/metro-config",
              target="0.86.0"):
        return [{"dep": dep, "target": target, "batch": "b"}]

    def test_example_mismatch_refuses_naming_example(self):
        self._f7_tree()
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches._rollup_lockstep_preflight(str(self.wt),
                                               self._leaf())
        self.assertIn("example", str(ctx.exception))
        self.assertIn("0.85", str(ctx.exception))
        self.assertIn("@react-native/metro-config 0.86.0",
                      str(ctx.exception))

    def test_single_workspace_match_passes(self):
        self._write("", {"dependencies": {"react-native": "0.86.2",
                         "@react-native/metro-config": "0.86.1"}})
        self.assertEqual(
            batches._rollup_lockstep_preflight(str(self.wt),
                                               self._leaf()), [])

    def test_workspace_without_rn_not_judged_by_root(self):
        self._write("", {"dependencies": {"react-native": "0.86.2"}})
        self._write("example",
                    {"devDependencies":
                     {"@react-native/metro-config": "0.85.0"}})
        leaves = [{"dep": "@react-native/metro-config",
                   "target": "0.88.0", "batch": "b"}]
        # The example declares no react-native, and the root does
        # not declare the dep — nothing to compare against.
        self.assertEqual(
            batches._rollup_lockstep_preflight(str(self.wt),
                                               leaves), [])


if __name__ == "__main__":
    unittest.main()

