"""Model selection: openmainty run/resume/doctor can pick the muse model.

Uses FakeExecutor + isolated OPENMAINTY_HOME where possible so no
subscription quota is spent. Run with: python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import cli, muse, runner  # noqa: E402
try:
    from tests._home import offline_muse_version  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import offline_muse_version  # noqa: E402


class ModelSelectTests(unittest.TestCase):
    def test_resolve_default(self):
        self.assertEqual(muse.resolve_model(None), muse.DEFAULT_MODEL)
        self.assertEqual(muse.resolve_model(""), muse.DEFAULT_MODEL)
        self.assertEqual(muse.resolve_model("  "), muse.DEFAULT_MODEL)

    def test_resolve_custom_stripped(self):
        self.assertEqual(muse.resolve_model("  muse-spark  "), "muse-spark")

    def test_build_cmd_default_and_custom(self):
        default_cmd = muse.build_cmd("hi")
        self.assertIn(muse.DEFAULT_MODEL, default_cmd)
        custom_cmd = muse.build_cmd("hi", model="muse-spark")
        self.assertIn("muse-spark", custom_cmd)
        self.assertNotIn(muse.DEFAULT_MODEL, custom_cmd)
        # contributor flag still independent of model choice
        off_cmd = muse.build_cmd("hi", model="muse-spark", contributor=False)
        self.assertIn("off", off_cmd)

    def test_agent_info_model(self):
        # Model choice is the assertion; version string is fixture input.
        # Real agent_info model/provider/billing logic stays intact.
        offline_muse_version(self, "fixture-muse-0.0.0")
        self.assertEqual(muse.agent_info()["model"], muse.DEFAULT_MODEL)
        self.assertEqual(muse.agent_info(model="muse-spark")["model"], "muse-spark")

    def test_make_executor_model(self):
        self.assertEqual(runner.make_executor("muse").model, muse.DEFAULT_MODEL)
        ex = runner.make_executor("muse", model="muse-spark")
        self.assertEqual(ex.model, "muse-spark")

    def test_cli_parses_model(self):
        cases = [
            ["run", "--repo", "r", "--issue", "1", "--model", "muse-spark"],
            ["resume", "--run", "run-1", "--model", "muse-spark"],
            ["watch", "--repo", "r", "--model", "muse-spark"],
            ["doctor", "--model", "muse-spark"],
        ]
        for argv in cases:
            args = cli.build_parser().parse_args(argv)
            self.assertEqual(args.model, "muse-spark")


if __name__ == "__main__":
    unittest.main()
