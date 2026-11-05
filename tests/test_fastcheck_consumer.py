"""M2.15 consumer build step tests.

A dep no fastcheck step executed (census "not-run") may still run
in a one-shot build script (live: webpack in example's build:web).
The gate runs at most one allow-listed script in a scratch clone —
never the derived tree (a build:web writes into TRACKED
example/dist). Offline: fixture trees, stub clone/runner/identity;
the real clone_tree runs only where APFS allows it. Py3.9-safe.
"""
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import fastcheck  # noqa: E402


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _webpack_tree(root: Path, root_scripts=None,
                  example_scripts=None) -> Path:
    """Live-like webpack fixture: declared in root + example, run
    only in example scripts, tracked example/dist bundle."""
    (root / "node_modules" / "webpack").mkdir(parents=True,
                                                exist_ok=True)
    _write_json(root / "package.json", {
        "devDependencies": {"webpack": "^5.112.0"},
        "scripts": root_scripts if root_scripts is not None else {
            "build:web": "yarn workspace example build:web"}})
    ex = root / "example"
    (ex / "dist").mkdir(parents=True, exist_ok=True)
    _write_json(ex / "package.json", {
        "devDependencies": {"webpack": "^5.112.0"},
        "scripts": example_scripts if example_scripts is not None
        else {"build:web": "webpack --mode production",
              "web": "webpack serve --mode development"}})
    _write_json(root / "node_modules" / "webpack" / "package.json", {
        "name": "webpack", "version": "5.112.2",
        "bin": {"webpack": "bin/webpack.js"}})
    (ex / "dist" / "bundle.js").write_text("old-bundle\n",
                                           encoding="utf-8")
    (root / "yarn.lock").write_text("# yarn lockfile v1\n",
                                    encoding="utf-8")
    (root / "example" / "dist").mkdir(parents=True, exist_ok=True)
    return root


def _lint_steps():
    return [{"argv": ["yarn", "lint"], "gate": "unit"}]


def _fake_ok_builder(calls, new_bundle="new-bundle\n",
                     elapsed=59.2, rc=0, tail="built in 59s"):
    def _fake(argv, worktree, env, timeout_s, install, log_path,
              loopback=False, toolchain=(), cwd=""):
        calls.append({"argv": list(argv), "worktree": worktree,
                      "cwd": cwd})
        target = Path(cwd or worktree) / "dist" / "bundle.js"
        target.write_text(new_bundle, encoding="utf-8")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("building...\n", encoding="utf-8")
        return {"argv": list(argv), "rc": rc, "timed_out": False,
                "elapsed_s": elapsed, "timeout_s": timeout_s,
                "install": bool(install),
                "network_sandbox": "strict",
                "sandbox_profile": "files+network",
                "log": str(log_path), "log_sha256": "x",
                "log_bytes": 11, "log_truncated": False,
                "log_tail": tail, "children_verified_gone": True,
                "escaped_processes": [], "escaped_unverified": [],
                "reaper": "ok"}
    return _fake


def _real_copy(src, dst):
    shutil.copytree(src, dst)
    return ""


class AllowListTests(unittest.TestCase):
    def test_oneshot_builds_allowed(self):
        for name in ("build", "build:web", "build:lib-esm",
                     "build:docs2"):
            self.assertTrue(fastcheck.consumer_script_allowed(name),
                            name)

    def test_servers_devices_native_never_run(self):
        for name in ("web", "serve", "dev", "start", "android",
                     "ios", "build:android", "build:ios",
                     "build:ios-sim", "build:android-test", "test",
                     "lint", "", "build:web!", "build:WEB"):
            self.assertFalse(
                fastcheck.consumer_script_allowed(name), name)


class PickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name) / "wt"

    def test_delegated_workspace_script_wins(self):
        _webpack_tree(
            self.wt,
            root_scripts={"build:web":
                          "webpack --version && "
                          "yarn workspace example build:web",
                          "lint": "eslint ."},
            example_scripts={"build:web": "webpack --mode production",
                             "web": "webpack serve"})
        out = fastcheck.pick_consumer_script(
            str(self.wt), ["root: build:web", "example: build:web",
                           "example: web"])
        self.assertEqual(out, {"rel": "example", "name": "build:web",
                               "label": "example: build:web"})

    def test_workspace_first_without_delegation(self):
        _webpack_tree(self.wt)
        out = fastcheck.pick_consumer_script(
            str(self.wt), ["root: build", "example: build:web"])
        self.assertEqual(out, {"rel": "example", "name": "build:web",
                               "label": "example: build:web"})

    def test_nothing_qualifies_is_none(self):
        _webpack_tree(self.wt)
        self.assertIsNone(fastcheck.pick_consumer_script(
            str(self.wt), ["example: web", "root: start"]))
        self.assertIsNone(fastcheck.pick_consumer_script(
            str(self.wt), []))
        self.assertIsNone(fastcheck.pick_consumer_script(
            "/nonexistent-wt", None))


