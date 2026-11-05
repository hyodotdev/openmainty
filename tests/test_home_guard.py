"""M2.15b: the real-home guard and the restore-not-pop helper.

The guard (paths.home with OPENMAINTY_TEST_GUARD=1, set suite-wide
by tests/__init__.py) must fail loud on the real home and stay
quiet on fixture homes. The helper must restore the previous
OPENMAINTY_HOME value instead of deleting it. Offline; temp homes
only — the real home is named but never touched.
"""
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import paths  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


class _Recorder:
    """Minimal testcase double: records cleanups for manual runs."""

    def __init__(self):
        self.cleanups = []

    def addCleanup(self, fn, *args, **kwargs):
        self.cleanups.append((fn, args, kwargs))

    def run_cleanups(self):
        for fn, args, kwargs in reversed(self.cleanups):
            fn(*args, **kwargs)


class GuardTests(unittest.TestCase):
    def test_guard_on_for_suite(self):
        self.assertEqual(os.environ.get("OPENMAINTY_TEST_GUARD"), "1")

    def test_unset_home_raises(self):
        old = os.environ.pop("OPENMAINTY_HOME", None)
        try:
            with self.assertRaises(RuntimeError):
                paths.home()
        finally:
            if old is not None:
                os.environ["OPENMAINTY_HOME"] = old

    def test_real_home_value_raises(self):
        old = os.environ.get("OPENMAINTY_HOME")
        os.environ["OPENMAINTY_HOME"] = str(Path.home() / ".openmainty")
        try:
            with self.assertRaises(RuntimeError):
                paths.home()
        finally:
            if old is None:
                os.environ.pop("OPENMAINTY_HOME", None)
            else:
                os.environ["OPENMAINTY_HOME"] = old

    def test_temp_home_passes(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.environ.get("OPENMAINTY_HOME")
        os.environ["OPENMAINTY_HOME"] = str(
            Path(tmp.name) / "home")
        try:
            self.assertEqual(paths.home(),
                             Path(tmp.name) / "home")
        finally:
            if old is None:
                os.environ.pop("OPENMAINTY_HOME", None)
            else:
                os.environ["OPENMAINTY_HOME"] = old

    def test_guard_off_allows_real_home(self):
        old_guard = os.environ.get("OPENMAINTY_TEST_GUARD")
        old_home = os.environ.get("OPENMAINTY_HOME")
        if "OPENMAINTY_TEST_GUARD" in os.environ:
            del os.environ["OPENMAINTY_TEST_GUARD"]
        os.environ["OPENMAINTY_HOME"] = str(Path.home() / ".openmainty")
        try:
            # Production shape: no guard reads through untouched.
            self.assertEqual(paths.home(),
                             Path.home() / ".openmainty")
        finally:
            if old_guard is None:
                os.environ.pop("OPENMAINTY_TEST_GUARD", None)
            else:
                os.environ["OPENMAINTY_TEST_GUARD"] = old_guard
            if old_home is None:
                os.environ.pop("OPENMAINTY_HOME", None)
            else:
                os.environ["OPENMAINTY_HOME"] = old_home


class HelperTests(unittest.TestCase):
    def test_restores_previous_value(self):
        old = os.environ.get("OPENMAINTY_HOME")
        os.environ["OPENMAINTY_HOME"] = "/tmp/om66-m215b-prev"
        rec = _Recorder()
        try:
            self.assertEqual(
                use_temp_home(rec, "/tmp/om66-m215b-next"),
                "/tmp/om66-m215b-next")
            self.assertEqual(os.environ["OPENMAINTY_HOME"],
                             "/tmp/om66-m215b-next")
            rec.run_cleanups()
            self.assertEqual(os.environ["OPENMAINTY_HOME"],
                             "/tmp/om66-m215b-prev")
        finally:
            if old is None:
                os.environ.pop("OPENMAINTY_HOME", None)
            else:
                os.environ["OPENMAINTY_HOME"] = old

    def test_pops_only_what_it_added(self):
        old = os.environ.pop("OPENMAINTY_HOME", None)
        rec = _Recorder()
        try:
            use_temp_home(rec, "/tmp/om66-m215b-fresh")
            self.assertEqual(os.environ["OPENMAINTY_HOME"],
                             "/tmp/om66-m215b-fresh")
            rec.run_cleanups()
            self.assertNotIn("OPENMAINTY_HOME", os.environ)
        finally:
            if old is not None:
                os.environ["OPENMAINTY_HOME"] = old


if __name__ == "__main__":
    unittest.main()
