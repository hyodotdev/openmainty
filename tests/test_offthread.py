"""S14 off-thread refresh + disk meter tests.

POST /api/m2/policy and POST /api/repo/profile return at once with
the new state; the summary refresh lands in the background
(single-flight: two quick flips cause one extra refresh at most).
/api/disk answers from cache and recomputes off-thread.
Py3.9 portable.
"""
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, repos, sched, serve  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


class OffThreadFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})
        self._real_refresh = batches.refresh_summary_snapshot
        self.addCleanup(self.drain_refresh)

    def tearDown(self):
        batches.refresh_summary_snapshot = self._real_refresh

    def drain_refresh(self):
        for _ in range(200):
            with serve._REFRESH_LOCK:
                idle = not serve._REFRESH_RUNNING
            if idle:
                return
            time.sleep(0.1)

    def titles(self):
        from openmainty import events
        return [e.get("title") for e in events.list_events("fakerepo")]


class RefreshSoonTests(OffThreadFixture):
    def test_returns_at_once_while_refresh_lands(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow(reason):
            calls.append(reason)
            started.set()
            release.wait(15)

        batches.refresh_summary_snapshot = slow
        t0 = time.time()
        self.assertEqual(serve.refresh_summary_soon("t1"), "started")
        elapsed = time.time() - t0
        self.assertLess(elapsed, 2.0)
        self.assertTrue(started.wait(10))
        release.set()
        self.drain_refresh()
        self.assertEqual(calls, ["t1"])

    def test_two_quick_flips_cause_one_extra_refresh(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow(reason):
            calls.append(reason)
            started.set()
            release.wait(15)

        batches.refresh_summary_snapshot = slow
        self.assertEqual(serve.refresh_summary_soon("first"), "started")
        self.assertTrue(started.wait(10))
        # A worker is inside the first refresh: this queues, never
        # starts a second worker.
        self.assertEqual(serve.refresh_summary_soon("second"), "queued")
        release.set()
        self.drain_refresh()
        self.assertEqual(calls, ["first", "second"])

    def test_queued_all_survives_summary(self):
        # N1: while the worker is inside a refresh, queue ("all",
        # hold) and then ("summary", policy) — the follow-up pass
        # must still run refresh_all_snapshots, never a downgraded
        # summary-only pass that would lose the hold's envelope.
        started = threading.Event()
        release = threading.Event()
        all_calls = []
        sum_calls = []

        def slow_sum(reason):
            sum_calls.append(reason)
            started.set()
            release.wait(15)

        def quick_all(reason):
            all_calls.append(reason)

        real_all = batches.refresh_all_snapshots
        batches.refresh_all_snapshots = quick_all
        try:
            batches.refresh_summary_snapshot = slow_sum
            self.assertEqual(serve.refresh_summary_soon("first"),
                             "started")
            self.assertTrue(started.wait(10))
            self.assertEqual(serve.refresh_all_soon("hold"), "queued")
            self.assertEqual(
                serve.refresh_summary_soon("m2-policy"), "queued")
            release.set()
            self.drain_refresh()
        finally:
            batches.refresh_all_snapshots = real_all
        self.assertEqual(sum_calls, ["first"])
        self.assertEqual(all_calls, ["m2-policy"])

    def test_policy_flip_returns_fast_with_new_state(self):
        from openmainty import m2 as _m2
        calls = []
        release = threading.Event()

        def slow(reason):
            calls.append(reason)
            release.wait(15)

        batches.refresh_summary_snapshot = slow
        t0 = time.time()
        body = serve.api_m2_policy({"repo": "fakerepo",
                                    "stage": "prepare", "on": True})
        elapsed = time.time() - t0
        self.assertLess(elapsed, 2.0)
        self.assertTrue(body["policy"]["prepare"])
        self.assertIn(body.get("refresh"), ("started", "queued"))
        self.assertNotIn("refresh_ms", body)
        release.set()
        self.drain_refresh()
        self.assertTrue(calls)


class DiskApiTests(OffThreadFixture):
    def setUp(self):
        super().setUp()
        serve._DISK_SPAWNED_AT = 0.0
        serve._DISK_PROC = None
        serve._DISK_TMP = None
        serve._DISK_LAST_RC = None
        self.addCleanup(self._restore_disk_state)

    def _restore_disk_state(self):
        proc = serve._DISK_PROC
        serve._DISK_PROC = None
        serve._DISK_TMP = None
        serve._DISK_SPAWNED_AT = 0.0
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001 - test hygiene only
                pass

    def test_no_cache_shows_free_and_computing(self):
        # S17/D1+D2: no cache -> free_bytes live, stale_s null,
        # computing true. The spawn is mocked: no fork in this test.
        (self.home / "cache").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(serve, "_spawn_disk_walk",
                               return_value=False) as sp:
            t0 = time.time()
            out = serve.api_disk({})
            self.assertLess(time.time() - t0, 2.0)
        self.assertGreater(int(out["free_bytes"]), 0)
        self.assertIsNone(out["home_bytes"])
        self.assertIsNone(out["stale_s"])
        self.assertTrue(out["computing"])
        self.assertEqual(out["note"], "계산 중")
        self.assertEqual(sp.call_count, 1)

    def test_disk_single_flight(self):
        # S17/D3: two requests start exactly one out-of-process walk.
        (self.home / "cache").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(serve, "_spawn_disk_walk",
                               return_value=True) as sp:
            serve.api_disk({})
            serve.api_disk({})
        self.assertEqual(sp.call_count, 1)

    def test_walk_lands_cache_out_of_process(self):
        # S17r/D3: the real spawn (Popen fork+exec, never
        # fork-without-exec) lands the cache with no thread of ours —
        # the request never blocks on the walk.
        before = threading.active_count()
        (self.home / "cache").mkdir(parents=True, exist_ok=True)
        (self.home / "f.txt").write_text("hello", encoding="utf-8")
        self.assertTrue(serve._spawn_disk_walk())
        deadline = time.time() + 30
        landed = None
        while time.time() < deadline:
            out = serve.api_disk({})
            if out["home_bytes"] is not None and not out["computing"]:
                landed = out
                break
            time.sleep(0.2)
        self.assertIsNotNone(landed, "out-of-process walk never landed")
        self.assertGreaterEqual(int(landed["home_bytes"]), 0)
        self.assertLess(int(landed["stale_s"]), 120)
        self.assertEqual(threading.active_count(), before)
        # Parsed in-process and cleaned up: no tmp left behind.
        leftovers = list((self.home / "cache").glob("*.du.tmp"))
        self.assertEqual(leftovers, [])

    def test_walk_timeout_kills_and_records(self):
        # S17r: an overdue walk is SIGKILLed, the error is cached at
        # once, and the handle is reaped (returncode set, no zombie).
        (self.home / "cache").mkdir(parents=True, exist_ok=True)
        self.assertTrue(serve._spawn_disk_walk(argv=["sleep", "5"],
                                               timeout_s=1))
        deadline = time.time() + 30
        done = None
        while time.time() < deadline:
            out = serve.api_disk({})
            if out["note"] == "크기 계산 실패" and not out["computing"] \
                    and serve._DISK_PROC is None \
                    and serve._DISK_LAST_RC == -9:
                done = out
                break
            time.sleep(0.2)
        self.assertIsNotNone(done, "timeout never recorded and reaped")
        self.assertGreater(int(done["free_bytes"]), 0)

    def test_failure_note_keeps_free(self):
        # A failed walk still shows live free space with a plain
        # failure note — never "계산 중" forever.
        import openmainty.gc as _gc
        (self.home / "cache").mkdir(parents=True, exist_ok=True)
        _gc.write_disk_cache(self.home, home_bytes=None,
                             error="du failed")
        out = serve.api_disk({})
        self.assertGreater(int(out["free_bytes"]), 0)
        self.assertIsNone(out["home_bytes"])
        self.assertEqual(out["note"], "크기 계산 실패")
        self.assertFalse(out["computing"])


class _FakeWalkProc:
    """Finished-walk stand-in: poll aligns threads on a barrier so
    both enter the reap together, then reports the fixed rc."""

    def __init__(self, rc, barrier=None):
        self.returncode = rc
        self._barrier = barrier

    def poll(self):
        if self._barrier is not None:
            try:
                self._barrier.wait(timeout=15)
            except threading.BrokenBarrierError:
                pass
        return self.returncode

    def kill(self):
        self.returncode = -9


class DiskReapTests(DiskApiTests):
    """S18c: F2 (one reap under the lock) and F3 (rc=1 partial)."""

    def start_finished_walk(self, rc, total="12345\t/home\n",
                            barrier=None):
        (self.home / "cache").mkdir(parents=True, exist_ok=True)
        tmp = self.home / "cache" / "disk.du.tmp"
        tmp.write_text(total, encoding="utf-8")
        serve._DISK_TMP = tmp
        serve._DISK_PROC = _FakeWalkProc(rc, barrier)
        serve._DISK_DEADLINE = time.time() + 600
        serve._DISK_SPAWNED_AT = time.time()

    def read_cache(self):
        import json as _json
        return _json.loads(
            (self.home / "cache" / "disk.json").read_text(
                encoding="utf-8"))

    def test_f2_concurrent_reap_writes_once(self):
        # Two requests reap one finished walk together: exactly one
        # cache write, keeping the good home_bytes. Without the
        # lock the second sees no tmp and overwrites it with an
        # error for 30 minutes.
        import openmainty.gc as _gc
        barrier = threading.Barrier(2)
        self.start_finished_walk(0, barrier=barrier)
        writes = []
        real_write = _gc.write_disk_cache

        def spy(home, home_bytes=None, error=None, at=None):
            writes.append((home_bytes, error))
            return real_write(home, home_bytes=home_bytes,
                              error=error, at=at)

        with mock.patch.object(_gc, "write_disk_cache",
                               side_effect=spy):
            ts = [threading.Thread(target=serve._poll_disk_walk)
                  for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(15)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0],
                         (12345 * 1024, None))
        self.assertEqual(self.read_cache()["home_bytes"],
                         12345 * 1024)

    def test_f3_partial_sum_kept_with_note(self):
        # du rc=1 (some entry unreadable) still prints a total: keep
        # it as a partial sum with an honest note, never a failure.
        self.start_finished_walk(1)
        serve._poll_disk_walk()
        cached = self.read_cache()
        self.assertEqual(cached["home_bytes"], 12345 * 1024)
        self.assertEqual(cached["error"], "일부 항목 읽기 실패(부분 합계)")

    def test_f3_unparseable_output_fails(self):
        self.start_finished_walk(2, total="du: cannot read\n")
        serve._poll_disk_walk()
        cached = self.read_cache()
        self.assertIsNone(cached["home_bytes"])
        self.assertEqual(cached["error"], "du rc=2")


class HelperCleanupWaitsTests(unittest.TestCase):
    """M2.16b2: use_temp_home cleanup joins a running refresh
    before HOME is restored and before the temp dir is removed.

    Since M2.16b the snapshot writers resolve the home once when
    they start, so a refresh still running at teardown would
    write into the temp home while TemporaryDirectory.cleanup
    removes it (Errno 66) — or leak into the restored home, as
    the 30 fakerepo tmps in the real home did.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))

    def test_cleanup_waits_for_slow_refresh(self):
        started = threading.Event()
        release = threading.Event()
        marks = []

        def slow(reason):
            marks.append(("refresh-start",
                          os.environ.get("OPENMAINTY_HOME")))
            started.set()
            release.wait(15)
            marks.append(("refresh-end",
                          os.environ.get("OPENMAINTY_HOME"),
                          Path(self.tmp.name).is_dir()))

        real = batches.refresh_all_snapshots
        batches.refresh_all_snapshots = slow
        try:
            self.assertEqual(serve.refresh_all_soon("t-wait"),
                             "started")
            self.assertTrue(started.wait(10))
            timer = threading.Timer(0.5, release.set)
            timer.start()
            self.doCleanups()
            timer.join()
        finally:
            batches.refresh_all_snapshots = real
        ends = [m for m in marks if m[0] == "refresh-end"]
        self.assertEqual(len(ends), 1)
        # The worker finished while the temp home was still the
        # current HOME and still on disk: the wait ran before
        # HOME restore and before tmp.cleanup removed the dir.
        self.assertEqual(ends[0][1], str(self.home))
        self.assertTrue(ends[0][2])
        self.assertFalse(Path(self.tmp.name).exists())


class DashboardS14Tests(unittest.TestCase):
    def test_probe_label_says_global(self):
        # S16: the probe runs --version with cwd = the registered
        # checkout (read-only), so the label promises the pinned
        # project version — the S14 "전역 버전" wording is gone.
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        self.assertIn("등록된 체크아웃에서", src)
        self.assertIn("프로젝트 고정 버전", src)
        self.assertNotIn("전역 버전이며", src)

    def test_disk_meter_wired(self):
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        self.assertIn("/api/disk", src)
        self.assertIn("diskmeter", src)


if __name__ == "__main__":
    unittest.main()
