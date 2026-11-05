"""StageA offline PR/QA fixture boundary tests (test-only, no product change)."""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tests import _home as _home_helper


class _CleanupScope:
    """Non-discoverable addCleanup/doCleanups container (not a TestCase)."""

    def __init__(self):
        self._cleanups = []

    def addCleanup(self, fn, *args, **kwargs):
        self._cleanups.append((fn, args, kwargs))

    def doCleanups(self):
        errors = []
        while self._cleanups:
            fn, args, kwargs = self._cleanups.pop()
            try:
                fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - surface cleanup failure
                errors.append(exc)
        if errors:
            raise errors[0]


class OfflinePrsHelperTest(unittest.TestCase):
    def test_canned_scoped_and_restored(self):
        import openmainty.cli as _cli
        import openmainty.util as _util

        real_open_prs = _cli._open_prs
        real_open_issues = _cli._open_issues
        rows = [{"number": 857, "title": "fix t"}]
        scope = _CleanupScope()
        try:
            _home_helper.offline_prs(scope, rows)
            # Canned rows served without touching real transport.
            with mock.patch.object(
                    _util, "run_cmd",
                    side_effect=AssertionError("real gh reached")) as denied:
                self.assertEqual(_cli._open_prs("o/r"), rows)
                denied.assert_not_called()
            # Negative control: the sentinel would trip real transport.
            with mock.patch.object(
                    _util, "run_cmd",
                    side_effect=AssertionError("real gh reached")):
                with self.assertRaises(AssertionError):
                    real_open_prs("o/r", limit=1)
            # Unrelated issue collector untouched.
            self.assertIs(_cli._open_issues, real_open_issues)
        finally:
            scope.doCleanups()
        self.assertIs(_cli._open_prs, real_open_prs)
        self.assertIs(_cli._open_issues, real_open_issues)

    def test_nesting_via_child_scopes(self):
        import openmainty.cli as _cli

        real_open_prs = _cli._open_prs
        outer = _CleanupScope()
        inner = _CleanupScope()
        try:
            _home_helper.offline_prs(outer, [{"number": 1}])
            self.assertEqual(_cli._open_prs("o/r"), [{"number": 1}])
            _home_helper.offline_prs(inner, [{"number": 2}])
            self.assertEqual(_cli._open_prs("o/r"), [{"number": 2}])
            inner.doCleanups()
            self.assertEqual(_cli._open_prs("o/r"), [{"number": 1}])
        finally:
            try:
                inner.doCleanups()
            except Exception:
                pass
            outer.doCleanups()
        self.assertIs(_cli._open_prs, real_open_prs)


class OfflineQaRegistryHelperTest(unittest.TestCase):
    def test_mapping_scoped_and_restored(self):
        import openmainty.batches as _batches
        import subprocess as _sp

        real_fn = _batches._qa_registry_repo
        scope = _CleanupScope()
        try:
            _home_helper.offline_qa_registry(
                scope, {"webpack": "webpack/webpack"})
            with mock.patch.object(
                    _sp, "run",
                    side_effect=AssertionError("real npm reached")) as denied:
                self.assertEqual(
                    _batches._qa_registry_repo("webpack"),
                    "webpack/webpack")
                self.assertEqual(
                    _batches._qa_registry_repo("unknown-dep"), "")
                denied.assert_not_called()
            # Negative control: sentinel would trip the real transport.
            with mock.patch.object(
                    _sp, "run",
                    side_effect=AssertionError("real npm reached")):
                with self.assertRaises(AssertionError):
                    real_fn("webpack")
        finally:
            scope.doCleanups()
        self.assertIs(_batches._qa_registry_repo, real_fn)

    def test_empty_mapping_and_nesting(self):
        import openmainty.batches as _batches

        real_fn = _batches._qa_registry_repo
        outer = _CleanupScope()
        inner = _CleanupScope()
        try:
            _home_helper.offline_qa_registry(outer, {})
            self.assertEqual(_batches._qa_registry_repo("anything"), "")
            _home_helper.offline_qa_registry(inner, {"a": "o/a"})
            self.assertEqual(_batches._qa_registry_repo("a"), "o/a")
            inner.doCleanups()
            self.assertEqual(_batches._qa_registry_repo("a"), "")
        finally:
            try:
                inner.doCleanups()
            except Exception:
                pass
            outer.doCleanups()
        self.assertIs(_batches._qa_registry_repo, real_fn)


