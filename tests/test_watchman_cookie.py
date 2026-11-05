"""Watchman sync-cookie blindness: transient cookies never dirty readiness.

Production defect (same symptom as the recurring readiness flap;
reproduced live and in aligned operational captures 13:27-13:30Z):
watchman drops `.watchman-cookie-<host>-<pid>-<n>` files in the ROOT of
linked worktrees (which carry a .git FILE) on every sync sweep (~60 s
cadence, ~20 ms life). They are untracked and never git-ignored, so a
sweep-aligned `git status` read made ``batches._source_clean`` False
("has uncommitted changes") and a sweep-aligned
``runner.source_snapshot`` hashed them into the fingerprint
("fingerprint changed") — each recovering on the next poll.

These tests use REAL temp git repos with `git worktree add` (linked
worktrees with a .git FILE, the production shape) and assert the shared
``runner._noise_rel`` rule plus every consumer agrees: cookies are
invisible, real untracked source still counts.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, runner  # noqa: E402

COOKIE = ".watchman-cookie-CPK-Mac-mini-3.local-17185-2268"


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout


def make_repo_with_linked_worktree(base: Path) -> Path:
    """origin repo + one linked worktree; returns the WORKTREE path."""
    origin = base / "origin"
    origin.mkdir(parents=True)
    git("init", "-b", "main", cwd=origin)
    git("config", "user.email", "test@localhost", cwd=origin)
    git("config", "user.name", "test", cwd=origin)
    (origin / "a.txt").write_text("before\n", encoding="utf-8")
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "init", cwd=origin)
    wt = base / "wt"
    git("worktree", "add", "-b", "wt-main", str(wt), "main", cwd=origin)
    assert (wt / ".git").is_file(), "linked worktree must carry a .git FILE"
    return wt


class WatchmanCookieTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = make_repo_with_linked_worktree(Path(self.tmp.name))

    def test_noise_rel_names_cookie_at_any_depth(self):
        self.assertTrue(runner._noise_rel(COOKIE))
        self.assertTrue(runner._noise_rel("sub/dir/" + COOKIE))
        self.assertTrue(runner._noise_rel(".watchman-cookie"))
        self.assertFalse(runner._noise_rel("src/new.js"))
        self.assertFalse(runner._noise_rel(".watchman-config"))
        self.assertFalse(runner._noise_rel("watchman-cookie-x"))

    def test_cookie_in_root_keeps_clean_and_fingerprint(self):
        before = runner.source_snapshot(self.wt)
        (self.wt / COOKIE).write_text("cookie\n", encoding="utf-8")
        try:
            self.assertTrue(batches._source_clean(str(self.wt)))
            after = runner.source_snapshot(self.wt)
        finally:
            (self.wt / COOKIE).unlink()
        self.assertEqual(after["fingerprint"], before["fingerprint"])
        self.assertEqual(after["head"], before["head"])
        self.assertNotIn(COOKIE, after.get("untracked") or [])

    def test_real_untracked_source_still_counts(self):
        before = runner.source_snapshot(self.wt)
        src = self.wt / "src"
        src.mkdir()
        (src / "new.js").write_text("real\n", encoding="utf-8")
        self.assertFalse(batches._source_clean(str(self.wt)))
        after = runner.source_snapshot(self.wt)
        self.assertNotEqual(after["fingerprint"], before["fingerprint"])
        self.assertIn("src/new.js", after.get("untracked") or [])

    def test_listed_cookie_never_stat_or_read(self):
        """The real race: the cookie IS listed by git status, yet the
        snapshot must skip it before stat/read (it may vanish at any
        microsecond). Any stat/read of a cookie path fails the test."""
        (self.wt / COOKIE).write_text("cookie\n", encoding="utf-8")
        try:
            status = git("status", "--porcelain=v1",
                         "--untracked-files=all", cwd=self.wt)
            self.assertIn(COOKIE, status)
            expected = runner.source_snapshot(self.wt)["fingerprint"]
            real_classify = runner._classify
            real_read_bytes = Path.read_bytes

            def no_cookie_stat(p, rel):
                if ".watchman-cookie" in str(rel or ""):
                    raise AssertionError(
                        f"cookie must never be stat-ed: {rel}")
                return real_classify(p, rel)

            def no_cookie_read(self):
                if ".watchman-cookie" in self.name:
                    raise AssertionError(
                        f"cookie bytes must never be read: {self.name}")
                return real_read_bytes(self)

            with mock.patch.object(runner, "_classify",
                                   side_effect=no_cookie_stat):
                with mock.patch.object(Path, "read_bytes",
                                       no_cookie_read):
                    snap = runner.source_snapshot(self.wt)
            self.assertEqual(snap["fingerprint"], expected)
            self.assertNotIn(COOKIE, snap.get("untracked") or [])
        finally:
            cookie = self.wt / COOKIE
            if cookie.exists():
                cookie.unlink()

    def test_vanished_real_file_still_raises(self):
        """Contrast: a REAL untracked file that vanishes between listing
        and read stays fail-closed (SourceUnreadable), never silent."""
        target = self.wt / "real-vanish.txt"
        target.write_text("real\n", encoding="utf-8")
        real_read_bytes = Path.read_bytes

        def vanish_then_read(self):
            if self.name == "real-vanish.txt":
                raise FileNotFoundError("injected vanishing read")
            return real_read_bytes(self)

        with mock.patch.object(Path, "read_bytes", vanish_then_read):
            with self.assertRaises(runner.SourceUnreadable):
                runner.source_snapshot(self.wt)
        target.unlink()

    def test_nested_cookie_ignored_nested_source_counts(self):
        nested = self.wt / "sub" / "dir"
        nested.mkdir(parents=True)
        (nested / COOKIE).write_text("cookie\n", encoding="utf-8")
        self.assertTrue(batches._source_clean(str(self.wt)))
        (nested / "real.js").write_text("real\n", encoding="utf-8")
        self.assertFalse(batches._source_clean(str(self.wt)))

    def test_worktree_changed_ignores_cookie_sees_real_edit(self):
        (self.wt / COOKIE).write_text("cookie\n", encoding="utf-8")
        try:
            self.assertFalse(runner._worktree_changed(self.wt))
        finally:
            (self.wt / COOKIE).unlink()
        with open(self.wt / "a.txt", "a", encoding="utf-8") as fh:
            fh.write("edit\n")
        self.assertTrue(runner._worktree_changed(self.wt))

    def test_worktree_changed_tracked_dist_edit_still_counts(self):
        """_worktree_changed uses only the legacy prefix screen plus the
        cookie predicate: a TRACKED edit under a noise-named dir (dist/)
        is still a real change for the `git add -u` work commit."""
        origin = self.wt.resolve().parent / "origin"
        dist = origin / "dist"
        dist.mkdir(exist_ok=True)
        (dist / "x.js").write_text("built\n", encoding="utf-8")
        git("add", "-A", cwd=origin)
        git("commit", "-qm", "add dist", cwd=origin)
        # Linked worktree shares origin's refs: fast-forward it onto the
        # moved main tip, then edit the TRACKED dist file.
        git("reset", "--hard", "main", cwd=self.wt)
        with open(self.wt / "dist" / "x.js", "a",
                  encoding="utf-8") as fh:
            fh.write("edit\n")
        self.assertTrue(runner._worktree_changed(self.wt))


if __name__ == "__main__":
    unittest.main()
