"""Multi-commit candidate replay in prepare (S2/D2).

Covers batches.prepare_integration range handling with real git fixture
repos under an isolated OPENMAINTY_HOME: ordered replay, per-commit
conflict naming, merge/over-bound/history-move refusals, op identity
(commit_list/replayed_from), and the single-commit shape guard. No
network, no devices, no builds.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, paths, runner, store  # noqa: E402
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


class ReplayMixin(unittest.TestCase):
    """Origin @ base; candidate branch with N commits; READY base batch."""

    N_COMMITS = 2

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
        (self.origin / "a.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "base", cwd=self.origin)
        self.base_head = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-qb", "cand", cwd=self.origin)
        self.cand_heads = []
        for i in range(self.N_COMMITS):
            (self.origin / f"f{i}.txt").write_text(f"v{i}\n",
                                                  encoding="utf-8")
            git("add", "-A", cwd=self.origin)
            git("commit", "-qm", f"candidate{i + 1}", cwd=self.origin)
            self.cand_heads.append(
                git("rev-parse", "HEAD", cwd=self.origin).strip())
        git("checkout", "-q", "main", cwd=self.origin)
        self.base_wt = self.root / "home" / "base-wt"
        git("worktree", "add", "--detach", str(self.base_wt),
            self.base_head, cwd=self.origin)
        self.base_snap = snap_of(self.base_wt)
        self.run_id = "run-testreplay1"
        self.run_work = paths.runs_dir() / self.run_id / "work"
        git("worktree", "add", "--detach", str(self.run_work),
            self.cand_heads[-1], cwd=self.origin)
        self.cand_snap = snap_of(self.run_work)
        state = store.create("fakerepo", "dep-foo-1.2.3",
                             "chore(deps): foo 1.2.2 -> 1.2.3",
                             self.base_head, {})
        state["id"] = self.run_id
        state["dep"] = "foo"
        state["target"] = "1.2.3"
        state["lane"] = "main"
        state["branch"] = "codex/maint/foo-1-2-3"
        state["workCommit"] = self.cand_heads[-1]
        state["workTreeFingerprint"] = self.cand_snap["fingerprint"]
        state["verifiedCommits"] = {"review": self.cand_heads[-1],
                                    "e2e": self.cand_heads[-1]}
        state["verifiedFingerprints"] = {
            "review": self.cand_snap["fingerprint"],
            "e2e": self.cand_snap["fingerprint"]}
        state["steps"] = {"review": {"status": "done", "exit_code": 0},
                          "e2e": {"status": "done", "exit_code": 0}}
        store.save(state)
        self.base_run_id = "run-testreplay0"
        self.base_run_work = paths.runs_dir() / self.base_run_id / "work"
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
        return paths.home() / "reviews" / f"prep-{self.batch_id}-foo-1-2-3"

    def save_run(self, **fields):
        state = store.load(self.run_id)
        state.update(fields)
        store.save(state)


class ReplaySuccessTests(ReplayMixin):
    def test_ordered_replay_records_identity(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done")
        self.assertEqual([c["head"] for c in
                          rec["candidate"]["commit_list"]],
                         self.cand_heads)
        self.assertEqual([c["subject"] for c in
                          rec["candidate"]["commit_list"]],
                         ["candidate1", "candidate2"])
        self.assertEqual(rec["result"]["replayed_from"], self.cand_heads)
        res_path = Path(rec["result"]["path"])
        self.assertEqual((res_path / "f0.txt").read_text(
            encoding="utf-8"), "v0\n")
        self.assertEqual((res_path / "f1.txt").read_text(
            encoding="utf-8"), "v1\n")
        gates = {g["key"]: g["status"] for g in rec["gates"]}
        self.assertEqual(gates["source-prepared"], "pass")


class ReplayRefusalTests(ReplayMixin):
    def test_conflict_at_second_commit_names_it(self):
        real_git = batches._run_git

        def flaky(args, cwd, timeout=120):
            if (len(args) >= 2 and args[-2] == "cherry-pick"
                    and args[-1] == self.cand_heads[1]
                    and str(cwd) != str(self.run_work)):
                return subprocess.CompletedProcess(
                    args, 1, "", "error: could not apply "
                    f"{self.cand_heads[1][:7]}... candidate2")
            return real_git(args, cwd, timeout=timeout)

        with mock.patch.object(batches, "_run_git", side_effect=flaky):
            rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("commit 2/2", rec["error"])
        self.assertIn(self.cand_heads[1][:12], rec["error"])
        self.assertIn("candidate2", rec["error"])
        # Worktree removed; nothing integrated.
        self.assertFalse(self.op_path().exists())

    def test_merge_in_range_refused_before_mutation(self):
        git("checkout", "-q", "cand", cwd=self.origin)
        git("checkout", "-qb", "side", self.base_head, cwd=self.origin)
        (self.origin / "side.txt").write_text("side\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "side", cwd=self.origin)
        git("checkout", "-q", "cand", cwd=self.origin)
        git("merge", "--no-ff", "-qm", "merge-side", "side",
            cwd=self.origin)
        merged = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        git("worktree", "remove", "--force", str(self.run_work),
            cwd=self.origin)
        git("worktree", "add", "--detach", str(self.run_work), merged,
            cwd=self.origin)
        snap = snap_of(self.run_work)
        self.save_run(workCommit=merged,
                      workTreeFingerprint=snap["fingerprint"],
                      verifiedCommits={"review": merged, "e2e": merged},
                      verifiedFingerprints={"review": snap["fingerprint"],
                                            "e2e": snap["fingerprint"]})
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertIn("merge commit", str(ctx.exception))
        self.assertFalse(self.op_path().exists())
        self.assertNotIn("codex/review-foo-1-2-3",
                         git("branch", "--list", cwd=self.origin))

    def test_history_move_refused(self):
        real_git = batches._run_git

        def drifting(args, cwd, timeout=120):
            if (len(args) >= 2 and args[0] == "rev-list"
                    and args[1] == "--reverse"):
                out = real_git(args, cwd, timeout=timeout)
                fake = "0" * 40
                return subprocess.CompletedProcess(
                    out.args, 0, out.stdout + fake + "\n", out.stderr)
            return real_git(args, cwd, timeout=timeout)

        with mock.patch.object(batches, "_run_git", side_effect=drifting):
            with self.assertRaises(batches.IntegrationError) as ctx:
                batches.prepare_integration(self.run_id, self.batch_id)
        self.assertIn("history moved", str(ctx.exception))
        self.assertFalse(self.op_path().exists())


class ReplayBoundTests(ReplayMixin):
    N_COMMITS = 6

    def test_over_bound_refused_with_count(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertIn("6-commit range", str(ctx.exception))
        self.assertIn("at most 5", str(ctx.exception))
        self.assertFalse(self.op_path().exists())


class SingleShapeGuardTests(ReplayMixin):
    N_COMMITS = 1

    def test_single_commit_carries_no_replay_keys(self):
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done")
        self.assertNotIn("commit_list", rec["candidate"])
        self.assertNotIn("replayed_from", rec["result"])
        self.assertEqual(rec["candidate"]["head"], self.cand_heads[0])


if __name__ == "__main__":
    unittest.main()
