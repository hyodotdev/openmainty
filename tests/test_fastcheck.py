"""M2.1b-2 fastcheck runner tests (t93 E + sandbox/scrub/kill proofs).

All offline: fixture argv steps, a temp OPENMAINTY_HOME, an injected
disk measure and sandbox path. Real process groups, timeouts and the
double-fork reaper run for real on POSIX. Py3.9 portable.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import fastcheck, repos, sched  # noqa: E402
from openmainty.util import write_json  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

HAVE_SANDBOX = os.path.isfile(fastcheck.SANDBOX_EXEC)
HAVE_FORK = hasattr(os, "fork")

POISON = {"GH_TOKEN": "secret-gh", "GITHUB_TOKEN": "secret-gh2",
          "SSH_AUTH_SOCK": "/tmp/ssh-sock",
          "AWS_SECRET_ACCESS_KEY": "secret-aws",
          "MY_API_KEY": "secret-key",
          "NPM_CONFIG_FOO": "nope",
          "OPENMAINTY_HOME": "/should/not/leak"}


class FastcheckFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # L2: the guard enforces the macOS AF_UNIX ~103-byte cap on
        # the temp HOME, so the fixture home is short like the
        # production ~/.openmainty (a /var/folders home would trip
        # the guard before any step runs).
        self.short = TemporaryDirectory(prefix="om66fc-", dir="/tmp")
        self.addCleanup(self.short.cleanup)
        self.home = Path(self.short.name) / "home"
        self._old_home = os.environ.get("OPENMAINTY_HOME")
        os.environ["OPENMAINTY_HOME"] = str(self.home)
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})
        # Mirrors production (<home>/reviews/<slug>); K1 validation
        # requires every reaper root inside the app home.
        self.worktree = self.home / "reviews" / "w"
        self.worktree.mkdir(parents=True)
        self._old_env = dict(os.environ)
        # Test-env robustness: the product reaper shells out to lsof
        # via PATH, and every real macOS shell has /usr/sbin on PATH
        # while the bare gate env does not. Ensure the system sbin
        # dirs without touching anything the scrubbed step env asserts
        # (scrub_env builds its own PATH). Restored by _restore_env.
        _path_parts = os.environ.get("PATH", "").split(os.pathsep)
        for _d in ("/usr/sbin", "/sbin"):
            if os.path.isdir(_d) and _d not in _path_parts:
                _path_parts.insert(0, _d)
                os.environ["PATH"] = os.pathsep.join(_path_parts)
        os.environ.update(POISON)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        if self._old_home is None:
            os.environ.pop("OPENMAINTY_HOME", None)
        else:
            os.environ["OPENMAINTY_HOME"] = self._old_home

    def profile(self, steps, toolchain=None):
        tools = self.root / "tools"
        tools.mkdir(exist_ok=True)
        return {"steps": steps,
                "toolchain": [str(tools)] if toolchain is None
                else toolchain}

    def py_step(self, code, install=True, timeout_s=120, network=False):
        return {"argv": [sys.executable, "-c", code],
                "install": install, "timeout_s": timeout_s}

    def fcrun(self, op="op-fc1", steps=None, _disk_avail=None,
            _sandbox_exec=fastcheck.SANDBOX_EXEC, **kw):
        if steps is None:
            steps = [self.py_step("pass")]
        return fastcheck.run(op, str(self.worktree),
                             self.profile(steps), head="abc",
                             fingerprint="fp1", home=self.home,
                             _disk_avail=_disk_avail,
                             _sandbox_exec=_sandbox_exec, **kw)

    def big_disk(self, path):
        return 31 * 1024 ** 3


class ScrubTests(FastcheckFixture):
    def test_env_scrubbed_allowlist_only(self):
        code = ("import os, json; "
                "print(json.dumps(dict(os.environ)))")
        summary = self.fcrun(steps=[self.py_step(code)],
                           _disk_avail=self.big_disk)
        self.assertTrue(summary["ok"])
        log = Path(summary["steps"][0]["log"]).read_text(
            encoding="utf-8")
        seen = json.loads(log.strip().splitlines()[-1])
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK",
                    "AWS_SECRET_ACCESS_KEY", "MY_API_KEY",
                    "NPM_CONFIG_FOO", "OPENMAINTY_HOME"):
            self.assertNotIn(key, seen)
        self.assertEqual(seen["CI"], "1")
        self.assertNotEqual(seen["HOME"], "/should/not/leak")
        self.assertRegex(seen["HOME"], r"/fc-[0-9a-f]{10}-\d+$")
        self.assertEqual(seen["TMPDIR"], seen["HOME"])
        self.assertIn("YARN_ENABLE_SCRIPTS", seen)
        self.assertIn("YARN_CACHE_FOLDER", seen)
        self.assertIn("COREPACK_HOME", seen)

    def test_lang_default_and_inherited(self):
        tools = self.root / "tools"
        env = fastcheck.scrub_env([str(tools)], "/tmp/x", "/tmp/c")
        os.environ.pop("LANG", None)
        env2 = fastcheck.scrub_env([str(tools)], "/tmp/x", "/tmp/c")
        self.assertEqual(env2["LANG"], "en_US.UTF-8")
        os.environ["LANG"] = "ko_KR.UTF-8"
        try:
            env3 = fastcheck.scrub_env([str(tools)], "/tmp/x", "/tmp/c")
        finally:
            del os.environ["LANG"]
        self.assertEqual(env3["LANG"], "ko_KR.UTF-8")

    def test_registry_recorded(self):
        summary = self.fcrun(_disk_avail=self.big_disk)
        self.assertEqual(summary["registry_host"], "default")
        os.environ["NPM_CONFIG_REGISTRY"] = "https://r.example.com"
        try:
            summary2 = self.fcrun(op="op-fc2", _disk_avail=self.big_disk)
        finally:
            del os.environ["NPM_CONFIG_REGISTRY"]
        self.assertEqual(summary2["registry_host"],
                         "https://r.example.com")


class KillTests(FastcheckFixture):
    def test_timeout_kills_process_group(self):
        started = time.time()
        summary = self.fcrun(
            steps=[self.py_step("import time; time.sleep(30)",
                                timeout_s=2)],
            _disk_avail=self.big_disk)
        self.assertFalse(summary["ok"])
        step = summary["steps"][0]
        self.assertTrue(step["timed_out"])
        self.assertTrue(step["children_verified_gone"])
        self.assertLess(time.time() - started, 25)


@unittest.skipUnless(HAVE_FORK, "needs os.fork")
class ReaperTests(FastcheckFixture):
    def test_double_forked_sleeper_reported_and_reaped(self):
        script = self.root / "sleeper.py"
        script.write_text("import os, time\n"
                          "c = os.fork()\n"
                          "if c == 0:\n"
                          "    g = os.fork()\n"
                          "    if g == 0:\n"
                          "        os.setsid()\n"
                          "        time.sleep(60)\n"
                          "    os._exit(0)\n"
                          "os.wait()\n",
                          encoding="utf-8")
        steps = [{"argv": [sys.executable, str(script)],
                  "install": True, "timeout_s": 60}]
        summary = self.fcrun(steps=steps, _disk_avail=self.big_disk)
        step = summary["steps"][0]
        self.assertEqual(step["rc"], 0)
        # K5: every reaped root sits under this test's temp dir.
        valid, why = fastcheck.validate_reaper_roots(
            [str(self.worktree)], self.home)
        self.assertEqual(why, "ok")
        for entry in step["escaped_processes"]:
            # Resolved: macOS symlinks /tmp to /private/tmp.
            self.assertTrue(str(Path(entry["cwd"]).resolve()).startswith(
                str(Path(self.short.name).resolve())))
        escaped = step["escaped_processes"]
        self.assertEqual(len(escaped), 1)
        self.assertTrue(escaped[0]["gone"])
        self.assertIn(str(self.worktree), escaped[0]["cwd"])


class ReaperSafetyTests(FastcheckFixture):
    def patch(self, name, fake):
        real = getattr(fastcheck, name)
        setattr(fastcheck, name, fake)
        self.addCleanup(setattr, fastcheck, name, real)

    def test_k1_unsafe_roots_refused(self):
        for roots in (["/"], ["/tmp"], [str(self.home)], [""],
                      ["relative/x"]):
            valid, reason = fastcheck.validate_reaper_roots(
                roots, self.home)
            self.assertEqual(valid, [], roots)
            self.assertNotEqual(reason, "ok", roots)
        valid, reason = fastcheck.validate_reaper_roots(
            [str(self.worktree)], self.home)
        self.assertEqual(reason, "ok")
        self.assertEqual(len(valid), 1)

    def test_k1_unsafe_roots_kill_nothing(self):
        euid = os.geteuid()
        self.patch("_ps_table",
                   lambda: [(99999, euid, "evil --root /")])
        self.patch("_all_cwds", lambda uid: {99999: "/"})
        self.patch("_cwd_of", lambda pid: "/")
        self.patch("_uid_of", lambda pid: euid)
        kills = []
        real_kill = os.kill
        os.kill = lambda pid, sig: kills.append((pid, sig))
        self.addCleanup(setattr, os, "kill", real_kill)
        out = fastcheck.reap_escaped(
            [{"pid": 99999, "cmd": "evil", "cwd": "/"}],
            ["/tmp"], self.home)
        self.assertEqual(kills, [])
        self.assertFalse(out[0]["gone"])
        self.assertIn("unsafe root", out[0]["note"])

    def test_k2_boundary_and_unverified(self):
        euid = os.geteuid()
        self.patch("_parent_chain", lambda: set())
        self.patch("_all_cwds", lambda uid: {})
        # ".../prep-x" must not match ".../prep-x-lockfix".
        self.patch("_ps_table", lambda: [
            (11, euid, "/x/prep-x-lockfix --flag"),
            (12, euid, "/x/prep-x --flag"),
            (13, euid, "/x/prep-x/sub --flag")])
        found = fastcheck.find_escaped(["/x/prep-x"])
        self.assertEqual(found["kill"], [])
        self.assertEqual([i["pid"] for i in found["unverified"]],
                         [12, 13])

    def test_k2_cwd_confirmed_kills_argv_only_reports(self):
        euid = os.geteuid()
        root = str(self.worktree)
        self.patch("_parent_chain", lambda: set())
        self.patch("_all_cwds",
                   lambda uid: {21: root + "/sub"})
        self.patch("_ps_table", lambda: [
            (21, euid, "jest worker"),
            (22, euid, "run %s --watch" % root)])
        found = fastcheck.find_escaped([root])
        self.assertEqual([i["pid"] for i in found["kill"]], [21])
        self.assertEqual([i["pid"] for i in found["unverified"]],
                         [22])

    def test_k3_never_self_parents_one_or_other_uid(self):
        euid = os.geteuid()
        other = 0 if euid != 0 else 1
        root = str(self.worktree)
        self.patch("_parent_chain", lambda: {777})
        self.patch("_all_cwds", lambda uid: {
            1: root, os.getpid(): root, 777: root, 888: root})
        self.patch("_ps_table", lambda: [
            (1, euid, "init"),
            (os.getpid(), euid, "myself"),
            (777, euid, "parent"),
            (888, other, "stranger")])
        found = fastcheck.find_escaped([root])
        self.assertEqual(found["kill"], [])
        self.assertEqual(found["unverified"], [])

    def test_h4_home_mandatory(self):
        # H4: no caller can skip the K1 validation by omitting home.
        with self.assertRaises(TypeError):
            fastcheck.reap_escaped([], [str(self.worktree)])

    def test_k4_single_lsof_for_all_cwds(self):
        euid = os.geteuid()
        calls = []
        self.patch("_parent_chain", lambda: set())

        def fake(uid):
            calls.append(uid)
            return {}

        self.patch("_all_cwds", fake)
        self.patch("_ps_table", lambda: [
            (31 + i, euid, "proc %d" % i) for i in range(5)])
        fastcheck.find_escaped([str(self.worktree)])
        self.assertEqual(len(calls), 1)


class SandboxTests(FastcheckFixture):
    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_non_install_step_sandboxed(self):
        summary = self.fcrun(
            steps=[{"argv": [sys.executable, "-c", "pass"],
                    "install": False, "timeout_s": 60}],
            _disk_avail=self.big_disk)
        step = summary["steps"][0]
        self.assertTrue(summary["ok"])
        self.assertTrue(step["network_sandbox"])
        self.assertTrue(step["sandboxed_argv"])

    def test_missing_sandbox_skips_never_runs(self):
        with self.assertRaises(fastcheck.FastcheckSkip) as ctx:
            self.fcrun(steps=[{"argv": [sys.executable, "-c", "pass"],
                             "install": False, "timeout_s": 60}],
                     _disk_avail=self.big_disk,
                     _sandbox_exec="/nonexistent/sandbox-exec")
        self.assertIn("sandbox-exec", str(ctx.exception))

    def test_npm_install_forces_ignore_scripts(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        npm = bindir / "npm"
        npm.write_text("#!/bin/sh\necho \"args: $@\"\n",
                       encoding="utf-8")
        npm.chmod(0o755)
        profile = {"steps": [{"argv": ["npm", "install"],
                               "install": True, "timeout_s": 60}],
                   "toolchain": [str(bindir)]}
        summary = fastcheck.run("op-npm", str(self.worktree), profile,
                                home=self.home,
                                _disk_avail=self.big_disk)
        self.assertTrue(summary["ok"])
        log = Path(summary["steps"][0]["log"]).read_text(
            encoding="utf-8")
        self.assertIn("--ignore-scripts", log)


HAVE_GIT = shutil.which("git") is not None
MDNS_SOCK = "/private/var/run/mDNSResponder"


class SandboxHardeningTests(FastcheckFixture):
    """S13: unix IPC passes while TCP/DNS fail (F1); the git
    common dir is write-denied except the worktree's own admin dir
    (F2); _tool_version runs in the worktree (F3);
    COREPACK_ENABLE_DOWNLOAD_PROMPT + stdin DEVNULL (F4)."""

    def step_env(self):
        cache = self.home / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        tmp_home = self.home / "tmp-probe"
        tmp_home.mkdir(parents=True, exist_ok=True)
        return fastcheck.scrub_env([], str(tmp_home), str(cache))

    def run_raw(self, argv, worktree, install=False):
        log = self.home / ("s13-%d.log" % time.time_ns())
        return fastcheck.run_step(argv, str(worktree), self.step_env(),
                                  120, install, log)

    def py(self, code, *args):
        return [sys.executable, "-c", code] + list(args)

    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_unix_socket_passes(self):
        code = ("import socket, sys; a, b = socket.socketpair(); "
                "a.send(b'x'); assert b.recv(1) == b'x'; "
                "s = socket.socket(socket.AF_UNIX); "
                "s.bind(sys.argv[1])")
        rec = self.run_raw(
            self.py(code, str(self.worktree / "t.sock")),
            self.worktree, install=False)
        self.assertEqual(rec["rc"], 0, rec["log_tail"])
        self.assertEqual(rec["sandbox_profile"], "files+network")

    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_tcp_loopback_blocked(self):
        code = ("\n".join([
            "import socket, sys",
            "srv = socket.socket()",
            "try:",
            "    srv.bind(('127.0.0.1', 0))",
            "except OSError:",
            "    sys.exit(3)",
            "srv.listen(1)",
            "port = srv.getsockname()[1]",
            "c = socket.socket()",
            "try:",
            "    c.connect(('127.0.0.1', port))",
            "except OSError:",
            "    sys.exit(3)"]))
        rec = self.run_raw(self.py(code), self.worktree,
                           install=False)
        self.assertEqual(rec["rc"], 3, rec["log_tail"])

    @unittest.skipUnless(HAVE_SANDBOX and os.path.exists(MDNS_SOCK),
                         "needs sandbox-exec + mDNSResponder")
    def test_mdns_socket_blocked(self):
        code = ("\n".join([
            "import socket, sys",
            "s = socket.socket(socket.AF_UNIX)",
            "try:",
            "    s.connect(%r)" % MDNS_SOCK,
            "except OSError:",
            "    sys.exit(5)"]))
        rec = self.run_raw(self.py(code), self.worktree,
                           install=False)
        self.assertEqual(rec["rc"], 5, rec["log_tail"])

    def make_linked(self):
        main = self.root / "gmain"
        main.mkdir()
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
        for args in (["git", "init", "-q", "."],
                     ["git", "config", "user.email", "t@t"],
                     ["git", "config", "user.name", "t"],
                     ["git", "config", "commit.gpgsign", "false"]):
            proc = subprocess.run(args, cwd=str(main), env=env,
                                  capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, args)
        (main / "f.txt").write_text("hi\n", encoding="utf-8")
        for args in (["git", "add", "f.txt"],
                     ["git", "commit", "-qm", "init"],
                     ["git", "worktree", "add", "--detach",
                      str(self.root / "glinked")]):
            proc = subprocess.run(args, cwd=str(main), env=env,
                                  capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, args)
        return main, self.root / "glinked"

    @unittest.skipUnless(HAVE_SANDBOX and HAVE_GIT,
                         "needs sandbox-exec + git")
    def test_common_config_write_denied(self):
        main, linked = self.make_linked()
        cfg = main / ".git" / "config"
        before = cfg.read_bytes()
        # Install steps are sandboxed too (write-only profile, F2).
        rec = self.run_raw(["git", "config", "core.hooksPath", "zzz"],
                           linked, install=True)
        self.assertNotEqual(rec["rc"], 0, rec["log_tail"])
        self.assertEqual(rec["sandbox_profile"], "files")
        self.assertEqual(cfg.read_bytes(), before)

    @unittest.skipUnless(HAVE_SANDBOX and HAVE_GIT,
                         "needs sandbox-exec + git")
    def test_git_status_works(self):
        _main, linked = self.make_linked()
        rec = self.run_raw(["git", "status", "--porcelain"], linked,
                           install=False)
        self.assertEqual(rec["rc"], 0, rec["log_tail"])

    @unittest.skipUnless(HAVE_SANDBOX and HAVE_GIT,
                         "needs sandbox-exec + git")
    def test_worktree_write_passes(self):
        _main, linked = self.make_linked()
        code = "open('s13-new.txt', 'w').write('x\\n')"
        rec = self.run_raw(self.py(code), linked, install=False)
        self.assertEqual(rec["rc"], 0, rec["log_tail"])
        self.assertTrue((linked / "s13-new.txt").is_file())

    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_tool_version_runs_in_worktree(self):
        bindir = self.root / "vbin"
        bindir.mkdir()
        showver = bindir / "showver"
        showver.write_text("#!/bin/sh\npwd\n", encoding="utf-8")
        showver.chmod(0o755)
        summary = fastcheck.run(
            "op-s13v", str(self.worktree),
            {"steps": [{"argv": ["showver"], "install": True,
                        "timeout_s": 120}],
             "toolchain": [str(bindir)]},
            home=self.home, _disk_avail=self.big_disk)
        self.assertTrue(summary["ok"])
        # _tool_version keeps the first 64 chars; pwd reports the
        # resolved path (/private/var/…) while the fixture holds the
        # unresolved one, so compare the resolved truncation.
        self.assertEqual(summary["manager_version"],
                         os.path.realpath(str(self.worktree))[:64])

    def test_corepack_download_prompt_off(self):
        env = fastcheck.scrub_env([], str(self.home), str(self.home))
        self.assertEqual(env.get("COREPACK_ENABLE_DOWNLOAD_PROMPT"),
                         "0")

    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_stdin_is_devnull(self):
        code = ("import sys; data = sys.stdin.read(); "
                "assert data == '', repr(data)")
        rec = self.run_raw(self.py(code), self.worktree,
                           install=False)
        self.assertEqual(rec["rc"], 0, rec["log_tail"])


class GuardTests(FastcheckFixture):
    def test_disk_guard_blocks_with_numbers(self):
        with self.assertRaises(fastcheck.FastcheckBlocked) as ctx:
            self.fcrun(_disk_avail=lambda path: 29 * 1024 ** 3)
        self.assertIn("29", str(ctx.exception))
        self.assertIn("30GB", str(ctx.exception))

    def test_slot_busy_skips(self):
        taken = fastcheck._slot_take(self.home)
        try:
            with self.assertRaises(fastcheck.FastcheckSkip):
                self.fcrun(_disk_avail=self.big_disk)
        finally:
            taken.close()

    def test_missing_steps_blocked(self):
        with self.assertRaises(fastcheck.FastcheckBlocked) as ctx:
            fastcheck.run("op-x", str(self.worktree), {},
                          home=self.home,
                          _disk_avail=self.big_disk)
        self.assertIn("fastcheck_steps 없음", str(ctx.exception))

    def test_fastcheck_never_takes_global_lease(self):
        calls = []
        real_acquire = sched.acquire_worker_lease
        real_release = sched.release_worker_lease
        sched.acquire_worker_lease = lambda *a, **k: (
            calls.append("acquire"), None)[1]
        sched.release_worker_lease = lambda *a, **k: calls.append(
            "release")
        try:
            summary = self.fcrun(_disk_avail=self.big_disk)
        finally:
            sched.acquire_worker_lease = real_acquire
            sched.release_worker_lease = real_release
        self.assertTrue(summary["ok"])
        self.assertEqual(calls, [])


class ReceiptTests(FastcheckFixture):
    def write_op(self, op="op-fc1"):
        ops = self.home / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        rec = {"op": "prepare", "op_id": op,
               "gates": [{"key": "source-prepared", "status": "pass",
                          "note": ""},
                         {"key": "fastchecks", "status": "pending",
                          "note": ""}]}
        write_json(ops / (op + ".json"), rec)

    def test_attestation_and_versions(self):
        self.write_op()
        summary = self.fcrun(steps=[self.py_step("pass")],
                           _disk_avail=self.big_disk)
        self.assertTrue(summary["worker_attested"])
        self.assertFalse(summary["root_attested"])
        self.assertEqual(summary["attestor"], "worker")
        self.assertIn("node_version", summary)
        self.assertTrue(
            summary["manager_version"].startswith("Python"))
        self.assertIn(str(self.root / "tools"), summary["toolchain"])
        self.assertTrue(summary["cache"]["regenerable"])
        self.assertIn("enforced", summary["cache"]["checksums"])
        # H2: the disk measure itself is recorded, not just the GB.
        self.assertEqual(summary["disk_measure"],
                         "statvfs f_bavail (conservative)")

    def test_h1_gate_carries_attestor_and_receipt_sha(self):
        # H1: the op's fastchecks gate says who attested plus the
        # receipt sha, so the batch builder can bind them.
        import hashlib
        self.write_op()
        summary = self.fcrun(_disk_avail=self.big_disk)
        self.assertTrue(summary["ok"])
        raw = fastcheck.op_fastcheck_path(
            "op-fc1", self.home).read_bytes()
        rec = json.loads((self.home / "integration_ops" /
                          "op-fc1.json").read_text(encoding="utf-8"))
        gate = [g for g in rec["gates"] if g["key"] == "fastchecks"][0]
        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["attested_by"], "worker")
        self.assertEqual(gate["receipt_sha256"],
                         hashlib.sha256(raw).hexdigest())

    def test_gate_marked_pass_and_receipt_kept_after_cleanup(self):
        self.write_op()
        nm = self.worktree / "node_modules" / "fake-pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("x" * 1500, encoding="utf-8")
        summary = self.fcrun(_disk_avail=self.big_disk)
        self.assertTrue(summary["ok"])
        self.assertGreater(summary["cleanup"]["freed_bytes"], 1000)
        self.assertFalse((self.worktree / "node_modules").exists())
        # Receipts, logs and the op record survive cleanup.
        self.assertTrue(fastcheck.op_fastcheck_path(
            "op-fc1", self.home).is_file())
        self.assertTrue(Path(summary["steps"][0]["log"]).is_file())
        rec = json.loads((self.home / "integration_ops" /
                          "op-fc1.json").read_text(encoding="utf-8"))
        gates = {g["key"]: g["status"] for g in rec["gates"]}
        self.assertEqual(gates["fastchecks"], "pass")
        self.assertEqual(gates["source-prepared"], "pass")

    def test_red_step_blocks_with_log_tail(self):
        self.write_op()
        summary = self.fcrun(
            steps=[self.py_step("import sys; print('boom-tail'); "
                                "sys.exit(3)")],
            _disk_avail=self.big_disk)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["failed"]["rc"], 3)
        self.assertIn("boom-tail", summary["failed"]["log_tail"])
        rec = json.loads((self.home / "integration_ops" /
                          "op-fc1.json").read_text(encoding="utf-8"))
        gates = {g["key"]: g["status"] for g in rec["gates"]}
        self.assertEqual(gates["fastchecks"], "fail")

    def test_no_push_invariant_on_runner_path(self):
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "fastcheck.py").read_text(encoding="utf-8")
        for token in ("github.com", "urllib", "socket", "worktree add",
                      " gh ", "push"):
            self.assertNotIn(token, src,
                             "runner path must stay offline: %s" % token)

    def test_lock_held_during_run(self):
        # R5 live: a concurrent prepare-retry/reresolve on this op
        # gets OpBusy while the fastcheck holds the exclusion lock.
        from openmainty import m2 as _m2
        probe = self.py_step("import time; time.sleep(4)")
        holder = {}

        def run_in_thread():
            holder["summary"] = self.fcrun(
                op="op-locklive", steps=[probe],
                _disk_avail=self.big_disk)

        rt = threading.Thread(target=run_in_thread, daemon=True)
        rt.start()
        time.sleep(2)
        with self.assertRaises(_m2.OpBusy):
            with _m2.op_locked("op-locklive", self.home):
                pass
        rt.join(120)
        self.assertTrue(holder["summary"]["ok"])


class ExecutedCensusTests(FastcheckFixture):
    # FIX-a2 (Codex F14): the exercised census must come from
    # EXECUTED step receipts only. lint fails before unit here, so
    # the planned jest step (whose script would read "direct")
    # never ran — the census must say "not-run", not "direct".

    def test_failed_lint_leaves_planned_unit_not_run(self):
        import json as _json
        (self.worktree / "node_modules" / "eslint").mkdir(
            parents=True, exist_ok=True)
        (self.worktree / "node_modules" / "eslint" /
         "package.json").write_text(
            _json.dumps({"name": "eslint",
                         "bin": {"eslint": "bin/eslint.js"}}),
            encoding="utf-8")
        (self.worktree / "package.json").write_text(
            _json.dumps(
                {"devDependencies": {"eslint": "^9.0.0"},
                 "scripts": {"lint": "eslint .",
                             "test": "jest eslint"}}),
            encoding="utf-8")
        ops = self.home / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        (ops / "op-a2.json").write_text(
            _json.dumps({"op": "prepare", "op_id": "op-a2",
                         "dep": "eslint",
                         "gates": [{"key": "fastchecks",
                                    "status": "pending", "note": ""}]}),
            encoding="utf-8")
        steps = [self.py_step("pass", install=True),
                 {"argv": [sys.executable, "-c",
                           "raise SystemExit(1)"],
                  "gate": "lint", "timeout_s": 120},
                 {"argv": ["yarn", "test"], "gate": "unit",
                  "timeout_s": 120}]
        summary = self.fcrun(op="op-a2", steps=steps,
                             _disk_avail=self.big_disk)
        self.assertFalse(summary["ok"])
        self.assertEqual(len(summary["steps"]), 2)
        self.assertEqual(summary["exercised"]["status"], "not-run")
        self.assertEqual(summary["exercised"]["direct_steps"], [])


class CiConsumerTests(unittest.TestCase):
    # M2.8a1: the census also reads tracked CI workflow files and
    # root tool configs. turbo IS run by ci-web.yml/ci-android.yml
    # (plus turbo.json), although no local gate step runs it — the
    # status stays not-run but ci_consumers names the files.

    def _tree(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wt = Path(tmp.name) / "wt"
        wt.mkdir()
        (wt / "package.json").write_text(
            json.dumps({"devDependencies": {"turbo": "^2.10.8"},
                        "scripts": {"test": "jest"}}),
            encoding="utf-8")
        (wt / "turbo.json").write_text("{}", encoding="utf-8")
        nm = wt / "node_modules" / "turbo"
        nm.mkdir(parents=True, exist_ok=True)
        (nm / "package.json").write_text(
            json.dumps({"name": "turbo",
                        "bin": {"turbo": "./bin/turbo"}}),
            encoding="utf-8")
        return wt

    def _wf(self, wt, name, text):
        d = wt / ".github" / "workflows"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text, encoding="utf-8")

    def _unit_step(self):
        return {"argv": ["yarn", "test"], "gate": "unit",
                "name": "unit", "rc": 0}

    def test_turbo_shape_lists_files_and_stays_not_run(self):
        wt = self._tree()
        self._wf(wt, "ci-web.yml",
                 "jobs:\n  build:\n    steps:\n"
                 "      - run: yarn turbo run build\n")
        self._wf(wt, "ci-android.yml",
                 "jobs:\n  build:\n    steps:\n"
                 "      - run: yarn turbo run build:android\n")
        out = fastcheck.check_exercised(str(wt), "turbo",
                                        [self._unit_step()])
        self.assertEqual(out["status"], "not-run")
        self.assertEqual(out["ci_consumers"],
                         ["ci-android.yml", "ci-web.yml",
                          "turbo.json"])

    def test_comment_or_url_only_is_not_listed(self):
        wt = self._tree()
        (wt / "turbo.json").unlink()
        self._wf(wt, "ci-web.yml",
                 "# turbo cache docs: https://turbo.build\n"
                 "jobs:\n  build:\n    steps:\n"
                 "      - run: yarn jest\n")
        out = fastcheck.check_exercised(str(wt), "turbo",
                                        [self._unit_step()])
        self.assertEqual(out["status"], "not-run")
        self.assertEqual(out["ci_consumers"], [])

    def test_no_github_dir_leaves_output_unchanged(self):
        wt = self._tree()
        (wt / "turbo.json").unlink()
        out = fastcheck.check_exercised(str(wt), "turbo",
                                        [self._unit_step()])
        self.assertEqual(out["status"], "not-run")
        self.assertEqual(out["ci_consumers"], [])

    def test_untracked_workflow_is_not_listed(self):
        import subprocess as _sp
        wt = self._tree()
        self._wf(wt, "ci-web.yml",
                 "jobs:\n  build:\n    steps:\n"
                 "      - run: yarn turbo run build\n")
        (wt / "turbo.json").unlink()
        _sp.run(["git", "init", "-q"], cwd=str(wt), check=True)
        _sp.run(["git", "config", "user.email", "t@t"],
                cwd=str(wt), check=True)
        _sp.run(["git", "config", "user.name", "t"],
                cwd=str(wt), check=True)
        _sp.run(["git", "add", "package.json"], cwd=str(wt),
                check=True)
        _sp.run(["git", "commit", "-qm", "base"], cwd=str(wt),
                check=True)
        # The workflow is present on disk but never tracked.
        out = fastcheck.check_exercised(str(wt), "turbo",
                                        [self._unit_step()])
        self.assertEqual(out["ci_consumers"], [])


class TrackedFilesNoHelperTests(unittest.TestCase):
    # M2.8b2: the tracked-files probe must not run any configured
    # helper program — fsmonitor and hooks stay off on every call.

    def test_ls_files_argv_disables_helpers(self):
        import subprocess as _sp
        from unittest import mock as _mock
        seen = {}
        real_run = _sp.run

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            # 301: the hardened shape carries several -c K=V pairs
            # (fsmonitor/hooks off plus driver/pager/ssh pins);
            # strip every pair to prove the bare command underneath
            # still lists the tree.
            rest = []
            skip = False
            for a in argv:
                if skip:
                    skip = False
                    continue
                if a == "-c":
                    skip = True
                    continue
                rest.append(a)
            return real_run(rest, **kw)

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wt = Path(tmp.name) / "wt"
        wt.mkdir()
        (wt / "a.txt").write_text("a\n", encoding="utf-8")
        _sp.run(["git", "init", "-q"], cwd=str(wt), check=True)
        _sp.run(["git", "config", "user.email", "t@t"],
                cwd=str(wt), check=True)
        _sp.run(["git", "config", "user.name", "t"],
                cwd=str(wt), check=True)
        _sp.run(["git", "add", "."], cwd=str(wt), check=True)
        _sp.run(["git", "commit", "-qm", "base"], cwd=str(wt),
                check=True)
        with _mock.patch.object(fastcheck.subprocess, "run",
                                side_effect=fake_run):
            out = fastcheck._git_tracked_files(str(wt))
        argv = seen.get("argv") or []
        self.assertIn("core.fsmonitor=false", argv)
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertIn("a.txt", out or set())


class ProbeCwdTests(unittest.TestCase):
    """S16: the probe runs --version in the registered checkout."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))

    def make_tool(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        record = self.root / "probe-pwd.txt"
        tool = bindir / "fakey"
        tool.write_text("#!/bin/sh\n"
                        f'echo "$PWD" > "{record}"\n'
                        'echo "fakey 9.9"\n', encoding="utf-8")
        os.chmod(tool, 0o755)
        return str(bindir), record

    def test_probe_runs_in_checkout_read_only(self):
        bindir, record = self.make_tool()
        co = self.root / "checkout"
        co.mkdir()
        sentinel = co / "sentinel.txt"
        sentinel.write_text("untouched\n", encoding="utf-8")
        before = sentinel.read_bytes()
        profile = {"toolchain": [bindir],
                   "steps": [{"argv": ["fakey"], "gate": "unit"}]}
        out = fastcheck.probe_profile_tools(profile, cwd=str(co))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["ok"], out)
        self.assertEqual(out[0]["version"], "fakey 9.9")
        self.assertEqual(out[0]["cwd"], str(co))
        # macOS points /tmp at /private/tmp: compare resolved paths.
        self.assertEqual(os.path.realpath(record.read_text().strip()),
                         os.path.realpath(str(co)))
        self.assertEqual(sentinel.read_bytes(), before)

    def test_probe_without_cwd_reports_empty(self):
        bindir, _record = self.make_tool()
        profile = {"toolchain": [bindir],
                   "steps": [{"argv": ["fakey"], "gate": "unit"}]}
        out = fastcheck.probe_profile_tools(profile)
        self.assertTrue(out[0]["ok"], out)
        self.assertEqual(out[0]["cwd"], "")

    def test_probe_missing_cwd_falls_back(self):
        bindir, _record = self.make_tool()
        profile = {"toolchain": [bindir],
                   "steps": [{"argv": ["fakey"], "gate": "unit"}]}
        out = fastcheck.probe_profile_tools(
            profile, cwd=str(self.root / "gone"))
        self.assertTrue(out[0]["ok"], out)
        self.assertEqual(out[0]["cwd"], "")


