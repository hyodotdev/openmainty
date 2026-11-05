"""Typed issue prepare → register → pending independent review.

Offline coverage for the Source13 transport: strict issue admission
(validators, queue/run/profile binding, no missing-queue fallback),
original-vs-integration source separation
(m2.build_issue_batch_manifest), the register route/binder contract
(batches.register_issue_batch + serve.api_integration_register with
lease/confirm guards), idempotent reuse, stale/corrupt refusals with
byte preservation, and the direct-import op-lock ordering.

Real temp git repos under an isolated OPENMAINTY_HOME. No network,
no model calls, no target writes, no execution of the product under
test beyond the direct helper/route calls below.
"""
import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import batches, m2, repos, runner, sched, serve, store  # noqa: E402
from openmainty.util import write_json  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def git(*args, cwd):
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "-C", str(cwd)] + list(args),
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout.strip()


class IssueFixtureMixin(unittest.TestCase):
    """Seeded issue fixture mirroring the real #856 shape.

    Queue carries kind issue-triage with base_branch (no queue lane —
    the actual stored shape); the run carries lane/base_branch/branch;
    the profile carries the canonical GitHub slug plus a nested
    fastcheck section. ORIGINAL head (cand) and RESULT head (res)
    always differ, and the original branch already exists in the
    shared store, so prepare/register must use a NEW branch.
    """
    repo = "fakerepo"
    slug = "example/fakerepo"
    run_id = "run-issue-856"
    number = "856"
    lane = "next"
    branch = "codex/next/issue-856"
    op_id = "prep-issue-856-t1"
    batch_id = "test-batch"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        repos.save({"name": self.repo, "github": self.slug,
                    "baseBranch": "main", "knowledge": [], "checks": [],
                    "e2e": [], "release": {},
                    "fastcheck": {"steps": [{"argv": ["true"],
                                             "gate": "unit"}],
                                   "toolchain": []}})
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git("init", "-b", "main", cwd=self.origin)
        (self.origin / "f.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "base", cwd=self.origin)
        self.base_head = git("rev-parse", "HEAD", cwd=self.origin)
        git("checkout", "-qb", self.branch, cwd=self.origin)
        (self.origin / "f.txt").write_text("issue change\n", encoding="utf-8")
        (self.origin / "g.txt").write_text("new\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "candidate", cwd=self.origin)
        self.cand_head = git("rev-parse", "HEAD", cwd=self.origin)
        git("checkout", "-qb", "codex/review-issue-856-test",
            cwd=self.origin)
        (self.origin / "f.txt").write_text("integrated\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "integrate", cwd=self.origin)
        self.res_head = git("rev-parse", "HEAD", cwd=self.origin)
        git("checkout", "-q", "main", cwd=self.origin)
        self.assertNotEqual(self.cand_head, self.res_head)
        # A distinct base-candidate commit for the accepted base run:
        # the issue patch replays cleanly on the base integration, so
        # result != original is meaningful (not a same-parent trick).
        git("checkout", "-qb", "codex/next/base-bar", cwd=self.origin)
        (self.origin / "bar.txt").write_text("bar\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "base bar", cwd=self.origin)
        self.bar_head = git("rev-parse", "HEAD", cwd=self.origin)
        git("checkout", "-q", "main", cwd=self.origin)
        # Base integration worktree @ B (the approved source). The
        # supported _snapshot helper is the source-clean contract;
        # runner.source_snapshot carries no clean key.
        self.base_wt = self.root / "home" / "base-wt"
        git("worktree", "add", "--detach", str(self.base_wt),
            self.base_head, cwd=self.origin)
        self.base_snap = batches._snapshot(str(self.base_wt))
        self.assertIsNotNone(self.base_snap)
        self.assertEqual(self.base_snap["head"], self.base_head)
        self.assertTrue(self.base_snap["clean"])
        # Base-candidate worktree ON its owned queue branch (the dep
        # validator requires a live checkout on the queue branch).
        self.base_bar_wt = self.root / "home" / "base-bar-wt"
        git("worktree", "add", str(self.base_bar_wt),
            "codex/next/base-bar", cwd=self.origin)
        self.bar_snap = batches._snapshot(str(self.base_bar_wt))
        self.assertIsNotNone(self.bar_snap)
        self.assertEqual(self.bar_snap["head"], self.bar_head)
        self.assertTrue(self.bar_snap["clean"])
        # Candidate run worktree @ C (the ORIGINAL bytes under proof).
        self.run_work = store.run_dir(self.run_id) / "work"
        git("worktree", "add", "--detach", str(self.run_work),
            self.cand_head, cwd=self.origin)
        self.cand_snap = batches._snapshot(str(self.run_work))
        self.assertIsNotNone(self.cand_snap)
        self.assertEqual(self.cand_snap["head"], self.cand_head)
        self.assertTrue(self.cand_snap["clean"])
        # Prepared integration worktree @ R on the NEW branch.
        self.result_wt = self.root / "home" / "result-wt"
        git("worktree", "add", str(self.result_wt),
            "codex/review-issue-856-test", cwd=self.origin)
        self.res_snap = batches._snapshot(str(self.result_wt))
        self.assertIsNotNone(self.res_snap)
        self.assertEqual(self.res_snap["head"], self.res_head)
        self.assertTrue(self.res_snap["clean"])
        state = store.create(self.repo, self.number,
                             "Web demo is not working", self.base_head,
                             {"by": "t"})
        state["id"] = self.run_id
        state["lane"] = self.lane
        state["base_branch"] = self.lane
        state["branch"] = self.branch
        # Actual #856 stores the same app-owned queue key on the run.
        state["queue_key"] = f"{self.repo}#issue-{self.number}"
        state["sourceKind"] = "git"
        state["workCommit"] = self.cand_head
        state["workTreeFingerprint"] = self.cand_snap["fingerprint"]
        state["verifiedCommits"] = {"review": self.cand_head,
                                    "e2e": self.cand_head}
        state["verifiedFingerprints"] = {
            "review": self.cand_snap["fingerprint"],
            "e2e": self.cand_snap["fingerprint"]}
        for step in store.VERIFY_STEPS:
            state["steps"][step]["status"] = "done"
            state["steps"][step]["exit_code"] = 0
        store.save(state)
        st = sched.load(self.repo)
        st.setdefault("queue", []).append(
            {"key": f"{self.repo}#issue-{self.number}", "repo": self.repo,
             "kind": "issue-triage", "issue": self.number,
             "base_branch": self.lane, "status": "waiting-review",
             "run_id": self.run_id, "branch": self.branch})
        # A REAL accepted dep run + queue binding for the base batch:
        # without these the base batch can never evaluate READY and
        # prepare would refuse at the base-READY gate for the wrong
        # reason. The base candidate lives ON its queue branch.
        self.base_run_id = "run-base-bar"
        base_run = store.create(self.repo, "bar@9.9.9", "bar",
                                self.base_head, {"by": "t"})
        base_run.update({"id": self.base_run_id, "dep": "bar",
                         "target": "9.9.9", "lane": self.lane,
                         "base_branch": self.lane,
                         "branch": "codex/next/base-bar",
                         "workCommit": self.bar_head,
                         "workTreeFingerprint":
                         self.bar_snap["fingerprint"],
                         "verifiedCommits": {"review": self.bar_head,
                                              "e2e": self.bar_head},
                         "verifiedFingerprints": {
                             "review": self.bar_snap["fingerprint"],
                             "e2e": self.bar_snap["fingerprint"]}})
        store.save(base_run)
        for step in store.VERIFY_STEPS:
            base_run["steps"][step]["status"] = "done"
            base_run["steps"][step]["exit_code"] = 0
        store.save(base_run)
        # The dep validator's fresh-proof gate reads the candidate
        # checkout under the run dir (same contract as issue runs).
        git("worktree", "add", "--detach",
            str(store.run_dir(self.base_run_id) / "work"),
            self.bar_head, cwd=self.origin)
        st.setdefault("queue", []).append(
            {"key": f"{self.repo}|bar|9.9.9|{self.lane}",
             "repo": self.repo, "dep": "bar", "target": "9.9.9",
             "lane": self.lane, "base_branch": self.lane,
             "branch": "codex/next/base-bar",
             "run_id": self.base_run_id, "status": "waiting-review"})
        sched.save(st)
        self._write_base_batch()

    def _write_base_batch(self):
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        ev_dir = batches.evidence_dir(self.batch_id)
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "proof.txt").write_text("proof\n", encoding="utf-8")
        ev_sha = batches.sha256_file(ev_dir / "proof.txt")
        # Fresh input-bound stored proof for the configured independent
        # Root-review gate: the base is genuinely READY (its own review
        # landed), while the NEW registered batch keeps Root-review
        # pending on the new bytes.
        (ev_dir / "root-proof.txt").write_text("root review\n",
                                               encoding="utf-8")
        root_sha = batches.sha256_file(ev_dir / "root-proof.txt")
        write_json(batches.batches_dir() / (self.batch_id + ".json"), {
            "schema": 3, "id": self.batch_id, "repo": self.repo,
            "target_branch": self.lane, "primaries": [{
                "dep": "bar", "target": "9.9.9", "lane": self.lane,
                "run_id": self.base_run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit", "root-review"],
                "source": {"worktree": str(self.base_bar_wt),
                           "branch": "codex/next/base-bar",
                           "head": self.bar_head,
                           "fingerprint":
                           self.bar_snap["fingerprint"]}}],
            "inherited": [],
            "origin": {"checkout": str(self.base_wt),
                       "base_branch": "main", "base_head": self.base_head},
            "integrations": [{"lane": self.lane,
                              "worktree": str(self.base_wt),
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
                                      "sha256": ev_sha,
                                      "bytes": 6}]},
                                 {"key": "root-review", "status": "pass",
                                  "root_attested": True,
                                  "inputs": {
                                      "head": self.base_head,
                                      "fingerprint":
                                      self.base_snap["fingerprint"]},
                                  "evidence": [{
                                      "label": "root review proof",
                                      "kind": "file", "stored": True,
                                      "stored_path": "root-proof.txt",
                                      "sha256": root_sha,
                                      "bytes": 12}]}]}],
            "policy": {"gates": {
                "unit": {"label": "unit"},
                "root-review": {"label": "Root review",
                                "root": True}},
                       "required_gates": ["unit", "root-review"]}})

    def _write_op(self, status="done", with_issue=True, batch_id=None,
                  result_head=None, result_fp=None, gates=None):
        rec = {"op": "prepare", "op_id": self.op_id, "status": status,
               "repo": self.repo, "run_id": self.run_id, "lane": self.lane,
               "batch_id": self.batch_id if batch_id is None else batch_id,
               "candidate": {"head": self.cand_head,
                             "fingerprint": self.cand_snap["fingerprint"],
                             "worktree": str(self.run_work),
                             "branch": self.branch},
               "result": {"path": str(self.result_wt),
                          "head": (self.res_head if result_head is None
                                   else result_head),
                          "fingerprint": (self.res_snap["fingerprint"]
                                          if result_fp is None else result_fp),
                          "branch": "codex/review-issue-856-test"}}
        if with_issue:
            rec["issue"] = {"issue": self.number, "repo": self.repo,
                            "lane": self.lane,
                            "url": ("https://github.com/" + self.slug +
                                    "/issues/" + self.number)}
        rec["gates"] = ([{"key": "fastchecks", "status": "pass",
                           "attested_by": "worker",
                           "receipt_sha256": self._write_receipt()}]
                        if gates is None else gates)
        write_json(batches.op_file(self.op_id), rec)
        return rec

    def _write_receipt(self, root_attested=False):
        from openmainty import fastcheck
        log_path = (batches.integration_ops_dir() /
                    (self.op_id + ".fastcheck.0.log"))
        log_path.write_bytes(b"unit ok\n")
        log_sha = hashlib.sha256(b"unit ok\n").hexdigest()
        steps = [{"argv": ["true"], "gate": "unit", "install": False,
                  "timeout_s": 600, "network_sandbox": "strict",
                  "head": self.res_head,
                  "fingerprint": self.res_snap["fingerprint"],
                  "rc": 0, "timed_out": False,
                  "log": str(log_path), "log_sha256": log_sha}]
        summary = {"op": self.op_id, "ok": True, "steps": steps,
                   "worker_attested": True, "attestor": "worker",
                   "root_attested": root_attested}
        write_json(fastcheck.op_fastcheck_path(self.op_id), summary)
        return fastcheck._receipt_sha(
            fastcheck.op_fastcheck_path(self.op_id))

    def _base_live(self):
        # Live map + diagnostics for the accepted base batch, mirroring
        # exactly what prepare_integration feeds evaluate (closure +
        # live snapshots).
        base = batches.load_batch(self.batch_id)
        return batches.live_map_with_diag(batches.closure([base]))

    def _assert_base_ready(self):
        # The genuine READY base (worker:unit pass + independent
        # Root-review landed with fresh stored proofs) that the
        # positive prepare/register paths require.
        base = batches.load_batch(self.batch_id)
        live, diag = self._base_live()
        results = batches.evaluate(
            base, live, live_diag=diag, record=True).get("results") or []
        self.assertTrue(results)
        for res in results:
            self.assertTrue(res.get("ready"), res.get("reasons"))


