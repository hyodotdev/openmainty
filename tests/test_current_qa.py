"""Current-integration QA projection served with /api/work.

Covers serve._current_qa_for_run and its wiring into api_work: a
registered batch bound to the run projects a fresh ready verdict with
catalog gate labels and integration file scope; drifted/dirty sources
go stale (never stay green); other-repo or other-run batches never
match; unreadable state degrades to none/unknown without breaking
/api/work. Report counts stay per-run dynamic, and bare authoritative
paths (locks, project files) link to the in-app diff viewer.
Run with: python3 -m unittest tests.test_current_qa
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, pcconfig, repos, runner, sched, serve, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

PROFILE = {
    "name": "fakerepo",
    "github": "example/fakerepo",
    "baseBranch": "main",
    "knowledge": [],
    "checks": [{"name": "unit", "cmd": ["make", "unit"], "requires": []}],
    "e2e": [],
    "release": {"notes": "CHANGELOG.md"},
    "announce": {"medium": False, "discord": False, "slack": False},
}


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _bind_accept(run_id, head, fp):
    run = store.load(run_id)
    for step in store.VERIFY_STEPS:
        run["steps"][step]["status"] = "done"
    run["workCommit"] = head
    run["workTreeFingerprint"] = fp
    run["verifiedCommits"] = {"review": head, "e2e": head}
    run["verifiedFingerprints"] = {"review": fp, "e2e": fp}
    store.save(run)


class CurrentQaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        checkout = Path(self.tmp.name) / "checkout"
        checkout.mkdir()
        (checkout / "README.md").write_text("# fake\n")
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["fakerepo"] = str(checkout)
        pcconfig.save(pc)

    def _seed(self, batch_id="qa-batch-1"):
        root = Path(self.tmp.name)
        origin = root / "origin"
        origin.mkdir()
        _git(origin, "init", "-b", "main")
        (origin / "f.txt").write_text("v1\n")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "base")
        base_head = subprocess.run(
            ["git", "rev-parse", "main"], cwd=str(origin), check=True,
            text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL).stdout.strip()
        _git(origin, "checkout", "-b", "codex/next/demo-qa")
        (origin / "f.txt").write_text("v2\n")
        _git(origin, "commit", "-am", "cand")
        cand = runner.source_snapshot(origin)
        integ = root / "integ"
        _git(origin, "worktree", "add", "--detach", str(integ),
             cand["head"])
        integ_snap = runner.source_snapshot(integ)
        self.assertEqual(integ_snap["head"], cand["head"])
        proof = root / "proof.json"
        proof.write_bytes(b'{"ok": true}')
        run = store.create("fakerepo", "nitrogen@0.37.1", "t", None,
                           {"by": "t"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k", "repo": "fakerepo", "dep": "nitrogen",
             "target": "0.37.1", "lane": "next", "status": "waiting-review",
             "run_id": run["id"], "branch": "codex/next/demo-qa"})
        sched.save(st)
        _bind_accept(run["id"], cand["head"], cand["fingerprint"])
        batches.import_batch({
            "id": batch_id, "repo": "fakerepo",
            "target_branch": "next", "purpose": "Nitrogen 통합 검증",
            "origin": {"checkout": str(origin), "base_branch": "main",
                       "base_head": base_head},
            "primaries": [{
                "dep": "nitrogen", "target": "0.37.1", "lane": "next",
                "run_id": run["id"],
                "source": {"worktree": str(origin),
                           "branch": "codex/next/demo-qa",
                           "head": cand["head"],
                           "fingerprint": cand["fingerprint"]},
                "review": {"verdict": "accept",
                           "findings": ["라벨 확인용 finding"]}}],
            "integrations": [{
                "lane": "next", "worktree": str(integ),
                "status": "premerge-verified",
                "gates": [{
                    "key": "unit", "status": "pass",
                    "note": "6묶음 통과.", "root_attested": True,
                    "inputs": {"head": integ_snap["head"],
                               "fingerprint": integ_snap["fingerprint"]},
                    "evidence": [{"label": "회귀 로그", "kind": "file",
                                  "origin": str(proof)}]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {"label": "단위 검사"}}},
        })
        return run["id"], origin, integ

    def test_ready_projection_with_labels_and_file_scope(self):
        run_id, _origin, _integ = self._seed()
        body = serve.api_work({"run": run_id})
        qa = body["current_qa"]
        self.assertEqual(qa["state"], "ready")
        self.assertTrue(qa["ready"])
        self.assertEqual(qa["batch"], "qa-batch-1")
        self.assertEqual(qa["purpose"], "Nitrogen 통합 검증")
        self.assertEqual(qa["gates_passed"], 1)
        self.assertEqual(qa["gates_total"], 1)
        self.assertEqual(qa["gates"][0]["label"], "단위 검사")
        self.assertEqual(qa["gates"][0]["key"], "unit")
        self.assertIn("라벨 확인용 finding", qa["review_findings"])
        # Integration scope is the prepared worktree base..head range.
        self.assertIn("f.txt", qa["files"])
        self.assertEqual(qa["files_error"], "")
        self.assertTrue(qa["source"]["clean"])

    def test_no_batch_is_none_and_work_survives(self):
        run = store.create("fakerepo", "dep@1", "t", None, {"by": "t"})
        body = serve.api_work({"run": run["id"]})
        self.assertEqual(body["current_qa"]["state"], "none")

    def test_other_run_batch_never_matches(self):
        run_id, _origin, _integ = self._seed()
        other = store.create("fakerepo", "dep@2", "t", None, {"by": "t"})
        body = serve.api_work({"run": other["id"]})
        self.assertEqual(body["current_qa"]["state"], "none")
        _ = run_id

    def test_drifted_integration_source_goes_stale(self):
        run_id, _origin, integ = self._seed()
        (integ / "f.txt").write_text("v3-drift\n")
        _git(integ, "commit", "-am", "drift")
        qa = serve.api_work({"run": run_id})["current_qa"]
        self.assertEqual(qa["state"], "stale")
        self.assertFalse(qa["ready"])
        self.assertTrue(qa["reasons"])

    def test_dirty_integration_worktree_goes_stale(self):
        run_id, _origin, integ = self._seed()
        (integ / "f.txt").write_text("v3-dirty\n")
        qa = serve.api_work({"run": run_id})["current_qa"]
        self.assertEqual(qa["state"], "stale")
        self.assertFalse(qa["ready"])

    def test_removed_integration_worktree_is_stale_not_green(self):
        import shutil
        run_id, _origin, integ = self._seed()
        shutil.rmtree(integ)
        body = serve.api_work({"run": run_id})
        qa = body["current_qa"]
        self.assertEqual(qa["state"], "stale")
        self.assertFalse(qa["ready"])
        self.assertTrue(qa["reasons"])
        # File scope degrades explicitly instead of an empty pass.
        self.assertEqual(qa["files"], [])
        self.assertTrue(qa["files_error"])

    def test_report_counts_are_per_run_dynamic(self):
        run_id, _origin, _integ = self._seed()
        run = store.load(run_id)
        run["steps"]["e2e"]["note"] = "3 check(s) passed"
        store.save(run)
        rep = serve._work_report(store.load(run_id), run_id)
        self.assertIsInstance(rep["fix_files"], int)
        self.assertEqual(rep["scope_note"], "3 check(s) passed")

    def _seed_pair(self):
        """Two-batch chain: A (stable base) + B inheriting A.

        Mirrors the real N/A topology on a small scale: B's readiness
        depends on A's live proof, so drift in A must surface in both
        the work-view projection and the batch detail evaluation.
        """
        root = Path(self.tmp.name)

        def _repo(name, branch):
            wt = root / name
            wt.mkdir()
            _git(wt, "init", "-b", "main")
            (wt / "f.txt").write_text("v1\n")
            _git(wt, "add", ".")
            _git(wt, "commit", "-m", "base")
            base = subprocess.run(
                ["git", "rev-parse", "main"], cwd=str(wt), check=True,
                text=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL).stdout.strip()
            _git(wt, "checkout", "-b", branch)
            (wt / "f.txt").write_text("v2\n")
            _git(wt, "commit", "-am", "cand")
            cand = runner.source_snapshot(wt)
            integ = root / (name + "-integ")
            _git(wt, "worktree", "add", "--detach", str(integ),
                 cand["head"])
            integ_snap = runner.source_snapshot(integ)
            return wt, base, cand, integ, integ_snap

        proof = root / "pair-proof.json"
        proof.write_bytes(b'{"ok": true}')

        def _run(dep, target, branch):
            run = store.create("fakerepo", f"{dep}@{target}", "t",
                               None, {"by": "t"})
            st = sched.load("fakerepo")
            st.setdefault("queue", []).append(
                {"key": f"k-{dep}", "repo": "fakerepo", "dep": dep,
                 "target": target, "lane": "next",
                 "status": "waiting-review", "run_id": run["id"],
                 "branch": branch})
            sched.save(st)
            return run["id"]

        o_a, base_a, cand_a, integ_a, snap_a = _repo(
            "pair-a", "codex/next/pair-a")
        run_a = _run("stable", "1.0", "codex/next/pair-a")
        _bind_accept(run_a, cand_a["head"], cand_a["fingerprint"])
        batches.import_batch({
            "id": "pair-a", "repo": "fakerepo",
            "target_branch": "next", "purpose": "기반 묶음",
            "origin": {"checkout": str(o_a), "base_branch": "main",
                       "base_head": base_a},
            "primaries": [{
                "dep": "stable", "target": "1.0", "lane": "next",
                "run_id": run_a,
                "source": {"worktree": str(o_a),
                           "branch": "codex/next/pair-a",
                           "head": cand_a["head"],
                           "fingerprint": cand_a["fingerprint"]},
                "review": {"verdict": "accept"}}],
            "integrations": [{
                "lane": "next", "worktree": str(integ_a),
                "status": "premerge-verified",
                "gates": [{
                    "key": "unit", "status": "pass", "note": "통과.",
                    "inputs": {"head": snap_a["head"],
                               "fingerprint": snap_a["fingerprint"]},
                    "evidence": [{"label": "로그", "kind": "file",
                                  "origin": str(proof)}]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {"label": "단위 검사"}}},
        })
        o_b, base_b, cand_b, integ_b, snap_b = _repo(
            "pair-b", "codex/next/pair-b")
        run_b = _run("next", "2.0", "codex/next/pair-b")
        _bind_accept(run_b, cand_b["head"], cand_b["fingerprint"])
        batches.import_batch({
            "id": "pair-b", "repo": "fakerepo",
            "target_branch": "next", "purpose": "상속 통합 검증",
            "origin": {"checkout": str(o_b), "base_branch": "main",
                       "base_head": base_b},
            "primaries": [{
                "dep": "next", "target": "2.0", "lane": "next",
                "run_id": run_b,
                "source": {"worktree": str(o_b),
                           "branch": "codex/next/pair-b",
                           "head": cand_b["head"],
                           "fingerprint": cand_b["fingerprint"]},
                "review": {"verdict": "accept"}}],
            "integrations": [{
                "lane": "next", "worktree": str(integ_b),
                "status": "premerge-verified",
                "gates": [{
                    "key": "unit", "status": "pass", "note": "통과.",
                    "inputs": {"head": snap_b["head"],
                               "fingerprint": snap_b["fingerprint"]},
                    "evidence": [{"label": "로그", "kind": "file",
                                  "origin": str(proof)}]}]}],
            "inherited": [{"group": "기반 1건 상속", "batch": "pair-a",
                           "count": 1}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {"label": "단위 검사"}}},
        })
        return run_b, integ_a, integ_b

    def test_inherited_parity_both_fresh_ready(self):
        run_b, _integ_a, _integ_b = self._seed_pair()
        qa = serve.api_work({"run": run_b})["current_qa"]
        self.assertEqual(qa["state"], "ready")
        self.assertTrue(qa["ready"])
        det = serve.api_batch({"id": "pair-b"})
        results = (det.get("evaluation") or {}).get("results") or []
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ready"])
        self.assertEqual(results[0].get("inherited"),
                         [{"batch": "pair-a", "ready": 1, "total": 1,
                           "stale": False}])

    def test_inherited_parity_drift_in_base_stales_both(self):
        run_b, integ_a, _integ_b = self._seed_pair()
        (integ_a / "f.txt").write_text("v3-drift\n")
        _git(integ_a, "commit", "-am", "drift")
        qa = serve.api_work({"run": run_b})["current_qa"]
        self.assertEqual(qa["state"], "stale")
        self.assertFalse(qa["ready"])
        self.assertTrue(any("pair-a" in r for r in qa["reasons"]))
        det = serve.api_batch({"id": "pair-b"})
        results = (det.get("evaluation") or {}).get("results") or []
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ready"])
        self.assertTrue(any("pair-a" in r
                            for r in results[0].get("reasons") or []))

    def test_inherited_leaf_surfaces_in_work_projection(self):
        run_b, integ_a, _integ_b = self._seed_pair()
        qa0 = serve.api_work({"run": run_b})["current_qa"]
        self.assertEqual(qa0["state"], "ready")
        self.assertEqual(qa0.get("leaf") or "", "")
        (integ_a / "f.txt").write_text("v3-drift\n")
        _git(integ_a, "commit", "-am", "drift")
        qa = serve.api_work({"run": run_b})["current_qa"]
        self.assertEqual(qa["state"], "stale")
        self.assertFalse(qa["ready"])
        self.assertIn("pair-a", qa.get("leaf") or "")
        self.assertIn("STALE", qa.get("leaf") or "")

    def test_bare_lock_and_pbxproj_paths_link_to_viewer(self):
        out = serve._rewrite_report_links(
            "synced yarn.lock and example/ios/project.pbxproj, "
            "see [package.json](workfile:package.json) and /etc/x",
            "/wt", ["yarn.lock", "example/ios/project.pbxproj",
                    "package.json"])
        self.assertIn("[yarn.lock](workfile:yarn.lock)", out)
        self.assertIn("[example/ios/project.pbxproj]"
                      "(workfile:example/ios/project.pbxproj)", out)
        # Already-linked targets are not double-wrapped.
        self.assertEqual(out.count("workfile:package.json"), 1)
        self.assertNotIn("workfile:/etc/x", out)
        # Unknown paths stay plain text.
        other = serve._rewrite_report_links("touched other.txt today",
                                            "/wt", ["yarn.lock"])
        self.assertNotIn("workfile:", other)


if __name__ == "__main__":
    unittest.main()
