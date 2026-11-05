"""Revision 66 read gate: concurrent dashboard reads share one live-map pass.

Deterministic concurrency (Events/Barriers only; every wait bounded with
a timeout, then asserted so nothing can hang), temp OPENMAINTY_HOME,
real temp git repos, Python 3.9 compatible.

Covers: (a) N concurrent same-key requests -> at most 2 underlying reads
with identical results; (b) freshness: a change made after generation k
started is seen by a request that arrived after the change; (c) failing
(rc 124) and raising generations fail closed for all waiters and the
next generation recovers; (d) sequential requests never reuse a result;
(e) obs uniqueness per evaluation with identical semantics; (f) same-frame
diag on a negative under sharing; (g) a real ThreadingHTTPServer on an
ephemeral port with N clients. Plus review corrections: C1 read-key
keying (key == live set; same ids with changed content do not share;
batches/overview share), C3 one negatives line under sharing with
record=True unchanged, C4 waiter deadline (no fallback read), C5
read-only sharing canary across all four endpoint paths.
"""
import copy
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, pcconfig, repos, runner, sched, serve, store  # noqa: E402
try:
    from tests._home import use_temp_home, offline_service_status  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home, offline_service_status  # noqa: E402

PROFILE = {
    "name": "fakerepo",
    "github": "example/fakerepo",
    "baseBranch": "main",
    "knowledge": [],
    "checks": [{"name": "unit", "cmd": ["make", "unit"], "requires": []}],
    "e2e": [],
    "release": {"notes": "CHANGELOG.md"},
    "announce": {"medium": True, "discord": False, "slack": False},
}

WAIT_S = 60
JOIN_S = 120


def _git(cwd, *args):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            "git %s failed: %s" % (" ".join(args), proc.stderr[-300:]))
    return proc.stdout.strip()


def _strip_clocks(obj):
    """Deep-copy nouveau without per-observation clocks for semantic compare."""
    if isinstance(obj, dict):
        return {k: _strip_clocks(v) for k, v in obj.items()
                if k not in ("obs", "evaluated_at", "at")}
    if isinstance(obj, list):
        return [_strip_clocks(v) for v in obj]
    return obj


def _sem_json(obj):
    return json.dumps(_strip_clocks(obj), sort_keys=True, default=str)


class _Probe:
    """Counts batches.live_map_with_diag calls; optionally blocks them."""

    def __init__(self, test):
        self.calls = 0
        self.started = threading.Event()
        self.read_done = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self._lock = threading.Lock()
        self._orig = batches.live_map_with_diag
        batches.live_map_with_diag = self.wrap
        test.addCleanup(self.restore)

    def restore(self):
        batches.live_map_with_diag = self._orig

    def block(self):
        self.release.clear()

    def wrap(self, batch_list):
        with self._lock:
            self.calls += 1
        self.started.set()
        out = self._orig(batch_list)
        self.read_done.set()
        if not self.release.wait(timeout=JOIN_S):
            raise AssertionError("gate probe stalled")
        return out


class _FailFirst:
    """Raises on the first n underlying reads, then delegates.

    wait_before, when given, parks the first failing call until the
    event is set: failing generations publish in microseconds, so
    without it cohort workers would arrive after the failure deleted
    the entry and start fresh generations instead of sharing it.
    """

    def __init__(self, test, n, exc_factory, wait_before=None):
        self.calls = 0
        self._n = n
        self._exc_factory = exc_factory
        self._wait_before = wait_before
        self._orig = batches.live_map_with_diag
        batches.live_map_with_diag = self.wrap
        test.addCleanup(self.restore)

    def restore(self):
        batches.live_map_with_diag = self._orig

    def wrap(self, batch_list):
        self.calls += 1
        if self.calls <= self._n:
            if self.calls == 1 and self._wait_before is not None:
                self._wait_before.wait(timeout=JOIN_S)
            raise self._exc_factory()
        return self._orig(batch_list)


