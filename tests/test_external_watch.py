"""External-watch deferred outcome: settled no-change waits, not failures.

Covers the run-6a243a45 defect (conditional no-change marked failed and
retried) without hardcoding run IDs or dependency names: a structured
watch block settles the same run+key with reason/evidence/trigger,
scans cheaply compare watched inputs (unchanged -> zero model calls,
changed reliable evidence -> same-run reevaluation, failed evidence ->
stays deferred), unrelated queued work still executes, exact-key dispatch
refuses watch items, and the outcome is never a verified upgrade.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import deps, pcconfig, repos, runner, sched, store  # noqa: E402
try:
    from tests._home import (use_temp_home, offline_discussions,  # noqa: E402
                             offline_prs, offline_muse_version)
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import (use_temp_home, offline_discussions,  # noqa: E402
                       offline_prs, offline_muse_version)


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


WATCH_TEXT = """No migration applied. The tree is exactly at the recorded base.

FILES CHANGED: none.

WATCH-DECISION: DEFER
WATCH-REASON: settled operator decision: no migration until stable formal deprecation or real demand
WATCH-EVIDENCE: stable SDK material.dart deprecations 0; issue comments 0; material_ui 1.7.0 needs flutter >=3.47.0
WATCH-TRIGGER: stable material.dart deprecation OR new demand on the tracked issue
"""

VAGUE_TEXT = """No migration applied. The tree is exactly at the recorded base.

