"""Executor-owned install path + honest environment failures.

The run-fed7e06a e2e failed with `node_modules state file missing` because
only the sandboxed model worker could install, and its network was denied.
Checks must label that environment failure distinctly (never a code
verdict), and a healthy job must be able to install + check inside its own
isolated worktree through the executor shell (unsandboxed daemon/CLI path,
no global Muse setting changes).
"""
import json
import os
import shutil
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


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    use_temp_home(test, str(Path(tmp.name) / "home"))
    return Path(tmp.name)


class _InstallingFakeExecutor(runner.FakeExecutor):
    """Fake shell faithful to a real success: an exit-0 install leaves
    the installed tree behind, which ensure_worktree_installed honestly
    verifies via deps_installed(). A fake that claims success without
    producing node_modules must still read as a failed install."""

    def shell(self, cmd, cwd):
        code, text, err = super().shell(cmd, cwd)
        if code == 0 and (Path(cwd) / "package.json").is_file():
            (Path(cwd) / "node_modules").mkdir(exist_ok=True)
        return code, text, err


YARN_STATE_ERROR = ("Usage Error: Couldn't find the node_modules state file - "
                    "running an install might help (findPackageLocation)")


class DetectInstallTests(unittest.TestCase):
    def test_manager_detection(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            self.assertIsNone(runner.detect_install_cmd(work))
            (work / "package.json").write_text("{}")
            self.assertEqual(runner.detect_install_cmd(work)[0], "npm")
            (work / "yarn.lock").write_text("")
            self.assertEqual(runner.detect_install_cmd(work)[:2], ["yarn", "install"])
            (work / "yarn.lock").unlink()
            (work / "package-lock.json").write_text("{}")
            self.assertEqual(runner.detect_install_cmd(work), ["npm", "ci"])
            (work / "package-lock.json").unlink()
            # Independent realistic pub fixture: no npm manifest present,
            # so the pub worktree resolves to flutter on its own.
            (work / "package.json").unlink()
            (work / "pubspec.yaml").write_text("name: x\n")
            self.assertEqual(runner.detect_install_cmd(work)[:2], ["flutter", "pub"])

    def test_dual_manifest_prefers_npm(self):
        # Deterministic npm-first precedence when both manifests coexist.
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            (work / "package.json").write_text("{}")
            (work / "pubspec.yaml").write_text("name: x\n")
            self.assertEqual(runner.detect_install_cmd(work)[0], "npm")

    def test_deps_installed_probe(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            (work / "package.json").write_text("{}")
            self.assertFalse(runner.deps_installed(work))
            (work / "node_modules").mkdir()
            self.assertTrue(runner.deps_installed(work))


class EnvFailureHonestyTests(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_missing_state_file_is_environment_failure(self):
        state = store.create("r", "1", "t", "base0", {"worker": "test"})
        work = Path(store.run_dir(state["id"])) / "work"
        work.mkdir()
        ex = runner.FakeExecutor(script={
            "shell:yarn typecheck": [(1, YARN_STATE_ERROR)],
        })
        ok = runner.run_shell_checks(
            state, "e2e",
            [{"name": "typecheck", "cmd": ["yarn", "typecheck"], "requires": []}],
            ex, work, {})
        self.assertFalse(ok)
        note = state["steps"]["e2e"].get("note") or ""
        self.assertTrue(note.startswith("environment failure:"),
                        f"env failure must be distinct, got: {note!r}")
        self.assertIn("not a code verdict", note)

    def test_genuine_check_failure_keeps_code_verdict(self):
        state = store.create("r", "1", "t", "base0", {"worker": "test"})
        work = Path(store.run_dir(state["id"])) / "work"
        work.mkdir()
        ex = runner.FakeExecutor(script={
            "shell:yarn typecheck": [(1, "error TS2322: Type 'string' is not assignable")],
        })
        ok = runner.run_shell_checks(
            state, "e2e",
            [{"name": "typecheck", "cmd": ["yarn", "typecheck"], "requires": []}],
            ex, work, {})
        self.assertFalse(ok)
        note = state["steps"]["e2e"].get("note") or ""
        self.assertTrue(note.startswith("check 'typecheck' failed"),
                        f"genuine failure keeps code verdict, got: {note!r}")


class EnsureInstalledTests(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_skips_when_already_installed(self):
        state = store.create("r", "1", "t", "base0", {"worker": "test"})
        work = Path(store.run_dir(state["id"])) / "work"
        work.mkdir()
        (work / "package.json").write_text("{}")
        (work / "node_modules").mkdir()
        ex = runner.FakeExecutor()
        out = runner.ensure_worktree_installed(state, ex, work)
        self.assertFalse(out["ran"])
        self.assertTrue(out["installed"])
        self.assertEqual(ex.calls, [])

    def test_runs_install_via_executor_shell(self):
        state = store.create("r", "1", "t", "base0", {"worker": "test"})
        work = Path(store.run_dir(state["id"])) / "work"
        work.mkdir()
        (work / "package.json").write_text("{}")
        (work / "yarn.lock").write_text("")
        ex = _InstallingFakeExecutor(
            script={"shell:yarn install": [(0, "install ok")]})
        out = runner.ensure_worktree_installed(state, ex, work)
        self.assertTrue(out["ran"])
        self.assertTrue(out["installed"])
        self.assertIn("shell:yarn install", ex.calls)
        self.assertTrue((store.run_dir(state["id"]) / "install.log.txt").is_file())

    def test_failed_install_is_honest(self):
        state = store.create("r", "1", "t", "base0", {"worker": "test"})
        work = Path(store.run_dir(state["id"])) / "work"
        work.mkdir()
        (work / "package.json").write_text("{}")
        ex = runner.FakeExecutor(script={"shell:npm install": [(1, "ENOTFOUND registry")]}
                                 )
        out = runner.ensure_worktree_installed(state, ex, work)
        self.assertTrue(out["ran"])
        self.assertFalse(out["installed"])
        self.assertIn("environment", out["note"])


@unittest.skipUnless(shutil.which("npm") and shutil.which("node"),
                     "needs npm+node for the scoped local install integration")
class ScopedLocalInstallIntegrationTests(unittest.TestCase):
    """Real executor shell (no model): install + check in an isolated worktree."""

    def setUp(self):
        self.root = make_home(self)

    def test_install_and_check_in_isolated_worktree(self):
        work = self.root / "work"
        work.mkdir()
        sibling = self.root / "original"
        sibling.mkdir()
        (sibling / "sentinel.txt").write_text("untouched\n")
        # Small LOCAL file: dependency living inside the isolated worktree,
        # so npm really installs it (deterministic/offline, no registry).
        vendor = work / "vendor" / "fixture-dep"
        vendor.mkdir(parents=True)
        (vendor / "package.json").write_text(json.dumps({
            "name": "fixture-dep", "version": "1.0.0", "main": "index.js",
        }))
        (vendor / "index.js").write_text("module.exports = 42;\n")
        (work / "package.json").write_text(json.dumps({
            "name": "fixture", "version": "1.0.0",
            "dependencies": {"fixture-dep": "file:./vendor/fixture-dep"},
            "scripts": {"probe": (
                "node -e \"const v = require('fixture-dep'); "
                "if (v !== 42) { process.exit(1); }\"")},
        }))
        state = store.create("r", "1", "t", "base0", {"worker": "test"})
        ex = runner.Executor(model=None)
        out = runner.ensure_worktree_installed(
            state, ex, work,
            install_cmd=["npm", "install", "--no-audit", "--no-fund"])
        self.assertTrue(out["installed"], out)
        self.assertTrue((work / "node_modules").is_dir())
        code, check_out, check_err = ex.shell(["npm", "run", "probe", "--silent"], work)
        self.assertEqual(code, 0, f"{check_out[-500:]} {check_err[-500:]}")
        self.assertEqual((sibling / "sentinel.txt").read_text(), "untouched\n")
        self.assertFalse((sibling / "node_modules").exists())


if __name__ == "__main__":
    unittest.main()
