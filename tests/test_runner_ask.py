"""Unit coverage: the ASK pause protocol and answer context.

No quota, no network: FakeExecutor + isolated OPENMAINTY_HOME.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import planner, runner, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

PROFILE = {"name": "fakerepo", "github": "example/fakerepo",
           "baseBranch": "main", "knowledge": [], "checks": [], "e2e": []}


class AskProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()

    def _state(self):
        return store.create("fakerepo", "1", "t", "base0", {})

    def test_extract_ask(self):
        self.assertEqual(runner.extract_ask("a\nOPENMAINTY_ASK: which one?\nzzz"),
                         "which one?")
        self.assertIsNone(runner.extract_ask("no marker here"))
        self.assertIsNone(runner.extract_ask(""))
        self.assertIsNone(runner.extract_ask("OPENMAINTY_ASK:"))
        self.assertEqual(runner.extract_ask("OPENMAINTY_ASK: first\nOPENMAINTY_ASK: second"),
                         "first")

    def test_pause_marks_blocked_and_stops(self):
        state = self._state()
        ex = runner.FakeExecutor(
            script={"triage": [(0, "thinking\nOPENMAINTY_ASK: use v1 or v2?")]})
        self.assertFalse(runner.run_worker_step(state, PROFILE, "triage", ex,
                                                self.work, "body"))
        entry = state["steps"]["triage"]
        self.assertEqual(entry["status"], "blocked")
        self.assertIn("use v1 or v2?", entry["note"])
        self.assertEqual(state["waitReason"], "question: use v1 or v2?")
        self.assertEqual(state["question_step"], "triage")
        kinds = [i["kind"] for i in state["interventions"]]
        self.assertIn("question", kinds)
        reloaded = store.load(state["id"])
        self.assertEqual(reloaded["waitReason"], "question: use v1 or v2?")

    def test_answers_reach_worker_prompt(self):
        seen = {}

        class Rec(runner.FakeExecutor):
            def muse_step(self, step, prompt, workdir):
                seen[step] = prompt
                return super().muse_step(step, prompt, workdir)

        state = self._state()
        state["answers"] = [{"q": "use v1 or v2?", "a": "v2"}]
        self.assertTrue(runner.run_worker_step(state, PROFILE, "analyze",
                                               Rec(), self.work, "body", context="ctx"))
        self.assertIn("Operator answers to earlier questions", seen["analyze"])
        self.assertIn("use v1 or v2?", seen["analyze"])
        self.assertIn("v2", seen["analyze"])
        self.assertIn("ctx", seen["analyze"])

    def test_planner_carries_ask_hint(self):
        for step in ("triage", "reproduce", "analyze", "fix"):
            prompt = planner.prompt_for(step, PROFILE, "1", "t", "body")
            self.assertIn("OPENMAINTY_ASK", prompt, step)

    def test_planner_shows_answers_to_triage_and_reproduce(self):
        for step in ("triage", "reproduce"):
            prompt = planner.prompt_for(step, PROFILE, "1", "t", "body",
                                        context="Q: same-app?\nA: yes")
            self.assertIn("Q: same-app?", prompt, step)
            self.assertIn("do not re-ask", prompt, step)
        plain = planner.prompt_for("triage", PROFILE, "1", "t", "body")
        self.assertNotIn("do not re-ask", plain)

    def test_planner_fix_prefers_fast_checks(self):
        prompt = planner.prompt_for("fix", PROFILE, "1", "t", "body")
        self.assertIn("fast checks only", prompt)
        self.assertIn("do not run device/simulator", prompt)

    def test_planner_forbids_external_posts(self):
        for step in ("triage", "reproduce", "analyze", "fix", "review",
                     "apply_review", "e2e", "release", "announce"):
            prompt = planner.prompt_for(step, PROFILE, "1", "t", "body")
            self.assertIn("cannot post", prompt, step)

    def test_normal_output_unaffected(self):
        state = self._state()
        self.assertTrue(runner.run_worker_step(state, PROFILE, "triage",
                                               runner.FakeExecutor(), self.work, "b"))
        self.assertEqual(state["steps"]["triage"]["status"], "done")
        self.assertIsNone(state["waitReason"])

    def test_live_log_written_during_step(self):
        from openmainty.util import Cancelled

        class Abort(runner.FakeExecutor):
            def muse_step_stream(self, step, prompt, workdir, on_delta, timeout=1200):
                on_delta("streamed-part")
                raise Cancelled("stop")

        state = self._state()
        with self.assertRaises(Cancelled):
            runner.run_worker_step(state, PROFILE, "triage", Abort(), self.work, "b")
        live = store.run_dir(state["id"]) / "triage.live.log.txt"
        self.assertIn("streamed-part", live.read_text(encoding="utf-8"))

    def test_empty_output_falls_back_to_live_log(self):
        class Silent(runner.FakeExecutor):
            def muse_step_stream(self, step, prompt, workdir, on_delta, timeout=1200):
                on_delta("partial work note")
                return 124, "", {"partial": True}

        state = self._state()
        self.assertFalse(runner.run_worker_step(state, PROFILE, "triage", Silent(),
                                                self.work, "b"))
        log = store.run_dir(state["id"]) / "triage.log.txt"
        self.assertIn("partial work note", log.read_text(encoding="utf-8"))

    def test_ask_inside_json_envelope_pauses(self):
        import json as _json
        envelope = _json.dumps(
            {"schema_version": 1, "record_type": "event",
             "payload": {"kind": "run_terminal",
                         "text": "thinking\nOPENMAINTY_ASK: stop or pause?"}})
        state = self._state()
        ex = runner.FakeExecutor(script={"analyze": [(0, envelope)]})
        self.assertFalse(runner.run_worker_step(state, PROFILE, "analyze", ex,
                                                self.work, "body"))
        self.assertEqual(state["waitReason"], "question: stop or pause?")

    def _git_work(self):
        from openmainty.util import run_cmd
        d = Path(self.tmp.name) / "gwork"
        d.mkdir()
        run_cmd(["git", "init", "-q", str(d)])
        run_cmd(["git", "-C", str(d), "config", "user.email", "t@t"])
        run_cmd(["git", "-C", str(d), "config", "user.name", "t"])
        (d / "f.txt").write_text("x")
        run_cmd(["git", "-C", str(d), "add", "."])
        run_cmd(["git", "-C", str(d), "commit", "-qm", "init"])
        return d

    def _live(self, script):
        ex = runner.FakeExecutor(script=script)
        ex.kind = "muse"
        return ex

    def test_empty_fix_fails_for_live_executor(self):
        work = self._git_work()
        state = self._state()
        ex = self._live({"fix": [(0, "FILES CHANGED: none\nTEST ADDED: none")]})
        self.assertFalse(runner.run_worker_step(state, PROFILE, "fix", ex, work, "b"))
        self.assertEqual(state["steps"]["fix"]["status"], "failed")
        self.assertIn("changed no files", state["steps"]["fix"]["note"])

    def test_nonempty_fix_passes_guard(self):
        work = self._git_work()
        (work / "fix.txt").write_text("y")
        state = self._state()
        ex = self._live({"fix": [(0, "FILES CHANGED: fix.txt\nTEST ADDED: t")]})
        self.assertTrue(runner.run_worker_step(state, PROFILE, "fix", ex, work, "b"))
        self.assertEqual(state["steps"]["fix"]["status"], "done")

    def test_noise_only_changes_do_not_pass_guard(self):
        work = self._git_work()
        (work / "node_modules").mkdir()
        (work / "node_modules" / "pkg").write_text("z")
        self.assertFalse(runner._worktree_changed(work))
        (work / "real.txt").write_text("y")
        self.assertTrue(runner._worktree_changed(work))


if __name__ == "__main__":
    unittest.main()
