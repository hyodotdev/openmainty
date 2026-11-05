"""Slice 294: canary turn transport + ledger-only turn stream (gate 2).

E31: every gate passed and the host accepted the turn, then the
product reader broke on a notification outside
_MSP_WORKER_LIFECYCLE_METHODS (accept + zero network denies
prove NOTHING about the transport: no completion or response
was observed). This slice:

- muse reader: documented turn-scoped notifications become
  ledger-only (method name + count, NEVER payload); the
  host-scoped usage/changed (SubscriptionUsage, no
  sessionId) ledgers with session_id None. Anything else
  still fails closed, and every break records the exact
  method on rec["broken_method"]. Non-turn behaviour is
  unchanged.
- ncontain: subscription-canary policy = subscription text +
  exactly the two E31 network lines, proven base-plus by
  removal. spawn_msp_worker(profile="subscription-canary")
  uses it. The "subscription" profile stays network-denied.
- muse.msp_worker_canary_turn: profile + prior canary
  admission + single use (flag before the wire write). NO
  prompt argument: the pinned bytes are a module constant
  (pfid "canary"). One turn/start, ledger wait, bounded
  session/read polls with the ALLOWLIST control (only
  userMessage/agentMessage/reasoning pass; first other item
  records kind + name, closes, returns "tool-attempted").
  Timeout closes and returns "timeout". No retry anywhere.
  msp_worker_turn keeps refusing; capability stays False.

Offline only: scripted-child fixtures. No live spawn, no
real binary, no model turn. Isolated temp homes only; never
the real home. No deletes: TemporaryDirectory cleanup only.
"""

import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import muse as muse_mod  # noqa: E402
from openmainty import ncontain  # noqa: E402
from test_273_followups import (  # noqa: E402
    Worker273Mixin, _msp_test_err)
from test_276_subscription_profile import (  # noqa: E402
    GOLDEN_SUBSCRIPTION_TEXT)
from test_preflight import (  # noqa: E402
    _MspScriptedChild, _msp_test_init_result, _msp_test_ok)

FAKE_ROOT = "/private/tmp/fake294/root"
FAKE_HOME = "/Users/fake294"
FAKE_EXE = "/Users/fake294/.local/bin/muse-bin-test"

LEDGER_ONLY_METHODS = (
    "item/delta",
    "item/updated",
    "session/tokenUsage",
    "session/contextUsage",
    "turn/retryScheduled",
    "turn/modelRetryScheduled",
    "turn/retracted",
    "turn/unqueued",
    "session/nameChanged",
    "session/viewHealthChanged",
    "session/todoListChanged",
    "recap/completed",
)

HOST_LEDGER_METHODS = (
    "usage/changed",
)

STAY_CLOSED_METHODS = (
    "session/permissionProfileChanged",
    "session/approvalModeChanged",
    "session/branchChanged",
    "session/modelRouteUnserved",
    "session/reasoningEffortChanged",
    "approval/requested",
    "admission/requested",
    "userInput/requested",
    "mcpServer/changed",
    "totally/bogus",
)

CANARY_LINES = (
    "(allow network-outbound (remote tcp \"*:443\"))",
    "(allow network-outbound "
    "(literal \"/private/var/run/mDNSResponder\"))",
)


def _settings_result(**over):
    values = {"provider": "meta",
              "model": "muse-spark-1.3",
              "reasoning_effort": "high",
              "contributor_mode_enabled": True}
    values.update(over)
    return {"schemaVersion": 1, "values": values}


def _read_result(items, session=None):
    sess = {"providerId": "meta",
            "modelId": "muse-spark-1.3",
            "sessionId": "s", "status": "idle",
            "turnCount": 1}
    if session:
        sess.update(session)
    return {"history": {"items": items, "mode": "full",
                        "snapshot": None},
            "pendingRequests": [],
            "session": sess,
            "viewCursor": "v1"}


class ReaderCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def _bound(self):
        handle, child = self._bootstrap("accountLogin")
        self._start_session(handle, child, 3)
        rec = muse_mod._MSP_WORKERS[handle]
        return handle, child, rec["worker_session"]["id"]

    def _notify(self, handle, method, params):
        rec = muse_mod._MSP_WORKERS[handle]
        raw = json.dumps({"jsonrpc": "2.0", "method": method,
                          "params": params}).encode("utf-8")
        muse_mod._dispatch_worker_inbox(rec, raw)
        return rec

    def test_turn_notifications_ledgered_without_payload(self):
        handle, _child, sid = self._bound()
        try:
            secret = "sekrit-payload-294"
            for method in LEDGER_ONLY_METHODS:
                rec = self._notify(
                    handle, method,
                    {"sessionId": sid, "leak": secret,
                     "delta": {"text": secret}})
                self.assertIsNone(rec.get("broken"), method)
                counts = rec.get("turn_notification_counts")
                self.assertEqual(counts.get(method), 1,
                                 method)
            rec = muse_mod._MSP_WORKERS[handle]
            ledger = rec.get("lifecycle")
            seen = [e["method"] for e in ledger]
            for method in LEDGER_ONLY_METHODS:
                self.assertIn(method, seen)
            blob = json.dumps(
                {"counts": rec.get("turn_notification_counts"),
                 "ledger": ledger}, sort_keys=True)
            self.assertNotIn(secret, blob)
        finally:
            self._close(handle)

    def test_unknown_notification_breaks_and_names_method(self):
        for method in STAY_CLOSED_METHODS:
            handle, _child, sid = self._bound()
            try:
                with self.subTest(method=method):
                    rec = self._notify(
                        handle, method, {"sessionId": sid})
                    self.assertEqual(rec.get("broken"),
                                     "server-notification")
                    self.assertEqual(rec.get("broken_method"),
                                     method)
            finally:
                self._close_quiet(handle)

    def test_host_notification_ledgers_without_sid(self):
        handle, _child, _sid = self._bound()
        try:
            secret = "sekrit-usage-294"
            params = {"observedAtMs": 1730000000000,
                      "tier": "team",
                      "weekly": {"used": 3, "leak": secret},
                      "window": {"used": 1200, "leak": secret}}
            rec = self._notify(handle, "usage/changed", params)
            self.assertIsNone(rec.get("broken"))
            self.assertEqual(
                rec["turn_notification_counts"]["usage/changed"],
                1)
            self.assertIn({"method": "usage/changed",
                           "session_id": None},
                          rec["lifecycle"])
            blob = json.dumps(
                {"counts": rec.get("turn_notification_counts"),
                 "ledger": rec["lifecycle"]}, sort_keys=True)
            self.assertNotIn(secret, blob)
            self.assertNotIn("team", blob)
        finally:
            self._close(handle)

    def test_ledger_only_mismatched_sid_breaks(self):
        handle, _child, _sid = self._bound()
        try:
            rec = self._notify(handle, "item/delta",
                               {"sessionId": "foreign-sid"})
            self.assertEqual(rec.get("broken"),
                             "session-mismatch")
            self.assertEqual(rec.get("broken_method"),
                             "item/delta")
        finally:
            self._close_quiet(handle)

    def test_ledger_only_missing_sid_breaks(self):
        handle, _child, _sid = self._bound()
        try:
            rec = self._notify(handle, "item/delta",
                               {"delta": {"text": "x"}})
            self.assertEqual(rec.get("broken"), "malformed")
            self.assertEqual(rec.get("broken_method"),
                             "item/delta")
        finally:
            self._close_quiet(handle)

    def test_streaming_flood_counts_without_growth(self):
        handle, _child, sid = self._bound()
        try:
            for _i in range(100):
                self._notify(handle, "item/delta",
                             {"sessionId": sid, "n": _i})
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertIsNone(rec.get("broken"))
            self.assertEqual(
                rec["turn_notification_counts"]["item/delta"],
                100)
            self.assertLessEqual(len(rec["lifecycle"]), 64)
        finally:
            self._close(handle)

    def test_existing_lifecycle_unchanged(self):
        handle, _child, sid = self._bound()
        try:
            rec = self._notify(handle, "turn/completed",
                               {"sessionId": sid})
            self.assertIsNone(rec.get("broken"))
            self.assertIn(
                {"method": "turn/completed",
                 "session_id": sid},
                rec["lifecycle"])
            self.assertNotIn("turn/completed", rec.get(
                "turn_notification_counts", {}))
        finally:
            self._close(handle)

    def _close_quiet(self, handle):
        try:
            muse_mod.msp_worker_close(handle, reason="t")
        except muse_mod.MspError:
            pass


