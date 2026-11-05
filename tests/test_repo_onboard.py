"""M3.2 onboarding tests: clone hardening, detect, register-first-run.

All offline: the clone runner is injected, fixtures are local dirs,
and no test touches the network. Py3.9 portable.
"""
import json
import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import repos, runner, sched, serve  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

MINUTE = 20


class OnboardFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        use_temp_home(self, str(self.home))
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        for tool in ("yarn", "node", "npm", "flutter"):
            self.make_tool(tool)

    def make_tool(self, name):
        path = self.bindir / name
        path.write_text("#!/bin/sh\necho 9.9.9\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def checkout(self, files):
        co = self.root / "co"
        co.mkdir(exist_ok=True)
        for rel, text in files.items():
            p = co / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return co

    def with_path(self):
        return mock.patch.dict(
            os.environ, {"PATH": str(self.bindir) + os.pathsep +
                         os.environ.get("PATH", "")})


class CloneSafetyTests(OnboardFixture):
    def test_token_url_refused_before_network(self):
        calls = []
        with mock.patch.object(
                serve, "_clone_repo",
                side_effect=lambda *a, **k: calls.append(1)):
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_job_clone(
                    {"github": "https://tok@github.com/o/r"})
        self.assertIn("토큰", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_clone_argv_env_and_stdin(self):
        seen = {}

        def fake(cmd, **kw):
            seen["cmd"] = cmd
            seen.update(kw)
            return 0, "", ""

        dest = self.root / "dest" / "repo"
        serve._clone_repo("https://github.com/o/r.git", dest,
                          _run=fake)
        self.assertIn("--filter=blob:none", seen["cmd"])
        self.assertNotIn("--depth", " ".join(seen["cmd"]))
        self.assertEqual(seen["env"].get("GIT_TERMINAL_PROMPT"), "0")
        self.assertEqual(seen["env"].get("GIT_ASKPASS"),
                         "/usr/bin/false")
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)

    def test_auth_failure_becomes_plain_refusal(self):
        def fake(cmd, **kw):
            return 128, "", ("fatal: Authentication failed for "
                             "'https://github.com/o/r.git/'")

        with self.assertRaises(SystemExit) as ctx:
            serve._clone_repo("https://github.com/o/r.git",
                              self.root / "d1", _run=fake)
        self.assertIn("비공개 저장소는 로컬 체크아웃으로",
                      str(ctx.exception))

    def test_timeout_becomes_plain_refusal(self):
        with self.assertRaises(SystemExit) as ctx:
            serve._clone_repo("https://github.com/o/r.git",
                              self.root / "d2",
                              _run=lambda cmd, **kw: (124, "", ""))
        self.assertIn("시간 초과", str(ctx.exception))

    def test_clone_lands_under_checkouts(self):
        dests = []
        real_clone = serve._clone_repo

        def fake(url, dest, cancel_event=None, _run=None):
            dests.append((url, dest))
            dest.mkdir(parents=True)
            return None

        with mock.patch.object(serve, "_clone_repo", fake):
            with mock.patch.object(serve, "_register",
                                   lambda name, github, dest: {}):
                out = serve.api_job_clone({"github": "o/r"})
        url, dest = dests[0]
        self.assertEqual(url, "https://github.com/o/r.git")
        self.assertEqual(
            dest, serve.paths.home() / "checkouts" / "o" / "r")
        for _ in range(100):
            if serve._JOBS[out["job"]["id"]]["status"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(
            serve._JOBS[out["job"]["id"]]["status"], "done")


class DetectTests(OnboardFixture):
    def valid(self, proposal):
        self.assertNotIn("unsupported", proposal)
        errors = repos.validate_fastcheck(
            {"steps": proposal["steps"],
             "toolchain": proposal["toolchain"]})
        self.assertEqual(errors, [])
        return proposal

    def test_yarn_berry_from_lockfile(self):
        with self.with_path():
            co = self.checkout({
                "package.json": json.dumps(
                    {"scripts": {"build": "x", "lint": "y"}}),
                "yarn.lock": "# yarn lockfile v1\n",
                ".yarnrc.yml": "nodeLinker: node-modules\n"})
            out = self.valid(repos.detect_fastcheck(co))
        self.assertEqual(out["manager"], "yarn-berry")
        self.assertIn("--immutable", out["steps"][0]["argv"])
        names = [s["name"] for s in out["steps"]]
        self.assertIn("빌드", names)
        self.assertNotIn("테스트", names)  # no test script -> absent

    def test_yarn_classic_without_yarnrc(self):
        with self.with_path():
            co = self.checkout({"package.json": "{}",
                                "yarn.lock": "# yarn lockfile v1\n"})
            out = self.valid(repos.detect_fastcheck(co))
        self.assertEqual(out["manager"], "yarn-classic")
        self.assertIn("--frozen-lockfile", out["steps"][0]["argv"])

    def test_npm_from_package_lock(self):
        with self.with_path():
            co = self.checkout({"package.json": json.dumps(
                {"scripts": {"test": "jest"}}),
                "package-lock.json": "{}"})
            out = self.valid(repos.detect_fastcheck(co))
        self.assertEqual(out["manager"], "npm")
        self.assertEqual(out["steps"][0]["argv"], ["npm", "ci"])
        self.assertEqual(out["steps"][1]["argv"], ["npm", "run",
                                                   "test"])

    def test_pnpm_refused_plainly(self):
        co = self.checkout({"package.json": "{}",
                            "pnpm-lock.yaml": "x: 1\n"})
        out = repos.detect_fastcheck(co)
        self.assertIn("unsupported", out)
        self.assertIn("pnpm", out["unsupported"])

    def test_flutter_detected_with_lock(self):
        with self.with_path():
            co = self.checkout({"pubspec.yaml": "name: x\n",
                                "pubspec.lock": "sdks: {}\n"})
            # M3.3a three-way rule: only a TRACKED lock enforces.
            for args in (["init", "-b", "main"],
                         ["config", "user.email", "t@t"],
                         ["config", "user.name", "t"],
                         ["add", "-A"],
                         ["commit", "-qm", "x"]):
                subprocess.run(
                    ["git", "-C", str(co)] + args, check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, timeout=60)
            out = self.valid(repos.detect_fastcheck(co))
        self.assertEqual(out["manager"], "flutter")
        # F3: the lock is read-only — installs can never rewrite it.
        self.assertEqual(out["steps"][0]["argv"],
                         ["flutter", "pub", "get",
                          "--enforce-lockfile"])

    def test_flutter_without_lock_refused(self):
        co = self.checkout({"pubspec.yaml": "name: x\n"})
        out = repos.detect_fastcheck(co)
        self.assertIn("unsupported", out)
        self.assertIn("잠금 파일 없음", out["unsupported"])

    def test_scripts_without_lockfile_refused(self):
        # F2: no lockfile means no reproducible install. The old npm
        # fallback wrote package-lock.json into the worktree (an
        # untracked fingerprint change) while installing nothing.
        co = self.checkout({"package.json": json.dumps(
            {"scripts": {"build": "x", "test": "y"}})})
        out = repos.detect_fastcheck(co)
        self.assertIn("unsupported", out)
        self.assertIn("잠금 파일 없음", out["unsupported"])
        self.assertFalse((co / "package-lock.json").exists())

    def test_install_steps_marked(self):
        with self.with_path():
            co = self.checkout({"package.json": "{}",
                                "yarn.lock": "v1\n"})
            out = self.valid(repos.detect_fastcheck(co))
        self.assertTrue(out["steps"][0]["install"])


class RegisterFirstRunTests(OnboardFixture):
    def test_register_leaves_toggles_off_and_scans_first(self):
        from openmainty import m2 as _m2
        co = self.root / "real"
        co.mkdir()
        self.assertEqual(
            subprocess.run(["git", "init", "-q", str(co)],
                           capture_output=True).returncode, 0)
        out = serve.api_repo_add_local(
            {"path": str(co), "name": "fakerepo", "github": "o/r"})
        self.assertEqual(out["repo"]["name"], "fakerepo")
        policy = _m2.get_policy("fakerepo")
        self.assertTrue(policy)
        self.assertFalse(any(policy.values()))
        st = sched.load("fakerepo")
        self.assertTrue(st.get("enabled"))
        self.assertFalse(st.get("execute_enabled"))
        self.assertTrue(st.get("next_scan"))


class CapsTests(unittest.TestCase):
    def test_caps_pinned_and_honest(self):
        from openmainty import m2 as _m2
        caps = serve.api_onboard_caps()
        self.assertEqual(caps["scan_interval_s"],
                         sched.DEFAULT_INTERVAL_S)
        self.assertEqual(caps["prepare_per_tick"], 1)
        self.assertEqual(caps["refresh_per_tick"], 1)
        self.assertEqual(caps["refresh_per_day"],
                         _m2._REFRESH_PER_DAY)
        self.assertEqual(caps["cooldown_blocks"],
                         _m2._COOLDOWN_BLOCKS)
        self.assertEqual(caps["cooldown_hours"],
                         _m2._COOLDOWN_S // 3600)
        self.assertIsNone(caps["repo_limit"])
        self.assertEqual(caps["repo_limit_note"], "등록 수 제한 없음")


class FlutterLockRuleTests(OnboardFixture):
    """M3.3a three-way pubspec.lock rule + per-dir example install.

    All offline: local git fixtures, no network. Py3.9 portable."""

    def git(self, *args):
        subprocess.run(
            ["git", "-C", str(self.co)] + list(args), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=60)

    def flutter_co(self, lock=True, ignore_lock=False, example=None):
        files = {"pubspec.yaml":
                 "name: cal\nenvironment:\n  sdk: ^3.0.0\n"}
        if lock:
            files["pubspec.lock"] = "# lock\n"
        self.co = self.checkout(files)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        if ignore_lock:
            (self.co / ".gitignore").write_text(
                "pubspec.lock\n", encoding="utf-8")
        if example:
            ex = self.co / "example"
            ex.mkdir()
            (ex / "pubspec.yaml").write_text(
                "name: ex\n", encoding="utf-8")
            if example in ("tracked", "ignored"):
                (ex / "pubspec.lock").write_text(
                    "# lock\n", encoding="utf-8")
            if example == "ignored":
                (ex / ".gitignore").write_text(
                    "pubspec.lock\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "x")
        return self.co

    def test_tracked_lock_enforces(self):
        co = self.flutter_co()
        with self.with_path():
            proposal = repos.detect_fastcheck(co)
        self.assertEqual(proposal["manager"], "flutter")
        self.assertEqual(proposal["steps"][0]["argv"],
                         ["flutter", "pub", "get", "--enforce-lockfile"])
        with self.with_path():
            self.assertEqual(repos.validate_fastcheck(proposal), [])

    def test_ignored_lock_plain_get(self):
        co = self.flutter_co(ignore_lock=True)
        with self.with_path():
            proposal = repos.detect_fastcheck(co)
        self.assertEqual(proposal["steps"][0]["argv"],
                         ["flutter", "pub", "get"])

    def test_absent_lock_refused(self):
        co = self.flutter_co(lock=False)
        with self.with_path():
            proposal = repos.detect_fastcheck(co)
        self.assertIn("잠금 파일 없음", proposal["unsupported"])

    def test_non_git_lock_refused(self):
        co = self.checkout({"pubspec.yaml": "name: x\n",
                            "pubspec.lock": "# lock\n"})
        with self.with_path():
            proposal = repos.detect_fastcheck(co)
        self.assertIn("잠금 파일 없음", proposal["unsupported"])

    def test_example_per_dir_install(self):
        co = self.flutter_co(example="tracked")
        with self.with_path():
            proposal = repos.detect_fastcheck(co)
        names = [s["name"] for s in proposal["steps"]]
        self.assertEqual(names,
                         ["설치", "설치(example)", "분석", "테스트"])
        self.assertEqual(
            proposal["steps"][1]["argv"],
            ["flutter", "pub", "get", "--enforce-lockfile",
             "--directory", "example"])

    def test_example_absent_lock_refused(self):
        co = self.flutter_co(example="absent")
        with self.with_path():
            proposal = repos.detect_fastcheck(co)
        self.assertIn("example/", proposal["unsupported"])

    def test_ignored_lock_edits_keep_fingerprint(self):
        # An ignored lock never survives into a fresh worktree
        # (untracked files stay behind), so regenerating it cannot
        # move the fingerprint.
        co = self.flutter_co(ignore_lock=True)
        before = runner.source_snapshot(co)["fingerprint"]
        (co / "pubspec.lock").write_text("# lock\nv: 2\n",
                                         encoding="utf-8")
        after = runner.source_snapshot(co)["fingerprint"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
