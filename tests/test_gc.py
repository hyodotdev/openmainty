"""S14 gc --dry-run tests: report only, delete nothing.

Offline fixture home. Every reclaimable entry is regenerable; receipts,
logs, diffs, ops, batches and events are never listed. Py3.9 portable.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import gc  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


class GcFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / "integration_ops").mkdir(parents=True)
        (self.home / "reviews").mkdir(parents=True)
        (self.home / "runs").mkdir(parents=True)
        use_temp_home(self, str(self.home))
        self.tmproot = self.root / "tmp"
        self.tmproot.mkdir()

    def old(self, path, days=10):
        past = time.time() - days * 86400
        os.utime(str(path), (past, past))

    def write_op(self, name, rec):
        p = self.home / "integration_ops" / (name + ".json")
        p.write_text(json.dumps(rec), encoding="utf-8")
        return p

    def make_repo(self):
        co = self.root / "co"
        co.mkdir()
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
        for args in (["git", "init", "-q", "."],
                     ["git", "config", "user.email", "t@t"],
                     ["git", "config", "user.name", "t"],
                     ["git", "config", "commit.gpgsign", "false"],
                     ["git", "commit", "-q", "--allow-empty",
                      "-m", "init"]):
            proc = subprocess.run(args, cwd=str(co), env=env,
                                  capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, args)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(co),
                              capture_output=True, text=True,
                              timeout=60, env=env).stdout.strip()
        return co, head, env

    def paths(self, report):
        return [i["path"] for i in report["reclaimable"]]


class DryRunTests(GcFixture):
    def test_deletes_nothing_and_never_lists_evidence(self):
        nm = self.home / "runs" / "r1" / "work" / "node_modules"
        nm.mkdir(parents=True)
        (nm / "pkg.js").write_text("x" * 1000, encoding="utf-8")
        before = sorted(str(p) for p in self.home.rglob("*"))
        report = gc.dry_run_report(self.home, tmp_root=self.tmproot)
        after = sorted(str(p) for p in self.home.rglob("*"))
        self.assertEqual(before, after)
        self.assertTrue(report["deletes_nothing"])
        self.assertIn(str(nm), self.paths(report))
        for item in (report["reclaimable"] + report["needs_review"]):
            self.assertFalse(item["path"].endswith(".log"))
            self.assertNotIn("integration_ops", item["path"])
            self.assertNotIn("/batches", item["path"])

    def test_stale_worktree_at_base_is_reclaimable(self):
        if not HAVE_GIT:
            self.skipTest("needs git")
        co, head, env = self.make_repo()
        wt = self.home / "reviews" / "w1"
        wt.mkdir()
        (wt / "yarn.lock").write_text("y" * 500, encoding="utf-8")
        subprocess.run(["git", "branch", "codex/w1", head], cwd=str(co),
                       check=True, timeout=60, env=env)
        self.write_op("prep-x", {
            "op_id": "prep-x", "status": "blocked",
            "result": {"path": str(wt), "branch": "codex/w1",
                       "head": "", "fingerprint": "f"},
            "base": {"checkout": str(co), "head": head}})
        self.old(wt)
        report = gc.dry_run_report(self.home, tmp_root=self.tmproot)
        got = [i for i in report["reclaimable"]
               if i["path"] == str(wt)]
        self.assertEqual(len(got), 1)
        self.assertIn("자체 커밋 없음", got[0]["note"])
        # The branch is still there: dry-run never deletes.
        out = subprocess.run(["git", "branch", "--list", "codex/w1"],
                             cwd=str(co), capture_output=True,
                             text=True, timeout=60).stdout.strip()
        self.assertTrue(out)

    def test_stale_worktree_with_own_commits_needs_review(self):
        if not HAVE_GIT:
            self.skipTest("needs git")
        co, head, env = self.make_repo()
        wt = self.home / "reviews" / "w2"
        wt.mkdir()
        subprocess.run(["git", "checkout", "-qb", "codex/w2", head],
                       cwd=str(co), check=True, timeout=60, env=env)
        (co / "g.txt").write_text("mine\n", encoding="utf-8")
        subprocess.run(["git", "add", "g.txt"], cwd=str(co),
                       check=True, timeout=60, env=env)
        subprocess.run(["git", "commit", "-qm", "own"], cwd=str(co),
                       check=True, timeout=60, env=env)
        self.write_op("prep-y", {
            "op_id": "prep-y", "status": "done",
            "result": {"path": str(wt), "branch": "codex/w2",
                       "head": "h", "fingerprint": "f"},
            "base": {"checkout": str(co), "head": head}})
        self.old(wt)
        report = gc.dry_run_report(self.home, tmp_root=self.tmproot)
        self.assertNotIn(str(wt), self.paths(report))
        rev = [i for i in report["needs_review"]
               if i["path"] == str(wt)]
        self.assertEqual(len(rev), 1)
        self.assertIn("자체 커밋", rev[0]["note"])

    def test_young_worktree_ignored(self):
        wt = self.home / "reviews" / "w3"
        wt.mkdir()
        self.write_op("prep-z", {
            "op_id": "prep-z", "status": "done",
            "result": {"path": str(wt), "branch": "codex/w3",
                       "head": "h", "fingerprint": "f"},
            "base": {"checkout": "/nonexistent", "head": "h"}})
        report = gc.dry_run_report(self.home, older_than_days=7,
                                   tmp_root=self.tmproot)
        self.assertNotIn(str(wt), self.paths(report))
        self.assertFalse([i for i in report["needs_review"]
                          if i["path"] == str(wt)])

    def test_old_apps_keep_newest_three_plus_current(self):
        app = self.home / "app"
        app.mkdir()
        for sha in ("old1", "old2", "new1", "new2"):
            (app / sha).mkdir()
            ((app / sha) / "f").write_text("z", encoding="utf-8")
        (app / "xxx").mkdir()
        (app / "current").symlink_to(app / "new2")
        hist = [{"event": "promote", "to": s}
                for s in ("old1", "old2", "new1", "new2")]
        (app / "history.jsonl").write_text(
            "\n".join(json.dumps(e) for e in hist), encoding="utf-8")
        report = gc.dry_run_report(self.home, tmp_root=self.tmproot)
        got = [i for i in report["reclaimable"]
               if i["category"] == "old_app"]
        self.assertEqual([i["path"] for i in got],
                         [str(app / "old1")])
        rev = [i for i in report["needs_review"]
               if i["category"] == "old_app"]
        self.assertEqual([i["path"] for i in rev], [str(app / "xxx")])

    def test_caches_and_tmp_scratch_listed(self):
        cache = self.home / "cache" / "yarn"
        cache.mkdir(parents=True)
        (cache / "blob").write_text("c" * 2000, encoding="utf-8")
        owned = self.tmproot / "openmainty-probe-1"
        owned.mkdir()
        (owned / "x").write_text("t", encoding="utf-8")
        (self.tmproot / "random-tmp-1").mkdir()
        report = gc.dry_run_report(self.home, tmp_root=self.tmproot)
        cats = {i["path"]: i["category"] for i in report["reclaimable"]}
        self.assertEqual(cats.get(str(cache)), "cache")
        self.assertEqual(cats.get(str(owned)), "tmp_scratch")
        self.assertNotIn(str(self.tmproot / "random-tmp-1"),
                         self.paths(report))

    def test_pub_cache_listed(self):
        # M3.3a: the fastcheck PUB_CACHE lives under the app-owned
        # cache root, so gc reports it as regenerable cache weight.
        cache = self.home / "cache" / "pub"
        cache.mkdir(parents=True)
        (cache / "blob").write_text("c" * 2000, encoding="utf-8")
        report = gc.dry_run_report(self.home, tmp_root=self.tmproot)
        cats = {i["path"]: i["category"] for i in report["reclaimable"]}
        self.assertEqual(cats.get(str(cache)), "cache")

    def test_disk_status_measures(self):
        st = gc.disk_status(self.home)
        self.assertGreater(st["free_bytes"], 0)
        self.assertGreaterEqual(st["home_bytes"], 0)

    def _dead_pid(self):
        proc = subprocess.Popen(["true"])
        pid = proc.pid
        proc.wait()
        return pid

    def test_stale_tmp_lists_only_dead_old_tmps(self):
        # M2.16b: summary/*.tmp older than 1 h whose pid is not
        # alive. Never a fresh tmp, a live-pid tmp, or any .json.
        sdir = self.home / "summary"
        sdir.mkdir(parents=True)
        dead = self._dead_pid()
        stale1 = sdir / f"snapshot.{dead}.tmp"
        stale1.write_text("s" * 520, encoding="utf-8")
        stale2 = sdir / f"batches-snapshot.{dead}.tmp"
        stale2.write_text("b" * 520, encoding="utf-8")
        self.old(stale1, days=2)
        self.old(stale2, days=2)
        fresh = sdir / f"snapshot.{self._dead_pid()}.tmp"
        fresh.write_text("fresh", encoding="utf-8")
        live = sdir / f"snapshot.{os.getpid()}.tmp"
        live.write_text("live", encoding="utf-8")
        self.old(live, days=2)
        (sdir / "snapshot.json").write_text("{}", encoding="utf-8")
        (sdir / "snapshot.prev.json").write_text("{}", encoding="utf-8")
        report = gc.dry_run_report(self.home, tmp_root=self.tmproot)
        got = {i["path"]: i for i in report["reclaimable"]
               if i["category"] == "stale_tmp"}
        self.assertEqual(sorted(got),
                         sorted([str(stale1), str(stale2)]))
        for item in got.values():
            self.assertGreater(item["bytes"], 500)
        # Never a .json, never the fresh/live ones.
        for item in report["reclaimable"]:
            self.assertFalse(item["path"].endswith(".json"))
        self.assertNotIn(str(fresh), got)
        self.assertNotIn(str(live), got)


class DiskWalkTests(GcFixture):
    def test_no_fork_without_exec_in_src(self):
        # S17r: fork-without-exec can deadlock a threaded dashboard
        # child (and Apple frameworks may abort it). Walks spawn via
        # subprocess.Popen (fork+exec) only.
        root = Path(__file__).resolve().parent.parent / "src" / \
            "openmainty"
        hits = sorted(p.name for p in root.glob("*.py")
                      if "os.fork(" in p.read_text(encoding="utf-8"))
        self.assertEqual(hits, [])
    def test_home_bytes_du_measures_dir(self):
        (self.home / "f.txt").write_text("hello", encoding="utf-8")
        size = gc.home_bytes_du(self.home)
        self.assertIsNotNone(size)
        self.assertGreaterEqual(size, 0)

    def test_home_bytes_du_missing_returns_none(self):
        self.assertIsNone(gc.home_bytes_du(self.home / "gone"))

    def test_write_disk_cache_roundtrip(self):
        payload = gc.write_disk_cache(self.home, home_bytes=12345)
        self.assertEqual(payload["home_bytes"], 12345)
        self.assertIsNone(payload["error"])
        self.assertGreater(payload["at_epoch"], 0)
        back = json.loads(
            (self.home / "cache" / "disk.json").read_text(encoding="utf-8"))
        self.assertEqual(back, payload)
        # Only the one small cache file is written — no temp left.
        self.assertFalse(
            (self.home / "cache" / "disk.json.tmp").exists())


class CmdGcTests(GcFixture):
    def test_gc_json_reports(self):
        import io
        from contextlib import redirect_stdout
        from openmainty import cli as _cli
        args = _cli.build_parser().parse_args(
            ["gc", "--dry-run", "--json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _cli.cmd_gc(args)
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        self.assertTrue(report["deletes_nothing"])
        self.assertIn("never", report)

    def test_gc_human_says_no_delete(self):
        import io
        from contextlib import redirect_stdout
        from openmainty import cli as _cli
        args = _cli.build_parser().parse_args(["gc", "--dry-run"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _cli.cmd_gc(args)
        self.assertEqual(rc, 0)
        self.assertIn("삭제 없음", buf.getvalue())

    def test_gc_requires_dry_run(self):
        from openmainty import cli as _cli
        with self.assertRaises(SystemExit):
            _cli.build_parser().parse_args(["gc"])

    def test_dry_run_prints_exact_bytes_per_category(self):
        import io
        from contextlib import redirect_stdout
        from openmainty import cli as _cli
        nm = self.home / "runs" / "r1" / "work" / "node_modules"
        nm.mkdir(parents=True)
        (nm / "pkg.js").write_text("x" * 1000, encoding="utf-8")
        args = _cli.build_parser().parse_args(["gc", "--dry-run"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _cli.cmd_gc(args)
        self.assertEqual(rc, 0)
        # Exact total, copyable straight into --confirm-bytes.
        size = sum(p.stat().st_size for p in nm.rglob("*")
                   if p.is_file())
        self.assertIn(f"({size} bytes)", buf.getvalue())


class ApplyTests(GcFixture):
    """M3.1 gc --apply: exact-set delete with refuse-first gates."""

    MINUTE = 20  # never the owner-cron window

    def apply(self, category, confirm="fresh", **kw):
        if confirm == "fresh":
            report = gc.dry_run_report(self.home,
                                       tmp_root=self.tmproot)
            confirm = sum(int(i.get("bytes") or 0)
                          for i in report.get("reclaimable", [])
                          if i.get("category") == category)
        kw.setdefault("_minute", self.MINUTE)
        return gc.apply_category(self.home, category, confirm,
                                 tmp_root=self.tmproot, **kw)

    def test_apply_deletes_exactly_report_set(self):
        nm = self.home / "runs" / "r1" / "work" / "node_modules"
        nm.mkdir(parents=True)
        (nm / "pkg.js").write_text("x" * 1000, encoding="utf-8")
        keep = self.home / "runs" / "r1" / "work" / "src.js"
        keep.write_text("keep", encoding="utf-8")
        receipt = self.apply("node_modules")
        self.assertFalse(nm.exists())
        self.assertTrue(keep.is_file())
        self.assertEqual(len(receipt["deleted"]), 1)
        self.assertEqual(receipt["skipped"], [])
        self.assertGreater(receipt["freed_bytes"], 900)
        saved = sorted((self.home / "gc").glob("*.json"))
        self.assertEqual(len(saved), 1)

    def test_confirm_mismatch_deletes_nothing(self):
        nm = self.home / "runs" / "r1" / "work" / "node_modules"
        nm.mkdir(parents=True)
        (nm / "pkg.js").write_text("x" * 10, encoding="utf-8")
        with self.assertRaises(gc.ApplyError) as ctx:
            gc.apply_category(self.home, "node_modules", 1,
                              tmp_root=self.tmproot,
                              _minute=self.MINUTE)
        self.assertIn("--confirm-bytes", str(ctx.exception))
        self.assertIn("삭제 안 함", str(ctx.exception))
        self.assertTrue(nm.is_dir())

    def test_stale_worktree_stays_report_only(self):
        with self.assertRaises(gc.ApplyError) as ctx:
            gc.apply_category(self.home, "stale_worktree", 0,
                              tmp_root=self.tmproot,
                              _minute=self.MINUTE)
        self.assertIn("보고서만", str(ctx.exception))

    def test_held_worktree_skipped(self):
        # A done op whose exclusion lock is held (a fastcheck running
        # on it) keeps its node_modules: skipped, never deleted. The
        # status gate alone would pass (done), so the lock check is
        # what saves it.
        import fcntl
        wt = self.home / "reviews" / "wt-live"
        nm = wt / "node_modules"
        nm.mkdir(parents=True)
        (nm / "pkg.js").write_text("x" * 100, encoding="utf-8")
        self.write_op("prep-live", {
            "op": "prepare", "op_id": "prep-live", "status": "done",
            "result": {"path": str(wt), "branch": "codex/live",
                       "head": "h"}})
        lock = self.home / "integration_ops" / ".prep-live.lock"
        fh = open(str(lock), "a+b")
        self.addCleanup(fh.close)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = self.apply("node_modules")
        self.assertTrue(nm.is_dir())
        self.assertEqual(receipt["deleted"], [])
        self.assertEqual(len(receipt["skipped"]), 1)
        self.assertIn("진행 중", receipt["skipped"][0]["reason"])

    def test_running_op_refuses_whole_apply(self):
        self.write_op("prep-live", {
            "op": "prepare", "op_id": "prep-live", "status": "started",
            "result": {"path": "/nowhere", "branch": "codex/live",
                       "head": "h"}})
        with self.assertRaises(gc.ApplyError) as ctx:
            self.apply("cache")
        self.assertIn("진행 중 작업 있음", str(ctx.exception))

    def test_never_names_refused_even_if_forced(self):
        from unittest import mock
        ev = self.home / "integration_ops" / "prep-x.json"
        ev.write_text(json.dumps({"status": "done"}), encoding="utf-8")
        size = ev.stat().st_size
        forced = {"reclaimable": [
            {"category": "cache", "path": str(ev),
             "bytes": size, "note": "forced"}],
            "needs_review": [], "at": "t"}
        with mock.patch.object(gc, "dry_run_report",
                               return_value=forced):
            receipt = gc.apply_category(self.home, "cache", size,
                                        tmp_root=self.tmproot,
                                        _minute=self.MINUTE)
        self.assertTrue(ev.is_file())
        self.assertEqual(receipt["deleted"], [])
        self.assertEqual(len(receipt["skipped"]), 1)
        self.assertIn("증거 영역", receipt["skipped"][0]["reason"])

    def test_symlink_never_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("do-not-touch",
                                            encoding="utf-8")
        link = self.home / "cache" / "yarn"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink unavailable")
        receipt = self.apply("cache")
        self.assertTrue((outside / "secret.txt").is_file())
        self.assertTrue(link.is_symlink())
        reasons = [s["reason"] for s in receipt["skipped"]]
        self.assertTrue(any("심볼릭 링크" in r for r in reasons),
                        receipt["skipped"])

    def test_cron_window_refuses(self):
        with self.assertRaises(gc.ApplyError) as ctx:
            gc.apply_category(self.home, "cache", 0,
                              tmp_root=self.tmproot, _minute=13)
        self.assertIn(":13", str(ctx.exception))

    def test_tmp_prefixes_exact_drift_guard(self):
        # Blocker: OWNED lists ONLY prefixes this repo's own code
        # creates on the system tmp — and every such prefix is
        # listed. A bare "openmainty-" would match the supervision
        # record; "om66-" is Root's scratch.
        import re
        srcd = (Path(__file__).resolve().parent.parent / "src" /
                "openmainty")
        created = set()
        for path in sorted(srcd.glob("*.py")):
            for line in path.read_text(
                    encoding="utf-8").splitlines():
                if "mkdtemp" in line or "TemporaryDirectory" in line:
                    m = re.search(r'prefix\s*=\s*"([^"]+)"', line)
                    if m:
                        created.add(m.group(1))
        self.assertEqual(set(gc.OWNED_TMP_PREFIXES), created)
        self.assertNotIn("openmainty-", gc.OWNED_TMP_PREFIXES)
        self.assertNotIn("om66-", gc.OWNED_TMP_PREFIXES)

    def test_supervision_and_foreign_names_never_listed(self):
        for name in ("openmainty-supervision-x", "om66-x",
                     "openmainty-x"):
            entry = self.tmproot / name
            entry.mkdir()
            (entry / "keep.txt").write_text("keep", encoding="utf-8")
            self.old(entry, days=30)
        report = gc.dry_run_report(self.home, tmp_root=self.tmproot)
        paths = [i["path"] for i in report["reclaimable"]]
        paths += [i["path"] for i in report["needs_review"]]
        self.assertEqual(paths, [])
        receipt = gc.apply_category(self.home, "tmp_scratch", 0,
                                    tmp_root=self.tmproot,
                                    _minute=self.MINUTE)
        self.assertEqual(receipt["deleted"], [])
        for name in ("openmainty-supervision-x", "om66-x",
                     "openmainty-x"):
            self.assertTrue(
                (self.tmproot / name / "keep.txt").is_file())

    def test_owned_probe_prefix_listed_and_deleted(self):
        probe = self.tmproot / "openmainty-probe-x"
        probe.mkdir()
        (probe / "w.txt").write_text("x" * 100, encoding="utf-8")
        self.old(probe, days=30)
        report = gc.dry_run_report(self.home, tmp_root=self.tmproot)
        hits = [i for i in report["reclaimable"]
                if i["path"] == str(probe)]
        self.assertEqual(len(hits), 1)
        total = sum(int(i.get("bytes") or 0)
                    for i in report["reclaimable"]
                    if i.get("category") == "tmp_scratch")
        receipt = gc.apply_category(self.home, "tmp_scratch", total,
                                    tmp_root=self.tmproot,
                                    _minute=self.MINUTE)
        self.assertFalse(probe.exists())
        self.assertEqual(len(receipt["deleted"]), 1)

    def test_stale_tmp_apply_needs_matching_confirm(self):
        # M2.16b: refuses without matching confirm_bytes and
        # deletes exactly the listed files. The snapshot.json
        # beside them is never touched.
        sdir = self.home / "summary"
        sdir.mkdir(parents=True)
        dead = DryRunTests._dead_pid(self)
        victim = sdir / f"snapshot.{dead}.tmp"
        victim.write_text("v" * 520, encoding="utf-8")
        self.old(victim, days=2)
        keep = sdir / "snapshot.json"
        keep.write_text("{}", encoding="utf-8")
        with self.assertRaises(gc.ApplyError) as ctx:
            gc.apply_category(self.home, "stale_tmp", 0,
                              tmp_root=self.tmproot,
                              _minute=self.MINUTE)
        self.assertIn("확인 바이트 불일치", str(ctx.exception))
        self.assertTrue(victim.is_file())
        receipt = self.apply("stale_tmp")
        self.assertFalse(victim.exists())
        self.assertTrue(keep.is_file())
        self.assertEqual(len(receipt["deleted"]), 1)
        self.assertEqual(receipt["deleted"][0]["bytes"], 520)
        self.assertEqual(receipt["skipped"], [])
        saved = sorted((self.home / "gc").glob("*.json"))
        self.assertEqual(len(saved), 1)

    def test_stale_tmp_forced_json_never_deleted(self):
        # Even a forced report cannot delete a .json through the
        # stale_tmp category, nor anything outside summary/.
        from unittest import mock
        ev = self.home / "integration_ops" / "prep-x.json"
        ev.write_text(json.dumps({"status": "done"}), encoding="utf-8")
        size = ev.stat().st_size
        forced = {"reclaimable": [
            {"category": "stale_tmp", "path": str(ev),
             "bytes": size, "note": "forced"}],
            "needs_review": [], "at": "t"}
        with mock.patch.object(gc, "dry_run_report",
                               return_value=forced):
            receipt = gc.apply_category(self.home, "stale_tmp",
                                        size,
                                        tmp_root=self.tmproot,
                                        _minute=self.MINUTE)
        self.assertTrue(ev.is_file())
        self.assertEqual(receipt["deleted"], [])
        self.assertEqual(len(receipt["skipped"]), 1)

    def test_daemon_never_applies(self):
        # stale_tmp is owner-CLI only: no daemon/scheduler/serve
        # path may call apply_category.
        srcd = (Path(__file__).resolve().parent.parent / "src" /
                "openmainty")
        for name in ("sched.py", "serve.py", "m2.py", "batches.py",
                     "runner.py"):
            src = (srcd / name).read_text(encoding="utf-8")
            self.assertNotIn("apply_category", src, name)

    def test_fingerprint_unchanged_after_node_modules_delete(self):
        if not HAVE_GIT:
            self.skipTest("needs git")
        from openmainty import runner as _runner
        co, _head, _env = self.make_repo()
        (co / ".gitignore").write_text("node_modules/\n",
                                       encoding="utf-8")
        (co / "src.js").write_text("console.log(1)\n",
                                   encoding="utf-8")
        nm = co / "node_modules"
        nm.mkdir()
        (nm / "pkg.js").write_text("x" * 500, encoding="utf-8")
        before = _runner.source_snapshot(co)["fingerprint"]
        import shutil as _shutil
        _shutil.rmtree(nm)
        after = _runner.source_snapshot(co)["fingerprint"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