class ManagerTests(unittest.TestCase):
    def test_profile_manager_first(self):
        self.assertEqual(
            fastcheck.consumer_manager(
                [{"argv": ["pnpm", "install"], "install": True}], "/x"),
            "pnpm")

    def test_lockfile_then_default(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.assertEqual(fastcheck.consumer_manager([], str(root)),
                         "yarn")
        (root / "package-lock.json").write_text("{}\n")
        self.assertEqual(fastcheck.consumer_manager([], str(root)),
                         "npm")


class DepNamesTests(unittest.TestCase):
    def test_primary_plus_group_strings_and_members(self):
        self.assertEqual(
            fastcheck.consumer_dep_names(
                {"dep": "webpack",
                 "group": ["webpack-dev-server 5.0.0->6.1.0",
                           {"dep": "x"}],
                 "members": [{"dep": "y"}, "junk"]}),
            ["webpack", "webpack-dev-server", "x", "y"])

    def test_garbage_is_empty(self):
        self.assertEqual(fastcheck.consumer_dep_names(None), [])
        self.assertEqual(fastcheck.consumer_dep_names({}), [])
        self.assertEqual(fastcheck.consumer_dep_names({"dep": ""}),
                         [])


class GateFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wt = self.root / "wt"
        self.base = self.root / "home"
        (self.base / "integration_ops").mkdir(parents=True)
        self.op = {"dep": "webpack", "target": "5.112.2"}
        self.env = {"HOME": str(self.root), "TMPDIR": str(self.root)}

    def _gate(self, wt_scripts=True, _clone=None, _run_step=None,
              _identity=None, _keep_scratch=False, op=None,
              steps=None, **kw):
        if wt_scripts:
            _webpack_tree(self.wt)
        steps = _lint_steps() if steps is None else steps
        receipts = [{"argv": list(s["argv"]), "gate": s.get("gate")}
                    for s in steps]
        return fastcheck.run_consumer_gate(
            str(self.wt), self.op if op is None else op, steps,
            receipts, self.env, self.base, "op-consumer-1",
            str(self.root), (), _clone=_clone, _run_step=_run_step,
            _identity=_identity, _keep_scratch=_keep_scratch, **kw)

    def _scratch_dirs(self):
        return sorted((self.base / "tmp").glob("consumer-*")) \
            if (self.base / "tmp").is_dir() else []


class PassTests(GateFixture):
    def test_pass_rewrites_scratch_only(self):
        _webpack_tree(self.wt)
        calls = []
        before = (self.wt / "example" / "dist"
                  / "bundle.js").read_text(encoding="utf-8")
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_ok_builder(calls),
            _identity=lambda wt: ("head1", "fp1"),
            _keep_scratch=True)
        self.assertEqual(out["status"], "pass")
        self.assertEqual(out["script"], "example: build:web")
        self.assertEqual(out["label"],
                         "소비 스크립트 build:web 통과 (59초)")
        self.assertEqual(out["argv"], ["yarn", "build:web"])
        self.assertEqual(out["receipt"]["gate"], "consumer")
        self.assertEqual(out["receipt"]["name"],
                         "consumer (build:web: webpack)")
        # The derived tree is byte-identical (tracked dist kept).
        self.assertEqual((self.wt / "example" / "dist" / "bundle.js")
                         .read_text(encoding="utf-8"), before)
        # The scratch copy holds the new output.
        dirs = self._scratch_dirs()
        self.assertEqual(len(dirs), 1)
        self.assertEqual((dirs[0] / "example" / "dist" / "bundle.js")
                         .read_text(encoding="utf-8"), "new-bundle\n")
        # The build ran in the workspace subdir, not the derived tree.
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["cwd"].endswith("example"))
        self.assertNotEqual(calls[0]["worktree"], str(self.wt))

    def test_scratch_removed_by_default(self):
        _webpack_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_ok_builder([]),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "pass")
        self.assertEqual(self._scratch_dirs(), [])

    def test_group_member_notrun_runs(self):
        _webpack_tree(self.wt)
        pkg = json.loads((self.wt / "package.json")
                         .read_text(encoding="utf-8"))
        pkg["devDependencies"]["webpack-dev-server"] = "^6.1.0"
        pkg["scripts"]["check"] = "webpack --version"
        (self.wt / "package.json").write_text(json.dumps(pkg),
                                              encoding="utf-8")
        ex_pkg = json.loads((self.wt / "example" / "package.json")
                            .read_text(encoding="utf-8"))
        ex_pkg["devDependencies"]["webpack-dev-server"] = "^6.1.0"
        (self.wt / "example" / "package.json").write_text(
            json.dumps(ex_pkg), encoding="utf-8")
        _write_json(self.wt / "node_modules" / "webpack-dev-server"
                    / "package.json",
                    {"name": "webpack-dev-server",
                     "bin": {"webpack-dev-server": "bin/cli.js"}})
        calls = []
        out = self._gate(
            wt_scripts=False,
            steps=[{"argv": ["yarn", "check"], "gate": "unit"}],
            op={"dep": "webpack",
                "group": ["webpack-dev-server 5.0.0->6.1.0"]},
            _clone=_real_copy,
            _run_step=_fake_ok_builder(calls),
            _identity=lambda wt: ("head1", "fp1"),
            _keep_scratch=True)
        self.assertEqual(out["status"], "pass")
        self.assertEqual(len(calls), 1)

    def test_direct_dep_never_runs(self):
        _webpack_tree(self.wt)
        pkg = json.loads((self.wt / "package.json")
                         .read_text(encoding="utf-8"))
        pkg["scripts"]["check"] = "webpack --version"
        (self.wt / "package.json").write_text(json.dumps(pkg),
                                              encoding="utf-8")
        calls = []
        out = self._gate(
            wt_scripts=False,
            steps=[{"argv": ["yarn", "check"], "gate": "unit"}],
            _clone=_real_copy,
            _run_step=_fake_ok_builder(calls),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertIsNone(out)
        self.assertEqual(calls, [])


class FailSkipTests(GateFixture):
    def test_failing_build_is_red_with_tail(self):
        _webpack_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_ok_builder([], rc=1,
                                       tail="ERROR Failed to compile"),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("ERROR Failed to compile", out["log_tail"])
        self.assertIn("rc=1", out["reason"])
        summary = fastcheck.apply_consumer_result(
            {"ok": True, "steps": []}, out)
        self.assertFalse(summary["ok"])
        self.assertIn("ERROR Failed to compile",
                      summary["failed"]["log_tail"])
        self.assertEqual(summary["failed"]["argv"],
                         ["yarn", "build:web"])

    def test_non_allowlist_scripts_never_run(self):
        (self.wt / "node_modules" / "webpack").mkdir(parents=True)
        _write_json(self.wt / "package.json", {
            "devDependencies": {"webpack": "^5.112.0"},
            "scripts": {"lint": "eslint ."}})
        _write_json(self.wt / "example" / "package.json", {
            "devDependencies": {"webpack": "^5.112.0"},
            "scripts": {"web": "webpack serve",
                        "serve": "webpack serve --port 9"}})
        _write_json(self.wt / "node_modules" / "webpack"
                    / "package.json",
                    {"name": "webpack", "bin": "bin/webpack.js"})
        calls = []

        def _boom(*a, **k):
            raise AssertionError("must never run")

        out = self._gate(wt_scripts=False, _clone=_real_copy,
                         _run_step=_boom,
                         _identity=lambda wt: ("head1", "fp1"))
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_clone_failure_is_honest_skip(self):
        _webpack_tree(self.wt)
        calls = []
        out = self._gate(
            _clone=lambda s, d: "복제 실패 — 소비 스크립트 미실행 "
                                "(no space)",
            _run_step=_fake_ok_builder(calls),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "skip")
        self.assertTrue(out["reason"].startswith("복제 실패"))
        self.assertEqual(calls, [])
        summary = fastcheck.apply_consumer_result(
            {"ok": True, "steps": []}, out)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["consumer"]["status"], "skip")

    def test_fingerprint_change_fails_loudly(self):
        _webpack_tree(self.wt)
        states = [("head1", "fp1"), ("head1", "fp2")]
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_ok_builder([]),
            _identity=lambda wt: states.pop(0))
        self.assertEqual(out["status"], "fail")
        self.assertEqual(out["reason"], "작업실이 바뀜")
        self.assertTrue(out["log_tail"].startswith("작업실이 바뀜"))
        summary = fastcheck.apply_consumer_result(
            {"ok": True, "steps": []}, out)
        self.assertFalse(summary["ok"])

    def test_unreadable_identity_fails_closed(self):
        _webpack_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_ok_builder([]),
            _identity=lambda wt: ("", ""))
        self.assertEqual(out["status"], "fail")
        self.assertEqual(out["reason"], "작업실이 바뀜")


