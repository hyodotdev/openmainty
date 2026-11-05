"""Batches snapshot for /api/batches (S7-a).

Covers batches.build_batches_envelope / refresh_batches_snapshot and
the serve layer on top, against an isolated OPENMAINTY_HOME with one
minimal batch (no worktrees — unreadable sources read as not-ready,
never as matching):

- refresh writes an atomic stamped file (at/writer/schema + the same
  batches/summary/unregistered/obs shape the request path served).
- GET serves the file verbatim plus served_at (no recompute).
- POST /api/batches/refresh needs confirm:true, returns at once, and
  the background thread writes the file.
- Cold start (no file) computes synchronously, as before.

Py3.9 portable.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, store  # noqa: E402
from openmainty.util import write_json  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


class BatchesSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        write_json(batches.batches_dir() / "snap-b1.json", {
            "schema": 3, "id": "snap-b1", "repo": "fakerepo",
            "target_branch": "next", "purpose": "snapshot probe",
            "updated_at": "2026-10-20T00:00:00Z",
            "primaries": [], "inherited": [], "integrations": [],
            "policy": {"gates": {}, "required_gates": []}})

    def test_refresh_writes_stamped_file(self):
        snap = batches.refresh_batches_snapshot("test")
        self.assertEqual(snap.get("writer"), "test")
        self.assertTrue(snap.get("at"))
        self.assertEqual([b["id"] for b in snap.get("batches")],
                         ["snap-b1"])
        self.assertIn("summary", snap)
        self.assertIn("unregistered", snap)
        self.assertTrue((snap.get("obs") or "").startswith("obs-"))
        stored = json.loads(
            batches.batches_snapshot_path().read_text(
                encoding="utf-8"))
        self.assertEqual(stored["at"], snap["at"])
        again = batches.refresh_batches_snapshot("test2")
        self.assertEqual(again.get("writer"), "test2")

    def test_serve_reads_file_verbatim(self):
        from openmainty import serve  # noqa: E402
        snap = batches.refresh_batches_snapshot("test")
        out = serve.api_batches({})
        self.assertEqual(out["at"], snap["at"])
        self.assertEqual(out["writer"], "test")
        self.assertEqual([b["id"] for b in out["batches"]],
                         ["snap-b1"])
        # No per-request clock in the body: repaint compares semantic
        # content (obs/at/evaluated_at stripped); `at` is the stamp.
        self.assertNotIn("served_at", out)

    def test_refresh_route(self):
        from openmainty import serve  # noqa: E402
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_batches_refresh({})
        self.assertEqual(ctx.exception.status, 400)
        started = serve.api_batches_refresh({"confirm": True})
        self.assertTrue(started.get("started"))
        for _ in range(40):
            if batches.batches_snapshot_path().is_file():
                break
            time.sleep(0.25)
        self.assertTrue(
            batches.batches_snapshot_path().is_file())

    def test_cold_start_computes_synchronously(self):
        from openmainty import serve  # noqa: E402
        self.assertFalse(batches.batches_snapshot_path().exists())
        out = serve.api_batches({})
        self.assertEqual([b["id"] for b in out["batches"]],
                         ["snap-b1"])
        self.assertEqual(out.get("writer"), "request-fallback")


class StartupStaleTests(unittest.TestCase):
    # M2.9c: a restart must not drop a pending refresh. If the
    # batches snapshot predates the newest hold/attest event, or
    # any batch file is newer than the snapshot, the dashboard
    # schedules refresh_all_soon("startup").

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        write_json(batches.batches_dir() / "snap-b1.json", {
            "schema": 3, "id": "snap-b1", "repo": "fakerepo",
            "target_branch": "next", "purpose": "snapshot probe",
            "updated_at": "2026-10-20T00:00:00Z",
            "primaries": [], "inherited": [], "integrations": [],
            "policy": {"gates": {}, "required_gates": []}})

    def test_fresh_snapshot_not_stale(self):
        batches.refresh_batches_snapshot("test")
        st = batches.snapshot_stale_on_startup()
        self.assertFalse(st["stale"], st)

    def test_hold_event_after_snapshot_is_stale(self):
        from openmainty import events as _events
        batches.refresh_batches_snapshot("test")
        # A hold recorded after the snapshot (worker killed before
        # it could rewrite the file) — appended directly so no
        # background refresh fires.
        _events.append("fakerepo", "hold", "snap-b1", "보류 설정 — t")
        st = batches.snapshot_stale_on_startup()
        self.assertTrue(st["stale"], st)
        self.assertIn("hold", st["reason"])

    def test_newer_batch_file_is_stale(self):
        batches.refresh_batches_snapshot("test")
        path = batches.batches_dir() / "snap-b1.json"
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["purpose"] = "snapshot probe v2"
        write_json(path, rec)
        st = batches.snapshot_stale_on_startup()
        self.assertTrue(st["stale"], st)

    def test_startup_schedules_refresh_when_stale(self):
        from openmainty import events as _events
        from openmainty import serve as _serve
        batches.refresh_batches_snapshot("test")
        _events.append("fakerepo", "hold", "snap-b1", "보류 설정 — t")
        seen = []
        real = _serve.refresh_all_soon
        _serve.refresh_all_soon = lambda reason: (
            seen.append(reason), "started")[1]
        try:
            out = _serve._maybe_startup_refresh()
        finally:
            _serve.refresh_all_soon = real
        self.assertEqual(seen, ["startup"])
        self.assertTrue(out["stale"])


class ConcurrentRefreshTests(unittest.TestCase):
    """S7 condition 2: two concurrent refreshes leave batch files valid
    with exactly one readiness-negatives line per failure signature.

    The refresh lock serializes concurrent refreshes (the second serves
    the current file instead of stacking work); evaluate() performs
    zero batch-file writes under record=True, so batch bytes are
    untouched; the negatives dedupe keeps one line per signature.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        self.wt = self.root / "wt"
        self.wt.mkdir()
        run = store.create("fakerepo", "dep-foo-1.2.3",
                           "chore(deps): foo", None, {"by": "t"})
        self.run_id = run["id"]
        st = store.load(self.run_id)
        st["dep"] = "foo"
        st["target"] = "1.2.3"
        st["lane"] = "next"
        st["steps"] = {"review": {"status": "done", "exit_code": 0},
                       "e2e": {"status": "done", "exit_code": 0}}
        st["workCommit"] = "h" * 40
        st["workTreeFingerprint"] = "f" * 40
        st["verifiedCommits"] = {"review": "h" * 40, "e2e": "h" * 40}
        st["verifiedFingerprints"] = {"review": "f" * 40,
                                      "e2e": "f" * 40}
        store.save(st)
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        write_json(batches.batches_dir() / "snap-c1.json", {
            "schema": 3, "id": "snap-c1", "repo": "fakerepo",
            "target_branch": "next", "purpose": "concurrency probe",
            "updated_at": "2026-10-20T00:00:00Z",
            "primaries": [{
                "dep": "foo", "target": "1.2.3", "lane": "next",
                "run_id": self.run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(self.wt),
                           "head": "h" * 40, "fingerprint": "f" * 40}}],
            "inherited": [],
            "origin": {"checkout": str(self.wt),
                       "base_branch": "main", "base_head": "h" * 40},
            "integrations": [{
                "lane": "next", "worktree": str(self.wt),
                "status": "integration-testing",
                "gates": [{
                    "key": "unit", "status": "pass",
                    "inputs": {"head": "h" * 40,
                               "fingerprint": "f" * 40},
                    "evidence": [{"label": "log", "kind": "file",
                                  "origin": "proof"}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})
        self.batch_path = batches.batches_dir() / "snap-c1.json"
        self.before = self.batch_path.read_bytes()
        orig = batches._snapshot_status

        def fault(worktree):
            if str(self.wt) in (worktree or ""):
                return None, "source_unreadable", "tracked-list", 124
            return orig(worktree)

        batches._snapshot_status = fault
        self.addCleanup(setattr, batches, "_snapshot_status", orig)

    def test_two_concurrent_refreshes(self):
        errs = []
        outs = []

        def _run():
            try:
                outs.append(batches.refresh_all_snapshots("test"))
            except Exception as exc:  # noqa: BLE001 - record it
                errs.append(exc)

        threads = [threading.Thread(target=_run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
            self.assertFalse(t.is_alive(), "refresh thread hung")
        self.assertEqual(errs, [])
        self.assertEqual(len(outs), 2)
        # Batch files untouched (the refresh never writes them).
        self.assertEqual(self.batch_path.read_bytes(), self.before)
        json.loads(self.before.decode("utf-8"))
        # Exactly one negatives line for the failure signature.
        lines_path = (Path(os.environ["OPENMAINTY_HOME"])
                      / "diagnostics" / "readiness-negatives.jsonl")
        self.assertTrue(lines_path.is_file(), "no negatives file written")
        hits = [ln for ln in lines_path.read_text(
            encoding="utf-8").splitlines()
            if json.loads(ln).get("batch") == "snap-c1"]
        self.assertEqual(len(hits), 1)
        rec = json.loads(hits[0])
        self.assertEqual(rec["rc"], 124)
        # The snapshot shows the failure either way.
        snap = json.loads(
            batches.batches_snapshot_path().read_text(
                encoding="utf-8"))
        card = next(b for b in snap["batches"] if b["id"] == "snap-c1")
        self.assertEqual(card["ready"], 0)
        self.assertIsNotNone(
            (card.get("evaluation") or {}).get("first_failure"))


class CrossProcessNegativesTests(unittest.TestCase):
    """S7.1-c: the daemon tick (another process) and the dashboard
    cannot interleave negatives writes — fcntl.flock guards the whole
    read-modify-write. Two real processes refreshing concurrently leave
    exactly one line per signature and a valid state file. No mocks:
    the failure is a genuinely missing worktree."""

    def setUp(self):
        try:
            import fcntl  # noqa: F401
        except ImportError:
            self.skipTest("flock unavailable on this platform")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        missing = self.root / "gone-wt"
        run = store.create("fakerepo", "dep-foo-1.2.3",
                           "chore(deps): foo", None, {"by": "t"})
        st = store.load(run["id"])
        st["dep"] = "foo"
        st["target"] = "1.2.3"
        st["lane"] = "next"
        st["steps"] = {"review": {"status": "done", "exit_code": 0},
                       "e2e": {"status": "done", "exit_code": 0}}
        st["workCommit"] = "h" * 40
        st["workTreeFingerprint"] = "f" * 40
        st["verifiedCommits"] = {"review": "h" * 40, "e2e": "h" * 40}
        st["verifiedFingerprints"] = {"review": "f" * 40,
                                      "e2e": "f" * 40}
        store.save(st)
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        write_json(batches.batches_dir() / "snap-x1.json", {
            "schema": 3, "id": "snap-x1", "repo": "fakerepo",
            "target_branch": "next", "purpose": "xproc probe",
            "updated_at": "2026-10-20T00:00:00Z",
            "primaries": [{
                "dep": "foo", "target": "1.2.3", "lane": "next",
                "run_id": run["id"],
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(missing),
                           "head": "h" * 40, "fingerprint": "f" * 40}}],
            "inherited": [],
            "origin": {"checkout": str(missing),
                       "base_branch": "main", "base_head": "h" * 40},
            "integrations": [{
                "lane": "next", "worktree": str(missing),
                "status": "integration-testing",
                "gates": [{
                    "key": "unit", "status": "pass",
                    "inputs": {"head": "h" * 40,
                               "fingerprint": "f" * 40},
                    "evidence": [{"label": "log", "kind": "file",
                                  "origin": "proof"}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})

    def test_two_processes_one_line(self):
        home = os.environ["OPENMAINTY_HOME"]
        srcdir = str(Path(batches.__file__).resolve().parent.parent)
        code = ("import sys; sys.path.insert(0, %r); "
                "from openmainty import batches; "
                "batches.refresh_all_snapshots('xproc')" % srcdir)
        env = dict(os.environ, OPENMAINTY_HOME=home,
                   PYTHONPATH=srcdir)
        procs = [subprocess.Popen(
            [sys.executable, "-I", "-c", code],
            env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE) for _ in range(2)]
        for p in procs:
            _, err = p.communicate(timeout=300)
            self.assertEqual(p.returncode, 0, err.decode()[-2000:])
        lines_path = (Path(home) / "diagnostics"
                      / "readiness-negatives.jsonl")
        self.assertTrue(lines_path.is_file(), "no negatives file written")
        hits = [ln for ln in lines_path.read_text(
            encoding="utf-8").splitlines()
            if json.loads(ln).get("batch") == "snap-x1"]
        self.assertEqual(len(hits), 1, hits)
        state_path = (Path(home) / "diagnostics"
                      / "readiness-negatives-state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIsInstance(state.get("entries"), dict)
        self.assertTrue(
            any(k.startswith("snap-x1\x00")
                for k in state["entries"]), state)


class RefreshObsTests(unittest.TestCase):
    """S7.1-d: both snapshots carry the refresh pass id as
    `refresh_obs`; per-response `obs` differs on the wire for
    /api/batches and stays stored for /api/summary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        batches.batches_dir().mkdir(parents=True, exist_ok=True)

    def test_refresh_obs_shared_and_split(self):
        from openmainty import serve  # noqa: E402
        all_snap = batches.refresh_all_snapshots("test")
        bfile = json.loads(
            batches.batches_snapshot_path().read_text(
                encoding="utf-8"))
        sfile = json.loads(
            batches.summary_snapshot_path().read_text(
                encoding="utf-8"))
        self.assertEqual(bfile["refresh_obs"], all_snap["obs"])
        self.assertEqual(sfile["refresh_obs"], all_snap["obs"])
        out = serve.api_batches({})
        self.assertEqual(out["refresh_obs"], bfile["refresh_obs"])
        self.assertNotEqual(out["obs"], bfile["obs"])
        for c in out["batches"]:
            self.assertEqual(c["obs"], out["obs"])
        sout = serve.api_summary({})
        self.assertEqual(sout["refresh_obs"], sfile["refresh_obs"])
        self.assertEqual(sout["obs"], sfile["obs"])


class PrimaryItemsProjectionTests(unittest.TestCase):
    def test_unit_bounded_typed_only(self):
        self.assertEqual(batches._primary_items({}), [])
        self.assertEqual(batches._primary_items({"primaries": "nope"}), [])
        self.assertEqual(
            batches._primary_items({"primaries": [
                "nope", {"nodep": 1},
                {"dep": "turbo", "target": "2.11.10", "lane": "next",
                 "run_id": "run-5720a20b",
                 "source": {"worktree": "/secret"},
                 "evidence": [{"sha256": "x"}]},
                {"dep": 7, "target": "1.0"},
                {"dep": "expo", "target": "58.0.19", "lane": "next",
                 "run_id": "r", "extra": "kept-out"},
            ]}),
            [{"dep": "turbo", "target": "2.11.10", "lane": "next",
              "run_id": "run-5720a20b"},
             {"dep": "expo", "target": "58.0.19", "lane": "next",
              "run_id": "r"}])
        prefix = "d" * 80
        got = batches._primary_items(
            {"primaries": [{"dep": prefix + "-one", "target": "t"},
                           {"dep": prefix + "-two", "target": "t"}]})
        self.assertEqual(len(got), 2)
        self.assertNotEqual(got[0]["dep"], got[1]["dep"])
        self.assertTrue(got[0]["dep"].endswith("-one"))
        very_long = "d" * 300
        capped = batches._primary_items(
            {"primaries": [{"dep": very_long, "target": "t"}]})
        self.assertEqual(len(capped[0]["dep"]), 256)
        hostile = batches._primary_items(
            {"primaries": [{"dep": "<img>", "target": "<s>"}]})
        self.assertEqual(hostile[0]["dep"], "<img>")
        self.assertEqual(hostile[0]["target"], "<s>")

    def test_cached_envelope_carries_empty_projection(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "home"
        use_temp_home(self, str(home))
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        write_json(batches.batches_dir() / "snap-b1.json", {
            "schema": 3, "id": "snap-b1", "repo": "fakerepo",
            "target_branch": "next", "purpose": "snapshot probe",
            "updated_at": "2026-10-20T00:00:00Z",
            "primaries": [], "inherited": [], "integrations": [],
            "policy": {"gates": {}, "required_gates": []}})
        snap = batches.refresh_batches_snapshot("test")
        self.assertEqual(snap["batches"][0]["primary_items"], [])
        self.assertEqual(snap["batches"][0]["primaries"], 0)


if __name__ == "__main__":
    unittest.main()
