"""M3.1 release/rollback tests: owner self-run kit.

All offline: fixture home + fixture source root, injected suite
runners, kickstart and /api/app wait. The real FULL/Py3.9 suites run
only through `openmainty release` itself, never here. Py3.9 portable.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import gc, release  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

MINUTE = 20  # never the owner-cron window


class ReleaseFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.srcroot = self.root / "srcroot"
        (self.srcroot / "src" / "openmainty").mkdir(parents=True)
        (self.srcroot / "tests").mkdir(parents=True)
        (self.srcroot / "src" / "openmainty" / "a.py").write_text(
            "x = 1\n", encoding="utf-8")
        (self.srcroot / "tests" / "t.py").write_text(
            "y = 2\n", encoding="utf-8")
        (self.srcroot / "pyproject.toml").write_text(
            "[p]\n", encoding="utf-8")
        (self.srcroot / "src" / "openmainty" / "__pycache__").mkdir()
        (self.srcroot / "src" / "openmainty" / "__pycache__" /
         "a.pyc").write_text("junk", encoding="utf-8")
        use_temp_home(self, str(self.home))

    def green_runner(self, box, full_n=10, py39_n=5):
        def run(cmd, home, cwd):
            box.append(cmd)
            if cmd[0].endswith("python3") and "-m" in cmd:
                return 0, "Ran %d tests in 1s\nOK\n" % py39_n
            return 0, "Ran %d tests in 1s\nOK\n" % full_n
        return run

    def release_ok(self, box, **kw):
        kw.setdefault("_minute", MINUTE)
        kw.setdefault("_runner", self.green_runner(box))
        kw.setdefault("_kick", lambda: (0, "ok"))
        kw.setdefault("_wait", lambda port, sha: True)
        return release.release(home=self.home, root=self.srcroot, **kw)


class SnapshotTests(ReleaseFixture):
    def test_snapshot_covers_src_tests_pyproject(self):
        snap = release.snapshot_sources(self.srcroot)
        self.assertIn("src/openmainty/a.py", snap)
        self.assertIn("tests/t.py", snap)
        self.assertIn("pyproject.toml", snap)
        # pycache never rides along.
        self.assertFalse(any("__pycache__" in k for k in snap))

    def test_snapshot_matches_root_builder_shape(self):
        import hashlib
        snap = release.snapshot_sources(self.srcroot)
        expect = hashlib.sha256(b"x = 1\n").hexdigest()
        self.assertEqual(snap["src/openmainty/a.py"], expect)


class IdleGateTests(ReleaseFixture):
    def test_lease_held_refuses(self):
        from openmainty import sched
        holder = mock.patch.object(sched, "worker_lease_held",
                                   return_value=True)
        holder.start()
        self.addCleanup(holder.stop)
        with self.assertRaises(release.ReleaseError) as ctx:
            release.release(home=self.home, root=self.srcroot,
                            _minute=MINUTE,
                            _runner=self.green_runner([]))
        self.assertIn("잠금", str(ctx.exception))

    def test_cron_window_refuses(self):
        with self.assertRaises(release.ReleaseError) as ctx:
            release.release(home=self.home, root=self.srcroot,
                            _minute=13,
                            _runner=self.green_runner([]))
        self.assertIn(":13", str(ctx.exception))

    def test_running_op_refuses(self):
        ops = self.home / "integration_ops"
        ops.mkdir(parents=True)
        (ops / "prep-x.json").write_text(json.dumps(
            {"op_id": "prep-x", "status": "started"}),
            encoding="utf-8")
        with self.assertRaises(release.ReleaseError) as ctx:
            release.release(home=self.home, root=self.srcroot,
                            _minute=MINUTE,
                            _runner=self.green_runner([]))
        self.assertIn("prep-x", str(ctx.exception))

    def test_dry_run_writes_nothing(self):
        before = sorted(str(p) for p in self.home.rglob("*"))
        out = release.release(home=self.home, root=self.srcroot,
                              dry_run=True, _minute=MINUTE)
        self.assertTrue(out["dry_run"])
        self.assertEqual(sorted(str(p) for p in self.home.rglob("*")),
                         before)


class PipelineTests(ReleaseFixture):
    def test_red_suite_refuses_before_promote(self):
        from openmainty import promote as _promote
        calls = []
        with mock.patch.object(_promote, "promote",
                               lambda *a, **k: calls.append(1)):
            with self.assertRaises(release.ReleaseError) as ctx:
                release.release(
                    home=self.home, root=self.srcroot,
                    _minute=MINUTE,
                    _runner=lambda cmd, home, cwd: (1, "FAILED\n"))
        self.assertIn("FULL 실패", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_mid_run_source_change_refuses(self):
        from openmainty import promote as _promote
        calls = []

        def run(cmd, home, cwd):
            (self.srcroot / "src" / "openmainty" / "b.py").write_text(
                "new\n", encoding="utf-8")
            return 0, "Ran 3 tests in 1s\nOK\n"

        with mock.patch.object(_promote, "promote",
                               lambda *a, **k: calls.append(1)):
            with self.assertRaises(release.ReleaseError) as ctx:
                release.release(home=self.home, root=self.srcroot,
                                _minute=MINUTE, _runner=run,
                                _py39_path="/nonexistent/python3")
        self.assertIn("원본 변경", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_py39_required_when_present(self):
        box = []
        fake_py = self.root / "python3"
        fake_py.write_text("#!/bin/sh\n", encoding="utf-8")
        out = self.release_ok(box, _py39_path=str(fake_py))
        cmds = [" ".join(c) for c in box]
        self.assertEqual(len(box), 2)
        self.assertTrue(any("discover" in c for c in cmds))
        self.assertIn("tests.test_m2", cmds[1])

    def test_py39_skipped_plainly_when_absent(self):
        box = []
        out = release.release(
            home=self.home, root=self.srcroot, _minute=MINUTE,
            _runner=self.green_runner(box),
            _kick=lambda: (0, "ok"),
            _wait=lambda port, sha: True,
            _py39_path="/nonexistent/python3")
        self.assertIn("건너뜀", out["py39_skipped"])
        self.assertEqual(len(box), 1)

    def test_suite_tmp_home_cleaned_on_success(self):
        import glob
        import tempfile
        before = set(glob.glob(tempfile.gettempdir()
                               + "/openmainty-release-*"))
        box = []
        self.release_ok(box)
        after = set(glob.glob(tempfile.gettempdir()
                              + "/openmainty-release-*"))
        self.assertEqual(after - before, set())

    def test_busy_at_promote_refuses_without_promoting(self):
        from openmainty import promote as _promote
        from openmainty import sched
        held = {"n": 0}

        def lease():
            held["n"] += 1
            return held["n"] > 1  # start gate passes, then busy

        sleeps = []
        with mock.patch.object(sched, "worker_lease_held",
                               side_effect=lease):
            with mock.patch.object(_promote, "promote") as promo:
                with self.assertRaises(release.ReleaseError) as ctx:
                    release.release(
                        home=self.home, root=self.srcroot,
                        _minute=MINUTE,
                        _runner=self.green_runner([]),
                        _kick=lambda: (0, "ok"),
                        _wait=lambda port, sha: True,
                        _sleep=lambda s: sleeps.append(s),
                        _py39_path="/nonexistent/python3")
        self.assertIn("검사는 통과 — 작업이 끝난 뒤 다시 실행하세요 "
                      "(승격 안 함)", str(ctx.exception))
        promo.assert_not_called()
        self.assertEqual(sleeps, [15] * 40)

    def test_becomes_idle_within_wait_proceeds(self):
        from openmainty import promote as _promote
        from openmainty import sched
        held = {"n": 0}

        def lease():
            held["n"] += 1
            # start gate idle; promote-time busy twice, then idle
            return held["n"] in (2, 3)

        sleeps = []
        with mock.patch.object(sched, "worker_lease_held",
                               side_effect=lease):
            out = self.release_ok(
                [], _sleep=lambda s: sleeps.append(s),
                _py39_path="/nonexistent/python3")
        self.assertEqual(sleeps, [15, 15])
        self.assertIn("sha", out)

    def test_cron_at_promote_time_refuses(self):
        from openmainty import promote as _promote
        minutes = iter([MINUTE] + [13] * 50)
        with mock.patch.object(_promote, "promote") as promo:
            with self.assertRaises(release.ReleaseError) as ctx:
                release.release(
                    home=self.home, root=self.srcroot,
                    _minute=lambda: next(minutes),
                    _runner=self.green_runner([]),
                    _kick=lambda: (0, "ok"),
                    _wait=lambda port, sha: True,
                    _sleep=lambda s: None,
                    _py39_path="/nonexistent/python3")
        self.assertIn("검사는 통과 — 작업이 끝난 뒤 다시 실행하세요 "
                      "(승격 안 함)", str(ctx.exception))
        self.assertIn(":13", str(ctx.exception))
        promo.assert_not_called()

    def test_kickstart_failure_reports_truth(self):
        box = []
        out = release.release(
            home=self.home, root=self.srcroot, _minute=MINUTE,
            _runner=self.green_runner(box),
            _kick=lambda: (1, "no launchd"),
            _wait=lambda port, sha: False)
        self.assertIn("새 사본으로 전환됨", out["warning"])
        self.assertIn("rollback", out["warning"])
        self.assertNotIn("유지", out.get("warning", ""))


class RollbackTests(ReleaseFixture):
    def test_needs_yes(self):
        with self.assertRaises(release.ReleaseError) as ctx:
            release.rollback(home=self.home, yes=False,
                             _minute=MINUTE)
        self.assertIn("--yes", str(ctx.exception))

    def test_no_record_refuses(self):
        with self.assertRaises(release.ReleaseError) as ctx:
            release.rollback(home=self.home, yes=True,
                             _minute=MINUTE,
                             _kick=lambda: (0, "ok"),
                             _wait=lambda port, sha: True)
        self.assertIn("롤백 거부", str(ctx.exception))

    def test_verify_mismatch_reports_warning(self):
        from openmainty import promote as _promote
        adir = self.home / "app"
        for sha in ("aaa", "bbb"):
            d = adir / sha
            d.mkdir(parents=True)
            (d / "PROMOTED.json").write_text(json.dumps({"sha": sha}),
                                             encoding="utf-8")
        with mock.patch.object(_promote, "rollback",
                               return_value={"sha": "aaa",
                                             "previous": "bbb"}):
            out = release.rollback(
                home=self.home, yes=True, _minute=MINUTE,
                _kick=lambda: (0, "ok"),
                _wait=lambda port, sha: False)
        self.assertFalse(out["verified"])
        self.assertIn("kickstart 재시도", out["warning"])


class InvariantTests(unittest.TestCase):
    def test_no_push_merge_or_comment_paths(self):
        import re
        for name in ("release.py", "gc.py"):
            src = (Path(__file__).resolve().parent.parent / "src" /
                   "openmainty" / name).read_text(encoding="utf-8")
            # Word boundaries: prose like "merged-or-abandoned" is
            # not a merge operation.
            for token in ("push", "merge", "comment", "github.com",
                          "pull request"):
                self.assertIsNone(
                    re.search(r"\b%s\b" % re.escape(token), src),
                    f"{name} must stay release-free: {token}")
            self.assertNotIn(" gh ", src)


if __name__ == "__main__":
    unittest.main()