class ApplyTests(unittest.TestCase):
    def test_none_leaves_summary(self):
        summary = {"ok": True, "steps": [{"rc": 0}]}
        self.assertIs(fastcheck.apply_consumer_result(summary, None),
                      summary)
        self.assertNotIn("consumer", summary)

    def test_pass_appends_receipt(self):
        summary = {"ok": True, "steps": []}
        rec = {"status": "pass", "script": "example: build:web",
               "label": "소비 스크립트 build:web 통과 (59초)",
               "receipt": {"gate": "consumer", "rc": 0}}
        fastcheck.apply_consumer_result(summary, rec)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["steps"],
                         [{"gate": "consumer", "rc": 0}])
        self.assertEqual(summary["consumer"]["label"],
                         "소비 스크립트 build:web 통과 (59초)")


class CloneTreeTests(unittest.TestCase):
    def test_missing_source_is_honest_skip(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "src-missing"
        dst = Path(tmp.name) / "dst-nope"
        self.assertFalse(src.exists())
        self.assertFalse(dst.exists())
        reason = fastcheck.clone_tree(str(src), str(dst))
        self.assertTrue(reason.startswith("복제 실패"))
        self.assertIn("소비 스크립트 미실행", reason)
        self.assertFalse(dst.exists())

    def test_real_apfs_clone(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "src"
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "f.txt").write_text("data\n",
                                           encoding="utf-8")
        dst = Path(tmp.name) / "dst"
        reason = fastcheck.clone_tree(str(src), str(dst))
        if reason:
            self.skipTest(f"no clonefile support here: {reason}")
        self.assertEqual((dst / "sub" / "f.txt")
                         .read_text(encoding="utf-8"), "data\n")


def _turbo_tree(root: Path, turbo_version="2.11.10",
                with_turbo_json=True, declared_tasks=None,
                example_name="example-pkg") -> Path:
    """Live-like turbo fixture: root + example manifests, turbo.json
    graph, installed turbo record. No network, all local files."""
    (root / "node_modules" / "turbo").mkdir(parents=True,
                                            exist_ok=True)
    _write_json(root / "package.json", {
        "name": "root-pkg",
        "devDependencies": {"turbo": "^" + turbo_version},
        "scripts": {"build:web": "yarn workspace example build:web"}})
    ex = root / "example"
    ex_scripts = {"build:web": "webpack --mode production"}
    ex_pkg = {"scripts": ex_scripts}
    if example_name is not None:
        ex_pkg["name"] = example_name
    _write_json(ex / "package.json", ex_pkg)
    _write_json(root / "node_modules" / "turbo" / "package.json", {
        "name": "turbo", "version": turbo_version,
        "bin": {"turbo": "bin/turbo.js"}})
    if with_turbo_json:
        tasks = declared_tasks if declared_tasks is not None else {
            "build:web": {"dependsOn": ["^build"]},
            "build:android": {}, "build:ios": {}}
        _write_json(root / "turbo.json", {"tasks": tasks})
    (root / "yarn.lock").write_text("# yarn lockfile v1\n",
                                    encoding="utf-8")
    return root


def _fake_turbo_builder(calls, version="2.11.10", dry_obj=None,
                        build_rc=0, build_tail="built via turbo in 5s",
                        version_rc=0, dry_rc=0, elapsed=59.2):
    """Dispatching fake: writes version/graph/build outputs to the
    step log (mirroring real run_step log capture), records argv,
    cwd and timeout per call."""
    if dry_obj is None:
        dry_obj = {"tasks": [{"taskId": "example#build:web",
                              "task": "build:web",
                              "package": "example-pkg",
                              "command": "webpack --mode production",
                              "dependsOn": []}]}

    def _fake(argv, worktree, env, timeout_s, install, log_path,
              loopback=False, toolchain=(), cwd=""):
        calls.append({"argv": list(argv), "worktree": worktree,
                      "cwd": cwd, "timeout_s": timeout_s})
        text = ""
        rc = 0
        if argv[-1] == "--version":
            text, rc = version + "\n", version_rc
        elif "--dry=json" in argv:
            text = dry_obj if isinstance(dry_obj, str) \
                else json.dumps(dry_obj)
            rc = dry_rc
        else:
            text, rc = build_tail + "\n", build_rc
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(text, encoding="utf-8")
        return {"argv": list(argv), "rc": rc, "timed_out": False,
                "elapsed_s": elapsed, "timeout_s": timeout_s,
                "install": bool(install),
                "network_sandbox": "strict",
                "sandbox_profile": "files+network",
                "sandboxed_argv": True,
                "file_sandbox": "allowlist: scratch+caches",
                "write_allow": [worktree],
                "scripts_off": True,
                "log": str(log_path), "log_sha256": "x",
                "log_bytes": len(text), "log_truncated": False,
                "log_tail": text[-2000:],
                "children_verified_gone": True,
                "escaped_processes": [], "escaped_unverified": [],
                "reaper": "ok"}
    return _fake


class TurboGateFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wt = self.root / "wt"
        self.base = self.root / "home"
        (self.base / "integration_ops").mkdir(parents=True)
        self.op = {"dep": "turbo", "target": "2.11.10"}
        self.env = {"HOME": str(self.root), "TMPDIR": str(self.root)}

    def _gate(self, _clone=None, _run_step=None, _identity=None,
              _keep_scratch=False, op=None, steps=None,
              timeout_s=600):
        steps = [{"argv": ["yarn", "lint"], "gate": "unit"}] \
            if steps is None else steps
        receipts = [{"argv": list(s["argv"]), "gate": s.get("gate")}
                    for s in steps]
        return fastcheck.run_consumer_gate(
            str(self.wt), self.op if op is None else op, steps,
            receipts, self.env, self.base, "op-turbo-1",
            str(self.root), (), _clone=_clone, _run_step=_run_step,
            _identity=_identity, _keep_scratch=_keep_scratch,
            timeout_s=timeout_s)

    def _scratch_dirs(self):
        return sorted((self.base / "tmp").glob("consumer-*")) \
            if (self.base / "tmp").is_dir() else []


class TurboPassTests(TurboGateFixture):
    def test_three_steps_pass_with_full_evidence(self):
        _turbo_tree(self.wt)
        calls = []
        before = (self.wt / "turbo.json").read_text(encoding="utf-8")
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(calls),
            _identity=lambda wt: ("head1", "fp1"),
            _keep_scratch=True)
        self.assertEqual(out["status"], "pass")
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            calls[0]["argv"],
            ["yarn", "exec", "turbo", "--version"])
        self.assertEqual(
            calls[1]["argv"],
            ["yarn", "exec", "turbo", "run", "build:web",
             "--dry=json", "--filter=example-pkg"])
        self.assertEqual(calls[1]["argv"][-1], calls[2]["argv"][-1])
        self.assertEqual(
            calls[2]["argv"],
            ["yarn", "exec", "turbo", "run", "build:web", "--force",
             "--filter=example-pkg"])
        for call in calls:
            self.assertEqual(call["worktree"],
                             str(self._scratch_dirs()[0]))
            self.assertEqual(call["cwd"],
                             str(self._scratch_dirs()[0]))
            self.assertLessEqual(call["timeout_s"], 600)
        self.assertEqual(out["name"], "consumer (turbo: build:web)")
        self.assertIn("Turbo Web", out["label"])
        self.assertIn("작업 실행 검증 통과", out["label"])
        self.assertEqual(out["turbo_version_expected"], "2.11.10")
        self.assertEqual(out["turbo_version_actual"], "2.11.10")
        self.assertEqual(out["turbo_tasks"], ["example#build:web"])
        self.assertEqual(out["turbo_task_count"], 1)
        self.assertEqual(out["turbo_scope"], "--filter=example-pkg")
        self.assertEqual(len(out["turbo_steps"]), 3)
        self.assertIn("Android", out["note"])
        self.assertIn("미검증", out["note"])
        self.assertEqual(out["receipt"]["gate"], "consumer")
        # Actual runner provenance is preserved unchanged —
        # isolation metadata on every step and the receipt.
        scratch = str(self._scratch_dirs()[0])
        for step in out["turbo_steps"]:
            self.assertTrue(step["sandboxed_argv"])
            self.assertEqual(step["sandbox_profile"],
                             "files+network")
            self.assertEqual(step["write_allow"], [scratch])
            self.assertTrue(step["children_verified_gone"])
        self.assertEqual(out["receipt"]["sandbox_profile"],
                         "files+network")
        self.assertEqual(out["receipt"]["write_allow"], [scratch])
        self.assertEqual(out["receipt"]["file_sandbox"],
                         "allowlist: scratch+caches")
        self.assertTrue(out["receipt"]["children_verified_gone"])
        self.assertTrue(out["receipt"]["sandboxed_argv"])
        # Derived tree untouched; scratch holds the clone.
        self.assertEqual((self.wt / "turbo.json")
                         .read_text(encoding="utf-8"), before)
        self.assertEqual(len(self._scratch_dirs()), 1)
        summary = fastcheck.apply_consumer_result(
            {"ok": True, "steps": []}, out)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["consumer"]["turbo_version_actual"],
                         "2.11.10")
        self.assertEqual(summary["consumer"]["turbo_task_count"], 1)

    def test_scratch_removed_by_default(self):
        _turbo_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder([]),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "pass")
        self.assertEqual(self._scratch_dirs(), [])

    def test_unfiltered_when_workspace_unnamed(self):
        _turbo_tree(self.wt, example_name=None)
        calls = []
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(calls),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "pass")
        self.assertEqual(
            calls[1]["argv"],
            ["yarn", "exec", "turbo", "run", "build:web",
             "--dry=json"])
        self.assertEqual(
            calls[2]["argv"],
            ["yarn", "exec", "turbo", "run", "build:web", "--force"])
        self.assertEqual(out["turbo_scope"], "미지정")

    def test_large_valid_graph_parses_beyond_tail(self):
        _turbo_tree(self.wt)
        long_command = "webpack --mode production " + "x" * 5000
        calls = []
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                calls, dry_obj={"tasks": [
                    {"taskId": "example#build:web",
                     "package": "example-pkg",
                     "command": long_command}]}),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "pass")
        self.assertEqual(len(calls), 3)
        self.assertGreater(len(long_command), 2000)

    def test_oversize_graph_fails_without_build(self):
        _turbo_tree(self.wt)
        calls = []
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                calls, dry_obj={"tasks": [
                    {"taskId": "example#build:web",
                     "package": "example-pkg",
                     "command": "webpack --mode production",
                     "pad": "y" * (1024 * 1024 + 8)}]}),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("초과", out["reason"])
        self.assertEqual(len(calls), 2)


