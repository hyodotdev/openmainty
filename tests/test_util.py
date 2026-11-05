"""Durable coverage: cancellable subprocess helpers.

No quota, no network. Run with:
  python3 -m unittest discover -s tests
"""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import runner as _runner  # noqa: E402
from openmainty import util as _util  # noqa: E402
from openmainty.util import (  # noqa: E402
    Cancelled, git_env_for_readonly, git_readonly_child_env,
    iter_proc_lines, run_cmd)


class RunCmdTests(unittest.TestCase):
    def test_ok_returns_output(self):
        code, out, err = run_cmd(["echo", "hi"])
        self.assertEqual((code, out.strip()), (0, "hi"))

    def test_missing_binary_is_127(self):
        code, _, err = run_cmd(["openmainty-no-such-binary-xyz"])
        self.assertEqual(code, 127)
        self.assertIn("command not found", err)

    def test_timeout_preserves_partial_output(self):
        code, out, err = run_cmd(
            [sys.executable, "-c",
             "import sys, time; sys.stdout.write('part'); sys.stdout.flush(); time.sleep(30)"],
            timeout=2)
        self.assertEqual(code, 124)
        self.assertIn("part", out)
        self.assertIn("timed out", err)

    def test_timeout_is_124(self):
        start = time.monotonic()
        code, _, err = run_cmd(["sleep", "30"], timeout=0.3)
        self.assertEqual(code, 124)
        self.assertLess(time.monotonic() - start, 10)

    def test_cancel_kills_and_raises(self):
        ev = threading.Event()
        timer = threading.Timer(0.2, ev.set)
        timer.start()
        try:
            start = time.monotonic()
            with self.assertRaises(Cancelled):
                run_cmd(["sleep", "30"], timeout=60, cancel_event=ev)
            self.assertLess(time.monotonic() - start, 10)
        finally:
            timer.cancel()

    def test_pre_set_event_raises_without_running(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(Cancelled):
            run_cmd(["sleep", "30"], timeout=60, cancel_event=ev)


class IterLinesTests(unittest.TestCase):
    def test_collects_lines(self):
        proc = subprocess.Popen(["printf", "a\\nb\\n"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        lines = list(iter_proc_lines(proc, timeout=5, cancel_event=None))
        proc.wait(timeout=5)
        self.assertEqual([ln.strip() for ln in lines], ["a", "b"])

    def test_cancel_reaps_proc(self):
        ev = threading.Event()
        proc = subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        timer = threading.Timer(0.2, ev.set)
        timer.start()
        try:
            with self.assertRaises(Cancelled):
                for _ in iter_proc_lines(proc, timeout=60, cancel_event=ev):
                    pass
            self.assertIsNotNone(proc.poll())
        finally:
            timer.cancel()


class GitReadonlyEnvTests(unittest.TestCase):
    def test_status_gets_no_lock_env(self):
        self.assertEqual(
            git_env_for_readonly(["git", "-C", "/w", "status",
                                  "--porcelain"]),
            {**dict(os.environ),
             "GIT_OPTIONAL_LOCKS": "0"})

    def test_c_pairs_and_bare_flags_skipped(self):
        env = git_env_for_readonly(
            ["git", "-c", "core.fsmonitor=false", "-c",
             "core.hooksPath=/dev/null", "-C", "/w", "ls-files",
             "-z"])
        self.assertEqual(env.get("GIT_OPTIONAL_LOCKS"), "0")
        env = git_env_for_readonly(
            ["git", "--no-optional-locks", "diff", "--stat", "a",
             "b"])
        self.assertEqual(env.get("GIT_OPTIONAL_LOCKS"), "0")

    def test_symbolic_ref_single_name_only(self):
        env = git_env_for_readonly(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
        self.assertEqual(env.get("GIT_OPTIONAL_LOCKS"), "0")
        self.assertIsNone(git_env_for_readonly(
            ["git", "symbolic-ref", "HEAD", "refs/heads/x"]))

    def test_writing_commands_untouched(self):
        for argv in (["git", "commit", "-m", "x"],
                     ["git", "-C", "/w", "worktree", "add", "b",
                      "/wt", "base"],
                     ["git", "-C", "/w", "add", "-u"],
                     ["git", "remote", "get-url", "origin"],
                     ["git"],
                     ["notgit", "status"]):
            with self.subTest(argv=argv):
                self.assertIsNone(git_env_for_readonly(argv))

    def test_caller_env_preserved_not_mutated(self):
        given = {"A": "1"}
        out = git_env_for_readonly(["git", "status"], given)
        self.assertEqual(given, {"A": "1"})
        self.assertIsNot(given, out)
        self.assertEqual(
            out, {"A": "1", "GIT_OPTIONAL_LOCKS": "0"})

    def test_run_cmd_read_only_gets_env(self):
        seen = {}

        class _Popen:
            def __init__(self, cmd, **kw):
                seen["env"] = kw.get("env")
                self.returncode = 0

            def communicate(self, timeout=None):
                return "", ""

        import unittest.mock as _mock
        with _mock.patch("subprocess.Popen", _Popen):
            run_cmd(["git", "-C", "/w", "show", "HEAD:x"])
        self.assertEqual(seen["env"].get("GIT_OPTIONAL_LOCKS"),
                         "0")
        with _mock.patch("subprocess.Popen", _Popen):
            run_cmd(["git", "-C", "/w", "commit", "-m", "x"])
        self.assertNotIn("GIT_OPTIONAL_LOCKS", seen["env"] or {})


class PrivateIndexEnvTests(unittest.TestCase):
    def test_diff_gets_private_copy_then_removed(self):
        import subprocess as _sp
        import tempfile as _tf
        tmp = _tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wt = Path(tmp.name) / "r"
        wt.mkdir()
        _sp.run(["git", "init", "-b", "main"], cwd=str(wt),
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                timeout=60, check=True)
        (wt / "f.txt").write_text("x\n")
        _sp.run(["git", "add", "f.txt"], cwd=str(wt),
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                timeout=60, check=True)
        with git_readonly_child_env(
                ["git", "-C", str(wt), "diff", "--stat"]) as env:
            copy = env.get("GIT_INDEX_FILE")
            self.assertEqual(env.get("GIT_OPTIONAL_LOCKS"), "0")
            self.assertTrue(copy)
            self.assertTrue(Path(copy).is_file())
        self.assertFalse(Path(copy).exists())

    def test_status_with_cwd_param_gets_copy(self):
        import subprocess as _sp
        import tempfile as _tf
        tmp = _tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wt = Path(tmp.name) / "r"
        wt.mkdir()
        _sp.run(["git", "init", "-b", "main"], cwd=str(wt),
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                timeout=60, check=True)
        (wt / "f.txt").write_text("x\n")
        _sp.run(["git", "add", "f.txt"], cwd=str(wt),
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                timeout=60, check=True)
        with git_readonly_child_env(["git", "status"],
                                    cwd=str(wt)) as env:
            self.assertTrue(env.get("GIT_INDEX_FILE"))
        # No copy, no env: plain fallback, never raises.
        with git_readonly_child_env(
                ["git", "-C", "/nonexistent-wt-xyz", "diff",
                 "--stat"]) as env:
            self.assertEqual(env.get("GIT_OPTIONAL_LOCKS"), "0")
            self.assertNotIn("GIT_INDEX_FILE", env)

    def test_non_refresh_probes_get_no_copy(self):
        for argv in (["git", "show", "HEAD:x"],
                     ["git", "ls-files", "-z"],
                     ["git", "rev-parse", "--git-dir"],
                     ["git", "commit", "-m", "x"]):
            with self.subTest(argv=argv):
                with git_readonly_child_env(argv) as env:
                    if argv[1] == "commit":
                        self.assertIsNone(env)
                    else:
                        self.assertNotIn("GIT_INDEX_FILE", env)


class LinkedWorktreeIndexTests(unittest.TestCase):
    """M2.21: read-only probes must not refresh a linked
    worktree's index (it lives in the owner's .git)."""

    def setUp(self):
        import tempfile
        self._saved = os.environ.pop("GIT_OPTIONAL_LOCKS", None)
        self.addCleanup(self._restore_guard)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin = Path(self.tmp.name) / "origin"
        self.origin.mkdir()
        self._git(self.origin, "init", "-b", "main")
        self._git(self.origin, "config", "user.email", "t@t")
        self._git(self.origin, "config", "user.name", "t")
        (self.origin / "file.txt").write_text("one\n")
        self._git(self.origin, "add", "file.txt")
        self._git(self.origin, "commit", "-m", "one")
        self.wt = Path(self.tmp.name) / "wt"
        self._git(self.origin, "worktree", "add", str(self.wt),
                   "-b", "linked")
        gitdir = (self.wt / ".git").read_text().strip()
        self.assertIn(".git/worktrees/", gitdir)
        self.index = Path(gitdir.split(":", 1)[1].strip()) / "index"

    def _restore_guard(self):
        if self._saved is not None:
            os.environ["GIT_OPTIONAL_LOCKS"] = self._saved

    def _git(self, cwd, *args):
        import subprocess as _sp
        return _sp.run(["git", "-C", str(cwd)] + list(args),
                       stdout=_sp.PIPE, stderr=_sp.DEVNULL,
                       timeout=60, text=True)

    def _index_state(self):
        return (self.index.stat().st_mtime_ns,
                self.index.read_bytes())

    def _force_racy(self, target, i):
        """Force target's stat past everything git has cached.

        The mtime is set strictly newer than the index file
        itself (escalating per iteration), so every probe takes
        the slow path: re-hash, same content, refresh the entry.
        Without the private index copy that refresh is written
        back to the real index every single run (verified 20/20
        on Apple Git 2.54.0; plain equality with the index mtime
        never triggers there, 0/840 on the M2.21 fixture). With
        the copy, git can only refresh the copy: 0 writes.
        """
        fresh = (self.index.stat().st_mtime_ns
                 + (i + 2) * 1_000_000_000)
        os.utime(target, ns=(fresh, fresh))

    def _all_probes(self):
        self.assertEqual(_runner.source_snapshot(self.wt)["head"][:7],
                         self._git(self.wt, "rev-parse", "--short",
                                   "HEAD").stdout.strip()[:7])
        _runner._worktree_changed(self.wt)
        _runner._git_z(self.wt, ["ls-files", "-z"], "test")
        _runner._is_ignored(self.wt, "file.txt")
        run_cmd(["git", "-C", str(self.wt), "diff", "--stat"])
        run_cmd(["git", "-C", str(self.wt), "status",
                 "--porcelain"])

    def test_probes_leave_index_alone(self):
        target = self.wt / "file.txt"
        for i in range(20):
            self._force_racy(target, i)
            before = self._index_state()
            self._all_probes()
            self.assertEqual(self._index_state(), before)

    def test_probes_write_index_without_private_copy(self):
        """Teeth: with the M2.21b copy patched out, the same
        fixture must refresh the index on the very first probe —
        proving the fixture (not luck) is what the test above
        exercises."""
        import unittest.mock as _mock
        target = self.wt / "file.txt"
        self._force_racy(target, 0)
        before = self._index_state()
        with _mock.patch.object(_util, "_private_index_copy",
                                return_value=None):
            run_cmd(["git", "-C", str(self.wt), "diff",
                     "--stat"])
        self.assertNotEqual(self._index_state(), before,
                            "unprotected diff left the index alone; "
                            "the teeth check is blind")

    def test_diff_status_output_identical(self):
        """The private copy must not change what the probes
        report: dirty content reads the same with and without it."""
        import subprocess as _sp
        (self.wt / "file.txt").write_text("two\n")
        target = self.wt / "file.txt"
        self._force_racy(target, 0)
        code1, out1, _ = run_cmd(["git", "-C", str(self.wt),
                                  "diff", "--stat"])
        code2, out2, _ = run_cmd(["git", "-C", str(self.wt),
                                  "status", "--porcelain"])
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env.pop("GIT_INDEX_FILE", None)
        raw1 = _sp.run(["git", "-C", str(self.wt), "diff",
                        "--stat"], stdout=_sp.PIPE, stderr=_sp.DEVNULL,
                       timeout=60, text=True, env=env)
        raw2 = _sp.run(["git", "-C", str(self.wt), "status",
                        "--porcelain"], stdout=_sp.PIPE,
                       stderr=_sp.DEVNULL, timeout=60, text=True,
                       env=env)
        self.assertEqual(code1, raw1.returncode)
        self.assertEqual(out1, raw1.stdout)
        self.assertEqual(code2, raw2.returncode)
        self.assertEqual(out2, raw2.stdout)
        self.assertIn("file.txt", out1 + out2)

    def test_commit_still_writes_index(self):
        before = self._index_state()
        (self.wt / "file.txt").write_text("two\n")
        code, _, _ = run_cmd(
            ["git", "-C", str(self.wt), "add", "file.txt"])
        self.assertEqual(code, 0)
        code, _, _ = run_cmd(
            ["git", "-C", str(self.wt), "-c", "user.email=t@t",
             "-c", "user.name=t", "commit", "-m", "two"])
        self.assertEqual(code, 0)
        self.assertNotEqual(self._index_state(), before)


class PrivateIndexRacyTests(unittest.TestCase):
    """M2.22b: the private index copy must keep the index mtime.

    copyfile stamps "now" on the copy, which breaks git's
    racy-entry rule (Apple git compares at second granularity):
    a same-size edit in the same second as the index write then
    reads clean — the "clean tree" / "no commit" flakes. copy2
    keeps the rule firing exactly as on the real index.
    """

    def setUp(self):
        import subprocess as _sp
        import tempfile as _tf
        self._saved = os.environ.pop("GIT_OPTIONAL_LOCKS", None)
        self.addCleanup(self._restore_guard)
        self.tmp = _tf.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "r"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        self.target = self.repo / "file.txt"
        self.target.write_text("one\n")

    def _restore_guard(self):
        if self._saved is not None:
            os.environ["GIT_OPTIONAL_LOCKS"] = self._saved

    def _git(self, *args):
        import subprocess as _sp
        return _sp.run(["git", "-C", str(self.repo)] + list(args),
                       stdout=_sp.PIPE, stderr=_sp.DEVNULL,
                       timeout=60, text=True)

    def _commit_same_second_edit(self, committed, rewritten):
        """Commit one content, rewrite same-size immediately.

        The rewrite lands in the same (or newer) second as the
        fresh index write with equal size but different bytes —
        exactly the state git's racy-entry rule exists for."""
        self.target.write_text(committed)
        self._git("add", "file.txt")
        self._git("commit", "-m", "x")
        self.target.write_text(rewritten)
        self.assertEqual(len(committed), len(rewritten))

    def test_status_sees_same_second_same_size_edit(self):
        for i in range(3):
            base = "one\n" if i % 2 == 0 else "two\n"
            other = "two\n" if i % 2 == 0 else "one\n"
            self._commit_same_second_edit(base, other)
            time.sleep(1.2)
            code, out, _ = run_cmd(
                ["git", "-C", str(self.repo), "status",
                 "--porcelain"])
            self.assertEqual(code, 0)
            self.assertIn("file.txt", out,
                          f"round {i}: status missed a same-size "
                          f"edit through the private index copy")

    def test_worktree_changed_sees_it(self):
        self._commit_same_second_edit("one\n", "two\n")
        time.sleep(1.2)
        self.assertTrue(_runner._worktree_changed(self.repo))

    def test_copyfile_copy_misses_it(self):
        """Teeth: with copyfile patched back in, the same fixture
        must miss the edit — proving the regression test above
        exercises the mtime rule, not luck."""
        import shutil as _shutil
        import unittest.mock as _mock
        missed = 0
        for i in range(3):
            base = "one\n" if i % 2 == 0 else "two\n"
            other = "two\n" if i % 2 == 0 else "one\n"
            self._commit_same_second_edit(base, other)
            time.sleep(1.2)
            with _mock.patch.object(
                    _util.shutil, "copy2",
                    side_effect=lambda s, d: _shutil.copyfile(
                        s, d)):
                code, out, _ = run_cmd(
                    ["git", "-C", str(self.repo), "status",
                     "--porcelain"])
            self.assertEqual(code, 0)
            if "file.txt" not in out:
                missed += 1
        self.assertGreater(
            missed, 0,
            "copyfile copy never missed the edit; the teeth "
            "check is blind")


if __name__ == "__main__":
    unittest.main()
