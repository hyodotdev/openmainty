"""Unit coverage: run worktrees never share installed deps with the checkout.

A worktree `node_modules` symlink into the original checkout lets an
install/update inside the worktree mutate the user's shared dependencies
and lets verification pass against stale packages. ensure_* helpers must
remove such escaping links (worktree-local only, never the checkout).
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import runner, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def _git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout


class WorktreeAddHookTests(unittest.TestCase):
    # M2.9b: worktree adds never run repo hooks. A post-checkout
    # hook (fires on `git worktree add`) touching a sentinel file
    # must never run.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        co = self.root / "co"
        co.mkdir()
        _git("init", "-b", "main", cwd=co)
        _git("config", "user.email", "t@t", cwd=co)
        _git("config", "user.name", "t", cwd=co)
        (co / "f.txt").write_text("v1\n", encoding="utf-8")
        _git("add", ".", cwd=co)
        _git("commit", "-qm", "base", cwd=co)
        self.co = co
        self.sentinel = self.root / "hook-ran.txt"
        hook = co / ".git" / "hooks" / "post-checkout"
        hook.write_text(
            "#!/bin/sh\n"
            f'echo hooked >> "{self.sentinel}"\n'
            "echo 'hook must never run' >&2\n"
            "exit 1\n", encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    def test_ensure_branch_workdir_skips_post_checkout(self):
        state = store.create("fakerepo", "dep-x-1", "t", "base0", {})
        work = runner.ensure_branch_workdir(
            state, self.co, "main", "codex/wt-hook-test")
        self.assertTrue(work.is_dir())
        self.assertFalse(self.sentinel.exists())
        self.assertTrue((work / "f.txt").is_file())


class IsolateNodeModulesTests(unittest.TestCase):
    def test_removes_escaping_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            co = Path(tmp) / "co"
            (co / "node_modules" / "pkg").mkdir(parents=True)
            work = Path(tmp) / "work"
            work.mkdir()
            (work / "node_modules").symlink_to(co / "node_modules",
                                              target_is_directory=True)
            note = runner._isolate_node_modules(work)
            self.assertIsNotNone(note)
            self.assertFalse((work / "node_modules").is_symlink())
            # The original checkout is untouched.
            self.assertTrue((co / "node_modules" / "pkg").is_dir())

    def test_keeps_worktree_local_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            co = Path(tmp) / "co"
            (co / "node_modules").mkdir(parents=True)
            work = Path(tmp) / "work"
            mine = work / "node_modules"
            mine.mkdir(parents=True)
            self.assertIsNone(runner._isolate_node_modules(work))
            self.assertTrue(mine.is_dir())
            self.assertFalse(mine.is_symlink())

    def test_noop_when_checkout_lacks_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            co = Path(tmp) / "co"
            co.mkdir()
            work = Path(tmp) / "work"
            work.mkdir()
            self.assertIsNone(runner._isolate_node_modules(work))
            self.assertFalse((work / "node_modules").exists())


if __name__ == "__main__":
    unittest.main()