class HelperCleanupOrderTest(unittest.TestCase):
    def test_wait_runs_before_stop_with_home_active(self):
        import os as _os
        import subprocess as _sp

        import openmainty.batches as _batches
        import openmainty.cli as _cli
        import openmainty.util as _util

        real_open_prs = _cli._open_prs
        real_qa_repo = _batches._qa_registry_repo
        before_home = _os.environ.get("OPENMAINTY_HOME")
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = str(Path(tmp.name) / "home")
        Path(home).mkdir(parents=True, exist_ok=True)
        scope = _CleanupScope()
        events = []
        real_wait = _home_helper._wait_refresh_idle

        def _recording_wait():
            # Never call the restored real collector: sample guarded.
            try:
                prs_val = _cli._open_prs("o/r")
            except AssertionError:
                prs_val = "real-blocked"
            try:
                qa_val = _batches._qa_registry_repo("webpack")
            except AssertionError:
                qa_val = "real-blocked"
            events.append({
                "home": _os.environ.get("OPENMAINTY_HOME"),
                "prs": prs_val,
                "qa": qa_val,
            })
            return real_wait()

        with mock.patch.object(
                _util, "run_cmd",
                side_effect=AssertionError("real gh reached")), \
                mock.patch.object(
                    _sp, "run",
                    side_effect=AssertionError("real npm reached")), \
                mock.patch.object(
                    _home_helper, "_wait_refresh_idle",
                    side_effect=_recording_wait) as wait_mock:
            _home_helper.use_temp_home(scope, home)
            _home_helper.offline_prs(scope, [{"number": 9}])
            _home_helper.offline_qa_registry(
                scope, {"webpack": "webpack/webpack"})
            scope.doCleanups()
            # Helper waits + use_temp_home restore wait all ran.
            self.assertGreaterEqual(wait_mock.call_count, 2)
            # First helper wait saw canned PR+QA while HOME active.
            self.assertEqual(events[0]["home"], home)
            self.assertEqual(events[0]["prs"], [{"number": 9}])
            self.assertEqual(
                events[0]["qa"], "webpack/webpack")
        # Originals and enclosing HOME restored; no live transport ran.
        self.assertIs(_cli._open_prs, real_open_prs)
        self.assertIs(_batches._qa_registry_repo, real_qa_repo)
        self.assertEqual(_os.environ.get("OPENMAINTY_HOME"), before_home)


class HelperDoesNotTouchDiscussionsTest(unittest.TestCase):
    def test_discussions_boundary_untouched(self):
        import openmainty.discussions as _disc

        before_scan = _disc.scan_source
        before_labels = _disc.fetch_labels
        scope = _CleanupScope()
        try:
            _home_helper.offline_prs(scope, [])
            _home_helper.offline_qa_registry(scope, {})
            self.assertIs(_disc.scan_source, before_scan)
            self.assertIs(_disc.fetch_labels, before_labels)
        finally:
            scope.doCleanups()
        self.assertIs(_disc.scan_source, before_scan)
        self.assertIs(_disc.fetch_labels, before_labels)