class S18ReceiptWordingTests(FastcheckFixture):
    """S18d: origin stamp, network_sandbox rename (+compat), and the
    compact op-record summary the UI renders honestly."""

    def write_op(self, op="op-s18"):
        ops = self.home / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        rec = {"op": "prepare", "op_id": op,
               "gates": [{"key": "source-prepared", "status": "pass",
                          "note": ""},
                         {"key": "fastchecks", "status": "pending",
                          "note": ""}]}
        write_json(ops / (op + ".json"), rec)

    def test_origin_defaults_none_driver(self):
        self.write_op()
        summary = self.fcrun(op="op-s18", _disk_avail=self.big_disk)
        self.assertTrue(summary["ok"])
        self.assertIsNone(summary.get("origin"))

    def test_origin_ui_recorded_never_root_attested(self):
        self.write_op()
        summary = self.fcrun(op="op-s18", _disk_avail=self.big_disk,
                             origin="ui")
        self.assertEqual(summary["origin"], "ui")
        self.assertTrue(summary["worker_attested"])
        self.assertFalse(summary["root_attested"])
        self.assertEqual(summary["attestor"], "worker")

    def test_step_uses_network_sandbox_not_sandbox(self):
        self.write_op()
        summary = self.fcrun(op="op-s18", _disk_avail=self.big_disk)
        step = summary["steps"][0]
        self.assertIn("network_sandbox", step)
        self.assertNotIn("sandbox", step)
        self.assertFalse(step["network_sandbox"])  # install step

    def test_step_name_recorded(self):
        self.write_op()
        summary = self.fcrun(
            op="op-s18",
            steps=[{"argv": [sys.executable, "-c", "pass"],
                    "name": "설치", "install": True, "timeout_s": 60}],
            _disk_avail=self.big_disk)
        self.assertEqual(summary["steps"][0]["name"], "설치")

    def test_compat_alias_reads_old_receipts(self):
        self.assertTrue(
            fastcheck.step_network_sandbox({"sandbox": True}))
        self.assertFalse(
            fastcheck.step_network_sandbox({"sandbox": False}))
        self.assertTrue(
            fastcheck.step_network_sandbox({"network_sandbox": True}))
        self.assertFalse(fastcheck.step_network_sandbox({}))
        self.assertFalse(fastcheck.step_network_sandbox(None))

    def test_op_record_carries_fastcheck_summary(self):
        self.write_op()
        summary = self.fcrun(op="op-s18", _disk_avail=self.big_disk)
        rec = json.loads((self.home / "integration_ops" /
                          "op-s18.json").read_text(encoding="utf-8"))
        compact = rec.get("fastcheck_summary")
        self.assertTrue(compact["ok"])
        self.assertEqual(len(compact["steps"]), 1)
        first = compact["steps"][0]
        self.assertFalse(first["install"] is None)
        self.assertIn("network_sandbox", first)
        self.assertEqual(first["rc"], 0)


