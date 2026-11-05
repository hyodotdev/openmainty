"""Readiness leaf diagnostics + persisted negative record (revision 65b).

The SAME failed evaluation response must retain, per failing leaf: the
batch chain (parent->..->leaf), primary key, live-map key kind and
worktree, expected vs actual head/fingerprint (short) and clean, the
failing reason code, and the read-error category/op/rc — without
re-reading sources, and without ever exporting stderr text, file bytes,
or secrets. A bounded persisted record (diagnostics/, never batches/)
keeps the frame when nobody watches the UI: deduped within a window,
re-appended after the window or after recovery, never edited by a
recovered poll.
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

from openmainty import batches, runner, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

COOKIE = ".watchman-cookie-CPK-Mac-mini-3.local-17185-2268"
SECRET = "SECRET-XYZ-123-NEVER-EXPORT"


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout.strip()


class ChainFixture(unittest.TestCase):
    """Three-level inheritance over real git worktrees (linked .git FILE)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        self.base = Path(self.tmp.name) / "r"
        self.base.mkdir()
        git("init", "-b", "main", cwd=self.base)
        git("config", "user.email", "test@localhost", cwd=self.base)
        git("config", "user.name", "test", cwd=self.base)
        (self.base / "a.txt").write_text("v1\n", encoding="utf-8")
        git("add", "-A", cwd=self.base)
        git("commit", "-qm", "init", cwd=self.base)
        self.integ = Path(self.tmp.name) / "integ"
        git("worktree", "add", "-b", "integ-main", str(self.integ), "main",
            cwd=self.base)
        self.cands = {}
        for name in ("leaf", "mid", "top"):
            wt = Path(self.tmp.name) / ("cand-" + name)
            git("worktree", "add", "-b", "cand-" + name, str(wt), "main",
                cwd=self.base)
            self.cands[name] = wt
        self.run_ids = {n: self._write_run(w) for n, w in self.cands.items()}
        self.leaf_id, self.mid_id, self.top_id = \
            "t5-leaf-20261019", "t5-mid-20261019", "t5-top-20261019"
        self._write_batch(self.leaf_id, "leaf", [])
        self._write_batch(self.mid_id, "mid", [self.leaf_id])
        self._write_batch(self.top_id, "top", [self.mid_id])
        self.by_id = {b["id"]: b for b in
                      (self._read(self.leaf_id), self._read(self.mid_id),
                       self._read(self.top_id))}

    def _read(self, bid):
        return json.loads(batches.batch_file(bid).read_text(
            encoding="utf-8"))

    def _write_run(self, cand_wt):
        snap = runner.source_snapshot(cand_wt)
        run = store.create("fakerepo", "1", "t", None, {"by": "test"})
        run["workCommit"] = snap["head"]
        run["workTreeFingerprint"] = snap["fingerprint"]
        for step in ("review", "e2e"):
            run["steps"][step]["status"] = "done"
        run["verifiedCommits"] = {"review": snap["head"],
                                  "e2e": snap["head"]}
        run["verifiedFingerprints"] = {"review": snap["fingerprint"],
                                       "e2e": snap["fingerprint"]}
        store.save(run)
        return run["id"]

    def _write_batch(self, bid, cand_name, inherits):
        isnap = runner.source_snapshot(self.integ)
        csnap = runner.source_snapshot(self.cands[cand_name])
        base_head = git("rev-parse", "main", cwd=self.base)
        evdir = batches.evidence_dir(bid)
        evdir.mkdir(parents=True, exist_ok=True)
        blob = ("evidence %s\n" % bid).encode()
        import hashlib
        digest = hashlib.sha256(blob).hexdigest()
        (evdir / (digest + "-proof.txt")).write_bytes(blob)
        batch = {
            "schema": 3, "id": bid, "repo": "fakerepo",
            "target_branch": "main", "purpose": "t",
            "created_at": "x", "updated_at": "x",
            "origin": {"checkout": str(self.base), "base_branch": "main",
                       "base_head": base_head},
            "primaries": [{
                "dep": "d-" + cand_name, "target": "1", "lane": "main",
                "run_id": self.run_ids[cand_name],
                "source": {"worktree": str(self.cands[cand_name]),
                           "branch": "cand-" + cand_name,
                           "head": csnap["head"],
                           "fingerprint": csnap["fingerprint"]},
                "review": {"verdict": "accept", "at": "", "findings": []},
                "required_gates": ["fastchecks"], "note": ""}],
            "inherited": [{"group": "g", "count": 1, "batch": ref}
                          for ref in inherits],
            "integrations": [{
                "lane": "main", "worktree": str(self.integ),
                "head": isnap["head"], "fingerprint": isnap["fingerprint"],
                "status": "",
                "gates": [{
                    "key": "fastchecks", "status": "pass", "at": "",
                    "note": "", "root_attested": False,
                    "inputs": {"head": isnap["head"],
                               "fingerprint": isnap["fingerprint"]},
                    "evidence": [{
                        "label": "e", "kind": "file", "origin": "/x",
                        "sha256": digest, "bytes": len(blob),
                        "stored": True,
                        "stored_path": "evidence/" + digest + "-proof.txt",
                    }],
                }],
            }],
            "policy": {"required_gates": ["fastchecks"],
                       "gates": {"fastchecks": {"label": "Fast",
                                                "root": False,
                                                "runtime": False}},
                       "notes": ""},
            "history": [],
        }
        batches.batch_file(bid).write_text(json.dumps(batch),
                                           encoding="utf-8")

    def _eval_top(self, record=False):
        top = self.by_id[self.top_id]
        live, diag = batches.live_map_with_diag(
            batches.closure([top]))
        return batches.evaluate(top, live, live_diag=diag, record=record)

    def test_green_baseline_ready(self):
        ev = self._eval_top()
        self.assertTrue(ev["results"][0]["ready"],
                        ev["results"][0]["reasons"])
        self.assertNotIn("diag", ev["results"][0])

    def test_real_dirt_leaf_diag_rides_three_levels(self):
        """A lone cookie is now a PASSING state (the fix), so the
        propagation test uses a real untracked file as the trigger —
        with a cookie alongside to prove it stays invisible."""
        (self.cands["leaf"] / "real-new.js").write_text("real\n",
                                                        encoding="utf-8")
        (self.cands["leaf"] / COOKIE).write_text("cookie\n",
                                                encoding="utf-8")
        try:
            ev = self._eval_top()
        finally:
            (self.cands["leaf"] / "real-new.js").unlink()
            (self.cands["leaf"] / COOKIE).unlink()
        top_res = ev["results"][0]
        self.assertFalse(top_res["ready"])
        self.assertIn("not fully proven (0/1 ready)",
                      " ".join(top_res["reasons"]))
        d = top_res.get("diag")
        self.assertIsInstance(d, dict)
        self.assertEqual(d["chain"],
                         [self.top_id, self.mid_id, self.leaf_id])
        leaf_key = batches.candidate_key("fakerepo", "d-leaf", "1",
                                         "main")
        self.assertEqual(d["key"], leaf_key)
        self.assertEqual(d["kind"], "cand")
        self.assertEqual(d["reason_code"],
                         "candidate_fingerprint_changed")
        self.assertEqual(d["path"], "real-new.js")
        self.assertEqual(d["untracked_sample"], ["real-new.js"])
        self.assertEqual(d["worktree"], str(self.cands["leaf"]))
        self.assertFalse(d["clean"])
        leaf_batch = self.by_id[self.leaf_id]
        rec_fp = leaf_batch["primaries"][0]["source"]["fingerprint"][:12]
        self.assertEqual(d["expected_fp"], rec_fp)
        self.assertNotEqual(d["actual_fp"], rec_fp)
        inh = [h for h in top_res["inherited"]
               if h.get("batch") == self.mid_id][0]
        self.assertEqual(inh.get("diag"), d)
        cards = batches.card_diagnostic(
            self.by_id[self.top_id], ev,
            batches.live_map([self.by_id[self.top_id]]),
            self.by_id, {self.top_id: ev})
        ff = cards["first_failure"]
        self.assertEqual((ff.get("diag") or {}).get("reason_code"),
                         "candidate_fingerprint_changed")
        self.assertEqual((ff.get("diag") or {}).get("path"),
                         "real-new.js")

    def test_record_once_per_top_eval_recovery_preserves(self):
        from openmainty import paths
        lines_path = (paths.home() / "diagnostics"
                      / "readiness-negatives.jsonl")
        (self.cands["leaf"] / "real-new.js").write_text("real\n",
                                                        encoding="utf-8")
        try:
            ev = self._eval_top(record=True)
        finally:
            (self.cands["leaf"] / "real-new.js").unlink()
        self.assertFalse(ev["results"][0]["ready"])
        saved = lines_path.read_bytes()
        self.assertEqual(len(saved.splitlines()), 1)
        rec = json.loads(saved.decode("utf-8"))
        self.assertEqual(rec["chain"],
                         [self.top_id, self.mid_id, self.leaf_id])
        self.assertEqual(rec["path"], "real-new.js")
        ev2 = self._eval_top(record=True)
        self.assertTrue(ev2["results"][0]["ready"])
        self.assertEqual(lines_path.read_bytes(), saved)

    def test_mismatch_sample_names_real_file_never_cookie(self):
        (self.cands["leaf"] / "real-new.js").write_text("real\n",
                                                        encoding="utf-8")
        (self.cands["leaf"] / COOKIE).write_text("cookie\n",
                                                encoding="utf-8")
        try:
            leaf = self.by_id[self.leaf_id]
            live, diag = batches.live_map_with_diag([leaf])
            ev = batches.evaluate(leaf, live, live_diag=diag)
        finally:
            (self.cands["leaf"] / "real-new.js").unlink()
            (self.cands["leaf"] / COOKIE).unlink()
        res = ev["results"][0]
        self.assertFalse(res["ready"])
        d = res.get("diag") or {}
        self.assertEqual(d.get("reason_code"),
                         "candidate_fingerprint_changed")
        self.assertEqual(d.get("untracked_sample"), ["real-new.js"])

    def test_diagless_not_ready_neither_records_nor_recovers(self):
        """End to end through evaluate(record=True): a primary whose
        every failure is diag-less (verdict pending, notes-only gate
        evidence, unbound run — no read failure anywhere) gets no diag,
        writes no record, and does not count as recovery."""
        from openmainty import paths
        isnap = runner.source_snapshot(self.integ)
        csnap = runner.source_snapshot(self.cands["leaf"])
        base_head = git("rev-parse", "main", cwd=self.base)
        gate = {"key": "fastchecks", "status": "pass", "at": "",
                "note": "", "root_attested": False,
                "inputs": {"head": isnap["head"],
                           "fingerprint": isnap["fingerprint"]},
                "evidence": [{"label": "n", "kind": "note", "text": "t"}]}
        top = {"schema": 3, "id": "b-pending", "repo": "fakerepo",
               "target_branch": "main", "purpose": "t",
               "created_at": "x", "updated_at": "x",
               "origin": {"checkout": str(self.base),
                          "base_branch": "main", "base_head": base_head},
               "primaries": [{"dep": "d", "target": "1", "lane": "main",
                              "run_id": "",
                              "source": {"worktree": str(self.cands["leaf"]),
                                         "branch": "cand-leaf",
                                         "head": csnap["head"],
                                         "fingerprint": csnap["fingerprint"]},
                              "review": {"verdict": "pending", "at": "",
                                         "findings": []},
                              "required_gates": ["fastchecks"], "note": ""}],
               "inherited": [],
               "integrations": [{"lane": "main",
                                 "worktree": str(self.integ),
                                 "head": isnap["head"],
                                 "fingerprint": isnap["fingerprint"],
                                 "status": "", "gates": [gate]}],
               "policy": {"required_gates": ["fastchecks"],
                          "gates": {"fastchecks": {"label": "Fast",
                                                   "root": False,
                                                   "runtime": False}},
                          "notes": ""},
               "history": []}
        lines_path = (paths.home() / "diagnostics"
                      / "readiness-negatives.jsonl")
        before = (lines_path.read_bytes()
                  if lines_path.exists() else None)
        live, diag = batches.live_map_with_diag([top])
        ev = batches.evaluate(top, live, live_diag=diag, record=True)
        res = ev["results"][0]
        self.assertFalse(res["ready"])
        self.assertNotIn("diag", res)
        after = (lines_path.read_bytes()
                 if lines_path.exists() else None)
        self.assertEqual(after, before)

    def test_run_proof_flip_shows_step_states(self):
        vstat = {"review": "unknown", "e2e": "fresh", "reproduce": "open"}
        with mock.patch.object(store, "verification_status",
                               return_value=dict(vstat)):
            ev = self._eval_top()
        res = ev["results"][0]
        self.assertFalse(res["ready"])
        self.assertIn("bound run review proof no longer fresh",
                      " ".join(res["reasons"]))
        d = res.get("diag") or {}
        self.assertEqual(d["reason_code"], "run_proof_stale")
        self.assertEqual(d["kind"], "run-proof")
        self.assertEqual(d.get("vstat"), vstat)

    def test_secret_bytes_never_export_op_rc_kept(self):
        target = self.cands["leaf"] / "leak.txt"
        target.write_text("payload %s end\n" % SECRET, encoding="utf-8")
        try:
            real_snap = runner.source_snapshot

            def fail_leaf(work):
                if str(work) == str(self.cands["leaf"]):
                    raise runner.SourceUnreadable("read poisoned-rel",
                                                  rc=5)
                return real_snap(work)

            with mock.patch.object(runner, "source_snapshot",
                                   side_effect=fail_leaf):
                leaf = self.by_id[self.leaf_id]
                live, diag = batches.live_map_with_diag([leaf])
                ev = batches.evaluate(leaf, live, live_diag=diag)
        finally:
            target.unlink()
        blob = json.dumps(ev, ensure_ascii=False, default=str)
        self.assertNotIn(SECRET, blob)
        d = ev["results"][0].get("diag") or {}
        self.assertEqual(d.get("category"), "source_unreadable")
        self.assertEqual(d.get("op"), "read poisoned-rel")
        self.assertEqual(d.get("rc"), 5)


class RecordLayerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _diag(self, path="rel/x.js", reason="candidate_not_clean"):
        return {"chain": ["b-top", "b-mid", "b-leaf"], "key": "r|d|1|main",
                "kind": "cand", "map_key": "cand:/wt",
                "worktree": "/wt", "reason_code": reason,
                "category": None, "op": None, "rc": None, "path": path,
                "expected_head": "a" * 12, "expected_fp": "b" * 12,
                "actual_head": "a" * 12, "actual_fp": "c" * 12,
                "clean": False}

    def _lines_path(self):
        from openmainty import paths
        return paths.home() / "diagnostics" / "readiness-negatives.jsonl"

    def _state_path(self):
        from openmainty import paths
        return (paths.home() / "diagnostics"
                / "readiness-negatives-state.json")

    def _read_lines(self):
        p = self._lines_path()
        if not p.exists():
            return []
        return p.read_text(encoding="utf-8").splitlines()

    def test_identical_repeats_suppressed_until_recovery(self):
        """A persistently failing key appends once no matter how much
        mocked time passes (short of the heartbeat): no flood, no
        eviction of older flap records."""
        d = self._diag()
        epochs = [1000.0 + i * 400 for i in range(8)]
        with mock.patch.object(batches, "time") as mock_time:
            mock_time.time.side_effect = epochs
            for _ in range(8):
                batches._record_negative("b-top", "r|d|1|main", d)
        self.assertEqual(len(self._read_lines()), 1)

    def test_heartbeat_reappends_after_six_hours(self):
        d = self._diag()
        with mock.patch.object(batches, "time") as mock_time:
            mock_time.time.side_effect = [2000.0,
                                          2000.0 + 6 * 3600 + 1]
            batches._record_negative("b-top", "r|d|1|main", d)
            batches._record_negative("b-top", "r|d|1|main", d)
        self.assertEqual(len(self._read_lines()), 2)

    def test_record_bounded_by_lines_and_bytes(self):
        for i in range(250):
            d = self._diag(path="rel/f%03d.js" % i)
            batches._record_negative("b-top", "k%03d" % i, d)
        lines = self._read_lines()
        self.assertEqual(len(lines), batches._NEG_MAX_LINES)
        self.assertLessEqual(
            len(("\n".join(lines) + "\n").encode("utf-8")),
            batches._NEG_MAX_BYTES)
        first = json.loads(lines[0])
        self.assertEqual(first["path"], "rel/f050.js")

    def test_recovery_appends_nothing_and_reenables(self):
        d = self._diag()
        batches._record_negative("b-top", "r|d|1|main", d)
        snapshot = self._lines_path().read_bytes()
        batches._record_recovery("b-top", "r|d|1|main")
        self.assertEqual(self._lines_path().read_bytes(), snapshot)
        batches._record_negative("b-top", "r|d|1|main", d)
        lines = self._read_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual((lines[0] + "\n").encode("utf-8"), snapshot)

    def test_green_evaluation_writes_nothing(self):
        from openmainty import paths
        batches._record_recovery("nope", "nope")
        self.assertFalse((paths.home() / "diagnostics").exists())


if __name__ == "__main__":
    unittest.main()