class OfflineMuseVersionHelperTest(unittest.TestCase):
    def test_sentinel_scoped_and_restored(self):
        import openmainty.muse as _muse

        real_version = _muse.version
        scope = _CleanupScope()
        try:
            sentinel = _home_helper.offline_muse_version(
                scope, "fixture-muse-9.9.9")
            self.assertEqual(sentinel, "fixture-muse-9.9.9")
            self.assertEqual(_muse.version(), "fixture-muse-9.9.9")
            # Explicit caller input only: a second scope composes back.
            inner = _CleanupScope()
            try:
                _home_helper.offline_muse_version(inner, "fixture-muse-1.0")
                self.assertEqual(_muse.version(), "fixture-muse-1.0")
                inner.doCleanups()
                self.assertEqual(_muse.version(), "fixture-muse-9.9.9")
            finally:
                try:
                    inner.doCleanups()
                except Exception:
                    pass
        finally:
            scope.doCleanups()
        self.assertIs(_muse.version, real_version)

    def test_never_reaches_real_cli(self):
        import subprocess as _sp

        import openmainty.muse as _muse

        real_version = _muse.version
        outer = _CleanupScope()
        inner = _CleanupScope()
        try:
            # Controlled outer fake: no real CLI/version invoked anywhere.
            _home_helper.offline_muse_version(outer, "outer-fake-7")
            _home_helper.offline_muse_version(inner, "fixture-muse-0.0.0")
            with mock.patch.object(
                    _sp, "run",
                    side_effect=AssertionError("real muse reached")), \
                    mock.patch.object(
                        _sp, "Popen",
                        side_effect=AssertionError("real muse reached")):
                # Inner sentinel hides the outer fake, no transport used.
                self.assertEqual(_muse.version(), "fixture-muse-0.0.0")
            inner.doCleanups()
            # Cleanup restores exactly the outer fake, never the real CLI.
            self.assertEqual(_muse.version(), "outer-fake-7")
        finally:
            try:
                inner.doCleanups()
            except Exception:
                pass
            outer.doCleanups()
        self.assertIs(_muse.version, real_version)


class OfflineServiceStatusHelperTest(unittest.TestCase):
    ROW = {"installed": True, "running": False, "pid": None,
           "note": "fixture offline"}

    def test_row_defensive_copy_and_restored(self):
        import openmainty.serve as _serve

        real_fn = _serve._maintenance_service
        scope = _CleanupScope()
        nested_in = {"installed": True, "running": False, "pid": None,
                     "note": "fixture offline",
                     "detail": {"queue": ["a"], "flags": {"x": 1}}}
        try:
            owned = _home_helper.offline_service_status(scope, nested_in)
            # Helper must detach: mutating the caller's input (incl.
            # nested fields) after setup must not change served rows.
            nested_in["running"] = True
            nested_in["detail"]["queue"].append("zzz")
            nested_in["detail"]["flags"]["x"] = 999
            first = _serve._maintenance_service()
            self.assertFalse(first["running"])
            self.assertEqual(first["detail"]["queue"], ["a"])
            self.assertEqual(first["detail"]["flags"], {"x": 1})
            # Per-call consumer mutation (incl. nested) must not leak.
            first["running"] = True
            first["pid"] = 1234
            first["detail"]["queue"].append("leak")
            second = _serve._maintenance_service()
            self.assertFalse(second["running"])
            self.assertIsNone(second["pid"])
            self.assertEqual(second["detail"]["queue"], ["a"])
            # Mutating the helper's own return value must not leak either.
            owned["running"] = True
            owned["detail"]["flags"]["x"] = 2
            third = _serve._maintenance_service()
            self.assertFalse(third["running"])
            self.assertEqual(third["detail"]["flags"], {"x": 1})
        finally:
            scope.doCleanups()
        self.assertIs(_serve._maintenance_service, real_fn)

    def test_nesting_and_never_reaches_launchctl(self):
        import subprocess as _sp

        import openmainty.serve as _serve

        real_fn = _serve._maintenance_service
        outer = _CleanupScope()
        inner = _CleanupScope()
        try:
            _home_helper.offline_service_status(outer, dict(self.ROW))
            other = dict(self.ROW)
            other["running"] = True
            other["pid"] = 4242
            _home_helper.offline_service_status(inner, other)
            with mock.patch.object(
                    _sp, "run",
                    side_effect=AssertionError("real launchctl reached")):
                self.assertEqual(
                    _serve._maintenance_service()["pid"], 4242)
            inner.doCleanups()
            self.assertEqual(
                _serve._maintenance_service()["pid"], None)
        finally:
            try:
                inner.doCleanups()
            except Exception:
                pass
            outer.doCleanups()
        self.assertIs(_serve._maintenance_service, real_fn)