class LoopbackSandboxTests(FastcheckFixture):
    """M3.3a: `flutter test` binds a loopback HttpServer, so it gets
    the loopback-only profile (remote TCP and DNS stay denied).
    Every other argv falls back to strict. Offline: the profile
    denials fail fast inside sandbox-exec; skipped without it."""

    def profile_shim(self):
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        out = self.root / "profile.txt"
        shim = bindir / "sbexec"
        shim.write_text("#!/bin/sh\nprintf '%s' \"$2\" > '" + str(out)
                        + "'\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
        return str(shim), out

    def step_env(self):
        tmp_home = self.root / "tmphome"
        tmp_home.mkdir(exist_ok=True)
        cache = self.root / "cache"
        cache.mkdir(exist_ok=True)
        return fastcheck.scrub_env([], str(tmp_home), str(cache))

    def test_flutter_test_gets_loopback(self):
        shim, out = self.profile_shim()
        receipt = fastcheck.run_step(
            ["flutter", "test"], str(self.worktree), self.step_env(),
            60, False, self.root / "t.log", sandbox_exec=shim,
            loopback=True)
        profile = out.read_text(encoding="utf-8")
        self.assertIn("network-bind", profile)
        self.assertEqual(receipt["network_sandbox"], "loopback-only")
        self.assertEqual(receipt["sandbox_profile"],
                         "files+network-loopback")

    def test_other_argv_falls_back_to_strict(self):
        shim, out = self.profile_shim()
        receipt = fastcheck.run_step(
            [sys.executable, "-c", "pass"], str(self.worktree),
            self.step_env(), 60, False, self.root / "t.log",
            sandbox_exec=shim, loopback=True)
        profile = out.read_text(encoding="utf-8")
        self.assertNotIn("network-bind", profile)
        self.assertEqual(receipt["network_sandbox"], "strict")
        self.assertEqual(receipt["sandbox_profile"], "files+network")

    def test_install_never_gets_loopback(self):
        shim, out = self.profile_shim()
        receipt = fastcheck.run_step(
            ["flutter", "test"], str(self.worktree), self.step_env(),
            60, True, self.root / "t.log", sandbox_exec=shim,
            loopback=True)
        profile = out.read_text(encoding="utf-8")
        self.assertNotIn("deny network*", profile)
        self.assertFalse(receipt["network_sandbox"])

    def test_validate_loopback_only_flutter(self):
        tools = self.root / "tools"
        tools.mkdir(exist_ok=True)
        errors = repos.validate_fastcheck({
            "steps": [{"argv": ["yarn", "test"], "gate": "unit",
                       "install": False, "sandbox": "loopback"}],
            "toolchain": [str(tools)]})
        self.assertTrue(any("loopback" in e for e in errors))
        errors = repos.validate_fastcheck({
            "steps": [{"argv": ["flutter", "test"], "gate": "unit",
                       "install": False, "sandbox": "loopback"}],
            "toolchain": [str(tools)]})
        self.assertEqual(errors, [])

    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_loopback_profile_properties(self):
        def sb(code):
            return subprocess.run(
                [fastcheck.SANDBOX_EXEC, "-p",
                 fastcheck.SANDBOX_LOOPBACK_PROFILE,
                 sys.executable, "-I", "-c", code],
                capture_output=True, text=True, timeout=60)

        loop = sb("import socket;s=socket.socket();"
                  "s.bind(('127.0.0.1',0));s.listen(1);"
                  "c=socket.socket();c.settimeout(5);"
                  "c.connect(s.getsockname());print('LOOPBACK-OK')")
        self.assertIn("LOOPBACK-OK", loop.stdout)
        remote = sb("import socket;s=socket.socket();s.settimeout(8);"
                    "s.connect(('93.184.216.34',80))")
        self.assertNotEqual(remote.returncode, 0)
        dns = sb("import socket;"
                 "print(socket.gethostbyname('pub.dev'))")
        self.assertNotEqual(dns.returncode, 0)
        sock_path = self.home / "loop.sock"
        self.assertFalse(sock_path.exists())
        unix = sb("import socket;s=socket.socket(socket.AF_UNIX);"
                  f"s.bind({str(sock_path)!r});print('UNIX-OK')")
        self.assertIn("UNIX-OK", unix.stdout)


class SummarySandboxModeTests(FastcheckFixture):
    """M3.3b: the compact op-record summary stores the raw sandbox
    mode (False | "strict" | "loopback-only"), so the UI's
    loopback-only branch fires instead of overstating isolation."""

    def write_op(self, op="op-mode"):
        ops = self.home / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        write_json(ops / (op + ".json"),
                   {"op": "prepare", "op_id": op,
                    "gates": [{"key": "fastchecks", "status": "pending",
                             "note": ""}]})
        return op

    def summarize(self, steps, op="op-mode"):
        self.write_op(op)
        fastcheck._update_op_gate(self.home, op, True, "note",
                                  steps=steps)
        rec = json.loads((self.home / "integration_ops" /
                          (op + ".json")).read_text(encoding="utf-8"))
        return rec["fastcheck_summary"]["steps"]

    def test_loopback_summary_raw(self):
        got = self.summarize(
            [{"name": "테스트", "install": False,
              "network_sandbox": "loopback-only", "rc": 0}])
        self.assertEqual(got[0]["network_sandbox"], "loopback-only")
        # The bool helper stays truthy for old callers.
        self.assertTrue(fastcheck.step_network_sandbox(got[0]))

    def test_legacy_true_reads_strict(self):
        got = self.summarize(
            [{"name": "옛 단계", "install": False,
              "network_sandbox": True, "rc": 0}])
        self.assertEqual(got[0]["network_sandbox"], "strict")

    def test_strict_false_missing_garbage(self):
        got = self.summarize(
            [{"name": "a", "install": False,
              "network_sandbox": "strict", "rc": 0},
             {"name": "b", "install": True,
              "network_sandbox": False, "rc": 0},
             {"name": "c", "install": False, "rc": 0},
             {"name": "d", "install": False,
              "network_sandbox": "everything-is-fine", "rc": 0}])
        self.assertEqual(
            [s["network_sandbox"] for s in got],
            ["strict", False, False, False])

    def test_end_to_end_loopback_receipt_to_summary(self):
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        out = self.root / "profile.txt"
        shim = bindir / "sbexec"
        shim.write_text("#!/bin/sh\nprintf '%s' \"$2\" > '" + str(out)
                        + "'\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
        tmp_home = self.root / "tmphome"
        tmp_home.mkdir(exist_ok=True)
        cache = self.root / "cache"
        cache.mkdir(exist_ok=True)
        env = fastcheck.scrub_env([], str(tmp_home), str(cache))
        receipt = fastcheck.run_step(
            ["flutter", "test"], str(self.worktree), env, 60, False,
            self.root / "t.log", sandbox_exec=str(shim), loopback=True)
        got = self.summarize([receipt], op="op-e2e")
        self.assertEqual(got[0]["network_sandbox"], "loopback-only")

    def test_fc_isolation_line_shows_loopback(self):
        page = (Path(__file__).resolve().parent.parent / "src" /
                "openmainty" / "dashboard.html").read_text(
                    encoding="utf-8")
        self.assertIn('s.network_sandbox === "loopback-only"', page)
        self.assertIn("로컬 루프백만 허용", page)


class TmpHomeTests(FastcheckFixture):
    """L2: short per-op temp HOME — macOS AF_UNIX paths cap near
    103 bytes and tsx appends tsx-<uid>/<pid>.pipe under TMPDIR."""

    def test_worst_case_pipe_path_fits(self):
        home = fastcheck.op_tmp_home(Path("/tmp") / "x", "o" * 120)
        pipe = str(home) + "/tsx-501/99999.pipe"
        self.assertLess(len(pipe), 104)
        self.assertTrue(home.name.startswith("fc-"))
        self.assertNotIn("o" * 10, home.name)

    def test_op_txt_names_op_mid_run(self):
        code = ("import os; print(open(os.path.join("
                "os.environ['TMPDIR'], 'op.txt')).read())")
        summary = self.fcrun(op="op-tmp1", steps=[self.py_step(code)],
                             _disk_avail=self.big_disk)
        self.assertTrue(summary["ok"])
        log = Path(summary["steps"][0]["log"]).read_text(
            encoding="utf-8")
        self.assertIn("op-tmp1", log)

    def test_guard_refuses_before_running(self):
        deep = self.root / ("d" * 120) / "home"
        with self.assertRaises(fastcheck.FastcheckBlocked) as ctx:
            fastcheck.run("op-guard-1", str(self.worktree),
                          self.profile([self.py_step("pass")]),
                          home=deep, _disk_avail=self.big_disk)
        self.assertIn("소켓", str(ctx.exception))
        self.assertFalse((deep / "integration_ops" /
                          "op-guard-1.fastcheck.json").exists())

    def test_short_root_passes_reaper_validation(self):
        home = fastcheck.op_tmp_home(self.home, "o" * 120)
        home.mkdir(parents=True, exist_ok=True)
        valid, why = fastcheck.validate_reaper_roots(
            [str(home), str(self.worktree)], self.home)
        self.assertEqual(why, "ok")
        self.assertEqual(len(valid), 2)


class PodDriftTests(unittest.TestCase):
    """M2.5c: the native lock drift census over a fixture worktree."""

    PODLOCK = ("PODS:\n"
               '  - RNVVideo (6.19.2):\n'
               "    - RNVVideo/Core (= 6.19.2)\n"
               "\n"
               "DEPENDENCIES:\n"
               "  - RNVVideo\n")

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name) / "wt"
        pkg = self.wt / "node_modules" / "rnv-pkg"
        pkg.mkdir(parents=True)
        (pkg / "RNVVideo.podspec").write_text(
            'Pod::Spec.new do |s|\n  s.name = "RNVVideo"\nend\n',
            encoding="utf-8")
        ios = self.wt / "example" / "ios"
        ios.mkdir(parents=True)
        (ios / "Podfile.lock").write_text(self.PODLOCK,
                                          encoding="utf-8")

    def test_stale_pin_is_labelled(self):
        out = fastcheck.check_pod_drift(str(self.wt), "rnv-pkg",
                                        "6.19.3")
        self.assertIsNotNone(out)
        self.assertEqual(out["pod"], "RNVVideo")
        self.assertEqual(out["pinned"], "6.19.2")
        cmd = ("cd example/ios && pod update RNVVideo "
               "--no-repo-update")
        self.assertEqual(out["command"], cmd)
        self.assertEqual(
            out["label"],
            "iOS Podfile.lock 갱신 필요 — `" + cmd + "`")

    def test_uptodate_pin_is_quiet(self):
        out = fastcheck.check_pod_drift(str(self.wt), "rnv-pkg",
                                        "6.19.2")
        self.assertIsNone(out)

    def test_non_native_dep_is_quiet(self):
        plain = self.wt / "node_modules" / "left-pad"
        plain.mkdir(parents=True)
        (plain / "package.json").write_text("{}", encoding="utf-8")
        out = fastcheck.check_pod_drift(str(self.wt), "left-pad",
                                        "1.3.1")
        self.assertIsNone(out)

    def test_fallback_pod_for_cleaned_tree(self):
        # Old receipts: node_modules already cleaned, so the pod
        # name falls back to the package name (best-effort).
        shutil.rmtree(self.wt / "node_modules")
        (self.wt / "example" / "ios" / "Podfile.lock").write_text(
            self.PODLOCK.replace("RNVVideo (6.19.2)",
                                 "rnv-pkg (6.19.2)"),
            encoding="utf-8")
        out = fastcheck.check_pod_drift(str(self.wt), "rnv-pkg",
                                        "6.19.3",
                                        fallback_pod="rnv-pkg")
        self.assertIsNotNone(out)
        self.assertEqual(out["pinned"], "6.19.2")

    def test_garbage_never_raises(self):
        self.assertIsNone(
            fastcheck.check_pod_drift("/nonexistent-wt", None, None))


class PrebuiltPodUpdateTests(unittest.TestCase):
    """M2.16e: when the lock pins RN prebuilt pods, the drift
    label names the exact owner `pod update` command (live
    RN 0.88 joint: `pod install` fails on those bytes)."""

    PODLOCK_ALL = ("PODS:\n"
                   "  - RNVVideo (6.19.2):\n"
                   "    - RNVVideo/Core (= 6.19.2)\n"
                   "  - ReactNativeDependencies (0.86.2)\n"
                   "  - React-Core-prebuilt (0.86.2):\n"
                   "  - hermes-engine (0.86.2):\n"
                   "\n"
                   "DEPENDENCIES:\n"
                   "  - RNVVideo\n")

    PODLOCK_ONE = ("PODS:\n"
                   "  - RNVVideo (6.19.2):\n"
                   "  - React-Core-prebuilt (0.86.2):\n"
                   "\n"
                   "DEPENDENCIES:\n"
                   "  - RNVVideo\n")

    def _tree(self, lock_text=None, name="wt"):
        wt = Path(self.tmp.name) / name
        pkg = wt / "node_modules" / "rnv-pkg"
        pkg.mkdir(parents=True)
        (pkg / "RNVVideo.podspec").write_text(
            'Pod::Spec.new do |s|\n  s.name = "RNVVideo"\nend\n',
            encoding="utf-8")
        if lock_text is not None:
            ios = wt / "example" / "ios"
            ios.mkdir(parents=True)
            (ios / "Podfile.lock").write_text(
                lock_text, encoding="utf-8")
        return wt

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_prebuilt_pins_do_not_hijack_other_pod(self):
        # M2.23: prebuilt pins in the lock do not reroute a
        # non-prebuilt drift — the tree declares no RN version,
        # so there is no RN drift to mix in.
        wt = self._tree(self.PODLOCK_ALL)
        out = fastcheck.check_pod_drift(str(wt), "rnv-pkg", "6.19.3")
        self.assertIsNotNone(out)
        cmd = ("cd example/ios && pod update RNVVideo "
               "--no-repo-update")
        self.assertEqual(out["command"], cmd)
        self.assertEqual(
            out["label"],
            "iOS Podfile.lock 갱신 필요 — `" + cmd + "`")

    def test_single_prebuilt_pin_does_not_hijack(self):
        wt = self._tree(self.PODLOCK_ONE)
        out = fastcheck.check_pod_drift(str(wt), "rnv-pkg", "6.19.3")
        self.assertIsNotNone(out)
        self.assertEqual(
            out["command"],
            "cd example/ios && pod update RNVVideo "
            "--no-repo-update")

    def test_no_prebuilt_names_own_pod(self):
        wt = self._tree(PodDriftTests.PODLOCK)
        out = fastcheck.check_pod_drift(str(wt), "rnv-pkg", "6.19.3")
        self.assertIsNotNone(out)
        cmd = ("cd example/ios && pod update RNVVideo "
               "--no-repo-update")
        self.assertEqual(out["command"], cmd)
        self.assertEqual(
            out["label"],
            "iOS Podfile.lock 갱신 필요 — `" + cmd + "`")

    def test_mixed_names_prebuilt_first_then_pod(self):
        # M2.23: the tree also carries RN prebuilt drift
        # (declared 0.88.0, pins 0.86.2) — one command names the
        # prebuilt pods first, then the drifted pod, with the
        # failure claim kept.
        import json as _json
        wt = self._tree(self.PODLOCK_ALL)
        (wt / "example" / "package.json").write_text(_json.dumps(
            {"dependencies": {"react-native": "0.88.0"}}),
            encoding="utf-8")
        out = fastcheck.check_pod_drift(str(wt), "rnv-pkg", "6.19.3")
        self.assertIsNotNone(out)
        cmd = ("cd example/ios && pod update "
               "ReactNativeDependencies React-Core-prebuilt "
               "hermes-engine RNVVideo --no-repo-update")
        self.assertEqual(out["command"], cmd)
        self.assertEqual(
            out["label"],
            "iOS Podfile.lock 갱신 필요 — `" + cmd +
            "` (pod install만으로는 실패)")

    def test_rn_drift_keeps_exact_prebuilt_text(self):
        # M2.23: a prebuilt-pod drift keeps the exact live joint
        # text, byte for byte.
        import json as _json
        wt = self._tree(self.PODLOCK_ALL)
        (wt / "example" / "package.json").write_text(_json.dumps(
            {"dependencies": {"react-native": "0.88.0"}}),
            encoding="utf-8")
        out = fastcheck.check_pod_drift(
            str(wt), "react-native", "0.88.0",
            fallback_pod="React-Core-prebuilt")
        cmd = ("cd example/ios && pod update "
               "ReactNativeDependencies React-Core-prebuilt "
               "hermes-engine --no-repo-update")
        self.assertEqual(
            out["label"],
            "iOS Podfile.lock 갱신 필요 — `" + cmd +
            "` (pod install만으로는 실패)")

    def test_unreadable_lock_still_names_pod(self):
        wt = self._tree(PodDriftTests.PODLOCK)
        (wt / "ios").mkdir(parents=True)
        (wt / "ios" / "Podfile.lock").write_text(
            "{unparsable", encoding="utf-8")
        out = fastcheck.check_pod_drift(str(wt), "rnv-pkg", "6.19.3")
        self.assertIsNotNone(out)
        self.assertEqual(
            out["command"],
            "cd example/ios && pod update RNVVideo "
            "--no-repo-update")

    def test_helper_never_raises(self):
        self.assertEqual(fastcheck.prebuilt_pod_update(
            "/nonexistent-wt"), "")
        self.assertEqual(fastcheck.prebuilt_pod_update(""), "")
        self.assertEqual(fastcheck.prebuilt_pod_update(None), "")


class MemberPodDriftsTests(unittest.TestCase):
    """M2.22: receipt-time drift over every joint/family member."""

    PODLOCK = ("PODS:\n"
               '  - RNVVideo (6.19.2):\n'
               "\n"
               "DEPENDENCIES:\n"
               "  - RNVVideo\n")

    RNPODLOCK = ("PODS:\n"
                 "  - ReactNativeDependencies (0.86.2)\n"
                 "  - React-Core-prebuilt (0.86.2):\n"
                 "  - hermes-engine (0.86.2):\n"
                 "\n"
                 "DEPENDENCIES:\n"
                 "  - ReactNativeDependencies\n")

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name) / "wt"
        pkg = self.wt / "node_modules" / "rnv-pkg"
        pkg.mkdir(parents=True)
        (pkg / "RNVVideo.podspec").write_text(
            'Pod::Spec.new do |s|\n  s.name = "RNVVideo"\nend\n',
            encoding="utf-8")
        ios = self.wt / "example" / "ios"
        ios.mkdir(parents=True)
        (ios / "Podfile.lock").write_text(self.PODLOCK,
                                          encoding="utf-8")

    def test_joint_member_drift_found(self):
        # The op's own dep (expo) is pod-quiet; the RN/video
        # member is not. First labelled drift wins.
        op_rec = {"dep": "expo", "target": "58.0.19",
                  "joint_candidates": [
                      {"run": "run-x", "dep": "rnv-pkg",
                       "target": "6.19.3"},
                      {"run": "run-y", "dep": "expo",
                       "target": "58.0.19"}]}
        drifts = fastcheck.member_pod_drifts(str(self.wt), op_rec)
        self.assertTrue(drifts)
        self.assertEqual(drifts[0]["pod"], "RNVVideo")
        self.assertEqual(drifts[0]["pinned"], "6.19.2")
        self.assertIn("iOS Podfile.lock", drifts[0]["label"])
        labels = [d["label"] for d in drifts]
        self.assertEqual(len(labels), len(set(labels)))

    def test_in_sync_lock_is_quiet(self):
        (self.wt / "example" / "ios" / "Podfile.lock").write_text(
            self.PODLOCK.replace("6.19.2", "6.19.3"),
            encoding="utf-8")
        op_rec = {"dep": "expo", "target": "58.0.19",
                  "joint_candidates": [
                      {"dep": "rnv-pkg", "target": "6.19.3"}]}
        self.assertEqual(
            fastcheck.member_pod_drifts(str(self.wt), op_rec), [])

    def test_tree_rn_check_catches_prebuilt_drift(self):
        import json as _json
        wt = Path(self.tmp.name) / "rnwt"
        ios = wt / "example" / "ios"
        ios.mkdir(parents=True)
        (ios / "Podfile.lock").write_text(self.RNPODLOCK,
                                          encoding="utf-8")
        (wt / "example" / "package.json").write_text(_json.dumps(
            {"dependencies": {"react-native": "0.88.0"}}),
            encoding="utf-8")
        drifts = fastcheck.member_pod_drifts(
            str(wt), {"dep": "expo", "target": "58.0.19"})
        self.assertTrue(drifts)
        self.assertIn("pod update", drifts[0]["label"])
        self.assertIn("React-Core-prebuilt",
                      drifts[0].get("command") or "")

    def test_tree_rn_check_quiet_when_in_sync(self):
        import json as _json
        wt = Path(self.tmp.name) / "rnwt2"
        ios = wt / "example" / "ios"
        ios.mkdir(parents=True)
        (ios / "Podfile.lock").write_text(self.RNPODLOCK,
                                          encoding="utf-8")
        (wt / "example" / "package.json").write_text(_json.dumps(
            {"dependencies": {"react-native": "0.86.2"}}),
            encoding="utf-8")
        self.assertEqual(fastcheck.member_pod_drifts(
            str(wt), {"dep": "left-pad", "target": "1.0.0"}), [])

    def test_family_members_resolved_from_ops(self):
        home = Path(self.tmp.name) / "home"
        ops = home / "integration_ops"
        ops.mkdir(parents=True)
        (ops / "op-m1.json").write_text(
            '{"run_id": "run-m1", "dep": "rnv-pkg", '
            '"target": "6.19.3"}', encoding="utf-8")
        (ops / "op-m2.json").write_text(
            '{"run_id": "run-m2", "dep": "left-pad", '
            '"target": "1.0.0"}', encoding="utf-8")
        op_rec = {"dep": "rnv-pkg", "target": "6.19.3",
                  "family": {"role": "primary",
                             "members": ["run-m1", "run-m2",
                                         "run-gone"]}}
        drifts = fastcheck.member_pod_drifts(str(self.wt), op_rec,
                                             home)
        self.assertTrue(drifts)
        self.assertEqual(drifts[0]["pod"], "RNVVideo")

    def test_garbage_never_raises(self):
        self.assertEqual(fastcheck.member_pod_drifts(
            "/nonexistent-wt", None, None), [])
        self.assertEqual(fastcheck.member_pod_drifts(
            str(self.wt), {"family": {"members": ["x"]}},
            "/nonexistent-home"), [])