class AdmissionTests(IssueFixtureMixin):
    def test_valid_number_branch_slug(self):
        self.assertEqual(batches._strict_issue_number("856"), "856")
        self.assertEqual(
            batches._strict_issue_branch("codex/next/issue-856"),
            "codex/next/issue-856")
        self.assertEqual(batches._strict_github_slug("example/fakerepo"),
                         "example/fakerepo")

    def test_number_rejects_non_canonical(self):
        for bad in (True, False, 0, 856, "0", "00", "01", " 856", "856 ",
                    "856\n", "8５6", "８５６", "", None, "856a",
                    "1" * 21, "１２３"):
            with self.subTest(bad=bad):
                self.assertIsNone(batches._strict_issue_number(bad))

    def test_branch_rejects_non_codex_traversal(self):
        for bad in ("", None, 7, "main", "codex/", "codex//issue-856",
                    "codex/../escape", "codex/next/issue-856 ",
                    "codex/next/issue 856"):
            with self.subTest(bad=bad):
                self.assertIsNone(batches._strict_issue_branch(bad))

    def test_slug_rejects_malformed(self):
        for bad in ("", None, "noslash", "a/b/c", "a//b", " a/b", "a/b ",
                    "https://github.com/a/b", "a/b?x=1", "a/b\n"):
            with self.subTest(bad=bad):
                self.assertIsNone(batches._strict_github_slug(bad))

    def test_slug_returns_canonical_string(self):
        self.assertEqual(batches._strict_github_slug("example/fakerepo"),
                         "example/fakerepo")

    def test_key_namespace_distinct_from_dep(self):
        issue_key = batches.issue_candidate_key(
            self.repo, self.lane, self.number)
        self.assertEqual(issue_key, "issue:fakerepo:next:856")
        legacy = batches.primary_result_key(
            self.repo, {"dep": "fakerepo", "target": "next", "lane": ""})
        self.assertEqual(legacy, "fakerepo|fakerepo|next|")
        self.assertNotEqual(issue_key, legacy)

    def test_primary_result_key_issue_vs_malformed(self):
        good = {"issue": {"issue": "856", "repo": self.repo,
                          "lane": self.lane},
                "lane": self.lane, "run_id": self.run_id}
        self.assertEqual(batches.primary_result_key(self.repo, good),
                         "issue:fakerepo:next:856")
        for bad in ({"dep": "x", "issue": {"issue": "856"}},
                    {"dep": "x", "target": "y",
                     "issue": {"issue": "856", "repo": self.repo,
                               "lane": self.lane}},
                    {"issue": {"issue": "856"}},
                    {"issue": {"issue": "856", "repo": "other/r",
                               "lane": self.lane}},
                    {"issue": {"issue": "0", "repo": self.repo,
                               "lane": self.lane}},
                    {"issue": {"issue": "856", "repo": self.repo}}):
            with self.subTest(bad=bad):
                self.assertIsNone(
                    batches.primary_result_key(self.repo, bad))
        # A pure dependency primary keeps its exact legacy key and
        # never collides with the issue namespace; a PARTIAL dep
        # (missing target) is invalid, never a valid legacy key.
        self.assertEqual(
            batches.primary_result_key(
                self.repo, {"dep": "d", "target": "1.0"}),
            "fakerepo|d|1.0|")
        self.assertIsNone(
            batches.primary_result_key(self.repo, {"dep": "d"}))
        self.assertIsNone(
            batches.primary_result_key(
                self.repo, {"dep": "d", "target": ""}))
        self.assertIsNone(
            batches.primary_result_key(
                self.repo, {"dep": "", "target": "1.0"}))

    def test_effective_lane_agreement_and_contradiction(self):
        queue = {"base_branch": "next", "branch": self.branch}
        run = {"lane": "next", "base_branch": "next",
               "branch": self.branch}
        self.assertEqual(
            batches.issue_queue_effective_lane(queue, run), "next")
        queue2 = {"lane": "next", "base_branch": "next",
                  "branch": self.branch}
        self.assertEqual(
            batches.issue_queue_effective_lane(queue2, run), "next")
        bad_queue = {"lane": "main", "base_branch": "next",
                     "branch": self.branch}
        self.assertEqual(
            batches.issue_queue_effective_lane(bad_queue, run), "")
        bad_run = {"lane": "main", "base_branch": "next",
                   "branch": self.branch}
        self.assertEqual(
            batches.issue_queue_effective_lane(queue2, bad_run), "")
        other_branch_run = dict(run, branch="codex/next/other")
        self.assertEqual(
            batches.issue_queue_effective_lane(queue2,
                                               other_branch_run), "")

    def test_uncovered_admits_only_issue_triage_kind(self):
        # Discussions, PRs and unknown numeric rows are not issues:
        # uncovered_candidates must return the valid issue-triage row
        # (with its namespaced issue key) while omitting the rest,
        # even when they carry a numeric issue-looking field.
        base = {"repo": self.repo, "issue": "856", "branch": self.branch,
                "status": "waiting-review", "run_id": "r-1",
                "key": f"{self.repo}#issue-856",
                "lane": self.lane, "base_branch": self.lane}
        rows = [dict(base, kind="issue-triage"),
                dict(base, kind="discussion", key="k-d"),
                dict(base, kind="pr", key="k-p"),
                dict(base, kind="unknown", key="k-u"),
                dict(base, key="k-nokind")]
        out = batches.uncovered_candidates(rows, [], self.repo)
        keys = [c["key"] for c in out]
        self.assertEqual(keys, ["issue:fakerepo:next:856"])

    def test_primary_items_never_displays_mixed_as_pure_issue(self):
        # A mixed dep+issue record is invalid (primary_result_key
        # marks it None); the list projection over an ACTUAL batch
        # shape must skip it while still rendering an adjacent valid
        # pure-issue row.
        mixed = {"dep": "turbo", "target": "2.11.10",
                 "lane": "next", "run_id": "r-x",
                 "issue": {"issue": "856", "repo": self.repo,
                           "lane": "next"}}
        self.assertIsNone(batches.primary_result_key(self.repo, mixed))
        pure = {"dep": "", "target": "", "lane": "next",
                "run_id": self.run_id,
                "issue": {"issue": "856", "repo": self.repo,
                          "lane": "next"}}
        items = batches._primary_items(
            {"repo": self.repo, "primaries": [mixed, pure]})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["issue"]["issue"], "856")
        self.assertEqual(items[0]["run_id"], self.run_id)

    def test_resolve_positive_matches_fixture_shape(self):
        ctx = batches._resolve_issue_candidate(self.repo, self.run_id)
        self.assertEqual(ctx["number"], "856")
        self.assertEqual(ctx["lane"], "next")
        self.assertEqual(ctx["branch"], self.branch)
        self.assertEqual(ctx["url"],
                         "https://github.com/example/fakerepo/issues/856")
        self.assertEqual(ctx["repo"], self.repo)
        self.assertEqual(ctx["run_id"], self.run_id)

    def _op_json_names(self):
        d = batches.integration_ops_dir()
        if not d.is_dir():
            return set()
        return set(p.name for p in d.glob("*.json"))

    def test_resolve_rejects_before_any_write(self):
        before = self._op_json_names()
        st = sched.load(self.repo)
        st["queue"] = [dict(q, kind="discussion") for q in st["queue"]]
        sched.save(st)
        with self.assertRaises(batches.IntegrationError):
            batches._resolve_issue_candidate(self.repo, self.run_id)
        self.assertEqual(before, self._op_json_names())

    def test_resolve_rejects_run_issue_mismatch(self):
        state = store.load(self.run_id)
        state["issue"] = "999"
        store.save(state)
        with self.assertRaises(batches.IntegrationError):
            batches._resolve_issue_candidate(self.repo, self.run_id)

    def test_resolve_rejects_branch_mismatch(self):
        state = store.load(self.run_id)
        state["branch"] = "codex/next/other"
        store.save(state)
        with self.assertRaises(batches.IntegrationError):
            batches._resolve_issue_candidate(self.repo, self.run_id)

    def test_resolve_requires_queue_key_binding(self):
        # The queue record key must name this exact repo issue
        # ("fakerepo#issue-856"); an otherwise valid row with a
        # forged/unrelated key refuses before any write.
        st = sched.load(self.repo)
        for row in st["queue"]:
            if row.get("run_id") == self.run_id:
                row["key"] = "forged-key"
        sched.save(st)
        before = self._op_json_names()
        with self.assertRaises(batches.IntegrationError):
            batches._resolve_issue_candidate(self.repo, self.run_id)
        self.assertEqual(before, self._op_json_names())
        st = sched.load(self.repo)
        for row in st["queue"]:
            if row.get("run_id") == self.run_id:
                row["key"] = 856
        sched.save(st)
        with self.assertRaises(batches.IntegrationError):
            batches._resolve_issue_candidate(self.repo, self.run_id)

    def test_resolve_rejects_stored_run_queue_key_mismatch(self):
        # This typed issue path requires the nonempty canonical stored
        # run key (actual #856 carries it): a missing, empty,
        # malformed, or mismatched stored key refuses, with the stored
        # state left untouched.
        state = store.load(self.run_id)
        for bad in ("forged-key", True, None, ""):
            with self.subTest(bad=bad):
                state["queue_key"] = bad
                store.save(state)
                with self.assertRaises(batches.IntegrationError):
                    batches._resolve_issue_candidate(self.repo, self.run_id)
                self.assertEqual(
                    store.load(self.run_id)["queue_key"], bad)
        del state["queue_key"]
        store.save(state)
        with self.assertRaises(batches.IntegrationError):
            batches._resolve_issue_candidate(self.repo, self.run_id)

    def test_resolve_rejects_non_codex_branch(self):
        # A queue-owned branch outside codex/ is not a strict owned
        # branch even when both sides agree — no string-coerced pass.
        st = sched.load(self.repo)
        for row in st["queue"]:
            if row.get("run_id") == self.run_id:
                row["branch"] = "main"
        sched.save(st)
        state = store.load(self.run_id)
        state["branch"] = "main"
        store.save(state)
        with self.assertRaises(batches.IntegrationError):
            batches._resolve_issue_candidate(self.repo, self.run_id)

    def test_resolve_rejects_non_string_branch(self):
        st = sched.load(self.repo)
        for row in st["queue"]:
            if row.get("run_id") == self.run_id:
                row["branch"] = ["codex/next/issue-856"]
        sched.save(st)
        with self.assertRaises(batches.IntegrationError):
            batches._resolve_issue_candidate(self.repo, self.run_id)

    def test_effective_lane_rejects_present_malformed_types(self):
        # Present-but-wrong-type lanes must refuse, never be silently
        # dropped to force agreement among the remaining fields. A
        # MISSING lane key stays absent (real #856 has no queue lane);
        # a PRESENT None/list/bool/numeric is malformed, not missing.
        run = {"lane": "next", "base_branch": "next",
               "branch": self.branch}
        for bad in ({"lane": 5}, {"lane": ["next"]},
                    {"base_branch": True}, {"lane": "NEXT"},
                    {"lane": " next "}, {"lane": None},
                    {"base_branch": None}, {"lane": False},
                    {"lane": 0}, {"lane": ""},
                    {"base_branch": ""}):
            queue = dict({"base_branch": "next",
                          "branch": self.branch}, **bad)
            with self.subTest(bad=bad):
                self.assertEqual(
                    batches.issue_queue_effective_lane(queue, run), "")

    def test_resolve_rejects_changed_profile_origin(self):
        # Admission resolves the CURRENT profile slug; the stale
        # prepare record keeps the old URL, so registration — not
        # admission — is where the rebind refuses.
        repos.save({"name": self.repo, "github": "example/moved",
                    "baseBranch": "main"})
        ctx = batches._resolve_issue_candidate(self.repo, self.run_id)
        self.assertEqual(
            ctx["url"], "https://github.com/example/moved/issues/856")


