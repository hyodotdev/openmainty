"""Slice 299d: mutation-covering tests for the 299b git binding.

Root's mutation run (om364, 13 survivors) showed protections that
could vanish with every test still green. Each test below names the
mutation it kills. Real git in scratch repos under TMPDIR; the only
mocks are the worker-transport seam (no model turn) and os.environ
for m26. No refusal behaviour changes in this slice.
"""
import copy
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import batches  # noqa: E402
from openmainty import derived_fix as df  # noqa: E402
from openmainty import issue_recovery as ir  # noqa: E402
from openmainty import m2  # noqa: E402
from openmainty import ncontain  # noqa: E402
from openmainty import runner  # noqa: E402
from openmainty.issue_recovery import IssueRecoveryError  # noqa: E402
from openmainty.ncontain import ContainmentError  # noqa: E402
from test_derived_fix import (  # noqa: E402
    DerivedFixFixtureMixin, _proposal, _worker_jsonl)
from test_integration_prepare_issue import git  # noqa: E402


def _scratch_env(root):
    home = os.path.join(root, "home")
    os.makedirs(home, exist_ok=True)
    empty = os.path.join(home, "empty-gitconfig")
    with open(empty, "w"):
        pass
    env = dict(os.environ)
    env["HOME"] = home
    env["GIT_CONFIG_GLOBAL"] = empty
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _git(args, cwd, env):
    return subprocess.run(
        ["git"] + list(args), cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60)


