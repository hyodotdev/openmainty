"""Slice 301: app git on model-written M2/fastcheck/serve trees (RED first).

Real git in scratch repos under TMPDIR. Each test plants a
worker-controlled script (fsmonitor / hooks / config / diff driver)
or a rewritten .git pointer and asserts, through the flow's real
entry point, that the plant never ran and the read refused (or the
value stayed honest). Before the 301 hardening these FAIL
behaviorally (marker files exist / wrong values / proceeds);
afterwards they pass. No new product APIs are used here so RED
stays FAIL lines, never TypeError.
"""
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
from openmainty import fastcheck  # noqa: E402
from openmainty import prs  # noqa: E402
from openmainty import runner  # noqa: E402
from openmainty import serve as serve_mod  # noqa: E402


def _scratch_home(root):
    home = os.path.join(root, "home")
    os.makedirs(home, exist_ok=True)
    empty = os.path.join(home, "empty-gitconfig")
    with open(empty, "w"):
        pass
    return home, empty


def _git_env(home, empty):
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


class _GitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t301-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.empty = _scratch_home(self.tmp)
        self.env = _git_env(self.home, self.empty)
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

    def mk_repo(self, name="repo"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        proc = _git(["init", "-b", "main"], path, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(path, "a.txt"), "w") as fh:
            fh.write("one\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "add", "a.txt"], path, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qm", "init"], path, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        return path

    def mk_worktree(self, repo, name="work"):
        work = os.path.join(self.tmp, name)
        proc = _git(["worktree", "add", "--detach", work, "HEAD"],
                    repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        return work

    def mk_script(self, name="hook.sh"):
        script = os.path.join(self.tmp, name)
        marker = script + ".ran"
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\ntouch %s\n" % marker)
        os.chmod(script, 0o700)
        return script, marker

    def head(self, repo):
        proc = _git(["rev-parse", "HEAD"], repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        return proc.stdout.decode().strip()

    def plant_fsmonitor(self, work):
        script, marker = self.mk_script(
            "fsmon-%s.sh" % os.path.basename(work))
        proc = _git(["config", "core.fsmonitor", script],
                    work, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        return script, marker


class WorktreeChangedTest(_GitCase):
    def test_fsmonitor_plant_does_not_run(self):
        work = self.mk_repo()
        _, marker = self.plant_fsmonitor(work)
        self.assertFalse(runner._worktree_changed(Path(work)))
        self.assertFalse(os.path.exists(marker))


class SnapshotDiffTest(_GitCase):
    def test_fsmonitor_plant_does_not_run(self):
        repo = self.mk_repo()
        work = self.mk_worktree(repo)
        _, marker = self.plant_fsmonitor(work)
        out = runner.snapshot_diff(
            Path(work), Path(self.tmp), "fix", self.head(repo))
        self.assertFalse(os.path.exists(marker))
        self.assertTrue(out is None or isinstance(out, str))


class CandidatePathSetTest(_GitCase):
    def test_fsmonitor_plant_does_not_run(self):
        repo = self.mk_repo()
        work = self.mk_worktree(repo)
        with open(os.path.join(work, "a.txt"), "a") as fh:
            fh.write("two\n")
        _, marker = self.plant_fsmonitor(work)
        tracked, untracked = runner._candidate_path_set(
            Path(work), self.head(repo))
        self.assertFalse(os.path.exists(marker))
        self.assertIn("a.txt", tracked)


class DraftContextTest(_GitCase):
    def test_fsmonitor_plant_does_not_run(self):
        repo = self.mk_repo()
        work = self.mk_worktree(repo)
        with open(os.path.join(work, "a.txt"), "a") as fh:
            fh.write("two\n")
        _, marker = self.plant_fsmonitor(work)
        state = {"baseCommit": self.head(repo),
                 "workCommit": self.head(repo)}
        ctx = runner._draft_context(state, Path(work))
        self.assertFalse(os.path.exists(marker))
        self.assertIsInstance(ctx, str)


class WorkCommitPointerTest(_GitCase):
    def test_rewritten_pointer_refuses(self):
        repo = self.mk_repo()
        work = self.mk_worktree(repo)
        rec = runner.record_git_pointer(work)
        other = self.mk_repo("other")
        with open(os.path.join(other, "a.txt"), "w") as fh:
            fh.write("forged\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qam", "forged"], other, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % os.path.join(other, ".git"))
        state = {"workSeq": 0, "workGit": rec}
        # A rewritten pointer answering the forged repo's HEAD is
        # the 1b1aef3 vector on this site: refuse, never answer.
        with self.assertRaises(runner.SourceUnreadable):
            runner.work_commit(Path(work), state)


class HeadFileHashesPointerTest(_GitCase):
    def test_rewritten_pointer_refuses(self):
        repo = self.mk_repo()
        work = self.mk_worktree(repo)
        rec = runner.record_git_pointer(work)
        other = self.mk_repo("other2")
        with open(os.path.join(other, "a.txt"), "w") as fh:
            fh.write("forged-bytes\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qam", "forged"], other, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % os.path.join(other, ".git"))
        with self.assertRaises(runner.SourceUnreadable):
            runner._head_file_hashes(Path(work), ["a.txt"],
                                     git_bound=rec)


class CandidateAlreadyCommittedTest(_GitCase):
    def _blocked_state(self, work, base, digest):
        return {"baseCommit": base, "dep": "d", "target": "t",
                "workGit": runner.record_git_pointer(work),
                "commitBlocked": {
                    "candidate": {
                        "complete": True, "base": base,
                        "dep": "d", "target": "t",
                        "paths": ["a.txt"], "untracked": [],
                        "files": {"a.txt": digest}}}}

    def _committed_work(self, name="cac"):
        # A linked worktree with the candidate change actually
        # committed on top of base (head != base, tree clean).
        repo = self.mk_repo(name)
        base = self.head(repo)
        work = self.mk_worktree(repo, name + "work")
        with open(os.path.join(work, "a.txt"), "w") as fh:
            fh.write("two\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qam", "candidate"], work, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(work, "a.txt"), "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        return work, base, digest

    def test_fsmonitor_plant_does_not_run(self):
        work, base, digest = self._committed_work()
        state = self._blocked_state(work, base, digest)
        _, marker = self.plant_fsmonitor(work)
        self.assertTrue(
            runner._candidate_already_committed(state, Path(work)))
        self.assertFalse(os.path.exists(marker))

    def test_rewritten_pointer_stays_blocked(self):
        work, base, digest = self._committed_work("cacother")
        state = self._blocked_state(work, base, digest)
        other = self.mk_repo("cacother2")
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % os.path.join(other, ".git"))
        self.assertFalse(
            runner._candidate_already_committed(state, Path(work)))


class MaybeCommitTest(_GitCase):
    def _run_state(self, work, rec=None):
        from openmainty import store as _store
        rdir = _store.run_dir("t301-mc")
        rdir.mkdir(parents=True, exist_ok=True)
        state = {"id": "t301-mc", "branch": "b",
                 "baseCommit": self.head(work),
                 "workCommit": self.head(work),
                 "workSeq": 0, "steps": {}}
        if rec is not None:
            state["workGit"] = rec
        return state, rdir

    def test_fsmonitor_plant_does_not_run(self):
        from openmainty import store as _store
        work = self.mk_repo("mcwork")
        rec = runner.record_git_pointer(work)
        rdir = _store.run_dir("t301-mc")
        if rdir.exists():
            shutil.rmtree(rdir, ignore_errors=True)
        rdir.mkdir(parents=True, exist_ok=True)
        target = rdir / "work"
        if target.exists() or target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(work, str(target),
                        ignore=shutil.ignore_patterns(".git"))
        proc = _git(["init", "-b", "b"], str(target), self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(str(target), "a.txt"), "w") as fh:
            fh.write("one\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "add", "a.txt"], str(target), self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qm", "init"], str(target), self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(str(target), "a.txt"), "a") as fh:
            fh.write("two\n")
        rec = runner.record_git_pointer(str(target))
        _, marker = self.plant_fsmonitor(str(target))
        state = {"id": "t301-mc", "branch": "b",
                 "baseCommit": self.head(str(target)),
                 "workCommit": self.head(str(target)),
                 "workSeq": 0, "steps": {}, "workGit": rec}
        # The plant rewrites the tracked gitdir config: the commit
        # refuses before any spawn, so the monitor script never runs.
        with self.assertRaises(runner.SourceUnreadable):
            runner.maybe_commit_worktree(
                state, Path(str(target)), "fix")
        self.assertFalse(os.path.exists(marker))

    def test_missing_record_refuses(self):
        from openmainty import store as _store
        work = self.mk_repo("mcwork2")
        rdir = _store.run_dir("t301-mc2")
        if rdir.exists():
            shutil.rmtree(rdir, ignore_errors=True)
        rdir.mkdir(parents=True, exist_ok=True)
        target = rdir / "work"
        if target.exists() or target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(work, str(target),
                        ignore=shutil.ignore_patterns(".git"))
        proc = _git(["init", "-b", "b"], str(target), self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(str(target), "a.txt"), "w") as fh:
            pass
        with open(os.path.join(str(target), "a.txt"), "w") as fh:
            fh.write("one\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "add", "a.txt"], str(target), self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qm", "init"], str(target), self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(str(target), "a.txt"), "a") as fh:
            fh.write("two\n")
        state = {"id": "t301-mc2", "branch": "b",
                 "baseCommit": self.head(str(target)),
                 "workCommit": self.head(str(target)),
                 "workSeq": 0, "steps": {}}
        refused = False
        try:
            out = runner.maybe_commit_worktree(
                state, Path(str(target)), "fix")
        except runner.SourceUnreadable:
            refused = True
        else:
            refused = not out.get("committed", False)
        self.assertTrue(refused)


