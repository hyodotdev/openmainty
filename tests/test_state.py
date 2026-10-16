"""Durable coverage: run state transitions, resume, failure, dedup.

Uses FakeExecutor + an isolated OPENMAINTY_HOME so no subscription quota is
spent and no target repo is touched. Run with:
  python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import pcconfig, repos, runner, store  # noqa: E402

PROFILE = {
    "name": "fakerepo",
    "github": "example/fakerepo",
    "baseBranch": "main",
    "knowledge": [],
    "checks": [{"name": "unit", "cmd": ["make", "unit"], "requires": []}],
    "e2e": [],
    "release": {"notes": "CHANGELOG.md"},
    "announce": {"medium": True, "discord": False, "slack": False},
}


class HomeMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["OPENMAINTY_HOME"] = str(Path(self.tmp.name) / "home")
        self.checkout = Path(self.tmp.name) / "checkout"
        self.checkout.mkdir()
        (self.checkout / "README.md").write_text("# fake\n")
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["fakerepo"] = str(self.checkout)
        pcconfig.save(pc)

    def tearDown(self):
        os.environ.pop("OPENMAINTY_HOME", None)
        os.environ.pop("OPENMAINTY_FAKE_QUOTA", None)

    def make_run(self):
        state = store.create("fakerepo", "1", "fake issue", "base0", {"worker": "test"})
        store.claim("fakerepo", "1", state["id"])
        return state


class StateTransitionTests(HomeMixin):
    def test_full_pass_with_fake(self):
        state = self.make_run()
        ok = runner.execute(state, repos.load("fakerepo"), runner.FakeExecutor(), self.checkout)
        self.assertTrue(ok)
        state = store.load(state["id"])
        for step in store.STEP_ORDER:
            self.assertEqual(state["steps"][step]["status"], "done", step)
        for key in ("pr", "release_notes", "medium", "discord", "slack"):
            self.assertTrue((store.run_dir(state["id"]) / f"{key}.md").exists(), key)
        # usage honesty: nothing may look like a measured token count
        for step, entry in state["steps"].items():
            usage = entry.get("usage") or {}
            if isinstance(usage, dict) and "tokens" in usage:
                self.assertIsNone(usage["tokens"], step)

    def test_failure_stops_and_resume_skips_done(self):
        state = self.make_run()
        ex = runner.FakeExecutor(script={"fix": [(1, "fake fix boom")]})
        ok = runner.execute(state, repos.load("fakerepo"), ex, self.checkout)
        self.assertFalse(ok)
        state = store.load(state["id"])
        self.assertEqual(state["steps"]["fix"]["status"], "failed")
        self.assertEqual(state["steps"]["review"]["status"], "pending")
        # resume with a healthy executor: completed steps must not re-run
        ex2 = runner.FakeExecutor()
        ok = runner.execute(state, repos.load("fakerepo"), ex2, self.checkout)
        self.assertTrue(ok)
        triage_calls = [c for c in ex2.calls if c == "triage"]
        self.assertEqual(triage_calls, [], "resume must not repeat completed triage")
        self.assertIn("fix", ex2.calls, "failed fix must be retried")

    def test_stale_verification_invalidated_after_code_change(self):
        state = self.make_run()
        self.assertTrue(runner.execute(state, repos.load("fakerepo"), runner.FakeExecutor(), self.checkout))
        state = store.load(state["id"])
        # simulate: fix produced new code after verification passed
        state["workSeq"] = state.get("workSeq", 0) + 1
        state["workCommit"] = "copyseq-changed"
        store.save(state)
        ex = runner.FakeExecutor()
        runner.execute(state, repos.load("fakerepo"), ex, self.checkout)
        state = store.load(state["id"])
        # reproduce/review re-run as worker steps; e2e re-runs its shell check
        for step in ("reproduce", "review"):
            self.assertIn(step, ex.calls, f"{step} must re-run after code change")
        self.assertIn("shell:make unit", ex.calls, "e2e check must re-run after code change")
        self.assertNotIn("triage", ex.calls)
        self.assertNotIn("fix", ex.calls)

    def test_duplicate_run_refused_then_allowed(self):
        state = self.make_run()
        self.assertEqual(store.active_for("fakerepo", "1"), state["id"])
        # a completed run releases the claim
        self.assertTrue(runner.execute(state, repos.load("fakerepo"), runner.FakeExecutor(), self.checkout))
        store.release("fakerepo", "1", state["id"])
        self.assertIsNone(store.active_for("fakerepo", "1"))

    def test_missing_capability_blocks_e2e(self):
        profile = dict(PROFILE)
        profile["e2e"] = [{"name": "ios-device", "cmd": ["make", "e2e-ios"], "requires": ["ios_device"]}]
        state = self.make_run()
        # run everything up to e2e first
        ex = runner.FakeExecutor()
        runner.execute(state, profile, ex, self.checkout,
                       only=["triage", "reproduce", "analyze", "fix", "review"])
        ok = runner.execute(state, profile, ex, self.checkout, only=["e2e"])
        self.assertFalse(ok)
        state = store.load(state["id"])
        self.assertEqual(state["steps"]["e2e"]["status"], "blocked")
        self.assertIn("ios_device", state["steps"]["e2e"]["note"])

    def test_quota_wait_records_reason_and_resumes(self):
        state = self.make_run()
        os.environ["OPENMAINTY_FAKE_QUOTA"] = "fix"
        with self.assertRaises(runner.QuotaHit):
            runner.execute(state, repos.load("fakerepo"), runner.FakeExecutor(), self.checkout)
        state = store.load(state["id"])
        self.assertEqual(state["waitReason"], "subscription-quota at step fix")
        self.assertTrue(any(i["kind"] == "quota-wait" for i in state["interventions"]))
        del os.environ["OPENMAINTY_FAKE_QUOTA"]
        ok = runner.execute(state, repos.load("fakerepo"), runner.FakeExecutor(), self.checkout)
        self.assertTrue(ok)

    def test_review_without_valid_findings_skips_apply(self):
        state = self.make_run()
        ex = runner.FakeExecutor(script={"review": [(0, "VALID: none. All good.")]})
        self.assertTrue(runner.run_review(
            state, repos.load("fakerepo"), ex, self.checkout, "body"))
        self.assertNotIn("apply_review", ex.calls)
        state = store.load(state["id"])
        self.assertEqual(state["steps"]["review"]["status"], "done")

    def test_review_with_valid_finding_applies(self):
        state = self.make_run()
        ex = runner.FakeExecutor(
            script={"review": [(0, "VALID: fix the stale link")]})
        self.assertTrue(runner.run_review(
            state, repos.load("fakerepo"), ex, self.checkout, "body"))
        self.assertIn("apply_review", ex.calls)

    def test_shell_failure_fails_step_with_exit_code(self):
        profile = dict(PROFILE)
        profile["checks"] = [{"name": "unit", "cmd": ["make", "unit"], "requires": []}]
        state = self.make_run()
        ex = runner.FakeExecutor(script={"shell:make unit": [(2, "boom")]})
        runner.execute(state, profile, ex, self.checkout,
                       only=["triage", "reproduce", "analyze", "fix", "review"])
        ok = runner.execute(state, profile, ex, self.checkout, only=["e2e"])
        self.assertFalse(ok)
        state = store.load(state["id"])
        self.assertEqual(state["steps"]["e2e"]["status"], "failed")
        self.assertEqual(state["steps"]["e2e"]["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