class PolicyCase(unittest.TestCase):
    def test_subscription_profile_unchanged_golden(self):
        got = ncontain.build_subscription_worker_policy_text(
            "/private/tmp/fake276/root",
            "/Users/fake276/.local/bin/muse-bin-test",
            "/Users/fake276")
        self.assertEqual(got, GOLDEN_SUBSCRIPTION_TEXT)
        self.assertNotIn("network-outbound", got)

    def test_canary_is_base_plus_exactly_two_lines(self):
        import difflib
        sub = ncontain.build_subscription_worker_policy_text(
            FAKE_ROOT, FAKE_EXE, FAKE_HOME)
        got = ncontain.build_subscription_canary_policy_text(
            FAKE_ROOT, FAKE_EXE, FAKE_HOME)
        diff = list(difflib.unified_diff(
            sub.splitlines(), got.splitlines(),
            "subscription", "canary", lineterm=""))
        added = [ln[1:] for ln in diff
                 if ln.startswith("+")
                 and not ln.startswith("+++")]
        removed = [ln for ln in diff
                   if ln.startswith("-")
                   and not ln.startswith("---")]
        self.assertEqual(removed, [])
        self.assertEqual(added, list(CANARY_LINES))
        for line in CANARY_LINES:
            self.assertEqual(got.count(line), 1)

    def test_canary_create_writes_identity(self):
        with tempfile.TemporaryDirectory(
                prefix="msp294-pol-") as tmp:
            root = os.path.realpath(tmp)
            os.chmod(root, 0o700)
            pin = {"path": FAKE_EXE, "real_path": FAKE_EXE,
                   "sha256": "0" * 64, "size": 8,
                   "dev": 1, "ino": 2, "mode": "0o755",
                   "offline_only": True,
                   "admits_worker": False}
            with mock.patch.object(
                    ncontain, "_repinned",
                    return_value=dict(pin)):
                ident = ncontain.create_subscription_canary_policy(
                    root, pin, FAKE_HOME)
            self.assertTrue(ident["policy_path"].endswith(
                "/" + ncontain.POLICY_NAME))
            text = Path(ident["policy_path"]).read_text(
                encoding="utf-8")
            for line in CANARY_LINES:
                self.assertIn(line, text)
            self.assertEqual(ident["profile"], "canary")
            with self.assertRaises(ncontain.ContainmentError):
                with mock.patch.object(
                        ncontain, "_repinned",
                        return_value=dict(pin)):
                    ncontain.create_subscription_canary_policy(
                        root, pin, FAKE_HOME)

    def test_canary_create_refuses_bad_pin(self):
        with tempfile.TemporaryDirectory(
                prefix="msp294-pol-") as tmp:
            root = os.path.realpath(tmp)
            os.chmod(root, 0o700)
            with self.assertRaises(ncontain.ContainmentError):
                ncontain.create_subscription_canary_policy(
                    root, "not-a-dict", FAKE_HOME)
            pin = {"path": FAKE_EXE, "admits_worker": True}
            with self.assertRaises(ncontain.ContainmentError):
                ncontain.create_subscription_canary_policy(
                    root, pin, FAKE_HOME)


class SpawnProfileCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def _spawn_root(self):
        root_tmp = tempfile.TemporaryDirectory(
            prefix="msp294-root-")
        self.addCleanup(root_tmp.cleanup)
        root = os.path.realpath(root_tmp.name)
        os.chmod(root, 0o700)
        return root

    def test_canary_env_mirrors_subscription(self):
        import pwd
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(
            root, profile="subscription-canary")
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
            _cmd, env, _cwd = self.spawned[-1]
            self.assertEqual(env["HOME"], home)
            self.assertEqual(env["XDG_CONFIG_HOME"],
                             os.path.join(home, ".config"))
            for key in ("XDG_DATA_HOME", "XDG_CACHE_HOME",
                        "TMPDIR"):
                self.assertTrue(
                    env[key].startswith(root + "/"), key)
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertEqual(rec["profile"],
                             "subscription-canary")
            self.assertEqual(rec["worker_home"], home)
            called = muse_mod._msp_build_wrapper.call_args
            self.assertEqual(called[1].get("profile"),
                             "subscription-canary")
        finally:
            self._close(handle)