class ReviewScopeTest(_GitCase):
    def test_fsmonitor_plant_does_not_run(self):
        from openmainty import store as _store
        repo = self.mk_repo("scope")
        work = self.mk_worktree(repo, "scopework")
        rec = runner.record_git_pointer(work)
        rdir = _store.run_dir("t301-scope")
        rdir.mkdir(parents=True, exist_ok=True)
        _, marker = self.plant_fsmonitor(work)
        state = {"id": "t301-scope", "branch": "b",
                 "base_branch": "main", "lane": "l",
                 "baseCommit": self.head(repo),
                 "workCommit": self.head(repo),
                 "workTreeFingerprint": "fp",
                 "workGit": rec, "steps": {}, "artifacts": {}}
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            scope = runner.review_scope_artifact(
                state, Path(work))
        self.assertFalse(os.path.exists(marker))
        self.assertIsInstance(scope, dict)


class ServeSourceChangesTest(_GitCase):
    def test_fsmonitor_plant_does_not_run(self):
        from openmainty import store as _store
        repo = self.mk_repo("serve")
        rdir = _store.run_dir("t301-serve")
        if rdir.exists():
            shutil.rmtree(rdir, ignore_errors=True)
        rdir.mkdir(parents=True, exist_ok=True)
        target = rdir / "work"
        proc = _git(["worktree", "add", "--detach", str(target),
                     "HEAD"], repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        rec = runner.record_git_pointer(str(target))
        _, marker = self.plant_fsmonitor(str(target))
        state = {"id": "t301-serve", "branch": "b",
                 "base_branch": "main", "lane": "l",
                 "baseCommit": self.head(repo),
                 "workCommit": self.head(repo), "workGit": rec,
                 "steps": {"fix": {"status": "pending"}}}
        payload = serve_mod._source_changes(state)
        self.assertFalse(os.path.exists(marker))
        self.assertIsInstance(payload, dict)

    def test_live_file_patch_fsmonitor_does_not_run(self):
        repo = self.mk_repo("servepatch")
        work = self.mk_worktree(repo, "servepatchwork")
        with open(os.path.join(work, "a.txt"), "a") as fh:
            fh.write("two\n")
        _, marker = self.plant_fsmonitor(work)
        out = serve_mod._live_file_patch(
            Path(work), self.head(repo), "a.txt", set())
        self.assertFalse(os.path.exists(marker))
        self.assertIsInstance(out, dict)


class PrsDiffstatTest(_GitCase):
    def test_fsmonitor_plant_does_not_run(self):
        repo = self.mk_repo("prward")
        work = self.mk_worktree(repo, "prwardwork")
        with open(os.path.join(work, "a.txt"), "a") as fh:
            fh.write("two\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qam", "change"], work, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        _, marker = self.plant_fsmonitor(work)
        out = prs.work_diffstat(work, self.head(repo))
        self.assertFalse(os.path.exists(marker))
        self.assertIsInstance(out, str)


class FastcheckTrackedTest(_GitCase):
    def test_rewritten_pointer_refuses(self):
        repo = self.mk_repo("fc")
        work = self.mk_worktree(repo, "fcwork")
        rec = runner.record_git_pointer(work)
        other = self.mk_repo("fcother")
        with open(os.path.join(other, "evil.txt"), "w") as fh:
            fh.write("planted\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "add", "evil.txt"], other, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qam", "evil"], other, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % os.path.join(other, ".git"))
        # A rewritten pointer serving the forged repo's file list
        # is the 1b1aef3 vector on this site: refuse, never answer.
        self.assertIsNone(fastcheck._git_tracked_files(
            work, git_bound=rec))

    def test_repo_fsmonitor_does_not_run(self):
        work = self.mk_repo("fc2")
        _, marker = self.plant_fsmonitor(work)
        pins = fastcheck._podfile_pins_by_file(work)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(pins, [])