class HelperWaitBeforeStopLifetimeTest(unittest.TestCase):
    def test_wait_observes_fixtures_then_restores(self):
        import os as _os
        import subprocess as _sp
        import threading as _threading

        import openmainty.muse as _muse
        import openmainty.serve as _serve

        real_version = _muse.version
        real_svc = _serve._maintenance_service
        before_home = _os.environ.get("OPENMAINTY_HOME")
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home_outer = str(Path(tmp.name) / "home-outer")
        home_inner = str(Path(tmp.name) / "home-inner")
        Path(home_outer).mkdir(parents=True, exist_ok=True)
        Path(home_inner).mkdir(parents=True, exist_ok=True)
        outer = _CleanupScope()
        inner = _CleanupScope()
        row_outer = {"installed": True, "running": False, "pid": None,
                     "note": "outer"}
        row_inner = {"installed": True, "running": True, "pid": 4242,
                     "note": "inner"}
        observed = {}
        start_ev = _threading.Event()
        done_ev = _threading.Event()
        real_wait = _home_helper._wait_refresh_idle

        def _worker():
            start_ev.wait(timeout=10)
            observed["version"] = _muse.version()
            observed["svc"] = dict(_serve._maintenance_service())
            observed["home"] = _os.environ.get("OPENMAINTY_HOME")
            done_ev.set()

        worker = _threading.Thread(target=_worker, daemon=True)
        calls = []

        def _recording_wait():
            calls.append(_os.environ.get("OPENMAINTY_HOME"))
            if len(calls) == 1:
                start_ev.set()
                self.assertTrue(done_ev.wait(timeout=10))
            return real_wait()

        try:
            with mock.patch.object(
                    _sp, "run",
                    side_effect=AssertionError("real transport")), \
                    mock.patch.object(
                        _sp, "Popen",
                        side_effect=AssertionError("real transport")), \
                    mock.patch.object(
                        _home_helper, "_wait_refresh_idle",
                        side_effect=_recording_wait):
                _home_helper.use_temp_home(outer, home_outer)
                _home_helper.offline_muse_version(outer, "outer-v")
                _home_helper.offline_service_status(outer, row_outer)
                _home_helper.use_temp_home(inner, home_inner)
                _home_helper.offline_muse_version(inner, "inner-v")
                _home_helper.offline_service_status(inner, row_inner)
                worker.start()
                inner.doCleanups()
                worker.join(timeout=10)
                # First wait released/joined the owned worker: it saw the
                # inner fixture metadata patches and the fixture HOME.
                self.assertFalse(worker.is_alive())
                self.assertTrue(done_ev.is_set())
                self.assertEqual(observed.get("version"), "inner-v")
                self.assertEqual(observed.get("svc", {}).get("pid"), 4242)
                self.assertEqual(observed.get("home"), home_inner)
                # Inner patches restore to the owned outer fakes + outer HOME.
                self.assertEqual(_muse.version(), "outer-v")
                self.assertEqual(
                    _serve._maintenance_service()["note"], "outer")
                self.assertEqual(
                    _os.environ.get("OPENMAINTY_HOME"), home_outer)
        finally:
            try:
                inner.doCleanups()
            except Exception:
                pass
            outer.doCleanups()
        self.assertFalse(worker.is_alive())
        self.assertIs(_muse.version, real_version)
        self.assertIs(_serve._maintenance_service, real_svc)
        self.assertEqual(_os.environ.get("OPENMAINTY_HOME"), before_home)


if __name__ == "__main__":
    unittest.main()
