"""Request-path git timeout is bounded and fail-closed (claude225).

A wedged worktree must fail a read-only git probe fast (code 124, inside
_REQUEST_GIT_TIMEOUT_S plus reap margin) and the source-changes result must
carry the fail-closed UNKNOWN marker with every stable key intact — never a
clean bill or "no changes".

No real repos, no network, no git in ~/.openmainty or any owner worktree:
every git argv here resolves to a fake `sleep` executable placed first on
PATH inside a temp dir.
"""

import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

sys.path.insert(0, str(ROOT / "src"))

from openmainty import serve, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

# Stable keys _source_changes promises (docstring, never removed).
STABLE_KEYS = [
    "baseRef", "baseCommit", "head", "liveHead", "headMatch", "branch",
    "lane", "fileDiffs", "filesTotal", "filesSource", "untracked",
    "untrackedContents", "untrackedCapped", "untrackedSkipped",
    "dirtyStatus", "diff", "diffCommitted", "diffStaged", "diffUnstaged",
    "diffCapped", "hasChanges", "planned", "localOnly", "note",
    "availability", "historical", "worktreeAvailable", "patchSource",
    "error",
]


class TimeoutGitMixin(unittest.TestCase):
    """Fake git-first PATH plus an isolated home; restores everything."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        fake = bindir / "git"
        # Hangs longer than probe timeout + reap so the probe must time
        # out (124) instead of succeeding. Lingers boundedly; init reaps.
        fake.write_text("#!/bin/sh\nsleep 30\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        previous = os.environ.get("PATH", "")
        os.environ["PATH"] = str(bindir) + os.pathsep + previous
        self.addCleanup(self._restore_path, previous)

    def _restore_path(self, previous):
        os.environ["PATH"] = previous


class GitTextTimeoutTests(TimeoutGitMixin):
    def test_git_text_times_out_with_124(self):
        bound = serve._REQUEST_GIT_TIMEOUT_S
        self.assertLessEqual(bound, 8)
        work = Path(self.tmp.name) / "work"
        work.mkdir()
        start = time.monotonic()
        code, out, err = serve._git_text(work, ["rev-parse", "HEAD"])
        elapsed = time.monotonic() - start
        self.assertEqual(code, 124)
        # Bound plus reap margin: run_cmd waits out the probe timeout, then
        # up to 10 s reaping the hung child. Never the 600 s default.
        self.assertLess(elapsed, bound + 10 + 7)

    def test_source_changes_timeout_is_unknown_not_clean(self):
        run_id = "run-timeout-probe"
        rdir = store.run_dir(run_id)
        work = rdir / "work"
        (work / ".git").mkdir(parents=True)
        state = {
            "id": run_id,
            "baseCommit": "abc123",
            "workCommit": "abc123",
            "steps": {"prepare": {"status": "done"}},
        }
        start = time.monotonic()
        sc = serve._source_changes(state)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, serve._REQUEST_GIT_TIMEOUT_S + 10 + 7)
        # Fail-closed unknown marker, not clean, not "no changes".
        self.assertEqual(sc.get("availability"), "unknown")
        self.assertIsNone(sc.get("headMatch"))
        self.assertIsNot(sc.get("hasChanges"), False)
        self.assertTrue(sc.get("hasChanges"))
        self.assertIn("제때 응답하지 않아", sc.get("note", ""))
        # Every stable key intact.
        for key in STABLE_KEYS:
            self.assertIn(key, sc, key)


class TimeoutUiLineTests(unittest.TestCase):
    def test_unknown_availability_line_is_static(self):
        # Static presence check (not a render proof): the work view shows
        # the timeout state as one honest line, never as success.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('avail === "unknown"', html)
        self.assertIn("esc(sc.note", html)


if __name__ == "__main__":
    unittest.main()
