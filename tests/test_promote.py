"""S6 promote tests: whole-tree verify, atomic switch, rollback, prune.

All fixture trees and homes live under temp dirs; nothing may escape
into the real ~/.openmainty (every promote call gets home=tmp, and the
OPENMAINTY_HOME override is restored after each test).
"""
import hashlib
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import promote
from openmainty import cli as _cli


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PromoteFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tree = self.root / "dev"
        self.home = self.root / "home"
        self._old_home = os.environ.get("OPENMAINTY_HOME")
        promote._DIFF_CACHE = {"at": 0.0, "value": None}

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("OPENMAINTY_HOME", None)
        else:
            os.environ["OPENMAINTY_HOME"] = self._old_home
        promote._DIFF_CACHE = {"at": 0.0, "value": None}

    def mk_tree(self, extra=None, tag="v1"):
        files = {
            "pyproject.toml": b"[project]\nname = 'om'\n",
            "src/openmainty/__init__.py": b"",
            "src/openmainty/serve.py": ("# serve " + tag).encode(),
            "src/openmainty/batches.py": ("# batches " + tag).encode(),
            "src/openmainty/sched.py": ("# sched " + tag).encode(),
            "src/openmainty/dashboard.html": b"<html></html>",
            "src/openmainty/cli.py": b"# cli",
            "tests/test_x.py": b"# tests never run promoted",
            "openmainty": b"#!/usr/bin/env python3\n",
        }
        files.update(extra or {})
        for rel, data in files.items():
            p = self.tree / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return files

    def mk_manifest(self, files):
        return {"sources_sha256": {
            rel: _sha_bytes(data) for rel, data in files.items()}}

    def test_1_promote_happy(self):
        files = self.mk_tree()
        out = promote.promote(self.mk_manifest(files), root=self.tree,
                              home=self.home, at="t1")
        dest = Path(out["path"])
        self.assertTrue((dest / "src/openmainty/serve.py").is_file())
        self.assertTrue((dest / "pyproject.toml").is_file())
        self.assertTrue((dest / "openmainty").is_file())
        self.assertTrue((dest / "PROMOTED.json").is_file())
        self.assertFalse((dest / "tests").exists())
        self.assertEqual(promote.current_link(self.home).resolve(),
                         dest.resolve())
        prom = json.loads((dest / "PROMOTED.json").read_text())
        self.assertEqual(prom["sha"], out["sha"])
        self.assertEqual(prom["launcher_sha"],
                         _sha_bytes(b"#!/usr/bin/env python3\n"))

    def test_2_second_promote_switches(self):
        files = self.mk_tree()
        a = promote.promote(self.mk_manifest(files), root=self.tree,
                            home=self.home, at="t1")
        files2 = self.mk_tree(tag="v2")
        b = promote.promote(self.mk_manifest(files2), root=self.tree,
                            home=self.home, at="t2")
        self.assertNotEqual(a["sha"], b["sha"])
        self.assertEqual(promote.current_link(self.home).resolve().name,
                         b["sha"])
        self.assertEqual(b["previous"], a["sha"])

    def test_3_refuse_modified(self):
        files = self.mk_tree()
        man = self.mk_manifest(files)
        (self.tree / "src/openmainty/serve.py").write_bytes(b"# TAMPERED")
        with self.assertRaises(promote.PromoteError) as ctx:
            promote.promote(man, root=self.tree, home=self.home, at="t")
        self.assertIn("changed: src/openmainty/serve.py", str(ctx.exception))
        self.assertFalse((self.home / "app").exists())

    def test_4_refuse_extra_src(self):
        files = self.mk_tree()
        man = self.mk_manifest(files)
        (self.tree / "src/openmainty/rogue.py").write_text("x")
        with self.assertRaises(promote.PromoteError) as ctx:
            promote.promote(man, root=self.tree, home=self.home, at="t")
        self.assertIn("unlisted src file", str(ctx.exception))

    def test_5_refuse_missing(self):
        files = self.mk_tree()
        man = self.mk_manifest(files)
        (self.tree / "src/openmainty/sched.py").unlink()
        with self.assertRaises(promote.PromoteError) as ctx:
            promote.promote(man, root=self.tree, home=self.home, at="t")
        self.assertIn("missing: src/openmainty/sched.py", str(ctx.exception))

    def test_6_current_always_resolves(self):
        files = self.mk_tree()
        a = promote.promote(self.mk_manifest(files), root=self.tree,
                            home=self.home, at="t1")
        files2 = self.mk_tree(tag="v2")
        b = promote.promote(self.mk_manifest(files2), root=self.tree,
                            home=self.home, at="t2")
        link = promote.current_link(self.home)
        errors = []

        def hammer():
            try:
                for _ in range(100):
                    if not link.resolve().is_dir():
                        errors.append("dangling")
            except Exception as exc:  # noqa: BLE001 - record, don't raise
                errors.append(type(exc).__name__)

        workers = [threading.Thread(target=hammer) for _ in range(2)]
        for w in workers:
            w.start()
        for _ in range(5):
            promote.rollback(home=self.home, at="x")
            promote.rollback(b["sha"], home=self.home, at="x")
        for w in workers:
            w.join()
        self.assertEqual(errors, [])
        self.assertEqual(link.resolve().name, b["sha"])
        self.assertEqual(a["sha"], promote.rollback(
            home=self.home, at="x")["sha"])

    def test_7_rollback_and_unknown(self):
        files = self.mk_tree()
        a = promote.promote(self.mk_manifest(files), root=self.tree,
                            home=self.home, at="t1")
        b = promote.promote(self.mk_manifest(self.mk_tree(tag="v2")),
                            root=self.tree, home=self.home, at="t2")
        back = promote.rollback(home=self.home, at="t3")
        self.assertEqual(back["sha"], a["sha"])
        self.assertEqual(back["previous"], b["sha"])
        with self.assertRaises(promote.PromoteError):
            promote.rollback("deadbeef" * 2, home=self.home, at="t4")

    def test_8_prune_keeps_current_and_rollback_target(self):
        shas = []
        for i in range(4):
            files = self.mk_tree(tag=f"v{i}")
            out = promote.promote(self.mk_manifest(files), root=self.tree,
                                  home=self.home, at=f"t{i}")
            shas.append(out["sha"])
        sentinel = self.home / "app" / "sentinel.txt"
        sentinel.write_text("hands off")
        with self.assertRaises(promote.PromoteError):
            promote.prune(home=self.home, at="x")
        out = promote.prune(home=self.home, keep=3, assume_yes=True, at="y")
        # 4 trees, keep 3 + current + rollback target: exactly 1 goes,
        # and it is neither current (shas[3]) nor its rollback target.
        self.assertEqual(len(out["removed"]), 1)
        gone = out["removed"][0]
        self.assertIn(gone, shas)
        self.assertNotIn(gone, out["kept"])
        self.assertIn(shas[3], out["kept"])
        self.assertIn(shas[2], out["kept"])
        self.assertTrue(sentinel.is_file())
        self.assertFalse((self.home / "app" / gone).exists())
        self.assertTrue((self.home / "app" / shas[3]).is_dir())

    def test_8b_prune_keeps_newest_not_lexical(self):
        # Lexical order ("000" < "888" < "fff") differs from promotion
        # order (fff, then 000, then 888). keep=2 must spare the two
        # newest (000, 888), not the two lexical-last (888, fff).
        adir = self.home / "app"
        adir.mkdir(parents=True)
        names = ["f" * 16, "0" * 16, "8" * 16]
        for name in names:
            d = adir / name
            d.mkdir()
            (d / "PROMOTED.json").write_text(json.dumps({"sha": name}))
        with open(adir / "history.jsonl", "a",
                   encoding="utf-8") as fh:
            for name in names:
                fh.write(json.dumps({"event": "promote", "from": "",
                                     "to": name, "at": "t"}) + "\n")
        (adir / "current").symlink_to(names[2])
        out = promote.prune(home=self.home, keep=2, assume_yes=True,
                            at="t")
        self.assertEqual(out["removed"], [names[0]])
        self.assertEqual(sorted(out["kept"]), [names[1], names[2]])
        # Unknown dirs (no promote event) count as oldest.
        stray = adir / ("a" * 16)
        stray.mkdir()
        (stray / "PROMOTED.json").write_text("{}")
        ordered = promote._promo_order(
            self.home, [names[1], names[2], stray.name])
        self.assertEqual(ordered[0], stray.name)

    def test_9_app_info_and_differs(self):
        self.assertEqual(promote.app_info(home=self.home), {})
        self.assertIsNone(promote.dev_differs(home=self.home))
        files = self.mk_tree()
        out = promote.promote(self.mk_manifest(files), root=self.tree,
                              home=self.home, at="t1")
        info = promote.app_info(home=self.home)
        self.assertEqual(info["sha"], out["sha"])
        self.assertFalse(info["running"])
        self.assertFalse(promote.dev_differs(home=self.home))
        (self.tree / "src/openmainty/serve.py").write_bytes(b"# CHANGED")
        promote._DIFF_CACHE = {"at": 0.0, "value": None}
        self.assertTrue(promote.dev_differs(home=self.home))
        line = promote.app_start_line(home=self.home)
        self.assertRegex(line, r"^app (dev-tree|[0-9a-f]{12}) daemon-tick start$")
        self.assertIn("dev-tree", line)

    def test_10_plist_launcher_choice(self):
        text = _cli.dashboard_plist_text("/x/openmainty", 8901, "/bin",
                                         self.home)
        self.assertIn("<string>/x/openmainty</string>", text)
        self.assertIn("dev.openmainty.dashboard", text)
        os.environ["OPENMAINTY_HOME"] = str(self.home)
        try:
            # No promotion here: falls back to the dev launcher.
            self.assertEqual(_cli.dashboard_launcher().name, "openmainty")
            files = self.mk_tree()
            promote.promote(self.mk_manifest(files), root=self.tree,
                            home=self.home, at="t1")
            launcher = _cli.dashboard_launcher()
            # Unresolved on purpose: reinstalls survive later promotes.
            self.assertTrue(str(launcher).endswith("app/current/openmainty"))
            self.assertTrue(launcher.is_file())
            text2 = _cli.dashboard_plist_text(launcher, 8901, "/bin",
                                              self.home)
            self.assertIn("app/current/openmainty", text2)
        finally:
            os.environ.pop("OPENMAINTY_HOME", None)
            if self._old_home is not None:
                os.environ["OPENMAINTY_HOME"] = self._old_home

    def test_12_api_app_and_snapshot_sha_dev(self):
        from openmainty import batches as _batches
        from openmainty import serve as _serve
        os.environ["OPENMAINTY_HOME"] = str(self.home)
        try:
            out = _serve.api_app({})
            # G9 health contract: base identity plus admitted-handler
            # count, the cap, the open-fd count, and a degraded flag.
            self.assertIsNone(out["app_sha"])
            self.assertIsNone(out["dev_differs"])
            self.assertIsInstance(out["in_flight"], int)
            self.assertGreaterEqual(out["in_flight"], 0)
            self.assertEqual(out["handler_cap"], _serve._HANDLER_CAP)
            self.assertTrue(out["open_fds"] is None
                            or (isinstance(out["open_fds"], int)
                                and out["open_fds"] >= 0))
            self.assertIsInstance(out["degraded"], bool)
            self.assertEqual(set(out), {"app_sha", "dev_differs",
                                        "in_flight", "handler_cap",
                                        "open_fds", "degraded"})
            self.assertIsNone(_batches._m1_app_sha())
        finally:
            os.environ.pop("OPENMAINTY_HOME", None)
            if self._old_home is not None:
                os.environ["OPENMAINTY_HOME"] = self._old_home

    def test_14_promoted_launcher_executes(self):
        # S6.1-a: the gap was a non-executable promoted launcher that no
        # test ever ran. Promote a runnable fixture tree, then execute
        # <home>/app/current/openmainty in a subprocess end to end.
        import stat as _stat
        import subprocess as _sp
        tree = self.root / "runnable"
        launcher_src = (b"#!/usr/bin/env python3\n"
                        b"import sys\n"
                        b"from pathlib import Path\n"
                        b"sys.path.insert(0, str(Path(__file__).resolve()"
                        b".parent / 'src'))\n"
                        b"from openmainty.cli import main\n"
                        b"if __name__ == '__main__':\n"
                        b"    raise SystemExit(main())\n")
        files = {
            "pyproject.toml": b"[project]\n",
            "src/openmainty/__init__.py": b"",
            "src/openmainty/cli.py": (b"def main():\n"
                                      b"    print('runnable-ok')\n"
                                      b"    return 0\n"),
            "openmainty": launcher_src,
        }
        for rel, data in files.items():
            p = tree / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        man = {"sources_sha256": {
            rel: hashlib.sha256(d).hexdigest()
            for rel, d in files.items()}}
        out = promote.promote(man, root=tree, home=self.home, at="t1")
        got = Path(out["path"]) / "openmainty"
        self.assertTrue(bool(got.stat().st_mode & _stat.S_IXUSR))
        self.assertTrue(os.access(got, os.X_OK))
        env = dict(os.environ)
        env["OPENMAINTY_HOME"] = str(self.home)
        proc = _sp.run([str(self.home / "app" / "current" / "openmainty")],
                       capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertIn("runnable-ok", proc.stdout)

    def test_15_repair_identical_repromote(self):
        import stat as _stat
        files = self.mk_tree()
        out = promote.promote(self.mk_manifest(files), root=self.tree,
                              home=self.home, at="t1")
        dest = Path(out["path"])
        # Simulate the S6.0 defect: mode bits lost on the copy.
        os.chmod(dest / "openmainty", 0o644)
        fixed = promote.promote(self.mk_manifest(files), root=self.tree,
                                home=self.home, at="t2")
        self.assertTrue(fixed.get("repaired"))
        self.assertEqual(fixed["sha"], out["sha"])
        self.assertTrue(bool((dest / "openmainty").stat().st_mode
                             & _stat.S_IXUSR))
        # A byte difference behind the same sha still refuses.
        (dest / "src/openmainty/serve.py").write_bytes(b"# ROGUE")
        with self.assertRaises(promote.PromoteError) as ctx:
            promote.promote(self.mk_manifest(files), root=self.tree,
                            home=self.home, at="t3")
        self.assertIn("exists and differs", str(ctx.exception))

    def test_16_owner_commands_runnable(self):
        text = _cli.owner_promote_commands("20261020")
        self.assertIn("launchd-backup-20261020", text)
        self.assertIn("Documents/OpenMainty", text)
        self.assertNotIn("$HOME", text)
        self.assertNotIn(" ~/Library", text)
        self.assertIn("/usr/libexec/PlistBuddy", text)
        self.assertIn("dev.openmainty.dashboard.plist", text)
        self.assertIn("dev.openmainty.maintenance.plist", text)
        self.assertIn("plutil -lint", text)
        self.assertIn("bootstrap", text)
        self.assertIn("curl -s http://127.0.0.1:8901/api/app", text)
        self.assertIn("daemon-tick start", text)
        self.assertIn("rollback", text)
        self.assertNotIn("Desktop", text)
        # S6.2: grep-not-head verify, both pre-checks, RunAtLoad note.
        self.assertIn('grep "daemon-tick start"', text)
        self.assertIn("| tail -1", text)
        self.assertNotIn("head -1", text)
        self.assertIn("app <sha12> daemon-tick start", text)
        self.assertIn('date -u +%M', text)
        self.assertIn("lsof", text)
        self.assertIn(".worker.flock", text)
        self.assertIn("RunAtLoad ticks immediately", text)

    def test_13_glance_footer_wiring(self):
        # Accepted Source9 footer contract: running version is always
        # the fresh /api/app sha, never a snapshot-baked value; unknown
        # stays unknown; dev-differs warning clears on fetch failure.
        src = (Path(__file__).resolve().parent.parent / "src" / "openmainty"
               / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="appfoot"', src)
        self.assertIn('id="appver"', src)
        self.assertIn('id="appdiff"', src)
        self.assertIn('jget("/api/app")', src)
        self.assertIn("앱 버전 확인 중", src)
        self.assertIn("앱 버전 확인 불가", src)
        self.assertIn("개발 트리와 다름", src)
        self.assertNotIn("개발 트리 실행 중", src)
        self.assertNotIn("(r.system || {}).app_sha", src)

    def test_17_followup_skips_switch_when_already_current(self):
        # S16b: both plists already run app/current → one-line
        # restart, not the one-time switch block. Read-only on
        # the plists (mtimes unchanged).
        import plistlib
        launcher = "/Users/o/.openmainty/app/current/openmainty"
        agents = self.root / "agents"
        agents.mkdir()
        for name in ("dev.openmainty.dashboard.plist",
                     "dev.openmainty.maintenance.plist"):
            (agents / name).write_bytes(plistlib.dumps(
                {"ProgramArguments": [launcher, "serve"]}))
        before = {p: p.stat().st_mtime_ns for p in agents.iterdir()}
        text = _cli.promote_followup_text(
            agents / "dev.openmainty.dashboard.plist",
            agents / "dev.openmainty.maintenance.plist",
            launcher, 501)
        self.assertIn("이미 승격 사본을 실행 중", text)
        self.assertIn("launchctl kickstart -k gui/501/"
                      "dev.openmainty.dashboard", text)
        self.assertNotIn("PlistBuddy", text)
        self.assertEqual(
            {p: p.stat().st_mtime_ns for p in agents.iterdir()}, before)

    def test_18_followup_prints_switch_when_stale(self):
        # One plist points elsewhere (or is missing) → the full
        # one-time switch block.
        import plistlib
        launcher = "/Users/o/.openmainty/app/current/openmainty"
        agents = self.root / "agents"
        agents.mkdir()
        (agents / "dev.openmainty.dashboard.plist").write_bytes(
            plistlib.dumps({"ProgramArguments": [launcher]}))
        (agents / "dev.openmainty.maintenance.plist").write_bytes(
            plistlib.dumps({"ProgramArguments": ["/old/tree/openmainty",
                                                 "sched"]}))
        text = _cli.promote_followup_text(
            agents / "dev.openmainty.dashboard.plist",
            agents / "dev.openmainty.maintenance.plist",
            launcher, 501)
        self.assertIn("PlistBuddy", text)
        self.assertNotIn("이미 승격 사본을 실행 중", text)
        # Missing files count as not-switched, never crash.
        text2 = _cli.promote_followup_text(
            agents / "dev.openmainty.dashboard.plist",
            agents / "gone.plist", launcher, 501)
        self.assertIn("PlistBuddy", text2)

    def test_19_followup_skips_switch_for_bash_wrapper(self):
        # F1(243): plists that run /bin/bash run-agent.sh — whose script
        # execs app/current/openmainty — already track promotion: the
        # one-line restart, not the switch block. Read-only on the
        # plists and the wrapper (mtimes unchanged).
        import plistlib
        launcher = "/Users/o/.openmainty/app/current/openmainty"
        agents = self.root / "agents"
        agents.mkdir()
        svc = self.root / "svc"
        svc.mkdir()
        wrapper = svc / "run-agent.sh"
        wrapper.write_text(
            "#!/bin/bash\nom_home=/Users/o/.openmainty\n"
            '"$om_home/app/current/openmainty" "$@"\n',
            encoding="utf-8")
        specs = (("dev.openmainty.dashboard.plist", "dashboard.log"),
                 ("dev.openmainty.maintenance.plist", "maintenance.log"))
        for name, log in specs:
            (agents / name).write_bytes(plistlib.dumps(
                {"ProgramArguments": ["/bin/bash", str(wrapper),
                                      log, "serve"]}))
        before = {p: p.stat().st_mtime_ns
                  for p in list(agents.iterdir()) + [wrapper]}
        text = _cli.promote_followup_text(
            agents / "dev.openmainty.dashboard.plist",
            agents / "dev.openmainty.maintenance.plist",
            launcher, 501)
        self.assertIn("이미 승격 사본을 실행 중", text)
        self.assertIn("launchctl kickstart -k gui/501/"
                      "dev.openmainty.dashboard", text)
        self.assertNotIn("PlistBuddy", text)
        self.assertEqual(
            {p: p.stat().st_mtime_ns
             for p in list(agents.iterdir()) + [wrapper]}, before)
        # A wrapper that does not exec the promoted copy still needs
        # the switch block — and so does a missing wrapper.
        wrapper.write_text("#!/bin/bash\necho stale\n", encoding="utf-8")
        text2 = _cli.promote_followup_text(
            agents / "dev.openmainty.dashboard.plist",
            agents / "dev.openmainty.maintenance.plist",
            launcher, 501)
        self.assertIn("PlistBuddy", text2)
        wrapper.unlink()
        text3 = _cli.promote_followup_text(
            agents / "dev.openmainty.dashboard.plist",
            agents / "dev.openmainty.maintenance.plist",
            launcher, 501)
        self.assertIn("PlistBuddy", text3)

    def test_11_cmd_promote_refusals(self):
        class Args:
            manifest = None
            rollback = None
            prune = False
            yes = False
            keep = 3

        os.environ["OPENMAINTY_HOME"] = str(self.home)
        try:
            with self.assertRaises(SystemExit):
                _cli.cmd_promote(Args())
            Args.rollback = ""
            self.assertEqual(_cli.cmd_promote(Args()), 2)
            Args.rollback = None
            Args.manifest = str(self.root / "nope.json")
            self.assertEqual(_cli.cmd_promote(Args()), 2)
            bad = self.root / "bad.json"
            bad.write_text("{not json")
            Args.manifest = str(bad)
            self.assertEqual(_cli.cmd_promote(Args()), 2)
        finally:
            os.environ.pop("OPENMAINTY_HOME", None)
            if self._old_home is not None:
                os.environ["OPENMAINTY_HOME"] = self._old_home
