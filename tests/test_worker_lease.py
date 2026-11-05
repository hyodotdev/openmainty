"""Single worker lease across all entry points.

Isolated fixtures only (temp OPENMAINTY_HOME, FakeExecutor, stubbed gh).
Contention is always proven with a SEPARATE PROCESS holding the real OS
lease — never a second real model worker, never the live repos.

Covers: CLI resume/start refusal while busy (zero worker calls, run
byte-identical), managed answers saved durably + same-run queued while
busy and resumed when free, scheduler reentrancy (no self-deadlock),
nested release retention (inner release keeps the OS lease until the
outer releases, proven by a separate process), two contenders never
overlapping, crash release, and the terminal duplicate/info guards
still holding under the new boundary.

Run with: python3 -m unittest tests.test_worker_lease
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import cli, pcconfig, repos, runner, sched, serve, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

PROFILE = {
    "name": "wl",
    "github": "example/wl",
    "baseBranch": "main",
    "knowledge": [],
    "checks": [{"name": "unit", "cmd": ["make", "unit"], "requires": []}],
    "e2e": [],
    "release": {"notes": "CHANGELOG.md"},
    "announce": {"medium": False, "discord": False, "slack": False},
}

SRC = str(Path(__file__).resolve().parent.parent / "src")


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    use_temp_home(test, str(Path(tmp.name) / "home"))
    return Path(tmp.name)


def make_repo(test, root):
    repos.save(dict(PROFILE))
    co = root / "co"
    co.mkdir(exist_ok=True)
    (co / "README.md").write_text("# fake\n")
    pc = pcconfig.load()
    pc["checkouts"]["wl"] = str(co)
    pcconfig.save(pc)
    patcher = mock.patch("openmainty.cli._open_issues", return_value=[])
    patcher.start()
    test.addCleanup(patcher.stop)
    return co


class LeaseCase(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = make_repo(self, self.root)
        self._saved_meta = os.environ.pop("META_API_KEY", None)
        self.addCleanup(self._restore_meta)
        # Offline hermeticity: these tests verify lease/queue guard
        # codes, not the subscription gate. The gate reads the ambient
        # `muse` CLI / login state and would 403 under a bare env, so
        # it stays open here (no test in this module asserts it).
        from openmainty import chat as chat_mod
        gate = mock.patch.object(chat_mod, "subscription_error",
                                 return_value=None)
        gate.start()
        self.addCleanup(gate.stop)
        self.ex = runner.FakeExecutor()
        patcher = mock.patch("openmainty.runner.make_executor",
                             lambda name, model=None, cancel_event=None: self.ex)
        patcher.start()
        self.addCleanup(patcher.stop)
        serve._JOBS.clear()
        serve._JOB_CANCEL.clear()
        self.addCleanup(sched._release_global_lock, None)
        self.addCleanup(lambda: sched._thread_owners().clear())
        self.holder = None
        self.addCleanup(self._kill_holder)

    def _restore_meta(self):
        if self._saved_meta is not None:
            os.environ["META_API_KEY"] = self._saved_meta
        else:
            os.environ.pop("META_API_KEY", None)

    def _kill_holder(self):
        if self.holder is not None:
            try:
                self.holder.kill()
            except OSError:
                pass
            try:
                self.holder.wait(timeout=15)
            except Exception:  # noqa: BLE001
                pass
            self.holder = None

    # -- contention primitive: a SEPARATE PROCESS holds the real lease --
    #
    # Handshake (no timing guess): the child retries its acquire
    # boundedly and writes a ready file ONLY after a real OS acquire,
    # then sleeps. The parent waits for that file while watching for
    # early child death, then performs ONE decisive contention probe.
    # The old shape (one-shot child acquire racing a parent probe
    # loop) could land the child inside a probe-held window and fail
    # the child assert while the parent then timed out waiting busy —
    # a fixture readiness race, not a lease failure. Probing only
    # after proven readiness removes the race without weakening any
    # assertion: separate process, real OS lease, busy still proven
    # by a failed acquire, cleanup unchanged.

    def _start_holder(self, name="holder", sleep_s=60):
        home = os.environ["OPENMAINTY_HOME"]
        ready = self.root / ("holder-ready-%s" % name)
        errpath = self.root / ("holder-error-%s" % name)
        for stale in (ready, errpath):
            try:
                stale.unlink()
            except OSError:
                pass
        # Real multiline script (compound statements cannot follow
        # a semicolon): compiled here so a syntax slip surfaces
        # without spawning anything.
        script = (
            "import sys, time, traceback\n"
            "sys.path.insert(0, %r)\n"
            "from openmainty import sched\n"
            "ready, errpath, name, sleep_s = sys.argv[1:5]\n"
            "token = None\n"
            "end = time.time() + 15\n"
            "while token is None and time.time() < end:\n"
            "    token = sched.acquire_worker_lease(name)\n"
            "    if token is None:\n"
            "        time.sleep(0.05)\n"
            "try:\n"
            "    assert token is not None,"
            " 'holder never acquired the lease'\n"
            "    open(ready, 'w').write('ready')\n"
            "    time.sleep(int(sleep_s))\n"
            "except BaseException:\n"
            "    open(errpath, 'w').write(traceback.format_exc())\n"
            "    raise SystemExit(1)\n" % SRC)
        compile(script, "<lease-holder>", "exec")
        proc = subprocess.Popen(
            ["python3", "-c", script, str(ready), str(errpath), name,
             str(sleep_s)],
            env={"PATH": "/usr/bin:/bin", "OPENMAINTY_HOME": home})
        try:
            end = time.time() + 15
            while time.time() < end:
                if ready.is_file():
                    break
                if proc.poll() is not None:
                    detail = ""
                    try:
                        detail = errpath.read_text()[-800:]
                    except OSError:
                        pass
                    raise AssertionError(
                        "holder process died before signalling "
                        "readiness: " + (detail or "no error recorded"))
                time.sleep(0.02)
            else:
                raise AssertionError(
                    "holder process did not take the lease in time")
            # Decisive single probe: the holder provably owns the real
            # OS lease, so a successful acquire here fails loudly (and
            # is released, never leaked).
            token = sched.acquire_worker_lease("probe-handshake")
            if token is not None:
                sched.release_worker_lease(token)
                raise AssertionError(
                    "lease acquirable while holder proven live")
        except BaseException:
            # Every failure exit reaps the unowned proc (callers only
            # adopt it on success); the child's own error file keeps
            # the details.
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001 - reap is best-effort
                pass
            raise
        return proc

    def hold_lease_elsewhere(self, name="holder", sleep_s=60):
        self.holder = self._start_holder(name=name, sleep_s=sleep_s)

    def _lease_free(self):
        owner = sched.acquire_worker_lease("probe")
        if owner is None:
            return False
        sched.release_worker_lease(owner)
        return True

    def _wait_free(self, timeout_s=15):
        end = time.time() + timeout_s
        while time.time() < end:
            if self._lease_free():
                return True
            time.sleep(0.2)
        return False

    def _run_bytes(self, run_id):
        return Path(store.run_file(run_id)).read_bytes()

    def _make_waiting_run(self):
        """Question-waiting run via the real worker-step path (no lease)."""
        state = store.create("wl", "7", "t", "base0", {})
        work = self.root / "askwork"
        work.mkdir(exist_ok=True)
        ask = runner.FakeExecutor(
            script={"triage": [(0, "notes\nOPENMAINTY_ASK: which version?")]})
        self.assertFalse(runner.run_worker_step(state, PROFILE, "triage",
                                                ask, work, "body"))
        self.assertTrue(state["waitReason"].startswith("question:"))
        return store.load(state["id"])

    def _manage(self, state, key="wl#issue-7", status="waiting-review"):
        """Wire a run to the scheduler queue (fixture wiring only)."""
        state["queue_key"] = key
        store.save(state)
        with sched.locked("wl"):
            st = sched.load("wl")
            st["enabled"] = True
            st["queue"].append({"kind": "issue-triage", "repo": "wl",
                                "issue": "7", "key": key, "status": status,
                                "attempts": 0, "next_retry_at": None,
                                "run_id": state["id"],
                                "created_at": "2026-10-18T00:00:00+00:00"})
            sched.save(st)

    # -- CLI resume/start refusal --

    def test_cli_resume_refuses_busy_with_zero_calls(self):
        state = store.create("wl", "7", "t", "base0", {})
        before = self._run_bytes(state["id"])
        self.hold_lease_elsewhere()
        ns = argparse.Namespace(run=state["id"], executor="fake",
                                model=None, only=None)
        rc = cli.cmd_resume(ns)
        self.assertEqual(rc, cli.WORKER_BUSY_EXIT)
        self.assertEqual(self.ex.calls, [])
        self.assertEqual(self._run_bytes(state["id"]), before)

    def test_cli_start_refuses_busy_without_creating(self):
        before = {r["id"] for r in store.list_runs(200)}
        self.hold_lease_elsewhere()
        ns = argparse.Namespace(repo="wl", issue="8", title="t", body="b",
                                body_file=None, allow_duplicate=True,
                                only=None, executor="fake", model=None)
        rc, run_id = cli._start_run_with(ns, self.ex)
        self.assertEqual(rc, cli.WORKER_BUSY_EXIT)
        self.assertIsNone(run_id)
        self.assertEqual(self.ex.calls, [])
        self.assertEqual({r["id"] for r in store.list_runs(200)}, before)

    def test_cross_thread_refusal_same_process(self):
        state = store.create("wl", "7", "t", "base0", {})
        before = self._run_bytes(state["id"])
        acquired = threading.Event()
        release = threading.Event()
        box = {}

        def _hold():
            owner = sched.acquire_worker_lease("holder-thread")
            box["owner"] = owner
            acquired.set()
            release.wait(timeout=30)
            if owner is not None:
                sched.release_worker_lease(owner)

        th = threading.Thread(target=_hold, daemon=True)
        th.start()
        self.assertTrue(acquired.wait(timeout=15))
        self.assertIsNotNone(box.get("owner"))
        try:
            ns = argparse.Namespace(run=state["id"], executor="fake",
                                    model=None, only=None)
            rc = cli.cmd_resume(ns)
        finally:
            release.set()
            th.join(timeout=15)
        self.assertEqual(rc, cli.WORKER_BUSY_EXIT)
        self.assertEqual(self.ex.calls, [])
        self.assertEqual(self._run_bytes(state["id"]), before)

    # -- managed answers: saved + same-run queued while busy --

    def test_answer_saved_and_queued_while_busy(self):
        state = self._make_waiting_run()
        self._manage(state)
        run_id = state["id"]
        self.hold_lease_elsewhere()
        out = serve.api_job_answer({"run": run_id, "answer": "v2"})
        self.assertTrue(out["saved"])
        self.assertFalse(out["started"])
        self.assertTrue(out["queued"])
        self.assertIn("job", out)
        self.assertEqual(self.ex.calls, [])
        # Durable answer on the SAME run; same key/run queued, no fork.
        again = store.load(run_id)
        self.assertEqual(again["answers"], [{"q": "which version?", "a": "v2"}])
        self.assertIsNone(again.get("waitReason"))
        st = sched.load("wl")
        items = [i for i in st["queue"] if i.get("key") == "wl#issue-7"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "queued")
        self.assertEqual(items[0]["run_id"], run_id)
        self.assertEqual({r["id"] for r in store.list_runs(200)}, {run_id})
        # Lease free again: the SAME run resumes to completion.
        self._kill_holder()
        self.assertTrue(self._wait_free())
        ns = argparse.Namespace(run=run_id, executor="fake",
                                model=None, only=None)
        self.assertEqual(cli.cmd_resume(ns), 0)
        done = store.load(run_id)
        self.assertTrue(all(e["status"] == "done"
                            for e in done["steps"].values()))
        self.assertEqual({r["id"] for r in store.list_runs(200)}, {run_id})

    def test_api_resume_refuses_busy_without_job(self):
        state = store.create("wl", "7", "t", "base0", {})
        before = self._run_bytes(state["id"])
        self.hold_lease_elsewhere()
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_job_resume({"run": state["id"]})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(serve._JOBS, {})
        self.assertEqual(self.ex.calls, [])
        self.assertEqual(self._run_bytes(state["id"]), before)

    def test_requeue_never_duplicates_or_forks(self):
        state = self._make_waiting_run()
        self._manage(state)
        run_id = state["id"]
        store.record_answer(run_id, "v2")
        first = sched.requeue_run_for_dispatch("wl", run_id)
        self.assertTrue(first["requeued"])
        second = sched.requeue_run_for_dispatch("wl", run_id)
        self.assertTrue(second["requeued"])
        st = sched.load("wl")
        items = [i for i in st["queue"] if i.get("key") == "wl#issue-7"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["run_id"], run_id)
        # Foreign/unknown/cancelled items are never touched.
        other = store.create("wl", "9", "t", "base0", {})
        self.assertFalse(sched.requeue_run_for_dispatch("wl", other["id"])["requeued"])
        self.assertFalse(sched.requeue_run_for_dispatch("wl", "run-deadbeef")["requeued"])
        with sched.locked("wl"):
            st = sched.load("wl")
            target = next(i for i in st["queue"] if i.get("key") == "wl#issue-7")
            target["status"] = "cancelled"
            sched.save(st)
        res = sched.requeue_run_for_dispatch("wl", run_id)
        self.assertFalse(res["requeued"])

    # -- scheduler-owned execution stays valid and reentrant --

    def test_scheduler_nested_resume_no_self_deadlock(self):
        state = store.create("wl", "7", "t", "base0", {})
        run_id = state["id"]
        fetched = {"title": "t", "body": "b", "state": "OPEN", "url": "",
                   "error": None, "provenance": "test",
                   "fetched_at": "2026-10-18T00:00:00+00:00"}
        patcher = mock.patch("openmainty.cli.fetch_issue_content",
                             return_value=dict(fetched))
        patcher.start()
        self.addCleanup(patcher.stop)
        with sched.locked("wl"):
            st = sched.load("wl")
            st["enabled"] = True
            st["queue"].append({"kind": "issue-triage", "repo": "wl",
                                "issue": "7", "key": "wl#issue-7",
                                "status": "queued", "attempts": 2,
                                "next_retry_at": None, "run_id": run_id,
                                "created_at": "2026-10-18T00:00:00+00:00"})
            sched.save(st)
        out = sched.run_due("wl", self.ex, key="wl#issue-7")
        self.assertTrue(out.get("ran"))
        # Same run resumed in place (no fork), attempts preserved,
        # lease fully released with no nesting residue.
        st = sched.load("wl")
        item = next(i for i in st["queue"] if i.get("key") == "wl#issue-7")
        self.assertEqual(item.get("run_id"), run_id)
        self.assertEqual({r["id"] for r in store.list_runs(200)}, {run_id})
        self.assertFalse(sched.worker_lease_held())
        self.assertEqual(sched._thread_owners(), [])
        self.assertTrue(self._lease_free())

    def test_nested_release_keeps_lease_until_outer(self):
        home = os.environ["OPENMAINTY_HOME"]
        env = {"PATH": "/usr/bin:/bin", "OPENMAINTY_HOME": home}
        script = ("import sys; sys.path.insert(0, %r);"
                  "from openmainty import sched;"
                  "print('ACQUIRED' if sched.acquire_worker_lease('child')"
                  " is not None else 'BUSY')" % SRC)

        def _child_verdict():
            out = subprocess.run(["python3", "-c", script],
                                 capture_output=True, text=True,
                                 env=env, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout.strip()

        outer = sched.acquire_worker_lease("outer")
        self.assertIsNotNone(outer)
        try:
            inner = sched.acquire_worker_lease("inner")
            self.assertEqual(inner, outer)
            sched.release_worker_lease(inner)
            self.assertTrue(sched.worker_lease_held())
            # Outer scope still held: a separate process must refuse.
            self.assertEqual(_child_verdict(), "BUSY")
        finally:
            sched.release_worker_lease(outer)
        self.assertFalse(sched.worker_lease_held())
        self.assertEqual(sched._thread_owners(), [])
        # Outer released: a separate process may now acquire.
        self.assertEqual(_child_verdict(), "ACQUIRED")

    # -- cross-process mutual exclusion and crash release --

    def test_two_contenders_never_both_execute(self):
        script = ("import sys, time; sys.path.insert(0, %r);"
                  "from openmainty import sched;"
                  "w = sched.acquire_worker_lease('racer');"
                  "print('WON' if w is not None else 'LOST', flush=True);"
                  "sys.stdout.flush();"
                  "time.sleep(5 if w is not None else 0);"
                  % SRC)
        home = os.environ["OPENMAINTY_HOME"]
        env = {"PATH": "/usr/bin:/bin", "OPENMAINTY_HOME": home}
        procs = [subprocess.Popen(["python3", "-c", script],
                                  stdout=subprocess.PIPE, env=env)
                 for _ in range(2)]
        try:
            outs = [p.communicate(timeout=30)[0].decode().strip()
                    for p in procs]
        finally:
            for p in procs:
                try:
                    p.kill()
                except OSError:
                    pass
        self.assertEqual(sorted(outs), ["LOST", "WON"])

    def test_owner_crash_releases_lease(self):
        holder = self._start_holder("doomed", sleep_s=60)
        try:
            holder.kill()
            holder.wait(timeout=15)
            holder = None
            self.assertTrue(self._wait_free(timeout_s=15))
        finally:
            if holder is not None:
                try:
                    holder.kill()
                except OSError:
                    pass

    # -- terminal guards still hold under the new boundary --

    def test_duplicate_and_waiting_guards_hold(self):
        from openmainty import runner as _runner
        dup = store.create("wl", "7", "dup", "base0", {})
        _runner.set_disposition(dup, "DUPLICATE", detail="dup of #1", ref="#1")
        dup["waitReason"] = "duplicate: #1"
        store.save(dup)
        waiting = store.create("wl", "8", "w", "base0", {})
        _runner.set_disposition(waiting, "INFO-NEEDED", detail="need version")
        waiting["waitReason"] = "question: which version?"
        store.save(waiting)
        for run_id in (dup["id"], waiting["id"]):
            ns = argparse.Namespace(run=run_id, executor="fake",
                                    model=None, only=None)
            self.assertEqual(cli.cmd_resume(ns), 1)
        # Zero worker calls for both; waits preserved (only a bounded
        # gate intervention is appended, no step or attempt advances).
        self.assertEqual(self.ex.calls, [])
        dup_after = store.load(dup["id"])
        waiting_after = store.load(waiting["id"])
        self.assertTrue(dup_after["waitReason"].startswith("duplicate:"))
        self.assertTrue(waiting_after["waitReason"].startswith("question:"))
        self.assertTrue(all(e["status"] == "pending"
                            for e in dup_after["steps"].values()))
        self.assertTrue(all(e["status"] == "pending"
                            for e in waiting_after["steps"].values()))

    def test_serve_answer_guards_unchanged(self):
        state = store.create("wl", "7", "t", "base0", {})
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_job_answer({"run": state["id"], "answer": "x"})
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_job_answer({"run": state["id"], "answer": " "})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_job_answer({"run": "run-deadbeef", "answer": "x"})
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