class ManifestTests(IssueFixtureMixin):
    def _manifest(self, rec=None, issue_ctx=None):
        if rec is None:
            rec = self._write_op()
        if issue_ctx is None:
            issue_ctx = batches._resolve_issue_candidate(self.repo,
                                                         self.run_id)
        return m2.build_issue_batch_manifest(
            self.repo, self.run_id, self.lane, rec, issue_ctx,
            self.batch_id, {"steps": []},
            repos.load(self.repo))

    def test_manifest_separates_original_and_integration(self):
        manifest = self._manifest()
        primary = manifest["primaries"][0]
        self.assertEqual(primary["source"]["head"], self.cand_head)
        self.assertEqual(primary["source"]["fingerprint"],
                         self.cand_snap["fingerprint"])
        self.assertEqual(primary["source"]["worktree"], str(self.run_work))
        self.assertEqual(primary["source"]["branch"], self.branch)
        self.assertEqual(primary["issue"]["issue"], "856")
        self.assertEqual(primary["issue"]["url"],
                         "https://github.com/example/fakerepo/issues/856")
        # The integration binding carries the result in the
        # integration WORKTREE plus the worker gate inputs — never a
        # top-level result head/fingerprint on the integration record.
        integ = manifest["integrations"][0]
        self.assertEqual(integ["worktree"], str(self.result_wt))
        self.assertNotIn("head", integ)
        self.assertNotIn("fingerprint", integ)
        by_key = {g["key"]: g for g in integ["gates"]}
        self.assertEqual(by_key["worker:unit"]["inputs"]["head"],
                         self.res_head)
        self.assertEqual(by_key["worker:unit"]["inputs"]["fingerprint"],
                         self.res_snap["fingerprint"])
        self.assertNotEqual(primary["source"]["head"],
                            by_key["worker:unit"]["inputs"]["head"])
        self.assertEqual(manifest["origin_m2"], self.op_id)
        # Purpose/count assertions follow the real configured required
        # list — the fixture base genuinely requires unit + Root-review,
        # so both stay pending on the new batch.
        required = batches.load_batch(
            self.batch_id)["policy"].get("required_gates", [])
        self.assertIn("root-review", required)
        self.assertIn("Root 확인 대기 %d건" % len(required),
                      manifest["purpose"])

    def test_manifest_refuses_issue_mismatch(self):
        rec = self._write_op()
        rec["issue"] = {"issue": "999", "repo": self.repo,
                        "lane": self.lane,
                        "url": "https://github.com/example/fakerepo/issues/999"}
        with self.assertRaises(ValueError):
            self._manifest(rec=rec)

    def test_manifest_refuses_bad_number_and_lane(self):
        rec = self._write_op()
        ctx = batches._resolve_issue_candidate(self.repo, self.run_id)
        bad = dict(ctx, number="0")
        with self.assertRaises(ValueError):
            m2.build_issue_batch_manifest(
                self.repo, self.run_id, self.lane, rec, bad,
                self.batch_id, {"steps": []}, repos.load(self.repo))
        # Stored-level bool/None numbers (True == 1 in Python) must
        # refuse at the manifest boundary, never stringify into a
        # valid-looking issue identity.
        for bad_number in (True, False, None):
            with self.subTest(bad_number=bad_number):
                bad_ctx = dict(ctx, number=bad_number)
                with self.assertRaises(ValueError):
                    m2.build_issue_batch_manifest(
                        self.repo, self.run_id, self.lane, rec, bad_ctx,
                        self.batch_id, {"steps": []},
                        repos.load(self.repo))
        bad_lane = dict(ctx, lane="other")
        with self.assertRaises(ValueError):
            m2.build_issue_batch_manifest(
                self.repo, self.run_id, self.lane, rec, bad_lane,
                self.batch_id, {"steps": []}, repos.load(self.repo))

    def test_primary_key_matches_manifest_binding(self):
        manifest = self._manifest()
        primary = manifest["primaries"][0]
        self.assertEqual(
            batches.primary_result_key(self.repo, primary),
            "issue:fakerepo:next:856")