class RollupPodCommandTests(unittest.TestCase):
    """M2.16e2: the rollup-tree command is drift-gated — an
    in-sync lock gives nothing even though it pins the same
    prebuilt pods."""

    PODLOCK = ("PODS:\n"
               "  - ReactNativeDependencies (0.86.2)\n"
               "  - React-Core-prebuilt (0.86.2):\n"
               "  - hermes-engine (0.86.2):\n"
               "\n"
               "DEPENDENCIES:\n"
               "  - ReactNativeDependencies\n")

    PODCMD = ("cd example/ios && pod update "
              "ReactNativeDependencies React-Core-prebuilt "
              "hermes-engine --no-repo-update")

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _tree(self, lock_text=None, rn=None, rn_garbage=False,
              name="rpowt"):
        import json as _json
        wt = Path(self.tmp.name) / name
        if lock_text is not None:
            ios = wt / "example" / "ios"
            ios.mkdir(parents=True)
            (ios / "Podfile.lock").write_text(
                lock_text, encoding="utf-8")
        if rn_garbage:
            ex = wt / "example"
            ex.mkdir(parents=True, exist_ok=True)
            (ex / "package.json").write_text(
                "{unparsable", encoding="utf-8")
        elif rn is not None:
            ex = wt / "example"
            ex.mkdir(parents=True, exist_ok=True)
            (ex / "package.json").write_text(_json.dumps(
                {"dependencies": {"react-native": rn}}),
                encoding="utf-8")
        return wt

    def test_exact_version_read(self):
        wt = self._tree(rn="0.88.0")
        self.assertEqual(
            fastcheck.tree_react_native_version(str(wt)),
            "0.88.0")

    def test_drifted_tree_gives_command(self):
        wt = self._tree(self.PODLOCK, rn="0.88.0")
        self.assertEqual(fastcheck.rollup_pod_command(str(wt)),
                         self.PODCMD)

    def test_in_sync_tree_gives_nothing(self):
        wt = self._tree(self.PODLOCK, rn="0.86.2")
        self.assertEqual(fastcheck.rollup_pod_command(str(wt)),
                         "")

    def test_unreadable_manifest_gives_nothing(self):
        wt = self._tree(self.PODLOCK, rn_garbage=True)
        self.assertEqual(fastcheck.rollup_pod_command(str(wt)),
                         "")
        self.assertEqual(
            fastcheck.tree_react_native_version(str(wt)), "")

    def test_helpers_never_raise(self):
        self.assertEqual(fastcheck.rollup_pod_command(
            "/nonexistent-wt"), "")
        self.assertEqual(fastcheck.tree_react_native_version(
            "/nonexistent-wt"), "")
        self.assertEqual(fastcheck.rollup_pod_command(None), "")


