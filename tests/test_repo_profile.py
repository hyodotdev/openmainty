"""M2.1b-1: repo-profile fastcheck block validation.

The daemon runs under launchd with a minimal PATH, so the repo
carries explicit absolute toolchain dirs (existing at save) and argv
lists executed with no shell. Py3.9 portable.
"""
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import repos  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


class FastcheckSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        self.tools = Path(self.tmp.name) / "tools"
        self.tools.mkdir()

    def profile(self, **kw):
        base = {"name": "fakerepo", "github": "o/r", "baseBranch": "main"}
        base.update(kw)
        return base

    def good_fc(self):
        return {"steps": [{"argv": ["yarn", "install", "--immutable"],
                            "gate": "install",
                            "install": True, "timeout_s": 1200},
                           {"argv": ["yarn", "jest"], "gate": "unit",
                            "timeout_s": 600}],
                "toolchain": [str(self.tools)]}

    def test_1_valid_block_saves(self):
        path = repos.save(self.profile(fastcheck=self.good_fc()))
        self.assertTrue(path.is_file())
        self.assertEqual(repos.load("fakerepo")["fastcheck"]["steps"][0]
                         ["argv"][0], "yarn")

    def test_2_missing_block_is_valid(self):
        # No fastcheck key: the lane blocks at runtime, the profile
        # itself stays valid.
        self.assertEqual(repos.validate(self.profile()), [])
        repos.save(self.profile())

    def test_3_bad_shapes_rejected(self):
        self.assertTrue(repos.validate_fastcheck("yarn"))
        self.assertTrue(repos.validate_fastcheck({"steps": []}))
        self.assertTrue(repos.validate_fastcheck({"steps": [
            {"argv": []}]}))
        self.assertTrue(repos.validate_fastcheck({"steps": [
            {"argv": "yarn install"}]}))
        self.assertTrue(repos.validate_fastcheck({"steps": [
            {"argv": ["yarn"], "install": "yes"}]}))
        self.assertTrue(repos.validate_fastcheck({"steps": [
            {"argv": ["yarn"], "timeout_s": 0}]}))
        self.assertTrue(repos.validate_fastcheck({"steps": [
            {"argv": ["yarn"], "timeout_s": True}]}))
        self.assertTrue(repos.validate_fastcheck({"steps": [
            {"argv": ["yarn"]}]}))
        self.assertEqual(repos.validate_fastcheck(self.good_fc()), [])

    def test_4_toolchain_must_exist_absolute(self):
        bad_rel = dict(self.good_fc(), toolchain=["tools"])
        self.assertTrue(repos.validate_fastcheck(bad_rel))
        bad_missing = dict(self.good_fc(),
                           toolchain=[str(self.tools / "nope")])
        self.assertTrue(repos.validate_fastcheck(bad_missing))
        with self.assertRaises(SystemExit):
            repos.save(self.profile(fastcheck=bad_missing))

    def test_5_argv_lists_allow_spaces_without_shell(self):
        # An argv element with a space is data, not shell syntax: the
        # runner never joins the list, so this stays valid.
        fc = {"steps": [{"argv": ["node", "run test with spaces"],
                          "gate": "unit"}],
              "toolchain": []}
        self.assertEqual(repos.validate_fastcheck(fc), [])


class ProbeTests(FastcheckSchemaTests):
    def fake_tool(self, name="probe-tool"):
        # A resolvable tool: the running interpreter under a
        # toolchain name. `probe-tool --version` exits 0.
        (self.tools / name).symlink_to(sys.executable)
        return name

    def test_probe_ok_version(self):
        from openmainty import fastcheck
        self.fake_tool()
        fc = {"steps": [{"argv": ["probe-tool", "--all"],
                         "gate": "unit"}],
              "toolchain": [str(self.tools)]}
        (out,) = fastcheck.probe_profile_tools(fc)
        self.assertTrue(out["ok"])
        self.assertIn("probe-tool", out["path"])
        self.assertTrue(out["version"])

    def test_probe_missing_tool(self):
        from openmainty import fastcheck
        fc = {"steps": [{"argv": ["no-such-tool-xyz"],
                         "gate": "unit"}],
              "toolchain": [str(self.tools)]}
        (out,) = fastcheck.probe_profile_tools(fc)
        self.assertFalse(out["ok"])
        self.assertIn("도구 없음", out["error"])

    def test_probe_env_scrubbed(self):
        # The probe child never sees secrets, even when set.
        from openmainty import fastcheck
        os.environ["M3_TEST_TOKEN"] = "must-not-leak"
        self.addCleanup(os.environ.pop, "M3_TEST_TOKEN", None)
        seen = {}

        import subprocess
        real_run = subprocess.run

        def spy(*a, **k):
            env = (k.get("env") or {})
            seen.update(env)
            return real_run(*a, **k)
        subprocess.run = spy
        try:
            self.fake_tool()
            fc = {"steps": [{"argv": ["probe-tool"],
                             "gate": "unit"}],
                  "toolchain": [str(self.tools)]}
            fastcheck.probe_profile_tools(fc)
        finally:
            subprocess.run = real_run
        self.assertNotIn("M3_TEST_TOKEN", seen)


