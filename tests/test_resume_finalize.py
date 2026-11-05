"""Guarded same-run finalization: CLI resume and scheduler retry settle a
completed existing run into its exact linked queue item.

Offline fixtures only (temp OPENMAINTY_HOME, temp git repos, fixture
executors, mocked gh/registry). No network, no public writes, no model
calls — a recording executor proves zero worker spend on reconcile.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import cli, deps, pcconfig, repos, runner, sched, store  # noqa: E402
try:
    from tests._home import (use_temp_home, offline_discussions,  # noqa: E402
                             offline_prs, offline_muse_version)
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import (use_temp_home, offline_discussions,  # noqa: E402
                       offline_prs, offline_muse_version)


PROFILE = {
    "name": "fz", "github": "e/fz", "baseBranch": "main",
    "knowledge": [],
    "checks": [{"name": "unit", "cmd": ["fixture", "unit"], "requires": []}],
    "e2e": [], "release": {}, "announce": {},
}
DEP, TARGET = "left-pad", "1.1.0"
BRANCH = "codex/test/left-pad-1-1-0"
KEY = f"fz+{DEP}+{TARGET}+stable"
ISSUE = "dep-left-pad-1.1.0"


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    use_temp_home(test, str(Path(tmp.name) / "home"))
    # C2: the Discussions scan source runs on every sched.scan; fixture
    # repos must never reach real gh/network.
    offline_discussions(test)
    # Generic scan fixture has no PR input: stub the PR boundary too.
    offline_prs(test, [])
    # Generic behavior fixture has no real CLI version: stub metadata.
    offline_muse_version(test, "fixture-muse-0.0.0")
    return Path(tmp.name)


def make_git_repo(root: Path) -> Path:
    co = root / "co"
    co.mkdir()
    git("init", "-b", "main", cwd=co)
    git("config", "user.email", "test@localhost", cwd=co)
    git("config", "user.name", "test", cwd=co)
    (co / "package.json").write_text(json.dumps(
        {"dependencies": {DEP: "1.0.0"}}), encoding="utf-8")
    (co / "yarn.lock").write_text(
        '"left-pad@npm:1.0.0":\n  version: 1.0.0\n', encoding="utf-8")
    git("add", "-A", cwd=co)
    git("commit", "-qm", "init", cwd=co)
    return co


class DiffWriter(runner.FakeExecutor):
    """Fixture worker that leaves a real lockfile delta (a proven upgrade)."""

    def muse_step(self, step, prompt, workdir):
        code, text, usage = super().muse_step(step, prompt, workdir)
        if step == "fix" and code == 0:
            lock = Path(workdir) / "yarn.lock"
            lock.write_text(lock.read_text(encoding="utf-8")
                            + '\n"left-pad@npm:1.1.0":\n  version: 1.1.0\n',
                            encoding="utf-8")
        return code, text, usage


class Recording(runner.FakeExecutor):
    """Counts every worker call: reconcile must spend zero model calls."""

    def __init__(self, script=None):
        super().__init__(script)
        self.calls = []

    def muse_step(self, step, prompt, workdir):
        self.calls.append(("muse_step", step))
        return super().muse_step(step, prompt, workdir)

    def shell(self, cmd, cwd):
        self.calls.append(("shell", " ".join(cmd[:2])))
        return super().shell(cmd, cwd)


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = make_git_repo(self.root)
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["fz"] = str(self.co)
        pcconfig.save(pc)
        sched.configure("fz", enabled=True, interval_s=60)
        self.orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: TARGET)
        self.addCleanup(lambda: deps.set_latest_fn(self.orig))
        from unittest import mock as _mock
        patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _base(self):
        return git("rev-parse", "HEAD", cwd=self.co).strip()

    def _craft_run(self, with_delta_writer=None):
        """Hand-built scheduler-managed run (mirrors _pipeline_state)."""
        state = store.create("fz", ISSUE, f"chore(deps): {DEP} 1.0.0 -> {TARGET}",
                             self._base(), worker={})
        state.update(queue_key=KEY, dep=DEP, target=TARGET, declared="1.0.0",
                     lane="next", branch=BRANCH, base_branch="main",
                     scopes=["dependencies"])
        store.save(state)
        return state

    def _link_queued(self, run_id, status="queued", attempts=0):
        st = sched.load("fz")
        item = {"key": KEY, "kind": "dep-update", "repo": "fz",
                "dep": DEP, "target": TARGET, "lane": "next",
                "branch": BRANCH, "base_branch": "main",
                "run_issue": ISSUE, "run_id": run_id,
                "status": status, "attempts": attempts,
                "next_retry_at": None}
        st["queue"] = [i for i in st.get("queue", []) if i.get("key") != KEY]
        st["queue"].append(item)
        sched.save(st)
        return item

    def _full_run(self, executor=None):
        """Genuinely finished run via the real pipeline (fixture worker)."""
        sched.scan("fz")
        out = sched.run_due("fz", executor or DiffWriter())
        self.assertTrue(out.get("ok"), out)
        run_id = sched.load("fz")["queue"][0]["run_id"]
        run = store.load(run_id)
        self.assertTrue(all(e.get("status") == "done"
                            for e in run["steps"].values()))
        return run_id

    def _resume_ns(self, run_id, only=None):
        return argparse.Namespace(run=run_id, executor="fake",
                                  model=None, only=only)

    def test_cli_resume_success_finalizes_linked_queue(self):
        state = self._craft_run()
        run_id = state["id"]
        self._link_queued(run_id)
        ok = runner.execute(state, dict(PROFILE), DiffWriter(), self.co,
                            only=["triage", "reproduce", "analyze", "fix"])
        self.assertTrue(ok)
        rc = cli.cmd_resume(self._resume_ns(run_id))
        self.assertEqual(rc, 0)
        item = sched.load("fz")["queue"][0]
        self.assertEqual(item["status"], "waiting-review")
        self.assertTrue(item["verified"])
        self.assertEqual(item["run_id"], run_id)
        self.assertEqual(item["attempts"], 0)
        hist = [h for h in sched.load("fz")["history"]
                if h.get("key") == KEY and h.get("result") == "waiting-review"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["run_id"], run_id)

    def test_sched_retry_of_finished_run_reconciles_without_fork(self):
        run_id = self._full_run()
        rec = Recording()
        runs_before = {r["id"] for r in store.list_runs(200)}
        # Stale queue shape: run done via another entry point, item queued.
        st = sched.load("fz")
        item = next(i for i in st["queue"] if i.get("key") == KEY)
        item["status"] = "queued"
        item["verified"] = False
        sched.save(st)
        out = sched.run_due("fz", rec)
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("reconciled"))
        self.assertEqual(out.get("run_id"), run_id)
        self.assertEqual(rec.calls, [])
        self.assertEqual({r["id"] for r in store.list_runs(200)}, runs_before)
        item = sched.load("fz")["queue"][0]
        self.assertEqual(item["status"], "waiting-review")
        self.assertTrue(item["verified"])
        self.assertEqual(item["run_id"], run_id)

    def test_subset_resume_does_not_finalize(self):
        state = self._craft_run()
        run_id = state["id"]
        self._link_queued(run_id)
        rc = cli.cmd_resume(self._resume_ns(run_id, only="triage"))
        self.assertEqual(rc, 0)
        item = sched.load("fz")["queue"][0]
        self.assertEqual(item["status"], "queued")
        self.assertNotEqual(item.get("verified"), True)

    def test_stale_proof_refuses_and_leaves_unverified(self):
        run_id = self._full_run()
        st = sched.load("fz")
        stale_item = next(i for i in st["queue"] if i.get("key") == KEY)
        stale_item["status"] = "queued"
        stale_item["verified"] = False
        sched.save(st)
        n_hist = len(sched.load("fz")["history"])
        work = store.run_dir(run_id) / "work"
        pkg = work / "package.json"
        pkg.write_text(pkg.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        before = json.dumps(sched.load("fz")["queue"], sort_keys=True)
        out = sched.finalize_finished_run("fz", KEY, run_id)
        self.assertFalse(out.get("ok"))
        self.assertIn("stale", out.get("reason", ""))
        self.assertEqual(json.dumps(sched.load("fz")["queue"], sort_keys=True),
                         before)
        self.assertNotEqual(sched.load("fz")["queue"][0].get("verified"), True)
        self.assertEqual(len(sched.load("fz")["history"]), n_hist)

    def test_commit_blocked_refuses(self):
        run_id = self._full_run()
        st = sched.load("fz")
        next(i for i in st["queue"] if i.get("key") == KEY)["status"] = "queued"
        sched.save(st)
        run = store.load(run_id)
        run["commitBlocked"] = {"step": "fix", "exit": 128,
                                "stderr": "hook rejected"}
        store.save(run)
        out = sched.finalize_finished_run("fz", KEY, run_id)
        self.assertFalse(out.get("ok"))
        self.assertIn("commit blocked", out.get("reason", ""))
        self.assertEqual(sched.load("fz")["queue"][0]["status"], "queued")

    def test_wrong_identity_never_mutates(self):
        run_id = self._full_run()
        st = sched.load("fz")
        next(i for i in st["queue"] if i.get("key") == KEY)["status"] = "queued"
        sched.save(st)
        before = json.dumps(sched.load("fz")["queue"], sort_keys=True)
        out = sched.finalize_finished_run("fz", KEY, "run-deadbeef")
        self.assertFalse(out.get("ok"))
        out2 = sched.finalize_finished_run("fz", "fz+other+9.9.9+stable", run_id)
        self.assertFalse(out2.get("ok"))
        self.assertEqual(json.dumps(sched.load("fz")["queue"], sort_keys=True),
                         before)

    def test_empty_diff_settles_no_code_without_model_call(self):
        # Plain fixture worker: all steps done, but no lock/manifest delta.
        run_id = self._full_run(executor=runner.FakeExecutor())
        run = store.load(run_id)
        self.assertTrue(all(e.get("status") == "done"
                            for e in run["steps"].values()))
        st = sched.load("fz")
        next(i for i in st["queue"] if i.get("key") == KEY)["status"] = "queued"
        sched.save(st)
        runs_before = {r["id"] for r in store.list_runs(200)}
        rec = Recording()
        out = sched.run_due("fz", rec)
        self.assertTrue(out.get("ran"), out)
        self.assertEqual(rec.calls, [])
        self.assertEqual({r["id"] for r in store.list_runs(200)}, runs_before)
        item = sched.load("fz")["queue"][0]
        self.assertEqual(item["run_id"], run_id)
        self.assertEqual(item["status"], "waiting-review")
        self.assertFalse(item.get("verified"))

    def test_cancelled_never_auto_resurrects(self):
        run_id = self._full_run()
        self.assertEqual(sched.cancel("fz", KEY)["status"], "cancelled")
        runs_before = {r["id"] for r in store.list_runs(200)}
        out = sched.finalize_finished_run("fz", KEY, run_id)
        self.assertFalse(out.get("ok"))
        self.assertIn("cancelled", out.get("reason", ""))
        keyed = sched.run_due("fz", Recording(), key=KEY)
        self.assertFalse(keyed.get("ran"))
        sched.scan("fz")
        item = sched.load("fz")["queue"][0]
        self.assertEqual(item["status"], "cancelled")
        self.assertEqual(item["run_id"], run_id)
        self.assertEqual({r["id"] for r in store.list_runs(200)}, runs_before)

    def test_reasoned_correction_finalizes_cancelled_run(self):
        run_id = self._full_run()
        st = sched.load("fz")
        attempts_before = next(i for i in st["queue"]
                               if i.get("key") == KEY)["attempts"]
        self.assertEqual(sched.cancel("fz", KEY)["status"], "cancelled")
        reason = "erroneous workaround cancel; run actually completed fresh"
        # Empty reason must not correct.
        self.assertFalse(sched.finalize_finished_run(
            "fz", KEY, run_id, correct_cancelled=True,
            reason="  ", actor="cli-reconcile").get("ok"))
        out = sched.finalize_finished_run(
            "fz", KEY, run_id, correct_cancelled=True,
            reason=reason, actor="cli-reconcile")
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("corrected_from"), "cancelled")
        item = sched.load("fz")["queue"][0]
        self.assertEqual(item["status"], "waiting-review")
        self.assertTrue(item["verified"])
        self.assertEqual(item["run_id"], run_id)
        self.assertEqual(item["attempts"], attempts_before)
        hist = [h for h in sched.load("fz")["history"]
                if h.get("key") == KEY]
        self.assertTrue(any(h.get("result") == "cancelled" for h in hist))
        fixed = [h for h in hist
                 if h.get("result") == "waiting-review"
                 and h.get("corrected_from") == "cancelled"]
        self.assertEqual(len(fixed), 1)
        self.assertEqual(fixed[0]["correction_reason"], reason[:300])
        self.assertEqual(fixed[0]["run_id"], run_id)
        run = store.load(run_id)
        corrected = [i for i in run.get("interventions", [])
                     if i.get("kind") == "cancel-corrected"]
        self.assertEqual(len(corrected), 1)
        self.assertIn("cancelled", corrected[0]["detail"])

    def test_refinalize_is_idempotent(self):
        run_id = self._full_run()
        st = sched.load("fz")
        next(i for i in st["queue"] if i.get("key") == KEY)["status"] = "queued"
        sched.save(st)
        first = sched.finalize_finished_run("fz", KEY, run_id)
        self.assertTrue(first.get("ok"))
        n_hist = len(sched.load("fz")["history"])
        second = sched.finalize_finished_run("fz", KEY, run_id)
        self.assertTrue(second.get("ok"))
        self.assertEqual(second.get("note"), "already final")
        self.assertEqual(len(sched.load("fz")["history"]), n_hist)


class LateQuestionBarrierTests(unittest.TestCase):
    """A post-triage open question blocks EVERY resume entry point.

    Regression for the 21:35Z automatic dispatch that resumed a run
    whose triage disposition was ACTIONABLE but whose analyze step was
    blocked on an operator question: the old gate read the triage
    verdict first and returned None, so the daemon fabricated a
    "human-resume" intervention and spent model calls on fix. Now any
    CURRENT unanswered question blocks scheduler/CLI/UI resume with
    zero worker spend, no attempt increment, and no fork — only a real
    recorded answer unblocks the SAME run.
    """

    def setUp(self):
        self.root = make_home(self)
        self.co = make_git_repo(self.root)
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["fz"] = str(self.co)
        pcconfig.save(pc)
        sched.configure("fz", enabled=True, interval_s=60)
        self.orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: TARGET)
        self.addCleanup(lambda: deps.set_latest_fn(self.orig))
        from unittest import mock as _mock
        patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _question_run(self):
        """733 shape: ACTIONABLE triage, analyze blocked on a question."""
        state = store.create("fz", ISSUE,
                             f"chore(deps): {DEP} 1.0.0 -> {TARGET}",
                             git("rev-parse", "HEAD", cwd=self.co).strip(),
                             worker={})
        state.update(queue_key=KEY, dep=DEP, target=TARGET,
                     declared="1.0.0", lane="next", branch=BRANCH,
                     base_branch="main", scopes=["dependencies"])
        store.save(state)
        runner.set_disposition(state, "ACTIONABLE", "fixture actionable")
        state = store.load(state["id"])
        state["waitReason"] = ("question: Should the coupled tool also be "
                               "bumped in lockstep, or only the target?")
        state["question_step"] = "analyze"
        # Preserve the canonical store.create steps; update only the two
        # phases this fixture exercises (a replaced steps map breaks
        # invalidate_stale_verifications for reasons unrelated to the gate).
        state["steps"]["triage"] = {"status": "done", "exit_code": 0}
        state["steps"]["analyze"] = {"status": "blocked", "exit_code": 0,
                                     "note": "waiting on operator"}
        store.save(state)
        st = sched.load("fz")
        item = {"key": KEY, "kind": "dep-update", "repo": "fz",
                "dep": DEP, "target": TARGET, "lane": "next",
                "branch": BRANCH, "base_branch": "main",
                "run_issue": ISSUE, "run_id": state["id"],
                "status": "queued", "attempts": 1,
                "next_retry_at": None}
        st["queue"] = [i for i in st.get("queue", [])
                       if i.get("key") != KEY]
        st["queue"].append(item)
        sched.save(st)
        return state["id"]

    def _item(self):
        return next(i for i in sched.load("fz")["queue"]
                    if i.get("key") == KEY)

    def _human_resumes(self, run_id):
        return [i for i in store.load(run_id).get("interventions", [])
                if i.get("kind") == "human-resume"]

    def test_gate_reads_current_question_under_actionable(self):
        run_id = self._question_run()
        self.assertEqual(runner.resume_gate(store.load(run_id)), "waiting")
        # Terminal duplicate still outranks a stale question text.
        dup = store.load(run_id)
        runner.set_disposition(dup, "DUPLICATE", "confirmed duplicate")
        dup = store.load(run_id)
        self.assertTrue(dup.get("waitReason", "").startswith("question:"))
        self.assertEqual(runner.resume_gate(dup), "duplicate")
        # Legacy shape without any disposition is unchanged.
        legacy = {"waitReason": "question: old wait text"}
        self.assertEqual(runner.resume_gate(legacy), "waiting")
        self.assertIsNone(runner.resume_gate(
            {"disposition": {"kind": "ACTIONABLE"}}))

    def test_cli_resume_spends_zero_calls_and_no_fake_human_audit(self):
        run_id = self._question_run()
        rec = Recording()
        ns = argparse.Namespace(run=run_id, executor="fake", model=None,
                                only=None)
        with unittest.mock.patch.object(runner, "make_executor",
                                        return_value=rec):
            rc = cli.cmd_resume(ns)
        self.assertEqual(rc, 1)
        self.assertEqual(rec.calls, [])
        reloaded = store.load(run_id)
        self.assertTrue(reloaded.get("waitReason", "").startswith(
            "question:"))
        self.assertEqual(self._item()["attempts"], 1)
        self.assertEqual(self._item()["run_id"], run_id)
        self.assertEqual(self._human_resumes(run_id), [])

    def test_sched_retick_blocks_without_fork_or_spend(self):
        run_id = self._question_run()
        rec = Recording()
        out = sched.run_due("fz", rec)
        self.assertFalse(out.get("ran"))
        self.assertIn("operator", out.get("reason", ""))
        self.assertEqual(rec.calls, [])
        item = self._item()
        self.assertEqual(item["run_id"], run_id)
        self.assertEqual(item["attempts"], 1)
        self.assertIn("operator", item.get("blocker", ""))
        self.assertTrue(store.load(run_id).get("waitReason", "").startswith(
            "question:"))

    def test_recorded_answer_unblocks_same_run(self):
        run_id = self._question_run()
        store.record_answer(run_id, "Bump the coupled tool in lockstep; "
                                   "repo policy requires aligned floors.")
        answered = store.load(run_id)
        self.assertIsNone(answered.get("waitReason"))
        self.assertEqual(runner.resume_gate(answered), None)
        rec = Recording()
        with unittest.mock.patch.object(runner, "make_executor",
                                        return_value=rec):
            rc = cli.cmd_resume(argparse.Namespace(
                run=run_id, executor="fake", model=None, only=None))
        self.assertEqual(rc, 0)
        self.assertTrue(rec.calls)
        self.assertEqual(self._item()["run_id"], run_id)
        kinds = [i.get("kind") for i in
                 store.load(run_id).get("interventions", [])]
        self.assertIn("answer", kinds)
        # The earlier blocked resumes never fabricated a human resume.
        self.assertNotIn("human-resume", kinds)


if __name__ == "__main__":
    unittest.main()