class _LinkedCase(unittest.TestCase):
    """Scratch repo plus a real linked worktree (the prod shape)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t299c-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = _scratch_env(self.tmp)
        self.origin = os.path.join(self.tmp, "origin")
        os.makedirs(self.origin)
        proc = _git(["init", "-b", "main"], self.origin,
                    self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(self.origin, "a.txt"), "w") as fh:
            fh.write("one\n")
        for args in (["-c", "user.name=t", "-c", "user.email=t@t",
                      "add", "a.txt"],
                     ["-c", "user.name=t", "-c", "user.email=t@t",
                      "commit", "-qm", "init"]):
            proc = _git(args, self.origin, self.env)
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        self.work = os.path.join(self.tmp, "linked")
        proc = _git(["worktree", "add", "--detach", self.work,
                     "HEAD"], self.origin, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])

    def _record(self):
        return runner.record_git_pointer(self.work)

    def _plant_nested(self):
        nested = os.path.join(self.work, "sub", ".git")
        os.makedirs(nested, exist_ok=True)
        with open(os.path.join(nested, "config"), "w") as fh:
            fh.write("[core]\n\trepositoryformatversion = 0\n")


class FlowPrepareGitBlockTest(DerivedFixFixtureMixin):
    """m27: the prepare step must store a valid "git" block."""

    def test_prepare_stores_valid_git_block(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        ws = rec["workspace"]
        # Mutation m27 drops this key; the record must carry it.
        self.assertIn("git", ws)
        df._validate_git_record(ws["git"])
        work = ws["path"]
        node = os.path.join(work, ".git")
        # Production shape is a linked worktree: a pointer file.
        self.assertTrue(os.path.isfile(node))
        self.assertFalse(os.path.isdir(node))
        self.assertEqual(ws["git"]["kind"], "file")
        with open(node, "rb") as fh:
            raw = fh.read()
        self.assertEqual(ws["git"]["pointer_hex"], raw.hex())
        text = raw.decode("utf-8", "replace").strip()
        self.assertTrue(text.startswith("gitdir: "))
        target = text[len("gitdir: "):]
        self.assertEqual(ws["git"]["gitdir"],
                         os.path.realpath(target))


class FlowWorkerResultBoundTest(DerivedFixFixtureMixin):
    """m29: the post-worker step must stay bound to the record."""

    def _start_flow(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        return out["dfix"], started["attempt_id"]

    def _refused_worker_run(self, plant):
        dfix, attempt_id = self._start_flow()
        rec = self._read(dfix)
        work = rec["workspace"]["path"]
        prep_head = rec["workspace"]["prep_head"]
        plant(work)
        proposal = _proposal(
            ["example/webpack.config.js"],
            {"example/webpack.config.js": "0" * 64},
            [{"path": "example/webpack.config.js",
              "conflict_id": "example/webpack.config.js",
              "choice": "H-A"}])

        def _run(*args, **kwargs):
            return (0, _worker_jsonl(proposal), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            with m2.op_locked(self.blocked_id):
                rc = df.run_repair_worker(dfix, attempt_id)
        self.assertEqual(rc, 1)
        rec = self._read(dfix)
        attempt = next(a for a in rec["source_attempts"]
                       if a.get("id") == attempt_id)
        self.assertEqual(attempt["status"], "refused")
        self.assertIn("[git-pointer-changed]",
                      attempt.get("error") or "")
        head = git("rev-parse", "HEAD", cwd=work)
        self.assertEqual(head, prep_head)
        self.assertTrue(os.path.isdir(work))

    def test_nested_git_plant_refuses_with_code(self):
        def _plant(work):
            nested = os.path.join(work, "sub", ".git")
            os.makedirs(nested, exist_ok=True)
            with open(os.path.join(nested, "config"), "w") as fh:
                fh.write("[core]\n")

        self._refused_worker_run(_plant)

    def test_pointer_rewrite_refuses_with_code(self):
        def _plant(work):
            node = os.path.join(work, ".git")
            with open(node, "rb") as fh:
                raw = fh.read()
            text = raw.decode("utf-8", "replace").strip()
            target = text[len("gitdir: "):]
            # Same gitdir, different bytes: the unbound
            # pre-checks still pass, the bound proof refuses.
            with open(node, "wb") as fh:
                fh.write(("gitdir: " + target + "/.\n").encode())
            self.assertEqual(os.path.realpath(target + "/."),
                             os.path.realpath(target))

        self._refused_worker_run(_plant)


class SnapshotBoundTest(_LinkedCase):
    """m28: _snapshot_worktree must forward git_bound."""

    def test_planted_nested_refuses(self):
        rec = self._record()
        self._plant_nested()
        with self.assertRaises(df.DerivedFixError) as cm:
            df._snapshot_worktree(self.work, git_bound=rec)
        self.assertEqual(cm.exception.code, "unreadable")

    def test_clean_tree_passes(self):
        rec = self._record()
        snap = df._snapshot_worktree(self.work, git_bound=rec)
        self.assertTrue(snap["head"])
        self.assertTrue(snap["fingerprint"])
        self.assertTrue(snap["clean"])


class LinkedWorktreeReproveTest(_LinkedCase):
    """m07/m20/m21: the file branch must walk, compare bytes and
    compare the target."""

    def test_nested_walk_in_file_branch(self):
        rec = self._record()
        self._plant_nested()
        with self.assertRaises(runner.SourceUnreadable) as cm:
            runner.reprove_git_pointer(self.work, rec)
        self.assertEqual(cm.exception.op, "nested-git")

    def test_pointer_bytes_checked(self):
        rec = self._record()
        node = os.path.join(self.work, ".git")
        with open(node, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8", "replace").strip()
        target = text[len("gitdir: "):]
        with open(node, "wb") as fh:
            fh.write(("gitdir: " + target + "/.\n").encode())
        # Same gitdir, different bytes: m20 must still refuse.
        self.assertEqual(os.path.realpath(target + "/."),
                         os.path.realpath(target))
        with self.assertRaises(runner.SourceUnreadable) as cm:
            runner.reprove_git_pointer(self.work, rec)
        self.assertEqual(cm.exception.op, "git-pointer")

    def test_pointer_target_checked(self):
        rec = self._record()
        bad = dict(rec)
        bad["gitdir"] = "/nonexistent-299d-gitdir"
        with self.assertRaises(runner.SourceUnreadable) as cm:
            runner.reprove_git_pointer(self.work, bad)
        self.assertEqual(cm.exception.op, "git-pointer")

    def test_clean_reprove_returns_paths(self):
        rec = self._record()
        paths = runner.reprove_git_pointer(self.work, rec)
        self.assertEqual(paths["gitdir"], rec["gitdir"])
        self.assertEqual(paths["worktree"],
                         os.path.realpath(self.work))


class CallPathBoundTest(_LinkedCase):
    """m24/m31/m32/m26: every call path must honor its binding."""

    def test_runner_snapshot_ignores_nothing(self):
        rec = self._record()
        self._plant_nested()
        with self.assertRaises(runner.SourceUnreadable) as cm:
            runner.source_snapshot(self.work, git_bound=rec)
        self.assertEqual(cm.exception.op, "nested-git")

    def test_batches_lines_return_none_when_bound(self):
        rec = self._record()
        clean = batches._git_lines(
            ["status", "--porcelain=v1"], self.work,
            git_bound=rec)
        self.assertIsNotNone(clean)
        self._plant_nested()
        self.assertIsNone(batches._git_lines(
            ["status", "--porcelain=v1"], self.work,
            git_bound=rec))

    def test_issue_recovery_git_refuses_with_code(self):
        rec = self._record()
        self._plant_nested()
        with self.assertRaises(IssueRecoveryError) as cm:
            ir._git(self.work, ["rev-parse", "HEAD"],
                    git_bound=rec)
        self.assertEqual(cm.exception.code,
                         "git-pointer-changed")

    def test_unbound_env_stays_scrubbed(self):
        # m26: without the scrubbed env, a GIT_DIR in the
        # parent process poisons even an unbound call.
        with mock.patch.dict(os.environ,
                             {"GIT_DIR": "/nonexistent-299d"}):
            proc = df._git(self.origin, ["rev-parse", "HEAD"])
        self.assertEqual(proc.returncode, 0)


class WalkRuleTest(_LinkedCase):
    """m10/m09/m05: the walk fails closed and the rule rejects
    unsafe workdirs."""

    def test_unreadable_directory_refuses(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root: chmod 000 is readable")
        sub = os.path.join(self.work, "dark")
        os.makedirs(sub, exist_ok=True)
        self.addCleanup(os.chmod, sub, 0o700)
        os.chmod(sub, 0o000)
        with self.assertRaises(runner.SourceUnreadable) as cm:
            runner.refuse_nested_git(self.work)
        self.assertEqual(cm.exception.op, "nested-git")

    def test_symlink_to_outside_dir_not_walked(self):
        outside = os.path.join(self.tmp, "outside")
        dotgit = os.path.join(outside, ".git")
        os.makedirs(dotgit, exist_ok=True)
        with open(os.path.join(dotgit, "config"), "w") as fh:
            fh.write("[core]\n")
        os.symlink(outside, os.path.join(self.work, "link"))
        # Must neither walk out nor refuse: symlinks are not
        # descended, and the link itself is not named .git.
        self.assertIsNone(runner.refuse_nested_git(self.work))

    def test_symlink_named_git_refuses(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside, exist_ok=True)
        sub = os.path.join(self.work, "sub")
        os.makedirs(sub, exist_ok=True)
        os.symlink(outside, os.path.join(sub, ".git"))
        with self.assertRaises(runner.SourceUnreadable) as cm:
            runner.refuse_nested_git(self.work)
        self.assertEqual(cm.exception.op, "nested-git")

    def test_unsafe_workdir_chars_refused(self):
        for bad in ('"', "\\", "\n", "\x7f"):
            with self.subTest(char=repr(bad)):
                path = "/tmp/my work+dir-1" + bad + "x"
                with self.assertRaises(ContainmentError):
                    ncontain._repair_git_deny_line(path)

    def test_normal_workdir_accepted(self):
        line = ncontain._repair_git_deny_line(
            "/tmp/my work+dir-1")
        self.assertIn("[gG][iI][tT]", line)
        self.assertTrue(line.startswith("(deny file-write*"))


class WorkspaceGitRecordTest(DerivedFixFixtureMixin):
    """m30: _validate_workspace must validate the "git" block."""

    def _valid_case(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        return out["dfix"], copy.deepcopy(rec["workspace"])

    def _assert_corrupt(self, ws, dfix):
        with self.assertRaises(df.DerivedFixError) as cm:
            df._validate_workspace(ws, dfix)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_valid_record_passes(self):
        dfix, ws = self._valid_case()
        self.assertEqual(df._validate_workspace(ws, dfix)["git"],
                         ws["git"])

    def test_wrong_keys_refused(self):
        dfix, ws = self._valid_case()
        ws["git"]["extra"] = 1
        self._assert_corrupt(ws, dfix)

    def test_wrong_kind_refused(self):
        dfix, ws = self._valid_case()
        ws["git"]["kind"] = "link"
        self._assert_corrupt(ws, dfix)

    def test_relative_gitdir_refused(self):
        dfix, ws = self._valid_case()
        ws["git"]["gitdir"] = "rel/path"
        self._assert_corrupt(ws, dfix)

    def test_bad_hex_refused(self):
        dfix, ws = self._valid_case()
        ws["git"]["pointer_hex"] = "zz"
        self._assert_corrupt(ws, dfix)

    def test_non_list_hooks_refused(self):
        dfix, ws = self._valid_case()
        ws["git"]["hooks"] = "x"
        self._assert_corrupt(ws, dfix)

    def test_legacy_record_without_git_validates(self):
        dfix, ws = self._valid_case()
        del ws["git"]
        df._validate_workspace(ws, dfix)


class ProofDenyLastTest(unittest.TestCase):
    """m33: the proof must require the .git deny last."""

    def test_deny_last_check_reached(self):
        tmp = os.path.realpath(
            tempfile.mkdtemp(prefix="t299c-pol-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "root")
        work = os.path.join(tmp, "ws")
        os.makedirs(root, exist_ok=True)
        os.makedirs(work, exist_ok=True)
        base = ncontain.build_subscription_broker_policy_text(
            root, "/bin/true", "/Users/qa-owner", 15441)
        text = ncontain.build_repair_broker_policy_text(
            root, "/bin/true", "/Users/qa-owner", 15441, work)
        extra = ncontain._repair_extra_lines(work, root)
        # Workdir outside the root: write, read, then deny.
        self.assertEqual(len(extra), 3)
        self.assertTrue(extra[-1].startswith(
            "(deny file-write*"))
        consistent = [ln for ln in extra
                      if not ln.startswith("(deny file-write*")]
        stripped = "\n".join(
            ln for ln in text.splitlines()
            if not ln.startswith("(deny file-write*")) + "\n"
        # Two-line list, otherwise consistent: only the
        # deny-last check can refuse it.
        with self.assertRaises(ContainmentError) as cm:
            ncontain._prove_repair_plus(stripped, base,
                                        consistent)
        self.assertIn("deny must be last", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
