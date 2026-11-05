"""Scheduler due-time race (S5-A): tick-anchored next_scan + tolerance.

A launchd StartInterval tick can start seconds BEFORE a repo's next_scan
(which used to be anchored to the previous scan's completion +
interval), skipping the repo for a whole interval. due_repos() treats a
repo as due within a small tolerance of the tick start, and each scan
anchors the next one to the tick start.

Offline only: fixture sched/*.json state under a temp OPENMAINTY_HOME,
explicit now= strings (no sleeping), and a stubbed scan for the tick
wiring test. No registry, no daemon, no live repos.

Run with: python3 -m unittest tests.test_sched_due
"""
import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import sched  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

I = 1800
T0 = datetime.datetime(2026, 10, 20, 12, 0, 0,
                       tzinfo=datetime.timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def write_state(home, repo, **kw):
    d = home / "sched"
    d.mkdir(parents=True, exist_ok=True)
    st = {"repo": repo, "enabled": True, "execute_enabled": False,
          "interval_s": I, "last_scan": None, "next_scan": None,
          "heartbeat": None, "current_job": None, "stale": False,
          "error": None, "scan_ok": True, "sources": {},
          "backoff_s": 0, "failures": 0, "queue": [], "history": []}
    st.update(kw)
    (d / f"{repo}.json").write_text(json.dumps(st), encoding="utf-8")


class DueRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))

    def test_early_tick_catches_repo(self):
        # Legacy completion-anchored state: previous scan
        # completed 1795s ago, next_scan 5s in the future. The old
        # `nxt <= now` rule skipped it for a whole interval; the tick
        # start tolerance catches it.
        t0 = iso(T0)
        write_state(self.home, "r",
                    last_scan=iso(T0 - datetime.timedelta(seconds=1795)),
                    next_scan=iso(T0 + datetime.timedelta(seconds=5)))
        self.assertEqual(sched.due_repos(now=t0), ["r"])

    def test_far_future_not_due(self):
        t0 = iso(T0)
        write_state(self.home, "r",
                    last_scan=iso(T0 - datetime.timedelta(seconds=1700)),
                    next_scan=iso(T0 + datetime.timedelta(seconds=61)))
        self.assertEqual(sched.due_repos(now=t0), [])

    def test_back_to_back_tick_skips(self):
        # Repo scanned 30s ago; a second tick right after must not
        # relaunch it (launches stay >= interval - tol apart).
        t0 = iso(T0)
        last = T0 - datetime.timedelta(seconds=30)
        write_state(self.home, "r", last_scan=iso(last),
                    next_scan=iso(last + datetime.timedelta(seconds=I)))
        self.assertEqual(sched.due_repos(now=t0), [])

    def test_unparseable_next_self_heals(self):
        write_state(self.home, "r", next_scan="not-a-time")
        self.assertEqual(sched.due_repos(now=iso(T0)), ["r"])

    def test_disabled_never_due(self):
        write_state(self.home, "r", enabled=False,
                    next_scan=iso(T0 - datetime.timedelta(seconds=10)))
        self.assertEqual(sched.due_repos(now=iso(T0)), [])

    def test_anchor_math(self):
        t0 = iso(T0)
        done = iso(T0 + datetime.timedelta(seconds=25))
        self.assertEqual(sched._anchor_next(t0, done, I),
                         iso(T0 + datetime.timedelta(seconds=I)))
        self.assertEqual(sched._anchor_next(None, done, I),
                         iso(T0 + datetime.timedelta(seconds=25 + I)))
        self.assertEqual(sched._anchor_next("garbage", done, I),
                         iso(T0 + datetime.timedelta(seconds=25 + I)))

    def test_tick_threads_start(self):
        # tick() captures one start and passes it to due_repos and to
        # scan, so the scan anchors to the tick that launched it.
        t0 = iso(T0)
        write_state(self.home, "r",
                    last_scan=iso(T0 - datetime.timedelta(seconds=1795)),
                    next_scan=iso(T0 + datetime.timedelta(seconds=5)))
        calls = []

        def fake_scan(repo, issue_limit=20, tick_start=None):
            calls.append((repo, tick_start))
            return {"repo": repo}

        with mock.patch("openmainty.util.utcnow", return_value=t0), \
                mock.patch.object(sched, "scan", fake_scan):
            out = sched.tick()
        self.assertEqual(list(out), ["r"])
        self.assertEqual(calls, [("r", t0)])


if __name__ == "__main__":
    unittest.main()