class RegisterTests(IssueFixtureMixin):
    def test_positive_registers_pending_review(self):
        self._write_op()
        # The base is genuinely READY (worker:unit pass + independent
        # Root-review landed with fresh stored proofs) before the
        # positive register path that requires it.
        self._assert_base_ready()
        base_gates = {g["key"]: g for g in batches.load_batch(
            self.batch_id)["integrations"][0]["gates"]}
        self.assertEqual(base_gates["root-review"]["status"], "pass")
        self.assertTrue(base_gates["root-review"].get("root_attested"))
        out = batches.register_issue_batch(self.op_id)
        bid = m2.batch_id_for_op(self.op_id)
        self.assertEqual(out["id"], bid)
        self.assertFalse(out["reused"])
        self.assertEqual(out["primaries"], 1)
        b = batches.load_batch(bid)
        primary = b["primaries"][0]
        self.assertEqual(primary["dep"], "")
        self.assertEqual(primary["target"], "")
        self.assertEqual(
            primary["issue"],
            {"issue": "856", "repo": self.repo, "lane": self.lane,
             "url": "https://github.com/example/fakerepo/issues/856"})
        self.assertEqual(primary["run_id"], self.run_id)
        self.assertEqual(primary["source"]["worktree"], str(self.run_work))
        self.assertEqual(primary["source"]["head"], self.cand_head)
        self.assertEqual(primary["source"]["branch"], self.branch)
        self.assertEqual(primary["review"]["verdict"], "pending")
        integ = b["integrations"][0]
        self.assertEqual(integ["lane"], self.lane)
        self.assertEqual(integ["worktree"], str(self.result_wt))
        by_key = {g["key"]: g for g in integ["gates"]}
        self.assertEqual(by_key["worker:unit"]["status"], "pass")
        self.assertEqual(by_key["worker:unit"]["inputs"]["head"],
                         self.res_head)
        self.assertEqual(by_key["worker:unit"]["inputs"]["fingerprint"],
                         self.res_snap["fingerprint"])
        # The worker companion pass is not independent Root proof.
        self.assertFalse(by_key["worker:unit"].get("root_attested"))
        # The configured Root-review gate stays pending on the new
        # bytes, separate from the worker pass.
        self.assertEqual(by_key["root-review"]["status"], "pending")
        self.assertFalse(by_key["root-review"].get("root_attested"))
        # Original branch/record bytes are untouched.
        self.assertEqual(git("rev-parse", self.branch, cwd=self.origin),
                         self.cand_head)
        self.assertEqual(b["origin_m2"], self.op_id)

    def test_idempotent_reuse_preserves_bytes(self):
        self._write_op()
        first = batches.register_issue_batch(self.op_id)
        bid = first["id"]
        raw = batches.batch_file(bid).read_bytes()
        second = batches.register_issue_batch(self.op_id)
        self.assertEqual(second["id"], bid)
        self.assertTrue(second["reused"])
        self.assertEqual(batches.batch_file(bid).read_bytes(), raw)

    def test_stale_result_refuses_and_preserves(self):
        self._write_op()
        bid = batches.register_issue_batch(self.op_id)["id"]
        raw = batches.batch_file(bid).read_bytes()
        git("commit", "-q", "--allow-empty", "-m", "drift",
            cwd=self.result_wt)
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)
        self.assertEqual(batches.batch_file(bid).read_bytes(), raw)

    def test_stale_original_refuses(self):
        self._write_op()
        (self.run_work / "f.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)

    def test_corrupt_batch_preserved_on_reregister(self):
        self._write_op()
        bid = batches.register_issue_batch(self.op_id)["id"]
        batches.batch_file(bid).write_bytes(b"{broken")
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)
        self.assertEqual(batches.batch_file(bid).read_bytes(), b"{broken")

    def test_empty_object_batch_preserved_not_overwritten(self):
        # A present-but-empty batch object is a stored record, not an
        # absence: register must refuse and preserve it, never treat
        # it as missing and write a fresh batch over it.
        self._write_op()
        bid = batches.register_issue_batch(self.op_id)["id"]
        batches.batch_file(bid).write_bytes(b"{}")
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)
        self.assertEqual(batches.batch_file(bid).read_bytes(), b"{}")

    def test_empty_list_batch_preserved_not_overwritten(self):
        self._write_op()
        bid = batches.register_issue_batch(self.op_id)["id"]
        batches.batch_file(bid).write_bytes(b"[]")
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)
        self.assertEqual(batches.batch_file(bid).read_bytes(), b"[]")

    def test_null_batch_preserved_not_overwritten(self):
        # A present null batch file parses as a non-dict absence of
        # content, not a missing file: register must refuse and keep
        # the exact bytes, never treat it as absent and overwrite it.
        self._write_op()
        bid = batches.register_issue_batch(self.op_id)["id"]
        batches.batch_file(bid).write_bytes(b"null")
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)
        self.assertEqual(batches.batch_file(bid).read_bytes(), b"null")

    def test_wrongshape_batch_preserved_not_overwritten(self):
        # A present non-empty wrong shape (non-dict JSON, or a dict
        # that is not this op's batch) is a stored record, not an
        # absence: refuse and preserve the exact bytes in both cases.
        self._write_op()
        bid = batches.register_issue_batch(self.op_id)["id"]
        for raw in (b"[1]", b'"str"', b'{"foo": 1}'):
            with self.subTest(raw=raw):
                batches.batch_file(bid).write_bytes(raw)
                with self.assertRaises(batches.IntegrationError):
                    batches.register_issue_batch(self.op_id)
                self.assertEqual(
                    batches.batch_file(bid).read_bytes(), raw)

    def test_unknown_op_404(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.register_issue_batch("prep-no-such-op")
        self.assertEqual(ctx.exception.http_status, 404)

    def test_non_prepare_op_refused(self):
        self._write_op(status="blocked")
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)

    def test_non_issue_op_refused(self):
        self._write_op(with_issue=False)
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)

    def test_missing_base_batch_refused(self):
        self._write_op(batch_id="no-such-base")
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)

    def test_moved_queue_refuses(self):
        self._write_op()
        batches.register_issue_batch(self.op_id)
        st = sched.load(self.repo)
        st["queue"][0]["issue"] = "999"
        sched.save(st)
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)

    def test_foreign_batch_base_refused(self):
        self._write_op()
        bid = batches.register_issue_batch(self.op_id)["id"]
        raw = batches.batch_file(bid).read_bytes()
        other = json.loads(raw.decode("utf-8"))
        other["repo"] = "other-repo"
        batches.batch_file(bid).write_bytes(
            json.dumps(other, ensure_ascii=False).encode("utf-8"))
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)

    def test_busy_op_lock_refuses_before_any_write(self):
        rec = self._write_op()
        manifest = m2.build_issue_batch_manifest(
            self.repo, self.run_id, self.lane, rec,
            batches._resolve_issue_candidate(self.repo, self.run_id),
            self.batch_id, {"steps": []}, repos.load(self.repo))
        bid = manifest["id"]
        held = threading.Event()
        release = threading.Event()

        def holder():
            with m2.op_locked(self.op_id):
                held.set()
                release.wait(timeout=60)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(timeout=30))
        try:
            with self.assertRaises(batches.BatchError):
                batches.import_batch(manifest)
            self.assertFalse(batches.batch_file(bid).exists())
        finally:
            release.set()
            thread.join(timeout=30)

    def test_direct_import_binds_typed_primary(self):
        rec = self._write_op()
        manifest = m2.build_issue_batch_manifest(
            self.repo, self.run_id, self.lane, rec,
            batches._resolve_issue_candidate(self.repo, self.run_id),
            self.batch_id, {"steps": []}, repos.load(self.repo))
        out = batches.import_batch(manifest)
        self.assertEqual(out["id"], manifest["id"])
        stored = batches.load_batch(manifest["id"])
        self.assertEqual(
            batches.primary_result_key(self.repo, stored["primaries"][0]),
            "issue:fakerepo:next:856")

    def test_direct_import_unknown_op_refused(self):
        rec = self._write_op()
        manifest = m2.build_issue_batch_manifest(
            self.repo, self.run_id, self.lane, rec,
            batches._resolve_issue_candidate(self.repo, self.run_id),
            self.batch_id, {"steps": []}, repos.load(self.repo))
        manifest["origin_m2"] = "prep-unknown-op"
        with self.assertRaises(batches.BatchError):
            batches.import_batch(manifest)

    def test_direct_import_profile_origin_change_refused(self):
        # A changed GitHub profile cannot rebind an already prepared
        # op: the direct-import path enforces the same canonical URL
        # gate as the register route, under the op lock.
        rec = self._write_op()
        manifest = m2.build_issue_batch_manifest(
            self.repo, self.run_id, self.lane, rec,
            batches._resolve_issue_candidate(self.repo, self.run_id),
            self.batch_id, {"steps": []}, repos.load(self.repo))
        repos.save({"name": self.repo, "github": "example/moved",
                    "baseBranch": "main"})
        with self.assertRaises(batches.BatchError):
            batches.import_batch(manifest)
        self.assertFalse(batches.batch_file(manifest["id"]).exists())

    def test_direct_import_stale_original_proof_refused(self):
        # Dirty original candidate bytes refuse direct import even
        # through a pending typed primary: the SAME fresh-proof gate
        # as register applies.
        rec = self._write_op()
        manifest = m2.build_issue_batch_manifest(
            self.repo, self.run_id, self.lane, rec,
            batches._resolve_issue_candidate(self.repo, self.run_id),
            self.batch_id, {"steps": []}, repos.load(self.repo))
        (self.run_work / "f.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(batches.BatchError):
            batches.import_batch(manifest)
        self.assertFalse(batches.batch_file(manifest["id"]).exists())

    def test_direct_import_primary_origin_mismatch_refused(self):
        # A manifest primary whose repo/URL differ from the bound op
        # record refuses: payloads cannot rebind the prepared origin.
        rec = self._write_op()
        ctx = batches._resolve_issue_candidate(self.repo, self.run_id)
        manifest = m2.build_issue_batch_manifest(
            self.repo, self.run_id, self.lane, rec, ctx,
            self.batch_id, {"steps": []}, repos.load(self.repo))
        manifest["primaries"][0]["issue"]["repo"] = "example/other"
        with self.assertRaises(batches.BatchError):
            batches.import_batch(manifest)
        self.assertFalse(batches.batch_file(manifest["id"]).exists())

    def test_import_holds_op_lock_through_commit(self):
        # The op exclusion spans validation AND the batch-file commit:
        # while the commit write itself is running, a competing
        # op_locked acquisition must still be excluded. A narrow
        # observation barrier on the exact batch-file write (no
        # binder/proof/evaluate stubbing) proves the lock is held
        # through the commit window, not just before validation.
        # Real threading, owned and joined; failures surface.
        rec = self._write_op()
        manifest = m2.build_issue_batch_manifest(
            self.repo, self.run_id, self.lane, rec,
            batches._resolve_issue_candidate(self.repo, self.run_id),
            self.batch_id, {"steps": []}, repos.load(self.repo))
        bid = manifest["id"]
        target = str(batches.batch_file(bid))
        in_commit = threading.Event()
        proceed = threading.Event()
        errors = []
        real_write_json = batches.write_json

        def gated_write_json(path, data):
            if str(path) == target:
                in_commit.set()
                if not proceed.wait(timeout=60):
                    raise AssertionError("commit barrier stuck")
            return real_write_json(path, data)

        def do_import():
            try:
                with mock.patch.object(batches, "write_json",
                                       gated_write_json):
                    out_box.append(batches.import_batch(manifest))
            except Exception as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        out_box = []
        thread = threading.Thread(target=do_import, daemon=True)
        thread.start()
        try:
            self.assertTrue(in_commit.wait(timeout=60),
                            "import never reached the batch commit")
            # The commit is in flight: the op must still be excluded.
            with self.assertRaises(m2.OpBusy):
                with m2.op_locked(self.op_id):
                    pass
        finally:
            proceed.set()
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(out_box), 1)
        self.assertEqual(out_box[0]["id"], bid)
        self.assertTrue(batches.batch_file(bid).exists())

    def test_route_requires_confirm(self):
        self._write_op()
        with mock.patch.object(serve, "_require_gate", lambda: None):
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_integration_register({"op": self.op_id})
        self.assertEqual(ctx.exception.status, 400)

    def test_route_unknown_op_404(self):
        with mock.patch.object(serve, "_require_gate", lambda: None):
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_integration_register(
                    {"op": "prep-no-such-op", "confirm": True})
        self.assertEqual(ctx.exception.status, 404)

    def test_route_busy_lease_409_without_mutation(self):
        self._write_op()
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
                    serve.api_integration_register(
                        {"op": self.op_id, "confirm": True})
            self.assertEqual(ctx.exception.status, 409)
        finally:
            released.set()
            thread.join(timeout=30)
        self.assertFalse(
            batches.batch_file(m2.batch_id_for_op(self.op_id)).exists())

    def test_route_positive_registers(self):
        self._write_op()
        with mock.patch.object(serve, "_require_gate", lambda: None):
            out = serve.api_integration_register(
                {"op": self.op_id, "confirm": True})
        self.assertEqual(out["id"], m2.batch_id_for_op(self.op_id))
        self.assertFalse(out["reused"])


