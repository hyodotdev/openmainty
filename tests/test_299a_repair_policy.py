"""Slice 299a: the repair policy describes what actually runs.

profile repair-broker, network_mode brokered, trust_workspace
False; the argv drops --trust-workspace/--sandbox-network and
gains --no-session-log. Native and channel legs are unchanged:
production still refuses before spawn (pinned by
test_298_repair_broker.test_production_refuses_before_broker_start,
kept green, not duplicated here).
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from tests.test_preflight import (
    _read_leaf, _fixture_observer, _fake_transport,
    _fixture_values)
from openmainty import preflight as pf
from openmainty import muse

WANT_POLICY = {
    "profile": "repair-broker",
    "provider": "meta",
    "model": "muse-spark-1.3",
    "contributor_mode": True,
    "approval_mode": "never",
    "approval_judge": "off",
    "network_mode": "brokered",
    "disable_request_user_input": True,
    "trust_workspace": False,
    "max_model_steps": 25,
    "max_tool_output_bytes": 200000,
}


class RepairPolicyValuesTest(unittest.TestCase):
    def test_source_repair_profile_values(self):
        self.assertEqual(muse.SOURCE_REPAIR_PROFILE,
                         WANT_POLICY)

    def test_required_policy_values(self):
        self.assertEqual(pf.REQUIRED_POLICY, WANT_POLICY)

    def test_fingerprint_names_brokered(self):
        self.assertIn("net=brokered",
                      muse.pinned_fingerprint())

    def test_argv_golden(self):
        argv = muse.build_source_repair_cmd("do repair",
                                            "/w")
        self.assertEqual(argv[0:2], ["muse", "exec"])
        for flag in ("--disable-shell",
                     "--disable-web-tools",
                     "--disable-reminders",
                     "--no-foreign-personal-context",
                     "--disable-request-user-input",
                     "--approval-mode", "--approval-judge",
                     "--no-session-log",
                     "--max-model-steps",
                     "--max-tool-output-bytes",
                     "--workspace", "--json"):
            self.assertIn(flag, argv)
        for flag in ("--trust-workspace",
                     "--sandbox-network",
                     "--disable-write"):
            self.assertNotIn(flag, argv)
        self.assertEqual(
            argv[argv.index("--max-model-steps") + 1], "25")
        self.assertEqual(
            argv[argv.index("--max-tool-output-bytes") + 1],
            "200000")
        self.assertEqual(argv[-1], "do repair")

    def test_dashboard_names_brokered_network(self):
        srcd = os.path.join(
            os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))),
            "src", "openmainty", "dashboard.html")
        with open(srcd, "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(">brokered<", src)
        self.assertNotIn(">restricted<", src)


class RepairPolicyGateTest(unittest.TestCase):
    """Own fixture (mirrors PreflightCase; subclassing would
    collect its tests here)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, mode=0o700)
        prev_bin = os.environ.get("OPENMAINTY_MUSE_BINARY")
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir, mode=0o700)
        self.bin = os.path.join(bindir, "muse")
        with open(self.bin, "wb") as fh:
            fh.write(b"fixture-muse-binary-v1")
        os.chmod(self.bin, 0o755)
        os.environ["OPENMAINTY_MUSE_BINARY"] = self.bin
        if prev_bin is None:
            self.addCleanup(os.environ.pop,
                            "OPENMAINTY_MUSE_BINARY", None)
        else:
            self.addCleanup(os.environ.__setitem__,
                            "OPENMAINTY_MUSE_BINARY",
                            prev_bin)
        prev_home = os.environ.get("OPENMAINTY_HOME")
        os.environ["OPENMAINTY_HOME"] = self.home
        if prev_home is None:
            self.addCleanup(os.environ.pop,
                            "OPENMAINTY_HOME", None)
        else:
            self.addCleanup(os.environ.__setitem__,
                            "OPENMAINTY_HOME", prev_home)
        prev_key = os.environ.pop("META_API_KEY", None)
        if prev_key is not None:
            self.addCleanup(os.environ.__setitem__,
                            "META_API_KEY", prev_key)
        prev_contrib = os.environ.pop("MUSE_CONTRIBUTOR_MODE",
                                      None)
        if prev_contrib is not None:
            self.addCleanup(os.environ.__setitem__,
                            "MUSE_CONTRIBUTOR_MODE",
                            prev_contrib)
        cur = mock.patch(
            "openmainty.preflight.current_identity",
            return_value=_fixture_values())
        cur.start()
        self.addCleanup(cur.stop)

    def _no_spawn(self):
        popen = mock.patch.object(
            muse.subprocess, "Popen",
            side_effect=AssertionError(
                "no spawn in offline tests"))
        popen.start()
        self.addCleanup(popen.stop)

    def _created_input(self, dedupe):
        self._no_spawn()
        created = pf.create_preflight(self.home, dedupe)
        return json.loads(_read_leaf(
            self.home, created["pfid"], "input"))

    def test_created_input_carries_new_policy(self):
        doc = self._created_input("k-policy")
        self.assertEqual(doc["policy"], WANT_POLICY)
        pf._validate_input_doc(doc)

    def test_profile_drift_refuses(self):
        doc = self._created_input("k-drift-profile")
        doc["policy"] = dict(doc["policy"])
        doc["policy"]["profile"] = "source-repair"
        with self.assertRaises(pf.PreflightError) as cm:
            pf._validate_input_doc(doc)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_network_drift_refuses(self):
        doc = self._created_input("k-drift-network")
        doc["policy"] = dict(doc["policy"])
        doc["policy"]["network_mode"] = "restricted"
        with self.assertRaises(pf.PreflightError) as cm:
            pf._validate_input_doc(doc)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_trust_drift_refuses(self):
        doc = self._created_input("k-drift-trust")
        doc["policy"] = dict(doc["policy"])
        doc["policy"]["trust_workspace"] = True
        with self.assertRaises(pf.PreflightError) as cm:
            pf._validate_input_doc(doc)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_verified_projection_tracks_policy(self):
        self._no_spawn()
        created = pf.create_preflight(self.home,
                                      "k-projection")
        out = pf.run_preflight(
            self.home, created["pfid"],
            transport=_fake_transport(),
            observer=_fixture_observer())
        self.assertEqual(out["state"], "verified")
        described = pf.describe_preflight(self.home)
        self.assertEqual(described["policy"], {
            "profile": "repair-broker",
            "model": "muse-spark-1.3",
            "network_mode": "brokered"})


if __name__ == "__main__":
    unittest.main()