class TurboQualificationTests(TurboGateFixture):
    def test_non_yarn_manager_is_none(self):
        _turbo_tree(self.wt)
        (self.wt / "yarn.lock").unlink()
        (self.wt / "package-lock.json").write_text("{}\n")
        calls = []

        def _boom(*a, **k):
            raise AssertionError("must never run")

        out = self._gate(_clone=_real_copy, _run_step=_boom,
                         _identity=lambda wt: ("head1", "fp1"),
                         steps=[{"argv": ["npm", "install"],
                                 "install": True}])
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_missing_turbo_json_is_none(self):
        _turbo_tree(self.wt, with_turbo_json=False)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder([]),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertIsNone(out)

    def test_undeclared_task_is_none(self):
        _turbo_tree(self.wt,
                    declared_tasks={"build:android": {},
                                    "build:ios": {}})
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder([]),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertIsNone(out)

    def test_missing_target_is_none(self):
        _turbo_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder([]),
            _identity=lambda wt: ("head1", "fp1"),
            op={"dep": "turbo"})
        self.assertIsNone(out)

    def test_non_turbo_primary_keeps_generic_path(self):
        _webpack_tree(self.wt)
        calls = []
        out = fastcheck.run_consumer_gate(
            str(self.wt), {"dep": "webpack", "target": "5.112.2"},
            _lint_steps(),
            [{"argv": ["yarn", "lint"], "gate": "unit"}],
            self.env, self.base, "op-webpack-1", str(self.root), (),
            _clone=_real_copy,
            _run_step=_fake_ok_builder(calls),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "pass")
        self.assertEqual(out["receipt"]["name"],
                         "consumer (build:web: webpack)")
        self.assertNotIn("turbo_version_actual", out)

    def test_group_only_turbo_never_becomes_primary(self):
        (self.wt / "node_modules" / "webpack").mkdir(parents=True)
        _write_json(self.wt / "package.json", {
            "devDependencies": {"webpack": "^5.112.0"},
            "scripts": {"lint": "eslint ."}})
        _write_json(self.wt / "example" / "package.json", {
            "devDependencies": {"webpack": "^5.112.0"},
            "scripts": {"web": "webpack serve"}})
        _write_json(self.wt / "node_modules" / "webpack"
                    / "package.json",
                    {"name": "webpack", "bin": "bin/webpack.js"})
        (self.wt / "yarn.lock").write_text("# yarn lockfile v1\n")
        calls = []

        def _boom(*a, **k):
            raise AssertionError("turbo probe must never run")

        out = fastcheck.run_consumer_gate(
            str(self.wt),
            {"dep": "webpack", "target": "5.112.2",
             "group": ["turbo 2.10.8->2.11.10"]},
            _lint_steps(),
            [{"argv": ["yarn", "lint"], "gate": "unit"}],
            self.env, self.base, "op-webpack-group-1",
            str(self.root), (),
            _clone=_real_copy, _run_step=_boom,
            _identity=lambda wt: ("head1", "fp1"))
        self.assertIsNone(out)
        self.assertEqual(calls, [])