class ExercisedTests(unittest.TestCase):
    """M2.5a: the exercised census over a fixture worktree.

    Pure read-only helper — no step runs here. The live shape is
    webpack-dev-server: declared by example/, used only by the
    un-gated `example: web` script, so no gate step executes it.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name) / "wt"
        (self.wt / "example" / "node_modules"
         / "webpack-dev-server").mkdir(parents=True)
        (self.wt / "node_modules" / "eslint").mkdir(parents=True)
        self._write("", "package.json",
                    {"dependencies": {"react": "19.3.1"},
                     "devDependencies": {"eslint": "^9.0.0"},
                     "scripts": {"lint": "eslint .",
                                 "typecheck": "tsc --noEmit"}})
        self._write("example", "package.json",
                    {"devDependencies":
                     {"webpack-dev-server": "^6.1.0"},
                     "scripts": {"web": "webpack serve --port 3000"}})
        self._write("node_modules/eslint", "package.json",
                    {"bin": {"eslint": "bin/eslint.js"}})
        self._write("example/node_modules/webpack-dev-server",
                    "package.json",
                    {"bin": {"webpack-serve": "bin/serve.js",
                             "webpack-dev-server": "bin/cli.js"}})

    def _write(self, rel, name, data):
        import json as _json
        d = self.wt / rel if rel else self.wt
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(_json.dumps(data), encoding="utf-8")

    def test_gate_script_user_is_direct(self):
        out = fastcheck.check_exercised(
            str(self.wt), "eslint",
            [{"argv": ["yarn", "lint"], "gate": "lint"},
             {"argv": ["yarn", "install", "--immutable"],
              "install": True}])
        self.assertEqual(out["status"], "direct")
        self.assertEqual(out["declared_in"], ["root"])
        # M2.5d: steps read as gate + script, never bare argv.
        self.assertEqual(out["direct_steps"], ["lint (lint: eslint)"])

    def test_ungated_workspace_script_is_not_run(self):
        out = fastcheck.check_exercised(
            str(self.wt), "webpack-dev-server",
            [{"argv": ["yarn", "lint"], "gate": "lint"},
             {"argv": ["yarn", "typecheck"], "gate": "typecheck"}])
        self.assertEqual(out["status"], "not-run")
        self.assertEqual(out["declared_in"], ["example"])
        self.assertEqual(out["consumer_scripts"], ["example: web"])
        self.assertEqual(out["direct_steps"], [])

    def test_missing_node_modules_is_unknown(self):
        shutil.rmtree(self.wt / "node_modules")
        shutil.rmtree(self.wt / "example" / "node_modules")
        out = fastcheck.check_exercised(
            str(self.wt), "eslint",
            [{"argv": ["yarn", "lint"], "gate": "lint"}])
        self.assertEqual(out["status"], "unknown")
        self.assertIn("설치", out["reason"])

    def test_undeclared_dep_is_unknown(self):
        out = fastcheck.check_exercised(
            str(self.wt), "left-pad",
            [{"argv": ["yarn", "lint"], "gate": "lint"}])
        self.assertEqual(out["status"], "unknown")

    def test_garbage_never_raises(self):
        out = fastcheck.check_exercised("/nonexistent-wt", None, None)
        self.assertEqual(out["status"], "unknown")


class ViaToolTests(unittest.TestCase):
    """M2.5d: the via-tool census — plugin/config/types packages a
    gate tool loads by convention. Exact dep/bin tokens stay the
    only "direct"; the companion-prefix guess only lists
    consumers."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name) / "wt"
        (self.wt / "node_modules" / "jest").mkdir(parents=True)
        (self.wt / "node_modules" / "eslint-plugin-jest").mkdir(
            parents=True)
        (self.wt / "node_modules" / "@types" / "react").mkdir(
            parents=True)
        (self.wt / "node_modules" / "webpack-dev-server").mkdir(
            parents=True)
        self._write("", "package.json",
                    {"devDependencies":
                     {"jest": "^30.0.0",
                      "eslint-plugin-jest": "^29.17.0",
                      "@types/react": "^19.3.0",
                      "webpack-dev-server": "^6.1.0"},
                     "scripts": {"lint": "eslint .",
                                 "typecheck": "tsc --noEmit",
                                 "test": "jest --ci"}})
        self._write("node_modules/jest", "package.json",
                    {"bin": {"jest": "bin/jest.js"}})
        self._write("node_modules/eslint-plugin-jest", "package.json",
                    {"name": "eslint-plugin-jest",
                     "version": "29.17.0"})
        self._write("node_modules/@types/react", "package.json",
                    {"name": "@types/react", "version": "19.3.0"})
        self._write("node_modules/webpack-dev-server", "package.json",
                    {"bin": {"webpack-dev-server": "bin/cli.js"}})

    def _write(self, rel, name, data):
        import json as _json
        d = self.wt / rel if rel else self.wt
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(_json.dumps(data), encoding="utf-8")

    def test_exact_bin_is_direct_with_gate_label(self):
        out = fastcheck.check_exercised(
            str(self.wt), "jest",
            [{"argv": ["yarn", "test"], "gate": "unit"}])
        self.assertEqual(out["status"], "direct")
        self.assertEqual(out["direct_steps"], ["unit (test: jest)"])
        self.assertEqual(out["via"], {})

    def test_eslint_plugin_rides_lint(self):
        # Live run-18aa3c03: the old prefix rule called this
        # direct; the truth is via-tool on the lint gate.
        out = fastcheck.check_exercised(
            str(self.wt), "eslint-plugin-jest",
            [{"argv": ["yarn", "lint"], "gate": "lint"},
             {"argv": ["yarn", "typecheck"], "gate": "typecheck"}])
        self.assertEqual(out["status"], "via-tool")
        self.assertEqual(out["via"],
                         {"gate": "lint",
                          "label": "lint (lint: eslint)"})
        self.assertEqual(out["direct_steps"], [])

    def test_types_ride_typecheck(self):
        out = fastcheck.check_exercised(
            str(self.wt), "@types/react",
            [{"argv": ["yarn", "typecheck"], "gate": "typecheck"}])
        self.assertEqual(out["status"], "via-tool")
        self.assertEqual(out["via"]["gate"], "typecheck")

    def test_webpack_prefix_never_decides_direct(self):
        # Live run-9173d495: "webpack serve" shares a prefix with
        # webpack-dev-server but never executes it — not-run, with
        # the consumer listed by the prefix rule.
        (self.wt / "example").mkdir(parents=True, exist_ok=True)
        self._write("example", "package.json",
                    {"devDependencies":
                     {"webpack-dev-server": "^6.1.0"},
                     "scripts": {"web": "webpack serve"}})
        out = fastcheck.check_exercised(
            str(self.wt), "webpack-dev-server",
            [{"argv": ["yarn", "lint"], "gate": "lint"}])
        self.assertEqual(out["status"], "not-run")
        self.assertIn("example: web", out["consumer_scripts"])

    def test_prettier_plugin_needs_prettier_in_script(self):
        (self.wt / "node_modules" / "prettier-plugin-x").mkdir(
            parents=True)
        self._write("node_modules/prettier-plugin-x", "package.json",
                    {"name": "prettier-plugin-x"})
        pkg = self._read_root()
        pkg["devDependencies"]["prettier-plugin-x"] = "^1.0.0"
        self._write("", "package.json", pkg)
        plain = fastcheck.check_exercised(
            str(self.wt), "prettier-plugin-x",
            [{"argv": ["yarn", "lint"], "gate": "lint"}])
        self.assertEqual(plain["status"], "not-run")
        pkg["scripts"]["lint"] = "eslint . && prettier --check ."
        self._write("", "package.json", pkg)
        rode = fastcheck.check_exercised(
            str(self.wt), "prettier-plugin-x",
            [{"argv": ["yarn", "lint"], "gate": "lint"}])
        self.assertEqual(rode["status"], "via-tool")
        self.assertEqual(rode["via"]["gate"], "lint")

    def _read_root(self):
        import json as _json
        return _json.loads(
            (self.wt / "package.json").read_text(encoding="utf-8"))