class CanaryTurnCase(Worker273Mixin, unittest.TestCase):
    PROMPT = (
        "Owned capability probe (no target data): reply with "
        "one JSON line {\"probe\": \"preflight\", \"pfid\": "
        "\"canary\"}. Do not read, write, or describe any "
        "repository.")

    def setUp(self):
        self._worker_fixture()

    def _spawn_root(self):
        root_tmp = tempfile.TemporaryDirectory(
            prefix="msp294-root-")
        self.addCleanup(root_tmp.cleanup)
        root = os.path.realpath(root_tmp.name)
        os.chmod(root, 0o700)
        return root

    def _boot_script(self, root):
        home = os.path.join(root, "data", "muse")
        return {"initialize": [_msp_test_ok(
                    1, _msp_test_init_result(home))],
                "account/read": [_msp_test_ok(
                    2, {"state": "accountLogin",
                        "credentialRequired": True})]}

    def _admitted_canary(self):
        """Bootstrap+session+settings+hooks+canary on canary."""
        self._stop_worker_patches()
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(
            root, profile="subscription-canary")
        self.addCleanup(self._close_quiet, handle)
        child._script.update(self._boot_script(root))
        muse_mod.msp_worker_bootstrap(handle, timeout=10,
                                      app_version="t")
        self.root = root
        self._start_session(handle, child, 3)
        rec = muse_mod._MSP_WORKERS[handle]
        sid = rec["worker_session"]["id"]
        child._script["settings/read"] = [_msp_test_ok(
            4, _settings_result())]
        muse_mod.msp_worker_settings_read(handle, timeout=10)
        child._script["hook/list"] = [_msp_test_ok(
            5, {"hooks": []})]
        muse_mod.msp_worker_hooks(handle, timeout=10)
        admitted = muse_mod.canary_admission(handle)
        self.assertTrue(admitted["admitted_once"])
        return handle, child, sid

    def _close_quiet(self, handle):
        try:
            muse_mod.msp_worker_close(handle, reason="t")
        except muse_mod.MspError:
            pass

    def _turn_ack(self, req_id, command_id, turn_id):
        return _msp_test_ok(req_id, {
            "commandId": command_id,
            "disposition": "started",
            "startedNewTurn": True, "status": "accepted",
            "turnId": turn_id})

    def test_no_prompt_parameter_pinned_constant(self):
        params = list(inspect.signature(
            muse_mod.msp_worker_canary_turn).parameters)
        self.assertEqual(params, ["handle", "timeout"])
        self.assertEqual(muse_mod._MSP_CANARY_PROMPT,
                         self.PROMPT)
        self.assertIn('"pfid": "canary"',
                      muse_mod._MSP_CANARY_PROMPT)

    def test_happy_path_with_fixed_command_id(self):
        handle, child, sid = self._admitted_canary()
        try:
            fixed_cmd = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e02"
            turn_id = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e01"
            child._script["turn/start"] = [self._turn_ack(
                6, fixed_cmd, turn_id)]
            # One payload: the scripted child rewrites the
            # whole method script on EVERY request, so a
            # second payload id would arrive unpending and
            # break the host with unknown-id.
            child._script["session/read"] = [
                _msp_test_ok(7, _read_result(
                    [{"kind": "userMessage",
                      "text": self.PROMPT},
                     {"kind": "reasoning", "text": "r"},
                     {"kind": "agentMessage",
                      "text": "{\"probe\": \"ok\"}"}],
                    session={"tokenUsage": {"input": 12,
                                           "output": 34}}))]
            rec = muse_mod._MSP_WORKERS[handle]
            for method in ("item/delta", "item/delta",
                           "session/tokenUsage"):
                raw = json.dumps(
                    {"jsonrpc": "2.0", "method": method,
                     "params": {"sessionId": sid}}).encode()
                muse_mod._dispatch_worker_inbox(rec, raw)
            raw = json.dumps(
                {"jsonrpc": "2.0", "method": "turn/completed",
                 "params": {"sessionId": sid}}).encode()
            muse_mod._dispatch_worker_inbox(rec, raw)
            with mock.patch.object(
                    muse_mod, "_msp_new_command_id",
                    return_value=fixed_cmd):
                with mock.patch.object(
                        muse_mod.time, "sleep",
                        return_value=None):
                    out = muse_mod.msp_worker_canary_turn(
                        handle, timeout=30)
            self.assertEqual(out["stop"], "completed")
            self.assertEqual(out["ack"]["turn_id"], turn_id)
            self.assertEqual(out["ack"]["disposition"],
                             "started")
            self.assertEqual(
                out["item_kinds"],
                ["userMessage", "reasoning", "agentMessage"])
            self.assertIn("{\"probe\": \"ok\"}",
                          out["text"])
            self.assertFalse(out["text_truncated"])
            self.assertEqual(out["token_counts"],
                             {"tokenUsage.input": 12,
                              "tokenUsage.output": 34})
            models = dict(out["model_strings"])
            self.assertEqual(models.get("modelId"),
                             "muse-spark-1.3")
            self.assertEqual(out["ledger_counts"].get(
                "item/delta"), 2)
            self.assertIsNone(out["tool_hit"])
            self.assertIsNone(out["error"])
            # The pinned prompt went on the wire exactly once.
            sent = [o for o in self._methods(child)
                    if o.get("method") == "turn/start"]
            self.assertEqual(len(sent), 1)
            self.assertEqual(
                sent[0]["params"]["input"],
                [{"type": "text", "text": self.PROMPT}])
            self.assertIn(handle, muse_mod._MSP_WORKERS)
        finally:
            self._close(handle)

    def test_text_cap_truncates(self):
        handle, child, sid = self._admitted_canary()
        try:
            fixed_cmd = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e03"
            turn_id = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e04"
            child._script["turn/start"] = [self._turn_ack(
                6, fixed_cmd, turn_id)]
            big = "x" * 5000
            child._script["session/read"] = [_msp_test_ok(
                7, _read_result([{"kind": "agentMessage",
                                  "text": big}]))]
            rec = muse_mod._MSP_WORKERS[handle]
            raw = json.dumps(
                {"jsonrpc": "2.0", "method": "turn/completed",
                 "params": {"sessionId": sid}}).encode()
            muse_mod._dispatch_worker_inbox(rec, raw)
            with mock.patch.object(
                    muse_mod, "_msp_new_command_id",
                    return_value=fixed_cmd):
                with mock.patch.object(
                        muse_mod.time, "sleep",
                        return_value=None):
                    out = muse_mod.msp_worker_canary_turn(
                        handle, timeout=30)
            self.assertEqual(out["stop"], "completed")
            self.assertTrue(out["text_truncated"])
            self.assertLessEqual(out["text_bytes"], 4096)
        finally:
            self._close(handle)

    def test_each_non_message_kind_stops_tool_attempted(self):
        bad_items = [
            ({"kind": "webSearch", "name": "w"}, "webSearch"),
            ({"kind": "userShell"}, "userShell"),
            ({"kind": "toolCall"}, "toolCall"),
            ({"kind": "SomethingNew"}, "SomethingNew"),
            ({"text": "no-kind"}, "missing-kind"),
            ({"kind": None}, "missing-kind"),
            ({"kind": 7}, "non-string-kind"),
        ]
        for item, want_kind in bad_items:
            handle, child, _sid = self._admitted_canary()
            try:
                with self.subTest(item=item):
                    fixed = ("0193e0f0-7a2b-7c3d-9e4f-"
                             "5a6b7c8d9e%02d" % 10)
                    turn_id = ("0193e0f0-7a2b-7c3d-9e4f-"
                               "5a6b7c8d9e%02d" % 11)
                    child._script["turn/start"] = [
                        self._turn_ack(6, fixed, turn_id)]
                    child._script["session/read"] = [
                        _msp_test_ok(7, _read_result(
                            [{"kind": "userMessage",
                              "text": "q"}, item]))]
                    with mock.patch.object(
                            muse_mod, "_msp_new_command_id",
                            return_value=fixed):
                        with mock.patch.object(
                                muse_mod.time, "sleep",
                                return_value=None):
                            out = muse_mod.\
                                msp_worker_canary_turn(
                                    handle, timeout=30)
                    self.assertEqual(out["stop"],
                                     "tool-attempted")
                    self.assertEqual(out["tool_hit"]["kind"],
                                     want_kind)
                    self.assertNotIn(handle,
                                     muse_mod._MSP_WORKERS)
                    methods = [o.get("method")
                               for o in self._methods(child)]
                    self.assertNotIn("usage/read", methods)
            finally:
                self._close_quiet(handle)

    def test_refuses_without_admission(self):
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(
            root, profile="subscription-canary")
        try:
            child._script.update(self._boot_script(root))
            muse_mod.msp_worker_bootstrap(handle, timeout=10,
                                          app_version="t")
            self.root = root
            self._start_session(handle, child, 3)
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.msp_worker_canary_turn(
                    handle, timeout=10)
            self.assertEqual(cm.exception.code,
                             "canary-not-admitted")
            methods = [o.get("method")
                       for o in self._methods(child)]
            self.assertNotIn("turn/start", methods)
        finally:
            self._close(handle)

    def test_refuses_on_wrong_profile(self):
        handle, child, _sid = self._admitted_canary()
        try:
            rec = muse_mod._MSP_WORKERS[handle]
            rec["profile"] = "product"
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.msp_worker_canary_turn(
                    handle, timeout=10)
            self.assertEqual(cm.exception.code,
                             "canary-profile")
            methods = [o.get("method")
                       for o in self._methods(child)]
            self.assertNotIn("turn/start", methods)
        finally:
            self._close(handle)

    def test_second_use_refuses_without_second_write(self):
        handle, child, sid = self._admitted_canary()
        try:
            fixed_cmd = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e05"
            turn_id = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e06"
            child._script["turn/start"] = [self._turn_ack(
                6, fixed_cmd, turn_id)]
            child._script["session/read"] = [_msp_test_ok(
                7, _read_result([{"kind": "agentMessage",
                                  "text": "done"}]))]
            rec = muse_mod._MSP_WORKERS[handle]
            raw = json.dumps(
                {"jsonrpc": "2.0", "method": "turn/completed",
                 "params": {"sessionId": sid}}).encode()
            muse_mod._dispatch_worker_inbox(rec, raw)
            with mock.patch.object(
                    muse_mod, "_msp_new_command_id",
                    return_value=fixed_cmd):
                with mock.patch.object(
                        muse_mod.time, "sleep",
                        return_value=None):
                    out = muse_mod.msp_worker_canary_turn(
                        handle, timeout=30)
            self.assertEqual(out["stop"], "completed")
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.msp_worker_canary_turn(
                    handle, timeout=10)
            self.assertEqual(cm.exception.code,
                             "canary-already-sent")
            sent = [o for o in self._methods(child)
                    if o.get("method") == "turn/start"]
            self.assertEqual(len(sent), 1)
        finally:
            self._close(handle)

    def test_timeout_closes(self):
        handle, child, _sid = self._admitted_canary()
        try:
            fixed_cmd = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e07"
            turn_id = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e08"
            child._script["turn/start"] = [self._turn_ack(
                6, fixed_cmd, turn_id)]
            # One payload: the scripted child rewrites the
            # whole method script on EVERY request, so extra
            # payload ids would arrive unpending and break
            # the host with unknown-id instead of timing out.
            child._script["session/read"] = [
                _msp_test_ok(7, _read_result(
                    [{"kind": "userMessage",
                      "text": "q"}]))]
            with mock.patch.object(
                    muse_mod, "_msp_new_command_id",
                    return_value=fixed_cmd):
                out = muse_mod.msp_worker_canary_turn(
                    handle, timeout=0.3)
            self.assertEqual(out["stop"], "timeout")
            self.assertNotIn(handle, muse_mod._MSP_WORKERS)
        finally:
            self._close_quiet(handle)

    def test_foreign_ack_is_error_stop(self):
        handle, child, _sid = self._admitted_canary()
        try:
            fixed_cmd = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e09"
            other_cmd = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e0a"
            turn_id = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e0b"
            child._script["turn/start"] = [self._turn_ack(
                6, other_cmd, turn_id)]
            with mock.patch.object(
                    muse_mod, "_msp_new_command_id",
                    return_value=fixed_cmd):
                with mock.patch.object(
                        muse_mod.time, "sleep",
                        return_value=None):
                    out = muse_mod.msp_worker_canary_turn(
                        handle, timeout=30)
            self.assertEqual(out["stop"], "error")
            self.assertEqual(out["error"]["code"],
                             "command-mismatch")
            self.assertNotIn(handle, muse_mod._MSP_WORKERS)
            # Single use is consumed even on a bad ack.
            handle2, _child2, _sid2 = self._admitted_canary()
            try:
                rec2 = muse_mod._MSP_WORKERS[handle2]
                rec2["worker_canary_turn_sent"] = True
                with self.assertRaises(
                        muse_mod.MspError) as cm:
                    muse_mod.msp_worker_canary_turn(
                        handle2, timeout=10)
                self.assertEqual(cm.exception.code,
                                 "canary-already-sent")
            finally:
                self._close_quiet(handle2)
        finally:
            self._close_quiet(handle)

    def test_poll_error_is_error_stop(self):
        handle, child, _sid = self._admitted_canary()
        try:
            fixed_cmd = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e0c"
            turn_id = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e0d"
            child._script["turn/start"] = [self._turn_ack(
                6, fixed_cmd, turn_id)]
            child._script["session/read"] = [
                _msp_test_err(7)]
            with mock.patch.object(
                    muse_mod, "_msp_new_command_id",
                    return_value=fixed_cmd):
                with mock.patch.object(
                        muse_mod.time, "sleep",
                        return_value=None):
                    out = muse_mod.msp_worker_canary_turn(
                        handle, timeout=30)
            self.assertEqual(out["stop"], "error")
            self.assertEqual(out["error"]["code"],
                             "method-error")
            self.assertNotIn(handle, muse_mod._MSP_WORKERS)
        finally:
            self._close_quiet(handle)

    def test_direct_turn_start_refuses_off_profile(self):
        for profile in ("product", "subscription"):
            handle, child, sid = self._admitted_canary()
            try:
                with self.subTest(profile=profile):
                    rec = muse_mod._MSP_WORKERS[handle]
                    rec["profile"] = profile
                    before = len(child.raw_requests)
                    with self.assertRaises(
                            muse_mod.MspError) as cm:
                        muse_mod._worker_send_request(
                            handle, "turn/start",
                            {"commandId": "c-direct",
                             "sessionId": sid,
                             "input": [{"type": "text",
                                        "text": "x"}]}, 10)
                    self.assertEqual(cm.exception.code,
                                     "canary-profile")
                    self.assertEqual(len(child.raw_requests),
                                     before)
            finally:
                self._close_quiet(handle)

    def test_direct_turn_start_needs_flag(self):
        handle, child, sid = self._admitted_canary()
        try:
            before = len(child.raw_requests)
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod._worker_send_request(
                    handle, "turn/start",
                    {"commandId": "c-direct",
                     "sessionId": sid,
                     "input": [{"type": "text",
                                "text": "x"}]}, 10)
            self.assertEqual(cm.exception.code,
                             "canary-not-admitted")
            self.assertEqual(len(child.raw_requests), before)
        finally:
            self._close_quiet(handle)

    def test_direct_turn_start_is_one_shot(self):
        handle, child, sid = self._admitted_canary()
        try:
            fixed_cmd = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e21"
            turn_id = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e22"
            child._script["turn/start"] = [self._turn_ack(
                6, fixed_cmd, turn_id)]
            rec = muse_mod._MSP_WORKERS[handle]
            rec["worker_canary_turn_sent"] = True
            raw = muse_mod._worker_send_request(
                handle, "turn/start",
                {"commandId": fixed_cmd,
                 "sessionId": sid,
                 "input": [{"type": "text",
                            "text": self.PROMPT}]}, 10)
            self.assertEqual(raw["commandId"], fixed_cmd)
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod._worker_send_request(
                    handle, "turn/start",
                    {"commandId": fixed_cmd,
                     "sessionId": sid,
                     "input": [{"type": "text",
                                "text": self.PROMPT}]}, 10)
            self.assertEqual(cm.exception.code,
                             "canary-already-sent")
            sent = [o for o in self._methods(child)
                    if o.get("method") == "turn/start"]
            self.assertEqual(len(sent), 1)
        finally:
            self._close_quiet(handle)

    def test_turn_and_capability_still_closed(self):
        handle, _child, _sid = self._admitted_canary()
        try:
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.msp_worker_turn(handle, "hi",
                                         timeout=10)
            self.assertEqual(cm.exception.code,
                             "capability-unproved")
            cap = muse_mod.msp_worker_capability(handle)
            self.assertFalse(cap["admitted"])
        finally:
            self._close(handle)
