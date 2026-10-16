"""Durable coverage: drift detection never raises, errors are reported."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import deps  # noqa: E402

PROFILE = {"name": "x", "github": "e/x", "baseBranch": "main",
           "dependencies": [
               {"name": "same", "kind": "npm", "id": "pkg-same",
                "pin_file": "pins.json", "pin_key": "same"},
               {"name": "old", "kind": "npm", "id": "pkg-old",
                "pin_file": "pins.json", "pin_key": "old"},
               {"name": "broken", "kind": "npm", "id": "pkg-broken",
                "pin_file": "pins.json", "pin_key": "missing"},
               {"name": "weird", "kind": "nope", "id": "z",
                "pin_file": "pins.json", "pin_key": "same"},
               {"name": "future", "kind": "npm", "id": "pkg-future",
                "pin_file": "pins.json", "pin_key": "same"},
           ]}


class DepsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkout = Path(self.tmp.name)
        (self.checkout / "pins.json").write_text('{"same": "1.0.0", "old": "1.0.0"}')
        self.orig = deps.latest

        def fake_latest(kind, ident):
            if kind != "npm":
                raise ValueError("unknown kind")
            if ident == "pkg-future":
                raise LookupError("not published")
            return {"pkg-same": "1.0.0", "pkg-old": "2.0.0"}[ident]

        deps.latest = fake_latest
        self.addCleanup(lambda: setattr(deps, "latest", self.orig))

    def test_drift_and_errors_reported_without_raise(self):
        rows = deps.check(PROFILE, self.checkout)
        by_name = {r["name"]: r for r in rows}
        self.assertFalse(by_name["same"]["drift"])
        self.assertEqual(by_name["same"]["latest"], "1.0.0")
        self.assertTrue(by_name["old"]["drift"])
        self.assertIn("1.0.0", deps.drift_body(by_name["old"]))
        self.assertIn("2.0.0", deps.drift_body(by_name["old"]))
        self.assertIsNotNone(by_name["broken"]["error"])   # missing pin key
        self.assertIsNotNone(by_name["weird"]["error"])    # unknown kind
        self.assertTrue(by_name["future"]["unpublished"])  # 404, not an error
        self.assertIsNone(by_name["future"]["error"])
        self.assertFalse(any(r["drift"] for r in rows if r["error"]))


if __name__ == "__main__":
    unittest.main()