class TurboFailTests(TurboGateFixture):
    def test_version_mismatch_fails_closed(self):
        _turbo_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder([], version="2.10.8"),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("2.11.10", out["reason"])
        self.assertIn("2.10.8", out["reason"])
        self.assertEqual(out["turbo_version_expected"], "2.11.10")
        self.assertEqual(out["turbo_version_actual"], "2.10.8")
        summary = fastcheck.apply_consumer_result(
            {"ok": True, "steps": []}, out)
        self.assertFalse(summary["ok"])

    def test_empty_graph_fails(self):
        _turbo_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                [], dry_obj={"tasks": []}),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("비어 있음", out["reason"])

    def test_malformed_graph_fails(self):
        _turbo_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                [], dry_obj="not json at all"),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("파싱 실패", out["reason"])

    def test_no_matching_task_fails(self):
        _turbo_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                [], dry_obj={"tasks": [
                    {"taskId": "example#lint",
                     "command": "eslint ."}]}),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("비-Web", out["reason"])

    def test_nonexistent_command_never_reaches_build(self):
        _turbo_tree(self.wt)
        calls = []
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                calls, dry_obj={"tasks": [
                    {"taskId": "example#build:web",
                     "package": "example-pkg",
                     "command": "<NONEXISTENT>"}]}),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("명령 없음", out["reason"])
        self.assertEqual(len(calls), 2)

    def test_native_dependency_refuses_before_build(self):
        _turbo_tree(self.wt)
        calls = []
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                calls, dry_obj={"tasks": [
                    {"taskId": "example#build:web",
                     "package": "example-pkg",
                     "command": "webpack --mode production",
                     "dependsOn": ["example#build:android"]}]}),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("비-Web 의존 포함", out["reason"])
        self.assertEqual(len(calls), 2)

    def test_bare_banner_version_never_counts(self):
        # A yarn-style banner line is not a full-line version, so
        # only the real turbo stdout line counts here.
        _turbo_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                [], version="yarn run v1.22.19\n$ turbo --version\n"
                            "2.11.10\nDone in 0.21s."),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "pass")
        self.assertEqual(out["turbo_version_actual"], "2.11.10")

    def test_ambiguous_versions_fail_closed(self):
        # Two distinct bare versions must never resolve to either.
        _turbo_tree(self.wt)
        calls = []
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                calls, version="1.22.19\n2.11.10\n"),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("모호함", out["reason"])
        self.assertEqual(len(calls), 1)

    def test_real_build_failure_is_red(self):
        _turbo_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                [], build_rc=1, build_tail="error Command failed"),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("실제 빌드 실패", out["reason"])
        self.assertIn("error Command failed", out["log_tail"])
        summary = fastcheck.apply_consumer_result(
            {"ok": True, "steps": []}, out)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["failed"]["argv"][-2:],
                         ["--force", "--filter=example-pkg"])

    def test_mixed_unknown_entry_refuses_build(self):
        _turbo_tree(self.wt)
        calls = []
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                calls, dry_obj={"tasks": [
                    {"taskId": "example#build:web",
                     "package": "example-pkg",
                     "command": "webpack --mode production"},
                    "mystery-string"]}),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("형식 오류", out["reason"])
        self.assertEqual(len(calls), 2)

    def test_missing_package_refuses_build(self):
        _turbo_tree(self.wt)
        calls = []
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(
                calls, dry_obj={"tasks": [
                    {"taskId": "example#build:web",
                     "command": "webpack --mode production"}]}),
            _identity=lambda wt: ("head1", "fp1"))
        self.assertEqual(out["status"], "fail")
        self.assertIn("소속 불명", out["reason"])
        self.assertEqual(len(calls), 2)

    def test_zero_initial_budget_fails_without_running(self):
        _turbo_tree(self.wt)
        calls = []
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder(calls),
            _identity=lambda wt: ("head1", "fp1"),
            timeout_s=0)
        self.assertEqual(out["status"], "fail")
        self.assertTrue(out["timed_out"])
        self.assertEqual(out["reason"], "소비 검사 시간 초과")
        self.assertEqual(calls, [])

    def test_budget_exhausted_after_version_fails(self):
        _turbo_tree(self.wt)
        calls = []

        def _slow(argv, worktree, env, timeout_s, install, log_path,
                 loopback=False, toolchain=(), cwd=""):
            calls.append(list(argv))
            time.sleep(0.05)
            return _fake_turbo_builder([])(
                argv, worktree, env, timeout_s, install, log_path,
                loopback=loopback, toolchain=toolchain, cwd=cwd)

        out = self._gate(
            _clone=_real_copy, _run_step=_slow,
            _identity=lambda wt: ("head1", "fp1"),
            timeout_s=0.01)
        self.assertEqual(out["status"], "fail")
        self.assertTrue(out["timed_out"])
        self.assertEqual(out["reason"], "소비 검사 시간 초과")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], "--version")

    def test_fingerprint_change_fails_loudly(self):
        _turbo_tree(self.wt)
        states = [("head1", "fp1"), ("head1", "fp2")]
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder([]),
            _identity=lambda wt: states.pop(0))
        self.assertEqual(out["status"], "fail")
        self.assertEqual(out["reason"], "작업실이 바뀜")

    def test_unreadable_identity_fails_closed(self):
        _turbo_tree(self.wt)
        out = self._gate(
            _clone=_real_copy,
            _run_step=_fake_turbo_builder([]),
            _identity=lambda wt: ("", ""))
        self.assertEqual(out["status"], "fail")
        self.assertEqual(out["reason"], "작업실이 바뀜")


if __name__ == "__main__":
    unittest.main()