class FastcheckAllowlistTest(_GitCase):
    def test_gitdir_not_writable(self):
        repo = self.mk_repo("fcallow")
        work = self.mk_worktree(repo, "fcallowwork")
        rules, note, paths = fastcheck._write_allowlist(
            work, {"HOME": self.home, "TMPDIR": self.tmp})
        text = "".join(rules)
        _, gitdir = fastcheck._git_dirs(work)
        self.assertTrue(gitdir)
        self.assertNotIn(gitdir, paths)
        self.assertNotIn(gitdir, text)


class BatchesLinesTest(_GitCase):
    def test_fsmonitor_plant_does_not_run(self):
        work = self.mk_repo("bl")
        _, marker = self.plant_fsmonitor(work)
        lines = batches._git_lines(
            ["status", "--porcelain=v1",
             "--untracked-files=all"], work)
        self.assertFalse(os.path.exists(marker))
        self.assertIsInstance(lines, list)

    def test_read_wt_cand_fsmonitor_does_not_run(self):
        work = self.mk_repo("blc")
        _, marker = self.plant_fsmonitor(work)
        snap, diag = batches._read_wt_cand(work, "wt")
        self.assertFalse(os.path.exists(marker))
        self.assertIsInstance(diag, dict)

    def test_rewritten_pointer_refuses(self):
        repo = self.mk_repo("blp")
        work = self.mk_worktree(repo, "blpwork")
        rec = runner.record_git_pointer(work)
        other = self.mk_repo("blpother")
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % os.path.join(other, ".git"))
        self.assertIsNone(batches._git_lines(
            ["status", "--porcelain=v1",
             "--untracked-files=all"], work, git_bound=rec))
        self.assertIsNone(batches._git_text(
            ["rev-parse", "HEAD"], work, git_bound=rec))
        snap, diag = batches._read_wt_cand(
            work, "wt", git_bound=rec)
        self.assertIsNone(snap)
        self.assertFalse(diag.get("status_ok", True))
        self.assertIsNone(batches._snapshot(work, git_bound=rec))


