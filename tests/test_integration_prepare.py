"""App-owned integration preparation: lifecycle, idempotency, contention.

Covers batches.prepare_integration (registry ids in, derived paths/commits
out) plus the serve-layer lease guard, with real git fixture repos under an
isolated OPENMAINTY_HOME. No network, no devices, no builds.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, paths, runner, sched, serve, store  # noqa: E402
from openmainty.util import write_json  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout


def snap_of(worktree):
    snap = runner.source_snapshot(Path(worktree))
    return {"head": snap["head"], "fingerprint": snap["fingerprint"]}


class PrepareMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        # Origin repo: main @ B, candidate branch @ C (tracked change).
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git("init", "-b", "main", cwd=self.origin)
        git("config", "user.email", "test@localhost", cwd=self.origin)
        git("config", "user.name", "test", cwd=self.origin)
        (self.origin / "a.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "base", cwd=self.origin)
        self.base_head = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-qb", "cand", cwd=self.origin)
        (self.origin / "a.txt").write_text("cand\n", encoding="utf-8")
        (self.origin / "b.txt").write_text("new\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "candidate", cwd=self.origin)
        self.cand_head = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        # Base integration worktree @ B (the approved source, detached so
        # the origin checkout can stay on main).
        self.base_wt = self.root / "home" / "base-wt"
        git("worktree", "add", "--detach", str(self.base_wt),
            self.base_head, cwd=self.origin)
        self.base_snap = snap_of(self.base_wt)
        self.assertEqual(self.base_snap["head"], self.base_head)
        # Candidate run worktree @ C with a recorded run.json carrying
        # fresh review+e2e proof bound to the live bytes.
        self.run_id = "run-testprep1"
        self.run_work = paths.runs_dir() / self.run_id / "work"
        git("worktree", "add", "--detach", str(self.run_work),
            self.cand_head, cwd=self.origin)
        self.cand_snap = snap_of(self.run_work)
        self.assertEqual(self.cand_snap["head"], self.cand_head)
        state = store.create("fakerepo", "dep-foo-1.2.3",
                             "chore(deps): foo 1.2.2 -> 1.2.3",
                             self.base_head, {})
        state["id"] = self.run_id
        state["dep"] = "foo"
        state["target"] = "1.2.3"
        state["lane"] = "main"
        state["branch"] = "codex/maint/foo-1-2-3"
        state["workCommit"] = self.cand_head
        state["workTreeFingerprint"] = self.cand_snap["fingerprint"]
        state["verifiedCommits"] = {"review": self.cand_head,
                                    "e2e": self.cand_head}
        state["verifiedFingerprints"] = {
            "review": self.cand_snap["fingerprint"],
            "e2e": self.cand_snap["fingerprint"]}
        state["steps"] = {"review": {"status": "done", "exit_code": 0},
                          "e2e": {"status": "done", "exit_code": 0}}
        store.save(state)
        # Baseline run: a DISTINCT previously-accepted candidate (bar
        # 9.9.9 @ base) that owns the base batch's primary. The
        # candidate under test (foo 1.2.3 @ cand) is deliberately NOT
        # in the base batch, so legitimate prepares proceed and only
        # genuine duplicates trip the already-present guard.
        self.base_run_id = "run-testprep0"
        self.base_run_work = (paths.runs_dir() / self.base_run_id / "work")
        git("worktree", "add", "--detach", str(self.base_run_work),
            self.base_head, cwd=self.origin)
        self.base_run_snap = snap_of(self.base_run_work)
        self.assertEqual(self.base_run_snap["head"], self.base_head)
        bstate = store.create("fakerepo", "dep-bar-9.9.9",
                              "chore(deps): bar 9.9.8 -> 9.9.9",
                              self.base_head, {})
        bstate["id"] = self.base_run_id
        bstate["dep"] = "bar"
        bstate["target"] = "9.9.9"
        bstate["lane"] = "main"
        bstate["branch"] = "codex/maint/bar-9-9-9"
        bstate["workCommit"] = self.base_head
        bstate["workTreeFingerprint"] = self.base_run_snap["fingerprint"]
        bstate["verifiedCommits"] = {"review": self.base_head,
                                     "e2e": self.base_head}
        bstate["verifiedFingerprints"] = {
            "review": self.base_run_snap["fingerprint"],
            "e2e": self.base_run_snap["fingerprint"]}
        bstate["steps"] = {"review": {"status": "done", "exit_code": 0},
                           "e2e": {"status": "done", "exit_code": 0}}
        store.save(bstate)
        # Registered base batch: one READY primary (the baseline run,
        # accepted review, one passing gate with hash-reverified file
        # evidence bound to the live integration source).
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        ev_dir = batches.evidence_dir("test-batch")
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "proof.txt").write_text("proof\n", encoding="utf-8")
        ev_sha = batches.sha256_file(ev_dir / "proof.txt")
        write_json(batches.batches_dir() / "test-batch.json", {
            "schema": 3, "id": "test-batch", "repo": "fakerepo",
            "target_branch": "main", "primaries": [{
                "dep": "bar", "target": "9.9.9", "lane": "main",
                "run_id": self.base_run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(self.base_run_work),
                           "head": self.base_head,
                           "fingerprint":
                           self.base_run_snap["fingerprint"]}}],
            "inherited": [],
            "origin": {"checkout": str(self.base_wt),
                       "base_branch": "main", "base_head": self.base_head},
            "integrations": [{"lane": "main", "worktree": str(self.base_wt),
                              "head": self.base_head,
                              "fingerprint": self.base_snap["fingerprint"],
                              "status": "integration-testing",
                              "gates": [{
                                  "key": "unit", "status": "pass",
                                  "inputs": {
                                      "head": self.base_head,
                                      "fingerprint":
                                      self.base_snap["fingerprint"]},
                                  "evidence": [{
                                      "label": "unit proof",
                                      "kind": "file", "stored": True,
                                      "stored_path": "proof.txt",
                                      "sha256": ev_sha, "bytes": 6}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})
        self.batch_id = "test-batch"

    def op_path(self):
        slug = f"prep-{self.batch_id}-foo-1-2-3"
        return paths.home() / "reviews" / slug

    def save_run(self, **fields):
        state = store.load(self.run_id)
        state.update(fields)
        store.save(state)


class LifecycleTests(PrepareMixin):
    def test_prepare_creates_isolated_worktree_with_provenance(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["repo"], "fakerepo")
        self.assertEqual(rec["candidate"]["head"], self.cand_head)
        self.assertEqual(rec["base"]["head"], self.base_head)
        res = rec["result"]
        self.assertEqual(res["branch"], "codex/review-foo-1-2-3")
        self.assertTrue(Path(res["path"]).is_dir())
        self.assertNotEqual(res["head"], self.base_head)
        self.assertTrue(res["fingerprint"])
        # All runtime gates start pending except the preparation itself.
        gates = {g["key"]: g["status"] for g in rec["gates"]}
        self.assertEqual(gates["source-prepared"], "pass")
        self.assertEqual(gates["fastchecks"], "pending")
        self.assertEqual(gates["native"], "pending")
        # Record persisted before/after with UTC stamps.
        stored = json.loads(batches.op_file(rec["op_id"]).read_text(
            encoding="utf-8"))
        self.assertEqual(stored["result"]["head"], res["head"])
        self.assertTrue(stored["created_at"] and stored["updated_at"])
        # Integrated tree really contains the candidate change.
        self.assertEqual((Path(res["path"]) / "a.txt").read_text(
            encoding="utf-8"), "cand\n")
        self.assertEqual((Path(res["path"]) / "b.txt").read_text(
            encoding="utf-8"), "new\n")

    def test_retry_returns_existing_without_duplicate(self):
        first = batches.prepare_integration(self.run_id, self.batch_id)
        branches_before = git("branch", "--list", cwd=self.origin)
        second = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertTrue(second.get("existing"))
        self.assertEqual(second["result"]["head"], first["result"]["head"])
        self.assertEqual(git("branch", "--list", cwd=self.origin),
                         branches_before)

    def test_drift_after_done_projects_blocked(self):
        first = batches.prepare_integration(self.run_id, self.batch_id)
        (Path(first["result"]["path"]) / "a.txt").write_text(
            "drifted\n", encoding="utf-8")
        second = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertTrue(second.get("existing"))
        self.assertTrue(second.get("live_mismatch"))
        self.assertEqual(second["status"], "blocked")
        stored = json.loads(batches.op_file(first["op_id"]).read_text(
            encoding="utf-8"))
        self.assertEqual(stored["status"], "blocked")

    def test_candidate_drift_is_blocked_not_integrated(self):
        (self.run_work / "a.txt").write_text("drifted\n", encoding="utf-8")
        with self.assertRaises(batches.IntegrationError):
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertFalse(self.op_path().exists())

    def test_missing_proof_is_blocked(self):
        state = store.load(self.run_id)
        state.pop("verifiedFingerprints", None)
        store.save(state)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertIn("not fresh", str(ctx.exception))
        self.assertFalse(self.op_path().exists())

    def test_stale_proof_is_blocked(self):
        self.save_run(verifiedFingerprints={"review": "0" * 64,
                                             "e2e": "0" * 64})
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertIn("not fresh", str(ctx.exception))

    def test_base_not_ready_is_blocked(self):
        batch_path = batches.batches_dir() / "test-batch.json"
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        batch["integrations"][0]["gates"][0]["status"] = "fail"
        write_json(batch_path, batch)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertIn("not READY", str(ctx.exception))
        self.assertFalse(self.op_path().exists())

    def test_two_commit_candidate_replays_in_recorded_order(self):
        git("checkout", "-q", "cand", cwd=self.origin)
        (self.origin / "a.txt").write_text("cand2\n", encoding="utf-8")
        git("commit", "-qam", "candidate2", cwd=self.origin)
        cand2 = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        git("worktree", "remove", "--force", str(self.run_work),
            cwd=self.origin)
        git("worktree", "add", "--detach", str(self.run_work), cand2,
            cwd=self.origin)
        snap2 = snap_of(self.run_work)
        self.save_run(workCommit=cand2,
                      workTreeFingerprint=snap2["fingerprint"],
                      verifiedCommits={"review": cand2, "e2e": cand2},
                      verifiedFingerprints={"review": snap2["fingerprint"],
                                            "e2e": snap2["fingerprint"]})
        # The base batch primary is the distinct baseline run (bar
        # 9.9.9 @ base), so it stays untouched: the base remains READY
        # and the two-commit range replays in recorded order.
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done")
        self.assertEqual([c["head"] for c in
                          rec["candidate"]["commit_list"]],
                         [self.cand_head, cand2])
        self.assertEqual(rec["result"]["replayed_from"],
                         [self.cand_head, cand2])
        self.assertEqual(rec["result"]["branch"],
                         "codex/review-foo-1-2-3")
        # Integrated tree really contains both commits' changes, in order.
        self.assertEqual((Path(rec["result"]["path"]) / "a.txt").read_text(
            encoding="utf-8"), "cand2\n")
        self.assertEqual((Path(rec["result"]["path"]) / "b.txt").read_text(
            encoding="utf-8"), "new\n")

    def test_unknown_ids_are_404(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration("run-nope", self.batch_id)
        self.assertEqual(ctx.exception.http_status, 404)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, "batch-nope")
        self.assertEqual(ctx.exception.http_status, 404)

    def test_unrelated_branch_is_never_overwritten(self):
        git("branch", "codex/review-foo-1-2-3", "main", cwd=self.origin)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertIn("already exists", str(ctx.exception))
        self.assertFalse(self.op_path().exists())

    def test_conflict_records_blocked_and_reports_cleanup(self):
        # Move base to B2 with a conflicting edit; re-record the batch
        # (base + evidence) so readiness still holds and the pick itself
        # is what fails.
        git("checkout", "-q", "main", cwd=self.origin)
        (self.origin / "a.txt").write_text("base2\n", encoding="utf-8")
        git("commit", "-qam", "base2", cwd=self.origin)
        base2 = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        git("worktree", "remove", "--force", str(self.base_wt),
            cwd=self.origin)
        git("worktree", "add", "--detach", str(self.base_wt), base2,
            cwd=self.origin)
        snap2 = snap_of(self.base_wt)
        batch_path = batches.batches_dir() / "test-batch.json"
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        batch["origin"]["base_head"] = base2
        batch["integrations"][0]["head"] = base2
        batch["integrations"][0]["fingerprint"] = snap2["fingerprint"]
        batch["integrations"][0]["gates"][0]["inputs"] = {
            "head": base2, "fingerprint": snap2["fingerprint"]}
        write_json(batch_path, batch)
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertTrue(rec["error"])
        # Cleanup outcome is verified, never merely claimed.
        self.assertFalse(self.op_path().exists())

    def test_git_crash_after_start_settles_blocked(self):
        real_run = subprocess.run
        calls = []

        def flaky(*args, **kwargs):
            cmd = list(args[0]) if args and args[0] else []
            calls.append(cmd)
            if "worktree" in cmd:
                raise RuntimeError("boom")
            return real_run(*args, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                batches.prepare_integration(self.run_id, self.batch_id)
        rec = batches.latest_integration_op(self.run_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("interrupted", rec["error"])
        # Retry returns the settled record without duplicating.
        again = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertTrue(again.get("existing"))
        self.assertEqual(again["status"], "blocked")

    def test_started_record_reconciled_without_mutation(self):
        op_id = f"prep-{self.run_id}-{self.batch_id}"
        write_json(batches.op_file(op_id), {
            "schema": 1, "op": "prepare", "op_id": op_id,
            "repo": "fakerepo", "run_id": self.run_id,
            "batch_id": self.batch_id, "dep": "foo", "target": "1.2.3",
            "lane": "main",
            "candidate": {"head": self.cand_head,
                          "fingerprint": self.cand_snap["fingerprint"]},
            "base": {"batch": self.batch_id, "head": self.base_head,
                     "fingerprint": self.base_snap["fingerprint"]},
            "result": {"path": str(self.op_path()),
                       "branch": "codex/review-foo-1-2-3"},
            "gates": [{"key": "source-prepared", "status": "pending",
                       "note": ""}],
            "status": "started", "error": "", "created_at": "t",
            "updated_at": "t"})
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertTrue(rec.get("existing"))
        self.assertTrue(rec.get("reconciled"))
        self.assertEqual(rec["status"], "blocked")
        self.assertFalse(self.op_path().exists())


class HookSuppressionTests(PrepareMixin):
    # M2.7-SEC F3: app git mutations never run owner hooks. The
    # fixture hooksPath points at a hook that would touch a sentinel
    # (and fail loudly); prepare must complete with no sentinel.
    def test_prepare_runs_no_hooks(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        sentinel = self.root / "hook-ran.txt"
        hook = hooks / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "echo ran > %s\n" % sentinel +
            "exit 1\n", encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
        git("config", "core.hooksPath", str(hooks), cwd=self.origin)
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done")
        self.assertFalse(sentinel.exists())


class LaneSafetyTests(PrepareMixin):
    """A main-lane candidate must never anchor a next-lane base.

    Regression for the prep-batch UI defaulting a main candidate
    (e.g. Bob 0.43.2) to the newest next batch: the backend refuses
    before any git mutation, so UI filtering alone is never the gate.
    """

    def setUp(self):
        super().setUp()
        # A coherent next-lane origin needs a resolvable base branch;
        # an extra branch at the base commit disturbs no other test.
        git("branch", "-f", "next", self.base_head, cwd=self.origin)

    def set_batch_lane(self, lane):
        # Coherent lane move: evaluate() binds primaries to
        # same-lane integrations (and the origin base branch), so a
        # lane switch must move all three — otherwise the base would
        # fail readiness instead of exercising the anchoring guard.
        batch_path = batches.batches_dir() / "test-batch.json"
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        batch["target_branch"] = lane
        origin = batch.get("origin") or {}
        origin["base_branch"] = lane
        batch["origin"] = origin
        for p in batch.get("primaries", []):
            p["lane"] = lane
        for integ in batch.get("integrations", []):
            integ["lane"] = lane
        write_json(batch_path, batch)

    def branches(self):
        return git("branch", "--list", cwd=self.origin)

    def test_main_candidate_cannot_anchor_next_base(self):
        self.set_batch_lane("next")
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertIn("main-lane candidate cannot anchor",
                      str(ctx.exception))
        # Refused before mutation: no worktree dir, no new branch.
        self.assertFalse(self.op_path().exists())
        self.assertNotIn("codex/review-foo-1-2-3", self.branches())

    def test_main_candidate_may_anchor_main_base(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["lane"], "main")
        self.assertEqual(rec["base"]["lane"], "main")

    def test_next_candidate_may_anchor_main_base(self):
        self.save_run(lane="next")
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["lane"], "next")
        self.assertEqual(rec["base"]["lane"], "main")

    def test_next_candidate_may_anchor_next_base(self):
        self.save_run(lane="next")
        self.set_batch_lane("next")
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["lane"], "next")
        self.assertEqual(rec["base"]["lane"], "next")

    def test_unknown_candidate_lane_fails_closed(self):
        for bad in ("", "stable", None):
            self.save_run(lane=bad)
            with self.assertRaises(batches.IntegrationError) as ctx:
                batches.prepare_integration(self.run_id, self.batch_id)
            self.assertIn("no usable lane", str(ctx.exception))
            self.assertFalse(self.op_path().exists())
        self.assertNotIn("codex/review-foo-1-2-3", self.branches())

    def test_malformed_base_lane_fails_closed(self):
        self.set_batch_lane("")
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertIn("cannot anchor", str(ctx.exception))
        self.assertFalse(self.op_path().exists())
        self.assertNotIn("codex/review-foo-1-2-3", self.branches())


class DuplicateGuardTests(PrepareMixin):
    """A candidate already present in the requested base is refused.

    Regression for the registered-ready defect (current QA 10/10 yet
    prepare offered with its own batch as default): the backend
    refuses BEFORE any git/worktree/op/job/model mutation, naming the
    direct or inherited provenance. Recorded-operation idempotency
    still reconciles instead of refusing.
    """

    def candidate_primary(self):
        return {"dep": "foo", "target": "1.2.3", "lane": "main",
                "run_id": self.run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(self.run_work),
                           "head": self.cand_head,
                           "fingerprint": self.cand_snap["fingerprint"]}}

    def baseline_primary(self):
        return {"dep": "bar", "target": "9.9.9", "lane": "main",
                "run_id": self.base_run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(self.base_run_work),
                           "head": self.base_head,
                           "fingerprint":
                           self.base_run_snap["fingerprint"]}}

    def write_batch_copy(self, batch_id, primaries, inherited=None):
        batch_path = batches.batches_dir() / "test-batch.json"
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        batch["id"] = batch_id
        batch["primaries"] = primaries
        batch["inherited"] = inherited or []
        write_json(batches.batches_dir() / (batch_id + ".json"), batch)
        # The copy is a coherent READY base on its own (stored gate
        # evidence is content-addressed per batch id), so a refusal
        # proves the duplicate guard itself — not unreadiness.
        src_ev = batches.evidence_dir("test-batch")
        dest_ev = batches.evidence_dir(batch_id)
        dest_ev.mkdir(parents=True, exist_ok=True)
        for entry in src_ev.iterdir():
            if entry.is_file():
                (dest_ev / entry.name).write_bytes(entry.read_bytes())
        return batch_id

    def branches(self):
        return git("branch", "--list", cwd=self.origin)

    def reviews_children(self):
        d = paths.home() / "reviews"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def test_direct_duplicate_refused_before_mutation(self):
        dup = self.write_batch_copy("test-batch-dup",
                                    [self.candidate_primary()])
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, dup)
        self.assertIn("already present", str(ctx.exception))
        self.assertIn(dup, str(ctx.exception))
        self.assertIn("direct primary", str(ctx.exception))
        # No mutation: no op record, no worktree dir, no new branch.
        self.assertFalse(
            batches.op_file(f"prep-{self.run_id}-{dup}").exists())
        self.assertEqual(self.reviews_children(), [])
        self.assertNotIn("codex/review-foo-1-2-3", self.branches())

    def test_inherited_duplicate_refused_before_mutation(self):
        sub = self.write_batch_copy("test-batch-sub",
                                    [self.candidate_primary()])
        inh = self.write_batch_copy("test-batch-inh",
                                    [self.baseline_primary()],
                                    inherited=[{"group": "g", "batch": sub,
                                              "count": 1}])
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, inh)
        self.assertIn("already present", str(ctx.exception))
        self.assertIn(sub, str(ctx.exception))
        self.assertFalse(
            batches.op_file(f"prep-{self.run_id}-{inh}").exists())
        self.assertEqual(self.reviews_children(), [])
        self.assertNotIn("codex/review-foo-1-2-3", self.branches())

    def test_recorded_retry_still_reconciles_for_duplicate_pair(self):
        dup = self.write_batch_copy("test-batch-dup",
                                    [self.candidate_primary()])
        op_id = f"prep-{self.run_id}-{dup}"
        write_json(batches.op_file(op_id), {
            "schema": 1, "op": "prepare", "op_id": op_id,
            "repo": "fakerepo", "run_id": self.run_id,
            "batch_id": dup, "dep": "foo", "target": "1.2.3",
            "lane": "main",
            "candidate": {"head": self.cand_head,
                          "fingerprint": self.cand_snap["fingerprint"]},
            "base": {"batch": dup, "head": self.base_head,
                     "fingerprint": self.base_snap["fingerprint"]},
            "result": {"path": str(paths.home() / "reviews" / "nope"),
                       "branch": "codex/review-foo-1-2-3"},
            "gates": [{"key": "source-prepared", "status": "blocked",
                       "note": "earlier attempt"}],
            "status": "blocked", "error": "earlier attempt",
            "created_at": "t", "updated_at": "t"})
        rec = batches.prepare_integration(self.run_id, dup)
        self.assertTrue(rec.get("existing"))
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual(self.reviews_children(), [])

    def test_api_duplicate_is_409_before_mutation(self):
        dup = self.write_batch_copy("test-batch-dup",
                                    [self.candidate_primary()])
        with mock.patch.object(serve, "_require_gate", lambda: None):
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_integration_prepare(
                    {"run": self.run_id, "batch": dup})
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("already present", str(ctx.exception.message))
        self.assertFalse(
            batches.op_file(f"prep-{self.run_id}-{dup}").exists())
        self.assertEqual(self.reviews_children(), [])


class LeafRefusalTests(PrepareMixin):
    """A genuinely unready inherited batch names its leaf in refusal."""

    def _sub_batch(self, bid="test-sub"):
        sub_integ = self.root / "home" / "sub-integ"
        git("worktree", "add", "--detach", str(sub_integ),
            self.base_head, cwd=self.origin)
        sub_snap = snap_of(sub_integ)
        sub_run_id = "run-testprep-sub"
        sub_run_work = paths.runs_dir() / sub_run_id / "work"
        git("worktree", "add", "--detach", str(sub_run_work),
            self.cand_head, cwd=self.origin)
        sub_cand = snap_of(sub_run_work)
        st = store.create("fakerepo", "dep-sub-1.0",
                          "chore(deps): sub 0.9 -> 1.0", self.base_head, {})
        st["id"] = sub_run_id
        st["dep"] = "sub"
        st["target"] = "1.0"
        st["lane"] = "main"
        st["branch"] = "codex/maint/sub-1-0"
        st["workCommit"] = self.cand_head
        st["workTreeFingerprint"] = sub_cand["fingerprint"]
        st["verifiedCommits"] = {"review": self.cand_head,
                                 "e2e": self.cand_head}
        st["verifiedFingerprints"] = {"review": sub_cand["fingerprint"],
                                      "e2e": sub_cand["fingerprint"]}
        st["steps"] = {"review": {"status": "done", "exit_code": 0},
                       "e2e": {"status": "done", "exit_code": 0}}
        store.save(st)
        ev_dir = batches.evidence_dir(bid)
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "proof.txt").write_text("proof\n", encoding="utf-8")
        ev_sha = batches.sha256_file(ev_dir / "proof.txt")
        write_json(batches.batches_dir() / (bid + ".json"), {
            "schema": 3, "id": bid, "repo": "fakerepo",
            "target_branch": "main", "primaries": [{
                "dep": "sub", "target": "1.0", "lane": "main",
                "run_id": sub_run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(sub_run_work),
                           "head": self.cand_head,
                           "fingerprint": sub_cand["fingerprint"]}}],
            "inherited": [],
            "origin": {"checkout": str(self.origin),
                       "base_branch": "main", "base_head": self.base_head},
            "integrations": [{"lane": "main", "worktree": str(sub_integ),
                              "head": self.base_head,
                              "fingerprint": sub_snap["fingerprint"],
                              "status": "integration-testing",
                              "gates": [{
                                  "key": "unit", "status": "pass",
                                  "inputs": {
                                      "head": self.base_head,
                                      "fingerprint":
                                      sub_snap["fingerprint"]},
                                  "evidence": [{
                                      "label": "unit proof",
                                      "kind": "file", "stored": True,
                                      "stored_path": "proof.txt",
                                      "sha256": ev_sha, "bytes": 6}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})
        batch_path = batches.batches_dir() / "test-batch.json"
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        batch["inherited"] = [{"group": "sub 1건 상속", "count": 1,
                               "batch": bid}]
        write_json(batch_path, batch)
        return sub_integ

    def test_base_not_ready_refusal_carries_inherited_leaf(self):
        sub_integ = self._sub_batch()
        (sub_integ / "a.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id)
        msg = str(ctx.exception)
        self.assertIn("not READY", msg)
        self.assertIn("test-sub", msg)
        self.assertIn("leaf:", msg)
        self.assertFalse(self.op_path().exists())


class StaleSnapshotRefusalTests(PrepareMixin):
    """S7 condition 3(b): a stale READY snapshot never makes prepare
    unsafe — the base source is re-verified live server-side and a
    drifted base refuses with the live reason."""

    def _card(self, bid):
        snap = json.loads(
            batches.batches_snapshot_path().read_text(
                encoding="utf-8"))
        return next(b for b in snap["batches"] if b["id"] == bid)

    def test_prepare_refuses_after_base_drift(self):
        batches.refresh_all_snapshots("test")
        self.assertEqual(self._card("test-batch")["ready"], 1)
        (self.base_wt / "drift.txt").write_text("drift\n",
                                                encoding="utf-8")
        git("add", "drift.txt", cwd=self.base_wt)
        git("commit", "-qm", "drift", cwd=self.base_wt)
        # The stale premise: no refresh ran, the file still says READY.
        self.assertEqual(self._card("test-batch")["ready"], 1)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertIn("drifted or unreadable", str(ctx.exception))
        self.assertFalse(self.op_path().exists())


class BusyLeaseTests(PrepareMixin):
    def test_api_reports_busy_with_owner_while_guard_held(self):
        held = threading.Event()
        released = threading.Event()

        def holder():
            token = sched.acquire_worker_lease("external-guard")
            held.set()
            released.wait(timeout=60)
            if token is not None:
                sched.release_worker_lease(token)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(timeout=30))
        try:
            with mock.patch.object(serve, "_require_gate", lambda: None):
                with self.assertRaises(serve.ApiError) as ctx:
                    serve.api_integration_prepare(
                        {"run": self.run_id, "batch": self.batch_id})
            self.assertEqual(ctx.exception.status, 409)
            self.assertIn("external-guard", str(ctx.exception.message))
        finally:
            released.set()
            thread.join(timeout=30)
        self.assertFalse(self.op_path().exists())


class StaleCandidateTests(unittest.TestCase):
    # FIX-a4: a "conflict" that is really a stale candidate. The
    # lane base moved bob 0.43.0 -> 0.43.2 in both manifests after
    # the candidate was cut; the candidate bumps 0.43.0 -> 0.44.1.
    # The only conflicting manifest hunks are bob's own line, so
    # the record must carry the stale label (with the base
    # version), never a bare cherry-pick detail.

    def _manifest(self, bob, alice="0.1.0"):
        import json as _json
        return _json.dumps(
            {"dependencies": {"alice": alice, "bob": bob}},
            indent=2) + "\n"

    def _build(self, cand_bob="0.44.1", cand_alice="0.1.0",
               base_bob="0.43.2", base_alice="0.1.0",
               cand_lock="lock-v1\n", base_lock="lock-v2\n",
               base_lock_only=False):
        import json as _json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git("init", "-b", "main", cwd=self.origin)
        git("config", "user.email", "test@localhost", cwd=self.origin)
        git("config", "user.name", "test", cwd=self.origin)
        (self.origin / "package.json").write_text(
            self._manifest("0.43.0"), encoding="utf-8")
        (self.origin / "example").mkdir()
        (self.origin / "example" / "package.json").write_text(
            self._manifest("0.43.0"), encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            "lock-v0\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "parent", cwd=self.origin)
        parent = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-qb", "cand", cwd=self.origin)
        if not base_lock_only:
            (self.origin / "package.json").write_text(
                self._manifest(cand_bob, cand_alice), encoding="utf-8")
            (self.origin / "example" / "package.json").write_text(
                self._manifest(cand_bob, cand_alice), encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            cand_lock, encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "candidate", cwd=self.origin)
        cand = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        (self.origin / "package.json").write_text(
            self._manifest(base_bob, base_alice), encoding="utf-8")
        (self.origin / "example" / "package.json").write_text(
            self._manifest(base_bob, base_alice), encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            base_lock, encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "base moves on", cwd=self.origin)
        base = git("rev-parse", "HEAD", cwd=self.origin).strip()
        # Base integration worktree @ base; candidate run work @
        # cand with fresh review+e2e proof bound to the live bytes.
        self.base_wt = self.root / "home" / "base-wt"
        git("worktree", "add", "--detach", str(self.base_wt), base,
            cwd=self.origin)
        self.base_snap = snap_of(self.base_wt)
        self.run_id = "run-stale-bob"
        self.run_work = paths.runs_dir() / self.run_id / "work"
        git("worktree", "add", "--detach", str(self.run_work), cand,
            cwd=self.origin)
        self.cand_snap = snap_of(self.run_work)
        state = store.create(
            "fakerepo", "dep-bob-0-44-1",
            "chore(deps): bob 0.43.0 -> 0.44.1", parent, {})
        state["id"] = self.run_id
        state["dep"] = "bob"
        state["target"] = "0.44.1"
        state["lane"] = "main"
        state["branch"] = "codex/maint/bob-0-44-1"
        state["workCommit"] = cand
        state["workTreeFingerprint"] = self.cand_snap["fingerprint"]
        state["verifiedCommits"] = {"review": cand, "e2e": cand}
        state["verifiedFingerprints"] = {
            "review": self.cand_snap["fingerprint"],
            "e2e": self.cand_snap["fingerprint"]}
        state["steps"] = {"review": {"status": "done", "exit_code": 0},
                          "e2e": {"status": "done", "exit_code": 0}}
        store.save(state)
        # Baseline run owning the base batch's primary (bar @ base).
        base_run_work = paths.runs_dir() / "run-stale0" / "work"
        git("worktree", "add", "--detach", str(base_run_work), base,
            cwd=self.origin)
        base_run_snap = snap_of(base_run_work)
        bstate = store.create("fakerepo", "dep-bar-9.9.9",
                              "chore(deps): bar 9.9.8 -> 9.9.9",
                              base, {})
        bstate["id"] = "run-stale0"
        bstate["dep"] = "bar"
        bstate["target"] = "9.9.9"
        bstate["lane"] = "main"
        bstate["branch"] = "codex/maint/bar-9-9-9"
        bstate["workCommit"] = base
        bstate["workTreeFingerprint"] = base_run_snap["fingerprint"]
        bstate["verifiedCommits"] = {"review": base, "e2e": base}
        bstate["verifiedFingerprints"] = {
            "review": base_run_snap["fingerprint"],
            "e2e": base_run_snap["fingerprint"]}
        bstate["steps"] = {"review": {"status": "done", "exit_code": 0},
                           "e2e": {"status": "done", "exit_code": 0}}
        store.save(bstate)
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        ev_dir = batches.evidence_dir("test-batch-stale")
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "proof.txt").write_text("proof\n", encoding="utf-8")
        ev_sha = batches.sha256_file(ev_dir / "proof.txt")
        write_json(batches.batches_dir() / "test-batch-stale.json", {
            "schema": 3, "id": "test-batch-stale", "repo": "fakerepo",
            "target_branch": "main", "primaries": [{
                "dep": "bar", "target": "9.9.9", "lane": "main",
                "run_id": "run-stale0",
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(base_run_work),
                           "head": base,
                           "fingerprint":
                           base_run_snap["fingerprint"]}}],
            "inherited": [],
            "origin": {"checkout": str(self.base_wt),
                       "base_branch": "main", "base_head": base},
            "integrations": [{
                "lane": "main", "worktree": str(self.base_wt),
                "head": base,
                "fingerprint": self.base_snap["fingerprint"],
                "status": "integration-testing",
                "gates": [{
                    "key": "unit", "status": "pass",
                    "inputs": {"head": base,
                               "fingerprint":
                               self.base_snap["fingerprint"]},
                    "evidence": [{
                        "label": "unit proof", "kind": "file",
                        "stored": True, "stored_path": "proof.txt",
                        "sha256": ev_sha, "bytes": 6}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})
        self.batch_id = "test-batch-stale"

    def test_same_line_bump_conflict_is_stale(self):
        self._build()
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertTrue(
            str(rec.get("error") or "").startswith(
                "기준이 이미 bob를 0.43.2로 올림 — "
                "0.44.1 후보를 현재 기준에서 다시 만들어야 함"),
            rec.get("error"))
        self.assertEqual((rec.get("stale") or {}).get("base_version"),
                         "0.43.2")

    def test_conflict_on_another_dep_line_stays_true_conflict(self):
        self._build(cand_alice="0.2.0", base_alice="0.1.5")
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertNotIn("stale", rec)
        self.assertTrue(
            str(rec.get("error") or "").startswith(
                "cherry-pick conflicted or failed"), rec.get("error"))

    def test_lockfile_only_conflict_is_unchanged(self):
        self._build(base_lock_only=True)
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertNotIn("stale", rec)
        self.assertIn("yarn.lock",
                      (rec.get("result") or {}).get("conflicted_paths")
                      or [])


class PubspecPrepareTests(unittest.TestCase):
    """M3.3a prepare of a direct pubspec.yaml dependency bump.

    Slim git fixture (no baseline run needed beyond the batch
    itself): origin @ base with pubspec.yaml, candidate branch
    bumping one dep line. Offline, Py3.9 portable."""

    PUB_BASE = ("name: cal\nenvironment:\n  sdk: ^3.0.0\n"
                "dependencies:\n  intl: ^0.20.1\n")
    PUB_CAND = ("name: cal\nenvironment:\n  sdk: ^3.0.0\n"
                "dependencies:\n  intl: ^0.20.2\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git("init", "-b", "main", cwd=self.origin)
        git("config", "user.email", "test@localhost", cwd=self.origin)
        git("config", "user.name", "test", cwd=self.origin)
        (self.origin / "pubspec.yaml").write_text(
            self.PUB_BASE, encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "base", cwd=self.origin)
        self.base_head = git("rev-parse", "HEAD",
                             cwd=self.origin).strip()
        git("checkout", "-qb", "cand", cwd=self.origin)
        (self.origin / "pubspec.yaml").write_text(
            self.PUB_CAND, encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "candidate", cwd=self.origin)
        self.cand_head = git("rev-parse", "HEAD",
                             cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        self.base_wt = self.root / "home" / "base-wt"
        git("worktree", "add", "--detach", str(self.base_wt),
            self.base_head, cwd=self.origin)
        self.base_snap = snap_of(self.base_wt)
        self.run_id = "run-testpub1"
        self.run_work = paths.runs_dir() / self.run_id / "work"
        git("worktree", "add", "--detach", str(self.run_work),
            self.cand_head, cwd=self.origin)
        self.cand_snap = snap_of(self.run_work)
        state = store.create("fakerepo", "dep-intl-0-20-2",
                             "chore(deps): intl 0.20.1 -> 0.20.2",
                             self.base_head, {})
        state["id"] = self.run_id
        state["dep"] = "intl"
        state["target"] = "0.20.2"
        state["lane"] = "main"
        state["branch"] = "codex/maint/intl-0-20-2"
        state["workCommit"] = self.cand_head
        state["workTreeFingerprint"] = self.cand_snap["fingerprint"]
        state["verifiedCommits"] = {"review": self.cand_head,
                                    "e2e": self.cand_head}
        state["verifiedFingerprints"] = {
            "review": self.cand_snap["fingerprint"],
            "e2e": self.cand_snap["fingerprint"]}
        state["steps"] = {"review": {"status": "done", "exit_code": 0},
                          "e2e": {"status": "done", "exit_code": 0}}
        store.save(state)
        # Baseline run owning the base batch's READY primary (same
        # shape as PrepareMixin: accepted review, one passing gate
        # with hash-reverified file evidence bound to live bytes).
        self.base_run_id = "run-testpub0"
        self.base_run_work = (paths.runs_dir() / self.base_run_id
                              / "work")
        git("worktree", "add", "--detach", str(self.base_run_work),
            self.base_head, cwd=self.origin)
        self.base_run_snap = snap_of(self.base_run_work)
        bstate = store.create("fakerepo", "dep-bar-9.9.9",
                              "chore(deps): bar 9.9.8 -> 9.9.9",
                              self.base_head, {})
        bstate["id"] = self.base_run_id
        bstate["dep"] = "bar"
        bstate["target"] = "9.9.9"
        bstate["lane"] = "main"
        bstate["branch"] = "codex/maint/bar-9-9-9"
        bstate["workCommit"] = self.base_head
        bstate["workTreeFingerprint"] = self.base_run_snap["fingerprint"]
        bstate["verifiedCommits"] = {"review": self.base_head,
                                     "e2e": self.base_head}
        bstate["verifiedFingerprints"] = {
            "review": self.base_run_snap["fingerprint"],
            "e2e": self.base_run_snap["fingerprint"]}
        bstate["steps"] = {"review": {"status": "done", "exit_code": 0},
                           "e2e": {"status": "done", "exit_code": 0}}
        store.save(bstate)
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        ev_dir = batches.evidence_dir("test-batch-pub")
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "proof.txt").write_text("proof\n", encoding="utf-8")
        ev_sha = batches.sha256_file(ev_dir / "proof.txt")
        write_json(batches.batches_dir() / "test-batch-pub.json", {
            "schema": 3, "id": "test-batch-pub", "repo": "fakerepo",
            "target_branch": "main",
            "primaries": [{
                "dep": "bar", "target": "9.9.9", "lane": "main",
                "run_id": self.base_run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(self.base_run_work),
                           "head": self.base_head,
                           "fingerprint":
                           self.base_run_snap["fingerprint"]}}],
            "inherited": [],
            "origin": {"checkout": str(self.base_wt),
                       "base_branch": "main",
                       "base_head": self.base_head},
            "integrations": [{
                "lane": "main", "worktree": str(self.base_wt),
                "head": self.base_head,
                "fingerprint": self.base_snap["fingerprint"],
                "status": "integration-testing",
                "gates": [{
                    "key": "unit", "status": "pass",
                    "inputs": {"head": self.base_head,
                               "fingerprint":
                               self.base_snap["fingerprint"]},
                    "evidence": [{
                        "label": "unit proof", "kind": "file",
                        "stored": True, "stored_path": "proof.txt",
                        "sha256": ev_sha, "bytes": 6}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})
        self.batch_id = "test-batch-pub"

    def test_pubspec_bump_prepares(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done")
        res = rec["result"]
        self.assertEqual(
            (Path(res["path"]) / "pubspec.yaml").read_text(
                encoding="utf-8"), self.PUB_CAND)
        self.assertNotEqual(res["head"], self.base_head)


class DerivedNewestTests(unittest.TestCase):
    # M2.13: the in-prepare re-derive writes the done derived
    # record and the blocked source note in the same second, and
    # "-rederive.json" sorts before ".json" — the tie used to
    # return the blocked prepare and strand the run forever.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        self.run_id = "run-m213tie"
        self.prep_id = "prep-%s-base-1" % self.run_id
        self.at = "2026-10-23T05:59:32+00:00"

    def _op(self, op_id, **kw):
        rec = {"schema": 1, "op": "prepare", "op_id": op_id,
               "repo": "fakerepo", "run_id": self.run_id,
               "batch_id": "base-1", "dep": "foo", "target": "1.2.3",
               "lane": "next", "status": "blocked", "error": "pick",
               "created_at": self.at, "updated_at": self.at,
               "result": {"path": "/w", "branch": "codex/x",
                          "head": "", "fingerprint": ""}}
        rec.update(kw)
        write_json(batches.op_file(op_id), rec)
        return rec

    def test_tie_returns_done_rederive(self):
        self._op(self.prep_id)
        self._op(self.prep_id + "-rederive", op="reresolve",
                 derived_from=self.prep_id, kind="rederive",
                 status="done",
                 result={"path": "/w", "branch": "codex/x",
                         "head": "dea8e30", "fingerprint": "f"})
        rec = batches.latest_integration_op(self.run_id)
        self.assertEqual(rec["op_id"], self.prep_id + "-rederive")

    def test_blocked_derived_keeps_prepare(self):
        self._op(self.prep_id)
        self._op(self.prep_id + "-rederive", op="reresolve",
                 derived_from=self.prep_id, kind="rederive",
                 status="blocked")
        rec = batches.latest_integration_op(self.run_id)
        self.assertEqual(rec["op_id"], self.prep_id)

    def test_newer_lockfix_without_kind_still_wins(self):
        self._op(self.prep_id)
        self._op(self.prep_id + "-lockfix", op="reresolve",
                 derived_from=self.prep_id, status="done",
                 updated_at="2026-10-23T06:00:01+00:00",
                 result={"path": "/w", "branch": "codex/x",
                         "head": "abc123", "fingerprint": "f"})
        rec = batches.latest_integration_op(self.run_id)
        self.assertEqual(rec["op_id"], self.prep_id + "-lockfix")

    def test_foreign_derived_ignored(self):
        self._op(self.prep_id)
        self._op(self.prep_id + "-rederive", op="reresolve",
                 derived_from="prep-other-base-1", kind="rederive",
                 status="done",
                 result={"path": "/w", "branch": "codex/x",
                         "head": "dea8e30", "fingerprint": "f"})
        rec = batches.latest_integration_op(self.run_id)
        self.assertEqual(rec["op_id"], self.prep_id)


if __name__ == "__main__":
    unittest.main()
