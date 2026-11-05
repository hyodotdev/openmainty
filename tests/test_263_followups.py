"""Fail-closed source_snapshot (263): unreadable source is unknown.

Root's 261b repro: an unreadable directory hides source and yields a
FALSE GREEN — git status exits 0 with only a stderr warning, the
copy branch rglob silently skips, and .git presence flips by Python
version. This file pins the fix with real git and real chmod in
isolated homes (no deletes except TemporaryDirectory cleanup;
permissions restored before cleanup):

(a) the exact false-green repro raises, and the graph renders the
    run unverified with "current source unreadable; proof unverified";
(b) chmod 000 worktree root raises on both Pythons (version-independent);
(c) copy branch with an unreadable subdir raises;
(d) unreadable git-IGNORED or noise dirs stay readable (decision pin:
    neither can hide source — ignored dirs are outside git's untracked
    discovery by definition, and noise paths are excluded from the
    fingerprint by construction in every path);
(e) GOLDEN: success-path fingerprints identical to 261b (Root's
    3eda7b0b value plus values captured from 261b code pre-change);
(f) the disk seed never stores an entry for a raising worktree.
Plus the req-4 caller audit as tests: graph (in (a)), fastcheck,
batches snapshot status, rollup redraft refusal, and the
maybe_commit_worktree py39 no-crash path.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BASEDIR = "/tmp/openmainty-supervision-20261018"

sys.path.insert(0, str(ROOT / "src"))

from openmainty import batches, fastcheck, graph, repos, runner, store  # noqa: E402

try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


# Root-computed golden (test_source_read_failure): .gitignore with
# "*.log\nnode_modules/\n" + a.txt with "before\n". Reused verbatim.
GOLDEN_TWO_FILE_FP = "3eda7b0b4a72baf9b446957dacbc51409a58d690b96213a4ec899c0cfd766c53"

# Captured from 261b code (muse263-probe-golden-b.py) before the fix.
# Fixture: a.txt "before\n" + sub/b.txt "bee\n" committed, no ignores.
GOLDEN_CLEAN_FP = "b14b2c97a2a766f3ea947312dbad60512e48d85787a06a5ffca84723bd070c2b"
GOLDEN_DIRTY_FP = "538078c2820d772e3f3965f54c985d406bee52e0fa4bbc17d6f10f7669364a43"
GOLDEN_UNTRACKED_FILE_FP = "4123212e256437f3c82bd2cf05e413a497a2f5ebf082e4abe4ea197565c4d691"
GOLDEN_UNTRACKED_DIR_FP = "33b607848cf6c200574db855b5e3ce7953b727da94bbff90847fbdf70008234b"
# Copy tree: a.txt + x/b.txt + link.txt->a.txt + node_modules (excluded).
GOLDEN_COPY_FP = "0f8a9993ed4071aa29c4eae83eb1a99d608242c98dd9b5f4072dc2d8b7431ce5"


def _iso(test, env=None, drop=("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                               "GIT_CONFIG_NOSYSTEM", "XDG_CONFIG_HOME")):
    """Isolated HOME + OPENMAINTY_HOME; default live env shape."""
    tmp = tempfile.TemporaryDirectory(prefix="muse263-.", dir=BASEDIR)
    test.addCleanup(tmp.cleanup)
    base = Path(tmp.name)
    (base / "home").mkdir()
    use_temp_home(test, str(base / "omh"))
    vals = {"HOME": str(base / "home")}
    vals.update(env or {})
    patcher = mock.patch.dict(os.environ, vals)
    patcher.start()
    test.addCleanup(patcher.stop)
    for var in drop:
        if var not in vals:
            os.environ.pop(var, None)
    runner._SCOPE_CACHE = {"at": 0.0, "env": None, "origins": (),
                           "valid": False}
    graph._SNAP_MEMO.clear()
    graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}
    graph._SNAP_DISK_KNOWN = {"digest": None}
    test.addCleanup(graph._SNAP_MEMO.clear)
    return base


def _git(*args, cwd):
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@localhost",
         "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
        + list(args), cwd=str(cwd), capture_output=True, text=True,
        timeout=60)
    assert proc.returncode == 0, "git %s failed: %s" % (args, proc.stderr)
    return proc.stdout


def _make_profile(name="t1"):
    repos.save({"name": name, "github": "example/%s" % name,
                "baseBranch": "main", "knowledge": [], "checks": [],
                "e2e": [], "release": {}, "announce": {}})


def _seed_verified_run(repo, issue, files, worker="t263"):
    """Verified run whose worktree holds exactly `files` (rel -> text).

    Files are committed; the run's verified fingerprint is the clean
    snapshot. Returns (state, work).
    """
    st = store.create(repo, issue, "fail-closed run %s" % issue,
                      "fbase%s" % issue, {"worker": worker})
    st["steps"] = {"reproduce": {"status": "done", "exit_code": 0}}
    work = store.run_dir(st["id"]) / "work"
    work.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=work)
    for rel, text in files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "init", cwd=work)
    head = _git("rev-parse", "HEAD", cwd=work).strip()
    fp = runner.source_snapshot(work)["fingerprint"]
    st["workCommit"] = head
    st["verifiedFingerprints"] = {"reproduce": fp}
    st["verifiedCommits"] = {"reproduce": head}
    store.save(st)
    return st, work


def _chmod(test, path, mode=0, restore=0o755):
    os.chmod(path, mode)
    test.addCleanup(os.chmod, path, restore)


class FalseGreenReproTest(unittest.TestCase):
    # (a) Root's exact repro. Verified app.py; a new untracked source
    # file first flips the fingerprint honestly stale; chmod 000 on
    # its directory must then raise (never the verified fp again).
    # The graph renders the run unverified with the unreadable detail.

    def test_unreadable_new_source_raises_and_renders_unknown(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        st, work = _seed_verified_run("t1", "801", {"app.py": "print(1)\n"})
        verified = st["verifiedFingerprints"]["reproduce"]
        (work / "sub").mkdir()
        (work / "sub" / "new.py").write_text("print(2)\n", encoding="utf-8")
        stale = runner.source_snapshot(work)
        self.assertNotEqual(stale["fingerprint"], verified,
                            "new source first flips honestly stale")
        self.assertEqual(stale["untracked"], ["sub/new.py"])
        _chmod(self, work / "sub")
        with self.assertRaises(runner.SourceUnreadable):
            runner.source_snapshot(work)
        self.assertIsNone(runner.worktree_signature(work),
                          "the memo never serves an unreadable tree")
        out = graph.build("t1")
        verify_ids = {n["id"] for n in out["nodes"]
                      if n.get("type") == "verification"}
        self.assertNotIn("verify:%s:reproduce" % st["id"], verify_ids,
                         "no proof node for the unreadable run")
        runs = [n for n in out["nodes"] if n.get("type") == "run"]
        self.assertEqual(len(runs), 1)
        self.assertIn("current source unreadable; proof unverified",
                      runs[0].get("detail") or "",
                      "the detail says unreadable, never verified")


class WorktreeRootChmodTest(unittest.TestCase):
    # (b) chmod 000 on the worktree root raises SourceUnreadable on
    # BOTH Pythons (explicit lstat; no version-dependent .exists()).

    def test_chmod_000_root_raises(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        _st, work = _seed_verified_run("t1", "802", {"app.py": "print(1)\n"})
        _chmod(self, work)
        with self.assertRaises(runner.SourceUnreadable):
            runner.source_snapshot(work)
        self.assertIsNone(runner.worktree_signature(work))


class CopyUnreadableTest(unittest.TestCase):
    # (c) copy branch: an unreadable subdir raises (strict walk, no
    # silent rglob skip). No git anywhere in this fixture.

    def test_copy_unreadable_subdir_raises(self):
        base = _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        work = base / "copywork"
        (work / "sub").mkdir(parents=True)
        (work / "a.txt").write_text("before\n", encoding="utf-8")
        (work / "sub" / "c.txt").write_text("sea\n", encoding="utf-8")
        before = runner.source_snapshot(work)
        self.assertEqual(before["kind"], "copy")
        self.assertEqual(before["files"], 2)
        _chmod(self, work / "sub")
        with self.assertRaises(runner.SourceUnreadable):
            runner.source_snapshot(work)


class IgnoredNoiseUnreadableTest(unittest.TestCase):
    # (d) DECISION: an unreadable git-IGNORED or noise dir stays
    # readable (no flip to unknown). Neither can hide source:
    # ignored dirs are outside git's untracked discovery by
    # definition, and noise paths are excluded from the fingerprint
    # by construction in every path. Fingerprints are byte-identical
    # before and after the chmod.

    def _repo_with(self, gitignore, files):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        base = Path(os.environ["HOME"]).parent
        work = base / "ignwork"
        work.mkdir(parents=True, exist_ok=True)
        _git("init", cwd=work)
        (work / ".gitignore").write_text(gitignore, encoding="utf-8")
        (work / "a.txt").write_text("before\n", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "init", cwd=work)
        # Payload stays untracked: ignored in (d-i), plain noise in (d-ii).
        for rel, text in files.items():
            target = work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return work

    def test_unreadable_ignored_dir_stays_readable(self):
        work = self._repo_with("ignmod/\n", {"ignmod/y.py": "print(1)\n"})
        before = runner.source_snapshot(work)
        _chmod(self, work / "ignmod")
        after = runner.source_snapshot(work)
        self.assertEqual(after["fingerprint"], before["fingerprint"])
        self.assertNotIn("ignmod/y.py", after["untracked"])
        # The memo stays conservative (no serve) while the read
        # itself is exact: exclusion, not guesswork.
        self.assertIsNone(runner.worktree_signature(work))

    def test_unreadable_noise_dir_stays_readable(self):
        # node_modules deliberately NOT ignored here: only the noise
        # rule excuses it.
        work = self._repo_with("*.log\n",
                               {"node_modules/pkg/index.js": "1\n"})
        before = runner.source_snapshot(work)
        self.assertNotIn("node_modules/pkg/index.js", before["untracked"])
        _chmod(self, work / "node_modules")
        after = runner.source_snapshot(work)
        self.assertEqual(after["fingerprint"], before["fingerprint"])
        self.assertIsNone(runner.worktree_signature(work))


class UnreadableFileTest(unittest.TestCase):
    # (g) PIN (green before and after; real chmod where the existing
    # suite only mocks): an unreadable tracked or untracked source
    # file raises in git mode.

    def _repo(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        base = Path(os.environ["HOME"]).parent
        work = base / "filework"
        work.mkdir(parents=True, exist_ok=True)
        _git("init", cwd=work)
        (work / "app.py").write_text("print(1)\n", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "init", cwd=work)
        return work

    def test_unreadable_tracked_file_raises(self):
        work = self._repo()
        _chmod(self, work / "app.py", restore=0o644)
        with self.assertRaises(runner.SourceUnreadable):
            runner.source_snapshot(work)

    def test_unreadable_untracked_file_raises(self):
        work = self._repo()
        (work / "new.py").write_text("print(2)\n", encoding="utf-8")
        mid = runner.source_snapshot(work)
        self.assertEqual(mid["untracked"], ["new.py"])
        _chmod(self, work / "new.py", restore=0o644)
        with self.assertRaises(runner.SourceUnreadable):
            runner.source_snapshot(work)


class SymlinkDirTest(unittest.TestCase):
    # (h) PIN (green before and after): a symlinked dir pointing
    # outside the worktree is not descended; the golden is unchanged.

    def test_external_symlink_dir_not_descended(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        base = Path(os.environ["HOME"]).parent
        outside = base / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "secret.txt").write_text("not source\n",
                                            encoding="utf-8")
        work = base / "linkwork"
        work.mkdir(parents=True, exist_ok=True)
        _git("init", cwd=work)
        (work / "a.txt").write_text("before\n", encoding="utf-8")
        (work / "sub").mkdir()
        (work / "sub" / "b.txt").write_text("bee\n", encoding="utf-8")
        os.symlink(str(outside), work / "extlink")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "init", cwd=work)
        snap = runner.source_snapshot(work)
        again = runner.source_snapshot(work)
        self.assertEqual(again["fingerprint"], snap["fingerprint"])
        self.assertEqual(snap["untracked"], [])
        # The committed symlink records as one tracked path; the
        # outside target is never read (same as 261b rglob).
        self.assertNotIn("secret.txt",
                         " ".join(snap["untracked"]) + snap["fingerprint"])


class NestedUnreadableTest(unittest.TestCase):
    # (i) An unreadable dir INSIDE a git-ignored dir is excused, but
    # one inside a NON-ignored untracked dir raises.

    def _repo(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        base = Path(os.environ["HOME"]).parent
        work = base / "nestwork"
        work.mkdir(parents=True, exist_ok=True)
        _git("init", cwd=work)
        (work / ".gitignore").write_text("ignbig/\n", encoding="utf-8")
        (work / "a.txt").write_text("before\n", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "init", cwd=work)
        # Both trees below stay untracked: one ignored, one not.
        (work / "ignbig" / "inner").mkdir(parents=True)
        (work / "ignbig" / "inner" / "f.txt").write_text("x\n",
                                                        encoding="utf-8")
        (work / "udir").mkdir()
        (work / "udir" / "c.txt").write_text("sea\n", encoding="utf-8")
        (work / "udir" / "inner").mkdir()
        (work / "udir" / "inner" / "d.txt").write_text("dee\n",
                                                      encoding="utf-8")
        return work

    def test_unreadable_inside_ignored_is_excused(self):
        work = self._repo()
        before = runner.source_snapshot(work)
        _chmod(self, work / "ignbig" / "inner")
        after = runner.source_snapshot(work)
        self.assertEqual(after["fingerprint"], before["fingerprint"])

    def test_unreadable_inside_untracked_raises(self):
        work = self._repo()
        _chmod(self, work / "udir" / "inner")
        with self.assertRaises(runner.SourceUnreadable):
            runner.source_snapshot(work)


class SignatureSnapshotAgreementTest(unittest.TestCase):
    # (j) On every unreadable case the memo serves no fresh green:
    # either the signature is None (never consulted) or it moved
    # (chmod bumps ctime, so the memo misses) while the fresh read
    # raises. Proved through the real memo path after priming.

    def test_memo_and_snapshot_never_green_when_unreadable(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        st, work = _seed_verified_run("t1", "806", {"app.py": "print(1)\n"})
        key = str(work)
        primed = graph._memo_live_snapshot(st, work)
        self.assertIsNotNone(primed, "healthy tree memoises")
        primed_sig = runner.worktree_signature(work)
        self.assertIsNotNone(primed_sig)

        (work / "sub").mkdir()
        (work / "sub" / "new.py").write_text("print(2)\n", encoding="utf-8")
        _chmod(self, work / "sub")
        self.assertIsNone(runner.worktree_signature(work),
                          "unreadable dir: no signature to serve")
        self.assertIsNone(graph._memo_live_snapshot(st, work),
                          "unreadable dir: memo reports unknown")
        self.assertNotIn(key, graph._SNAP_MEMO)
        os.chmod(work / "sub", 0o755)

        _chmod(self, work / "app.py", restore=0o644)
        moved = runner.worktree_signature(work)
        self.assertIsNotNone(moved, "stats still read; only bytes hide")
        self.assertNotEqual(moved, primed_sig,
                            "chmod bumps ctime, so the memo misses")
        self.assertIsNone(graph._memo_live_snapshot(st, work),
                          "unreadable file: memo reports unknown")
        self.assertNotIn(key, graph._SNAP_MEMO)


class LevelBatchCostTest(unittest.TestCase):
    # Steer 1 cost: a healthy snapshot adds zero subprocesses, while
    # a wide ignored level is pruned with exactly one batch
    # check-ignore (no full walk). Counts run_cmd calls, not clocks.

    def _counted_snapshot(self, work):
        calls = []
        orig = runner.run_cmd

        def counting(cmd, **kw):
            calls.append(list(cmd))
            return orig(cmd, **kw)

        with mock.patch.object(runner, "run_cmd", side_effect=counting):
            snap = runner.source_snapshot(work)
        batches = [c for c in calls if "check-ignore" in c]
        return snap, calls, batches

    def test_narrow_tree_spawns_no_check_ignore(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        base = Path(os.environ["HOME"]).parent
        work = base / "narrowwork"
        work.mkdir(parents=True, exist_ok=True)
        _git("init", cwd=work)
        (work / "a.txt").write_text("before\n", encoding="utf-8")
        (work / "sub").mkdir()
        (work / "sub" / "b.txt").write_text("bee\n", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "init", cwd=work)
        _snap, calls, batches = self._counted_snapshot(work)
        self.assertEqual(batches, [],
                         "healthy narrow snapshot: zero added spawns")
        self.assertEqual(len(calls), 3, "rev-parse + ls-files + status")

    def test_wide_ignored_level_uses_one_batch(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        base = Path(os.environ["HOME"]).parent
        work = base / "widework"
        work.mkdir(parents=True, exist_ok=True)
        _git("init", cwd=work)
        (work / ".gitignore").write_text("gen/\n", encoding="utf-8")
        (work / "a.txt").write_text("before\n", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "init", cwd=work)
        for i in range(runner._WALK_LEVEL_BATCH_MIN + 50):
            (work / "gen" / ("d%04d" % i)).mkdir(parents=True)
        snap, _calls, batches = self._counted_snapshot(work)
        self.assertEqual(len(batches), 1,
                         "one batch prunes the wide ignored level")
        self.assertEqual(snap["files"], 2)
        self.assertEqual(snap["untracked"], [])


class SuccessFingerprintGoldenTest(unittest.TestCase):
    # (e) GUARD (green before and after): the success-path fingerprint
    # algorithm is byte-identical. Root's 3eda7b0b value plus five
    # values captured from 261b code pre-change (muse263-probe-golden-b).

    def _base_repo(self, name):
        base = Path(os.environ["HOME"]).parent
        work = base / name
        work.mkdir(parents=True, exist_ok=True)
        _git("init", cwd=work)
        (work / "a.txt").write_text("before\n", encoding="utf-8")
        (work / "sub").mkdir(exist_ok=True)
        (work / "sub" / "b.txt").write_text("bee\n", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "init", cwd=work)
        return work

    def test_goldens(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        snap = runner.source_snapshot(self._base_repo("g-clean"))
        self.assertEqual(snap["fingerprint"], GOLDEN_CLEAN_FP)
        self.assertEqual(snap["files"], 2)
        self.assertEqual(snap["untracked"], [])

        dirty = self._base_repo("g-dirty")
        (dirty / "a.txt").write_text("after\n", encoding="utf-8")
        snap = runner.source_snapshot(dirty)
        self.assertEqual(snap["fingerprint"], GOLDEN_DIRTY_FP)
        self.assertEqual(snap["files"], 2)

        unfile = self._base_repo("g-unfile")
        (unfile / "new.py").write_text("print('new')\n", encoding="utf-8")
        snap = runner.source_snapshot(unfile)
        self.assertEqual(snap["fingerprint"], GOLDEN_UNTRACKED_FILE_FP)
        self.assertEqual(snap["files"], 3)
        self.assertEqual(snap["untracked"], ["new.py"])

        undir = self._base_repo("g-undir")
        (undir / "udir").mkdir()
        (undir / "udir" / "c.txt").write_text("sea\n", encoding="utf-8")
        (undir / "udir" / "deep").mkdir()
        (undir / "udir" / "deep" / "d.txt").write_text("dee\n",
                                                      encoding="utf-8")
        snap = runner.source_snapshot(undir)
        self.assertEqual(snap["fingerprint"], GOLDEN_UNTRACKED_DIR_FP)
        self.assertEqual(snap["files"], 4)
        self.assertEqual(snap["untracked"],
                         ["udir/c.txt", "udir/deep/d.txt"])

        base = Path(os.environ["HOME"]).parent
        copyt = base / "g-copy"
        (copyt / "x").mkdir(parents=True, exist_ok=True)
        (copyt / "a.txt").write_text("before\n", encoding="utf-8")
        (copyt / "x" / "b.txt").write_text("bee\n", encoding="utf-8")
        (copyt / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
        (copyt / "node_modules" / "pkg" / "index.js").write_text(
            "1\n", encoding="utf-8")
        os.symlink("a.txt", copyt / "link.txt")
        snap = runner.source_snapshot(copyt)
        self.assertEqual(snap["kind"], "copy")
        self.assertEqual(snap["fingerprint"], GOLDEN_COPY_FP)
        self.assertEqual(snap["files"], 3)

        frozen = base / "g-frozen59"
        frozen.mkdir(parents=True, exist_ok=True)
        _git("init", cwd=frozen)
        (frozen / ".gitignore").write_text("*.log\nnode_modules/\n",
                                           encoding="utf-8")
        (frozen / "a.txt").write_text("before\n", encoding="utf-8")
        _git("add", "-A", cwd=frozen)
        _git("commit", "-qm", "init", cwd=frozen)
        snap = runner.source_snapshot(frozen)
        self.assertEqual(snap["fingerprint"], GOLDEN_TWO_FILE_FP)
        self.assertEqual(snap["files"], 2)


class DiskSeedExclusionTest(unittest.TestCase):
    # (f) the disk seed never stores a snapshot for a worktree that
    # raised: after a build with one raising run, the seed holds the
    # healthy entry only, and memory holds no entry for the broken one.

    def test_raising_worktree_leaves_no_seed_entry(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        st0, work0 = _seed_verified_run("t1", "803", {"app.py": "print(1)\n"})
        st1, _w1 = _seed_verified_run("t1", "804", {"app.py": "print(9)\n"})
        (work0 / "sub").mkdir()
        (work0 / "sub" / "new.py").write_text("print(2)\n", encoding="utf-8")
        _chmod(self, work0 / "sub")
        out = graph.build("t1")
        verify_ids = {n["id"] for n in out["nodes"]
                      if n.get("type") == "verification"}
        self.assertNotIn("verify:%s:reproduce" % st0["id"], verify_ids)
        self.assertIn("verify:%s:reproduce" % st1["id"], verify_ids)
        key0 = str(work0)
        self.assertNotIn(key0, graph._SNAP_MEMO,
                         "a failed read memoises nothing")
        import json
        disk_path = (Path(os.environ["OPENMAINTY_HOME"]) / "cache"
                     / graph._SNAP_DISK_NAME)
        raw = json.loads(disk_path.read_text(encoding="utf-8"))
        self.assertNotIn(key0, raw["entries"],
                         "the seed never stores a raising worktree")
        self.assertEqual(len(raw["entries"]), 1,
                         "only the healthy entry is stored")


def _broken_sub_work(test):
    """Worktree in the (a) false-green state; perms restored on cleanup."""
    _iso(test, env={"GIT_CONFIG_NOSYSTEM": "1"})
    base = Path(os.environ["HOME"]).parent
    work = base / "brokenwork"
    work.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=work)
    (work / "app.py").write_text("print(1)\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "init", cwd=work)
    (work / "sub").mkdir()
    (work / "sub" / "new.py").write_text("print(2)\n", encoding="utf-8")
    _chmod(test, work / "sub")
    return work


class NonGraphCallerTest(unittest.TestCase):
    # Req 4, non-graph paths: fail-closed surfaces, never green.

    def test_fastcheck_tree_identity_empty_on_unreadable(self):
        work = _broken_sub_work(self)
        self.assertEqual(fastcheck.tree_identity(str(work)), ("", ""))

    def test_batches_snapshot_status_maps_unreadable(self):
        work = _broken_sub_work(self)
        snap, category, op, _rc = batches._snapshot_status(str(work))
        self.assertIsNone(snap)
        self.assertEqual(category, "source_unreadable")
        self.assertTrue(op, "the failing op travels outward")

    def test_rollup_redraft_refuses_unreadable_worktree(self):
        work = _broken_sub_work(self)
        head = _git("rev-parse", "HEAD", cwd=work).strip()
        op_id = "t263-redraft-op"
        batches.write_json(batches.op_file(op_id), {
            "op_id": op_id, "op": "rollup", "status": "done",
            "result": {"path": str(work), "branch": "main",
                       "head": head, "fingerprint": "f" * 64}})
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.regenerate_rollup_drafts(op_id)
        self.assertIn("redraft refused", str(ctx.exception))
        self.assertIn("unreadable", str(ctx.exception))
        self.assertEqual(ctx.exception.http_status, 409)


class MaybeCommitUnreadableTest(unittest.TestCase):
    # Req 4: maybe_commit_worktree raises SourceUnreadable (not a raw
    # PermissionError, not a "non-git fixture" skip) when the worktree
    # root is unreadable — every caller catches it into blocked.

    def test_chmod_000_root_raises_source_unreadable(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        st, work = _seed_verified_run("t1", "805", {"app.py": "print(1)\n"})
        st["branch"] = "codex/t263-unreadable"
        store.save(st)
        _chmod(self, work)
        with self.assertRaises(runner.SourceUnreadable):
            runner.maybe_commit_worktree(st, work, "fix")