class RunnerPointerRefusalTest(_GitCase):
    def _rewrite(self, repo, name):
        # Linked worktree (.git is a file) + prep record, then a
        # rewritten pointer at a forged repo.
        work = self.mk_worktree(repo, name)
        rec = runner.record_git_pointer(work)
        other = self.mk_repo(name + "-other")
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % os.path.join(other, ".git"))
        return work, rec

    def test_worktree_changed_refuses(self):
        repo = self.mk_repo("rwc")
        work, rec = self._rewrite(repo, "rwcwork")
        with self.assertRaises(runner.SourceUnreadable):
            runner._worktree_changed(Path(work), git_bound=rec)

    def test_snapshot_diff_refuses(self):
        repo = self.mk_repo("rws")
        work, rec = self._rewrite(repo, "rwswork")
        self.assertIsNone(runner.snapshot_diff(
            Path(work), Path(self.tmp), "fix",
            self.head(repo), git_bound=rec))

    def test_candidate_path_set_refuses(self):
        repo = self.mk_repo("rwps")
        work, rec = self._rewrite(repo, "rwpswork")
        with self.assertRaises(runner.SourceUnreadable):
            runner._candidate_path_set(
                Path(work), self.head(repo), git_bound=rec)

    def test_review_scope_refuses(self):
        from openmainty import store as _store
        repo = self.mk_repo("rwr")
        work, rec = self._rewrite(repo, "rwrwork")
        rdir = _store.run_dir("t301-rwr")
        rdir.mkdir(parents=True, exist_ok=True)
        state = {"id": "t301-rwr", "branch": "b",
                 "base_branch": "main", "lane": "l",
                 "baseCommit": self.head(repo),
                 "workCommit": self.head(repo),
                 "workTreeFingerprint": "fp",
                 "workGit": rec, "steps": {}, "artifacts": {}}
        with self.assertRaises(runner.SourceUnreadable):
            runner.review_scope_artifact(state, Path(work))

    def test_hooks_plant_does_not_run_commit(self):
        # Plain repo (.git is a dir): a hostile pre-commit hook plus
        # a hooksPath config recorded as-is, then the commit must
        # still succeed with the hook suppressed by the hardened
        # -c override.
        from openmainty import store as _store
        rdir = _store.run_dir("t301-hook")
        if rdir.exists():
            shutil.rmtree(rdir, ignore_errors=True)
        rdir.mkdir(parents=True, exist_ok=True)
        target = rdir / "work"
        target.mkdir(parents=True, exist_ok=True)
        proc = _git(["init", "-b", "b"], str(target), self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(str(target), "a.txt"), "w") as fh:
            fh.write("one\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "add", "a.txt"], str(target), self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qm", "init"], str(target), self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(str(target), "a.txt"), "a") as fh:
            fh.write("two\n")
        hook = os.path.join(str(target), ".git", "hooks",
                            "pre-commit")
        marker = hook + ".ran"
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\ntouch %s\n" % marker)
        os.chmod(hook, 0o700)
        with open(os.path.join(str(target), ".git", "config"),
                   "a") as fh:
            fh.write("\n[core]\n\thooksPath = %s\n" % os.path.join(
                str(target), ".git", "hooks"))
        hook_rec = runner.record_git_pointer(str(target))
        state = {"id": "t301-hook", "branch": "b",
                 "baseCommit": self.head(str(target)),
                 "workCommit": self.head(str(target)),
                 "workSeq": 0, "steps": {}, "workGit": hook_rec}
        out = runner.maybe_commit_worktree(
            state, Path(str(target)), "fix")
        self.assertFalse(os.path.exists(marker))
        self.assertTrue(out.get("committed"))

    def test_diff_driver_plant_refuses_before_spawn(self):
        # Plain repo (.git is a dir): a planted diff driver rewrites
        # the tracked gitdir config, so the scope refuses before any
        # spawn and the driver never runs.
        work = self.mk_repo("rwdrv")
        with open(os.path.join(work, "a.txt"), "a") as fh:
            fh.write("two\n")
        rec = runner.record_git_pointer(work)
        script, marker = self.mk_script("drv-plant.sh")
        with open(os.path.join(work, ".git", "config"), "a") as fh:
            fh.write("\n[diff \"evil\"]\n\tcommand = %s\n"
                     % script)
        with open(os.path.join(work, ".gitattributes"), "w") as fh:
            fh.write("*.txt diff=evil\n")
        from openmainty import store as _store
        rdir = _store.run_dir("t301-drv")
        rdir.mkdir(parents=True, exist_ok=True)
        state = {"id": "t301-drv", "branch": "b",
                 "base_branch": "main", "lane": "l",
                 "baseCommit": self.head(work),
                 "workCommit": self.head(work),
                 "workTreeFingerprint": "fp",
                 "workGit": rec, "steps": {}, "artifacts": {}}
        # The planted driver config changes the recorded gitdir
        # hash: the scope refuses before any spawn, so the driver
        # script never runs.
        with self.assertRaises(runner.SourceUnreadable):
            runner.review_scope_artifact(state, Path(work))
        self.assertFalse(os.path.exists(marker))


