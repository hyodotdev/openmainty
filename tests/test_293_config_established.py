"""Slice 293: observer-backed config identity (gate 2, owner D1/D2).

E28 proved settings/read succeeds on the contained subscription
worker once the policy allows data+create on ONE file:
<home>/.config/muse/.settings.json.lock. This slice brings that
into the product:

- ncontain: the subscription-worker policy gains exactly that
  one write rule (data+create, no unlink/rename, nothing else
  in ~/.config/muse writable). The product profile is unchanged.
- muse.msp_worker_settings_read: read-only settings/read with a
  hard method guard (settings/write* refuses before sending)
  and an allowlist-only parser (provider, model,
  reasoning_effort, contributor_mode_enabled,
  contributor_mode_disclosure_acknowledged). No other value is
  returned, stored, or logged.
- rec["config_established"] is set ONLY by that function, and
  only when the read ran on the same alive child pid as the
  bound session, after bootstrap, with provider=="meta",
  contributor_mode_enabled True, and model == session-bound
  model. Anything else leaves it unset with a precise reason.
- canary_admission clears config-unestablished only through
  that flag; msp_worker_capability reports the observed
  identity; msp_worker_turn still refuses (next slice).

Offline only: scripted-child fixtures (Worker273Mixin). No
live spawn, no real Muse binary, no model turn. Isolated temp
homes only; never the real home. No deletes anywhere:
TemporaryDirectory cleanup only.
"""

import hashlib
import inspect
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import muse as muse_mod  # noqa: E402
from openmainty import ncontain  # noqa: E402
from test_273_followups import (  # noqa: E402
    Worker273Mixin, _msp_test_err)
from test_preflight import (  # noqa: E402
    _msp_test_ok)

FAKE_ROOT = "/private/tmp/fake293/root"
FAKE_HOME = "/Users/fake293"
FAKE_EXE = "/Users/fake293/.local/bin/muse-bin-test"
LOCK_SUFFIX = ".config/muse/.settings.json.lock"
EXPECTED_RULE = (
    "(allow file-write-data file-write-create "
    "(literal \"/Users/fake293/.config/muse/"
    ".settings.json.lock\"))")

ALLOWLIST = (
    "provider",
    "model",
    "reasoning_effort",
    "contributor_mode_enabled",
    "contributor_mode_disclosure_acknowledged",
)


def _settings_values(**over):
    values = {"provider": "meta",
              "model": "muse-spark-1.3",
              "reasoning_effort": "high",
              "contributor_mode_enabled": True}
    values.update(over)
    return {"schemaVersion": 1, "values": values}


class PolicyCase(unittest.TestCase):
    def test_exactly_one_write_rule_for_subscription(self):
        got = ncontain.build_subscription_worker_policy_text(
            FAKE_ROOT, FAKE_EXE, FAKE_HOME)
        writes = [ln for ln in got.splitlines()
                  if ln.startswith("(allow file-write")]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0], EXPECTED_RULE)

    def test_write_deny_line_unchanged_from_base(self):
        base = ncontain.build_policy_text(FAKE_ROOT, FAKE_EXE)
        got = ncontain.build_subscription_worker_policy_text(
            FAKE_ROOT, FAKE_EXE, FAKE_HOME)

        def one(text, prefix):
            found = [ln for ln in text.splitlines()
                     if ln.startswith(prefix)]
            self.assertEqual(len(found), 1, prefix)
            return found[0]

        self.assertEqual(one(got, "(deny file-write*"),
                         one(base, "(deny file-write*"))
        for prefix in ("(version 1)", "(allow default)",
                       "(deny network*)", "(deny mach-lookup",
                       "(deny process-exec"):
            self.assertEqual(one(got, prefix), one(base, prefix),
                             prefix)

    def test_no_unlink_rename_or_other_config_writes(self):
        got = ncontain.build_subscription_worker_policy_text(
            FAKE_ROOT, FAKE_EXE, FAKE_HOME)
        self.assertNotIn("unlink", got)
        self.assertNotIn("rename", got)
        self.assertEqual(got.count(".settings.json.lock"), 1)
        self.assertEqual(got.count("file-write-create"), 1)

    def test_product_profile_has_no_write_allow(self):
        base = ncontain.build_policy_text(FAKE_ROOT, FAKE_EXE)
        writes = [ln for ln in base.splitlines()
                  if ln.startswith("(allow file-write")]
        self.assertEqual(writes, [])

    def test_lock_under_owned_root_refuses(self):
        root = "/private/tmp/fake293/root"
        home = "/private/tmp/fake293/root/home"
        with self.assertRaises(ncontain.ContainmentError):
            ncontain.build_subscription_worker_policy_text(
                root, FAKE_EXE, home)


class TransportCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def test_settings_read_admitted_and_sent(self):
        self.assertIn("settings/read",
                      muse_mod._MSP_WORKER_METHODS)
        handle, child = self._bootstrap("accountLogin")
        try:
            child._script["settings/read"] = [_msp_test_ok(
                3, _settings_values())]
            out = muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            self.assertEqual(out["schemaVersion"], 1)
            sent = [obj for obj in self._methods(child)
                    if obj.get("method") == "settings/read"]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["params"], {})
        finally:
            self._close(handle)

    def test_settings_write_refused_before_send(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            before = len(child.raw_requests)
            for method in ("settings/writeValue",
                           "settings/writeMany"):
                with self.subTest(method=method):
                    with self.assertRaises(
                            muse_mod.MspError) as cm:
                        muse_mod._worker_send_request(
                            handle, method, {}, 10)
                    self.assertEqual(cm.exception.code,
                                     "bad-method")
            self.assertEqual(len(child.raw_requests), before)
            for line in child.raw_requests:
                self.assertNotIn("settings/write",
                                 line.decode("utf-8"))
        finally:
            self._close(handle)


class ParseCase(unittest.TestCase):
    def test_allowlist_only_values_kept(self):
        result = _settings_values(
            authToken="sekrit-token-123",
            displayName="Owner Name",
            nested={"a": 1}, somelist=[1, 2])
        parsed = muse_mod.parse_msp_settings_read(result)
        self.assertEqual(set(parsed["allow"].keys()),
                         set(ALLOWLIST))
        blob = json.dumps(parsed, sort_keys=True)
        self.assertNotIn("sekrit-token-123", blob)
        self.assertNotIn("Owner Name", blob)
        self.assertNotIn("authToken", blob)
        self.assertEqual(parsed["allow"]["provider"]["value"],
                         "meta")

    def test_rendering_rules(self):
        result = _settings_values(
            provider="meta",
            model="has space",
            reasoning_effort="x" * 65,
            contributor_mode_enabled=True)
        result["values"]["extra_dropped"] = "${HOME}/evil"
        parsed = muse_mod.parse_msp_settings_read(result)
        allow = parsed["allow"]
        self.assertEqual(allow["provider"]["value"], "meta")
        self.assertTrue(allow["provider"]["verbatim"])
        self.assertTrue(allow["contributor_mode_enabled"][
            "verbatim"])
        self.assertTrue(
            allow["contributor_mode_enabled"]["value"] is True)
        # Unsafe strings keep only their JSON type, never the
        # text.
        self.assertFalse(allow["model"]["verbatim"])
        self.assertIn("str", allow["model"]["value"])
        self.assertNotIn("has space",
                         json.dumps(parsed, sort_keys=True))
        self.assertFalse(
            allow["reasoning_effort"]["verbatim"])
        # Absent keys report absent.
        self.assertFalse(
            allow["contributor_mode_disclosure_acknowledged"][
                "served"])
        self.assertEqual(
            allow["contributor_mode_disclosure_acknowledged"][
                "value"], "absent")

    def test_malformed_shapes_refuse(self):
        for bad in (None, [], "x",
                    {"schemaVersion": 1},
                    {"schemaVersion": 1, "values": None},
                    {"schemaVersion": 1, "values": []}):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(
                        muse_mod.MspError) as cm:
                    muse_mod.parse_msp_settings_read(bad)
                self.assertEqual(cm.exception.code,
                                 "malformed")


class EstablishCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def _bound(self, state="accountLogin"):
        handle, child = self._bootstrap(state)
        self._start_session(handle, child, 3)
        return handle, child

    def _script_settings(self, child, req_id, **over):
        child._script["settings/read"] = [_msp_test_ok(
            req_id, _settings_values(**over))]

    def test_match_sets_flag_and_identity(self):
        handle, child = self._bound()
        try:
            self._script_settings(child, 4)
            out = muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertTrue(rec.get("config_established")
                            is True)
            ident = rec.get("config_identity")
            self.assertIsInstance(ident, dict)
            self.assertEqual(ident["pid"], rec["child_pid"])
            self.assertEqual(ident["provider"], "meta")
            self.assertEqual(ident["model"], "muse-spark-1.3")
            self.assertTrue(ident["contributor"] is True)
            canon = json.dumps(out["allow"], sort_keys=True,
                               separators=(",", ":"))
            self.assertEqual(
                ident["sha256"],
                hashlib.sha256(
                    canon.encode("utf-8")).hexdigest())
            self.assertNotIn("config_unset_reason", rec)
        finally:
            self._close(handle)

    def _assert_unset(self, handle, fragment):
        rec = muse_mod._MSP_WORKERS[handle]
        self.assertTrue(rec.get("config_established")
                        is not True)
        self.assertNotIn("config_identity", rec)
        reason = rec.get("config_unset_reason")
        self.assertIsInstance(reason, str)
        self.assertTrue(reason.strip())
        self.assertIn(fragment, reason)

    def test_provider_mismatch(self):
        handle, child = self._bound()
        try:
            self._script_settings(child, 4, provider="other")
            muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            self._assert_unset(handle, "provider")
        finally:
            self._close(handle)

    def test_contributor_false(self):
        handle, child = self._bound()
        try:
            self._script_settings(
                child, 4, contributor_mode_enabled=False)
            muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            self._assert_unset(handle, "contributor")
        finally:
            self._close(handle)

    def test_contributor_nonbool(self):
        handle, child = self._bound()
        try:
            self._script_settings(
                child, 4, contributor_mode_enabled="yes")
            muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            self._assert_unset(handle, "contributor")
        finally:
            self._close(handle)

    def test_model_mismatch(self):
        handle, child = self._bound()
        try:
            self._script_settings(child, 4, model="other-model")
            muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            self._assert_unset(handle, "model")
        finally:
            self._close(handle)

    def test_no_session_returns_read_but_unset(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            self._script_settings(child, 3)
            out = muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            self.assertEqual(out["allow"]["provider"]["value"],
                             "meta")
            self._assert_unset(handle, "no-session")
        finally:
            self._close(handle)

    def test_pid_changed(self):
        handle, child = self._bound()
        try:
            rec = muse_mod._MSP_WORKERS[handle]
            rec["worker_session_pid"] = rec["child_pid"] + 1
            self._script_settings(child, 4)
            muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            self._assert_unset(handle, "pid")
        finally:
            self._close(handle)

    def test_rpc_error_propagates_and_unset(self):
        handle, child = self._bound()
        try:
            child._script["settings/read"] = [_msp_test_err(4)]
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.msp_worker_settings_read(
                    handle, timeout=10)
            self.assertEqual(cm.exception.code, "method-error")
            self._assert_unset(handle, "settings-error")
        finally:
            self._close(handle)

    def test_closed_handle_refuses(self):
        handle, child = self._bootstrap("accountLogin")
        self._close(handle)
        with self.assertRaises(muse_mod.MspError) as cm:
            muse_mod.msp_worker_settings_read(
                handle, timeout=10)
        self.assertEqual(cm.exception.code, "unknown-handle")

    def test_no_caller_establish_argument(self):
        params = list(inspect.signature(
            muse_mod.msp_worker_settings_read).parameters)
        self.assertEqual(params, ["handle", "timeout"])
        src = Path(muse_mod.__file__).read_text(
            encoding="utf-8")
        self.assertEqual(
            src.count('rec["config_established"] = True'), 1)

    def test_mismatch_revokes_prior_establishment(self):
        handle, child = self._bound()
        try:
            self._script_settings(child, 4)
            muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertTrue(rec.get("config_established")
                            is True)
            self._script_settings(child, 5, provider="other")
            muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            self._assert_unset(handle, "provider")
        finally:
            self._close(handle)


class CanaryCapabilityCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def _ready(self):
        handle, child = self._bootstrap("accountLogin")
        self._start_session(handle, child, 3)
        child._script["settings/read"] = [_msp_test_ok(
            4, _settings_values())]
        muse_mod.msp_worker_settings_read(handle, timeout=10)
        child._script["hook/list"] = [_msp_test_ok(
            5, {"hooks": []})]
        muse_mod.msp_worker_hooks(handle, timeout=10)
        child._script["usage/read"] = [_msp_test_ok(6, {})]
        muse_mod.msp_worker_usage(handle, timeout=10)
        return handle

    def test_canary_clears_only_via_settings_read(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            self._start_session(handle, child, 3)
            child._script["hook/list"] = [_msp_test_ok(
                4, {"hooks": []})]
            muse_mod.msp_worker_hooks(handle, timeout=10)
            child._script["usage/read"] = [_msp_test_ok(5, {})]
            muse_mod.msp_worker_usage(handle, timeout=10)
            before = muse_mod.canary_admission(handle)
            self.assertFalse(before["admitted_once"])
            self.assertIn("config-unestablished",
                          before["missing"])
            child._script["settings/read"] = [_msp_test_ok(
                6, _settings_values())]
            muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            after = muse_mod.canary_admission(handle)
            self.assertTrue(after["admitted_once"])
            self.assertEqual(after["missing"], [])
        finally:
            self._close(handle)

    def test_capability_reports_observed_identity(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            self._start_session(handle, child, 3)
            before = muse_mod.msp_worker_capability(handle)
            self.assertFalse(before["admitted"])
            self.assertTrue(any(
                line.startswith("contributor-config:")
                for line in before["missing"]))
            self.assertEqual(
                before["contributor_config"]["observed"],
                False)
            child._script["settings/read"] = [_msp_test_ok(
                4, _settings_values())]
            muse_mod.msp_worker_settings_read(
                handle, timeout=10)
            after = muse_mod.msp_worker_capability(handle)
            self.assertFalse(after["admitted"])
            self.assertFalse(any(
                line.startswith("contributor-config:")
                for line in after["missing"]))
            seen = after["contributor_config"]
            self.assertTrue(seen["observed"])
            self.assertEqual(seen["provider"], "meta")
            self.assertEqual(seen["model"], "muse-spark-1.3")
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertEqual(seen["pid"], rec["child_pid"])
            self.assertEqual(
                seen["sha256"],
                rec["config_identity"]["sha256"])
        finally:
            self._close(handle)

    def test_turn_still_refuses(self):
        handle = self._ready()
        try:
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.msp_worker_turn(handle, "hi",
                                         timeout=10)
            self.assertEqual(cm.exception.code,
                             "capability-unproved")
            self.assertIn("metaready", str(cm.exception))
        finally:
            self._close(handle)