class WorkspaceOnlyCensusTests(unittest.TestCase):
    """M2.16a: a census rule credits a step only when that step
    resolves the bumped tree. Live run-160ec8d7: the candidate
    touched only example/package.json (@babel/runtime ^7 -> ^8)
    but fastcheck credited ROOT prepare (bob build), which
    resolves the root 7.x tree — the example 8.x was never
    exercised."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name) / "wt"
        (self.wt / "node_modules" / "@babel" / "runtime").mkdir(
            parents=True)
        self._write("", "package.json",
                    {"devDependencies": {"@babel/core": "^7.29.7"},
                     "scripts": {"build": "bob build",
                                 "typecheck": "tsc --noEmit"}})
        self._write("example", "package.json",
                    {"devDependencies": {"@babel/runtime": "^8.0.8"},
                     "scripts": {"build:web":
                                 "webpack build --runtime "
                                 "@babel/runtime"}})
        self._write("node_modules/@babel/runtime", "package.json",
                    {"name": "@babel/runtime", "version": "8.0.8"})

    def _write(self, rel, name, data):
        import json as _json
        d = self.wt / rel if rel else self.wt
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(_json.dumps(data), encoding="utf-8")

    def test_workspace_only_bump_root_prepare_is_not_run(self):
        out = fastcheck.check_exercised(
            str(self.wt), "@babel/runtime",
            [{"argv": ["yarn", "build"], "gate": "prepare"}])
        self.assertEqual(out["status"], "not-run")
        self.assertEqual(out["via"], {})
        self.assertEqual(out["direct_steps"], [])
        self.assertIn("example: build:web",
                      out["consumer_scripts"])
        picked = fastcheck.pick_consumer_script(
            str(self.wt), out["consumer_scripts"])
        self.assertEqual(picked,
                         {"rel": "example", "name": "build:web",
                          "label": "example: build:web"})

    def test_workspace_step_for_workspace_bump_still_credits(self):
        # The rule is about tree resolution, not a blanket
        # denial: a step resolving the declaring workspace
        # (example) still credits the dep — here exact-token
        # direct through example's own script.
        out = fastcheck.check_exercised(
            str(self.wt), "@babel/runtime",
            [{"argv": ["yarn", "workspace", "example", "build:web"],
              "gate": "prepare"}])
        self.assertEqual(out["status"], "direct")
        self.assertEqual(out["direct_steps"],
                         ["prepare (build:web: webpack)"])

    def test_workspace_build_listed_without_mention(self):
        # Live run-160ec8d7 shape: example's build:web never
        # names @babel/runtime, yet it is the bumped tree's
        # one-shot build — listed and picked for the consumer
        # gate all the same. android/ios/start stay out.
        import json as _json
        pkg = _json.loads(
            (self.wt / "example" / "package.json").read_text(
                encoding="utf-8"))
        pkg["scripts"] = {"build:web": "webpack --mode production",
                          "start": "react-native start",
                          "build:android": "react-native build-android"}
        (self.wt / "example" / "package.json").write_text(
            _json.dumps(pkg), encoding="utf-8")
        out = fastcheck.check_exercised(
            str(self.wt), "@babel/runtime",
            [{"argv": ["yarn", "build"], "gate": "prepare"}])
        self.assertEqual(out["status"], "not-run")
        self.assertEqual(out["consumer_scripts"],
                         ["example: build:web"])
        self.assertEqual(
            fastcheck.pick_consumer_script(
                str(self.wt), out["consumer_scripts"]),
            {"rel": "example", "name": "build:web",
             "label": "example: build:web"})

    def test_root_manifest_bump_keeps_today_crediting(self):
        import json as _json
        pkg = _json.loads(
            (self.wt / "package.json").read_text(encoding="utf-8"))
        pkg["devDependencies"]["@babel/runtime"] = "^7.29.7"
        (self.wt / "package.json").write_text(
            _json.dumps(pkg), encoding="utf-8")
        out = fastcheck.check_exercised(
            str(self.wt), "@babel/runtime",
            [{"argv": ["yarn", "build"], "gate": "prepare"}])
        self.assertEqual(out["status"], "via-tool")
        self.assertEqual(out["via"]["gate"], "prepare")


class WriteContainmentTests(FastcheckFixture):
    """M2.7-SEC F2: deny-all file-write* plus an explicit allowlist.

    A sibling dir stands in for the owner checkout: writes there
    fail, while writes in the worktree and TMPDIR succeed. The
    allowlist is recorded in the receipt.
    """

    def step_env(self):
        cache = self.home / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        tmp_home = self.home / "tmp-probe"
        tmp_home.mkdir(parents=True, exist_ok=True)
        return fastcheck.scrub_env([], str(tmp_home), str(cache))

    def run_raw(self, argv, worktree, install=False):
        log = self.home / ("f2-%d.log" % time.time_ns())
        return fastcheck.run_step(argv, str(worktree), self.step_env(),
                                  120, install, log)

    def py(self, code, *args):
        return [sys.executable, "-c", code] + list(args)

    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_sibling_checkout_write_denied(self):
        sibling = self.root / "owner-checkout"
        sibling.mkdir()
        code = ("import sys; open(sys.argv[1], 'w').write('evil\\n')")
        rec = self.run_raw(
            self.py(code, str(sibling / "evil.txt")),
            self.worktree, install=False)
        self.assertNotEqual(rec["rc"], 0, rec["log_tail"])
        self.assertFalse((sibling / "evil.txt").exists())

    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_worktree_and_tmpdir_writes_pass(self):
        env = self.step_env()
        tmp_file = os.path.join(env["TMPDIR"], "f2-ok.txt")
        code = ("import sys; open(sys.argv[1], 'w').write('a\\n'); "
                "open(sys.argv[2], 'w').write('b\\n')")
        rec = self.run_raw(
            self.py(code, str(self.worktree / "ok.txt"), tmp_file),
            self.worktree, install=False)
        self.assertEqual(rec["rc"], 0, rec["log_tail"])
        self.assertTrue((self.worktree / "ok.txt").is_file())
        self.assertTrue(Path(tmp_file).is_file())

    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_allowlist_recorded(self):
        rec = self.run_raw(self.py("pass"), self.worktree,
                           install=False)
        self.assertEqual(rec["rc"], 0, rec["log_tail"])
        allow = rec.get("write_allow") or []
        tree = os.path.realpath(str(self.worktree))
        self.assertIn(tree, allow)
        self.assertTrue(all(os.path.isabs(p) for p in allow))

    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_install_step_contained_too(self):
        sibling = self.root / "owner-checkout"
        sibling.mkdir()
        code = ("import sys; open(sys.argv[1], 'w').write('evil\\n')")
        rec = self.run_raw(
            self.py(code, str(sibling / "evil.txt")),
            self.worktree, install=True)
        self.assertNotEqual(rec["rc"], 0, rec["log_tail"])
        self.assertFalse((sibling / "evil.txt").exists())


class FlutterSdkCacheTests(FastcheckFixture):
    # M2.7-SEC2: a toolchain Flutter SDK contributes ONLY its
    # bin/cache — writes there pass (stamps, lockfiles), writes in
    # packages are denied, and no toolchain means no SDK path.
    def fake_sdk(self):
        sdk_bin = self.root / "sdk" / "bin"
        (sdk_bin / "cache").mkdir(parents=True)
        exe = sdk_bin / "flutter"
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        exe.chmod(0o755)
        (sdk_bin.parent / "packages").mkdir(parents=True)
        return sdk_bin

    def allow(self, toolchain):
        rules, _note, paths = fastcheck._write_allowlist(
            str(self.worktree), {}, toolchain)
        return rules, paths

    def step_env(self):
        cache = self.home / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        tmp_home = self.home / "tmp-probe"
        tmp_home.mkdir(parents=True, exist_ok=True)
        return fastcheck.scrub_env([], str(tmp_home), str(cache))

    def test_sdk_cache_allowed_packages_denied(self):
        sdk_bin = self.fake_sdk()
        _rules, paths = self.allow([str(sdk_bin)])
        cache = os.path.realpath(str(sdk_bin / "cache"))
        sdk_root = os.path.realpath(str(sdk_bin.parent))
        self.assertIn(cache, paths)
        for p in paths:
            if p == cache:
                continue
            self.assertFalse(
                p == sdk_root or p.startswith(sdk_root + os.sep),
                p)

    def test_no_toolchain_no_sdk_paths(self):
        sdk_bin = self.fake_sdk()
        _rules, paths = self.allow([])
        sdk_root = os.path.realpath(str(sdk_bin.parent))
        for p in paths:
            self.assertFalse(
                p == sdk_root or p.startswith(sdk_root + os.sep),
                p)

    @unittest.skipUnless(HAVE_SANDBOX, "needs /usr/bin/sandbox-exec")
    def test_sdk_cache_write_passes_packages_denied(self):
        sdk_bin = self.fake_sdk()
        tool = [str(sdk_bin)]
        log = self.home / ("f2sdk-%d.log" % time.time_ns())
        env = self.step_env()
        ok_file = sdk_bin / "cache" / "engine.stamp.tmp.test"
        code = ("import sys; open(sys.argv[1], 'w').write('s\\n')")
        rec = fastcheck.run_step(
            [sys.executable, "-c", code, str(ok_file)],
            str(self.worktree), env, 120, True, log,
            toolchain=tool)
        self.assertEqual(rec["rc"], 0, rec["log_tail"])
        self.assertTrue(ok_file.is_file())
        bad_file = sdk_bin.parent / "packages" / "evil.txt"
        rec2 = fastcheck.run_step(
            [sys.executable, "-c", code, str(bad_file)],
            str(self.worktree), env, 120, True, log,
            toolchain=tool)
        self.assertNotEqual(rec2["rc"], 0, rec2["log_tail"])
        self.assertFalse(bad_file.exists())


if __name__ == "__main__":
    unittest.main()