class ReceiptTests(IssueFixtureMixin):
    """Configured fastcheck receipt contract negatives (all refuse,
    preserving batch bytes) plus the real-shaped consumer positive.

    Every inner-field negative rebinds the op fastchecks gate to the
    mutated receipt bytes, so the refusal proves the targeted check —
    not the stale-SHA gate. The stale-SHA case is covered separately
    by NOT rebinding.
    """

    def _mutate(self, receipt_fn=None, rebind=True, gate_fn=None):
        from openmainty import fastcheck
        rpath = fastcheck.op_fastcheck_path(self.op_id)
        doc = json.loads(rpath.read_bytes())
        if receipt_fn is not None:
            receipt_fn(doc)
        write_json(rpath, doc)
        opath = batches.op_file(self.op_id)
        rec = json.loads(opath.read_bytes())
        if gate_fn is not None:
            gate_fn(rec)
        elif rebind:
            rec["gates"][0]["receipt_sha256"] = \
                fastcheck._receipt_sha(rpath)
        write_json(opath, rec)

    def _register_fails(self):
        self.assertFalse(
            batches.batch_file(m2.batch_id_for_op(self.op_id)).exists())
        with self.assertRaises(batches.IntegrationError):
            batches.register_issue_batch(self.op_id)
        self.assertFalse(
            batches.batch_file(m2.batch_id_for_op(self.op_id)).exists())

    def test_receipt_missing_rc_refused(self):
        self._write_op()
        self._mutate(lambda d: d["steps"][0].pop("rc"))
        self._register_fails()

    def test_receipt_bool_rc_refused(self):
        # True fails rc != 0 anyway; False == 0 is the case proving
        # strict type rejection (bool is not int).
        for bad_rc in (True, False):
            with self.subTest(bad_rc=bad_rc):
                self._write_op()
                self._mutate(lambda d, bad_rc=bad_rc:
                             d["steps"][0].__setitem__("rc", bad_rc))
                self._register_fails()

    def test_receipt_timed_out_refused(self):
        self._write_op()
        self._mutate(lambda d: d["steps"][0].__setitem__(
            "timed_out", True))
        self._register_fails()
        self._write_op()
        self._mutate(lambda d: d["steps"][0].pop("timed_out"))
        self._register_fails()

    def test_receipt_wrong_log_path_refused(self):
        self._write_op()
        other = batches.integration_ops_dir() / "unrelated.log"
        other.write_bytes(b"unit ok\n")
        self._mutate(lambda d: d["steps"][0].__setitem__(
            "log", str(other)))
        self._register_fails()

    def test_receipt_unreadable_log_refused(self):
        self._write_op()
        (batches.integration_ops_dir() /
         (self.op_id + ".fastcheck.0.log")).unlink()
        self._mutate()
        self._register_fails()

    def test_receipt_log_sha_mismatch_refused(self):
        self._write_op()
        with open(batches.integration_ops_dir() /
                  (self.op_id + ".fastcheck.0.log"), "ab") as fh:
            fh.write(b"drift\n")
        self._mutate()
        self._register_fails()

    def test_receipt_not_worker_attested_refused(self):
        self._write_op()
        self._mutate(lambda d: d.__setitem__("worker_attested", False))
        self._register_fails()
        self._write_op()
        self._mutate(lambda d: d.__setitem__("attestor", "root"))
        self._register_fails()
        self._write_op()
        self._mutate(lambda d: d.__setitem__("root_attested", True))
        self._register_fails()

    def test_receipt_partial_steps_refused(self):
        self._write_op()
        self._mutate(lambda d: d.__setitem__("steps", []))
        self._register_fails()

    def test_receipt_profile_steps_changed_refused(self):
        self._write_op()
        repos.save({"name": self.repo, "github": self.slug,
                    "baseBranch": "main",
                    "fastcheck": {"steps": [{"argv": ["other"],
                                             "gate": "unit"}],
                                   "toolchain": []}})
        self._mutate()
        self._register_fails()

    def test_receipt_profile_without_fastcheck_refused(self):
        self._write_op()
        repos.save({"name": self.repo, "github": self.slug,
                    "baseBranch": "main"})
        self._mutate()
        self._register_fails()

    def test_op_gate_not_worker_refused(self):
        self._write_op()
        self._mutate(gate_fn=lambda r: r["gates"][0].__setitem__(
            "attested_by", "root"))
        self._register_fails()
        self._write_op()
        self._mutate(gate_fn=lambda r: r["gates"][0].__setitem__(
            "status", "fail"))
        self._register_fails()

    def test_gate_receipt_sha_stale_refused(self):
        # Mutated bytes WITHOUT rebinding: the mutation below trips
        # no step/profile check, so only the stale gate SHA refuses —
        # the gate must cite the current receipt bytes.
        self._write_op()
        self._mutate(lambda d: d.__setitem__("note", "stale probe"),
                     rebind=False)
        self._register_fails()

    def test_receipt_toolchain_changed_refused(self):
        self._write_op()
        self._mutate(lambda d: d.__setitem__("toolchain", ["x"]))
        self._register_fails()

    def test_receipt_sandbox_changed_refused(self):
        self._write_op()
        self._mutate(lambda d: d["steps"][0].__setitem__(
            "network_sandbox", "other"))
        self._register_fails()

    def test_consumer_positive_binds_op_log(self):
        # The actual producer (fastcheck.apply_consumer_result) folds
        # a consumer record and its raw run_step receipt — which has
        # NO head/fingerprint stamps — into the summary.
        from openmainty import fastcheck
        self._write_op()
        rpath = fastcheck.op_fastcheck_path(self.op_id)
        doc = json.loads(rpath.read_bytes())
        cons_log = (batches.integration_ops_dir() /
                    (self.op_id + ".fastcheck.consumer.log"))
        cons_log.write_bytes(b"consumer ok\n")
        cons_sha = hashlib.sha256(b"consumer ok\n").hexdigest()
        cons_argv = ["npm", "run", "build"]
        cons = {"status": "pass", "script": "build", "name": "web",
                "manager": "npm", "argv": cons_argv,
                "rc": 0, "timed_out": False, "elapsed_s": 1.0,
                "label": "consumer build pass",
                "log": str(cons_log),
                "receipt": {"argv": cons_argv, "gate": "consumer",
                            "name": "consumer (web)",
                            "rc": 0, "timed_out": False,
                            "log": str(cons_log),
                            "log_sha256": cons_sha}}
        summary = fastcheck.apply_consumer_result(doc, cons)
        self.assertNotIn("head", summary["steps"][-1])
        self.assertNotIn("fingerprint", summary["steps"][-1])
        write_json(rpath, summary)
        self._mutate()
        out = batches.register_issue_batch(self.op_id)
        self.assertFalse(out["reused"])

    def _consumer_negative(self, fn, expect_success=False):
        # Each case mutates the SAME raw consumer receipt dict and
        # returns it; the helper attaches that exact dict as
        # cons["receipt"] before the real producer folding, so the
        # negative reaches the consumer validation block instead of
        # being dropped. The extra-without-record case explicitly
        # returns cons None with its step pre-appended. An unmodified
        # pass control proves the helper path itself registers.
        from openmainty import fastcheck
        self._write_op()
        rpath = fastcheck.op_fastcheck_path(self.op_id)
        doc = json.loads(rpath.read_bytes())
        cons_log = (batches.integration_ops_dir() /
                    (self.op_id + ".fastcheck.consumer.log"))
        cons_log.write_bytes(b"consumer ok\n")
        cons_sha = hashlib.sha256(b"consumer ok\n").hexdigest()
        cons_argv = ["npm", "run", "build"]
        base_receipt = {"argv": cons_argv, "gate": "consumer",
                        "name": "consumer (web)",
                        "rc": 0, "timed_out": False,
                        "log": str(cons_log),
                        "log_sha256": cons_sha}
        base_cons = {"status": "pass", "script": "build",
                     "name": "web", "manager": "npm",
                     "argv": cons_argv, "rc": 0,
                     "timed_out": False, "elapsed_s": 1.0,
                     "label": "consumer build pass",
                     "log": str(cons_log)}
        doc, cons, receipt = fn(doc, dict(base_receipt),
                                dict(base_cons), str(cons_log),
                                cons_sha, cons_argv)
        if cons is not None:
            self.assertIsNotNone(
                receipt, "consumer case must supply its raw receipt")
            cons = dict(cons)
            cons["receipt"] = receipt
        summary = fastcheck.apply_consumer_result(doc, cons)
        write_json(rpath, summary)
        self._mutate()
        if expect_success:
            out = batches.register_issue_batch(self.op_id)
            self.assertFalse(out["reused"])
        else:
            self._register_fails()

    def test_consumer_helper_path_registers_unmodified(self):
        # Pass control for the helper above: an unmodified raw receipt
        # folded by the real producer registers successfully.
        def fn(doc, receipt, cons, log, sha, argv):
            return doc, cons, receipt
        self._consumer_negative(fn, expect_success=True)

    def test_consumer_extra_without_record_refused(self):
        def fn(doc, receipt, cons, log, sha, argv):
            doc["steps"].append(receipt)
            return doc, None, None
        self._consumer_negative(fn)

    def test_consumer_failed_status_refused(self):
        # The real producer flips ok False on a failed consumer, so a
        # passing summary carrying a failed consumer record can only
        # be hand-assembled: the validator's own consumer-status gate
        # must still refuse it (defense in depth, real field names).
        from openmainty import fastcheck
        self._write_op()
        rpath = fastcheck.op_fastcheck_path(self.op_id)
        doc = json.loads(rpath.read_bytes())
        cons_log = (batches.integration_ops_dir() /
                    (self.op_id + ".fastcheck.consumer.log"))
        cons_log.write_bytes(b"consumer ok\n")
        cons_sha = hashlib.sha256(b"consumer ok\n").hexdigest()
        cons_argv = ["npm", "run", "build"]
        doc["steps"].append({"argv": cons_argv, "gate": "consumer",
                             "name": "consumer (web)",
                             "rc": 0, "timed_out": False,
                             "log": str(cons_log),
                             "log_sha256": cons_sha})
        doc["consumer"] = {"status": "fail", "script": "build",
                           "name": "web", "manager": "npm",
                           "argv": cons_argv, "rc": 0,
                           "timed_out": False, "elapsed_s": 1.0,
                           "label": "consumer build pass",
                           "log": str(cons_log)}
        write_json(rpath, doc)
        self._mutate()
        self._register_fails()

    def test_consumer_wrong_log_path_refused(self):
        def fn(doc, receipt, cons, log, sha, argv):
            other = batches.integration_ops_dir() / "other-c.log"
            other.write_bytes(b"consumer ok\n")
            receipt["log"] = str(other)
            cons["log"] = str(other)
            return doc, cons, receipt
        self._consumer_negative(fn)

    def test_consumer_unreadable_log_refused(self):
        def fn(doc, receipt, cons, log, sha, argv):
            (batches.integration_ops_dir() /
             (self.op_id + ".fastcheck.consumer.log")).unlink()
            return doc, cons, receipt
        self._consumer_negative(fn)

    def test_consumer_sha_mismatch_refused(self):
        def fn(doc, receipt, cons, log, sha, argv):
            with open(log, "ab") as fh:
                fh.write(b"drift\n")
            return doc, cons, receipt
        self._consumer_negative(fn)

    def test_consumer_bool_rc_refused(self):
        # True fails rc != 0 anyway; False == 0 is the case proving
        # strict type rejection (bool is not int).
        for bad_rc in (True, False):
            with self.subTest(bad_rc=bad_rc):
                def fn(doc, receipt, cons, log, sha, argv,
                       bad_rc=bad_rc):
                    receipt["rc"] = bad_rc
                    cons["rc"] = bad_rc
                    return doc, cons, receipt
                self._consumer_negative(fn)

    def test_consumer_argv_mismatch_refused(self):
        def fn(doc, receipt, cons, log, sha, argv):
            cons["argv"] = ["npm", "run", "other"]
            return doc, cons, receipt
        self._consumer_negative(fn)


