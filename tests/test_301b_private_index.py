"""Slice 301b: read-only hardened git must not write the index (RED first).

M2.21b ran diff/status against a private per-call index copy. The
301 rewrites use the hardened env only, and GIT_OPTIONAL_LOCKS=0
does not stop `git diff` from refreshing and writing the index —
on the owner's own .git/index for the batches rows, and on the
run worktree's index (inside the owner's .git) for the run rows.

Each row test makes stat-only edits (os.utime, same bytes), runs
the real entry point once, and asserts the index bytes and mtime
are unchanged. Row 7 proves the writing path still uses the real
index. Rows 1-6 fail on the pre-301b code; row 7 passes throughout.
"""
import glob
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                               "..", "src"))

from openmainty import batches  # noqa: E402
from openmainty import prs  # noqa: E402
from openmainty import runner  # noqa: E402
from openmainty import serve as serve_mod  # noqa: E402


def _git(args, cwd, env):
    return subprocess.run(
        ["git"] + list(args), cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=60)


class _IndexCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t301b-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, exist_ok=True)
        self.empty = os.path.join(self.home, "empty-gitconfig")
        with open(self.empty, "w"):
            pass
        self.env = dict(os.environ)
        self.env.update(HOME=self.home,
                        GIT_CONFIG_GLOBAL=self.empty,
                        GIT_CONFIG_NOSYSTEM="1")
        self.env.pop("GIT_DIR", None)
        self.env.pop("GIT_WORK_TREE", None)
        omh = os.path.join(self.tmp, "omh")
        os.makedirs(omh, exist_ok=True)
        patch = mock.patch.dict(os.environ, {
            "HOME": self.home,
            "OPENMAINTY_HOME": omh,
            "GIT_CONFIG_GLOBAL": self.empty,
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        patch.start()
        self.addCleanup(patch.stop)

    def fresh(self, name="t"):
        base = os.path.join(self.tmp, name)
        os.makedirs(base, exist_ok=True)
        owner = os.path.join(base, "o")
        os.mkdir(owner)
        self.assertEqual(_git(["init", "-q", "."], owner,
                              self.env).returncode, 0)
        for i in (1, 2, 3):
            with open(os.path.join(owner, "f%d" % i), "w") as fh:
                fh.write("%d\n" % i)
        ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
        self.assertEqual(
            _git(ident + ["add", "."], owner, self.env).returncode, 0)
        self.assertEqual(
            _git(ident + ["commit", "-qm", "i"], owner,
                 self.env).returncode, 0)
        wt = os.path.join(base, "wt")
        self.assertEqual(
            _git(["worktree", "add", "-q", "--detach", wt, "HEAD"],
                 owner, self.env).returncode, 0)
        return owner, wt

    def bump(self, tree):
        for fname in ("f1", "f2"):
            path = os.path.join(tree, fname)
            st = os.stat(path)
            os.utime(path, (st.st_atime + 100, st.st_mtime + 100))

    def mk_other(self, name="other"):
        base = os.path.join(self.tmp, name)
        os.makedirs(base, exist_ok=True)
        other = os.path.join(base, "o")
        os.mkdir(other)
        self.assertEqual(_git(["init", "-q", "."], other,
                              self.env).returncode, 0)
        for i in (1, 2, 3):
            with open(os.path.join(other, "g%d" % i), "w") as fh:
                fh.write("other-%d\n" % i)
        ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
        self.assertEqual(
            _git(ident + ["add", "."], other, self.env).returncode, 0)
        self.assertEqual(
            _git(ident + ["commit", "-qm", "i"], other,
                 self.env).returncode, 0)
        return other

    def minimal_base(self):
        """301c: minimal caller env for child-env tests.

        Only PATH/HOME/TMPDIR plus the test gitconfig guard;
        never dict(os.environ), so a test failure cannot print
        inherited owner secrets.
        """
        base = {}
        for key in ("PATH", "TMPDIR", "TMP", "TEMP"):
            if key in os.environ:
                base[key] = os.environ[key]
        base["HOME"] = self.home
        base["GIT_CONFIG_GLOBAL"] = self.empty
        base["GIT_CONFIG_NOSYSTEM"] = "1"
        return base

    def owner_index(self, owner):
        return os.path.join(owner, ".git", "index")

    def worktree_index(self, owner):
        hits = glob.glob(os.path.join(owner, ".git", "worktrees",
                                      "*", "index"))
        self.assertEqual(len(hits), 1)
        return hits[0]

    def digest(self, path):
        with open(path, "rb") as fh:
            data = fh.read()
        return (hashlib.sha256(data).hexdigest(),
                os.stat(path).st_mtime_ns)

    def assert_index_untouched(self, index, fn):
        before = self.digest(index)
        fn()
        self.assertEqual(self.digest(index), before)


class BatchesOwnerIndexTest(_IndexCase):
    def test_git_text_diff_stat_leaves_owner_index(self):
        owner, _ = self.fresh("b1")
        self.bump(owner)
        self.assert_index_untouched(
            self.owner_index(owner),
            lambda: batches._git_text(["diff", "--stat"], owner))

    def test_git_lines_diff_stat_leaves_owner_index(self):
        owner, _ = self.fresh("b2")
        self.bump(owner)
        self.assert_index_untouched(
            self.owner_index(owner),
            lambda: batches._git_lines(["diff", "--stat"], owner))


class BoundReadLeavesIndexTest(_IndexCase):
    """301c: bound reads leave the worktree index alone.

    Each test calls the site WITH git_bound after a stat-only
    bump and asserts the real index (bytes + mtime) is untouched
    while the read itself answers. Removing only that site's
    private-index wrap makes its test fail.
    """
    def _bound_bumped(self, name):
        owner, wt = self.fresh(name)
        rec = runner.record_git_pointer(wt)
        self.bump(wt)
        return owner, wt, rec

    def test_batches_git_text_bound(self):
        owner, wt, rec = self._bound_bumped("bbt")
        out = []
        self.assert_index_untouched(
            self.worktree_index(owner),
            lambda: out.append(batches._git_text(
                ["diff", "--stat"], wt, git_bound=rec)))
        self.assertEqual(out, [""])

    def test_batches_git_lines_bound(self):
        owner, wt, rec = self._bound_bumped("bbl")
        out = []
        self.assert_index_untouched(
            self.worktree_index(owner),
            lambda: out.append(batches._git_lines(
                ["diff", "--stat"], wt, git_bound=rec)))
        self.assertEqual(out, [[]])

    def test_prs_diffstat_bound(self):
        owner, wt, rec = self._bound_bumped("pbd")
        out = []
        self.assert_index_untouched(
            self.worktree_index(owner),
            lambda: out.append(prs.work_diffstat(
                wt, "", git_bound=rec)))
        self.assertEqual(out, ["변경 없음"])


class Task2PinnedProbeTest(_IndexCase):
    """301c RED: util._private_index_copy must honor the pinned
    gitdir and the caller's scrubbed env. Both fail pre-fix."""

    def test_probe_flags_match_hardened_config(self):
        from openmainty import util as _util
        self.assertEqual(list(_util._HARDENED_PROBE_CONFIG),
                         list(runner._HARDENED_GIT_CONFIG))

    def test_pinned_argv_copies_pinned_gitdir_index(self):
        from openmainty.util import git_readonly_child_env
        owner, wt = self.fresh("pa")
        self.bump(wt)
        other = self.mk_other("pa-other")
        pinned = runner.hardened_git_argv(
            ["diff", "--stat"],
            gitdir=os.path.join(other, ".git"), worktree=wt)
        child = runner.hardened_git_env(self.minimal_base())
        with mock.patch("openmainty.util.subprocess.run",
                        side_effect=AssertionError("spawned")):
            with git_readonly_child_env(
                    pinned, child,
                    cwd=None) as env:
                # Pre-fix the probe itself spawns (mock raises) and
                # no copy is made: a FAIL here, never an error, and
                # never a dump of the env mapping.
                self.assertTrue("GIT_INDEX_FILE" in env,
                                "GIT_INDEX_FILE not set")
                copied = env["GIT_INDEX_FILE"]
                with open(copied, "rb") as fh:
                    copied_bytes = fh.read()
        with open(os.path.join(other, ".git", "index"), "rb") as fh:
            self.assertEqual(copied_bytes, fh.read())

    def test_caller_env_not_os_environ_for_probe(self):
        from openmainty.util import git_readonly_child_env
        owner, wt = self.fresh("pb")
        self.bump(wt)
        evil = self.mk_other("pb-evil")
        argv = runner.hardened_git_argv(
            ["-C", wt, "diff", "--stat"])
        scrubbed = runner.hardened_git_env(self.minimal_base())
        with mock.patch.dict(os.environ, {
                "GIT_DIR": os.path.join(evil, ".git"),
                "GIT_INDEX_FILE": os.path.join(
                    evil, ".git", "index")}):
            with git_readonly_child_env(
                    argv, scrubbed, wt) as env:
                copied = env["GIT_INDEX_FILE"]
                with open(copied, "rb") as fh:
                    copied_bytes = fh.read()
        with open(self.worktree_index(owner), "rb") as fh:
            self.assertEqual(copied_bytes, fh.read())


class BoundBatchesSnapshotTest(_IndexCase):
    def test_snapshot_bound_leaves_worktree_index(self):
        owner, wt = self.fresh("b3")
        rec = runner.record_git_pointer(wt)
        self.bump(wt)
        out = []
        self.assert_index_untouched(
            self.worktree_index(owner),
            lambda: out.append(
                batches._snapshot(wt, git_bound=rec)))
        self.assertIsNotNone(out[0])


class RunWorktreeIndexTest(_IndexCase):
    def test_serve_git_text_diff_leaves_worktree_index(self):
        owner, wt = self.fresh("s1")
        self.bump(wt)
        self.assert_index_untouched(
            self.worktree_index(owner),
            lambda: serve_mod._git_text(Path(wt), ["diff", "--"]))

    def test_snapshot_diff_bound_leaves_worktree_index(self):
        owner, wt = self.fresh("s2")
        self.bump(wt)
        rec = runner.record_git_pointer(wt)
        self.assert_index_untouched(
            self.worktree_index(owner),
            lambda: runner.snapshot_diff(
                Path(wt), Path(wt).parent, "probe", None,
                git_bound=rec))

    def test_hgit_run_bound_diff_leaves_worktree_index(self):
        owner, wt = self.fresh("s3")
        self.bump(wt)
        rec = runner.record_git_pointer(wt)
        self.assert_index_untouched(
            self.worktree_index(owner),
            lambda: runner._hgit_run(
                wt, ["diff", "--stat"], git_bound=rec, timeout=30))

    def test_prs_diffstat_leaves_worktree_index(self):
        owner, wt = self.fresh("s4")
        self.bump(wt)
        out = []
        self.assert_index_untouched(
            self.worktree_index(owner),
            lambda: out.append(prs.work_diffstat(wt, "")))
        self.assertTrue(out and isinstance(out[0], str))


class CommitStillUsesRealIndexTest(_IndexCase):
    def test_maybe_commit_stages_and_commits(self):
        from openmainty import store as _store
        owner, wt = self.fresh("c1")
        rdir = _store.run_dir("t301b-commit")
        if rdir.exists():
            shutil.rmtree(rdir, ignore_errors=True)
        rdir.mkdir(parents=True, exist_ok=True)
        target = rdir / "work"
        proc = _git(["worktree", "add", "--detach", str(target),
                     "HEAD"], owner, self.env)
        self.assertEqual(proc.returncode, 0)
        with open(os.path.join(str(target), "f1"), "w") as fh:
            fh.write("changed-bytes\n")
        rec = runner.record_git_pointer(str(target))
        head_before = _git(["rev-parse", "HEAD"], str(target),
                            self.env).stdout.decode().strip()
        state = {"id": "t301b-commit", "branch": "b",
                 "baseCommit": head_before,
                 "workCommit": head_before,
                 "workSeq": 0, "steps": {}, "workGit": rec}
        out = runner.maybe_commit_worktree(
            state, Path(str(target)), "fix")
        self.assertTrue(out.get("committed"), out)
        head_after = _git(["rev-parse", "HEAD"], str(target),
                           self.env).stdout.decode().strip()
        self.assertNotEqual(head_after, head_before)
        shown = _git(["show", "%s:f1" % head_after], str(target),
                     self.env).stdout.decode()
        self.assertEqual(shown, "changed-bytes\n")


if __name__ == "__main__":
    unittest.main()