class Gate66Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        with serve._READGATE_COND:
            serve._READGATE.clear()
        self.addCleanup(self._assert_gate_drained)
        checkout = Path(self.tmp.name) / "checkout"
        checkout.mkdir()
        (checkout / "README.md").write_text("# fake\n")
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["fakerepo"] = str(checkout)
        pcconfig.save(pc)
        self.batch_id = "gate-batch-1"
        self.run_id, self.origin, self.integ = self._seed(self.batch_id)

    def _assert_gate_drained(self):
        with serve._READGATE_COND:
            self.assertEqual(serve._READGATE, {})

    def _seed(self, batch_id):
        root = Path(self.tmp.name)
        origin = root / ("origin-%s" % batch_id)
        origin.mkdir()
        _git(origin, "init", "-b", "main")
        _git(origin, "config", "user.email", "t@t.t")
        _git(origin, "config", "user.name", "t")
        (origin / "f.txt").write_text("v1\n")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "base")
        base_head = _git(origin, "rev-parse", "main")
        _git(origin, "checkout", "-b", "codex/next/demo-qa")
        (origin / "f.txt").write_text("v2\n")
        _git(origin, "commit", "-am", "cand")
        cand = runner.source_snapshot(origin)
        integ = root / ("integ-%s" % batch_id)
        _git(origin, "worktree", "add", "--detach", str(integ), cand["head"])
        integ_snap = runner.source_snapshot(integ)
        self.assertEqual(integ_snap["head"], cand["head"])
        proof = root / ("proof-%s.json" % batch_id)
        proof.write_bytes(b'{"ok": true}')
        run = store.create("fakerepo", "nitrogen@0.37.1", "t", None,
                           {"by": "t"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k-%s" % batch_id, "repo": "fakerepo", "dep": "nitrogen",
             "target": "0.37.1", "lane": "next", "status": "waiting-review",
             "run_id": run["id"], "branch": "codex/next/demo-qa"})
        sched.save(st)
        self._bind_accept(run["id"], cand["head"], cand["fingerprint"])
        batches.import_batch({
            "id": batch_id, "repo": "fakerepo",
            "target_branch": "next", "purpose": "gate 66",
            "origin": {"checkout": str(origin), "base_branch": "main",
                       "base_head": base_head},
            "primaries": [{
                "dep": "nitrogen", "target": "0.37.1", "lane": "next",
                "run_id": run["id"],
                "source": {"worktree": str(origin),
                           "branch": "codex/next/demo-qa",
                           "head": cand["head"],
                           "fingerprint": cand["fingerprint"]},
                "review": {"verdict": "accept", "findings": []}}],
            "integrations": [{
                "lane": "next", "worktree": str(integ),
                "status": "premerge-verified",
                "gates": [{
                    "key": "unit", "status": "pass",
                    "note": "gate66.", "root_attested": True,
                    "inputs": {"head": integ_snap["head"],
                               "fingerprint": integ_snap["fingerprint"]},
                    "evidence": [{"label": "log", "kind": "file",
                                  "origin": str(proof)}]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {"label": "unit"}}},
        })
        return run["id"], origin, integ

    def _bind_accept(self, run_id, head, fp):
        run = store.load(run_id)
        for step in store.VERIFY_STEPS:
            run["steps"][step]["status"] = "done"
        run["workCommit"] = head
        run["workTreeFingerprint"] = fp
        run["verifiedCommits"] = {"review": head, "e2e": head}
        run["verifiedFingerprints"] = {"review": fp, "e2e": fp}
        store.save(run)

    def _full_list(self):
        return batches.list_batches()

    def _spawn(self, n, fn):
        """Start n barrier-aligned workers; return (barrier, threads, out, errs)."""
        barrier = threading.Barrier(n + 1)
        out = [None] * n
        errs = [None] * n

        def work(i):
            try:
                barrier.wait(timeout=WAIT_S)
                out[i] = fn(i)
            except BaseException as exc:  # noqa: BLE001 - collected below
                errs[i] = exc

        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        return barrier, threads, out, errs

    def _join(self, threads):
        for t in threads:
            t.join(timeout=JOIN_S)
            self.assertFalse(t.is_alive(), "worker thread hung")

    def _hook_wait(self, expect):
        """Fire when `expect` waiters have entered _readgate_wait."""
        entered = []
        lock = threading.Lock()
        all_in = threading.Event()
        orig = serve._readgate_wait

        def wrap(read_key, entry, waiter, read_fn):
            with lock:
                entered.append(waiter)
                if len(entered) >= expect:
                    all_in.set()
            return orig(read_key, entry, waiter, read_fn)

        serve._readgate_wait = wrap
        self.addCleanup(self._restore_wait, orig)
        return all_in

    def _restore_wait(self, orig):
        serve._readgate_wait = orig

    # C1: the gate key is the exact read-key set, never batch ids.
    def test_c1_key_matches_live_map(self):
        closed = batches.closure(self._full_list())
        live, _diag = batches.live_map_with_diag(closed)
        self.assertEqual(set(live), set(batches.read_keys(closed)))
        self.assertEqual(len(live), len(batches.read_keys(closed)))

    def test_c1_same_ids_changed_content_do_not_share(self):
        integ2 = Path(self.tmp.name) / "integ-extra"
        integ2.mkdir()
        base_a = {
            "id": "same-id", "repo": "fakerepo",
            "origin": {"checkout": str(self.origin),
                       "base_branch": "main", "base_head": "h"},
            "primaries": [], "integrations": [
                {"lane": "next", "worktree": str(self.integ),
                 "status": "x", "gates": []}],
            "policy": {},
        }
        only_a = [dict(base_a)]
        with_extra = [dict(base_a, integrations=list(
            base_a["integrations"]) + [{"lane": "aux",
                                        "worktree": str(integ2),
                                        "status": "x", "gates": []}])]
        key_a = frozenset(batches.read_keys(only_a))
        key_b = frozenset(batches.read_keys(with_extra))
        self.assertNotEqual(key_a, key_b)
        probe = _Probe(self)
        barrier, threads, out, errs = self._spawn(
            2, lambda i: serve._live_for_batches(
                only_a if i == 0 else with_extra))
        barrier.wait(timeout=WAIT_S)
        self._join(threads)
        self.assertEqual(errs, [None, None])
        # Different read sets: no sharing, one pass each.
        self.assertEqual(probe.calls, 2)
        self.assertIn("wt:" + str(self.integ), out[0][0]["same-id"])
        self.assertIn("wt:" + str(integ2), out[1][0]["same-id"])

    def test_c1b_overview_shares_snapshot_obs(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        # S8-b: overview performs no live reads (never touches the
        # readgate). It serves the batches snapshot's own summary bytes
        # and obs — the same numbers the reviews tab shows. The live
        # single-batch path (api_batch) still reads through the gate;
        # that contract is covered by the remaining C1 tests.
        probe = _Probe(self)
        snap = batches.read_json(batches.batches_snapshot_path(),
                                 default=None)
        self.assertIsInstance(snap, dict)
        out = serve.overview()
        listed = serve.api_batches({})
        self.assertEqual(out["review_batches"], snap["summary"])
        self.assertEqual(out["review_batches_obs"], snap["obs"])
        self.assertEqual(out["review_batches_at"], snap["at"])
        self.assertEqual(listed["refresh_obs"], snap["refresh_obs"])
        self.assertEqual(probe.calls, 0)

    def test_h_one_observation_across_screens(self):
        # S7 condition 1: one refresh pass stamps both snapshots with
        # one observation id, and the glance ready count equals the
        # reviews ready count.
        all_snap = batches.refresh_all_snapshots("test")
        self.assertTrue((all_snap.get("obs") or "").startswith("obs-"))
        self.assertEqual(all_snap["obs"], all_snap["batches"]["obs"])
        self.assertEqual(all_snap["obs"], all_snap["summary"]["obs"])
        glance = all_snap["summary"]["repos"][0]
        self.assertEqual(glance["name"], "fakerepo")
        reviews = all_snap["batches"]["summary"]["fakerepo"]
        self.assertEqual(
            (glance.get("pending_release") or {}).get("count"),
            reviews["ready"])
        self.assertEqual(reviews["ready"], 1)

    # (a) N concurrent same-key requests: at most 2 reads, identical results.
    def test_a_concurrent_same_key_two_reads_identical(self):
        n = 8
        probe = _Probe(self)
        probe.block()
        all_in = self._hook_wait(n - 1)
        barrier, threads, out, errs = self._spawn(
            n, lambda _i: serve._live_for_batches(self._full_list()))
        barrier.wait(timeout=WAIT_S)
        self.assertTrue(all_in.wait(timeout=WAIT_S),
                        "waiters never registered")
        probe.release.set()
        self._join(threads)
        self.assertEqual(errs, [None] * n)
        # Next-generation chaining: k serves the leader, k+1 serves every
        # waiter that arrived during k. Never N reads.
        self.assertEqual(probe.calls, 2)
        first = _sem_json(out[0])
        for i in range(1, n):
            self.assertEqual(_sem_json(out[i]), first)

    # (d) Sequential requests never reuse a result.
    def test_d_sequential_never_reuses(self):
        probe = _Probe(self)
        first = serve._live_for_batches(self._full_list())
        second = serve._live_for_batches(self._full_list())
        self.assertEqual(probe.calls, 2)
        self.assertEqual(_sem_json(first), _sem_json(second))

    # (b) Freshness: a change made after generation k started is seen by
    # a request that arrived after the change.
    def test_b_late_arrival_sees_post_change_source(self):
        probe = _Probe(self)
        probe.block()
        integ_key = "wt:" + str(self.integ)
        leader_out = []
        late_out = []
        commit_done = threading.Event()
        registered = self._hook_wait(1)

        def leader():
            leader_out.append(serve._live_for_batches(self._full_list()))

        def late():
            self.assertTrue(commit_done.wait(timeout=WAIT_S))
            late_out.append(serve._live_for_batches(self._full_list()))

        lt = threading.Thread(target=leader)
        lt.start()
        # Generation k's read completes first; only then change the source.
        self.assertTrue(probe.read_done.wait(timeout=WAIT_S))
        (self.integ / "f.txt").write_text("v3\n")
        commit_done.set()
        nt = threading.Thread(target=late)
        nt.start()
        # The late request is parked as a waiter while k is still blocked.
        self.assertTrue(registered.wait(timeout=WAIT_S),
                        "late request never registered")
        probe.release.set()
        for t in (lt, nt):
            t.join(timeout=JOIN_S)
            self.assertFalse(t.is_alive(), "worker thread hung")
        self.assertEqual(probe.calls, 2)
        old_fp = leader_out[0][0][self.batch_id][integ_key]["fingerprint"]
        new_fp = late_out[0][0][self.batch_id][integ_key]["fingerprint"]
        self.assertNotEqual(old_fp, new_fp)
        self.assertEqual(
            new_fp, runner.source_snapshot(self.integ)["fingerprint"])

    # (c) A failing (rc 124) generation fails closed for every waiter.
    def test_c_shared_rc124_fails_closed_identically(self):
        orig = batches._snapshot_status

        def fault(worktree):
            if str(self.integ) in (worktree or ""):
                return None, "source_unreadable", "tracked-list", 124
            return orig(worktree)

        batches._snapshot_status = fault
        self.addCleanup(self._restore_snapshot, orig)
        probe = _Probe(self)
        probe.block()
        all_in = self._hook_wait(3)
        # S7-a: the live single-batch path (record=True, readgate-shared)
        # stands in for the snapshotted list path here.
        barrier, threads, out, errs = self._spawn(
            4, lambda _i: serve.api_batch({"id": self.batch_id}))
        barrier.wait(timeout=WAIT_S)
        self.assertTrue(all_in.wait(timeout=WAIT_S),
                        "waiters never registered")
        probe.release.set()
        self._join(threads)
        self.assertEqual(errs, [None] * 4)
        for body in out:
            ev = body["evaluation"]
            self.assertEqual(ev["batch"], self.batch_id)
            self.assertEqual(ev["results"][0]["ready"], False)
        first_eval = _sem_json(out[0]["evaluation"])
        for i in range(1, 4):
            self.assertEqual(
                _sem_json(out[i]["evaluation"]), first_eval)
        self.assertEqual(probe.calls, 2)

    def _restore_snapshot(self, orig):
        batches._snapshot_status = orig

    # C3: record=True unchanged, yet one shared negative frame writes
    # exactly one readiness-negatives line (revision 65 dedupe).
    def test_c3_one_negative_line_under_sharing(self):
        orig = batches._snapshot_status

        def fault(worktree):
            if str(self.integ) in (worktree or ""):
                return None, "source_unreadable", "tracked-list", 124
            return orig(worktree)

        batches._snapshot_status = fault
        self.addCleanup(self._restore_snapshot, orig)
        probe = _Probe(self)
        probe.block()
        all_in = self._hook_wait(3)
        # S7-a: the negatives dedupe still runs on the live single-batch
        # path (record=True); the snapshotted list path records nothing.
        barrier, threads, out, errs = self._spawn(
            4, lambda _i: serve.api_batch({"id": self.batch_id}))
        barrier.wait(timeout=WAIT_S)
        self.assertTrue(all_in.wait(timeout=WAIT_S),
                        "waiters never registered")
        probe.release.set()
        self._join(threads)
        self.assertEqual(errs, [None] * 4)
        self.assertEqual(probe.calls, 2)
        lines_path = (Path(os.environ["OPENMAINTY_HOME"]) / "diagnostics"
                      / "readiness-negatives.jsonl")
        self.assertTrue(lines_path.is_file(), "no negatives file written")
        hits = [ln for ln in lines_path.read_text(
            encoding="utf-8").splitlines()
            if json.loads(ln).get("batch") == self.batch_id]
        self.assertEqual(len(hits), 1)
        rec = json.loads(hits[0])
        self.assertEqual(rec["rc"], 124)
        # Recovery: the next generation re-reads from scratch and is ready.
        batches._snapshot_status = orig
        body = serve.api_batch({"id": self.batch_id})
        self.assertEqual(body["evaluation"]["results"][0]["ready"], True)

    # (c2) A raising generation fails closed per waiter; next recovers.
    def test_c2_raising_generation_per_waiter_error_then_recovers(self):
        # Hold the first failure until the other three workers are
        # parked as waiters on the same entry (see _FailFirst).
        all_in = self._hook_wait(3)
        probe = _FailFirst(self, 2, lambda: RuntimeError("boom-r66"),
                           wait_before=all_in)
        barrier, threads, out, errs = self._spawn(
            4, lambda _i: serve._live_for_batches(self._full_list()))
        barrier.wait(timeout=WAIT_S)
        self._join(threads)
        raised = [e for e in errs if e is not None]
        self.assertEqual(len(raised), 4)
        by_type = {}
        for e in raised:
            by_type.setdefault(type(e).__name__, []).append(e)
        # k's leader and the elected k+1 leader re-raise the original;
        # the two remaining waiters get their own chained instances.
        self.assertEqual(len(by_type.get("RuntimeError", [])), 2)
        gate_errs = by_type.get("ReadGateError", [])
        self.assertEqual(len(gate_errs), 2)
        self.assertIsNot(gate_errs[0], gate_errs[1])
        for e in gate_errs:
            self.assertIsInstance(e.__cause__, RuntimeError)
        self.assertEqual(out, [None] * 4)
        # The next generation re-reads from scratch and succeeds.
        ok = serve._live_for_batches(self._full_list())
        self.assertEqual(probe.calls, 3)
        self.assertIn("wt:" + str(self.integ), ok[0][self.batch_id])

    # (e) Obs uniqueness per evaluation, identical semantics.
    def test_e_obs_unique_per_response_semantics_identical(self):
        barrier, threads, out, errs = self._spawn(
            4, lambda _i: serve.api_batches({}))
        barrier.wait(timeout=WAIT_S)
        self._join(threads)
        self.assertEqual(errs, [None] * 4)
        obs = [b["obs"] for b in out]
        self.assertEqual(len(set(obs)), 4)
        card_obs = [b["batches"][0]["obs"] for b in out]
        self.assertEqual(len(set(card_obs)), 4)
        first = _sem_json(out[0])
        for i in range(1, 4):
            self.assertEqual(_sem_json(out[i]), first)

    # C4: waiter deadline raises instead of reading on its own; the
    # unstarted generation never runs; the entry drains.
    def test_c4_waiter_deadline_no_fallback_read(self):
        old_budget = serve._READGATE_WAITER_BUDGET_S
        serve._READGATE_WAITER_BUDGET_S = 3
        self.addCleanup(self._restore_budget, old_budget)
        probe = _Probe(self)
        probe.block()
        all_in = self._hook_wait(2)
        barrier, threads, out, errs = self._spawn(
            3, lambda _i: serve._live_for_batches(self._full_list()))
        barrier.wait(timeout=WAIT_S)
        self.assertTrue(all_in.wait(timeout=WAIT_S),
                        "waiters never registered")
        # The leader stays blocked; join only lets the two waiters expire
        # (budget 3 s) while the leader thread is still parked.
        for t in threads:
            t.join(timeout=30)
        finished = [t for t in threads if not t.is_alive()]
        self.assertEqual(len(finished), 2)
        alive = [t for t in threads if t.is_alive()]
        self.assertEqual(len(alive), 1)
        timeouts = [e for e in errs if isinstance(e, serve.ReadGateTimeout)]
        self.assertEqual(len(timeouts), 2)
        self.assertIsNot(timeouts[0], timeouts[1])
        # Both waiters expired: release the leader; k publishes to nobody,
        # k+1 is never started, and nothing is retained.
        probe.release.set()
        alive[0].join(timeout=JOIN_S)
        self.assertFalse(alive[0].is_alive(), "leader thread hung")
        self.assertEqual(probe.calls, 1)
        with serve._READGATE_COND:
            self.assertEqual(serve._READGATE, {})

    def _restore_budget(self, old):
        serve._READGATE_WAITER_BUDGET_S = old

    # C4: overview never waits on the readgate, so a stalled live-read
    # generation can neither hang it nor empty its summary.
    def test_c4b_overview_ignores_blocked_gate(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        probe = _Probe(self)
        probe.block()
        leader_done = []
        lt = threading.Thread(
            target=lambda: leader_done.append(
                serve._live_for_batches(self._full_list())))
        lt.start()
        self.assertTrue(probe.started.wait(timeout=WAIT_S))
        out = []
        ot = threading.Thread(
            target=lambda: out.append(serve.overview()))
        ot.start()
        ot.join(timeout=JOIN_S)
        self.assertFalse(ot.is_alive(), "overview hung on a blocked gate")
        self.assertTrue(out[0]["review_batches"],
                        "overview lost its summary during a gate stall")
        probe.release.set()
        lt.join(timeout=JOIN_S)
        self.assertFalse(lt.is_alive(), "leader thread hung")
        self.assertEqual(probe.calls, 1)

    # C5: the shared pair is never mutated downstream, on any path.
    def test_c5_shared_pair_unmutated_all_paths(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        snapshots = []
        orig = batches.live_map_with_diag

        def snap(batch_list):
            live, diag = orig(batch_list)
            snapshots.append((copy.deepcopy(live), copy.deepcopy(diag),
                              live, diag))
            return live, diag

        batches.live_map_with_diag = snap
        self.addCleanup(self._restore_live_map, orig)
        serve.api_batches({})
        serve.api_batch({"id": self.batch_id})
        serve.api_work({"run": self.run_id})
        serve.overview()
        self.assertTrue(snapshots, "no reads observed")
        for before_live, before_diag, live, diag in snapshots:
            self.assertEqual(live, before_live)
            self.assertEqual(diag, before_diag)

    def _restore_live_map(self, orig):
        batches.live_map_with_diag = orig

    # (g) Real ThreadingHTTPServer on an ephemeral port, N clients.
    def test_g_http_burst_serves_snapshot_without_reads(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        # S7-a: the list path serves the precomputed file (written by the
        # import refresh during seeding), so a burst of 8 performs zero
        # live reads; responses stay unique-obs with identical semantics.
        n = 8
        probe = _Probe(self)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        port = httpd.server_port
        st = threading.Thread(target=httpd.serve_forever)
        st.start()
        self.addCleanup(self._close_http, httpd, st)

        def get(path):
            with urlopen("http://127.0.0.1:%d%s" % (port, path),
                         timeout=JOIN_S) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        barrier, threads, out, errs = self._spawn(
            n, lambda _i: get("/api/batches"))
        barrier.wait(timeout=WAIT_S)
        self._join(threads)
        self.assertEqual(errs, [None] * n)
        for status, _body in out:
            self.assertEqual(status, 200)
        self.assertEqual(probe.calls, 0)
        bodies = [b for _s, b in out]
        self.assertEqual(len({b["obs"] for b in bodies}), n)
        first = _sem_json(bodies[0])
        for i in range(1, n):
            self.assertEqual(_sem_json(bodies[i]), first)
        # The other three paths stay 200 on the same fixture.
        self.assertEqual(get("/api/overview")[0], 200)
        self.assertEqual(
            get("/api/batch?id=" + self.batch_id)[0], 200)
        qa = get("/api/work?run=" + self.run_id + "&id=")
        self.assertEqual(qa[0], 200)
        self.assertEqual(qa[1]["current_qa"]["state"], "ready")

    def _close_http(self, httpd, thread):
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=JOIN_S)


if __name__ == "__main__":
    unittest.main()