class ServePointerRefusalTest(_GitCase):
    def test_live_views_refuse_rewritten_pointer(self):
        from openmainty import store as _store
        repo = self.mk_repo("srvp")
        rdir = _store.run_dir("t301-srvp")
        if rdir.exists():
            shutil.rmtree(rdir, ignore_errors=True)
        rdir.mkdir(parents=True, exist_ok=True)
        target = rdir / "work"
        proc = _git(["worktree", "add", "--detach", str(target),
                     "HEAD"], repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        rec = runner.record_git_pointer(str(target))
        with open(os.path.join(str(target), "a.txt"), "a") as fh:
            fh.write("two\n")
        other = self.mk_repo("srvpother")
        with open(os.path.join(str(target), ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % os.path.join(other, ".git"))
        code, out, _ = serve_mod._git_text(
            target, ["rev-parse", "HEAD"], git_bound=rec)
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        patch = serve_mod._live_file_patch(
            target, self.head(repo), "a.txt", set(), git_bound=rec)
        self.assertEqual(patch.get("diffCommitted"), "")
        self.assertEqual(patch.get("diffUnstaged"), "")
        state = {"id": "t301-srvp", "branch": "b",
                 "base_branch": "main", "lane": "l",
                 "baseCommit": self.head(repo),
                 "workCommit": self.head(repo), "workGit": rec,
                 "steps": {"fix": {"status": "done"}}}
        payload = serve_mod._source_changes(state)
        self.assertNotEqual(payload.get("availability"), "live")

    def test_missing_record_fails_closed(self):
        from openmainty import store as _store
        repo = self.mk_repo("srvn")
        rdir = _store.run_dir("t301-srvn")
        if rdir.exists():
            shutil.rmtree(rdir, ignore_errors=True)
        rdir.mkdir(parents=True, exist_ok=True)
        target = rdir / "work"
        proc = _git(["worktree", "add", "--detach", str(target),
                     "HEAD"], repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(str(target), "a.txt"), "a") as fh:
            fh.write("two\n")
        state = {"id": "t301-srvn", "branch": "b",
                 "base_branch": "main", "lane": "l",
                 "baseCommit": self.head(repo),
                 "workCommit": self.head(repo),
                 "steps": {"fix": {"status": "done"}}}
        payload = serve_mod._source_changes(state)
        self.assertNotEqual(payload.get("availability"), "live")


class PrsPointerRefusalTest(_GitCase):
    def test_rewritten_pointer_reads_failure(self):
        repo = self.mk_repo("prp")
        work = self.mk_worktree(repo, "prpwork")
        with open(os.path.join(work, "a.txt"), "a") as fh:
            fh.write("two\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qam", "change"], work, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        rec = runner.record_git_pointer(work)
        other = self.mk_repo("prpother")
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % os.path.join(other, ".git"))
        out = prs.work_diffstat(work, self.head(repo),
                                git_bound=rec)
        self.assertEqual(out, "실행 내역 읽기 실패")


class FastcheckPointerRefusalTest(_GitCase):
    def test_pod_pins_and_dirs_refuse(self):
        repo = self.mk_repo("fcp")
        work = self.mk_worktree(repo, "fcpwork")
        rec = runner.record_git_pointer(work)
        other = self.mk_repo("fcpother")
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % os.path.join(other, ".git"))
        self.assertEqual(fastcheck._podfile_pins_by_file(
            work, git_bound=rec), [])
        self.assertEqual(fastcheck._git_dirs(
            work, git_bound=rec), ("", ""))
        self.assertEqual(
            fastcheck.tree_identity(work, git_bound=rec), ("", ""))


if __name__ == "__main__":
    unittest.main()