class PrepareIssueTests(IssueFixtureMixin):
    def test_prepare_creates_new_branch_leaving_original(self):
        # The positive prepare path requires the genuine READY base.
        self._assert_base_ready()
        rec = batches.prepare_integration(self.run_id, self.batch_id)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["repo"], self.repo)
        self.assertEqual(rec["run_id"], self.run_id)
        self.assertEqual(rec["issue"]["issue"], "856")
        digest = hashlib.sha256("|".join(
            (self.repo, self.lane, self.number, self.run_id,
             self.batch_id)).encode("utf-8")).hexdigest()[:12]
        self.assertEqual(rec["result"]["branch"],
                         f"codex/review-issue-856-{digest}")
        self.assertNotEqual(rec["result"]["branch"], self.branch)
        self.assertEqual(rec["candidate"]["branch"], self.branch)
        self.assertEqual(rec["candidate"]["head"], self.cand_head)
        # The original queue-owned branch still points at C.
        self.assertEqual(git("rev-parse", self.branch, cwd=self.origin),
                         self.cand_head)
        self.assertNotEqual(rec["result"]["head"], self.cand_head)

    def test_prepare_rejects_partial_dep_target(self):
        state = store.load(self.run_id)
        state["dep"] = "bar"
        store.save(state)
        with self.assertRaises(batches.IntegrationError):
            batches.prepare_integration(self.run_id, self.batch_id)
        self.assertFalse(list(
            batches.integration_ops_dir().glob("prep-*")))

    def test_prepare_rejects_unknown_queue_kind(self):
        st = sched.load(self.repo)
        st["queue"][0]["kind"] = "discussion"
        sched.save(st)
        with self.assertRaises(batches.IntegrationError):
            batches.prepare_integration(self.run_id, self.batch_id)
