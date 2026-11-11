"""Slice 302: hardened reads must ignore in-tree .gitattributes (RED first).

A model can add `* filter=x` / `* diff=y` to .gitattributes with the
driver defined in the repo's own config. core.attributesFile=/dev/null
only drops the GLOBAL file, so pre-302 reads run the planted commands.
Each test plants a filter driver (clean) and a diff driver (command)
whose only effect is `tee`-ing a marker file, runs a real read entry
point on a linked worktree, and asserts the markers are ABSENT (plus a
basic answer shape). Pre-fix every test FAILS with the marker present.

Marker commands are `tee <abs path>`: single binary, no shell needed,
passthrough-safe. Child envs are minimal (PATH/HOME/test gitconfig);
no test ever asserts on or prints an env mapping.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                "..", "src"))

from openmainty import batches  # noqa: E402
from openmainty import fastcheck  # noqa: E402
from openmainty import prs  # noqa: E402
from openmainty import runner  # noqa: E402
from openmainty import serve as serve_mod  # noqa: E402


def _git(args, cwd, env):
    return subprocess.run(
        ["git"] + list(args), cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=60)


class _AttrCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t302-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, exist_ok=True)
        self.empty = os.path.join(self.home, "empty-gitconfig")
        with open(self.empty, "w"):
            pass
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": self.home,
                    "GIT_CONFIG_GLOBAL": self.empty,
                    "GIT_CONFIG_NOSYSTEM": "1"}
        patch = mock.patch.dict(os.environ, {
            "HOME": self.home,
            "OPENMAINTY_HOME": os.path.join(self.tmp, "omh"),
            "GIT_CONFIG_GLOBAL": self.empty,
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        os.makedirs(os.path.join(self.tmp, "omh"), exist_ok=True)
        patch.start()
        self.addCleanup(patch.stop)

    def fresh(self, name="t", with_diff_attr=True):
        """Owner repo + linked worktree with planted drivers.

        .gitattributes names filter x (clean) and, unless
        with_diff_attr is False, diff driver y (command); both
        drivers are defined in the worktree config and only
        `tee` a marker. f1 gets a REAL content change so diffs
        are non-empty and the drivers fire.
        """
        base = os.path.join(self.tmp, name)
        os.makedirs(base, exist_ok=True)
        owner = os.path.join(base, "o")
        os.mkdir(owner)
        self.assertEqual(_git(["init", "-q", "."], owner,
                              self.env).returncode, 0)
        with open(os.path.join(owner, "f1"), "w") as fh:
            fh.write("one\n")
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
        marks = {"clean": os.path.join(base, "MARK-clean"),
                 "diff": os.path.join(base, "MARK-diff"),
                 "ext": os.path.join(base, "MARK-ext")}
        attrs = "* filter=x\n"
        if with_diff_attr:
            attrs += "* diff=y\n"
        with open(os.path.join(wt, ".gitattributes"), "w") as fh:
            fh.write(attrs)
        self.assertEqual(
            _git(["-C", wt, "config", "filter.x.clean",
                  "tee %s" % marks["clean"]],
                 wt, self.env).returncode, 0)
        if with_diff_attr:
            self.assertEqual(
                _git(["-C", wt, "config", "diff.y.command",
                      "tee %s" % marks["diff"]],
                     wt, self.env).returncode, 0)
        with open(os.path.join(wt, "f1"), "a") as fh:
            fh.write("two\n")
        return owner, wt, marks

    def assert_no_markers(self, marks, *names):
        for key in names:
            self.assertFalse(os.path.exists(marks[key]),
                             "driver ran: %s exists" % marks[key])


class BatchesAttrTest(_AttrCase):
    def test_text_bound_ignores_attributes(self):
        _, wt, marks = self.fresh("btb")
        rec = runner.record_git_pointer(wt)
        out = batches._git_text(["diff"], wt, git_bound=rec)
        self.assert_no_markers(marks, "diff", "clean")
        self.assertIsNotNone(out)

    def test_text_unbound_ignores_attributes(self):
        _, wt, marks = self.fresh("btu")
        out = batches._git_text(["diff"], wt)
        self.assert_no_markers(marks, "diff", "clean")
        self.assertIsNotNone(out)

    def test_lines_bound_ignores_attributes(self):
        _, wt, marks = self.fresh("blb")
        rec = runner.record_git_pointer(wt)
        out = batches._git_lines(["diff"], wt, git_bound=rec)
        self.assert_no_markers(marks, "diff", "clean")
        self.assertIsNotNone(out)

    def test_lines_unbound_ignores_attributes(self):
        _, wt, marks = self.fresh("blu")
        out = batches._git_lines(["diff"], wt)
        self.assert_no_markers(marks, "diff", "clean")
        self.assertIsNotNone(out)


class ServeAttrTest(_AttrCase):
    def test_serve_bound_ignores_attributes(self):
        _, wt, marks = self.fresh("svb")
        rec = runner.record_git_pointer(wt)
        code, _, _ = serve_mod._git_text(Path(wt), ["diff"],
                                         git_bound=rec)
        self.assert_no_markers(marks, "diff", "clean")
        self.assertEqual(code, 0)

    def test_serve_unbound_ignores_attributes(self):
        _, wt, marks = self.fresh("svu")
        code, _, _ = serve_mod._git_text(Path(wt), ["diff"])
        self.assert_no_markers(marks, "diff", "clean")
        self.assertEqual(code, 0)


class PrsAttrTest(_AttrCase):
    def test_diffstat_bound_ignores_attributes(self):
        # --stat renders no per-file diff: only the clean filter
        # can fire here (verified on this git).
        _, wt, marks = self.fresh("pbd")
        rec = runner.record_git_pointer(wt)
        out = prs.work_diffstat(wt, "", git_bound=rec)
        self.assert_no_markers(marks, "clean")
        self.assertTrue(isinstance(out, str) and out)

    def test_diffstat_unbound_ignores_attributes(self):
        _, wt, marks = self.fresh("pdu")
        out = prs.work_diffstat(wt, "")
        self.assert_no_markers(marks, "clean")
        self.assertTrue(isinstance(out, str) and out)


class RunnerAttrTest(_AttrCase):
    def test_hgit_run_bound_ignores_attributes(self):
        _, wt, marks = self.fresh("rhb")
        rec = runner.record_git_pointer(wt)
        code, _, _ = runner._hgit_run(
            wt, ["diff"], git_bound=rec, timeout=30)
        self.assert_no_markers(marks, "diff", "clean")
        self.assertEqual(code, 0)

    def test_hgit_run_unbound_ignores_attributes(self):
        _, wt, marks = self.fresh("rhu")
        code, _, _ = runner._hgit_run(wt, ["diff"], timeout=30)
        self.assert_no_markers(marks, "diff", "clean")
        self.assertEqual(code, 0)

    def test_snapshot_diff_bound_ignores_attributes(self):
        _, wt, marks = self.fresh("rsb")
        rec = runner.record_git_pointer(wt)
        rdir = Path(self.tmp) / "rsb-out"
        rdir.mkdir(parents=True, exist_ok=True)
        out = runner.snapshot_diff(Path(wt), rdir, "n",
                                   None, git_bound=rec)
        self.assert_no_markers(marks, "clean")
        self.assertIsNotNone(out)

    def test_snapshot_diff_unbound_ignores_attributes(self):
        _, wt, marks = self.fresh("rsu")
        rdir = Path(self.tmp) / "rsu-out"
        rdir.mkdir(parents=True, exist_ok=True)
        out = runner.snapshot_diff(Path(wt), rdir, "n", None)
        self.assert_no_markers(marks, "clean")
        self.assertIsNotNone(out)


class FastcheckAttrTest(_AttrCase):
    def test_fc_run_bound_ignores_attributes(self):
        _, wt, marks = self.fresh("fcb")
        rec = runner.record_git_pointer(wt)
        text, ok = fastcheck._fc_run(wt, ["diff"], git_bound=rec)
        self.assert_no_markers(marks, "diff", "clean")
        self.assertTrue(ok)

    def test_fc_run_unbound_ignores_attributes(self):
        _, wt, marks = self.fresh("fcu")
        text, ok = fastcheck._fc_run(wt, ["diff"])
        self.assert_no_markers(marks, "diff", "clean")
        self.assertTrue(ok)


class StatusAttrTest(_AttrCase):
    """302b P10: status reads must not run the clean filter.

    `git status` converts racy stat-clean entries through the
    clean filter to verify them, so the fixture forces that
    state: f1 is really modified, f2/f3 are untouched but
    utime-forced into the read tree's index second at the same
    size (deterministic racy). Without --attr-source on status
    the clean marker appears; with it, neither marker does.
    Smudge is planted too (per P10) and asserted absent.
    """
    def fresh_status(self, name="st"):
        import glob as _glob
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
        marks = {"clean": os.path.join(base, "MARK-clean"),
                 "smudge": os.path.join(base, "MARK-smudge")}
        for tree in (owner, wt):
            with open(os.path.join(tree, ".gitattributes"), "w") as fh:
                fh.write("* filter=x diff=y\n")
            self.assertEqual(
                _git(["config", "filter.x.clean",
                      "tee %s" % marks["clean"]],
                     tree, self.env).returncode, 0)
            self.assertEqual(
                _git(["config", "filter.x.smudge",
                      "tee %s" % marks["smudge"]],
                     tree, self.env).returncode, 0)
            with open(os.path.join(tree, "f1"), "w") as fh:
                fh.write("changed\n")
        wt_index = _glob.glob(os.path.join(
            owner, ".git", "worktrees", "*", "index"))
        self.assertEqual(len(wt_index), 1)
        # Deterministic racy: untouched files at the index's own
        # second and size read as same-second entries, which
        # status must convert to verify.
        for tree, index in ((owner, os.path.join(owner, ".git",
                                                 "index")),
                            (wt, wt_index[0])):
            stamp = os.stat(index).st_mtime_ns
            for fname in ("f2", "f3"):
                os.utime(os.path.join(tree, fname),
                         ns=(stamp, stamp))
        return owner, wt, marks

    def test_lines_bound_status_ignores_attributes(self):
        _, wt, marks = self.fresh_status("slb")
        rec = runner.record_git_pointer(wt)
        out = batches._git_lines(["status", "--porcelain"], wt,
                                 git_bound=rec)
        self.assert_no_markers(marks, "clean", "smudge")
        self.assertIsNotNone(out)

    def test_lines_unbound_status_ignores_attributes(self):
        owner, _, marks = self.fresh_status("slu")
        out = batches._git_lines(["status", "--porcelain"], owner)
        self.assert_no_markers(marks, "clean", "smudge")
        self.assertIsNotNone(out)

    def test_hgit_run_bound_status_ignores_attributes(self):
        _, wt, marks = self.fresh_status("shb")
        rec = runner.record_git_pointer(wt)
        code, _, _ = runner._hgit_run(
            wt, ["status", "--porcelain"], git_bound=rec,
            timeout=30)
        self.assert_no_markers(marks, "clean", "smudge")
        self.assertEqual(code, 0)

    def test_hgit_run_unbound_status_ignores_attributes(self):
        _, wt, marks = self.fresh_status("shu")
        code, _, _ = runner._hgit_run(
            wt, ["status", "--porcelain"], timeout=30)
        self.assert_no_markers(marks, "clean", "smudge")
        self.assertEqual(code, 0)


class ProbeCacheTest(unittest.TestCase):
    """302c: only a definitive probe answer is cached.

    A hung probe surfaces as a timeout (never "unsupported",
    never cached), so the next call on a working git succeeds.
    A git that runs and rejects the flag (nonzero exit) is
    cached False and reads fail closed. No fixture: these call
    the probe path directly with a stubbed spawn.
    """
    def _reset_cache(self):
        prev = runner._ATTR_SOURCE_OK
        runner._ATTR_SOURCE_OK = None
        self.addCleanup(setattr, runner, "_ATTR_SOURCE_OK", prev)

    def test_probe_timeout_not_cached(self):
        self._reset_cache()
        real_run = runner.subprocess.run
        state = {"fired": False}

        def flaky(*args, **kwargs):
            if not state["fired"]:
                state["fired"] = True
                raise subprocess.TimeoutExpired(
                    args[0] if args else "git", 3)
            return real_run(*args, **kwargs)

        with mock.patch.object(runner.subprocess, "run",
                               side_effect=flaky):
            with self.assertRaises(subprocess.TimeoutExpired):
                runner.hardened_git_argv(["diff"])
            self.assertIsNone(runner._ATTR_SOURCE_OK)
            argv = runner.hardened_git_argv(["diff"])
        self.assertIn("--attr-source=" +
                      runner._ATTR_SOURCE_EMPTY_TREE, argv)

    def test_probe_refusal_cached_false(self):
        self._reset_cache()
        refused = subprocess.CompletedProcess(
            ["git"], 129, stdout="", stderr="")
        with mock.patch.object(runner.subprocess, "run",
                               return_value=refused) as spy:
            with self.assertRaises(runner.SourceUnreadable):
                runner.hardened_git_argv(["diff"])
            with self.assertRaises(runner.SourceUnreadable):
                runner.hardened_git_argv(["diff"])
        self.assertFalse(runner._ATTR_SOURCE_OK)
        self.assertEqual(spy.call_count, 1)

    def test_probe_oserror_not_cached(self):
        """302d Q5: a spawn failure (OSError) is not an answer:
        the next call on a working git succeeds (nothing cached
        False)."""
        self._reset_cache()
        real_run = runner.subprocess.run
        state = {"fired": False}

        def flaky(*args, **kwargs):
            if not state["fired"]:
                state["fired"] = True
                raise OSError(1, "denied")
            return real_run(*args, **kwargs)

        with mock.patch.object(runner.subprocess, "run",
                               side_effect=flaky) as spy:
            self.assertTrue(runner.attr_source_supported())
            self.assertIsNone(runner._ATTR_SOURCE_OK)
            self.assertTrue(runner.attr_source_supported())
        self.assertTrue(runner._ATTR_SOURCE_OK)
        self.assertEqual(spy.call_count, 2)


class HungHgitTest(_AttrCase):
    """302d Q3: a hung probe surfaces as _hgit_run's own timeout
    (124), bound and unbound alike — never a refusal, never
    cached."""

    def test_hgit_run_unbound_hung_124(self):
        _, wt, _ = self.fresh("hu1")
        with mock.patch.object(
                runner, "attr_source_supported",
                side_effect=subprocess.TimeoutExpired("git", 3)):
            code, _, _ = runner._hgit_run(wt, ["diff"], timeout=30)
        self.assertEqual(code, 124)

    def test_hgit_run_bound_hung_124(self):
        _, wt, _ = self.fresh("hu2")
        rec = runner.record_git_pointer(wt)
        with mock.patch.object(
                runner, "attr_source_supported",
                side_effect=subprocess.TimeoutExpired("git", 3)):
            code, _, _ = runner._hgit_run(
                wt, ["diff"], git_bound=rec, timeout=30)
        self.assertEqual(code, 124)


class TypedGitErrorTest(_AttrCase):
    """302d part B: derived_fix._git and issue_recovery._git must
    surface probe failures as their typed 502 (code
    "unreadable"), never raw. Bound pointer refusal stays
    git-pointer-changed (pinned by the *_pointer tests)."""

    def _workdir(self, label):
        work = os.path.join(self.tmp, label)
        os.mkdir(work)
        return work

    def _assert_unreadable(self, exc, modname):
        self.assertEqual(getattr(exc, "http_status", None), 502,
                         modname)
        self.assertEqual(getattr(exc, "code", None), "unreadable",
                         modname)

    def test_derived_fix_unsupported_typed(self):
        from openmainty import derived_fix as _df
        work = self._workdir("dfu")
        with mock.patch.object(runner, "attr_source_supported",
                               return_value=False):
            with self.assertRaises(_df.DerivedFixError) as ctx:
                _df._git(work, ["rev-parse", "HEAD"])
        self._assert_unreadable(ctx.exception, "derived_fix")

    def test_derived_fix_hung_typed(self):
        from openmainty import derived_fix as _df
        work = self._workdir("dfh")
        with mock.patch.object(
                runner, "attr_source_supported",
                side_effect=subprocess.TimeoutExpired("git", 3)):
            with self.assertRaises(_df.DerivedFixError) as ctx:
                _df._git(work, ["rev-parse", "HEAD"])
        self._assert_unreadable(ctx.exception, "derived_fix")

    def test_derived_fix_pointer_stays(self):
        from openmainty import derived_fix as _df
        _, wt_a, _ = self.fresh("dfpa")
        _, wt_b, _ = self.fresh("dfpb")
        rec_a = runner.record_git_pointer(wt_a)
        with self.assertRaises(_df.DerivedFixError) as ctx:
            _df._git(wt_b, ["rev-parse", "HEAD"], git_bound=rec_a)
        self.assertEqual(getattr(ctx.exception, "code", None),
                         "git-pointer-changed")

    def test_issue_recovery_unsupported_typed(self):
        from openmainty import issue_recovery as _ir
        work = self._workdir("iru")
        with mock.patch.object(runner, "attr_source_supported",
                               return_value=False):
            with self.assertRaises(_ir.IssueRecoveryError) as ctx:
                _ir._git(work, ["rev-parse", "HEAD"])
        self._assert_unreadable(ctx.exception, "issue_recovery")

    def test_issue_recovery_hung_typed(self):
        from openmainty import issue_recovery as _ir
        work = self._workdir("irh")
        with mock.patch.object(
                runner, "attr_source_supported",
                side_effect=subprocess.TimeoutExpired("git", 3)):
            with self.assertRaises(_ir.IssueRecoveryError) as ctx:
                _ir._git(work, ["rev-parse", "HEAD"])
        self._assert_unreadable(ctx.exception, "issue_recovery")

    def test_issue_recovery_pointer_stays(self):
        from openmainty import issue_recovery as _ir
        _, wt_a, _ = self.fresh("irpa")
        _, wt_b, _ = self.fresh("irpb")
        rec_a = runner.record_git_pointer(wt_a)
        with self.assertRaises(_ir.IssueRecoveryError) as ctx:
            _ir._git(wt_b, ["rev-parse", "HEAD"], git_bound=rec_a)
        self.assertEqual(getattr(ctx.exception, "code", None),
                         "git-pointer-changed")


class UnboundExternalTest(_AttrCase):
    def test_unbound_diff_ignores_diff_external(self):
        # No diff attribute here (it would shadow external);
        # diff.external is repo config, pre-302 it runs unbound.
        _, wt, marks = self.fresh("ext", with_diff_attr=False)
        self.assertEqual(
            _git(["-C", wt, "config", "diff.external",
                  "tee %s" % marks["ext"]],
                 wt, self.env).returncode, 0)
        out = batches._git_text(["diff"], wt)
        self.assert_no_markers(marks, "ext")
        self.assertIsNotNone(out)


class UnsupportedGitTest(_AttrCase):
    """Fail-closed when this git rejects --attr-source.

    New API (post-fix only): not part of the driver RED.
    """
    def _no_support(self):
        return mock.patch.object(runner, "attr_source_supported",
                                 return_value=False)

    def test_hgit_run_bound_raises(self):
        _, wt, _ = self.fresh("ub1")
        rec = runner.record_git_pointer(wt)
        with self._no_support():
            with self.assertRaises(runner.SourceUnreadable):
                runner._hgit_run(wt, ["diff"], git_bound=rec,
                                 timeout=30)

    def test_hgit_run_unbound_rc(self):
        _, wt, _ = self.fresh("ub2")
        with self._no_support():
            code, _, _ = runner._hgit_run(wt, ["diff"], timeout=30)
        self.assertNotEqual(code, 0)

    def test_batches_unbound_none(self):
        _, wt, _ = self.fresh("ub3")
        with self._no_support():
            self.assertIsNone(batches._git_text(["diff"], wt))

    def test_fc_unbound_not_ok(self):
        _, wt, _ = self.fresh("ub4")
        with self._no_support():
            text, ok = fastcheck._fc_run(wt, ["diff"])
        self.assertFalse(ok)

    def test_fc_bound_raises(self):
        _, wt, _ = self.fresh("ub5")
        rec = runner.record_git_pointer(wt)
        with self._no_support():
            with self.assertRaises(runner.SourceUnreadable):
                fastcheck._fc_run(wt, ["diff"], git_bound=rec)

    def test_lines_unbound_unsupported_none(self):
        """302b P4b: the _git_lines unbound pre-check fails
        closed to None (never a raise) on a git too old for
        --attr-source."""
        _, wt, _ = self.fresh("ub6")
        with self._no_support():
            self.assertIsNone(batches._git_lines(["diff"], wt))

    def test_prs_unbound_unsupported_note(self):
        """302b P5: the work_diffstat unbound pre-check fails
        closed to the failure note (never a raise) on a git too
        old for --attr-source."""
        _, wt, _ = self.fresh("ub7")
        with self._no_support():
            self.assertEqual(prs.work_diffstat(wt, ""),
                             "실행 내역 읽기 실패")

    def _hung(self):
        return mock.patch.object(
            runner, "attr_source_supported",
            side_effect=subprocess.TimeoutExpired("git", 3))

    def test_text_unbound_hung_none(self):
        """302d Q6: a hung probe fails a batches._git_text
        unbound read to None (never a raise, never cached)."""
        _, wt, _ = self.fresh("ub8")
        with self._hung():
            self.assertIsNone(batches._git_text(["diff"], wt))

    def test_lines_unbound_hung_none(self):
        """302d Q6: a hung probe fails a batches._git_lines
        unbound read to None (never a raise, never cached)."""
        _, wt, _ = self.fresh("ub9")
        with self._hung():
            self.assertIsNone(batches._git_lines(["diff"], wt))

    def test_prs_unbound_hung_note(self):
        """302d Q6: a hung probe fails work_diffstat unbound to
        the failure note (never a raise, never cached)."""
        _, wt, _ = self.fresh("ub10")
        with self._hung():
            self.assertEqual(prs.work_diffstat(wt, ""),
                             "실행 내역 읽기 실패")

    def test_fc_unbound_hung_not_ok(self):
        """302d Q6: a hung probe fails fastcheck unbound to
        (None, False) (never a raise, never cached)."""
        _, wt, _ = self.fresh("ub11")
        with self._hung():
            text, ok = fastcheck._fc_run(wt, ["diff"])
        self.assertFalse(ok)
        self.assertIsNone(text)

    def test_fc_bound_hung_raises(self):
        """302d Q7: a hung probe on a bound fastcheck read raises
        SourceUnreadable (never a silent (None, False))."""
        _, wt, _ = self.fresh("ub12")
        rec = runner.record_git_pointer(wt)
        with self._hung():
            with self.assertRaises(runner.SourceUnreadable):
                fastcheck._fc_run(wt, ["diff"], git_bound=rec)


if __name__ == "__main__":
    unittest.main()