class ProfileApiTests(FastcheckSchemaTests):
    def test_get_unknown_repo_404(self):
        from openmainty import serve
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_repo_profile({"repo": "nope"})
        self.assertEqual(ctx.exception.status, 404)

    def test_get_returns_validation_and_probes(self):
        from openmainty import serve
        (self.tools / "probe-tool").symlink_to(sys.executable)
        repos.save(self.profile(fastcheck={
            "steps": [{"argv": ["probe-tool"], "gate": "unit"}],
            "toolchain": [str(self.tools)]}))
        out = serve.api_repo_profile({"repo": "fakerepo"})
        self.assertEqual(out["validation"], [])
        # P1: GET never probes — the explicit POST does.
        self.assertEqual(out["probes"], [])

    def test_p1_get_runs_no_process(self):
        # P1: GET is side-effect free — no subprocess, so a
        # no-Origin <img> probe cannot trigger tool runs.
        import subprocess
        from openmainty import serve
        (self.tools / "probe-tool").symlink_to(sys.executable)
        repos.save(self.profile(fastcheck={
            "steps": [{"argv": ["probe-tool"], "gate": "unit"}],
            "toolchain": [str(self.tools)]}))
        calls = []
        real_run = subprocess.run

        def spy(*a, **k):
            calls.append(a)
            return real_run(*a, **k)
        subprocess.run = spy
        try:
            out = serve.api_repo_profile({"repo": "fakerepo"})
        finally:
            subprocess.run = real_run
        self.assertEqual(calls, [])
        self.assertEqual(out["probes"], [])

    def test_p1_post_probe_runs_explicitly(self):
        from openmainty import serve
        (self.tools / "probe-tool").symlink_to(sys.executable)
        repos.save(self.profile(fastcheck={
            "steps": [{"argv": ["probe-tool"], "gate": "unit"}],
            "toolchain": [str(self.tools)]}))
        out = serve.api_repo_profile({"repo": "fakerepo",
                                      "probe": True})
        self.assertEqual(len(out["probes"]), 1)
        self.assertTrue(out["probes"][0]["ok"])

    def test_s16_post_probe_runs_in_checkout(self):
        # S16: the probe runs --version with cwd = the registered
        # checkout (read-only), so a pinned manager shows.
        from openmainty import pcconfig, serve
        (self.tools / "probe-tool").symlink_to(sys.executable)
        repos.save(self.profile(fastcheck={
            "steps": [{"argv": ["probe-tool"], "gate": "unit"}],
            "toolchain": [str(self.tools)]}))
        co = Path(self.tmp.name) / "checkout"
        co.mkdir()
        (co / "sentinel.txt").write_text("untouched\n")
        pc = pcconfig.load()
        pc["checkouts"]["fakerepo"] = str(co)
        pcconfig.save(pc)
        out = serve.api_repo_profile({"repo": "fakerepo",
                                      "probe": True})
        self.assertEqual(len(out["probes"]), 1)
        self.assertTrue(out["probes"][0]["ok"], out["probes"])
        self.assertEqual(out["probes"][0]["cwd"], str(co))
        self.assertEqual((co / "sentinel.txt").read_text(), "untouched\n")

    def test_post_needs_confirm(self):
        from openmainty import serve
        repos.save(self.profile())
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_repo_profile({"repo": "fakerepo",
                                    "fastcheck": {"steps": []}})
        self.assertEqual(ctx.exception.status, 400)

    def test_post_invalid_block_refused(self):
        from openmainty import serve
        repos.save(self.profile())
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_repo_profile(
                {"repo": "fakerepo", "confirm": True,
                 "fastcheck": {"steps": [{"argv": "yarn"}]}})
        self.assertEqual(ctx.exception.status, 400)

    def test_post_unresolvable_tool_names_it(self):
        from openmainty import serve
        repos.save(self.profile())
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_repo_profile(
                {"repo": "fakerepo", "confirm": True,
                 "fastcheck": {
                     "steps": [{"argv": ["no-such-tool-xyz"],
                                "gate": "unit"}],
                     "toolchain": [str(self.tools)]}})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("no-such-tool-xyz", ctx.exception.message)

    def test_post_saves_only_fastcheck(self):
        from openmainty import serve
        repos.save(self.profile(fastcheck={
            "steps": [{"argv": ["probe-tool"], "gate": "unit"}],
            "toolchain": [str(self.tools)]}))
        (self.tools / "probe-tool").symlink_to(sys.executable)
        out = serve.api_repo_profile(
            {"repo": "fakerepo", "confirm": True,
             "fastcheck": {
                 "steps": [{"argv": ["probe-tool", "test"],
                            "gate": "unit", "timeout_s": 60}],
                 "toolchain": [str(self.tools)]}})
        self.assertEqual(out["validation"], [])
        saved = repos.load("fakerepo")
        self.assertEqual(saved["github"], "o/r")
        self.assertEqual(saved["baseBranch"], "main")
        self.assertEqual(saved["fastcheck"]["steps"][0]["argv"],
                         ["probe-tool", "test"])

    def test_p3_save_is_owner_timeline_event(self):
        # P3: saving edits unattended commands, so it lands in the
        # timeline with old/new tools+gates (never env values).
        import json
        from openmainty import serve
        (self.tools / "probe-tool").symlink_to(sys.executable)
        repos.save(self.profile(fastcheck={
            "steps": [{"argv": ["old-tool"], "gate": "install"}],
            "toolchain": [str(self.tools)]}))
        out = serve.api_repo_profile(
            {"repo": "fakerepo", "confirm": True,
             "fastcheck": {
                 "steps": [{"argv": ["probe-tool"], "gate": "unit"}],
                 "toolchain": [str(self.tools)]}})
        self.assertEqual(out["validation"], [])
        lines = (self.home / "events.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        ev = json.loads(lines[-1])
        self.assertEqual(ev["actor"], "owner")
        self.assertEqual(ev["kind"], "repo-profile")
        self.assertIn("검사 설정 변경", ev["title"])
        self.assertIn("probe-tool[unit]", ev["title"] + ev["detail"])
        self.assertIn("old-tool[install]", ev["detail"])


if __name__ == "__main__":
    unittest.main()
