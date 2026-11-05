"""Durable coverage: review batches import, validation, readiness, leaf causes.

Exercises the maintained batches module (the trusted-local CLI handoff
for real gate evidence): stored readiness never comes from cached
fingerprints, invalid input is rejected without touching prior batches,
run/queue identity is real metadata, and scheduler queue/run history
stays untouched. Run with: python3 -m unittest discover -s tests
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, repos, runner, sched, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def _profile(name):
    return {"name": name, "github": "example/" + name, "baseBranch": "main",
            "knowledge": [], "checks": [], "e2e": [], "release": {}}


def _git(origin, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    *args], cwd=str(origin), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _origin_with_branch(tmp, branch="codex/next/demo-1"):
    origin = Path(tmp.name) / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "f.txt").write_text("v1\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "base")
    _git(origin, "checkout", "-b", branch)
    (origin / "f.txt").write_text("v2\n")
    _git(origin, "commit", "-am", "candidate")
    base_head = subprocess.run(["git", "rev-parse", "main"], cwd=str(origin),
                               check=True, text=True,
                               stdout=subprocess.PIPE).stdout.strip()
    return origin, branch, base_head


def _payload(origin_path, origin_file, branch, **kw):
    d = {"id": "t-batch-1", "repo": "fakerepo", "target_branch": "next",
         "purpose": "stable",
         "origin": {"checkout": str(origin_path), "base_branch": "main"},
         "primaries": [{"dep": "jest", "target": "30.5.2", "lane": "next",
                        "run_id": "run-for-jest",
                        "source": {"worktree": str(origin_path),
                                   "branch": branch},
                        "review": {"verdict": "accept", "at": "t",
                                   "findings": []},
                        "required_gates": ["unit", "native-runtime"]}],
         "inherited": [],
         "integrations": [{"lane": "next",
                           "worktree": str(origin_path),
                           "status": "testing",
                           "gates": [
                               {"key": "unit", "status": "pass",
                                "inputs": {},
                                "evidence": [
                                    {"label": "unit log", "kind": "file",
                                     "origin": origin_file}]},
                               {"key": "native-runtime", "status": "pass",
                                "root_attested": True, "inputs": {},
                                "evidence": [
                                    {"label": "junit", "kind": "file",
                                     "origin": origin_file}]},
                           ]}],
         "policy": {"required_gates": ["unit", "native-runtime"],
                    "gates": {"unit": {},
                              "native-runtime": {"root": True,
                                                 "runtime": True}}}}
    d.update(kw)
    return d


def _bind_accept(run_id, head, fp):
    """Mirror real review runs: reproduce done pre-fingerprint (legacy),
    review+e2e fresh bound to the subject."""
    run = store.load(run_id)
    for step in store.VERIFY_STEPS:
        run["steps"][step]["status"] = "done"
    run["workCommit"] = head
    run["workTreeFingerprint"] = fp
    run["verifiedCommits"] = {"review": head, "e2e": head}
    run["verifiedFingerprints"] = {"review": fp, "e2e": fp}
    store.save(run)


class BatchFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        repos.save(_profile("fakerepo"))
        repos.save(_profile("otherrepo"))
        self.origin, self.branch, self.base_head = _origin_with_branch(
            self.tmp)
        self.proof = Path(self.tmp.name) / "proof.json"
        self.proof.write_bytes(b'{"ok": true}')
        self.run = store.create("fakerepo", "jest@30.5.2", "t", None,
                                {"by": "test"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "fakerepo+jest+30.5.2+next", "repo": "fakerepo",
             "dep": "jest", "target": "30.5.2", "lane": "next",
             "status": "waiting-review", "run_id": self.run["id"],
             "branch": self.branch})
        sched.save(st)
        self.run_id = self.run["id"]

    def _payload(self, **kw):
        d = _payload(str(self.origin), str(self.proof), self.branch, **kw)
        d["primaries"][0]["run_id"] = self.run_id
        snap = runner.source_snapshot(self.origin)
        # Reviewed subject + base identities travel with the verdict.
        d["primaries"][0]["source"]["head"] = snap["head"]
        d["primaries"][0]["source"]["fingerprint"] = snap["fingerprint"]
        d["origin"]["base_head"] = self.base_head
        _bind_accept(self.run_id, snap["head"], snap["fingerprint"])
        d["integrations"][0]["head"] = snap["head"]
        d["integrations"][0]["fingerprint"] = snap["fingerprint"]
        for g in d["integrations"][0]["gates"]:
            if g["status"] == "pass":
                g["inputs"] = {"head": snap["head"],
                               "fingerprint": snap["fingerprint"]}
        return d

    def _import_ok(self, **kw):
        return batches.import_batch(self._payload(**kw))

    def _live_ok(self, batch):
        return batches.live_map([batch])

    def _move_branch(self, branch):
        subprocess.run(["git", "checkout", branch], cwd=str(self.origin),
                       check=True, capture_output=True)
        (self.origin / "f.txt").write_text("moved\n")
        _git(self.origin, "add", "-A")
        _git(self.origin, "commit", "-m", "move " + branch)
        _git(self.origin, "checkout", self.branch)


class ImportTest(BatchFixture):
    def test_import_stores_content_addressed_evidence(self):
        b = self._import_ok()
        ev = b["integrations"][0]["gates"][0]["evidence"][0]
        want = hashlib.sha256(b'{"ok": true}').hexdigest()
        self.assertEqual(ev["sha256"], want)
        self.assertIn(want, ev["stored_path"])
        self.assertEqual(b["origin"]["base_head"], self.base_head)
        self.assertEqual(b["primaries"][0]["source"]["branch"], self.branch)

    def test_ready_when_everything_proven_live(self):
        b = self._import_ok()
        ev = batches.evaluate(b, self._live_ok(b))
        self.assertTrue(ev["results"][0]["ready"], ev["results"][0]["reasons"])

    def test_probe_case_rejected_not_ready(self):
        """Root probe: nonexistent run + empty evidence + no policy."""
        bad = {"id": "probe-bad", "repo": "fakerepo",
               "primaries": [{"dep": "x", "target": "1", "lane": "next",
                              "run_id": "run-nope",
                              "review": {"verdict": "accept"}}],
               "integrations": [{"lane": "next", "worktree": "/tmp",
                                 "gates": [{"key": "unit", "status": "pass",
                                            "evidence": []}]}]}
        with self.assertRaises(batches.BatchError):
            batches.import_batch(bad)
        with self.assertRaises(batches.BatchError):
            batches.load_batch("probe-bad")
        self.assertFalse(batches.batch_file("probe-bad").exists())

    def test_stale_accept_reimport_rejected_never_rebound(self):
        """Dirty original bytes without a HEAD move: reimporting the old
        ACCEPT receipt must be rejected, not rebound to the new bytes."""
        b = self._import_ok()
        before_meta = batches.batch_file(b["id"]).read_bytes()
        before_history = len(b["history"])
        old = self._payload()
        (self.origin / "f.txt").write_text("dirty-bytes\n")
        with self.assertRaises(batches.BatchError):
            batches.import_batch(old)
        # Prior batch bytes/history survive the rejected reimport.
        self.assertEqual(batches.batch_file(b["id"]).read_bytes(),
                         before_meta)
        self.assertEqual(len(batches.load_batch(b["id"])["history"]),
                         before_history)

    def test_moved_base_reimport_rejected(self):
        """Moved base + unchanged old receipt: the reimport must fail
        instead of adopting the new base head."""
        b = self._import_ok()
        before_meta = batches.batch_file(b["id"]).read_bytes()
        before_history = len(b["history"])
        old = self._payload()
        self._move_branch("main")
        with self.assertRaises(batches.BatchError):
            batches.import_batch(old)
        self.assertEqual(batches.batch_file(b["id"]).read_bytes(),
                         before_meta)
        self.assertEqual(len(batches.load_batch(b["id"])["history"]),
                         before_history)

    def test_wrong_worktree_rejected(self):
        """A foreign checkout is rejected even when the payload branch
        string claims the owned branch — actual branch and same-repo
        bindings are checked, not the claim."""
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        _git(other, "init", "-b", "main")
        (other / "x.txt").write_text("x\n")
        _git(other, "add", ".")
        _git(other, "commit", "-m", "other")
        d = self._payload()
        d["primaries"][0]["source"]["worktree"] = str(other)
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        _git(other, "checkout", "-b", self.branch)
        (other / "x.txt").write_text("y\n")
        _git(other, "commit", "-am", "other-cand")
        d = self._payload()
        d["primaries"][0]["source"]["worktree"] = str(other)
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)

    def test_accept_proof_scoped_to_review_steps(self):
        """Legacy reproduce (done pre-fingerprint) never greens an accept
        by itself, but does not block one proven by fresh review+e2e;
        a stale review step still rejects the accept."""
        b = self._import_ok()
        self.assertEqual(b["primaries"][0]["run_binding"]["verification"]
                         ["reproduce"], "legacy")
        d = self._payload(id="t-batch-stale")
        run = store.load(self.run_id)
        run["verifiedCommits"]["review"] = "dead" * 10
        store.save(run)
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)

    def test_rejects_traversal_and_bad_ids(self):
        for bad in ("../x", "a/b", "", "UPPER", "x" * 70):
            with self.assertRaises(batches.BatchError):
                batches.import_batch(self._payload(id=bad))
        with self.assertRaises(batches.BatchError):
            batches.load_batch("../x")

    def test_rejects_unknown_repo_run_and_mismatch(self):
        with self.assertRaises(batches.BatchError):
            batches.import_batch(self._payload(repo="nosuchrepo"))
        d = self._payload()
        d["primaries"][0]["run_id"] = "run-missing"
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        d = self._payload()
        d["primaries"][0]["target"] = "9.9.9"
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        other = store.create("otherrepo", "jest@30.5.2", "t", None,
                             {"by": "test"})
        d = self._payload()
        d["primaries"][0]["run_id"] = other["id"]
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)

    def test_rejects_arbitrary_branch_and_missing_origin(self):
        d = self._payload()
        d["primaries"][0]["source"]["branch"] = "codex/next/elsewhere"
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        d = self._payload()
        d["origin"] = {}
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        d = self._payload()
        d["origin"] = {"checkout": str(self.origin), "base_branch": "nope"}
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)

    def test_rejects_duplicates_unknown_gates_lanes(self):
        d = self._payload()
        d["primaries"].append(dict(d["primaries"][0]))
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        d = self._payload()
        d["primaries"][0]["required_gates"] = ["nope-gate"]
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        d = self._payload()
        d["policy"] = {}
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        d = self._payload()
        d["integrations"].append(dict(d["integrations"][0]))
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        d = self._payload()
        d["integrations"][0]["gates"].append(
            dict(d["integrations"][0]["gates"][0]))
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        d = self._payload()
        for g in d["integrations"][0]["gates"]:
            g["inputs"] = {}
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)

    def test_cross_batch_duplicate_rejected(self):
        self._import_ok()
        with self.assertRaises(batches.BatchError):
            batches.import_batch(self._payload(id="t-batch-2"))

    def test_policy_union_keeps_requirements(self):
        d = self._payload()
        d["primaries"][0]["required_gates"] = ["unit"]
        b = batches.import_batch(d)
        self.assertIn("native-runtime",
                      b["primaries"][0]["required_gates"])

    def test_failed_reimport_preserves_prior_bytes_and_history(self):
        b = self._import_ok()
        ev = b["integrations"][0]["gates"][0]["evidence"][0]
        stored = batches.evidence_dir("t-batch-1") / \
            Path(ev["stored_path"]).name
        before_bytes = stored.read_bytes()
        before_meta = batches.batch_file("t-batch-1").read_bytes()
        before_history = len(b["history"])
        d = self._payload()
        d["integrations"][0]["gates"][0]["key"] = "unknown-gate"
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        self.assertEqual(stored.read_bytes(), before_bytes)
        self.assertEqual(batches.batch_file("t-batch-1").read_bytes(),
                         before_meta)
        self.assertEqual(len(batches.load_batch("t-batch-1")["history"]),
                         before_history)

    def test_evidence_scope_enforced_when_scoped(self):
        outside = Path(self.tmp.name).parent / "outside.json"
        outside.write_bytes(b"{}")
        d = self._payload()
        d["integrations"][0]["gates"][0]["evidence"][0]["origin"] = \
            str(outside)
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d, allowed_roots=[str(self.origin)])
        link = Path(self.tmp.name) / "link.json"
        try:
            link.symlink_to(outside)
        except OSError:
            return
        d2 = self._payload()
        d2["integrations"][0]["gates"][0]["evidence"][0]["origin"] = str(link)
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d2, allowed_roots=[str(self.origin)])

    def test_no_http_import_surface(self):
        self.assertFalse(hasattr(batches, "default_api_roots"))

    def test_import_leaves_queue_runs_history_untouched(self):
        qfile = Path(os.environ["OPENMAINTY_HOME"]) / "sched" / \
            "fakerepo.json"
        before_q = qfile.read_bytes()
        runs_dir = Path(os.environ["OPENMAINTY_HOME"]) / "runs"
        before_runs = sorted(p.name for p in runs_dir.rglob("*"))
        self._import_ok()
        self.assertEqual(qfile.read_bytes(), before_q)
        self.assertEqual(sorted(p.name for p in runs_dir.rglob("*")),
                         before_runs)


class FreshnessTest(BatchFixture):
    def _mutate_live(self, batch, key, **kw):
        live = batches.live_map([batch])
        live[key] = dict(live[key], **kw)
        return live

    def test_moved_sources_and_bases_block(self):
        b = self._import_ok()
        lane = b["integrations"][0]["lane"]
        wt = b["integrations"][0]["worktree"]
        live = self._mutate_live(b, "wt:" + wt, head="dead" * 10)
        ev = batches.evaluate(b, live)
        self.assertFalse(ev["results"][0]["ready"])
        live = self._mutate_live(b, "wt:" + wt, fingerprint="0" * 64)
        ev = batches.evaluate(b, live)
        self.assertFalse(ev["results"][0]["ready"])
        live = self._mutate_live(b, "wt:" + wt, clean=False)
        ev = batches.evaluate(b, live)
        self.assertFalse(ev["results"][0]["ready"])
        src_wt = b["primaries"][0]["source"]["worktree"]
        live = self._mutate_live(b, "cand:" + src_wt, head="dead" * 10)
        ev = batches.evaluate(b, live)
        self.assertFalse(ev["results"][0]["ready"])
        # Real move of the recorded base branch blocks readiness.
        self._move_branch("main")
        ev = batches.evaluate(b, batches.live_map([b]))
        self.assertFalse(ev["results"][0]["ready"])
        self.assertTrue(any("base" in r.lower()
                            for r in ev["results"][0]["reasons"]))

    def test_diverging_bases_same_checkout(self):
        """Same checkout, main vs release bases from real divergent refs:
        moving only release blocks the next batch while main stays ready."""
        _git(self.origin, "checkout", "-b", "release", "main")
        _git(self.origin, "checkout", self.branch)
        a = self._import_ok(id="t-main")
        run2 = store.create("fakerepo", "eslint@10.0.0", "t", None,
                            {"by": "test"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k-eslint-next", "repo": "fakerepo", "dep": "eslint",
             "target": "10.0.0", "lane": "next", "status": "waiting-review",
             "run_id": run2["id"], "branch": self.branch})
        sched.save(st)
        d = self._payload(id="t-next")
        d["origin"]["base_branch"] = "release"
        d["origin"]["base_head"] = subprocess.run(
            ["git", "rev-parse", "release"], cwd=str(self.origin),
            check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        d["primaries"][0]["dep"] = "eslint"
        d["primaries"][0]["target"] = "10.0.0"
        d["primaries"][0]["run_id"] = run2["id"]
        snap2 = runner.source_snapshot(self.origin)
        _bind_accept(run2["id"], snap2["head"], snap2["fingerprint"])
        n = batches.import_batch(d)
        ev = batches.evaluate(a, batches.live_map([a]))
        self.assertTrue(ev["results"][0]["ready"], ev["results"][0])
        ev = batches.evaluate(n, batches.live_map([n]))
        self.assertTrue(ev["results"][0]["ready"], ev["results"][0])
        self._move_branch("release")
        ev = batches.evaluate(a, batches.live_map([a]))
        self.assertTrue(ev["results"][0]["ready"], ev["results"][0])
        ev = batches.evaluate(n, batches.live_map([n]))
        self.assertFalse(ev["results"][0]["ready"])
        self.assertTrue(any("base" in r.lower()
                            for r in ev["results"][0]["reasons"]),
                        ev["results"][0]["reasons"])

    def test_cache_only_dirt_stays_ready(self):
        """Reserved caches (tool writes) must not block readiness."""
        b = self._import_ok()
        cache = self.origin / ".openmainty-cache" / "node_modules"
        cache.mkdir(parents=True)
        (cache / "x.tgz").write_text("junk\n")
        nm = self.origin / "node_modules"
        nm.mkdir(exist_ok=True)
        (nm / "y.js").write_text("junk\n")
        ev = batches.evaluate(b, batches.live_map([b]))
        self.assertTrue(ev["results"][0]["ready"],
                        ev["results"][0]["reasons"])

    def test_relevant_untracked_source_blocks(self):
        b = self._import_ok()
        (self.origin / "newsource.ts").write_text("export const x = 1;\n")
        ev = batches.evaluate(b, batches.live_map([b]))
        self.assertFalse(ev["results"][0]["ready"])
        self.assertTrue(any("uncommitted" in r
                            for r in ev["results"][0]["reasons"]))

    def test_ignored_file_stays_ready(self):
        (self.origin / ".gitignore").write_text("local-only.log\n")
        _git(self.origin, "add", ".gitignore")
        _git(self.origin, "commit", "-m", "ignore")
        b = self._import_ok()
        (self.origin / "local-only.log").write_text("junk\n")
        ev = batches.evaluate(b, batches.live_map([b]))
        self.assertTrue(ev["results"][0]["ready"],
                        ev["results"][0]["reasons"])

    def test_dirty_original_without_head_move_blocks(self):
        b = self._import_ok()
        (self.origin / "f.txt").write_text("dirty\n")
        ev = batches.evaluate(b, batches.live_map([b]))
        self.assertFalse(ev["results"][0]["ready"])
        self.assertTrue(any("uncommitted" in r
                            for r in ev["results"][0]["reasons"]),
                        ev["results"][0]["reasons"])

    def test_stale_gate_inputs_block(self):
        b = self._import_ok()
        # Reimport bumps integration source but keeps the old gate proof:
        # the gate must read STALE instead of borrowing the old pass.
        (self.origin / "f.txt").write_text("v3\n")
        _git(self.origin, "commit", "-am", "bump")
        d = self._payload()
        ev = batches.evaluate(b, batches.live_map([b]))
        self.assertFalse(ev["results"][0]["ready"])
        self.assertTrue(any("STALE" in r for r in
                            ev["results"][0]["reasons"]))

    def test_deleted_run_blocks(self):
        b = self._import_ok()
        (Path(os.environ["OPENMAINTY_HOME"]) / "runs" / self.run_id /
         "run.json").unlink()
        ev = batches.evaluate(b, batches.live_map([b]))
        self.assertFalse(ev["results"][0]["ready"])

    def test_note_only_and_unattested_runtime_block(self):
        b = self._import_ok()
        b["integrations"][0]["gates"][0]["evidence"] = [
            {"label": "words", "kind": "note", "text": "all good"}]
        ev = batches.evaluate(b, batches.live_map([b]))
        self.assertFalse(ev["results"][0]["ready"])
        self.assertTrue(any("notes only" in r for r in
                            ev["results"][0]["reasons"]))
        b["integrations"][0]["gates"][1]["root_attested"] = False
        ev = batches.evaluate(b, batches.live_map([b]))
        self.assertFalse(ev["results"][0]["ready"])


class ChainFixtureMixin:
    """Shared two-batch-chain builders (helper methods only, no cases).

    Mixed into InheritedTest and LeafCauseTest so both reuse the same
    distinct-origin/second-batch setup without re-running anyone's
    cases under a second class.
    """

    def _second(self, dep="eslint", target="10.0.0", id="t-batch-base",
                base_branch=None):
        run = store.create("fakerepo", dep + "@" + target, "t", None,
                           {"by": "test"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k-" + dep, "repo": "fakerepo", "dep": dep,
             "target": target, "lane": "next", "status": "waiting-review",
             "run_id": run["id"], "branch": self.branch})
        sched.save(st)
        d = self._payload(id=id)
        if base_branch:
            d["origin"]["base_branch"] = base_branch
        d["primaries"][0]["dep"] = dep
        d["primaries"][0]["target"] = target
        d["primaries"][0]["run_id"] = run["id"]
        snap = runner.source_snapshot(self.origin)
        _bind_accept(run["id"], snap["head"], snap["fingerprint"])
        return batches.import_batch(d)

    def _distinct_base(self, dep="eslint", target="10.0.0",
                       branch="codex/next/demo-2"):
        """Owned batch on a genuinely separate checkout: its own repo,
        candidate branch, worktree snapshots and run/queue identity."""
        origin_b = Path(self.tmp.name) / "origin-b"
        origin_b.mkdir()
        _git(origin_b, "init", "-b", "main")
        (origin_b / "g.txt").write_text("b1\n")
        _git(origin_b, "add", ".")
        _git(origin_b, "commit", "-m", "base-b")
        _git(origin_b, "checkout", "-b", branch)
        (origin_b / "g.txt").write_text("b2\n")
        _git(origin_b, "commit", "-am", "cand-b")
        run = store.create("fakerepo", dep + "@" + target, "t", None,
                           {"by": "test"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k-" + dep + "-b", "repo": "fakerepo", "dep": dep,
             "target": target, "lane": "next", "status": "waiting-review",
             "run_id": run["id"], "branch": branch})
        sched.save(st)
        d = _payload(str(origin_b), str(self.proof), branch,
                     id="t-batch-base")
        d["primaries"][0]["dep"] = dep
        d["primaries"][0]["target"] = target
        d["primaries"][0]["run_id"] = run["id"]
        snap = runner.source_snapshot(origin_b)
        d["primaries"][0]["source"]["head"] = snap["head"]
        d["primaries"][0]["source"]["fingerprint"] = snap["fingerprint"]
        d["origin"]["base_head"] = subprocess.run(
            ["git", "rev-parse", "main"], cwd=str(origin_b), check=True,
            text=True, stdout=subprocess.PIPE).stdout.strip()
        _bind_accept(run["id"], snap["head"], snap["fingerprint"])
        d["integrations"][0]["head"] = snap["head"]
        d["integrations"][0]["fingerprint"] = snap["fingerprint"]
        for g in d["integrations"][0]["gates"]:
            if g["status"] == "pass":
                g["inputs"] = {"head": snap["head"],
                               "fingerprint": snap["fingerprint"]}
        return batches.import_batch(d), origin_b, branch

class InheritedTest(ChainFixtureMixin, BatchFixture):
    def test_inherited_requires_real_batch_count_and_freshness(self):
        base = self._second()
        d = self._payload(id="t-batch-top")
        d["inherited"] = [{"group": "stable", "count": 1,
                           "batch": "t-batch-base"}]
        top = batches.import_batch(d)
        ev = batches.evaluate(top, batches.live_map([top, base]))
        inh = ev["results"][0]["inherited"]
        self.assertEqual((inh[0]["ready"], inh[0]["total"]), (1, 1))
        self.assertTrue(ev["results"][0]["ready"])
        d["inherited"] = [{"group": "stable", "count": 2,
                           "batch": "t-batch-base"}]
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        d["inherited"] = [{"group": "stable", "count": 1,
                           "batch": "nope"}]
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)
        d["inherited"] = [{"group": "stable", "count": 1,
                           "batch": "t-batch-top"}]
        with self.assertRaises(batches.BatchError):
            batches.import_batch(d)

    def test_closure_positive_and_direction(self):
        # A lives on its own checkout; B's map alone carries none of it.
        base, origin_b, _ = self._distinct_base()
        d = self._payload(id="t-batch-top")
        d["inherited"] = [{"group": "stable", "count": 1,
                           "batch": "t-batch-base"}]
        top = batches.import_batch(d)
        ev = batches.evaluate(top, batches.live_map([top]))
        self.assertFalse(ev["results"][0]["ready"])
        self.assertTrue(any("t-batch-base" in r
                            for r in ev["results"][0]["reasons"]),
                        ev["results"][0]["reasons"])
        inh = ev["results"][0].get("inherited") or []
        self.assertEqual([(h.get("batch"), h.get("ready"), h.get("total"))
                          for h in inh],
                         [("t-batch-base", 0, 1)])
        # With the shared reachable-inheritance closure both become ready.
        live = batches.live_map(batches.closure([top]))
        ev = batches.evaluate(top, live)
        self.assertTrue(ev["results"][0]["ready"],
                        ev["results"][0]["reasons"])
        ev_base = batches.evaluate(base, live)
        self.assertTrue(ev_base["results"][0]["ready"])
        # A moving under B blocks B through the inherited entry.
        (origin_b / "g.txt").write_text("b3\n")
        _git(origin_b, "commit", "-am", "drift-b")
        live = batches.live_map(batches.closure([top]))
        ev = batches.evaluate(top, live)
        self.assertFalse(ev["results"][0]["ready"])
        self.assertTrue(any("inherit" in r.lower()
                            for r in ev["results"][0]["reasons"]),
                        ev["results"][0]["reasons"])
        ev_base = batches.evaluate(base, live)
        self.assertFalse(ev_base["results"][0]["ready"])
        # B's own failure must not block A: restore A, fail B instead.
        _git(origin_b, "checkout", "codex/next/demo-2")
        subprocess.run(["git", "reset", "--hard", "HEAD~1"],
                       cwd=str(origin_b), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        live = batches.live_map(batches.closure([top]))
        top["integrations"][0]["gates"][0]["status"] = "fail"
        ev_base = batches.evaluate(base, live)
        self.assertTrue(ev_base["results"][0]["ready"],
                        ev_base["results"][0]["reasons"])
        ev = batches.evaluate(top, live)
        self.assertFalse(ev["results"][0]["ready"])

    def test_stale_inherited_blocks_parent(self):
        base = self._second()
        d = self._payload(id="t-batch-top")
        d["inherited"] = [{"group": "stable", "count": 1,
                           "batch": "t-batch-base"}]
        top = batches.import_batch(d)
        live = batches.live_map([top, base])
        (self.origin / "f.txt").write_text("v9\n")
        _git(self.origin, "commit", "-am", "drift")
        ev = batches.evaluate(top, batches.live_map([top, base]))
        self.assertFalse(ev["results"][0]["ready"])
        self.assertTrue(any("inherited" in r for r in
                            ev["results"][0]["reasons"]))
        _ = live


class LeafCauseTest(ChainFixtureMixin, BatchFixture):
    """Nested failure leaves ride the computed evaluation (no re-reads).

    A three-level chain top -> mid -> base exposes the grandchild leaf
    with bounded batch/candidate context; fresh chains carry no leaf so
    existing shapes stay compatible; stored batches and live maps are
    never mutated; diagnostics never re-evaluate (call counter proves
    one structural evaluation per batch).
    """

    def _wire_inherited(self, batch_id, ref):
        from openmainty.batches import batch_file
        path = batch_file(batch_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["inherited"] = [{"group": "leaf-chain", "count": 1,
                              "batch": ref}]
        path.write_text(json.dumps(data), encoding="utf-8")

    def _chain3(self):
        base = self._distinct_base()
        origin_b = base[1]
        self._second(dep="mid", target="2.0", id="t-leaf-mid")
        self._wire_inherited("t-leaf-mid", "t-batch-base")
        top = self._payload(id="t-leaf-top")
        top["inherited"] = [{"group": "leaf-chain", "count": 1,
                             "batch": "t-leaf-mid"}]
        top = batches.import_batch(top)
        return top, origin_b

    def _eval_top(self, top):
        from openmainty import batches as _b
        live = _b.live_map(_b.closure([top]))
        return _b.evaluate(top, live), live

    def test_grandchild_missing_source_leaf_names_base(self):
        top, origin_b = self._chain3()
        shutil.rmtree(origin_b)
        ev, _live = self._eval_top(top)
        res = ev["results"][0]
        self.assertFalse(res["ready"])
        self.assertIn("inherited 't-leaf-mid' not fully proven (0/1 ready)",
                      res["reasons"])
        inh = res["inherited"][0]
        self.assertEqual((inh["batch"], inh["ready"], inh["total"],
                          inh["stale"]), ("t-leaf-mid", 0, 1, True))
        self.assertIn("t-batch-base", inh["cause"])
        self.assertIn("unreadable", inh["cause"])
        self.assertIn("t-batch-base", res["leaf"])
        self.assertIn("unreadable", res["leaf"])
        # The mid level carries the same deepest leaf (propagation).
        mid = batches.load_batch("t-leaf-mid")
        live = batches.live_map(batches.closure([mid]))
        mid_leaf = batches.evaluate(mid, live)["results"][0]["leaf"]
        self.assertIn("t-batch-base", mid_leaf)

    def test_grandchild_stale_gate_leaf_names_base(self):
        top, origin_b = self._chain3()
        (origin_b / "g.txt").write_text("b3\n")
        _git(origin_b, "commit", "-am", "drift-b")
        ev, _live = self._eval_top(top)
        res = ev["results"][0]
        self.assertFalse(res["ready"])
        self.assertIn("t-batch-base", res["leaf"])
        self.assertIn("STALE", res["leaf"])

    def test_fresh_chain_empty_leaf_stable_shapes_no_mutation(self):
        top, _origin_b = self._chain3()
        live = batches.live_map(batches.closure([top]))
        before_live = copy.deepcopy(live)
        before_bytes = {}
        for bid in ("t-leaf-top", "t-leaf-mid", "t-batch-base"):
            before_bytes[bid] = batches.batch_file(bid).read_bytes()
        ev = batches.evaluate(top, live)
        for r in ev["results"]:
            self.assertTrue(r["ready"])
            self.assertEqual(r["leaf"], "")
            for h in r["inherited"]:
                self.assertNotIn("cause", h)
        self.assertEqual(live, before_live)
        for bid, raw in before_bytes.items():
            self.assertEqual(batches.batch_file(bid).read_bytes(), raw)

    def test_no_diagnostic_reevaluation_on_deep_chain(self):
        top, origin_b = self._chain3()
        shutil.rmtree(origin_b)
        calls = []
        real = batches.evaluate

        def counting(batch, live=None, _chain=(), **kw):
            calls.append(batch.get("id"))
            return real(batch, live, _chain, **kw)

        with mock.patch.object(batches, "evaluate", counting):
            live = batches.live_map(batches.closure([top]))
            ev = batches.evaluate(top, live)
        # One structural evaluation per batch; diagnostics re-read nothing.
        self.assertEqual(sorted(calls),
                         ["t-batch-base", "t-leaf-mid", "t-leaf-top"])
        self.assertFalse(ev["results"][0]["ready"])
        self.assertIn("t-batch-base", ev["results"][0]["leaf"])

    def test_leaf_bounds_prioritize_deepest(self):
        sub = {"results": [{"ready": False, "dep": "d", "target": "t",
                            "lane": "l",
                            "reasons": ["z" * 500],
                            "inherited": [], "leaf": "y" * 600}]}
        cause = batches._cause_from_sub("b", sub)
        self.assertLessEqual(len(cause), 500)
        # The deepest leaf survives whole; the wrapper shrinks first.
        self.assertTrue(cause.endswith("y" * 100))
        self.assertEqual(batches._cause_from_sub("b", {"results": []}),
                         None)

    def test_circular_inherited_terminates_with_reason(self):
        self._second()
        from openmainty.batches import batch_file
        path = batch_file("t-batch-base")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["inherited"] = [{"group": "s", "count": 1,
                              "batch": "t-batch-base"}]
        path.write_text(json.dumps(data), encoding="utf-8")
        b = batches.load_batch("t-batch-base")
        ev = batches.evaluate(b, batches.live_map([b]))
        self.assertFalse(ev["results"][0]["ready"])
        self.assertTrue(any("circular" in r
                            for r in ev["results"][0]["reasons"]))


class AggregateTest(BatchFixture):
    def test_counts_and_next_action(self):
        b = self._import_ok()
        live = {"t-batch-1": batches.live_map([b])}
        row = batches.summarize([b], live)["fakerepo"]
        self.assertEqual((row["unique"], row["ready"], row["reviewed"]),
                         (1, 1, 1))
        live = {"t-batch-1": dict(batches.live_map([b]))}
        wt = b["integrations"][0]["worktree"]
        live["t-batch-1"]["wt:" + wt] = dict(
            live["t-batch-1"]["wt:" + wt], head="zzz")
        row = batches.summarize([b], live)["fakerepo"]
        self.assertEqual(row["ready"], 0)
        self.assertIn("검증 중", row["next_action"])

    def test_summarize_precomputed_reuses_without_evaluate(self):
        b = self._import_ok()
        live = {"t-batch-1": batches.live_map([b])}
        ev = batches.evaluate(b, live["t-batch-1"])
        with mock.patch.object(batches, "evaluate",
                               side_effect=AssertionError(
                                   "must not re-evaluate")):
            row = batches.summarize(
                [b], live, precomputed={"t-batch-1": ev})["fakerepo"]
        self.assertEqual((row["unique"], row["ready"], row["reviewed"]),
                         (1, 1, 1))

    def test_summarize_precomputed_missing_fails_closed(self):
        b = self._import_ok()
        live = {"t-batch-1": batches.live_map([b])}
        with mock.patch.object(batches, "evaluate",
                               side_effect=AssertionError(
                                   "must not re-evaluate")):
            row = batches.summarize([b], live,
                                    precomputed={})["fakerepo"]
        self.assertEqual(row["ready"], 0)
        self.assertEqual(row["validating"], 1)

    def test_descendant_disagreement_keeps_original_cause(self):
        """Side-effect fixture: the parent recursion saw the child
        stale, but the same-envelope child card computed ready (a run
        read moved between the two computations). The nested entry
        must keep the ORIGINAL cause, label its provenance, and mark
        the disagreement — never present the later success as clearing
        the parent failure."""
        child_rec = {"id": "t-child", "repo": "fakerepo",
                     "primaries": [{"dep": "c", "target": "1",
                                    "lane": "next"}],
                     "integrations": [], "origin": {}}
        parent_rec = {"id": "t-parent", "repo": "fakerepo",
                      "primaries": [{"dep": "p", "target": "1",
                                     "lane": "next"}],
                      "integrations": [], "origin": {}}
        parent_ev = {"results": [
            {"key": "fakerepo|p|1|next", "dep": "p", "target": "1",
             "lane": "next", "ready": False,
             "reasons": ["inherited 't-child' not fully proven "
                         "(0/1 ready)"],
             "leaf": "parent-leaf",
             "inherited": [{"batch": "t-child", "ready": 0, "total": 1,
                            "stale": True, "cause": "ORIGINAL-CAUSE"}]}]}
        child_ready_ev = {"results": [
            {"key": "fakerepo|c|1|next", "dep": "c", "target": "1",
             "lane": "next", "ready": True, "reasons": [], "leaf": "",
             "inherited": []}]}
        by_id = {"t-parent": parent_rec, "t-child": child_rec}
        env = {"t-parent": parent_ev, "t-child": child_ready_ev}
        diag = batches.card_diagnostic(parent_rec, parent_ev, {}, by_id,
                                       env)
        self.assertEqual(diag["ready"], 0)
        ff = diag["first_failure"]
        self.assertEqual(ff["key"], "fakerepo|p|1|next")
        self.assertEqual(ff["leaf"], "parent-leaf")
        desc = ff["descendants"][0]
        self.assertEqual(desc["batch"], "t-child")
        self.assertEqual(desc["cause"], "ORIGINAL-CAUSE")
        self.assertIn("separate computation", desc["provenance"])
        self.assertIn("t-child", desc["disagreement"])
        self.assertIn("original parent cause retained",
                      desc["disagreement"])
        # Consistent case: failing child card carries no disagreement.
        child_fail_ev = {"results": [
            {"key": "fakerepo|c|1|next", "dep": "c", "target": "1",
             "lane": "next", "ready": False,
             "reasons": ["candidate source fingerprint changed"],
             "leaf": "child-leaf", "inherited": []}]}
        env2 = {"t-parent": parent_ev, "t-child": child_fail_ev}
        diag2 = batches.card_diagnostic(parent_rec, parent_ev, {}, by_id,
                                        env2)
        desc2 = diag2["first_failure"]["descendants"][0]
        self.assertIsNone(desc2["disagreement"])
        self.assertEqual(
            desc2["first_failure"]["key"], "fakerepo|c|1|next")

    def test_uncovered_only_review_terminal_dep_items(self):
        st = sched.load("fakerepo")
        st.setdefault("queue", []).extend([
            {"key": "k-new", "repo": "fakerepo", "dep": "eslint",
             "target": "10.13.0", "lane": "next", "status": "queued"},
            {"key": "k-w", "repo": "fakerepo", "dep": "watched",
             "target": "1", "lane": "next", "status": "waiting-review",
             "watch": {"active": True}},
            {"key": "k-q", "repo": "fakerepo", "dep": "rawbuffer",
             "target": "", "lane": "", "status": "blocked"}])
        sched.save(st)
        b = self._import_ok()
        out = batches.uncovered_candidates(
            sched.load("fakerepo")["queue"], [b], "fakerepo")
        deps = {(c["dep"], c["target"]) for c in out}
        self.assertEqual(deps, set())
        st = sched.load("fakerepo")
        st["queue"].append({"key": "k-e", "repo": "fakerepo", "dep": "eslint",
                            "target": "10.13.0", "lane": "next",
                            "status": "waiting-review"})
        out = batches.uncovered_candidates(st["queue"], [b], "fakerepo")
        self.assertEqual({(c["dep"], c["target"]) for c in out},
                         {("eslint", "10.13.0")})


if __name__ == "__main__":
    unittest.main()