FILES CHANGED: none. This is a deferred-migration decision, not a behavior change.
Re-entry stays trigger-gated.
"""


class ParseWatchBlockTests(unittest.TestCase):
    def test_valid_block_parses(self):
        got = runner.parse_watch_block(WATCH_TEXT)
        self.assertIsNotNone(got)
        self.assertIn("deprecation", got["reason"])
        self.assertIn("deprecations 0", got["evidence"])
        self.assertIn("OR", got["trigger"])

    def test_vague_no_change_never_qualifies(self):
        self.assertIsNone(runner.parse_watch_block(VAGUE_TEXT))
        self.assertIsNone(runner.parse_watch_block("FILES CHANGED: none\n"))
        self.assertIsNone(runner.parse_watch_block(""))

    def test_missing_fields_rejected(self):
        self.assertIsNone(runner.parse_watch_block(
            "WATCH-DECISION: DEFER\nWATCH-REASON: why\n"))
        self.assertIsNone(runner.parse_watch_block(
            "WATCH-DECISION: DEFER\nWATCH-REASON: why\nWATCH-EVIDENCE: what\n"))
        self.assertIsNone(runner.parse_watch_block(
            "WATCH-REASON: why\nWATCH-EVIDENCE: what\nWATCH-TRIGGER: when\n"))

    def test_non_defer_decision_rejected(self):
        self.assertIsNone(runner.parse_watch_block(
            "WATCH-DECISION: PROCEED\nWATCH-REASON: why\nWATCH-EVIDENCE: what\nWATCH-TRIGGER: when\n"))


class FixWatchSettlementTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)

    def _git_run(self, fix_exit=0):
        from openmainty.util import run_cmd
        co = self.root / "co"
        co.mkdir()
        (co / "a.txt").write_text("v1\n")
        self.assertEqual(run_cmd(["git", "-C", str(co), "init", "-q"])[0], 0)
        self.assertEqual(run_cmd(["git", "-C", str(co), "add", "."])[0], 0)
        self.assertEqual(run_cmd(["git", "-C", str(co), "-c", "user.name=t",
                                  "-c", "user.email=t@t", "commit", "-qm", "base"])[0], 0)
        state = store.create("r", "1", "t", "base0", {"worker": "test"})
        store.mark(state, "fix", "done" if fix_exit == 0 else "failed",
                   exit_code=fix_exit, evidence=[], usage={})
        return state, co

    def test_declared_watch_settles_clean_tree(self):
        state, work = self._git_run()
        self.assertTrue(runner.settle_fix_watch_if_declared(state, work, WATCH_TEXT))
        self.assertEqual(state["steps"]["fix"]["status"], "done")
        self.assertIn("external-watch", state["steps"]["fix"]["note"])
        self.assertEqual(runner.resume_gate(state), "watch")

    def test_vague_no_change_does_not_settle(self):
        state, work = self._git_run()
        store.mark(state, "fix", "failed", exit_code=0, evidence=[], usage={},
                   note="fix step changed no files")
        self.assertFalse(runner.settle_fix_watch_if_declared(state, work, VAGUE_TEXT))
        self.assertIsNone(runner.resume_gate(state))
        self.assertEqual(state["steps"]["fix"]["status"], "failed")

    def test_nonzero_exit_does_not_settle(self):
        state, work = self._git_run(fix_exit=1)
        self.assertFalse(runner.settle_fix_watch_if_declared(state, work, WATCH_TEXT))
        self.assertIsNone(runner.resume_gate(state))

    def test_dirty_tree_does_not_settle(self):
        state, work = self._git_run()
        (work / "b.txt").write_text("new\n")
        self.assertFalse(runner.settle_fix_watch_if_declared(state, work, WATCH_TEXT))
        self.assertIsNone(runner.resume_gate(state))


class SettleWatchGateTests(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def _run(self):
        return store.create("r", "1", "t", "base0", {"worker": "test"})

    def test_settle_writes_structured_record_and_gate(self):
        state = self._run()
        watch = runner.settle_external_watch(
            state, reason="wait for stable deprecation",
            evidence="SDK deprecations 0; comments 0",
            trigger="stable deprecation OR issue demand",
            inputs={"issue": {"number": "1", "comments": 0}},
            settled_by="test")
        self.assertEqual(state["disposition"]["kind"], "EXTERNAL-WATCH")
        self.assertTrue(state["watch"]["active"])
        self.assertEqual(state["watch"]["inputs"]["issue"]["comments"], 0)
        self.assertTrue(state["watch"]["last_check"])
        self.assertTrue(state["watch"]["next_check"])
        self.assertTrue((state.get("waitReason") or "").startswith("watch:"))
        self.assertEqual(runner.resume_gate(state), "watch")
        self.assertEqual(watch["reason"], "wait for stable deprecation")

    def test_settle_validates_inputs(self):
        state = self._run()
        with self.assertRaises(ValueError):
            runner.settle_external_watch(state, reason="", evidence="e",
                                         trigger="t")
        with self.assertRaises(ValueError):
            runner.settle_external_watch(state, reason="r", evidence="",
                                         trigger="t")
        with self.assertRaises(ValueError):
            runner.settle_external_watch(state, reason="r", evidence="e",
                                         trigger="")
        self.assertIsNone(runner.resume_gate(state))

    def test_legacy_watch_wait_text_gates(self):
        state = self._run()
        state["waitReason"] = "watch: waiting on upstream"
        store.save(state)
        self.assertEqual(runner.resume_gate(state), "watch")
        runner.enforce_gate(state, "watch")
        self.assertTrue(store.load(state["id"])["waitReason"].startswith("watch:"))

    def test_execute_makes_no_model_calls(self):
        state = self._run()
        runner.settle_external_watch(state, reason="r", evidence="e", trigger="t")
        profile = {"name": "r", "github": "e/r", "baseBranch": "main"}
        work = Path(store.run_dir(state["id"])) / "work"
        work.mkdir()
        ex = runner.FakeExecutor()
        ok = runner.execute(state, profile, ex, work, body="")
        self.assertFalse(ok)
        self.assertEqual(ex.calls, [])
        reloaded = store.load(state["id"])
        self.assertEqual(runner.resume_gate(reloaded), "watch")


class SchedWatchOpTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps({"dependencies": {"alpha": "1.0.0"}}))
        repos.save({"name": "kk", "github": "e/kk", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["kk"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: {"alpha": "2.0.0"}[ident])
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        patcher = mock.patch("openmainty.cli._open_issues", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        sched.configure("kk", enabled=True, interval_s=60)
        sched.scan("kk")

    def _queue_key(self):
        return sched.load("kk")["queue"][0]["key"]

    def _run_item_to_watch(self, attempts=2):
        """A supported-ops path to a failed run: scan, fake-execute, settle."""
        key = self._queue_key()
        out = sched.run_due("kk", runner.FakeExecutor(), key=key)
        self.assertTrue(out["ran"] or out.get("item"), out)
        run_id = sched.load("kk")["queue"][0]["run_id"]
        self.assertTrue(run_id)
        with sched.locked("kk"):
            st = sched.load("kk")
            item = next(i for i in st["queue"] if i["key"] == key)
            item["attempts"] = attempts
            item["status"] = "queued"
            item["next_retry_at"] = None
            sched.save(st)
        return key, run_id

    def test_set_watch_preserves_identity_and_attempts(self):
        key, run_id = self._run_item_to_watch()
        # The op takes no executor at all: settling a watch cannot spend model calls.
        out = sched.set_external_watch(
            "kk", key, reason="toolchain incompatible; wait for peers",
            evidence="fix confirmed incompatibility; manifests unchanged",
            trigger="compatible peer release",
            inputs={"registry": {"alpha": "2.0.0"}})
        self.assertTrue(out["ok"], out)
        st = sched.load("kk")
        item = next(i for i in st["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "waiting-review")
        self.assertFalse(item.get("verified"))
        self.assertEqual(item["attempts"], 2, "attempts preserved, never reset")
        self.assertEqual(item["run_id"], run_id, "same run, never forked")
        self.assertTrue(item.get("watch"))
        hist = [h for h in st["history"] if h["key"] == key]
        self.assertTrue(hist, "history records the deferral")
        self.assertFalse(sched.worker_lease_held(), "lease released after op")
        run = store.load(run_id)
        self.assertEqual(runner.resume_gate(run), "watch")

    def test_set_watch_unknown_key_refused(self):
        out = sched.set_external_watch("kk", "kk+nope+9.9.9+stable",
                                       reason="r", evidence="e", trigger="t")
        self.assertFalse(out["ok"])
        self.assertIn("unknown key", out["reason"])

    def test_keyed_dispatch_refuses_watch_without_running_other_key(self):
        key, _ = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        ex = runner.FakeExecutor()
        out = sched.run_due("kk", ex, key=key)
        self.assertFalse(out["ran"])
        self.assertIn("watch", out["reason"])
        self.assertEqual(ex.calls, [])
        st = sched.load("kk")
        self.assertEqual(st["queue"][0]["attempts"], 2)

    def test_no_key_dispatch_runs_nothing_with_zero_model_calls(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        ex = runner.FakeExecutor()
        out = sched.run_due("kk", ex)
        self.assertFalse(out["ran"])
        self.assertEqual(ex.calls, [], "a deferred queue spends zero model calls")
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "waiting-review")
        self.assertEqual(item["attempts"], 2)
        self.assertEqual(runner.resume_gate(store.load(run_id)), "watch")

    def test_unchanged_inputs_stay_deferred_with_zero_model_calls(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        ex = runner.FakeExecutor()
        out = sched.refresh_external_watch("kk", key, {"registry": {"alpha": "2.0.0"}})
        self.assertTrue(out["checked"])
        self.assertFalse(out["changed"])
        self.assertEqual(ex.calls, [])
        run = store.load(run_id)
        self.assertEqual(runner.resume_gate(run), "watch")
        self.assertTrue(run["watch"]["last_check"])
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "waiting-review")
        self.assertEqual(item["attempts"], 2)

    def test_changed_reliable_evidence_lifts_same_run(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        out = sched.refresh_external_watch("kk", key, {"registry": {"alpha": "3.0.0"}})
        self.assertTrue(out["changed"])
        run = store.load(run_id)
        self.assertIsNone(runner.resume_gate(run), "lifted run is resumable again")
        self.assertFalse(run["watch"]["active"])
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["run_id"], run_id, "same run eligible, not a new run")
        self.assertEqual(item["attempts"], 2, "attempts preserved across lift")

    def test_failed_evidence_stays_deferred(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        out = sched.refresh_external_watch("kk", key, None)
        self.assertTrue(out["checked"])
        self.assertFalse(out["changed"])
        run = store.load(run_id)
        self.assertEqual(runner.resume_gate(run), "watch")
        self.assertTrue(run["watch"]["check_error"])

    def test_missing_baseline_records_then_compares(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e", trigger="t")
        out = sched.refresh_external_watch("kk", key, {"registry": {"alpha": "2.0.0"}})
        self.assertFalse(out["changed"], "first observation records baseline, never wakes")
        run = store.load(run_id)
        self.assertEqual(runner.resume_gate(run), "watch")
        out2 = sched.refresh_external_watch("kk", key, {"registry": {"alpha": "2.0.0"}})
        self.assertFalse(out2["changed"])
        out3 = sched.refresh_external_watch("kk", key, {"registry": {"alpha": "9.9.9"}})
        self.assertTrue(out3["changed"])

    def test_unrelated_queued_work_can_execute(self):
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": {"alpha": "1.0.0", "beta": "1.0.0"}}))
        deps.set_latest_fn(lambda kind, ident: {"alpha": "2.0.0", "beta": "3.0.0"}[ident])
        sched.scan("kk")
        keys = {i["dep"]: i["key"] for i in sched.load("kk")["queue"]}
        sched.run_due("kk", runner.FakeExecutor(), key=keys["alpha"])
        sched.set_external_watch("kk", keys["alpha"], reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        out = sched.run_due("kk", runner.FakeExecutor())
        self.assertTrue(out["ran"], out)
        self.assertEqual(out["item"]["key"], keys["beta"])
        alpha = next(i for i in sched.load("kk")["queue"] if i["dep"] == "alpha")
        self.assertEqual(alpha["status"], "waiting-review")

    def test_restart_persistence(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        run = store.load(run_id)
        self.assertEqual(run["disposition"]["kind"], "EXTERNAL-WATCH")
        self.assertEqual(runner.resume_gate(run), "watch")

    def test_cancel_respected_on_watch_item(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e", trigger="t")
        out = sched.cancel("kk", key, reason="operator cancel")
        self.assertEqual(out["status"], "cancelled")
        run = store.load(run_id)
        self.assertEqual(runner.resume_gate(run), "watch",
                         "cancelled queue item keeps the run's watch record")

    def test_watch_run_is_not_a_verified_upgrade(self):
        from openmainty import branches, graph
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e", trigger="t")
        run = store.load(run_id)
        self.assertEqual(graph._run_status(run), "watch")
        self.assertNotEqual(graph._run_status(run), "done")
        promo = branches.promotion_readiness(run)
        self.assertFalse(promo.get("ready"))


def _mock_latest(test, mapping):
    orig = deps._latest_fn
    deps.set_latest_fn(lambda kind, ident: mapping[(kind, ident)])
    test.addCleanup(lambda: deps.set_latest_fn(orig))


class ObservePeerInputsTests(unittest.TestCase):
    """The watch observes every baseline-declared input, not just item.dep.

    Regression for the isolated probe (baseline alpha 2.0.0 + peer7
    7.0.0, fresh rows alpha 2.0.0 + peer8): the old observer saw only
    alpha, so a peer release never woke the watch. Registry keys may be
    plain names or explicit `manager:id`; a stable `sdk` class observes
    the official toolchain release. No run IDs or dependency names are
    hardcoded: everything comes from the baseline and the rows.
    """

    def setUp(self):
        make_home(self)

    def _item_run(self, baseline, dep="alpha"):
        item = {"kind": "dep-update", "dep": dep}
        run = {"watch": {"inputs": baseline}}
        return item, run

    def _rows(self, specs):
        return [{"name": name, "kind": kind, "latest": latest, "error": None}
                for name, kind, latest in specs]

    def test_baseline_peers_all_observed(self):
        _mock_latest(self, {})
        item, run = self._item_run({"registry": {"alpha": "2.0.0",
                                                 "peer7": "7.0.0"}})
        rows = self._rows([("alpha", "npm", "2.0.0"),
                           ("peer7", "npm", "7.0.0")])
        got = sched.observe_watch_inputs("kk", item, run, rows, [])
        self.assertEqual(got["registry"],
                         {"alpha": "2.0.0", "peer7": "7.0.0"})

    def test_peer_change_wakes(self):
        _mock_latest(self, {})
        item, run = self._item_run({"registry": {"alpha": "2.0.0",
                                                 "peer7": "7.0.0"}})
        rows = self._rows([("alpha", "npm", "2.0.0"),
                           ("peer7", "npm", "8.0.0")])
        got = sched.observe_watch_inputs("kk", item, run, rows, [])
        changed, detail = sched.compare_watch_inputs(
            run["watch"]["inputs"], got)
        self.assertTrue(changed)
        self.assertIn("peer7 latest: 7.0.0 -> 8.0.0", detail)

    def test_replaced_peer_without_lookup_stays_deferred_with_error(self):
        # Fresh rows dropped peer7 (peer8 instead) and no registry
        # lookup answers: the peer input is unobservable, so observation
        # fails instead of reporting a silently healthy partial view.
        def _boom(kind, ident):
            raise LookupError(f"not published: {kind}:{ident}")
        orig = deps._latest_fn
        deps.set_latest_fn(_boom)
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        item, run = self._item_run({"registry": {"alpha": "2.0.0",
                                                 "peer7": "7.0.0"}})
        rows = self._rows([("alpha", "npm", "2.0.0"),
                           ("peer8", "npm", "1.0.0")])
        self.assertIsNone(sched.observe_watch_inputs("kk", item, run, rows, []))

    def test_absent_row_falls_back_to_readonly_lookup(self):
        _mock_latest(self, {("npm", "peer7"): "7.0.0"})
        item, run = self._item_run({"registry": {"alpha": "2.0.0",
                                                 "peer7": "7.0.0"}})
        rows = self._rows([("alpha", "npm", "2.0.0")])
        got = sched.observe_watch_inputs("kk", item, run, rows, [])
        self.assertEqual(got["registry"],
                         {"alpha": "2.0.0", "peer7": "7.0.0"})

    def test_explicit_manager_id_keys(self):
        _mock_latest(self, {})
        item, run = self._item_run({"registry": {"npm:alpha": "2.0.0",
                                                 "npm:peer7": "7.0.0"}})
        rows = self._rows([("alpha", "npm", "2.0.0"),
                           ("peer7", "npm", "8.0.0")])
        got = sched.observe_watch_inputs("kk", item, run, rows, [])
        self.assertEqual(got["registry"],
                         {"npm:alpha": "2.0.0", "npm:peer7": "8.0.0"})
        changed, detail = sched.compare_watch_inputs(
            run["watch"]["inputs"], got)
        self.assertTrue(changed)
        self.assertIn("peer7", detail)

    def test_sdk_class_observes_stable_toolchain(self):
        _mock_latest(self, {("sdk", "flutter"): "3.44.0"})
        item = {"kind": "issue-triage", "issue": "431"}
        run = {"watch": {"inputs": {"sdk": {"flutter": "3.44.0"}}}}
        got = sched.observe_watch_inputs("kk", item, run, [], [])
        self.assertEqual(got.get("sdk"), {"flutter": "3.44.0"})
        changed, _ = sched.compare_watch_inputs(
            run["watch"]["inputs"], {"sdk": {"flutter": "3.45.0"}})
        self.assertTrue(changed)
        self.assertFalse(sched.compare_watch_inputs(
            run["watch"]["inputs"], {"sdk": {"flutter": "3.44.0"}})[0])

    def test_sdk_lookup_failure_stays_deferred(self):
        def _boom(kind, ident):
            raise LookupError("offline")
        orig = deps._latest_fn
        deps.set_latest_fn(_boom)
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        item = {"kind": "issue-triage", "issue": "431"}
        run = {"watch": {"inputs": {"sdk": {"flutter": "3.44.0"}}}}
        self.assertIsNone(sched.observe_watch_inputs("kk", item, run, [], []))

    def test_fetch_metadata_alone_never_wakes(self):
        baseline = {"registry": {"alpha": "2.0.0"}}
        # Extra observation keys and identical pins: nothing semantic moved.
        self.assertFalse(sched.compare_watch_inputs(
            baseline, {"registry": {"alpha": "2.0.0",
                                    "unrelated-new": "9.9.9"}})[0])
        self.assertFalse(sched.compare_watch_inputs(
            {"issue": {"number": "1", "title": "t", "state": "open",
                       "updated_at": "2026-01-01T00:00:00Z", "comments": 0}},
            {"issue": {"number": "1", "title": "t", "state": "open",
                       "updated_at": "2026-01-01T00:00:00Z", "comments": 0}})[0])


class CancelStaysCancelledTests(SchedWatchOpTests):
    """Cancellation survives watch refreshes: no reopen, no lift.

    Regression for the isolated probe (set watch, cancel the exact key,
    refresh with changed evidence -> queue became queued). Both
    refresh_external_watch and check_repo_watches refuse cancelled
    items and preserve the original cancellation, history, and run.
    """

    def test_changed_refresh_after_cancel_stays_cancelled(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        out = sched.cancel("kk", key, reason="operator cancel")
        self.assertEqual(out["status"], "cancelled")
        before = store.load(run_id)["watch"]
        res = sched.refresh_external_watch(
            "kk", key, {"registry": {"alpha": "9.9.9"}})
        self.assertFalse(res["checked"])
        self.assertIn("cancelled", res["reason"])
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "cancelled")
        self.assertEqual(item.get("attempts"), 2)
        run = store.load(run_id)
        self.assertEqual(run["watch"], before, "run record preserved")
        hist = [h for h in sched.load("kk")["history"] if h["key"] == key]
        self.assertIn("cancelled", {h["result"] for h in hist})
        self.assertNotIn("watch-lifted", {h["result"] for h in hist})

    def test_real_scan_after_cancel_keeps_cancelled(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        sched.cancel("kk", key, reason="operator cancel")
        rows = [{"name": "alpha", "kind": "npm", "latest": "9.9.9",
                 "error": None}]
        report = sched.check_repo_watches("kk", rows, [])
        self.assertEqual(report["checked"], 0)
        self.assertEqual(report["changed"], [])
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "cancelled")

    def test_cancel_racing_observation_stays_cancelled(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        st = sched.load("kk")
        item = next(i for i in st["queue"] if i["key"] == key)
        run = store.load(run_id)
        rows = [{"name": "alpha", "kind": "npm", "latest": "9.9.9",
                 "error": None}]
        observed = sched.observe_watch_inputs("kk", dict(item), run, rows, [])
        self.assertIsNotNone(observed, "observation raced ahead of cancel")
        sched.cancel("kk", key, reason="operator cancel")
        res = sched.refresh_external_watch("kk", key, observed)
        self.assertFalse(res["checked"])
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "cancelled")


class WatchLiftChainTests(SchedWatchOpTests):
    """A lifted watch makes the SAME run re-assess changed evidence.

    Previous receipts are archived (never overwritten), the
    triage/analyze/fix chain reopens so done steps rerun against the
    changed inputs, old proof is invalidated, and the changed evidence
    travels with the run. Attempts and history are preserved; nothing
    is committed and nothing is blessed.
    """

    def _watch_and_lift(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        out = sched.refresh_external_watch(
            "kk", key, {"registry": {"alpha": "3.0.0"}})
        self.assertTrue(out["changed"])
        return key, run_id

    def test_lift_reopens_assessment_chain(self):
        key, run_id = self._watch_and_lift()
        run = store.load(run_id)
        for step in ("triage", "analyze", "fix"):
            entry = run["steps"][step]
            self.assertEqual(entry["status"], "pending", step)
            self.assertIn("re-assessing", entry.get("note") or "", step)
        self.assertIn("alpha latest", run["steps"]["fix"]["note"])
        self.assertEqual(runner.resume_gate(run), None)
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["run_id"], run_id)
        self.assertEqual(item["attempts"], 2, "attempts preserved across lift")

    def test_lift_archives_receipts_and_records_context(self):
        from pathlib import Path as _Path
        key, run_id = self._watch_and_lift()
        rdir = _Path(store.run_dir(run_id))
        archived = sorted(p.name for p in rdir.iterdir()
                          if ".historical-" in p.name)
        self.assertTrue(archived, "previous receipts archived, never dropped")
        self.assertTrue((rdir / "watch.lift.txt").is_file())
        self.assertIn("alpha latest",
                      (rdir / "watch.lift.txt").read_text(encoding="utf-8"))
        run = store.load(run_id)
        self.assertEqual(run["artifacts"].get("watch_lift"), "watch.lift.txt")
        kinds = [i.get("kind") for i in run.get("interventions", [])]
        self.assertIn("external-watch-lifted", kinds)

    def test_lift_invalidates_old_proof_without_committing(self):
        key, run_id = self._watch_and_lift()
        run = store.load(run_id)
        self.assertEqual(run["steps"]["review"]["status"], "pending")
        self.assertIn("invalidated", run["steps"]["review"]["note"])
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertFalse(item.get("verified"), "lift blesses nothing")
        self.assertNotIn("commit", item.get("note") or "")

    def test_partial_input_stays_deferred_with_error(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t",
                                 inputs={"registry": {"alpha": "2.0.0",
                                                      "peer7": "7.0.0"}})
        out = sched.refresh_external_watch(
            "kk", key, {"registry": {"alpha": "9.9.9"}})
        self.assertTrue(out["checked"])
        self.assertFalse(out["changed"])
        run = store.load(run_id)
        self.assertEqual(runner.resume_gate(run), "watch")
        self.assertIn("partial", run["watch"]["check_error"])
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "waiting-review")

    def test_unknown_schema_stays_deferred_with_error(self):
        key, run_id = self._run_item_to_watch()
        sched.set_external_watch("kk", key, reason="r", evidence="e",
                                 trigger="t", inputs={"registry": {"alpha": "2.0.0"}})
        out = sched.refresh_external_watch("kk", key, ["not", "a", "record"])
        self.assertTrue(out["checked"])
        self.assertFalse(out["changed"])
        run = store.load(run_id)
        self.assertEqual(runner.resume_gate(run), "watch")
        self.assertIn("unknown watch schema", run["watch"]["check_error"])


class SetWatchValidationTests(SchedWatchOpTests):
    """set_external_watch validates everything before mutating anything."""

    def test_invalid_reopen_step_leaves_run_untouched(self):
        key, run_id = self._run_item_to_watch()
        before = store.load(run_id)
        self.assertIsNone(runner.resume_gate(before))
        out = sched.set_external_watch("kk", key, reason="r", evidence="e",
                                       trigger="t", reopen_steps=("bogus-step",))
        self.assertFalse(out["ok"])
        self.assertIn("unknown step", out["reason"])
        run = store.load(run_id)
        self.assertIsNone(runner.resume_gate(run),
                          "refused settle must not half-mutate the run")
        self.assertNotIn("watch", run)
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertNotEqual(item["status"], "waiting-review")

    def test_valid_reopen_step_proven_wrong_receipt(self):
        key, run_id = self._run_item_to_watch()
        out = sched.set_external_watch("kk", key, reason="r", evidence="e",
                                       trigger="t",
                                       inputs={"registry": {"alpha": "2.0.0"}},
                                       reopen_steps=("review",))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["reopened"], ["review"])
        run = store.load(run_id)
        self.assertEqual(run["steps"]["review"]["status"], "pending")
        self.assertIn("proven wrong", run["steps"]["review"]["note"])
        self.assertEqual(runner.resume_gate(run), "watch")


class FreshDepWatchObserverTests(unittest.TestCase):
    """A fresh automatic dep watch must initialize from registry evidence.

    Regression for the empty-baseline dep-watch defect: an empty-baseline
    dep-update watch whose issue slot is a synthetic `dep-...` marker
    (no numeric upstream issue) returned None before ever observing the
    healthy cached registry row, so it could never seed a baseline and
    falsely recorded "upstream evidence unavailable" forever. No run,
    repo, or dependency names are hardcoded: everything is generic.
    """

    def setUp(self):
        make_home(self)

    def _fresh_dep(self, dep="alpha", issue="dep-alpha-2.0.0"):
        item = {"kind": "dep-update", "dep": dep, "issue": issue}
        run = {"watch": {"inputs": {}}}
        rows = [{"name": dep, "kind": "npm", "latest": "2.0.0",
                 "error": None}]
        return item, run, rows

    def test_fresh_dep_watch_seeds_registry_without_issue_fetch(self):
        item, run, rows = self._fresh_dep()
        with mock.patch("openmainty.sched._fetch_issue_watch_inputs",
                        side_effect=AssertionError("no upstream issue exists")):
            orig = deps._latest_fn
            deps.set_latest_fn(lambda kind, ident: (_ for _ in ()).throw(
                LookupError("cached row must suffice; no lookup needed")))
            try:
                got = sched.observe_watch_inputs("kk", item, run, rows, [])
            finally:
                deps.set_latest_fn(orig)
        self.assertEqual(got, {"registry": {"alpha": "2.0.0"}})
        self.assertNotIn("issue", got)

    def test_fresh_dep_watch_registry_failure_stays_deferred(self):
        item, run, _ = self._fresh_dep()
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: (_ for _ in ()).throw(
            LookupError("offline")))
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        self.assertIsNone(sched.observe_watch_inputs("kk", item, run, [], []))
        rows = [{"name": "alpha", "kind": "npm", "latest": None,
                 "error": "boom"}]
        self.assertIsNone(sched.observe_watch_inputs("kk", item, run, rows, []))

    def test_explicit_issue_baseline_stays_fail_closed(self):
        item, run, rows = self._fresh_dep()
        run = {"watch": {"inputs": {"issue": {"number": "dep-alpha-2.0.0"}}}}
        self.assertIsNone(sched.observe_watch_inputs("kk", item, run, rows, []))

    def test_fresh_issue_triage_stays_fail_closed(self):
        item = {"kind": "issue-triage", "issue": "431"}
        run = {"watch": {"inputs": {}}}
        rows = [{"name": "alpha", "kind": "npm", "latest": "2.0.0",
                 "error": None}]
        with mock.patch("openmainty.sched._fetch_issue_watch_inputs",
                        return_value=None):
            self.assertIsNone(
                sched.observe_watch_inputs("kk", item, run, rows, []))

    def test_unknown_legacy_task_preserves_fail_closed(self):
        run = {"watch": {"inputs": {}}}
        rows = [{"name": "alpha", "kind": "npm", "latest": "2.0.0",
                 "error": None}]
        self.assertIsNone(sched.observe_watch_inputs(
            "kk", {"kind": "mystery"}, run, rows, []))
        self.assertIsNone(sched.observe_watch_inputs("kk", {}, run, rows, []))


class FreshDepWatchRefreshTests(unittest.TestCase):
    """The first real scan seeds the baseline through refresh_external_watch.

    Covers the full real path (not only the observer helper): settle an
    owned run with empty inputs, observe, refresh to seed the baseline
    and clear the error with same run id / attempts / waiting-review
    status and verified false, a second unchanged check with zero model
    calls, and the preserved same-run lift contract on a real registry
    change. Isolated fixtures, no real model calls.
    """

    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": {"alpha": "1.0.0"}}))
        repos.save({"name": "kk", "github": "e/kk", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["kk"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: {"alpha": "2.0.0"}[ident])
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        patcher = mock.patch("openmainty.cli._open_issues", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        sched.configure("kk", enabled=True, interval_s=60)
        sched.scan("kk")

    def _watch_with_empty_baseline(self, attempts=0):
        key = sched.load("kk")["queue"][0]["key"]
        out = sched.run_due("kk", runner.FakeExecutor(), key=key)
        self.assertTrue(out["ran"] or out.get("item"), out)
        run_id = sched.load("kk")["queue"][0]["run_id"]
        self.assertTrue(run_id)
        with sched.locked("kk"):
            st = sched.load("kk")
            item = next(i for i in st["queue"] if i["key"] == key)
            item["attempts"] = attempts
            item["status"] = "queued"
            item["next_retry_at"] = None
            sched.save(st)
        op = sched.set_external_watch(
            "kk", key, reason="wait for compatible release",
            evidence="fix confirmed incompatibility; manifests unchanged",
            trigger="compatible peer release")
        self.assertTrue(op["ok"], op)
        return key, run_id

    def _rows(self, latest):
        return [{"name": "alpha", "kind": "npm", "latest": latest,
                 "error": None}]

    def test_first_real_check_seeds_baseline_and_clears_error(self):
        key, run_id = self._watch_with_empty_baseline()
        st = sched.load("kk")
        item = next(i for i in st["queue"] if i["key"] == key)
        run = store.load(run_id)
        self.assertEqual(run["watch"]["inputs"], {})
        with mock.patch("openmainty.sched._fetch_issue_watch_inputs",
                        side_effect=AssertionError("no upstream issue exists")):
            observed = sched.observe_watch_inputs(
                "kk", dict(item), run, self._rows("2.0.0"), [])
        self.assertEqual(observed, {"registry": {"alpha": "2.0.0"}})
        res = sched.refresh_external_watch("kk", key, observed)
        self.assertTrue(res["checked"])
        self.assertFalse(res["changed"])
        self.assertIn("baseline recorded", res["reason"])
        run = store.load(run_id)
        self.assertEqual(run["id"], run_id)
        self.assertEqual(run["watch"]["inputs"],
                         {"registry": {"alpha": "2.0.0"}})
        self.assertIsNone(run["watch"]["check_error"])
        self.assertTrue(run["watch"]["last_check"])
        self.assertEqual(runner.resume_gate(run), "watch")
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["run_id"], run_id, "same run, never forked")
        self.assertEqual(item["attempts"], 0, "attempts preserved, never reset")
        self.assertEqual(item["status"], "waiting-review")
        self.assertFalse(item.get("verified"), "seed blesses nothing")

    def test_second_unchanged_check_spends_zero_model_calls(self):
        key, run_id = self._watch_with_empty_baseline()
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        observed = sched.observe_watch_inputs(
            "kk", dict(item), store.load(run_id), self._rows("2.0.0"), [])
        sched.refresh_external_watch("kk", key, observed)
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        observed2 = sched.observe_watch_inputs(
            "kk", dict(item), store.load(run_id), self._rows("2.0.0"), [])
        res2 = sched.refresh_external_watch("kk", key, observed2)
        self.assertTrue(res2["checked"])
        self.assertFalse(res2["changed"])
        self.assertIn("unchanged", res2["reason"])
        ex = runner.FakeExecutor()
        out = sched.run_due("kk", ex)
        self.assertFalse(out["ran"])
        self.assertEqual(ex.calls, [], "unchanged watch spends zero model calls")
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "waiting-review")
        self.assertEqual(item["run_id"], run_id)
        self.assertEqual(item["attempts"], 0)

    def test_real_registry_change_keeps_same_run_lift_contract(self):
        key, run_id = self._watch_with_empty_baseline()
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        observed = sched.observe_watch_inputs(
            "kk", dict(item), store.load(run_id), self._rows("2.0.0"), [])
        sched.refresh_external_watch("kk", key, observed)
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        observed2 = sched.observe_watch_inputs(
            "kk", dict(item), store.load(run_id), self._rows("3.0.0"), [])
        res = sched.refresh_external_watch("kk", key, observed2)
        self.assertTrue(res["changed"])
        run = store.load(run_id)
        self.assertIsNone(runner.resume_gate(run), "lifted run is resumable")
        item = next(i for i in sched.load("kk")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["run_id"], run_id, "same run eligible, not a new run")
        self.assertEqual(item["attempts"], 0, "attempts preserved across lift")
        self.assertFalse(item.get("verified"), "lift blesses nothing")


if __name__ == "__main__":
    unittest.main()
